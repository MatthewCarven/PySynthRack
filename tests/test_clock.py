"""Tests for the Clock module — tempo to a gate pulse train.

The transport (love pass, 2026-09-19) — ``reset``, ``run``, ``bpm_cv`` —
is pinned by MEASURED edge positions, never by eye: a reset is a fresh
clock from that sample, so the edges after a reset at E are exactly E +
the free-running clock's own edges; a run rise is the same; a held clock
emits nothing and its phase does not move. Bit-exactness at default (and
under a run cable that never falls + a reset cable that never rises) is
the recipe's pin.

``swing`` (love pass, 2026-09-20) is pinned the same way: the odd edges
land ``round(swing * period)`` samples after the straight clock's, the
even ones ON them (and the even periods are bit-identical), the widths
are the straight clock's, a reset restarts the parity, the ceiling never
swallows a downbeat, and the default is the straight clock -- the recipe
(18 reference renders) plus a digest of the free-running render.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type
from pysynthrack.modules.clock import Clock

SR = 44100


def _backend(patch):
    b = NumpyBackend()
    b.compile(patch)
    return b


def _render_concat(backend, module, frames, blocks):
    return np.concatenate([backend._render_clock(module, frames) for _ in range(blocks)])


class TestClockModel:
    def test_register_and_defaults(self):
        patch = Patch()
        c = patch.add_module("clock")
        assert isinstance(c, Clock)
        assert c.TYPE == "clock"
        assert c.params == {
            "bpm": 120.0, "division": 4.0, "pulse_width": 0.5, "bpm_cv_depth": 1.0,
            "swing": 0.0, "swing_cv_depth": 0.5,
        }

    def test_ports(self):
        patch = Patch()
        c = patch.add_module("clock")
        assert [(p.name, p.signal_kind) for p in c.input_ports] == [
            ("reset", "gate"), ("run", "gate"), ("bpm_cv", "cv"),
            ("swing_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in c.output_ports] == [("out", "gate")]

    def test_pre_love_pass_patch_loads_with_the_default_depth(self):
        # A patch saved before bpm_cv_depth existed gets the default.
        patch = Patch()
        c = patch.add_module("clock")
        d = patch.to_dict()
        for m in d["modules"]:
            m["params"].pop("bpm_cv_depth", None)
        loaded = Patch.from_dict(d)
        assert loaded.modules[c.id].params["bpm_cv_depth"] == 1.0

    def test_pre_swing_patch_loads_straight(self):
        patch = Patch()
        c = patch.add_module("clock")
        d = patch.to_dict()
        for m in d["modules"]:
            m["params"].pop("swing", None)
        loaded = Patch.from_dict(d)
        assert loaded.modules[c.id].params["swing"] == 0.0

    def test_pre_swing_cv_patch_loads_with_the_default_depth(self):
        # A patch saved before swing_cv_depth existed gets the default.
        patch = Patch()
        c = patch.add_module("clock")
        d = patch.to_dict()
        for m in d["modules"]:
            m["params"].pop("swing_cv_depth", None)
        loaded = Patch.from_dict(d)
        assert loaded.modules[c.id].params["swing_cv_depth"] == 0.5


class TestClockSignal:
    def test_gate_is_binary(self):
        patch = Patch()
        c = patch.add_module("clock")
        b = _backend(patch)
        out = b._render_clock(c, 1000)
        assert set(np.unique(out)).issubset({0.0, 1.0})

    def test_pulse_rate_matches_bpm_and_division(self):
        # 120 BPM x 4 pulses/beat = 8 Hz -> period SR/8 samples.
        patch = Patch()
        c = patch.add_module("clock", params={"bpm": 120.0, "division": 4.0})
        b = _backend(patch)
        buf = _render_concat(b, c, 4096, SR // 4096 + 2)[:SR]
        edges = np.where((buf[1:] > 0.5) & (buf[:-1] <= 0.5))[0]
        spacing = np.diff(edges)
        assert abs(np.median(spacing) - SR / 8.0) < 2.0  # ~5512.5 samples

    def test_division_changes_rate(self):
        patch = Patch()
        c = patch.add_module("clock", params={"bpm": 120.0, "division": 2.0})
        b = _backend(patch)
        buf = _render_concat(b, c, 4096, SR // 4096 + 2)[:SR]
        edges = np.where((buf[1:] > 0.5) & (buf[:-1] <= 0.5))[0]
        # 120 x 2 = 4 Hz -> ~11025 samples between edges.
        assert abs(np.median(np.diff(edges)) - SR / 4.0) < 2.0

    def test_pulse_width_sets_duty(self):
        patch = Patch()
        c = patch.add_module("clock", params={"pulse_width": 0.25})
        b = _backend(patch)
        buf = _render_concat(b, c, 4096, 20)
        assert abs(float(buf.mean()) - 0.25) < 0.01

    def test_phase_is_continuous_across_blocks(self):
        # Rendering in two halves must match rendering in one go (no seam).
        patch = Patch()
        c = patch.add_module("clock")
        b1 = _backend(patch)
        whole = b1._render_clock(c, 2048)
        patch2 = Patch()
        c2 = patch2.add_module("clock")
        b2 = _backend(patch2)
        halves = np.concatenate([b2._render_clock(c2, 1024), b2._render_clock(c2, 1024)])
        assert np.array_equal(whole, halves)

    def test_dispatch_returns_mono_buffer(self):
        patch = Patch()
        c = patch.add_module("clock")
        b = _backend(patch)
        out = b._render_module(c, 256, {}, patch)
        assert isinstance(out, np.ndarray)
        assert out.shape == (256,)


# ----- the transport -------------------------------------------------------------

N = 172 * 512  # ~2 s, a multiple of both block sizes under test


def _rising(g):
    g = np.asarray(g) > 0.5
    prev = np.concatenate([[False], g[:-1]])
    return np.flatnonzero(g & ~prev)


def _falling(g):
    g = np.asarray(g) > 0.5
    prev = np.concatenate([[False], g[:-1]])
    return np.flatnonzero(~g & prev)


def _transport(rows, params=None, block=512, total=N, dispatch=False):
    """Render a clock with full-length ``rows`` {port: array} on its jacks.

    The sources are real modules (so the cables are valid) but their
    buffers are injected block by block -- the same trick the LFO and
    filter cv_depth tests use. ``dispatch`` renders through
    ``_render_module`` (the buffers/patch path the graph walk takes).
    """
    p = Patch()
    clk = p.add_module("clock", params=params or {})
    keys = {}
    for port in rows:
        if port in ("bpm_cv", "swing_cv"):
            src = p.add_module("lfo")
            keys[port] = (src.id, "cv")
        else:
            src = p.add_module("clock")
            keys[port] = (src.id, "out")
        p.connect(*keys[port], clk.id, port)
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    out = []
    for i in range(0, total, block):
        bufs = {keys[port]: np.asarray(arr[i:i + block], dtype=np.float32)
                for port, arr in rows.items()}
        if dispatch:
            out.append(b._render_module(clk, block, bufs, p))
        else:
            out.append(b._render_clock(clk, block, bufs, p))
    return np.concatenate(out), b, clk


def _free_edges(total=N):
    y, _b, _c = _transport({}, total=total)
    return _rising(y)


class TestReset:
    def test_reset_is_a_fresh_clock_from_that_sample(self):
        # The free-running clock at 8 Hz: edges 0, 5512, 11024, 16537, ...
        free = _free_edges()
        assert free[:4].tolist() == [0, 5512, 11024, 16537]
        reset = np.zeros(N, np.float32)
        reset[3000:3100] = 1.0  # mid-block: 3000 = 5 * 512 + 440
        y, _b, _c = _transport({"reset": reset})
        assert y[2999] == 0.0 and y[3000] == 1.0  # high ON the reset sample
        edges = _rising(y)
        after = edges[edges >= 3000] - 3000
        expect = free[free < N - 3000]
        assert after.tolist() == expect.tolist()  # E + the fresh clock's edges

    def test_reset_while_high_extends_the_pulse_and_restarts_the_count(self):
        # At 8 Hz / pw 0.5 the first pulse is high 0..2755 and the second
        # rises at 5512 (high to 8267). A reset at 5600 lands inside it.
        reset = np.zeros(N, np.float32)
        reset[5600:5700] = 1.0
        y, _b, _c = _transport({"reset": reset})
        assert _rising(y)[:3].tolist() == [0, 5512, 5600 + 5512]  # no new rise at 5600
        assert _falling(y)[:2].tolist() == [2756, 5600 + 2756]  # the pulse extends

    def test_edge_straddling_a_block_boundary_counts_once(self):
        # Reset high from the last sample of block 0 through block 1: ONE
        # edge, at 511 (inside the first pulse, so it extends it), and the
        # count restarts from 511 -- a second edge at 512 would put the
        # next pulse at 6024, not 6023.
        reset = np.zeros(N, np.float32)
        reset[511:900] = 1.0
        y, _b, _c = _transport({"reset": reset})
        assert _rising(y)[:3].tolist() == [0, 511 + 5512, 511 + 11024]
        assert _falling(y)[0] == 511 + 2756

    def test_voice_gate_on_reset_collapses_any_voice_high(self):
        # A (V, F) gate (midi_input) on reset: only voice 3 rises, at
        # sample 3000 of a 4096-frame block (the first pulse fell at 2756,
        # so 3000 is a real rising edge on the output).
        p = Patch()
        clk = p.add_module("clock")
        midi = p.add_module("midi_input")
        p.connect(midi.id, "gate", clk.id, "reset")
        b = NumpyBackend(sample_rate=SR, block_size=4096)
        b.compile(p)
        g = np.zeros((16, 4096), np.float32)
        g[3, 3000:3100] = 1.0
        out = b._render_clock(clk, 4096, {(midi.id, "gate"): g}, p)
        assert _rising(out).tolist() == [0, 3000]


class TestRun:
    def test_held_is_low_and_a_rise_restarts_on_that_sample(self):
        free = _free_edges()
        run = np.zeros(N, np.float32)
        run[:10000] = 1.0
        run[20000:] = 1.0
        y, _b, _c = _transport({"run": run})
        assert not np.any(y[10000:20000] > 0.5)  # held: all low
        assert y[19999] == 0.0 and y[20000] == 1.0  # play starts on the rise
        edges = _rising(y)
        assert edges[edges < 10000].tolist() == [0, 5512]
        after = edges[edges >= 20000] - 20000
        assert after.tolist() == free[free < N - 20000].tolist()

    def test_held_phase_does_not_move(self):
        # A whole block held: the carried phase is untouched.
        p = Patch()
        clk = p.add_module("clock")
        src = p.add_module("clock")
        p.connect(src.id, "out", clk.id, "run")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        b._render_clock(clk, 512, {(src.id, "out"): np.ones(512, np.float32)}, p)
        phase = b._state[clk.id]["phase"]
        assert phase > 0.0
        for _ in range(3):
            out = b._render_clock(clk, 512, {(src.id, "out"): np.zeros(512, np.float32)}, p)
            assert not np.any(out > 0.5)
        assert b._state[clk.id]["phase"] == phase

    def test_reset_while_held_emits_nothing_until_run_rises(self):
        run = np.zeros(N // 2, np.float32)
        run[9000:] = 1.0
        reset = np.zeros(N // 2, np.float32)
        reset[4000:4100] = 1.0
        y, _b, _c = _transport({"run": run, "reset": reset}, total=N // 2)
        assert not np.any(y[:9000] > 0.5)
        assert _rising(y)[:3].tolist() == [9000, 9000 + 5512, 9000 + 11024]

    def test_run_never_falls_and_reset_never_rises_is_no_cables(self):
        # The recipe's pin, through the dispatcher: bit-exact.
        free, _b, _c = _transport({}, dispatch=True)
        rows = {"run": np.ones(N, np.float32), "reset": np.zeros(N, np.float32)}
        y, _b2, _c2 = _transport(rows, dispatch=True)
        assert np.array_equal(y, free)
        # ... and with the CV jack patched at depth 0 too.
        rows["bpm_cv"] = np.full(N, 0.7, np.float32)
        y0, _b3, _c3 = _transport(rows, params={"bpm_cv_depth": 0.0}, dispatch=True)
        assert np.array_equal(y0, free)


class TestBpmCv:
    def test_plus_one_at_depth_one_halves_the_period(self):
        y, _b, _c = _transport({"bpm_cv": np.ones(N, np.float32)})
        spacing = np.diff(_rising(y))
        assert set(spacing.tolist()) <= {2756, 2757}  # 16 Hz: 2756.25 samples

    def test_depth_two_quarters_it_and_minus_one_doubles_it(self):
        y, _b, _c = _transport({"bpm_cv": np.ones(N, np.float32)},
                               params={"bpm_cv_depth": 2.0})
        assert set(np.diff(_rising(y)).tolist()) <= {1378, 1379}
        y, _b, _c = _transport({"bpm_cv": -np.ones(N, np.float32)})
        assert set(np.diff(_rising(y)).tolist()) <= {11025}

    def test_absurd_cv_is_clipped_to_six_doublings_and_stays_finite(self):
        # 2 ** (1e6) would raise OverflowError; the clip pins x64 (86.1
        # samples at 8 Hz) and /64 (one edge in two seconds).
        y, _b, _c = _transport({"bpm_cv": np.full(N, 1e6, np.float32)})
        assert np.all(np.isfinite(y))
        assert set(np.diff(_rising(y)).tolist()) <= {86, 87}
        y, _b, _c = _transport({"bpm_cv": np.full(N, -1e6, np.float32)})
        assert np.all(np.isfinite(y))
        assert _rising(y).tolist() == [0]
        # A NaN mean reads as 0: the plain clock.
        y, _b, _c = _transport({"bpm_cv": np.full(N, np.nan, np.float32)})
        assert _rising(y)[:3].tolist() == [0, 5512, 11024]

    def test_voice_cv_is_averaged(self):
        # (V, F) on bpm_cv: voices 0 and 1 at +1 and -1 average to 0.
        p = Patch()
        clk = p.add_module("clock")
        midi = p.add_module("midi_input")
        p.connect(midi.id, "pitch_cv", clk.id, "bpm_cv")
        b = NumpyBackend(sample_rate=SR, block_size=4096)
        b.compile(p)
        cv = np.zeros((2, 4096), np.float32)
        cv[0] = 1.0
        cv[1] = -1.0
        out = np.concatenate([
            b._render_clock(clk, 4096, {(midi.id, "pitch_cv"): cv}, p) for _ in range(4)
        ])
        assert _rising(out)[:3].tolist() == [0, 5512, 11024]

    def test_depth_map_row_is_documented(self):
        md = (Path(__file__).resolve().parent.parent / "docs" / "MODULES.md").read_text(
            encoding="utf-8"
        )
        assert re.search(r"^\| `clock\.bpm_cv` \| `1\.0` \(`bpm_cv_depth`\)", md, re.M)


class TestBlockSize:
    _reset = None

    @staticmethod
    def _rows():
        reset = np.zeros(N, np.float32)
        reset[7001:7050] = 1.0
        reset[40003:40050] = 1.0
        run = np.ones(N, np.float32)
        run[20005:30011] = 0.0
        return {"reset": reset, "run": run}

    def test_edge_sequence_is_exact_across_block_sizes_at_the_default_tempo(self):
        # The 5512.5-sample period never crosses ON a sample, so 64 and
        # 512 agree to the sample -- measured, and pinned exact. Edges:
        # 0; 5512; the reset at 7001 (+5512 = 12513, 18025); the run
        # rise at 30011 (+5512 = 35523); the reset at 40003 (+5512).
        y512, _b, _c = _transport(self._rows(), block=512)
        y64, _b2, _c2 = _transport(self._rows(), block=64)
        assert np.array_equal(y512, y64)
        edges = _rising(y512)
        assert edges[:8].tolist() == [
            0, 5512, 12513, 18025, 30011, 35523, 40003, 45515,
        ]

    def test_integer_period_tempo_agrees_within_a_sample(self):
        # 120 BPM x 1 = a 22050-sample period: the crossing lands ON a
        # sample and the float accumulator's rounding decides which side,
        # so between block sizes an edge may sit one sample apart (the
        # house lesson). Same number of edges, each within +/-1.
        y512, _b, _c = _transport(self._rows(), params={"division": 1.0}, block=512)
        y64, _b2, _c2 = _transport(self._rows(), params={"division": 1.0}, block=64)
        e512, e64 = _rising(y512), _rising(y64)
        assert len(e512) == len(e64)
        assert np.abs(e512 - e64).max() <= 1
        # The restart edges themselves are exact on both.
        assert 30011 in e512 and 30011 in e64


# ----- swing ---------------------------------------------------------------------------

PERIOD = SR / 8.0  # 5512.5 samples at the default 120 x 4


def _widths(y):
    r, f = _rising(y), _falling(y)
    return (f - r[:len(f)]).tolist()


class TestSwing:
    def test_odd_edges_are_late_by_swing_of_the_period_and_even_edges_do_not_move(self):
        # The divider's convention: every SECOND pulse late by swing x
        # period -- at 8 Hz and 0.33 that is round(1819.125) = 1819
        # samples, measured exactly; the even edges are the straight
        # clock's own samples.
        free = _free_edges()
        y, _b, _c = _transport({}, params={"swing": 0.33})
        edges = _rising(y)
        assert len(edges) == len(free)
        late = edges - free
        assert np.all(late[0::2] == 0)
        assert set(late[1::2].tolist()) <= {1818, 1819, 1820}
        assert late[1::2].tolist().count(1819) == len(late[1::2])  # measured: exact
        assert edges[:6].tolist() == [0, 7331, 11024, 18356, 22049, 29381]

    def test_pulse_widths_are_the_straight_clocks(self):
        free, _b, _c = _transport({})
        y, _b2, _c2 = _transport({}, params={"swing": 0.33})
        ws, ww = _widths(free), _widths(y)
        assert len(ws) == len(ww)
        assert np.abs(np.array(ws) - np.array(ww)).max() <= 1
        assert ww[:6] == ws[:6] == [2756, 2756, 2757, 2756, 2757, 2756]  # measured: equal

    def test_even_periods_are_bit_identical_and_the_phase_is_untouched(self):
        # The swing is a phase OFFSET on odd periods: the accumulator and
        # every even period's samples are the straight clock's, bit for bit.
        free, bf, cf = _transport({})
        y, b, c = _transport({}, params={"swing": 0.33})
        fe = _rising(free)
        for k in range(len(fe) // 2 - 1):
            a, z = fe[2 * k], fe[2 * k + 1]
            assert np.array_equal(y[a:z], free[a:z]), k
        assert b._state[c.id]["phase"] == bf._state[cf.id]["phase"]
        assert b._state[c.id]["parity"] in (0, 1)

    def test_swing_is_a_fraction_of_the_current_period(self):
        # bpm_cv +1 at depth 1 = 16 Hz, a 2756.25-sample period: the odd
        # edges are round(0.33 * 2756.25) = 910 late (+/-1), not 1819.
        cv = np.ones(N, np.float32)
        free, _b, _c = _transport({"bpm_cv": cv})
        y, _b2, _c2 = _transport({"bpm_cv": cv}, params={"swing": 0.33})
        late = _rising(y) - _rising(free)
        assert np.all(late[0::2] == 0)
        assert set(late[1::2].tolist()) <= {909, 910, 911}

    def test_reset_restarts_the_parity_the_reset_pulse_is_even(self):
        # A reset at 6000 sits in period 1 (the odd one) BEFORE its late
        # pulse would rise at 7331: the gate goes high on the reset sample
        # (an even, straight pulse), the dropped odd pulse never comes,
        # and the count starts over -- 6000 + the swung clock's own edges.
        reset = np.zeros(N, np.float32)
        reset[6000:6100] = 1.0
        y, _b, _c = _transport({"reset": reset}, params={"swing": 0.33})
        assert y[5999] == 0.0 and y[6000] == 1.0
        assert _rising(y)[:6].tolist() == [
            0, 6000, 6000 + 7331, 6000 + 11024, 6000 + 18356, 6000 + 22049,
        ]

    def test_run_rise_restarts_the_parity_too(self):
        run = np.ones(N, np.float32)
        run[20005:30011] = 0.0
        y, _b, _c = _transport({"run": run}, params={"swing": 0.33})
        edges = _rising(y)
        assert edges[:4].tolist() == [0, 7331, 11024, 18356]
        assert not np.any(y[20005:30011] > 0.5)
        after = edges[edges >= 30011] - 30011
        assert after[:4].tolist() == [0, 7331, 11024, 18356]

    def test_the_ceiling_cuts_the_odd_pulse_a_sample_before_the_downbeat(self):
        # swing 0.5 + pw 0.5 = 1: the odd pulse would run straight into
        # the even one and the downbeat would never RISE. It is cut one
        # sample short instead: every straight edge is still an edge, a
        # low sample precedes every even edge, odd widths 2755 vs 2756/7.
        free = _free_edges()
        y, _b, _c = _transport({}, params={"swing": 0.5})
        edges = _rising(y)
        assert len(edges) == len(free)
        assert np.array_equal(edges[0::2], free[0::2])
        assert all(y[k - 1] == 0.0 for k in edges[2::2])
        assert _widths(y)[:8] == [2756, 2755, 2757, 2755, 2757, 2755, 2757, 2755]
        # Past the ceiling (pw 0.9) the odd pulse SHRINKS to the room it
        # has; the even one keeps its 0.9 and every downbeat still rises.
        y9, _b2, _c2 = _transport({}, params={"swing": 0.5, "pulse_width": 0.9})
        e9 = _rising(y9)
        assert len(e9) == len(free)
        assert np.array_equal(e9[0::2], free[0::2])
        assert _widths(y9)[:4] == [4961, 2755, 4962, 2755]

    def test_hand_edited_swing_clamps_at_the_dividers_ceiling_and_garbage_is_straight(self):
        from pysynthrack.modules.clockwork import DIVIDER_MAX_SWING

        assert NumpyBackend._CLOCK_MAX_SWING == DIVIDER_MAX_SWING == 0.75
        free, _b, _c = _transport({})
        y, _b2, _c2 = _transport({}, params={"swing": 0.9})
        late = _rising(y) - _rising(free)
        assert set(late[1::2].tolist()) <= {4133, 4134, 4135}  # round(0.75 * 5512.5)
        for bad in (float("nan"), "abc", None, -0.3):
            yb, _b3, _c3 = _transport({}, params={"swing": bad})
            assert np.array_equal(yb, free), bad

    def test_swing_zero_is_the_straight_clock_bit_exact_through_the_dispatcher(self):
        # The recipe's pin in durable form: the free-running render at
        # the default (512-sample blocks, N samples) hashed on the day
        # the swing shipped, when 18 reference renders (16 examples + 2
        # transport patches) captured BEFORE the edit compared bit-exact
        # AFTER it. A swing of 0 is the same code path, so this pins both.
        free, _b, _c = _transport({}, dispatch=True)
        assert hashlib.sha256(free.tobytes()).hexdigest() == (
            "40474ea156c99390d8b1dba80d61c55dba77dfb4980741af4e3e7d4a03a9a1fa"
        )
        y, _b2, _c2 = _transport({}, params={"swing": 0.0}, dispatch=True)
        assert np.array_equal(y, free)
        rows = {"run": np.ones(N, np.float32), "reset": np.zeros(N, np.float32)}
        y2, _b3, _c3 = _transport(rows, params={"swing": 0.0}, dispatch=True)
        assert np.array_equal(y2, free)

    def test_swung_edge_sequence_is_exact_across_block_sizes_at_the_default_tempo(self):
        # The transport rows (reset 7001, hold 20005..30011, reset 40003)
        # with swing 0.33: 64 and 512 agree to the sample. The reset at
        # 40003 lands INSIDE the late odd pulse (37342..40098) and extends
        # it -- the standing rule -- so the count restarts without a rise.
        rows = TestBlockSize._rows()
        y512, _b, _c = _transport(rows, params={"swing": 0.33}, block=512)
        y64, _b2, _c2 = _transport(rows, params={"swing": 0.33}, block=64)
        assert np.array_equal(y512, y64)
        assert _rising(y512)[:10].tolist() == [
            0, 7001, 14332, 18025, 30011, 37342, 47334, 51027, 58359, 62052,
        ]

    def test_integer_period_tempo_swung_agrees_within_a_sample(self):
        # Where the straight clock is +/-1 across block sizes (the
        # 22050-sample period crosses ON a sample) the swung one is too.
        rows = TestBlockSize._rows()
        p = {"swing": 0.33, "division": 1.0}
        y512, _b, _c = _transport(rows, params=p, block=512)
        y64, _b2, _c2 = _transport(rows, params=p, block=64)
        e512, e64 = _rising(y512), _rising(y64)
        assert len(e512) == len(e64)
        assert np.abs(e512 - e64).max() <= 1
        assert 30011 in e512 and 30011 in e64


# ----- swing_cv (love pass, 2026-09-22) --------------------------------------------

TEN = 861 * 512  # ~10 s, a whole number of 512- AND 64-sample blocks


class TestSwingCv:
    def test_a_constant_cv_is_the_knob_at_the_same_value(self):
        # swing 0.1 + cv 1.0 x depth 0.2 == the knob at 0.3, bit-exact:
        # the CV path is the same scalar, arrived at by addition.
        via = _transport({"swing_cv": np.ones(N, np.float32)},
                         params={"swing": 0.1, "swing_cv_depth": 0.2})[0]
        knob = _transport({}, params={"swing": 0.3})[0]
        assert np.array_equal(via, knob)

    def test_default_depth_is_a_half(self):
        explicit = _transport({"swing_cv": np.full(N, 0.6, np.float32)},
                              params={"swing_cv_depth": 0.5})[0]
        default = _transport({"swing_cv": np.full(N, 0.6, np.float32)})[0]
        assert np.array_equal(explicit, default)
        assert np.array_equal(default, _transport({}, params={"swing": 0.3})[0])

    def test_depth_zero_and_an_unpatched_jack_are_the_knob_only_clock(self):
        knob = _transport({}, params={"swing": 0.3})[0]
        off = _transport({"swing_cv": np.ones(N, np.float32)},
                         params={"swing": 0.3, "swing_cv_depth": 0.0})[0]
        assert np.array_equal(off, knob)

    def test_a_non_finite_mean_reads_as_no_modulation(self):
        straight = _transport({})[0]
        y = _transport({"swing_cv": np.full(N, np.nan, np.float32)})[0]
        assert np.array_equal(y, straight)

    def test_the_sum_is_clamped_to_the_dividers_ceiling_and_to_zero(self):
        top = _transport({"swing_cv": np.full(N, 10.0, np.float32)},
                         params={"swing": 0.5})[0]
        assert np.array_equal(top, _transport({}, params={"swing": 0.75})[0])
        floor = _transport({"swing_cv": np.full(N, -10.0, np.float32)},
                           params={"swing": 0.3})[0]
        assert np.array_equal(floor, _transport({})[0])

    def test_voice_cv_is_averaged(self):
        # (V, F) on swing_cv: voices at +1 and -1 average to 0, so the
        # knob stands alone -- the house mono rule.
        p = Patch()
        clk = p.add_module("clock", params={"swing": 0.3})
        midi = p.add_module("midi_input")
        p.connect(midi.id, "pitch_cv", clk.id, "swing_cv")
        b = NumpyBackend(sample_rate=SR, block_size=4096)
        b.compile(p)
        cv = np.zeros((2, 4096), np.float32)
        cv[0], cv[1] = 1.0, -1.0
        out = np.concatenate([
            b._render_clock(clk, 4096, {(midi.id, "pitch_cv"): cv}, p)
            for _ in range(4)
        ])
        knob = _transport({}, params={"swing": 0.3}, total=4 * 4096,
                          block=4096)[0]
        assert np.array_equal(out, knob)

    def test_a_moving_cv_emits_exactly_the_straight_clocks_edges(self):
        # The pin: a 0.5 Hz LFO on swing_cv over ~10 s must never double
        # an edge or lose a pulse, at any depth. 80 edges at 8 Hz.
        t = np.arange(TEN) / SR
        lfo = np.sin(2 * np.pi * 0.5 * t).astype(np.float32)
        straight = _transport({}, total=TEN)[0]
        n_straight = len(_rising(straight))
        assert n_straight == 80
        for depth in (0.25, 0.5, 1.0):
            y = _transport({"swing_cv": lfo},
                           params={"swing": 0.35, "swing_cv_depth": depth},
                           total=TEN)[0]
            assert len(_rising(y)) == n_straight, depth
        # ... and a unipolar breath from straight to triplet does too.
        uni = ((1 - np.cos(2 * np.pi * 0.05 * t)) / 2).astype(np.float32)
        y = _transport({"swing_cv": uni}, params={"swing_cv_depth": 0.33},
                       total=TEN)[0]
        assert len(_rising(y)) == n_straight

    def test_the_latch_holds_the_value_for_the_period_in_flight(self):
        # The rule, measured. The CV steps 0 -> 0.4 at sample 7000, well
        # inside the FIRST odd period (5512..11025) and after its pulse
        # has already risen. That pulse must not move, split or vanish:
        # it keeps the straight clock's 5512/8268 exactly. The change
        # lands at the next even period start (11025), so the NEXT odd
        # pulse (period 3, from 16537) is late by round(0.4 x 5512.5) =
        # 2205 samples -> 18742. The even edges never move at all.
        cv = np.zeros(N, np.float32)
        cv[7000:] = 0.4
        y = _transport({"swing_cv": cv}, params={"swing_cv_depth": 1.0})[0]
        straight = _transport({})[0]
        assert _rising(y)[:8].tolist() == [
            0, 5512, 11024, 18742, 22049, 29767, 33074, 40792,
        ]
        assert _falling(y)[:3].tolist() == _falling(straight)[:3].tolist()
        assert _rising(y)[:8][0::2].tolist() == _rising(straight)[:8][0::2].tolist()
        assert 18742 - 16537 == round(0.4 * PERIOD)

    def test_even_edges_are_block_exact_and_the_odd_ones_are_a_mean_apart(self):
        # The honest block-size story for a MOVING block-rate CV: the
        # even pulses do not depend on the swing at all, so they land on
        # the straight clock's own samples at 64 and 512 alike; the odd
        # ones ride a block MEAN, so they sit up to a millisecond apart
        # between block sizes -- with the same count, always.
        t = np.arange(TEN) / SR
        lfo = np.sin(2 * np.pi * 0.5 * t).astype(np.float32)
        rows = {"swing_cv": lfo}
        params = {"swing": 0.35}
        e512 = _rising(_transport(rows, params=params, block=512, total=TEN)[0])
        e64 = _rising(_transport(rows, params=params, block=64, total=TEN)[0])
        straight = _rising(_transport({}, total=TEN)[0])
        assert len(e512) == len(e64) == len(straight)
        assert np.array_equal(e512[0::2], straight[0::2])
        assert np.array_equal(e64[0::2], straight[0::2])
        odd = np.abs(e512[1::2] - e64[1::2])
        assert odd.max() <= 44 and odd.max() > 0      # ~1 ms, measured

    def test_a_reset_latches_the_current_value(self):
        # A restart edge is an even period start, so the value in force
        # from that sample is the block's -- the reset pulse is even and
        # straight, and the odd pulse after it uses the new swing.
        cv = np.zeros(N, np.float32)
        cv[7000:] = 0.4
        reset = np.zeros(N, np.float32)
        reset[9000:9050] = 1.0
        y = _transport({"swing_cv": cv, "reset": reset},
                       params={"swing_cv_depth": 1.0})[0]
        e = _rising(y)
        assert 9000 in e                       # the reset pulse: even, on time
        # the odd period after the reset starts at 9000 + 5512 and its
        # pulse is round(0.4 x 5512.5) = 2205 late.
        assert 9000 + 5512 + 2205 in e

    def test_depth_map_row_is_documented(self):
        md = (Path(__file__).resolve().parent.parent / "docs" / "MODULES.md").read_text(
            encoding="utf-8"
        )
        assert re.search(r"^\| `clock\.swing_cv` \| `0\.5` \(`swing_cv_depth`\)",
                         md, re.M)


# ----- widget sweep ----------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("clock")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (
                k, call.kwargs.get("format"), call.kwargs.get("max_value"),
            )
    return out


def test_every_param_gets_a_bounded_widget(monkeypatch):
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("clock").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["bpm"][1].endswith(" BPM")
    assert "dbl/unit" in w["bpm_cv_depth"][1]
    assert w["bpm_cv_depth"][1].isascii()
    # swing: a bounded slider to the hard shuffle (0.5), ASCII label.
    swing = [lb for lb in labels
             if lb.startswith("swing") and not lb.startswith("swing_cv")]
    assert swing and swing[0].isascii()
    assert w[swing[0]] == ("add_slider_float", "%.2f", 0.5)
    # swing_cv_depth: its own bounded drag, not the generic fallback.
    depth = [lb for lb in labels if lb.startswith("swing_cv_depth")]
    assert depth and depth[0].isascii()
    assert w[depth[0]] == ("add_drag_float", "%.2f", 1.0)
    assert "swing/unit" in depth[0]


# ----- examples ----------------------------------------------------------------------


def test_the_swing_example_swings_the_hats_and_keeps_the_kick_straight():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "clock_swing.json"
    patch = load_patch(path)
    assert len(patch.modules) <= 10
    clk = next(m for m in patch if m.TYPE == "clock")
    assert clk.params["swing"] == 0.3
    div = next(m for m in patch if m.TYPE == "clock_divider")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    clock_out, div4, div8 = [], [], []
    orig = b._render_clock
    orig_div = b._render_clock_divider

    def spy(module, frames, buffers=None, p=None):
        r = orig(module, frames, buffers, p)
        clock_out.append(np.asarray(r).copy())
        return r

    def spy_div(module, frames, buffers, p):
        r = orig_div(module, frames, buffers, p)
        div4.append(np.asarray(r["div4"]).copy())
        div8.append(np.asarray(r["div8"]).copy())
        return r

    b._render_clock = spy
    b._render_clock_divider = spy_div
    np.random.seed(5)  # the drums are seeded per hit; belt and braces
    out = []
    for _ in range(int(SR * 4 / 512)):
        y, _devices = b.render_block_multi(512)
        assert y is not None and np.all(np.isfinite(y))
        out.append(np.asarray(y).copy())
    y = np.concatenate(out, axis=0)
    peak = float(np.abs(y).max())
    assert 0.3 < peak < 0.8
    # The hat's trigger IS the swung train: 96 x 4 = 6.4 Hz, a 6890.625-
    # sample period, the offbeats round(0.3 * 6890.625) = 2067 late, so
    # the spacing alternates 8957 / 4824 (+/-1).
    edges = _rising(np.concatenate(clock_out))
    spacing = np.diff(edges)
    assert set(spacing[0::2].tolist()) <= {8957, 8958}
    assert set(spacing[1::2].tolist()) <= {4823, 4824}
    # The divider counts edges and edge parity is pulse parity: div4 fires
    # on edges 0, 4, 8, ... -- even pulses only, exactly on their samples
    # -- so the kick is dead straight under the swung hats; div8 likewise.
    assert np.array_equal(_rising(np.concatenate(div4)), edges[0::4])
    assert np.array_equal(_rising(np.concatenate(div8)), edges[0::8])
    assert div.params["swing"] == 0.0  # the divider adds none of its own


def test_the_transport_example_plays_two_bars_and_holds_one():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "clock_transport.json"
    patch = load_patch(path)
    assert len(patch.modules) <= 12
    clk = next(m for m in patch if m.TYPE == "clock" and m.params["division"] == 4.0)
    bar = next(m for m in patch if m.TYPE == "clock" and m.params["bpm"] == 120.0)
    seq = next(m for m in patch if m.TYPE == "sequencer")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    gates = {clk.id: [], bar.id: []}
    cv = []
    orig = b._render_clock
    orig_seq = b._render_sequencer

    def spy(module, frames, buffers=None, p=None):
        r = orig(module, frames, buffers, p)
        if module.id in gates:
            gates[module.id].append(np.asarray(r).copy())
        return r

    def spy_seq(module, frames, buffers, p):
        r = orig_seq(module, frames, buffers, p)
        cv.append(np.asarray(r["cv"]).copy())
        return r

    b._render_clock = spy
    b._render_sequencer = spy_seq
    np.random.seed(3)  # the pluck's burst is unseeded
    out = []
    for _ in range(int(SR * 8.5 / 512)):
        y, _devices = b.render_block_multi(512)
        assert y is not None and np.all(np.isfinite(y))
        out.append(np.asarray(y).copy())
    y = np.concatenate(out)
    peak = float(np.abs(y).max())
    assert 0.3 < peak < 0.8

    def rms(t0, t1):
        s = y[int(t0 * SR):int(t1 * SR)]
        return float(np.sqrt(np.mean(s ** 2)))

    # Two bars on, one bar held (the pluck's ring-down is < 1 s).
    assert rms(5.0, 5.9) < 0.02 * rms(0.5, 1.5)
    assert rms(6.5, 7.5) > 0.5 * rms(0.5, 1.5)
    sixteenths = _rising(np.concatenate(gates[clk.id]))
    downbeats = _rising(np.concatenate(gates[bar.id]))
    # No pulse while held (PLAY falls at 3.96 s, rises at 6 s) ...
    assert not np.any((sixteenths > int(3.96 * SR) + 1) & (sixteenths < 6 * SR))
    # ... play resumes ON the sample PLAY rises (6 s exactly) ...
    assert 6 * SR in sixteenths
    # ... and despite the drift every downbeat pulse lands ON the bar
    # clock's own edge, with the sequencer back on step 1 (0 semitones).
    seq_cv = np.concatenate(cv)
    for d in downbeats:
        if d >= len(seq_cv) - 8:
            break
        if int(3.96 * SR) < d < 6 * SR:
            continue  # the held bar: reset by decree, no pulse
        assert d in sixteenths, d
        assert abs(seq_cv[d + 5]) < 1e-6, (d, seq_cv[d + 5])
    # The tempo breathes but never overruns the bar: 16 pulses per bar.
    for k in (0, 1, 3):
        n = np.sum((sixteenths >= k * 2 * SR - 2) & (sixteenths < (k + 1) * 2 * SR - 2))
        assert n == 16, (k, n)


def test_the_breathe_example_sweeps_the_swing_and_keeps_the_divided_gates_steady():
    """``clock_swing_breathe.json``: swing_cv sweeping straight -> triplet
    -> straight over 20 s, with the divider's gates measured steady.

    Both halves of the 2026-09-22 love pass in one patch: the sweep is
    (a), the flat gate lengths under it are (b). The old last-interval
    rule is recomputed from the very same clock edges, so the before/
    after numbers come from one render.
    """
    from pysynthrack.io_patch import load_patch

    path = (Path(__file__).resolve().parent.parent / "examples"
            / "clock_swing_breathe.json")
    patch = load_patch(path)
    assert len(patch.modules) <= 12
    clk = next(m for m in patch if m.TYPE == "clock")
    div = next(m for m in patch if m.TYPE == "clock_divider")
    assert clk.params["swing"] == 0.0            # the jack does all of it
    assert clk.params["swing_cv_depth"] == 0.33  # 0..1 CV -> straight..triplet
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    clock_out = []
    rows = {k: [] for k in ("div4", "divn")}
    orig, orig_div = b._render_clock, b._render_clock_divider

    def spy(module, frames, buffers=None, p=None):
        r = orig(module, frames, buffers, p)
        if module.id == clk.id:
            clock_out.append(np.asarray(r).copy())
        return r

    def spy_div(module, frames, buffers, p):
        r = orig_div(module, frames, buffers, p)
        for k in rows:
            rows[k].append(np.asarray(r[k]).copy())
        return r

    b._render_clock, b._render_clock_divider = spy, spy_div
    np.random.seed(11)  # the drums draw their noise per hit
    out = []
    for _ in range(int(SR * 21.0 / 512)):
        y, _devices = b.render_block_multi(512)
        assert y is not None and np.all(np.isfinite(y))
        out.append(np.asarray(y).copy())
    y = np.concatenate(out, axis=0)
    assert 0.3 < float(np.abs(y).max()) < 0.8

    # (a) The breath: 100 BPM x 4 = 6.667 Hz, a 6615-sample period.
    # Straight at 0 s and 20 s (both gaps equal), fully shuffled at 10 s
    # (0.33 -> gaps of 1.33 and 0.67 periods), and never a lost or
    # doubled pulse: exactly the straight clock's edge count.
    clock_row = np.concatenate(clock_out)
    edges = _rising(clock_row)
    gaps = np.diff(edges)

    def gaps_at(t):
        i = min(int(np.searchsorted(edges, t * SR)), len(gaps) - 2)
        return int(gaps[i]), int(gaps[i + 1])

    for t in (0, 20):
        lo, hi = sorted(gaps_at(t))
        assert hi - lo <= 8, (t, lo, hi)            # straight
    lo, hi = sorted(gaps_at(10))
    assert abs(lo - round(0.67 * 6615)) < 60 and abs(hi - round(1.33 * 6615)) < 60
    straight = _transport({}, params={"bpm": 100.0, "division": 4.0,
                                      "pulse_width": 0.3},
                          total=len(clock_row), block=512)[0]
    assert len(edges) == len(_rising(straight))

    # (b) The divided gates. div4 lands on even pulses only, so the kick
    # is dead straight: constant spacing AND constant gate length. divn
    # at n=3 rides the shuffle but its length is now steady to a few
    # milliseconds -- against 148 ms under the old last-interval rule,
    # recomputed here from these same edges.
    def rise_len(row):
        g = np.asarray(row) > 0.5
        prev = np.concatenate([[False], g[:-1]])
        nxt = np.concatenate([g[1:], [False]])
        r = np.flatnonzero(g & ~prev)
        f = np.flatnonzero(g & ~nxt)
        return r, [int(q - p + 1) for p, q in zip(r, f)]

    r4, l4 = rise_len(np.concatenate(rows["div4"]))
    assert np.array_equal(r4, edges[::4][:len(r4)])       # edges, not lengths
    assert len(set(l4[2:])) == 1                           # dead steady
    assert len(set(np.diff(r4)[2:].tolist())) == 1
    rn, ln = rise_len(np.concatenate(rows["divn"]))
    assert np.array_equal(rn, edges[::3][:len(rn)])
    spread = max(ln[2:]) - min(ln[2:])
    assert spread < 0.005 * SR                             # < 5 ms, measured 155
    iv, pw, n_div = np.diff(edges), float(div.params["pw"]), int(div.params["n"])
    old = [round(pw * n_div * iv[i - 1]) for i in range(1, len(iv)) if i % n_div == 0]
    assert max(old) - min(old) > 20 * spread               # 6530 vs 155
