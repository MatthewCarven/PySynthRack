"""Pure value math for scroll-to-adjust on hovered param widgets.

Kept dpg-free so it unit-tests headless -- the ui/zoom.py + ui/buffer.py split.
One mouse-wheel notch bumps a param by the least-significant digit its readout
shows, snapped to a "nice" size near 1% of the range (Shift -> x10 coarse);
integers step by 1 (Shift -> 10). The app.py wheel handler owns the dpg glue --
finding the hovered widget, reading its min/max/format, writing the value back
-- while every numeric decision lives here where it can be tested.
"""
from __future__ import annotations

import math
import re

# The precision field of a printf float format: "%.2f st" -> 2, "%.0f Hz" -> 0.
_FMT_PREC = re.compile(r"%[#0\- +]*\d*\.(\d+)f")


def decimals_from_format(fmt):
    """Decimal places of a printf float ``fmt`` ("%.2f st" -> 2, "%.0f Hz" ->
    0), or None when there is no ``%.Nf`` field (int slider, text, no format).
    """
    if not fmt:
        return None
    m = _FMT_PREC.search(str(fmt))
    return int(m.group(1)) if m else None


def scroll_step(min_value, max_value, decimals):
    """Per-notch step for a float widget: the largest power-of-ten multiple of
    the displayed precision (``10 ** -decimals``) that stays within ~1% of the
    range, and never finer than one displayed digit.

    So a notch moves the digit you can actually see -- 0.01 on a "%.2f" 0..1
    mix *and* on a "%.2f" 0.05..10 LFO rate, 100 Hz on a "%.0f" 20..20000
    cutoff, 0.1 st on a "%.2f" -24..24 semitone -- instead of a blunt 1% that
    is 0.1 on the narrow ones and enormous on the wide ones.
    """
    prec = 10.0 ** (-int(decimals))
    span = abs(float(max_value) - float(min_value))
    onepct = span / 100.0
    if onepct <= prec:
        return prec
    k = math.floor(math.log10(onepct / prec))
    return prec * (10.0 ** max(0, k))


def nudge_number(value, direction, *, min_value, max_value,
                 is_int=False, coarse=False, decimals=None):
    """``value`` nudged one wheel notch in ``direction`` (>0 up, <=0 down).

    A float widget with a known ``decimals`` (its readout precision) steps by
    :func:`scroll_step` -- the displayed-digit size -- and the result is
    rounded to that precision so it matches what the readout shows. Without
    ``decimals`` (no parseable format) it falls back to 1% of the range.
    Integers step by 1. ``coarse`` (Shift) multiplies the step by 10. The
    result is clamped to the range regardless of bound order.
    """
    d = 1.0 if direction > 0 else -1.0
    lo = float(min(min_value, max_value))
    hi = float(max(min_value, max_value))
    if is_int:
        new = round(float(value)) + d * (10.0 if coarse else 1.0)
        return int(min(max(new, lo), hi))
    if decimals is None:
        step = (hi - lo) / 100.0
        new = float(value) + d * step * (10.0 if coarse else 1.0)
        return float(min(max(new, lo), hi))
    step = scroll_step(lo, hi, decimals) * (10.0 if coarse else 1.0)
    new = min(max(float(value) + d * step, lo), hi)
    return round(new, max(0, int(decimals)))


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
