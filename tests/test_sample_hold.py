"""Tests for the SampleHold module (rising-edge sample-and-hold).

Coverage:
  - Model: registration, no params, ports/signal kinds, JSON round-trip,
    unknown param rejected, type walls (cv→in legal, gate→trig legal,
    audio→in illegal, cv→trig illegal, cv out→audio sink illegal).
  - Mono: holds 0 before any trigger; samples the input value at a
    rising edge; holds it flat between edges; steps at each new edge;
    only rising edges sample (falling / held-high do not); state carries
    across blocks (held value + no spurious edge at a block seam);
    unpatched in samples 0; unpatched trig holds last value.
  - Voice-aware: (V, F) inputs sample per-voice on per-voice edges;
    a mono partner broadcasts (shared clock + per-voice source, and
    per-voice clocks + shared source); per-voice state across blocks;
    mono in + mono trig stays 1D.
  - Integration: LFO → Schmitt → SampleHold produces a piecewise-
    constant staircase with ~one step per clock cycle, and the whole
    LFO→S&H→CVScale→CVOffset→CVToFrequency→speaker chain renders to
    finite, audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.patch import Cable
from pysynthrack.modules.samplehold import SampleHold


def _gate(*runs):
    """Build a 0/1 gate from (value, length) runs."""
    parts = [np.full(n, v, dtype=np.float32) for v, n in runs]
    return np.concatenate(parts)


def _plateaus(buf):
    """Number of distinct constant runs (staircase steps) in a 1D buffer."""
    b = np.asarray(buf)
    if b.size == 0:
        return 0
    return int(1 + np.count_nonzero(np.diff(b) != 0))


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_no_params(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        assert isinstance(sh, SampleHold)
        assert sh.params == {}

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        assert [(p.name, p.signal_kind) for p in sh.input_ports] == [
            ("in", "cv"),
            ("trig", "gate"),
        ]
        assert [(p.name, p.signal_kind) for p in sh.output_ports] == [("out", "cv")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("sample_hold")
        restored = Patch.from_dict(patch.to_dict())
        assert any(m.TYPE == "sample_hold" for m in restored)

    def test_unknown_param_rejected(self):
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("sample_hold", params={"slew": 0.1})

    def test_cv_into_in_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        sh = patch.add_module("sample_hold")
        patch.connect(lfo.id, "cv", sh.id, "in")  # cv → cv

    def test_gate_into_trig_accepted(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        sh = patch.add_module("sample_hold")
        patch.connect(sch.id, "gate", sh.id, "trig")  # gate → gate

    def test_audio_into_in_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        sh = patch.add_module("sample_hold")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", sh.id, "in")  # audio → cv

    def test_cv_into_trig_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        sh = patch.add_module("sample_hold")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", sh.id, "trig")  # cv → gate

    def test_cv_out_into_audio_sink_rejected(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        spk = patch.add_module("speaker_output")
        with pytest.raises(ValueError):
            patch.connect(sh.id, "out", spk.id, "in")  # cv → audio


# ----- Mono behaviour --------------------------------------------------------


class TestMono:
    def _make(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        patch.cables.append(Cable(77, "out", sh.id, "in"))
        patch.cables.append(Cable(88, "gate", sh.id, "trig"))
        return patch, sh, backend

    def _render(self, backend, sh, patch, in_arr, trig_arr):
        buffers = {}
        if in_arr is not None:
            buffers[(77, "out")] = np.asarray(in_arr, dtype=np.float32)
        if trig_arr is not None:
            buffers[(88, "gate")] = np.asarray(trig_arr, dtype=np.float32)
        n = len(trig_arr) if trig_arr is not None else len(in_arr)
        return backend._render_sample_hold(sh, n, buffers, patch)

    def test_holds_zero_before_first_trigger(self):
        patch, sh, backend = self._make()
        out = self._render(backend, sh, patch, np.full(256, 0.9), _gate((0.0, 256)))
        assert out.shape == (256,)
        assert np.all(out == 0.0)

    def test_samples_value_at_rising_edge(self):
        patch, sh, backend = self._make()
        in_arr = np.linspace(0.0, 1.0, 200).astype(np.float32)
        trig = _gate((0.0, 100), (1.0, 100))  # rises at sample 100
        out = self._render(backend, sh, patch, in_arr, trig)
        assert np.all(out[:100] == 0.0)              # before the edge: held 0
        assert np.allclose(out[100:], in_arr[100])   # sampled in[100], held flat
        assert len(np.unique(out[100:])) == 1

    def test_holds_flat_between_edges(self):
        patch, sh, backend = self._make()
        # input keeps changing, but only two edges occur
        in_arr = np.linspace(-1.0, 1.0, 300).astype(np.float32)
        trig = _gate((0.0, 50), (1.0, 10), (0.0, 90), (1.0, 10), (0.0, 140))
        out = self._render(backend, sh, patch, in_arr, trig)
        assert np.allclose(out[:50], 0.0)
        assert np.allclose(out[50:150], in_arr[50])   # first edge value, held
        assert np.allclose(out[150:], in_arr[150])    # second edge value, held
        assert _plateaus(out) == 3

    def test_only_rising_edges_sample(self):
        patch, sh, backend = self._make()
        in_arr = np.arange(200, dtype=np.float32) / 200.0
        # one rise (at 40), stays high, falls (at 120) — only the rise samples
        trig = _gate((0.0, 40), (1.0, 80), (0.0, 80))
        out = self._render(backend, sh, patch, in_arr, trig)
        assert np.allclose(out[40:], in_arr[40])   # value frozen at the rise
        assert _plateaus(out) == 2                  # 0, then the held sample

    def test_state_carries_across_blocks(self):
        patch, sh, backend = self._make()
        in1 = np.full(64, 0.42, dtype=np.float32)
        out1 = self._render(backend, sh, patch, in1, _gate((0.0, 30), (1.0, 34)))
        # next block: no triggers at all → must keep holding 0.42
        out2 = self._render(backend, sh, patch, np.full(64, 0.99), _gate((0.0, 64)))
        assert np.allclose(out1[30:], 0.42)
        assert np.allclose(out2, 0.42)

    def test_no_spurious_edge_at_block_seam(self):
        patch, sh, backend = self._make()
        # block 1 ends HIGH; block 2 starts HIGH with a different input.
        # No rising edge spans the seam → block 2 must not re-sample.
        out1 = self._render(backend, sh, patch, np.full(64, 0.3), _gate((0.0, 20), (1.0, 44)))
        out2 = self._render(backend, sh, patch, np.full(64, 0.8), _gate((1.0, 64)))
        assert np.allclose(out1[20:], 0.3)
        assert np.allclose(out2, 0.3)   # held high, no new edge → no resample

    def test_unpatched_in_samples_zero(self):
        patch, sh, backend = self._make()
        out = self._render(backend, sh, patch, None, _gate((0.0, 20), (1.0, 44)))
        assert np.all(out == 0.0)

    def test_unpatched_trig_holds_last_value(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        out = backend._render_sample_hold(sh, 128, {}, patch)
        assert out.shape == (128,)
        assert np.all(out == 0.0)


# ----- Voice-aware -----------------------------------------------------------


class TestVoiceAware:
    def _make(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        patch.cables.append(Cable(77, "out", sh.id, "in"))
        patch.cables.append(Cable(88, "gate", sh.id, "trig"))
        return patch, sh, backend

    def _render(self, backend, sh, patch, in_arr, trig_arr, n):
        buffers = {}
        if in_arr is not None:
            buffers[(77, "out")] = np.asarray(in_arr, dtype=np.float32)
        if trig_arr is not None:
            buffers[(88, "gate")] = np.asarray(trig_arr, dtype=np.float32)
        return backend._render_sample_hold(sh, n, buffers, patch)

    def test_voice_inputs_sample_per_voice(self):
        patch, sh, backend = self._make()
        F = 200
        in0 = np.full(F, 0.2, dtype=np.float32)
        in1 = np.full(F, -0.5, dtype=np.float32)
        in_2d = np.stack([in0, in1])
        # voice 0 rises at 50, voice 1 rises at 120
        t0 = _gate((0.0, 50), (1.0, 150))
        t1 = _gate((0.0, 120), (1.0, 80))
        trig_2d = np.stack([t0, t1]).astype(np.float32)
        out = self._render(backend, sh, patch, in_2d, trig_2d, F)
        assert out.shape == (2, F)
        assert np.all(out[0, :50] == 0.0) and np.allclose(out[0, 50:], 0.2)
        assert np.all(out[1, :120] == 0.0) and np.allclose(out[1, 120:], -0.5)

    def test_mono_source_per_voice_clocks(self):
        patch, sh, backend = self._make()
        F = 200
        in_arr = np.linspace(0.0, 1.0, F).astype(np.float32)  # mono source
        t0 = _gate((0.0, 40), (1.0, 160))
        t1 = _gate((0.0, 150), (1.0, 50))
        trig_2d = np.stack([t0, t1]).astype(np.float32)
        out = self._render(backend, sh, patch, in_arr, trig_2d, F)
        assert out.shape == (2, F)
        assert np.allclose(out[0, 40:], in_arr[40])    # voice 0 sampled early
        assert np.allclose(out[1, 150:], in_arr[150])  # voice 1 sampled late
        assert out[0, 40] != out[1, 150]

    def test_shared_clock_per_voice_sources(self):
        patch, sh, backend = self._make()
        F = 120
        in_2d = np.stack([np.full(F, 0.7), np.full(F, -0.3)]).astype(np.float32)
        trig = _gate((0.0, 60), (1.0, 60))  # mono shared clock
        out = self._render(backend, sh, patch, in_2d, trig, F)
        assert out.shape == (2, F)
        assert np.allclose(out[0, 60:], 0.7)
        assert np.allclose(out[1, 60:], -0.3)

    def test_per_voice_state_across_blocks(self):
        patch, sh, backend = self._make()
        in_2d = np.stack([np.full(64, 0.9), np.full(64, 0.1)]).astype(np.float32)
        trig_2d = np.stack([_gate((0.0, 20), (1.0, 44)),
                            _gate((0.0, 64))]).astype(np.float32)
        o1 = self._render(backend, sh, patch, in_2d, trig_2d, 64)
        # block 2: no edges anywhere → both voices hold
        o2 = self._render(backend, sh, patch,
                          np.stack([np.full(64, 0.0), np.full(64, 0.0)]).astype(np.float32),
                          np.zeros((2, 64), dtype=np.float32), 64)
        assert np.allclose(o1[0, 20:], 0.9) and np.allclose(o1[1], 0.0)
        assert np.allclose(o2[0], 0.9) and np.allclose(o2[1], 0.0)

    def test_mono_in_and_trig_stays_1d(self):
        patch, sh, backend = self._make()
        out = self._render(backend, sh, patch,
                           np.full(32, 0.5, dtype=np.float32),
                           _gate((0.0, 10), (1.0, 22)), 32)
        assert out.ndim == 1


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def _run(self, patch, backend, blocks, taps, block=512):
        out = {k: [] for k in taps}
        for _ in range(blocks):
            buffers = {}
            for mid in backend._topo_order:
                m = patch.modules.get(mid)
                if m is None:
                    continue
                result = backend._render_module(m, block, buffers, patch)
                if result is None:
                    continue
                if isinstance(result, dict):
                    for port, buf in result.items():
                        buffers[(mid, port)] = buf
                elif m.OUTPUT_PORTS:
                    buffers[(mid, m.OUTPUT_PORTS[0].name)] = result
            for k in taps:
                out[k].append(buffers[k])
        return {k: np.concatenate(v) for k, v in out.items()}

    def test_lfo_schmitt_samplehold_makes_staircase(self):
        """A continuous LFO sampled at a Schmitt clock comes out as a
        piecewise-constant staircase, ~one step per clock cycle."""
        sr = 44100
        patch = Patch()
        src = patch.add_module(
            "lfo", params={"waveform": "triangle", "rate": 0.7, "depth": 1.0, "bipolar": True}
        )
        clk = patch.add_module(
            "lfo", params={"waveform": "square", "rate": 5.0, "depth": 1.0, "bipolar": False}
        )
        sch = patch.add_module("schmitt")
        sh = patch.add_module("sample_hold")
        patch.connect(src.id, "cv", sh.id, "in")
        patch.connect(clk.id, "cv", sch.id, "in")
        patch.connect(sch.id, "gate", sh.id, "trig")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)

        res = self._run(patch, backend, blocks=(2 * sr) // 512, taps=[(sh.id, "out")])
        held = res[(sh.id, "out")]
        seconds = held.size / sr
        steps = _plateaus(held)
        # ~5 steps/sec from the 5 Hz clock (allow slack for block edges/rounding)
        assert 7 <= steps <= 13          # ~10 over ~2 s
        assert abs(steps / seconds - 5.0) <= 2.0
        assert np.isfinite(held).all()
        # genuinely piecewise-constant: most samples equal their neighbour
        assert np.mean(np.diff(held) == 0) > 0.95

    def test_full_chain_renders_audio(self):
        """LFO(random) → S&H (Schmitt clock) → CVScale → CVOffset →
        CVToFrequency → speaker: a self-playing stepped tone."""
        np.random.seed(0)
        sr = 44100
        patch = Patch()
        src = patch.add_module(
            "lfo", params={"waveform": "random", "rate": 12.0, "depth": 1.0, "bipolar": True}
        )
        clk = patch.add_module(
            "lfo", params={"waveform": "square", "rate": 4.0, "depth": 1.0, "bipolar": False}
        )
        sch = patch.add_module("schmitt")
        sh = patch.add_module("sample_hold")
        scale = patch.add_module("cv_scale", params={"scale": 0.5})
        offset = patch.add_module("cv_offset", params={"offset": 0.5})
        c2f = patch.add_module(
            "cv_to_frequency",
            params={"f0": 110.0, "fm": 220.0, "f1": 880.0, "mode": "log", "waveform": "saw_blep"},
        )
        spk = patch.add_module("speaker_output", params={"gain": 0.7})
        patch.connect(src.id, "cv", sh.id, "in")
        patch.connect(clk.id, "cv", sch.id, "in")
        patch.connect(sch.id, "gate", sh.id, "trig")
        patch.connect(sh.id, "out", scale.id, "in")
        patch.connect(scale.id, "out", offset.id, "in")
        patch.connect(offset.id, "out", c2f.id, "cv")
        patch.connect(c2f.id, "out", spk.id, "in")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        peak = 0.0
        for _ in range(20):
            blk = backend.render_block(512)
            assert blk.shape == (512, 2)
            assert np.isfinite(blk).all()
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
