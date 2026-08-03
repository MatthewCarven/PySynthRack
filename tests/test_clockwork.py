"""Clockwork trio — euclidean, burst, bernoulli_gate.

Pins the contract: the arithmetic Bjorklund table lands the canonical
patterns (E(3,8) tresillo verbatim, E(4,16) four-on-the-floor, E(5,8) a
rotation of the Bjorklund set), rotation walks it, reset realigns,
gate_len holds a measured fraction of the step across block joins, and
accents intersect the hits; burst fires exactly ``count`` gates on a
grid whose spacing spread warps, env steps (1−decay)^k, retrigger
restarts, and clocked mode lands on every division-th edge; bernoulli
routes whole gates (lengths preserved, counts partitioned exactly),
p=0/1 degenerate exactly, p_cv shifts the coin, toggle alternates at
p=1, and seeded streams reproduce. All three block-size independent.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.clockwork import euclidean_pattern

SR = 1000


def _driver(module_type, params=None, jacks=("clock",), block=64):
    patch = Patch()
    m = patch.add_module(module_type, params=params or {})
    feeds = {}
    for jack in jacks:
        src = patch.add_module("constant" if jack == "p_cv" else "clock")
        patch.connect(src.id, "out", m.id, jack)
        feeds[jack] = src
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    render = {
        "euclidean": b._render_euclidean,
        "burst": b._render_burst,
        "bernoulli_gate": b._render_bernoulli,
    }[module_type]

    def step(**blocks):
        frames = len(next(iter(blocks.values())))
        buffers = {
            (feeds[j].id, "out"): np.asarray(a, dtype=np.float32)
            for j, a in blocks.items()
        }
        return render(patch.get(m.id), frames, buffers, patch)

    step.module = m
    step.backend = b
    return step


def _clock_pulses(n_edges, gap=10, width=3):
    out = np.zeros(n_edges * gap, dtype=np.float32)
    for k in range(n_edges):
        out[k * gap : k * gap + width] = 1.0
    return out


# ----- euclidean_pattern (the table) ----------------------------------------


def test_tresillo_verbatim():
    assert euclidean_pattern(8, 3) == (
        True, False, False, True, False, False, True, False,
    )  # 10010010


def test_four_on_the_floor():
    p = euclidean_pattern(16, 4)
    assert p == tuple(i % 4 == 0 for i in range(16))


def test_e58_is_a_rotation_of_bjorklund():
    p = tuple(euclidean_pattern(8, 5))
    canonical = (True, False, True, True, False, True, True, False)  # 10110110
    rotations = {
        tuple(canonical[i:] + canonical[:i]) for i in range(8)
    }
    assert p in rotations
    assert sum(p) == 5


def test_rotation_walks_the_pattern():
    base = euclidean_pattern(8, 3, 0)
    rot = euclidean_pattern(8, 3, 1)
    assert rot == tuple(base[(i + 1) % 8] for i in range(8))


def test_degenerate_fills():
    assert euclidean_pattern(8, 0) == (False,) * 8
    assert euclidean_pattern(8, 8) == (True,) * 8


# ----- euclidean renderer ----------------------------------------------------


def _euclid_hits(step, n_edges, gap=10):
    """Which clock ticks produced a gate (sampled at each edge)."""
    clock = _clock_pulses(n_edges, gap)
    gate = step(clock=clock)["gate"]
    return [bool(gate[k * gap] > 0.5) for k in range(n_edges)]


def test_renderer_plays_the_tresillo():
    step = _driver("euclidean", {"steps": 8, "fills": 3})
    hits = _euclid_hits(step, 16)
    assert hits == [True, False, False, True, False, False, True, False] * 2


def test_reset_realigns_to_step_one():
    step = _driver("euclidean", {"steps": 8, "fills": 3}, jacks=("clock", "reset"))
    clock = _clock_pulses(5)
    step(clock=clock, reset=np.zeros_like(clock))  # 5 steps in
    rst = np.zeros(10, dtype=np.float32)
    rst[0] = 1.0
    step(clock=np.zeros(10, dtype=np.float32), reset=rst)
    hits = []
    clock2 = _clock_pulses(8)
    gate = step(clock=clock2, reset=np.zeros_like(clock2))["gate"]
    hits = [bool(gate[k * 10] > 0.5) for k in range(8)]
    assert hits == [True, False, False, True, False, False, True, False]


def test_gate_len_holds_fraction_of_measured_step():
    step = _driver("euclidean", {"steps": 4, "fills": 4, "gate_len": 0.5})
    clock = _clock_pulses(8, gap=20, width=2)
    gate = step(clock=clock)["gate"]
    # After the first interval is measured (edge 2 onward), each hit
    # holds 10 samples = 0.5 × the 20-sample step.
    third = gate[40:60]
    assert np.sum(third) == 10.0
    assert np.all(third[:10] == 1.0)


def test_gate_len_carries_across_block_joins():
    big = _driver("euclidean", {"steps": 4, "fills": 4, "gate_len": 0.8},
                  block=160)
    small = _driver("euclidean", {"steps": 4, "fills": 4, "gate_len": 0.8},
                    block=16)
    clock = _clock_pulses(8, gap=20, width=2)
    g_big = big(clock=clock)["gate"]
    parts = [
        small(clock=clock[i : i + 16])["gate"]
        for i in range(0, 160, 16)
    ]
    assert np.array_equal(g_big, np.concatenate(parts))


def test_accent_is_a_subset_of_hits():
    step = _driver("euclidean", {"steps": 8, "fills": 5, "accent_fills": 2})
    clock = _clock_pulses(16)
    res = step(clock=clock)
    for k in range(16):
        if res["accent"][k * 10] > 0.5:
            assert res["gate"][k * 10] > 0.5  # accents only on hits
    assert float(res["accent"].sum()) > 0.0
    assert float(res["accent"].sum()) < float(res["gate"].sum())


# ----- burst -----------------------------------------------------------------


def _count_gates(gate):
    g = np.asarray(gate) > 0.5
    return int(np.sum(g[1:] & ~g[:-1]) + (1 if g[0] else 0))


def test_burst_fires_exact_count():
    step = _driver("burst", {"count": 5, "rate": 50.0}, jacks=("trigger",))
    trig = np.zeros(200, dtype=np.float32)
    trig[0] = 1.0
    gate = step(trigger=trig)["gate"]
    assert _count_gates(gate) == 5


def test_burst_grid_even_at_spread_zero():
    step = _driver("burst", {"count": 4, "rate": 20.0, "spread": 0.0},
                   jacks=("trigger",), block=256)
    trig = np.zeros(256, dtype=np.float32)
    trig[0] = 1.0
    gate = step(trigger=trig)["gate"] > 0.5
    starts = np.flatnonzero(gate[1:] & ~gate[:-1]) + 1
    starts = np.concatenate(([0], starts)) if gate[0] else starts
    intervals = np.diff(starts)
    assert len(starts) == 4
    assert np.all(np.abs(intervals - 50) <= 1)  # 20 Hz at SR 1000


def test_burst_spread_warps_the_grid():
    def intervals(spread):
        step = _driver("burst", {"count": 5, "rate": 20.0, "spread": spread},
                       jacks=("trigger",), block=400)
        trig = np.zeros(400, dtype=np.float32)
        trig[0] = 1.0
        gate = step(trigger=trig)["gate"] > 0.5
        starts = np.flatnonzero(gate[1:] & ~gate[:-1]) + 1
        starts = np.concatenate(([0], starts)) if gate[0] else starts
        return np.diff(starts)

    rit = intervals(1.0)
    acc = intervals(-1.0)
    assert rit[-1] > rit[0]  # ritardando: gaps widen
    assert acc[-1] < acc[0]  # accelerando: gaps shrink


def test_burst_env_tapers_per_gate():
    step = _driver("burst", {"count": 3, "rate": 25.0, "decay": 0.5},
                   jacks=("trigger",), block=200)
    trig = np.zeros(200, dtype=np.float32)
    trig[0] = 1.0
    res = step(trigger=trig)
    gate = res["gate"] > 0.5
    starts = np.flatnonzero(gate[1:] & ~gate[:-1]) + 1
    starts = np.concatenate(([0], starts)) if gate[0] else starts
    vals = [float(res["env"][s]) for s in starts]
    assert np.allclose(vals, [1.0, 0.5, 0.25])


def test_burst_retrigger_restarts():
    step = _driver("burst", {"count": 8, "rate": 10.0}, jacks=("trigger",),
                   block=1000)
    trig = np.zeros(1000, dtype=np.float32)
    trig[0] = 1.0
    trig[150] = 1.0  # retrigger mid-burst
    gate = step(trigger=trig)["gate"]
    # 8 gates from the retrigger (the first burst got ~2 out) — total
    # well under 2×count, and the last gate lands ~150+700ms in.
    n = _count_gates(gate)
    assert 8 <= n <= 10
    assert np.any(gate[800:] > 0.5)  # restarted burst reaches its tail


def test_burst_clocked_lands_on_divided_edges():
    step = _driver("burst", {"count": 3, "division": 2},
                   jacks=("trigger", "clock"), block=200)
    trig = np.zeros(200, dtype=np.float32)
    trig[0] = 1.0
    clock = _clock_pulses(10, gap=20, width=4)[:200]
    gate = step(trigger=trig, clock=clock)["gate"]
    starts = np.flatnonzero((gate[1:] > 0.5) & (gate[:-1] <= 0.5)) + 1
    if gate[0] > 0.5:
        starts = np.concatenate(([0], starts))
    assert len(starts) == 3
    assert np.array_equal(starts % 40, [0, 0, 0])  # every 2nd edge


# ----- bernoulli -------------------------------------------------------------


def test_bernoulli_degenerate_probabilities():
    for p, key in ((1.0, "out_a"), (0.0, "out_b")):
        step = _driver("bernoulli_gate", {"probability": p}, jacks=("in",))
        gates = _clock_pulses(10)
        res = step(**{"in": gates})
        assert np.array_equal(res[key], gates)
        other = "out_b" if key == "out_a" else "out_a"
        assert np.all(res[other] == 0.0)


def test_bernoulli_partitions_gates_exactly():
    step = _driver("bernoulli_gate", {"probability": 0.5, "seed": 7},
                   jacks=("in",), block=400)
    gates = _clock_pulses(40)
    res = step(**{"in": gates})
    total = _count_gates(gates)
    assert _count_gates(res["out_a"]) + _count_gates(res["out_b"]) == total
    # Whole-gate routing: A + B reconstructs the input exactly.
    assert np.array_equal(res["out_a"] + res["out_b"], gates)


def test_bernoulli_seeded_reproducible():
    a = _driver("bernoulli_gate", {"seed": 9}, jacks=("in",), block=400)
    b_ = _driver("bernoulli_gate", {"seed": 9}, jacks=("in",), block=400)
    c = _driver("bernoulli_gate", {"seed": 10}, jacks=("in",), block=400)
    gates = _clock_pulses(40)
    ra, rb, rc = a(**{"in": gates}), b_(**{"in": gates}), c(**{"in": gates})
    assert np.array_equal(ra["out_a"], rb["out_a"])
    assert not np.array_equal(ra["out_a"], rc["out_a"])


def test_bernoulli_toggle_alternates_at_p1():
    step = _driver("bernoulli_gate", {"probability": 1.0, "mode": "toggle"},
                   jacks=("in",), block=100)
    gates = _clock_pulses(8)[:100]
    res = step(**{"in": gates})
    seq = []
    for k in range(8):
        n = k * 10
        seq.append("a" if res["out_a"][n] > 0.5 else "b")
    # p=1 toggle flips every gate: strict alternation.
    assert all(x != y for x, y in zip(seq, seq[1:]))


def test_bernoulli_p_cv_shifts_the_coin():
    step = _driver("bernoulli_gate", {"probability": 0.0},
                   jacks=("in", "p_cv"), block=200)
    gates = _clock_pulses(10)[:200]
    res = step(**{"in": gates}, p_cv=np.ones(200, dtype=np.float32))
    assert np.array_equal(res["out_a"], gates)  # 0 + 1 → certainty of A


def test_bernoulli_block_size_independent():
    gates = _clock_pulses(32)
    big = _driver("bernoulli_gate", {"seed": 3}, jacks=("in",), block=len(gates))
    small = _driver("bernoulli_gate", {"seed": 3}, jacks=("in",), block=32)
    ra = big(**{"in": gates})["out_a"]
    parts = [
        small(**{"in": gates[i : i + 32]})["out_a"]
        for i in range(0, len(gates), 32)
    ]
    assert np.array_equal(ra, np.concatenate(parts))


# ----- registration ----------------------------------------------------------


@pytest.mark.parametrize(
    "type_name,in_ports,out_ports",
    [
        ("euclidean", ["clock", "reset"], ["gate", "accent"]),
        ("burst", ["trigger", "clock"], ["gate", "env"]),
        ("bernoulli_gate", ["in", "p_cv"], ["out_a", "out_b"]),
    ],
)
def test_registered_with_ports(type_name, in_ports, out_ports):
    cls = all_module_types()[type_name]
    assert cls is get_module_type(type_name)
    assert cls.CATEGORY == "Modulation"
    m = cls(1)
    assert [p.name for p in m.input_ports] == in_ports
    assert [p.name for p in m.output_ports] == out_ports
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params
