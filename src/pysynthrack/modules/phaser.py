"""Phaser — a swept notch filter (the whooshing, vocal sweep).

A phaser passes the input through a chain of **allpass filters** — stages
that leave the *level* of every frequency untouched but rotate their
*phase* by an amount that grows toward the top of the spectrum. On its own
that is inaudible; the effect appears when the phase-shifted signal is
**summed back with the dry input**. Wherever a stage has rotated a
frequency a half-cycle out of phase with the dry copy, the two cancel and a
**notch** is carved into the spectrum. Each *pair* of allpass stages makes
one notch, so a 4-stage phaser has two notches, 6 stages three, 8 stages
four.

An internal LFO sweeps the allpass **break frequency** up and down, and the
notches slide with it — that gliding, hollow, vocal "whoosh" is the phaser.
Unlike a flanger (whose notches come from a short *delay* and are stacked at
even harmonic spacing), a phaser's notches come from *allpass phase* and are
spread **non-uniformly** across the spectrum, which is the softer, rounder,
less metallic character that sets the two apart.

A portion of the last stage's output is fed back to the input
(**feedback**, or resonance/regeneration), which sharpens the notches into
ringing peaks. The feedback here is **bipolar**: positive gives a bright,
vocal emphasis; negative shifts the notch pattern for a hollower colour.

The sweep is spread across a **stereo pair** — ``out_l`` and ``out_r`` run
their own allpass chain from the same mono input with the L and R LFOs a
quarter-cycle apart, so the notches sweep out of step between the channels
for a wide, rotating image. Patch ``out_l`` / ``out_r`` into the
``left_speaker_output`` / ``right_speaker_output`` modules.

Controls:
  * ``rate`` — LFO sweep speed in Hz. Slow (~0.3 Hz) is a long, breathing
    sweep; faster shades toward a warble.
  * ``depth`` — how far the notches sweep, 0..1, measured in octaves around
    ``center`` (up to ±2 octaves at 1). Higher slides the notches across
    more of the spectrum.
  * ``center`` — the centre frequency of the sweep in **Hz** (the "manual"
    knob). Low centres a dark, throaty sweep; high centres a bright, airy
    one. The sweep moves around this centre.
  * ``feedback`` — resonance/regeneration, **bipolar** (−0.95..0.95). 0 is
    a plain moving notch; toward +1 rings and gets vocal; toward −1 goes
    hollow.
  * ``stages`` — how many allpass stages, 4, 6 or 8 (two, three or four
    notches). More stages = a deeper, busier, more intense sweep.
  * ``mix`` — dry/wet balance. The notches are deepest at ~0.5 (equal dry
    and wet). At 0 the output is a bit-exact dry passthrough on both
    channels.

A ``rate_cv`` input modulates the LFO rate (1 V/oct, scaled by
``cv_depth`` in octaves per unit), so an envelope or a second LFO can
drive the sweep — an auto-phaser that speeds up and slows down.

Use cases:
  * The classic swept phaser on an electric-piano chord, a pad or a
    guitar-like lead.
  * High ``feedback`` for a resonant, vocal, almost talk-box sweep.
  * A slow envelope into ``rate_cv`` for a sweep that accelerates through
    a build.

Where the [`chorus`] thickens with delay and the [`flanger`] rings with a
short fed-back delay, the phaser is the third of the modulation trio — the
allpass-notch sweep — and the softest, roundest of the three.

Ports:
  * ``in`` (audio): the signal to phase. A polyphonic (voice-aware)
    source is summed to mono first — you phase the mix. Unpatched
    -> silence.
  * ``rate_cv`` (cv): modulates the LFO rate (1 V/oct * ``cv_depth``).
    Optional; unpatched means the LFO runs at ``rate``.
  * ``out_l`` (audio): left channel (dry + swept notch chain A).
  * ``out_r`` (audio): right channel (dry + swept notch chain B).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Phaser(Module):
    """Swept allpass notch phaser (mono in, L/R out, bipolar feedback).

    Parameters:
        rate: LFO sweep speed in Hz (0.05 .. 10).
        depth: Sweep width in octaves around ``center``, 0 (static) .. 1
            (±2 octaves).
        center: Centre frequency of the notch sweep in Hz (100 .. 6000).
            The notches sweep around this frequency.
        feedback: Resonance, bipolar (−0.95 .. 0.95). 0 = plain notches;
            positive = ringing/vocal; negative = hollow.
        stages: Number of allpass stages — 4, 6 or 8 (two, three or four
            notches). More = deeper, busier sweep.
        mix: Dry/wet balance, dry (0) -> wet (1). The notches are deepest
            near 0.5; 0 is a bit-exact dry passthrough on both channels.
        cv_depth: Octaves of LFO-rate shift per unit of ``rate_cv``.

    Ports:
        in (in, audio): signal to phase (voice sources summed to mono).
            Unpatched -> silence.
        rate_cv (in, cv): modulates LFO rate (1 V/oct * ``cv_depth``).
        out_l (out, audio): left channel.
        out_r (out, audio): right channel.
    """

    TYPE = "phaser"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "rate": 0.5,
        "depth": 0.6,
        "center": 800.0,
        "feedback": 0.4,
        "stages": 6,
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
