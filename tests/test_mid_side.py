"""MidSide — M/S encode/decode + width + bass mono, all outs live.

Pins the contract: the decode at width 1 is the identity (bit-close),
mid/side land the exact sum/difference forms (hard-pan included),
width 0 collapses to dual mono, width 2 doubles the side, `width_cv`
adds per sample and clamps to 0..2 at both ends, a single patched
input is a level-preserving mono passthrough with width inert, both
unpatched is silence, and the module drives a real stereo sink.

`side_hp` (bass mono): at 0 the filter code is never called and the
render is bit-exact with the filterless pair; on, the side under the
corner collapses (measured on the L−R difference: 60 Hz down > 10 dB
under a 120 Hz corner, 40 Hz > 15 dB, 1 kHz < 1 dB, the 30-vs-60 Hz
slope ~12 dB/oct) while `mid` is bit-equal; the `side` jack is the
filtered side; `width` scales the filtered side; mono passthrough is
untouched; 64-, 1- and 4096-sample blocks agree bit-exactly with 512;
the corner is held to 20..500; the widget sweep; the bass-mono example.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch

SR = 1000
SR_HI = 44100


def _driver(params=None, jacks=("in_l", "in_r"), with_cv=False, sr=SR, block=64):
    patch = Patch()
    ms = patch.add_module("mid_side", params=params or {})
    feeds = {}
    for jack in jacks:
        src = patch.add_module("oscillator")
        patch.connect(src.id, "out", ms.id, jack)
        feeds[jack] = src
    if with_cv:
        cv = patch.add_module("constant")
        patch.connect(cv.id, "out", ms.id, "width_cv")
        feeds["width_cv"] = cv
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(frames=None, **blocks):
        for arr in blocks.values():
            frames = np.asarray(arr).shape[-1]
            break
        buffers = {
            (feeds[j].id, "out"): np.asarray(arr, dtype=np.float32)
            for j, arr in blocks.items()
        }
        return b._render_mid_side(patch.get(ms.id), frames, buffers, patch)

    step.ms = ms
    step.patch = patch
    step.backend = b
    return step


def _stream(params, L, R=None, block=512):
    """Run L (and R) through a 44.1 kHz module block by block."""
    jacks = ("in_l", "in_r") if R is not None else ("in_l",)
    step = _driver(params, jacks=jacks, sr=SR_HI, block=block)
    outs = {k: [] for k in ("mid", "side", "out_l", "out_r")}
    for i in range(0, L.shape[0], block):
        feed = {"in_l": L[i:i + block]}
        if R is not None:
            feed["in_r"] = R[i:i + block]
        r = step(**feed)
        for k in outs:
            outs[k].append(np.asarray(r[k]))
    return {k: np.concatenate(v) for k, v in outs.items()}, step


def _tone_amp(x, f, sr=SR_HI):
    """Amplitude of the ``f`` Hz component of ``x`` (Hann-windowed)."""
    n = x.shape[0]
    t = np.arange(n) / sr
    w = np.hanning(n)
    return 2.0 * abs(np.sum(x * w * np.exp(-2j * np.pi * f * t))) / np.sum(w)


def _db(a, b):
    return 20.0 * np.log10(a / b)


def _tones_left(freqs, amps, seconds=2.0):
    """A sum of sines on in_l only: half of it is mid, half is side."""
    t = np.arange(int(SR_HI * seconds)) / SR_HI
    x = sum(a * np.sin(2 * np.pi * f * t) for f, a in zip(freqs, amps))
    return x.astype(np.float32)


SETTLE = slice(SR_HI // 2, None)   # skip the filter's settle


def _lr(F=128, seed=0):
    rng = np.random.default_rng(seed)
    return (
        rng.uniform(-1, 1, F).astype(np.float32),
        rng.uniform(-1, 1, F).astype(np.float32),
    )


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["mid_side"]
    assert cls is get_module_type("mid_side")
    assert cls.CATEGORY == "Routing & VCA"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["in_l", "in_r", "width_cv"]
    assert [p.name for p in m.output_ports] == ["mid", "side", "out_l", "out_r"]
    assert m.params["width"] == 1.0
    assert m.params["side_hp"] == 0.0     # bass mono ships OFF


def test_serialization_round_trip():
    cls = all_module_types()["mid_side"]
    m = cls(3, params={"width": 1.7, "side_hp": 120.0})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the math --------------------------------------------------------------


def test_width_one_decode_is_identity():
    step = _driver()
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["out_l"], L, atol=1e-6)
    np.testing.assert_allclose(out["out_r"], R, atol=1e-6)


def test_encode_lands_sum_and_difference():
    step = _driver()
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["mid"], (L + R) / 2.0, atol=1e-6)
    np.testing.assert_allclose(out["side"], (L - R) / 2.0, atol=1e-6)


def test_hard_pan_encode():
    step = _driver()
    L = np.ones(16, dtype=np.float32)
    R = np.zeros(16, dtype=np.float32)
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["mid"], 0.5, atol=1e-6)
    np.testing.assert_allclose(out["side"], 0.5, atol=1e-6)


def test_width_zero_collapses_to_dual_mono():
    step = _driver({"width": 0.0})
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    np.testing.assert_allclose(out["out_l"], out["out_r"], atol=1e-9)
    np.testing.assert_allclose(out["out_l"], (L + R) / 2.0, atol=1e-6)


def test_width_two_doubles_the_side():
    step = _driver({"width": 2.0})
    L, R = _lr()
    out = step(in_l=L, in_r=R)
    mid = (L + R) / 2.0
    side = (L - R) / 2.0
    np.testing.assert_allclose(out["out_l"], mid + 2.0 * side, atol=1e-5)
    np.testing.assert_allclose(out["out_r"], mid - 2.0 * side, atol=1e-5)


# ----- width_cv --------------------------------------------------------------


def test_width_cv_adds_per_sample():
    step = _driver({"width": 1.0}, with_cv=True)
    L, R = _lr()
    cv = np.linspace(-0.5, 0.5, L.shape[0]).astype(np.float32)
    out = step(in_l=L, in_r=R, width_cv=cv)
    w = 1.0 + cv.astype(np.float64)
    side = (L.astype(np.float64) - R) / 2.0
    mid = (L.astype(np.float64) + R) / 2.0
    np.testing.assert_allclose(out["out_l"], mid + w * side, atol=1e-5)


def test_width_cv_clamps_both_ends():
    step = _driver({"width": 1.0}, with_cv=True)
    L, R = _lr()
    F = L.shape[0]
    big = np.full(F, 9.0, dtype=np.float32)   # 1 + 9 -> clamp 2
    neg = np.full(F, -9.0, dtype=np.float32)  # 1 - 9 -> clamp 0
    mid = (L.astype(np.float64) + R) / 2.0
    side = (L.astype(np.float64) - R) / 2.0
    out_hi = step(in_l=L, in_r=R, width_cv=big)
    np.testing.assert_allclose(out_hi["out_l"], mid + 2.0 * side, atol=1e-5)
    out_lo = step(in_l=L, in_r=R, width_cv=neg)
    np.testing.assert_allclose(out_lo["out_l"], mid, atol=1e-5)
    np.testing.assert_allclose(out_lo["out_l"], out_lo["out_r"], atol=1e-9)


# ----- mono edges ------------------------------------------------------------


@pytest.mark.parametrize("jack", ["in_l", "in_r"])
def test_single_input_is_level_preserving_mono(jack):
    step = _driver({"width": 2.0}, jacks=(jack,))
    x = np.random.default_rng(1).uniform(-1, 1, 64).astype(np.float32)
    out = step(**{jack: x})
    # The one input IS the mid (not halved), side is 0, width inert.
    np.testing.assert_allclose(out["mid"], x, atol=1e-9)
    assert not np.any(out["side"])
    np.testing.assert_allclose(out["out_l"], x, atol=1e-9)
    np.testing.assert_allclose(out["out_r"], x, atol=1e-9)


def test_both_unpatched_is_silence():
    step = _driver(jacks=())
    out = step(frames=32)
    for name in ("mid", "side", "out_l", "out_r"):
        assert not np.any(out[name])


# ----- integration -----------------------------------------------------------


def test_full_graph_render_to_stereo_sink():
    patch = Patch()
    o1 = patch.add_module("oscillator", params={"freq": 220.0})
    o2 = patch.add_module("oscillator", params={"freq": 223.0})
    ms = patch.add_module("mid_side", params={"width": 1.5})
    lfo = patch.add_module("lfo")
    out = patch.add_module("stereo_speaker_output")
    patch.connect(o1.id, "out", ms.id, "in_l")
    patch.connect(o2.id, "out", ms.id, "in_r")
    patch.connect(lfo.id, "cv", ms.id, "width_cv")
    patch.connect(ms.id, "out_l", out.id, "in_l")
    patch.connect(ms.id, "out_r", out.id, "in_r")
    b = NumpyBackend(sample_rate=44100, block_size=256)
    b.compile(patch)
    for _ in range(8):
        mixdown, _dev = b.render_block_multi(256)
        assert mixdown is not None and np.all(np.isfinite(mixdown))


# ----- side_hp (bass mono) ---------------------------------------------------


def _unfiltered(L, R, width):
    """The shipped math, the way the module computed it before side_hp."""
    mid = (L.astype(np.float64) + R) * 0.5
    side = (L.astype(np.float64) - R) * 0.5
    return {
        "mid": mid.astype(np.float32),
        "side": side.astype(np.float32),
        "out_l": (mid + width * side).astype(np.float32),
        "out_r": (mid - width * side).astype(np.float32),
    }


@pytest.mark.parametrize("side_hp", [0.0, -50.0, "junk"])
def test_side_hp_off_is_bit_exact_and_never_calls_the_filter(side_hp):
    L, R = _lr(F=4096, seed=5)
    ref = _unfiltered(L, R, 1.4)
    out, step = _stream({"width": 1.4, "side_hp": side_hp}, L, R, block=512)
    for k in ref:
        assert np.array_equal(out[k], ref[k]), k
    assert step.ms.id not in step.backend._state

    # The code path is SKIPPED at 0, not run with a 0 Hz filter.
    def boom(*a, **k):
        raise AssertionError("side_hp filter ran while off")

    step.backend._mid_side_side_hp = boom
    again = step(in_l=L[:512], in_r=R[:512])
    for k in ref:
        assert np.array_equal(again[k], ref[k][:512]), k


def test_side_hp_collapses_the_low_side_and_leaves_mid_alone():
    # 30/40/60 Hz + 1 kHz on in_l only: every tone is half mid, half side.
    L = _tones_left([30.0, 40.0, 60.0, 1000.0], [0.3, 0.3, 0.3, 0.3])
    R = np.zeros_like(L)
    off, _ = _stream({"width": 1.0, "side_hp": 0.0}, L, R)
    on, _ = _stream({"width": 1.0, "side_hp": 120.0}, L, R)
    d_off = (off["out_l"] - off["out_r"])[SETTLE]
    d_on = (on["out_l"] - on["out_r"])[SETTLE]
    # One octave under the corner a Q-0.707 2-pole is 1/sqrt(1+2^4) =
    # -12.3 dB by definition; 40 Hz (1.58 oct under) is -19.1 dB.
    assert _db(_tone_amp(d_on, 60.0), _tone_amp(d_off, 60.0)) < -10.0
    assert _db(_tone_amp(d_on, 40.0), _tone_amp(d_off, 40.0)) < -15.0
    assert abs(_db(_tone_amp(d_on, 1000.0), _tone_amp(d_off, 1000.0))) < 1.0
    # The mid never sees the filter: bit-equal, not merely close.
    assert np.array_equal(on["mid"], off["mid"])
    # And the low end really did land in the middle: the 60 Hz that
    # left the side is still all there in L+R.
    assert _tone_amp((on["out_l"] + on["out_r"])[SETTLE], 60.0) > 0.29


def test_side_hp_slope_is_12_db_per_octave():
    L = _tones_left([30.0, 60.0], [0.4, 0.4])
    R = np.zeros_like(L)
    off, _ = _stream({"width": 1.0, "side_hp": 0.0}, L, R)
    on, _ = _stream({"width": 1.0, "side_hp": 120.0}, L, R)
    d_off = (off["out_l"] - off["out_r"])[SETTLE]
    d_on = (on["out_l"] - on["out_r"])[SETTLE]
    att30 = _db(_tone_amp(d_on, 30.0), _tone_amp(d_off, 30.0))
    att60 = _db(_tone_amp(d_on, 60.0), _tone_amp(d_off, 60.0))
    # Theory: -24.1 dB vs -12.3 dB -> 11.8 dB apart (12 asymptotically).
    assert 10.5 < att60 - att30 < 13.0


def test_side_jack_is_the_filtered_side():
    L = _tones_left([60.0, 1000.0], [0.4, 0.4])
    R = np.zeros_like(L)
    off, _ = _stream({"width": 1.0, "side_hp": 0.0}, L, R)
    on, _ = _stream({"width": 1.0, "side_hp": 120.0}, L, R)
    # side == (out_l - out_r) / 2 at width 1: the decode uses the same
    # filtered side the jack carries.
    np.testing.assert_allclose(
        on["side"], (on["out_l"] - on["out_r"]) * 0.5, atol=2e-6
    )
    s_off = off["side"][SETTLE]
    s_on = on["side"][SETTLE]
    assert _db(_tone_amp(s_on, 60.0), _tone_amp(s_off, 60.0)) < -10.0
    assert abs(_db(_tone_amp(s_on, 1000.0), _tone_amp(s_off, 1000.0))) < 1.0


def test_width_scales_the_filtered_side():
    L, R = _lr(F=4096, seed=9)
    one, _ = _stream({"width": 1.0, "side_hp": 120.0}, L, R)
    two, _ = _stream({"width": 2.0, "side_hp": 120.0}, L, R)
    zero, _ = _stream({"width": 0.0, "side_hp": 120.0}, L, R)
    # Same filtered side on the jack whatever the width...
    assert np.array_equal(one["side"], two["side"])
    assert np.array_equal(one["side"], zero["side"])
    # ...and the decode is mid +/- width * that side.
    mid = one["mid"].astype(np.float64)
    fs = one["side"].astype(np.float64)
    np.testing.assert_allclose(two["out_l"], mid + 2.0 * fs, atol=2e-6)
    np.testing.assert_allclose(two["out_r"], mid - 2.0 * fs, atol=2e-6)
    np.testing.assert_allclose(zero["out_l"], zero["out_r"], atol=1e-9)
    np.testing.assert_allclose(zero["out_l"], mid, atol=1e-6)


@pytest.mark.parametrize("jack", ["in_l", "in_r"])
def test_side_hp_leaves_mono_passthrough_untouched(jack):
    step = _driver({"width": 2.0, "side_hp": 120.0}, jacks=(jack,),
                   sr=SR_HI, block=512)
    x = _tones_left([60.0, 1000.0], [0.4, 0.3], seconds=0.1)[:512]
    for _ in range(3):
        out = step(**{jack: x})
        assert np.array_equal(out["mid"], x)
        assert not np.any(out["side"])
        assert np.array_equal(out["out_l"], x)
        assert np.array_equal(out["out_r"], x)
    assert step.ms.id not in step.backend._state


@pytest.mark.parametrize("block", [64, 1, 4096])
def test_side_hp_is_block_size_independent(block):
    n = 4096
    L, R = _lr(F=n, seed=3)
    ref, _ = _stream({"width": 1.3, "side_hp": 90.0}, L, R, block=512)
    got, _ = _stream({"width": 1.3, "side_hp": 90.0}, L, R, block=block)
    for k in ref:
        assert np.array_equal(ref[k], got[k]), (k, block)


def test_side_hp_corner_is_held_to_20_500_and_coeffs_are_cached():
    L, R = _lr(F=2048, seed=4)
    lo, _ = _stream({"side_hp": 5.0}, L, R)
    lo_ref, _ = _stream({"side_hp": 20.0}, L, R)
    hi, _ = _stream({"side_hp": 5000.0}, L, R)
    hi_ref, _ = _stream({"side_hp": 500.0}, L, R)
    for k in lo:
        assert np.array_equal(lo[k], lo_ref[k]), k
        assert np.array_equal(hi[k], hi_ref[k]), k
    # Coefficients are built once per corner, not per block.
    out, step = _stream({"side_hp": 120.0}, L, R, block=256)
    st = step.backend._state[step.ms.id]
    b_obj = st["hp_b"]
    step(in_l=L[:256], in_r=R[:256])
    assert st["hp_b"] is b_obj and st["hp_freq"] == 120.0
    step.ms.params["side_hp"] = 200.0
    step(in_l=L[:256], in_r=R[:256])
    assert st["hp_b"] is not b_obj and st["hp_freq"] == 200.0
    # Switching the corner off drops the history: a corner switched
    # back on starts from rest, not from a stale tail.
    step.ms.params["side_hp"] = 0.0
    step(in_l=L[:256], in_r=R[:256])
    assert step.ms.id not in step.backend._state


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("mid_side")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("format"))
    return out


def test_every_param_gets_a_bounded_widget(monkeypatch):
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("mid_side").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["width"] == ("add_slider_float", "%.2f x")
    assert w["side_hp"] == ("add_drag_float", "%.0f Hz")


# ----- example ------------------------------------------------------------------


def _band_rms(x, lo, hi, sr=SR_HI):
    n = x.shape[0]
    spec = np.fft.rfft(x * np.hanning(n))
    f = np.fft.rfftfreq(n, 1.0 / sr)
    m = (f >= lo) & (f <= hi)
    return float(np.sqrt(np.sum(np.abs(spec[m]) ** 2)) / n)


def _render_bass_mono_example(side_hp=None, seconds=3.0):
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "mid_side_bass_mono.json"
    patch = load_patch(path)
    ms = next(m for m in patch if m.TYPE == "mid_side")
    if side_hp is not None:
        ms.params["side_hp"] = side_hp
    b = NumpyBackend(sample_rate=SR_HI, block_size=512)
    b.compile(patch)
    cap = []
    orig = b._render_mid_side

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.append((r["out_l"].copy(), r["out_r"].copy()))
        return r

    b._render_mid_side = spy
    peak = 0.0
    for _ in range(int(SR_HI * seconds / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    L = np.concatenate([x for x, _ in cap])
    R = np.concatenate([y for _, y in cap])
    return L, R, peak, ms


def test_the_bass_mono_example_keeps_the_sub_in_the_middle():
    L, R, peak, ms = _render_bass_mono_example()
    assert ms.params["width"] == 1.6 and ms.params["side_hp"] == 120.0
    assert 0.1 < peak < 1.0
    seg = slice(SR_HI, None)
    diff, summ = (L - R)[seg], (L + R)[seg]
    # The sub (55 Hz) sits in the middle: L-R is a sliver of L+R there...
    assert _band_rms(diff, 40.0, 70.0) / _band_rms(summ, 40.0, 70.0) < 0.15
    # ...while the pad around 1 kHz is still wide.
    assert _band_rms(diff, 800.0, 1500.0) / _band_rms(summ, 800.0, 1500.0) > 0.3
    # And it is side_hp doing it: the chorus DID smear the sub. With the
    # corner off the 55 Hz side is > 10 dB hotter (theory 13.7 dB for
    # 55 Hz under a 120 Hz corner) and the 1 kHz side is the same.
    L0, R0, _peak0, _ = _render_bass_mono_example(side_hp=0.0)
    diff0 = (L0 - R0)[seg]
    assert _db(_band_rms(diff, 40.0, 70.0), _band_rms(diff0, 40.0, 70.0)) < -10.0
    assert abs(_db(_band_rms(diff, 800.0, 1500.0),
                   _band_rms(diff0, 800.0, 1500.0))) < 0.5
    assert _band_rms(diff0, 40.0, 70.0) / _band_rms((L0 + R0)[seg], 40.0, 70.0) > 0.25
