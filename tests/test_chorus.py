"""Tests for the Chorus (detuned multi-voice stereo thickener).

Coverage:
  - Model: registration, defaults, ports/kinds (audio ``in`` + cv
    ``rate_cv`` -> ``out_l`` / ``out_r`` audio), JSON round-trip,
    unknown-param rejection, and the signal-kind type walls.
  - DSP: disconnected -> silence; ``mix=0`` is a bit-exact dry
    passthrough on both channels; an impulse produces delayed taps;
    ``depth=0`` is a static comb while ``depth>0`` modulates; more
    voices changes the texture; output stays finite/bounded; a voice
    (2D) input is summed to mono.
  - Block independence: the chunked, feedback-free engine gives bit-
    identical output at any block size (512 vs 4096 vs an odd size).
  - Stereo: the two channels are decorrelated with >= 2 voices, and
    collapse together with a single voice.
  - CV: ``rate_cv`` alters the sweep; an all-zero ``rate_cv`` is a noop.
  - Integration: osc -> chorus -> L/R speakers renders audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.chorus import Chorus

SR = 44100
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    ch = patch.add_module("chorus", params=params or {})
    patch.connect(src.id, "out", ch.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, ch, b


def _run(b, patch, src, ch, signal, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        o = b._render_chorus(ch, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _rig_cv(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    ch = patch.add_module("chorus", params=params or {})
    patch.connect(src.id, "out", ch.id, "in")
    patch.connect(lfo.id, "cv", ch.id, "rate_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, lfo, ch, b


def _run_cv(b, patch, src, lfo, ch, signal, cv, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {
            (src.id, "out"): signal[..., sl].astype(np.float32),
            (lfo.id, "cv"): cv[..., sl].astype(np.float32),
        }
        o = b._render_chorus(ch, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _impulse(n):
    x = np.zeros(n, dtype=np.float32)
    x[0] = 1.0
    return x


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        ch = Patch().add_module("chorus")
        assert isinstance(ch, Chorus)
        assert ch.params == {
            "rate": 0.6,
            "depth": 0.5,
            "voices": 3,
            "mix": 0.5,
            "cv_depth": 1.0,
        }

    def test_voices_default_is_int(self):
        ch = Patch().add_module("chorus")
        assert isinstance(ch.params["voices"], int)

    def test_ports_and_kinds(self):
        ch = Patch().add_module("chorus")
        assert [(p.name, p.signal_kind) for p in ch.input_ports] == [
            ("in", "audio"),
            ("rate_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in ch.output_ports] == [
            ("out_l", "audio"),
            ("out_r", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("chorus", params={"rate": 2.5, "voices": 4, "mix": 0.7})
        restored = Patch.from_dict(patch.to_dict())
        ch = next(m for m in restored if m.TYPE == "chorus")
        assert ch.params["rate"] == 2.5
        assert ch.params["voices"] == 4
        assert ch.params["mix"] == 0.7

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("chorus", params={"feedback": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ch = patch.add_module("chorus")
        patch.connect(osc.id, "out", ch.id, "in")  # no raise

    def test_cv_into_rate_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ch = patch.add_module("chorus")
        patch.connect(lfo.id, "cv", ch.id, "rate_cv")  # no raise

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ch = patch.add_module("chorus")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ch.id, "in")

    def test_audio_into_rate_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ch = patch.add_module("chorus")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", ch.id, "rate_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        ch = patch.add_module("chorus")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(ch.id, "out_l", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        ch = patch.add_module("chorus")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_chorus(ch, F, {}, patch)
        assert not np.any(o["out_l"]) and not np.any(o["out_r"])
        assert o["out_l"].shape == (F,)

    def test_frames_zero_empty(self):
        patch, src, ch, b = _rig()
        o = b._render_chorus(ch, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch)
        assert o["out_l"].shape == (0,) and o["out_r"].shape == (0,)

    def test_mix_zero_exact_dry_passthrough(self):
        patch, src, ch, b = _rig({"mix": 0.0, "depth": 0.8, "voices": 4})
        x = (np.random.randn(F * 4)).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert np.array_equal(lo, x[: len(lo)])
        assert np.array_equal(r, x[: len(r)])

    def test_output_is_float32(self):
        patch, src, ch, b = _rig({"mix": 0.6})
        lo, r = _run(b, patch, src, ch, np.random.randn(F * 2).astype(np.float32))
        assert lo.dtype == np.float32 and r.dtype == np.float32

    def test_impulse_produces_delayed_taps(self):
        # A dry impulse at t=0; at mix=1 the output is only the delayed
        # wet taps, which land around the ~12..24 ms base delays, i.e.
        # clearly after the input sample and before ~40 ms.
        patch, src, ch, b = _rig({"mix": 1.0, "depth": 0.5, "voices": 3})
        lo, _ = _run(b, patch, src, ch, _impulse(F * 8))
        lo_start = int(0.002 * SR)   # 2 ms
        lo_end = int(0.040 * SR)     # 40 ms
        assert np.max(np.abs(lo[lo_start:lo_end])) > 1e-3
        assert np.all(np.isfinite(lo))

    def test_depth_zero_static_but_not_dry(self):
        # depth=0 -> no LFO sweep -> a static comb; with mix>0 it is not
        # the dry signal, and it differs from a modulated (depth>0) render.
        x = (np.random.randn(F * 4)).astype(np.float32)
        p0, s0, c0, b0 = _rig({"depth": 0.0, "mix": 0.6, "voices": 3, "rate": 1.5})
        l0, _ = _run(b0, p0, s0, c0, x)
        pm, sm, cm, bm = _rig({"depth": 0.6, "mix": 0.6, "voices": 3, "rate": 1.5})
        lm, _ = _run(bm, pm, sm, cm, x)
        assert not np.array_equal(l0, x[: len(l0)])       # comb, not dry
        assert not np.allclose(l0, lm, atol=1e-6)         # modulation matters

    def test_more_voices_changes_texture(self):
        x = (np.random.randn(F * 4)).astype(np.float32)
        p1, s1, c1, b1 = _rig({"voices": 1, "depth": 0.6, "mix": 0.7, "rate": 1.2})
        l1, _ = _run(b1, p1, s1, c1, x)
        p4, s4, c4, b4 = _rig({"voices": 4, "depth": 0.6, "mix": 0.7, "rate": 1.2})
        l4, _ = _run(b4, p4, s4, c4, x)
        assert not np.allclose(l1, l4, atol=1e-6)

    def test_finite_and_bounded_at_extremes(self):
        patch, src, ch, b = _rig({"voices": 6, "depth": 1.0, "mix": 1.0, "rate": 10.0})
        x = (np.random.randn(2 * SR) * 0.5).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))
        assert np.max(np.abs(lo)) < 8.0 and np.max(np.abs(r)) < 8.0

    def test_voice_input_summed_to_mono(self):
        patch, src, ch, b = _rig({"mix": 1.0, "depth": 0.5})
        v = np.random.randn(3, F).astype(np.float32)
        o = b._render_chorus(ch, F, {(src.id, "out"): v}, patch)
        assert o["out_l"].shape == (F,) and o["out_r"].shape == (F,)
        assert np.all(np.isfinite(o["out_l"]))


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def test_output_independent_of_block_size(self):
        x = (np.sin(2 * np.pi * 220 * np.arange(12000) / SR) * 0.4).astype(np.float32)
        params = {"rate": 2.0, "depth": 0.7, "voices": 3, "mix": 0.5}
        pa, sa, ca, ba = _rig(params, block=512)
        la, ra = _run(ba, pa, sa, ca, x, block=512)
        pb, sb, cb, bb = _rig(params, block=4096)
        lb, rb = _run(bb, pb, sb, cb, x, block=4096)
        pc, sc, cc, bc = _rig(params, block=333)
        lc, rc = _run(bc, pc, sc, cc, x, block=333)
        m = min(len(la), len(lb), len(lc))
        assert np.array_equal(la[:m], lb[:m])
        assert np.array_equal(la[:m], lc[:m])
        assert np.array_equal(ra[:m], rb[:m])


# ----- Stereo ----------------------------------------------------------------


class TestStereo:
    def test_channels_are_decorrelated(self):
        patch, src, ch, b = _rig({"voices": 3, "depth": 0.6, "mix": 0.7, "rate": 1.5})
        x = (np.random.randn(SR) * 0.3).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert not np.array_equal(lo, r)
        corr = np.corrcoef(lo[3000:], r[3000:])[0, 1]
        assert abs(corr) < 0.99

    def test_single_voice_channels_equal(self):
        # One voice sits dead centre, so both channels are identical.
        patch, src, ch, b = _rig({"voices": 1, "depth": 0.6, "mix": 0.8, "rate": 1.5})
        x = (np.random.randn(F * 4) * 0.3).astype(np.float32)
        lo, r = _run(b, patch, src, ch, x)
        assert np.allclose(lo, r, atol=1e-6)


# ----- CV --------------------------------------------------------------------


class TestCV:
    def test_rate_cv_alters_output(self):
        x = (np.random.randn(F * 6) * 0.3).astype(np.float32)
        params = {"rate": 1.0, "depth": 0.6, "voices": 3, "mix": 0.7, "cv_depth": 2.0}
        # +1.0 unit constant CV -> +2 octaves of LFO rate.
        p, s, lfo, ch, b = _rig_cv(params)
        l_hi, _ = _run_cv(b, p, s, lfo, ch, x, np.ones_like(x))
        # Same patch, zero CV.
        p0, s0, lfo0, ch0, b0 = _rig_cv(params)
        l_zero, _ = _run_cv(b0, p0, s0, lfo0, ch0, x, np.zeros_like(x))
        assert not np.allclose(l_hi, l_zero, atol=1e-6)

    def test_zero_rate_cv_is_noop(self):
        x = (np.random.randn(F * 6) * 0.3).astype(np.float32)
        params = {"rate": 1.0, "depth": 0.6, "voices": 3, "mix": 0.7, "cv_depth": 2.0}
        p, s, lfo, ch, b = _rig_cv(params)
        l_cv, _ = _run_cv(b, p, s, lfo, ch, x, np.zeros_like(x))
        pn, sn, cn, bn = _rig(params)
        l_no, _ = _run(bn, pn, sn, cn, x)
        assert np.array_equal(l_cv, l_no)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_chorus_stereo_speakers(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0})
        ch = patch.add_module("chorus", params={"depth": 0.6, "mix": 0.5, "voices": 3})
        spk_l = patch.add_module("left_speaker_output")
        spk_r = patch.add_module("right_speaker_output")
        patch.connect(osc.id, "out", ch.id, "in")
        patch.connect(ch.id, "out_l", spk_l.id, "in")
        patch.connect(ch.id, "out_r", spk_r.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(60):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
