"""Tests for ui/param_scroll.py -- the dpg-free value math behind
scroll-to-adjust. A notch bumps the least-significant displayed digit, snapped
to a nice size near 1% of range (Shift = x10); ints step by 1 / 10. The dpg
wiring (hover detection, value write-back) lives in ui/app.py and is eyeball-
verified in the real window."""
from __future__ import annotations

import pytest

from pysynthrack.ui.param_scroll import (
    cycle_index,
    decimals_from_format,
    nudge_number,
    scroll_step,
)


class TestDecimalsFromFormat:
    @pytest.mark.parametrize("fmt,want", [
        ("%.2f", 2),
        ("%.2f st", 2),
        ("%.0f Hz", 0),
        ("%.3f s", 3),
        ("%.1f dB/unit", 1),
        ("%.2f oct/unit", 2),
        ("%d%%", None),      # int format (the zoom %) -- no float field
        ("", None),
        (None, None),
    ])
    def test_parses_precision(self, fmt, want):
        assert decimals_from_format(fmt) == want


class TestScrollStep:
    @pytest.mark.parametrize("lo,hi,dec,want", [
        (0.0, 1.0, 2, 0.01),          # mix "%.2f" -> a hundredth
        (0.05, 10.0, 2, 0.01),        # LFO rate "%.2f" -> a hundredth (the fix)
        (0.1, 20.0, 2, 0.1),          # resonance "%.2f", ~1% = 0.2 -> 0.1
        (20.0, 20000.0, 0, 100.0),    # cutoff "%.0f Hz" -> 100 Hz, not 1
        (-24.0, 24.0, 2, 0.1),        # semitones "%.2f st" -> 0.1, not 0.48
        (-100.0, 100.0, 0, 1.0),      # cents "%.0f ct" -> 1
        (-24.0, 24.0, 1, 0.1),        # dB "%.1f" -> 0.1
        (10.0, 200.0, 0, 1.0),        # grain_size "%.0f ms" -> 1
        (0.0, 5.0, 3, 0.01),          # attack "%.3f s" -> 0.01 (10 ms)
    ])
    def test_nice_step(self, lo, hi, dec, want):
        assert scroll_step(lo, hi, dec) == pytest.approx(want)

    def test_never_finer_than_displayed_digit(self):
        # A tiny range whose 1% is below the precision still steps one digit.
        assert scroll_step(0.0, 0.3, 2) == pytest.approx(0.01)


class TestNudgeNumberFloat:
    def test_narrow_two_decimal_steps_a_hundredth(self):
        # The reported bug: LFO rate 0.05..10 "%.2f" now moves 0.01, not 0.1.
        assert nudge_number(1.0, 1, min_value=0.05, max_value=10.0,
                            decimals=2) == pytest.approx(1.01)

    def test_mix_still_a_hundredth(self):
        assert nudge_number(0.5, 1, min_value=0.0, max_value=1.0,
                            decimals=2) == pytest.approx(0.51)

    def test_wide_cutoff_steps_coarse(self):
        assert nudge_number(1000.0, 1, min_value=20.0, max_value=20000.0,
                            decimals=0) == pytest.approx(1100.0)

    def test_semitone_steps_a_tenth(self):
        assert nudge_number(5.0, 1, min_value=-24.0, max_value=24.0,
                            decimals=2) == pytest.approx(5.1)

    def test_down(self):
        assert nudge_number(0.5, -1, min_value=0.0, max_value=1.0,
                            decimals=2) == pytest.approx(0.49)

    def test_coarse_is_ten_notches(self):
        assert nudge_number(0.5, 1, min_value=0.0, max_value=1.0,
                            decimals=2, coarse=True) == pytest.approx(0.6)

    def test_result_rounds_to_displayed_precision(self):
        # An off-grid value snaps onto the displayed grid so value == readout.
        assert nudge_number(0.517, 1, min_value=0.0, max_value=1.0,
                            decimals=2) == pytest.approx(0.53)

    def test_clamped_at_bounds(self):
        assert nudge_number(1.0, 1, min_value=0.0, max_value=1.0,
                            decimals=2) == pytest.approx(1.0)
        assert nudge_number(0.0, -1, min_value=0.0, max_value=1.0,
                            decimals=2) == pytest.approx(0.0)

    def test_no_decimals_falls_back_to_one_percent(self):
        assert nudge_number(0.5, 1, min_value=0.0, max_value=1.0) == pytest.approx(0.51)
        assert nudge_number(0.0, 1, min_value=-100.0, max_value=100.0) == pytest.approx(2.0)


class TestNudgeNumberInt:
    def test_int_steps_by_one(self):
        assert nudge_number(2, 1, min_value=2, max_value=4, is_int=True) == 3

    def test_int_coarse_steps_by_ten_then_clamps(self):
        assert nudge_number(2, 1, min_value=2, max_value=4, is_int=True, coarse=True) == 4

    def test_int_clamped(self):
        assert nudge_number(4, 1, min_value=2, max_value=4, is_int=True) == 4

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
