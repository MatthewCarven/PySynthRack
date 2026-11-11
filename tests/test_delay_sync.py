"""The delay's clock sync + stereo echo love pass (spec: docs/MODULE_IDEAS.md,
"`delay`: clock sync + stereo echo").

Coverage (grows with the build, one commit per step):
  - Neutral: SHA pins captured from the pre-edit code (e61daa4) before any
    line of the delay changed -- the delay's own renderer driven block by
    block on synthesized rows, output AND the float64 line state it leaves,
    across the fast path, the old per-sample path (which the hops replace),
    a delay crossing the 4-sample hop floor, the freeze gate and tickbox,
    time_cv and three voices. Never guessed: each digest is what the old
    code computed.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
F = 512


# ----- harness ----------------------------------------------------------------


def _noise(seed, n, amp=0.3):
    return (np.random.default_rng(seed).standard_normal(n) * amp).astype(np.float32)


def _sine(hz, amp, n):
    return (amp * np.sin(2 * np.pi * hz * np.arange(n) / SR)).astype(np.float32)


def _gate(spans, n):
    g = np.zeros(n, dtype=np.float32)
    for a, b in spans:
        g[a:b] = 1.0
    return g


class Rig:
    """One delay, a source module per patched jack, driven block by block
    with raw rows (the ``test_modfx_block_exact`` idiom). ``stereo`` cables
    ``out_l`` / ``out_r`` (or just the named one) into a stereo sink."""

    def __init__(self, params, jacks, block=F, stereo=(), voices=None):
        self.patch = Patch()
        self.m = self.patch.add_module("delay", params=dict(params))
        self.srcs = {}
        for port in jacks:
            if port == "in":
                s, op = self.patch.add_module("oscillator"), "out"
            elif port in ("freeze", "clock"):
                s, op = self.patch.add_module("clock"), "out"
            else:
                s, op = self.patch.add_module("lfo"), "cv"
            self.patch.connect(s.id, op, self.m.id, port)
            self.srcs[port] = (s.id, op)
        self.sink = None
        if stereo:
            self.sink = self.patch.add_module("stereo_speaker_output")
            for port in stereo:
                self.patch.connect(self.m.id, port, self.sink.id,
                                   "in_l" if port == "out_l" else "in_r")
        self.jacks = jacks
        self.block = block
        self.b = NumpyBackend(sample_rate=SR, block_size=block)
        self.b.compile(self.patch)
        self.k = 0

    def run(self, n, script=None, ticks=None):
        """Render ``n`` samples. ``script`` = {sample: {param: value}},
        applied at the head of the block that sample falls in; ``ticks``
        = list of (k0, k1) SAMPLE ranges with the freeze tickbox on, set
        before each block the way the panel writes it. Returns ``out``
        (and ``out_l``, ``out_r`` when the renderer handed back a dict)."""
        outs = {}
        end = self.k + n
        while self.k < end:
            f = min(self.block, end - self.k)
            if script:
                for at, ch in script.items():
                    if self.k <= at < self.k + f:
                        self.m.params.update(ch)
            if ticks is not None:
                self.m.params["freeze"] = any(a <= self.k < b for a, b in ticks)
            bufs = {self.srcs[p]: v[..., self.k:self.k + f]
                    for p, v in self.jacks.items()}
            o = self.b._render_delay(self.m, f, bufs, self.patch)
            if not isinstance(o, dict):
                o = {"out": o}
            for key, v in o.items():
                outs.setdefault(key, []).append(np.asarray(v))
            self.k += f
        got = {key: np.concatenate(v, axis=-1) for key, v in outs.items()}
        if len(got) == 1:
            return got["out"]
        return got["out"], got["out_l"], got["out_r"]

    @property
    def st(self):
        return self.b._state[self.m.id]


def _digest(out, st):
    """sha256 over the float32 output bytes and the float64 line state."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(out, dtype=np.float32).tobytes())
    h.update(np.ascontiguousarray(st["buf"], dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(st["lp"], dtype=np.float64).tobytes())
    h.update(str(int(st["write_idx"])).encode())
    return h.hexdigest()


# ----- neutral: the pre-edit pins -------------------------------------------------

N_PIN = 3 * SR


def _pin_case(name, block=F):
    """The pinned scenarios. Every one uses only the jacks and params the
    pre-edit delay had, so the new code must reach the same bits through
    whichever path it now takes."""
    n = N_PIN
    if name == "long":
        # 400.37 ms +/- 100 ms: the fast path at every block size.
        params = {"time": 400.37, "feedback": 0.9, "cv_depth": 100.0, "mix": 0.5}
        jacks = {"in": _noise(1, n), "time_cv": _sine(0.11, 1.0, n),
                 "freeze": _gate([(90001, 110000)], n)}
        ticks = None
    elif name == "short_poly":
        # 5 ms +/- 3 ms, three voices: the old per-sample path at 512.
        params = {"time": 5.0, "feedback": 0.8, "tone": 0.7, "cv_depth": 3.0,
                  "mix": 0.6}
        jacks = {"in": np.stack([_noise(10 + v, n) for v in range(3)]),
                 "time_cv": np.stack([_sine(0.2 + 0.13 * v, 1.0, n)
                                      for v in range(3)]),
                 "freeze": _gate([(50001, 70003), (90007, 110777)], n)}
        ticks = None
    elif name == "floor":
        # 0.1 ms +/- 0.05 ms: 2.2 .. 6.6 samples, across the hop floor.
        params = {"time": 0.1, "feedback": 0.7, "tone": 0.9, "cv_depth": 0.05,
                  "mix": 0.5}
        jacks = {"in": _noise(3, n), "time_cv": _sine(1.7, 1.0, n)}
        ticks = None
    elif name == "mid_tick":
        # 200 samples, the freeze tickbox: per-sample at 512, fast at 64.
        # The tick lands on a block head shared by 64, 512 and 1000.
        params = {"time": 200 / SR * 1000.0, "feedback": 0.6, "tone": 0.5,
                  "mix": 0.8}
        jacks = {"in": _noise(4, n) * 0.5 + _sine(330.0, 0.1, n)}
        ticks = [(64000, 128000)]
    else:
        raise KeyError(name)
    rig = Rig(params, jacks, block=block)
    out = rig.run(n, ticks=ticks)
    return out, rig.st


# sha256 of ``_digest`` for each scenario, rendered by the pre-edit delay
# (e61daa4) at 512-sample blocks and checked identical at 64 and 1000.
_PIN_SHA = {
    "long": "5443480adcd70f90ebddd0b2d13986c6e919af54c05c31197d934d7f0a341d49",
    "short_poly": "335081adb6acc72969b00641b10b2bd35d9deb82aeafa3ec33c09ddcd2a030df",
    "floor": "b7af3635ae49af9e7e5f7e781b10d7849072542c46259611b7729737652308d9",
    "mid_tick": "ecd9bd0e6aad9e7856f9cabddd0c5f65f0172d61f3a5640017cf14c84c183fee",
}


@pytest.mark.parametrize("name", sorted(_PIN_SHA))
def test_the_pre_edit_renders_are_unchanged(name):
    out, st = _pin_case(name)
    assert np.all(np.isfinite(out)) and float(np.abs(out).max()) > 0.01
    assert _digest(out, st) == _PIN_SHA[name]


@pytest.mark.parametrize("name", sorted(_PIN_SHA))
@pytest.mark.parametrize("block", [64, 1000])
def test_the_pins_are_block_size_independent(name, block):
    out, st = _pin_case(name, block=block)
    assert _digest(out, st) == _PIN_SHA[name]


# ----- hops (the prep commit) ---------------------------------------------------


def _hop_case(name, n=SR):
    """Delays shorter than a block, which the per-sample loop ran before."""
    if name == "poly_cv_freeze":
        params = {"time": 5.0, "feedback": 0.8, "tone": 0.7, "cv_depth": 3.0,
                  "mix": 0.6}
        jacks = {"in": np.stack([_noise(20 + v, n) for v in range(3)]),
                 "time_cv": np.stack([_sine(0.7 + 0.3 * v, 1.0, n)
                                      for v in range(3)]),
                 "freeze": _gate([(9001, 20003), (30007, 33777)], n)}
    elif name == "twelve":
        params = {"time": 12 / SR * 1000.0, "feedback": 0.9, "tone": 0.3,
                  "mix": 0.7}
        jacks = {"in": _noise(21, n)}
    elif name == "four":
        # exactly the floor: hops of 4, whole-sample reads (frac 0)
        params = {"time": 4 / SR * 1000.0, "feedback": 0.95, "tone": 1.0,
                  "mix": 1.0}
        jacks = {"in": _noise(22, n)}
    else:
        raise KeyError(name)
    return params, jacks


@pytest.mark.parametrize("name", ["poly_cv_freeze", "twelve", "four"])
@pytest.mark.parametrize("block", [512, 1000])
def test_hops_equal_the_forced_per_sample_loop(name, block, monkeypatch):
    # Raise the floor past any delay and every block shorter than its
    # delay runs the per-sample loop again, as the delay did before the
    # hops. Same output, same line, same filter state.
    params, jacks = _hop_case(name)
    rig = Rig(params, jacks, block=block)
    hops = rig.run(SR)
    st_h = {k: np.array(rig.st[k]) for k in ("buf", "lp", "write_idx")}
    monkeypatch.setattr(NumpyBackend, "_DELAY_HOP_FLOOR", 10 ** 9)
    rig2 = Rig(params, jacks, block=block)
    loop = rig2.run(SR)
    assert float(np.abs(hops).max()) > 0.01
    assert np.array_equal(hops, loop)
    for k, v in st_h.items():
        assert np.array_equal(np.array(rig2.st[k]), v), k


def _out_and_line(params, jacks, n):
    rig = Rig(params, jacks)
    return rig.run(n), np.array(rig.st["buf"])


@pytest.mark.parametrize("name", ["poly_cv_freeze", "twelve", "four"])
def test_the_hop_comparison_can_fail(name, monkeypatch):
    # Self-test: a hop ONE sample longer than the shortest delay reads a
    # sample of the same hop before it is written. The forced loop must
    # then disagree, or the test above proves nothing. The line is part
    # of the exposure: "12 samples" is 11.999999999999998, so the early
    # read weighs 2e-15 -- it moves the float64 line and never reaches
    # the float32 output.
    params, jacks = _hop_case(name)
    n = SR // 4
    monkeypatch.setattr(NumpyBackend, "_DELAY_HOP_FLOOR", 10 ** 9)
    y_loop, line_loop = _out_and_line(params, jacks, n)
    monkeypatch.undo()
    y, line = _out_and_line(params, jacks, n)
    assert np.array_equal(y, y_loop) and np.array_equal(line, line_loop)
    real = NumpyBackend._delay_hop

    def one_too_long(self, low, frames):
        hop = real(self, low, frames)
        return hop + 1 if 0 < hop < frames else hop

    monkeypatch.setattr(NumpyBackend, "_delay_hop", one_too_long)
    y, line = _out_and_line(params, jacks, n)
    assert not (np.array_equal(y, y_loop) and np.array_equal(line, line_loop))
