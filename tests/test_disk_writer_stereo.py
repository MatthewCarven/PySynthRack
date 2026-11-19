"""disk_writer's stereo jack: ``in`` + ``in_r`` -> ONE two-channel WAV.

Matthew, 2026-11-19, picking the first item from docs/stereo-scope.md. Two
mono writers recording left and right each have their own queue; when the
disk falls behind, each drops blocks on its own, so the two files part by a
whole block and never rejoin. One stereo writer puts each block on one
queue as a (frames, 2) array: a drop loses both sides of that block or
neither, so the channels cannot go out of step.

The drop tests hold the worker back (a gated worker) so the 64-block queue
overflows on purpose. Each block's left carries +k and its right -k for
block k, so "L == -R in every frame" is "the channels never parted", and
the value tells which block a frame came from.
"""
from __future__ import annotations

import threading
import time
import wave
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.ui.buffer import format_disk_writer_status

SR = 44100
F = 256
QUEUE = 64          # the writer's queue depth (renderers/io.py)


def _rig(path, wire_left=True, wire_right=True, **params):
    patch = Patch()
    src_l = patch.add_module("oscillator")
    src_r = patch.add_module("oscillator")
    dw = patch.add_module("disk_writer", params={"path": str(path), "armed": True, **params})
    if wire_left:
        patch.connect(src_l.id, "out", dw.id, "in")
    if wire_right:
        patch.connect(src_r.id, "out", dw.id, "in_r")
    be = NumpyBackend(sample_rate=SR, block_size=F)
    be.compile(patch)
    return be, patch, dw, src_l, src_r


def _push(rig, left, right):
    be, patch, dw, src_l, src_r = rig
    bufs = {}
    if left is not None:
        bufs[(src_l.id, "out")] = np.asarray(left, dtype=np.float32)
    if right is not None:
        bufs[(src_r.id, "out")] = np.asarray(right, dtype=np.float32)
    be._render_disk_writer(dw, F, bufs, patch)


def _drain_and_close(be, dw, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = be._state.get(dw.id, {}).get("queue")
        if q is None or q.empty():
            break
        time.sleep(0.01)
    be._close_disk_writer_state(be._state[dw.id])


def _read(path):
    with wave.open(str(path), "rb") as wf:
        ch, n = wf.getnchannels(), wf.getnframes()
        data = np.frombuffer(wf.readframes(n), dtype=np.int16)
    return ch, data.reshape(-1, ch) if ch > 1 else data


def _gate_workers(monkeypatch):
    """Hold every writer's worker until the returned event is set."""
    release = threading.Event()
    real = NumpyBackend._disk_writer_worker

    def gated(*args):
        release.wait(10)
        real(*args)

    monkeypatch.setattr(NumpyBackend, "_disk_writer_worker", staticmethod(gated))
    return release


def _code(k):
    """Block k's level: distinct per block, exact in 16 bits."""
    return (k + 1) / 400.0


class TestModel:
    def test_jacks(self):
        dw = Patch().add_module("disk_writer")
        assert [(p.name, p.signal_kind) for p in dw.input_ports] == [
            ("in", "audio"), ("in_r", "audio")]
        assert dw.output_ports == []


class TestStereoTake:
    def test_one_two_channel_file_each_side_exact(self, tmp_path):
        path = tmp_path / "st.wav"
        rig = _rig(path)
        rng = np.random.default_rng(0)
        L = (0.5 * rng.standard_normal((20, F))).clip(-1, 1)
        R = (0.5 * rng.standard_normal((20, F))).clip(-1, 1)
        for k in range(20):
            _push(rig, L[k], R[k])
        _drain_and_close(rig[0], rig[2])
        ch, data = _read(path)
        assert ch == 2 and data.shape == (20 * F, 2)
        want_l = (np.clip(L.astype(np.float32).ravel(), -1, 1) * 32767.0).astype(np.int16)
        want_r = (np.clip(R.astype(np.float32).ravel(), -1, 1) * 32767.0).astype(np.int16)
        np.testing.assert_array_equal(data[:, 0], want_l)
        np.testing.assert_array_equal(data[:, 1], want_r)

    def test_a_missing_side_records_silence(self, tmp_path):
        path = tmp_path / "right_only.wav"
        rig = _rig(path, wire_left=False)
        for _ in range(4):
            _push(rig, None, np.full(F, 0.25))
        _drain_and_close(rig[0], rig[2])
        ch, data = _read(path)
        assert ch == 2
        assert not data[:, 0].any()
        assert np.all(data[:, 1] == int(0.25 * 32767))

    def test_in_r_empty_is_the_mono_writer(self, tmp_path):
        path = tmp_path / "mono.wav"
        rig = _rig(path, wire_right=False)
        for _ in range(5):
            _push(rig, np.full(F, 0.5), None)
        _drain_and_close(rig[0], rig[2])
        ch, data = _read(path)
        assert ch == 1 and data.shape == (5 * F,)


class TestDrops:
    def test_two_mono_writers_part_when_they_drop_differently(self, tmp_path, monkeypatch):
        # Exposure: the defect the stereo jack exists for. Left's disk keeps
        # up; right's stalls for a while (blocks 64..99 are dropped), then
        # catches up and the take goes on. From the gap on, the right file
        # is 36 blocks ahead of the left -- for the rest of the take.
        real = NumpyBackend._disk_writer_worker
        gates = {}

        def gate(path):
            return gates.setdefault(str(path), threading.Event())

        def gated(q, stop_event, target, *rest):
            gate(target).wait(10)
            real(q, stop_event, target, *rest)

        monkeypatch.setattr(NumpyBackend, "_disk_writer_worker", staticmethod(gated))
        pl, pr = tmp_path / "left.wav", tmp_path / "right.wav"
        gate(pl).set()                                   # left's disk keeps up
        rl = _rig(pl, wire_right=False)
        rr = _rig(pr, wire_right=False)
        for k in range(130):
            if k == 100:
                gate(pr).set()                           # right's disk catches up
                time.sleep(0.3)
            if k % 16 == 0:
                time.sleep(0.02)                         # let left's writer breathe
            _push(rl, np.full(F, _code(k)), None)
            _push(rr, np.full(F, _code(k)), None)
        _drain_and_close(rl[0], rl[2])
        _drain_and_close(rr[0], rr[2])
        assert rl[0]._state[rl[2].id]["dropped_blocks"] == 0
        assert rr[0]._state[rr[2].id]["dropped_blocks"] == 36
        _, a = _read(pl)
        _, b = _read(pr)
        at = 70 * F                                      # past the gap
        assert a[at] == int(_code(70) * 32767)           # left: block 70
        assert b[at] == int(_code(106) * 32767)          # right: block 106

    def test_a_stereo_take_drops_both_sides_together(self, tmp_path, monkeypatch):
        release = _gate_workers(monkeypatch)
        path = tmp_path / "dropped.wav"
        rig = _rig(path)
        blocks = QUEUE + 37
        for k in range(blocks):
            _push(rig, np.full(F, _code(k)), np.full(F, -_code(k)))
        st = rig[0]._state[rig[2].id]
        assert st["dropped_blocks"] == blocks - QUEUE
        assert rig[0].snapshot_disk_writers() == {rig[2].id: (2, blocks - QUEUE)}
        release.set()
        _drain_and_close(rig[0], rig[2])
        ch, data = _read(path)
        assert ch == 2 and data.shape == (QUEUE * F, 2)
        np.testing.assert_array_equal(data[:, 0], -data[:, 1])   # never parted

    def test_the_drop_count_is_per_take(self, tmp_path, monkeypatch):
        release = _gate_workers(monkeypatch)
        rig = _rig(tmp_path / "a.wav")
        for _ in range(QUEUE + 5):
            _push(rig, np.full(F, 0.1), np.full(F, -0.1))
        assert rig[0]._state[rig[2].id]["dropped_blocks"] == 5
        release.set()
        _drain_and_close(rig[0], rig[2])
        _push(rig, np.full(F, 0.1), np.full(F, -0.1))            # a new take
        assert rig[0]._state[rig[2].id]["dropped_blocks"] == 0
        _drain_and_close(rig[0], rig[2])


class TestTakes:
    def test_patching_in_r_mid_take_starts_a_new_take(self, tmp_path):
        path = tmp_path / "switch.wav"
        rig = _rig(path, wire_right=False, timestamp=True)
        be, patch, dw, src_l, src_r = rig
        for _ in range(3):
            _push(rig, np.full(F, 0.2), None)
        first = be._state[dw.id]["thread"]
        patch.connect(src_r.id, "out", dw.id, "in_r")
        for _ in range(4):
            _push(rig, np.full(F, 0.2), np.full(F, -0.2))
        assert be._state[dw.id]["thread"] is not first
        _drain_and_close(be, dw)
        files = sorted(tmp_path.glob("switch_*.wav"))
        assert len(files) == 2
        chans = sorted(_read(f)[0] for f in files)
        assert chans == [1, 2]

    def test_snapshot_lists_open_takes_only(self, tmp_path):
        rig = _rig(tmp_path / "s.wav", wire_right=False)
        be, patch, dw, *_ = rig
        assert be.snapshot_disk_writers() == {}
        _push(rig, np.full(F, 0.1), None)
        assert be.snapshot_disk_writers() == {dw.id: (1, 0)}
        _drain_and_close(be, dw)
        assert be.snapshot_disk_writers() == {}


class TestReadout:
    def test_text(self):
        assert format_disk_writer_status(None) == "rec: idle"
        assert format_disk_writer_status((1, 0)) == "rec: mono, dropped 0"
        assert format_disk_writer_status((2, 3)) == "rec: stereo, dropped 3"

    def test_the_node_shows_it_and_the_frame_updates_it(self, monkeypatch, tmp_path):
        pytest.importorskip("dearpygui.dearpygui")
        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
        app = app_mod.App()
        app.backend = NumpyBackend(sample_rate=SR, block_size=F)
        app.patch = Patch()
        dw = app.patch.add_module("disk_writer", params={"path": str(tmp_path / "x.wav")})
        app._create_node_for_module(dw)
        assert dw.id in app._disk_writer_labels
        label = app._disk_writer_labels[dw.id]
        app.backend.snapshot_disk_writers = lambda: {dw.id: (2, 4)}
        app._update_disk_writers()
        app_mod.dpg.set_value.assert_any_call(label, "rec: stereo, dropped 4")
        app_mod.dpg.configure_item.assert_any_call(label, color=app._SINK_BUF_FAILED_RGBA)
        app.backend.snapshot_disk_writers = lambda: {}
        app._update_disk_writers()
        app_mod.dpg.set_value.assert_any_call(label, "rec: idle")
