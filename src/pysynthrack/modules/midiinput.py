"""MIDIInput module — note source driven by a real MIDI device.

Architecturally this is a sibling of :class:`Keyboard` — same shape, same
output ports (plus pitch_cv since v0.4 mid-cycle), same "voices live
inside the module" polyphony model. The only difference is the input
side: instead of DearPyGui key events mutating ``active_notes``, a
`mido` callback running on its own IO thread does the mutating.

That makes this a *self-polyphonic* module: chords are summed internally
and the rest of the patch sees one mono ``out``, one global ``gate``,
one block-constant ``pitch_cv``, one block-constant ``mod_cv``, and
one block-constant ``pressure_cv``. True per-voice routing (each
note → its own osc/filter/envelope) is the job of the voice routing
manager in a later v0.4+ pass. Polyphonic (per-note) aftertouch is the
one MIDI feature that requires voice-aware signals to do faithfully;
it lands when voice routing does.

Threading model. The mido port runs its own daemon thread; the audio
backend reads ``active_notes``, ``_pitch_bend``, ``_mod_wheel`` and
``_aftertouch`` from the audio callback. All four touch shared state
under ``self._lock`` — exactly the same pattern as ``Keyboard``. The
audio thread takes a snapshot copy each block so it never iterates
state the MIDI thread might be mutating.

mido optionality. ``mido`` and ``python-rtmidi`` are an opt-in extra
(``pip install -e ".[midi]"``). If the install is missing, this module
*still imports successfully* and the class is still registered — the UI
needs to be able to show the palette entry regardless. ``start_midi()``
is where the missing-import is reported, with a log line that explains
what to install.

Message semantics:
  * ``note_on`` with velocity > 0  -> add note with normalized velocity
  * ``note_on`` with velocity == 0 -> treat as ``note_off`` (the
    running-status optimization most controllers use)
  * ``note_off``                   -> remove note
  * ``pitchwheel``                 -> update pitch bend state
                                     (msg.pitch / 8192.0, clamped to +-1)
  * CC 1 (mod wheel)               -> update mod wheel state
                                     (msg.value / 127.0, clamped [0, 1])
  * ``aftertouch`` (channel)       -> update channel-pressure state
                                     (msg.value / 127.0, clamped [0, 1])
  * CC 123 (All Notes Off)         -> clear all notes (pitch wheel, mod
                                     wheel, and aftertouch positions
                                     are all independent — not reset)
  * Everything else                -> ignored at this slice (sustain
                                     pedal CC 64 is next; polyphonic
                                     aftertouch lands with voice routing)
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from ..core.module import Module, register_module_type
from ..core.port import Port

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
        active_notes: ``{midi_note: velocity_normalized_0_1}``.
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
        # active_notes maps MIDI note number -> normalized velocity [0, 1].
        # Storing velocity (not just presence) is what makes
        # velocity_sensitive rendering possible without a parallel dict.
        self.active_notes: dict[int, float] = {}
        # Pitch wheel deflection in [-1, 1]. 0 = wheel at rest. Lives
        # under the same lock as active_notes so the audio thread can
        # take a consistent snapshot of all MIDI state.
        self._pitch_bend: float = 0.0
        # Mod wheel value in [0, 1]. Unipolar (CC 1 only goes 0..127).
        # Also lives under self._lock alongside the rest of MIDI state.
        self._mod_wheel: float = 0.0
        # Channel aftertouch (pressure) value in [0, 1]. Unipolar -
        # MIDI aftertouch goes 0..127, never negative. Note that this
        # is *channel* aftertouch, applied identically to every voice;
        # polyphonic aftertouch (per-note pressure) needs voice-aware
        # signals and lands with the voice routing slice.
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
            # on our side. The lock around active_notes is what keeps it
            # safe for the audio thread to read concurrently.
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
        # the pitch wheel too — a stale wheel value from a previous
        # session shouldn't leak into the next compile.
        self.all_notes_off()
        with self._lock:
            self._pitch_bend = 0.0
            self._mod_wheel = 0.0
            self._aftertouch = 0.0

    # ----- message handling -----------------------------------------------

    def _on_message(self, msg: Any) -> None:
        """Mido callback. Runs on the mido IO thread, not the audio thread.

        Kept narrow on purpose: parse the message, route to a public method,
        return. All the lock juggling is in note_on / note_off /
        all_notes_off / set_pitch_bend.
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
            # reset pitch wheel or mod wheel — their physical positions
            # are independent of held-note state.
            self.all_notes_off()
        elif msg.type == "control_change" and msg.control == 1:
            # CC 1 — mod wheel. Normalized to [0, 1] (unipolar; the
            # wheel only goes up from rest, never below 0).
            self.set_mod_wheel(msg.value / 127.0)
        elif msg.type == "aftertouch":
            # Channel aftertouch — one pressure value per channel,
            # applied to all held notes equally. Normalized to [0, 1].
            # mido attribute is msg.value (unlike CC where it's also
            # msg.value, this is just the channel-pressure byte).
            self.set_aftertouch(msg.value / 127.0)
        elif msg.type == "pitchwheel":
            # mido reports msg.pitch as a signed integer in [-8192, 8191].
            # Normalize to [-1, 1] by dividing by 8192. The +8191 case
            # gives 0.99988 which is fine; the asymmetry is in the MIDI
            # spec, not our problem.
            self.set_pitch_bend(msg.pitch / 8192.0)
        # Other message types (sustain pedal CC 64, polyphonic
        # aftertouch ``polytouch``) are intentionally ignored for now.
        # Sustain pedal lands with the voice-routing slice (it's
        # per-voice state). Polyphonic aftertouch needs voice-aware
        # CV signals to do faithfully, also voice-routing territory.

    # ----- public note ingest ---------------------------------------------

    def note_on(self, midi_note: int, velocity: float = 1.0) -> None:
        """Apply octave shift and record the note with its velocity.

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
            self.active_notes[shifted] = v

    def note_off(self, midi_note: int) -> None:
        shifted = int(midi_note) + 12 * int(self.params.get("octave_shift", 0))
        with self._lock:
            self.active_notes.pop(shifted, None)

    def all_notes_off(self) -> None:
        with self._lock:
            self.active_notes.clear()

    def snapshot_active_notes(self) -> dict[int, float]:
        """Return a thread-safe copy of {note: velocity}."""
        with self._lock:
            return dict(self.active_notes)

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
