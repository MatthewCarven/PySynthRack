"""CVToFrequency module — CV-controlled oscillator with three-point Hz mapping.

A self-contained oscillator that maps an incoming CV signal to a
frequency in Hz via a user-specified three-point curve (``f0`` at
CV=0, ``fm`` at CV=0.5, ``f1`` at CV=1.0) and then synthesizes a
waveform at that frequency. Unlike :class:`Oscillator` -- whose
``freq_cv`` follows the modular-synth 1V/octave convention against
a base ``freq`` -- this module is opinionated about the CV→Hz
relationship and packages the oscillator + mapping in one node.

The three anchor points are interpolated piecewise around the
midpoint ``fm``:

  * CV ∈ [0, 0.5] interpolates from ``f0`` to ``fm``.
  * CV ∈ [0.5, 1.0] interpolates from ``fm`` to ``f1``.

The ``mode`` param picks the interpolation flavour:

  * ``"log"`` (default) interpolates in log-Hz, so equal CV
    steps produce equal *octave* steps — the musical default.
    Recommended for pitch envelopes, LFO-driven bass sweeps,
    musical tracking.
  * ``"linear"`` interpolates literal Hz, producing equal-Hz
    splits that sound deliberately bent / non-musical over wide
    spans. Useful for FM sidebands, sub-audio sweeps, or any
    "this isn't supposed to sound in tune" gesture.

Bipolar CV in phase 1 is clamped to ``[0, 1]`` before mapping.
Phase 2 (planned) adds a mirror three-point mapping for the
negative side so an LFO can sweep a full bipolar range across
two independently-shaped curves.

Unpatched CV falls back to the ``freq`` param — the module always
produces sound, matching :class:`Oscillator`'s pattern (where an
unpatched ``freq_cv`` is treated as zero, so the static ``freq``
plays). This is deliberately different from :class:`CVToAudio`,
which is silent without an input, because CVToFrequency is a
*sound source*, not a passthrough.

Voice-awareness is by shape polymorphism on the CV input. A 1D
``(F,)`` CV drives a single phase accumulator and emits ``(F,)``.
A 2D ``(V, F)`` CV (from a polyphonic source — e.g. a per-voice
ADSR via a CVCombiner) drives V independent phase accumulators
and emits ``(V, F)``. Same convention as the rest of the v0.4
voice-aware DSP modules.

Phase 1 ships the positive-side mapping only. Phase 2 will add
``negative_enabled``, ``f0_neg`` / ``fm_neg`` / ``f1_neg``, and an
independent ``mode_neg`` while preserving the phase-1 clamp
behaviour by default. See ``memory/project_cvtofrequency_plan.md``
for the full design.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Mirrors oscillator.WAVEFORMS: naive + PolyBLEP/PolyBLAMP + wavetable.
WAVEFORMS = (
    "sine",
    "saw",
    "square",
    "triangle",
    "saw_blep",
    "square_blep",
    "triangle_blep",
    "saw_wt",
    "square_wt",
    "triangle_wt",
)
MODES = ("log", "linear")


@register_module_type
class CVToFrequency(Module):
    """CV-controlled oscillator with a three-point CV→Hz mapping.

    Parameters:
        waveform: One of ``"sine"``, ``"saw"``, ``"square"``,
            ``"triangle"``. Default ``"sine"``.
        f0: Frequency in Hz at CV=0. Default 110.0 (musical A2).
        fm: Frequency in Hz at CV=0.5. Default 440.0 (musical A4).
        f1: Frequency in Hz at CV=1.0. Default 1760.0 (musical A6).
        freq: Fallback frequency in Hz when no CV is patched.
            Default 440.0. Matches :class:`Oscillator`'s pattern:
            the module always produces sound.
        mode: ``"log"`` (default, equal-octave splits) or
            ``"linear"`` (equal-Hz splits — deliberately non-
            musical for FM-style sweeps).

    Ports:
        cv (in, cv): CV input clamped to [0, 1] before mapping.
            Bipolar sources have their negative half clipped to
            f0 in phase 1; phase 2 will add a separate negative-
            side mapping.
        out (out, audio): synthesized waveform.
    """

    TYPE = "cv_to_frequency"
    DEFAULT_PARAMS = {
        "waveform": "sine",
        "f0": 110.0,
        "fm": 440.0,
        "f1": 1760.0,
        "freq": 440.0,
        "mode": "log",
    }
    INPUT_PORTS = [Port("cv", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
