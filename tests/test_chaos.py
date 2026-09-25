"""Chaos — strange-attractor CV source.

Pins the contract: seeded determinism is bit-exact (the module contains
no randomness beyond the seeded initial condition); a reset lands
exactly on the seeded starting point, sample-accurate; every output is
bit-exact across block splits (absolute-sample control grid); long runs
stay inside the range with zero non-finite samples; different seeds
decorrelate (it is actually chaotic) while identical seeds are
identical; rate scales the orbital speed; the gate follows each
system's documented semantics (lorenz = sign of x, rossler = sparse z
spikes).
"""
from __future__ import annotations

import numpy as np

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch

SR = 8000


def _driver(params=None, block=256, sr=SR):
    patch = Patch()
    ch = patch.add_module("chaos", params=params or {})
    kt = patch.add_module("key_trigger")
    patch.connect(kt.id, "out", ch.id, "reset")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(frames, reset=None):
        bufs = {}
        if reset is not None:
            bufs[(kt.id, "out")] = np.asarray(reset, dtype=np.float32)
        return b._render_chaos(patch.get(ch.id), frames, bufs, patch)

    step.ch = ch
    step.patch = patch
    step.backend = b
    return step


def _run(params, frames, block=256, reset=None):
    step = _driver(params, block=block)
    outs = {k: np.empty(frames, dtype=np.float32) for k in ("x", "y", "z", "gate")}
    for s in range(0, frames, block):
        e = min(s + block, frames)
        seg = None if reset is None else reset[s:e]
        r = step(e - s, seg)
        for k in outs:
            outs[k][s:e] = r[k]
    return outs


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["chaos"]
    assert cls is get_module_type("chaos")
    assert cls.CATEGORY == "Modulation"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["reset"]
    assert [p.name for p in m.output_ports] == ["x", "y", "z", "gate"]
    assert m.params["system"] == "lorenz"
    assert m.params["rate"] == 1.0
    assert m.params["range"] == 2.0
    assert m.params["bipolar"] is False
    assert m.params["seed"] == 1


def test_serialization_round_trip():
    cls = all_module_types()["chaos"]
    m = cls(3, params={"system": "rossler", "rate": 7.5, "seed": 99})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- determinism -----------------------------------------------------------


def test_seeded_determinism_bit_exact():
    a = _run({"rate": 5.0, "seed": 7}, 8192)
    b = _run({"rate": 5.0, "seed": 7}, 8192)
    for k in ("x", "y", "z", "gate"):
        assert np.array_equal(a[k], b[k])


def test_different_seeds_decorrelate():
    """The chaos test: nearby starts diverge — two seeds must not track."""
    a = _run({"rate": 5.0, "seed": 1, "bipolar": True}, 16000)["x"]
    b = _run({"rate": 5.0, "seed": 2, "bipolar": True}, 16000)["x"]
    tail_a = a[8000:].astype(np.float64)
    tail_b = b[8000:].astype(np.float64)
    corr = np.corrcoef(tail_a, tail_b)[0, 1]
    assert abs(corr) < 0.9


def test_live_seed_change_rerolls_to_the_fresh_orbit():
    step = _driver({"rate": 5.0, "seed": 1}, block=256)
    step(256)
    step.ch.params["seed"] = 42
    after = step(256)["x"]
    fresh = _run({"rate": 5.0, "seed": 42}, 256)["x"]
    assert np.array_equal(after, fresh)


# ----- reset -----------------------------------------------------------------


def test_reset_lands_on_the_seeded_start_sample_accurate():
    F = 4096
    reset = np.zeros(F, dtype=np.float32)
    reset[1777:1827] = 1.0
    outs = _run({"rate": 5.0, "seed": 3}, F, reset=reset)
    fresh = _run({"rate": 5.0, "seed": 3}, 256)
    for k in ("x", "y", "z"):
        assert outs[k][1777] == fresh[k][0]


def test_reset_is_block_split_independent():
    F = 4096
    reset = np.zeros(F, dtype=np.float32)
    reset[777:827] = 1.0
    a = _run({"rate": 5.0, "seed": 3}, F, block=64, reset=reset)
    b = _run({"rate": 5.0, "seed": 3}, F, block=1024, reset=reset)
    for k in ("x", "y", "z", "gate"):
        assert np.array_equal(a[k], b[k])


# ----- block-size independence ----------------------------------------------


def test_block_size_independent_bit_exact():
    for system in ("lorenz", "rossler"):
        a = _run({"system": system, "rate": 5.0}, 4096, block=64)
        b = _run({"system": system, "rate": 5.0}, 4096, block=1024)
        for k in ("x", "y", "z", "gate"):
            assert np.array_equal(a[k], b[k])


def test_single_frame_blocks_match():
    a = _run({"rate": 10.0}, 200, block=1)
    b = _run({"rate": 10.0}, 200, block=200)
    for k in ("x", "y", "z", "gate"):
        assert np.array_equal(a[k], b[k])


# ----- bounds / soak ---------------------------------------------------------


def test_long_run_stays_bounded_and_finite():
    for system in ("lorenz", "rossler"):
        outs = _run(
            {"system": system, "rate": 20.0, "range": 2.0}, 100_000, block=1024
        )
        for k in ("x", "y", "z"):
            arr = outs[k]
            assert np.all(np.isfinite(arr))
            assert arr.min() >= 0.0
            assert arr.max() <= 2.0


def test_bipolar_range_mapping():
    uni = _run({"rate": 10.0, "range": 3.0}, 16000)["x"]
    bi = _run({"rate": 10.0, "range": 3.0, "bipolar": True}, 16000)["x"]
    assert uni.min() >= 0.0 and uni.max() <= 3.0
    assert bi.min() < 0.0 and bi.min() >= -3.0 and bi.max() <= 3.0


def test_no_blowups_recorded():
    step = _driver({"rate": 50.0})
    for _ in range(100):
        step(1024)
    st = step.backend._state[step.ch.id]
    assert st.get("blowups", 0) == 0


# ----- rate ------------------------------------------------------------------


def test_rate_scales_orbital_speed():
    """z oscillates roughly once per orbit — peak count scales with rate."""

    def z_peaks(rate):
        z = _run({"rate": rate, "seed": 5}, 32000)["z"].astype(np.float64)
        interior = (z[1:-1] > z[:-2]) & (z[1:-1] > z[2:])
        big = z[1:-1] > np.percentile(z, 60)
        return int(np.sum(interior & big))

    slow, fast = z_peaks(2.0), z_peaks(16.0)
    assert fast > 3 * slow


def test_rate_calibration_is_roughly_orbits_per_second():
    """rate 10 for 2 s ≈ 20 orbits (loose pin, per the spec)."""
    z = _run({"rate": 10.0, "seed": 5}, 2 * SR)["z"].astype(np.float64)
    interior = (z[1:-1] > z[:-2]) & (z[1:-1] > z[2:])
    big = z[1:-1] > np.percentile(z, 60)
    peaks = int(np.sum(interior & big))
    assert 8 <= peaks <= 45


# ----- gates -----------------------------------------------------------------


def test_lorenz_gate_is_the_sign_of_x():
    outs = _run({"rate": 10.0, "bipolar": True}, 16000)
    x = outs["x"].astype(np.float64)
    gate = outs["gate"]
    assert np.array_equal(gate > 0.5, x > 0.0)
    # And it actually switches lobes now and then.
    assert 0 < np.sum(np.abs(np.diff(gate)) > 0.5) < 400


def test_rossler_gate_is_sparse_spikes():
    gate = _run({"system": "rossler", "rate": 10.0}, 64000)["gate"]
    duty = float(np.mean(gate))
    assert 0.005 < duty < 0.4
    assert np.sum(np.diff(gate) > 0.5) >= 3  # several distinct bursts
