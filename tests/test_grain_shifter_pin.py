"""The lone WSOLA engine, pinned against the engine it was before the split.

``_GrainShifter`` has two users. The pitch shifter is a render, so nothing it
plays may move by a sample. The buffered sink's ring governor stretches its
push through the same engine, one per side, and on 2026-11-11 the grain step
was cut into ``_grid`` / ``_terms`` / ``_pick`` / ``_place`` (and the read-out
of ``process`` into ``_read``) so a pair of engines could share ONE splice
decision (see docs/channel-timing-plan.md, and ``_GrainShifterPair``).

That cut has to be invisible to a lone engine. ``_FrozenGrainShifter`` below
is the engine exactly as it stood before the split: ``_produce_one`` and
``process`` lifted from the source verbatim, written out whole, leaning on
none of the new helpers. Every test here runs the live engine and the frozen
one side by side and wants them equal sample for sample.

Two engines compared in one process, not a stored checksum, on purpose: the
search runs through BLAS, whose last bit can differ between machines, and a
checksum from one machine would fail on another for no fault of the code.

If the engine is ever changed ON PURPOSE (a new search, a different window),
this file is the thing to update: lift the new methods in, and say in the
worklog which renders moved.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import _GrainShifter

SR = 44100
SECONDS = 5.0
GRAIN = int(round(0.05 * SR))     # the pitch shifter's and the sink's 50 ms
HEAD = 16384


class _FrozenGrainShifter(_GrainShifter):
    """``_GrainShifter`` with its grain step and ``process`` as they stood
    before the 2026-11-11 split -- verbatim, do not tidy."""

    def _produce_one(self, r: float) -> bool:
        Lg, Hs, Lov, seek = self.Lg, self.Hs, self.Lov, self.seek
        c = int(round(self.a))
        if c + seek + Lg + 2 > self.iw:              # not enough input yet
            return False
        if c - seek - 1 < self.iw - self.Lin + 2:
            # The grid fell off the back of the ring (only possible
            # after an abnormal stall -- with the ideal-grid pointer
            # this is a safety net, not a steady state). Snap forward.
            self.a = float(self.iw - self.Lin + 3 + seek)
            c = int(round(self.a))
            if c + seek + Lg + 2 > self.iw:
                return False
        if not self.have_tgt:
            p = float(c)
        else:
            seg = self.ib[(c - seek + np.arange(2 * seek + Lov + 1)) % self.Lin]
            dot = np.correlate(seg[:-1], self.tgt, "valid")          # (2*seek+1,)
            cs = np.concatenate([[0.0], np.cumsum(seg * seg)])
            nrm = np.sqrt(np.maximum(cs[Lov:] - cs[:-Lov], 1e-12))[: 2 * seek + 1]
            tn = float(np.linalg.norm(self.tgt)) + 1e-9
            ncc = dot / (nrm * tn) - self.bias * np.abs(np.arange(-seek, seek + 1)) / seek
            k = int(np.argmax(ncc))
            if 0 < k < 2 * seek:
                # Parabolic sub-sample refinement of the NCC peak.
                y0, y1, y2 = float(ncc[k - 1]), float(ncc[k]), float(ncc[k + 1])
                den = y0 - 2.0 * y1 + y2
                dfr = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
                dfr = float(np.clip(dfr, -0.5, 0.5))
            else:
                dfr = 0.0
            p = c + (k - seek) + dfr
        # Fractional grain extraction (linear interp) at position p.
        i0 = int(np.floor(p))
        fr = p - i0
        seg2 = self.ib[(i0 + np.arange(Lg + 1)) % self.Lin]
        g = seg2[:-1] * (1.0 - fr) + seg2[1:] * fr
        ring = (self.onset + np.arange(Lg)) % self.Lstr
        self.ss[ring] += self.win * g
        self.sw[ring] += self.win
        # Target = this grain's continuation Hs later (fractional too).
        ts = self.ib[(i0 + Hs + np.arange(Lov + 1)) % self.Lin]
        self.tgt = ts[:-1] * (1.0 - fr) + ts[1:] * fr
        self.have_tgt = True
        self.a += Hs / r      # ideal grid: search excursions never accumulate
        self.onset += Hs
        self.final = self.onset
        return True

    def process(self, x: np.ndarray, r: float, x_dry=None) -> np.ndarray:
        """Push one input block (1D float64), return the shifted block.

        ``x_dry``, when given, is written to the parallel raw ring so
        ``dry_tap`` reads the true input even when ``x`` is a whitened
        residual (formant-preserve mode).
        """
        F = x.shape[0]
        if F == 0:
            return np.zeros(0, dtype=np.float64)
        slots = (self.iw + np.arange(F)) % self.Lin
        self.ib[slots] = x
        if x_dry is not None:
            if self.db is None:
                self.db = np.zeros(self.Lin)
            self.db[slots] = x_dry
        self.iw += F
        if not self.primed:
            while self.final < self.Lg:
                if not self._produce_one(r):
                    break
            if self.final >= self.Lg:
                self.primed = True
                self.rp = 0.0
                self.zeroed = 0
        out = np.zeros(F, dtype=np.float64)
        if self.primed:
            need = self.rp + r * F + 2.0
            guard = 0
            while self.final < need:
                if not self._produce_one(r):
                    break
                guard += 1
                if guard > 20000:
                    break
            pos = self.rp + np.arange(F) * r
            pos = np.minimum(pos, self.final - 1.0001)   # underrun guard
            i0 = np.floor(pos).astype(np.int64)
            fr = pos - i0
            w0 = self.sw[i0 % self.Lstr]
            w1 = self.sw[(i0 + 1) % self.Lstr]
            v0 = np.where(w0 > 1e-6, self.ss[i0 % self.Lstr] / np.where(w0 > 1e-6, w0, 1.0), 0.0)
            v1 = np.where(w1 > 1e-6, self.ss[(i0 + 1) % self.Lstr] / np.where(w1 > 1e-6, w1, 1.0), 0.0)
            out = v0 * (1.0 - fr) + v1 * fr
            self.rp = min(self.rp + r * F, float(self.final))
            tz = int(np.floor(self.rp)) - 1
            if tz > self.zeroed:
                sl = np.arange(self.zeroed, tz) % self.Lstr
                self.ss[sl] = 0.0
                self.sw[sl] = 0.0
                self.zeroed = tz
        return out


def _signals():
    n = int(SECONDS * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(20261111)
    clicks = np.zeros(n)
    clicks[SR // 5::SR // 5] = 0.5
    return {
        # No period at all: every re-splice is a free choice.
        "noise": 0.3 * rng.standard_normal(n),
        # Periodic, with transients: the search hops between whole periods.
        "tone": (0.3 * np.sin(2 * np.pi * 196.0 * t)
                 + 0.1 * np.sin(2 * np.pi * 1310.0 * t) + clicks),
    }


# Ratio as a function of the block index. ``wander`` is the shape the ring
# governor makes: a restless 2%, with one hard swing up and back.
_PATHS = {
    "unity": lambda i: 1.0,
    "up": lambda i: 1.25,
    "down": lambda i: 0.8,
    "wander": lambda i: 1.0 + 0.02 * np.sin(i * 0.37)
    + 0.15 * np.exp(-(((i - 200) / 15.0) ** 2)),
}

_SIGNALS = _signals()


@pytest.mark.parametrize("block", [64, 512, 1000])
@pytest.mark.parametrize("path", sorted(_PATHS))
@pytest.mark.parametrize("signal", sorted(_SIGNALS))
def test_lone_engine_matches_the_frozen_engine(signal, path, block):
    x = _SIGNALS[signal]
    ratio = _PATHS[path]
    live = _GrainShifter(GRAIN, 2, HEAD)
    frozen = _FrozenGrainShifter(GRAIN, 2, HEAD)
    for i in range(len(x) // block):
        blk = x[i * block:(i + 1) * block]
        got = live.process(blk, ratio(i))
        want = frozen.process(blk, ratio(i))
        assert np.array_equal(got, want), (
            f"{signal} / {path} / block {block}: first difference in block {i}"
        )
    # The carried state too, so a difference that has not reached the
    # output yet still fails here.
    assert live.a == frozen.a and live.rp == frozen.rp
    assert live.onset == frozen.onset and live.final == frozen.final
    assert np.array_equal(live.tgt, frozen.tgt)
    assert np.array_equal(live.ss, frozen.ss) and np.array_equal(live.sw, frozen.sw)


def test_the_run_is_long_enough_to_mean_something():
    # Exposure is part of the assertion. Every path lays well over a hundred
    # grains in five seconds, and every path but unity has to change what
    # comes out: the ratio really is steering the engine.
    x = _SIGNALS["noise"]
    outs = {}
    for name, ratio in _PATHS.items():
        eng = _FrozenGrainShifter(GRAIN, 2, HEAD)
        outs[name] = np.concatenate(
            [eng.process(x[i * 512:(i + 1) * 512], ratio(i))
             for i in range(len(x) // 512)]
        )
        assert eng.onset // eng.Hs > 120, name
    for name in ("up", "down", "wander"):
        assert not np.array_equal(outs[name], outs["unity"]), name


def test_dry_ring_and_latency_match_too():
    # The pitch shifter's formant mode feeds a second ring (``x_dry``) and
    # reads ``dry_tap`` / ``latency`` every block; both hang off the state
    # ``process`` carries.
    x = _SIGNALS["tone"]
    rng = np.random.default_rng(7)
    xd = 0.2 * rng.standard_normal(len(x))
    live = _GrainShifter(GRAIN, 2, HEAD)
    frozen = _FrozenGrainShifter(GRAIN, 2, HEAD)
    for i in range(len(x) // 512):
        s = slice(i * 512, (i + 1) * 512)
        r = _PATHS["wander"](i)
        assert np.array_equal(live.process(x[s], r, x_dry=xd[s]),
                              frozen.process(x[s], r, x_dry=xd[s]))
        assert live.latency(r) == frozen.latency(r)
        assert np.array_equal(live.dry_tap(512, live.latency(r)),
                              frozen.dry_tap(512, frozen.latency(r)))
