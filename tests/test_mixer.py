"""Tests for the Mixer module."""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.mixer import MIXER_GAIN_NAMES, MIXER_INPUT_NAMES, Mixer


class TestMixerModel:
    def test_register_and_defaults(self):
        patch = Patch()
        mx = patch.add_module("mixer")
        assert isinstance(mx, Mixer)
        assert mx.params == {
            "gain1": 1.0,
            "gain2": 1.0,
            "gain3": 1.0,
            "gain4": 1.0,
            "master": 0.7,
        }
        in_names = [p.name for p in mx.input_ports]
        assert in_names == list(MIXER_INPUT_NAMES)
        for port in mx.input_ports:
            assert port.signal_kind == "audio"
        assert [p.name for p in mx.output_ports] == ["out"]
        assert mx.output_ports[0].signal_kind == "audio"

    def test_gain_and_input_names_align(self):
        # in1 ↔ gain1 etc. — the renderer relies on this convention.
        assert len(MIXER_INPUT_NAMES) == len(MIXER_GAIN_NAMES) == 4
        for inp, g in zip(MIXER_INPUT_NAMES, MIXER_GAIN_NAMES):
            assert inp.endswith(g[-1])

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "mixer",
            params={
                "gain1": 0.3,
                "gain2": 0.5,
                "gain3": 0.7,
                "gain4": 0.0,
                "master": 1.2,
            },
        )
        restored = Patch.from_dict(patch.to_dict())
        mx = next(m for m in restored if m.TYPE == "mixer")
        assert mx.params["gain1"] == 0.3
        assert mx.params["gain4"] == 0.0
        assert mx.params["master"] == 1.2

    def test_rejects_cv_into_audio_input(self):
        """Mixer inputs are audio; plugging a CV cable in must fail."""
        patch = Patch()
        lfo = patch.add_module("lfo")
        mx = patch.add_module("mixer")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", mx.id, "in1")

    def test_one_cable_per_input_jack(self):
        """Each mixer input accepts only one cable — to bus more sources,
        chain mixers."""
        patch = Patch()
        o1 = patch.add_module("oscillator")
        o2 = patch.add_module("oscillator")
        mx = patch.add_module("mixer")
        patch.connect(o1.id, "out", mx.id, "in1")
        with pytest.raises(ValueError):
            patch.connect(o2.id, "out", mx.id, "in1")
        # But a different jack is fine.
        patch.connect(o2.id, "out", mx.id, "in2")


class TestMixerBehavior:
    def _backend(self, sr=44100, block=512):
        return NumpyBackend(sample_rate=sr, block_size=block)

    def test_silent_when_no_inputs(self):
        patch = Patch()
        mx = patch.add_module("mixer", params={"master": 1.0})
        backend = self._backend()
        backend.compile(patch)
        out = backend._render_mixer(mx, 512, {}, patch)
        assert np.allclose(out, 0.0)

    def test_sums_two_inputs_with_unity_gain(self):
        """A=0.3 const + B=0.4 const, all gains=1, master=1 → 0.7 out."""
        patch = Patch()
        o1 = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 1.0, "amp": 0.3}
        )
        o2 = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 1.0, "amp": 0.4}
        )
        mx = patch.add_module(
            "mixer",
            params={"gain1": 1.0, "gain2": 1.0, "gain3": 1.0, "gain4": 1.0, "master": 1.0},
        )
        patch.connect(o1.id, "out", mx.id, "in1")
        patch.connect(o2.id, "out", mx.id, "in2")
        backend = self._backend()
        backend.compile(patch)

        # Inject constant buffers directly to make the arithmetic exact.
        buffers = {
            (o1.id, "out"): np.full(64, 0.3, dtype=np.float32),
            (o2.id, "out"): np.full(64, 0.4, dtype=np.float32),
        }
        out = backend._render_mixer(mx, 64, buffers, patch)
        np.testing.assert_allclose(out, np.full(64, 0.7, dtype=np.float32), atol=1e-6)

    def test_per_channel_gain_scales_that_channel_only(self):
        patch = Patch()
        o1 = patch.add_module("oscillator")
        o2 = patch.add_module("oscillator")
        mx = patch.add_module(
            "mixer",
            params={"gain1": 0.5, "gain2": 1.0, "gain3": 1.0, "gain4": 1.0, "master": 1.0},
        )
        patch.connect(o1.id, "out", mx.id, "in1")
        patch.connect(o2.id, "out", mx.id, "in2")
        backend = self._backend()
        backend.compile(patch)
        buffers = {
            (o1.id, "out"): np.full(32, 1.0, dtype=np.float32),
            (o2.id, "out"): np.full(32, 1.0, dtype=np.float32),
        }
        out = backend._render_mixer(mx, 32, buffers, patch)
        # 0.5 + 1.0 = 1.5 → master 1.0 → 1.5
        np.testing.assert_allclose(out, 1.5, atol=1e-6)

    def test_master_scales_whole_output(self):
        patch = Patch()
        o1 = patch.add_module("oscillator")
        mx = patch.add_module(
            "mixer",
            params={"gain1": 1.0, "gain2": 1.0, "gain3": 1.0, "gain4": 1.0, "master": 0.25},
        )
        patch.connect(o1.id, "out", mx.id, "in1")
        backend = self._backend()
        backend.compile(patch)
        buffers = {(o1.id, "out"): np.full(16, 0.8, dtype=np.float32)}
        out = backend._render_mixer(mx, 16, buffers, patch)
        np.testing.assert_allclose(out, 0.8 * 0.25, atol=1e-6)

    def test_four_inputs_all_contribute(self):
        patch = Patch()
        oscs = [patch.add_module("oscillator") for _ in range(4)]
        mx = patch.add_module(
            "mixer",
            params={"gain1": 1.0, "gain2": 1.0, "gain3": 1.0, "gain4": 1.0, "master": 1.0},
        )
        for i, osc in enumerate(oscs, start=1):
            patch.connect(osc.id, "out", mx.id, f"in{i}")
        backend = self._backend()
        backend.compile(patch)
        buffers = {(osc.id, "out"): np.full(8, 0.1, dtype=np.float32) for osc in oscs}
        out = backend._render_mixer(mx, 8, buffers, patch)
        np.testing.assert_allclose(out, 0.4, atol=1e-6)

    def test_disconnected_channels_contribute_silence(self):
        """Wire in1 and in4; in2/in3 unpatched should contribute 0."""
        patch = Patch()
        o1 = patch.add_module("oscillator")
        o4 = patch.add_module("oscillator")
        mx = patch.add_module(
            "mixer",
            params={"gain1": 1.0, "gain2": 1.0, "gain3": 1.0, "gain4": 1.0, "master": 1.0},
        )
        patch.connect(o1.id, "out", mx.id, "in1")
        patch.connect(o4.id, "out", mx.id, "in4")
        backend = self._backend()
        backend.compile(patch)
        buffers = {
            (o1.id, "out"): np.full(16, 0.2, dtype=np.float32),
            (o4.id, "out"): np.full(16, 0.5, dtype=np.float32),
        }
        out = backend._render_mixer(mx, 16, buffers, patch)
        np.testing.assert_allclose(out, 0.7, atol=1e-6)


class TestMixerIntegration:
    def test_full_chain_three_detuned_saws_through_mixer(self):
        """End-to-end render_block on the fat_saw example shape produces
        finite audio and is non-silent."""
        sr = 44100
        patch = Patch()
        s1 = patch.add_module(
            "oscillator", params={"waveform": "saw", "freq": 218.5, "amp": 0.4}
        )
        s2 = patch.add_module(
            "oscillator", params={"waveform": "saw", "freq": 220.0, "amp": 0.4}
        )
        s3 = patch.add_module(
            "oscillator", params={"waveform": "saw", "freq": 221.5, "amp": 0.4}
        )
        mx = patch.add_module(
            "mixer",
            params={"gain1": 1.0, "gain2": 1.0, "gain3": 1.0, "gain4": 0.0, "master": 0.6},
        )
        spk = patch.add_module("speaker_output", params={"gain": 0.8})
        patch.connect(s1.id, "out", mx.id, "in1")
        patch.connect(s2.id, "out", mx.id, "in2")
        patch.connect(s3.id, "out", mx.id, "in3")
        patch.connect(mx.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        backend.compile(patch)
        block = backend.render_block(1024)
        assert np.all(np.isfinite(block))
        assert float(np.max(np.abs(block))) > 0.05  # not silent
        assert float(np.max(np.abs(block))) <= 1.0  # clipped at speaker

    def test_example_fat_saw_loads_and_renders(self):
        """The shipped fat_saw.json must load and produce audio."""
        from pathlib import Path
        from pysynthrack.io_patch import load_patch

        example = Path(__file__).parent.parent / "examples" / "fat_saw.json"
        patch = load_patch(example)
        # Sanity: mixer is in there with 4 inputs declared.
        mx = next(m for m in patch if m.TYPE == "mixer")
        assert len(mx.input_ports) == 4

        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        block = backend.render_block(512)
        assert np.all(np.isfinite(block))
        assert float(np.max(np.abs(block))) > 0.0
