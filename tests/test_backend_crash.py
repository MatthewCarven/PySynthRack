"""Tests for the audio-callback crash-protection path.

These exercise the wrapper around ``render_block`` in
:class:`NumpyBackend._audio_callback`. The injection trick: monkeypatch
``render_block`` itself to raise, then call ``_audio_callback`` directly
with a dummy outdata buffer. No PortAudio needed.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect Path.home() so crash files land in tmp_path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _make_backend_with_patch():
    """Build a minimal compiled backend so the callback has somewhere to
    point. The actual renderer is monkeypatched in the tests; the
    backend just needs to be initialised + have flags set up."""
    backend = NumpyBackend(sample_rate=44100, block_size=512)
    patch = Patch()
    osc = patch.add_module("oscillator")
    spk = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", spk.id, "in")
    backend.compile(patch)
    return backend


class TestAudioCallbackCrashProtection:
    def test_callback_returns_silence_on_first_crash(self, home, monkeypatch):
        backend = _make_backend_with_patch()

        def boom(self, frames):
            raise RuntimeError("simulated render explosion")

        monkeypatch.setattr(NumpyBackend, "render_block", boom)

        outdata = np.ones((512, 2), dtype=np.float32)  # start non-zero
        # Should NOT raise.
        backend._audio_callback(outdata, 512, None, None)

        assert float(np.max(np.abs(outdata))) == 0.0
        assert backend._render_disabled is True
        assert backend._crash_reported is True

    def test_callback_writes_crash_file_exactly_once(self, home, monkeypatch):
        backend = _make_backend_with_patch()

        def boom(self, frames):
            raise RuntimeError("simulated render explosion")

        monkeypatch.setattr(NumpyBackend, "render_block", boom)

        outdata = np.zeros((512, 2), dtype=np.float32)
        # Five crashes - only the first should produce a file.
        for _ in range(5):
            backend._audio_callback(outdata, 512, None, None)

        crash_dir = home / ".pysynthrack" / "crashes"
        files = list(crash_dir.glob("crash_*_audio_callback.txt"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        # The heavy report should mention the exception type.
        assert "RuntimeError" in body
        assert "simulated render explosion" in body

    def test_subsequent_blocks_short_circuit_render(self, home, monkeypatch):
        backend = _make_backend_with_patch()

        call_count = {"n": 0}

        def boom(self, frames):
            call_count["n"] += 1
            raise RuntimeError("boom")

        monkeypatch.setattr(NumpyBackend, "render_block", boom)

        outdata = np.zeros((512, 2), dtype=np.float32)
        backend._audio_callback(outdata, 512, None, None)
        assert call_count["n"] == 1  # the crash call
        # Subsequent callbacks short-circuit at the _render_disabled
        # check and never touch render_block.
        for _ in range(3):
            backend._audio_callback(outdata, 512, None, None)
        assert call_count["n"] == 1  # still 1

    def test_compile_resets_disable_flag(self, home, monkeypatch):
        backend = _make_backend_with_patch()
        # Force the backend into the crashed state.
        backend._render_disabled = True
        backend._crash_reported = True

        # Recompile - should clear both flags.
        patch = Patch()
        osc = patch.add_module("oscillator")
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", spk.id, "in")
        backend.compile(patch)

        assert backend._render_disabled is False
        assert backend._crash_reported is False

    def test_init_flags_default_false(self):
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        assert backend._render_disabled is False
        assert backend._crash_reported is False

    def test_crash_helper_never_raises_even_when_writer_fails(self, monkeypatch):
        # write_crash_report returns None on failure - the helper should
        # log to stderr and return cleanly without propagating.
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        # Point Path.home at a regular file so the crash dir mkdir fails.
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            file_path = Path(f.name)
            monkeypatch.setattr(Path, "home", lambda: file_path)
            try:
                raise ValueError("test")
            except ValueError as e:
                # Should not raise even though crash file can't be written.
                backend._handle_audio_crash(e)
            assert backend._render_disabled is True
            assert backend._crash_reported is True


class TestNormalOperationUnaffected:
    """A successful render shouldn't trigger any of the crash machinery."""

    def test_normal_callback_does_not_set_flags(self, home):
        backend = _make_backend_with_patch()
        outdata = np.zeros((512, 2), dtype=np.float32)
        backend._audio_callback(outdata, 512, None, None)
        assert backend._render_disabled is False
        assert backend._crash_reported is False

    def test_normal_callback_does_not_write_crash_file(self, home):
        backend = _make_backend_with_patch()
        outdata = np.zeros((512, 2), dtype=np.float32)
        backend._audio_callback(outdata, 512, None, None)
        crash_dir = home / ".pysynthrack" / "crashes"
        # Directory might not even exist - that is correct, no crash means no file.
        if crash_dir.exists():
            assert list(crash_dir.iterdir()) == []
