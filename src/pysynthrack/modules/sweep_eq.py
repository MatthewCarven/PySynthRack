"""SweepEQ — one resonant band whose centre frequency you sweep with CV.

The focused auto-wah / envelope-filter node. Where :class:`ParametricEQ`
gives you four static bells and :class:`Crossover` splits a signal, SweepEQ
is a single resonant band tuned to *move*: patch an LFO, an envelope
(via :class:`AudioToCV`), a :class:`Sequencer` or a keyboard into
``freq_cv`` and the band's centre frequency sweeps 1 V/oct — the classic
wah "wow" as the resonant peak slides up and down the spectrum.

Three voicings via ``mode``:

  * ``bandpass`` (default) — a resonant band-pass. Only frequencies near
    the centre pass; everything else is rejected. High ``q`` + a sweep =
    the textbook wah pedal. This is the drop-in auto-wah.
  * ``lowpass`` — a resonant low-pass. Sweeping the corner is the other
    classic "wah/yoy" voice (think acid-bassline filter sweeps); the
    resonance sings at the corner.
  * ``peak`` — a peaking EQ *bell* that boosts (or cuts) a swept band
    while leaving the rest of the signal intact. Gentler than the
    filters — an "animated EQ" bump you slide around rather than a wah.
    This is the one voicing the plain :class:`Filter` can't do, since it
    keeps the full-range signal and only lifts the moving band. ``gain``
    (dB) only applies here.

``mix`` blends the processed signal back against the dry input (1.0 =
fully wet, the effect; 0.0 = bypass, bit-exact dry). ``q`` sets the
resonance/width and defaults high for a vocal wah bite. ``cv_depth`` is
octaves of sweep per CV unit (1 V/oct), exactly like the Crossover's
``freq_cv``.

DSP: a single RBJ biquad. ``peak`` reuses the ParametricEQ peaking
coefficients; ``bandpass``/``lowpass`` reuse the Filter's cookbook
coefficients — so the sweep, clamping and stability all match the modules
they borrow from. Shape-polymorphic (mono ``(F,)`` and per-voice
``(V, F)``, each voice its own biquad memory), and the centre frequency
is block-meaned from ``freq_cv`` — one coefficient set per block, shared
across voices (a macro sweep), like the Crossover.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Voicings offered by the mode combo. bandpass first = the default
# auto-wah; peak is the EQ-bell voicing the plain Filter can't do.
SWEEP_EQ_MODES = ("bandpass", "lowpass", "peak")


@register_module_type
class SweepEQ(Module):
    """A single CV-swept resonant band (auto-wah / envelope filter).

    Parameters:
        mode: ``bandpass`` (default, classic wah), ``lowpass`` (resonant
            corner sweep) or ``peak`` (a swept EQ bell; only this mode
            uses ``gain``).
        freq: Centre/corner frequency in Hz. Clamped to (20 Hz,
            0.45 * sample_rate) by the renderer.
        gain: Peak boost/cut in dB — ``peak`` mode only, ignored by the
            filters. 0 dB is transparent.
        q: Resonance / band width. Defaults high (4.0) for a wah voicing;
            clamped to (0.1, 20).
        cv_depth: Octaves the centre moves per unit of ``freq_cv``
            (1 V/oct). Default 1.0.
        mix: Dry/wet blend. 1.0 = fully wet (the effect), 0.0 = bypass
            (bit-exact dry).

    Ports:
        in (in, audio): the signal to filter.
        freq_cv (in, cv): sweeps the centre frequency; optional.
        out (out, audio): the processed (mixed) signal.
    """

    TYPE = "sweep_eq"
    CATEGORY = "Filters & EQ"
    SWEEP_EQ_MODES = SWEEP_EQ_MODES
    DEFAULT_PARAMS = {
        "mode": "bandpass",
        "freq": 800.0,
        "gain": 12.0,
        "q": 4.0,
        "cv_depth": 1.0,
        "mix": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("freq_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
