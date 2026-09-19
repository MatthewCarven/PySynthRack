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
    VOWEL_NAMES,
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


def _render(params=None, seconds=1.0, block=512, cv_value=None, x=None, cv_port="vowel_cv"):
    step = _driver(params, cv=cv_value is not None, block=block, cv_port=cv_port)
    if x is None:
        x = _noise(int(SR * seconds))
    out = []
    for i in range(0, len(x) - block + 1, block):
        cvb = np.full(block, cv_value, dtype=np.float32) if cv_value is not None else None
        out.append(step(x[i:i + block], cvb))
    return np.concatenate(out)


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
    assert widgets["voice"] == ("add_combo", list(VOWEL_VOICES))
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
