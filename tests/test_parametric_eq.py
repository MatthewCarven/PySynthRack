"""Tests for the ParametricEQ module (4-band peaking parametric EQ).

Coverage:
  - Model: registration, defaults (4 bands x freq/gain/Q), EQ_BANDS,
    ports/signal kinds (mono audio in -> audio out), JSON round-trip,
    unknown-param rejection, type walls (audio->audio legal,
    cv->audio-in illegal, audio-out->cv-in illegal).
  - Coeffs: RBJ peaking math vs a hand computation; 0 dB band yields
    identity coefficients (exact passthrough); freq/Q clamping.
  - Mono DSP: shape/dtype, flat (all 0 dB) is bit-exact transparent,
    +12/-12 dB boost/cut measured at the band centre, Q controls
    width, two bands act independently, block-stitch equivalence,
    frames==0, disconnected input -> silence.
  - Voice DSP: (V, F) shape, each row equals the mono result, voice
    block-stitch state carry, mono<->voice state reinit.
  - Integration: noise -> eq -> speaker renders finite audio via
    render_block.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.parametric_eq import EQ_BANDS, ParametricEQ

SR, F = 44100, 512


def _backend():
    return NumpyBackend(sample_rate=SR, block_size=F)


def _sine(freq, n, amp=0.3):
    t = np.arange(n) / SR
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _steady_gain_db(params, probe_freq, blocks=40):
    """Push a probe sine through a fresh EQ and measure tail gain in dB."""
    patch = Patch()
    eq = patch.add_module("parametric_eq", params=params)
    backend = _backend()
    backend.compile(patch)
    sig = _sine(probe_freq, blocks * F)
    chunks = [
        backend._render_parametric_eq_mono(eq, F, sig[i * F:(i + 1) * F])
        for i in range(blocks)
    ]
    out = np.concatenate(chunks)[-8192:]
    ref = sig[:8192]
    return 20.0 * np.log10(
        np.sqrt(np.mean(out**2)) / np.sqrt(np.mean(ref**2))
    )


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        eq = patch.add_module("parametric_eq")
        assert isinstance(eq, ParametricEQ)
        assert eq.params == {
            "band1_freq": 25.0, "band1_gain": 0.0, "band1_q": 1.0,
            "band2_freq": 50.0, "band2_gain": 0.0, "band2_q": 1.0,
            "band3_freq": 100.0, "band3_gain": 0.0, "band3_q": 1.0,
            "band4_freq": 250.0, "band4_gain": 0.0, "band4_q": 1.0,
        }

    def test_band_count(self):
        assert EQ_BANDS == 4
        assert ParametricEQ.EQ_BANDS == 4

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        eq = patch.add_module("parametric_eq")
        assert [(p.name, p.signal_kind) for p in eq.input_ports] == [
            ("in", "audio")
        ]
        assert [(p.name, p.signal_kind) for p in eq.output_ports] == [
            ("out", "audio")
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "parametric_eq",
            params={"band2_freq": 80.0, "band2_gain": -6.0, "band3_q": 4.0},
        )
        restored = Patch.from_dict(patch.to_dict())
        eq = next(m for m in restored if m.TYPE == "parametric_eq")
        assert eq.params["band2_freq"] == 80.0
        assert eq.params["band2_gain"] == -6.0
        assert eq.params["band3_q"] == 4.0

    def test_unknown_param_rejected(self):
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("parametric_eq", params={"band5_freq": 5000.0})

    def test_audio_into_audio_accepted(self):
        patch = Patch()
        nz = patch.add_module("noise")
        eq = patch.add_module("parametric_eq")
        spk = patch.add_module("speaker_output")
        patch.connect(nz.id, "out", eq.id, "in")   # audio -> audio
        patch.connect(eq.id, "out", spk.id, "in")  # audio -> audio sink

    def test_cv_into_audio_input_rejected(self):
        patch = Patch()
        nz = patch.add_module("noise")
        eq = patch.add_module("parametric_eq")
        with pytest.raises(ValueError):
            patch.connect(nz.id, "cv", eq.id, "in")  # cv -> audio

    def test_audio_out_into_cv_input_rejected(self):
        patch = Patch()
        eq = patch.add_module("parametric_eq")
        sh = patch.add_module("sample_hold")
        with pytest.raises(ValueError):
            patch.connect(eq.id, "out", sh.id, "in")  # audio -> cv


# ----- Coefficients ----------------------------------------------------------


class TestCoeffs:
    def test_zero_db_is_identity(self):
        b0, b1, b2, a1n, a2n = _backend()._peq_coeffs([100.0], [0.0], [1.0])
        # b == a => transfer function is 1 at every frequency.
        assert np.isclose(b0[0], 1.0)
        assert np.isclose(b1[0], a1n[0])
        assert np.isclose(b2[0], a2n[0])

    def test_matches_hand_rbj(self):
        f0, gain, q = 1000.0, 9.0, 2.0
        b0, b1, b2, a1n, a2n = _backend()._peq_coeffs([f0], [gain], [q])
        A = 10.0 ** (gain / 40.0)
        w0 = 2.0 * np.pi * f0 / SR
        alpha = np.sin(w0) / (2.0 * q)
        cw = np.cos(w0)
        a0 = 1.0 + alpha / A
        assert np.isclose(b0[0], (1.0 + alpha * A) / a0)
        assert np.isclose(b1[0], (-2.0 * cw) / a0)
        assert np.isclose(b2[0], (1.0 - alpha * A) / a0)
        assert np.isclose(a1n[0], (-2.0 * cw) / a0)
        assert np.isclose(a2n[0], (1.0 - alpha / A) / a0)

    def test_vectorized_over_bands(self):
        b0, b1, b2, a1n, a2n = _backend()._peq_coeffs(
            [50.0, 500.0, 5000.0], [3.0, -3.0, 6.0], [0.7, 1.0, 4.0]
        )
        assert b0.shape == (3,)

    def test_freq_and_q_clamped(self):
        # freq below 20 and above 0.45*sr, Q outside (0.1, 20) must not
        # produce Na/inf coefficients.
        b0, b1, b2, a1n, a2n = _backend()._peq_coeffs(
            [1.0, 40000.0], [12.0, 12.0], [0.0, 1000.0]
        )
        for arr in (b0, b1, b2, a1n, a2n):
            assert np.all(np.isfinite(arr))


# ----- Mono DSP --------------------------------------------------------------


class TestMonoDSP:
    def test_shape_and_dtype(self):
        eq = Patch().add_module("parametric_eq")
        out = _backend()._render_parametric_eq_mono(eq, F, _sine(440, F))
        assert out.shape == (F,)
        assert out.dtype == np.float32

    def test_flat_is_transparent(self):
        # All four bands at 0 dB -> bit-exact passthrough (cascade of
        # identity biquads).
        patch = Patch()
        nz = patch.add_module("noise", params={"amp": 1.0})
        eq = patch.add_module("parametric_eq")
        patch.connect(nz.id, "out", eq.id, "in")
        backend = _backend()
        backend.compile(patch)
        np.random.seed(3)
        x = backend._render_noise(nz, F, {}, patch)["out"]
        out = backend._render_parametric_eq(eq, F, {(nz.id, "out"): x}, patch)
        assert np.max(np.abs(out - x)) < 1e-6

    def test_boost_at_centre(self):
        g = _steady_gain_db(
            {"band1_freq": 1000.0, "band1_gain": 12.0, "band1_q": 1.0}, 1000.0
        )
        assert abs(g - 12.0) < 0.5

    def test_cut_at_centre(self):
        g = _steady_gain_db(
            {"band1_freq": 1000.0, "band1_gain": -12.0, "band1_q": 1.0}, 1000.0
        )
        assert abs(g + 12.0) < 0.5

    def test_far_band_unaffected(self):
        # A boost at 1 kHz should barely touch 60 Hz.
        g = _steady_gain_db(
            {"band1_freq": 1000.0, "band1_gain": 12.0, "band1_q": 2.0}, 60.0
        )
        assert abs(g) < 1.0

    def test_q_controls_width(self):
        # An octave above the centre: a broad (low-Q) bell leaks more
        # boost than a narrow (high-Q) one.
        wide = _steady_gain_db(
            {"band1_freq": 1000.0, "band1_gain": 12.0, "band1_q": 0.5}, 2000.0
        )
        narrow = _steady_gain_db(
            {"band1_freq": 1000.0, "band1_gain": 12.0, "band1_q": 5.0}, 2000.0
        )
        assert wide > narrow + 3.0

    def test_two_bands_independent(self):
        # Boost at 300 and cut at 3000; probe each frequency.
        params = {
            "band1_freq": 300.0, "band1_gain": 9.0, "band1_q": 2.0,
            "band2_freq": 3000.0, "band2_gain": -9.0, "band2_q": 2.0,
        }
        assert _steady_gain_db(params, 300.0) > 6.0
        assert _steady_gain_db(params, 3000.0) < -6.0

    def test_block_stitch_equivalence(self):
        # Two 512 blocks must equal one 1024 block (state carries
        # exactly across the boundary).
        params = {"band1_gain": 9.0, "band3_gain": -6.0}
        big = (np.random.RandomState(7).rand(1024).astype(np.float32) * 2 - 1)

        pa = Patch(); ea = pa.add_module("parametric_eq", params=params)
        ba = _backend(); ba.compile(pa)
        two = np.concatenate([
            ba._render_parametric_eq_mono(ea, 512, big[:512]),
            ba._render_parametric_eq_mono(ea, 512, big[512:]),
        ])

        pb = Patch(); eb = pb.add_module("parametric_eq", params=params)
        bb = _backend(); bb.compile(pb)
        whole = bb._render_parametric_eq_mono(eb, 1024, big)

        assert np.max(np.abs(two - whole)) < 1e-6

    def test_frames_zero(self):
        eq = Patch().add_module("parametric_eq")
        out = _backend()._render_parametric_eq_mono(eq, 0, np.empty(0, np.float32))
        assert out.shape == (0,)

    def test_disconnected_input_is_silence(self):
        patch = Patch()
        eq = patch.add_module("parametric_eq")
        backend = _backend()
        backend.compile(patch)
        out = backend._render_parametric_eq(eq, 256, {}, patch)
        assert out.shape == (256,)
        assert np.array_equal(out, np.zeros(256, dtype=np.float32))


# ----- Voice DSP -------------------------------------------------------------


class TestVoiceDSP:
    def test_voice_shape(self):
        eq = Patch().add_module("parametric_eq", params={"band1_gain": 6.0})
        src = np.stack([_sine(440, F) for _ in range(4)]).astype(np.float32)
        out = _backend()._render_parametric_eq_voice(eq, F, src)
        assert out.shape == (4, F)
        assert out.dtype == np.float32

    def test_voice_rows_match_mono(self):
        params = {"band1_freq": 1000.0, "band1_gain": 12.0, "band1_q": 1.5}
        V = 4
        src = np.stack(
            [_sine(1000, F, amp=0.2 + 0.1 * r) for r in range(V)]
        ).astype(np.float32)

        pv = Patch(); ev = pv.add_module("parametric_eq", params=params)
        bv = _backend(); bv.compile(pv)
        ov = bv._render_parametric_eq_voice(ev, F, src)

        for r in range(V):
            pm = Patch(); em = pm.add_module("parametric_eq", params=params)
            bm = _backend(); bm.compile(pm)
            om = bm._render_parametric_eq_mono(em, F, src[r])
            assert np.max(np.abs(ov[r] - om)) < 1e-6

    def test_voice_block_stitch(self):
        params = {"band1_gain": 9.0}
        V = 3
        big = (np.random.RandomState(11).rand(V, 1024).astype(np.float32) * 2 - 1)

        pa = Patch(); ea = pa.add_module("parametric_eq", params=params)
        ba = _backend(); ba.compile(pa)
        two = np.concatenate([
            ba._render_parametric_eq_voice(ea, 512, big[:, :512]),
            ba._render_parametric_eq_voice(ea, 512, big[:, 512:]),
        ], axis=1)

        pb = Patch(); eb = pb.add_module("parametric_eq", params=params)
        bb = _backend(); bb.compile(pb)
        whole = bb._render_parametric_eq_voice(eb, 1024, big)

        assert np.max(np.abs(two - whole)) < 1e-6

    def test_mono_voice_state_reinit(self):
        # Calling mono, then voice, then mono on the same module must not
        # carry stale state of the wrong shape.
        patch = Patch()
        eq = patch.add_module("parametric_eq", params={"band1_gain": 6.0})
        backend = _backend()
        backend.compile(patch)
        mono = _sine(440, F)
        voice = np.stack([_sine(440, F) for _ in range(4)]).astype(np.float32)
        o1 = backend._render_parametric_eq_mono(eq, F, mono)
        ov = backend._render_parametric_eq_voice(eq, F, voice)
        o2 = backend._render_parametric_eq_mono(eq, F, mono)
        assert o1.shape == (F,)
        assert ov.shape == (4, F)
        assert o2.shape == (F,)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_noise_eq_speaker_renders(self):
        patch = Patch()
        nz = patch.add_module("noise", params={"amp": 0.5})
        eq = patch.add_module(
            "parametric_eq", params={"band1_freq": 80.0, "band1_gain": 12.0}
        )
        spk = patch.add_module("speaker_output")
        patch.connect(nz.id, "out", eq.id, "in")
        patch.connect(eq.id, "out", spk.id, "in")
        backend = _backend()
        backend.compile(patch)
        block = backend.render_block(F)
        assert block is not None
        assert np.all(np.isfinite(block))
        assert np.abs(block).max() > 0.0
