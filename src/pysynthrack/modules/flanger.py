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
phase apart, so the notches sweep out of step between the channels for a
wide, rotating image. ``spread`` is that phase offset: **0** puts both
combs on the same LFO (one sweep, dead centre), **0.5** is the
quarter-cycle quadrature the module shipped with, and **1** runs them a
half cycle apart — when one channel's comb is at the top of its travel
the other's is at the bottom, the widest the pair goes. Patch ``out_l`` /
``out_r`` into the ``left_speaker_output`` / ``right_speaker_output``
modules.

**Tempo sync.** Patch a gate into ``clock`` and the sweep stops being a
speed in Hz and becomes a *length in beats*: one full comb sweep every
``division`` ticks of whatever is cabled (4 = a sweep per bar of four,
8 = a two-bar sweep, 1 = a sweep per beat, 0.5 = twice a beat). The
period is measured between the last two rising edges, so the sweep
follows a tempo that moves; until two edges have arrived the ``rate``
knob still drives it, and with ``clock`` unpatched ``rate`` is all there
is. While the lock holds, ``rate`` and ``rate_cv`` step aside entirely —
the sweep length is the cable's, and the phase is keyed to the absolute
sample count rather than accumulated, so a synced sweep renders
bit-identically at any block size.

**The manual jack.** ``manual`` is the centre delay; ``manual_cv`` is
that knob as a jack, read **per sample** and scaled by ``manual_depth``
in octaves per unit. Turn ``depth`` to 0 and the LFO stops entirely;
then an ``audio_to_cv`` follower or an ``adsr`` into ``manual_cv`` sweeps
the comb from the playing itself — the envelope flanger. In
through-zero mode the jack moves the **reference** tap as well as the
moving one, so the crossing itself travels: the jet passes through zero
wherever the envelope puts it.

**Through-zero mode.** With ``through_zero`` enabled the flanger becomes a
*through-zero* (tape) flanger. Instead of summing the dry signal with a
single positive-delayed copy, it delays a fixed **reference** tap and
sweeps a second **moving** tap around it, so the *relative* delay between
the two passes through zero — and goes negative — twice per LFO cycle. At
each crossing the comb notches sweep out to infinity and the comb flips
polarity: the dramatic "jet through zero" whoosh two tape decks make as
their delays coincide. The ``polarity`` knob sets what happens at the
crossing — ``+1`` is the bright additive **bloom** (the classic tape
sound), ``−1`` the subtractive **null** (a momentary cancellation hole),
and values between blend the two. Feedback still works here (the moving
tap is floored a few samples behind the write head so regeneration stays
stable across the crossing). With ``through_zero`` off the module is the
plain positive-delay flanger, byte-for-byte unchanged.

Controls:
  * ``rate`` — LFO sweep speed in Hz. Slow (~0.2 Hz) is a long, ocean-
    liner sweep; faster shades toward a warble.
  * ``depth`` — how far the delay is swept, 0..1. Higher sweeps the comb
    across more of the spectrum (and, in through-zero mode, how far the
    moving tap travels either side of the reference).
  * ``manual`` — the centre delay in **milliseconds** (the "manual" knob).
    Short (~0.5 ms) is a tight, high whoosh; longer (~5 ms) is a lower,
    hollower sweep. The sweep moves around this centre; in through-zero
    mode it is also the reference-tap delay the moving tap sweeps through.
  * ``feedback`` — regeneration, **bipolar** (−0.95..0.95). 0 is a plain
    moving comb; toward +1 rings brightly; toward −1 goes hollow/metallic.
  * ``mix`` — dry/wet balance. The comb is deepest at ~0.5 (equal dry and
    wet). At 0 the output is a bit-exact dry passthrough on both channels.
  * ``through_zero`` — off: the standard positive-delay flanger. On: the
    tape-style through-zero flanger described above.
  * ``polarity`` — through-zero only: ``+1`` additive bloom (bright),
    ``−1`` subtractive null (cancellation), blended in between.
  * ``spread`` — the L/R LFO phase offset, 0..1 (0 mono, 0.5 the shipped
    quadrature, 1 a half cycle apart).
  * ``division`` — with ``clock`` patched, how many ticks one sweep takes.

A ``rate_cv`` input modulates the LFO rate (1 V/oct, scaled by
``cv_depth`` in octaves per unit), so an envelope or a second LFO can
drive the sweep — an auto-flanger that speeds up and slows down.

Use cases:
  * The classic flanged sweep on a saw lead or a drum bus.
  * Negative feedback + short ``manual`` for a hollow, phaser-adjacent
    tone.
  * A slow envelope into ``rate_cv`` for a sweep that accelerates through
    a build.
  * ``through_zero`` on with a slow ``rate`` for the dramatic tape "jet"
    sweep that thins through zero and blooms back.
  * ``depth`` 0 + an envelope into ``manual_cv`` for the envelope flanger.
  * A ``clock`` into the sync jack with ``division`` 8 for a jet that
    takes exactly two bars, however the tempo moves.

Ports:
  * ``in`` (audio): the signal to flange. A polyphonic (voice-aware)
    source is summed to mono first — you flange the mix. Unpatched
    -> silence.
  * ``rate_cv`` (cv): modulates the LFO rate (1 V/oct * ``cv_depth``).
    Optional; unpatched means the LFO runs at ``rate``.
  * ``manual_cv`` (cv): moves the centre delay per sample (1 V/oct *
    ``manual_depth`` on ``manual``). A polyphonic source is summed to
    mono. Optional; unpatched means the comb sits at ``manual``.
  * ``clock`` (gate): tempo-syncs the sweep — one sweep every
    ``division`` ticks. Optional; unpatched means ``rate`` rules.
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
        rate: LFO sweep speed in Hz (0.05 .. 10). The free-running speed,
            and the fallback whenever ``clock`` is unpatched.
        division: Clock ticks per sweep (0.25 .. 64) — used only while
            ``clock`` is patched. 4 = one sweep per bar of four.
        depth: Sweep amount, 0 (static comb) .. 1 (wide sweep).
        manual: Centre delay in milliseconds (0.1 .. 10). The comb sweeps
            around this delay; shorter = higher/tighter, longer = lower.
            In through-zero mode this is also the reference-tap delay.
        feedback: Regeneration, bipolar (−0.95 .. 0.95). 0 = plain comb;
            positive = ringing; negative = hollow/metallic.
        mix: Dry/wet balance, dry (0) -> wet (1). The comb is deepest near
            0.5; 0 is a bit-exact dry passthrough on both channels.
        spread: L/R LFO phase offset, 0 .. 1 (0 = both combs sweep
            together, 0.5 = a quarter cycle apart — the shipped
            quadrature, 1 = half a cycle, counter-sweeping).
        cv_depth: Octaves of LFO-rate shift per unit of ``rate_cv``.
        manual_depth: Octaves of ``manual`` (centre-delay) shift per unit
            of ``manual_cv`` (0 .. 4).
        through_zero: False = standard positive-delay flanger (unchanged).
            True = tape-style through-zero flanger (reference + moving tap
            whose relative delay sweeps through zero).
        polarity: Through-zero only, −1 .. +1. +1 = additive bloom (bright
            tape sound at the crossing); −1 = subtractive null (a momentary
            cancellation hole); values between blend the two.

    Ports:
        in (in, audio): signal to flange (voice sources summed to mono).
            Unpatched -> silence.
        rate_cv (in, cv): modulates LFO rate (1 V/oct * ``cv_depth``).
        manual_cv (in, cv): moves the centre delay per sample
            (1 V/oct * ``manual_depth``). Voice sources summed to mono.
        clock (in, gate): tempo-syncs the sweep — one sweep every
            ``division`` ticks. Unpatched -> the ``rate`` knob.
        out_l (out, audio): left channel.
        out_r (out, audio): right channel.
    """

    TYPE = "flanger"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "rate": 0.3,
        "division": 4.0,
        "depth": 0.7,
        "manual": 1.5,
        "feedback": 0.5,
        "mix": 0.5,
        "spread": 0.5,
        "cv_depth": 1.0,
        "manual_depth": 1.0,
        "through_zero": False,
        "polarity": 1.0,
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
