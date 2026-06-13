"""Tests for the CV meter capture in NumpyBackend.

The backend records one block-mean scalar per cv-kind output port into
``_meter_levels`` each block, swapping the dict reference atomically so
the GUI thread can read a coherent snapshot via ``snapshot_meter_levels``.
These tests exercise the capture, the per-compile rebuild/reset, voice-
aware collapse, and snapshot isolation -- everything the UI relies on,
all headless.
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch


def _render(backend: NumpyBackend, blocks: int, frames: int = 512) -> None:
    for _ in range(blocks):
        backend.render_block(frames)


class TestCvMeterCapture:
    def test_lfo_cv_output_is_metered(self):
        """An LFO's cv output should appear in the levels and sit in the
        bipolar range it actually emits."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 2.0, "depth": 1.0, "bipolar": True},
        )
        backend.compile(patch)
        _render(backend, 10)

        levels = backend.snapshot_meter_levels()
        assert (lfo.id, "cv") in levels
        assert -1.0 <= levels[(lfo.id, "cv")] <= 1.0

    def test_cv_output_ports_precomputed_on_compile(self):
        """compile() builds the (module_id, port) list of cv outputs so
        render_block doesn't re-derive signal kinds per block."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        lfo = patch.add_module("lfo")
        adsr = patch.add_module("adsr")
        backend.compile(patch)

        assert set(backend._cv_output_ports) == {(lfo.id, "cv"), (adsr.id, "cv")}

    def test_idle_adsr_reads_zero(self):
        """An ADSR with no gate sits idle -> its metered level is 0.0."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        adsr = patch.add_module("adsr")
        backend.compile(patch)
        _render(backend, 5)

        levels = backend.snapshot_meter_levels()
        assert levels.get((adsr.id, "cv")) == 0.0

    def test_levels_reset_and_rebuild_on_recompile(self):
        """A recompile to a patch with no cv outputs clears the levels
        and the precomputed port list; no stale meters linger."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        cv_patch = Patch()
        cv_patch.add_module("lfo")
        backend.compile(cv_patch)
        _render(backend, 3)
        assert backend.snapshot_meter_levels()  # non-empty

        audio_only = Patch()
        osc = audio_only.add_module("oscillator")
        spk = audio_only.add_module("speaker_output")
        audio_only.connect(osc.id, "out", spk.id, "in")
        backend.compile(audio_only)

        assert backend._cv_output_ports == []
        assert backend.snapshot_meter_levels() == {}
        _render(backend, 3)
        # Audio-only patch never populates the meters.
        assert backend.snapshot_meter_levels() == {}

    def test_voice_aware_cv_collapses_via_full_mean(self):
        """A polyphonic gate driving an ADSR yields a (V, F) cv buffer;
        the meter collapses it with a full mean rather than crashing on
        the extra axis."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        kb = patch.add_module("keyboard")
        adsr = patch.add_module("adsr")
        patch.connect(kb.id, "gate", adsr.id, "gate")
        backend.compile(patch)

        kb.note_on(60)
        kb.note_on(64)
        _render(backend, 5)

        levels = backend.snapshot_meter_levels()
        assert (adsr.id, "cv") in levels
        # Gates are rising through attack -> strictly above the idle 0.
        assert levels[(adsr.id, "cv")] > 0.0

    def test_snapshot_is_an_isolated_copy(self):
        """Mutating a returned snapshot must not touch the backend's
        live dict (the GUI holds onto its copy across frames)."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        patch.add_module("lfo")
        backend.compile(patch)
        _render(backend, 2)

        snap = backend.snapshot_meter_levels()
        snap[(999, "fake")] = 123.0
        assert (999, "fake") not in backend.snapshot_meter_levels()

    def test_disconnected_cv_port_simply_absent(self):
        """If a cv-output module renders nothing (no buffer), its key is
        just missing from the levels rather than present-but-garbage."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        # A bare patch with only a speaker -- no cv sources at all.
        patch.add_module("speaker_output")
        backend.compile(patch)
        _render(backend, 2)
        assert backend.snapshot_meter_levels() == {}
