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
