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
        ("euclidean", ["clock", "reset", "fills_cv"], ["gate", "accent"]),
        ("burst", ["trigger", "clock", "count_cv"], ["gate", "env"]),
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


# ----- the 2026-09-11 love pass: fills_cv, count_cv, clock_divider -----------


def _driver_cv(module_type, params=None, jacks=("clock",), cvs=(), block=64):
    """_driver plus constant-module feeds on the named CV jacks."""
    patch = Patch()
    m = patch.add_module(module_type, params=params or {})
    feeds = {}
    for jack in jacks:
        src = patch.add_module("clock")
        patch.connect(src.id, "out", m.id, jack)
        feeds[jack] = src
    for jack in cvs:
        src = patch.add_module("constant")
        patch.connect(src.id, "out", m.id, jack)
        feeds[jack] = src
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    render = {
        "euclidean": b._render_euclidean,
        "burst": b._render_burst,
        "bernoulli_gate": b._render_bernoulli,
        "clock_divider": b._render_clock_divider,
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


def _edges(gate):
    g = np.asarray(gate) > 0.5
    return np.flatnonzero(g & ~np.concatenate([[False], g[:-1]])).tolist()


# -- euclidean fills_cv -----------------------------------------------------------

def test_fills_cv_unpatched_is_unchanged():
    plain = _driver("euclidean", {"steps": 8, "fills": 3})
    withjack = _driver_cv("euclidean", {"steps": 8, "fills": 3}, cvs=())
    clock = _clock_pulses(16)
    assert np.array_equal(plain(clock=clock)["gate"], withjack(clock=clock)["gate"])


def test_fills_cv_moves_the_fill_count():
    """fills 3 + depth 8 x 0.25 V = 5 -> E(5,8), not E(3,8)."""
    step = _driver_cv("euclidean", {"steps": 8, "fills": 3}, cvs=("fills_cv",))
    clock = _clock_pulses(8)
    cv = np.full(len(clock), 0.25, dtype=np.float32)
    gate = step(clock=clock, fills_cv=cv)["gate"]
    hits = [bool(gate[k * 10] > 0.5) for k in range(8)]
    assert hits == list(euclidean_pattern(8, 5))
    assert sum(hits) == 5


def test_fills_cv_is_clamped_to_the_loop():
    step = _driver_cv("euclidean", {"steps": 8, "fills": 3}, cvs=("fills_cv",))
    clock = _clock_pulses(8)
    gate = step(clock=clock, fills_cv=np.full(len(clock), 5.0, dtype=np.float32))["gate"]
    assert all(gate[k * 10] > 0.5 for k in range(8))          # every step
    step = _driver_cv("euclidean", {"steps": 8, "fills": 3}, cvs=("fills_cv",))
    gate = step(clock=clock, fills_cv=np.full(len(clock), -5.0, dtype=np.float32))["gate"]
    assert np.all(gate == 0.0)                                  # none


def test_fills_cv_is_read_at_each_edge():
    """The CV steps mid-loop: the first half plays E(3,8)'s hits, the
    second half E(8,8)'s — the pattern is rebuilt at the tick."""
    step = _driver_cv("euclidean", {"steps": 8, "fills": 3}, cvs=("fills_cv",))
    clock = _clock_pulses(8)
    cv = np.zeros(len(clock), dtype=np.float32)
    cv[40:] = 1.0                       # from tick 4 on: fills 3 + 8 -> 8
    gate = step(clock=clock, fills_cv=cv)["gate"]
    hits = [bool(gate[k * 10] > 0.5) for k in range(8)]
    assert hits[:4] == list(euclidean_pattern(8, 3))[:4]
    assert hits[4:] == [True] * 4


def test_fills_cv_depth_scales():
    step = _driver_cv("euclidean", {"steps": 16, "fills": 0, "fills_cv_depth": 16.0},
                      cvs=("fills_cv",))
    clock = _clock_pulses(16)
    gate = step(clock=clock, fills_cv=np.full(len(clock), 0.25, dtype=np.float32))["gate"]
    hits = [bool(gate[k * 10] > 0.5) for k in range(16)]
    assert sum(hits) == 4


# -- burst count_cv ---------------------------------------------------------------

def test_count_cv_unpatched_is_unchanged():
    trig = np.zeros(200, dtype=np.float32)
    trig[0] = 1.0
    a = _driver("burst", {"count": 3, "rate": 50.0}, jacks=("trigger",))(trigger=trig)
    b = _driver_cv("burst", {"count": 3, "rate": 50.0}, jacks=("trigger",))(trigger=trig)
    assert np.array_equal(a["gate"], b["gate"])
    assert np.array_equal(a["env"], b["env"])


def test_count_cv_sets_the_gate_count():
    step = _driver_cv("burst", {"count": 3, "rate": 50.0}, jacks=("trigger",),
                      cvs=("count_cv",))
    trig = np.zeros(300, dtype=np.float32)
    trig[0] = 1.0
    cv = np.full(300, 0.5, dtype=np.float32)        # 3 + 8 x 0.5 = 7
    assert _count_gates(step(trigger=trig, count_cv=cv)["gate"]) == 7


def test_count_cv_is_latched_at_the_trigger():
    """A burst's count is decided when it fires; the CV moving afterwards
    (even to a smaller count) changes nothing about that burst."""
    step = _driver_cv("burst", {"count": 3, "rate": 50.0}, jacks=("trigger",),
                      cvs=("count_cv",))
    trig = np.zeros(300, dtype=np.float32)
    trig[0] = 1.0
    cv = np.full(300, -1.0, dtype=np.float32)
    cv[0] = 0.5
    assert _count_gates(step(trigger=trig, count_cv=cv)["gate"]) == 7


def test_count_cv_is_clamped():
    step = _driver_cv("burst", {"count": 3, "rate": 50.0}, jacks=("trigger",),
                      cvs=("count_cv",))
    trig = np.zeros(600, dtype=np.float32)
    trig[0] = 1.0
    assert _count_gates(step(trigger=trig, count_cv=np.full(600, 9.0, np.float32))["gate"]) == 16
    step = _driver_cv("burst", {"count": 3, "rate": 50.0}, jacks=("trigger",),
                      cvs=("count_cv",))
    assert _count_gates(step(trigger=trig, count_cv=np.full(600, -9.0, np.float32))["gate"]) == 1


# -- clock_divider ------------------------------------------------------------------

def test_clock_divider_registered():
    cls = all_module_types()["clock_divider"]
    assert cls.CATEGORY == "Modulation"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["clock", "reset"]
    assert [p.name for p in m.output_ports] == ["div2", "div4", "div8", "divn", "mult"]
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


def _div_run(params, n_edges, gap=20, width=4, block=64, jacks=("clock",), reset_at=None):
    step = _driver_cv("clock_divider", params, jacks=jacks, block=block)
    clock = _clock_pulses(n_edges, gap, width)
    feeds = {"clock": clock}
    if "reset" in jacks:
        rst = np.zeros_like(clock)
        if reset_at is not None:
            rst[reset_at] = 1.0
        feeds["reset"] = rst
    outs = {k: [] for k in ("div2", "div4", "div8", "divn", "mult")}
    for i in range(0, len(clock), block):
        res = step(**{k: v[i:i + block] for k, v in feeds.items()})
        for k in outs:
            outs[k].append(res[k])
    return {k: np.concatenate(v) for k, v in outs.items()}, gap


def test_divisions_count_exactly_over_many_edges():
    """Division counts exact over 1000 edges (the spec's test)."""
    outs, gap = _div_run({"n": 3}, 1000)
    assert len(_edges(outs["div2"])) == 500
    assert len(_edges(outs["div4"])) == 250
    assert len(_edges(outs["div8"])) == 125
    assert len(_edges(outs["divn"])) == 334          # ceil(1000/3)


def test_every_output_fires_on_the_downbeat_and_on_its_own_edges():
    outs, gap = _div_run({"n": 3}, 24)
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        assert _edges(outs[name]) == [i * gap for i in range(0, 24, k)], name
    assert _edges(outs["mult"])[0] == 0


def test_reset_realigns_every_counter():
    """5 edges in, reset, then the next edge is a downbeat everywhere."""
    step = _driver_cv("clock_divider", {"n": 3}, jacks=("clock", "reset"))
    gap = 20
    clock = _clock_pulses(5, gap)
    step(clock=clock, reset=np.zeros_like(clock))
    rst = np.zeros(10, dtype=np.float32)
    rst[0] = 1.0
    step(clock=np.zeros(10, dtype=np.float32), reset=rst)
    clock2 = _clock_pulses(8, gap)
    res = step(clock=clock2, reset=np.zeros_like(clock2))
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        assert _edges(res[name]) == [i * gap for i in range(0, 8, k)], name


def test_gate_width_is_pw_of_each_outputs_period():
    outs, gap = _div_run({"n": 3, "pw": 0.25}, 24)
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        g = outs[name] > 0.5
        # second gate (the first mirrors the clock: no interval yet)
        start = _edges(outs[name])[1]
        length = int(np.argmin(g[start:])) if not g[start:].all() else len(g) - start
        assert length == round(0.25 * k * gap), (name, length)


def test_first_gate_mirrors_the_clock_before_an_interval_exists():
    outs, gap = _div_run({"n": 3, "pw": 0.9}, 4, width=4)
    for name in ("div2", "div4", "div8", "divn", "mult"):
        g = outs[name] > 0.5
        assert g[:4].all() and not g[4], name


def test_swing_delays_every_second_divn_gate():
    """swing 0.5 on n=1: odd gates land half a period late — the classic
    shuffle. Even gates stay on the edge."""
    outs, gap = _div_run({"n": 1, "swing": 0.5, "pw": 0.2}, 12)
    e = _edges(outs["divn"])
    assert e[0] == 0                    # first: mirrors (no interval yet)
    assert e[1] == gap + round(0.5 * gap)
    assert e[2] == 2 * gap
    assert e[3] == 3 * gap + round(0.5 * gap)
    # and triplet feel at 0.33, on n = 2 (period 2 gaps)
    outs, gap = _div_run({"n": 2, "swing": 0.33, "pw": 0.2}, 12)
    e = _edges(outs["divn"])
    assert e[1] == 2 * gap + round(0.33 * 2 * gap)
    assert e[2] == 4 * gap


def test_swing_zero_is_straight():
    outs, gap = _div_run({"n": 1, "swing": 0.0, "pw": 0.2}, 12)
    assert _edges(outs["divn"]) == [i * gap for i in range(12)]


def test_mult_emits_m_gates_per_period():
    outs, gap = _div_run({"m": 2, "pw": 0.2}, 10)
    e = _edges(outs["mult"])
    # first period: only the edge (no interval yet); after: edge + midpoint
    assert e[0] == 0 and e[1] == gap
    assert e[2] == gap + gap // 2 and e[3] == 2 * gap
    outs, gap = _div_run({"m": 4, "pw": 0.2}, 10)
    e = _edges(outs["mult"])
    assert e[1:5] == [gap, gap + 5, gap + 10, gap + 15]


def test_mult_tracks_a_tempo_change_within_one_period():
    """The spec's claim: after the interval halves, one period of stale
    scheduling and then the multiplier is back on the new grid."""
    step = _driver_cv("clock_divider", {"m": 2, "pw": 0.2}, block=64)
    slow = _clock_pulses(4, 40, 4)
    fast = _clock_pulses(8, 20, 4)
    clock = np.concatenate([slow, fast])
    out = np.concatenate([step(clock=clock[i:i + 64])["mult"]
                          for i in range(0, len(clock), 64)])
    e = _edges(out)
    # once the fast interval has been measured (two fast edges in), the
    # midpoints sit at +10, not +20
    fast_start = len(slow)
    later = [x for x in e if x >= fast_start + 2 * 20]
    for x in later[:4]:
        assert (x - fast_start) % 10 == 0, (x, e)


def test_divider_is_block_size_independent():
    params = {"n": 5, "m": 3, "swing": 0.4, "pw": 0.6}
    big, _ = _div_run(params, 40, block=800)
    small, _ = _div_run(params, 40, block=16)
    for k in big:
        assert np.array_equal(big[k], small[k]), k


# -- the 2026-09-22 love pass: steady gate lengths on a swung clock ------------------


def _swung_pulses(n_edges, long_gap, short_gap, width=4):
    """A clock train whose intervals alternate long/short — what a
    swung ``clock`` hands the divider (even pulse, late odd pulse)."""
    starts, t = [], 0
    for k in range(n_edges):
        starts.append(t)
        t += long_gap if k % 2 == 0 else short_gap
    out = np.zeros(t, dtype=np.float32)
    for s in starts:
        out[s:s + width] = 1.0
    return out, starts


def _run_row(clock, params, block=64):
    step = _driver_cv("clock_divider", params, jacks=("clock",), block=block)
    outs = {k: [] for k in ("div2", "div4", "div8", "divn", "mult")}
    for i in range(0, len(clock), block):
        res = step(clock=clock[i:i + block])
        for k in outs:
            outs[k].append(res[k])
    return {k: np.concatenate(v) for k, v in outs.items()}


def _lengths(y):
    g = np.asarray(y) > 0.5
    prev = np.concatenate([[False], g[:-1]])
    nxt = np.concatenate([g[1:], [False]])
    r = np.flatnonzero(g & ~prev)
    f = np.flatnonzero(g & ~nxt)
    return r.tolist(), [int(b - a + 1) for a, b in zip(r, f)]


def test_gate_lengths_come_off_the_average_period_on_a_swung_clock():
    """The finding the clock's swing pass documented, now fixed.

    Intervals alternate 26/14 (a 0.3-swung 20-sample period). Before,
    ``div2``/``div4``/``div8`` always measured the SHORT one (they land
    on the even pulses) and sat ~30% under their true period, and
    ``divn`` at an odd ``n`` flapped between the two. Now every length
    is ``pw x k x mean(last two intervals)`` = ``pw x k x 20``, flat.
    """
    clock, starts = _swung_pulses(44, 26, 14)
    outs = _run_row(clock, {"n": 3, "pw": 0.5})
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        rises, lens = _lengths(outs[name])
        steady = lens[2:22] if len(lens) > 22 else lens[2:]
        assert steady, name
        assert max(steady) - min(steady) <= 1, (name, steady)
        assert steady[0] == round(0.5 * k * 20), (name, steady[0])


def test_the_swung_fix_moves_no_rising_edge():
    """Only the falling edges move: every division still fires on its
    own input edge, and ``divn``'s swing offset is still measured off
    the LAST real interval (a position, not a length)."""
    clock, starts = _swung_pulses(44, 26, 14)
    outs = _run_row(clock, {"n": 3, "pw": 0.5})
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        rises, _ = _lengths(outs[name])
        assert rises == [starts[i] for i in range(0, 44, k)][:len(rises)], name
    # and with the divider's OWN swing on, the offset is swing x n x the
    # LAST real interval -- the pre-love-pass position, unchanged, even
    # though the length beside it now comes off the average.
    outs = _run_row(clock, {"n": 2, "swing": 0.5, "pw": 0.2})
    rises, lens = _lengths(outs["divn"])
    assert rises[0] == starts[0]                          # mirrors: no interval
    assert rises[1] == starts[2] + round(0.5 * 2 * 14)    # interval into edge 2
    assert rises[2] == starts[4]                          # the even one, on time
    assert lens[1] == round(0.2 * 2 * 20)                 # length off the average


def test_a_steady_clock_is_untouched_by_the_average():
    """mean(I, I) == I exactly, so a steady clock's lengths are the
    pre-love-pass ones, sample for sample."""
    outs, gap = _div_run({"n": 3, "pw": 0.25}, 24)
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        _r, lens = _lengths(outs[name])
        assert set(lens[1:]) == {round(0.25 * k * gap)}, (name, lens)


def test_a_new_gate_truncates_a_stale_one_so_the_gates_behind_it_survive():
    """The guard: a gate scheduled while an older one of the same output
    is still running cuts that one short, and the gates behind it get
    their own rising edges again.

    Intervals 26/14, ``m`` 2, ``pw`` 0.9: edge 1's second sub-gate (39,
    12 long) used to run to 50 and swallow BOTH of edge 2's (40 and 47).
    The 2026-09-22 pass freed 47 but left 40 merged (the sample where
    they meet was already emitted high). Since 2026-09-24 the early-edge
    fallback starts edge 2's gate one sample late instead, so 40 reads
    LOW and the gate rises at 41 -- a merge is never allowed.
    """
    clock, starts = _swung_pulses(12, 26, 14)
    outs = _run_row(clock, {"m": 2, "pw": 0.9})
    rises, _ = _lengths(outs["mult"])
    assert 39 in rises                       # the late sub-gate of edge 1
    assert outs["mult"][40] == 0.0           # the sample where they meet: low
    assert 41 in rises                       # edge 2's gate, one sample late
    assert 47 in rises                       # freed by the truncation


def test_the_real_clocks_swung_lengths_at_the_default_tempo():
    """The numbers the docs quote, measured through the real modules:
    8 Hz (5512.5-sample period) at ``swing`` 0.3, divider ``n`` 3,
    ``pw`` 0.5 — ``div2`` 5512 (was 3858, the short interval), ``divn``
    8269 flat (was 10750/5787)."""
    p = Patch()
    clk = p.add_module("clock", params={"pulse_width": 0.3, "swing": 0.3})
    div = p.add_module("clock_divider", params={"n": 3, "pw": 0.5})
    p.connect(clk.id, "out", div.id, "clock")
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(p)
    rows = {k: [] for k in ("div2", "div4", "div8", "divn")}
    for _ in range(int(44100 * 12 / 512)):
        c = b._render_clock(clk, 512, {}, p)
        r = b._render_clock_divider(div, 512, {(clk.id, "out"): c}, p)
        for k in rows:
            rows[k].append(np.asarray(r[k]).copy())
    got = {}
    for k in rows:
        _r, lens = _lengths(np.concatenate(rows[k]))
        got[k] = lens[1:21] if len(lens) > 21 else lens[1:]
    assert set(got["div2"]) == {5512}
    assert set(got["div4"]) == {11025}
    assert set(got["div8"]) == {22050}
    assert set(got["divn"]) == {8269}


def test_mult_keeps_the_real_interval():
    """``mult``'s job is to subdivide the period that ACTUALLY happened,
    so its sub-gates stay on the real interval -- positions and lengths
    both (the average would fight its own grid)."""
    outs, gap = _div_run({"m": 2, "pw": 0.4}, 12)
    _r, lens = _lengths(outs["mult"])
    assert set(lens[1:]) == {round(0.4 * gap / 2)}


def test_divider_unpatched_is_silent():
    patch = Patch()
    m = patch.add_module("clock_divider")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    res = b._render_clock_divider(patch.get(m.id), 64, {}, patch)
    assert all(np.all(v == 0.0) for v in res.values())


# -- the 2026-09-24 pass: a gate never merges into the next one ----------------------


def _real_clock_row(swing, secs=12.0, tail=1.0):
    """The real ``clock`` at its default 8 Hz, then ``tail`` seconds of
    silence so every scheduled gate lands inside the row."""
    p = Patch()
    clk = p.add_module("clock", params={"pulse_width": 0.3, "swing": swing})
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(p)
    rows = [np.asarray(b._render_clock(clk, 512, {}, p)).copy()
            for _ in range(int(44100 * secs / 512))]
    return np.concatenate(rows + [np.zeros(int(44100 * tail), np.float32)])


def _mult_expected(starts, m):
    """mult's gate count: edge 0 alone (no interval yet), then each
    edge's ``m`` sub-gates that start before the next real edge (the rest
    are dropped by design when that edge arrives)."""
    exp = 1
    for i in range(1, len(starts)):
        iv = starts[i] - starts[i - 1]
        nxt = starts[i + 1] if i + 1 < len(starts) else 10 ** 12
        exp += sum(1 for k in range(m) if starts[i] + int(round(k * iv / m)) < nxt)
    return exp


def _divn_expected(starts, n, swing):
    out = []
    for j, i in enumerate(range(0, len(starts), n)):
        late = j % 2 == 1 and swing > 0.0
        off = int(round(swing * n * (starts[i] - starts[i - 1]))) if late else 0
        out.append(starts[i] + off)
    return out


def test_a_long_swung_divn_gate_no_longer_merges_into_the_next():
    """The residue the 2026-09-22 pass left documented: a 0.3-swung 8 Hz
    clock, ``n`` 3, ``pw`` 0.9 -- 15 of 32 ``divn`` gates ran into the
    next one (their length is off the AVERAGE period, and an odd ``n`` on
    a swung clock alternates long/short division spans). Now every output
    rises on every expected sample. The swing is period-2, so the
    prediction is exact and it is the CAP that does it: no rising edge is
    moved by the fallback, every one is on its own clock edge."""
    clock = _real_clock_row(0.3)
    starts = _edges(clock)
    assert len(starts) == 96
    outs = _run_row(clock, {"n": 3, "m": 3, "pw": 0.9}, block=512)
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        assert _edges(outs[name]) == starts[0::k], name
    assert len(_edges(outs["mult"])) == _mult_expected(starts, 3)   # was 47 short
    # the capped divn gates: every one ends at least one sample before
    # the next rises (a short-side span, S+L+S, is 0.9 x the mean span)
    rises, lens = _lengths(outs["divn"])
    for r, ln, r_next in zip(rises, lens, rises[1:]):
        assert r + ln <= r_next - 1


@pytest.mark.parametrize("long_short", [(200, 200), (260, 140)])
def test_rising_edge_counts_are_exact_across_a_pw_by_swing_sweep(long_short):
    """Every output, every gate, at ``pw`` 0.5..0.95 x the divider's own
    swing 0..0.5 x ``n`` 1/3/4, on a straight and a 0.3-swung input.
    Before this pass the census over the real-clock version of this sweep
    was 2519 merged ``divn`` gates and 2256 merged ``mult`` gates."""
    long_gap, short_gap = long_short
    clock, starts = _swung_pulses(48, long_gap, short_gap)
    clock = np.concatenate([clock, np.zeros(4 * long_gap, np.float32)])
    for pw in (0.5, 0.7, 0.9, 0.95):
        for swing in (0.0, 0.3, 0.5):
            for n in (1, 3, 4):
                p = {"n": n, "m": 3, "swing": swing, "pw": pw}
                outs = _run_row(clock, p)
                for name, k in (("div2", 2), ("div4", 4), ("div8", 8)):
                    assert _edges(outs[name]) == starts[0::k], (name, p)
                rises, want = _edges(outs["divn"]), _divn_expected(starts, n, swing)
                assert len(rises) == len(want), p
                late = [r - w for r, w in zip(rises, want)]
                # Start-up: with ONE interval measured the prediction
                # assumes a steady clock, so on a swung one an early gate
                # may take the fallback's one-sample step. Once two
                # intervals are known the prediction is exact: on time.
                assert set(late[:3]) <= {0, 1}, (p, late)
                assert set(late[3:]) == {0}, (p, late)
                assert len(_edges(outs["mult"])) == _mult_expected(starts, 3), p


def test_an_early_edge_after_a_tempo_change_is_caught_by_the_fallback():
    """The prediction can be wrong: the interval halves (40 -> 20) and the
    gates scheduled off the old tempo are still high when the new edge
    arrives. That sample cannot be un-written, so the new gate starts ONE
    sample late and the stale one ends a sample before it -- every gate
    still gets its own rising edge. Exposure: the fallback must actually
    fire here, then settle back onto the edges."""
    slow = _clock_pulses(5, 40, 4)
    fast = _clock_pulses(16, 20, 4)
    clock = np.concatenate([slow, fast, np.zeros(80, np.float32)])
    starts = _edges(clock)
    outs = _run_row(clock, {"n": 1, "m": 2, "pw": 0.95})
    for name, k in (("div2", 2), ("divn", 1)):
        rises = _edges(outs[name])
        want = starts[0::k]
        assert len(rises) == len(want), name
        late = [r - w for r, w in zip(rises, want)]
        assert set(late) <= {0, 1}, (name, late)
        assert 1 in late, name                        # the fallback fired
        assert late[-3:] == [0, 0, 0], (name, late)   # and it settled
        g = outs[name] > 0.5
        for r in rises:
            assert not g[r - 1], (name, r)


def test_a_reset_on_an_edge_under_a_running_gate_still_gets_a_downbeat():
    """A reset makes a division fire earlier than predicted. If a long
    gate is still running there, the downbeat starts a sample late rather
    than vanishing into it."""
    outs, gap = _div_run({"n": 3, "pw": 0.95}, 12, jacks=("clock", "reset"),
                         reset_at=5 * 20)
    # edge 5 is the downbeat after the reset; div4's gate from edge 4
    # (76 long at pw 0.95) is still high there
    rises = _edges(outs["div4"])
    assert rises[:2] == [0, 4 * gap]
    assert rises[2] == 5 * gap + 1
    assert outs["div4"][5 * gap] == 0.0


def test_no_collision_means_no_change():
    """The cap only binds where the shipped render merged: a steady clock
    at ``pw`` 0.95 keeps its ``round(pw x period)`` lengths exactly, and
    so does ``divn`` at ``swing`` 0.3 while ``pw`` stays under the short
    side (0.5 < 0.7)."""
    outs, gap = _div_run({"n": 3, "pw": 0.95}, 48)
    for name, k in (("div2", 2), ("div4", 4), ("div8", 8), ("divn", 3)):
        _r, lens = _lengths(outs[name])
        assert set(lens[1:]) == {round(0.95 * k * gap)}, (name, lens)
    outs, gap = _div_run({"n": 3, "swing": 0.3, "pw": 0.5}, 48)
    _r, lens = _lengths(outs["divn"])
    assert set(lens[1:]) == {round(0.5 * 3 * gap)}, lens


def test_the_merge_guards_are_block_size_independent():
    """64 = 128 = 512 = 1000 over 5 s of the real swung clock with both
    guards busy, and block 1 on the tempo change (the fallback then reads
    the carried last sample on every edge)."""
    clock = _real_clock_row(0.3, secs=5.0)
    p = {"n": 3, "m": 3, "swing": 0.3, "pw": 0.9}
    ref = _run_row(clock, p, block=512)
    for blk in (64, 128, 1000):
        got = _run_row(clock, p, block=blk)
        for k in ref:
            assert np.array_equal(got[k], ref[k]), (blk, k)
    slow = _clock_pulses(5, 40, 4)
    fast = _clock_pulses(16, 20, 4)
    clock = np.concatenate([slow, fast, np.zeros(80, np.float32)])
    p = {"n": 1, "m": 2, "pw": 0.95}
    ref = _run_row(clock, p, block=64)
    got = _run_row(clock, p, block=1)
    for k in ref:
        assert np.array_equal(got[k], ref[k]), k
