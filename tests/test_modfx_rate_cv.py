"""``flanger`` / ``phaser`` ``rate_cv``, read PER SAMPLE (2026-10-12).

It was a block mean: the rate stepped once a block, a moving CV
re-anchored the sweep every block, and so a modulated flanger or phaser
was the one render of either that differed at every block size. Both now
take the chorus's road (2026-09-24): the knob's sweep runs exactly as
before -- an anchor plus an integer sample count (``_mod_free_phase``) --
and the CV's share, the instantaneous rate minus the knob's, is summed
per sample on an integer grid of 2**48 steps per cycle
(``_mod_rate_cv_phase``). Integer addition is associative, so the sum at
a sample is the same number however the stream was cut.

Pinned here: a modulated sweep is bit-exact at 64 / 128 / 512 / 1000 over
four seconds (the float output AND the DSP state); a zero CV is
bit-identical to an unpatched jack; a non-finite sample is no modulation;
the rate moves WITHIN a block; the flanger tracks an ideal float64
integral of the rate to half a float32 ulp (the block mean missed it by
0.04 at 512 and 0.15 at 1000); and while the ``clock`` lock holds,
rate_cv steps aside and its running share is folded into the anchor.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
N = 4 * SR


def _noise(seed, n=N):
    return (np.random.default_rng(seed).standard_normal(n) * 0.3).astype(np.float32)


def _sine(hz, amp, n=N):
    return (amp * np.sin(2 * np.pi * hz * np.arange(n) / SR)).astype(np.float32)


def _render(mtype, params, jacks, block, n=N, sizes=None):
    """Drive ``_render_<mtype>`` block by block (a short last block too);
    returns the (2, n) output and the module's state."""
    patch = Patch()
    m = patch.add_module(mtype, params=dict(params))
    srcs = {}
    for port in jacks:
        if port == "in":
            s, op = patch.add_module("oscillator"), "out"
        elif port == "clock":
            s, op = patch.add_module("clock"), "out"
        else:
            s, op = patch.add_module("lfo"), "cv"
        patch.connect(s.id, op, m.id, port)
        srcs[port] = (s.id, op)
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    fn = getattr(b, "_render_" + mtype)
    if sizes is None:
        sizes = [block] * (n // block) + ([n % block] if n % block else [])
    outs, k = [], 0
    for f in sizes:
        o = fn(m, f, {srcs[p]: v[..., k:k + f] for p, v in jacks.items()}, patch)
        outs.append(np.stack([o["out_l"], o["out_r"]]))
        k += f
    return np.concatenate(outs, axis=-1), b._state[m.id]


_CASES = {
    "flanger": ("flanger", {"manual": 6.0, "depth": 1.0, "rate": 0.8,
                            "spread": 0.3, "feedback": -0.6, "cv_depth": 1.0}),
    "flanger_tz": ("flanger", {"through_zero": True, "spread": 1.0, "feedback": 0.7,
                               "depth": 0.9, "manual": 4.0, "rate": 2.0,
                               "polarity": -1.0, "cv_depth": 1.5}),
    "phaser": ("phaser", {"stages": 8, "spread": 1.0, "feedback": 0.9,
                          "depth": 1.0, "rate": 0.7, "cv_depth": 1.0}),
}
_STATE = {"flanger": ("buf", "write_idx", "rc_acc", "phase", "ph_n"),
          "phaser": ("s", "yprev", "rc_acc", "phase", "ph_n")}


@pytest.mark.parametrize("case", list(_CASES))
def test_modulated_rate_is_bit_exact_across_block_sizes(case):
    # FOUR SECONDS of a 0.3 Hz sine on rate_cv (+/- cv_depth octaves),
    # 1.2 cycles of the CV, at four block sizes sharing no alignment; 1000
    # ends on a short block. manual_cv rides along on the through-zero
    # flanger and the phaser, so every jack is live. Under the block mean
    # every one of these differed from 512 almost everywhere.
    mtype, params = _CASES[case]
    jacks = {"in": _noise(1), "rate_cv": _sine(0.3, 1.0)}
    if case != "flanger":
        jacks["manual_cv"] = _sine(0.23, 0.8)
    ref, ref_st = _render(mtype, params, jacks, 512)
    assert np.all(np.isfinite(ref)) and float(np.abs(ref).max()) > 0.1
    for block in (64, 128, 1000):
        got, st = _render(mtype, params, jacks, block)
        bad = int(np.count_nonzero(got != ref))
        assert bad == 0, (case, block, bad, float(np.abs(got - ref).max()))
        for key in _STATE[mtype]:
            assert np.array_equal(np.asarray(st[key]), np.asarray(ref_st[key])), (case, block, key)


@pytest.mark.parametrize("case", list(_CASES))
def test_zero_rate_cv_is_bit_identical_to_unpatched(case):
    mtype, params = _CASES[case]
    x = _noise(2, n=2 * SR)
    patched, st = _render(mtype, params, {"in": x, "rate_cv": np.zeros(2 * SR, np.float32)},
                          512, n=2 * SR)
    bare, _ = _render(mtype, params, {"in": x}, 512, n=2 * SR)
    assert np.array_equal(patched, bare)
    assert st["rc_acc"] == 0


@pytest.mark.parametrize("mtype", ["flanger", "phaser"])
def test_non_finite_cv_sample_is_no_modulation(mtype):
    n = 4 * 512
    x = _noise(9, n=n)
    cv = np.zeros(n, dtype=np.float32)
    cv[100], cv[700], cv[1500] = np.nan, np.inf, -np.inf
    params = _CASES[mtype][1]
    got, _ = _render(mtype, params, {"in": x, "rate_cv": cv}, 512, n=n)
    clean, _ = _render(mtype, params, {"in": x, "rate_cv": np.zeros(n, np.float32)}, 512, n=n)
    assert np.all(np.isfinite(got))
    assert np.array_equal(got, clean)


@pytest.mark.parametrize("mtype", ["flanger", "phaser"])
def test_rate_moves_within_a_block(mtype):
    # One 4096-sample block, the CV stepping from 0 to +2 octaves at its
    # middle. Read per sample, the sweep speeds up AT the step: up to it
    # the render is the zero-CV render, bit for bit (sample n sees the
    # deviations BEFORE it). A block mean would have moved both halves.
    n = 4096
    x = _noise(5, n=n)
    cv = np.zeros(n, dtype=np.float32)
    cv[n // 2:] = 2.0
    params = dict(_CASES[mtype][1], rate=4.0)
    stepped, _ = _render(mtype, params, {"in": x, "rate_cv": cv}, n, n=n)
    flat, _ = _render(mtype, params, {"in": x, "rate_cv": np.zeros(n, np.float32)}, n, n=n)
    assert np.array_equal(stepped[:, :n // 2 + 1], flat[:, :n // 2 + 1])
    assert not np.array_equal(stepped[:, n // 2 + 64:], flat[:, n // 2 + 64:])


# 6 ms +/- 4 ms: the sweep never reaches the 2-sample floor or the 16 ms rail
_IDEAL = {"manual": 6.0, "depth": 1.0, "rate": 0.6, "spread": 0.5,
          "feedback": 0.0, "mix": 1.0, "cv_depth": 1.0}


def _ideal_flanger(x, cv, p=_IDEAL):
    """The standard flanger with no feedback and all wet, read through an
    IDEAL sweep: the instantaneous rate in float64 per sample, integrated by
    ONE exclusive cumsum over the whole run -- no blocks anywhere. With
    feedback 0 the line holds the input itself, so the whole read is a
    vectorised gather. Pure numpy, independent of the renderer."""
    n = x.shape[-1]
    r = np.clip(p["rate"] * 2.0 ** (p["cv_depth"] * cv.astype(np.float64)), 0.01, 20.0)
    phase = np.concatenate(([0.0], np.cumsum(r / SR)[:-1]))
    ph = (phase[None, :] + np.array([0.0, 0.5 * p["spread"]])[:, None]) % 1.0
    delay = p["manual"] * SR / 1000.0 + (4.0 * p["depth"] * SR / 1000.0) * np.sin(2 * np.pi * ph)
    back = np.ceil(delay)
    frac = back - delay
    i0 = np.arange(n)[None, :] - back.astype(np.int64)
    xx = x.astype(np.float64)

    def at(i):
        return np.where(i >= 0, xx[np.clip(i, 0, n - 1)], 0.0)

    return at(i0) * (1.0 - frac) + at(i0 + 1) * frac


@pytest.mark.parametrize("block", [64, 1000])
def test_flanger_tracks_the_ideal_integral(block):
    # The rate is continuous now, not a staircase: against the ideal the
    # render is within half a float32 ulp at any block size (measured
    # 5.95e-8 at 64 / 128 / 512 / 1000). The block mean was off by 6.1e-4
    # at 64, 2.5e-3 at 128, 4.2e-2 at 512 and 0.15 at 1000 over these two
    # seconds -- a 1000-sample staircase moves the comb audibly.
    n = 2 * SR
    x = _noise(3, n=n)
    cv = _sine(0.3, 1.0, n=n)
    got, _ = _render("flanger", _IDEAL, {"in": x, "rate_cv": cv}, block, n=n)
    err = float(np.max(np.abs(got.astype(np.float64) - _ideal_flanger(x, cv))))
    assert err < 2.5e-7, f"block {block}: max error {err:.3e}"


@pytest.mark.parametrize("fname", ["_render_chorus", "_render_flanger", "_render_phaser"])
def test_every_sweep_reads_rate_cv_through_the_integer_door(fname):
    """Count the doors in the source: all three modulation sweeps take
    rate_cv through the integer integrator, and none folds it into a
    block mean again (that was the flanger's and phaser's block-size
    dependence; a behavioural pin would only notice it at another block
    size)."""
    src = inspect.getsource(getattr(NumpyBackend, fname))
    assert "self._mod_rate_cv_phase(" in src, f"{fname}: rate_cv bypasses the integrator"
    assert "_finite_mean(rate_cv" not in src, f"{fname}: a rate_cv block mean is back"


@pytest.mark.parametrize("mtype", ["flanger", "phaser"])
def test_the_clock_lock_sets_rate_cv_aside(mtype):
    # A clock every quarter second locks the sweep at its second edge. The
    # CV is silent until 1 s -- so the lock engages on the same free phase
    # with or without it -- and swings two octaves after: the locked sweep
    # must not hear it, and the CV's running share is folded into the
    # anchor at every locked block (so pulling the cable would carry on
    # from the locked phase, not jump by the share).
    n = 2 * SR
    clock = np.zeros(n, dtype=np.float32)
    for s in range(0, n, SR // 4):
        clock[s:s + 200] = 1.0
    cv = np.zeros(n, dtype=np.float32)
    cv[SR:] = _sine(0.7, 2.0, n=n)[SR:]
    params = _CASES[mtype][1]
    x = _noise(4, n=n)
    with_cv, st = _render(mtype, params, {"in": x, "clock": clock, "rate_cv": cv}, 512, n=n)
    bare, _ = _render(mtype, params, {"in": x, "clock": clock}, 512, n=n)
    assert np.array_equal(with_cv, bare)
    assert st["rc_acc"] == 0 and st["ph_n"] == 0
