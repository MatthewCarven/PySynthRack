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
  - Integration: osc -> resampler -> speaker renders audible audio; an
    LFO into pitch_cv gives vibrato.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.resampler import Resampler

SR = 44100
F = 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(params=None, with_cv=False):
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
    b = _backend()
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
        outs.append(b._render_resampler(rs, block, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _tone(freq, secs=1.0):
    t = np.arange(int(secs * SR))
    return np.sin(2 * np.pi * freq * t / SR).astype(np.float32)


def _dominant_hz(y):
    yp = y[len(y) // 3:]  # skip the priming latency
    spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    fr = np.fft.rfftfreq(len(yp), 1.0 / SR)
    return float(fr[spec.argmax()])


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
        }

    def test_ports_and_signal_kinds(self):
        rs = Patch().add_module("resampler")
        assert [(p.name, p.signal_kind) for p in rs.input_ports] == [
            ("in", "audio"),
            ("pitch_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in rs.output_ports] == [("out", "audio")]

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
        out = b._render_resampler(rs, 256, {}, patch)
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
                out = b._render_resampler(rs, F, {(src.id, "out"): blk}, patch)
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
                chunks.append(b._render_resampler(rs, F, {(src.id, "out"): blk}, patch))
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
        mono = b1._render_resampler(r1, F, {(s1.id, "out"): x}, p1)
        p2, s2, r2, _, b2 = _rig({"semitones": 3.0})
        voice = b2._render_resampler(r2, F, {(s2.id, "out"): np.tile(x, (2, 1))}, p2)
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
                )
            )
        y = np.concatenate(outs, axis=-1)
        assert _dominant_hz(y[0]) == pytest.approx(2000.0, rel=0.04)
        assert _dominant_hz(y[1]) == pytest.approx(500.0, rel=0.04)

    def test_mono_voice_state_reinit(self):
        patch, src, rs, _, b = _rig({"semitones": 2.0})
        mono_x = np.random.RandomState(5).randn(F).astype(np.float32)
        voice_x = np.tile(mono_x, (4, 1))
        o1 = b._render_resampler(rs, F, {(src.id, "out"): mono_x}, patch)
        ov = b._render_resampler(rs, F, {(src.id, "out"): voice_x}, patch)
        o2 = b._render_resampler(rs, F, {(src.id, "out"): mono_x}, patch)
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
