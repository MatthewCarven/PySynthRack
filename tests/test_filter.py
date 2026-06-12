"""Tests for the Filter (RBJ biquad) module."""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.filter import FILTER_MODES, Filter


def _wire_osc_filter_speaker(waveform: str, freq: float, mode: str, cutoff: float, q: float):
    """Build osc → filter → speaker. Returns (patch, osc, filt)."""
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": waveform, "freq": freq, "amp": 0.8}
    )
    filt = patch.add_module(
        "filter", params={"mode": mode, "cutoff": cutoff, "resonance": q}
    )
    spk = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", filt.id, "in")
    patch.connect(filt.id, "out", spk.id, "in")
    return patch, osc, filt


def _render_blocks(backend: NumpyBackend, patch: Patch, blocks: int) -> np.ndarray:
    """Render N consecutive blocks for the filter module; returns concatenated output."""
    filt = next(m for m in patch if m.TYPE == "filter")
    osc = next(m for m in patch if m.TYPE == "oscillator")
    chunks = []
    for _ in range(blocks):
        # Buffers are port-keyed since the multi-output Keyboard landed.
        buffers: dict = {}
        buffers[(osc.id, "out")] = backend._render_oscillator(
            osc, frames=backend.block_size
        )
        chunks.append(backend._render_filter(filt, backend.block_size, buffers, patch))
    return np.concatenate(chunks)


class TestFilterModel:
    def test_register_and_construct(self):
        patch = Patch()
        f = patch.add_module("filter")
        assert isinstance(f, Filter)
        assert f.params == {"mode": "lowpass", "cutoff": 1000.0, "resonance": 0.707}
        # v0.3: filter exposes audio in + an optional cutoff CV input.
        assert [p.name for p in f.input_ports] == ["in", "cutoff_cv"]
        cv_port = next(p for p in f.input_ports if p.name == "cutoff_cv")
        assert cv_port.signal_kind == "cv"
        assert [p.name for p in f.output_ports] == ["out"]

    def test_modes_constant_matches_defaults(self):
        assert "lowpass" in FILTER_MODES
        assert "highpass" in FILTER_MODES
        assert "bandpass" in FILTER_MODES

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "filter", params={"mode": "bandpass", "cutoff": 800.0, "resonance": 4.0}
        )
        restored = Patch.from_dict(patch.to_dict())
        f = next(m for m in restored if m.TYPE == "filter")
        assert f.params["mode"] == "bandpass"
        assert f.params["cutoff"] == 800.0
        assert f.params["resonance"] == 4.0


class TestFilterBehavior:
    """Smoke tests for filter shape — RMS comparisons across modes/cutoffs."""

    def _rms(self, buf: np.ndarray) -> float:
        return float(np.sqrt(np.mean(buf.astype(np.float64) ** 2)))

    def test_lowpass_attenuates_high_freq(self):
        """A 4 kHz tone through a 200 Hz LP should be much quieter than
        the unfiltered signal."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt = _wire_osc_filter_speaker(
            "sine", freq=4000.0, mode="lowpass", cutoff=200.0, q=0.707
        )
        backend.compile(patch)
        # Render enough blocks to let the IIR settle past its transient.
        filtered = _render_blocks(backend, patch, blocks=10)
        # Stable region — drop the first block to skip the warmup transient.
        steady = filtered[1024:]
        # Unfiltered amplitude is 0.8 → RMS ≈ 0.566; filtered should be ≪.
        assert self._rms(steady) < 0.1

    def test_lowpass_passes_low_freq(self):
        """A 100 Hz tone through a 2 kHz LP should pass close to unaltered."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt = _wire_osc_filter_speaker(
            "sine", freq=100.0, mode="lowpass", cutoff=2000.0, q=0.707
        )
        backend.compile(patch)
        filtered = _render_blocks(backend, patch, blocks=10)
        steady = filtered[1024:]
        # Expect close to source amplitude (0.8 amp → ~0.566 RMS).
        assert self._rms(steady) > 0.4

    def test_highpass_attenuates_low_freq(self):
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt = _wire_osc_filter_speaker(
            "sine", freq=80.0, mode="highpass", cutoff=2000.0, q=0.707
        )
        backend.compile(patch)
        filtered = _render_blocks(backend, patch, blocks=10)
        steady = filtered[1024:]
        assert self._rms(steady) < 0.1

    def test_highpass_passes_high_freq(self):
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt = _wire_osc_filter_speaker(
            "sine", freq=8000.0, mode="highpass", cutoff=2000.0, q=0.707
        )
        backend.compile(patch)
        filtered = _render_blocks(backend, patch, blocks=10)
        steady = filtered[1024:]
        assert self._rms(steady) > 0.4

    def test_bandpass_passes_centre_freq_attenuates_far_freqs(self):
        """1 kHz BP: 1 kHz tone passes; 100 Hz and 10 kHz tones don't."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)

        # Centre frequency — should pass.
        backend._state.clear()
        patch_c, _, _ = _wire_osc_filter_speaker(
            "sine", freq=1000.0, mode="bandpass", cutoff=1000.0, q=2.0
        )
        backend.compile(patch_c)
        centre = _render_blocks(backend, patch_c, blocks=10)[1024:]
        centre_rms = self._rms(centre)

        # Far below the band — should attenuate.
        backend._state.clear()
        patch_low, _, _ = _wire_osc_filter_speaker(
            "sine", freq=100.0, mode="bandpass", cutoff=1000.0, q=2.0
        )
        backend.compile(patch_low)
        low = _render_blocks(backend, patch_low, blocks=10)[1024:]
        low_rms = self._rms(low)

        # Far above the band — should attenuate.
        backend._state.clear()
        patch_high, _, _ = _wire_osc_filter_speaker(
            "sine", freq=10000.0, mode="bandpass", cutoff=1000.0, q=2.0
        )
        backend.compile(patch_high)
        high = _render_blocks(backend, patch_high, blocks=10)[1024:]
        high_rms = self._rms(high)

        assert centre_rms > low_rms * 3
        assert centre_rms > high_rms * 3


class TestFilterStability:
    def test_silent_input_silent_output(self):
        """Zero input should give zero output regardless of params."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        patch, osc, filt = _wire_osc_filter_speaker(
            "sine", freq=1000.0, mode="lowpass", cutoff=500.0, q=5.0
        )
        backend.compile(patch)
        buffers = {(osc.id, "out"): np.zeros(512, dtype=np.float32)}
        out = backend._render_filter(filt, 512, buffers, patch)
        assert np.allclose(out, 0.0)

    @pytest.mark.parametrize("mode", FILTER_MODES)
    def test_no_nan_or_inf_with_extreme_q(self, mode):
        """High-Q filter should not blow up — clamping must keep it stable."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt = _wire_osc_filter_speaker(
            "saw", freq=440.0, mode=mode, cutoff=500.0, q=15.0
        )
        backend.compile(patch)
        for _ in range(5):
            buffers: dict = {}
            buffers[(osc.id, "out")] = backend._render_oscillator(osc, 1024)
            out = backend._render_filter(filt, 1024, buffers, patch)
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) < 50.0  # generous bound vs. unstable explosion

    def test_disconnected_filter_outputs_silence(self):
        """Filter with no incoming cable should output zeros, not crash."""
        patch = Patch()
        filt = patch.add_module("filter")
        backend = NumpyBackend(sample_rate=44100, block_size=256)
        backend.compile(patch)
        out = backend._render_filter(filt, 256, {}, patch)
        assert np.allclose(out, 0.0)


def _reference_filter_mono(backend, module, blocks, cv_blocks=None):
    """The pre-slice-3 per-sample DF-I loop, kept verbatim as the oracle.

    This is the exact mono implementation `_render_filter_mono` used
    before filter vectorization slice 3 replaced it with
    scipy.signal.lfilter: scalar Python recurrence, float64 math,
    float32 output, raw (x1, x2, y1, y2) history carried across blocks
    and coefficients recomputed per block from the block-mean cutoff_cv.
    """
    mode = str(module.params.get("mode", "lowpass"))
    base = float(module.params.get("cutoff", 1000.0))
    q = float(module.params.get("resonance", 0.707))
    x1 = x2 = y1 = y2 = 0.0
    outs = []
    for i, src_buf in enumerate(blocks):
        cutoff = base
        cv = None if cv_blocks is None else cv_blocks[i]
        if cv is not None and cv.size > 0:
            cutoff = cutoff * float(2.0 ** float(np.mean(cv)))
        b0, b1, b2, a1n, a2n = backend._filter_coeffs(mode, cutoff, q)
        out = np.empty(len(src_buf), dtype=np.float32)
        for n in range(len(src_buf)):
            x0 = float(src_buf[n])
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2
            out[n] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0
        outs.append(out)
    return np.concatenate(outs)


class TestFilterMonoLfilterEquivalence:
    """Slice 3: the lfilter mono path must match the old per-sample loop.

    The new implementation carries raw DF-I history (coefficient-
    independent) and converts it to lfilter's zi at block start, so it
    should be *bit-identical* to the old loop after the float32 cast --
    including across blocks where cutoff_cv changes the coefficients.
    We still assert with a small tolerance rather than == so a future
    scipy that reorders float ops doesn't break the suite spuriously.
    """

    @pytest.mark.parametrize("mode", FILTER_MODES)
    def test_multiblock_equivalence_static_cutoff(self, mode):
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        filt = patch.add_module(
            "filter", params={"mode": mode, "cutoff": 1000.0, "resonance": 2.0}
        )
        rng = np.random.default_rng(42)
        blocks = [
            (rng.standard_normal(512) * 0.5).astype(np.float32) for _ in range(8)
        ]
        got = np.concatenate(
            [backend._render_filter_mono(filt, 512, b, None) for b in blocks]
        )
        ref = _reference_filter_mono(backend, filt, blocks)
        assert got.dtype == np.float32
        assert np.max(np.abs(got.astype(np.float64) - ref.astype(np.float64))) < 1e-6

    def test_equivalence_with_per_block_cutoff_cv(self):
        """Coefficients change between blocks; raw-history state carry
        must reproduce the old loop exactly (this is the case that
        carrying lfilter's zf across blocks would get wrong)."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        filt = patch.add_module(
            "filter", params={"mode": "lowpass", "cutoff": 800.0, "resonance": 4.0}
        )
        rng = np.random.default_rng(7)
        blocks = [
            (rng.standard_normal(512) * 0.5).astype(np.float32) for _ in range(8)
        ]
        sweep = (-1.0, -0.5, 0.0, 0.7, 1.5, -2.0, 2.0, 0.25)
        cvs = [np.full(512, c, dtype=np.float32) for c in sweep]
        got = np.concatenate(
            [
                backend._render_filter_mono(filt, 512, b, cv)
                for b, cv in zip(blocks, cvs)
            ]
        )
        ref = _reference_filter_mono(backend, filt, blocks, cvs)
        assert np.max(np.abs(got.astype(np.float64) - ref.astype(np.float64))) < 1e-6

    def test_single_sample_blocks(self):
        """frames=1 exercises the history-tail edge case (x2/y2 must
        come from the carried state, not the one-sample buffer)."""
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        patch = Patch()
        filt = patch.add_module(
            "filter", params={"mode": "lowpass", "cutoff": 1000.0, "resonance": 2.0}
        )
        rng = np.random.default_rng(3)
        ones = [rng.standard_normal(1).astype(np.float32) for _ in range(64)]
        got = np.concatenate(
            [backend._render_filter_mono(filt, 1, b, None) for b in ones]
        )
        ref = _reference_filter_mono(backend, filt, ones)
        assert np.max(np.abs(got.astype(np.float64) - ref.astype(np.float64))) < 1e-6

    def test_split_render_matches_whole_render(self):
        """Intrinsic continuity check, no oracle: filtering two 512-
        sample blocks back to back must equal filtering the same 1024
        samples in one call."""
        rng = np.random.default_rng(11)
        big = (rng.standard_normal(1024) * 0.5).astype(np.float32)

        def fresh():
            patch = Patch()
            filt = patch.add_module(
                "filter",
                params={"mode": "bandpass", "cutoff": 2000.0, "resonance": 3.0},
            )
            return NumpyBackend(sample_rate=44100, block_size=512), filt

        b1, f1 = fresh()
        split = np.concatenate(
            [
                b1._render_filter_mono(f1, 512, big[:512], None),
                b1._render_filter_mono(f1, 512, big[512:], None),
            ]
        )
        b2, f2 = fresh()
        whole = b2._render_filter_mono(f2, 1024, big, None)
        assert np.max(np.abs(split.astype(np.float64) - whole.astype(np.float64))) < 1e-6

    def test_unknown_mode_passthrough_unchanged(self):
        backend = NumpyBackend(sample_rate=44100, block_size=256)
        patch = Patch()
        filt = patch.add_module("filter", params={"mode": "notch?!"})
        buf = np.linspace(-1, 1, 256, dtype=np.float32)
        out = backend._render_filter_mono(filt, 256, buf, None)
        assert np.array_equal(out, buf)
