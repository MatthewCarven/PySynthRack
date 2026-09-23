"""Tests for ``autopan`` -- module #101, the stereo motion utility.

A panner with its own LFO: one input cabled is a mono source placed by a
law (power / compromise / linear), both cabled is a stereo pair under a
balance control; ``position = clip(pan + pan_cv + depth * lfo)``; the LFO
phase is keyed to an absolute sample count (or to the clock through
``_mod_clock_sync``); the square glides between sides in 10 ms; and
``tremolo`` is the phase between the two sides (1 = a mono tremolo).
"""
from __future__ import annotations

import math
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.autopan import (
    AUTOPAN_LAWS,
    AUTOPAN_SHAPES,
    AUTOPAN_SQUARE_EDGE_S,
)

SR = 44100


def _render(params=None, feeds=None, block=512, n=None, want_backend=False):
    """Render an autopan with ``feeds`` = {port: signal} on its jacks.

    Audio jacks are fed from a ``noise`` module, CV jacks from a
    ``constant`` and ``clock`` from a ``clock`` -- the buffers are
    overridden with the given signals, so the modules only lend a cable.
    """
    feeds = feeds or {}
    patch = Patch()
    m = patch.add_module("autopan", params=params or {})
    keys = {}
    for port in feeds:
        kind = ("noise" if port.startswith("in_")
                else "clock" if port == "clock" else "constant")
        src = patch.add_module(kind)
        patch.connect(src.id, "out", m.id, port)
        keys[port] = (src.id, "out")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    if n is None:
        n = min(np.asarray(v).shape[-1] for v in feeds.values()) if feeds else SR
    L, R = [], []
    t = 0
    while t < n:
        f = min(block, n - t)
        bufs = {keys[p]: np.asarray(v[..., t:t + f], dtype=np.float32)
                for p, v in feeds.items()}
        o = b._render_autopan(patch.get(m.id), f, bufs, patch)
        L.append(o["out_l"])
        R.append(o["out_r"])
        t += f
    L, R = np.concatenate(L), np.concatenate(R)
    return (L, R, b, patch.get(m.id)) if want_backend else (L, R)


def _ones(n):
    return np.ones(n, dtype=np.float32)


def _sine(n, hz=440.0, amp=0.5):
    return (amp * np.sin(2 * np.pi * hz * np.arange(n) / SR)).astype(np.float32)


def _noise(n, seed=1, amp=0.5):
    return np.random.default_rng(seed).uniform(-amp, amp, n).astype(np.float32)


def _gate(n, period, width=None):
    """A clock: high for the first ``width`` samples of every ``period``,
    rising at sample 0."""
    width = period // 2 if width is None else width
    return ((np.arange(n) % period) < width).astype(np.float32)


def _position(L, R):
    """The power-law pan position a DC-in (L, R) pair was placed at."""
    th = np.arctan2(R.astype(np.float64), L.astype(np.float64))
    return th / (np.pi / 4.0) - 1.0


def _db(x):
    return 20.0 * math.log10(float(x))


# ----- registration ------------------------------------------------------------


def test_registered_with_ports_params_and_category():
    cls = all_module_types()["autopan"]
    assert cls is get_module_type("autopan")
    assert cls.CATEGORY == "Routing & VCA"
    m = cls(1)
    assert [(p.name, p.signal_kind) for p in m.input_ports] == [
        ("in_l", "audio"), ("in_r", "audio"), ("pan_cv", "cv"),
        ("rate_cv", "cv"), ("clock", "gate")]
    assert [(p.name, p.signal_kind) for p in m.output_ports] == [
        ("out_l", "audio"), ("out_r", "audio")]
    assert m.params == {"pan": 0.0, "depth": 0.7, "rate": 0.5, "shape": "sine",
                        "tremolo": 0.0, "law": "power", "division": 4.0,
                        "cv_depth": 1.0}
    assert AUTOPAN_SHAPES == ("sine", "triangle", "square")
    assert AUTOPAN_LAWS == ("power", "compromise", "linear")


def test_serialization_round_trip():
    cls = all_module_types()["autopan"]
    m = cls(3, params={"pan": -0.4, "shape": "square", "law": "linear", "tremolo": 0.5})
    assert cls.from_dict(m.to_dict()).params == m.params


# ----- the law -------------------------------------------------------------------


@pytest.mark.parametrize("pan", [-1.0, -0.73, -0.3, 0.0, 0.2, 0.55, 0.9, 1.0])
def test_power_law_is_constant_power_at_every_pan(pan):
    x = _sine(4096)
    L, R = _render({"pan": pan, "depth": 0.0}, {"in_l": x})
    e = L.astype(np.float64) ** 2 + R.astype(np.float64) ** 2
    np.testing.assert_allclose(e, x.astype(np.float64) ** 2, atol=2e-7)


@pytest.mark.parametrize("law,db", [("power", -3.0103), ("compromise", -4.5154),
                                    ("linear", -6.0206)])
def test_the_three_centre_levels(law, db):
    L, R = _render({"depth": 0.0, "law": law}, {"in_l": _ones(256)})
    assert abs(_db(L[0]) - db) < 1e-3 and abs(_db(R[0]) - db) < 1e-3
    assert np.all(L == L[0]) and np.array_equal(L, R)


@pytest.mark.parametrize("law", AUTOPAN_LAWS)
def test_hard_pan_is_silence_on_the_other_side(law):
    x = _sine(2048)
    L, R = _render({"pan": 1.0, "depth": 0.0, "law": law}, {"in_l": x})
    assert np.abs(L).max() < 1e-12
    np.testing.assert_allclose(R, x, atol=1e-7)
    L, R = _render({"pan": -1.0, "depth": 0.0, "law": law}, {"in_l": x})
    assert np.abs(R).max() < 1e-12
    np.testing.assert_allclose(L, x, atol=1e-7)


def test_linear_law_sums_to_the_source():
    x = _sine(2048)
    L, R = _render({"pan": 0.37, "depth": 0.0, "law": "linear"}, {"in_l": x})
    np.testing.assert_allclose(L.astype(np.float64) + R, x, atol=1e-7)


def test_a_mono_source_in_the_right_jack_is_placed_the_same():
    x = _noise(4096)
    a = _render({"pan": 0.4, "depth": 0.5}, {"in_l": x})
    b = _render({"pan": 0.4, "depth": 0.5}, {"in_r": x})
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def test_the_power_law_is_the_stereo_sinks_placement():
    # (cos, sin) of (p + 1) * pi/4 -- the stereo_speaker_output formula.
    p = 0.3
    L, R = _render({"pan": p, "depth": 0.0}, {"in_l": _ones(64)})
    th = (p + 1.0) * (np.pi / 4.0)
    assert L[0] == np.float32(np.cos(th)) and R[0] == np.float32(np.sin(th))


# ----- balance (both jacks cabled) -----------------------------------------------


def test_a_stereo_pair_at_centre_passes_bit_exactly():
    xl, xr = _noise(4096, 1), _noise(4096, 2)
    L, R = _render({"depth": 0.0}, {"in_l": xl, "in_r": xr})
    assert np.array_equal(L, xl) and np.array_equal(R, xr)


def test_balance_fades_only_the_far_side():
    xl, xr = _noise(2048, 1), _noise(2048, 2)
    L, R = _render({"pan": 0.5, "depth": 0.0}, {"in_l": xl, "in_r": xr})
    assert np.array_equal(R, xr)                                  # near side untouched
    np.testing.assert_allclose(L, xl * np.cos(0.25 * np.pi), atol=1e-7)
    L, R = _render({"pan": -1.0, "depth": 0.0}, {"in_l": xl, "in_r": xr})
    assert np.array_equal(L, xl) and np.abs(R).max() < 1e-12


# ----- the LFO -------------------------------------------------------------------


def test_depth_zero_is_a_static_pan():
    L, R = _render({"pan": -0.25, "depth": 0.0, "rate": 7.0}, {"in_l": _ones(SR)})
    assert np.all(L == L[0]) and np.all(R == R[0])


def test_the_sine_lfo_places_the_source_at_depth_times_sine():
    n = SR * 2
    L, R = _render({"depth": 0.8, "rate": 1.5}, {"in_l": _ones(n)})
    want = 0.8 * np.sin(2 * np.pi * 1.5 * np.arange(n) / SR)
    np.testing.assert_allclose(_position(L, R), want, atol=2e-6)


def test_the_triangle_has_its_corners_on_the_quarter_cycles():
    n = SR
    L, R = _render({"depth": 1.0, "rate": 1.0, "shape": "triangle"}, {"in_l": _ones(n)})
    pos = _position(L, R)
    assert abs(pos[SR // 4] - 1.0) < 1e-5 and abs(pos[3 * SR // 4] + 1.0) < 1e-5
    # constant speed between them: 4 pan units per cycle
    seg = np.diff(pos[SR // 4 + 10: 3 * SR // 4 - 10])
    np.testing.assert_allclose(seg, -4.0 / SR, atol=1e-6)


def test_depth_clamps_against_the_wall():
    L, R = _render({"pan": 0.8, "depth": 1.0, "rate": 1.0, "law": "linear"},
                   {"in_l": _ones(SR)})
    assert L.min() == 0.0 and np.all(R <= 1.0)


def test_rate_cv_plus_one_is_the_knob_doubled_bit_exact():
    x = _noise(SR * 2)
    a = _render({"rate": 0.75}, {"in_l": x, "rate_cv": np.ones(SR * 2)})
    b = _render({"rate": 1.5}, {"in_l": x})
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def test_a_moving_rate_keeps_the_phase_continuous():
    n = SR * 3
    ramp = np.linspace(-1.0, 2.0, n).astype(np.float32)
    L, R = _render({"depth": 1.0, "rate": 1.0}, {"in_l": _ones(n), "rate_cv": ramp})
    pos = _position(L, R)
    # the fastest the rate gets is 4 Hz: no step may exceed that sine's slope
    assert np.abs(np.diff(pos)).max() < 2 * np.pi * 4.0 / SR * 1.01


# ----- exactness -----------------------------------------------------------------


@pytest.mark.parametrize("shape", AUTOPAN_SHAPES)
def test_block_size_exact_at_64_128_512_1000(shape):
    n = int(SR * 4.5)
    feeds = {
        "in_l": _noise(n, 3),
        "pan_cv": (0.3 * np.sin(2 * np.pi * 0.7 * np.arange(n) / SR)).astype(np.float32),
        "rate_cv": np.full(n, 0.37, dtype=np.float32),
    }
    params = {"shape": shape, "tremolo": 0.3, "rate": 1.3, "depth": 0.9}
    ref = _render(params, feeds, block=512)
    for block in (64, 128, 1000):
        got = _render(params, feeds, block=block)
        assert np.array_equal(got[0], ref[0]) and np.array_equal(got[1], ref[1]), block


@pytest.mark.parametrize("block", [64, 1000])
def test_the_phase_is_keyed_to_the_sample_count_not_accumulated(block):
    """At a steady rate the free-running anchor never moves: the phase at
    sample N is (N * rate / sr) mod 1, computed fresh. This pins the float64
    phase itself -- the float32 output of the TRIANGLE hides an
    accumulator's ulp drift (measured: a monkeypatched per-block
    accumulator passes the triangle's output pin), so the output pins
    alone are not enough."""
    n = int(SR * 4.5)
    L, R, b, m = _render({"depth": 1.0, "rate": 0.5, "shape": "triangle"},
                         {"in_l": _ones(n), "rate_cv": np.full(n, 0.37, np.float32)},
                         block=block, want_backend=True)
    st = b._state[m.id]
    assert st["fr_n"] == 0 and st["fr_ph"] == 0.0 and st["samples"] == n


# ----- clock sync ---------------------------------------------------------------


def _rising_zero_crossings(pos):
    i = np.flatnonzero((pos[:-1] < 0.0) & (pos[1:] >= 0.0))
    return i + (-pos[i]) / (pos[i + 1] - pos[i])


@pytest.mark.parametrize("division", [0.5, 2.0, 4.0])
def test_a_clocked_cycle_is_division_clock_intervals_long(division):
    period = 11025                                   # 240 bpm quarters
    n = SR * 6
    L, R = _render({"depth": 1.0, "rate": 0.37, "division": division},
                   {"in_l": _ones(n), "clock": _gate(n, period)})
    zc = _rising_zero_crossings(_position(L, R))
    zc = zc[zc > 2 * period]                          # after the lock
    assert len(zc) >= 2
    np.testing.assert_allclose(np.diff(zc), division * period, atol=0.05)


def test_the_clocked_render_is_block_size_exact():
    n = int(SR * 4.5)
    feeds = {"in_l": _noise(n, 4), "clock": _gate(n, 9187)}
    params = {"shape": "square", "division": 2.0, "tremolo": 0.2}
    ref = _render(params, feeds, block=512)
    for block in (64, 128, 1000):
        got = _render(params, feeds, block=block)
        assert np.array_equal(got[0], ref[0]) and np.array_equal(got[1], ref[1]), block


def test_unpatching_the_clock_carries_on_from_the_locked_phase():
    period = 8000
    n = 512 * 170
    patch = Patch()
    m = patch.add_module("autopan", params={"depth": 1.0, "rate": 3.0, "division": 1.0})
    src = patch.add_module("noise")
    clk = patch.add_module("clock")
    patch.connect(src.id, "out", m.id, "in_l")
    patch.connect(clk.id, "out", m.id, "clock")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    g = _gate(n, period)
    pos = []
    for t in range(0, n, 512):
        o = b._render_autopan(m, 512, {(src.id, "out"): _ones(512),
                                       (clk.id, "out"): g[t:t + 512]}, patch)
        pos.append(_position(o["out_l"], o["out_r"]))
    assert patch.disconnect(clk.id, "out", m.id, "clock")
    for _ in range(20):
        o = b._render_autopan(m, 512, {(src.id, "out"): _ones(512)}, patch)
        pos.append(_position(o["out_l"], o["out_r"]))
    pos = np.concatenate(pos)
    # no jump anywhere -- not at the lock (the locked cycle is SR/8000 =
    # 5.51 Hz, faster than the knob) and not at the unpatch, where the
    # free-running line takes over in place at the knob's 3 Hz
    assert np.abs(np.diff(pos)).max() < 2 * np.pi * (SR / period) / SR * 1.01
    k = 170 * 512
    assert abs(pos[k] - pos[k - 1]) < 2 * np.pi * (SR / period) / SR * 1.01
    tail = np.diff(pos[k + 1000:])
    assert np.abs(tail).max() < 2 * np.pi * 3.0 / SR * 1.01


# ----- the square does not click ---------------------------------------------------


@pytest.mark.parametrize("rate", [0.5, 2.0, 8.0])
def test_the_square_glides_calibrated_against_a_sine_at_the_same_rate(rate):
    """The square's steepest step is K = 1/(2*rate*T) times the sine's at
    the same rate and depth -- i.e. exactly as steep as a 50 Hz sine
    sweep, never a switch -- and far below the hard switch's ~0.7."""
    n = SR * 2
    sine = _render({"depth": 1.0, "rate": rate}, {"in_l": _ones(n)})
    sq = _render({"depth": 1.0, "rate": rate, "shape": "square"}, {"in_l": _ones(n)})
    k = 1.0 / (2.0 * rate * AUTOPAN_SQUARE_EDGE_S)
    s_step = np.abs(np.diff(sine[0])).max()
    q_step = np.abs(np.diff(sq[0])).max()
    assert q_step <= k * s_step * 1.02
    assert q_step >= k * s_step * 0.9                 # it IS that steep: a real square
    assert q_step < 0.7 / 50                          # a hard switch steps the full gain
    # and it reaches the sides: the plateaus are hard left / hard right
    pos = _position(*sq)
    assert pos.max() > 0.999 and pos.min() < -0.999


# ----- tremolo --------------------------------------------------------------------


def test_tremolo_one_is_two_equal_gains_a_mono_tremolo():
    n = SR
    for shape in AUTOPAN_SHAPES:
        L, R = _render({"depth": 1.0, "rate": 2.0, "tremolo": 1.0, "shape": shape},
                       {"in_l": _ones(n)})
        np.testing.assert_allclose(L, R, atol=1e-6)
        assert L.min() < 1e-3 and L.max() > 0.999        # silence .. unity


def test_tremolo_zero_is_bit_exact_autopan_and_half_is_between():
    x = _noise(SR)
    a = _render({"depth": 1.0, "rate": 2.0}, {"in_l": x})
    b = _render({"depth": 1.0, "rate": 2.0, "tremolo": 0.0}, {"in_l": x})
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
    L, R = _render({"depth": 1.0, "rate": 2.0, "tremolo": 0.5}, {"in_l": _ones(SR)})
    e = L.astype(np.float64) ** 2 + R.astype(np.float64) ** 2
    assert e.min() < 0.5 and e.max() > 1.2          # the swirl breathes...
    assert np.abs(L - R).max() > 0.5                 # ...and still moves side to side


# ----- robustness / neutral --------------------------------------------------------


def test_non_finite_cvs_do_not_poison_the_output():
    n = SR
    bad = np.full(n, np.nan, dtype=np.float32)
    bad[::7] = np.inf
    x = _noise(n)
    L, R = _render({"depth": 0.6}, {"in_l": x, "pan_cv": bad, "rate_cv": bad})
    assert np.all(np.isfinite(L)) and np.all(np.isfinite(R))
    ref = _render({"depth": 0.6}, {"in_l": x})
    assert np.array_equal(L, ref[0]) and np.array_equal(R, ref[1])


def test_pan_cv_is_per_sample_and_one_to_one():
    n = 4096
    cv = np.linspace(-1.0, 1.0, n).astype(np.float32)
    L, R = _render({"depth": 0.0}, {"in_l": _ones(n), "pan_cv": cv})
    np.testing.assert_allclose(_position(L, R), cv, atol=2e-6)


def test_a_voiced_input_is_the_summed_render():
    n = 4096
    v = np.stack([_noise(n, s) for s in (1, 2, 3)])
    a = _render({"depth": 0.8, "rate": 3.0}, {"in_l": v})
    # The house collapse (float64 sum, cast back) -- not a float32
    # v.sum(axis=0), which rounds per voice and depends on voice order.
    b = _render({"depth": 0.8, "rate": 3.0}, {"in_l": NumpyBackend._voice_sum(v)})
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def test_nothing_cabled_is_silence_and_the_lfo_keeps_time():
    patch = Patch()
    m = patch.add_module("autopan")
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)
    o = b._render_autopan(m, 256, {}, patch)
    assert not o["out_l"].any() and not o["out_r"].any()
    assert b._state[m.id]["samples"] == 256
    e = b._render_autopan(m, 0, {}, patch)
    assert e["out_l"].shape == (0,) and e["out_r"].shape == (0,)


def test_absurd_params_are_clamped():
    L, R = _render({"pan": 9.0, "depth": -3.0, "rate": 1e9, "shape": "zigzag",
                    "law": "nope", "tremolo": 7.0, "division": 0.0},
                   {"in_l": _ones(1024), "clock": _gate(1024, 100)})
    assert np.all(np.isfinite(L)) and np.abs(L).max() < 1e-12   # pan 9 -> hard right


# ----- UI ----------------------------------------------------------------------------


def test_every_param_has_a_widget_and_the_combos_offer_their_lists(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("autopan")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    widgets = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            widgets[str(call.kwargs.get("label"))] = (k, call.kwargs.get("items"))
    for name in get_module_type("autopan").DEFAULT_PARAMS:
        hits = [lb for lb in widgets if lb == name or lb.startswith(name + " ")]
        assert hits, (name, list(widgets))
        assert widgets[hits[0]][0] != "add_input_text", name
    assert widgets["shape"] == ("add_combo", list(AUTOPAN_SHAPES))
    law = next(lb for lb in widgets if lb.startswith("law"))
    assert widgets[law] == ("add_combo", list(AUTOPAN_LAWS))
    assert all(ord(ch) < 128 for lb in widgets for ch in lb)


# ----- the example -------------------------------------------------------------------


def test_the_bounce_example_throws_alternate_notes_to_alternate_sides():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "autopan_pluck_bounce.json"
    patch = load_patch(path)
    ap = next(m for m in patch if m.TYPE == "autopan")
    assert any(c.dst_module_id == ap.id and c.dst_port == "clock" for c in patch.cables)
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    outs = []
    orig = b._render_autopan

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        outs.append((r["out_l"], r["out_r"]))
        return r

    b._render_autopan = spy
    peak = 0.0
    for _ in range(int(SR * 6 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.05 < peak < 1.0
    L = np.concatenate([o[0] for o in outs]).astype(np.float64)
    R = np.concatenate([o[1] for o in outs]).astype(np.float64)
    # One note per eighth (96 bpm -> 13781.25 samples); after the lock the
    # energy of consecutive eighths alternates sides.
    eighth = SR * 60.0 / 96.0 / 2.0
    sides = []
    for k in range(2, 10):
        a, z = int(k * eighth), int((k + 1) * eighth)
        el, er = np.sum(L[a:z] ** 2), np.sum(R[a:z] ** 2)
        sides.append(np.sign(el - er))
        assert max(el, er) > 4.0 * min(el, er)       # each note sits on one side
    assert all(sides[i] == -sides[i + 1] for i in range(len(sides) - 1))
