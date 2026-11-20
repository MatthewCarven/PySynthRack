"""The speaker device list names each host-API entry, and a sink opens
exactly the entry it names (2026-11-21).

From Matthew's device tour (buffer 4096 / device_block 512): "Speakers
(Realtek(R) Audio)" could not be opened at all -- MME, DirectSound and WASAPI
all offer that name, and sounddevice refuses an ambiguous one; the WDM-KS
entries failed to open or to start; DirectSound's entry just worked; and the
list, de-duped by name, never let him choose which. The fake table below is
shaped like his machine.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.modules.output import (
    available_output_devices,
    device_label,
    resolve_output_device,
)
from tests.test_sink_open_fallback import FakeSD, _running, _sink_patch

HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"},
            {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
DEVICES = [
    {"name": "Microphone (Realtek(R) Audio)", "hostapi": 0, "max_output_channels": 0},
    {"name": "Speakers (Realtek(R) Audio)", "hostapi": 0, "max_output_channels": 2},
    {"name": "Speakers 2 (Realtek HD Audio output)", "hostapi": 0, "max_output_channels": 2},
    {"name": "Primary Sound Driver", "hostapi": 1, "max_output_channels": 2},
    {"name": "Speakers (Realtek(R) Audio)", "hostapi": 1, "max_output_channels": 2},
    {"name": "Speakers 2 (Realtek HD Audio output)", "hostapi": 1, "max_output_channels": 2},
    {"name": "Speakers (Realtek(R) Audio)", "hostapi": 2, "max_output_channels": 2},
    {"name": "Speakers 2 (Realtek HD Audio output)", "hostapi": 2, "max_output_channels": 8},
    {"name": "Speakers ()", "hostapi": 3, "max_output_channels": 2},
    {"name": "Speakers 1 (Realtek HD Audio output with SST)", "hostapi": 3,
     "max_output_channels": 2},
]


class FakeQuery:
    def query_hostapis(self):
        return HOSTAPIS

    def query_devices(self):
        return DEVICES


class FakeSDWithDevices(FakeSD):
    """The open-recording fake, plus the device queries."""

    def query_hostapis(self):
        return HOSTAPIS

    def query_devices(self):
        return DEVICES


Q = FakeQuery()


class TestTheList:
    def test_each_host_api_entry_is_its_own_labelled_item(self):
        got = available_output_devices(Q)
        assert got == [
            "MME: Speakers (Realtek(R) Audio)",
            "MME: Speakers 2 (Realtek HD Audio output)",
            "DirectSound: Primary Sound Driver",
            "DirectSound: Speakers (Realtek(R) Audio)",
            "DirectSound: Speakers 2 (Realtek HD Audio output)",
            "WASAPI: Speakers (Realtek(R) Audio)",
            "WASAPI: Speakers 2 (Realtek HD Audio output)",
        ]

    def test_wdm_ks_and_inputs_are_left_out(self):
        got = available_output_devices(Q)
        assert not any(x.startswith("WDM-KS") for x in got)
        assert not any("Microphone" in x for x in got)

    def test_it_never_raises(self):
        class Broken:
            def query_hostapis(self):
                raise RuntimeError("no audio")

        assert available_output_devices(Broken()) == []


class TestResolving:
    def test_a_list_entry_opens_exactly_that_entry(self):
        assert resolve_output_device("DirectSound: Speakers (Realtek(R) Audio)", Q) == 4
        assert resolve_output_device("WASAPI: Speakers 2 (Realtek HD Audio output)", Q) == 7
        assert resolve_output_device("MME: Speakers 2 (Realtek HD Audio output)", Q) == 2

    def test_an_entry_that_is_not_there_comes_back_unchanged(self):
        # (so the open fails and says so, rather than opening something else)
        t = "WASAPI: Headphones (USB)"
        assert resolve_output_device(t, Q) == t

    def test_an_old_ambiguous_name_opens_directsound(self):
        # The name that "could not open at any buffer size" before.
        assert resolve_output_device("Speakers (Realtek(R) Audio)", Q) == 4

    def test_an_old_unique_name_opens_its_entry(self):
        assert resolve_output_device("Primary Sound Driver", Q) == 3
        assert resolve_output_device(
            "Speakers 1 (Realtek HD Audio output with SST)", Q) == 9   # even WDM-KS

    def test_anything_else_is_left_to_sounddevice_as_before(self):
        assert resolve_output_device("Realtek", Q) == "Realtek"
        assert resolve_output_device("", Q) == ""

    def test_without_device_queries_nothing_changes(self):
        assert resolve_output_device("DirectSound: X", object()) == "DirectSound: X"

    def test_the_label_round_trips(self):
        for i, d in enumerate(DEVICES):
            if d["max_output_channels"] and d["hostapi"] != 3:
                tag = ["MME", "DirectSound", "WASAPI"][d["hostapi"]]
                assert resolve_output_device(device_label(d["name"], tag), Q) == i


class TestTheBackendOpensTheIndex:
    def test_the_stream_gets_the_resolved_index(self, monkeypatch):
        sd = FakeSDWithDevices()
        monkeypatch.setattr(nb, "sd", sd)
        p, s = _sink_patch(device="DirectSound: Speakers (Realtek(R) Audio)",
                           buffer_size=2048)
        b = _running(p)
        assert sd.attempts[0] == (4, 2048)
        assert b.snapshot_sink_streams()[s.id] == (2048, 2048, "")
        # the key, the readout and the patch keep the label
        assert set(b._device_outputs) == {("DirectSound: Speakers (Realtek(R) Audio)", 2048)}
        b._fill_output(np.zeros((512, 2), np.float32), 512)

    def test_an_old_patch_with_the_ambiguous_name_now_opens(self, monkeypatch):
        sd = FakeSDWithDevices()
        monkeypatch.setattr(nb, "sd", sd)
        p, s = _sink_patch(device="Speakers (Realtek(R) Audio)", buffer_size=2048)
        b = _running(p)
        assert sd.attempts[0] == (4, 2048)
        assert b._device_open_failures == {}


@pytest.mark.parametrize("label", ["DirectSound: Speakers (Realtek(R) Audio)"])
def test_the_combo_offers_the_labels(label, monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setattr(app_mod, "spk_available_devices", lambda: available_output_devices(Q))
    items, n = app_mod.App._device_combo_items("buffered_specific_speaker_output", "")
    assert label in items and n == 7 and items[0] == ""
