"""Tests for Combiner (audio many→one summer) and CVCombiner."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.combiner import COMBINER_INPUT_NAMES, Combiner
from pysynthrack.modules.cvcombiner import (
    CVCOMBINER_INPUT_NAMES,
    CVCOMBINER_MODES,
    CVCombiner,
)


def _backend() -> NumpyBackend:
    return NumpyBackend(sample_rate=44100, block_size=512)


class TestCombinerModel:
    def test_register_and_defaults(self):
        patch = Patch()
        c = patch.add_module("combiner")
        assert isinstance(c, Combiner)
        assert [p.name for p in c.input_ports] == list(COMBINER_INPUT_NAMES)
        assert all(p.signal_kind == "audio" for p in c.input_ports)
        assert [p.name for p in c.output_ports] == ["out"]
        assert c.output_ports[0].signal_kind == "audio"

    def test_rejects_cv_into_audio_input(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        c = patch.add_module("combiner")
        try:
            patch.connect(lfo.id, "cv", c.id, "in1")
        except ValueError:
            return
        raise AssertionError("combiner accepted a CV cable into an audio input")


class TestCombinerBehavior:
    def test_silent_when_no_inputs_connected(self):
        patch = Patch()
        c = patch.add_module("combiner")
        backend = _backend()
        backend.compile(patch)
        out = backend._render_combiner(c, frames=64, buffers={}, patch=patch)
        assert np.all(out == 0.0)

    def test_sums_two_oscillators(self):
        """Two unit-amp sines at different freqs sum to a non-flat waveform
        whose RMS is plausibly larger than either source alone."""
        sr = 44100
        patch = Patch()
        osc_a = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 220.0, "amp": 0.5},
        )
        osc_b = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5},
        )
        c = patch.add_module("combiner")
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc_a.id, "out", c.id, "in1")
        patch.connect(osc_b.id, "out", c.id, "in2")
        patch.connect(c.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        block = backend.render_block(512)
        rms = float(np.sqrt(np.mean(block[:, 0] ** 2)))
        # Two ~0.35 RMS sines summed give somewhere around 0.5 RMS.
        assert 0.3 < rms < 0.9

    def test_unconnected_inputs_are_silent(self):
        """Connecting just in3 shouldn't multiply or zero the signal — it
        should pass straight through at unit gain."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5},
        )
        c = patch.add_module("combiner")
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc.id, "out", c.id, "in3")
        patch.connect(c.id, "out", spk.id, "in")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        block = backend.render_block(512)
        # Source RMS at amp=0.5 should be ~0.35.
        rms = float(np.sqrt(np.mean(block[:, 0] ** 2)))
        assert 0.25 < rms < 0.45

    def test_combiner_lets_one_output_fan_to_many(self):
        """Use a combiner downstream of a single oscillator wired through
        TWO cables (audio fanout). The combiner of two copies of A is 2A —
        verifying fanout actually duplicates the signal."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 220.0, "amp": 0.4},
        )
        c = patch.add_module("combiner")
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        # Two cables from osc.out → in1 and in2 of combiner.
        patch.connect(osc.id, "out", c.id, "in1")
        patch.connect(osc.id, "out", c.id, "in2")
        patch.connect(c.id, "out", spk.id, "in")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        block = backend.render_block(512)
        # A=0.4 amp → RMS ≈ 0.283; doubled → 0.566 RMS (clipped at 1.0).
        rms = float(np.sqrt(np.mean(block[:, 0] ** 2)))
        assert 0.45 < rms < 0.85, f"expected ~doubled RMS, got {rms}"


class TestCVCombinerModel:
    def test_register_and_defaults(self):
        patch = Patch()
        c = patch.add_module("cv_combiner")
        assert isinstance(c, CVCombiner)
        assert c.params == {"mode": "sum"}
        assert [p.name for p in c.input_ports] == list(CVCOMBINER_INPUT_NAMES)
        assert all(p.signal_kind == "cv" for p in c.input_ports)
        assert [p.name for p in c.output_ports] == ["out"]
        assert c.output_ports[0].signal_kind == "cv"

    def test_modes_constant(self):
        for m in ("sum", "average"):
            assert m in CVCOMBINER_MODES

    def test_rejects_audio_into_cv_input(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        c = patch.add_module("cv_combiner")
        try:
            patch.connect(osc.id, "out", c.id, "in1")
        except ValueError:
            return
        raise AssertionError("cv_combiner accepted an audio cable")


class TestCVCombinerBehavior:
    def test_silent_when_no_inputs(self):
        patch = Patch()
        c = patch.add_module("cv_combiner")
        backend = _backend()
        backend.compile(patch)
        out = backend._render_cv_combiner(c, frames=64, buffers={}, patch=patch)
        assert np.all(out == 0.0)

    def test_sum_mode_adds(self):
        """Two LFOs at depth=1 unipolar give a max of 2 in sum mode."""
        sr = 44100
        patch = Patch()
        lfo_a = patch.add_module(
            "lfo",
            params={"waveform": "square", "rate": 1.0, "depth": 1.0, "bipolar": False},
        )
        lfo_b = patch.add_module(
            "lfo",
            params={"waveform": "square", "rate": 1.0, "depth": 1.0, "bipolar": False},
        )
        cc = patch.add_module("cv_combiner", params={"mode": "sum"})
        patch.connect(lfo_a.id, "cv", cc.id, "in1")
        patch.connect(lfo_b.id, "cv", cc.id, "in2")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        # Walk topo once and capture the cv_combiner buffer.
        # At t=0 a 1Hz square is in its HIGH portion for the first 500ms,
        # so a single 512-sample render lands solidly in HIGH=1.0 territory.
        bufs = {}
        for mid in backend._topo_order:
            mod = patch.modules[mid]
            res = backend._render_module(mod, 512, bufs, patch)
            if isinstance(res, dict):
                for pn, b in res.items():
                    bufs[(mid, pn)] = b
            elif res is not None and mod.OUTPUT_PORTS:
                bufs[(mid, mod.OUTPUT_PORTS[0].name)] = res
        out = bufs[(cc.id, "out")]
        # Max of two unit unipolar squares summed = 2.0 exactly.
        assert float(out.max()) > 1.5

    def test_average_mode_divides_by_connected_count(self):
        """Same two LFOs but in average mode stay within depth=1 range."""
        sr = 44100
        patch = Patch()
        lfo_a = patch.add_module(
            "lfo",
            params={"waveform": "square", "rate": 1.0, "depth": 1.0, "bipolar": False},
        )
        lfo_b = patch.add_module(
            "lfo",
            params={"waveform": "square", "rate": 1.0, "depth": 1.0, "bipolar": False},
        )
        cc = patch.add_module("cv_combiner", params={"mode": "average"})
        patch.connect(lfo_a.id, "cv", cc.id, "in1")
        patch.connect(lfo_b.id, "cv", cc.id, "in2")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        # Manually re-run topo to capture the CV buffer.
        bufs = {}
        for mid in backend._topo_order:
            mod = patch.modules[mid]
            res = backend._render_module(mod, 512, bufs, patch)
            if isinstance(res, dict):
                for pn, b in res.items():
                    bufs[(mid, pn)] = b
            elif res is not None and mod.OUTPUT_PORTS:
                bufs[(mid, mod.OUTPUT_PORTS[0].name)] = res
        out = bufs[(cc.id, "out")]
        # Average of two unipolar squares is bounded by [0, 1].
        assert float(out.max()) <= 1.0 + 1e-6
        # And actually reaches near 1 where both are high.
        assert float(out.max()) > 0.9
