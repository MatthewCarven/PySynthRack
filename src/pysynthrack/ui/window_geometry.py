"""Pure helpers for per-patch window geometry restore.

A patch may carry the editor window's size and position under
``patch.ui["window"]`` so reopening it restores the same window. The dpg-free
maths lives here (unit-tested without a graphics context); the DearPyGui and
Win32 glue — reading/setting the viewport and querying the virtual-desktop
bounds — lives in ``app.py``.

The one real hazard is a *stale* saved position: coordinates saved on a second
monitor (or a larger screen) that no longer exists would place the window
off-screen and unreachable. ``resolve`` guards against that by clamping the
window fully inside the current desktop bounds; when the bounds are unknown
(non-Windows, or the query failed) it restores the size but leaves the
position alone, since a blind position is the dangerous half.

Note: DearPyGui 2.3.1 exposes no maximized-state query (no ``is_viewport_
maximized`` and no ``maximized`` key in ``get_viewport_configuration``), so
only size and position are captured. A window that was maximized restores to
that same full size and position — visually near-identical, just not a true
OS-maximized state.
"""
from __future__ import annotations

from typing import Any, Optional

# Sane floor so a corrupt/tiny saved size can't produce an unusable window.
MIN_W = 320
MIN_H = 240


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clamp(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def make_geometry(width: Any, height: Any, x: Any, y: Any) -> Optional[dict]:
    """Build the serializable ``patch.ui["window"]`` dict from raw values.

    Returns ``None`` if width/height aren't usable numbers (nothing worth
    saving). An x/y that doesn't parse is stored as ``None`` — the size is
    still saved.
    """
    w = _as_int(width)
    h = _as_int(height)
    if w is None or h is None:
        return None
    return {"width": w, "height": h, "x": _as_int(x), "y": _as_int(y)}


def resolve(saved: Any, screen: Optional[tuple]) -> Optional[dict]:
    """Resolve saved geometry against the current desktop bounds.

    ``saved`` is a ``patch.ui["window"]`` dict (or junk). ``screen`` is the
    virtual-desktop rect ``(x, y, w, h)`` spanning all monitors, or ``None`` if
    unknown. Returns ``{"width","height","x","y"}`` to apply — ``x``/``y`` may
    be ``None`` meaning "leave the window where it is" — or ``None`` when
    there's no usable geometry to apply at all.
    """
    if not isinstance(saved, dict):
        return None
    w = _as_int(saved.get("width"))
    h = _as_int(saved.get("height"))
    if w is None or h is None:
        return None
    w = max(MIN_W, w)
    h = max(MIN_H, h)
    x = _as_int(saved.get("x"))
    y = _as_int(saved.get("y"))

    if screen is None:
        # Bounds unknown: restore the size, but never a blind position.
        return {"width": w, "height": h, "x": None, "y": None}

    sx, sy, sw, sh = screen
    # A window can't be wider/taller than the whole desktop.
    w = min(w, sw)
    h = min(h, sh)
    if x is None or y is None:
        return {"width": w, "height": h, "x": None, "y": None}
    # Clamp the whole window inside the desktop, so a stale off-screen
    # coordinate is pulled back into view rather than lost.
    x = _clamp(x, sx, sx + sw - w)
    y = _clamp(y, sy, sy + sh - h)
    return {"width": w, "height": h, "x": x, "y": y}
