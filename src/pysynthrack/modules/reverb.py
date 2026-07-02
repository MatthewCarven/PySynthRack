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
  * ``decay_cv`` (cv): added to ``decay`` (× ``cv_depth``) — animate the
    tail length (open the room on held notes). Optional.
  * ``damping_cv`` (cv): added to ``damping`` (× ``cv_depth``) — darken
    the tail over a phrase, or LFO the air in the room. Optional.
  * ``mix_cv`` (cv): added to ``mix`` (× ``cv_depth``) — envelope-driven
    reverb throws / wet ducking. Optional.
  * ``out_l`` (audio): left channel (dry + decorrelated wet tap A).
  * ``out_r`` (audio): right channel (dry + decorrelated wet tap B).

CV: all three targets are 0..1 macros, so the depths are level units
per CV unit (one shared ``cv_depth``, default 1.0, like the Loudness),
summed additively and block-meaned — one macro value per block, then
clamped to 0..1 exactly as the static params are. Damping is safe to
sweep: it only retunes the recirculation low-pass between blocks (the
filter state carries). ``size`` deliberately has no CV: sweeping the
delay-line lengths clicks.
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
        cv_depth: Level units per CV unit, shared by ``decay_cv``,
            ``damping_cv`` and ``mix_cv``. Default 1.0; 0 disables all
            three.

    Ports:
        in (in, audio): signal to reverberate (voice sources summed to
            mono). Unpatched -> silence.
        decay_cv (in, cv): added to ``decay``, scaled by ``cv_depth``.
        damping_cv (in, cv): added to ``damping``, scaled by ``cv_depth``.
        mix_cv (in, cv): added to ``mix``, scaled by ``cv_depth``.
        out_l (out, audio): left channel.
        out_r (out, audio): right channel.
    """

    TYPE = "reverb"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "size": 0.5,
        "decay": 0.5,
        "damping": 0.5,
        "mix": 0.3,
        "cv_depth": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("decay_cv", "in", "cv"),
        Port("damping_cv", "in", "cv"),
        Port("mix_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
