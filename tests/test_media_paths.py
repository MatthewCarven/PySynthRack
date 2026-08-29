"""Relative media paths must not depend on where the app was launched.

The bug, found 2026-08-29 by Matthew: `examples/sampler_breaks.json`
played nothing. The samples were present and correct; the patch stores a
RELATIVE path (`examples/samples/breaks.wav`) and the backend resolved it
against the process working directory alone, so the patch was audible
from the project root and silent from anywhere else -- including from
`dist/`. Silent rather than loud, because every media loader here fails
soft so a bad path can never raise on the audio thread.

Two halves are pinned here:

  * **Resolution** -- a relative path is tried against the patch's own
    folder, that folder's parent, the cwd, and the resource root, first
    hit wins; absolute paths are untouched; the result is stable within a
    compile (the renderers use it as a cache key) and re-evaluated across
    one (so a file that appears is found).
  * **Visibility** -- a load that finishes with nothing is reportable, so
    the GUI can say so instead of playing silence. Silence that looks
    healthy is what cost the listening pass.

The end-to-end tests below render the real shipped examples from a
foreign cwd, which is the exact thing that was broken.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.io_patch import load_patch, save_patch

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """Run the body with the working directory somewhere unrelated."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_wav(path, seconds=0.05, sr=44100):
    """A real (tiny) WAV, so a decode of it succeeds quietly."""
    from scipy.io import wavfile

    n = int(sr * seconds)
    tone = (0.2 * np.sin(2 * np.pi * 220.0 * np.arange(n) / sr)).astype(np.float32)
    wavfile.write(str(path), sr, tone)
    return path


def _settle_loaders(backend, timeout=20.0):
    """Wait for every background media decode kicked at compile to finish.

    The loaders are daemon threads started by ``compile()`` and polled by
    the renderer, so how many blocks render before a sample is available
    depends on disk timing. That is fine in the app (it just means the
    first hit or two are quiet) and poison in a test that compares two
    renders sample-for-sample. Settling first makes the comparison about
    the audio rather than the race.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [
            st["pending"]
            for st in backend._state.values()
            if isinstance(st, dict) and st.get("pending") is not None
        ]
        if not pending:
            return
        if all(p["loader"].done for p in pending):
            return
        time.sleep(0.005)


def _render(patch, blocks=700, block=256, settle=False):
    backend = NumpyBackend(sample_rate=44100, block_size=block)
    backend.compile(patch)
    if settle:
        _settle_loaders(backend)
    chunks = []
    for _ in range(blocks):
        out, _devices = backend.render_block_multi(block)
        if out is not None:
            arr = np.asarray(out)
            chunks.append(arr.reshape(-1, 2)[:, 0] if arr.ndim > 1 else arr)
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


# ----- The reported bug ------------------------------------------------------


class TestLaunchedFromAnywhere:
    """The regression itself: the shipped examples, from a foreign cwd."""

    @pytest.mark.parametrize(
        "name", ["sampler_breaks", "convolver_reverb"]
    )
    def test_example_is_audible_from_an_unrelated_directory(self, name, elsewhere):
        wav = EXAMPLES / "samples" / "breaks.wav"
        ir = EXAMPLES / "irs" / "hall.wav"
        if name == "sampler_breaks" and not wav.is_file():
            pytest.skip("run examples/samples/generate_samples.py first")
        if name == "convolver_reverb" and not ir.is_file():
            pytest.skip("run examples/irs/generate_irs.py first")

        sig = _render(load_patch(EXAMPLES / f"{name}.json"))
        assert np.all(np.isfinite(sig))
        assert float(np.max(np.abs(sig))) > 0.01, (
            f"{name} rendered silence from cwd={os.getcwd()} -- the relative "
            "media path did not resolve"
        )

    def test_the_same_patch_sounds_identical_from_both_directories(
        self, tmp_path, monkeypatch
    ):
        """Not merely 'audible' -- the SAME audio. Resolution must find the
        same file, not some other copy."""
        if not (EXAMPLES / "samples" / "breaks.wav").is_file():
            pytest.skip("run examples/samples/generate_samples.py first")
        path = EXAMPLES / "sampler_breaks.json"

        monkeypatch.chdir(REPO)
        from_root = _render(load_patch(path), blocks=200, settle=True)
        monkeypatch.chdir(tmp_path)
        from_elsewhere = _render(load_patch(path), blocks=200, settle=True)

        assert float(np.max(np.abs(from_root))) > 0.01, "the reference is silent"
        np.testing.assert_array_equal(from_root, from_elsewhere)


# ----- The resolver ----------------------------------------------------------


class TestResolver:
    def _backend_at(self, patch_dir):
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b._patch_dir = str(patch_dir) if patch_dir is not None else None
        return b

    def test_empty_stays_empty(self):
        b = self._backend_at(None)
        assert b._resolve_media_path("") == ""
        assert b._resolve_media_path(None) == ""

    def test_absolute_is_untouched_even_when_missing(self, tmp_path):
        """The user named an exact file; second-guessing them is worse
        than failing, and the error must name what they asked for."""
        b = self._backend_at(tmp_path)
        missing = str(tmp_path / "nope" / "gone.wav")
        assert b._resolve_media_path(missing) == missing

    def test_prefers_the_patch_folder(self, tmp_path, monkeypatch):
        """Two copies of the same relative name: the patch's own wins."""
        patch_dir = tmp_path / "patches"
        (patch_dir / "media").mkdir(parents=True)
        (patch_dir / "media" / "a.wav").write_bytes(b"patch copy")
        cwd = tmp_path / "cwd"
        (cwd / "media").mkdir(parents=True)
        (cwd / "media" / "a.wav").write_bytes(b"cwd copy")
        monkeypatch.chdir(cwd)

        b = self._backend_at(patch_dir)
        got = Path(b._resolve_media_path("media/a.wav")).read_bytes()
        assert got == b"patch copy"

    def test_falls_back_to_the_patch_folders_parent(self, tmp_path, monkeypatch):
        """The shipped-example shape: the patch lives in examples/ and
        names its media from the project root."""
        root = tmp_path / "project"
        (root / "examples").mkdir(parents=True)
        (root / "examples" / "samples").mkdir()
        target = root / "examples" / "samples" / "breaks.wav"
        target.write_bytes(b"x")
        monkeypatch.chdir(tmp_path)

        b = self._backend_at(root / "examples")
        assert Path(b._resolve_media_path("examples/samples/breaks.wav")) == target

    def test_falls_back_to_cwd_when_no_patch_folder(self, tmp_path, monkeypatch):
        """A patch built in memory has no folder; the old behaviour must
        still work so nothing that worked before stops working."""
        (tmp_path / "media").mkdir()
        target = tmp_path / "media" / "b.wav"
        target.write_bytes(b"x")
        monkeypatch.chdir(tmp_path)

        b = self._backend_at(None)
        assert Path(b._resolve_media_path("media/b.wav")) == target

    def test_missing_comes_back_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        b = self._backend_at(tmp_path)
        assert b._resolve_media_path("no/such.wav") == "no/such.wav"

    def test_result_is_stable_within_a_compile(self, tmp_path, monkeypatch):
        """The renderers use this string as a 'still the right file?' cache
        key, so an unstable answer would reload every block forever."""
        (tmp_path / "m").mkdir()
        (tmp_path / "m" / "c.wav").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)
        b = self._backend_at(tmp_path)
        first = b._resolve_media_path("m/c.wav")
        assert all(b._resolve_media_path("m/c.wav") == first for _ in range(50))

    def test_a_recompile_re_evaluates(self, tmp_path, monkeypatch):
        """Drop the missing file into place, recompile, and it is found --
        the memo must not outlive the compile that built it."""
        monkeypatch.chdir(tmp_path)
        patch = Patch()
        patch.add_module("sampler", params={"path": "late.wav"})
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b.compile(patch)
        assert b._resolve_media_path("late.wav") == "late.wav"   # not there yet

        _write_wav(tmp_path / "late.wav")
        b.compile(patch)
        assert Path(b._resolve_media_path("late.wav")) == tmp_path / "late.wav"


# ----- Patch.source_path -----------------------------------------------------


class TestSourcePath:
    def test_load_stamps_an_absolute_source_path(self, tmp_path):
        p = tmp_path / "x.json"
        save_patch(Patch(), p)
        loaded = load_patch(p)
        assert Path(loaded.source_path) == p.resolve()

    def test_save_stamps_the_new_location(self, tmp_path):
        """Save-As then recompile must resolve against the NEW folder."""
        patch = Patch()
        first, second = tmp_path / "a.json", tmp_path / "sub" / "b.json"
        second.parent.mkdir()
        save_patch(patch, first)
        assert Path(patch.source_path) == first.resolve()
        save_patch(patch, second)
        assert Path(patch.source_path) == second.resolve()

    def test_source_path_is_not_serialized(self, tmp_path):
        """It describes where the file IS, not what it contains -- it must
        not travel inside it, or a copied patch would point at the
        original's folder."""
        import json

        p = tmp_path / "x.json"
        patch = Patch()
        patch.add_module("oscillator")
        save_patch(patch, p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "source_path" not in data
        assert "source_path" not in json.dumps(data)

    def test_in_memory_patch_has_no_source_path(self):
        assert Patch().source_path is None

    def test_compile_picks_up_the_patch_folder(self, tmp_path):
        p = tmp_path / "x.json"
        save_patch(Patch(), p)
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b.compile(load_patch(p))
        assert Path(b._patch_dir) == tmp_path.resolve()


# ----- Failures are reportable, not silent -----------------------------------


class TestFailureIsVisible:
    def _settled(self, backend, patch, tries=400):
        """Render until the background loader has resolved one way or the
        other -- the load is off-thread, so a failure is not instant."""
        for _ in range(tries):
            backend.render_block_multi(256)
            failures = backend.media_load_failures()
            if failures:
                return failures
        return backend.media_load_failures()

    def test_a_missing_sample_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        patch = Patch()
        sampler = patch.add_module("sampler", params={"path": "ghost.wav"})
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b.compile(patch)
        failures = self._settled(b, patch)
        assert failures, "a missing sample must be reportable, not silent"
        mid, mtype, path = failures[0]
        assert (mid, mtype) == (sampler.id, "sampler")
        # The path AS WRITTEN -- that is what the user will go looking for.
        assert path == "ghost.wav"

    def test_a_healthy_sample_is_not_reported(self, tmp_path, monkeypatch):
        wav = EXAMPLES / "samples" / "breaks.wav"
        if not wav.is_file():
            pytest.skip("run examples/samples/generate_samples.py first")
        monkeypatch.chdir(tmp_path)
        patch = Patch()
        patch.add_module("sampler", params={"path": str(wav)})
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b.compile(patch)
        for _ in range(200):
            b.render_block_multi(256)
        assert b.media_load_failures() == []

    def test_an_empty_path_is_not_a_failure(self, tmp_path, monkeypatch):
        """An unpatched slot is idle, not broken."""
        monkeypatch.chdir(tmp_path)
        patch = Patch()
        patch.add_module("sampler", params={"path": ""})
        patch.add_module("convolver", params={"path": ""})
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b.compile(patch)
        for _ in range(50):
            b.render_block_multi(256)
        assert b.media_load_failures() == []

    def test_no_patch_compiled_is_not_a_failure(self):
        b = NumpyBackend(sample_rate=44100, block_size=256)
        assert b.media_load_failures() == []
