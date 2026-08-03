"""Chord — mono pitch + gate → four-voice (4, F) poly chord.

Pins the contract: every preset row lands its exact semitone offsets
(``5`` runs three voices), custom reads the interval/enable slots
(disabled rows gate-low but the shape stays (4, F)), spread opens the
voicing (0, +12, −12, 0), pitch tracks the input continuously (glides
chord along), gates mirror the input exactly at strum 0, strum staggers
onsets sample-accurately over enabled rows (falls drop every row
together and cancel unfired onsets, block joins preserved), unpatched
inputs degrade cleanly (no pitch → intervals from 0 V; no gate →
silence), and the rows drive a real voice-aware downstream chain.

Drives the renderer directly with hand-fed buffers (the clockwork-test
harness pattern).
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.chord import (
    CHORD_ENABLE_KEYS,
    CHORD_INTERVAL_KEYS,
    CHORD_PRESETS,
    CHORD_SPREAD_OFFSETS,
)

SR = 1000


def _driver(params=None, block=64):
    """Backend + constant/clock → chord patch; feed blocks via .step()."""
    patch = Patch()
    ch = patch.add_module("chord", params=params or {})
    src = patch.add_module("constant")
    gsrc = patch.add_module("clock")
    patch.connect(src.id, "out", ch.id, "pitch_cv")
    patch.connect(gsrc.id, "out", ch.id, "gate")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(pitch=None, gate=None, frames=None):
        for a in (pitch, gate):
            if a is not None:
                frames = np.asarray(a).shape[-1]
                break
        buffers = {}
        if pitch is not None:
            buffers[(src.id, "out")] = np.asarray(pitch, dtype=np.float32)
        if gate is not None:
            buffers[(gsrc.id, "out")] = np.asarray(gate, dtype=np.float32)
        return b._render_chord(patch.get(ch.id), frames, buffers, patch)

    step.chord = ch
    step.patch = patch
    step.backend = b
    return step


def _rows_st(out, n=0):
    """Pitch rows at sample n, in semitones."""
    return [round(float(v) * 12.0, 3) for v in out["pitch_cv"][:, n]]


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["chord"]
    assert cls is get_module_type("chord")
    assert cls.CATEGORY == "CV & Utilities"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["pitch_cv", "gate"]
    assert [p.name for p in m.output_ports] == ["pitch_cv", "gate"]
    assert m.params["preset"] == "major"
    assert m.params["strum"] == 0.0
    for key in CHORD_ENABLE_KEYS:
        assert m.params[key] is True


def test_serialization_round_trip():
    cls = all_module_types()["chord"]
    m = cls(3, params={"preset": "custom", "interval_2": -5.0, "spread": True})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


def test_preset_table_shape():
    assert "custom" in CHORD_PRESETS
    for name, row in CHORD_PRESETS.items():
        assert len(row) == 4, name
    assert len(CHORD_SPREAD_OFFSETS) == 4


# ----- pitch rows ------------------------------------------------------------


@pytest.mark.parametrize(
    "preset,expected",
    [
        ("major", [0, 4, 7, 12]),
        ("minor", [0, 3, 7, 12]),
        ("m7", [0, 3, 7, 10]),
        ("sus4", [0, 5, 7, 12]),
        ("dim", [0, 3, 6, 9]),
    ],
)
def test_preset_rows_land_exact_offsets(preset, expected):
    step = _driver({"preset": preset})
    F = 40
    root = np.full(F, 2 / 12.0, dtype=np.float32)  # D
    out = step(pitch=root, gate=np.ones(F, dtype=np.float32))
    assert out["pitch_cv"].shape == (4, F)
    assert _rows_st(out) == [e + 2 for e in expected]
    assert np.all(out["gate"] == 1.0)


def test_power_chord_runs_three_voices():
    step = _driver({"preset": "5"})
    F = 40
    out = step(
        pitch=np.zeros(F, dtype=np.float32),
        gate=np.ones(F, dtype=np.float32),
    )
    assert _rows_st(out)[:3] == [0, 7, 12]
    assert np.all(out["gate"][:3] == 1.0)
    assert np.all(out["gate"][3] == 0.0)  # slot 4 disabled, shape kept


def test_custom_reads_slots_and_enables():
    step = _driver({
        "preset": "custom",
        "interval_1": 0.0, "interval_2": -12.0,
        "interval_3": 7.0, "interval_4": 16.0,
        "enable_3": False,
    })
    F = 40
    out = step(
        pitch=np.zeros(F, dtype=np.float32),
        gate=np.ones(F, dtype=np.float32),
    )
    assert _rows_st(out) == [0, -12, 7, 16]  # pitch still tracks when off
    assert np.all(out["gate"][2] == 0.0)  # …but the row never gates
    assert np.all(out["gate"][[0, 1, 3]] == 1.0)


def test_spread_opens_the_voicing():
    step = _driver({"preset": "major", "spread": True})
    F = 20
    out = step(
        pitch=np.zeros(F, dtype=np.float32),
        gate=np.ones(F, dtype=np.float32),
    )
    assert _rows_st(out) == [0, 16, -5, 12]


def test_pitch_tracks_the_input_continuously():
    step = _driver({"preset": "major"})
    F = 100
    root = np.linspace(0.0, 1.0, F, dtype=np.float32)  # a glide up an octave
    out = step(pitch=root, gate=np.ones(F, dtype=np.float32))
    for k, off in enumerate((0.0, 4.0, 7.0, 12.0)):
        np.testing.assert_allclose(
            np.asarray(out["pitch_cv"][k]) * 12.0,
            root.astype(np.float64) * 12.0 + off,
            atol=1e-3,
        )


def test_unpatched_pitch_plays_intervals_from_zero():
    step = _driver({"preset": "minor"})
    F = 40
    out = step(gate=np.ones(F, dtype=np.float32), frames=F)
    assert _rows_st(out) == [0, 3, 7, 12]


def test_unpatched_gate_is_silent():
    step = _driver({"preset": "major"})
    F = 40
    out = step(pitch=np.zeros(F, dtype=np.float32), frames=F)
    assert not np.any(out["gate"] > 0.0)


# ----- gates / strum ---------------------------------------------------------


def test_gates_mirror_input_exactly_at_strum_zero():
    step = _driver({"preset": "major"})
    F = 100
    g = np.zeros(F, dtype=np.float32)
    g[10:40] = 1.0
    g[60:80] = 1.0
    out = step(pitch=np.zeros(F, dtype=np.float32), gate=g)
    for k in range(4):
        assert np.array_equal(out["gate"][k], (g > 0.5).astype(np.float32))


def test_strum_staggers_onsets_and_falls_together():
    step = _driver({"preset": "major", "strum": 15.0})  # 15 samples at SR=1000
    F = 200
    g = np.zeros(F, dtype=np.float32)
    g[20:150] = 1.0
    out = step(pitch=np.zeros(F, dtype=np.float32), gate=g)
    onsets = [int(np.argmax(out["gate"][k] > 0.5)) for k in range(4)]
    assert onsets == [20, 35, 50, 65]
    for k in range(4):  # every row falls with the input
        assert out["gate"][k, 149] == 1.0
        assert out["gate"][k, 150] == 0.0


def test_strum_counts_enabled_rows_only():
    step = _driver({"preset": "5", "strum": 15.0})
    F = 100
    g = np.zeros(F, dtype=np.float32)
    g[10:] = 1.0
    out = step(pitch=np.zeros(F, dtype=np.float32), gate=g)
    onsets = [int(np.argmax(out["gate"][k] > 0.5)) for k in range(3)]
    assert onsets == [10, 25, 40]
    assert not np.any(out["gate"][3] > 0.0)


def test_fall_before_a_scheduled_onset_cancels_it():
    step = _driver({"preset": "major", "strum": 30.0})
    F = 200
    g = np.zeros(F, dtype=np.float32)
    g[10:50] = 1.0  # released 40 in — rows 3 & 4 (onsets 70/100) never fire
    out = step(pitch=np.zeros(F, dtype=np.float32), gate=g)
    assert np.any(out["gate"][0] > 0.0)
    assert np.any(out["gate"][1] > 0.0)
    assert not np.any(out["gate"][2] > 0.0)
    assert not np.any(out["gate"][3] > 0.0)


def test_strum_carries_across_block_joins():
    F = 200
    g = np.zeros(F, dtype=np.float32)
    g[5:180] = 1.0
    root = np.zeros(F, dtype=np.float32)

    def render(block):
        step = _driver({"preset": "major", "strum": 40.0}, block=block)
        if block >= F:
            return np.asarray(step(pitch=root, gate=g)["gate"])
        parts = [
            np.asarray(step(pitch=root[i : i + block], gate=g[i : i + block])["gate"])
            for i in range(0, F, block)
        ]
        return np.concatenate(parts, axis=1)

    assert np.array_equal(render(F), render(16))


# ----- integration -----------------------------------------------------------


def test_rows_drive_a_voice_aware_downstream_chain():
    """sequencer-style mono line → chord → per-voice osc/adsr/vca renders."""
    patch = Patch()
    root = patch.add_module("constant", params={"value": 0.25})
    clk = patch.add_module("clock", params={"bpm": 240.0})
    ch = patch.add_module("chord", params={"preset": "m7", "strum": 5.0})
    osc = patch.add_module("oscillator")
    env = patch.add_module("adsr")
    vca = patch.add_module("vca", params={"gain": 0.25})
    out = patch.add_module("speaker_output")
    patch.connect(root.id, "out", ch.id, "pitch_cv")
    patch.connect(clk.id, "out", ch.id, "gate")
    patch.connect(ch.id, "pitch_cv", osc.id, "freq_cv")
    patch.connect(ch.id, "gate", env.id, "gate")
    patch.connect(osc.id, "out", vca.id, "audio")
    patch.connect(env.id, "cv", vca.id, "cv")
    patch.connect(vca.id, "out", out.id, "in")
    b = NumpyBackend(sample_rate=44100, block_size=256)
    b.compile(patch)
    for _ in range(8):
        mix, _dev = b.render_block_multi(256)
        assert mix is not None and np.all(np.isfinite(mix))
