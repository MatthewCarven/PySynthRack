"""Chord — explode a mono pitch + gate into a four-voice poly chord.

The mono→poly *explorer*, the :mod:`arpeggiator`'s mirror twin: one
pitch CV in, four voice rows out. Feed any mono melody source
(``sequencer``, ``quantizer``, ``shift_random``…) into ``pitch_cv`` +
``gate`` and the outputs are ``(4, F)`` voice buffers — each row the
input pitch plus one interval — ready for any voice-aware chain
(oscillator ``freq_cv``, per-voice ``adsr``, ``vca``), exactly as if
four keys were held on a ``cv_keyboard``. The 16-slot voice
architecture as an instrument.

Four interval slots define the chord. A named ``preset`` fills them
from a table (``major`` = 0/4/7/12 and friends; ``5`` is the power
chord with slot 4 off); ``custom`` reads the four ``interval_n`` /
``enable_n`` params instead. A disabled slot's row stays gate-low
forever — the output is always 4 rows, so downstream per-voice state
never re-shapes when you toggle a slot live.

``strum`` staggers the gate *onsets*: on each input rising edge, row k
(counting enabled rows only) rises ``k × strum`` ms later —
sample-accurate, carried across blocks — and every row falls together
on the input's falling edge (a fall cancels not-yet-fired onsets).
Pitch is continuous: each row tracks ``in + interval`` every sample,
so glides chord along and release tails stay in tune.

``spread`` opens the voicing by one octave, alternating: slot 2 up 12,
slot 3 down 12, slots 1 and 4 unchanged — ``major`` becomes the open
(−5, 0, 12, 16) voicing instead of a fist of close thirds.

Mind the sum: four rows into a mono sink collapse to a 4× signal —
keep the downstream VCA/mixer trimmed (0.25 each is unity).

Ports:
  * ``pitch_cv`` (cv, in): mono root pitch, 1 V/oct. Unpatched → 0 V
    root (C4).
  * ``gate`` (gate, in): mono gate; all rows follow it (strummed on
    the rise). Unpatched → all rows silent.
  * ``pitch_cv`` (cv, out): ``(4, F)`` per-row pitch, 1 V/oct.
  * ``gate`` (gate, out): ``(4, F)`` per-row gates.

Params:
  * ``preset``: chord table — ``major`` ``minor`` ``7`` ``m7``
    ``maj7`` ``sus2`` ``sus4`` ``dim`` ``aug`` ``5`` ``custom``.
    Default ``major``.
  * ``interval_1`` … ``interval_4``: per-slot offset in semitones,
    −24..+24, read when ``preset`` is ``custom``. Defaults 0/4/7/12.
  * ``enable_1`` … ``enable_4``: per-slot tickbox (``custom``).
    Default all on.
  * ``strum``: per-row onset stagger, 0..200 ms. Default 0.
  * ``spread``: alternate middle slots ±1 octave. Default off.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Preset name → four interval slots in semitones; None = slot disabled.
# ``custom`` reads the interval_n / enable_n params instead. Insertion
# order is the UI combo order.
CHORD_PRESETS: dict[str, tuple[int | None, int | None, int | None, int | None]] = {
    "major": (0, 4, 7, 12),
    "minor": (0, 3, 7, 12),
    "7": (0, 4, 7, 10),
    "m7": (0, 3, 7, 10),
    "maj7": (0, 4, 7, 11),
    "sus2": (0, 2, 7, 12),
    "sus4": (0, 5, 7, 12),
    "dim": (0, 3, 6, 9),
    "aug": (0, 4, 8, 12),
    "5": (0, 7, 12, None),
    "custom": (None, None, None, None),  # placeholder; params win
}

# Per-slot octave offsets applied when ``spread`` is on: the middle
# slots alternate ±12 st, the outer two stay — major (0,4,7,12) opens
# to (0, 16, −5, 12).
CHORD_SPREAD_OFFSETS = (0, 12, -12, 0)

# The four (interval, enable) param-name pairs, in slot order. UI and
# renderer both walk these so the slot count can't drift.
CHORD_INTERVAL_KEYS = ("interval_1", "interval_2", "interval_3", "interval_4")
CHORD_ENABLE_KEYS = ("enable_1", "enable_2", "enable_3", "enable_4")


@register_module_type
class Chord(Module):
    """Mono pitch + gate → four-voice (4, F) poly chord.

    Parameters:
        preset: Chord table (``major`` … ``5``) or ``custom`` to read
            the slot params. Default ``major``.
        interval_1..4: Custom per-slot semitone offsets, −24..+24.
            Defaults 0/4/7/12.
        enable_1..4: Custom per-slot tickboxes. Default all on.
        strum: Per-row gate-onset stagger in ms, 0..200. Default 0.
        spread: Open the voicing — slots 2/3 shifted ±1 octave.
            Default False.

    Ports:
        pitch_cv (in, cv): mono root, 1 V/oct (unpatched → C4).
        gate (in, gate): mono gate for every row.
        pitch_cv (out, cv): (4, F) per-row pitch.
        gate (out, gate): (4, F) per-row gates.
    """

    TYPE = "chord"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {
        "preset": "major",
        "interval_1": 0.0,
        "interval_2": 4.0,
        "interval_3": 7.0,
        "interval_4": 12.0,
        "enable_1": True,
        "enable_2": True,
        "enable_3": True,
        "enable_4": True,
        "strum": 0.0,
        "spread": False,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("gate", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("pitch_cv", "out", "cv"),
        Port("gate", "out", "gate"),
    ]
