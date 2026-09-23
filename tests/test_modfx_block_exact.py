"""Block-size exactness pins for ``delay`` (the flanger and phaser join
in the next commit).

Four seconds of noise through the delay with its features ON, rendered at
blocks of 64, 128, 512 and 1000 (1000 does not divide the length, so the
last block is a short one) -- and every render must be ``np.array_equal``
to the 512 one -- the float32 output AND the float64 DSP state left at the
end (the delay line, the damping memory). Exposure is part of the
assertion: a pin that runs a few thousand samples passes a drift that four
seconds catches, and an ulp in a float64 state flips a float32 output
sample only rarely (against the old delay the short pin below differed in
NO output sample over four seconds, and in 106,817 samples of its line).

The mechanisms these pin (2026-09-24):

* **A ring index rounds at its own magnitude.** ``wp - delay`` hands back
  a fraction a float64 ulp apart depending on where the index wrapped,
  which depends on the block size. The delay core now splits the delay
  into whole samples (``ceil``) and a fraction (``back - delay``, exact by
  Sterbenz) and indexes the ring with integers.
* **The delay's two paths damped differently.** The fast path runs the
  damping one-pole through ``lfilter`` (``g*d + (1-g)*lp``) and the
  per-sample path spelled it ``lp + g*(d - lp)``; which path a block
  takes depends on the block size (``delay >= frames``), so a delay
  between 64 and 1000 samples, or a ``time_cv`` sweeping across that
  range, switched arithmetic with the block size.
"""
from __future__ import annotations

import numpy as np

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


# The float64 DSP state each renderer carries between blocks. The ring is a
# fixed length, so it lines up sample for sample at any block size.
_STATE = {"delay": ("buf", "lp", "write_idx")}


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
