"""Headless CLI runner — load a patch, then play it or render it to a file.

The CLI exists for two reasons:

  1. DearPyGui has poor wheel coverage on bleeding-edge Python releases.
     Until that resolves, the CLI lets us still hear sound out of a patch.
  2. Running headless makes it possible to render a patch from a script,
     a test, or a future scheduled task.

Usage:
    python -m pysynthrack --cli                              # default patch, until Enter
    python -m pysynthrack --cli --patch path/to/p.json       # custom patch
    python -m pysynthrack --cli --seconds 5                  # auto-stop after 5s
    python -m pysynthrack --cli --backend numpy              # force backend
    python -m pysynthrack --render out.wav --patch p.json --seconds 2   # write a WAV, play nothing

``--render`` is the offline path: no audio device is opened and nothing is
heard. The patch is rendered block by block, exactly as the headless tests
do it, and written as a 16-bit PCM WAV. It always uses the numpy backend.
"""
from __future__ import annotations

import math
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

# Ensure module types register themselves.
import pysynthrack.modules  # noqa: F401

from ._resources import examples_dir
from .audio import pick_backend
from .io_patch import load_patch


def _default_patch() -> Path:
    """Resolve the default patch lazily so frozen builds pick it up correctly."""
    return examples_dir() / "hello_sine.json"


def run_cli(
    patch_path: str | None = None,
    seconds: float | None = None,
    backend_name: str | None = None,
) -> int:
    """Run a patch headlessly. Returns a process exit code."""
    target = Path(patch_path) if patch_path else _default_patch()
    if not target.is_file():
        print(f"error: patch file not found: {target}", file=sys.stderr)
        return 2

    if backend_name:
        # ``pick_backend`` honours the env var. Set it for this process.
        os.environ["PYSYNTHRACK_BACKEND"] = backend_name

    print(f"[pysynthrack] loading patch: {target}")
    patch = load_patch(target)
    # Dead cables are dropped, not obeyed (see ``Patch.from_dict``) -- say so
    # here or the patch just quietly plays less than the file describes.
    for line in patch.load_warnings:
        print(f"warning: dropped dead cable: {line}", file=sys.stderr)
    print(f"[pysynthrack] modules: {len(patch.modules)}, cables: {len(patch.cables)}")
    for module in patch:
        print(
            f"  #{module.id} {module.TYPE:<16} name={module.name!r:<14} "
            f"params={module.params}"
        )
    for cable in patch.cables:
        print(
            f"  cable: #{cable.src_module_id}.{cable.src_port} -> "
            f"#{cable.dst_module_id}.{cable.dst_port}"
        )

    try:
        backend = pick_backend()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"[pysynthrack] backend: {backend.name} @ {backend.sample_rate} Hz")

    try:
        backend.compile(patch)
        backend.start()
    except Exception as exc:
        print(f"error: failed to start audio: {exc}", file=sys.stderr)
        return 4

    try:
        if seconds is not None:
            print(f"[pysynthrack] playing for {seconds}s ...")
            time.sleep(float(seconds))
        else:
            print("[pysynthrack] playing. Press Enter to stop.")
            try:
                input()
            except EOFError:
                # Piped stdin closed — just wait until Ctrl+C.
                while True:
                    time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[pysynthrack] interrupted")
    finally:
        backend.stop()
        print("[pysynthrack] stopped.")

    return 0


#: The offline render's sample rate and block size. The block size is the
#: engine default; a patch renders the same at any other (the modules are
#: pinned block-size independent), so it is not exposed on the command line.
RENDER_SAMPLE_RATE = 44100
RENDER_BLOCK_SIZE = 512


def render_cli(
    patch_path: str | None,
    out_path: str,
    seconds: float | None,
    mono: bool = False,
    backend_name: str | None = None,
    sample_rate: int = RENDER_SAMPLE_RATE,
    block_size: int = RENDER_BLOCK_SIZE,
) -> int:
    """Render a patch offline to a 16-bit PCM WAV. Returns a process exit code.

    No audio device is opened: the numpy backend is compiled and its master
    bus pulled block by block (``render_block``, the offline tests' own
    path), so this runs on a machine with no sound card and no PortAudio.
    The file is exactly ``round(seconds * sample_rate)`` frames long, stereo
    unless ``mono`` (which writes the mean of the two sides).

    The master bus is already clipped to +/-1 by the backend, so a patch that
    is too hot arrives here flat-topped; the peak is printed and a warning
    given at full scale, and nothing is normalised -- the file is what the
    speakers would have had.
    """
    if backend_name not in (None, "numpy"):
        print(
            f"error: --render uses the numpy backend, not {backend_name!r}",
            file=sys.stderr,
        )
        return 3
    if seconds is None:
        print("error: --render needs a length: --seconds N", file=sys.stderr)
        return 5
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds <= 0.0:
        print(f"error: --seconds must be greater than 0, got {seconds}", file=sys.stderr)
        return 5
    frames = int(round(seconds * sample_rate))
    if frames < 1:
        print(f"error: {seconds}s is less than one sample", file=sys.stderr)
        return 5

    target = Path(patch_path) if patch_path else _default_patch()
    if not target.is_file():
        print(f"error: patch file not found: {target}", file=sys.stderr)
        return 2
    out = Path(out_path)
    if out.parent != Path("") and not out.parent.is_dir():
        print(f"error: output folder does not exist: {out.parent}", file=sys.stderr)
        return 5

    print(f"[pysynthrack] loading patch: {target}")
    patch = load_patch(target)
    for line in patch.load_warnings:
        print(f"warning: dropped dead cable: {line}", file=sys.stderr)

    # Imported here, not at the top: the play path picks its backend through
    # ``pick_backend`` and must not pay for (or require) this one.
    from .audio.numpy_backend import NumpyBackend

    backend = NumpyBackend(sample_rate=sample_rate, block_size=block_size)
    try:
        backend.compile(patch)
    except Exception as exc:
        print(f"error: failed to compile patch: {exc}", file=sys.stderr)
        return 4

    blocks = []
    done = 0
    while done < frames:
        block = backend.render_block(block_size)
        if block is None:
            block = np.zeros((block_size, 2), dtype=np.float32)
        blocks.append(np.asarray(block, dtype=np.float32))
        done += block_size
    audio = np.concatenate(blocks, axis=0)[:frames]
    if mono:
        audio = audio.mean(axis=1, keepdims=True)

    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    pcm = np.round(np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    try:
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(pcm.shape[1])
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm.tobytes())
    except OSError as exc:
        print(f"error: could not write {out}: {exc}", file=sys.stderr)
        return 5

    level = f"{20.0 * math.log10(peak):.1f} dBFS" if peak > 0.0 else "silent"
    print(
        f"[pysynthrack] rendered {frames} frames ({frames / sample_rate:.3f}s, "
        f"{'mono' if mono else 'stereo'}, {sample_rate} Hz, 16-bit) -> {out}"
    )
    print(f"[pysynthrack] peak: {level}")
    if peak == 0.0:
        print("warning: the render is silent", file=sys.stderr)
    elif peak >= 0.999:
        print(
            "warning: the output reaches full scale and may be clipped; "
            "turn the patch down",
            file=sys.stderr,
        )
    return 0
