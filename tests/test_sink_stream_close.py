"""A device stream is always released, and a rebuild on the same device
closes the old stream before the new one opens (2026-11-20).

Matthew, changing device_block 512 -> 1024 live on an MME device: the output
"kept repeating the last buffer", and after Stop / Start the OLD stream kept
playing alongside the new one until he exited. `_DeviceOutput.close()` ran
`stop()` and `close()` in one `try` that swallowed errors, so a `stop()` that
raised skipped `close()` and orphaned the PortAudio stream -- out of the
backend's map, beyond Stop's reach. These pin the repair with a fake
sounddevice that records every call in order.
"""
from __future__ import annotations

from unittest import mock

import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend, _DeviceOutput
from pysynthrack.core import Patch

SR, F = 44100, 512
BUFFERED = "buffered_specific_speaker_output"


class LoggingSD:
    """A fake `sounddevice` whose streams log every call to `events`."""

    def __init__(self):
        self.events: list[tuple] = []
        self.stop_raises = False
        self.abort_raises = False
        self.close_raises = False
        fake = self

        class OutputStream:
            def __init__(self, **kw):
                self.kw = kw
                fake.events.append(("open", kw["device"], kw["blocksize"]))

            def _tag(self, what):
                fake.events.append((what, self.kw["device"], self.kw["blocksize"]))

            def start(self):
                self._tag("start")

            def stop(self):
                self._tag("stop")
                if fake.stop_raises:
                    raise RuntimeError("Unanticipated host error")

            def abort(self):
                self._tag("abort")
                if fake.abort_raises:
                    raise RuntimeError("abort refused")

            def close(self):
                self._tag("close")
                if fake.close_raises:
                    raise RuntimeError("close refused")

        self.OutputStream = OutputStream

    def of(self, what):
        return [e for e in self.events if e[0] == what]


@pytest.fixture
def sd(monkeypatch):
    fake = LoggingSD()
    monkeypatch.setattr(nb, "sd", fake)
    return fake


def _running(device="Cans", buffer_size=4096, device_block=512):
    p = Patch()
    o = p.add_module("oscillator")
    s = p.add_module(BUFFERED, params={"device": device, "buffer_size": buffer_size,
                                       "device_block": device_block})
    p.connect(o.id, "out", s.id, "in_l")
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(p)
    b._running = True
    b._sync_device_outputs()
    return b, s


class TestTheStreamIsAlwaysReleased:
    def test_a_clean_close_is_stop_then_close(self, sd):
        d = _DeviceOutput("Cans", SR, 512)
        d.open()
        assert d.close() == []
        assert [e[0] for e in sd.events] == ["open", "start", "stop", "close"]

    def test_a_failed_stop_still_aborts_and_closes(self, sd):
        sd.stop_raises = True
        d = _DeviceOutput("Cans", SR, 512)
        d.open()
        errors = d.close()
        assert [e[0] for e in sd.events] == ["open", "start", "stop", "abort", "close"]
        assert errors and "stop failed" in errors[0]
        assert d.zombie is None                       # released all the same

    def test_a_stream_that_will_not_close_is_kept_and_retried_at_stop(self, sd):
        sd.stop_raises = sd.close_raises = True
        b, s = _running(device_block=512)
        b.set_param(s.id, "device_block", 1024)       # the live change
        assert len(b._zombie_streams) == 1
        assert any("may still be playing" in t for t in b._device_close_errors)
        sd.close_raises = False                       # the driver recovers
        b._stream = mock.MagicMock()
        b.stop()
        assert b._zombie_streams == []
        assert sd.of("close")[-1] == ("close", "Cans", 512)   # the orphan, closed at last


class TestARebuildClosesFirst:
    def test_a_new_device_block_closes_the_old_stream_before_opening(self, sd):
        b, s = _running(device_block=512)
        sd.events.clear()
        b.set_param(s.id, "device_block", 1024)
        kinds = [(e[0], e[2]) for e in sd.events]
        assert kinds.index(("close", 512)) < kinds.index(("open", 1024))
        assert set(b._device_outputs) == {("Cans", 4096, 1024)}

    def test_a_new_buffer_size_does_too(self, sd):
        b, s = _running(buffer_size=2048, device_block="auto")
        sd.events.clear()
        b.set_param(s.id, "buffer_size", 4096)
        kinds = [(e[0], e[2]) for e in sd.events]
        assert kinds.index(("close", 2048)) < kinds.index(("open", 4096))

    def test_moving_to_another_device_keeps_the_old_order(self, sd):
        # Not a rebuild on one device: the new device opens first and the
        # old one closes after the swap, as it always did.
        b, s = _running(device="Cans")
        sd.events.clear()
        b.set_param(s.id, "device", "Desk")
        names = [(e[0], e[1]) for e in sd.events]
        assert names.index(("open", "Desk")) < names.index(("close", "Cans"))

    def test_the_status_bar_says_a_bad_close_once(self, sd, monkeypatch):
        pytest.importorskip("dearpygui.dearpygui")
        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
        sd.stop_raises = True
        b, s = _running(device_block=512)
        app = app_mod.App()
        app.backend, app.patch = b, b._patch
        said = []
        app._set_status = said.append
        b.set_param(s.id, "device_block", 1024)
        app._report_sink_streams()
        app._report_sink_streams()
        closes = [t for t in said if "stop failed" in t]
        assert len(closes) == 1 and "released anyway" in closes[0]
