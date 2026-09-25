"""Quantizer — CV → nearest scale note (1 V/oct, C4 = 0 V).

Pins the contract: pitch-class membership per scale (root-shifted too),
chromatic+hyst0 = semitone rounding (NOT passthrough), hysteresis kills
boundary flutter, `changed` fires once per new note with a block-size-
independent ~5 ms pulse, gated mode samples on rising edges only, custom
tickboxes (empty = chromatic fallback), post-quantize transpose,
voice-aware per-voice state with mono ≡ single-voice parity, block-size
independence, priming (no spurious first-block trigger), and
unpatched → zeros.

Drives the renderer directly with hand-fed buffers (the slew-test
harness pattern).
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules.quantizer import (
    CUSTOM_KEYS,
    QUANTIZER_SCALES,
    SCALE_INTERVALS,
)

SR = 1000  # pulse length = 5 samples exactly


def _driver(params=None, with_gate=False, block=64):
    """Backend + `constant → quantizer` patch; feed blocks via .step()."""
    patch = Patch()
    q = patch.add_module("quantizer", params=params or {})
    src = patch.add_module("constant")
    patch.connect(src.id, "out", q.id, "in")
    gsrc = None
    if with_gate:
        # A gate-kind source (constant is cv-kind and won't connect);
        # its buffer is injected by hand, its own render never runs.
        gsrc = patch.add_module("clock")
        patch.connect(gsrc.id, "out", q.id, "gate")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(cv_block, gate_block=None):
        arr = np.asarray(cv_block, dtype=np.float32)
        F = arr.shape[-1]
        buffers = {(src.id, "out"): arr}
        if gsrc is not None:
            gb = (
                np.zeros(F, dtype=np.float32)
                if gate_block is None
                else np.asarray(gate_block, dtype=np.float32)
            )
            buffers[(gsrc.id, "out")] = gb
        return b._render_quantizer(patch.get(q.id), F, buffers, patch)

    step.q = q
    step.patch = patch
    step.backend = b
    return step


def _st(out):
    """Output CV → semitones."""
    return np.asarray(out) * 12.0


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["quantizer"]
    assert cls is get_module_type("quantizer")
    assert cls.CATEGORY == "CV & Utilities"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["in", "gate"]
    assert [p.name for p in m.output_ports] == ["out", "changed"]
    assert m.params["scale"] == "major"
    assert m.params["root"] == "C"
    for key in CUSTOM_KEYS:
        assert m.params[key] is True


def test_serialization_round_trip():
    cls = all_module_types()["quantizer"]
    m = cls(3, params={"scale": "blues", "root": "F#", "transpose": 7.0})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


def test_every_scale_name_has_intervals():
    for name in QUANTIZER_SCALES:
        if name != "custom":
            assert name in SCALE_INTERVALS


# ----- scale membership ------------------------------------------------------


@pytest.mark.parametrize("scale", [s for s in QUANTIZER_SCALES if s != "custom"])
def test_output_stays_in_scale(scale):
    step = _driver({"scale": scale, "hysteresis": 0.0})
    sweep = np.linspace(-2.0, 2.0, 512, dtype=np.float32)  # ±2 oct
    out = _st(step(sweep)["out"])
    allowed = set(SCALE_INTERVALS[scale])
    for note in np.unique(np.round(out).astype(int)):
        assert note % 12 in allowed, f"{scale}: emitted pc {note % 12}"


def test_root_shifts_the_scale():
    step = _driver({"scale": "major", "root": "D", "hysteresis": 0.0})
    sweep = np.linspace(-1.0, 1.0, 512, dtype=np.float32)
    out = _st(step(sweep)["out"])
    allowed = {(pc + 2) % 12 for pc in SCALE_INTERVALS["major"]}
    for note in np.unique(np.round(out).astype(int)):
        assert note % 12 in allowed


def test_chromatic_hyst0_is_semitone_rounding_not_passthrough():
    step = _driver({"scale": "chromatic", "hysteresis": 0.0})
    x = np.array([0.31, -0.24, 0.049, 0.051], dtype=np.float32) / 12.0 * 12.0
    # feed semitone values 3.72, -2.88, 0.588, 0.612 (as CV = st/12)
    cv = np.array([3.72, -2.88, 0.588, 0.612], dtype=np.float32) / 12.0
    out = _st(step(cv)["out"])
    assert np.array_equal(out, [4.0, -3.0, 1.0, 1.0])
    assert not np.allclose(out / 12.0, cv)  # snapped, not passed through
    del x


# ----- hysteresis ------------------------------------------------------------


def test_hysteresis_holds_through_boundary_wobble():
    # C major: allowed notes 0 and 2; boundary at 1.0 st. A ±4 ct wobble
    # around the boundary must NOT switch with 10 ct hysteresis.
    step = _driver({"scale": "major", "hysteresis": 10.0})
    prime = np.full(8, 0.0, dtype=np.float32)  # hold note 0 first
    step(prime)
    wob = (1.0 + 0.04 * np.sin(np.linspace(0, 20, 256))) / 12.0
    res = step(wob.astype(np.float32))
    assert np.all(_st(res["out"]) == 0.0)
    assert np.all(res["changed"] == 0.0)


def test_zero_hysteresis_flutters_at_the_same_boundary():
    step = _driver({"scale": "major", "hysteresis": 0.0})
    step(np.full(8, 0.0, dtype=np.float32))
    wob = (1.0 + 0.04 * np.sin(np.linspace(0, 20, 256))) / 12.0
    out = _st(step(wob.astype(np.float32))["out"])
    assert len(np.unique(out)) > 1  # crosses between 0 and 2


def test_decisive_move_switches_despite_hysteresis():
    step = _driver({"scale": "major", "hysteresis": 50.0})
    step(np.zeros(8, dtype=np.float32))
    res = step(np.full(64, 2.0 / 12.0, dtype=np.float32))  # dead-on note 2
    assert _st(res["out"])[-1] == 2.0


# ----- changed trigger -------------------------------------------------------


def test_changed_fires_once_per_note_with_5ms_pulse():
    step = _driver({"scale": "chromatic", "hysteresis": 0.0})
    step(np.zeros(16, dtype=np.float32))  # prime at note 0
    cv = np.concatenate(
        [np.zeros(10), np.full(54, 1.0 / 12.0)]
    ).astype(np.float32)
    res = step(cv)
    changed = res["changed"]
    # One rising edge at sample 10, pulse exactly 5 samples (SR 1000).
    assert np.array_equal(np.flatnonzero(changed), np.arange(10, 15))


def test_no_spurious_trigger_on_first_block():
    step = _driver({"scale": "chromatic", "hysteresis": 0.0})
    res = step(np.full(64, 5.0 / 12.0, dtype=np.float32))
    assert np.all(res["changed"] == 0.0)  # primed, not "changed to" 5


def test_changed_pulse_carries_across_blocks():
    step = _driver({"scale": "chromatic", "hysteresis": 0.0}, block=16)
    step(np.zeros(16, dtype=np.float32))
    a = step(
        np.concatenate([np.zeros(14), np.full(2, 3.0 / 12.0)]).astype(np.float32)
    )
    b_ = step(np.full(16, 3.0 / 12.0, dtype=np.float32))
    # 2 pulse samples at the tail of block a, 3 carried into block b.
    assert np.array_equal(np.flatnonzero(a["changed"]), [14, 15])
    assert np.array_equal(np.flatnonzero(b_["changed"]), [0, 1, 2])


# ----- gated mode ------------------------------------------------------------


def test_gated_mode_samples_on_rising_edges_only():
    step = _driver({"scale": "chromatic", "hysteresis": 0.0}, with_gate=True)
    cv = np.linspace(0.0, 11.0, 64, dtype=np.float32) / 12.0  # sweeping input
    gate = np.zeros(64, dtype=np.float32)
    gate[20] = 1.0  # a single one-sample edge
    res = step(cv, gate)
    out = _st(res["out"])
    expected = round(float(cv[20]) * 12.0)
    # Before the edge: primed to nearest(first sample) = 0; after: held.
    assert np.all(out[:20] == 0.0)
    assert np.all(out[20:] == expected)


def test_gated_mode_holds_without_edges():
    step = _driver({"scale": "chromatic"}, with_gate=True)
    cv = np.linspace(0.0, 1.0, 64, dtype=np.float32)
    res = step(cv, np.zeros(64, dtype=np.float32))
    assert np.all(_st(res["out"]) == 0.0)  # never sampled past priming


def test_gated_held_gate_does_not_retrigger_across_blocks():
    step = _driver({"scale": "chromatic"}, with_gate=True, block=32)
    high = np.ones(32, dtype=np.float32)
    cv1 = np.full(32, 4.0 / 12.0, dtype=np.float32)
    cv2 = np.full(32, 9.0 / 12.0, dtype=np.float32)
    r1 = step(cv1, high)  # edge at sample 0 → note 4
    r2 = step(cv2, high)  # gate still high: NO new edge → still note 4
    assert np.all(_st(r1["out"]) == 4.0)
    assert np.all(_st(r2["out"]) == 4.0)


# ----- custom scale / transpose ---------------------------------------------


def test_custom_scale_uses_tickboxes():
    params = {"scale": "custom", "hysteresis": 0.0}
    params.update({key: False for key in CUSTOM_KEYS})
    params["custom_c"] = True
    params["custom_g"] = True
    step = _driver(params)
    sweep = np.linspace(-1.0, 1.0, 512, dtype=np.float32)
    out = _st(step(sweep)["out"])
    for note in np.unique(np.round(out).astype(int)):
        assert note % 12 in {0, 7}


def test_empty_custom_falls_back_to_chromatic():
    params = {"scale": "custom", "hysteresis": 0.0}
    params.update({key: False for key in CUSTOM_KEYS})
    step = _driver(params)
    cv = np.array([3.4, -5.4], dtype=np.float32) / 12.0
    out = _st(step(cv)["out"])
    assert np.array_equal(out, [3.0, -5.0])


def test_transpose_applies_after_quantize():
    step = _driver({"scale": "major", "hysteresis": 0.0, "transpose": 1.0})
    out = _st(step(np.zeros(16, dtype=np.float32))["out"])
    # Note 0 (C) + 1 st = C# — off-scale, proving it's a transposition.
    assert np.all(out == 1.0)


# ----- voices / shapes / blocks ---------------------------------------------


def test_voice_aware_independent_rows():
    step = _driver({"scale": "chromatic", "hysteresis": 0.0})
    x = np.stack(
        [np.full(64, 3.2 / 12.0), np.full(64, -7.4 / 12.0)]
    ).astype(np.float32)
    res = step(x)
    assert res["out"].shape == (2, 64)
    assert np.all(_st(res["out"][0]) == 3.0)
    assert np.all(_st(res["out"][1]) == -7.0)


def test_single_voice_row_matches_mono():
    mono = _driver({"scale": "major", "hysteresis": 10.0})
    voiced = _driver({"scale": "major", "hysteresis": 10.0})
    rng = np.random.default_rng(7)
    for _ in range(4):
        blk = rng.uniform(-1, 1, 64).astype(np.float32)
        m = mono(blk)
        v = voiced(blk[None, :])
        assert np.array_equal(m["out"], v["out"][0])
        assert np.array_equal(m["changed"], v["changed"][0])


def test_block_size_independent():
    rng = np.random.default_rng(11)
    sig = np.cumsum(rng.uniform(-0.02, 0.02, 512)).astype(np.float32)
    big = _driver({"scale": "minor", "hysteresis": 15.0}, block=512)
    small = _driver({"scale": "minor", "hysteresis": 15.0}, block=32)
    out_big = big(sig)["out"]
    outs = [small(sig[i : i + 32])["out"] for i in range(0, 512, 32)]
    assert np.array_equal(out_big, np.concatenate(outs))


def test_unpatched_input_emits_zeros_and_drops_state():
    patch = Patch()
    q = patch.add_module("quantizer")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    res = b._render_quantizer(patch.get(q.id), 64, {}, patch)
    assert np.all(res["out"] == 0.0)
    assert np.all(res["changed"] == 0.0)
    assert q.id not in b._state
