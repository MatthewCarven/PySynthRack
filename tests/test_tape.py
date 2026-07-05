"""Tests for the Tape ("put it on tape": wow/flutter/drift, saturation,
hiss, head bump).

Coverage:
  - Model: registration, defaults, ports/kinds (audio ``in`` -> audio
    ``out``), JSON round-trip, unknown-param rejection, signal-kind walls.
  - DSP: disconnected -> silence; ``frames == 0`` -> empty; the neutral
    setting (all zero, hiss off) is a **bit-exact passthrough**; ``mix =
    0`` is bit-exact dry even with everything else driven; output is
    float32/finite/bounded; a voice (2D) input keeps its shape.
  - Character: ``wow`` produces measurable pitch deviation (windowed
    parabolic-peak tracker); saturation THD is monotone in ``sat``; hiss
    is a calibrated, reproducible noise floor; the head bump lifts the
    low end and leaves the top alone.
  - Block independence: the chunked, feedback-free engine (with its
    seeded 1:1 noise streams) is bit-identical at any block size.
  - Voice: a single voice row is bit-identical to mono; independent
    voices stay independent (no cross-talk through the shared motion).
  - Integration: osc -> tape -> speaker renders audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.tape import Tape

SR = 44100
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    tp = patch.add_module("tape", params=params or {})
    patch.connect(src.id, "out", tp.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, tp, b


def _run(b, patch, src, tp, signal, block=F):
    n = (signal.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        outs.append(b._render_tape(tp, block, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _tone(freq, secs=1.0, amp=0.5):
    t = np.arange(int(secs * SR))
    return (np.sin(2 * np.pi * freq * t / SR) * amp).astype(np.float32)


def _inst_freqs(y, win=1024, hop=512):
    """Dominant frequency per window, parabolically interpolated (sub-bin)."""
    w = np.hanning(win)
    out = []
    for s in range(0, len(y) - win, hop):
        sp = np.abs(np.fft.rfft(y[s:s + win] * w))
        k = int(sp.argmax())
        if 1 <= k < len(sp) - 1:
            a, b, c = sp[k - 1], sp[k], sp[k + 1]
            k = k + 0.5 * (a - c) / (a - 2 * b + c + 1e-20)
        out.append(k * SR / win)
    return np.array(out)


def _thd(y, f0=1000.0):
    seg = y[2000:2000 + SR // 2]
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    frq = np.fft.rfftfreq(len(seg), 1.0 / SR)
    kf = int(np.argmin(np.abs(frq - f0)))
    fund = sp[kf - 2:kf + 3].sum()
    harm = 0.0
    for h in range(2, 11):
        kh = int(np.argmin(np.abs(frq - f0 * h)))
        harm += sp[kh - 2:kh + 3].sum() ** 2
    return float(np.sqrt(harm) / (fund + 1e-20))


def _band_rms(y, lo, hi):
    sp = np.abs(np.fft.rfft(y))
    frq = np.fft.rfftfreq(len(y), 1.0 / SR)
    m = (frq >= lo) & (frq < hi)
    return float(np.sqrt(np.mean(sp[m] ** 2)))


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        tp = Patch().add_module("tape")
        assert isinstance(tp, Tape)
        assert tp.params == {
            "wow": 0.0,
            "flutter": 0.0,
            "drift": 0.0,
            "sat": 0.0,
            "hiss": -80.0,
            "bump": 0.0,
            "mix": 1.0,
        }

    def test_ports_and_kinds(self):
        tp = Patch().add_module("tape")
        assert [(p.name, p.signal_kind) for p in tp.input_ports] == [("in", "audio")]
        assert [(p.name, p.signal_kind) for p in tp.output_ports] == [("out", "audio")]

    def test_category(self):
        assert Patch().add_module("tape").CATEGORY == "Effects"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("tape", params={"wow": 0.4, "sat": 0.7, "hiss": -45.0})
        restored = Patch.from_dict(patch.to_dict())
        tp = next(m for m in restored if m.TYPE == "tape")
        assert tp.params["wow"] == 0.4
        assert tp.params["sat"] == 0.7
        assert tp.params["hiss"] == -45.0

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("tape", params={"feedback": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        tp = patch.add_module("tape")
        patch.connect(osc.id, "out", tp.id, "in")  # no raise

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        tp = patch.add_module("tape")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", tp.id, "in")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        tp = patch.add_module("tape")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(tp.id, "out", vca.id, "cv")


# ----- DSP: contract ---------------------------------------------------------


class TestContract:
    def test_disconnected_is_silent(self):
        patch = Patch()
        tp = patch.add_module("tape")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_tape(tp, F, {}, patch)
        assert o.shape == (F,) and not np.any(o)

    def test_frames_zero_empty(self):
        patch, src, tp, b = _rig({"sat": 0.5})
        o = b._render_tape(tp, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch)
        assert o.shape == (0,)

    def test_neutral_is_bit_exact_passthrough(self):
        # All zero + hiss off (the registered defaults) -> transparent.
        patch, src, tp, b = _rig({})
        x = (np.random.RandomState(0).randn(F * 4) * 0.4).astype(np.float32)
        out = _run(b, patch, src, tp, x)
        assert np.array_equal(out, x[: len(out)])

    def test_mix_zero_is_bit_exact_dry(self):
        # Everything driven hard, but mix=0 -> the dry input, untouched.
        patch, src, tp, b = _rig(
            {"wow": 0.8, "flutter": 0.6, "drift": 0.5, "sat": 0.7,
             "hiss": -40.0, "bump": 5.0, "mix": 0.0}
        )
        x = (np.random.RandomState(1).randn(F * 4) * 0.4).astype(np.float32)
        out = _run(b, patch, src, tp, x)
        assert np.array_equal(out, x[: len(out)])

    def test_output_is_float32(self):
        patch, src, tp, b = _rig({"sat": 0.5, "mix": 0.6})
        out = _run(b, patch, src, tp, np.random.randn(F * 2).astype(np.float32))
        assert out.dtype == np.float32

    def test_finite_and_bounded_at_extremes(self):
        patch, src, tp, b = _rig(
            {"wow": 1.0, "flutter": 1.0, "drift": 1.0, "sat": 1.0,
             "hiss": -30.0, "bump": 6.0, "mix": 1.0}
        )
        x = (np.random.RandomState(2).randn(2 * SR) * 0.5).astype(np.float32)
        out = _run(b, patch, src, tp, x)
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) < 8.0

    def test_voice_input_keeps_shape(self):
        patch, src, tp, b = _rig({"sat": 0.5, "mix": 1.0})
        v = (np.random.RandomState(4).randn(3, F) * 0.3).astype(np.float32)
        o = b._render_tape(tp, F, {(src.id, "out"): v}, patch)
        assert o.shape == (3, F) and np.all(np.isfinite(o))


# ----- Character -------------------------------------------------------------


class TestCharacter:
    def test_wow_produces_pitch_deviation(self):
        tone = _tone(2000.0, secs=1.0)
        pw, sw, tw, bw = _rig({"wow": 1.0, "mix": 1.0})
        y_wow = _run(bw, pw, sw, tw, tone)
        # A driven-but-unmodulated render is the control: sat colours the
        # tone but must not wander its pitch.
        pf, sf, tf, bf = _rig({"wow": 0.0, "sat": 0.3, "mix": 1.0})
        y_flat = _run(bf, pf, sf, tf, tone)
        spread_wow = np.ptp(_inst_freqs(y_wow[2000:]))
        spread_flat = np.ptp(_inst_freqs(y_flat[2000:]))
        assert spread_wow > 15.0      # ~30 Hz peak dev on 2 kHz, seen as a swing
        assert spread_flat < 3.0

    def test_saturation_thd_monotone_in_sat(self):
        tone = _tone(1000.0, secs=1.0)
        vals = []
        for s in (0.2, 0.5, 0.9):
            p, sr_, t, b = _rig({"sat": s, "mix": 1.0})
            vals.append(_thd(_run(b, p, sr_, t, tone)))
        assert vals[0] < vals[1] < vals[2]
        assert vals[0] > 0.0

    def test_hiss_level_is_calibrated(self):
        sil = np.zeros(SR, dtype=np.float32)
        for hdb in (-30.0, -40.0, -50.0):
            p, s, t, b = _rig({"hiss": hdb, "mix": 1.0})
            y = _run(b, p, s, t, sil)
            rms = float(np.sqrt(np.mean(y[1000:] ** 2)))
            assert rms == pytest.approx(10 ** (hdb / 20.0), rel=0.1)

    def test_hiss_off_by_default_is_silent_on_silence(self):
        # Default hiss (-80, off) on a silent input adds nothing.
        p, s, t, b = _rig({"hiss": -80.0, "mix": 1.0})
        y = _run(b, p, s, t, np.zeros(F * 4, dtype=np.float32))
        assert not np.any(y)

    def test_hiss_is_reproducible(self):
        sil = np.zeros(SR // 2, dtype=np.float32)
        p1, s1, t1, b1 = _rig({"hiss": -35.0, "mix": 1.0})
        p2, s2, t2, b2 = _rig({"hiss": -35.0, "mix": 1.0})
        assert np.array_equal(
            _run(b1, p1, s1, t1, sil), _run(b2, p2, s2, t2, sil)
        )

    def test_head_bump_lifts_lows_not_highs(self):
        noise = (np.random.RandomState(3).randn(SR) * 0.2).astype(np.float32)
        p0, s0, t0, b0 = _rig({"bump": 0.0, "mix": 1.0})
        pb, sb, tb, bb = _rig({"bump": 6.0, "mix": 1.0})
        y0 = _run(b0, p0, s0, t0, noise)
        yb = _run(bb, pb, sb, tb, noise)
        low_gain = 20 * np.log10(_band_rms(yb[500:], 30, 90)
                                 / _band_rms(y0[500:], 30, 90))
        high_gain = 20 * np.log10(_band_rms(yb[500:], 2000, 8000)
                                  / _band_rms(y0[500:], 2000, 8000))
        assert low_gain > 2.0          # clear low-end lift
        assert abs(high_gain) < 0.5    # top end untouched


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def test_output_independent_of_block_size(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(20000) / SR) * 0.4).astype(np.float32)
        params = {"wow": 0.6, "flutter": 0.4, "drift": 0.3, "sat": 0.5,
                  "hiss": -42.0, "bump": 3.0, "mix": 0.6}
        pa, sa, ta, ba = _rig(params, block=512)
        a = _run(ba, pa, sa, ta, x, block=512)
        pb, sb, tb, bb = _rig(params, block=4096)
        bb_out = _run(bb, pb, sb, tb, x, block=4096)
        pc, sc, tc, bc = _rig(params, block=333)
        c = _run(bc, pc, sc, tc, x, block=333)
        m = min(len(a), len(bb_out), len(c))
        assert np.array_equal(a[:m], bb_out[:m])
        assert np.array_equal(a[:m], c[:m])


# ----- Voice -----------------------------------------------------------------


class TestVoice:
    _P = {"wow": 0.5, "flutter": 0.4, "drift": 0.3, "sat": 0.5,
          "hiss": -40.0, "bump": 3.0, "mix": 0.7}

    def test_single_voice_row_bit_identical_to_mono(self):
        x = (np.random.RandomState(5).randn(F) * 0.3).astype(np.float32)
        pm, sm, tm, bm = _rig(self._P)
        o_mono = bm._render_tape(tm, F, {(sm.id, "out"): x}, pm)
        pv, sv, tv, bv = _rig(self._P)
        o_voice = bv._render_tape(tv, F, {(sv.id, "out"): x[None, :].copy()}, pv)
        assert o_mono.shape == (F,) and o_voice.shape == (1, F)
        assert np.array_equal(o_voice[0], o_mono)

    def test_voices_are_independent(self):
        # Two different signals in two rows: each row must equal that same
        # signal rendered alone (no cross-talk through the shared motion).
        x0 = (np.random.RandomState(6).randn(F * 4) * 0.3).astype(np.float32)
        x1 = (np.random.RandomState(7).randn(F * 4) * 0.3).astype(np.float32)
        pv, sv, tv, bv = _rig(self._P)
        stacked = np.stack([x0, x1])
        rows = []
        for k in range(4):
            sl = slice(k * F, (k + 1) * F)
            rows.append(bv._render_tape(tv, F, {(sv.id, "out"): stacked[:, sl]}, pv))
        row = np.concatenate(rows, axis=-1)
        p0, s0, t0, b0 = _rig(self._P)
        solo0 = _run(b0, p0, s0, t0, x0)
        assert np.array_equal(row[0], solo0[: row.shape[1]])


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_tape_speaker(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 110.0})
        tp = patch.add_module(
            "tape", params={"wow": 0.4, "flutter": 0.3, "sat": 0.5,
                            "hiss": -50.0, "bump": 3.0, "mix": 0.6}
        )
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", tp.id, "in")
        patch.connect(tp.id, "out", spk.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(40):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
