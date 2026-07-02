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

Ports:
  * ``in`` (cv): the signal to sample. Unpatched is treated as 0, so
    an unpatched SampleHold simply holds 0 (pure S&H — no internal
    noise source; that's the Noise generator's job once it lands).
  * ``trig`` (gate): the clock. Each rising edge (crossing the
    backend's gate threshold upward) takes one sample. Unpatched means
    no triggers, so the output holds its last value (0 at startup).
  * ``out`` (cv): the held value.

No params — it's purely structural. (A ``slew``/glide to smooth the
steps is an easy follow-up.)

Voice-awareness:
  Shape-polymorphic on its inputs, per the v0.4 convention. Mono
  ``(F,)`` in/trig -> mono ``(F,)`` out with a scalar held value and
  scalar held-gate carried across blocks. A voice-aware ``(V, F)`` on
  either input -> ``(V, F)`` out with per-voice held values and
  per-voice edge detection; a mono partner broadcasts across the voice
  axis (a shared clock sampling per-voice sources, or per-voice clocks
  sampling one shared source).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class SampleHold(Module):
    """Sample ``in`` on each rising edge of ``trig``; hold until the next.

    No parameters.

    Ports:
        in (in, cv): signal to sample. Unpatched is treated as 0.
        trig (in, gate): clock; samples on each rising edge.
        out (out, cv): the held value.
    """

    TYPE = "sample_hold"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS: dict = {}
    INPUT_PORTS = [
        Port("in", "in", "cv"),
        Port("trig", "in", "gate"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "cv")]
