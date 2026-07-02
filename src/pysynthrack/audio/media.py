"""Media decode helpers — turn arbitrary audio/video files into PCM.

WAV is handled directly by the backend (``scipy``, zero extra deps).
Everything else — mp3, m4a, flac, ogg, and the *audio track of video
containers* (mp4, mkv, mov, webm, ...) — is decoded by shelling out to
ffmpeg, which writes raw little-endian float32 PCM to a pipe that we read
straight into numpy.

ffmpeg is optional and discovered at runtime in two places, in order:
  1. a binary bundled by the ``imageio-ffmpeg`` package (the ``[media]``
     extra) — travels inside the packaged exe, nothing for the user to
     install;
  2. a system ``ffmpeg`` on PATH.
If neither is present the helpers return ``None`` and the FilePlayer
falls back to its WAV-only behaviour (non-WAV → silence), so the synth
never hard-depends on ffmpeg.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import threading

import numpy as np


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Locate an ffmpeg executable: bundled (imageio-ffmpeg) then PATH.

    Cached for the process — the first lookup (hit or miss) sticks.
    Install ffmpeg (or the ``[media]`` extra) and restart to refresh.
    """
    # 1) Bundled binary via imageio-ffmpeg, if that optional dep is present.
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    # 2) A system ffmpeg on PATH.
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    """True if a usable ffmpeg (bundled or on PATH) was found."""
    return find_ffmpeg() is not None


def decode_with_ffmpeg(path, target_sr) -> "np.ndarray | None":
    """Decode any ffmpeg-readable file to a contiguous ``(2, N)`` float32.

    Mirrors ``NumpyBackend._load_wav``'s contract: stereo, resampled to
    ``target_sr``, values in roughly [-1, 1], or ``None`` on any failure
    (no ffmpeg, missing file, no audio stream, decode error) so the
    caller renders silence rather than raising. ffmpeg does the channel
    downmix (``-ac 2``) and the resample (``-ar``); we only reshape the
    interleaved float32 bytes into ``(2, N)``.
    """
    exe = find_ffmpeg()
    if not exe or not path or not os.path.isfile(path):
        return None

    cmd = [
        exe,
        "-v", "error",
        "-nostdin",
        "-i", str(path),
        "-vn",                    # ignore any video stream
        "-f", "f32le",           # raw little-endian float32 PCM container
        "-acodec", "pcm_f32le",
        "-ac", "2",              # force stereo (mono up-, multi-channel down-mixed)
        "-ar", str(int(target_sr)),
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - OS/spawn-specific
        print(f"[FilePlayer] ffmpeg failed to run for {path}: {exc}")
        return None

    if proc.returncode != 0 or not proc.stdout:
        if proc.returncode != 0:
            lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            tail = lines[-1] if lines else "unknown error"
            print(f"[FilePlayer] ffmpeg could not decode {path}: {tail}")
        return None

    raw = proc.stdout
    raw = raw[: (len(raw) // 4) * 4]  # whole float32 samples only
    samples = np.frombuffer(raw, dtype="<f4")
    n = samples.size // 2  # interleaved L/R
    if n == 0:
        return None
    stereo = samples[: n * 2].reshape(n, 2).T  # (2, N)
    return np.ascontiguousarray(stereo, dtype=np.float32)


class StreamingDecoder:
    """Decode a media file on a background thread, publishing progressively.

    Built for the FilePlayer's realtime path: the audio thread must NEVER
    wait on a decode. Construction returns immediately; a daemon worker
    thread fills a growing ``(2, N)`` float32 buffer and publishes
    ``frames_ready`` after every chunk it lands. The consumer (the audio
    render) reads ``frames_ready`` first and then slices ``buffer`` below
    that watermark — writes happen strictly before the watermark moves, so
    no lock is needed on the hot path (attribute loads/stores of ints and
    references are atomic under the GIL).

    Decode strategy mirrors ``NumpyBackend._decode_audio``: an optional
    ``full_decode`` callable (the backend's scipy WAV fast path) is tried
    first *inside the worker* — it returns the whole array at once, which
    is published in one step. Everything else streams from ffmpeg's stdout
    in ~256 KiB chunks (≈0.7 s of stereo float32 at 44.1 kHz), so a long
    video's audio becomes playable ~instantly while the tail keeps
    decoding.

    Terminal states: ``done`` is True once the worker has finished (for
    any reason) and ``total_frames`` is then final; ``failed`` is True if
    nothing usable was decoded (no ffmpeg, missing file, no audio stream).
    A decode error mid-stream keeps whatever was already published and
    finishes with the truncated total rather than discarding audio.

    ``close()`` kills any live ffmpeg and lets the worker exit; ``wait()``
    joins the worker (test / offline-render hook — never call it from the
    audio thread).
    """

    _CHUNK_BYTES = 1 << 18  # 256 KiB reads from ffmpeg's stdout
    _FRAME_BYTES = 8        # 2 channels x float32

    def __init__(self, path, target_sr, full_decode=None) -> None:
        self.path = str(path)
        self.target_sr = int(target_sr)
        self.frames_ready: int = 0
        self.total_frames: int | None = None
        self.done: bool = False
        self.failed: bool = False
        self._buf = np.zeros((2, 0), dtype=np.float32)
        self._full_decode = full_decode
        self._proc: subprocess.Popen | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._work,
            daemon=True,
            name=f"FileDecode-{os.path.basename(self.path) or 'empty'}",
        )
        self._thread.start()

    # ----- consumer API (audio-thread safe) --------------------------------

    @property
    def buffer(self) -> np.ndarray:
        """The decoded samples so far; only indices < frames_ready are valid."""
        return self._buf

    def wait(self, timeout: float | None = None) -> bool:
        """Join the worker. True if decode finished and produced audio."""
        self._thread.join(timeout)
        return self.done and not self.failed

    def close(self) -> None:
        """Abort a decode in flight (path change / module removal / recompile)."""
        self._closed = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    # ----- worker -----------------------------------------------------------

    def _work(self) -> None:
        try:
            self._decode()
        except Exception as exc:  # pragma: no cover - belt and braces
            print(f"[FilePlayer] decode worker crashed for {self.path}: {exc}")
        finally:
            if self.frames_ready == 0:
                self.failed = True
            self.total_frames = self.frames_ready
            self.done = True

    def _decode(self) -> None:
        # 1) The caller-supplied whole-file fast path (scipy WAV). Runs on
        #    this worker thread, so even a big resample never blocks audio.
        if self._full_decode is not None:
            try:
                samples = self._full_decode(self.path, self.target_sr)
            except Exception:
                samples = None
            if samples is not None and samples.shape[1] > 0:
                self._buf = np.ascontiguousarray(samples, dtype=np.float32)
                self.frames_ready = self._buf.shape[1]
                return
        if self._closed:
            return

        # 2) Stream from ffmpeg. Same command line as decode_with_ffmpeg,
        #    but consumed incrementally instead of via subprocess.run.
        exe = find_ffmpeg()
        if not exe or not self.path or not os.path.isfile(self.path):
            return
        cmd = [
            exe,
            "-v", "error",
            "-nostdin",
            "-i", self.path,
            "-vn",
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ac", "2",
            "-ar", str(self.target_sr),
            "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:  # pragma: no cover - OS/spawn-specific
            print(f"[FilePlayer] ffmpeg failed to run for {self.path}: {exc}")
            return

        leftover = b""
        try:
            while not self._closed:
                chunk = self._proc.stdout.read(self._CHUNK_BYTES)
                if not chunk:
                    break
                data = leftover + chunk
                usable = (len(data) // self._FRAME_BYTES) * self._FRAME_BYTES
                leftover = data[usable:]
                if usable:
                    flat = np.frombuffer(data[:usable], dtype="<f4")
                    n = flat.size // 2
                    self._append(flat[: n * 2].reshape(n, 2).T)
        finally:
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            # -v error keeps stderr tiny, so a post-hoc read can't deadlock.
            err = b""
            try:
                err = self._proc.stderr.read() or b""
                self._proc.stderr.close()
            except Exception:
                pass
            rc = self._proc.wait()
            if rc != 0 and not self._closed:
                tail = err.decode("utf-8", "replace").strip().splitlines()
                msg = tail[-1] if tail else f"exit code {rc}"
                if self.frames_ready == 0:
                    print(f"[FilePlayer] ffmpeg could not decode {self.path}: {msg}")
                else:  # keep the audio we already have
                    print(
                        f"[FilePlayer] decode of {self.path} ended early "
                        f"({msg}); keeping {self.frames_ready} frames"
                    )

    def _append(self, frames: np.ndarray) -> None:
        """Land a ``(2, n)`` chunk and move the watermark (publish last)."""
        n = frames.shape[1]
        ready = self.frames_ready
        need = ready + n
        if need > self._buf.shape[1]:
            # Geometric growth; the swap is safe because the old buffer
            # remains valid below the (old) watermark for any reader that
            # grabbed a reference before the swap.
            grown = np.zeros((2, max(need, 2 * self._buf.shape[1], 65536)),
                             dtype=np.float32)
            grown[:, :ready] = self._buf[:, :ready]
            self._buf = grown
        self._buf[:, ready:need] = frames
        self.frames_ready = need  # publish AFTER the data is in place
