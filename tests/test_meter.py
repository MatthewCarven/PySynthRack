"""Tests for the Meter module (audio level indicator, pass-through).

Coverage:
  - Model: registration, empty params, ports/signal kinds (audio in →
    audio out), JSON round-trip, type walls (audio→audio legal, cv→in
    illegal, out→cv illegal).
  - Pass-through: out is the input untouched (mono + voice), shape
    preserved; disconnected input → silence.
  - Envelope: peak read after a block (instant attack), slow decay on
    silence (falls by ~_METER_DECAY per block, stays > 0), attack jumps
    instantly to a louder block, silence from start → 0; compile
    pre-creates the snapshot key at 0.0.
  - Integration: oscillator → meter → speaker renders audible audio
    (the meter is transparent) and the level snapshot reads nonzero.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.meter import Meter

SR, F = 44100, 512


def _backend():
    return NumpyBackend(sample_rate=SR, block_size=F)


def _meter_rig():
    """oscillator → meter, compiled. Returns (patch, src, meter, backend)."""
    patch = Patch()
    src = patch.add_module("oscillator")
    m = patch.add_module("meter")
    patch.connect(src.id, "out", m.id, "in")
    b = _backend()
    b.compile(patch)
    return patch, src, m, b


def _drive(b, patch, src, m, block):
    """Render the meter with ``block`` as its input buffer; return out."""
    return b._render_meter(m, block.shape[-1], {(src.id, "out"): block}, patch)["out"]


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        m = patch.add_module("meter")
        assert isinstance(m, Meter)
        assert m.params == {}

    def test_ports_and_signal_kinds(self):
        m = Patch().add_module("meter")
        assert [(p.name, p.signal_kind) for p in m.input_ports] == [("in", "audio")]
        assert [(p.name, p.signal_kind) for p in m.output_ports] == [("out", "audio")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("meter")
        restored = Patch.from_dict(patch.to_dict())
        assert any(mod.TYPE == "meter" for mod in restored)

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("meter", params={"decay": 0.9})

    def test_audio_into_meter_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        m = patch.add_module("meter")
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", m.id, "in")   # audio → audio
        patch.connect(m.id, "out", spk.id, "in")   # audio → audio sink

    def test_cv_into_meter_rejected(self):
        patch = Patch()
        nz = patch.add_module("noise")
        m = patch.add_module("meter")
        with pytest.raises(ValueError):
            patch.connect(nz.id, "cv", m.id, "in")  # cv → audio

    def test_meter_out_into_cv_rejected(self):
        patch = Patch()
        m = patch.add_module("meter")
        sh = patch.add_module("sample_hold")
        with pytest.raises(ValueError):
            patch.connect(m.id, "out", sh.id, "in")  # audio → cv


# ----- Pass-through ----------------------------------------------------------


class TestPassThrough:
    def test_out_equals_in_mono(self):
        patch, src, m, b = _meter_rig()
        x = (np.random.RandomState(1).rand(F).astype(np.float32) * 2 - 1)
        out = _drive(b, patch, src, m, x)
        assert out.shape == (F,)
        assert np.array_equal(out, x)

    def test_out_equals_in_voice(self):
        patch, src, m, b = _meter_rig()
        x = (np.random.RandomState(2).rand(4, F).astype(np.float32) * 2 - 1)
        out = _drive(b, patch, src, m, x)
        assert out.shape == (4, F)
        assert np.array_equal(out, x)

    def test_disconnected_is_silence(self):
        patch = Patch()
        m = patch.add_module("meter")
        b = _backend()
        b.compile(patch)
        out = b._render_meter(m, 256, {}, patch)["out"]
        assert out.shape == (256,)
        assert not out.any()


# ----- Envelope --------------------------------------------------------------


class TestEnvelope:
    def test_key_precreated_at_zero(self):
        _, _, m, b = _meter_rig()
        assert b.snapshot_audio_levels().get(m.id) == 0.0

    def test_instant_attack_reads_peak(self):
        patch, src, m, b = _meter_rig()
        block = np.full(F, 0.8, dtype=np.float32)
        _drive(b, patch, src, m, block)
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.8, abs=1e-6)

    def test_peak_uses_max_abs(self):
        patch, src, m, b = _meter_rig()
        block = np.zeros(F, dtype=np.float32)
        block[100] = -0.6  # a single negative spike sets the peak
        _drive(b, patch, src, m, block)
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.6, abs=1e-6)

    def test_slow_decay_on_silence(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, np.full(F, 0.8, dtype=np.float32))
        before = b.snapshot_audio_levels()[m.id]
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))
        after = b.snapshot_audio_levels()[m.id]
        # Falls, but not to zero — a gentle release.
        assert 0.0 < after < before
        assert after == pytest.approx(before * NumpyBackend._METER_DECAY, rel=1e-6)

    def test_attack_overrides_decayed_level(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, np.full(F, 0.3, dtype=np.float32))
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))  # decaying
        _drive(b, patch, src, m, np.full(F, 0.9, dtype=np.float32))  # loud
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.9, abs=1e-6)

    def test_silence_from_start_is_zero(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))
        assert b.snapshot_audio_levels()[m.id] == 0.0

    def test_voice_peak_is_loudest_voice(self):
        patch, src, m, b = _meter_rig()
        block = np.zeros((3, F), dtype=np.float32)
        block[1, :] = 0.5  # only the middle voice carries signal
        _drive(b, patch, src, m, block)
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.5, abs=1e-6)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_meter_speaker_renders_and_meters(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0, "amp": 0.7})
        m = patch.add_module("meter")
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", m.id, "in")
        patch.connect(m.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        block = None
        for _ in range(4):
            block = b.render_block(F)
        assert block is not None and np.all(np.isfinite(block))
        assert np.abs(block).max() > 0.0          # audio passed through the meter
        assert b.snapshot_audio_levels()[m.id] > 0.0  # and the level registered
