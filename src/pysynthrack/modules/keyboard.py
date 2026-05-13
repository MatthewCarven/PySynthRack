"""Keyboard module — computer keys play notes polyphonically.

A Keyboard owns a thread-safe set of currently-pressed MIDI notes. The UI
mutates that set in response to DearPyGui key-press / key-release events;
the audio backend reads it each block to decide which voices to render.

Each voice synthesizes one waveform at the note's frequency. Multiple
voices sum together (polyphony — chord = stack of voices). A short attack
and release ramp prevents click artefacts on note-on / note-off; a full
ADSR is a separate v0.2 module.
"""
from __future__ import annotations

import threading

from ..core.module import Module, register_module_type
from ..core.port import Port


# Standard MIDI: C4 (middle C) = note 60. The formula below puts:
#   octave=4, semitone=0  → note 60 (C4)
#   octave=4, semitone=9  → note 69 (A4 = 440 Hz)
def semitone_to_midi(octave: int, semitone: int) -> int:
    return 12 * (octave + 1) + semitone


def midi_to_freq(midi_note: int) -> float:
    """A4 (note 69) = 440 Hz. Twelve-tone equal temperament."""
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def midi_to_name(midi_note: int) -> str:
    """e.g. 60 → 'C4', 69 → 'A4'."""
    octave = (midi_note // 12) - 1
    return f"{_NOTE_NAMES[midi_note % 12]}{octave}"


@register_module_type
class Keyboard(Module):
    """Polyphonic computer-keyboard input.

    Parameters:
        octave: Base octave for the home row (default 4 → home row starts at C4).
        waveform: Voice waveform — sine / saw / square / triangle.
        volume: Master output level in [0, 1].

    Runtime state (not serialized):
        active_notes: MIDI note numbers currently held down.
    """

    TYPE = "keyboard"
    DEFAULT_PARAMS = {
        "octave": 4,
        "waveform": "sine",
        "volume": 0.5,
    }
    INPUT_PORTS: list[Port] = []
    # ``out`` carries the polyphonic audio. ``gate`` carries a single
    # global note-on signal — high while any key is held, low otherwise —
    # which is what an ADSR envelope listens to in master-envelope mode.
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("gate", "out", "gate"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Transient runtime state — not part of params, not serialized.
        self.active_notes: set[int] = set()
        self._lock = threading.Lock()

    # ----- transport ------------------------------------------------------

    def note_on(self, midi_note: int) -> None:
        with self._lock:
            self.active_notes.add(int(midi_note))

    def note_off(self, midi_note: int) -> None:
        with self._lock:
            self.active_notes.discard(int(midi_note))

    def all_notes_off(self) -> None:
        with self._lock:
            self.active_notes.clear()

    def snapshot_active_notes(self) -> set[int]:
        """Return a thread-safe copy of the currently-pressed notes."""
        with self._lock:
            return set(self.active_notes)
