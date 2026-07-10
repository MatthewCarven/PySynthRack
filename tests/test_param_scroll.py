"""Tests for ui/param_scroll.py -- the dpg-free value math behind
scroll-to-adjust on hovered param widgets (1% of range per notch, Shift =
10%; ints step by 1 / 10). The dpg wiring (hover detection, value write-back)
lives in ui/app.py and is eyeball-verified in the real window."""
from __future__ import annotations

import pytest

from pysynthrack.ui.param_scroll import cycle_index, nudge_number


class TestNudgeNumberFloat:
    def test_up_is_one_percent_of_range(self):
        # mix slider 0..1 -> a notch is 0.01
        assert nudge_number(0.5, 1, min_value=0.0, max_value=1.0) == pytest.approx(0.51)

    def test_down_is_one_percent_of_range(self):
        assert nudge_number(0.5, -1, min_value=0.0, max_value=1.0) == pytest.approx(0.49)

    def test_coarse_is_ten_percent(self):
        assert nudge_number(0.5, 1, min_value=0.0, max_value=1.0, coarse=True) == pytest.approx(0.6)

    def test_wide_range_scales(self):
        # cents -100..100 -> a notch is 2.0
        assert nudge_number(0.0, 1, min_value=-100.0, max_value=100.0) == pytest.approx(2.0)

    def test_clamped_at_max(self):
        assert nudge_number(1.0, 1, min_value=0.0, max_value=1.0) == pytest.approx(1.0)

    def test_clamped_at_min(self):
        assert nudge_number(0.0, -1, min_value=0.0, max_value=1.0) == pytest.approx(0.0)

    def test_reversed_bounds_still_clamp(self):
        # Bounds handed in high..low must not invert the clamp.
        v = nudge_number(5.0, 1, min_value=10.0, max_value=0.0)
        assert 0.0 <= v <= 10.0

    def test_zero_width_range_returns_clamped_input(self):
        assert nudge_number(3.0, 1, min_value=3.0, max_value=3.0) == pytest.approx(3.0)

    def test_direction_zero_counts_as_down(self):
        assert nudge_number(0.5, 0, min_value=0.0, max_value=1.0) == pytest.approx(0.49)


class TestNudgeNumberInt:
    def test_int_steps_by_one(self):
        assert nudge_number(2, 1, min_value=2, max_value=4, is_int=True) == 3

    def test_int_down_steps_by_one(self):
        assert nudge_number(4, -1, min_value=2, max_value=4, is_int=True) == 3

    def test_int_coarse_steps_by_ten_then_clamps(self):
        assert nudge_number(2, 1, min_value=2, max_value=4, is_int=True, coarse=True) == 4

    def test_int_clamped_at_bounds(self):
        assert nudge_number(4, 1, min_value=2, max_value=4, is_int=True) == 4
        assert nudge_number(2, -1, min_value=2, max_value=4, is_int=True) == 2

    def test_int_result_is_python_int(self):
        assert isinstance(nudge_number(2, 1, min_value=0, max_value=8, is_int=True), int)


class TestCycleIndex:
    def test_forward(self):
        assert cycle_index(0, 1, 3) == 1

    def test_backward(self):
        assert cycle_index(2, -1, 3) == 1

    def test_clamped_top(self):
        assert cycle_index(2, 1, 3) == 2

    def test_clamped_bottom(self):
        assert cycle_index(0, -1, 3) == 0

    def test_empty_list(self):
        assert cycle_index(0, 1, 0) == 0
