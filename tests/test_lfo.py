"""Tests for the LFO module."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.lfo import LFO, LFO_WAVEFORMS


class TestLFOModel:
    def test_register_and_defaults(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        assert isinstance(lfo, LFO)
        assert lfo.params == {
            "waveform": "sine",
            "rate": 4.0,
            "depth": 1.0,
            "bipolar": False,
        }
        assert lfo.input_ports == []
        assert [p.name for p in lfo.output_ports] == ["cv"]
        assert lfo.output_ports[0].signal_kind == "cv"

    def test_waveforms_includes_random(self):
        for w in ("sine", "triangle", "square", "saw", "random"):
            assert w in LFO_WAVEFORMS

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "lfo",
            params={
                "waveform": "triangle",
                "rate": 0.5,
                "depth": 0.7,
                "bipolar": True,
            },
        )
        restored = Patch.from_dict(patch.to_dict())
        lfo = next(m for m in restored if m.TYPE == "lfo")
        assert lfo.params["waveform"] == "triangle"
        assert lfo.params["rate"] == 0.5
        assert lfo.params["depth"] == 0.7
        assert lfo.params["bipolar"] is True


class TestLFOBehavior:
    def _backend(self, sr=44100, block=512):
        return NumpyBackend(sample_rate=sr, block_size=block)

    def test_unipolar_output_stays_in_zero_to_depth(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 1.0, "bipolar": False},
        )
        backend = self._backend()
        backend.compile(patch)
        chunks = [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        out = np.concatenate(chunks)
        assert float(out.min()) >= -1e-5
        assert float(out.max()) <= 1.0 + 1e-5

    def test_bipolar_output_spans_negative_to_positive(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        chunks = [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        out = np.concatenate(chunks)
        assert float(out.min()) < -0.9
        assert float(out.max()) > 0.9

    def test_depth_scales_amplitude(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 0.3, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        chunks = [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        out = np.concatenate(chunks)
        assert float(np.max(np.abs(out))) <= 0.3 + 1e-5
        assert float(np.max(np.abs(out))) > 0.25

    def test_rate_matches_number_of_cycles(self):
        """A 2 Hz LFO over one second should complete ~2 cycles."""
        sr = 44100
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 2.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend(sr=sr, block=sr)
        backend.compile(patch)
        out = backend._render_lfo(lfo, frames=sr)
        zero_crossings = int(np.sum(np.diff(np.signbit(out)).astype(int)))
        assert 3 <= zero_crossings <= 5

    def test_phase_continuous_across_blocks(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 1.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend(block=512)
        backend.compile(patch)
        block1 = backend._render_lfo(lfo, frames=512)
        block2 = backend._render_lfo(lfo, frames=512)
        jump = abs(float(block2[0]) - float(block1[-1]))
        assert jump < 0.05

    def test_square_lfo_takes_two_values(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "square", "rate": 2.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        out = np.concatenate(
            [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        )
        uniques = np.unique(np.round(out, 4))
        assert set(uniques.tolist()) == {-1.0, 1.0}

    def test_random_waveform_is_finite_and_bounded(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "random", "rate": 8.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        out = np.concatenate(
            [backend._render_lfo(lfo, frames=2048) for _ in range(4)]
        )
        assert np.all(np.isfinite(out))
        assert float(np.max(np.abs(out))) <= 1.0 + 1e-5

    def test_extreme_rate_clamps_safely(self):
        """A rate beyond Nyquist should clamp, not crash or NaN."""
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 1e9, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        out = backend._render_lfo(lfo, frames=512)
        assert np.all(np.isfinite(out))


class TestLFOIntegration:
    def test_tremolo_through_vca_modulates_amplitude(self):
        """LFO -> VCA.cv at unipolar depth=1 with a held note should produce
        an audio envelope whose RMS varies over a cycle."""
        sr = 44100
        patch = Patch()
        kb = patch.add_module(
            "keyboard", params={"waveform": "sine", "volume": 1.0}
        )
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 1.0, "bipolar": False},
        )
        vca = patch.add_module("vca", params={"gain": 1.0})
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(kb.id, "out", vca.id, "audio")
        patch.connect(lfo.id, "cv", vca.id, "cv")
        patch.connect(vca.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)

        kb.note_on(60)
        _ = backend.render_block(sr)  # warm up past attack ramp
        block = backend.render_block(sr)

        left = block[:, 0].astype(np.float64)
        n_windows = 8
        win_len = len(left) // n_windows
        rms_vals = [
            float(np.sqrt(np.mean(left[i * win_len:(i + 1) * win_len] ** 2)))
            for i in range(n_windows)
        ]
        assert (max(rms_vals) - min(rms_vals)) > 0.05
