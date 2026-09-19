"""Tests for the ``freeze`` module — the spectral freeze (module #100).

The renderer is driven directly with hand-made buffers (a tiny fake
patch whose cables point at buffer keys), so a test can put an exact
sine, an exact gate and an exact CV on the jacks without an oscillator
or a clock in the way. Pitch and steadiness are MEASURED (Hilbert
instantaneous frequency, windowed RMS), never eyeballed.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from scipy.signal import hilbert

import pysynthrack.modules  # noqa: F401  (registers every type)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.freeze import FREEZE_SIZES

SR = 44100


class _Cable:
    def __init__(self, src_port, dst_port):
        self.src_module_id = 99
        self.src_port = src_port
        self.dst_port = dst_port


class _FakePatch:
    def __init__(self, cables):
        self._cables = cables

    def cables_into(self, module_id):
        return self._cables


def _render(params, sig, gate=None, cv=None, block=512, sr=SR, backend=None, ports=None):
    """Render ``sig`` through one freeze module; returns ``out`` (or, with
    ``ports``, a dict of the named jacks)."""
    p = Patch()
    fz = p.add_module("freeze")
    for k, v in params.items():
        fz.params[k] = v
    b = backend or NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(p)
    cables = [_Cable("in", "in")]
    if gate is not None:
        cables.append(_Cable("gate", "freeze"))
    if cv is not None:
        cables.append(_Cable("cv", "pitch_cv"))
    fp = _FakePatch(cables)
    want = tuple(ports) if ports else ("out",)
    out = {k: [] for k in want}
    pos = 0
    while pos < len(sig):
        f = min(block, len(sig) - pos)
        bufs = {(99, "in"): np.asarray(sig[pos:pos + f], dtype=np.float32)}
        if gate is not None:
            bufs[(99, "gate")] = np.asarray(gate[pos:pos + f], dtype=np.float32)
        if cv is not None:
            bufs[(99, "cv")] = np.asarray(cv[pos:pos + f], dtype=np.float32)
        r = b._render_freeze(fz, f, bufs, fp)
        for k in want:
            out[k].append(np.asarray(r[k]).copy())
        pos += f
    got = {k: np.concatenate(v) for k, v in out.items()}
    return (got if ports else got["out"]), b, fz


def _sine(freq, seconds, amp=0.5, sr=SR):
    t = np.arange(int(seconds * sr))
    return (amp * np.sin(2 * np.pi * freq * t / sr)).astype(np.float32)


def _gate_from(start_s, seconds, sr=SR):
    g = np.zeros(int(seconds * sr), dtype=np.float32)
    g[int(start_s * sr):] = 1.0
    return g


def _inst_freq(seg, sr=SR):
    an = hilbert(np.asarray(seg, dtype=np.float64))
    f = np.diff(np.unwrap(np.angle(an))) * sr / (2 * np.pi)
    return f[200:-200], np.abs(an)[200:-200]


def _rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


# ----- registration ----------------------------------------------------------


def test_registered_with_ports_and_params():
    m = get_module_type("freeze")
    assert m.CATEGORY == "Effects"
    assert [p.name for p in m.INPUT_PORTS] == ["in", "freeze", "pitch_cv"]
    assert [(p.name, p.signal_kind) for p in m.OUTPUT_PORTS] == [
        ("out", "audio"), ("out_l", "audio"), ("out_r", "audio")]
    kinds = {p.name: p.signal_kind for p in m.INPUT_PORTS}
    assert kinds == {"in": "audio", "freeze": "gate", "pitch_cv": "cv"}
    assert m.DEFAULT_PARAMS == {
        "size": 4096, "freeze": False, "smear": 0.0, "pitch": 0.0,
        "pitch_cv_depth": 1.0, "level": 0.7, "dry": 1.0, "fade": 60.0, "seed": 1,
        "width": 0.0, "decay": 0.0,
    }
    assert m.DEFAULT_PARAMS["size"] in FREEZE_SIZES
    assert FREEZE_SIZES == (1024, 2048, 4096, 8192, 16384)


def test_unpatched_input_is_silence_with_no_state():
    p = Patch()
    fz = p.add_module("freeze")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    out = b._render_freeze(fz, 512, {}, _FakePatch([]))
    assert set(out) == {"out", "out_l", "out_r"}
    assert out["out"].shape == (512,) and not out["out"].any()
    assert out["out_l"] is out["out"] and out["out_r"] is out["out"]
    assert fz.id not in b._state


# ----- the neutral ------------------------------------------------------------


def test_never_rising_gate_is_the_input_itself():
    sig = _sine(441.3, 1.0)
    y0, _, _ = _render({}, sig)
    y1, _, _ = _render({}, sig, gate=np.zeros_like(sig))
    assert np.array_equal(y0, sig)
    assert np.array_equal(y1, sig)


def test_passthrough_returns_the_source_buffer_at_dry_one():
    p = Patch()
    fz = p.add_module("freeze")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    sig = _sine(300.0, 512 / SR)
    out = b._render_freeze(fz, 512, {(99, "in"): sig}, _FakePatch([_Cable("in", "in")]))
    assert out["out"] is sig
    assert out["out_l"] is sig and out["out_r"] is sig    # the pair is the mono


def test_dry_scales_the_passthrough():
    sig = _sine(441.3, 0.5)
    y, _, _ = _render({"dry": 0.5}, sig)
    assert np.allclose(y, sig * 0.5, atol=1e-7)


def test_a_voice_shaped_input_is_the_house_sum():
    sig = _sine(441.3, 0.25)
    two = np.stack([sig, sig * 0.5])
    p = Patch()
    fz = p.add_module("freeze")
    b = NumpyBackend(sample_rate=SR, block_size=len(sig))
    b.compile(p)
    out = b._render_freeze(fz, len(sig), {(99, "in"): two}, _FakePatch([_Cable("in", "in")]))
    assert np.allclose(out["out"], sig * 1.5, atol=1e-6)


# ----- the hold ----------------------------------------------------------------


def test_a_sine_freezes_to_the_same_sine_forever():
    """Non-bin-centred 441.3 Hz; the input goes SILENT at 1.5 s and the
    hold carries on unchanged to 5 s: same frequency, unity amplitude,
    steady RMS."""
    sig = _sine(441.3, 5.5)
    sig[int(1.5 * SR):] = 0.0
    y, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=_gate_from(1.0, 5.5))
    f, amp = _inst_freq(y[2 * SR:5 * SR])
    assert abs(np.median(f) - 441.3) < 0.5
    assert np.percentile(f, 5) > 439.0 and np.percentile(f, 95) < 443.6   # no beating
    assert abs(np.median(amp) - 0.5) < 0.02
    r2, r5 = _rms(y[2 * SR:2 * SR + 8192]), _rms(y[5 * SR:5 * SR + 8192])
    assert abs(r2 - 0.5 / np.sqrt(2)) < 0.01
    assert abs(r5 - r2) < 0.005


def test_hold_starts_at_the_edge_and_the_dry_keeps_passing():
    sig = _sine(441.3, 3.0)
    y, _, _ = _render({"dry": 1.0, "level": 1.0, "fade": 1000.0}, sig, gate=_gate_from(1.0, 3.0))
    # before the edge: the input itself
    assert np.array_equal(y[:SR], sig[:SR])
    # the frozen layer rises linearly over the fade: the two coherent
    # copies of the same sine add, so the level climbs from 1x to 2x
    assert 1.4 < _rms(y[int(1.4 * SR):int(1.6 * SR)]) / _rms(sig[:SR]) < 1.6
    assert 1.9 < _rms(y[int(2.5 * SR):]) / _rms(sig[:SR]) < 2.1


@pytest.mark.parametrize("size", FREEZE_SIZES)
def test_every_window_size_holds_a_sine(size):
    sig = _sine(441.3, 3.0)
    sig[int(1.5 * SR):] = 0.0
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "size": size}, sig, gate=_gate_from(1.0, 3.0))
    f, amp = _inst_freq(y[2 * SR:3 * SR])
    assert abs(np.median(f) - 441.3) < 1.0
    assert abs(np.median(amp) - 0.5) < 0.03
    assert np.all(np.isfinite(y))


def test_an_unknown_size_falls_back_to_4096():
    sig = _sine(441.3, 2.0)
    y0, _, _ = _render({"dry": 0.0, "level": 1.0, "size": 4096}, sig, gate=_gate_from(1.0, 2.0))
    y1, b, fz = _render({"dry": 0.0, "level": 1.0, "size": 3000}, sig, gate=_gate_from(1.0, 2.0))
    assert np.array_equal(y0, y1)
    assert b._state[fz.id]["n"] == 4096


def test_a_triad_holds_every_partial_at_4096():
    """The resolution rule: C-E-G (68 Hz apart) at the default window
    keeps every partial within 10%."""
    t = np.arange(4 * SR)
    freqs = (261.6, 329.6, 392.0)
    sig = sum(0.2 * np.sin(2 * np.pi * f * t / SR + ph) for f, ph in zip(freqs, (0.3, 1.1, 2.0)))
    sig = sig.astype(np.float32)
    y, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=_gate_from(1.0, 4.0))

    def levels(seg):
        w = np.hanning(len(seg))
        S = np.abs(np.fft.rfft(seg * w)) / (len(seg) / 4)
        fr = np.fft.rfftfreq(len(seg), 1 / SR)
        return [S[np.argmin(np.abs(fr - f))] for f in freqs]

    before = levels(sig[:SR].astype(np.float64))
    after = levels(y[2 * SR:3 * SR].astype(np.float64))
    for a, b_ in zip(before, after):
        assert abs(b_ - a) / a < 0.03, (before, after)


def test_the_hold_is_stationary_phase_locked():
    """Without phase locking the triad's contaminated lobe bins dephase
    and the hold eats itself over seconds (measured: two partials down to
    a third by 10 s). Locked, 2 s and 10 s are the same to 1%, and even
    a 2048 window holds the triad within 6%."""
    t = np.arange(12 * SR)
    freqs = (261.6, 329.6, 392.0)
    sig = sum(0.2 * np.sin(2 * np.pi * f * t / SR + ph) for f, ph in zip(freqs, (0.3, 1.1, 2.0)))
    sig = sig.astype(np.float32)
    sig[int(1.5 * SR):] = 0.0

    def levels(seg):
        w = np.hanning(len(seg))
        S = np.abs(np.fft.rfft(seg * w)) / (len(seg) / 4)
        fr = np.fft.rfftfreq(len(seg), 1 / SR)
        return np.array([S[np.argmin(np.abs(fr - f))] for f in freqs])

    y, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=_gate_from(1.0, 12.0))
    early = levels(y[2 * SR:3 * SR].astype(np.float64))
    late = levels(y[10 * SR:11 * SR].astype(np.float64))
    assert np.all(np.abs(late - early) / early < 0.01)
    y2, _, _ = _render({"dry": 0.0, "level": 1.0, "size": 2048}, sig[:5 * SR], gate=_gate_from(1.0, 5.0))
    ref = levels(sig[:SR].astype(np.float64))
    got = levels(y2[3 * SR:4 * SR].astype(np.float64))
    assert np.all(np.abs(got - ref) / ref < 0.06), (ref, got)
    assert np.allclose(NumpyBackend._freeze_lock(np.array([1.0, 3.0, 1.0, 0.5, 2.0, 0.1]),
                                                 np.arange(6.0)),
                       [1.0, 1.0, 1.0, 4.0, 4.0, 4.0])


# ----- smear -----------------------------------------------------------------------


def test_smear_keeps_the_spectrum_and_loses_the_coherence():
    sig = _sine(441.3, 3.0)
    sig[int(1.5 * SR):] = 0.0
    g = _gate_from(1.0, 3.0)
    y0, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 0.0}, sig, gate=g)
    y1, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 1.0}, sig, gate=g)
    a, b_ = y0[2 * SR:3 * SR], y1[2 * SR:3 * SR]
    assert abs(int(np.abs(np.fft.rfft(a)).argmax()) - int(np.abs(np.fft.rfft(b_)).argmax())) <= 2
    assert abs(np.corrcoef(a, b_)[0, 1]) < 0.3
    assert 0.2 < _rms(b_) / _rms(a) < 0.8      # the wash is quieter: level is the makeup
    assert np.all(np.isfinite(y1))


def test_smear_is_reproducible_per_seed():
    sig = _sine(441.3, 2.0)
    g = _gate_from(0.5, 2.0)
    y1, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 0.7, "seed": 3}, sig, gate=g)
    y2, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 0.7, "seed": 3}, sig, gate=g)
    y3, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 0.7, "seed": 4}, sig, gate=g)
    assert np.array_equal(y1, y2)
    assert not np.array_equal(y1, y3)


# ----- pitch ---------------------------------------------------------------------


@pytest.mark.parametrize("semis,want", [(12.0, 882.6), (-12.0, 220.65), (7.0, 441.3 * 2 ** (7 / 12))])
def test_pitch_transposes_the_hold_exactly_at_unity(semis, want):
    sig = _sine(441.3, 3.5)
    sig[int(1.5 * SR):] = 0.0
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "pitch": semis}, sig, gate=_gate_from(1.0, 3.5))
    f, amp = _inst_freq(y[2 * SR:int(3.4 * SR)])
    assert abs(np.median(f) - want) / want < 0.002
    assert abs(np.median(amp) - 0.5) < 0.02


def test_pitch_cv_plus_one_at_depth_one_is_pitch_plus_twelve():
    sig = _sine(441.3, 2.5)
    g = _gate_from(0.5, 2.5)
    y0, _, _ = _render({"dry": 0.0, "level": 1.0, "pitch": 12.0}, sig, gate=g)
    y1, _, _ = _render({"dry": 0.0, "level": 1.0, "pitch": 0.0}, sig, gate=g, cv=np.ones_like(sig))
    assert np.array_equal(y0, y1)
    y2, _, _ = _render({"dry": 0.0, "level": 1.0, "pitch": 0.0, "pitch_cv_depth": 0.5},
                       sig, gate=g, cv=np.ones_like(sig))
    y3, _, _ = _render({"dry": 0.0, "level": 1.0, "pitch": 6.0}, sig, gate=g)
    assert np.array_equal(y2, y3)


def test_an_absurd_pitch_cv_is_clipped_and_finite():
    sig = _sine(441.3, 2.0)
    g = _gate_from(0.5, 2.0)
    y, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=g, cv=np.full_like(sig, 1e6))
    assert np.all(np.isfinite(y))
    y_lim, _, _ = _render({"dry": 0.0, "level": 1.0, "pitch": 48.0}, sig, gate=g)
    assert np.array_equal(y, y_lim)      # +4 octaves is the ceiling
    y_nan, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=g, cv=np.full_like(sig, np.nan))
    y_zero, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=g, cv=np.zeros_like(sig))
    assert np.array_equal(y_nan, y_zero)  # a non-finite mean reads as 0


def test_pitch_up_does_not_alias_above_nyquist():
    """+24 semitones on a partial near 10 kHz: the source content above
    Nyquist/4 is dropped at synthesis, so nothing folds back."""
    sig = (0.3 * np.sin(2 * np.pi * 10000.0 * np.arange(3 * SR) / SR)
           + 0.3 * np.sin(2 * np.pi * 300.0 * np.arange(3 * SR) / SR)).astype(np.float32)
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "pitch": 24.0}, sig, gate=_gate_from(1.0, 3.0))
    seg = y[2 * SR:3 * SR].astype(np.float64)
    S = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    fr = np.fft.rfftfreq(len(seg), 1 / SR)
    assert S[np.argmin(np.abs(fr - 1200.0))] > 50 * S[(fr > 3000) & (fr < 4100)].max()


# ----- fade, release, re-freeze -------------------------------------------------


def test_fade_is_a_linear_rise_of_fade_ms():
    sig = _sine(441.3, 3.0)
    sig[int(1.5 * SR):] = 0.0
    fade_s = 0.4
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "fade": fade_s * 1000.0}, sig, gate=_gate_from(1.0, 3.0))
    # the layer is silent before the edge, half level at half the fade,
    # full after it (the envelope is read inside the active part only --
    # a Hilbert transform rings into a silent region)
    assert not y[:SR].any()
    env = np.abs(hilbert(y[SR:int(2.0 * SR)].astype(np.float64)))
    half = env[int(fade_s * SR / 2)]
    assert abs(half - 0.25) < 0.03
    assert abs(env[int(fade_s * SR) + 2000] - 0.5) < 0.02
    assert env[int(0.1 * SR)] < env[int(0.2 * SR)] < env[int(0.3 * SR)]


def test_release_fades_out_and_drops_the_layer():
    sig = _sine(441.3, 4.0)
    g = np.zeros_like(sig)
    g[SR:2 * SR] = 1.0
    y, b, fz = _render({"dry": 0.0, "level": 1.0, "fade": 100.0}, sig, gate=g)
    assert _rms(y[int(1.5 * SR):int(1.9 * SR)]) > 0.3
    assert not y[int(2.2 * SR):].any()
    assert b._state[fz.id]["layers"] == []
    # and with nothing held the render is the passthrough again
    y2, _, _ = _render({"fade": 100.0}, sig, gate=g)
    assert np.array_equal(y2[int(2.2 * SR):], sig[int(2.2 * SR):])


def test_a_refreeze_crossfades_old_into_new_without_a_click():
    """440 Hz held, then the input becomes 660 Hz and a one-sample dip
    in the gate re-triggers: the new hold is 660, the old fades, and the
    largest sample step across the swap is no larger than a 660 Hz sine's
    own (the house click tripwire)."""
    t = np.arange(5 * SR)
    sig = np.where(t < 2 * SR, 0.5 * np.sin(2 * np.pi * 440 * t / SR),
                   0.5 * np.sin(2 * np.pi * 660 * t / SR)).astype(np.float32)
    g = np.zeros_like(sig)
    g[SR:] = 1.0
    dip = int(2.5 * SR)
    g[dip] = 0.0
    y, b, fz = _render({"dry": 0.0, "level": 1.0}, sig, gate=g)
    f, _ = _inst_freq(y[3 * SR:5 * SR])
    assert abs(np.median(f) - 660.0) < 1.0
    f0, _ = _inst_freq(y[int(1.5 * SR):2 * SR])
    assert abs(np.median(f0) - 440.0) < 1.0
    own = 0.5 * 2 * np.pi * 660.0 / SR
    assert np.abs(np.diff(y[dip - 100:dip + int(0.1 * SR)])).max() <= own * 1.05
    assert len(b._state[fz.id]["layers"]) == 1     # the old one was dropped


def test_at_most_four_layers_live():
    sig = _sine(441.3, 2.0)
    g = np.zeros_like(sig)
    for k in range(8):
        g[SR + k * 400:SR + k * 400 + 300] = 1.0   # eight quick re-triggers
    y, b, fz = _render({"dry": 0.0, "level": 1.0, "fade": 1500.0}, sig, gate=g)
    assert len(b._state[fz.id]["layers"]) <= 4
    assert np.all(np.isfinite(y))


def _render_blocks(fz, b, sig, gate=None, on_block=None, block=512, port=None):
    cables = [_Cable("in", "in")] + ([_Cable("gate", "freeze")] if gate is not None else [])
    fp = _FakePatch(cables)
    out = []
    for k, pos in enumerate(range(0, len(sig), block)):
        if on_block is not None:
            on_block(k, fz)
        f = min(block, len(sig) - pos)
        bufs = {(99, "in"): sig[pos:pos + f]}
        if gate is not None:
            bufs[(99, "gate")] = gate[pos:pos + f]
        r = b._render_freeze(fz, f, bufs, fp)
        out.append(np.asarray(r["out"] if port is None else r[port]).copy())
    return np.concatenate(out)


def test_the_tickbox_is_the_gate():
    """Ticking ``freeze`` on at block 100 == a gate cable rising at that
    block's first sample; a gate high from sample 0 freezes the silence
    before the input (an honest capture of nothing)."""
    sig = _sine(441.3, 2.5)
    sig[int(1.6 * SR):] = 0.0
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update({"dry": 0.0, "level": 1.0})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)

    def tick(k, m):
        if k == 100:
            m.params["freeze"] = True

    y0 = _render_blocks(fz, b, sig, on_block=tick)
    g = np.zeros_like(sig)
    g[100 * 512:] = 1.0
    y1, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=g)
    assert np.array_equal(y0, y1)
    assert _rms(y0[2 * SR:]) > 0.3
    y2, _, _ = _render({"dry": 0.0, "level": 1.0, "freeze": True}, sig)
    assert not y2.any()


# ----- exactness ------------------------------------------------------------------


def test_block_size_independence_with_edges_mid_stream():
    sig = _sine(441.3, 4.0)
    g = np.zeros_like(sig)
    g[SR + 100:3 * SR + 37] = 1.0
    g[int(3.5 * SR) + 5:] = 1.0
    prm = {"dry": 0.5, "level": 0.8, "pitch": 7.0, "smear": 0.5, "fade": 80.0}
    ya, _, _ = _render(prm, sig, gate=g, block=64)
    yb, _, _ = _render(prm, sig, gate=g, block=512)
    assert np.array_equal(ya, yb)
    yc, _, _ = _render(prm, sig, gate=g, block=1000)
    assert np.array_equal(ya, yc)


def test_a_ratio_change_rebases_without_a_jump():
    """The pitch knob moves mid-hold: the read position is continuous
    (no step in the output beyond a sine's own)."""
    sig = _sine(441.3, 3.0)
    sig[int(1.5 * SR):] = 0.0
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update({"dry": 0.0, "level": 1.0, "pitch": 0.0})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)

    def turn(k, m):
        if k == (2 * SR) // 512 - 10:
            m.params["pitch"] = 5.0

    y = _render_blocks(fz, b, sig, gate=_gate_from(1.0, 3.0), on_block=turn)
    own = 0.5 * 2 * np.pi * 441.3 * 2 ** (5 / 12) / SR
    assert np.abs(np.diff(y[int(1.8 * SR):int(2.2 * SR)])).max() <= own * 1.05
    f, _ = _inst_freq(y[int(2.2 * SR):3 * SR])
    assert abs(np.median(f) - 441.3 * 2 ** (5 / 12)) < 1.0


def test_finite_under_absurd_params():
    sig = _sine(441.3, 1.5)
    y, _, _ = _render({"dry": 5.0, "level": 9.0, "smear": 7.0, "pitch": -400.0, "fade": -3.0, "seed": -5,
                       "width": 40.0, "decay": -3.0},
                      sig, gate=_gate_from(0.3, 1.5), ports=_LR)
    assert all(np.all(np.isfinite(y[k])) for k in _LR)
    # a NaN width reads as 0; a 1e-9 s decay underflows to silence and
    # drops the layer at once; an infinite decay is forever
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "width": float("nan"), "decay": 1e-9},
                      sig, gate=_gate_from(0.3, 1.5), ports=_LR)
    assert all(np.all(np.isfinite(y[k])) for k in _LR)
    assert not y["out"][SR:].any()
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 1.0, "decay": float("inf")},
                      sig, gate=_gate_from(0.3, 1.5), ports=_LR)
    assert all(np.all(np.isfinite(y[k])) for k in _LR)
    assert _rms(y["out"][SR:]) > 0.3


# ----- width (the love pass) ----------------------------------------------------

_LR = ("out", "out_l", "out_r")
_TRIAD = (261.6, 329.6, 392.0)


def _triad(seconds):
    t = np.arange(int(seconds * SR))
    sig = sum(0.2 * np.sin(2 * np.pi * f * t / SR + ph) for f, ph in zip(_TRIAD, (0.3, 1.1, 2.0)))
    return sig.astype(np.float32)


def _levels(seg, freqs=_TRIAD):
    seg = np.asarray(seg, dtype=np.float64)
    w = np.hanning(len(seg))
    S = np.abs(np.fft.rfft(seg * w)) / (len(seg) / 4)
    fr = np.fft.rfftfreq(len(seg), 1 / SR)
    return np.array([S[np.argmin(np.abs(fr - f))] for f in freqs])


def _db(x):
    return 20.0 * np.log10(max(float(x), 1e-12))


def test_width_zero_is_the_mono_on_all_three_jacks():
    """The recipe pin: at width 0 the pair IS the mono buffer (the same
    object per block), so the stereo jacks cost nothing and the render
    is the shipped one."""
    sig = _triad(3.0)
    g = _gate_from(1.0, 3.0)
    y, b, fz = _render({"dry": 0.5, "level": 0.8, "smear": 0.3}, sig, gate=g, ports=_LR)
    assert np.array_equal(y["out_l"], y["out"]) and np.array_equal(y["out_r"], y["out"])
    p = Patch()
    fz = p.add_module("freeze")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    fp = _FakePatch([_Cable("in", "in"), _Cable("gate", "freeze")])
    for pos in range(0, 2 * SR, 512):
        r = b._render_freeze(fz, 512, {(99, "in"): sig[pos:pos + 512], (99, "gate"): g[pos:pos + 512]}, fp)
        assert r["out_l"] is r["out"] and r["out_r"] is r["out"]
    assert b._state[fz.id]["layers"][0]["syn_l"] is None     # no channel buffers were ever made


def test_width_one_decorrelates_and_keeps_every_partial():
    """A triad held at width 1: the channels are uncorrelated (the
    quadrature law: corr = cos(pi/2) = 0), each keeps every partial's
    level (0.00 dB measured -- one phase per peak REGION, never per bin),
    each has the mono's RMS, and the mono ``out`` is untouched by
    width, bit-exact. The fold (L + R)/2 is the mono hold at -3.01 dB
    (cos(pi/4)), the number the docs quote."""
    sig = _triad(4.0)
    g = _gate_from(1.0, 4.0)
    mono, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=g)
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 1.0}, sig, gate=g, ports=_LR)
    assert np.array_equal(y["out"], mono)
    seg = slice(2 * SR, 4 * SR)
    L, R, M = (y[k][seg].astype(np.float64) for k in ("out_l", "out_r", "out"))
    assert abs(np.corrcoef(L, R)[0, 1]) < 0.05
    ref = _levels(M)
    for ch in (L, R):
        assert np.all(np.abs(20 * np.log10(_levels(ch) / ref)) < 0.1)
        assert abs(_db(_rms(ch) / _rms(M))) < 0.1
        assert int(np.abs(np.fft.rfft(ch)).argmax()) == int(np.abs(np.fft.rfft(M)).argmax())
    fold = 0.5 * (L + R)
    assert abs(_db(_rms(fold) / _rms(M)) - (-3.01)) < 0.1
    assert np.corrcoef(fold, M)[0, 1] > 0.9999      # the fold IS the mono, quieter


@pytest.mark.parametrize("width", [0.25, 0.5, 0.75])
def test_width_follows_the_cosine_law(width):
    """corr(L, R) = cos(width * pi/2): 0.924 / 0.707 / 0.383 measured to
    three places, levels untouched at every setting."""
    sig = _triad(3.5)
    g = _gate_from(1.0, 3.5)
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "width": width}, sig, gate=g, ports=_LR)
    seg = slice(2 * SR, int(3.5 * SR))
    L, R, M = (y[k][seg].astype(np.float64) for k in ("out_l", "out_r", "out"))
    assert abs(np.corrcoef(L, R)[0, 1] - np.cos(width * np.pi / 2)) < 0.01
    assert abs(_db(_rms(L) / _rms(M))) < 0.1 and abs(_db(_rms(R) / _rms(M))) < 0.1
    assert abs(_db(_rms(0.5 * (L + R)) / _rms(M)) - _db(np.cos(width * np.pi / 4))) < 0.1


def test_width_with_smear_shares_the_jitter():
    """The smear's per-frame jitter is the same for both channels, so
    the width law still holds through a wash: corr ~ 0 at width 1 and
    the two channels are equally loud."""
    sig = _triad(4.0)
    g = _gate_from(1.0, 4.0)
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 1.0, "smear": 0.6}, sig, gate=g, ports=_LR)
    seg = slice(2 * SR, 4 * SR)
    L, R = y["out_l"][seg].astype(np.float64), y["out_r"][seg].astype(np.float64)
    assert abs(np.corrcoef(L, R)[0, 1]) < 0.05
    assert abs(_db(_rms(L) / _rms(R))) < 0.1
    mono, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 0.6}, sig, gate=g)
    assert np.array_equal(y["out"], mono)


def test_width_turned_up_and_back_down_mid_hold_never_steps():
    """Width goes 0 -> 1 at 2.0 s and back to 0 at 3.0 s on a live sine
    hold. The channels start as the mono stream and diverge across the
    overlap (largest sample step <= a sine's own), and once the scatter
    is back to 0 the channel frames are the mono frames again, so the
    tail of ``out_l`` is ``out`` bit-exact."""
    sig = _sine(441.3, 4.5)
    sig[int(1.5 * SR):] = 0.0
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update({"dry": 0.0, "level": 1.0})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    up, down = (2 * SR) // 512, (3 * SR) // 512

    def turn(k, m):
        if k == up:
            m.params["width"] = 1.0
        if k == down:
            m.params["width"] = 0.0

    g = _gate_from(1.0, 4.5)
    yl = _render_blocks(fz, b, sig, gate=g, on_block=turn, port="out_l")
    p2 = Patch()
    fz2 = p2.add_module("freeze")
    fz2.params.update({"dry": 0.0, "level": 1.0})
    b2 = NumpyBackend(sample_rate=SR, block_size=512)
    b2.compile(p2)
    ym = _render_blocks(fz2, b2, sig, gate=g, on_block=turn, port="out")
    own = 0.5 * 2 * np.pi * 441.3 / SR
    assert np.abs(np.diff(yl[int(1.9 * SR):int(3.4 * SR)])).max() <= own * 1.05
    assert not np.array_equal(yl[int(2.3 * SR):int(2.9 * SR)], ym[int(2.3 * SR):int(2.9 * SR)])
    assert np.array_equal(yl[int(3.4 * SR):], ym[int(3.4 * SR):])


# ----- decay (the love pass) ----------------------------------------------------


def test_decay_is_minus_sixty_db_per_decay_seconds():
    """Against the same hold at decay 0 (forever), the decayed hold is
    10^(-3t/decay) at every t -- measured at 0.5 / 1 / 2 s within 0.1
    dB (the spec's '-60 dB at 2 s relative to 0.2 s' is -54 dB, because
    0.2 s in the layer is already 6 dB down; test the law, not the
    hunch)."""
    sig = _sine(441.3, 5.0)
    sig[int(1.5 * SR):] = 0.0
    g = _gate_from(1.0, 5.0)
    y0, _, _ = _render({"dry": 0.0, "level": 1.0}, sig, gate=g)
    y2, _, _ = _render({"dry": 0.0, "level": 1.0, "decay": 2.0}, sig, gate=g)
    for t in (0.5, 1.0, 2.0):
        i = SR + int(t * SR)
        win = slice(i - 1024, i + 1024)
        want = -60.0 * t / 2.0
        assert abs(_db(_rms(y2[win]) / _rms(y0[win])) - want) < 0.1, t
    i02, i2 = SR + int(0.2 * SR), SR + 2 * SR
    assert abs(_db(_rms(y2[i2:i2 + 4096]) / _rms(y2[i02:i02 + 4096])) - (-54.0)) < 1.0


def test_decayed_layer_is_dropped_at_minus_ninety_even_with_the_gate_high():
    """decay 2 s: -90 dB is 3 s after birth. At 2.9 s the layer is still
    there; by 3.1 s the state has no layers and the render is the
    passthrough again although the gate never fell; a fresh edge after
    the drop starts a new layer at full level."""
    sig = _sine(441.3, 6.0)
    sig[int(1.5 * SR):] = 0.0
    g = _gate_from(1.0, 6.0)
    prm = {"dry": 0.0, "level": 1.0, "decay": 2.0}
    _, b, fz = _render(prm, sig[:int(3.9 * SR)], gate=g[:int(3.9 * SR)])
    assert len(b._state[fz.id]["layers"]) == 1
    y, b, fz = _render(prm, sig[:int(4.1 * SR)], gate=g[:int(4.1 * SR)])
    assert b._state[fz.id]["layers"] == []
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update(prm)
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    fp = _FakePatch([_Cable("in", "in"), _Cable("gate", "freeze")])
    pos = 0
    while pos < int(4.5 * SR):
        r = b._render_freeze(fz, 512, {(99, "in"): sig[pos:pos + 512], (99, "gate"): g[pos:pos + 512]}, fp)
        pos += 512
    assert not r["out"].any() and r["out_l"] is r["out"]     # dropped: silence at dry 0
    # a re-trigger after the drop: the new layer starts at full level
    g2 = g.copy()
    g2[int(4.5 * SR):int(4.5 * SR) + 100] = 0.0
    sig2 = sig.copy()
    again = _sine(441.3, 1.4)
    sig2[4 * SR:4 * SR + len(again)] = again
    y, b, fz = _render(prm, sig2, gate=g2)
    r_first = _rms(y[SR + int(0.3 * SR):SR + int(0.3 * SR) + 4096])
    r_again = _rms(y[int(4.5 * SR) + 100 + int(0.3 * SR):int(4.5 * SR) + 100 + int(0.3 * SR) + 4096])
    assert abs(_db(r_again / r_first)) < 0.5
    assert len(b._state[fz.id]["layers"]) == 1


def test_decay_zero_is_forever_and_bit_exact_with_the_default():
    sig = _sine(441.3, 3.0)
    g = _gate_from(1.0, 3.0)
    y0, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 0.4}, sig, gate=g, ports=_LR)
    y1, _, _ = _render({"dry": 0.0, "level": 1.0, "smear": 0.4, "decay": 0.0}, sig, gate=g, ports=_LR)
    for k in _LR:
        assert np.array_equal(y0[k], y1[k])


def test_decay_knob_turn_mid_hold_continues_from_the_level_it_has():
    """decay 0 -> 1 s at 2.0 s on a sine held from 1.0 s: no step at the
    turn (the fall is rebased to now), and from there the hold is
    10^(-3 (t - 2)) -- -30 dB at 2.5 s, -60 dB at 3.0 s -- not the
    -90 dB it would be if the count ran from birth."""
    sig = _sine(441.3, 4.0)
    sig[int(1.5 * SR):] = 0.0
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update({"dry": 0.0, "level": 1.0})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    turn_at = (2 * SR) // 512

    def turn(k, m):
        if k == turn_at:
            m.params["decay"] = 1.0

    y = _render_blocks(fz, b, sig, gate=_gate_from(1.0, 4.0), on_block=turn)
    own = 0.5 * 2 * np.pi * 441.3 / SR
    assert np.abs(np.diff(y[int(1.9 * SR):int(2.1 * SR)])).max() <= own * 1.05
    t0 = turn_at * 512
    ref = _rms(y[int(1.8 * SR):int(1.8 * SR) + 2048])
    for dt, want in ((0.5, -30.0), (1.0, -60.0)):
        i = t0 + int(dt * SR)
        assert abs(_db(_rms(y[i - 1024:i + 1024]) / ref) - want) < 0.5, dt


def test_refreeze_starts_the_new_layer_at_full_level_under_decay():
    """decay 1.5 s: the first hold is 20 dB down by the time a dip
    re-triggers at 2.0 s; the new layer starts at full, so 0.3 s after
    the second edge the level matches 0.3 s after the first (within
    0.5 dB) instead of carrying the fall."""
    sig = _sine(441.3, 4.0)
    g = np.zeros_like(sig)
    g[SR:] = 1.0
    g[2 * SR] = 0.0
    y, b, fz = _render({"dry": 0.0, "level": 1.0, "decay": 1.5}, sig, gate=g)
    a = _rms(y[SR + int(0.3 * SR):SR + int(0.3 * SR) + 4096])
    c = _rms(y[2 * SR + int(0.3 * SR):2 * SR + int(0.3 * SR) + 4096])
    assert abs(_db(c / a)) < 0.5
    assert len(b._state[fz.id]["layers"]) == 1


def test_block_size_independence_with_width_and_decay():
    """64 vs 512 vs 1000 with width 0.6 and decay 3 s live, edges
    mid-stream, smear and pitch on: all three jacks bit-exact (the
    decay is a per-sample factor of the integer count since birth, the
    scatter a constant per layer)."""
    sig = _sine(441.3, 4.0)
    g = np.zeros_like(sig)
    g[SR + 100:3 * SR + 37] = 1.0
    g[int(3.5 * SR) + 5:] = 1.0
    prm = {"dry": 0.5, "level": 0.8, "pitch": 7.0, "smear": 0.5, "fade": 80.0, "width": 0.6, "decay": 3.0}
    ya, _, _ = _render(prm, sig, gate=g, block=64, ports=_LR)
    yb, _, _ = _render(prm, sig, gate=g, block=512, ports=_LR)
    yc, _, _ = _render(prm, sig, gate=g, block=1000, ports=_LR)
    for k in _LR:
        assert np.array_equal(ya[k], yb[k]) and np.array_equal(ya[k], yc[k]), k
    assert not np.array_equal(ya["out_l"], ya["out_r"])
    # and the decay was live: the wet (the output less the dry) is down
    # ~30 dB 1.5 s into the first hold
    wet = ya["out"].astype(np.float64) - 0.5 * sig
    assert _rms(wet[int(2.5 * SR):int(2.9 * SR)]) < 0.1 * _rms(wet[int(1.2 * SR):int(1.6 * SR)])


# ----- UI ---------------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("freeze")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("format"), call.kwargs)
    return out, app, module


def test_every_param_gets_a_bounded_widget(monkeypatch):
    w, _, _ = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("freeze").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["size (fft)"][0] == "add_combo"
    assert w["size (fft)"][2]["items"] == [str(n) for n in FREEZE_SIZES]
    assert w["freeze (or gate)"][0] == "add_checkbox"
    assert w["pitch"][1].endswith(" st")
    assert "oct/unit" in w["pitch_cv_depth"][1]
    assert w["fade"][1].endswith(" ms")
    # the love pass: width a 0..1 slider, decay a 0..60 s drag
    wd = w["width (stereo, out_l/r)"]
    assert wd[0] == "add_slider_float" and (wd[2]["min_value"], wd[2]["max_value"]) == (0.0, 1.0)
    dc = w["decay (0 = forever)"]
    assert dc[0] == "add_drag_float" and (dc[2]["min_value"], dc[2]["max_value"]) == (0.0, 60.0)
    assert dc[1] == "%.1f s"


def test_size_combo_stores_an_int(monkeypatch):
    w, app, module = _widgets(monkeypatch)
    cb = w["size (fft)"][2]["callback"]
    cb(None, "8192", (module.id, "size"))
    assert module.params["size"] == 8192 and isinstance(module.params["size"], int)
    cb(None, "3000", (module.id, "size"))
    assert module.params["size"] == 2048          # snapped onto the set


# ----- example --------------------------------------------------------------------------


def test_the_chord_pad_example_holds_between_chords():
    """Each chord plays for a quarter of its bar; the freeze holds the rest.
    Measured: the output never drops out inside a bar, and the frozen part
    of a bar keeps the chord's level."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "freeze_chord_pad.json"
    patch = load_patch(path)
    fz = next(m for m in patch if m.TYPE == "freeze")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    cap = []
    orig = b._render_freeze

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.append(np.asarray(r["out"]).copy())
        return r

    b._render_freeze = spy
    peak = 0.0
    for _ in range(int(SR * 13 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.9
    sig = np.concatenate(cap)
    # bar length from the patch's clock: bpm 60, division 0.25 -> a pulse every 4 s
    clock = next(m for m in patch if m.TYPE == "clock")
    bar = int(round(SR * 60.0 / float(clock.params["bpm"]) / float(clock.params["division"])))
    # second bar: chord sounds in [bar, bar + 0.25 bar), the hold in the rest
    chord = _rms(sig[bar + bar // 10: bar + bar // 4 - bar // 20])
    held = _rms(sig[bar + bar // 2: bar + bar - bar // 20])
    assert chord > 0.05 and held > 0.05
    assert 0.3 < held / chord < 3.0
    # no silence anywhere inside bars two and three (the pad bridges them)
    for start in range(bar + bar // 8, 3 * bar - bar // 8, bar // 8):
        assert _rms(sig[start:start + bar // 16]) > 0.01, start / SR


def test_the_wide_wash_example_blooms_wide_and_dies_on_its_own():
    """Every four seconds the edge lands on the run's last note and the
    hold blooms as a wide wash that decays by itself. Measured on the
    freeze's own jacks: the rest half of a cycle (nothing but the wash)
    is stereo (corr(L, R) < 0.6 at width 0.8: cos(0.4 pi) = 0.31 plus
    the shared dry tail), it falls ~10 dB/s (decay 6), and at the end
    the state holds the current layer only."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "freeze_wide_wash.json"
    patch = load_patch(path)
    fz = next(m for m in patch if m.TYPE == "freeze")
    assert fz.params["width"] == 0.8 and fz.params["decay"] == 6.0
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    cap = {"out_l": [], "out_r": []}
    orig = b._render_freeze

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        for k in cap:
            cap[k].append(np.asarray(r[k]).copy())
        return r

    b._render_freeze = spy
    np.random.seed(3)
    peak = 0.0
    for _ in range(int(SR * 13 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8
    L = np.concatenate(cap["out_l"]).astype(np.float64)
    R = np.concatenate(cap["out_r"]).astype(np.float64)
    # the hold born at 8.0 s: the rest window 8.3 .. 9.8 s is the wash alone
    a, c = int(8.3 * SR), int(9.8 * SR)
    assert _rms(L[a:a + 4096]) > 0.01
    assert abs(np.corrcoef(L[a:c], R[a:c])[0, 1]) < 0.6
    assert -18.0 < _db(_rms(L[c:c + 4096]) / _rms(L[a:a + 4096])) < -12.0
    assert len(b._state[fz.id]["layers"]) == 1
