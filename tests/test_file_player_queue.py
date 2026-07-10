"""End-to-end tests for the FilePlayer queue ("file list") auto-advance.

These drive the *GUI glue* (``App._advance_file_playlists`` and friends)
headlessly: DearPyGui is imported but ``app.dpg`` is swapped for a mock, so
no window or display is needed. The backend is a real ``NumpyBackend`` and
tracks are consumed by rendering blocks exactly as the audio callback would,
so the "current track finished → next loads → drops off the list" flow is
exercised for real rather than mocked.

The whole file skips cleanly where DearPyGui can't be imported (e.g. a
headless CI without the wheel), since it's the only GUI-touching test module.
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


def _write_ramp(path, n=1000, sr=SR):
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


def _add_player(app, **params):
    fp = app.patch.add_module("file_player", params=params)
    # A fake listbox tag registers the node as a queue the advancer polls.
    app._playlist_listboxes[fp.id] = 10_000 + fp.id
    return fp


def _play_to_end(be, fp, patch, blocks=2, frames=512):
    for _ in range(blocks):
        be._render_module(fp, frames, {}, patch)


def test_finished_track_advances_to_next_and_removes_it(tmp_path, monkeypatch):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _write_ramp(a, n=1000)
    _write_ramp(b, n=1000)
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_player(app, path=str(a), playlist=[str(b)])
    be.compile(app.patch)
    be._running = True
    assert be.wait_for_file_decodes()

    # Track A plays out; the advancer sees the rising 'finished' edge.
    _play_to_end(be, fp, app.patch)
    assert be.file_player_finished(fp.id) is True
    app._advance_file_playlists()

    assert fp.params["path"] == str(b)     # B is now the current track
    assert fp.params["playlist"] == []     # ...and was removed from the list

    # The path change kicks B's decode on the next render (silent that block
    # per the prebuffer gate); wait for the decode, then B plays from 0:00.
    be._render_module(fp, 512, {}, app.patch)      # kicks B's decoder
    assert be.wait_for_file_decodes()
    b_play = be._render_module(fp, 512, {}, app.patch)
    assert np.any(b_play["left"] != 0.0)           # B is actually sounding


def test_empty_queue_at_end_just_stops(tmp_path, monkeypatch):
    a = tmp_path / "a.wav"
    _write_ramp(a, n=1000)
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_player(app, path=str(a), playlist=[])
    be.compile(app.patch)
    be._running = True
    assert be.wait_for_file_decodes()

    _play_to_end(be, fp, app.patch)
    assert be.file_player_finished(fp.id) is True
    app._advance_file_playlists()  # nothing queued: must not raise or change path

    assert fp.params["path"] == str(a)
    assert fp.params["playlist"] == []
    tail = be._render_module(fp, 512, {}, app.patch)
    assert np.all(tail["left"] == 0.0)  # parked at the end -> silence


def test_advance_is_edge_triggered_not_per_frame(tmp_path, monkeypatch):
    """A finished track must consume exactly one queued item, even though
    'finished' stays true for several frames until the reload lands."""
    a, b, c = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "c.wav"
    for p in (a, b, c):
        _write_ramp(p, n=1000)
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_player(app, path=str(a), playlist=[str(b), str(c)])
    be.compile(app.patch)
    be._running = True
    assert be.wait_for_file_decodes()

    _play_to_end(be, fp, app.patch)
    # Tick several times WITHOUT rendering in between: state still shows A
    # finished, so a per-frame (level) trigger would wrongly eat B *and* C.
    app._advance_file_playlists()
    app._advance_file_playlists()
    app._advance_file_playlists()

    assert fp.params["path"] == str(b)      # only one hop
    assert fp.params["playlist"] == [str(c)]  # C still waiting


def test_empty_path_with_queue_kickstarts_only_when_running(tmp_path, monkeypatch):
    a = tmp_path / "a.wav"
    _write_ramp(a, n=1000)
    be = NumpyBackend(sample_rate=SR, block_size=512)
    app = _make_app(monkeypatch, be)
    fp = _add_player(app, path="", playlist=[str(a)])
    be.compile(app.patch)

    # Stopped: the queue must be left untouched (don't consume before Start).
    be._running = False
    app._advance_file_playlists()
    assert fp.params["path"] == ""
    assert fp.params["playlist"] == [str(a)]

    # Running: an empty-path player with a queue kick-starts its first track.
    be._running = True
    app._advance_file_playlists()
    assert fp.params["path"] == str(a)
    assert fp.params["playlist"] == []
