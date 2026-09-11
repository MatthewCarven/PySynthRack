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
        ("kick_drum", ["trigger", "vel", "pitch_cv"]),
        ("snare_drum", ["trigger", "vel"]),
        ("hat_drum", ["closed_trigger", "open_trigger", "vel"]),
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


# ----- velocity / pitch_cv / tone (the 2026-09-11 love pass) ----------------


def _driver_cv(module_type, params=None, jacks=("trigger",), cvs=("vel",),
               block=512):
    """Like _driver, plus constant-module feeds on the named CV jacks."""
    patch = Patch()
    m = patch.add_module(module_type, params=params or {})
    feeds = {}
    for jack in jacks:
        src = patch.add_module("clock")
        patch.connect(src.id, "out", m.id, jack)
        feeds[jack] = src
    for jack in cvs:
        src = patch.add_module("constant")
        patch.connect(src.id, "out", m.id, jack)
        feeds[jack] = src
    b = NumpyBackend(sample_rate=SR, block_size=block)
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
    return step


def _hit_with(module_type, params, jack, seconds=0.5, block=512, **cv_values):
    """One hit at sample 0 with constant CV buffers on the named jacks."""
    step = _driver_cv(module_type, params, jacks=(jack,), cvs=tuple(cv_values))
    out = []
    for i in range(int(seconds * SR / block)):
        t = np.zeros(block, dtype=np.float32)
        if i == 0:
            t[0] = 1.0
        feeds = {jack: t}
        for name, val in cv_values.items():
            feeds[name] = np.full(block, val, dtype=np.float32)
        out.append(step(**feeds))
    return np.concatenate(out)


def _rms_head(x):
    return float(np.sqrt(np.mean(x[: SR // 10] ** 2)))


@pytest.mark.parametrize(
    "type_name,jack",
    [("kick_drum", "trigger"), ("snare_drum", "trigger"),
     ("hat_drum", "closed_trigger"), ("hat_drum", "open_trigger")],
)
def test_vel_unpatched_is_bit_identical_to_before(type_name, jack):
    """The new jacks default to "not there": a pre-2026-09-11 patch renders
    exactly as it did."""
    plain = _one_hit(type_name, {"level": 1.0}, seconds=0.3, jack=jack)
    step = _driver_cv(type_name, {"level": 1.0}, jacks=(jack,), cvs=())
    out = []
    for i in range(int(0.3 * SR / 512)):
        t = np.zeros(512, dtype=np.float32)
        if i == 0:
            t[0] = 1.0
        out.append(step(**{jack: t}))
    assert np.array_equal(plain, np.concatenate(out))


@pytest.mark.parametrize(
    "type_name,jack",
    [("snare_drum", "trigger"), ("hat_drum", "closed_trigger"),
     ("hat_drum", "open_trigger")],
)
def test_vel_scales_the_hit(type_name, jack):
    full = _hit_with(type_name, {"level": 1.0}, jack, vel=1.0)
    half = _hit_with(type_name, {"level": 1.0}, jack, vel=0.5)
    assert np.allclose(half, 0.5 * full, atol=1e-7)


def test_vel_on_the_kick_is_before_the_drive():
    """A clean kick scales linearly; a driven one saturates LESS when hit
    softly -- the half-velocity hit is more than half the loud one."""
    clean = {"level": 1.0, "drive": 0.0}
    driven = {"level": 1.0, "drive": 0.8}
    clean_full = _hit_with("kick_drum", clean, "trigger", vel=1.0)
    clean_half = _hit_with("kick_drum", clean, "trigger", vel=0.5)
    assert np.allclose(clean_half, 0.5 * clean_full, atol=1e-7)
    driven_full = _hit_with("kick_drum", driven, "trigger", vel=1.0)
    driven_half = _hit_with("kick_drum", driven, "trigger", vel=0.5)
    ratio = _rms_head(driven_half) / _rms_head(driven_full)
    assert 0.5 < ratio < 0.95, ratio


def test_vel_is_read_at_the_edge_and_latched():
    """What the CV does after the hit changes nothing about that hit."""
    step = _driver_cv("snare_drum", {"level": 1.0}, cvs=("vel",))
    block = 512
    outs = []
    for i in range(20):
        t = np.zeros(block, dtype=np.float32)
        v = np.full(block, 0.9, dtype=np.float32)
        if i == 0:
            t[100] = 1.0
            v[100] = 0.25           # AT the edge: this one; the rest is noise
        outs.append(step(trigger=t, vel=v))
    latched = np.concatenate(outs)
    ref = _hit_with("snare_drum", {"level": 1.0}, "trigger", vel=0.25)
    n = min(len(ref), len(latched) - 100)
    assert np.allclose(latched[100:100 + n], ref[:n], atol=1e-7)


def test_vel_collapses_voices_to_the_loudest_at_the_edge():
    """A midi_input velocity bus is (V, F) with 0 on idle slots; the drum
    takes the max, not the house sum."""
    step = _driver_cv("snare_drum", {"level": 1.0}, cvs=("vel",))
    block = 512
    t = np.zeros(block, dtype=np.float32)
    t[0] = 1.0
    v = np.zeros((4, block), dtype=np.float32)
    v[2, :] = 0.6
    v[3, :] = 0.3
    out = step(trigger=t, vel=v)
    ref = _hit_with("snare_drum", {"level": 1.0}, "trigger", seconds=0.1, vel=0.6)
    assert np.allclose(out, ref[:block], atol=1e-7)


def test_vel_negative_is_silence():
    out = _hit_with("hat_drum", {"level": 1.0}, "closed_trigger", vel=-0.5)
    assert np.all(out == 0.0)


def test_kick_pitch_cv_is_one_volt_per_octave():
    """+1 V on pitch_cv is tune +12: the two renders are the same hit and
    the tail settles on 100 Hz."""
    p = {"click": 0.0, "decay": 800.0, "level": 1.0}
    by_cv = _hit_with("kick_drum", p, "trigger", seconds=1.0, pitch_cv=1.0)
    by_tune = _one_hit("kick_drum", dict(p, tune=12.0), seconds=1.0)
    assert np.allclose(by_cv, by_tune, atol=1e-6)
    tail = by_cv[int(0.4 * SR) : int(0.8 * SR)]
    crossings = np.flatnonzero((tail[:-1] < 0) & (tail[1:] >= 0))
    freq = (len(crossings) - 1) / ((crossings[-1] - crossings[0]) / SR)
    assert abs(freq - 100.0) / 100.0 < 0.05


def test_kick_pitch_cv_is_latched_per_hit():
    """A sequencer plays a tuned line: a hit keeps the pitch it was struck
    at even when the CV moves while it rings, and the next hit takes the
    new one."""
    p = {"click": 0.0, "decay": 300.0, "level": 1.0}
    step = _driver_cv("kick_drum", p, cvs=("pitch_cv",))
    block = 512
    outs = []
    for i in range(24):
        t = np.zeros(block, dtype=np.float32)
        cv = np.zeros(block, dtype=np.float32)
        if i == 0:
            t[0] = 1.0
        if i >= 2:
            cv[:] = 1.0          # moves an octave while the first hit rings
        if i == 12:
            t[0] = 1.0           # second hit: struck at +1 V
        outs.append(step(trigger=t, pitch_cv=cv))
    sig = np.concatenate(outs)
    low = _one_hit("kick_drum", p, seconds=0.3)
    high = _one_hit("kick_drum", dict(p, tune=12.0), seconds=0.3)
    # The first hit is the low hit all the way through -- including the
    # blocks where the CV had already moved.
    assert np.allclose(sig[: 12 * block], low[: 12 * block], atol=1e-6)
    # The second hit is the high one. (Its first 2 ms carry the first
    # hit's fading tail -- the retrigger declick -- so compare past it.)
    n = 6 * block
    fade = int(0.002 * SR) + 8
    assert np.allclose(
        sig[12 * block + fade : 12 * block + n], high[fade:n], atol=1e-6
    )


def test_hat_tone_moves_the_stack():
    """Spectral claim, several renders. The HP at 7 kHz is fixed, so what
    `tone` changes is how DENSE the square stack is above it: a low base
    packs more partials into the band (noisier -- higher spectral
    flatness, the trashy hat), a high base leaves it sparse and pitched
    (the thin one). Centroid is the wrong observable here -- it barely
    moves, the band is the band. And the default is unchanged."""
    default = _one_hit("hat_drum", {}, seconds=0.2, jack="closed_trigger")
    explicit = _one_hit("hat_drum", {"tone": 400.0}, seconds=0.2, jack="closed_trigger")
    assert np.array_equal(default, explicit)

    def flatness(x):
        spec = np.abs(np.fft.rfft(x)) ** 2
        f = np.fft.rfftfreq(len(x), 1 / SR)
        band = spec[f > 7000.0] + 1e-20
        return float(np.exp(np.mean(np.log(band))) / np.mean(band))

    flats = [
        flatness(_one_hit("hat_drum", {"tone": tone}, seconds=0.2,
                          jack="closed_trigger"))
        for tone in (200.0, 400.0, 800.0, 1600.0)
    ]
    assert flats == sorted(flats, reverse=True), flats
    assert flats[0] > 1.5 * flats[-1], flats


def test_hat_tone_is_clamped_to_its_range():
    from pysynthrack.modules.drums import HAT_TONE_MAX, HAT_TONE_MIN
    lo = _one_hit("hat_drum", {"tone": 1.0}, seconds=0.1, jack="closed_trigger")
    lo_ok = _one_hit("hat_drum", {"tone": HAT_TONE_MIN}, seconds=0.1, jack="closed_trigger")
    assert np.array_equal(lo, lo_ok)
    hi = _one_hit("hat_drum", {"tone": 1e6}, seconds=0.1, jack="closed_trigger")
    hi_ok = _one_hit("hat_drum", {"tone": HAT_TONE_MAX}, seconds=0.1, jack="closed_trigger")
    assert np.array_equal(hi, hi_ok)


def test_vel_and_pitch_keep_block_size_independence():
    trig = np.zeros(2048, dtype=np.float32)
    trig[300] = 1.0
    trig[1400] = 1.0
    vel = np.linspace(0.2, 1.0, 2048).astype(np.float32)
    cv = np.linspace(-1.0, 1.0, 2048).astype(np.float32)
    big = _driver_cv("kick_drum", {"drive": 0.5}, cvs=("vel", "pitch_cv"), block=2048)
    small = _driver_cv("kick_drum", {"drive": 0.5}, cvs=("vel", "pitch_cv"), block=256)
    out_big = big(trigger=trig, vel=vel, pitch_cv=cv)
    outs = [
        small(trigger=trig[i:i + 256], vel=vel[i:i + 256], pitch_cv=cv[i:i + 256])
        for i in range(0, 2048, 256)
    ]
    assert np.array_equal(out_big, np.concatenate(outs))


# ----- the node's widgets -----------------------------------------------------

pytest.importorskip("dearpygui.dearpygui")


def test_hat_tone_gets_a_bounded_widget(monkeypatch):
    """`tone` must not fall through to another TYPE's `tone` branch (the
    octaver's 200..2000 LP, the distortion's 200..20000) or to a
    free-range drag: it is the hat's own 200..1600 Hz."""
    from unittest import mock

    import pysynthrack.ui.app as app_mod
    from pysynthrack.modules.drums import HAT_TONE_MAX, HAT_TONE_MIN

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=SR, block_size=512)
    app.patch = Patch()
    module = app.patch.add_module("hat_drum")
    app._create_node_for_module(module)
    drags = [
        c.kwargs for c in app_mod.dpg.add_drag_float.call_args_list
        if "tone" in str(c.kwargs.get("label"))
    ]
    assert len(drags) == 1, drags
    assert drags[0]["min_value"] == HAT_TONE_MIN
    assert drags[0]["max_value"] == HAT_TONE_MAX
