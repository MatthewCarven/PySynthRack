"""Pure value math for scroll-to-adjust on hovered param widgets.

Kept dpg-free so it unit-tests headless -- the same split as ``ui/zoom.py``
and ``ui/buffer.py``. One mouse-wheel notch nudges a numeric param by 1% of
its range (Shift -> 10%); integer params step by 1 (Shift -> 10). The wheel
handler in ``ui/app.py`` owns the dpg glue -- finding the hovered widget,
reading its min/max, writing the value back -- while every numeric decision
lives here where it can be tested.
"""
from __future__ import annotations


def nudge_number(value, direction, *, min_value, max_value,
                 is_int=False, coarse=False):
    """``value`` nudged one wheel notch in ``direction`` (>0 up, <=0 down).

    Float step is 1/100 of ``|max_value - min_value|`` (1/10 when ``coarse``);
    integer step is 1 (10 when ``coarse``). The result is clamped to the range
    regardless of which way the bounds are ordered, and rounded for ints. A
    degenerate (zero-width) range returns the clamped input unchanged.
    """
    d = 1.0 if direction > 0 else -1.0
    lo = float(min(min_value, max_value))
    hi = float(max(min_value, max_value))
    if is_int:
        step = 10.0 if coarse else 1.0
        new = round(float(value)) + d * step
    else:
        mag = (hi - lo) / (10.0 if coarse else 100.0)
        new = float(value) + d * mag
    new = min(max(new, lo), hi)
    return int(round(new)) if is_int else float(new)


def cycle_index(index, direction, count):
    """Next (>0) / previous (<=0) option index for a combo, clamped to
    ``[0, count - 1]``.

    No wrap-around -- a wheel that silently looped past the ends would be
    disorienting. Returns 0 for an empty option list.
    """
    if count <= 0:
        return 0
    d = 1 if direction > 0 else -1
    return min(max(int(index) + d, 0), count - 1)
