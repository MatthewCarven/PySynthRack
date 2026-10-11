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

The afterglow (2026-10-12) adds the face's own contract: opt-in, never
in the audio path, bounded ``glow_frames``, ONE points push per trace
per repaint into a rotating slot with the rest only recoloured, and a
tick-off that hides every slot and returns the face to its old cost —
pinned on a mocked dpg AND once against a real (viewport-less)
DearPyGui context, since a MagicMock accepts any kwarg.
"""
from __future__ import annotations

import itertools
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
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


# ----- afterglow: params and the audio path ---------------------------------


def test_afterglow_defaults_off_and_round_trips():
    cls = all_module_types()["scope"]
    m = cls(1)
    assert m.params["afterglow"] is False
    assert m.params["glow_frames"] == sm.GLOW_FRAMES_DEFAULT == 8
    m2 = cls.from_dict(
        cls(3, params={"afterglow": True, "glow_frames": 16}).to_dict()
    )
    assert m2.params["afterglow"] is True
    assert m2.params["glow_frames"] == 16


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"afterglow": True},
        {"afterglow": True, "glow_frames": 16},
        {"afterglow": True, "glow_frames": 2, "mode": "xy"},
    ],
)
def test_afterglow_never_touches_the_pass_through(params):
    step = _driver(params, wire=("in", "in_r"))
    rng = np.random.default_rng(7)
    x = rng.uniform(-1, 1, 64).astype(np.float32)
    y = rng.uniform(-1, 1, (3, 64)).astype(np.float32)
    out = step(**{"in": x, "in_r": y})
    assert out["out"] is x and out["out_r"] is y  # the very same arrays


def _render_through_scope(scope_params, flips=()):
    """saw -> scope -> speaker, 12 blocks of 128 at 44.1 kHz; ``flips`` are
    (block, param, value) writes through the backend mid-run."""
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": "saw_blep", "freq": 220.0}
    )
    sc = patch.add_module("scope", params=scope_params)
    spk = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", sc.id, "in")
    patch.connect(sc.id, "out", spk.id, "in")
    b = NumpyBackend(sample_rate=44100, block_size=128)
    b.compile(patch)
    outs = []
    for k in range(12):
        for blk, name, value in flips:
            if blk == k:
                b.set_param(sc.id, name, value)
        out, _devices = b.render_block_multi(128)
        outs.append(np.asarray(out).copy())
    return outs


def test_afterglow_leaves_the_render_bit_exact():
    ref = _render_through_scope({})
    assert max(float(np.abs(o).max()) for o in ref) > 0.1  # not vacuous
    for params, flips in (
        ({"afterglow": True}, ()),
        ({"afterglow": True, "glow_frames": 16, "mode": "xy"}, ()),
        ({}, ((3, "afterglow", True), (6, "glow_frames", 2),
              (9, "afterglow", False))),
    ):
        got = _render_through_scope(params, flips)
        assert len(got) == len(ref)
        assert all(np.array_equal(a, b) for a, b in zip(ref, got)), params


# ----- afterglow: scope_math ------------------------------------------------


def test_clamp_glow_frames_bounds_and_junk():
    assert sm.clamp_glow_frames(8) == 8
    assert sm.clamp_glow_frames(1) == sm.GLOW_FRAMES_MIN == 2
    assert sm.clamp_glow_frames(99) == sm.GLOW_FRAMES_MAX == 16
    assert sm.clamp_glow_frames(8.6) == 9
    assert sm.clamp_glow_frames("12") == 12
    for junk in (None, "lots", float("nan"), float("inf")):
        assert sm.clamp_glow_frames(junk) == sm.GLOW_FRAMES_DEFAULT


@pytest.mark.parametrize("frames", [2, 8, 16])
def test_glow_alpha_fades_from_full_to_faint(frames):
    alphas = [sm.glow_alpha(a, frames) for a in range(frames)]
    assert alphas[0] == 255                                  # the live trace
    assert all(b < a for a, b in zip(alphas, alphas[1:]))    # fainter with age
    assert alphas[-1] == round(255 / frames) > 0             # oldest still shows


def test_glow_ring_rotates_one_slot_per_repaint():
    ring = sm.GlowRing(4)
    assert [ring.age(s) for s in range(4)] == [None] * 4
    assert ring.colours((1, 2, 3)) == [None] * 4
    written = [ring.advance() for _ in range(9)]
    assert written == [0, 1, 2, 3, 0, 1, 2, 3, 0]
    assert [ring.age(s) for s in range(4)] == [0, 3, 2, 1]
    assert sm.GlowRing(99).frames == 16 and sm.GlowRing(0).frames == 2


def test_glow_ring_takes_the_oldest_slot_and_hides_unwritten_ones():
    ring = sm.GlowRing(5)
    ring.advance()
    ring.advance()
    cols = ring.colours((10, 20, 30))
    assert cols[0] == (10, 20, 30, sm.glow_alpha(1, 5))
    assert cols[1] == (10, 20, 30, 255)
    assert cols[2:] == [None, None, None]
    for _ in range(3):
        ring.advance()
    for _ in range(7):  # full ring: each new trace replaces the oldest
        ages = [ring.age(s) for s in range(5)]
        assert sorted(ages) == [0, 1, 2, 3, 4]
        assert ring.advance() == ages.index(4)


# ----- afterglow: the face (mocked dpg) -------------------------------------


class _FakeScopeBackend:
    """Just what _update_scopes reads: a sample rate, and a scope_window
    whose content slides on every call (a signal that is running)."""

    sample_rate = 1000

    def __init__(self):
        self.calls = 0

    def scope_window(self, module_id, n):
        self.calls += 1
        t = (np.arange(n) + 13 * self.calls) * 0.07
        return {
            "t1": np.sin(t).astype(np.float32),
            "t2": np.cos(1.3 * t).astype(np.float32),
            "tg": None,
        }


def _face(monkeypatch, **params):
    """A scope node on a mocked dpg, a fake running backend, and
    ``frame()`` -> that repaint's configure_item calls as (tag, kwargs).

    The polylines get DISTINCT ids: a bare MagicMock hands every call the
    same return value, and then every slot would look like every other.
    """
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    dpg = mock.MagicMock()
    ids = itertools.count(50_000)
    dpg.draw_polyline.side_effect = lambda *a, **k: next(ids)
    monkeypatch.setattr(app_mod, "dpg", dpg)
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("scope", params={"trigger": "free", **params})
    app._create_node_for_module(module)
    app.backend = _FakeScopeBackend()
    bundle = app._scope_displays[module.id]

    def frame():
        dpg.configure_item.reset_mock()
        app._update_scopes()
        return [(c.args[0], dict(c.kwargs)) for c in dpg.configure_item.call_args_list]

    return app, module, bundle, frame


def _pushed(calls):
    return [t for t, kw in calls if "points" in kw]


def test_face_builds_hidden_glow_slots_for_both_traces(monkeypatch):
    _app, _m, bundle, _frame = _face(monkeypatch)
    g1, g2 = bundle["g1"], bundle["g2"]
    assert len(g1) == len(g2) == sm.GLOW_FRAMES_MAX
    tags = set(g1) | set(g2) | {bundle["p1"], bundle["p2"]}
    assert len(tags) == 2 * sm.GLOW_FRAMES_MAX + 2   # every one distinct


@pytest.mark.parametrize("mode", ["xy", "mono", "dual"])
def test_afterglow_off_never_touches_a_slot(monkeypatch, mode):
    _app, _m, bundle, frame = _face(monkeypatch, mode=mode)
    slots = set(bundle["g1"]) | set(bundle["g2"])
    want = [bundle["p1"]] + ([bundle["p2"]] if mode == "dual" else [])
    for _ in range(3):
        calls = frame()
        assert _pushed(calls) == want
        assert not [t for t, _kw in calls if t in slots]


@pytest.mark.parametrize("mode", ["xy", "mono"])
def test_afterglow_pushes_one_trace_per_repaint_into_a_rotating_slot(
    monkeypatch, mode
):
    n = 4
    _app, _m, bundle, frame = _face(
        monkeypatch, mode=mode, afterglow=True, glow_frames=n
    )
    g1 = bundle["g1"]
    first = frame()
    assert (bundle["p1"], {"show": False}) in first  # the ring draws it now
    pushed = [_pushed(first)] + [_pushed(frame()) for _ in range(2 * n)]
    assert pushed == [[g1[k % n]] for k in range(2 * n + 1)]


def test_afterglow_fills_up_one_slot_at_a_time(monkeypatch):
    n = 3
    _app, _m, bundle, frame = _face(
        monkeypatch, mode="xy", afterglow=True, glow_frames=n
    )
    g1 = bundle["g1"]
    for k in range(n):
        calls = frame()
        assert {t for t, _kw in calls if t in g1} == set(g1[: k + 1])
        assert [t for t, kw in calls if kw.get("show") is True] == [g1[k]]


def test_afterglow_recolours_the_rest_by_age(monkeypatch):
    n = 4
    app, _m, bundle, frame = _face(
        monkeypatch, mode="xy", afterglow=True, glow_frames=n
    )
    g1 = bundle["g1"]
    for _ in range(n + 2):  # past fill-up: repaint 6 wrote slot (6-1) % 4
        calls = frame()
    newest = (n + 2 - 1) % n
    by_tag = dict(calls)
    assert set(by_tag) == set(g1[:n])  # nothing else on the face touched
    for s in range(n):
        kw = by_tag[g1[s]]
        age = (newest - s) % n
        assert kw["color"] == (*app._SCOPE_RGB1, sm.glow_alpha(age, n))
        assert ("points" in kw) == (s == newest)  # the rest: colour only
        assert "show" not in kw                   # shown once, at fill-up


def test_tick_off_hides_the_glow_and_the_face_costs_what_it_did(monkeypatch):
    _app, module, bundle, frame = _face(
        monkeypatch, mode="xy", afterglow=True, glow_frames=5
    )
    for _ in range(7):
        frame()
    module.params["afterglow"] = False
    calls = frame()
    assert {t for t, kw in calls if kw == {"show": False}} == set(bundle["g1"][:5])
    assert (bundle["p1"], {"show": True}) in calls
    assert _pushed(calls) == [bundle["p1"]]
    # From the next repaint on: exactly the afterglow-off face, one call.
    assert frame() == [(bundle["p1"], {"points": mock.ANY})]


def test_dual_glows_both_traces_and_a_change_starts_afresh(monkeypatch):
    n = 3
    app, module, bundle, frame = _face(
        monkeypatch, mode="dual", afterglow=True, glow_frames=n
    )
    g1, g2 = bundle["g1"], bundle["g2"]
    for _ in range(n + 1):
        calls = frame()
    assert _pushed(calls) == [g1[0], g2[0]]
    by_tag = dict(calls)
    assert by_tag[g2[0]]["color"] == (*app._SCOPE_RGB2, 255)
    assert by_tag[g2[1]]["color"] == (*app._SCOPE_RGB2, sm.glow_alpha(2, n))
    # mode -> xy: every ghost hides (wrong shape), ring 1 restarts at slot
    # 0, and trace 2 has no ring (xy draws one polyline from both).
    module.params["mode"] = "xy"
    calls = frame()
    assert {t for t, kw in calls if kw == {"show": False}} == set(g1[:n]) | set(g2[:n])
    assert _pushed(calls) == [g1[0]]
    # glow_frames change: same again, the new count from slot 0
    frame()
    module.params["glow_frames"] = 6
    calls = frame()
    assert {t for t, kw in calls if kw == {"show": False}} == {g1[0], g1[1]}
    assert _pushed(calls) == [g1[0]]


def test_freeze_holds_the_glow_too(monkeypatch):
    _app, module, _bundle, frame = _face(
        monkeypatch, mode="xy", afterglow=True, glow_frames=4
    )
    frame()
    frame()
    module.params["freeze"] = True
    assert frame() == []


def test_every_param_gets_a_bounded_widget(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("scope")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int", "add_slider_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    widgets = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            widgets[str(call.kwargs.get("label"))] = (k, call.kwargs)
    for name in get_module_type("scope").DEFAULT_PARAMS:
        assert name in widgets, (name, list(widgets))
        assert widgets[name][0] != "add_input_text", name
    assert widgets["afterglow"][0] == "add_checkbox"
    kind, kw = widgets["glow_frames"]
    assert kind == "add_drag_int"
    assert (kw["min_value"], kw["max_value"]) == (2, 16) and kw["clamped"] is True
    assert kw["default_value"] == 8 and kw["format"] == "%d frames"
    assert kw["callback"] == app._on_param_changed  # -> _set_module_param
    assert all(ord(ch) < 128 for lb in widgets for ch in lb)
    assert all(ord(ch) < 128 for _k, kw in widgets.values()
               for ch in str(kw.get("format", "")))


# ----- afterglow: the face against a real DearPyGui ------------------------


def test_afterglow_against_a_real_dearpygui_context(monkeypatch):
    """A MagicMock accepts any kwarg; this proves real DearPyGui takes the
    face's calls and ends up holding what the ring says. No viewport is
    created, so nothing is shown."""
    dpg = pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", dpg)
    dpg.create_context()
    try:
        app = app_mod.App()
        app._build_ui()
        app.patch = Patch()
        module = app.patch.add_module(
            "scope",
            params={"trigger": "free", "mode": "xy", "afterglow": True,
                    "glow_frames": 4},
        )
        app._create_node_for_module(module)
        app.backend = _FakeScopeBackend()
        bundle = app._scope_displays[module.id]
        for _ in range(6):
            app._update_scopes()
        cfg = [dpg.get_item_configuration(t) for t in bundle["g1"]]
        assert [c["show"] for c in cfg] == [True] * 4 + [False] * 12
        assert dpg.get_item_configuration(bundle["p1"])["show"] is False
        newest = (6 - 1) % 4
        for s in range(4):
            alpha = round(cfg[s]["color"][3] * 255)
            assert alpha == sm.glow_alpha((newest - s) % 4, 4)
        assert len(cfg[newest]["points"]) >= 2
        module.params["afterglow"] = False
        app._update_scopes()
        assert not any(
            dpg.get_item_configuration(t)["show"] for t in bundle["g1"]
        )
        assert dpg.get_item_configuration(bundle["p1"])["show"] is True
    finally:
        dpg.destroy_context()
