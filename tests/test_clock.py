"""Tests for the Clock module — tempo to a gate pulse train."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
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
        assert c.params == {"bpm": 120.0, "division": 4.0, "pulse_width": 0.5}

    def test_ports(self):
        patch = Patch()
        c = patch.add_module("clock")
        assert c.input_ports == []
        assert [(p.name, p.signal_kind) for p in c.output_ports] == [("out", "gate")]


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
