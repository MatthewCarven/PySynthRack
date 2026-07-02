"""Output modules — sinks that drive a speaker or disk file."""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class SpeakerOutput(Module):
    """Routes its input to the system audio output device.

    Mono, routed to both channels. For placement in the field, see
    :class:`StereoSpeakerOutput` (pan / width — the stereo variant this
    docstring promised since v0.1).

    Parameters:
        gain: Linear gain applied just before output, in [0, 1].
    """

    TYPE = "speaker_output"
    CATEGORY = "Outputs"
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
    CATEGORY = "Outputs"
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
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list[Port] = []


@register_module_type
class StereoSpeakerOutput(Module):
    """The stereo speaker — a sink with a place in the field.

    The full-width sibling the mono :class:`SpeakerOutput` docstring has
    promised since v0.1. Two audio inputs and three knobs put a signal
    *somewhere* between the speakers instead of dead centre:

    * ``pan`` — where. With only ``in_l`` patched the module treats the
      source as MONO and places it with a constant-power (−3 dB centre)
      cos/sin law, so a sweep keeps even loudness. With both inputs
      patched it acts as a BALANCE control: unity at centre, smoothly
      attenuating the far side as you turn.
    * ``width`` — how wide (stereo pairs only; a mono source has no
      side content to widen). Mid/side scaling: 0 collapses to mono,
      1 leaves the pair untouched, up to 2 exaggerates the sides.
    * ``gain`` — how loud, applied last.

    ``pan_cv`` modulates the pan per sample and ``width_cv`` the width
    (both scaled by the shared ``cv_depth``, like the Reverb's paired
    CV inputs — drop a CVScale in front for independent sensitivity):
    an LFO on ``pan_cv`` is the classic autopan, an envelope walks each
    note across the field, and a slow LFO on ``width_cv`` makes the
    image breathe between mono and wide. Voice-aware sources are summed at the jack (the
    implicit-sum-at-mono-sinks rule), and everything lands on the same
    master bus as the other speaker sinks, clipped at ±1.

    At the defaults (pan 0, width 1, gain 1, CV jacks unpatched) a
    stereo pair passes to the bus bit-exactly — patching a
    chorus/reverb straight into this sink is transparent until you
    reach for a knob (or a CV cable).

    Parameters:
        gain: Linear output gain, applied after pan/width.
        pan: Position/balance, -1 (hard left) .. 1 (hard right).
        width: Stereo width, 0 (mono) .. 2 (over-wide). Pairs only.
        cv_depth: Knob units added per CV unit, shared by ``pan_cv``
            and ``width_cv``. 0 disables both.
    """

    TYPE = "stereo_speaker_output"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
        "pan": 0.0,
        "width": 1.0,
        "cv_depth": 1.0,
    }
    INPUT_PORTS = [
        Port("in_l", "in", "audio"),
        Port("in_r", "in", "audio"),
        Port("pan_cv", "in", "cv"),
        Port("width_cv", "in", "cv"),
    ]
    OUTPUT_PORTS: list[Port] = []
