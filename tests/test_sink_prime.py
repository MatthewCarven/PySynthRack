"""A buffered sink's stream starts with its ring HALF FULL of silence
(2026-11-21).

Matthew's screenshot, buffer_size 8192 / device_block 2048 / auto_govern off:
``2048/65536``. The ring started empty and the device started pulling at
once, so it never held more than about one device block -- a big
buffer_size was a ceiling, not a cushion -- and a governed sink spent its
first seconds (after Start or any live change) stretching hard to fill it.
Primed, the cushion is real from the first callback and the governor starts
at its setpoint. The plain specific sink keeps its old, unprimed ring.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.audio.numpy_backend as nb
import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.core import Patch
from tests.test_sink_open_fallback import BUFFERED, SPECIFIC, FakeSD

SR, F = 44100, 512


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(nb, "sd", sd)
    return sd


def _rig(sink=BUFFERED, wave="square", freq=10.0, **params):
    p = Patch()
    o = p.add_module("oscillator", params={"waveform": wave, "freq": freq, "amp": 0.5})
    s = p.add_module(sink, params={"device": "X", **params})
    p.connect(o.id, "out", s.id, "in_l")
    p.connect(o.id, "out", s.id, "in_r")
    b = nb.NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(p)
    b._running = True
    b._sync_device_outputs()
    (_key, dev), = b._device_outputs.items()
    return b, s, dev


def _play(b, s, dev, seconds, jitter_s=0.0, seed=0):
    """Render and pull on two clocks (the device 150 ppm fast, its callbacks
    optionally jittered); return what the device received and the ratios."""
    rng = np.random.default_rng(seed)
    pull = dev.opened_block
    per = pull / (SR * (1 + 150e-6))
    got, ratios = [], []
    t_main, t_dev_nominal = 0.0, 0.5 * F / SR
    t_dev = t_dev_nominal
    while t_main < seconds:
        if t_dev <= t_main:
            buf = np.zeros((pull, 2), np.float32)
            dev._callback(buf, pull, None, None)
            got.append(buf)
            t_dev_nominal += per
            t_dev = t_dev_nominal + (rng.uniform(0, jitter_s) if jitter_s else 0.0)
        else:
            b._fill_output(np.zeros((F, 2), np.float32), F)
            ratios.append(b._sink_ratio.get(s.id, 1.0))
            t_main += F / SR
    return np.concatenate(got)[:, 0].astype(np.float64), np.array(ratios)


def _edge_gaps(y, skip_s):
    y = y[int(skip_s * SR):]
    idx = np.flatnonzero(np.abs(np.diff(y)) > 0.5)
    ev = idx[np.concatenate([[True], np.diff(idx) > 50])] if idx.size else idx
    return np.diff(ev)


class TestPrimed:
    def test_a_buffered_stream_opens_half_full_of_silence(self, fake_sd):
        _b, _s, dev = _rig(buffer_size=2048)
        queued, cap, under, _d = dev.telemetry()
        assert (queued, cap, under) == (cap // 2, 8 * 2048, 0)
        out = np.ones((2048, 2), np.float32)
        dev._callback(out, 2048, None, None)
        assert not out.any() and dev.telemetry()[2] == 0   # silence, no underrun

    def test_the_plain_specific_sink_is_not_primed(self, fake_sd):
        _b, _s, dev = _rig(sink=SPECIFIC)
        assert dev.telemetry()[0] == 0

    def test_a_live_change_primes_the_new_stream_too(self, fake_sd):
        b, s, _dev = _rig(buffer_size=2048)
        b.set_param(s.id, "device_block", 512)
        (_k, dev), = b._device_outputs.items()
        assert dev.telemetry()[0] == dev.telemetry()[1] // 2


class TestTheCushionIsReal:
    def test_ungoverned_it_holds_about_half_and_rides_out_jitter(self, fake_sd):
        # The screenshot's settings; device callbacks up to 30 ms late.
        b, s, dev = _rig(buffer_size=8192, device_block=2048)
        _y, _r = _play(b, s, dev, 10.0, jitter_s=0.030)
        queued, cap, under, _d = dev.telemetry()
        assert 0.4 < queued / cap < 0.6                # was ~one device block
        assert under == 0

    def test_governed_it_starts_at_the_setpoint_and_never_stretches_hard(self, fake_sd):
        # 4096 / 512: unprimed, the ratio climbed to ~1.19 for the first
        # seconds and ~15 square edges were spliced; primed, the ratio stays
        # by 1 and every edge after the cushion's silence is on time.
        b, s, dev = _rig(buffer_size=4096, device_block=512, auto_govern=True)
        y, ratios = _play(b, s, dev, 8.0)
        assert ratios.max() < 1.01
        gaps = _edge_gaps(y, skip_s=0.6)               # past the 372 ms of silence
        assert len(gaps) >= 140 and np.all(np.abs(gaps - SR / 20) <= 2)
