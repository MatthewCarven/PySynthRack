"""Tests for the Distortion module (4x-oversampled saturation).

Coverage:
  - Model: registration/defaults, ports & signal kinds (in/drive_cv ->
    out), JSON round-trip, unknown param rejected, type walls.
  - Bypass & identity: disconnected input -> silence; mix=0 is a
    bit-exact passthrough; near-zero drive is the identity (modulo the
    16-sample oversampler latency and FIR passband ripple).
  - Curves: soft produces odd harmonics only; hard clips at the rails
    with a flat top; tube adds even harmonics with the DC blocked;
    unknown mode falls back to soft.
  - Controls: tone darkens (and 20 kHz bypasses); level scales the wet
    path linearly; mix blends the delay-compensated dry with the wet.
  - CV: a constant drive_cv equals the equivalent static drive;
    cv_depth = 0 disables the input.
  - Aliasing: a hard-clipped 6 kHz sine's folded 5th harmonic sits far
    below the legitimate 3rd (the 4x oversampling doing its job).
  - Invariants: block-size independent (bit-identical 512/4096/333);
    a single voice row is bit-identical to mono; voices are
    independent; (V, F) shape is preserved; extremes stay finite.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend, _OS_LATENCY
from pysynthrack.core import Patch
from pysynthrack.modules.distortion import DISTORTION_MODES, Distortion

SR, F = 44100, 512
LAT = _OS_LATENCY  # 16 base-rate samples through the 4x pair


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(**params):
    patch = Patch()
    src = patch.add_module("oscillator")
    d = patch.add_module("distortion", params=params)
    patch.connect(src.id, "out", d.id, "in")
    b = _backend()
    b.compile(patch)
    return patch, src, d, b


def _cv_rig(**params):
    patch = Patch()
    src = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    d = patch.add_module("distortion", params=params)
    patch.connect(src.id, "out", d.id, "in")
    patch.connect(lfo.id, "cv", d.id, "drive_cv")
    b = _backend()
    b.compile(patch)
    return patch, src, lfo, d, b


def _run(b, patch, src, d, signal, cv=None, lfo=None, block=F):
    """Render ``signal`` through the distortion in ``block`` chunks."""
    outs = []
    n = signal.shape[-1]
    for i in range(0, n, block):
        blk = signal[..., i:i + block]
        bufs = {(src.id, "out"): blk}
        if cv is not None:
            bufs[(lfo.id, "cv")] = cv[..., i:i + block]
        outs.append(b._render_distortion(d, blk.shape[-1], bufs, patch))
    return np.concatenate([o["out"] for o in outs], axis=-1)


def _sine(amp=0.8, freq=441.0, n=F * 40):
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
        d = patch.add_module("distortion")
        assert isinstance(d, Distortion)
        assert d.params == {
            "drive": 4.0, "mode": "soft", "tone": 20000.0,
            "level": 1.0, "mix": 1.0, "cv_depth": 5.0,
        }
        assert DISTORTION_MODES == ("soft", "hard", "tube")

    def test_ports_and_signal_kinds(self):
        d = Patch().add_module("distortion")
        assert [(p.name, p.signal_kind) for p in d.input_ports] == [
            ("in", "audio"), ("drive_cv", "cv")
        ]
        assert [(p.name, p.signal_kind) for p in d.output_ports] == [
            ("out", "audio")
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("distortion", params={"drive": 9.0, "mode": "tube"})
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == "distortion")
        assert mod.params["drive"] == 9.0
        assert mod.params["mode"] == "tube"

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("distortion", params={"gain": 2.0})

    def test_audio_wall(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        d = patch.add_module("distortion")
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", d.id, "in")  # cv -> audio in

    def test_cv_port_accepts_cv(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        d = patch.add_module("distortion")
        patch.connect(lfo.id, "cv", d.id, "drive_cv")  # no raise


# ----- Bypass & identity ------------------------------------------------------


class TestBypass:
    def test_disconnected_is_silence(self):
        patch = Patch()
        d = patch.add_module("distortion")
        b = _backend()
        b.compile(patch)
        out = b._render_distortion(d, F, {}, patch)["out"]
        assert out.shape == (F,)
        assert not out.any()

    def test_mix_zero_is_bit_exact(self):
        patch, src, d, b = _rig(mix=0.0, drive=20.0)
        blk = _sine(n=F)
        out = b._render_distortion(d, F, {(src.id, "out"): blk}, patch)["out"]
        assert np.array_equal(out, blk)

    def test_tiny_drive_is_identity_delayed(self):
        patch, src, d, b = _rig(drive=0.01)
        sig = _sine()
        out = _run(b, patch, src, d, sig)
        err = np.max(np.abs(out[F + LAT:] - sig[F:-LAT]))
        assert err < 0.02


# ----- Curves ----------------------------------------------------------------


class TestCurves:
    def test_soft_odd_harmonics_only(self):
        patch, src, d, b = _rig(drive=6.0, mode="soft")
        out = _run(b, patch, src, d, _sine())
        X = _spectrum(out)
        k = _bin(441.0)
        assert X[3 * k] / X[k] > 0.1        # strong 3rd
        assert X[2 * k] / X[k] < 1e-3       # essentially no 2nd

    def test_soft_bounded(self):
        patch, src, d, b = _rig(drive=30.0, mode="soft")
        out = _run(b, patch, src, d, _sine(amp=0.95))
        assert np.max(np.abs(out)) < 1.1    # rails + filter ringing margin

    def test_hard_clips_flat(self):
        patch, src, d, b = _rig(drive=10.0, mode="hard")
        out = _run(b, patch, src, d, _sine())
        tail = out[-4096:]
        assert np.max(np.abs(tail)) < 1.1
        # a heavily clipped sine spends most of its time pinned near the rails
        assert np.mean(np.abs(tail) > 0.9) > 0.5

    def test_tube_even_harmonics_dc_blocked(self):
        patch, src, d, b = _rig(drive=6.0, mode="tube")
        out = _run(b, patch, src, d, _sine())
        X = _spectrum(out)
        k = _bin(441.0)
        assert X[2 * k] / X[k] > 0.01       # even content present
        assert abs(np.mean(out[-8192:])) < 0.01  # DC removed

    def test_soft_and_hard_differ(self):
        patch, src, d, b = _rig(drive=6.0, mode="soft")
        patch2, src2, d2, b2 = _rig(drive=6.0, mode="hard")
        sig = _sine()
        assert np.max(np.abs(
            _run(b, patch, src, d, sig) - _run(b2, patch2, src2, d2, sig)
        )) > 0.01

    def test_unknown_mode_falls_back_to_soft(self):
        patch, src, d, b = _rig(drive=6.0, mode="banana")
        patch2, src2, d2, b2 = _rig(drive=6.0, mode="soft")
        sig = _sine()
        assert np.array_equal(
            _run(b, patch, src, d, sig), _run(b2, patch2, src2, d2, sig)
        )


# ----- Controls --------------------------------------------------------------


class TestControls:
    def test_tone_darkens(self):
        sig = _sine()
        patch, src, d, b = _rig(drive=8.0, tone=500.0)
        patch2, src2, d2, b2 = _rig(drive=8.0)  # tone 20k = bypass
        dark = _spectrum(_run(b, patch, src, d, sig))
        bright = _spectrum(_run(b2, patch2, src2, d2, sig))
        k3 = 3 * _bin(441.0)
        # the 3rd harmonic (1323 Hz) sits above a 500 Hz tone cutoff
        assert dark[k3] < bright[k3] * 0.6

    def test_tone_open_is_out_of_circuit(self):
        sig = _sine()
        patch, src, d, b = _rig(drive=8.0, tone=20000.0)
        patch2, src2, d2, b2 = _rig(drive=8.0, tone=19000.0)
        open_out = _run(b, patch, src, d, sig)
        near = _run(b2, patch2, src2, d2, sig)
        assert not np.array_equal(open_out, near)  # 19 kHz still filters

    def test_level_scales_wet(self):
        sig = _sine()
        patch, src, d, b = _rig(drive=6.0, level=1.0)
        patch2, src2, d2, b2 = _rig(drive=6.0, level=0.5)
        full = _run(b, patch, src, d, sig)
        half = _run(b2, patch2, src2, d2, sig)
        assert np.allclose(half, 0.5 * full, atol=1e-6)

    def test_mix_blends_compensated_dry(self):
        sig = _sine()
        patch, src, d, b = _rig(drive=8.0, mix=1.0)
        patch2, src2, d2, b2 = _rig(drive=8.0, mix=0.5)
        wet = _run(b, patch, src, d, sig)
        blended = _run(b2, patch2, src2, d2, sig)
        dry_delayed = np.concatenate([np.zeros(LAT, np.float32), sig[:-LAT]])
        expect = 0.5 * dry_delayed + 0.5 * wet
        assert np.allclose(blended, expect, atol=1e-5)


# ----- CV --------------------------------------------------------------------


class TestCV:
    def test_constant_cv_equals_static_drive(self):
        sig = _sine()
        cv = np.full(sig.shape, 1.0, dtype=np.float32)
        patch, src, lfo, d, b = _cv_rig(drive=4.0, cv_depth=2.0)
        via_cv = _run(b, patch, src, d, sig, cv=cv, lfo=lfo)
        patch2, src2, d2, b2 = _rig(drive=6.0)
        static = _run(b2, patch2, src2, d2, sig)
        assert np.allclose(via_cv, static, atol=1e-6)

    def test_cv_depth_zero_disables(self):
        sig = _sine()
        cv = np.full(sig.shape, 1.0, dtype=np.float32)
        patch, src, lfo, d, b = _cv_rig(drive=4.0, cv_depth=0.0)
        with_cv = _run(b, patch, src, d, sig, cv=cv, lfo=lfo)
        patch2, src2, d2, b2 = _rig(drive=4.0)
        without = _run(b2, patch2, src2, d2, sig)
        assert np.array_equal(with_cv, without)

    def test_cv_actually_changes_output(self):
        sig = _sine()
        cv = np.full(sig.shape, 1.0, dtype=np.float32)
        patch, src, lfo, d, b = _cv_rig(drive=2.0, cv_depth=10.0)
        with_cv = _run(b, patch, src, d, sig, cv=cv, lfo=lfo)
        patch2, src2, d2, b2 = _rig(drive=2.0)
        without = _run(b2, patch2, src2, d2, sig)
        assert np.max(np.abs(with_cv - without)) > 0.01


# ----- Aliasing --------------------------------------------------------------


class TestAliasing:
    def test_folded_fifth_harmonic_suppressed(self):
        # f0 on an exact FFT bin, chosen so 5*f0 > Nyquist would alias
        # onto a bin that is NOT a harmonic of f0.
        L, k0 = 8192, 1115
        f0 = SR * k0 / L
        sig = _sine(amp=0.9, freq=f0)
        patch, src, d, b = _rig(drive=10.0, mode="hard")
        out = _run(b, patch, src, d, sig)[-L:]
        X = np.abs(np.fft.rfft(out))
        h3 = X[3 * k0]              # legitimate 3rd harmonic, in band
        alias5 = X[L - 5 * k0]      # where the folded 5th would land
        assert alias5 < h3 * 0.02   # > 34 dB down


# ----- Invariants ------------------------------------------------------------


class TestInvariants:
    def test_block_size_independent(self):
        sig = _sine(n=F * 24)
        outs = []
        for block in (512, 4096, 333):
            patch, src, d, b = _rig(drive=7.0, mode="tube", tone=3000.0)
            outs.append(_run(b, patch, src, d, sig[:F * 24 // 333 * 333]
                             if block == 333 else sig, block=block))
        n = min(o.shape[-1] for o in outs)
        assert np.array_equal(outs[0][:n], outs[1][:n])
        assert np.array_equal(outs[0][:n], outs[2][:n])

    def test_voice_row_bit_identical_to_mono(self):
        sig = _sine(n=F * 8)
        patch, src, d, b = _rig(drive=7.0)
        mono = _run(b, patch, src, d, sig)
        patch2, src2, d2, b2 = _rig(drive=7.0)
        voiced = _run(b2, patch2, src2, d2, sig[np.newaxis, :])
        assert voiced.shape == (1, sig.shape[-1])
        assert np.array_equal(voiced[0], mono)

    def test_voices_independent(self):
        a = _sine(amp=0.8, freq=441.0, n=F * 8)
        c = _sine(amp=0.3, freq=333.0, n=F * 8)
        both = np.stack([a, c])
        patch, src, d, b = _rig(drive=7.0)
        stereo = _run(b, patch, src, d, both)
        patch2, src2, d2, b2 = _rig(drive=7.0)
        solo = _run(b2, patch2, src2, d2, a)
        assert stereo.shape == both.shape
        assert np.array_equal(stereo[0], solo)

    def test_extremes_stay_finite(self):
        patch, src, d, b = _rig(drive=30.0, mode="tube", level=2.0, tone=200.0)
        out = _run(b, patch, src, d, _sine(amp=0.99))
        assert np.all(np.isfinite(out))
