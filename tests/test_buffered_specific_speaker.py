"""Tests for BufferedSpecificSpeakerOutput — the device-targetable stereo
sink with its own per-sink output buffer size.

It is a :class:`SpecificStereoSpeakerOutput` plus a ``buffer_size`` param that
sets the block size of its *own* secondary output stream, independent of the
global block size. The DSP (pan / width / gain / CV drain) is unchanged; only
the routing key differs: secondary streams are keyed by ``(device,
block_size)`` so one physical device can carry several streams at different
buffer sizes.

Coverage:
  - Model: registration, Outputs category, defaults (``device=""``,
    ``buffer_size=512``), ports identical to the stereo speaker, JSON
    round-trip of device + buffer_size, set_param round-trip, unknown param
    rejected, sink-ness (drained, not rendered).
  - _sink_block_size / _stream_key: a buffered sink keys by its own
    buffer_size; an empty device is master (None); a plain specific sink keys
    by the global block size; buffer_size is clamped to [16, 8192] and coerced
    from float / garbage.
  - Default device: bit-identical master bus to stereo_speaker_output and no
    device_blocks, whatever the buffer_size (it only affects a routed stream).
  - Routing: a routed buffered sink lands under (device, buffer_size); two
    buffer sizes on one device split into two streams; a buffered + a plain
    sink on one device share only when the sizes match; the routed device bus
    equals the plain stereo-speaker drain.
  - Secondary stream: _sync_device_outputs opens each stream at the sink's
    buffer_size; a live buffer_size change rebuilds only that stream; a change
    while stopped does not reconcile.
  - Extended sizes: the sink keys/opens past the global 1024 ceiling (2048 /
    4096 / 8192, the SINK_BUFFER_SIZES extensions).
  - Ring telemetry: a fresh ring reads (0, cap, 0, 0); fill tracks push/pop;
    underruns arm only once the ring FIRST fills to one device block (priming
    callbacks and the large-sink fill-up gap at Start are startup, not
    trouble), then starvation counts; overflow (drop-oldest) and
    oversize-block truncation count as drops; close() resets everything.
  - snapshot_sink_buffers: keyed by module id; idle (absent) for master-bus /
    stopped / failed-open sinks; sinks sharing one stream report the same
    tuple; the plain specific sink is included too.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import (
    _MAX_SINK_BLOCK,
    _MIN_SINK_BLOCK,
    NumpyBackend,
    _DeviceOutput,
)
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type, grouped_module_types
from pysynthrack.modules.output import (
    AUTO_DEVICE,
    BufferedSpecificSpeakerOutput,
)

SR, F = 44100, 512
TYPE = "buffered_specific_speaker_output"
SPECIFIC = "specific_stereo_speaker_output"


def _backend(block_size=F):
    return NumpyBackend(sample_rate=SR, block_size=block_size)


def _render(patch, blocks=4):
    b = _backend()
    b.compile(patch)
    return np.concatenate([b.render_block(F) for _ in range(blocks)]), b


def _multi(patch, frames=F, block_size=F):
    b = _backend(block_size)
    b.compile(patch)
    return b.render_block_multi(frames)


def _build(sink_type, mode, params, cv=False):
    """Identical wiring for either sink type; only the sink TYPE/params differ."""
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
        assert isinstance(sp, BufferedSpecificSpeakerOutput)
        assert sp.params == {
            "gain": 1.0, "pan": 0.0, "width": 1.0, "cv_depth": 1.0,
            "device": AUTO_DEVICE, "buffer_size": 512,
        }

    def test_registered_in_outputs_category(self):
        assert get_module_type(TYPE) is BufferedSpecificSpeakerOutput
        outs = dict(grouped_module_types())["Outputs"]
        assert TYPE in outs

    def test_ports_match_the_stereo_speaker(self):
        sp = Patch().add_module(TYPE)
        assert [(p.name, p.signal_kind) for p in sp.input_ports] == [
            ("in_l", "audio"), ("in_r", "audio"),
            ("pan_cv", "cv"), ("width_cv", "cv"),
        ]
        assert sp.output_ports == []

    def test_device_and_buffer_round_trip_through_json(self):
        patch = Patch()
        patch.add_module(
            TYPE, params={"device": "Focusrite USB", "buffer_size": 256, "pan": -0.4}
        )
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == TYPE)
        assert mod.params["device"] == "Focusrite USB"
        assert mod.params["buffer_size"] == 256
        assert mod.params["pan"] == -0.4

    def test_set_param_buffer_size(self):
        sp = Patch().add_module(TYPE)
        sp.set_param("buffer_size", 1024)
        assert sp.params["buffer_size"] == 1024

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module(TYPE, params={"latency": 0})

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
        b = _backend()
        b.compile(patch)
        assert b._render_module(sp, F, {}, patch) is None


# ----- Stream keying ---------------------------------------------------------


class TestStreamKey:
    def test_buffered_sink_keys_by_its_buffer_size(self):
        b = _backend(block_size=F)               # global 512
        p = Patch()
        s = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 256})
        b.compile(p)
        assert b._stream_key(p.get(s.id)) == ("Cans", 256)

    def test_empty_device_is_master_bus(self):
        b = _backend()
        p = Patch()
        s = p.add_module(TYPE, params={"device": "", "buffer_size": 256})
        b.compile(p)
        assert b._stream_key(p.get(s.id)) is None

    def test_plain_specific_sink_keys_by_global_block(self):
        b = _backend(block_size=128)
        p = Patch()
        s = p.add_module(SPECIFIC, params={"device": "Cans"})
        b.compile(p)
        assert b._stream_key(p.get(s.id)) == ("Cans", 128)

    def test_buffer_size_clamped_low_and_high(self):
        b = _backend()
        lo = Patch().add_module(TYPE, params={"buffer_size": 0, "device": "X"})
        hi = Patch().add_module(TYPE, params={"buffer_size": 10 ** 9, "device": "X"})
        assert b._sink_block_size(lo) == _MIN_SINK_BLOCK
        assert b._sink_block_size(hi) == _MAX_SINK_BLOCK

    def test_buffer_size_coerced_from_float(self):
        b = _backend()
        m = Patch().add_module(TYPE, params={"buffer_size": 256.0, "device": "X"})
        assert b._sink_block_size(m) == 256
        assert isinstance(b._sink_block_size(m), int)

    def test_garbage_buffer_size_falls_back_to_global(self):
        b = _backend(block_size=384)
        m = Patch().add_module(TYPE, params={"buffer_size": "nope", "device": "X"})
        assert b._sink_block_size(m) == 384


# ----- Default device: buffer_size is inert on the master bus ----------------


class TestDefaultDevice:
    @pytest.mark.parametrize("bs", [64, 512, 1024])
    def test_master_bus_bit_identical_whatever_the_buffer(self, bs):
        ref, _ = _render(_build("stereo_speaker_output", "stereo", {"pan": 0.3}))
        got, _ = _render(
            _build(TYPE, "stereo", {"pan": 0.3, "buffer_size": bs})  # device ""
        )
        assert np.array_equal(ref, got)

    def test_empty_device_produces_no_device_blocks(self):
        _, dev = _multi(_build(TYPE, "stereo", {"pan": 0.3, "buffer_size": 128}))
        assert dev == {}


# ----- Routing: (device, buffer_size) keys -----------------------------------


class TestRouting:
    def test_routed_sink_uses_its_buffer_size_key(self):
        p = Patch()
        o = p.add_module("oscillator", params={"amp": 0.4})
        s = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 256})
        p.connect(o.id, "out", s.id, "in_l")
        master, dev = _multi(p)
        assert list(dev) == [("Cans", 256)]
        assert np.max(np.abs(master)) == 0.0
        assert np.max(np.abs(dev[("Cans", 256)])) > 0.0

    def test_two_buffer_sizes_one_device_split(self):
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.4})
        o2 = p.add_module("oscillator", params={"amp": 0.4, "freq": 330.0})
        s1 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 128})
        s2 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 512})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        _, dev = _multi(p)
        assert set(dev) == {("Cans", 128), ("Cans", 512)}

    def test_buffered_and_plain_same_size_share_one_stream(self):
        # buffer_size == global block size -> same key as the plain specific
        # sink -> one shared bus (they sum into it).
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.4})
        o2 = p.add_module("oscillator", params={"amp": 0.4})
        s1 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": F})
        s2 = p.add_module(SPECIFIC, params={"device": "Cans"})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        _, dev = _multi(p, block_size=F)
        assert list(dev) == [("Cans", F)]

    def test_buffered_and_plain_diff_size_split(self):
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.4})
        o2 = p.add_module("oscillator", params={"amp": 0.4})
        s1 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 128})
        s2 = p.add_module(SPECIFIC, params={"device": "Cans"})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        _, dev = _multi(p, block_size=F)     # plain -> ("Cans", 512)
        assert set(dev) == {("Cans", 128), ("Cans", F)}

    def test_device_bus_equals_stereo_speaker_drain(self):
        # The DSP is unchanged: the routed device bus equals the plain stereo
        # speaker's master drain for the same pan/width/gain.
        master, dev = _multi(
            _build(TYPE, "stereo", {"pan": 0.4, "device": "Cans", "buffer_size": 256})
        )
        ref, _ = _render(_build("stereo_speaker_output", "stereo", {"pan": 0.4}))
        assert np.array_equal(dev[("Cans", 256)], ref[:F])
        assert np.max(np.abs(master)) == 0.0

    def test_device_bus_is_clipped(self):
        p = Patch()
        o1 = p.add_module("oscillator", params={"amp": 0.9})
        o2 = p.add_module("oscillator", params={"amp": 0.9})
        s1 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 256, "pan": -1.0})
        s2 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 256, "pan": -1.0})
        p.connect(o1.id, "out", s1.id, "in_l")
        p.connect(o2.id, "out", s2.id, "in_l")
        _, dev = _multi(p)
        blk = dev[("Cans", 256)]
        assert blk.max() <= 1.0 and blk.min() >= -1.0


# ----- Secondary stream opens at the sink's buffer size ----------------------


class TestSecondaryStream:
    """_DeviceOutput.open / .close are stubbed so no real PortAudio device is
    touched; the tests assert the reconcile bookkeeping and the block size each
    stream is opened with."""

    @pytest.fixture
    def rig(self, monkeypatch):
        opened = []
        monkeypatch.setattr(
            _DeviceOutput, "open",
            lambda self: opened.append((self.device, self._block_size)),
        )
        monkeypatch.setattr(_DeviceOutput, "close", lambda self: None)
        p = Patch()
        o = p.add_module("oscillator", params={"amp": 0.4})
        s = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 256})
        p.connect(o.id, "out", s.id, "in_l")
        b = _backend(block_size=F)
        b.compile(p)
        b._running = True             # simulate a running stream (no PortAudio)
        b._sync_device_outputs()      # start-equivalent: opens ("Cans", 256)
        return b, s, opened

    def test_opens_stream_at_sink_buffer_size(self, rig):
        b, s, opened = rig
        assert set(b._device_outputs) == {("Cans", 256)}
        assert opened == [("Cans", 256)]
        assert b._device_outputs[("Cans", 256)]._block_size == 256

    def test_live_buffer_change_rebuilds_only_that_stream(self, rig):
        b, s, opened = rig
        b.set_param(s.id, "buffer_size", 512)
        assert set(b._device_outputs) == {("Cans", 512)}
        assert opened == [("Cans", 256), ("Cans", 512)]

    def test_buffer_change_while_stopped_does_not_reconcile(self, rig):
        b, s, opened = rig
        b._running = False
        opened.clear()
        b.set_param(s.id, "buffer_size", 512)
        assert opened == [] and set(b._device_outputs) == {("Cans", 256)}

    def test_unrelated_param_leaves_stream_alone(self, rig):
        b, s, opened = rig
        opened.clear()
        b.set_param(s.id, "pan", 0.5)
        assert opened == [] and set(b._device_outputs) == {("Cans", 256)}

    @pytest.mark.parametrize("bs", [2048, 4096, 8192])
    def test_extended_sizes_key_and_open_past_global_ceiling(self, rig, bs):
        # The SINK_BUFFER_SIZES extensions: sizes the global slider never
        # offers must still key, open, and size the ring correctly.
        b, s, opened = rig
        b.set_param(s.id, "buffer_size", bs)
        assert set(b._device_outputs) == {("Cans", bs)}
        assert opened[-1] == ("Cans", bs)
        assert b._device_outputs[("Cans", bs)]._capacity == 8 * bs


# ----- Ring telemetry (the node readout's data) -------------------------------


class TestRingTelemetry:
    """(queued, capacity, underruns, drops) accounting on _DeviceOutput,
    exercised without a real PortAudio stream (push/_callback are plain
    methods; telemetry() never touches the stream)."""

    def _blk(self, v, n=F):
        return np.full((n, 2), v, dtype=np.float32)

    def _out(self, n=F):
        return np.zeros((n, 2), dtype=np.float32)

    def test_fresh_ring_reads_empty_and_clean(self):
        d = _DeviceOutput("X", SR, F, max_blocks=4)
        assert d.telemetry() == (0, 4 * F, 0, 0)

    def test_fill_tracks_push_and_pop(self):
        d = _DeviceOutput("X", SR, F, max_blocks=4)
        d.push(self._blk(0.1))
        assert d.telemetry()[0] == F
        d._callback(self._out(F // 2), F // 2, None, None)
        assert d.telemetry()[0] == F // 2

    def test_priming_callbacks_are_not_underruns(self):
        # PortAudio fires callbacks before the first render lands; an empty
        # ring at that point is startup, not trouble.
        d = _DeviceOutput("X", SR, F, max_blocks=4)
        d._callback(self._out(), F, None, None)
        d._callback(self._out(), F, None, None)
        assert d.telemetry() == (0, 4 * F, 0, 0)

    def test_fill_up_gap_at_large_sink_block_is_not_an_underrun(self):
        # Device block 2F fed by F-sample pushes: the first callback lands
        # mid-fill-up (inevitable at the 2048/4096/8192 stops, where one
        # device block takes many main blocks to accumulate). That gap is
        # startup; only starvation after the ring has once filled counts.
        d = _DeviceOutput("X", SR, 2 * F, max_blocks=4)
        d.push(self._blk(0.1))                        # half a device block
        d._callback(self._out(2 * F), 2 * F, None, None)   # zero-padded tail
        assert d.telemetry()[2] == 0                  # ...but not an underrun
        d.push(self._blk(0.2, 2 * F))                 # ring reaches one block
        d._callback(self._out(2 * F), 2 * F, None, None)   # fully served
        d._callback(self._out(2 * F), 2 * F, None, None)   # dry: NOW it counts
        assert d.telemetry()[2] == 1

    def test_starvation_after_priming_counts(self):
        d = _DeviceOutput("X", SR, F, max_blocks=4)
        d.push(self._blk(0.1))                    # fills one device block
        d._callback(self._out(), F, None, None)   # fully served: clean
        assert d.telemetry()[2] == 0
        d._callback(self._out(), F, None, None)   # ring dry: underrun
        assert d.telemetry()[2] == 1

    def test_partial_serve_after_priming_is_an_underrun(self):
        d = _DeviceOutput("X", SR, F, max_blocks=4)
        d.push(self._blk(0.1))                    # prime: one full block
        d._callback(self._out(), F, None, None)
        d.push(self._blk(0.2, F // 2))
        d._callback(self._out(), F, None, None)   # half audio, half pad
        assert d.telemetry()[2] == 1

    def test_overflow_counts_a_drop(self):
        d = _DeviceOutput("X", SR, F, max_blocks=2)      # cap 2F
        d.push(self._blk(0.1)); d.push(self._blk(0.2))   # exactly full: clean
        assert d.telemetry()[3] == 0
        d.push(self._blk(0.3))                           # drop-oldest fires
        assert d.telemetry() == (2 * F, 2 * F, 0, 1)

    def test_oversize_block_counts_a_drop(self):
        # A push bigger than the whole ring keeps only its tail — audio was
        # lost even though no previously-queued sample was overwritten.
        d = _DeviceOutput("X", SR, 64, max_blocks=1)     # cap 64
        d.push(self._blk(0.1, 128))
        assert d.telemetry() == (64, 64, 0, 1)

    def test_close_resets_counters_and_prime(self):
        d = _DeviceOutput("X", SR, F, max_blocks=2)
        d.push(self._blk(0.1))
        d._callback(self._out(), F, None, None)
        d._callback(self._out(), F, None, None)          # underrun
        d.push(self._blk(0.2)); d.push(self._blk(0.3)); d.push(self._blk(0.4))
        assert d.telemetry()[2] > 0 and d.telemetry()[3] > 0
        d.close()
        assert d.telemetry() == (0, 2 * F, 0, 0)
        d._callback(self._out(), F, None, None)          # back to pre-prime
        assert d.telemetry()[2] == 0


# ----- snapshot_sink_buffers (the GUI hook) -----------------------------------


class TestSnapshotSinkBuffers:
    """Module-id-keyed telemetry for the node readouts. _DeviceOutput.open /
    .close are stubbed (no real PortAudio); the rings themselves are real."""

    @pytest.fixture
    def stub_streams(self, monkeypatch):
        monkeypatch.setattr(_DeviceOutput, "open", lambda self: None)
        monkeypatch.setattr(_DeviceOutput, "close", lambda self: None)

    def _routed(self, buffer_size=256, device="Cans"):
        p = Patch()
        o = p.add_module("oscillator", params={"amp": 0.4})
        s = p.add_module(TYPE, params={"device": device, "buffer_size": buffer_size})
        p.connect(o.id, "out", s.id, "in_l")
        b = _backend()
        b.compile(p)
        b._running = True
        b._sync_device_outputs()
        return b, s

    def test_no_patch_or_no_streams_is_empty(self, stub_streams):
        assert _backend().snapshot_sink_buffers() == {}
        b, s = self._routed()
        b._device_outputs = {}          # what stop() leaves behind
        assert b.snapshot_sink_buffers() == {}

    def test_routed_sink_reports_its_ring(self, stub_streams):
        b, s = self._routed(buffer_size=256)
        assert b.snapshot_sink_buffers() == {s.id: (0, 8 * 256, 0, 0)}
        b._device_outputs[("Cans", 256)].push(
            np.zeros((100, 2), dtype=np.float32)
        )
        assert b.snapshot_sink_buffers()[s.id] == (100, 8 * 256, 0, 0)

    def test_master_bus_sink_is_absent(self, stub_streams):
        b, s = self._routed(device="")
        assert b.snapshot_sink_buffers() == {}

    def test_failed_open_sink_is_absent(self, monkeypatch):
        def boom(self):
            raise RuntimeError("no such device")
        monkeypatch.setattr(_DeviceOutput, "open", boom)
        monkeypatch.setattr(_DeviceOutput, "close", lambda self: None)
        b, s = self._routed()
        assert b.snapshot_sink_buffers() == {}

    def test_shared_stream_reports_identical_tuples(self, stub_streams):
        p = Patch()
        o = p.add_module("oscillator", params={"amp": 0.4})
        s1 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 256})
        s2 = p.add_module(TYPE, params={"device": "Cans", "buffer_size": 256})
        p.connect(o.id, "out", s1.id, "in_l")
        p.connect(o.id, "out", s2.id, "in_l")
        b = _backend()
        b.compile(p)
        b._running = True
        b._sync_device_outputs()
        snap = b.snapshot_sink_buffers()
        assert set(snap) == {s1.id, s2.id}
        assert snap[s1.id] == snap[s2.id]

    def test_plain_specific_sink_is_included(self, stub_streams):
        # The hook covers every routed speaker, so a future readout on the
        # plain specific sink is a UI-only change.
        p = Patch()
        o = p.add_module("oscillator", params={"amp": 0.4})
        s = p.add_module(SPECIFIC, params={"device": "Cans"})
        p.connect(o.id, "out", s.id, "in_l")
        b = _backend()
        b.compile(p)
        b._running = True
        b._sync_device_outputs()
        assert set(b.snapshot_sink_buffers()) == {s.id}
