"""Tests for StereoSpeakerOutput (the pan/width stereo sink).

Coverage:
  - Model: registration/defaults, ports & signal kinds (in_l/in_r
    audio + pan_cv cv, no outputs), JSON round-trip, unknown param
    rejected, type walls.
  - Mono mode (in_r uncabled): constant-power pan — L == R at centre
    (each -3 dB), hard left/right kill the far channel, and L² + R²
    equals the source power at EVERY pan position; width is a no-op.
  - Stereo mode: the defaults pass a pair to the bus bit-exactly;
    balance leaves the near side at unity and fades the far side with
    the cosine taper; width 0 collapses to mono, width 2 doubles the
    side signal; width 1 is skipped (bit-exact).
  - pan_cv: a constant CV equals the equivalent static pan; cv_depth 0
    disables; the result is clamped at the rails; a (V, F) pan_cv is
    averaged across voices.
  - width_cv: shares ``cv_depth`` with pan_cv (Reverb convention); a
    constant CV equals the equivalent static width; clamped 0..2;
    silent-jack default stays bit-exact; a swept width really moves
    the side level within one render; mono mode ignores it.
  - Bus behaviour: voice-aware audio inputs sum their voice axis; two
    sinks add into the same bus; gain scales both channels; the master
    ±1 clip still applies; an uncabled sink contributes silence.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.output import StereoSpeakerOutput

SR, F = 44100, 512


def _render(patch, blocks=4):
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    return np.concatenate([b.render_block(F) for _ in range(blocks)]), b


def _mono_rig(**params):
    patch = Patch()
    src = patch.add_module("oscillator", params={"amp": 0.5})
    sp = patch.add_module("stereo_speaker_output", params=params)
    patch.connect(src.id, "out", sp.id, "in_l")
    return patch, src, sp


def _stereo_rig(**params):
    patch = Patch()
    l = patch.add_module("oscillator", params={"amp": 0.4})
    # deterministic R source (noise would differ between renders)
    r = patch.add_module(
        "oscillator", params={"amp": 0.3, "waveform": "square", "freq": 330.0}
    )
    sp = patch.add_module("stereo_speaker_output", params=params)
    patch.connect(l.id, "out", sp.id, "in_l")
    patch.connect(r.id, "out", sp.id, "in_r")
    return patch, l, r, sp


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        sp = patch.add_module("stereo_speaker_output")
        assert isinstance(sp, StereoSpeakerOutput)
        assert sp.params == {
            "gain": 1.0, "pan": 0.0, "width": 1.0, "cv_depth": 1.0,
        }

    def test_ports_and_signal_kinds(self):
        sp = Patch().add_module("stereo_speaker_output")
        assert [(p.name, p.signal_kind) for p in sp.input_ports] == [
            ("in_l", "audio"), ("in_r", "audio"),
            ("pan_cv", "cv"), ("width_cv", "cv")
        ]
        assert sp.output_ports == []

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "stereo_speaker_output", params={"pan": -0.5, "width": 1.8}
        )
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == "stereo_speaker_output")
        assert mod.params["pan"] == -0.5
        assert mod.params["width"] == 1.8

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("stereo_speaker_output", params={"balance": 0.0})

    def test_type_walls(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        osc = patch.add_module("oscillator")
        sp = patch.add_module("stereo_speaker_output")
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", sp.id, "in_l")     # cv -> audio
        with pytest.raises(Exception):
            patch.connect(osc.id, "out", sp.id, "pan_cv")  # audio -> cv


# ----- Mono mode (constant-power pan) -----------------------------------------


class TestMonoPan:
    def test_centre_is_equal_and_minus_3db(self):
        patch, src, sp = _mono_rig(pan=0.0)
        out, _ = _render(patch)
        L, R = out[:, 0], out[:, 1]
        assert np.array_equal(L, R)
        # -3 dB: peak = 0.5 * cos(pi/4)
        assert np.max(np.abs(L)) == pytest.approx(0.5 * np.cos(np.pi / 4), abs=1e-4)

    def test_hard_left_and_right(self):
        patch, src, sp = _mono_rig(pan=-1.0)
        out, _ = _render(patch)
        assert np.max(np.abs(out[:, 1])) < 1e-7   # right silent
        assert np.max(np.abs(out[:, 0])) == pytest.approx(0.5, abs=1e-4)
        patch, src, sp = _mono_rig(pan=1.0)
        out, _ = _render(patch)
        assert np.max(np.abs(out[:, 0])) < 1e-7   # left silent

    def test_constant_power_everywhere(self):
        for pan in (-0.75, -0.3, 0.2, 0.6, 0.95):
            patch, src, sp = _mono_rig(pan=pan)
            out, _ = _render(patch, blocks=2)
            L, R = out[:, 0].astype(np.float64), out[:, 1].astype(np.float64)
            power = L * L + R * R
            # reconstruct the mono source's square from the pair
            patch2, src2, sp2 = _mono_rig(pan=-1.0)  # all left = raw source
            out2, _ = _render(patch2, blocks=2)
            raw_sq = out2[:, 0].astype(np.float64) ** 2
            assert np.allclose(power, raw_sq, atol=1e-8)

    def test_width_is_noop_for_mono(self):
        patch, src, sp = _mono_rig(pan=0.3, width=2.0)
        out_wide, _ = _render(patch)
        patch2, src2, sp2 = _mono_rig(pan=0.3, width=0.0)
        out_none, _ = _render(patch2)
        assert np.array_equal(out_wide, out_none)


# ----- Stereo mode (balance + width) ------------------------------------------


class TestStereoPair:
    def test_defaults_are_bit_exact(self):
        patch, l, r, sp = _stereo_rig()
        out, b = _render(patch, blocks=1)
        # re-render the sources alone for reference
        p2 = Patch()
        l2 = p2.add_module("oscillator", params={"amp": 0.4})
        b2 = NumpyBackend(sample_rate=SR, block_size=F)
        b2.compile(p2)
        ref = b2._render_oscillator(l2, F).astype(np.float32)
        assert np.array_equal(out[:, 0], ref)

    def test_balance_keeps_near_side_at_unity(self):
        patch, l, r, sp = _stereo_rig(pan=0.5)
        out, _ = _render(patch, blocks=1)
        patch2, l2, r2, sp2 = _stereo_rig(pan=0.0)
        ref, _ = _render(patch2, blocks=1)
        # right (near side) untouched, left scaled by cos(pi/4)
        assert np.array_equal(out[:, 1], ref[:, 1])
        assert np.allclose(out[:, 0], ref[:, 0] * np.cos(0.5 * np.pi / 2), atol=1e-7)

    def test_width_zero_collapses_to_mono(self):
        patch, l, r, sp = _stereo_rig(width=0.0)
        out, _ = _render(patch)
        assert np.allclose(out[:, 0], out[:, 1], atol=1e-7)

    def test_width_two_doubles_the_side(self):
        patch, l, r, sp = _stereo_rig(width=2.0)
        wide, _ = _render(patch, blocks=1)
        patch2, l2, r2, sp2 = _stereo_rig(width=1.0)
        ref, _ = _render(patch2, blocks=1)
        side_ref = ref[:, 0].astype(np.float64) - ref[:, 1].astype(np.float64)
        side_wide = wide[:, 0].astype(np.float64) - wide[:, 1].astype(np.float64)
        assert np.allclose(side_wide, 2.0 * side_ref, atol=1e-6)
        # and the mid is preserved
        assert np.allclose(
            wide[:, 0] + wide[:, 1], ref[:, 0] + ref[:, 1], atol=1e-6
        )

    def test_only_in_r_cabled_is_stereo_with_silent_left(self):
        patch = Patch()
        r = patch.add_module("noise", params={"amp": 0.3})
        sp = patch.add_module("stereo_speaker_output")
        patch.connect(r.id, "out", sp.id, "in_r")
        out, _ = _render(patch)
        assert np.max(np.abs(out[:, 1])) > 0.05
        assert np.max(np.abs(out[:, 0])) < 1e-7


# ----- pan_cv ------------------------------------------------------------------


class TestPanCV:
    def _cv_rig(self, value, **params):
        patch = Patch()
        src = patch.add_module("oscillator", params={"amp": 0.5})
        const = patch.add_module("constant", params={"value": value})
        sp = patch.add_module("stereo_speaker_output", params=params)
        patch.connect(src.id, "out", sp.id, "in_l")
        patch.connect(const.id, "out", sp.id, "pan_cv")
        return patch

    def test_constant_cv_equals_static_pan(self):
        out_cv, _ = _render(self._cv_rig(0.6, pan=0.0, cv_depth=1.0))
        patch, src, sp = _mono_rig(pan=0.6)
        out_static, _ = _render(patch)
        assert np.allclose(out_cv, out_static, atol=1e-7)

    def test_cv_depth_zero_disables(self):
        out_cv, _ = _render(self._cv_rig(1.0, pan=0.2, cv_depth=0.0))
        patch, src, sp = _mono_rig(pan=0.2)
        out_static, _ = _render(patch)
        assert np.array_equal(out_cv, out_static)

    def test_pan_clamped_at_rails(self):
        out, _ = _render(self._cv_rig(5.0, pan=0.5, cv_depth=1.0))
        assert np.max(np.abs(out[:, 0])) < 1e-7  # pinned hard right
        assert np.max(np.abs(out[:, 1])) == pytest.approx(0.5, abs=1e-4)

    def test_voice_pan_cv_is_averaged(self):
        # Drive the drain directly with a crafted (V, F) pan_cv.
        patch = Patch()
        src = patch.add_module("oscillator", params={"amp": 0.5})
        lfo = patch.add_module("lfo")
        sp = patch.add_module("stereo_speaker_output")
        patch.connect(src.id, "out", sp.id, "in_l")
        patch.connect(lfo.id, "cv", sp.id, "pan_cv")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        mono = np.full(F, 0.5, dtype=np.float32)
        cv2d = np.stack([np.full(F, 1.0), np.full(F, -1.0)]).astype(np.float32)
        out = np.zeros((F, 2), dtype=np.float32)
        b._drain_stereo_speaker(
            sp, F,
            {(src.id, "out"): mono, (lfo.id, "cv"): cv2d},
            patch, out,
        )
        # mean CV = 0 -> centre: both channels equal
        assert np.allclose(out[:, 0], out[:, 1], atol=1e-7)


# ----- Bus behaviour -----------------------------------------------------------


class TestBus:
    def test_voice_audio_sums(self):
        patch = Patch()
        src = patch.add_module("oscillator", params={"amp": 0.2})
        sp = patch.add_module("stereo_speaker_output", params={"pan": -1.0})
        patch.connect(src.id, "out", sp.id, "in_l")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        block = np.stack([np.full(F, 0.1), np.full(F, 0.2)]).astype(np.float32)
        out = np.zeros((F, 2), dtype=np.float32)
        b._drain_stereo_speaker(sp, F, {(src.id, "out"): block}, patch, out)
        assert np.allclose(out[:, 0], 0.3, atol=1e-6)  # voices summed

    def test_two_sinks_add(self):
        patch = Patch()
        a = patch.add_module("oscillator", params={"amp": 0.2})
        sp1 = patch.add_module("stereo_speaker_output", params={"pan": -1.0})
        sp2 = patch.add_module("stereo_speaker_output", params={"pan": -1.0})
        patch.connect(a.id, "out", sp1.id, "in_l")
        patch.connect(a.id, "out", sp2.id, "in_l")
        out, _ = _render(patch, blocks=1)
        p2, src2, spx = _mono_rig(pan=-1.0)
        # single sink at amp 0.5 peaks at 0.5; here 2 x 0.2 -> 0.4
        assert np.max(np.abs(out[:, 0])) == pytest.approx(0.4, abs=1e-3)

    def test_gain_scales_both(self):
        patch, l, r, sp = _stereo_rig(gain=0.5)
        half, _ = _render(patch, blocks=1)
        patch2, l2, r2, sp2 = _stereo_rig(gain=1.0)
        full, _ = _render(patch2, blocks=1)
        assert np.allclose(half, 0.5 * full, atol=1e-7)

    def test_master_clip_applies(self):
        patch, src, sp = _mono_rig(pan=-1.0, gain=2.0)
        # amp 0.5 * gain 2 = 1.0 exactly; push over with two sinks
        src.params["amp"] = 0.9
        out, _ = _render(patch)
        assert np.max(out) <= 1.0
        assert np.min(out) >= -1.0

    def test_uncabled_sink_is_silent(self):
        patch = Patch()
        patch.add_module("stereo_speaker_output")
        out, _ = _render(patch, blocks=1)
        assert not out.any()


class TestWidthCV:
    def _w_rig(self, value, **params):
        patch = Patch()
        l = patch.add_module("oscillator", params={"amp": 0.4})
        r = patch.add_module(
            "oscillator",
            params={"amp": 0.3, "waveform": "square", "freq": 330.0},
        )
        const = patch.add_module("constant", params={"value": value})
        sp = patch.add_module("stereo_speaker_output", params=params)
        patch.connect(l.id, "out", sp.id, "in_l")
        patch.connect(r.id, "out", sp.id, "in_r")
        patch.connect(const.id, "out", sp.id, "width_cv")
        return patch

    def test_constant_cv_equals_static_width(self):
        out_cv, _ = _render(self._w_rig(0.5, width=1.0, cv_depth=1.0))
        patch, l, r, sp = _stereo_rig(width=1.5)
        out_static, _ = _render(patch)
        assert np.allclose(out_cv, out_static, atol=1e-7)

    def test_cv_depth_zero_disables(self):
        out_cv, _ = _render(self._w_rig(1.0, width=1.2, cv_depth=0.0))
        patch, l, r, sp = _stereo_rig(width=1.2)
        out_static, _ = _render(patch)
        assert np.array_equal(out_cv, out_static)

    def test_width_clamped_low_collapses_to_mono(self):
        # width 1 + cv -5 -> clamped at 0 -> mono collapse
        out, _ = _render(self._w_rig(-5.0, width=1.0, cv_depth=1.0))
        assert np.allclose(out[:, 0], out[:, 1], atol=1e-7)

    def test_width_clamped_high_at_two(self):
        out_hi, _ = _render(self._w_rig(10.0, width=1.0, cv_depth=1.0))
        patch, l, r, sp = _stereo_rig(width=2.0)
        out_two, _ = _render(patch)
        assert np.allclose(out_hi, out_two, atol=1e-7)

    def test_shared_depth_scales_width_cv(self):
        # depth 0.25 x cv 2.0 -> +0.5 width
        out_cv, _ = _render(self._w_rig(2.0, width=1.0, cv_depth=0.25))
        patch, l, r, sp = _stereo_rig(width=1.5)
        out_static, _ = _render(patch)
        assert np.allclose(out_cv, out_static, atol=1e-7)

    def test_swept_width_moves_the_side_within_a_render(self):
        # Drive the drain directly with a ramped width_cv: the side
        # level must grow across the block.
        patch = Patch()
        l = patch.add_module("oscillator", params={"amp": 0.4})
        r = patch.add_module("oscillator", params={"amp": 0.3})
        lfo = patch.add_module("lfo")
        sp = patch.add_module("stereo_speaker_output")
        patch.connect(l.id, "out", sp.id, "in_l")
        patch.connect(r.id, "out", sp.id, "in_r")
        patch.connect(lfo.id, "cv", sp.id, "width_cv")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        lb = np.full(F, 0.4, dtype=np.float32)
        rb = np.full(F, -0.2, dtype=np.float32)  # constant side content
        ramp = np.linspace(-1.0, 1.0, F).astype(np.float32)  # width 0 -> 2
        out = np.zeros((F, 2), dtype=np.float32)
        b._drain_stereo_speaker(
            sp, F,
            {(l.id, "out"): lb, (r.id, "out"): rb, (lfo.id, "cv"): ramp},
            patch, out,
        )
        side = out[:, 0] - out[:, 1]
        assert abs(side[0]) < 0.02          # width ~0: no side
        assert side[-1] == pytest.approx(1.2, abs=0.02)  # width ~2: doubled side (0.6 * 2)

    def test_voice_width_cv_is_averaged(self):
        patch = Patch()
        l = patch.add_module("oscillator", params={"amp": 0.4})
        r = patch.add_module("oscillator", params={"amp": 0.3})
        lfo = patch.add_module("lfo")
        sp = patch.add_module("stereo_speaker_output")
        patch.connect(l.id, "out", sp.id, "in_l")
        patch.connect(r.id, "out", sp.id, "in_r")
        patch.connect(lfo.id, "cv", sp.id, "width_cv")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        lb = np.full(F, 0.4, dtype=np.float32)
        rb = np.full(F, -0.2, dtype=np.float32)
        cv2d = np.stack([np.full(F, 1.0), np.full(F, -1.0)]).astype(np.float32)
        out = np.zeros((F, 2), dtype=np.float32)
        b._drain_stereo_speaker(
            sp, F,
            {(l.id, "out"): lb, (r.id, "out"): rb, (lfo.id, "cv"): cv2d},
            patch, out,
        )
        # mean CV 0 -> width 1 -> pair passes unchanged
        assert np.allclose(out[:, 0], lb, atol=1e-6)
        assert np.allclose(out[:, 1], rb, atol=1e-6)

    def test_mono_mode_ignores_width_cv(self):
        patch = Patch()
        src = patch.add_module("oscillator", params={"amp": 0.5})
        const = patch.add_module("constant", params={"value": 1.0})
        sp = patch.add_module("stereo_speaker_output", params={"pan": 0.3})
        patch.connect(src.id, "out", sp.id, "in_l")
        patch.connect(const.id, "out", sp.id, "width_cv")
        out_cv, _ = _render(patch)
        patch2, src2, sp2 = _mono_rig(pan=0.3)
        out_plain, _ = _render(patch2)
        assert np.array_equal(out_cv, out_plain)
