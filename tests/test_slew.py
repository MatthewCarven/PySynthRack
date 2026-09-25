"""Slew — the CV slew limiter / lag / glide.

Pins the contract: independent rise/fall times, the two shapes (linear
constant-rate reaches the target; exponential eases to ~99% in the same
wall-clock), instant on a zero time, prime-to-input (no startup swoop),
voice-aware per-voice slew with mono==single-voice parity, block-size
independence, and unpatched → silence.

v2 (2026-09-14): rise_cv / fall_cv are 1 V/oct on that side's rate (+1
halves the reach time, -1 doubles it, each side alone, clamped at +-5),
per voice when (V, F) against a (V, F) input and one law otherwise;
a patched clock makes the times multiples of its measured period (a
tempo change re-times the glide, seconds until two edges are seen, and
seconds again when unpatched); every fast path (symmetric lfilter,
per-row lfilter) stays bit-parity with the scalar reference; explicit
zeros render array_equal to their absence.

Runs the renderer directly with hand-fed input blocks (no audio hardware),
injecting the upstream buffer into the store the same way render_block_multi
would — so an arbitrary mono (F,) or voice (V, F) input can be driven.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules import slew as _slew  # noqa: F401

SR = 1000  # 1 kHz keeps sample-count math exact and readable


def _driver(shape="linear", rise=0.1, fall=0.1, block=64):
    """A backend + a `constant → slew` patch, plus the slew's src id.

    Feed input by calling `.step(block_array)` with a mono (F,) or voice
    (V, F) array; it injects that as the upstream buffer and returns the
    slew's output for that block. State carries across calls."""
    patch = Patch()
    slew = patch.add_module(
        "slew", params={"shape": shape, "rise_time": rise, "fall_time": fall}
    )
    src = patch.add_module("constant")
    patch.connect(src.id, "out", slew.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(block_array):
        arr = np.asarray(block_array, dtype=np.float32)
        F = arr.shape[-1]
        return b._render_slew(patch.get(slew.id), F, {(src.id, "out"): arr}, patch)

    step.backend = b
    step.slew = slew
    return step


def _ramp_to(target, blocks, F):
    """[zeros block (primes at 0), then `blocks` blocks of `target`]."""
    return [np.zeros(F, dtype=np.float32)] + [
        np.full(F, target, dtype=np.float32) for _ in range(blocks)
    ]


# ----- registration ----------------------------------------------------------


class TestRegistration:
    def test_registered_in_cv_utilities(self):
        cls = get_module_type("slew")
        assert cls.CATEGORY == "CV & Utilities"
        assert "slew" in all_module_types()

    def test_ports_and_params(self):
        cls = get_module_type("slew")
        assert [p.name for p in cls.INPUT_PORTS] == ["in", "rise_cv", "fall_cv", "clock"]
        assert [p.name for p in cls.OUTPUT_PORTS] == ["out"]
        assert cls.DEFAULT_PARAMS["shape"] == "linear"
        assert cls.DEFAULT_PARAMS["rise_time"] == 0.1
        assert cls.DEFAULT_PARAMS["fall_time"] == 0.1


# ----- linear: constant rate, reaches the target -----------------------------


class TestLinear:
    def test_up_reaches_in_rise_time(self):
        # rise 0.05 s @ 1 kHz → step 1/50, a unit step reaches in ~50 samples.
        d = _driver("linear", rise=0.05, fall=0.2)
        d(np.zeros(64, dtype=np.float32))  # prime at 0
        y = np.concatenate([d(np.ones(64, dtype=np.float32)) for _ in range(2)])
        reach = int(np.argmax(y >= 0.999))
        assert 49 <= reach <= 51
        assert y.max() <= 1.0 + 1e-6  # never overshoots

    def test_fall_is_independent_and_slower(self):
        # fall 0.2 s = 4× the rise → ~200 samples down for a unit drop.
        d = _driver("linear", rise=0.05, fall=0.2)
        d(np.ones(64, dtype=np.float32))  # prime at 1
        y = np.concatenate([d(np.zeros(64, dtype=np.float32)) for _ in range(5)])
        reach = int(np.argmax(y <= 0.001))
        assert 198 <= reach <= 202

    def test_constant_rate_equal_steps(self):
        # Successive samples move by the same amount (a straight ramp).
        d = _driver("linear", rise=0.1, fall=0.1)
        d(np.zeros(32, dtype=np.float32))
        y = d(np.ones(32, dtype=np.float32))
        diffs = np.diff(y[:10])
        assert np.allclose(diffs, diffs[0], atol=1e-9)

    def test_slews_toward_negative_targets(self):
        # A downward move to a negative value uses fall_time and reaches it.
        d = _driver("linear", rise=0.5, fall=0.05)
        d(np.zeros(64, dtype=np.float32))
        y = np.concatenate([d(np.full(64, -1.0, dtype=np.float32)) for _ in range(2)])
        reach = int(np.argmax(y <= -0.999))
        assert 49 <= reach <= 51


# ----- exponential: one-pole ease to ~99% in the same wall-clock -------------


class TestExponential:
    def test_reaches_99pct_in_rise_time(self):
        d = _driver("exponential", rise=0.05, fall=0.05)
        d(np.zeros(64, dtype=np.float32))
        y = np.concatenate([d(np.ones(64, dtype=np.float32)) for _ in range(2)])
        r99 = int(np.argmax(y >= 0.99))
        assert 47 <= r99 <= 53

    def test_monotonic_and_no_overshoot(self):
        d = _driver("exponential", rise=0.05, fall=0.05)
        d(np.zeros(64, dtype=np.float32))
        y = np.concatenate([d(np.ones(64, dtype=np.float32)) for _ in range(2)])
        assert np.all(np.diff(y) >= -1e-9)  # never turns back
        assert y.max() <= 1.0 + 1e-9        # asymptotic, never exceeds target

    def test_curve_differs_from_linear_midway(self):
        # Same arrival time, different shape: exponential is already past the
        # linear ramp's halfway point at the midpoint of the glide.
        lin = _driver("linear", rise=0.1, fall=0.1)
        exp = _driver("exponential", rise=0.1, fall=0.1)
        for drv in (lin, exp):
            drv(np.zeros(128, dtype=np.float32))
        yl = lin(np.ones(128, dtype=np.float32))
        ye = exp(np.ones(128, dtype=np.float32))
        mid = 50  # ~half of the 0.1 s @ 1 kHz glide
        assert ye[mid] > yl[mid]

    def test_symmetric_fast_path_matches_scalar_reference(self):
        # rise == fall takes the vectorised lfilter path; it must stay
        # bit-parity (incl. the zi priming + block-carry) with the scalar
        # one-pole recurrence it replaced for performance. Multi-block,
        # arbitrary input, to exercise the cross-block state.
        rise = 0.05
        F = 100
        rng = np.random.default_rng(0)
        blocks = [rng.standard_normal(F).astype(np.float32) for _ in range(3)]
        d = _driver("exponential", rise=rise, fall=rise, block=F)
        got = np.concatenate([d(b) for b in blocks])

        a = float(np.exp(-NumpyBackend._LN100 / (rise * SR)))
        x = np.concatenate(blocks).astype(np.float64)
        ref = np.empty_like(x)
        c = float(x[0])  # primed to the first input sample
        for n in range(x.shape[0]):
            c = a * c + (1.0 - a) * x[n]
            ref[n] = c
        assert np.allclose(got, ref, atol=1e-6)


# ----- instant / prime / unpatched -------------------------------------------


class TestEdges:
    def test_zero_time_passes_through_instantly(self):
        d = _driver("linear", rise=0.0, fall=0.0)
        d(np.zeros(16, dtype=np.float32))
        y = d(np.ones(16, dtype=np.float32))
        assert np.allclose(y, 1.0)

    def test_primes_to_first_input_no_swoop(self):
        # First-ever block of a constant 0.7 emerges AT 0.7 immediately — the
        # running value primes to the input, it does not glide up from 0.
        d = _driver("linear", rise=0.5, fall=0.5)
        y = d(np.full(64, 0.7, dtype=np.float32))
        assert np.allclose(y, 0.7)

    def test_unpatched_is_silent(self):
        # No cable into `in` → nothing to slew → zeros, and no state kept.
        patch = Patch()
        s = patch.add_module("slew")
        b = NumpyBackend(sample_rate=SR, block_size=32)
        b.compile(patch)
        out = b._render_slew(patch.get(s.id), 32, {}, patch)
        assert out.shape == (32,) and not out.any()
        assert s.id not in b._state


# ----- voice-awareness -------------------------------------------------------


class TestVoiceAware:
    def test_voice_input_keeps_shape_and_slews_per_voice(self):
        d = _driver("linear", rise=0.05, fall=0.05, block=64)
        d(np.zeros((2, 64), dtype=np.float32))  # prime both voices at 0
        # Voice 0 jumps to 1.0, voice 1 to 0.5 — independent slews.
        block = np.zeros((2, 64), dtype=np.float32)
        block[0] = 1.0
        block[1] = 0.5
        y = np.concatenate([d(block) for _ in range(2)], axis=1)
        assert y.shape == (2, 128)
        # Same constant rate, so voice 1 (half the distance) reaches first.
        assert int(np.argmax(y[1] >= 0.499)) < int(np.argmax(y[0] >= 0.999))

    def test_mono_equals_single_voice(self):
        seq = [np.zeros(48, dtype=np.float32),
               np.ones(48, dtype=np.float32),
               np.full(48, -0.3, dtype=np.float32)]
        mono = _driver("linear", rise=0.07, fall=0.09, block=48)
        voice = _driver("linear", rise=0.07, fall=0.09, block=48)
        m = np.concatenate([mono(b) for b in seq])
        v = np.concatenate([voice(b[None, :])[0] for b in seq])
        assert np.allclose(m, v, atol=1e-6)


# ----- block-size independence ----------------------------------------------


class TestBlockSizeIndependence:
    def test_same_result_at_two_block_sizes(self):
        # A long ramp fed as 32- vs 64-sample blocks yields the same signal.
        full = np.concatenate([
            np.zeros(64, dtype=np.float32),
            np.ones(128, dtype=np.float32),
            np.full(128, -0.5, dtype=np.float32),
        ])

        def render(block):
            d = _driver("exponential", rise=0.08, fall=0.13, block=block)
            outs = []
            for i in range(0, full.shape[0], block):
                outs.append(d(full[i:i + block]))
            return np.concatenate(outs)

        assert np.allclose(render(32), render(64), atol=1e-6)


# ----- v2: rise_cv / fall_cv + clock sync (2026-09-14) -----------------------


def _driver2(shape="linear", rise=0.1, fall=0.1, block=64):
    """Like _driver, with lfo -> rise_cv / fall_cv and clock -> clock
    cabled; feed any of them per block (None = leave that jack silent
    for this block, which the renderer reads as unpatched-this-block)."""
    patch = Patch()
    slew = patch.add_module(
        "slew", params={"shape": shape, "rise_time": rise, "fall_time": fall}
    )
    src = patch.add_module("constant")
    rcv = patch.add_module("lfo")
    fcv = patch.add_module("lfo")
    clk = patch.add_module("clock")
    patch.connect(src.id, "out", slew.id, "in")
    patch.connect(rcv.id, "cv", slew.id, "rise_cv")
    patch.connect(fcv.id, "cv", slew.id, "fall_cv")
    patch.connect(clk.id, "out", slew.id, "clock")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(x, rise_cv=None, fall_cv=None, clock=None):
        arr = np.asarray(x, dtype=np.float32)
        F = arr.shape[-1]
        bufs = {(src.id, "out"): arr}
        if rise_cv is not None:
            bufs[(rcv.id, "cv")] = np.asarray(rise_cv, dtype=np.float32)
        if fall_cv is not None:
            bufs[(fcv.id, "cv")] = np.asarray(fall_cv, dtype=np.float32)
        if clock is not None:
            bufs[(clk.id, "out")] = np.asarray(clock, dtype=np.float32)
        return b._render_slew(patch.get(slew.id), F, bufs, patch)
    step.backend = b
    step.slew = slew
    return step


def _kw(F, kw):
    """Scalar CV values -> constant blocks; arrays pass through."""
    return {k: (np.full(F, v, np.float32) if np.ndim(v) == 0 else v) for k, v in kw.items()}


def _reach_up(step, blocks=4, F=64, **kw):
    """Prime at 0, step to 1, return the sample index where y >= 0.99 --
    the 99% point, which is what an exponential time MEANS (linear
    lands there one sample before it reaches)."""
    kws = _kw(F, kw)
    step(np.zeros(F, np.float32), **kws)
    y = np.concatenate([step(np.ones(F, np.float32), **kws) for _ in range(blocks)])
    hits = np.flatnonzero(y >= 0.99)
    return int(hits[0]) if hits.size else -1


def _reach_down(step, blocks=6, F=64, **kw):
    kws = _kw(F, kw)
    step(np.ones(F, np.float32), **kws)
    y = np.concatenate([step(np.zeros(F, np.float32), **kws) for _ in range(blocks)])
    hits = np.flatnonzero(y <= 0.01)
    return int(hits[0]) if hits.size else -1


class TestRateCVs:
    def test_explicit_zeros_match_their_absence(self):
        rng = np.random.RandomState(2)
        xs = [rng.uniform(-1, 1, 64).astype(np.float32) for _ in range(6)]
        for shape, rise, fall in (("linear", 0.05, 0.2), ("exponential", 0.1, 0.1), ("exponential", 0.03, 0.3)):
            a = _driver(shape, rise, fall)
            b = _driver2(shape, rise, fall)
            z = np.zeros(64, np.float32)
            for x in xs:
                assert np.array_equal(a(x), b(x, rise_cv=z, fall_cv=z))
            # Voice input, voice zeros.
            a = _driver(shape, rise, fall)
            b = _driver2(shape, rise, fall)
            for x in xs:
                v = np.stack([x, -x, x * 0.5])
                assert np.array_equal(a(v), b(v, rise_cv=np.zeros((3, 64), np.float32)))

    @pytest.mark.parametrize("shape", ["linear", "exponential"])
    def test_rise_cv_is_one_volt_per_octave_on_the_rise_only(self, shape):
        # rise 0.1 s @ 1 kHz: reach ~100; +1 -> ~50, -1 -> ~200; fall untouched.
        base_up = _reach_up(_driver2(shape, 0.1, 0.1))
        base_dn = _reach_down(_driver2(shape, 0.1, 0.1))
        assert 95 <= base_up <= 105
        up1 = _reach_up(_driver2(shape, 0.1, 0.1), rise_cv=1.0)
        dn1 = _reach_down(_driver2(shape, 0.1, 0.1), rise_cv=1.0)
        assert 46 <= up1 <= 54 and dn1 == base_dn
        upm = _reach_up(_driver2(shape, 0.1, 0.1), rise_cv=-1.0)
        assert 190 <= upm <= 210

    @pytest.mark.parametrize("shape", ["linear", "exponential"])
    def test_fall_cv_is_the_fall_only(self, shape):
        base_up = _reach_up(_driver2(shape, 0.1, 0.1))
        dn1 = _reach_down(_driver2(shape, 0.1, 0.1), fall_cv=1.0)
        up1 = _reach_up(_driver2(shape, 0.1, 0.1), fall_cv=1.0)
        assert 46 <= dn1 <= 54 and up1 == base_up

    def test_clamped_at_five_octaves(self):
        # +9 clamps to +5: 0.1 s / 32 = ~3 samples, not ~0.2.
        r = _reach_up(_driver2("linear", 0.1, 0.1), rise_cv=9.0)
        assert 2 <= r <= 5
        r2 = _reach_up(_driver2("linear", 0.1, 0.1), rise_cv=5.0)
        assert r == r2

    def test_per_voice_when_the_cv_is_voiced(self):
        """rows get rise_cv [+1, 0, -1]: reach ~50 / ~100 / ~200."""
        d = _driver2("linear", 0.1, 0.1)
        z = np.zeros((3, 64), np.float32)
        cv = np.stack([np.full(64, 1.0), np.zeros(64), np.full(64, -1.0)]).astype(np.float32)
        d(z, rise_cv=cv)
        y = np.concatenate([d(np.ones((3, 64), np.float32), rise_cv=cv) for _ in range(4)], axis=-1)
        reach = [int(np.flatnonzero(y[v] >= 0.999)[0]) for v in range(3)]
        assert 46 <= reach[0] <= 54 and 95 <= reach[1] <= 105 and 190 <= reach[2] <= 210

    def test_voice_row_matches_mono_fed_that_constant(self):
        for shape in ("linear", "exponential"):
            x = np.random.RandomState(7).uniform(-1, 1, 64).astype(np.float32)
            m = _driver2(shape, 0.05, 0.2)
            mono = [m(x, rise_cv=np.full(64, 0.7, np.float32), fall_cv=np.full(64, -0.4, np.float32)) for _ in range(4)]
            v = _driver2(shape, 0.05, 0.2)
            cvr = np.stack([np.full(64, -2.0), np.full(64, 0.7)]).astype(np.float32)
            cvf = np.stack([np.full(64, 1.0), np.full(64, -0.4)]).astype(np.float32)
            for k in range(4):
                out = v(np.stack([x * 0.3, x]), rise_cv=cvr, fall_cv=cvf)
                assert np.array_equal(out[1], mono[k]), (shape, k)

    def test_symmetric_per_row_lfilter_matches_the_scalar_reference(self):
        """Exponential with rise == fall but per-voice CVs takes the
        per-row lfilter path; a row must equal the scalar recurrence."""
        x = np.random.RandomState(3).uniform(-1, 1, 64).astype(np.float32)
        d = _driver2("exponential", 0.1, 0.1)
        cv = np.stack([np.full(64, 1.0), np.full(64, -0.5)]).astype(np.float32)
        outs = [d(np.stack([x, x]), rise_cv=cv, fall_cv=cv) for _ in range(3)]
        # Scalar reference for row 1: time 0.1 * 2**0.5, a = exp(-ln100/(t*sr)).
        t = 0.1 * 2 ** 0.5
        a = np.exp(-np.log(100.0) / (t * SR))
        c = float(x[0])
        ref = []
        for _ in range(3):
            for xn in x.astype(np.float64):
                c = a * c + (1.0 - a) * xn
                ref.append(c)
        got = np.concatenate([o[1] for o in outs])
        assert np.allclose(got, np.asarray(ref, np.float32), atol=1e-6)

    def test_voice_cv_into_a_mono_input_is_one_law(self):
        d = _driver2("linear", 0.1, 0.1)
        cv = np.stack([np.full(64, 0.5), np.full(64, 0.5)]).astype(np.float32)  # sums to +1
        d(np.zeros(64, np.float32), rise_cv=cv)
        y = np.concatenate([d(np.ones(64, np.float32), rise_cv=cv) for _ in range(3)])
        r = int(np.flatnonzero(y >= 0.999)[0])
        assert 46 <= r <= 54


def _clock(F, period, phase=0, width=3):
    c = np.zeros(F, np.float32)
    for k in range(-1, F // period + 2):
        st = k * period + phase
        if 0 <= st < F:
            c[st:st + width] = 1.0
    return c


class TestClockSync:
    def test_times_become_multiples_of_the_period(self):
        """rise_time 0.5 with a 200-sample clock: a unit rise takes ~100
        samples (0.5 x 200), not 500 ms."""
        d = _driver2("linear", 0.5, 0.25)
        F = 64
        clk = _clock(F * 8, 200)
        # Warm-up so two edges have been seen, holding at 0.
        for k in range(4):
            d(np.zeros(F, np.float32), clock=clk[k * F:(k + 1) * F])
        y = np.concatenate([d(np.ones(F, np.float32), clock=clk[(4 + k) * F:(5 + k) * F]) for k in range(4)])
        r = int(np.flatnonzero(y >= 0.999)[0])
        assert 98 <= r <= 102

    def test_tempo_change_retimes_the_glide(self):
        d = _driver2("linear", 0.5, 0.5)
        F = 64
        slow = _clock(F * 8, 200)
        fast = _clock(F * 8, 50)
        for k in range(4):
            d(np.zeros(F, np.float32), clock=slow[k * F:(k + 1) * F])
        y = np.concatenate([d(np.ones(F, np.float32), clock=slow[(4 + k) * F:(5 + k) * F]) for k in range(4)])
        r_slow = int(np.flatnonzero(y >= 0.999)[0])
        for k in range(4):
            d(np.ones(F, np.float32), clock=fast[k * F:(k + 1) * F])
        y = np.concatenate([d(np.zeros(F, np.float32), clock=fast[(4 + k) * F:(5 + k) * F]) for k in range(4)])
        r_fast = int(np.flatnonzero(y <= 0.001)[0])
        assert 98 <= r_slow <= 102
        assert 23 <= r_fast <= 27

    def test_seconds_until_two_edges_and_seconds_again_when_unpatched(self):
        # One edge only: no interval yet -> 0.5 s = 500 samples at 1 kHz.
        d = _driver2("linear", 0.5, 0.5)
        F = 64
        one = np.zeros(F * 12, np.float32)
        one[10:13] = 1.0
        d(np.zeros(F, np.float32), clock=one[:F])
        y = np.concatenate([d(np.ones(F, np.float32), clock=one[(1 + k) * F:(2 + k) * F]) for k in range(11)])
        assert int(np.flatnonzero(y >= 0.999)[0]) >= 495
        # Two edges 100 apart: now 50 samples.
        d = _driver2("linear", 0.5, 0.5)
        clk = _clock(F * 8, 100)
        for k in range(3):
            d(np.zeros(F, np.float32), clock=clk[k * F:(k + 1) * F])
        y = np.concatenate([d(np.ones(F, np.float32), clock=clk[(3 + k) * F:(4 + k) * F]) for k in range(3)])
        assert 48 <= int(np.flatnonzero(y >= 0.999)[0]) <= 52
        # Unpatch the clock: seconds again, and the old period is forgotten.
        d(np.ones(F, np.float32))
        y = np.concatenate([d(np.zeros(F, np.float32)) for _ in range(10)])
        assert int(np.flatnonzero(y <= 0.001)[0]) >= 495
        assert d.backend._state[d.slew.id]["interval"] == 0

    def test_period_is_carried_across_a_block_join(self):
        """Edges in different blocks measure the same period as edges in
        one block: the counter is absolute, not per block."""
        F = 64
        d = _driver2("linear", 1.0, 1.0)
        clk = _clock(F * 6, 150)         # 150 straddles blocks of 64
        for k in range(6):
            d(np.zeros(F, np.float32), clock=clk[k * F:(k + 1) * F])
        assert d.backend._state[d.slew.id]["interval"] == 150

    def test_clock_and_cv_compose(self):
        """0.5 x 200-sample period = 100, then rise_cv +1 halves it to 50."""
        d = _driver2("linear", 0.5, 0.5)
        F = 64
        clk = _clock(F * 8, 200)
        cv = np.full(F, 1.0, np.float32)
        for k in range(4):
            d(np.zeros(F, np.float32), clock=clk[k * F:(k + 1) * F], rise_cv=cv)
        y = np.concatenate([d(np.ones(F, np.float32), clock=clk[(4 + k) * F:(5 + k) * F], rise_cv=cv) for k in range(4)])
        assert 48 <= int(np.flatnonzero(y >= 0.999)[0]) <= 52

    def test_gate_into_clock_type_wall_and_full_render(self):
        patch = Patch()
        kb = patch.add_module("cv_keyboard")
        clk = patch.add_module("clock")
        lfo = patch.add_module("lfo")
        sl = patch.add_module("slew", params={"rise_time": 0.25, "fall_time": 0.25})
        patch.connect(kb.id, "pitch_cv", sl.id, "in")
        patch.connect(clk.id, "out", sl.id, "clock")
        patch.connect(lfo.id, "cv", sl.id, "rise_cv")
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", sl.id, "clock")     # cv -> gate
        b = NumpyBackend(sample_rate=SR, block_size=64)
        b.compile(patch)
        for _ in range(3):
            b.render_block(64)
