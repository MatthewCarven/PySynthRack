"""Octaver — zero-crossing flip-flop sub-octaves under the dry.

Pins the contract: a sine at f puts the sub squares' fundamentals at
exactly f/2 (sub1) and f/4 (sub2) — FFT-verified well above the floor;
sub gains isolate their own octave; the subs ride the input's envelope
(silence in → silence out, tails release); `tone` low-passes the
squares' upper harmonics; dry-only is a bit-exact passthrough (the
input buffer itself); all state carries across blocks (block-size
independent); unpatched input is silence.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type

SR = 8000


def _driver(params=None, block=256, sr=SR):
    patch = Patch()
    oc = patch.add_module("octaver", params=params or {})
    src = patch.add_module("oscillator")
    patch.connect(src.id, "out", oc.id, "in")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(x):
        arr = np.asarray(x, dtype=np.float32)
        return b._render_octaver(
            patch.get(oc.id), arr.shape[-1], {(src.id, "out"): arr}, patch
        )

    step.oc = oc
    step.patch = patch
    step.backend = b
    return step


def _sine(freq, seconds=1.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _peak_near(x, f0, sr=SR, tol=10.0):
    """Windowed spectrum magnitude near f0."""
    n = len(x)
    spec = np.abs(np.fft.rfft(np.asarray(x, dtype=np.float64) * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    mask = (freqs > f0 - tol) & (freqs < f0 + tol)
    return float(spec[mask].max())


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["octaver"]
    assert cls is get_module_type("octaver")
    assert cls.CATEGORY == "Effects"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["in"]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["dry"] == 1.0
    assert m.params["sub1"] == 0.5
    assert m.params["sub2"] == 0.0
    assert m.params["tone"] == 800.0


def test_serialization_round_trip():
    cls = all_module_types()["octaver"]
    m = cls(3, params={"dry": 0.3, "sub2": 0.8, "tone": 400.0})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the octaves -----------------------------------------------------------


def test_subs_land_half_and_quarter_frequency():
    step = _driver({"dry": 0.0, "sub1": 1.0, "sub2": 1.0, "tone": 2000.0})
    out = step(_sine(400.0))
    floor = _peak_near(out, 330.0)  # nothing musical lives here
    assert _peak_near(out, 200.0) > 10 * floor  # f/2
    assert _peak_near(out, 100.0) > 10 * floor  # f/4


def test_sub_gains_isolate_their_octave():
    only1 = _driver({"dry": 0.0, "sub1": 1.0, "sub2": 0.0, "tone": 2000.0})
    out1 = step_out = only1(_sine(400.0))
    assert _peak_near(out1, 200.0) > 10 * _peak_near(out1, 100.0)
    only2 = _driver({"dry": 0.0, "sub1": 0.0, "sub2": 1.0, "tone": 2000.0})
    out2 = only2(_sine(400.0))
    assert _peak_near(out2, 100.0) > 10 * _peak_near(out2, 200.0)


def test_tone_rounds_the_square():
    """The 200 Hz sub square's 3rd harmonic (600 Hz) drops when tone
    closes from 2000 Hz to 200 Hz; the fundamental survives."""
    bright = _driver({"dry": 0.0, "sub1": 1.0, "tone": 2000.0})(_sine(400.0))
    dark = _driver({"dry": 0.0, "sub1": 1.0, "tone": 200.0})(_sine(400.0))
    # One-pole (6 dB/oct): |H(600)| at tone 200 ≈ 0.32 vs ≈ 0.96 at
    # tone 2000 — expect ~0.33x, allow up to 0.4x.
    assert _peak_near(dark, 600.0) < 0.4 * _peak_near(bright, 600.0)
    assert _peak_near(dark, 200.0) > 0.4 * _peak_near(bright, 200.0)


# ----- envelope gating -------------------------------------------------------


def test_silence_in_silence_out_with_subs_up():
    step = _driver({"dry": 0.0, "sub1": 1.0, "sub2": 1.0})
    out = step(np.zeros(SR, dtype=np.float32))
    assert np.max(np.abs(out)) < 1e-9


def test_sub_tail_releases_after_the_note_stops():
    step = _driver({"dry": 0.0, "sub1": 1.0, "tone": 2000.0})
    step(_sine(400.0, 0.5))  # note rings; follower charged
    tail = step(np.zeros(SR, dtype=np.float32))  # 1 s of silence
    # 50 ms release: by the last quarter the sub is gone.
    assert np.max(np.abs(tail[-SR // 4 :])) < 1e-3
    # ...but it did ring INTO the silence briefly (the release tail).
    assert np.max(np.abs(tail[: SR // 100])) > 1e-3


# ----- passthrough / edges ---------------------------------------------------


def test_dry_only_is_bit_exact_passthrough():
    step = _driver()  # defaults but subs zeroed:
    step.oc.params.update({"sub1": 0.0, "sub2": 0.0, "dry": 1.0})
    x = _sine(400.0, 0.1)
    out = step(x)
    assert out is x  # the input buffer itself (fan-out precedent)


def test_unpatched_input_is_silence():
    patch = Patch()
    oc = patch.add_module("octaver")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    out = b._render_octaver(patch.get(oc.id), 64, {}, patch)
    assert not np.any(out)


# ----- block-size independence ----------------------------------------------


def test_block_size_independent():
    x = (_sine(313.0, 0.5) * np.linspace(1.0, 0.2, SR // 2)).astype(np.float32)
    big = _driver({"dry": 0.4, "sub1": 0.8, "sub2": 0.5, "tone": 600.0})
    small = _driver({"dry": 0.4, "sub1": 0.8, "sub2": 0.5, "tone": 600.0})
    out_big = np.asarray(big(x))
    parts = [np.asarray(small(x[i : i + 160])) for i in range(0, len(x), 160)]
    np.testing.assert_allclose(out_big, np.concatenate(parts), atol=1e-6)


# ----- integration -----------------------------------------------------------


def test_full_graph_render_under_a_played_voice():
    patch = Patch()
    kb = patch.add_module("cv_keyboard")
    osc = patch.add_module("oscillator")
    env = patch.add_module("adsr")
    vca = patch.add_module("vca")
    oc = patch.add_module("octaver", params={"sub1": 0.7})
    out = patch.add_module("speaker_output")
    patch.connect(kb.id, "pitch_cv", osc.id, "freq_cv")
    patch.connect(kb.id, "gate", env.id, "gate")
    patch.connect(osc.id, "out", vca.id, "audio")
    patch.connect(env.id, "cv", vca.id, "cv")
    patch.connect(vca.id, "out", oc.id, "in")
    patch.connect(oc.id, "out", out.id, "in")
    kb.note_on(64)
    b = NumpyBackend(sample_rate=44100, block_size=256)
    b.compile(patch)
    for _ in range(8):
        mixdown, _dev = b.render_block_multi(256)
        assert mixdown is not None and np.all(np.isfinite(mixdown))
