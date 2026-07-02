"""ADSR envelope module — gate in, CV out.

Classic attack / decay / sustain / release shape, sample-accurate. The
envelope listens to a gate signal (high = note held, low = released)
and emits a 0-to-1 CV signal that downstream modules (typically a VCA,
also a Filter cutoff once we wire CV to params) shape by.

Behaviour at the edges of the gate:
  - Gate rises  → enter Attack from the current level. Ramps to 1.0 over
    ``attack`` seconds, then Decay ramps to ``sustain`` over ``decay``
    seconds, then holds until the gate falls.
  - Gate falls  → enter Release. The level at the time of release is
    captured and ramped to 0 over ``release`` seconds. This means a key
    that's released mid-attack still takes the full release time — no
    snap.

All four parameters live on the module; modulation inputs (CV → attack,
etc.) are a later upgrade.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class ADSR(Module):
    """Attack / decay / sustain / release envelope generator.

    Parameters:
        attack: Attack time in seconds (0 → instant).
        decay: Decay time in seconds from 1.0 down to ``sustain``.
        sustain: Sustain level in [0, 1].
        release: Release time in seconds from the gate-fall level down to 0.
    """

    TYPE = "adsr"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "attack": 0.01,
        "decay": 0.10,
        "sustain": 0.70,
        "release": 0.30,
    }
    INPUT_PORTS = [Port("gate", "in", "gate")]
    OUTPUT_PORTS = [Port("cv", "out", "cv")]
