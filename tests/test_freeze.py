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


def _render(params, sig, gate=None, cv=None, block=512, sr=SR, backend=None):
    """Render ``sig`` through one freeze module; returns the output."""
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
    out = []
    pos = 0
    while pos < len(sig):
        f = min(block, len(sig) - pos)
        bufs = {(99, "in"): np.asarray(sig[pos:pos + f], dtype=np.float32)}
        if gate is not None:
            bufs[(99, "gate")] = np.asarray(gate[pos:pos + f], dtype=np.float32)
        if cv is not None:
            bufs[(99, "cv")] = np.asarray(cv[pos:pos + f], dtype=np.float32)
        out.append(np.asarray(b._render_freeze(fz, f, bufs, fp)).copy())
        pos += f
    return np.concatenate(out), b, fz


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
    assert [(p.name, p.signal_kind) for p in m.OUTPUT_PORTS] == [("out", "audio")]
    kinds = {p.name: p.signal_kind for p in m.INPUT_PORTS}
    assert kinds == {"in": "audio", "freeze": "gate", "pitch_cv": "cv"}
    assert m.DEFAULT_PARAMS == {
        "size": 4096, "freeze": False, "smear": 0.0, "pitch": 0.0,
        "pitch_cv_depth": 1.0, "level": 0.7, "dry": 1.0, "fade": 60.0, "seed": 1,
    }
    assert m.DEFAULT_PARAMS["size"] in FREEZE_SIZES
    assert FREEZE_SIZES == (1024, 2048, 4096, 8192, 16384)


def test_unpatched_input_is_silence_with_no_state():
    p = Patch()
    fz = p.add_module("freeze")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    out = b._render_freeze(fz, 512, {}, _FakePatch([]))
    assert out.shape == (512,) and not out.any()
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
    assert out is sig


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
    assert np.allclose(out, sig * 1.5, atol=1e-6)


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


def _render_blocks(fz, b, sig, gate=None, on_block=None, block=512):
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
        out.append(np.asarray(b._render_freeze(fz, f, bufs, fp)).copy())
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
    y, _, _ = _render({"dry": 5.0, "level": 9.0, "smear": 7.0, "pitch": -400.0, "fade": -3.0, "seed": -5},
                      sig, gate=_gate_from(0.3, 1.5))
    assert np.all(np.isfinite(y))


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
        cap.append(np.asarray(r).copy())
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
