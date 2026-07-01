"""Tests for the Linkwitz-Riley crossover."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.crossover import Crossover


SR = 44100


def _build_patch_with_test_tone(freq: float, amp: float = 0.5):
    """Helper: oscillator → crossover, return (patch, osc, xo)."""
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": "sine", "freq": freq, "amp": amp},
    )
    xo = patch.add_module("crossover", params={"freq": 1000.0})
    patch.connect(osc.id, "out", xo.id, "in")
    return patch, osc, xo


def _capture_xo_outputs(patch, xo, backend, frames):
    """Drive the topo and pluck out the crossover's low/high buffers."""
    bufs = {}
    for mid in backend._topo_order:
        mod = patch.modules[mid]
        res = backend._render_module(mod, frames, bufs, patch)
        if isinstance(res, dict):
            for pn, b in res.items():
                bufs[(mid, pn)] = b
        elif res is not None and mod.OUTPUT_PORTS:
            bufs[(mid, mod.OUTPUT_PORTS[0].name)] = res
    return bufs[(xo.id, "low")], bufs[(xo.id, "high")]


class TestCrossoverModel:
    def test_register_and_defaults(self):
        patch = Patch()
        xo = patch.add_module("crossover")
        assert isinstance(xo, Crossover)
        assert xo.params == {"freq": 1000.0}
        assert [p.name for p in xo.input_ports] == ["in"]
        assert xo.input_ports[0].signal_kind == "audio"
        assert [p.name for p in xo.output_ports] == ["low", "high"]
        assert all(p.signal_kind == "audio" for p in xo.output_ports)

    def test_unpatched_input_yields_silence_on_both(self):
        patch = Patch()
        xo = patch.add_module("crossover")
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        out = backend._render_crossover(xo, frames=64, buffers={}, patch=patch)
        assert isinstance(out, dict)
        assert np.all(out["low"] == 0.0)
        assert np.all(out["high"] == 0.0)


class TestCrossoverBehavior:
    def test_lf_tone_lands_mostly_in_low_branch(self):
        """A 100 Hz sine well below the 1 kHz corner should come out the
        ``low`` port at roughly full amplitude and the ``high`` port near
        silence."""
        patch, _, xo = _build_patch_with_test_tone(freq=100.0, amp=0.5)
        backend = NumpyBackend(sample_rate=SR, block_size=4096)
        backend.compile(patch)
        # Render two blocks: first warms up the biquads, second is steady-state.
        _ = _capture_xo_outputs(patch, xo, backend, frames=4096)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=4096)
        rms_low = float(np.sqrt(np.mean(low ** 2)))
        rms_high = float(np.sqrt(np.mean(high ** 2)))
        # Source RMS ≈ 0.5 / sqrt(2) ≈ 0.354. Low branch should be close.
        assert rms_low > 0.25, f"LF RMS in low branch too small: {rms_low}"
        # High branch should be very small at 3.3 octaves below corner
        # (LR4 = -24 dB/oct → about -80 dB).
        assert rms_high < 0.02, f"LF leaked into high branch: {rms_high}"

    def test_hf_tone_lands_mostly_in_high_branch(self):
        patch, _, xo = _build_patch_with_test_tone(freq=8000.0, amp=0.5)
        backend = NumpyBackend(sample_rate=SR, block_size=4096)
        backend.compile(patch)
        _ = _capture_xo_outputs(patch, xo, backend, frames=4096)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=4096)
        rms_low = float(np.sqrt(np.mean(low ** 2)))
        rms_high = float(np.sqrt(np.mean(high ** 2)))
        assert rms_high > 0.25, f"HF RMS in high branch too small: {rms_high}"
        assert rms_low < 0.02, f"HF leaked into low branch: {rms_low}"

    def test_at_corner_both_branches_are_minus_six_db(self):
        """At the LR4 corner each branch should be -6 dB (half amplitude)
        relative to the source. Tolerate ±25% to absorb the finite
        block / numerical noise."""
        patch, _, xo = _build_patch_with_test_tone(freq=1000.0, amp=0.5)
        backend = NumpyBackend(sample_rate=SR, block_size=8192)
        backend.compile(patch)
        _ = _capture_xo_outputs(patch, xo, backend, frames=8192)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=8192)
        rms_low = float(np.sqrt(np.mean(low ** 2)))
        rms_high = float(np.sqrt(np.mean(high ** 2)))
        source_rms = 0.5 / (2.0 ** 0.5)  # ≈ 0.354
        target = source_rms * 0.5  # -6 dB
        assert 0.75 * target < rms_low < 1.25 * target, rms_low
        assert 0.75 * target < rms_high < 1.25 * target, rms_high

    def test_low_plus_high_summed_back_through_combiner(self):
        """An LR4 with low+high routed into a Combiner reconstructs a
        signal whose RMS is within ~10% of the original — the LR4 phase
        relationship is built precisely for this clean recombination."""
        sr = SR
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 1500.0, "amp": 0.5},
        )
        xo = patch.add_module("crossover", params={"freq": 1000.0})
        comb = patch.add_module("combiner")
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc.id, "out", xo.id, "in")
        patch.connect(xo.id, "low", comb.id, "in1")
        patch.connect(xo.id, "high", comb.id, "in2")
        patch.connect(comb.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=8192)
        backend.compile(patch)
        _ = backend.render_block(8192)  # warm
        block = backend.render_block(8192)
        rms = float(np.sqrt(np.mean(block[:, 0] ** 2)))
        source_rms = 0.5 / (2.0 ** 0.5)
        # Allow generous slack — LR4 sums to all-pass magnitude in theory,
        # but block-rate measurement and the clip in the speaker stage
        # introduce small deviations.
        assert 0.85 * source_rms < rms < 1.15 * source_rms, rms

    def test_extreme_frequency_clamps_safely(self):
        patch, _, xo = _build_patch_with_test_tone(freq=440.0)
        # Crank to absurd values; renderer should clamp without NaN/inf.
        xo.set_param("freq", 1e9)
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=512)
        assert np.all(np.isfinite(low))
        assert np.all(np.isfinite(high))
        xo.set_param("freq", 0.0001)
        backend.compile(patch)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=512)
        assert np.all(np.isfinite(low))
        assert np.all(np.isfinite(high))
