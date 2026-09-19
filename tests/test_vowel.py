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


def _driver(params=None, cv=False, sr=SR, block=512):
    patch = Patch()
    m = patch.add_module("vowel", params=params or {})
    src = patch.add_module("noise")
    patch.connect(src.id, "out", m.id, "in")
    keys = {"in": (src.id, "out")}
    if cv:
        c = patch.add_module("constant")
        patch.connect(c.id, "out", m.id, "vowel_cv")
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


def _render(params=None, seconds=1.0, block=512, cv_value=None, x=None):
    step = _driver(params, cv=cv_value is not None, block=block)
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
    assert [(p.name, p.signal_kind) for p in m.input_ports] == [("in", "audio"), ("vowel_cv", "cv")]
    assert [(p.name, p.signal_kind) for p in m.output_ports] == [("out", "audio")]
    assert m.params["voice"] == "tenor" and m.params["mix"] == 1.0 and m.params["vowel"] == 0.0
    assert VOWEL_VOICES == ("soprano", "alto", "countertenor", "tenor", "bass")
    assert VOWEL_NAMES == ("a", "e", "i", "o", "u")


def test_serialization_round_trip():
    cls = all_module_types()["vowel"]
    m = cls(2, params={"vowel": 2.5, "voice": "bass", "resonance": 1.5, "mix": 0.4})
    assert cls.from_dict(m.to_dict()).params == m.params


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
    for name in get_module_type("vowel").DEFAULT_PARAMS:
        hits = [lb for lb in widgets if lb == name or lb.startswith(name + " ")]
        assert hits, (name, list(widgets))
        assert widgets[hits[0]][0] != "add_input_text", name


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
