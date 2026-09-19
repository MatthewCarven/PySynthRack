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

`direction` picks the **order** the steps are visited in — the classic
hardware "run mode" switch: `forward` (1 2 … steps, wrap), `backward`
(steps … 2 1, wrap), `pendulum` (1 2 … steps … 2 1 2 … — the turnaround
steps are *not* repeated, so steps 5 runs an 8-note period; steps 1 is
1 1 1…, steps 2 is 1 2 1 2) and `random` (every clock draws a uniformly
random step from `seed`'s stream). `reset` rewinds to the pattern's *start
for that direction*: forward and pendulum play step 1 next (pendulum going
up), backward plays step `steps`, and random restarts its stream too — so
a reset **replays the same random phrase**, which is what makes a
"random" sequence loopable and a patch file recall its melody. Shrinking
`steps` under a step that is now past the end treats that step as the new
last one on the next clock (forward wraps to 1, backward walks to
`steps − 1`, pendulum turns around). All four orders share one pure rule,
:func:`next_step_index`, so the tests can pin the exact sequences.

`reverse` (gate in, 2026-09-20) is the run-mode switch's **live flip**:
read at each clock edge, and while high the direction is reversed *for
that step* — `forward` steps as `backward`, `backward` as `forward`, the
`pendulum` turns around from wherever it is, `random` is unaffected. A
gate that rises or falls mid-phrase just continues from the current step
the other way (no jump, no reset), so a slow square into `reverse` plays
every other bar backwards and a keyboard gate on it is a "flip it now"
button. `reset` is unchanged — it rewinds to *before the start* — and the
next edge then moves in the effective direction, so a reset with the
gate high lands on the reversed start (the last step in `forward`, step 1
in `backward`, the top of the `pendulum` heading down): a reset-plus-
reverse at the bar line plays the bar as a true mirror
(`examples/sequencer_reverse_bars.json`). Unpatched, nothing changes.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Maximum addressable steps. The param list (step{i}_pitch / step{i}_on) and
# the renderer both size off this, so the step count lives in one place.
MAX_STEPS = 16

# The run-mode switch. Order matters only for the UI combo; the default
# (``forward``) is today's behaviour, bit-exact. Named ``direction`` and
# not ``mode`` on purpose: ``mode`` is routed through a shared combo
# branch in the UI (see docs/MODULES.md, "mode combo shadowing").
SEQ_DIRECTIONS = ("forward", "backward", "pendulum", "random")

# A gentle C-major scale so a freshly-dropped sequencer plays something
# musical out of the box; steps past the 8-note scale default to C (0).
_DEFAULT_SCALE = (0, 2, 4, 5, 7, 9, 11, 12)


def _default_params() -> dict[str, object]:
    """Build the flat param dict: ``steps``, ``direction``, ``seed``, then
    per-step pitch+on pairs.

    Interleaved (pitch then on, per step) so the node UI groups each step's
    two controls together rather than listing 16 pitches then 16 toggles.
    ``direction``/``seed`` sit beside ``steps`` for the same reason: the
    three whole-pattern controls come first, the per-step rows after.
    """
    params: dict[str, object] = {"steps": 8, "direction": "forward", "seed": 1}
    for i in range(1, MAX_STEPS + 1):
        pitch = float(_DEFAULT_SCALE[i - 1]) if i <= len(_DEFAULT_SCALE) else 0.0
        params[f"step{i}_pitch"] = pitch
        params[f"step{i}_on"] = True
    return params


def _pendulum_step(idx: int, steps: int, ascending: bool) -> tuple[int, bool]:
    """The pendulum's own move: climb, turn at the top, descend, turn at
    the bottom; the turnaround steps play once. ``idx`` is a real step
    (>= 0) inside ``steps``; the heading is the PHYSICAL one."""
    if steps == 1:
        return 0, True
    if ascending:
        if idx + 1 < steps:
            return idx + 1, True
        return idx - 1, False
    if idx - 1 >= 0:
        return idx - 1, False
    return idx + 1, True


def next_step_index(idx: int, steps: int, direction: str, ascending: bool = True,
                    rng=None, reverse: bool = False) -> tuple[int, bool]:
    """The one rule for "which step plays on the next clock".

    Pure, so the renderer and the tests call the same function. ``idx`` is
    the current 0-based step index, or ``-1`` for *before the start* (a
    fresh module, or just after a ``reset``); ``ascending`` is the
    pendulum's heading; ``reverse`` is the ``reverse`` gate as read at
    this clock edge. Returns ``(next_idx, ascending)``.

    * ``forward``: ``(idx + 1) % steps`` — from the start, step 1. Leaves
      the pendulum heading *up*.
    * ``backward``: ``(idx - 1) % steps`` — from the start, the LAST step
      (so a reset in backward lands on step ``steps``). Leaves the
      pendulum heading *down* — switching to ``pendulum`` live carries on
      the way you were going.
    * ``pendulum``: climbs to the last step, turns, descends to step 1,
      turns; the turnaround steps play once (1 2 3 2 1 2 3…). Degenerate
      lengths: steps 1 is 1 1 1…, steps 2 is 1 2 1 2 — the same rule,
      nothing special-cased.
    * ``random``: one ``rng.integers(steps)`` draw per call. With
      ``steps == 1`` there is no choice to make and NO draw is consumed,
      so the stream is a pure function of the real decisions (house rule).

    **``reverse``** flips the direction *for this one edge* — a per-edge
    modifier, not a mode, so the gate can drop mid-phrase and the pattern
    simply carries on the other way from the CURRENT step (no jump, no
    reset). ``forward`` steps as ``backward`` and ``backward`` as
    ``forward``; the ``pendulum`` runs the other way: it turns around from
    wherever it is. ``random`` is unaffected (a reversed shuffle is a
    shuffle; the same draw is consumed, so the phrase is identical with
    the gate high or low). From *before the start* the reversed rule
    lands on the reversed direction's start — ``forward`` on the last
    step, ``backward`` on step 1, ``pendulum`` on the last step heading
    down (the mirror image of the unreversed run) — which is what makes
    a reset-plus-reverse at a bar line play the bar as a true mirror.

    The returned heading is always in the **base frame** — the way the
    pendulum would be heading with the gate low — so the renderer's
    state never knows about the gate: the physical heading is ``ascending
    XOR reverse`` on the way into the pendulum move and the result is
    XOR'd back on the way out. A bounce while reversed therefore flips
    the base heading too, which is exactly why dropping the gate after
    the bounce turns the pendulum around again instead of leaping.
    ``forward`` and ``backward`` set the heading as they always do (up /
    down), reversed or not.

    **Mid-run ``steps`` change.** If ``idx`` is beyond the new end it is
    first clamped to the new last step, and the rule proceeds as though
    that step had just played: forward wraps to step 1, backward walks
    to ``steps - 1``, pendulum turns around (the same index either way,
    so the heading needs no special case). An ``idx`` still inside the
    new length is untouched. Documented in MODULES.md and pinned by
    ``tests/test_sequencer.py``.
    """
    steps = max(1, int(steps))
    idx = int(idx)
    if idx >= steps:
        idx = steps - 1
    if reverse and direction != "random":
        if direction == "backward":
            return (idx + 1) % steps, False
        if direction == "pendulum":
            if idx < 0:
                # The mirror image starts at the top, heading down —
                # physically; in the base frame that is heading up.
                return steps - 1, True
            nxt, physical = _pendulum_step(idx, steps, not ascending)
            return nxt, not physical
        # forward (and unknown names) reversed: count down.
        if idx < 0:
            return steps - 1, True
        return (idx - 1) % steps, True
    if direction == "backward":
        if idx < 0:
            return steps - 1, False
        return (idx - 1) % steps, False
    if direction == "pendulum":
        if idx < 0:
            return 0, True
        return _pendulum_step(idx, steps, ascending)
    if direction == "random":
        if steps == 1:
            return 0, ascending
        if rng is None:
            raise ValueError("next_step_index('random') needs an rng")
        return int(rng.integers(steps)), ascending
    # forward (and any unknown direction name: the renderer has already
    # collapsed those onto forward, but stay safe for direct callers).
    return (idx + 1) % steps, True


@register_module_type
class Sequencer(Module):
    """Clock-driven step sequencer.

    Parameters:
        steps: Active loop length, 1..16. The sequence wraps after this many
            steps regardless of how many step params are populated.
        direction: The order the steps are visited in — one of
            :data:`SEQ_DIRECTIONS` (``forward`` | ``backward`` |
            ``pendulum`` | ``random``). See :func:`next_step_index`.
        seed: The ``random`` direction's stream (int >= 0). Changing it
            re-seeds on the spot; ``reset`` restarts the stream so the
            same phrase replays.
        step{i}_pitch: Pitch of step *i* in semitones (output as
            ``semitones / 12`` on the 1V/oct ``cv``; 0 = C4).
        step{i}_on: Whether step *i* fires its gate (``False`` = a rest —
            the step still consumes a clock tick).

    Ports: ``clock`` and ``reset`` (gate in), ``reverse`` (gate in — read
    at each clock edge; while high the step is taken in the reversed
    direction, see :func:`next_step_index`), ``cv`` and ``gate`` out.
    Gates are mono here: a ``(V, F)`` source on any of the three collapses
    to any-voice-high.

    Runtime state (backend, not serialized): the current step index, the
    pendulum heading (always in the base frame — the ``reverse`` gate is
    stateless), the random Generator (rebuilt from ``seed`` on a seed
    change or a reset), the held cv level, and the previous clock/reset
    levels for edge detection.
    """

    TYPE = "sequencer"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
        Port("reverse", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("cv", "out", "cv"),
        Port("gate", "out", "gate"),
    ]
