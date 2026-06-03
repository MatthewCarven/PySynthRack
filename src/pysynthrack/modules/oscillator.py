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
    """

    TYPE = "oscillator"
    DEFAULT_PARAMS = {
        "waveform": "sine",
        "freq": 440.0,
        "amp": 0.5,
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
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
