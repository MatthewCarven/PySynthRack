"""Tests for the Phaser (swept allpass-notch, bipolar-feedback).

Coverage:
  - Model: registration, defaults, ports/kinds (audio ``in`` + cv
    ``rate_cv`` -> ``out_l`` / ``out_r`` audio), JSON round-trip,
    unknown-param rejection, and the signal-kind type walls.
  - DSP: disconnected -> silence; ``mix=0`` is a bit-exact dry
    passthrough on both channels *even with strong feedback*; a fixed
    tone is amplitude-modulated as the notch sweeps through it (a moving
    notch); ``depth=0`` is a static notch pattern (not dry) that differs
    from a swept ``depth>0`` render; output stays finite/bounded at
    extreme settings; a voice (2D) input is summed to mono, and a
    single-voice input is bit-identical to mono.
  - Block independence: the per-sample allpass + feedback engine carries
    its LFO phase, allpass state and feedback memory across blocks, so the
    output is bit-identical at any block size (512 vs 4096 vs an odd size).
  - Feedback: bipolar -- positive and negative differ; more feedback rings
    longer; the loop stays bounded at the +/-0.95 clamp.
  - Stages: 4/6/8 change the sound and the number of spectral notches;
    out-of-range / string values snap to a legal count.
  - Stereo: the two channels are decorrelated (quadrature LFO).
  - CV: ``rate_cv`` alters the sweep; an all-zero ``rate_cv`` is a noop.
  - Integration: osc -> phaser -> L/R speakers renders audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.phaser import Phaser

SR = 44100
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    ph = patch.add_module("phaser", params=params or {})
    patch.connect(src.id, "out", ph.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, ph, b


def _run(patch, src, ph, b, signal, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        o = b._render_phaser(ph, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _rig_cv(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    ph = patch.add_module("phaser", params=params or {})
    patch.connect(src.id, "out", ph.id, "in")
    patch.connect(lfo.id, "cv", ph.id, "rate_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, lfo, ph, b


def _run_cv(patch, src, lfo, ph, b, signal, cv, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {
            (src.id, "out"): signal[..., sl].astype(np.float32),
            (lfo.id, "cv"): cv[..., sl].astype(np.float32),
        }
        o = b._render_phaser(ph, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _impulse(n):
    x = np.zeros(n, dtype=np.float32)
    x[0] = 1.0
    return x


def _notch_count(sig, lo_hz=200.0, hi_hz=6000.0):
    """Rough count of spectral notches in a band (smoothed magnitude dips)."""
    m = len(sig)
    S = np.abs(np.fft.rfft(sig))
    w = 48
    S = np.convolve(S, np.ones(w) / w, mode="same")
    f = np.fft.rfftfreq(m, 1.0 / SR)
    band = (f > lo_hz) & (f < hi_hz)
    Sb = S[band]
    med = np.median(Sb)
    dip = (Sb < 0.5 * med).astype(int)
    return int(np.sum(np.diff(dip) == 1))


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        ph = Patch().add_module("phaser")
        assert isinstance(ph, Phaser)
        assert ph.params == {
            "rate": 0.5,
            "depth": 0.6,
            "center": 800.0,
            "feedback": 0.4,
            "stages": 6,
            "mix": 0.5,
            "cv_depth": 1.0,
        }

    def test_ports_and_kinds(self):
        ph = Patch().add_module("phaser")
        assert [(p.name, p.signal_kind) for p in ph.input_ports] == [
            ("in", "audio"),
            ("rate_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in ph.output_ports] == [
            ("out_l", "audio"),
            ("out_r", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "phaser",
            params={"rate": 1.2, "feedback": -0.6, "center": 1200.0, "stages": 8},
        )
        restored = Patch.from_dict(patch.to_dict())
        ph = next(m for m in restored if m.TYPE == "phaser")
        assert ph.params["rate"] == 1.2
        assert ph.params["feedback"] == -0.6
        assert ph.params["center"] == 1200.0
        assert ph.params["stages"] == 8

    def test_unknown_param_rejected(self):
        # Phaser has no ``manual`` (that's the flanger) or ``voices`` (chorus).
        with pytest.raises(KeyError):
            Patch().add_module("phaser", params={"manual": 2.0})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ph = patch.add_module("phaser")
        patch.connect(osc.id, "out", ph.id, "in")  # no raise

    def test_cv_into_rate_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ph = patch.add_module("phaser")
        patch.connect(lfo.id, "cv", ph.id, "rate_cv")  # no raise

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ph = patch.add_module("phaser")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ph.id, "in")

    def test_audio_into_rate_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ph = patch.add_module("phaser")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", ph.id, "rate_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        ph = patch.add_module("phaser")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(ph.id, "out_l", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        ph = patch.add_module("phaser")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_phaser(ph, F, {}, patch)
        assert not np.any(o["out_l"]) and not np.any(o["out_r"])
        assert o["out_l"].shape == (F,)

    def test_frames_zero_empty(self):
        patch, src, ph, b = _rig()
        o = b._render_phaser(
            ph, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch
        )
        assert o["out_l"].shape == (0,) and o["out_r"].shape == (0,)

    def test_mix_zero_exact_dry_passthrough(self):
        # Strong feedback must not leak into the dry output at mix=0.
        patch, src, ph, b = _rig(
            {"mix": 0.0, "feedback": 0.8, "depth": 0.9, "stages": 8}
        )
        x = np.random.randn(F * 4).astype(np.float32)
        lo, r = _run(patch, src, ph, b, x)
        assert np.array_equal(lo, x[: len(lo)])
        assert np.array_equal(r, x[: len(r)])

    def test_output_is_float32(self):
        patch, src, ph, b = _rig({"mix": 0.6})
        lo, r = _run(patch, src, ph, b, np.random.randn(F * 2).astype(np.float32))
        assert lo.dtype == np.float32 and r.dtype == np.float32

    def test_notch_sweeps_across_tone(self):
        # A fixed 800 Hz tone through a phaser centred at 800 Hz: as the
        # notch sweeps through the tone its amplitude dips and returns, so
        # the output envelope is strongly modulated -- a moving notch.
        t = np.arange(2 * SR) / SR
        tone = np.sin(2 * np.pi * 800 * t).astype(np.float32)
        lo, _ = _run(
            *_rig(
                {"rate": 1.0, "depth": 0.8, "center": 800.0,
                 "feedback": 0.3, "mix": 0.5, "stages": 6}
            ),
            tone,
        )
        w = 1000
        env = np.convolve(np.abs(lo), np.ones(w) / w, mode="valid")
        assert env.max() / max(env.min(), 1e-6) > 2.0

    def test_depth_zero_static_but_not_dry(self):
        x = np.random.randn(F * 4).astype(np.float32)
        l0, _ = _run(
            *_rig({"depth": 0.0, "mix": 0.6, "feedback": 0.3, "rate": 1.5}), x
        )
        lm, _ = _run(
            *_rig({"depth": 0.7, "mix": 0.6, "feedback": 0.3, "rate": 1.5}), x
        )
        assert not np.array_equal(l0, x[: len(l0)])   # static notches, not dry
        assert not np.allclose(l0, lm, atol=1e-6)     # sweep matters

    def test_finite_and_bounded_at_extremes(self):
        patch, src, ph, b = _rig(
            {"depth": 1.0, "mix": 1.0, "feedback": 0.95, "rate": 10.0,
             "center": 6000.0, "stages": 8}
        )
        x = (np.random.randn(2 * SR) * 0.5).astype(np.float32)
        lo, r = _run(patch, src, ph, b, x)
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))
        assert np.max(np.abs(lo)) < 30.0 and np.max(np.abs(r)) < 30.0

    def test_voice_input_summed_to_mono(self):
        patch, src, ph, b = _rig({"mix": 1.0, "depth": 0.5})
        v = np.random.randn(3, F).astype(np.float32)
        o = b._render_phaser(ph, F, {(src.id, "out"): v}, patch)
        assert o["out_l"].shape == (F,) and o["out_r"].shape == (F,)
        assert np.all(np.isfinite(o["out_l"]))

    def test_single_voice_bit_identical_to_mono(self):
        params = {"mix": 0.7, "feedback": 0.5, "depth": 0.6, "stages": 8}
        x = np.random.randn(F * 4).astype(np.float32)
        lm, _ = _run(*_rig(params), x)
        patch, src, ph, b = _rig(params)
        ls = []
        for k in range(4):
            sl = slice(k * F, (k + 1) * F)
            o = b._render_phaser(
                ph, F, {(src.id, "out"): x[sl][None, :].astype(np.float32)}, patch
            )
            ls.append(o["out_l"])
        assert np.array_equal(np.concatenate(ls), lm)


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def test_output_independent_of_block_size(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(12000) / SR) * 0.4).astype(np.float32)
        params = {"rate": 2.0, "depth": 0.7, "center": 700.0,
                  "feedback": 0.6, "mix": 0.5, "stages": 6}
        la, ra = _run(*_rig(params, block=512), x, block=512)
        lb, rb = _run(*_rig(params, block=4096), x, block=4096)
        lc, rc = _run(*_rig(params, block=333), x, block=333)
        m = min(len(la), len(lb), len(lc))
        assert np.array_equal(la[:m], lb[:m])
        assert np.array_equal(la[:m], lc[:m])
        assert np.array_equal(ra[:m], rb[:m])


# ----- Feedback (bipolar resonance) ------------------------------------------


class TestFeedback:
    def test_positive_and_negative_feedback_differ(self):
        x = np.random.randn(F * 4).astype(np.float32)
        lp, _ = _run(*_rig({"feedback": 0.7, "mix": 0.6, "depth": 0.5}), x)
        ln, _ = _run(*_rig({"feedback": -0.7, "mix": 0.6, "depth": 0.5}), x)
        assert not np.allclose(lp, ln, atol=1e-6)

    def test_more_feedback_rings_longer(self):
        # An impulse rings in the resonant allpass loop; more regeneration
        # => a much fatter tail.
        imp = _impulse(F * 8)
        l_lo, _ = _run(*_rig({"feedback": 0.1, "mix": 0.7, "depth": 0.0, "stages": 8}), imp)
        l_hi, _ = _run(*_rig({"feedback": 0.9, "mix": 0.7, "depth": 0.0, "stages": 8}), imp)
        tail = slice(200, None)
        assert np.sum(np.abs(l_hi[tail])) > 3.0 * np.sum(np.abs(l_lo[tail]))

    def test_extreme_feedback_stays_bounded(self):
        patch, src, ph, b = _rig({"feedback": 0.95, "mix": 0.7, "depth": 0.4, "stages": 8})
        x = (np.random.randn(3 * SR) * 0.4).astype(np.float32)
        lo, r = _run(patch, src, ph, b, x)
        assert np.all(np.isfinite(lo)) and np.max(np.abs(lo)) < 60.0


# ----- Stages ----------------------------------------------------------------


class TestStages:
    def test_stage_count_changes_sound(self):
        x = np.random.randn(F * 4).astype(np.float32)
        l4, _ = _run(*_rig({"stages": 4, "mix": 0.5, "depth": 0.6}), x)
        l8, _ = _run(*_rig({"stages": 8, "mix": 0.5, "depth": 0.6}), x)
        assert not np.allclose(l4, l8, atol=1e-6)

    def test_more_stages_more_notches(self):
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(F * 40) * 0.3).astype(np.float32)
        l4, _ = _run(
            *_rig({"stages": 4, "feedback": 0.0, "mix": 0.5,
                   "depth": 0.0, "center": 1500.0}),
            noise,
        )
        l8, _ = _run(
            *_rig({"stages": 8, "feedback": 0.0, "mix": 0.5,
                   "depth": 0.0, "center": 1500.0}),
            noise,
        )
        assert _notch_count(l8) > _notch_count(l4)

    def test_out_of_range_and_string_stages_snap(self):
        # 5 -> snaps to a legal 4/6/8; "8" (combo string) coerces to 8.
        x = np.random.randn(F * 2).astype(np.float32)
        l5, _ = _run(*_rig({"stages": 5, "mix": 0.5}), x)
        l_str, _ = _run(*_rig({"stages": "8", "mix": 0.5}), x)
        l8, _ = _run(*_rig({"stages": 8, "mix": 0.5}), x)
        assert np.all(np.isfinite(l5))
        assert np.array_equal(l_str, l8)   # "8" == 8


# ----- Stereo ----------------------------------------------------------------


class TestStereo:
    def test_channels_are_decorrelated(self):
        patch, src, ph, b = _rig(
            {"depth": 0.7, "mix": 0.6, "feedback": 0.5, "rate": 1.5}
        )
        x = (np.random.randn(SR) * 0.3).astype(np.float32)
        lo, r = _run(patch, src, ph, b, x)
        assert not np.array_equal(lo, r)
        corr = np.corrcoef(lo[3000:], r[3000:])[0, 1]
        assert abs(corr) < 0.99


# ----- CV --------------------------------------------------------------------


class TestCV:
    def test_rate_cv_alters_output(self):
        x = (np.random.randn(F * 6) * 0.3).astype(np.float32)
        params = {"rate": 1.0, "depth": 0.6, "feedback": 0.4, "mix": 0.7, "cv_depth": 2.0}
        l_hi, _ = _run_cv(*_rig_cv(params), x, np.ones_like(x))
        l_zero, _ = _run_cv(*_rig_cv(params), x, np.zeros_like(x))
        assert not np.allclose(l_hi, l_zero, atol=1e-6)

    def test_zero_rate_cv_is_noop(self):
        x = (np.random.randn(F * 6) * 0.3).astype(np.float32)
        params = {"rate": 1.0, "depth": 0.6, "feedback": 0.4, "mix": 0.7, "cv_depth": 2.0}
        l_cv, _ = _run_cv(*_rig_cv(params), x, np.zeros_like(x))
        l_no, _ = _run(*_rig(params), x)
        assert np.array_equal(l_cv, l_no)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_phaser_stereo_speakers(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0})
        ph = patch.add_module(
            "phaser", params={"depth": 0.7, "mix": 0.5, "feedback": 0.6, "stages": 6}
        )
        spk_l = patch.add_module("left_speaker_output")
        spk_r = patch.add_module("right_speaker_output")
        patch.connect(osc.id, "out", ph.id, "in")
        patch.connect(ph.id, "out_l", spk_l.id, "in")
        patch.connect(ph.id, "out_r", spk_r.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(60):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
