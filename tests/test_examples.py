"""Every shipped example must actually open and play.

`test_io.py` pins `hello_sine.json` specifically; this sweeps the WHOLE
``examples/`` folder so a renamed port, param, or module type can never
leave a dead patch that greets a future user with a stack trace. Each
example is loaded, compiled on the numpy backend, and rendered for a few
blocks — the same path the app takes on Open + Start, minus the audio
device.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401 — registers module types for load
from pysynthrack.io_patch import load_patch
from pysynthrack.audio.numpy_backend import NumpyBackend

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.json"))


def test_examples_folder_is_present_and_populated():
    # Guard against a glob that silently matches nothing (wrong cwd, moved
    # folder) — that would make every parametrized test vacuously "pass".
    assert EXAMPLE_FILES, f"no example patches found under {EXAMPLES_DIR}"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.stem)
def test_example_loads_compiles_and_renders(path):
    patch = load_patch(path)
    assert patch.modules, f"{path.name}: loaded an empty patch"
    backend = NumpyBackend(sample_rate=44100, block_size=256)
    backend.compile(patch)
    for _ in range(4):
        out, _device_blocks = backend.render_block_multi(256)
        if out is not None:
            assert np.all(np.isfinite(out)), f"{path.name}: non-finite output"


def test_ring_governor_example_keeps_its_feedback_loop():
    """The governor demo's whole point is the fill -> ratio_cv cycle; lock
    it so a future edit can't quietly sever the loop and leave a patch
    that merely looks like a governor."""
    patch = load_patch(EXAMPLES_DIR / "ring_governor_monitor.json")
    sink = next(
        m for m in patch if m.TYPE == "buffered_specific_speaker_output"
    )
    leaves_fill = [c for c in patch.cables if c.src_port == "fill"]
    into_ratio = [c for c in patch.cables if c.dst_port == "ratio_cv"]
    assert leaves_fill, "governor demo lost its fill cv-out cable"
    assert into_ratio, "governor demo lost its ratio_cv cable"
    # Both ends land on the one buffered sink — a closed loop.
    assert leaves_fill[0].src_module_id == sink.id
    assert into_ratio[0].dst_module_id == sink.id
