"""Supersaw — seven detuned band-limited saws per voice (JP-8000 lineage).

The trance chord machine: each voice is a stack of seven PolyBLEP saws —
one center, six spread around it on the classic **asymmetric** detune
table (the sides don't mirror exactly, which is half the shimmer). The
outermost pair reaches ±50 cents at ``detune`` = 1. Free-running seeded
initial phases per (voice slot, saw) are the other half of the
signature — phase-locked saws buzz; free ones *hover* — and being
seeded per slot they are deterministic: a patch renders the same way
every time.

``blend`` is the JP control: 0 = the center saw alone, up = the six
side saws rise as the center recedes. The seven gains are
RMS-normalised (``1/√Σg²``) so blend/detune moves don't pump the
level. ``spread`` pans the saw stack across the stereo field
(flat-side saws left, sharp-side right, equal-power) — at 0 the two
outs are bit-identical (patch either one for mono).

Voice-aware via ``freq_cv`` (1 V/oct around ``freq``, per-sample —
true FM/vibrato, the oscillator convention): a ``(V, F)`` pitch buys
V independent 7-saw stacks — [`chord`](#chord) → supersaw is the
obvious wall of sound. ``amp_cv`` broadcast-multiplies at the end.

Ports:
  * ``freq_cv`` (cv, in): 1 V/oct around ``freq``. Optional;
    voice-aware.
  * ``amp_cv`` (cv, in): linear level. Optional.
  * ``out_l``, ``out_r`` (audio, out): the stack. Identical at
    ``spread`` 0.

Params:
  * ``freq``: base frequency in Hz. Default 261.6256 (C4).
  * ``detune``: 0..1 — outer saws reach ±50 ct at 1. Default 0.35.
  * ``blend``: 0..1 — center saw vs the six sides. Default 0.75.
  * ``spread``: 0..1 — stereo width of the stack. Default 0.5.
  * ``amp``: output level. Default 0.5.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# The classic asymmetric detune offsets (JP-8000 analysis lineage),
# unit-normalised to the outermost saw; index 3 is the center. Scaled
# by SUPERSAW_MAX_CENTS · detune into frequency ratios.
SUPERSAW_OFFSETS = (-1.0, -0.5716, -0.1774, 0.0, 0.1810, 0.5650, 0.9766)
SUPERSAW_MAX_CENTS = 50.0
# Stereo placement per saw at full spread (flat side left, sharp side
# right); the center saw stays in the middle.
SUPERSAW_PAN = (-1.0, -0.6, -0.2, 0.0, 0.2, 0.6, 1.0)
SUPERSAW_N = 7


@register_module_type
class Supersaw(Module):
    """Seven detuned PolyBLEP saws per voice (see the module docstring).

    Parameters:
        freq: Base frequency in Hz. Default 261.6256 (C4).
        detune: Spread of the seven saws, 0..1 (±50 ct outer at 1).
            Default 0.35.
        blend: Center vs side saws, 0..1. Default 0.75.
        spread: Stereo width, 0..1. Default 0.5.
        amp: Output level. Default 0.5.

    Ports:
        freq_cv (in, cv): 1 V/oct around freq (per-sample). Optional.
        amp_cv (in, cv): linear level. Optional.
        out_l, out_r (out, audio): identical at spread 0.
    """

    TYPE = "supersaw"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "freq": 261.6256,
        "detune": 0.35,
        "blend": 0.75,
        "spread": 0.5,
        "amp": 0.5,
    }
    INPUT_PORTS = [
        Port("freq_cv", "in", "cv"),
        Port("amp_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
