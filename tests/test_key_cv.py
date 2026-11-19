"""key_cv: one bound key -> a CV between ``idle`` and ``active``, with glide,
chainable through its ``in`` jack.

Matthew, 2026-11-19: "a module that can be bound to a key and set when
active to output 0, 1, 2, -1, -2 cv ... or a slow drift between ... initial
value and activated value and just let the user specify" and "let one key
driven cv feed into another key driven cv please".

The engine tests drive the module's raw-key transport directly (as
test_key_trigger.py does) and render through ``NumpyBackend``; the UI tests
mock dpg (as test_key_trigger_ui.py does).
"""
from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import all_module_types
from pysynthrack.modules.key_cv import KEY_CV_CHAINS, KEY_CV_MODES

SR = 48000
F = 512


def _rig(*param_sets, wire=True):
    """A chain of key_cv modules, each feeding the next one's ``in``."""
    be = NumpyBackend(sample_rate=SR, block_size=F)
    patch = Patch()
    keys = [patch.add_module("key_cv", params=dict(p)) for p in param_sets]
    if wire:
        for a, b in zip(keys, keys[1:]):
            patch.connect(a.id, "out", b.id, "in")
    be.compile(patch)
    return be, patch, keys


def _block(be, patch, keys, frames=F):
    """Render the chain in order (as the topological walk would); return
    the last module's outputs."""
    buffers = {}
    out = None
    for k in keys:
        out = be._render_module(k, frames, buffers, patch)
        for port, buf in out.items():
            buffers[(k.id, port)] = buf
    return out


class TestModel:
    def test_shape(self):
        cls = all_module_types()["key_cv"]
        assert cls.CATEGORY == "Sources"
        assert [(p.name, p.signal_kind) for p in cls.INPUT_PORTS] == [("in", "cv")]
        assert [(p.name, p.signal_kind) for p in cls.OUTPUT_PORTS] == [
            ("out", "cv"), ("gate", "gate")]
        assert getattr(cls, "ACCEPTS_RAW_KEYS", False) is True

    def test_defaults(self):
        m = all_module_types()["key_cv"](1)
        assert m.params == {"key": "", "mode": "gate", "units": "cv", "idle": 0.0,
                            "active": 1.0, "glide": 0.0, "chain": "add"}
        assert KEY_CV_MODES == ("gate", "latch")
        assert KEY_CV_CHAINS == ("add", "override")

    def test_key_trigger_is_untouched(self):
        cls = all_module_types()["key_trigger"]
        assert cls.INPUT_PORTS == [] and [p.name for p in cls.OUTPUT_PORTS] == ["out"]
        assert "idle" not in cls.DEFAULT_PARAMS


class TestValues:
    @pytest.mark.parametrize("idle,active", [(0.0, 1.0), (0.0, 2.0), (1.0, -1.0),
                                             (0.0, -2.0), (0.1, 0.3)])
    def test_idle_up_active_down_exactly(self, idle, active):
        be, patch, (k,) = _rig({"key": "Q", "idle": idle, "active": active})
        r = _block(be, patch, [k])
        assert np.all(r["out"] == np.float32(idle)) and not r["gate"].any()
        k.raw_key_down("Q")
        r = _block(be, patch, [k])
        assert np.all(r["out"] == np.float32(active)) and np.all(r["gate"] == 1.0)
        k.raw_key_up("Q")
        r = _block(be, patch, [k])
        assert np.all(r["out"] == np.float32(idle)) and not r["gate"].any()

    def test_unbound_sits_at_idle(self):
        be, patch, (k,) = _rig({"key": "", "idle": -1.0, "active": 2.0})
        k.raw_key_down("Q")
        assert np.all(_block(be, patch, [k])["out"] == -1.0)

    def test_latch_toggles(self):
        be, patch, (k,) = _rig({"key": "L", "mode": "latch", "active": 2.0})
        k.raw_key_down("L")
        k.raw_key_up("L")
        assert np.all(_block(be, patch, [k])["out"] == 2.0)   # on after a tap
        assert np.all(_block(be, patch, [k])["out"] == 2.0)   # and stays on
        k.raw_key_down("L")
        k.raw_key_up("L")
        assert np.all(_block(be, patch, [k])["out"] == 0.0)   # off on the next


class TestGlide:
    def test_a_full_move_takes_glide_seconds(self):
        glide = 0.1
        be, patch, (k,) = _rig({"key": "G", "idle": 0.0, "active": 2.0, "glide": glide})
        k.raw_key_down("G")
        n = int(glide * SR)
        ys = np.concatenate([_block(be, patch, [k])["out"] for _ in range(n // F + 2)])
        # Linear from the first sample, there at n samples, then held.
        assert ys[0] == pytest.approx(2.0 / n, rel=1e-6)
        assert ys[n // 2 - 1] == pytest.approx(1.0, rel=1e-4)
        assert np.all(ys[n:] == 2.0)
        assert np.all(np.diff(ys[:n]) > 0)

    def test_letting_go_half_way_comes_back_in_half_the_time(self):
        glide = 0.2
        be, patch, (k,) = _rig({"key": "G", "active": 1.0, "glide": glide})
        n = int(glide * SR)
        k.raw_key_down("G")
        up = np.concatenate([_block(be, patch, [k])["out"] for _ in range(n // 2 // F)])
        k.raw_key_up("G")
        down = np.concatenate([_block(be, patch, [k])["out"] for _ in range(n // F)])
        peak = float(up[-1])
        assert 0.4 < peak < 0.6
        back = int(np.argmax(down == 0.0))
        assert back == pytest.approx(len(up), abs=2)
        assert down[0] < peak

    def test_glide_is_block_size_independent(self):
        def run(block):
            be, patch, (k,) = _rig({"key": "G", "idle": -1.0, "active": 1.5, "glide": 0.05})
            out = []
            total = 0
            n = int(0.2 * SR)
            while total < n:
                if total == 0:
                    k.raw_key_down("G")
                if total == 4096:
                    k.raw_key_up("G")
                out.append(_block(be, patch, [k], frames=block)["out"])
                total += block
            return np.concatenate(out)[:n]
        a, b = run(256), run(1024)
        np.testing.assert_allclose(a, b, rtol=0, atol=1e-6)


class TestChaining:
    def test_add_stacks(self):
        be, patch, (a, b, c) = _rig(
            {"key": "1", "active": 1.0},
            {"key": "2", "active": 7 / 12},
            {"key": "3", "active": -1.0},
        )
        assert np.all(_block(be, patch, [a, b, c])["out"] == 0.0)
        a.raw_key_down("1")
        assert np.allclose(_block(be, patch, [a, b, c])["out"], 1.0)
        b.raw_key_down("2")
        assert np.allclose(_block(be, patch, [a, b, c])["out"], 1.0 + 7 / 12)
        c.raw_key_down("3")
        assert np.allclose(_block(be, patch, [a, b, c])["out"], 7 / 12)
        a.raw_key_up("1")
        assert np.allclose(_block(be, patch, [a, b, c])["out"], 7 / 12 - 1.0)

    def test_override_takes_over_while_held(self):
        be, patch, (a, b) = _rig(
            {"key": "1", "active": 3.0},
            {"key": "2", "idle": 0.0, "active": -2.0, "chain": "override"},
        )
        a.raw_key_down("1")
        assert np.all(_block(be, patch, [a, b])["out"] == 3.0)   # passes in through
        b.raw_key_down("2")
        assert np.all(_block(be, patch, [a, b])["out"] == -2.0)  # b wins
        b.raw_key_up("2")
        assert np.all(_block(be, patch, [a, b])["out"] == 3.0)   # back to a

    def test_override_glide_crossfades_from_the_input(self):
        be, patch, (a, b) = _rig(
            {"key": "1", "active": 1.0},
            {"key": "2", "active": -1.0, "glide": 0.05, "chain": "override"},
        )
        a.raw_key_down("1")
        b.raw_key_down("2")
        n = int(0.05 * SR)
        ys = np.concatenate([_block(be, patch, [a, b])["out"] for _ in range(n // F + 2)])
        assert ys[0] == pytest.approx(1.0, abs=1e-3)       # starts at the input
        assert np.all(ys[n:] == -1.0)                       # ends at active
        assert np.all(np.diff(ys[:n]) < 0)

    def test_idle_offsets_the_input_in_both_modes(self):
        for chain in ("add", "override"):
            be, patch, (a, b) = _rig({"key": "1", "active": 2.0},
                                     {"key": "2", "idle": 0.5, "chain": chain})
            a.raw_key_down("1")
            assert np.all(_block(be, patch, [a, b])["out"] == 2.5), chain

    def test_a_voice_aware_input_keeps_its_voices(self):
        be, patch, (k,) = _rig({"key": "Q", "active": 1.0})
        src = patch.add_module("lfo")
        patch.connect(src.id, "cv", k.id, "in")
        k.raw_key_down("Q")
        cv = np.stack([np.zeros(F), np.full(F, 0.5)]).astype(np.float32)
        r = be._render_module(k, F, {(src.id, "cv"): cv}, patch)
        assert r["out"].shape == (2, F)
        assert np.allclose(r["out"][0], 1.0) and np.allclose(r["out"][1], 1.5)

    def test_a_non_finite_input_reads_as_zero(self):
        be, patch, (k,) = _rig({"key": "Q", "idle": 0.25})
        src = patch.add_module("lfo")
        patch.connect(src.id, "cv", k.id, "in")
        cv = np.full(F, np.nan, dtype=np.float32)
        r = be._render_module(k, F, {(src.id, "cv"): cv}, patch)
        assert np.all(r["out"] == 0.25)


def test_on_an_oscillator_the_keys_transpose_by_octaves():
    # Through the real render path: key at +1 on freq_cv (1 V/oct) doubles
    # the pitch.
    patch = Patch()
    k = patch.add_module("key_cv", params={"key": "1", "active": 1.0})
    osc = patch.add_module("oscillator", params={"waveform": "sine", "freq": 220.0, "amp": 0.5})
    spk = patch.add_module("speaker_output")
    patch.connect(k.id, "out", osc.id, "freq_cv")
    patch.connect(osc.id, "out", spk.id, "in")
    be = NumpyBackend(sample_rate=SR, block_size=F)
    be.compile(patch)

    def peak(blocks):
        y = np.concatenate([be.render_block(F)[:, 0] for _ in range(blocks)]).astype(np.float64)
        spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        return np.fft.rfftfreq(len(y), 1.0 / SR)[spec.argmax()]

    assert peak(40) == pytest.approx(220.0, rel=0.01)
    k.raw_key_down("1")
    be.render_block(F)
    assert peak(40) == pytest.approx(440.0, rel=0.01)


# ----- UI (dpg mocked) ------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    a = app_mod.App()
    a.backend = NumpyBackend(sample_rate=SR, block_size=F)
    a.patch = Patch()
    return a, app_mod


def test_ui_raw_keys_reach_key_cv(app):
    a, _ = app
    k = a.patch.add_module("key_cv", params={"key": "Q"})
    a._dispatch_raw_key("Q", down=True)
    assert k.snapshot() == (True, 1)


def test_ui_learn_binds_key_cv(app):
    a, _ = app
    k = a.patch.add_module("key_cv", params={"key": ""})
    a._on_key_learn(None, None, k.id)
    a._bind_learned_key("7")
    assert k.params["key"] == "7"


def test_ui_node_has_learn_button_and_its_own_combos(app):
    a, app_mod = app
    k = a.patch.add_module("key_cv")
    dpg = app_mod.dpg
    b0 = len(dpg.add_button.call_args_list)
    c0 = len(dpg.add_combo.call_args_list)
    d0 = len(dpg.add_drag_float.call_args_list)
    i0 = len(dpg.add_input_text.call_args_list)
    a._create_node_for_module(k)
    tags = [c.kwargs.get("tag") for c in dpg.add_button.call_args_list[b0:]]
    assert f"keytrigger_learnbtn_{k.id}" in tags
    combos = {str(c.kwargs.get("label")): list(c.kwargs.get("items") or [])
              for c in dpg.add_combo.call_args_list[c0:]}
    assert combos.get("mode") == list(KEY_CV_MODES)
    assert combos.get("chain") == list(KEY_CV_CHAINS)
    drags = [str(c.kwargs.get("label")) for c in dpg.add_drag_float.call_args_list[d0:]]
    assert "idle" in drags and "active" in drags and any("glide" in d for d in drags)
    # The key is the Learn panel, never a raw text box.
    assert not any(c.kwargs.get("label") == "key"
                   for c in dpg.add_input_text.call_args_list[i0:])


# ----- units: semitones (2026-11-20) -----------------------------------------


class TestSemitones:
    """``units = semitones``: idle / active read as semitones, divided by 12
    exactly. Matthew: "so .01667 is the value for correct pitch addition" --
    with semitones he types 2 for a whole tone and nothing rounds."""

    def test_default_is_cv(self):
        from pysynthrack.modules.key_cv import KEY_CV_UNITS
        assert KEY_CV_UNITS == ("cv", "semitones")
        assert all_module_types()["key_cv"](1).params["units"] == "cv"

    @pytest.mark.parametrize("st", [1.0, 2.0, 7.0, 12.0, -12.0, 7.02, 3.86])
    def test_divided_by_twelve_exactly(self, st):
        be, patch, (k,) = _rig({"key": "Q", "units": "semitones", "active": st})
        k.raw_key_down("Q")
        assert np.all(_block(be, patch, [k])["out"] == np.float32(st / 12.0))

    def test_stacked_whole_tones_land_on_the_notes(self):
        be, patch, keys = _rig(*({"key": c, "units": "semitones", "active": 2.0} for c in "QWER"))
        for k, c in zip(keys, "QWER"):
            k.raw_key_down(c)
        out = _block(be, patch, keys)["out"]
        assert np.allclose(out, 8.0 / 12.0, rtol=0, atol=1e-7)   # four whole tones

    def test_idle_is_in_semitones_too_and_in_stays_cv(self):
        be, patch, (a, b) = _rig({"key": "1", "active": 1.0},
                                 {"key": "2", "units": "semitones", "idle": -12.0})
        a.raw_key_down("1")
        assert np.allclose(_block(be, patch, [a, b])["out"], 0.0, atol=1e-7)  # +1 oct - 12 st

    @pytest.fixture
    def app(self, monkeypatch):
        pytest.importorskip("dearpygui.dearpygui")
        import itertools

        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
        ids = itertools.count(5000)
        app_mod.dpg.last_item.side_effect = lambda: next(ids)
        a = app_mod.App()
        a.backend = NumpyBackend(sample_rate=SR, block_size=F)
        a.patch = Patch()
        return a, app_mod

    def test_switching_units_keeps_the_output(self, app):
        a, app_mod = app
        k = a.patch.add_module("key_cv", params={"idle": 0.25, "active": 0.5})
        a._create_node_for_module(k)
        a._on_key_cv_units_changed(None, "semitones", (k.id, "units"))
        assert k.params["units"] == "semitones"
        assert k.params["idle"] == 3.0 and k.params["active"] == 6.0
        a._on_key_cv_units_changed(None, "cv", (k.id, "units"))
        assert k.params["idle"] == 0.25 and k.params["active"] == 0.5

    def test_the_drags_follow_the_units(self, app):
        a, app_mod = app
        k = a.patch.add_module("key_cv")
        a._create_node_for_module(k)
        a._on_key_cv_units_changed(None, "semitones", (k.id, "units"))
        wid = a._param_widget(k.id, "active")
        app_mod.dpg.configure_item.assert_any_call(
            wid, speed=0.05, min_value=-120.0, max_value=120.0, format="%.2f st")

    def test_the_node_has_the_units_combo(self, app):
        a, app_mod = app
        from pysynthrack.modules.key_cv import KEY_CV_UNITS
        c0 = len(app_mod.dpg.add_combo.call_args_list)
        k = a.patch.add_module("key_cv", params={"units": "semitones"})
        a._create_node_for_module(k)
        combos = {c.kwargs.get("label"): list(c.kwargs.get("items") or [])
                  for c in app_mod.dpg.add_combo.call_args_list[c0:]}
        assert combos["units"] == list(KEY_CV_UNITS)
        drags = {c.kwargs.get("label"): c.kwargs for c in app_mod.dpg.add_drag_float.call_args_list}
        assert drags["active"]["format"] == "%.2f st"
