"""Tests for the CVGates module — computer keys as a bank of enveloped CV gates.

CVGates gives each of the 17 home-row keys its own CV output that idles at 0
and, while held, runs a shared ADSR toward 1. It is the amplitude/trigger
counterpart to CVKeyboard (which puts out 1V/oct pitch). These tests cover
the model (ports, key→index mapping), the per-key envelope shape and
independence, the render dispatch, and an end-to-end amplitude-control patch.
"""
from __future__ import annotations

import math

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.cv_gates import (
    CVGates,
    KEY_BASE_NOTE,
    KEY_CV_NAMES,
    NUM_KEYS,
)

SR = 44100


def _backend(patch) -> NumpyBackend:
    b = NumpyBackend()
    b.compile(patch)
    return b


def _render_n(backend, module, frames, n):
    """Render n consecutive blocks of the gate bank, return the last dict."""
    last = None
    for _ in range(n):
        last = backend._render_cv_gates(module, frames)
    return last


def _blocks_for(seconds, frames):
    return math.ceil(seconds * SR / frames) + 2


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class TestCVGatesModel:
    def test_register_and_defaults(self):
        patch = Patch()
        g = patch.add_module("cv_gates")
        assert isinstance(g, CVGates)
        assert g.TYPE == "cv_gates"
        assert g.params == {
            "attack": 0.01,
            "decay": 0.10,
            "sustain": 0.80,
            "release": 0.30,
        }

    def test_accepts_computer_keys_marker(self):
        # The UI routes physical keystrokes by this flag, not by type.
        assert CVGates.ACCEPTS_COMPUTER_KEYS is True

    def test_no_inputs_only_cv_outputs(self):
        patch = Patch()
        g = patch.add_module("cv_gates")
        assert g.input_ports == []
        assert len(g.output_ports) == NUM_KEYS == 17
        assert all(p.signal_kind == "cv" for p in g.output_ports)
        assert all(p.direction == "out" for p in g.output_ports)

    def test_output_names_span_c4_to_e5(self):
        patch = Patch()
        g = patch.add_module("cv_gates")
        names = [p.name for p in g.output_ports]
        assert names == list(KEY_CV_NAMES)
        assert names[0] == "c4"      # MIDI 60, index 0
        assert names[12] == "c5"     # one octave up
        assert names[-1] == "e5"     # MIDI 76, index 16

    def test_note_on_maps_midi_to_key_index(self):
        g = CVGates(1)
        g.note_on(KEY_BASE_NOTE)        # 60 -> idx 0 (c4)
        g.note_on(KEY_BASE_NOTE + 16)   # 76 -> idx 16 (e5)
        down = g.snapshot_down()
        assert [i for i, b in enumerate(down) if b] == [0, 16]

    def test_out_of_range_notes_ignored(self):
        g = CVGates(1)
        g.note_on(KEY_BASE_NOTE - 1)    # 59, below the span
        g.note_on(KEY_BASE_NOTE + 17)   # 77, above the span
        assert not any(g.snapshot_down())

    def test_note_off_clears_only_that_key(self):
        g = CVGates(1)
        g.note_on(60)
        g.note_on(64)
        g.note_off(60)
        assert [i for i, b in enumerate(g.snapshot_down()) if b] == [4]

    def test_all_notes_off(self):
        g = CVGates(1)
        for n in (60, 64, 67):
            g.note_on(n)
        g.all_notes_off()
        assert not any(g.snapshot_down())

    def test_snapshot_is_a_copy(self):
        g = CVGates(1)
        g.note_on(60)
        snap = g.snapshot_down()
        snap[0] = False  # mutating the snapshot must not affect the module
        assert g.snapshot_down()[0] is True


# ---------------------------------------------------------------------------
# Envelope behaviour
# ---------------------------------------------------------------------------

class TestCVGatesEnvelope:
    def test_idle_outputs_all_zero(self):
        patch = Patch()
        g = patch.add_module("cv_gates")
        b = _backend(patch)
        out = b._render_cv_gates(g, 256)
        assert set(out.keys()) == set(KEY_CV_NAMES)
        for name in KEY_CV_NAMES:
            assert np.array_equal(out[name], np.zeros(256, dtype=np.float32))

    def test_attack_reaches_unity(self):
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.02, "decay": 0.2, "sustain": 0.5, "release": 0.05},
        )
        b = _backend(patch)
        g.note_on(60)  # c4
        peak = 0.0
        for _ in range(_blocks_for(0.05, 256)):
            peak = max(peak, float(b._render_cv_gates(g, 256)["c4"].max()))
        assert peak == 1.0

    def test_settles_to_sustain(self):
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.005, "decay": 0.02, "sustain": 0.6, "release": 0.05},
        )
        b = _backend(patch)
        g.note_on(60)
        last = _render_n(b, g, 256, _blocks_for(0.05, 256))
        assert abs(float(last["c4"][-1]) - 0.6) < 1e-4

    def test_sustain_full_has_no_decay(self):
        # sustain == 1.0: decay_step is 0, level holds at 1 with no dip.
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.005, "decay": 0.1, "sustain": 1.0, "release": 0.05},
        )
        b = _backend(patch)
        g.note_on(60)
        last = _render_n(b, g, 256, _blocks_for(0.05, 256))
        assert abs(float(last["c4"][-1]) - 1.0) < 1e-6

    def test_release_falls_to_zero(self):
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.005, "decay": 0.01, "sustain": 0.7, "release": 0.02},
        )
        b = _backend(patch)
        g.note_on(60)
        _render_n(b, g, 256, _blocks_for(0.03, 256))
        g.note_off(60)
        last = _render_n(b, g, 256, _blocks_for(0.03, 256))
        assert float(last["c4"][-1]) == 0.0

    def test_retrigger_attacks_from_current_level(self):
        # Re-pressing mid-release should resume attack from the partially
        # decayed level, never snapping to 0 first (no click).
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.5, "decay": 0.1, "sustain": 0.5, "release": 0.5},
        )
        b = _backend(patch)
        g.note_on(60)
        _render_n(b, g, 256, 3)  # part-way up the slow attack
        before = float(b._render_cv_gates(g, 256)["c4"][-1])
        g.note_off(60)
        _render_n(b, g, 256, 2)  # part-way down the slow release
        g.note_on(60)            # retrigger
        after_first = float(b._render_cv_gates(g, 256)["c4"][0])
        assert 0.0 < after_first  # did not reset to 0
        assert before > 0.0

    def test_keys_are_independent(self):
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.005, "decay": 0.01, "sustain": 0.6, "release": 0.05},
        )
        b = _backend(patch)
        g.note_on(60)   # c4 only
        last = _render_n(b, g, 256, _blocks_for(0.03, 256))
        assert abs(float(last["c4"][-1]) - 0.6) < 1e-4
        assert float(last["e4"].max()) == 0.0
        assert float(last["g4"].max()) == 0.0

    def test_idle_key_buffers_are_distinct_objects(self):
        # Short-circuited idle keys must each get their own zero buffer, so a
        # downstream consumer writing in place can't corrupt a sibling jack.
        patch = Patch()
        g = patch.add_module("cv_gates")
        b = _backend(patch)
        out = b._render_cv_gates(g, 64)
        assert out["c4"] is not out["e4"]

    def test_instant_attack(self):
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.0, "decay": 0.0, "sustain": 1.0, "release": 0.0},
        )
        b = _backend(patch)
        g.note_on(60)
        out = b._render_cv_gates(g, 64)
        assert float(out["c4"][0]) == 1.0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestCVGatesDispatch:
    def test_render_module_routes_cv_gates(self):
        patch = Patch()
        g = patch.add_module("cv_gates")
        b = _backend(patch)
        result = b._render_module(g, 128, {}, patch)
        assert isinstance(result, dict)
        assert set(result.keys()) == set(KEY_CV_NAMES)
        for buf in result.values():
            assert buf.shape == (128,)
            assert buf.dtype == np.float32


# ---------------------------------------------------------------------------
# Integration — the amplitude-control use case
# ---------------------------------------------------------------------------

class TestCVGatesIntegration:
    def test_key_drives_oscillator_amplitude(self):
        # cv_gates.c4 -> oscillator.amp_cv -> speaker. Idle = silence;
        # holding the key opens the VCA-like amp and audio appears; releasing
        # and letting the envelope finish returns to silence.
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.002, "decay": 0.01, "sustain": 0.8, "release": 0.01},
        )
        osc = patch.add_module("oscillator", params={"freq": 220.0, "amp": 0.5})
        spk = patch.add_module("speaker_output")
        patch.connect(g.id, "c4", osc.id, "amp_cv")
        patch.connect(osc.id, "out", spk.id, "in")
        b = _backend(patch)

        # Idle: amp_cv is 0, so the speaker is silent.
        silent = b.render_block(256)
        assert float(np.max(np.abs(silent))) < 1e-6

        # Press: envelope opens, audio appears.
        g.note_on(60)
        loud = 0.0
        for _ in range(_blocks_for(0.03, 256)):
            loud = max(loud, float(np.max(np.abs(b.render_block(256)))))
        assert loud > 0.1

        # Release and settle: back to silence.
        g.note_off(60)
        tail = None
        for _ in range(_blocks_for(0.03, 256)):
            tail = b.render_block(256)
        assert float(np.max(np.abs(tail))) < 1e-3

    def test_one_key_fans_out_to_three_oscillators(self):
        # The headline workflow: a single key's CV drives the amp_cv of three
        # oscillators at once. The patch model allows many cables from one
        # output port, so this is just three connects off the same jack.
        patch = Patch()
        g = patch.add_module(
            "cv_gates",
            params={"attack": 0.002, "decay": 0.01, "sustain": 0.9, "release": 0.01},
        )
        # Three oscillators summed through a combiner into the speaker (the
        # speaker takes one cable; the fan-out under test is on the CV side).
        comb = patch.add_module("combiner")
        spk = patch.add_module("speaker_output")
        patch.connect(comb.id, "out", spk.id, "in")
        for idx, f in enumerate((110.0, 220.0, 440.0), start=1):
            o = patch.add_module("oscillator", params={"freq": f, "amp": 0.3})
            patch.connect(g.id, "c4", o.id, "amp_cv")  # same jack, 3 cables
            patch.connect(o.id, "out", comb.id, f"in{idx}")
        b = _backend(patch)

        g.note_on(60)
        loud = 0.0
        for _ in range(_blocks_for(0.03, 256)):
            loud = max(loud, float(np.max(np.abs(b.render_block(256)))))
        assert loud > 0.1  # all three voices triggered by the one keystroke


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestCVGatesSerialization:
    def test_round_trip(self, tmp_path):
        from pysynthrack.io_patch import load_patch, save_patch

        patch = Patch()
        patch.add_module(
            "cv_gates",
            params={"attack": 0.05, "decay": 0.2, "sustain": 0.4, "release": 0.6},
        )
        path = tmp_path / "gates.json"
        save_patch(patch, str(path))
        loaded = load_patch(str(path))
        g = next(m for m in loaded if m.TYPE == "cv_gates")
        assert g.params == {
            "attack": 0.05,
            "decay": 0.2,
            "sustain": 0.4,
            "release": 0.6,
        }
        assert [p.name for p in g.output_ports] == list(KEY_CV_NAMES)
