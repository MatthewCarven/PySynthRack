"""Unit tests for the dpg-free new-node placement maths.

Covers the overlap-avoidance that keeps a freshly-added node off the top of an
existing one (see ``node_layout`` for why the overlap causes a click-through
into the lower node's slider). Imports no DearPyGui, so it runs in the normal
suite; the app.py glue that reads real node rects gets a manual eyeball.
"""

from pysynthrack.ui import node_layout as nl
from pysynthrack.ui.node_layout import find_free_position, rects_overlap


# ----- rects_overlap ------------------------------------------------------

def test_overlap_true_when_nested():
    assert rects_overlap((0, 0, 100, 100), (10, 10, 20, 20))


def test_overlap_false_when_apart():
    assert not rects_overlap((0, 0, 100, 100), (200, 200, 50, 50))


def test_touching_edges_not_overlap_without_margin():
    # b starts exactly where a ends on x; zero-gap touch is not an overlap.
    assert not rects_overlap((0, 0, 100, 100), (100, 0, 50, 100))


def test_touching_edges_overlap_with_margin():
    # The same touch is "too close" once a gap is required.
    assert rects_overlap((0, 0, 100, 100), (100, 0, 50, 100), margin=12)


def test_margin_gap_exactly_clear():
    # b sits a full margin to the right of a -> just clear.
    assert not rects_overlap((0, 0, 100, 100), (112, 0, 50, 100), margin=12)
    # one pixel closer -> too close.
    assert rects_overlap((0, 0, 100, 100), (111, 0, 50, 100), margin=12)


# ----- find_free_position: trivial / preferred honoured -------------------

def test_empty_returns_preferred():
    assert find_free_position([], (40, 40)) == (40.0, 40.0)


def test_preferred_kept_when_clear():
    # One node far away; the preferred spot is untouched.
    existing = [(600, 600, 180, 200)]
    assert find_free_position(existing, (40, 40)) == (40.0, 40.0)


def test_zero_size_rects_ignored():
    # A sibling that hasn't rendered yet reports 0x0 and must not block.
    existing = [(40, 40, 0, 0)]
    assert find_free_position(existing, (40, 40)) == (40.0, 40.0)


# ----- find_free_position: collision resolved -----------------------------

def _overlaps_any(pos, existing, node_size=nl.DEFAULT_NODE_SIZE, margin=nl.MARGIN):
    cand = (pos[0], pos[1], node_size[0], node_size[1])
    return any(rects_overlap(cand, r, margin) for r in existing)


def test_collision_moves_to_clear_slot():
    # A tall node covering the preferred spot forces a move.
    existing = [(40, 40, 180, 300)]
    pos = find_free_position(existing, (40, 100))
    assert pos != (40.0, 100.0)
    assert not _overlaps_any(pos, existing)


def test_result_clears_every_node():
    existing = [
        (40, 40, 180, 200),
        (240, 40, 180, 200),
        (40, 260, 180, 200),
    ]
    pos = find_free_position(existing, (60, 60))
    assert not _overlaps_any(pos, existing)


def test_first_free_slot_is_beside_a_single_node():
    # Node 1 fills the top-left; row-major scan tucks the newcomer just to its
    # right on the same row (first clear grid column past node 1 + margin).
    existing = [(40, 40, 180, 200)]
    pos = find_free_position(existing, (40, 100))
    assert not _overlaps_any(pos, existing)
    # Same top row, to the right (not shoved down a whole node height).
    assert pos[1] == 40.0
    assert pos[0] >= 40 + 180  # past node 1's right edge


def test_bigger_new_node_needs_bigger_gap():
    # A wide newcomer can't fit a narrow slot a small one would.
    existing = [(40, 40, 180, 200), (300, 40, 180, 200)]
    gap_small = find_free_position(existing, (0, 0), node_size=(60, 200))
    gap_big = find_free_position(existing, (0, 0), node_size=(400, 200))
    # The 60-wide node may nestle in the 40px gap between the two; the
    # 400-wide one cannot and must land clear of both.
    assert not _overlaps_any(gap_small, existing, node_size=(60, 200))
    assert not _overlaps_any(gap_big, existing, node_size=(400, 200))


# ----- find_free_position: crammed canvas falls back ----------------------

def test_full_canvas_falls_back_to_preferred():
    # Tile the whole scan region so no grid slot clears; preferred returned.
    step = 20
    existing = [
        (x, y, step, step)
        for x in range(0, 1700, step)
        for y in range(0, 1100, step)
    ]
    pos = find_free_position(
        existing,
        (123, 456),
        node_size=(180, 200),
        grid_step=(40, 40),
        grid_bounds=(1600, 1000),
        margin=12,
    )
    assert pos == (123.0, 456.0)


def test_returned_tuple_is_floats():
    pos = find_free_position([(40, 40, 180, 300)], (40, 40))
    assert isinstance(pos[0], float) and isinstance(pos[1], float)
