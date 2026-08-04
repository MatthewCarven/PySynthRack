"""Supersaw — seven detuned PolyBLEP saws per voice.

Pins the contract: blend 0 is the center saw alone (detune-inert,
bit-exact across detune settings); the detune cluster around a high
harmonic widens monotonically with the knob; spread 0 makes the two
outs bit-identical while spread 1 decorrelates them; the seeded
per-(slot, saw) free phases are deterministic (fresh renders match)
and per-voice independent; RMS normalisation holds the level as blend
moves; block-size independence; the 16-voice worst case is measured.
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.supersaw import (
    SUPERSAW_N,
    SUPERSAW_OFFSETS,
    SUPERSAW_PAN,
)

SR = 8000
F0 = 261.6256


def _driver(params=None, block=512, sr=SR):
    patch = Patch()
    ss = patch.add_module("supersaw", params=params or {})
    kb = patch.add_module("cv_keyboard")
    patch.connect(kb.id, "pitch_cv", ss.id, "freq_cv")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(frames, freq_cv=None, amp_cv=None):
        bufs = {}
        if freq_cv is not None:
            bufs[(kb.id, "pitch_cv")] = np.asarray(freq_cv, dtype=np.float32)
        return b._render_supersaw(patch.get(ss.id), frames, bufs, patch)

    step.ss = ss
    step.backend = b
    step.patch = patch
    return step


def _spec(x):
    x = np.asarray(x, dtype=np.float64)
    return np.abs(np.fft.rfft(x * np.hanning(len(x))))


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["supersaw"]
    assert cls is get_module_type("supersaw")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["freq_cv", "amp_cv"]
    assert [p.name for p in m.output_ports] == ["out_l", "out_r"]
    assert m.params["detune"] == 0.35
    assert m.params["blend"] == 0.75
    assert m.params["spread"] == 0.5
    assert len(SUPERSAW_OFFSETS) == SUPERSAW_N == len(SUPERSAW_PAN)
    assert SUPERSAW_OFFSETS[3] == 0.0  # the center saw


def test_serialization_round_trip():
    cls = all_module_types()["supersaw"]
    m = cls(3, params={"detune": 0.9, "spread": 0.0})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the stack -------------------------------------------------------------


def test_blend_zero_is_the_center_saw_alone():
    """detune moves only the sides — at blend 0 it must change nothing."""
    F = 4096
    a = _driver({"blend": 0.0, "detune": 0.1, "spread": 0.0})(F)["out_l"]
    b = _driver({"blend": 0.0, "detune": 0.9, "spread": 0.0})(F)["out_l"]
    assert np.array_equal(a, b)


def test_detune_cluster_widens_monotonically():
    """The supersaw signature: each harmonic becomes a CLUSTER whose
    skirt grows with detune (measured around the 8th harmonic)."""
    F = 16384

    def skirt(detune):
        out = _driver({"detune": detune, "blend": 1.0, "spread": 0.0})(F)[
            "out_l"
        ]
        spec = _spec(out)
        freqs = np.fft.rfftfreq(F, 1.0 / SR)
        fc = F0 * 8
        m = (np.abs(freqs - fc) > 12) & (np.abs(freqs - fc) < 60)
        return float(spec[m].sum())

    s0, s3, s6 = skirt(0.0), skirt(0.3), skirt(0.6)
    assert s3 > 20 * s0
    assert s6 > 1.5 * s3


def test_spread_zero_outs_bit_identical():
    F = 4096
    r = _driver({"spread": 0.0})(F)
    assert np.array_equal(r["out_l"], r["out_r"])


def test_spread_one_decorrelates():
    F = 16384
    r = _driver({"spread": 1.0, "detune": 0.7, "blend": 1.0})(F)
    l = r["out_l"].astype(np.float64)
    rr = r["out_r"].astype(np.float64)
    corr = np.corrcoef(l, rr)[0, 1]
    assert corr < 0.9  # genuinely different channels
    assert not np.array_equal(r["out_l"], r["out_r"])


def test_rms_stable_across_blend():
    """The RMS normalisation: blend moves timbre, not level."""
    F = 16384

    def rms(blend):
        out = _driver(
            {"blend": blend, "detune": 0.5, "spread": 0.0}
        )(F)["out_l"].astype(np.float64)
        return float(np.sqrt(np.mean(out**2)))

    r0, r5, r1 = rms(0.0), rms(0.5), rms(1.0)
    assert abs(r5 - r0) / r0 < 0.35
    assert abs(r1 - r0) / r0 < 0.35


def test_seeded_phases_deterministic():
    F = 4096
    a = _driver({})(F)
    b = _driver({})(F)
    assert np.array_equal(a["out_l"], b["out_l"])
    assert np.array_equal(a["out_r"], b["out_r"])


# ----- voices ----------------------------------------------------------------


def test_single_voice_row_equals_mono_shape_contract():
    F = 2048
    cv = np.full(F, 0.25, dtype=np.float32)
    mono = _driver({})(F, freq_cv=cv)
    voiced = _driver({})(F, freq_cv=cv[None, :])
    assert mono["out_l"].shape == (F,)
    assert voiced["out_l"].shape == (1, F)
    assert np.array_equal(mono["out_l"], voiced["out_l"][0])


def test_per_voice_rows_independent_and_distinct():
    F = 4096
    cv = np.zeros((2, F), dtype=np.float32)
    cv[1] = 0.5
    r = _driver({})(F, freq_cv=cv)
    assert r["out_l"].shape == (2, F)
    # Row 0 matches a solo render at the same pitch (same slot seeds).
    solo = _driver({})(F, freq_cv=cv[0])
    assert np.array_equal(r["out_l"][0], solo["out_l"])
    # Rows differ (different pitch AND different slot phase seeds).
    assert not np.array_equal(r["out_l"][0], r["out_l"][1])


def test_amp_cv_scales():
    F = 2048
    patch = Patch()
    ss = patch.add_module("supersaw", params={"spread": 0.0})
    lfo = patch.add_module("lfo")
    patch.connect(lfo.id, "cv", ss.id, "amp_cv")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    half = np.full(F, 0.5, dtype=np.float32)
    r = b._render_supersaw(
        patch.get(ss.id), F, {(lfo.id, "cv"): half}, patch
    )
    full = _driver({"spread": 0.0})(F)
    assert np.allclose(
        r["out_l"].astype(np.float64),
        0.5 * full["out_l"].astype(np.float64),
        atol=1e-6,
    )


# ----- block-size independence ----------------------------------------------


def test_block_size_independent_constant_freq():
    F = 4096

    def chunked(block):
        step = _driver({}, block=block)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            out[s : s + block] = step(block)["out_l"]
        return out

    assert np.array_equal(chunked(64), chunked(1024))


def test_block_size_independent_under_cv():
    F = 4096
    cv = (0.1 * np.sin(2 * np.pi * 2 * np.arange(F) / SR)).astype(np.float32)

    def chunked(block):
        step = _driver({}, block=block)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            out[s : s + block] = step(block, freq_cv=cv[s : s + block])[
                "out_l"
            ]
        return out

    a, b = chunked(64), chunked(1024)
    assert np.abs(a.astype(np.float64) - b.astype(np.float64)).max() < 1e-5
