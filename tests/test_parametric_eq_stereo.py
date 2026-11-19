"""parametric_eq's stereo jacks: ``in_r`` -> ``out_r`` through the same bands.

Matthew, 2026-11-19, after the stereo pitch_shifter: "can we do the same for
tape and parametric eq? Its just the convenience really for the parametric eq
... i can see people using like 3 output devices and 3 separate stereo sources
in the future". An EQ is a fixed filter, so two of them with matching settings
already keep the sides in time; the stereo jack is one node and one set of
knobs per stereo source (his own song patch had the two EQs a Q of 1.06 vs
1.00 apart). These tests pin:

  * the jacks, and that ``in_r`` empty is the mono EQ (no dict, no right
    state, ``out_r`` carries nothing);
  * each side renders exactly as a lone EQ with these settings renders it,
    so no memory crosses between the sides;
  * the same signal both sides gives the same output both sides;
  * a knob turned mid-stream moves both sides;
  * shapes, a missing side, and patching ``in_r`` in and out.
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
F = 512
PARAMS = {
    "band1_freq": 20.0, "band1_gain": 24.0, "band1_q": 1.06,   # his patch
    "band2_freq": 180.0, "band2_gain": -6.0, "band2_q": 2.0,
    "band3_freq": 2500.0, "band3_gain": 4.0, "band3_q": 0.8,
}


def _noise(seconds, seed):
    rng = np.random.default_rng(seed)
    return (0.2 * rng.standard_normal(int(seconds * SR))).astype(np.float32)


def _rig(params=PARAMS, wire_left=True, wire_right=True):
    patch = Patch()
    src_l = patch.add_module("oscillator")
    src_r = patch.add_module("oscillator")
    eq = patch.add_module("parametric_eq", params=dict(params))
    if wire_left:
        patch.connect(src_l.id, "out", eq.id, "in")
    if wire_right:
        patch.connect(src_r.id, "out", eq.id, "in_r")
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    return patch, b, eq, src_l, src_r


def _run(rig, xl, xr, block=F, on_block=None):
    patch, b, eq, src_l, src_r = rig
    outs = {"out": [], "out_r": []}
    for k in range(xl.shape[-1] // block):
        if on_block is not None:
            on_block(k, eq)
        sl = slice(k * block, (k + 1) * block)
        r = b._render_parametric_eq(
            eq, block, {(src_l.id, "out"): xl[..., sl], (src_r.id, "out"): xr[..., sl]}, patch
        )
        if not isinstance(r, dict):
            r = {"out": r}
        for key in outs:
            if key in r:
                outs[key].append(np.asarray(r[key]).copy())
    return {k: np.concatenate(v, axis=-1) for k, v in outs.items() if v}


def _lone(x, params=PARAMS, block=F, on_block=None):
    """A mono EQ with these settings, on its own."""
    rig = _rig(params, wire_right=False)
    return _run(rig, x, np.zeros_like(x), block, on_block)["out"]


class TestModel:
    def test_jacks(self):
        eq = Patch().add_module("parametric_eq")
        assert [(p.name, p.signal_kind) for p in eq.input_ports] == [
            ("in", "audio"), ("in_r", "audio")]
        assert [(p.name, p.signal_kind) for p in eq.output_ports] == [
            ("out", "audio"), ("out_r", "audio")]

    def test_audio_into_in_r_accepted_cv_rejected(self):
        import pytest
        patch = Patch()
        osc = patch.add_module("oscillator")
        lfo = patch.add_module("lfo")
        eq = patch.add_module("parametric_eq")
        patch.connect(osc.id, "out", eq.id, "in_r")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", eq.id, "in")


class TestMonoUntouched:
    def test_in_r_empty_is_the_mono_eq(self):
        x = _noise(1.0, 1)
        patch, b, eq, src_l, src_r = rig = _rig(wire_right=False)
        r = b._render_parametric_eq(eq, F, {(src_l.id, "out"): x[:F]}, patch)
        assert isinstance(r, np.ndarray)          # no out_r key published
        assert "right" not in b._state[eq.id]
        del rig


class TestStereo:
    def test_each_side_is_a_lone_eq(self):
        xl, xr = _noise(2.0, 1), _noise(2.0, 2)
        y = _run(_rig(), xl, xr)
        np.testing.assert_array_equal(y["out"], _lone(xl))
        np.testing.assert_array_equal(y["out_r"], _lone(xr))

    def test_same_signal_both_sides_same_output(self):
        x = _noise(2.0, 3)
        y = _run(_rig(), x, x)
        np.testing.assert_array_equal(y["out"], y["out_r"])

    def test_a_knob_turned_mid_stream_moves_both_sides(self):
        xl, xr = _noise(2.0, 4), _noise(2.0, 5)

        def turn(k, eq):
            if k == 60:
                eq.params["band2_gain"] = 9.0
        y = _run(_rig(), xl, xr, on_block=turn)
        np.testing.assert_array_equal(y["out"], _lone(xl, on_block=turn))
        np.testing.assert_array_equal(y["out_r"], _lone(xr, on_block=turn))

    def test_block_size_does_not_matter(self):
        xl, xr = _noise(1.5, 6), _noise(1.5, 7)
        a = _run(_rig(), xl, xr, block=64)
        b = _run(_rig(), xl, xr, block=512)
        n = min(a["out_r"].shape[-1], b["out_r"].shape[-1])
        np.testing.assert_allclose(a["out_r"][:n], b["out_r"][:n], rtol=0, atol=1e-6)
        np.testing.assert_allclose(a["out"][:n], b["out"][:n], rtol=0, atol=1e-6)


class TestEdges:
    def test_voice_left_mono_right(self):
        xl, xr = _noise(1.0, 8), _noise(1.0, 9)
        L = np.stack([xl, 0.5 * xl]).astype(np.float32)
        y = _run(_rig(), L, xr)
        assert y["out"].shape[0] == 2 and y["out_r"].ndim == 1
        np.testing.assert_array_equal(y["out_r"], _lone(xr))

    def test_voices_both_sides_stay_apart(self):
        xl, xr = _noise(1.0, 10), _noise(1.0, 11)
        L = np.stack([xl, xr]).astype(np.float32)
        R = np.stack([xr, xl]).astype(np.float32)
        y = _run(_rig(), L, R)
        np.testing.assert_array_equal(y["out"][0], y["out_r"][1])
        np.testing.assert_array_equal(y["out"][1], y["out_r"][0])

    def test_only_in_r_patched(self):
        xr = _noise(1.0, 12)
        y = _run(_rig(wire_left=False), np.zeros_like(xr), xr)
        assert not y["out"].any()
        np.testing.assert_array_equal(y["out_r"], _lone(xr))

    def test_unpatching_in_r_drops_the_right_memory(self):
        xl, xr = _noise(1.0, 13), _noise(1.0, 14)
        patch, b, eq, src_l, src_r = _rig()

        def block(k):
            sl = slice(k * F, (k + 1) * F)
            return b._render_parametric_eq(
                eq, F, {(src_l.id, "out"): xl[sl], (src_r.id, "out"): xr[sl]}, patch)

        for k in range(10):
            assert isinstance(block(k), dict)
        assert "right" in b._state[eq.id]
        assert patch.disconnect(src_r.id, "out", eq.id, "in_r")
        block(10)
        assert "right" not in b._state[eq.id]
        patch.connect(src_r.id, "out", eq.id, "in_r")
        r = block(11)
        # The right side starts from rest: block 11 alone through a fresh EQ.
        fresh = _lone(xr[11 * F:12 * F])
        np.testing.assert_array_equal(r["out_r"], fresh)


def test_through_render_block_into_a_stereo_speaker():
    patch = Patch()
    osc_l = patch.add_module("oscillator", params={"waveform": "sine", "freq": 100.0, "amp": 0.3})
    osc_r = patch.add_module("oscillator", params={"waveform": "sine", "freq": 1000.0, "amp": 0.3})
    eq = patch.add_module("parametric_eq", params={
        "band1_freq": 100.0, "band1_gain": 12.0, "band1_q": 1.0})
    spk = patch.add_module("stereo_speaker_output")
    patch.connect(osc_l.id, "out", eq.id, "in")
    patch.connect(osc_r.id, "out", eq.id, "in_r")
    patch.connect(eq.id, "out", spk.id, "in_l")
    patch.connect(eq.id, "out_r", spk.id, "in_r")
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    y = np.concatenate([b.render_block(F) for _ in range(80)])[20 * F:]
    rms_l = float(np.sqrt(np.mean(y[:, 0] ** 2)))
    rms_r = float(np.sqrt(np.mean(y[:, 1] ** 2)))
    # The 100 Hz left is boosted ~12 dB by the band; the 1 kHz right is not.
    assert rms_l > 3.0 * rms_r
    assert rms_r > 0.05
