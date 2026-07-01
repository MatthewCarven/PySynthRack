"""Chorus — a lush, detuned stereo thickener.

Runs the input through a small bank of short modulated delay lines and
mixes them back with the dry signal. Each line is swept by an internal
low-frequency oscillator, and because a moving delay is a moving pitch,
every voice drifts a few cents sharp then flat around the original —
the small, constantly-shifting detune between the copies is what the ear
reads as a single sound *thickened* into an ensemble. It's the shimmer of
a 12-string, an "ensemble" string patch, or a widened synth pad.

The voices are spread across a **stereo pair** — ``out_l`` and ``out_r``
tap the bank through different pan weights and their own slices of the
LFO, so the two channels are decorrelated. That width is the other half
of the chorus sound: patch ``out_l`` / ``out_r`` into the
``left_speaker_output`` / ``right_speaker_output`` modules for a wide
ensemble from a mono source. (There's deliberately no feedback control —
a chorus with feedback becomes a flanger, which is its own module.)

Controls:
  * ``rate`` — LFO speed in Hz. Slow (~0.3 Hz) is a gentle, seasick
    drift; faster (~3–6 Hz) shades toward vibrato/Leslie.
  * ``depth`` — how far the delay is swept, 0..1. Low is subtle
    thickening; high is a wide, warbly detune.
  * ``voices`` — how many detuned copies (1–6). More voices = a denser,
    creamier ensemble (and a wider stereo image).
  * ``mix`` — dry/wet balance. At 0 the output is a bit-exact dry
    passthrough on both channels; ~0.5 is the classic "half in" chorus.

A ``rate_cv`` input modulates the LFO rate (1 V/oct, scaled by
``cv_depth`` in octaves per unit), so a second LFO or an envelope can
speed the shimmer up and slow it down — evolving, breathing chorus.

Use cases:
  * Widen a mono pad or pluck: ``… → chorus → L/R speakers``.
  * Ensemble a saw lead for that classic string-machine sound.
  * A slow envelope or LFO into ``rate_cv`` for a chorus that drifts in
    and out of a shimmer over time.

Ports:
  * ``in`` (audio): the signal to thicken. A polyphonic (voice-aware)
    source is summed to mono first — you chorus the mix. Unpatched
    -> silence.
  * ``rate_cv`` (cv): modulates the LFO rate (1 V/oct * ``cv_depth``).
    Optional; unpatched means the LFO runs at ``rate``.
  * ``out_l`` (audio): left channel (dry + panned wet A).
  * ``out_r`` (audio): right channel (dry + panned wet B).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Chorus(Module):
    """Detuned multi-voice stereo chorus (mono in, L/R out).

    Parameters:
        rate: LFO sweep speed in Hz (0.05 .. 10).
        depth: Sweep amount, 0 (none) .. 1 (wide detune).
        voices: Number of detuned delay voices (1 .. 6).
        mix: Dry/wet balance, dry (0) -> wet (1). 0 is a bit-exact dry
            passthrough on both channels.
        cv_depth: Octaves of LFO-rate shift per unit of ``rate_cv``.

    Ports:
        in (in, audio): signal to thicken (voice sources summed to mono).
            Unpatched -> silence.
        rate_cv (in, cv): modulates LFO rate (1 V/oct * ``cv_depth``).
        out_l (out, audio): left channel.
        out_r (out, audio): right channel.
    """

    TYPE = "chorus"
    DEFAULT_PARAMS = {
        "rate": 0.6,
        "depth": 0.5,
        "voices": 3,
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
