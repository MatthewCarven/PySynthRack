"""Tests for the AD (trigger Attack/Decay) envelope.

Coverage:
  - Model: registration, defaults, ports/signal kinds (trig gate →
    cv), JSON round-trip, unknown-param rejection, type walls (gate→trig
    legal, cv→trig illegal, cv-out→audio-sink illegal).
  - Mono DSP: no trigger → silence; a trigger reaches 1.0 then returns
    to 0.0; gate *length is ignored* (long pulse == 1-sample pulse);
    attack/decay lengths track the params; instant attack; retrigger
    deep in the decay climbs back toward 1.0 from the current level;
    output stays in [0, 1].
  - Voice DSP: a single-voice row is bit-identical to the mono path;
    voices trigger independently; mono↔voice state reinit.
  - Integration: lfo→schmitt clock → AD → vca renders audible audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.ad_envelope import ADEnvelope

SR = 44100


def _backend(block=512):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(attack=0.001, decay=0.01):
    """schmitt → ad_envelope, compiled. Returns (patch, src, ad, backend)."""
    patch = Patch()
    src = patch.add_module("schmitt")
    ad = patch.add_module("ad_envelope", params={"attack": attack, "decay": decay})
    patch.connect(src.id, "gate", ad.id, "trig")
    b = _backend()
    b.compile(patch)
    return patch, src, ad, b


def _drive(b, patch, src, ad, gate):
    return b._render_ad(ad, gate.shape[-1], {(src.id, "gate"): gate.astype(np.float32)}, patch)


def _pulse(F, at=0, width=1):
    g = np.zeros(F, dtype=np.float32)
    g[at:at + width] = 1.0
    return g


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        ad = Patch().add_module("ad_envelope")
        assert isinstance(ad, ADEnvelope)
        assert ad.params == {"attack": 0.005, "decay": 0.20}

    def test_ports_and_signal_kinds(self):
        ad = Patch().add_module("ad_envelope")
        assert [(p.name, p.signal_kind) for p in ad.input_ports] == [("trig", "gate")]
        assert [(p.name, p.signal_kind) for p in ad.output_ports] == [("cv", "cv")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("ad_envelope", params={"attack": 0.02, "decay": 0.5})
        restored = Patch.from_dict(patch.to_dict())
        ad = next(m for m in restored if m.TYPE == "ad_envelope")
        assert ad.params == {"attack": 0.02, "decay": 0.5}

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("ad_envelope", params={"sustain": 0.5})

    def test_gate_into_trig_accepted(self):
        patch = Patch()
        sch = patch.add_module("schmitt")
        ad = patch.add_module("ad_envelope")
        patch.connect(sch.id, "gate", ad.id, "trig")  # gate → gate

    def test_cv_into_trig_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ad = patch.add_module("ad_envelope")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ad.id, "trig")  # cv → gate

    def test_cv_out_into_audio_sink_rejected(self):
        patch = Patch()
        ad = patch.add_module("ad_envelope")
        spk = patch.add_module("speaker_output")
        with pytest.raises(ValueError):
            patch.connect(ad.id, "cv", spk.id, "in")  # cv → audio


# ----- Mono DSP --------------------------------------------------------------


class TestMonoDSP:
    def test_no_trigger_is_silence(self):
        patch, src, ad, b = _rig()
        out = _drive(b, patch, src, ad, np.zeros(1024, dtype=np.float32))
        assert out.shape == (1024,)
        assert not out.any()

    def test_trigger_reaches_one_then_zero(self):
        patch, src, ad, b = _rig(attack=0.001, decay=0.01)
        out = _drive(b, patch, src, ad, _pulse(3000))
        assert out.max() == pytest.approx(1.0, abs=1e-6)
        assert out[-1] == pytest.approx(0.0, abs=1e-6)
        assert out.min() >= 0.0 and out.max() <= 1.0

    def test_gate_length_ignored(self):
        # A 1-sample trigger and a 500-sample gate produce the same shape.
        p1, s1, a1, b1 = _rig()
        short = _drive(b1, p1, s1, a1, _pulse(3000, width=1))
        p2, s2, a2, b2 = _rig()
        long = _drive(b2, p2, s2, a2, _pulse(3000, width=500))
        assert np.allclose(short, long, atol=1e-6)

    def test_attack_length_tracks_param(self):
        patch, src, ad, b = _rig(attack=0.002, decay=0.05)  # 2 ms ≈ 88 samples
        out = _drive(b, patch, src, ad, _pulse(6000))
        peak_idx = int(np.argmax(out))
        assert peak_idx == pytest.approx(88, abs=2)

    def test_attack_then_decay_monotone(self):
        patch, src, ad, b = _rig(attack=0.002, decay=0.02)
        out = _drive(b, patch, src, ad, _pulse(6000))
        peak = int(np.argmax(out))
        assert np.all(np.diff(out[:peak + 1]) >= -1e-9)        # rising to peak
        assert np.all(np.diff(out[peak:peak + 400]) <= 1e-9)    # falling after

    def test_instant_attack(self):
        patch, src, ad, b = _rig(attack=0.0, decay=0.05)
        out = _drive(b, patch, src, ad, _pulse(3000))
        assert out[0] == pytest.approx(1.0, abs=1e-6)  # full level on the trigger sample

    def test_retrigger_climbs_from_current_level(self):
        # Trigger, let it decay well down, retrigger -> level jumps back up.
        patch, src, ad, b = _rig(attack=0.001, decay=0.02)  # decay ≈ 882 samples
        F = 1200
        g = np.zeros(F, dtype=np.float32)
        g[0] = 1.0
        g[600] = 1.0  # deep into the decay
        out = _drive(b, patch, src, ad, g)
        assert out[610] > out[599]  # climbing again right after the retrigger

    def test_disconnected_trig_is_silence(self):
        patch = Patch()
        ad = patch.add_module("ad_envelope")
        b = _backend()
        b.compile(patch)
        out = b._render_ad(ad, 256, {}, patch)
        assert out.shape == (256,)
        assert not out.any()


# ----- Voice DSP -------------------------------------------------------------


class TestVoiceDSP:
    def test_single_voice_matches_mono(self):
        patch, src, ad, b = _rig(attack=0.001, decay=0.02)
        g = _pulse(2000)
        mono = _drive(b, patch, src, ad, g)
        pv, sv, av, bv = _rig(attack=0.001, decay=0.02)
        voice = bv._render_ad(av, 2000, {(sv.id, "gate"): g[np.newaxis, :]}, pv)
        assert voice.shape == (1, 2000)
        assert np.array_equal(voice[0], mono)

    def test_voices_independent(self):
        patch, src, ad, b = _rig(attack=0.001, decay=0.02)
        F = 2000
        g = np.zeros((3, F), dtype=np.float32)
        g[0, 0] = 1.0      # voice 0 fires immediately
        g[1, 300] = 1.0    # voice 1 fires later
        # voice 2 never fires
        out = b._render_ad(ad, F, {(src.id, "gate"): g}, patch)
        assert out[0].max() == pytest.approx(1.0, abs=1e-6)
        assert out[1].max() == pytest.approx(1.0, abs=1e-6)
        assert not out[2].any()
        assert int(np.argmax(out[1])) > int(np.argmax(out[0]))

    def test_mono_voice_state_reinit(self):
        patch, src, ad, b = _rig()
        mono_g = _pulse(512)
        voice_g = np.tile(_pulse(512), (4, 1))
        o1 = b._render_ad(ad, 512, {(src.id, "gate"): mono_g}, patch)
        ov = b._render_ad(ad, 512, {(src.id, "gate"): voice_g}, patch)
        o2 = b._render_ad(ad, 512, {(src.id, "gate"): mono_g}, patch)
        assert o1.shape == (512,)
        assert ov.shape == (4, 512)
        assert o2.shape == (512,)

    def test_block_stitch_equivalence(self):
        # Envelope split across two blocks equals one contiguous render.
        g = _pulse(1024, at=10)
        p1, s1, a1, b1 = _rig(attack=0.002, decay=0.02)
        two = np.concatenate([
            _drive(b1, p1, s1, a1, g[:512]),
            _drive(b1, p1, s1, a1, g[512:]),
        ])
        p2, s2, a2, b2 = _rig(attack=0.002, decay=0.02)
        whole = _drive(b2, p2, s2, a2, g)
        assert np.allclose(two, whole, atol=1e-6)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_clocked_ad_drum_renders(self):
        patch = Patch()
        lfo = patch.add_module("lfo", params={"waveform": "square", "rate": 8.0})
        sch = patch.add_module("schmitt", params={"high": 0.6, "low": 0.4})
        ad = patch.add_module("ad_envelope", params={"attack": 0.001, "decay": 0.08})
        osc = patch.add_module("oscillator", params={"waveform": "sine", "freq": 60.0, "amp": 0.9})
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        patch.connect(lfo.id, "cv", sch.id, "in")
        patch.connect(sch.id, "gate", ad.id, "trig")
        patch.connect(osc.id, "out", vca.id, "audio")
        patch.connect(ad.id, "cv", vca.id, "cv")
        patch.connect(vca.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(40):  # span several clock periods (8 Hz)
            block = b.render_block(512)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0  # the clocked AD drum made sound across the run
