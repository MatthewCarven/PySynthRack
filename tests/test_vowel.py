"""Vowel — the formant filter.

Pins the contract: the classic five-formant table's shape and ordering;
the pure `vowel_formants` helper (geometric frequency interpolation, dB
levels, clamping); white noise through each vowel peaks within ±15% of
that vowel's F1 and shows F2; `vowel_cv` at depth 2 == the knob moved by
2·cv, bit-exact; `resonance` sharpens monotonically; `gain` +6.02 dB
doubles; `mix` 0 is bit-exact dry (the input buffer itself) and 0.5 the
half blend; voice-aware `(V, F)` in/out with a single row ≡ mono;
block-size independence at a constant vowel; the clamp; the voice combo
offers the five voices; the widget sweep; the example.

The love pass of 2026-09-22, three follow-ons: (a) `vowel_cv`'s block
mean is taken in float64, so a CONSTANT cv is block-size exact (it was
not: the float32 accumulation reads 0.29999998 over 64 samples and
0.30000001 over 512), and a non-finite cv is ignored rather than
silently collapsed to A by Python's non-propagating `min`/`max`;
(b) `cv_rate` `sample` follows the CV per sample by quantising the
modulation into runs of constant vowel -- pinned against a TRUE
per-sample rebuild (-62 dB), against clicks (its max sample step is
smaller than the block mode's), by its sidebands (the 2nd-order pair
9-11 dB up on the block mode's), by the run collapsing (a constant cv
is one coefficient set; a clamped sweep uses fewer than an unclamped
one), and by being the block render BIT FOR BIT whenever nothing moves;
(c) `voice` `custom` takes its five frequencies from `f1`..`f5` and the
tenor table's bandwidths and levels, measured: the -3 dB bandwidth stays
~40 Hz from f1 300 Hz to 2500 Hz (so Q runs 7.4 -> 63) and the `vowel`
knob moves the bandwidth, not the peak.

The formant shift (love pass, 2026-09-20): `formant` +12 / -12 puts
noise-through-A's peak at 2x / 0.5x the table F1, measured; `formant_cv`
+1 at depth 1 == the knob at +12 bit-exact, depth 0 and cv 0 are the
unshifted render bit-exact; the shift is constant-Q (bass I's F1 at
resonance 2 keeps its -3 dB Q and its quarter-octave peak-to-skirt at
+12, measured on the impulse response); `formant` 0 unpatched is
bit-exact with a verbatim pre-shift oracle; one shift per block for
every voice; 64 vs 512 at a constant shift; the +-4 octave clip and the
0.45 sr park read off the coefficients; the widgets; the example.
"""
from __future__ import annotations

import math
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.vowel import (
    FORMANTS,
    N_FORMANTS,
    VOWEL_CUSTOM_DEFAULT_FREQS,
    VOWEL_CUSTOM_FREQ_MAX,
    VOWEL_CUSTOM_FREQ_MIN,
    VOWEL_CV_RATES,
    VOWEL_NAMES,
    VOWEL_VOICE_CHOICES,
    VOWEL_VOICES,
    vowel_formants,
)

SR = 44100


def _driver(params=None, cv=False, sr=SR, block=512, cv_port="vowel_cv"):
    patch = Patch()
    m = patch.add_module("vowel", params=params or {})
    src = patch.add_module("noise")
    patch.connect(src.id, "out", m.id, "in")
    keys = {"in": (src.id, "out")}
    if cv:
        c = patch.add_module("constant")
        patch.connect(c.id, "out", m.id, cv_port)
        keys["cv"] = (c.id, "out")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(x, cv_block=None):
        bufs = {keys["in"]: np.asarray(x, dtype=np.float32)}
        if cv_block is not None:
            bufs[keys["cv"]] = np.asarray(cv_block, dtype=np.float32)
        return b._render_vowel(patch.get(m.id), np.asarray(x).shape[-1], bufs, patch)

    step.module = m
    step.backend = b
    return step


def _noise(n, seed=1):
    return np.random.default_rng(seed).uniform(-1.0, 1.0, n).astype(np.float32)


def _render(params=None, seconds=1.0, block=512, cv_value=None, x=None,
            cv_port="vowel_cv", cv_sig=None, want_step=False):
    """Render x through a vowel. ``cv_value`` is a constant on the jack,
    ``cv_sig`` a per-sample CV signal (what ``cv_rate`` "sample" wants)."""
    step = _driver(params, cv=cv_value is not None or cv_sig is not None,
                   block=block, cv_port=cv_port)
    if x is None:
        x = _noise(int(SR * seconds))
    out = []
    for i in range(0, len(x) - block + 1, block):
        if cv_sig is not None:
            cvb = np.asarray(cv_sig[i:i + block], dtype=np.float32)
        elif cv_value is not None:
            cvb = np.full(block, cv_value, dtype=np.float32)
        else:
            cvb = None
        out.append(step(x[i:i + block], cvb))
    y = np.concatenate(out)
    return (y, step) if want_step else y


def _tri(n, hz):
    """A bipolar triangle sample by sample -- the CV an LFO would send."""
    ph = (np.arange(n) * (hz / SR)) % 1.0
    return (4.0 * np.abs(ph - 0.5) - 1.0).astype(np.float32)


def _sine(n, hz, amp=0.8):
    return (amp * np.sin(2 * np.pi * hz * np.arange(n) / SR)).astype(np.float32)


def _spectrum(sig):
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    return spec, np.fft.rfftfreq(len(sig), 1.0 / SR)


def _smooth_peak_hz(sig, lo=100.0, hi=6000.0, width=25):
    spec, fr = _spectrum(sig)
    sm = np.convolve(spec, np.ones(width) / width, mode="same")
    band = (fr >= lo) & (fr <= hi)
    return float(fr[band][np.argmax(sm[band])])


# ----- registration / the table ------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["vowel"]
    assert cls is get_module_type("vowel")
    assert cls.CATEGORY == "Filters & EQ"
    m = cls(1)
    assert [(p.name, p.signal_kind) for p in m.input_ports] == [
        ("in", "audio"), ("vowel_cv", "cv"), ("formant_cv", "cv")]
    assert [(p.name, p.signal_kind) for p in m.output_ports] == [("out", "audio")]
    assert m.params["voice"] == "tenor" and m.params["mix"] == 1.0 and m.params["vowel"] == 0.0
    assert m.params["formant"] == 0.0 and m.params["formant_cv_depth"] == 1.0
    assert VOWEL_VOICES == ("soprano", "alto", "countertenor", "tenor", "bass")
    assert VOWEL_NAMES == ("a", "e", "i", "o", "u")


def test_serialization_round_trip():
    cls = all_module_types()["vowel"]
    m = cls(2, params={"vowel": 2.5, "voice": "bass", "resonance": 1.5, "mix": 0.4,
                       "formant": -7.0, "formant_cv_depth": 0.5})
    assert cls.from_dict(m.to_dict()).params == m.params


def test_pre_shift_patch_loads_with_the_default_shift():
    d = Patch()
    d.add_module("vowel", params={"vowel": 1.0})
    raw = d.to_dict()
    for m in raw["modules"]:
        m["params"].pop("formant", None)
        m["params"].pop("formant_cv_depth", None)
    vw = next(m for m in Patch.from_dict(raw) if m.TYPE == "vowel")
    assert vw.params["formant"] == 0.0 and vw.params["formant_cv_depth"] == 1.0


def test_the_formant_table_is_complete_and_ordered():
    assert set(FORMANTS) == set(VOWEL_VOICES)
    for voice in VOWEL_VOICES:
        assert set(FORMANTS[voice]) == set(VOWEL_NAMES)
        for vowel in VOWEL_NAMES:
            freqs, dbs, bws = FORMANTS[voice][vowel]
            assert len(freqs) == len(dbs) == len(bws) == N_FORMANTS
            assert list(freqs) == sorted(freqs) and freqs[0] >= 250 and freqs[-1] <= 5000
            assert dbs[0] == 0 and all(d <= 0 for d in dbs)       # F1 is the reference
            assert all(20 <= bw <= 250 for bw in bws)


# ----- the helper ------------------------------------------------------------------


def test_vowel_formants_returns_the_table_rows_at_integers():
    for v, name in enumerate(VOWEL_NAMES):
        freqs, gains, bws = vowel_formants("tenor", float(v))
        f, d, b = FORMANTS["tenor"][name]
        assert freqs == pytest.approx(list(f))
        assert gains == pytest.approx([10 ** (x / 20) for x in d])
        assert bws == pytest.approx(list(b))


def test_vowel_formants_interpolates_geometrically_between_neighbours():
    freqs, gains, bws = vowel_formants("tenor", 0.5)
    fa, da, ba = FORMANTS["tenor"]["a"]
    fe, de, be = FORMANTS["tenor"]["e"]
    assert freqs[0] == pytest.approx(math.sqrt(fa[0] * fe[0]))       # 650 -> 400: 509.9
    assert bws[0] == pytest.approx((ba[0] + be[0]) / 2)
    assert gains[1] == pytest.approx(10 ** (((da[1] + de[1]) / 2) / 20))


def test_vowel_formants_clamps_and_falls_back():
    assert vowel_formants("tenor", 9.0) == vowel_formants("tenor", 4.0)
    assert vowel_formants("tenor", -3.0) == vowel_formants("tenor", 0.0)
    assert vowel_formants("kazoo", 1.0) == vowel_formants("tenor", 1.0)


# ----- the filter ----------------------------------------------------------------


@pytest.mark.parametrize("v,name", list(enumerate(VOWEL_NAMES)))
def test_white_noise_through_each_vowel_peaks_at_f1_and_shows_f2(v, name):
    y = _render({"vowel": float(v), "voice": "tenor", "gain": 0.0}, seconds=2.0)
    f1, f2 = FORMANTS["tenor"][name][0][:2]
    peak = _smooth_peak_hz(y[SR // 4:])
    assert abs(peak - f1) < 0.15 * f1, (name, peak, f1)
    # F2 is a local maximum: higher than the trough between F1 and F2.
    spec, fr = _spectrum(y[SR // 4:])
    sm = np.convolve(spec, np.ones(25) / 25, mode="same")
    at_f2 = sm[(fr > f2 * 0.95) & (fr < f2 * 1.05)].max()
    trough = sm[(fr > (f1 + f2) / 2 * 0.95) & (fr < (f1 + f2) / 2 * 1.05)].min()
    assert at_f2 > 1.5 * trough


def test_vowel_cv_moves_the_knob_bit_exact():
    with_cv = _render({"vowel": 0.5, "cv_depth": 2.0}, cv_value=0.75)      # 0.5 + 1.5 = 2.0
    knob = _render({"vowel": 2.0})
    assert np.array_equal(with_cv, knob)
    off = _render({"vowel": 0.5, "cv_depth": 0.0}, cv_value=0.75)
    assert np.array_equal(off, _render({"vowel": 0.5}))


def test_cv_past_u_holds_u():
    assert np.array_equal(_render({"vowel": 3.0}, cv_value=5.0), _render({"vowel": 4.0}))


def test_resonance_sharpens_the_peaks_monotonically():
    ratios = []
    for res in (0.5, 1.0, 2.0):
        y = _render({"vowel": 0.0, "resonance": res, "gain": 0.0}, seconds=2.0)[SR // 4:]
        spec, fr = _spectrum(y)
        peak = spec[(fr > 600) & (fr < 700)].max()
        skirt = spec[(fr > 1300) & (fr < 1500)].mean()
        ratios.append(peak / skirt)
    assert ratios[0] < ratios[1] < ratios[2]


def test_gain_is_makeup_in_db():
    x = _noise(SR // 2)
    a = _render({"gain": 0.0}, x=x)
    b = _render({"gain": 6.0205999}, x=x)
    assert np.allclose(b, 2.0 * a, atol=1e-5)


def test_mix_zero_is_the_input_buffer_itself_and_half_is_the_blend():
    step = _driver({"mix": 0.0})
    x = _noise(512)
    out = step(x)
    assert out is not None and np.array_equal(out, x)
    assert step.module.id not in step.backend._state           # nothing ran
    x2 = _noise(512 * 40, seed=3)                             # a whole number of blocks
    wet = _render({"mix": 1.0}, x=x2)
    half = _render({"mix": 0.5}, x=x2)
    assert np.allclose(half, 0.5 * x2 + 0.5 * wet, atol=1e-6)


def test_unpatched_input_is_silent_and_stateless():
    patch = Patch()
    m = patch.add_module("vowel")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    out = b._render_vowel(patch.get(m.id), 64, {}, patch)
    assert np.all(out == 0.0) and m.id not in b._state


# ----- voices / blocks -------------------------------------------------------------


def test_voiced_input_gives_voiced_output_with_independent_rows():
    step = _driver({"vowel": 1.0})
    x = np.stack([_noise(512, 1), np.zeros(512, np.float32), _noise(512, 2)])
    out = step(x)
    assert out.shape == (3, 512)
    assert np.all(out[1] == 0.0) and np.any(out[0] != 0.0) and not np.array_equal(out[0], out[2])


def test_single_voice_row_matches_mono():
    mono = _driver({"vowel": 2.0})
    voiced = _driver({"vowel": 2.0})
    for seed in (1, 2, 3):
        x = _noise(512, seed)
        assert np.array_equal(mono(x), voiced(x[None, :])[0])


def test_block_size_independent_at_a_constant_vowel():
    x = _noise(4096, 7)
    big = _render({"vowel": 1.5, "resonance": 1.3}, x=x, block=4096)
    small = _render({"vowel": 1.5, "resonance": 1.3}, x=x, block=64)
    assert np.array_equal(big, small)


# ----- the formant shift (the child / giant knob) --------------------------------


def _reference_unshifted(params, x, block=512):
    """The pre-shift filter, verbatim (2026-09-19): five RBJ constant-peak
    bandpasses at the table's F/BW with Q = F/BW x resonance, F clamped
    at 0.45 sr, lfilter along the last axis with the state carried, summed
    with the table's gains, `gain` dB, `mix` blend. The oracle the module
    must match bit for bit at `formant` 0 with nothing on the jack."""
    from scipy.signal import lfilter

    voice = params.get("voice", "tenor")
    vowel = float(params.get("vowel", 0.0))
    resonance = float(params.get("resonance", 1.0))
    gain = 10.0 ** (float(params.get("gain", 6.0)) / 20.0)
    mix = float(params.get("mix", 1.0))
    freqs, gains, bws = vowel_formants(voice, vowel)
    coefs = []
    for k in range(N_FORMANTS):
        f = min(freqs[k], 0.45 * SR)
        q = max(0.1, (f / bws[k]) * resonance)
        w0 = 2.0 * np.pi * f / SR
        alpha = np.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        coefs.append((np.array([alpha / a0, 0.0, -alpha / a0]),
                      np.array([1.0, -2.0 * np.cos(w0) / a0, (1.0 - alpha) / a0]), gains[k]))
    zi = np.zeros((N_FORMANTS, 1, 2))
    out = []
    for i in range(0, len(x) - block + 1, block):
        xb = x[i:i + block][None, :].astype(np.float64)
        wet = np.zeros_like(xb)
        for k, (b, a, g) in enumerate(coefs):
            y, zi[k] = lfilter(b, a, xb, axis=-1, zi=zi[k])
            wet += g * y
        wet *= gain
        o = wet if mix >= 1.0 else xb * (1.0 - mix) + wet * mix
        out.append(o[0].astype(np.float32))
    return np.concatenate(out)


@pytest.mark.parametrize("params", [
    {},
    {"vowel": 1.5, "voice": "soprano", "resonance": 2.0, "gain": 12.0},
    {"vowel": 3.2, "voice": "bass", "resonance": 0.6, "gain": 0.0, "mix": 0.6},
])
def test_formant_zero_unpatched_is_the_pre_shift_filter_bit_exact(params):
    # 2 ** 0 == 1.0 and f * 1.0 is f, bw * 1.0 is bw: the coefficients
    # are the pre-shift coefficients, not merely close to them.
    x = _noise(512 * 40, seed=11)
    ref = _reference_unshifted(params, x)
    assert np.array_equal(_render(params, x=x), ref)
    assert np.array_equal(_render({**params, "formant": 0.0}, x=x), ref)
    # Patched but idle (cv 0) is the same render, and so is depth 0.
    assert np.array_equal(_render(params, x=x, cv_value=0.0, cv_port="formant_cv"), ref)
    assert np.array_equal(
        _render({**params, "formant_cv_depth": 0.0}, x=x, cv_value=0.8, cv_port="formant_cv"), ref)


@pytest.mark.parametrize("st,factor", [(12.0, 2.0), (-12.0, 0.5)])
def test_formant_shift_moves_the_peak_an_octave(st, factor):
    # Measured: 1338 Hz at +12 and 342 Hz at -12 against 650 x 2 / 650 / 2.
    y = _render({"vowel": 0.0, "voice": "tenor", "gain": 0.0, "formant": st}, seconds=2.0)
    f1 = FORMANTS["tenor"]["a"][0][0]
    peak = _smooth_peak_hz(y[SR // 4:])
    assert abs(peak - f1 * factor) < 0.1 * f1 * factor, (st, peak, f1 * factor)


def test_formant_cv_at_depth_one_is_the_knob_at_twelve_bit_exact():
    # 0/12 + 1.0 * 1.0 and 12/12 are both 1.0 exactly: the same ratio,
    # the same coefficients, the same render.
    x = _noise(512 * 20, seed=5)
    knob = _render({"formant": 12.0}, x=x)
    jack = _render({"formant": 0.0, "formant_cv_depth": 1.0}, x=x, cv_value=1.0, cv_port="formant_cv")
    assert np.array_equal(knob, jack)
    # They sum: -6 st on the knob and -1 at depth 0.5 on the jack is -12.
    both = _render({"formant": -6.0, "formant_cv_depth": 0.5}, x=x, cv_value=-1.0, cv_port="formant_cv")
    assert np.array_equal(both, _render({"formant": -12.0}, x=x))


def _impulse_response(params, seconds=2.0):
    imp = np.zeros(int(SR * seconds), np.float32)
    imp[0] = 1.0
    h = _render(params, x=imp).astype(np.float64)
    return np.abs(np.fft.rfft(h)), np.fft.rfftfreq(len(h), 1.0 / SR)


def _peak_bw_skirt(H, fr, near):
    """Peak frequency, -3 dB bandwidth and the peak-to-skirt ratio at a
    quarter octave either side, of the resonance nearest `near`."""
    band = (fr > near / 2) & (fr < near * 2)
    i = int(np.argmax(np.where(band, H, 0.0)))
    half = H[i] / np.sqrt(2.0)
    lo = i
    while lo > 0 and H[lo] > half:
        lo -= 1
    hi = i
    while hi < len(H) - 1 and H[hi] > half:
        hi += 1
    skirt = 0.5 * (H[np.argmin(np.abs(fr - fr[i] * 2 ** 0.25))]
                   + H[np.argmin(np.abs(fr - fr[i] / 2 ** 0.25))])
    return float(fr[i]), float(fr[hi] - fr[lo]), float(H[i] / skirt)


def test_the_shift_is_constant_q_not_constant_bandwidth():
    # Bass I is the single-formant case (F2 sits 30 dB down); at resonance
    # 2 its F1 (250 Hz, bw 60) has Q 8.3. Measured on the impulse response
    # (the module IS an LTI filter at a constant shift, so |H| is exact,
    # not a noise estimate): at +12 the peak moves to 500 Hz, the -3 dB
    # bandwidth DOUBLES (31 -> 61 Hz) and the Q and the quarter-octave
    # peak-to-skirt hold within 3% -- the same vowel, a different throat.
    # A constant-bandwidth shift would have doubled the Q instead.
    f1 = FORMANTS["bass"]["i"][0][0]
    base = {"vowel": 2.0, "voice": "bass", "resonance": 2.0, "gain": 0.0}
    H0, fr = _impulse_response(base)
    H1, _ = _impulse_response({**base, "formant": 12.0})
    p0, bw0, ps0 = _peak_bw_skirt(H0, fr, f1)
    p1, bw1, ps1 = _peak_bw_skirt(H1, fr, 2 * f1)
    assert abs(p0 - f1) < 0.02 * f1 and abs(p1 - 2 * f1) < 0.02 * f1, (p0, p1)
    assert abs(bw1 / bw0 - 2.0) < 0.05, (bw0, bw1)
    q0, q1 = p0 / bw0, p1 / bw1
    assert abs(q1 / q0 - 1.0) < 0.03, (q0, q1)
    assert abs(ps1 / ps0 - 1.0) < 0.03, (ps0, ps1)


def test_one_shift_per_block_for_every_voice():
    # A (V, F) input takes the block's one shift on every row (the jack is
    # read as a block mean like vowel_cv): each noise row equals its own
    # mono render at the same shift, bit for bit.
    x = _noise(512 * 6, 1)
    z = _noise(512 * 6, 2)
    voiced = _driver({"formant": 7.0, "formant_cv_depth": 1.0}, cv=True, cv_port="formant_cv")
    rows = []
    for i in range(0, 512 * 6, 512):
        blk = np.stack([x[i:i + 512], np.zeros(512, np.float32), z[i:i + 512]])
        rows.append(voiced(blk, np.full(512, -0.25, np.float32)))
    got = np.concatenate(rows, axis=-1)
    assert got.shape == (3, 512 * 6) and np.all(got[1] == 0.0)
    assert np.array_equal(got[0], _render({"formant": 4.0}, x=x))       # 7 - 3 semitones
    assert np.array_equal(got[2], _render({"formant": 4.0}, x=z))


def test_block_size_independent_at_a_constant_shift():
    x = _noise(512 * 8, 7)
    p = {"vowel": 1.5, "resonance": 1.3, "formant": 7.0}
    assert np.array_equal(_render(p, x=x, block=512), _render(p, x=x, block=64))
    q = {"vowel": 0.5, "formant": -5.0, "formant_cv_depth": 1.0}
    assert np.array_equal(_render(q, x=x, block=512, cv_value=0.3, cv_port="formant_cv"),
                          _render(q, x=x, block=64, cv_value=0.3, cv_port="formant_cv"))


def _centres_from_coefs(coefs):
    # RBJ bandpass: a1 = -2 cos(w0) / a0, a2 = (1 - alpha) / a0, a0 = 1 + alpha.
    out = []
    for _b, a, _g in coefs:
        alpha = (1.0 - a[2]) / (1.0 + a[2])
        cos_w0 = -a[1] * (1.0 + alpha) / 2.0
        out.append(float(np.arccos(cos_w0) / (2.0 * np.pi) * SR))
    return out


def test_absurd_cv_is_clipped_to_four_octaves_and_parks_at_the_ceiling():
    # The exponent is clipped to +-4 BEFORE the power: a cv of a million
    # at depth 1 is the same render as cv +4, finite, and its ratio is 16.
    x = _noise(512 * 10, 9)
    huge = _render({"voice": "soprano"}, x=x, cv_value=1e6, cv_port="formant_cv")
    four = _render({"voice": "soprano"}, x=x, cv_value=4.0, cv_port="formant_cv")
    assert np.all(np.isfinite(huge)) and np.array_equal(huge, four)
    assert np.all(np.isfinite(_render({}, x=x, cv_value=-1e6, cv_port="formant_cv")))
    # A non-finite CV is ignored (the knob alone), not propagated.
    assert np.array_equal(_render({"formant": 5.0}, x=x, cv_value=np.nan, cv_port="formant_cv"),
                          _render({"formant": 5.0}, x=x))
    # Read the artifact: at x16 soprano A's formants land at 12800, 18400,
    # 46400, 62400, 79200 Hz -- the first two move, the last three park at
    # 0.45 sr (19845 Hz), read back off the coefficients.
    step = _driver({"voice": "soprano"}, cv=True, cv_port="formant_cv")
    step(x[:512], np.full(512, 1e6, np.float32))
    st = step.backend._state[step.module.id]
    assert st["key"][3] == 16.0
    centres = _centres_from_coefs(st["coefs"])
    table = FORMANTS["soprano"]["a"][0]
    assert centres[0] == pytest.approx(table[0] * 16, rel=1e-6)
    assert centres[1] == pytest.approx(table[1] * 16, rel=1e-6)
    for c in centres[2:]:
        assert c == pytest.approx(0.45 * SR, rel=1e-6)


# ----- UI ------------------------------------------------------------------------


def test_voice_combo_offers_the_five_voices_and_every_param_has_a_widget(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("vowel")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    widgets = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            widgets[str(call.kwargs.get("label"))] = (k, call.kwargs.get("items"))
    assert widgets["voice"] == ("add_combo", list(VOWEL_VOICE_CHOICES))
    rate = next(lb for lb in widgets if lb.startswith("cv_rate"))
    assert widgets[rate] == ("add_combo", list(VOWEL_CV_RATES))
    formats = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            formats[str(call.kwargs.get("label"))] = str(call.kwargs.get("format"))
    for name in get_module_type("vowel").DEFAULT_PARAMS:
        hits = [lb for lb in widgets if lb == name or lb.startswith(name + " ")]
        assert hits, (name, list(widgets))
        assert widgets[hits[0]][0] != "add_input_text", name
    formant = next(lb for lb in widgets if lb.startswith("formant "))
    assert widgets[formant][0] == "add_drag_float" and formats[formant].endswith(" st")
    assert widgets["formant_cv_depth"][0] == "add_drag_float"
    assert "oct/unit" in formats["formant_cv_depth"]
    # The custom voice's five frequency knobs are bounded Hz drags.
    bounds = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            bounds[str(call.kwargs.get("label"))] = (call.kwargs.get("min_value"),
                                                     call.kwargs.get("max_value"))
    for i in range(1, N_FORMANTS + 1):
        lb = next(x for x in widgets if x.startswith(f"f{i}"))
        assert widgets[lb][0] == "add_drag_float", lb
        assert formats[lb].endswith(" Hz"), lb
        assert bounds[lb] == (VOWEL_CUSTOM_FREQ_MIN, VOWEL_CUSTOM_FREQ_MAX), lb
    assert all(ord(ch) < 128 for lb in widgets for ch in lb)


# ----- example ----------------------------------------------------------------------


def test_the_talking_pad_example_sweeps_the_vowels():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "vowel_talk.json"
    patch = load_patch(path)
    vw = next(m for m in patch if m.TYPE == "vowel")
    assert any(c.dst_module_id == vw.id and c.dst_port == "vowel_cv" for c in patch.cables)
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    keys = []
    orig = b._render_vowel

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        keys.append(b._state[module.id]["key"][1])
        return r

    b._render_vowel = spy
    peak = 0.0
    for _ in range(int(SR * 8 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.1 < peak < 1.0
    assert max(keys) - min(keys) > 2.5                        # it went most of the way A..U


def test_the_giant_child_example_grows_the_throat_an_octave_each_way():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "vowel_giant_child.json"
    patch = load_patch(path)
    assert len(list(patch)) <= 12
    vw = next(m for m in patch if m.TYPE == "vowel")
    assert any(c.dst_module_id == vw.id and c.dst_port == "formant_cv" for c in patch.cables)
    assert any(c.dst_module_id == vw.id and c.dst_port == "vowel_cv" for c in patch.cables)
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    ratios = []
    orig = b._render_vowel

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        ratios.append(b._state[module.id]["key"][3])
        return r

    b._render_vowel = spy
    np.random.seed(1)
    peak = 0.0
    # Half the 25 s cycle: the bipolar triangle starts at the giant (-1
    # octave) and reaches the child (+1) at 12.5 s.
    for _ in range(int(SR * 13 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8, peak
    assert min(ratios) < 0.55 and max(ratios) > 1.9, (min(ratios), max(ratios))


# ----- (a) the block mean in float64 (love pass, 2026-09-22) ---------------------


def test_the_float32_mean_of_a_constant_really_does_move_with_the_block_size():
    # Self-test the tripwire before trusting it: np.mean ACCUMULATES in the
    # buffer's own dtype, so the float32 mean of a constant 0.3 is a
    # different number at 64 samples than at 512. In float64 the product
    # n * v is exact in 53 bits, so the mean IS the constant, at any size.
    v64, v512 = np.full(64, 0.3, np.float32), np.full(512, 0.3, np.float32)
    assert float(np.mean(v64)) != float(np.mean(v512))
    assert float(np.mean(v64)) == pytest.approx(0.29999998, abs=1e-8)
    exact = float(np.float32(0.3))
    assert float(np.mean(v64, dtype=np.float64)) == exact
    assert float(np.mean(v512, dtype=np.float64)) == exact


@pytest.mark.parametrize("cv", [0.3, 0.7, 0.15, 1.3])
def test_a_constant_vowel_cv_renders_the_same_at_64_and_512(cv):
    # The finding: the block mean landed an ulp apart at the two block
    # sizes, the coefficients followed it, and a static patch was not
    # block-size exact. (The key rounds to 6 dp but the COEFFICIENTS are
    # built from the unrounded vowel, so the rounding never hid it.)
    x = _noise(512 * 8, 13)
    p = {"vowel": 1.0, "resonance": 1.3, "cv_depth": 1.0}
    assert np.array_equal(_render(p, x=x, block=512, cv_value=cv),
                          _render(p, x=x, block=64, cv_value=cv))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_a_non_finite_vowel_cv_is_ignored_rather_than_snapped_to_a_vowel(bad):
    # Python's min/max do not propagate NaN -- ``min(4, max(0, nan))`` is
    # 0.0, so an unscrubbed NaN used to jump the filter to A and an inf to
    # U. Both are now ignored, the way formant_cv's already were: the knob
    # alone, and the render is the unpatched one bit for bit.
    x = _noise(512 * 4, 17)
    assert np.array_equal(_render({"vowel": 3.0}, x=x, cv_value=bad),
                          _render({"vowel": 3.0}, x=x))


# ----- (b) cv_rate: the per-sample mouth -----------------------------------------


def test_cv_rate_defaults_to_block_and_a_pre_love_pass_patch_still_loads():
    assert VOWEL_CV_RATES == ("block", "sample")
    d = Patch()
    d.add_module("vowel", params={"vowel": 1.0})
    raw = d.to_dict()
    for m in raw["modules"]:
        for gone in ("cv_rate", "f1", "f2", "f3", "f4", "f5"):
            m["params"].pop(gone, None)
    vw = next(m for m in Patch.from_dict(raw) if m.TYPE == "vowel")
    assert vw.params["cv_rate"] == "block"
    assert [vw.params[f"f{k + 1}"] for k in range(N_FORMANTS)] == \
        list(VOWEL_CUSTOM_DEFAULT_FREQS)


def test_sample_mode_with_nothing_moving_is_the_block_render_bit_exact():
    # The quantisation grid is relative to the KNOB, not absolute, so step
    # 0 IS the knob: an idle jack, cv_depth 0 and an unpatched jack all
    # give the block-mean render bit for bit. Flipping cv_rate on a still
    # patch is silent, which is the whole point of a knob-relative grid.
    x = _noise(512 * 8, 21)
    zero = np.zeros(len(x), np.float32)
    wob = _tri(len(x), 37.0)
    p = {"vowel": 1.3, "cv_depth": 2.0, "resonance": 1.4}
    assert np.array_equal(_render({**p, "cv_rate": "sample"}, x=x, cv_sig=zero),
                          _render({**p, "cv_rate": "block"}, x=x, cv_sig=zero))
    z = {**p, "cv_depth": 0.0}
    assert np.array_equal(_render({**z, "cv_rate": "sample"}, x=x, cv_sig=wob),
                          _render({**z, "cv_rate": "block"}, x=x, cv_sig=wob))
    assert np.array_equal(_render({**p, "cv_rate": "sample"}, x=x),
                          _render({**p, "cv_rate": "block"}, x=x))
    # An unknown cv_rate falls back to block rather than failing.
    assert np.array_equal(_render({**p, "cv_rate": "audio"}, x=x, cv_sig=wob),
                          _render({**p, "cv_rate": "block"}, x=x, cv_sig=wob))


def test_sample_mode_is_block_size_independent():
    # Runs are cut at buffer boundaries, and lfilter carries zi across a
    # cut exactly, so WHERE the runs are split cannot matter.
    x = _noise(512 * 8, 23)
    wob = _tri(len(x), 37.0)
    p = {"vowel": 2.0, "cv_rate": "sample", "cv_depth": 2.0, "resonance": 1.5}
    assert np.array_equal(_render(p, x=x, block=512, cv_sig=wob),
                          _render(p, x=x, block=64, cv_sig=wob))


def _sideband_db(y, carrier=220.0, freqs=(130.0, 160.0, 190.0, 250.0, 280.0, 310.0)):
    seg = y[SR // 4:].astype(np.float64)
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    fr = np.fft.rfftfreq(len(seg), 1.0 / SR)
    car = spec[(fr > carrier - 3) & (fr < carrier + 3)].max()
    return [float(20 * np.log10(spec[(fr > f - 4) & (fr < f + 4)].max() / car))
            for f in freqs]


def test_sample_mode_follows_the_cv_inside_the_block_and_makes_richer_sidebands():
    # The audible claim, measured where it lives: a 220 Hz sine through a
    # 30 Hz vowel sweep. Reading the CV once per 512-sample buffer samples
    # the modulation at 86 Hz; reading it per sample does not, and the
    # difference shows as higher-order sidebands. Measured, relative to
    # the carrier, at 130 / 160 / 190 / 250 / 280 / 310 Hz:
    #   block  -57.1 -27.0 -20.7 -16.6 -21.0 -30.1
    #   sample -26.6 -16.0 -21.8 -21.7 -12.0 -33.0
    # The 2nd-order pair (160 / 280 Hz) is 11.0 / 9.0 dB up and the
    # 3rd-order at 130 Hz 30.5 dB up -- the ring-mod-ish buzz.
    n = 512 * 172
    x = _sine(n, 220.0)
    cv = _tri(n, 30.0)
    p = {"vowel": 2.0, "cv_depth": 2.0, "resonance": 1.6, "gain": 0.0}
    blk = _sideband_db(_render({**p, "cv_rate": "block"}, x=x, cv_sig=cv))
    smp = _sideband_db(_render({**p, "cv_rate": "sample"}, x=x, cv_sig=cv))
    assert smp[1] - blk[1] > 6.0, (blk, smp)          # 160 Hz
    assert smp[4] - blk[4] > 6.0, (blk, smp)          # 280 Hz
    assert smp[0] - blk[0] > 20.0, (blk, smp)         # 130 Hz


def test_sample_mode_does_not_click():
    # A seam is a coefficient change with the filter STATE carried, not
    # reset, so nothing pops. The tripwire calibrates itself against the
    # BLOCK mode, which jumps coefficients once per buffer and is not a
    # clicking filter: measured, the swept sine's biggest sample-to-sample
    # step is 0.00367 in sample mode against 0.00498 in block mode -- the
    # finer-grained mode is the SMOOTHER one.
    n = 512 * 172
    x = _sine(n, 220.0)
    cv = _tri(n, 30.0)
    p = {"vowel": 2.0, "cv_depth": 2.0, "resonance": 1.6, "gain": 0.0}
    blk = _render({**p, "cv_rate": "block"}, x=x, cv_sig=cv)[SR // 4:]
    smp = _render({**p, "cv_rate": "sample"}, x=x, cv_sig=cv)[SR // 4:]
    blk_step = float(np.abs(np.diff(blk.astype(np.float64))).max())
    smp_step = float(np.abs(np.diff(smp.astype(np.float64))).max())
    assert smp_step <= blk_step, (smp_step, blk_step)
    # And a sanity ceiling: a 220 Hz sine at this level cannot step more
    # than 2 pi f A / sr per sample even with no filter at all.
    assert smp_step < 2 * np.pi * 220.0 * float(np.abs(smp).max()) / SR * 1.2


def _per_sample_oracle(params, x, cv):
    """Coefficients rebuilt EVERY sample, transposed DF2 in pure Python --
    the thing the quantised mode approximates. Same state layout scipy's
    lfilter carries in zi, so the two are comparable sample by sample."""
    vowel = float(params["vowel"])
    depth = float(params["cv_depth"])
    res = float(params["resonance"])
    v = np.clip(vowel + depth * cv.astype(np.float64), 0.0, 4.0)
    z = np.zeros((N_FORMANTS, 2))
    y = np.zeros(len(x))
    for i in range(len(x)):
        freqs, gains, bws = vowel_formants("tenor", v[i])
        xi = float(x[i])
        acc = 0.0
        for k in range(N_FORMANTS):
            f = min(freqs[k], 0.45 * SR)
            q = max(0.1, (f / bws[k]) * res)
            w0 = 2.0 * np.pi * f / SR
            alpha = np.sin(w0) / (2.0 * q)
            a0 = 1.0 + alpha
            b0, b2 = alpha / a0, -alpha / a0
            a1, a2 = -2.0 * np.cos(w0) / a0, (1.0 - alpha) / a0
            yi = b0 * xi + z[k, 0]
            z[k, 0] = -a1 * yi + z[k, 1]
            z[k, 1] = b2 * xi - a2 * yi
            acc += gains[k] * yi
        y[i] = acc
    return y


def test_the_quantised_vowel_tracks_a_true_per_sample_rebuild():
    # The accuracy the 0.02-vowel grid buys, against the filter nobody can
    # afford: measured -61.7 dB for sample mode and -8.9 dB for block mode
    # on a 30 Hz sweep. 0.02 vowels is about 1% of a formant frequency.
    n = 512 * 8
    x = _sine(n, 220.0)
    cv = _tri(n, 30.0)
    p = {"vowel": 2.0, "cv_depth": 2.0, "resonance": 1.6, "gain": 0.0}
    ref = _per_sample_oracle(p, x, cv)
    rms = np.sqrt(np.mean(ref ** 2))

    def err(mode):
        y = _render({**p, "cv_rate": mode}, x=x, cv_sig=cv).astype(np.float64)
        return 20 * np.log10(np.sqrt(np.mean((y - ref) ** 2)) / rms)

    smp, blk = err("sample"), err("block")
    assert smp < -50.0, smp
    assert smp < blk - 40.0, (smp, blk)


def test_the_run_split_collapses_constant_and_clamped_runs():
    # Count the doors. A constant CV is ONE coefficient set however long
    # the render; the cache can never exceed the 201 points of the 0..4
    # grid; and a sweep that spends most of its time PINNED at A or U
    # (depth 10) uses fewer sets than one that does not (depth 2) --
    # because the step COUNT is what gets clipped, so the clamped samples
    # share a run instead of getting one each.
    x = _noise(512 * 8, 27)
    p = {"vowel": 2.0, "cv_rate": "sample", "resonance": 1.4}

    def cache(params, cv_sig):
        _y, step = _render(params, x=x, cv_sig=cv_sig, want_step=True)
        return step.backend._state[step.module.id]["cache"]

    assert len(cache({**p, "cv_depth": 2.0},
                     np.full(len(x), 0.4, np.float32))) == 1
    wide = len(cache({**p, "cv_depth": 10.0}, _tri(len(x), 50.0)))
    full = len(cache({**p, "cv_depth": 2.0}, _tri(len(x), 50.0)))
    assert full <= 201 and wide < full, (wide, full)


@pytest.mark.parametrize("bad", [1e9, np.nan, -np.inf])
def test_an_absurd_or_non_finite_cv_in_sample_mode_stays_finite(bad):
    # Scrub before the clamp, clip the step COUNT before the cast: an
    # int64 cast of rint(1e9 / 0.02) would have been nonsense.
    x = _noise(512 * 4, 33)
    p = {"vowel": 2.0, "cv_rate": "sample", "cv_depth": 2.0}
    y = _render(p, x=x, cv_sig=np.full(len(x), bad, np.float32))
    assert np.all(np.isfinite(y))
    mixed = _tri(len(x), 21.0)
    mixed[::97] = np.nan
    assert np.all(np.isfinite(_render(p, x=x, cv_sig=mixed)))


def test_sample_mode_is_voice_aware():
    # vowel_cv is collapsed to one stream, so every voice row takes the
    # same per-sample vowel -- each row equals its own mono render.
    n = 512 * 6
    a, b = _noise(n, 41), _noise(n, 43)
    cv = _tri(n, 29.0)
    p = {"vowel": 2.0, "cv_rate": "sample", "cv_depth": 2.0, "resonance": 1.4}
    step = _driver(p, cv=True)
    rows = []
    for i in range(0, n, 512):
        blk = np.stack([a[i:i + 512], np.zeros(512, np.float32), b[i:i + 512]])
        rows.append(step(blk, cv[i:i + 512]))
    got = np.concatenate(rows, axis=-1)
    assert got.shape == (3, n) and np.all(got[1] == 0.0)
    assert np.array_equal(got[0], _render(p, x=a, cv_sig=cv))
    assert np.array_equal(got[2], _render(p, x=b, cv_sig=cv))


# ----- (c) the custom voice ------------------------------------------------------


def test_custom_is_offered_and_starts_as_the_tenor_a():
    assert VOWEL_VOICE_CHOICES == VOWEL_VOICES + ("custom",)
    assert "custom" not in FORMANTS                    # it is not a table row
    m = all_module_types()["vowel"](1)
    assert [m.params[f"f{k + 1}"] for k in range(N_FORMANTS)] == \
        list(VOWEL_CUSTOM_DEFAULT_FREQS)
    assert list(VOWEL_CUSTOM_DEFAULT_FREQS) == list(FORMANTS["tenor"]["a"][0])


def test_custom_takes_the_frequencies_and_the_tenor_bandwidths_and_levels():
    mine = (300.0, 900.0, 1500.0, 2000.0, 4000.0)
    freqs, gains, bws = vowel_formants("custom", 3.0, mine)
    t_freqs, t_gains, t_bws = vowel_formants("tenor", 3.0)
    assert freqs == list(mine)
    assert bws == t_bws and gains == t_gains          # borrowed from tenor
    assert freqs != t_freqs
    # With no custom frequencies "custom" is just tenor -- it is not in
    # the table, so it falls back like any unknown voice.
    assert vowel_formants("custom", 3.0) == vowel_formants("tenor", 3.0)
    # And the argument is honoured whoever asks, so the helper stays pure.
    assert vowel_formants("bass", 1.0, mine)[0] == list(mine)


def _peak_bw_near(H, fr, near, frac=0.12):
    """Peak frequency and -3 dB bandwidth of the resonance within +-12% of
    `near` -- tighter than _peak_bw_skirt's octave, because a custom F1
    can be parked right next to F2."""
    band = (fr > near * (1 - frac)) & (fr < near * (1 + frac))
    i = int(np.argmax(np.where(band, H, 0.0)))
    half = H[i] / np.sqrt(2.0)
    lo = i
    while lo > 0 and H[lo] > half:
        lo -= 1
    hi = i
    while hi < len(H) - 1 and H[hi] > half:
        hi += 1
    return float(fr[i]), float(fr[hi] - fr[lo])


def test_custom_with_the_tenor_as_frequencies_is_the_tenor_at_vowel_zero():
    # The pin the spec asked for. Note it is NOT a bit-exactness claim by
    # construction: the table path computes exp((1-t)ln fa + t ln fb),
    # and exp(log(650)) is 649.9999999999999, so the two coefficient sets
    # agree to a part in 1e15 rather than exactly (the float32 output
    # happens to round them together, but that is luck, not a contract).
    x = _noise(512 * 8, 47)
    same = {f"f{k + 1}": float(FORMANTS["tenor"]["a"][0][k]) for k in range(N_FORMANTS)}
    cust = _render({"voice": "custom", "vowel": 0.0, "gain": 0.0,
                    "resonance": 2.0, **same}, x=x)
    tenor = _render({"voice": "tenor", "vowel": 0.0, "gain": 0.0,
                     "resonance": 2.0}, x=x)
    assert np.allclose(cust, tenor, atol=1e-6)
    H0, fr = _impulse_response({"voice": "custom", "vowel": 0.0, "gain": 0.0,
                                "resonance": 2.0, **same})
    H1, _ = _impulse_response({"voice": "tenor", "vowel": 0.0, "gain": 0.0,
                               "resonance": 2.0})
    for f in FORMANTS["tenor"]["a"][0][:3]:
        p0, _b0 = _peak_bw_near(H0, fr, float(f))
        p1, _b1 = _peak_bw_near(H1, fr, float(f))
        assert abs(p0 - p1) < 0.02 * p1 and abs(p0 - f) < 0.02 * f, (f, p0, p1)


@pytest.mark.parametrize("f1,bw,q", [(300.0, 40.56, 7.40), (650.0, 40.56, 16.01),
                                     (1200.0, 40.06, 29.99), (2500.0, 39.56, 63.15)])
def test_a_custom_f1_lands_where_you_asked_with_the_tables_bandwidth(f1, bw, q):
    # The documented consequence, measured on the impulse response (the
    # module is an LTI filter at a fixed vowel, so |H| is exact): the
    # bandwidth stays the TABLE's in Hz (80 Hz for tenor A's F1, halved
    # to ~40 by resonance 2) wherever you put the formant, so Q climbs
    # from 7.4 at 300 Hz to 63 at 2500 Hz. That is why `resonance` is the
    # knob for a custom voice's character.
    H, fr = _impulse_response({"voice": "custom", "vowel": 0.0, "gain": 0.0,
                               "resonance": 2.0, "f1": f1})
    peak, meas_bw = _peak_bw_near(H, fr, f1)
    assert abs(peak - f1) < 0.02 * f1, (f1, peak)
    assert meas_bw == pytest.approx(bw, rel=0.05)
    assert peak / meas_bw == pytest.approx(q, rel=0.05)


def test_the_vowel_knob_morphs_a_custom_voices_resonances_not_its_pitches():
    # A custom voice is a fixed formant CHORD: the knob moves the
    # bandwidths and levels (tenor A's F1 bw 80 -> tenor O's 70, so the
    # measured -3 dB width goes 40.6 -> 35.6 at resonance 2) while the
    # peak stays exactly where f1 put it.
    base = {"voice": "custom", "gain": 0.0, "resonance": 2.0, "f1": 650.0}
    Ha, fr = _impulse_response({**base, "vowel": 0.0})
    Ho, _ = _impulse_response({**base, "vowel": 3.0})
    pa, ba = _peak_bw_near(Ha, fr, 650.0)
    po, bo = _peak_bw_near(Ho, fr, 650.0)
    assert abs(pa - 650.0) < 0.02 * 650.0 and abs(po - 650.0) < 0.02 * 650.0
    assert ba == pytest.approx(40.56, rel=0.05)
    assert bo == pytest.approx(35.55, rel=0.05)
    assert bo / ba == pytest.approx(70.0 / 80.0, rel=0.05)


def test_the_f_knobs_do_nothing_to_a_table_voice():
    # f1..f5 are read only for "custom", so they must not invalidate a
    # table voice's cached coefficients or change a note of its render.
    x = _noise(512 * 6, 51)
    assert np.array_equal(_render({"voice": "tenor", "f1": 4000.0, "f5": 120.0}, x=x),
                          _render({"voice": "tenor"}, x=x))
    assert np.array_equal(_render({"voice": "kazoo", "vowel": 1.0}, x=x),
                          _render({"voice": "tenor", "vowel": 1.0}, x=x))


def test_a_custom_frequency_is_clamped_to_the_widgets_range():
    # The knobs are 50..8000 Hz and the renderer clamps to the same
    # bounds, so a hand-edited patch cannot push a formant past Nyquist.
    x = _noise(512 * 4, 53)
    assert np.array_equal(_render({"voice": "custom", "f1": 1e9}, x=x),
                          _render({"voice": "custom", "f1": VOWEL_CUSTOM_FREQ_MAX}, x=x))
    assert np.array_equal(_render({"voice": "custom", "f1": -5.0}, x=x),
                          _render({"voice": "custom", "f1": VOWEL_CUSTOM_FREQ_MIN}, x=x))
    assert np.all(np.isfinite(_render({"voice": "custom", "f1": 1e9}, x=x)))


def test_the_custom_voice_and_sample_mode_work_together():
    # The two follow-ons share the coefficient cache, keyed per quantised
    # step and dropped whenever the voice, knob, resonance, shift or the
    # custom frequencies change -- so a custom voice swept per sample is
    # still block-size exact and still not the block render.
    x = _noise(512 * 8, 57)
    cv = _tri(len(x), 31.0)
    p = {"voice": "custom", "vowel": 2.0, "cv_rate": "sample", "cv_depth": 2.0,
         "f1": 420.0, "f2": 1500.0}
    assert np.array_equal(_render(p, x=x, cv_sig=cv, block=512),
                          _render(p, x=x, cv_sig=cv, block=64))
    assert not np.array_equal(_render(p, x=x, cv_sig=cv),
                              _render({**p, "cv_rate": "block"}, x=x, cv_sig=cv))


# ----- the robot example ---------------------------------------------------------


def test_the_robot_talk_example_buzzes_per_sample():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "vowel_robot_talk.json"
    patch = load_patch(path)
    assert len(list(patch)) <= 12
    vw = next(m for m in patch if m.TYPE == "vowel")
    assert vw.params["cv_rate"] == "sample"
    assert any(c.dst_module_id == vw.id and c.dst_port == "vowel_cv" for c in patch.cables)
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    np.random.seed(1)
    peak = 0.0
    for _ in range(int(SR * 5 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8, peak
    # It really used the per-sample path: the 25 Hz mouth swept dozens of
    # quantised vowel positions, so the coefficient cache is full of them
    # (a block-rate read would have left one key per block at most).
    assert len(b._state[vw.id]["cache"]) > 50
