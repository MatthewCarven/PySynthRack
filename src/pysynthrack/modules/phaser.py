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
phase apart, so the notches sweep out of step between the channels for a
wide, rotating image. ``spread`` is that phase offset: **0** puts both
chains on the same LFO (one notch sweep, dead centre), **0.5** is the
quarter-cycle quadrature the module shipped with, and **1** runs them a
half cycle apart — when one channel's notches are at the top of their
travel the other's are at the bottom, the widest the pair goes. Patch
``out_l`` / ``out_r`` into the ``left_speaker_output`` /
``right_speaker_output`` modules.

**Tempo sync.** Patch a gate into ``clock`` and the sweep stops being a
speed in Hz and becomes a *length in beats*: one full notch sweep every
``division`` ticks of whatever is cabled (4 = a sweep per bar of four,
8 = a two-bar sweep, 1 = a sweep per beat, 0.5 = twice a beat). The
period is measured between the last two rising edges, so the sweep
follows a tempo that moves; until two edges have arrived the ``rate``
knob still drives it, and with ``clock`` unpatched ``rate`` is all there
is. While the lock holds, ``rate`` and ``rate_cv`` step aside entirely —
the sweep length is the cable's, and the phase is keyed to the absolute
sample count rather than accumulated, so a synced sweep renders
bit-identically at any block size.

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
  * ``spread`` — the L/R LFO phase offset, 0..1 (0 mono, 0.5 the shipped
    quadrature, 1 a half cycle apart).
  * ``division`` — with ``clock`` patched, how many ticks one sweep takes.

A ``rate_cv`` input modulates the LFO rate (1 V/oct, scaled by
``cv_depth`` in octaves per unit), so an envelope or a second LFO can
drive the sweep — an auto-phaser that speeds up and slows down.

**The manual jack.** ``center`` is the phaser's manual knob — where the
notches sit before the LFO moves them — and ``manual_cv`` is that knob as
a jack, read **per sample** and scaled by ``manual_depth`` in octaves per
unit. Turn ``depth`` to 0 and the LFO stops entirely; then an
``audio_to_cv`` follower or an ``adsr`` into ``manual_cv`` sweeps the notches
from the playing itself. That is the classic envelope phaser — the notch
position tracks how hard you hit rather than a clock — and it stacks with
the LFO if you leave ``depth`` up.

Use cases:
  * The classic swept phaser on an electric-piano chord, a pad or a
    guitar-like lead.
  * High ``feedback`` for a resonant, vocal, almost talk-box sweep.
  * A slow envelope into ``rate_cv`` for a sweep that accelerates through
    a build.
  * ``depth`` 0 + an envelope into ``manual_cv`` for the envelope phaser.
  * A ``clock`` into the sync jack with ``division`` 8 for a sweep that
    takes exactly two bars, however the tempo moves.

Where the [`chorus`] thickens with delay and the [`flanger`] rings with a
short fed-back delay, the phaser is the third of the modulation trio — the
allpass-notch sweep — and the softest, roundest of the three.

Ports:
  * ``in`` (audio): the signal to phase. A polyphonic (voice-aware)
    source is summed to mono first — you phase the mix. Unpatched
    -> silence.
  * ``rate_cv`` (cv): modulates the LFO rate (1 V/oct * ``cv_depth``).
    Optional; unpatched means the LFO runs at ``rate``.
  * ``manual_cv`` (cv): moves the sweep centre per sample (1 V/oct *
    ``manual_depth`` on ``center``). A polyphonic source is summed to
    mono. Optional; unpatched means the notches sit at ``center``.
  * ``clock`` (gate): tempo-syncs the sweep — one sweep every
    ``division`` ticks. Optional; unpatched means ``rate`` rules.
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
        rate: LFO sweep speed in Hz (0.05 .. 10). The free-running speed,
            and the fallback whenever ``clock`` is unpatched.
        division: Clock ticks per sweep (0.25 .. 64) — used only while
            ``clock`` is patched. 4 = one sweep per bar of four.
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
        spread: L/R LFO phase offset, 0 .. 1 (0 = both chains sweep
            together, 0.5 = a quarter cycle apart — the shipped
            quadrature, 1 = half a cycle, counter-sweeping).
        cv_depth: Octaves of LFO-rate shift per unit of ``rate_cv``.
        manual_depth: Octaves of ``center`` shift per unit of
            ``manual_cv`` (0 .. 4).

    Ports:
        in (in, audio): signal to phase (voice sources summed to mono).
            Unpatched -> silence.
        rate_cv (in, cv): modulates LFO rate (1 V/oct * ``cv_depth``).
        manual_cv (in, cv): moves the sweep centre per sample
            (1 V/oct * ``manual_depth``). Voice sources summed to mono.
        clock (in, gate): tempo-syncs the sweep — one sweep every
            ``division`` ticks. Unpatched -> the ``rate`` knob.
        out_l (out, audio): left channel.
        out_r (out, audio): right channel.
    """

    TYPE = "phaser"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "rate": 0.5,
        "division": 4.0,
        "depth": 0.6,
        "center": 800.0,
        "feedback": 0.4,
        "stages": 6,
        "mix": 0.5,
        "spread": 0.5,
        "cv_depth": 1.0,
        "manual_depth": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("rate_cv", "in", "cv"),
        Port("manual_cv", "in", "cv"),
        Port("clock", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
