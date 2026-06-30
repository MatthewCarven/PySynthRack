"""Tests for media decode — the ffmpeg fallback for non-WAV / video audio.

Coverage:
  - find_ffmpeg / ffmpeg_available never crash and return sane types.
  - decode_with_ffmpeg byte parsing is deterministic via a mocked
    subprocess (interleaved f32le → (2, N); empty/error/no-ffmpeg/
    missing-file → None).
  - NumpyBackend._decode_audio dispatch: a readable WAV uses the scipy
    fast path (ffmpeg never invoked); a garbage path → None; a non-WAV
    with no ffmpeg → None.
  - Integration (skipped when no ffmpeg present): a transcoded FLAC/OGG
    round-trips, decode resamples to the engine rate, the backend routes
    non-WAV through ffmpeg, a FilePlayer renders audible audio from a
    non-WAV file, and the *audio track of a synthesized video* decodes.
"""
from __future__ import annotations

import subprocess

import numpy as np
import pytest
from scipy.io import wavfile

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio import media
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
FF = media.find_ffmpeg()


def _write_sine_wav(path, freq=440.0, secs=0.25, sr=SR, amp=0.5):
    t = np.arange(int(secs * sr)) / sr
    wavfile.write(str(path), sr, (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32))
    return str(path)


class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ----- discovery -------------------------------------------------------------


class TestFindFfmpeg:
    def test_returns_str_or_none(self):
        r = media.find_ffmpeg()
        assert r is None or isinstance(r, str)

    def test_available_is_bool(self):
        assert isinstance(media.ffmpeg_available(), bool)


# ----- byte parsing (deterministic, mocked subprocess) -----------------------


class TestFfmpegParsing:
    def test_parses_interleaved_f32le_to_stereo(self, monkeypatch, tmp_path):
        f = tmp_path / "x.mp3"
        f.write_bytes(b"stub")  # only needs to exist
        n = 128
        sig = (np.arange(n * 2, dtype="<f4") * 0.0001)  # L,R,L,R,...
        monkeypatch.setattr(media, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr(media.subprocess, "run",
                            lambda *a, **k: _Proc(0, sig.tobytes()))
        out = media.decode_with_ffmpeg(str(f), SR)
        assert out.shape == (2, n)
        assert out.dtype == np.float32
        assert np.allclose(out[0], sig[0::2])  # left = even samples
        assert np.allclose(out[1], sig[1::2])  # right = odd samples
        assert out.flags["C_CONTIGUOUS"]

    def test_nonzero_returncode_is_none(self, monkeypatch, tmp_path):
        f = tmp_path / "x.mp3"; f.write_bytes(b"stub")
        monkeypatch.setattr(media, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr(media.subprocess, "run",
                            lambda *a, **k: _Proc(1, b"", b"boom"))
        assert media.decode_with_ffmpeg(str(f), SR) is None

    def test_empty_stdout_is_none(self, monkeypatch, tmp_path):
        f = tmp_path / "x.mp3"; f.write_bytes(b"stub")
        monkeypatch.setattr(media, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: _Proc(0, b""))
        assert media.decode_with_ffmpeg(str(f), SR) is None

    def test_no_ffmpeg_is_none(self, monkeypatch, tmp_path):
        f = tmp_path / "x.mp3"; f.write_bytes(b"stub")
        monkeypatch.setattr(media, "find_ffmpeg", lambda: None)
        assert media.decode_with_ffmpeg(str(f), SR) is None

    def test_missing_file_is_none(self, monkeypatch):
        monkeypatch.setattr(media, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
        assert media.decode_with_ffmpeg("/no/such/file.mp3", SR) is None

    def test_ragged_bytes_truncated(self, monkeypatch, tmp_path):
        # 9 bytes is not a whole number of float32s; must not raise.
        f = tmp_path / "x.mp3"; f.write_bytes(b"stub")
        monkeypatch.setattr(media, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: _Proc(0, b"123456789"))
        out = media.decode_with_ffmpeg(str(f), SR)
        # 9 bytes -> 2 whole float32s -> 1 stereo frame
        assert out is None or out.shape == (2, 1)


# ----- backend dispatch ------------------------------------------------------


class TestDecodeDispatch:
    def test_wav_uses_fast_path_not_ffmpeg(self, tmp_path, monkeypatch):
        wav = _write_sine_wav(tmp_path / "s.wav")
        calls = {"n": 0}

        def _spy(*a, **k):
            calls["n"] += 1
            return None

        monkeypatch.setattr(media, "decode_with_ffmpeg", _spy)
        b = NumpyBackend(sample_rate=SR, block_size=512)
        out = b._decode_audio(wav, SR)
        assert out is not None and out.shape[0] == 2
        assert calls["n"] == 0  # ffmpeg path untouched for a readable WAV

    def test_garbage_path_is_none(self):
        b = NumpyBackend(sample_rate=SR, block_size=512)
        assert b._decode_audio("/no/such/file.xyz", SR) is None

    def test_non_wav_without_ffmpeg_is_none(self, tmp_path, monkeypatch):
        f = tmp_path / "a.mp3"; f.write_bytes(b"not really an mp3")
        monkeypatch.setattr(media, "find_ffmpeg", lambda: None)
        b = NumpyBackend(sample_rate=SR, block_size=512)
        assert b._decode_audio(str(f), SR) is None


# ----- real ffmpeg integration (skipped when ffmpeg is absent) ---------------


@pytest.mark.skipif(FF is None, reason="ffmpeg not available")
class TestFfmpegIntegration:
    def _transcode(self, src, dst):
        subprocess.run([FF, "-v", "error", "-y", "-i", src, dst], check=True)
        return dst

    def test_flac_roundtrip(self, tmp_path):
        wav = _write_sine_wav(tmp_path / "s.wav", secs=0.3)
        flac = self._transcode(wav, str(tmp_path / "s.flac"))
        out = media.decode_with_ffmpeg(flac, SR)
        assert out is not None and out.shape[0] == 2 and out.shape[1] > 0
        assert np.isfinite(out).all()
        assert np.abs(out).max() > 0.01  # audible

    def test_decode_resamples_to_target(self, tmp_path):
        # Source at 22050; decode at 44100 -> roughly double the frames.
        t = np.arange(int(0.5 * 22050)) / 22050
        src = str(tmp_path / "s22.wav")
        wavfile.write(src, 22050, (0.4 * np.sin(2 * np.pi * 330 * t)).astype(np.float32))
        flac = self._transcode(src, str(tmp_path / "s22.flac"))
        out = media.decode_with_ffmpeg(flac, 44100)
        assert out is not None
        assert abs(out.shape[1] - int(0.5 * 44100)) < 4000

    def test_backend_routes_non_wav_through_ffmpeg(self, tmp_path):
        wav = _write_sine_wav(tmp_path / "s.wav")
        flac = self._transcode(wav, str(tmp_path / "s.flac"))
        b = NumpyBackend(sample_rate=SR, block_size=512)
        out = b._decode_audio(flac, SR)
        assert out is not None and out.shape[0] == 2

    def test_file_player_renders_non_wav(self, tmp_path):
        wav = _write_sine_wav(tmp_path / "s.wav", secs=0.3)
        flac = self._transcode(wav, str(tmp_path / "s.flac"))
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": flac, "loop": True})
        spk = patch.add_module("speaker_output")
        patch.connect(fp.id, "left", spk.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        block = None
        for _ in range(4):
            block = b.render_block(512)
        assert block is not None and np.all(np.isfinite(block))
        assert np.abs(block).max() > 0.0

    def test_video_audio_extraction(self, tmp_path):
        # Synthesize a tiny video with a tone, then pull its audio track.
        mp4 = str(tmp_path / "clip.mp4")
        subprocess.run(
            [FF, "-v", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
             "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=0.4",
             "-c:v", "mpeg4", "-c:a", "aac", "-shortest", mp4],
            check=True,
        )
        out = media.decode_with_ffmpeg(mp4, SR)
        assert out is not None and out.shape[0] == 2 and out.shape[1] > 0
        assert np.abs(out).max() > 0.01  # the tone survived the round-trip
