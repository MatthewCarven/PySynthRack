"""Scope — the oscilloscope pass-through tap.

Pins the contract: bit-exact pass-through (mono AND voice-shaped, the
meter precedent), the cv jack as fallback main trace, capture-ring
accumulation + the scope_window hook, min/max decimation that never
misses a one-sample spike, trigger search (rising/falling/free,
phase-lock across snapshots, external trig override), snapshot column
shapes, xy decimation, block-size-independent capture, and geometry
helpers (envelope zig-zag, xy mapping).

Display maths comes from the dpg-free ui/scope_math.py, so the whole
pipeline runs headless: renderer → scope_window → build_snapshot.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules import scope as _scope  # noqa: F401
from pysynthrack.ui import scope_math as sm

SR = 1000


def _driver(params=None, wire=("in",), block=64):
    """Backend + a scope patch with the given jacks wired.

    Sources match the jack's signal kind (the patch model enforces it):
    oscillator for the audio jacks, constant for "cv", clock for "trig".
    Buffers are injected by hand per step; unwired jacks are unpatched.
    """
    kind_src = {"in": "oscillator", "in_r": "oscillator",
                "cv": "constant", "trig": "clock"}
    patch = Patch()
    scope = patch.add_module("scope", params=params or {})
    feeds = {}
    for jack in wire:
        src = patch.add_module(kind_src[jack])
        patch.connect(src.id, "out", scope.id, jack)
        feeds[jack] = src
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(**blocks):
        frames = len(next(iter(blocks.values())))
        buffers = {}
        for jack, arr in blocks.items():
            buffers[(feeds[jack].id, "out")] = np.asarray(arr, dtype=np.float32)
        return b._render_scope(patch.get(scope.id), frames, buffers, patch)

    step.scope = scope
    step.patch = patch
    step.backend = b
    return step


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["scope"]
    assert cls is get_module_type("scope")
    assert cls.CATEGORY == "CV & Utilities"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["in", "in_r", "cv", "trig"]
    assert [p.name for p in m.output_ports] == ["out", "out_r"]
    assert m.params["mode"] == "mono"
    assert m.params["trigger"] == "rising"


def test_serialization_round_trip():
    cls = all_module_types()["scope"]
    m = cls(2, params={"time_div": 50.0, "mode": "xy", "freeze": True})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- pass-through ----------------------------------------------------------


def test_passthrough_is_bit_exact_mono():
    step = _driver()
    x = np.random.default_rng(1).uniform(-1, 1, 64).astype(np.float32)
    out = step(**{"in": x})
    assert out["out"] is x  # the very same array, meter precedent


def test_passthrough_preserves_voice_shape():
    step = _driver()
    x = np.random.default_rng(2).uniform(-1, 1, (4, 64)).astype(np.float32)
    out = step(**{"in": x})
    assert out["out"] is x
    assert out["out"].shape == (4, 64)


def test_unpatched_inputs_output_silence():
    patch = Patch()
    scope = patch.add_module("scope")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    out = b._render_scope(patch.get(scope.id), 64, {}, patch)
    assert np.all(out["out"] == 0.0)
    assert np.all(out["out_r"] == 0.0)


# ----- capture ring / scope_window ------------------------------------------


def test_capture_ring_accumulates_and_returns_tail():
    step = _driver(block=32)
    a = np.arange(32, dtype=np.float32)
    b_ = np.arange(32, 64, dtype=np.float32)
    step(**{"in": a})
    step(**{"in": b_})
    win = step.backend.scope_window(step.scope.id, 48)
    assert win is not None
    assert np.array_equal(win["t1"], np.arange(16, 64, dtype=np.float32))
    assert win["t2"] is None
    assert win["tg"] is None


def test_cv_jack_is_fallback_main_trace():
    step = _driver(wire=("cv",))
    step(cv=np.full(64, 0.25, dtype=np.float32))
    win = step.backend.scope_window(step.scope.id, 64)
    assert np.all(win["t1"] == np.float32(0.25))


def test_voice_input_captures_the_sum():
    step = _driver()
    x = np.stack([np.full(64, 0.5), np.full(64, 0.25)]).astype(np.float32)
    step(**{"in": x})
    win = step.backend.scope_window(step.scope.id, 64)
    assert np.allclose(win["t1"], 0.75)


def test_window_capped_to_filled_and_none_when_empty():
    step = _driver()
    assert step.backend.scope_window(step.scope.id, 64) is None  # no state yet
    step(**{"in": np.ones(64, dtype=np.float32)})
    win = step.backend.scope_window(step.scope.id, 10_000)
    assert len(win["t1"]) == 64  # only what's been captured


def test_capture_is_block_size_independent():
    sig = np.sin(np.linspace(0, 20, 256)).astype(np.float32)
    big = _driver(block=256)
    small = _driver(block=32)
    big(**{"in": sig})
    for i in range(0, 256, 32):
        small(**{"in": sig[i : i + 32]})
    wa = big.backend.scope_window(big.scope.id, 256)
    wb = small.backend.scope_window(small.scope.id, 256)
    assert np.array_equal(wa["t1"], wb["t1"])


# ----- scope_math: decimation ------------------------------------------------


def test_minmax_never_misses_a_one_sample_spike():
    x = np.zeros(10_000, dtype=np.float32)
    x[6173] = 1.0  # a single-sample click
    cols = sm.minmax_columns(x, 200)
    assert cols.shape == (200, 2)
    assert cols[:, 1].max() == 1.0  # the spike survives into its column


def test_minmax_column_count_exact_and_short_input_upsamples():
    cols = sm.minmax_columns(np.array([1.0, -1.0, 0.5]), 10)
    assert cols.shape == (10, 2)
    assert cols[0, 1] == 1.0 and cols[-1, 0] == 0.5


def test_minmax_empty_input_is_zeros():
    cols = sm.minmax_columns(np.array([]), 8)
    assert cols.shape == (8, 2)
    assert np.all(cols == 0.0)


def test_decimate_points_short_input_passthrough():
    x = np.arange(5, dtype=np.float32)
    assert np.array_equal(sm.decimate_points(x, 10), x)
    assert len(sm.decimate_points(np.arange(1000, dtype=np.float32), 10)) == 10


# ----- scope_math: trigger ---------------------------------------------------


def test_trigger_rising_finds_last_fitting_crossing():
    x = np.array([-1, 1, -1, 1, -1, 1, -1, -1], dtype=np.float32)
    # window 4 → search in x[:5]; rising crossings at 1 and 3 → picks 3.
    assert sm.find_trigger_index(x, "rising", 0.0, 4) == 3


def test_trigger_falling_and_free():
    x = np.array([1, -1, 1, -1, 1, -1, 1, 1], dtype=np.float32)
    assert sm.find_trigger_index(x, "falling", 0.0, 4) == 3
    assert sm.find_trigger_index(x, "free", 0.0, 4) is None


def test_trigger_none_when_no_crossing():
    x = np.ones(64, dtype=np.float32)
    assert sm.find_trigger_index(x, "rising", 0.0, 8) is None


def test_trigger_phase_locks_consecutive_snapshots():
    # A sine whose period divides the feed length: two snapshots taken
    # a whole number of periods apart must be identical. The 0.3 rad
    # phase offset keeps zero crossings BETWEEN samples — on-sample
    # zeros round to ±2e-15 and make the trigger sample ambiguous.
    period = 50  # samples at SR=1000 → 20 Hz
    t = np.arange(1000, dtype=np.float64)
    sig = np.sin(2 * np.pi * t / period + 0.3).astype(np.float32)
    step = _driver({"time_div": 10.0}, block=100)  # window 100 = 2 periods
    for i in range(0, 1000, 100):
        step(**{"in": sig[i : i + 100]})
    need = sm.request_samples(10.0, SR)
    snap1 = sm.build_snapshot(
        step.backend.scope_window(step.scope.id, need),
        step.scope.params, SR, 100,
    )
    step(**{"in": sig[:100]})  # two more whole periods
    snap2 = sm.build_snapshot(
        step.backend.scope_window(step.scope.id, need),
        step.scope.params, SR, 100,
    )
    assert np.allclose(snap1["cols1"], snap2["cols1"], atol=1e-6)


def test_external_trig_overrides_level_trigger():
    step = _driver({"time_div": 2.0}, wire=("in", "trig"), block=64)
    sig = np.linspace(-1, 1, 64).astype(np.float32)  # one rising ramp
    trig = np.zeros(64, dtype=np.float32)
    trig[10] = 1.0  # external edge at sample 10
    step(**{"in": sig, "trig": trig})
    need = sm.request_samples(2.0, SR)
    snap = sm.build_snapshot(
        step.backend.scope_window(step.scope.id, need),
        step.scope.params, SR, 20,
    )
    # window = 20 samples starting at the trig edge → values sig[10:30].
    assert np.isclose(snap["cols1"][0, 0], sig[10], atol=0.05)


# ----- scope_math: snapshots -------------------------------------------------


def test_snapshot_shapes_mono_dual_xy():
    step = _driver(wire=("in", "in_r"))
    x = np.sin(np.linspace(0, 30, 256)).astype(np.float32)
    step(**{"in": x[:64], "in_r": x[:64]})
    win = step.backend.scope_window(step.scope.id, 64)

    mono = sm.build_snapshot(win, {"time_div": 5.0, "mode": "mono"}, SR, 40)
    assert mono["cols1"].shape == (40, 2) and mono["cols2"] is None

    dual = sm.build_snapshot(win, {"time_div": 5.0, "mode": "dual"}, SR, 40)
    assert dual["cols2"].shape == (40, 2)

    xy = sm.build_snapshot(win, {"time_div": 5.0, "mode": "xy"}, SR, 40)
    assert xy["cols1"] is None
    assert len(xy["xy1"]) == len(xy["xy2"]) <= 41


def test_snapshot_none_when_nothing_patched():
    assert sm.build_snapshot(None, {}, SR, 10) is None
    assert sm.build_snapshot(
        {"t1": None, "t2": None, "tg": None}, {}, SR, 10
    ) is None


# ----- scope_math: geometry --------------------------------------------------


def test_envelope_polyline_covers_extremes_and_respects_gain():
    cols = np.array([[-1.0, 1.0], [0.0, 0.0]], dtype=np.float32)
    pts = sm.envelope_polyline(cols, 100.0, 100.0, gain=1.0)
    ys = [p[1] for p in pts]
    assert min(ys) == 1.0 and max(ys) == 99.0  # full face, 1 px margin
    # gain 0.5 halves the extent
    pts_g = sm.envelope_polyline(cols, 100.0, 100.0, gain=0.5)
    ys_g = [p[1] for p in pts_g]
    assert min(ys_g) > 25.0 and max(ys_g) < 75.0


def test_envelope_polyline_flat_column_emits_single_point():
    cols = np.array([[0.5, 0.5]], dtype=np.float32)
    assert len(sm.envelope_polyline(cols, 10.0, 10.0, 1.0)) == 1


def test_xy_polyline_maps_and_clips():
    x = np.array([0.0, 1.0, -1.0, 5.0], dtype=np.float32)
    y = np.array([0.0, 1.0, -1.0, -5.0], dtype=np.float32)
    pts = sm.xy_polyline(x, y, 100.0, 100.0, 1.0)
    assert pts[0] == (50.0, 50.0)          # origin → centre
    assert pts[1] == (99.0, 1.0)           # (+1,+1) → top right
    assert pts[3] == (99.0, 99.0)          # clipped over-range


def test_window_samples_math():
    assert sm.window_samples(10.0, 1000) == 100   # 10 ms × 10 div at 1 kHz
    assert sm.window_samples(500.0, 44100) == 220500
    assert sm.window_samples(0.0, 1000) >= 2      # floor, never zero
