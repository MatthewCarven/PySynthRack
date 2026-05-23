"""Tests for the CVToAudio signal-kind-bridge module.

Coverage:
  - Model: registration, defaults, ports, signal kinds, JSON round-trip,
    cabling rules (cv -> audio is the whole point and must succeed;
    audio -> cv input must fail; the canonical LFO -> CVToAudio ->
    Speaker chain compiles).
  - Mono renderer: 1D CV in produces equal 1D audio out, gain scales,
    unpatched input is silent.
  - Voice-aware: (V, F) CV in produces (V, F) audio out, per-voice
    independence preserved.
  - Integration: LFO at audio rate -> CVToAudio -> Speaker produces
    a tone whose FFT peak lands at the LFO's rate, demonstrating the
    "LFO as oscillator" use case end-to-end.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.cvtoaudio import CVToAudio


class TestCVToAudioModel:
    def test_register_and_defaults(self):
        patch = Patch()
        cta = patch.add_module("cv_to_audio")
        assert isinstance(cta, CVToAudio)
        assert cta.params == {"gain": 1.0}
        assert [p.name for p in cta.input_ports] == ["cv"]
        assert cta.input_ports[0].signal_kind == "cv"
        assert [p.name for p in cta.output_ports] == ["out"]
        assert cta.output_ports[0].signal_kind == "audio"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("cv_to_audio", params={"gain": 2.5})
        restored = Patch.from_dict(patch.to_dict())
        cta = next(m for m in restored if m.TYPE == "cv_to_audio")
        assert cta.params["gain"] == 2.5

    def test_lfo_cv_into_cv_input_accepted(self):
        """The canonical case: LFO.cv (cv) -> CVToAudio.cv (cv)."""
        patch = Patch()
        lfo = patch.add_module("lfo")
        cta = patch.add_module("cv_to_audio")
        # Should not raise.
        patch.connect(lfo.id, "cv", cta.id, "cv")

    def test_audio_into_cv_input_rejected(self):
        """The thing the bridge exists to navigate: an Oscillator (audio)
        can't plug into CVToAudio's cv input — that would defeat the
        type-system purpose. Must still be enforced at the model layer."""
        patch = Patch()
        osc = patch.add_module("oscillator")
        cta = patch.add_module("cv_to_audio")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", cta.id, "cv")

    def test_audio_out_feeds_speaker(self):
        """CVToAudio.out (audio) -> SpeakerOutput.in (audio) must compile."""
        patch = Patch()
        cta = patch.add_module("cv_to_audio")
        spk = patch.add_module("speaker_output")
        patch.connect(cta.id, "out", spk.id, "in")


class TestCVToAudioMonoBehavior:
    def test_unpatched_input_is_silent(self):
        patch = Patch()
        cta = patch.add_module("cv_to_audio")
        backend = NumpyBackend(sample_rate=44100, block_size=128)
        backend.compile(patch)
        out = backend._render_cv_to_audio(cta, 128, {}, patch)
        assert out.shape == (128,)
        assert np.allclose(out, 0.0)

    def test_passthrough_equality(self):
        """With gain=1, output should equal input sample-for-sample."""
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 50.0, "depth": 1.0, "bipolar": True},
        )
        cta = patch.add_module("cv_to_audio", params={"gain": 1.0})
        patch.connect(lfo.id, "cv", cta.id, "cv")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        # Render the LFO, then run the CVToAudio renderer with its buffer.
        cv = backend._render_lfo(lfo, 512, buffers={}, patch=patch)
        buffers = {(lfo.id, "cv"): cv}
        out = backend._render_cv_to_audio(cta, 512, buffers, patch)
        np.testing.assert_allclose(out, cv, atol=1e-6)

    def test_gain_scales_output(self):
        """gain=2.0 doubles the amplitude."""
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 50.0, "depth": 1.0, "bipolar": True},
        )
        cta = patch.add_module("cv_to_audio", params={"gain": 2.0})
        patch.connect(lfo.id, "cv", cta.id, "cv")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        cv = backend._render_lfo(lfo, 512, buffers={}, patch=patch)
        buffers = {(lfo.id, "cv"): cv}
        out = backend._render_cv_to_audio(cta, 512, buffers, patch)
        np.testing.assert_allclose(out, cv * 2.0, atol=1e-6)

    def test_negative_gain_inverts(self):
        """gain=-1.0 inverts polarity — same shape, flipped sign."""
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "saw", "rate": 50.0, "depth": 1.0, "bipolar": True},
        )
        cta = patch.add_module("cv_to_audio", params={"gain": -1.0})
        patch.connect(lfo.id, "cv", cta.id, "cv")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        cv = backend._render_lfo(lfo, 512, buffers={}, patch=patch)
        buffers = {(lfo.id, "cv"): cv}
        out = backend._render_cv_to_audio(cta, 512, buffers, patch)
        np.testing.assert_allclose(out, -cv, atol=1e-6)


class TestCVToAudioVoiceAware:
    def test_voice_shape_preserved(self):
        """A (V, F) CV in produces a (V, F) audio out with each row
        equal to (input row) * gain."""
        patch = Patch()
        cta = patch.add_module("cv_to_audio", params={"gain": 0.5})
        backend = NumpyBackend(sample_rate=44100, block_size=128)
        backend.compile(patch)

        # Build a synthetic (V, F) CV: voice 0 ramp up, voice 1 ramp down,
        # voice 2-15 silent. Wire it in via a fake source module's buffer.
        V, F = 16, 128
        cv = np.zeros((V, F), dtype=np.float32)
        cv[0] = np.linspace(-1.0, 1.0, F, dtype=np.float32)
        cv[1] = np.linspace(1.0, -1.0, F, dtype=np.float32)

        # Use a real LFO as the source so a cable exists.
        lfo = patch.add_module("lfo")
        patch.connect(lfo.id, "cv", cta.id, "cv")
        # _input_buffer reads from buffers dict keyed by (src_id, src_port).
        buffers = {(lfo.id, "cv"): cv}
        out = backend._render_cv_to_audio(cta, F, buffers, patch)
        assert out.shape == (V, F)
        np.testing.assert_allclose(out[0], cv[0] * 0.5, atol=1e-6)
        np.testing.assert_allclose(out[1], cv[1] * 0.5, atol=1e-6)
        # Silent voices stay silent.
        assert np.all(np.abs(out[2:, :]) < 1e-6)

    def test_mono_input_stays_mono(self):
        """1D CV in produces 1D audio out — the mono fast path is
        preserved when no voice-aware source is feeding."""
        patch = Patch()
        lfo = patch.add_module("lfo", params={"rate": 5.0})
        cta = patch.add_module("cv_to_audio")
        patch.connect(lfo.id, "cv", cta.id, "cv")
        backend = NumpyBackend(sample_rate=44100, block_size=256)
        backend.compile(patch)
        cv = backend._render_lfo(lfo, 256, buffers={}, patch=patch)
        assert cv.ndim == 1  # sanity: LFO mono path
        buffers = {(lfo.id, "cv"): cv}
        out = backend._render_cv_to_audio(cta, 256, buffers, patch)
        assert out.ndim == 1
        assert out.shape == (256,)


class TestCVToAudioIntegration:
    def test_audio_rate_lfo_becomes_oscillator(self):
        """LFO at 220 Hz -> CVToAudio -> Speaker. Output's FFT should
        peak near 220 Hz, proving the bridge actually carries the LFO's
        oscillation into the audio domain at the right rate.

        This is the canonical 'LFO as oscillator' use case end-to-end.
        """
        sr = 44100
        block = 4096
        target_freq = 220.0

        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={
                "waveform": "sine",
                "rate": target_freq,
                "depth": 1.0,
                "bipolar": True,
            },
        )
        cta = patch.add_module("cv_to_audio")
        spk = patch.add_module("speaker_output", params={"gain": 0.5})
        patch.connect(lfo.id, "cv", cta.id, "cv")
        patch.connect(cta.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=block)
        backend.compile(patch)
        out = backend.render_block(block)
        # Channel 0 (stereo speaker mixes mono -> both channels).
        mono = out[:, 0]
        spec = np.abs(np.fft.rfft(mono))
        freqs = np.fft.rfftfreq(block, 1.0 / sr)
        peak_idx = int(np.argmax(spec))
        peak_freq = float(freqs[peak_idx])
        # FFT bin resolution at block=4096 is sr/block ≈ 10.77 Hz.
        # Peak must land within one bin of the target.
        bin_hz = sr / block
        assert abs(peak_freq - target_freq) <= bin_hz, (
            f"expected peak near {target_freq} Hz, got {peak_freq:.1f} Hz "
            f"(bin width {bin_hz:.2f} Hz)"
        )

    def test_fm_via_lfo_into_rate_cv(self):
        """Two LFOs: a slow modulator (5 Hz) drives a fast carrier's
        rate_cv at 220 Hz. Through CVToAudio -> Speaker we should still
        see significant energy near 220 Hz, plus *sidebands* around it
        from the FM. We check the carrier peak is present rather than
        proving the sideband structure exactly — that would tie the
        test to specific LFO 1V/oct math more tightly than is useful.
        """
        sr = 44100
        block = 8192
        carrier_hz = 220.0

        patch = Patch()
        modulator = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 5.0, "depth": 0.3, "bipolar": True},
        )
        carrier = patch.add_module(
            "lfo",
            params={
                "waveform": "sine",
                "rate": carrier_hz,
                "depth": 1.0,
                "bipolar": True,
            },
        )
        cta = patch.add_module("cv_to_audio")
        spk = patch.add_module("speaker_output", params={"gain": 0.5})
        patch.connect(modulator.id, "cv", carrier.id, "rate_cv")
        patch.connect(carrier.id, "cv", cta.id, "cv")
        patch.connect(cta.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=block)
        backend.compile(patch)
        out = backend.render_block(block)
        mono = out[:, 0]
        spec = np.abs(np.fft.rfft(mono))
        freqs = np.fft.rfftfreq(block, 1.0 / sr)
        # Energy in a +/- 20 Hz window around the carrier should dominate
        # the spectrum (the FM smears the peak slightly, hence the window).
        carrier_band = (freqs >= carrier_hz - 20) & (freqs <= carrier_hz + 20)
        assert carrier_band.any()
        band_energy = float(np.sum(spec[carrier_band]))
        total_energy = float(np.sum(spec))
        # The carrier band should hold >= 30% of total spectral energy
        # (the rest leaks into sidebands and DC artefacts of windowing).
        ratio = band_energy / max(total_energy, 1e-9)
        assert ratio > 0.30, (
            f"carrier band held only {ratio:.2%} of energy "
            f"(expected > 30%)"
        )
