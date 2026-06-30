"""Sequencer module — a clock-driven step sequencer (pitch CV + gate).

The self-playing centrepiece. Drive its `clock` input from a
[`clock`](#clock) (or any gate — an LFO→[`schmitt`](#schmitt) works too) and
it walks through up to 16 steps, one step per clock pulse, emitting the
current step's pitch as a **1V/octave** control voltage on `cv` and a `gate`
that pulses on each *enabled* step. Wire `cv` into an [`oscillator`](#oscillator)
`freq_cv` (set the osc's base `freq` to C4 = 261.6256 Hz to play in tune) and
`gate` into an [`adsr`](#adsr) → [`vca`](#vca) and the patch plays a melody by
itself.

Each step has a **pitch** in semitones (output as `semitones / 12` so it
lands on the 1V/oct convention, C4 = 0 V) and an **on/off** toggle — turning
a step off makes it a rest (the step still occupies a clock tick, but its
gate stays low). `steps` sets the loop length; the sequence wraps back to
step 1 after the last active step. An optional `reset` gate input snaps the
sequence back to the start on its rising edge (next clock plays step 1).

The current pitch is held on `cv` for the whole step (sample-and-hold style),
so a note stays in tune while its envelope rings on after the gate falls.
No internal tempo — that's the [`clock`](#clock)'s job, kept separate so one
clock can drive several sequencers (and other modules) in lockstep.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Maximum addressable steps. The param list (step{i}_pitch / step{i}_on) and
# the renderer both size off this, so the step count lives in one place.
MAX_STEPS = 16

# A gentle C-major scale so a freshly-dropped sequencer plays something
# musical out of the box; steps past the 8-note scale default to C (0).
_DEFAULT_SCALE = (0, 2, 4, 5, 7, 9, 11, 12)


def _default_params() -> dict[str, object]:
    """Build the flat param dict: ``steps`` then per-step pitch+on pairs.

    Interleaved (pitch then on, per step) so the node UI groups each step's
    two controls together rather than listing 16 pitches then 16 toggles.
    """
    params: dict[str, object] = {"steps": 8}
    for i in range(1, MAX_STEPS + 1):
        pitch = float(_DEFAULT_SCALE[i - 1]) if i <= len(_DEFAULT_SCALE) else 0.0
        params[f"step{i}_pitch"] = pitch
        params[f"step{i}_on"] = True
    return params


@register_module_type
class Sequencer(Module):
    """Clock-driven step sequencer.

    Parameters:
        steps: Active loop length, 1..16. The sequence wraps after this many
            steps regardless of how many step params are populated.
        step{i}_pitch: Pitch of step *i* in semitones (output as
            ``semitones / 12`` on the 1V/oct ``cv``; 0 = C4).
        step{i}_on: Whether step *i* fires its gate (``False`` = a rest —
            the step still consumes a clock tick).

    Runtime state (backend, not serialized): the current step index, the
    held cv level, and the previous clock/reset levels for edge detection.
    """

    TYPE = "sequencer"
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("cv", "out", "cv"),
        Port("gate", "out", "gate"),
    ]
