"""Clockwork — euclidean, burst, bernoulli_gate and clock_divider.

Three small gate processors that make the clock → sequencer chain
*surprise* you: together with the drum voices they turn the rack into a
groovebox that plays itself. All clocked modules share the house
clock-edge semantics (rising edge > 0.5, previous state carried across
blocks) and all randomness takes a ``seed`` (deterministic — patches
recall their character, renders are testable).

``euclidean`` — the rhythm generator: distributes ``fills`` hits as
evenly as possible across ``steps`` clock ticks using the arithmetic
Bjorklund pattern ``hit[i] = ((i + rotate)·fills) mod steps < fills`` —
no recursion, and E(3,8) lands the tresillo verbatim. ``rotate`` walks
the pattern; ``reset`` realigns so the next clock plays step 1. The
``gate`` output holds each hit for ``gate_len`` of a step (the step
length is measured from the incoming clock; until two edges have been
seen it simply mirrors the clock's high time). ``accent`` is a second,
sparser Euclidean layer (``accent_fills``) intersected with the main
pattern, so accents always land on hits — patch it to a second envelope
or a VCA boost.

``burst`` — the ratchet generator: one ``trigger`` edge fires ``count``
gates. Free-running, they span ``count / rate`` seconds with ``spread``
warping the grid (negative = accelerando, positive = ritardando, 0 =
even); with ``clock`` patched the gates land on every ``division``-th
clock edge instead. ``env`` steps down ``(1 − decay)^k`` per gate while
its gate is high — patch it straight into a VCA for decaying ratchets.
A retrigger mid-burst restarts the burst (documented hardware
behaviour).

``bernoulli_gate`` — the probability switch: each incoming gate is
routed *whole* (decision latched on the rising edge, length preserved)
to ``out_a`` with probability ``p`` or ``out_b`` otherwise, where ``p``
is ``probability`` plus the ``p_cv`` value at the edge, clamped 0..1.
``mode`` ``independent`` = fresh coin per gate; ``toggle`` = the coin
decides whether to *switch* outputs (p = 1 alternates A/B/A/B). Seeded:
one rng draw per gate, block-size independent by construction.

``clock_divider`` (added 2026-09-11) — the family's fourth member: one
clock in, ``div2`` / ``div4`` / ``div8`` out, a ``divn`` you set (``n``
1..32) with **swing** (every second ``divn`` gate is delayed by ``swing``
of its period — 0.33 is the triplet feel), and a ``mult`` that emits
``m`` gates per incoming period from the last measured interval (so it
is approximate for exactly one period after a tempo change). Every
output fires together on the first edge after ``reset`` — the downbeat —
and ``pw`` sets each gate's width as a fraction of *that output's*
period. Until an interval has been measured, gates simply mirror the
clock's own high time.

**Steady gates on a swung clock (2026-09-22).** A [`clock`](#clock) with
``swing`` on it hands the divider intervals that alternate long/short,
and the gate lengths used to follow the *last* one: ``divn`` on an odd
``n`` flapped 2:1 and ``div2``/``div4``/``div8``, which always land on
the even pulses and so always measure the SHORT interval, sat ~30% under
their true period. The lengths now come from the mean of the last TWO
intervals — the straight period of a swung clock exactly, and the last
interval of a steady one exactly, so nothing moves on a steady clock.
Only falling edges move: every rising edge, including ``divn``'s swing
offset, still comes off the real clock edges and the last real interval.
``mult`` keeps the real interval — subdividing the period that actually
happened is its job.

**A gate never merges into the next one (2026-09-24).** At high ``pw``
on a swung clock (or with ``divn``'s own swing) a gate could still be
high when the next one of the same output was due, and the two merged
into one note — 15 of 32 ``divn`` gates at ``pw`` 0.9 on a 0.3-swung
8 Hz clock. Every gate's length is now capped, when it starts, to end
at least one sample before the output's predicted next gate (the swing
pattern is long/short, so the prediction is exact for a straight or
swung clock); when a tempo change or a reset makes the prediction wrong,
the late gate starts one sample late instead of merging. Where nothing
collided, nothing moves.

**CV on the counts (2026-09-11):** ``euclidean.fills_cv`` and
``burst.count_cv`` move ``fills`` / ``count`` by ``*_cv_depth`` per unit
(default 8 — 0..1 V sweeps eight), read at the clock / trigger edge and
rounded, so a slow LFO into ``fills_cv`` breathes a pattern from sparse
to dense and a sequencer into ``count_cv`` writes the ratchet count per
step. Unpatched, nothing changes.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

BERNOULLI_MODES = ("independent", "toggle")

EUCLIDEAN_MAX_STEPS = 32


def euclidean_pattern(steps: int, fills: int, rotate: int = 0) -> tuple[bool, ...]:
    """The arithmetic Bjorklund pattern, hit[i] for i in 0..steps−1.

    ``hit[i] = ((i + rotate)·fills) mod steps < fills`` — an even
    distribution equivalent (up to rotation) to the recursive Bjorklund
    algorithm; E(3,8) at rotate 0 is the tresillo ``10010010`` exactly.
    Pure; shared by the renderer and the tests.
    """
    steps = max(1, min(EUCLIDEAN_MAX_STEPS, int(steps)))
    fills = max(0, min(steps, int(fills)))
    if fills == 0:
        return tuple([False] * steps)
    return tuple(
        ((i + int(rotate)) * fills) % steps < fills for i in range(steps)
    )


@register_module_type
class Euclidean(Module):
    """Euclidean rhythm gate: ``fills`` hits spread evenly over ``steps``.

    Parameters:
        steps: Loop length in clock ticks, 1..32. Default 16.
        fills: Hits distributed across the loop, 0..steps. Default 4.
        rotate: Pattern rotation, 0..steps−1. Default 0.
        accent_fills: Second sparser layer for the ``accent`` jack
            (intersected with the main pattern). Default 0.
        gate_len: Hit length as a fraction of one step, 0.05..1.
            Default 0.5.
        fills_cv_depth: Fills per unit of ``fills_cv``. Default 8.

    Ports:
        clock (in, gate): advance one step per rising edge.
        reset (in, gate): rising edge realigns to step 1.
        fills_cv (in, cv): moves ``fills`` by ``fills_cv_depth`` per unit,
            read at each clock edge and rounded.
        gate (out, gate): the pattern.
        accent (out, gate): the accent layer (subset of hits).
    """

    TYPE = "euclidean"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "steps": 16,
        "fills": 4,
        "rotate": 0,
        "accent_fills": 0,
        "gate_len": 0.5,
        "fills_cv_depth": 8.0,
    }
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
        Port("fills_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("gate", "out", "gate"),
        Port("accent", "out", "gate"),
    ]


@register_module_type
class Burst(Module):
    """Ratchet generator: one trigger → ``count`` gates + a taper CV.

    Parameters:
        count: Gates per burst, 1..16. Default 3.
        rate: Free-running gate rate in Hz (the burst spans
            ``count/rate`` seconds), 0.5..50. Ignored when ``clock`` is
            patched. Default 8.
        division: Clocked mode — fire on every Nth clock edge, 1..8.
            Default 1.
        decay: Per-gate amplitude taper on ``env``: gate k rides
            ``(1 − decay)^k``. 0 = flat. Default 0.3.
        spread: Grid warp, −1 (accelerando) .. +1 (ritardando).
            Default 0.
        count_cv_depth: Gates per unit of ``count_cv``. Default 8.

    Ports:
        trigger (in, gate): start (or restart) a burst per rising edge.
        clock (in, gate): optional — gates land on clock edges instead.
        count_cv (in, cv): moves ``count`` by ``count_cv_depth`` per unit,
            read at the trigger edge and rounded.
        gate (out, gate): the ratchet gates.
        env (out, cv): ``(1−decay)^k`` while gate k is high, else 0.
    """

    TYPE = "burst"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "count": 3,
        "rate": 8.0,
        "division": 1,
        "decay": 0.3,
        "spread": 0.0,
        "count_cv_depth": 8.0,
    }
    INPUT_PORTS = [
        Port("trigger", "in", "gate"),
        Port("clock", "in", "gate"),
        Port("count_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("gate", "out", "gate"),
        Port("env", "out", "cv"),
    ]


@register_module_type
class BernoulliGate(Module):
    """Probability gate router: each gate goes whole to A or B.

    Parameters:
        probability: Chance of ``out_a``, 0..1 (plus ``p_cv`` at the
            edge, clamped). Default 0.5.
        mode: ``independent`` (fresh coin per gate) or ``toggle`` (the
            coin decides whether to switch outputs). Default
            ``independent``.
        seed: RNG seed; one draw per incoming gate. Default 1.

    Ports:
        in (in, gate): gates to route; length preserved.
        p_cv (in, cv): added to ``probability`` at each edge.
        out_a (out, gate): heads.
        out_b (out, gate): tails.
    """

    TYPE = "bernoulli_gate"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "probability": 0.5,
        "mode": "independent",
        "seed": 1,
    }
    INPUT_PORTS = [
        Port("in", "in", "gate"),
        Port("p_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out_a", "out", "gate"),
        Port("out_b", "out", "gate"),
    ]


DIVIDER_MAX_N = 32
DIVIDER_MAX_M = 4
DIVIDER_MAX_SWING = 0.75


@register_module_type
class ClockDivider(Module):
    """Clock divider / multiplier with swing on the custom division.

    Parameters:
        n: Custom division for ``divn``, 1..32. Default 3.
        m: Multiplier for ``mult`` (gates per incoming period), 2..4.
            Default 2.
        swing: Every second ``divn`` gate is delayed by this fraction of
            the ``divn`` period, 0..0.75. Default 0 (straight); 0.33 is
            the triplet feel.
        pw: Gate width as a fraction of each output's own period,
            0.05..0.95. Default 0.5. The period is the MEAN of the last
            two measured input intervals, so a swung clock's alternating
            intervals do not flutter the gate lengths (``mult`` keeps
            the real interval). A gate is capped to end at least one
            sample before the next gate of the same output, so a high
            ``pw`` on a swung clock never merges two notes into one.

    Ports:
        clock (in, gate): the clock to divide.
        reset (in, gate): rising edge — the next clock edge is the
            downbeat and every output fires on it.
        div2 / div4 / div8 (out, gate): fixed divisions.
        divn (out, gate): ``n``-division, with ``swing``.
        mult (out, gate): ``m`` gates per incoming period.
    """

    TYPE = "clock_divider"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "n": 3,
        "m": 2,
        "swing": 0.0,
        "pw": 0.5,
    }
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("div2", "out", "gate"),
        Port("div4", "out", "gate"),
        Port("div8", "out", "gate"),
        Port("divn", "out", "gate"),
        Port("mult", "out", "gate"),
    ]
