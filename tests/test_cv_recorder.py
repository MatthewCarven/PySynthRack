"""CVRecorder — the modulation looper.

Pins the contract: nothing patched is silent and stateless-ish; the first
pass records and every later pass plays it back bit-exact; ``out`` while
recording is what is being written; replace punches in only where rec was
high, overdub sums with ``feedback`` scaling the old layer; playback
continues with rec low; ``clear`` wipes, rewinds and holds until the next
rec; ``pos`` is a 0..1 ramp of period L; clocked: the length is in ticks
of the measured period, rec edges land on the next tick, the loop
re-syncs at the boundary; the knob path ramps across the block;
block-size independence with a patched input; the ``mode`` combo offers
replace/overdub (the shared-branch trap); the widget sweep; the example.

Plumbing tests run at SR 1000 with 50-sample blocks; the example at 44100.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.cv_recorder import CV_RECORDER_MODES

SR = 1000
BLOCK = 50


def _driver(params=None, sr=SR, block=BLOCK, cable_in=True, clock=False, clear=False):
    patch = Patch()
    m = patch.add_module("cv_recorder", params=params or {})
    keys = {}
    if cable_in:
        src = patch.add_module("constant")
        patch.connect(src.id, "out", m.id, "in")
        keys["in"] = (src.id, "out")
    rg = patch.add_module("clock")
    patch.connect(rg.id, "out", m.id, "rec")
    keys["rec"] = (rg.id, "out")
    if clock:
        ck = patch.add_module("clock")
        patch.connect(ck.id, "out", m.id, "clock")
        keys["clock"] = (ck.id, "out")
    if clear:
        cl = patch.add_module("clock")
        patch.connect(cl.id, "out", m.id, "clear")
        keys["clear"] = (cl.id, "out")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(frames=block, **bufs):
        return b._render_cv_recorder(
            patch.get(m.id), frames,
            {keys[k]: np.asarray(v, dtype=np.float32) for k, v in bufs.items()}, patch)

    step.module = m
    step.patch = patch
    step.backend = b
    return step


def _render(total, params=None, block=BLOCK, sr=SR, x=None, rec=None, clock=None, clear=None):
    """Render ``total`` samples; ``x``/``rec``/``clock``/``clear`` are functions
    of the absolute sample index array (or None = unpatched)."""
    step = _driver(params, sr=sr, block=block, cable_in=x is not None,
                   clock=clock is not None, clear=clear is not None)
    outs, poss = [], []
    for i in range(total // block):
        t = np.arange(i * block, (i + 1) * block)
        bufs = {"rec": rec(t) if rec is not None else np.zeros(block)}
        if x is not None:
            bufs["in"] = x(t)
        if clock is not None:
            bufs["clock"] = clock(t)
        if clear is not None:
            bufs["clear"] = clear(t)
        r = step(block, **bufs)
        outs.append(r["out"])
        poss.append(r["pos"])
    return np.concatenate(outs), np.concatenate(poss), step


RAMP = lambda t: (t / 400.0).astype(np.float32)               # a slow rising ramp
REC_FIRST_LOOP = lambda t: (t < 100).astype(np.float32)        # record the first 100 samples


# ----- registration -----------------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["cv_recorder"]
    assert cls is get_module_type("cv_recorder")
    assert cls.CATEGORY == "CV & Utilities"
    m = cls(1)
    assert [(p.name, p.signal_kind) for p in m.input_ports] == [
        ("in", "cv"), ("clock", "gate"), ("rec", "gate"), ("clear", "gate")]
    assert [(p.name, p.signal_kind) for p in m.output_ports] == [("out", "cv"), ("pos", "cv")]
    assert m.params["mode"] == "overdub" and m.params["length"] == 4.0
    assert CV_RECORDER_MODES == ("replace", "overdub")


def test_serialization_round_trip():
    cls = all_module_types()["cv_recorder"]
    m = cls(2, params={"mode": "replace", "length": 2.0, "feedback": 0.5, "value": -0.3})
    assert cls.from_dict(m.to_dict()).params == m.params


# ----- the loop -----------------------------------------------------------------


def test_nothing_recorded_is_silent_and_the_position_holds():
    out, pos, step = _render(400, {"length": 0.1}, x=RAMP)      # rec never rises
    assert np.all(out == 0.0) and np.all(pos == 0.0)
    assert not step.backend._state[step.module.id]["exists"]


def test_first_pass_records_and_every_later_pass_plays_it_back():
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP)
    first = out[:100]
    assert np.array_equal(first, RAMP(np.arange(100)))            # out while recording == what is written
    for k in range(1, 4):
        assert np.array_equal(out[k * 100:(k + 1) * 100], first)  # loops forever
    assert np.array_equal(pos[:100], np.arange(100, dtype=np.float32) / 100)
    assert np.array_equal(pos[100:200], pos[:100])


def test_the_loop_starts_at_the_rec_edge_not_at_time_zero():
    rec = lambda t: ((t >= 30) & (t < 130)).astype(np.float32)
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=rec)
    assert np.all(out[:30] == 0.0) and np.all(pos[:30] == 0.0)
    assert out[30] == RAMP(np.array([30]))[0]
    assert pos[30] == 0.0 and pos[129] == pytest.approx(0.99)
    assert np.array_equal(out[130:230], out[30:130])


def test_replace_punches_in_only_where_rec_was_high():
    rec = lambda t: ((t < 100) | ((t >= 220) & (t < 240))).astype(np.float32)
    out, _, _ = _render(500, {"length": 0.1, "mode": "replace"}, x=RAMP, rec=rec)
    first = out[:100]
    third = out[300:400]
    assert np.array_equal(third[:20], first[:20]) and np.array_equal(third[40:], first[40:])
    assert np.array_equal(third[20:40], RAMP(np.arange(220, 240)))   # the punched stretch


def test_overdub_sums_and_feedback_scales_the_old_layer():
    rec = lambda t: ((t < 100) | ((t >= 200) & (t < 300))).astype(np.float32)
    x = lambda t: np.where(t < 200, 0.5, 0.25).astype(np.float32)
    out1, _, _ = _render(500, {"length": 0.1, "mode": "overdub", "feedback": 1.0}, x=x, rec=rec)
    assert np.allclose(out1[100:200], 0.5)                          # pass 2: the first layer
    assert np.allclose(out1[200:300], 0.75)                         # pass 3 (recording): old + new
    assert np.allclose(out1[400:500], 0.75)                         # and it stays
    out2, _, _ = _render(500, {"length": 0.1, "mode": "overdub", "feedback": 0.5}, x=x, rec=rec)
    assert np.allclose(out2[400:500], 0.5 * 0.5 + 0.25)             # the old layer halved first


def test_playback_continues_with_rec_low_and_ignores_the_input():
    rec = REC_FIRST_LOOP
    x = lambda t: np.where(t < 100, RAMP(t), 9.0).astype(np.float32)
    out, _, _ = _render(400, {"length": 0.1}, x=x, rec=rec)
    assert out.max() < 1.0                                          # the 9.0 never got in


def test_clear_wipes_rewinds_and_holds_until_the_next_rec():
    rec = lambda t: ((t < 100) | ((t >= 310) & (t < 410))).astype(np.float32)
    clear = lambda t: ((t >= 250) & (t < 252)).astype(np.float32)
    out, pos, _ = _render(600, {"length": 0.1}, x=RAMP, rec=rec, clear=clear)
    assert np.any(out[100:250] != 0.0)
    assert np.all(out[250:310] == 0.0) and np.all(pos[250:310] == 0.0)   # wiped, holding
    assert pos[310] == 0.0                                          # the next take starts at the top
    assert np.array_equal(out[310:410], RAMP(np.arange(310, 410)))
    assert np.array_equal(out[410:510], out[310:410])


def test_pos_is_a_ramp_with_the_loop_period():
    _, pos, _ = _render(1000, {"length": 0.25}, x=RAMP, rec=lambda t: (t < 10).astype(np.float32))
    wraps = np.flatnonzero(np.diff(pos) < 0) + 1
    assert np.array_equal(wraps, np.arange(250, 1000, 250))
    assert pos.max() == pytest.approx(0.996) and pos.min() == 0.0


# ----- the knob -------------------------------------------------------------------


def test_unpatched_in_records_the_value_knob_ramped_across_the_block():
    step = _driver({"length": 0.1, "value": 0.0}, cable_in=False)
    rec_on = np.ones(BLOCK, dtype=np.float32)
    step(rec=rec_on)                                 # block 1: knob at 0
    step.module.params["value"] = 1.0                # the hand moves it
    r = step(rec=rec_on)                             # block 2: ramps 0 -> 1 across the block
    assert np.allclose(r["out"], np.arange(1, BLOCK + 1) / BLOCK, atol=1e-6)
    rec_off = np.zeros(BLOCK, dtype=np.float32)
    played = np.concatenate([step(rec=rec_off)["out"] for _ in range(2)])   # the loop plays the gesture
    assert np.allclose(played[:BLOCK], 0.0) and np.allclose(played[BLOCK:], np.arange(1, BLOCK + 1) / BLOCK, atol=1e-6)


# ----- clocked --------------------------------------------------------------------


def test_clocked_length_is_in_ticks_of_the_measured_period():
    period = 40
    clock = lambda t: ((t % period) < 5).astype(np.float32)
    rec = lambda t: ((t >= 85) & (t < 250)).astype(np.float32)   # pressed between ticks
    out, pos, step = _render(1200, {"length": 4.0}, x=RAMP, rec=rec, clock=clock)
    st = step.backend._state[step.module.id]
    assert st["L"] == 4 * period                                   # four ticks, not four seconds
    # Quantised punch-in: the loop starts on the tick AFTER the edge (t=120), not at 85.
    assert np.all(out[:120] == 0.0) and pos[120] == 0.0
    assert out[120] == RAMP(np.array([120]))[0]
    # And the loop plays back a full period later, bit-exact.
    assert np.array_equal(out[280:440], out[120:280])


def test_clocked_rec_off_lands_on_the_next_tick_too():
    period = 40
    clock = lambda t: ((t % period) < 5).astype(np.float32)
    rec = lambda t: ((t >= 45) & (t < 130)).astype(np.float32)    # on -> tick 80; off at 130 -> tick 160
    x = lambda t: np.where(t < 160, 0.5, 0.0).astype(np.float32)
    out, _, _ = _render(800, {"length": 4.0, "mode": "replace"}, x=x, rec=rec, clock=clock)
    # The loop is 80..240. Recording ran until t=160 (the tick after the
    # gate fell at 130), so positions 0..80 hold 0.5 -- including the
    # 130..160 stretch after the gate fell -- and 80..160 were never
    # written. Read it off the second pass.
    assert np.allclose(out[240:320], 0.5)
    assert np.all(out[320:400] == 0.0)


def test_a_clocked_rec_edge_waits_for_the_clocks_period():
    # Rec high from t=0 with the clock's first tick at t=0: the period is
    # unknown until the second tick, so the loop is created there.
    period = 40
    clock = lambda t: ((t % period) < 5).astype(np.float32)
    out, pos, step = _render(400, {"length": 2.0}, x=RAMP, rec=lambda t: (t < 200).astype(np.float32), clock=clock)
    assert np.all(out[:40] == 0.0) and pos[40] == 0.0
    assert step.backend._state[step.module.id]["L"] == 80
    assert out[40] == RAMP(np.array([40]))[0]


def test_clocked_loop_resyncs_at_the_boundary_tick():
    # A clock that speeds up after the loop exists: the loop hard-syncs to
    # 0 on every length-th tick, so its start stays on the transport.
    def clock(t):
        return np.where(t < 400, (t % 40) < 5, ((t - 400) % 30) < 5).astype(np.float32)
    rec = lambda t: (t < 170).astype(np.float32)
    out, pos, step = _render(1000, {"length": 4.0}, x=RAMP, rec=rec, clock=clock)
    assert step.backend._state[step.module.id]["L"] == 160
    # The loop was created at tick 40 (the period is known then); every
    # fourth tick after it is a boundary: 200, 360, then -- with ticks
    # every 30 from t=400 -- 490 and 610. The position restarts there
    # even though the buffer is 160 long.
    zeros = set((np.flatnonzero(pos == 0.0)).tolist())
    for t in (40, 200, 360, 490, 610):
        assert t in zeros, t
    assert 520 not in zeros and 640 not in zeros            # not the free-running wrap


# ----- block sizes --------------------------------------------------------------------


def test_block_size_independent_with_a_patched_input():
    rec = lambda t: (((t >= 37) & (t < 137)) | ((t >= 300) & (t < 350))).astype(np.float32)
    clear = lambda t: ((t >= 600) & (t < 603)).astype(np.float32)
    a_out, a_pos, _ = _render(1000, {"length": 0.1, "feedback": 0.7}, block=50, x=RAMP, rec=rec, clear=clear)
    b_out, b_pos, _ = _render(1000, {"length": 0.1, "feedback": 0.7}, block=250, x=RAMP, rec=rec, clear=clear)
    assert np.array_equal(a_out, b_out) and np.array_equal(a_pos, b_pos)


# ----- UI -------------------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("cv_recorder")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("items"))
    return out


def test_mode_combo_offers_replace_and_overdub_and_every_param_has_a_widget(monkeypatch):
    w = _widgets(monkeypatch)
    assert w["mode"] == ("add_combo", list(CV_RECORDER_MODES))
    for name in get_module_type("cv_recorder").DEFAULT_PARAMS:
        hits = [lb for lb in w if lb == name or lb.startswith(name + " ")]
        assert hits, (name, list(w))
        assert w[hits[0]][0] != "add_input_text", name


# ----- example ------------------------------------------------------------------------


def test_the_example_loops_a_layering_modulation():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "cv_recorder_layers.json"
    patch = load_patch(path)
    recs = [m for m in patch if m.TYPE == "cv_recorder"]
    assert len(recs) == 1
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    cap = {"out": [], "pos": []}
    orig = b._render_cv_recorder

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap["out"].append(np.asarray(r["out"]).copy())
        cap["pos"].append(np.asarray(r["pos"]).copy())
        return r

    b._render_cv_recorder = spy
    peak = 0.0
    for _ in range(int(44100 * 9 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.1 < peak < 1.0
    loop = np.concatenate(cap["out"])
    pos = np.concatenate(cap["pos"])
    assert pos.max() > 0.9                                          # the loop exists and runs
    L = b._state[recs[0].id]["L"]
    assert L > 0
    # Bar 3 is bar 2 with another layer on top: the loop keeps evolving.
    sr = 44100
    bar2 = loop[2 * L:3 * L]
    bar3 = loop[3 * L:4 * L]
    assert bar2.std() > 0.05 and not np.array_equal(bar2, bar3)
