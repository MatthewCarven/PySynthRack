"""Flanger — a swept, resonant comb (the jet-plane whoosh).

A flanger mixes the input with a *very short* delayed copy of itself and
sweeps that delay up and down with an internal low-frequency oscillator.
Summing a signal with a delayed version of itself builds a **comb
filter** — a stack of evenly-spaced notches — and moving the delay slides
those notches up and down the spectrum. That sweeping comb is the classic
flanger sound: the "jet plane", the whoosh, the metallic sweep.

A portion of the delayed signal is fed back into the line (**feedback**,
a.k.a. regeneration), which sharpens the comb into ringing resonances.
The feedback here is **bipolar**: positive values give a bright, ringing
sweep; negative values invert each pass for a hollow, more metallic
character (the odd-harmonic comb).

Where a chorus uses a *longer* delay and *no* feedback to thicken a sound
into an ensemble, a flanger uses a *shorter* delay *with* feedback to make
that resonant sweep — the two are close cousins, and this is the fed-back
sibling the chorus's docs point at.

The sweep is spread across a **stereo pair** — ``out_l`` and ``out_r``
run their own comb from the same mono input, with the L and R LFOs a
quarter-cycle apart, so the notches sweep out of step between the
channels for a wide, rotating image. Patch ``out_l`` / ``out_r`` into the
``left_speaker_output`` / ``right_speaker_output`` modules.

Controls:
  * ``rate`` — LFO sweep speed in Hz. Slow (~0.2 Hz) is a long, ocean-
    liner sweep; faster shades toward a warble.
  * ``depth`` — how far the delay is swept, 0..1. Higher sweeps the comb
    across more of the spectrum.
  * ``manual`` — the centre delay in **milliseconds** (the "manual" knob).
    Short (~0.5 ms) is a tight, high whoosh; longer (~5 ms) is a lower,
    hollower sweep. The sweep moves around this centre.
  * ``feedback`` — regeneration, **bipolar** (−0.95..0.95). 0 is a plain
    moving comb; toward +1 rings brightly; toward −1 goes hollow/metallic.
  * ``mix`` — dry/wet balance. The comb is deepest at ~0.5 (equal dry and
    wet). At 0 the output is a bit-exact dry passthrough on both channels.

A ``rate_cv`` input modulates the LFO rate (1 V/oct, scaled by
``cv_depth`` in octaves per unit), so an envelope or a second LFO can
drive the sweep — an auto-flanger that speeds up and slows down.

Use cases:
  * The classic flanged sweep on a saw lead or a drum bus.
  * Negative feedback + short ``manual`` for a hollow, phaser-adjacent
    tone.
  * A slow envelope into ``rate_cv`` for a sweep that accelerates through
    a build.

Ports:
  * ``in`` (audio): the signal to flange. A polyphonic (voice-aware)
    source is summed to mono first — you flange the mix. Unpatched
    -> silence.
  * ``rate_cv`` (cv): modulates the LFO rate (1 V/oct * ``cv_depth``).
    Optional; unpatched means the LFO runs at ``rate``.
  * ``out_l`` (audio): left channel (dry + swept comb A).
  * ``out_r`` (audio): right channel (dry + swept comb B).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Flanger(Module):
    """Swept resonant comb flanger (mono in, L/R out, bipolar feedback).

    Parameters:
        rate: LFO sweep speed in Hz (0.05 .. 10).
        depth: Sweep amount, 0 (static comb) .. 1 (wide sweep).
        manual: Centre delay in milliseconds (0.1 .. 10). The comb sweeps
            around this delay; shorter = higher/tighter, longer = lower.
        feedback: Regeneration, bipolar (−0.95 .. 0.95). 0 = plain comb;
            positive = ringing; negative = hollow/metallic.
        mix: Dry/wet balance, dry (0) -> wet (1). The comb is deepest near
            0.5; 0 is a bit-exact dry passthrough on both channels.
        cv_depth: Octaves of LFO-rate shift per unit of ``rate_cv``.

    Ports:
        in (in, audio): signal to flange (voice sources summed to mono).
            Unpatched -> silence.
        rate_cv (in, cv): modulates LFO rate (1 V/oct * ``cv_depth``).
        out_l (out, audio): left channel.
        out_r (out, audio): right channel.
    """

    TYPE = "flanger"
    DEFAULT_PARAMS = {
        "rate": 0.3,
        "depth": 0.7,
        "manual": 1.5,
        "feedback": 0.5,
        "mix": 0.5,
        "cv_depth": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("rate_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
