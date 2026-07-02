"""Tests for the TiltEQ (CV-controlled spectral tilt / bass<->treble seesaw).

Coverage:
  - Model: registration, defaults, ports/kinds (audio in, tilt_cv in,
    audio out), JSON round-trip, unknown-param rejection, type walls.
  - DSP: flat (bit-exact) passthrough at tilt 0; positive tilt boosts
    lows and cuts highs by the knob amount (mirrored); negative tilt is
    the exact mirror; the pivot stays ~0 dB and moves with ``pivot``;
    tilt_cv adds in dB space (a +1 CV at depth 6 renders bit-identically
    to a static +6 dB tilt); cv_depth 0 disables the CV; effective tilt
    clamps at +/-18 dB; mono == voice bit-identical; block-size
    independent; disconnected -> silence.
  - Integration: osc -> tilt_eq -> speaker renders finite audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.tilt_eq import TiltEQ

SR = 44100


def _gain_db(freq, cv=None, **params):
    """Steady-state gain (dB) of a tilt_eq module at `freq`."""
    p = Patch()
    osc = p.add_module("oscillator")
    te = p.add_module("tilt_eq")
    for k, v in params.items():
        te.set_param(k, v)
    p.connect(osc.id, "out", te.id, "in")
    cvsrc = None
    if cv is not None:
        cvsrc = p.add_module("lfo")
        p.connect(cvsrc.id, "cv", te.id, "tilt_cv")
    b = NumpyBackend(sample_rate=SR, block_size=1024)
    b.compile(p)
    t = np.arange(SR) / SR
    x = np.sin(2 * np.pi * freq * t).astype(np.float32)
    out = []
    for i in range(0, len(x) // 1024 * 1024, 1024):
        bufs = {(osc.id, "out"): x[i:i + 1024]}
        if cvsrc is not None:
            bufs[(cvsrc.id, "cv")] = np.full(1024, cv, np.float32)
        out.append(b._render_tilt_eq(te, 1024, bufs, p))
    y = np.concatenate(out)
    a = y[SR // 2:]
    ref = x[SR // 2:SR // 2 + len(a)]
    return 20 * np.log10(
        (np.sqrt(np.mean(a ** 2)) + 1e-20) / (np.sqrt(np.mean(ref ** 2)) + 1e-20)
    )


def _render(x, cv=None, block=1024, **params):
    """Render `x` through a fresh tilt_eq, optional constant CV, given block."""
    p = Patch()
    osc = p.add_module("oscillator")
    te = p.add_module("tilt_eq")
    for k, v in params.items():
        te.set_param(k, v)
    p.connect(osc.id, "out", te.id, "in")
    cvsrc = None
    if cv is not None:
        cvsrc = p.add_module("lfo")
        p.connect(cvsrc.id, "cv", te.id, "tilt_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    out = []
    for i in range(0, len(x) // block * block, block):
        bufs = {(osc.id, "out"): x[i:i + block]}
        if cvsrc is not None:
            bufs[(cvsrc.id, "cv")] = np.full(block, cv, np.float32)
        out.append(b._render_tilt_eq(te, block, bufs, p))
    return np.concatenate(out)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        te = Patch().add_module("tilt_eq")
        assert isinstance(te, TiltEQ)
        assert te.params == {"pivot": 1000.0, "tilt": 0.0, "cv_depth": 6.0}

    def test_ports_and_kinds(self):
        te = Patch().add_module("tilt_eq")
        assert [(p.name, p.signal_kind) for p in te.input_ports] == [
            ("in", "audio"),
            ("tilt_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in te.output_ports] == [("out", "audio")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("tilt_eq", params={"pivot": 500.0, "tilt": -4.0})
        restored = Patch.from_dict(patch.to_dict())
        te = next(m for m in restored if m.TYPE == "tilt_eq")
        assert te.params["pivot"] == 500.0
        assert te.params["tilt"] == -4.0

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("tilt_eq", params={"gain": 1.0})

    def test_cv_into_tilt_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        te = patch.add_module("tilt_eq")
        patch.connect(lfo.id, "cv", te.id, "tilt_cv")

    def test_cv_into_audio_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        te = patch.add_module("tilt_eq")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", te.id, "in")

    def test_audio_into_tilt_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        te = patch.add_module("tilt_eq")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", te.id, "tilt_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        te = patch.add_module("tilt_eq")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(te.id, "out", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        te = patch.add_module("tilt_eq")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        out = b._render_tilt_eq(te, 512, {}, patch)
        assert out.shape == (512,) and not np.any(out)

    def test_flat_passthrough_at_tilt_zero(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        te = patch.add_module("tilt_eq")  # tilt defaults to 0
        patch.connect(osc.id, "out", te.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        x = np.random.randn(512).astype(np.float32)
        y = b._render_tilt_eq(te, 512, {(osc.id, "out"): x}, patch)
        assert np.array_equal(y, x)  # 0 dB shelves -> identity

    def test_positive_tilt_boosts_lows_cuts_highs(self):
        assert _gain_db(60, tilt=6.0) == pytest.approx(6.0, abs=0.3)
        assert _gain_db(12000, tilt=6.0) == pytest.approx(-6.0, abs=0.3)

    def test_negative_tilt_is_the_mirror(self):
        assert _gain_db(60, tilt=-6.0) == pytest.approx(-6.0, abs=0.3)
        assert _gain_db(12000, tilt=-6.0) == pytest.approx(6.0, abs=0.3)

    def test_pivot_stays_flat(self):
        for tilt in (-8.0, 6.0, 12.0):
            assert _gain_db(1000, tilt=tilt) == pytest.approx(0.0, abs=0.5)

    def test_pivot_moves_the_null(self):
        # 500 Hz sits at the null when pivot=500, in the boosted lows
        # when the pivot moves up to 4 kHz.
        assert _gain_db(500, tilt=8.0, pivot=500.0) == pytest.approx(0.0, abs=0.5)
        assert _gain_db(500, tilt=8.0, pivot=4000.0) == pytest.approx(8.0, abs=0.5)

    def test_cv_adds_in_db_space(self):
        # A +1 CV at depth 6 must render bit-identically to a static
        # +6 dB tilt: same effective tilt, same coefficients.
        x = (np.random.default_rng(7).standard_normal(4096) * 0.3).astype(np.float32)
        via_cv = _render(x, cv=1.0, cv_depth=6.0, tilt=0.0)
        static = _render(x, tilt=6.0)
        assert np.array_equal(via_cv, static)

    def test_cv_depth_zero_disables_cv(self):
        x = (np.random.default_rng(8).standard_normal(4096) * 0.3).astype(np.float32)
        with_cv = _render(x, cv=1.0, cv_depth=0.0, tilt=3.0)
        without = _render(x, tilt=3.0)
        assert np.array_equal(with_cv, without)

    def test_effective_tilt_clamps_at_18db(self):
        x = (np.random.default_rng(9).standard_normal(4096) * 0.3).astype(np.float32)
        wild = _render(x, tilt=12.0, cv=2.0, cv_depth=6.0)   # 12 + 12 -> clamp 18
        clamped = _render(x, tilt=18.0)
        assert np.array_equal(wild, clamped)

    def test_block_size_independent(self):
        x = (np.random.default_rng(10).standard_normal(8192) * 0.3).astype(np.float32)
        a = _render(x, tilt=7.0, block=512)
        c = _render(x, tilt=7.0, block=2048)
        n = min(len(a), len(c))
        assert np.array_equal(a[:n], c[:n])

    def test_mono_matches_voice(self):
        patch = Patch()
        te = patch.add_module("tilt_eq")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        x = np.random.randn(512)
        co = b._tilt_eq_coeffs(1000.0, 5.0)
        mono = b._render_loudness_mono(te, 512, x, co)
        b2 = NumpyBackend(sample_rate=SR, block_size=512)
        b2.compile(patch)
        voice = b2._render_loudness_voice(te, 512, np.stack([x, x]), co)
        assert np.array_equal(voice[0], mono)
        assert np.array_equal(voice[0], voice[1])


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_tilt_eq_speaker(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 110.0})
        te = patch.add_module("tilt_eq", params={"tilt": 6.0})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", te.id, "in")
        patch.connect(te.id, "out", spk.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        peak = 0.0
        for _ in range(20):
            blk = b.render_block(512)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
