"""Tests for SpecificStereoSpeakerOutput — the device-targetable stereo sink.

Slice 2: real per-device routing. A sink left on the default device
(``device == ""``) still drains into the shared master bus, bit-identically to
:class:`StereoSpeakerOutput`. A sink with a **named** device is pulled OFF the
master onto that device's own bus (``render_block_multi`` returns it under the
device name), fed live to a secondary OutputStream through a drop-oldest ring.

Coverage:
  - Model: registration, Outputs category, defaults (incl. ``device=""``),
    ports & signal kinds (identical to the stereo speaker), JSON round-trip of
    ``device``, ``set_param`` round-trip, unknown param rejected, type walls,
    sink-ness (drained, not rendered).
  - available_output_devices(): returns a list, never raises.
  - Default-device equivalence: bit-identical master bus to
    stereo_speaker_output across a mono / stereo / pan / width / gain / CV
    sweep (device left empty), and no device_blocks produced.
  - Routing: a named device leaves the master silent and appears in
    device_blocks with the exact stereo-speaker drain; two sinks on one device
    sum; two devices split; a routed + a master sink coexist; render_block
    still returns master only; each device bus is clipped.
  - _DeviceOutput ring (sample-accurate): FIFO order, underrun -> zero-pad,
    overflow -> drop oldest, and a device block size that differs from the
    push size (smaller, larger, or partially filled) reads sample-accurately.
  - Neutral default: a stereo pair passes to the bus bit-exactly via the
    shared _drain_stereo_speaker.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend, _DeviceOutput
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type, grouped_module_types
from pysynthrack.modules.output import (
    AUTO_DEVICE,
    SpecificStereoSpeakerOutput,
    available_output_devices,
)

SR, F = 44100, 512
TYPE = "specific_stereo_speaker_output"


def _render(patch, blocks=4):
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    return np.concatenate([b.render_block(F) for _ in range(blocks)]), b


def _multi(patch, frames=F):
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    return b.render_block_multi(frames)


def _build(sink_type, mode, params, cv=False):
    """Identical wiring for either sink type; only the sink TYPE differs."""
    patch = Patch()
    l = patch.add_module("oscillator", params={"amp": 0.4})
    sink = patch.add_module(sink_type, params=params)
    patch.connect(l.id, "out", sink.id, "in_l")
    if mode == "stereo":
        r = patch.add_module(
            "oscillator",
            params={"amp": 0.3, "waveform": "square", "freq": 330.0},
        )
        patch.connect(r.id, "out", sink.id, "in_r")
    if cv:
        lfo = patch.add_module("lfo", params={"rate": 2.0, "depth": 1.0})
        patch.connect(lfo.id, "cv", sink.id, "pan_cv")
        patch.connect(lfo.id, "cv", sink.id, "width_cv")
    return patch


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        sp = Patch().add_module(TYPE)
        assert isinstance(sp, SpecificStereoSpeakerOutput)
        assert sp.params == {
            "gain": 1.0, "pan": 0.0, "width": 1.0, "cv_depth": 1.0,
            "device": AUTO_DEVICE,
        }
        assert AUTO_DEVICE == ""

    def test_registered_in_outputs_category(self):
        assert get_module_type(TYPE) is SpecificStereoSpeakerOutput
        outs = dict(grouped_module_types())["Outputs"]
        assert TYPE in outs

    def test_ports_match_the_stereo_speaker(self):
        sp = Patch().add_module(TYPE)
        assert [(p.name, p.signal_kind) for p in sp.input_ports] == [
            ("in_l", "audio"), ("in_r", "audio"),
            ("pan_cv", "cv"), ("width_cv", "cv"),
        ]
        assert sp.output_ports == []

    def test_device_round_trips_through_json(self):
        patch = Patch()
        patch.add_module(TYPE, params={"device": "Focusrite USB", "pan": -0.4})
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == TYPE)
        assert mod.params["device"] == "Focusrite USB"
        assert mod.params["pan"] == -0.4

    def test_set_param_device(self):
        sp = Patch().add_module(TYPE)
        sp.set_param("device", "Speakers (Realtek)")
        assert sp.params["device"] == "Speakers (Realtek)"

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module(TYPE, params={"balance": 0.0})

    def test_type_walls(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        osc = patch.add_module("oscillator")
        sp = patch.add_module(TYPE)
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", sp.id, "in_l")     # cv -> audio
        with pytest.raises(Exception):
            patch.connect(osc.id, "out", sp.id, "pan_cv")  # audio -> cv

    def test_is_a_drained_sink_not_rendered(self):
        patch = Patch()
        sp = patch.add_module(TYPE)
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        assert b._render_module(sp, F, {}, patch) is None


class TestAvailableOutputDevices:
    def test_returns_list_never_raises(self):
        got = available_output_devices()
        assert isinstance(got, list)
        assert all(isinstance(n, str) for n in got)


# ----- Default device: bit-identical to the stereo speaker -------------------

_PARAM_CASES = [
    ("mono", {}, False),
    ("mono", {"pan": -0.7}, False),
    ("mono", {"pan": 0.5, "gain": 0.8}, False),
    ("stereo", {}, False),
    ("stereo", {"pan": 0.4, "width": 1.6}, False),
    ("stereo", {"width": 0.0}, False),
    ("stereo", {"width": 2.0, "gain": 1.2}, False),
    ("mono", {"pan": 0.2, "cv_depth": 0.8}, True),
    ("stereo", {"width": 1.3, "cv_depth": 1.5}, True),
    ("stereo", {"pan": -0.3, "width": 0.7, "gain": 0.9, "cv_depth": 1.0}, True),
]


class TestDefaultDeviceMatchesStereoSpeaker:
    @pytest.mark.parametrize("mode,params,cv", _PARAM_CASES)
    def test_master_bus_bit_identical(self, mode, params, cv):
        ref, _ = _render(_build("stereo_speaker_output", mode, dict(params), cv))
        got, _ = _render(_build(TYPE, mode, dict(params), cv))  # device left ""
        assert np.array_equal(ref, got)

    def test_empty_device_produces_no_device_blocks(self):
        master, dev = _multi(_build(TYPE, "stereo", {"pan": 0.3}))
        assert dev == {}
        ref, _ = _render(_build("stereo_speaker_output", "stereo", {"pan": 0.3}))
        assert np.array_equal(master, ref[:F])


# ----- Routing: a named device pulls the sink off the master -----------------


class TestDeviceRouting:
    def test_named_device_leaves_master_silent(self):
        master, dev = _multi(_build(TYPE, "mono", {"device": "Cans"}))
        assert np.max(np.abs(master)) == 0.0
        assert ("Cans", F) in dev and np.max(np.abs(dev[("Cans", F)])) > 0.0

    def test_device_bus_equals_stereo_speaker_drain(self):
        master, dev = _multi(_build(TYPE, "stereo", {"pan": 0.4, "device": "Cans"}))
        ref, _ = _render(_build("stereo_speaker_output", "stereo", {"pan": 0.4}))
        assert np.array_equal(dev[("Cans", F)], ref[:F])
        assert np.max(np.abs(master)) == 0.0

    def test_two_sinks_same_device_sum(self):
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.4})
        o2 = p.add_module("oscillator", params={"amp": 0.4})
        s1 = p.add_module(TYPE, params={"device": "Cans"})
        s2 = p.add_module(TYPE, params={"device": "Cans"})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        _, dev = _multi(p)
        assert list(dev) == [("Cans", F)]
        one = Patch()
        oa = one.add_module("oscillator", params={"amp": 0.4})
        sa = one.add_module(TYPE, params={"device": "Cans"})
        one.connect(oa.id, "out", sa.id, "in_l")
        _, dev1 = _multi(one)
        assert np.allclose(dev[("Cans", F)], 2.0 * dev1[("Cans", F)], atol=1e-6)

    def test_two_devices_split(self):
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.4})
        o2 = p.add_module("oscillator", params={"amp": 0.3, "freq": 330.0})
        s1 = p.add_module(TYPE, params={"device": "A"})
        s2 = p.add_module(TYPE, params={"device": "B"})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        master, dev = _multi(p)
        assert set(dev) == {("A", F), ("B", F)}
        assert np.max(np.abs(master)) == 0.0
        assert not np.array_equal(dev[("A", F)], dev[("B", F)])

    def test_routed_and_master_sinks_coexist(self):
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.4})
        o2 = p.add_module("oscillator", params={"amp": 0.4})
        routed = p.add_module(TYPE, params={"device": "Cans"})
        onbus = p.add_module(TYPE)  # default device -> master
        p.connect(o1.id, "out", routed.id, "in_l")
        p.connect(o2.id, "out", onbus.id, "in_l")
        master, dev = _multi(p)
        ref_master, _ = _render(_build(TYPE, "mono", {}))
        assert np.array_equal(master, ref_master[:F])
        assert np.max(np.abs(dev[("Cans", F)])) > 0.0

    def test_render_block_returns_master_only(self):
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(_build(TYPE, "mono", {"device": "Cans"}))
        master_only = b.render_block(F)
        assert np.max(np.abs(master_only)) == 0.0

    def test_device_bus_is_clipped(self):
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.9})
        o2 = p.add_module("oscillator", params={"amp": 0.9})
        s1 = p.add_module(TYPE, params={"device": "Cans", "pan": -1.0})
        s2 = p.add_module(TYPE, params={"device": "Cans", "pan": -1.0})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        _, dev = _multi(p)
        assert dev[("Cans", F)].max() <= 1.0 and dev[("Cans", F)].min() >= -1.0


class TestDeviceOutputRing:
    """Ring semantics of _DeviceOutput without opening a real PortAudio stream."""

    def _blk(self, v):
        return np.full((F, 2), v, dtype=np.float32)

    def test_fifo_order(self):
        d = _DeviceOutput("X", SR, F, max_blocks=4)
        d.push(self._blk(0.1)); d.push(self._blk(0.2))
        o = np.zeros((F, 2), dtype=np.float32)
        d._callback(o, F, None, None); assert np.allclose(o, 0.1)
        d._callback(o, F, None, None); assert np.allclose(o, 0.2)

    def test_underrun_is_silence(self):
        d = _DeviceOutput("X", SR, F, max_blocks=4)
        o = np.ones((F, 2), dtype=np.float32)
        d._callback(o, F, None, None)
        assert np.all(o == 0.0)

    def test_overflow_drops_oldest(self):
        d = _DeviceOutput("X", SR, F, max_blocks=2)
        d.push(self._blk(0.1)); d.push(self._blk(0.2)); d.push(self._blk(0.3))
        o = np.zeros((F, 2), dtype=np.float32)
        d._callback(o, F, None, None); assert np.allclose(o, 0.2)  # 0.1 dropped
        d._callback(o, F, None, None); assert np.allclose(o, 0.3)

    def test_smaller_device_block_reads_partial(self):
        # One F-sample push drained by a device whose block is F//2: two
        # sample-accurate halves, not silence (the old block-ring's failure
        # mode — a size mismatch used to yield a dead-silent stream).
        d = _DeviceOutput("X", SR, F // 2, max_blocks=4)
        d.push(self._blk(0.5))
        o = np.zeros((F // 2, 2), dtype=np.float32)
        d._callback(o, F // 2, None, None); assert np.allclose(o, 0.5)
        d._callback(o, F // 2, None, None); assert np.allclose(o, 0.5)
        o[:] = 1.0
        d._callback(o, F // 2, None, None); assert np.all(o == 0.0)  # drained

    def test_larger_device_block_assembles_from_pushes(self):
        # Device block is 2F, fed by two F-sample pushes: one pop spans both.
        d = _DeviceOutput("X", SR, 2 * F, max_blocks=4)
        d.push(self._blk(0.1)); d.push(self._blk(0.2))
        o = np.zeros((2 * F, 2), dtype=np.float32)
        d._callback(o, 2 * F, None, None)
        assert np.allclose(o[:F], 0.1) and np.allclose(o[F:], 0.2)

    def test_partial_fill_zero_pads_tail(self):
        # Only F queued but 2F asked for: first half is the audio, tail silent.
        d = _DeviceOutput("X", SR, 2 * F, max_blocks=4)
        d.push(self._blk(0.3))
        o = np.ones((2 * F, 2), dtype=np.float32)
        d._callback(o, 2 * F, None, None)
        assert np.allclose(o[:F], 0.3) and np.all(o[F:] == 0.0)

    def test_capacity_holds_max_blocks_device_blocks(self):
        # Capacity scales with the DEVICE block, so a secondary buffer larger
        # than the main block still has room for one full pop.
        d = _DeviceOutput("X", SR, 2 * F, max_blocks=3)
        assert d._capacity == 3 * 2 * F


class TestLiveDeviceSwitch:
    """Reconciling secondary streams live, without a Stop/Start.

    _DeviceOutput.open / .close are stubbed so no real PortAudio device is
    touched; the tests assert the reconcile bookkeeping — which streams open,
    which close, which are kept.
    """

    @pytest.fixture
    def rig(self, monkeypatch):
        opened, closed = [], []
        monkeypatch.setattr(
            _DeviceOutput, "open", lambda self: opened.append(self.device)
        )
        monkeypatch.setattr(
            _DeviceOutput, "close", lambda self: closed.append(self.device)
        )
        p = Patch()
        o = p.add_module("oscillator", params={"amp": 0.4})
        s = p.add_module(TYPE, params={"device": "A"})
        p.connect(o.id, "out", s.id, "in_l")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(p)
        b._running = True          # simulate a running stream (no PortAudio)
        b._sync_device_outputs()   # start-equivalent: opens "A"
        return b, s, opened, closed

    def test_initial_open(self, rig):
        b, s, opened, closed = rig
        assert set(b._device_outputs) == {("A", F)} and opened == ["A"]

    def test_switch_rebuilds_only_the_changed_stream(self, rig):
        b, s, opened, closed = rig
        b.set_param(s.id, "device", "B")
        assert set(b._device_outputs) == {("B", F)}
        assert opened == ["A", "B"] and closed == ["A"]

    def test_unrelated_param_leaves_streams_alone(self, rig):
        b, s, opened, closed = rig
        opened.clear(); closed.clear()
        b.set_param(s.id, "pan", 0.5)
        assert opened == [] and closed == [] and set(b._device_outputs) == {("A", F)}

    def test_kept_stream_identity_preserved(self, rig):
        b, s, opened, closed = rig
        a_obj = b._device_outputs[("A", F)]
        b.set_param(s.id, "gain", 0.8)     # no device change
        assert b._device_outputs[("A", F)] is a_obj

    def test_switch_to_default_closes_stream(self, rig):
        b, s, opened, closed = rig
        b.set_param(s.id, "device", "")
        assert b._device_outputs == {} and closed == ["A"]

    def test_no_reconcile_while_stopped(self, rig):
        b, s, opened, closed = rig
        b._running = False
        opened.clear(); closed.clear()
        b.set_param(s.id, "device", "C")
        assert opened == [] and closed == [] and set(b._device_outputs) == {("A", F)}

    def test_open_failure_is_skipped(self, rig, monkeypatch):
        b, s, opened, closed = rig

        def boom(self):
            raise RuntimeError("no such device")

        monkeypatch.setattr(_DeviceOutput, "open", boom)
        b.set_param(s.id, "device", "Dead")   # must not raise
        assert b._device_outputs == {}         # that sink is silent

    def test_compile_reconciles_added_routed_sink(self, rig):
        b, s, opened, closed = rig
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.4})
        s1 = p.add_module(TYPE, params={"device": "A"})
        o2 = p.add_module("oscillator", params={"amp": 0.4})
        s2 = p.add_module(TYPE, params={"device": "B"})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        b.compile(p)                           # running -> reconcile at end
        assert set(b._device_outputs) == {("A", F), ("B", F)}


class TestNeutralDefault:
    def test_stereo_pair_passes_bit_exactly(self):
        patch = Patch()
        l = patch.add_module("oscillator", params={"amp": 0.4})
        r = patch.add_module("oscillator", params={"amp": 0.3})
        sp = patch.add_module(TYPE)
        patch.connect(l.id, "out", sp.id, "in_l")
        patch.connect(r.id, "out", sp.id, "in_r")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        lb = np.full(F, 0.4, dtype=np.float32)
        rb = np.full(F, -0.2, dtype=np.float32)
        out = np.zeros((F, 2), dtype=np.float32)
        b._drain_stereo_speaker(
            sp, F, {(l.id, "out"): lb, (r.id, "out"): rb}, patch, out
        )
        assert np.array_equal(out[:, 0], lb)
        assert np.array_equal(out[:, 1], rb)
