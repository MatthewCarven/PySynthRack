"""Unit tests for the dpg-free zoom maths (``pysynthrack.ui.zoom``).

These cover the logic the GUI's fake canvas-zoom relies on. They import no
DearPyGui, so they run in the normal suite without a graphics context; the
end-to-end DPG behaviour is exercised separately by a headless check.
"""

import pytest

from pysynthrack.ui import zoom as z

# ----- constants ----------------------------------------------------------

def test_default_is_unity():
    assert z.ZOOM_DEFAULT == 1.0


def test_bounds_ordered_and_sane():
    assert z.ZOOM_MIN < z.ZOOM_DEFAULT < z.ZOOM_MAX
    assert z.ZOOM_MIN > 0.0
    assert z.ZOOM_STEP > 1.0


# ----- clamp_zoom ----------------------------------------------------------

def test_clamp_inside_range_is_identity():
    assert z.clamp_zoom(1.0) == 1.0
    assert z.clamp_zoom(2.0) == 2.0


def test_clamp_below_min():
    assert z.clamp_zoom(0.01) == z.ZOOM_MIN
    assert z.clamp_zoom(-5.0) == z.ZOOM_MIN


def test_clamp_above_max():
    assert z.clamp_zoom(99.0) == z.ZOOM_MAX


def test_clamp_at_exact_bounds():
    assert z.clamp_zoom(z.ZOOM_MIN) == z.ZOOM_MIN
    assert z.clamp_zoom(z.ZOOM_MAX) == z.ZOOM_MAX


# ----- step_zoom -----------------------------------------------------------

def test_step_in_multiplies():
    assert z.step_zoom(1.0, +1) == pytest.approx(z.ZOOM_STEP)


def test_step_out_divides():
    assert z.step_zoom(1.0, -1) == pytest.approx(1.0 / z.ZOOM_STEP)


def test_step_zero_is_clamp_only():
    assert z.step_zoom(1.5, 0) == 1.5
    assert z.step_zoom(99.0, 0) == z.ZOOM_MAX


def test_step_in_then_out_round_trips():
    start = 1.0
    assert z.step_zoom(z.step_zoom(start, +1), -1) == pytest.approx(start)


def test_step_out_then_in_round_trips():
    start = 1.7
    assert z.step_zoom(z.step_zoom(start, -1), +1) == pytest.approx(start)


def test_repeated_step_in_never_exceeds_max():
    v = 1.0
    for _ in range(100):
        v = z.step_zoom(v, +1)
    assert v == z.ZOOM_MAX


def test_repeated_step_out_never_below_min():
    v = 1.0
    for _ in range(100):
        v = z.step_zoom(v, -1)
    assert v == z.ZOOM_MIN


def test_custom_step_value():
    assert z.step_zoom(1.0, +1, step=2.0) == pytest.approx(2.0)


# ----- scale_pos -----------------------------------------------------------

def test_scale_pos_basic():
    assert z.scale_pos((100.0, 200.0), 2.0) == (200.0, 400.0)


def test_scale_pos_identity():
    assert z.scale_pos((37.0, -12.0), 1.0) == (37.0, -12.0)


def test_scale_pos_handles_negative_and_zero():
    assert z.scale_pos((-50.0, 0.0), 0.5) == (-25.0, 0.0)


def test_scale_pos_accepts_list_returns_float_tuple():
    out = z.scale_pos([10, 20], 1.5)
    assert out == (15.0, 30.0)
    assert isinstance(out, tuple)
    assert all(isinstance(c, float) for c in out)


def test_scale_pos_ratio_composition():
    # Scaling by a then by b equals scaling by a*b (telescoping ratios, the
    # property that keeps slider-drag rescaling drift-free).
    p = (123.0, 45.0)
    a, b = 1.3, 0.7
    once = z.scale_pos(p, a * b)
    twice = z.scale_pos(z.scale_pos(p, a), b)
    assert once[0] == pytest.approx(twice[0])
    assert once[1] == pytest.approx(twice[1])


# ----- percent conversion --------------------------------------------------

def test_factor_to_percent_rounds():
    assert z.factor_to_percent(1.0) == 100
    assert z.factor_to_percent(0.25) == 25
    assert z.factor_to_percent(3.0) == 300
    assert z.factor_to_percent(1.215) == 122


def test_percent_to_factor_basic():
    assert z.percent_to_factor(100) == 1.0
    assert z.percent_to_factor(25) == 0.25


def test_percent_to_factor_clamps():
    assert z.percent_to_factor(1000) == z.ZOOM_MAX
    assert z.percent_to_factor(1) == z.ZOOM_MIN


def test_percent_round_trip_at_slider_stops():
    for pct in (25, 50, 100, 150, 200, 300):
        assert z.factor_to_percent(z.percent_to_factor(pct)) == pct
