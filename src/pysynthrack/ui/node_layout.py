"""Pure helpers for placing a freshly-added node so it doesn't overlap.

DearPyGui's node editor is a thin wrapper over the C library *imnodes*, and
imnodes only begins a title-bar drag when ImGui reports *no widget* hovered at
the click point — it deliberately yields the mouse to widgets so you can grab a
slider near a node's edge. A node's title bar is not an ImGui widget (imnodes
draws and hit-tests it itself), so when one node sits on top of another the
lower node's slider occupies the same pixels as the upper node's title bar,
ImGui reports that slider hovered, and imnodes yields — the click adjusts the
slider instead of dragging the node. There is no DearPyGui knob to make a bare
title bar win over an overlapping widget; the only reliable cure is spatial:
never drop a new node on top of an existing one.

Auto-placement used to cascade every new node ~60px down — far less than a
node's height — so each newcomer landed almost entirely on its predecessor.
``find_free_position`` replaces that with a scan for a genuinely clear slot.

This module holds the dpg-free maths so it can be unit-tested without a
graphics context; the DearPyGui glue (reading each existing node's rect,
un-zooming it, and creating the node) lives in ``app.py``.
"""
from __future__ import annotations

from typing import Iterable, Sequence, Tuple

Rect = Tuple[float, float, float, float]  # (x, y, w, h)
Point = Tuple[float, float]

# A new node hasn't rendered yet, so its true size is unknown at placement
# time. This nominal (logical-pixel) size is used for the clearance test; a
# slight over-estimate is safe — it just reserves a touch more room.
DEFAULT_NODE_SIZE: Point = (180.0, 200.0)

# Where the fallback scan starts and how it steps. The grid is fine enough to
# tuck a node into a modest gap, coarse enough to stay cheap. ``GRID_BOUNDS``
# only caps the search (the imnodes canvas itself scrolls without limit).
GRID_ORIGIN: Point = (40.0, 40.0)
GRID_STEP: Point = (40.0, 40.0)
GRID_BOUNDS: Point = (1600.0, 1000.0)

# Required empty gap between two nodes, in logical pixels.
MARGIN: float = 12.0


def rects_overlap(a: Rect, b: Rect, margin: float = 0.0) -> bool:
    """True if rects ``a`` and ``b`` are closer than ``margin`` on both axes.

    Each rect is ``(x, y, w, h)`` with y growing downward. ``margin`` is the
    gap that must separate them to count as clear: with ``margin=0`` two rects
    that merely touch edge-to-edge do *not* overlap.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (
        ax < bx + bw + margin
        and bx < ax + aw + margin
        and ay < by + bh + margin
        and by < ay + ah + margin
    )


def find_free_position(
    existing: Iterable[Sequence[float]],
    preferred: Sequence[float],
    *,
    node_size: Point = DEFAULT_NODE_SIZE,
    grid_origin: Point = GRID_ORIGIN,
    grid_step: Point = GRID_STEP,
    grid_bounds: Point = GRID_BOUNDS,
    margin: float = MARGIN,
) -> Point:
    """Pick a top-left for a new node that clears every existing node.

    ``existing`` is the current node rects ``(x, y, w, h)`` in the same
    (logical) space as ``preferred``; rects with a non-positive size (e.g. a
    sibling that hasn't rendered yet, reported as 0×0) are ignored. ``preferred``
    is the spot the caller would like — tried first, so an already-clear cascade
    position is honoured unchanged. On a collision the function scans a grid and
    returns the first clear slot; if the canvas is so full that nothing on the
    grid clears, it falls back to ``preferred`` (overlap is then unavoidable).
    """
    nw, nh = float(node_size[0]), float(node_size[1])
    rects = [
        (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
        for r in existing
        if float(r[2]) > 0.0 and float(r[3]) > 0.0
    ]

    def is_free(x: float, y: float) -> bool:
        cand: Rect = (x, y, nw, nh)
        return all(not rects_overlap(cand, r, margin) for r in rects)

    px, py = float(preferred[0]), float(preferred[1])
    if is_free(px, py):
        return (px, py)

    ox, oy = grid_origin
    sx, sy = grid_step
    bw, bh = grid_bounds
    cols = int(bw // sx) + 1
    rows = int(bh // sy) + 1
    for r in range(rows):
        y = oy + r * sy
        for c in range(cols):
            x = ox + c * sx
            if is_free(x, y):
                return (x, y)
    return (px, py)
