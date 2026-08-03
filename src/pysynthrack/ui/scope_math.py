"""Scope maths — window sizing, trigger search, min/max decimation, geometry.

Everything the Scope node needs that is *not* dearpygui lives here (the
zoom.py precedent), so the whole display pipeline tests headless: the
backend's snapshot hook calls these on the GUI thread, and the node's
drawlist just plots the points it gets back.

The display is a classic 10-division scope face: the visible window is
``time_div`` (ms per division) × 10. Each pixel column shows the min and
max of its slice of the window, so a one-sample spike can never fall
between pixels — the industry decimation for waveform displays.
"""
from __future__ import annotations

import numpy as np

# Horizontal divisions on the scope face (the classic 10).
DIVISIONS = 10


def window_samples(time_div_ms: float, sample_rate: float) -> int:
    """Visible-window length in samples for a ms-per-division setting."""
    time_div_ms = max(0.001, float(time_div_ms))
    return max(2, int(round(time_div_ms * 1e-3 * DIVISIONS * sample_rate)))


def find_trigger_index(
    x: np.ndarray, mode: str, level: float, window: int
) -> int | None:
    """Start index of the trigger-aligned window, or None (no crossing).

    Searches for the LAST ``level`` crossing in ``x`` that still leaves a
    full ``window`` of samples after it, so the display shows the most
    recent aligned sweep. ``rising`` = crossing upward through the level,
    ``falling`` = downward; anything else means free-run (caller shows the
    end-aligned window instead).
    """
    if mode not in ("rising", "falling"):
        return None
    n = len(x)
    if n < window + 2:
        return None
    search = x[: n - window + 1]
    a, b = search[:-1], search[1:]
    if mode == "rising":
        hits = np.flatnonzero((a < level) & (b >= level))
    else:
        hits = np.flatnonzero((a > level) & (b <= level))
    if len(hits) == 0:
        return None
    return int(hits[-1]) + 1  # first sample at/past the level


def minmax_columns(x: np.ndarray, columns: int) -> np.ndarray:
    """Decimate ``x`` to ``(columns, 2)`` per-column ``[min, max]`` pairs.

    Every input sample lands in exactly one column, so a single-sample
    spike always survives into its column's min or max. Fewer samples
    than columns simply repeats samples (zoomed-in staircase) — the
    column count is always exactly ``columns``.
    """
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return np.zeros((columns, 2), dtype=np.float32)
    # Column index of every sample: floor(i * columns / n) — monotone,
    # gap-free, exact for both n >= columns and n < columns.
    n = x.size
    idx = (np.arange(n, dtype=np.int64) * columns) // n
    out = np.empty((columns, 2), dtype=np.float32)
    mins = np.full(columns, np.inf, dtype=np.float64)
    maxs = np.full(columns, -np.inf, dtype=np.float64)
    np.minimum.at(mins, idx, x)
    np.maximum.at(maxs, idx, x)
    # Columns that received no sample (n < columns) inherit their left
    # neighbour (zero-order hold).
    hit = np.flatnonzero(np.isfinite(mins))
    if len(hit) < columns:
        fill = np.zeros(columns, dtype=np.int64)
        fill[hit] = hit
        np.maximum.accumulate(fill, out=fill)
        mins = mins[fill]
        maxs = maxs[fill]
    out[:, 0] = mins
    out[:, 1] = maxs
    return out


def decimate_points(x: np.ndarray, points: int) -> np.ndarray:
    """Plain stride decimation to ~``points`` samples (for XY mode)."""
    x = np.asarray(x, dtype=np.float32)
    if x.size <= points:
        return x.copy()
    idx = np.linspace(0, x.size - 1, points).astype(np.int64)
    return x[idx]


def envelope_polyline(
    cols: np.ndarray, width: float, height: float, gain: float
) -> list[tuple[float, float]]:
    """Column min/max pairs → one zig-zag polyline in drawlist pixels.

    Each column contributes its max then its min at the same x, so the
    single polyline paints the full vertical extent of every column
    (spikes included). y maps +1 (× gain) to the top, −1 to the bottom,
    clamped to the face.
    """
    columns = len(cols)
    if columns == 0:
        return []
    g = float(gain) if gain > 0 else 1.0
    xs = (np.arange(columns, dtype=np.float64) + 0.5) * (width / columns)
    half = height / 2.0

    def to_y(vals: np.ndarray) -> np.ndarray:
        y = half - np.clip(vals * g, -1.0, 1.0) * (half - 1.0)
        return y

    y_max = to_y(cols[:, 1].astype(np.float64))
    y_min = to_y(cols[:, 0].astype(np.float64))
    pts: list[tuple[float, float]] = []
    for i in range(columns):
        pts.append((float(xs[i]), float(y_max[i])))
        if y_min[i] != y_max[i]:
            pts.append((float(xs[i]), float(y_min[i])))
    return pts


def request_samples(time_div_ms: float, sample_rate: float) -> int:
    """How many ring samples a snapshot wants: one window + search slack.

    The slack (the larger of one window or one second) is where the
    trigger search looks for the last crossing that still fits a full
    window after it.
    """
    window = window_samples(time_div_ms, sample_rate)
    return window + max(window, int(sample_rate))


def build_snapshot(
    win: dict | None, params: dict, sample_rate: float, columns: int
) -> dict | None:
    """Turn a backend ``scope_window`` dict + module params into columns.

    Returns ``{"mode", "cols1", "cols2", "xy1", "xy2"}`` — min/max column
    arrays for mono/dual (``cols2`` None when there is no second trace),
    decimated point arrays for xy — or None when nothing is patched yet.
    Alignment: an external ``trig`` capture (``tg``) overrides the level
    trigger (its last rising edge that fits a window); otherwise the
    ``trigger``/``level`` params run on the main trace; free-run (or no
    crossing found) shows the newest window, end-aligned.
    """
    if win is None:
        return None
    t1, t2, tg = win.get("t1"), win.get("t2"), win.get("tg")
    base = t1 if t1 is not None else t2
    if base is None:
        return None

    mode = str(params.get("mode", "mono"))
    if mode not in ("mono", "dual", "xy"):
        mode = "mono"
    window = min(window_samples(float(params.get("time_div", 10.0)),
                                sample_rate), len(base))

    if tg is not None:
        start = find_trigger_index(tg, "rising", 0.5, window)
    else:
        start = find_trigger_index(
            base,
            str(params.get("trigger", "rising")),
            float(params.get("level", 0.0)),
            window,
        )
    if start is None:
        start = len(base) - window

    def sl(trace):
        if trace is None:
            return None
        return trace[start : start + window]

    s1, s2 = sl(t1), sl(t2)
    if mode == "xy":
        return {
            "mode": mode,
            "cols1": None,
            "cols2": None,
            "xy1": decimate_points(s1 if s1 is not None else np.zeros(1), columns),
            "xy2": decimate_points(s2, columns) if s2 is not None else None,
        }
    return {
        "mode": mode,
        "cols1": minmax_columns(s1, columns) if s1 is not None else None,
        "cols2": minmax_columns(s2, columns)
        if (s2 is not None and mode == "dual")
        else None,
        "xy1": None,
        "xy2": None,
    }


def xy_polyline(
    x: np.ndarray, y: np.ndarray, width: float, height: float, gain: float
) -> list[tuple[float, float]]:
    """Two decimated traces → goniometer points in drawlist pixels."""
    n = min(len(x), len(y))
    if n == 0:
        return []
    g = float(gain) if gain > 0 else 1.0
    half_w, half_h = width / 2.0, height / 2.0
    px = half_w + np.clip(x[:n] * g, -1.0, 1.0) * (half_w - 1.0)
    py = half_h - np.clip(y[:n] * g, -1.0, 1.0) * (half_h - 1.0)
    return [(float(a), float(b)) for a, b in zip(px, py)]
