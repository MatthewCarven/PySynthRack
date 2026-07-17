"""The warping buffered sink — the tape-warp sibling of the buffered speaker.

Same fill→ratio ring governor as ``buffered_specific_speaker_output``, but a
different actuator: instead of the buffered sink's PITCH-PRESERVING WSOLA
stretch, this one does a plain VARISPEED resample, so the governor's
correction bends the pitch. A starving ring commands ratio > 1 → a longer,
slower push → the pitch dives (the tape running out of juice); a recovering
ring spins it back up. The ratio is slewed at a constant tape-transport rate
(``brake_time`` down, ``spinup_time`` up) rather than one-pole smoothed.

Everything runs through ``render_block_multi`` on an unopened-stream backend
with a faked ring, exactly like test_sink_governor.py — no audio hardware.

The contract this pins:
  * the sink joins the buffered *family* (own buffer_size, fill out, governor);
  * the built-in governor still moves the push length the same way (low ring
    → longer push, full ring → shorter);
  * but the push now BENDS PITCH — a governed sine lands at F0 / ratio, the
    varispeed frequency the buffered sink was explicitly built to avoid;
  * the ratio is slewed at the constant brake/spin-up rate, asymmetrically;
  * the plain buffered sink is untouched (its one-pole path still runs).
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend, _DeviceOutput

# Importing the module files registers their types with the Patch factory.
from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules import oscillator as _osc  # noqa: F401
from pysynthrack.modules import output as _output  # noqa: F401

SINK = "warping_buffered_speaker_output"
BUFFERED = "buffered_specific_speaker_output"

SR = 44100
FRAMES = 256
SINK_BLOCK = 512
CAPACITY = 8 * SINK_BLOCK


def _backend(patch):
    b = NumpyBackend(sample_rate=SR, block_size=FRAMES)
    b.compile(patch)
    return b


def _inject_ring(backend, fill_frac, device="AutoDev"):
    """A live secondary ring pre-filled to ``fill_frac`` under the sink's
    stream key, with no real PortAudio stream. The controller reads its fill
    via telemetry; the fake never refills, so it is a fixed operating point."""
    ring = _DeviceOutput(device, SR, SINK_BLOCK)
    n = round(fill_frac * CAPACITY)
    if n > 0:
        ring.push(np.zeros((n, 2), dtype=np.float32))
    backend._device_outputs[(device, SINK_BLOCK)] = ring
    return ring


# ----- family membership / plumbing ------------------------------------------


class TestFamilyMembership:
    def test_in_the_stereo_and_routed_and_buffered_sets(self):
        assert SINK in NumpyBackend._STEREO_SPEAKERS
        assert SINK in NumpyBackend._ROUTED_SPEAKERS
        assert SINK in NumpyBackend._BUFFERED_SPEAKERS

    def test_carries_its_own_buffer_size_like_the_buffered_sink(self):
        patch = Patch()
        sink = patch.add_module(SINK, params={"buffer_size": 2048})
        b = _backend(patch)
        assert b._sink_block_size(sink) == 2048

    def test_fill_out_is_seeded_and_delayed(self):
        # The fill cv-out is published every block (neutral 0.5 with no live
        # stream) and its cables are treated as delayed edges by the sort.
        patch = Patch()
        sink = patch.add_module(SINK)
        b = _backend(patch)
        b.render_block_multi(FRAMES)
        assert b.snapshot_meter_levels()[(sink.id, "fill")] == 0.5


# ----- the governor still moves the push length ------------------------------


class TestWarpAutoGovern:
    """Length behaviour matches the buffered sink (shared control loop);
    auto_govern defaults ON for this sink, so no cable is needed."""

    def _patch(self, device="AutoDev"):
        patch = Patch()
        sink = patch.add_module(
            SINK, params={"device": device, "buffer_size": SINK_BLOCK}
        )
        osc = patch.add_module("oscillator", params={"freq": 220.0})
        patch.connect(osc.id, "out", sink.id, "in_l")
        return patch, sink

    def _run(self, patch, fill_frac, blocks=200):
        b = _backend(patch)
        _inject_ring(b, fill_frac)
        blk = None
        for _ in range(blocks):
            _out, dev = b.render_block_multi(FRAMES)
            blk = dev[("AutoDev", SINK_BLOCK)]
        return b, blk

    def test_auto_defaults_on_for_the_warping_sink(self):
        patch, sink = self._patch()
        assert patch.get(sink.id).params["auto_govern"] is True

    def test_low_ring_stretches_to_refill(self):
        # A near-empty ring → ratio > 1 → MORE samples pushed (and pitch dives).
        _b, blk = self._run(self._patch()[0], fill_frac=0.05)
        assert blk.shape[0] > FRAMES

    def test_full_ring_shrinks_the_push(self):
        # A near-full ring → ratio < 1 → FEWER samples pushed (drain + spin up).
        _b, blk = self._run(self._patch()[0], fill_frac=0.95)
        assert blk.shape[0] < FRAMES

    def test_off_leaves_the_push_bit_identical(self):
        # auto_govern explicitly off and no cable → ungoverned: exactly frames.
        patch = Patch()
        sink = patch.add_module(
            SINK,
            params={
                "device": "AutoDev",
                "buffer_size": SINK_BLOCK,
                "auto_govern": False,
            },
        )
        osc = patch.add_module("oscillator", params={"freq": 220.0})
        patch.connect(osc.id, "out", sink.id, "in_l")
        b = _backend(patch)
        _inject_ring(b, 0.05)
        blk = None
        for _ in range(40):
            _out, dev = b.render_block_multi(FRAMES)
            blk = dev[("AutoDev", SINK_BLOCK)]
        assert blk.shape[0] == FRAMES
        assert b._sink_ratio == {} and b._sink_stretch == {}


# ----- the defining difference: the push BENDS pitch -------------------------


class TestWarpBendsPitch:
    """The buffered sink holds pitch (WSOLA); this one moves it (varispeed).

    A 1 kHz sine pushed through a converged ratio-1.25 stretch lands at
    1000 / 1.25 = 800 Hz — the tape frequency the buffered sink was built to
    cancel. test_sink_governor.py::TestPitchPreserved asserts the *opposite*
    (1000 Hz) for the buffered sink, so together they pin the divergence.
    """

    F0 = 1000.0
    RATIO = 1.25  # constant ratio_cv 1.0 at default depth 0.25

    def _governed_sine_push(self, sink_type, blocks_total=160, keep_last=40):
        patch = Patch()
        sink = patch.add_module(
            sink_type, params={"device": "GovDev", "buffer_size": SINK_BLOCK}
        )
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": self.F0}
        )
        const = patch.add_module("constant", params={"value": 1.0})
        patch.connect(osc.id, "out", sink.id, "in_l")
        patch.connect(const.id, "out", sink.id, "ratio_cv")  # overrides auto
        b = _backend(patch)
        tail = []
        for i in range(blocks_total):
            _out, dev = b.render_block_multi(FRAMES)
            if i >= blocks_total - keep_last:
                (blk,) = dev.values()
                tail.append(blk[:, 0].copy())
        return np.concatenate(tail)

    def _peak_hz(self, sig):
        spectrum = np.abs(np.fft.rfft(sig * np.hanning(sig.shape[0])))
        return float(np.argmax(spectrum)) * SR / sig.shape[0]

    def test_warp_push_lands_at_the_varispeed_frequency(self):
        sig = self._governed_sine_push(SINK)
        assert float(np.max(np.abs(sig))) > 0.05  # producing sound, not silent
        peak = self._peak_hz(sig)
        expected = self.F0 / self.RATIO  # 800 Hz
        assert abs(peak - expected) < 40.0, (
            f"fundamental at {peak:.1f} Hz — varispeed should bend 1 kHz to "
            f"{expected:.0f} Hz (buffered sink would hold 1000 Hz)"
        )

    def test_buffered_sibling_still_holds_pitch(self):
        # Same patch on the plain buffered sink keeps the fundamental — proof
        # the shared refactor didn't leak the varispeed path into it.
        sig = self._governed_sine_push(BUFFERED)
        peak = self._peak_hz(sig)
        assert abs(peak - self.F0) < 40.0, (
            f"buffered sink fundamental at {peak:.1f} Hz — should stay "
            f"{self.F0:.0f} Hz (pitch-preserving)"
        )


# ----- the tape-transport slew -----------------------------------------------


class TestBrakeSlew:
    """The warp ratio coasts at a constant per-block rate (linear, not the
    one-pole ease-out), asymmetric between braking and spin-up."""

    def _sink(self, **params):
        patch = Patch()
        sink = patch.add_module(SINK, params=params)
        b = _backend(patch)
        return b, sink

    def _max_step(self, secs):
        return NumpyBackend._RATIO_SWING * FRAMES / (secs * SR)

    def test_rising_uses_brake_time(self):
        b, sink = self._sink(brake_time=0.5, spinup_time=0.25)
        out = b._brake_slew(sink, 1.0, 1.9)  # far target → capped at one step
        assert out == 1.0 + self._max_step(0.5)

    def test_falling_uses_spinup_time(self):
        b, sink = self._sink(brake_time=0.5, spinup_time=0.25)
        out = b._brake_slew(sink, 1.5, 0.6)  # far target down → one step
        assert out == 1.5 - self._max_step(0.25)

    def test_recovery_is_faster_than_braking_by_default(self):
        # spinup_time 0.25 < brake_time 0.5 → the down-step is the bigger move.
        b, sink = self._sink(brake_time=0.5, spinup_time=0.25)
        up = b._brake_slew(sink, 1.0, 2.0) - 1.0
        down = 1.0 - b._brake_slew(sink, 1.0, 0.5)
        assert down > up

    def test_small_move_reaches_target_in_one_block(self):
        b, sink = self._sink(brake_time=0.5, spinup_time=0.25)
        # A step smaller than the per-block cap lands exactly on target.
        tiny = 1.0 + self._max_step(0.5) / 3.0
        assert b._brake_slew(sink, 1.0, tiny) == tiny

    def test_zero_time_snaps_instantly(self):
        b, sink = self._sink(brake_time=0.0, spinup_time=0.0)
        assert b._brake_slew(sink, 1.0, 2.0) == 2.0
        assert b._brake_slew(sink, 2.0, 0.5) == 0.5

    def test_walks_linearly_not_exponentially(self):
        # Successive braking steps are equal-sized (constant torque), unlike a
        # one-pole whose steps shrink as it approaches the target.
        b, sink = self._sink(brake_time=0.5, spinup_time=0.25)
        r0 = 1.0
        r1 = b._brake_slew(sink, r0, 2.0)
        r2 = b._brake_slew(sink, r1, 2.0)
        assert abs((r1 - r0) - (r2 - r1)) < 1e-12
