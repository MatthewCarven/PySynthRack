"""The oscillator node's read-only output-frequency readout.

Matthew, 2026-11-19: "on the oscillator can we have a number read only
frequency output so we can see in not quite realtime but like not lazily the
output frequency number?" The renderer records ``freq * 2 ** freq_cv`` at the
last sample of each block (read-only, beside the wave); the node shows it
about ten times a second with the nearest note, grey ``out: --`` while
stopped.
"""
from __future__ import annotations

import math
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.ui.readouts import format_hz, format_osc_freq, note_of

SR = 48000
F = 512


def _rig(freq=440.0, cv=True):
    patch = Patch()
    osc = patch.add_module("oscillator", params={"waveform": "sine", "freq": freq, "amp": 0.3})
    src = patch.add_module("lfo")
    if cv:
        patch.connect(src.id, "cv", osc.id, "freq_cv")
    be = NumpyBackend(sample_rate=SR, block_size=F)
    be.compile(patch)
    return be, patch, osc, src


def _render(be, patch, osc, src, cv=None):
    bufs = {} if cv is None else {(src.id, "cv"): np.asarray(cv, dtype=np.float32)}
    return be._render_module(osc, F, bufs, patch)


class TestBackend:
    def test_none_before_the_first_block(self):
        be, patch, osc, src = _rig()
        assert be.snapshot_osc_freqs() == {osc.id: None}

    def test_no_cv_is_the_knob(self):
        be, patch, osc, src = _rig(freq=220.0, cv=False)
        _render(be, patch, osc, src)
        assert be.snapshot_osc_freqs()[osc.id] == (220.0, 220.0, 1)

    def test_a_held_cv_is_freq_times_two_to_the_cv(self):
        be, patch, osc, src = _rig()
        _render(be, patch, osc, src, np.full(F, 0.3))
        lo, hi, v = be.snapshot_osc_freqs()[osc.id]
        assert v == 1 and lo == hi
        assert lo == pytest.approx(440.0 * 2 ** 0.3, rel=1e-6)

    def test_it_is_the_last_sample_of_the_block(self):
        be, patch, osc, src = _rig()
        _render(be, patch, osc, src, np.linspace(0.0, 1.0, F))
        assert be.snapshot_osc_freqs()[osc.id][0] == pytest.approx(880.0, rel=1e-6)

    def test_voices_give_the_range(self):
        be, patch, osc, src = _rig()
        cv = np.stack([np.full(F, -1.0), np.zeros(F), np.full(F, 1.0)])
        _render(be, patch, osc, src, cv)
        lo, hi, v = be.snapshot_osc_freqs()[osc.id]
        assert (v, lo, hi) == (3, pytest.approx(220.0), pytest.approx(880.0))

    def test_a_non_finite_cv_reads_nan(self):
        be, patch, osc, src = _rig()
        cv = np.zeros(F)
        cv[-1] = np.nan
        _render(be, patch, osc, src, cv)
        lo, hi, _ = be.snapshot_osc_freqs()[osc.id]
        assert math.isnan(lo) and math.isnan(hi)

    def test_the_wave_is_untouched(self):
        # The readout is bookkeeping beside the render: the same block with
        # and without a key for this module in _osc_freqs is the same wave.
        be1, p1, o1, s1 = _rig()
        be2, p2, o2, s2 = _rig()
        be2._osc_freqs.clear()
        cv = np.linspace(-0.5, 0.5, F)
        a = _render(be1, p1, o1, s1, cv)
        b = _render(be2, p2, o2, s2, cv)
        np.testing.assert_array_equal(a, b)
        assert be2.snapshot_osc_freqs() == {}

    def test_compile_tracks_the_patch(self):
        be, patch, osc, src = _rig()
        _render(be, patch, osc, src, np.zeros(F))
        osc2 = patch.add_module("oscillator")
        be.compile(patch)
        assert set(be.snapshot_osc_freqs()) == {osc.id, osc2.id}
        patch.remove_module(osc.id) if hasattr(patch, "remove_module") else patch.modules.pop(osc.id)
        be.compile(patch)
        assert set(be.snapshot_osc_freqs()) == {osc2.id}

    def test_through_render_block(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "sine", "freq": 330.0, "amp": 0.3})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", spk.id, "in")
        be = NumpyBackend(sample_rate=SR, block_size=F)
        be.compile(patch)
        be.render_block(F)
        assert be.snapshot_osc_freqs()[osc.id] == (330.0, 330.0, 1)


class TestText:
    @pytest.mark.parametrize("hz,want", [
        (440.0, "A4 +0c"), (261.6256, "C4 +0c"), (541.9, "C#5 -39c"),
        (27.5, "A0 +0c"), (4186.0, "C8 +0c"), (450.0, "A4 +39c"),
    ])
    def test_note_names(self, hz, want):
        assert note_of(hz) == want

    def test_no_note_outside_the_audible_range(self):
        assert note_of(0.5) == "" and note_of(25000.0) == "" and note_of(float("nan")) == ""

    def test_precision(self):
        assert (format_hz(55.0), format_hz(440.0), format_hz(12345.6)) == ("55.00", "440.0", "12346")

    def test_lines(self):
        assert format_osc_freq(None) == "out: --"
        assert format_osc_freq((440.0, 440.0, 1), running=False) == "out: --"
        assert format_osc_freq((440.0, 440.0, 1)) == "out: 440.0 Hz  A4 +0c"
        assert format_osc_freq((0.5, 0.5, 1)) == "out: 0.50 Hz"
        assert format_osc_freq((220.0, 220.0, 4)) == "out: 220.0 Hz  A3 +0c"
        assert format_osc_freq((130.81, 523.25, 16)) == "out: 130.8-523.2 Hz (16 voices)"
        assert format_osc_freq((float("nan"), float("nan"), 1)) == "out: nan Hz"
        assert format_osc_freq((440.0, 440.0, 1)).isascii()


class TestUI:
    @pytest.fixture
    def app(self, monkeypatch):
        pytest.importorskip("dearpygui.dearpygui")
        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
        a = app_mod.App()
        a.backend = NumpyBackend(sample_rate=SR, block_size=F)
        a.patch = Patch()
        return a, app_mod

    def test_the_node_gets_the_line(self, app):
        a, app_mod = app
        osc = a.patch.add_module("oscillator")
        a._create_node_for_module(osc)
        assert osc.id in a._osc_freq_labels

    def test_running_shows_the_number_stopped_shows_dashes(self, app, monkeypatch):
        a, app_mod = app
        osc = a.patch.add_module("oscillator")
        a._create_node_for_module(osc)
        label = a._osc_freq_labels[osc.id]
        a.backend.snapshot_osc_freqs = lambda: {osc.id: (440.0, 440.0, 1)}
        monkeypatch.setattr(type(a.backend), "is_running", property(lambda self: True))
        a._update_osc_freqs(force=True)
        app_mod.dpg.set_value.assert_any_call(label, "out: 440.0 Hz  A4 +0c")
        monkeypatch.setattr(type(a.backend), "is_running", property(lambda self: False))
        a._update_osc_freqs(force=True)
        app_mod.dpg.set_value.assert_any_call(label, "out: --")

    def test_it_repeats_about_ten_times_a_second_not_every_frame(self, app, monkeypatch):
        a, app_mod = app
        osc = a.patch.add_module("oscillator")
        a._create_node_for_module(osc)
        a.backend.snapshot_osc_freqs = lambda: {osc.id: (440.0, 440.0, 1)}
        clock = iter([100.0, 100.05, 100.11])
        monkeypatch.setattr(app_mod.time, "monotonic", lambda: next(clock))
        n0 = app_mod.dpg.set_value.call_count
        a._update_osc_freqs()            # t=100.00 -> updates
        a._update_osc_freqs()            # t=100.05 -> too soon
        a._update_osc_freqs()            # t=100.11 -> updates
        assert app_mod.dpg.set_value.call_count - n0 == 2
