"""LFO module — low-frequency oscillator emitting a CV signal.

An LFO is shaped just like an oscillator but typically runs below the
audible range and is used to modulate parameters or scale other signals.
Patched into a VCA's CV input it gives tremolo; patched into a filter's
cutoff (once filter has a CV port — a v0.3 upgrade) it gives wah.

Output is signal-kind ``cv`` so it can plug into VCA.cv and future
CV-modulatable ports, but cannot accidentally route to an audio input.

``bipolar=True`` swings between -1 and +1 (multiplied by ``depth``),
which is the natural shape for pitch / cutoff modulation. ``bipolar=
False`` maps the same wave into the [0, depth] range — the right shape
when feeding a VCA, since negative gain would invert the audio. Default
is unipolar precisely so the LFO → VCA tremolo case "just works".
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

LFO_WAVEFORMS = ("sine", "triangle", "square", "saw", "random")


@register_module_type
class LFO(Module):
    """Low-frequency oscillator. Outputs CV.

    Parameters:
        waveform: ``"sine"`` / ``"triangle"`` / ``"square"`` / ``"saw"`` /
            ``"random"`` (sample-and-hold — re-rolls once per cycle).
        rate: Frequency in Hz. Tremolo lives ~3–8 Hz; slow filter sweeps
            ~0.05–1 Hz; audio-rate FM ~20+ Hz.
        depth: Output amplitude in [0, 1]. Pre-scales the wave before the
            bipolar/unipolar shaping.
        bipolar: True → output in [-depth, +depth]. False → [0, depth].
    """

    TYPE = "lfo"
    DEFAULT_PARAMS = {
        "waveform": "sine",
        "rate": 4.0,
        "depth": 1.0,
        "bipolar": False,
    }
    INPUT_PORTS: list[Port] = []
    OUTPUT_PORTS = [Port("cv", "out", "cv")]
