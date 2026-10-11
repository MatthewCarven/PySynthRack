"""A device stream that won't open at the asked block size falls back -- and
says so -- instead of going silently idle.

The defect (screenshot-confirmed 2026-07-16): ``buffer_size`` 8192 on an HD
Audio device made ``_DeviceOutput.open()`` throw, ``_sync_device_outputs``
logged and skipped it, and the node read ``buffer: idle`` -- the same as a
sink with nothing to do. Same failure shape as the media-path bug and the
disk_writer status line: a subsystem that fails soft must SAY it failed.

Now ``open()`` walks a ladder (the asked size, halvings down to 256, then
blocksize 0 = PortAudio picks) and keeps the first that runs; the ring stays
sized by the asked buffer (the cushion), the stream keeps its ``(device,
asked)`` key, and ``snapshot_sink_streams`` reports ``(asked, opened,
error)`` for the readout and the status bar. Only a device that opens at no
size is silent, and then the readout says "could not open", in red.

No real audio stream is ever opened here: ``numpy_backend.sd`` is replaced
by a fake whose ``OutputStream`` refuses whatever each test says it should.
"""
from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import (
    _AUTO_RING_MIN,
    _MIN_FALLBACK_BLOCK,
    NumpyBackend,
    _DeviceOutput,
    _open_ladder,
)
from pysynthrack.core import Patch
from pysynthrack.ui.buffer import format_sink_buffer

SR, F = 44100, 512
BUFFERED = "buffered_specific_speaker_output"
WARPING = "warping_buffered_speaker_output"
SPECIFIC = "specific_stereo_speaker_output"


class _Refused(Exception):
    """Stands in for sounddevice.PortAudioError (a refused open)."""


class FakeSD:
    """Replaces the ``sounddevice`` module inside numpy_backend.

    ``refuse(device, blocksize)`` decides whether the stream constructor
    throws; ``refuse_start`` does the same for ``start()``. Every attempted
    blocksize is logged in ``attempts``; every stream closed in ``closed``.
    """

    def __init__(self):
        self.refuse = lambda device, bs: False
        self.refuse_start = lambda device, bs: False
        self.lookup_error = False
        self.attempts: list[tuple[str, int]] = []
        self.closed: list[tuple[str, int]] = []
        self.started: list[tuple[str, int]] = []
        fake = self

        class OutputStream:
            def __init__(self, **kw):
                self.kw = kw
                dev, bs = kw["device"], kw["blocksize"]
                fake.attempts.append((dev, bs))
                if fake.lookup_error:
                    raise ValueError(f"No output device matching {dev!r}")
                if fake.refuse(dev, bs):
                    raise _Refused(
                        f"Error opening OutputStream: blocksize {bs} refused"
                    )

            def start(self):
                dev, bs = self.kw["device"], self.kw["blocksize"]
                if fake.refuse_start(dev, bs):
                    raise _Refused(f"Error starting stream at {bs}")
                fake.started.append((dev, bs))

            def stop(self):
                pass

            def close(self):
                fake.closed.append((self.kw["device"], self.kw["blocksize"]))

        self.OutputStream = OutputStream


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(nb, "sd", sd)
    return sd


def _sink_patch(sink_type=BUFFERED, device="Cans", buffer_size=8192, **extra):
    p = Patch()
    o = p.add_module("oscillator", params={"amp": 0.4})
    params = {"device": device, **extra}
    if sink_type != SPECIFIC:
        params["buffer_size"] = buffer_size
    s = p.add_module(sink_type, params=params)
    p.connect(o.id, "out", s.id, "in_l")
    return p, s


def _running(patch, block_size=F):
    """A backend that believes it is running, reconciled once (Start's
    secondary-stream step) -- the main stream is never opened."""
    b = NumpyBackend(sample_rate=SR, block_size=block_size)
    b.compile(patch)
    b._running = True
    b._sync_device_outputs()
    return b


# ----- the ladder -------------------------------------------------------------


class TestOpenLadder:
    def test_8192_halves_to_the_floor_then_lets_portaudio_pick(self):
        assert _open_ladder(8192) == [8192, 4096, 2048, 1024, 512, 256, 0]

    def test_small_sizes_go_straight_to_portaudio(self):
        assert _open_ladder(64) == [64, 0]
        assert _open_ladder(128) == [128, 0]
        assert _open_ladder(_MIN_FALLBACK_BLOCK) == [_MIN_FALLBACK_BLOCK, 0]

    def test_half_steps_halve_too(self):
        assert _open_ladder(768) == [768, 384, 0]
        assert _open_ladder(1024) == [1024, 512, 256, 0]

    def test_every_rung_is_smaller_than_the_last(self):
        for asked in (64, 128, 256, 384, 512, 768, 1024, 2048, 4096, 8192):
            ladder = _open_ladder(asked)
            assert ladder[0] == asked and ladder[-1] == 0
            assert all(a > b for a, b in zip(ladder, ladder[1:]))


# ----- _DeviceOutput.open -----------------------------------------------------


class TestDeviceOutputOpen:
    def test_asked_size_that_opens_is_used_as_is(self, fake_sd):
        d = _DeviceOutput("Cans", SR, 8192)
        d.open()
        assert fake_sd.attempts == [("Cans", 8192)]
        assert d.opened_block == 8192 and d._block_size == 8192
        assert d._capacity == 8 * 8192
        assert fake_sd.started == [("Cans", 8192)]

    def test_the_default_1024_opens_exactly_as_before(self, fake_sd):
        # ring_governor_monitor's configuration: one attempt, the asked
        # block, the 8 x 1024 ring Matthew's eyeball ran on.
        d = _DeviceOutput("Cans", SR, 1024)
        d.open()
        assert fake_sd.attempts == [("Cans", 1024)]
        assert (d.opened_block, d._block_size, d._capacity) == (1024, 1024, 8192)

    def test_refused_8192_steps_down_to_4096(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs == 8192
        d = _DeviceOutput("Cans", SR, 8192)
        d.open()
        assert fake_sd.attempts == [("Cans", 8192), ("Cans", 4096)]
        assert d.opened_block == 4096 and d._block_size == 4096
        assert d.requested_block == 8192
        # The cushion asked for survives the fallback: the ring is still
        # sized by the asked buffer, not by the smaller device block.
        assert d._capacity == 8 * 8192
        assert d.telemetry() == (0, 8 * 8192, 0, 0)

    def test_keeps_stepping_until_one_opens(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs > 2048
        d = _DeviceOutput("Cans", SR, 8192)
        d.open()
        assert [bs for _, bs in fake_sd.attempts] == [8192, 4096, 2048]
        assert d.opened_block == 2048

    def test_a_stream_that_will_not_start_is_closed_and_stepped_past(self, fake_sd):
        fake_sd.refuse_start = lambda dev, bs: bs == 8192
        d = _DeviceOutput("Cans", SR, 8192)
        d.open()
        assert fake_sd.closed == [("Cans", 8192)]      # no leaked stream
        assert fake_sd.started == [("Cans", 4096)]
        assert d.opened_block == 4096

    def test_portaudio_block_is_the_last_resort(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs > 0
        d = _DeviceOutput("Cans", SR, 8192)
        d.open()
        assert [bs for _, bs in fake_sd.attempts] == _open_ladder(8192)
        assert d.opened_block == 0 and d._block_size == 0
        assert d._capacity == 8 * 8192                # already >= the floor

    def test_portaudio_block_grows_a_tiny_ring(self, fake_sd):
        # 8 x 64 = 512 samples could be smaller than one host callback.
        fake_sd.refuse = lambda dev, bs: bs > 0
        d = _DeviceOutput("Cans", SR, 64)
        d.open()
        assert d.opened_block == 0
        assert d._capacity == _AUTO_RING_MIN
        assert d._ring.shape == (_AUTO_RING_MIN, 2)
        assert d.telemetry() == (0, _AUTO_RING_MIN, 0, 0)

    def test_device_lookup_error_is_not_retried(self, fake_sd):
        # sounddevice raises ValueError when the name matches no device:
        # no block size fixes that, so one attempt, and it propagates.
        fake_sd.lookup_error = True
        d = _DeviceOutput("Gone", SR, 8192)
        with pytest.raises(ValueError):
            d.open()
        assert fake_sd.attempts == [("Gone", 8192)]
        assert d._stream is None and d._block_size == 8192

    def test_nothing_opens_raises_after_the_whole_ladder(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: True
        d = _DeviceOutput("Cans", SR, 8192)
        with pytest.raises(_Refused, match="blocksize 0 refused"):
            d.open()
        assert [bs for _, bs in fake_sd.attempts] == _open_ladder(8192)
        assert fake_sd.started == [] and d._stream is None
        assert d._block_size == 8192


class TestAutoBlockPriming:
    """A blocksize-0 stream's callback size is the host's choice, so the ring
    can't arm on 'fill reached one device block'; it arms on the first
    callback served whole, and starvation after that counts."""

    def _auto(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs > 0
        d = _DeviceOutput("Cans", SR, 8192)
        d.open()
        return d

    @staticmethod
    def _out(n):
        return np.zeros((n, 2), dtype=np.float32)

    def test_startup_callbacks_are_not_underruns(self, fake_sd):
        d = self._auto(fake_sd)
        d._callback(self._out(441), 441, None, None)   # PortAudio priming
        d.push(np.full((100, 2), 0.1, dtype=np.float32))
        d._callback(self._out(441), 441, None, None)   # part-served fill-up
        assert d.telemetry()[2] == 0

    def test_starvation_after_a_whole_callback_counts(self, fake_sd):
        d = self._auto(fake_sd)
        d.push(np.full((F, 2), 0.1, dtype=np.float32))
        assert d._primed is False                      # a push alone never arms it
        o = self._out(441)
        d._callback(o, 441, None, None)                # served whole: armed
        assert np.allclose(o, 0.1) and d.telemetry()[2] == 0
        d._callback(self._out(441), 441, None, None)   # 71 left: starved
        assert d.telemetry()[2] == 1

    def test_fixed_block_priming_is_unchanged(self, fake_sd):
        # The pre-existing rule still arms a fixed-block ring on fill.
        d = _DeviceOutput("Cans", SR, F)
        d.open()
        d.push(np.zeros((F, 2), dtype=np.float32))
        assert d._primed is True


# ----- the backend: routing, governor seed, snapshots --------------------------


class TestBackendFallback:
    def test_refused_size_still_plays_under_its_key(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs == 8192
        p, s = _sink_patch(buffer_size=8192)
        b = _running(p)
        # The stream keeps its (device, asked) key, so routing never notices.
        assert set(b._device_outputs) == {("Cans", 8192)}
        assert b._device_outputs[("Cans", 8192)].opened_block == 4096
        assert b._device_open_failures == {}
        assert b.snapshot_sink_streams() == {s.id: (8192, 4096, "")}
        # ...and it is FED: one main callback lands a block in its ring.
        b._fill_output(np.zeros((F, 2), dtype=np.float32), F)
        assert b.snapshot_sink_buffers() == {s.id: (F, 8 * 8192, 0, 0)}

    def test_governor_reads_the_fallback_ring(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs == 8192
        p, s = _sink_patch(buffer_size=8192)
        b = _running(p)
        assert b._sink_fill(p.get(s.id)) == 0.0        # live (empty), not 0.5
        b._fill_output(np.zeros((F, 2), dtype=np.float32), F)
        assert b._sink_fill(p.get(s.id)) == F / (8 * 8192)

    def test_warping_sibling_falls_back_too(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs > 1024
        p, s = _sink_patch(WARPING, buffer_size=4096)
        b = _running(p)
        assert b.snapshot_sink_streams() == {s.id: (4096, 1024, "")}

    def test_plain_specific_sink_gets_the_ladder(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs == F
        p, s = _sink_patch(SPECIFIC)
        b = _running(p)
        assert set(b._device_outputs) == {("Cans", F)}
        assert b.snapshot_sink_streams() == {s.id: (F, 256, "")}

    def test_a_stream_that_opens_as_asked_reports_no_fallback(self, fake_sd):
        p, s = _sink_patch(buffer_size=1024)
        b = _running(p)
        assert fake_sd.attempts == [("Cans", 1024)]
        assert b.snapshot_sink_streams() == {s.id: (1024, 1024, "")}

    def test_nothing_opens_is_reported_not_idle(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: True
        p, s = _sink_patch(buffer_size=8192)
        b = _running(p)
        assert b._device_outputs == {}                  # the sink is silent...
        assert b.snapshot_sink_buffers() == {}          # ...has no ring...
        asked, opened, error = b.snapshot_sink_streams()[s.id]
        assert (asked, opened) == (8192, None)          # ...and SAYS so
        assert "refused" in error
        assert b._sink_fill(p.get(s.id)) == 0.5         # governor stays neutral

    def test_unknown_device_is_reported_with_its_reason(self, fake_sd):
        fake_sd.lookup_error = True
        p, s = _sink_patch(device="Gone", buffer_size=1024)
        b = _running(p)
        assert len(fake_sd.attempts) == 1
        assert b.snapshot_sink_streams() == {
            s.id: (1024, None, "No output device matching 'Gone'")
        }

    def test_failure_clears_when_the_sink_moves_to_a_device_that_opens(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: dev == "Dead"
        p, s = _sink_patch(device="Dead", buffer_size=2048)
        b = _running(p)
        assert b.snapshot_sink_streams()[s.id][1] is None
        b.set_param(s.id, "device", "Cans")
        assert b._device_open_failures == {}
        assert b.snapshot_sink_streams() == {s.id: (2048, 2048, "")}

    def test_failure_clears_when_the_sink_goes_back_to_master(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: True
        p, s = _sink_patch(buffer_size=2048)
        b = _running(p)
        b.set_param(s.id, "device", "")
        assert b._device_open_failures == {}
        assert b.snapshot_sink_streams() == {}

    def test_stop_clears_failures_and_fallbacks(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: dev == "Dead" or bs == 8192
        p = Patch()
        o = p.add_module("oscillator", params={"amp": 0.4})
        s1 = p.add_module(BUFFERED, params={"device": "Cans", "buffer_size": 8192})
        s2 = p.add_module(BUFFERED, params={"device": "Dead", "buffer_size": 512})
        p.connect(o.id, "out", s1.id, "in_l")
        p.connect(o.id, "out", s2.id, "in_l")
        b = _running(p)
        snap = b.snapshot_sink_streams()
        assert snap[s1.id] == (8192, 4096, "") and snap[s2.id][1] is None
        b._stream = mock.MagicMock()                   # the main stream's stand-in
        b.stop()
        assert b.snapshot_sink_streams() == {}
        assert b._device_open_failures == {} and b._device_outputs == {}

    def test_no_patch_or_nothing_routed_is_empty(self, fake_sd):
        assert NumpyBackend(sample_rate=SR, block_size=F).snapshot_sink_streams() == {}
        p, _s = _sink_patch(device="")
        assert _running(p).snapshot_sink_streams() == {}


# ----- the readout text ---------------------------------------------------------


class TestReadoutText:
    ENTRY = (30802, 65536, 0, 2)
    LINE = "buffer 47% (30802/65536)  under 0  drop 2"

    def test_as_asked_reads_exactly_as_before(self):
        assert format_sink_buffer(self.ENTRY, (8192, 8192, "")) == self.LINE
        assert format_sink_buffer(self.ENTRY) == self.LINE

    def test_a_smaller_device_block_gets_a_second_line(self):
        assert format_sink_buffer(self.ENTRY, (8192, 2048, "")) == (
            self.LINE + "\ndevice block 2048 - 8192 would not open"
        )

    def test_portaudio_block_reads_auto(self):
        assert format_sink_buffer(self.ENTRY, (8192, 0, "")).endswith(
            "\ndevice block auto - 8192 would not open"
        )

    def test_failed_open_is_not_idle(self):
        text = format_sink_buffer(None, (8192, None, "refused"))
        assert text == "buffer: could not open the device"
        assert text != format_sink_buffer(None)

    def test_ascii_only(self):
        for stream in ((8192, 2048, ""), (8192, 0, ""), (8192, None, "x")):
            format_sink_buffer(self.ENTRY, stream).encode("ascii")


# ----- the UI: readout colour + text, the status bar ---------------------------


def _ui_app(monkeypatch, backend):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = backend
    app.patch = backend._patch
    return app, app_mod


class TestUI:
    def test_status_bar_names_a_fallback_once_per_start(self, fake_sd, monkeypatch):
        fake_sd.refuse = lambda dev, bs: bs == 8192
        p, s = _sink_patch(buffer_size=8192)
        b = _running(p)
        app, _ = _ui_app(monkeypatch, b)
        said = []
        app._set_status = said.append
        app._report_sink_streams()
        assert said == [
            f"{s.name or s.TYPE}: 'Cans' would not open with 8192-sample "
            "blocks - playing on 4096-sample blocks instead (the ring is "
            "still sized for 8192)"
        ]
        app._report_sink_streams()
        assert len(said) == 1                           # once, not every frame
        b._stream = mock.MagicMock()
        b.stop()
        app._report_sink_streams()                      # stream gone: forgotten
        b._running = True
        b._sync_device_outputs()                        # Start again
        app._report_sink_streams()
        assert len(said) == 2 and said[1] == said[0]

    def test_status_bar_names_portaudio_block(self, fake_sd, monkeypatch):
        fake_sd.refuse = lambda dev, bs: bs > 0
        p, s = _sink_patch(buffer_size=8192)
        app, _ = _ui_app(monkeypatch, _running(p))
        said = []
        app._set_status = said.append
        app._report_sink_streams()
        assert len(said) == 1 and "PortAudio's own block size" in said[0]

    def test_status_bar_names_a_failed_open_with_its_reason(self, fake_sd, monkeypatch):
        fake_sd.refuse = lambda dev, bs: True
        p, s = _sink_patch(buffer_size=8192)
        app, _ = _ui_app(monkeypatch, _running(p))
        said = []
        app._set_status = said.append
        app._report_sink_streams()
        assert len(said) == 1
        assert said[0].startswith(
            f"{s.name or s.TYPE}: could not open 'Cans' at any buffer size ("
        )
        assert "blocksize 0 refused" in said[0]
        assert said[0].endswith(") - this sink is silent")

    def test_status_bar_is_quiet_when_streams_open_as_asked(self, fake_sd, monkeypatch):
        p, _s = _sink_patch(buffer_size=1024)
        app, _ = _ui_app(monkeypatch, _running(p))
        said = []
        app._set_status = said.append
        app._report_sink_streams()
        assert said == []

    def test_status_bar_covers_the_plain_specific_sink(self, fake_sd, monkeypatch):
        # No readout on that node, so the status bar is its only voice.
        fake_sd.refuse = lambda dev, bs: True
        p, _s = _sink_patch(SPECIFIC)
        app, _ = _ui_app(monkeypatch, _running(p))
        said = []
        app._set_status = said.append
        app._report_sink_streams()
        assert len(said) == 1 and "this sink is silent" in said[0]

    def _readout(self, app, app_mod, mid):
        app._sink_buffer_labels[mid] = 4242
        app_mod.dpg.reset_mock()
        app._update_sink_buffers()
        texts = [c.args[1] for c in app_mod.dpg.set_value.call_args_list
                 if c.args and c.args[0] == 4242]
        colors = [c.kwargs.get("color") for c in app_mod.dpg.configure_item.call_args_list
                  if c.args and c.args[0] == 4242]
        return texts[-1], colors[-1]

    def test_readout_says_could_not_open_in_red(self, fake_sd, monkeypatch):
        fake_sd.refuse = lambda dev, bs: True
        p, s = _sink_patch(buffer_size=8192)
        app, app_mod = _ui_app(monkeypatch, _running(p))
        text, color = self._readout(app, app_mod, s.id)
        assert text == "buffer: could not open the device"
        assert color == app._SINK_BUF_FAILED_RGBA != app._SINK_BUF_IDLE_RGBA

    def test_readout_names_the_fallback_block(self, fake_sd, monkeypatch):
        fake_sd.refuse = lambda dev, bs: bs == 8192
        p, s = _sink_patch(buffer_size=8192)
        app, app_mod = _ui_app(monkeypatch, _running(p))
        text, color = self._readout(app, app_mod, s.id)
        assert text == (
            "buffer 0% (0/65536)  under 0  drop 0\n"
            "device block 4096 - 8192 would not open"
        )
        assert color == app._SINK_BUF_OK_RGBA

    def test_readout_still_idles_when_stopped(self, fake_sd, monkeypatch):
        p, s = _sink_patch(buffer_size=8192)
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(p)                                   # never started
        app, app_mod = _ui_app(monkeypatch, b)
        text, color = self._readout(app, app_mod, s.id)
        assert text == "buffer: idle" and color == app._SINK_BUF_IDLE_RGBA


def test_no_test_here_can_reach_real_sounddevice(fake_sd):
    # Belt and braces: the fixture's fake is what the backend sees.
    assert nb.sd is fake_sd
