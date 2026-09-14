"""Arpeggiator — collapse held poly notes into a clocked mono line.

The first poly→mono *collapser*: where every voice-aware module so far
fans a mono signal out across the 16-slot voice architecture, this one
runs it in reverse. Feed it a polyphonic ``pitch_cv`` + ``gate`` pair
(``cv_keyboard`` or ``midi_input``), hold a chord, and each ``clock``
rising edge plays the next note of the chord as a mono 1 V/oct
``pitch_cv`` + ``gate`` — the classic hardware arp, patched not preset.

The held set is keyed by voice slot and stamped in arrival order, so:

  * ``up`` / ``down`` — the held notes sorted ascending / descending,
    then repeated one octave up per extra ``octaves``.
  * ``updown`` — ascending then descending without repeating the
    endpoints (C E G → C E G E C E G …).
  * ``order`` — as-played order (the arrival stamp, not the slot
    index), octave passes stacked after the first.
  * ``random`` — one seeded rng draw per clock step from the expanded
    set; deterministic per ``seed``, block-size independent.

Notes joining or leaving mid-arp rebuild the sequence on the next clock
edge (the position is kept by index, wrapped). When the last note is
released the arp falls silent, the position rewinds, and the *pitch
holds* — the release tail of whatever the arp drives stays in tune,
exactly like ``cv_keyboard``'s pitch_cv. A ``reset`` rising edge
rewinds so the next clock plays the first note of the sequence.

``hold`` latches: releasing keys keeps their notes in the set, and
pressing a key when *nothing* is physically held clears the latch and
starts a new chord — the classic performance latch. Untick to fall back
to the live keys.

The output gate holds each step for ``gate_len`` of the measured clock
period (last two edges, carried across blocks — the euclidean
convention); until an interval exists it mirrors the clock's own high
time. A mono ``pitch_cv``/``gate`` pair also works (V = 1): with
``octaves`` up it becomes a one-finger octave arp.

**Internal clock** (added 2026-09-14): leave ``clock`` unpatched and the
arp steps on its own at ``bpm`` × ``division`` steps per minute (120 × 4
= sixteenths) — one module fewer for the common case. The counter is an
integer period (no drift), and a ``reset`` rise re-phases it so the very
next sample is a step on note 1. Patch a ``clock`` and both params are
ignored — the cable wins, as it always did.

Ports:
  * ``pitch_cv`` (cv, in): poly ``(V, F)`` — or mono — pitch, 1 V/oct.
  * ``gate`` (gate, in): matching per-voice gates; a slot's note is
    read from ``pitch_cv`` at its gate's rising edge. Unpatched →
    nothing is ever held (silence).
  * ``clock`` (gate, in): advance one note per rising edge. Unpatched
    → the internal ``bpm`` / ``division`` clock runs instead.
  * ``reset`` (gate, in): rewind so the next clock plays note 1.
  * ``pitch_cv`` (cv, out): the mono line, 1 V/oct (holds between
    steps and after release).
  * ``gate`` (gate, out): high for ``gate_len`` of a step per note.

Params:
  * ``mode``: ``up`` | ``down`` | ``updown`` | ``order`` | ``random``.
    Default ``up``.
  * ``octaves``: repeat the pattern over 1..4 octaves. Default 1.
  * ``gate_len``: step-note length as a fraction of the clock period,
    0.05..0.95. Default 0.5.
  * ``hold``: latch notes after release (see above). Default off.
  * ``seed``: rng seed for ``random`` mode, one draw per step.
    Default 1.
  * ``bpm`` / ``division``: the internal clock, used only with ``clock``
    unpatched — ``bpm/60 × division`` steps per second, like the
    ``clock`` module. Defaults 120 / 4.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

ARP_MODES = ("up", "down", "updown", "order", "random")


@register_module_type
class Arpeggiator(Module):
    """Clocked poly→mono note collapser (the hardware arp, patched).

    Parameters:
        mode: Note order — ``up``, ``down``, ``updown`` (endpoints not
            repeated), ``order`` (as played), ``random`` (seeded).
            Default ``up``.
        octaves: Octave passes over the pattern, 1..4. Default 1.
        gate_len: Output gate as a fraction of the measured clock
            period, 0.05..0.95. Default 0.5.
        hold: Latch — releases keep notes; a press from silence starts
            a new chord. Default False.
        seed: RNG seed for ``random`` mode. Default 1.
        bpm: Internal clock tempo, 20..300, used when ``clock`` is
            unpatched. Default 120.
        division: Internal clock steps per beat, 0.25..16. Default 4.

    Ports:
        pitch_cv (in, cv): poly (V, F) or mono pitch, 1 V/oct.
        gate (in, gate): per-voice gates; pitch sampled at each rise.
        clock (in, gate): one note per rising edge; unpatched → the
            internal clock.
        reset (in, gate): next clock replays note 1.
        pitch_cv (out, cv): mono line (holds between steps).
        gate (out, gate): ``gate_len`` of a step per note.
    """

    TYPE = "arpeggiator"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "mode": "up",
        "octaves": 1,
        "gate_len": 0.5,
        "hold": False,
        "seed": 1,
        "bpm": 120.0,
        "division": 4.0,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("gate", "in", "gate"),
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("pitch_cv", "out", "cv"),
        Port("gate", "out", "gate"),
    ]
