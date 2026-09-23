"""Oscillator — block-size exactness (2026-09-24).

The oscillator's phase used to be a per-block float accumulator: the
constant-frequency path did ``phase += frames * inc`` and the CV paths
did ``(start + cumsum(inc)) % 1`` with the start wrapped at every block
end. Each takes a different rounding path under a different block
partition, so the same patch rendered a different sample at block 64
than at 512 — within ~0.1 s under a held CV, ~0.4-2.4 s on the constant
path. Two engines replace it, both keyed to ABSOLUTE sample counts:

  * constant frequency: ``(origin + inc * k) % 1`` over the integer
    sample count ``k`` since the origin, re-anchored only when the
    increment changes (the organ's 2026-09-22 fix, and now the same
    numbers — the organ's lone 8' pin is exact over 10 s);
  * per-sample CV (mono and every voice): a sequential running sum
    ``cumsum([carry, inc...])`` carried UNWRAPPED across blocks and
    wrapped only at absolute multiples of ``_OSC_EPOCH`` samples.

A second mechanism fell out of measuring it: the wavetable shapes picked
their mipmap band from the BLOCK's max increment, so a block straddling
a note change played the lower note on the higher note's table — a
sequence moved by up to ~0.18 between block sizes. The band is now
picked per sample (still the max across voices: the conservative pick).

Exposure is part of the assertion: every pin runs 6 s at 44.1 kHz, which
also crosses four epoch wraps.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch

SR = 44100
SECONDS = 6.0
N = int(SR * SECONDS)
BLOCKS = (64, 128, 512, 1000)
WAVES = (
    "sine", "saw", "square", "triangle",
    "saw_blep", "square_blep", "triangle_blep",
    "saw_wt", "square_wt", "triangle_wt",
)


def _seq(n, V=None):
    """A 4-note sequence, a step every 0.37 s (not block aligned)."""
    notes = np.array([0.0, 7 / 12, 3 / 12, 10 / 12], dtype=np.float32)
    cv = notes[(np.arange(n) // int(0.37 * SR)) % 4]
    if V is None:
        return cv
    return np.stack([cv + np.float32(v * 4 / 12) for v in range(V)])


def _held(n, V=None):
    c = np.float32(0.25 + 7 / 12)
    if V is None:
        return np.full(n, c, dtype=np.float32)
    return np.stack([np.full(n, c + np.float32(v / 12), dtype=np.float32)
                     for v in range(V)])


def _sine_cv(n, rate, depth, V=None, spread=1 / 12):
    cv = (depth * np.sin(2 * np.pi * rate * np.arange(n) / SR)).astype(np.float32)
    if V is None:
        return cv
    return np.stack([cv + np.float32(v * spread) for v in range(V)])


def _render(waveform, block, freq_cv=None, pw_cv=None, freq=311.127, n=N):
    """Drive the oscillator in ``block``-sized chunks, slicing full-length
    CV arrays per block (the organ test's direct-renderer idiom)."""
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"freq": freq, "amp": 0.5, "waveform": waveform}
    )
    fsrc = patch.add_module("cv_keyboard")
    psrc = patch.add_module("cv_keyboard")
    if freq_cv is not None:
        patch.connect(fsrc.id, "pitch_cv", osc.id, "freq_cv")
    if pw_cv is not None:
        patch.connect(psrc.id, "pitch_cv", osc.id, "pw_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    m = patch.get(osc.id)
    outs = []
    for pos in range(0, n, block):
        f = min(block, n - pos)
        bufs = {}
        if freq_cv is not None:
            bufs[(fsrc.id, "pitch_cv")] = freq_cv[..., pos:pos + f]
        if pw_cv is not None:
            bufs[(psrc.id, "pitch_cv")] = pw_cv[..., pos:pos + f]
        outs.append(b._render_oscillator(m, f, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _assert_block_exact(label, **kw):
    ref = _render(block=BLOCKS[0], **kw)
    for block in BLOCKS[1:]:
        out = _render(block=block, **kw)
        diff = np.flatnonzero((out != ref).reshape(-1, out.shape[-1]).any(axis=0))
        assert diff.size == 0, (
            f"{label}: block {block} parts from block {BLOCKS[0]} at "
            f"{diff[0] / SR:.3f} s ({diff.size} samples)"
        )


# ----- the pins ------------------------------------------------------------


@pytest.mark.parametrize("waveform", WAVES)
def test_held_note_is_block_size_exact(waveform):
    """A held note on every waveform — the constant-frequency path (no
    CV), a held mono CV, and a held 4-voice chord — renders the identical
    sample at 64 / 128 / 512 / 1000 for 6 s."""
    _assert_block_exact(f"{waveform} knob", waveform=waveform)
    _assert_block_exact(f"{waveform} held cv", waveform=waveform,
                        freq_cv=_held(N))
    _assert_block_exact(f"{waveform} held 4v", waveform=waveform,
                        freq_cv=_held(N, 4))


@pytest.mark.parametrize("waveform", WAVES)
def test_sequence_is_block_size_exact(waveform):
    """A 4-note sequence (steps that land mid-block), mono and at 4
    voices — including the wavetable shapes, whose band used to be
    picked per block."""
    _assert_block_exact(f"{waveform} seq", waveform=waveform,
                        freq_cv=_seq(N))
    _assert_block_exact(f"{waveform} seq 4v", waveform=waveform,
                        freq_cv=_seq(N, 4))


@pytest.mark.parametrize("waveform", ("sine", "saw_blep", "saw_wt"))
def test_vibrato_is_block_size_exact(waveform):
    """Per-sample FM is exact too: the running sum does exactly the
    additions a sample-at-a-time loop would. The wt case swings across
    a band edge (±0.6 oct around 311 Hz crosses 320 Hz)."""
    _assert_block_exact(f"{waveform} vib", waveform=waveform,
                        freq_cv=_sine_cv(N, 5.5, 0.6))
    _assert_block_exact(f"{waveform} vib 4v", waveform=waveform,
                        freq_cv=_sine_cv(N, 5.5, 0.6, V=4))


def test_pwm_is_block_size_exact():
    """pulse_width + pw_cv on square_blep: mono, a sequence under PWM,
    per-voice widths, and one mono pw_cv broadcast across 4 voices."""
    pw = _sine_cv(N, 0.7, 0.8)
    pw4 = np.stack([pw * np.float32(1 - 0.1 * v) for v in range(4)])
    _assert_block_exact("pwm", waveform="square_blep", pw_cv=pw)
    _assert_block_exact("pwm seq", waveform="square_blep", pw_cv=pw,
                        freq_cv=_seq(N))
    _assert_block_exact("pwm 4v", waveform="square_blep", pw_cv=pw4,
                        freq_cv=_seq(N, 4))
    _assert_block_exact("pwm broadcast", waveform="square_blep", pw_cv=pw4)


# ----- the mechanisms --------------------------------------------------------


def test_carried_phase_is_a_sample_at_a_time_sum_with_absolute_wraps():
    """``_osc_carried_phase`` over ANY partition equals a pure-Python
    one-sample-at-a-time running sum that wraps at absolute multiples of
    the epoch — bit for bit, voices and all."""
    rng = np.random.default_rng(7)
    inc = rng.uniform(0.001, 0.4, size=(3, 500))
    epoch = 37

    ref = np.empty_like(inc)
    for v in range(3):
        x = 0.25
        for k in range(inc.shape[1]):
            x = x + inc[v, k]
            ref[v, k] = x % 1.0
            if (k + 1) % epoch == 0:
                x = x - np.floor(x)

    for cuts in ([500], [1] * 500, [64] * 7 + [52], [37, 1, 36, 100, 326],
                 [13, 200, 287]):
        carry = np.full(3, 0.25)
        n0 = 0
        parts = []
        for c in cuts:
            ph, carry = NumpyBackend._osc_carried_phase(
                carry, n0, inc[:, n0:n0 + c], epoch
            )
            parts.append(ph)
            n0 += c
        assert np.array_equal(np.concatenate(parts, axis=1), ref), cuts
        # The last wrap fell after sample 480 (13 epochs); the carry is
        # the wrapped value plus the 19 increments since.
        assert np.all(carry < 1.0 + inc[:, 481:].sum(axis=1)), cuts


def test_constant_frequency_lands_on_the_true_edge():
    """110 Hz at 44.1 kHz completes exactly 11 cycles every 4410
    samples. ``inc * k`` lands those samples on phase 0.0 — the naive
    saw's post-edge value — where the old per-block accumulator landed
    one ulp short of the wrap, on the pre-edge side (a full-step flip,
    the only >1e-4 move this fix makes to a shipped example: the naive
    saws/squares of meter_levels, fat_saw, mixer_crossfade_verb and
    noise_gate_chop)."""
    out = _render("saw", 512, freq=110.0, n=6 * SR)
    edges = np.arange(0, 6 * SR, 4410)
    assert np.all(out[edges] == np.float32(-0.5))


def test_switching_engines_is_continuous():
    """Patching / unpatching freq_cv switches between the two engines;
    each carries on one step from the last sample the other rendered, so
    a sine at the same pitch is one unbroken sine. (The old pair skipped
    a sample going fast -> CV and repeated one coming back: they
    disagreed on whether the stored phase was the last sample's or the
    next one's.)"""
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"freq": 440.0, "amp": 1.0, "waveform": "sine"}
    )
    src = patch.add_module("cv_keyboard")
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)
    m = patch.get(osc.id)
    zero = {(src.id, "pitch_cv"): np.zeros(256, dtype=np.float32)}
    blocks = [b._render_oscillator(m, 256, {}, patch) for _ in range(3)]
    patch.connect(src.id, "pitch_cv", osc.id, "freq_cv")
    blocks += [b._render_oscillator(m, 256, zero, patch) for _ in range(3)]
    assert patch.disconnect(src.id, "pitch_cv", osc.id, "freq_cv")
    blocks += [b._render_oscillator(m, 256, {}, patch) for _ in range(3)]
    x = np.concatenate(blocks).astype(np.float64)
    ref = np.sin(2 * np.pi * 440.0 * np.arange(x.size) / SR)
    assert np.abs(x - ref).max() < 1e-6


def test_wavetable_band_steady_pitch_matches_the_scalar_pick():
    """Under a steady pitch the per-sample band IS the old per-block
    pick: a constant dt array renders exactly what the scalar dt does."""
    b = NumpyBackend(sample_rate=SR, block_size=512)
    ph = (np.arange(512) * 0.0123) % 1.0
    for base in ("saw", "square", "triangle"):
        for hz in (20.0, 55.0, 319.9, 320.0, 1000.0, 15000.0):
            dt = hz / SR
            a = b._waveshape_wt(base, ph, dt)
            c = b._waveshape_wt(base, ph, np.full(512, dt))
            c4 = b._waveshape_wt(base, np.stack([ph] * 4),
                                 np.full((4, 512), dt))
            assert np.array_equal(a, c), (base, hz)
            assert np.array_equal(np.stack([a] * 4), c4), (base, hz)


def test_wavetable_band_follows_the_note_inside_a_block():
    """A note change mid-block: each half is shaped with its own note's
    band, exactly as if the block had been split at the change."""
    b = NumpyBackend(sample_rate=SR, block_size=512)
    ph = (np.arange(512) * 0.0123) % 1.0
    dt = np.where(np.arange(512) < 300, 150.0 / SR, 900.0 / SR)
    whole = b._waveshape_wt("saw", ph, dt)
    lo = b._waveshape_wt("saw", ph[:300], 150.0 / SR)
    hi = b._waveshape_wt("saw", ph[300:], 900.0 / SR)
    assert np.array_equal(whole, np.concatenate([lo, hi]))
    # ...and the low half is NOT the high note's table (the old pick).
    assert not np.array_equal(lo, b._waveshape_wt("saw", ph[:300], 900.0 / SR))
