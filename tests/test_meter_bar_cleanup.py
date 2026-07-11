"""Regression tests for the CV-meter 'Item not found' GUI crash.

Deleting a node freed its meter bar drawlist items, but ``_on_delete_selected``
did not prune the ``(module_id, port) -> bar`` maps, so the next
``_update_cv_meters`` frame called ``dpg.set_value`` on a dead item and the
whole GUI loop went down. These drive the GUI glue headlessly (dpg mocked):
the delete path must prune the meter maps, and the meter loop must survive a
stale bar rather than crash.
"""
from __future__ import annotations

from unittest import mock

import pytest

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.modules  # noqa: F401  (registers module types)
import pysynthrack.ui.app as app_mod
from pysynthrack.core import Patch


def _make_app(monkeypatch):
    """A headless App with dpg mocked and a stopped mock backend."""
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = mock.MagicMock()
    app.backend.is_running = False  # so _recompile_if_running no-ops
    app.patch = Patch()
    return app, app_mod.dpg


def test_delete_prunes_meter_maps(monkeypatch):
    app, dpg = _make_app(monkeypatch)

    # Two modules; only the first is deleted.
    victim = app.patch.add_module("lfo")
    keep = app.patch.add_module("lfo")
    vnode, knode = 5001, 5002
    app._node_to_module = {vnode: victim.id, knode: keep.id}
    app._module_to_node = {victim.id: vnode, keep.id: knode}

    # Meter bookkeeping for both.
    app._cv_meter_bars = {(victim.id, "out"): 111, (keep.id, "out"): 222}
    app._audio_meter_bars = {victim.id: {"l": {}}, keep.id: {"l": {}}}
    app._meter_bounds = {(victim.id, "out"): [0.0, 1.0], (keep.id, "out"): [0.0, 1.0]}

    dpg.get_selected_links.return_value = []
    dpg.get_selected_nodes.return_value = [vnode]

    app._on_delete_selected(None, None)

    # Victim's entries gone from every meter map...
    assert (victim.id, "out") not in app._cv_meter_bars
    assert victim.id not in app._audio_meter_bars
    assert (victim.id, "out") not in app._meter_bounds
    # ...the other module's untouched.
    assert (keep.id, "out") in app._cv_meter_bars
    assert keep.id in app._audio_meter_bars
    assert (keep.id, "out") in app._meter_bounds


def test_update_cv_meters_survives_stale_bar(monkeypatch):
    app, dpg = _make_app(monkeypatch)

    good, stale = (14, "out"), (13, "out")
    app._cv_meter_bars = {stale: 999, good: 1000}
    app.backend.snapshot_meter_levels = lambda: {stale: 0.5, good: 0.5}

    # set_value raises for the freed bar (999), like the real dpg does.
    def set_value(item, value, **kw):
        if item == 999:
            raise Exception("Item not found: 999")

    dpg.set_value.side_effect = set_value

    app._update_cv_meters()  # must not raise

    # The stale entry self-heals (pruned); the live one stays.
    assert stale not in app._cv_meter_bars
    assert good in app._cv_meter_bars


def test_update_cv_meters_noop_without_bars(monkeypatch):
    app, _ = _make_app(monkeypatch)
    app._cv_meter_bars = {}
    # Should return immediately without touching the backend snapshot.
    app.backend.snapshot_meter_levels = mock.MagicMock(
        side_effect=AssertionError("should not be called")
    )
    app._update_cv_meters()
