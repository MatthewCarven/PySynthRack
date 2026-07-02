"""Tests for MotionEQ's per-band Q CV — the bands learn to focus.

Completes the per-band CV set (freq 2026-07-02, gain 2026-07-02, now Q):
each band gains a ``band{i}_q_cv`` input, **multiplicative** like the
freq sweep — Q is a ratio-like quantity (0.1…20), so the natural unit
is a *doubling*: ``q_i * 2**(q_cv_depth * mean cv)``, block-meaned,
with one shared ``q_cv_depth`` (default 1.0). No extra clamp: the
result rides ``_peq_coeffs``'s existing (0.1, 20) Q clip, the same
rail the static param rides.

Coverage mirrors the gain-CV file: model (ports after the gain CVs,
default depth, kind wall, pre-CV dicts load), bit-identical static
equivalences at exact powers of two, depth scale/disable, both clip
rails, zero-mean alternating CV == static (block-mean proof), band
independence, an audible narrowing at the band's skirt, voice == mono.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100

_BANDS = {
    "band1_freq": 120.0, "band1_gain": 6.0, "band1_q": 1.5,
    "band2_freq": 500.0, "band2_gain": -4.0, "band2_q": 2.0,
    "band3_freq": 1800.0, "band3_gain": 8.0, "band3_q": 1.0,
    "band4_freq": 6000.0, "band4_gain": -3.0, "band4_q": 1.2,
}


def _render(x, cvs=None, blocks=4, N=512, **params):
    """Drive x through a motion_eq; ``cvs`` maps port->value (constant)
    or port->array (full per-sample CV block)."""
    p = Patch()
    osc = p.add_module("oscillator")
    m = p.add_module("motion_eq", params={**_BANDS, **params})
    p.connect(osc.id, "out", m.id, "in")
    consts = {}
    if cvs:
        for port, val in cvs.items():
            c = p.add_module("constant")
            p.connect(c.id, "out", m.id, port)
            consts[port] = (c.id, val)
    b = NumpyBackend(sample_rate=SR, block_size=N)
    b.compile(p)
    outs = []
    for i in range(blocks):
        seg = x[..., i * N:(i + 1) * N]
        bufs = {(osc.id, "out"): seg}
        for cid, val in consts.values():
            arr = (val if isinstance(val, np.ndarray)
                   else np.full(N, val, np.float32))
            bufs[(cid, "out")] = arr
        outs.append(b._render_motion_eq(m, N, bufs, p))
    return np.concatenate(outs, axis=-1)


def _noise(blocks=4, N=512, seed=13):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(blocks * N) * 0.3).astype(np.float32)


class TestQCVModel:
    def test_ports_and_default(self):
        p = Patch()
        m = p.add_module("motion_eq")
        names = [pt.name for pt in m.input_ports]
        assert names[-4:] == ["band1_q_cv", "band2_q_cv",
                              "band3_q_cv", "band4_q_cv"]
        assert all(pt.signal_kind == "cv"
                   for pt in m.input_ports if pt.name.endswith("_q_cv"))
        assert m.params["q_cv_depth"] == 1.0

    def test_audio_into_q_cv_rejected(self):
        p = Patch()
        osc = p.add_module("oscillator")
        m = p.add_module("motion_eq")
        with pytest.raises(ValueError):
            p.connect(osc.id, "out", m.id, "band4_q_cv")

    def test_cv_into_q_cv_accepted(self):
        p = Patch()
        lfo = p.add_module("lfo")
        m = p.add_module("motion_eq")
        p.connect(lfo.id, "cv", m.id, "band1_q_cv")

    def test_pre_q_cv_patch_dict_loads_with_default(self):
        p = Patch()
        m = p.add_module("motion_eq")
        d = p.to_dict()
        for mod in d["modules"]:
            mod["params"].pop("q_cv_depth", None)
        p2 = Patch.from_dict(d)
        m2 = p2.modules[m.id]
        assert m2.params["q_cv_depth"] == 1.0
        assert "band3_q_cv" in [pt.name for pt in m2.input_ports]


class TestQCVDSP:
    def setup_method(self):
        self.x = _noise()

    def test_q_cv_equivalent_to_static_q(self):
        # band3 q 1.0 * 2**(1.0 * 1.0) = 2.0 — an exact power of two,
        # so the render must be bit-identical to a static band3_q of 2.
        via_cv = _render(self.x, cvs={"band3_q_cv": 1.0}, band3_q=1.0)
        static = _render(self.x, band3_q=2.0)
        assert np.array_equal(via_cv, static)

    def test_negative_cv_widens(self):
        # q 2.0 * 2**(-1) = 1.0 (exact).
        via_cv = _render(self.x, cvs={"band3_q_cv": -1.0}, band3_q=2.0)
        static = _render(self.x, band3_q=1.0)
        assert np.array_equal(via_cv, static)

    def test_depth_scales(self):
        # depth 2, cv 1 -> 2**2 = 4x: q 1.0 -> 4.0 (exact).
        via_cv = _render(self.x, cvs={"band3_q_cv": 1.0},
                         band3_q=1.0, q_cv_depth=2.0)
        static = _render(self.x, band3_q=4.0)
        assert np.array_equal(via_cv, static)

    def test_depth_zero_disables(self):
        via_cv = _render(self.x, cvs={"band3_q_cv": 1.0}, q_cv_depth=0.0)
        static = _render(self.x)
        assert np.array_equal(via_cv, static)

    def test_high_rail_clips_at_20(self):
        # q 8 * 2**2 = 32 rides _peq_coeffs's clip to 20 == static 20
        # (which the same clip also touches -> bit-identical).
        via_cv = _render(self.x, cvs={"band3_q_cv": 2.0}, band3_q=8.0)
        static = _render(self.x, band3_q=20.0)
        assert np.array_equal(via_cv, static)

    def test_low_rail_clips_at_0_1(self):
        # q 0.5 * 2**(-4) = 0.03125 clips to 0.1 == static 0.05 (also
        # clipped to 0.1).
        via_cv = _render(self.x, cvs={"band3_q_cv": -4.0}, band3_q=0.5)
        static = _render(self.x, band3_q=0.05)
        assert np.array_equal(via_cv, static)

    def test_q_cv_is_block_meaned(self):
        # Alternating +1/−1 CV has an exactly zero mean -> 2**0 = 1 ->
        # bit-identical to no CV. Per-sample application would fail.
        alt = np.tile(np.array([1.0, -1.0], np.float32), 256)
        via_cv = _render(self.x, cvs={"band3_q_cv": alt})
        static = _render(self.x)
        assert np.array_equal(via_cv, static)

    def test_bands_are_independent(self):
        # CV on band2's Q == the static-band2 render with the other
        # three bands live — overrides don't cross-talk.
        via_cv = _render(self.x, cvs={"band2_q_cv": 1.0}, band2_q=2.0)
        static = _render(self.x, band2_q=4.0)
        assert np.array_equal(via_cv, static)

    def test_narrowing_is_audible_at_the_skirt(self):
        # A tone one-third octave off band3's centre sits on the bell's
        # skirt: at low Q it still gets most of the +12 dB boost, at
        # CV-narrowed high Q it gets far less.
        t = np.arange(4 * 512, dtype=np.float64) / SR
        tone = (0.2 * np.sin(2 * np.pi * 2268.0 * t)).astype(np.float32)
        rms = lambda a: float(np.sqrt(np.mean(a[512:].astype(np.float64) ** 2)))
        wide = _render(tone, band3_gain=12.0, band3_q=0.7)
        narrow = _render(tone, cvs={"band3_q_cv": 4.0},
                         band3_gain=12.0, band3_q=0.7)  # q -> 11.2
        assert rms(narrow) < 0.6 * rms(wide)

    def test_voice_path_single_row_matches_mono(self):
        mono = _render(self.x, cvs={"band3_q_cv": 1.0}, band3_q=1.0)
        voiced = _render(self.x[np.newaxis, :],
                         cvs={"band3_q_cv": 1.0}, band3_q=1.0)
        assert voiced.ndim == 2 and voiced.shape[0] == 1
        assert np.array_equal(voiced[0], mono)
