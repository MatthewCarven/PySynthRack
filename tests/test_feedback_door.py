"""The feedback door, generalized (2026-09-16).

Until now a feedback loop closed only through a ``matrix_mixer`` (or a
buffered sink's ``fill``): ``_compute_late_edges`` marked cables INTO a
matrix that would close a cycle as late-reads, and every other cycle
was SEVERED by the topological sort -- legal, compiling, rendering, and
inert. Two shipped examples were inert because of it: the krell's
``eoc -> trig`` (routed around with ``loop`` mode) and the May self-wah
(``envelope_follower_wah.json``), which had never wahed.

Now ``_compute_late_edges`` has a second pass: any remaining cable that
would close a cycle becomes a late-read too, walking the cables in
REVERSE order so the cable that closed the loop (drawn last) carries
the one block of latency and the feed-forward path stays instant.

Coverage:
  - Pass 1 unchanged: the existing matrix loop examples compile to
    exactly the late sets they had (the renders were checked
    bit-identical against pre-edit references in the session).
  - A non-matrix cycle marks exactly one cable, the last-drawn one; the
    forward chain still sorts source -> ... -> sink.
  - A self-loop marks itself; a buffered sink's fill cable is never
    marked (it is already a delayed edge in its own right).
  - The mixer-door staircase: a direct self-loop with a DC driver
    advances exactly one generation of the geometric series per block
    -- the twin of the matrix test, through the new door.
  - Two independent loops and a figure-eight all sort fully (Kahn's
    leftover tail is empty).
  - ``eoc -> trig`` fires forever; the period is cycle + block - 1 at
    512 AND 64 -- block-size dependence is the door's documented
    property, as it already was for the matrix.
  - The self-wah loop changes the render and stays bounded.
  - A runaway loop stays finite: non-finite stash values are scrubbed
    to zero and counted.
  - Live recompile: closing a loop while running keeps the graph sane.
  - Examples: krell_feedback.json self-plays through the real
    self-patch (many notes, breathing pace); envelope_follower_wah.json
    now sorts the follower AFTER the filter with its cable late.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.io_patch import load_patch

SR = 48000
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _backend(p, block=512):
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    return b


def _severed(p, b):
    """Modules Kahn cannot emit once delayed edges are ignored."""
    in_deg = {m: 0 for m in p.modules}
    for c in p.cables:
        if b._is_delayed_edge(p, c):
            continue
        in_deg[c.dst_module_id] += 1
    ready = [m for m, d in in_deg.items() if d == 0]
    order = []
    while ready:
        m = ready.pop(0)
        order.append(m)
        for c in p.cables_out_of(m):
            if b._is_delayed_edge(p, c):
                continue
            in_deg[c.dst_module_id] -= 1
            if in_deg[c.dst_module_id] == 0:
                ready.append(c.dst_module_id)
    return [m for m in p.modules if m not in order]


def _types(p, b):
    return [p.modules[m].TYPE for m in b._topo_order]


# ----- Pass 1 is untouched ------------------------------------------------------


class TestMatrixDoorUnchanged:
    @pytest.mark.parametrize("name,expect", [
        ("matrix_feedback_echo.json", 1),
        ("organ_feedback_drone.json", 1),
        ("organ_shimmer.json", 1),
    ])
    def test_matrix_examples_keep_their_single_late_edge_into_the_matrix(self, name, expect):
        p = load_patch(EXAMPLES / name)
        b = _backend(p)
        assert len(b._late_edges) == expect
        for (_s, _sp, dst, _dp) in b._late_edges:
            assert p.modules[dst].TYPE == "matrix_mixer"
        assert _severed(p, b) == []

    def test_governor_fill_cable_is_delayed_but_never_marked_late(self):
        p = load_patch(EXAMPLES / "ring_governor_monitor.json")
        b = _backend(p)
        fills = [c for c in p.cables if c.src_port == "fill"]
        assert fills
        for c in fills:
            assert b._is_delayed_edge(p, c)
            assert (c.src_module_id, c.src_port, c.dst_module_id, c.dst_port) not in b._late_edges
        assert b._late_edges == set()
        assert _severed(p, b) == []


# ----- Which cable goes late ----------------------------------------------------


class TestWhichCable:
    def test_last_drawn_cable_of_a_loop_goes_late_and_the_forward_chain_sorts(self):
        p = Patch()
        o = p.add_module("oscillator")
        mx = p.add_module("mixer")
        d = p.add_module("delay", params={"feedback": 0.0, "mix": 1.0})
        f = p.add_module("filter")
        s = p.add_module("speaker_output")
        p.connect(o.id, "out", mx.id, "in1")
        p.connect(mx.id, "out", d.id, "in")
        p.connect(d.id, "out", f.id, "in")
        p.connect(f.id, "out", s.id, "in")
        p.connect(f.id, "out", mx.id, "in2")            # the return, drawn last
        b = _backend(p)
        assert b._late_edges == {(f.id, "out", mx.id, "in2")}
        order = b._topo_order
        assert order.index(o.id) < order.index(mx.id) < order.index(d.id) < order.index(f.id) < order.index(s.id)
        assert _severed(p, b) == []

    def test_drawing_the_return_first_moves_the_block_to_the_last_drawn_cable(self):
        """Same loop, cables in the other order: the late cable follows
        the drawing order, not the topology -- documented, and the way
        to move the block of latency is to re-draw a different cable."""
        p = Patch()
        o = p.add_module("oscillator")
        mx = p.add_module("mixer")
        d = p.add_module("delay", params={"feedback": 0.0, "mix": 1.0})
        p.connect(d.id, "out", mx.id, "in2")            # return first
        p.connect(o.id, "out", mx.id, "in1")
        p.connect(mx.id, "out", d.id, "in")             # forward leg last
        b = _backend(p)
        assert b._late_edges == {(mx.id, "out", d.id, "in")}
        assert _severed(p, b) == []

    def test_a_self_loop_marks_itself(self):
        p = Patch()
        m = p.add_module("mixer")
        p.connect(m.id, "out", m.id, "in2")
        b = _backend(p)
        assert b._late_edges == {(m.id, "out", m.id, "in2")}
        assert b._topo_order == [m.id]

    def test_one_cable_per_cycle_two_loops_and_a_figure_eight_all_sort(self):
        p = Patch()
        a1 = p.add_module("mixer"); a2 = p.add_module("delay")
        b1 = p.add_module("mixer"); b2 = p.add_module("filter")
        hub = p.add_module("mixer")
        p.connect(a1.id, "out", a2.id, "in"); p.connect(a2.id, "out", a1.id, "in1")      # loop A
        p.connect(b1.id, "out", b2.id, "in"); p.connect(b2.id, "out", b1.id, "in1")      # loop B
        p.connect(hub.id, "out", a1.id, "in2"); p.connect(a2.id, "out", hub.id, "in1")   # eight
        p.connect(hub.id, "out", b1.id, "in2"); p.connect(b2.id, "out", hub.id, "in2")
        b = _backend(p)
        assert 2 <= len(b._late_edges) <= 4
        assert _severed(p, b) == []
        assert len(b._topo_order) == 5
        # and every module renders (no leftover-tail ordering surprises)
        out, _ = b.render_block_multi(256)

    def test_no_cycle_no_late_edges(self):
        p = Patch()
        o = p.add_module("oscillator"); d = p.add_module("delay"); s = p.add_module("speaker_output")
        p.connect(o.id, "out", d.id, "in"); p.connect(d.id, "out", s.id, "in")
        assert _backend(p)._late_edges == set()

    def test_the_late_set_survives_a_save_and_reload(self):
        from pysynthrack.io_patch import patch_from_json, patch_to_json
        p = Patch()
        o = p.add_module("oscillator"); mx = p.add_module("mixer"); d = p.add_module("delay")
        p.connect(o.id, "out", mx.id, "in1"); p.connect(mx.id, "out", d.id, "in"); p.connect(d.id, "out", mx.id, "in2")
        before = _backend(p)._late_edges
        after = _backend(patch_from_json(patch_to_json(p)))._late_edges
        assert before == after == {(d.id, "out", mx.id, "in2")}


# ----- Latency ---------------------------------------------------------------------


class TestLatency:
    def test_mixer_self_loop_is_one_generation_per_block_at_every_sample(self):
        """The matrix test's twin through the new door: a DC driver into
        a mixer whose output feeds its own second input at 0.5. Block k
        is the k-th partial sum of 0.4 * (1 + 0.5 + 0.25 ...), and it is
        that at EVERY sample of the block (the seed is the previous
        block, nothing older or fresher)."""
        p = Patch()
        m = p.add_module("mixer", params={"gain1": 1.0, "gain2": 0.5, "master": 1.0})
        k = p.add_module("constant", params={"value": 0.4})
        c2a = p.add_module("cv_to_audio")
        p.connect(k.id, "out", c2a.id, "cv")
        p.connect(c2a.id, "out", m.id, "in1")
        p.connect(m.id, "out", m.id, "in2")
        b = _backend(p, 256)
        expected = 0.0
        for _ in range(8):
            b.render_block_multi(256)
            blk = b._late_prev[(m.id, "out")]
            expected = 0.4 + 0.5 * expected
            assert np.allclose(blk, expected, atol=1e-6)
        assert abs(expected - 0.8) < 0.01

    def test_first_block_of_a_fresh_loop_reads_silence(self):
        p = Patch()
        m = p.add_module("mixer", params={"gain1": 1.0, "gain2": 0.5, "master": 1.0})
        k = p.add_module("constant", params={"value": 0.4})
        c2a = p.add_module("cv_to_audio")
        p.connect(k.id, "out", c2a.id, "cv"); p.connect(c2a.id, "out", m.id, "in1"); p.connect(m.id, "out", m.id, "in2")
        b = _backend(p, 256)
        b.render_block_multi(256)
        assert np.allclose(b._late_prev[(m.id, "out")], 0.4)       # dry only: the seed was zeros


def _krell_loop(block, rise=0.05, fall=0.10, seconds=4.0):
    """fg in trigger mode, eoc OR'd with a short starter back into trig."""
    p = Patch()
    fg = p.add_module("function_generator", params={"mode": "trigger", "rise": rise, "fall": fall, "curve": 0.0})
    clk = p.add_module("clock", params={"bpm": 1.0, "division": 1.0, "pulse_width": 0.0005})   # 30 ms, once a minute
    lg = p.add_module("logic")
    p.connect(clk.id, "out", lg.id, "a")
    p.connect(fg.id, "eoc", lg.id, "b")
    p.connect(lg.id, "or", fg.id, "trig")
    b = _backend(p, block)
    seen = []
    orig = b._render_function_generator

    def spy(module, frames, buffers, patch):
        r = orig(module, frames, buffers, patch)
        if module.id == fg.id:
            seen.append(np.asarray(r["eoc"]).copy())
        return r

    b._render_function_generator = spy
    for _ in range(int(seconds * SR / block)):
        b.render_block_multi(block)
    e = np.concatenate(seen)
    rises = np.flatnonzero((e[1:] > 0.5) & (e[:-1] <= 0.5)) + 1
    return b, p, fg, lg, rises


class TestKrell:
    @pytest.mark.parametrize("block", [512, 64])
    def test_eoc_into_trig_fires_forever_with_one_block_of_latency(self, block):
        b, p, fg, lg, rises = _krell_loop(block)
        assert b._late_edges == {(lg.id, "or", fg.id, "trig")}       # the cable that closed it
        cycle = round(0.05 * SR) + round(0.10 * SR)
        assert len(rises) > 20
        assert set(np.diff(rises).tolist()) == {cycle + block - 1}

    def test_the_starter_alone_does_not_make_a_krell(self):
        """Without the return cable the engine fires once per starter
        pulse -- the failure mode the door exists to end."""
        p = Patch()
        fg = p.add_module("function_generator", params={"mode": "trigger", "rise": 0.05, "fall": 0.10})
        clk = p.add_module("clock", params={"bpm": 1.0, "division": 1.0, "pulse_width": 0.0005})
        p.connect(clk.id, "out", fg.id, "trig")
        b = _backend(p, 512)
        seen = []
        orig = b._render_function_generator

        def spy(module, frames, buffers, patch):
            r = orig(module, frames, buffers, patch)
            seen.append(np.asarray(r["eoc"]).copy())
            return r

        b._render_function_generator = spy
        for _ in range(int(4 * SR / 512)):
            b.render_block_multi(512)
        e = np.concatenate(seen)
        assert int(((e[1:] > 0.5) & (e[:-1] <= 0.5)).sum()) == 1


# ----- The self-wah ------------------------------------------------------------------


def _wah(loop):
    p = Patch()
    o = p.add_module("oscillator", params={"waveform": "saw", "freq": 110.0, "amp": 0.4})
    f = p.add_module("filter", params={"mode": "lowpass", "cutoff": 320.0, "resonance": 2.2})
    e = p.add_module("audio_to_cv", params={"attack_ms": 6.0, "release_ms": 140.0, "gain": 0.9})
    s = p.add_module("speaker_output")
    p.connect(o.id, "out", f.id, "in")
    p.connect(f.id, "out", e.id, "in")
    p.connect(f.id, "out", s.id, "in")
    if loop:
        p.connect(e.id, "cv", f.id, "cutoff_cv")
    b = _backend(p)
    y = np.concatenate([np.asarray(b.render_block_multi(512)[0]) for _ in range(100)])
    return y, b, p, (e.id, "cv", f.id, "cutoff_cv")


class TestSelfWah:
    def test_the_loop_now_changes_the_render_and_stays_bounded(self):
        a, b, p, key = _wah(True)
        c, *_ = _wah(False)
        assert b._late_edges == {key}
        assert _types(p, b) == ["oscillator", "filter", "audio_to_cv", "speaker_output"]
        assert not np.array_equal(a, c)
        assert np.all(np.isfinite(a)) and np.abs(a).max() <= 1.0

    def test_the_shipped_example_sorts_the_follower_after_the_filter(self):
        p = load_patch(EXAMPLES / "envelope_follower_wah.json")
        b = _backend(p)
        f = next(m for m in p if m.TYPE == "filter")
        e = next(m for m in p if m.TYPE == "audio_to_cv")
        assert (e.id, "cv", f.id, "cutoff_cv") in b._late_edges
        assert b._topo_order.index(f.id) < b._topo_order.index(e.id)
        assert _severed(p, b) == []


# ----- Safety ------------------------------------------------------------------------


class TestRunaway:
    def test_a_gain_two_loop_stays_finite_and_is_counted(self):
        p = Patch()
        m = p.add_module("mixer", params={"gain1": 1.0, "gain2": 2.0, "master": 1.0})
        o = p.add_module("oscillator", params={"amp": 0.5})
        s = p.add_module("speaker_output")
        p.connect(o.id, "out", m.id, "in1"); p.connect(m.id, "out", m.id, "in2"); p.connect(m.id, "out", s.id, "in")
        b = _backend(p)
        assert b._late_nonfinite == 0
        with np.errstate(over="ignore", invalid="ignore"):
            for _ in range(400):
                out, _ = b.render_block_multi(512)
                assert np.all(np.isfinite(out))
        assert b._late_nonfinite > 0
        assert np.all(np.isfinite(b._late_prev[(m.id, "out")]))

    def test_a_tame_loop_never_trips_the_counter(self):
        p = Patch()
        m = p.add_module("mixer", params={"gain1": 1.0, "gain2": 0.5, "master": 1.0})
        o = p.add_module("oscillator", params={"amp": 0.5})
        p.connect(o.id, "out", m.id, "in1"); p.connect(m.id, "out", m.id, "in2")
        b = _backend(p)
        for _ in range(200):
            b.render_block_multi(512)
        assert b._late_nonfinite == 0


# ----- Live recompile ---------------------------------------------------------------


class TestLive:
    def test_closing_a_loop_while_running_recompiles_cleanly(self):
        p = Patch()
        o = p.add_module("oscillator"); mx = p.add_module("mixer"); d = p.add_module("delay", params={"mix": 1.0})
        s = p.add_module("speaker_output")
        p.connect(o.id, "out", mx.id, "in1"); p.connect(mx.id, "out", d.id, "in"); p.connect(d.id, "out", s.id, "in")
        b = _backend(p)
        for _ in range(5):
            b.render_block_multi(512)
        assert b._late_edges == set()
        p.connect(d.id, "out", mx.id, "in2")
        b.compile(p)
        assert b._late_edges == {(d.id, "out", mx.id, "in2")}
        for _ in range(5):
            out, _ = b.render_block_multi(512)
            assert np.all(np.isfinite(out))
        p.disconnect(d.id, "out", mx.id, "in2")
        b.compile(p)
        assert b._late_edges == set()
        out, _ = b.render_block_multi(512)
        assert np.all(np.isfinite(out))


# ----- The example -------------------------------------------------------------------


class TestKrellExample:
    def test_krell_feedback_example_self_plays_through_the_real_self_patch(self):
        p = load_patch(EXAMPLES / "krell_feedback.json")
        fg = next(m for m in p if m.TYPE == "function_generator")
        lg = next(m for m in p if m.TYPE == "logic")
        assert fg.params["mode"] == "trigger"
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b.compile(p)
        assert b._late_edges == {(lg.id, "or", fg.id, "trig")}
        assert _severed(p, b) == []
        seen = []
        orig = b._render_function_generator

        def spy(module, frames, buffers, patch):
            r = orig(module, frames, buffers, patch)
            if module.id == fg.id:
                seen.append(np.asarray(r["out"]).copy())
            return r

        b._render_function_generator = spy
        for _ in range(int(44100 * 20 / 256)):
            out, _ = b.render_block_multi(256)
            assert out is not None and np.all(np.isfinite(out))
        env = np.concatenate(seen)
        starts = np.flatnonzero((env[:-1] == 0.0) & (env[1:] > 0.0)) + 1
        assert len(starts) > 10, f"only {len(starts)} notes in 20 s"
        gaps = np.diff(starts) / 44100.0
        assert gaps.max() > gaps.min() * 1.3, "the pace never breathes"
