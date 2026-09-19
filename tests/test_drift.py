"""Drift — the smooth wandering random CV.

Pins the contract: an integer tick schedule (ticks at k·round(sr/rate),
`rate_cv` an octave doubling them), bit-exact block-size independence,
seed determinism and divergence, `smooth` targets uniform across ±1,
`walk` a reflected random walk, `glide` 0 == the stepped output and
`glide` 1 a continuous half-cosine between targets (no jumps, even when
a tick lands mid-glide), `trig` one 1 ms pulse per tick carried across
blocks, `clock` patched ticking on edges with the glide sized from the
measured interval, bipolar/unipolar scaling, depth 0, the mode combo
offering drift's own modes (the shared-branch trap), and the example.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.drift import DRIFT_MODES

SR = 44100


def _driver(params=None, sr=SR, block=512, rate_cv=False, clock=False):
    patch = Patch()
    m = patch.add_module("drift", params=params or {})
    keys = {}
    if rate_cv:
        c = patch.add_module("constant")
        patch.connect(c.id, "out", m.id, "rate_cv")
        keys["rate_cv"] = (c.id, "out")
    if clock:
        k = patch.add_module("clock")
        patch.connect(k.id, "out", m.id, "clock")
        keys["clock"] = (k.id, "out")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(frames=block, **bufs):
        return b._render_drift(
            patch.get(m.id), frames,
            {keys[name]: np.asarray(buf, dtype=np.float32) for name, buf in bufs.items()},
            patch)

    step.module = m
    step.patch = patch
    step.backend = b
    return step


def _render(params=None, total=SR * 2, block=512, sr=SR, rate_cv=None, clock=None):
    step = _driver(params, sr=sr, block=block, rate_cv=rate_cv is not None, clock=clock is not None)
    outs = {"cv": [], "stepped": [], "trig": []}
    for i in range(total // block):
        t = np.arange(i * block, (i + 1) * block)
        bufs = {}
        if rate_cv is not None:
            bufs["rate_cv"] = np.full(block, rate_cv, dtype=np.float32)
        if clock is not None:
            bufs["clock"] = clock(t)
        r = step(block, **bufs)
        for k in outs:
            outs[k].append(r[k])
    return {k: np.concatenate(v) for k, v in outs.items()}


def _ticks(trig):
    rises = np.flatnonzero((trig[1:] > 0.5) & (trig[:-1] <= 0.5)) + 1
    return np.concatenate([[0], rises]) if trig[0] > 0.5 else rises


# ----- registration -----------------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["drift"]
    assert cls is get_module_type("drift")
    assert cls.CATEGORY == "Modulation"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["rate_cv", "clock"]
    assert [p.name for p in m.output_ports] == ["cv", "stepped", "trig"]
    assert m.params["mode"] == "smooth"
    assert DRIFT_MODES == ("smooth", "walk")


def test_serialization_round_trip():
    cls = all_module_types()["drift"]
    m = cls(2, params={"mode": "walk", "rate": 3.0, "seed": 9, "bipolar": False})
    assert cls.from_dict(m.to_dict()).params == m.params


# ----- the tick schedule ------------------------------------------------------


def test_ticks_land_on_an_integer_schedule():
    r = _render({"rate": 2.0})
    ticks = _ticks(r["trig"])
    assert ticks[0] == 0                                   # the first draw is at t=0
    assert np.array_equal(np.diff(ticks), np.full(len(ticks) - 1, round(SR / 2.0)))


def test_rate_cv_an_octave_doubles_the_ticks():
    base = len(_ticks(_render({"rate": 2.0}, rate_cv=0.0)["trig"]))
    up = len(_ticks(_render({"rate": 2.0}, rate_cv=1.0)["trig"]))
    off = len(_ticks(_render({"rate": 2.0, "cv_depth": 0.0}, rate_cv=1.0)["trig"]))
    assert up == 2 * base - 1 or up == 2 * base
    assert off == base


def test_trig_is_one_millisecond_wide_and_one_per_tick():
    r = _render({"rate": 4.0})
    ticks = _ticks(r["trig"])
    width = round(0.001 * SR)
    for t in ticks:
        assert np.all(r["trig"][t:t + width] == 1.0)
        assert r["trig"][t + width] == 0.0
    assert int(r["trig"].sum()) == len(ticks) * width


@pytest.mark.parametrize("params", [
    {"rate": 2.0, "seed": 3},
    {"rate": 33.3, "seed": 5, "glide": 0.4},
    {"rate": 7.0, "mode": "walk", "seed": 2, "step": 0.3},
    {"rate": 0.3, "seed": 1, "bipolar": False, "depth": 0.6},
])
def test_block_size_independent_bit_exact(params):
    total = 2048 * 43                                       # a shared boundary
    big = _render(params, total=total, block=2048)
    small = _render(params, total=total, block=64)
    for k in big:
        assert np.array_equal(big[k], small[k]), k


def test_trig_carries_across_a_block_boundary():
    # rate chosen so a tick lands 10 samples before a 64-block boundary.
    r = _render({"rate": SR / 118.0}, total=64 * 40, block=64)
    ticks = _ticks(r["trig"])
    width = round(0.001 * SR)
    for t in ticks[:-1]:
        assert np.all(r["trig"][t:t + width] == 1.0)


# ----- the draws ---------------------------------------------------------------


def test_seed_determinism_and_divergence():
    a = _render({"seed": 4})
    b = _render({"seed": 4})
    c = _render({"seed": 5})
    for k in a:
        assert np.array_equal(a[k], b[k])
    assert not np.array_equal(a["cv"], c["cv"])


def test_smooth_targets_cover_the_range_evenly():
    r = _render({"rate": 40.0, "seed": 7}, total=SR * 10)
    targets = r["stepped"][_ticks(r["trig"])]
    assert len(targets) >= 390
    assert targets.min() < -0.9 and targets.max() > 0.9
    assert abs(targets.mean()) < 0.1
    assert abs(np.std(targets) - 1.0 / np.sqrt(3.0)) < 0.06     # uniform(-1, 1)


def test_walk_is_a_reflected_random_walk():
    r = _render({"rate": 40.0, "seed": 2, "mode": "walk", "step": 0.2}, total=SR * 10)
    targets = r["stepped"][_ticks(r["trig"])]
    moves = np.diff(targets)
    assert np.abs(targets).max() <= 1.0                       # reflected, never outside
    assert 0.12 < np.std(moves) < 0.3                         # ~step, some reflections
    assert np.abs(moves).max() < 0.9                          # a walk, not a fresh draw
    # And it wanders: the running position is far from white -- consecutive
    # targets are correlated where `smooth` targets are not.
    s = _render({"rate": 40.0, "seed": 2}, total=SR * 10)
    st = s["stepped"][_ticks(s["trig"])]
    assert np.corrcoef(targets[:-1], targets[1:])[0, 1] > 0.8
    assert abs(np.corrcoef(st[:-1], st[1:])[0, 1]) < 0.2


def test_walk_step_zero_holds_still():
    r = _render({"mode": "walk", "step": 0.0, "rate": 10.0})
    assert np.all(r["stepped"] == 0.0)
    assert np.all(r["cv"] == 0.0)


# ----- the glide -----------------------------------------------------------------


def test_glide_zero_is_the_stepped_output():
    r = _render({"rate": 5.0, "glide": 0.0, "seed": 3})
    assert np.array_equal(r["cv"], r["stepped"])
    assert len(np.unique(r["stepped"])) >= 8


def test_glide_one_is_a_continuous_half_cosine_between_targets():
    rate = 4.0
    r = _render({"rate": rate, "glide": 1.0, "seed": 3})
    ticks = _ticks(r["trig"])
    interval = round(SR / rate)
    # No jumps: the largest sample-to-sample move is the half-cosine's
    # steepest slope, pi * (b - a) / (2 * interval).
    assert np.abs(np.diff(r["cv"])).max() < np.pi * 2.0 / (2 * interval) * 1.01
    # And between two ticks the curve IS the half-cosine from a to b.
    t0, t1 = ticks[2], ticks[3]
    a, b = r["cv"][t0 - 1], r["stepped"][t0]
    u = (np.arange(t0, t1) - t0 + 1) / interval
    expect = a + (b - a) * 0.5 * (1 - np.cos(np.pi * u))
    assert np.allclose(r["cv"][t0:t1], expect, atol=1e-5)
    assert abs(r["cv"][t1 - 1] - b) < 1e-5                    # arrives exactly


def test_partial_glide_arrives_early_and_holds():
    rate = 4.0
    r = _render({"rate": rate, "glide": 0.25, "seed": 3})
    ticks = _ticks(r["trig"])
    interval = round(SR / rate)
    t0, t1 = ticks[2], ticks[3]
    b = r["stepped"][t0]
    glide_n = round(0.25 * interval)
    assert abs(r["cv"][t0 + glide_n - 1] - b) < 1e-5            # arrived at a quarter
    assert np.all(r["cv"][t0 + glide_n:t1] == r["cv"][t0 + glide_n])   # then flat


def test_a_tick_arriving_mid_glide_continues_from_the_current_value():
    # Speed the clock up under a long glide: the next segment starts from
    # where the previous one had got to, never from the old target.
    step = _driver({"rate": 2.0, "glide": 1.0, "seed": 3}, block=512)
    first = np.concatenate([step()["cv"] for _ in range(4)])       # 2048 samples of a 22050 glide
    step.module.params["rate"] = 40.0                              # now 1102-sample intervals
    nxt = step()["cv"]
    assert abs(nxt[0] - first[-1]) < 0.01                          # continuous across the change
    assert np.abs(np.diff(np.concatenate([first[-64:], nxt]))).max() < 0.01


# ----- clocked ---------------------------------------------------------------------


def test_clock_patched_ticks_on_edges_only():
    period = 3000
    clock = lambda t: ((t % period) < 100).astype(np.float32)
    r = _render({"rate": 40.0, "seed": 1}, clock=clock)            # internal rate ignored
    ticks = _ticks(r["trig"])
    assert np.array_equal(ticks, np.arange(0, SR * 2 - 1, period)[:len(ticks)])
    assert len(ticks) == (SR * 2) // 512 * 512 // period + (1 if ((SR * 2) // 512 * 512) % period else 0)


def test_clocked_glide_uses_the_measured_interval():
    period = 3000
    clock = lambda t: ((t % period) < 100).astype(np.float32)
    r = _render({"seed": 1, "glide": 1.0}, clock=clock)
    ticks = _ticks(r["trig"])
    # First edge: no interval known -> a jump to the first target.
    assert r["cv"][0] == r["stepped"][0]
    # From the second tick on: a half-cosine across exactly one period.
    t0, t1 = ticks[2], ticks[3]
    a, b = r["cv"][t0 - 1], r["stepped"][t0]
    u = (np.arange(t0, t1) - t0 + 1) / period
    assert np.allclose(r["cv"][t0:t1], a + (b - a) * 0.5 * (1 - np.cos(np.pi * u)), atol=1e-5)


# ----- scaling ------------------------------------------------------------------


def test_bipolar_and_unipolar_scaling():
    bi = _render({"rate": 20.0, "seed": 6, "depth": 0.5})
    uni = _render({"rate": 20.0, "seed": 6, "depth": 0.5, "bipolar": False})
    assert bi["cv"].min() < -0.3 and bi["cv"].max() > 0.3 and np.abs(bi["cv"]).max() <= 0.5
    assert uni["cv"].min() >= 0.0 and uni["cv"].max() <= 0.5 and uni["cv"].max() > 0.3
    assert np.allclose(uni["cv"], (bi["cv"] / 0.5 + 1.0) * 0.25, atol=1e-6)


def test_depth_zero_is_exact_zeros_but_the_die_still_rolls():
    r = _render({"depth": 0.0, "rate": 10.0})
    assert np.all(r["cv"] == 0.0) and np.all(r["stepped"] == 0.0)
    assert r["trig"].sum() > 0


# ----- UI -----------------------------------------------------------------------


def test_mode_combo_offers_drifts_own_modes(monkeypatch):
    """The shared ``mode`` branch trap: the dropdown must list smooth/walk,
    not the filter's LP/HP/BP."""
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("drift")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    widgets = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            widgets[str(call.kwargs.get("label"))] = (k, call.kwargs.get("items"))
    assert widgets["mode"] == ("add_combo", list(DRIFT_MODES))
    for name in get_module_type("drift").DEFAULT_PARAMS:
        hits = [lb for lb in widgets if lb == name or lb.startswith(name + " ")]
        assert hits, (name, list(widgets))
        assert widgets[hits[0]][0] != "add_input_text", name


# ----- example --------------------------------------------------------------------


def test_the_example_drifts_a_filter_and_steps_a_pitch():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "drift_wander.json"
    patch = load_patch(path)
    drifts = [m for m in patch if m.TYPE == "drift"]
    assert len(drifts) >= 1
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    cap = {}
    orig = b._render_drift

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.setdefault(module.id, []).append(np.asarray(r["cv"]).copy())
        return r

    b._render_drift = spy
    peak = 0.0
    for _ in range(int(SR * 6 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.05 < peak < 1.0
    for m in drifts:
        cv = np.concatenate(cap[m.id])
        depth = float(m.params["depth"])
        assert cv.max() - cv.min() > 0.5 * depth               # it wandered (the walk is 0.03 deep)
        assert np.abs(np.diff(cv)).max() < 0.01 * depth + 1e-4  # smoothly
