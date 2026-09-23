"""Tests for the Chorus (detuned multi-voice stereo thickener).

Coverage:
  - Model: registration, defaults, ports/kinds (audio ``in`` + cv
    ``rate_cv`` -> ``out_l`` / ``out_r`` audio), JSON round-trip,
    unknown-param rejection, and the signal-kind type walls.
  - DSP: disconnected -> silence; ``mix=0`` is a bit-exact dry
    passthrough on both channels; an impulse produces delayed taps;
    ``depth=0`` is a static comb while ``depth>0`` modulates; more
    voices changes the texture; output stays finite/bounded; a voice
    (2D) input is summed to mono.
  - Block independence: the chunked, feedback-free engine gives bit-
    identical output at any block size (512 vs 4096 vs an odd size).
  - Stereo: the two channels are decorrelated with >= 2 voices, and
    collapse together with a single voice.
  - CV: ``rate_cv`` alters the sweep; an all-zero ``rate_cv`` is a noop
    (bit-exact); ``rate_cv`` is read PER SAMPLE -- a modulated rate is
    bit-exact at 64 / 128 / 512 / 1000 over six seconds and tracks an
    ideal float64 integral of the instantaneous rate; the integer phase
    grid wraps exactly, both ways; a non-finite CV sample is no
    modulation.
  - Integration: osc -> chorus -> L/R speakers renders audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.chorus import Chorus

SR = 44100
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    ch = patch.add_module("chorus", params=params or {})
    patch.connect(src.id, "out", ch.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, ch, b


def _run(b, patch, src, ch, signal, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        o = b._render_chorus(ch, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _rig_cv(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    ch = patch.add_module("chorus", params=params or {})
    patch.connect(src.id, "out", ch.id, "in")
    patch.connect(lfo.id, "cv", ch.id, "rate_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, lfo, ch, b


def _run_cv(b, patch, src, lfo, ch, signal, cv, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {
            (src.id, "out"): signal[..., sl].astype(np.float32),
            (lfo.id, "cv"): cv[..., sl].astype(np.float32),
        }
        o = b._render_chorus(ch, block, bufs, patch)
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
        ch = Patch().add_module("chorus")
        assert isinstance(ch, Chorus)
        assert ch.params == {
            "rate": 0.6,
            "depth": 0.5,
            "voices": 3,
            "mix": 0.5,
            "cv_depth": 1.0,
        }

    def test_voices_default_is_int(self):
        ch = Patch().add_module("chorus")
        assert isinstance(ch.params["voices"], int)

    def test_ports_and_kinds(self):
        ch = Patch().add_module("chorus")
        assert [(p.name, p.signal_kind) for p in ch.input_ports] == [
            ("in", "audio"),
            ("rate_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in ch.output_ports] == [
            ("out_l", "audio"),
            ("out_r", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("chorus", params={"rate": 2.5, "voices": 4, "mix": 0.7})
        restored = Patch.from_dict(patch.to_dict())
        ch = next(m for m in restored if m.TYPE == "chorus")
        assert ch.params["rate"] == 2.5
        assert ch.params["voices"] == 4
        assert ch.params["mix"] == 0.7

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("chorus", params={"feedback": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ch = patch.add_module("chorus")
        patch.connect(osc.id, "out", ch.id, "in")  # no raise

    def test_cv_into_rate_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ch = patch.add_module("chorus")
        patch.connect(lfo.id, "cv", ch.id, "rate_cv")  # no raise

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ch = patch.add_module("chorus")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ch.id, "in")

    def test_audio_into_rate_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ch = patch.add_module("chorus")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", ch.id, "rate_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        ch = patch.add_module("chorus")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(ch.id, "out_l", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        ch = patch.add_module("chorus")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_chorus(ch, F, {}, patch)
        assert not np.any(o["out_l"]) and not np.any(o["out_r"])
        assert o["out_l"].shape == (F,)

    def test_frames_zero_empty(self):
        patch, src, ch, b = _rig()
        o = b._render_chorus(ch, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch)
        assert o["out_l"].shape == (0,) and o["out_r"].shape == (0,)

    def test_mix_zero_exact_dry_passthrough(self):
        patch, src, ch, b = _rig({"mix": 0.0, "depth": 0.8, "voices": 4})
        x = (np.random.randn(F * 4)).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert np.array_equal(lo, x[: len(lo)])
        assert np.array_equal(r, x[: len(r)])

    def test_output_is_float32(self):
        patch, src, ch, b = _rig({"mix": 0.6})
        lo, r = _run(b, patch, src, ch, np.random.randn(F * 2).astype(np.float32))
        assert lo.dtype == np.float32 and r.dtype == np.float32

    def test_impulse_produces_delayed_taps(self):
        # A dry impulse at t=0; at mix=1 the output is only the delayed
        # wet taps, which land around the ~12..24 ms base delays, i.e.
        # clearly after the input sample and before ~40 ms.
        patch, src, ch, b = _rig({"mix": 1.0, "depth": 0.5, "voices": 3})
        lo, _ = _run(b, patch, src, ch, _impulse(F * 8))
        lo_start = int(0.002 * SR)   # 2 ms
        lo_end = int(0.040 * SR)     # 40 ms
        assert np.max(np.abs(lo[lo_start:lo_end])) > 1e-3
        assert np.all(np.isfinite(lo))

    def test_depth_zero_static_but_not_dry(self):
        # depth=0 -> no LFO sweep -> a static comb; with mix>0 it is not
        # the dry signal, and it differs from a modulated (depth>0) render.
        x = (np.random.randn(F * 4)).astype(np.float32)
        p0, s0, c0, b0 = _rig({"depth": 0.0, "mix": 0.6, "voices": 3, "rate": 1.5})
        l0, _ = _run(b0, p0, s0, c0, x)
        pm, sm, cm, bm = _rig({"depth": 0.6, "mix": 0.6, "voices": 3, "rate": 1.5})
        lm, _ = _run(bm, pm, sm, cm, x)
        assert not np.array_equal(l0, x[: len(l0)])       # comb, not dry
        assert not np.allclose(l0, lm, atol=1e-6)         # modulation matters

    def test_more_voices_changes_texture(self):
        x = (np.random.randn(F * 4)).astype(np.float32)
        p1, s1, c1, b1 = _rig({"voices": 1, "depth": 0.6, "mix": 0.7, "rate": 1.2})
        l1, _ = _run(b1, p1, s1, c1, x)
        p4, s4, c4, b4 = _rig({"voices": 4, "depth": 0.6, "mix": 0.7, "rate": 1.2})
        l4, _ = _run(b4, p4, s4, c4, x)
        assert not np.allclose(l1, l4, atol=1e-6)

    def test_finite_and_bounded_at_extremes(self):
        patch, src, ch, b = _rig({"voices": 6, "depth": 1.0, "mix": 1.0, "rate": 10.0})
        x = (np.random.randn(2 * SR) * 0.5).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))
        assert np.max(np.abs(lo)) < 8.0 and np.max(np.abs(r)) < 8.0

    def test_voice_input_summed_to_mono(self):
        patch, src, ch, b = _rig({"mix": 1.0, "depth": 0.5})
        v = np.random.randn(3, F).astype(np.float32)
        o = b._render_chorus(ch, F, {(src.id, "out"): v}, patch)
        assert o["out_l"].shape == (F,) and o["out_r"].shape == (F,)
        assert np.all(np.isfinite(o["out_l"]))


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def _render(self, params, x, block):
        p, s, c, b = _rig(params, block=block)
        return _run(b, p, s, c, x, block=block)

    def test_output_independent_of_block_size(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(12000) / SR) * 0.4).astype(np.float32)
        params = {"rate": 2.0, "depth": 0.7, "voices": 3, "mix": 0.5}
        la, ra = self._render(params, x, 512)
        for block in (4096, 333):
            lb, rb = self._render(params, x, block)
            m = min(len(la), len(lb))
            assert np.array_equal(la[:m], lb[:m]), f"block {block}"
            assert np.array_equal(ra[:m], rb[:m]), f"block {block}"

    @pytest.mark.parametrize("params", [
        {"rate": 0.5, "depth": 0.6, "voices": 4, "mix": 0.5},
        {"rate": 0.8, "depth": 1.0, "voices": 6, "mix": 1.0},
        {"rate": 0.05, "depth": 0.3, "voices": 1, "mix": 1.0},
    ])
    def test_four_seconds_is_bit_exact(self, params):
        # FOUR SECONDS at four block sizes sharing no alignment -- long
        # enough for the ring to wrap hundreds of times and for a carried
        # float phase to drift. Two mechanisms had to be fixed to get here
        # (measured 2026-09-22, against the 512 render; the lush setting
        # was 487 / 103 / 55 differing samples at 64 / 128 / 1000, max
        # 6e-8):
        #   * the read formed ``absidx - delay``, and a ring index rounds
        #     at its OWN magnitude -- the ring is ``max_ms + frames``
        #     long, so it wraps at a different absolute sample per block
        #     size. It now splits the delay into whole samples + a
        #     fraction, both functions of the delay alone;
        #   * the sweep carried a float phase, and ``ph + frames * inc``
        #     rounds once per block. It now counts samples since the last
        #     rate change (which re-anchors, so a rate move stays
        #     continuous).
        x = (np.random.default_rng(7).standard_normal(4 * SR) * 0.3).astype(np.float32)
        la, ra = self._render(params, x, 512)
        for block in (64, 128, 1000, 4096, 333):
            lb, rb = self._render(params, x, block)
            m = min(len(la), len(lb))
            assert np.array_equal(la[:m], lb[:m]), f"block {block}"
            assert np.array_equal(ra[:m], rb[:m]), f"block {block}"

    def test_a_rate_change_re_anchors_the_sweep_without_a_jump(self):
        # The sample count is keyed to the CURRENT rate, so a rate move
        # has to freeze the phase reached so far rather than restart it:
        # otherwise the sweep jumps and clicks. Change the rate mid-run
        # and the delay's step across the seam stays in family with the
        # steps either side of it.
        x = (np.random.default_rng(3).standard_normal(SR) * 0.3).astype(np.float32)
        p, s, c, b = _rig({"rate": 4.0, "depth": 1.0, "voices": 1,
                           "mix": 1.0}, block=F)
        outs = []
        for k in range(SR // F):
            if k == SR // F // 2:
                c.params["rate"] = 0.25          # a big rate move, mid-run
            bufs = {(s.id, "out"): x[k * F:(k + 1) * F]}
            o = b._render_chorus(c, F, bufs, p)
            outs.append(o["out_l"])
        y = np.concatenate(outs)
        seam = (SR // F // 2) * F
        step = np.abs(np.diff(y.astype(np.float64)))
        assert step[seam - 1] <= 6.0 * float(np.median(step))
        # and the phase carried across, rather than restarting at 0; the
        # count runs from the change, not from the start of the stream
        assert b._state[c.id]["phase"] != 0.0
        assert b._state[c.id]["ph_n"] == (SR // F - SR // F // 2) * F


# ----- Stereo ----------------------------------------------------------------


class TestStereo:
    def test_channels_are_decorrelated(self):
        patch, src, ch, b = _rig({"voices": 3, "depth": 0.6, "mix": 0.7, "rate": 1.5})
        x = (np.random.randn(SR) * 0.3).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert not np.array_equal(lo, r)
        corr = np.corrcoef(lo[3000:], r[3000:])[0, 1]
        assert abs(corr) < 0.99

    def test_single_voice_channels_equal(self):
        # One voice sits dead centre, so both channels are identical.
        patch, src, ch, b = _rig({"voices": 1, "depth": 0.6, "mix": 0.8, "rate": 1.5})
        x = (np.random.randn(F * 4) * 0.3).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert np.allclose(lo, r, atol=1e-6)


# ----- CV --------------------------------------------------------------------


class TestCV:
    def test_rate_cv_alters_output(self):
        x = (np.random.randn(F * 6) * 0.3).astype(np.float32)
        params = {"rate": 1.0, "depth": 0.6, "voices": 3, "mix": 0.7, "cv_depth": 2.0}
        # +1.0 unit constant CV -> +2 octaves of LFO rate.
        p, s, lfo, ch, b = _rig_cv(params)
        l_hi, _ = _run_cv(b, p, s, lfo, ch, x, np.ones_like(x))
        # Same patch, zero CV.
        p0, s0, lfo0, ch0, b0 = _rig_cv(params)
        l_zero, _ = _run_cv(b0, p0, s0, lfo0, ch0, x, np.zeros_like(x))
        assert not np.allclose(l_hi, l_zero, atol=1e-6)

    def test_zero_rate_cv_is_noop(self):
        x = (np.random.randn(F * 6) * 0.3).astype(np.float32)
        params = {"rate": 1.0, "depth": 0.6, "voices": 3, "mix": 0.7, "cv_depth": 2.0}
        p, s, lfo, ch, b = _rig_cv(params)
        l_cv, _ = _run_cv(b, p, s, lfo, ch, x, np.zeros_like(x))
        pn, sn, cn, bn = _rig(params)
        l_no, _ = _run(bn, pn, sn, cn, x)
        assert np.array_equal(l_cv, l_no)


# ----- rate_cv, per sample ---------------------------------------------------

_PS_PARAMS = {"rate": 0.6, "depth": 0.8, "voices": 4, "mix": 1.0, "cv_depth": 1.0}


def _ps_signals(n):
    x = (np.random.default_rng(1).standard_normal(n) * 0.3).astype(np.float32)
    cv = np.sin(2 * np.pi * 0.3 * np.arange(n) / SR).astype(np.float32)
    return x, cv


def _ps_render(x, cv, block, params=_PS_PARAMS):
    p, s, lfo, ch, b = _rig_cv(dict(params), block=block)
    lo, ro = _run_cv(b, p, s, lfo, ch, x, cv, block=block)
    return np.stack([lo, ro])


def _ideal_chorus(x, cv, params=_PS_PARAMS):
    """The chorus's own read with an IDEAL sweep: the instantaneous rate in
    float64 per sample, integrated by ONE exclusive cumsum over the whole
    run -- no blocks anywhere. Pure numpy, independent of the renderer."""
    n = x.shape[-1]
    V = params["voices"]
    r = np.clip(params["rate"] * 2.0 ** (params["cv_depth"] * cv.astype(np.float64)),
                0.01, 20.0)
    phase = np.concatenate(([0.0], np.cumsum(r / SR)[:-1]))
    ph = (phase[None, :] + (np.arange(V) / V)[:, None]) % 1.0
    delay = (np.linspace(12.0, 24.0, V) * SR / 1000.0)[:, None] \
        + (8.0 * params["depth"] * SR / 1000.0) * np.sin(2 * np.pi * ph)
    back = np.ceil(delay)
    frac = back - delay
    i0 = np.arange(n)[None, :] - back.astype(np.int64)
    xx = x.astype(np.float64)

    def at(i):
        return np.where(i >= 0, xx[np.clip(i, 0, n - 1)], 0.0)

    tap = at(i0) * (1.0 - frac) + at(i0 + 1) * frac
    pos = (np.arange(V) + 0.5) / V
    gl, gr = np.cos(pos * np.pi / 2), np.sin(pos * np.pi / 2)
    return np.stack([(gl @ tap) / np.sqrt(np.sum(gl * gl)),
                     (gr @ tap) / np.sqrt(np.sum(gr * gr))])


class TestRateCVPerSample:
    def test_modulated_rate_is_bit_exact_across_block_sizes(self):
        # SIX SECONDS of a 0.3 Hz sine on rate_cv (+/-1 octave) at four
        # block sizes sharing no alignment. Before 2026-09-24 rate_cv was
        # a BLOCK MEAN: the sweep re-anchored every block, so 64 / 128 /
        # 1000 each differed from 512 at 526,914 of 528,000 samples (both
        # channels). The CV's share of the phase is now summed on an
        # integer grid, so it is the same number however the stream is
        # cut. Each size renders its whole blocks of 6 s; the first
        # 264,000 samples are covered at every size.
        m = 264000
        x, cv = _ps_signals(6 * SR)
        ref = _ps_render(x, cv, 512)[:, :m]
        assert ref.shape == (2, m)
        for block in (64, 128, 1000):
            got = _ps_render(x, cv, block)[:, :m]
            assert np.array_equal(got, ref), (
                f"block {block}: {int(np.count_nonzero(got != ref))} samples differ")

    @pytest.mark.parametrize("block", [64, 1000])
    def test_modulated_rate_tracks_the_ideal_integral(self, block):
        # The rate is continuous now, not a staircase. Against an ideal
        # float64 per-sample integration over the whole run the render is
        # within a float32 ulp or two (measured 5.96e-8 max at every block
        # size over 6 s); the block mean was 1.0e-3 off at 64 and 0.24 at
        # 1000 -- a 1000-sample staircase moves the sweep audibly.
        n = 2 * SR - (2 * SR) % 1000 if block == 1000 else 2 * SR - (2 * SR) % 64
        x, cv = _ps_signals(n)
        got = _ps_render(x, cv, block).astype(np.float64)
        ideal = _ideal_chorus(x, cv)
        assert got.shape == ideal.shape
        err = float(np.max(np.abs(got - ideal)))
        assert err < 2.5e-7, f"block {block}: max error {err:.3e}"

    def test_rate_moves_within_a_block(self):
        # One 4096-sample block, CV stepping from 0 to +2 octaves at its
        # middle. A per-sample read speeds the sweep up at the step, so
        # the first half is bit-identical to the zero-CV render and the
        # second half is not. (A block mean would have moved both halves.)
        n = 4096
        x = (np.random.default_rng(5).standard_normal(n) * 0.3).astype(np.float32)
        cv = np.zeros(n, dtype=np.float32)
        cv[n // 2:] = 2.0
        params = dict(_PS_PARAMS, rate=4.0)
        stepped = _ps_render(x, cv, n, params)
        flat = _ps_render(x, np.zeros(n, dtype=np.float32), n, params)
        # sample n//2 is the first to see the step's first increment
        # at n//2 + 1 (the exclusive-sum convention): up to n//2 is equal
        assert np.array_equal(stepped[:, :n // 2 + 1], flat[:, :n // 2 + 1])
        assert not np.array_equal(stepped[:, n // 2 + 64:], flat[:, n // 2 + 64:])

    def test_integrator_zero_cv_adds_exactly_nothing(self):
        b = NumpyBackend(sample_rate=SR, block_size=F)
        st = {}
        inc = 0.6 / SR
        d = b._chorus_rate_cv_phase(st, np.zeros(F, np.float32), 0.6, inc, 1.0, F)
        assert d.dtype == np.float64
        assert np.array_equal(d, np.zeros(F))
        assert st["rc_acc"] == 0

    def test_integrator_wraps_exactly_both_ways(self):
        # Sample n sees the sum of the deviations BEFORE it, on a grid of
        # 2**48 per cycle, wrapped by a mask -- including a negative sum.
        b = NumpyBackend(sample_rate=SR, block_size=F)
        bits = NumpyBackend._CHORUS_PH_BITS
        mask = (1 << bits) - 1
        for cv_value in (3.0, -3.0):
            st = {}
            knob = 2.0
            inc = knob / SR
            cv = np.full(F, cv_value, dtype=np.float32)
            outs = [b._chorus_rate_cv_phase(st, cv, knob, inc, 1.0, F)
                    for _ in range(200)]
            d = np.concatenate(outs)
            q = int(np.rint((knob * 2.0 ** cv_value / SR - inc) * float(1 << bits)))
            k = np.arange(d.size, dtype=object)
            want = np.array([float((q * int(i)) & mask) for i in k]) / float(1 << bits)
            assert np.array_equal(d, want)
            assert 0 <= st["rc_acc"] <= mask
            assert np.all((d >= 0.0) & (d < 1.0))

    def test_non_finite_cv_sample_is_no_modulation(self):
        n = 4 * F
        x = (np.random.default_rng(9).standard_normal(n) * 0.3).astype(np.float32)
        cv = np.zeros(n, dtype=np.float32)
        cv[100] = np.nan
        cv[700] = np.inf
        cv[1500] = -np.inf
        got = _ps_render(x, cv, F)
        clean = _ps_render(x, np.zeros(n, dtype=np.float32), F)
        assert np.all(np.isfinite(got))
        assert np.array_equal(got, clean)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_chorus_stereo_speakers(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0})
        ch = patch.add_module("chorus", params={"depth": 0.6, "mix": 0.5, "voices": 3})
        spk_l = patch.add_module("left_speaker_output")
        spk_r = patch.add_module("right_speaker_output")
        patch.connect(osc.id, "out", ch.id, "in")
        patch.connect(ch.id, "out_l", spk_l.id, "in")
        patch.connect(ch.id, "out_r", spk_r.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(60):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
