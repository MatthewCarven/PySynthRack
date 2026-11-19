"""Pure text formatters for read-only node readouts (no dpg here).

``format_osc_freq`` (2026-11-19, Matthew: "on the oscillator can we have a
number read only frequency output so we can see in not quite realtime but
like not lazily the output frequency number?"): the oscillator node's live
line, from the backend's ``snapshot_osc_freqs`` entry. ASCII only, like
every node readout -- the node font has no wider glyph coverage.
"""
from __future__ import annotations

import math

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Name the note only where a pitch is a note: below 16 Hz an oscillator is
# a slow LFO, above 20 kHz nobody is singing along.
_NOTE_MIN_HZ = 16.0
_NOTE_MAX_HZ = 20000.0


def format_hz(hz: float) -> str:
    """A frequency at a sensible precision: two decimals below 100 Hz, one
    below 10 kHz, whole hertz above."""
    if hz < 100.0:
        return f"{hz:.2f}"
    if hz < 10000.0:
        return f"{hz:.1f}"
    return f"{hz:.0f}"


def note_of(hz: float) -> str:
    """Nearest equal-tempered note (A4 = 440 Hz) and the offset from it in
    cents, e.g. ``"C#5 -39c"``; ``""`` outside the audible note range."""
    if not (math.isfinite(hz) and _NOTE_MIN_HZ <= hz <= _NOTE_MAX_HZ):
        return ""
    midi = 69.0 + 12.0 * math.log2(hz / 440.0)
    n = int(round(midi))
    cents = int(round((midi - n) * 100.0))
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1} {cents:+d}c"


def format_osc_freq(entry, running: bool = True) -> str:
    """The oscillator node's read-only output-frequency line.

    ``entry`` is ``(lo_hz, hi_hz, voices)`` from
    ``NumpyBackend.snapshot_osc_freqs`` (the frequency at the last sample of
    the last block -- base ``freq`` times ``2 ** freq_cv``), or ``None``
    before the first block. ``running`` False (transport stopped) reads
    ``out: --`` so a stale number never passes for a live one.

    * mono: ``out: 541.9 Hz  C#5 -39c``
    * voices that agree: the same, one number
    * voices that differ: ``out: 130.8-523.3 Hz (16 voices)`` -- the range
      across the voice slots (idle slots keep their last pitch).
    * a non-finite cv: ``out: nan Hz``
    """
    if entry is None or not running:
        return "out: --"
    lo, hi, voices = entry
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return "out: nan Hz"
    if int(voices) <= 1 or abs(hi - lo) < 1e-9:
        note = note_of(lo)
        return f"out: {format_hz(lo)} Hz" + (f"  {note}" if note else "")
    return f"out: {format_hz(lo)}-{format_hz(hi)} Hz ({int(voices)} voices)"
