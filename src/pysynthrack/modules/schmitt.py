"""Schmitt module — CV threshold crossing emits a gate, with hysteresis.

The last of the three signal-kind bridges (after :class:`AudioToCV`
and :class:`CVToAudio`): it crosses the cv → gate wall, so any CV
signal can *trigger* things instead of merely modulating them.

A plain comparator (gate = cv > threshold) chatters: any noise or
slow wobble around the threshold flips the gate dozens of times. The
Schmitt trigger's classic fix is two thresholds — the gate goes high
only when the CV rises above ``high`` and goes low only when it falls
below ``low``. Inside the band the gate *holds*. The (high - low) gap
is the hysteresis, and it's what makes an LFO usable as a clock.

Use cases:
  * LFO clock: ``lfo.cv → schmitt.in → adsr.gate`` retriggers an
    envelope every LFO cycle — self-playing rhythmic patches with no
    keyboard involved.
  * Envelope chaining: one ADSR's CV output (via a CVCombiner if it's
    also modulating something) trips a second envelope partway
    through its travel — staged, sequenced movement.
  * Follower-to-trigger: ``audio_to_cv → schmitt`` turns "the bass
    got loud" into a clean gate edge (a crude beat detector).

Params:
  * ``high``: rising threshold. The gate sets when CV exceeds this
    (strict >). Default 0.6.
  * ``low``: falling threshold. The gate clears when CV drops below
    this (strict <). Default 0.4. Effectively clamped to
    ``min(low, high)`` — an inverted pair degenerates to a plain
    comparator at ``high`` rather than doing anything surprising.

Voice-awareness:
  Shape-polymorphic on the CV input's ``ndim``, per the v0.4
  convention: a 1D ``(F,)`` CV yields a 1D gate with scalar held
  state; a ``(V, F)`` CV yields a ``(V, F)`` gate with per-voice held
  state — so a per-voice envelope bank can clock per-voice triggers.
  Unpatched input emits a constant-low gate.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Schmitt(Module):
    """Schmitt trigger: CV in → gate out, with hysteresis.

    Parameters:
        high: Rising threshold; gate sets when CV exceeds it
            (strict >). Default 0.6.
        low: Falling threshold; gate clears when CV drops below it
            (strict <). Default 0.4. Values above ``high`` are
            treated as ``high`` (plain comparator).

    Ports:
        in (in, cv): the CV to threshold.
        gate (out, gate): 0.0 / 1.0 gate signal.
    """

    TYPE = "schmitt"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {
        "high": 0.6,
        "low": 0.4,
    }
    INPUT_PORTS = [Port("in", "in", "cv")]
    OUTPUT_PORTS = [Port("gate", "out", "gate")]
