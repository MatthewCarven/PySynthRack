"""Tests for the MicInput source module (live capture -> stereo audio).

Headless: the renderer reads ``NumpyBackend._input_block`` (normally set by
the duplex audio callback); the tests inject that array directly, so no
audio device, PortAudio, or duplex stream is needed. The duplex stream
setup in ``start()`` is verified live on a real machine, not here.
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import all_module_types
from pysynthrack.modules import micinput


SR = 44100


def _mic_render(backend, module, patch, frames):
    return backend._render_module(module, frames, {}, patch)


class TestModuleShape:
    def test_registered_with_stereo_outs_no_inputs(self):
        assert "mic_input" in all_module_types()
        mi = all_module_types()["mic_input"](1)
        assert [p.name for p in mi.output_ports] == ["left", "right"]
        assert all(p.signal_kind == "audio" for p in mi.output_ports)
        assert mi.input_ports == []

    def test_default_params(self):
        mi = all_module_types()["mic_input"](1)
        assert mi.params == {"device": "", "gain": 1.0}


class TestRenderer:
    def _backend(self):
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        mic = patch.add_module("mic_input")
        be.compile(patch)
        return be, mic, patch

    def test_stereo_block_maps_to_left_right(self):
        be, mic, patch = self._backend()
        blk = np.zeros((512, 2), dtype=np.float32)
        blk[:, 0] = 0.3
        blk[:, 1] = -0.4
        be._input_block = blk
        out = _mic_render(be, mic, patch, 512)
        assert np.allclose(out["left"], 0.3) and np.allclose(out["right"], -0.4)

    def test_mono_block_duplicated_to_both(self):
        be, mic, patch = self._backend()
        be._input_block = np.full((512, 1), 0.25, dtype=np.float32)
        out = _mic_render(be, mic, patch, 512)
        assert np.allclose(out["left"], 0.25) and np.allclose(out["right"], 0.25)

    def test_gain_scales_both_channels(self):
        be, mic, patch = self._backend()
        mic.params["gain"] = 0.5
        blk = np.zeros((512, 2), dtype=np.float32)
        blk[:, 0] = 0.4
        blk[:, 1] = -0.2
        be._input_block = blk
        out = _mic_render(be, mic, patch, 512)
        assert np.allclose(out["left"], 0.2) and np.allclose(out["right"], -0.1)

    def test_no_input_block_is_silence(self):
        be, mic, patch = self._backend()
        be._input_block = None
        out = _mic_render(be, mic, patch, 512)
        assert np.all(out["left"] == 0.0) and np.all(out["right"] == 0.0)

    def test_short_block_zero_padded(self):
        be, mic, patch = self._backend()
        be._input_block = np.ones((300, 2), dtype=np.float32)
        out = _mic_render(be, mic, patch, 512)
        assert np.all(out["left"][:300] == 1.0) and np.all(out["left"][300:] == 0.0)

    def test_long_block_truncated(self):
        be, mic, patch = self._backend()
        be._input_block = np.ones((1000, 2), dtype=np.float32)
        out = _mic_render(be, mic, patch, 512)
        assert out["left"].shape[0] == 512 and np.all(out["left"] == 1.0)

    def test_full_dispatch_mic_into_speaker(self):
        patch = Patch()
        mic = patch.add_module("mic_input")
        spk = patch.add_module("speaker_output")
        patch.connect(mic.id, "left", spk.id, "in")
        be = NumpyBackend(sample_rate=SR, block_size=256)
        be.compile(patch)
        be._input_block = np.full((256, 2), 0.5, dtype=np.float32)
        out = be.render_block(256)
        assert out is not None
        assert np.all(np.isfinite(out)) and np.max(np.abs(out)) > 0.0

    def test_stop_clears_input_block(self):
        be, mic, patch = self._backend()
        be._input_block = np.ones((512, 2), dtype=np.float32)
        be._running = True
        be._stream = type("S", (), {"stop": lambda s: None, "close": lambda s: None})()
        be.stop()
        assert be._input_block is None


class TestResolveMicInput:
    def test_default_device_resolves_to_none(self):
        be = NumpyBackend(sample_rate=SR)
        patch = Patch()
        mic = patch.add_module("mic_input")  # device=""
        dev, ch = be._resolve_mic_input(mic)
        assert dev is None          # "" -> system default input
        assert ch in (1, 2)         # clamped 1..2: mono-dup, or true stereo
                                    # (sandbox has no sounddevice -> 1; a real
                                    # stereo default mic -> 2)

    def test_named_device_passed_through(self):
        be = NumpyBackend(sample_rate=SR)
        patch = Patch()
        mic = patch.add_module("mic_input", params={"device": "Scarlett 2i2"})
        dev, ch = be._resolve_mic_input(mic)
        assert dev == "Scarlett 2i2"   # name passed straight through
        assert ch in (1, 2)            # clamped 1..2 regardless of host


class TestDeviceEnumeration:
    def test_no_sounddevice_returns_empty(self, monkeypatch):
        monkeypatch.setattr(micinput, "_HAS_SD", False)
        assert micinput.available_input_devices() == []

    def test_filters_input_devices_and_dedupes(self, monkeypatch):
        fake = [
            {"name": "Built-in Mic", "max_input_channels": 1},
            {"name": "Speakers", "max_input_channels": 0},      # output-only, drop
            {"name": "USB Interface", "max_input_channels": 2},
            {"name": "USB Interface", "max_input_channels": 2},  # dupe, drop
        ]

        class _FakeSD:
            @staticmethod
            def query_devices():
                return fake

        monkeypatch.setattr(micinput, "_HAS_SD", True)
        monkeypatch.setattr(micinput, "_sd", _FakeSD)
        assert micinput.available_input_devices() == ["Built-in Mic", "USB Interface"]

    def test_enumeration_never_raises(self, monkeypatch):
        class _BoomSD:
            @staticmethod
            def query_devices():
                raise RuntimeError("audio stack on fire")

        monkeypatch.setattr(micinput, "_HAS_SD", True)
        monkeypatch.setattr(micinput, "_sd", _BoomSD)
        assert micinput.available_input_devices() == []
