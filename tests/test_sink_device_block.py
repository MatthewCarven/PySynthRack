"""The buffered sink's ``device_block`` (decision #2, 2026-11-20).

Matthew: "b add a separate device blocksize if you can easily also offer up
as status the current buffer size and length in time so you can use it to do
a little prediction". ``buffer_size`` keeps sizing the ring (the cushion);
``device_block`` is the PortAudio callback block asked of the device --
"auto" (the default) follows ``buffer_size``, exactly as before. The node's
readout gains a time line: queued and ring in ms, and the device block
running, in samples and ms.

No real audio stream is opened: ``numpy_backend.sd`` is the fake from
test_sink_open_fallback.
"""
from __future__ import annotations

import json
from unittest import mock

import numpy as np
import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend, _DeviceOutput
from pysynthrack.core import Patch
from pysynthrack.ui.buffer import (
    DEVICE_BLOCK_CHOICES,
    SINK_BUFFER_SIZES,
    coerce_device_block,
    format_sink_buffer,
)
from tests.test_sink_open_fallback import BUFFERED, SPECIFIC, WARPING, FakeSD, _running, _sink_patch

SR, F = 44100, 512


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(nb, "sd", sd)
    return sd


def _only_stream(b):
    assert len(b._device_outputs) == 1
    return next(iter(b._device_outputs.items()))


# ----- auto is today's sink ------------------------------------------------------


class TestAuto:
    @pytest.mark.parametrize("sink", [BUFFERED, WARPING])
    def test_the_default_is_auto(self, sink):
        assert Patch().add_module(sink).params["device_block"] == "auto"

    @pytest.mark.parametrize("value", ["auto", None, "", "AUTO", 4096, "4096"])
    def test_auto_and_the_same_number_keep_the_old_key_and_stream(self, fake_sd, value):
        p, s = _sink_patch(buffer_size=4096, device_block=value)
        b = _running(p)
        key, d = _only_stream(b)
        assert key == ("Cans", 4096)                 # the two-element key, unchanged
        assert fake_sd.attempts[0] == ("Cans", 4096)
        assert d.requested_block == d.opened_block == d.ring_block == 4096
        assert d.telemetry()[1] == 8 * 4096          # the ring, as before
        assert b.snapshot_sink_streams()[s.id] == (4096, 4096, "")

    def test_an_old_patch_without_the_key_loads_as_auto(self, tmp_path):
        from pysynthrack.io_patch import load_patch, save_patch

        p, s = _sink_patch(buffer_size=2048)
        path = tmp_path / "p.json"
        save_patch(p, path)
        data = json.loads(path.read_text())
        for m in data["modules"]:
            m.get("params", {}).pop("device_block", None)
        path.write_text(json.dumps(data))
        q = load_patch(path)
        assert q.modules[s.id].params["device_block"] == "auto"

    def test_a_set_value_survives_a_round_trip(self, tmp_path):
        from pysynthrack.io_patch import load_patch, save_patch

        p, s = _sink_patch(buffer_size=4096, device_block=512)
        path = tmp_path / "p.json"
        save_patch(p, path)
        assert load_patch(path).modules[s.id].params["device_block"] == 512


# ----- a device block of its own -------------------------------------------------


class TestDeviceBlock:
    def test_a_big_cushion_on_a_modest_device_chunk(self, fake_sd):
        # buffer 4096 (the ring: 8 x 4096 = 743 ms of room), device 512.
        p, s = _sink_patch(buffer_size=4096, device_block=512)
        b = _running(p)
        key, d = _only_stream(b)
        assert key == ("Cans", 4096, 512)
        assert fake_sd.attempts == [("Cans", 512)]   # the device was asked for 512
        assert d.ring_block == 4096 and d.opened_block == 512
        assert d.telemetry()[1] == 8 * 4096
        assert b.snapshot_sink_streams()[s.id] == (512, 512, "")

    def test_a_big_device_block_on_a_small_cushion_still_serves(self, fake_sd):
        # buffer 64 would make a 512-sample ring; a device block of 8192
        # needs at least two of those, so the ring grows to 16384.
        p, s = _sink_patch(buffer_size=64, device_block=8192)
        b = _running(p)
        _key, d = _only_stream(b)
        assert d.telemetry()[1] == 2 * 8192
        for _ in range(8192 // F):
            d.push(np.full((F, 2), 0.25, np.float32))
        out = np.zeros((8192, 2), np.float32)
        d._callback(out, 8192, None, None)
        assert np.all(out == 0.0)                    # the primed half: silence
        d._callback(out, 8192, None, None)
        assert np.all(out == 0.25)                   # then one whole device block
        assert d.telemetry()[2] == 0                 # and no underrun counted

    def test_the_fallback_ladder_starts_at_the_device_block(self, fake_sd):
        fake_sd.refuse = lambda dev, bs: bs == 2048
        p, s = _sink_patch(buffer_size=4096, device_block=2048)
        b = _running(p)
        _key, d = _only_stream(b)
        assert [bs for _dev, bs in fake_sd.attempts] == [2048, 1024]
        assert d.opened_block == 1024
        assert d.telemetry()[1] == 8 * 4096          # the cushion survives
        assert b.snapshot_sink_streams()[s.id] == (2048, 1024, "")

    @pytest.mark.parametrize("raw, want", [
        ("lots", None), (float("nan"), None), (-5, 16), (99999, 8192), (512.0, 512),
    ])
    def test_junk_is_auto_and_numbers_are_clamped(self, raw, want):
        p, s = _sink_patch(buffer_size=1024, device_block=raw)
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(p)
        assert b._sink_device_block(s) == (1024 if want is None else want)

    def test_two_sinks_one_device_two_device_blocks_are_two_streams(self, fake_sd):
        p, s = _sink_patch(buffer_size=2048, device_block=256)
        o = next(m for m in p.modules.values() if m.TYPE == "oscillator")
        s2 = p.add_module(BUFFERED, params={"device": "Cans", "buffer_size": 2048})
        p.connect(o.id, "out", s2.id, "in_r")
        b = _running(p)
        assert set(b._device_outputs) == {("Cans", 2048, 256), ("Cans", 2048)}

    def test_a_live_change_rebuilds_only_that_stream(self, fake_sd):
        p, s = _sink_patch(buffer_size=4096)
        b = _running(p)
        (old_key, old) = _only_stream(b)
        old.close = mock.MagicMock(wraps=old.close)
        b.set_param(s.id, "device_block", 1024)
        new_key, new = _only_stream(b)
        assert (old_key, new_key) == (("Cans", 4096), ("Cans", 4096, 1024))
        assert new is not old and old.close.called
        assert fake_sd.attempts[-1] == ("Cans", 1024)
        b.set_param(s.id, "device_block", "auto")
        assert set(b._device_outputs) == {("Cans", 4096)}

    def test_the_plain_specific_sink_has_no_such_param(self, fake_sd):
        # It runs at the global block, ring and device alike; the param is
        # the buffered family's only (the plain sink refuses it outright).
        with pytest.raises(KeyError):
            _sink_patch(sink_type=SPECIFIC, device_block=2048)
        p, s = _sink_patch(sink_type=SPECIFIC)
        b = _running(p)
        key, d = _only_stream(b)
        assert key == ("Cans", F) and d.opened_block == F
        assert b._sink_device_block(s) == F

    def test_the_device_output_alone(self):
        # The constructor: device_block None is the old object exactly.
        a = _DeviceOutput("X", SR, 1024)
        assert (a.ring_block, a.requested_block, a.telemetry()[1]) == (1024, 1024, 8192)
        c = _DeviceOutput("X", SR, 1024, device_block=256)
        assert (c.ring_block, c.requested_block, c.telemetry()[1]) == (1024, 256, 8192)


# ----- the readout ---------------------------------------------------------------


class TestReadout:
    def test_the_time_line(self):
        text = format_sink_buffer((3686, 8192, 0, 0), (512, 512, ""), 44100)
        assert text == (
            "buffer 45% (3686/8192)  under 0  drop 0\n"
            "queued 83.6 of 185.8 ms  dev block 512 = 11.6 ms"
        )

    def test_without_a_rate_it_is_the_old_line(self):
        assert format_sink_buffer((3686, 8192, 0, 0), (512, 512, "")) == (
            "buffer 45% (3686/8192)  under 0  drop 0"
        )

    def test_portaudio_picking_reads_auto(self):
        text = format_sink_buffer((0, 8192, 0, 0), (1024, 0, ""), 44100)
        assert "dev block auto" in text
        assert text.endswith("device block auto - 1024 would not open")

    def test_idle_and_failed_are_unchanged(self):
        assert format_sink_buffer(None, None, 44100) == "buffer: idle"
        assert format_sink_buffer(None, (512, None, "x"), 44100) == (
            "buffer: could not open the device"
        )


class TestCoerce:
    def test_choices(self):
        assert DEVICE_BLOCK_CHOICES == ("auto", *(str(s) for s in SINK_BUFFER_SIZES))

    @pytest.mark.parametrize("raw, want", [
        ("auto", "auto"), (None, "auto"), ("", "auto"), ("junk", "auto"),
        ("512", 512), (500, 512), (8192, 8192), (10 ** 6, 8192),
    ])
    def test_coerce(self, raw, want):
        assert coerce_device_block(raw) == want


# ----- UI ------------------------------------------------------------------------


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
    def test_the_combo_stores_auto_or_a_clean_int(self, fake_sd, monkeypatch):
        p, s = _sink_patch(buffer_size=4096)
        b = _running(p)
        app, _ = _ui_app(monkeypatch, b)
        app._on_device_block_changed(None, "1024", (s.id, "device_block"))
        assert s.params["device_block"] == 1024
        assert ("Cans", 4096, 1024) in b._device_outputs
        app._on_device_block_changed(None, "auto", (s.id, "device_block"))
        assert s.params["device_block"] == "auto"
        assert set(b._device_outputs) == {("Cans", 4096)}

    def test_the_status_bar_names_the_ring_not_the_device_block(self, fake_sd, monkeypatch):
        fake_sd.refuse = lambda dev, bs: bs == 2048
        p, s = _sink_patch(buffer_size=4096, device_block=2048)
        app, _ = _ui_app(monkeypatch, _running(p))
        said = []
        app._set_status = said.append
        app._report_sink_streams()
        assert said == [
            f"{s.name or s.TYPE}: 'Cans' would not open with 2048-sample "
            "blocks - playing on 1024-sample blocks instead (the ring is "
            "still sized for 4096)"
        ]
