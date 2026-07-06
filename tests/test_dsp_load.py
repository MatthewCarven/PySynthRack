"""Tests for the toolbar DSP-load readout.

Coverage:
  - Maths (dpg-free ``ui.dsp_load``): formatting at stopped / zero /
    typical / over-budget loads, rounding, negative clamp; colour ramp
    thresholds including the stopped grey; palette matches the meter
    green/red.
  - Backend bookkeeping: a fresh backend reports zeros; rendered blocks
    move the smoothed load and peak (positive, finite, peak >=
    smoothed); the load figure is render time over the block budget (a
    monkeypatched sleep lands near its known value); an over-budget
    block bumps the overload counter; a crashing render leaves the
    stats untouched; ``start()``-time reset is exercised via the same
    attribute wipe start() performs; the snapshot is a plain 3-tuple.

The injection trick mirrors test_backend_crash.py: monkeypatch
``render_block_multi`` and call ``_fill_output`` / ``_audio_callback``
directly with a dummy outdata buffer -- no PortAudio needed.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch
from pysynthrack.ui.dsp_load import (
    HOT_COLOR,
    HOT_LOAD,
    IDLE_COLOR,
    OK_COLOR,
    WARN_COLOR,
    WARN_LOAD,
    format_dsp_load,
    load_color,
)

SR = 44100
F = 512


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect Path.home() so crash files land in tmp_path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _make_backend_with_patch():
    backend = NumpyBackend(sample_rate=SR, block_size=F)
    patch = Patch()
    osc = patch.add_module("oscillator")
    spk = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", spk.id, "in")
    backend.compile(patch)
    return backend


def _render_blocks(backend, n):
    outdata = np.zeros((F, 2), dtype=np.float32)
    for _ in range(n):
        backend._fill_output(outdata, F)


# ----- Maths (dpg-free) -------------------------------------------------------


class TestFormat:
    def test_stopped_is_dashes(self):
        assert format_dsp_load(None) == "DSP --"

    def test_zero(self):
        assert format_dsp_load(0.0) == "DSP 0%"

    def test_typical(self):
        assert format_dsp_load(0.073) == "DSP 7%"

    def test_rounds_not_truncates(self):
        assert format_dsp_load(0.078) == "DSP 8%"

    def test_over_budget_shows_over_100(self):
        assert format_dsp_load(1.34) == "DSP 134%"

    def test_negative_clamps_to_zero(self):
        # perf_counter is monotonic so this shouldn't happen, but the
        # formatter must not print nonsense if it ever does.
        assert format_dsp_load(-0.2) == "DSP 0%"


class TestColor:
    def test_stopped_is_grey(self):
        assert load_color(None) == IDLE_COLOR

    def test_comfortable_is_green(self):
        assert load_color(0.0) == OK_COLOR
        assert load_color(WARN_LOAD - 1e-9) == OK_COLOR

    def test_warn_band_is_amber(self):
        assert load_color(WARN_LOAD) == WARN_COLOR
        assert load_color(HOT_LOAD - 1e-9) == WARN_COLOR

    def test_hot_and_over_budget_are_red(self):
        assert load_color(HOT_LOAD) == HOT_COLOR
        assert load_color(2.0) == HOT_COLOR

    def test_thresholds_ordered(self):
        assert 0.0 < WARN_LOAD < HOT_LOAD <= 1.0


# ----- Backend bookkeeping ----------------------------------------------------


class TestBackendDspLoad:
    def test_fresh_backend_reports_zeros(self):
        backend = _make_backend_with_patch()
        assert backend.dsp_load_snapshot() == (0.0, 0.0, 0)

    def test_rendered_blocks_move_the_load(self):
        backend = _make_backend_with_patch()
        _render_blocks(backend, 20)
        load, peak, overloads = backend.dsp_load_snapshot()
        assert 0.0 < load
        assert np.isfinite(load)
        assert peak >= load
        # A trivial osc->speaker patch must sit far under the budget.
        assert load < 1.0
        assert overloads == 0

    def test_load_measures_render_time_over_budget(self, monkeypatch):
        # A render pinned at ~4x the block budget must converge near 4.
        backend = _make_backend_with_patch()
        budget = F / SR
        orig = NumpyBackend.render_block_multi

        def slow(self, frames):
            t_end = time.perf_counter() + 4.0 * budget
            out = orig(self, frames)
            while time.perf_counter() < t_end:
                pass
            return out

        monkeypatch.setattr(NumpyBackend, "render_block_multi", slow)
        _render_blocks(backend, 40)
        load, peak, overloads = backend.dsp_load_snapshot()
        # EMA after 40 blocks of a constant is within 2% of it; leave
        # slack for scheduling noise on top (never below the pinned 4).
        assert 3.8 < load < 6.0
        assert peak >= 4.0
        assert overloads == 40

    def test_single_overload_counts_once(self, monkeypatch):
        backend = _make_backend_with_patch()
        budget = F / SR
        orig = NumpyBackend.render_block_multi
        calls = {"n": 0}

        def one_slow(self, frames):
            calls["n"] += 1
            out = orig(self, frames)
            if calls["n"] == 3:
                t_end = time.perf_counter() + 2.0 * budget
                while time.perf_counter() < t_end:
                    pass
            return out

        monkeypatch.setattr(NumpyBackend, "render_block_multi", one_slow)
        _render_blocks(backend, 10)
        load, peak, overloads = backend.dsp_load_snapshot()
        assert overloads == 1
        assert peak >= 2.0

    def test_crash_leaves_stats_untouched(self, home, monkeypatch):
        backend = _make_backend_with_patch()
        _render_blocks(backend, 5)
        before = backend.dsp_load_snapshot()
        assert before[0] > 0.0

        def boom(self, frames):
            raise RuntimeError("simulated render explosion")

        monkeypatch.setattr(NumpyBackend, "render_block_multi", boom)
        outdata = np.ones((F, 2), dtype=np.float32)
        backend._audio_callback(outdata, F, None, None)
        assert backend._render_disabled is True
        assert backend.dsp_load_snapshot() == before
        # Disabled blocks short-circuit before the timer, too.
        backend._audio_callback(outdata, F, None, None)
        assert backend.dsp_load_snapshot() == before

    def test_start_reset_wipes_stats(self):
        # start() zeroes the stats before opening the stream; exercise
        # the same wipe without PortAudio by mirroring its assignments.
        backend = _make_backend_with_patch()
        _render_blocks(backend, 5)
        assert backend.dsp_load_snapshot()[0] > 0.0
        backend._dsp_load = 0.0
        backend._dsp_load_peak = 0.0
        backend._dsp_overloads = 0
        assert backend.dsp_load_snapshot() == (0.0, 0.0, 0)

    def test_snapshot_is_plain_tuple(self):
        backend = _make_backend_with_patch()
        snap = backend.dsp_load_snapshot()
        assert isinstance(snap, tuple) and len(snap) == 3
        assert isinstance(snap[0], float)
        assert isinstance(snap[1], float)
        assert isinstance(snap[2], int)
