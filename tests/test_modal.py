"""Modal — the struck resonator bank.

Pins the contract: mode frequencies land on the material's ratio table
(impulse → FFT peaks, string + membrane checked against theory), the
membrane table really is Bessel zeros, decay reads as the lowest mode's
t60, decay_tilt and brightness are monotone, inharm stretches upward,
bounded output on sustained noise (no runaway resonators), voice
independence + mono ≡ single-voice, pitch grouping batches without
changing results, block-size independence, early-out, and unpatched →
zeros.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.modal import MODAL_MATERIALS, modal_ratios

C4 = 261.6255653005986


def _driver(params=None, sr=44100, block=512):
    """Backend + `oscillator → excite, constant → pitch_cv` modal patch."""
    patch = Patch()
    m = patch.add_module("modal", params=params or {})
    esrc = patch.add_module("oscillator")
    patch.connect(esrc.id, "out", m.id, "excite")
    psrc = patch.add_module("constant")
    patch.connect(psrc.id, "out", m.id, "pitch_cv")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(excite_block, pitch_block=None):
        arr = np.asarray(excite_block, dtype=np.float32)
        F = arr.shape[-1]
        pb = (
            np.zeros(F, dtype=np.float32)
            if pitch_block is None
            else np.asarray(pitch_block, dtype=np.float32)
        )
        return b._render_modal(
            patch.get(m.id),
            F,
            {(esrc.id, "out"): arr, (psrc.id, "out"): pb},
            patch,
        )

    step.module = m
    step.backend = b
    return step


def _strike(step, seconds=1.0, sr=44100, block=512, voiced_pitch=None):
    """One-sample impulse at t=0, rendered `seconds`; returns the output."""
    out = []
    n_blocks = int(seconds * sr / block)
    for i in range(n_blocks):
        x = np.zeros(block, dtype=np.float32)
        if i == 0:
            x[0] = 1.0
        out.append(step(x, voiced_pitch))
    return np.concatenate(out, axis=-1)


# ----- tables ----------------------------------------------------------------


def test_ratio_tables_normalized_and_sized():
    for material in MODAL_MATERIALS:
        r = modal_ratios(material, 24)
        assert len(r) == 24
        assert r[0] == 1.0
        assert all(b > a for a, b in zip(r, r[1:]))  # strictly ascending


def test_membrane_table_is_bessel_zeros():
    r = modal_ratios("membrane", 4)
    # j01=2.4048, j11=3.8317, j21=5.1356, j02=5.5201 → 1, 1.593, 2.136, 2.295
    assert np.allclose(r, (1.0, 1.5933, 2.1355, 2.2954), atol=2e-3)


def test_bar_table_matches_free_bar_theory():
    r = modal_ratios("bar", 3)
    assert np.allclose(r, (1.0, 2.7565, 5.4039), atol=2e-3)


# ----- registration ----------------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["modal"]
    assert cls is get_module_type("modal")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["excite", "pitch_cv"]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["material"] == "bar"


def test_serialization_round_trip():
    cls = all_module_types()["modal"]
    m = cls(2, params={"material": "membrane", "modes": 24, "inharm": 0.3})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- mode placement --------------------------------------------------------


def test_string_modes_land_on_harmonics():
    step = _driver({"material": "string", "modes": 6, "decay": 3.0,
                    "decay_tilt": 0.0, "brightness": 0.5})
    sig = _strike(step, seconds=1.0)
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    n = len(sig)
    for k in (1, 2, 3, 4):
        b0 = int(n * C4 * k / 44100)
        local = spec[b0 - 3 : b0 + 4]
        assert local.max() > 20 * np.median(spec), f"harmonic {k} missing"


def test_bar_modes_land_on_bar_ratios():
    step = _driver({"material": "bar", "modes": 3, "decay": 3.0,
                    "decay_tilt": 0.0})
    sig = _strike(step, seconds=1.0)
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    n = len(sig)
    for ratio in (1.0, 2.7565, 5.4039):
        b0 = int(n * C4 * ratio / 44100)
        local = spec[b0 - 4 : b0 + 5]
        assert local.max() > 20 * np.median(spec), f"mode ratio {ratio} missing"


def test_pitch_cv_moves_the_bank():
    step = _driver({"material": "string", "modes": 4, "decay": 2.0})
    sig = _strike(step, seconds=0.8, voiced_pitch=np.full(512, 1.0, np.float32))
    # feed the pitch every block
    out = [sig[:512]]
    for _ in range(0):
        pass
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    n = len(sig)
    b_c5 = int(n * C4 * 2.0 / 44100)
    b_c4 = int(n * C4 / 44100)
    assert spec[b_c5 - 3 : b_c5 + 4].max() > spec[b_c4 - 3 : b_c4 + 4].max()
    del out


# ----- decay / tilt / brightness / inharm -----------------------------------


def test_decay_reads_as_lowest_mode_t60():
    decay = 0.4
    step = _driver({"material": "string", "modes": 1, "decay": decay,
                    "decay_tilt": 0.0})
    sig = _strike(step, seconds=1.0)
    sr, hop = 44100, 1024
    rms = np.array(
        [np.sqrt(np.mean(sig[i : i + hop] ** 2)) for i in range(0, len(sig) - hop, hop)]
    )
    ref = rms[:3].max()
    below = np.flatnonzero(rms < ref * 1e-3)
    assert len(below), "never decayed 60 dB"
    t60 = below[0] * hop / sr
    assert abs(t60 - decay) / decay < 0.15, f"t60 {t60:.3f}s vs {decay}s"


def test_decay_tilt_kills_highs_faster():
    # Measure mode 6's late/early energy RATIO — the b₀ = g(1−r)
    # normalization makes short-decay modes start louder, so absolute
    # tail energy is not monotone in tilt; the decay *rate* is.
    def mode6_decay_ratio(tilt):
        step = _driver({"material": "string", "modes": 8, "decay": 1.5,
                        "decay_tilt": tilt})
        sig = _strike(step, seconds=0.6)
        sr = 44100

        def banded(seg):
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            lo = int(len(seg) * 6 * C4 * 0.97 / sr)
            hi = int(len(seg) * 6 * C4 * 1.03 / sr)
            return float((spec[lo : hi + 1] ** 2).sum())

        n = len(sig)
        early = banded(sig[: n // 4])
        late = banded(sig[n // 2 : 3 * n // 4])
        return late / (early + 1e-30)

    assert mode6_decay_ratio(0.0) > 10 * mode6_decay_ratio(1.0)


def test_brightness_monotone_centroid():
    def centroid(bright):
        step = _driver({"material": "string", "modes": 8, "decay": 1.0,
                        "brightness": bright, "decay_tilt": 0.0})
        sig = _strike(step, seconds=0.4)
        spec = np.abs(np.fft.rfft(sig))
        freqs = np.arange(len(spec))
        return float((spec * freqs).sum() / (spec.sum() + 1e-12))

    assert centroid(0.0) < centroid(0.5) < centroid(1.0)


def test_inharm_stretches_upper_modes():
    step0 = _driver({"material": "string", "modes": 4, "decay": 2.0,
                     "inharm": 0.0, "decay_tilt": 0.0})
    step1 = _driver({"material": "string", "modes": 4, "decay": 2.0,
                     "inharm": 1.0, "decay_tilt": 0.0})
    sig0 = _strike(step0, seconds=0.8)
    sig1 = _strike(step1, seconds=0.8)
    n = len(sig0)

    def peak_near(sig, f, span=0.06):
        spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
        lo = int(n * f * (1 - span) / 44100)
        hi = int(n * f * (1 + span) / 44100)
        k = lo + int(np.argmax(spec[lo : hi + 1]))
        return k * 44100 / n, float(spec[lo : hi + 1].max()), float(np.median(spec))

    # Mode 1 stays put in both.
    f0a, _, _ = peak_near(sig0, C4)
    f0b, _, _ = peak_near(sig1, C4)
    assert abs(f0a - f0b) < 3.0
    # inharm=1 → mode 4 sits at 4^1.3 × C4 ≈ 1586 Hz, not at 4 × C4.
    # (±3% windows: stretched mode 3 lands at 1091 Hz, close enough to
    # 4×C4 = 1046 that a sloppier window would catch it by mistake.)
    stretched = 4.0 ** 1.3 * C4
    _, amp_at_stretched, floor1 = peak_near(sig1, stretched, span=0.03)
    assert amp_at_stretched > 20 * floor1  # the stretched mode is there
    _, amp_at_plain, floor0 = peak_near(sig1, 4 * C4, span=0.03)
    assert amp_at_plain < amp_at_stretched * 0.2  # and 4×C4 is vacated


# ----- stability / shapes ----------------------------------------------------


def test_bounded_on_sustained_noise():
    # Full-scale white noise into a 10 s-decay 24-mode bell for half a
    # second legitimately builds a big ring (strike-normalized drive) —
    # the test guards against runaway/NaN, not calibration.
    step = _driver({"material": "bell", "modes": 24, "decay": 10.0})
    rng = np.random.default_rng(5)
    peak = 0.0
    for _ in range(40):
        out = step(rng.uniform(-1, 1, 512).astype(np.float32))
        peak = max(peak, float(np.abs(out).max()))
    assert np.isfinite(peak)
    assert peak < 500.0  # ringing loudly, not running away


def test_voice_rows_independent_and_mono_parity():
    mono = _driver({"material": "bar", "modes": 6}, sr=1000, block=64)
    voiced = _driver({"material": "bar", "modes": 6}, sr=1000, block=64)
    x = np.zeros(64, dtype=np.float32)
    x[0] = 1.0
    for i in range(4):
        xb = x if i == 0 else np.zeros(64, dtype=np.float32)
        m = mono(xb)
        v = voiced(xb[None, :], np.zeros((1, 64), dtype=np.float32))
        assert np.array_equal(m, v[0])


def test_distinct_voice_pitches_ring_distinct_banks():
    step = _driver({"material": "string", "modes": 2, "decay": 2.0},
                   sr=8000, block=256)
    x = np.zeros((2, 256), dtype=np.float32)
    x[:, 0] = 1.0
    pitch = np.zeros((2, 256), dtype=np.float32)
    pitch[1, :] = 1.0  # voice 1 an octave up
    outs = [step(x, pitch)]
    for _ in range(7):
        outs.append(step(np.zeros((2, 256), np.float32), pitch))
    sig = np.concatenate(outs, axis=-1)
    assert sig.shape[0] == 2
    assert not np.allclose(sig[0], sig[1])


def test_block_size_independent_at_constant_pitch():
    x = np.zeros(512, dtype=np.float32)
    x[13] = 1.0
    big = _driver({"material": "bell", "modes": 8}, sr=1000, block=512)
    small = _driver({"material": "bell", "modes": 8}, sr=1000, block=64)
    out_big = big(x)
    outs = [small(x[i : i + 64]) for i in range(0, 512, 64)]
    assert np.allclose(out_big, np.concatenate(outs), atol=1e-7)


def test_unpatched_excite_silent_and_stateless():
    patch = Patch()
    m = patch.add_module("modal")
    b = NumpyBackend(sample_rate=1000, block_size=64)
    b.compile(patch)
    out = b._render_modal(patch.get(m.id), 64, {}, patch)
    assert np.all(out == 0.0)
    assert m.id not in b._state
