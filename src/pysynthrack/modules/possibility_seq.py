"""PossibilitySeq — a step sequencer whose steps can be undecided.

The bridge module from PythonBinaryPossibility (Matthew's possibility-space
toy): each of up to 16 steps is ``"0"`` (a rest), ``"1"`` (a hit), or
``"?"`` — *undecided*, both at once, resolved only when the music needs it.
A pattern with k undecided steps holds 2^k distinct bars; this module plays
one of them per pass and can pick a fresh one whenever you ask. Drive
``clock`` from a [`clock`](#clock) and wire ``gate`` into a
[`kick_drum`](#kick_drum) (or any gate consumer) and the patch drums a
pattern that is genuinely not decided yet.

``mode`` says *when* the undecided steps decide:

* ``loop`` (default) — a fresh take every pass: each time the pattern wraps
  back to step 1, every ``?`` re-resolves. The bar you just heard existed;
  the next one is drawn from the same possibility space.
* ``latch`` — one take, held: the ``?``s resolve on first play and stay
  resolved until a ``reroll`` edge (or a ``seed`` change) asks for a new
  take. The pattern is a bar you *found* and get to keep.
* ``dice`` — no memory at all: every ``?`` rolls fresh each time it comes
  around. The loosest setting, and the closest relative of
  [`bernoulli_gate`](#bernoulli_gate). (Under ``balanced`` the bag then
  deals through *time* — consecutive fair rolls pair up.)

Each ``?`` fires with its own ``step{i}_p`` odds — 0.5 is a fair coin, 0.2
a ghost note that turns up in a fifth of takes. And because independent
coins clump (measured in the source project: one 16-step bar in fifteen
comes out audibly lopsided), ``balanced`` deals the *fair* ``?``s from a
shuffle-bag instead — every pair of fair steps gets exactly one hit, so a
bar always lands on its share, the same reason Tetris deals pieces from a
bag. Weighted steps keep their own odds either way (a ghost note's whole
point is its rarity and its clumping).

All randomness comes from ``seed`` (house rule): the sequence of takes is a
pure function of the seed and the clock, patches reload with the character
they were saved with, and renders are deterministic. ``reroll`` is the
performance handle — patch a slow clock into it and the pattern re-decides
itself every few bars; ``reset`` rewinds to step 1 without disturbing the
current take.

Ports:
  * ``clock`` (gate in): advance one step per rising edge.
  * ``reset`` (gate in): rising edge rewinds so the next clock plays step 1.
  * ``reroll`` (gate in): rising edge draws a fresh take (all ``?``s).
  * ``gate`` (gate out): high while the clock is high and the current step
    fires — the [`sequencer`](#sequencer) convention, so pulse width follows
    the clock's.

Params:
  * ``steps``: loop length, 1..16. Default 16.
  * ``mode``: ``loop`` / ``latch`` / ``dice``. Default ``loop``.
  * ``balanced``: deal fair ``?``s from the bag instead of flipping coins.
    Default off (the coin — some grooves want the clumping).
  * ``seed``: RNG seed (non-negative int); deterministic per seed. Default 1.
  * ``step{i}_state``: ``"0"`` / ``"1"`` / ``"?"`` per step.
  * ``step{i}_p``: odds a ``?`` fires, 0..1. Default 0.5 (fair).

Ancestry: PythonBinaryPossibility's ``PsynthRack`` (a register of possibility
bits with a synth voice attached) — this module is its step semantics ported
to the rack, self-contained by design so ``core``/``modules`` stay
dependency-free. The collapse-and-deal logic below mirrors
``PsynthRack.collapse(balanced=...)`` behaviour exactly.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

#: Maximum addressable steps — mirrors the Sequencer's ceiling so the two
#: modules feel like siblings on the panel.
MAX_STEPS = 16

#: Legal per-step states.
STEP_STATES = ("0", "1", "?")

POSSIBILITY_MODES = ("loop", "latch", "dice")

#: The out-of-the-box pattern: a driving alternation with four undecided
#: steps — 16 possible bars before you touch anything.
_DEFAULT_PATTERN = "1?101?101?101?10"


def _default_params() -> dict[str, object]:
    params: dict[str, object] = {
        "steps": 16,
        "mode": "loop",
        "balanced": False,
        "seed": 1,
    }
    for i in range(1, MAX_STEPS + 1):
        params[f"step{i}_state"] = _DEFAULT_PATTERN[i - 1]
        params[f"step{i}_p"] = 0.5
    return params


def resolve_step(state: str, p: float, balanced: bool, bag: list[int],
                 rand) -> bool:
    """Resolve one step to fire/rest, mutating ``bag`` for balanced deals.

    ``"1"``/``"0"`` are already decided. A ``"?"`` fires with odds ``p`` —
    unless ``balanced`` and the step is fair (p == 0.5), in which case it is
    dealt from ``bag`` (counts of rests and hits dealt so far): draw
    uniformly among whichever outcomes have been dealt *least*, so the
    spread never exceeds one — a width-1, order-1 shuffle-bag, the exact
    semantics of PythonBinaryPossibility's balanced collapse. ``rand`` is a
    callable returning uniforms in [0, 1); it is consumed only when a real
    choice exists, so the take is a pure function of the rng stream.
    """
    if state == "1":
        return True
    if state == "0":
        return False
    if balanced and p == 0.5:
        lo = min(bag)
        eligible = [v for v in (0, 1) if bag[v] == lo]
        value = eligible[0] if len(eligible) == 1 else (
            1 if rand() < 0.5 else 0)
        bag[value] += 1
        return bool(value)
    return bool(rand() < p)


# ----- panel helpers (dpg-free, so the UI and the tests share one truth) -----

def next_state(state: str) -> str:
    """The panel's one gesture: click a step to cycle ``0 -> 1 -> ? -> 0``.

    Anything unrecognised lands on ``"0"``, so a hand-edited patch file with
    a junk state still cycles sensibly instead of getting stuck.
    """
    try:
        return STEP_STATES[(STEP_STATES.index(state) + 1) % len(STEP_STATES)]
    except ValueError:
        return STEP_STATES[0]


def undecided_count(states, steps: int) -> int:
    """How many ``?`` steps lie inside the active loop length.

    Steps past ``steps`` are parked, not played, so they do not widen the
    possibility space — the panel greys them for the same reason.
    """
    steps = max(0, min(MAX_STEPS, int(steps)))
    return sum(1 for s in list(states)[:steps] if s == "?")


def possibility_count(states, steps: int) -> int:
    """How many distinct bars the pattern currently holds: ``2 ** k``.

    The headline number the panel prints. A fully decided pattern holds
    exactly one bar (itself); sixteen ``?``s hold 65,536.
    """
    return 1 << undecided_count(states, steps)


def format_possibilities(states, steps: int) -> str:
    """The panel's possibility readout, e.g. ``"4 ? -> 16 possible bars"``."""
    k = undecided_count(states, steps)
    if k == 0:
        return "no ? -> 1 bar, decided"
    n = 1 << k
    return f"{k} ? -> {n:,} possible bars"


def collapse_pattern(states, odds, balanced: bool, rand) -> tuple[bool, ...]:
    """Resolve a whole take in step order — the pure reference the renderer
    is tested against. A fresh bag per call, exactly one bar's deal."""
    bag = [0, 0]
    return tuple(
        resolve_step(state, p, balanced, bag, rand)
        for state, p in zip(states, odds)
    )


@register_module_type
class PossibilitySeq(Module):
    """Clock-driven step sequencer with undecided (``?``) steps.

    Parameters:
        steps: Active loop length, 1..16. Default 16.
        mode: When the ``?``s decide — ``loop`` (fresh take per pass),
            ``latch`` (one take, held until reroll), ``dice`` (every ``?``
            rolls fresh each time around). Default ``loop``.
        balanced: Deal fair ``?``s from a shuffle-bag so every bar lands on
            its share (weighted steps keep their own odds). Default False.
        seed: RNG seed; the sequence of takes is deterministic per seed.
            Default 1.
        step{i}_state: ``"0"`` (rest), ``"1"`` (hit) or ``"?"`` (undecided).
        step{i}_p: Odds a ``?`` step fires, 0..1. Default 0.5.

    Ports:
        clock (in, gate): advance one step per rising edge.
        reset (in, gate): rising edge rewinds to step 1 (take kept).
        reroll (in, gate): rising edge draws a fresh take.
        gate (out, gate): high while clock is high and the step fires.
    """

    TYPE = "possibility_seq"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
        Port("reroll", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("gate", "out", "gate"),
    ]
