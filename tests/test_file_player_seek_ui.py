"""End-to-end tests for the FilePlayer seek / scrub bar GUI glue.

Like ``test_file_player_queue`` these drive the UI glue headlessly:
DearPyGui is imported but ``app.dpg`` is swapped for a mock, so no window
or display is needed, while the backend is a real ``NumpyBackend`` whose
playhead is advanced by rendering blocks exactly as the audio callback
would. That lets the seek bar's three-way per-frame behaviour — reflect the
playhead when idle, keep hands off the thumb while dragging, commit the
seek to the backend on release — be exercised against real state rather
than mocked.

The whole file skips cleanly where DearPyGui can't be imported.
"""
from __future__ import annotations

from unittest import mock

import numpy as np
import pytest
from scipy.io import wavfile

# GUI import guard: skip the module rather than error where dpg is absent.
pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.modules  # noqa: F401  (registers module types)
import pysynthrack.ui.app as app_mod
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100


def _write_ramp(path, n=2000, sr=SR):
    left = (np.arange(n) / n).astype(np.float32)
    data = np.stack(
        [(left * 30000).astype(np.int16), (-left * 30000).astype(np.int16)],
        axis=1,
    )
    wavfile.write(str(path), sr, data)


def _make_app(monkeypatch, backend):
    """A headless App with dpg mocked and a numpy backend forced in."""
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = backend
    app.patch = Patch()
    return app


def _add_seek_player(app, be, tmp_path, n=2000, **params):
    """Register a compiled file_player wired to fake seek-bar / readout ids."""
    wav = tmp_path / "ramp.wav"
    _write_ramp(wav, n=n)
    fp = app.patch.add_module("file_player", params={"path": str(wav), **params})
    # Fake dpg item ids; distinct so a set_value can be attributed to one.
    app._file_pos_labels[fp.id] = 20_000 + fp.id
    app._file_seek_sliders[fp.id] = 30_000 + fp.id
    be.compile(app.patch)
    assert be.wait_for_file_decodes()
    return fp


def _slider_set_values(app, slider):
    """Every value dpg.set_value was asked to write to ``slider``."""
    return [
        c.args[1]
        for c in app_mod.dpg.set_value.call_args_list
        if c.args and c.args[0] == slider
    ]


def test_idle_thumb_reflects_playhead(tmp_path, monkeypatch):
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_seek_player(app, be, tmp_path, n=2000)
    be._render_module(fp, 512, {}, app.patch)
    be._render_module(fp, 512, {}, app.patch)  # pos = 1024 of 2000

    app_mod.dpg.is_item_active.return_value = False
    app_mod.dpg.set_value.reset_mock()
    app._update_file_positions()

    slider = app._file_seek_sliders[fp.id]
    writes = _slider_set_values(app, slider)
    assert writes == [pytest.approx(1024 / 2000, abs=1e-6)]  # fill == elapsed/total
    assert fp.id not in app._file_seek_active


def test_dragging_leaves_thumb_and_does_not_seek(tmp_path, monkeypatch):
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_seek_player(app, be, tmp_path, n=2000)
    be._render_module(fp, 512, {}, app.patch)  # pos = 512

    app_mod.dpg.is_item_active.return_value = True
    app_mod.dpg.set_value.reset_mock()
    app._update_file_positions()

    slider = app._file_seek_sliders[fp.id]
    assert _slider_set_values(app, slider) == []   # thumb left for the mouse
    assert fp.id in app._file_seek_active           # scrub flagged
    assert be._state[fp.id].get("seek") is None     # nothing committed mid-drag


def test_release_commits_seek_to_backend(tmp_path, monkeypatch):
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_seek_player(app, be, tmp_path, n=2000)

    # Frame 1: actively dragging -> flagged, thumb untouched.
    app_mod.dpg.is_item_active.return_value = True
    app._update_file_positions()
    assert fp.id in app._file_seek_active

    # Frame 2: released with the thumb left at 0.75 -> seek committed.
    app_mod.dpg.is_item_active.return_value = False
    app_mod.dpg.get_value.return_value = 0.75
    app._update_file_positions()

    assert fp.id not in app._file_seek_active
    assert be._state[fp.id]["seek"] == int(0.75 * 2000)  # 1500


def test_quick_click_commits_via_callback_flag(tmp_path, monkeypatch):
    """A click that lands and releases between two frame polls is never seen
    as ``is_item_active``; the ``_on_file_seek`` callback flag is what makes
    the release check still commit it."""
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_seek_player(app, be, tmp_path, n=2000)

    app._on_file_seek(None, None, fp.id)     # the slider callback fired
    assert fp.id in app._file_seek_active

    app_mod.dpg.is_item_active.return_value = False
    app_mod.dpg.get_value.return_value = 0.25
    app._update_file_positions()

    assert fp.id not in app._file_seek_active
    assert be._state[fp.id]["seek"] == 500


def test_seek_bar_survives_node_teardown(tmp_path, monkeypatch):
    """Deleting the node drops the seek-bar bookkeeping so a later frame
    can't drive a freed slider (the 'Item not found' class of GUI crash)."""
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_seek_player(app, be, tmp_path, n=2000)
    app._file_seek_active.add(fp.id)

    app._file_seek_sliders.pop(fp.id, None)
    app._file_seek_active.discard(fp.id)
    app._file_pos_labels.pop(fp.id, None)

    # No slider registered -> _update_file_positions has nothing to poke.
    app_mod.dpg.set_value.reset_mock()
    app._update_file_positions()
    assert app_mod.dpg.set_value.call_count == 0
