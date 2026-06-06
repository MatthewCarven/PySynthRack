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

Bipolar CV (phase 2, shipped 2026-06-07): with ``negative_enabled``
(default False) the module grows a *mirror* three-point curve for
the negative side — ``f0_neg`` at CV=0, ``fm_neg`` at CV=-0.5,
``f1_neg`` at CV=-1.0 — with its own independent ``mode_neg``, so a
bipolar LFO can sweep e.g. musically (log) on the upswing and bent
(linear) on the downswing. CV exactly 0 belongs to the positive
side; the zero-crossing snaps ``f0`` → ``f0_neg`` and continuity is
deliberately the user's choice: set them equal for a smooth crossing
or different for a hard step. CV outside [-1, 1] clamps to the
nearest endpoint. With ``negative_enabled`` False, bipolar CV is
clamped to ``[0, 1]`` before mapping, exactly as phase 1 shipped.

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
            ``"triangle"`` (plus ``*_blep`` / ``*_wt`` anti-aliased
            forms). Default ``"sine"``.
        f0: Frequency in Hz at CV=0. Default 110.0 (musical A2).
        fm: Frequency in Hz at CV=0.5. Default 440.0 (musical A4).
        f1: Frequency in Hz at CV=1.0. Default 1760.0 (musical A6).
        freq: Fallback frequency in Hz when no CV is patched.
            Default 440.0. Matches :class:`Oscillator`'s pattern:
            the module always produces sound.
        mode: ``"log"`` (default, equal-octave splits) or
            ``"linear"`` (equal-Hz splits — deliberately non-
            musical for FM-style sweeps).
        negative_enabled: Phase 2. Default False. When True, CV in
            [-1, 0) maps through the independent negative-side
            curve below instead of clamping to ``f0``.
        f0_neg: Hz at CV=0 approached from below. Default 110.0
            (equal to ``f0``'s default, so the zero-crossing is
            smooth until deliberately split).
        fm_neg: Hz at CV=-0.5. Default 440.0.
        f1_neg: Hz at CV=-1.0. Default 1760.0.
        mode_neg: ``"log"`` or ``"linear"``, independent of
            ``mode`` — mix scales across the sign at will.

    Ports:
        cv (in, cv): CV input. [0, 1] maps through the positive
            curve; with ``negative_enabled``, [-1, 0) maps through
            the negative curve; otherwise it clamps to [0, 1].
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
        "negative_enabled": False,
        "f0_neg": 110.0,
        "fm_neg": 440.0,
        "f1_neg": 1760.0,
        "mode_neg": "log",
    }
    INPUT_PORTS = [Port("cv", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
