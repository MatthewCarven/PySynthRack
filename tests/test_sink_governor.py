"""The buffered sink's patchable ring governor — fill cv out, ratio_cv in.

Covers the three engine pieces that make a feedback governor patch legal
and useful:

  * the topological sort ignores cables leaving the sink's delayed
    ``fill`` port, so a fill -> controller -> ratio_cv loop doesn't drop
    the controller chain into arbitrary leftover order;
  * ``fill`` is seeded into the buffer store every block (neutral 0.5
    with no live stream), one block delayed, visible to the CV meters;
  * a cabled ``ratio_cv`` time-stretches the block pushed to the sink's
    secondary stream (smoothed, clamped) — pitch-preserving since Slice
    2: a streaming WSOLA shift by ``ratio`` cancelled by the length
    resample — while an unpatched ratio_cv leaves the push bit-identical
    to the pre-governor engine.

No audio hardware is touched: everything runs through render_block_multi
on an uncompiled-stream backend, same as the routing tests in
test_buffered_specific_speaker.py.
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend, _DeviceOutput

# Importing the module files registers their types with the Patch factory.
from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules import lfo as _lfo  # noqa: F401
from pysynthrack.modules import oscillator as _osc  # noqa: F401
from pysynthrack.modules import output as _output  # noqa: F401

SINK = "buffered_specific_speaker_output"


# ----- topological sort with a feedback patch --------------------------------


class TestDelayedEdgeTopoSort:
    def _loop_patch(self):
        """sink.fill -> lfoA -> lfoB -> sink.ratio_cv, built in an
        adversarial creation order (downstream lfoB added BEFORE its
        upstream lfoA) so the old leftover-append order would run the
        controllers backwards."""
        patch = Patch()
        sink = patch.add_module(SINK)
        lfo_b = patch.add_module("lfo")   # downstream, created first
        lfo_a = patch.add_module("lfo")   # upstream, created second
        patch.connect(sink.id, "fill", lfo_a.id, "rate_cv")
        patch.connect(lfo_a.id, "cv", lfo_b.id, "rate_cv")
        patch.connect(lfo_b.id, "cv", sink.id, "ratio_cv")
        return patch, sink, lfo_a, lfo_b

    def test_controller_chain_orders_upstream_first(self):
        patch, _sink, lfo_a, lfo_b = self._loop_patch()
        order = NumpyBackend._topological_sort(patch)
        assert order.index(lfo_a.id) < order.index(lfo_b.id)

    def test_every_module_ordered_exactly_once(self):
        patch, *_ = self._loop_patch()
        order = NumpyBackend._topological_sort(patch)
        assert sorted(order) == sorted(patch.modules)

    def test_sink_orders_after_its_governor(self):
        # ratio_cv is a REAL within-block dependency (only fill is
        # delayed), so the sink sorts after the controller that feeds it.
        patch, sink, _lfo_a, lfo_b = self._loop_patch()
        order = NumpyBackend._topological_sort(patch)
        assert order.index(lfo_b.id) < order.index(sink.id)

    def test_acyclic_patches_unaffected(self):
        # No feedback: plain source -> sink still sorts source-first.
        patch = Patch()
        sink = patch.add_module(SINK)
        osc = patch.add_module("oscillator")
        patch.connect(osc.id, "out", sink.id, "in_l")
        order = NumpyBackend._topological_sort(patch)
        assert order.index(osc.id) < order.index(sink.id)


# ----- fill seeding & the governed push ---------------------------------------

SR = 44100
FRAMES = 256


def _backend(patch):
    b = NumpyBackend(sample_rate=SR, block_size=FRAMES)
    b.compile(patch)
    return b


class TestFillSeed:
    def test_neutral_half_with_no_stream(self):
        # Master-bus sink (device empty): no ring, so fill seeds 0.5 —
        # zero error against the half-full setpoint.
        patch = Patch()
        sink = patch.add_module(SINK)
        b = _backend(patch)
        b.render_block_multi(FRAMES)
        assert b.snapshot_meter_levels()[(sink.id, "fill")] == 0.5

    def test_neutral_half_with_device_but_unopened_stream(self):
        # A named device whose stream never opened (no start() in tests)
        # is the 'failed open / stopped' shape: still the neutral seed.
        patch = Patch()
        sink = patch.add_module(SINK, params={"device": "GovDev"})
        b = _backend(patch)
        b.render_block_multi(FRAMES)
        assert b.snapshot_meter_levels()[(sink.id, "fill")] == 0.5


class TestGovernedPush:
    def _patch(self, value):
        patch = Patch()
        sink = patch.add_module(SINK, params={"device": "GovDev"})
        const = patch.add_module("constant", params={"value": value})
        patch.connect(const.id, "out", sink.id, "ratio_cv")
        return patch, sink

    def test_unpatched_ratio_pushes_exactly_frames(self):
        patch = Patch()
        patch.add_module(SINK, params={"device": "GovDev"})
        b = _backend(patch)
        _out, blocks = b.render_block_multi(FRAMES)
        (blk,) = blocks.values()
        assert blk.shape == (FRAMES, 2)
        assert b._sink_ratio == {}     # no cable -> no governor state at all
        assert b._sink_stretch == {}   # ...and no stretch engines either

    def test_positive_cv_stretches_the_push(self):
        # cv +1 at default depth 0.25 -> ratio converges on 1.25, so the
        # pushed block settles at frames * 1.25 (more samples: the
        # catch-a-draining-ring direction).
        patch, _sink = self._patch(1.0)
        b = _backend(patch)
        for _ in range(80):
            _out, blocks = b.render_block_multi(FRAMES)
        (blk,) = blocks.values()
        assert blk.shape[0] == round(FRAMES * 1.25)

    def test_negative_cv_shrinks_the_push(self):
        patch, _sink = self._patch(-1.0)
        b = _backend(patch)
        for _ in range(80):
            _out, blocks = b.render_block_multi(FRAMES)
        (blk,) = blocks.values()
        assert blk.shape[0] == round(FRAMES * 0.75)

    def test_ratio_clamped_to_2x(self):
        # cv +10 asks for 1 + 10*0.25 = 3.5; the engine rail is 2.0.
        patch, _sink = self._patch(10.0)
        b = _backend(patch)
        for _ in range(80):
            _out, blocks = b.render_block_multi(FRAMES)
        (blk,) = blocks.values()
        assert blk.shape[0] == 2 * FRAMES

    def test_smoothing_walks_not_jumps(self):
        # First governed block moves one smoothing step toward the
        # target (1 + 0.2 * 0.25 = 1.05), not the whole way.
        patch, _sink = self._patch(1.0)
        b = _backend(patch)
        _out, blocks = b.render_block_multi(FRAMES)
        (blk,) = blocks.values()
        assert blk.shape[0] == round(FRAMES * 1.05)


class TestPitchPreserved:
    """Slice 2's whole point: a governed stretch must NOT bend pitch.

    A 1 kHz sine pushed through a converged ratio-1.25 stretch keeps its
    1 kHz fundamental (the WSOLA shift and the length resample cancel);
    the Slice-1 varispeed actuator would have landed it at 800 Hz.
    """

    F0 = 1000.0

    def _governed_sine_push(self, blocks_total=120, keep_last=30):
        patch = Patch()
        sink = patch.add_module(SINK, params={"device": "GovDev"})
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": self.F0}
        )
        const = patch.add_module("constant", params={"value": 1.0})
        patch.connect(osc.id, "out", sink.id, "in_l")
        patch.connect(const.id, "out", sink.id, "ratio_cv")
        b = _backend(patch)
        tail = []
        for i in range(blocks_total):
            _out, dev = b.render_block_multi(FRAMES)
            if i >= blocks_total - keep_last:
                (blk,) = dev.values()
                tail.append(blk[:, 0].copy())
        return np.concatenate(tail)

    def test_stretched_push_keeps_the_fundamental(self):
        # Skip engine warm-up (~one 50 ms grain) and ratio convergence
        # by analyzing only the tail; the fundamental of the pushed
        # audio must sit at F0, not F0 / 1.25.
        sig = self._governed_sine_push()
        assert float(np.max(np.abs(sig))) > 0.05   # engine is primed, not silent
        spectrum = np.abs(np.fft.rfft(sig * np.hanning(sig.shape[0])))
        peak_hz = float(np.argmax(spectrum)) * SR / sig.shape[0]
        assert abs(peak_hz - self.F0) < 50.0, (
            f"fundamental at {peak_hz:.1f} Hz — varispeed bend would be "
            f"{self.F0 / 1.25:.0f} Hz, pitch-preserving keeps {self.F0:.0f} Hz"
        )


# ----- the built-in governor (auto_govern) -----------------------------------

SINK_BLOCK = 512          # the sink's own stream block; ring capacity = 8x
CAPACITY = 8 * SINK_BLOCK


def _inject_ring(backend, fill_frac, device="AutoDev"):
    """Give the sink a live secondary ring pre-filled to ``fill_frac`` and
    register it under the sink's stream key, WITHOUT opening a real
    PortAudio stream. The built-in controller reads its fill via telemetry;
    the fake never refills (render_block_multi doesn't run _fill_output), so
    the fill is a fixed operating point to probe the ratio response at."""
    ring = _DeviceOutput(device, 44100, SINK_BLOCK)
    n = round(fill_frac * CAPACITY)
    if n > 0:
        ring.push(np.zeros((n, 2), dtype=np.float32))
    backend._device_outputs[(device, SINK_BLOCK)] = ring
    return ring


class TestAutoGovern:
    def _patch(self, *, auto, cable_value=None, device="AutoDev"):
        patch = Patch()
        sink = patch.add_module(
            SINK,
            params={"device": device, "buffer_size": SINK_BLOCK, "auto_govern": auto},
        )
        osc = patch.add_module("oscillator", params={"freq": 220.0})
        patch.connect(osc.id, "out", sink.id, "in_l")
        if cable_value is not None:
            const = patch.add_module("constant", params={"value": cable_value})
            patch.connect(const.id, "out", sink.id, "ratio_cv")
        return patch, sink

    def _run(self, patch, fill_frac, blocks=70):
        b = _backend(patch)
        _inject_ring(b, fill_frac)
        blk = None
        for _ in range(blocks):
            _out, dev = b.render_block_multi(FRAMES)
            blk = dev[("AutoDev", SINK_BLOCK)]
        return b, blk

    def test_auto_off_ignores_the_ring(self):
        # auto_govern False, no cable: the sink is ungoverned even with a
        # live off-centre ring — the push stays exactly frames.
        patch, _sink = self._patch(auto=False)
        b, blk = self._run(patch, fill_frac=0.05)
        assert blk.shape[0] == FRAMES
        assert b._sink_ratio == {} and b._sink_stretch == {}

    def test_auto_low_ring_stretches_to_refill(self):
        # A near-empty ring -> ratio > 1 -> MORE samples pushed (catch up).
        _b, blk = self._run(self._patch(auto=True)[0], fill_frac=0.05)
        assert blk.shape[0] > FRAMES

    def test_auto_full_ring_shrinks_the_push(self):
        # A near-full ring -> ratio < 1 -> FEWER samples pushed (drain it).
        _b, blk = self._run(self._patch(auto=True)[0], fill_frac=0.95)
        assert blk.shape[0] < FRAMES

    def test_auto_matches_the_canonical_patch_gain(self):
        # Gain 0.5 on the 0.5-fill error: fill 0.10 -> target 1.20.
        _b, blk = self._run(self._patch(auto=True)[0], fill_frac=0.10)
        assert blk.shape[0] == round(FRAMES * (1.0 + 0.5 * (0.5 - 0.10)))

    def test_cable_overrides_auto_govern(self):
        # auto ON *and* a ratio_cv cable: the cable wins. constant -3 ->
        # target 1 + (-3)*0.25 = 0.25, clamped to 0.5x. If auto had won on
        # this near-empty ring the push would be LONGER than frames, not
        # half — so the length proves which path ran.
        patch, _sink = self._patch(auto=True, cable_value=-3.0)
        _b, blk = self._run(patch, fill_frac=0.05)
        assert blk.shape[0] == round(FRAMES * 0.5)
