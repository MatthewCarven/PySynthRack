"""Smoke tests for tools/render_audit.py (the before/after example audit).

The tool lives outside the package, so it is loaded from its path. These
render one short example, not the whole folder: the point is that the
render -> compare contract keeps working, not to re-test the examples.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

TOOL = Path(__file__).resolve().parent.parent / "tools" / "render_audit.py"
_spec = importlib.util.spec_from_file_location("render_audit", TOOL)
ra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ra)


def _render(out: Path, stem: str = "hello_sine") -> None:
    assert ra.render(ra.REPO, out, seconds=0.1, block=512, only={stem},
                     quiet=True) == 0


def test_two_renders_of_the_same_code_are_identical(tmp_path):
    _render(tmp_path / "a")
    _render(tmp_path / "b")
    report = ra.compare(tmp_path / "a", tmp_path / "b", noise=tmp_path / "b")
    assert report.startswith("examples: 1  moved: 0  same: 1  new: 0")
    assert "- nothing to report" in report


def test_a_moved_render_is_reported_with_its_size(tmp_path):
    _render(tmp_path / "a")
    _render(tmp_path / "b")
    f = tmp_path / "b" / "hello_sine.npz"
    with np.load(f) as z:
        arrays = {k: z[k].copy() for k in z.files}
    arrays["master"][10] += 0.5
    np.savez_compressed(f, **arrays)
    report = ra.compare(tmp_path / "a", tmp_path / "b")
    assert "moved: 1" in report
    assert "| hello_sine | MOVED | 5.00e-01 |" in report


def test_health_flags_silence_and_non_finite(tmp_path):
    np.savez_compressed(tmp_path / "quiet.npz", master=np.zeros((64, 2)))
    np.savez_compressed(tmp_path / "broken.npz",
                        master=np.full((64, 2), np.nan))
    assert any("SILENT" in m for m in
               ra.health(tmp_path, "quiet", ra._load(tmp_path, "quiet")))
    assert any("NON-FINITE" in m for m in
               ra.health(tmp_path, "broken", ra._load(tmp_path, "broken")))


def test_a_new_example_is_reported_as_new(tmp_path):
    (tmp_path / "a").mkdir()
    _render(tmp_path / "b")
    assert "| hello_sine | NEW |" in ra.compare(tmp_path / "a", tmp_path / "b")
