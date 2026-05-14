"""MIDIInput module — note source driven by a real MIDI device.

Architecturally this is a sibling of :class:`Keyboard` — same shape, same
output ports, same "voices live inside the module" polyphony model. The
only difference is the input side: instead of DearPyGui key events
mutating ``active_notes``, a `mido` callback running on its own IO
thread does the mutating.

That makes this a *self-polyphonic* module: chords are summed internally
and the rest of the patch sees one mono ``out`` and one global ``gate``.
True per-voice routing (each note → its own osc/filter/envelope) is the
job of the voice routing manager in a later v0.4 pass, and is
deliberately out of scope here so this slice can ship without
committing to a voice-aware signal model.

Threading model. The mido port runs its own daemon thread; the audio
backend reads ``active_notes`` from the audio callback. Both touch the
shared dict under ``self._lock`` — exactly the same pattern as
``Keyboard``. The audio thread takes a snapshot copy each block so it
never iterates a dict the MIDI thread might be mutating.

mido optionality. ``mido`` and ``python-rtmidi`` are an opt-in extra
(``pip install -e ".[midi]"``). If the install is missing, this module
*still imports successfully* and the class is still registered — the UI
needs to be able to show the palette entry regardless. ``start_midi()``
is where the missing-import is reported, with a log line that explains
what to install.

Note semantics:
  * ``note_on`` with velocity > 0  → add note with normalized velocity
  * ``note_on`` with velocity == 0 → treat as ``note_off`` (the
    running-status optimization most controllers use)
  * ``note_off``                   → remove note
  * CC 123 (All Notes Off)         → clear all
  * Everything else                → ignored at this slice (pitch bend,
    mod wheel, sustain pedal etc. are future work)
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

    Runtime state (not serialized):
        active_notes: ``{midi_note: velocity_normalized_0_1}``.
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
    }
    INPUT_PORTS: list[Port] = []
    # Mirrors Keyboard exactly so a MIDIInput is a drop-in replacement
    # in any patch that currently uses Keyboard.
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("gate", "out", "gate"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # active_notes maps MIDI note number → normalized velocity [0, 1].
        # Storing velocity (not just presence) is what makes velocity_sensitive
        # rendering possible without a parallel dict.
        self.active_notes: dict[int, float] = {}
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
        # Different device than what we have open → close and reopen.
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
        """Close the MIDI port and clear pending notes."""
        port = self._midi_port
        self._midi_port = None
        self._opened_device = ""
        if port is not None:
            try:
                port.close()
            except Exception as e:  # pragma: no cover - host MIDI stack
                logger.debug("MIDIInput close raised: %s", e)
        # Flush any hung notes so the next compile starts silent.
        self.all_notes_off()

    # ----- message handling -----------------------------------------------

    def _on_message(self, msg: Any) -> None:
        """Mido callback. Runs on the mido IO thread, not the audio thread.

        Kept narrow on purpose: parse the message, route to a public method,
        return. All the lock juggling is in note_on / note_off /
        all_notes_off.
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
            # CC 123 — All Notes Off. Standard panic message.
            self.all_notes_off()
        # Other message types (pitchwheel, sustain pedal, mod wheel, etc.)
        # are intentionally ignored for now. They land in v0.4 follow-ups.

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
