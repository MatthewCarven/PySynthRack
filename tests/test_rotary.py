"""Rotary — the Leslie cabinet (horn + drum, tremolo + Doppler, stereo).

Coverage:
  - Model: registration in Effects, defaults, ports/signal kinds, JSON
    round-trip, unknown-param rejection, type walls (gate -> fast ok, cv
    -> fast refused, audio out -> speaker ok).
  - Silence: unpatched -> zeros on every jack; silence in -> silence out.
  - Standing still: speed stop + depth 0 is the crossover recombined
    through a fixed delay -- flat to the input (LR4 low + high sums
    flat), and L == R; spread 0 gives L == R even while spinning.
  - Tremolo: the horn band's 10 ms peak envelope modulates at the horn
    rate (fast ~6.7 Hz, slow ~0.7 Hz); the drum band at the drum's
    0.85x rate; depth 0 flattens the envelope; the raw envelope is NOT
    the Hilbert envelope of a 3 s slice (edge transients -- measured
    once, wrongly, by me).
  - Doppler: the horn's zero-crossing period on a 3 kHz tone swings by
    about r/c * 2*pi*f -- +-2.3% at 6.7 Hz, depth 1 -- and not at depth 0.
  - Ramps: the horn reaches fast within ~2 s, the drum lags it (its rate
    is still climbing at 2 s and reaches fast by 20 s); stop coasts
    both to 0; ramp scales the times.
  - The fast gate overrides the combo (majority level per block).
  - Balance +1 / -1 leaves only the horn / drum band; mix 0 is the
    delay-matched dry (== input shifted by the centre delay).
  - Stereo: L != R at spread 0.7; out == (L + R) / 2 bit-exact.
  - Block-size exactness (2026-09-24): 64 / 128 / 512 / 1000 render the
    identical sample over four seconds at 48 kHz -- slow, fast, the fast
    jack toggling, fast -> stop -> slow -- and so does an irregular
    partition (1..4500-sample blocks, the ring growing mid-run). The old
    pin was 2 s of 64 vs 512 under allclose; the drift it missed was a
    float32 ulp at a handful of samples. Voice inputs sum.
  - Example organ_leslie.json loads, compiles, renders stereo, and
    switches speed off its clock.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.io_patch import load_patch
from pysynthrack.modules.rotary import ROTARY_SPEEDS, Rotary

SR = 48000
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    osc = patch.add_module("oscillator")
    rot = patch.add_module("rotary", params=params or {})
    clk = patch.add_module("clock")
    patch.connect(osc.id, "out", rot.id, "in")
    patch.connect(clk.id, "out", rot.id, "fast")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, osc, rot, clk, b


def _tone(freq, secs, amp=0.5):
    t = np.arange(int(secs * SR))
    return (amp * np.sin(2 * np.pi * freq * t / SR)).astype(np.float32)


def _run(params, x, gate=None, block=F):
    """Render x (F,) or (V, F) through a fresh rotary; returns (L, R, M, backend, rot)."""
    patch, osc, rot, clk, b = _rig(params, block)
    n = x.shape[-1] // block
    L, R, M = [], [], []
    for k in range(n):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(osc.id, "out"): x[..., sl]}
        if gate is not None:
            bufs[(clk.id, "out")] = gate[sl]
        r = b._render_rotary(rot, block, bufs, patch)
        L.append(r["out_l"]); R.append(r["out_r"]); M.append(r["out"])
    return np.concatenate(L), np.concatenate(R), np.concatenate(M), b, rot


def _peaks(y, win_ms=10.0):
    w = int(SR * win_ms / 1000)
    n = (len(y) // w) * w
    return np.abs(y[:n]).reshape(-1, w).max(axis=1)


def _env_rate(y, skip_s):
    """Dominant modulation rate (Hz) of the 10 ms peak envelope."""
    pk = _peaks(y[int(skip_s * SR):])
    pk = pk - pk.mean()
    spec = np.abs(np.fft.rfft(pk * np.hanning(len(pk))))
    fr = np.fft.rfftfreq(len(pk), 0.01)
    return float(fr[spec.argmax()])


def _zc_period_swing(y):
    """(min, max) zero-crossing period in samples, edges trimmed."""
    y = y[int(0.5 * SR):-int(0.5 * SR)]
    zc = np.flatnonzero((y[1:] >= 0) & (y[:-1] < 0))
    # average over 8 cycles to beat the integer quantisation
    per = np.diff(zc)
    per8 = per[: (len(per) // 8) * 8].reshape(-1, 8).mean(axis=1)
    return float(per8.min()), float(per8.max())


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        rot = Patch().add_module("rotary")
        assert isinstance(rot, Rotary)
        assert rot.CATEGORY == "Effects"
        assert rot.params == {
            "speed": "slow", "slow_rate": 0.7, "fast_rate": 6.7, "ramp": 1.0,
            "depth": 0.7, "spread": 0.7, "balance": 0.0, "crossover": 800.0,
            "mix": 1.0,
        }
        assert ROTARY_SPEEDS == ("slow", "fast", "stop")

    def test_ports_and_signal_kinds(self):
        rot = Patch().add_module("rotary")
        assert [(p.name, p.signal_kind) for p in rot.input_ports] == [
            ("in", "audio"), ("fast", "gate"),
        ]
        assert [(p.name, p.signal_kind) for p in rot.output_ports] == [
            ("out_l", "audio"), ("out_r", "audio"), ("out", "audio"),
        ]

    def test_json_round_trip_and_unknown_param(self):
        from pysynthrack.io_patch import patch_from_json, patch_to_json
        patch = Patch()
        patch.add_module("rotary", params={"speed": "fast", "spread": 0.3, "crossover": 600.0})
        back = patch_from_json(patch_to_json(patch))
        rot = next(iter(back))
        assert rot.params["speed"] == "fast"
        assert rot.params["spread"] == pytest.approx(0.3)
        assert rot.params["crossover"] == pytest.approx(600.0)
        with pytest.raises(Exception):
            Patch().add_module("rotary", params={"rpm": 400})

    def test_type_walls(self):
        patch = Patch()
        rot = patch.add_module("rotary")
        clk = patch.add_module("clock")
        lfo = patch.add_module("lfo")
        spk_l = patch.add_module("left_speaker_output")
        patch.connect(clk.id, "out", rot.id, "fast")           # gate -> gate
        patch.connect(rot.id, "out_l", spk_l.id, "in")        # audio -> audio
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", rot.id, "fast")       # cv -> gate
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", rot.id, "in")         # cv -> audio


# ----- Silence and standing still --------------------------------------------


class TestStill:
    def test_unpatched_and_silent_inputs(self):
        patch = Patch()
        rot = patch.add_module("rotary")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        r = b._render_rotary(rot, F, {}, patch)
        for key in ("out_l", "out_r", "out"):
            assert r[key].shape == (F,) and not r[key].any()
        L, R, M, _b, _r = _run({"speed": "fast"}, np.zeros(F * 8, np.float32))
        assert not L.any() and not R.any() and not M.any()

    def test_stopped_and_flat_is_the_input_through_a_fixed_delay(self):
        """stop + depth 0: no tremolo, no Doppler; the LR4 bands recombine
        to a flat MAGNITUDE (their sum is an allpass, so the waveform is
        phase-rotated, not identical -- a correlation test would lie),
        one fixed delay, L == R."""
        rng = np.random.RandomState(1)
        x = (0.3 * rng.randn(4 * SR)).astype(np.float32)
        L, R, M, b, rot = _run({"speed": "stop", "depth": 0.0, "spread": 0.9}, x)
        assert np.array_equal(L, R)
        x = x[:L.shape[0]]
        seg = slice(SR // 2, L.shape[0])
        X = np.abs(np.fft.rfft(x[seg].astype(np.float64)))
        Y = np.abs(np.fft.rfft(L[seg].astype(np.float64)))
        fr = np.fft.rfftfreq(seg.stop - seg.start, 1 / SR)
        for lo, hi in ((60, 200), (200, 600), (600, 1000), (1000, 3000), (3000, 10000), (10000, 20000)):
            band = (fr >= lo) & (fr < hi)
            ratio_db = 20 * np.log10(np.sqrt((Y[band] ** 2).sum() / (X[band] ** 2).sum()))
            assert abs(ratio_db) < 0.5, (lo, hi, ratio_db)

    def test_spread_zero_is_mono_even_while_spinning(self):
        L, R, M, _b, _r = _run({"speed": "fast", "spread": 0.0}, _tone(2000.0, 1.0))
        assert np.array_equal(L, R)
        assert np.array_equal(M, L)

    def test_out_is_the_mono_sum(self):
        L, R, M, _b, _r = _run({"speed": "fast"}, _tone(2000.0, 1.0))
        assert np.array_equal(M, 0.5 * (L + R))       # float32, the renderer's own op
        assert not np.array_equal(L, R)


# ----- Tremolo (AM) ----------------------------------------------------------


class TestTremolo:
    def test_horn_band_modulates_at_the_horn_rate(self):
        L, R, M, b, rot = _run({"speed": "fast", "depth": 1.0}, _tone(3000.0, 6.0))
        assert _env_rate(L, 3.0) == pytest.approx(6.7, abs=0.35)
        assert b._state[rot.id]["horn_f"] == pytest.approx(6.7, abs=0.05)
        L, R, M, _b, _r = _run({"speed": "slow", "depth": 1.0}, _tone(3000.0, 8.0))
        assert _env_rate(L, 2.0) == pytest.approx(0.7, abs=0.2)

    def test_drum_band_modulates_at_the_drum_rate(self):
        """100 Hz sits in the drum band; once the drum has spun up (its
        ramp is ~4.5 s) its envelope runs at 0.85 x the horn rate."""
        L, R, M, b, rot = _run({"speed": "fast", "depth": 1.0, "ramp": 0.25}, _tone(100.0, 8.0))
        assert _env_rate(L, 3.0) == pytest.approx(6.7 * 0.85, abs=0.35)
        assert b._state[rot.id]["drum_f"] == pytest.approx(6.7 * 0.85, abs=0.05)

    def test_depth_zero_flattens_the_envelope(self):
        L, R, M, _b, _r = _run({"speed": "fast", "depth": 0.0}, _tone(3000.0, 3.0))
        pk = _peaks(L[SR:])
        assert pk.max() - pk.min() < 0.01
        L, R, M, _b, _r = _run({"speed": "fast", "depth": 1.0}, _tone(3000.0, 3.0))
        pk = _peaks(L[SR:])
        # horn AM 0.8 at depth 1: the trough is ~20% of the crest
        assert pk.min() / pk.max() == pytest.approx(0.2, abs=0.08)

    def test_left_and_right_are_out_of_phase_at_full_spread(self):
        """spread 1 puts the mics 180 deg apart: when the horn faces one
        it faces away from the other, so the peak envelopes anticorrelate."""
        L, R, M, _b, _r = _run({"speed": "fast", "depth": 1.0, "spread": 1.0}, _tone(3000.0, 4.0))
        pl, pr = _peaks(L[2 * SR:]), _peaks(R[2 * SR:])
        assert np.corrcoef(pl, pr)[0, 1] < -0.8


# ----- Doppler ---------------------------------------------------------------


class TestDoppler:
    def test_horn_pitch_swings_by_r_over_c_times_angular_rate(self):
        """The delay swing is (r/c) * cos(theta); its derivative at 6.7 Hz
        is +-(r/c) * 2*pi*6.7 = +-2.3% of the pitch. Zero-crossing periods
        averaged over 8 cycles swing by about that; at depth 0 they don't."""
        NumpyBackend_am = NumpyBackend._ROT_HORN_AM
        try:
            NumpyBackend._ROT_HORN_AM = 0.0      # isolate the FM from the AM
            L, R, M, b, rot = _run({"speed": "fast", "depth": 1.0, "balance": 1.0, "spread": 0.0}, _tone(3000.0, 6.0))
        finally:
            NumpyBackend._ROT_HORN_AM = NumpyBackend_am
        lo, hi = _zc_period_swing(L[3 * SR:])
        nominal = SR / 3000.0
        expected = b._ROT_HORN_R / b._ROT_C * 2 * np.pi * 6.7
        swing = (hi - lo) / 2 / nominal
        assert swing == pytest.approx(expected, rel=0.3)
        L, R, M, _b, _r = _run({"speed": "fast", "depth": 0.0, "balance": 1.0, "spread": 0.0}, _tone(3000.0, 4.0))
        lo, hi = _zc_period_swing(L[2 * SR:])
        assert (hi - lo) / nominal < 0.003

    def test_signal_envelope_is_flat_under_pure_doppler(self):
        """With the AM off, the moving tap must not amplitude-modulate:
        10 ms peaks stay put (a Hilbert envelope of a 3 s slice does NOT
        -- its edge transients are what fooled the first measurement)."""
        am = NumpyBackend._ROT_HORN_AM
        try:
            NumpyBackend._ROT_HORN_AM = 0.0
            L, R, M, _b, _r = _run({"speed": "fast", "depth": 1.0, "balance": 1.0, "spread": 0.0}, _tone(3000.0, 4.0))
        finally:
            NumpyBackend._ROT_HORN_AM = am
        pk = _peaks(L[2 * SR:])
        assert pk.max() - pk.min() < 0.01


# ----- Ramps and the switch --------------------------------------------------


class TestRamps:
    def test_horn_is_quick_and_the_drum_lags(self):
        x = _tone(1000.0, 2.0)
        L, R, M, b, rot = _run({"speed": "fast"}, x)
        st = b._state[rot.id]
        assert st["horn_f"] > 0.8 * 6.7               # tau 1 s: 86% by 2 s
        assert st["drum_f"] < 0.5 * 6.7 * 0.85        # tau 4.5 s: 36% by 2 s
        assert st["drum_f"] > 0.0
        L, R, M, b, rot = _run({"speed": "fast"}, _tone(1000.0, 20.0))
        assert b._state[rot.id]["drum_f"] == pytest.approx(6.7 * 0.85, abs=0.1)

    def test_ramp_scales_the_times(self):
        L, R, M, b, rot = _run({"speed": "fast", "ramp": 0.25}, _tone(1000.0, 2.0))
        quick = b._state[rot.id]["drum_f"]
        L, R, M, b, rot = _run({"speed": "fast", "ramp": 4.0}, _tone(1000.0, 2.0))
        lazy = b._state[rot.id]["drum_f"]
        assert quick > 4 * lazy

    def test_stop_coasts_both_rotors_to_rest(self):
        patch, osc, rot, clk, b = _rig({"speed": "fast"})
        x = _tone(1000.0, 4.0)
        for k in range(x.shape[0] // F):
            b._render_rotary(rot, F, {(osc.id, "out"): x[k * F:(k + 1) * F]}, patch)
        assert b._state[rot.id]["horn_f"] > 6.0
        rot.set_param("speed", "stop")
        x = _tone(1000.0, 30.0)
        for k in range(x.shape[0] // F):
            r = b._render_rotary(rot, F, {(osc.id, "out"): x[k * F:(k + 1) * F]}, patch)
        assert b._state[rot.id]["horn_f"] < 0.01
        assert b._state[rot.id]["drum_f"] < 0.05
        # Frozen: the last block's L and R have flat envelopes.
        assert _peaks(r["out_l"], 2.0).std() < 0.01

    def test_fast_gate_overrides_the_combo(self):
        x = _tone(1000.0, 3.0)
        gate = np.ones_like(x)
        L, R, M, b, rot = _run({"speed": "slow"}, x, gate=gate)
        assert b._state[rot.id]["horn_f"] > 6.0
        L, R, M, b, rot = _run({"speed": "fast"}, x, gate=np.zeros_like(x))
        assert b._state[rot.id]["horn_f"] == pytest.approx(0.7, abs=0.05)
        # A 40% duty pulse train in a block reads as low; 60% as high.
        L, R, M, b, rot = _run({"speed": "slow"}, x, gate=(np.arange(x.shape[0]) % 10 < 4).astype(np.float32))
        assert b._state[rot.id]["horn_f"] < 1.0
        L, R, M, b, rot = _run({"speed": "slow"}, x, gate=(np.arange(x.shape[0]) % 10 < 6).astype(np.float32))
        assert b._state[rot.id]["horn_f"] > 6.0


# ----- Balance, mix, crossover ----------------------------------------------


class TestBands:
    def test_balance_isolates_a_band(self):
        x = _tone(100.0, 1.0) + _tone(3000.0, 1.0)
        L, R, M, _b, _r = _run({"speed": "stop", "depth": 0.0, "balance": 1.0}, x)
        seg = L[SR // 2:].astype(np.float64)
        spec = np.abs(np.fft.rfft(seg)); fr = np.fft.rfftfreq(len(seg), 1 / SR)
        lo = spec[np.argmin(np.abs(fr - 100))]; hi = spec[np.argmin(np.abs(fr - 3000))]
        assert 20 * np.log10(hi / lo) > 40
        L, R, M, _b, _r = _run({"speed": "stop", "depth": 0.0, "balance": -1.0}, x)
        seg = L[SR // 2:].astype(np.float64)
        spec = np.abs(np.fft.rfft(seg))
        lo = spec[np.argmin(np.abs(fr - 100))]; hi = spec[np.argmin(np.abs(fr - 3000))]
        assert 20 * np.log10(lo / hi) > 40

    def test_crossover_moves_the_split(self):
        x = _tone(1500.0, 1.0)
        L1, *_ = _run({"speed": "stop", "depth": 0.0, "balance": 1.0, "crossover": 800.0}, x)
        L2, *_ = _run({"speed": "stop", "depth": 0.0, "balance": 1.0, "crossover": 3000.0}, x)
        assert np.abs(L1[SR // 2:]).max() > 3 * np.abs(L2[SR // 2:]).max()

    def test_mix_zero_is_the_delay_matched_dry(self):
        rng = np.random.RandomState(4)
        x = (0.3 * rng.randn(SR)).astype(np.float32)
        L, R, M, b, rot = _run({"speed": "fast", "mix": 0.0}, x)
        d = int(np.ceil(b._ROT_BASE_MS * 1e-3 * SR + b._ROT_HORN_R / b._ROT_C * SR))
        x = x[:L.shape[0]]
        assert np.array_equal(L[d:], x[:-d])           # an integer-sample read: exact
        assert np.array_equal(L, R)


# ----- Shape and blocks ------------------------------------------------------


LCM = 64000  # lcm(64, 128, 512, 1000): the switch points every block size shares
EVERYTHING = {"depth": 1.0, "spread": 0.7, "balance": 0.2, "mix": 0.6, "ramp": 0.5}


def _busy(secs=4.0):
    """Two tones (one per band) plus noise: every tap interpolates."""
    rng = np.random.default_rng(11)
    x = _tone(1000.0, secs, 0.3) + _tone(150.0, secs, 0.3)
    return (x + 0.1 * rng.standard_normal(x.shape[0])).astype(np.float32)


def _partition(n, block):
    """Block start samples: a fixed size, or ``"irregular"`` -- sizes from
    1 to 4500 (past the ring's 4096 headroom, so it grows mid-run) that
    still start a block on every multiple of LCM."""
    if block != "irregular":
        return list(range(0, n, block))
    rng = np.random.default_rng(3)
    starts, s = [], 0
    while s < n:
        starts.append(s)
        step = int(rng.choice([1, 17, 64, 333, 512, 1000, 2048, 4500]))
        s = min(s + step, (s // LCM + 1) * LCM)
    return starts


def _run_exact(params, x, block, gate=None, schedule=()):
    """Render through a fresh rotary in any partition; ``schedule`` =
    ((sample, {param: value}), ...) applied at a block START. Returns the
    (3, n) stack of out_l, out_r, out and the rotary's state."""
    patch, osc, rot, clk, b = _rig(params, block if isinstance(block, int) else F)
    n = x.shape[-1]
    starts = _partition(n, block)
    sched = dict(schedule)
    outs = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else n
        for k, v in sched.get(s, {}).items():
            rot.params[k] = v
        bufs = {(osc.id, "out"): x[..., s:e]}
        if gate is not None:
            bufs[(clk.id, "out")] = gate[s:e]
        r = b._render_rotary(rot, e - s, bufs, patch)
        outs.append(np.stack([r["out_l"], r["out_r"], r["out"]]))
    return np.concatenate(outs, axis=1), b._state[rot.id]


def _exact_case(case):
    """(params, gate, schedule) for each flavour of the 4 s exactness pin."""
    n = 4 * SR
    if case == "gate":
        # The fast jack toggling -- on shared boundaries, because the gate
        # is read as the block's MAJORITY level (by design), so its switch
        # time is block-quantised; the physics is what is compared.
        gate = ((np.arange(n) >= LCM) & (np.arange(n) < 2 * LCM)).astype(np.float32)
        return {**EVERYTHING, "speed": "slow"}, gate, ()
    if case == "switches":
        return ({**EVERYTHING, "speed": "fast"}, None,
                ((LCM, {"speed": "stop"}), (2 * LCM, {"speed": "slow"})))
    return {**EVERYTHING, "speed": case}, None, ()


class TestShape:
    @pytest.mark.parametrize("case", ["slow", "fast", "gate", "switches"])
    def test_bit_exact_at_every_block_size_over_four_seconds(self, case):
        """Both mics, both bands, the dry blend, the rotors spinning up,
        braking and switching: 64 / 128 / 512 / 1000 render the identical
        sample for four seconds at 48 kHz (2026-09-24).

        Before, the read formed ``absidx - delay`` over a ring index that
        wrapped at a different sample for every block size, and each
        rotor's angle was ``th0 + cumsum(f)`` re-wrapped mod 2*pi at every
        block end: measured against 512, slow differed at 1 / 2 / 2
        samples (64 / 128 / 1000), fast at 1 / 1 / 1, the gate at 0 / 0 /
        1, the switches at 0 / 1 / 2 -- a float32 ulp or less, a handful
        of samples in four seconds, which is why the old 2 s ``allclose``
        pin never saw it. Four seconds is part of the assertion."""
        x = _busy()
        params, gate, sched = _exact_case(case)
        ref, _ = _run_exact(params, x, 512, gate, sched)
        for block in (64, 128, 1000):
            got, _ = _run_exact(params, x, block, gate, sched)
            assert np.array_equal(got, ref), (case, block)

    def test_irregular_blocks_and_a_growing_ring_are_bit_exact(self):
        """Blocks of 1 to 4500 samples in no pattern render what 512s do.
        The ring used to be ``hist + frames`` long and was CLEARED --
        history, rotor rates and angles -- whenever a block of a new length
        arrived; it is now sized for 4096 up front, indexed by an absolute
        clock, and grows keeping its history. The rotor sums stay small:
        whole turns come off on an absolute 2**16-sample grid."""
        x = _busy()
        params, gate, _ = _exact_case("gate")
        ref, _ = _run_exact(params, x, 512, gate)
        got, st = _run_exact(params, x, "irregular", gate)
        assert np.array_equal(got, ref)
        assert st["L"] > st["hist"] + 4096           # it grew, mid-run
        assert st["n"] == x.shape[0]
        wrap_max = SR + NumpyBackend._ROT_WRAP * 12.0  # a turn + a grid of the top rate
        for rotor in ("horn", "drum"):
            assert 0.0 <= st[rotor + "_acc"] < wrap_max

    def test_voice_input_is_summed(self):
        x = _tone(1000.0, 0.5)
        v = np.stack([x * 0.5, x * 0.5, np.zeros_like(x)])
        a = _run({"speed": "fast"}, x)
        b = _run({"speed": "fast"}, v)
        assert np.allclose(a[0], b[0], atol=1e-6)


# ----- Example ---------------------------------------------------------------


class TestExample:
    def test_organ_leslie_example_plays_and_switches(self):
        path = Path(__file__).resolve().parent.parent / "examples" / "organ_leslie.json"
        patch = load_patch(path)
        rot = next(m for m in patch if m.TYPE == "rotary")
        b = NumpyBackend(sample_rate=44100, block_size=512)
        b.compile(patch)
        rates = []
        peak = 0.0
        for _k in range(int(12 * 44100 / 512)):
            out, _ = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            rates.append(b._state[rot.id]["horn_f"])
            peak = max(peak, float(np.abs(out).max()))
        assert np.asarray(out).shape[1] == 2
        rates = np.asarray(rates)
        assert rates.max() > 5.0 and rates.min() < 1.5      # it went fast AND slow
        assert peak > 0.02
