"""Loudness — an equal-loudness "contour" control (loudness compensation).

The ear goes progressively deaf to bass and treble as things get quieter
(the equal-loudness contours: Fletcher–Munson / ISO 226). A hi-fi
"loudness" button compensates by boosting the low and high ends as you
turn the level down, so a quiet mix still sounds full instead of thin and
mid-heavy. This module is that control, plus manual bass/treble trims on
top for taste.

``level`` is the listening level: at 1.0 the response is flat, and as you
lower it the module blooms the bass (a low shelf) and treble (a high
shelf), bass more than treble, following the shape of the equal-loudness
curves. ``bass`` and ``treble`` add fixed shelf trims in dB regardless of
level — a manual tilt on top of the automatic compensation. A ``level_cv``
input modulates the level, so an envelope or LFO can open the contour up
as a sound fades.

It reshapes the *frequency balance* of whatever you feed it — it is not an
envelope sweeping a filter over time (that's an ADSR into a Filter's
``cutoff_cv``); it's a static, level-dependent EQ curve.

Use cases:
  * A master "loudness" on the output bus so quiet patches stay full.
  * Fatten a thin oscillator or the mic input with a touch of low + high
    shelf.
  * Automate the contour with an envelope on ``level_cv`` for a sound that
    warms as it decays.

Ports:
  * ``in`` (audio): the signal to shape. Unpatched -> silence.
  * ``level_cv`` (cv): added to ``level``, scaled by ``cv_depth``.
    Optional.
  * ``out`` (audio): the contoured signal.

Voice-awareness:
  Shape-polymorphic like Filter / ParametricEQ. A mono ``(F,)`` input runs
  one low+high shelf cascade; a ``(V, F)`` input runs V parallel cascades
  (one biquad memory per voice slot). The contour amount is a single
  global control (a ``(V, F)`` ``level_cv`` is averaged), so every voice
  shares the same curve; a single voice row is bit-identical to mono.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Loudness(Module):
    """Equal-loudness contour / loudness-compensation shelving EQ.

    Parameters:
        level: Listening level, 1.0 = flat, lower = more bass/treble
            compensation (0..1).
        bass: Manual low-shelf trim in dB, added on top of the auto
            curve.
        treble: Manual high-shelf trim in dB, added on top.
        cv_depth: Level change per unit of ``level_cv``.

    Ports:
        in (in, audio): signal to shape. Unpatched -> silence.
        level_cv (in, cv): added to ``level``, scaled by ``cv_depth``.
        out (out, audio): the contoured signal.
    """

    TYPE = "loudness"
    DEFAULT_PARAMS = {
        "level": 0.5,
        "bass": 0.0,
        "treble": 0.0,
        "cv_depth": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("level_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
