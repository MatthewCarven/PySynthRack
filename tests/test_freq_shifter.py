"""Tests for the FreqShifter module (Bode single-sideband frequency shift).

Coverage:
  - Model: registration/defaults, ports & kinds (``in`` audio + ``shift_cv``
    cv -> ``out_up`` / ``out_down`` audio), category, JSON round-trip,
    unknown-param rejection, and the signal-kind type walls.
  - Bypass & neutral: disconnected -> silence on both outs; ``mix=0`` is a
    bit-exact dry passthrough on both outs (no latency, no state advance);
    frames=0 -> empty; output is float32.
  - DSP: at ``shift=0`` the wet *is* the input delayed by the Hilbert group
    delay (bit-exact, both outs); a sine at ``f0`` shifted by ``s`` gives a
    single peak at ``f0+s`` on ``out_up`` (opposite sideband > 40 dB down)
    and ``f0-s`` on ``out_down``; the desired sideband is ~unity gain; a
    negative ``shift`` swaps the sidebands.
  - Controls: ``shift_cv`` moves the shift by ``shift_cv_depth`` Hz/unit
    (and depth 0 disables it); ``mix`` blends the delayed dry against wet.
  - Feedback: bounded/finite up to the 0.9 clamp; feedback adds extra
    shifted copies (barberpole) vs the clean single shift.
  - Invariants: a single voice row is bit-identical to mono; voices are
    independent; (V, F) shape preserved; block-size independent (bit-exact
    with no feedback, to < 1e-6 with feedback); extremes finite.
  - Integration: osc -> freq_shifter -> L/R speakers renders finite audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend, _FS_LATENCY
from pysynthrack.core import Patch
from pysynthrack.modules.freq_shifter import FreqShifter

SR = 44100
F = 512


def _rig(params=None, block=F):
    """oscillator -> freq_shifter.in"""
    patch = Patch()
    src = patch.add_module("oscillator")
    fs = patch.add_module("freq_shifter", params=params or {})
    patch.connect(src.id, "out", fs.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, fs, b


def _rig_cv(params=None, block=F):
    """oscillator -> freq_shifter.in ; constant -> freq_shifter.shift_cv"""
    patch = Patch()
    src = patch.add_module("oscillator")
    cv = patch.add_module("constant")
    fs = patch.add_module("freq_shifter", params=params or {})
    patch.connect(src.id, "out", fs.id, "in")
    patch.connect(cv.id, "out", fs.id, "shift_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, cv, fs, b


def _run(b, patch, src, fs, signal, block=F):
    """Render ``signal`` through ``fs`` in ``block`` chunks -> (up, down)."""
    n = (signal.shape[-1] // block) * block
    ups, downs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        o = b._render_freq_shifter(fs, block, bufs, patch)
        ups.append(o["out_up"])
        downs.append(o["out_down"])
    axis = -1
    return np.concatenate(ups, axis=axis), np.concatenate(downs, axis=axis)


def _run_cv(b, patch, src, cvsrc, fs, signal, cv, block=F):
    n = (signal.shape[-1] // block) * block
    ups, downs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {
            (src.id, "out"): signal[..., sl].astype(np.float32),
            (cvsrc.id, "out"): cv[..., sl].astype(np.float32),
        }
        o = b._render_freq_shifter(fs, block, bufs, patch)
        ups.append(o["out_up"])
        downs.append(o["out_down"])
    return np.concatenate(ups, axis=-1), np.concatenate(downs, axis=-1)


def _sine(freq, amp=0.5, n=F * 40):
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _peak_db(sig, freq):
    """Windowed-FFT magnitude (dB) in a small band around ``freq``."""
    seg = sig[4 * _FS_LATENCY:]
    w = np.blackman(len(seg))
    X = np.abs(np.fft.rfft(seg * w))
    fr = np.fft.rfftfreq(len(seg), 1.0 / SR)
    k = int(np.argmin(np.abs(fr - freq)))
    band = X[max(0, k - 3):k + 4]
    return 20.0 * np.log10(band.max() + 1e-20)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        fs = Patch().add_module("freq_shifter")
        assert isinstance(fs, FreqShifter)
        assert fs.params == {
            "shift": 0.0,
            "shift_cv_depth": 200.0,
            "mix": 1.0,
            "feedback": 0.0,
        }

    def test_category_is_effects(self):
        assert FreqShifter.CATEGORY == "Effects"

    def test_ports_and_kinds(self):
        fs = Patch().add_module("freq_shifter")
        assert [(p.name, p.signal_kind) for p in fs.input_ports] == [
            ("in", "audio"),
            ("shift_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in fs.output_ports] == [
            ("out_up", "audio"),
            ("out_down", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "freq_shifter", params={"shift": 137.0, "mix": 0.4, "feedback": 0.5}
        )
        restored = Patch.from_dict(patch.to_dict())
        fs = next(m for m in restored if m.TYPE == "freq_shifter")
        assert fs.params["shift"] == 137.0
        assert fs.params["mix"] == 0.4
        assert fs.params["feedback"] == 0.5

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("freq_shifter", params={"depth": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        fs = patch.add_module("freq_shifter")
        patch.connect(osc.id, "out", fs.id, "in")  # no raise

    def test_cv_into_shift_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        fs = patch.add_module("freq_shifter")
        patch.connect(lfo.id, "cv", fs.id, "shift_cv")  # no raise

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        fs = patch.add_module("freq_shifter")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", fs.id, "in")

    def test_audio_into_shift_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        fs = patch.add_module("freq_shifter")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", fs.id, "shift_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        fs = patch.add_module("freq_shifter")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(fs.id, "out_up", vca.id, "cv")


# ----- Bypass & neutral ------------------------------------------------------


class TestBypass:
    def test_disconnected_is_silent(self):
        patch = Patch()
        fs = patch.add_module("freq_shifter")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_freq_shifter(fs, F, {}, patch)
        assert o["out_up"].shape == (F,) and o["out_down"].shape == (F,)
        assert not np.any(o["out_up"]) and not np.any(o["out_down"])

    def test_frames_zero_empty(self):
        patch, src, fs, b = _rig()
        o = b._render_freq_shifter(fs, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch)
        assert o["out_up"].shape == (0,) and o["out_down"].shape == (0,)

    def test_mix_zero_exact_dry_passthrough(self):
        x = _sine(330.0, 0.5, n=F * 4)
        patch, src, fs, b = _rig({"shift": 300.0, "mix": 0.0})
        up, down = _run(b, patch, src, fs, x)
        assert np.array_equal(up, x[: len(up)])
        assert np.array_equal(down, x[: len(down)])

    def test_output_is_float32(self):
        x = _sine(440.0, 0.5, n=F * 4)
        patch, src, fs, b = _rig({"shift": 120.0})
        up, down = _run(b, patch, src, fs, x)
        assert up.dtype == np.float32 and down.dtype == np.float32


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_shift_zero_is_delayed_dry(self):
        # White-ish noise so the equality is meaningful across the band.
        rng = np.random.RandomState(0)
        x = (rng.standard_normal(F * 6) * 0.3).astype(np.float32)
        patch, src, fs, b = _rig({"shift": 0.0, "mix": 1.0})
        up, down = _run(b, patch, src, fs, x)
        delayed = np.concatenate([np.zeros(_FS_LATENCY, np.float32), x])[: len(up)]
        assert np.array_equal(up, delayed)
        assert np.array_equal(down, delayed)

    def test_up_is_single_upper_sideband(self):
        f0, s = 1000.0, 220.0
        x = _sine(f0, 0.5)
        patch, src, fs, b = _rig({"shift": s, "mix": 1.0})
        up, _ = _run(b, patch, src, fs, x)
        want = _peak_db(up, f0 + s)      # desired
        image = _peak_db(up, f0 - s)     # rejected opposite sideband
        assert want - image > 40.0

    def test_down_is_single_lower_sideband(self):
        f0, s = 1000.0, 220.0
        x = _sine(f0, 0.5)
        patch, src, fs, b = _rig({"shift": s, "mix": 1.0})
        _, down = _run(b, patch, src, fs, x)
        want = _peak_db(down, f0 - s)
        image = _peak_db(down, f0 + s)
        assert want - image > 40.0

    def test_desired_sideband_is_near_unity_gain(self):
        f0, s = 1000.0, 220.0
        x = _sine(f0, 0.5)
        patch, src, fs, b = _rig({"shift": s, "mix": 1.0})
        up, _ = _run(b, patch, src, fs, x)
        # Shifted peak within a few dB of the (delayed) input's own peak.
        ref = _peak_db(np.concatenate([np.zeros(_FS_LATENCY, np.float32), x]), f0)
        assert abs(_peak_db(up, f0 + s) - ref) < 3.0

    def test_negative_shift_swaps_sidebands(self):
        f0, s = 1000.0, 300.0
        x = _sine(f0, 0.5)
        patch, src, fs, b = _rig({"shift": -s, "mix": 1.0})
        up, _ = _run(b, patch, src, fs, x)
        # With shift < 0, out_up's content lands at f0 - s.
        assert _peak_db(up, f0 - s) - _peak_db(up, f0 + s) > 40.0

    def test_mix_blends_dry_and_wet(self):
        f0, s = 1000.0, 220.0
        x = _sine(f0, 0.5)
        patch, src, fs, b = _rig({"shift": s, "mix": 0.5})
        up, _ = _run(b, patch, src, fs, x)
        # A half-mix keeps the original partial (delayed dry) AND the shift.
        dry = _peak_db(up, f0)
        wet = _peak_db(up, f0 + s)
        floor = _peak_db(up, f0 + 2 * s)   # nothing here
        assert dry - floor > 20.0 and wet - floor > 20.0


# ----- Controls: shift_cv ----------------------------------------------------


class TestShiftCV:
    def test_shift_cv_moves_the_shift(self):
        f0 = 1000.0
        x = _sine(f0, 0.5)
        # depth 200 Hz/unit, cv = 1.0 -> +200 Hz on top of shift 0.
        patch, src, cv, fs, b = _rig_cv({"shift": 0.0, "shift_cv_depth": 200.0})
        cvbuf = np.ones_like(x)
        up, _ = _run_cv(b, patch, src, cv, fs, x, cvbuf)
        assert _peak_db(up, f0 + 200.0) - _peak_db(up, f0 - 200.0) > 40.0

    def test_shift_cv_depth_zero_disables(self):
        f0 = 1000.0
        x = _sine(f0, 0.5)
        patch, src, cv, fs, b = _rig_cv({"shift": 0.0, "shift_cv_depth": 0.0})
        cvbuf = np.ones_like(x) * 5.0
        up, _ = _run_cv(b, patch, src, cv, fs, x, cvbuf)
        # depth 0 -> shift stays 0 -> out_up is the delayed dry (peak at f0).
        assert _peak_db(up, f0) - _peak_db(up, f0 + 100.0) > 40.0


# ----- Feedback --------------------------------------------------------------


class TestFeedback:
    def test_feedback_is_bounded_and_finite(self):
        x = _sine(440.0, 0.5, n=F * 40)
        for fb in (0.0, 0.5, 0.9):
            patch, src, fs, b = _rig({"shift": 150.0, "mix": 1.0, "feedback": fb})
            up, down = _run(b, patch, src, fs, x)
            assert np.all(np.isfinite(up)) and np.all(np.isfinite(down))
            assert np.abs(up).max() < 50.0

    def test_feedback_clamped_at_0p9(self):
        # feedback well above 1 would run away without the clamp.
        x = _sine(440.0, 0.5, n=F * 40)
        patch, src, fs, b = _rig({"shift": 150.0, "feedback": 5.0})
        up, _ = _run(b, patch, src, fs, x)
        assert np.all(np.isfinite(up)) and np.abs(up).max() < 50.0

    def test_feedback_adds_shifted_copies(self):
        f0, s = 700.0, 150.0
        x = _sine(f0, 0.4)
        p0, s0, f0m, b0 = _rig({"shift": s, "mix": 1.0, "feedback": 0.0})
        up0, _ = _run(b0, p0, s0, f0m, x)
        p1, s1, f1m, b1 = _rig({"shift": s, "mix": 1.0, "feedback": 0.8})
        up1, _ = _run(b1, p1, s1, f1m, x)
        # The second harmonic-copy at f0 + 2s is much stronger with feedback.
        assert _peak_db(up1, f0 + 2 * s) > _peak_db(up0, f0 + 2 * s) + 15.0


# ----- Invariants ------------------------------------------------------------


class TestInvariants:
    def test_single_voice_row_equals_mono(self):
        x = _sine(320.0, 0.4, n=F * 4)
        p0, s0, f0m, b0 = _rig({"shift": 200.0, "mix": 0.7, "feedback": 0.3})
        up_m, dn_m = _run(b0, p0, s0, f0m, x)
        p1, s1, f1m, b1 = _rig({"shift": 200.0, "mix": 0.7, "feedback": 0.3})
        up_v, dn_v = _run(b1, p1, s1, f1m, x[None, :])   # (1, F) rows
        assert np.array_equal(up_v[0], up_m)
        assert np.array_equal(dn_v[0], dn_m)

    def test_voices_are_independent(self):
        xa = _sine(300.0, 0.4, n=F * 4)
        xb = _sine(900.0, 0.3, n=F * 4)
        # two-voice render
        p, s, f, b = _rig({"shift": 175.0, "mix": 0.8})
        up2, _ = _run(b, p, s, f, np.stack([xa, xb]))
        # each voice rendered alone
        pa, sa, fa, ba = _rig({"shift": 175.0, "mix": 0.8})
        upa, _ = _run(ba, pa, sa, fa, xa)
        pb, sb, fb_, bb = _rig({"shift": 175.0, "mix": 0.8})
        upb, _ = _run(bb, pb, sb, fb_, xb)
        assert np.array_equal(up2[0], upa)
        assert np.array_equal(up2[1], upb)

    def test_voice_shape_preserved(self):
        x = np.stack([_sine(300.0, 0.4, n=F), _sine(500.0, 0.4, n=F)])
        patch, src, fs, b = _rig({"shift": 100.0})
        o = b._render_freq_shifter(fs, F, {(src.id, "out"): x.astype(np.float32)}, patch)
        assert o["out_up"].shape == (2, F) and o["out_down"].shape == (2, F)

    def test_block_size_independent_no_feedback(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(12000) / SR) * 0.4).astype(np.float32)
        params = {"shift": 175.0, "mix": 0.6, "feedback": 0.0}
        pa, sa, fa, ba = _rig(params, block=512)
        ua, da = _run(ba, pa, sa, fa, x, block=512)
        pb, sb, fbk, bb = _rig(params, block=4096)
        ub, db = _run(bb, pb, sb, fbk, x, block=4096)
        pc, sc, fc, bc = _rig(params, block=333)
        uc, dc = _run(bc, pc, sc, fc, x, block=333)
        m = min(len(ua), len(ub), len(uc))
        assert np.array_equal(ua[:m], ub[:m])
        assert np.array_equal(ua[:m], uc[:m])
        assert np.array_equal(da[:m], db[:m])

    def test_block_size_independent_with_feedback(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(12000) / SR) * 0.4).astype(np.float32)
        params = {"shift": 175.0, "mix": 1.0, "feedback": 0.6}
        pa, sa, fa, ba = _rig(params, block=512)
        ua, _ = _run(ba, pa, sa, fa, x, block=512)
        pb, sb, fbk, bb = _rig(params, block=4096)
        ub, _ = _run(bb, pb, sb, fbk, x, block=4096)
        pc, sc, fc, bc = _rig(params, block=333)
        uc, _ = _run(bc, pc, sc, fc, x, block=333)
        m = min(len(ua), len(ub), len(uc))
        assert np.allclose(ua[:m], ub[:m], atol=1e-6)
        assert np.allclose(ua[:m], uc[:m], atol=1e-6)

    def test_finite_at_extremes(self):
        x = _sine(440.0, 0.9, n=F * 4)
        for params in (
            {"shift": 2000.0, "mix": 1.0, "feedback": 0.9},
            {"shift": -2000.0, "mix": 0.5},
            {"shift": 5.0, "feedback": 0.9},
        ):
            patch, src, fs, b = _rig(params)
            up, down = _run(b, patch, src, fs, x)
            assert np.all(np.isfinite(up)) and np.all(np.isfinite(down))


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_freqshifter_stereo_speakers(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0})
        fs = patch.add_module("freq_shifter", params={"shift": 111.0, "mix": 1.0})
        spk_l = patch.add_module("left_speaker_output")
        spk_r = patch.add_module("right_speaker_output")
        patch.connect(osc.id, "out", fs.id, "in")
        patch.connect(fs.id, "out_up", spk_l.id, "in")
        patch.connect(fs.id, "out_down", spk_r.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(60):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
