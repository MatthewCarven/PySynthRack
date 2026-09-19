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

Love pass (2026-09-19) — ``mode`` / ``prob`` + ``seed`` / ``glide``:
  - Defaults are OFF: the default render equals a pure-Python model of
    the shipped S&H bit for bit (mono and voice), ``prob`` 1 leaves the
    rng's state untouched, ``glide`` 0 never calls lfilter.
  - Track: follows ``in`` while the gate is high and holds the last
    followed sample at the fall (measured), across a block seam too.
  - Sometimes: prob 0 never samples; prob 0.5 with seed 3 samples on an
    exactly pinned count of 200 edges (the count IS ``default_rng(3)``'s
    first 200 doubles under 0.5 — one draw per edge, in time order);
    the seed reproduces and a live seed change re-rolls; in track mode
    a losing window is skipped whole.
  - Glide: 99% of a step in ``glide`` seconds (measured, within 5%);
    primes to the held value when switched on live; lags the followed
    signal in track mode.
  - Block-size independence for every feature, 64 vs 512 with edges
    mid-stream, in BOTH the mono and the voice path.
  - Voice path: the per-voice draw order is pinned against a hand
    model (time-major, voice-minor); one lag per voice; per-voice
    track windows.
  - UI: the ``mode`` combo offers SAMPLE_HOLD_MODES (not the filter's)
    and every param gets a bounded widget (mocked dpg).
  - The example ``sample_hold_sometimes.json`` renders at a sane peak,
    repeats notes at about ``prob`` and stays in C pentatonic minor.

Love pass (2026-09-20) — ``prob_cv`` + ``prob_cv_depth``:
  - The jack ships OFF: a ``prob_cv`` cable carrying 0 (at any depth)
    and a moving CV at depth 0 are both the unpatched render draw for
    draw, and the unpatched render is the scalar path the tests above
    already pin against the pure-Python model and ``default_rng``.
  - The spec pins: ``prob`` 0.5 with +0.5 at depth 1 == ``prob`` 1.0
    bit-exact AND the rng untouched; a CV of 1e6 clamps to 1 (every
    edge, no draws, finite), -1e6 to 0; NaN reads as 0; negative depth
    inverts.
  - Read at the EDGE'S OWN SAMPLE: a CV that is +1 only on the even
    edges' samples and -1 everywhere else (block mean ~ -1) samples
    every even edge and no odd one, consuming no draws.
  - A moving CV's decisions equal a hand model: draws only where
    0 < p < 1, in time order, from ``default_rng(seed)``.
  - Voice path: a ``(V, F)`` CV is one chance per voice at that voice's
    edge (v0 pinned at 1 samples every edge, v1 at 0 never, v2 at 0.5
    draws ALONE — its hits are the rng's first doubles); a ``(V, F)`` CV
    on a mono in/trig promotes the module to the voice path; a mono CV
    broadcasts; a CV of the wrong V is averaged to mono.
  - Track mode: the die at the window's rising edge decides the window.
  - Block-size independence, 64 vs 512, with edges mid-stream and a
    moving CV, mono and voice, sample and track.
  - UI: a bounded ``prob_cv_depth`` drag (p/unit); the CV-depth map row.
  - The example ``sample_hold_prob_sweep.json`` renders identically
    twice with no global seeding, at a sane peak, changes on every tick
    where the sweep pins the chance near 1 and almost never where it
    pins it near 0, and stays in C pentatonic minor.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.patch import Cable
from pysynthrack.modules.samplehold import SAMPLE_HOLD_MODES, SampleHold


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
    def test_register_and_default_params(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        assert isinstance(sh, SampleHold)
        # Every love-pass knob ships OFF (a depth of 1 on an unpatched jack is off).
        assert sh.params == {
            "mode": "sample", "prob": 1.0, "seed": 1, "glide": 0.0, "prob_cv_depth": 1.0,
        }

    def test_modes_registered(self):
        assert SAMPLE_HOLD_MODES == ("sample", "track")
        assert SampleHold.DEFAULT_PARAMS["mode"] in SAMPLE_HOLD_MODES

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        sh = patch.add_module("sample_hold")
        assert [(p.name, p.signal_kind) for p in sh.input_ports] == [
            ("in", "cv"),
            ("trig", "gate"),
            ("prob_cv", "cv"),
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


# ----- Love pass: shared helpers ----------------------------------------------

SR = 44100


def _make(params=None):
    """A lone sample_hold with fake feeders on (77, out) -> in and
    (88, gate) -> trig, so a test can hand the renderer exact buffers."""
    patch = Patch()
    sh = patch.add_module("sample_hold", params=params or {})
    backend = NumpyBackend(sample_rate=SR, block_size=512)
    backend.compile(patch)
    patch.cables.append(Cable(77, "out", sh.id, "in"))
    patch.cables.append(Cable(88, "gate", sh.id, "trig"))
    return patch, sh, backend


def _run(backend, sh, patch, in_arr, trig_arr, block):
    """Render the whole stream in ``block``-sized chunks (1-D or (V, F))."""
    n = trig_arr.shape[-1]
    outs = []
    for s0 in range(0, n, block):
        e = min(n, s0 + block)
        buffers = {
            (77, "out"): np.asarray(in_arr[..., s0:e], dtype=np.float32),
            (88, "gate"): np.asarray(trig_arr[..., s0:e], dtype=np.float32),
        }
        outs.append(backend._render_sample_hold(sh, e - s0, buffers, patch))
    return np.concatenate(outs, axis=-1)


def _clock(n, period, width, phase=0):
    """A 0/1 pulse train: high for ``width`` samples every ``period``."""
    t = (np.arange(n) + phase) % period
    return (t < width).astype(np.float32)


def _model_sample_hold(x, trig, thresh=0.5):
    """The shipped S&H, as a pure-Python loop: sample at each rising edge,
    hold between. The reference the default render must equal exactly."""
    x = np.asarray(x, dtype=np.float32)
    g = np.asarray(trig) > thresh
    held = np.float32(0.0)
    prev = False
    out = np.empty_like(x)
    for n in range(x.size):
        if g[n] and not prev:
            held = x[n]
        out[n] = held
        prev = bool(g[n])
    return out


_N = 12000
_X = np.sin(2 * np.pi * 2.3 * np.arange(_N) / SR).astype(np.float32)
_TRIG = _clock(_N, 1000, 400)   # 12 edges, none on a 64- or 512-block boundary


# ----- Love pass: defaults are OFF --------------------------------------------


class TestDefaultsAreOff:
    def test_default_render_equals_the_shipped_model_mono(self):
        patch, sh, backend = _make()
        out = _run(backend, sh, patch, _X, _TRIG, 512)
        assert np.array_equal(out, _model_sample_hold(_X, _TRIG))
        assert out.dtype == np.float32

    def test_explicit_defaults_equal_bare_defaults_mono(self):
        _p, sh_a, b_a = _make()
        _q, sh_b, b_b = _make({"mode": "sample", "prob": 1.0, "seed": 1, "glide": 0.0})
        assert np.array_equal(_run(b_a, sh_a, _p, _X, _TRIG, 512),
                              _run(b_b, sh_b, _q, _X, _TRIG, 512))

    def test_default_render_equals_the_shipped_model_voice(self):
        patch, sh, backend = _make()
        xv = np.stack([_X, -_X, 0.5 * _X])
        tv = np.stack([_clock(_N, 1000, 400), _clock(_N, 1300, 100), _clock(_N, 700, 350)])
        out = _run(backend, sh, patch, xv, tv, 512)
        assert out.shape == (3, _N)
        for v in range(3):
            assert np.array_equal(out[v], _model_sample_hold(xv[v], tv[v]))

    def test_prob_one_consumes_no_draws(self):
        """The default must be bit-exact with the pre-love-pass render,
        so prob 1 must not even touch the die."""
        patch, sh, backend = _make()
        _run(backend, sh, patch, _X, _TRIG, 512)
        rng = backend._state[sh.id]["rng"]
        before = rng.bit_generator.state
        _run(backend, sh, patch, _X, _TRIG, 512)
        assert rng.bit_generator.state == before
        assert before == np.random.default_rng(1).bit_generator.state

    def test_glide_zero_calls_no_filter(self, monkeypatch):
        import pysynthrack.audio.numpy_backend as nb

        def boom(*_a, **_k):
            raise AssertionError("lfilter must not run at glide 0")

        monkeypatch.setattr(nb, "lfilter", boom)
        patch, sh, backend = _make({"glide": 0.0})
        out = _run(backend, sh, patch, _X, _TRIG, 512)
        assert np.array_equal(out, _model_sample_hold(_X, _TRIG))
        # ...and the voice path, and the unpatched-trig path.
        patch, sh, backend = _make()
        _run(backend, sh, patch, np.stack([_X, -_X]), _TRIG, 512)
        backend._render_sample_hold(sh, 128, {}, patch)

    def test_unpatched_contracts_unchanged(self):
        # unpatched in -> samples 0; unpatched trig -> no edges, holds.
        patch, sh, backend = _make({"mode": "track", "prob": 0.5, "glide": 0.01})
        buffers = {(88, "gate"): _clock(300, 100, 50)}
        assert np.all(backend._render_sample_hold(sh, 300, buffers, patch) == 0.0)
        patch2 = Patch()
        sh2 = patch2.add_module("sample_hold", params={"mode": "track", "glide": 0.01})
        b2 = NumpyBackend(sample_rate=SR, block_size=512)
        b2.compile(patch2)
        assert np.all(b2._render_sample_hold(sh2, 128, {}, patch2) == 0.0)


# ----- Love pass: track ---------------------------------------------------------


class TestTrack:
    def test_follows_while_high_and_holds_the_last_seen_at_the_fall(self):
        patch, sh, backend = _make({"mode": "track"})
        out = _run(backend, sh, patch, _X, _TRIG, 512)
        hi = _TRIG > 0.5
        assert np.array_equal(out[hi], _X[hi])            # follows, exactly
        for k in range(12):
            lo = slice(k * 1000 + 400, (k + 1) * 1000)
            assert np.all(out[lo] == _X[k * 1000 + 399])  # the last high sample
        # and it really is the tail, not the head, of each window
        assert out[1400] != _X[1000]

    def test_window_spanning_a_block_seam(self):
        patch, sh, backend = _make({"mode": "track"})
        x = np.linspace(-1, 1, 200).astype(np.float32)
        # high from 40 to 140, straddling a seam at 100
        trig = _gate((0.0, 40), (1.0, 100), (0.0, 60))
        o1 = _run(backend, sh, patch, x[:100], trig[:100], 100)
        o2 = _run(backend, sh, patch, x[100:], trig[100:], 100)
        out = np.concatenate([o1, o2])
        assert np.array_equal(out[40:140], x[40:140])
        assert np.all(out[140:] == x[139])

    def test_live_flip_to_track_under_a_held_gate_follows_at_once(self):
        """At prob 1 every window wins, so flipping the combo while the
        gate is already high starts following in the very next block --
        no waiting for the next edge."""
        patch, sh, backend = _make({"mode": "sample"})
        x = np.linspace(0, 1, 400).astype(np.float32)
        trig = np.ones(400, dtype=np.float32)
        o1 = _run(backend, sh, patch, x[:200], trig[:200], 200)   # sampled x[0], held
        assert np.all(o1 == x[0])
        sh.params["mode"] = "track"
        o2 = _run(backend, sh, patch, x[200:], trig[200:], 200)
        assert np.array_equal(o2, x[200:])

    def test_sample_mode_is_unchanged(self):
        _p, sh_a, b_a = _make({"mode": "sample"})
        out = _run(b_a, sh_a, _p, _X, _TRIG, 512)
        assert np.array_equal(out, _model_sample_hold(_X, _TRIG))

    def test_unknown_mode_falls_back_to_sample(self):
        patch, sh, backend = _make()
        sh.params["mode"] = "bogus"
        out = _run(backend, sh, patch, _X, _TRIG, 512)
        assert np.array_equal(out, _model_sample_hold(_X, _TRIG))

    def test_voice_path_tracks_per_voice(self):
        patch, sh, backend = _make({"mode": "track"})
        xv = np.stack([_X, -_X])
        tv = np.stack([_clock(_N, 1000, 400), _clock(_N, 1300, 100)])
        out = _run(backend, sh, patch, xv, tv, 512)
        for v in range(2):
            hi = tv[v] > 0.5
            assert np.array_equal(out[v][hi], xv[v][hi])
        # voice 1's low stretch holds ITS last high sample
        assert np.all(out[1, 100:1300] == xv[1, 99])


# ----- Love pass: sometimes (prob + seed) --------------------------------------


def _sampled_edges(out, trig):
    """Which rising edges of ``trig`` changed the held value (a sampled
    edge on a moving input always does; a skipped one never does)."""
    g = trig > 0.5
    edges = np.flatnonzero(g[1:] & ~g[:-1]) + 1
    return np.array([out[e] != out[e - 1] for e in edges])


class TestSometimes:
    N = 200_000            # 200 edges at period 1000
    X = np.sin(2 * np.pi * 2.3 * np.arange(N) / SR).astype(np.float32)
    TRIG = _clock(N, 1000, 400, phase=-1)   # first edge at sample 1, not 0

    def test_prob_zero_never_samples(self):
        patch, sh, backend = _make({"prob": 0.0, "seed": 3})
        out = _run(backend, sh, patch, self.X, self.TRIG, 512)
        assert np.all(out == 0.0)
        # and nothing was rolled for it either
        assert (backend._state[sh.id]["rng"].bit_generator.state
                == np.random.default_rng(3).bit_generator.state)

    def test_prob_half_seed_3_samples_a_pinned_count_of_200_edges(self):
        patch, sh, backend = _make({"prob": 0.5, "seed": 3})
        out = _run(backend, sh, patch, self.X, self.TRIG, 512)
        hits = _sampled_edges(out, self.TRIG)
        assert hits.size == 200
        # One draw per edge, in time order, from default_rng(3): the
        # module's decisions ARE its first 200 doubles compared to 0.5.
        expect = np.random.default_rng(3).random(200) < 0.5
        assert np.array_equal(hits, expect)
        assert int(hits.sum()) == int(expect.sum()) == 95

    def test_seed_reproduces_and_differs(self):
        a = _run_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG)
        b = _run_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG)
        c = _run_with({"prob": 0.5, "seed": 4}, self.X, self.TRIG)
        assert np.array_equal(a, b)
        assert not np.array_equal(a, c)

    def test_live_seed_change_rerolls(self):
        patch, sh, backend = _make({"prob": 0.5, "seed": 3})
        half = self.N // 2
        _run(backend, sh, patch, self.X[:half], self.TRIG[:half], 512)
        sh.params["seed"] = 9
        out2 = _run(backend, sh, patch, self.X[half:], self.TRIG[half:], 512)
        hits2 = _sampled_edges(out2, self.TRIG[half:])
        # a fresh default_rng(9) decided the second half's 100 edges
        assert np.array_equal(hits2, np.random.default_rng(9).random(100) < 0.5)

    def test_prob_is_clamped(self):
        patch, sh, backend = _make({"prob": 7.0})
        out = _run(backend, sh, patch, _X, _TRIG, 512)
        assert np.array_equal(out, _model_sample_hold(_X, _TRIG))

    def test_track_mode_skips_a_losing_window_whole(self):
        patch, sh, backend = _make({"mode": "track", "prob": 0.5, "seed": 3})
        out = _run(backend, sh, patch, self.X, self.TRIG, 512)
        verdicts = np.random.default_rng(3).random(200) < 0.5
        held = np.float32(0.0)
        for k, won in enumerate(verdicts):
            s0 = k * 1000 + 1
            win = slice(s0, s0 + 400)
            low = slice(s0 + 400, s0 + 1000)
            if won:
                assert np.array_equal(out[win], self.X[win])
                held = self.X[s0 + 399]
            else:
                assert np.all(out[win] == held)          # held straight through
            assert np.all(out[low] == held)
        assert 0 < verdicts.sum() < 200


def _run_with(params, x, trig, block=512):
    patch, sh, backend = _make(params)
    return _run(backend, sh, patch, x, trig, block)


# ----- Love pass: glide ---------------------------------------------------------


class TestGlide:
    def test_reaches_99_percent_in_glide_seconds(self):
        n0, n = 100, 20000
        x = np.zeros(n, dtype=np.float32)
        x[n0:] = 1.0
        trig = np.zeros(n, dtype=np.float32)
        trig[n0:] = 1.0                       # one edge at n0: a unit step
        for glide in (0.05, 0.1, 0.2):
            out = _run_with({"glide": glide}, x, trig)
            k99 = int(np.argmax(out >= 0.99)) - n0 + 1   # samples to cross 0.99
            assert abs(k99 - glide * SR) <= 0.05 * glide * SR, (glide, k99)
            assert out[n0 - 1] == 0.0 and out[-1] > 0.999

    def test_primes_to_the_held_value_when_switched_on_live(self):
        patch, sh, backend = _make({"glide": 0.0})
        x = np.full(2000, 0.7, dtype=np.float32)
        trig = _clock(2000, 2000, 10)
        _run(backend, sh, patch, x, trig, 512)          # held 0.7, no lag
        sh.params["glide"] = 0.1
        out = _run(backend, sh, patch, x, np.zeros(2000, np.float32), 512)
        assert np.allclose(out, 0.7, atol=1e-6)         # sat there, no swoop from 0

    def test_lags_the_followed_signal_in_track_mode(self):
        n = 6000
        x = np.zeros(n, dtype=np.float32)
        x[1000:] = 1.0                                   # a step WHILE the gate is high
        trig = np.ones(n, dtype=np.float32)
        out = _run_with({"mode": "track", "glide": 0.05}, x, trig)
        assert out[1000] < 0.01 and out[1500] > 0.1 and out[5999] > 0.99
        assert np.all(np.diff(out[1000:]) >= 0)          # monotone one-pole rise

    def test_voice_path_lags_each_voice_independently(self):
        n = 6000
        xv = np.stack([np.full(n, 1.0), np.zeros(n)]).astype(np.float32)
        trig = np.zeros(n, dtype=np.float32)
        trig[100:] = 1.0                                  # shared edge at 100
        out = _run_with({"glide": 0.05}, xv, trig)
        assert out.shape == (2, n)
        assert 0.5 < out[0, 100 + SR // 40] < 0.99 and out[0, -1] > 0.99
        assert np.all(out[1] == 0.0)


# ----- Love pass: block-size independence, both paths ---------------------------


_FEATURE_PARAMS = [
    {"prob": 0.5, "seed": 3},
    {"glide": 0.02},
    {"mode": "track"},
    {"mode": "track", "prob": 0.5, "seed": 9},
    {"mode": "track", "prob": 0.5, "glide": 0.01, "seed": 9},
]


class TestBlockSizeIndependence:
    @pytest.mark.parametrize("params", _FEATURE_PARAMS, ids=[str(p) for p in _FEATURE_PARAMS])
    def test_mono_64_vs_512(self, params):
        a = _run_with(params, _X, _TRIG, 64)
        b = _run_with(params, _X, _TRIG, 512)
        assert np.array_equal(a, b)
        assert np.count_nonzero(np.diff(a)) > 0          # something happened

    @pytest.mark.parametrize("params", _FEATURE_PARAMS, ids=[str(p) for p in _FEATURE_PARAMS])
    def test_voice_64_vs_512(self, params):
        xv = np.stack([_X, -_X, 0.5 * _X])
        tv = np.stack([_clock(_N, 1000, 400), _clock(_N, 1300, 100), _clock(_N, 700, 350)])
        a = _run_with(params, xv, tv, 64)
        b = _run_with(params, xv, tv, 512)
        assert a.shape == (3, _N)
        assert np.array_equal(a, b)


# ----- Love pass: voice path draws ---------------------------------------------


class TestVoiceDraws:
    def test_shared_clock_draws_per_voice_in_time_major_order(self):
        """One die per edge per voice. Under a shared clock every voice
        has an edge at the same sample; the draws go voice 0, 1, 2 at
        that sample, then the next sample's — the documented order."""
        n = 30000
        x = np.sin(2 * np.pi * 2.3 * np.arange(n) / SR).astype(np.float32)
        xv = np.stack([x, -x, 0.5 * x])
        trig = _clock(n, 1000, 400, phase=-1)           # 30 shared edges
        out = _run_with({"prob": 0.5, "seed": 5}, xv, trig)
        draws = np.random.default_rng(5).random(30 * 3) < 0.5
        expect = draws.reshape(30, 3)                     # [edge, voice]
        for v in range(3):
            hits = _sampled_edges(out[v], trig)
            assert np.array_equal(hits, expect[:, v]), v
        # the voices genuinely differ from one another
        assert not np.array_equal(expect[:, 0], expect[:, 1])

    def test_per_voice_clocks_draw_in_time_order(self):
        """Per-voice clocks: the edges interleave in time; the draw order
        is the time order of (sample, voice) pairs."""
        n = 30000
        x = np.sin(2 * np.pi * 2.3 * np.arange(n) / SR).astype(np.float32)
        xv = np.stack([x, -x])
        t0 = _clock(n, 1000, 300, phase=-1)
        t1 = _clock(n, 1300, 300, phase=-1)
        tv = np.stack([t0, t1])
        out = _run_with({"prob": 0.5, "seed": 5}, xv, tv)
        # hand model of the order: every (sample, voice) edge, sorted by
        # sample then voice
        events = []
        for v, t in enumerate((t0, t1)):
            g = t > 0.5
            for e in (np.flatnonzero(g[1:] & ~g[:-1]) + 1).tolist():
                events.append((e, v))
        events.sort()
        draws = np.random.default_rng(5).random(len(events)) < 0.5
        expect = {0: [], 1: []}
        for (e, v), d in zip(events, draws):
            expect[v].append(bool(d))
        for v in range(2):
            assert np.array_equal(_sampled_edges(out[v], tv[v]), np.array(expect[v]))


# ----- Love pass: UI ------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("sample_hold")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (
                k, call.kwargs.get("format"), call.kwargs.get("items"),
                call.kwargs.get("min_value"), call.kwargs.get("max_value"),
            )
    return out


class TestUI:
    def test_mode_combo_offers_sample_hold_modes_not_the_filters(self, monkeypatch):
        """The shared ``mode`` branch trap: without its own arm the S&H
        would be offered lowpass/highpass/bandpass."""
        w = _widgets(monkeypatch)
        assert w["mode"][0] == "add_combo"
        assert w["mode"][2] == list(SAMPLE_HOLD_MODES)

    def test_every_param_gets_a_bounded_widget(self, monkeypatch):
        w = _widgets(monkeypatch)
        labels = list(w)
        for name in SampleHold.DEFAULT_PARAMS:
            hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
            assert hits, (name, labels)
            assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
        # "prob " with the space: "prob_cv_depth" is a drag, collected first.
        prob = next(v for k, v in w.items() if k.startswith("prob "))
        assert prob[0] == "add_slider_float" and (prob[3], prob[4]) == (0.0, 1.0)
        glide = next(v for k, v in w.items() if k.startswith("glide"))
        assert glide[0] == "add_drag_float" and glide[1].endswith(" s")
        assert (glide[3], glide[4]) == (0.0, 5.0)
        assert w["seed"][0] == "add_drag_int" and w["seed"][3] == 0
        depth = w["prob_cv_depth"]
        assert depth[0] == "add_drag_float" and depth[1] == "%.2f p/unit"
        assert (depth[3], depth[4]) == (-2.0, 2.0)


# ----- Love pass: the example ---------------------------------------------------


class TestExample:
    def test_sometimes_example_repeats_notes_in_key(self):
        """The `noise` module is UNSEEDED (numpy's global rng), so the
        render is pinned with np.random.seed before it."""
        from pysynthrack.io_patch import load_patch

        path = Path(__file__).resolve().parent.parent / "examples" / "sample_hold_sometimes.json"
        patch = load_patch(path)
        sh = next(m for m in patch if m.TYPE == "sample_hold")
        assert sh.params["prob"] == 0.6 and sh.params["glide"] == 0.05
        assert sh.params["seed"] == 11
        clk = next(m for m in patch if m.TYPE == "clock")
        np.random.seed(0)
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        cap, capq = [], []
        orig, origq = b._render_sample_hold, b._render_quantizer

        def spy(m, f, bu, p):
            r = orig(m, f, bu, p)
            cap.append(np.asarray(r).copy())
            return r

        def spyq(m, f, bu, p):
            r = origq(m, f, bu, p)
            capq.append(np.asarray(r["out"]).copy())
            return r

        b._render_sample_hold, b._render_quantizer = spy, spyq
        peak = 0.0
        for _ in range(int(SR * 8 / 512)):
            out, _devices = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        assert 0.3 < peak < 0.8, peak
        held = np.concatenate(cap)
        period = SR * 60.0 / (float(clk.params["bpm"]) * float(clk.params["division"]))
        ticks = int(held.size // period)
        changed = 0
        for k in range(1, ticks):
            n = int(round(k * period))
            changed += abs(float(held[n + 400]) - float(held[n - 1])) > 1e-4
        ratio = changed / (ticks - 1)
        assert ticks >= 50 and 0.4 < ratio < 0.85, (ticks, ratio)   # ~prob 0.6
        # every quantised pitch is in C pentatonic minor (0 3 5 7 10)
        st = np.round(np.concatenate(capq) * 12).astype(int) % 12
        assert set(st.tolist()) <= {0, 3, 5, 7, 10}, sorted(set(st.tolist()))


# ----- Love pass (2026-09-20): prob_cv ------------------------------------------


def _make_cv(params=None):
    """``_make`` plus a fake feeder on (99, cv) -> prob_cv."""
    patch, sh, backend = _make(params)
    patch.cables.append(Cable(99, "cv", sh.id, "prob_cv"))
    return patch, sh, backend


def _run_cv(backend, sh, patch, in_arr, trig_arr, cv_arr, block):
    """``_run`` with a prob_cv buffer (1-D or (V, F)); None = the cable
    is there but its source never rendered, which reads as unpatched."""
    n = trig_arr.shape[-1]
    outs = []
    for s0 in range(0, n, block):
        e = min(n, s0 + block)
        buffers = {
            (77, "out"): np.asarray(in_arr[..., s0:e], dtype=np.float32),
            (88, "gate"): np.asarray(trig_arr[..., s0:e], dtype=np.float32),
        }
        if cv_arr is not None:
            buffers[(99, "cv")] = np.asarray(cv_arr[..., s0:e], dtype=np.float32)
        outs.append(backend._render_sample_hold(sh, e - s0, buffers, patch))
    return np.concatenate(outs, axis=-1)


def _run_cv_with(params, x, trig, cv, block=512):
    patch, sh, backend = _make_cv(params)
    return _run_cv(backend, sh, patch, x, trig, cv, block)


def _rng_state(backend, sh):
    return backend._state[sh.id]["rng"].bit_generator.state


def _edges(trig):
    g = trig > 0.5
    return np.flatnonzero(g[1:] & ~g[:-1]) + 1


class TestProbCV:
    N = 200_000
    X = np.sin(2 * np.pi * 2.3 * np.arange(N) / SR).astype(np.float32)
    TRIG = _clock(N, 1000, 400, phase=-1)          # 200 edges, the first at 1
    SWEEP = (0.6 * np.sin(2 * np.pi * 0.05 * np.arange(N) / SR)).astype(np.float32)

    def test_plus_half_at_depth_one_is_prob_one_and_draws_nothing(self):
        """The spec's pin: the CV pushes p to exactly 1 at every edge, so
        every edge samples and the die is never thrown."""
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 3})
        out = _run_cv(backend, sh, patch, self.X, self.TRIG, np.full(self.N, 0.5), 512)
        assert np.array_equal(out, _run_with({"prob": 1.0, "seed": 3}, self.X, self.TRIG))
        assert np.array_equal(out, _model_sample_hold(self.X, self.TRIG))
        assert _rng_state(backend, sh) == np.random.default_rng(3).bit_generator.state

    def test_cv_at_zero_is_the_unpatched_render_draw_for_draw(self):
        """The jack ships OFF: a cable carrying 0 changes nothing, and
        the draw sequence is the unpatched one (pinned against
        default_rng above), so the rng ends in the same state."""
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 3})
        out = _run_cv(backend, sh, patch, self.X, self.TRIG, np.zeros(self.N), 512)
        p2, sh2, b2 = _make({"prob": 0.5, "seed": 3})
        ref = _run(b2, sh2, p2, self.X, self.TRIG, 512)
        assert np.array_equal(out, ref)
        assert _rng_state(backend, sh) == _rng_state(b2, sh2)
        assert int(_sampled_edges(out, self.TRIG).sum()) == 95      # the pinned count

    def test_source_that_never_rendered_reads_as_unpatched(self):
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 3})
        out = _run_cv(backend, sh, patch, self.X, self.TRIG, None, 512)
        assert np.array_equal(out, _run_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG))

    def test_depth_zero_disables_a_moving_cv(self):
        out = _run_cv_with({"prob": 0.5, "seed": 3, "prob_cv_depth": 0.0},
                           self.X, self.TRIG, self.SWEEP)
        assert np.array_equal(out, _run_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG))

    def test_read_at_the_edge_sample_not_a_block_mean(self):
        """+1 on the even edges' own samples, -1 everywhere else: the block
        mean is ~ -1 (never sample), but the per-edge read pins the even
        edges at 1 and the odd ones at 0 -- and neither side draws."""
        cv = np.full(self.N, -1.0, np.float32)
        edges = _edges(self.TRIG)
        cv[edges[::2]] = 1.0
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 3})
        out = _run_cv(backend, sh, patch, self.X, self.TRIG, cv, 512)
        hits = _sampled_edges(out, self.TRIG)
        assert hits.size == 200 and hits[::2].all() and not hits[1::2].any()
        assert _rng_state(backend, sh) == np.random.default_rng(3).bit_generator.state
        # the same CV one sample late misses every edge: nothing samples
        late = np.roll(cv, 1)
        out2 = _run_cv_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG, late)
        assert np.all(out2 == 0.0)

    def test_moving_cv_matches_the_hand_model(self):
        """A +-0.6 sweep around prob 0.5: edges whose p lands on [1, ..)
        sample without a draw, (.., 0] hold without one, and only the
        edges in between consume the rng -- in time order."""
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 3})
        out = _run_cv(backend, sh, patch, self.X, self.TRIG, self.SWEEP, 512)
        edges = _edges(self.TRIG)
        p = np.clip(0.5 + self.SWEEP[edges].astype(np.float64), 0.0, 1.0)
        need = (p > 0.0) & (p < 1.0)
        expect = p >= 1.0
        expect[need] = np.random.default_rng(3).random(int(need.sum())) < p[need]
        assert np.array_equal(_sampled_edges(out, self.TRIG), expect)
        assert 0 < int(need.sum()) < 200                      # both kinds of edge occurred
        assert int(need.sum()) == 139
        # and the rng advanced by exactly that many draws
        rng = np.random.default_rng(3)
        rng.random(int(need.sum()))
        assert _rng_state(backend, sh) == rng.bit_generator.state

    def test_clamp_is_finite(self):
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 3})
        out = _run_cv(backend, sh, patch, self.X, self.TRIG, np.full(self.N, 1e6), 512)
        assert np.isfinite(out).all()
        assert np.array_equal(out, _model_sample_hold(self.X, self.TRIG))
        assert _rng_state(backend, sh) == np.random.default_rng(3).bit_generator.state
        out = _run_cv_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG, np.full(self.N, -1e6))
        assert np.all(out == 0.0)

    def test_non_finite_cv_reads_as_zero(self):
        for bad in (np.nan, np.inf, -np.inf):
            out = _run_cv_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG, np.full(self.N, bad))
            assert np.array_equal(out, _run_with({"prob": 0.5, "seed": 3}, self.X, self.TRIG)), bad

    def test_negative_depth_inverts(self):
        out = _run_cv_with({"prob": 0.5, "seed": 3, "prob_cv_depth": -1.0},
                           self.X, self.TRIG, np.full(self.N, 0.5))
        assert np.all(out == 0.0)                              # p = 0.5 - 0.5
        out = _run_cv_with({"prob": 0.5, "seed": 3, "prob_cv_depth": -1.0},
                           self.X, self.TRIG, np.full(self.N, -0.5))
        assert np.array_equal(out, _model_sample_hold(self.X, self.TRIG))

    def test_depth_scales(self):
        # depth 0.5 with +1 == depth 1 with +0.5 == prob 1
        a = _run_cv_with({"prob": 0.5, "seed": 3, "prob_cv_depth": 0.5},
                         self.X, self.TRIG, np.full(self.N, 1.0))
        assert np.array_equal(a, _model_sample_hold(self.X, self.TRIG))

    def test_track_mode_the_die_at_the_edge_decides_the_window(self):
        cv = np.full(self.N, -1.0, np.float32)
        edges = _edges(self.TRIG)
        cv[edges[::2]] = 1.0
        out = _run_cv_with({"mode": "track", "prob": 0.5, "seed": 3}, self.X, self.TRIG, cv)
        held = np.float32(0.0)
        for k, s0 in enumerate(edges.tolist()):
            win = slice(s0, s0 + 400)
            low = slice(s0 + 400, s0 + 1000)
            if k % 2 == 0:
                assert np.array_equal(out[win], self.X[win])   # followed
                held = self.X[s0 + 399]
            else:
                assert np.all(out[win] == held)                # skipped whole
            assert np.all(out[low] == held)


class TestProbCVVoice:
    N = 30000
    X = np.sin(2 * np.pi * 2.3 * np.arange(N) / SR).astype(np.float32)
    XV = np.stack([X, -X, 0.5 * X])
    TRIG = _clock(N, 1000, 400, phase=-1)          # 30 shared edges
    CVV = np.stack([np.full(N, 0.5), np.full(N, -0.5), np.zeros(N)]).astype(np.float32)

    def test_voice_cv_is_one_chance_per_voice_at_its_own_edge(self):
        """v0 pinned at 1 samples every edge, v1 at 0 never, and v2 at
        0.5 draws ALONE: its hits are default_rng(5)'s first 30 doubles."""
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 5})
        out = _run_cv(backend, sh, patch, self.XV, self.TRIG, self.CVV, 512)
        assert out.shape == (3, self.N)
        assert _sampled_edges(out[0], self.TRIG).all()
        assert not _sampled_edges(out[1], self.TRIG).any()
        assert np.array_equal(_sampled_edges(out[2], self.TRIG),
                              np.random.default_rng(5).random(30) < 0.5)
        rng = np.random.default_rng(5)
        rng.random(30)
        assert _rng_state(backend, sh) == rng.bit_generator.state

    def test_voice_cv_alone_promotes_to_the_voice_path(self):
        """Mono in + mono trig + a (V, F) CV: one source, one clock, three
        dice with three different odds -> (V, F) out."""
        out = _run_cv_with({"prob": 0.5, "seed": 5}, self.X, self.TRIG, self.CVV)
        assert out.shape == (3, self.N)
        assert _sampled_edges(out[0], self.TRIG).all()
        assert not _sampled_edges(out[1], self.TRIG).any()
        assert np.array_equal(out[0], _model_sample_hold(self.X, self.TRIG))

    def test_mono_cv_broadcasts_to_every_voice(self):
        patch, sh, backend = _make_cv({"prob": 0.5, "seed": 5})
        out = _run_cv(backend, sh, patch, self.XV, self.TRIG, np.full(self.N, 0.5), 512)
        for v in range(3):
            assert np.array_equal(out[v], _model_sample_hold(self.XV[v], self.TRIG))
        assert _rng_state(backend, sh) == np.random.default_rng(5).bit_generator.state

    def test_wrong_voice_count_is_averaged(self):
        """A (2, F) CV of +1 / -1 on a 3-voice module averages to 0: the
        unpatched voice render, draw for draw."""
        cv2 = np.stack([np.full(self.N, 1.0), np.full(self.N, -1.0)]).astype(np.float32)
        out = _run_cv_with({"prob": 0.5, "seed": 5}, self.XV, self.TRIG, cv2)
        assert out.shape == (3, self.N)
        assert np.array_equal(out, _run_with({"prob": 0.5, "seed": 5}, self.XV, self.TRIG))

    def test_per_voice_clocks_and_a_moving_voice_cv_match_the_hand_model(self):
        """Edges interleave in time; the draw order is (sample, voice)
        and only the edges with 0 < p < 1 draw."""
        t0 = _clock(self.N, 1000, 300, phase=-1)
        t1 = _clock(self.N, 1300, 300, phase=-1)
        tv = np.stack([t0, t1])
        sweep = (0.6 * np.sin(2 * np.pi * 0.5 * np.arange(self.N) / SR)).astype(np.float32)
        cvv = np.stack([sweep, -sweep])
        out = _run_cv_with({"prob": 0.5, "seed": 5}, self.XV[:2], tv, cvv)
        events = []
        for v, t in enumerate((t0, t1)):
            for e in _edges(t).tolist():
                events.append((e, v))
        events.sort()
        p = np.array([np.clip(0.5 + float(cvv[v, e]), 0.0, 1.0) for e, v in events])
        need = (p > 0.0) & (p < 1.0)
        verdict = p >= 1.0
        verdict[need] = np.random.default_rng(5).random(int(need.sum())) < p[need]
        expect = {0: [], 1: []}
        for (e, v), d in zip(events, verdict):
            expect[v].append(bool(d))
        for v in range(2):
            assert np.array_equal(_sampled_edges(out[v], tv[v]), np.array(expect[v])), v
        assert 0 < int(need.sum()) < len(events)


class TestProbCVBlockSize:
    N = _N
    SWEEP = (0.6 * np.sin(2 * np.pi * 0.7 * np.arange(N) / SR)).astype(np.float32)
    PARAMS = [
        {"prob": 0.5, "seed": 3},
        {"mode": "track", "prob": 0.5, "seed": 9},
        {"mode": "track", "prob": 0.5, "glide": 0.01, "seed": 9},
        {"prob": 0.5, "seed": 3, "glide": 0.02, "prob_cv_depth": 0.8},
    ]

    @pytest.mark.parametrize("params", PARAMS, ids=[str(p) for p in PARAMS])
    def test_mono_64_vs_512_with_a_moving_cv(self, params):
        a = _run_cv_with(params, _X, _TRIG, self.SWEEP, 64)
        b = _run_cv_with(params, _X, _TRIG, self.SWEEP, 512)
        assert np.array_equal(a, b)
        assert np.count_nonzero(np.diff(a)) > 0

    @pytest.mark.parametrize("params", PARAMS, ids=[str(p) for p in PARAMS])
    def test_voice_64_vs_512_with_a_moving_voice_cv(self, params):
        xv = np.stack([_X, -_X, 0.5 * _X])
        tv = np.stack([_clock(_N, 1000, 400), _clock(_N, 1300, 100), _clock(_N, 700, 350)])
        cvv = np.stack([self.SWEEP, -self.SWEEP, 0.3 * self.SWEEP])
        a = _run_cv_with(params, xv, tv, cvv, 64)
        b = _run_cv_with(params, xv, tv, cvv, 512)
        assert a.shape == (3, _N)
        assert np.array_equal(a, b)


class TestProbCVDocs:
    def test_depth_map_row_is_documented(self):
        import re
        md = (Path(__file__).resolve().parent.parent / "docs" / "MODULES.md").read_text(
            encoding="utf-8"
        )
        assert re.search(r"^\| `sample_hold\.prob_cv` \| `1\.0` \(`prob_cv_depth`\)", md, re.M)
        assert re.search(r"^\| `prob_cv` \| in \| cv \|", md, re.M)
        assert re.search(r"^\| `prob_cv_depth` \| `1\.0` \|", md, re.M)


class TestProbSweepExample:
    def _render(self, seconds):
        from pysynthrack.io_patch import load_patch

        path = Path(__file__).resolve().parent.parent / "examples" / "sample_hold_prob_sweep.json"
        patch = load_patch(path)
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        cap, capq = [], []
        orig, origq = b._render_sample_hold, b._render_quantizer

        def spy(m, f, bu, p):
            r = orig(m, f, bu, p)
            cap.append(np.asarray(r).copy())
            return r

        def spyq(m, f, bu, p):
            r = origq(m, f, bu, p)
            capq.append(np.asarray(r["out"]).copy())
            return r

        b._render_sample_hold, b._render_quantizer = spy, spyq
        master = []
        for _ in range(int(SR * seconds / 512)):
            out, _devices = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            master.append(np.asarray(out).copy())
        return patch, np.concatenate(master), np.concatenate(cap), np.concatenate(capq)

    def test_renders_identically_twice_with_no_global_seeding(self):
        """Every source in the patch is seeded (the noise's own ``seed``),
        so two cold renders agree bit for bit without np.random.seed."""
        _p, m1, h1, _q = self._render(3)
        _p, m2, h2, _q = self._render(3)
        assert np.array_equal(m1, m2) and np.array_equal(h1, h2)

    def test_melody_frees_up_and_gets_stuck_with_the_sweep(self):
        patch, master, held, quant = self._render(18)
        sh = next(m for m in patch if m.TYPE == "sample_hold")
        assert sh.params["prob"] == 0.5 and sh.params["prob_cv_depth"] == 0.5
        assert sh.params["glide"] == 0.03
        lfo = next(m for m in patch if m.TYPE == "lfo")
        assert lfo.params["rate"] == 0.05 and lfo.params["bipolar"] is True
        nz = next(m for m in patch if m.TYPE == "noise")
        assert int(nz.params["seed"]) != 0
        assert len(list(patch)) <= 12
        peak = float(np.abs(master).max())
        assert 0.3 < peak < 0.8, peak
        clk = next(m for m in patch if m.TYPE == "clock")
        period = SR * 60.0 / (float(clk.params["bpm"]) * float(clk.params["division"]))
        ticks = int(held.size // period)
        t = np.arange(1, ticks) * period / SR
        changed = np.array([
            abs(float(held[int(round(k * period)) + 400]) - float(held[int(round(k * period)) - 1])) > 1e-4
            for k in range(1, ticks)
        ])
        # a 0.05 Hz sine from phase 0: p = 0.5 + 0.5 sin -> above 0.85 on
        # 2.5..7.5 s, below 0.15 on 12.5..17.5 s
        free = changed[(t >= 2.5) & (t < 7.5)]
        stuck = changed[(t >= 12.5) & (t < 17.5)]
        assert free.size >= 15 and free.mean() > 0.8, (free.size, free.mean())
        assert stuck.size >= 15 and stuck.mean() < 0.3, (stuck.size, stuck.mean())
        # every quantised pitch is in C pentatonic minor (0 3 5 7 10)
        st = np.round(quant * 12).astype(int) % 12
        assert set(st.tolist()) <= {0, 3, 5, 7, 10}, sorted(set(st.tolist()))
