"""Tests for the Reverb (stereo Feedback Delay Network).

Coverage:
  - Model: registration, defaults, ports/kinds (audio in -> out_l/out_r
    audio), JSON round-trip, unknown-param rejection, type walls.
  - DSP: disconnected -> silence; mix=0 is a bit-exact dry passthrough on
    both channels; an impulse produces a decaying tail; more decay = a
    longer tail; damping rolls the tail's highs off; the tail is dense
    (diffusion) not gappy; output stays finite/bounded at max decay; a
    voice (2D) input is summed to mono.
  - Block independence: the chunked FDN gives identical output at any
    block size (512 vs 4096 vs an odd size) -- the key correctness
    property.
  - Stereo: out_l and out_r are decorrelated (that's the width).
  - Integration: osc -> reverb -> L/R speakers renders audible audio.
  - Freeze (2026-09-19 love pass): the ``freeze`` gate holds the tail as
    a pad -- RMS measured at +5 s / +10 s against the moment of the
    freeze (within 3 dB; the circulating tank energy within 0.01 dB,
    the lossless pin) while the unfrozen tail is gone; the input is
    muted from the tank but the dry path passes; release resumes decay;
    no click at either edge (steps no larger than the tail's own);
    patched-but-low == unpatched bit-exact (the ship-off pin); block-size
    independence with the edge mid-stream; a (V, F) gate collapses to
    any-voice-high; finite/bounded at max settings; every param gets a
    bounded widget; the example plays.
  - Freeze tickbox (2026-09-20 love pass): the ``freeze`` param ORed
    with the gate -- ticked at block k with nothing patched it is a gate
    cable rising at block k's first sample, bit-exact (twice over, and
    at 64 as at 512); ticked under a held cable or with the cable low
    it is the cable alone; un-ticked and unpatched never touches the
    ramp state (the pre-freeze code by construction); the panel gets a
    ``freeze (or gate)`` checkbox.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type
from pysynthrack.modules.reverb import Reverb

SR = 44100
F = 512


def _rig(params=None, block=F):
    patch = Patch()
    src = patch.add_module("oscillator")
    rv = patch.add_module("reverb", params=params or {})
    patch.connect(src.id, "out", rv.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, rv, b


def _run(b, patch, src, rv, signal, block=F):
    n = (signal.shape[-1] // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        o = b._render_reverb(rv, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _impulse(n):
    x = np.zeros(n, dtype=np.float32)
    x[0] = 1.0
    return x


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        rv = Patch().add_module("reverb")
        assert isinstance(rv, Reverb)
        assert rv.params == {
            "size": 0.5,
            "decay": 0.5,
            "damping": 0.5,
            "mix": 0.3,
            "cv_depth": 1.0,
            "freeze": False,
        }

    def test_ports_and_kinds(self):
        rv = Patch().add_module("reverb")
        assert [(p.name, p.signal_kind) for p in rv.input_ports] == [
            ("in", "audio"),
            ("decay_cv", "cv"),
            ("damping_cv", "cv"),
            ("mix_cv", "cv"),
            ("freeze", "gate"),
        ]
        assert [(p.name, p.signal_kind) for p in rv.output_ports] == [
            ("out_l", "audio"),
            ("out_r", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("reverb", params={"decay": 0.85, "mix": 0.5})
        restored = Patch.from_dict(patch.to_dict())
        rv = next(m for m in restored if m.TYPE == "reverb")
        assert rv.params["decay"] == 0.85
        assert rv.params["mix"] == 0.5

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("reverb", params={"room": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        rv = patch.add_module("reverb")
        patch.connect(osc.id, "out", rv.id, "in")

    def test_cv_into_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        rv = patch.add_module("reverb")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", rv.id, "in")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        rv = patch.add_module("reverb")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(rv.id, "out_l", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        rv = patch.add_module("reverb")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        o = b._render_reverb(rv, F, {}, patch)
        assert not np.any(o["out_l"]) and not np.any(o["out_r"])
        assert o["out_l"].shape == (F,)

    def test_frames_zero_empty(self):
        patch, src, rv, b = _rig()
        o = b._render_reverb(rv, 0, {(src.id, "out"): np.zeros(0, np.float32)}, patch)
        assert o["out_l"].shape == (0,) and o["out_r"].shape == (0,)

    def test_mix_zero_exact_dry_passthrough(self):
        patch, src, rv, b = _rig({"mix": 0.0, "decay": 0.7})
        x = np.random.randn(F * 3).astype(np.float32)
        lo, r = _run(b, patch, src, rv, x)
        assert np.array_equal(lo, x[: len(lo)])
        assert np.array_equal(r, x[: len(r)])

    def test_impulse_decays(self):
        patch, src, rv, b = _rig({"decay": 0.6, "mix": 1.0, "damping": 0.4})
        lo, r = _run(b, patch, src, rv, _impulse(SR))
        early = np.sqrt(np.mean(lo[:2000] ** 2))
        late = np.sqrt(np.mean(lo[-2000:] ** 2))
        assert late < 0.2 * early  # tail has decayed
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))

    def test_more_decay_longer_tail(self):
        imp = _impulse(SR)
        p1, s1, r1, b1 = _rig({"decay": 0.3, "mix": 1.0, "damping": 0.3})
        p2, s2, r2, b2 = _rig({"decay": 0.85, "mix": 1.0, "damping": 0.3})
        l_short, _ = _run(b1, p1, s1, r1, imp)
        l_long, _ = _run(b2, p2, s2, r2, imp)
        w = slice(int(0.5 * SR), int(0.6 * SR))
        assert np.sqrt(np.mean(l_long[w] ** 2)) > 5 * np.sqrt(np.mean(l_short[w] ** 2))

    def test_tail_is_dense_not_gappy(self):
        patch, src, rv, b = _rig({"decay": 0.7, "mix": 1.0, "damping": 0.4})
        lo, _ = _run(b, patch, src, rv, _impulse(SR))
        seg = lo[int(0.1 * SR):int(0.5 * SR)]
        assert np.mean(np.abs(seg) < 1e-5) < 0.1  # diffusion fills the tail

    def test_damping_rolls_off_tail_highs(self):
        imp = _impulse(SR)
        pb, sb, rb_, bb = _rig({"decay": 0.7, "mix": 1.0, "damping": 0.05})
        pd, sd, rd_, bd = _rig({"decay": 0.7, "mix": 1.0, "damping": 0.95})
        lb, _ = _run(bb, pb, sb, rb_, imp)
        ld, _ = _run(bd, pd, sd, rd_, imp)

        def hf_energy(y):
            yt = y[int(0.2 * SR):int(0.6 * SR)]
            d = np.diff(yt)  # crude high-pass
            return float(np.sum(d ** 2))

        assert hf_energy(ld) < hf_energy(lb)

    def test_stability_at_max_decay(self):
        patch, src, rv, b = _rig({"decay": 1.0, "mix": 1.0, "damping": 0.2})
        x = np.random.randn(2 * SR).astype(np.float32)
        lo, r = _run(b, patch, src, rv, x)
        assert np.all(np.isfinite(lo)) and np.all(np.isfinite(r))
        assert np.max(np.abs(lo)) < 50.0 and np.max(np.abs(r)) < 50.0

    def test_voice_input_summed_to_mono(self):
        # A 2D (V, F) input is collapsed to mono by the input helper.
        patch, src, rv, b = _rig({"mix": 1.0, "decay": 0.5})
        v = np.random.randn(3, F).astype(np.float32)
        o = b._render_reverb(rv, F, {(src.id, "out"): v}, patch)
        assert o["out_l"].shape == (F,) and np.all(np.isfinite(o["out_l"]))


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def test_output_independent_of_block_size(self):
        x = (np.random.randn(8192) * 0.3).astype(np.float32)
        params = {"size": 0.6, "decay": 0.6, "damping": 0.4, "mix": 0.5}
        pa, sa, ra, ba = _rig(params, block=512)
        la, raa = _run(ba, pa, sa, ra, x, block=512)
        pb, sb, rb, bb = _rig(params, block=4096)
        lb, rbb = _run(bb, pb, sb, rb, x, block=4096)
        pc, sc, rc, bc = _rig(params, block=333)
        lc, rcc = _run(bc, pc, sc, rc, x, block=333)
        m = min(len(la), len(lb), len(lc))
        assert np.array_equal(la[:m], lb[:m])
        assert np.array_equal(la[:m], lc[:m])
        assert np.array_equal(raa[:m], rbb[:m])


# ----- Stereo ----------------------------------------------------------------


class TestStereo:
    def test_channels_are_decorrelated(self):
        patch, src, rv, b = _rig({"decay": 0.7, "mix": 1.0})
        x = (np.random.randn(SR) * 0.3).astype(np.float32)
        lo, r = _run(b, patch, src, rv, x)
        assert not np.array_equal(lo, r)
        corr = np.corrcoef(lo[3000:], r[3000:])[0, 1]
        assert abs(corr) < 0.5


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_reverb_stereo_speakers(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0})
        rv = patch.add_module("reverb", params={"decay": 0.7, "mix": 0.4})
        spk_l = patch.add_module("left_speaker_output")
        spk_r = patch.add_module("right_speaker_output")
        patch.connect(osc.id, "out", rv.id, "in")
        patch.connect(rv.id, "out_l", spk_l.id, "in")
        patch.connect(rv.id, "out_r", spk_r.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(80):
            blk = b.render_block(F)
            assert blk is not None and np.all(np.isfinite(blk))
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0


# ----- Freeze ----------------------------------------------------------------

FZ_PARAMS = {"size": 0.8, "decay": 0.5, "damping": 0.5, "mix": 1.0}
RAMP = int(round(NumpyBackend._REVERB_FREEZE_RAMP_S * SR))
T_FZ = SR + 37          # the freeze edge: mid-block at 64 and at 512
W = int(0.25 * SR)      # RMS window


def _fz_rig(params=None, block=F, patched=True):
    patch = Patch()
    src = patch.add_module("oscillator")
    clk = patch.add_module("clock")
    rv = patch.add_module("reverb", params=dict(FZ_PARAMS if params is None else params))
    patch.connect(src.id, "out", rv.id, "in")
    if patched:
        patch.connect(clk.id, "out", rv.id, "freeze")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, clk, rv, b


def _fz_run(rig, x, fz, block=F, tick=None):
    """Drive the renderer block by block with an audio row and a gate row
    (``fz`` None = the freeze buffer is never published). ``tick`` is a
    list of ``(k0, k1)`` block ranges over which the ``freeze`` tickbox
    is on -- toggled on the module's params before each block, the way
    the panel writes it."""
    patch, src, clk, rv, b = rig
    n = (len(x) // block) * block
    ls, rs = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): x[sl].astype(np.float32)}
        if fz is not None:
            bufs[(clk.id, "out")] = fz[sl].astype(np.float32)
        if tick is not None:
            rv.params["freeze"] = any(k0 <= k < k1 for k0, k1 in tick)
        o = b._render_reverb(rv, block, bufs, patch)
        ls.append(o["out_l"])
        rs.append(o["out_r"])
    return np.concatenate(ls), np.concatenate(rs)


def _burst(n, seconds=0.3, level=0.3, seed=7, at=0):
    rng = np.random.default_rng(seed)
    x = np.zeros(n, dtype=np.float32)
    m = int(seconds * SR)
    x[at:at + m] = (rng.standard_normal(m) * level).astype(np.float32)
    return x


def _tone(n, freq=220.0, level=0.3):
    return (level * np.sin(2.0 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


def _gate(n, start, stop):
    g = np.zeros(n, dtype=np.float32)
    g[start:stop] = 1.0
    return g


LMAX = int(max(NumpyBackend._REVERB_BASE)) + 2   # the longest line, at 44.1 kHz


def _edge_steps(y, t, ramp_n):
    """(max step across the edge, the tail's own max step beside it).

    The freeze changes what the tank WRITES, and the taps read a write
    ``L_i`` samples (21-60 ms) later, so the window has to run from the
    edge to ``t + LMAX + 2 ramp`` -- a window of a few ramps around the
    edge itself sees nothing at all. The reference is the larger of the
    half second before the edge and the half second after the window."""
    d = np.abs(np.diff(y.astype(np.float64)))
    end = t + LMAX + 2 * ramp_n
    edge = float(d[t - ramp_n:end].max())
    own = max(float(d[max(0, t - SR // 2):t - ramp_n].max()),
              float(d[end:end + SR // 2].max()))
    return edge, own


def _rms(y):
    return float(np.sqrt(np.mean(np.asarray(y, dtype=np.float64) ** 2)))


def _db(a, b):
    return 20.0 * np.log10(a / b)


def _hold_ref(y, t_fz):
    """The held level: RMS of the first window after the ramp completes.

    Not the window BEFORE the edge -- at decay 0.5 the tail loses ~10 dB
    across a quarter second, so a pre-edge window averages well above
    the level that was actually caught."""
    return _rms(y[t_fz + RAMP: t_fz + RAMP + W])


class TestFreeze:
    def test_frozen_tail_holds_while_unfrozen_decays(self):
        n = 7 * SR
        x = _burst(n)
        lf, _ = _fz_run(_fz_rig(), x, _gate(n, T_FZ, n))
        lu, _ = _fz_run(_fz_rig(), x, np.zeros(n, np.float32))
        ref = _hold_ref(lf, T_FZ)
        assert ref > 1e-4                       # there was a tail to catch
        t5 = T_FZ + 5 * SR
        assert abs(_db(_rms(lf[t5 - W:t5]), ref)) < 3.0     # held
        assert _db(_rms(lu[t5 - W:t5]) + 1e-12, ref) < -40.0  # gone

    def test_hold_over_ten_seconds_and_the_tank_is_lossless(self):
        # The Hadamard matrix is orthonormal, so the unity loop conserves
        # the energy in circulation: the last L[i] writes of each line.
        # (The whole ring is the WRONG observable -- beyond L[i] each row
        # holds stale samples that get overwritten as the state mixes.)
        patch, src, clk, rv, b = _fz_rig()
        base = np.array(NumpyBackend._REVERB_BASE, dtype=np.float64)
        Lmax = int(base.max()) + 2
        L = np.clip(np.round(base * (0.25 + 0.75 * FZ_PARAMS["size"])).astype(np.int64), 32, Lmax - 2)

        def circulating():
            st = b._state[rv.id]
            wp = int(st["write_idx"])
            return sum(float(np.sum(st["buf"][i, (wp - 1 - np.arange(L[i])) % Lmax] ** 2))
                       for i in range(len(L)))

        n = 12 * SR
        x = _burst(n)
        fz = _gate(n, T_FZ, n)
        marks = {"ramp": T_FZ + RAMP, "+5s": T_FZ + 5 * SR, "+10s": T_FZ + 10 * SR}
        energy, ys = {}, []
        for k in range(n // F):
            sl = slice(k * F, (k + 1) * F)
            o = b._render_reverb(rv, F, {(src.id, "out"): x[sl], (clk.id, "out"): fz[sl]}, patch)
            ys.append(o["out_l"])
            for name, t in marks.items():
                if name not in energy and (k + 1) * F >= t:
                    energy[name] = circulating()
        y = np.concatenate(ys)
        assert np.all(np.isfinite(y))
        ref = _hold_ref(y, T_FZ)
        for name in ("+5s", "+10s"):
            t = marks[name]
            assert abs(_db(_rms(y[t - W:t]), ref)) < 3.0
            assert abs(10.0 * np.log10(energy[name] / energy["ramp"])) < 0.01

    def test_input_is_muted_from_the_tank_but_the_dry_path_passes(self):
        n = 6 * SR
        t_b = T_FZ + 2 * SR
        quiet = _burst(n)
        loud = quiet + _burst(n, level=0.9, seed=11, at=t_b)   # a shout mid-freeze
        fz = _gate(n, T_FZ, n)
        lq, _ = _fz_run(_fz_rig(), quiet, fz)
        lb, _ = _fz_run(_fz_rig(), loud, fz)
        ref = _hold_ref(lq, T_FZ)
        bw = slice(t_b, t_b + int(0.3 * SR))
        # wet only (mix 1): the shout never reaches the output at all ...
        assert abs(_db(_rms(lb[bw]), ref)) < 1.0
        # ... and the held level is unchanged a second later.
        t = t_b + SR
        assert abs(_db(_rms(lb[t - W:t]), ref)) < 1.0
        # mix 0.5: the shout is heard dry, on top of half the held pad.
        ld, _ = _fz_run(_fz_rig(dict(FZ_PARAMS, mix=0.5)), loud, fz)
        assert abs(_db(_rms(ld[bw]), _rms(0.5 * loud[bw]))) < 1.0
        assert abs(_db(_rms(ld[bw] - 0.5 * loud[bw]), 0.5 * ref)) < 3.0

    def test_release_resumes_decay(self):
        n = 6 * SR
        t_rel = T_FZ + 3 * SR
        lf, _ = _fz_run(_fz_rig(), _burst(n), _gate(n, T_FZ, t_rel))
        ref = _hold_ref(lf, T_FZ)
        at_release = _rms(lf[t_rel - W:t_rel])
        assert abs(_db(at_release, ref)) < 3.0          # still held at the edge
        after = _rms(lf[t_rel + SR - W:t_rel + SR])
        assert _db(after, at_release) < -20.0           # decay 0.5: ~-39 dB/s

    @pytest.mark.parametrize("kind", ["burst tail", "steady tone"])
    def test_no_click_at_either_edge(self, kind):
        # The max sample step across each edge is no larger than the
        # tail's own steps in the half second on either side. Two inputs:
        # a decaying burst tail (the spec's case), and a steady 220 Hz
        # tone held THROUGH the freeze -- the sensitive one, because a
        # pure tone's steps are small and bounded, so an abrupt mute or
        # gain step stands out (see the tripwire self-test below). White
        # noise cannot tell: its own steps are already maximal.
        n = 5 * SR
        t_rel = T_FZ + 2 * SR + 101
        x = _burst(n) if kind == "burst tail" else _tone(n)
        lf, rf = _fz_run(_fz_rig(), x, _gate(n, T_FZ, t_rel))
        for y in (lf, rf):
            for t in (T_FZ, t_rel):
                edge, own = _edge_steps(y, t, RAMP)
                assert edge <= own, (kind, t, edge, own)

    def test_the_click_tripwire_trips_on_an_abrupt_switch(self, monkeypatch):
        # Self-test: with the ramp collapsed to one sample the same
        # measurement must FAIL at every edge -- otherwise the test above
        # is not measuring the switch. (The margin is slim at one edge
        # because an abrupt step is CAPTURED by the lossless loop and
        # recirculates forever, so the "after" reference carries it too
        # -- which is the other reason the ramp exists.)
        monkeypatch.setattr(NumpyBackend, "_REVERB_FREEZE_RAMP_S", 0.0)
        n = 5 * SR
        t_rel = T_FZ + 2 * SR + 101
        lf, rf = _fz_run(_fz_rig(), _tone(n), _gate(n, T_FZ, t_rel))
        for y in (lf, rf):
            for t in (T_FZ, t_rel):
                edge, own = _edge_steps(y, t, 1)
                assert edge > own, (t, edge, own)

    def test_patched_but_low_freeze_is_bit_exact_with_unpatched(self):
        # The ship-off pin: with the gate low the hop loop runs its
        # pre-freeze code verbatim, so a patched-but-silent freeze jack
        # cannot change a sample.
        n = 3 * SR
        x = _burst(n)
        lu, ru = _fz_run(_fz_rig(patched=False), x, None)
        ll, rl = _fz_run(_fz_rig(), x, np.zeros(n, np.float32))
        assert np.array_equal(lu, ll) and np.array_equal(ru, rl)

    def test_block_size_independent_with_a_freeze_edge_mid_stream(self):
        n = 4 * SR
        x = _burst(n)
        fz = _gate(n, T_FZ, T_FZ + int(1.5 * SR) + 11)
        assert T_FZ % 64 and T_FZ % 512                 # the edge is mid-block for both
        l64, r64 = _fz_run(_fz_rig(block=64), x, fz, block=64)
        l512, r512 = _fz_run(_fz_rig(block=512), x, fz, block=512)
        m = min(len(l64), len(l512))
        assert np.array_equal(l64[:m], l512[:m])
        assert np.array_equal(r64[:m], r512[:m])

    def test_voice_gate_collapses_to_any_voice_high(self):
        # A (V, F) gate goes through the house sum: one voice high is
        # exactly the mono all-high gate (sum 1.0 > 0.5, same bool row).
        x = _burst(3 * F, seconds=3 * F / SR)
        outs = []
        for voiced in (True, False):
            patch, src, clk, rv, b = _fz_rig()
            ys = []
            for k in range(3):
                sl = slice(k * F, (k + 1) * F)
                if voiced:
                    g = np.zeros((4, F), np.float32)
                    if k == 2:
                        g[2] = 1.0
                else:
                    g = np.full(F, float(k == 2), np.float32)
                o = b._render_reverb(rv, F, {(src.id, "out"): x[sl], (clk.id, "out"): g}, patch)
                ys.append(o["out_l"])
            assert b._state[rv.id]["fz_prev"] is True
            outs.append(np.concatenate(ys))
        assert np.array_equal(outs[0], outs[1])
        # all voices low: never frozen
        patch, src, clk, rv, b = _fz_rig()
        b._render_reverb(rv, F, {(src.id, "out"): x[:F], (clk.id, "out"): np.zeros((4, F), np.float32)}, patch)
        assert b._state[rv.id]["fz_prev"] is False and b._state[rv.id]["fz_on"] == 0

    def test_finite_and_bounded_at_max_settings(self):
        n = 6 * SR
        x = _burst(n, level=1.0) + _burst(n, level=1.0, seed=5, at=T_FZ + SR)
        rig = _fz_rig({"size": 1.0, "decay": 1.0, "damping": 0.0, "mix": 1.0})
        lf, rf = _fz_run(rig, x, _gate(n, T_FZ, n))
        assert np.all(np.isfinite(lf)) and np.all(np.isfinite(rf))
        assert np.abs(lf).max() < 10.0 and np.abs(rf).max() < 10.0
        ref = _hold_ref(lf, T_FZ)
        t = T_FZ + 4 * SR
        assert abs(_db(_rms(lf[t - W:t]), ref)) < 3.0   # held, not grown

    # --- the freeze tickbox (2026-09-20 love pass) ---

    def test_tickbox_is_a_gate_edge_at_the_block_boundary(self):
        # The panel tickbox with nothing patched: ticked at block k it is
        # a rising edge at block k's first sample -- ramp and all -- and
        # cleared at block m it is a fall there. Twice over, so the second
        # hold rises from the idle state the release hands back to.
        # Bit-exact with a gate cable doing the same, and the tick at 64
        # lands on the same sample as the tick at 512.
        n = 6 * SR
        x = _burst(n)
        holds = [(90, 200), (260, 330)]                 # blocks of 512
        fz = np.zeros(n, np.float32)
        for k0, k1 in holds:
            fz[k0 * F:k1 * F] = 1.0
        cl, cr = _fz_run(_fz_rig(), x, fz)
        patch, src, clk, rv, b = rig = _fz_rig(patched=False)
        tl, tr = _fz_run(rig, x, None, tick=holds)
        assert np.array_equal(tl, cl) and np.array_equal(tr, cr)
        # it really holds: the level a second in is the level caught
        t0 = holds[0][0] * F
        t = t0 + SR
        assert _hold_ref(tl, t0) > 1e-4
        assert abs(_db(_rms(tl[t - W:t]), _hold_ref(tl, t0))) < 3.0
        # ... and once the release ran out the machinery handed back:
        # the off count stopped at the first block boundary past the ramp
        assert b._state[rv.id]["fz_prev"] is False
        assert b._state[rv.id]["fz_off"] == -(-RAMP // F) * F
        # block-size independence of the tick edge
        rig64 = _fz_rig(patched=False, block=64)
        l64, r64 = _fz_run(rig64, x, None, block=64, tick=[(8 * a, 8 * b_) for a, b_ in holds])
        m = min(len(l64), len(tl))
        assert np.array_equal(l64[:m], tl[:m]) and np.array_equal(r64[:m], tr[:m])

    def test_tickbox_is_ored_with_the_gate(self):
        # Ticked under a held cable it changes nothing; ticked with the
        # cable patched but low it holds exactly as the cable would.
        n = 4 * SR
        x = _burst(n)
        k0, k1 = 90, 200
        fz = _gate(n, k0 * F, k1 * F)
        cl, cr = _fz_run(_fz_rig(), x, fz)
        ul, ur = _fz_run(_fz_rig(), x, fz, tick=[(k0 + 20, k1 - 20)])
        ll, lr = _fz_run(_fz_rig(), x, np.zeros(n, np.float32), tick=[(k0, k1)])
        for l_, r_ in ((ul, ur), (ll, lr)):
            assert np.array_equal(l_, cl) and np.array_equal(r_, cr)

    def test_unticked_and_unpatched_never_enters_the_freeze_machinery(self):
        # The ship-off pin for the tickbox: gate unpatched and the tick
        # off (the default), the ramp state is never touched -- the hop
        # loop is the pre-freeze code by construction. (The recipe ran
        # outside the suite too: every shipped example with a reverb or
        # a delay re-rendered bit-exact against reference renders
        # captured before the tickbox existed.)
        patch, src, clk, rv, b = rig = _fz_rig(patched=False)
        assert rv.params["freeze"] is False
        _fz_run(rig, _burst(2 * SR), None)
        st = b._state[rv.id]
        assert st["fz_prev"] is False and st["fz_on"] == 0
        assert st["fz_off"] == 0 and st["fz_env"] == 0.0
        # The tripwire can tell: a patched-but-low cable does walk the
        # ramp (its off count grows), the unpatched module's never moves.
        patch, src, clk, rv, b = rig = _fz_rig()
        _fz_run(rig, _burst(2 * SR), np.zeros(2 * SR, np.float32))
        assert b._state[rv.id]["fz_off"] > 0


# ----- UI --------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("reverb")
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
    for name in get_module_type("reverb").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert "lvl/unit" in w["cv_depth"][1]
    # the freeze tickbox: a labelled checkbox beside the gate jack
    assert w["freeze (or gate)"][0] == "add_checkbox"


# ----- example ---------------------------------------------------------------


def test_the_freeze_pad_example_hangs_a_pad():
    from pysynthrack.io_patch import load_patch

    np.random.seed(0)   # the plucks' excitation is unseeded
    path = Path(__file__).resolve().parent.parent / "examples" / "reverb_freeze_pad.json"
    patch = load_patch(path)
    assert len(patch.modules) <= 12
    rv = next(m for m in patch if m.TYPE == "reverb")
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    outs, frozen = [], []
    for _ in range(int(SR * 7 / F)):
        out, _devices = b.render_block_multi(F)
        assert out is not None and np.all(np.isfinite(out))
        outs.append(np.asarray(out).copy())
        frozen.append(bool(b._state[rv.id]["fz_prev"]))
    y = np.concatenate(outs, axis=0)
    assert 0.3 < float(np.abs(y).max()) < 0.8
    # The strike clock is high for the first 0.72 s of every 6 s and the
    # logic NOT of it is the freeze: open during the strike, held between.
    at = lambda sec: frozen[int(sec * SR) // F]   # noqa: E731
    assert not at(0.3) and at(2.0) and at(5.5) and not at(6.2)
    # The pad hangs: RMS at 5 s within 3 dB of RMS at 2 s (the dry line
    # rides on top in both windows).
    r2 = _rms(y[int(1.75 * SR):int(2.0 * SR)])
    r5 = _rms(y[int(4.75 * SR):int(5.0 * SR)])
    assert abs(_db(r5, r2)) < 3.0
