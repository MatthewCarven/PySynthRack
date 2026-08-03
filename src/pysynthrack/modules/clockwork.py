"""Clockwork trio — euclidean, burst and bernoulli_gate.

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

    Ports:
        clock (in, gate): advance one step per rising edge.
        reset (in, gate): rising edge realigns to step 1.
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
    }
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
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

    Ports:
        trigger (in, gate): start (or restart) a burst per rising edge.
        clock (in, gate): optional — gates land on clock edges instead.
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
    }
    INPUT_PORTS = [
        Port("trigger", "in", "gate"),
        Port("clock", "in", "gate"),
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
