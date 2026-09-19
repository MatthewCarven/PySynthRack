"""Tests for FaderSeq — the Sequencer's fader-bank twin.

The engine is shared (the numpy backend routes both TYPEs through
``_render_sequencer``), so the load-bearing test here is the bit-identical
A/B: same params, same clock, same output. Behavioural depth (stepping,
rests, reset, wrap, the ``reverse`` gate) lives in test_sequencer.py and
applies verbatim. The port-contract test is the tripwire for a jack the
original grows and this class forgets: ``reverse`` (2026-09-20) is on
both lists, and ``test_reverse_gate_flips_the_fader_seq`` shows the
inherited jack actually reaches this TYPE's render.
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.fader_seq import FADER_RANGE_ST, FaderSeq
from pysynthrack.modules.sequencer import MAX_STEPS, Sequencer


def _backend(patch):
    b = NumpyBackend()
    b.compile(patch)
    return b


def _pulses(frames, positions, width=4):
    buf = np.zeros(frames, dtype=np.float32)
    for pos in positions:
        buf[pos:pos + width] = 1.0
    return buf


class TestFaderSeqModel:
    def test_register_and_type(self):
        patch = Patch()
        fs = patch.add_module("fader_seq")
        assert isinstance(fs, FaderSeq)
        assert fs.TYPE == "fader_seq"
        assert fs.CATEGORY == "Modulation"

    def test_param_contract_identical_to_sequencer(self):
        # One engine, one param shape — any drift here breaks the shared
        # renderer silently, so pin full equality.
        assert FaderSeq.DEFAULT_PARAMS == Sequencer.DEFAULT_PARAMS

    def test_port_contract_identical_to_sequencer(self):
        fs = [(p.name, p.direction, p.signal_kind) for p in FaderSeq.INPUT_PORTS + FaderSeq.OUTPUT_PORTS]
        sq = [(p.name, p.direction, p.signal_kind) for p in Sequencer.INPUT_PORTS + Sequencer.OUTPUT_PORTS]
        assert fs == sq
        # ...and the list is the full one: the reverse jack is here too.
        assert [p.name for p in FaderSeq.INPUT_PORTS] == ["clock", "reset", "reverse"]

    def test_default_scale_fits_fader_range(self):
        # The panel's faders span ±FADER_RANGE_ST; the factory C-major
        # scale must be reachable on them.
        fs = FaderSeq(module_id=1)
        for i in range(1, MAX_STEPS + 1):
            assert abs(float(fs.params[f"step{i}_pitch"])) <= FADER_RANGE_ST

    def test_serialization_round_trip(self):
        fs = FaderSeq(module_id=1, params={"step3_pitch": -7.0, "step3_on": False})
        clone = FaderSeq.from_dict(fs.to_dict())
        assert clone.params["step3_pitch"] == -7.0
        assert clone.params["step3_on"] is False


class TestFaderSeqEngine:
    def test_bit_identical_to_sequencer(self):
        # Same params, same clock -> byte-for-byte identical cv and gate.
        params = {
            "steps": 5,
            "step1_pitch": 0.0, "step2_pitch": 3.0, "step3_pitch": -7.0,
            "step4_pitch": 12.0, "step5_pitch": 5.0,
            "step3_on": False,
        }
        patch = Patch()
        sq = patch.add_module("sequencer", params=dict(params))
        fs = patch.add_module("fader_seq", params=dict(params))
        clk = patch.add_module("clock")
        patch.connect(clk.id, "out", sq.id, "clock")
        patch.connect(clk.id, "out", fs.id, "clock")
        b = _backend(patch)

        frames = 400
        clock = _pulses(frames, [0, 50, 100, 150, 200, 250, 300, 350])
        buffers = {(clk.id, "out"): clock}
        out_sq = b._render_module(sq, frames, dict(buffers), patch)
        out_fs = b._render_module(fs, frames, dict(buffers), patch)
        assert np.array_equal(out_sq["cv"], out_fs["cv"])
        assert np.array_equal(out_sq["gate"], out_fs["gate"])

    def test_independent_state_from_sibling_sequencer(self):
        # Shared renderer, per-module state: clocking one must not advance
        # the other.
        patch = Patch()
        fs = patch.add_module("fader_seq", params={"steps": 4})
        sq = patch.add_module("sequencer", params={"steps": 4})
        clk = patch.add_module("clock")
        patch.connect(clk.id, "out", fs.id, "clock")
        patch.connect(clk.id, "out", sq.id, "clock")
        b = _backend(patch)

        frames = 200
        pulses = _pulses(frames, [0, 60, 120])
        silent = np.zeros(frames, dtype=np.float32)
        out_fs = b._render_module(fs, frames, {(clk.id, "out"): pulses}, patch)
        out_sq = b._render_module(sq, frames, {(clk.id, "out"): silent}, patch)
        # fader_seq advanced to step 3 (pitch 4 st -> 4/12 V);
        # sequencer never clocked -> cv still 0, gate silent.
        assert out_fs["cv"][-1] == np.float32(4.0 / 12.0)
        assert np.all(out_sq["cv"] == 0.0)
        assert np.all(out_sq["gate"] == 0.0)

    def test_rest_and_reset_behave(self):
        # Light behavioural smoke through the fader_seq TYPE itself:
        # step 2 is a rest (gate low, still consumes the tick), reset
        # rewinds so the next clock plays step 1.
        patch = Patch()
        fs = patch.add_module(
            "fader_seq",
            params={"steps": 3, "step1_pitch": 0.0, "step2_pitch": 2.0,
                    "step3_pitch": 4.0, "step2_on": False},
        )
        clk = patch.add_module("clock")
        rst = patch.add_module("clock")
        patch.connect(clk.id, "out", fs.id, "clock")
        patch.connect(rst.id, "out", fs.id, "reset")
        b = _backend(patch)

        frames = 300
        clock = _pulses(frames, [0, 50, 100, 200, 250])
        reset = _pulses(frames, [150])
        out = b._render_module(
            fs, frames, {(clk.id, "out"): clock, (rst.id, "out"): reset}, patch
        )
        # Step 2 (samples 50..53): rest -> gate low while clock is high.
        assert np.all(out["gate"][50:54] == 0.0)
        # ...but the cv still moved to step 2's pitch.
        assert out["cv"][50] == np.float32(2.0 / 12.0)
        # After reset at 150, the clock at 200 plays step 1 again.
        assert out["cv"][200] == np.float32(0.0)
        assert np.all(out["gate"][200:204] == 1.0)

    def test_reverse_gate_flips_the_fader_seq(self):
        # The inherited jack on THIS type: a forward fader_seq with
        # reverse held high plays its steps descending, a flip mid-phrase
        # continues from the current step, and a reverse cable carrying
        # zeros is bit-exact with no cable at all.
        params = {"steps": 4, "direction": "forward"}
        for i in range(1, 5):
            params[f"step{i}_pitch"] = float(i)

        def build(with_reverse):
            patch = Patch()
            fs = patch.add_module("fader_seq", params=dict(params))
            clk = patch.add_module("clock")
            patch.connect(clk.id, "out", fs.id, "clock")
            rev = None
            if with_reverse:
                rev = patch.add_module("clock")
                patch.connect(rev.id, "out", fs.id, "reverse")
            return patch, fs, clk, rev

        frames = 200
        clock = _pulses(frames, [k * 20 for k in range(10)])

        def played(out):
            return [int(round(float(out["cv"][k * 20 + 1]) * 12)) for k in range(10)]

        patch, fs, clk, rev = build(True)
        b = _backend(patch)
        high = np.ones(frames, dtype=np.float32)
        out = b._render_module(fs, frames, {(clk.id, "out"): clock, (rev.id, "out"): high}, patch)
        assert played(out) == [4, 3, 2, 1, 4, 3, 2, 1, 4, 3]

        patch, fs, clk, rev = build(True)
        b = _backend(patch)
        flip = np.zeros(frames, dtype=np.float32)
        flip[90:150] = 1.0                     # edges 5, 6, 7 reversed
        out = b._render_module(fs, frames, {(clk.id, "out"): clock, (rev.id, "out"): flip}, patch)
        assert played(out) == [1, 2, 3, 4, 1, 4, 3, 2, 3, 4]

        patch_a, fs_a, clk_a, _ = build(False)
        patch_b, fs_b, clk_b, rev_b = build(True)
        out_a = _backend(patch_a)._render_module(fs_a, frames, {(clk_a.id, "out"): clock}, patch_a)
        out_b = _backend(patch_b)._render_module(
            fs_b, frames, {(clk_b.id, "out"): clock, (rev_b.id, "out"): np.zeros(frames, np.float32)}, patch_b)
        assert np.array_equal(out_a["cv"], out_b["cv"])
        assert np.array_equal(out_a["gate"], out_b["gate"])
