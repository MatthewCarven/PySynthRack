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


@register_module_type
class LeftSpeakerOutput(Module):
    """Routes its mono input exclusively to the LEFT output channel.

    The numpy backend's drain mixes this sink into the left bus only;
    the right bus stays silent for this node. Place a Left + Right pair
    to get hard-panned stereo without a stereo Speaker module.

    Parameters:
        gain: Linear gain applied just before output, in [0, 1].
    """

    TYPE = "left_speaker_output"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list[Port] = []


@register_module_type
class RightSpeakerOutput(Module):
    """Mirror of :class:`LeftSpeakerOutput` — mono input to the RIGHT
    channel only. Compose with LeftSpeakerOutput for stereo patches.

    Parameters:
        gain: Linear gain applied just before output, in [0, 1].
    """

    TYPE = "right_speaker_output"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list[Port] = []
