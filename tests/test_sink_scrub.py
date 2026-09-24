"""The output stage scrubs NaN / inf before the clip.

``np.clip`` passes NaN straight through (and turns inf into a full-scale
sample), so before this fix one non-finite sample anywhere upstream went
to the audio device as garbage. The sink now zeroes just the bad samples
on every bus bound for a device -- the master bus and each routed sink's
own -- and counts the block (``sink_scrubs()``) so the status bar can say
so. A finite block is untouched bit for bit.
"""

from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 48000
N = 512
BAD = {100: np.nan, 200: np.inf, 300: -np.inf}


def _speaker_patch():
    p = Patch()
    o = p.add_module("oscillator", params={"amp": 0.5})
    s = p.add_module("speaker_output")
    p.connect(o.id, "out", s.id, "in")
    return p, o


def _routed_patch():
    """An oscillator into a stereo sink routed to a named device, so its
    audio goes to that device's own bus rather than the master."""
    p = Patch()
    o = p.add_module("oscillator", params={"amp": 0.5})
    s = p.add_module("specific_stereo_speaker_output", params={"device": "Test Device"})
    p.connect(o.id, "out", s.id, "in_l")
    p.connect(o.id, "out", s.id, "in_r")
    return p, o


def _poison(backend, module_id, bad=BAD):
    """Make one module's output carry the given non-finite samples."""
    orig = backend._render_module

    def render(module, frames, buffers, patch):
        result = orig(module, frames, buffers, patch)
        if module.id != module_id:
            return result
        if isinstance(result, dict):
            result = dict(result)
            buf = np.array(result["out"], copy=True)
            for i, v in bad.items():
                buf[..., i] = v
            result["out"] = buf
            return result
        buf = np.array(result, copy=True)
        for i, v in bad.items():
            buf[..., i] = v
        return buf

    backend._render_module = render


def _render(make, poison, blocks=1):
    p, o = make()
    b = NumpyBackend(sample_rate=SR, block_size=N)
    b.compile(p)
    if poison:
        _poison(b, o.id)
    outs = []
    with np.errstate(invalid="ignore", over="ignore"):
        for _ in range(blocks):
            outs.append(b.render_block_multi(N))
    return b, outs


class TestMasterBus:
    def test_a_clean_render_is_untouched_and_counts_nothing(self):
        b, outs = _render(_speaker_patch, poison=False, blocks=4)
        assert b.sink_scrubs() == 0
        assert all(np.isfinite(out).all() for out, _ in outs)

    def test_nan_and_inf_become_silence_and_nothing_else_moves(self):
        _, clean = _render(_speaker_patch, poison=False)
        b, dirty = _render(_speaker_patch, poison=True)
        ref, out = clean[0][0], dirty[0][0]
        assert np.isfinite(out).all()
        bad = sorted(BAD)
        assert np.array_equal(out[bad], np.zeros((len(bad), 2), dtype=np.float32))
        keep = np.ones(N, dtype=bool)
        keep[bad] = False
        # Only the poisoned samples changed -- the rest of the block plays
        # exactly as it would have.
        assert np.array_equal(out[keep], ref[keep])
        assert ref[bad].any()  # the samples we zeroed were not already zero

    def test_the_count_is_blocks_not_samples(self):
        b, _ = _render(_speaker_patch, poison=True, blocks=5)
        assert b.sink_scrubs() == 5

    def test_render_block_gets_the_scrubbed_bus(self):
        p, o = _speaker_patch()
        b = NumpyBackend(sample_rate=SR, block_size=N)
        b.compile(p)
        _poison(b, o.id)
        with np.errstate(invalid="ignore"):
            out = b.render_block(N)
        assert np.isfinite(out).all()
        assert b.sink_scrubs() == 1


class TestRoutedBus:
    def test_a_device_bus_is_scrubbed_too(self):
        b, dirty = _render(_routed_patch, poison=True)
        master, device_blocks = dirty[0]
        assert device_blocks, "the routed sink should render to its own bus"
        for blk in device_blocks.values():
            assert np.isfinite(blk).all()
            assert np.array_equal(blk[sorted(BAD)], np.zeros((len(BAD), 2), dtype=np.float32))
        assert np.isfinite(master).all()
        assert b.sink_scrubs() == 1

    def test_a_clean_device_bus_matches_its_unscrubbed_self(self):
        _, clean = _render(_routed_patch, poison=False)
        _, dirty = _render(_routed_patch, poison=True)
        (_, cb), (_, db) = clean[0], dirty[0]
        assert cb.keys() == db.keys()
        keep = np.ones(N, dtype=bool)
        keep[sorted(BAD)] = False
        for key in cb:
            assert np.array_equal(db[key][keep], cb[key][keep])


def test_a_runaway_loop_never_reaches_the_device():
    """The real-world shape: a bare feedback loop with gain 2 runs to inf
    then NaN. The door scrubs what it feeds BACK, but the block the loop
    blew up in still flows forward to the speaker -- the sink catches it."""
    p = Patch()
    m = p.add_module("mixer", params={"gain1": 1.0, "gain2": 2.0, "master": 1.0})
    o = p.add_module("oscillator", params={"amp": 0.5})
    s = p.add_module("speaker_output")
    p.connect(o.id, "out", m.id, "in1")
    p.connect(m.id, "out", s.id, "in")
    p.connect(m.id, "out", m.id, "in2")
    b = NumpyBackend(sample_rate=SR, block_size=N)
    b.compile(p)
    with np.errstate(over="ignore", invalid="ignore"):
        outs = [b.render_block(N) for _ in range(300)]
    assert b.feedback_scrubs() > 0, "the loop should have run away"
    assert b.sink_scrubs() > 0
    assert all(np.isfinite(out).all() for out in outs)
    assert max(float(np.abs(out).max()) for out in outs) <= 1.0


@pytest.mark.parametrize("frames", [64, 1000])
def test_scrub_holds_at_any_block_size(frames):
    p, o = _speaker_patch()
    b = NumpyBackend(sample_rate=SR, block_size=frames)
    b.compile(p)
    _poison(b, o.id, bad={0: np.nan, frames - 1: np.inf})
    with np.errstate(invalid="ignore"):
        out = b.render_block(frames)
    assert np.isfinite(out).all()
    assert b.sink_scrubs() == 1
