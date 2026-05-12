"""Oscillator module — generates a periodic waveform."""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

WAVEFORMS = ("sine", "saw", "square", "triangle")


@register_module_type
class Oscillator(Module):
    """A simple audio-rate oscillator.

    Parameters:
        waveform: One of ``"sine"``, ``"saw"``, ``"square"``, ``"triangle"``.
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
    INPUT_PORTS: list[Port] = []  # v0.1: no modulation inputs yet
    OUTPUT_PORTS = [Port("out", "out", "audio")]
