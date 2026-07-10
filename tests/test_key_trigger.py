"""Tests for the KeyTrigger module (one bound key → gate / trigger / latch).

All headless: the module's raw-key transport (``raw_key_down`` /
``raw_key_up``) is driven directly and the three output modes are exercised
through ``NumpyBackend._render_key_trigger``, so no window or dpg is needed.
The UI's raw-key routing + Learn button live in ``ui/app.py`` and get their
own coverage / real-window eyeball.
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import all_module_types
from pysynthrack.modules.key_trigger import KEY_TRIGGER_MODES

SR = 48000


def _be_patch(**params):
    be = NumpyBackend(sample_rate=SR, block_size=512)
    patch = Patch()
    kt = patch.add_module("key_trigger", params=params)
    be.compile(patch)
    return be, patch, kt


def _render(be, kt, frames=512):
    return be._render_module(kt, frames, {}, None)


class TestModuleShape:
    def test_registered_source_one_gate_out_no_inputs(self):
        cls = all_module_types()["key_trigger"]
        assert cls.CATEGORY == "Sources"
        assert cls.INPUT_PORTS == []
        outs = cls.OUTPUT_PORTS
        assert len(outs) == 1
        assert outs[0].name == "out"
        assert outs[0].signal_kind == "gate"
        assert getattr(cls, "ACCEPTS_RAW_KEYS", False) is True

    def test_default_params_unbound_gate(self):
        cls = all_module_types()["key_trigger"]
        kt = cls(1)
        assert kt.params["key"] == ""
        assert kt.params["mode"] == "gate"

    def test_modes_vocabulary(self):
        assert KEY_TRIGGER_MODES == ("gate", "trigger", "latch")


class TestTransportFiltersByBoundKey:
    def test_unbound_node_ignores_every_key(self):
        _, _, kt = _be_patch(key="", mode="gate")
        kt.raw_key_down("Q")
        assert kt.snapshot() == (False, 0)

    def test_only_the_bound_key_registers(self):
        _, _, kt = _be_patch(key="Q", mode="gate")
        kt.raw_key_down("W")  # a different key
        assert kt.snapshot() == (False, 0)
        kt.raw_key_down("Q")  # the bound key
        assert kt.snapshot() == (True, 1)

    def test_snapshot_consumes_the_press_edge_but_keeps_held(self):
        _, _, kt = _be_patch(key="Q")
        kt.raw_key_down("Q")
        assert kt.snapshot() == (True, 1)
        assert kt.snapshot() == (True, 0)  # still held, edge already consumed

    def test_all_notes_off_releases_and_clears_edges(self):
        _, _, kt = _be_patch(key="Q")
        kt.raw_key_down("Q")
        kt.all_notes_off()
        assert kt.snapshot() == (False, 0)

    def test_rebinding_key_param_changes_what_matches(self):
        _, _, kt = _be_patch(key="Q")
        kt.params["key"] = "5"          # e.g. a Learn re-bind
        kt.raw_key_down("Q")            # old key no longer matches
        assert kt.snapshot() == (False, 0)
        kt.raw_key_down("5")
        assert kt.snapshot() == (True, 1)


class TestGateMode:
    def test_high_while_held_low_after_release(self):
        be, _, kt = _be_patch(key="Q", mode="gate")
        assert np.all(_render(be, kt) == 0.0)  # idle
        kt.raw_key_down("Q")
        assert np.all(_render(be, kt) == 1.0)  # held
        assert np.all(_render(be, kt) == 1.0)  # still held, no new event
        kt.raw_key_up("Q")
        assert np.all(_render(be, kt) == 0.0)  # released

    def test_output_is_mono_frames_shaped(self):
        be, _, kt = _be_patch(key="Q", mode="gate")
        out = _render(be, kt, frames=333)
        assert out.shape == (333,)
        assert out.dtype == np.float32


class TestLatchMode:
    def test_press_toggles_and_survives_release(self):
        be, _, kt = _be_patch(key="Q", mode="latch")
        assert np.all(_render(be, kt) == 0.0)
        kt.raw_key_down("Q")
        kt.raw_key_up("Q")                     # released...
        assert np.all(_render(be, kt) == 1.0)  # ...latch stays on
        kt.raw_key_down("Q")                   # second press
        kt.raw_key_up("Q")
        assert np.all(_render(be, kt) == 0.0)  # toggled back off

    def test_even_presses_in_one_block_net_no_change(self):
        be, _, kt = _be_patch(key="Q", mode="latch")
        kt.raw_key_down("Q"); kt.raw_key_up("Q")
        kt.raw_key_down("Q"); kt.raw_key_up("Q")  # two presses before a render
        assert np.all(_render(be, kt) == 0.0)     # even count -> unchanged
        kt.raw_key_down("Q")                       # one more -> on
        assert np.all(_render(be, kt) == 1.0)


class TestTriggerMode:
    def test_pulse_on_press_then_returns_low(self):
        be, _, kt = _be_patch(key="Q", mode="trigger")
        assert np.all(_render(be, kt) == 0.0)
        kt.raw_key_down("Q")
        blk = _render(be, kt, frames=512)
        assert blk[0] == 1.0        # pulse starts at the block head
        assert blk[-1] == 0.0       # ~5 ms (240 @ 48k) << 512, so it ends
        assert np.all(_render(be, kt) == 0.0)  # a held key does not re-pulse

    def test_pulse_length_is_block_size_independent(self):
        # A pulse that spans several small blocks still totals ~5 ms of highs.
        be, _, kt = _be_patch(key="Q", mode="trigger")
        kt.raw_key_down("Q")
        got = np.concatenate([_render(be, kt, frames=64) for _ in range(8)])
        high = int(np.count_nonzero(got))
        expected = max(1, round(SR * 0.005))
        assert abs(high - expected) <= 1

    def test_each_fresh_press_refires(self):
        be, _, kt = _be_patch(key="Q", mode="trigger")
        kt.raw_key_down("Q")
        assert _render(be, kt)[0] == 1.0
        assert np.all(_render(be, kt) == 0.0)  # drained
        kt.raw_key_up("Q")
        kt.raw_key_down("Q")                    # a new press
        assert _render(be, kt)[0] == 1.0        # pulses again
