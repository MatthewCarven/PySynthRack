"""Tests for the Filter (RBJ biquad) module."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type
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
        assert f.params == {
            "mode": "lowpass",
            "cutoff": 1000.0,
            "resonance": 0.707,
            "cv_depth": 1.0,
            "res_cv_depth": 1.0,
        }
        # v0.3: filter exposes audio in + an optional cutoff CV input;
        # the 2026-09-19 love pass added the resonance CV beside it.
        assert [p.name for p in f.input_ports] == ["in", "cutoff_cv", "resonance_cv"]
        for name in ("cutoff_cv", "resonance_cv"):
            cv_port = next(p for p in f.input_ports if p.name == name)
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


def _reference_filter_voice(backend, module, blocks, cv_blocks=None):
    """The pre-slice-4 per-sample voice loop, kept verbatim as the oracle.

    Scalar-in-time / vectorized-across-voices recurrence with raw
    (x1, x2, y1, y2) arrays carried across blocks and coefficients
    recomputed per block: shared scalars from _filter_coeffs, or
    per-voice (V,) arrays from a (V, F) cutoff_cv block-mean.
    """
    mode = str(module.params.get("mode", "lowpass"))
    base = float(module.params.get("cutoff", 1000.0))
    q = float(module.params.get("resonance", 0.707))
    V = blocks[0].shape[0]
    x1 = np.zeros(V)
    x2 = np.zeros(V)
    y1 = np.zeros(V)
    y2 = np.zeros(V)
    outs = []
    for i, src_buf in enumerate(blocks):
        cv = None if cv_blocks is None else cv_blocks[i]
        per_voice = (
            cv is not None and cv.ndim == 2 and cv.shape[0] == V and cv.size > 0
        )
        if per_voice:
            sr = backend.sample_rate
            # float64 like the renderer (``_finite_mean``, 2026-09-22):
            # this oracle exists to pin the lfilter path against the
            # per-sample loop, not to restate how the block mean
            # accumulates, and a float32 reduction here puts the two
            # coefficient sets ~1e-7 apart -- which a resonant biquad
            # amplifies past the tolerance.
            cpv = np.clip(
                base * np.power(2.0, cv.mean(axis=1, dtype=np.float64)),
                20.0,
                sr * 0.45,
            )
            qc = max(0.1, min(q, 20.0))
            w0 = 2.0 * np.pi * cpv / sr
            cw, sw = np.cos(w0), np.sin(w0)
            al = sw / (2.0 * qc)
            if mode == "lowpass":
                b0 = (1.0 - cw) / 2.0
                b1 = 1.0 - cw
                b2 = (1.0 - cw) / 2.0
            elif mode == "highpass":
                b0 = (1.0 + cw) / 2.0
                b1 = -(1.0 + cw)
                b2 = (1.0 + cw) / 2.0
            else:  # bandpass
                b0 = sw / 2.0
                b1 = np.zeros(V)
                b2 = -sw / 2.0
            a0 = 1.0 + al
            b0, b1, b2 = b0 / a0, b1 / a0, b2 / a0
            a1n = (-2.0 * cw) / a0
            a2n = (1.0 - al) / a0
        else:
            cutoff = base
            if cv is not None and cv.size > 0:
                cutoff = cutoff * float(2.0 ** float(np.mean(cv)))
            b0, b1, b2, a1n, a2n = backend._filter_coeffs(mode, cutoff, q)
        frames = src_buf.shape[1]
        out = np.empty((V, frames), dtype=np.float32)
        for n in range(frames):
            x0 = src_buf[:, n].astype(np.float64)
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2
            out[:, n] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0
        outs.append(out)
    return np.concatenate(outs, axis=1)


class TestFilterVoiceLfilterEquivalence:
    """Slice 4: the lfilter voice path must match the old per-sample loop.

    Same raw-history state design as the mono path, so equivalence is
    bit-identical after the float32 cast -- including across blocks
    where a (V, F) cutoff_cv gives every voice new coefficients each
    block. Tolerance is < 1e-6 rather than == for the same
    scipy-future-proofing reason as the mono tests.
    """

    V = 16
    F = 512

    def _fresh(self, mode="lowpass", cutoff=1000.0, res=2.0):
        patch = Patch()
        filt = patch.add_module(
            "filter", params={"mode": mode, "cutoff": cutoff, "resonance": res}
        )
        return NumpyBackend(sample_rate=44100, block_size=self.F), filt

    def _blocks(self, rng, n, frames=None):
        frames = self.F if frames is None else frames
        return [
            (rng.standard_normal((self.V, frames)) * 0.5).astype(np.float32)
            for _ in range(n)
        ]

    @staticmethod
    def _err(a, b):
        return np.max(np.abs(a.astype(np.float64) - b.astype(np.float64)))

    @pytest.mark.parametrize("mode", FILTER_MODES)
    def test_shared_coeff_multiblock(self, mode):
        backend, filt = self._fresh(mode)
        blocks = self._blocks(np.random.default_rng(42), 8)
        got = np.concatenate(
            [backend._render_filter_voice(filt, self.F, b, None) for b in blocks],
            axis=1,
        )
        ref = _reference_filter_voice(backend, filt, blocks)
        assert got.dtype == np.float32 and got.shape == (self.V, self.F * 8)
        assert self._err(got, ref) < 1e-6

    def test_macro_cv_changing_per_block(self):
        """1D (F,) cutoff_cv: one shared coefficient set per block,
        changing every block -- the macro filter-sweep case."""
        backend, filt = self._fresh("lowpass", 800.0, 4.0)
        rng = np.random.default_rng(7)
        blocks = self._blocks(rng, 8)
        sweep = (-1.0, -0.5, 0.0, 0.7, 1.5, -2.0, 2.0, 0.25)
        cvs = [np.full(self.F, c, dtype=np.float32) for c in sweep]
        got = np.concatenate(
            [
                backend._render_filter_voice(filt, self.F, b, cv)
                for b, cv in zip(blocks, cvs)
            ],
            axis=1,
        )
        ref = _reference_filter_voice(backend, filt, blocks, cvs)
        assert self._err(got, ref) < 1e-6

    def test_per_voice_cv_changing_per_block(self):
        """(V, F) cutoff_cv: every voice gets its own coefficients,
        and they change every block -- the case where raw-history state
        carry matters most (zf-carry would diverge here)."""
        backend, filt = self._fresh("lowpass", 800.0, 4.0)
        rng = np.random.default_rng(13)
        blocks = self._blocks(rng, 8)
        cvs = [
            (rng.standard_normal((self.V, self.F)) * 0.8).astype(np.float32)
            for _ in range(8)
        ]
        got = np.concatenate(
            [
                backend._render_filter_voice(filt, self.F, b, cv)
                for b, cv in zip(blocks, cvs)
            ],
            axis=1,
        )
        ref = _reference_filter_voice(backend, filt, blocks, cvs)
        assert self._err(got, ref) < 1e-6

    def test_single_sample_blocks(self):
        backend, filt = self._fresh()
        rng = np.random.default_rng(3)
        ones = self._blocks(rng, 64, frames=1)
        got = np.concatenate(
            [backend._render_filter_voice(filt, 1, b, None) for b in ones],
            axis=1,
        )
        ref = _reference_filter_voice(backend, filt, ones)
        assert self._err(got, ref) < 1e-6

    def test_split_render_matches_whole_render(self):
        """Intrinsic continuity, no oracle, both coefficient shapes.
        The per-voice variant uses a per-voice-constant CV so the
        block-mean (and thus the coefficients) is identical whether the
        signal is rendered split or whole."""
        rng = np.random.default_rng(11)
        big = (rng.standard_normal((self.V, 1024)) * 0.5).astype(np.float32)
        cv = np.tile(
            np.linspace(-1.0, 1.0, self.V, dtype=np.float32)[:, None], (1, 1024)
        )
        for cv_whole in (None, cv):
            b1, f1 = self._fresh("bandpass", 2000.0, 3.0)
            halves = (
                (big[:, : self.F], None if cv_whole is None else cv_whole[:, : self.F]),
                (big[:, self.F :], None if cv_whole is None else cv_whole[:, self.F :]),
            )
            split = np.concatenate(
                [
                    b1._render_filter_voice(f1, self.F, part, part_cv)
                    for part, part_cv in halves
                ],
                axis=1,
            )
            b2, f2 = self._fresh("bandpass", 2000.0, 3.0)
            whole = b2._render_filter_voice(f2, 1024, big, cv_whole)
            assert self._err(split, whole) < 1e-6

    def test_mono_then_voice_reinits_state(self):
        """Switching audio shape mono -> voice must discard mono state
        and start the voice biquads from silence."""
        backend, filt = self._fresh()
        rng = np.random.default_rng(5)
        mono_buf = (rng.standard_normal(self.F) * 0.5).astype(np.float32)
        backend._render_filter_mono(filt, self.F, mono_buf, None)
        blocks = self._blocks(rng, 2)
        got = np.concatenate(
            [backend._render_filter_voice(filt, self.F, b, None) for b in blocks],
            axis=1,
        )
        ref = _reference_filter_voice(backend, filt, blocks)  # zero state
        assert self._err(got, ref) < 1e-6

    def test_unknown_mode_passthrough_per_voice_branch(self):
        backend, filt = self._fresh("allpass??")
        rng = np.random.default_rng(9)
        buf = (rng.standard_normal((self.V, 256))).astype(np.float32)
        cv = np.zeros((self.V, 256), dtype=np.float32)
        out = backend._render_filter_voice(filt, 256, buf, cv)
        assert np.array_equal(out, buf)


# ----- resonance_cv (love pass 2026-09-19) ----------------------------------

SR = 44100
F = 512


def _res_patch(mode="lowpass", cutoff=1000.0, q=3.0, res_cv_depth=None, block=F):
    """osc -> filter <- (lfo on cutoff_cv, lfo on resonance_cv).

    Returns (patch, backend, osc, cut_lfo, res_lfo, filt). Nothing is
    rendered through the LFOs -- the tests hand the filter constant CV
    blocks directly, so the cables only exist to make the ports "patched".
    """
    p = Patch()
    osc = p.add_module("oscillator")
    cut_lfo = p.add_module("lfo")
    res_lfo = p.add_module("lfo")
    filt = p.add_module(
        "filter", params={"mode": mode, "cutoff": cutoff, "resonance": q}
    )
    if res_cv_depth is not None:
        filt.set_param("res_cv_depth", res_cv_depth)
    p.connect(osc.id, "out", filt.id, "in")
    p.connect(cut_lfo.id, "cv", filt.id, "cutoff_cv")
    p.connect(res_lfo.id, "cv", filt.id, "resonance_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    return p, b, osc, cut_lfo, res_lfo, filt


def _res_render(x, mode="lowpass", cutoff=1000.0, q=3.0, res_cv=None,
                cut_cv=None, res_cv_depth=None, block=F):
    """Render ``x`` (mono ``(N,)`` or voice ``(V, N)``) block by block.

    ``res_cv`` / ``cut_cv``: None = unpatched (no buffer); a scalar = a
    constant CV block the shape of the audio; an array = per-voice rows
    ``(V, N)`` sliced alongside the audio.
    """
    p, b, osc, cut_lfo, res_lfo, filt = _res_patch(mode, cutoff, q, res_cv_depth, block)
    outs = []
    for i in range(x.shape[-1] // block):
        xs = x[..., i * block:(i + 1) * block]
        bufs = {(osc.id, "out"): xs}
        for src, cv in ((res_lfo, res_cv), (cut_lfo, cut_cv)):
            if cv is None:
                continue
            if np.ndim(cv) == 0:
                bufs[(src.id, "cv")] = np.full(xs.shape, cv, np.float32)
            else:
                bufs[(src.id, "cv")] = cv[..., i * block:(i + 1) * block]
        outs.append(b._render_filter(filt, block, bufs, p))
    return np.concatenate(outs, axis=-1)


def _peak_to_skirt(y, f0=1000.0, seg=4096):
    """Averaged power spectrum: mean power within +/-5 % of ``f0`` over
    the mean power in the skirts (250-500 Hz and 2-4 kHz). The first two
    segments are dropped so the biquad's warm-up doesn't count."""
    w = np.hanning(seg)
    n = len(y) // seg
    acc = np.zeros(seg // 2 + 1)
    for i in range(2, n):
        acc += np.abs(np.fft.rfft(y[i * seg:(i + 1) * seg].astype(np.float64) * w)) ** 2
    fr = np.fft.rfftfreq(seg, 1.0 / SR)
    peak = acc[(fr > f0 * 0.95) & (fr < f0 * 1.05)].mean()
    skirt = acc[((fr > 250) & (fr < 500)) | ((fr > 2000) & (fr < 4000))].mean()
    return float(peak / skirt)


class TestFilterResonanceCv:
    """``resonance_cv`` + ``res_cv_depth``: Q doublings per CV unit.

    ``Q_eff = resonance * 2 ** (res_cv_depth * mean cv)``, block mean
    like cutoff_cv, then the same (0.1, 20) clamp the param goes
    through. The contract: unpatched is the pre-love-pass filter
    bit-for-bit (the reference-render recipe held 86/86 at default);
    depth 0 disables the input without unpatching it; cv +1 at depth 1
    is *exactly* the same render as doubling ``resonance`` (a doubling
    is an exponent bump, so the coefficients are identical); a runaway
    CV pins the Q at the rail rather than blowing the biquad up.
    """

    def setup_method(self):
        rng = np.random.default_rng(42)
        self.x = (rng.standard_normal(F * 80) * 0.3).astype(np.float32)

    # -- model -----------------------------------------------------------------

    def test_pre_love_pass_patch_loads_with_the_default_depth(self):
        d = Patch()
        d.add_module("filter")
        raw = d.to_dict()
        for m in raw["modules"]:
            m["params"].pop("res_cv_depth", None)
        restored = Patch.from_dict(raw)
        f = next(m for m in restored if m.TYPE == "filter")
        assert f.params["res_cv_depth"] == 1.0

    def test_resonance_cv_is_a_cv_port_beside_cutoff_cv(self):
        f = Patch().add_module("filter")
        kinds = {p.name: p.signal_kind for p in f.input_ports}
        assert kinds["resonance_cv"] == "cv"
        assert kinds["cutoff_cv"] == "cv"

    # -- the contract: unpatched / depth 0 ----------------------------------------

    def test_unpatched_resonance_cv_is_the_old_filter(self):
        """The dispatcher with only ``in`` + ``cutoff_cv`` patched must
        reproduce the pre-love-pass oracle loop (kept verbatim above),
        cutoff sweeping per block and all."""
        p = Patch()
        osc = p.add_module("oscillator")
        lfo = p.add_module("lfo")
        filt = p.add_module(
            "filter", params={"mode": "lowpass", "cutoff": 800.0, "resonance": 4.0}
        )
        p.connect(osc.id, "out", filt.id, "in")
        p.connect(lfo.id, "cv", filt.id, "cutoff_cv")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(p)
        blocks = [self.x[i * F:(i + 1) * F] for i in range(8)]
        sweep = (-1.0, -0.5, 0.0, 0.7, 1.5, -2.0, 2.0, 0.25)
        cvs = [np.full(F, c, dtype=np.float32) for c in sweep]
        got = np.concatenate(
            [
                b._render_filter(filt, F, {(osc.id, "out"): blk, (lfo.id, "cv"): cv}, p)
                for blk, cv in zip(blocks, cvs)
            ]
        )
        ref = _reference_filter_mono(b, filt, blocks, cvs)
        assert np.max(np.abs(got.astype(np.float64) - ref.astype(np.float64))) < 1e-6

    def test_cv_zero_is_bit_exact_with_unpatched(self):
        # 2 ** 0 == 1.0 exactly, so q * 1.0 is q: patched-but-idle is
        # not merely close to unpatched, it is the same render.
        patched = _res_render(self.x, res_cv=0.0)
        unpatched = _res_render(self.x)
        assert np.array_equal(patched, unpatched)

    def test_depth_zero_disables_without_unpatching(self):
        with_cv = _res_render(self.x, res_cv=1.7, res_cv_depth=0.0)
        without = _res_render(self.x)
        assert np.array_equal(with_cv, without)

    # -- the law: Q doublings per unit ------------------------------------------

    @pytest.mark.parametrize("mode", FILTER_MODES)
    def test_cv_plus_one_at_depth_one_doubles_the_q(self, mode):
        # Bit-exact, not allclose: 3.0 * 2 ** 1.0 is 6.0 exactly, so
        # _filter_coeffs sees the identical Q either way.
        via_cv = _res_render(self.x, mode=mode, q=3.0, res_cv=1.0)
        static = _res_render(self.x, mode=mode, q=6.0)
        assert np.array_equal(via_cv, static)

    def test_cv_minus_one_halves_the_q(self):
        via_cv = _res_render(self.x, q=3.0, res_cv=-1.0)
        static = _res_render(self.x, q=1.5)
        assert np.array_equal(via_cv, static)

    def test_depth_two_makes_cv_plus_one_a_quadrupling(self):
        via_cv = _res_render(self.x, q=3.0, res_cv=1.0, res_cv_depth=2.0)
        static = _res_render(self.x, q=12.0)
        assert np.array_equal(via_cv, static)

    def test_default_depth_is_one(self):
        explicit = _res_render(self.x, q=3.0, res_cv=0.5, res_cv_depth=1.0)
        default = _res_render(self.x, q=3.0, res_cv=0.5)
        assert np.array_equal(explicit, default)

    # -- measured: the peak sharpens as the CV rises --------------------------------

    @pytest.mark.parametrize("mode", ["bandpass", "lowpass"])
    def test_rising_resonance_cv_sharpens_the_peak(self, mode):
        """White noise through a 1 kHz filter at Q 1: each +0.5 on
        resonance_cv (Q x sqrt 2) must raise the peak-to-skirt power
        ratio, and by close to the 2x that Q^2 predicts (measured 1.82x
        to 1.93x -- the +/-5 % peak band is wider than the -3 dB
        bandwidth at the high-Q end, so it under-reads a little)."""
        ratios = [
            _peak_to_skirt(_res_render(self.x, mode=mode, q=1.0, res_cv=cv))
            for cv in (0.0, 0.5, 1.0, 1.5, 2.0)
        ]
        for lo, hi in zip(ratios, ratios[1:]):
            assert hi > lo * 1.5, ratios
        # cv +1 (Q doubled) is at least 3x the peak-to-skirt of cv 0.
        assert ratios[2] > ratios[0] * 3.0, ratios

    # -- stability: the clamp ------------------------------------------------------

    def test_huge_cv_pins_the_q_at_the_top_rail(self):
        """+10 at depth 1 asks for Q x 1024; the filter's own clamp
        answers Q = 20, so the render IS the resonance-20 render and
        stays finite and bounded."""
        wild = _res_render(self.x, q=3.0, res_cv=10.0)
        rail = _res_render(self.x, q=20.0)
        assert np.all(np.isfinite(wild))
        assert np.max(np.abs(wild)) < 50.0
        assert np.array_equal(wild, rail)

    def test_huge_negative_cv_pins_the_q_at_the_bottom_rail(self):
        wild = _res_render(self.x, q=3.0, res_cv=-10.0)
        rail = _res_render(self.x, q=0.1)
        assert np.array_equal(wild, rail)

    def test_absurd_cv_does_not_overflow(self):
        # 2.0 ** 1e6 would raise OverflowError in Python; the exponent
        # is clipped before it gets there and the Q pins at 20.
        wild = _res_render(self.x, q=3.0, res_cv=1e6)
        assert np.all(np.isfinite(wild))
        assert np.array_equal(wild, _res_render(self.x, q=20.0))

    # -- voice awareness -------------------------------------------------------------

    def test_per_voice_resonance_cv_rows_match_mono(self):
        """A (V, F) resonance_cv gives every voice its own Q. Each row
        must be bit-identical to the mono filter at that row's CV --
        including the non-integer one, which is why the exponent is
        taken in float64 (a float32 2 ** 0.5 sat one ulp off)."""
        cvs = (0.0, 0.5, 1.0, 2.0)
        V = len(cvs)
        xv = np.stack([self.x] * V)
        cv = np.stack([np.full(self.x.shape, c, np.float32) for c in cvs])
        out = _res_render(xv, cutoff=800.0, q=2.0, res_cv=cv)
        assert out.shape == xv.shape
        for v, c in enumerate(cvs):
            mono = _res_render(self.x, cutoff=800.0, q=2.0, res_cv=c)
            assert np.array_equal(out[v], mono), (v, c)

    def test_macro_resonance_cv_is_one_q_for_every_voice(self):
        """An (F,) resonance_cv on a (V, F) signal: the shared-coefficient
        path, one Q broadcast across the voices, each row bit-identical
        to mono."""
        V = 3
        xv = np.stack([self.x] * V)
        p, b, osc, cut_lfo, res_lfo, filt = _res_patch(cutoff=800.0, q=2.0)
        outs = []
        for i in range(xv.shape[1] // F):
            bufs = {
                (osc.id, "out"): xv[:, i * F:(i + 1) * F],
                (res_lfo.id, "cv"): np.full(F, 1.0, np.float32),
            }
            outs.append(b._render_filter(filt, F, bufs, p))
        out = np.concatenate(outs, axis=1)
        mono = _res_render(self.x, cutoff=800.0, q=2.0, res_cv=1.0)
        for v in range(V):
            assert np.array_equal(out[v], mono)

    def test_per_voice_q_with_per_voice_cutoff(self):
        """Both CVs (V, F): voice 2 at cutoff +1 oct and Q +1 doubling
        must land on the static filter at (1600 Hz, Q 4). allclose, not
        bit-exact, and that is the CUTOFF side, not the Q: the per-voice
        cutoff path (pre-existing, pinned by
        TestFilterVoiceLfilterEquivalence against an oracle that does
        the same) takes its block mean in float32 and its cos/sin on a
        (V,) array, whose SIMD kernel can sit an ulp from the scalar
        libm call -- even voice 0 at cv 0 is 1e-6 off the mono render.
        The per-voice Q path alone is bit-exact (the test above)."""
        cvs = (0.0, 0.5, 1.0, 2.0)
        V = len(cvs)
        xv = np.stack([self.x] * V)
        cv = np.stack([np.full(self.x.shape, c, np.float32) for c in cvs])
        out = _res_render(xv, cutoff=800.0, q=2.0, res_cv=cv, cut_cv=cv)
        static = _res_render(self.x, cutoff=1600.0, q=4.0)
        assert np.allclose(out[2], static, rtol=1e-5, atol=1e-5)
        base = _res_render(self.x, cutoff=800.0, q=2.0)
        assert np.allclose(out[0], base, rtol=1e-5, atol=1e-5)

    # -- block-size independence ----------------------------------------------------

    def test_constant_cv_is_block_size_independent(self):
        # A constant CV has the same block mean at any block size, so the
        # coefficients -- and the raw-history state carry -- agree exactly.
        small = _res_render(self.x, mode="bandpass", q=2.0, res_cv=0.75, block=64)
        big = _res_render(self.x, mode="bandpass", q=2.0, res_cv=0.75, block=512)
        assert np.array_equal(small, big)


# ----- UI -----------------------------------------------------------------------


def _filter_widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("filter")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("format"))
    return out


def test_every_filter_param_gets_a_bounded_widget(monkeypatch):
    w = _filter_widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("filter").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["mode"][0] == "add_combo"
    assert "oct/unit" in w["cv_depth"][1]
    assert "dbl/unit" in w["res_cv_depth"][1]


# ----- example -------------------------------------------------------------------


def test_the_resonance_sweep_example_breathes():
    """examples/filter_resonance_sweep.json: a saw through a lowpass, a
    0.5 Hz LFO on cutoff_cv and a 0.11 Hz triangle on resonance_cv. The
    cutoff LFO covers its whole range inside every second, so the
    per-second peak of the filter's output only moves because the Q
    does: with the sweep the peaks spread 2.1x across the resonance
    LFO's 9 s cycle, with res_cv_depth 0 they sit within 1.09x."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "filter_resonance_sweep.json"

    def run(res_cv_depth=None):
        patch = load_patch(path)
        assert len(patch.modules) <= 10
        filt = next(m for m in patch if m.TYPE == "filter")
        if res_cv_depth is not None:
            filt.set_param("res_cv_depth", res_cv_depth)
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        cap = []
        orig = b._render_filter

        def spy(module, frames, buffers, p):
            r = orig(module, frames, buffers, p)
            cap.append(np.asarray(r).copy())
            return r

        b._render_filter = spy
        peak = 0.0
        for _ in range(int(SR * 12 / 512)):
            out, _devices = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        sig = np.concatenate(cap)
        per_second = [float(np.abs(sig[s * SR:(s + 1) * SR]).max()) for s in range(12)]
        return peak, max(per_second) / min(per_second)

    peak, spread = run()
    assert 0.3 < peak < 0.8, peak
    assert spread > 1.6, spread
    _peak_flat, spread_flat = run(res_cv_depth=0.0)
    assert spread_flat < 1.2, spread_flat
