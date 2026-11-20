"""`late N`: the device layer's own underflows, counted (2026-11-21).

Matthew's stutter on an MME device with ``under`` steady at 1 -- the ring was
never running dry, so the glitch was below it, where nothing counted. The
device callback now counts the callbacks PortAudio flags
``output_underflow``, and the node's first line ends ``late N``.
"""
from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import _DeviceOutput
from pysynthrack.ui.buffer import format_sink_buffer
from tests.test_sink_open_fallback import FakeSD, _running, _sink_patch

SR = 44100


class _Flags:
    """Stands in for sounddevice.CallbackFlags."""

    def __init__(self, underflow):
        self.output_underflow = underflow

    def __bool__(self):
        return self.output_underflow


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(nb, "sd", sd)
    return sd


class TestTheCount:
    def test_flagged_callbacks_count_and_clean_ones_do_not(self):
        d = _DeviceOutput("X", SR, 512)
        out = np.zeros((512, 2), np.float32)
        for flags in (None, _Flags(False), _Flags(True), _Flags(True), None):
            d._callback(out, 512, None, flags)
        assert d._late == 2
        assert d.telemetry()[2] == 0               # not the ring's own counter

    def test_close_resets_it(self):
        d = _DeviceOutput("X", SR, 512)
        d._callback(np.zeros((512, 2), np.float32), 512, None, _Flags(True))
        d.close()
        assert d._late == 0

    def test_the_backend_hands_it_to_each_sink(self, fake_sd):
        p, s = _sink_patch(buffer_size=2048)
        b = _running(p)
        (_k, d), = b._device_outputs.items()
        d._callback(np.zeros((2048, 2), np.float32), 2048, None, _Flags(True))
        assert b.snapshot_sink_late() == {s.id: 1}


class TestTheReadout:
    def test_the_first_line_ends_late_n(self):
        assert format_sink_buffer((3686, 8192, 0, 0), late=3) == (
            "buffer 45% (3686/8192)  under 0  drop 0  late 3"
        )

    def test_without_it_the_line_is_unchanged(self):
        assert format_sink_buffer((3686, 8192, 0, 0)) == (
            "buffer 45% (3686/8192)  under 0  drop 0"
        )

    def test_a_late_tick_flashes_the_readout_amber(self, fake_sd, monkeypatch):
        pytest.importorskip("dearpygui.dearpygui")
        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        dpg = mock.MagicMock()
        monkeypatch.setattr(app_mod, "dpg", dpg)
        p, s = _sink_patch(buffer_size=2048)
        b = _running(p)
        app = app_mod.App()
        app.backend, app.patch = b, b._patch
        app._sink_buffer_labels = {s.id: "lbl"}
        app._update_sink_buffers()                  # baseline
        (_k, d), = b._device_outputs.items()
        d._callback(np.zeros((2048, 2), np.float32), 2048, None, _Flags(True))
        app._update_sink_buffers()
        colors = [c.kwargs.get("color") for c in dpg.configure_item.call_args_list]
        assert colors[-1] == app._SINK_BUF_TROUBLE_RGBA
        text = dpg.set_value.call_args_list[-1].args[1]
        assert "late 1" in text.splitlines()[0]
