"""Tests for the CVToFrequency CV-controlled oscillator module (phase 1).

Coverage:
  - Model: registration, defaults, ports, signal kinds, JSON round-trip,
    a valid cv -> cv_to_frequency.cv cable, the audio out reaching a Speaker.
  - Mono renderer:
      * CV=0 / 0.5 / 1.0 produce tones at f0 / fm / f1 (zero-crossing count).
      * log mode at the lower-segment midpoint produces the geometric mean
        between f0 and fm; linear mode produces the arithmetic mean.
        Same intermediate CV value -> distinguishably different Hz.
      * Out-of-range CV (-0.5, 1.5) clamps to f0 / f1.
      * Unpatched CV falls back to the ``freq`` param.
      * Phase is continuous across blocks (no glitch at the boundary).
  - Voice-aware:
      * (V, F) CV in -> (V, F) audio out; each row carries its own
        per-voice frequency (FFT per row).
      * Mono CV preserves the 1D fast path.
  - Integration: bipolar LFO clamped into [0, 1] sweeps the frequency
    audibly through speaker mix without producing NaNs / Infs.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.patch import Cable
from pysynthrack.modules.cvtofrequency import CVToFrequency


def _zero_crossings(buf: np.ndarray) -> int:
    """Return zero-crossing count for a 1D buffer."""
    return int(np.sum(np.diff(np.signbit(buf)).astype(int)))


def _fft_peak_hz(buf: np.ndarray, sr: int) -> float:
    """Return the frequency (Hz) of the FFT peak of a 1D buffer."""
    spec = np.abs(np.fft.rfft(buf))
    freqs = np.fft.rfftfreq(buf.size, 1.0 / sr)
    return float(freqs[int(np.argmax(spec))])


# ----- Model -----------------------------------------------------------------


class TestCVToFrequencyModel:
    def test_register_and_defaults(self):
        patch = Patch()
        ctf = patch.add_module("cv_to_frequency")
        assert isinstance(ctf, CVToFrequency)
        assert ctf.params == {
            "waveform": "sine",
            "f0": 110.0,
            "fm": 440.0,
            "f1": 1760.0,
            "freq": 440.0,
            "mode": "log",
            "negative_enabled": False,
            "f0_neg": 110.0,
            "fm_neg": 440.0,
            "f1_neg": 1760.0,
            "mode_neg": "log",
        }

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        ctf = patch.add_module("cv_to_frequency")
        assert [p.name for p in ctf.input_ports] == ["cv"]
        assert ctf.input_ports[0].signal_kind == "cv"
        assert [p.name for p in ctf.output_ports] == ["out"]
        assert ctf.output_ports[0].signal_kind == "audio"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "cv_to_frequency",
            params={
                "waveform": "saw",
                "f0": 80.0,
                "fm": 200.0,
                "f1": 400.0,
                "freq": 220.0,
                "mode": "linear",
            },
        )
        restored = Patch.from_dict(patch.to_dict())
        ctf = next(m for m in restored if m.TYPE == "cv_to_frequency")
        assert ctf.params["waveform"] == "saw"
        assert ctf.params["f0"] == 80.0
        assert ctf.params["fm"] == 200.0
        assert ctf.params["f1"] == 400.0
        assert ctf.params["freq"] == 220.0
        assert ctf.params["mode"] == "linear"

    def test_unknown_param_rejected(self):
        # (phase 1 used negative_enabled as the impostor here; it became
        # a real param in phase 2, so the impostor moved on.)
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("cv_to_frequency", params={"negative_squelched": True})

    def test_cv_into_cv_input_accepted(self):
        """LFO.cv (cv) -> CVToFrequency.cv (cv): legal."""
        patch = Patch()
        lfo = patch.add_module("lfo")
        ctf = patch.add_module("cv_to_frequency")
        patch.connect(lfo.id, "cv", ctf.id, "cv")  # must not raise

    def test_audio_into_cv_input_rejected(self):
        """Oscillator.out (audio) -> CVToFrequency.cv (cv): type wall."""
        patch = Patch()
        osc = patch.add_module("oscillator")
        ctf = patch.add_module("cv_to_frequency")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", ctf.id, "cv")

    def test_audio_out_feeds_speaker(self):
        """CVToFrequency.out (audio) -> SpeakerOutput.in (audio) compiles."""
        patch = Patch()
        ctf = patch.add_module("cv_to_frequency")
        spk = patch.add_module("speaker_output")
        patch.connect(ctf.id, "out", spk.id, "in")


# ----- Mono behaviour --------------------------------------------------------


class TestCVToFrequencyMonoBehaviour:
    """Constant-CV mono renderer behaviour. One-second buffers at 44.1 kHz so
    zero-crossing counts have sub-percent resolution against the target."""

    def _make(self, params=None):
        sr = 44100
        patch = Patch()
        ctf = patch.add_module(
            "cv_to_frequency",
            params=params or {
                "f0": 110.0,
                "fm": 440.0,
                "f1": 1760.0,
                "freq": 440.0,
                "mode": "log",
            },
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        # Splice a synthetic CV cable in (the renderer uses cables_into
        # to find the input; the source module id is fictional here).
        patch.cables.append(Cable(77, "cv", ctf.id, "cv"))
        return sr, patch, ctf, backend

    def _render_with_cv(self, backend, ctf, patch, cv_value, sr):
        cv = np.full(sr, cv_value, dtype=np.float32)
        buffers = {(77, "cv"): cv}
        return backend._render_cv_to_frequency(ctf, sr, buffers, patch)

    def test_cv_zero_produces_f0(self):
        sr, patch, ctf, backend = self._make()
        buf = self._render_with_cv(backend, ctf, patch, 0.0, sr)
        # 110 Hz -> 220 zero crossings in 1 s. Defaults give exactly this.
        zc = _zero_crossings(buf)
        assert 218 <= zc <= 222

    def test_cv_half_produces_fm(self):
        sr, patch, ctf, backend = self._make()
        buf = self._render_with_cv(backend, ctf, patch, 0.5, sr)
        # 440 Hz -> ~880 zero crossings.
        zc = _zero_crossings(buf)
        assert 875 <= zc <= 885

    def test_cv_one_produces_f1(self):
        sr, patch, ctf, backend = self._make()
        buf = self._render_with_cv(backend, ctf, patch, 1.0, sr)
        # 1760 Hz -> ~3520 zero crossings.
        zc = _zero_crossings(buf)
        assert 3510 <= zc <= 3530

    def test_cv_clamps_negative_to_f0(self):
        sr, patch, ctf, backend = self._make()
        buf_clamped = self._render_with_cv(backend, ctf, patch, -0.5, sr)
        zc = _zero_crossings(buf_clamped)
        # Negative CV clamps to 0 in phase 1 -> f0=110.
        assert 218 <= zc <= 222

    def test_cv_clamps_above_one_to_f1(self):
        sr, patch, ctf, backend = self._make()
        buf_clamped = self._render_with_cv(backend, ctf, patch, 1.5, sr)
        zc = _zero_crossings(buf_clamped)
        # CV > 1 clamps to 1 -> f1=1760.
        assert 3510 <= zc <= 3530

    def test_unpatched_cv_falls_back_to_freq(self):
        """No cv cable -> oscillates at the ``freq`` fallback param."""
        sr = 44100
        patch = Patch()
        ctf = patch.add_module(
            "cv_to_frequency", params={"freq": 220.0}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        # No cable in patch.cables. _input_buffer returns None.
        buf = backend._render_cv_to_frequency(ctf, sr, {}, patch)
        zc = _zero_crossings(buf)
        # 220 Hz -> ~440 zero crossings.
        assert 435 <= zc <= 445

    def test_log_mode_midpoint_is_geometric_mean(self):
        """At cv=0.25 (lower-segment midpoint), log mode produces
        sqrt(f0 * fm). With f0=100, fm=400 that's 200 Hz."""
        sr, patch, ctf, backend = self._make(
            params={
                "f0": 100.0, "fm": 400.0, "f1": 1600.0,
                "freq": 440.0, "mode": "log",
            }
        )
        buf = self._render_with_cv(backend, ctf, patch, 0.25, sr)
        # 200 Hz -> ~400 zero crossings.
        assert 395 <= _zero_crossings(buf) <= 405

    def test_linear_mode_midpoint_is_arithmetic_mean(self):
        """At cv=0.25, linear mode produces (f0 + fm) / 2. With f0=100,
        fm=400 that's 250 Hz -- different from log mode's 200."""
        sr, patch, ctf, backend = self._make(
            params={
                "f0": 100.0, "fm": 400.0, "f1": 1600.0,
                "freq": 440.0, "mode": "linear",
            }
        )
        buf = self._render_with_cv(backend, ctf, patch, 0.25, sr)
        # 250 Hz -> ~500 zero crossings.
        assert 495 <= _zero_crossings(buf) <= 505

    def test_log_and_linear_modes_differ(self):
        """Direct A/B: same params, same CV, different mode -> different
        zero-crossing counts. Guards against an accidental mode-string
        passthrough or constant-fold collapsing the branches."""
        params_common = dict(
            f0=100.0, fm=400.0, f1=1600.0, freq=440.0,
        )
        sr_a, patch_a, ctf_a, backend_a = self._make(
            params={**params_common, "mode": "log"}
        )
        buf_log = self._render_with_cv(backend_a, ctf_a, patch_a, 0.25, sr_a)
        sr_b, patch_b, ctf_b, backend_b = self._make(
            params={**params_common, "mode": "linear"}
        )
        buf_lin = self._render_with_cv(backend_b, ctf_b, patch_b, 0.25, sr_b)
        zc_log = _zero_crossings(buf_log)
        zc_lin = _zero_crossings(buf_lin)
        # Log: ~400 zc, Linear: ~500 zc. Gap must be unambiguous.
        assert zc_lin - zc_log >= 50, (
            f"log/linear modes too close: log={zc_log}, linear={zc_lin}"
        )

    def test_phase_continuous_across_blocks(self):
        """Per-sample frequency integration must not glitch on the
        block boundary -- phase state has to carry across calls."""
        sr = 44100
        block = 512
        patch = Patch()
        ctf = patch.add_module("cv_to_frequency")
        backend = NumpyBackend(sample_rate=sr, block_size=block)
        backend.compile(patch)
        patch.cables.append(Cable(77, "cv", ctf.id, "cv"))
        cv = np.full(block, 0.5, dtype=np.float32)
        buffers = {(77, "cv"): cv}
        b1 = backend._render_cv_to_frequency(ctf, block, buffers, patch)
        b2 = backend._render_cv_to_frequency(ctf, block, buffers, patch)
        # Per-sample delta at 440 Hz is tiny; the boundary jump should
        # be of the same order, not a full half-cycle.
        assert abs(float(b2[0]) - float(b1[-1])) < 0.1

    def test_output_is_audible_no_nans(self):
        """Sanity: no NaN/Inf, output has nonzero energy at CV=0.5."""
        sr, patch, ctf, backend = self._make()
        buf = self._render_with_cv(backend, ctf, patch, 0.5, sr)
        assert np.isfinite(buf).all()
        # Sine at fm with unit amplitude -> RMS ~0.707.
        rms = float(np.sqrt(np.mean(buf.astype(np.float64) ** 2)))
        assert rms > 0.5


# ----- Voice-aware behaviour -------------------------------------------------


class TestCVToFrequencyVoiceAware:
    """Voice-aware path: (V, F) CV in -> (V, F) audio out, each row at
    its own per-voice frequency."""

    def test_voice_shape_preserved(self):
        sr = 44100
        block = 4096
        V, F = 16, block
        patch = Patch()
        ctf = patch.add_module(
            "cv_to_frequency",
            params={"f0": 110.0, "fm": 440.0, "f1": 1760.0, "mode": "log"},
        )
        backend = NumpyBackend(sample_rate=sr, block_size=block)
        backend.compile(patch)
        patch.cables.append(Cable(77, "cv", ctf.id, "cv"))

        # Voice 0 sits at CV=0 (f0), voice 5 at CV=0.5 (fm), voice 10 at
        # CV=1.0 (f1). Remaining voices at CV=0 (silently advance at f0;
        # in real patches a VCA/ADSR would gate these).
        cv = np.zeros((V, F), dtype=np.float32)
        cv[5, :] = 0.5
        cv[10, :] = 1.0
        buffers = {(77, "cv"): cv}
        out = backend._render_cv_to_frequency(ctf, F, buffers, patch)

        assert out.shape == (V, F)
        assert np.isfinite(out).all()

        bin_hz = sr / block

        # Voice 0 should peak near f0=110 Hz.
        peak0 = _fft_peak_hz(out[0], sr)
        assert abs(peak0 - 110.0) <= bin_hz * 1.5

        # Voice 5 should peak near fm=440 Hz.
        peak5 = _fft_peak_hz(out[5], sr)
        assert abs(peak5 - 440.0) <= bin_hz * 1.5

        # Voice 10 should peak near f1=1760 Hz.
        peak10 = _fft_peak_hz(out[10], sr)
        assert abs(peak10 - 1760.0) <= bin_hz * 1.5

    def test_mono_cv_keeps_mono_output(self):
        """1D CV in -> 1D audio out. Mono fast path is not accidentally
        promoted to voice shape by the renderer."""
        sr = 44100
        block = 512
        patch = Patch()
        ctf = patch.add_module("cv_to_frequency")
        backend = NumpyBackend(sample_rate=sr, block_size=block)
        backend.compile(patch)
        patch.cables.append(Cable(77, "cv", ctf.id, "cv"))
        cv = np.full(block, 0.5, dtype=np.float32)
        out = backend._render_cv_to_frequency(ctf, block, {(77, "cv"): cv}, patch)
        assert out.ndim == 1
        assert out.shape == (block,)

    def test_per_voice_phase_state_independence(self):
        """Two consecutive blocks: each voice integrates its own phase,
        and a row at a higher frequency advances further between blocks
        than a row at a lower frequency. The phase state is per-voice,
        not shared."""
        sr = 44100
        block = 512
        V, F = 16, block
        patch = Patch()
        ctf = patch.add_module(
            "cv_to_frequency",
            params={"f0": 100.0, "fm": 400.0, "f1": 1600.0, "mode": "log"},
        )
        backend = NumpyBackend(sample_rate=sr, block_size=block)
        backend.compile(patch)
        patch.cables.append(Cable(77, "cv", ctf.id, "cv"))

        cv = np.zeros((V, F), dtype=np.float32)
        cv[0, :] = 0.0   # f0 = 100 Hz, slowest
        cv[1, :] = 1.0   # f1 = 1600 Hz, fastest
        buffers = {(77, "cv"): cv}

        b1 = backend._render_cv_to_frequency(ctf, F, buffers, patch)
        b2 = backend._render_cv_to_frequency(ctf, F, buffers, patch)

        # If phase carried across, voice 1's b2[0] should be exactly the
        # continuation of voice 1's phase trajectory -- comparable in
        # amplitude to b1's tail (no glitch back to zero), and definitely
        # not equal to voice 0's b2[0] (which integrates at 1/16 the rate).
        # Cheap diagnostic: per-voice b2[0] vs b1[-1] gap should be small.
        gap_slow = abs(float(b2[0, 0]) - float(b1[0, -1]))
        gap_fast = abs(float(b2[1, 0]) - float(b1[1, -1]))
        assert gap_slow < 0.2, f"slow voice phase jump too large: {gap_slow}"
        assert gap_fast < 0.5, f"fast voice phase jump too large: {gap_fast}"


# ----- Integration -----------------------------------------------------------


class TestCVToFrequencyIntegration:
    def test_lfo_sweep_produces_finite_audible_output(self):
        """LFO (unipolar 5 Hz) -> CVToFrequency -> Speaker. The unipolar
        LFO swings CV between 0 and 1, sweeping pitch across f0..f1.
        Verify the full chain compiles, renders without NaN/Inf, and the
        speaker output is non-silent and bounded."""
        sr = 44100
        block = 4096
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={
                "waveform": "sine",
                "rate": 5.0,
                "depth": 1.0,
                "bipolar": False,
            },
        )
        ctf = patch.add_module(
            "cv_to_frequency",
            params={
                "f0": 110.0, "fm": 440.0, "f1": 1760.0, "mode": "log",
            },
        )
        spk = patch.add_module("speaker_output", params={"gain": 0.5})
        patch.connect(lfo.id, "cv", ctf.id, "cv")
        patch.connect(ctf.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=block)
        backend.compile(patch)
        out = backend.render_block(block)

        assert out.shape == (block, 2)
        assert np.isfinite(out).all()
        # Pitch-sweeping sine has substantial RMS.
        rms = float(np.sqrt(np.mean(out[:, 0].astype(np.float64) ** 2)))
        assert rms > 0.1, f"output too quiet: rms={rms}"
        # Bounded by speaker clipper at +/- 1.0.
        assert float(np.max(np.abs(out))) <= 1.0


# ----- Phase 2: negative-side mirror -----------------------------------------


class TestCVToFrequencyPhase2:
    """Negative-side mirror mapping (negative_enabled, *_neg params).

    Same constant-CV / fictional-cable technique as the mono behaviour
    class: one-second buffers so zero-crossing counts resolve Hz to
    sub-percent accuracy.
    """

    PARAMS = {
        "f0": 220.0,
        "fm": 440.0,
        "f1": 880.0,
        "mode": "log",
        "negative_enabled": True,
        "f0_neg": 330.0,
        "fm_neg": 550.0,
        "f1_neg": 1100.0,
        "mode_neg": "log",
    }

    def _make(self, params=None):
        sr = 44100
        patch = Patch()
        ctf = patch.add_module(
            "cv_to_frequency", params=dict(params if params is not None else self.PARAMS)
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        patch.cables.append(Cable(77, "cv", ctf.id, "cv"))
        return sr, patch, ctf, backend

    def _hz_at_cv(self, cv_value, params=None):
        sr, patch, ctf, backend = self._make(params)
        cv = np.full(sr, cv_value, dtype=np.float32)
        buffers = {(77, "cv"): cv}
        buf = backend._render_cv_to_frequency(ctf, sr, buffers, patch)
        return _zero_crossings(buf) / 2.0

    def test_disabled_ignores_negative_curve(self):
        """negative_enabled=False: cv=-1 clamps to f0 even with a
        loud f1_neg configured — phase-1 behaviour is the default."""
        params = dict(self.PARAMS, negative_enabled=False)
        assert abs(self._hz_at_cv(-1.0, params) - 220.0) < 3.0

    def test_negative_full_produces_f1_neg(self):
        assert abs(self._hz_at_cv(-1.0) - 1100.0) < 5.0

    def test_negative_half_produces_fm_neg(self):
        assert abs(self._hz_at_cv(-0.5) - 550.0) < 5.0

    def test_zero_belongs_to_positive_side(self):
        """CV exactly 0 -> f0, not f0_neg (the documented snap rule)."""
        assert abs(self._hz_at_cv(0.0) - 220.0) < 3.0

    def test_just_below_zero_lands_near_f0_neg(self):
        """CV=-1/256 sits a hair down the negative curve: ~f0_neg."""
        hz = self._hz_at_cv(-1.0 / 256.0)
        assert abs(hz - 330.0) < 8.0

    def test_negative_clamps_below_minus_one(self):
        assert abs(self._hz_at_cv(-1.5) - 1100.0) < 5.0

    def test_positive_side_unchanged_when_enabled(self):
        assert abs(self._hz_at_cv(0.5) - 440.0) < 5.0
        assert abs(self._hz_at_cv(1.0) - 880.0) < 5.0

    def test_mixed_modes_are_independent(self):
        """mode=log / mode_neg=linear: the lower-segment midpoint is the
        geometric mean on the positive side but the arithmetic mean on
        the negative side, from the same anchor spacing."""
        params = dict(
            self.PARAMS,
            f0=200.0, fm=800.0, f1=1600.0, mode="log",
            f0_neg=200.0, fm_neg=800.0, f1_neg=1600.0, mode_neg="linear",
        )
        hz_pos = self._hz_at_cv(0.25, params)
        hz_neg = self._hz_at_cv(-0.25, params)
        assert abs(hz_pos - 400.0) < 6.0   # sqrt(200*800) = 400 (geometric)
        assert abs(hz_neg - 500.0) < 6.0   # (200+800)/2  = 500 (arithmetic)
        assert abs(hz_pos - hz_neg) > 50.0  # measurably different curves

    def test_zero_crossing_step_is_deliberate(self):
        """f0 != f0_neg produces a hard frequency step across CV=0."""
        params = dict(self.PARAMS, f0=220.0, f0_neg=660.0)
        just_above = self._hz_at_cv(1.0 / 256.0, params)
        just_below = self._hz_at_cv(-1.0 / 256.0, params)
        assert abs(just_above - 220.0) < 8.0
        assert abs(just_below - 660.0) < 12.0

    def test_json_round_trip_phase2(self):
        patch = Patch()
        patch.add_module("cv_to_frequency", params=dict(self.PARAMS))
        restored = Patch.from_dict(patch.to_dict())
        ctf = next(m for m in restored if m.TYPE == "cv_to_frequency")
        assert ctf.params["negative_enabled"] is True
        assert ctf.params["f0_neg"] == 330.0
        assert ctf.params["fm_neg"] == 550.0
        assert ctf.params["f1_neg"] == 1100.0
        assert ctf.params["mode_neg"] == "log"

    def test_voice_aware_bipolar_rows(self):
        """(V, F) CV with one row at +1 and one at -1: per-row FFT peaks
        land on f1 and f1_neg respectively."""
        sr, patch, ctf, backend = self._make()
        cv = np.stack([
            np.full(sr, 1.0, dtype=np.float32),
            np.full(sr, -1.0, dtype=np.float32),
        ])
        buffers = {(77, "cv"): cv}
        buf = backend._render_cv_to_frequency(ctf, sr, buffers, patch)
        assert buf.shape == (2, sr)
        assert abs(_fft_peak_hz(buf[0], sr) - 880.0) < 5.0
        assert abs(_fft_peak_hz(buf[1], sr) - 1100.0) < 5.0

    def test_bipolar_sweep_finite_and_audible(self):
        """Full-range bipolar ramp through both curves: no NaNs/Infs,
        nonzero signal, phase accumulator survives the sign flips."""
        sr, patch, ctf, backend = self._make()
        cv = np.linspace(-1.2, 1.2, sr, dtype=np.float32)
        buffers = {(77, "cv"): cv}
        buf = backend._render_cv_to_frequency(ctf, sr, buffers, patch)
        assert np.isfinite(buf).all()
        assert np.std(buf) > 0.1
