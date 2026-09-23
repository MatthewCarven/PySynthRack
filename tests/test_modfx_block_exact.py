"""Block-size exactness pins for ``delay``, ``flanger`` and ``phaser``.

Four seconds of noise through each module with its features ON, rendered
at blocks of 64, 128, 512 and 1000 (1000 does not divide the length, so
the last block is a short one -- which is itself a case: the flanger used
to size its ring from ``frames`` and re-initialised on a short block) --
and every render must be ``np.array_equal`` to the 512 one -- the float32
output AND the float64 DSP state left at the end (the delay line, the
damping memory, the allpass memories). Exposure is part of the assertion:
a pin that runs a few thousand samples passes a drift that four seconds
catches, and an ulp in a float64 state flips a float32 output sample only
rarely (against the old delay the short pin below differed in NO output
sample over four seconds, and in 106,817 samples of its line).

The mechanisms these pin (2026-09-24):

* **A ring index rounds at its own magnitude.** ``wp - delay`` hands back
  a fraction a float64 ulp apart depending on where the index wrapped,
  which depends on the block size. The delay core and both flanger taps
  now split the delay into whole samples (``ceil``) and a fraction
  (``back - delay``, exact by Sterbenz) and index the ring with integers.
* **The flanger's ring was ``max_ms + frames`` long,** so its delay clamp
  moved with the block size (at 64 it bit into the deepest standard
  sweeps -- ``manual`` 10 ms at ``depth`` 1 -- that 512 let through), and
  a block of a different length re-initialised the line.
* **The free-running LFO carried a float phase** (``ph += frames * inc``
  rounds once per block). Both sweeps now count samples since the last
  rate change (``_mod_free_phase``), re-anchoring only when the rate
  moves; a STEADY ``rate_cv`` is a steady float64 (``_finite_mean``), so
  it never re-anchors.
* **The delay's two paths damped differently.** The fast path runs the
  damping one-pole through ``lfilter`` (``g*d + (1-g)*lp``) and the
  per-sample path spelled it ``lp + g*(d - lp)``; which path a block
  takes depends on the block size (``delay >= frames``), so a delay
  between 64 and 1000 samples, or a ``time_cv`` sweeping across that
  range, switched arithmetic with the block size.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
N = 4 * SR                                   # four seconds
BLOCKS = (64, 128, 1000)                     # each against 512


def _noise(seed, amp=0.3, n=N):
    return (np.random.default_rng(seed).standard_normal(n) * amp).astype(np.float32)


def _sine(hz, amp, n=N):
    return (amp * np.sin(2 * np.pi * hz * np.arange(n) / SR)).astype(np.float32)


def _gate(spans, n=N):
    g = np.zeros(n, dtype=np.float32)
    for a, b in spans:
        g[a:b] = 1.0
    return g


def _render(mtype, params, jacks, block):
    """Drive ``_render_<mtype>`` block by block; a short last block too."""
    patch = Patch()
    m = patch.add_module(mtype, params=dict(params))
    srcs = {}
    for port in jacks:
        if port == "in":
            s, op = patch.add_module("oscillator"), "out"
        elif port == "freeze":
            s, op = patch.add_module("clock"), "out"
        else:
            s, op = patch.add_module("lfo"), "cv"
        patch.connect(s.id, op, m.id, port)
        srcs[port] = (s.id, op)
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    fn = getattr(b, "_render_" + mtype)
    outs, k = [], 0
    st = b._state.setdefault(m.id, {})
    while k < N:
        f = min(block, N - k)
        o = fn(m, f, {srcs[p]: v[..., k:k + f] for p, v in jacks.items()}, patch)
        if isinstance(o, dict):
            o = np.stack([o["out_l"], o["out_r"]])
        outs.append(o)
        k += f
    state = {key: np.array(st[key]) for key in _STATE[mtype]}
    return np.concatenate(outs, axis=-1), state


# The float64 DSP state each renderer carries between blocks. The rings are
# a fixed length now (the flanger's was ``+ frames``), so they line up
# sample for sample at any block size.
_STATE = {"delay": ("buf", "lp", "write_idx"),
          "flanger": ("buf", "write_idx"),
          "phaser": ("s", "yprev")}


def _assert_exact(mtype, params, jacks):
    ref, ref_st = _render(mtype, params, jacks, 512)
    assert np.all(np.isfinite(ref)) and float(np.abs(ref).max()) > 0.01
    for block in BLOCKS:
        got, got_st = _render(mtype, params, jacks, block)
        bad = int(np.count_nonzero(got != ref))
        assert bad == 0, (mtype, block, bad, float(np.abs(got - ref).max()))
        for key, v in ref_st.items():
            bad = int(np.count_nonzero(got_st[key] != v))
            assert bad == 0, (mtype, block, key, bad)


# ----- delay ------------------------------------------------------------------


def test_delay_short_modulated_frozen_poly_is_block_size_exact():
    # 5 ms +/- a 3 ms time_cv: the delay sweeps ~88..353 samples, so the
    # block takes the per-sample path at 512 / 1000 and flips between the
    # two paths at 64 / 128 -- the paths must compute the same bits. Three
    # voices, each its own cv, and a freeze gate that rises and falls
    # mid-block at every size.
    jacks = {
        "in": np.stack([_noise(10 + v) for v in range(3)]),
        "time_cv": np.stack([_sine(0.2 + 0.13 * v, 1.0) for v in range(3)]),
        "freeze": _gate([(50001, 90003), (120007, 150777)]),
    }
    _assert_exact("delay", {"time": 5.0, "feedback": 0.8, "tone": 0.7,
                            "cv_depth": 3.0, "mix": 0.6}, jacks)


def test_delay_long_modulated_frozen_is_block_size_exact():
    # 400 ms +/- 100 ms: the fast path at every size, where the read index
    # ``wp + n`` has not wrapped yet this block at one size and has at
    # another -- the ring-read rounding on its own. The line (2 s + 4
    # samples) wraps once in four seconds, at sample 96004; the freeze
    # comes AFTER it, because a held read is whole samples and exact
    # anyway -- holding across the wrap hid the old rounding entirely.
    # And the knob is NOT a round number: 400 ms is 17640 samples and a
    # float32 cv times 4410 fits float64's mantissa with room to spare, so
    # ``index - delay`` happened to be exact; 400.37 ms carries a full
    # float64 fraction, like any knob a hand has turned.
    jacks = {"in": _noise(1), "time_cv": _sine(0.11, 1.0),
             "freeze": _gate([(120001, 160000)])}
    _assert_exact("delay", {"time": 400.37, "feedback": 0.9,
                            "cv_depth": 100.0, "mix": 0.5}, jacks)


# ----- flanger ----------------------------------------------------------------


def test_flanger_deepest_standard_sweep_is_block_size_exact():
    # manual 10 ms + a full 4 ms sweep = 14 ms: past the old 12 ms +
    # ``frames`` ring at 64, so the old clamp bit only at small blocks.
    # A steady rate_cv, spread and negative feedback ride along.
    jacks = {"in": _noise(1), "rate_cv": np.full(N, 0.3, np.float32)}
    _assert_exact("flanger", {"manual": 10.0, "depth": 1.0, "rate": 1.3,
                              "spread": 0.3, "feedback": -0.6}, jacks)


def test_flanger_through_zero_spread_manual_cv_is_block_size_exact():
    jacks = {"in": _noise(2), "manual_cv": _sine(0.23, 0.8),
             "rate_cv": np.full(N, -0.45, np.float32)}
    _assert_exact("flanger", {"through_zero": True, "spread": 1.0,
                              "feedback": 0.7, "depth": 0.9, "manual": 4.0,
                              "rate": 2.0, "polarity": -1.0}, jacks)


# ----- phaser -----------------------------------------------------------------


def test_phaser_eight_stages_spread_feedback_manual_cv_is_block_size_exact():
    jacks = {"in": _noise(3), "manual_cv": _sine(0.3, 1.0),
             "rate_cv": np.full(N, 0.3, np.float32)}
    _assert_exact("phaser", {"stages": 8, "spread": 1.0, "feedback": 0.9,
                             "depth": 1.0, "rate": 0.7}, jacks)


def test_phaser_fast_four_stage_sweep_is_block_size_exact():
    _assert_exact("phaser", {"stages": 4, "rate": 5.0, "feedback": -0.7,
                             "spread": 0.2, "depth": 0.8}, {"in": _noise(4)})


# ----- the free-running anchor ----------------------------------------------


@pytest.mark.parametrize("mtype", ["flanger", "phaser"])
def test_a_rate_change_re_anchors_without_a_step(mtype):
    # The knob moves mid-run: the phase reached so far is frozen into the
    # anchor and the count restarts, so the sweep is continuous (no jump
    # in phase across the change) -- and the first block after the change
    # starts exactly where the old rate would have put it.
    patch = Patch()
    m = patch.add_module(mtype, params={"rate": 0.5})
    src = patch.add_module("oscillator")
    patch.connect(src.id, "out", m.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    x = _noise(5, n=512 * 20)
    fn = getattr(b, "_render_" + mtype)
    for k in range(20):
        if k == 10:
            m.set_param("rate", 3.0)
        fn(m, 512, {(src.id, "out"): x[k * 512:(k + 1) * 512]}, patch)
        st = b._state[m.id]
        if k == 9:
            want = (0.5 / SR) * (10 * 512) % 1.0
        if k == 10:
            assert st["phase"] == pytest.approx(want, abs=1e-12)
            assert st["ph_n"] == 512 and st["inc"] == 3.0 / SR
    assert st["ph_n"] == 10 * 512
