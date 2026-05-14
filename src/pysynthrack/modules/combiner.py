"""Combiner — bare audio summer, no per-channel gain.

The lighter sibling of ``Mixer``. Four audio inputs (``in1`` … ``in4``)
are added together with unit gain and emitted on a single ``out``.
Unconnected inputs contribute silence; the result is identical to
patching the same source into one of ``Mixer``'s channels with every
gain at 1.0 and master at 1.0, just without the four gain widgets in
the way.

Use ``Combiner`` when you want a structural sum (parallel filter paths
re-joining, ADSR shape mixed with a square dry-thump, the wet/dry of a
crossover stitched back together). Use ``Mixer`` when you actually need
to balance levels per channel.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Combiner(Module):
    """Sum four audio inputs into one output. No per-channel gain."""

    TYPE = "combiner"
    DEFAULT_PARAMS: dict = {}
    INPUT_PORTS = [
        Port("in1", "in", "audio"),
        Port("in2", "in", "audio"),
        Port("in3", "in", "audio"),
        Port("in4", "in", "audio"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]


# Exposed for the UI/backend so the input count lives in one place.
COMBINER_INPUT_NAMES = ("in1", "in2", "in3", "in4")
