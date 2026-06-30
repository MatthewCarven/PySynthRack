"""Tests for the Sequencer module — clock-driven step sequencer."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.sequencer import MAX_STEPS, Sequencer

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


class TestSequencerModel:
    def test_register_and_defaults(self):
        patch = Patch()
        seq = patch.add_module("sequencer")
        assert isinstance(seq, Sequencer)
        assert seq.TYPE == "sequencer"
        assert seq.params["steps"] == 8
        # 1 (steps) + 16 pitch + 16 on
        assert len(seq.params) == 1 + 2 * MAX_STEPS
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


class TestSequencerIntegration:
    def test_clock_drives_sequencer_end_to_end(self):
        # clock -> sequencer; render real blocks and confirm the cv steps
        # through more than one distinct value as the clock ticks.
        patch, seq, clk = _seq_patch({"steps": 4})
        # fast clock so several steps land inside a handful of blocks
        clk.set_param("bpm", 600.0)
        clk.set_param("division", 4.0)
        b = _backend(patch)
        seen = set()
        for _ in range(20):
            buffers = {}
            clk_out = b._render_clock(clk, 1024)
            buffers[(clk.id, "out")] = clk_out
            out = b._render_sequencer(seq, 1024, buffers, patch)
            seen.update(np.round(out["cv"], 4).tolist())
        assert len(seen) >= 3  # cycled through multiple step pitches

    def test_full_voice_makes_sound(self):
        # clock -> seq.cv -> osc.freq_cv (C4 base); seq.gate -> adsr -> vca -> speaker
        patch = Patch()
        clk = patch.add_module("clock", params={"bpm": 480.0, "division": 4.0})
        seq = patch.add_module("sequencer", params={"steps": 4})
        osc = patch.add_module("oscillator", params={"freq": 261.6256, "amp": 0.5})
        env = patch.add_module("adsr", params={"attack": 0.002, "decay": 0.05,
                                               "sustain": 0.6, "release": 0.05})
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        patch.connect(clk.id, "out", seq.id, "clock")
        patch.connect(seq.id, "cv", osc.id, "freq_cv")
        patch.connect(osc.id, "out", vca.id, "audio")
        patch.connect(seq.id, "gate", env.id, "gate")
        patch.connect(env.id, "cv", vca.id, "cv")
        patch.connect(vca.id, "out", spk.id, "in")
        b = _backend(patch)
        peak = 0.0
        for _ in range(40):
            peak = max(peak, float(np.max(np.abs(b.render_block(512)))))
        assert peak > 0.05  # the patch plays itself
