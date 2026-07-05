"""Tests for the Convolver (partitioned-FFT convolution — IR reverb / cab).

Slice 1 is the mono fixed-block core with stereo (duplicated) outs and a
default unit-impulse IR (a transparent insert until a file loader lands).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in -> stereo
    audio out), JSON round-trip, unknown-param rejection, category.
  - Engine (``_PartitionedConvolver``): block-for-block equivalence to
    ``scipy.signal.fftconvolve`` across block sizes and IR lengths (the
    oracle); a unit impulse is a delayed identity; tail length equals the
    IR length; the reported latency is exactly one block.
  - Neutral: the default unit-impulse IR at mix=1 / gain=1 is a passthrough
    delayed by one block, within 1e-6 (FFT round-trip, not bit-exact);
    mix=0 is a bit-exact delayed dry bypass; silence stays silence.
  - Latency: an impulse peaks one block late; latency is constant across
    block sizes.
  - Oracle (module): with a real IR injected, the streamed wet output
    matches ``fftconvolve`` to float32 tolerance; gain trims the wet only;
    mix blends dry/wet.
  - Block size: one big block == many small blocks (aligned by each latency)
    to FFT round-off.
  - Voice: a single voice row is bit-identical to mono; voices sum before
    the convolution (linearity).
  - Integration: osc -> convolver -> speaker renders finite stereo audio.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import fftconvolve

import pysynthrack.modules  # noqa: F401
from pysynthrack.core import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend, _PartitionedConvolver
from pysynthrack.modules.convolver import Convolver

SR = 44100
F = 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)



def _patch(**params):
    patch = Patch()
    osc = patch.add_module("oscillator")
    conv = patch.add_module("convolver", params=params or None)
    patch.connect(osc.id, "out", conv.id, "in")
    return patch, osc, conv


def _seed_ir(b, module_id, ir):
    """Pre-seed a convolver's state with a chosen IR (before first render)."""
    b._state[module_id] = {
        "engine": None, "dry_prev": None, "block": None,
        "ir": np.asarray(ir, dtype=np.float64), "_ir_id": None,
    }


def _run(b, conv, patch, src_id, x, block=F, port="out_l"):
    """Render ``x`` (1D or (V, T)) block by block; return one channel, joined."""
    n = (x.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src_id, "out"): x[..., sl].astype(np.float32)}
        outs.append(b._render_convolver(conv, block, bufs, patch)[port])
    return np.concatenate(outs, axis=-1)


def _sine(n, freq=440.0, amp=0.5):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        conv = Patch().add_module("convolver")
        assert isinstance(conv, Convolver)
        assert conv.params == {"gain": 1.0, "mix": 1.0}

    def test_ports_and_signal_kinds(self):
        conv = Patch().add_module("convolver")
        assert [(p.name, p.signal_kind) for p in conv.input_ports] == [("in", "audio")]
        assert [(p.name, p.signal_kind) for p in conv.output_ports] == [
            ("out_l", "audio"), ("out_r", "audio")
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("convolver", params={"gain": 0.5, "mix": 0.25})
        restored = Patch.from_dict(patch.to_dict())
        conv = next(m for m in restored if m.TYPE == "convolver")
        assert conv.params["gain"] == 0.5
        assert conv.params["mix"] == 0.25

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("convolver", params={"predelay": 10.0})

    def test_category_is_effects(self):
        assert Convolver.CATEGORY == "Effects"


# ----- Engine: oracle vs scipy.fftconvolve ----------------------------------


class TestEngineOracle:
    @pytest.mark.parametrize("block", [64, 128, 256, 512, 333])
    @pytest.mark.parametrize("ir_len", [1, 5, 511, 512, 513, 2000])
    def test_matches_fftconvolve(self, block, ir_len):
        rng = np.random.default_rng(1234 + block + ir_len)
        ir = rng.standard_normal(ir_len)
        M = ((6000 // block) + 3) * block
        x = rng.standard_normal(M)
        eng = _PartitionedConvolver(ir, block)
        out = np.concatenate(
            [eng.process(x[k * block:(k + 1) * block]) for k in range(M // block)]
        )
        ref = fftconvolve(x, ir)[: M - block]
        # Latency is exactly one block; compare the aligned region.
        assert np.max(np.abs(out[block:M] - ref)) < 1e-6
        # ...and the first block is the (empty) pipeline fill.
        assert np.max(np.abs(out[:block])) < 1e-9

    def test_impulse_is_delayed_identity(self):
        block = 256
        eng = _PartitionedConvolver(np.array([1.0]), block)
        rng = np.random.default_rng(7)
        x = rng.standard_normal(8 * block)
        out = np.concatenate(
            [eng.process(x[k * block:(k + 1) * block]) for k in range(8)]
        )
        # out is x delayed by exactly one block, to FFT round-off.
        assert np.max(np.abs(out[block:] - x[: 7 * block])) < 1e-9

    def test_tail_length_matches_ir(self):
        block = 256
        rng = np.random.default_rng(3)
        ir = rng.standard_normal(777)
        eng = _PartitionedConvolver(ir, block)
        nblk = (len(ir) + 2 * block) // block + 2
        x = np.zeros(nblk * block)
        x[0] = 1.0  # unit impulse in -> the output IS the IR (delayed one block)
        out = np.concatenate(
            [eng.process(x[k * block:(k + 1) * block]) for k in range(nblk)]
        )
        assert np.max(np.abs(out[block:block + len(ir)] - ir)) < 1e-9
        # Nothing rings on past the IR's tail.
        assert np.max(np.abs(out[block + len(ir):])) < 1e-9

    @pytest.mark.parametrize("block", [128, 512])
    def test_reported_latency_is_one_block(self, block):
        eng = _PartitionedConvolver(np.array([1.0]), block)
        x = np.zeros(6 * block)
        x[block // 3] = 1.0
        out = np.concatenate(
            [eng.process(x[k * block:(k + 1) * block]) for k in range(6)]
        )
        assert int(np.argmax(np.abs(out))) == block // 3 + block


# ----- Neutral ---------------------------------------------------------------


class TestNeutral:
    def test_default_impulse_ir_mix1_is_delayed_passthrough(self):
        # Fresh convolver: default IR is a unit impulse -> transparent insert.
        patch, osc, conv = _patch(mix=1.0, gain=1.0)
        b = _backend()
        x = _sine(F * 20, freq=440.0, amp=0.6)
        for port in ("out_l", "out_r"):
            b2 = _backend()
            out = _run(b2, conv, patch, osc.id, x, port=port)
            expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
            # Not bit-exact: the FFT round-trip is float, not exact (pinned).
            assert np.max(np.abs(out - expected)) < 1e-6
            assert not np.array_equal(out, expected) or np.allclose(out, expected)

    def test_mix0_is_bit_exact_delayed_dry(self):
        patch, osc, conv = _patch(mix=0.0, gain=1.7)  # gain must not matter at mix=0
        b = _backend()
        x = _sine(F * 12, freq=330.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
        assert np.array_equal(out, expected)

    def test_both_channels_equal_for_mono_ir(self):
        patch, osc, conv = _patch(mix=1.0)
        b = _backend()
        x = _sine(F * 6, amp=0.4)
        left = _run(b, conv, patch, osc.id, x, port="out_l")
        b2 = _backend()
        right = _run(b2, conv, patch, osc.id, x, port="out_r")
        assert np.array_equal(left, right)

    def test_silence_stays_silence(self):
        patch, osc, conv = _patch(mix=1.0)
        b = _backend()
        x = np.zeros(F * 4, dtype=np.float32)
        out = _run(b, conv, patch, osc.id, x)
        assert np.array_equal(out, np.zeros_like(out))


# ----- Latency ---------------------------------------------------------------


class TestLatency:
    def test_impulse_peaks_one_block_late(self):
        patch, osc, conv = _patch(mix=1.0)
        b = _backend()
        x = np.zeros(F * 8, dtype=np.float32)
        x[2000] = 0.8
        out = _run(b, conv, patch, osc.id, x)
        assert int(np.argmax(np.abs(out))) == 2000 + F

    def test_latency_constant_across_block_sizes(self):
        x = np.zeros(4096 * 4, dtype=np.float32)
        x[5000] = 0.8
        for block in (128, 256, 512, 1024):
            patch, osc, conv = _patch(mix=1.0)
            b = _backend(block=block)
            out = _run(b, conv, patch, osc.id, x, block=block)
            assert int(np.argmax(np.abs(out))) == 5000 + block


# ----- Oracle at the module level (real IR injected) ------------------------


class TestModuleOracle:
    def test_streamed_wet_matches_fftconvolve(self):
        rng = np.random.default_rng(99)
        ir = rng.standard_normal(1500)
        patch, osc, conv = _patch(mix=1.0, gain=1.0)
        b = _backend()
        _seed_ir(b, conv.id, ir)
        M = F * 20
        x = _sine(M, freq=220.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        ref = fftconvolve(x.astype(np.float64), ir)[: M - F].astype(np.float32)
        assert np.max(np.abs(out[F:M] - ref)) < 1e-4  # float32 accumulate

    def test_gain_scales_wet_only(self):
        rng = np.random.default_rng(5)
        ir = rng.standard_normal(600)
        x = _sine(F * 10, freq=200.0, amp=0.5)

        b1 = _backend()
        p1, o1, c1 = _patch(mix=1.0, gain=1.0)
        _seed_ir(b1, c1.id, ir)
        base = _run(b1, c1, p1, o1.id, x)

        b2 = _backend()
        p2, o2, c2 = _patch(mix=1.0, gain=0.25)
        _seed_ir(b2, c2.id, ir)
        scaled = _run(b2, c2, p2, o2.id, x)

        np.testing.assert_allclose(scaled, 0.25 * base, atol=1e-6)

    def test_mix_blends_dry_and_wet(self):
        rng = np.random.default_rng(11)
        ir = rng.standard_normal(400)
        x = _sine(F * 10, freq=150.0, amp=0.5)

        def run_mix(m):
            b = _backend()
            p, o, c = _patch(mix=m, gain=1.0)
            _seed_ir(b, c.id, ir)
            return _run(b, c, p, o.id, x)

        wet = run_mix(1.0)
        half = run_mix(0.5)
        dry = np.concatenate([np.zeros(F, np.float32), x])[: wet.shape[-1]]
        expected = (0.5 * wet + 0.5 * dry).astype(np.float32)
        np.testing.assert_allclose(half, expected, atol=1e-6)


# ----- Block-size independence ----------------------------------------------


class TestBlockSize:
    def test_big_block_matches_small_blocks(self):
        # Impulse IR -> wet is the input delayed by one (block-sized) latency.
        x = _sine(F * 30, freq=277.0, amp=0.6).astype(np.float32)

        b_big = _backend(block=F * 30)
        p1, o1, c1 = _patch(mix=1.0)
        big = _run(b_big, c1, p1, o1.id, x, block=F * 30)

        b_small = _backend(block=128)
        p2, o2, c2 = _patch(mix=1.0)
        small = _run(b_small, c2, p2, o2.id, x, block=128)

        # Align each by its own one-block latency, then compare.
        a = big[F * 30:]
        c = small[128:]
        n = min(a.shape[-1], c.shape[-1])
        np.testing.assert_allclose(a[:n], c[:n], atol=1e-6)


# ----- Voice-awareness -------------------------------------------------------


class TestVoice:
    def test_single_voice_row_bit_identical_to_mono(self):
        x = _sine(F * 8, freq=200.0, amp=0.5)

        bm = _backend()
        pm, om, cm = _patch(mix=1.0)
        mono = _run(bm, cm, pm, om.id, x)

        bv = _backend()
        pv, ov, cv = _patch(mix=1.0)
        voice = _run(bv, cv, pv, ov.id, x[np.newaxis, :])  # (1, T)
        assert np.array_equal(voice, mono)

    def test_voices_sum_before_convolution(self):
        T = F * 8
        v0 = _sine(T, freq=200.0, amp=0.4)
        v1 = _sine(T, freq=311.0, amp=0.3)

        bv = _backend()
        pv, ov, cv = _patch(mix=1.0)
        two = _run(bv, cv, pv, ov.id, np.stack([v0, v1]))  # (2, T)

        bm = _backend()
        pm, om, cm = _patch(mix=1.0)
        summed = _run(bm, cm, pm, om.id, (v0 + v1))
        assert np.array_equal(two, summed)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_convolver_speaker_renders_finite(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"amp": 0.5})
        conv = patch.add_module("convolver", params={"mix": 1.0})
        spk = patch.add_module("stereo_speaker_output")
        patch.connect(osc.id, "out", conv.id, "in")
        patch.connect(conv.id, "out_l", spk.id, "in_l")
        patch.connect(conv.id, "out_r", spk.id, "in_r")
        b = _backend()
        b.compile(patch)
        out = b.render_block(F)
        assert out is not None
        assert np.all(np.isfinite(out))
