"""PossibilitySelector — a router whose routes can be undecided.

The second brick of the possibility bridge, and the *meta* one.
[`possibility_seq`](#possibility_seq) holds a register of bits with holes in
it: each step is a hit, a rest, or ``?`` — *whether* something fires. This
module holds a register of **routes** with holes in it: each step sends the
incoming gate to ``out1``, ``out2``, ``out3`` or ``out4`` — *which* module
fires — or is ``?``, undecided among them until the music needs an answer.
Patch a clock into ``in``, the four outputs into a kick, a snare, a hat and
a clap, and every tick fires *some* drum; which one is the undecided bit.
The router itself collapses.

Each step's state is the set of outputs it may go to:

* ``"1"`` … ``"4"`` — decided: every take, that output.
* ``"?"`` — undecided among all four (the click cycle's last stop).
* a subset, written as its digits — ``"13"`` is *kick or hat, never snare*;
  ``"234"`` is *anything but the kick*. The right-click popup on the panel
  writes these.
* ``"0"`` — a rest: nothing passes.

A pattern with k undecided steps holds up to 4^k distinct bars (exactly the
product of each open step's choices); the panel counts them.

``mode`` says *when* the open steps decide, exactly as in
[`possibility_seq`](#possibility_seq): ``loop`` re-resolves every ``?`` each
time the pattern wraps; ``latch`` resolves once and holds until a ``reroll``
edge (or a ``seed`` change); ``dice`` rolls fresh every time round.

``weight1`` … ``weight4`` say **how the open steps lean**: an undecided step
draws among its candidates in proportion to their weights, so ``weight4``
= 0.25 makes the clap a rare guest and ``weight2`` = 0 takes the snare out of
every ``?`` without touching a decided ``"2"``. All 1.0 is a fair draw.

``balanced`` is the source project's measured fix for clumping, generalised
from a coin to a hand of cards: a *fair* open step (its candidates equally
weighted) is dealt from a shuffle-bag — the output dealt least so far among
its candidates, ties broken by the die — so a bar of fair ``?``s spreads its
hits across the outputs instead of landing three kicks in a row. Weighted
steps keep their lean. In PythonBinaryPossibility this least-used draw is
called *the selector*, which is where the module's name comes from.

Two stepping contracts, so one cable is enough either way:

* ``clock`` patched — the sequencer reading: step *k*'s route applies to
  clock tick *k*, and ``in`` is what passes through it (a
  [`possibility_seq`](#possibility_seq) ``gate`` into ``in`` makes a
  *whether* × *which* kit: the sequencer decides if the sixteenth hits,
  the selector decides where it goes).
* ``clock`` unpatched — the distributor reading: ``in``'s own rising edges
  step the register, so route *k* applies to the *k*-th hit, whenever it
  arrives. Four plucks tuned to a chord on the four outputs and a sparse
  gate into ``in`` is a harp whose string order is a register you can
  leave holes in.
* ``in`` unpatched — the clock itself is routed.

All randomness comes from ``seed`` (house rule): the sequence of takes is a
pure function of the seed and the edge history, block-size independent.
``reroll`` draws a fresh take; ``reset`` rewinds to step 1 without touching
the current take.

Ports:
  * ``in`` (gate in): the gate to route.
  * ``clock`` (gate in): advance one step per rising edge; unpatched, ``in``
    steps the register itself.
  * ``reset`` (gate in): rising edge rewinds so the next step is step 1.
  * ``reroll`` (gate in): rising edge draws a fresh take (every open step).
  * ``out1`` … ``out4`` (gate out): high while ``in`` is high and the
    current step routes there — the [`sequencer`](#sequencer) contract,
    so pulse width follows the input's.

Params:
  * ``steps``: loop length, 1..16. Default 16.
  * ``mode``: ``loop`` / ``latch`` / ``dice``. Default ``loop``.
  * ``balanced``: deal fair open steps from the bag. Default off.
  * ``seed``: RNG seed (non-negative int). Default 1.
  * ``weight1`` … ``weight4``: how the open steps lean, 0..1. Default 1.0.
  * ``step{i}_state``: ``"0"``, ``"1"``–``"4"``, ``"?"`` or a digit subset.

Ancestry: PythonBinaryPossibility's ``PsynthRack`` register, one level up —
a symbol per step instead of a bit, and the least-used-value *selector*
from its ``RandomGeneratorPerfect`` as the balanced deal. Self-contained by
design so ``core``/``modules`` stay dependency-free.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port
from .possibility_seq import POSSIBILITY_MODES

#: Maximum addressable steps — the sequencer family's ceiling.
MAX_STEPS = 16

#: Number of routed outputs (``out1`` … ``out4``).
N_OUTS = 4

#: The click cycle on the panel: rest, each output in turn, then open.
ROUTE_STATES = ("0", "1", "2", "3", "4", "?")

#: Out of the box: kick on the downbeats, snare on the backbeats, hat on the
#: off-eighths, and every other sixteenth open to any of the four —
#: 4^8 = 65,536 possible bars before you touch anything.
_DEFAULT_ROUTES = "1?3?2?3?1?3?2?3?"


def _default_params() -> dict[str, object]:
    params: dict[str, object] = {
        "steps": 16,
        "mode": "loop",
        "balanced": False,
        "seed": 1,
    }
    for k in range(1, N_OUTS + 1):
        params[f"weight{k}"] = 1.0
    for i in range(1, MAX_STEPS + 1):
        params[f"step{i}_state"] = _DEFAULT_ROUTES[i - 1]
    return params


def parse_route(state) -> tuple[int, ...]:
    """The candidate outputs of one step, 1-based and sorted.

    ``"?"`` is every output; a digit string is that subset (``"31"`` and
    ``"13"`` are the same step); ``"0"``, ``""`` and junk are a rest. A
    decided step is a singleton; an undecided one has two or more.
    """
    s = str(state).strip()
    if s == "?":
        return tuple(range(1, N_OUTS + 1))
    return tuple(sorted({int(ch) for ch in s if ch in "1234"}))


def format_route(candidates) -> str:
    """The canonical state string for a candidate set: ``"0"``, ``"3"``,
    ``"13"``, or ``"?"`` for the full set. The panel's popup writes this so
    the patch file always reads the same way for the same step."""
    cands = sorted({int(k) for k in candidates if 1 <= int(k) <= N_OUTS})
    if not cands:
        return "0"
    if len(cands) == N_OUTS:
        return "?"
    return "".join(str(k) for k in cands)


# ----- panel helpers (dpg-free, so the UI and the tests share one truth) -----

def next_state(state: str) -> str:
    """The panel's one gesture: click a step to cycle
    ``0 -> 1 -> 2 -> 3 -> 4 -> ? -> 0``.

    Any undecided subset sits in the ``?`` slot of the cycle (the next
    click is a rest), and anything unrecognised lands on ``"0"`` so a
    hand-edited patch file with a junk state still cycles sensibly.
    """
    cands = parse_route(state)
    if not cands:
        return "1"
    if len(cands) == 1:
        return str(cands[0] + 1) if cands[0] < N_OUTS else "?"
    return "0"


def undecided_count(states, steps: int) -> int:
    """How many open steps (two or more candidates) lie inside the loop."""
    steps = max(0, min(MAX_STEPS, int(steps)))
    return sum(1 for s in list(states)[:steps] if len(parse_route(s)) >= 2)


def possibility_count(states, steps: int) -> int:
    """How many distinct bars the pattern holds: the product over the
    active steps of each step's number of choices (a rest or a decided
    step is one). Sixteen full ``?``s hold 4^16."""
    steps = max(0, min(MAX_STEPS, int(steps)))
    n = 1
    for s in list(states)[:steps]:
        n *= max(1, len(parse_route(s)))
    return n


def format_possibilities(states, steps: int) -> str:
    """The panel's possibility readout, e.g. ``"8 ? -> 65,536 possible bars"``."""
    k = undecided_count(states, steps)
    if k == 0:
        return "no ? -> 1 bar, decided"
    return f"{k} ? -> {possibility_count(states, steps):,} possible bars"


def resolve_route(state, weights, balanced: bool, bag: list[int],
                  rand) -> int:
    """Resolve one step to an output (1..4) or a rest (0), mutating ``bag``
    (per-output deal counts) for balanced deals.

    A rest and a decided step need no draw. An open step draws among its
    candidates in proportion to ``weights`` (one uniform, walked through
    the cumulative weights); if every candidate's weight is zero the draw
    is uniform. Under ``balanced`` a *fair* open step — all its candidates
    equally weighted — is dealt instead: uniformly among the candidates
    dealt *least* so far, PythonBinaryPossibility's least-used-value
    selector, width 1. ``rand`` is consumed only when a real choice exists,
    so the take is a pure function of the rng stream.
    """
    cands = parse_route(state)
    if not cands:
        return 0
    if len(cands) == 1:
        return cands[0]
    w = [max(0.0, float(weights[k - 1])) for k in cands]
    total = sum(w)
    if total <= 0.0:
        w = [1.0] * len(cands)
        total = float(len(cands))
    fair = all(x == w[0] for x in w)
    if balanced and fair:
        lo = min(bag[k - 1] for k in cands)
        eligible = [k for k in cands if bag[k - 1] == lo]
        pick = eligible[0] if len(eligible) == 1 else (
            eligible[min(len(eligible) - 1, int(rand() * len(eligible)))])
        bag[pick - 1] += 1
        return pick
    u = rand() * total
    acc = 0.0
    for k, wk in zip(cands, w):
        acc += wk
        if u < acc:
            return k
    return cands[-1]


def collapse_routes(states, weights, balanced: bool, rand) -> tuple[int, ...]:
    """Resolve a whole take in step order — the pure reference the renderer
    is tested against. A fresh bag per call, exactly one bar's deal."""
    bag = [0] * N_OUTS
    return tuple(
        resolve_route(state, weights, balanced, bag, rand) for state in states
    )


@register_module_type
class PossibilitySelector(Module):
    """Clocked 1-to-4 gate router whose routes can be undecided (``?``).

    Parameters:
        steps: Active loop length, 1..16. Default 16.
        mode: When the open steps decide — ``loop`` (fresh take per pass),
            ``latch`` (one take, held until reroll), ``dice`` (every open
            step rolls fresh each time around). Default ``loop``.
        balanced: Deal fair open steps from a shuffle-bag — the output
            used least so far — so a bar spreads across the outputs.
            Default False.
        seed: RNG seed; the sequence of takes is deterministic per seed.
            Default 1.
        weight{k}: How the open steps lean towards ``out{k}``, 0..1.
            Default 1.0 (all equal = a fair draw).
        step{i}_state: ``"0"`` (rest), ``"1"``–``"4"`` (that output), ``"?"``
            (any output) or a digit subset such as ``"13"``.

    Ports:
        in (in, gate): the gate to route.
        clock (in, gate): advance one step per rising edge; unpatched, the
            input's own edges step the register.
        reset (in, gate): rising edge rewinds to step 1 (take kept).
        reroll (in, gate): rising edge draws a fresh take.
        out1..out4 (out, gate): high while ``in`` is high and the current
            step routes there.
    """

    TYPE = "possibility_selector"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [
        Port("in", "in", "gate"),
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
        Port("reroll", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port(f"out{k}", "out", "gate") for k in range(1, N_OUTS + 1)
    ]

