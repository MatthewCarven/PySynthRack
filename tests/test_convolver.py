"""Tests for the Convolver (partitioned-FFT convolution — IR reverb / cab).

Slice 1 built the mono fixed-block core (default unit-impulse IR = transparent
insert). Slice 2 adds IR **file loading** (off-thread decode via the FilePlayer
WAV/ffmpeg path) and **true stereo** (a stereo IR convolves the mono-summed
input through its two channels into out_l / out_r).

Coverage:
  - Model: registration, defaults (incl. ``path``), ports, JSON round-trip,
    unknown-param rejection, category.
  - Engine (``_PartitionedConvolver``): block-for-block equivalence to
    ``scipy.signal.fftconvolve`` across block sizes and IR lengths; a unit
    impulse is a delayed identity; tail length equals IR length; one-block
    latency.
  - Neutral: default (no IR) unit-impulse at mix=1/gain=1 is a one-block
    delayed passthrough within 1e-6; mix=0 bit-exact delayed dry; silence.
  - Latency: one block; constant across block sizes.
  - Module oracle (seeded IR): streamed wet matches ``fftconvolve`` to
    float32 tolerance; gain trims wet only; mix blends.
  - Stereo: a seeded stereo IR gives out_l / out_r that each match their own
    channel's ``fftconvolve`` and differ from each other; a mono IR shares
    one engine and drives both channels identically.
  - File load: a real (temp) WAV IR decodes off-thread and convolves; mono
    file -> equal channels; empty / unreadable path -> transparent; a live
    ``path`` change adopts the new IR without blocking.
  - Block size: big == small blocks to FFT round-off.
  - Voice: single voice row ≡ mono; voices sum before the convolution.
  - Integration: osc -> convolver -> stereo speaker renders finite audio.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
from scipy.signal import fftconvolve
from scipy.io import wavfile

import pysynthrack.modules  # noqa: F401
from pysynthrack.core import Patch
from pysynthrack.audio import numpy_backend as _nbmod
from pysynthrack.audio.numpy_backend import (
    NumpyBackend, _PartitionedConvolver, _normalize_ir,
)
from pysynthrack.modules.convolver import Convolver

SR = 44100
F = 512
SEED = "__seed__"  # sentinel path for pre-seeded (no-loader) IR tests


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _patch(**params):
    patch = Patch()
    osc = patch.add_module("oscillator")
    conv = patch.add_module("convolver", params=params or None)
    patch.connect(osc.id, "out", conv.id, "in")
    return patch, osc, conv


def _seed_ir(b, module_id, ir_l, ir_r=None, path=SEED):
    """Pre-seed a convolver's state with a chosen IR (no loader/thread).

    The module's ``path`` param must equal ``path`` (default SEED) so the
    renderer treats the IR as already-loaded and never kicks a background
    load — keeping these tests deterministic.
    """
    left = np.asarray(ir_l, dtype=np.float64)
    right = left if ir_r is None else np.asarray(ir_r, dtype=np.float64)
    b._state[module_id] = {
        "engine_l": None, "engine_r": None, "ir_l": left, "ir_r": right,
        "loaded_path": path, "pending": None, "block": None, "dry_prev": None,
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


def _write_wav(data):
    """Write ``data`` (1D mono or (N, 2) stereo) to a temp WAV; return path."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "ir.wav")
    wavfile.write(path, SR, np.asarray(data, dtype=np.float32))
    return path


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        conv = Patch().add_module("convolver")
        assert isinstance(conv, Convolver)
        assert conv.params == {"path": "", "predelay": 0.0, "tone": 20000.0,
                               "gain": 1.0, "mix": 1.0}

    def test_ports_and_signal_kinds(self):
        conv = Patch().add_module("convolver")
        assert [(p.name, p.signal_kind) for p in conv.input_ports] == [("in", "audio")]
        assert [(p.name, p.signal_kind) for p in conv.output_ports] == [
            ("out_l", "audio"), ("out_r", "audio")
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("convolver", params={"path": "room.wav", "gain": 0.5,
                                              "mix": 0.25})
        restored = Patch.from_dict(patch.to_dict())
        conv = next(m for m in restored if m.TYPE == "convolver")
        assert conv.params["path"] == "room.wav"
        assert conv.params["gain"] == 0.5
        assert conv.params["mix"] == 0.25

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("convolver", params={"width": 10.0})

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
        assert np.max(np.abs(out[block:M] - ref)) < 1e-6
        assert np.max(np.abs(out[:block])) < 1e-9

    def test_impulse_is_delayed_identity(self):
        block = 256
        eng = _PartitionedConvolver(np.array([1.0]), block)
        rng = np.random.default_rng(7)
        x = rng.standard_normal(8 * block)
        out = np.concatenate(
            [eng.process(x[k * block:(k + 1) * block]) for k in range(8)]
        )
        assert np.max(np.abs(out[block:] - x[: 7 * block])) < 1e-9

    def test_tail_length_matches_ir(self):
        block = 256
        rng = np.random.default_rng(3)
        ir = rng.standard_normal(777)
        eng = _PartitionedConvolver(ir, block)
        nblk = (len(ir) + 2 * block) // block + 2
        x = np.zeros(nblk * block)
        x[0] = 1.0
        out = np.concatenate(
            [eng.process(x[k * block:(k + 1) * block]) for k in range(nblk)]
        )
        assert np.max(np.abs(out[block:block + len(ir)] - ir)) < 1e-9
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


# ----- Neutral (default: no IR -> transparent) ------------------------------


class TestNeutral:
    def test_default_no_ir_mix1_is_delayed_passthrough(self):
        patch, osc, conv = _patch(mix=1.0, gain=1.0)  # path defaults ""
        b = _backend()
        x = _sine(F * 20, freq=440.0, amp=0.6)
        out = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
        assert np.max(np.abs(out - expected)) < 1e-6

    def test_mix0_is_bit_exact_delayed_dry(self):
        patch, osc, conv = _patch(mix=0.0, gain=1.7)  # gain must not matter
        b = _backend()
        x = _sine(F * 12, freq=330.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
        assert np.array_equal(out, expected)

    def test_default_both_channels_equal(self):
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


# ----- Module oracle (seeded IR) --------------------------------------------


class TestModuleOracle:
    def test_streamed_wet_matches_fftconvolve(self):
        rng = np.random.default_rng(99)
        ir = rng.standard_normal(1500)
        patch, osc, conv = _patch(path=SEED, mix=1.0, gain=1.0)
        b = _backend()
        _seed_ir(b, conv.id, ir)
        M = F * 20
        x = _sine(M, freq=220.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        ref = fftconvolve(x.astype(np.float64), ir)[: M - F].astype(np.float32)
        assert np.max(np.abs(out[F:M] - ref)) < 1e-4

    def test_gain_scales_wet_only(self):
        rng = np.random.default_rng(5)
        ir = rng.standard_normal(600)
        x = _sine(F * 10, freq=200.0, amp=0.5)

        b1 = _backend()
        p1, o1, c1 = _patch(path=SEED, mix=1.0, gain=1.0)
        _seed_ir(b1, c1.id, ir)
        base = _run(b1, c1, p1, o1.id, x)

        b2 = _backend()
        p2, o2, c2 = _patch(path=SEED, mix=1.0, gain=0.25)
        _seed_ir(b2, c2.id, ir)
        scaled = _run(b2, c2, p2, o2.id, x)

        np.testing.assert_allclose(scaled, 0.25 * base, atol=1e-6)

    def test_mix_blends_dry_and_wet(self):
        rng = np.random.default_rng(11)
        ir = rng.standard_normal(400)
        x = _sine(F * 10, freq=150.0, amp=0.5)

        def run_mix(m):
            b = _backend()
            p, o, c = _patch(path=SEED, mix=m, gain=1.0)
            _seed_ir(b, c.id, ir)
            return _run(b, c, p, o.id, x)

        wet = run_mix(1.0)
        half = run_mix(0.5)
        dry = np.concatenate([np.zeros(F, np.float32), x])[: wet.shape[-1]]
        expected = (0.5 * wet + 0.5 * dry).astype(np.float32)
        np.testing.assert_allclose(half, expected, atol=1e-6)


# ----- Stereo ----------------------------------------------------------------


class TestStereo:
    def test_stereo_ir_channels_match_per_channel_fftconvolve(self):
        rng = np.random.default_rng(42)
        ir_l = rng.standard_normal(900)
        ir_r = rng.standard_normal(1300)
        M = F * 20
        x = _sine(M, freq=220.0, amp=0.5)

        b = _backend()
        p, o, c = _patch(path=SEED, mix=1.0)
        _seed_ir(b, c.id, ir_l, ir_r)
        left = _run(b, c, p, o.id, x, port="out_l")

        b2 = _backend()
        p2, o2, c2 = _patch(path=SEED, mix=1.0)
        _seed_ir(b2, c2.id, ir_l, ir_r)
        right = _run(b2, c2, p2, o2.id, x, port="out_r")

        refL = fftconvolve(x.astype(np.float64), ir_l)[: M - F].astype(np.float32)
        refR = fftconvolve(x.astype(np.float64), ir_r)[: M - F].astype(np.float32)
        assert np.max(np.abs(left[F:M] - refL)) < 1e-4
        assert np.max(np.abs(right[F:M] - refR)) < 1e-4
        # A genuine stereo image: the two channels are clearly different.
        assert np.max(np.abs(left - right)) > 0.05

    def test_mono_ir_shares_one_engine_and_equal_channels(self):
        rng = np.random.default_rng(8)
        ir = rng.standard_normal(500)
        b = _backend()
        p, o, c = _patch(path=SEED, mix=1.0)
        _seed_ir(b, c.id, ir, ir)  # identical channels
        x = _sine(F * 6, freq=200.0, amp=0.5)
        res = b._render_convolver(c, F, {(o.id, "out"): x[:F]}, p)
        st = b._state[c.id]
        assert st["engine_r"] is st["engine_l"]  # convolved once
        assert np.array_equal(res["out_l"], res["out_r"])


# ----- File load (off-thread decode) ----------------------------------------


class TestFileLoad:
    def test_stereo_wav_loads_and_convolves(self):
        rng = np.random.default_rng(3)
        ir = (rng.standard_normal((1200, 2)) * 0.3).astype(np.float32)
        wav = _write_wav(ir)
        patch, osc, conv = _patch(path=wav, mix=1.0)
        b = _backend()
        b.compile(patch)                       # kicks the background loader
        assert b.wait_for_ir_loads(timeout=10)
        M = F * 20
        x = _sine(M, freq=220.0, amp=0.5)
        left = _run(b, conv, patch, osc.id, x, port="out_l")
        # The loader energy-normalises the IR, so the reference must too.
        nl, nr, _ = _normalize_ir(ir[:, 0].astype(np.float64),
                                  ir[:, 1].astype(np.float64))
        refL = fftconvolve(x.astype(np.float64), nl)[: M - F].astype(np.float32)
        assert np.max(np.abs(left[F:M] - refL)) < 1e-3

    def test_mono_wav_gives_equal_channels(self):
        rng = np.random.default_rng(4)
        ir = (rng.standard_normal(800) * 0.3).astype(np.float32)  # mono
        wav = _write_wav(ir)
        patch, osc, conv = _patch(path=wav, mix=1.0)
        b = _backend()
        b.compile(patch)
        assert b.wait_for_ir_loads(timeout=10)
        x = _sine(F * 8, freq=200.0, amp=0.5)
        left = _run(b, conv, patch, osc.id, x, port="out_l")
        b2 = _backend()
        b2.compile(patch)
        assert b2.wait_for_ir_loads(timeout=10)
        right = _run(b2, conv, patch, osc.id, x, port="out_r")
        assert np.array_equal(left, right)

    def test_missing_path_is_transparent(self):
        patch, osc, conv = _patch(path="/no/such/ir_file_12345.wav", mix=1.0)
        b = _backend()
        b.compile(patch)
        b.wait_for_ir_loads(timeout=10)  # load fails -> stays transparent
        x = _sine(F * 8, freq=330.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
        assert np.max(np.abs(out - expected)) < 1e-6

    def test_live_path_change_adopts_new_ir(self):
        rng = np.random.default_rng(6)
        ir = (rng.standard_normal((700, 2)) * 0.3).astype(np.float32)
        wav = _write_wav(ir)
        patch, osc, conv = _patch(path="", mix=1.0)
        b = _backend()
        x = _sine(F * 6, freq=210.0, amp=0.5)

        # Transparent while no IR is loaded.
        out_before = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out_before.shape[-1]]
        assert np.max(np.abs(out_before - expected)) < 1e-6

        # Point at an IR live; kick (one block), wait, then it convolves.
        conv.set_param("path", wav)
        b._render_convolver(conv, F, {(osc.id, "out"): np.zeros(F, np.float32)}, patch)
        assert b.wait_for_ir_loads(timeout=10)
        out_after = _run(b, conv, patch, osc.id, x)
        # The IR is now audibly applied — no longer a clean passthrough.
        assert np.max(np.abs(out_after[F:] - x[: out_after.shape[-1] - F])) > 0.05


# ----- Block-size independence ----------------------------------------------


class TestBlockSize:
    def test_big_block_matches_small_blocks(self):
        x = _sine(F * 30, freq=277.0, amp=0.6).astype(np.float32)

        b_big = _backend(block=F * 30)
        p1, o1, c1 = _patch(mix=1.0)
        big = _run(b_big, c1, p1, o1.id, x, block=F * 30)

        b_small = _backend(block=128)
        p2, o2, c2 = _patch(mix=1.0)
        small = _run(b_small, c2, p2, o2.id, x, block=128)

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
        voice = _run(bv, cv, pv, ov.id, x[np.newaxis, :])
        assert np.array_equal(voice, mono)

    def test_voices_sum_before_convolution(self):
        T = F * 8
        v0 = _sine(T, freq=200.0, amp=0.4)
        v1 = _sine(T, freq=311.0, amp=0.3)

        bv = _backend()
        pv, ov, cv = _patch(mix=1.0)
        two = _run(bv, cv, pv, ov.id, np.stack([v0, v1]))

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


# ----- Wet shaping: predelay + tone -----------------------------------------


class TestShaping:
    def test_predelay_delays_wet_by_samples(self):
        pd_ms = 10.0
        D = int(round(pd_ms * 1e-3 * SR))
        patch, osc, conv = _patch(mix=1.0, predelay=pd_ms)  # default impulse IR
        b = _backend()
        x = np.zeros(F * 8, dtype=np.float32)
        x[2000] = 0.8
        out = _run(b, conv, patch, osc.id, x)
        # one-block engine latency + the predelay
        assert int(np.argmax(np.abs(out))) == 2000 + F + D

    def test_tone_max_is_transparent(self):
        patch, osc, conv = _patch(mix=1.0, tone=20000.0)
        b = _backend()
        x = _sine(F * 12, freq=500.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
        assert np.max(np.abs(out - expected)) < 1e-6

    def test_tone_low_darkens_high_frequencies(self):
        hf = _sine(F * 12, freq=9000.0, amp=0.5)
        b_on = _backend()
        p1, o1, c1 = _patch(mix=1.0, tone=1500.0)
        dark = _run(b_on, c1, p1, o1.id, hf)
        b_off = _backend()
        p2, o2, c2 = _patch(mix=1.0, tone=20000.0)
        bright = _run(b_off, c2, p2, o2.id, hf)
        # A 1.5 kHz low-pass should strongly attenuate a 9 kHz tone.
        assert np.max(np.abs(dark)) < 0.5 * np.max(np.abs(bright))

    def test_shaping_does_not_touch_mix0(self):
        # Predelay + tone are wet-only, so mix=0 stays a bit-exact dry bypass.
        patch, osc, conv = _patch(mix=0.0, predelay=25.0, tone=1200.0, gain=1.9)
        b = _backend()
        x = _sine(F * 10, freq=330.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
        assert np.array_equal(out, expected)


# ----- Normalise on load + length cap ---------------------------------------


class TestNormalizeAndCap:
    def test_hot_ir_is_energy_normalised(self):
        rng = np.random.default_rng(0)
        ir = (rng.standard_normal((1000, 2)) * 5.0).astype(np.float32)  # very hot
        wav = _write_wav(ir)
        patch, osc, conv = _patch(path=wav, mix=1.0)
        b = _backend()
        b.compile(patch)
        assert b.wait_for_ir_loads(timeout=10)
        M = F * 20
        x = _sine(M, freq=220.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        nl, nr, _ = _normalize_ir(ir[:, 0].astype(np.float64),
                                  ir[:, 1].astype(np.float64))
        refL = fftconvolve(x.astype(np.float64), nl)[: M - F].astype(np.float32)
        assert np.max(np.abs(out[F:M] - refL)) < 1e-3
        # Normalisation kept the hot IR from blowing up the output.
        assert np.max(np.abs(out)) < 2.0

    def test_normalised_impulse_ir_is_transparent(self):
        # A single-spike IR normalises to a unit impulse -> transparent insert.
        ir = np.zeros((F, 2), dtype=np.float32)
        ir[0, :] = 0.5
        wav = _write_wav(ir)
        patch, osc, conv = _patch(path=wav, mix=1.0)
        b = _backend()
        b.compile(patch)
        assert b.wait_for_ir_loads(timeout=10)
        x = _sine(F * 8, freq=440.0, amp=0.5)
        out = _run(b, conv, patch, osc.id, x)
        expected = np.concatenate([np.zeros(F, np.float32), x])[: out.shape[-1]]
        assert np.max(np.abs(out - expected)) < 1e-6

    def test_long_ir_is_length_capped(self):
        rng = np.random.default_rng(1)
        ir = (rng.standard_normal((8000, 2)) * 0.2).astype(np.float32)
        wav = _write_wav(ir)
        patch, osc, conv = _patch(path=wav, mix=1.0)
        b = _backend()
        saved = _nbmod._IR_MAX_SECONDS
        _nbmod._IR_MAX_SECONDS = 0.05  # 2205 samples
        try:
            b.compile(patch)
            assert b.wait_for_ir_loads(timeout=10)
            b._render_convolver(conv, F, {(osc.id, "out"): np.zeros(F, np.float32)},
                                patch)  # trigger adoption
        finally:
            _nbmod._IR_MAX_SECONDS = saved
        assert b._state[conv.id]["ir_l"].shape[0] == int(0.05 * SR)
