"""The oscillator's note mode: ``tune`` = "note" sets the pitch from ``note``
moved by ``cents``; "hz" (the default) is the oscillator as it was.

Matthew, 2026-11-20: "i'd like to implement note selection on the oscillator
... but at the same time i do not wish to loose control over the final value
... some songs sing things a little brighter some songs flatter". So the note
is a way of entering the pitch, not a cage: cents are continuous, switching
modes converts both ways without a jump, and Hz mode is untouched.
"""
from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.pitch import (
    NOTE_CHOICES,
    hz_to_note,
    midi_to_note,
    note_to_hz,
    note_to_midi,
)
from pysynthrack.modules.oscillator import OSC_TUNES

SR = 48000
F = 512


class TestPitch:
    @pytest.mark.parametrize("note,midi", [("A4", 69), ("C4", 60), ("C0", 12), ("B8", 119),
                                           ("C#3", 49), ("Db3", 49), ("Cb4", 59), ("B#3", 60),
                                           ("a4", 69), (" G2 ", 43)])
    def test_names(self, note, midi):
        assert note_to_midi(note) == midi

    def test_bad_names(self):
        for bad in ("", "H4", "A", "4A", "A#b4", None):
            assert note_to_midi(bad) is None
            assert note_to_hz(bad) is None

    def test_hz(self):
        assert note_to_hz("A4") == 440.0
        assert note_to_hz("A5") == 880.0
        assert note_to_hz("C4") == pytest.approx(261.6255653, rel=1e-9)
        assert note_to_hz("A4", 100.0) == pytest.approx(note_to_hz("A#4"), rel=1e-12)
        assert note_to_hz("A4", 7.85) == pytest.approx(442.0, rel=1e-4)   # A = 442
        assert note_to_hz("A4", float("nan")) is None

    @pytest.mark.parametrize("hz", [440.0, 541.9, 466.1637615180899, 27.5, 7902.0, 100.0])
    def test_round_trip_is_the_same_frequency(self, hz):
        note, cents = hz_to_note(hz)
        assert abs(cents) <= 50.0 + 1e-9
        assert note_to_hz(note, cents) == pytest.approx(hz, rel=1e-12)

    def test_choices(self):
        assert NOTE_CHOICES[0] == "C0" and NOTE_CHOICES[-1] == "B8" and len(NOTE_CHOICES) == 108
        assert all(midi_to_note(note_to_midi(n)) == n for n in NOTE_CHOICES)


def _osc(**params):
    patch = Patch()
    osc = patch.add_module("oscillator", params={"waveform": "sine", "amp": 0.3, **params})
    be = NumpyBackend(sample_rate=SR, block_size=F)
    be.compile(patch)
    return be, patch, osc


def _peak_hz(be, patch, osc, blocks=60):
    y = np.concatenate([be._render_module(osc, F, {}, patch) for _ in range(blocks)])
    spec = np.abs(np.fft.rfft(y.astype(np.float64) * np.hanning(len(y))))
    return np.fft.rfftfreq(len(y), 1.0 / SR)[spec.argmax()]


class TestRenderer:
    def test_defaults(self):
        osc = Patch().add_module("oscillator")
        assert osc.params["tune"] == "hz" and osc.params["note"] == "A4"
        assert osc.params["cents"] == 0.0
        assert OSC_TUNES == ("hz", "note")

    def test_hz_mode_ignores_note_and_cents(self):
        a = _osc(freq=330.0)
        b = _osc(freq=330.0, note="C2", cents=55.0)
        ya = np.concatenate([a[0]._render_module(a[2], F, {}, a[1]) for _ in range(4)])
        yb = np.concatenate([b[0]._render_module(b[2], F, {}, b[1]) for _ in range(4)])
        np.testing.assert_array_equal(ya, yb)

    def test_note_mode_plays_the_note(self):
        be, patch, osc = _osc(tune="note", note="E5", freq=100.0)
        assert _peak_hz(be, patch, osc) == pytest.approx(note_to_hz("E5"), rel=0.01)
        assert be.snapshot_osc_freqs()[osc.id][0] == pytest.approx(note_to_hz("E5"), rel=1e-12)

    def test_cents_move_it_continuously(self):
        be, patch, osc = _osc(tune="note", note="A4", cents=7.85)
        be._render_module(osc, F, {}, patch)
        assert be.snapshot_osc_freqs()[osc.id][0] == pytest.approx(442.0, rel=1e-4)

    def test_cents_are_clamped_to_a_semitone(self):
        be, patch, osc = _osc(tune="note", note="A4", cents=250.0)
        be._render_module(osc, F, {}, patch)
        assert be.snapshot_osc_freqs()[osc.id][0] == pytest.approx(note_to_hz("A#4"), rel=1e-12)

    def test_a_bad_note_falls_back_to_freq(self):
        be, patch, osc = _osc(tune="note", note="H9", freq=123.0)
        be._render_module(osc, F, {}, patch)
        assert be.snapshot_osc_freqs()[osc.id][0] == 123.0

    def test_freq_cv_rides_on_top(self):
        be, patch, osc = _osc(tune="note", note="A3")
        src = patch.add_module("lfo")
        patch.connect(src.id, "cv", osc.id, "freq_cv")
        be.compile(patch)
        be._render_module(osc, F, {(src.id, "cv"): np.full(F, 1.0, dtype=np.float32)}, patch)
        assert be.snapshot_osc_freqs()[osc.id][0] == pytest.approx(440.0, rel=1e-12)

    def test_note_mode_equals_hz_mode_at_the_same_pitch(self):
        a = _osc(freq=note_to_hz("D4", 12.5))
        b = _osc(tune="note", note="D4", cents=12.5)
        ya = np.concatenate([a[0]._render_module(a[2], F, {}, a[1]) for _ in range(4)])
        yb = np.concatenate([b[0]._render_module(b[2], F, {}, b[1]) for _ in range(4)])
        np.testing.assert_array_equal(ya, yb)


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

    def _node(self, a, **params):
        osc = a.patch.add_module("oscillator", params=params)
        # Give each widget a distinct id so the widget map can tell them apart.
        import itertools
        ids = itertools.count(1000)
        a_mod_dpg = __import__("pysynthrack.ui.app", fromlist=["dpg"]).dpg
        a_mod_dpg.last_item.side_effect = lambda: next(ids)
        a._create_node_for_module(osc)
        return osc

    def test_the_widgets(self, app):
        a, app_mod = app
        dpg = app_mod.dpg
        c0 = len(dpg.add_combo.call_args_list)
        self._node(a)
        combos = {c.kwargs.get("label"): list(c.kwargs.get("items") or [])
                  for c in dpg.add_combo.call_args_list[c0:]}
        assert combos["tune"] == ["hz", "note"]
        assert combos["note"] == list(NOTE_CHOICES)
        assert any(c.kwargs.get("label") == "cents" for c in dpg.add_drag_float.call_args_list)

    def test_hz_mode_greys_note_and_cents(self, app):
        a, app_mod = app
        osc = self._node(a)
        calls = {(c.args[0], c.kwargs.get("enabled"))
                 for c in app_mod.dpg.configure_item.call_args_list if "enabled" in c.kwargs}
        assert (a._param_widget(osc.id, "freq"), True) in calls
        assert (a._param_widget(osc.id, "note"), False) in calls
        assert (a._param_widget(osc.id, "cents"), False) in calls

    def test_switching_to_note_keeps_the_pitch(self, app):
        a, _ = app
        osc = self._node(a, freq=541.9)
        a._on_osc_tune_changed(None, "note", (osc.id, "tune"))
        assert osc.params["tune"] == "note" and osc.params["note"] == "C#5"
        assert osc.params["cents"] == pytest.approx(-39.3723, abs=1e-3)
        assert note_to_hz(osc.params["note"], osc.params["cents"]) == pytest.approx(541.9, rel=1e-12)

    def test_switching_back_to_hz_keeps_the_pitch(self, app):
        a, _ = app
        osc = self._node(a, tune="note", note="G3", cents=-12.0, freq=1.0)
        a._on_osc_tune_changed(None, "hz", (osc.id, "tune"))
        assert osc.params["tune"] == "hz"
        assert osc.params["freq"] == pytest.approx(note_to_hz("G3", -12.0), rel=1e-12)

    def test_note_and_cents_write_freq_in_note_mode(self, app):
        a, app_mod = app
        osc = self._node(a, tune="note", note="A4")
        a._on_osc_note_changed(None, "E4", (osc.id, "note"))
        assert osc.params["freq"] == pytest.approx(note_to_hz("E4"), rel=1e-12)
        a._on_osc_note_changed(None, 25.0, (osc.id, "cents"))
        assert osc.params["freq"] == pytest.approx(note_to_hz("E4", 25.0), rel=1e-12)
        app_mod.dpg.set_value.assert_any_call(a._param_widget(osc.id, "freq"), osc.params["freq"])

    def test_note_edits_leave_freq_alone_in_hz_mode(self, app):
        a, _ = app
        osc = self._node(a, freq=333.0)
        a._on_osc_note_changed(None, "E4", (osc.id, "note"))
        assert osc.params["freq"] == 333.0 and osc.params["note"] == "E4"

    def test_out_of_range_freq_clamps_cents_and_says_so(self, app):
        a, _ = app
        osc = self._node(a, freq=5.0)
        statuses = []
        a._set_status = statuses.append
        a._on_osc_tune_changed(None, "note", (osc.id, "tune"))
        assert osc.params["note"] == "C0" and osc.params["cents"] == -100.0
        assert statuses and "outside" in statuses[-1]
