"""Pure helpers for the node-editor UI scale ("zoom") factor.

DearPyGui's node editor is a thin wrapper over the C library *imnodes*,
which has never implemented real canvas zoom (it is imnodes' single
most-requested feature, open upstream since 2020). So PySynthRack fakes a
zoom the only way the toolkit allows from Python:

* ``dpg.set_global_font_scale`` scales all text — and because nodes
  auto-size to their contents, every node grows or shrinks with it; and
* each node's position is multiplied by the same factor about the editor
  origin, so the relative layout (and therefore the cable lengths) tracks
  the size instead of overlapping on zoom-in or scattering on zoom-out.

This module holds the dpg-free maths so it can be unit-tested without a
graphics context. The DearPyGui glue lives in ``app.py``.
"""
from __future__ import annotations

# Zoom is a multiplier on the natural (100 %) size. The bounds keep text
# legible at the small end and stop nodes flying off-canvas at the large
# end; they also define the toolbar slider's range (percent = factor*100).
ZOOM_MIN = 0.25
ZOOM_MAX = 3.0
ZOOM_DEFAULT = 1.0

# Multiplicative step per Ctrl+= / Ctrl+- press or wheel notch. A geometric
# step feels even across the whole range — each press is the same
# *proportional* change — and the in/out steps are exact inverses, so
# stepping out then in (or vice versa) lands back where you started.
ZOOM_STEP = 1.1


def clamp_zoom(z: float) -> float:
    """Clamp a zoom factor into the supported [ZOOM_MIN, ZOOM_MAX] range."""
    if z < ZOOM_MIN:
        return ZOOM_MIN
    if z > ZOOM_MAX:
        return ZOOM_MAX
    return z


def step_zoom(z: float, direction: int, step: float = ZOOM_STEP) -> float:
    """Return ``z`` moved one notch and clamped.

    ``direction`` > 0 zooms in (×step), < 0 zooms out (÷step), 0 leaves the
    factor unchanged (still clamped into range).
    """
    if direction > 0:
        return clamp_zoom(z * step)
    if direction < 0:
        return clamp_zoom(z / step)
    return clamp_zoom(z)


def scale_pos(pos, ratio: float):
    """Scale an ``(x, y)`` position by ``ratio`` about the origin.

    Accepts any 2-element sequence and returns a ``(float, float)`` tuple.
    """
    return (float(pos[0]) * ratio, float(pos[1]) * ratio)


def factor_to_percent(z: float) -> int:
    """Zoom factor -> integer percent for the toolbar slider / readout."""
    return int(round(z * 100.0))


def percent_to_factor(pct: float) -> float:
    """Integer percent (from the slider) -> clamped zoom factor."""
    return clamp_zoom(pct / 100.0)
