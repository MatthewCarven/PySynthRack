"""Tests for PossibilitySelector — the router whose routes can be undecided.

The possibility register one level up: each step is a rest, an output
(1..4), or an open set of outputs resolved per mode (loop / latch / dice);
`balanced` deals fair open steps from a least-used-output bag. The reference
implementation is `collapse_routes` in the module file; the renderer is
tested for exact equivalence against it.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.possibility_selector import (
    MAX_STEPS,
    N_OUTS,
    PossibilitySelector,
    collapse_routes,
    format_route,
    parse_route,
    resolve_route,
)

STEP = 20        # samples per pulse period in the hand-built gate trains
PULSE = 4        # samples each pulse stays high
FAIR = [1.0] * N_OUTS


def _pulses(count, step=STEP, width=PULSE, offset=0):
    buf = np.zeros(offset + count * step, dtype=np.float32)
    for k in range(count):
        buf[offset + k * step: offset + k * step + width] = 1.0
    return buf


def _patch(params=None, with_clock=False):
    """A selector fed on ``in`` by one clock; ``with_clock`` adds a second
    clock on the ``clock`` jack so the two stepping contracts can be told
    apart."""
    patch = Patch()
    sel = patch.add_module("possibility_selector", params=params or {})
    src = patch.add_module("clock")
    patch.connect(src.id, "out", sel.id, "in")
    clk = None
    if with_clock:
        clk = patch.add_module("clock")
        patch.connect(clk.id, "out", sel.id, "clock")
    return patch, sel, src, clk


def _backend(patch):
    b = NumpyBackend()
    b.compile(patch)
    return b


def _routes(out, count, step=STEP, offset=0):
    """Per-pulse route read off the four outputs (0 = nothing passed)."""
    routes = []
    for k in range(count):
        hit = [j for j in range(1, N_OUTS + 1)
               if out[f"out{j}"][offset + k * step] > 0.5]
        assert len(hit) <= 1, "two outputs high at once"
        routes.append(hit[0] if hit else 0)
    return tuple(routes)


def _bar_routes(backend, sel, src, patch, bars=1, steps=16):
    out_bars = []
    for _ in range(bars):
        gate = _pulses(steps)
        out = backend._render_possibility_selector(
            sel, len(gate), {(src.id, "out"): gate}, patch)
        out_bars.append(_routes(out, steps))
    return out_bars


def _all_open(extra=None):
    params = {f"step{i}_state": "?" for i in range(1, MAX_STEPS + 1)}
    params.update(extra or {})
    return params


def _states(pattern, extra=None):
    """``pattern`` is a list of state strings, one per step (padded with
    rests)."""
    params = {}
    for i in range(1, MAX_STEPS + 1):
        params[f"step{i}_state"] = pattern[i - 1] if i <= len(pattern) else "0"
    params.update(extra or {})
    return params


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        sel = patch.add_module("possibility_selector")
        assert isinstance(sel, PossibilitySelector)
        assert sel.TYPE == "possibility_selector"
        assert sel.CATEGORY == "Modulation"
        # steps + mode + balanced + seed, four weights, sixteen states.
        assert len(sel.params) == 4 + N_OUTS + MAX_STEPS
        assert sel.params["mode"] == "loop"
        assert sel.params["balanced"] is False
        assert all(sel.params[f"weight{k}"] == 1.0 for k in range(1, N_OUTS + 1))
        # The default register: eight open steps, 4^8 bars.
        states = [sel.params[f"step{i}_state"] for i in range(1, MAX_STEPS + 1)]
        assert states.count("?") == 8

    def test_ports(self):
        patch = Patch()
        sel = patch.add_module("possibility_selector")
        assert [(p.name, p.signal_kind) for p in sel.input_ports] == [
            ("in", "gate"), ("clock", "gate"), ("reset", "gate"), ("reroll", "gate"),
        ]
        assert [(p.name, p.signal_kind) for p in sel.output_ports] == [
            ("out1", "gate"), ("out2", "gate"), ("out3", "gate"), ("out4", "gate"),
        ]


class TestRouteStrings:
    def test_parse_route_reads_every_form(self):
        assert parse_route("0") == ()
        assert parse_route("") == ()
        assert parse_route("banana") == ()
        assert parse_route("3") == (3,)
        assert parse_route("?") == (1, 2, 3, 4)
        assert parse_route("13") == (1, 3)
        assert parse_route("31") == (1, 3)          # order is not identity
        assert parse_route("1133") == (1, 3)        # nor repetition
        assert parse_route("1234") == (1, 2, 3, 4)  # the long way to write ?
        assert parse_route("59") == ()              # no such outputs

    def test_format_route_is_canonical(self):
        assert format_route(()) == "0"
        assert format_route([2]) == "2"
        assert format_route([3, 1]) == "13"
        assert format_route([4, 3, 2, 1]) == "?"
        assert format_route([1, 1, 7]) == "1"       # junk outputs dropped
        # Round trip: every state the panel can write parses back to itself.
        for s in ("0", "1", "2", "3", "4", "?", "13", "24", "234"):
            assert format_route(parse_route(s)) == s


class TestReference:
    """The pure helpers, before any rendering is involved."""

    def test_decided_steps_are_decided(self):
        rng = np.random.default_rng(0)
        assert collapse_routes(["1", "0", "4", "2"], FAIR, False, rng.random) == (1, 0, 4, 2)

    def test_open_steps_draw_only_among_their_candidates(self):
        for seed in range(30):
            rng = np.random.default_rng(seed)
            take = collapse_routes(["13"] * 8 + ["24"] * 8, FAIR, False, rng.random)
            assert set(take[:8]) <= {1, 3}
            assert set(take[8:]) <= {2, 4}

    def test_weights_lean_the_draw(self):
        # weight2 = 0 takes the snare out of every ? ...
        for seed in range(30):
            rng = np.random.default_rng(seed)
            take = collapse_routes(["?"] * 16, [1.0, 0.0, 1.0, 1.0], False, rng.random)
            assert 2 not in take
        # ... but a decided "2" is untouched by its weight.
        rng = np.random.default_rng(1)
        assert collapse_routes(["2"], [1.0, 0.0, 1.0, 1.0], False, rng.random) == (2,)
        # And a heavy lean is heard: out4 at 0.9 against 0.1 x 3 wins most.
        counts = [0] * (N_OUTS + 1)
        for seed in range(50):
            rng = np.random.default_rng(seed)
            for r in collapse_routes(["?"] * 16, [0.1, 0.1, 0.1, 0.9], False, rng.random):
                counts[r] += 1
        assert counts[4] > 0.6 * 16 * 50

    def test_all_zero_weights_fall_back_to_a_fair_draw(self):
        seen = set()
        for seed in range(30):
            rng = np.random.default_rng(seed)
            seen |= set(collapse_routes(["?"] * 16, [0.0] * 4, False, rng.random))
        assert seen == {1, 2, 3, 4}

    def test_balanced_deals_every_output_its_share(self):
        # 16 fair ?s over four outputs: the bag guarantees four of each,
        # every seed, and every consecutive four holds one of each.
        for seed in range(25):
            rng = np.random.default_rng(seed)
            take = collapse_routes(["?"] * 16, FAIR, True, rng.random)
            assert sorted(take) == [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4
            for k in range(0, 16, 4):
                assert sorted(take[k:k + 4]) == [1, 2, 3, 4]

    def test_balanced_respects_a_subset(self):
        # Fair among {1, 3}: dealt least-used among those two only.
        for seed in range(20):
            rng = np.random.default_rng(seed)
            take = collapse_routes(["13"] * 8, FAIR, True, rng.random)
            assert sorted(take) == [1] * 4 + [3] * 4
            for k in range(0, 8, 2):
                assert sorted(take[k:k + 2]) == [1, 3]

    def test_coin_clumps(self):
        # The contrast that justifies `balanced`: a fair draw wanders.
        shares = set()
        for seed in range(25):
            rng = np.random.default_rng(seed)
            take = collapse_routes(["?"] * 16, FAIR, False, rng.random)
            shares.add(tuple(take.count(k) for k in range(1, N_OUTS + 1)))
        assert len(shares) > 1

    def test_weighted_steps_keep_their_lean_under_balance(self):
        # A leaning ? is a coin even with the bag on.
        for seed in range(30):
            rng = np.random.default_rng(seed)
            take = collapse_routes(["?"] * 16, [1.0, 0.0, 1.0, 1.0], True, rng.random)
            assert 2 not in take

    def test_resolve_route_consumes_rng_only_on_real_choices(self):
        calls = []

        def rand():
            calls.append(1)
            return 0.25

        bag = [0] * N_OUTS
        resolve_route("0", FAIR, True, bag, rand)      # rest: no draw
        resolve_route("3", FAIR, True, bag, rand)      # decided: no draw
        assert calls == []
        resolve_route("13", FAIR, True, bag, rand)     # both least-used: one draw
        assert len(calls) == 1
        resolve_route("13", FAIR, True, bag, rand)     # forced laggard: no draw
        assert len(calls) == 1


class TestRendering:
    def test_first_pulse_plays_step_one_and_decided_routes_are_exact(self):
        pattern = ["1", "3", "2", "3", "1", "3", "2", "0", "4", "3", "2", "3", "1", "0", "2", "4"]
        patch, sel, src, _ = _patch(_states(pattern))
        b = _backend(patch)
        (bar,) = _bar_routes(b, sel, src, patch)
        assert bar == tuple(int(s) for s in pattern)

    def test_one_output_high_at_a_time_and_the_gate_follows_the_input_duty(self):
        params = _states(["2"] * 4, {"steps": 4})
        patch, sel, src, _ = _patch(params)
        b = _backend(patch)
        gate = _pulses(4)
        out = b._render_possibility_selector(sel, len(gate), {(src.id, "out"): gate}, patch)
        assert np.array_equal(out["out2"], gate)          # the whole pulse passes
        for k in (1, 3, 4):
            assert not out[f"out{k}"].any()

    def test_nothing_passes_before_the_first_step(self):
        # A high input with no edge yet: idx is -1, so every output is quiet.
        patch, sel, src, clk = _patch(_states(["1"] * 4), with_clock=True)
        b = _backend(patch)
        high = np.ones(64, dtype=np.float32)
        quiet = np.zeros(64, dtype=np.float32)
        out = b._render_possibility_selector(
            sel, 64, {(src.id, "out"): high, (clk.id, "out"): quiet}, patch)
        assert not any(out[f"out{k}"].any() for k in range(1, N_OUTS + 1))

    def test_loop_mode_matches_collapse_routes_exactly(self):
        for seed in (1, 7, 42):
            patch, sel, src, _ = _patch(_all_open({"seed": seed}))
            b = _backend(patch)
            (bar,) = _bar_routes(b, sel, src, patch)
            rng = np.random.default_rng(seed)
            assert bar == collapse_routes(["?"] * 16, FAIR, False, rng.random)

    def test_balanced_loop_matches_the_reference_and_pins_every_share(self):
        for seed in (1, 7, 42):
            patch, sel, src, _ = _patch(_all_open({"seed": seed, "balanced": True}))
            b = _backend(patch)
            bars = _bar_routes(b, sel, src, patch, bars=3)
            rng = np.random.default_rng(seed)
            for bar in bars:
                # Each bar is one fresh deal from the same stream.
                assert bar == collapse_routes(["?"] * 16, FAIR, True, rng.random)
                assert sorted(bar) == [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4

    def test_loop_mode_redraws_each_bar(self):
        patch, sel, src, _ = _patch(_all_open({"seed": 1}))
        b = _backend(patch)
        assert len(set(_bar_routes(b, sel, src, patch, bars=6))) > 1

    def test_latch_mode_holds_the_take(self):
        patch, sel, src, _ = _patch(_all_open({"seed": 1, "mode": "latch"}))
        b = _backend(patch)
        assert len(set(_bar_routes(b, sel, src, patch, bars=4))) == 1

    def test_dice_mode_rolls_every_time_round(self):
        patch, sel, src, _ = _patch(_all_open({"seed": 3, "mode": "dice"}))
        b = _backend(patch)
        bars = _bar_routes(b, sel, src, patch, bars=6)
        assert len(set(bars)) > 1
        # And matches one long draw from the stream: dice never memoizes.
        rng = np.random.default_rng(3)
        flat = tuple(r for bar in bars for r in bar)
        assert flat == collapse_routes(["?"] * 96, FAIR, False, rng.random)

    def test_reroll_draws_a_fresh_take_in_latch_mode(self):
        params = _all_open({"seed": 2, "mode": "latch"})
        patch = Patch()
        sel = patch.add_module("possibility_selector", params=params)
        src = patch.add_module("clock")
        rrl = patch.add_module("clock")
        patch.connect(src.id, "out", sel.id, "in")
        patch.connect(rrl.id, "out", sel.id, "reroll")
        b = _backend(patch)
        gate = _pulses(16, offset=4)
        silent = np.zeros_like(gate)
        buffers = {(src.id, "out"): gate, (rrl.id, "out"): silent}
        one = _routes(b._render_possibility_selector(sel, len(gate), buffers, patch), 16, offset=4)
        two = _routes(b._render_possibility_selector(sel, len(gate), buffers, patch), 16, offset=4)
        assert one == two
        reroll = np.zeros_like(gate)
        reroll[0:2] = 1.0           # strictly before the bar's first edge
        buffers = {(src.id, "out"): gate, (rrl.id, "out"): reroll}
        three = _routes(b._render_possibility_selector(sel, len(gate), buffers, patch), 16, offset=4)
        assert one.count(0) == 0                 # the pulses were read, not the gaps
        assert three != one

    def test_reset_rewinds_without_a_fresh_take(self):
        params = _all_open({"seed": 5, "mode": "latch"})
        patch = Patch()
        sel = patch.add_module("possibility_selector", params=params)
        src = patch.add_module("clock")
        rst = patch.add_module("clock")
        patch.connect(src.id, "out", sel.id, "in")
        patch.connect(rst.id, "out", sel.id, "reset")
        b = _backend(patch)
        gate = _pulses(8, offset=4)
        silent = np.zeros_like(gate)
        first = _routes(b._render_possibility_selector(
            sel, len(gate), {(src.id, "out"): gate, (rst.id, "out"): silent}, patch), 8, offset=4)
        reset = np.zeros_like(gate)
        reset[0:2] = 1.0
        again = _routes(b._render_possibility_selector(
            sel, len(gate), {(src.id, "out"): gate, (rst.id, "out"): reset}, patch), 8, offset=4)
        assert first.count(0) == 0
        assert first == again

    def test_seed_determinism_and_divergence(self):
        takes = {}
        for seed in (1, 2):
            patch, sel, src, _ = _patch(_all_open({"seed": seed}))
            takes[seed] = _bar_routes(_backend(patch), sel, src, patch, bars=2)
        patch, sel, src, _ = _patch(_all_open({"seed": 1}))
        assert _bar_routes(_backend(patch), sel, src, patch, bars=2) == takes[1]
        assert takes[1] != takes[2]

    def test_block_size_independence(self):
        params = _all_open({"seed": 11})
        patch, sel, src, _ = _patch(params)
        b = _backend(patch)
        gate = _pulses(16)
        whole = b._render_possibility_selector(sel, len(gate), {(src.id, "out"): gate}, patch)
        patch2, sel2, src2, _ = _patch(params)
        b2 = _backend(patch2)
        pieces = {f"out{k}": [] for k in range(1, N_OUTS + 1)}
        for start in range(0, len(gate), 64):
            chunk = gate[start:start + 64]
            out = b2._render_possibility_selector(
                sel2, len(chunk), {(src2.id, "out"): chunk}, patch2)
            for k in pieces:
                pieces[k].append(out[k])
        for k in pieces:
            assert np.array_equal(whole[k], np.concatenate(pieces[k]))

    def test_steps_param_shortens_the_loop(self):
        params = _states(["1", "2", "3", "4"] + ["1"] * 12, {"steps": 4})
        patch, sel, src, _ = _patch(params)
        b = _backend(patch)
        gate = _pulses(8)
        out = b._render_possibility_selector(sel, len(gate), {(src.id, "out"): gate}, patch)
        assert _routes(out, 8) == (1, 2, 3, 4, 1, 2, 3, 4)

    def test_decided_steps_read_live_between_takes(self):
        # A panel edit to a decided step lands on the very next pass.
        params = _states(["1"] * 4, {"steps": 4})
        patch, sel, src, _ = _patch(params)
        b = _backend(patch)
        gate = _pulses(4)
        assert _routes(b._render_possibility_selector(
            sel, len(gate), {(src.id, "out"): gate}, patch), 4) == (1, 1, 1, 1)
        sel.params["step2_state"] = "4"
        assert _routes(b._render_possibility_selector(
            sel, len(gate), {(src.id, "out"): gate}, patch), 4) == (1, 4, 1, 1)


class TestSteppingContracts:
    def test_clock_unpatched_steps_on_the_inputs_own_edges(self):
        # Route k applies to the k-th hit, whenever it arrives: a sparse
        # gate (hits on pulses 0, 3, 4, 9) still walks 1, 2, 3, 4.
        params = _states(["1", "2", "3", "4"], {"steps": 4})
        patch, sel, src, _ = _patch(params)
        b = _backend(patch)
        gate = np.zeros(10 * STEP, dtype=np.float32)
        for k in (0, 3, 4, 9):
            gate[k * STEP: k * STEP + PULSE] = 1.0
        out = b._render_possibility_selector(sel, len(gate), {(src.id, "out"): gate}, patch)
        routes = _routes(out, 10)
        assert routes == (1, 0, 0, 2, 3, 0, 0, 0, 0, 4)

    def test_clock_patched_indexes_by_clock_tick_and_passes_the_input(self):
        # The sequencer reading: step k belongs to clock tick k; ``in`` only
        # says whether anything passes. Same sparse gate as above, now a
        # sixteenth clock on ``clock``: the hits land on steps 1, 4, 1, 2.
        params = _states(["1", "2", "3", "4"], {"steps": 4})
        patch, sel, src, clk = _patch(params, with_clock=True)
        b = _backend(patch)
        clock = _pulses(10)
        gate = np.zeros(10 * STEP, dtype=np.float32)
        for k in (0, 3, 4, 9):
            gate[k * STEP: k * STEP + PULSE] = 1.0
        out = b._render_possibility_selector(
            sel, len(gate), {(src.id, "out"): gate, (clk.id, "out"): clock}, patch)
        assert _routes(out, 10) == (1, 0, 0, 4, 1, 0, 0, 0, 0, 2)

    def test_clock_patched_and_the_input_held_high_switches_mid_gate(self):
        # A held input crosses clock ticks: the route moves with the step.
        params = _states(["1", "2"], {"steps": 2})
        patch, sel, src, clk = _patch(params, with_clock=True)
        b = _backend(patch)
        clock = _pulses(4)
        held = np.ones_like(clock)
        out = b._render_possibility_selector(
            sel, len(clock), {(src.id, "out"): held, (clk.id, "out"): clock}, patch)
        expect1 = np.zeros_like(clock)
        expect2 = np.zeros_like(clock)
        expect1[0:STEP] = 1.0
        expect2[STEP:2 * STEP] = 1.0
        expect1[2 * STEP:3 * STEP] = 1.0
        expect2[3 * STEP:] = 1.0
        assert np.array_equal(out["out1"], expect1)
        assert np.array_equal(out["out2"], expect2)

    def test_in_unpatched_routes_the_clock_itself(self):
        params = _states(["3", "1"], {"steps": 2})
        patch = Patch()
        sel = patch.add_module("possibility_selector", params=params)
        clk = patch.add_module("clock")
        patch.connect(clk.id, "out", sel.id, "clock")
        b = _backend(patch)
        clock = _pulses(4)
        out = b._render_possibility_selector(sel, len(clock), {(clk.id, "out"): clock}, patch)
        assert _routes(out, 4) == (3, 1, 3, 1)
        assert np.array_equal(out["out3"] + out["out1"], clock)

    def test_nothing_patched_is_silent(self):
        patch = Patch()
        sel = patch.add_module("possibility_selector")
        b = _backend(patch)
        out = b._render_possibility_selector(sel, 64, {}, patch)
        for k in range(1, N_OUTS + 1):
            assert out[f"out{k}"].shape == (64,)
            assert not out[f"out{k}"].any()


class TestWhetherTimesWhich:
    def test_a_possibility_seq_gate_through_the_selector_is_whether_times_which(self):
        """The kit patch the module exists for: the sequencer decides IF a
        sixteenth hits, the selector decides WHERE it goes. Rendered
        through the real graph at two block sizes; the sum of the four
        outputs is exactly the sequencer's gate, and every hit lands on
        the route the selector's own take chose."""
        seq_states = {f"step{i}_state": "1" if i % 2 else "?" for i in range(1, 17)}
        seq_states["seed"] = 5
        sel_states = _states(["1", "?", "3", "?", "2", "?", "3", "34"] * 2, {"seed": 2})
        renders = {}
        for block in (512, 64):
            patch = Patch()
            clk = patch.add_module("clock", params={"bpm": 240.0, "division": 4.0, "pulse_width": 0.2})
            seq = patch.add_module("possibility_seq", params=seq_states)
            sel = patch.add_module("possibility_selector", params=sel_states)
            patch.connect(clk.id, "out", seq.id, "clock")
            patch.connect(clk.id, "out", sel.id, "clock")
            patch.connect(seq.id, "gate", sel.id, "in")
            b = NumpyBackend(sample_rate=48000, block_size=block)
            b.compile(patch)
            got = {"gate": [], "outs": []}
            orig_seq, orig_sel = b._render_possibility_seq, b._render_possibility_selector

            def spy_seq(module, frames, buffers, p, _o=orig_seq, _g=got):
                r = _o(module, frames, buffers, p)
                _g["gate"].append(np.asarray(r["gate"]).copy())
                return r

            def spy_sel(module, frames, buffers, p, _o=orig_sel, _g=got):
                r = _o(module, frames, buffers, p)
                _g["outs"].append(np.stack([r[f"out{k}"] for k in range(1, 5)]))
                return r

            b._render_possibility_seq = spy_seq
            b._render_possibility_selector = spy_sel
            for _ in range(191488 // block):     # 374 x 512 == 2992 x 64 (4 s)
                b.render_block_multi(block)
            gate = np.concatenate(got["gate"])
            outs = np.concatenate(got["outs"], axis=1)
            assert np.array_equal(outs.sum(axis=0), gate)      # whether
            assert outs.max(axis=0).max() <= 1.0                  # one place at a time
            # The route of every hit, in order. (Compared as a sequence
            # rather than sample-for-sample: the clock's float phase
            # accumulator lands an edge a sample apart between block
            # sizes now and then -- the clock's property, not this
            # module's, whose own block-size pin uses hand-built pulses.)
            rises = np.flatnonzero((gate[1:] > 0.5) & (gate[:-1] <= 0.5)) + 1
            if gate[0] > 0.5:
                rises = np.concatenate([[0], rises])
            renders[block] = tuple(int(outs[:, t].argmax()) + 1 for t in rises)
        assert len(renders[512]) >= 32        # 64 sixteenths, half decided hits
        assert renders[512] == renders[64]                        # which, block-free
        # Something actually got routed everywhere the register allows.
        assert set(renders[512]) == {1, 2, 3, 4}


# ----- the example (2026-09-18) ------------------------------------------------

class TestKitExample:
    """possibility_selector_kit.json: a WHETHER x WHICH drum kit (the
    sequencer's gate through a clocked selector) and a four-string harp
    whose selector has no clock and steps on the hits themselves."""

    @staticmethod
    def _render(seconds=8.0, block=512, sr=44100):
        from pathlib import Path

        from pysynthrack.io_patch import load_patch

        path = Path(__file__).parent.parent / "examples" / "possibility_selector_kit.json"
        patch = load_patch(path)
        b = NumpyBackend(sample_rate=sr, block_size=block)
        b.compile(patch)
        drums = next(m for m in patch if m.TYPE == "possibility_selector"
                     and m.name.startswith("WHICH drum"))
        harp = next(m for m in patch if m.TYPE == "possibility_selector"
                    and m.name.startswith("WHICH string"))
        whether = next(m for m in patch if m.TYPE == "possibility_seq"
                       and m.name.startswith("WHETHER"))
        assert any(c.src_module_id == whether.id and c.dst_module_id == drums.id
                   and c.dst_port == "in" for c in patch.cables)
        assert not any(c.dst_module_id == harp.id and c.dst_port == "clock"
                       for c in patch.cables)          # the harp steps on hits
        cap: dict = {}
        orig_sel = b._render_possibility_selector
        orig_seq = b._render_possibility_seq

        def spy_sel(module, frames, buffers, p):
            r = orig_sel(module, frames, buffers, p)
            cap.setdefault(module.id, []).append(
                np.stack([r[f"out{k}"] for k in range(1, N_OUTS + 1)]))
            return r

        def spy_seq(module, frames, buffers, p):
            r = orig_seq(module, frames, buffers, p)
            cap.setdefault(module.id, []).append(np.asarray(r["gate"]).copy())
            return r

        b._render_possibility_selector = spy_sel
        b._render_possibility_seq = spy_seq
        peak = 0.0
        for _ in range(int(sr * seconds / block)):
            out, _devices = b.render_block_multi(block)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        outs = {mid: np.concatenate(v, axis=1) for mid, v in cap.items()
                if v[0].ndim == 2}
        gates = {mid: np.concatenate(v) for mid, v in cap.items() if v[0].ndim == 1}
        return peak, outs[drums.id], gates[whether.id], outs[harp.id]

    @staticmethod
    def _route_sequence(outs):
        gate = outs.sum(axis=0)
        rises = np.flatnonzero((gate[1:] > 0.5) & (gate[:-1] <= 0.5)) + 1
        if gate[0] > 0.5:
            rises = np.concatenate([[0], rises])
        return [int(outs[:, t].argmax()) + 1 for t in rises]

    def test_the_kit_is_whether_times_which(self):
        peak, drums, whether, _harp = self._render()
        assert 0.2 < peak < 1.0                              # headroom
        assert np.array_equal(drums.sum(axis=0), whether)    # every hit, one place
        routes = self._route_sequence(drums)
        assert len(routes) >= 40
        assert set(routes) == {1, 2, 3, 4}                   # every drum got played

    def test_the_harp_walks_its_register_on_the_hits(self):
        _peak, _drums, _whether, harp = self._render()
        routes = self._route_sequence(harp)
        assert len(routes) >= 16
        for start in range(0, len(routes) - 8 + 1, 8):
            bar = routes[start:start + 8]
            assert bar[:4] == [1, 2, 3, 4] and bar[5] == 3 and bar[7] == 1
            assert bar[4] != bar[6]         # balanced: the second ? avoids the first's string
