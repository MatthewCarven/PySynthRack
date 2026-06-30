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
  * Rhythmic echoes — set ``time`` to a note value of your clock by ear,
    or wobble ``time_cv`` from an LFO for tape flutter.

Ports:
  * ``in`` (audio): the signal to echo. Unpatched -> silence out.
  * ``time_cv`` (cv): added to ``time``, scaled by ``cv_depth``.
    Optional; unpatched means no modulation.
  * ``out`` (audio): the dry+echo mix.

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` audio in
  -> mono ``(F,)`` out through one delay line. A voice-aware ``(V, F)``
  audio in -> ``(V, F)`` out with one delay line per voice slot, so a
  polyphonic source upstream echoes without cross-talk. A mono
  ``time_cv`` broadcasts across voices; a ``(V, F)`` ``time_cv`` drives
  each voice independently. A single voice row is bit-identical to the
  mono path.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Delay(Module):
    """Analog-voiced feedback delay (echo) with a damped feedback path.

    Parameters:
        time: Base delay time in milliseconds (1..2000).
        feedback: Fraction of the delayed signal fed back (0 = one
            repeat; clamped just below 1 to stay stable).
        tone: Damping in the feedback path, 0..1. Low = dark repeats
            (high end rolled off each pass), high = bright/faithful.
        mix: Dry/wet balance, 0 (dry only) .. 1 (wet only).
        cv_depth: Milliseconds of delay time per unit of ``time_cv``.

    Ports:
        in (in, audio): signal to echo. Unpatched -> silence.
        time_cv (in, cv): added to ``time``, scaled by ``cv_depth``.
        out (out, audio): dry + echo mix.
    """

    TYPE = "delay"
    DEFAULT_PARAMS = {
        "time": 300.0,
        "feedback": 0.4,
        "tone": 0.5,
        "mix": 0.35,
        "cv_depth": 50.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("time_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
