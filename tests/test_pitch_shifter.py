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
  - Accuracy (ideal-grid WSOLA + sub-sample joins): octave up/down,
    fourth up and one-semitone shifts land within ~a cent on a pure
    sine (interpolated-FFT measurement); configs that used to deadlock
    the old accumulating analysis pointer (100 ms grain @ +12, +5 st)
    now sustain full level for the whole render.
  - Deep bass: the period detector grows the working grain when the
    user grain holds < ~2.5 cycles (35/30 Hz), observable via the
    regrains counter and the engine's effective grain; normal material
    never regrains (hysteresis, no thrash); pitch stays sub-3-cents.
  - Formant preserve: default off and registered; st=0 with it on is
    level-neutral; a synthetic two-resonator vowel shifted +7 keeps its
    formant centroid (off, the centroid migrates up); mix=0 dry stays
    the raw input bit-exactly; white noise stays bounded; LPC recovers
    a known AR(2); voice row == mono through the formant path.
  - 2026-09-14 love pass (feedback shimmer / harmonizer / stereo
    spread), every knob OFF by default: explicit zeros render
    array_equal to their absence (mono + voice, formant on and off);
    shimmer at +12 stacks octaves that feedback=0 does not have (A/B
    spectral), stays bounded at the clamp on hot input, and leaves the
    dry side of `mix` bit-exact; the harmonizer adds a second interval
    that pitch_cv moves with the main, at a level that scales, and is
    not built until the level rises; spread 0 aliases out_l/out_r to
    out, spread 1 hard-pans main left and harmony right with the dry
    centred; voice rows == mono with all three on; type walls on the
    new jacks.

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
        outs.append(b._render_pitch_shifter(ps, block, bufs, patch)["out"])
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
            "formant_preserve": False,
            "feedback": 0.0,
            "harmony": 0.0,
            "harmony_level": 0.0,
            "spread": 0.0,
        }

    def test_ports_and_signal_kinds(self):
        ps = Patch().add_module("pitch_shifter")
        assert [(p.name, p.signal_kind) for p in ps.input_ports] == [
            ("in", "audio"),
            ("pitch_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in ps.output_ports] == [
            ("out", "audio"), ("out_l", "audio"), ("out_r", "audio"),
        ]

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
        out = b._render_pitch_shifter(ps, 256, {}, patch)["out"]
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

    def test_mix_is_phase_coherent_at_unison(self):
        # At shift 0 the wet output is exactly the input delayed by the
        # engine's latency, so the dry tap must land on the SAME delay --
        # otherwise a partial mix comb-filters and loses energy. Render the
        # same noise at mix=1/0/0.5: wet and dry must be near-identical, and
        # the 50% blend must keep ~full RMS (no cancellation). This fails
        # with a constant Dc=grain latency comp; passes with the exact one.
        rng = np.random.RandomState(3)
        sig = (rng.randn(SR) * 0.3).astype(np.float32)

        def render(mix):
            patch, src, ps, _, b = _rig({"semitones": 0.0, "mix": mix})
            return _run(b, patch, src, ps, sig)

        wet, dry, blend = render(1.0), render(0.0), render(0.5)
        s = slice(SR // 2, SR // 2 + 8192)   # well past priming
        w, d, bl = wet[s], dry[s], blend[s]
        corr = float(np.dot(w, d) / (np.linalg.norm(w) * np.linalg.norm(d) + 1e-12))
        assert corr > 0.98                    # dry is delay-matched to the wet
        w_rms = float(np.sqrt((w ** 2).mean()))
        bl_rms = float(np.sqrt((bl ** 2).mean()))
        assert bl_rms > 0.9 * w_rms           # blend keeps full energy

    def test_finite_on_extremes(self):
        for st in (-36.0, -24.0, -7.0, 0.0, 7.0, 24.0, 36.0):
            patch, src, ps, _, b = _rig({"semitones": st})
            rng = np.random.RandomState(2)
            for _ in range(120):
                blk = (rng.randn(F) * 0.3).astype(np.float32)
                out = b._render_pitch_shifter(ps, F, {(src.id, "out"): blk}, patch)["out"]
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

    def test_overlap_clamped_to_2_4(self):
        # Documented range is 2..4 (the UI slider caps there); only hand-
        # edited JSON can pass 1 or >4. Those clamp, so overlap=1 renders
        # identically to 2 and overlap=8 identically to 4 -- the engine no
        # longer takes the degenerate overlap=1 path (no grain overlap ->
        # amplitude dips).
        def render(ov):
            patch, src, ps, _, b = _rig({"semitones": 7.0, "overlap": ov})
            return _run(b, patch, src, ps, _tone(330.0))

        assert np.array_equal(render(1), render(2))
        assert np.array_equal(render(8), render(4))


# ----- Voice DSP -------------------------------------------------------------


class TestVoiceDSP:
    def test_single_voice_matches_mono(self):
        blocks = [np.random.RandomState(i).randn(F).astype(np.float32) for i in range(14)]
        p1, s1, r1, _, b1 = _rig({"semitones": 5.0})
        mono = [b1._render_pitch_shifter(r1, F, {(s1.id, "out"): x}, p1)["out"] for x in blocks]
        p2, s2, r2, _, b2 = _rig({"semitones": 5.0})
        voice = [
            b2._render_pitch_shifter(r2, F, {(s2.id, "out"): np.tile(x, (2, 1))}, p2)["out"]
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
                b._render_pitch_shifter(r, F, {(s.id, "out"): audio, (c.id, "cv"): cv}, p)["out"]
            )
        y = np.concatenate(outs, axis=-1)
        assert _dominant_hz(y[0]) == pytest.approx(880.0, rel=0.04)
        assert _dominant_hz(y[1]) == pytest.approx(220.0, rel=0.04)

    def test_mono_voice_state_reinit(self):
        patch, src, ps, _, b = _rig({"semitones": 3.0})
        mono_x = np.random.RandomState(5).randn(F).astype(np.float32)
        voice_x = np.tile(mono_x, (4, 1))
        o1 = b._render_pitch_shifter(ps, F, {(src.id, "out"): mono_x}, patch)["out"]
        ov = b._render_pitch_shifter(ps, F, {(src.id, "out"): voice_x}, patch)["out"]
        o2 = b._render_pitch_shifter(ps, F, {(src.id, "out"): mono_x}, patch)["out"]
        assert o1.shape == (F,)
        assert ov.shape == (4, F)
        assert o2.shape == (F,)
        assert np.all(np.isfinite(ov))


# ----- Accuracy (grid WSOLA + sub-sample joins) --------------------------------


def _cents_vs(y, target_hz, lo=0.5, hi=0.95):
    """Interpolated-FFT pitch error in cents against ``target_hz``."""
    yp = y[int(len(y) * lo):int(len(y) * hi)]
    spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    k0 = int(np.argmax(spec))
    a = np.log(spec[k0 - 1] + 1e-30)
    b = np.log(spec[k0] + 1e-30)
    c = np.log(spec[k0 + 1] + 1e-30)
    d = 0.5 * (a - c) / (a - 2 * b + c)
    f_est = (k0 + d) * SR / len(yp)
    return 1200.0 * np.log2(f_est / target_hz)


def _tail_rms(y, secs=0.5):
    return float(np.sqrt((y[-int(secs * SR):] ** 2).mean()))


class TestAccuracy:
    def test_octave_up_sub_cent(self):
        patch, src, ps, _, b = _rig({"semitones": 12.0})
        out = _run(b, patch, src, ps, _tone(440.0, 4.0))
        assert abs(_cents_vs(out, 880.0)) < 1.5

    def test_octave_down_sub_cent(self):
        patch, src, ps, _, b = _rig({"semitones": -12.0})
        out = _run(b, patch, src, ps, _tone(440.0, 4.0))
        assert abs(_cents_vs(out, 220.0)) < 1.5

    def test_one_semitone_fine(self):
        patch, src, ps, _, b = _rig({"semitones": 1.0})
        out = _run(b, patch, src, ps, _tone(440.0, 4.0))
        assert abs(_cents_vs(out, 440.0 * 2 ** (1 / 12))) < 1.0

    def test_fourth_up_no_deadlock(self):
        # +5 st deadlocked the old accumulating analysis pointer (the
        # constant alignment residue under-consumed input until the
        # pointer fell out of the ring and the output died to DC).
        patch, src, ps, _, b = _rig({"semitones": 5.0})
        out = _run(b, patch, src, ps, _tone(440.0, 4.0))
        assert _tail_rms(out) > 0.5
        assert abs(_cents_vs(out, 440.0 * 2 ** (5 / 12))) < 1.5

    def test_big_grain_no_deadlock(self):
        # 100 ms grain @ +12 was another dead config.
        patch, src, ps, _, b = _rig({"semitones": 12.0, "grain_size": 100.0})
        out = _run(b, patch, src, ps, _tone(440.0, 4.0))
        assert _tail_rms(out) > 0.5
        assert abs(_cents_vs(out, 880.0)) < 2.0

    def test_no_dropouts_long_run(self):
        patch, src, ps, _, b = _rig({"semitones": 7.0})
        out = _run(b, patch, src, ps, _tone(440.0, 5.0))
        yp = out[SR:]
        n = (len(yp) // 4096) * 4096
        rms = np.sqrt((yp[:n].reshape(-1, 4096) ** 2).mean(axis=1))
        assert float(rms.min()) > 0.4

    def test_single_voice_matches_mono_new_path(self):
        sig = _tone(440.0, 2.0)
        p1, s1, r1, _, b1 = _rig({"semitones": 5.0})
        mono = _run(b1, p1, s1, r1, sig)
        p2, s2, r2, _, b2 = _rig({"semitones": 5.0})
        voice = _run(b2, p2, s2, r2, np.tile(sig, (2, 1)))
        assert np.array_equal(voice[0], mono)
        assert np.array_equal(voice[0], voice[1])


# ----- Deep bass (pitch-synchronous grain sizing) ------------------------------


class TestDeepBass:
    def test_35hz_octave_up_regrains_and_tracks(self):
        patch, src, ps, _, b = _rig({"semitones": 12.0})
        out = _run(b, patch, src, ps, _tone(35.0, 6.0))
        st = b._state[ps.id]
        assert int(st["regrains"][0]) >= 1
        assert st["eng"][0].Lg > int(round(50.0e-3 * SR))
        assert _tail_rms(out) > 0.5
        assert abs(_cents_vs(out, 70.0, lo=0.6)) < 3.0

    def test_30hz_fifth_up_sustains(self):
        patch, src, ps, _, b = _rig({"semitones": 7.0})
        out = _run(b, patch, src, ps, _tone(30.0, 6.0))
        st = b._state[ps.id]
        assert int(st["regrains"][0]) >= 1
        assert _tail_rms(out) > 0.5
        assert abs(_cents_vs(out, 30.0 * 2 ** (7 / 12), lo=0.6)) < 3.0

    def test_normal_material_never_regrains(self):
        patch, src, ps, _, b = _rig({"semitones": 3.0})
        _run(b, patch, src, ps, _tone(440.0, 3.0))
        st = b._state[ps.id]
        assert int(st["regrains"][0]) == 0
        assert st["eng"][0].Lg == int(round(50.0e-3 * SR))

    def test_no_thrash_on_steady_tone(self):
        patch, src, ps, _, b = _rig({"semitones": 5.0})
        _run(b, patch, src, ps, _tone(220.0, 4.0))
        assert int(b._state[ps.id]["regrains"][0]) == 0


# ----- Formant preserve ---------------------------------------------------------


def _vowel(secs, f0=110.0):
    """Synthetic vowel: an ``f0`` pulse train through two resonators
    (800 Hz + 2400 Hz) -- fixed formants over a definite pitch."""
    from scipy.signal import lfilter as _lf
    n = int(secs * SR)
    pulses = np.zeros(n)
    pulses[:: int(round(SR / f0))] = 1.0

    def reso(fc, q, x):
        w0 = 2 * np.pi * fc / SR
        al = np.sin(w0) / (2 * q)
        b_ = np.array([al, 0.0, -al]) / (1 + al)
        a_ = np.array([1 + al, -2 * np.cos(w0), 1 - al]) / (1 + al)
        return _lf(b_, a_, x)

    v = reso(800.0, 8.0, pulses) + 0.7 * reso(2400.0, 8.0, pulses)
    return (v / np.max(np.abs(v)) * 0.8).astype(np.float32)


def _band_centroid(y, lo, hi):
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    fr = np.fft.rfftfreq(len(y), 1.0 / SR)
    m = (fr >= lo) & (fr <= hi)
    return float((fr[m] * spec[m]).sum() / (spec[m].sum() + 1e-12))


class TestFormantPreserve:
    def test_unshifted_is_level_neutral(self):
        v = _vowel(4.0)
        patch, src, ps, _, b = _rig({"semitones": 0.0, "formant_preserve": True})
        out = _run(b, patch, src, ps, v)[2 * SR:]
        vin = v[2 * SR: (len(v) // F) * F]
        ratio = np.sqrt((out ** 2).mean()) / np.sqrt((vin ** 2).mean())
        assert 0.7 < float(ratio) < 1.4

    def test_vowel_keeps_formant_when_on(self):
        # +7 st moves the fundamental 110 -> 165. Without preservation
        # the 800 Hz formant migrates toward 1200; with it, the energy
        # centroid of the first-formant band stays near 800.
        v = _vowel(5.0)
        p1, s1, r1, _, b1 = _rig({"semitones": 7.0, "formant_preserve": True})
        on = _run(b1, p1, s1, r1, v)[2 * SR:]
        p2, s2, r2, _, b2 = _rig({"semitones": 7.0, "formant_preserve": False})
        off = _run(b2, p2, s2, r2, v)[2 * SR:]
        c_on = _band_centroid(on, 400.0, 1600.0)
        c_off = _band_centroid(off, 400.0, 1600.0)
        assert c_on < 1050.0
        assert c_off > 1100.0
        assert c_on < c_off - 100.0

    def test_dry_tap_stays_raw(self):
        # mix=0 must return the true input regardless of the whitened
        # wet path: bit-equal to the formant-off dry render.
        sig = _tone(220.0, 2.0)
        p1, s1, r1, _, b1 = _rig({"semitones": 12.0, "mix": 0.0, "formant_preserve": True})
        p2, s2, r2, _, b2 = _rig({"semitones": 12.0, "mix": 0.0, "formant_preserve": False})
        assert np.array_equal(
            _run(b1, p1, s1, r1, sig), _run(b2, p2, s2, r2, sig)
        )

    def test_no_startup_blast(self):
        # Regression: envelopes estimated before the wet path primed
        # once blasted the first wet block ~10x over the input level.
        v = _vowel(2.0)
        patch, src, ps, _, b = _rig({"semitones": 5.0, "formant_preserve": True})
        out = _run(b, patch, src, ps, v)
        steady = float(np.abs(out[SR:]).max())
        startup = float(np.abs(out[:SR]).max())
        assert startup < max(4.0 * steady, 0.2)

    def test_noise_stays_bounded(self):
        sig = (np.random.RandomState(6).randn(int(4.0 * SR)) * 0.3).astype(np.float32)
        patch, src, ps, _, b = _rig({"semitones": 12.0, "formant_preserve": True})
        out = _run(b, patch, src, ps, sig)
        assert np.all(np.isfinite(out))
        assert float(np.abs(out).max()) < 4.0

    def test_lpc_recovers_known_ar2(self):
        from scipy.signal import lfilter as _lf

        from pysynthrack.audio.numpy_backend import _lpc_coeffs
        rng = np.random.RandomState(3)
        x = _lf([1.0], [1.0, -1.2, 0.72], rng.randn(8192))
        a = _lpc_coeffs(x, 8, SR)
        assert a is not None
        assert abs(a[1] - (-1.2)) < 0.1
        assert abs(a[2] - 0.72) < 0.1
        assert np.all(np.abs(a[3:]) < 0.1)

    def test_voice_matches_mono_formant_on(self):
        v = _vowel(2.0)
        p1, s1, r1, _, b1 = _rig({"semitones": 7.0, "formant_preserve": True})
        mono = _run(b1, p1, s1, r1, v)
        p2, s2, r2, _, b2 = _rig({"semitones": 7.0, "formant_preserve": True})
        voice = _run(b2, p2, s2, r2, np.tile(v, (2, 1)))
        assert np.array_equal(voice[0], mono)


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


# ----- 2026-09-14 love pass: shimmer / harmonizer / stereo -------------------


def _run_all(b, patch, src, ps, signal, cvsrc=None, cv=None, block=F):
    """Like _run but keeps every jack: {"out", "out_l", "out_r"}."""
    n = (signal.shape[-1] // block) * block
    outs = {"out": [], "out_l": [], "out_r": []}
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        if cvsrc is not None and cv is not None:
            bufs[(cvsrc.id, "cv")] = cv[..., sl].astype(np.float32)
        r = b._render_pitch_shifter(ps, block, bufs, patch)
        for key in outs:
            outs[key].append(np.asarray(r[key]).copy())
    return {k: np.concatenate(v, axis=-1) for k, v in outs.items()}


def _band_db(y, hz, lo=0.5, hi=0.92, half=12.0):
    """Peak magnitude (dB) within +-half Hz of `hz` in the settled window."""
    yp = y[int(len(y) * lo):int(len(y) * hi)]
    spec = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    fr = np.fft.rfftfreq(len(yp), 1.0 / SR)
    sel = (fr >= hz - half) & (fr <= hz + half)
    return 20.0 * np.log10(float(spec[sel].max()) + 1e-12)


class TestLovePassDefaultsAreOff:
    @pytest.mark.parametrize("formant", [False, True])
    def test_explicit_zeros_render_as_their_absence(self, formant):
        """The three knobs at 0 must be indistinguishable from a patch
        that predates them -- mono and a two-voice render, every jack."""
        rng = np.random.RandomState(9)
        sig = (0.4 * rng.randn(F * 24)).astype(np.float32)
        base = {"semitones": 7.0, "mix": 0.6, "formant_preserve": formant}
        p1, s1, r1, _, b1 = _rig(base)
        plain = _run_all(b1, p1, s1, r1, sig)
        p2, s2, r2, _, b2 = _rig({**base, "feedback": 0.0, "harmony": 5.0,
                                  "harmony_level": 0.0, "spread": 0.7})
        explicit = _run_all(b2, p2, s2, r2, sig)
        for key in ("out", "out_l", "out_r"):
            assert np.array_equal(plain[key], explicit[key]), key
        # And the harmony engine was never built.
        assert "eng2" not in b2._state[r2.id]
        # Voice path.
        v = np.stack([sig, sig[::-1]])
        p1, s1, r1, _, b1 = _rig(base)
        plain = _run_all(b1, p1, s1, r1, v)
        p2, s2, r2, _, b2 = _rig({**base, "feedback": 0.0, "harmony": 5.0,
                                  "harmony_level": 0.0, "spread": 0.7})
        explicit = _run_all(b2, p2, s2, r2, v)
        for key in ("out", "out_l", "out_r"):
            assert np.array_equal(plain[key], explicit[key]), key

    def test_out_l_and_out_r_are_out_without_a_harmony(self):
        sig = _tone(330.0, 0.5)
        p, s, r, _, b = _rig({"semitones": 12.0, "spread": 1.0})
        o = _run_all(b, p, s, r, sig)
        assert np.array_equal(o["out_l"], o["out"])
        assert np.array_equal(o["out_r"], o["out"])
        p, s, r, _, b = _rig({"semitones": 12.0, "feedback": 0.5, "spread": 1.0})
        o = _run_all(b, p, s, r, sig)
        assert np.array_equal(o["out_l"], o["out"])
        assert np.array_equal(o["out_r"], o["out"])

    def test_type_walls_on_the_new_jacks(self):
        patch = Patch()
        ps = patch.add_module("pitch_shifter")
        spk_l = patch.add_module("left_speaker_output")
        vca = patch.add_module("vca")
        patch.connect(ps.id, "out_l", spk_l.id, "in")       # audio -> audio
        with pytest.raises(Exception):
            patch.connect(ps.id, "out_r", vca.id, "cv")    # audio -> cv


class TestShimmer:
    def test_octaves_stack_that_feedback_zero_does_not_have(self):
        """+12 with feedback: the 220 Hz tone comes back at 440, and the
        440 goes round again to 880 and 1760. Without feedback there is
        exactly one octave. A two-render A/B, per the DSP test lesson."""
        sig = _tone(220.0, 2.0) * 0.5
        p, s, r, _, b = _rig({"semitones": 12.0})
        dry_loop = _run(b, p, s, r, sig)
        p, s, r, _, b = _rig({"semitones": 12.0, "feedback": 0.7})
        shimmer = _run(b, p, s, r, sig)
        for hz in (880.0, 1760.0):
            gain = _band_db(shimmer, hz) - _band_db(dry_loop, hz)
            assert gain > 12.0, (hz, gain)
        # The first octave is still the loudest partial of the cloud.
        assert _band_db(shimmer, 440.0) > _band_db(shimmer, 880.0)
        assert _band_db(shimmer, 880.0) > _band_db(shimmer, 1760.0)

    def test_hot_loop_stays_bounded(self):
        """feedback at the clamp on full-scale noise for 3 s: finite, and
        the soft ceiling + damping keep it near unity, not climbing."""
        rng = np.random.RandomState(3)
        sig = np.clip(rng.randn(int(3.0 * SR)), -1, 1).astype(np.float32)
        p, s, r, _, b = _rig({"semitones": 12.0, "feedback": 2.0})   # clamps to 0.9
        y = _run(b, p, s, r, sig)
        assert np.all(np.isfinite(y))
        assert np.abs(y).max() < 4.0
        first = np.sqrt(np.mean(y[SR // 2:SR] ** 2))
        last = np.sqrt(np.mean(y[-SR // 2:] ** 2))
        assert last < 2.0 * first + 0.1

    def test_dry_side_of_mix_is_the_raw_input_bit_exact(self):
        """mix=0 with the loop running is the delay-matched ORIGINAL --
        the dry tap reads the raw ring, never the recirculated one."""
        sig = _tone(300.0, 0.6)
        p, s, r, _, b = _rig({"semitones": 12.0, "mix": 0.0})
        plain = _run(b, p, s, r, sig)
        p, s, r, _, b = _rig({"semitones": 12.0, "mix": 0.0, "feedback": 0.8})
        looped = _run(b, p, s, r, sig)
        assert np.array_equal(plain, looped)

    def test_feedback_moves_the_sound_at_unison_too(self):
        """At 0 st the loop is a short comb/echo -- it must still DO
        something, i.e. the knob is wired (guards against a refactor
        that drops the recirculation on the r == 1 path)."""
        sig = _tone(440.0, 0.6)
        p, s, r, _, b = _rig({"semitones": 0.0})
        plain = _run(b, p, s, r, sig)
        p, s, r, _, b = _rig({"semitones": 0.0, "feedback": 0.6})
        looped = _run(b, p, s, r, sig)
        assert not np.array_equal(plain, looped)
        assert np.all(np.isfinite(looped))

    def test_voice_row_matches_mono_with_feedback(self):
        blocks = [np.random.RandomState(i).randn(F).astype(np.float32) * 0.4 for i in range(16)]
        p1, s1, r1, _, b1 = _rig({"semitones": 12.0, "feedback": 0.6, "mix": 0.7})
        mono = [b1._render_pitch_shifter(r1, F, {(s1.id, "out"): x}, p1)["out"] for x in blocks]
        p2, s2, r2, _, b2 = _rig({"semitones": 12.0, "feedback": 0.6, "mix": 0.7})
        for k, x in enumerate(blocks):
            v = b2._render_pitch_shifter(r2, F, {(s2.id, "out"): np.tile(x, (2, 1))}, p2)["out"]
            assert np.array_equal(v[1], mono[k]), k


class TestHarmonizer:
    def test_second_interval_appears_and_scales_with_level(self):
        """main +0 (unity), harmony +7 on a 220 Hz tone: 329.6 Hz shows
        up; at level 0.5 it is ~6 dB below level 1; at level 0 absent."""
        sig = _tone(220.0, 1.5) * 0.5
        fifth = 220.0 * 2 ** (7 / 12)
        levels = {}
        for lvl in (0.0, 0.5, 1.0):
            p, s, r, _, b = _rig({"semitones": 0.0, "harmony": 7.0, "harmony_level": lvl})
            y = _run(b, p, s, r, sig)
            levels[lvl] = _band_db(y, fifth)
        assert levels[1.0] - levels[0.0] > 30.0
        assert levels[1.0] - levels[0.5] == pytest.approx(6.02, abs=0.6)

    def test_harmony_engine_is_built_lazily_and_torn_down(self):
        p, s, r, _, b = _rig({"harmony": 7.0})
        x = np.random.RandomState(1).randn(F).astype(np.float32)
        b._render_pitch_shifter(r, F, {(s.id, "out"): x}, p)
        assert "eng2" not in b._state[r.id]
        r.set_param("harmony_level", 0.8)
        b._render_pitch_shifter(r, F, {(s.id, "out"): x}, p)
        assert "eng2" in b._state[r.id]
        r.set_param("harmony_level", 0.0)
        b._render_pitch_shifter(r, F, {(s.id, "out"): x}, p)
        assert "eng2" not in b._state[r.id]

    def test_pitch_cv_transposes_the_chord_together(self):
        """main +4, harmony +7, pitch_cv +1 (one octave): both partials
        land an octave above where they sat without CV."""
        sig = _tone(220.0, 1.5) * 0.5
        third = 220.0 * 2 ** (4 / 12)
        fifth = 220.0 * 2 ** (7 / 12)
        p, s, r, c, b = _rig({"semitones": 4.0, "harmony": 7.0, "harmony_level": 1.0}, with_cv=True)
        cv = np.ones_like(sig)
        y = _run(b, p, s, r, sig, cvsrc=c, cv=cv)
        for hz in (third, fifth):
            assert _band_db(y, 2 * hz) - _band_db(y, hz) > 20.0, hz

    def test_harmony_hears_the_raw_input_not_the_loop(self):
        """With shimmer running on the main (+12) and a harmony at +7,
        the harmony's partial does not itself climb: 329.6 Hz is there
        at the same level as without feedback, and a +7-over-the-first-
        lap partial (440 * 2^(7/12) = 659 Hz) -- which a harmony fed
        the loop WOULD put there at full level -- stays down at the
        shimmer's own noise floor, >= 60 dB under the harmony."""
        sig = _tone(220.0, 2.0) * 0.4
        fifth = 220.0 * 2 ** (7 / 12)
        lap_fifth = 440.0 * 2 ** (7 / 12)
        p, s, r, _, b = _rig({"semitones": 12.0, "harmony": 7.0, "harmony_level": 1.0})
        a = _run(b, p, s, r, sig)
        p, s, r, _, b = _rig({"semitones": 12.0, "harmony": 7.0, "harmony_level": 1.0, "feedback": 0.7})
        bb = _run(b, p, s, r, sig)
        assert _band_db(bb, fifth) == pytest.approx(_band_db(a, fifth), abs=1.5)
        assert _band_db(bb, fifth) - _band_db(bb, lap_fifth) > 60.0
        # ...while the main's cascade is present.
        assert _band_db(bb, 880.0) - _band_db(a, 880.0) > 12.0

    def test_formant_path_survives_a_harmony(self):
        sig = _tone(200.0, 1.0) * 0.5
        p, s, r, _, b = _rig({"semitones": 5.0, "harmony": 9.0, "harmony_level": 0.8,
                              "formant_preserve": True, "feedback": 0.3})
        y = _run(b, p, s, r, sig)
        assert np.all(np.isfinite(y))
        assert np.abs(y).max() < 4.0
        assert _band_db(y, 200.0 * 2 ** (9 / 12)) > -40.0

    def test_voice_row_matches_mono_with_the_pair(self):
        blocks = [np.random.RandomState(i).randn(F).astype(np.float32) * 0.4 for i in range(16)]
        params = {"semitones": 4.0, "harmony": 7.0, "harmony_level": 0.8, "spread": 0.6, "mix": 0.7}
        p1, s1, r1, _, b1 = _rig(params)
        mono = [b1._render_pitch_shifter(r1, F, {(s1.id, "out"): x}, p1) for x in blocks]
        p2, s2, r2, _, b2 = _rig(params)
        for k, x in enumerate(blocks):
            v = b2._render_pitch_shifter(r2, F, {(s2.id, "out"): np.tile(x, (2, 1))}, p2)
            for key in ("out", "out_l", "out_r"):
                assert np.array_equal(v[key][1], mono[k][key]), (k, key)


class TestSpread:
    def test_zero_aliases_and_one_hard_pans(self):
        """spread 1, mix 1, main +4 / harmony +7: out_l carries the
        third and none of the fifth, out_r the reverse; out is the sum."""
        sig = _tone(220.0, 1.5) * 0.5
        third = 220.0 * 2 ** (4 / 12)
        fifth = 220.0 * 2 ** (7 / 12)
        base = {"semitones": 4.0, "harmony": 7.0, "harmony_level": 1.0}
        p, s, r, _, b = _rig({**base, "spread": 0.0})
        o = _run_all(b, p, s, r, sig)
        assert np.array_equal(o["out_l"], o["out"]) and np.array_equal(o["out_r"], o["out"])
        p, s, r, _, b = _rig({**base, "spread": 1.0})
        o = _run_all(b, p, s, r, sig)
        assert _band_db(o["out_l"], third) - _band_db(o["out_l"], fifth) > 30.0
        assert _band_db(o["out_r"], fifth) - _band_db(o["out_r"], third) > 30.0
        assert np.allclose(o["out_l"] + o["out_r"], o["out"], atol=1e-4)

    def test_half_spread_fades_the_far_channel_by_half(self):
        sig = _tone(220.0, 1.5) * 0.5
        fifth = 220.0 * 2 ** (7 / 12)
        base = {"semitones": 4.0, "harmony": 7.0, "harmony_level": 1.0}
        p, s, r, _, b = _rig({**base, "spread": 0.5})
        o = _run_all(b, p, s, r, sig)
        # fifth in L is the harmony at 0.5 -> ~6 dB under its R level
        assert _band_db(o["out_r"], fifth) - _band_db(o["out_l"], fifth) == pytest.approx(6.02, abs=0.6)

    def test_dry_stays_centred(self):
        """mix 0.5, spread 1: the dry component is identical in L and R
        (subtract the wet-only renders and compare)."""
        sig = _tone(220.0, 1.0) * 0.5
        base = {"semitones": 4.0, "harmony": 7.0, "harmony_level": 1.0, "spread": 1.0}
        p, s, r, _, b = _rig({**base, "mix": 1.0})
        wet = _run_all(b, p, s, r, sig)
        p, s, r, _, b = _rig({**base, "mix": 0.5})
        half = _run_all(b, p, s, r, sig)
        dry_l = half["out_l"] - 0.5 * wet["out_l"]
        dry_r = half["out_r"] - 0.5 * wet["out_r"]
        assert np.allclose(dry_l, dry_r, atol=1e-5)
        assert np.abs(dry_l).max() > 0.1
