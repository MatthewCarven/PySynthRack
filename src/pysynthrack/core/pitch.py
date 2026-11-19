"""Note names <-> frequencies, in equal temperament (A4 = 440 Hz).

Shared by the oscillator's note mode (2026-11-19) and the node readouts.
Matthew: "i'd like to implement note selection on the oscillator ... but
at the same time i do not wish to loose control over the final value" --
so a note is only a way of *entering* a frequency: ``note_to_hz`` takes a
continuous ``cents`` offset, and ``hz_to_note`` hands back the exact
remainder, so converting a frequency to note + cents and back is the same
frequency (to float rounding), never a snap.

Note names are a letter, an optional ``#`` or ``b``, and an octave number
(scientific pitch: C4 is middle C, A4 is 440 Hz). ``NOTE_CHOICES`` is the
picker's range, C0 (16.35 Hz) to B8 (7902 Hz).
"""
from __future__ import annotations

import math
import re

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
NOTE_CHOICES = tuple(f"{n}{o}" for o in range(0, 9) for n in NOTE_NAMES)

A4_HZ = 440.0
A4_MIDI = 69

_LETTER = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_RE = re.compile(r"^\s*([A-Ga-g])([#b]?)(-?\d+)\s*$")
_MIDI_MIN = 12          # C0
_MIDI_MAX = 12 * 10 - 1  # B8 = 119


def note_to_midi(note: str) -> int | None:
    """MIDI number of a note name (``"A4"`` -> 69, ``"Db3"`` -> 49), or
    ``None`` if it does not parse."""
    m = _NOTE_RE.match(str(note))
    if not m:
        return None
    letter, accidental, octave = m.groups()
    semitone = _LETTER[letter.upper()] + (1 if accidental == "#" else -1 if accidental == "b" else 0)
    return 12 * (int(octave) + 1) + semitone


def midi_to_note(midi: int) -> str:
    """Note name (sharps) of a MIDI number: 69 -> ``"A4"``."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def note_to_hz(note: str, cents: float = 0.0, a4: float = A4_HZ) -> float | None:
    """Frequency of ``note`` moved by ``cents`` (continuous; 100 = one
    semitone), or ``None`` for a name that does not parse or a non-finite
    ``cents``."""
    midi = note_to_midi(note)
    if midi is None:
        return None
    try:
        c = float(cents)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(c):
        return None
    return a4 * 2.0 ** ((midi - A4_MIDI) / 12.0 + c / 1200.0)


def hz_to_note(hz: float, a4: float = A4_HZ) -> tuple[str, float]:
    """Nearest note in the picker's range (C0..B8) and the exact cents from
    it, so ``note_to_hz(*hz_to_note(f)) == f`` (to float rounding) for any
    ``f`` within half a semitone of the range. Outside it the end note is
    returned with however many cents it takes (the caller may clamp)."""
    if not (math.isfinite(hz) and hz > 0.0):
        return "A4", 0.0
    midi_f = A4_MIDI + 12.0 * math.log2(hz / a4)
    midi = min(max(int(round(midi_f)), _MIDI_MIN), _MIDI_MAX)
    return midi_to_note(midi), (midi_f - midi) * 100.0
