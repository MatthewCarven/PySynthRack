"""Tests for the Clock module — tempo to a gate pulse train.

The transport (love pass, 2026-09-19) — ``reset``, ``run``, ``bpm_cv`` —
is pinned by MEASURED edge positions, never by eye: a reset is a fresh
clock from that sample, so the edges after a reset at E are exactly E +
the free-running clock's own edges; a run rise is the same; a held clock
emits nothing and its phase does not move. Bit-exactness at default (and
under a run cable that never falls + a reset cable that never rises) is
the recipe's pin.
"""
from __future__ import annotations

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
        }

    def test_ports(self):
        patch = Patch()
        c = patch.add_module("clock")
        assert [(p.name, p.signal_kind) for p in c.input_ports] == [
            ("reset", "gate"), ("run", "gate"), ("bpm_cv", "cv"),
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
        if port == "bpm_cv":
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
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("format"))
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


# ----- example -----------------------------------------------------------------------


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
