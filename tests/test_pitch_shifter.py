"""Tests for the PitchShifter (granular WSOLA, time-preserving).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in,
    pitch_cv in, audio out), JSON round-trip, unknown-param rejection,
    type walls.
  - Mono DSP: disconnected -> silence; pitch accuracy (octave up/down,
    a fifth); the shift is *time-preserving* (a held tone stays steady
    and full-level, not speeding up or dying); CV summed in semitone
    space; dry/wet mix (mix=0 is dry, mix=1 is wet); finite/bounded on
    extremes; grain_size / overlap changes still shift correctly.
  - Voice DSP: a single-voice row is bit-identical to mono across many
    blocks; per-voice CV transposes voices independently; mono<->voice
    state reinit.
  - Integration: osc -> pitch_shifter -> speaker renders audible audio;
    a 50% mix harmony renders.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.pitch_shifter import PitchShifter

SR = 44100
F = 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(params=None, with_cv=False):
    patch = Patch()
    src = patch.add_module("oscillator")
    ps = patch.add_module("pitch_shifter", params=params or {})
    patch.connect(src.id, "out", ps.id, "in")
    cvsrc = None
    if with_cv:
        cvsrc = patch.add_module("lfo")
        patch.connect(cvsrc.id, "cv", ps.id, "pitch_cv")
    b = _backend()
    b.compile(patch)
    return patch, src, ps, cvsrc, b


def _run(b, patch, src, ps, signal, cvsrc=None, cv=None, block=F):
    n = (signal.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        if cvsrc is not None and cv is not None:
            bufs[(cvsrc.id, "cv")] = cv[..., sl].astype(np.float32)
        outs.append(b._render_pitch_shifter(ps, block, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _tone(freq, secs=1.3):
    t = np.arange(int(secs * SR))
    return np.sin(2 * np.pi * freq * t / SR).astype(np.float32)


def _dominant_hz(y, lo=0.5, hi=0.92):
    yp = y[int(len(y) * lo):int(len(y) * hi)]
    spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    fr = np.fft.rfftfreq(len(yp), 1.0 / SR)
    return float(fr[spec.argmax()])


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        ps = Patch().add_module("pitch_shifter")
        assert isinstance(ps, PitchShifter)
        assert ps.params == {
            "semitones": 0.0,
            "cents": 0.0,
            "cv_depth": 12.0,
            "mix": 1.0,
            "grain_size": 50.0,
            "overlap": 2,
        }

    def test_ports_and_signal_kinds(self):
        ps = Patch().add_module("pitch_shifter")
        assert [(p.name, p.signal_kind) for p in ps.input_ports] == [
            ("in", "audio"),
            ("pitch_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in ps.output_ports] == [("out", "audio")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("pitch_shifter", params={"semitones": -5.0, "mix": 0.4, "overlap": 4})
        restored = Patch.from_dict(patch.to_dict())
        ps = next(m for m in restored if m.TYPE == "pitch_shifter")
        assert ps.params["semitones"] == -5.0
        assert ps.params["mix"] == 0.4
        assert ps.params["overlap"] == 4

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("pitch_shifter", params={"window": 1024})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ps = patch.add_module("pitch_shifter")
        patch.connect(osc.id, "out", ps.id, "in")

    def test_cv_into_pitch_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ps = patch.add_module("pitch_shifter")
        patch.connect(lfo.id, "cv", ps.id, "pitch_cv")

    def test_cv_into_audio_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ps = patch.add_module("pitch_shifter")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ps.id, "in")

    def test_audio_into_pitch_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ps = patch.add_module("pitch_shifter")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", ps.id, "pitch_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        ps = patch.add_module("pitch_shifter")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(ps.id, "out", vca.id, "cv")


# ----- Mono DSP --------------------------------------------------------------


class TestMonoDSP:
    def test_disconnected_audio_is_silence(self):
        patch = Patch()
        ps = patch.add_module("pitch_shifter")
        b = _backend()
        b.compile(patch)
        out = b._render_pitch_shifter(ps, 256, {}, patch)
        assert out.shape == (256,)
        assert not out.any()

    def test_octave_up(self):
        patch, src, ps, _, b = _rig({"semitones": 12.0})
        out = _run(b, patch, src, ps, _tone(440.0))
        assert _dominant_hz(out) == pytest.approx(880.0, rel=0.03)

    def test_octave_down(self):
        patch, src, ps, _, b = _rig({"semitones": -12.0})
        out = _run(b, patch, src, ps, _tone(440.0))
        assert _dominant_hz(out) == pytest.approx(220.0, rel=0.03)

    def test_fifth_up(self):
        patch, src, ps, _, b = _rig({"semitones": 7.0})
        out = _run(b, patch, src, ps, _tone(440.0))
        assert _dominant_hz(out) == pytest.approx(659.3, rel=0.03)

    def test_time_preserving_steady_tone(self):
        # A held tone in -> a held tone out: shifted pitch, but steady
        # amplitude across time (not speeding up or decaying), which is
        # what distinguishes this from the varispeed resampler.
        patch, src, ps, _, b = _rig({"semitones": 5.0})
        out = _run(b, patch, src, ps, _tone(330.0, secs=1.5))
        tail = out[len(out) // 2:]
        q = len(tail) // 4
        rmss = [np.sqrt(np.mean(tail[i * q:(i + 1) * q] ** 2)) for i in range(4)]
        assert min(rmss) > 0.3            # sustained, not dying
        assert max(rmss) / max(min(rmss), 1e-9) < 1.6   # roughly constant level

    def test_cv_summed_in_semitone_space(self):
        # pitch_cv == 1.0 with cv_depth 12 == a static +12 semitones.
        tone = _tone(440.0)
        p1, s1, r1, c1, b1 = _rig({"cv_depth": 12.0}, with_cv=True)
        cv = np.ones_like(tone)
        o1 = _run(b1, p1, s1, r1, tone, cvsrc=c1, cv=cv)
        assert _dominant_hz(o1) == pytest.approx(880.0, rel=0.04)

    def test_mix_zero_is_dry(self):
        patch, src, ps, _, b = _rig({"semitones": 12.0, "mix": 0.0})
        out = _run(b, patch, src, ps, _tone(440.0))
        assert _dominant_hz(out) == pytest.approx(440.0, rel=0.02)

    def test_mix_one_is_wet(self):
        patch, src, ps, _, b = _rig({"semitones": 12.0, "mix": 1.0})
        out = _run(b, patch, src, ps, _tone(440.0))
        assert _dominant_hz(out) == pytest.approx(880.0, rel=0.03)

    def test_finite_on_extremes(self):
        for st in (-36.0, -24.0, -7.0, 0.0, 7.0, 24.0, 36.0):
            patch, src, ps, _, b = _rig({"semitones": st})
            rng = np.random.RandomState(2)
            for _ in range(120):
                blk = (rng.randn(F) * 0.3).astype(np.float32)
                out = b._render_pitch_shifter(ps, F, {(src.id, "out"): blk}, patch)
                assert np.all(np.isfinite(out))
                assert np.abs(out).max() <= 2.0

    def test_grain_and_overlap_variants_shift(self):
        for grain in (20.0, 80.0):
            for ov in (2, 4):
                patch, src, ps, _, b = _rig(
                    {"semitones": 12.0, "grain_size": grain, "overlap": ov}
                )
                out = _run(b, patch, src, ps, _tone(440.0))
                assert np.all(np.isfinite(out))
                assert _dominant_hz(out) == pytest.approx(880.0, rel=0.05)


# ----- Voice DSP -------------------------------------------------------------


class TestVoiceDSP:
    def test_single_voice_matches_mono(self):
        blocks = [np.random.RandomState(i).randn(F).astype(np.float32) for i in range(14)]
        p1, s1, r1, _, b1 = _rig({"semitones": 5.0})
        mono = [b1._render_pitch_shifter(r1, F, {(s1.id, "out"): x}, p1) for x in blocks]
        p2, s2, r2, _, b2 = _rig({"semitones": 5.0})
        voice = [
            b2._render_pitch_shifter(r2, F, {(s2.id, "out"): np.tile(x, (2, 1))}, p2)
            for x in blocks
        ]
        assert voice[-1].shape == (2, F)
        for k in range(len(blocks)):
            assert np.array_equal(voice[k][0], mono[k])
            assert np.array_equal(voice[k][0], voice[k][1])

    def test_voices_transpose_independently(self):
        tone = _tone(440.0)
        p, s, r, c, b = _rig({"cv_depth": 12.0}, with_cv=True)
        n = tone.shape[0] // F
        outs = []
        for k in range(n):
            blk = tone[k * F:(k + 1) * F]
            audio = np.tile(blk, (2, 1))
            cv = np.zeros((2, F), dtype=np.float32)
            cv[0, :] = 1.0   # +12 st
            cv[1, :] = -1.0  # -12 st
            outs.append(
                b._render_pitch_shifter(r, F, {(s.id, "out"): audio, (c.id, "cv"): cv}, p)
            )
        y = np.concatenate(outs, axis=-1)
        assert _dominant_hz(y[0]) == pytest.approx(880.0, rel=0.04)
        assert _dominant_hz(y[1]) == pytest.approx(220.0, rel=0.04)

    def test_mono_voice_state_reinit(self):
        patch, src, ps, _, b = _rig({"semitones": 3.0})
        mono_x = np.random.RandomState(5).randn(F).astype(np.float32)
        voice_x = np.tile(mono_x, (4, 1))
        o1 = b._render_pitch_shifter(ps, F, {(src.id, "out"): mono_x}, patch)
        ov = b._render_pitch_shifter(ps, F, {(src.id, "out"): voice_x}, patch)
        o2 = b._render_pitch_shifter(ps, F, {(src.id, "out"): mono_x}, patch)
        assert o1.shape == (F,)
        assert ov.shape == (4, F)
        assert o2.shape == (F,)
        assert np.all(np.isfinite(ov))


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_pitch_shifter_speaker_renders(self):
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "saw", "freq": 220.0, "amp": 0.8}
        )
        ps = patch.add_module("pitch_shifter", params={"semitones": 12.0})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", ps.id, "in")
        patch.connect(ps.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(120):
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0

    def test_harmony_via_mix(self):
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "saw", "freq": 220.0, "amp": 0.7}
        )
        ps = patch.add_module(
            "pitch_shifter", params={"semitones": 7.0, "mix": 0.5}
        )
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", ps.id, "in")
        patch.connect(ps.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(120):
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0
