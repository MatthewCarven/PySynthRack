"""SampleHold — sample a CV on a trigger edge and hold it until the next.

The classic modular sample-and-hold. On each rising edge of the
``trig`` gate it grabs the instantaneous value of ``in`` and holds that
value steady on ``out`` until the next trigger. Between triggers the
output is a flat plateau — a staircase whose steps land on the clock.

It's a *quantiser of time*, not of pitch: it turns a continuous signal
into discrete held steps. What you get depends on what you feed it:

  * Stepped/random melodies: feed a wandering source (an LFO, or a fast
    ``random`` LFO as a stand-in for noise) into ``in`` and a steady
    clock into ``trig`` -> a new held voltage every clock tick. Route
    ``out`` through :class:`CVScale`/:class:`CVOffset` into a
    :class:`CVToFrequency` for self-playing arpeggios.
  * Decimate a modulator: sampling a slow LFO at a faster clock
    "stair-steps" it; sampling a fast source at a slow clock thins it
    out.
  * Track-and-grab: freeze an envelope or follower value at a moment of
    your choosing.

The trigger is a ``gate`` input, so anything that emits gates clocks
it: :class:`Schmitt` (turn any LFO/CV into a clock), a Keyboard/MIDI
gate, or an ADSR gate. Pair an LFO -> Schmitt -> SampleHold for a
tempo'd clock with no keyboard involved.

Love pass (2026-09-19) — three knobs, every one OFF at its default so
a patch saved before today renders bit-identically:

  * ``mode`` — ``sample`` is the above. ``track`` is the *other*
    classic S&H, track-and-hold: while ``trig`` is HIGH the output
    FOLLOWS ``in``, and on the falling edge it holds the last value it
    saw. A long-pulse clock into track mode is "ride the LFO for the
    first half of every step, then freeze its tail" — the width of the
    gate is now a parameter of the sound (``clock.pulse_width``, or a
    ``function_generator`` whose ``eoc`` is not what you want). The
    same forward-fill as sample mode, keyed on the last HIGH sample
    instead of the last EDGE.
  * ``prob`` — the chance, 0..1, that a rising edge actually samples;
    a losing edge is simply ignored and the old value keeps holding.
    A "sometimes" S&H: a random melody that repeats notes, a clock
    that grabs a new filter setting only now and then. The die is
    ``seed``'s own private generator (the ``bernoulli_gate``
    convention), thrown ONCE PER EDGE and only when 0 < ``prob`` < 1
    — at 1 there is nothing to decide and no draw is consumed, so the
    default is literally today's render; at 0 nothing ever samples.
    Changing ``seed`` live re-rolls. In ``track`` mode the die is
    thrown at the window's rising edge and decides the WHOLE window: a
    losing window is skipped entirely and the output holds straight
    through it.
  * ``glide`` — seconds of one-pole lag on the output, the
    :class:`Slew` module's premise (99% of a step in ``glide`` seconds)
    without the second module: portamento between the held values, a
    stepped LFO turned into a smooth wander. In ``track`` mode the lag
    sits on the followed signal too (it is a lag on ``out``, wherever
    ``out`` came from). 0 is a straight wire — no filter runs.

Ports:
  * ``in`` (cv): the signal to sample. Unpatched is treated as 0, so
    an unpatched SampleHold simply holds 0 (pure S&H — no internal
    noise source; that's the Noise generator's job).
  * ``trig`` (gate): the clock. Each rising edge (crossing the
    backend's gate threshold upward) takes one sample. Unpatched means
    no triggers, so the output holds its last value (0 at startup).
  * ``out`` (cv): the held value.

Params:
  * ``mode``: ``sample`` / ``track``. Default ``sample``.
  * ``prob``: chance an edge samples, 0..1. Default 1.
  * ``seed``: the die for ``prob`` (non-negative int). Default 1.
  * ``glide``: output lag, seconds to 99% of a step. Default 0.

Voice-awareness:
  Shape-polymorphic on its inputs, per the v0.4 convention. Mono
  ``(F,)`` in/trig -> mono ``(F,)`` out with a scalar held value and
  scalar held-gate carried across blocks. A voice-aware ``(V, F)`` on
  either input -> ``(V, F)`` out with per-voice held values and
  per-voice edge detection; a mono partner broadcasts across the voice
  axis (a shared clock sampling per-voice sources, or per-voice clocks
  sampling one shared source). Every knob is per voice: ``prob`` throws
  one die per edge PER VOICE (a shared clock into four voices is four
  independent decisions — a chord where each note sometimes holds),
  drawn in time order and voice order within a sample so a block split
  never reorders them; ``glide`` carries one lag per voice.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

SAMPLE_HOLD_MODES = ("sample", "track")


@register_module_type
class SampleHold(Module):
    """Sample ``in`` on each rising edge of ``trig``; hold until the next.

    Parameters:
        mode: ``sample`` (grab at the rising edge, hold) or ``track``
            (follow while high, hold the last value at the fall).
            Default ``sample``.
        prob: Chance, 0..1, that a rising edge samples (a losing edge
            keeps the old value; in ``track`` mode it skips the whole
            window). Default 1.0 — every edge, no draws.
        seed: RNG seed for ``prob``. Default 1.
        glide: One-pole lag on the output, seconds to 99% of a step;
            0 = none. Default 0.0.

    Ports:
        in (in, cv): signal to sample. Unpatched is treated as 0.
        trig (in, gate): clock; samples on each rising edge.
        out (out, cv): the held value.
    """

    TYPE = "sample_hold"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS: dict = {
        "mode": "sample",
        "prob": 1.0,
        "seed": 1,
        "glide": 0.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "cv"),
        Port("trig", "in", "gate"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "cv")]
