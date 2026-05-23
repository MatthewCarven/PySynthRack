"""Tests for the AudioToCV envelope-follower module.

Coverage:
  - Model: registration, defaults, ports, signal kinds, JSON round-trip,
    cabling rejection (audio-only input).
  - Mono renderer: silence stays silent, loud step rises with attack
    time, decay falls with release time, gain scales output.
  - Voice-aware: (V, F) audio in produces (V, F) CV out, per-voice
    state is independent, mono fast path preserved when input is 1D.
  - Integration: oscillator -> audio_to_cv -> filter.cutoff_cv chain
    is cable-legal and renders to non-zero CV.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.audiotocv import AudioToCV


class TestAudioToCVModel:
    def test_register_and_defaults(self):
        patch = Patch()
        atc = patch.add_module("audio_to_cv")
        assert isinstance(atc, AudioToCV)
        assert atc.params == {
            "attack_ms": 5.0,
            "release_ms": 100.0,
            "gain": 1.0,
        }
        assert [p.name for p in atc.input_ports] == ["in"]
        assert atc.input_ports[0].signal_kind == "audio"
        assert [p.name for p in atc.output_ports] == ["cv"]
        assert atc.output_ports[0].signal_kind == "cv"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "audio_to_cv",
            params={"attack_ms": 2.5, "release_ms": 250.0, "gain": 1.5},
        )
        restored = Patch.from_dict(patch.to_dict())
        atc = next(m for m in restored if m.TYPE == "audio_to_cv")
        assert atc.params["attack_ms"] == 2.5
        assert atc.params["release_ms"] == 250.0
        assert atc.params["gain"] == 1.5

    def test_rejects_cv_into_audio_input(self):
        """An LFO (CV out) must not cable into AudioToCV's audio in."""
        patch = Patch()
        lfo = patch.add_module("lfo")
        atc = patch.add_module("audio_to_cv")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", atc.id, "in")

    def test_cv_output_routes_into_filter_cutoff(self):
        """End of the bridge: AudioToCV's cv out must plug into a
        cutoff_cv (cv input). Sanity-check that signal-kind plumbing
        actually permits the canonical use case."""
        patch = Patch()
        atc = patch.add_module("audio_to_cv")
        filt = patch.add_module("filter")
        # Should not raise.
        patch.connect(atc.id, "cv", filt.id, "cutoff_cv")


class TestAudioToCVMonoBehavior:
    def test_silence_stays_silent(self):
        patch = Patch()
        atc = patch.add_module("audio_to_cv")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        # Inject silence directly.
        buffers = {(atc.id, "in"): np.zeros(512, dtype=np.float32)}
        # _input_buffer looks up cables; we need a real source. Build it.
        src = patch.add_module("oscillator", params={"amp": 0.0})
        patch.connect(src.id, "out", atc.id, "in")
        backend.compile(patch)
        # Render via dispatch so state initializes correctly.
        for _ in range(5):
            out = backend.render_block(512)
        # Read AudioToCV's level from state.
        level = backend._state[atc.id]["level"]
        assert level < 1e-4

    def test_unpatched_input_is_silent(self):
        """No audio cable -> output silence, state untouched."""
        patch = Patch()
        atc = patch.add_module("audio_to_cv")
        backend = NumpyBackend(sample_rate=44100, block_size=128)
        backend.compile(patch)
        out = backend._render_audio_to_cv(atc, 128, {}, patch)
        assert out.shape == (128,)
        assert np.allclose(out, 0.0)

    def test_step_input_rises_with_attack(self):
        """A constant-1 step should rise toward 1.0 over ``attack_ms``.

        With one-pole math:
            after t seconds at attack_coef, level ~ 1 - exp(-t / tau)
        At t = attack_ms (the time constant) the level should reach
        ~63%. We verify the level after exactly one attack-time of
        samples is in the 60-66% band.
        """
        sr = 44100
        attack_ms = 5.0
        patch = Patch()
        # Drive AudioToCV with a constant DC source via an oscillator
        # at amp=1.0, waveform=sine? A DC step isn't trivially available
        # as a module, but we can call _render_audio_to_cv_mono directly
        # with a synthetic step.
        atc = patch.add_module(
            "audio_to_cv", params={"attack_ms": attack_ms, "release_ms": 1000.0}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)

        # 5 ms at 44.1 kHz = 220.5 samples; render 221.
        n = int(round(attack_ms * 1e-3 * sr))
        step = np.ones(n, dtype=np.float32)

        # Coefficients identical to the renderer's derivation.
        attack_coef = 1.0 - math.exp(-1.0 / (attack_ms * 1e-3 * sr))
        release_coef = 1.0 - math.exp(-1.0 / (1000.0 * 1e-3 * sr))
        out = backend._render_audio_to_cv_mono(
            atc, n, step, attack_coef, release_coef, 1.0
        )
        # Expect ~1 - 1/e = 0.6321
        level_at_tau = out[-1]
        assert 0.60 < level_at_tau < 0.66, f"level at tau was {level_at_tau:.4f}"

    def test_release_decays_toward_zero(self):
        """Pre-load level near 1.0 then feed silence with a 10 ms release.
        After one release time the level should fall to ~37% (1/e)."""
        sr = 44100
        release_ms = 10.0
        patch = Patch()
        atc = patch.add_module(
            "audio_to_cv", params={"attack_ms": 0.5, "release_ms": release_ms}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)

        # Force state to level=1.0.
        backend._state[atc.id] = {"level": 1.0}

        attack_coef = 1.0 - math.exp(-1.0 / (0.5 * 1e-3 * sr))
        release_coef = 1.0 - math.exp(-1.0 / (release_ms * 1e-3 * sr))

        n = int(round(release_ms * 1e-3 * sr))
        silence = np.zeros(n, dtype=np.float32)
        out = backend._render_audio_to_cv_mono(
            atc, n, silence, attack_coef, release_coef, 1.0
        )
        level_at_tau = out[-1]
        # Expect ~ 1/e = 0.3679.
        assert 0.34 < level_at_tau < 0.40, f"level after tau release was {level_at_tau:.4f}"

    def test_gain_scales_output(self):
        sr = 44100
        patch = Patch()
        atc = patch.add_module(
            "audio_to_cv",
            params={"attack_ms": 0.1, "release_ms": 1000.0, "gain": 3.0},
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        attack_coef = 1.0 - math.exp(-1.0 / (0.1 * 1e-3 * sr))
        release_coef = 1.0 - math.exp(-1.0 / (1000.0 * 1e-3 * sr))

        # Plenty of samples to saturate at 1.0 internally.
        step = np.ones(2048, dtype=np.float32)
        out = backend._render_audio_to_cv_mono(
            atc, 2048, step, attack_coef, release_coef, 3.0
        )
        # Internal level saturates near 1.0; output = level * gain ≈ 3.0.
        assert 2.9 < out[-1] < 3.05

    def test_negative_audio_rectified(self):
        """A -1.0 step should produce the same follower curve as a +1.0
        step — the follower rectifies."""
        sr = 44100
        patch = Patch()
        atc = patch.add_module(
            "audio_to_cv", params={"attack_ms": 1.0, "release_ms": 50.0}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=256)
        backend.compile(patch)
        attack_coef = 1.0 - math.exp(-1.0 / (1.0 * 1e-3 * sr))
        release_coef = 1.0 - math.exp(-1.0 / (50.0 * 1e-3 * sr))

        pos = backend._render_audio_to_cv_mono(
            atc, 200, np.ones(200, dtype=np.float32), attack_coef, release_coef, 1.0
        )
        # Reset state for the negative-step pass.
        backend._state[atc.id] = {"level": 0.0}
        neg = backend._render_audio_to_cv_mono(
            atc, 200, -np.ones(200, dtype=np.float32), attack_coef, release_coef, 1.0
        )
        np.testing.assert_allclose(pos, neg, atol=1e-6)


class TestAudioToCVVoiceAware:
    def test_voice_input_produces_voice_output(self):
        """A (V, F) audio in → (V, F) CV out, with per-voice state."""
        sr = 44100
        patch = Patch()
        atc = patch.add_module(
            "audio_to_cv", params={"attack_ms": 0.5, "release_ms": 10.0}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=256)
        backend.compile(patch)
        attack_coef = 1.0 - math.exp(-1.0 / (0.5 * 1e-3 * sr))
        release_coef = 1.0 - math.exp(-1.0 / (10.0 * 1e-3 * sr))

        V, F = 16, 256
        audio = np.zeros((V, F), dtype=np.float32)
        # Voice 0: steady 1.0. Voice 1: steady 0.5. Voice 2-15: silent.
        audio[0, :] = 1.0
        audio[1, :] = 0.5
        out = backend._render_audio_to_cv_voice(
            atc, F, audio, attack_coef, release_coef, 1.0
        )
        assert out.shape == (V, F)
        # Voice 0 saturates above voice 1; both above the silent voices.
        assert out[0, -1] > out[1, -1] > 0.4
        assert np.all(np.abs(out[2:, :]) < 1e-6)

    def test_voice_state_persists_across_blocks(self):
        """Per-voice level should accumulate across successive blocks."""
        sr = 44100
        patch = Patch()
        atc = patch.add_module(
            "audio_to_cv", params={"attack_ms": 2.0, "release_ms": 200.0}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=128)
        backend.compile(patch)
        attack_coef = 1.0 - math.exp(-1.0 / (2.0 * 1e-3 * sr))
        release_coef = 1.0 - math.exp(-1.0 / (200.0 * 1e-3 * sr))

        V, F = 16, 128
        audio = np.ones((V, F), dtype=np.float32)
        block1 = backend._render_audio_to_cv_voice(
            atc, F, audio, attack_coef, release_coef, 1.0
        )
        block2 = backend._render_audio_to_cv_voice(
            atc, F, audio, attack_coef, release_coef, 1.0
        )
        # End-of-block2 level should be strictly higher than end-of-block1
        # — the smoother carried state forward.
        assert np.all(block2[:, -1] > block1[:, -1])

    def test_mono_to_voice_state_reinit(self):
        """Switching shape between blocks should reinit state cleanly
        rather than indexing into a wrong-shape state array."""
        sr = 44100
        patch = Patch()
        atc = patch.add_module(
            "audio_to_cv", params={"attack_ms": 1.0, "release_ms": 10.0}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=64)
        backend.compile(patch)
        attack_coef = 1.0 - math.exp(-1.0 / (1.0 * 1e-3 * sr))
        release_coef = 1.0 - math.exp(-1.0 / (10.0 * 1e-3 * sr))

        # Mono first.
        mono = backend._render_audio_to_cv_mono(
            atc, 64, np.ones(64, dtype=np.float32), attack_coef, release_coef, 1.0
        )
        assert mono.ndim == 1
        # Then voice — should not crash, and should return (V, F).
        voice = backend._render_audio_to_cv_voice(
            atc, 64, np.ones((16, 64), dtype=np.float32), attack_coef, release_coef, 1.0
        )
        assert voice.shape == (16, 64)
        # And back to mono — should reinit again.
        mono2 = backend._render_audio_to_cv_mono(
            atc, 64, np.ones(64, dtype=np.float32), attack_coef, release_coef, 1.0
        )
        assert mono2.ndim == 1


class TestAudioToCVIntegration:
    def test_self_modulating_filter_chain(self):
        """Oscillator -> Filter -> AudioToCV -> Filter.cutoff_cv.

        This is the canonical "self-ducking" patch: when the filter's
        output gets loud, the AudioToCV pulls cutoff_cv up (or down,
        depending on patch polarity). All we test here is that the
        chain compiles, renders, and produces audible output —
        signal-kind plumbing through the chain is what matters.
        """
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "saw", "freq": 110.0, "amp": 0.8}
        )
        filt = patch.add_module(
            "filter", params={"mode": "lowpass", "cutoff": 800.0, "resonance": 0.707}
        )
        atc = patch.add_module(
            "audio_to_cv",
            params={"attack_ms": 5.0, "release_ms": 80.0, "gain": -0.5},
        )
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", filt.id, "in")
        patch.connect(filt.id, "out", atc.id, "in")
        patch.connect(atc.id, "cv", filt.id, "cutoff_cv")
        patch.connect(filt.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        # Run a few blocks; check the output has signal and didn't NaN.
        for _ in range(8):
            out = backend.render_block(512)
        assert out is not None
        assert np.isfinite(out).all()
        peak = float(np.max(np.abs(out)))
        # Should produce audible output (oscillator at amp 0.8 through
        # a lowpass at 800 Hz still passes plenty of fundamental).
        assert peak > 0.05, f"chain produced near-silence: peak={peak:.4f}"
