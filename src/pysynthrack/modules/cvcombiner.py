"""CVCombiner — combine multiple CV signals into one.

Fills a genuine architectural gap: each CV input jack accepts exactly
one cable (the Patch model enforces this), so if you want both an LFO
*and* an ADSR modulating the same parameter (filter cutoff, oscillator
freq, VCA amp …) you need somewhere to merge them first. That somewhere
is here.

Four CV inputs, one CV output. Mode is either:

* ``"sum"`` (default) — the analog-modular convention. Two unit LFOs
  swinging ±1 add up to a ±2 swing. With 1V/oct CV destinations this
  means two LFOs at depth=1.0 will give you a two-octave sweep, not
  one. Great for stacking, easy to overcook; turn the source depths
  down to taste.

* ``"average"`` — divides by the *connected* input count so blending
  two depth-1.0 LFOs still gives a depth-1.0 result. Better when you
  want LFO+ADSR to share control of a param without the sum doubling
  the modulation depth.

Unconnected inputs contribute zero in both modes and don't influence
the divisor in ``"average"`` mode. If nothing is connected the output
is silence — patches with disconnected CVCombiners still render.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


CVCOMBINER_MODES = ("sum", "average")


@register_module_type
class CVCombiner(Module):
    """Sum or average up to four CV sources into one CV output."""

    TYPE = "cv_combiner"
    CATEGORY = "Routing & VCA"
    DEFAULT_PARAMS = {"mode": "sum"}
    INPUT_PORTS = [
        Port("in1", "in", "cv"),
        Port("in2", "in", "cv"),
        Port("in3", "in", "cv"),
        Port("in4", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "cv")]


CVCOMBINER_INPUT_NAMES = ("in1", "in2", "in3", "in4")
