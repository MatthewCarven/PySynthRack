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

Love pass (2026-09-22), three features all OFF at their defaults:
  - Pins: the default render is pinned to literals computed from the
    shipped code, and every new knob at its default / every new jack
    unpatched is bit-exactly that.
  - ``spread``: the L/R LFO phase offset the quadrature used to hard-code.
    0 makes the channels bit-identical, 0.5 IS the shipped quadrature,
    and the LEFT channel never moves (its offset is 0 at any spread).
    Correlation measured at 0 / 0.5 / 1.
  - ``clock`` + ``division``: the sweep as a length in ticks. Ignored
    unpatched (``division`` then changes nothing); needs two edges; the
    locked sweep rate is measured off the DELAY (cross-correlation, not
    a tone's AM -- see the test); and the locked render is EXACTLY
    block-size independent (the free-running one only approximately --
    an absolute-sample phase schedule against a float accumulator).
  - ``manual_cv`` + ``manual_depth``: the centre delay as a per-sample
    jack, measured by where an impulse's tap lands (an octave of CV
    halves or doubles the delay, in BOTH modes -- in through-zero the
    reference tap moves with it, so the crossing travels). Depth-scales,
    disabled at depth 0, per-sample rather than block-mean, clamped to
    the knob's own rails, voice sources summed, NaN reads as no
    modulation.
  - Widgets: every param gets a bounded widget.
  - Example: ``flanger_jet.json``.
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
            "division": 4.0,
            "depth": 0.7,
            "manual": 1.5,
            "feedback": 0.5,
            "mix": 0.5,
            "spread": 0.5,
            "cv_depth": 1.0,
            "manual_depth": 1.0,
            "through_zero": False,
            "polarity": 1.0,
        }

    def test_ports_and_kinds(self):
        fl = Patch().add_module("flanger")
        assert [(p.name, p.signal_kind) for p in fl.input_ports] == [
            ("in", "audio"),
            ("rate_cv", "cv"),
            ("manual_cv", "cv"),
            ("clock", "gate"),
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


# ----- Love pass 2026-09-22: spread / clock+division / manual_cv --------------

# The default render, pinned to literals computed from the code as it
# shipped (seeded noise, block 512). Every "OFF at its default" claim
# below leans on this.
_PIN_IDX = (0, 1, 511, 512, 2000, 4095)
_PIN_L = (0.0002460306859575212, 0.05974910780787468, 0.17219002544879913,
          -0.1301274448633194, -0.19941115379333496, -0.04489603638648987)
_PIN_R = (0.0002460306859575212, 0.05974910780787468, 0.18407094478607178,
          -0.11562705039978027, 0.09172980487346649, 0.1644388735294342)


def _noise(n=4096, seed=7, amp=0.4):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(n)).astype(np.float32)


def _rig_jacks(params=None, block=F, manual=False, clock=False):
    """A flanger with optional ``manual_cv`` / ``clock`` cables attached."""
    patch = Patch()
    src = patch.add_module("oscillator")
    fl = patch.add_module("flanger", params=params or {})
    patch.connect(src.id, "out", fl.id, "in")
    man = clk = None
    if manual:
        man = patch.add_module("lfo")
        patch.connect(man.id, "cv", fl.id, "manual_cv")
    if clock:
        clk = patch.add_module("clock")
        patch.connect(clk.id, "out", fl.id, "clock")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, fl, b, man, clk


def _run_jacks(rig, signal, manual_cv=None, gate=None, block=F):
    patch, src, fl, b, man, clk = rig
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        if man is not None:
            bufs[(man.id, "cv")] = manual_cv[..., sl].astype(np.float32)
        if clk is not None:
            bufs[(clk.id, "out")] = gate[sl].astype(np.float32)
        o = b._render_flanger(fl, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _plain(params=None, signal=None, block=F):
    sig = _noise() if signal is None else signal
    return _run(*_rig(params, block=block), sig, block=block)


def _ticks(n, period, width=8):
    g = np.zeros(n, dtype=np.float32)
    for i in range(0, n, period):
        g[i:i + width] = 1.0
    return g


def _corr(a, b):
    a = a.astype(np.float64) - a.mean()
    b = b.astype(np.float64) - b.mean()
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def _tap(y, lo=4, hi=800):
    """Sample lag of the loudest echo after the dry impulse."""
    return int(np.argmax(np.abs(y[lo:hi]))) + lo


class TestDefaultsArePinned:
    def test_the_default_render_matches_the_shipped_literals(self):
        l, r = _plain()
        assert [float(l[i]) for i in _PIN_IDX] == list(_PIN_L)
        assert [float(r[i]) for i in _PIN_IDX] == list(_PIN_R)

    def test_every_new_knob_at_its_default_is_the_default_render(self):
        base = _plain()
        explicit = _plain({"spread": 0.5, "division": 4.0,
                           "manual_depth": 1.0})
        assert np.array_equal(base[0], explicit[0])
        assert np.array_equal(base[1], explicit[1])


class TestSpread:
    def test_zero_makes_the_two_channels_bit_identical(self):
        l, r = _plain({"spread": 0.0})
        assert np.array_equal(l, r)

    def test_the_left_channel_never_moves(self):
        # The L offset is 0 at any spread, so ``spread`` is purely a
        # right-channel control -- nothing to un-learn about the mono sum.
        ref = _plain({"spread": 0.0})[0]
        for sp in (0.25, 0.5, 0.75, 1.0):
            assert np.array_equal(_plain({"spread": sp})[0], ref)

    def test_the_right_channel_does_move(self):
        a = _plain({"spread": 0.0})[1]
        for sp in (0.25, 0.5, 1.0):
            assert not np.array_equal(_plain({"spread": sp})[1], a)

    def test_correlation_falls_as_the_pair_opens(self):
        # Measured on a noise bed at depth 1: 1.000 / 0.579 / 0.497.
        sig = _noise(n=SR)
        c0 = _corr(*_plain({"spread": 0.0, "depth": 1.0}, sig))
        c5 = _corr(*_plain({"spread": 0.5, "depth": 1.0}, sig))
        c1 = _corr(*_plain({"spread": 1.0, "depth": 1.0}, sig))
        assert c0 == pytest.approx(1.0, abs=1e-6)
        assert 0.45 < c5 < 0.7
        assert 0.4 < c1 < 0.6
        assert c1 < c5 < c0

    def test_it_works_in_through_zero_too(self):
        sig = _noise(n=SR)
        p = {"through_zero": True, "depth": 1.0, "manual": 6.0}
        assert np.array_equal(*_plain(dict(p, spread=0.0), sig))
        assert _corr(*_plain(dict(p, spread=1.0), sig)) < 0.99


class TestClockSync:
    def test_division_does_nothing_with_the_clock_unpatched(self):
        a = _plain({"division": 0.25})
        b = _plain({"division": 64.0})
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_one_edge_is_not_a_period(self):
        # A period needs two edges; until then the rate knob still drives
        # the sweep, bit-for-bit as if nothing were patched.
        sig = _noise(n=SR)
        gate = np.zeros(SR, dtype=np.float32)
        gate[100:108] = 1.0                       # exactly one rising edge
        rig = _rig_jacks({"division": 4.0}, clock=True)
        l, r = _run_jacks(rig, sig, gate=gate)
        free = _plain({"division": 4.0}, sig)
        assert np.array_equal(l, free[0]) and np.array_equal(r, free[1])

    def test_the_sweep_takes_division_ticks(self):
        # MEASURE THE RIGHT OBSERVABLE. A tone's AM is the phaser's
        # observable, not the flanger's: a comb has a notch every 1/delay
        # Hz, so one tone crosses many of them per sweep and the envelope
        # says nothing useful (the first draft of this test read 2.50 Hz
        # where 0.5 was wanted). What the flanger sweeps is the DELAY, so
        # measure the delay: pure wet (``mix`` 1, no feedback) makes the
        # output a delayed copy of the input, and the lag that maximises
        # the cross-correlation of a short window against the input IS
        # the delay. That lag series wobbles at exactly the sweep rate.
        n = SR * 12
        sig = _noise(n=n, seed=17, amp=0.5)
        gate = _ticks(n, SR // 2)                 # a 2 Hz clock
        win, step, maxlag = 4096, 2048, 600
        for div in (2.0, 4.0):
            rig = _rig_jacks(
                {"rate": 0.05, "division": div, "depth": 1.0,
                 "feedback": 0.0, "mix": 1.0, "manual": 5.0, "spread": 0.0},
                clock=True,
            )
            wet, _r = _run_jacks(rig, sig, gate=gate)
            x = sig.astype(np.float64)
            y = wet.astype(np.float64)
            lags = []
            start = SR * 2                        # after the lock engages
            while start + win + maxlag < y.size:
                seg = y[start:start + win]
                ref = x[start - maxlag:start + win]
                c = np.correlate(ref, seg, mode="valid")   # maxlag+1 taps
                lags.append(maxlag - int(np.argmax(c)))
                start += step
            lag = np.asarray(lags, dtype=np.float64)
            lag -= lag.mean()
            S = np.abs(np.fft.rfft(lag))
            f = np.fft.rfftfreq(lag.size, step / SR)
            band = f > 0.02
            got = float(f[band][np.argmax(S[band])])
            want = 1.0 / (0.5 * div)              # ticks are 0.5 s apart
            assert got == pytest.approx(want, rel=0.1), (div, got, want)

    def test_a_locked_sweep_is_exactly_block_size_independent(self):
        # Keying the locked phase to the ABSOLUTE sample index instead of
        # accumulating it buys identical bits at 64 and 512.
        n = 44032                       # a whole number of 512s AND of 64s
        sig = _noise(n=n, seed=3)
        gate = _ticks(n, 64)            # locks at sample 64, before any
        #                                 block boundary the two disagree on
        p = {"division": 32.0, "depth": 0.8, "feedback": 0.5, "spread": 0.7}
        a = _run_jacks(_rig_jacks(p, clock=True, block=512), sig,
                       gate=gate, block=512)
        b = _run_jacks(_rig_jacks(p, clock=True, block=64), sig,
                       gate=gate, block=64)
        assert np.array_equal(a[0], b[0])
        assert np.array_equal(a[1], b[1])

    def test_the_free_running_sweep_only_drifts_a_little(self):
        # Honest counterpart: the shipped float phase accumulator is NOT
        # bit-exact across block sizes (~3e-8 over a second here), which
        # is why the clocked path does not use one.
        n = 44032
        sig = _noise(n=n, seed=3)
        a = _plain({"depth": 0.8, "feedback": 0.5}, sig, block=512)[0]
        b = _plain({"depth": 0.8, "feedback": 0.5}, sig, block=64)[0]
        assert float(np.abs(a - b).max()) < 1e-5

    def test_a_faster_clock_sweeps_faster(self):
        n = SR * 4
        sig = _noise(n=n, seed=5)
        p = {"division": 4.0, "depth": 1.0, "spread": 0.0}
        slow = _run_jacks(_rig_jacks(p, clock=True), sig,
                          gate=_ticks(n, SR // 2))[0]
        fast = _run_jacks(_rig_jacks(p, clock=True), sig,
                          gate=_ticks(n, SR // 4))[0]
        assert not np.array_equal(slow, fast)

    def test_the_lock_survives_a_through_zero_flip(self):
        # ``through_zero`` re-inits the ring; the measured period must
        # survive that clear or the sweep would fall back to the knob.
        n = SR
        sig = _noise(n=n, seed=9)
        gate = _ticks(n, 64)
        patch, src, fl, b, _m, clk = _rig_jacks({"division": 32.0},
                                                clock=True)
        for k in range(n // F):
            if k == 3:
                fl.set_param("through_zero", True)
            sl = slice(k * F, (k + 1) * F)
            b._render_flanger(fl, F, {(src.id, "out"): sig[sl],
                                      (clk.id, "out"): gate[sl]}, patch)
        assert b._state[fl.id]["interval"] == 64
        assert b._state[fl.id]["period"] == 64 * 32.0


class TestManualCv:
    def test_manual_depth_does_nothing_with_the_jack_unpatched(self):
        a = _plain({"manual_depth": 0.0})
        b = _plain({"manual_depth": 4.0})
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_an_octave_of_cv_halves_or_doubles_the_delay(self):
        # Measured off the impulse response: the echo lands at 33 / 66 /
        # 132 samples for cv -1 / 0 / +1 with ``manual`` at 1.5 ms.
        n = SR
        imp = _impulse(n)
        p = {"depth": 0.0, "feedback": 0.0, "mix": 0.5, "manual": 1.5,
             "manual_depth": 1.0, "spread": 0.0}
        taps = {}
        for c in (-1.0, 0.0, 1.0):
            rig = _rig_jacks(p, manual=True)
            l, _r = _run_jacks(rig, imp, manual_cv=np.full(n, c, np.float32))
            taps[c] = _tap(l)
        assert taps[0.0] == pytest.approx(1.5 * SR / 1000.0, abs=1.5)
        assert taps[-1.0] == pytest.approx(taps[0.0] / 2.0, abs=1.5)
        assert taps[1.0] == pytest.approx(taps[0.0] * 2.0, abs=1.5)

    def test_through_zero_moves_its_reference_tap_too(self):
        # With depth 0 the two through-zero taps sit on top of each other
        # at the centre delay, so the impulse's echo IS the reference tap:
        # it has to travel with the jack, or the crossing would be stuck.
        n = SR
        imp = _impulse(n)
        p = {"through_zero": True, "polarity": 1.0, "depth": 0.0,
             "feedback": 0.0, "mix": 0.5, "manual": 3.0,
             "manual_depth": 1.0, "spread": 0.0}
        taps = {}
        for c in (-1.0, 0.0):
            rig = _rig_jacks(p, manual=True)
            l, _r = _run_jacks(rig, imp, manual_cv=np.full(n, c, np.float32))
            taps[c] = _tap(l)
        assert taps[0.0] == pytest.approx(3.0 * SR / 1000.0, abs=2.0)
        assert taps[-1.0] == pytest.approx(taps[0.0] / 2.0, abs=2.0)

    def test_depth_scales_the_jack(self):
        n = 4096
        sig = _noise(n=n)
        a = _run_jacks(_rig_jacks({"manual_depth": 2.0}, manual=True), sig,
                       manual_cv=np.full(n, 0.5, np.float32))
        b = _run_jacks(_rig_jacks({"manual_depth": 1.0}, manual=True), sig,
                       manual_cv=np.full(n, 1.0, np.float32))
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_depth_zero_is_the_unpatched_render(self):
        n = 4096
        sig = _noise(n=n)
        a = _run_jacks(_rig_jacks({"manual_depth": 0.0}, manual=True), sig,
                       manual_cv=np.full(n, 0.9, np.float32))
        b = _plain({}, sig)
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_a_zero_cv_is_a_noop(self):
        n = 4096
        sig = _noise(n=n)
        a = _run_jacks(_rig_jacks({"manual_depth": 2.0}, manual=True), sig,
                       manual_cv=np.zeros(n, np.float32))
        b = _plain({}, sig)
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_the_jack_is_per_sample_not_block_mean(self):
        # A CV whose mean over every block is 0 but which moves inside the
        # block: a block-mean jack would be a noop here, a per-sample one
        # is not.
        n = 4096
        sig = _noise(n=n)
        cv = np.tile(np.concatenate([np.full(256, 1.0, np.float32),
                                     np.full(256, -1.0, np.float32)]), 8)
        a = _run_jacks(_rig_jacks({"manual_depth": 2.0}, manual=True), sig,
                       manual_cv=cv)
        b = _plain({}, sig)
        assert not np.array_equal(a[0], b[0])

    def test_the_modulated_centre_stays_inside_the_knob_rails(self):
        # The jack is clamped to the same 0.1..10 ms the knob is, which is
        # what keeps the read inside the ring however hard the CV pushes.
        n = 8192
        sig = _noise(n=n)
        cv = np.linspace(-40.0, 40.0, n).astype(np.float32)
        for tz in (False, True):
            a = _run_jacks(
                _rig_jacks({"manual_depth": 4.0, "manual": 5.0,
                            "feedback": 0.9, "through_zero": tz},
                           manual=True),
                sig, manual_cv=cv,
            )
            assert np.all(np.isfinite(a[0])) and np.all(np.isfinite(a[1]))
            assert float(np.abs(a[0]).max()) < 50.0

    def test_a_voice_source_is_summed_to_mono(self):
        n = 2048
        sig = _noise(n=n)
        # 0.25 + 0.5 is exact in float32, so the collapse is bit-exact
        # rather than merely close -- no tolerance to argue about.
        rows = np.stack([np.full(n, 0.25, np.float32),
                         np.full(n, 0.5, np.float32)])
        a = _run_jacks(_rig_jacks({"manual_depth": 1.5}, manual=True), sig,
                       manual_cv=rows)
        b = _run_jacks(_rig_jacks({"manual_depth": 1.5}, manual=True), sig,
                       manual_cv=np.full(n, 0.75, np.float32))
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_a_non_finite_cv_reads_as_no_modulation(self):
        n = 2048
        sig = _noise(n=n)
        cv = np.full(n, 0.5, np.float32)
        cv[500] = np.nan
        cv[900] = np.inf
        a = _run_jacks(_rig_jacks({"manual_depth": 2.0}, manual=True), sig,
                       manual_cv=cv)
        assert np.all(np.isfinite(a[0])) and np.all(np.isfinite(a[1]))


def test_all_three_features_live_stay_block_size_independent():
    n = 44032
    sig = _noise(n=n, seed=13)
    cv = (0.8 * np.sin(2 * np.pi * 0.7 * np.arange(n) / SR)).astype(np.float32)
    gate = _ticks(n, 64)
    for tz in (False, True):
        p = {"spread": 0.83, "division": 24.0, "manual_depth": 1.7,
             "depth": 0.6, "feedback": 0.6, "mix": 0.55, "manual": 2.0,
             "through_zero": tz}
        a = _run_jacks(_rig_jacks(p, manual=True, clock=True, block=512),
                       sig, manual_cv=cv, gate=gate, block=512)
        b = _run_jacks(_rig_jacks(p, manual=True, clock=True, block=64),
                       sig, manual_cv=cv, gate=gate, block=64)
        assert np.array_equal(a[0], b[0]), tz
        assert np.array_equal(a[1], b[1]), tz


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch):
    from unittest import mock

    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("flanger")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("format"))
    return out


def test_every_param_gets_a_bounded_widget(monkeypatch):
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in Flanger.DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    # And the new three reached the FLANGER's own branch, not somebody
    # else's same-named one ("matching a label is not matching the widget").
    assert w["division"][1] == "%.2f ticks"
    assert w["manual_depth"][1] == "%.2f oct/unit"
    assert w["spread"][0] == "add_slider_float"
    assert w["manual"][1] == "%.2f ms"


# ----- example ----------------------------------------------------------------


def _render_example(name, tweak=None, seconds=6.0, block=F):
    from pathlib import Path

    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / name
    patch = load_patch(path)
    if tweak:
        tweak(patch)
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    cap = []
    orig = b._render_flanger

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.append((r["out_l"].copy(), r["out_r"].copy()))
        return r

    b._render_flanger = spy
    peak = 0.0
    for _ in range(int(SR * seconds / block)):
        out, _devices = b.render_block_multi(block)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    return (peak,
            np.concatenate([c[0] for c in cap]),
            np.concatenate([c[1] for c in cap]))


def _set(**kw):
    def t(patch):
        m = next(x for x in patch if x.TYPE == "flanger")
        for k, v in kw.items():
            m.set_param(k, v)
    return t


def test_the_jet_example_is_wide_clocked_and_through_zero():
    peak, l, r = _render_example("flanger_jet.json")
    assert 0.3 < peak < 0.8
    # ``spread`` 1 is a real stereo pair; 0 collapses it to one comb.
    assert _corr(l, r) < 0.95
    _p, l0, r0 = _render_example("flanger_jet.json", tweak=_set(spread=0.0))
    assert np.array_equal(l0, r0)

    # The clock cable is doing work: pull it and the sweep free-runs.
    def unclock(patch):
        fl = next(x for x in patch if x.TYPE == "flanger")
        for c in list(patch.cables):
            if c.dst_module_id == fl.id and c.dst_port == "clock":
                patch.disconnect(c.src_module_id, c.src_port,
                                 c.dst_module_id, c.dst_port)

    _p, lu, _ru = _render_example("flanger_jet.json", tweak=unclock)
    assert not np.array_equal(l, lu)
    # And so is through-zero.
    _p, lf, _rf = _render_example("flanger_jet.json",
                                  tweak=_set(through_zero=False))
    assert not np.array_equal(l, lf)
