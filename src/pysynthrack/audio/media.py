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
