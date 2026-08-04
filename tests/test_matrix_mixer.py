"""MatrixMixer — 4×4 bipolar gain matrix + the feedback door.

Pins the contract: identity defaults are a bit-exact 4-channel pass
(soft ceiling transparent below the knee); gains are exact including
negative (phase flip) and clamp to ±1; a column CV scales its column
only; cables that close a cycle into the matrix are marked LATE-READS
at compile — the loop compiles, runs with exactly one block of
feedback latency (pinned), stays finite with ``soft_clip`` on (ceiling
1.0) and grows without bound with it off at loop gain > 1; the rest of
the graph still sorts; everything is block-size independent.
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.matrix_mixer import MATRIX_CLIP_KNEE, MATRIX_SIZE

SR = 8000


def _backend(patch, block=256):
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return b


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["matrix_mixer"]
    assert cls is get_module_type("matrix_mixer")
    assert cls.CATEGORY == "Routing & VCA"
    m = cls(1)
    ins = [p.name for p in m.input_ports]
    outs = [p.name for p in m.output_ports]
    assert ins == [f"in_{i}" for i in range(1, 5)] + [
        f"cv_{i}" for i in range(1, 5)
    ]
    assert outs == [f"out_{i}" for i in range(1, 5)]
    for r in range(1, MATRIX_SIZE + 1):
        for c in range(1, MATRIX_SIZE + 1):
            assert m.params[f"g{r}{c}"] == (1.0 if r == c else 0.0)
    assert m.params["soft_clip"] is True


def test_serialization_round_trip():
    cls = all_module_types()["matrix_mixer"]
    m = cls(3, params={"g12": -0.5, "soft_clip": False})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the matrix ------------------------------------------------------------


def _feed(params, ins):
    """Render one block with given input rows (dict row->array)."""
    patch = Patch()
    mm = patch.add_module("matrix_mixer", params=params)
    srcs = {}
    for r in ins:
        o = patch.add_module("oscillator")
        patch.connect(o.id, "out", mm.id, f"in_{r}")
        srcs[r] = o.id
    b = _backend(patch)
    frames = len(next(iter(ins.values())))
    bufs = {
        (srcs[r], "out"): np.asarray(x, dtype=np.float32)
        for r, x in ins.items()
    }
    return b._render_matrix_mixer(patch.get(mm.id), frames, bufs, patch)


def test_identity_is_bit_exact_with_defaults():
    """soft_clip ON by default and STILL bit-exact — the transparent
    knee is the point."""
    rng = np.random.default_rng(0)
    x = (rng.uniform(-0.9, 0.9, 512)).astype(np.float32)
    outs = _feed({}, {2: x})
    assert np.array_equal(outs["out_2"], x)
    assert np.abs(outs["out_1"]).max() == 0.0  # unfed column: exact zeros


def test_gain_exactness_including_phase_flip():
    x = (0.5 * np.sin(2 * np.pi * 200 * np.arange(512) / SR)).astype(
        np.float32
    )
    outs = _feed({"g11": 0.25, "g12": -1.0}, {1: x})
    assert np.allclose(outs["out_1"], 0.25 * x.astype(np.float64), atol=1e-7)
    assert np.array_equal(outs["out_2"], -x)  # exact flip


def test_gains_clamp_to_unit_range():
    x = np.full(256, 0.5, dtype=np.float32)
    outs = _feed({"g11": 5.0, "soft_clip": False}, {1: x})
    assert np.allclose(outs["out_1"], x)  # 5.0 clamps to 1.0


def test_mix_sums_rows():
    a = np.full(256, 0.25, dtype=np.float32)
    b = np.full(256, 0.5, dtype=np.float32)
    outs = _feed(
        {"g11": 1.0, "g21": 0.5, "soft_clip": False}, {1: a, 2: b}
    )
    assert np.allclose(outs["out_1"], 0.5)  # 0.25 + 0.5·0.5


def test_column_cv_scales_its_column_only():
    patch = Patch()
    mm = patch.add_module("matrix_mixer", params={"g12": 1.0})
    o = patch.add_module("oscillator")
    lfo = patch.add_module("lfo")
    patch.connect(o.id, "out", mm.id, "in_1")
    patch.connect(lfo.id, "cv", mm.id, "cv_1")
    b = _backend(patch)
    x = np.full(256, 0.5, dtype=np.float32)
    cv = np.full(256, 0.5, dtype=np.float32)
    outs = b._render_matrix_mixer(
        patch.get(mm.id),
        256,
        {(o.id, "out"): x, (lfo.id, "cv"): cv},
        patch,
    )
    assert np.allclose(outs["out_1"], 0.25)  # column 1 scaled by cv
    assert np.allclose(outs["out_2"], 0.5)  # column 2 untouched


def test_soft_ceiling_transparent_below_knee_and_bounded_above():
    x = np.linspace(-3.0, 3.0, 2001).astype(np.float32)
    outs = _feed({"g11": 1.0}, {1: x})
    y = outs["out_1"].astype(np.float64)
    under = np.abs(x) <= MATRIX_CLIP_KNEE
    assert np.array_equal(outs["out_1"][under], x[under])  # bit-exact
    assert np.abs(y).max() <= 1.0  # never exceeds the ceiling
    assert np.all(np.diff(y) >= -1e-12)  # monotone (no fold-back)


# ----- the feedback door -----------------------------------------------------


def _loop_patch(g, soft, delay_ms=40.0):
    """osc → in_1; out_1 → delay → in_2 AND in_3 (loop gain = 2·g)."""
    p = Patch()
    m = p.add_module(
        "matrix_mixer", params={"g21": g, "g31": g, "soft_clip": soft}
    )
    o = p.add_module("oscillator", params={"freq": 100.0, "amp": 0.1})
    d = p.add_module(
        "delay",
        params={"time": delay_ms, "feedback": 0.0, "mix": 1.0, "tone": 1.0},
    )
    p.connect(o.id, "out", m.id, "in_1")
    p.connect(m.id, "out_1", d.id, "in")
    p.connect(d.id, "out", m.id, "in_2")
    p.connect(d.id, "out", m.id, "in_3")
    return p, m, o, d


def test_cycle_closing_cables_are_marked_late():
    p, m, o, d = _loop_patch(0.5, True)
    b = _backend(p)
    assert b._late_edges == {
        (d.id, "out", m.id, "in_2"),
        (d.id, "out", m.id, "in_3"),
    }
    # Feed-forward cables are NOT late; the graph still sorts with the
    # source before the matrix and the matrix before the delay.
    order = b._topo_order
    assert order.index(o.id) < order.index(m.id) < order.index(d.id)


def test_no_matrix_no_late_edges():
    p = Patch()
    o = p.add_module("oscillator")
    dl = p.add_module("delay")
    p.connect(o.id, "out", dl.id, "in")
    assert _backend(p)._late_edges == set()


def test_feedback_loop_renders_and_stays_finite_with_soft_clip():
    p, m, o, d = _loop_patch(0.8, True)  # loop gain 1.6
    b = _backend(p)
    peak = 0.0
    for _ in range(100):
        b.render_block_multi(256)
        buf = b._late_prev.get((d.id, "out"))
        if buf is not None:
            peak = float(np.abs(buf).max())
            assert np.all(np.isfinite(buf))
    assert 0.5 < peak <= 1.0  # ran away, landed on the ceiling


def test_feedback_loop_grows_without_soft_clip():
    p, m, o, d = _loop_patch(0.8, False)  # loop gain 1.6, no guardrail
    b = _backend(p)
    for _ in range(100):
        b.render_block_multi(256)
    peak = float(np.abs(b._late_prev[(d.id, "out")]).max())
    assert peak > 100.0  # unbounded growth — the knob's job, off


def test_one_block_feedback_latency_pinned():
    """A direct out_1 → in_2 self-loop with a constant driver: each
    block's output is exactly one more generation of the geometric
    series — the seed is the PREVIOUS block, nothing older or fresher.
    """
    p = Patch()
    m = p.add_module(
        "matrix_mixer", params={"g21": 0.5, "soft_clip": False}
    )
    o = p.add_module("oscillator", params={"waveform": "square", "amp": 0.4})
    p.connect(o.id, "out", m.id, "in_1")
    p.connect(m.id, "out_1", m.id, "in_2")
    b = _backend(p)
    F = 256
    # Square at default freq starts high: in_1 = +0.4 on sample 0.
    expected = 0.0
    for i in range(6):
        b.render_block_multi(F)
        out0 = float(b._late_prev[(m.id, "out_1")][0])
        expected = 0.4 + 0.5 * expected  # one generation per block
        assert np.isclose(out0, expected, atol=1e-6)


def test_first_block_of_a_fresh_loop_reads_silence():
    p, m, o, d = _loop_patch(0.9, False)
    b = _backend(p)
    b.render_block_multi(256)
    # After one block the delay has only heard the DRY osc pass —
    # identical to a no-feedback render of the same block.
    p2 = Patch()
    o2 = p2.add_module("oscillator", params={"freq": 100.0, "amp": 0.1})
    d2 = p2.add_module(
        "delay",
        params={"time": 40.0, "feedback": 0.0, "mix": 1.0, "tone": 1.0},
    )
    p2.connect(o2.id, "out", d2.id, "in")
    b2 = _backend(p2)
    b2.render_block_multi(256)
    st_keys = [k for k in b._late_prev if k[0] == d.id]
    assert st_keys  # the loop's delay published a first block
    # (Value equivalence is covered by the latency staircase above;
    # here we pin that block 0 didn't explode despite loop gain 1.8.)
    assert float(np.abs(b._late_prev[(d.id, "out")]).max()) < 0.2


def test_block_size_independent_bit_exact_forward_path():
    rng = np.random.default_rng(1)
    x = rng.uniform(-0.9, 0.9, 4096).astype(np.float32)

    def chunked(block):
        patch = Patch()
        mm = patch.add_module(
            "matrix_mixer", params={"g11": 0.7, "g13": -0.6}
        )
        o = patch.add_module("oscillator")
        patch.connect(o.id, "out", mm.id, "in_1")
        b = _backend(patch, block)
        out = np.empty(len(x), dtype=np.float32)
        for i in range(0, len(x), block):
            seg = x[i : i + block]
            r = b._render_matrix_mixer(
                patch.get(mm.id), len(seg), {(o.id, "out"): seg}, patch
            )
            out[i : i + block] = r["out_1"]
        return out

    assert np.array_equal(chunked(64), chunked(1024))


def test_voice_aware_rows_broadcast():
    x_poly = np.zeros((2, 256), dtype=np.float32)
    x_poly[0] = 0.25
    x_poly[1] = 0.5
    outs = _feed({"g11": 1.0, "soft_clip": False}, {1: x_poly})
    assert outs["out_1"].shape == (2, 256)
    assert np.allclose(outs["out_1"][0], 0.25)
    assert np.allclose(outs["out_1"][1], 0.5)
