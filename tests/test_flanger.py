"""Tests for the Flanger (swept, resonant, bipolar-feedback comb).

Coverage:
  - Model: registration, defaults, ports/kinds (audio ``in`` + cv
    ``rate_cv`` -> ``out_l`` / ``out_r`` audio), JSON round-trip,
    unknown-param rejection, and the signal-kind type walls.
  - DSP: disconnected -> silence; ``mix=0`` is a bit-exact dry
    passthrough on both channels *even with strong feedback*; an impulse
    produces a tap around the ``manual`` delay; ``depth=0`` is a static
    comb (not dry) that differs from a swept ``depth>0`` render; output
    stays finite/bounded at extreme settings; a voice (2D) input is
    summed to mono, and a single-voice input is bit-identical to mono.
  - Block independence: the per-sample feedback engine carries its LFO
    phase and ring state across blocks, so the output is bit-identical
    at any block size (512 vs 4096 vs an odd size).
  - Feedback: bipolar -- positive and negative feedback differ; more
    feedback rings longer; the loop stays bounded at the +/-0.95 clamp.
  - Stereo: the two channels are decorrelated (quadrature LFO).
  - CV: ``rate_cv`` alters the sweep; an all-zero ``rate_cv`` is a noop.
  - Integration: osc -> flanger -> L/R speakers renders audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.flanger import Flanger

SR = 44100
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    fl = patch.add_module("flanger", params=params or {})
    patch.connect(src.id, "out", fl.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, fl, b


def _run(patch, src, fl, b, signal, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        o = b._render_flanger(fl, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _rig_cv(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    fl = patch.add_module("flanger", params=params or {})
    patch.connect(src.id, "out", fl.id, "in")
    patch.connect(lfo.id, "cv", fl.id, "rate_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, lfo, fl, b


def _run_cv(patch, src, lfo, fl, b, signal, cv, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {
            (src.id, "out"): signal[..., sl].astype(np.float32),
            (lfo.id, "cv"): cv[..., sl].astype(np.float32),
        }
        o = b._render_flanger(fl, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _impulse(n):
    x = np.zeros(n, dtype=np.float32)
    x[0] = 1.0
    return x


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        fl = Patch().add_module("flanger")
        assert isinstance(fl, Flanger)
        assert fl.params == {
            "rate": 0.3,
            "depth": 0.7,
            "manual": 1.5,
            "feedback": 0.5,
            "mix": 0.5,
            "cv_depth": 1.0,
            "through_zero": False,
            "polarity": 1.0,
        }

    def test_ports_and_kinds(self):
        fl = Patch().add_module("flanger")
        assert [(p.name, p.signal_kind) for p in fl.input_ports] == [
            ("in", "audio"),
            ("rate_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in fl.output_ports] == [
            ("out_l", "audio"),
            ("out_r", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "flanger", params={"rate": 1.2, "feedback": -0.6, "manual": 3.0}
        )
        restored = Patch.from_dict(patch.to_dict())
        fl = next(m for m in restored if m.TYPE == "flanger")
        assert fl.params["rate"] == 1.2
        assert fl.params["feedback"] == -0.6
        assert fl.params["manual"] == 3.0

    def test_unknown_param_rejected(self):
        # Flanger has no ``voices`` (that's the chorus).
        with pytest.raises(KeyError):
            Patch().add_module("flanger", params={"voices": 3})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        fl = patch.add_module("flanger")
        patch.connect(osc.id, "out", fl.id, "in")  # no raise

    def test_cv_into_rate_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        fl = patch.add_module("flanger")
        patch.connect(lfo.id, "cv", fl.id, "rate_cv")  # no raise

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        fl = patch.add_module("flanger")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", fl.id, "in")

    def test_audio_into_rate_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        fl = patch.add_module("flanger")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", fl.id, "rate_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        fl = patch.add_module("flanger")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(fl.id, "out_l", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        fl = patch.add_module("flanger")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_flanger(fl, F, {}, patch)
        assert not np.any(o["out_l"]) and not np.any(o["out_r"])
        assert o["out_l"].shape == (F,)

    def test_frames_zero_empty(self):
        patch, src, fl, b = _rig()
        o = b._render_flanger(
            fl, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch
        )
        assert o["out_l"].shape == (0,) and o["out_r"].shape == (0,)

    def test_mix_zero_exact_dry_passthrough(self):
        # Strong feedback must not leak into the dry output at mix=0.
        patch, src, fl, b = _rig(
            {"mix": 0.0, "feedback": 0.8, "depth": 0.9, "manual": 2.0}
        )
        x = np.random.randn(F * 4).astype(np.float32)
        lo, r = _run(patch, src, fl, b, x)
        assert np.array_equal(lo, x[: len(lo)])
        assert np.array_equal(r, x[: len(r)])

    def test_output_is_float32(self):
        patch, src, fl, b = _rig({"mix": 0.6})
        lo, r = _run(patch, src, fl, b, np.random.randn(F * 2).astype(np.float32))
        assert lo.dtype == np.float32 and r.dtype == np.float32

    def test_impulse_produces_tap_near_manual(self):
        # depth=0, feedback=0, mix=1 -> a single delayed tap at ``manual``.
        manual_ms = 1.5
        patch, src, fl, b = _rig(
            {"mix": 1.0, "depth": 0.0, "feedback": 0.0, "manual": manual_ms}
        )
        lo, _ = _run(patch, src, fl, b, _impulse(F * 4))
        tap = int(round(manual_ms * 1e-3 * SR))
        assert abs(int(np.argmax(np.abs(lo[:200]))) - tap) <= 2
        assert np.all(np.isfinite(lo))

    def test_depth_zero_static_but_not_dry(self):
        x = np.random.randn(F * 4).astype(np.float32)
        p0, s0, f0, b0 = _rig(
            {"depth": 0.0, "mix": 0.6, "feedback": 0.3, "rate": 1.5}
        )
        l0, _ = _run(p0, s0, f0, b0, x)
        pm, sm, fm, bm = _rig(
            {"depth": 0.7, "mix": 0.6, "feedback": 0.3, "rate": 1.5}
        )
        lm, _ = _run(pm, sm, fm, bm, x)
        assert not np.array_equal(l0, x[: len(l0)])   # static comb, not dry
        assert not np.allclose(l0, lm, atol=1e-6)     # sweep matters

    def test_finite_and_bounded_at_extremes(self):
        patch, src, fl, b = _rig(
            {"depth": 1.0, "mix": 1.0, "feedback": 0.95, "rate": 10.0, "manual": 0.1}
        )
        x = (np.random.randn(2 * SR) * 0.5).astype(np.float32)
        lo, r = _run(patch, src, fl, b, x)
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))
        assert np.max(np.abs(lo)) < 12.0 and np.max(np.abs(r)) < 12.0

    def test_voice_input_summed_to_mono(self):
        patch, src, fl, b = _rig({"mix": 1.0, "depth": 0.5})
        v = np.random.randn(3, F).astype(np.float32)
        o = b._render_flanger(fl, F, {(src.id, "out"): v}, patch)
        assert o["out_l"].shape == (F,) and o["out_r"].shape == (F,)
        assert np.all(np.isfinite(o["out_l"]))

    def test_single_voice_bit_identical_to_mono(self):
        params = {"mix": 0.7, "feedback": 0.5, "depth": 0.6, "manual": 2.0}
        x = np.random.randn(F * 4).astype(np.float32)
        lm, _ = _run(*_rig(params), x)
        # Same signal shaped (1, F) per block should match the mono render.
        patch, src, fl, b = _rig(params)
        ls = []
        for k in range(4):
            sl = slice(k * F, (k + 1) * F)
            o = b._render_flanger(
                fl, F, {(src.id, "out"): x[sl][None, :].astype(np.float32)}, patch
            )
            ls.append(o["out_l"])
        assert np.array_equal(np.concatenate(ls), lm)


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def test_output_independent_of_block_size(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(12000) / SR) * 0.4).astype(np.float32)
        params = {"rate": 2.0, "depth": 0.7, "manual": 1.5, "feedback": 0.6, "mix": 0.5}
        la, ra = _run(*_rig(params, block=512), x, block=512)
        lb, rb = _run(*_rig(params, block=4096), x, block=4096)
        lc, rc = _run(*_rig(params, block=333), x, block=333)
        m = min(len(la), len(lb), len(lc))
        assert np.array_equal(la[:m], lb[:m])
        assert np.array_equal(la[:m], lc[:m])
        assert np.array_equal(ra[:m], rb[:m])


# ----- Feedback (bipolar regeneration) ---------------------------------------


class TestFeedback:
    def test_positive_and_negative_feedback_differ(self):
        x = np.random.randn(F * 4).astype(np.float32)
        lp, _ = _run(*_rig({"feedback": 0.7, "mix": 0.6, "depth": 0.5}), x)
        ln, _ = _run(*_rig({"feedback": -0.7, "mix": 0.6, "depth": 0.5}), x)
        assert not np.allclose(lp, ln, atol=1e-6)

    def test_more_feedback_rings_longer(self):
        # An impulse rings in the comb; more regeneration => a fatter tail.
        imp = _impulse(F * 8)
        l_lo, _ = _run(*_rig({"feedback": 0.2, "mix": 0.8, "depth": 0.0}), imp)
        l_hi, _ = _run(*_rig({"feedback": 0.9, "mix": 0.8, "depth": 0.0}), imp)
        tail = slice(int(0.01 * SR), None)   # past the first tap
        assert np.sum(np.abs(l_hi[tail])) > 2.0 * np.sum(np.abs(l_lo[tail]))

    def test_extreme_feedback_stays_bounded(self):
        patch, src, fl, b = _rig({"feedback": 0.95, "mix": 0.7, "depth": 0.4})
        x = (np.random.randn(3 * SR) * 0.4).astype(np.float32)
        lo, r = _run(patch, src, fl, b, x)
        assert np.all(np.isfinite(lo)) and np.max(np.abs(lo)) < 20.0


# ----- Stereo ----------------------------------------------------------------


class TestStereo:
    def test_channels_are_decorrelated(self):
        patch, src, fl, b = _rig(
            {"depth": 0.7, "mix": 0.6, "feedback": 0.5, "rate": 1.5}
        )
        x = (np.random.randn(SR) * 0.3).astype(np.float32)
        lo, r = _run(patch, src, fl, b, x)
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
    def test_osc_flanger_stereo_speakers(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0})
        fl = patch.add_module(
            "flanger", params={"depth": 0.7, "mix": 0.5, "feedback": 0.6}
        )
        spk_l = patch.add_module("left_speaker_output")
        spk_r = patch.add_module("right_speaker_output")
        patch.connect(osc.id, "out", fl.id, "in")
        patch.connect(fl.id, "out_l", spk_l.id, "in")
        patch.connect(fl.id, "out_r", spk_r.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(60):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0


# ----- Through-zero (tape) mode ---------------------------------------------


class TestThroughZero:
    def test_defaults_present(self):
        fl = Patch().add_module("flanger")
        assert fl.params["through_zero"] is False
        assert fl.params["polarity"] == 1.0

    def test_off_matches_unspecified(self):
        # Explicit through_zero=False is byte-identical to not passing it:
        # the standard positive-delay path is untouched.
        rng = np.random.default_rng(4)
        sig = rng.standard_normal(F * 6).astype(np.float32)
        a = _run(*_rig({"manual": 2.0, "feedback": 0.6}), sig)
        b = _run(*_rig({"manual": 2.0, "feedback": 0.6, "through_zero": False}), sig)
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_on_differs_from_standard(self):
        rng = np.random.default_rng(5)
        sig = rng.standard_normal(F * 6).astype(np.float32)
        std = _run(*_rig({"manual": 6.0, "depth": 0.9}), sig)
        tz = _run(*_rig({"manual": 6.0, "depth": 0.9, "through_zero": True}), sig)
        assert not np.allclose(std[0], tz[0])

    def test_mix0_bit_exact_dry(self):
        rng = np.random.default_rng(6)
        sig = rng.standard_normal(F * 6).astype(np.float32)
        l, r = _run(
            *_rig({"through_zero": True, "mix": 0.0, "feedback": 0.8,
                   "depth": 0.9, "manual": 5.0}),
            sig,
        )
        dry = sig[: l.shape[0]]
        assert np.array_equal(l, dry) and np.array_equal(r, dry)

    def test_polarity_identity(self):
        # The polarity knob is exactly the additive/subtractive tap blend:
        # add(+1) + sub(-1) == 2 * ref(0).
        rng = np.random.default_rng(11)
        sig = (rng.standard_normal(F * 8) * 0.4).astype(np.float32)
        common = {"through_zero": True, "rate": 0.5, "depth": 1.0,
                  "manual": 8.0, "feedback": 0.0, "mix": 1.0}
        add = _run(*_rig({**common, "polarity": 1.0}), sig)[0]
        sub = _run(*_rig({**common, "polarity": -1.0}), sig)[0]
        ref = _run(*_rig({**common, "polarity": 0.0}), sig)[0]
        assert np.max(np.abs((add + sub) - 2.0 * ref)) < 1e-5

    def test_additive_subtractive_antiphase(self):
        # Additive and subtractive combs are inverted (interleaved notches):
        # as the sweep moves, when one notches a probe tone the other passes
        # it, so the two output envelopes strongly anti-correlate.
        t = np.arange(SR * 2) / SR
        tone = (0.6 * np.sin(2 * np.pi * 2000 * t)).astype(np.float32)
        common = {"through_zero": True, "rate": 1.0, "depth": 1.0,
                  "manual": 8.0, "feedback": 0.0, "mix": 1.0}
        ya = _run(*_rig({**common, "polarity": 1.0}), tone)[0]
        ys = _run(*_rig({**common, "polarity": -1.0}), tone)[0]
        W, H = 1024, 256

        def env(y):
            fr = (len(y) - W) // H
            e = np.array([np.sqrt(np.mean(y[i * H:i * H + W] ** 2)) for i in range(fr)])
            return e[SR // H:]

        assert np.corrcoef(env(ya), env(ys))[0, 1] < -0.5

    def test_feedback_bounded(self):
        # Feedback taps the moving read floored a few samples behind the
        # write head, so even fb=0.9 through the sweep extreme stays stable.
        imp = np.zeros(F * 40, dtype=np.float32)
        imp[0] = 1.0
        l, r = _run(
            *_rig({"through_zero": True, "feedback": 0.9, "depth": 1.0,
                   "manual": 6.0, "rate": 2.0, "mix": 0.6}),
            imp,
        )
        assert np.all(np.isfinite(l)) and float(np.abs(l).max()) < 50.0

    def test_voice_equals_mono(self):
        rng = np.random.default_rng(8)
        sig = rng.standard_normal(F * 6).astype(np.float32)
        prm = {"through_zero": True, "depth": 0.8, "manual": 4.0, "feedback": 0.4}
        lm, rm = _run(*_rig(prm), sig)
        lv, rv = _run(*_rig(prm), sig[None, :])
        assert np.array_equal(lm, lv) and np.array_equal(rm, rv)

    def test_block_independent(self):
        rng = np.random.default_rng(9)
        sig = rng.standard_normal(F * 8).astype(np.float32)
        prm = {"through_zero": True, "depth": 0.9, "manual": 4.0, "feedback": 0.4}
        a = _run(*_rig(prm, block=512), sig, block=512)[0]
        c = _run(*_rig(prm, block=333), sig, block=333)[0]
        m = min(a.shape[0], c.shape[0])
        assert np.max(np.abs(a[:m] - c[:m])) == 0.0
