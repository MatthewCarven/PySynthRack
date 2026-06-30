"""Tests for the Noise generator (white / pink, audio + cv source).

Coverage:
  - Model: registration, defaults, ports/signal kinds (no inputs; dual
    `out` audio + `cv` cv), JSON round-trip, unknown param rejected,
    type walls (audio out→audio sink legal, cv out→cv input legal,
    audio out→cv input illegal, cv out→audio sink illegal).
  - White: mono shape/dtype, hard-bounded to ±amp, ~zero mean, roughly
    flat spectrum, amp scaling, both jacks are the same stream.
  - Pink: mono shape, steep low-vs-high spectral tilt (≈ −3 dB/oct),
    filter state (zi) carried across blocks, switching back to white
    drops the stale pink state, RMS ≈ white.
  - Randomness: consecutive blocks differ; two modules draw independent
    streams.
  - Integration: white→filter→speaker renders audible audio; noise.cv→
    SampleHold (clocked) yields a bounded random staircase; noise.cv→
    CVToAudio→speaker (the audio-via-bridge path) renders.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import welch

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.noise import NOISE_COLORS, Noise


def _backend():
    return NumpyBackend(sample_rate=44100, block_size=512)


def _render(color="white", amp=1.0, frames=512, seed=0):
    np.random.seed(seed)
    patch = Patch()
    nz = patch.add_module("noise", params={"color": color, "amp": amp})
    backend = _backend()
    backend.compile(patch)
    return backend._render_noise(nz, frames, {}, patch)


def _bandpow(sig, sr, lo, hi):
    f, P = welch(sig, fs=sr, nperseg=4096)
    m = (f >= lo) & (f < hi)
    return float(P[m].mean())


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        nz = patch.add_module("noise")
        assert isinstance(nz, Noise)
        assert nz.params == {"color": "white", "amp": 1.0}

    def test_colors_constant(self):
        assert NOISE_COLORS == ("white", "pink")

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        nz = patch.add_module("noise")
        assert nz.input_ports == []
        assert [(p.name, p.signal_kind) for p in nz.output_ports] == [
            ("out", "audio"),
            ("cv", "cv"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("noise", params={"color": "pink", "amp": 0.3})
        restored = Patch.from_dict(patch.to_dict())
        nz = next(m for m in restored if m.TYPE == "noise")
        assert nz.params["color"] == "pink"
        assert nz.params["amp"] == 0.3

    def test_unknown_param_rejected(self):
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("noise", params={"seed": 42})

    def test_audio_out_into_audio_sink_accepted(self):
        patch = Patch()
        nz = patch.add_module("noise")
        spk = patch.add_module("speaker_output")
        patch.connect(nz.id, "out", spk.id, "in")  # audio → audio

    def test_cv_out_into_cv_input_accepted(self):
        patch = Patch()
        nz = patch.add_module("noise")
        sh = patch.add_module("sample_hold")
        patch.connect(nz.id, "cv", sh.id, "in")  # cv → cv

    def test_audio_out_into_cv_input_rejected(self):
        patch = Patch()
        nz = patch.add_module("noise")
        sh = patch.add_module("sample_hold")
        with pytest.raises(ValueError):
            patch.connect(nz.id, "out", sh.id, "in")  # audio → cv

    def test_cv_out_into_audio_sink_rejected(self):
        patch = Patch()
        nz = patch.add_module("noise")
        spk = patch.add_module("speaker_output")
        with pytest.raises(ValueError):
            patch.connect(nz.id, "cv", spk.id, "in")  # cv → audio


# ----- White -----------------------------------------------------------------


class TestWhite:
    def test_shape_and_dtype(self):
        r = _render("white", frames=256)
        assert r["out"].shape == (256,)
        assert r["out"].dtype == np.float32

    def test_both_jacks_same_stream(self):
        r = _render("white")
        assert r["out"] is r["cv"]
        assert np.array_equal(r["out"], r["cv"])

    def test_hard_bounded_to_amp(self):
        r = _render("white", amp=1.0, frames=8192)
        assert r["out"].min() >= -1.0 and r["out"].max() <= 1.0
        r2 = _render("white", amp=0.25, frames=8192)
        assert np.abs(r2["out"]).max() <= 0.25

    def test_zero_mean(self):
        r = _render("white", frames=44100)
        assert abs(float(r["out"].mean())) < 0.02

    def test_roughly_flat_spectrum(self):
        sig = _render("white", frames=44100, seed=1)["out"]
        ratio = _bandpow(sig, 44100, 50, 500) / _bandpow(sig, 44100, 5000, 15000)
        assert 0.5 < ratio < 2.0  # flat-ish: low ≈ high

    def test_amp_scales_rms(self):
        full = _render("white", amp=1.0, frames=20000, seed=2)["out"].std()
        half = _render("white", amp=0.5, frames=20000, seed=2)["out"].std()
        assert abs(half / full - 0.5) < 0.02


# ----- Pink ------------------------------------------------------------------


class TestPink:
    def test_shape_mono(self):
        r = _render("pink", frames=512)
        assert r["out"].shape == (512,)
        assert r["out"] is r["cv"]

    def test_low_frequency_tilt(self):
        sig = _render("pink", frames=44100, seed=3)["out"]
        ratio = _bandpow(sig, 44100, 50, 500) / _bandpow(sig, 44100, 5000, 15000)
        assert ratio > 5.0  # markedly more low-frequency energy than white

    def test_slope_near_minus_3_db_per_octave(self):
        import math
        sig = _render("pink", frames=44100 * 4, seed=4)["out"]
        f, P = welch(sig, fs=44100, nperseg=8192)

        def at(c):
            m = (f >= c / 1.2) & (f < c * 1.2)
            return 10 * math.log10(P[m].mean())

        slope = (at(10000) - at(100)) / math.log2(10000 / 100)
        assert -4.0 < slope < -2.0  # ≈ -3 dB/oct

    def test_zi_carried_across_blocks(self):
        np.random.seed(5)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "pink"})
        backend = _backend()
        backend.compile(patch)
        backend._render_noise(nz, 512, {}, patch)
        zi1 = backend._state[nz.id]["pink_zi"].copy()
        backend._render_noise(nz, 512, {}, patch)
        zi2 = backend._state[nz.id]["pink_zi"]
        assert not np.allclose(zi1, 0.0)      # state accrued
        assert not np.allclose(zi1, zi2)      # and evolves block to block

    def test_switch_back_to_white_drops_pink_state(self):
        np.random.seed(6)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "pink"})
        backend = _backend()
        backend.compile(patch)
        backend._render_noise(nz, 512, {}, patch)
        assert "pink_zi" in backend._state[nz.id]
        nz.set_param("color", "white")
        backend._render_noise(nz, 512, {}, patch)
        assert "pink_zi" not in backend._state[nz.id]

    def test_rms_matches_white(self):
        w = _render("white", frames=44100, seed=7)["out"].std()
        p = _render("pink", frames=44100, seed=7)["out"].std()
        assert 0.7 < p / w < 1.4  # RMS-normalised to roughly white's level


# ----- Randomness ------------------------------------------------------------


class TestRandomness:
    def test_consecutive_blocks_differ(self):
        np.random.seed(8)
        patch = Patch()
        nz = patch.add_module("noise")
        backend = _backend()
        backend.compile(patch)
        a = backend._render_noise(nz, 512, {}, patch)["out"].copy()
        b = backend._render_noise(nz, 512, {}, patch)["out"].copy()
        assert not np.array_equal(a, b)

    def test_two_modules_independent(self):
        np.random.seed(9)
        patch = Patch()
        n1 = patch.add_module("noise")
        n2 = patch.add_module("noise")
        backend = _backend()
        backend.compile(patch)
        a = backend._render_noise(n1, 512, {}, patch)["out"]
        b = backend._render_noise(n2, 512, {}, patch)["out"]
        assert not np.array_equal(a, b)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_white_through_filter_to_speaker(self):
        np.random.seed(10)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "white", "amp": 0.8})
        filt = patch.add_module("filter", params={"mode": "lowpass", "cutoff": 1200.0, "resonance": 3.0})
        spk = patch.add_module("speaker_output", params={"gain": 0.8})
        patch.connect(nz.id, "out", filt.id, "in")
        patch.connect(filt.id, "out", spk.id, "in")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        peak = 0.0
        for _ in range(10):
            blk = backend.render_block(512)
            assert blk.shape == (512, 2)
            assert np.isfinite(blk).all()
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0

    def test_noise_cv_into_samplehold_is_bounded_staircase(self):
        """noise.cv → S&H clocked by an LFO→Schmitt: held random steps."""
        np.random.seed(11)
        sr = 44100
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "white"})
        clk = patch.add_module("lfo", params={"waveform": "square", "rate": 8.0, "depth": 1.0, "bipolar": False})
        sch = patch.add_module("schmitt")
        sh = patch.add_module("sample_hold")
        patch.connect(nz.id, "cv", sh.id, "in")
        patch.connect(clk.id, "cv", sch.id, "in")
        patch.connect(sch.id, "gate", sh.id, "trig")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        held = []
        for _ in range(sr // 512):  # ~1 s
            buffers = {}
            for mid in backend._topo_order:
                m = patch.modules.get(mid)
                if m is None:
                    continue
                res = backend._render_module(m, 512, buffers, patch)
                if res is None:
                    continue
                if isinstance(res, dict):
                    for port, buf in res.items():
                        buffers[(mid, port)] = buf
                elif m.OUTPUT_PORTS:
                    buffers[(mid, m.OUTPUT_PORTS[0].name)] = res
            held.append(buffers[(sh.id, "out")])
        out = np.concatenate(held)
        assert np.isfinite(out).all()
        assert out.min() >= -1.0 and out.max() <= 1.0     # white-bounded samples
        assert np.mean(np.diff(out) == 0) > 0.95          # piecewise-constant
        assert len(np.unique(out)) > 3                    # actually stepping

    def test_noise_cv_through_cvtoaudio_bridge(self):
        np.random.seed(12)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "pink", "amp": 0.5})
        bridge = patch.add_module("cv_to_audio", params={"gain": 1.0})
        spk = patch.add_module("speaker_output", params={"gain": 0.8})
        patch.connect(nz.id, "cv", bridge.id, "cv")
        patch.connect(bridge.id, "out", spk.id, "in")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        blk = backend.render_block(512)
        assert blk.shape == (512, 2)
        assert np.isfinite(blk).all()
        assert np.abs(blk).max() > 0.0
