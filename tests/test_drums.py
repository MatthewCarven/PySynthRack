"""Drum voices — kick, snare, hat.

Pins the contract: the kick's rendered hit matches the closed-form
synthesis exactly (deterministic, seeded per hit), its tail settles on
freq_end and carries no DC, tune shifts it, click adds attack noise;
the snare's head modes land at 185/330 Hz with snappy balancing shell
vs band-limited wires; the hat is high-passed with closed short / open
long and the closed hit CHOKING a ringing open one; retriggers declick
(2 ms fades, no replace jump); everything is block-size independent by
construction; unpatched triggers are silent.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import (
    NumpyBackend,
    _hat_hit,
    _kick_hit,
    _snare_hit,
)
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules import drums as _drums  # noqa: F401

SR = 44100


def _driver(module_type, params=None, jacks=("trigger",), sr=SR, block=512):
    patch = Patch()
    m = patch.add_module(module_type, params=params or {})
    feeds = {}
    for jack in jacks:
        src = patch.add_module("clock")
        patch.connect(src.id, "out", m.id, jack)
        feeds[jack] = src
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)
    render = {
        "kick_drum": b._render_kick,
        "snare_drum": b._render_snare,
        "hat_drum": b._render_hat,
    }[module_type]

    def step(**blocks):
        frames = len(next(iter(blocks.values())))
        buffers = {
            (feeds[j].id, "out"): np.asarray(a, dtype=np.float32)
            for j, a in blocks.items()
        }
        return render(patch.get(m.id), frames, buffers, patch)

    step.module = m
    step.backend = b
    return step


def _one_hit(module_type, params=None, seconds=1.0, jack="trigger", block=512):
    step = _driver(module_type, params, jacks=(jack,))
    out = []
    for i in range(int(seconds * SR / block)):
        t = np.zeros(block, dtype=np.float32)
        if i == 0:
            t[0] = 1.0
        out.append(step(**{jack: t}))
    return np.concatenate(out)


# ----- registration ----------------------------------------------------------


@pytest.mark.parametrize(
    "type_name,in_ports",
    [
        ("kick_drum", ["trigger"]),
        ("snare_drum", ["trigger"]),
        ("hat_drum", ["closed_trigger", "open_trigger"]),
    ],
)
def test_registered_with_ports(type_name, in_ports):
    cls = all_module_types()[type_name]
    assert cls is get_module_type(type_name)
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == in_ports
    assert [p.name for p in m.output_ports] == ["out"]
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- kick ------------------------------------------------------------------


def test_kick_matches_closed_form_exactly():
    out = _one_hit("kick_drum", {"level": 1.0}, seconds=0.6)
    rng = np.random.default_rng((0x44524D53, 1, 0))  # module id 1, hit 0
    expected = _kick_hit(SR, 180.0, 50.0, 0.04, 0.35, 0.3, 0.0, 0.0, rng)
    n = min(len(out), len(expected))
    assert np.allclose(out[:n], expected[:n].astype(np.float32), atol=1e-7)


def test_kick_tail_settles_on_freq_end():
    out = _one_hit(
        "kick_drum", {"decay": 800.0, "click": 0.0, "freq_end": 50.0}, seconds=1.0
    )
    tail = out[int(0.4 * SR) : int(0.8 * SR)]
    # Zero-crossing rate → frequency.
    crossings = np.flatnonzero((tail[:-1] < 0) & (tail[1:] >= 0))
    freq = (len(crossings) - 1) / ((crossings[-1] - crossings[0]) / SR)
    assert abs(freq - 50.0) / 50.0 < 0.05


def test_kick_has_no_dc():
    out = _one_hit("kick_drum", {"click": 0.5}, seconds=1.0)
    assert abs(float(np.mean(out))) < 0.005


def test_kick_tune_shifts_the_tail():
    up = _one_hit(
        "kick_drum", {"tune": 12.0, "click": 0.0, "decay": 800.0}, seconds=1.0
    )
    tail = up[int(0.4 * SR) : int(0.8 * SR)]
    crossings = np.flatnonzero((tail[:-1] < 0) & (tail[1:] >= 0))
    freq = (len(crossings) - 1) / ((crossings[-1] - crossings[0]) / SR)
    assert abs(freq - 100.0) / 100.0 < 0.05  # 50 Hz + 12 st = 100 Hz


def test_kick_click_adds_attack_noise():
    quiet = _one_hit("kick_drum", {"click": 0.0}, seconds=0.05)
    clicky = _one_hit("kick_drum", {"click": 1.0}, seconds=0.05)
    n = int(0.002 * SR)

    def hf(x):
        d = np.diff(x[:n])
        return float(np.sum(d * d))

    assert hf(clicky) > 10 * hf(quiet)


def test_kick_drive_saturates():
    clean = _one_hit("kick_drum", {"drive": 0.0, "click": 0.0}, seconds=0.3)
    driven = _one_hit("kick_drum", {"drive": 1.0, "click": 0.0}, seconds=0.3)
    # Saturation flattens the crest: RMS rises relative to peak.
    def crest(x):
        return float(np.abs(x).max() / (np.sqrt(np.mean(x**2)) + 1e-12))

    assert crest(driven) < crest(clean)


# ----- snare -----------------------------------------------------------------


def test_snare_head_modes_land():
    out = _one_hit(
        "snare_drum",
        {"snappy": 0.0, "tone_decay": 400.0, "level": 1.0},
        seconds=0.5,
    )
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    n = len(out)
    for f in (185.0, 330.0):
        b0 = int(n * f / SR)
        assert spec[b0 - 3 : b0 + 4].max() > 20 * np.median(spec), f"{f} Hz missing"


def test_snare_snappy_balances_shell_and_wires():
    def noise_ratio(snappy):
        out = _one_hit("snare_drum", {"snappy": snappy}, seconds=0.3)
        spec = np.abs(np.fft.rfft(out))
        n = len(out)
        cut = int(n * 600.0 / SR)
        hi = float((spec[cut:] ** 2).sum())
        lo = float((spec[:cut] ** 2).sum())
        return hi / (lo + 1e-12)

    assert noise_ratio(1.0) > 10 * noise_ratio(0.0)


def test_snare_wires_are_band_limited():
    out = _one_hit("snare_drum", {"snappy": 1.0}, seconds=0.3)
    spec = np.abs(np.fft.rfft(out)) ** 2
    n = len(out)
    below = float(spec[: int(n * 300.0 / SR)].sum())
    total = float(spec.sum())
    assert below < 0.05 * total


# ----- hat -------------------------------------------------------------------


def test_hat_is_high_passed():
    out = _one_hit("hat_drum", {}, seconds=0.2, jack="closed_trigger")
    spec = np.abs(np.fft.rfft(out)) ** 2
    n = len(out)
    below = float(spec[: int(n * 3000.0 / SR)].sum())
    assert below < 0.05 * float(spec.sum())


def test_hat_closed_shorter_than_open():
    closed = _one_hit("hat_drum", {}, seconds=0.6, jack="closed_trigger")
    opened = _one_hit("hat_drum", {}, seconds=0.6, jack="open_trigger")
    k = int(0.15 * SR)
    assert float(np.sum(opened[k:] ** 2)) > 50 * float(np.sum(closed[k:] ** 2))


def test_hat_closed_chokes_open():
    step = _driver("hat_drum", {}, jacks=("closed_trigger", "open_trigger"))
    block = 512
    outs = []
    for i in range(40):
        c = np.zeros(block, dtype=np.float32)
        o = np.zeros(block, dtype=np.float32)
        if i == 0:
            o[0] = 1.0  # open hit rings...
        if i == 8:
            c[0] = 1.0  # ...then the pedal comes down
        outs.append(step(closed_trigger=c, open_trigger=o))
    sig = np.concatenate(outs)
    # Shortly after the choke (+ closed's own short decay), silence —
    # the open hit alone would still be ringing here.
    k = 20 * block
    choked_tail = float(np.sum(sig[k:] ** 2))
    alone = _one_hit("hat_drum", {}, seconds=0.6, jack="open_trigger")
    open_tail = float(np.sum(alone[k : len(sig)] ** 2))
    assert choked_tail < 0.02 * open_tail


# ----- shared engine ---------------------------------------------------------


def test_retrigger_declicks():
    step = _driver("kick_drum", {"click": 0.0, "decay": 600.0})
    block = 512
    outs = []
    for i in range(30):
        t = np.zeros(block, dtype=np.float32)
        if i == 0:
            t[0] = 1.0
        if i == 10:
            t[77] = 1.0
        outs.append(step(trigger=t))
    sig = np.concatenate(outs)
    k = 10 * block + 77
    jump = float(np.max(np.abs(np.diff(sig[k - 32 : k + 96]))))
    # A sine at ~180 Hz moves at most 2π·180/SR ≈ 0.026/sample; the
    # 2 ms crossfade keeps the retrigger in that class (replace ≈ 2.0).
    assert jump < 0.1


def test_block_size_independent():
    trig = np.zeros(2048, dtype=np.float32)
    trig[300] = 1.0
    trig[1400] = 1.0
    big = _driver("snare_drum", {}, block=2048)
    small = _driver("snare_drum", {}, block=256)
    out_big = big(trigger=trig)
    outs = [small(trigger=trig[i : i + 256]) for i in range(0, 2048, 256)]
    assert np.array_equal(out_big, np.concatenate(outs))


def test_renders_deterministic():
    a = _one_hit("snare_drum", {}, seconds=0.2)
    b = _one_hit("snare_drum", {}, seconds=0.2)
    assert np.array_equal(a, b)


def test_unpatched_trigger_is_silent():
    patch = Patch()
    m = patch.add_module("kick_drum")
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)
    out = b._render_kick(patch.get(m.id), 256, {}, patch)
    assert np.all(out == 0.0)
