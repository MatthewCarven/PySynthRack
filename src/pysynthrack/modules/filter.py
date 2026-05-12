"""Filter module — Robert Bristow-Johnson biquad (LP / HP / BP).

A biquad is a 2nd-order IIR filter — two zeros and two poles. The RBJ
cookbook coefficients give us textbook lowpass / highpass / bandpass /
notch / peak shapes from one set of variables (cutoff, Q). We start
with LP / HP / BP in v0.2; the others can come along when there's a use.

Tone-wise:
  * lowpass + saw + a touch of resonance = the classic "wow" synth sweep
  * highpass = the tinny end of a phaser, or removing rumble from a kick
  * bandpass = focused, vocal-like timbre; sweep it for wah

Cutoff is clamped to (20 Hz, 0.45 * sample_rate) and Q to (0.1, 20) to
keep the filter numerically stable across param edits.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

FILTER_MODES = ("lowpass", "highpass", "bandpass")


@register_module_type
class Filter(Module):
    """Biquad audio filter.

    Parameters:
        mode: ``"lowpass"`` / ``"highpass"`` / ``"bandpass"``.
        cutoff: Corner frequency in Hz.
        resonance: Q factor. ~0.707 is "no peak" (Butterworth); higher
            values give a resonant peak that whistles musically when swept.
    """

    TYPE = "filter"
    DEFAULT_PARAMS = {
        "mode": "lowpass",
        "cutoff": 1000.0,
        "resonance": 0.707,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
