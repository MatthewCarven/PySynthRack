"""Output modules — sinks that drive a speaker or disk file."""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class SpeakerOutput(Module):
    """Routes its input to the system audio output device.

    Mono in v0.1 — a stereo variant arrives once the mixer module exists.

    Parameters:
        gain: Linear gain applied just before output, in [0, 1].
    """

    TYPE = "speaker_output"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list[Port] = []
