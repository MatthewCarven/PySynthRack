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

CV: the split point is voltage-controllable. Patch a CV source (LFO,
envelope, sequencer…) into ``freq_cv`` and the crossover frequency
sweeps 1 V/oct — ``freq`` is multiplied by ``2 ** (cv_depth * cv)``,
block-mean like the Filter's ``cutoff_cv`` and the modulation FX'
``rate_cv``. ``cv_depth`` sets how many octaves one CV unit moves the
corner (default 1.0 = the standard 1 V/oct). Leave ``freq_cv`` unpatched
and the corner is the static ``freq`` param exactly as before. A
voice-aware ``freq_cv`` is averaged to a single macro sweep — the
crossover keeps one coefficient set shared across voices by design.

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
    """LR4 two-way audio crossover with a CV-sweepable split point.

    Parameters:
        freq: Crossover corner in Hz. Clamped to (20 Hz,
            0.45 * sample_rate) by the renderer to keep the biquads
            numerically stable.
        cv_depth: Octaves the corner moves per unit of ``freq_cv``
            (1 V/oct). Default 1.0. Ignored when ``freq_cv`` is
            unpatched.

    Ports:
        in (in, audio): the signal to split.
        freq_cv (in, cv): sweeps the crossover corner (1 V/oct ×
            ``cv_depth``); optional.
        low (out, audio): everything below the corner.
        high (out, audio): everything above the corner.
    """

    TYPE = "crossover"
    PARAM_ALIASES = {"frequency": "freq"}  # legacy name
    DEFAULT_PARAMS = {"freq": 1000.0, "cv_depth": 1.0}
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("freq_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("low", "out", "audio"),
        Port("high", "out", "audio"),
    ]
