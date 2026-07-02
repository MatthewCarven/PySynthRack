"""Distortion — a drive pedal: push the signal into a saturating curve.

The rack's first nonlinear stage. Everything before this (filters, EQs,
delays, the modulation trio) reshapes or rearranges the signal
*linearly* — no new harmonics, ever. Distortion is the other food
group: it bends the waveform itself, generating harmonics that were
never in the input. A dull sine grows teeth; a saw through heavy drive
turns into a wall.

``drive`` scales the input into the curve — more drive, more of the
signal pushed into the bend, more harmonics. ``mode`` picks the curve:

* ``soft`` — normalised tanh. Smooth, warm, symmetric; odd harmonics
  only. The classic overdrive.
* ``hard`` — straight clipping at the rails. Aggressive, buzzy; odd
  harmonics with much slower roll-off. The fuzz/transistor sound.
* ``tube`` — an asymmetric tanh (positive half bends earlier than the
  negative). Adds *even* harmonics too — the octave-flavoured warmth of
  valve gear. The DC component asymmetry creates is removed by an
  internal DC blocker.

``tone`` is the classic post-distortion low-pass (in Hz — turn it down
to tame the fizz; at 20 kHz it's out of the circuit entirely).
``level`` trims the output (saturation is loud), and ``mix`` blends the
dry signal back in for parallel-distortion textures. ``drive_cv``
modulates the drive per sample (an envelope makes it bite on the
attack; an LFO makes it chew).

**Oversampled 4×.** Nonlinear curves generate harmonics past Nyquist
that fold back as inharmonic aliasing hash at the native rate. The
whole curve runs at 4× the sample rate between a streaming polyphase
up/down pair, so those harmonics are filtered off *before* they can
fold. Costs a fixed ~16 samples of latency (~0.4 ms); the dry path of
``mix`` is delay-compensated to match, and ``mix`` = 0 is a bit-exact
passthrough.

Use it: after a VCA for a driven lead; on the noise source for grit;
in parallel (``mix`` ~0.3) to thicken a bass without losing its
fundamental; ``tube`` at low drive as a subtle exciter on the master.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Saturation curve families. ``soft`` = normalised tanh (odd harmonics,
# smooth), ``hard`` = clipping at the rails (odd, aggressive),
# ``tube`` = asymmetric tanh (adds even harmonics, DC-blocked).
DISTORTION_MODES = ("soft", "hard", "tube")


@register_module_type
class Distortion(Module):
    """Saturating drive stage (soft / hard / tube), 4x oversampled.

    Parameters:
        drive: How hard the signal is pushed into the curve (0.1..30).
            Low values are nearly clean; high values saturate fully.
        mode: Curve family — ``soft`` (tanh), ``hard`` (clip) or
            ``tube`` (asymmetric, even harmonics).
        tone: Post-distortion low-pass cutoff in Hz. 20000 = bypassed.
        level: Output trim (0..2) — saturation is loud.
        mix: Dry/wet. 0 = bit-exact passthrough, 1 = fully distorted.
        cv_depth: Drive units added per unit of ``drive_cv``.

    Ports:
        in (audio): signal to distort. Unpatched -> silence.
        drive_cv (cv): per-sample drive modulation, scaled by
            ``cv_depth``. Optional.
        out (audio): the distorted signal.
    """

    TYPE = "distortion"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "drive": 4.0,
        "mode": "soft",
        "tone": 20000.0,
        "level": 1.0,
        "mix": 1.0,
        "cv_depth": 5.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio"), Port("drive_cv", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
