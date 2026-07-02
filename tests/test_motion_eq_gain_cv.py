"""Tests for MotionEQ's per-band gain CV — the bands learn to breathe.

2026-07-02 extras on the animated EQ: each band gains a ``band{i}_gain_cv``
input, additive **in dB** (the tilt_eq convention) scaled by one shared
``gain_cv_depth`` (default 6.0 dB/unit), block-meaned like the freq sweep
and clamped to the knob range (±24 dB). Per-band sensitivity stays
reachable with a CVScale, exactly like the freq CVs.

Coverage:
  - Model: the four new ports exist with kind cv (after the freq CVs),
    ``gain_cv_depth`` defaults to 6.0, audio into a gain jack is
    rejected, pre-gain-CV patch dicts still load with the default.
  - DSP: CV c on band i at depth d renders bit-identically to a static
    gain of ``gain + d·c`` (dyadic values); depth 0 disables
    bit-identically; over-range CV clamps to ±24 exactly; an unpatched
    module stays bit-identical to a ParametricEQ of the same params;
    the CV is block-meaned (a zero-mean alternating CV == static); a
    gain push is audible on a tone at the band's centre; the voice
    path applies the same macro (single voice row == mono).
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


def _render(x, cvs=None, blocks=4, N=512, module_type="motion_eq", **params):
    """Drive x through a (motion|parametric)_eq; ``cvs`` maps port->value
    for constant CVs, or port->array for a full per-sample CV block."""
    p = Patch()
    osc = p.add_module("oscillator")
    m = p.add_module(module_type, params={**_BANDS, **params})
    p.connect(osc.id, "out", m.id, "in")
    consts = {}
    if cvs:
        for port, val in cvs.items():
            c = p.add_module("constant")
            p.connect(c.id, "out", m.id, port)
            consts[port] = (c.id, val)
    b = NumpyBackend(sample_rate=SR, block_size=N)
    b.compile(p)
    render = (b._render_motion_eq if module_type == "motion_eq"
              else b._render_parametric_eq)
    outs = []
    for i in range(blocks):
        seg = x[..., i * N:(i + 1) * N]
        bufs = {(osc.id, "out"): seg}
        for cid, val in consts.values():
            arr = (val if isinstance(val, np.ndarray)
                   else np.full(N, val, np.float32))
            bufs[(cid, "out")] = arr
        outs.append(render(m, N, bufs, p))
    return np.concatenate(outs, axis=-1)


def _noise(blocks=4, N=512, seed=11):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(blocks * N) * 0.3).astype(np.float32)


class TestGainCVModel:
    def test_ports_and_default(self):
        p = Patch()
        m = p.add_module("motion_eq")
        names = [pt.name for pt in m.input_ports]
        assert names == ["in",
                         "band1_freq_cv", "band2_freq_cv",
                         "band3_freq_cv", "band4_freq_cv",
                         "band1_gain_cv", "band2_gain_cv",
                         "band3_gain_cv", "band4_gain_cv"]
        assert all(pt.signal_kind == "cv"
                   for pt in m.input_ports if pt.name.endswith("_gain_cv"))
        assert m.params["gain_cv_depth"] == 6.0

    def test_audio_into_gain_cv_rejected(self):
        p = Patch()
        osc = p.add_module("oscillator")
        m = p.add_module("motion_eq")
        with pytest.raises(ValueError):
            p.connect(osc.id, "out", m.id, "band1_gain_cv")

    def test_cv_into_gain_cv_accepted(self):
        p = Patch()
        lfo = p.add_module("lfo")
        m = p.add_module("motion_eq")
        p.connect(lfo.id, "cv", m.id, "band3_gain_cv")

    def test_pre_gain_cv_patch_dict_loads_with_default(self):
        # A patch saved before gain_cv existed has no gain_cv_depth key:
        # it must load with the 6.0 default and the new ports present.
        p = Patch()
        m = p.add_module("motion_eq")
        d = p.to_dict()
        for mod in d["modules"]:
            mod["params"].pop("gain_cv_depth", None)
        p2 = Patch.from_dict(d)
        m2 = p2.modules[m.id]
        assert m2.params["gain_cv_depth"] == 6.0
        assert "band2_gain_cv" in [pt.name for pt in m2.input_ports]


class TestGainCVDSP:
    def setup_method(self):
        self.x = _noise()

    def test_gain_cv_equivalent_to_static_gain(self):
        # band3 gain 4.0 + depth 6.0 * cv 0.5 = 7.0 dB — all dyadic, so
        # the sum is float-exact and the render must be bit-identical
        # to a static band3_gain of 7.0.
        via_cv = _render(self.x, cvs={"band3_gain_cv": 0.5},
                         band3_gain=4.0, gain_cv_depth=6.0)
        static = _render(self.x, band3_gain=7.0)
        assert np.array_equal(via_cv, static)

    def test_negative_cv_cuts(self):
        # 8.0 + 6.0 * (-0.5) = 5.0 dB.
        via_cv = _render(self.x, cvs={"band3_gain_cv": -0.5},
                         band3_gain=8.0)
        static = _render(self.x, band3_gain=5.0)
        assert np.array_equal(via_cv, static)

    def test_depth_scales(self):
        # depth 12: 8.0 + 12.0 * 0.5 = 14.0 dB.
        via_cv = _render(self.x, cvs={"band3_gain_cv": 0.5},
                         band3_gain=8.0, gain_cv_depth=12.0)
        static = _render(self.x, band3_gain=14.0)
        assert np.array_equal(via_cv, static)

    def test_depth_zero_disables(self):
        via_cv = _render(self.x, cvs={"band3_gain_cv": 1.0},
                         gain_cv_depth=0.0)
        static = _render(self.x)
        assert np.array_equal(via_cv, static)

    def test_over_range_cv_clamps_to_24(self):
        # 20.0 + 6.0 * 2.0 = 32 -> clamped to +24 = static 24.
        via_cv = _render(self.x, cvs={"band3_gain_cv": 2.0},
                         band3_gain=20.0)
        static = _render(self.x, band3_gain=24.0)
        assert np.array_equal(via_cv, static)

    def test_under_range_cv_clamps_to_minus_24(self):
        via_cv = _render(self.x, cvs={"band3_gain_cv": -4.0},
                         band3_gain=-8.0)
        static = _render(self.x, band3_gain=-24.0)
        assert np.array_equal(via_cv, static)

    def test_unpatched_bit_identical_to_parametric_eq(self):
        # Nothing patched: MotionEQ (gain CVs and all) must still match
        # a ParametricEQ of the same band params exactly.
        meq = _render(self.x)
        peq = _render(self.x, module_type="parametric_eq")
        assert np.array_equal(meq, peq)

    def test_gain_cv_is_block_meaned(self):
        # An alternating +1/−1 CV sums to an exactly zero mean, so the
        # render must be bit-identical to no CV at all. A per-sample
        # application would tremolo the band and fail this.
        alt = np.tile(np.array([1.0, -1.0], np.float32), 256)
        via_cv = _render(self.x, cvs={"band3_gain_cv": alt})
        static = _render(self.x)
        assert np.array_equal(via_cv, static)

    def test_bands_are_independent(self):
        # CV on band2 must equal the static-band2 render even with the
        # other three bands active — no crosstalk between overrides.
        via_cv = _render(self.x, cvs={"band2_gain_cv": 0.5},
                         band2_gain=-4.0)  # -4 + 3 = -1 dB
        static = _render(self.x, band2_gain=-1.0)
        assert np.array_equal(via_cv, static)

    def test_push_is_audible_at_band_centre(self):
        # A sine at band3's centre gets ~12 dB louder when the CV pushes
        # the band from 0 dB to +12 dB.
        t = np.arange(4 * 512, dtype=np.float64) / SR
        tone = (0.2 * np.sin(2 * np.pi * 1800.0 * t)).astype(np.float32)
        flat = _render(tone, band3_gain=0.0)
        pushed = _render(tone, cvs={"band3_gain_cv": 2.0},
                         band3_gain=0.0)  # +12 dB
        rms = lambda a: float(np.sqrt(np.mean(a[512:].astype(np.float64) ** 2)))
        assert rms(pushed) > 3.0 * rms(flat)

    def test_voice_path_single_row_matches_mono(self):
        # A (1, F) voice input through the CV-pushed cascade must be
        # bit-identical to the same (F,) mono render.
        mono = _render(self.x, cvs={"band3_gain_cv": 0.5}, band3_gain=4.0)
        voiced = _render(self.x[np.newaxis, :],
                         cvs={"band3_gain_cv": 0.5}, band3_gain=4.0)
        assert voiced.ndim == 2 and voiced.shape[0] == 1
        assert np.array_equal(voiced[0], mono)
