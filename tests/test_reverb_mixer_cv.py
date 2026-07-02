"""Tests for the last CV-coverage stragglers: reverb + mixer CV.

Reverb gains ``decay_cv`` + ``mix_cv`` — additive in level units,
scaled by one shared ``cv_depth`` (default 1.0), block-meaned and
clamped 0..1 exactly like the static params. ``size`` deliberately has
no CV (sweeping delay-line lengths clicks).

Mixer gains per-channel ``gain{i}_cv`` — VCA-style **per-sample
multiplicative** (channel i = ``in_i * gain_i * cv_i``), unpatched =
unity. Knobless by the house rule ("CV depth conventions"): the CV *is*
the amplitude, like ``vca.cv``.

Coverage:
  - Model: new ports + kinds on both modules; reverb cv_depth default;
    pre-CV patch dicts still load; type walls.
  - Reverb DSP: CV renders bit-identically to the equivalent static
    param (mix 0.3 + cv 0.7 == static mix 1.0; decay 0.2 + cv 0.5 ==
    static 0.7); cv_depth scales and 0 disables; mix_cv driving mix to
    0 gives the bit-exact dry passthrough; over-range CV clamps.
  - Mixer DSP: unpatched CVs leave the mix bit-identical to before; a
    constant CV scales its channel exactly; the CV is per-sample (a
    ramp CV shapes the block sample-by-sample, out == in*gain*cv*master
    to float32); one channel's CV leaves the others untouched.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100


def _reverb_render(x, blocks, cv_port=None, cv_value=None, **params):
    """Render x through a reverb; optional constant CV on one port."""
    p = Patch()
    osc = p.add_module("oscillator")
    rv = p.add_module("reverb")
    for k, v in params.items():
        rv.set_param(k, v)
    p.connect(osc.id, "out", rv.id, "in")
    lfo = None
    if cv_port is not None:
        lfo = p.add_module("lfo")
        p.connect(lfo.id, "cv", rv.id, cv_port)
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    ls, rs = [], []
    for i in range(blocks):
        bufs = {(osc.id, "out"): x[i * 512:(i + 1) * 512]}
        if lfo is not None:
            bufs[(lfo.id, "cv")] = np.full(512, cv_value, np.float32)
        out = b._render_reverb(rv, 512, bufs, p)
        ls.append(out["out_l"]); rs.append(out["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_reverb_new_ports_and_kinds(self):
        rv = Patch().add_module("reverb")
        assert [(p.name, p.signal_kind) for p in rv.input_ports] == [
            ("in", "audio"),
            ("decay_cv", "cv"),
            ("mix_cv", "cv"),
        ]

    def test_reverb_cv_depth_default(self):
        rv = Patch().add_module("reverb")
        assert rv.params["cv_depth"] == 1.0

    def test_mixer_new_ports_and_kinds(self):
        mx = Patch().add_module("mixer")
        cv_ports = [(p.name, p.signal_kind) for p in mx.input_ports
                    if p.name.endswith("_cv")]
        assert cv_ports == [(f"gain{i}_cv", "cv") for i in (1, 2, 3, 4)]
        # ...and no params were added: the CVs are knobless multipliers.
        assert set(mx.params) == {"gain1", "gain2", "gain3", "gain4", "master"}

    def test_pre_cv_reverb_patch_loads_with_default(self):
        d = Patch()
        d.add_module("reverb")
        raw = d.to_dict()
        for m in raw["modules"]:
            m["params"].pop("cv_depth", None)
        restored = Patch.from_dict(raw)
        rv = next(m for m in restored if m.TYPE == "reverb")
        assert rv.params["cv_depth"] == 1.0

    def test_audio_into_reverb_mix_cv_rejected(self):
        p = Patch()
        osc = p.add_module("oscillator")
        rv = p.add_module("reverb")
        with pytest.raises(ValueError):
            p.connect(osc.id, "out", rv.id, "mix_cv")

    def test_audio_into_mixer_gain_cv_rejected(self):
        p = Patch()
        osc = p.add_module("oscillator")
        mx = p.add_module("mixer")
        with pytest.raises(ValueError):
            p.connect(osc.id, "out", mx.id, "gain1_cv")

    def test_cv_into_mixer_gain_cv_accepted(self):
        p = Patch()
        lfo = p.add_module("lfo")
        mx = p.add_module("mixer")
        p.connect(lfo.id, "cv", mx.id, "gain2_cv")


# ----- Reverb DSP ------------------------------------------------------------


class TestReverbCV:
    def setup_method(self):
        rng = np.random.default_rng(3)
        self.x = (rng.standard_normal(8 * 512) * 0.3).astype(np.float32)

    def test_mix_cv_equivalent_to_static_mix(self):
        # mix 0.5 + cv 0.5 at depth 1 must render bit-identically to a
        # static mix of 1.0. (Dyadic values so the additive sum is
        # float-exact; 0.3 + 0.7 would differ in the last ulp.)
        via_cv = _reverb_render(self.x, 8, cv_port="mix_cv", cv_value=0.5,
                                mix=0.5)
        static = _reverb_render(self.x, 8, mix=1.0)
        assert np.array_equal(via_cv[0], static[0])
        assert np.array_equal(via_cv[1], static[1])

    def test_decay_cv_equivalent_to_static_decay(self):
        via_cv = _reverb_render(self.x, 8, cv_port="decay_cv", cv_value=0.5,
                                decay=0.25)
        static = _reverb_render(self.x, 8, decay=0.75)
        assert np.array_equal(via_cv[0], static[0])

    def test_cv_depth_scales(self):
        # depth 0.5 halves the CV: mix 0.25 + 0.5*0.5 = 0.5 (dyadic).
        via_cv = _reverb_render(self.x, 8, cv_port="mix_cv", cv_value=0.5,
                                mix=0.25, cv_depth=0.5)
        static = _reverb_render(self.x, 8, mix=0.5)
        assert np.array_equal(via_cv[0], static[0])

    def test_cv_depth_zero_disables(self):
        via_cv = _reverb_render(self.x, 8, cv_port="mix_cv", cv_value=1.0,
                                mix=0.3, cv_depth=0.0)
        static = _reverb_render(self.x, 8, mix=0.3)
        assert np.array_equal(via_cv[0], static[0])

    def test_mix_cv_to_zero_is_bit_exact_dry(self):
        # mix 0.25 + cv -0.25 -> effective 0.0 = the documented bit-exact
        # dry passthrough, now reachable by CV (full wet ducking).
        via_cv = _reverb_render(self.x, 8, cv_port="mix_cv", cv_value=-0.25,
                                mix=0.25)
        assert np.array_equal(via_cv[0], self.x)
        assert np.array_equal(via_cv[1], self.x)

    def test_over_range_cv_clamps(self):
        # mix 0.5 + cv 5.0 clamps to 1.0, same as static mix 1.0.
        via_cv = _reverb_render(self.x, 8, cv_port="mix_cv", cv_value=5.0,
                                mix=0.5)
        static = _reverb_render(self.x, 8, mix=1.0)
        assert np.array_equal(via_cv[0], static[0])

    def test_decay_cv_lengthens_the_tail(self):
        # Burst in, then silence: a decay_cv opening the tail leaves
        # far more energy late in the render than the short base decay.
        blocks = 30
        burst = np.zeros(blocks * 512, dtype=np.float32)
        burst[:512] = np.random.default_rng(4).standard_normal(512).astype(np.float32) * 0.5
        lo = _reverb_render(burst, blocks, cv_port="decay_cv", cv_value=0.0,
                            decay=0.1, mix=1.0)
        hi = _reverb_render(burst, blocks, cv_port="decay_cv", cv_value=0.85,
                            decay=0.1, mix=1.0)
        tail = slice(22 * 512, blocks * 512)
        assert np.sqrt(np.mean(hi[0][tail] ** 2)) > 4.0 * np.sqrt(np.mean(lo[0][tail] ** 2))


# ----- Mixer DSP -------------------------------------------------------------


class TestMixerCV:
    def _mixer(self, gains=None):
        p = Patch()
        oscs = [p.add_module("oscillator") for _ in range(2)]
        mx = p.add_module("mixer")
        if gains:
            for k, v in gains.items():
                mx.set_param(k, v)
        p.connect(oscs[0].id, "out", mx.id, "in1")
        p.connect(oscs[1].id, "out", mx.id, "in2")
        lfo = p.add_module("lfo")
        return p, oscs, mx, lfo

    def test_unpatched_cv_is_bit_identical_to_before(self):
        # No CV cables -> exactly the old sum (the retrofit is inert).
        p, oscs, mx, _ = self._mixer({"gain1": 0.8, "gain2": 0.5, "master": 0.7})
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        rng = np.random.default_rng(5)
        x1 = (rng.standard_normal(512) * 0.4).astype(np.float32)
        x2 = (rng.standard_normal(512) * 0.4).astype(np.float32)
        out = b._render_mixer(mx, 512, {(oscs[0].id, "out"): x1,
                                        (oscs[1].id, "out"): x2}, p)
        expected = ((x1 * 0.8).astype(np.float32)
                    + (x2 * 0.5).astype(np.float32)) * np.float32(0.7)
        assert np.array_equal(out, expected.astype(np.float32))

    def test_cv_is_per_sample(self):
        # A ramp CV shapes the channel sample-by-sample:
        # out == in1 * gain1 * ramp * master exactly (float32).
        p, oscs, mx, lfo = self._mixer({"gain1": 1.0, "master": 1.0})
        p.connect(lfo.id, "cv", mx.id, "gain1_cv")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        x1 = np.ones(512, dtype=np.float32) * 0.5
        ramp = np.linspace(0.0, 1.0, 512).astype(np.float32)
        out = b._render_mixer(mx, 512, {(oscs[0].id, "out"): x1,
                                        (lfo.id, "cv"): ramp}, p)
        expected = ((x1 * 1.0) * ramp).astype(np.float32) * np.float32(1.0)
        assert np.array_equal(out, expected.astype(np.float32))

    def test_cv_zero_silences_only_its_channel(self):
        p, oscs, mx, lfo = self._mixer({"gain1": 1.0, "gain2": 1.0, "master": 1.0})
        p.connect(lfo.id, "cv", mx.id, "gain1_cv")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        rng = np.random.default_rng(6)
        x1 = (rng.standard_normal(512) * 0.4).astype(np.float32)
        x2 = (rng.standard_normal(512) * 0.4).astype(np.float32)
        out = b._render_mixer(mx, 512, {(oscs[0].id, "out"): x1,
                                        (oscs[1].id, "out"): x2,
                                        (lfo.id, "cv"): np.zeros(512, np.float32)}, p)
        assert np.array_equal(out, x2)  # channel 1 gone, channel 2 intact

    def test_crossfade_sums_to_constant(self):
        # cv and (1-cv) on two channels of the same signal reconstruct
        # the original: per-sample multiplicative CV distributes over +.
        p, oscs, mx, lfo = self._mixer({"gain1": 1.0, "gain2": 1.0, "master": 1.0})
        lfo2 = p.add_module("lfo")
        p.connect(lfo.id, "cv", mx.id, "gain1_cv")
        p.connect(lfo2.id, "cv", mx.id, "gain2_cv")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        x = (np.random.default_rng(7).standard_normal(512) * 0.4).astype(np.float32)
        cv = np.linspace(0.0, 1.0, 512).astype(np.float32)
        out = b._render_mixer(mx, 512, {(oscs[0].id, "out"): x,
                                        (oscs[1].id, "out"): x,
                                        (lfo.id, "cv"): cv,
                                        (lfo2.id, "cv"): (1.0 - cv)}, p)
        assert np.allclose(out, x, atol=1e-6)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_lfo_crossfade_into_reverb_throw(self):
        # Two oscillators auto-crossfaded by an LFO pair into a reverb
        # whose mix breathes under a second LFO -> finite stereo audio.
        p = Patch()
        a = p.add_module("oscillator", params={"waveform": "saw", "freq": 110.0, "amp": 0.3})
        c = p.add_module("oscillator", params={"waveform": "square", "freq": 220.0, "amp": 0.3})
        mx = p.add_module("mixer")
        fade = p.add_module("lfo", params={"rate": 0.2})
        inv_scale = p.add_module("cv_scale", params={"scale": -1.0})
        inv_off = p.add_module("cv_offset", params={"offset": 1.0})
        rv = p.add_module("reverb", params={"mix": 0.2})
        breathe = p.add_module("lfo", params={"rate": 0.1})
        sl = p.add_module("left_speaker_output")
        sr_ = p.add_module("right_speaker_output")
        p.connect(a.id, "out", mx.id, "in1")
        p.connect(c.id, "out", mx.id, "in2")
        p.connect(fade.id, "cv", mx.id, "gain1_cv")
        p.connect(fade.id, "cv", inv_scale.id, "in")
        p.connect(inv_scale.id, "out", inv_off.id, "in")
        p.connect(inv_off.id, "out", mx.id, "gain2_cv")
        p.connect(mx.id, "out", rv.id, "in")
        p.connect(breathe.id, "cv", rv.id, "mix_cv")
        p.connect(rv.id, "out_l", sl.id, "in")
        p.connect(rv.id, "out_r", sr_.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        peak = 0.0
        for _ in range(40):
            blk = b.render_block(512)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert 0.0 < peak < 1.0
