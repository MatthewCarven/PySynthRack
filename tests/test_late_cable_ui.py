"""The late-cable UI (2026-09-16): amber feedback cables + the ``loops`` readout.

The feedback door reads the cable that closes each loop one block late.
That block of latency should never be a mystery, so:

  - the backend exposes ``feedback_cables(patch)`` (the late set a
    compile would produce -- a pure function of the patch, so the editor
    can colour a loop the moment it is drawn, running or not) and
    ``feedback_scrubs()`` (blocks in which a loop went non-finite and was
    scrubbed);
  - ``App._refresh_late_links`` binds an amber node-link theme to exactly
    the late links and the default theme to every other, on every cable
    or module change and on load/new -- never per frame;
  - the toolbar ``loops`` slot reads ``loops --`` / ``loops N`` / ``loops
    N !K`` (K scrubbed blocks, warning colour) with a tooltip naming the
    late cables;
  - drawing the cable that closes a loop says so in the status bar.

Coverage: the backend observables agree with the compile; a loaded loop
patch paints its one late link amber and the rest default; the readout
and tooltip; the status line on closing a loop; deleting the late link
clears everything; the theme is built once; a backend without the
observables (the pyo stub) leaves the editor untouched; nothing the
readout paints is non-ASCII.
"""
from __future__ import annotations

import itertools
from unittest import mock

import numpy as np
import pytest

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.modules  # noqa: F401
import pysynthrack.ui.app as app_mod
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.io_patch import patch_to_json


class _Ctx:
    """A context manager that yields a distinct id, standing in for
    ``dpg.node_attribute(...)`` / ``dpg.theme()`` / ``dpg.node(...)``."""

    def __init__(self, ident):
        self.ident = ident

    def __enter__(self):
        return self.ident

    def __exit__(self, *exc):
        return False


def _mock_dpg():
    dpg = mock.MagicMock()
    ids = itertools.count(1000)
    for ctx in ("node", "node_attribute", "theme", "theme_component", "group",
                "tooltip", "node_editor", "window", "menu_bar", "menu", "child_window",
                "table", "table_row", "drawlist", "plot"):
        getattr(dpg, ctx).side_effect = lambda *a, **k: _Ctx(next(ids))
    for factory in ("add_node_link", "add_text", "add_button", "add_slider_float",
                    "add_drag_float", "add_combo", "add_checkbox", "add_drag_int",
                    "add_progress_bar", "add_input_text", "add_spacer"):
        getattr(dpg, factory).side_effect = lambda *a, **k: next(ids)
    dpg.does_item_exist.return_value = True
    return dpg


def _loop_patch():
    p = Patch()
    o = p.add_module("oscillator")
    mx = p.add_module("mixer")
    d = p.add_module("delay", params={"feedback": 0.0, "mix": 1.0})
    s = p.add_module("speaker_output")
    p.connect(o.id, "out", mx.id, "in1")
    p.connect(mx.id, "out", d.id, "in")
    p.connect(d.id, "out", s.id, "in")
    p.connect(d.id, "out", mx.id, "in2")        # closes the loop, drawn last
    return p, o, mx, d, s


def _make_app(monkeypatch, tmp_path, patch=None):
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    dpg = _mock_dpg()
    monkeypatch.setattr(app_mod, "dpg", dpg)
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    if patch is not None:
        path = tmp_path / "loop.json"
        path.write_text(patch_to_json(patch), encoding="utf-8")
        app._load_patch_from(str(path))
    return app, dpg


def _bound(dpg):
    """{link_id: theme} from every bind_item_theme call, last one wins."""
    out = {}
    for call in dpg.bind_item_theme.call_args_list:
        item, theme = call.args[:2]
        out[item] = theme
    return out


def _readout(dpg):
    vals = [c.args for c in dpg.set_value.call_args_list if c.args and c.args[0] == app_mod.LOOPS_TEXT_TAG]
    tips = [c.args for c in dpg.set_value.call_args_list if c.args and c.args[0] == app_mod.LOOPS_TOOLTIP_TAG]
    return (vals[-1][1] if vals else None), (tips[-1][1] if tips else None)


def _status(dpg):
    vals = [c.args for c in dpg.set_value.call_args_list if c.args and c.args[0] == app_mod.STATUS_TEXT_TAG]
    return vals[-1][1] if vals else ""


# ----- backend observables --------------------------------------------------------


class TestBackendObservables:
    def test_feedback_cables_is_the_compile_s_late_set(self):
        p, o, mx, d, s = _loop_patch()
        b = NumpyBackend(sample_rate=48000, block_size=512)
        before = b.feedback_cables(p)            # no compile yet
        b.compile(p)
        assert before == b._late_edges == {(d.id, "out", mx.id, "in2")}
        assert b.feedback_cables(Patch()) == set()

    def test_feedback_scrubs_counts_a_runaway(self):
        p = Patch()
        m = p.add_module("mixer", params={"gain1": 1.0, "gain2": 2.0, "master": 1.0})
        o = p.add_module("oscillator", params={"amp": 0.5})
        p.connect(o.id, "out", m.id, "in1"); p.connect(m.id, "out", m.id, "in2")
        b = NumpyBackend(sample_rate=48000, block_size=512)
        b.compile(p)
        assert b.feedback_scrubs() == 0
        with np.errstate(over="ignore", invalid="ignore"):
            for _ in range(300):
                b.render_block_multi(512)
        assert b.feedback_scrubs() > 0


# ----- the editor ----------------------------------------------------------------------


class TestAmberLinks:
    def test_a_loaded_loop_paints_exactly_the_late_link_amber(self, monkeypatch, tmp_path):
        p, o, mx, d, s = _loop_patch()
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        assert len(app._link_to_cable) == 4
        assert app._late_keys == {(d.id, "out", mx.id, "in2")}
        theme = app._late_link_theme
        assert theme is not None
        bound = _bound(dpg)
        late_links = [lid for lid, c in app._link_to_cable.items() if app._cable_key(c) in app._late_keys]
        assert len(late_links) == 1
        assert bound[late_links[0]] == theme
        for lid in app._link_to_cable:
            if lid != late_links[0]:
                assert bound[lid] == 0
        # the theme itself: amber link colour in the nodes category
        colours = [c.args for c in dpg.add_theme_color.call_args_list]
        assert any(a[0] == dpg.mvNodeCol_Link and a[1] == app_mod.LATE_LINK_COLOR for a in colours)

    def test_readout_and_tooltip_name_the_cable(self, monkeypatch, tmp_path):
        p, o, mx, d, s = _loop_patch()
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        text, tip = _readout(dpg)
        assert text == "loops 1"
        assert f"delay#{d.id}.out -> mixer#{mx.id}.in2" in tip
        assert "one block late" in tip
        assert text.isascii() and tip.isascii()

    def test_no_loop_reads_dashes(self, monkeypatch, tmp_path):
        p = Patch()
        o = p.add_module("oscillator"); s = p.add_module("speaker_output")
        p.connect(o.id, "out", s.id, "in")
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        text, tip = _readout(dpg)
        assert text == "loops --"
        assert "No feedback loops" in tip
        assert app._late_link_theme is None          # never built when nothing is late

    def test_drawing_the_closing_cable_says_so(self, monkeypatch, tmp_path):
        p = Patch()
        o = p.add_module("oscillator"); mx = p.add_module("mixer"); d = p.add_module("delay")
        p.connect(o.id, "out", mx.id, "in1"); p.connect(mx.id, "out", d.id, "in")
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        assert _readout(dpg)[0] == "loops --"
        out_attr = app._port_to_attr[(d.id, "out", "out")]
        in_attr = app._port_to_attr[(mx.id, "in2", "in")]
        app._on_link_created(None, (out_attr, in_attr))
        assert app._late_keys == {(d.id, "out", mx.id, "in2")}
        assert "Loop closed" in _status(dpg) and "one block late" in _status(dpg)
        assert _readout(dpg)[0] == "loops 1"
        # and a cable that does NOT close a loop stays quiet about it
        s = app.patch.add_module("speaker_output")
        app._create_node_for_module(s)
        app._on_link_created(None, (app._port_to_attr[(d.id, "out", "out")], app._port_to_attr[(s.id, "in", "in")]))
        loop_msgs = [c for c in dpg.set_value.call_args_list
                     if c.args and c.args[0] == app_mod.STATUS_TEXT_TAG and "Loop closed" in str(c.args[1])]
        assert len(loop_msgs) == 1                    # the closing cable said so; the plain one did not
        assert _readout(dpg)[0] == "loops 1"

    def test_deleting_the_late_link_clears_everything(self, monkeypatch, tmp_path):
        p, o, mx, d, s = _loop_patch()
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        late_link = next(lid for lid, c in app._link_to_cable.items() if app._cable_key(c) in app._late_keys)
        app._on_link_deleted(None, late_link)
        assert app._late_keys == set()
        assert _readout(dpg)[0] == "loops --"
        for lid in app._link_to_cable:
            assert _bound(dpg)[lid] == 0

    def test_the_theme_is_built_once(self, monkeypatch, tmp_path):
        p, o, mx, d, s = _loop_patch()
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        n = dpg.theme.call_count
        app._refresh_late_links()
        app._refresh_late_links()
        assert dpg.theme.call_count == n == 1

    def test_scrubs_turn_the_readout_amber_while_running(self, monkeypatch, tmp_path):
        p, o, mx, d, s = _loop_patch()
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        monkeypatch.setattr(type(app.backend), "is_running", property(lambda self: True))
        app.backend._late_nonfinite = 3
        app._update_loops_readout()
        text, tip = _readout(dpg)
        assert text == "loops 1 !3"
        assert "scrubbed" in tip and "limiter" in tip
        colours = [c.kwargs.get("color") for c in dpg.configure_item.call_args_list
                   if c.args and c.args[0] == app_mod.LOOPS_TEXT_TAG]
        assert colours[-1] == app_mod.WARN_COLOR

    def test_a_backend_without_the_observables_leaves_the_editor_alone(self, monkeypatch, tmp_path):
        p, o, mx, d, s = _loop_patch()
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        bare = mock.MagicMock(spec=[])            # no feedback_cables, no is_running
        bare.is_running = False
        app.backend = bare
        dpg.bind_item_theme.reset_mock()
        app._refresh_late_links()
        assert app._late_keys == set()
        for call in dpg.bind_item_theme.call_args_list:
            assert call.args[1] == 0
        assert _readout(dpg)[0] == "loops --"

    def test_new_patch_resets_the_readout(self, monkeypatch, tmp_path):
        p, o, mx, d, s = _loop_patch()
        app, dpg = _make_app(monkeypatch, tmp_path, p)
        assert _readout(dpg)[0] == "loops 1"
        app._on_new()
        assert app._late_keys == set()
        assert _readout(dpg)[0] == "loops --"


# ----- the sink scrub's status line ---------------------------------------------------


class TestSinkScrubStatus:
    def _running(self, monkeypatch, app, n):
        monkeypatch.setattr(type(app.backend), "is_running", property(lambda self: True))
        app.backend._sink_nonfinite = n

    def test_a_scrub_posts_a_status_line(self, monkeypatch, tmp_path):
        app, dpg = _make_app(monkeypatch, tmp_path)
        self._running(monkeypatch, app, 4)
        app._update_sink_scrubs()
        msg = _status(dpg)
        assert "4 block(s)" in msg and "NaN/inf" in msg and "silenced" in msg
        assert all(ord(ch) < 128 for ch in msg)   # DPG paints above U+00FF as "?"

    def test_it_posts_only_when_the_count_rises(self, monkeypatch, tmp_path):
        app, dpg = _make_app(monkeypatch, tmp_path)
        self._running(monkeypatch, app, 2)
        app._update_sink_scrubs()
        dpg.set_value.reset_mock()
        app._update_sink_scrubs()                 # unchanged: leave the bar alone
        assert _status(dpg) == ""
        app.backend._sink_nonfinite = 3
        app._update_sink_scrubs()
        assert "3 block(s)" in _status(dpg)

    def test_a_clean_run_says_nothing(self, monkeypatch, tmp_path):
        app, dpg = _make_app(monkeypatch, tmp_path)
        self._running(monkeypatch, app, 0)
        dpg.set_value.reset_mock()
        app._update_sink_scrubs()
        assert _status(dpg) == ""

    def test_a_fresh_backend_is_reported_from_zero(self, monkeypatch, tmp_path):
        app, dpg = _make_app(monkeypatch, tmp_path)
        self._running(monkeypatch, app, 9)
        app._update_sink_scrubs()
        app.backend._sink_nonfinite = 1           # a new backend counting again
        app._update_sink_scrubs()
        assert "1 block(s)" in _status(dpg)

    def test_the_dsp_tick_drives_it(self, monkeypatch, tmp_path):
        app, dpg = _make_app(monkeypatch, tmp_path)
        self._running(monkeypatch, app, 1)
        app._update_dsp_load()
        assert "NaN/inf" in _status(dpg)

    def test_a_backend_without_the_observable_is_left_alone(self, monkeypatch, tmp_path):
        app, dpg = _make_app(monkeypatch, tmp_path)
        bare = mock.MagicMock(spec=[])
        bare.is_running = True
        app.backend = bare
        dpg.set_value.reset_mock()
        app._update_sink_scrubs()
        assert _status(dpg) == ""
