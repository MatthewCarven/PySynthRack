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
        assert dw.params == {
            "path": "recording.wav", "armed": True, "timestamp": False,
        }
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


# ----- 2026-10-10: the recordings folder, the timestamp tickbox, the log ----
#
# A relative ``path`` now records into ``<Music>/PySynthRack`` (the suite's
# conftest points that at a temp dir); an absolute one is unchanged -- every
# test above uses absolute temp paths and passes untouched, which is the
# "absolute paths are honoured as before" half of the claim.

from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from pysynthrack.audio import recordings  # noqa: E402


@pytest.fixture
def rec_dir(tmp_path, monkeypatch):
    """A fresh recordings folder per test."""
    folder = tmp_path / "recs"
    monkeypatch.setenv(recordings.RECORDINGS_ENV, str(folder))
    return folder


def _freeze_clock(monkeypatch, *times):
    """Make the recordings module's clock return ``times`` in turn (the
    last one repeats) without touching the global ``time`` module."""
    seq = list(times)

    def now():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(
        recordings, "time",
        SimpleNamespace(
            time=now, strftime=time.strftime, localtime=time.localtime,
        ),
    )


def _writer_patch(path, **params):
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.3},
    )
    dw = patch.add_module(
        "disk_writer", params={"path": path, "armed": True, **params},
    )
    patch.connect(osc.id, "out", dw.id, "in")
    backend = NumpyBackend(sample_rate=SR, block_size=512)
    backend.compile(patch)
    return backend, dw


def _take(backend, dw, blocks=3):
    """Render one take and close it (what Stop does)."""
    for _ in range(blocks):
        backend.render_block(512)
    _wait_for_drain(backend, dw.id)
    backend._close_disk_writer_state(backend._state[dw.id])


def _stamp(y, mo, d, h, mi, s):
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


class TestRecordingsPaths:
    def test_relative_names_land_in_the_recordings_folder(self, rec_dir):
        assert recordings.resolve_recording_path("take.wav") == rec_dir / "take.wav"
        assert recordings.resolve_recording_path("set/a.wav") == rec_dir / "set" / "a.wav"

    def test_absolute_paths_are_used_as_given(self, rec_dir, tmp_path):
        target = tmp_path / "elsewhere" / "x.wav"
        assert recordings.resolve_recording_path(str(target)) == target

    def test_an_empty_path_records_to_the_default_name(self, rec_dir):
        assert recordings.resolve_recording_path("  ") == rec_dir / "recording.wav"

    def test_the_folder_is_music_slash_pysynthrack(self, monkeypatch):
        monkeypatch.delenv(recordings.RECORDINGS_ENV, raising=False)
        monkeypatch.setattr(recordings, "music_dir", lambda: Path("M"))
        assert recordings.recordings_dir() == Path("M") / "PySynthRack"

    def test_music_falls_back_to_home_music(self, monkeypatch):
        monkeypatch.setattr(recordings, "_windows_music_dir", lambda: None)
        recordings.music_dir.cache_clear()
        try:
            assert recordings.music_dir() == Path.home() / "Music"
        finally:
            recordings.music_dir.cache_clear()

    @pytest.mark.skipif(os.name != "nt", reason="the known-folder lookup is Windows-only")
    def test_the_windows_lookup_finds_a_real_folder(self):
        found = recordings._windows_music_dir()
        assert found is not None and found.is_absolute()

    def test_stamped_names_are_sortable_ascii_without_colons(self):
        when = _stamp(2026, 10, 10, 17, 45, 3)
        name = recordings.stamped_name("take.wav", when)
        assert name == "take_2026-10-10_17-45-03.wav"
        assert name.isascii() and ":" not in name
        assert recordings.stamped_name("take", when) == "take_2026-10-10_17-45-03"

    def test_unique_path_steps_aside_from_existing_files(self, tmp_path):
        first = tmp_path / "t.wav"
        assert recordings.unique_path(first) == first
        first.write_bytes(b"")
        assert recordings.unique_path(first) == tmp_path / "t_2.wav"
        (tmp_path / "t_2.wav").write_bytes(b"")
        assert recordings.unique_path(first) == tmp_path / "t_3.wav"

    def test_a_pick_inside_the_folder_is_stored_relative(self, rec_dir, tmp_path):
        inside = rec_dir / "set" / "a.wav"
        assert recordings.as_patch_path(str(inside)) == "set/a.wav"
        outside = tmp_path / "b.wav"
        assert recordings.as_patch_path(str(outside)) == str(outside)


class TestRecordingTakes:
    def test_a_relative_path_records_into_the_folder(self, rec_dir):
        backend, dw = _writer_patch("rel_take_check.wav")
        _take(backend, dw, blocks=5)
        target = rec_dir / "rel_take_check.wav"
        with wave.open(str(target), "rb") as wf:
            assert wf.getnframes() == 5 * 512
        assert not Path("rel_take_check.wav").exists()  # not the cwd any more

    def test_a_relative_subfolder_is_created(self, rec_dir):
        backend, dw = _writer_patch("sessions/one/take.wav")
        _take(backend, dw)
        assert (rec_dir / "sessions" / "one" / "take.wav").exists()

    def test_untimestamped_takes_keep_one_name(self, rec_dir):
        """Tickbox off = the old behaviour: the same name every take,
        overwritten."""
        backend, dw = _writer_patch("take.wav")
        _take(backend, dw, blocks=3)
        _take(backend, dw, blocks=5)
        assert sorted(p.name for p in rec_dir.iterdir()) == ["take.wav"]
        with wave.open(str(rec_dir / "take.wav"), "rb") as wf:
            assert wf.getnframes() == 5 * 512  # the second take won

    def test_timestamped_takes_each_get_their_own_file(self, rec_dir, monkeypatch):
        t1, t2 = _stamp(2026, 10, 10, 17, 45, 3), _stamp(2026, 10, 10, 17, 46, 0)
        _freeze_clock(monkeypatch, t1, t2)
        backend, dw = _writer_patch("take.wav", timestamp=True)
        _take(backend, dw, blocks=3)
        _take(backend, dw, blocks=4)
        names = sorted(p.name for p in rec_dir.iterdir())
        assert names == [
            "take_2026-10-10_17-45-03.wav", "take_2026-10-10_17-46-00.wav",
        ]
        with wave.open(str(rec_dir / names[0]), "rb") as wf:
            assert wf.getnframes() == 3 * 512
        with wave.open(str(rec_dir / names[1]), "rb") as wf:
            assert wf.getnframes() == 4 * 512

    def test_two_takes_in_one_second_do_not_overwrite(self, rec_dir, monkeypatch):
        _freeze_clock(monkeypatch, _stamp(2026, 10, 10, 9, 0, 0))
        backend, dw = _writer_patch("take.wav", timestamp=True)
        _take(backend, dw)
        _take(backend, dw)
        assert sorted(p.name for p in rec_dir.iterdir()) == [
            "take_2026-10-10_09-00-00.wav", "take_2026-10-10_09-00-00_2.wav",
        ]

    def test_ticking_timestamp_mid_take_starts_a_new_file(self, rec_dir, monkeypatch):
        _freeze_clock(monkeypatch, _stamp(2026, 10, 10, 9, 0, 0))
        backend, dw = _writer_patch("take.wav")
        for _ in range(3):
            backend.render_block(512)
        _wait_for_drain(backend, dw.id)
        dw.set_param("timestamp", True)
        _take(backend, dw, blocks=2)
        assert sorted(p.name for p in rec_dir.iterdir()) == [
            "take.wav", "take_2026-10-10_09-00-00.wav",
        ]

    def test_the_log_names_each_take(self, rec_dir):
        backend, dw = _writer_patch("logged.wav")
        assert backend.recording_log() == []
        _take(backend, dw)
        assert backend.recording_log() == [("opened", str(rec_dir / "logged.wav"))]

    @pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
    def test_an_unopenable_path_is_logged_not_raised(self, rec_dir, tmp_path):
        """An absolute path into a folder that does not exist is NOT
        created (only the recordings folder is) -- it fails, and says so.
        And quietly: ``wave.open`` on a bad name used to leave a half-built
        writer whose ``__del__`` raised, which the app's crash hooks log
        as a crash (the filterwarnings mark is that tripwire)."""
        target = tmp_path / "no_such_folder" / "x.wav"
        backend, dw = _writer_patch(str(target))
        for _ in range(3):
            backend.render_block(512)
        deadline = time.time() + 2.0
        while not backend.recording_log() and time.time() < deadline:
            time.sleep(0.01)
        backend._close_disk_writer_state(backend._state[dw.id])
        log = backend.recording_log()
        assert len(log) == 1 and log[0][0] == "failed"
        assert log[0][1] == str(target)
        assert not target.parent.exists()


# ----- the node's widgets and the status bar ---------------------------------


def _ui_app(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    from unittest import mock

    import pysynthrack.ui.app as app_mod

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=SR, block_size=512)
    app.patch = Patch()
    return app, app_mod


class TestDiskWriterUI:
    def test_the_node_gets_browse_and_a_labelled_tickbox(self, monkeypatch, rec_dir):
        app, app_mod = _ui_app(monkeypatch)
        module = app.patch.add_module("disk_writer")
        app._create_node_for_module(module)
        buttons = [
            c.kwargs for c in app_mod.dpg.add_button.call_args_list
            if c.kwargs.get("label") == "Browse..."
        ]
        assert any(
            b.get("callback") == app._show_recording_dialog
            and b.get("user_data") == module.id
            for b in buttons
        ), "disk_writer has no Browse... wired to the recording dialog"
        boxes = [c.kwargs.get("label") for c in app_mod.dpg.add_checkbox.call_args_list]
        assert "timestamp (new file per take)" in boxes
        assert "armed" in boxes

    def test_browse_opens_in_the_recordings_folder(self, monkeypatch, rec_dir):
        app, app_mod = _ui_app(monkeypatch)
        module = app.patch.add_module("disk_writer")
        app._show_recording_dialog(None, None, module.id)
        assert rec_dir.is_dir()  # created so the dialog has somewhere to open
        kw = app_mod.dpg.file_dialog.call_args.kwargs
        assert kw["default_path"] == str(rec_dir)
        assert kw["default_filename"] == "recording.wav"
        assert kw["callback"] == app._on_recording_selected
        exts = [c.args[0] for c in app_mod.dpg.add_file_extension.call_args_list]
        assert exts[-1] == ".wav"  # no ".*" filter on this dialog

    def test_a_pick_inside_the_folder_is_stored_relative(self, monkeypatch, rec_dir):
        app, _app_mod = _ui_app(monkeypatch)
        module = app.patch.add_module("disk_writer")
        app._show_recording_dialog(None, None, module.id)
        app._on_recording_selected(
            None, {"file_path_name": str(rec_dir / "set" / "a.*")},
        )
        assert module.params["path"] == "set/a.wav"

    def test_a_pick_elsewhere_is_stored_absolute(self, monkeypatch, rec_dir, tmp_path):
        app, _app_mod = _ui_app(monkeypatch)
        module = app.patch.add_module("disk_writer")
        app._show_recording_dialog(None, None, module.id)
        target = tmp_path / "out" / "mix.wav"
        app._on_recording_selected(None, {"file_path_name": str(target)})
        assert module.params["path"] == str(target)

    def test_the_status_bar_names_each_take(self, monkeypatch, rec_dir):
        app, _app_mod = _ui_app(monkeypatch)
        said = []
        app._set_status = said.append
        app._update_recordings()
        assert said == []  # nothing yet, nothing said
        app.backend._recording_log.append(("opened", "C:/M/PySynthRack/a.wav"))
        app._update_recordings()
        assert said == ["Recording -> C:/M/PySynthRack/a.wav"]
        app._update_recordings()
        assert len(said) == 1  # only NEW takes touch the status bar
        app.backend._recording_log.append(("failed", "D:/x/b.wav", "no such dir"))
        app._update_recordings()
        assert said[-1] == "Recording FAILED - cannot open D:/x/b.wav: no such dir"
