"""Pluck — the extended Karplus–Strong string.

Pins the contract: pitch accuracy within ±5 cents across C2..C6 (the
allpass fractional delay + damping-phase compensation earning their
keep), decay reading as a real t60 (within 10% at damping 0), damping
monotone (more damping = less energy left), retrigger-while-ringing
declicks (plucks superpose — no replace jump), per-hit seeded
determinism, exciter shaping (color brightens, position combs), voice
independence with mono ≡ single-voice parity, block-size independence
under constant pitch, silent-voice early-out to exact zeros, and
unpatched behaviour.

Velocity (the 2026-09-19 love pass): ``vel`` scales the burst, read at
the trigger edge and latched -- a cable holding 1.0 is bit-exact with no
cable, a soft hit is the burst scaled linearly (measured), a re-pluck
adds a burst scaled by ITS OWN velocity (superposition, click-free), a
non-positive velocity is a silent hit that leaves the string bit-exactly
as it was, mono broadcasts / voiced latches per row / a voiced bus on a
mono pluck takes the loudest voice, single voice ≡ mono, block-size
independence with the trigger mid-stream, the widget sweep, the example.

Velocity colour (the 2026-09-20 love pass): ``vel_color`` darkens a soft
hit's burst -- MEASURED as the spectral centroid of the first 50 ms (a
0.3-velocity hit at C4, ``color`` 0.8, ``vel_color`` 1: 3540 Hz against
5782 Hz at full velocity), a full hit is bit-exact with the knob off,
``vel_color`` 0 is the shipped path, a hit renders bit-exactly as a plain
hit at the effective colour (the formula, with both clamps), the loop is
untouched (t60 and loop state at damping 0, a re-pluck's tail slope),
block-size independence with hits mid-stream, per-voice latching, the
widget, the example.

Pitch/decay tests run at 44100 Hz (they measure real frequencies);
plumbing tests run fast at SR 1000.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend, _pluck_exciter
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules import pluck as _pluck  # noqa: F401


def _driver(params=None, sr=44100, block=512, vel=False):
    """Backend + `constant → pitch_cv, clock → trigger` pluck patch.

    ``vel=True`` cables a third ``constant`` into ``vel``; ``step`` then
    takes the velocity buffer as its third argument. Without it there is
    no ``vel`` cable at all (the unpatched path).
    """
    patch = Patch()
    m = patch.add_module("pluck", params=params or {})
    psrc = patch.add_module("constant")
    patch.connect(psrc.id, "out", m.id, "pitch_cv")
    tsrc = patch.add_module("clock")
    patch.connect(tsrc.id, "out", m.id, "trigger")
    vsrc = None
    if vel:
        vsrc = patch.add_module("constant")
        patch.connect(vsrc.id, "out", m.id, "vel")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(pitch_block, trig_block, vel_block=None):
        arr = np.asarray(pitch_block, dtype=np.float32)
        F = arr.shape[-1]
        buffers = {
            (psrc.id, "out"): arr,
            (tsrc.id, "out"): np.asarray(trig_block, dtype=np.float32),
        }
        if vsrc is not None:
            assert vel_block is not None, "a vel driver needs a vel buffer"
            buffers[(vsrc.id, "out")] = np.asarray(vel_block, dtype=np.float32)
        return b._render_pluck(patch.get(m.id), F, buffers, patch)

    step.module = m
    step.patch = patch
    step.backend = b
    return step


def _two_hit_render(vel_second, first=1.0, sr=44100, block=512, n_blocks=40,
                    params=None, second=True):
    """Hit 0 at block 0 sample 0 (velocity ``first``), hit 1 at block 20
    sample 100 (velocity ``vel_second``) -- a re-pluck while ringing.
    ``vel_second`` None renders with NO vel cable; ``second=False`` drops
    the second trigger entirely. Returns the mono signal."""
    params = params or {"decay": 3.0, "color": 0.5}
    step = _driver(params, sr=sr, block=block, vel=vel_second is not None)
    out = []
    for i in range(n_blocks):
        trig = np.zeros(block, dtype=np.float32)
        if i == 0:
            trig[0] = 1.0
        if i == 20 and second:
            trig[100] = 1.0
        pitch = np.zeros(block, dtype=np.float32)
        if vel_second is None:
            out.append(step(pitch, trig))
        else:
            vel = np.full(block, first if i < 20 else vel_second, dtype=np.float32)
            out.append(step(pitch, trig, vel))
    return np.concatenate(out)


def _render_pluck_tail(cv, seconds=1.0, sr=44100, params=None, block=512):
    """One pluck at t=0, rendered `seconds` long; returns the mono signal."""
    step = _driver(params, sr=sr, block=block)
    n_blocks = int(seconds * sr / block)
    out = []
    for i in range(n_blocks):
        trig = np.zeros(block, dtype=np.float32)
        if i == 0:
            trig[0] = 1.0
        out.append(step(np.full(block, cv, dtype=np.float32), trig))
    return np.concatenate(out)


def _partial_freq(sig, sr, f_near):
    """Frequency of the spectral peak within ±6% of ``f_near``.

    A bright pluck's strongest partial is often an upper harmonic, so the
    tuning tests measure the *fundamental partial* directly: the loop is
    a comb resonator, so partial 1 sits exactly at sr/loop_delay — the
    quantity the allpass tuning must get right. Hann window + parabolic
    interpolation for sub-bin accuracy.
    """
    w = np.hanning(len(sig))
    spec = np.abs(np.fft.rfft(sig * w))
    lo = max(1, int(len(sig) * f_near * 0.94 / sr))
    hi = min(len(spec) - 2, int(len(sig) * f_near * 1.06 / sr) + 1)
    k = lo + int(np.argmax(spec[lo : hi + 1]))
    a, b, c = spec[k - 1], spec[k], spec[k + 1]
    denom = a - 2 * b + c
    delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
    return (k + delta) * sr / len(sig)


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["pluck"]
    assert cls is get_module_type("pluck")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["pitch_cv", "trigger", "vel"]
    assert [p.signal_kind for p in m.input_ports] == ["cv", "gate", "cv"]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["decay"] == 2.0
    assert "vel" not in m.params  # knobless by house rule


def test_serialization_round_trip():
    cls = all_module_types()["pluck"]
    m = cls(2, params={"decay": 8.0, "color": 1.0, "position": 0.0})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- pitch accuracy --------------------------------------------------------


@pytest.mark.parametrize(
    "cv", [-2.0, -1.0, 0.0, 1.0, 2.0]  # C2, C3, C4, C5, C6
)
def test_pitch_within_5_cents(cv):
    sig = _render_pluck_tail(cv, seconds=0.9, params={"damping": 0.6})
    sr = 44100
    tail = sig[len(sig) // 3 :]  # skip the noisy attack
    expected = 261.6255653005986 * 2.0**cv
    measured = _partial_freq(tail, sr, expected)
    cents = 1200.0 * np.log2(measured / expected)
    assert abs(cents) < 5.0, f"cv {cv}: {measured:.2f} Hz vs {expected:.2f} ({cents:+.1f} ct)"


def test_fundamental_partial_is_present():
    # The fundamental must actually carry energy — a comb resonator with
    # a notched fundamental would pass the local-peak test on noise.
    sig = _render_pluck_tail(0.0, seconds=0.9, params={"position": 0.0, "damping": 0.8})
    tail = sig[len(sig) // 3 :]
    spec = np.abs(np.fft.rfft(tail * np.hanning(len(tail))))
    f0_bin = int(len(tail) * 261.6256 / 44100)
    local = spec[max(1, f0_bin - 4) : f0_bin + 5].max()
    assert local > 0.1 * spec.max()


# ----- decay / damping -------------------------------------------------------


def test_decay_reads_as_t60_within_10_percent():
    decay = 0.5
    sig = _render_pluck_tail(
        0.0, seconds=1.2, params={"decay": decay, "damping": 0.0}
    )
    sr = 44100
    hop = 1024
    rms = np.array(
        [np.sqrt(np.mean(sig[i : i + hop] ** 2)) for i in range(0, len(sig) - hop, hop)]
    )
    ref = rms[:4].max()
    below = np.flatnonzero(rms < ref * 10 ** (-60.0 / 20.0))
    assert len(below), "never decayed 60 dB"
    t60 = below[0] * hop / sr
    assert abs(t60 - decay) / decay < 0.10, f"t60 {t60:.3f}s vs {decay}s"


def test_damping_monotone_energy():
    energies = []
    for d in (0.0, 0.5, 1.0):
        sig = _render_pluck_tail(
            1.0, seconds=0.5, params={"damping": d, "decay": 2.0}
        )
        tail = sig[len(sig) // 2 :]
        energies.append(float(np.sum(tail**2)))
    assert energies[0] > energies[1] > energies[2]


# ----- retrigger / determinism ----------------------------------------------


def test_retrigger_while_ringing_does_not_click():
    """A re-pluck ADDS into the ring; a replace would jump discontinuously."""
    sr = 44100
    block = 512
    step = _driver({"decay": 3.0, "color": 0.5}, sr=sr, block=block)
    single = _driver({"decay": 3.0, "color": 0.5}, sr=sr, block=block)
    out_b, out_a = [], []
    for i in range(40):
        trig = np.zeros(block, dtype=np.float32)
        if i == 0:
            trig[0] = 1.0
        if i == 20:
            trig[100] = 1.0  # retrigger mid-ring
        out_b.append(step(np.zeros(block, dtype=np.float32), trig))
        trig1 = np.zeros(block, dtype=np.float32)
        if i == 0:
            trig1[0] = 1.0
        out_a.append(single(np.zeros(block, dtype=np.float32), trig1))
    b_ = np.concatenate(out_b)
    a = np.concatenate(out_a)
    # Sample-to-sample steps around the retrigger stay in the same class
    # as a fresh pluck's own attack — no replace-style discontinuity.
    k = 20 * block + 100
    jump = np.max(np.abs(np.diff(b_[k - 64 : k + 64])))
    attack_jump = np.max(np.abs(np.diff(a[:256])))
    assert jump <= attack_jump * 1.5 + 1e-6


def test_renders_are_deterministic():
    a = _render_pluck_tail(0.5, seconds=0.2, params={"color": 0.9})
    b = _render_pluck_tail(0.5, seconds=0.2, params={"color": 0.9})
    assert np.array_equal(a, b)


def test_consecutive_hits_use_fresh_noise():
    step = _driver({"decay": 0.2}, sr=1000, block=64)
    trig = np.zeros(64, dtype=np.float32)
    trig[0] = 1.0
    first = step(np.zeros(64, dtype=np.float32), trig).copy()
    for _ in range(30):  # let it die
        step(np.zeros(64, dtype=np.float32), np.zeros(64, dtype=np.float32))
    second = step(np.zeros(64, dtype=np.float32), trig).copy()
    assert not np.array_equal(first, second)  # hit #2 seeds differently


# ----- exciter shaping -------------------------------------------------------


def test_exciter_color_brightens():
    rng = np.random.default_rng(1)
    dark = _pluck_exciter(2048, 0.0, 0.0, np.random.default_rng(1))
    bright = _pluck_exciter(2048, 1.0, 0.0, np.random.default_rng(1))

    def hf_ratio(e):
        spec = np.abs(np.fft.rfft(e))
        half = len(spec) // 2
        return float(spec[half:].sum() / (spec[:half].sum() + 1e-12))

    assert hf_ratio(bright) > 4 * hf_ratio(dark)
    del rng


def test_exciter_position_comb_is_the_delayed_difference():
    # The comb contract, tested directly: out = raw − raw delayed by
    # position·n (acyclic — the first d samples pass un-combed), then
    # zero-meaned and peak-normalized.
    n = 1024
    raw = np.random.default_rng(3).uniform(-1.0, 1.0, n)
    e = _pluck_exciter(n, 1.0, 0.5, np.random.default_rng(3))
    d = n // 2
    expected = raw.copy()
    expected[d:] -= raw[:-d]
    expected -= expected.mean()
    expected /= np.max(np.abs(expected))
    assert np.allclose(e, expected, atol=1e-12)


def test_exciter_normalized_and_seeded():
    a = _pluck_exciter(256, 0.7, 0.2, np.random.default_rng(9))
    b = _pluck_exciter(256, 0.7, 0.2, np.random.default_rng(9))
    assert np.array_equal(a, b)
    assert np.isclose(np.max(np.abs(a)), 1.0)


# ----- voices / blocks / lifecycle ------------------------------------------


def test_voice_rows_are_independent_strings():
    step = _driver(sr=1000, block=64)
    pitch = np.zeros((2, 64), dtype=np.float32)
    trig = np.zeros((2, 64), dtype=np.float32)
    trig[0, 0] = 1.0  # pluck only voice 0
    out = step(pitch, trig)
    assert out.shape == (2, 64)
    assert np.any(out[0] != 0.0)
    assert np.all(out[1] == 0.0)


def test_single_voice_row_matches_mono():
    mono = _driver({"decay": 0.5}, sr=1000, block=64)
    voiced = _driver({"decay": 0.5}, sr=1000, block=64)
    trig = np.zeros(64, dtype=np.float32)
    trig[5] = 1.0
    for i in range(4):
        t = trig if i == 0 else np.zeros(64, dtype=np.float32)
        m = mono(np.zeros(64, dtype=np.float32), t)
        v = voiced(np.zeros((1, 64), dtype=np.float32), t[None, :])
        assert np.array_equal(m, v[0])


def test_block_size_independent_at_constant_pitch():
    trig_full = np.zeros(512, dtype=np.float32)
    trig_full[37] = 1.0
    big = _driver({"decay": 1.0}, sr=1000, block=512)
    small = _driver({"decay": 1.0}, sr=1000, block=64)
    out_big = big(np.zeros(512, dtype=np.float32), trig_full)
    outs = [
        small(np.zeros(64, dtype=np.float32), trig_full[i : i + 64])
        for i in range(0, 512, 64)
    ]
    assert np.array_equal(out_big, np.concatenate(outs))


def test_silent_voice_early_outs_to_exact_zeros():
    step = _driver({"decay": 0.15}, sr=1000, block=64)
    trig = np.zeros(64, dtype=np.float32)
    trig[0] = 1.0
    step(np.zeros(64, dtype=np.float32), trig)
    last = None
    for _ in range(40):
        last = step(
            np.zeros(64, dtype=np.float32), np.zeros(64, dtype=np.float32)
        )
    assert np.all(last == 0.0)


def test_unpatched_everything_is_silent_and_stateless():
    patch = Patch()
    m = patch.add_module("pluck")
    b = NumpyBackend(sample_rate=1000, block_size=64)
    b.compile(patch)
    out = b._render_pluck(patch.get(m.id), 64, {}, patch)
    assert np.all(out == 0.0)
    assert m.id not in b._state


# ----- velocity --------------------------------------------------------------


def test_vel_cable_holding_one_is_bit_exact_with_no_cable():
    """The feature ships OFF: unpatched is the old code path verbatim, and
    a bus at 1.0 multiplies by 1.0 -- `x * 1.0 == x` in IEEE. Mono with a
    re-pluck and a pitch change, then a (2, F) voiced string."""
    assert np.array_equal(_two_hit_render(None), _two_hit_render(1.0))

    plain = _driver({"decay": 1.0}, sr=1000, block=64)
    cabled = _driver({"decay": 1.0}, sr=1000, block=64, vel=True)
    for i in range(12):
        trig = np.zeros((2, 64), dtype=np.float32)
        pitch = np.zeros((2, 64), dtype=np.float32)
        pitch[1] = 0.5 if i < 6 else -0.5
        if i == 0:
            trig[0, 3] = 1.0
        if i in (2, 6):
            trig[1, 40] = 1.0
        a = plain(pitch, trig)
        b_ = cabled(pitch, trig, np.ones((2, 64), dtype=np.float32))
        assert np.array_equal(a, b_)


def test_vel_is_read_at_the_edge_and_latched():
    """Only the edge sample matters, and it holds for the whole hit: a bus
    that is 1.0 everywhere except 0.5 AT the edge equals a constant 0.5,
    and a bus that moves after the edge (2.0 for every later block) does
    not touch the ringing string."""
    sr, block = 1000, 64
    ref = _driver({"decay": 0.5}, sr=sr, block=block, vel=True)
    spike = _driver({"decay": 0.5}, sr=sr, block=block, vel=True)
    later = _driver({"decay": 0.5}, sr=sr, block=block, vel=True)
    first = None
    for i in range(10):
        trig = np.zeros(block, dtype=np.float32)
        if i == 0:
            trig[7] = 1.0
        pitch = np.full(block, -3.0, dtype=np.float32)  # C1: a real loop at SR 1000
        v_ref = np.full(block, 0.5, dtype=np.float32)
        v_spike = np.ones(block, dtype=np.float32)
        if i == 0:
            v_spike[7] = 0.5
        v_later = np.full(block, 0.5 if i == 0 else 2.0, dtype=np.float32)
        a = ref(pitch, trig, v_ref)
        assert np.array_equal(a, spike(pitch, trig, v_spike))
        assert np.array_equal(a, later(pitch, trig, v_later))
        first = a if first is None else first
    assert np.any(first != 0.0)


@pytest.mark.parametrize("vel", [0.5, 0.25])
def test_soft_hit_is_the_burst_scaled_linearly(vel):
    """A fresh string is a linear loop from a zero state, so the whole
    render -- and its measured peak -- scales with the velocity, while it
    rings down the same way (the normalized signals coincide)."""
    def render(v):
        step = _driver({"decay": 0.8}, sr=44100, block=512, vel=True)
        out = []
        for i in range(30):
            trig = np.zeros(512, dtype=np.float32)
            if i == 0:
                trig[0] = 1.0
            out.append(step(np.zeros(512, dtype=np.float32), trig,
                            np.full(512, v, dtype=np.float32)))
        return np.concatenate(out)

    full, soft = render(1.0), render(vel)
    ratio = float(np.abs(soft).max()) / float(np.abs(full).max())
    assert abs(ratio - vel) < 1e-3, ratio
    assert np.allclose(soft, vel * full, atol=1e-6)
    # Same ring-down: the late/early energy ratio is velocity-independent.
    def decay_ratio(sig):
        return float(np.sum(sig[-4096:] ** 2) / np.sum(sig[:4096] ** 2))
    assert np.isclose(decay_ratio(soft), decay_ratio(full), rtol=1e-4)


def test_re_pluck_adds_a_burst_scaled_by_its_own_vel():
    """Superposition: hit 1 lands on a string still ringing from hit 0 (at
    velocity 1). Its contribution is linear in ITS velocity: with hit-1
    velocities 1.0 / 0.5 / 0.25 (same seed, same ring before the hit, so
    the relock/allpass-clear transient cancels in the differences),
    (B - A) == 2 (A - D). And it stays click-free, as before."""
    b_ = _two_hit_render(1.0)
    a = _two_hit_render(0.5)
    d = _two_hit_render(0.25)
    k = 20 * 512 + 100
    assert np.any(b_[k:] != a[k:])
    assert np.allclose(b_ - a, 2.0 * (a - d), atol=1e-5)
    # The click check from the full-velocity retrigger test, at 0.5.
    single = _two_hit_render(None, n_blocks=2)
    jump = np.max(np.abs(np.diff(a[k - 64 : k + 64])))
    attack_jump = np.max(np.abs(np.diff(single[:256])))
    assert jump <= attack_jump * 1.5 + 1e-6


@pytest.mark.parametrize("vel", [0.0, -0.5])
def test_non_positive_vel_is_a_silent_hit(vel):
    """No burst, and no relock or allpass clear either -- the string keeps
    ringing BIT-EXACTLY as it was (the clear alone would step the output
    by ~7-18% of the ring, measured). The hit still counts, and the next
    real hit plucks as normal."""
    silent = _two_hit_render(vel)
    alone = _two_hit_render(None, second=False)
    assert np.array_equal(silent, alone)
    assert np.any(silent[20 * 512 :] != 0.0)
    # ...and a bare re-pluck (no cable) really is a different string.
    assert not np.array_equal(_two_hit_render(None), alone)

    step = _driver({"decay": 0.5}, sr=1000, block=64, vel=True)
    trig = np.zeros(64, dtype=np.float32)
    trig[0] = 1.0
    zero = np.zeros(64, dtype=np.float32)
    low = np.full(64, -3.0, dtype=np.float32)  # C1: a real loop at SR 1000
    step(low, trig, np.ones(64, dtype=np.float32))
    step(low, trig, np.full(64, vel, dtype=np.float32))
    assert int(step.backend._state[step.module.id]["hits"][0]) == 2
    quiet = step(low, zero, np.ones(64, dtype=np.float32))
    loud = step(low, trig, np.ones(64, dtype=np.float32))
    assert np.abs(loud).max() > 2.0 * np.abs(quiet).max()
    assert int(step.backend._state[step.module.id]["hits"][0]) == 3


def test_vel_mono_broadcasts_and_voiced_latches_per_row():
    """(2, F) strings hit together: a (2, F) bus scales each from its own
    row (0.5 / 1.0 -> voice 0 halved, voice 1 untouched to the bit); a mono
    bus at 0.5 halves both. Low pitches (C1 / G1) so the strings ring for
    the whole render at SR 1000 -- a C4 loop there is ~4 samples and dies
    into the early-out floor, which is an absolute threshold and so NOT
    linear in velocity."""
    sr, block = 1000, 64

    def render(vel_rows):
        step = _driver({"decay": 2.0}, sr=sr, block=block, vel=True)
        out = []
        for i in range(8):
            trig = np.zeros((2, block), dtype=np.float32)
            if i == 0:
                trig[:, 4] = 1.0
            pitch = np.full((2, block), -3.0, dtype=np.float32)
            pitch[1] = -2.5
            if vel_rows is None:
                vel = np.full(block, 0.5, dtype=np.float32)
            else:
                vel = np.array(vel_rows, dtype=np.float32)[:, None] * np.ones(
                    (2, block), dtype=np.float32
                )
            out.append(step(pitch, trig, vel))
        return np.concatenate(out, axis=1)

    both = render([1.0, 1.0])
    rows = render([0.5, 1.0])
    mono = render(None)
    assert both.shape == (2, 8 * block)
    assert np.allclose(rows[0], 0.5 * both[0], atol=1e-6)
    assert np.array_equal(rows[1], both[1])
    assert np.allclose(mono, 0.5 * both, atol=1e-6)
    assert np.abs(both[:, -block:]).max() > 1e-3  # still ringing, above the floor


def test_voiced_vel_on_mono_pluck_takes_loudest_voice_at_edge():
    """A (3, F) bus on a mono string collapses to the max at the edge
    (an idle slot's velocity is 0, so the max is the key that was struck)
    -- bit-exact with a mono bus at that value, shape still (F,)."""
    sr, block = 1000, 64
    poly = _driver({"decay": 0.6}, sr=sr, block=block, vel=True)
    mono = _driver({"decay": 0.6}, sr=sr, block=block, vel=True)
    for i in range(6):
        trig = np.zeros(block, dtype=np.float32)
        if i == 0:
            trig[9] = 1.0
        pitch = np.zeros(block, dtype=np.float32)
        vel3 = np.zeros((3, block), dtype=np.float32)
        vel3[0] = 0.2
        vel3[1] = 0.7
        a = poly(pitch, trig, vel3)
        b_ = mono(pitch, trig, np.full(block, 0.7, dtype=np.float32))
        assert a.shape == (block,)
        assert np.array_equal(a, b_)
    assert np.any(a != 0.0)


def test_single_voice_row_matches_mono_with_vel():
    mono = _driver({"decay": 0.5}, sr=1000, block=64, vel=True)
    voiced = _driver({"decay": 0.5}, sr=1000, block=64, vel=True)
    trig = np.zeros(64, dtype=np.float32)
    trig[5] = 1.0
    vel = np.full(64, 0.6, dtype=np.float32)
    for i in range(4):
        t = trig if i == 0 else np.zeros(64, dtype=np.float32)
        m = mono(np.zeros(64, dtype=np.float32), t, vel)
        v = voiced(np.zeros((1, 64), dtype=np.float32), t[None, :], vel[None, :])
        assert np.array_equal(m, v[0])
    assert np.any(m != 0.0)


def test_block_size_independent_with_vel_mid_stream():
    """Constant pitch; the bus is a ramp (so the value AT the edge is what
    counts); a hit at sample 5, a silent hit at 300 on the ringing string
    and a soft hit at 400 in the next block. 2x512 == 16x64 to the bit.

    Stays above the -100 dB early-out floor on purpose (long decay, a
    C1 loop): the early-out is decided on the BLOCK peak, so below that
    floor a small block goes to exact zero a little before a big one
    would -- shipped behaviour, block-size dependent by construction and
    inaudible, but not this test's claim."""
    F = 1024
    trig = np.zeros(F, dtype=np.float32)
    trig[[5, 300 + 512, 400 + 512]] = 1.0
    vel = np.linspace(0.2, 1.4, F).astype(np.float32)
    vel[300 + 512] = -1.0
    pitch = np.full(F, -3.0, dtype=np.float32)
    big = _driver({"decay": 4.0}, sr=1000, block=512, vel=True)
    small = _driver({"decay": 4.0}, sr=1000, block=64, vel=True)
    out_big = np.concatenate(
        [big(pitch[i : i + 512], trig[i : i + 512], vel[i : i + 512])
         for i in range(0, F, 512)]
    )
    out_small = np.concatenate(
        [small(pitch[i : i + 64], trig[i : i + 64], vel[i : i + 64])
         for i in range(0, F, 64)]
    )
    assert np.array_equal(out_big, out_small)
    assert np.abs(out_big[-64:]).max() > 1e-3  # never reached the floor
    assert np.abs(out_big[912:976]).max() > np.abs(out_big[848:912]).max()


# ----- vel_color -------------------------------------------------------------


def _centroid(sig, sr=44100):
    """Hann-windowed magnitude-spectrum centroid in Hz -- the house
    brightness observable (centroid, not flatness)."""
    w = np.hanning(len(sig))
    spec = np.abs(np.fft.rfft(sig * w))
    f = np.fft.rfftfreq(len(sig), 1.0 / sr)
    return float((f * spec).sum() / (spec.sum() + 1e-20))


def _one_hit(vel, params, n_blocks=6, block=512, cv=0.0, sr=44100):
    """One hit at sample 0 (velocity ``vel``; None = no vel cable),
    ``n_blocks`` long. Six blocks of 512 is ~70 ms -- the first 50 ms
    (the burst and its first laps round the loop) is what the centroid
    tests window."""
    step = _driver(params, sr=sr, block=block, vel=vel is not None)
    out = []
    for i in range(n_blocks):
        trig = np.zeros(block, dtype=np.float32)
        if i == 0:
            trig[0] = 1.0
        pitch = np.full(block, cv, dtype=np.float32)
        if vel is None:
            out.append(step(pitch, trig))
        else:
            out.append(step(pitch, trig, np.full(block, vel, dtype=np.float32)))
    return np.concatenate(out)


_N50 = int(0.05 * 44100)


def test_vel_color_soft_hit_is_duller():
    """The feature, measured: at ``vel_color`` 1 a 0.3-velocity hit's
    first 50 ms has a lower spectral centroid than a full hit's --
    3540 Hz vs 5782 Hz measured at C4 with ``color`` 0.8 (ratio 0.61),
    pinned below 0.75. At ``vel_color`` 0 the two hits are the same burst
    scaled, so the centroid ratio is 1 (to float noise). Halfway
    (``vel_color`` 0.5) sits between (measured 4460 Hz)."""
    soft = _centroid(_one_hit(0.3, {"color": 0.8, "vel_color": 1.0})[:_N50])
    full = _centroid(_one_hit(1.0, {"color": 0.8, "vel_color": 1.0})[:_N50])
    assert 3000.0 < soft < 4000.0, soft
    assert 5300.0 < full < 6300.0, full
    assert soft / full < 0.75
    half = _centroid(_one_hit(0.3, {"color": 0.8, "vel_color": 0.5})[:_N50])
    assert soft < half < full
    off_soft = _centroid(_one_hit(0.3, {"color": 0.8, "vel_color": 0.0})[:_N50])
    off_full = _centroid(_one_hit(1.0, {"color": 0.8, "vel_color": 0.0})[:_N50])
    assert np.isclose(off_soft, off_full, rtol=1e-3)
    assert np.isclose(off_full, full, rtol=1e-12)


def test_vel_color_full_hit_is_bit_exact_with_the_knob_off():
    """The anchor: the formula is ``color + vel_color * (vel - 1)``, and
    at vel 1.0 that is ``color + 0.0`` -- ``color`` itself in IEEE. So a
    bus holding 1.0 renders bit-for-bit the same at ``vel_color`` 1 as at
    0, and so does an UNPATCHED ``vel`` (the knob cannot change a patch
    with no velocity source). Mono with a re-pluck and a pitch step, then
    a (2, F) string."""
    on = {"decay": 3.0, "color": 0.5, "vel_color": 1.0}
    off = {"decay": 3.0, "color": 0.5, "vel_color": 0.0}
    assert np.array_equal(_two_hit_render(1.0, params=on), _two_hit_render(1.0, params=off))
    assert np.array_equal(_two_hit_render(None, params=on), _two_hit_render(None, params=off))

    plain = _driver({"decay": 1.0, "vel_color": 0.0}, sr=1000, block=64, vel=True)
    knob = _driver({"decay": 1.0, "vel_color": 1.0}, sr=1000, block=64, vel=True)
    for i in range(12):
        trig = np.zeros((2, 64), dtype=np.float32)
        pitch = np.zeros((2, 64), dtype=np.float32)
        pitch[1] = 0.5 if i < 6 else -0.5
        if i == 0:
            trig[0, 3] = 1.0
        if i in (2, 6):
            trig[1, 40] = 1.0
        ones = np.ones((2, 64), dtype=np.float32)
        assert np.array_equal(plain(pitch, trig, ones), knob(pitch, trig, ones))


def test_vel_color_zero_is_the_shipped_path():
    """``vel_color`` 0 (the default) with ANY velocity is what shipped:
    the key absent and the key at 0.0 render bit-exactly the same for a
    soft re-pluck (the reference-render recipe pinned the shipped code
    itself before the change; this keeps the default honest)."""
    absent = {"decay": 3.0, "color": 0.5}
    zero = {"decay": 3.0, "color": 0.5, "vel_color": 0.0}
    assert np.array_equal(_two_hit_render(0.4, params=absent), _two_hit_render(0.4, params=zero))


@pytest.mark.parametrize(
    "color, vel_color, vel",
    [(0.8, 1.0, 0.3), (0.7, 0.8, 0.55), (0.5, 1.0, 0.3), (0.8, 1.0, 1.5)],
)
def test_vel_color_hit_equals_a_plain_hit_at_the_effective_colour(color, vel_color, vel):
    """The contract pinned literally: a hit at (color, vel_color, vel)
    is bit-exact with a plain hit (``vel_color`` 0) at the same velocity
    whose ``color`` IS ``clamp(color + vel_color * (vel - 1), 0, 1)`` --
    the same float expression, so the same exciter. The third case
    clamps at 0 (0.5 - 0.7), the fourth at 1 (a hit above full velocity
    brightens, to the ceiling)."""
    scale = float(np.float32(vel))  # the bus is float32; the edge read is its value
    eff = min(1.0, max(0.0, color + vel_color * (scale - 1.0)))
    if color == 0.5:
        assert eff == 0.0
    if vel == 1.5:
        assert eff == 1.0
    a = _one_hit(vel, {"color": color, "vel_color": vel_color, "decay": 2.0})
    b_ = _one_hit(vel, {"color": eff, "vel_color": 0.0, "decay": 2.0})
    assert np.array_equal(a, b_)
    assert np.any(a != 0.0)


def test_vel_color_leaves_the_loop_alone():
    """Only the exciter sees the colour. At ``damping`` 0 the loop gain
    is frequency-independent, so the t60 of a 0.3-velocity hit is the
    same to the hop with ``vel_color`` 1 as with 0 (measured 0.5108 s
    both, ``decay`` 0.5), the tail's log-RMS slope agrees within 1%, the
    carried loop state (``g``, ``ap_c``, ``n_int``) is identical, and a
    re-pluck's ring-down slope after the second hit agrees too. (At
    ``damping`` > 0 the ENVELOPE does differ -- a duller burst has less
    HF for the damping to eat -- which is the burst meeting the loop, not
    the loop changing; hence the damping-0 pin.)"""
    sr, hop = 44100, 1024

    def rms_env(sig):
        return np.array(
            [np.sqrt(np.mean(sig[i : i + hop] ** 2)) for i in range(0, len(sig) - hop, hop)]
        )

    def t60_of(sig):
        rms = rms_env(sig)
        ref = rms[:4].max()
        below = np.flatnonzero(rms < ref * 10 ** (-60.0 / 20.0))
        assert len(below), "never decayed 60 dB"
        return below[0] * hop / sr

    def slope(sig, lo, hi):
        rms = rms_env(sig)
        seg = np.log(rms[lo:hi] + 1e-30)
        return float(np.polyfit(np.arange(len(seg)), seg, 1)[0])

    outs, states = [], []
    for vc in (0.0, 1.0):
        params = {"decay": 0.5, "damping": 0.0, "color": 0.8, "vel_color": vc}
        step = _driver(params, sr=sr, block=512, vel=True)
        out = []
        for i in range(int(1.2 * sr / 512)):
            trig = np.zeros(512, dtype=np.float32)
            if i == 0:
                trig[0] = 1.0
            out.append(step(np.zeros(512, dtype=np.float32), trig,
                            np.full(512, 0.3, dtype=np.float32)))
        outs.append(np.concatenate(out))
        st = step.backend._state[step.module.id]
        states.append((st["g"].copy(), st["ap_c"].copy(), st["n_int"].copy()))
    assert not np.array_equal(outs[0], outs[1])  # the burst DID change
    assert abs(t60_of(outs[0]) - t60_of(outs[1])) <= hop / sr
    assert abs(t60_of(outs[1]) - 0.5) / 0.5 < 0.10
    s0, s1 = slope(outs[0], 4, 20), slope(outs[1], 4, 20)
    assert s0 < 0 and abs(s1 - s0) / abs(s0) < 0.01, (s0, s1)
    for a, b_ in zip(states[0], states[1]):
        assert np.array_equal(a, b_)

    # A re-pluck on the ringing string: hit 0 full, hit 1 at 0.3 (block 20,
    # sample 100); the slope of the ring-down after hit 1 is the same.
    base = {"decay": 0.5, "damping": 0.0, "color": 0.8}
    two_off = _two_hit_render(0.3, params={**base, "vel_color": 0.0}, n_blocks=60)
    two_on = _two_hit_render(0.3, params={**base, "vel_color": 1.0}, n_blocks=60)
    assert not np.array_equal(two_off, two_on)
    after = 20 * 512 + 100
    s_off = slope(two_off[after:], 4, 20)
    s_on = slope(two_on[after:], 4, 20)
    assert s_off < 0 and abs(s_on - s_off) / abs(s_off) < 0.01, (s_off, s_on)


def test_vel_color_block_size_independent_mid_stream():
    """The mid-stream twin of the vel test with the colour live: a ramp
    bus (the value AT the edge is what colours the hit), a hit at 5, a
    silent hit at 812 and a soft hit at 912; ``vel_color`` 1. 2x512 ==
    16x64 to the bit, and the soft hit is really a duller one (its burst
    differs from the ``vel_color`` 0 render)."""
    F = 1024
    trig = np.zeros(F, dtype=np.float32)
    trig[[5, 300 + 512, 400 + 512]] = 1.0
    vel = np.linspace(0.2, 1.4, F).astype(np.float32)
    vel[300 + 512] = -1.0
    pitch = np.full(F, -3.0, dtype=np.float32)
    params = {"decay": 4.0, "color": 0.8, "vel_color": 1.0}
    big = _driver(params, sr=1000, block=512, vel=True)
    small = _driver(params, sr=1000, block=64, vel=True)
    off = _driver({**params, "vel_color": 0.0}, sr=1000, block=512, vel=True)
    out_big = np.concatenate(
        [big(pitch[i : i + 512], trig[i : i + 512], vel[i : i + 512])
         for i in range(0, F, 512)]
    )
    out_small = np.concatenate(
        [small(pitch[i : i + 64], trig[i : i + 64], vel[i : i + 64])
         for i in range(0, F, 64)]
    )
    out_off = np.concatenate(
        [off(pitch[i : i + 512], trig[i : i + 512], vel[i : i + 512])
         for i in range(0, F, 512)]
    )
    assert np.array_equal(out_big, out_small)
    assert not np.array_equal(out_big, out_off)
    assert np.abs(out_big[-64:]).max() > 1e-3  # never reached the floor


def test_vel_color_latches_per_voice():
    """Two strings hit together with a (2, F) bus at 0.3 / 1.0 and
    ``vel_color`` 1: voice 0 is duller (centroid ratio < 0.75, the same
    figure as mono), voice 1 is bit-exact with the ``vel_color`` 0 render
    (a full hit is a full hit), and voice 0 is bit-exact with a MONO
    string at 0.3 (single voice ≡ mono, colour included)."""
    sr, block = 44100, 512

    def render(vel_color):
        step = _driver({"color": 0.8, "vel_color": vel_color}, sr=sr, block=block, vel=True)
        out = []
        for i in range(6):
            trig = np.zeros((2, block), dtype=np.float32)
            if i == 0:
                trig[:, 0] = 1.0
            pitch = np.zeros((2, block), dtype=np.float32)
            vel = np.ones((2, block), dtype=np.float32)
            vel[0] = 0.3
            out.append(step(pitch, trig, vel))
        return np.concatenate(out, axis=1)

    on, off = render(1.0), render(0.0)
    assert on.shape == (2, 6 * block)
    c0, c1 = _centroid(on[0, :_N50]), _centroid(on[1, :_N50])
    assert c0 / c1 < 0.75, (c0, c1)
    assert np.array_equal(on[1], off[1])
    assert not np.array_equal(on[0], off[0])
    mono = _one_hit(0.3, {"color": 0.8, "vel_color": 1.0})
    assert np.array_equal(on[0], mono)


# ----- widgets ---------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("pluck")
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
    for name in get_module_type("pluck").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["decay"][1].endswith(" s")
    assert w["vel_color"][0] == "add_slider_float"  # 0..1, beside color
    assert "vel" not in w  # a jack, not a knob


# ----- example ---------------------------------------------------------------


def test_the_velocity_example_accents_each_hit():
    """examples/pluck_velocity.json: a shift_random accent loop into
    pluck.vel. Rendered with and without the vel cable, each hit's onset
    peak (first 256 samples after its gate edge) scales by the bus value
    at that edge -- measured within +-0.01, pinned at +-0.05 -- and the
    line has a real dynamic spread."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "pluck_velocity.json"

    def render(strip_vel):
        patch = load_patch(path)
        seq = next(m for m in patch if m.TYPE == "sequencer")
        off = next(m for m in patch if m.TYPE == "cv_offset")
        if strip_vel:
            patch.cables.remove(next(c for c in patch.cables if c.dst_port == "vel"))
        np.random.seed(0)
        b = NumpyBackend(sample_rate=44100, block_size=512)
        b.compile(patch)
        outs, vels, gates = [], [], []
        orig = b._render_pluck

        def spy(module, frames, buffers, p):
            vb = buffers.get((off.id, "out"))
            vels.append(np.zeros(frames) if vb is None else np.asarray(vb).copy())
            gates.append(np.asarray(buffers[(seq.id, "gate")]).copy())
            r = orig(module, frames, buffers, p)
            outs.append(np.asarray(r).copy())
            return r

        b._render_pluck = spy
        peak = 0.0
        for _ in range(int(44100 * 4 / 512)):
            out, _devices = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        assert 0.1 < peak < 1.0
        return (np.concatenate(outs), np.concatenate(vels),
                np.concatenate(gates) > 0.5)

    assert len(load_patch(path)) <= 12
    with_vel, vel, gate = render(False)
    without, _, _ = render(True)
    edges = np.flatnonzero(gate[1:] & ~gate[:-1]) + 1
    assert len(edges) >= 12
    vels = np.array([vel[e] for e in edges])
    peaks = np.array([float(np.abs(with_vel[e : e + 256]).max()) for e in edges])
    plain = np.array([float(np.abs(without[e : e + 256]).max()) for e in edges])
    assert np.all(np.abs(peaks / plain - vels) < 0.05)
    assert np.corrcoef(peaks, vels)[0, 1] > 0.9
    assert peaks.max() > 1.8 * peaks.min()
    assert vels.min() >= 0.3 and vels.max() <= 1.0


def test_the_velocity_color_example_dulls_the_soft_picks():
    """examples/pluck_velocity_color.json: the accent loop into ``vel``
    with ``vel_color`` 0.8. Rendered as shipped and again with the knob
    forced to 0 (the pitch confound removed -- the line's own centroid
    swings 3650..5190 Hz note to note), each hit's first-50 ms centroid
    ratio (with / without) tracks its velocity: measured 0.61 at vel 0.36
    up to 0.96 at 0.92, correlation 0.97 over 14 hits; pinned every
    ratio < 0.98, the soft picks (< 0.5) under 0.8, correlation > 0.9.
    Peak 0.40 (pinned 0.3..0.8), 8 modules."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "pluck_velocity_color.json"

    def render(vel_color):
        patch = load_patch(path)
        seq = next(m for m in patch if m.TYPE == "sequencer")
        off = next(m for m in patch if m.TYPE == "cv_offset")
        pl = next(m for m in patch if m.TYPE == "pluck")
        if vel_color is not None:
            pl.params["vel_color"] = vel_color
        np.random.seed(0)
        b = NumpyBackend(sample_rate=44100, block_size=512)
        b.compile(patch)
        outs, vels, gates = [], [], []
        orig = b._render_pluck

        def spy(module, frames, buffers, p):
            vb = buffers.get((off.id, "out"))
            vels.append(np.zeros(frames) if vb is None else np.asarray(vb).copy())
            gates.append(np.asarray(buffers[(seq.id, "gate")]).copy())
            r = orig(module, frames, buffers, p)
            outs.append(np.asarray(r).copy())
            return r

        b._render_pluck = spy
        peak = 0.0
        for _ in range(int(44100 * 4 / 512)):
            out, _devices = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        assert 0.3 < peak < 0.8, peak
        return (np.concatenate(outs), np.concatenate(vels),
                np.concatenate(gates) > 0.5)

    patch = load_patch(path)
    assert len(patch) <= 12
    assert next(m for m in patch if m.TYPE == "pluck").params["vel_color"] == 0.8
    shipped, vel, gate = render(None)
    plain, _, _ = render(0.0)
    edges = np.flatnonzero(gate[1:] & ~gate[:-1]) + 1
    assert len(edges) >= 12
    vels = np.array([vel[e] for e in edges])
    ratios = np.array(
        [_centroid(shipped[e : e + _N50]) / _centroid(plain[e : e + _N50]) for e in edges]
    )
    assert np.all(ratios < 0.98), ratios
    assert ratios.min() < 0.7
    assert np.any(vels < 0.5) and ratios[vels < 0.5].max() < 0.8
    assert np.corrcoef(ratios, vels)[0, 1] > 0.9
    assert vels.min() >= 0.3 and vels.max() <= 1.0
