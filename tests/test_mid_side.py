"""MidSide — M/S encode/decode + width, all outs live.

Pins the contract: the decode at width 1 is the identity (bit-close),
mid/side land the exact sum/difference forms (hard-pan included),
width 0 collapses to dual mono, width 2 doubles the side, `width_cv`
adds per sample and clamps to 0..2 at both ends, a single patched
input is a level-preserving mono passthrough with width inert, both
unpatched is silence, and the module drives a real stereo sink.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type

SR = 1000


def _driver(params=None, jacks=("in_l", "in_r"), with_cv=False):
    patch = Patch()
    ms = patch.add_module("mid_side", params=params or {})
    feeds = {}
    for jack in jacks:
        src = patch.add_module("oscillator")
        patch.connect(src.id, "out", ms.id, jack)
        feeds[jack] = src
    if with_cv:
        cv = patch.add_module("constant")
        patch.connect(cv.id, "out", ms.id, "width_cv")
        feeds["width_cv"] = cv
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)

    def step(frames=None, **blocks):
        for arr in blocks.values():
            frames = np.asarray(arr).shape[-1]
            break
        buffers = {
            (feeds[j].id, "out"): np.asarray(arr, dtype=np.float32)
            for j, arr in blocks.items()
        }
        return b._render_mid_side(patch.get(ms.id), frames, buffers, patch)

    step.ms = ms
    step.patch = patch
    return step


def _lr(F=128, seed=0):
    rng = np.random.default_rng(seed)
    return (
        rng.uniform(-1, 1, F).astype(np.float32),
        rng.uniform(-1, 1, F).astype(np.float32),
    )


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["mid_side"]
    assert cls is get_module_type("mid_side")
    assert cls.CATEGORY == "Routing & VCA"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["in_l", "in_r", "width_cv"]
    assert [p.name for p in m.output_ports] == ["mid", "side", "out_l", "out_r"]
    assert m.params["width"] == 1.0


def test_serialization_round_trip():
    cls = all_module_types()["mid_side"]
    m = cls(3, params={"width": 1.7})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the math --------------------------------------------------------------


def test_width_one_decode_is_identity():
    step = _driver()
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["out_l"], L, atol=1e-6)
    np.testing.assert_allclose(out["out_r"], R, atol=1e-6)


def test_encode_lands_sum_and_difference():
    step = _driver()
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["mid"], (L + R) / 2.0, atol=1e-6)
    np.testing.assert_allclose(out["side"], (L - R) / 2.0, atol=1e-6)


def test_hard_pan_encode():
    step = _driver()
    L = np.ones(16, dtype=np.float32)
    R = np.zeros(16, dtype=np.float32)
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["mid"], 0.5, atol=1e-6)
    np.testing.assert_allclose(out["side"], 0.5, atol=1e-6)


def test_width_zero_collapses_to_dual_mono():
    step = _driver({"width": 0.0})
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["out_l"], out["out_r"], atol=1e-9)
    np.testing.assert_allclose(out["out_l"], (L + R) / 2.0, atol=1e-6)


def test_width_two_doubles_the_side():
    step = _driver({"width": 2.0})
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    mid = (L + R) / 2.0
    side = (L - R) / 2.0
    np.testing.assert_allclose(out["out_l"], mid + 2.0 * side, atol=1e-5)
    np.testing.assert_allclose(out["out_r"], mid - 2.0 * side, atol=1e-5)


# ----- width_cv --------------------------------------------------------------


def test_width_cv_adds_per_sample():
    step = _driver({"width": 1.0}, with_cv=True)
    L, R = _lr()
    cv = np.linspace(-0.5, 0.5, L.shape[0]).astype(np.float32)
    out = step(in_l=L, in_r=R, width_cv=cv)
    w = 1.0 + cv.astype(np.float64)
    side = (L.astype(np.float64) - R) / 2.0
    mid = (L.astype(np.float64) + R) / 2.0
    np.testing.assert_allclose(out["out_l"], mid + w * side, atol=1e-5)


def test_width_cv_clamps_both_ends():
    step = _driver({"width": 1.0}, with_cv=True)
    L, R = _lr()
    F = L.shape[0]
    big = np.full(F, 9.0, dtype=np.float32)   # 1 + 9 -> clamp 2
    neg = np.full(F, -9.0, dtype=np.float32)  # 1 - 9 -> clamp 0
    mid = (L.astype(np.float64) + R) / 2.0
    side = (L.astype(np.float64) - R) / 2.0
    out_hi = step(in_l=L, in_r=R, width_cv=big)
    np.testing.assert_allclose(out_hi["out_l"], mid + 2.0 * side, atol=1e-5)
    out_lo = step(in_l=L, in_r=R, width_cv=neg)
    np.testing.assert_allclose(out_lo["out_l"], mid, atol=1e-5)
    np.testing.assert_allclose(out_lo["out_l"], out_lo["out_r"], atol=1e-9)


# ----- mono edges ------------------------------------------------------------


@pytest.mark.parametrize("jack", ["in_l", "in_r"])
def test_single_input_is_level_preserving_mono(jack):
    step = _driver({"width": 2.0}, jacks=(jack,))
    x = np.random.default_rng(1).uniform(-1, 1, 64).astype(np.float32)
    out = step(**{jack: x})
    # The one input IS the mid (not halved), side is 0, width inert.
    np.testing.assert_allclose(out["mid"], x, atol=1e-9)
    assert not np.any(out["side"])
    np.testing.assert_allclose(out["out_l"], x, atol=1e-9)
    np.testing.assert_allclose(out["out_r"], x, atol=1e-9)


def test_both_unpatched_is_silence():
    step = _driver(jacks=())
    out = step(frames=32)
    for name in ("mid", "side", "out_l", "out_r"):
        assert not np.any(out[name])


# ----- integration -----------------------------------------------------------


def test_full_graph_render_to_stereo_sink():
    patch = Patch()
    o1 = patch.add_module("oscillator", params={"freq": 220.0})
    o2 = patch.add_module("oscillator", params={"freq": 223.0})
    ms = patch.add_module("mid_side", params={"width": 1.5})
    lfo = patch.add_module("lfo")
    out = patch.add_module("stereo_speaker_output")
    patch.connect(o1.id, "out", ms.id, "in_l")
    patch.connect(o2.id, "out", ms.id, "in_r")
    patch.connect(lfo.id, "cv", ms.id, "width_cv")
    patch.connect(ms.id, "out_l", out.id, "in_l")
    patch.connect(ms.id, "out_r", out.id, "in_r")
    b = NumpyBackend(sample_rate=44100, block_size=256)
    b.compile(patch)
    for _ in range(8):
        mixdown, _dev = b.render_block_multi(256)
        assert mixdown is not None and np.all(np.isfinite(mixdown))
