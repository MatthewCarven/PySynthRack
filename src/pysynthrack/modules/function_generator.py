"""Function generator — the west-coast rise/fall function, with EOR/EOC.

One shape, three jobs. A *function generator* is a ramp that goes up over
``rise`` seconds and down over ``fall`` seconds, and what it becomes depends
entirely on how you cable it:

  * an **envelope** — ``trig`` from a keyboard gate, ``out`` into a VCA;
  * an **LFO** — ``mode`` ``loop``, nothing patched in, ``out`` into a filter
    cutoff. Now ``rise``/``fall`` are the up and down slopes of a triangle
    you can make as lopsided as you like (a slow rise and an instant fall
    is a saw; ``curve`` bends it);
  * a **clock** — ``mode`` ``loop`` again, but take ``eoc``: a pulse every
    time the cycle completes, at ``1/(rise+fall)`` Hz, into a
    [sequencer]'s clock;
  * a **delay line for gates** — ``mode`` ``trigger``, take ``eoc``: a
    trigger in, a trigger out ``rise+fall`` seconds later. Two of these
    chained is a rhythm.

That last one is why ``eor``/``eoc`` are the important jacks. They are the
module's *self-awareness*: it announces when it reaches the top (**end of
rise**) and when it gets back to the bottom (**end of cycle**). On hardware
you patch ``eoc`` back into ``trig`` and it re-fires itself forever — the
"krell" patch. That works here too (since 2026-09-16 any cable that closes
a loop is read one block late — see the feedback notes in MODULES.md):
OR the returning ``eoc`` with a short starter pulse through a [logic]
module into ``trig`` (an input takes one cable, and the loop needs one
poke to begin), and the period is the cycle plus one block.
``examples/krell_feedback.json`` is exactly that. ``loop`` mode is the
same machine with no block of latency and no starter, which is why it is
here; put something wandering into ``rate_cv`` and the pace never
repeats. See ``examples/krell_machine.json``.

``eor`` is the other announcement, and its job is to fire things **at the
peak**: ``eor → pluck.trigger`` strikes a note at the top of a swell,
``eor → sample_hold.trig`` grabs a voltage at the loudest moment, ``eor``
into a *second* function generator's ``trig`` chains an attack into
another shape (a four-stage envelope from two modules), and ``eor`` into
a sequencer's clock is ``eoc``'s clock shifted by the rise time.
``examples/fg_eor_swell_strike.json`` is the first two: a swelling drone
whose every peak fires a pentatonic pluck. One patch that does NOT work,
and is worth knowing why: ``eor`` back into this module's own ``trig``.
The pulse arrives a block after the top, a few milliseconds into the
fall; a retrigger there restarts a rise from ~0.99 that finishes in a
couple of milliseconds and fires ``eor`` again, so the output is pinned
at the top with a block-rate ripple -- and at small block sizes the 2 ms
pulse straddles two blocks, the edge never re-arms, and the loop simply
dies. A latch, not a cycle. The krell is ``eoc``, always.

Three modes, which are three different answers to "what does the gate mean?"

  * ``trigger`` — fire and forget. A rising edge starts the rise; the gate's
    *length is ignored* and the full rise→fall plays out. This is the
    percussion envelope (and the AD envelope's more flexible cousin — it has
    curve and EOR/EOC, which [ad_envelope] does not).
  * ``gate`` — the sustaining envelope. Rise while the gate is high, **hold
    at 1.0** for as long as it stays high, fall when it drops. An AR/ASR: no
    separate sustain level to set, so it is a two-knob envelope with a
    proper hold.
  * ``loop`` — free-running. It cycles forever with no trigger at all; a
    rising edge on ``trig`` is a **reset/sync** instead, restarting the rise
    from wherever the output currently is (click-free, so you can sync it to
    a clock without a glitch on every downbeat).

``curve`` is one bipolar knob that bends both slopes at once. 0 is a straight
line. Positive is **exponential** — a rise that starts slow and accelerates,
and, being the same curve mirrored, a fall that drops fast then trails away.
That is the natural, percussive, plucked shape; +1 is dramatic. Negative is
**logarithmic** — a rise that leaps then eases into the top, and a fall that
lingers before dropping out. That is the swelling, orchestral shape.

``curve_rise`` and ``curve_fall`` (added 2026-09-12) are one knob per slope
on top of that: each is added to ``curve`` for its own slope and the sum
clamped to ±1, so ``curve`` stays the "both" knob and the pair skews it —
the way you'd set a Maths channel and then want the attack snappier
than the decay. Both 0 by default, so nothing moves until you move them.
An exponential rise into a logarithmic fall (``curve_rise`` +, ``curve_fall``
−) is the classic pluck-then-linger; the reverse is a swell that drops
out. When the two effective exponents differ the fall is no longer the
rise mirrored — by design.

``rate_cv`` scales both times together at **1 V/oct on the rate**: +1 makes
the whole function twice as fast, −1 half as fast, clamped to ±5 octaves
(×32 / ÷32). One knob's worth of CV over the whole shape, which is what you
want for a krell patch — put a [shift_random] or an LFO into it and the
machine breathes. For a shallower or offset sweep, put a [cv_scale] /
[cv_offset] in front; there is deliberately no depth param here.

``rise_cv`` and ``fall_cv`` (added 2026-09-14) are the same 1 V/oct law on
one slope each — Maths has a CV jack per slope *and* a "both" jack, and
these are the per-slope pair with ``rate_cv`` as the "both". They **sum in
octaves** with ``rate_cv`` and the total is clamped to ±5, so a patch that
uses only ``rate_cv`` is untouched, and one that uses all three gets the
hardware arithmetic: ``rate_cv`` sets the tempo, ``rise_cv`` / ``fall_cv``
skew it. Velocity into ``fall_cv`` (via [cv_scale]) is the classic — hard
hits ring shorter or longer; an LFO into ``rise_cv`` alone makes a loop-mode
clock whose *period* wanders while its EOC stays put relative to the fall.
All three CVs are block-mean. ``rate_cv`` is always collapsed to mono — one
tempo for the whole rack, as the hardware "both" jack is. ``rise_cv`` and
``fall_cv`` go **per voice** (same day, second pass) when they arrive as
``(V, F)`` alongside a ``(V, F)`` trigger: each slot gets its own stage
lengths, so ``midi_input.velocity_cv → cv_scale → fall_cv`` gives every note
in a chord its own ring. A mono CV, or a voice CV into a mono trigger, is one
law for all (summed across slots, the way any mono module sees a voice
source).

``out_inv`` (same day) is ``1 − out``: it sits at 1, dips to 0 at the end of
the rise and climbs back to 1 by the end of the cycle. Into a VCA that is a
**ducker** — sidechain compression without a compressor, keyed off whatever
is patched into ``trig``; into a filter's cutoff it opens as the function
closes. Bit-exact ``1 − out`` sample for sample, so ``out + out_inv`` is
always 1.0 — ``out`` into one [vca] and ``out_inv`` into another, summed in
a [mixer], is an equal-sum crossfade driven by the function.

Retriggers are click-free everywhere, because every stage entry solves the
curve *backwards* for the position that matches the current output level and
starts from there. A retrigger deep in the fall climbs from where it was; a
gate released mid-rise falls from where it got to.

Voice-awareness:
  Shape-polymorphic, per the house convention, and ``trig`` decides the
  branch. A mono ``(F,)`` (or unpatched) trigger runs one function and emits
  ``(F,)`` on all three outputs; a voice-aware ``(V, F)`` trigger runs V
  independent functions and emits ``(V, F)``, so a polyphonic keyboard gets
  one envelope per note. A voice row is bit-identical to the mono path for
  the same trigger. ``rate_cv`` is collapsed to mono either way — one rate
  for all voices, which is what a "both" CV means on the hardware.

Params:
  * ``mode``: ``"trigger"`` / ``"gate"`` / ``"loop"``. Default ``"trigger"``.
  * ``rise``: seconds, 0…10. 0 = instant. Default 0.05.
  * ``fall``: seconds, 0…10. 0 = instant. Default 0.5.
  * ``curve``: −1 (logarithmic) … 0 (linear) … +1 (exponential).
    Default 0.
  * ``curve_rise`` / ``curve_fall``: per-slope offsets on ``curve``, ±1,
    summed and clamped. Default 0.

Ports:
  * ``trig`` (in, gate): start / sustain / sync, depending on ``mode``.
  * ``rate_cv`` (in, cv): 1 V/oct on the rate; scales rise and fall together.
  * ``rise_cv`` (in, cv): 1 V/oct on the rise rate only; sums with ``rate_cv``.
    Per voice when ``(V, F)`` against a ``(V, F)`` trigger.
  * ``fall_cv`` (in, cv): 1 V/oct on the fall rate only; likewise.
  * ``out`` (out, cv): the function, 0…1.
  * ``out_inv`` (out, cv): ``1 − out``, the same function upside down.
  * ``eor`` (out, gate): a short pulse the moment the rise completes.
  * ``eoc`` (out, gate): a short pulse the moment the fall completes.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Valid ``mode`` values; the UI combo renders these in this order. Anything
# else is treated as ``trigger`` by the renderer.
FUNCTION_GENERATOR_MODES = ("trigger", "gate", "loop")


@register_module_type
class FunctionGenerator(Module):
    """Rise/fall function generator with EOR/EOC gate outs.

    Parameters:
        mode: ``"trigger"`` (fire-and-forget, gate length ignored),
            ``"gate"`` (rise, hold at 1.0 while held, fall on release) or
            ``"loop"`` (free-running; ``trig`` becomes a sync reset).
            Default ``"trigger"``.
        rise: Rise time in seconds, 0…10. 0 = instant. Default 0.05.
        fall: Fall time in seconds, 0…10. 0 = instant. Default 0.5.
        curve: Slope shape, −1 (logarithmic) … 0 (linear) … +1
            (exponential). Bends both slopes. Default 0.
        curve_rise: Added to ``curve`` for the rise only, ±1 (the sum
            is clamped to ±1). Default 0.
        curve_fall: Added to ``curve`` for the fall only, likewise.
            Default 0.

    Ports:
        trig (in, gate): start / sustain / sync — see ``mode``.
        rate_cv (in, cv): 1 V/oct on the rate; +1 = twice as fast.
        rise_cv (in, cv): 1 V/oct on the rise only, summed with ``rate_cv``;
            per voice when ``(V, F)`` against a ``(V, F)`` trigger.
        fall_cv (in, cv): 1 V/oct on the fall only, likewise.
        out (out, cv): the function, 0…1.
        out_inv (out, cv): ``1 − out``.
        eor (out, gate): pulse at end of rise.
        eoc (out, gate): pulse at end of cycle.
    """

    TYPE = "function_generator"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "mode": "trigger",
        "rise": 0.05,
        "fall": 0.5,
        "curve": 0.0,
        "curve_rise": 0.0,
        "curve_fall": 0.0,
    }
    INPUT_PORTS = [
        Port("trig", "in", "gate"),
        Port("rate_cv", "in", "cv"),
        Port("rise_cv", "in", "cv"),
        Port("fall_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "cv"),
        Port("out_inv", "out", "cv"),
        Port("eor", "out", "gate"),
        Port("eoc", "out", "gate"),
    ]
