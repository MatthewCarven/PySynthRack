"""Tests for the Resampler (varispeed pitch shifter).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in,
    pitch_cv in, audio out), JSON round-trip, unknown-param rejection,
    type walls (audio->in legal, cv->pitch_cv legal, cv->in illegal,
    audio->pitch_cv illegal, audio out->cv sink illegal).
  - Mono DSP: disconnected -> silence; unity (0 st, no glide) is a
    bit-exact delayed passthrough; octave up doubles / octave down
    halves the pitch; cents == semitones; CV summed in semitone space
    (cv*depth equivalent to the same semitones); finite + bounded on
    sustained input at extreme settings; glide ramps through
    intermediate pitches.
  - Voice DSP: a single-voice row is bit-identical to mono; voices
    transpose independently via per-voice CV; mono<->voice state reinit.
  - Loop-seam declick: sustained up/down shifts fire half-span head
    jumps (observable via the seam_jumps state counter) and the output
    stays click-free (sample-to-sample step bounded, no RMS dropout);
    unity never jumps; extreme ratios stay finite with many jumps; a
    fade spanning multiple small blocks is still click-free; a single
    voice row through seams is bit-identical to mono; voices jump
    independently.
  - Dry/wet mix: registered default 1.0; mix=0 is the delayed dry
    passthrough (bit-equal to the unity render) even when pitched;
    mix=0.5 at unity is coherent (bit-equal to full wet -- dry and wet
    taps are sample-aligned); mix=0.5 pitched shows both spectral
    peaks; out-of-range mix clamps; voice row bit-identical to mono.
  - Window param: default 200 ms reproduces the legacy fixed window
    (bit-identical render, legacy half-window latency); latency is half
    the window and scales with it; the ring floors at 4 blocks;
    out-of-range values clamp to 20..2000 ms bit-exactly; a tight
    window still crossfades its (more frequent) seams; growing or
    modestly shrinking the window mid-stream preserves the recent tail
    (unity passthrough continues bit-exactly across the change); a hard
    shrink below the head's lag recovers finite and audible; a window
    change preserves the seam_jumps observable; single voice row
    matches mono at non-default windows; JSON round-trip.
  - Integration: osc -> resampler -> speaker renders audible audio; an
    LFO into pitch_cv gives vibrato.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend, _hermite4
from pysynthrack.core import Patch
from pysynthrack.modules.resampler import Resampler

SR = 44100
F = 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(params=None, with_cv=False, block=F):
    """oscillator -> resampler (optionally lfo -> pitch_cv), compiled.

    Returns (patch, src, rs, cvsrc, backend). DSP is driven by calling
    ``b._render_resampler`` with an explicit ``buffers`` dict, matching
    the other module tests.
    """
    patch = Patch()
    src = patch.add_module("oscillator")
    rs = patch.add_module("resampler", params=params or {})
    patch.connect(src.id, "out", rs.id, "in")
    cvsrc = None
    if with_cv:
        cvsrc = patch.add_module("lfo")
        patch.connect(cvsrc.id, "cv", rs.id, "pitch_cv")
    b = _backend(block)
    b.compile(patch)
    return patch, src, rs, cvsrc, b


def _run(b, patch, src, rs, signal, cvsrc=None, cv=None, block=F):
    """Render ``signal`` through the resampler block by block; concat out."""
    n = (signal.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        if cvsrc is not None and cv is not None:
            bufs[(cvsrc.id, "cv")] = cv[..., sl].astype(np.float32)
        outs.append(b._render_resampler(rs, block, bufs, patch)["out"])
    return np.concatenate(outs, axis=-1)


def _tone(freq, secs=1.0):
    t = np.arange(int(secs * SR))
    return np.sin(2 * np.pi * freq * t / SR).astype(np.float32)


def _dominant_hz(y):
    yp = y[len(y) // 3:]  # skip the priming latency
    spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    fr = np.fft.rfftfreq(len(yp), 1.0 / SR)
    return float(fr[spec.argmax()])


def _bl_saw(freq, secs=1.5):
    """Band-limited sawtooth (additive, harmonics kept below ~21 kHz), so
    any aliasing in a pitch-up render is the resampler's, not the source's."""
    t = np.arange(int(secs * SR))
    y = np.zeros(len(t), dtype=np.float64)
    k = 1
    while freq * k < 21000.0:
        y += np.sin(2 * np.pi * freq * k * t / SR) / k
        k += 1
    return (0.5 * y / np.max(np.abs(y))).astype(np.float32)


def _render(params, sig, block=F):
    """Convenience: build an osc->resampler rig and run ``sig`` through it."""
    patch, src, rs, _, b = _rig(params, block=block)
    return _run(b, patch, src, rs, sig, block=block)


def _run_multi(params, sig, block=F):
    """Run through the resampler, returning a dict of concatenated channel
    arrays. Handles both the mono-array return (spread 0) and the
    ``{out, out_l, out_r}`` dict return (spread > 0)."""
    patch, src, rs, _, b = _rig(params, block=block)
    n = (sig.shape[-1] // block) * block
    chans: dict = {}
    for k in range(n // block):
        blk = sig[..., k * block:(k + 1) * block].astype(np.float32)
        r = b._render_resampler(rs, block, {(src.id, "out"): blk}, patch)
        if not isinstance(r, dict):
            r = {"out": r}
        for name, buf in r.items():
            chans.setdefault(name, []).append(buf)
    return {name: np.concatenate(bufs, axis=-1) for name, bufs in chans.items()}


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        rs = Patch().add_module("resampler")
        assert isinstance(rs, Resampler)
        assert rs.params == {
            "semitones": 0.0,
            "cents": 0.0,
            "cv_depth": 12.0,
            "glide": 0.0,
            "mix": 1.0,
            "window": 200.0,
            "antialias": False,
            "spread": 0.0,
        }

    def test_ports_and_signal_kinds(self):
        rs = Patch().add_module("resampler")
        assert [(p.name, p.signal_kind) for p in rs.input_ports] == [
            ("in", "audio"),
            ("pitch_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in rs.output_ports] == [
            ("out", "audio"),
            ("out_l", "audio"),
            ("out_r", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("resampler", params={"semitones": 7.0, "glide": 0.25})
        restored = Patch.from_dict(patch.to_dict())
        rs = next(m for m in restored if m.TYPE == "resampler")
        assert rs.params["semitones"] == 7.0
        assert rs.params["glide"] == 0.25

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("resampler", params={"ratio": 2.0})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        rs = patch.add_module("resampler")
        patch.connect(osc.id, "out", rs.id, "in")  # audio -> audio

    def test_cv_into_pitch_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        rs = patch.add_module("resampler")
        patch.connect(lfo.id, "cv", rs.id, "pitch_cv")  # cv -> cv

    def test_cv_into_audio_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        rs = patch.add_module("resampler")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", rs.id, "in")  # cv -> audio

    def test_audio_into_pitch_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        rs = patch.add_module("resampler")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", rs.id, "pitch_cv")  # audio -> cv

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        rs = patch.add_module("resampler")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(rs.id, "out", vca.id, "cv")  # audio -> cv


# ----- Mono DSP --------------------------------------------------------------


class TestMonoDSP:
    def test_disconnected_audio_is_silence(self):
        patch = Patch()
        rs = patch.add_module("resampler")
        b = _backend()
        b.compile(patch)
        out = b._render_resampler(rs, 256, {}, patch)["out"]
        assert out.shape == (256,)
        assert not out.any()

    def test_unity_is_delayed_passthrough(self):
        # 0 semitones, no glide -> output is the input delayed by the
        # buffer latency, *bit-exact* (ratio 1 means integer reads, no
        # interpolation error).
        patch, src, rs, _, b = _rig({"semitones": 0.0})
        sig = np.random.RandomState(7).randn(40 * F).astype(np.float32)
        full = _run(b, patch, src, rs, sig)
        inp = sig[: full.shape[0]]
        # Find the latency by the first nonzero output sample, then assert
        # an exact delayed copy from there on.
        lag = int(np.argmax(np.abs(full) > 1e-6))
        assert 0 < lag < 20000
        a = full[lag : lag + 4000]
        c = inp[: 4000]
        assert np.max(np.abs(a - c)) == 0.0

    def test_octave_up_doubles_pitch(self):
        patch, src, rs, _, b = _rig({"semitones": 12.0})
        out = _run(b, patch, src, rs, _tone(1000.0, 1.0))
        assert _dominant_hz(out) == pytest.approx(2000.0, rel=0.03)

    def test_octave_down_halves_pitch(self):
        patch, src, rs, _, b = _rig({"semitones": -12.0})
        out = _run(b, patch, src, rs, _tone(1000.0, 1.0))
        assert _dominant_hz(out) == pytest.approx(500.0, rel=0.03)

    def test_cents_equivalent_to_semitones(self):
        # +200 cents == +2 semitones, bit-for-bit (same ratio path).
        sig = np.random.RandomState(1).randn(8 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"cents": 200.0})
        p2, s2, r2, _, b2 = _rig({"semitones": 2.0})
        o1 = _run(b1, p1, s1, r1, sig)
        o2 = _run(b2, p2, s2, r2, sig)
        assert np.array_equal(o1, o2)

    def test_cv_summed_in_semitone_space(self):
        # pitch_cv == 1.0 with cv_depth 12 == a static +12 semitones.
        tone = _tone(1000.0, 1.0)
        p1, s1, r1, c1, b1 = _rig({"cv_depth": 12.0}, with_cv=True)
        p2, s2, r2, _, b2 = _rig({"semitones": 12.0})
        cv = np.ones_like(tone)
        o1 = _run(b1, p1, s1, r1, tone, cvsrc=c1, cv=cv)
        o2 = _run(b2, p2, s2, r2, tone)
        assert np.array_equal(o1, o2)

    def test_finite_and_bounded_on_extremes(self):
        for st in (-60.0, -24.0, -7.0, 0.0, 5.0, 24.0, 60.0):
            patch, src, rs, _, b = _rig({"semitones": st, "glide": 0.03})
            rng = np.random.RandomState(3)
            for _ in range(150):
                blk = (rng.randn(F) * 0.3).astype(np.float32)
                out = b._render_resampler(rs, F, {(src.id, "out"): blk}, patch)["out"]
                assert np.all(np.isfinite(out))
                assert np.abs(out).max() <= 1.5  # interp can't exceed input peak by much

    def test_glide_ramps_through_intermediate_pitch(self):
        # Hold unity, then jump the param to +12 st. With glide the output
        # pitch should sweep up through intermediate frequencies; instant
        # should not linger between 1000 and 2000 Hz.
        def sweep(glide):
            patch, src, rs, _, b = _rig({"semitones": 0.0, "glide": glide})
            tone = _tone(1000.0, 1.5)
            chunks = []
            switched = False
            n = tone.shape[0] // F
            for k in range(n):
                if k > n // 3 and not switched:
                    rs.set_param("semitones", 12.0)
                    switched = True
                blk = tone[k * F : (k + 1) * F]
                chunks.append(b._render_resampler(rs, F, {(src.id, "out"): blk}, patch)["out"])
            y = np.concatenate(chunks)
            # dominant pitch per ~46 ms window across the second half
            win = 2048
            mids = 0
            for i in range(len(y) // 2, len(y) - win, win):
                seg = y[i : i + win]
                if np.abs(seg).max() < 1e-4:
                    continue
                spec = np.abs(np.fft.rfft(seg * np.hanning(win)))
                fr = np.fft.rfftfreq(win, 1.0 / SR)
                hz = fr[spec.argmax()]
                if 1200.0 < hz < 1800.0:
                    mids += 1
            return mids

        glided = sweep(0.4)
        instant = sweep(0.0)
        assert glided > instant
        assert glided >= 1


# ----- Voice DSP -------------------------------------------------------------


class TestVoiceDSP:
    def test_single_voice_matches_mono(self):
        x = np.random.RandomState(0).randn(F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 3.0})
        mono = b1._render_resampler(r1, F, {(s1.id, "out"): x}, p1)["out"]
        p2, s2, r2, _, b2 = _rig({"semitones": 3.0})
        voice = b2._render_resampler(r2, F, {(s2.id, "out"): np.tile(x, (2, 1))}, p2)["out"]
        assert voice.shape == (2, F)
        assert np.array_equal(voice[0], mono)
        assert np.array_equal(voice[0], voice[1])

    def test_voices_transpose_independently(self):
        # Per-voice CV: voice 0 up an octave, voice 1 down an octave.
        tone = _tone(1000.0, 1.0)
        p, s, r, c, b = _rig({"cv_depth": 12.0}, with_cv=True)
        n = tone.shape[0] // F
        outs = []
        for k in range(n):
            blk = tone[k * F : (k + 1) * F]
            audio = np.tile(blk, (2, 1))
            cv = np.zeros((2, F), dtype=np.float32)
            cv[0, :] = 1.0   # +12 st
            cv[1, :] = -1.0  # -12 st
            outs.append(
                b._render_resampler(
                    r, F, {(s.id, "out"): audio, (c.id, "cv"): cv}, p
                )["out"]
            )
        y = np.concatenate(outs, axis=-1)
        assert _dominant_hz(y[0]) == pytest.approx(2000.0, rel=0.04)
        assert _dominant_hz(y[1]) == pytest.approx(500.0, rel=0.04)

    def test_mono_voice_state_reinit(self):
        patch, src, rs, _, b = _rig({"semitones": 2.0})
        mono_x = np.random.RandomState(5).randn(F).astype(np.float32)
        voice_x = np.tile(mono_x, (4, 1))
        o1 = b._render_resampler(rs, F, {(src.id, "out"): mono_x}, patch)["out"]
        ov = b._render_resampler(rs, F, {(src.id, "out"): voice_x}, patch)["out"]
        o2 = b._render_resampler(rs, F, {(src.id, "out"): mono_x}, patch)["out"]
        assert o1.shape == (F,)
        assert ov.shape == (4, F)
        assert o2.shape == (F,)
        assert np.all(np.isfinite(ov))


# ----- Loop-seam declick ------------------------------------------------------


def _max_step(y):
    """Largest sample-to-sample jump after the priming latency."""
    yp = y[len(y) // 3:]
    return float(np.max(np.abs(np.diff(yp))))


def _seam_jumps(b, rs):
    return int(np.sum(b._state[rs.id]["seam_jumps"]))


class TestDeclick:
    def test_pitch_up_jumps_and_stays_clickfree(self):
        # +12 st: the read head gains on the write head and must jump
        # repeatedly. A hard wrap would splice audio ~0.2 s apart (a
        # step on the order of the amplitude); the crossfaded jump keeps
        # the step bounded by the (doubled) tone's own angular step.
        patch, src, rs, _, b = _rig({"semitones": 12.0})
        out = _run(b, patch, src, rs, _tone(440.0, 3.0))
        assert _seam_jumps(b, rs) >= 5
        assert _max_step(out) < 2.5 * (2 * np.pi * 880.0 / SR)

    def test_pitch_down_jumps_and_stays_clickfree(self):
        patch, src, rs, _, b = _rig({"semitones": -12.0})
        out = _run(b, patch, src, rs, _tone(440.0, 3.0))
        assert _seam_jumps(b, rs) >= 5
        assert _max_step(out) < 2.5 * (2 * np.pi * 220.0 / SR)

    def test_unity_never_jumps(self):
        # No drift at ratio 1 -> the guard band never fires; the
        # bit-exact delayed-passthrough test above stays honest.
        patch, src, rs, _, b = _rig({"semitones": 0.0})
        _run(b, patch, src, rs, _tone(440.0, 2.0))
        assert _seam_jumps(b, rs) == 0

    def test_no_rms_dropout_through_seams(self):
        # Equal-power fades: windowed RMS through many seams never
        # collapses (a hard mute/dropout would).
        patch, src, rs, _, b = _rig({"semitones": 7.0})
        out = _run(b, patch, src, rs, _tone(440.0, 3.0))
        yp = out[len(out) // 3:]
        n = (len(yp) // 2048) * 2048
        rms = np.sqrt((yp[:n].reshape(-1, 2048) ** 2).mean(axis=1))
        assert float(rms.min()) > 0.35 * float(rms.max())

    def test_extreme_ratio_finite_with_many_jumps(self):
        patch, src, rs, _, b = _rig({"semitones": 36.0})
        sig = (np.random.RandomState(9).randn(90 * F) * 0.3).astype(np.float32)
        out = _run(b, patch, src, rs, sig)
        assert np.all(np.isfinite(out))
        assert _seam_jumps(b, rs) >= 10

    def test_fade_spans_small_blocks_clickfree(self):
        # Block (64) much shorter than the fade (~350 samples): the
        # crossfade must carry across block boundaries seamlessly.
        patch, src, rs, _, b = _rig({"semitones": 12.0})
        out = _run(b, patch, src, rs, _tone(440.0, 2.0), block=64)
        assert _seam_jumps(b, rs) >= 3
        assert _max_step(out) < 2.5 * (2 * np.pi * 880.0 / SR)

    def test_single_voice_matches_mono_through_seams(self):
        sig = np.random.RandomState(4).randn(70 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 12.0})
        mono = _run(b1, p1, s1, r1, sig)
        p2, s2, r2, _, b2 = _rig({"semitones": 12.0})
        voice = _run(b2, p2, s2, r2, np.tile(sig, (2, 1)))
        assert _seam_jumps(b1, r1) >= 5
        assert np.array_equal(voice[0], mono)
        assert np.array_equal(voice[0], voice[1])

    def test_voices_jump_independently(self):
        # Voice 0 shifted (must jump), voice 1 held at unity via CV
        # (must never jump) -- per-voice heads, per-voice seams.
        tone = _tone(440.0, 3.0)
        p, s, r, c, b = _rig({"cv_depth": 12.0}, with_cv=True)
        n = tone.shape[0] // F
        for k in range(n):
            blk = np.tile(tone[k * F:(k + 1) * F], (2, 1))
            cv = np.zeros((2, F), dtype=np.float32)
            cv[0, :] = 1.0
            b._render_resampler(r, F, {(s.id, "out"): blk, (c.id, "cv"): cv}, p)
        jumps = b._state[r.id]["seam_jumps"]
        assert int(jumps[0]) >= 5
        assert int(jumps[1]) == 0


# ----- Dry/wet mix ------------------------------------------------------------


class TestMix:
    def test_mix_zero_is_delayed_dry_even_when_pitched(self):
        # The dry tap ignores the pitch entirely: mix=0 at +7 st equals
        # the unity full-wet render bit-for-bit (same fixed-lag tap).
        sig = np.random.RandomState(11).randn(20 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 7.0, "mix": 0.0})
        p2, s2, r2, _, b2 = _rig({"semitones": 0.0, "mix": 1.0})
        o1 = _run(b1, p1, s1, r1, sig)
        o2 = _run(b2, p2, s2, r2, sig)
        assert np.array_equal(o1, o2)

    def test_mix_half_at_unity_is_coherent(self):
        # Latency-compensated dry: at unity the wet and dry taps are the
        # same samples, so 0.5/0.5 sums back to exactly 1.0x -- a mix
        # sweep is a coherent blend, not a slapback comb.
        sig = np.random.RandomState(12).randn(20 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 0.0, "mix": 0.5})
        p2, s2, r2, _, b2 = _rig({"semitones": 0.0, "mix": 1.0})
        o1 = _run(b1, p1, s1, r1, sig)
        o2 = _run(b2, p2, s2, r2, sig)
        assert np.array_equal(o1, o2)

    def test_mix_half_pitched_has_both_spectral_peaks(self):
        out = _run(*_rig_run({"semitones": 12.0, "mix": 0.5}))
        m440, m880 = _mag_at(out, 440.0), _mag_at(out, 880.0)
        floor = _mag_at(out, 660.0)  # off-peak reference
        assert m440 > 10 * floor
        assert m880 > 10 * floor

    def test_full_wet_has_no_dry_leak(self):
        out = _run(*_rig_run({"semitones": 12.0, "mix": 1.0}))
        m440, m880 = _mag_at(out, 440.0), _mag_at(out, 880.0)
        assert m440 < 0.05 * m880

    def test_mix_clamped(self):
        sig = np.random.RandomState(13).randn(10 * F).astype(np.float32)
        for lo, hi in ((1.7, 1.0), (-0.4, 0.0)):
            p1, s1, r1, _, b1 = _rig({"semitones": 5.0, "mix": lo})
            p2, s2, r2, _, b2 = _rig({"semitones": 5.0, "mix": hi})
            assert np.array_equal(
                _run(b1, p1, s1, r1, sig), _run(b2, p2, s2, r2, sig)
            )

    def test_mix_voice_matches_mono(self):
        sig = np.random.RandomState(14).randn(20 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 5.0, "mix": 0.5})
        mono = _run(b1, p1, s1, r1, sig)
        p2, s2, r2, _, b2 = _rig({"semitones": 5.0, "mix": 0.5})
        voice = _run(b2, p2, s2, r2, np.tile(sig, (2, 1)))
        assert np.array_equal(voice[0], mono)


def _rig_run(params):
    """(b, patch, src, rs, tone) tuple for the spectral mix tests."""
    patch, src, rs, _, b = _rig(params)
    return b, patch, src, rs, _tone(440.0, 2.0)


def _mag_at(y, hz, width=8.0):
    """Peak windowed-FFT magnitude within +/-width Hz of ``hz``."""
    yp = y[len(y) // 3:]
    spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    fr = np.fft.rfftfreq(len(yp), 1.0 / SR)
    band = (fr >= hz - width) & (fr <= hz + width)
    return float(spec[band].max())


# ----- Window param -----------------------------------------------------------


def _lag_of(full):
    """Measured unity-passthrough latency: index of the first audible sample."""
    return int(np.argmax(np.abs(full) > 1e-6))


class TestWindow:
    def test_default_is_200ms_with_legacy_latency(self):
        # `window` defaults to 200 ms -- the old fixed constant -- so the
        # unity latency is the legacy half-window, and the output is the
        # bit-exact delayed passthrough from there.
        sig = np.random.RandomState(21).randn(40 * F).astype(np.float32)
        p, s, r, _, b = _rig({})
        full = _run(b, p, s, r, sig)
        lag = _lag_of(full)
        assert lag == int(0.5 * int(0.2 * SR)) - F
        assert np.array_equal(full[lag:], sig[: full.shape[0] - lag])

    def test_explicit_default_bit_identical_to_unset(self):
        sig = np.random.RandomState(22).randn(20 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 7.0})
        p2, s2, r2, _, b2 = _rig({"semitones": 7.0, "window": 200.0})
        assert np.array_equal(
            _run(b1, p1, s1, r1, sig), _run(b2, p2, s2, r2, sig)
        )

    def test_window_sets_latency(self):
        # Latency is half the window: 100 ms -> 2205 samples at 44.1k,
        # 1000 ms -> 11025.
        sig = np.random.RandomState(23).randn(120 * F).astype(np.float32)
        for w in (100.0, 1000.0):
            p, s, r, _, b = _rig({"window": w})
            lag = _lag_of(_run(b, p, s, r, sig))
            assert lag == int(0.5 * int((w / 1000.0) * SR)) - F

    def test_window_floored_by_block_size(self):
        # The ring never shrinks below 4 blocks: at block 512, 20 ms
        # (882 samples) floors to 2048 -> 1024 samples of latency.
        sig = np.random.RandomState(24).randn(40 * F).astype(np.float32)
        p, s, r, _, b = _rig({"window": 20.0})
        assert _lag_of(_run(b, p, s, r, sig)) == 2 * F - F

    def test_window_clamped_low(self):
        # Below-range values behave as 20 ms, bit-for-bit. Small block so
        # the block floor doesn't mask the clamp (at 128, unclamped 5 ms
        # would floor to 512 samples, clamped 20 ms is 882).
        blk = 128
        sig = np.random.RandomState(25).randn(200 * blk).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 4.0, "window": 5.0}, block=blk)
        p2, s2, r2, _, b2 = _rig({"semitones": 4.0, "window": 20.0}, block=blk)
        assert np.array_equal(
            _run(b1, p1, s1, r1, sig, block=blk),
            _run(b2, p2, s2, r2, sig, block=blk),
        )

    def test_window_clamped_high(self):
        # Above-range values behave as 2000 ms, bit-for-bit.
        sig = np.random.RandomState(26).randn(150 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 4.0, "window": 99999.0})
        p2, s2, r2, _, b2 = _rig({"semitones": 4.0, "window": 2000.0})
        assert np.array_equal(
            _run(b1, p1, s1, r1, sig), _run(b2, p2, s2, r2, sig)
        )

    def test_small_window_still_declicks(self):
        # A tight 40 ms window at block 128 wraps far more often than
        # the default; every seam must still ride a crossfade, so the
        # sample-to-sample step stays bounded by the tone's own step.
        blk = 128
        p, s, r, _, b = _rig({"semitones": 12.0, "window": 40.0}, block=blk)
        out = _run(b, p, s, r, _tone(440.0, 2.0), block=blk)
        assert _seam_jumps(b, r) >= 20
        assert np.all(np.isfinite(out))
        assert _max_step(out) < 2.5 * (2 * np.pi * 880.0 / SR)

    def test_smaller_window_loops_more_often(self):
        jumps = {}
        for w in (50.0, 200.0):
            p, s, r, _, b = _rig({"semitones": 12.0, "window": w})
            _run(b, p, s, r, _tone(440.0, 3.0))
            jumps[w] = _seam_jumps(b, r)
        assert jumps[50.0] > jumps[200.0]

    def test_grow_mid_stream_keeps_unity_passthrough(self):
        # Enlarging the window live rebuilds the ring around the same
        # audio: at unity the delayed passthrough continues bit-exactly
        # across the change, at the lag it already had (no dropout, no
        # latency snap).
        sig = np.random.RandomState(27).randn(60 * F).astype(np.float32)
        p, s, r, _, b = _rig({"window": 200.0})
        outs = []
        for k in range(60):
            if k == 30:
                r.params["window"] = 800.0
            blk = sig[k * F : (k + 1) * F]
            outs.append(b._render_resampler(r, F, {(s.id, "out"): blk}, p)["out"])
        full = np.concatenate(outs)
        lag = int(0.5 * int(0.2 * SR)) - F  # the *original* latency
        assert np.array_equal(full[lag:], sig[: full.shape[0] - lag])

    def test_shrink_mid_stream_keeps_unity_passthrough(self):
        # A shrink whose new window still covers the head's lag also
        # keeps the passthrough bit-exact (the kept tail contains the
        # content under the head).
        sig = np.random.RandomState(28).randn(80 * F).astype(np.float32)
        p, s, r, _, b = _rig({"window": 400.0})
        outs = []
        for k in range(80):
            if k == 40:
                r.params["window"] = 250.0
            blk = sig[k * F : (k + 1) * F]
            outs.append(b._render_resampler(r, F, {(s.id, "out"): blk}, p)["out"])
        full = np.concatenate(outs)
        lag = int(0.5 * int(0.4 * SR)) - F
        assert np.array_equal(full[lag:], sig[: full.shape[0] - lag])

    def test_hard_shrink_recovers(self):
        # Shrink far below the head's drifted lag while pitched down --
        # the tail under the head is genuinely gone. The clamp lands the
        # head inside the guard band, the ordinary seam machinery
        # re-centres it under a crossfade, and the stream stays finite,
        # in-window, and audibly alive.
        p, s, r, _, b = _rig({"semitones": -12.0, "window": 1500.0})
        sig = _tone(440.0, 4.0)
        outs = []
        for k in range(len(sig) // F):
            if k == 172:  # ~2 s in; the lag has drifted well past 100 ms
                r.params["window"] = 100.0
            blk = sig[k * F : (k + 1) * F]
            outs.append(b._render_resampler(r, F, {(s.id, "out"): blk}, p)["out"])
        full = np.concatenate(outs)
        assert np.all(np.isfinite(full))
        L_new = max(int(0.1 * SR), 4 * F)
        assert np.all(b._state[r.id]["delay"] < L_new)
        tail = full[-SR // 2 :]
        assert np.sqrt(np.mean(tail**2)) > 0.1

    def test_window_change_preserves_seam_counter(self):
        # seam_jumps is an observable; a window tweak must not reset it.
        p, s, r, _, b = _rig({"semitones": 12.0})
        sig = _tone(440.0, 2.0)
        for k in range(len(sig) // F):
            blk = sig[k * F : (k + 1) * F]
            b._render_resampler(r, F, {(s.id, "out"): blk}, p)
        before = _seam_jumps(b, r)
        assert before > 0
        r.params["window"] = 400.0
        b._render_resampler(r, F, {(s.id, "out"): sig[:F]}, p)
        assert _seam_jumps(b, r) >= before

    def test_window_voice_matches_mono(self):
        sig = np.random.RandomState(29).randn(20 * F).astype(np.float32)
        p1, s1, r1, _, b1 = _rig({"semitones": 5.0, "window": 80.0})
        mono = _run(b1, p1, s1, r1, sig)
        p2, s2, r2, _, b2 = _rig({"semitones": 5.0, "window": 80.0})
        voice = _run(b2, p2, s2, r2, np.tile(sig, (2, 1)))
        assert np.array_equal(voice[0], mono)

    def test_window_json_round_trip(self):
        patch = Patch()
        patch.add_module("resampler", params={"window": 350.0})
        restored = Patch.from_dict(patch.to_dict())
        rs = next(m for m in restored if m.TYPE == "resampler")
        assert rs.params["window"] == 350.0


# ----- Interpolation quality (cubic Hermite read) ----------------------------


class TestInterpolation:
    def test_hermite4_returns_p0_at_zero(self):
        # frac == 0 -> the sample itself, *exactly* (the constant term is
        # p0, untouched). This is what keeps an integer-position read --
        # unity ratio, octave shifts -- a bit-exact passthrough.
        rng = np.random.RandomState(0)
        pm1, p0, p1, p2 = rng.randn(4, 64)
        assert np.array_equal(_hermite4(pm1, p0, p1, p2, 0.0), p0)

    def test_hermite4_interpolates_at_one(self):
        # Catmull-Rom is an *interpolating* spline: frac -> 1 lands on p1,
        # so the read is continuous across sample boundaries (no seam).
        rng = np.random.RandomState(1)
        pm1, p0, p1, p2 = rng.randn(4, 64)
        assert np.allclose(_hermite4(pm1, p0, p1, p2, 1.0), p1, atol=1e-12)

    def test_hermite4_reproduces_linear_ramp(self):
        # On collinear taps the spline is the straight line through them
        # -- a ramp reads back as a ramp, no overshoot.
        a, d = 0.3, 0.11
        taps = (a - d, a, a + d, a + 2 * d)   # pm1, p0, p1, p2 on a line
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            assert _hermite4(*taps, t) == pytest.approx(a + t * d, abs=1e-12)

    def test_cubic_reconstruction_beats_linear(self):
        # Reconstruct a mid-band sine at fractional offsets from its
        # integer samples: cubic Hermite tracks the true continuum many
        # times more accurately than 2-tap linear -- the whole point of
        # the upgrade. (Measured ~17x here; assert a conservative 5x.)
        N, cyc = 4096, 256                    # ~2.76 kHz at 44.1 k
        n = np.arange(N)
        s = np.sin(2 * np.pi * cyc * n / N)
        idx = np.arange(2, N - 2)

        def rms_err(fn):
            errs = []
            for t in np.linspace(0.05, 0.95, 19):
                approx = fn(s[idx - 1], s[idx], s[idx + 1], s[idx + 2], t)
                true = np.sin(2 * np.pi * cyc * (idx + t) / N)
                errs.append(np.sqrt(np.mean((approx - true) ** 2)))
            return float(np.mean(errs))

        linear = lambda pm1, p0, p1, p2, t: p0 * (1 - t) + p1 * t  # noqa: E731
        assert rms_err(_hermite4) < rms_err(linear) / 5.0

    def test_engine_read_is_cubic(self, monkeypatch):
        # The module actually routes its ring read through cubic Hermite.
        # On a bright tone shifted *down* (its HF content stays in band,
        # where linear interpolation is worst), forcing the interpolator
        # back to 2-tap linear measurably raises the distortion floor.
        import pysynthrack.audio.numpy_backend as nb

        def thd(y):
            yp = y[len(y) // 3:]
            spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
            fr = np.fft.rfftfreq(len(yp), 1.0 / SR)
            fund = np.abs(fr - fr[spec.argmax()]) < 20.0
            rest = np.sqrt((spec[~fund] ** 2).sum())
            return float(rest / (np.sqrt((spec[fund] ** 2).sum()) + 1e-12))

        tone = _tone(12000.0, 1.0)
        p, s, r, _, b = _rig({"semitones": -7.0})
        cubic = thd(_run(b, p, s, r, tone))
        monkeypatch.setattr(
            nb, "_hermite4",
            lambda pm1, p0, p1, p2, t: p0 * (1.0 - t) + p1 * t,
        )
        p2, s2, r2, _, b2 = _rig({"semitones": -7.0})
        linear = thd(_run(b2, p2, s2, r2, tone))
        assert cubic < 0.85 * linear


# ----- Anti-alias (pitch-up) -------------------------------------------------


def _alias_ratio_saw(y, shifted_fund=4000.0):
    """Energy off the true harmonics vs on them, for a pitched saw."""
    yp = y[len(y) // 3:]
    spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    fr = np.fft.rfftfreq(len(yp), 1.0 / SR)
    harm = [shifted_fund * i for i in range(1, 6) if shifted_fund * i < SR / 2]
    on = np.zeros_like(fr, dtype=bool)
    for h in harm:
        on |= np.abs(fr - h) < 40.0
    alias = np.sqrt((spec[(fr > 200) & ~on] ** 2).sum())
    sig = np.sqrt((spec[on] ** 2).sum())
    return float(alias / (sig + 1e-9))


class TestAntialias:
    def test_default_off_and_roundtrip(self):
        rs = Patch().add_module("resampler")
        assert rs.params["antialias"] is False
        patch = Patch()
        patch.add_module("resampler", params={"antialias": True})
        restored = Patch.from_dict(patch.to_dict())
        r = next(m for m in restored if m.TYPE == "resampler")
        assert r.params["antialias"] is True

    def test_unity_bit_exact_with_aa_on(self):
        # AA never engages at unity (ratio not > 1) -> the wet read stays on
        # the raw ring, bit-identical to AA off (delayed passthrough intact).
        sig = np.random.RandomState(31).randn(20 * F).astype(np.float32)
        on = _render({"semitones": 0.0, "antialias": True}, sig)
        off = _render({"semitones": 0.0, "antialias": False}, sig)
        assert np.array_equal(on, off)

    def test_pitch_down_unaffected(self):
        # Pitching down can't fold content past Nyquist, so AA doesn't
        # engage; on == off bit-for-bit.
        sig = np.random.RandomState(32).randn(20 * F).astype(np.float32)
        on = _render({"semitones": -7.0, "antialias": True}, sig)
        off = _render({"semitones": -7.0, "antialias": False}, sig)
        assert np.array_equal(on, off)

    def test_reduces_pitch_up_alias_folding_tone(self):
        # 15 kHz pitched up an octave lands at 30 kHz (> Nyquist): it should
        # vanish, but un-antialiased it folds back to ~14 kHz near full
        # level. AA removes the source content before the faster read.
        tone = _tone(15000.0, 1.5)
        off = _render({"semitones": 12.0, "antialias": False}, tone)
        on = _render({"semitones": 12.0, "antialias": True}, tone)
        poff = float(np.abs(off[len(off) // 3:]).max())
        pon = float(np.abs(on[len(on) // 3:]).max())
        assert poff > 0.7            # the fold is nearly the whole signal
        assert pon < 0.4 * poff      # AA collapses it (measured ~0.13x)

    def test_reduces_pitch_up_alias_saw(self):
        # A band-limited saw pitched up an octave folds its upper harmonics;
        # AA markedly lowers the off-harmonic (alias) energy.
        saw = _bl_saw(2000.0, 1.5)
        off = _alias_ratio_saw(_render({"semitones": 12.0, "antialias": False}, saw))
        on = _alias_ratio_saw(_render({"semitones": 12.0, "antialias": True}, saw))
        assert on < 0.5 * off        # measured ~0.28x (-13.4 -> -24.4 dB)

    def test_pitch_up_finite_bounded_at_extremes(self):
        # The sos low-pass stays stable (finite, bounded) even at the cutoff
        # floor reached by extreme up-shifts.
        for st in (12.0, 24.0, 48.0, 60.0):
            patch, src, rs, _, b = _rig({"semitones": st, "antialias": True})
            rng = np.random.RandomState(7)
            for _ in range(120):
                blk = (rng.randn(F) * 0.3).astype(np.float32)
                out = b._render_resampler(rs, F, {(src.id, "out"): blk}, patch)["out"]
                assert np.all(np.isfinite(out))
                assert np.abs(out).max() <= 1.5

    def test_voice_matches_mono(self):
        sig = np.random.RandomState(33).randn(30 * F).astype(np.float32)
        mono = _render({"semitones": 12.0, "antialias": True}, sig)
        p, s, r, _, b = _rig({"semitones": 12.0, "antialias": True})
        voice = _run(b, p, s, r, np.tile(sig, (2, 1)))
        assert np.array_equal(voice[0], mono)
        assert np.array_equal(voice[0], voice[1])

    def test_toggle_live_no_dropout(self):
        # Flipping AA on mid-stream during a pitch-up must not punch a wet
        # dropout: the second ring is seeded from the raw ring, not zeros.
        patch, src, rs, _, b = _rig({"semitones": 12.0, "antialias": False})
        tone = _tone(2000.0, 2.0)
        n = len(tone) // F
        rms = []
        for k in range(n):
            if k == n // 2:
                rs.params["antialias"] = True
            out = b._render_resampler(rs, F, {(src.id, "out"): tone[k * F:(k + 1) * F]}, patch)["out"]
            if k > n // 4:
                rms.append(float(np.sqrt(np.mean(out ** 2))))
        rms = np.array(rms)
        assert rms.min() > 0.2 * rms.max()   # no block collapses at the toggle


# ----- Stereo detune spread --------------------------------------------------


class TestStereoSpread:
    def test_default_and_roundtrip(self):
        rs = Patch().add_module("resampler")
        assert rs.params["spread"] == 0.0
        patch = Patch()
        patch.add_module("resampler", params={"spread": 20.0})
        restored = Patch.from_dict(patch.to_dict())
        r = next(m for m in restored if m.TYPE == "resampler")
        assert r.params["spread"] == 20.0

    def test_ports_include_stereo_pair(self):
        rs = Patch().add_module("resampler")
        assert [p.name for p in rs.output_ports] == ["out", "out_l", "out_r"]

    def test_spread_zero_mirrors_out_on_pair(self):
        # At spread 0 the stereo outs aren't silent -- they mirror ``out``
        # (a connected out_l/out_r plays the mono signal), so raising
        # spread later only *widens* rather than un-mutes.
        sig = np.random.RandomState(41).randn(F).astype(np.float32)
        p, s, r, _, b = _rig({"semitones": 5.0, "spread": 0.0})
        d = b._render_resampler(r, F, {(s.id, "out"): sig}, p)
        assert set(d) == {"out", "out_l", "out_r"}
        assert np.array_equal(d["out_l"], d["out"])
        assert np.array_equal(d["out_r"], d["out"])

    def test_out_unaffected_by_spread(self):
        # ``out`` is always the centre pitch: turning spread on doesn't
        # change it one sample (spread only adds the detuned L/R pair).
        sig = np.random.RandomState(42).randn(20 * F).astype(np.float32)
        mono = _run_multi({"semitones": 4.0, "spread": 0.0}, sig)["out"]
        wide = _run_multi({"semitones": 4.0, "spread": 25.0}, sig)["out"]
        assert np.array_equal(mono, wide)

    def test_spread_emits_detuned_pair(self):
        # out_l reads flat of centre, out_r sharp (+/- spread/2 cents).
        tone = _tone(1000.0, 1.5)
        ch = _run_multi({"semitones": 0.0, "spread": 30.0}, tone)
        assert set(ch) == {"out", "out_l", "out_r"}
        centre = _dominant_hz(ch["out"])
        assert _dominant_hz(ch["out_l"]) < centre - 3.0
        assert _dominant_hz(ch["out_r"]) > centre + 3.0

    def test_spread_decorrelates_channels(self):
        # The detuned pair drifts apart -> low L/R correlation is the
        # stereo width (a mono duplicate would correlate at ~1.0).
        tone = _tone(1000.0, 2.0)
        ch = _run_multi({"semitones": 0.0, "spread": 20.0}, tone)
        L, R = ch["out_l"], ch["out_r"]
        corr = float(np.corrcoef(L[len(L) // 2:], R[len(R) // 2:])[0, 1])
        assert abs(corr) < 0.5

    def test_spread_voice_matches_mono(self):
        sig = np.random.RandomState(43).randn(20 * F).astype(np.float32)
        mono = _run_multi({"semitones": 3.0, "spread": 20.0}, sig)
        p, s, r, _, b = _rig({"semitones": 3.0, "spread": 20.0})
        vch: dict = {}
        n = (sig.shape[-1] // F) * F
        for k in range(n // F):
            blk = np.tile(sig[k * F:(k + 1) * F], (2, 1))
            d = b._render_resampler(r, F, {(s.id, "out"): blk}, p)
            for name, buf in d.items():
                vch.setdefault(name, []).append(buf)
        for name in ("out", "out_l", "out_r"):
            v = np.concatenate(vch[name], axis=-1)
            assert np.array_equal(v[0], mono[name])
            assert np.array_equal(v[0], v[1])

    def test_spread_finite_and_bounded(self):
        sig = (np.random.RandomState(44).randn(60 * F) * 0.3).astype(np.float32)
        for st in (-12.0, 0.0, 12.0):
            ch = _run_multi({"semitones": st, "spread": 35.0}, sig)
            for name in ("out", "out_l", "out_r"):
                assert np.all(np.isfinite(ch[name]))
                assert np.abs(ch[name]).max() <= 1.5

    def test_spread_engage_no_dropout(self):
        # Raising spread mid-stream: out_l/out_r start aligned with the
        # centre head (not from scratch), so neither channel drops out.
        p, s, r, _, b = _rig({"semitones": 0.0, "spread": 0.0})
        tone = _tone(1000.0, 2.0)
        n = len(tone) // F
        rms = []
        for k in range(n):
            if k == n // 2:
                r.params["spread"] = 20.0
            d = b._render_resampler(r, F, {(s.id, "out"): tone[k * F:(k + 1) * F]}, p)
            if isinstance(d, dict) and k > n // 2 + 2:
                rms.append(float(np.sqrt(np.mean(d["out_l"] ** 2))))
        rms = np.array(rms)
        assert rms.min() > 0.2 * rms.max()


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_resampler_speaker_renders(self):
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "saw", "freq": 220.0, "amp": 0.8}
        )
        rs = patch.add_module("resampler", params={"semitones": 7.0})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", rs.id, "in")
        patch.connect(rs.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(60):  # enough blocks to clear the priming latency
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0

    def test_vibrato_via_lfo_into_pitch_cv(self):
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.8}
        )
        lfo = patch.add_module("lfo", params={"waveform": "sine", "rate": 6.0})
        rs = patch.add_module("resampler", params={"cv_depth": 1.0})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", rs.id, "in")
        patch.connect(lfo.id, "cv", rs.id, "pitch_cv")
        patch.connect(rs.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(60):
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0
