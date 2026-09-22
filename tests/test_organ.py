"""Organ — nine-drawbar additive voice.

Pins the contract: a lone 8' drawbar is BIT-EXACT against the mono sine
oscillator (same phase indexing, same sin, same multiply order — the
strong version of a source's neutral); the drawbar law is ~3 dB per
step; partials land at the classic footage ratios; partials at/above
Nyquist are masked to silence, never aliased; the gate ramp is click
free at click=0 and the key click is seeded-deterministic; the
percussion register single-triggers from silence only (legato does NOT
re-fire); voice rows are independent and a (1, F) render equals mono;
everything is bit-exact across block splits.

Block-size exactness (2026-09-22): the partial phases are an ORIGIN plus
an integer sample count, the percussion's decay/phase/cut-off are counted
from the strike, and the per-block pitch read averages in float64 — so
64 / 128 / 512 / 1000 render the identical sample over TEN SECONDS, tone
path and whole organ alike. The pins used to stop at 4096 frames, which
is the only reason the drift (a float32 ulp inside a second, and a
percussion tail that died on a different sample per block size) went
unseen.

The scanner (2026-09-19 love pass): ``vibrato`` off is never even called
(a raising stand-in proves it) so the pre-scanner render is untouched;
V3's pitch deviation is MEASURED via the Hilbert instantaneous frequency
at ~6.87 Hz and ~±41 cents, V1 < V2 < V3; C is exactly the average of
the V render and the dry (to float32 output rounding); a (2, F) row
equals its mono render (one shared scanner phase); the scanner alone is
bit-exact 64 vs 512 with switches mid-stream, and so is the whole organ;
every switch on a shared boundary is click-free; the fade back to dry
drops the state.

The sweep (2026-09-22 love pass): the pickup traces a rounded triangle,
so the pitch deviation is flat-topped — crest factor 1.12 and a third
harmonic at 0.287 where the sine it replaced gave 1.44 and 0.000, A/B'd
against the same renderer with the shape turned back down to a sine,
and the cents held at ±41. The scanner phase is the organ's own
free-running module clock and does NOT restart at off → on: two organs
switched to V3 half a scanner period apart render bit-identically once
their fades finish.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from scipy.signal import hilbert

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.organ import (
    ORGAN_BARS,
    ORGAN_DEFAULT_BARS,
    ORGAN_FOOTAGES,
    ORGAN_RATIOS,
    ORGAN_VIBRATO,
)

SR = 8000
C4 = 261.6255653005986
RAMP = max(1, round(SR * 0.001))  # backend _ORGAN_RAMP_S


def _driver(params=None, block=256, sr=SR):
    """Direct-renderer harness (octaver-test idiom)."""
    patch = Patch()
    org = patch.add_module("organ", params=params or {})
    kb = patch.add_module("cv_keyboard")
    patch.connect(kb.id, "pitch_cv", org.id, "pitch_cv")
    patch.connect(kb.id, "gate", org.id, "gate")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(pitch, gate):
        frames = np.asarray(gate).shape[-1]
        bufs = {}
        if pitch is not None:
            bufs[(kb.id, "pitch_cv")] = np.asarray(pitch, dtype=np.float32)
        bufs[(kb.id, "gate")] = np.asarray(gate, dtype=np.float32)
        return b._render_organ(patch.get(org.id), frames, bufs, patch)

    step.org = org
    step.patch = patch
    step.backend = b
    return step


def _spec_peak(x, f0, sr=SR, tol=15.0):
    n = len(x)
    spec = np.abs(np.fft.rfft(np.asarray(x, dtype=np.float64) * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    mask = (freqs > f0 - tol) & (freqs < f0 + tol)
    return float(spec[mask].max()) if mask.any() else 0.0


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["organ"]
    assert cls is get_module_type("organ")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["pitch_cv", "gate"]
    assert [p.name for p in m.output_ports] == ["out"]
    for i in range(ORGAN_BARS):
        assert m.params[f"bar{i + 1}"] == ORGAN_DEFAULT_BARS[i]
    assert m.params["click"] == 0.3
    assert m.params["perc"] == "off"
    assert m.params["perc_decay"] == "fast"
    assert m.params["perc_level"] == 0.7
    assert m.params["vibrato"] == "off"
    assert m.params["level"] == 0.5
    assert len(ORGAN_RATIOS) == ORGAN_BARS == len(ORGAN_FOOTAGES)
    assert ORGAN_VIBRATO == ("off", "v1", "v2", "v3", "c1", "c2", "c3")


def test_serialization_round_trip():
    cls = all_module_types()["organ"]
    m = cls(3, params={"bar4": 6, "click": 0.9, "perc": "3rd"})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the neutral pin: lone 8' ≡ oscillator sine ---------------------------


def test_lone_8ft_drawbar_bit_exact_against_sine_oscillator():
    """The source's neutral: one full 8' bar, click 0 → after the 1 ms
    onset ramp the output IS the mono sine oscillator, bit for bit.

    Still exact over a block, which is where the relationship is
    defined: from a zero origin the organ's ``inc * k`` over the
    absolute index and the oscillator's ``arange * inc`` are the same
    numbers. They part company only across MANY blocks, and in the
    organ's favour — the oscillator still carries a per-block float
    accumulator (``phase += frames * inc``), which is exactly the thing
    the organ stopped doing on 2026-09-22; see the 10 s pin below. The
    second half of this test measures that gap instead of pretending it
    is not there."""
    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    bars["bar3"] = 8  # 8' — ratio 1.0
    step = _driver({**bars, "click": 0.0, "level": 0.5})

    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"freq": C4, "amp": 0.5, "waveform": "sine"}
    )
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)

    F = 4096
    organ_out = step(None, np.ones(F))  # unpatched pitch → C4
    osc_out = b._render_oscillator(patch.get(osc.id), F)
    assert np.array_equal(organ_out[RAMP:], osc_out[RAMP:])

    # 10 s in 256-frame blocks: the two agree to a float32 ulp or two,
    # the oscillator's accumulator being the one that moved.
    step2 = _driver({**bars, "click": 0.0, "level": 0.5})
    patch2 = Patch()
    osc2 = patch2.add_module(
        "oscillator", params={"freq": C4, "amp": 0.5, "waveform": "sine"}
    )
    b2 = NumpyBackend(sample_rate=SR, block_size=256)
    b2.compile(patch2)
    N = 10 * SR // 256
    org_long = np.concatenate([step2(None, np.ones(256)) for _ in range(N)])
    osc_long = np.concatenate(
        [b2._render_oscillator(patch2.get(osc2.id), 256) for _ in range(N)]
    )
    eps = np.finfo(np.float32).eps
    assert np.abs(org_long[RAMP:] - osc_long[RAMP:]).max() <= 4.0 * eps


# ----- the drawbar law -------------------------------------------------------


def test_three_db_per_step_law():
    """Each drawbar step is exactly 10^(-3/20) in amplitude."""
    F = 4096

    def rms_at(bar_level):
        bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
        bars["bar3"] = bar_level
        step = _driver({**bars, "click": 0.0, "level": 1.0})
        out = step(None, np.ones(F))
        return float(np.sqrt(np.mean(out[RAMP:].astype(np.float64) ** 2)))

    r8, r5, r1 = rms_at(8), rms_at(5), rms_at(1)
    assert np.isclose(r5 / r8, 10.0 ** (-9.0 / 20.0), rtol=1e-4)
    assert np.isclose(r1 / r8, 10.0 ** (-21.0 / 20.0), rtol=1e-4)

    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    step = _driver({**bars, "click": 0.0})
    assert np.abs(step(None, np.ones(F))).max() == 0.0  # level 0 = silent


def test_partials_land_on_the_footage_ratios():
    """888000000 puts energy at 0.5f, 1.5f and f — and nowhere above."""
    step = _driver({"click": 0.0, "level": 1.0})
    F = 8192
    out = step(None, np.ones(F))
    tail = out[RAMP:]
    floor = _spec_peak(tail, 700.0)  # nothing musical lives here
    assert _spec_peak(tail, 0.5 * C4) > 10 * floor
    assert _spec_peak(tail, 1.5 * C4) > 10 * floor
    assert _spec_peak(tail, 1.0 * C4) > 10 * floor
    # The 4' bar is off: whatever sits at 2f is transient leakage,
    # orders of magnitude below the real partials.
    assert _spec_peak(tail, 2.0 * C4) < 1e-4 * _spec_peak(tail, C4)


def test_highest_bar_is_the_eighth_harmonic():
    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    bars["bar9"] = 8
    step = _driver({**bars, "click": 0.0, "level": 1.0})
    F = 8192
    out = step(None, np.ones(F))
    assert _spec_peak(out[RAMP:], 8.0 * C4) > 10 * _spec_peak(out[RAMP:], C4)


def test_constant_rms_normalisation():
    """Different registrations keep roughly the same RMS."""
    F = 8192

    def rms(params):
        step = _driver({**params, "click": 0.0, "level": 1.0})
        out = step(None, np.ones(F))
        return float(np.sqrt(np.mean(out[RAMP:].astype(np.float64) ** 2)))

    full = {f"bar{i + 1}": 8 for i in range(ORGAN_BARS)}
    jazz = {}  # the default 888000000
    assert np.isclose(rms(full), rms(jazz), rtol=0.02)


def test_partials_at_nyquist_are_masked_not_aliased():
    """A pitch that puts the 1' partial past Nyquist mutes it exactly."""
    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    bars["bar9"] = 8  # 8× the pitch
    step = _driver({**bars, "click": 0.0})
    F = 4096
    # cv = 1.0 → f ≈ 523 Hz → 8f ≈ 4186 Hz ≥ SR/2: masked → pure silence.
    out = step(np.full(F, 1.0), np.ones(F))
    assert np.abs(out).max() == 0.0


# ----- gate ramp & key click -------------------------------------------------


def test_click_zero_onset_is_ramped_not_stepped():
    step = _driver({"click": 0.0})
    F = 2048
    gate = np.zeros(F)
    gate[500:] = 1.0
    out = step(None, gate).astype(np.float64)
    assert np.abs(np.diff(out)).max() < 0.2  # no discontinuity
    assert np.abs(out[:500]).max() == 0.0  # silent before the key


def test_click_is_seeded_and_scales():
    F = 2048
    gate = np.zeros(F)
    gate[500:] = 1.0
    base = _driver({"click": 0.0})(None, gate).astype(np.float64)

    def click_energy(click):
        # The click-0 render subtracts the identical tone path, so the
        # difference IS the click transient.
        out = _driver({"click": click})(None, gate).astype(np.float64)
        return float(np.sum((out - base) ** 2))

    quiet, loud = click_energy(0.2), click_energy(1.0)
    assert loud > 10 * quiet  # amplitude ∝ click → energy ∝ click²

    a = _driver({"click": 0.7})(None, gate)
    b = _driver({"click": 0.7})(None, gate)
    assert np.array_equal(a, b)  # fresh renders identical (seeded)


def test_release_falls_to_exact_silence():
    step = _driver({"click": 0.0})
    F = 4096
    gate = np.zeros(F)
    gate[100:2000] = 1.0
    out = step(None, gate)
    assert np.abs(out[2000 + RAMP :]).max() == 0.0


# ----- percussion ------------------------------------------------------------


def _perc_diff(gates, perc="2nd"):
    """Render a (V, F) gate pattern with the register on and off; the
    difference isolates the percussion strike exactly (the tone paths
    are deterministic and identical)."""
    F = gates.shape[1]
    pitch = np.zeros_like(gates, dtype=np.float32)
    on = _driver({"click": 0.0, "perc": perc, "perc_level": 1.0})
    off = _driver({"click": 0.0, "perc": "off"})
    a = on(pitch, gates).astype(np.float64)
    b = off(pitch, gates).astype(np.float64)
    return (a - b).sum(axis=0)  # perc lives on row 0


def test_percussion_fires_from_silence_only():
    """THE test: a legato addition while a note is held does not
    re-fire the register; a fresh press after silence does."""
    F = 16000  # 2 s at SR 8000 — lets the fast strike die out
    gates = np.zeros((2, F), dtype=np.float32)
    gates[0, 100:4000] = 1.0     # first press: fires
    gates[1, 1500:4000] = 1.0    # legato addition: must NOT re-fire
    gates[1, 12000:] = 1.0       # press from silence: fires again
    d = _perc_diff(gates)

    def env(at, width=300):
        return float(np.abs(d[at : at + width]).max())

    strike = env(100)
    assert strike > 0.1  # the first press struck
    # At the legato press the strike is only the FIRST one's decayed
    # tail — no jump back up.
    assert env(1500) < strike * 0.5
    # Long silence later the tail is dead; the fresh press strikes anew.
    assert env(11000, 900) < 1e-3
    assert env(12000) > 0.1


def test_percussion_harmonic_choice():
    F = 8192
    gates = np.zeros((1, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    d2 = _perc_diff(gates, "2nd")[:4000]
    d3 = _perc_diff(gates, "3rd")[:4000]
    assert _spec_peak(d2, 2.0 * C4) > 5 * _spec_peak(d2, 3.0 * C4)
    assert _spec_peak(d3, 3.0 * C4) > 5 * _spec_peak(d3, 2.0 * C4)


def test_percussion_decay_speed():
    F = 16000
    gates = np.zeros((1, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    fast = _driver({"click": 0.0, "perc": "2nd", "perc_decay": "fast",
                    "perc_level": 1.0})
    slow = _driver({"click": 0.0, "perc": "2nd", "perc_decay": "slow",
                    "perc_level": 1.0})
    base = _driver({"click": 0.0})
    pitch = np.zeros_like(gates, dtype=np.float32)
    b = base(pitch, gates).astype(np.float64).sum(axis=0)
    df = fast(pitch, gates).astype(np.float64).sum(axis=0) - b
    ds = slow(pitch, gates).astype(np.float64).sum(axis=0) - b
    at = 100 + int(0.45 * SR)  # 0.45 s after the strike (> fast t60)
    assert np.abs(ds[at : at + 400]).max() > 5 * np.abs(df[at : at + 400]).max()


# ----- voices ----------------------------------------------------------------


def test_single_voice_row_equals_mono():
    F = 4096
    gate = np.zeros(F, dtype=np.float32)
    gate[100:3000] = 1.0
    pitch = np.full(F, 0.25, dtype=np.float32)
    mono = _driver({"click": 0.5})(pitch, gate)
    voiced = _driver({"click": 0.5})(pitch[None, :], gate[None, :])
    assert voiced.shape == (1, F)
    assert np.array_equal(mono, voiced[0])


def test_per_voice_independence():
    F = 4096
    gates = np.zeros((2, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    gates[1, 100:] = 1.0
    pitches = np.zeros((2, F), dtype=np.float32)
    pitches[1] = 0.5
    duo = _driver({"click": 0.0})(pitches, gates)
    solo0 = _driver({"click": 0.0})(pitches[0], gates[0])
    solo1 = _driver({"click": 0.0})(pitches[1], gates[1])
    assert np.array_equal(duo[0], solo0)
    assert np.array_equal(duo[1], solo1)


def test_unpatched_gate_is_silence():
    patch = Patch()
    org = patch.add_module("organ")
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)
    out = b._render_organ(patch.get(org.id), 512, {}, patch)
    assert out.shape == (512,)
    assert np.abs(out).max() == 0.0


# ----- block-size independence ----------------------------------------------


def test_block_size_independent_bit_exact():
    """Ramps, clicks, the percussion register and the partial phases all
    carry across any block split — bit-exact at 64 / 128 / 512 / 1000
    over TEN SECONDS.

    The old pin spanned 4096 frames, which is exactly why it never saw
    the 2026-09-22 finding: the per-block float phase accumulator drifts
    a float32 ulp somewhere between 0.2 s and 1.8 s, and the
    percussion's 1e-6 cut-off used to be tested once per POUR, so it
    landed on a different sample under a different block size (0.568 s
    in at SR 8000). The pitch here is 0.25 + 7/12 on purpose: its
    float32 block-MEAN is not block-size stable, so this also pins the
    float64 accumulator on the per-block pitch read."""
    F = 10 * SR
    gate = np.zeros(F, dtype=np.float32)
    gate[100:F // 3] = 1.0
    gate[F // 3 + 500:] = 1.0
    pitch = np.full(F, 0.25 + 7.0 / 12.0, dtype=np.float32)

    def chunked(block):
        step = _driver({"click": 0.5, "perc": "2nd"}, block=block)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            e = min(F, s + block)
            out[s:e] = step(pitch[s:e], gate[s:e])
        return out

    ref = chunked(512)
    for block in (64, 128, 1000):
        assert np.array_equal(chunked(block), ref), block


def test_partial_phase_is_an_origin_plus_an_integer_count():
    """THE 2026-09-22 fix, on the tone path alone: every block's ramp is
    ``origin + inc * k`` over the ABSOLUTE integer sample index, so a
    held note renders the identical sample at any block size for as long
    as you run it — pinned over 10 s at 44.1 kHz, where the old
    accumulator had already diverged by 0.22 s (block 1000) / 1.78 s
    (block 64) and reached a full float32 ulp by 30 s. Under a held
    pitch the origin is never re-anchored at all, so nothing
    accumulates."""
    sr = 44100
    F = 10 * sr
    gate = np.ones(F, dtype=np.float32)
    pitch = np.full(F, 0.25, dtype=np.float32)

    def chunked(block):
        step = _driver(_lone8({"vibrato": "off"}), block=block, sr=sr)
        return _chunked(step, gate, block, pitch)

    ref = chunked(512)
    for block in (64, 128, 1000):
        assert np.array_equal(chunked(block), ref), block

    # The state IS an origin plus a count, and the origin holds still.
    step = _driver(_lone8({}), block=256, sr=sr)
    for _ in range(3):
        step(pitch[:256], gate[:256])
    st = step.backend._state[step.org.id]
    assert int(st["phase_n"][0]) == 768
    assert float(np.abs(st["phases"]).max()) == 0.0  # never re-anchored


def test_percussion_decay_is_counted_from_the_strike():
    """The strike's envelope and phase are ``amp0 * g**k`` and
    ``(inc * k) % 1`` over the absolute count since it fired, and its
    cut-off is applied per SAMPLE — so the tail dies on the same sample
    whatever the block size, and the state is dropped once there is
    nothing left but zeros."""
    F = 6 * SR
    gates = np.zeros((1, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    pitch = np.zeros_like(gates)

    def chunked(block):
        step = _driver({"click": 0.0, "perc": "2nd", "perc_level": 1.0},
                       block=block)
        return _chunked(step, gates, block, pitch), step

    ref, st_ref = chunked(512)
    for block in (64, 128, 1000):
        assert np.array_equal(chunked(block)[0], ref), block
    st = st_ref.backend._state[st_ref.org.id]
    assert st["perc_amp"] == 0.0  # the strike is long dead
    # ...and it was COUNTED to the analytic cut-off, not carried: with
    # amp0 = perc_level * level = 0.5 and the fast t60, amp0·g^k crosses
    # 1e-6 at k ≈ 4559 samples.
    g = 10.0 ** (-3.0 / (SR * 0.3))
    dead_at = np.log(1e-6 / 0.5) / np.log(g)
    assert 4550.0 < dead_at < 4570.0, dead_at
    assert int(st["perc_n"]) >= dead_at


# ----- the scanner vibrato / chorus ------------------------------------------

SCAN_HZ = 412.0 / 60.0
V3_CENTS = 1200.0 * np.log2(1.0 + 2.0 * np.pi * SCAN_HZ * 0.55e-3)  # ~40.6


def _lone8(extra):
    bars = {f"bar{i + 1}": 0 for i in range(ORGAN_BARS)}
    bars["bar3"] = 8
    return {**bars, "click": 0.0, "level": 0.5, **extra}


def _chunked(step, gate, block, pitch=None, schedule=()):
    """Render in blocks; ``schedule`` = ((sample, {param: value}), ...)
    applied when a block STARTS at that sample (a shared boundary)."""
    F = gate.shape[-1]
    outs = []
    sched = dict(schedule)
    for s in range(0, F, block):
        if s in sched:
            for k, v in sched[s].items():
                step.org.params[k] = v
        p = None if pitch is None else pitch[..., s : s + block]
        outs.append(step(p, gate[..., s : s + block]))
    return np.concatenate(outs, axis=-1)


def _wobble(y, f0, sr, skip=None, trim=4000, k=200):
    """Peak pitch deviation (cents) and its rate (Hz) from the analytic
    signal — the vinyl-wobble idiom, with the Hilbert's edge transients
    trimmed off before the max is taken (they read as +200 cents)."""
    seg = y[sr if skip is None else skip :].astype(np.float64)
    phase = np.unwrap(np.angle(hilbert(seg)))
    inst = (np.diff(phase) * sr / (2.0 * np.pi))[trim:-trim]
    f_t = np.convolve(inst, np.ones(k) / k, mode="valid")
    cents = 1200.0 * np.log2(f_t / f0)
    spec = np.abs(np.fft.rfft((f_t - f_t.mean()) * np.hanning(len(f_t))))
    freqs = np.fft.rfftfreq(len(f_t), 1.0 / sr)
    return float(np.abs(cents).max()), float(freqs[np.argmax(spec)])


def _cents_curve(y, f0, sr, trim=4000, k=200):
    """``_wobble``'s deviation curve itself, mean removed — so the SHAPE
    of the wobble can be measured, not just its depth."""
    seg = y[sr:].astype(np.float64)
    phase = np.unwrap(np.angle(hilbert(seg)))
    inst = (np.diff(phase) * sr / (2.0 * np.pi))[trim:-trim]
    f_t = np.convolve(inst, np.ones(k) / k, mode="valid")
    cents = 1200.0 * np.log2(f_t / f0)
    return cents - cents.mean()


def _harmonic_ratio(curve, mult, sr, rate=SCAN_HZ):
    """|H(mult·rate)| / |H(rate)| of a deviation curve — 0 for a sine,
    1/3 for a square at the third."""
    n = len(curve)
    spec = np.abs(np.fft.rfft(curve * np.hanning(n)))

    def at(m):
        k = int(round(m * rate * n / sr))
        return float(spec[k - 2 : k + 3].max())

    return at(mult) / at(1)


def test_vibrato_off_never_touches_the_scanner():
    """Count the doors: at ``off`` the scanner is not called at all (a
    raising stand-in would fail the render) and no ring is allocated —
    so the pre-scanner organ, clicks and percussion included, is bit
    for bit what it was. The lone-8' pin above is the tone-path half."""
    step = _driver({"click": 0.5, "perc": "2nd"})
    step.backend._organ_scanner = mock.Mock(
        side_effect=AssertionError("scanner called at off")
    )
    F = 4096
    gate = np.zeros(F, dtype=np.float32)
    gate[100:3000] = 1.0
    out = step(None, gate)
    assert np.abs(out).max() > 0.0
    assert not any(k.startswith("scan") for k in step.backend._state[step.org.id])


def test_v3_pitch_deviation_measured_at_the_scanner_rate():
    """THE measurement: a lone 8' at C4 through V3 wobbles ~±41 cents at
    ~6.87 Hz (a sinusoidal delay of half-swing A deviates the pitch
    ratio by 2·pi·f·A at its peak; 0.55 ms → 40.6 cents predicted)."""
    sr = 44100
    step = _driver(_lone8({"vibrato": "v3"}), block=4096, sr=sr)
    out = _chunked(step, np.ones(sr * 4, dtype=np.float32), 4096)
    depth, rate = _wobble(out, C4, sr)
    assert 0.8 * V3_CENTS < depth < 1.2 * V3_CENTS, depth
    assert abs(rate - SCAN_HZ) < 0.3, rate


def test_the_sweep_is_a_rounded_triangle_not_a_sine():
    """(2026-09-22) The pickup traces a ROUNDED TRIANGLE across the taps,
    so the pitch deviation is flat-topped rather than sinusoidal.
    Measured on the module's own output at V3, A/B'd against the same
    renderer with the shape parameter turned down to zero — which IS the
    sine that shipped, so nothing but the shape differs between the two
    renders:

        shape             peak cents   crest (peak/RMS)   3rd harmonic
        sine (was)           41.7          1.44               0.000
        rounded triangle     41.8          1.12               0.287
        (square, ideal)        —           1.00               0.333

    The cents are the contract and they did not move: the swing is
    scaled by arcsin(R)/R to make up for the triangle's shallower peak
    slope (keeping the 0.35/0.70/1.10 ms swing instead would have
    dropped V3 to ±29 cents)."""
    sr = 44100
    shipped = NumpyBackend._ORGAN_SCAN_ROUND

    def curve(round_):
        with mock.patch.object(NumpyBackend, "_ORGAN_SCAN_ROUND", round_):
            step = _driver(_lone8({"vibrato": "v3"}), block=4096, sr=sr)
            out = _chunked(step, np.ones(sr * 4, dtype=np.float32), 4096)
        return _cents_curve(out, C4, sr)

    def shape(c):
        peak = float(np.abs(c).max())
        rms = float(np.sqrt(np.mean(c ** 2)))
        return peak, peak / rms, _harmonic_ratio(c, 3, sr)

    p_t, crest_t, h3_t = shape(curve(shipped))
    p_s, crest_s, h3_s = shape(curve(1e-7))  # R → 0 is exactly the sine

    assert 1.40 < crest_s < 1.46, crest_s   # a sinusoidal deviation
    assert h3_s < 0.01, h3_s
    assert 1.09 < crest_t < 1.15, crest_t   # flat-topped, not square
    assert 0.25 < h3_t < 0.32, h3_t
    assert abs(p_t - p_s) < 0.02 * p_s, (p_t, p_s)  # the cents held
    assert 0.8 * V3_CENTS < p_t < 1.2 * V3_CENTS, p_t


def test_vibrato_depths_increase_v1_v2_v3():
    sr = 44100
    depths = []
    for vib in ("v1", "v2", "v3"):
        step = _driver(_lone8({"vibrato": vib}), block=4096, sr=sr)
        out = _chunked(step, np.ones(sr * 4, dtype=np.float32), 4096)
        depths.append(_wobble(out, C4, sr)[0])
    v1, v2, v3 = depths
    assert v1 < v2 < v3
    # The swing table is 0.35 / 0.70 / 1.10 ms: V2 is twice V1.
    assert np.isclose(v2, 2.0 * v1, rtol=0.1), depths


@pytest.mark.parametrize("depth", ["1", "3"])
def test_chorus_is_the_average_of_vibrato_and_dry(depth):
    """The C algebra: ``c = 0.5 * (dry + scanned)`` — pinned against the
    V render and the off render of the same passage (clicks and the
    percussion strike included, they ride through the ring). Exact in
    float64; the only slack is the separate float32 rounding of the two
    reference renders, hence the eps32 tolerance."""
    F = 8192
    gate = np.zeros(F, dtype=np.float32)
    gate[100:] = 1.0
    prm = {"click": 0.4, "perc": "2nd"}
    v = _driver(_lone8({**prm, "vibrato": f"v{depth}"}))(None, gate)
    c = _driver(_lone8({**prm, "vibrato": f"c{depth}"}))(None, gate)
    off = _driver(_lone8({**prm, "vibrato": "off"}))(None, gate)
    want = 0.5 * (v.astype(np.float64) + off.astype(np.float64))
    tol = 2.0 * np.finfo(np.float32).eps * float(np.abs(c).max())
    assert np.abs(c.astype(np.float64) - want).max() <= tol
    # And the comb is real: the chorus is not merely quieter vibrato.
    assert not np.allclose(c, 0.5 * v, atol=1e-3)


def test_two_voice_rows_equal_their_mono_renders_under_the_scanner():
    """One scanner phase for every voice, one ring per voice: a (2, F)
    render's row 0 IS the mono render of that voice (clicks and the
    row-0 percussion strike included), and row 1 is its mono render too
    (click 0 there: the key click is seeded by voice SLOT, and the
    percussion is monophonic hardware — both pre-scanner contracts)."""
    F = 8192
    gates = np.zeros((2, F), dtype=np.float32)
    gates[0, 100:] = 1.0
    gates[1, 2000:6000] = 1.0
    pitches = np.zeros((2, F), dtype=np.float32)
    pitches[1] = 7.0 / 12.0
    prm = {"vibrato": "c2", "click": 0.5, "perc": "2nd"}
    duo = _chunked(_driver(prm), gates, 256, pitches)
    solo0 = _chunked(_driver(prm), gates[0], 256, pitches[0])
    assert duo.shape == (2, F)
    assert np.array_equal(duo[0], solo0)
    prm = {"vibrato": "c2", "click": 0.0}
    duo = _chunked(_driver(prm), gates, 256, pitches)
    solo1 = _chunked(_driver(prm), gates[1], 256, pitches[1])
    assert np.array_equal(duo[1], solo1)


def test_scanner_alone_is_block_size_independent_bit_exact():
    """The scanner is a pure function of (state, block): drive it
    directly with a (2, F) signal through v1 → c3 → off at 64 vs 512,
    switches on shared boundaries, ~34 scanner periods — bit-exact,
    the state dropped after the fade, and the tail IS the input."""
    b = NumpyBackend(sample_rate=SR, block_size=256)
    rng = np.random.default_rng(5)
    F = 40000
    x = 0.4 * np.sin(2.0 * np.pi * C4 * np.arange(F) / SR)
    x = x + 0.05 * rng.standard_normal(F)
    x = np.stack([x, np.roll(x, 777)])

    def run(block):
        st = {}
        outs = []
        for s in range(0, F, block):
            vib = "v1" if s < 8192 else ("c3" if s < 24576 else "off")
            n = min(block, F - s)
            if vib != "off" or "scan_buf" in st:
                # ``s`` IS the caller's free-running module clock.
                outs.append(
                    b._organ_scanner(
                        st, x[:, s : s + n].copy(), vib, n, float(SR), s
                    )
                )
            else:
                outs.append(x[:, s : s + n])
        return np.concatenate(outs, axis=1), st

    a, st_a = run(64)
    c, st_c = run(512)
    assert np.array_equal(a, c)
    assert not st_a and not st_c
    assert np.array_equal(a[:, 24576 + 400 :], x[:, 24576 + 400 :])
    assert not np.array_equal(a[:, :8192], x[:, :8192])  # it did something


def test_organ_with_scanner_block_size_independent_bit_exact():
    """The whole organ, sweep running, v1 → c3 switched on a shared
    boundary: bit-exact at 64 / 128 / 512 / 1000 over TEN SECONDS. The
    old pin stopped at 4096 frames — the span the organ's own float
    phase accumulator happened to be exact over; since 2026-09-22 the
    partials, the gate ramp, the click tails, the percussion and the
    scanner are ALL integer-counted, so there is no such span any
    more."""
    F = 10 * SR
    gate = np.zeros(F, dtype=np.float32)
    gate[100:F // 3] = 1.0
    gate[F // 3 + 500:] = 1.0
    pitch = np.full(F, 0.25, dtype=np.float32)
    # 64000 is lcm(64, 128, 512, 1000): the one switch point below 10 s
    # that starts a block at every size tested.
    switch = 64000

    def run(block):
        step = _driver({"click": 0.5, "perc": "2nd", "vibrato": "v1"}, block=block)
        return _chunked(step, gate, block, pitch,
                        schedule=((switch, {"vibrato": "c3"}),))

    ref = run(512)
    for block in (64, 128, 1000):
        assert np.array_equal(run(block), ref), block


@pytest.mark.parametrize(
    "start,then",
    [("v1", "v3"), ("off", "v3"), ("v3", "off"), ("v3", "c3")],
    ids=["v1-v3", "off-v3", "v3-off", "v3-c3"],
)
def test_switches_on_a_shared_boundary_are_click_free(start, then):
    """Gains and depth crossfade over an integer-counted ~40 ms ramp, so
    the largest sample step around the switch is no bigger than the
    steady V3 signal's own (the depth ramp adds at most ~2 % to the
    sweep's slope at its worst phase — measured 0.98–1.0×; a hard
    splice would jump by up to half the amplitude)."""
    sr = 44100
    B = 512
    S = 2 * sr // B * B
    F = 5 * S // 2
    gate = np.ones(F, dtype=np.float32)
    step = _driver(_lone8({"vibrato": start}), block=B, sr=sr)
    y = _chunked(step, gate, B, schedule=((S, {"vibrato": then}),)).astype(np.float64)
    ref = _driver(_lone8({"vibrato": "v3"}), block=B, sr=sr)
    steady = np.abs(np.diff(_chunked(ref, gate, B).astype(np.float64)[sr:])).max()
    around = np.abs(np.diff(y))[S - 10 : S + int(0.06 * sr)].max()
    assert around <= 1.03 * steady, (around, steady)


def test_scanner_phase_survives_off_to_on():
    """(2026-09-22) The motor does not stop when the knob is down. The
    scanner's phase is the organ's own free-running module clock, so an
    organ switched to V3 at 1.00 s and one switched at 1.37 s — moments
    HALF a scanner period apart, which is the worst case for a sweep
    that restarts — render BIT-IDENTICALLY once both fades have
    finished and both rings have filled. The always-on organ is the
    third witness: the sweep really is the same one, not two matching
    restarts."""
    sr = 44100
    B = 512
    F = 5 * sr
    gate = np.ones(F, dtype=np.float32)
    s1, s2 = 86 * B, 118 * B  # 0.998 s and 1.370 s, both block starts
    period = sr / SCAN_HZ  # ~6422 samples
    assert 0.3 < ((s2 - s1) % period) / period < 0.7  # genuinely antiphase

    def run(schedule, start="off"):
        step = _driver(_lone8({"vibrato": start}), block=B, sr=sr)
        return _chunked(step, gate, B, schedule=schedule)

    late = run(((s1, {"vibrato": "v3"}),))
    later = run(((s2, {"vibrato": "v3"}),))
    always = run((), start="v3")

    at = 3 * sr  # well past both fades and the ring span
    assert np.array_equal(late[at:], later[at:])
    assert np.array_equal(late[at:], always[at:])
    # ...and it is a real sweep being matched, not silence.
    assert np.abs(late[at:]).max() > 0.1


def test_fade_back_to_dry_drops_the_line_and_leaves_the_dry_render():
    """off → v3 → off: after the fade the scanner keys are gone and the
    tail is bit-identical to a never-scanned render on the same block
    partition (``1.0 * dry + 0.0 * wet`` is the dry, so the moment the
    state is dropped is invisible)."""
    F = 8192
    gate = np.ones(F, dtype=np.float32)
    step = _driver(_lone8({"vibrato": "off", "click": 0.3}))
    y = _chunked(step, gate, 256,
                 schedule=((1024, {"vibrato": "v3"}), (4096, {"vibrato": "off"})))
    st = step.backend._state[step.org.id]
    assert not any(k.startswith("scan") for k in st)
    ref = _chunked(_driver(_lone8({"vibrato": "off", "click": 0.3})), gate, 256)
    fade = round(SR * 0.04)
    assert np.array_equal(y[4096 + fade + 256 :], ref[4096 + fade + 256 :])
    assert not np.array_equal(y[2048:4096], ref[2048:4096])  # it was on


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("organ")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int", "add_slider_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            label = call.kwargs.get("label")
            key = str(label) if label is not None else str(call.kwargs.get("user_data"))
            out[key] = (k, call.kwargs.get("format"), call.kwargs.get("items"))
    return out, module.id


def test_every_param_gets_a_bounded_widget(monkeypatch):
    w, mid = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("organ").DEFAULT_PARAMS:
        if name.startswith("bar"):
            # The drawbar bank draws the bars: vertical int sliders keyed
            # by their user_data (they carry no label).
            hits = [lb for lb in labels if lb == str((mid, name))]
            assert hits, (name, labels)
            assert w[hits[0]][0] == "add_slider_int", (name, w[hits[0]])
            continue
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["vibrato"][0] == "add_combo"
    assert tuple(w["vibrato"][2]) == ORGAN_VIBRATO
    assert w["perc"][0] == "add_combo"


# ----- example ------------------------------------------------------------------


def test_the_scanner_example_plays_the_chorus_within_headroom():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "organ_scanner.json"
    patch = load_patch(path)
    organ = next(m for m in patch if m.TYPE == "organ")
    assert organ.params["vibrato"] == "c3"
    assert len(list(patch)) <= 12
    np.random.seed(1)
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    peak = 0.0
    cap = []
    orig = b._render_organ

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.append(np.asarray(r).copy())
        return r

    b._render_organ = spy
    for _ in range(int(44100 * 6 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8, peak
    # Four organ voices came out of the chord, and the scanner is alive.
    assert cap[0].ndim == 2 and cap[0].shape[0] == 4
    st = b._state[organ.id]
    assert "scan_buf" in st and st["scan_buf"].shape[0] == 4
