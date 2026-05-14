"""Tests for the WAV DiskWriter sink module."""
from __future__ import annotations

import os
import tempfile
import time
import wave

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.diskwriter import DiskWriter


SR = 44100


def _wait_for_drain(backend, module_id, timeout=2.0):
    """Wait until the writer's queue empties (worker has caught up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = backend._state.get(module_id, {})
        q = state.get("queue")
        if q is None or q.empty():
            return
        time.sleep(0.01)


class TestDiskWriterModel:
    def test_register_and_defaults(self):
        patch = Patch()
        dw = patch.add_module("disk_writer")
        assert isinstance(dw, DiskWriter)
        assert dw.params == {"path": "recording.wav", "armed": True}
        assert [p.name for p in dw.input_ports] == ["in"]
        assert dw.input_ports[0].signal_kind == "audio"
        assert dw.output_ports == []  # it's a sink

    def test_rejects_cv_into_audio_input(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        dw = patch.add_module("disk_writer")
        try:
            patch.connect(lfo.id, "cv", dw.id, "in")
        except ValueError:
            return
        raise AssertionError("disk_writer accepted a CV cable into its audio in")


class TestDiskWriterBehavior:
    def test_disarmed_writes_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "should_not_exist.wav")
            patch = Patch()
            osc = patch.add_module(
                "oscillator",
                params={"waveform": "sine", "freq": 440.0, "amp": 0.3},
            )
            dw = patch.add_module(
                "disk_writer", params={"path": path, "armed": False},
            )
            patch.connect(osc.id, "out", dw.id, "in")
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            for _ in range(4):
                backend.render_block(512)
            backend.stop()
            assert not os.path.exists(path)

    def test_armed_but_unpatched_writes_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "unpatched.wav")
            patch = Patch()
            patch.add_module(
                "disk_writer", params={"path": path, "armed": True},
            )
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            for _ in range(4):
                backend.render_block(512)
            backend.stop()
            assert not os.path.exists(path)

    def test_records_audio_to_wav_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "rec.wav")
            patch = Patch()
            osc = patch.add_module(
                "oscillator",
                params={"waveform": "sine", "freq": 440.0, "amp": 0.5},
            )
            dw = patch.add_module(
                "disk_writer", params={"path": path, "armed": True},
            )
            patch.connect(osc.id, "out", dw.id, "in")
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            n_blocks = 10
            for _ in range(n_blocks):
                backend.render_block(512)
            _wait_for_drain(backend, dw.id)
            # Need to invoke the writer cleanup. We didn't call start(),
            # so stop() bails early — manually close the writer state.
            state = backend._state[dw.id]
            backend._close_disk_writer_state(state)
            assert os.path.exists(path), "WAV file was not created"
            with wave.open(path, "rb") as wf:
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2
                assert wf.getframerate() == SR
                frames_written = wf.getnframes()
                # Should match what we rendered (10 blocks × 512 samples).
                assert frames_written == n_blocks * 512
                raw = wf.readframes(frames_written)
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
            # A sine at amp=0.5 should have RMS ≈ 0.5/√2 ≈ 0.354.
            rms = float(np.sqrt(np.mean(samples ** 2)))
            assert 0.25 < rms < 0.45, f"recorded RMS off: {rms}"

    def test_changing_path_restarts_file(self):
        """Edit the path mid-take and the writer should close the first
        file and start writing to the second."""
        with tempfile.TemporaryDirectory() as td:
            path_a = os.path.join(td, "a.wav")
            path_b = os.path.join(td, "b.wav")
            patch = Patch()
            osc = patch.add_module(
                "oscillator",
                params={"waveform": "sine", "freq": 220.0, "amp": 0.3},
            )
            dw = patch.add_module(
                "disk_writer", params={"path": path_a, "armed": True},
            )
            patch.connect(osc.id, "out", dw.id, "in")
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            for _ in range(5):
                backend.render_block(512)
            _wait_for_drain(backend, dw.id)
            # Mid-take path swap: writer should reroute.
            dw.set_param("path", path_b)
            for _ in range(5):
                backend.render_block(512)
            _wait_for_drain(backend, dw.id)
            backend._close_disk_writer_state(backend._state[dw.id])
            assert os.path.exists(path_a), "first file not written"
            assert os.path.exists(path_b), "second file not written"
            # Each file should hold roughly 5 × 512 samples.
            with wave.open(path_a, "rb") as wf:
                assert wf.getnframes() == 5 * 512
            with wave.open(path_b, "rb") as wf:
                assert wf.getnframes() == 5 * 512

    def test_disarming_mid_session_closes_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "rec.wav")
            patch = Patch()
            osc = patch.add_module(
                "oscillator",
                params={"waveform": "sine", "freq": 440.0, "amp": 0.3},
            )
            dw = patch.add_module(
                "disk_writer", params={"path": path, "armed": True},
            )
            patch.connect(osc.id, "out", dw.id, "in")
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            for _ in range(5):
                backend.render_block(512)
            _wait_for_drain(backend, dw.id)
            dw.set_param("armed", False)
            # Render some more blocks while disarmed — they should not
            # land in the file.
            backend.render_block(512)
            backend.render_block(512)
            assert os.path.exists(path)
            with wave.open(path, "rb") as wf:
                frames = wf.getnframes()
            # Should be ~5 blocks (some tolerance for queue-drain race).
            assert 4 * 512 <= frames <= 6 * 512, f"unexpected frames: {frames}"

    def test_compile_swap_closes_writer(self):
        """Recompiling a patch where a disk_writer is replaced by a
        different module type closes the writer cleanly."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "rec.wav")
            patch_a = Patch()
            osc = patch_a.add_module(
                "oscillator",
                params={"waveform": "sine", "freq": 440.0, "amp": 0.3},
            )
            dw = patch_a.add_module(
                "disk_writer", params={"path": path, "armed": True},
            )
            patch_a.connect(osc.id, "out", dw.id, "in")
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch_a)
            for _ in range(3):
                backend.render_block(512)
            _wait_for_drain(backend, dw.id)
            # Swap to a fresh patch — same ids, different types. The
            # writer state should be torn down (otherwise the file
            # handle leaks and the worker thread runs forever).
            patch_b = Patch()
            patch_b.add_module(
                "oscillator", params={"waveform": "sine"}
            )
            patch_b.add_module(
                "oscillator", params={"waveform": "sine"}
            )
            backend.compile(patch_b)
            # File should be flushed and closed.
            assert os.path.exists(path)
            with wave.open(path, "rb") as wf:
                assert wf.getnframes() > 0
