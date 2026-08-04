"""Chaos — a strange-attractor CV source (Lorenz / Rössler).

Deterministic wandering with *structure* — the corner none of the other
random sources cover: ``shift_random`` loops, the LFO's ``random``
steps, chaos **orbits**. The same shape never repeats, yet the module
is fully deterministic: same ``seed``, same render, forever (the house
randomness rule satisfied by containing no randomness at all — the only
rng use is the seeded initial-condition jitter).

Three CV outs — ``x``, ``y``, ``z`` — are three views of **one orbit**,
so one module modulates three destinations *coherently* (cutoff swells
arrive with the pitch wander that caused them). The ``gate`` out is the
attractor's own rhythm, and it differs by system:

  * ``lorenz`` — the sign of x: a quasi-random square that hangs on
    each wing of the butterfly for an unpredictable number of orbits.
  * ``rossler`` — z above threshold: z hugs the floor for most of each
    orbit then kicks, giving sparse, irregular trigger bursts.

Integration is classic RK4 at a rail-limited internal substep
(numerical stability rails per system), evaluated every 16 samples and
linearly interpolated to audio rate — CV-smooth, cheap, and **exactly
block-size independent** (the control grid is keyed to the absolute
sample count, and the integrator/interp state carries across blocks).
``rate`` scales attractor time and is calibrated so ~1.0 ≈ one orbit
per second (per-system calibration constants; approximate by nature).
Outputs are normalised by the attractor's known bounds, clipped at the
rails, then mapped by ``range``/``bipolar`` (the ``shift_random``
idiom). A non-finite state (should never happen inside the rails)
quietly re-seeds.

The demo that sells it: patch ``x`` and ``y`` into a ``scope`` in xy
mode — the butterfly, live on the node.

Ports:
  * ``reset`` (gate, in): rising edge returns the orbit to its seeded
    starting point (sample-accurate — a reset mid-block splits the
    block).
  * ``x``, ``y``, ``z`` (cv, out): the orbit, normalised and scaled.
  * ``gate`` (gate, out): the attractor's rhythm (see above).

Params:
  * ``system``: ``lorenz`` | ``rossler``. Default ``lorenz``.
  * ``rate``: speed, 0.01..50 (~orbits per second). Default 1.
  * ``range``: output span in volts, 0..5. Default 2.
  * ``bipolar``: False → 0..range, True → ±range. Default False.
  * ``seed``: initial-condition seed (≥ 0). Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

CHAOS_SYSTEMS = ("lorenz", "rossler")


@register_module_type
class Chaos(Module):
    """Lorenz / Rössler attractor CV source (see the module docstring).

    Parameters:
        system: lorenz | rossler. Default lorenz.
        rate: Speed, 0.01..50 (~orbits/second). Default 1.
        range: Output span in volts, 0..5. Default 2.
        bipolar: False → 0..range, True → ±range. Default False.
        seed: Initial-condition seed. Default 1.

    Ports:
        reset (in, gate): rising edge restarts the orbit from the seed.
        x, y, z (out, cv): the orbit — three coherent views.
        gate (out, gate): lorenz → sign(x); rossler → z spikes.
    """

    TYPE = "chaos"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "system": "lorenz",
        "rate": 1.0,
        "range": 2.0,
        "bipolar": False,
        "seed": 1,
    }
    INPUT_PORTS = [
        Port("reset", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("x", "out", "cv"),
        Port("y", "out", "cv"),
        Port("z", "out", "cv"),
        Port("gate", "out", "gate"),
    ]
