"""Crossover — Linkwitz-Riley 4th-order (LR4) two-way audio splitter.

One audio input, two audio outputs: ``low`` (below the crossover
frequency) and ``high`` (above it). LR4 is the textbook choice for
two-way crossovers because the low + high branches sum back to a flat
magnitude response — no notch or peak at the crossover frequency, just
a clean phase-aligned split.

How it works: LR4 = two cascaded Butterworth (Q = 1/√2 ≈ 0.7071)
biquads per branch. Each branch is 4th order, contributing a full 2π
of phase rotation, which is why they recombine cleanly. Stagger the
modes — both branches lowpass-then-lowpass for the low side, both
highpass-then-highpass for the high — at the same corner frequency.

Use cases:
  * Send sub-frequencies dry and high-frequencies through a chorus/delay.
  * Build a multi-band compressor (eventually — once we have a
    compressor module).
  * Mix a sub-oscillator only into the low band so the patch keeps
    bottom-end clarity even when the upper voicings get busy.

Patching the ``low`` and ``high`` outputs into a ``Combiner`` will
reconstruct the input (sample-accurate, modulo the unavoidable group
delay of two cascaded biquads — same delay on both branches so they
align).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Crossover(Module):
    """LR4 two-way audio crossover.

    Parameters:
        frequency: Crossover corner in Hz. Clamped to (20 Hz,
            0.45 * sample_rate) by the renderer to keep the biquads
            numerically stable.
    """

    TYPE = "crossover"
    DEFAULT_PARAMS = {"frequency": 1000.0}
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [
        Port("low", "out", "audio"),
        Port("high", "out", "audio"),
    ]
