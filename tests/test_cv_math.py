"""CVMath — two-input CV algebra, every jack live.

Pins the contract: seven outputs exact against numpy on random operands,
the unpatched-operand identities (a lone ``a`` makes ``max``/``min`` its
half-wave rectifiers, ``diff`` == ``a`` bit-exact, ``mult`` silence),
both-unpatched zeros, poly × mono broadcast with single-voice ≡ mono,
float32 out, statelessness, the widget sweep (no params, no widgets),
and the example (delayed vibrato through ``mult``, a two-LFO ``max``).
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules import cv_math as _cv_math  # noqa: F401

OUTS = ("min", "max", "avg", "diff", "mult", "rect", "inv")


def _driver(a=True, b=True, block=64):
    patch = Patch()
    m = patch.add_module("cv_math")
    keys = {}
    if a:
        ca = patch.add_module("constant")
        patch.connect(ca.id, "out", m.id, "a")
        keys["a"] = (ca.id, "out")
    if b:
        cb = patch.add_module("constant")
        patch.connect(cb.id, "out", m.id, "b")
        keys["b"] = (cb.id, "out")
    backend = NumpyBackend(sample_rate=1000, block_size=block)
    backend.compile(patch)

    def step(**bufs):
        frames = next(iter(bufs.values())).shape[-1] if bufs else block
        return backend._render_cv_math(
            patch.get(m.id), frames,
            {keys[k]: np.asarray(v, dtype=np.float32) for k, v in bufs.items()}, patch)

    step.module = m
    step.backend = backend
    return step


def _rand(shape, seed):
    return np.random.default_rng(seed).uniform(-1.5, 1.5, shape).astype(np.float32)


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_no_params():
    cls = all_module_types()["cv_math"]
    assert cls is get_module_type("cv_math")
    assert cls.CATEGORY == "CV & Utilities"
    m = cls(1)
    assert m.params == {}
    assert [(p.name, p.signal_kind) for p in m.input_ports] == [("a", "cv"), ("b", "cv")]
    assert [(p.name, p.signal_kind) for p in m.output_ports] == [(o, "cv") for o in OUTS]


def test_serialization_round_trip():
    cls = all_module_types()["cv_math"]
    m = cls(2)
    assert cls.from_dict(m.to_dict()).params == {}


# ----- the math ----------------------------------------------------------------


def test_every_output_is_exact_against_numpy():
    a, b = _rand(64, 1), _rand(64, 2)
    r = _driver()(a=a, b=b)
    assert np.array_equal(r["min"], np.minimum(a, b))
    assert np.array_equal(r["max"], np.maximum(a, b))
    assert np.array_equal(r["avg"], (a + b) * np.float32(0.5))
    assert np.array_equal(r["diff"], a - b)
    assert np.array_equal(r["mult"], a * b)
    assert np.array_equal(r["rect"], np.abs(a))
    assert np.array_equal(r["inv"], -a)
    for k in OUTS:
        assert r[k].dtype == np.float32 and r[k].shape == (64,)


def test_min_and_max_are_the_analog_and_or():
    # A gate-shaped a against a slow b: max tracks whichever is higher,
    # min whichever is lower -- and where a > b, min == b exactly.
    a = np.where(np.arange(64) % 16 < 8, 1.0, 0.0).astype(np.float32)
    b = np.linspace(-0.5, 0.5, 64, dtype=np.float32)
    r = _driver()(a=a, b=b)
    assert np.all(r["max"] >= a) and np.all(r["max"] >= b)
    assert np.all(r["min"] <= a) and np.all(r["min"] <= b)
    assert np.array_equal(r["min"][a > b], b[a > b])


def test_a_lone_operand_gets_the_normalled_identities():
    a = _rand(64, 3)
    r = _driver(b=False)(a=a)
    assert np.array_equal(r["max"], np.maximum(a, 0.0))     # positive half-wave
    assert np.array_equal(r["min"], np.minimum(a, 0.0))     # negative half-wave
    assert np.array_equal(r["avg"], a * np.float32(0.5))
    assert np.array_equal(r["diff"], a)                     # bit-exact passthrough
    assert np.all(r["mult"] == 0.0)
    assert np.array_equal(r["rect"], np.abs(a))
    assert np.array_equal(r["inv"], -a)


def test_only_b_patched():
    b = _rand(64, 4)
    r = _driver(a=False)(b=b)
    assert np.array_equal(r["diff"], -b)
    assert np.array_equal(r["max"], np.maximum(b, 0.0))
    assert np.all(r["rect"] == 0.0) and np.all(r["inv"] == 0.0) and np.all(r["mult"] == 0.0)


def test_both_unpatched_is_all_zeros_and_stateless():
    step = _driver(a=False, b=False)
    r = step()
    for k in OUTS:
        assert r[k].shape == (64,) and np.all(r[k] == 0.0)
    assert step.module.id not in step.backend._state


# ----- shapes ----------------------------------------------------------------------


def test_voiced_times_mono_broadcasts_across_voices():
    va, b = _rand((3, 64), 5), _rand(64, 6)
    r = _driver()(a=va, b=b)
    for k in OUTS:
        assert r[k].shape == (3, 64), k
    assert np.array_equal(r["mult"], va * b[None, :])
    assert np.array_equal(r["max"], np.maximum(va, b[None, :]))
    # The a-only functions come back voiced too (every jack agrees on width).
    assert np.array_equal(r["rect"], np.abs(va))
    # And a voiced b under a mono a: rect/inv are a broadcast, not a crash.
    r2 = _driver()(a=b, b=va)
    assert r2["rect"].shape == (3, 64)
    assert np.array_equal(r2["rect"][1], np.abs(b))
    assert np.array_equal(r2["diff"], b[None, :] - va)


def test_single_voice_row_matches_mono():
    a, b = _rand(64, 7), _rand(64, 8)
    mono = _driver()(a=a, b=b)
    voiced = _driver()(a=a[None, :], b=b[None, :])
    for k in OUTS:
        assert voiced[k].shape == (1, 64)
        assert np.array_equal(mono[k], voiced[k][0]), k


def test_two_voiced_operands_of_different_width_line_up_on_the_smaller():
    va, vb = _rand((4, 64), 9), _rand((2, 64), 10)
    r = _driver()(a=va, b=vb)
    for k in OUTS:
        assert r[k].shape == (2, 64), k
    assert np.array_equal(r["mult"], va[:2] * vb)


def test_renders_are_pure():
    a, b = _rand(64, 11), _rand(64, 12)
    step = _driver()
    first = step(a=a, b=b)
    second = step(a=a, b=b)
    for k in OUTS:
        assert np.array_equal(first[k], second[k])
    assert step.module.id not in step.backend._state


# ----- UI -----------------------------------------------------------------------


def test_the_node_has_jacks_and_no_param_widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("cv_math")
    app._create_node_for_module(module)
    assert [v for v in app._param_widgets.values() if v[0] == module.id] == []
    jacks = {k[1] for k in app._port_to_attr if k[0] == module.id}
    assert jacks == {"a", "b", *OUTS}


# ----- example --------------------------------------------------------------------


def test_the_example_delays_the_vibrato_and_shapes_the_filter():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "cv_math_delayed_vibrato.json"
    patch = load_patch(path)
    maths = {m.name.split(" ")[0]: m for m in patch if m.TYPE == "cv_math"}
    assert set(maths) == {"VIBRATO", "SHAPE"}
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    cap = {}
    orig = b._render_cv_math

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.setdefault(module.id, []).append({k: np.asarray(v).copy() for k, v in r.items()})
        return r

    b._render_cv_math = spy
    peak = 0.0
    for _ in range(int(44100 * 4 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.05 < peak < 1.0
    # VIBRATO.mult = lfo x envelope: silent at the note's start, wobbling
    # once the envelope is up; the first note starts at t=0 with a 0.6 s
    # attack, so the wobble grows over the first half second.
    vib = np.concatenate([c["mult"] for c in cap[maths["VIBRATO"].id]])
    early = np.abs(vib[:2205]).max()
    later = np.abs(vib[22050:33075]).max()
    assert later > 3.0 * early and later > 0.005
    # SHAPE.max of two LFOs is never below either -- and is not either.
    shape = np.concatenate([c["max"] for c in cap[maths["SHAPE"].id]])
    assert shape.max() > 0.5 and shape.min() > -0.9
