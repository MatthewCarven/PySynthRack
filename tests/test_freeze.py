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


def _render(params, sig, gate=None, cv=None, wcv=None, block=512, sr=SR, backend=None,
            ports=None):
    """Render ``sig`` through one freeze module; returns ``out`` (or, with
    ``ports``, a dict of the named jacks). ``cv`` is ``pitch_cv``, ``wcv``
    is ``width_cv``."""
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
    if wcv is not None:
        cables.append(_Cable("wcv", "width_cv"))
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
        if wcv is not None:
            bufs[(99, "wcv")] = np.asarray(wcv[pos:pos + f], dtype=np.float32)
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
    assert [p.name for p in m.INPUT_PORTS] == ["in", "freeze", "pitch_cv", "width_cv"]
    assert [(p.name, p.signal_kind) for p in m.OUTPUT_PORTS] == [
        ("out", "audio"), ("out_l", "audio"), ("out_r", "audio")]
    kinds = {p.name: p.signal_kind for p in m.INPUT_PORTS}
    assert kinds == {"in": "audio", "freeze": "gate", "pitch_cv": "cv", "width_cv": "cv"}
    assert m.DEFAULT_PARAMS == {
        "size": 4096, "freeze": False, "latch": False, "smear": 0.0, "pitch": 0.0,
        "pitch_cv_depth": 1.0, "level": 0.7, "dry": 1.0, "fade": 60.0, "seed": 1,
        "width": 0.0, "width_cv_depth": 1.0, "decay": 0.0,
    }
    assert m.DEFAULT_PARAMS["size"] in FREEZE_SIZES
    assert FREEZE_SIZES == (1024, 2048, 4096, 8192, 16384, 32768, 65536)


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
    # the capture reads the ``size + size/4`` samples before the edge: a
    # 1 s run-in covers every window up to 32768; 65536 (1.86 s of it)
    # gets a 2 s run-in so it captures the sine, not the silence before it
    run_in = 1 if size + size // 4 < SR else 2
    sig = _sine(441.3, 3.0 + run_in - 1)
    sig[int((run_in + 0.5) * SR):] = 0.0
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "size": size}, sig,
                      gate=_gate_from(run_in, 3.0 + run_in - 1))
    f, amp = _inst_freq(y[(run_in + 1) * SR:(run_in + 2) * SR])
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


def _render_blocks(fz, b, sig, gate=None, on_block=None, block=512, port=None, wcv=None):
    cables = [_Cable("in", "in")] + ([_Cable("gate", "freeze")] if gate is not None else [])
    if wcv is not None:
        cables.append(_Cable("wcv", "width_cv"))
    fp = _FakePatch(cables)
    out = []
    for k, pos in enumerate(range(0, len(sig), block)):
        if on_block is not None:
            on_block(k, fz)
        f = min(block, len(sig) - pos)
        bufs = {(99, "in"): sig[pos:pos + f]}
        if gate is not None:
            bufs[(99, "gate")] = gate[pos:pos + f]
        if wcv is not None:
            bufs[(99, "wcv")] = np.asarray(wcv[pos:pos + f], dtype=np.float32)
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


# ----- width_cv (love pass 2) -------------------------------------------------------


def _const(v, seconds):
    return np.full(int(seconds * SR), v, dtype=np.float32)


def test_width_cv_at_depth_one_is_the_width_knob_bit_exact():
    """The recipe pin: a constant CV of +0.5 at depth 1 IS `width` 0.5,
    on all three jacks, bit for bit — the CV moves the same scale the
    knob does, it does not take a different path. Depth 2 at cv 0.25 is
    the same number; depth 0 is the knob alone (the CV disabled)."""
    sig, g = _triad(3.0), _gate_from(1.0, 3.0)
    knob, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.5}, sig, gate=g, ports=_LR)
    cv, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.0}, sig, gate=g,
                       wcv=_const(0.5, 3.0), ports=_LR)
    for k in _LR:
        assert np.array_equal(knob[k], cv[k]), k
    half, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.0, "width_cv_depth": 2.0},
                         sig, gate=g, wcv=_const(0.25, 3.0), ports=_LR)
    off, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.5, "width_cv_depth": 0.0},
                        sig, gate=g, wcv=_const(0.9, 3.0), ports=_LR)
    for k in _LR:
        assert np.array_equal(knob[k], half[k]) and np.array_equal(knob[k], off[k]), k


def test_width_cv_unpatched_is_the_shipped_render():
    """An unpatched `width_cv` at the default depth adds exactly 0.0, so
    every width setting is the pre-CV render, bit-exact."""
    sig, g = _triad(2.5), _gate_from(0.8, 2.5)
    for w in (0.0, 0.35, 1.0):
        a, _, _ = _render({"dry": 0.3, "level": 0.9, "width": w}, sig, gate=g, ports=_LR)
        b, _, _ = _render({"dry": 0.3, "level": 0.9, "width": w, "width_cv_depth": 1.0},
                          sig, gate=g, ports=_LR)
        for k in _LR:
            assert np.array_equal(a[k], b[k]), (w, k)


@pytest.mark.parametrize("cv,eff", [(0.0, 0.2), (0.3, 0.5), (0.8, 1.0)])
def test_width_cv_follows_the_cosine_law_across_a_sweep(cv, eff):
    """Three points of a CV sweep on a held triad (`width` 0.2 + the CV):
    corr(L, R) is still cos(width_eff·π/2) — 0.951 / 0.707 / 0.000
    measured to three places — and each channel still has the mono's
    RMS, so the CV opens the field without touching the levels."""
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.2, "width_cv_depth": 1.0},
                      _triad(4.0), gate=_gate_from(1.0, 4.0), wcv=_const(cv, 4.0), ports=_LR)
    seg = slice(int(2.5 * SR), 4 * SR)
    L, R, M = (y["out_l"][seg].astype(np.float64),
               y["out_r"][seg].astype(np.float64),
               y["out"][seg].astype(np.float64))
    assert abs(np.corrcoef(L, R)[0, 1] - np.cos(eff * np.pi / 2)) < 0.01
    assert abs(_db(_rms(L) / _rms(M))) < 0.1 and abs(_db(_rms(R) / _rms(M))) < 0.1


def test_a_moving_width_cv_never_clicks_and_really_moves_the_field():
    """A 0.25 Hz LFO over the whole 0…1 range on a held triad. The
    scatter changes only where a frame is synthesised and reaches the
    ears through the overlap-add, so the largest sample step is no
    bigger than the same hold pinned at `width` 1 (measured 0.0351 vs
    0.0351) — a crossfade, never a zipper. And it is not a no-op: the
    channels are correlated 0.99 where the CV is near 0 and 0.06 where
    it is near 1."""
    secs = 6.0
    t = np.arange(int(secs * SR)) / SR
    lfo = (0.5 + 0.5 * np.sin(2 * np.pi * 0.25 * t)).astype(np.float32)
    prm = {"dry": 0.0, "level": 1.0, "width": 0.0, "width_cv_depth": 1.0}
    y, _, _ = _render(prm, _triad(secs), gate=_gate_from(0.5, secs), wcv=lfo, ports=_LR)
    pinned, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 1.0},
                           _triad(secs), gate=_gate_from(0.5, secs), ports=_LR)
    lim = np.abs(np.diff(pinned["out_l"][SR:].astype(np.float64))).max()
    for k in ("out_l", "out_r"):
        assert np.abs(np.diff(y[k][SR:].astype(np.float64))).max() <= lim * 1.05, k
    wide = slice(SR, int(1.5 * SR))          # the CV is ~0.95 here
    narrow = slice(int(2.5 * SR), 3 * SR)    # ~0.05 here
    assert abs(np.corrcoef(y["out_l"][wide].astype(np.float64),
                           y["out_r"][wide].astype(np.float64))[0, 1]) < 0.25
    assert np.corrcoef(y["out_l"][narrow].astype(np.float64),
                       y["out_r"][narrow].astype(np.float64))[0, 1] > 0.95


def test_width_cv_is_clamped_and_a_non_finite_cv_reads_as_no_modulation():
    """An absurd CV clamps to the 0…1 rail (a 50 V CV is `width` 1, a
    −50 V one is mono), and a NaN CV leaves the knob alone: the render
    is the un-modulated one, bit-exact, never a NaN in the output."""
    sig, g = _triad(2.0), _gate_from(0.6, 2.0)
    hi, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.2}, sig, gate=g,
                       wcv=_const(50.0, 2.0), ports=_LR)
    one, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 1.0}, sig, gate=g, ports=_LR)
    lo, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.2}, sig, gate=g,
                       wcv=_const(-50.0, 2.0), ports=_LR)
    for k in _LR:
        assert np.array_equal(hi[k], one[k]), k
    # clamped to 0: the pair is the mono again
    assert np.array_equal(lo["out_l"], lo["out"]) and np.array_equal(lo["out_r"], lo["out"])
    nan = np.full(int(2.0 * SR), np.nan, dtype=np.float32)
    bad, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.4}, sig, gate=g,
                        wcv=nan, ports=_LR)
    knob, _, _ = _render({"dry": 0.0, "level": 1.0, "width": 0.4}, sig, gate=g, ports=_LR)
    for k in _LR:
        assert np.all(np.isfinite(bad[k])) and np.array_equal(bad[k], knob[k]), k


# ----- latch (love pass 2) ----------------------------------------------------------


def _pulses(edges_s, seconds, high_s=0.02):
    g = np.zeros(int(seconds * SR), dtype=np.float32)
    for e in edges_s:
        g[int(e * SR):int((e + high_s) * SR)] = 1.0
    return g


def test_latch_makes_each_rising_edge_toggle_the_hold():
    """Four 20 ms pulses a second apart. With `latch` on they are
    hold / release / hold / release — the hold sounds between pulses one
    and two and between three and four, and is silent between two and
    three. With it off the same momentary pulses leave nothing: a 20 ms
    gate is a 20 ms hold."""
    sig = _triad(6.0)
    g = _pulses([1.0, 2.0, 3.0, 4.0], 6.0)
    prm = {"dry": 0.0, "level": 1.0, "fade": 30.0}
    on, _, _ = _render(dict(prm, latch=True), sig, gate=g)
    off, _, _ = _render(prm, sig, gate=g)
    held = [_rms(on[int(a * SR):int(b * SR)])
            for a, b in ((1.3, 1.9), (2.3, 2.9), (3.3, 3.9), (4.3, 4.9))]
    assert held[0] > 0.2 and held[2] > 0.2
    assert held[1] < 1e-6 and held[3] < 1e-6
    assert _rms(off[int(1.3 * SR):int(1.9 * SR)]) < 1e-6


def test_latch_off_is_the_shipped_gate_bit_exact():
    """`latch` False takes the shipped path: the gate row IS the cable,
    for a held gate and for a pulse train alike."""
    sig = _triad(3.0)
    for g in (_gate_from(1.0, 3.0), _pulses([0.8, 1.6, 2.2], 3.0, high_s=0.4)):
        prm = {"dry": 0.4, "level": 0.9, "width": 0.6, "smear": 0.3}
        a, _, _ = _render(prm, sig, gate=g, ports=_LR)
        b, _, _ = _render(dict(prm, latch=False), sig, gate=g, ports=_LR)
        for k in _LR:
            assert np.array_equal(a[k], b[k]), k


def test_the_latch_state_survives_across_blocks_and_block_sizes():
    """The toggle is a cumsum parity XORed with the state carried in, so
    a pulse train renders identically at 64, 512 and 1000 frames — and a
    pair of edges inside ONE block at 1000 is still a hold and a
    release."""
    sig = _triad(5.0)
    g = _pulses([1.0, 2.0, 3.5], 5.0)
    prm = {"dry": 0.3, "level": 1.0, "latch": True, "fade": 40.0, "width": 0.5}
    ya, _, _ = _render(prm, sig, gate=g, block=64, ports=_LR)
    yb, _, _ = _render(prm, sig, gate=g, block=512, ports=_LR)
    yc, _, _ = _render(prm, sig, gate=g, block=1000, ports=_LR)
    for k in _LR:
        assert np.array_equal(ya[k], yb[k]) and np.array_equal(ya[k], yc[k]), k
    # two edges 5 ms apart, inside one 1000-frame block: on then off
    tight = np.zeros(int(3.0 * SR), dtype=np.float32)
    tight[SR:SR + 100] = 1.0
    tight[SR + 300:SR + 400] = 1.0
    for blk in (64, 1000):
        y, b, fz = _render({"dry": 0.0, "level": 1.0, "latch": True, "fade": 20.0},
                           _triad(3.0), gate=tight, block=blk)
        assert _rms(y[int(2.0 * SR):int(2.8 * SR)]) < 1e-6, blk
        assert not b._state[fz.id]["layers"]


def test_latch_engaged_mid_hold_adopts_the_hold_and_does_not_glitch():
    """The gate is held high from 1.0 s and `latch` is switched on at
    2.0 s. The hold is adopted, not released: the level is unchanged
    across the flip (no step beyond a sine's own), it survives the
    cable's own fall at 2.5 s, and the next rising edge at 3.5 s is what
    finally lets go."""
    sig = _sine(441.3, 5.0)
    sig[int(1.5 * SR):] = 0.0
    g = _gate_from(1.0, 5.0)
    g[int(2.5 * SR):] = 0.0
    g[int(3.5 * SR):int(3.52 * SR)] = 1.0
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update({"dry": 0.0, "level": 1.0, "fade": 40.0})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)

    def flip(k, m):
        if k == (2 * SR) // 512:
            m.params["latch"] = True

    y = _render_blocks(fz, b, sig, gate=g, on_block=flip)
    own = 0.5 * 2 * np.pi * 441.3 / SR
    assert np.abs(np.diff(y[int(1.8 * SR):int(3.3 * SR)])).max() <= own * 1.05
    before = _rms(y[int(1.8 * SR):int(2.0 * SR)])
    assert abs(_db(_rms(y[int(2.1 * SR):int(2.3 * SR)]) / before)) < 0.1
    assert abs(_db(_rms(y[int(2.8 * SR):int(3.2 * SR)]) / before)) < 0.1
    assert _rms(y[int(3.8 * SR):int(4.5 * SR)]) < 1e-6


def test_the_tickbox_forces_the_hold_over_the_latch():
    """The tickbox beats the latch: ticked, the hold is on whatever the
    latch says; unticked, the hold goes back to the latch's own state
    (which kept running underneath)."""
    sig = _triad(5.0)
    g = _pulses([1.0, 2.0], 5.0)
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update({"dry": 0.0, "level": 1.0, "latch": True, "fade": 30.0})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)

    def tick(k, m):
        if k == int(2.5 * SR) // 512:
            m.params["freeze"] = True
        if k == int(3.5 * SR) // 512:
            m.params["freeze"] = False

    y = _render_blocks(fz, b, sig, gate=g, on_block=tick)
    assert _rms(y[int(1.3 * SR):int(1.9 * SR)]) > 0.2      # latched on
    assert _rms(y[int(2.2 * SR):int(2.4 * SR)]) < 1e-6     # latched off again
    assert _rms(y[int(2.8 * SR):int(3.4 * SR)]) > 0.2      # the tickbox forces it
    assert _rms(y[int(3.9 * SR):int(4.8 * SR)]) < 1e-6     # untick: the latch says off


# ----- the long windows (love pass 2) -----------------------------------------------


def test_the_long_window_is_offered_and_holds_a_triad():
    """32768 (743 ms at 44.1 kHz) is on the knob and holds the same triad
    the shipped 16384 does, partial for partial within 0.1 dB — the
    capture is longer, the hold is not weaker."""
    assert 32768 in FREEZE_SIZES
    ref = None
    for size in (16384, 32768):
        y, _, _ = _render({"dry": 0.0, "level": 1.0, "size": size},
                          _triad(6.0), gate=_gate_from(1.5, 6.0))
        seg = y[4 * SR:6 * SR]
        assert _rms(seg) > 0.2
        lv = _levels(seg)
        assert np.all(lv > 0.05)
        if ref is None:
            ref = lv
        else:
            assert np.all(np.abs(20 * np.log10(lv / ref)) < 0.1)


def test_the_long_window_averages_a_phrase_not_a_moment():
    """The documented character of 32768: the window is 743 ms, so what
    it holds is a PHRASE, not a moment. A three-note arpeggio (200 ms a
    note) frozen on its last note comes back as all three notes at 4096
    only the last one is there, the first two are 175 dB down, i.e.
    absent. At 32768 all three are inside 30 dB of each other and the
    first note is 100 dB louder than 4096 left it. Hann-weighted, so
    the middle of the window carries the most: the last note, right at
    the edge where the window tapers to zero, is the quietest of the
    three."""
    t = np.arange(int(4.0 * SR))
    sig = np.zeros(len(t), dtype=np.float64)
    step = int(0.2 * SR)
    for i, f in enumerate(_TRIAD):            # three notes, 200 ms each
        a, bnd = int(0.6 * SR) + i * step, int(0.6 * SR) + (i + 1) * step
        sig[a:bnd] = 0.35 * np.sin(2 * np.pi * f * t[a:bnd] / SR)
    sig = sig.astype(np.float32)
    g = _gate_from(1.2, 4.0)                  # the edge just after the last note
    lv = {}
    for size in (4096, 32768):
        y, _, _ = _render({"dry": 0.0, "level": 1.0, "size": size}, sig, gate=g)
        lv[size] = 20 * np.log10(np.maximum(_levels(y[int(2.5 * SR):4 * SR]), 1e-12))
    assert lv[4096][0] < lv[4096][2] - 100.0          # the first note is simply gone
    assert lv[4096][1] < lv[4096][2] - 100.0
    assert float(lv[32768].max() - lv[32768].min()) < 30.0
    assert lv[32768][0] > lv[4096][0] + 100.0
    assert lv[32768][2] == lv[32768].min()            # the edge is the window's taper


def test_the_long_window_is_block_size_exact_with_every_feature_live():
    """64 vs 512 at 32768 with `latch`, a constant `width_cv`, `decay`,
    `smear` and `pitch` all live: all three jacks bit-exact."""
    sig = _triad(4.0)
    g = _pulses([1.0, 2.6], 4.0)
    prm = {"dry": 0.3, "level": 1.0, "size": 32768, "latch": True, "smear": 0.4,
           "pitch": 7.0, "width": 0.3, "width_cv_depth": 1.0, "decay": 5.0, "fade": 90.0}
    wcv = _const(0.4, 4.0)
    ya, _, _ = _render(prm, sig, gate=g, wcv=wcv, block=64, ports=_LR)
    yb, _, _ = _render(prm, sig, gate=g, wcv=wcv, block=512, ports=_LR)
    for k in _LR:
        assert np.array_equal(ya[k], yb[k]), k
    assert not np.array_equal(ya["out_l"], ya["out_r"])
    assert _rms(ya["out"][int(1.6 * SR):int(2.4 * SR)]) > 0.05


def test_the_peak_region_map_matches_the_loop_it_replaced():
    """The vectorized `_freeze_lock_index` is INTEGER-identical to the
    Python loop it replaced (which cost 17 ms of a 23 ms capture at
    65536). Checked against a reference implementation of the loop over
    the awkward shapes: ties everywhere, all-zero, everything below the
    1e-12 floor, a single peak, and real triad / sine / noise / DC rffts
    at every offered window size."""
    def loop(mag):
        k_n = mag.shape[0]
        idx = np.arange(k_n)
        up = np.concatenate(([False], mag[1:] > mag[:-1]))
        down = np.concatenate((mag[:-1] >= mag[1:], [False]))
        peaks = np.flatnonzero(up & down & (mag > 1e-12))
        if peaks.size == 0:
            return idx
        bounds = [0]
        for a, b in zip(peaks[:-1].tolist(), peaks[1:].tolist()):
            bounds.append(a + int(np.argmin(mag[a:b + 1])))
        bounds.append(k_n)
        for pk, lo, hi in zip(peaks.tolist(), bounds[:-1], bounds[1:]):
            idx[lo:hi] = pk
        return idx

    rng = np.random.default_rng(7)
    for trial in range(600):
        k = int(rng.integers(1, 400))
        kind = trial % 6
        if kind == 0:
            mag = np.abs(rng.standard_normal(k))
        elif kind == 1:
            mag = np.abs(rng.integers(0, 4, k).astype(float))       # ties everywhere
        elif kind == 2:
            mag = np.zeros(k)
        elif kind == 3:
            mag = np.abs(rng.standard_normal(k)) * 1e-15            # all under the floor
        elif kind == 4:
            mag = np.abs(rng.standard_normal(k))
            mag[rng.random(k) < 0.4] = 0.0
        else:
            mag = np.abs(np.sin(np.arange(k) * 0.3)) + rng.random(k) * 1e-13
        got = NumpyBackend._freeze_lock_index(mag)
        want = loop(mag)
        assert got.shape == want.shape and np.array_equal(got, want), (trial, k)
    for n in FREEZE_SIZES:
        t = np.arange(n)
        w = np.hanning(n + 1)[:-1]
        for sig in (sum(0.25 * np.sin(2 * np.pi * f * t / SR) for f in _TRIAD),
                    np.random.default_rng(1).standard_normal(n),
                    np.sin(2 * np.pi * 441.3 * t / SR),
                    np.zeros(n), np.ones(n)):
            mag = np.abs(np.fft.rfft(sig * w))
            assert np.array_equal(NumpyBackend._freeze_lock_index(mag), loop(mag)), n


# ----- the relative peak floor + the staged birth (2026-09-24) -----------------------

_D65 = NumpyBackend._FREEZE_BIRTH_DELAY.get(65536, 0)


def _late_mag(sig, n):
    """|rfft| of the capture's later frame from the first n + n/4 samples."""
    hop = n // 4
    w = np.hanning(n + 1)[:-1]
    return np.abs(np.fft.rfft(np.asarray(sig[hop:hop + n], dtype=np.float64) * w))


def _rel_floor(mag):
    return float(mag.max()) * 10.0 ** (NumpyBackend._FREEZE_PEAK_FLOOR_DB / 20.0)


def test_the_relative_floor_finds_the_triads_three_partials():
    """A pure triad used to anchor thousands of regions in its own numerical
    floor (4518 at 32768 under the absolute 1e-12); the capture's floor is
    now -120 dB under the frame's loudest bin, and the region map is the
    three partials at every window that resolves them. Checked through the
    capture itself: the stereo scatter's sign flips exactly twice."""
    assert NumpyBackend._FREEZE_PEAK_FLOOR_DB == -120.0
    sig = _triad(2.0)
    for n in FREEZE_SIZES:
        if n < 4096:
            continue                      # too coarse for C-E-G (the resolution rule)
        mag = _late_mag(sig, n)
        assert np.unique(NumpyBackend._freeze_lock_index(mag)).size > 100, n
        assert np.unique(NumpyBackend._freeze_lock_index(mag, _rel_floor(mag))).size == 3, n
        spec = NumpyBackend._freeze_capture(np.asarray(sig[:n + n // 4], dtype=np.float64),
                                            n, n // 4)
        assert int(np.count_nonzero(np.diff(spec["jside"].imag))) == 2, n


def test_the_relative_floor_keeps_every_peak_of_a_dense_spectrum():
    """The risk of a relative floor is throwing real content away. It does
    not: white noise and a detuned saw chord keep EVERY region the absolute
    floor gave them, at the default window and the two longest."""
    t = np.arange(2 * SR)
    saws = sum(2.0 * ((f * t / SR) % 1.0) - 1.0
               for f in (130.4, 131.2, 164.81, 196.0, 246.94))
    noise = np.random.default_rng(3).standard_normal(2 * SR)
    for sig in (noise, saws):
        for n in (4096, 32768, 65536):
            mag = _late_mag(sig, n)
            assert np.array_equal(NumpyBackend._freeze_lock_index(mag),
                                  NumpyBackend._freeze_lock_index(mag, _rel_floor(mag))), n


def test_one_ulp_of_input_no_longer_reshuffles_the_stereo_field(monkeypatch):
    """The knife-edge the relative floor removes (found by the 2026-09-24
    float64 voice-collapse pass: a one-ulp input change moved the drone
    example's stereo hold by 4.8%). The scatter's sign alternates by
    region ORDINAL, so under the absolute 1e-12 floor an ulp that flips
    one numerical-floor "peak" in or out swaps half the partials between L
    and R. Measured on an organ maj7 at 32768, width 0.8: -13 dB of change
    on out_l from one ulp under the old floor; under -120 dB the change is
    the ulp's own size. The old floor is re-run here to prove the test can
    see the difference."""
    t = np.arange(5 * SR)
    org = sum(0.05 / h * np.sin(2 * np.pi * f * h * t / SR)
              for f in (130.81, 164.81, 196.0, 246.94) for h in range(1, 7)).astype(np.float32)
    rng = np.random.default_rng(0)
    nudged = org.copy()
    pick = rng.random(org.size) < 0.5
    nudged[pick] = np.nextafter(nudged[pick], np.float32(np.inf))
    g = _gate_from(2.0, 5.0)
    prm = {"dry": 0.0, "level": 1.0, "size": 32768, "width": 0.8}

    def change():
        a, _, _ = _render(prm, org, gate=g, ports=("out_l",))
        b, _, _ = _render(prm, nudged, gate=g, ports=("out_l",))
        d = b["out_l"][3 * SR:].astype(np.float64) - a["out_l"][3 * SR:]
        return _db(_rms(d) / _rms(a["out_l"][3 * SR:]))

    assert change() < -100.0
    monkeypatch.setattr(NumpyBackend, "_FREEZE_PEAK_FLOOR_DB", -1000.0)   # the old floor
    assert change() > -40.0


def test_the_region_map_with_a_floor_matches_the_loop():
    """The ``floor`` argument is the loop's threshold, nothing more:
    integer-identical to the reference loop at arbitrary floors."""
    def loop(mag, floor):
        k_n = mag.shape[0]
        idx = np.arange(k_n)
        up = np.concatenate(([False], mag[1:] > mag[:-1]))
        down = np.concatenate((mag[:-1] >= mag[1:], [False]))
        peaks = np.flatnonzero(up & down & (mag > floor))
        if peaks.size == 0:
            return idx
        bounds = [0]
        for a, b in zip(peaks[:-1].tolist(), peaks[1:].tolist()):
            bounds.append(a + int(np.argmin(mag[a:b + 1])))
        bounds.append(k_n)
        for pk, lo, hi in zip(peaks.tolist(), bounds[:-1], bounds[1:]):
            idx[lo:hi] = pk
        return idx

    rng = np.random.default_rng(11)
    for trial in range(300):
        k = int(rng.integers(1, 400))
        mag = np.abs(rng.standard_normal(k)) * 10.0 ** rng.uniform(-8, 0, k)
        floor = float(mag.max()) * 10.0 ** rng.uniform(-9, 0) if k else 1e-12
        assert np.array_equal(NumpyBackend._freeze_lock_index(mag, floor), loop(mag, floor)), trial


def test_the_rotor_stream_does_not_drift():
    """Frame j + 1 is frame j times the capture's rotor -- one complex
    multiply, not an exp and a mod over every bin. After 200000 frames
    (14 minutes of hold at 1024) the stream is still the exact
    ``X0 * exp(i * j * angle(rot))`` to 1e-9 on every bin that matters."""
    n, hop, J = 1024, 256, 200000
    spec = NumpyBackend._freeze_capture(np.asarray(_triad(0.1)[:n + hop], dtype=np.float64),
                                        n, hop)
    x0, rot = spec["X"], spec["rot"]
    x = x0.copy()
    for _ in range(J):
        x = x * rot
    theta = np.mod(J * np.angle(rot), 2.0 * np.pi)
    exact = x0 * np.exp(1j * theta)
    big = np.abs(x0) > 1e-6 * np.abs(x0).max()
    assert np.max(np.abs(x - exact)[big] / np.abs(x0)[big]) < 1e-9


def test_the_65536_window_is_born_a_staged_delay_after_the_edge():
    """65536 (1.49 s) is on the knob with a STAGED birth: the capture is
    taken at the edge, but the layer is born `_FREEZE_BIRTH_DELAY` (4096
    samples, 93 ms) later -- dry 0 is silent until then and the fade
    starts there. What it holds is the same triad 32768 holds, partial for
    partial within 0.1 dB."""
    assert 65536 in FREEZE_SIZES and _D65 == 4096
    sig = _triad(6.0)
    e = 2 * SR
    g = np.zeros_like(sig)
    g[e:] = 1.0
    y, _, _ = _render({"dry": 0.0, "level": 1.0, "size": 65536}, sig, gate=g)
    assert not y[:e + _D65].any()
    assert y[e + _D65:e + _D65 + 64].any()
    ref, _, _ = _render({"dry": 0.0, "level": 1.0, "size": 32768}, sig, gate=g)
    lv, lr = _levels(y[4 * SR:6 * SR]), _levels(ref[4 * SR:6 * SR])
    assert np.all(lv > 0.05)
    assert np.all(np.abs(20 * np.log10(lv / lr)) < 0.1)


def test_the_staged_birth_still_continues_the_input_in_phase():
    """The read starts at frozen time ``n + delay``, so where the hold is
    first heard it is the live input's own continuation: a sine frozen at
    65536 with dry 1 and level 1 DOUBLES. 446.8 Hz is picked so the delay
    is 41.5 of its cycles -- a read that ignored the delay would land half
    a cycle out and cancel instead."""
    assert abs(_D65 * 446.8 / SR - 41.5) < 0.01
    sig = _sine(446.8, 5.0)
    y, _, _ = _render({"dry": 1.0, "level": 1.0, "size": 65536}, sig, gate=_gate_from(2.0, 5.0))
    assert 1.95 < _rms(y[3 * SR:5 * SR]) / _rms(sig[:SR]) < 2.05


def test_the_staged_birth_spreads_its_work_one_stage_per_512_block():
    """Deterministic, not a stopwatch: the birth's seven stages (two FFTs,
    the analysis, the four frames under the first read) are spread over the
    delay -- at 512 no block runs more than one, the edge's own block runs
    just the first, and all seven are done by the birth."""
    sig = _triad(3.0)
    g = _gate_from(2.0, 3.0)
    p = Patch()
    fz = p.add_module("freeze")
    fz.params.update({"dry": 0.0, "level": 1.0, "size": 65536, "width": 0.5})
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(p)
    per_block = []
    orig = b._freeze_birth_step

    def counting(*a, **k):
        per_block[-1] += 1
        return orig(*a, **k)

    b._freeze_birth_step = counting

    def tick(k, m):
        per_block.append(0)

    _render_blocks(fz, b, sig, gate=g, on_block=tick)
    eb = (2 * SR) // 512
    assert sum(per_block) == 7 and max(per_block) == 1
    assert per_block[eb] == 1
    assert sum(per_block[:(2 * SR + _D65) // 512 + 1]) == 7
    assert not b._state[fz.id]["pending"] and len(b._state[fz.id]["layers"]) == 1


def test_a_tap_shorter_than_the_birth_delay_still_freezes():
    """Every layer hears the gate `delay` samples late, so the whole wet
    path is the shipped one shifted: a 30 ms tap (1323 samples, under the
    4096 delay) holds for its 30 ms and releases, 93 ms late."""
    sig = _triad(4.0)
    y, b, fz = _render({"dry": 0.0, "level": 1.0, "size": 65536, "fade": 5.0}, sig,
                       gate=_pulses([2.0], 4.0, high_s=0.03))
    e = 2 * SR + _D65
    assert _rms(y[e + 300:e + 1300]) > 0.1
    assert not y[e + 1323 + 500:].any()
    assert b._state[fz.id]["layers"] == []


def test_the_staged_birth_is_block_size_exact_with_every_feature_live():
    """64 = 512 = 1000 at 65536 with `latch`, a constant `width_cv`,
    `decay`, `smear` and `pitch` live, and a second edge landing INSIDE the
    first one's birth delay (two captures pending at once): all three
    jacks bit-exact."""
    sig = _triad(5.0)
    g = _pulses([2.0, 2.05, 3.6], 5.0)
    prm = {"dry": 0.3, "level": 1.0, "size": 65536, "latch": True, "smear": 0.4,
           "pitch": 7.0, "width": 0.3, "width_cv_depth": 1.0, "decay": 5.0, "fade": 90.0}
    wcv = _const(0.4, 5.0)
    ya, _, _ = _render(prm, sig, gate=g, wcv=wcv, block=64, ports=_LR)
    for blk in (512, 1000):
        yb, _, _ = _render(prm, sig, gate=g, wcv=wcv, block=blk, ports=_LR)
        for k in _LR:
            assert np.array_equal(ya[k], yb[k]), (blk, k)
    assert not np.array_equal(ya["out_l"], ya["out_r"])
    dry = 0.3 * _rms(sig)
    # latched off between the second pulse and the third: the dry alone;
    # latched on after the third: the hold on top of it
    assert _rms(ya["out"][int(2.4 * SR):int(3.5 * SR)]) < 1.02 * dry
    assert _rms(ya["out"][int(4.0 * SR):]) > 1.5 * dry


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
    # love pass 2: latch a checkbox, width_cv_depth a bounded depth drag,
    # and the long window on the combo
    assert w["latch (gate toggles)"][0] == "add_checkbox"
    wc = w["width_cv_depth"]
    assert wc[0] == "add_drag_float" and (wc[2]["min_value"], wc[2]["max_value"]) == (0.0, 4.0)
    assert wc[1] == "%.2f width/unit"
    assert "32768" in w["size (fft)"][2]["items"]


def test_size_combo_stores_an_int(monkeypatch):
    w, app, module = _widgets(monkeypatch)
    cb = w["size (fft)"][2]["callback"]
    cb(None, "8192", (module.id, "size"))
    assert module.params["size"] == 8192 and isinstance(module.params["size"], int)
    cb(None, "3000", (module.id, "size"))
    assert module.params["size"] == 2048          # snapped onto the set
    cb(None, "32768", (module.id, "size"))
    assert module.params["size"] == 32768
    cb(None, "65536", (module.id, "size"))
    assert module.params["size"] == 65536         # the staged-birth top (2026-09-24)
    cb(None, "131072", (module.id, "size"))
    assert module.params["size"] == 65536         # past the top: snapped back on


# ----- example --------------------------------------------------------------------------


def test_the_chord_pad_example_holds_between_chords():
    """Each chord plays for a quarter of its bar; the freeze holds the rest.
    Measured: the output never drops out inside a bar, and the frozen part
    of a bar keeps the chord's level."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "freeze_chord_pad.json"
    patch = load_patch(path)
    assert any(m.TYPE == "freeze" for m in patch)
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


def test_the_drone_breathe_example_holds_forever_and_breathes():
    """The long-window drone: ONE schmitt edge latches a 743 ms capture of
    the maj7 chord and `decay` 0 holds it for the rest of the render while
    a sparse pluck line plays over it. Measured on the freeze's own jacks:
    exactly one layer alive at the end with the latch still set, the held
    level flat to +/-8% across seconds 3..13 (the chord itself stopped at
    1.6 s -- the input is a tenth of the output from then on), and the
    0.12 Hz LFO on `width_cv` sweeps corr(L, R) from over 0.95 near the
    breath's narrow point to under 0.1 at its wide one."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "freeze_drone_breathe.json"
    patch = load_patch(path)
    fz = next(m for m in patch if m.TYPE == "freeze")
    assert fz.params["size"] == 32768 and fz.params["latch"] is True
    assert fz.params["decay"] == 0.0 and fz.params["width_cv_depth"] == 0.85
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    cap = {"out": [], "out_l": [], "out_r": [], "in": []}
    orig = b._render_freeze

    def spy(module, frames, buffers, p):
        src = b._input_buffer(p, buffers, module.id, "in")
        cap["in"].append(np.zeros(frames, np.float32) if src is None else np.asarray(src).copy())
        r = orig(module, frames, buffers, p)
        for k in ("out", "out_l", "out_r"):
            cap[k].append(np.asarray(r[k]).copy())
        return r

    b._render_freeze = spy
    np.random.seed(5)
    peak = 0.0
    for _ in range(int(SR * 13 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8
    L = np.concatenate(cap["out_l"]).astype(np.float64)
    R = np.concatenate(cap["out_r"]).astype(np.float64)
    M = np.concatenate(cap["out"]).astype(np.float64)
    src = np.concatenate(cap["in"]).astype(np.float64)
    # one edge, one layer, still latched at the end
    assert len(b._state[fz.id]["layers"]) == 1
    assert b._state[fz.id]["latched"] is True
    # the hold never dies and never grows: flat across seconds 3..13
    held = np.array([_rms(M[i * SR:(i + 1) * SR]) for i in range(3, 13)])
    assert held.min() > 0.05
    assert held.max() / held.min() < 1.16
    # and it is a HOLD, not the input: the source is a tenth of the output
    assert _rms(src[4 * SR:12 * SR]) < 0.35 * _rms(M[4 * SR:12 * SR])
    # the breath: one 8.33 s cycle of corr(L, R) between wide and narrow
    corr = np.array([np.corrcoef(L[i * SR:(i + 1) * SR], R[i * SR:(i + 1) * SR])[0, 1]
                     for i in range(2, 13)])
    assert corr.min() < 0.1 and corr.max() > 0.95
