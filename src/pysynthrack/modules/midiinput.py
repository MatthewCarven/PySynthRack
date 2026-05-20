"""MIDIInput module — note source driven by a real MIDI device.

Architecturally this is a sibling of :class:`Keyboard` — same shape, same
output ports (plus pitch_cv since v0.4 mid-cycle), same "voices live
inside the module" polyphony model. The only difference is the input
side: instead of DearPyGui key events mutating the held-note state, a
`mido` callback running on its own IO thread does the mutating.

That makes this a *self-polyphonic* module: chords are summed internally
and (until the voice-routing renderer lands) the rest of the patch sees
one mono ``out``, one global ``gate``, one block-constant ``pitch_cv``,
one block-constant ``mod_cv``, and one block-constant ``pressure_cv``.
The 16-slot :class:`VoiceSlots` allocator now backs the held-note
state — this is the model-layer prerequisite for true per-voice routing
in a follow-up slice. The renderer still calls
:meth:`snapshot_active_notes` and gets the same ``{note: velocity}``
dict it always did; it'll switch to :meth:`snapshot_voice_slots` when
the polyphonic renderer slice lands.

Threading model. The mido port runs its own daemon thread; the audio
backend reads from this module on the audio callback. ``self._lock``
guards every piece of MIDI-state mutation: the voice slots, the pitch
wheel, the mod wheel, the channel aftertouch, and the sustain pedal.
The audio thread takes a snapshot copy each block so it never iterates
state the MIDI thread might be mutating.

mido optionality. ``mido`` and ``python-rtmidi`` are an opt-in extra
(``pip install -e ".[midi]"``). If the install is missing, this module
*still imports successfully* and the class is still registered — the UI
needs to be able to show the palette entry regardless. ``start_midi()``
is where the missing-import is reported, with a log line that explains
what to install.

Message semantics:
  * ``note_on`` with velocity > 0  -> allocate a voice slot with the
                                     normalized velocity
  * ``note_on`` with velocity == 0 -> treat as ``note_off`` (the
                                     running-status optimization most
                                     controllers use)
  * ``note_off``                   -> release the matching slot (or
                                     mark it sustained if the pedal
                                     is currently down)
  * ``pitchwheel``                 -> update pitch bend state
                                     (msg.pitch / 8192.0, clamped to +-1)
  * CC 1 (mod wheel)               -> update mod wheel state
                                     (msg.value / 127.0, clamped [0, 1])
  * CC 64 (sustain pedal)          -> >= 64 means pedal down, < 64 means
                                     pedal up. Pedal-down causes the
                                     next note_off events to leave their
                                     slots sustained (gate stays high)
                                     until the pedal is released.
  * ``aftertouch`` (channel)       -> update channel-pressure state
                                     (msg.value / 127.0, clamped [0, 1])
  * CC 123 (All Notes Off)         -> clear all notes via VoiceSlots
                                     (pitch wheel, mod wheel, aftertouch,
                                     and pedal state are independent —
                                     not reset)
  * Everything else                -> ignored at this slice
                                     (polyphonic aftertouch lands with
                                     the voice-aware renderer)
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from ..core.module import Module, register_module_type
from ..core.port import Port
from ..core.voicing import VoiceSlots, VoiceSnapshot

logger = logging.getLogger(__name__)

# Import-guard mido so this module loads cleanly without the [midi] extra.
# The UI palette and JSON loader will still see MIDIInput in the registry;
# the missing-import becomes a runtime error only when start_midi() is
# actually called.
try:
    import mido  # type: ignore[import-untyped]

    _MIDO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when mido absent
    mido = None  # type: ignore[assignment]
    _MIDO_AVAILABLE = False


# Sentinel device name for the "auto-pick the first available device" path.
AUTO_DEVICE = ""

# MIDI CC 64 (sustain pedal) on/off threshold. The MIDI spec defines
# values 0-63 as "off" and 64-127 as "on" for a switch-style CC.
_SUSTAIN_ON_THRESHOLD = 64


def available_devices() -> list[str]:
    """List MIDI input device names; empty list if mido absent or no devices."""
    if not _MIDO_AVAILABLE:
        return []
    try:
        return list(mido.get_input_names())
    except Exception as e:  # pragma: no cover - depends on host MIDI stack
        logger.warning("Failed to enumerate MIDI inputs: %s", e)
        return []


@register_module_type
class MIDIInput(Module):
    """MIDI-driven polyphonic note source.

    Parameters:
        device: MIDI input device name, or ``""`` to auto-pick the first
            available device. The full name list is available via
            :func:`available_devices`.
        channel: MIDI channel filter, 1-16 in the standard hardware
            numbering. ``0`` means "all channels" (omni mode).
        octave_shift: Integer transpose applied at note arrival. -2 drops
            two octaves, +1 raises one octave.
        velocity_sensitive: When True, per-voice amplitude scales with
            note-on velocity (0-127 mapped to 0-1). When False, every note
            plays at unit velocity — useful when the controller has bad
            velocity curves.
        waveform: ``"sine"`` / ``"saw"`` / ``"square"`` / ``"triangle"``.
        volume: Master output level applied after voice summing, in [0, 1].
        bend_range: Pitch-wheel range in semitones. +-1.0 of normalized
            wheel deflection maps to +-``bend_range`` semitones, applied
            as a 1V/octave CV value of ``bend_normalized * bend_range /
            12`` both to the internal voices and to the ``pitch_cv``
            output port. GM standard is 2.0 (a whole tone each way); set
            higher for dive-bomb leads (12.0 = one octave each way),
            lower for subtle expressive vibrato (0.5).
        mod_scale: Multiplier applied to the normalized mod-wheel value
            (CC 1, range [0, 1]) before emission on the ``mod_cv``
            output port. Default 1.0 emits the wheel position verbatim;
            crank to e.g. 2.0 so a fully deflected wheel takes a
            downstream ``cutoff_cv`` consumer two octaves up (1V/oct =
            one octave per unit), or set above 1 for any effect that
            needs more dynamic range than [0, 1] allows.
        pressure_scale: Multiplier applied to the normalized channel-
            aftertouch value before emission on the ``pressure_cv``
            output port. Same shape as ``mod_scale`` -- the source
            publishes raw [0, 1] x scale and the downstream consumer
            decides what the units mean. Mod wheel and aftertouch are
            both "depth knobs" in the controller world but feel
            different to the player: mod wheel is a separate left-hand
            control, aftertouch is post-press pressure on the keys
            already held.

    Runtime state (not serialized):
        voices: :class:`VoiceSlots` allocator backing the held-note
            state. Held notes, sustained notes, and released-but-not-
            yet-stolen voices all live here.
        _pitch_bend: float in [-1, 1], wheel deflection normalized.
        _mod_wheel: float in [0, 1], mod-wheel value normalized.
        _aftertouch: float in [0, 1], channel-pressure value normalized.
        _midi_port: The open mido input port, or None when stopped.
    """

    TYPE = "midi_input"
    DEFAULT_PARAMS = {
        "device": AUTO_DEVICE,
        "channel": 0,
        "octave_shift": 0,
        "velocity_sensitive": True,
        "waveform": "sine",
        "volume": 0.5,
        "bend_range": 2.0,
        "mod_scale": 1.0,
        "pressure_scale": 1.0,
    }
    INPUT_PORTS: list[Port] = []
    # Mirrors Keyboard plus a pitch_cv output that carries the wheel
    # value as a 1V/oct CV signal. Internally the same value is also
    # applied to the playing voices, so the wheel "just works" with no
    # cabling required.
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("gate", "out", "gate"),
        Port("pitch_cv", "out", "cv"),
        Port("mod_cv", "out", "cv"),
        Port("pressure_cv", "out", "cv"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 16-slot voice allocator. Replaces the flat ``active_notes`` dict
        # that earlier versions used — the slot index is what the voice-
        # aware renderer (next slice) will key its per-voice state off.
        # ``snapshot_active_notes()`` is preserved as a thin proxy over
        # ``voices.held_notes()`` so existing callers and tests don't
        # notice the change.
        self.voices: VoiceSlots = VoiceSlots()
        # Pitch wheel deflection in [-1, 1]. 0 = wheel at rest. Lives
        # under the same lock as the voice allocator so the audio thread
        # can take a consistent snapshot of all MIDI state.
        self._pitch_bend: float = 0.0
        # Mod wheel value in [0, 1]. Unipolar (CC 1 only goes 0..127).
        self._mod_wheel: float = 0.0
        # Channel aftertouch (pressure) value in [0, 1]. Unipolar -
        # MIDI aftertouch goes 0..127, never negative. Note that this
        # is *channel* aftertouch, applied identically to every voice;
        # polyphonic aftertouch (per-note pressure) needs voice-aware
        # signals and lands with the voice routing renderer slice.
        self._aftertouch: float = 0.0
        self._lock = threading.Lock()
        self._midi_port: Any = None
        self._opened_device: str = ""

    # ----- mido integration -----------------------------------------------

    def start_midi(self) -> None:
        """Open the MIDI port and register the callback.

        Idempotent — calling twice without an intervening ``stop_midi()`` is
        a no-op as long as the configured device hasn't changed.
        """
        if not _MIDO_AVAILABLE:
            logger.warning(
                "MIDIInput requested but mido is not installed. "
                "Run: pip install -e \".[midi]\""
            )
            return

        desired_device = str(self.params.get("device", AUTO_DEVICE))
        # If we already have the right port open, nothing to do.
        if self._midi_port is not None and self._opened_device == desired_device:
            return
        # Different device than what we have open -> close and reopen.
        if self._midi_port is not None:
            self.stop_midi()

        try:
            devices = list(mido.get_input_names())
        except Exception as e:  # pragma: no cover - host MIDI stack
            logger.warning("Could not list MIDI inputs: %s", e)
            return

        if not devices:
            logger.info("MIDIInput: no MIDI input devices found on this host.")
            return

        device_name = desired_device if desired_device else devices[0]
        if device_name not in devices:
            logger.warning(
                "MIDIInput: configured device %r not found; available: %s",
                device_name,
                devices,
            )
            return

        try:
            # ``callback=`` makes mido invoke our handler on its own IO
            # thread for each incoming message — no manual thread juggling
            # on our side. The lock around mutation is what keeps it safe
            # for the audio thread to read concurrently.
            self._midi_port = mido.open_input(device_name, callback=self._on_message)
            self._opened_device = device_name
            logger.info("MIDIInput: opened %r", device_name)
        except Exception as e:  # pragma: no cover - host MIDI stack
            logger.warning("MIDIInput: failed to open %r: %s", device_name, e)
            self._midi_port = None

    def stop_midi(self) -> None:
        """Close the MIDI port and clear pending state."""
        port = self._midi_port
        self._midi_port = None
        self._opened_device = ""
        if port is not None:
            try:
                port.close()
            except Exception as e:  # pragma: no cover - host MIDI stack
                logger.debug("MIDIInput close raised: %s", e)
        # Flush any hung notes so the next compile starts silent. Reset
        # all physical-control state too — a stale wheel value or stuck
        # pedal from a previous session shouldn't leak into the next
        # compile.
        with self._lock:
            self.voices.all_notes_off()
            # set_sustain(False) also clears any sustained slots, but
            # since all_notes_off has just cleared every slot to empty
            # this is purely a pedal-state reset.
            self.voices.set_sustain(False)
            self._pitch_bend = 0.0
            self._mod_wheel = 0.0
            self._aftertouch = 0.0

    # ----- message handling -----------------------------------------------

    def _on_message(self, msg: Any) -> None:
        """Mido callback. Runs on the mido IO thread, not the audio thread.

        Kept narrow on purpose: parse the message, route to a public method,
        return. All the lock juggling is in the public mutator methods.
        """
        # Channel filter. mido reports 0-indexed channels (0-15); our
        # human-facing param is 1-indexed (1-16) with 0 meaning omni.
        channel_param = int(self.params.get("channel", 0))
        if channel_param != 0:
            msg_channel = getattr(msg, "channel", None)
            if msg_channel is None or msg_channel != channel_param - 1:
                return

        if msg.type == "note_on":
            if msg.velocity > 0:
                self.note_on(msg.note, msg.velocity / 127.0)
            else:
                # Running-status note-off: many controllers send note_on
                # with velocity 0 instead of an explicit note_off.
                self.note_off(msg.note)
        elif msg.type == "note_off":
            self.note_off(msg.note)
        elif msg.type == "control_change" and msg.control == 123:
            # CC 123 — All Notes Off. Standard panic message. Does NOT
            # reset pitch wheel, mod wheel, aftertouch, or the sustain
            # pedal — their physical positions are independent of
            # held-note state.
            self.all_notes_off()
        elif msg.type == "control_change" and msg.control == 1:
            # CC 1 — mod wheel. Normalized to [0, 1] (unipolar; the
            # wheel only goes up from rest, never below 0).
            self.set_mod_wheel(msg.value / 127.0)
        elif msg.type == "control_change" and msg.control == 64:
            # CC 64 — sustain pedal. Switch-style CC: 0-63 is "off",
            # 64-127 is "on". On pedal-up, every currently-sustained
            # voice transitions to released in one shot (handled
            # inside VoiceSlots.set_sustain).
            self.set_sustain(msg.value >= _SUSTAIN_ON_THRESHOLD)
        elif msg.type == "aftertouch":
            # Channel aftertouch — one pressure value per channel,
            # applied to all held notes equally. Normalized to [0, 1].
            self.set_aftertouch(msg.value / 127.0)
        elif msg.type == "pitchwheel":
            # mido reports msg.pitch as a signed integer in [-8192, 8191].
            # Normalize to [-1, 1] by dividing by 8192. The +8191 case
            # gives 0.99988 which is fine; the asymmetry is in the MIDI
            # spec, not our problem.
            self.set_pitch_bend(msg.pitch / 8192.0)
        # Other message types (polyphonic aftertouch ``polytouch``) are
        # intentionally ignored for now — that needs voice-aware CV
        # signals to do faithfully, which lands with the voice-routing
        # renderer slice.

    # ----- public note ingest ---------------------------------------------

    def note_on(self, midi_note: int, velocity: float = 1.0) -> None:
        """Apply octave shift and allocate a voice slot for the note.

        ``velocity`` is expected in [0, 1]. The MIDI callback normalises
        from the raw 0-127 range; tests calling this method directly can
        pass either form (we clamp into [0, 1]).
        """
        if velocity <= 0.0:
            self.note_off(midi_note)
            return
        shifted = int(midi_note) + 12 * int(self.params.get("octave_shift", 0))
        if shifted < 0 or shifted > 127:
            return  # Drop transposes that escape the MIDI range.
        v = max(0.0, min(1.0, float(velocity)))
        with self._lock:
            self.voices.allocate(shifted, v)

    def note_off(self, midi_note: int) -> None:
        shifted = int(midi_note) + 12 * int(self.params.get("octave_shift", 0))
        with self._lock:
            self.voices.release(shifted)

    def all_notes_off(self) -> None:
        with self._lock:
            self.voices.all_notes_off()

    def snapshot_active_notes(self) -> dict[int, float]:
        """Return ``{note: velocity}`` for currently-held keys.

        Stable across the voice-routing migration: only physically-held
        keys appear here. A note that's only being kept alive by the
        sustain pedal does NOT appear in this dict (its finger is up).
        The audio thread can use this for the pre-voice-routing render
        path; the voice-aware renderer will switch to
        :meth:`snapshot_voice_slots` when it lands.
        """
        with self._lock:
            return self.voices.held_notes()

    def snapshot_voice_slots(self) -> list[VoiceSnapshot]:
        """Return a 16-element per-slot snapshot for the voice-aware renderer.

        Slot index is the addressable voice id. Every entry is present;
        unused slots have ``note=-1`` and ``gating=False``. See
        :class:`pysynthrack.core.voicing.VoiceSlots` for slot semantics.
        """
        with self._lock:
            return self.voices.snapshot()

    # ----- sustain pedal --------------------------------------------------

    def set_sustain(self, on: bool) -> None:
        """Set the sustain pedal state.

        Pedal-down: subsequent note_off events leave the slot
        ``sustained`` (gate stays high) until the pedal is released.
        Pedal-up: every currently-sustained slot transitions to
        released in one pass; their gates fall on the next block.
        """
        with self._lock:
            self.voices.set_sustain(bool(on))

    def snapshot_sustain_pedal(self) -> bool:
        """Return the current sustain pedal state. Mostly for tests."""
        with self._lock:
            return self.voices.sustain_pedal

    # ----- pitch bend -----------------------------------------------------

    def set_pitch_bend(self, normalized: float) -> None:
        """Set the wheel position. ``normalized`` is clamped to [-1, 1].

        0 means wheel at rest. +-1 means fully deflected in either
        direction. The actual semitone offset applied at render time is
        ``normalized * bend_range``; the value emitted on ``pitch_cv``
        is ``normalized * bend_range / 12`` (1V/oct).
        """
        v = max(-1.0, min(1.0, float(normalized)))
        with self._lock:
            self._pitch_bend = v

    def snapshot_pitch_bend(self) -> float:
        """Return the current normalized wheel deflection in [-1, 1]."""
        with self._lock:
            return self._pitch_bend

    # ----- mod wheel ------------------------------------------------------

    def set_mod_wheel(self, normalized: float) -> None:
        """Set the mod wheel position. ``normalized`` is clamped to [0, 1].

        0 means wheel at rest. 1 means fully deflected. Emitted on the
        ``mod_cv`` output port multiplied by ``mod_scale``.
        """
        v = max(0.0, min(1.0, float(normalized)))
        with self._lock:
            self._mod_wheel = v

    def snapshot_mod_wheel(self) -> float:
        """Return the current normalized mod wheel value in [0, 1]."""
        with self._lock:
            return self._mod_wheel

    # ----- channel aftertouch ---------------------------------------------

    def set_aftertouch(self, normalized: float) -> None:
        """Set the channel-pressure value. ``normalized`` clamped to [0, 1].

        0 means no pressure (keys at rest). 1 means full pressure.
        Emitted on the ``pressure_cv`` output port multiplied by
        ``pressure_scale``. This is channel aftertouch -- one value per
        channel, applied identically to every held voice. Polyphonic
        aftertouch (per-note pressure) needs voice-aware CV signals
        and lands with the voice routing slice.
        """
        v = max(0.0, min(1.0, float(normalized)))
        with self._lock:
            self._aftertouch = v

    def snapshot_aftertouch(self) -> float:
        """Return the current normalized channel-pressure value in [0, 1]."""
        with self._lock:
            return self._aftertouch
