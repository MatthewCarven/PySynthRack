"""A block size that CHANGES mid-stream: the chorus and the organ scanner.

Both renderers are block-size exact in steady state (pinned at 64 / 128 /
512 / 1000 in their own test files). These pins are the other half: a
render whose block size changes partway through -- the toolbar's buffer
slider, an offline render's short last block, a zero-length block --
must be bit-identical to one that never changed. Before 2026-10-12 it
was not:

* the **chorus**'s ring was ``max_ms + frames`` long and any block of a
  new length CLEARED it -- the line, the LFO's anchor and count, the
  ``rate_cv`` accumulator -- so a switch at 1 s was a 12..24 ms hole in
  the wet and a sweep restarted from phase 0: every later sample
  differed, by up to 0.85 (measured; a zero-length block did the same);
* the **organ scanner**'s ring was sized for 4096-sample blocks and a
  block longer than any before replaced it with a fresh zeroed ring
  RESTING AT DRY, so the wet faded back in over 40 ms from a hole (the
  three voices below: 5757 samples off, by up to 0.50).

Both rings are now indexed by an absolute sample clock and only ever
grow, keeping their history (``_ring_regrow``, the rotary's
``_rotary_grow`` generalised) -- so the renders below are exact.
Exposure is part of the assertion: four seconds, switches well inside.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
N = 4 * SR


def _plan(steps, n):
    """Block lengths for ``steps`` = ((until_sample, block), ...): blocks of
    ``block`` until ``until_sample``; the last step runs to ``n``. A block
    of 0 inserts one zero-length call at that point."""
    out, s = [], 0
    for until, blk in steps:
        if blk == 0:
            out.append(0)
            continue
        until = min(until, n)
        while s < until:
            f = min(blk, until - s)
            out.append(f)
            s += f
    assert s == n
    return out


# ----- chorus ------------------------------------------------------------------

_CH = {"rate": 0.5, "depth": 0.6, "voices": 4, "mix": 0.5, "cv_depth": 1.0}


def _chorus(sizes, rate_cv=True):
    x = (np.random.default_rng(7).standard_normal(N) * 0.3).astype(np.float32)
    cv = np.sin(2 * np.pi * 0.3 * np.arange(N) / SR).astype(np.float32)
    patch = Patch()
    osc = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    ch = patch.add_module("chorus", params=dict(_CH))
    patch.connect(osc.id, "out", ch.id, "in")
    if rate_cv:
        patch.connect(lfo.id, "cv", ch.id, "rate_cv")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    outs, k = [], 0
    for f in sizes:
        bufs = {(osc.id, "out"): x[k:k + f], (lfo.id, "cv"): cv[k:k + f]}
        o = b._render_chorus(ch, f, bufs, patch)
        outs.append(np.stack([o["out_l"], o["out_r"]]))
        k += f
    return np.concatenate(outs, axis=1), b._state[ch.id]


_SWITCHES = {
    "512-64": ((SR, 512), (N, 64)),
    "512-8192": ((SR, 512), (N, 8192)),          # past the ring's 4096 headroom
    "64-1000-4500": ((SR, 64), (2 * SR, 1000), (N, 4500)),
    "512-16384-64": ((SR, 512), (2 * SR, 16384), (N, 64)),
    "zero-length": ((SR, 512), (SR, 0), (N, 512)),
}


@pytest.mark.parametrize("rate_cv", [False, True], ids=["knob", "rate_cv"])
@pytest.mark.parametrize("steps", list(_SWITCHES.values()), ids=list(_SWITCHES))
def test_chorus_block_size_change_is_bit_exact(steps, rate_cv):
    ref, _ = _chorus(_plan(((N, 512),), N), rate_cv)
    got, st = _chorus(_plan(steps, N), rate_cv)
    assert np.abs(ref).max() > 0.1
    bad = int(np.count_nonzero(got != ref))
    assert bad == 0, (steps, bad, float(np.abs(got - ref).max()))
    assert st["n"] == N
    assert st["buf"].shape[0] >= st["hist"] + max(f for f in _plan(steps, N))


def test_chorus_ring_only_grows_and_starts_with_headroom():
    _, st = _chorus(_plan(((N, 64),), N), rate_cv=False)
    L0 = st["buf"].shape[0]
    assert L0 == st["hist"] + NumpyBackend._CHORUS_MIN_BLOCK  # a short block never resizes
    _, st = _chorus(_plan(((SR, 64), (N, 9000)), N), rate_cv=False)
    assert st["buf"].shape[0] == st["hist"] + 9000


# ----- organ scanner -----------------------------------------------------------


def _organ(sizes, sr=48000):
    F = 4 * sr
    gate = np.zeros((3, F), dtype=np.float32)
    gate[0, 100:F // 3] = 1.0
    gate[0, F // 3 + 777:] = 1.0
    gate[1, 20011:] = 1.0
    gate[2, 5003:F // 2 + 31] = 1.0
    pitch = np.empty((3, F), dtype=np.float32)
    pitch[0] = 0.25 + 7.0 / 12.0
    pitch[1] = 4.0 / 12.0
    pitch[2] = -5.0 / 12.0
    patch = Patch()
    org = patch.add_module("organ", params={"click": 0.5, "perc": "2nd",
                                            "perc_level": 0.8, "vibrato": "c3"})
    kb = patch.add_module("cv_keyboard")
    patch.connect(kb.id, "pitch_cv", org.id, "pitch_cv")
    patch.connect(kb.id, "gate", org.id, "gate")
    b = NumpyBackend(sample_rate=sr, block_size=512)
    b.compile(patch)
    outs, s = [], 0
    for f in sizes:
        bufs = {(kb.id, "pitch_cv"): pitch[:, s:s + f], (kb.id, "gate"): gate[:, s:s + f]}
        outs.append(b._render_organ(org, f, bufs, patch))
        s += f
    return np.concatenate(outs, axis=1), b._state[org.id]


_ORGAN_SWITCHES = {
    "512-8192": ((48000, 512), (4 * 48000, 8192)),
    "512-16384-64": ((48000, 512), (2 * 48000, 16384), (4 * 48000, 64)),
    "64-4096-5000": ((48000, 64), (2 * 48000, 4096), (4 * 48000, 5000)),
}


@pytest.mark.parametrize("steps", list(_ORGAN_SWITCHES.values()),
                         ids=list(_ORGAN_SWITCHES))
def test_organ_scanner_survives_a_block_longer_than_any_before(steps):
    F = 4 * 48000
    ref, _ = _organ(_plan(((F, 512),), F))
    got, st = _organ(_plan(steps, F))
    assert np.abs(ref).max() > 0.1
    bad = int(np.count_nonzero(got != ref))
    assert bad == 0, (steps, bad, float(np.abs(got - ref).max()))
    # the ring grew to hold the biggest block (where one outgrew it)
    assert st["scan_buf"].shape[1] > max(_plan(steps, F))
