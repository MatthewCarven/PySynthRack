"""Tests for SpecificStereoSpeakerOutput — the device-targetable stereo sink.

This is slice 1: the clone plus a live ``device`` picker. The audio still
drains into the shared master bus, so the correctness contract here is
*equivalence* — for every wiring and knob setting, this sink must produce a
**bit-identical** master bus to :class:`StereoSpeakerOutput`, and the new
``device`` parameter must have **no** effect on the render (it is picker /
save-file only until the second-stream routing slice lands).

Coverage:
  - Model: registration, Outputs category, defaults (incl. ``device=""``),
    ports & signal kinds (identical to the stereo speaker), JSON round-trip
    of ``device``, ``set_param`` round-trip, unknown param rejected, type
    walls, sink-ness (drained, not rendered).
  - available_output_devices(): returns a list, never raises.
  - Drain equivalence: bit-identical to stereo_speaker_output across a mono /
    stereo / pan / width / gain / CV sweep, and invariant to the ``device``
    value (default, empty, and an unplugged bogus name all match).
  - Neutral default: a stereo pair passes to the bus bit-exactly via the
    shared _drain_stereo_speaker.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type, grouped_module_types
from pysynthrack.modules.output import (
    AUTO_DEVICE,
    SpecificStereoSpeakerOutput,
    available_output_devices,
)

SR, F = 44100, 512
TYPE = "specific_stereo_speaker_output"


def _render(patch, blocks=4):
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    return np.concatenate([b.render_block(F) for _ in range(blocks)]), b


def _build(sink_type, mode, params, cv=False):
    """Identical wiring for either sink type; only the sink TYPE differs."""
    patch = Patch()
    l = patch.add_module("oscillator", params={"amp": 0.4})
    sink = patch.add_module(sink_type, params=params)
    patch.connect(l.id, "out", sink.id, "in_l")
    if mode == "stereo":
        r = patch.add_module(
            "oscillator",
            params={"amp": 0.3, "waveform": "square", "freq": 330.0},
        )
        patch.connect(r.id, "out", sink.id, "in_r")
    if cv:
        lfo = patch.add_module("lfo", params={"rate": 2.0, "depth": 1.0})
        patch.connect(lfo.id, "cv", sink.id, "pan_cv")
        patch.connect(lfo.id, "cv", sink.id, "width_cv")
    return patch


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        sp = Patch().add_module(TYPE)
        assert isinstance(sp, SpecificStereoSpeakerOutput)
        assert sp.params == {
            "gain": 1.0, "pan": 0.0, "width": 1.0, "cv_depth": 1.0,
            "device": AUTO_DEVICE,
        }
        assert AUTO_DEVICE == ""

    def test_registered_in_outputs_category(self):
        assert get_module_type(TYPE) is SpecificStereoSpeakerOutput
        outs = dict(grouped_module_types())["Outputs"]
        assert TYPE in outs

    def test_ports_match_the_stereo_speaker(self):
        sp = Patch().add_module(TYPE)
        assert [(p.name, p.signal_kind) for p in sp.input_ports] == [
            ("in_l", "audio"), ("in_r", "audio"),
            ("pan_cv", "cv"), ("width_cv", "cv"),
        ]
        assert sp.output_ports == []

    def test_device_round_trips_through_json(self):
        patch = Patch()
        patch.add_module(TYPE, params={"device": "Focusrite USB", "pan": -0.4})
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == TYPE)
        assert mod.params["device"] == "Focusrite USB"
        assert mod.params["pan"] == -0.4

    def test_set_param_device(self):
        sp = Patch().add_module(TYPE)
        sp.set_param("device", "Speakers (Realtek)")
        assert sp.params["device"] == "Speakers (Realtek)"

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module(TYPE, params={"balance": 0.0})

    def test_type_walls(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        osc = patch.add_module("oscillator")
        sp = patch.add_module(TYPE)
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", sp.id, "in_l")     # cv -> audio
        with pytest.raises(Exception):
            patch.connect(osc.id, "out", sp.id, "pan_cv")  # audio -> cv

    def test_is_a_drained_sink_not_rendered(self):
        # Speaker-family sinks return None from _render_module (they are
        # mixed in the speaker pass), same as stereo_speaker_output.
        patch = Patch()
        sp = patch.add_module(TYPE)
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        assert b._render_module(sp, F, {}, patch) is None


class TestAvailableOutputDevices:
    def test_returns_list_never_raises(self):
        got = available_output_devices()
        assert isinstance(got, list)
        assert all(isinstance(n, str) for n in got)


# ----- Drain equivalence vs StereoSpeakerOutput ------------------------------

_PARAM_CASES = [
    ("mono", {}, False),
    ("mono", {"pan": -0.7}, False),
    ("mono", {"pan": 0.5, "gain": 0.8}, False),
    ("stereo", {}, False),                                   # neutral default
    ("stereo", {"pan": 0.4, "width": 1.6}, False),
    ("stereo", {"width": 0.0}, False),
    ("stereo", {"width": 2.0, "gain": 1.2}, False),
    ("mono", {"pan": 0.2, "cv_depth": 0.8}, True),
    ("stereo", {"width": 1.3, "cv_depth": 1.5}, True),
    ("stereo", {"pan": -0.3, "width": 0.7, "gain": 0.9, "cv_depth": 1.0}, True),
]


class TestDrainMatchesStereoSpeaker:
    @pytest.mark.parametrize("mode,params,cv", _PARAM_CASES)
    def test_bit_identical_master_bus(self, mode, params, cv):
        ref, _ = _render(_build("stereo_speaker_output", mode, dict(params), cv))
        pb = dict(params)
        pb["device"] = "Some Unplugged Device"  # must not change the drain
        got, _ = _render(_build(TYPE, mode, pb, cv))
        assert np.array_equal(ref, got)

    @pytest.mark.parametrize("device", [AUTO_DEVICE, "", "Bogus Interface 7"])
    def test_device_value_is_inert(self, device):
        ref, _ = _render(_build("stereo_speaker_output", "stereo", {"pan": 0.3}))
        got, _ = _render(
            _build(TYPE, "stereo", {"pan": 0.3, "device": device})
        )
        assert np.array_equal(ref, got)


class TestNeutralDefault:
    def test_stereo_pair_passes_bit_exactly(self):
        # Mirror of the stereo-speaker neutral test: at defaults a stereo
        # pair reaches the bus untouched through the shared drain.
        patch = Patch()
        l = patch.add_module("oscillator", params={"amp": 0.4})
        r = patch.add_module("oscillator", params={"amp": 0.3})
        sp = patch.add_module(TYPE)
        patch.connect(l.id, "out", sp.id, "in_l")
        patch.connect(r.id, "out", sp.id, "in_r")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        lb = np.full(F, 0.4, dtype=np.float32)
        rb = np.full(F, -0.2, dtype=np.float32)
        out = np.zeros((F, 2), dtype=np.float32)
        b._drain_stereo_speaker(
            sp, F, {(l.id, "out"): lb, (r.id, "out"): rb}, patch, out
        )
        assert np.array_equal(out[:, 0], lb)
        assert np.array_equal(out[:, 1], rb)
