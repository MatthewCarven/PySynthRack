"""The governor reads the ring's fill averaged over one device period.

Matthew, 2026-11-20, on a buffered sink at buffer_size 2048 (device_block
auto, so the device pulls 2048 at a time) under 512-sample render blocks,
auto_govern on: "it seems to do skip"; unticking auto_govern "made it more
steady". The raw fill read once per render block is a sawtooth -- up 512 a
block, down 2048 at each device pull, 12.5% of the 16384 ring -- and the
governor chased it, flipping its stretch ratio either side of 1 every 46 ms.
Reproduced here before the fix: 46 of 320 square-wave edges lost or smeared
in what the device received; 0 with the governor off.

No real audio stream: ``numpy_backend.sd`` is the fake from
test_sink_open_fallback, and the device's callback is driven by hand on its
own (slightly fast) clock.
"""
from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.core import Patch
from tests.test_sink_open_fallback import BUFFERED, FakeSD

SR, F = 44100, 512


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(nb, "sd", sd)
    return sd


def _rig(buffer_size, device_block="auto", govern=False, wave="sine", freq=440.0):
    p = Patch()
    o = p.add_module("oscillator", params={"waveform": wave, "freq": freq, "amp": 0.5})
    s = p.add_module(BUFFERED, params={
        "device": "X", "buffer_size": buffer_size, "device_block": device_block,
        "auto_govern": govern})
    p.connect(o.id, "out", s.id, "in_l")
    p.connect(o.id, "out", s.id, "in_r")
    # A probe on the `fill` out: what a patched governor would receive.
    probe = p.add_module("cv_offset", params={"offset": 0.0})
    p.connect(s.id, "fill", probe.id, "in")
    b = nb.NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(p)
    b._running = True
    b._sync_device_outputs()
    seen = {}
    orig = b._render_module

    def spy(module, frames, buffers, patch):
        if module.id == probe.id:
            seen["fill"] = float(np.asarray(buffers[(s.id, "fill")])[0])
        return orig(module, frames, buffers, patch)

    b._render_module = spy
    b._probe_seen = seen
    (_key, dev), = b._device_outputs.items()
    return b, s, dev


def _play(b, s, dev, seconds, drift_ppm=150.0, pull=None):
    """Render and pull in time order on two clocks; return what the device
    received, the fill out and the raw fill per render block, and the ratio.
    The device clock starts half a render block out of phase, so no pull
    ever ties with a render on the timeline (a float tie would decide the
    order by rounding)."""
    pull = pull or dev.opened_block
    per = pull / (SR * (1 + drift_ppm * 1e-6))
    got, fills, raws, ratios = [], [], [], []
    t_main, t_dev = 0.0, 0.5 * F / SR
    while t_main < seconds:
        if t_dev <= t_main:
            buf = np.zeros((pull, 2), np.float32)
            dev._callback(buf, pull, None, None)
            got.append(buf)
            t_dev += per
        else:
            raws.append(b._sink_fill(s))
            b._fill_output(np.zeros((F, 2), np.float32), F)
            fills.append(b._probe_seen["fill"])
            ratios.append(b._sink_ratio.get(s.id, 1.0))
            t_main += F / SR
    return np.concatenate(got)[:, 0], np.array(fills), np.array(raws), np.array(ratios)


def _edges(y, skip_s):
    y = y[int(skip_s * SR):].astype(np.float64)
    idx = np.flatnonzero(np.abs(np.diff(y)) > 0.5)
    ev = idx[np.concatenate([[True], np.diff(idx) > 50])] if idx.size else idx
    return np.diff(ev)


class TestTheAverage:
    def test_the_fill_out_is_flat_where_the_raw_ring_is_a_sawtooth(self, fake_sd):
        b, s, dev = _rig(2048)
        _y, fills, raws, _r = _play(b, s, dev, 4.0, drift_ppm=0.0)
        f, r = fills[40:], raws[40:]
        assert np.ptp(r) >= 0.09                      # read per block: 0..1536 of 16384
        assert np.ptp(f) < 1e-6                       # the average: flat

    def test_a_device_block_no_bigger_than_the_render_block_is_the_raw_fill(self, fake_sd):
        # Window of one reading: the fill out is the raw ring, bit for bit,
        # exactly as before.
        b, s, dev = _rig(512)
        _y, fills, raws, _r = _play(b, s, dev, 2.0)
        assert np.array_equal(fills.astype(np.float32), raws.astype(np.float32))

    def test_the_window_follows_portaudios_own_block_size(self, fake_sd):
        # A stream that only opened at blocksize 0 learns the period from
        # its callbacks.
        fake_sd.refuse = lambda dev, bs: bs != 0
        b, s, dev = _rig(2048)
        assert dev.opened_block == 0
        _play(b, s, dev, 1.0, pull=1024)
        (key, _d), = b._device_outputs.items()
        assert b._sink_fill_hist[key].maxlen == 2     # 1024 / 512

    def test_stop_forgets_the_history(self, fake_sd):
        b, s, dev = _rig(2048)
        _play(b, s, dev, 0.5)
        assert b._sink_fill_hist and b._sink_fill_avg
        b._stream = mock.MagicMock()                  # the main stream stop() closes
        b.stop()
        assert not b._sink_fill_hist and not b._sink_fill_avg


class TestTheGovernorNoLongerSkips:
    def test_the_ratio_holds_still(self, fake_sd):
        b, s, dev = _rig(2048, govern=True)
        _y, _f, _raw, ratios = _play(b, s, dev, 12.0)
        settled = ratios[int(6 * SR / F):]
        assert np.ptp(settled) < 1e-4                 # was ~7e-3, flipping each 46 ms

    def test_every_square_edge_arrives_once_and_on_time(self, fake_sd):
        # The listening symptom, measured: a 10 Hz naive square through the
        # governed sink, edges counted in what the device received.
        b, s, dev = _rig(2048, govern=True, wave="square", freq=10.0)
        y, _f, _raw, _r = _play(b, s, dev, 12.0)
        gaps = _edges(y, skip_s=6.0)
        assert len(gaps) >= 115                       # ~6 s of edges, none lost
        assert np.all(np.abs(gaps - SR / 20) <= 2)    # each on time, to 2 samples

    def test_ungoverned_is_untouched(self, fake_sd):
        b, s, dev = _rig(2048, wave="square", freq=10.0)
        y, _f, _raw, ratios = _play(b, s, dev, 8.0)
        assert np.all(ratios == 1.0)
        assert np.all(_edges(y, skip_s=2.0) == SR / 20)
