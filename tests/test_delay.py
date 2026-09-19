"""Tests for the Delay (analog-voiced feedback echo).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in, time_cv
    in, audio out), JSON round-trip, unknown-param rejection, type walls.
  - DSP: disconnected -> silence; mix=0 is a bit-exact passthrough; a
    single tap lands exactly `time` samples late; feedback gives decaying
    repeats; the feedback is clamped so runaway settings stay bounded; the
    tone knob damps the repeats (dark < bright high-frequency tail);
    block-spanning echoes are continuous across blocks.
  - Paths: the vectorized fast path (delay >= block) and the per-sample
    path (delay < block) agree bit-for-bit.
  - Voice: a single-voice row is bit-identical to mono; voices echo
    independently via per-voice time_cv.
  - time_cv: positive CV lengthens the delay; cv_depth scales it.
  - Integration: osc -> delay -> speaker renders audible, finite audio.
  - Freeze (2026-09-19 love pass): the ``freeze`` gate hangs the echo --
    the loop holds (RMS at +5 s and +10 s within 3 dB, the loop's energy
    conserved to 1e-9 dB: the read snaps to whole samples while held, so
    the unity loop is ``buf[n] = buf[n - D]`` bit for bit) while the
    unfrozen tail is gone; the held time is round(time) samples and a
    moving time_cv does not move it; the input is muted from the line
    (a shout mid-freeze leaves the held loop alone) but the dry path
    passes; release resumes decay; no click at either edge on a steady
    tone (steps bounded by the tone's own plus the ramp's slew, and the
    tripwire trips on an abrupt switch); a re-rise inside the release
    keeps the previous hold; patched-but-low is bit-exact with unpatched
    (the ship-off pin); block-size independent with the edge mid-stream
    (fast-only and mixed paths); a (V, F) gate collapses to any-voice-
    high; finite and never growing at runaway settings.
  - UI: every param still gets a bounded widget (the gate adds none).
  - Example: delay_freeze_stutter.json renders in range and stutters.
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
from pysynthrack.modules.delay import Delay

SR = 44100
F = 512


def _rig(params=None, with_cv=False, block=F):
    """oscillator -> delay (optionally constant -> time_cv), compiled."""
    patch = Patch()
    src = patch.add_module("oscillator")
    dl = patch.add_module("delay", params=params or {})
    patch.connect(src.id, "out", dl.id, "in")
    cvsrc = None
    if with_cv:
        cvsrc = patch.add_module("constant")
        patch.connect(cvsrc.id, "out", dl.id, "time_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, dl, cvsrc, b


def _run(b, patch, src, dl, signal, cvsrc=None, cv=None, block=F):
    """Render ``signal`` (1D or (V,F)) through the delay, block by block."""
    n = (signal.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        if cvsrc is not None and cv is not None:
            bufs[(cvsrc.id, "out")] = cv[..., sl].astype(np.float32)
        outs.append(b._render_delay(dl, block, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _impulse(n, at=0):
    x = np.zeros(n, dtype=np.float32)
    x[at] = 1.0
    return x


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        dl = Patch().add_module("delay")
        assert isinstance(dl, Delay)
        assert dl.params == {
            "time": 300.0,
            "feedback": 0.4,
            "tone": 0.5,
            "mix": 0.35,
            "cv_depth": 50.0,
        }

    def test_ports_and_signal_kinds(self):
        dl = Patch().add_module("delay")
        assert [(p.name, p.signal_kind) for p in dl.input_ports] == [
            ("in", "audio"),
            ("time_cv", "cv"),
            ("freeze", "gate"),
        ]
        assert [(p.name, p.signal_kind) for p in dl.output_ports] == [
            ("out", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("delay", params={"time": 125.0, "feedback": 0.7})
        restored = Patch.from_dict(patch.to_dict())
        dl = next(m for m in restored if m.TYPE == "delay")
        assert dl.params["time"] == 125.0
        assert dl.params["feedback"] == 0.7

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("delay", params={"wet": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        dl = patch.add_module("delay")
        patch.connect(osc.id, "out", dl.id, "in")

    def test_cv_into_time_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        dl = patch.add_module("delay")
        patch.connect(lfo.id, "cv", dl.id, "time_cv")

    def test_cv_into_audio_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        dl = patch.add_module("delay")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", dl.id, "in")

    def test_audio_into_time_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        dl = patch.add_module("delay")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", dl.id, "time_cv")

    def test_gate_into_freeze_accepted_cv_rejected(self):
        patch = Patch()
        clk = patch.add_module("clock")
        lfo = patch.add_module("lfo")
        dl = patch.add_module("delay")
        patch.connect(clk.id, "out", dl.id, "freeze")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", dl.id, "freeze")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        dl = patch.add_module("delay")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(dl.id, "out", vca.id, "cv")


# ----- Mono DSP --------------------------------------------------------------


class TestMonoDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        dl = patch.add_module("delay")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        out = b._render_delay(dl, F, {}, patch)
        assert out.shape == (F,)
        assert not np.any(out)

    def test_frames_zero_empty(self):
        patch, src, dl, _, b = _rig()
        out = b._render_delay_core(dl, 0, np.zeros((1, 0), np.float32), None)
        assert out.shape == (1, 0)

    def test_mix_zero_is_exact_passthrough(self):
        # Both paths: long delay (fast) and short delay (per-sample).
        for block, time_ms in ((128, 50.0), (700, 2.0)):
            patch, src, dl, _, b = _rig(
                {"mix": 0.0, "feedback": 0.8, "time": time_ms}, block=block
            )
            x = np.random.randn(block * 2).astype(np.float32)
            out = _run(b, patch, src, dl, x, block=block)
            assert np.array_equal(out, x[: len(out)])

    def test_single_tap_timing(self):
        # feedback 0, mix 1 -> one clean echo exactly `time` samples late.
        T = 256  # < block -> per-sample path
        time_ms = T / SR * 1000.0
        patch, src, dl, _, b = _rig(
            {"feedback": 0.0, "mix": 1.0, "tone": 1.0, "time": time_ms}
        )
        out = _run(b, patch, src, dl, _impulse(F))
        assert int(np.argmax(np.abs(out))) == T
        assert out[T] == pytest.approx(1.0, abs=1e-3)
        assert np.max(np.abs(out[:T])) < 1e-6  # no pre-echo

    def test_tap_timing_spans_blocks(self):
        # Delay longer than a block -> echo appears in a later block.
        T = 700  # > block(512) -> fast path, lands in block 2
        time_ms = T / SR * 1000.0
        patch, src, dl, _, b = _rig(
            {"feedback": 0.0, "mix": 1.0, "tone": 1.0, "time": time_ms}
        )
        out = _run(b, patch, src, dl, _impulse(F * 3))
        assert int(np.argmax(np.abs(out))) == T
        assert out[T] == pytest.approx(1.0, abs=1e-3)

    def test_feedback_decaying_repeats(self):
        T = 300
        time_ms = T / SR * 1000.0
        fb = 0.5
        patch, src, dl, _, b = _rig(
            {"feedback": fb, "mix": 1.0, "tone": 1.0, "time": time_ms}
        )
        out = _run(b, patch, src, dl, _impulse(F * 4))
        taps = [out[k * T] for k in range(1, 4)]
        # Strictly decreasing, each no larger than the feedback fraction of
        # the previous (damping only ever removes energy).
        assert taps[0] > taps[1] > taps[2] > 0
        for prev, cur in zip(taps, taps[1:]):
            assert 0.0 < cur <= prev * fb + 1e-6

    def test_runaway_feedback_is_bounded(self):
        # Absurd feedback must be clamped -> output stays finite/bounded.
        patch, src, dl, _, b = _rig(
            {"feedback": 9.0, "mix": 0.5, "time": 5.0}
        )
        x = np.random.randn(SR // 4).astype(np.float32)
        out = _run(b, patch, src, dl, x, block=256)
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) < 50.0

    def test_tone_damps_repeats(self):
        # An alternating (high-frequency) signal: the dark tone should bleed
        # far less high-frequency energy into the tail than the bright one.
        T = 200
        time_ms = T / SR * 1000.0
        nyq = np.tile([1.0, -1.0], F * 2).astype(np.float32)
        common = {"feedback": 0.7, "mix": 1.0, "time": time_ms}
        p1, s1, d1, _, b1 = _rig({**common, "tone": 0.95})
        p2, s2, d2, _, b2 = _rig({**common, "tone": 0.05})
        bright = _run(b1, p1, s1, d1, nyq.copy())
        dark = _run(b2, p2, s2, d2, nyq.copy())
        tail = slice(3 * T, None)
        assert np.sum(dark[tail] ** 2) < 0.6 * np.sum(bright[tail] ** 2)


# ----- Path equivalence ------------------------------------------------------


class TestPaths:
    def test_fast_and_per_sample_paths_agree(self):
        # Same delay, same input: render once in a single big block (delay <
        # block -> per-sample) and once in small blocks (delay >= block ->
        # vectorized). The two must match bit-for-bit.
        x = np.random.randn(1800).astype(np.float32)
        T = 300
        time_ms = T / SR * 1000.0
        params = {"time": time_ms, "feedback": 0.6, "tone": 0.4, "mix": 0.5}
        pa, sa, da, _, ba = _rig(params, block=1800)   # per-sample
        per_sample = _run(ba, pa, sa, da, x, block=1800)
        pb, sb, db, _, bb = _rig(params, block=100)    # fast path
        fast = _run(bb, pb, sb, db, x, block=100)
        assert np.array_equal(per_sample, fast)


# ----- Voice DSP -------------------------------------------------------------


class TestVoiceDSP:
    def test_single_voice_row_matches_mono(self):
        patch, src, dl, _, b = _rig({"feedback": 0.6, "time": 4.0})
        x = np.random.randn(F).astype(np.float32)
        mono = b._render_delay_core(dl, F, x[np.newaxis, :], None)
        # fresh state for the voice render
        b2 = NumpyBackend(sample_rate=SR, block_size=F)
        b2.compile(patch)
        voice = b2._render_delay_core(
            dl, F, np.stack([x, x]).astype(np.float32), None
        )
        assert np.array_equal(voice[0], mono[0])
        assert np.array_equal(voice[0], voice[1])

    def test_voices_echo_independently_via_cv(self):
        # Two voices, different per-voice time_cv -> echoes at different
        # places. Impulse in each voice, feedback 0, mix 1.
        V = 2
        n = F * 3
        x = np.zeros((V, n), dtype=np.float32)
        x[:, 0] = 1.0
        cv = np.zeros((V, n), dtype=np.float32)
        cv[0] = 0.0
        cv[1] = 1.0  # voice 1 gets +cv_depth ms of extra delay
        patch = Patch()
        src = patch.add_module("oscillator")
        cvs = patch.add_module("constant")
        dl = patch.add_module(
            "delay",
            params={"feedback": 0.0, "mix": 1.0, "tone": 1.0,
                    "time": 200 / SR * 1000.0, "cv_depth": 300 / SR * 1000.0},
        )
        patch.connect(src.id, "out", dl.id, "in")
        patch.connect(cvs.id, "out", dl.id, "time_cv")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        outs = []
        for k in range(n // F):
            sl = slice(k * F, (k + 1) * F)
            bufs = {(src.id, "out"): x[:, sl], (cvs.id, "out"): cv[:, sl]}
            outs.append(b._render_delay(dl, F, bufs, patch))
        out = np.concatenate(outs, axis=-1)
        assert out.ndim == 2 and out.shape[0] == V
        peak0 = int(np.argmax(np.abs(out[0])))
        peak1 = int(np.argmax(np.abs(out[1])))
        assert peak0 == 200
        assert peak1 == 500  # 200 base + 300 from cv


# ----- time_cv ---------------------------------------------------------------


class TestTimeCV:
    def test_positive_cv_lengthens_delay(self):
        n = F * 3
        base_T = 250
        patch, src, dl, cvs, b = _rig(
            {"feedback": 0.0, "mix": 1.0, "tone": 1.0,
             "time": base_T / SR * 1000.0, "cv_depth": 200 / SR * 1000.0},
            with_cv=True,
        )
        imp = _impulse(n)
        cv = np.ones(n, dtype=np.float32)  # +1 unit -> +200 samples
        out = _run(b, patch, src, dl, imp, cvsrc=cvs, cv=cv)
        assert int(np.argmax(np.abs(out))) == base_T + 200


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_delay_speaker_makes_sound(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        dl = patch.add_module("delay", params={"mix": 0.5, "feedback": 0.4})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", dl.id, "in")
        patch.connect(dl.id, "out", spk.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(40):  # clear the delay's priming latency
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0


# ----- Freeze ----------------------------------------------------------------

FZ_PARAMS = {"time": 300.37, "feedback": 0.5, "tone": 0.5, "mix": 1.0}
RAMP = int(round(NumpyBackend._DELAY_FREEZE_RAMP_S * SR))
T_FZ = SR + 37          # the freeze edge: mid-block at 64 and at 512
W = int(0.25 * SR)      # RMS window


def _fz_rig(params=None, block=F, patched=True, with_cv=False):
    patch = Patch()
    src = patch.add_module("oscillator")
    clk = patch.add_module("clock")
    dl = patch.add_module("delay", params=dict(FZ_PARAMS if params is None else params))
    patch.connect(src.id, "out", dl.id, "in")
    if patched:
        patch.connect(clk.id, "out", dl.id, "freeze")
    cvs = None
    if with_cv:
        cvs = patch.add_module("constant")
        patch.connect(cvs.id, "out", dl.id, "time_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, clk, dl, b, cvs


def _fz_run(rig, x, fz, block=F, cv=None):
    """Drive the renderer block by block with an audio row and a gate row
    (``fz`` None = the freeze buffer is never published)."""
    patch, src, clk, dl, b, cvs = rig
    n = (x.shape[-1] // block) * block
    ys = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): x[..., sl].astype(np.float32)}
        if fz is not None:
            bufs[(clk.id, "out")] = fz[..., sl].astype(np.float32)
        if cv is not None:
            bufs[(cvs.id, "out")] = cv[..., sl].astype(np.float32)
        ys.append(b._render_delay(dl, block, bufs, patch))
    return np.concatenate(ys, axis=-1)


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


def _rms(y):
    return float(np.sqrt(np.mean(np.asarray(y, dtype=np.float64) ** 2)))


def _db(a, b):
    return 20.0 * np.log10(a / b)


def _hold_ref(y, t_fz):
    """The held level: RMS of the first window after the ramp completes."""
    return _rms(y[t_fz + RAMP: t_fz + RAMP + W])


def _held_samples(params=FZ_PARAMS):
    return int(round(params["time"] * SR / 1000.0))


def _edge_steps(y, t, ramp_n, D):
    """(max step across the edge, the signal's own max step in the half
    second BEFORE it, the same AFTER the window, its peak before it).

    The freeze changes what the line WRITES and the read shows a write
    ``D`` samples later, so the window runs from the edge to
    ``t + D + 2 ramp``. Which reference is honest differs per edge: at
    the RISE only the pre-edge one -- a lossless loop CAPTURES whatever
    the ramp wrote and repeats it every lap, so the post-window would
    carry the very step under test and an abrupt switch would pass by
    construction; at the FALL the larger of the two -- the level
    legitimately changes as the loop reopens and the input re-enters,
    and a captured click only comes back at ``feedback`` times its size,
    so it still trips."""
    d = np.abs(np.diff(y.astype(np.float64)))
    end = t + D + 2 * ramp_n
    edge = float(d[t - ramp_n:end].max())
    pre = slice(max(0, t - SR // 2), t - ramp_n)
    own_pre = float(d[pre].max())
    own_post = float(d[end:end + SR // 2].max())
    peak = float(np.abs(y[pre]).max())
    return edge, own_pre, own_post, peak


def _click_bounds(y, t_fz, t_rel, ramp_n, D):
    """The two edges' (max step, allowed step): the reference per edge as
    above, plus what a RAMP-sample linear crossfade between two signals
    of the pre-edge peak can add on its own (2 peak / RAMP -- the ramp's
    slew; ~0.2 % of a 220 Hz tone's step, against the 10x of an abrupt
    switch). The allowance uses the REAL ramp even when a tripwire has
    collapsed the rendered one."""
    edge_r, pre_r, _post_r, peak_r = _edge_steps(y, t_fz, ramp_n, D)
    edge_f, pre_f, post_f, peak_f = _edge_steps(y, t_rel, ramp_n, D)
    return (
        (edge_r, pre_r + 2.0 * peak_r / RAMP),
        (edge_f, max(pre_f, post_f) + 2.0 * peak_f / RAMP),
    )


def _loop_energy(b, dl, D):
    """Energy in the last D writes of every voice's line -- what a unity
    loop of D samples recirculates. (The rest of the ring is stale.)"""
    st = b._state[dl.id]
    wp = int(st["write_idx"])
    buf = st["buf"]
    idx = (wp - 1 - np.arange(D)) % buf.shape[1]
    return float(np.sum(buf[:, idx] ** 2))


class TestFreeze:
    def test_frozen_echo_holds_while_unfrozen_decays(self):
        n = 7 * SR
        x = _burst(n)
        yf = _fz_run(_fz_rig(), x, _gate(n, T_FZ, n))
        yu = _fz_run(_fz_rig(), x, np.zeros(n, np.float32))
        ref = _hold_ref(yf, T_FZ)
        assert ref > 1e-4                       # there was an echo to catch
        t5 = T_FZ + 5 * SR
        assert abs(_db(_rms(yf[t5 - W:t5]), ref)) < 3.0     # held
        assert _db(_rms(yu[t5 - W:t5]) + 1e-12, ref) < -40.0  # gone

    @pytest.mark.parametrize("time_ms", [300.0, 300.37, 181.3])
    def test_hold_over_ten_seconds_and_the_loop_is_lossless(self, time_ms):
        # Whole-sample AND fractional times: while held the read snaps to
        # round(time) samples, so the unity loop is buf[n] = buf[n - D]
        # bit for bit and the energy in circulation is conserved exactly.
        # (A fractional linear-interp read would lose 9-11 dB in 10 s --
        # measured on a bright burst for fractions 0.1 .. 0.5 -- which is
        # why the snap exists.)
        params = dict(FZ_PARAMS, time=time_ms)
        D = _held_samples(params)
        patch, src, clk, dl, b, _ = rig = _fz_rig(params)
        n = 12 * SR
        x = _burst(n)
        fz = _gate(n, T_FZ, n)
        marks = {"ramp": T_FZ + RAMP, "+5s": T_FZ + 5 * SR, "+10s": T_FZ + 10 * SR}
        energy, ys = {}, []
        for k in range(n // F):
            sl = slice(k * F, (k + 1) * F)
            ys.append(b._render_delay(
                dl, F, {(src.id, "out"): x[sl], (clk.id, "out"): fz[sl]}, patch))
            for name, t in marks.items():
                if name not in energy and (k + 1) * F >= t:
                    energy[name] = _loop_energy(b, dl, D)
        y = np.concatenate(ys)
        assert np.all(np.isfinite(y))
        assert float(b._state[dl.id]["fz_dly"][0]) == D
        ref = _hold_ref(y, T_FZ)
        for name in ("+5s", "+10s"):
            t = marks[name]
            assert abs(_db(_rms(y[t - W:t]), ref)) < 3.0
            assert abs(10.0 * np.log10(energy[name] / energy["ramp"])) < 1e-9

    def test_held_time_is_round_time_and_time_cv_does_not_move_it(self):
        # An impulse echoing at 200 ms (8820 samples) gets frozen; then the
        # time_cv sweeps a full unit (+100 ms). The held repeats stay
        # exactly 8820 apart -- the read is pinned to the latched whole
        # number of samples, and the CV takes effect again only on release.
        params = {"time": 200.0, "feedback": 0.3, "tone": 1.0, "mix": 1.0, "cv_depth": 100.0}
        D = _held_samples(params)
        rig = _fz_rig(params, with_cv=True)
        n = 4 * SR
        x = np.zeros(n, np.float32)
        x[100] = 1.0
        t_fz = SR // 2 + 37
        cv = np.zeros(n, np.float32)
        cv[SR:] = np.linspace(0.0, 1.0, n - SR)         # sweeps AFTER the freeze
        y = _fz_run(rig, x, _gate(n, t_fz, n), cv=cv)
        assert float(rig[4]._state[rig[3].id]["fz_dly"][0]) == D
        peaks = np.flatnonzero(np.abs(y) > 0.05)
        gaps = np.diff(peaks)
        assert len(gaps) >= 12
        assert np.all(gaps == D)
        # ... whereas with the freeze low the CV DOES move the echo: a +1
        # step after the first repeat jumps the read and the repeats are
        # no longer D apart.
        cv2 = np.zeros(n, np.float32)
        cv2[int(0.3 * SR):] = 1.0
        yu = _fz_run(_fz_rig(params, with_cv=True), x, np.zeros(n, np.float32), cv=cv2)
        gu = np.diff(np.flatnonzero(np.abs(yu) > 0.05))
        assert len(gu) >= 2 and not np.all(gu == D)

    def test_input_is_muted_from_the_line_but_the_dry_path_passes(self):
        n = 6 * SR
        t_b = T_FZ + 2 * SR
        quiet = _burst(n)
        loud = quiet + _burst(n, level=0.9, seed=11, at=t_b)   # a shout mid-freeze
        fz = _gate(n, T_FZ, n)
        yq = _fz_run(_fz_rig(), quiet, fz)
        yb = _fz_run(_fz_rig(), loud, fz)
        ref = _hold_ref(yq, T_FZ)
        bw = slice(t_b, t_b + int(0.3 * SR))
        # wet only (mix 1): the shout never reaches the output at all ...
        assert abs(_db(_rms(yb[bw]), ref)) < 1.0
        # ... and the held level is unchanged a second later.
        t = t_b + SR
        assert abs(_db(_rms(yb[t - W:t]), ref)) < 1.0
        # mix 0.5: the shout is heard dry, on top of half the held echo.
        yd = _fz_run(_fz_rig(dict(FZ_PARAMS, mix=0.5)), loud, fz)
        assert abs(_db(_rms(yd[bw]), _rms(0.5 * loud[bw]))) < 1.0
        assert abs(_db(_rms(yd[bw] - 0.5 * loud[bw]), 0.5 * ref)) < 3.0

    def test_release_resumes_decay(self):
        n = 6 * SR
        t_rel = T_FZ + 3 * SR
        yf = _fz_run(_fz_rig(), _burst(n), _gate(n, T_FZ, t_rel))
        ref = _hold_ref(yf, T_FZ)
        at_release = _rms(yf[t_rel - W:t_rel])
        assert abs(_db(at_release, ref)) < 3.0          # still held at the edge
        after = _rms(yf[t_rel + SR - W:t_rel + SR])
        assert _db(after, at_release) < -10.0           # feedback 0.5 at 300 ms: ~-16 dB/s

    @pytest.mark.parametrize("kind", ["steady tone", "burst tail"])
    @pytest.mark.parametrize("mix", [1.0, 0.5])
    def test_no_click_at_either_edge(self, kind, mix):
        # The max sample step across each edge is no larger than the
        # signal's own steps plus the ramp's own slew (``_click_bounds``;
        # the smooth edges land at 0.67-0.93 of the bound, an abrupt
        # switch at 8-10x it -- see the tripwire). A steady 220 Hz tone
        # held THROUGH the freeze is the sensitive case: its steps are
        # small and bounded; white noise cannot tell. The fractional time
        # (300.37 ms) also exercises the read's slide to whole samples.
        n = 5 * SR
        t_rel = T_FZ + 2 * SR + 101
        x = _tone(n) if kind == "steady tone" else _burst(n)
        y = _fz_run(_fz_rig(dict(FZ_PARAMS, mix=mix)), x, _gate(n, T_FZ, t_rel))
        for edge, bound in _click_bounds(y, T_FZ, t_rel, RAMP, _held_samples() + 1):
            assert edge <= bound, (kind, mix, edge, bound)

    def test_the_click_tripwire_trips_on_an_abrupt_switch(self, monkeypatch):
        # Self-test: with the ramp collapsed to one sample the SAME bounds
        # must be exceeded severalfold -- otherwise the test above is not
        # measuring the switch. Two runs, because an abrupt rise is
        # captured by the lossless loop and would pollute the fall's own
        # reference: (A) abrupt rise, gate held; (B) a smooth rise, then
        # the ramp collapsed just before the fall (the constant is read
        # every block).
        n = 5 * SR
        t_rel = T_FZ + 2 * SR + 101
        D = _held_samples() + 1
        monkeypatch.setattr(NumpyBackend, "_DELAY_FREEZE_RAMP_S", 0.0)
        y = _fz_run(_fz_rig(), _tone(n), _gate(n, T_FZ, n))
        (edge, bound), _ = _click_bounds(y, T_FZ, t_rel, 1, D)
        assert edge > 4.0 * bound, ("rise", edge, bound)
        monkeypatch.setattr(NumpyBackend, "_DELAY_FREEZE_RAMP_S", 0.010)
        patch, src, clk, dl, b, _ = _fz_rig()
        x, fz, ys = _tone(n), _gate(n, T_FZ, t_rel), []
        for k in range(n // F):
            if k * F >= t_rel - 2 * F:
                monkeypatch.setattr(NumpyBackend, "_DELAY_FREEZE_RAMP_S", 0.0)
            sl = slice(k * F, (k + 1) * F)
            ys.append(b._render_delay(
                dl, F, {(src.id, "out"): x[sl], (clk.id, "out"): fz[sl]}, patch))
        _, (edge, bound) = _click_bounds(np.concatenate(ys), T_FZ, t_rel, 1, D)
        assert edge > 4.0 * bound, ("fall", edge, bound)

    def test_a_re_rise_inside_the_release_keeps_the_previous_hold(self):
        # The gate falls and rises again 3 ms later (inside the 10 ms
        # release) with time_cv moved in between: the held delay stays
        # the one first latched -- the read position never jumps mid-
        # blend -- and the echo carries on at that spacing.
        params = {"time": 200.0, "feedback": 0.3, "tone": 1.0, "mix": 1.0, "cv_depth": 100.0}
        D = _held_samples(params)
        rig = _fz_rig(params, with_cv=True)
        n = 4 * SR
        x = np.zeros(n, np.float32)
        x[100] = 1.0
        t_fz = SR // 2 + 37
        t_dip = t_fz + SR
        fz = _gate(n, t_fz, n)
        fz[t_dip:t_dip + int(0.003 * SR)] = 0.0
        cv = np.zeros(n, np.float32)
        cv[t_dip:] = 0.5                                # +50 ms, during the dip
        y = _fz_run(rig, x, fz, cv=cv)
        st = rig[4]._state[rig[3].id]
        assert float(st["fz_dly"][0]) == D
        gaps = np.diff(np.flatnonzero(np.abs(y) > 0.05))
        assert np.all(gaps == D)
        # ... whereas a rise from FULLY released re-latches the new time.
        rig2 = _fz_rig(params, with_cv=True)
        fz2 = _gate(n, t_fz, n)
        fz2[t_dip:t_dip + 3 * RAMP] = 0.0
        y2 = _fz_run(rig2, x, fz2, cv=cv)
        assert float(rig2[4]._state[rig2[3].id]["fz_dly"][0]) == D + int(round(50.0 * SR / 1000.0))
        assert np.all(np.isfinite(y2))

    def test_patched_but_low_freeze_is_bit_exact_with_unpatched(self):
        # The ship-off pin: with the gate low both paths run their
        # pre-freeze code verbatim, so a patched-but-silent freeze jack
        # cannot change a sample. Long delay (fast path) and short delay
        # (per-sample path), with a wobbling time_cv on each.
        n = 3 * SR
        x = _burst(n) + _tone(n, 330.0, 0.1)
        cv = (0.3 * np.sin(2 * np.pi * 0.5 * np.arange(n) / SR)).astype(np.float32)
        for params in (FZ_PARAMS, dict(FZ_PARAMS, time=3.0, cv_depth=1.0)):
            yu = _fz_run(_fz_rig(params, patched=False, with_cv=True), x, None, cv=cv)
            yl = _fz_run(_fz_rig(params, with_cv=True), x, np.zeros(n, np.float32), cv=cv)
            assert np.array_equal(yu, yl)

    @pytest.mark.parametrize("time_ms", [300.37, 200 / SR * 1000.0])
    def test_block_size_independent_with_a_freeze_edge_mid_stream(self, time_ms):
        # 300 ms: the fast path at both block sizes. 200 samples: the
        # per-sample path at 512 and the fast path at 64 -- the two must
        # still agree with the freeze ramping, holding and releasing.
        params = dict(FZ_PARAMS, time=time_ms)
        n = 4 * SR
        x = _burst(n)
        fz = _gate(n, T_FZ, T_FZ + int(1.5 * SR) + 11)
        assert T_FZ % 64 and T_FZ % 512                 # the edge is mid-block for both
        y64 = _fz_run(_fz_rig(params, block=64), x, fz, block=64)
        y512 = _fz_run(_fz_rig(params, block=512), x, fz, block=512)
        m = min(len(y64), len(y512))
        assert np.array_equal(y64[:m], y512[:m])

    def test_voice_gate_collapses_to_any_voice_high(self):
        # A (V, F) gate goes through the house sum: one voice high is
        # exactly the mono all-high gate (sum 1.0 > 0.5, same bool row),
        # and the one row freezes every voice's line.
        V = 3
        x = np.stack([_burst(3 * F, seconds=3 * F / SR, seed=s) for s in range(V)])
        outs = []
        for voiced in (True, False):
            patch, src, clk, dl, b, _ = _fz_rig(dict(FZ_PARAMS, time=4.0))
            ys = []
            for k in range(3):
                sl = slice(k * F, (k + 1) * F)
                if voiced:
                    g = np.zeros((4, F), np.float32)
                    if k == 2:
                        g[2] = 1.0
                else:
                    g = np.full(F, float(k == 2), np.float32)
                ys.append(b._render_delay(
                    dl, F, {(src.id, "out"): x[:, sl], (clk.id, "out"): g}, patch))
            assert b._state[dl.id]["fz_prev"] is True
            assert b._state[dl.id]["fz_dly"].shape == (V,)
            outs.append(np.concatenate(ys, axis=-1))
        assert outs[0].shape == (V, 3 * F)
        assert np.array_equal(outs[0], outs[1])
        # all voices low: never frozen
        patch, src, clk, dl, b, _ = _fz_rig(dict(FZ_PARAMS, time=4.0))
        b._render_delay(dl, F, {(src.id, "out"): x[:, :F],
                                (clk.id, "out"): np.zeros((4, F), np.float32)}, patch)
        assert b._state[dl.id]["fz_prev"] is False and b._state[dl.id]["fz_on"] == 0

    def test_finite_and_bounded_at_runaway_settings(self):
        n = 6 * SR
        x = _burst(n, level=1.0) + _burst(n, level=1.0, seed=5, at=T_FZ + SR)
        rig = _fz_rig({"time": 300.37, "feedback": 9.0, "tone": 0.0, "mix": 1.0})
        yf = _fz_run(rig, x, _gate(n, T_FZ, n))
        assert np.all(np.isfinite(yf))
        assert np.abs(yf).max() < 50.0
        ref = _hold_ref(yf, T_FZ)
        t = T_FZ + 4 * SR
        assert abs(_db(_rms(yf[t - W:t]), ref)) < 3.0   # held, not grown


# ----- UI --------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("delay")
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
    for name in get_module_type("delay").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["time"][1].endswith(" ms")
    assert "ms/unit" in w["cv_depth"][1]


# ----- example ---------------------------------------------------------------


def test_the_freeze_stutter_example_stutters():
    from pysynthrack.io_patch import load_patch

    np.random.seed(0)
    path = Path(__file__).resolve().parent.parent / "examples" / "delay_freeze_stutter.json"
    patch = load_patch(path)
    assert len(patch.modules) <= 12
    dl = next(m for m in patch if m.TYPE == "delay")
    D = int(round(dl.params["time"] * SR / 1000.0))        # 187.5 ms -> 8269
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    outs, frozen = [], []
    for _ in range(int(SR * 8 / F)):
        out, _devices = b.render_block_multi(F)
        assert out is not None and np.all(np.isfinite(out))
        outs.append(np.asarray(out).copy())
        frozen.append(bool(b._state[dl.id]["fz_prev"]))
    y = np.concatenate(outs, axis=0)
    assert 0.3 < float(np.abs(y).max()) < 0.8
    # The stutter clock is low for the first 2 s of every 4 s and the
    # logic NOT of it is the freeze: playing, then held, then playing.
    at = lambda sec: frozen[int(sec * SR) // F]   # noqa: E731
    assert not at(1.0) and at(3.0) and not at(5.0) and at(7.0)
    assert float(b._state[dl.id]["fz_dly"][0]) == D
    # It stutters: while held the echo is D-periodic and never decays, so
    # the output correlates with itself at lag k*D for many laps; the
    # ordinary echo (feedback 0.35, ~-9 dB a lap) has forgotten by lap 3.
    mono = y[:, 0].astype(np.float64)

    def corr(t0, lag):
        return float(np.corrcoef(mono[t0:t0 + D], mono[t0 + lag:t0 + lag + D])[0, 1])

    hold = [corr(int(2.1 * SR), k * D) for k in range(3, 9)]
    free = [corr(int(4.1 * SR), k * D) for k in range(3, 9)]
    assert min(hold) > 0.25, hold
    assert max(free) < 0.15, free
