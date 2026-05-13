"""VCA module — voltage-controlled amplifier.

Multiplies an audio signal by a CV signal (and a static ``gain``
parameter). This is what makes an ADSR audible: patch the keyboard's
audio into ``audio``, the ADSR's CV into ``cv``, and the VCA's output
becomes the keyboard tone shaped by the envelope.

If the CV input is left unpatched the VCA behaves like a plain gain
stage (CV = 1.0 implicit). That means a VCA in the chain with no
envelope is harmless — the signal flows through untouched.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class VCA(Module):
    """Audio × CV multiplier.

    Parameters:
        gain: Linear gain applied on top of the CV (so a VCA with CV=1
            and gain=0.5 attenuates by half — handy for trimming an
            individual voice in a chain).
    """

    TYPE = "vca"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [
        Port("audio", "in", "audio"),
        Port("cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
