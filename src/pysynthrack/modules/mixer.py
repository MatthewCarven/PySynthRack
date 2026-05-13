"""Mixer module — sum multiple audio sources with per-channel gain.

Four audio inputs (``in1`` … ``in4``), per-channel gain trims, and a
master gain. The signal is ``master * sum_i (gain_i * in_i)`` with
unconnected channels contributing silence. Output is audio.

Why four (not N): keeps the JSON schema flat and the UI legible. The
v0.3 ``Combiner`` will handle the unbounded-N case (pure sum, no
per-channel trims). Most v0.2 patches — layered oscillators, dual
keyboard splits, osc + sub-osc + noise + ADSR-gated VCA — fit in four
slots comfortably.

Cabling rule: one cable per input jack (enforced by the patch model).
To merge more than four sources, chain mixers.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Mixer(Module):
    """Four-channel audio mixer.

    Parameters:
        gain1 / gain2 / gain3 / gain4: Linear per-channel gain in
            [0, 2]. Above unity so users can lift a quiet channel.
        master: Linear master gain applied after the sum.
    """

    TYPE = "mixer"
    DEFAULT_PARAMS = {
        "gain1": 1.0,
        "gain2": 1.0,
        "gain3": 1.0,
        "gain4": 1.0,
        "master": 0.7,
    }
    INPUT_PORTS = [
        Port("in1", "in", "audio"),
        Port("in2", "in", "audio"),
        Port("in3", "in", "audio"),
        Port("in4", "in", "audio"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]


# Channel names exposed for the UI / backend so the count lives in one
# place if we ever bump it (or split into mini/full variants).
MIXER_INPUT_NAMES = ("in1", "in2", "in3", "in4")
MIXER_GAIN_NAMES = ("gain1", "gain2", "gain3", "gain4")
