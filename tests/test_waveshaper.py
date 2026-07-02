"""Tests for the Waveshaper module (4x-oversampled wavefolder).

Coverage:
  - Model: registration/defaults, ports & signal kinds (in/fold_cv ->
    out), JSON round-trip, unknown param rejected, type walls.
  - Bypass & identity: disconnected -> silence; mix=0 bit-exact;
    triangle fold=1 passes a full-scale signal unchanged (modulo the
    16-sample latency and FIR ripple); fold=0 collapses to silence.
  - Folding: fold=6 on a sine produces harmonics rivalling the
    fundamental while staying bounded; triangle and sine modes differ;
    the sine curve matches sin(pi/2*u) at low fold; unknown mode falls
    back to triangle.
  - Symmetry: off-centre folding adds even harmonics with the DC
    blocked; centred folding has none.
  - CV: constant fold_cv equals the equivalent static fold;
    cv_depth = 0 disables; a swept fold changes the output.
  - Invariants: block-size independent (bit-identical 512/4096/333);
    voice row bit-identical to mono; voices independent; shape
    preserved; extremes finite.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend, _OS_LATENCY
from pysynthrack.core import Patch
from pysynthrack.modules.waveshaper import WAVESHAPER_MODES, Waveshaper

SR, F = 44100, 512
LAT = _OS_LATENCY


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(**params):
    patch = Patch()
    src = patch.add_module("oscillator")
    w = patch.add_module("waveshaper", params=params)
    patch.connect(src.id, "out", w.id, "in")
    b = _backend()
    b.compile(patch)
    return patch, src, w, b


def _cv_rig(**params):
    patch = Patch()
    src = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    w = patch.add_module("waveshaper", params=params)
    patch.connect(src.id, "out", w.id, "in")
    patch.connect(lfo.id, "cv", w.id, "fold_cv")
    b = _backend()
    b.compile(patch)
    return patch, src, lfo, w, b


def _run(b, patch, src, w, signal, cv=None, lfo=None, block=F):
    outs = []
    n = signal.shape[-1]
    for i in range(0, n, block):
        blk = signal[..., i:i + block]
        bufs = {(src.id, "out"): blk}
        if cv is not None:
            bufs[(lfo.id, "cv")] = cv[..., i:i + block]
        outs.append(b._render_waveshaper(w, blk.shape[-1], bufs, patch))
    return np.concatenate([o["out"] for o in outs], axis=-1)


def _sine(amp=0.8, freq=220.5, n=F * 40):
    t = np.arange(n) / SR
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _spectrum(x, n=8192):
    seg = x[-n:]
    return np.abs(np.fft.rfft(seg * np.hanning(n)))


def _bin(freq, n=8192):
    return round(freq * n / SR)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        w = patch.add_module("waveshaper")
        assert isinstance(w, Waveshaper)
        assert w.params == {
            "fold": 1.0, "symmetry": 0.0, "mode": "triangle",
            "mix": 1.0, "cv_depth": 4.0,
        }
        assert WAVESHAPER_MODES == ("triangle", "sine")

    def test_ports_and_signal_kinds(self):
        w = Patch().add_module("waveshaper")
        assert [(p.name, p.signal_kind) for p in w.input_ports] == [
            ("in", "audio"), ("fold_cv", "cv")
        ]
        assert [(p.name, p.signal_kind) for p in w.output_ports] == [
            ("out", "audio")
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("waveshaper", params={"fold": 5.5, "mode": "sine"})
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == "waveshaper")
        assert mod.params["fold"] == 5.5
        assert mod.params["mode"] == "sine"

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("waveshaper", params={"drive": 2.0})

    def test_audio_wall(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        w = patch.add_module("waveshaper")
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", w.id, "in")

    def test_cv_port_accepts_cv(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        w = patch.add_module("waveshaper")
        patch.connect(lfo.id, "cv", w.id, "fold_cv")  # no raise


# ----- Bypass & identity ------------------------------------------------------


class TestBypass:
    def test_disconnected_is_silence(self):
        patch = Patch()
        w = patch.add_module("waveshaper")
        b = _backend()
        b.compile(patch)
        out = b._render_waveshaper(w, F, {}, patch)["out"]
        assert out.shape == (F,)
        assert not out.any()

    def test_mix_zero_is_bit_exact(self):
        patch, src, w, b = _rig(mix=0.0, fold=8.0)
        blk = _sine(n=F)
        out = b._render_waveshaper(w, F, {(src.id, "out"): blk}, patch)["out"]
        assert np.array_equal(out, blk)

    def test_triangle_fold_one_is_identity_delayed(self):
        patch, src, w, b = _rig(fold=1.0)
        sig = _sine(amp=0.9)
        out = _run(b, patch, src, w, sig)
        err = np.max(np.abs(out[F + LAT:] - sig[F:-LAT]))
        assert err < 5e-3

    def test_fold_zero_is_silence(self):
        patch, src, w, b = _rig(fold=0.0)
        out = _run(b, patch, src, w, _sine(n=F * 8))
        assert np.max(np.abs(out[F:])) < 1e-6


# ----- Folding ---------------------------------------------------------------


class TestFolding:
    def test_fold_six_is_rich_and_bounded(self):
        patch, src, w, b = _rig(fold=6.0)
        out = _run(b, patch, src, w, _sine())
        X = _spectrum(out)
        k = _bin(220.5)
        assert X[3 * k] > X[k] * 0.5     # upper partials rival the fundamental
        assert X[5 * k] > X[k] * 0.5
        assert np.max(np.abs(out)) < 1.15

    def test_modes_differ(self):
        sig = _sine()
        patch, src, w, b = _rig(fold=6.0, mode="triangle")
        patch2, src2, w2, b2 = _rig(fold=6.0, mode="sine")
        assert np.max(np.abs(
            _run(b, patch, src, w, sig) - _run(b2, patch2, src2, w2, sig)
        )) > 0.05

    def test_sine_curve_matches_formula_at_low_fold(self):
        patch, src, w, b = _rig(fold=1.2, mode="sine")
        sig = _sine(amp=0.7)
        out = _run(b, patch, src, w, sig)
        expect = np.sin(np.pi / 2.0 * 1.2 * sig[F:-LAT].astype(np.float64))
        err = np.max(np.abs(out[F + LAT:] - expect))
        assert err < 5e-3

    def test_unknown_mode_falls_back_to_triangle(self):
        sig = _sine()
        patch, src, w, b = _rig(fold=6.0, mode="banana")
        patch2, src2, w2, b2 = _rig(fold=6.0, mode="triangle")
        assert np.array_equal(
            _run(b, patch, src, w, sig), _run(b2, patch2, src2, w2, sig)
        )


# ----- Symmetry --------------------------------------------------------------


class TestSymmetry:
    def test_symmetry_adds_even_harmonics_dc_blocked(self):
        patch, src, w, b = _rig(fold=4.0, symmetry=0.4)
        out = _run(b, patch, src, w, _sine())
        X = _spectrum(out)
        k = _bin(220.5)
        assert X[2 * k] / X[k] > 0.01
        assert abs(np.mean(out[-8192:])) < 0.01

    def test_centred_fold_has_no_even_harmonics(self):
        patch, src, w, b = _rig(fold=4.0, symmetry=0.0)
        out = _run(b, patch, src, w, _sine())
        X = _spectrum(out)
        k = _bin(220.5)
        assert X[2 * k] / X[k] < 1e-3


# ----- CV --------------------------------------------------------------------


class TestCV:
    def test_constant_cv_equals_static_fold(self):
        sig = _sine()
        cv = np.full(sig.shape, 1.0, dtype=np.float32)
        patch, src, lfo, w, b = _cv_rig(fold=2.0, cv_depth=3.0)
        via_cv = _run(b, patch, src, w, sig, cv=cv, lfo=lfo)
        patch2, src2, w2, b2 = _rig(fold=5.0)
        static = _run(b2, patch2, src2, w2, sig)
        assert np.allclose(via_cv, static, atol=1e-6)

    def test_cv_depth_zero_disables(self):
        sig = _sine()
        cv = np.full(sig.shape, 1.0, dtype=np.float32)
        patch, src, lfo, w, b = _cv_rig(fold=2.0, cv_depth=0.0)
        with_cv = _run(b, patch, src, w, sig, cv=cv, lfo=lfo)
        patch2, src2, w2, b2 = _rig(fold=2.0)
        without = _run(b2, patch2, src2, w2, sig)
        assert np.array_equal(with_cv, without)

    def test_swept_fold_changes_timbre(self):
        # fold_cv held at 0 (pure tone) for the first half, then ramped
        # up: the 3rd harmonic must bloom between the two halves.
        sig = _sine()
        half = sig.size // 2
        ramp = np.concatenate([
            np.zeros(half, dtype=np.float32),
            np.linspace(0.0, 1.0, sig.size - half, dtype=np.float32),
        ])
        patch, src, lfo, w, b = _cv_rig(fold=1.0, cv_depth=6.0)
        out = _run(b, patch, src, w, sig, cv=ramp, lfo=lfo)
        early = _spectrum(out[:half])
        late = _spectrum(out)
        k = _bin(220.5)
        assert late[3 * k] > early[3 * k] * 5.0


# ----- Invariants ------------------------------------------------------------


class TestInvariants:
    def test_block_size_independent(self):
        sig = _sine(n=F * 24)
        outs = []
        for block in (512, 4096, 333):
            patch, src, w, b = _rig(fold=5.0, symmetry=0.2)
            outs.append(_run(b, patch, src, w, sig[:F * 24 // 333 * 333]
                             if block == 333 else sig, block=block))
        n = min(o.shape[-1] for o in outs)
        assert np.array_equal(outs[0][:n], outs[1][:n])
        assert np.array_equal(outs[0][:n], outs[2][:n])

    def test_voice_row_bit_identical_to_mono(self):
        sig = _sine(n=F * 8)
        patch, src, w, b = _rig(fold=5.0)
        mono = _run(b, patch, src, w, sig)
        patch2, src2, w2, b2 = _rig(fold=5.0)
        voiced = _run(b2, patch2, src2, w2, sig[np.newaxis, :])
        assert voiced.shape == (1, sig.shape[-1])
        assert np.array_equal(voiced[0], mono)

    def test_voices_independent(self):
        a = _sine(amp=0.8, freq=220.5, n=F * 8)
        c = _sine(amp=0.3, freq=333.0, n=F * 8)
        both = np.stack([a, c])
        patch, src, w, b = _rig(fold=5.0)
        stereo = _run(b, patch, src, w, both)
        patch2, src2, w2, b2 = _rig(fold=5.0)
        solo = _run(b2, patch2, src2, w2, a)
        assert stereo.shape == both.shape
        assert np.array_equal(stereo[0], solo)

    def test_extremes_stay_finite(self):
        patch, src, w, b = _rig(fold=16.0, symmetry=-1.0, mode="sine")
        out = _run(b, patch, src, w, _sine(amp=0.99))
        assert np.all(np.isfinite(out))
