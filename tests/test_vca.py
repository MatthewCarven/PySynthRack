"""Tests for the VCA (voltage-controlled amplifier) module."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.vca import VCA


class TestVCAModel:
    def test_register_and_defaults(self):
        patch = Patch()
        vca = patch.add_module("vca")
        assert isinstance(vca, VCA)
        assert vca.params == {"gain": 1.0}
        in_names = [p.name for p in vca.input_ports]
        assert in_names == ["audio", "cv"]
        # Signal kinds enforce correct cabling at the model layer.
        audio_in = next(p for p in vca.input_ports if p.name == "audio")
        cv_in = next(p for p in vca.input_ports if p.name == "cv")
        assert audio_in.signal_kind == "audio"
        assert cv_in.signal_kind == "cv"
        assert [p.name for p in vca.output_ports] == ["out"]
        assert vca.output_ports[0].signal_kind == "audio"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("vca", params={"gain": 0.5})
        restored = Patch.from_dict(patch.to_dict())
        vca = next(m for m in restored if m.TYPE == "vca")
        assert vca.params["gain"] == 0.5

    def test_rejects_wrong_signal_kind(self):
        """The patch model should refuse to plug audio into the CV input."""
        patch = Patch()
        osc = patch.add_module("oscillator")
        vca = patch.add_module("vca")
        # audio out → audio in is fine.
        patch.connect(osc.id, "out", vca.id, "audio")
        # But audio out → cv in must fail.
        import pytest

        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", vca.id, "cv")


class TestVCABehavior:
    def _make_osc_vca_patch(self, gain=1.0):
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.8}
        )
        vca = patch.add_module("vca", params={"gain": gain})
        patch.connect(osc.id, "out", vca.id, "audio")
        return patch, osc, vca

    def test_passes_audio_when_cv_unpatched(self):
        """No CV plugged → unity gain (audio passes through)."""
        patch, osc, vca = self._make_osc_vca_patch(gain=1.0)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)

        audio = backend._render_oscillator(osc, 512)
        buffers = {(osc.id, "out"): audio}
        out = backend._render_vca(vca, 512, buffers, patch)
        np.testing.assert_allclose(out, audio, atol=1e-6)

    def test_gain_parameter_scales_output(self):
        """gain=0.5 with no CV should halve the audio."""
        patch, osc, vca = self._make_osc_vca_patch(gain=0.5)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        audio = backend._render_oscillator(osc, 512)
        buffers = {(osc.id, "out"): audio}
        out = backend._render_vca(vca, 512, buffers, patch)
        np.testing.assert_allclose(out, audio * 0.5, atol=1e-6)

    def test_cv_multiplies_audio(self):
        """CV at 0.3 should attenuate to ~30% of input amplitude."""
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 1.0}
        )
        adsr = patch.add_module("adsr")
        vca = patch.add_module("vca")
        patch.connect(osc.id, "out", vca.id, "audio")
        patch.connect(adsr.id, "cv", vca.id, "cv")

        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)

        audio = backend._render_oscillator(osc, 512)
        cv = np.full(512, 0.3, dtype=np.float32)
        buffers = {
            (osc.id, "out"): audio,
            (adsr.id, "cv"): cv,
        }
        out = backend._render_vca(vca, 512, buffers, patch)
        np.testing.assert_allclose(out, audio * 0.3, atol=1e-6)

    def test_silent_audio_silent_output(self):
        patch, osc, vca = self._make_osc_vca_patch(gain=1.0)
        backend = NumpyBackend(sample_rate=44100, block_size=128)
        backend.compile(patch)
        buffers = {(osc.id, "out"): np.zeros(128, dtype=np.float32)}
        out = backend._render_vca(vca, 128, buffers, patch)
        assert np.allclose(out, 0.0)

    def test_unpatched_audio_input_silent(self):
        """A VCA with NO audio cable should emit silence, not crash."""
        patch = Patch()
        vca = patch.add_module("vca")
        backend = NumpyBackend(sample_rate=44100, block_size=128)
        backend.compile(patch)
        out = backend._render_vca(vca, 128, {}, patch)
        assert np.allclose(out, 0.0)


class TestVCAIntegration:
    def test_full_chain_envelope_shapes_audio(self):
        """End-to-end: keyboard → vca ← adsr ← keyboard.gate. With note
        held long enough for sustain, output amplitude should track the
        sustain level of the envelope."""
        sr = 44100
        patch = Patch()
        kb = patch.add_module("keyboard", params={"waveform": "sine", "volume": 1.0})
        env = patch.add_module(
            "adsr",
            params={"attack": 0.005, "decay": 0.01, "sustain": 0.4, "release": 0.05},
        )
        vca = patch.add_module("vca", params={"gain": 1.0})
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(kb.id, "out", vca.id, "audio")
        patch.connect(kb.id, "gate", env.id, "gate")
        patch.connect(env.id, "cv", vca.id, "cv")
        patch.connect(vca.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=2048)
        backend.compile(patch)

        # Idle render → silence.
        idle = backend.render_block(2048)
        assert np.max(np.abs(idle)) < 1e-3

        # Hold a note. Let attack/decay settle then sample sustain region.
        kb.note_on(60)
        for _ in range(5):
            block = backend.render_block(2048)

        # At sustain=0.4 with sine amp 0.5 (keyboard wave is normalized
        # then * volume=1.0), peak should be ~0.4 (within tolerance).
        peak = float(np.max(np.abs(block)))
        assert 0.30 < peak < 0.55, f"sustain-shaped peak was {peak:.3f}"
