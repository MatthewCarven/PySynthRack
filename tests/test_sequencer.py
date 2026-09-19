"""Tests for the Sequencer module — clock-driven step sequencer.

The 2026-09-19 love pass added ``direction`` (forward / backward /
pendulum / random) and ``seed``. The load-bearing claims:

* ``forward`` (the default) is bit-exact with the pre-direction engine —
  pinned here against a line-for-line copy of the shipped loop, with a
  reset in play, for both an old-shape param dict (no ``direction`` key,
  as a pre-love-pass patch file loads) and the explicit defaults. The
  recipe's five reference renders (the melody, clockwork_groove and
  lfo_retrigger examples plus two synthetic patches) were bit-exact too.
* every order is one pure rule, ``next_step_index``, so the exact
  sequences are pinned on the helper AND the renderer is shown to follow
  it edge for edge.
* ``random`` is a pure function of ``seed`` and the clock edges: covers
  every step, deterministic per seed, different across seeds, block-size
  independent, and a ``reset`` replays the same phrase.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type
from pysynthrack.modules.sequencer import (
    MAX_STEPS,
    SEQ_DIRECTIONS,
    Sequencer,
    next_step_index,
)

SR = 44100


def _backend(patch):
    b = NumpyBackend()
    b.compile(patch)
    return b


def _pulses(frames, positions, width=4):
    """A mono gate buffer with `width`-sample-high pulses at `positions`."""
    buf = np.zeros(frames, dtype=np.float32)
    for pos in positions:
        buf[pos:pos + width] = 1.0
    return buf


def _seq_patch(params=None):
    patch = Patch()
    seq = patch.add_module("sequencer", params=params or {})
    clk = patch.add_module("clock")
    patch.connect(clk.id, "out", seq.id, "clock")
    return patch, seq, clk


def _numbered(direction, steps, seed=1, module_type="sequencer", **extra):
    """A patch whose step *i* has pitch *i* semitones, so ``cv * 12`` read
    at a clock edge IS the 1-based step number — the orders read straight
    off the output. Returns (patch, seq, clk, rst)."""
    params = {"steps": steps, "direction": direction, "seed": seed}
    for i in range(1, MAX_STEPS + 1):
        params[f"step{i}_pitch"] = float(i)
    params.update(extra)
    patch = Patch()
    seq = patch.add_module(module_type, params=params)
    clk = patch.add_module("clock")
    rst = patch.add_module("clock")
    patch.connect(clk.id, "out", seq.id, "clock")
    patch.connect(rst.id, "out", seq.id, "reset")
    return patch, seq, clk, rst


def _steps_played(b, seq, clk, rst, clock, reset=None, chunk=None):
    """Render and return the step number at each clock edge (see
    ``_numbered``). ``chunk`` renders in that block size instead of one
    call — for the block-size-independence claim."""
    frames = len(clock)
    if reset is None:
        reset = np.zeros(frames, dtype=np.float32)
    buffers = {(clk.id, "out"): clock, (rst.id, "out"): reset}
    if chunk is None:
        cv = b._render_sequencer(seq, frames, buffers, seq_patch_of(b))["cv"]
    else:
        parts = []
        for s0 in range(0, frames, chunk):
            sub = {k: v[s0:s0 + chunk] for k, v in buffers.items()}
            parts.append(b._render_sequencer(seq, chunk, sub, seq_patch_of(b))["cv"])
        cv = np.concatenate(parts)
    edges = np.flatnonzero((clock[1:] > 0.5) & (clock[:-1] <= 0.5)) + 1
    if clock[0] > 0.5:
        edges = np.concatenate([[0], edges])
    return [int(round(float(cv[e]) * 12)) for e in edges]


def seq_patch_of(b):
    return b._patch


def _walk(direction, steps, n, rng=None):
    """Drive the pure rule ``n`` times from the start; 1-based steps."""
    idx, asc = -1, True
    out = []
    for _ in range(n):
        idx, asc = next_step_index(idx, steps, direction, asc, rng)
        out.append(idx + 1)
    return out


class TestSequencerModel:
    def test_register_and_defaults(self):
        patch = Patch()
        seq = patch.add_module("sequencer")
        assert isinstance(seq, Sequencer)
        assert seq.TYPE == "sequencer"
        assert seq.params["steps"] == 8
        # 3 (steps, direction, seed) + 16 pitch + 16 on
        assert len(seq.params) == 3 + 2 * MAX_STEPS
        assert seq.params["direction"] == "forward"
        assert seq.params["seed"] == 1
        assert SEQ_DIRECTIONS == ("forward", "backward", "pendulum", "random")
        # default C-major scale on the first 8 steps
        assert seq.params["step1_pitch"] == 0.0
        assert seq.params["step3_pitch"] == 4.0
        assert seq.params["step8_pitch"] == 12.0
        assert seq.params["step1_on"] is True

    def test_ports(self):
        patch = Patch()
        seq = patch.add_module("sequencer")
        assert [(p.name, p.signal_kind) for p in seq.input_ports] == [
            ("clock", "gate"), ("reset", "gate")
        ]
        assert [(p.name, p.signal_kind) for p in seq.output_ports] == [
            ("cv", "cv"), ("gate", "gate")
        ]


class TestSequencerStepping:
    def test_first_pulse_plays_step_one(self):
        patch, seq, clk = _seq_patch(
            {"steps": 4, "step1_pitch": 5.0, "step2_pitch": 0.0}
        )
        b = _backend(patch)
        clock = _pulses(60, [0, 20, 40])
        out = b._render_sequencer(seq, 60, {(clk.id, "out"): clock}, patch)
        # First pulse -> step1 = 5 semitones -> 5/12 V.
        assert abs(float(out["cv"][2]) - 5.0 / 12.0) < 1e-6

    def test_cv_is_one_volt_per_octave(self):
        patch, seq, clk = _seq_patch({
            "steps": 4,
            "step1_pitch": 0.0, "step2_pitch": 12.0,
            "step3_pitch": 7.0, "step4_pitch": -12.0,
        })
        b = _backend(patch)
        clock = _pulses(160, [k * 20 for k in range(4)])
        out = b._render_sequencer(seq, 160, {(clk.id, "out"): clock}, patch)
        vals = [round(float(out["cv"][k * 20 + 2]), 5) for k in range(4)]
        assert vals == [0.0, 1.0, round(7 / 12, 5), -1.0]

    def test_wraps_after_steps(self):
        patch, seq, clk = _seq_patch({
            "steps": 3,
            "step1_pitch": 1.0, "step2_pitch": 2.0, "step3_pitch": 3.0,
        })
        b = _backend(patch)
        clock = _pulses(120, [k * 20 for k in range(5)])  # 5 pulses, 3 steps
        out = b._render_sequencer(seq, 120, {(clk.id, "out"): clock}, patch)
        vals = [round(float(out["cv"][k * 20 + 2]) * 12, 3) for k in range(5)]
        assert vals == [1.0, 2.0, 3.0, 1.0, 2.0]  # wrapped back to step1

    def test_gate_high_only_during_clock_pulse(self):
        patch, seq, clk = _seq_patch({"steps": 2})
        b = _backend(patch)
        clock = _pulses(40, [0, 20], width=5)
        out = b._render_sequencer(seq, 40, {(clk.id, "out"): clock}, patch)
        gate = out["gate"]
        assert float(gate[2]) == 1.0           # inside first pulse
        assert float(gate[10]) == 0.0          # between pulses
        assert int(gate.sum()) == 10           # two 5-sample pulses, both enabled

    def test_disabled_step_is_a_rest(self):
        patch, seq, clk = _seq_patch({"steps": 3, "step2_on": False})
        b = _backend(patch)
        clock = _pulses(120, [k * 20 for k in range(3)], width=5)
        out = b._render_sequencer(seq, 120, {(clk.id, "out"): clock}, patch)
        # step2's pulse window (samples 20..25) must stay low...
        assert float(out["gate"][22]) == 0.0
        # ...but the step still advances: pulse 3 is step3, gate high again.
        assert float(out["gate"][42]) == 1.0

    def test_cv_holds_between_pulses(self):
        patch, seq, clk = _seq_patch({"steps": 2, "step1_pitch": 6.0})
        b = _backend(patch)
        clock = _pulses(60, [0, 40], width=3)
        out = b._render_sequencer(seq, 60, {(clk.id, "out"): clock}, patch)
        # Step1 value persists across the gap until the next pulse.
        assert abs(float(out["cv"][20]) - 6.0 / 12.0) < 1e-6

    def test_reset_rewinds_to_step_one(self):
        patch = Patch()
        seq = patch.add_module("sequencer", params={
            "steps": 4, "step1_pitch": 3.0, "step2_pitch": 6.0,
        })
        clk = patch.add_module("clock")
        rst = patch.add_module("clock")
        patch.connect(clk.id, "out", seq.id, "clock")
        patch.connect(rst.id, "out", seq.id, "reset")
        b = _backend(patch)
        clock = _pulses(100, [0, 20, 60])
        reset = _pulses(100, [40])
        out = b._render_sequencer(
            seq, 100, {(clk.id, "out"): clock, (rst.id, "out"): reset}, patch
        )
        vals = [round(float(out["cv"][pos + 1]) * 12, 3) for pos in (0, 20, 60)]
        assert vals == [3.0, 6.0, 3.0]  # reset before 3rd pulse -> back to step1

    def test_idle_without_clock_is_silent(self):
        patch, seq, clk = _seq_patch()
        b = _backend(patch)
        out = b._render_sequencer(seq, 128, {(clk.id, "out"): np.zeros(128, np.float32)}, patch)
        assert float(np.max(np.abs(out["cv"]))) == 0.0
        assert float(out["gate"].sum()) == 0.0

    def test_dispatch_returns_cv_and_gate(self):
        patch, seq, clk = _seq_patch()
        b = _backend(patch)
        out = b._render_module(seq, 64, {(clk.id, "out"): _pulses(64, [0])}, patch)
        assert isinstance(out, dict)
        assert set(out) == {"cv", "gate"}
        assert out["cv"].shape == (64,) and out["gate"].shape == (64,)


# ----- the pure rule ----------------------------------------------------------


class TestNextStepIndex:
    """``next_step_index`` — the exact orders, three passes at steps 5."""

    def test_forward_three_passes_at_five(self):
        assert _walk("forward", 5, 15) == [1, 2, 3, 4, 5] * 3

    def test_backward_three_passes_at_five(self):
        assert _walk("backward", 5, 15) == [5, 4, 3, 2, 1] * 3

    def test_pendulum_three_passes_at_five(self):
        # An 8-note period: the turnaround steps (1 and 5) play once.
        assert _walk("pendulum", 5, 24) == [1, 2, 3, 4, 5, 4, 3, 2] * 3

    def test_pendulum_degenerate_lengths(self):
        assert _walk("pendulum", 1, 5) == [1, 1, 1, 1, 1]
        assert _walk("pendulum", 2, 6) == [1, 2, 1, 2, 1, 2]
        # ...and the other orders at length 1 are the same one step.
        assert _walk("forward", 1, 3) == [1, 1, 1]
        assert _walk("backward", 1, 3) == [1, 1, 1]
        assert _walk("backward", 2, 4) == [2, 1, 2, 1]

    def test_random_draws_once_per_call_and_only_when_there_is_a_choice(self):
        rng = np.random.default_rng(3)
        ref = [int(v) + 1 for v in np.random.default_rng(3).integers(5, size=12)]
        assert _walk("random", 5, 12, rng) == ref
        # steps 1: no choice, no draw — the generator's state is untouched.
        rng = np.random.default_rng(3)
        before = rng.bit_generator.state
        assert _walk("random", 1, 6, rng) == [1] * 6
        assert rng.bit_generator.state == before

    def test_random_without_an_rng_is_an_error(self):
        with pytest.raises(ValueError):
            next_step_index(-1, 5, "random", True, None)

    def test_forward_and_backward_leave_the_pendulum_heading(self):
        # forward leaves it heading up, backward heading down, so a live
        # switch to pendulum carries on the way you were going.
        assert next_step_index(2, 5, "forward", False) == (3, True)
        assert next_step_index(2, 5, "backward", True) == (1, False)
        # random leaves it alone.
        assert next_step_index(2, 5, "random", False, np.random.default_rng(1))[1] is False

    def test_index_past_the_end_is_the_new_last_step(self):
        # steps shrank from 8+ to 4 while step 8 (idx 7) was playing:
        assert next_step_index(7, 4, "forward") == (0, True)        # wrap to 1
        assert next_step_index(7, 4, "backward") == (2, False)      # 4 -> 3
        assert next_step_index(7, 4, "pendulum", True) == (2, False)   # turn
        assert next_step_index(7, 4, "pendulum", False) == (2, False)  # same
        # An index still inside the new length is untouched.
        assert next_step_index(1, 4, "forward") == (2, True)
        assert next_step_index(1, 4, "pendulum", True) == (2, True)


# ----- the renderer -----------------------------------------------------------


def _old_engine(b, clock, reset, steps, pitches, ons):
    """The shipped (pre-direction, 2026-09-18) ``_render_sequencer`` inner
    loop, line for line — the documented previous behaviour."""
    gate_high = b._GATE_HIGH
    idx, cur_cv, prev_clock, prev_reset = -1, 0.0, False, False
    cv_out = np.empty(len(clock), dtype=np.float32)
    gate_out = np.empty(len(clock), dtype=np.float32)
    for n in range(len(clock)):
        c = bool(clock[n] > gate_high)
        r = bool(reset[n] > gate_high) if reset is not None else False
        if r and not prev_reset:
            idx = -1
        prev_reset = r
        if c and not prev_clock:
            idx = (idx + 1) % steps
            cur_cv = pitches[idx] / 12.0
        prev_clock = c
        cv_out[n] = cur_cv
        gate_out[n] = 1.0 if (c and idx >= 0 and ons[idx]) else 0.0
    return cv_out, gate_out


class TestSequencerDirection:
    def test_forward_default_is_bit_exact_with_the_old_engine(self):
        # Both param shapes: an old patch file (no direction/seed keys at
        # all) and the explicit defaults. Reset in play, a rest in play,
        # rendered in two blocks so state carries across.
        pitches = [0.0, 3.0, 7.0, 10.0, 12.0, -5.0, 2.0, 9.0] + [0.0] * 8
        ons = [True, True, False, True, True, True, True, False] + [True] * 8
        base = {"steps": 6}
        for i in range(MAX_STEPS):
            base[f"step{i + 1}_pitch"] = pitches[i]
            base[f"step{i + 1}_on"] = ons[i]
        old_shape = dict(base)
        new_shape = dict(base, direction="forward", seed=1)
        frames = 600
        clock = _pulses(frames, list(range(0, frames, 37)), width=6)
        reset = _pulses(frames, [222, 480], width=9)
        for params in (old_shape, new_shape):
            patch = Patch()
            seq = patch.add_module("sequencer", params=params)
            clk = patch.add_module("clock")
            rst = patch.add_module("clock")
            patch.connect(clk.id, "out", seq.id, "clock")
            patch.connect(rst.id, "out", seq.id, "reset")
            b = _backend(patch)
            half = frames // 2
            outs = [
                b._render_sequencer(
                    seq, half,
                    {(clk.id, "out"): clock[s0:s0 + half], (rst.id, "out"): reset[s0:s0 + half]},
                    patch,
                )
                for s0 in (0, half)
            ]
            cv = np.concatenate([o["cv"] for o in outs])
            gate = np.concatenate([o["gate"] for o in outs])
            ref_cv, ref_gate = _old_engine(b, clock, reset, 6, pitches, ons)
            assert np.array_equal(cv, ref_cv)
            assert np.array_equal(gate, ref_gate)

    @pytest.mark.parametrize("direction", SEQ_DIRECTIONS)
    def test_renderer_follows_the_rule_edge_for_edge(self, direction):
        patch, seq, clk, rst = _numbered(direction, 5, seed=11)
        b = _backend(patch)
        clock = _pulses(24 * 20, [k * 20 for k in range(24)])
        rng = np.random.default_rng(11) if direction == "random" else None
        assert _steps_played(b, seq, clk, rst, clock) == _walk(direction, 5, 24, rng)

    def test_backward_first_clock_and_reset_land_on_the_last_step(self):
        patch, seq, clk, rst = _numbered("backward", 5)
        b = _backend(patch)
        clock = _pulses(140, [0, 20, 40, 80, 100, 120])
        reset = _pulses(140, [60])
        assert _steps_played(b, seq, clk, rst, clock, reset) == [5, 4, 3, 5, 4, 3]

    def test_pendulum_reset_heads_up_again(self):
        patch, seq, clk, rst = _numbered("pendulum", 4)
        b = _backend(patch)
        # 1 2 3 4 3 | reset | 1 2 3 — descending when reset, up again after.
        clock = _pulses(200, [k * 20 for k in range(5)] + [120, 140, 160])
        reset = _pulses(200, [110])
        assert _steps_played(b, seq, clk, rst, clock, reset) == [1, 2, 3, 4, 3, 1, 2, 3]

    def test_random_covers_every_step_and_is_deterministic_per_seed(self):
        clock = _pulses(60 * 20, [k * 20 for k in range(60)])
        runs = []
        for _ in range(2):
            patch, seq, clk, rst = _numbered("random", 5, seed=1)
            runs.append(_steps_played(_backend(patch), seq, clk, rst, clock))
        assert runs[0] == runs[1]
        assert set(runs[0]) == {1, 2, 3, 4, 5}
        assert all(1 <= s <= 5 for s in runs[0])
        # ...and it is the seed's own stream, the rule applied per edge.
        assert runs[0] == _walk("random", 5, 60, np.random.default_rng(1))

    def test_random_differs_across_seeds(self):
        clock = _pulses(32 * 20, [k * 20 for k in range(32)])
        phrases = {}
        for seed in (1, 2, 3):
            patch, seq, clk, rst = _numbered("random", 5, seed=seed)
            phrases[seed] = _steps_played(_backend(patch), seq, clk, rst, clock)
        assert phrases[1] != phrases[2] != phrases[3] and phrases[1] != phrases[3]

    def test_random_reset_replays_the_same_phrase(self):
        patch, seq, clk, rst = _numbered("random", 6, seed=5)
        b = _backend(patch)
        # 8 clocks, reset, 8 clocks, reset, 8 clocks -> the same phrase x3
        positions = [k * 20 for k in range(8)] + [200 + k * 20 for k in range(8)] + [400 + k * 20 for k in range(8)]
        clock = _pulses(600, positions)
        reset = _pulses(600, [190, 390])
        played = _steps_played(b, seq, clk, rst, clock, reset)
        assert played[:8] == played[8:16] == played[16:24]
        # It's a phrase, not a drone.
        assert len(set(played[:8])) > 1

    def test_random_seed_change_reseeds_on_the_spot(self):
        patch, seq, clk, rst = _numbered("random", 5, seed=1)
        b = _backend(patch)
        clock = _pulses(8 * 20, [k * 20 for k in range(8)])
        first = _steps_played(b, seq, clk, rst, clock)
        seq.set_param("seed", 9)
        after = _steps_played(b, seq, clk, rst, clock)
        assert after == [int(v) + 1 for v in np.random.default_rng(9).integers(5, size=8)]
        assert first == [int(v) + 1 for v in np.random.default_rng(1).integers(5, size=8)]

    def test_random_at_one_step_consumes_nothing(self):
        # Clock through steps=1 first, then widen: the draws that follow
        # are the seed's FIRST draws — nothing was spent while there was
        # no choice to make.
        patch, seq, clk, rst = _numbered("random", 1, seed=4)
        b = _backend(patch)
        clock = _pulses(6 * 20, [k * 20 for k in range(6)])
        assert _steps_played(b, seq, clk, rst, clock) == [1] * 6
        seq.set_param("steps", 5)
        assert _steps_played(b, seq, clk, rst, clock) == _walk("random", 5, 6, np.random.default_rng(4))

    def test_random_is_block_size_independent(self):
        # Draws happen only at clock edges, so 64-sample and 512-sample
        # blocks see the same stream — cv and gate, sample for sample.
        frames = 4096
        clock = _pulses(frames, list(range(0, frames, 96)), width=5)
        outs = []
        for chunk in (64, 512):
            patch, seq, clk, rst = _numbered("random", 7, seed=2)
            b = _backend(patch)
            reset = np.zeros(frames, dtype=np.float32)
            parts = []
            for s0 in range(0, frames, chunk):
                sub = {(clk.id, "out"): clock[s0:s0 + chunk], (rst.id, "out"): reset[s0:s0 + chunk]}
                o = b._render_sequencer(seq, chunk, sub, patch)
                parts.append((o["cv"], o["gate"]))
            outs.append((np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts])))
        assert np.array_equal(outs[0][0], outs[1][0])
        assert np.array_equal(outs[0][1], outs[1][1])
        assert len(set(np.round(outs[0][0] * 12).tolist())) >= 5

    def test_steps_shrunk_mid_run(self):
        clock6 = _pulses(6 * 20, [k * 20 for k in range(6)])
        clock4 = _pulses(4 * 20, [k * 20 for k in range(4)])
        # forward: 8 steps, six clocks -> at step 6; shrink to 4 -> the
        # playing step is the new last step, so the next clock wraps to 1.
        patch, seq, clk, rst = _numbered("forward", 8)
        b = _backend(patch)
        assert _steps_played(b, seq, clk, rst, clock6) == [1, 2, 3, 4, 5, 6]
        seq.set_param("steps", 4)
        assert _steps_played(b, seq, clk, rst, clock4) == [1, 2, 3, 4]
        # forward, still inside the new length: untouched (2 -> 3, 4, 1).
        patch, seq, clk, rst = _numbered("forward", 8)
        b = _backend(patch)
        assert _steps_played(b, seq, clk, rst, _pulses(40, [0, 20])) == [1, 2]
        seq.set_param("steps", 4)
        assert _steps_played(b, seq, clk, rst, clock4) == [3, 4, 1, 2]
        # backward: 8 7 6 5 4 3 -> shrink to 4 (step 3 is inside) -> 2 1 4 3
        patch, seq, clk, rst = _numbered("backward", 8)
        b = _backend(patch)
        assert _steps_played(b, seq, clk, rst, clock6) == [8, 7, 6, 5, 4, 3]
        seq.set_param("steps", 4)
        assert _steps_played(b, seq, clk, rst, clock4) == [2, 1, 4, 3]
        # backward from beyond the end: at 8 (one clock), shrink to 4 -> 3 2 1 4
        patch, seq, clk, rst = _numbered("backward", 8)
        b = _backend(patch)
        assert _steps_played(b, seq, clk, rst, _pulses(20, [0])) == [8]
        seq.set_param("steps", 4)
        assert _steps_played(b, seq, clk, rst, clock4) == [3, 2, 1, 4]
        # pendulum: 1..6 climbing, shrink to 4 -> turns at the new end.
        patch, seq, clk, rst = _numbered("pendulum", 8)
        b = _backend(patch)
        assert _steps_played(b, seq, clk, rst, clock6) == [1, 2, 3, 4, 5, 6]
        seq.set_param("steps", 4)
        assert _steps_played(b, seq, clk, rst, _pulses(140, [k * 20 for k in range(7)])) == [3, 2, 1, 2, 3, 4, 3]

    def test_steps_grown_mid_run_pendulum_carries_on(self):
        patch, seq, clk, rst = _numbered("pendulum", 3)
        b = _backend(patch)
        # 1 2 3 2 -> heading down at step 2; grow to 6 -> 1 2 3 4 5 6 5
        assert _steps_played(b, seq, clk, rst, _pulses(80, [0, 20, 40, 60])) == [1, 2, 3, 2]
        seq.set_param("steps", 6)
        assert _steps_played(b, seq, clk, rst, _pulses(140, [k * 20 for k in range(7)])) == [1, 2, 3, 4, 5, 6, 5]

    def test_switching_direction_live_carries_the_heading(self):
        patch, seq, clk, rst = _numbered("backward", 5)
        b = _backend(patch)
        assert _steps_played(b, seq, clk, rst, _pulses(60, [0, 20, 40])) == [5, 4, 3]
        seq.set_param("direction", "pendulum")
        # backward left the pendulum heading down: 2 1 2 3 4 5 4
        assert _steps_played(b, seq, clk, rst, _pulses(140, [k * 20 for k in range(7)])) == [2, 1, 2, 3, 4, 5, 4]
        seq.set_param("direction", "forward")
        assert _steps_played(b, seq, clk, rst, _pulses(40, [0, 20])) == [5, 1]

    def test_unknown_direction_falls_back_to_forward(self):
        patch, seq, clk, rst = _numbered("sideways", 4)
        b = _backend(patch)
        assert _steps_played(b, seq, clk, rst, _pulses(100, [k * 20 for k in range(5)])) == [1, 2, 3, 4, 1]

    def test_fader_seq_shares_the_direction(self):
        # One engine: the fader-bank twin runs backward too, bit-exact.
        clock = _pulses(12 * 20, [k * 20 for k in range(12)])
        outs = []
        for module_type in ("sequencer", "fader_seq"):
            patch, seq, clk, rst = _numbered("backward", 5, module_type=module_type)
            b = _backend(patch)
            reset = np.zeros(len(clock), dtype=np.float32)
            o = b._render_module(seq, len(clock), {(clk.id, "out"): clock, (rst.id, "out"): reset}, patch)
            outs.append(o)
        assert np.array_equal(outs[0]["cv"], outs[1]["cv"])
        assert np.array_equal(outs[0]["gate"], outs[1]["gate"])
        assert int(round(float(outs[1]["cv"][2]) * 12)) == 5


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch, module_type):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module(module_type)
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int", "add_slider_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs)
    return out


class TestSequencerUI:
    def test_every_param_gets_a_bounded_widget(self, monkeypatch):
        w = _widgets(monkeypatch, "sequencer")
        labels = list(w)
        for name in get_module_type("sequencer").DEFAULT_PARAMS:
            hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
            assert hits, (name, labels)
            assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
        assert w["direction"][0] == "add_combo"
        assert w["seed"][0] == "add_drag_int"
        assert w["seed"][1]["min_value"] == 0

    def test_direction_combo_offers_the_directions(self, monkeypatch):
        # The contents, not the label (the mode-combo lesson).
        w = _widgets(monkeypatch, "sequencer")
        assert list(w["direction"][1]["items"]) == list(SEQ_DIRECTIONS)
        assert w["direction"][1]["default_value"] == "forward"

    def test_fader_seq_panel_carries_direction_and_seed(self, monkeypatch):
        # The fader-bank twin shares the param contract, so its panel has
        # to expose the two whole-pattern controls too.
        w = _widgets(monkeypatch, "fader_seq")
        assert w["direction"][0] == "add_combo"
        assert list(w["direction"][1]["items"]) == list(SEQ_DIRECTIONS)
        assert w["seed"][0] == "add_drag_int"
        assert w["steps"][0] == "add_slider_int"


# ----- example ------------------------------------------------------------------


class TestSequencerExample:
    def test_the_pendulum_example_plays_a_bar_and_replays_the_hat_phrase(self):
        from pysynthrack.io_patch import load_patch

        path = Path(__file__).resolve().parent.parent / "examples" / "sequencer_pendulum.json"
        patch = load_patch(path)
        assert len(list(patch)) <= 12
        seqs = [m for m in patch if m.TYPE == "sequencer"]
        mel = next(m for m in seqs if m.params["direction"] == "pendulum")
        hat = next(m for m in seqs if m.params["direction"] == "random")
        b = NumpyBackend(sample_rate=44100, block_size=512)
        b.compile(patch)
        cap = {mel.id: [], hat.id: []}
        orig = b._render_sequencer

        def spy(module, frames, buffers, p):
            r = orig(module, frames, buffers, p)
            if module.id in cap:
                cap[module.id].append((r["cv"].copy(), r["gate"].copy()))
            return r

        b._render_sequencer = spy
        np.random.seed(0)
        peak = 0.0
        seconds = 6.0
        for _ in range(int(44100 * seconds / 512)):
            out, _devices = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        assert 0.3 < peak < 0.8

        def edges_of(gate):
            e = np.flatnonzero((gate[1:] > 0.5) & (gate[:-1] <= 0.5)) + 1
            return np.concatenate([[0], e]) if gate[0] > 0.5 else e

        # The melody: eighths, five steps in pendulum = one 8-note bar,
        # 1 2 3 4 5 4 3 2 in the patch's pitches, bar after bar.
        mcv = np.concatenate([c for c, _g in cap[mel.id]])
        mgt = np.concatenate([g for _c, g in cap[mel.id]])
        mel_pitches = [int(round(float(mcv[e]) * 12)) for e in edges_of(mgt)]
        assert len(mel_pitches) >= 20
        assert mel_pitches[:16] == [0, 3, 7, 10, 12, 10, 7, 3] * 2

        # The hat: a random sixteenth pattern, reset every bar by the
        # divider's divn=16 -> bars 1 and 2 are the same phrase, with the
        # step pitches doubling as accents (more than one velocity).
        hcv = np.concatenate([c for c, _g in cap[hat.id]])
        hgt = np.concatenate([g for _c, g in cap[hat.id]])
        bar = int(round(44100 * 60 / 120 * 4))     # 88200 samples
        sixteenth = bar / 16
        hits = [(int(round(e / sixteenth)), int(round(float(hcv[e]) * 12))) for e in edges_of(hgt)]
        bars = [[(p - 16 * k, v) for p, v in hits if 16 * k <= p < 16 * (k + 1)] for k in range(3)]
        assert bars[1] == bars[2]
        assert 4 <= len(bars[1]) < 16          # a pattern: hits AND rests
        assert len({v for _p, v in bars[1]}) >= 2  # accents
