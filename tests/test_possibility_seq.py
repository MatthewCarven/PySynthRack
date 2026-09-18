"""Tests for PossibilitySeq — the sequencer with undecided steps.

Ported semantics from PythonBinaryPossibility: a step is "0", "1" or "?";
takes resolve per mode (loop / latch / dice); `balanced` deals fair ?s from
a shuffle-bag so every bar lands on its share. The reference implementation
is `collapse_pattern` in the module file; the renderer is tested for exact
equivalence against it.
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.possibility_seq import (
    MAX_STEPS,
    PossibilitySeq,
    collapse_pattern,
    resolve_step,
)

STEP = 20        # samples per clock period in the hand-built pulse trains
PULSE = 4        # samples the clock stays high


def _pulses(count, step=STEP, width=PULSE, offset=0):
    """A gate buffer with `count` pulses, one per `step` samples."""
    buf = np.zeros(offset + count * step, dtype=np.float32)
    for k in range(count):
        buf[offset + k * step: offset + k * step + width] = 1.0
    return buf


def _patch(params=None):
    patch = Patch()
    seq = patch.add_module("possibility_seq", params=params or {})
    clk = patch.add_module("clock")
    patch.connect(clk.id, "out", seq.id, "clock")
    return patch, seq, clk


def _backend(patch):
    b = NumpyBackend()
    b.compile(patch)
    return b


def _bar_hits(backend, seq, clk, patch, bars=1, steps=16):
    """Render `bars` bars of `steps` clocks and return per-bar fire tuples."""
    out_bars = []
    for _ in range(bars):
        clock = _pulses(steps)
        out = backend._render_possibility_seq(
            seq, len(clock), {(clk.id, "out"): clock}, patch)
        out_bars.append(tuple(bool(out["gate"][k * STEP] > 0.5)
                              for k in range(steps)))
    return out_bars


def _all_undecided(extra=None):
    params = {f"step{i}_state": "?" for i in range(1, MAX_STEPS + 1)}
    params.update(extra or {})
    return params


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        seq = patch.add_module("possibility_seq")
        assert isinstance(seq, PossibilitySeq)
        assert seq.TYPE == "possibility_seq"
        # steps + mode + balanced + seed, then per-step state + p.
        assert len(seq.params) == 4 + 2 * MAX_STEPS
        assert seq.params["mode"] == "loop"
        assert seq.params["balanced"] is False
        # The default pattern ships with four undecided steps: 16 bars.
        undecided = sum(
            1 for i in range(1, MAX_STEPS + 1)
            if seq.params[f"step{i}_state"] == "?"
        )
        assert undecided == 4

    def test_ports(self):
        patch = Patch()
        seq = patch.add_module("possibility_seq")
        assert [(p.name, p.signal_kind) for p in seq.input_ports] == [
            ("clock", "gate"), ("reset", "gate"), ("reroll", "gate"),
        ]
        assert [(p.name, p.signal_kind) for p in seq.output_ports] == [
            ("gate", "gate"),
        ]


class TestReference:
    """The pure helpers, before any rendering is involved."""

    def test_decided_steps_are_decided(self):
        rng = np.random.default_rng(0)
        fires = collapse_pattern("1010", [0.5] * 4, False, rng.random)
        assert fires == (True, False, True, False)

    def test_balanced_deals_every_pair(self):
        # 16 fair ?s: the bag guarantees exactly 8 hits, every seed.
        for seed in range(25):
            rng = np.random.default_rng(seed)
            fires = collapse_pattern("?" * 16, [0.5] * 16, True, rng.random)
            assert sum(fires) == 8
            # And every consecutive pair holds exactly one hit — the
            # width-1 order-1 bag signature.
            for k in range(0, 16, 2):
                assert fires[k] != fires[k + 1]

    def test_coin_clumps(self):
        # The contrast that justifies `balanced`: coins wander.
        hits = set()
        for seed in range(25):
            rng = np.random.default_rng(seed)
            hits.add(sum(collapse_pattern("?" * 16, [0.5] * 16, False,
                                          rng.random)))
        assert len(hits) > 1

    def test_weighted_steps_keep_their_odds_under_balance(self):
        rng = np.random.default_rng(3)
        states = "?" * 8
        odds = [1.0, 0.0] + [0.5] * 6
        fires = collapse_pattern(states, odds, True, rng.random)
        assert fires[0] is True and fires[1] is False
        assert sum(fires[2:]) == 3  # six fair steps dealt exactly half

    def test_resolve_step_consumes_rng_only_on_real_choices(self):
        calls = []

        def rand():
            calls.append(1)
            return 0.25

        bag = [0, 0]
        resolve_step("?", 0.5, True, bag, rand)   # both eligible: one draw
        resolve_step("?", 0.5, True, bag, rand)   # forced laggard: no draw
        assert len(calls) == 1


class TestRendering:
    def test_first_pulse_plays_step_one_and_decided_pattern_is_exact(self):
        pattern = "1001100110011001"
        params = {f"step{i}_state": pattern[i - 1]
                  for i in range(1, MAX_STEPS + 1)}
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        (bar,) = _bar_hits(b, seq, clk, patch)
        assert bar == tuple(c == "1" for c in pattern)

    def test_gate_follows_the_clock_duty(self):
        params = {f"step{i}_state": "1" for i in range(1, MAX_STEPS + 1)}
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        clock = _pulses(4)
        out = b._render_possibility_seq(
            seq, len(clock), {(clk.id, "out"): clock}, patch)
        assert np.array_equal(out["gate"], clock)  # every step fires

    def test_loop_mode_matches_collapse_pattern_exactly(self):
        for seed in (1, 7, 42):
            params = _all_undecided({"seed": seed, "mode": "loop"})
            patch, seq, clk = _patch(params)
            b = _backend(patch)
            (bar,) = _bar_hits(b, seq, clk, patch)
            rng = np.random.default_rng(seed)
            assert bar == collapse_pattern("?" * 16, [0.5] * 16, False,
                                           rng.random)

    def test_loop_mode_redraws_each_bar(self):
        params = _all_undecided({"seed": 1})
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        bars = _bar_hits(b, seq, clk, patch, bars=6)
        assert len(set(bars)) > 1  # seeded; six identical bars would be 2^-60ish

    def test_latch_mode_holds_the_take(self):
        params = _all_undecided({"seed": 1, "mode": "latch"})
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        bars = _bar_hits(b, seq, clk, patch, bars=4)
        assert len(set(bars)) == 1

    def test_reroll_draws_a_fresh_take_in_latch_mode(self):
        params = _all_undecided({"seed": 2, "mode": "latch"})
        patch = Patch()
        seq = patch.add_module("possibility_seq", params=params)
        clk = patch.add_module("clock")
        rrl = patch.add_module("clock")
        patch.connect(clk.id, "out", seq.id, "clock")
        patch.connect(rrl.id, "out", seq.id, "reroll")
        b = _backend(patch)

        def bar(reroll_first):
            clock = _pulses(16)
            reroll = np.zeros_like(clock)
            if reroll_first:
                reroll[0:2] = 1.0  # rising edge before the first clock edge?
                # The clock's first edge is at sample 0 too; put the reroll
                # strictly before by offsetting the clock pulses.
            return clock, reroll

        # Bar 1 and 2: no reroll — identical takes.
        clock = _pulses(16, offset=4)
        silent = np.zeros_like(clock)
        buffers = {(clk.id, "out"): clock, (rrl.id, "out"): silent}
        one = b._render_possibility_seq(seq, len(clock), buffers, patch)
        two = b._render_possibility_seq(seq, len(clock), buffers, patch)
        assert np.array_equal(one["gate"], two["gate"])
        # Bar 3: a reroll edge lands before the bar's first clock edge.
        reroll = np.zeros_like(clock)
        reroll[0:2] = 1.0
        buffers = {(clk.id, "out"): clock, (rrl.id, "out"): reroll}
        three = b._render_possibility_seq(seq, len(clock), buffers, patch)
        assert not np.array_equal(one["gate"], three["gate"])

    def test_reset_rewinds_without_a_fresh_take(self):
        params = _all_undecided({"seed": 5, "mode": "latch"})
        patch = Patch()
        seq = patch.add_module("possibility_seq", params=params)
        clk = patch.add_module("clock")
        rst = patch.add_module("clock")
        patch.connect(clk.id, "out", seq.id, "clock")
        patch.connect(rst.id, "out", seq.id, "reset")
        b = _backend(patch)
        clock = _pulses(8, offset=4)
        silent = np.zeros_like(clock)
        first = b._render_possibility_seq(
            seq, len(clock), {(clk.id, "out"): clock,
                              (rst.id, "out"): silent}, patch)
        # Reset, then replay the same eight steps: same resolutions.
        reset = np.zeros_like(clock)
        reset[0:2] = 1.0
        again = b._render_possibility_seq(
            seq, len(clock), {(clk.id, "out"): clock,
                              (rst.id, "out"): reset}, patch)
        assert np.array_equal(first["gate"], again["gate"])

    def test_balanced_loop_pins_every_bar_to_its_share(self):
        params = _all_undecided({"seed": 9, "balanced": True})
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        for bar in _bar_hits(b, seq, clk, patch, bars=8):
            assert sum(bar) == 8

    def test_unbalanced_loop_wanders(self):
        params = _all_undecided({"seed": 9})
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        counts = {sum(bar) for bar in _bar_hits(b, seq, clk, patch, bars=8)}
        assert len(counts) > 1

    def test_extreme_odds_are_certainties(self):
        params = _all_undecided({"seed": 4})
        for i in range(1, 9):
            params[f"step{i}_p"] = 1.0
        for i in range(9, 17):
            params[f"step{i}_p"] = 0.0
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        for bar in _bar_hits(b, seq, clk, patch, bars=3):
            assert bar == (True,) * 8 + (False,) * 8

    def test_seed_determinism_and_divergence(self):
        takes = {}
        for seed in (1, 2):
            params = _all_undecided({"seed": seed})
            patch, seq, clk = _patch(params)
            b = _backend(patch)
            takes[seed] = _bar_hits(b, seq, clk, patch, bars=2)
        # Rebuild seed 1 from scratch: identical history.
        params = _all_undecided({"seed": 1})
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        assert _bar_hits(b, seq, clk, patch, bars=2) == takes[1]
        assert takes[1] != takes[2]

    def test_block_size_independence(self):
        params = _all_undecided({"seed": 11})
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        clock = _pulses(16)
        whole = b._render_possibility_seq(
            seq, len(clock), {(clk.id, "out"): clock}, patch)["gate"]

        patch2, seq2, clk2 = _patch(params)
        b2 = _backend(patch2)
        pieces = []
        for start in range(0, len(clock), 64):
            chunk = clock[start:start + 64]
            pieces.append(b2._render_possibility_seq(
                seq2, len(chunk), {(clk2.id, "out"): chunk}, patch2)["gate"])
        assert np.array_equal(whole, np.concatenate(pieces))

    def test_steps_param_shortens_the_loop(self):
        pattern = "11?0"
        params = {"steps": 4, "seed": 1}
        for i in range(1, MAX_STEPS + 1):
            params[f"step{i}_state"] = pattern[i - 1] if i <= 4 else "1"
        patch, seq, clk = _patch(params)
        b = _backend(patch)
        clock = _pulses(8)
        out = b._render_possibility_seq(
            seq, len(clock), {(clk.id, "out"): clock}, patch)
        fires = [bool(out["gate"][k * STEP] > 0.5) for k in range(8)]
        # Steps 5..8 replay the 4-step loop: decided steps repeat exactly.
        assert fires[0] and fires[1] and not fires[3]
        assert fires[4] and fires[5] and not fires[7]


# ----- the reroll divider example (2026-09-18) ---------------------------------

class TestRerollDividerExample:
    """possibility_reroll_divider.json: one sixteenth clock, two chained
    clock_dividers (divn 16 = a bar, then div4 = four bars), three latched
    patterns. Kick and snare hold a take for four bars and re-decide on
    the downbeat of bar 5; the hat re-deals every bar off the first
    divider. The example is seeded, so the takes are pinned."""

    @staticmethod
    def _render(seconds=17.0, block=512, sr=44100):
        from pathlib import Path

        from pysynthrack.io_patch import load_patch

        path = (Path(__file__).parent.parent / "examples"
                / "possibility_reroll_divider.json")
        patch = load_patch(path)
        b = NumpyBackend(sample_rate=sr, block_size=block)
        b.compile(patch)
        clk = next(m for m in patch if m.TYPE == "clock")
        seqs = {m.name.split(" ")[0]: m for m in patch
                if m.TYPE == "possibility_seq"}
        four = next(m for m in patch if m.TYPE == "clock_divider"
                    and any(c.src_port == "div4"
                            for c in patch.cables if c.src_module_id == m.id))
        cap: dict = {}
        orig_seq = b._render_possibility_seq
        orig_clk = b._render_clock
        orig_div = b._render_clock_divider

        def spy_seq(module, frames, buffers, patch_ref):
            r = orig_seq(module, frames, buffers, patch_ref)
            cap.setdefault(module.id, []).append(np.asarray(r["gate"]).copy())
            return r

        def spy_clk(module, frames):
            r = orig_clk(module, frames)
            cap.setdefault(module.id, []).append(np.asarray(r).copy())
            return r

        def spy_div(module, frames, buffers, patch_ref):
            r = orig_div(module, frames, buffers, patch_ref)
            cap.setdefault(module.id, []).append(np.asarray(r["div4"]).copy())
            return r

        b._render_possibility_seq = spy_seq
        b._render_clock = spy_clk
        b._render_clock_divider = spy_div
        for _ in range(int(sr * seconds / block)):
            out, _devices = b.render_block_multi(block)
            assert out is not None and np.all(np.isfinite(out))

        def edges(buf):
            e = np.flatnonzero((buf[1:] > 0.5) & (buf[:-1] <= 0.5)) + 1
            # The clock's first edge is sample 0 of the first block -- an
            # edge detector that only looks at pairs would miss step 1.
            return np.concatenate([[0], e]) if buf[0] > 0.5 else e

        clock_edges = edges(np.concatenate(cap[clk.id]))
        bars = {}
        for key, mod in seqs.items():
            g = np.concatenate(cap[mod.id])
            bars[key] = [
                tuple(bool(g[clock_edges[bar * 16 + k]] > 0.5) for k in range(16))
                for bar in range(len(clock_edges) // 16)
            ]
        reroll_edges = edges(np.concatenate(cap[four.id]))
        return clock_edges, reroll_edges, bars

    def test_the_four_bar_reroll_lands_on_the_downbeat(self):
        clock_edges, reroll_edges, bars = self._render()
        assert len(bars["KICK"]) >= 8
        # Every four-bar reroll edge IS a clock edge, and it is step 1 of
        # bar 1, 5, 9 ... (the divider fires on its edge 0, so the first
        # lands with the very first sixteenth).
        idx = np.searchsorted(clock_edges, reroll_edges)
        assert np.array_equal(clock_edges[idx], reroll_edges)
        assert list(idx[:3]) == [0, 64, 128][:len(idx)]
        assert len(idx) >= 2

    def test_kick_and_snare_hold_a_take_for_four_bars_then_change(self):
        _clock_edges, _reroll_edges, bars = self._render()
        for key in ("KICK", "SNARE"):
            first, second = bars[key][0:4], bars[key][4:8]
            assert len(set(first)) == 1, f"{key} wandered inside bars 1-4"
            assert len(set(second)) == 1, f"{key} wandered inside bars 5-8"
            assert first[0] != second[0], f"{key} did not re-decide at bar 5"
        # The decided steps never move: the floor and the backbeat.
        for bar in bars["KICK"][:8]:
            assert bar[0] and bar[4] and bar[8] and bar[12]
        for bar in bars["SNARE"][:8]:
            assert bar[4] and bar[12]

    def test_the_hat_re_deals_every_bar_and_always_gets_its_share(self):
        _clock_edges, _reroll_edges, bars = self._render()
        hats = bars["HAT"][:8]
        for bar in hats:
            assert sum(bar) == 8          # balanced: sixteen fair ?s, eight hits
        assert len(set(hats)) >= 6        # rerolled every bar, not every four
