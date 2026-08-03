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

Pitch/decay tests run at 44100 Hz (they measure real frequencies);
plumbing tests run fast at SR 1000.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend, _pluck_exciter
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules import pluck as _pluck  # noqa: F401


def _driver(params=None, sr=44100, block=512):
    """Backend + `constant → pitch_cv, clock → trigger` pluck patch."""
    patch = Patch()
    m = patch.add_module("pluck", params=params or {})
    psrc = patch.add_module("constant")
    patch.connect(psrc.id, "out", m.id, "pitch_cv")
    tsrc = patch.add_module("clock")
    patch.connect(tsrc.id, "out", m.id, "trigger")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(pitch_block, trig_block):
        arr = np.asarray(pitch_block, dtype=np.float32)
        F = arr.shape[-1]
        return b._render_pluck(
            patch.get(m.id),
            F,
            {
                (psrc.id, "out"): arr,
                (tsrc.id, "out"): np.asarray(trig_block, dtype=np.float32),
            },
            patch,
        )

    step.module = m
    step.patch = patch
    step.backend = b
    return step


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
    assert [p.name for p in m.input_ports] == ["pitch_cv", "trigger"]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["decay"] == 2.0


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
