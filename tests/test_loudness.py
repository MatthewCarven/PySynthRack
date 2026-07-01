"""Tests for the Loudness (equal-loudness contour / loudness compensation).

Coverage:
  - Model: registration, defaults, ports/kinds (audio in, level_cv in,
    audio out), JSON round-trip, unknown-param rejection, type walls.
  - DSP: flat (bit-exact) passthrough at level=1 with no trims; bass and
    treble bloom as level drops (bass more than treble) while the mid is
    untouched; manual bass/treble trims add shelves on top; level_cv
    lowers the effective level (more boost); mono == voice bit-identical;
    disconnected -> silence.
  - Integration: osc -> loudness -> speaker renders finite audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.loudness import Loudness

SR = 44100


def _gain_db(freq, **params):
    """Steady-state gain (dB) of a loudness module at `freq`."""
    p = Patch()
    osc = p.add_module("oscillator")
    ld = p.add_module("loudness")
    cvval = params.pop("cv", None)
    for k, v in params.items():
        ld.set_param(k, v)
    p.connect(osc.id, "out", ld.id, "in")
    cvsrc = None
    if cvval is not None:
        cvsrc = p.add_module("lfo")
        p.connect(cvsrc.id, "cv", ld.id, "level_cv")
    b = NumpyBackend(sample_rate=SR, block_size=1024)
    b.compile(p)
    t = np.arange(SR) / SR
    x = np.sin(2 * np.pi * freq * t).astype(np.float32)
    out = []
    for i in range(0, len(x) // 1024 * 1024, 1024):
        bufs = {(osc.id, "out"): x[i:i + 1024]}
        if cvsrc is not None:
            bufs[(cvsrc.id, "cv")] = np.full(1024, cvval, np.float32)
        out.append(b._render_loudness(ld, 1024, bufs, p))
    y = np.concatenate(out)
    a = y[SR // 2:]
    ref = x[SR // 2:SR // 2 + len(a)]
    return 20 * np.log10(
        (np.sqrt(np.mean(a ** 2)) + 1e-20) / (np.sqrt(np.mean(ref ** 2)) + 1e-20)
    )


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        ld = Patch().add_module("loudness")
        assert isinstance(ld, Loudness)
        assert ld.params == {"level": 0.5, "bass": 0.0, "treble": 0.0, "cv_depth": 1.0}

    def test_ports_and_kinds(self):
        ld = Patch().add_module("loudness")
        assert [(p.name, p.signal_kind) for p in ld.input_ports] == [
            ("in", "audio"),
            ("level_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in ld.output_ports] == [("out", "audio")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("loudness", params={"level": 0.2, "bass": 3.0})
        restored = Patch.from_dict(patch.to_dict())
        ld = next(m for m in restored if m.TYPE == "loudness")
        assert ld.params["level"] == 0.2
        assert ld.params["bass"] == 3.0

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("loudness", params={"gain": 1.0})

    def test_cv_into_level_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ld = patch.add_module("loudness")
        patch.connect(lfo.id, "cv", ld.id, "level_cv")

    def test_cv_into_audio_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ld = patch.add_module("loudness")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ld.id, "in")

    def test_audio_into_level_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ld = patch.add_module("loudness")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", ld.id, "level_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        ld = patch.add_module("loudness")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(ld.id, "out", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        ld = patch.add_module("loudness")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        out = b._render_loudness(ld, 512, {}, patch)
        assert out.shape == (512,) and not np.any(out)

    def test_flat_passthrough_at_level_one(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ld = patch.add_module("loudness", params={"level": 1.0, "bass": 0.0, "treble": 0.0})
        patch.connect(osc.id, "out", ld.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        x = np.random.randn(512).astype(np.float32)
        y = b._render_loudness(ld, 512, {(osc.id, "out"): x}, patch)
        assert np.array_equal(y, x)  # 0 dB shelves -> identity

    def test_bass_blooms_as_level_drops(self):
        g1 = _gain_db(60, level=1.0)
        g_half = _gain_db(60, level=0.5)
        g0 = _gain_db(60, level=0.0)
        assert g1 == pytest.approx(0.0, abs=0.2)
        assert g0 > g_half > g1 + 1.0   # monotonic bass boost
        assert g0 > 8.0                 # substantial at the bottom

    def test_treble_blooms_but_less_than_bass(self):
        bass0 = _gain_db(60, level=0.0)
        treb0 = _gain_db(12000, level=0.0)
        assert treb0 > 3.0              # treble is boosted too
        assert treb0 < bass0            # ...but less than bass

    def test_mid_untouched_by_level(self):
        for lvl in (1.0, 0.5, 0.0):
            assert _gain_db(1000, level=lvl) == pytest.approx(0.0, abs=0.5)

    def test_manual_bass_trim_adds_low_shelf(self):
        assert _gain_db(60, level=1.0, bass=6.0) > 4.0
        assert _gain_db(12000, level=1.0, bass=6.0) == pytest.approx(0.0, abs=0.5)

    def test_manual_treble_trim_adds_high_shelf(self):
        assert _gain_db(12000, level=1.0, treble=6.0) > 4.0
        assert _gain_db(60, level=1.0, treble=6.0) == pytest.approx(0.0, abs=0.5)

    def test_level_cv_lowers_effective_level(self):
        flat = _gain_db(60, level=1.0, cv_depth=1.0, cv=0.0)
        boosted = _gain_db(60, level=1.0, cv_depth=1.0, cv=-1.0)
        assert flat == pytest.approx(0.0, abs=0.2)
        assert boosted > 8.0   # cv drove effective level to 0 -> full bass

    def test_mono_matches_voice(self):
        patch = Patch()
        ld = patch.add_module("loudness")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        x = np.random.randn(512)
        co = b._loudness_coeffs(0.4, 0.0, 0.0)
        mono = b._render_loudness_mono(ld, 512, x, co)
        b2 = NumpyBackend(sample_rate=SR, block_size=512)
        b2.compile(patch)
        voice = b2._render_loudness_voice(ld, 512, np.stack([x, x]), co)
        assert np.array_equal(voice[0], mono)
        assert np.array_equal(voice[0], voice[1])


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_loudness_speaker(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 110.0})
        ld = patch.add_module("loudness", params={"level": 0.3})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", ld.id, "in")
        patch.connect(ld.id, "out", spk.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(patch)
        peak = 0.0
        for _ in range(20):
            blk = b.render_block(512)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0
