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

2026-09-14 love pass: ``inversion`` sends the n lowest sounding notes
up an octave with slot identity kept (works with spread, with a
three-voice preset, and 0 is bit-exact); ``changed`` pulses ~2 ms on a
root step under a held gate, on an interval-set edit, never on a fresh
press, a glide, or with the gate low, and carries across a block join;
``retrig`` re-strums the rows (one-sample drop, staggered re-rise) on
that same event and is off by default (rows array_equal to the old
render).

Drives the renderer directly with hand-fed buffers (the clockwork-test
harness pattern).
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.chord import (
    CHORD_ENABLE_KEYS,
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
    assert [p.name for p in m.output_ports] == ["pitch_cv", "gate", "changed"]
    assert m.params["preset"] == "major"
    assert m.params["inversion"] == 0
    assert m.params["retrig"] is False
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


# ----- 2026-09-14: inversion / changed / retrig ------------------------------


def _hold(frames, on=0, off=None):
    g = np.zeros(frames, dtype=np.float32)
    g[on:(frames if off is None else off)] = 1.0
    return g


def _pulses(x):
    hi = np.asarray(x) > 0.5
    return sorted(([0] if hi[0] else []) + (np.flatnonzero(hi[1:] & ~hi[:-1]) + 1).tolist())


@pytest.mark.parametrize("inv,expected", [
    (0, [0, 4, 7, 12]),
    (1, [12, 4, 7, 12]),     # E in the bass; root doubled up top
    (2, [12, 16, 7, 12]),    # G in the bass
    (3, [12, 16, 19, 12]),   # root position an octave up
])
def test_inversion_sends_the_lowest_notes_up_an_octave(inv, expected):
    step = _driver({"inversion": inv})
    out = step(pitch=np.zeros(8, np.float32), gate=_hold(8))
    assert _rows_st(out, 3) == expected
    # Every enabled row still gates -- slot identity is kept.
    assert out["gate"][:, 3].tolist() == [1.0, 1.0, 1.0, 1.0]


def test_inversion_skips_disabled_slots_and_follows_spread():
    step = _driver({"preset": "5", "inversion": 1})          # 0/7/12/off
    out = step(pitch=np.zeros(8, np.float32), gate=_hold(8))
    assert _rows_st(out, 3) == [12, 7, 12, 0]
    assert out["gate"][3, 3] == 0.0
    step = _driver({"spread": True, "inversion": 1})           # 0/16/-5/12 -> slot 3 lowest
    out = step(pitch=np.zeros(8, np.float32), gate=_hold(8))
    assert _rows_st(out, 3) == [0, 16, 7, 12]
    step = _driver({"inversion": 9})                           # clamps to 3
    out = step(pitch=np.zeros(8, np.float32), gate=_hold(8))
    assert _rows_st(out, 3) == [12, 16, 19, 12]


def test_defaults_render_the_old_rows_bit_exact():
    rng = np.random.RandomState(4)
    root = np.repeat(rng.randint(-5, 7, 8), 40).astype(np.float32) / 12.0
    gate = np.zeros(320, np.float32)
    gate[10:150] = 1.0
    gate[160:300] = 1.0
    plain = _driver({"preset": "m7", "strum": 12.0})
    extra = _driver({"preset": "m7", "strum": 12.0, "inversion": 0, "retrig": False})
    for k in range(5):
        sl = slice(k * 64, (k + 1) * 64)
        a = plain(pitch=root[sl], gate=gate[sl])
        b = extra(pitch=root[sl], gate=gate[sl])
        assert np.array_equal(a["pitch_cv"], b["pitch_cv"])
        assert np.array_equal(a["gate"], b["gate"])
        assert "changed" in a and a["changed"].shape == (64,)


def test_changed_pulses_on_a_root_step_under_a_held_gate():
    step = _driver()
    root = np.zeros(64, np.float32)
    root[30:] = 2.0 / 12.0            # +2 st at sample 30, gate held throughout
    out = step(pitch=root, gate=_hold(64))
    assert _pulses(out["changed"]) == [30]
    assert out["changed"][30:32].tolist() == [1.0, 1.0]   # 2 ms at SR 1000
    assert out["changed"][32] == 0.0
    # ...and the gate rows did NOT move (retrig off).
    assert np.all(out["gate"][:, 1:] == 1.0)


def test_changed_ignores_fresh_presses_glides_and_silence():
    step = _driver()
    # A press with a new root at the same sample: the rise is the trigger.
    root = np.zeros(64, np.float32)
    root[20:] = 5.0 / 12.0
    out = step(pitch=root, gate=_hold(64, on=20))
    assert not out["changed"].any()
    # A glide: 1 octave over 64 samples never jumps half a semitone.
    step = _driver()
    out = step(pitch=np.linspace(0, 1, 64).astype(np.float32), gate=_hold(64))
    assert not out["changed"].any()
    # A jump with the gate low: nothing is sounding, nothing changed.
    step = _driver()
    root = np.zeros(64, np.float32)
    root[30:] = 1.0
    out = step(pitch=root, gate=np.zeros(64, np.float32))
    assert not out["changed"].any()
    # Gate rising at 40 after the jump at 30: still only the press.
    step = _driver()
    out = step(pitch=root, gate=_hold(64, on=40))
    assert not out["changed"].any()


def test_changed_pulses_on_an_interval_set_edit():
    step = _driver()
    out = step(pitch=np.zeros(64, np.float32), gate=_hold(64))
    assert not out["changed"].any()          # first block: nothing to compare
    step.chord.set_param("inversion", 1)
    out = step(pitch=np.zeros(64, np.float32), gate=_hold(64))
    assert _pulses(out["changed"]) == [0]
    step.chord.set_param("preset", "minor")
    out = step(pitch=np.zeros(64, np.float32), gate=_hold(64))
    assert _pulses(out["changed"]) == [0]
    # Same params again: no edit, no pulse.
    out = step(pitch=np.zeros(64, np.float32), gate=_hold(64))
    assert not out["changed"].any()


def test_changed_pulse_carries_across_a_block_join():
    step = _driver(block=8)
    root = np.zeros(16, np.float32)
    root[7:] = 1.0 / 12.0
    a = step(pitch=root[:8], gate=_hold(8))
    b = step(pitch=root[8:], gate=_hold(8))
    chg = np.concatenate([a["changed"], b["changed"]])
    assert _pulses(chg) == [7]
    assert chg[7:9].tolist() == [1.0, 1.0] and chg[9] == 0.0
    # The step itself sits on the join: sample 8 vs the carried sample 7.
    step = _driver(block=8)
    root[:] = 0.0
    root[8:] = 1.0 / 12.0
    a = step(pitch=root[:8], gate=_hold(8))
    b = step(pitch=root[8:], gate=_hold(8))
    assert _pulses(np.concatenate([a["changed"], b["changed"]])) == [8]


def test_retrig_restrums_the_rows_on_changed():
    step = _driver({"retrig": True, "strum": 3.0})   # 3 samples at SR 1000
    root = np.zeros(64, np.float32)
    root[30:] = 2.0 / 12.0
    out = step(pitch=root, gate=_hold(64))
    g = out["gate"]
    # Every row drops for exactly sample 30...
    assert g[:, 29].tolist() == [1.0] * 4
    assert g[:, 30].tolist() == [0.0] * 4
    # ...and re-rises staggered from 31: rows at 31, 34, 37, 40.
    for k, on in enumerate((31, 34, 37, 40)):
        assert g[k, on - 1] == 0.0 and g[k, on] == 1.0, k
    assert np.all(g[:, 41:] == 1.0)
    assert _pulses(out["changed"]) == [30]


def test_retrig_with_strum_zero_still_gives_a_fresh_edge():
    step = _driver({"retrig": True})
    root = np.zeros(64, np.float32)
    root[30:] = 2.0 / 12.0
    out = step(pitch=root, gate=_hold(64))
    g = out["gate"]
    assert g[:, 30].tolist() == [0.0] * 4
    assert g[:, 31].tolist() == [1.0] * 4
    assert np.all(g[:, :30] == 1.0) and np.all(g[:, 31:] == 1.0)


def test_retrig_restrum_survives_a_block_join():
    step = _driver({"retrig": True, "strum": 5.0}, block=8)
    root = np.zeros(24, np.float32)
    root[6:] = 3.0 / 12.0
    outs = [step(pitch=root[i:i + 8], gate=_hold(8)) for i in (0, 8, 16)]
    g = np.concatenate([o["gate"] for o in outs], axis=-1)
    assert g[:, 6].tolist() == [0.0] * 4
    for k, on in enumerate((7, 12, 17, 22)):
        assert g[k, on - 1] == 0.0 and g[k, on] == 1.0, k


def test_changed_drives_a_real_downstream_chain():
    """chord.changed -> burst.trig: the re-strum trigger is a gate jack
    another module can consume, through compile + render."""
    patch = Patch()
    seq = patch.add_module("constant")
    clk = patch.add_module("clock")
    ch = patch.add_module("chord")
    bst = patch.add_module("burst")
    patch.connect(seq.id, "out", ch.id, "pitch_cv")
    patch.connect(clk.id, "out", ch.id, "gate")
    patch.connect(ch.id, "changed", bst.id, "trigger")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    for _ in range(3):
        b.render_block(64)
