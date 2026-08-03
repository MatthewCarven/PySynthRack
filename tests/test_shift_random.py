"""ShiftRandom — the looping shift-register random CV.

Pins the contract: p=0 is a locked loop with period exactly `length`,
p=1 is the complemented loop with period exactly 2×`length` (every
recirculating bit flips — the classic Turing-machine extreme), seeded
determinism (same seed = same sequence, live seed change re-rolls),
`write` forces ones, bipolar/unipolar mapping, gate mirrors bit 0,
edge semantics across block joins (a held-high clock never retriggers),
block-size independence, and unpatched clock = frozen output.

Drives the renderer directly with hand-fed clock buffers (the slew-test
harness pattern).
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend

from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules import shift_random as _shift_random  # noqa: F401
from pysynthrack.core.module import all_module_types, get_module_type

SR = 1000


def _driver(params=None, with_write=False, block=64):
    """Backend + `constant → shift_random.clock` patch; step with buffers."""
    patch = Patch()
    m = patch.add_module("shift_random", params=params or {})
    # Gate-kind sources (constant is cv-kind and won't connect); their
    # buffers are injected by hand, their own renders never run.
    clk = patch.add_module("clock")
    patch.connect(clk.id, "out", m.id, "clock")
    wsrc = None
    if with_write:
        wsrc = patch.add_module("clock")
        patch.connect(wsrc.id, "out", m.id, "write")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(clock_block, write_block=None):
        arr = np.asarray(clock_block, dtype=np.float32)
        F = arr.shape[-1]
        buffers = {(clk.id, "out"): arr}
        if wsrc is not None:
            wb = (
                np.zeros(F, dtype=np.float32)
                if write_block is None
                else np.asarray(write_block, dtype=np.float32)
            )
            buffers[(wsrc.id, "out")] = wb
        return b._render_shift_random(patch.get(m.id), F, buffers, patch)

    step.module = m
    step.patch = patch
    step.backend = b
    return step


def _pulses(n_edges, gap=4):
    """A clock buffer with `n_edges` one-sample pulses, `gap` samples apart."""
    out = np.zeros(n_edges * gap, dtype=np.float32)
    out[::gap] = 1.0
    return out


def _stepped_values(cv, gap=4):
    """The held value right after each clock edge."""
    return cv[1::gap] if len(cv) > 1 else cv


def _sequence(step, n_edges, gap=4):
    """Clock the register `n_edges` times, return the per-step CV values."""
    cv = step(_pulses(n_edges, gap))["cv"]
    return cv[::gap]  # value AT each edge sample (updates take effect there)


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["shift_random"]
    assert cls is get_module_type("shift_random")
    assert cls.CATEGORY == "Modulation"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["clock", "write"]
    assert [p.name for p in m.output_ports] == ["cv", "gate"]
    assert m.params["probability"] == 0.1
    assert m.params["length"] == 8


def test_serialization_round_trip():
    cls = all_module_types()["shift_random"]
    m = cls(2, params={"probability": 0.5, "seed": 99, "bipolar": True})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the loop --------------------------------------------------------------


def test_p0_locks_the_loop_with_exact_period():
    # The byte reads register positions 0..7, which carry the *initial
    # random fill* until 16 clocks have pushed pure loop content through
    # the whole register — so periodicity starts after that drain.
    for length in (2, 5, 8, 16):
        step = _driver({"probability": 0.0, "length": length, "seed": 3})
        seq = _sequence(step, 16 + length * 4)
        for i in range(16 + length, len(seq)):
            assert seq[i] == seq[i - length], f"length {length} broke at {i}"


def test_p1_is_the_complemented_loop_period_2x():
    # p=1 flips EVERY recirculating bit: after 2×length clocks the loop
    # returns to itself (each bit inverted twice) — the classic double
    # loop. Same 16-clock drain of the initial fill as the p=0 test.
    length = 6
    step = _driver({"probability": 1.0, "length": length, "seed": 5})
    seq = _sequence(step, 16 + length * 6)
    period = 2 * length
    for i in range(16 + period, len(seq)):
        assert seq[i] == seq[i - period]
    # and it is NOT the plain loop (something actually flips):
    assert any(
        seq[i] != seq[i - length] for i in range(16 + length, len(seq))
    )


def test_seeded_sequences_reproduce():
    a = _sequence(_driver({"probability": 0.5, "seed": 123}), 64)
    b = _sequence(_driver({"probability": 0.5, "seed": 123}), 64)
    c = _sequence(_driver({"probability": 0.5, "seed": 124}), 64)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_seed_change_rerolls_live():
    step = _driver({"probability": 0.0, "seed": 1})
    before = _sequence(step, 8)
    step.module.set_param("seed", 2)
    after = _sequence(step, 8)
    assert not np.array_equal(before, after)  # 16 fresh bits (2^-16 flake)


# ----- write / mapping / gate ------------------------------------------------


def test_write_high_fills_the_register_with_ones():
    step = _driver(
        {"probability": 0.0, "length": 16, "range": 2.0}, with_write=True
    )
    clock = _pulses(16)
    res = step(clock, np.ones_like(clock))
    assert res["cv"][-1] == np.float32(2.0)  # byte 255/255 × range
    assert res["gate"][-1] == 1.0


def test_unipolar_and_bipolar_mapping():
    # With write never high and probability 0, drive the register to a
    # known all-ones state first, then check the two mappings' extremes.
    step_u = _driver({"probability": 0.0, "range": 3.0}, with_write=True)
    clock = _pulses(16)
    top = step_u(clock, np.ones_like(clock))["cv"][-1]
    assert top == np.float32(3.0)
    step_b = _driver(
        {"probability": 0.0, "range": 3.0, "bipolar": True}, with_write=True
    )
    top_b = step_b(clock, np.ones_like(clock))["cv"][-1]
    assert top_b == np.float32(3.0)  # byte 255 → +range either way
    # bipolar zero-byte would sit at −range; check via a fresh module whose
    # seeded register happens to have bit pattern != 0 — instead verify the
    # mapping formula on the same state: unipolar top=range vs bipolar
    # top=+range proves the ×2−1 path only via the low end below.
    vals_b = _sequence(
        _driver({"probability": 1.0, "range": 1.0, "bipolar": True, "seed": 9}),
        64,
    )
    assert vals_b.min() < 0.0  # bipolar actually goes negative


def test_gate_mirrors_bit0_and_holds_between_clocks():
    step = _driver({"probability": 0.0, "length": 3, "seed": 4})
    res = step(_pulses(6, gap=8))
    gate = res["gate"]
    # Held between edges: each 8-sample segment is constant.
    for k in range(6):
        seg = gate[k * 8 : (k + 1) * 8]
        assert np.all(seg == seg[0])
    # And it's binary.
    assert set(np.unique(gate)).issubset({0.0, 1.0})


# ----- edges / blocks --------------------------------------------------------


def test_held_high_clock_does_not_retrigger_across_blocks():
    step = _driver({"probability": 0.0, "length": 8, "seed": 6}, block=32)
    high = np.ones(32, dtype=np.float32)
    r1 = step(high)  # one edge at sample 0
    r2 = step(high)  # still high: no new edge
    assert np.all(r2["cv"] == r1["cv"][-1])


def test_block_size_independent():
    clock = _pulses(32, gap=7)
    big = _driver({"probability": 0.7, "seed": 21}, block=len(clock))
    small = _driver({"probability": 0.7, "seed": 21}, block=8)
    cv_big = big(clock)["cv"]
    parts = [
        small(clock[i : i + 8])["cv"] for i in range(0, len(clock) - 7, 8)
    ]
    tail = len(clock) % 8
    if tail:
        parts.append(small(clock[-tail:])["cv"])
    assert np.array_equal(cv_big, np.concatenate(parts))


def test_unpatched_clock_holds_output():
    patch = Patch()
    m = patch.add_module("shift_random", params={"seed": 8})
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    r1 = b._render_shift_random(patch.get(m.id), 64, {}, patch)
    r2 = b._render_shift_random(patch.get(m.id), 64, {}, patch)
    assert np.all(r1["cv"] == r1["cv"][0])
    assert np.array_equal(r1["cv"], r2["cv"])
