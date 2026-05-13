"""Tests for JSON save/load."""
from __future__ import annotations

import json
from pathlib import Path

import pysynthrack.modules  # noqa: F401
from pysynthrack.core import Patch
from pysynthrack.io_patch import (
    load_patch,
    patch_from_json,
    patch_to_json,
    save_patch,
)

EXAMPLE_PATCH = Path(__file__).parent.parent / "examples" / "hello_sine.json"


def _make_patch() -> Patch:
    patch = Patch()
    osc = patch.add_module("oscillator", params={"freq": 261.63, "amp": 0.5})
    out = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", out.id, "in")
    return patch


def test_json_round_trip_in_memory():
    patch = _make_patch()
    text = patch_to_json(patch)
    restored = patch_from_json(text)
    assert restored.to_dict() == patch.to_dict()


def test_json_round_trip_on_disk(tmp_path):
    patch = _make_patch()
    target = tmp_path / "patch.json"
    save_patch(patch, target)

    # File should be valid JSON.
    json.loads(target.read_text(encoding="utf-8"))

    restored = load_patch(target)
    assert restored.to_dict() == patch.to_dict()


def test_example_hello_sine_loads():
    """The shipped example must always be loadable, even after schema edits."""
    assert EXAMPLE_PATCH.is_file(), f"missing example: {EXAMPLE_PATCH}"
    patch = load_patch(EXAMPLE_PATCH)
    assert len(patch.modules) == 2
    assert len(patch.cables) == 1
    # The example should be wired osc → speaker.
    types = sorted(m.TYPE for m in patch)
    assert types == ["oscillator", "speaker_output"]


def test_ui_metadata_round_trips():
    """Node positions in patch.ui should survive a JSON round-trip."""
    patch = _make_patch()
    patch.ui["node_positions"] = {"1": [120.5, 80.0], "2": [400.0, 220.5]}
    text = patch_to_json(patch)
    restored = patch_from_json(text)
    assert restored.ui.get("node_positions") == {
        "1": [120.5, 80.0],
        "2": [400.0, 220.5],
    }


def test_ui_metadata_optional_for_legacy_patches():
    """Patches without a ui block should still load (backward compat)."""
    legacy = {
        "version": 1,
        "next_id": 3,
        "modules": [
            {"id": 1, "type": "oscillator", "name": "osc", "params": {}},
            {"id": 2, "type": "speaker_output", "name": "out", "params": {}},
        ],
        "cables": [
            {
                "src_module_id": 1,
                "src_port": "out",
                "dst_module_id": 2,
                "dst_port": "in",
            }
        ],
    }
    patch = Patch.from_dict(legacy)
    assert patch.ui == {}
    # And the round-trip should NOT inject an empty "ui" key.
    assert "ui" not in patch.to_dict()
