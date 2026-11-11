"""Delay — an analog-voiced feedback echo.

Feeds the incoming audio into a delay line and mixes the delayed signal
back into the output. A portion of that delayed signal is fed back into
the line, so the echo repeats and fades; a **damping** low-pass in the
feedback path rolls the high end off a little more on every pass, so the
repeats get progressively darker the way a tape or bucket-brigade (BBD)
echo does — the "analog" voicing — instead of staying digitally bright.

Set the echo spacing in **milliseconds** with ``time`` (slapback at
~80 ms, a roomy dub echo at ~500 ms, up to 2 s). ``feedback`` sets how
many repeats you get — from a single slap at 0 toward a long, near-
infinite tail near 1 (clamped just below runaway). ``tone`` opens or
closes the damping filter (low = dark, woolly repeats; high = bright,
faithful ones). ``mix`` is the dry/wet balance.

A ``time_cv`` input modulates the delay time, summed onto ``time`` and
scaled by ``cv_depth`` (milliseconds per CV unit). Slow modulation gives
tape wow and pitch-bending dub throws; with a short ``time`` and a little
LFO it shades toward chorus/vibrato territory.

Use cases:
  * Slapback on a lead or the FilePlayer (short time, low feedback).
  * Dub/space echo on a sequencer line (longer time, high feedback,
    low ``tone`` so the tail melts away).
  * Rhythmic echoes — patch the sequencer's ``clock`` into ``clock`` and
    the echo locks to it: ``division`` 1 with ``feel`` dotted is the
    dotted-eighth dub echo on an 8th-note clock, at any tempo. Wobble
    ``time_cv`` from an LFO on top for tape flutter.
  * Ping-pong: cable ``out_l`` / ``out_r`` into a stereo sink and the
    repeats bounce left, right, left.
  * Beat-repeat / stutter: pulse ``freeze`` from a slow clock and the last
    ``time`` milliseconds of whatever you played stutter until it drops.

Clock sync (2026-11-11 love pass):
  Patch a clock into ``clock`` and the echo becomes ``division`` clock
  ticks long (0.25..64), times ``feel`` (``straight`` 1, ``dotted`` 3/2,
  ``triplet`` 2/3 -- ratios, so they stay true whatever the clock's own
  division makes a tick), in WHOLE samples. While locked ``time`` steps
  aside; ``time_cv`` still adds on top (wow and dub throws on a synced
  echo), and with no cv the read is whole samples, so a locked echo skips
  the two-tap low-pass. The lock takes three rising edges (the span of
  the last three measures two ticks, which keeps a tempo that isn't a
  whole number of samples, and a swung clock, from retargeting every
  tick); until then ``time`` rules, bit for bit. An interval that breaks
  the two-tick pattern -- a ``reset``, a short stop, a tempo step --
  restarts the measurement from that edge, so no span is ever measured
  across it and the echo holds its time meanwhile; a real tempo step
  lands on its third tick. A clock that stops keeps its last time. A
  target past the 2 s line folds down by octaves, staying on the grid.
  Every change of time -- the lock, a new tempo, a ``division`` or
  ``feel`` move, the unpatch (home to ``time``) -- crossfades the old and
  new reads over 10 ms (one fade at a time; a new target waits its turn;
  during a ``freeze`` hold it waits for the release). The unclocked
  ``time`` knob still jumps, as it always has.

Stereo (2026-11-11 love pass):
  Cable ``out_l`` or ``out_r`` and the delay runs a second line, so the
  repeats ping-pong: an impulse lands L, R, L, R with the far side
  silent (swap the cables to start right). A hard ping-pong is two lines
  feeding each other; their sum obeys the delay's own loop and their
  difference the same loop with the feedback negated, so the existing
  line is the "mid", untouched -- ``out`` is bit-exact whatever the
  stereo jacks do -- and the new "side" line is one sign away. ``spread``
  is the far side's fade (its level is ``1 - spread``) at the output
  only: 1 is hard left-right, 0 is ``out`` on both. The dry stays
  centred. A freeze holds the side with gain -1, so a held ping-pong
  keeps bouncing. Uncabling both jacks drops the side line (a re-cable
  starts it empty, so it can't play back old audio); echoes already in
  the mid then play centred until they decay, and new notes bounce.

Freeze:
  While the ``freeze`` gate is high **the echo hangs**: the line
  recirculates at exactly unity gain with the ``tone`` damping bypassed,
  and the input is muted from the line — so the last ``time`` ms of
  whatever you played repeats, unchanged, for as long as the gate is
  held. The read snaps to a **whole number of samples** while held (the
  held time is ``round(time)`` samples, latched at the rising edge):
  the normal fractional read is a two-tap low-pass, and a unity loop
  through it loses ~9–11 dB in 10 s on a bright echo (measured), while a
  whole-sample read is ``buf[n] = buf[n - D]`` bit for bit — the held
  loop neither decays nor grows (measured over 10 s of freeze: the
  energy in the loop conserved to 1e-9 dB, the output RMS within
  ±0.85 dB of the level caught — the RMS window sliding against the
  loop, not loss). Because
  the read is pinned, ``time_cv`` and the ``time`` knob stop moving it
  while held; they take effect again on release. The dry path through
  ``mix`` never sees the gate, so notes played over a frozen echo are
  heard dry and never pile into the loop. When the gate falls,
  ``feedback`` and ``tone`` resume and the held loop decays away like
  any other repeat. The switch is per sample: an integer-count 10 ms
  ramp from each gate edge crossfades the loop gain, the damping bypass,
  the input mute and the read position together, so a freeze landing
  anywhere in a block is click-free and lands on the same sample at any
  block size. Unpatched, the module is bit-exact with the pre-freeze
  render.

  The ``freeze`` **tickbox** on the panel is ORed with the gate (the
  ``granular`` / ``freeze`` precedent), so you can hang the echo from
  the panel with nothing patched: ticking it is a rising edge at the
  first sample of the block it lands on — the same 10 ms ramp, the same
  ``round(time)`` latch, bit for bit the same hold as a gate cable
  rising there — and clearing it releases like a gate fall. With a gate
  patched the tick simply holds it high. Off, it changes nothing:
  unpatched and un-ticked is still the pre-freeze code.

Ports:
  * ``in`` (audio): the signal to echo. Unpatched -> silence out.
  * ``time_cv`` (cv): added to ``time``, scaled by ``cv_depth``.
    Optional; unpatched means no modulation.
  * ``freeze`` (gate): high (> 0.5) hangs the echo (see Freeze above).
    ORed with the ``freeze`` tickbox. Optional; unpatched means never
    frozen unless ticked.
  * ``clock`` (gate): locks the echo to ``division`` ticks times ``feel``
    (see Clock sync above). Optional; unpatched means ``time`` rules.
  * ``out`` (audio): the dry+echo mix.
  * ``out_l`` / ``out_r`` (audio): the ping-pong pair (see Stereo above).
    Optional; uncabled means no side line runs.

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` audio in
  -> mono ``(F,)`` out through one delay line. A voice-aware ``(V, F)``
  audio in -> ``(V, F)`` out with one delay line per voice slot, so a
  polyphonic source upstream echoes without cross-talk. A mono
  ``time_cv`` broadcasts across voices; a ``(V, F)`` ``time_cv`` drives
  each voice independently. A single voice row is bit-identical to the
  mono path. A ``(V, F)`` ``freeze`` gate collapses to any-voice-high
  (the house sum) and the one row freezes every voice's line together.
  ``clock`` and ``spread`` are shared by every voice (a ``(V, F)`` clock
  collapses like the freeze gate); ``time_cv`` stays per voice.
  ``out_l`` / ``out_r`` take ``out``'s shape -- ``(V, F)`` in gives
  ``(V, F)`` out, each voice with its own mid and side line, nothing
  summed (the stereo sinks sum voices themselves). Sixteen voices in
  stereo cost a second 11.3 MB ring.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# ``feel``: the multiplier a clocked echo takes on top of ``division``, as
# an exact ratio (num, den). Ratios, not note names, so they stay true on
# any clock: what one tick is depends on the clock's own ``division``.
DELAY_FEEL_RATIOS = {
    "straight": (1, 1),
    "dotted": (3, 2),
    "triplet": (2, 3),
}
DELAY_FEELS = tuple(DELAY_FEEL_RATIOS)


@register_module_type
class Delay(Module):
    """Analog-voiced feedback delay (echo) with a damped feedback path,
    clock sync (``clock`` / ``division`` / ``feel``) and a ping-pong pair
    (``out_l`` / ``out_r`` / ``spread``).

    Parameters:
        time: Base delay time in milliseconds (1..2000).
        feedback: Fraction of the delayed signal fed back (0 = one
            repeat; clamped just below 1 to stay stable).
        tone: Damping in the feedback path, 0..1. Low = dark repeats
            (high end rolled off each pass), high = bright/faithful.
        mix: Dry/wet balance, 0 (dry only) .. 1 (wet only).
        cv_depth: Milliseconds of delay time per unit of ``time_cv``.
        freeze: The panel tickbox (default False), ORed with the
            ``freeze`` gate -- hang the echo from the panel with nothing
            patched. Ticking is a rising edge at the first sample of the
            block it lands on (same ramp, same latch, same hold as a
            gate rising there); clearing releases like a gate fall.
        division: Clock ticks per echo while ``clock`` is patched,
            0.25..64 (default 1.0).
        feel: ``straight`` | ``dotted`` | ``triplet`` -- a multiplier of
            1, 3/2 or 2/3 on the clocked echo (default straight).
        spread: 0..1 (default 1.0), the ping-pong's width on
            ``out_l`` / ``out_r``: the far side's level is ``1 - spread``,
            so 1 bounces hard left-right and 0 is ``out`` on both.

    Ports:
        in (in, audio): signal to echo. Unpatched -> silence.
        time_cv (in, cv): added to ``time``, scaled by ``cv_depth``.
        freeze (in, gate): high hangs the echo -- unity loop, damping
            bypassed, input muted from the line, read pinned to whole
            samples (dry still passes). ``(V, F)`` collapses to
            any-voice-high. ORed with the ``freeze`` tickbox. Unpatched
            and un-ticked -> never frozen (bit-exact with the pre-freeze
            render).
        clock (in, gate): a patched clock sets the echo to ``division``
            ticks (times ``feel``), in whole samples, and moves between
            times with a 10 ms crossfade; ``time`` steps aside while
            locked, ``time_cv`` still adds. ``(V, F)`` collapses to the
            house sum. Unpatched -> ``time`` rules (bit-exact).
        out (out, audio): dry + echo mix.
        out_l, out_r (out, audio): the ping-pong pair -- repeats alternate
            L, R, L ... (swap the cables to start right), the dry centred.
            Cabling either runs a second "side" line beside the first, so
            ``out`` is bit-exact whatever these do. Uncabled -> no side
            line at all.
    """

    TYPE = "delay"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "time": 300.0,
        "feedback": 0.4,
        "tone": 0.5,
        "mix": 0.35,
        "cv_depth": 50.0,
        "freeze": False,
        "division": 1.0,
        "feel": "straight",
        "spread": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("time_cv", "in", "cv"),
        Port("freeze", "in", "gate"),
        Port("clock", "in", "gate"),
    ]
    # ``out`` stays first: the engine files a bare-array return under the
    # first output port, and the delay returns a bare array whenever
    # neither stereo jack is cabled.
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
