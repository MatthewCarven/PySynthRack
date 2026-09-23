"""Oscillator module — generates a periodic waveform."""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

WAVEFORMS = (
    # Naive shapes (cheap; alias above the fundamental).
    "sine",
    "saw",
    "square",
    "triangle",
    # PolyBLEP (saw/square) + PolyBLAMP (triangle) band-limited.
    "saw_blep",
    "square_blep",
    "triangle_blep",
    # Band-limited wavetable (per-octave additive mipmap).
    "saw_wt",
    "square_wt",
    "triangle_wt",
)

# The pulse width is clamped to this band, param and CV alike. Below 5% the
# pulse is a click train; above 95% it is the inverted click train; and at
# the rails the PolyBLEP corrections at the two edges would start to
# overlap at ordinary pitches (an edge pair closer than one correction
# window is not a pulse the blep can draw).
PULSE_WIDTH_MIN = 0.05
PULSE_WIDTH_MAX = 0.95


@register_module_type
class Oscillator(Module):
    """A simple audio-rate oscillator.

    Parameters:
        waveform: Shape + band-limiting method. Naive ``"sine"`` /
            ``"saw"`` / ``"square"`` / ``"triangle"``; PolyBLEP/PolyBLAMP
            ``"saw_blep"`` / ``"square_blep"`` / ``"triangle_blep"``; or
            band-limited wavetable ``"saw_wt"`` / ``"square_wt"`` /
            ``"triangle_wt"``. ``sine`` is already band-limited so it has
            no anti-aliased variant. See ``WAVEFORMS`` for the full tuple.
        freq: Frequency in Hz. v0.1 only supports a static value set via the
            UI; v0.2 will accept a CV cable on a ``freq_cv`` input port.
        amp: Linear amplitude in [0, 1].
        pulse_width: Duty cycle of the ``square`` and ``square_blep``
            shapes, 0.05..0.95 (0.5 = the classic symmetric square). The
            pulse is high while the phase is below the width. A non-50%
            pulse carries a DC offset of ``2*pw - 1``, which is removed
            per sample so slow width sweeps don't pump the speaker (at
            0.5 the correction is exactly zero). ``square_wt`` is a fixed
            50% table and ignores this: the wavetable flavour is for
            band-limited vintage tone, not PWM. Other shapes ignore it.
        pw_cv_depth: Width per CV unit on ``pw_cv`` (default 0.5, so a
            bipolar +/-1 LFO sweeps the whole 0.05..0.95 band).

    The pulse width is evaluated per sample: ``pw[n] = clip(pulse_width +
    pw_cv_depth * pw_cv[n], 0.05, 0.95)``. ``pw_cv`` is voice-aware like
    ``freq_cv`` -- a ``(V, F)`` source gives each voice its own width, a
    mono source is shared by every voice. ``square_blep`` keeps both
    edges band-limited under modulation: the falling edge's correction
    rides on the falling edge's own phase and its own per-sample
    increment (``dt - dpw``), so a width that moves against the phase
    still gets exactly one correction pair per edge.

    Renders are block-size exact on every waveform, mono and per voice
    (2026-09-24): with ``freq_cv`` unpatched the phase is an origin plus
    an integer sample count (the organ's scheme), with it patched a
    running sum carried unwrapped across blocks and wrapped only at
    absolute 65 536-sample epochs, and the ``_wt`` mipmap band is picked
    per sample. See the renderer's docstrings in ``numpy_backend``.
    """

    TYPE = "oscillator"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "waveform": "sine",
        "freq": 440.0,
        "amp": 0.5,
        "pulse_width": 0.5,
        "pw_cv_depth": 0.5,
    }
    INPUT_PORTS = [
        # Frequency CV: 1 volt = 1 octave. ``freq`` becomes
        # ``freq * 2 ** cv[n]`` evaluated per sample, so a bipolar
        # LFO produces real vibrato and an audio-rate signal here
        # gives FM. Unpatched = no modulation.
        Port("freq_cv", "in", "cv"),
        # Amplitude CV: linear multiplicative. ``amp`` becomes
        # ``amp * cv[n]`` per sample when patched. A unipolar LFO
        # here is ring-modulator-ish AM. Unpatched = no modulation.
        Port("amp_cv", "in", "cv"),
        # Pulse-width CV: ``pulse_width + pw_cv_depth * cv[n]`` per
        # sample, clamped to 0.05..0.95; only the square / square_blep
        # shapes listen. A slow LFO here is the classic PWM pad.
        # Unpatched = the static ``pulse_width``.
        Port("pw_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
