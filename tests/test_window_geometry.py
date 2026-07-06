"""Unit tests for the dpg-free window-geometry maths.

Covers the off-screen-safe restore logic the GUI relies on; imports no
DearPyGui, so it runs in the normal suite. The viewport / Win32 glue in
app.py is not covered here (needs a real window) and gets a manual eyeball.
"""

from pysynthrack.ui import window_geometry as w

FHD = (0, 0, 1920, 1080)  # single 1080p monitor


# ----- make_geometry ------------------------------------------------------

def test_make_geometry_basic():
    assert w.make_geometry(1280, 800, 100, 50) == {
        "width": 1280, "height": 800, "x": 100, "y": 50,
    }


def test_make_geometry_coerces_floats():
    # int() truncation is fine for pixel coordinates.
    assert w.make_geometry(1280.0, 800.9, 100.5, 50.2) == {
        "width": 1280, "height": 800, "x": 100, "y": 50,
    }


def test_make_geometry_bad_size_returns_none():
    assert w.make_geometry(None, 800, 0, 0) is None
    assert w.make_geometry("nope", 800, 0, 0) is None


def test_make_geometry_bad_pos_stored_none():
    assert w.make_geometry(800, 600, None, None) == {
        "width": 800, "height": 600, "x": None, "y": None,
    }


# ----- resolve: junk in -> None out ---------------------------------------

def test_resolve_non_dict_is_none():
    assert w.resolve(None, FHD) is None
    assert w.resolve("x", FHD) is None
    assert w.resolve(42, FHD) is None


def test_resolve_missing_size_is_none():
    assert w.resolve({"x": 10, "y": 10}, FHD) is None
    assert w.resolve({"width": 800}, FHD) is None


# ----- resolve: size clamping ---------------------------------------------

def test_resolve_size_floored_to_minimum():
    r = w.resolve({"width": 10, "height": 10, "x": 0, "y": 0}, FHD)
    assert r["width"] == w.MIN_W
    assert r["height"] == w.MIN_H


def test_resolve_size_capped_to_desktop():
    r = w.resolve({"width": 5000, "height": 4000, "x": 0, "y": 0}, FHD)
    assert r["width"] == 1920
    assert r["height"] == 1080


# ----- resolve: unknown bounds --------------------------------------------

def test_resolve_screen_none_keeps_size_drops_pos():
    r = w.resolve({"width": 800, "height": 600, "x": 300, "y": 200}, None)
    assert r == {"width": 800, "height": 600, "x": None, "y": None}


def test_resolve_missing_pos_leaves_pos_none():
    r = w.resolve({"width": 800, "height": 600}, FHD)
    assert r["x"] is None and r["y"] is None
    assert r["width"] == 800 and r["height"] == 600


# ----- resolve: position within / outside bounds --------------------------

def test_resolve_position_within_bounds_preserved():
    r = w.resolve({"width": 800, "height": 600, "x": 300, "y": 200}, FHD)
    assert (r["x"], r["y"]) == (300, 200)


def test_resolve_offscreen_right_clamped_fully_visible():
    # x=3200 on a single 1920 screen -> pulled left so the 800-wide window fits.
    r = w.resolve({"width": 800, "height": 600, "x": 3200, "y": 50}, FHD)
    assert r["x"] == 1920 - 800
    assert r["y"] == 50


def test_resolve_partial_offscreen_pulled_in():
    r = w.resolve({"width": 800, "height": 600, "x": 1500, "y": 50}, FHD)
    assert r["x"] == 1920 - 800  # 1120


def test_resolve_offscreen_negative_clamped_to_origin():
    r = w.resolve({"width": 800, "height": 600, "x": -500, "y": -300}, FHD)
    assert r["x"] == 0 and r["y"] == 0


def test_resolve_secondary_monitor_right_preserved():
    # Two monitors side by side: virtual desktop spans 0..3840.
    r = w.resolve({"width": 800, "height": 600, "x": 2000, "y": 100},
                  (0, 0, 3840, 1080))
    assert r["x"] == 2000


def test_resolve_secondary_monitor_left_negative_origin():
    # Second monitor to the LEFT: virtual origin is negative.
    r = w.resolve({"width": 800, "height": 600, "x": -1800, "y": 100},
                  (-1920, 0, 3840, 1080))
    assert r["x"] == -1800  # inside [-1920, 1120]
