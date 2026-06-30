"""Constant — a steady CV voltage you dial in by hand.

The simplest possible CV source: no inputs, one CV output that holds a
single fixed value every sample. It's the manual knob of a modular —
a patchable DC level — and it earns its keep precisely because so many
destinations are CV inputs:

  * Manual offset/bias: feed a ``Constant`` into a :class:`CVOffset`
    (or a :class:`CVCombiner`) to nudge an LFO/ADSR up or down by a
    fixed amount.
  * Tuned drone: ``constant.out → cv_to_frequency.cv`` turns a number
    into a fixed pitch — a sound source you tune by typing, no
    keyboard or MIDI required.
  * Fixed gain into a VCA: a steady ``value`` on a VCA's ``amp`` CV is
    a manual volume control.
  * Test signal: a known DC level is the easiest thing to verify a
    patch against.

Param:
  * ``value``: the level emitted on every sample. Default 1.0 — a
    unity level is the most useful neutral (0.0 would be silence, and
    you already get that by leaving an input unpatched). CV is
    nominally bipolar ±1 for modulation, but nothing clamps ``value``:
    a 1V/oct destination happily takes 2.0 for "+2 octaves".

The output is always mono ``(frames,)`` — a constant has no voice
context of its own, and a 1D CV broadcasts cleanly against any
per-voice ``(V, frames)`` consumer downstream.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Constant(Module):
    """A fixed CV level. No inputs; one CV output holding ``value``.

    Parameters:
        value: The level emitted on every sample. Default 1.0. Not
            clamped — use it for ±1 modulation offsets or larger
            1V/oct pitch voltages alike.

    Ports:
        out (out, cv): the constant CV signal.
    """

    TYPE = "constant"
    DEFAULT_PARAMS = {"value": 1.0}
    INPUT_PORTS: list[Port] = []
    OUTPUT_PORTS = [Port("out", "out", "cv")]
