"""Vinyl — surface noise + 33⅓ rpm warp.

Pins the contract: all-zero knobs return the input buffer itself
(bit-exact passthrough); every stream is seeded-deterministic and
exactly block-size independent (absolute-sample noise windows); the
dust-tick count scales with ``crackle``; rumble is LF-dominant; wobble
produces a measurable pitch deviation cycling at ~0.55 Hz (33⅓ rpm)
with depth in the tens of cents at full; unpatched input still emits
the noise bed.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import hilbert

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch

SR = 8000


def _chunked(params, x, block=512):
    patch = Patch()
    v = patch.add_module("vinyl", params=params)
    o = patch.add_module("oscillator")
    patch.connect(o.id, "out", v.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    out = np.empty(len(x), dtype=np.float32)
    for i in range(0, len(x), block):
        seg = np.asarray(x[i : i + block], dtype=np.float32)
        out[i : i + block] = b._render_vinyl(
            patch.get(v.id), len(seg), {(o.id, "out"): seg}, patch
        )
    return out


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["vinyl"]
    assert cls is get_module_type("vinyl")
    assert cls.CATEGORY == "Effects"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["in"]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["crackle"] == 0.3
    assert m.params["rumble"] == 0.2
    assert m.params["wobble"] == 0.2
    assert m.params["seed"] == 1


def test_serialization_round_trip():
    cls = all_module_types()["vinyl"]
    m = cls(3, params={"crackle": 0.9, "wobble": 0.0, "seed": 7})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- passthrough -----------------------------------------------------------


def test_all_zero_knobs_is_bit_exact_passthrough():
    x = (0.5 * np.sin(2 * np.pi * 440 * np.arange(4096) / SR)).astype(
        np.float32
    )
    zeroed = {"crackle": 0.0, "rumble": 0.0, "wobble": 0.0}
    out = _chunked(zeroed, x)
    assert np.array_equal(out, x)


def test_unpatched_input_still_emits_the_noise_bed():
    patch = Patch()
    v = patch.add_module("vinyl", params={"crackle": 1.0, "rumble": 1.0})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    energy = 0.0
    for _ in range(20):
        out = b._render_vinyl(patch.get(v.id), 512, {}, patch)
        energy += float(np.sum(out.astype(np.float64) ** 2))
    assert energy > 0.0


# ----- determinism / block independence -------------------------------------


def test_seeded_determinism_and_reroll():
    silence = np.zeros(SR * 2, dtype=np.float32)
    params = {"crackle": 0.7, "rumble": 0.3, "wobble": 0.0, "seed": 5}
    a = _chunked(params, silence)
    b = _chunked(params, silence)
    c = _chunked({**params, "seed": 6}, silence)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_block_size_independent_bit_exact_all_vices():
    x = (0.3 * np.sin(2 * np.pi * 220 * np.arange(16384) / SR)).astype(
        np.float32
    )
    params = {"crackle": 0.8, "rumble": 0.6, "wobble": 0.5}
    assert np.array_equal(
        _chunked(params, x, 64), _chunked(params, x, 1024)
    )


# ----- the vices -------------------------------------------------------------


def _tick_count(crackle, seconds=4):
    out = _chunked(
        {"crackle": crackle, "rumble": 0.0, "wobble": 0.0},
        np.zeros(SR * seconds, dtype=np.float32),
    )
    hot = (np.abs(out) > 1e-4).astype(int)
    return int(np.sum(np.diff(hot) > 0))


def test_crackle_count_scales_with_the_knob():
    few, many = _tick_count(0.2), _tick_count(1.0)
    assert many > 2 * few
    assert few > 0


def test_crackle_zero_is_clean():
    assert _tick_count(0.0) == 0


def test_rumble_is_lf_dominant():
    out = _chunked(
        {"crackle": 0.0, "rumble": 1.0, "wobble": 0.0},
        np.zeros(SR * 4, dtype=np.float32),
    )
    spec = np.abs(np.fft.rfft(out.astype(np.float64)))
    freqs = np.fft.rfftfreq(len(out), 1.0 / SR)
    lf = spec[(freqs > 10) & (freqs < 80)].mean()
    hf = spec[(freqs > 500) & (freqs < 2000)].mean()
    assert lf > 50 * hf


def test_wobble_pitch_deviation_cycles_at_33rpm():
    """The spec's pin: a pure tone through full wobble shows an
    instantaneous-pitch deviation of tens of cents cycling at ~0.55 Hz
    (measured via the analytic signal — the resampler pitch-test
    spirit)."""
    tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(SR * 10) / SR)).astype(
        np.float32
    )
    out = _chunked({"crackle": 0.0, "rumble": 0.0, "wobble": 1.0}, tone)
    seg = out[SR * 2 :].astype(np.float64)  # skip the onset transient
    phase = np.unwrap(np.angle(hilbert(seg)))
    inst_f = np.diff(phase) * SR / (2.0 * np.pi)
    k = np.ones(400) / 400.0
    f_t = np.convolve(inst_f, k, mode="valid")
    dev_cents = 1200.0 * np.log2(f_t / 440.0)
    depth = float(np.abs(dev_cents).max())
    assert 10.0 < depth < 40.0  # tens of cents at full wobble
    spec = np.abs(
        np.fft.rfft((f_t - f_t.mean()) * np.hanning(len(f_t)))
    )
    freqs = np.fft.rfftfreq(len(f_t), 1.0 / SR)
    peak = float(freqs[np.argmax(spec)])
    assert 0.45 < peak < 0.65  # the 33⅓ rpm cycle


def test_wobble_zero_leaves_pitch_untouched():
    tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(SR * 4) / SR)).astype(
        np.float32
    )
    out = _chunked({"crackle": 0.0, "rumble": 0.0, "wobble": 0.0}, tone)
    assert np.array_equal(out, tone)
