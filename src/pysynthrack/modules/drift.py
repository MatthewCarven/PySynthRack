"""Drift — a smooth wandering random CV.

The rack's random sources so far all have corners or a plan: the
[`lfo`](#lfo)'s ``random`` steps, [`sample_hold`](#sample_hold) steps,
[`shift_random`](#shift_random) loops, [`chaos`](#chaos) orbits a strange
attractor with perfect determinism. This one *stumbles*: a random voltage
that wanders smoothly from value to value — Buchla's fluctuating random
voltage, the Wogglebug's smooth out — for filter cutoffs that breathe,
pitches that drift like an old oscillator, pan that never sits still.

Every ``1/rate`` seconds a new target is drawn, and the output travels
to it along a half-cosine over ``glide`` of the interval, then holds.
Two ways of drawing:

  * ``smooth`` — each target is a fresh uniform draw across the whole
    range. The classic fluctuating random: it goes anywhere, at a
    walking pace.
  * ``walk`` — each target is the previous one plus a gaussian step of
    ``step`` (as a fraction of the range), reflected off the edges. A
    random walk: it goes *somewhere near*, and keeps going — the drift
    of a thing that has no home value.

``glide`` is the travel fraction: 0 is a plain sample-and-hold (stepped),
1 is one continuous curve with no flat spots, 0.3 arrives early and
waits. ``rate_cv`` bends the rate in octaves (``cv_depth`` per unit) at
block rate; patch a ``clock`` and the draws happen on its rising edges
instead, the glide sized to the measured beat.

Three outputs off the same die: ``cv`` (the smooth wander), ``stepped``
(the targets, held — the S&H twin, so a filter can drift while a
quantized pitch steps to the same values), and ``trig`` (a 1 ms pulse on
every new value — a random clock at the same pace).

All randomness comes from ``seed`` (house rule): the sequence of targets
is a pure function of the seed and the tick history, and the tick
schedule is an integer sample count, so a render is bit-identical at any
block size. Mono; a polyphonic ``rate_cv`` is averaged.

Ports:
  * ``rate_cv`` (cv in): rate in octaves per unit × ``cv_depth``.
  * ``clock`` (gate in, optional): a new value per rising edge.
  * ``cv`` (cv out): the smooth wander.
  * ``stepped`` (cv out): the held targets.
  * ``trig`` (gate out): a pulse on every new value.

Params:
  * ``mode``: ``smooth`` (fresh uniform targets) / ``walk`` (random walk).
    Default ``smooth``.
  * ``rate``: new values per second, 0.02..50 Hz. Default 0.5.
  * ``glide``: fraction of each interval spent travelling, 0..1. Default 1.
  * ``step``: walk step size as a fraction of the range, 0..1. Default 0.25.
  * ``depth``: output amplitude 0..1. Default 1.
  * ``bipolar``: ±depth (True) or 0..depth (False). Default True.
  * ``cv_depth``: octaves per unit on ``rate_cv``. Default 1.
  * ``seed``: RNG seed (non-negative int). Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

DRIFT_MODES = ("smooth", "walk")


@register_module_type
class Drift(Module):
    """Smooth random CV: targets at ``rate``, half-cosine glides between.

    Parameters:
        mode: ``smooth`` (uniform targets) or ``walk`` (gaussian random
            walk, reflected at the range). Default ``smooth``.
        rate: New values per second, 0.02..50. Default 0.5.
        glide: Fraction of each interval spent travelling to the new
            value; 0 = stepped, 1 = continuous. Default 1.0.
        step: Walk step as a fraction of the range, 0..1. Default 0.25.
        depth: Output amplitude, 0..1. Default 1.0.
        bipolar: ±depth if True, else 0..depth. Default True.
        cv_depth: Octaves per unit on ``rate_cv``. Default 1.0.
        seed: RNG seed. Default 1.

    Ports:
        rate_cv (in, cv): rate modulation in octaves (block mean).
        clock (in, gate): optional — a new value per rising edge.
        cv (out, cv): the smooth wander.
        stepped (out, cv): the held targets.
        trig (out, gate): a 1 ms pulse on every new value.
    """

    TYPE = "drift"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "mode": "smooth",
        "rate": 0.5,
        "glide": 1.0,
        "step": 0.25,
        "depth": 1.0,
        "bipolar": True,
        "cv_depth": 1.0,
        "seed": 1,
    }
    INPUT_PORTS = [
        Port("rate_cv", "in", "cv"),
        Port("clock", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("cv", "out", "cv"),
        Port("stepped", "out", "cv"),
        Port("trig", "out", "gate"),
    ]
