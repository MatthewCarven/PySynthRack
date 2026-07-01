"""Tests for the core model layer.

These tests don't touch audio or the UI — they run on any machine with
Python and numpy.
"""
from __future__ import annotations

import pytest

# Import modules subpackage so its types register with the core registry.
import pysynthrack.modules  # noqa: F401
from pysynthrack.core import Cable, Patch, Port, all_module_types


class TestPort:
    def test_compatible(self):
        out = Port("out", "out", "audio")
        inp = Port("in", "in", "audio")
        assert out.is_compatible_with(inp)
        assert inp.is_compatible_with(out)

    def test_same_direction_incompatible(self):
        a = Port("a", "out")
        b = Port("b", "out")
        assert not a.is_compatible_with(b)

    def test_different_kind_incompatible(self):
        audio = Port("a", "out", "audio")
        cv = Port("c", "in", "cv")
        assert not audio.is_compatible_with(cv)


class TestPatch:
    def test_add_module_assigns_increasing_ids(self):
        patch = Patch()
        m1 = patch.add_module("oscillator")
        m2 = patch.add_module("oscillator")
        assert m1.id == 1
        assert m2.id == 2

    def test_connect_oscillator_to_output(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        out = patch.add_module("speaker_output")
        cable = patch.connect(osc.id, "out", out.id, "in")
        assert cable in patch.cables
        assert len(patch.cables) == 1

    def test_connect_rejects_same_direction(self):
        patch = Patch()
        osc1 = patch.add_module("oscillator")
        osc2 = patch.add_module("oscillator")
        with pytest.raises(KeyError):
            # oscillator has no input port — get_port raises KeyError.
            patch.connect(osc1.id, "out", osc2.id, "in")

    def test_connect_rejects_duplicate_destination(self):
        patch = Patch()
        osc1 = patch.add_module("oscillator")
        osc2 = patch.add_module("oscillator")
        out = patch.add_module("speaker_output")
        patch.connect(osc1.id, "out", out.id, "in")
        with pytest.raises(ValueError):
            patch.connect(osc2.id, "out", out.id, "in")

    def test_disconnect(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        out = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", out.id, "in")
        removed = patch.disconnect(osc.id, "out", out.id, "in")
        assert removed is True
        assert patch.cables == []

    def test_remove_module_cleans_cables(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        out = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", out.id, "in")
        patch.remove_module(osc.id)
        assert patch.cables == []
        assert osc.id not in patch.modules


class TestRegistry:
    def test_oscillator_registered(self):
        types = all_module_types()
        assert "oscillator" in types
        assert "speaker_output" in types


class TestSerialization:
    def test_round_trip_via_dict(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"freq": 220.0, "amp": 0.4})
        out = patch.add_module("speaker_output", params={"gain": 0.9})
        patch.connect(osc.id, "out", out.id, "in")

        data = patch.to_dict()
        restored = Patch.from_dict(data)

        assert set(restored.modules) == {osc.id, out.id}
        assert restored.modules[osc.id].params["freq"] == 220.0
        assert restored.modules[out.id].params["gain"] == 0.9
        assert len(restored.cables) == 1
        cable = restored.cables[0]
        assert cable.src_module_id == osc.id
        assert cable.dst_module_id == out.id

    def test_next_id_survives_round_trip(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        out = patch.add_module("speaker_output")
        restored = Patch.from_dict(patch.to_dict())
        # The next module added to the restored patch must get id 3, not 1.
        new = restored.add_module("oscillator")
        assert new.id == 3
        assert new.id not in {osc.id, out.id}


class TestParamValidation:
    def test_unknown_param_rejected(self):
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("oscillator", params={"nope": 1})

    def test_known_param_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"freq": 123.4})
        assert osc.params["freq"] == 123.4


class TestParamAliases:
    """Legacy param names (renamed for consistency) still load via aliases."""

    def test_legacy_name_maps_on_load(self):
        kb = Patch().add_module("keyboard", params={"volume": 0.7})
        assert kb.params["amp"] == 0.7 and "volume" not in kb.params

    def test_legacy_name_maps_on_set_param(self):
        kb = Patch().add_module("keyboard")
        kb.set_param("volume", 0.2)
        assert kb.params["amp"] == 0.2

    def test_crossover_frequency_alias(self):
        xo = Patch().add_module("crossover", params={"frequency": 800.0})
        assert xo.params["freq"] == 800.0

    def test_to_dict_uses_canonical_name(self):
        d = Patch().add_module("keyboard", params={"volume": 0.9}).to_dict()
        assert "amp" in d["params"] and "volume" not in d["params"]

    def test_unknown_param_still_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("keyboard", params={"loudness": 1.0})
