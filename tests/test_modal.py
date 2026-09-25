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

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
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
        )["out"]

    def step_all(excite_block, pitch_block=None):
        """The whole port dict (out / out_l / out_r)."""
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
    step.all = step_all
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
    assert [p.name for p in m.output_ports] == ["out", "out_l", "out_r"]
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
    out = b._render_modal(patch.get(m.id), 64, {}, patch)["out"]
    assert np.all(out == 0.0)
    assert m.id not in b._state


# ----- the 2026-09-11 love pass: position / mallet / spread ------------------

from pysynthrack.modules.modal import mode_pans, strike_comb  # noqa: E402


def _peaks_db(sig, freqs, sr=44100):
    """dB level at each of `freqs` from a Hann-windowed FFT of `sig`."""
    seg = np.asarray(sig, dtype=np.float64)
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    fbin = np.fft.rfftfreq(len(seg), 1 / sr)
    out = []
    for f in freqs:
        i = int(np.argmin(np.abs(fbin - f)))
        out.append(20 * np.log10(max(float(spec[max(0, i - 2):i + 3].max()), 1e-12)))
    return np.array(out)


def test_love_pass_defaults_are_off():
    from pysynthrack.modules.modal import Modal
    assert Modal.DEFAULT_PARAMS["position"] == 0.0
    assert Modal.DEFAULT_PARAMS["mallet"] == 0.0
    assert Modal.DEFAULT_PARAMS["spread"] == 0.0


def test_defaults_render_identically_to_explicit_zeros():
    """The three knobs at 0 must not even touch the numbers: the code
    paths are skipped, not multiplied by 1."""
    plain = _strike(_driver({"material": "bell"}), seconds=0.3)
    explicit = _strike(
        _driver({"material": "bell", "position": 0.0, "mallet": 0.0, "spread": 0.0}),
        seconds=0.3,
    )
    assert np.array_equal(plain, explicit)


# -- position --------------------------------------------------------------------

def test_strike_comb_is_off_at_zero_and_never_all_zero():
    r = modal_ratios("string", 8)
    assert np.array_equal(strike_comb(r, 0.0), np.ones(8))
    assert np.array_equal(strike_comb(r, -1.0), np.ones(8))
    # position 1 on a harmonic string nulls every mode: fall back to ones
    assert np.array_equal(strike_comb(r, 1.0), np.ones(8))


def test_strike_comb_is_the_plucked_string_spectrum():
    r = modal_ratios("string", 8)
    comb = strike_comb(r, 0.5)
    assert np.allclose(comb[1::2], 0.0, atol=1e-12)   # even harmonics gone
    assert np.allclose(comb[0::2], 1.0, atol=1e-12)   # odd ones full
    third = strike_comb(r, 1.0 / 3.0)
    assert third[2] < 1e-9 and third[5] < 1e-9        # 3rd and 6th nulled


def test_position_half_on_a_string_cancels_the_even_harmonics():
    """Rendered claim, two renders: the same string struck everywhere vs
    at its midpoint. Even harmonics drop by >30 dB, odd ones hold."""
    p = {"material": "string", "modes": 8, "decay": 4.0, "decay_tilt": 0.0,
         "brightness": 0.5}
    plain = _strike(_driver(p), seconds=1.0)
    mid = _strike(_driver(dict(p, position=0.5)), seconds=1.0)
    freqs = [C4 * k for k in range(1, 7)]
    a = _peaks_db(plain[2000:], freqs)
    b = _peaks_db(mid[2000:], freqs)
    even = [1, 3, 5]
    odd = [0, 2, 4]
    assert np.all(a[even] - b[even] > 30), (a, b)
    # Odd harmonics hold to within the renormalization (+ a few dB at most).
    assert np.all(np.abs(b[odd] - a[odd]) < 8), (a, b)


def test_position_holds_the_level():
    """The comb is renormalized: striking near the edge is THIN, not
    quiet. Peak amplitude within a factor of two of the plain strike."""
    p = {"material": "bar", "modes": 12}
    plain = float(np.abs(_strike(_driver(p), seconds=0.3)).max())
    for pos in (0.05, 0.3, 0.7, 0.95):
        edge = float(np.abs(_strike(_driver(dict(p, position=pos)), seconds=0.3)).max())
        assert 0.5 * plain < edge < 2.0 * plain, (pos, plain, edge)


def test_position_near_the_edge_is_bright():
    """Just off zero the comb is ∝ ratio: a linear tilt toward the high
    modes — the spectral centroid rises. (On a harmonic string, where
    all eight modes sit in the comb's linear region; a bar's upper modes
    reach ratio 60 and saturate the sine, so its centroid barely moves.)"""
    p = {"material": "string", "modes": 8, "decay_tilt": 0.0}

    def centroid(x):
        spec = np.abs(np.fft.rfft(x)) ** 2
        f = np.fft.rfftfreq(len(x), 1 / 44100)
        return float((spec * f).sum() / spec.sum())

    plain = centroid(_strike(_driver(p), seconds=0.5))
    edge = centroid(_strike(_driver(dict(p, position=0.03)), seconds=0.5))
    assert edge > 1.3 * plain, (plain, edge)


# -- mallet ----------------------------------------------------------------------

def test_soft_mallet_darkens_the_strike():
    """A/B on a noise strike: mallet 1 (cut at the fundamental) leaves the
    high modes far quieter than mallet 0; the fundamental barely moves."""
    p = {"material": "string", "modes": 8, "decay": 3.0, "decay_tilt": 0.0}
    hard = _strike(_driver(p), seconds=0.7)
    soft = _strike(_driver(dict(p, mallet=1.0)), seconds=0.7)
    freqs = [C4, 4 * C4, 8 * C4]
    a = _peaks_db(hard[2000:], freqs)
    b = _peaks_db(soft[2000:], freqs)
    assert abs(a[0] - b[0]) < 4, (a, b)          # fundamental holds
    assert a[1] - b[1] > 8, (a, b)               # 4th harmonic well down
    assert a[2] - b[2] > 14, (a, b)              # 8th further down


def test_mallet_tracks_the_pitch():
    """The same softness an octave up: the SAME relative rolloff at the
    same harmonic numbers, because the cutoff moved with f0. A fixed-Hz
    filter would leave the high note duller than the low one."""
    p = {"material": "string", "modes": 8, "decay": 3.0, "decay_tilt": 0.0,
         "mallet": 0.7}
    # An impulse strike: a flat spectrum, so what is measured is the
    # filter and the bank, not a noise burst's own ripple.
    low = _strike(_driver(p), seconds=0.7, voiced_pitch=np.zeros(512, np.float32))
    high = _strike(_driver(p), seconds=0.7, voiced_pitch=np.ones(512, np.float32))
    rel_low = np.diff(_peaks_db(low[2000:], [C4, 4 * C4]))[0]
    rel_high = np.diff(_peaks_db(high[2000:], [2 * C4, 8 * C4]))[0]
    assert abs(rel_low - rel_high) < 3.0, (rel_low, rel_high)


def test_mallet_is_block_size_independent():
    p = {"material": "bell", "modes": 12, "mallet": 0.6}
    rng = np.random.default_rng(9)
    x = rng.uniform(-1, 1, 4096).astype(np.float32)
    big = _driver(p, block=4096)(x)
    small = _driver(p, block=256)
    parts = [small(x[i:i + 256]) for i in range(0, 4096, 256)]
    assert np.allclose(big, np.concatenate(parts), atol=1e-6)


def test_mallet_state_is_per_voice():
    p = {"material": "bell", "modes": 8, "mallet": 0.8}
    step = _driver(p)
    x = np.zeros((2, 512), dtype=np.float32)
    x[0, 0] = 1.0
    out = step(x)
    step2 = _driver(p)
    mono = step2(x[0])
    assert np.allclose(out[0], mono, atol=1e-7)
    assert np.all(out[1] == 0.0)


# -- spread ----------------------------------------------------------------------

def test_mode_pans_are_centred_at_zero_and_scattered():
    assert np.all(mode_pans(12, 0.0) == 0.0)
    pans = mode_pans(12, 1.0)
    assert pans.min() < -0.8 and pans.max() > 0.8
    assert np.all(np.abs(pans) <= 1.0)
    # not a simple alternation: neighbours are not just sign-flips
    assert not np.allclose(pans[1:], -pans[:-1])
    assert np.allclose(mode_pans(12, 0.5), 0.5 * pans)


def test_spread_zero_outs_are_the_mono_out():
    step = _driver({"material": "bell"})
    x = np.zeros(512, dtype=np.float32)
    x[0] = 1.0
    res = step.all(x)
    assert np.array_equal(res["out_l"], res["out"])
    assert np.array_equal(res["out_r"], res["out"])


def test_spread_puts_modes_on_different_sides():
    """Each mode's L/R balance follows its pan: at full spread a mode panned
    left is louder in out_l than out_r and vice versa."""
    p = {"material": "string", "modes": 6, "decay": 4.0, "decay_tilt": 0.0,
         "spread": 1.0}
    step = _driver(p)
    out = {"out": [], "out_l": [], "out_r": []}
    for i in range(80):
        x = np.zeros(512, dtype=np.float32)
        if i == 0:
            x[0] = 1.0
        res = step.all(x)
        for k in out:
            out[k].append(res[k])
    sig = {k: np.concatenate(v) for k, v in out.items()}
    freqs = [C4 * k for k in range(1, 7)]
    lvl_l = _peaks_db(sig["out_l"][2000:], freqs)
    lvl_r = _peaks_db(sig["out_r"][2000:], freqs)
    pans = mode_pans(6, 1.0)
    for i, pan in enumerate(pans):
        if pan < -0.2:
            assert lvl_l[i] > lvl_r[i] + 3, (i, pan, lvl_l[i], lvl_r[i])
        elif pan > 0.2:
            assert lvl_r[i] > lvl_l[i] + 3, (i, pan, lvl_l[i], lvl_r[i])


def test_spread_keeps_the_mono_out_and_the_power():
    """`out` is untouched by spread, and each mode's L²+R² equals what
    the centre gives (equal-power, ×√2 normalized)."""
    p = {"material": "string", "modes": 6, "decay": 4.0, "decay_tilt": 0.0}
    plain = _strike(_driver(p), seconds=0.5)
    step = _driver(dict(p, spread=1.0))
    out = {"out": [], "out_l": [], "out_r": []}
    for i in range(int(0.5 * 44100 / 512)):
        x = np.zeros(512, dtype=np.float32)
        if i == 0:
            x[0] = 1.0
        res = step.all(x)
        for k in out:
            out[k].append(res[k])
    sig = {k: np.concatenate(v) for k, v in out.items()}
    assert np.array_equal(sig["out"], plain)
    freqs = [C4 * k for k in range(1, 7)]
    mono = 10 ** (_peaks_db(plain[2000:], freqs) / 20)
    l = 10 ** (_peaks_db(sig["out_l"][2000:], freqs) / 20)
    r = 10 ** (_peaks_db(sig["out_r"][2000:], freqs) / 20)
    assert np.allclose(l ** 2 + r ** 2, 2.0 * mono ** 2, rtol=0.15)


def test_spread_is_voice_aware_and_matches_mono():
    p = {"material": "bell", "modes": 8, "spread": 0.7}
    step = _driver(p)
    x = np.zeros((2, 512), dtype=np.float32)
    x[1, 3] = 1.0
    res = step.all(x)
    assert res["out_l"].shape == (2, 512)
    mono = _driver(p).all(x[1])
    assert np.allclose(res["out_l"][1], mono["out_l"], atol=1e-7)
    assert np.allclose(res["out_r"][1], mono["out_r"], atol=1e-7)
    assert np.all(res["out_l"][0] == 0.0)


def test_all_three_together_survive_block_joins():
    p = {"material": "bar", "modes": 12, "position": 0.3, "mallet": 0.5,
         "spread": 0.8}
    rng = np.random.default_rng(11)
    x = rng.uniform(-1, 1, 4096).astype(np.float32)
    big = _driver(p, block=4096).all(x)
    small = _driver(p, block=256)
    parts = [small.all(x[i:i + 256]) for i in range(0, 4096, 256)]
    for k in ("out", "out_l", "out_r"):
        assert np.allclose(big[k], np.concatenate([q[k] for q in parts]), atol=1e-6)
