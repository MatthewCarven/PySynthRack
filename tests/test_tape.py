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
  - The tape-stop (``stop`` gate, ``stop_time`` / ``start_time``): the
    pitch dives linearly to a halt (Hilbert instantaneous frequency at a
    quarter, half, three quarters of the ramp), exact silence while
    stopped, the climb back, the level following the speed with no click
    (largest sample step over a cycle == the signal's own), the lag after
    a cycle == ``start_time / 2`` and not accumulating over three cycles
    (cross-correlation), a mid-ramp release re-articulating from the
    current speed and keeping the lag, patched-but-never-risen bit-exact
    with unpatched (neutral AND modulated), the neutral passthrough while
    the ring records, block-size independence with edges mid-stream, the
    voice gate collapsing to any-voice-high with per-voice rings, the
    ring allocated longer only when patched, bounded partial stops, the
    widget sweep, the example.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from scipy.signal import correlate, hilbert

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
            "stop_time": 1.0,
            "start_time": 0.5,
        }

    def test_ports_and_kinds(self):
        tp = Patch().add_module("tape")
        assert [(p.name, p.signal_kind) for p in tp.input_ports] == [
            ("in", "audio"), ("stop", "gate")]
        assert [(p.name, p.signal_kind) for p in tp.output_ports] == [("out", "audio")]

    def test_gate_into_stop_accepted_cv_and_audio_rejected(self):
        patch = Patch()
        clk = patch.add_module("clock")
        lfo = patch.add_module("lfo")
        osc = patch.add_module("oscillator")
        tp = patch.add_module("tape")
        patch.connect(clk.id, "out", tp.id, "stop")  # no raise
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", tp.id, "stop")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", tp.id, "stop")

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
    _ALL = {"wow": 0.6, "flutter": 0.4, "drift": 0.3, "sat": 0.5,
            "hiss": -42.0, "bump": 3.0, "mix": 0.6}

    def _render(self, params, x, block):
        p, s, t, b = _rig(params, block=block)
        return _run(b, p, s, t, x, block=block)

    def test_output_independent_of_block_size(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(20000) / SR) * 0.4).astype(np.float32)
        a = self._render(self._ALL, x, 512)
        for block in (4096, 333):
            y = self._render(self._ALL, x, block)
            m = min(len(a), len(y))
            assert np.array_equal(a[:m], y[:m]), f"block {block}"

    def test_four_seconds_with_everything_on_is_bit_exact(self):
        # FOUR SECONDS of noise -- long enough for the ring to wrap
        # hundreds of times and for a carried float phase to drift --
        # with every flavour on, at block sizes sharing no alignment.
        # Bit for bit. Three separate mechanisms had to be fixed to get
        # here (measured 2026-09-22, against the 512 render):
        #   * the modulated read formed ``absidx - delay``, and a ring
        #     index rounds at its OWN magnitude -- the ring is ``max_ms +
        #     frames`` long, so it wraps at a different absolute sample
        #     per block size. It now splits the delay into whole samples
        #     + a fraction, both functions of the delay alone. (This was
        #     the whole of the drift path's 95 / 90 / 78 differences.)
        #   * the wow/flutter sines carried a float phase: ``ph + frames
        #     * inc`` rounds once per block, so partitions drift apart
        #     within a second. They now read an absolute sample INDEX.
        #     (54 / 94 / 25 differences on wow alone.)
        #   * the 4x oversampler's FIR carried an ``lfilter`` ``zi``, and
        #     scipy short-circuits ``len(a) == 1`` to ``np.convolve`` +
        #     ``+= zi``, splitting each 65-term sum at the boundary. It
        #     now carries raw tail samples. (2 / 5 / 4 differences on
        #     ``sat`` feeding ``bump``.)
        # The whole chain was 53 / 99 / 26 differing samples at 64 / 128
        # / 1000, max 6e-8. Now zero.
        x = (np.random.default_rng(7).standard_normal(4 * SR) * 0.3).astype(np.float32)
        a = self._render(self._ALL, x, 512)
        for block in (64, 128, 1000, 4096, 333):
            y = self._render(self._ALL, x, block)
            m = min(len(a), len(y))
            assert np.array_equal(a[:m], y[:m]), f"block {block}"

    @pytest.mark.parametrize("params", [
        {"wow": 0.6, "mix": 1.0},               # the wow sine's phase
        {"flutter": 0.4, "mix": 1.0},           # the flutter sine + its noise
        {"drift": 0.3, "mix": 1.0},             # the ring index alone
        {"sat": 0.5, "bump": 3.0, "mix": 1.0},  # the oversampler's FIR
    ])
    def test_each_flavour_alone_is_bit_exact_over_four_seconds(self, params):
        # One flavour at a time: each of the three mechanisms above shows
        # up in a different one, so a regression in any of them fails
        # here with the cause already named.
        x = (np.random.default_rng(7).standard_normal(4 * SR) * 0.3).astype(np.float32)
        a = self._render(params, x, 512)
        for block in (64, 128, 1000):
            y = self._render(params, x, block)
            m = min(len(a), len(y))
            assert np.array_equal(a[:m], y[:m]), f"block {block}"

    def test_oversampler_fir_is_block_size_exact(self):
        # The primitive, direct: ``_Oversampler4`` is shared with
        # distortion and waveshaper, so this pins the FIR's streaming
        # carry for all three. scipy's ``lfilter`` with ``len(a) == 1``
        # takes an ``np.convolve`` + ``+= zi`` shortcut that associates
        # the additions differently per partition (~1e-15); the class
        # prepends the raw tail instead, which gives every output one
        # whole, unsplit window.
        from pysynthrack.audio.numpy_backend import _Oversampler4

        x = np.random.default_rng(11).standard_normal((2, 4 * SR))

        def chain(block):
            os4 = _Oversampler4(2)
            out, p = [], 0
            while p + block <= x.shape[-1]:
                u = os4.up(x[:, p:p + block] * 0.3)
                out.append(os4.down(np.tanh(3.0 * u)))
                p += block
            return np.concatenate(out, axis=-1)

        ref = chain(512)
        for block in (64, 128, 1000):
            y = chain(block)
            m = min(ref.shape[-1], y.shape[-1])
            # float64, below the float32 cast: no slack at all.
            assert np.array_equal(ref[:, :m], y[:, :m]), f"block {block}"


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


# ----- The tape-stop ---------------------------------------------------------

STOP_P = {"stop_time": 1.0, "start_time": 0.5, "mix": 1.0}
STOP_N = SR                      # stop_time 1.0 s
START_N = SR // 2                # start_time 0.5 s
T_ON = SR + 37                   # the rising edge: mid-block at 64 and at 512
T_OFF = int(2.5 * SR) + 37       # the falling edge, 0.5 s into the halt
F0 = 440.0


def _stop_rig(params=None, block=F, patched=True):
    patch = Patch()
    src = patch.add_module("oscillator")
    clk = patch.add_module("clock")
    tp = patch.add_module("tape", params=dict(STOP_P if params is None else params))
    patch.connect(src.id, "out", tp.id, "in")
    if patched:
        patch.connect(clk.id, "out", tp.id, "stop")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, clk, tp, b


def _stop_run(rig, x, g, block=F):
    """Drive the renderer block by block with an audio row and a gate row
    (``g`` None = the stop buffer is never published)."""
    patch, src, clk, tp, b = rig
    n = (x.shape[-1] // block) * block
    ys = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): x[..., sl].astype(np.float32)}
        if g is not None:
            bufs[(clk.id, "out")] = g[..., sl].astype(np.float32)
        ys.append(b._render_tape(tp, block, bufs, patch))
    return np.concatenate(ys, axis=-1)


def _sine(n, freq=F0, level=0.5):
    return (level * np.sin(2.0 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


def _noise(n, level=0.3, seed=3):
    return (level * np.random.default_rng(seed).standard_normal(n)).astype(np.float32)


def _gate(n, start, stop):
    g = np.zeros(n, dtype=np.float32)
    g[start:stop] = 1.0
    return g


def _inst_freq(y):
    """Instantaneous frequency (Hz) per sample via the analytic signal."""
    ph = np.unwrap(np.angle(hilbert(np.asarray(y, dtype=np.float64))))
    return np.diff(ph) * SR / (2.0 * np.pi)


def _median_freq(fi, at, half=200):
    return float(np.median(fi[at - half:at + half]))


def _rms_db(y):
    r = float(np.sqrt(np.mean(np.asarray(y, dtype=np.float64) ** 2)))
    return 20.0 * np.log10(max(r, 1e-12))


def _lag_of(y, x, t0, width=SR // 2, max_lag=40000):
    """The lag (samples) at which ``y[t0:t0+width]`` best matches ``x``
    read earlier, by cross-correlation over lags 0..max_lag."""
    a = np.asarray(y[t0:t0 + width], dtype=np.float64)
    win = np.asarray(x[t0 - max_lag:t0 + width], dtype=np.float64)
    c = correlate(win, a, mode="valid", method="fft")      # offsets 0..max_lag
    return max_lag - int(np.argmax(c))


class TestStop:
    def _cycle(self, params=None, n=4 * SR, x=None):
        x = _sine(n) if x is None else x
        return x, _stop_run(_stop_rig(params), x, _gate(n, T_ON, T_OFF))

    def test_pitch_dives_linearly_over_stop_time(self):
        # A coasting motor: the speed ramps 1 -> 0 linearly over stop_time,
        # so the pitch of a steady sine is (1 - t/stop_time) * f0 -- read at
        # a quarter, a half and three quarters of the ramp, within 10%.
        x, y = self._cycle()
        fi = _inst_freq(y)
        for frac in (0.25, 0.5, 0.75):
            got = _median_freq(fi, T_ON + int(frac * STOP_N))
            assert got == pytest.approx(F0 * (1.0 - frac), rel=0.1), (frac, got)

    def test_silent_while_stopped(self):
        # The head is stationary from T_ON + stop_time until the gate falls:
        # its EMF is zero, so the wet is exact silence, not merely quiet.
        x, y = self._cycle()
        held = y[T_ON + STOP_N + 100:T_OFF - 100]
        assert _rms_db(held) < -60.0
        assert not np.any(held)

    def test_pitch_climbs_over_start_time(self):
        x, y = self._cycle()
        fi = _inst_freq(y)
        for frac in (0.25, 0.5, 0.75):
            got = _median_freq(fi, T_OFF + int(frac * START_N))
            assert got == pytest.approx(F0 * frac, rel=0.1), (frac, got)
        # and back on pitch once the spin-up is done
        assert _median_freq(fi, T_OFF + START_N + 2000) == pytest.approx(F0, rel=0.01)

    def test_level_follows_the_speed_and_there_is_no_click(self):
        # A magnetic head's EMF is proportional to tape speed: the wet
        # amplitude at a quarter, half, three quarters of the stop is 0.75,
        # 0.5, 0.25 of the sine's (windowed RMS * sqrt 2). And the ramps are
        # per sample, so the largest sample-to-sample step over the whole
        # cycle -- dive, halt, restart -- is no larger than the sine's own.
        x, y = self._cycle()
        for frac in (0.25, 0.5, 0.75):
            at = T_ON + int(frac * STOP_N)
            amp = np.sqrt(2.0) * np.sqrt(np.mean(y[at - 500:at + 500].astype(np.float64) ** 2))
            assert amp / 0.5 == pytest.approx(1.0 - frac, abs=0.02), (frac, amp)
        for frac in (0.5,):
            at = T_OFF + int(frac * START_N)
            amp = np.sqrt(2.0) * np.sqrt(np.mean(y[at - 500:at + 500].astype(np.float64) ** 2))
            assert amp / 0.5 == pytest.approx(frac, abs=0.02)
        step = np.abs(np.diff(y.astype(np.float64)))
        own = float(np.abs(np.diff(x[:len(y)].astype(np.float64))).max())
        assert float(step.max()) <= own * (1.0 + 1e-9)
        # the restart's first samples are near-silent: no jump at the edge
        assert float(step[T_OFF - 8:T_OFF + 8].max()) < 1e-3

    def test_lag_after_a_cycle_is_half_start_time_and_does_not_accumulate(self):
        # The head falls behind live by the integral of (1 - speed). The
        # lag resets to 0 at the restart (the head is silent there), grows
        # to (start_n - 1) / 2 during the spin-up and stays: one, two and
        # three cycles later the wet is the same ~11025 samples late.
        n = (12 * SR // F) * F
        g = np.zeros(n, np.float32)
        for c in range(3):
            g[c * 4 * SR + T_ON:c * 4 * SR + T_OFF] = 1.0
        x = _noise(n)
        rig = _stop_rig()
        y = _stop_run(rig, x, g)
        expect = (START_N - 1) / 2.0                          # 11024.5
        for c in range(3):
            lag = _lag_of(y, x, c * 4 * SR + int(3.0 * SR))
            assert abs(lag - expect) <= 0.5, (c, lag)
        # the carried lag is the exact integral, not a re-measurement
        assert rig[4]._state[rig[3].id]["stop_lag"] == pytest.approx(expect, abs=1e-6)

    def test_mid_ramp_release_re_articulates_from_the_current_speed(self):
        # Release at 40% of the stop: the speed is 0.6 and the restart ramps
        # up from THERE (the _gate_ramp_env contract) -- the pitch just
        # after the edge equals the pitch just before, at the midpoint of
        # the release it is 1 - 0.4 * 0.5 = 0.8 of f0, and the lag is NOT
        # reset (the head was still audible): it keeps growing.
        n = 4 * SR
        x = _sine(n)
        t_rel = T_ON + int(0.4 * STOP_N)
        rig = _stop_rig()
        patch, src, clk, tp, b = rig
        lags = []
        y = []
        g = _gate(n, T_ON, t_rel)
        for k in range(n // F):
            sl = slice(k * F, (k + 1) * F)
            y.append(b._render_tape(tp, F, {(src.id, "out"): x[sl], (clk.id, "out"): g[sl]}, patch))
            lags.append(float(b._state[tp.id]["stop_lag"]))
        y = np.concatenate(y)
        fi = _inst_freq(y)
        before = _median_freq(fi, t_rel - 300, 100)
        after = _median_freq(fi, t_rel + 300, 100)
        assert before == pytest.approx(F0 * 0.6, rel=0.05)
        assert after == pytest.approx(before, rel=0.03)
        assert _median_freq(fi, t_rel + START_N // 2) == pytest.approx(F0 * 0.8, rel=0.05)
        assert _median_freq(fi, t_rel + START_N + 2000) == pytest.approx(F0, rel=0.01)
        k_rel = t_rel // F
        assert lags[k_rel + 1] > lags[k_rel - 1] > 0.0        # kept and growing
        assert lags[-1] == pytest.approx(lags[k_rel + 50], abs=1e-6)  # then settled
        # the integral: the stop's 0.4 s of ramp plus the release from 0.4
        expect = 0.4 * STOP_N * (0.4 * STOP_N + 1) / 2.0 / STOP_N + 0.4 * (START_N - 1) / 2.0
        assert lags[-1] == pytest.approx(expect, abs=1e-6)

    @pytest.mark.parametrize("params", [
        {},                                                        # neutral
        {"wow": 0.5, "flutter": 0.35, "drift": 0.3, "sat": 0.45,
         "hiss": -48.0, "bump": 3.5, "mix": 0.7},                  # modulated
    ])
    def test_never_rising_stop_is_bit_exact_with_no_cable(self, params):
        # The ship-off pin: a stop cable that never rises == no cable,
        # neutral and modulated, sample for sample.
        n = 3 * SR
        x = _noise(n) + _sine(n, 330.0, 0.1)
        yu = _stop_run(_stop_rig(params, patched=False), x, None)
        yl = _stop_run(_stop_rig(params), x, np.zeros(n, np.float32))
        assert np.array_equal(yu, yl)
        assert np.array_equal(_stop_run(_stop_rig(params), x, np.zeros(n, np.float32)), yl)

    def test_neutral_with_stop_patched_is_passthrough_until_the_first_stop(self):
        # No nominal delay, no clip floor: the head reads the sample it just
        # wrote and the block is ``src`` bit for bit -- while the ring
        # records, so the stop that comes later plays material from
        # BEFORE the edge. After the cycle the wet is the input, late.
        n = 4 * SR
        x = _noise(n)
        rig = _stop_rig({"stop_time": 1.0, "start_time": 0.5, "mix": 1.0})
        y = _stop_run(rig, x, _gate(n, T_ON, T_OFF))
        assert np.array_equal(y[:T_ON], x[:T_ON])
        assert rig[4]._state[rig[3].id]["comp"] == 0
        # during the dive the head plays what was recorded before the edge
        dive = y[T_ON + 1000:T_ON + 3000].astype(np.float64)
        assert np.corrcoef(dive, x[T_ON + 1000:T_ON + 3000])[0, 1] < 0.9
        assert float(np.abs(dive).max()) > 0.1
        # after: the input at the lag, no longer the passthrough
        t0 = int(3.2 * SR)
        assert abs(_lag_of(y, x, t0) - (START_N - 1) / 2.0) <= 0.5
        assert not np.array_equal(y[t0:t0 + F], x[t0:t0 + F])
        # mix < 1: the dry is live, the wet is late -> both present
        rig2 = _stop_rig({"stop_time": 1.0, "start_time": 0.5, "mix": 0.5})
        y2 = _stop_run(rig2, x, _gate(n, T_ON, T_OFF))
        assert np.array_equal(y2[:T_ON], x[:T_ON])
        seg = y2[t0:t0 + SR // 2].astype(np.float64)
        assert np.corrcoef(seg, x[t0:t0 + SR // 2])[0, 1] > 0.5
        assert abs(_lag_of(y2 - 0.5 * x[:len(y2)], x, t0) - (START_N - 1) / 2.0) <= 0.5

    @pytest.mark.parametrize("params", [
        {"stop_time": 1.0, "start_time": 0.5, "mix": 1.0},
        {"stop_time": 0.7, "start_time": 0.3, "sat": 0.4, "hiss": -45.0, "mix": 0.7},
        {"stop_time": 0.7, "start_time": 0.3, "hiss": -45.0, "bump": 3.0, "mix": 0.7},
        # These three USED to be exact only to a float32 ulp at the odd
        # sample, because of the two paths the stop's own read had always
        # got right and the ordinary read had not -- see
        # ``test_four_seconds_with_everything_on_is_bit_exact``. They are
        # exact now, so they belong here.
        {"stop_time": 1.0, "start_time": 0.5, "wow": 0.4, "mix": 1.0},
        {"stop_time": 1.0, "start_time": 0.5, "sat": 0.4, "bump": 3.0, "mix": 1.0},
        {"stop_time": 1.0, "start_time": 0.5, "wow": 0.5, "flutter": 0.35,
         "drift": 0.3, "sat": 0.45, "hiss": -48.0, "bump": 3.5, "mix": 0.7},
    ])
    def test_block_size_independent_with_edges_mid_stream(self, params):
        # 64 / 128 / 512 / 1000 over a shared length, both edges mid-block
        # for all of them: the ramps are integer counts from the edges,
        # the lag a running sum carried by prepending it to each block's
        # cumsum, and the read applies the lag as whole samples + a
        # fraction separately (a ring index rounds at its own magnitude,
        # and ``rp - lag`` would round at that magnitude). Bit for bit --
        # the ordinary read now does the same, so the whole chain is.
        n = 4 * SR
        x = _noise(n)
        g = _gate(n, T_ON, T_OFF)
        assert T_ON % 64 and T_ON % 512 and T_OFF % 64 and T_OFF % 512
        ref = _stop_run(_stop_rig(params, block=512), x, g, block=512)
        for block in (64, 128, 1000):
            y = _stop_run(_stop_rig(params, block=block), x, g, block=block)
            m = min(len(y), len(ref))
            assert np.array_equal(ref[:m], y[:m]), f"block {block}"

    def test_three_stop_cycles_stay_bit_exact_across_block_sizes(self):
        # The lag only resets at a halt, so repeated cycles are where a
        # per-block rounding would compound. Six seconds, three cycles,
        # every flavour on.
        n = 6 * SR
        x = _noise(n)
        g = np.zeros(n, np.float32)
        for k in range(3):
            g[int((0.6 + 1.8 * k) * SR) + 17:int((1.4 + 1.8 * k) * SR) + 53] = 1.0
        params = {"wow": 0.5, "flutter": 0.4, "drift": 0.3, "sat": 0.45,
                  "hiss": -48.0, "bump": 3.5, "mix": 0.7,
                  "stop_time": 1.0, "start_time": 0.5}
        ref = _stop_run(_stop_rig(params, block=512), x, g, block=512)
        for block in (64, 128, 1000):
            y = _stop_run(_stop_rig(params, block=block), x, g, block=block)
            m = min(len(y), len(ref))
            assert np.array_equal(ref[:m], y[:m]), f"block {block}"

    def test_voice_gate_collapses_to_any_voice_high_and_rings_are_per_voice(self):
        # A (V, F) gate goes through the house sum: one voice high is the
        # mono all-high gate, and the one transport stops every voice's
        # ring. Each row is that signal alone through the same gate: the
        # rings never cross-talk.
        V = 3
        n = 3 * SR
        x = np.stack([_noise(n, seed=s) for s in range(V)])
        g = _gate(n, T_ON, int(2.2 * SR))
        gv = np.zeros((4, n), np.float32)
        gv[2] = g
        yv = _stop_run(_stop_rig(), x, gv)
        ym = _stop_run(_stop_rig(), x, g)
        assert yv.shape == (V, (n // F) * F)
        assert np.array_equal(yv, ym)
        for k in range(V):
            solo = _stop_run(_stop_rig(), x[k], g)
            assert np.array_equal(yv[k], solo)
        # all voices low: never stopped
        rig = _stop_rig()
        _stop_run(rig, x[:, :4 * F], np.zeros((4, 4 * F), np.float32))
        st = rig[4]._state[rig[3].id]
        assert st["stop_prev"] is False and st["stop_lag"] == 0.0

    def test_ring_is_allocated_longer_only_when_stop_is_patched(self):
        # Unpatched keeps today's ring; patched adds ceil((stop_n +
        # start_n) / 2) + 2; raising the times reallocates larger, lowering
        # them keeps the ring (grown, never shrunk).
        n = 4 * F
        x = _noise(n)
        L_today = int(NumpyBackend._TAPE_MAX_MS * SR / 1000.0) + F + 4
        rig = _stop_rig({"sat": 0.3}, patched=False)
        _stop_run(rig, x, None)
        assert rig[4]._state[rig[3].id]["buf"].shape == (1, L_today)
        rig = _stop_rig({"sat": 0.3, "stop_time": 1.0, "start_time": 0.5})
        _stop_run(rig, x, np.zeros(n, np.float32))
        cap = int(np.ceil((SR + SR // 2) / 2.0))
        assert rig[4]._state[rig[3].id]["buf"].shape == (1, L_today + cap + 2)
        rig[3].params["stop_time"] = 3.0
        _stop_run(rig, x, np.zeros(n, np.float32))
        cap3 = int(np.ceil((3 * SR + SR // 2) / 2.0))
        assert rig[4]._state[rig[3].id]["buf"].shape == (1, L_today + cap3 + 2)
        rig[3].params["stop_time"] = 0.2
        _stop_run(rig, x, np.zeros(n, np.float32))
        assert rig[4]._state[rig[3].id]["buf"].shape == (1, L_today + cap3 + 2)
        # the 8 s caps bound the allocation
        rig = _stop_rig({"stop_time": 99.0, "start_time": 99.0})
        _stop_run(rig, x, np.zeros(n, np.float32))
        assert rig[4]._state[rig[3].id]["buf"].shape[1] == L_today + 8 * SR + 2

    def test_repeated_partial_stops_are_bounded_and_finite(self):
        # Releases before the halt keep the lag (the head is audible), so
        # it accumulates -- up to the ring's capacity, where the head rides
        # the ring's tail. Ten partial stops: finite, bounded, and never a
        # read ahead of the write.
        stop_n, start_n = int(0.2 * SR), int(0.1 * SR)
        params = {"stop_time": 0.2, "start_time": 0.1, "mix": 1.0}
        n = (6 * SR // F) * F
        g = np.zeros(n, np.float32)
        for c in range(10):
            s0 = SR // 2 + c * int(0.4 * SR)
            g[s0:s0 + int(0.9 * stop_n)] = 1.0
        x = _noise(n)
        r = _stop_rig(params)
        y = _stop_run(r, x, g)
        assert np.all(np.isfinite(y))
        assert float(np.abs(y).max()) <= float(np.abs(x).max()) * 1.001
        cap = np.ceil((stop_n + start_n) / 2.0)
        assert 0.0 < r[4]._state[r[3].id]["stop_lag"]
        # a full stop afterwards resets it below the cap again
        g2 = g.copy()
        g2[int(5.0 * SR):int(5.5 * SR)] = 1.0
        r2 = _stop_rig(params)
        _stop_run(r2, x, g2)
        assert r2[4]._state[r2[3].id]["stop_lag"] == pytest.approx((start_n - 1) / 2.0, abs=1e-6)
        assert r2[4]._state[r2[3].id]["stop_lag"] < cap

    def test_frames_zero_with_stop_patched_is_empty(self):
        patch, src, clk, tp, b = _stop_rig({"sat": 0.5})
        o = b._render_tape(tp, 0, {(src.id, "out"): np.zeros(0, np.float32),
                                   (clk.id, "out"): np.zeros(0, np.float32)}, patch)
        assert o.shape == (0,)


# ----- UI --------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("tape")
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
    for name in Tape.DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["stop_time"] == ("add_drag_float", "%.2f s")
    assert w["start_time"] == ("add_drag_float", "%.2f s")


# ----- example ---------------------------------------------------------------


def test_the_stop_drop_example_dives_halts_and_spins_back_up():
    from pysynthrack.io_patch import load_patch

    np.random.seed(0)
    path = Path(__file__).resolve().parent.parent / "examples" / "tape_stop_drop.json"
    patch = load_patch(path)
    assert len(patch.modules) <= 12
    tp = next(m for m in patch if m.TYPE == "tape")
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    outs, stopped, lag = [], [], []
    for _ in range(int(SR * 9 / F)):
        out, _devices = b.render_block_multi(F)
        assert out is not None and np.all(np.isfinite(out))
        outs.append(np.asarray(out).copy())
        st = b._state[tp.id]
        stopped.append(bool(st["stop_prev"]))
        lag.append(float(st["stop_lag"]))
    y = np.concatenate(outs, axis=0)
    assert 0.3 < float(np.abs(y).max()) < 0.8
    at = lambda sec: int(sec * SR) // F   # noqa: E731
    # the drop clock is high for the first 6.4 s of every 8; the logic NOT
    # of it is the stop: playing, then halted from 6.4 s, restarted at 8 s
    assert not stopped[at(3.0)] and stopped[at(7.0)] and not stopped[at(8.5)]
    mono = y[:, 0].astype(np.float64)

    def rms(sec0, sec1):
        return float(np.sqrt(np.mean(mono[int(sec0 * SR):int(sec1 * SR)] ** 2)))

    # the halt: from 6.4 + 1.25 s the head is still -> silence until 8 s
    assert rms(7.75, 7.95) < 1e-6
    assert rms(3.0, 5.0) > 0.05 and rms(8.6, 9.0) > 0.05
    # the lag after the restart is start_time / 2, and the head was live
    # (lag 0) before the first stop
    assert lag[at(5.0)] == 0.0
    assert lag[at(8.9)] == pytest.approx((int(0.6 * SR) - 1) / 2.0, abs=1e-6)
