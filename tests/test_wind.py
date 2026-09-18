"""Wind — the blown pipe (flute / reed).

Pins the contract: both models sustain and stay bounded while the gate
is high; the reed is in tune within ±8 cents C2..C6 and the flute within
±10 cents C3..C6 at the default breath (the tuned-to-1.5-periods bore
with the loss filter's exact phase delay compensated); the reed is
harmonic-rich where the flute is nearly pure; the breath
ramps (release then exact zeros, early-out); breath raises the reed's
level and the flute speaks across its whole breath range; breath noise
is seeded per note (deterministic, seed-dependent, audible); per-sample
breath_cv; damping darkens; voice independence with mono ≡ single voice;
block-size independence for a note that lands and lifts mid-stream;
unpatched behaviour; the widget sweep; the example.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.wind import WIND_MODELS

C4 = 261.6255653005986


def _driver(params=None, sr=44100, block=512, breath_cv=False):
    patch = Patch()
    m = patch.add_module("wind", params=params or {})
    psrc = patch.add_module("constant")
    patch.connect(psrc.id, "out", m.id, "pitch_cv")
    gsrc = patch.add_module("clock")
    patch.connect(gsrc.id, "out", m.id, "gate")
    keys = {"pitch": (psrc.id, "out"), "gate": (gsrc.id, "out")}
    if breath_cv:
        bc = patch.add_module("constant")
        patch.connect(bc.id, "out", m.id, "breath_cv")
        keys["breath"] = (bc.id, "out")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(pitch_block, gate_block, breath=None):
        arr = np.asarray(pitch_block, dtype=np.float32)
        bufs = {keys["pitch"]: arr, keys["gate"]: np.asarray(gate_block, dtype=np.float32)}
        if breath is not None:
            bufs[keys["breath"]] = np.asarray(breath, dtype=np.float32)
        return b._render_wind(patch.get(m.id), arr.shape[-1], bufs, patch)

    step.module = m
    step.patch = patch
    step.backend = b
    return step


def _render(cv, seconds=1.0, params=None, sr=44100, block=512, off_at=None, breath_cv=None):
    step = _driver(params, sr=sr, block=block, breath_cv=breath_cv is not None)
    out = []
    for i in range(int(seconds * sr / block)):
        t = np.arange(i * block, (i + 1) * block)
        gate = np.ones(block, dtype=np.float32) if off_at is None else (
            t < off_at * sr).astype(np.float32)
        breath = breath_cv(t) if breath_cv is not None else None
        out.append(step(np.full(block, cv, dtype=np.float32), gate, breath))
    return np.concatenate(out)


def _partial_freq(sig, sr, f_near):
    w = np.hanning(len(sig))
    spec = np.abs(np.fft.rfft(sig * w))
    lo = max(1, int(len(sig) * f_near * 0.92 / sr))
    hi = min(len(spec) - 2, int(len(sig) * f_near * 1.08 / sr) + 1)
    k = lo + int(np.argmax(spec[lo:hi + 1]))
    a, b, c = spec[k - 1], spec[k], spec[k + 1]
    denom = a - 2 * b + c
    delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
    return (k + delta) * sr / len(sig)


def _rms(sig):
    return float(np.sqrt(np.mean(np.asarray(sig, dtype=np.float64) ** 2)))


def _centroid(sig, sr=44100):
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    freqs = np.fft.rfftfreq(len(sig), 1.0 / sr)
    return float((spec * freqs).sum() / spec.sum())


def _harmonic(sig, f0, k, sr=44100):
    """Peak magnitude within ±3% of the k-th harmonic."""
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    lo = int(len(sig) * f0 * k * 0.97 / sr)
    hi = int(len(sig) * f0 * k * 1.03 / sr) + 1
    return float(spec[lo:hi].max())


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["wind"]
    assert cls is get_module_type("wind")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["pitch_cv", "gate", "breath_cv"]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["model"] == "flute"
    assert WIND_MODELS == ("flute", "reed")


def test_serialization_round_trip():
    cls = all_module_types()["wind"]
    m = cls(2, params={"model": "reed", "breath": 0.8, "seed": 7})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the note -------------------------------------------------------------


@pytest.mark.parametrize("model", WIND_MODELS)
def test_the_note_sustains_and_stays_bounded(model):
    sig = _render(0.0, seconds=2.0, params={"model": model})
    sr = 44100
    early = _rms(sig[int(0.3 * sr):int(0.5 * sr)])
    late = _rms(sig[int(1.7 * sr):int(1.9 * sr)])
    assert early > 0.05
    assert 0.5 < late / early < 2.0
    assert np.abs(sig).max() < 1.0
    assert np.all(np.isfinite(sig))


@pytest.mark.parametrize("cv", [-2.0, -1.0, 0.0, 1.0, 2.0])  # C2..C6
def test_reed_pitch_within_8_cents(cv):
    sig = _render(cv, seconds=1.2, params={"model": "reed"})
    tail = sig[len(sig) // 2:]
    expected = C4 * 2.0 ** cv
    cents = 1200.0 * np.log2(_partial_freq(tail, 44100, expected) / expected)
    assert abs(cents) < 8.0, f"cv {cv}: {cents:+.1f} ct"


@pytest.mark.parametrize("cv", [-1.0, 0.0, 1.0, 2.0])  # C3..C6 (the flute's range)
def test_flute_pitch_within_10_cents(cv):
    sig = _render(cv, seconds=1.2, params={"model": "flute"})
    tail = sig[len(sig) // 2:]
    expected = C4 * 2.0 ** cv
    cents = 1200.0 * np.log2(_partial_freq(tail, 44100, expected) / expected)
    assert abs(cents) < 10.0, f"cv {cv}: {cents:+.1f} ct"


def test_the_two_models_are_different_instruments_on_the_same_note():
    # Same pitch, same breath: the flute is a nearly pure tone with air
    # around it, the reed a harmonic-rich pipe -- the reed carries far
    # more of its energy above the fundamental.
    reed = _render(0.0, params={"model": "reed", "noise": 0.0})[22050:]
    flute = _render(0.0, params={"model": "flute", "noise": 0.0})[22050:]
    for sig in (reed, flute):
        assert abs(1200 * np.log2(_partial_freq(sig, 44100, C4) / C4)) < 10
    r_upper = sum(_harmonic(reed, C4, k) for k in (2, 3, 4, 5)) / _harmonic(reed, C4, 1)
    f_upper = sum(_harmonic(flute, C4, k) for k in (2, 3, 4, 5)) / _harmonic(flute, C4, 1)
    assert r_upper > 3.0 * f_upper


@pytest.mark.parametrize("model", WIND_MODELS)
def test_releasing_the_breath_stops_the_pipe_to_exact_zeros(model):
    sr = 44100
    sig = _render(0.0, seconds=2.0, off_at=1.0, params={"model": model, "release": 0.08})
    held = _rms(sig[int(0.8 * sr):int(0.95 * sr)])
    lifting = _rms(sig[int(1.02 * sr):int(1.06 * sr)])
    gone = _rms(sig[int(1.5 * sr):int(1.6 * sr)])
    assert lifting < held
    assert gone < held * 1e-3
    assert np.all(sig[-512:] == 0.0)


# ----- breath ----------------------------------------------------------------


def test_reed_breath_raises_level_monotonically():
    levels = [_rms(_render(0.0, params={"model": "reed", "breath": b})[22050:])
              for b in (0.3, 0.6, 1.0)]
    assert levels[0] < levels[1] < levels[2]


def test_flute_speaks_across_its_whole_breath_range():
    for b in (0.0, 0.25, 0.5, 0.75, 1.0):
        sig = _render(0.0, params={"model": "flute", "breath": b})[22050:]
        assert _rms(sig) > 0.05, f"breath {b} is silent"
        assert abs(1200 * np.log2(_partial_freq(sig, 44100, C4) / C4)) < 15


def test_breath_noise_is_seeded_per_note_and_audible():
    a = _render(0.0, seconds=0.5, params={"model": "flute", "seed": 1})
    b = _render(0.0, seconds=0.5, params={"model": "flute", "seed": 1})
    c = _render(0.0, seconds=0.5, params={"model": "flute", "seed": 2})
    d = _render(0.0, seconds=0.5, params={"model": "flute", "seed": 1, "noise": 0.0})
    assert np.array_equal(a, b)                 # deterministic
    assert not np.array_equal(a, c)             # the seed matters
    assert not np.array_equal(a, d)             # and the noise is in the signal


def test_per_sample_breath_cv_lands_within_the_block():
    sr = 44100
    sig = _render(0.0, seconds=2.0, params={"model": "reed", "breath": 0.3, "cv_depth": 1.0},
                  breath_cv=lambda t: (t >= sr).astype(np.float32) * 0.6)
    before = _rms(sig[int(0.7 * sr):int(0.95 * sr)])
    after = _rms(sig[int(1.6 * sr):int(1.9 * sr)])
    assert after > 1.1 * before
    off = _render(0.0, seconds=2.0, params={"model": "reed", "breath": 0.3, "cv_depth": 0.0},
                  breath_cv=lambda t: (t >= sr).astype(np.float32) * 0.6)
    assert abs(_rms(off[int(1.6 * sr):int(1.9 * sr)]) - _rms(off[int(0.7 * sr):int(0.95 * sr)])) < 0.05


@pytest.mark.parametrize("model", WIND_MODELS)
def test_damping_darkens(model):
    bright = _render(0.0, params={"model": model, "damping": 0.0, "noise": 0.0})[22050:]
    dark = _render(0.0, params={"model": model, "damping": 1.0, "noise": 0.0})[22050:]
    assert _centroid(dark) < _centroid(bright)


# ----- voices / blocks / lifecycle ------------------------------------------


def test_voice_rows_are_independent_pipes():
    step = _driver(params={"model": "reed"}, sr=8000, block=64)
    pitch = np.zeros((2, 64), dtype=np.float32)
    gate = np.zeros((2, 64), dtype=np.float32)
    gate[0, :] = 1.0
    out = None
    for _ in range(12):
        out = step(pitch, gate)
    assert out.shape == (2, 64)
    assert np.any(out[0] != 0.0)
    assert np.all(out[1] == 0.0)


@pytest.mark.parametrize("model", WIND_MODELS)
def test_single_voice_row_matches_mono(model):
    mono = _driver(params={"model": model}, sr=8000, block=64)
    voiced = _driver(params={"model": model}, sr=8000, block=64)
    for i in range(8):
        gate = np.ones(64, dtype=np.float32) if i < 5 else np.zeros(64, dtype=np.float32)
        m = mono(np.zeros(64, dtype=np.float32), gate)
        v = voiced(np.zeros((1, 64), dtype=np.float32), gate[None, :])
        assert np.array_equal(m, v[0])


@pytest.mark.parametrize("model", WIND_MODELS)
def test_block_size_independent_at_constant_pitch(model):
    gate_full = np.zeros(1024, dtype=np.float32)
    gate_full[37:700] = 1.0
    big = _driver(params={"model": model}, sr=8000, block=1024)
    small = _driver(params={"model": model}, sr=8000, block=64)
    out_big = big(np.zeros(1024, dtype=np.float32), gate_full)
    outs = [small(np.zeros(64, dtype=np.float32), gate_full[i:i + 64])
            for i in range(0, 1024, 64)]
    assert np.array_equal(out_big, np.concatenate(outs))


def test_a_released_silent_voice_early_outs_to_exact_zeros():
    step = _driver(params={"model": "reed", "release": 0.01}, sr=8000, block=64)
    for _ in range(8):
        step(np.zeros(64, dtype=np.float32), np.ones(64, dtype=np.float32))
    last = None
    for _ in range(200):
        last = step(np.zeros(64, dtype=np.float32), np.zeros(64, dtype=np.float32))
    assert np.all(last == 0.0)
    assert not step.backend._state[step.module.id]["active"][0]


def test_unpatched_everything_is_silent_and_stateless():
    patch = Patch()
    m = patch.add_module("wind")
    b = NumpyBackend(sample_rate=8000, block_size=64)
    b.compile(patch)
    out = b._render_wind(patch.get(m.id), 64, {}, patch)
    assert np.all(out == 0.0)
    assert m.id not in b._state


def test_switching_model_mid_stream_resets_cleanly():
    step = _driver(params={"model": "flute"}, sr=8000, block=64)
    for _ in range(6):
        step(np.zeros(64, dtype=np.float32), np.ones(64, dtype=np.float32))
    step.module.params["model"] = "reed"
    out = step(np.zeros(64, dtype=np.float32), np.ones(64, dtype=np.float32))
    assert np.all(np.isfinite(out))
    assert step.backend._state[step.module.id]["model"] == "reed"


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("wind")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("items"))
    return out


def test_every_param_gets_a_bounded_widget_and_model_offers_both(monkeypatch):
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("wind").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["model"] == ("add_combo", list(WIND_MODELS))
    assert w["seed"][0] == "add_drag_int"


# ----- example ------------------------------------------------------------------


def test_the_duet_example_plays_both_models():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "wind_duet.json"
    patch = load_patch(path)
    winds = {m.params["model"]: m for m in patch if m.TYPE == "wind"}
    assert set(winds) == {"flute", "reed"}
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    cap = {}
    orig = b._render_wind

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.setdefault(module.id, []).append(np.asarray(r).copy())
        return r

    b._render_wind = spy
    peak = 0.0
    for _ in range(int(44100 * 4 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.1 < peak < 1.0
    for model, m in winds.items():
        sig = np.concatenate(cap[m.id])
        assert _rms(sig[44100:]) > 0.02, f"the {model} is silent"
