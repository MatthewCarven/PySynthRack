"""Tests for the Schmitt trigger module (cv → gate bridge).

Coverage:
  - Model: registration, defaults, ports/signal kinds, JSON round-trip,
    unknown param rejected, type walls (cv in legal, audio in illegal,
    gate out → ADSR.gate legal, gate out → Speaker.in illegal).
  - Mono behaviour: rising crossing sets the gate at the crossing
    sample; deadband holds; hysteresis survives wobble inside the
    band; falling crossing clears; strict thresholds (== high does not
    set); state carries across blocks; low > high degenerates to a
    comparator at high; unpatched input is constant low.
  - Voice-aware: (V, F) CV produces (V, F) gate with independent
    per-voice crossings and per-voice held state across blocks; mono
    CV preserves the 1D fast path.
  - Integration: LFO → Schmitt → ADSR retriggers an envelope once per
    LFO cycle (rising-edge count over a known duration).
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.patch import Cable
from pysynthrack.modules.schmitt import Schmitt


def _rising_edges(buf: np.ndarray) -> int:
    b = buf > 0.5
    return int(np.sum(b[1:] & ~b[:-1]) + int(b[0]))


# ----- Model -----------------------------------------------------------------


class TestSchmittModel:
    def test_register_and_defaults(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        assert isinstance(sch, Schmitt)
        assert sch.params == {"high": 0.6, "low": 0.4}

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        assert [p.name for p in sch.input_ports] == ["in"]
        assert sch.input_ports[0].signal_kind == "cv"
        assert [p.name for p in sch.output_ports] == ["gate"]
        assert sch.output_ports[0].signal_kind == "gate"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("schmitt", params={"high": 0.8, "low": 0.2})
        restored = Patch.from_dict(patch.to_dict())
        sch = next(m for m in restored if m.TYPE == "schmitt")
        assert sch.params["high"] == 0.8
        assert sch.params["low"] == 0.2

    def test_unknown_param_rejected(self):
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("schmitt", params={"threshold": 0.5})

    def test_cv_into_input_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        sch = patch.add_module("schmitt")
        patch.connect(lfo.id, "cv", sch.id, "in")  # must not raise

    def test_audio_into_input_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        sch = patch.add_module("schmitt")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", sch.id, "in")

    def test_gate_out_feeds_adsr_gate(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        adsr = patch.add_module("adsr")
        patch.connect(sch.id, "gate", adsr.id, "gate")  # must not raise

    def test_gate_out_into_speaker_rejected(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        spk = patch.add_module("speaker_output")
        with pytest.raises(ValueError):
            patch.connect(sch.id, "gate", spk.id, "in")


# ----- Mono behaviour --------------------------------------------------------


class TestSchmittMonoBehaviour:
    def _make(self, params=None):
        patch = Patch()
        sch = patch.add_module("schmitt", params=params or {})
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        patch.cables.append(Cable(77, "cv", sch.id, "in"))
        return patch, sch, backend

    def _render(self, backend, sch, patch, cv):
        cv = np.asarray(cv, dtype=np.float32)
        buffers = {(77, "cv"): cv}
        return backend._render_schmitt(sch, cv.shape[-1], buffers, patch)

    def test_rising_crossing_sets_gate_at_that_sample(self):
        patch, sch, backend = self._make()
        cv = np.concatenate([np.full(100, 0.0), np.full(100, 0.9)])
        gate = self._render(backend, sch, patch, cv)
        assert gate.shape == (200,)
        assert np.all(gate[:100] == 0.0)
        assert np.all(gate[100:] == 1.0)

    def test_deadband_holds_low_before_first_crossing(self):
        patch, sch, backend = self._make()
        gate = self._render(backend, sch, patch, np.full(256, 0.5))
        assert np.all(gate == 0.0)

    def test_hysteresis_survives_wobble_inside_band(self):
        patch, sch, backend = self._make()
        wobble = 0.5 + 0.05 * np.sin(np.linspace(0, 40 * np.pi, 400))
        cv = np.concatenate([np.full(50, 0.9), wobble.astype(np.float32)])
        gate = self._render(backend, sch, patch, cv)
        assert np.all(gate == 1.0)

    def test_falling_crossing_clears_gate(self):
        patch, sch, backend = self._make()
        cv = np.concatenate([np.full(50, 0.9), np.full(50, 0.5), np.full(50, 0.1)])
        gate = self._render(backend, sch, patch, cv)
        assert np.all(gate[:100] == 1.0)   # high then held through deadband
        assert np.all(gate[100:] == 0.0)

    def test_thresholds_are_strict(self):
        patch, sch, backend = self._make()
        gate = self._render(backend, sch, patch, np.full(128, 0.6))
        assert np.all(gate == 0.0)  # == high does not set
        gate = self._render(backend, sch, patch, np.full(128, 0.7))
        assert np.all(gate == 1.0)
        gate = self._render(backend, sch, patch, np.full(128, 0.4))
        assert np.all(gate == 1.0)  # == low does not clear

    def test_state_carries_across_blocks(self):
        patch, sch, backend = self._make()
        g1 = self._render(backend, sch, patch, np.full(64, 0.9))
        g2 = self._render(backend, sch, patch, np.full(64, 0.5))  # deadband only
        assert np.all(g1 == 1.0)
        assert np.all(g2 == 1.0)

    def test_inverted_pair_degenerates_to_comparator(self):
        patch, sch, backend = self._make({"high": 0.6, "low": 0.9})
        cv = np.concatenate([np.full(50, 0.7), np.full(50, 0.65), np.full(50, 0.55)])
        gate = self._render(backend, sch, patch, cv)
        assert np.all(gate[:100] == 1.0)   # 0.65 is not < min(0.9, 0.6)
        assert np.all(gate[100:] == 0.0)   # 0.55 is

    def test_unpatched_input_is_constant_low(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        gate = backend._render_schmitt(sch, 512, {}, patch)
        assert gate.shape == (512,)
        assert np.all(gate == 0.0)


# ----- Voice-aware -----------------------------------------------------------


class TestSchmittVoiceAware:
    def _make(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        patch.cables.append(Cable(77, "cv", sch.id, "in"))
        return patch, sch, backend

    def test_voice_rows_cross_independently(self):
        patch, sch, backend = self._make()
        row0 = np.concatenate([np.full(100, 0.0), np.full(100, 0.9)])
        row1 = np.concatenate([np.full(100, 0.9), np.full(100, 0.5)])
        cv = np.stack([row0, row1]).astype(np.float32)
        buffers = {(77, "cv"): cv}
        gate = backend._render_schmitt(sch, 200, buffers, patch)
        assert gate.shape == (2, 200)
        assert np.all(gate[0, :100] == 0.0) and np.all(gate[0, 100:] == 1.0)
        assert np.all(gate[1] == 1.0)  # row1 set early, deadband holds

    def test_per_voice_state_across_blocks(self):
        patch, sch, backend = self._make()
        b1 = np.stack([np.full(64, 0.9), np.full(64, 0.0)]).astype(np.float32)
        b2 = np.full((2, 64), 0.5, dtype=np.float32)  # deadband both rows
        g1 = backend._render_schmitt(sch, 64, {(77, "cv"): b1}, patch)
        g2 = backend._render_schmitt(sch, 64, {(77, "cv"): b2}, patch)
        assert np.all(g1[0] == 1.0) and np.all(g1[1] == 0.0)
        assert np.all(g2[0] == 1.0) and np.all(g2[1] == 0.0)

    def test_mono_cv_keeps_mono_output(self):
        patch, sch, backend = self._make()
        gate = backend._render_schmitt(
            sch, 64, {(77, "cv"): np.full(64, 0.9, dtype=np.float32)}, patch
        )
        assert gate.ndim == 1


# ----- Integration -----------------------------------------------------------


class TestSchmittIntegration:
    def test_lfo_clock_retriggers_adsr(self):
        """LFO (4 Hz, unipolar) → Schmitt → ADSR: the gate shows ~4
        rising edges per second and the ADSR's CV output rises after
        each one — a self-playing envelope clock."""
        sr = 44100
        patch = Patch()
        lfo = patch.add_module(
            "lfo", params={"waveform": "sine", "rate": 4.0, "depth": 1.0, "bipolar": False}
        )
        sch = patch.add_module("schmitt")
        adsr = patch.add_module(
            "adsr", params={"attack": 0.005, "decay": 0.05, "sustain": 0.3, "release": 0.05}
        )
        patch.connect(lfo.id, "cv", sch.id, "in")
        patch.connect(sch.id, "gate", adsr.id, "gate")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)

        gates, envs = [], []
        blocks = (2 * sr) // 512  # ~2 seconds
        for _ in range(blocks):
            buffers = {}
            for mid in backend._topo_order:
                m = patch.modules.get(mid)
                result = backend._render_module(m, 512, buffers, patch)
                if result is None:
                    continue
                if isinstance(result, dict):
                    for port, buf in result.items():
                        buffers[(mid, port)] = buf
                elif m.OUTPUT_PORTS:
                    buffers[(mid, m.OUTPUT_PORTS[0].name)] = result
            gates.append(buffers[(sch.id, "gate")])
            envs.append(buffers[(adsr.id, "cv")])

        gate = np.concatenate(gates)
        env = np.concatenate(envs)
        seconds = gate.size / sr
        edges = _rising_edges(gate)
        assert abs(edges / seconds - 4.0) <= 1.0  # ~LFO rate
        assert np.isfinite(env).all()
        assert env.max() > 0.5   # envelope actually fires
        assert env.min() == 0.0  # and fully releases between cycles
