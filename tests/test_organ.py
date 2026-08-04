"""Organ — nine-drawbar additive voice.

Pins the contract: a lone 8' drawbar is BIT-EXACT against the mono sine
oscillator (same phase indexing, same sin, same multiply order — the
strong version of a source's neutral); the drawbar law is ~3 dB per
step; partials land at the classic footage ratios; partials at/above
Nyquist are masked to silence, never aliased; the gate ramp is click
free at click=0 and the key click is seeded-deterministic; the
percussion register single-triggers from silence only (legato does NOT
re-fire); voice rows are independent and a (1, F) render equals mono;
everything is bit-exact across block splits.
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.organ import (
    ORGAN_BARS,
    ORGAN_DEFAULT_BARS,
    ORGAN_FOOTAGES,
    ORGAN_RATIOS,
)

SR = 8000
C4 = 261.6255653005986
RAMP = max(1, round(SR * 0.001))  # backend _ORGAN_RAMP_S


def _driver(params=None, block=256, sr=SR):
    """Direct-renderer harness (octaver-test idiom)."""
    patch = Patch()
    org = patch.add_module("organ", params=params or {})
    kb = patch.add_module("cv_keyboard")
    patch.connect(kb.id, "pitch_cv", org.id, "pitch_cv")
    patch.connect(kb.id, "gate", org.id, "gate")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(pitch, gate):
        frames = np.asarray(gate).shape[-1]
        bufs = {}
        if pitch is not None:
            bufs[(kb.id, "pitch_cv")] = np.asarray(pitch, dtype=np.float32)
        bufs[(kb.id, "gate")] = np.asarray(gate, dtype=np.float32)
        return b._render_organ(patch.get(org.id), frames, bufs, patch)

    step.org = org
    step.patch = patch
    step.backend = b
    return step


def _spec_peak(x, f0, sr=SR, tol=15.0):
    n = len(x)
    spec = np.abs(np.fft.rfft(np.asarray(x, dtype=np.float64) * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    mask = (freqs > f0 - tol) & (freqs < f0 + tol)
    return float(spec[mask].max()) if mask.any() else 0.0


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["organ"]
    assert cls is get_module_type("organ")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["pitch_cv", "gate"]
    assert [p.name for p in m.output_ports] == ["out"]
    for i in range(ORGAN_BARS):
        assert m.params[f"bar{i + 1}"] == ORGAN_DEFAULT_BARS[i]
    assert m.params["click"] == 0.3
    assert m.params["perc"] == "off"
    assert m.params["perc_decay"] == "fast"
    assert m.params["perc_level"] == 0.7
    assert m.params["level"] == 0.5
    assert len(ORGAN_RATIOS) == ORGAN_BARS == len(ORGAN_FOOTAGES)


def test_serialization_round_trip():
    cls = all_module_types()["organ"]
    m = cls(3, params={"bar4": 6, "click": 0.9, "perc": "3rd"})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the neutral pin: lone 8' ≡ oscillator sine ---------------------------


def test_lone_8ft_drawbar_bit_exact_against_sine_oscillator():
    """The source's neutral: one full 8' bar, click 0 → after the 1 ms
    onset ramp the output IS the mono sine oscillator, bit for bit."""
    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    bars["bar3"] = 8  # 8' — ratio 1.0
    step = _driver({**bars, "click": 0.0, "level": 0.5})

    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"freq": C4, "amp": 0.5, "waveform": "sine"}
    )
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)

    F = 4096
    organ_out = step(None, np.ones(F))  # unpatched pitch → C4
    osc_out = b._render_oscillator(patch.get(osc.id), F)
    assert np.array_equal(organ_out[RAMP:], osc_out[RAMP:])


# ----- the drawbar law -------------------------------------------------------


def test_three_db_per_step_law():
    """Each drawbar step is exactly 10^(-3/20) in amplitude."""
    F = 4096

    def rms_at(bar_level):
        bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
        bars["bar3"] = bar_level
        step = _driver({**bars, "click": 0.0, "level": 1.0})
        out = step(None, np.ones(F))
        return float(np.sqrt(np.mean(out[RAMP:].astype(np.float64) ** 2)))

    r8, r5, r1 = rms_at(8), rms_at(5), rms_at(1)
    assert np.isclose(r5 / r8, 10.0 ** (-9.0 / 20.0), rtol=1e-4)
    assert np.isclose(r1 / r8, 10.0 ** (-21.0 / 20.0), rtol=1e-4)

    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    step = _driver({**bars, "click": 0.0})
    assert np.abs(step(None, np.ones(F))).max() == 0.0  # level 0 = silent


def test_partials_land_on_the_footage_ratios():
    """888000000 puts energy at 0.5f, 1.5f and f — and nowhere above."""
    step = _driver({"click": 0.0, "level": 1.0})
    F = 8192
    out = step(None, np.ones(F))
    tail = out[RAMP:]
    floor = _spec_peak(tail, 700.0)  # nothing musical lives here
    assert _spec_peak(tail, 0.5 * C4) > 10 * floor
    assert _spec_peak(tail, 1.5 * C4) > 10 * floor
    assert _spec_peak(tail, 1.0 * C4) > 10 * floor
    # The 4' bar is off: whatever sits at 2f is transient leakage,
    # orders of magnitude below the real partials.
    assert _spec_peak(tail, 2.0 * C4) < 1e-4 * _spec_peak(tail, C4)


def test_highest_bar_is_the_eighth_harmonic():
    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    bars["bar9"] = 8
    step = _driver({**bars, "click": 0.0, "level": 1.0})
    F = 8192
    out = step(None, np.ones(F))
    assert _spec_peak(out[RAMP:], 8.0 * C4) > 10 * _spec_peak(out[RAMP:], C4)


def test_constant_rms_normalisation():
    """Different registrations keep roughly the same RMS."""
    F = 8192

    def rms(params):
        step = _driver({**params, "click": 0.0, "level": 1.0})
        out = step(None, np.ones(F))
        return float(np.sqrt(np.mean(out[RAMP:].astype(np.float64) ** 2)))

    full = {f"bar{i + 1}": 8 for i in range(ORGAN_BARS)}
    jazz = {}  # the default 888000000
    assert np.isclose(rms(full), rms(jazz), rtol=0.02)


def test_partials_at_nyquist_are_masked_not_aliased():
    """A pitch that puts the 1' partial past Nyquist mutes it exactly."""
    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    bars["bar9"] = 8  # 8× the pitch
    step = _driver({**bars, "click": 0.0})
    F = 4096
    # cv = 1.0 → f ≈ 523 Hz → 8f ≈ 4186 Hz ≥ SR/2: masked → pure silence.
    out = step(np.full(F, 1.0), np.ones(F))
    assert np.abs(out).max() == 0.0


# ----- gate ramp & key click -------------------------------------------------


def test_click_zero_onset_is_ramped_not_stepped():
    step = _driver({"click": 0.0})
    F = 2048
    gate = np.zeros(F)
    gate[500:] = 1.0
    out = step(None, gate).astype(np.float64)
    assert np.abs(np.diff(out)).max() < 0.2  # no discontinuity
    assert np.abs(out[:500]).max() == 0.0  # silent before the key


def test_click_is_seeded_and_scales():
    F = 2048
    gate = np.zeros(F)
    gate[500:] = 1.0
    base = _driver({"click": 0.0})(None, gate).astype(np.float64)

    def click_energy(click):
        # The click-0 render subtracts the identical tone path, so the
        # difference IS the click transient.
        out = _driver({"click": click})(None, gate).astype(np.float64)
        return float(np.sum((out - base) ** 2))

    quiet, loud = click_energy(0.2), click_energy(1.0)
    assert loud > 10 * quiet  # amplitude ∝ click → energy ∝ click²

    a = _driver({"click": 0.7})(None, gate)
    b = _driver({"click": 0.7})(None, gate)
    assert np.array_equal(a, b)  # fresh renders identical (seeded)


def test_release_falls_to_exact_silence():
    step = _driver({"click": 0.0})
    F = 4096
    gate = np.zeros(F)
    gate[100:2000] = 1.0
    out = step(None, gate)
    assert np.abs(out[2000 + RAMP :]).max() == 0.0


# ----- percussion ------------------------------------------------------------


def _perc_diff(gates, perc="2nd"):
    """Render a (V, F) gate pattern with the register on and off; the
    difference isolates the percussion strike exactly (the tone paths
    are deterministic and identical)."""
    F = gates.shape[1]
    pitch = np.zeros_like(gates, dtype=np.float32)
    on = _driver({"click": 0.0, "perc": perc, "perc_level": 1.0})
    off = _driver({"click": 0.0, "perc": "off"})
    a = on(pitch, gates).astype(np.float64)
    b = off(pitch, gates).astype(np.float64)
    return (a - b).sum(axis=0)  # perc lives on row 0


def test_percussion_fires_from_silence_only():
    """THE test: a legato addition while a note is held does not
    re-fire the register; a fresh press after silence does."""
    F = 16000  # 2 s at SR 8000 — lets the fast strike die out
    gates = np.zeros((2, F), dtype=np.float32)
    gates[0, 100:4000] = 1.0     # first press: fires
    gates[1, 1500:4000] = 1.0    # legato addition: must NOT re-fire
    gates[1, 12000:] = 1.0       # press from silence: fires again
    d = _perc_diff(gates)

    def env(at, width=300):
        return float(np.abs(d[at : at + width]).max())

    strike = env(100)
    assert strike > 0.1  # the first press struck
    # At the legato press the strike is only the FIRST one's decayed
    # tail — no jump back up.
    assert env(1500) < strike * 0.5
    # Long silence later the tail is dead; the fresh press strikes anew.
    assert env(11000, 900) < 1e-3
    assert env(12000) > 0.1


def test_percussion_harmonic_choice():
    F = 8192
    gates = np.zeros((1, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    d2 = _perc_diff(gates, "2nd")[:4000]
    d3 = _perc_diff(gates, "3rd")[:4000]
    assert _spec_peak(d2, 2.0 * C4) > 5 * _spec_peak(d2, 3.0 * C4)
    assert _spec_peak(d3, 3.0 * C4) > 5 * _spec_peak(d3, 2.0 * C4)


def test_percussion_decay_speed():
    F = 16000
    gates = np.zeros((1, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    fast = _driver({"click": 0.0, "perc": "2nd", "perc_decay": "fast",
                    "perc_level": 1.0})
    slow = _driver({"click": 0.0, "perc": "2nd", "perc_decay": "slow",
                    "perc_level": 1.0})
    base = _driver({"click": 0.0})
    pitch = np.zeros_like(gates, dtype=np.float32)
    b = base(pitch, gates).astype(np.float64).sum(axis=0)
    df = fast(pitch, gates).astype(np.float64).sum(axis=0) - b
    ds = slow(pitch, gates).astype(np.float64).sum(axis=0) - b
    at = 100 + int(0.45 * SR)  # 0.45 s after the strike (> fast t60)
    assert np.abs(ds[at : at + 400]).max() > 5 * np.abs(df[at : at + 400]).max()


# ----- voices ----------------------------------------------------------------


def test_single_voice_row_equals_mono():
    F = 4096
    gate = np.zeros(F, dtype=np.float32)
    gate[100:3000] = 1.0
    pitch = np.full(F, 0.25, dtype=np.float32)
    mono = _driver({"click": 0.5})(pitch, gate)
    voiced = _driver({"click": 0.5})(pitch[None, :], gate[None, :])
    assert voiced.shape == (1, F)
    assert np.array_equal(mono, voiced[0])


def test_per_voice_independence():
    F = 4096
    gates = np.zeros((2, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    gates[1, 100:] = 1.0
    pitches = np.zeros((2, F), dtype=np.float32)
    pitches[1] = 0.5
    duo = _driver({"click": 0.0})(pitches, gates)
    solo0 = _driver({"click": 0.0})(pitches[0], gates[0])
    solo1 = _driver({"click": 0.0})(pitches[1], gates[1])
    assert np.array_equal(duo[0], solo0)
    assert np.array_equal(duo[1], solo1)


def test_unpatched_gate_is_silence():
    patch = Patch()
    org = patch.add_module("organ")
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)
    out = b._render_organ(patch.get(org.id), 512, {}, patch)
    assert out.shape == (512,)
    assert np.abs(out).max() == 0.0


# ----- block-size independence ----------------------------------------------


def test_block_size_independent_bit_exact():
    """Ramps, clicks and the percussion register all carry across any
    block split — bit-exact 64 vs 1024."""
    F = 4096
    gate = np.zeros(F, dtype=np.float32)
    gate[100:1500] = 1.0
    gate[2000:] = 1.0
    pitch = np.full(F, 0.25, dtype=np.float32)

    def chunked(block):
        step = _driver({"click": 0.5, "perc": "2nd"}, block=block)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            e = s + block
            out[s:e] = step(pitch[s:e], gate[s:e])
        return out

    assert np.array_equal(chunked(64), chunked(1024))
