"""Reverb — a stereo Feedback Delay Network (FDN).

Smears a sound into a sense of space by feeding it through a small bank
of delay lines that are cross-mixed into each other by an orthogonal
matrix. Each pass through the network re-injects an energy-preserving
blend of all the lines, so a single input blooms into the dense,
decaying wash of a room or hall. A damping low-pass in the recirculation
rolls the high end off as the tail fades — bright and splashy when open,
warm and dark when closed — like real air and soft surfaces absorbing
treble over distance.

The signal path stays mono, but the reverb emits a **stereo pair**:
``out_l`` and ``out_r`` tap the delay network through two different
(orthogonal) combinations of its lines, so the two channels are
decorrelated — which is exactly what the ear reads as width and space.
Patch them into the ``left_speaker_output`` / ``right_speaker_output``
modules for a wide stereo tail from a mono source.

Controls:
  * ``size`` — scales the delay-line lengths from a small room toward a
    large hall (more delay = a bigger, slower space).
  * ``decay`` — how long the tail rings (reverberation time). Low is a
    short ambience; high is a long, cathedral-like wash.
  * ``damping`` — high-frequency absorption in the tail. Low keeps it
    bright; high makes it dark and woolly.
  * ``mix`` — dry/wet balance. The dry signal is centred in both
    channels; the wet is the decorrelated stereo tail.

Use cases:
  * Put a pad, pluck, or drum in a room: `… → reverb → L/R speakers`.
  * A long, dark tail (`decay` up, `damping` up) behind a sparse melody.
  * A short, bright ambience (`decay` down, `damping` down) to glue a
    patch together without washing it out.

Ports:
  * ``in`` (audio): the signal to reverberate. A polyphonic (voice-aware)
    source is summed to mono first — you reverberate the mix. Unpatched
    -> silence.
  * ``out_l`` (audio): left channel (dry + decorrelated wet tap A).
  * ``out_r`` (audio): right channel (dry + decorrelated wet tap B).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Reverb(Module):
    """Stereo Feedback Delay Network reverb (mono in, L/R out).

    Parameters:
        size: Scales the delay-line lengths, small room (0) -> large
            hall (1).
        decay: Reverberation time (tail length), short (0) -> long (1).
        damping: High-frequency absorption in the tail, bright (0) ->
            dark (1).
        mix: Dry/wet balance, dry (0) -> wet (1).

    Ports:
        in (in, audio): signal to reverberate (voice sources summed to
            mono). Unpatched -> silence.
        out_l (out, audio): left channel.
        out_r (out, audio): right channel.
    """

    TYPE = "reverb"
    DEFAULT_PARAMS = {
        "size": 0.5,
        "decay": 0.5,
        "damping": 0.5,
        "mix": 0.3,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
