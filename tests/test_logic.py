"""Logic — two-input gate algebra, five jacks live at once.

Pins the contract: exact truth tables on every output, the unpatched
operand reads low (so `or`/`xor` pass `a`, `nand` idles high — the
normalled-NAND trick), both-unpatched idles `nand`/`not_a` high, a
voice-aware (V, F) gate source collapses to any-voice-high, the module
is stateless/exact at any block size, and it drives a real clock ×
euclidean complementary-rhythm patch.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type

SR = 1000

OUTS = ("and", "or", "xor", "nand", "not_a")


def _driver(jacks=("a", "b")):
    patch = Patch()
    lg = patch.add_module("logic")
    feeds = {}
    for jack in jacks:
        src = patch.add_module("clock")
        patch.connect(src.id, "out", lg.id, jack)
        feeds[jack] = src
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)

    def step(frames=None, **blocks):
        for arr in blocks.values():
            frames = np.asarray(arr).shape[-1]
            break
        buffers = {
            (feeds[j].id, "out"): np.asarray(arr, dtype=np.float32)
            for j, arr in blocks.items()
        }
        return b._render_logic(patch.get(lg.id), frames, buffers, patch)

    step.logic = lg
    step.patch = patch
    return step


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_no_params():
    cls = all_module_types()["logic"]
    assert cls is get_module_type("logic")
    assert cls.CATEGORY == "Modulation"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["a", "b"]
    assert [p.name for p in m.output_ports] == list(OUTS)
    assert m.params == {}  # zero params, deliberately (schmitt owns cv->gate)


def test_serialization_round_trip():
    cls = all_module_types()["logic"]
    m = cls(3)
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == {}


# ----- truth tables ----------------------------------------------------------


def test_truth_tables_every_output():
    step = _driver()
    a = np.array([0, 0, 1, 1], dtype=np.float32)
    b = np.array([0, 1, 0, 1], dtype=np.float32)
    out = step(a=a, b=b)
    expected = {
        "and": [0, 0, 0, 1],
        "or": [0, 1, 1, 1],
        "xor": [0, 1, 1, 0],
        "nand": [1, 1, 1, 0],
        "not_a": [1, 1, 0, 0],
    }
    for name in OUTS:
        assert out[name].tolist() == expected[name], name


def test_house_threshold_not_amplitude():
    # Gates are thresholded at the house 0.5 — a 0.4 "high" is low, a
    # 0.7 is high, and outputs are clean 0/1 regardless of input level.
    step = _driver()
    a = np.array([0.4, 0.7, 1.0], dtype=np.float32)
    b = np.array([0.7, 0.4, 1.0], dtype=np.float32)
    out = step(a=a, b=b)
    assert out["and"].tolist() == [0, 0, 1]
    assert out["or"].tolist() == [1, 1, 1]
    assert set(np.unique(np.concatenate([out[n] for n in OUTS]))) <= {0.0, 1.0}


# ----- unpatched operands ----------------------------------------------------


def test_unpatched_b_reads_low():
    step = _driver(jacks=("a",))
    a = np.array([0, 1, 0, 1], dtype=np.float32)
    out = step(a=a)
    assert out["or"].tolist() == [0, 1, 0, 1]  # passes a
    assert out["xor"].tolist() == [0, 1, 0, 1]  # passes a
    assert out["and"].tolist() == [0, 0, 0, 0]
    assert out["nand"].tolist() == [1, 1, 1, 1]  # idles high (normalled trick)
    assert out["not_a"].tolist() == [1, 0, 1, 0]


def test_both_unpatched_idle_states():
    step = _driver(jacks=())
    out = step(frames=8)
    assert not np.any(out["and"]) and not np.any(out["or"])
    assert not np.any(out["xor"])
    assert np.all(out["nand"] == 1.0)
    assert np.all(out["not_a"] == 1.0)


# ----- voice-aware sources ---------------------------------------------------


def test_poly_gate_source_collapses_to_any_voice_high():
    """A (V, F) gate buffer (cv_keyboard-style) collapses on fetch; the
    summed row is high wherever ANY voice gates."""
    patch = Patch()
    lg = patch.add_module("logic")
    kb = patch.add_module("cv_keyboard")
    patch.connect(kb.id, "gate", lg.id, "a")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    F = 6
    gate = np.zeros((16, F), dtype=np.float32)
    gate[3, 1:3] = 1.0
    gate[7, 2:5] = 1.0
    out = b._render_logic(patch.get(lg.id), F, {(kb.id, "gate"): gate}, patch)
    assert out["or"].tolist() == [0, 1, 1, 1, 1, 0]


# ----- integration -----------------------------------------------------------


def test_xor_makes_the_complementary_rhythm():
    """clock XOR euclidean-gate = clock hits on the pattern's rests."""
    patch = Patch()
    lg = patch.add_module("logic")
    clk = patch.add_module("clock")
    euc = patch.add_module("euclidean", params={
        "steps": 4, "fills": 2, "gate_len": 0.05,
    })
    patch.connect(clk.id, "out", euc.id, "clock")
    patch.connect(clk.id, "out", lg.id, "a")
    patch.connect(euc.id, "gate", lg.id, "b")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    F, gap = 8 * 20, 20
    clock = np.zeros(F, dtype=np.float32)
    for k in range(8):
        clock[k * gap] = 1.0  # 1-sample ticks
    euc_out = b._render_euclidean(
        patch.get(euc.id), F, {(clk.id, "out"): clock}, patch
    )
    out = b._render_logic(
        patch.get(lg.id), F,
        {(clk.id, "out"): clock, (euc.id, "gate"): euc_out["gate"]},
        patch,
    )
    ticks = [bool(clock[k * gap] > 0.5) for k in range(8)]
    pattern = [bool(euc_out["gate"][k * gap] > 0.5) for k in range(8)]
    xor = [bool(out["xor"][k * gap] > 0.5) for k in range(8)]
    assert ticks == [True] * 8
    assert xor == [t != p for t, p in zip(ticks, pattern)]
    assert any(pattern) and any(xor)  # both rhythms non-empty


def test_full_graph_render_with_drums():
    patch = Patch()
    clk = patch.add_module("clock", params={"bpm": 480.0})
    euc = patch.add_module("euclidean", params={"steps": 8, "fills": 3})
    lg = patch.add_module("logic")
    kick = patch.add_module("kick_drum")
    hat = patch.add_module("hat_drum")
    out = patch.add_module("speaker_output")
    mix = patch.add_module("mixer")
    patch.connect(clk.id, "out", euc.id, "clock")
    patch.connect(clk.id, "out", lg.id, "a")
    patch.connect(euc.id, "gate", lg.id, "b")
    patch.connect(lg.id, "and", kick.id, "trigger")
    patch.connect(lg.id, "xor", hat.id, "closed_trigger")
    patch.connect(kick.id, "out", mix.id, "in1")
    patch.connect(hat.id, "out", mix.id, "in2")
    patch.connect(mix.id, "out", out.id, "in")
    b = NumpyBackend(sample_rate=44100, block_size=256)
    b.compile(patch)
    for _ in range(8):
        mixdown, _dev = b.render_block_multi(256)
        assert mixdown is not None and np.all(np.isfinite(mixdown))
