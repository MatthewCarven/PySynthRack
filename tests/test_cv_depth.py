"""Tests for the cv_depth retrofit on Filter.cutoff_cv and LFO.rate_cv.

The house rule ("CV depth conventions" in docs/MODULES.md): every
modulatable ``*_cv`` input takes a ``cv_depth`` knob in the target's
natural unit per CV unit; frequency-domain depths are octaves/unit and
default to 1.0 = the classic 1 V/oct. Filter and LFO predate the rule
and had 1 V/oct baked in with no knob — this retrofit adds the knob
with a behaviour-preserving default.

Coverage:
  - Model: cv_depth in both modules' defaults; a patch dict saved
    BEFORE the retrofit (no cv_depth key) still loads and gets the
    default (old patches sound identical).
  - Filter DSP: depth 1.0 + CV c is bit-identical to a static filter at
    cutoff * 2**c (the pre-retrofit behaviour); depth 2.0 doubles the
    octave shift; depth 0 disables the CV (bit-identical to unpatched);
    the voice path applies depth per-voice.
  - LFO DSP: same ladder on the rate (depth 1 doubles frequency at
    cv=+1, depth 0 disables, voice path per-voice).
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100


def _filter_out(cutoff, x, cv=None, cv_depth=None, blocks=4):
    """Render x through a filter, optional constant cutoff_cv."""
    p = Patch()
    osc = p.add_module("oscillator")
    f = p.add_module("filter")
    f.set_param("cutoff", cutoff)
    if cv_depth is not None:
        f.set_param("cv_depth", cv_depth)
    p.connect(osc.id, "out", f.id, "in")
    lfo = None
    if cv is not None:
        lfo = p.add_module("lfo")
        p.connect(lfo.id, "cv", f.id, "cutoff_cv")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    out = []
    for i in range(blocks):
        bufs = {(osc.id, "out"): x[i * 512:(i + 1) * 512]}
        if lfo is not None:
            bufs[(lfo.id, "cv")] = np.full(512, cv, np.float32)
        out.append(b._render_filter(f, 512, bufs, p))
    return np.concatenate(out)


def _lfo_out(rate, cv=None, cv_depth=None, frames=SR):
    """Render an LFO, optional constant rate_cv."""
    p = Patch()
    lfo = p.add_module("lfo")
    lfo.set_param("rate", rate)
    lfo.set_param("bipolar", True)
    if cv_depth is not None:
        lfo.set_param("cv_depth", cv_depth)
    src = None
    if cv is not None:
        src = p.add_module("lfo", params={"rate": 1.0})
        p.connect(src.id, "cv", lfo.id, "rate_cv")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    out = []
    for _ in range(frames // 512):
        bufs = {}
        if src is not None:
            bufs = {(src.id, "cv"): np.full(512, cv, np.float32)}
        out.append(b._render_lfo(lfo, 512, bufs, p))
    return np.concatenate(out)


def _zero_crossings(y):
    return int(np.sum((y[:-1] < 0) & (y[1:] >= 0)))


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_filter_default_cv_depth(self):
        f = Patch().add_module("filter")
        assert f.params["cv_depth"] == 1.0

    def test_lfo_default_cv_depth(self):
        lfo = Patch().add_module("lfo")
        assert lfo.params["cv_depth"] == 1.0

    def test_pre_retrofit_filter_patch_loads_with_default(self):
        # A patch saved before the retrofit has no cv_depth key.
        d = Patch()
        f = d.add_module("filter")
        raw = d.to_dict()
        for m in raw["modules"]:
            m["params"].pop("cv_depth", None)
        restored = Patch.from_dict(raw)
        f2 = next(m for m in restored if m.TYPE == "filter")
        assert f2.params["cv_depth"] == 1.0

    def test_pre_retrofit_lfo_patch_loads_with_default(self):
        d = Patch()
        d.add_module("lfo")
        raw = d.to_dict()
        for m in raw["modules"]:
            m["params"].pop("cv_depth", None)
        restored = Patch.from_dict(raw)
        l2 = next(m for m in restored if m.TYPE == "lfo")
        assert l2.params["cv_depth"] == 1.0


# ----- Filter DSP ------------------------------------------------------------


class TestFilterCvDepth:
    def setup_method(self):
        rng = np.random.default_rng(42)
        self.x = (rng.standard_normal(2048) * 0.3).astype(np.float32)

    def test_depth_one_is_the_old_one_volt_per_octave(self):
        # depth 1.0 + cv +1 must equal a static filter at cutoff*2 —
        # exactly what the hard-coded 1 V/oct did before the knob.
        via_cv = _filter_out(500.0, self.x, cv=1.0, cv_depth=1.0)
        static = _filter_out(1000.0, self.x)
        assert np.array_equal(via_cv, static)

    def test_default_is_depth_one(self):
        explicit = _filter_out(500.0, self.x, cv=0.5, cv_depth=1.0)
        default = _filter_out(500.0, self.x, cv=0.5)
        assert np.array_equal(explicit, default)

    def test_depth_two_doubles_the_octave_shift(self):
        via_cv = _filter_out(500.0, self.x, cv=1.0, cv_depth=2.0)
        static = _filter_out(2000.0, self.x)  # 2 octaves up
        assert np.array_equal(via_cv, static)

    def test_depth_zero_disables_cv(self):
        with_cv = _filter_out(500.0, self.x, cv=1.0, cv_depth=0.0)
        without = _filter_out(500.0, self.x)
        assert np.array_equal(with_cv, without)

    def test_voice_path_applies_depth_per_voice(self):
        # (V, F) cutoff_cv: voice 0 at cv 0, voice 1 at cv +1, depth 2
        # -> voice 1 must match a static filter 2 octaves up.
        p = Patch()
        osc = p.add_module("oscillator")
        f = p.add_module("filter", params={"cutoff": 500.0, "cv_depth": 2.0})
        lfo = p.add_module("lfo")
        p.connect(osc.id, "out", f.id, "in")
        p.connect(lfo.id, "cv", f.id, "cutoff_cv")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        x2 = np.stack([self.x[:512], self.x[:512]])
        cv = np.stack([np.zeros(512, np.float32), np.ones(512, np.float32)])
        out = b._render_filter(f, 512, {(osc.id, "out"): x2, (lfo.id, "cv"): cv}, p)
        base = _filter_out(500.0, self.x[:512], blocks=1)
        up2 = _filter_out(2000.0, self.x[:512], blocks=1)
        assert np.allclose(out[0], base, atol=1e-6)
        assert np.allclose(out[1], up2, atol=1e-6)


# ----- LFO DSP ---------------------------------------------------------------


class TestLfoCvDepth:
    def test_depth_one_is_the_old_one_volt_per_octave(self):
        # cv +1 at depth 1 doubles the rate: 4 Hz -> 8 Hz over 1 s.
        y = _lfo_out(4.0, cv=1.0, cv_depth=1.0)
        assert _zero_crossings(y) in (7, 8, 9)

    def test_default_is_depth_one(self):
        explicit = _lfo_out(4.0, cv=1.0, cv_depth=1.0)
        default = _lfo_out(4.0, cv=1.0)
        assert np.array_equal(explicit, default)

    def test_depth_two_doubles_the_octave_shift(self):
        y = _lfo_out(4.0, cv=1.0, cv_depth=2.0)  # 2 octaves: 16 Hz
        assert _zero_crossings(y) in (15, 16, 17)

    def test_depth_zero_disables_cv(self):
        with_cv = _lfo_out(4.0, cv=1.0, cv_depth=0.0)
        without = _lfo_out(4.0)
        assert np.array_equal(with_cv, without)

    def test_voice_path_applies_depth_per_voice(self):
        # (V, F) rate_cv: voice 0 at cv 0 stays 4 Hz, voice 1 at cv +1
        # with depth 2 runs at 16 Hz.
        p = Patch()
        lfo = p.add_module("lfo", params={"rate": 4.0, "bipolar": True,
                                          "cv_depth": 2.0})
        src = p.add_module("lfo")
        p.connect(src.id, "cv", lfo.id, "rate_cv")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        out = []
        for _ in range(SR // 512):
            cv = np.stack([np.zeros(512, np.float32), np.ones(512, np.float32)])
            out.append(b._render_lfo(lfo, 512, {(src.id, "cv"): cv}, p))
        y = np.concatenate(out, axis=1)
        assert _zero_crossings(y[0]) in (3, 4, 5)
        assert _zero_crossings(y[1]) in (15, 16, 17)
