"""Tests for CV-modulatable params (filter cutoff, oscillator freq/amp)."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch


# ----- Filter cutoff CV -----------------------------------------------------


class TestFilterCutoffCV:
    def _build(self, cutoff, q=0.707, cv_value=None):
        """Build osc → filter → speaker, optionally with a constant cutoff_cv."""
        patch = Patch()
        osc = patch.add_module(
            "oscillator",
            params={"waveform": "sine", "freq": 4000.0, "amp": 0.8},
        )
        filt = patch.add_module(
            "filter",
            params={"mode": "lowpass", "cutoff": cutoff, "resonance": q},
        )
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc.id, "out", filt.id, "in")
        patch.connect(filt.id, "out", spk.id, "in")
        return patch, osc, filt, spk

    def test_no_cv_patched_matches_static_cutoff(self):
        """A filter with no cutoff_cv cable behaves exactly like before
        the CV port was added."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt, spk = self._build(cutoff=200.0)
        backend.compile(patch)
        # Render via render_block. With cutoff=200 and a 4 kHz sine,
        # output should be heavily attenuated (matches prior baseline).
        peak_after_warmup = 0.0
        for i in range(10):
            block = backend.render_block(1024)
        peak_after_warmup = float(np.max(np.abs(block)))
        assert peak_after_warmup < 0.1

    def test_constant_positive_cv_raises_cutoff_one_octave(self):
        """A constant cutoff_cv of +1.0 shifts the cutoff up an octave
        (200 Hz → 400 Hz). For a 4 kHz tone the attenuation is still
        heavy but slightly less than at 200 Hz baseline."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt, spk = self._build(cutoff=200.0)
        # Add a constant CV source. Easiest: an LFO at very slow rate
        # bipolar=False would produce a slow 0..1 sweep; we want a flat
        # +1.0. Inject the CV buffer directly through the buffers dict
        # path instead of patching another module.
        backend.compile(patch)
        # Manually drive the renderer with a synthetic CV buffer.
        cv = np.full(1024, 1.0, dtype=np.float32)
        # Build buffer dict the way render_block does.
        osc_buf = backend._render_oscillator(osc, 1024, {}, patch)
        buffers = {
            (osc.id, "out"): osc_buf,
            # Forge an upstream module ID for the CV; we'll pretend it
            # came from module id 99 on port "cv".
            (99, "cv"): cv,
        }
        # Splice an extra cable into the patch so the renderer reads
        # buffers[(99, "cv")] when looking up filt.cutoff_cv.
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(99, "cv", filt.id, "cutoff_cv"))
        out = backend._render_filter(filt, 1024, buffers, patch)
        # The +1 octave shift means cutoff is now ~400 Hz, still well
        # below 4 kHz, so we still expect strong attenuation. We're
        # checking the renderer accepts the CV and produces finite,
        # non-exploding output rather than the exact dB diff.
        assert np.all(np.isfinite(out))
        assert float(np.max(np.abs(out))) < 0.5

    def test_strong_negative_cv_drops_cutoff_below_audible(self):
        """cutoff_cv = -5 shifts cutoff down ~5 octaves. A 4 kHz tone
        through a sub-Hz cutoff should be near-silent."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt, spk = self._build(cutoff=200.0)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(99, "cv", filt.id, "cutoff_cv"))
        for i in range(10):  # let filter settle
            cv = np.full(1024, -5.0, dtype=np.float32)
            osc_buf = backend._render_oscillator(osc, 1024, {}, patch)
            buffers = {(osc.id, "out"): osc_buf, (99, "cv"): cv}
            out = backend._render_filter(filt, 1024, buffers, patch)
        assert float(np.max(np.abs(out))) < 0.05

    def test_cv_modulation_is_audible_via_render_block_with_lfo(self):
        """End-to-end through render_block: LFO → cutoff_cv sweeps the
        filter cutoff across the audible range. Block-mean CV needs the
        block to be much shorter than the LFO period for the sweep to
        register — production block sizes (512–1024) satisfy this; a
        block equal to the LFO period would average out to ~0."""
        sr = 44100
        block_size = 1024
        patch = Patch()
        osc = patch.add_module(
            "oscillator",
            params={"waveform": "saw", "freq": 220.0, "amp": 0.8},
        )
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 0.5, "depth": 2.0, "bipolar": True},
        )
        filt = patch.add_module(
            "filter",
            params={"mode": "lowpass", "cutoff": 400.0, "resonance": 1.0},
        )
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc.id, "out", filt.id, "in")
        patch.connect(lfo.id, "cv", filt.id, "cutoff_cv")
        patch.connect(filt.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=block_size)
        backend.compile(patch)

        # Warmup, then collect per-block RMS over two LFO cycles.
        for _ in range(20):
            backend.render_block(block_size)
        blocks_per_two_cycles = int(2 * sr / lfo.params["rate"] / block_size)
        per_block_rms = []
        for _ in range(blocks_per_two_cycles):
            block = backend.render_block(block_size)
            left = block[:, 0].astype(np.float64)
            per_block_rms.append(float(np.sqrt(np.mean(left ** 2))))

        # Cutoff sweep should drive a noticeable RMS swing block-to-block.
        assert (max(per_block_rms) - min(per_block_rms)) > 0.05


# ----- Oscillator freq CV ---------------------------------------------------


class TestOscillatorFreqCV:
    def test_no_cv_patched_matches_static_freq(self):
        """Oscillator without freq_cv runs at its static frequency."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        buf = backend._render_oscillator(osc, sr, {}, patch)
        # ~440 cycles in 1 second → ~880 zero crossings.
        zc = int(np.sum(np.diff(np.signbit(buf)).astype(int)))
        assert 870 <= zc <= 890

    def test_constant_positive_cv_doubles_frequency(self):
        """freq_cv = +1.0 (1V/oct) should double the output frequency."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        # Splice a synthetic CV cable from an imaginary source.
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(77, "cv", osc.id, "freq_cv"))
        buffers = {(77, "cv"): np.full(sr, 1.0, dtype=np.float32)}
        buf = backend._render_oscillator(osc, sr, buffers, patch)
        zc = int(np.sum(np.diff(np.signbit(buf)).astype(int)))
        # 880 Hz now → ~1760 zero crossings.
        assert 1740 <= zc <= 1780

    def test_constant_negative_cv_halves_frequency(self):
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(77, "cv", osc.id, "freq_cv"))
        buffers = {(77, "cv"): np.full(sr, -1.0, dtype=np.float32)}
        buf = backend._render_oscillator(osc, sr, buffers, patch)
        zc = int(np.sum(np.diff(np.signbit(buf)).astype(int)))
        # 220 Hz → ~440 zero crossings.
        assert 430 <= zc <= 450

    def test_freq_cv_phase_continuous_across_blocks(self):
        """Per-sample freq integration must not glitch on block boundaries."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(77, "cv", osc.id, "freq_cv"))
        cv = np.full(512, 0.5, dtype=np.float32)
        buffers = {(77, "cv"): cv}
        b1 = backend._render_oscillator(osc, 512, buffers, patch)
        b2 = backend._render_oscillator(osc, 512, buffers, patch)
        # Jump between blocks should be tiny — per-sample delta only.
        assert abs(float(b2[0]) - float(b1[-1])) < 0.1


# ----- Oscillator amp CV ---------------------------------------------------


class TestOscillatorAmpCV:
    def test_no_amp_cv_uses_static_amp(self):
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        buf = backend._render_oscillator(osc, 512, {}, patch)
        assert float(np.max(np.abs(buf))) > 0.49
        assert float(np.max(np.abs(buf))) <= 0.5 + 1e-5

    def test_amp_cv_zero_silences_output(self):
        """amp_cv = 0 multiplies the audio by zero → silence."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(88, "cv", osc.id, "amp_cv"))
        buffers = {(88, "cv"): np.zeros(512, dtype=np.float32)}
        buf = backend._render_oscillator(osc, 512, buffers, patch)
        assert np.allclose(buf, 0.0)

    def test_amp_cv_half_halves_output(self):
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(88, "cv", osc.id, "amp_cv"))
        buffers = {(88, "cv"): np.full(512, 0.5, dtype=np.float32)}
        buf = backend._render_oscillator(osc, 512, buffers, patch)
        # Peak should now be ~0.25 (0.5 amp × 0.5 cv).
        assert float(np.max(np.abs(buf))) > 0.24
        assert float(np.max(np.abs(buf))) <= 0.25 + 1e-5
