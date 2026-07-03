"""Tests for the Compressor (feed-forward dynamics + external sidechain).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in +
    sidechain, cv threshold_cv; audio out + cv gr), JSON round-trip,
    unknown-param rejection, signal-kind walls.
  - Gain law: the static gain computer matches the soft-knee formula
    analytically (below / knee / above / boundaries / hard knee), and a
    steady sine lands on the analytic gain after settling.
  - Time constants: attack and release each reach 1 - 1/e of a step in
    the gain-reduction envelope in their nominal time.
  - Neutral: ratio=1 & gain=0 & mix=1 is a bit-exact passthrough (gr=0).
  - Sidechain: an external key gains-controls `in` (kick ducks pad); an
    unpatched sidechain normals to `in`.
  - gr: the CV out mirrors the applied gain (applied_gain = gr + 1) and
    stays in [-1, 0].
  - Block size: one big block == many small blocks (exact recurrence).
  - Voice: a single voice row is bit-identical to mono; voices compress
    independently.
  - Integration: osc -> compressor -> speaker renders finite audio; make-up
    gain lifts the output.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.compressor import Compressor

SR = 44100
F = 512


def _backend(block=F):
    b = NumpyBackend(sample_rate=SR, block_size=block)
    return b


def _run(b, comp, patch, src_id, x, sc_id=None, sc=None,
         tcv_id=None, tcv=None, block=F):
    """Render ``x`` (1D or (V,F)) through the compressor, block by block.

    Returns (out, gr) concatenated along the time axis.
    """
    n = (x.shape[-1] // block) * block
    outs, grs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src_id, "out"): x[..., sl].astype(np.float32)}
        if sc_id is not None:
            bufs[(sc_id, "out")] = sc[..., sl].astype(np.float32)
        if tcv_id is not None:
            bufs[(tcv_id, "out")] = tcv[..., sl].astype(np.float32)
        r = b._render_compressor(comp, block, bufs, patch)
        outs.append(r["out"])
        grs.append(r["gr"])
    return np.concatenate(outs, axis=-1), np.concatenate(grs, axis=-1)


def _sine(n, freq=1000.0, amp=1.0):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        c = Patch().add_module("compressor")
        assert isinstance(c, Compressor)
        assert c.params == {
            "threshold": -18.0,
            "ratio": 2.0,
            "attack": 10.0,
            "release": 120.0,
            "knee": 6.0,
            "gain": 0.0,
            "mix": 1.0,
            "detector": "rms",
            "threshold_cv_depth": 12.0,
        }

    def test_ports_and_signal_kinds(self):
        c = Patch().add_module("compressor")
        assert [(p.name, p.signal_kind) for p in c.input_ports] == [
            ("in", "audio"),
            ("sidechain", "audio"),
            ("threshold_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in c.output_ports] == [
            ("out", "audio"),
            ("gr", "cv"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "compressor",
            params={"threshold": -30.0, "ratio": 8.0, "detector": "peak"},
        )
        restored = Patch.from_dict(patch.to_dict())
        c = next(m for m in restored if m.TYPE == "compressor")
        assert c.params["threshold"] == -30.0
        assert c.params["ratio"] == 8.0
        assert c.params["detector"] == "peak"

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("compressor", params={"makeup": 6.0})

    def test_signal_kind_walls(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        lfo = patch.add_module("lfo")
        comp = patch.add_module("compressor")
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        # audio -> in / sidechain OK
        patch.connect(osc.id, "out", comp.id, "in")
        patch.connect(osc.id, "out", comp.id, "sidechain")
        # cv -> threshold_cv OK
        patch.connect(lfo.id, "cv", comp.id, "threshold_cv")
        # cv -> audio in rejected; audio -> cv in rejected
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", comp.id, "in")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", comp.id, "threshold_cv")
        # gr is a cv out: -> cv in OK, -> audio in rejected
        patch.connect(comp.id, "gr", vca.id, "cv")
        with pytest.raises(ValueError):
            patch.connect(comp.id, "gr", spk.id, "in")
        # out is audio: -> cv in rejected
        with pytest.raises(ValueError):
            patch.connect(comp.id, "out", vca.id, "cv")


# ----- Gain law --------------------------------------------------------------


class TestGainLaw:
    def test_reduction_db_analytic(self):
        red = NumpyBackend._compressor_reduction_db
        # T=-18, R=4 (slope .75), W=6 knee spans [-21, -15].
        lv = np.array([-30.0, -21.0, -18.0, -15.0, -6.0])
        got = red(lv, -18.0, 4.0, 6.0)
        exp = np.array([0.0, 0.0, 0.5625, 2.25, 9.0])
        np.testing.assert_allclose(got, exp, atol=1e-9)

    def test_hard_knee(self):
        red = NumpyBackend._compressor_reduction_db
        got = red(np.array([-20.0, -18.0, -10.0]), -18.0, 4.0, 0.0)
        np.testing.assert_allclose(got, [0.0, 0.0, 6.0], atol=1e-9)

    def test_ratio_one_never_reduces(self):
        red = NumpyBackend._compressor_reduction_db
        got = red(np.array([-40.0, 0.0, 12.0]), -18.0, 1.0, 6.0)
        np.testing.assert_allclose(got, 0.0, atol=1e-12)

    def test_steady_sine_matches_law(self):
        # RMS detector: a steady sine has constant mean-square, so the gain
        # settles exactly on the analytic law.
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor",
            params={"threshold": -24.0, "ratio": 4.0, "knee": 0.0,
                    "detector": "rms", "attack": 5.0, "release": 50.0},
        )
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend()
        b.compile(patch)
        amp = 0.6
        x = _sine(F * 80, amp=amp)
        out, gr = _run(b, comp, patch, osc.id, x)
        tail = slice(-8 * F, None)
        level_db = 20 * np.log10(amp / np.sqrt(2))
        red_db = (1 - 1 / 4.0) * (level_db - (-24.0))
        g_exp = 10 ** (-red_db / 20)
        # applied gain from gr, and from the audio RMS ratio
        g_from_gr = float(np.mean(gr[tail])) + 1.0
        g_from_audio = np.sqrt(np.mean(out[tail] ** 2)) / np.sqrt(
            np.mean(x[len(out) - 8 * F:len(out)] ** 2)
        )
        assert abs(g_from_gr - g_exp) < 0.01
        assert abs(g_from_audio - g_exp) < 0.02


# ----- Time constants --------------------------------------------------------


class TestTimeConstants:
    def _red_from_gr(self, gr):
        return -20.0 * np.log10(np.clip(gr + 1.0, 1e-12, None))

    def test_attack_reaches_1_minus_1_over_e(self):
        # Peak detector + a constant above-threshold key -> the reduction
        # target is constant from sample 0, so the smoother is a clean
        # one-pole rising from 0.
        attack_ms = 15.0
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor",
            params={"threshold": -30.0, "ratio": 4.0, "knee": 0.0,
                    "detector": "peak", "attack": attack_ms, "release": 300.0},
        )
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend(block=8192)
        b.compile(patch)
        key = np.full(8192, 0.5, dtype=np.float32)  # -6 dB, above threshold
        _, gr = _run(b, comp, patch, osc.id, key, block=8192)
        red = self._red_from_gr(gr)
        R = red[-1]
        idx = int(np.argmax(red >= 0.632 * R))
        expected = attack_ms * 1e-3 * SR
        assert abs(idx - expected) < 0.06 * expected

    def test_release_reaches_1_minus_1_over_e(self):
        release_ms = 200.0
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor",
            params={"threshold": -30.0, "ratio": 4.0, "knee": 0.0,
                    "detector": "peak", "attack": 5.0, "release": release_ms},
        )
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend(block=16384)
        b.compile(patch)
        # settle at full reduction, then drop the key below threshold.
        key = np.concatenate([
            np.full(4096, 0.5, dtype=np.float32),      # settle (5 ms attack)
            np.full(12288, 1e-4, dtype=np.float32),    # release from R
        ])
        _, gr = _run(b, comp, patch, osc.id, key, block=16384)
        red = self._red_from_gr(gr)
        R = red[4095]  # settled reduction at the moment of release
        rel = red[4096:]
        idx = int(np.argmax(rel <= (1 - 0.632) * R))
        expected = release_ms * 1e-3 * SR
        assert abs(idx - expected) < 0.06 * expected


# ----- Neutral ---------------------------------------------------------------


class TestNeutral:
    def test_neutral_is_bit_exact_passthrough(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor", params={"ratio": 1.0, "gain": 0.0, "mix": 1.0}
        )
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend()
        b.compile(patch)
        x = np.random.randn(F).astype(np.float32)
        r = b._render_compressor(comp, F, {(osc.id, "out"): x}, patch)
        assert np.array_equal(r["out"], x)
        assert not np.any(r["gr"])

    def test_ratio_one_with_makeup_is_not_passthrough(self):
        # ratio=1 alone still applies make-up (not the neutral short-circuit).
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor", params={"ratio": 1.0, "gain": 6.0, "mix": 1.0}
        )
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend()
        b.compile(patch)
        x = _sine(F, amp=0.2)
        r = b._render_compressor(comp, F, {(osc.id, "out"): x}, patch)
        # +6 dB make-up, no reduction (ratio 1) -> ~2x louder, gr stays 0.
        assert np.allclose(r["out"], x * 10 ** (6 / 20), atol=1e-6)
        assert np.allclose(r["gr"], 0.0, atol=1e-7)


# ----- Sidechain -------------------------------------------------------------


class TestSidechain:
    def test_external_key_ducks(self):
        # Pad sits below threshold on its own; a loud kick in the sidechain
        # ducks it. Peak detector, fast attack.
        n = F * 60
        pad = _sine(n, freq=220.0, amp=0.2)
        kick = np.full(n, 1e-4, dtype=np.float32)
        kick[F * 20:F * 30] = 0.8  # loud burst in the middle
        patch = Patch()
        padsrc = patch.add_module("oscillator")
        ksrc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor",
            params={"threshold": -12.0, "ratio": 8.0, "knee": 0.0,
                    "detector": "peak", "attack": 2.0, "release": 40.0},
        )
        patch.connect(padsrc.id, "out", comp.id, "in")
        patch.connect(ksrc.id, "out", comp.id, "sidechain")
        b = _backend()
        b.compile(patch)
        out, gr = _run(b, comp, patch, padsrc.id, pad, sc_id=ksrc.id, sc=kick)
        before = slice(F * 5, F * 15)
        during = slice(F * 22, F * 28)
        rms_before = np.sqrt(np.mean(out[before] ** 2))
        rms_during = np.sqrt(np.mean(out[during] ** 2))
        assert rms_during < 0.6 * rms_before          # ducked
        assert np.mean(gr[during]) < np.mean(gr[before]) - 0.3
        assert abs(np.mean(gr[before])) < 1e-3         # pad alone: untouched

    def test_sidechain_normals_to_input(self):
        # No sidechain patched -> the detector keys off `in`, so a hot input
        # compresses itself.
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor",
            params={"threshold": -24.0, "ratio": 6.0, "knee": 0.0},
        )
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend()
        b.compile(patch)
        x = _sine(F * 40, amp=0.7)
        _, gr = _run(b, comp, patch, osc.id, x)
        assert np.mean(gr[-8 * F:]) < -0.1              # self-compressed

    def test_silent_sidechain_disables_compression(self):
        # A hot `in` but a silent external key -> no reduction (proves the
        # detector really switched from `in` to the sidechain).
        patch = Patch()
        osc = patch.add_module("oscillator")
        ksrc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor",
            params={"threshold": -24.0, "ratio": 6.0, "knee": 0.0},
        )
        patch.connect(osc.id, "out", comp.id, "in")
        patch.connect(ksrc.id, "out", comp.id, "sidechain")
        b = _backend()
        b.compile(patch)
        x = _sine(F * 40, amp=0.7)
        silent = np.zeros(F * 40, dtype=np.float32)
        out, gr = _run(b, comp, patch, osc.id, x, sc_id=ksrc.id, sc=silent)
        assert np.allclose(gr, 0.0, atol=1e-6)
        assert np.allclose(out, x[:len(out)], atol=1e-6)


# ----- gr output -------------------------------------------------------------


class TestGainReductionOut:
    def test_gr_mirrors_applied_gain(self):
        # mix=1, gain=0 -> out == in * (gr + 1) exactly (the applied gain).
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module(
            "compressor",
            params={"threshold": -30.0, "ratio": 6.0, "knee": 4.0,
                    "gain": 0.0, "mix": 1.0},
        )
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend()
        b.compile(patch)
        x = _sine(F * 20, amp=0.6)
        out, gr = _run(b, comp, patch, osc.id, x)
        assert np.allclose(out, x[:len(out)] * (gr + 1.0), atol=1e-6)
        assert np.all(gr <= 1e-7) and np.all(gr >= -1.0)


# ----- Block-size independence ----------------------------------------------


class TestBlockSize:
    def test_big_block_equals_small_blocks(self):
        # A varying-amplitude signal so attack and release both engage.
        n = 4096
        env = np.concatenate([
            np.linspace(0.05, 0.9, n // 2),
            np.linspace(0.9, 0.05, n // 2),
        ]).astype(np.float32)
        x = (_sine(n, amp=1.0) * env).astype(np.float32)
        params = {"threshold": -28.0, "ratio": 5.0, "knee": 6.0,
                  "detector": "rms", "attack": 8.0, "release": 90.0}

        pa = Patch(); oa = pa.add_module("oscillator")
        ca = pa.add_module("compressor", params=params)
        pa.connect(oa.id, "out", ca.id, "in")
        ba = _backend(block=n); ba.compile(pa)
        big_out, big_gr = _run(ba, ca, pa, oa.id, x, block=n)

        pb = Patch(); ob = pb.add_module("oscillator")
        cb = pb.add_module("compressor", params=params)
        pb.connect(ob.id, "out", cb.id, "in")
        bb = _backend(block=128); bb.compile(pb)
        small_out, small_gr = _run(bb, cb, pb, ob.id, x, block=128)

        np.testing.assert_allclose(big_out, small_out, atol=1e-6)
        np.testing.assert_allclose(big_gr, small_gr, atol=1e-6)


# ----- Voice -----------------------------------------------------------------


class TestVoice:
    def test_single_voice_row_matches_mono(self):
        params = {"threshold": -24.0, "ratio": 5.0, "knee": 3.0}
        x = (np.random.randn(F) * 0.5).astype(np.float32)

        pm = Patch(); om = pm.add_module("oscillator")
        cm = pm.add_module("compressor", params=params)
        pm.connect(om.id, "out", cm.id, "in")
        bm = _backend(); bm.compile(pm)
        rm = bm._render_compressor(cm, F, {(om.id, "out"): x}, pm)

        pv = Patch(); ov = pv.add_module("oscillator")
        cv = pv.add_module("compressor", params=params)
        pv.connect(ov.id, "out", cv.id, "in")
        bv = _backend(); bv.compile(pv)
        stereo = np.stack([x, x]).astype(np.float32)
        rv = bv._render_compressor(cv, F, {(ov.id, "out"): stereo}, pv)

        assert rv["out"].shape == (2, F)
        assert np.array_equal(rv["out"][0], rm["out"])
        assert np.array_equal(rv["gr"][0], rm["gr"])
        assert np.array_equal(rv["out"][0], rv["out"][1])

    def test_voices_compress_independently(self):
        # Quiet voice untouched, loud voice compressed (sidechain normalled,
        # so each voice keys off its own signal).
        params = {"threshold": -20.0, "ratio": 8.0, "knee": 0.0,
                  "detector": "rms"}
        n = F * 40
        quiet = _sine(n, amp=0.03)
        loud = _sine(n, amp=0.7)
        x = np.stack([quiet, loud]).astype(np.float32)
        patch = Patch()
        osc = patch.add_module("oscillator")
        comp = patch.add_module("compressor", params=params)
        patch.connect(osc.id, "out", comp.id, "in")
        b = _backend()
        b.compile(patch)
        _, gr = _run(b, comp, patch, osc.id, x)
        tail = slice(-8 * F, None)
        assert abs(np.mean(gr[0, tail])) < 0.01     # quiet: untouched
        assert np.mean(gr[1, tail]) < -0.1          # loud: reduced


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_compressor_speaker_makes_sound(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"amp": 0.8})
        comp = patch.add_module(
            "compressor", params={"threshold": -24.0, "ratio": 4.0}
        )
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", comp.id, "in")
        patch.connect(comp.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(20):
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0

    def test_makeup_gain_lifts_output(self):
        base = {"threshold": -30.0, "ratio": 4.0, "knee": 0.0, "mix": 1.0}
        x = _sine(F * 20, amp=0.6)

        p0 = Patch(); o0 = p0.add_module("oscillator")
        c0 = p0.add_module("compressor", params={**base, "gain": 0.0})
        p0.connect(o0.id, "out", c0.id, "in")
        b0 = _backend(); b0.compile(p0)
        out0, _ = _run(b0, c0, p0, o0.id, x)

        p1 = Patch(); o1 = p1.add_module("oscillator")
        c1 = p1.add_module("compressor", params={**base, "gain": 12.0})
        p1.connect(o1.id, "out", c1.id, "in")
        b1 = _backend(); b1.compile(p1)
        out1, _ = _run(b1, c1, p1, o1.id, x)

        ratio = np.sqrt(np.mean(out1[-4 * F:] ** 2)) / np.sqrt(
            np.mean(out0[-4 * F:] ** 2)
        )
        assert ratio == pytest.approx(10 ** (12 / 20), rel=0.02)
