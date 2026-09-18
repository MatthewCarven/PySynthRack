"""Bowed — the bowed-string waveguide.

Pins the contract: a sustained note (level holds while the gate is high,
bounded — no runaway), pitch within ±10 cents C2..C6 at the defaults (the
exact one-pole phase compensation on the loop), the bow lifting on gate
off (release ramp, then the string decays to exact zeros and the voice
early-outs), the two hands (velocity raises level, pressure changes the
tone and level), position and damping and body shaping the spectrum,
per-sample pressure/velocity CV, voice independence with mono ≡
single-voice parity, block-size independence at constant pitch, and
unpatched behaviour.

Pitch/level tests run at 44100 Hz (they measure real frequencies);
plumbing tests run fast at SR 4000.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules import bowed as _bowed  # noqa: F401

C4 = 261.6255653005986


def _driver(params=None, sr=44100, block=512, cvs=False):
    """Backend + `constant → pitch_cv, clock → gate` bowed patch; ``cvs``
    also cables two constants into pressure_cv / velocity_cv."""
    patch = Patch()
    m = patch.add_module("bowed", params=params or {})
    psrc = patch.add_module("constant")
    patch.connect(psrc.id, "out", m.id, "pitch_cv")
    gsrc = patch.add_module("clock")
    patch.connect(gsrc.id, "out", m.id, "gate")
    keys = {"pitch": (psrc.id, "out"), "gate": (gsrc.id, "out")}
    if cvs:
        pc = patch.add_module("constant")
        vc = patch.add_module("constant")
        patch.connect(pc.id, "out", m.id, "pressure_cv")
        patch.connect(vc.id, "out", m.id, "velocity_cv")
        keys["pressure"] = (pc.id, "out")
        keys["velocity"] = (vc.id, "out")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(pitch_block, gate_block, **cv_blocks):
        arr = np.asarray(pitch_block, dtype=np.float32)
        bufs = {keys["pitch"]: arr,
                keys["gate"]: np.asarray(gate_block, dtype=np.float32)}
        for name, buf in cv_blocks.items():
            bufs[keys[name]] = np.asarray(buf, dtype=np.float32)
        return b._render_bowed(patch.get(m.id), arr.shape[-1], bufs, patch)

    step.module = m
    step.patch = patch
    step.backend = b
    return step


def _render(cv, seconds=1.0, params=None, sr=44100, block=512, off_at=None,
            pressure_cv=None, velocity_cv=None):
    """Gate high from t=0 (until ``off_at`` seconds); mono signal."""
    step = _driver(params, sr=sr, block=block,
                   cvs=pressure_cv is not None or velocity_cv is not None)
    out = []
    for i in range(int(seconds * sr / block)):
        t = np.arange(i * block, (i + 1) * block)
        gate = np.ones(block, dtype=np.float32) if off_at is None else (
            t < off_at * sr).astype(np.float32)
        extra = {}
        if pressure_cv is not None:
            extra["pressure"] = pressure_cv(t)
        if velocity_cv is not None:
            extra["velocity"] = velocity_cv(t)
        out.append(step(np.full(block, cv, dtype=np.float32), gate, **extra))
    return np.concatenate(out)


def _partial_freq(sig, sr, f_near):
    """Frequency of the spectral peak within ±8% of ``f_near`` (Hann +
    parabolic interpolation, the pluck tests' measure)."""
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


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["bowed"]
    assert cls is get_module_type("bowed")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["pitch_cv", "gate", "pressure_cv", "velocity_cv"]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["pressure"] == 0.5
    assert m.params["position"] == pytest.approx(0.127)


def test_serialization_round_trip():
    cls = all_module_types()["bowed"]
    m = cls(2, params={"pressure": 0.8, "position": 0.3, "body": 0.0})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the note -------------------------------------------------------------


def test_the_note_sustains_and_stays_bounded():
    sig = _render(0.0, seconds=2.0)
    sr = 44100
    early = _rms(sig[int(0.3 * sr):int(0.5 * sr)])
    late = _rms(sig[int(1.7 * sr):int(1.9 * sr)])
    assert early > 0.05                       # it speaks
    assert 0.5 < late / early < 2.0           # and holds, neither dying nor running away
    assert np.abs(sig).max() < 1.0
    assert np.all(np.isfinite(sig))


@pytest.mark.parametrize("cv", [-2.0, -1.0, 0.0, 1.0, 2.0])  # C2..C6
def test_pitch_within_10_cents(cv):
    sig = _render(cv, seconds=1.2, params={"body": 0.0})
    tail = sig[len(sig) // 3:]
    expected = C4 * 2.0 ** cv
    measured = _partial_freq(tail, 44100, expected)
    cents = 1200.0 * np.log2(measured / expected)
    assert abs(cents) < 10.0, f"cv {cv}: {measured:.2f} Hz vs {expected:.2f} ({cents:+.1f} ct)"


def test_fundamental_carries_energy():
    sig = _render(0.0, seconds=1.0, params={"body": 0.0})
    tail = sig[len(sig) // 2:]
    spec = np.abs(np.fft.rfft(tail * np.hanning(len(tail))))
    f0_bin = int(len(tail) * C4 / 44100)
    local = spec[max(1, f0_bin - 4):f0_bin + 5].max()
    assert local > 0.1 * spec.max()


def test_lifting_the_bow_releases_then_the_string_dies_to_exact_zeros():
    sr = 44100
    sig = _render(0.0, seconds=2.0, off_at=1.0, params={"release": 0.1})
    held = _rms(sig[int(0.8 * sr):int(0.95 * sr)])
    lifting = _rms(sig[int(1.03 * sr):int(1.08 * sr)])
    gone = _rms(sig[int(1.5 * sr):int(1.6 * sr)])
    assert lifting < held                     # the release ramp is under way
    assert gone < held * 1e-3                 # and the string has stopped
    assert np.all(sig[-512:] == 0.0)          # early-out: exact zeros


def test_attack_time_shapes_how_fast_it_speaks():
    sr = 44100
    fast = _render(0.0, seconds=0.5, params={"attack": 0.01})
    slow = _render(0.0, seconds=0.5, params={"attack": 0.3})
    win = slice(int(0.03 * sr), int(0.08 * sr))
    assert _rms(fast[win]) > 3.0 * _rms(slow[win])


# ----- the two hands ----------------------------------------------------------


def test_velocity_raises_level_monotonically():
    levels = [_rms(_render(0.0, params={"velocity": v, "body": 0.0})[22050:])
              for v in (0.2, 0.6, 1.0)]
    assert levels[0] < levels[1] < levels[2]


def test_pressure_changes_the_tone_and_the_level():
    light = _render(0.0, params={"pressure": 0.2, "body": 0.0})[22050:]
    heavy = _render(0.0, params={"pressure": 0.8, "body": 0.0})[22050:]
    assert _rms(heavy) > 1.3 * _rms(light)    # pressed harder: more drive
    sl = np.abs(np.fft.rfft(light * np.hanning(len(light))))
    sh = np.abs(np.fft.rfft(heavy * np.hanning(len(heavy))))
    sl /= sl.max()
    sh /= sh.max()
    assert np.abs(sl - sh).max() > 0.1        # and a different spectrum


def test_per_sample_pressure_cv_lands_within_the_block():
    # A step on pressure_cv halfway through the note: the second half is
    # driven harder (the velocity-CV path is the same code, one array).
    sr = 44100
    sig = _render(0.0, seconds=2.0, params={"pressure": 0.2, "body": 0.0},
                  pressure_cv=lambda t: (t >= sr).astype(np.float32) * 0.6)
    before = _rms(sig[int(0.7 * sr):int(0.95 * sr)])
    after = _rms(sig[int(1.6 * sr):int(1.9 * sr)])
    assert after > 1.3 * before


def test_velocity_cv_is_scaled_by_cv_depth():
    sr = 44100
    sig_on = _render(0.0, params={"velocity": 0.2, "body": 0.0, "cv_depth": 1.0},
                     velocity_cv=lambda t: np.full(len(t), 0.8, np.float32))
    sig_off = _render(0.0, params={"velocity": 0.2, "body": 0.0, "cv_depth": 0.0},
                      velocity_cv=lambda t: np.full(len(t), 0.8, np.float32))
    assert _rms(sig_on[sr // 2:]) > 1.2 * _rms(sig_off[sr // 2:])


# ----- the string -------------------------------------------------------------


def test_bow_near_the_bridge_is_brighter_than_sul_tasto():
    near = _render(0.0, params={"position": 0.06, "body": 0.0})[22050:]
    tasto = _render(0.0, params={"position": 0.3, "body": 0.0})[22050:]
    assert _centroid(near) > 1.2 * _centroid(tasto)


def test_damping_darkens_monotonically():
    cents = [_centroid(_render(0.0, params={"damping": d, "body": 0.0})[22050:])
             for d in (0.0, 0.5, 1.0)]
    assert cents[0] > cents[1] > cents[2]


def test_body_colours_the_spectrum_without_changing_the_note():
    raw = _render(0.0, params={"body": 0.0})[22050:]
    boxed = _render(0.0, params={"body": 1.0})[22050:]
    assert _centroid(boxed) < 0.9 * _centroid(raw)
    assert abs(_partial_freq(boxed, 44100, C4) - _partial_freq(raw, 44100, C4)) < 1.0
    assert 0.5 < _rms(boxed) / _rms(raw) < 2.0   # level-matched bank


# ----- voices / blocks / lifecycle ------------------------------------------


def test_voice_rows_are_independent_strings():
    step = _driver(sr=4000, block=64)
    pitch = np.zeros((2, 64), dtype=np.float32)
    gate = np.zeros((2, 64), dtype=np.float32)
    gate[0, :] = 1.0                          # bow only voice 0
    out = None
    for _ in range(8):
        out = step(pitch, gate)
    assert out.shape == (2, 64)
    assert np.any(out[0] != 0.0)
    assert np.all(out[1] == 0.0)


def test_single_voice_row_matches_mono():
    mono = _driver(sr=4000, block=64)
    voiced = _driver(sr=4000, block=64)
    for i in range(6):
        gate = np.ones(64, dtype=np.float32) if i < 4 else np.zeros(64, dtype=np.float32)
        m = mono(np.zeros(64, dtype=np.float32), gate)
        v = voiced(np.zeros((1, 64), dtype=np.float32), gate[None, :])
        assert np.array_equal(m, v[0])


def test_block_size_independent_at_constant_pitch():
    gate_full = np.zeros(1024, dtype=np.float32)
    gate_full[37:700] = 1.0                   # bow lands, plays, lifts mid-stream
    big = _driver(sr=4000, block=1024)
    small = _driver(sr=4000, block=64)
    out_big = big(np.zeros(1024, dtype=np.float32), gate_full)
    outs = [small(np.zeros(64, dtype=np.float32), gate_full[i:i + 64])
            for i in range(0, 1024, 64)]
    assert np.array_equal(out_big, np.concatenate(outs))


def test_re_bowing_during_the_release_does_not_jump():
    # Gate off for 20 ms then on again: the bow picks up from where the
    # release left the envelope, so no sample-to-sample discontinuity
    # beyond what the sustained tone already has.
    sr = 44100
    step = _driver(params={"release": 0.2, "attack": 0.05, "body": 0.0}, sr=sr, block=512)
    out = []
    for i in range(int(1.2 * sr / 512)):
        t = np.arange(i * 512, (i + 1) * 512)
        gate = ~((t >= int(0.6 * sr)) & (t < int(0.62 * sr)))
        out.append(step(np.zeros(512, dtype=np.float32), gate.astype(np.float32)))
    sig = np.concatenate(out)
    steady = np.abs(np.diff(sig[int(0.4 * sr):int(0.58 * sr)])).max()
    around = np.abs(np.diff(sig[int(0.59 * sr):int(0.7 * sr)])).max()
    assert around < 1.5 * steady


def test_a_lifted_silent_voice_early_outs_to_exact_zeros():
    step = _driver(params={"release": 0.01}, sr=4000, block=64)
    for _ in range(6):
        step(np.zeros(64, dtype=np.float32), np.ones(64, dtype=np.float32))
    last = None
    for _ in range(120):
        last = step(np.zeros(64, dtype=np.float32), np.zeros(64, dtype=np.float32))
    assert np.all(last == 0.0)
    assert not step.backend._state[step.module.id]["active"][0]


def test_unpatched_everything_is_silent_and_stateless():
    patch = Patch()
    m = patch.add_module("bowed")
    b = NumpyBackend(sample_rate=4000, block_size=64)
    b.compile(patch)
    out = b._render_bowed(patch.get(m.id), 64, {}, patch)
    assert np.all(out == 0.0)
    assert m.id not in b._state


def test_gate_low_from_the_start_is_free_and_silent():
    step = _driver(sr=4000, block=64)
    for _ in range(4):
        out = step(np.zeros(64, dtype=np.float32), np.zeros(64, dtype=np.float32))
        assert np.all(out == 0.0)
    assert not step.backend._state[step.module.id]["active"][0]


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("bowed")
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
    for name in get_module_type("bowed").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["attack"][1].endswith(" s")
    assert "lvl/unit" in w["cv_depth"][1]


# ----- example ------------------------------------------------------------------


def test_the_cello_example_plays_a_moving_line():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "bowed_cello.json"
    patch = load_patch(path)
    bow = next(m for m in patch if m.TYPE == "bowed")
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    cap = []
    orig = b._render_bowed

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.append(np.asarray(r).copy())
        return r

    b._render_bowed = spy
    peak = 0.0
    for _ in range(int(44100 * 4 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.1 < peak < 1.0
    sig = np.concatenate(cap)
    # Steps 1 and 2 of the line are C2 and G2 (−24 / −17 semitones): the
    # string is at each pitch during the second half of its second.
    sr = 44100
    first = sig[int(0.5 * sr):int(0.95 * sr)]
    second = sig[int(1.5 * sr):int(1.95 * sr)]
    assert abs(1200 * np.log2(_partial_freq(first, sr, C4 / 4) / (C4 / 4))) < 30
    assert abs(1200 * np.log2(_partial_freq(second, sr, C4 * 2 ** (-17 / 12)) / (C4 * 2 ** (-17 / 12)))) < 30
