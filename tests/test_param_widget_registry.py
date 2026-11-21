"""The param-widget registry across a patch reload and a module delete.

``App._param_widgets`` maps each generic param widget's dpg id to its
``(module_id, param_name)``. ``_clear_editor`` deletes every widget in the
editor, so the registry must be emptied with it; otherwise a reloaded patch
that reuses a module id (File > Open, the auto-loaded default patch) makes
``_param_widget`` return the dead id first, and ``_mirror_param`` updates the
model and the audio but never moves the slider. Deleting one module must drop
that module's entries for the same reason.

DearPyGui is mocked so this runs without a window. Skips cleanly where
DearPyGui can't be imported.
"""
from __future__ import annotations

import itertools
from unittest import mock

import pytest

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.ui.app as app_mod  # noqa: E402


def _mock_dpg():
    """A dpg stand-in whose ``last_item`` and ``node`` hand out fresh ids, so
    every param widget and node gets a distinct id (a bare MagicMock returns
    one object for all of them)."""
    dpg = mock.MagicMock()
    ids = itertools.count(1000)
    dpg.last_item.side_effect = lambda *a, **k: next(ids)
    nodes = itertools.count(1)

    def _node(*a, **k):
        cm = mock.MagicMock()
        cm.__enter__.return_value = next(nodes)
        return cm

    dpg.node.side_effect = _node
    dpg.get_selected_links.return_value = []
    return dpg


def _make_app(monkeypatch):
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", _mock_dpg())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    return app


def _amp_widgets(app, module_id):
    return [w for w, k in app._param_widgets.items() if k == (module_id, "amp")]


def test_reload_with_a_reused_module_id_mirrors_into_the_new_widget(monkeypatch):
    app = _make_app(monkeypatch)
    first = app.patch.add_module("oscillator")
    app._create_node_for_module(first)
    (old_wid,) = _amp_widgets(app, first.id)

    # What a patch load does: wipe the editor, then build a fresh patch whose
    # oscillator gets the same module id.
    app._clear_editor()
    assert app._param_widgets == {}
    app.patch = Patch()
    second = app.patch.add_module("oscillator")
    assert second.id == first.id
    app._create_node_for_module(second)

    (new_wid,) = _amp_widgets(app, second.id)
    assert new_wid != old_wid
    assert app._param_widget(second.id, "amp") == new_wid

    dpg = app_mod.dpg
    dpg.set_value.reset_mock()
    app._mirror_param(second.id, "amp", 0.1)
    assert second.params["amp"] == 0.1
    targets = [c.args[0] for c in dpg.set_value.call_args_list]
    assert new_wid in targets
    assert old_wid not in targets


def test_clear_editor_also_empties_the_selector_panel_maps(monkeypatch):
    app = _make_app(monkeypatch)
    app._psel_cells[7] = [1, 2, 3]
    app._psel_count_labels[7] = 4
    app._clear_editor()
    assert app._psel_cells == {}
    assert app._psel_count_labels == {}


def test_deleting_a_module_drops_its_param_widgets_only(monkeypatch):
    app = _make_app(monkeypatch)
    keep = app.patch.add_module("oscillator")
    gone = app.patch.add_module("oscillator")
    app._create_node_for_module(keep)
    app._create_node_for_module(gone)
    assert _amp_widgets(app, gone.id)

    node = app._module_to_node[gone.id]
    app_mod.dpg.get_selected_nodes.return_value = [node]
    app._on_delete_selected(None, None)

    assert gone.id not in app.patch.modules
    assert [k for k in app._param_widgets.values() if k[0] == gone.id] == []
    assert _amp_widgets(app, keep.id)
