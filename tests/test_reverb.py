"""Tests for the Reverb (stereo Feedback Delay Network).

Coverage:
  - Model: registration, defaults, ports/kinds (audio in -> out_l/out_r
    audio), JSON round-trip, unknown-param rejection, type walls.
  - DSP: disconnected -> silence; mix=0 is a bit-exact dry passthrough on
    both channels; an impulse produces a decaying tail; more decay = a
    longer tail; damping rolls the tail's highs off; the tail is dense
    (diffusion) not gappy; output stays finite/bounded at max decay; a
    voice (2D) input is summed to mono.
  - Block independence: the chunked FDN gives identical output at any
    block size (512 vs 4096 vs an odd size) -- the key correctness
    property.
  - Stereo: out_l and out_r are decorrelated (that's the width).
  - Integration: osc -> reverb -> L/R speakers renders audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.reverb import Reverb

SR = 44100
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    rv = patch.add_module("reverb", params=params or {})
    patch.connect(src.id, "out", rv.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, rv, b


def _run(b, patch, src, rv, signal, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        o = b._render_reverb(rv, block, bufs, patch)
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
        rv = Patch().add_module("reverb")
        assert isinstance(rv, Reverb)
        assert rv.params == {
            "size": 0.5,
            "decay": 0.5,
            "damping": 0.5,
            "mix": 0.3,
            "cv_depth": 1.0,
        }

    def test_ports_and_kinds(self):
        rv = Patch().add_module("reverb")
        assert [(p.name, p.signal_kind) for p in rv.input_ports] == [
            ("in", "audio"),
            ("decay_cv", "cv"),
            ("mix_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in rv.output_ports] == [
            ("out_l", "audio"),
            ("out_r", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("reverb", params={"decay": 0.85, "mix": 0.5})
        restored = Patch.from_dict(patch.to_dict())
        rv = next(m for m in restored if m.TYPE == "reverb")
        assert rv.params["decay"] == 0.85
        assert rv.params["mix"] == 0.5

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("reverb", params={"room": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        rv = patch.add_module("reverb")
        patch.connect(osc.id, "out", rv.id, "in")

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        rv = patch.add_module("reverb")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", rv.id, "in")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        rv = patch.add_module("reverb")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(rv.id, "out_l", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        rv = patch.add_module("reverb")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_reverb(rv, F, {}, patch)
        assert not np.any(o["out_l"]) and not np.any(o["out_r"])
        assert o["out_l"].shape == (F,)

    def test_frames_zero_empty(self):
        patch, src, rv, b = _rig()
        o = b._render_reverb(rv, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch)
        assert o["out_l"].shape == (0,) and o["out_r"].shape == (0,)

    def test_mix_zero_exact_dry_passthrough(self):
        patch, src, rv, b = _rig({"mix": 0.0, "decay": 0.7})
        x = np.random.randn(F * 3).astype(np.float32)
        lo, r = _run(b, patch, src, rv, x)
        assert np.array_equal(lo, x[: len(lo)])
        assert np.array_equal(r, x[: len(r)])

    def test_impulse_decays(self):
        patch, src, rv, b = _rig({"decay": 0.6, "mix": 1.0, "damping": 0.4})
        lo, r = _run(b, patch, src, rv, _impulse(SR))
        early = np.sqrt(np.mean(lo[:2000] ** 2))
        late = np.sqrt(np.mean(lo[-2000:] ** 2))
        assert late < 0.2 * early  # tail has decayed
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))

    def test_more_decay_longer_tail(self):
        imp = _impulse(SR)
        p1, s1, r1, b1 = _rig({"decay": 0.3, "mix": 1.0, "damping": 0.3})
        p2, s2, r2, b2 = _rig({"decay": 0.85, "mix": 1.0, "damping": 0.3})
        l_short, _ = _run(b1, p1, s1, r1, imp)
        l_long, _ = _run(b2, p2, s2, r2, imp)
        w = slice(int(0.5 * SR), int(0.6 * SR))
        assert np.sqrt(np.mean(l_long[w] ** 2)) > 5 * np.sqrt(np.mean(l_short[w] ** 2))

    def test_tail_is_dense_not_gappy(self):
        patch, src, rv, b = _rig({"decay": 0.7, "mix": 1.0, "damping": 0.4})
        lo, _ = _run(b, patch, src, rv, _impulse(SR))
        seg = lo[int(0.1 * SR):int(0.5 * SR)]
        assert np.mean(np.abs(seg) < 1e-5) < 0.1  # diffusion fills the tail

    def test_damping_rolls_off_tail_highs(self):
        imp = _impulse(SR)
        pb, sb, rb_, bb = _rig({"decay": 0.7, "mix": 1.0, "damping": 0.05})
        pd, sd, rd_, bd = _rig({"decay": 0.7, "mix": 1.0, "damping": 0.95})
        lb, _ = _run(bb, pb, sb, rb_, imp)
        ld, _ = _run(bd, pd, sd, rd_, imp)

        def hf_energy(y):
            yt = y[int(0.2 * SR):int(0.6 * SR)]
            d = np.diff(yt)  # crude high-pass
            return float(np.sum(d ** 2))

        assert hf_energy(ld) < hf_energy(lb)

    def test_stability_at_max_decay(self):
        patch, src, rv, b = _rig({"decay": 1.0, "mix": 1.0, "damping": 0.2})
        x = np.random.randn(2 * SR).astype(np.float32)
        lo, r = _run(b, patch, src, rv, x)
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))
        assert np.max(np.abs(lo)) < 50.0 and np.max(np.abs(r)) < 50.0

    def test_voice_input_summed_to_mono(self):
        # A 2D (V, F) input is collapsed to mono by the input helper.
        patch, src, rv, b = _rig({"mix": 1.0, "decay": 0.5})
        v = np.random.randn(3, F).astype(np.float32)
        o = b._render_reverb(rv, F, {(src.id, "out"): v}, patch)
        assert o["out_l"].shape == (F,) and np.all(np.isfinite(o["out_l"]))


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def test_output_independent_of_block_size(self):
        x = (np.random.randn(8192) * 0.3).astype(np.float32)
        params = {"size": 0.6, "decay": 0.6, "damping": 0.4, "mix": 0.5}
        pa, sa, ra, ba = _rig(params, block=512)
        la, raa = _run(ba, pa, sa, ra, x, block=512)
        pb, sb, rb, bb = _rig(params, block=4096)
        lb, rbb = _run(bb, pb, sb, rb, x, block=4096)
        pc, sc, rc, bc = _rig(params, block=333)
        lc, rcc = _run(bc, pc, sc, rc, x, block=333)
        m = min(len(la), len(lb), len(lc))
        assert np.array_equal(la[:m], lb[:m])
        assert np.array_equal(la[:m], lc[:m])
        assert np.array_equal(raa[:m], rbb[:m])


# ----- Stereo ----------------------------------------------------------------


class TestStereo:
    def test_channels_are_decorrelated(self):
        patch, src, rv, b = _rig({"decay": 0.7, "mix": 1.0})
        x = (np.random.randn(SR) * 0.3).astype(np.float32)
        lo, r = _run(b, patch, src, rv, x)
        assert not np.array_equal(lo, r)
        corr = np.corrcoef(lo[3000:], r[3000:])[0, 1]
        assert abs(corr) < 0.5


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_reverb_stereo_speakers(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0})
        rv = patch.add_module("reverb", params={"decay": 0.7, "mix": 0.4})
        spk_l = patch.add_module("left_speaker_output")
        spk_r = patch.add_module("right_speaker_output")
        patch.connect(osc.id, "out", rv.id, "in")
        patch.connect(rv.id, "out_l", spk_l.id, "in")
        patch.connect(rv.id, "out_r", spk_r.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(80):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
