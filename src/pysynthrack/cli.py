"""Headless CLI runner — load a patch, start audio, exit on Enter.

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
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure module types register themselves.
import pysynthrack.modules  # noqa: F401

from .audio import pick_backend
from .io_patch import load_patch


DEFAULT_PATCH = (
    Path(__file__).resolve().parent.parent.parent / "examples" / "hello_sine.json"
)


def run_cli(
    patch_path: Optional[str] = None,
    seconds: Optional[float] = None,
    backend_name: Optional[str] = None,
) -> int:
    """Run a patch headlessly. Returns a process exit code."""
    target = Path(patch_path) if patch_path else DEFAULT_PATCH
    if not target.is_file():
        print(f"error: patch file not found: {target}", file=sys.stderr)
        return 2

    if backend_name:
        # ``pick_backend`` honours the env var. Set it for this process.
        os.environ["PYSYNTHRACK_BACKEND"] = backend_name

    print(f"[pysynthrack] loading patch: {target}")
    patch = load_patch(target)
    print(f"[pysynthrack] modules: {len(patch.modules)}, cables: {len(patch.cables)}")
    for module in patch:
        print(
            f"  #{module.id} {module.TYPE:<16} name={module.name!r:<14} "
            f"params={module.params}"
        )
    for cable in patch.cables:
        print(
            f"  cable: #{cable.src_module_id}.{cable.src_port} → "
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
            print(f"[pysynthrack] playing for {seconds}s …")
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
