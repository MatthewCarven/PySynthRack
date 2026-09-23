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

Love pass (2026-09-22), three features all OFF at their defaults:
  - Pins: the default render is pinned to literals computed from the
    shipped code, and every new knob at its default / every new jack
    unpatched is bit-exactly that.
  - ``spread``: the L/R LFO phase offset the quadrature used to hard-code.
    0 makes the channels bit-identical, 0.5 IS the shipped quadrature,
    and the LEFT channel never moves (its offset is 0 at any spread).
    Correlation measured at 0 / 0.5 / 1.
  - ``clock`` + ``division``: the sweep as a length in ticks. Ignored
    unpatched (``division`` then changes nothing); needs two edges;
    the locked sweep rate is measured off a tone's AM via the Hilbert
    envelope; and the locked render is EXACTLY block-size independent
    (and, since 2026-09-24, so is the free-running one -- an integer
    sample count since the last rate change, not a float accumulator;
    tests/test_modfx_block_exact.py has the four-second pins).
  - ``manual_cv`` + ``manual_depth``: the notch position as a per-sample
    jack. Measured by where the deepest notch lands (an octave of CV
    moves it an octave), depth-scales, disabled at depth 0, per-sample
    rather than block-mean, voice sources summed, NaN reads as no
    modulation.
  - Widgets: every param gets a bounded widget.
  - Example: ``phaser_envelope_sweep.json``.
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
            "division": 4.0,
            "depth": 0.6,
            "center": 800.0,
            "feedback": 0.4,
            "stages": 6,
            "mix": 0.5,
            "spread": 0.5,
            "cv_depth": 1.0,
            "manual_depth": 1.0,
        }

    def test_ports_and_kinds(self):
        ph = Patch().add_module("phaser")
        assert [(p.name, p.signal_kind) for p in ph.input_ports] == [
            ("in", "audio"),
            ("rate_cv", "cv"),
            ("manual_cv", "cv"),
            ("clock", "gate"),
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


# ----- Love pass 2026-09-22: spread / clock+division / manual_cv --------------

# The default render, pinned to literals computed from the code as it
# shipped (seeded noise, block 512). Every "OFF at its default" claim
# below leans on this: if a future change moves the defaults, this is
# the test that says so in one line instead of by ear.
_PIN_IDX = (0, 1, 511, 512, 2000, 4095)
_PIN_L = (0.0003700065135490149, 0.08971036970615387, -0.14920814335346222,
          -0.3966938257217407, -0.11950833350419998, -0.13438989222049713)
_PIN_R = (0.00029623162117786705, 0.0717831552028656, -0.08821968734264374,
          -0.2731110751628876, 0.13788281381130219, 0.2207154929637909)


def _noise(n=4096, seed=7, amp=0.4):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(n)).astype(np.float32)


def _rig_jacks(params=None, block=F, manual=False, clock=False):
    """A phaser with optional ``manual_cv`` / ``clock`` cables attached."""
    patch = Patch()
    src = patch.add_module("oscillator")
    ph = patch.add_module("phaser", params=params or {})
    patch.connect(src.id, "out", ph.id, "in")
    man = clk = None
    if manual:
        man = patch.add_module("lfo")
        patch.connect(man.id, "cv", ph.id, "manual_cv")
    if clock:
        clk = patch.add_module("clock")
        patch.connect(clk.id, "out", ph.id, "clock")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, ph, b, man, clk


def _run_jacks(rig, signal, manual_cv=None, gate=None, block=F):
    patch, src, ph, b, man, clk = rig
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        if man is not None:
            bufs[(man.id, "cv")] = manual_cv[..., sl].astype(np.float32)
        if clk is not None:
            bufs[(clk.id, "out")] = gate[sl].astype(np.float32)
        o = b._render_phaser(ph, block, bufs, patch)
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
        # Measured on a noise bed at depth 1: 1.000 / 0.698 / 0.675.
        sig = _noise(n=SR)
        c0 = _corr(*_plain({"spread": 0.0, "depth": 1.0}, sig))
        c5 = _corr(*_plain({"spread": 0.5, "depth": 1.0}, sig))
        c1 = _corr(*_plain({"spread": 1.0, "depth": 1.0}, sig))
        assert c0 == pytest.approx(1.0, abs=1e-6)
        assert 0.6 < c5 < 0.8
        assert 0.6 < c1 < 0.8
        assert c5 < c0 and c1 < c0

    def test_a_static_sweep_is_mono_whatever_spread_says(self):
        # depth 0 = no LFO, so there is no phase to offset. Worth pinning:
        # it is why phaser_envelope_sweep.json keeps a little depth.
        l, r = _plain({"spread": 1.0, "depth": 0.0})
        assert np.array_equal(l, r)


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
        # Measured, not asserted from the formula: a steady 800 Hz tone
        # through the sweeping notch is amplitude-modulated, and the notch
        # crosses the tone TWICE per LFO cycle, so the Hilbert envelope's
        # fundamental is 2x the sweep rate.
        from scipy.signal import hilbert

        n = SR * 12
        t = np.arange(n) / SR
        tone = (0.5 * np.sin(2 * np.pi * 800.0 * t)).astype(np.float32)
        period = SR // 2                          # a 2 Hz clock
        gate = _ticks(n, period)
        for div in (2.0, 4.0, 8.0):
            rig = _rig_jacks(
                {"rate": 0.05, "division": div, "depth": 1.0,
                 "feedback": 0.0, "mix": 0.5, "center": 800.0,
                 "spread": 0.0},
                clock=True,
            )
            l, _r = _run_jacks(rig, tone, gate=gate)
            env = np.abs(hilbert(l[SR * 2:].astype(np.float64)))
            env = env - env.mean()
            S = np.abs(np.fft.rfft(env))
            f = np.fft.rfftfreq(env.size, 1.0 / SR)
            band = (f > 0.05) & (f < 8.0)
            got = float(f[band][np.argmax(S[band])]) / 2.0
            want = 1.0 / (0.5 * div)              # ticks are 0.5 s apart
            assert got == pytest.approx(want, rel=0.02), (div, got, want)

    def test_a_locked_sweep_is_exactly_block_size_independent(self):
        # The point of keying the locked phase to the ABSOLUTE sample
        # index instead of accumulating it: identical bits at 64 and 512.
        n = 44032                       # a whole number of 512s AND of 64s
        sig = _noise(n=n, seed=3)
        gate = _ticks(n, 64)                      # locks at sample 64
        p = {"division": 32.0, "depth": 0.8, "feedback": 0.5,
             "spread": 0.7}
        a = _run_jacks(_rig_jacks(p, clock=True, block=512), sig,
                       gate=gate, block=512)
        b = _run_jacks(_rig_jacks(p, clock=True, block=64), sig,
                       gate=gate, block=64)
        assert np.array_equal(a[0], b[0])
        assert np.array_equal(a[1], b[1])

    def test_the_free_running_sweep_is_exact_too(self):
        # This used to be the honest counterpart -- the float phase
        # accumulator drifted ~1e-10 over a second at 64 vs 512 and the pin
        # was ``< 1e-6``. The free-running line now counts samples since
        # the last rate change (``_mod_free_phase``), so it is exact too.
        # Four seconds, and the float64 DSP state compared as well as the
        # float32 output: an ulp in the state rarely flips a float32
        # sample, so the output alone can pass a drift that is there.
        n = (4 * SR // 512) * 512       # a whole number of 512s AND 64s
        sig = _noise(n=n, seed=3)
        p = {"depth": 0.8, "feedback": 0.5}
        ra, rb = _rig(p, block=512), _rig(p, block=64)
        a = _run(*ra, sig, block=512)
        b = _run(*rb, sig, block=64)
        assert np.array_equal(a[0], b[0])
        assert np.array_equal(a[1], b[1])
        sa, sb = ra[3]._state[ra[2].id], rb[3]._state[rb[2].id]
        for k in ("s", "yprev"):
            assert np.array_equal(sa[k], sb[k]), k

    def test_a_faster_clock_sweeps_faster(self):
        n = SR * 4
        sig = _noise(n=n, seed=5)
        p = {"division": 4.0, "depth": 1.0, "spread": 0.0}
        slow = _run_jacks(_rig_jacks(p, clock=True), sig,
                          gate=_ticks(n, SR // 2))[0]
        fast = _run_jacks(_rig_jacks(p, clock=True), sig,
                          gate=_ticks(n, SR // 4))[0]
        assert not np.array_equal(slow, fast)

    def test_the_lock_survives_a_stages_change(self):
        # ``stages`` re-inits the DSP state; the measured period must
        # survive that clear or the sweep would fall back to the knob.
        n = SR
        sig = _noise(n=n, seed=9)
        gate = _ticks(n, 64)
        patch, src, ph, b, _m, clk = _rig_jacks(
            {"division": 32.0, "stages": 6}, clock=True
        )
        for k in range(n // F):
            if k == 3:
                ph.set_param("stages", 8)
            sl = slice(k * F, (k + 1) * F)
            b._render_phaser(ph, F, {(src.id, "out"): sig[sl],
                                     (clk.id, "out"): gate[sl]}, patch)
        assert b._state[ph.id]["interval"] == 64
        assert b._state[ph.id]["period"] == 64 * 32.0


class TestManualCv:
    def test_manual_depth_does_nothing_with_the_jack_unpatched(self):
        a = _plain({"manual_depth": 0.0})
        b = _plain({"manual_depth": 4.0})
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_an_octave_of_cv_moves_the_notch_pattern_an_octave(self):
        # Measured: the deepest notch lands at 1489 / 2943 / 5676 Hz for
        # cv -1 / 0 / +1 at depth 1 -- ratios 0.506 and 1.928.
        n = SR * 2
        sig = _noise(n=n, seed=11, amp=0.3)
        p = {"depth": 0.0, "feedback": 0.0, "mix": 0.5, "center": 800.0,
             "manual_depth": 1.0, "spread": 0.0}
        deep = {}
        for c in (-1.0, 0.0, 1.0):
            rig = _rig_jacks(p, manual=True)
            l, _r = _run_jacks(rig, sig, manual_cv=np.full(n, c, np.float32))
            tail = l[SR:].astype(np.float64)
            S = np.abs(np.fft.rfft(tail))
            f = np.fft.rfftfreq(tail.size, 1.0 / SR)
            S = np.convolve(S, np.ones(64) / 64, mode="same")
            band = (f > 150) & (f < 9000)
            deep[c] = float(f[band][np.argmin(S[band])])
        assert deep[-1.0] / deep[0.0] == pytest.approx(0.5, rel=0.05)
        assert deep[1.0] / deep[0.0] == pytest.approx(2.0, rel=0.05)

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
    p = {"spread": 0.83, "division": 24.0, "manual_depth": 1.7,
         "depth": 0.6, "feedback": 0.6, "mix": 0.55, "center": 700.0}
    a = _run_jacks(_rig_jacks(p, manual=True, clock=True, block=512), sig,
                   manual_cv=cv, gate=gate, block=512)
    b = _run_jacks(_rig_jacks(p, manual=True, clock=True, block=64), sig,
                   manual_cv=cv, gate=gate, block=64)
    assert np.array_equal(a[0], b[0])
    assert np.array_equal(a[1], b[1])


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch):
    from unittest import mock

    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("phaser")
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
    for name in Phaser.DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    # And the new three reached the PHASER's own branch, not somebody
    # else's same-named one ("matching a label is not matching the widget").
    assert w["division"][1] == "%.2f ticks"
    assert w["manual_depth"][1] == "%.2f oct/unit"
    assert w["spread"][0] == "add_slider_float"
    assert w["rate"][1] == "%.2f Hz"


# ----- example ----------------------------------------------------------------


def _render_example(name, tweak=None, seconds=6.0, block=F):
    from pathlib import Path

    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / name
    patch = load_patch(path)
    if tweak:
        tweak(next(m for m in patch if m.TYPE == "phaser"))
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    cap = []
    orig = b._render_phaser

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.append((r["out_l"].copy(), r["out_r"].copy()))
        return r

    b._render_phaser = spy
    peak = 0.0
    for _ in range(int(SR * seconds / block)):
        out, _devices = b.render_block_multi(block)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    return (peak,
            np.concatenate([c[0] for c in cap]),
            np.concatenate([c[1] for c in cap]))


def test_the_envelope_sweep_example_is_played_by_its_envelope():
    peak, l, r = _render_example("phaser_envelope_sweep.json")
    assert 0.3 < peak < 0.8
    # The envelope really is doing the sweeping: turning the jack's depth
    # to 0 (a fixed notch pattern -- the module before this pass) changes
    # the render by more than the signal's own rms.
    _p2, l0, _r0 = _render_example(
        "phaser_envelope_sweep.json",
        tweak=lambda m: m.set_param("manual_depth", 0.0),
    )
    d = float(np.sqrt(((l.astype(np.float64) - l0) ** 2).mean()))
    assert d > float(np.sqrt((l.astype(np.float64) ** 2).mean()))
    # And it is a real stereo pair, which ``spread`` 0 collapses.
    assert _corr(l, r) < 0.9
    _p3, l1, r1 = _render_example(
        "phaser_envelope_sweep.json",
        tweak=lambda m: m.set_param("spread", 0.0),
    )
    assert np.array_equal(l1, r1)
