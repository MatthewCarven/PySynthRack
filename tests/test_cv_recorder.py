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

The transport (the 2026-09-20 love pass): ``play`` low holds ``out`` and
``pos`` exactly and resumes from the same slot, and a stopped head writes
nothing (the take starts when play rises); ``reverse`` plays the slots in
descending order, a mid-loop flip turns around without a jump, a take
recorded backwards comes back time-reversed, and a clocked loop still
syncs; ``speed`` 2x plays the loop twice per length, 0.5x once per two
(interpolated), recording at 2x runs at 1x, a speed change does not jump;
every feature 50 vs 250 exact; the defaults are the shipped arithmetic
(an old-engine pin); the ``speed`` combo's CONTENTS; the backwards example.

The one-shot and the rate jack (the 2026-09-22 love pass): ``speed_cv``
quantises the head rate onto the half-sample grid — the eight reachable
rates and the ladder's boundaries are pinned, cv 0 is the combo
bit-exact, a non-finite CV reads as 0, a sweeping CV never jumps the
head, recording still runs at 1x, and every reachable rate is 50 vs 250
exact; ``play_mode`` ``one_shot`` fires exactly one lap from a rising
edge (a ramp appears once then holds at its TOP), ignores the level,
retriggers from 0 mid-lap, runs the lap backwards under reverse, lets
``rec`` move the head, still obeys a clocked sync tick, never fires with
``play`` unpatched, and ``gate`` is the shipped behaviour bit-exact.

Plumbing tests run at SR 1000 with 50-sample blocks; the example at 44100.
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
from pysynthrack.modules.cv_recorder import (
    CV_RECORDER_MODES,
    CV_RECORDER_PLAY_MODES,
    CV_RECORDER_SPEEDS,
)

SR = 1000
BLOCK = 50


def _driver(params=None, sr=SR, block=BLOCK, cable_in=True, clock=False, clear=False,
            play=False, reverse=False, speed_cv=False):
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
    for name, wanted in (("clock", clock), ("clear", clear), ("play", play), ("reverse", reverse)):
        if wanted:
            g = patch.add_module("clock")
            patch.connect(g.id, "out", m.id, name)
            keys[name] = (g.id, "out")
    if speed_cv:
        g = patch.add_module("constant")
        patch.connect(g.id, "out", m.id, "speed_cv")
        keys["speed_cv"] = (g.id, "out")
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


def _render(total, params=None, block=BLOCK, sr=SR, x=None, rec=None, clock=None, clear=None,
            play=None, reverse=None, speed_cv=None, knobs=None):
    """Render ``total`` samples; ``x``/``rec``/``clock``/``clear``/``play``/
    ``reverse`` are functions of the absolute sample index array (or None =
    unpatched). ``knobs`` is an optional {absolute_sample: {param: value}}
    schedule applied before the block that starts at that sample (a hand
    on the panel; the sample must be a block boundary at every size used)."""
    step = _driver(params, sr=sr, block=block, cable_in=x is not None,
                   clock=clock is not None, clear=clear is not None,
                   play=play is not None, reverse=reverse is not None,
                   speed_cv=speed_cv is not None)
    outs, poss = [], []
    for i in range(total // block):
        t = np.arange(i * block, (i + 1) * block)
        for at, changes in (knobs or {}).items():
            if t[0] <= at < t[0] + block:
                assert at == t[0], "knob schedule must sit on a block boundary"
                step.module.params.update(changes)
        bufs = {"rec": rec(t) if rec is not None else np.zeros(block)}
        for name, fn in (("in", x), ("clock", clock), ("clear", clear), ("play", play),
                         ("reverse", reverse), ("speed_cv", speed_cv)):
            if fn is not None:
                bufs[name] = fn(t)
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
        ("in", "cv"), ("clock", "gate"), ("rec", "gate"), ("clear", "gate"),
        ("play", "gate"), ("reverse", "gate"), ("speed_cv", "cv")]
    assert [(p.name, p.signal_kind) for p in m.output_ports] == [("out", "cv"), ("pos", "cv")]
    assert m.params["mode"] == "overdub" and m.params["length"] == 4.0
    assert m.params["reverse"] is False and m.params["speed"] == "1x"
    assert m.params["speed_cv_depth"] == 1.0 and m.params["play_mode"] == "gate"
    assert CV_RECORDER_MODES == ("replace", "overdub")
    assert CV_RECORDER_SPEEDS == ("0.5x", "1x", "2x")
    assert CV_RECORDER_PLAY_MODES == ("gate", "one_shot")


def test_serialization_round_trip():
    cls = all_module_types()["cv_recorder"]
    m = cls(2, params={"mode": "replace", "length": 2.0, "feedback": 0.5, "value": -0.3,
                       "reverse": True, "speed": "2x", "speed_cv_depth": -2.0,
                       "play_mode": "one_shot"})
    assert cls.from_dict(m.to_dict()).params == m.params


def test_an_old_patch_without_the_transport_keys_gets_the_defaults():
    cls = all_module_types()["cv_recorder"]
    m = cls.from_dict({"id": 3, "type": "cv_recorder", "name": "old",
                       "params": {"length": 2.0, "mode": "replace", "feedback": 1.0, "value": 0.0}})
    assert m.params["reverse"] is False and m.params["speed"] == "1x"
    assert m.params["speed_cv_depth"] == 1.0 and m.params["play_mode"] == "gate"


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


# ----- transport: play ------------------------------------------------------------


def test_play_low_holds_out_and_pos_and_resumes_from_the_same_slot():
    # A 100-sample ramp loop; play drops at t=150 (the head over slot 50)
    # and rises at 200.
    play = lambda t: ((t < 150) | (t >= 200)).astype(np.float32)
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP, play=play)
    first = out[:100]
    assert np.array_equal(out[100:150], first[:50])                 # running
    assert np.all(out[150:200] == first[50]) and np.all(pos[150:200] == 0.5)   # held, exactly
    assert np.array_equal(out[200:250], first[50:])                 # resumes from slot 50
    assert np.array_equal(pos[200:250], pos[50:100])


def test_a_stopped_head_writes_nothing_and_the_take_starts_when_play_rises():
    # Loop 0..100 (ramp). Rec high again 120..180 in replace with the
    # input at 9.0; play is low 120..160, so only the 160..180 stretch
    # (slots 20..40 -- the head stopped over slot 20) gets written.
    rec = lambda t: ((t < 100) | ((t >= 120) & (t < 180))).astype(np.float32)
    play = lambda t: ((t < 120) | (t >= 160)).astype(np.float32)
    x = lambda t: np.where(t < 100, RAMP(t), 9.0).astype(np.float32)
    out, pos, step = _render(500, {"length": 0.1, "mode": "replace"}, x=x, rec=rec, play=play)
    first = out[:100]
    assert np.all(out[120:160] == first[20]) and np.all(pos[120:160] == 0.2)   # stopped, held
    buf = step.backend._state[step.module.id]["buf"]
    assert np.all(buf[20:40] == 9.0)                                 # the take, from slot 20
    assert np.array_equal(buf[:20], first[:20]) and np.array_equal(buf[40:], first[40:])
    assert np.all(out[160:180] == 9.0)                               # what you hear is what you keep


def test_rec_while_stopped_creates_the_loop_and_arms_it():
    # Play is low from the start; rec rises at 30 (the loop is created,
    # position 0, nothing written); play rises at 60 and the take runs
    # 60..130 into slots 0..70. Read it off the first full replay.
    rec = lambda t: ((t >= 30) & (t < 130)).astype(np.float32)
    play = lambda t: (t >= 60).astype(np.float32)
    out, pos, step = _render(400, {"length": 0.1, "mode": "replace"}, x=RAMP, rec=rec, play=play)
    st = step.backend._state[step.module.id]
    assert st["exists"] and st["L"] == 100
    assert np.all(out[30:60] == 0.0) and np.all(pos[30:60] == 0.0)  # armed, stopped at the top
    assert np.array_equal(out[60:130], RAMP(np.arange(60, 130)))    # the take
    assert np.array_equal(out[160:230], RAMP(np.arange(60, 130)))   # replayed from slot 0 at t=160
    assert np.all(out[230:260] == 0.0)                              # slots 70..100 never written


def test_a_clocked_sync_tick_snaps_a_stopped_head_to_the_top():
    # The transport wins: with play gated low across a bar boundary the
    # head still snaps to 0 on the sync tick, so a play gate off the same
    # clock resumes at the top of the bar.
    period = 40
    clock = lambda t: ((t % period) < 5).astype(np.float32)
    rec = lambda t: ((t >= 45) & (t < 130)).astype(np.float32)    # loop 80..240 (L=160)
    play = lambda t: (~((t >= 380) & (t < 400))).astype(np.float32)
    out, pos, step = _render(600, {"length": 4.0}, x=RAMP, rec=rec, clock=clock, play=play)
    assert step.backend._state[step.module.id]["L"] == 160
    # Stopped over slot 140 (380 - 240) ... until the sync tick at 400.
    assert np.all(pos[380:400] == pytest.approx(140 / 160))
    assert pos[400] == 0.0 and pos[401] == pytest.approx(1 / 160)


# ----- transport: reverse ---------------------------------------------------------


def test_reverse_gate_plays_the_slots_in_descending_order():
    rev = lambda t: (t >= 200).astype(np.float32)                   # flips at a loop boundary
    out, pos, _ = _render(500, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP, reverse=rev)
    first = out[:100]
    assert out[200] == first[0]                                     # the head was over slot 0
    assert np.array_equal(out[201:300], first[99:0:-1])             # then 99, 98, ... 1: a falling ramp
    assert np.array_equal(out[300:400], out[200:300])               # and again
    assert pos[200] == 0.0 and pos[201] == pytest.approx(0.99) and pos[299] == pytest.approx(0.01)


def test_reverse_checkbox_does_the_same_as_the_gate():
    knobs = {200: {"reverse": True}}
    out, _, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP, knobs=knobs)
    first = out[:100]
    assert out[200] == first[0] and np.array_equal(out[201:300], first[99:0:-1])


def test_a_mid_loop_flip_turns_around_without_a_jump():
    # Reverse from 250 (slot 50) to 280, then forwards again.
    rev = lambda t: ((t >= 250) & (t < 280)).astype(np.float32)
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP, reverse=rev)
    first = out[:100]
    assert out[249] == first[49] and out[250] == first[50] and out[251] == first[49]
    assert np.array_equal(out[250:280], first[50:20:-1])            # 50 down to 21
    assert out[280] == first[20] and np.array_equal(out[280:360], first[20:100])   # from 20 forwards
    assert np.abs(np.diff(out[240:300])).max() < 1.01 / 400          # never more than one ramp step


def test_a_take_recorded_in_reverse_plays_as_performed_then_comes_back_time_reversed():
    # Rec and reverse both high 0..100: slot 0 gets x(0), slot 99 x(1),
    # ... slot 1 x(99). While reverse stays on the loop plays as
    # performed; once it is released (t=200) the ramp comes back falling.
    rev = lambda t: (t < 200).astype(np.float32)
    out, _, step = _render(400, {"length": 0.1, "mode": "replace"}, x=RAMP, rec=REC_FIRST_LOOP, reverse=rev)
    buf = step.backend._state[step.module.id]["buf"]
    assert buf[0] == RAMP(np.array([0]))[0] and np.array_equal(buf[99:0:-1], RAMP(np.arange(1, 100)))
    assert np.array_equal(out[:100], RAMP(np.arange(100)))          # what you hear is what you keep
    assert np.array_equal(out[100:200], out[:100])                  # as performed, still in reverse
    assert out[200] == buf[0] and np.array_equal(out[201:300], RAMP(np.arange(99, 0, -1)))   # time-reversed


def test_reverse_while_clocked_still_syncs_at_the_boundary():
    period = 40
    clock = lambda t: ((t % period) < 5).astype(np.float32)
    rec = lambda t: (t < 170).astype(np.float32)                    # created at tick 40, L=160
    rev = lambda t: (t >= 170).astype(np.float32)
    out, pos, step = _render(800, {"length": 4.0}, x=RAMP, rec=rec, clock=clock, reverse=rev)
    assert step.backend._state[step.module.id]["L"] == 160
    # Running backwards from slot 130 at t=170 ... the sync tick at 200
    # snaps the head to 0, and it keeps going backwards from there.
    assert pos[170] == pytest.approx(130 / 160) and pos[199] == pytest.approx(101 / 160)
    assert pos[200] == 0.0 and pos[201] == pytest.approx(159 / 160)
    assert pos[360] == 0.0                                          # and the next boundary


# ----- transport: speed -----------------------------------------------------------


def test_speed_2x_plays_the_loop_twice_per_length():
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP, knobs={100: {"speed": "2x"}})
    first = out[:100]
    assert np.array_equal(out[100:150], first[0::2])                # every other slot
    assert np.array_equal(out[150:200], first[0::2])                # twice per length
    assert np.array_equal(pos[100:150], pos[0:100:2])


def test_speed_half_plays_the_loop_once_per_two_lengths_interpolated():
    out, pos, _ = _render(600, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP, knobs={100: {"speed": "0.5x"}})
    first = out[:100].astype(np.float64)
    assert np.array_equal(out[100:300:2], first)                    # the slots themselves
    between = 0.5 * (first + np.roll(first, -1))                    # and the mean of each pair
    assert np.allclose(out[101:300:2], between, atol=1e-7)
    assert np.array_equal(out[300:500], out[100:300])               # once per two lengths
    assert pos[101] == pytest.approx(0.005) and pos[299] == pytest.approx(0.995)


def test_recording_at_2x_runs_at_1x():
    # Speed 2x from the start: the 100-sample rec window fills all 100
    # slots one per sample -- the take is real time -- and playback
    # after the edge runs at 2x.
    out, _, step = _render(300, {"length": 0.1, "mode": "replace", "speed": "2x"}, x=RAMP, rec=REC_FIRST_LOOP)
    buf = step.backend._state[step.module.id]["buf"]
    assert np.array_equal(buf, RAMP(np.arange(100)).astype(np.float64))
    assert np.array_equal(out[:100], RAMP(np.arange(100)))
    assert np.array_equal(out[100:150], out[0:100:2])


def test_a_rec_edge_at_half_speed_lands_the_head_on_its_slot():
    # At 0.5x from t=100 the head sits between slots on odd samples: at
    # t=201 it is over 50.5. The rec edge there floors it to slot 50 and
    # the ten-sample take writes 9.0 into 50..60, one slot per sample.
    rec = lambda t: ((t < 100) | ((t >= 201) & (t < 211))).astype(np.float32)
    x = lambda t: np.where(t < 100, RAMP(t), 9.0).astype(np.float32)
    out, pos, step = _render(400, {"length": 0.1, "mode": "replace", "speed": "0.5x"}, x=x, rec=rec)
    buf = step.backend._state[step.module.id]["buf"]
    assert pos[200] == 0.5 and pos[201] == 0.5 and pos[210] == pytest.approx(0.59)
    assert np.all(buf[50:60] == 9.0)
    assert buf[49] == RAMP(np.array([49]))[0] and buf[60] == RAMP(np.array([60]))[0]
    assert pos[211] == pytest.approx(0.6) and pos[212] == pytest.approx(0.605)   # 0.5x resumes at 60


def test_a_speed_change_mid_loop_does_not_jump():
    knobs = {150: {"speed": "2x"}, 200: {"speed": "0.5x"}, 250: {"speed": "1x"}}
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP, knobs=knobs)
    first = out[:100]
    assert out[149] == first[49] and out[150] == first[50] and out[151] == first[52]   # 1x -> 2x at slot 50
    # 2x for 50 samples covers 100 slots: back at slot 50 by t=200; then
    # 0.5x from 50: 50, 50.5, 51 ...
    assert out[200] == first[50] and out[202] == first[51]
    assert out[201] == pytest.approx(0.5 * (float(first[50]) + float(first[51])))
    # 0.5x for 50 samples covers 25 slots: at 75 by t=250, then 1x.
    assert np.array_equal(out[250:275], first[75:100])


# ----- the rate jack (speed_cv) ---------------------------------------------------------

CONST = lambda v: (lambda t: np.full(len(t), np.float32(v), np.float32))


def _predicted_rate2(speed, depth, cv):
    """The documented formula, re-derived here from the docs rather than
    imported from the engine: ``floor(base x 2 ** clip(depth x cv) + 0.5)``
    half-steps per sample, clamped 1..8."""
    base2 = {"0.5x": 1, "1x": 2, "2x": 4}[speed]
    e = min(4.0, max(-4.0, depth * float(np.float32(cv))))
    return int(min(8, max(1, math.floor(base2 * (2.0 ** e) + 0.5))))


def _measured_rate2(pos, L, lo, hi):
    """The head's step in half-samples, read straight off the pos ramp."""
    d = np.diff(pos[lo:hi].astype(np.float64)) * (2.0 * L)
    d = np.round(d[d > 1e-9]).astype(int)
    assert d.size and len(set(d.tolist())) == 1, sorted(set(d.tolist()))
    return int(d[0])


@pytest.mark.parametrize("speed", CV_RECORDER_SPEEDS)
def test_a_zero_rate_cv_is_the_combo_bit_exact(speed):
    # The pin that matters for every patch saved before today: a cable
    # carrying 0 must not move a single sample.
    kw = dict(x=RAMP, rec=REC_FIRST_LOOP)
    a, ap, _ = _render(400, {"length": 0.1, "speed": speed}, **kw)
    b, bp, _ = _render(400, {"length": 0.1, "speed": speed}, speed_cv=CONST(0.0), **kw)
    assert np.array_equal(a, b) and np.array_equal(ap, bp)


@pytest.mark.parametrize("cv,rate2", [
    (-3.0, 1), (-2.0, 1), (-1.0, 1), (0.0, 2), (1.0, 4), (2.0, 8), (3.0, 8), (9.0, 8),
])
def test_the_rate_ladder_at_1x_is_where_the_docs_say(cv, rate2):
    # Named rungs, all exact in binary: 0.5x is the floor and 4x the
    # ceiling (the clamp), and the jack never reaches 0 -- the stop is
    # what ``play`` is for.
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP,
                          speed_cv=CONST(cv))
    assert _measured_rate2(pos, 100, 150, 380) == rate2
    assert _predicted_rate2("1x", 1.0, cv) == rate2


@pytest.mark.parametrize("speed", CV_RECORDER_SPEEDS)
@pytest.mark.parametrize("depth", [-2.0, -1.0, 0.5, 1.0, 2.0])
@pytest.mark.parametrize("cv", [-1.7, -0.6, -0.2, 0.0, 0.3, 0.9, 1.4])
def test_the_engine_quantises_exactly_as_documented(speed, depth, cv):
    # A sweep across the combo x depth x CV: the engine must agree with
    # the formula in the docs at every point, including the ones that
    # land on an ODD number of half-steps (1.5x / 2.5x / 3.5x).
    params = {"length": 0.1, "speed": speed, "speed_cv_depth": depth}
    out, pos, _ = _render(400, params, x=RAMP, rec=REC_FIRST_LOOP, speed_cv=CONST(cv))
    assert _measured_rate2(pos, 100, 150, 380) == _predicted_rate2(speed, depth, cv)


def test_every_rung_of_the_ladder_is_reachable():
    seen = {_predicted_rate2("1x", 1.0, c): c for c in np.linspace(-1.2, 2.2, 400)}
    assert sorted(seen) == list(range(1, 9))                  # 0.5x .. 4x in 0.25x steps
    for rate2, cv in sorted(seen.items()):
        _, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP,
                            speed_cv=CONST(cv))
        assert _measured_rate2(pos, 100, 150, 380) == rate2


def test_the_step_between_1x_and_1_5x_sits_at_2_5_half_steps():
    # The quantiser rounds half-UP (floor(v + 0.5)), so the boundary
    # between 2 and 3 half-steps is a rate of exactly 2.5.
    lo = math.log2(2.49 / 2.0)
    hi = math.log2(2.51 / 2.0)
    _, pos_lo, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP,
                           speed_cv=CONST(lo))
    _, pos_hi, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP,
                           speed_cv=CONST(hi))
    assert _measured_rate2(pos_lo, 100, 150, 380) == 2
    assert _measured_rate2(pos_hi, 100, 150, 380) == 3


def test_a_sweeping_rate_cv_never_jumps_the_head():
    # The head is absolute state, not a phase x rate, so a rate change
    # cannot move it: every step of ``pos`` (mod the wrap) is at most the
    # fastest rate, 8 half-samples = 4 slots of a 100-slot loop.
    sweep = lambda t: (2.0 * np.sin(t / 130.0)).astype(np.float32)
    out, pos, _ = _render(2000, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP,
                          speed_cv=sweep)
    step = np.diff(pos.astype(np.float64)) % 1.0
    assert step.max() <= 8 / 200.0 + 1e-6, float(step.max())
    assert len(set(np.round(step[step > 1e-9] * 200.0).astype(int).tolist())) > 1


def test_a_non_finite_rate_cv_reads_as_zero():
    # Python's min/max do not propagate NaN, so the scrub has to happen
    # BEFORE the clamp or a NaN would reach ``int()``.
    ref, refp, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP)
    for bad in (np.nan, np.inf, -np.inf):
        out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP,
                              speed_cv=CONST(bad))
        assert np.array_equal(out, ref) and np.array_equal(pos, refp)


def test_a_polyphonic_rate_cv_is_averaged_not_summed():
    # The house rule for a block-mean depth: four voices at +1 read as
    # +1, not +4 (which would slam the rate into the clamp).
    def go(cv):
        step = _driver({"length": 0.1}, speed_cv=True)
        poss = []
        for i in range(8):
            t = np.arange(i * BLOCK, (i + 1) * BLOCK)
            poss.append(step(BLOCK, rec=(t < 100).astype(np.float32),
                             speed_cv=cv, **{"in": RAMP(t)})["pos"])
        return np.concatenate(poss)

    one = np.full(BLOCK, 1.0, np.float32)
    assert np.array_equal(go(one), go(np.vstack([one] * 4)))
    assert not np.array_equal(go(one), go(np.full(BLOCK, 4.0, np.float32)))


def test_the_rate_jack_does_not_touch_the_recording_rate():
    # "Record at 1x, play at any" still holds with the jack wide open.
    out, pos, _ = _render(400, {"length": 0.1}, x=RAMP, rec=REC_FIRST_LOOP,
                          speed_cv=CONST(2.0))
    assert np.array_equal(out[:100], RAMP(np.arange(100)))        # the take is real time
    assert _measured_rate2(pos, 100, 150, 380) == 8


@pytest.mark.parametrize("rate2", list(range(1, 9)))
def test_every_reachable_rate_is_block_size_independent(rate2):
    cv = math.log2(rate2 / 2.0) if rate2 != 1 else -1.0
    rec = lambda t: (((t >= 37) & (t < 137)) | ((t >= 313) & (t < 371))).astype(np.float32)
    rev = lambda t: ((t >= 433) & (t < 611)).astype(np.float32)
    kw = dict(x=RAMP, rec=rec, reverse=rev, speed_cv=CONST(cv))
    a = _render(1000, {"length": 0.1, "feedback": 0.7}, block=50, **kw)
    b = _render(1000, {"length": 0.1, "feedback": 0.7}, block=250, **kw)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
    assert _measured_rate2(a[1], 100, 150, 300) == rate2


# ----- the one-shot (play_mode) ----------------------------------------------------------

ONE_SHOT = {"length": 0.1, "mode": "replace", "play_mode": "one_shot"}
#: Record the first 100 samples, then trigger at 200 (the gate falls at
#: 210 -- a TRIGGER, not a gate: the lap must outlive it).
REC_THEN_TRIG = lambda t: ((t >= 200) & (t < 210)).astype(np.float32)
NO_PLAY = lambda t: np.zeros(len(t), np.float32)


def test_one_shot_fires_exactly_one_lap_and_holds_the_lap_s_end():
    out, pos, _ = _render(500, ONE_SHOT, x=RAMP, rec=REC_FIRST_LOOP, play=REC_THEN_TRIG)
    take = RAMP(np.arange(100))
    assert np.array_equal(out[200:300], take)                 # the ramp, once
    assert np.all(out[300:] == take[-1])                      # then held at its TOP
    assert np.all(pos[300:] == pos[300])
    assert np.all(out[137:200] == take[0])                    # and nothing before the shot


def test_one_shot_ignores_the_play_level():
    # A gate that stays HIGH for three loop lengths still fires one lap.
    play = lambda t: (t >= 200).astype(np.float32)
    out, _, _ = _render(600, ONE_SHOT, x=RAMP, rec=REC_FIRST_LOOP, play=play)
    take = RAMP(np.arange(100))
    assert np.array_equal(out[200:300], take) and np.all(out[300:] == take[-1])


def test_a_retrigger_mid_lap_restarts_the_lap_from_zero():
    play = lambda t: (((t >= 200) & (t < 205)) | ((t >= 250) & (t < 255))).astype(np.float32)
    out, _, _ = _render(500, ONE_SHOT, x=RAMP, rec=REC_FIRST_LOOP, play=play)
    take = RAMP(np.arange(100))
    assert np.array_equal(out[200:250], take[:50])            # the first lap, cut short
    assert np.array_equal(out[250:350], take)                 # restarted from 0
    assert np.all(out[350:] == take[-1])


def test_a_one_shot_in_reverse_runs_the_lap_backwards_from_the_end():
    rev = lambda t: np.ones(len(t), np.float32) * (t >= 150)
    out, _, _ = _render(500, ONE_SHOT, x=RAMP, rec=REC_FIRST_LOOP,
                        play=REC_THEN_TRIG, reverse=rev)
    take = RAMP(np.arange(100))
    assert np.array_equal(out[200:300], take[::-1])           # top to foot
    assert np.all(out[300:] == take[0])                       # held at the FOOT


def test_a_one_shot_lap_is_one_loop_of_travel_whatever_the_rate():
    # At 2x the lap is half as long in time -- one lap of the BUFFER.
    out, pos, _ = _render(500, dict(ONE_SHOT, speed="2x"), x=RAMP,
                          rec=REC_FIRST_LOOP, play=REC_THEN_TRIG)
    take = RAMP(np.arange(100))
    assert np.array_equal(out[200:250], take[::2])
    assert np.all(out[250:] == take[98])                      # the last slot it played


def test_recording_runs_the_head_at_one_shot():
    # A stopped head writes nothing, so between shots ``rec`` could never
    # reach the tape -- at one_shot recording itself moves the head.
    out, pos, _ = _render(500, ONE_SHOT, x=RAMP, rec=REC_FIRST_LOOP, play=NO_PLAY)
    take = RAMP(np.arange(100))
    assert np.array_equal(out[:100], take)                    # the take ran, at 1x
    assert np.all(out[100:] == take[0])                       # then the head stopped dead
    assert np.all(pos[100:] == 0.0)


def test_one_shot_with_play_unpatched_never_fires():
    out, pos, _ = _render(500, ONE_SHOT, x=RAMP, rec=REC_FIRST_LOOP)
    take = RAMP(np.arange(100))
    assert np.array_equal(out[:100], take) and np.all(out[100:] == take[0])


def test_a_clocked_sync_tick_snaps_a_one_shot_lap_and_the_lap_carries_on():
    # The transport wins (the same rule as a stopped head), and the lap's
    # remaining TRAVEL is untouched: it still ends L samples after the
    # trigger, having played the top of the loop twice.
    clock = lambda t: ((t % 40) < 5).astype(np.float32)
    rec = lambda t: ((t >= 37) & (t < 300)).astype(np.float32)
    play = lambda t: ((t >= 500) & (t < 505)).astype(np.float32)
    params = {"length": 4.0, "mode": "replace", "play_mode": "one_shot"}
    out, pos, _ = _render(900, params, x=RAMP, rec=rec, clock=clock, play=play)
    L = 160                                     # 4 ticks x a 40-sample period
    assert np.any(np.diff(pos[500:500 + L]) != 0.0)
    synced = np.flatnonzero(pos[501:500 + L] == 0.0) + 501
    assert synced.size == 1 and synced[0] % 40 == 0           # a tick snapped it to 0
    # One lap of TRAVEL on, the head is stopped for good: after that it
    # only ever moves where a sync tick snaps it (the stopped-head rule).
    moved = np.flatnonzero(np.diff(pos[500 + L:])) + 501 + L
    assert all(int(i) % 40 == 0 for i in moved), moved


def test_gate_is_the_shipped_transport_and_one_shot_is_block_size_independent():
    rec = lambda t: (((t >= 37) & (t < 137)) | ((t >= 613) & (t < 671))).astype(np.float32)
    play = lambda t: (((t >= 203) & (t < 211)) | ((t >= 449) & (t < 457))).astype(np.float32)
    rev = lambda t: ((t >= 301) & (t < 419)).astype(np.float32)
    kw = dict(x=RAMP, rec=rec, play=play, reverse=rev)
    params = {"length": 0.1, "mode": "replace", "play_mode": "one_shot"}
    a = _render(1000, params, block=50, **kw)
    b = _render(1000, params, block=250, **kw)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
    assert a[1].max() > 0.5 and a[0].std() > 0.0
    # And ``gate`` is exactly the transport as it shipped.
    g = _render(1000, dict(params, play_mode="gate"), block=50, **kw)
    old = _render(1000, {"length": 0.1, "mode": "replace"}, block=50, **kw)
    assert np.array_equal(g[0], old[0]) and np.array_equal(g[1], old[1])
    assert not np.array_equal(a[0], g[0])                     # the modes really differ


# ----- block sizes --------------------------------------------------------------------


def test_block_size_independent_with_a_patched_input():
    rec = lambda t: (((t >= 37) & (t < 137)) | ((t >= 300) & (t < 350))).astype(np.float32)
    clear = lambda t: ((t >= 600) & (t < 603)).astype(np.float32)
    a_out, a_pos, _ = _render(1000, {"length": 0.1, "feedback": 0.7}, block=50, x=RAMP, rec=rec, clear=clear)
    b_out, b_pos, _ = _render(1000, {"length": 0.1, "feedback": 0.7}, block=250, x=RAMP, rec=rec, clear=clear)
    assert np.array_equal(a_out, b_out) and np.array_equal(a_pos, b_pos)


@pytest.mark.parametrize("speed", CV_RECORDER_SPEEDS)
@pytest.mark.parametrize("clocked", [False, True])
def test_the_transport_is_block_size_independent(speed, clocked):
    # Play and reverse gates with edges mid-stream (odd samples), a
    # second rec take, a clear, at every speed, free-running and clocked.
    rec = lambda t: (((t >= 37) & (t < 137)) | ((t >= 313) & (t < 371))).astype(np.float32)
    play = lambda t: (~(((t >= 183) & (t < 227)) | ((t >= 541) & (t < 563)))).astype(np.float32)
    rev = lambda t: (((t >= 159) & (t < 251)) | ((t >= 433) & (t < 611))).astype(np.float32)
    clear = lambda t: ((t >= 703) & (t < 706)).astype(np.float32)
    clock = (lambda t: ((t % 40) < 5).astype(np.float32)) if clocked else None
    params = {"length": 4.0 if clocked else 0.1, "feedback": 0.7, "speed": speed}
    a = _render(1000, params, block=50, x=RAMP, rec=rec, clear=clear, play=play, reverse=rev, clock=clock)
    b = _render(1000, params, block=250, x=RAMP, rec=rec, clear=clear, play=play, reverse=rev, clock=clock)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
    assert a[1].max() > 0.5 and a[0].std() > 0.0                    # the loop ran, not a silent pass


# ----- defaults are the shipped arithmetic --------------------------------------------


def _old_engine(x, rec, L, mode, feedback):
    """The renderer as it shipped on 2026-09-19, free-running: an integer
    slot ``p``, out = buf[p], write on rec, wrap at L. Pure Python."""
    buf = np.zeros(L, dtype=np.float64)
    out = np.zeros(len(x), dtype=np.float64)
    pos = np.zeros(len(x), dtype=np.float64)
    p, exists, prev = 0, False, False
    for t in range(len(x)):
        on = bool(rec[t] > 0.5)
        if on and not prev and not exists:
            exists, p = True, 0
        prev = on
        if not exists:
            continue
        if on:
            buf[p] = float(x[t]) if mode == "replace" else buf[p] * feedback + float(x[t])
        out[t] = buf[p]
        pos[t] = p / L
        p = (p + 1) % L
    return out.astype(np.float32), pos.astype(np.float32)


@pytest.mark.parametrize("mode,feedback", [("replace", 1.0), ("overdub", 0.6)])
def test_defaults_are_the_shipped_arithmetic_bit_exact(mode, feedback):
    rec = lambda t: (((t >= 37) & (t < 137)) | ((t >= 300) & (t < 350))).astype(np.float32)
    x = lambda t: (np.sin(t * 0.07) * 0.8).astype(np.float32)
    t = np.arange(1000)
    ref_out, ref_pos = _old_engine(x(t), rec(t), 100, mode, feedback)
    for block in (50, 250):
        out, pos, _ = _render(1000, {"length": 0.1, "mode": mode, "feedback": feedback}, block=block, x=x, rec=rec)
        assert np.array_equal(out, ref_out) and np.array_equal(pos, ref_pos)


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


def test_every_param_gets_a_bounded_widget(monkeypatch):
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("cv_recorder").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert all(ord(ch) < 128 for lb in labels for ch in lb), labels   # DPG paints ASCII only


def test_speed_combo_carries_the_recorders_speeds_not_the_transient_shapers(monkeypatch):
    # ``speed`` has a shared combo branch below the TYPE blocks (the
    # transient shaper's fast/med/slow) -- the same shadowing shape as
    # ``mode``. Check the CONTENTS, not the label.
    w = _widgets(monkeypatch)
    speed = [lb for lb in w if lb == "speed" or lb.startswith("speed ")]
    assert len(speed) == 1 and w[speed[0]] == ("add_combo", list(CV_RECORDER_SPEEDS))
    reverse = [lb for lb in w if lb == "reverse" or lb.startswith("reverse ")]
    assert len(reverse) == 1 and w[reverse[0]][0] == "add_checkbox" and "gate" in reverse[0]


def test_play_mode_is_its_own_combo_and_the_rate_depth_is_a_bounded_drag(monkeypatch):
    # ``play_mode`` is deliberately not called ``mode`` -- that word is
    # caught by the shared combo branch, which would hand it the
    # recorder's replace/overdub list. Check the CONTENTS.
    w = _widgets(monkeypatch)
    pm = [lb for lb in w if lb == "play_mode" or lb.startswith("play_mode ")]
    assert len(pm) == 1 and w[pm[0]] == ("add_combo", list(CV_RECORDER_PLAY_MODES))
    assert w["mode"] == ("add_combo", list(CV_RECORDER_MODES))      # still its own
    dep = [lb for lb in w if lb == "speed_cv_depth" or lb.startswith("speed_cv_depth ")]
    assert len(dep) == 1 and w[dep[0]][0] == "add_drag_float"


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
    bar2 = loop[2 * L:3 * L]
    bar3 = loop[3 * L:4 * L]
    assert bar2.std() > 0.05 and not np.array_equal(bar2, bar3)


def test_the_backwards_example_freezes_on_the_downbeat_and_alternates_direction():
    """The transport example: a bar-long take (created on tick 1, the
    period being known then), both dividers reset onto the loop's bar,
    ``play`` low for the first beat of every playback bar, ``reverse``
    high on alternate bars, a fresh take every eight bars that is never
    interrupted. Measured on the recorder's own ``pos``."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "cv_recorder_backwards.json"
    patch = load_patch(path)
    recs = [m for m in patch if m.TYPE == "cv_recorder"]
    assert len(recs) == 1 and len(list(patch)) <= 12
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    cap = {"out": [], "pos": [], "play": []}
    orig = b._render_cv_recorder

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap["out"].append(np.asarray(r["out"]).copy())
        cap["pos"].append(np.asarray(r["pos"]).copy())
        cap["play"].append(np.asarray(b._input_buffer(p, buffers, module.id, "play")).copy())
        return r

    b._render_cv_recorder = spy
    peak = 0.0
    for i in range(int(44100 * 19 / 512)):
        np.random.seed(i)
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8
    pos = np.concatenate(cap["pos"])
    loop = np.concatenate(cap["out"])
    play = np.concatenate(cap["play"])
    tick = 44100 * 0.125                                            # a sixteenth at 120
    st = b._state[recs[0].id]
    assert st["exists"] and abs(st["L"] - 16 * tick) < 20          # a bar (16 x the measured period)

    def span(t0, t1, margin=300):
        return slice(int(round(t0 * tick)) + margin, int(round(t1 * tick)) - margin)

    # The take: ticks 1..17, running forwards, never stopped.
    assert np.all(play[span(1, 17)] > 0.5)
    assert np.all(np.diff(pos[span(1, 17)]) >= 0) and loop[span(1, 17)].std() > 0.1
    # Bars 2..8 (ticks 17 + 16k): frozen at the top for the first beat --
    # pos held at exactly 0 and out constant -- then backwards on the
    # even bars (pos falling from ~1) and forwards on the odd ones.
    for k in range(7):
        bar = 17 + 16 * k
        frozen = span(bar, bar + 4)
        assert np.all(pos[frozen] == 0.0), k
        assert np.all(loop[frozen] == loop[frozen][0]), k
        moving = span(bar + 4, bar + 16)
        d = np.diff(pos[moving])
        if k % 2 == 0:
            assert pos[moving][0] > 0.9 and np.all(d <= 0), k          # backwards
        else:
            assert pos[moving][0] < 0.1 and np.all(d >= 0), k          # forwards
        assert loop[moving].std() > 0.1, k                              # the wobble is moving
    # Bar 9 is the next take: forwards, not frozen, the same tick grid.
    take2 = span(129, 145)
    assert np.all(play[take2] > 0.5) and np.all(np.diff(pos[take2]) >= 0)


def test_the_oneshot_example_fires_one_lap_per_hit_at_a_wandering_rate():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "cv_recorder_oneshot.json"
    patch = load_patch(path)
    recs = [m for m in patch if m.TYPE == "cv_recorder"]
    assert len(recs) == 1 and recs[0].params["play_mode"] == "one_shot"
    assert len(patch.modules) <= 12
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    cap = {"out": [], "pos": [], "play": []}
    orig = b._render_cv_recorder

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap["out"].append(np.asarray(r["out"]).copy())
        cap["pos"].append(np.asarray(r["pos"]).copy())
        pl = b._input_buffer(p, buffers, module.id, "play")
        cap["play"].append(np.zeros(frames) if pl is None else np.asarray(pl).copy())
        return r

    b._render_cv_recorder = spy
    peak = 0.0
    for _ in range(int(44100 * 30 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8, peak
    pos = np.concatenate(cap["pos"])
    loop = np.concatenate(cap["out"])
    L = int(b._state[recs[0].id]["L"])
    assert L > 0 and loop.std() > 0.05

    # The head moves in DISCRETE runs: one take, then one lap per hit.
    moving = np.flatnonzero(np.diff(pos.astype(np.float64)) != 0.0) + 1
    runs, start, prev = [], moving[0], moving[0]
    for i in moving[1:]:
        if i - prev > 1:
            runs.append((int(start), int(prev)))
            start = i
        prev = i
    runs.append((int(start), int(prev)))
    runs = [r for r in runs if r[1] - r[0] > 2]     # drop the sync-tick snaps
    take, laps = runs[0], runs[1:]
    assert abs((take[1] - take[0] + 1) - L) < 8 and len(laps) >= 8

    rates = []
    for a, z in laps:
        d = np.diff(pos[a:z + 1].astype(np.float64)) * (2.0 * L)
        d = np.round(d[d > 1e-9]).astype(int)
        rates.append(sorted(set(d.tolist())))
        assert all(3 <= r <= 6 for r in rates[-1]), rates[-1]   # 1.5x .. 3x
        # A lap is one loop of TRAVEL, so it is shorter the faster it runs.
        assert abs((z - a + 1) - 2 * L / float(np.mean(d))) < 32, (a, z)
    flat = sorted({r for rr in rates for r in rr})
    assert len(flat) >= 3, flat                    # the wobble really wandered
    # Every lap finishes before the next hit: the head HOLDS in between.
    play = np.concatenate(cap["play"])
    hits = np.flatnonzero((play[1:] > 0.5) & (play[:-1] <= 0.5)) + 1
    assert hits.size >= len(laps)
    for (a, z), h in zip(laps, hits):
        assert h <= a <= h + 1, (a, h)              # the lap starts on the edge
        assert z < h + L                            # done before the loop length is up
