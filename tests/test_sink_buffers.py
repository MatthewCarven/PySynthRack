"""`buffers`: how many buffer_size blocks a buffered sink's ring holds
(2026-11-21, Matthew: "Can we also offer up the number of buffers as a
slider on the buffered speaker output please Claude?").

2 .. 32, default 8 -- the old fixed count, so a patch without the key, or
with 8, keeps its old stream key and ring exactly. The ring starts half
full, so the cushion (and the latency) is buffers / 2 x buffer_size.
"""
from __future__ import annotations

from unittest import mock

import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from tests.test_sink_open_fallback import BUFFERED, WARPING, FakeSD, _running, _sink_patch

SR, F = 44100, 512


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(nb, "sd", sd)
    return sd


def _only(b):
    assert len(b._device_outputs) == 1
    return next(iter(b._device_outputs.items()))


class TestTheParam:
    @pytest.mark.parametrize("sink", [BUFFERED, WARPING])
    def test_the_default_is_eight(self, sink):
        assert Patch().add_module(sink).params["buffers"] == 8

    @pytest.mark.parametrize("raw, want", [
        (8, 8), (2, 2), (32, 32), (1, 2), (0, 2), (100, 32), ("12", 12),
        (12.7, 12), ("lots", 8), (None, 8),
    ])
    def test_clamped_and_junk_is_eight(self, raw, want):
        p, s = _sink_patch(buffer_size=1024, buffers=raw)
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(p)
        assert b._sink_buffers(s) == want


class TestTheRing:
    def test_eight_keeps_the_old_key_and_ring(self, fake_sd):
        p, s = _sink_patch(buffer_size=2048, buffers=8)
        key, d = _only(_running(p))
        assert key == ("Cans", 2048)
        assert d.telemetry()[1] == 8 * 2048

    def test_another_count_sizes_the_ring_and_keys_its_own_stream(self, fake_sd):
        p, s = _sink_patch(buffer_size=2048, buffers=4)
        key, d = _only(_running(p))
        assert key == ("Cans", 2048, 2048, 4)
        q, cap, _u, _d = d.telemetry()
        assert (q, cap) == (2 * 2048, 4 * 2048)    # primed half: 2 blocks
        assert fake_sd.attempts[0] == ("Cans", 2048)   # the device block is unchanged

    def test_it_combines_with_a_device_block(self, fake_sd):
        p, s = _sink_patch(buffer_size=4096, device_block=512, buffers=16)
        key, d = _only(_running(p))
        assert key == ("Cans", 4096, 512, 16)
        assert d.telemetry()[1] == 16 * 4096
        assert fake_sd.attempts[0] == ("Cans", 512)

    def test_two_buffers_still_hold_two_device_blocks(self, fake_sd):
        p, s = _sink_patch(buffer_size=256, device_block=2048, buffers=2)
        _key, d = _only(_running(p))
        assert d.telemetry()[1] == 2 * 2048          # the floor, not 2 x 256

    def test_a_live_change_rebuilds_that_stream_primed(self, fake_sd):
        p, s = _sink_patch(buffer_size=2048)
        b = _running(p)
        b.set_param(s.id, "buffers", 12)
        key, d = _only(b)
        assert key == ("Cans", 2048, 2048, 12)
        assert d.telemetry()[:2] == (6 * 2048, 12 * 2048)
        b.set_param(s.id, "buffers", 8)
        assert set(b._device_outputs) == {("Cans", 2048)}

    def test_it_survives_a_round_trip(self, tmp_path):
        from pysynthrack.io_patch import load_patch, save_patch

        p, s = _sink_patch(buffer_size=2048, buffers=5)
        path = tmp_path / "p.json"
        save_patch(p, path)
        assert load_patch(path).modules[s.id].params["buffers"] == 5


class TestTheSlider:
    def test_the_slider_stores_a_clean_int_and_rebuilds(self, fake_sd, monkeypatch):
        pytest.importorskip("dearpygui.dearpygui")
        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
        p, s = _sink_patch(buffer_size=2048)
        b = _running(p)
        app = app_mod.App()
        app.backend, app.patch = b, b._patch
        app._on_sink_buffers_changed(None, 40, (s.id, "buffers"))
        assert s.params["buffers"] == 32
        assert ("Cans", 2048, 2048, 32) in b._device_outputs
