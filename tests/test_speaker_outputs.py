"""Left/RightSpeakerOut — hard-panned mono sinks.

v0.4: the speaker family grows two channel-pinned variants. The numpy
backend's drain mixes ``left_speaker_output`` sinks into the left bus
only and ``right_speaker_output`` into the right bus only; the original
``speaker_output`` keeps feeding both. A Left + Right pair is poor-man's
stereo without a dedicated stereo Speaker module.
"""
import numpy as np

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.io_patch.patch_io import patch_from_json, patch_to_json

SR = 44100


def _patch_osc_into(sink_type, freq=440.0, gain=1.0):
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": "sine", "freq": freq, "amp": 0.5}
    )
    sink = patch.add_module(sink_type, params={"gain": gain})
    patch.connect(osc.id, "out", sink.id, "in")
    backend = NumpyBackend(sample_rate=SR, block_size=512)
    backend.compile(patch)
    return backend, patch


class TestModel:
    def test_types_registered(self):
        types = all_module_types()
        assert "left_speaker_output" in types
        assert "right_speaker_output" in types

    def test_ports_and_params(self):
        for t in ("left_speaker_output", "right_speaker_output"):
            cls = get_module_type(t)
            assert [p.name for p in cls.INPUT_PORTS] == ["in"]
            assert cls.OUTPUT_PORTS == []
            assert cls.DEFAULT_PARAMS == {"gain": 1.0}

    def test_json_round_trip(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        left = patch.add_module("left_speaker_output", params={"gain": 0.7})
        right = patch.add_module("right_speaker_output", params={"gain": 0.3})
        patch.connect(osc.id, "out", left.id, "in")
        patch.connect(osc.id, "out", right.id, "in")
        text = patch_to_json(patch)
        patch2 = patch_from_json(text)
        types = sorted(m.TYPE for m in patch2.modules.values())
        assert types == ["left_speaker_output", "oscillator", "right_speaker_output"]
        left2 = next(
            m for m in patch2.modules.values() if m.TYPE == "left_speaker_output"
        )
        assert left2.params["gain"] == 0.7
        assert len(patch2.cables) == 2


class TestStereoDrain:
    def test_left_sink_fills_left_only(self):
        backend, _ = _patch_osc_into("left_speaker_output")
        out = backend.render_block(512)
        assert out.shape == (512, 2)
        assert np.max(np.abs(out[:, 0])) > 0.1, "left channel silent"
        assert np.allclose(out[:, 1], 0.0), "right channel should be silent"

    def test_right_sink_fills_right_only(self):
        backend, _ = _patch_osc_into("right_speaker_output")
        out = backend.render_block(512)
        assert np.allclose(out[:, 0], 0.0), "left channel should be silent"
        assert np.max(np.abs(out[:, 1])) > 0.1, "right channel silent"

    def test_plain_speaker_unchanged_fills_both(self):
        backend, _ = _patch_osc_into("speaker_output")
        out = backend.render_block(512)
        assert np.max(np.abs(out[:, 0])) > 0.1
        np.testing.assert_allclose(out[:, 0], out[:, 1], atol=1e-7)

    def test_left_right_pair_hard_pans(self):
        patch = Patch()
        osc_l = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 220.0, "amp": 0.5}
        )
        osc_r = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 880.0, "amp": 0.5}
        )
        left = patch.add_module("left_speaker_output")
        right = patch.add_module("right_speaker_output")
        patch.connect(osc_l.id, "out", left.id, "in")
        patch.connect(osc_r.id, "out", right.id, "in")
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        out = backend.render_block(2048)
        # Both channels audible...
        assert np.max(np.abs(out[:, 0])) > 0.1
        assert np.max(np.abs(out[:, 1])) > 0.1
        # ...and carrying different signals (different pitches => the
        # channels must not be copies of each other).
        assert not np.allclose(out[:, 0], out[:, 1], atol=1e-3)
        # Zero-crossing count confirms which pitch went where.
        zc_l = int(np.sum(np.abs(np.diff(np.signbit(out[:, 0])))))
        zc_r = int(np.sum(np.abs(np.diff(np.signbit(out[:, 1])))))
        assert zc_r > zc_l * 2, f"expected right (880) >> left (220): {zc_l} vs {zc_r}"

    def test_gain_applied_per_sink(self):
        backend_full, _ = _patch_osc_into("left_speaker_output", gain=1.0)
        backend_half, _ = _patch_osc_into("left_speaker_output", gain=0.5)
        full = backend_full.render_block(512)[:, 0]
        half = backend_half.render_block(512)[:, 0]
        np.testing.assert_allclose(half, full * 0.5, atol=1e-6)

    def test_mixing_pinned_and_plain_speakers(self):
        # osc -> speaker_output (both) AND osc -> left (left only):
        # left bus gets 2x the signal of the right bus.
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.25}
        )
        both = patch.add_module("speaker_output")
        left = patch.add_module("left_speaker_output")
        patch.connect(osc.id, "out", both.id, "in")
        patch.connect(osc.id, "out", left.id, "in")
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        out = backend.render_block(512)
        np.testing.assert_allclose(out[:, 0], out[:, 1] * 2.0, atol=1e-6)

    def test_voice_aware_source_collapses_into_pinned_channel(self):
        # Keyboard publishes (16, F) buffers; the pinned sink must sum
        # the voice axis (implicit-sum-at-mono-sinks) and land left-only.
        patch = Patch()
        kb = patch.add_module("keyboard", params={"volume": 1.0})
        left = patch.add_module("left_speaker_output")
        patch.connect(kb.id, "out", left.id, "in")
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        kb.note_on(60)
        kb.note_on(64)
        for _ in range(5):
            out = backend.render_block(512)
        assert out.shape == (512, 2)
        assert np.max(np.abs(out[:, 0])) > 0.1
        assert np.allclose(out[:, 1], 0.0)

    def test_unconnected_pinned_sink_is_silent(self):
        patch = Patch()
        patch.add_module("left_speaker_output")
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        out = backend.render_block(512)
        assert np.allclose(out, 0.0)
