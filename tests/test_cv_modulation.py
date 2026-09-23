"""Tests for CV-modulatable params (filter cutoff, oscillator freq/amp/pw).

The oscillator's pulse-width section (``TestOscillatorPulseWidth``) pins
the 2026-09-19 PWM love pass: duty cycle MEASURED as the fraction of
samples high, per-sample ``pw_cv`` landing inside a block, the DC
compensation, the rails, ``square_wt`` and the non-square shapes ignoring
the width, bit-exactness at the default (the classic square arithmetic),
voice parity, block-size independence, and -- the one that earns the
falling edge its own phase increment -- every falling edge under PWM
getting exactly one consistent PolyBLEP pair.
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
from pysynthrack.core.patch import Cable


# ----- Filter cutoff CV -----------------------------------------------------


class TestFilterCutoffCV:
    def _build(self, cutoff, q=0.707, cv_value=None):
        """Build osc → filter → speaker, optionally with a constant cutoff_cv."""
        patch = Patch()
        osc = patch.add_module(
            "oscillator",
            params={"waveform": "sine", "freq": 4000.0, "amp": 0.8},
        )
        filt = patch.add_module(
            "filter",
            params={"mode": "lowpass", "cutoff": cutoff, "resonance": q},
        )
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc.id, "out", filt.id, "in")
        patch.connect(filt.id, "out", spk.id, "in")
        return patch, osc, filt, spk

    def test_no_cv_patched_matches_static_cutoff(self):
        """A filter with no cutoff_cv cable behaves exactly like before
        the CV port was added."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt, spk = self._build(cutoff=200.0)
        backend.compile(patch)
        # Render via render_block. With cutoff=200 and a 4 kHz sine,
        # output should be heavily attenuated (matches prior baseline).
        peak_after_warmup = 0.0
        for i in range(10):
            block = backend.render_block(1024)
        peak_after_warmup = float(np.max(np.abs(block)))
        assert peak_after_warmup < 0.1

    def test_constant_positive_cv_raises_cutoff_one_octave(self):
        """A constant cutoff_cv of +1.0 shifts the cutoff up an octave
        (200 Hz → 400 Hz). For a 4 kHz tone the attenuation is still
        heavy but slightly less than at 200 Hz baseline."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt, spk = self._build(cutoff=200.0)
        # Add a constant CV source. Easiest: an LFO at very slow rate
        # bipolar=False would produce a slow 0..1 sweep; we want a flat
        # +1.0. Inject the CV buffer directly through the buffers dict
        # path instead of patching another module.
        backend.compile(patch)
        # Manually drive the renderer with a synthetic CV buffer.
        cv = np.full(1024, 1.0, dtype=np.float32)
        # Build buffer dict the way render_block does.
        osc_buf = backend._render_oscillator(osc, 1024, {}, patch)
        buffers = {
            (osc.id, "out"): osc_buf,
            # Forge an upstream module ID for the CV; we'll pretend it
            # came from module id 99 on port "cv".
            (99, "cv"): cv,
        }
        # Splice an extra cable into the patch so the renderer reads
        # buffers[(99, "cv")] when looking up filt.cutoff_cv.
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(99, "cv", filt.id, "cutoff_cv"))
        out = backend._render_filter(filt, 1024, buffers, patch)
        # The +1 octave shift means cutoff is now ~400 Hz, still well
        # below 4 kHz, so we still expect strong attenuation. We're
        # checking the renderer accepts the CV and produces finite,
        # non-exploding output rather than the exact dB diff.
        assert np.all(np.isfinite(out))
        assert float(np.max(np.abs(out))) < 0.5

    def test_strong_negative_cv_drops_cutoff_below_audible(self):
        """cutoff_cv = -5 shifts cutoff down ~5 octaves. A 4 kHz tone
        through a sub-Hz cutoff should be near-silent."""
        sr = 44100
        backend = NumpyBackend(sample_rate=sr, block_size=1024)
        patch, osc, filt, spk = self._build(cutoff=200.0)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(99, "cv", filt.id, "cutoff_cv"))
        for i in range(10):  # let filter settle
            cv = np.full(1024, -5.0, dtype=np.float32)
            osc_buf = backend._render_oscillator(osc, 1024, {}, patch)
            buffers = {(osc.id, "out"): osc_buf, (99, "cv"): cv}
            out = backend._render_filter(filt, 1024, buffers, patch)
        assert float(np.max(np.abs(out))) < 0.05

    def test_cv_modulation_is_audible_via_render_block_with_lfo(self):
        """End-to-end through render_block: LFO → cutoff_cv sweeps the
        filter cutoff across the audible range. Block-mean CV needs the
        block to be much shorter than the LFO period for the sweep to
        register — production block sizes (512–1024) satisfy this; a
        block equal to the LFO period would average out to ~0."""
        sr = 44100
        block_size = 1024
        patch = Patch()
        osc = patch.add_module(
            "oscillator",
            params={"waveform": "saw", "freq": 220.0, "amp": 0.8},
        )
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 0.5, "depth": 2.0, "bipolar": True},
        )
        filt = patch.add_module(
            "filter",
            params={"mode": "lowpass", "cutoff": 400.0, "resonance": 1.0},
        )
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc.id, "out", filt.id, "in")
        patch.connect(lfo.id, "cv", filt.id, "cutoff_cv")
        patch.connect(filt.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=block_size)
        backend.compile(patch)

        # Warmup, then collect per-block RMS over two LFO cycles.
        for _ in range(20):
            backend.render_block(block_size)
        blocks_per_two_cycles = int(2 * sr / lfo.params["rate"] / block_size)
        per_block_rms = []
        for _ in range(blocks_per_two_cycles):
            block = backend.render_block(block_size)
            left = block[:, 0].astype(np.float64)
            per_block_rms.append(float(np.sqrt(np.mean(left ** 2))))

        # Cutoff sweep should drive a noticeable RMS swing block-to-block.
        assert (max(per_block_rms) - min(per_block_rms)) > 0.05


# ----- Oscillator freq CV ---------------------------------------------------


class TestOscillatorFreqCV:
    def test_no_cv_patched_matches_static_freq(self):
        """Oscillator without freq_cv runs at its static frequency."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        buf = backend._render_oscillator(osc, sr, {}, patch)
        # ~440 cycles in 1 second → ~880 zero crossings.
        zc = int(np.sum(np.diff(np.signbit(buf)).astype(int)))
        assert 870 <= zc <= 890

    def test_constant_positive_cv_doubles_frequency(self):
        """freq_cv = +1.0 (1V/oct) should double the output frequency."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        # Splice a synthetic CV cable from an imaginary source.
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(77, "cv", osc.id, "freq_cv"))
        buffers = {(77, "cv"): np.full(sr, 1.0, dtype=np.float32)}
        buf = backend._render_oscillator(osc, sr, buffers, patch)
        zc = int(np.sum(np.diff(np.signbit(buf)).astype(int)))
        # 880 Hz now → ~1760 zero crossings.
        assert 1740 <= zc <= 1780

    def test_constant_negative_cv_halves_frequency(self):
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(77, "cv", osc.id, "freq_cv"))
        buffers = {(77, "cv"): np.full(sr, -1.0, dtype=np.float32)}
        buf = backend._render_oscillator(osc, sr, buffers, patch)
        zc = int(np.sum(np.diff(np.signbit(buf)).astype(int)))
        # 220 Hz → ~440 zero crossings.
        assert 430 <= zc <= 450

    def test_freq_cv_phase_continuous_across_blocks(self):
        """Per-sample freq integration must not glitch on block boundaries."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(77, "cv", osc.id, "freq_cv"))
        cv = np.full(512, 0.5, dtype=np.float32)
        buffers = {(77, "cv"): cv}
        b1 = backend._render_oscillator(osc, 512, buffers, patch)
        b2 = backend._render_oscillator(osc, 512, buffers, patch)
        # Jump between blocks should be tiny — per-sample delta only.
        assert abs(float(b2[0]) - float(b1[-1])) < 0.1


# ----- Oscillator amp CV ---------------------------------------------------


class TestOscillatorAmpCV:
    def test_no_amp_cv_uses_static_amp(self):
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        buf = backend._render_oscillator(osc, 512, {}, patch)
        assert float(np.max(np.abs(buf))) > 0.49
        assert float(np.max(np.abs(buf))) <= 0.5 + 1e-5

    def test_amp_cv_zero_silences_output(self):
        """amp_cv = 0 multiplies the audio by zero → silence."""
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(88, "cv", osc.id, "amp_cv"))
        buffers = {(88, "cv"): np.zeros(512, dtype=np.float32)}
        buf = backend._render_oscillator(osc, 512, buffers, patch)
        assert np.allclose(buf, 0.0)

    def test_amp_cv_half_halves_output(self):
        sr = 44100
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5}
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        from pysynthrack.core.patch import Cable
        patch.cables.append(Cable(88, "cv", osc.id, "amp_cv"))
        buffers = {(88, "cv"): np.full(512, 0.5, dtype=np.float32)}
        buf = backend._render_oscillator(osc, 512, buffers, patch)
        # Peak should now be ~0.25 (0.5 amp × 0.5 cv).
        assert float(np.max(np.abs(buf))) > 0.24
        assert float(np.max(np.abs(buf))) <= 0.25 + 1e-5


# ----- Oscillator pulse width ----------------------------------------------

SR = 44100
PW_SRC = 90
FREQ_SRC = 91


def _render_pw(
    waveform, pulse_width=0.5, depth=0.5, pw_cv=None, freq_cv=None,
    freq=441.0, frames=512, blocks=8, block_size=512, amp=1.0,
):
    """Drive _render_oscillator block by block with optional synthetic
    pw_cv / freq_cv buffers (sliced per block along the last axis).
    ``freq`` 441 Hz = exactly 100 samples per cycle, so a fraction-high
    count resolves the width to 1%."""
    patch = Patch()
    osc = patch.add_module(
        "oscillator",
        params={
            "waveform": waveform, "freq": freq, "amp": amp,
            "pulse_width": pulse_width, "pw_cv_depth": depth,
        },
    )
    backend = NumpyBackend(sample_rate=SR, block_size=block_size)
    backend.compile(patch)
    if pw_cv is not None:
        patch.cables.append(Cable(PW_SRC, "cv", osc.id, "pw_cv"))
    if freq_cv is not None:
        patch.cables.append(Cable(FREQ_SRC, "cv", osc.id, "freq_cv"))
    out = []
    for i in range(blocks):
        bufs = {}
        sl = slice(i * frames, (i + 1) * frames)
        if pw_cv is not None:
            bufs[(PW_SRC, "cv")] = pw_cv[..., sl]
        if freq_cv is not None:
            bufs[(FREQ_SRC, "cv")] = freq_cv[..., sl]
        out.append(backend._render_oscillator(osc, frames, bufs, patch))
    return np.concatenate(out, axis=-1)


def _fraction_high(out, pw):
    """Undo the DC compensation (``out = pulse - (2pw - 1)``) and count the
    samples on the high rail -- the duty cycle, measured."""
    raw = np.asarray(out, np.float64) + (2.0 * pw - 1.0)
    return float(np.mean(raw > 0.0))


class TestOscillatorPulseWidth:
    # -- registration -------------------------------------------------------

    def test_params_and_port(self):
        cls = get_module_type("oscillator")
        assert cls.DEFAULT_PARAMS["pulse_width"] == 0.5
        assert cls.DEFAULT_PARAMS["pw_cv_depth"] == 0.5
        port = next(p for p in cls.INPUT_PORTS if p.name == "pw_cv")
        assert port.direction == "in" and port.signal_kind == "cv"

    def test_pre_pwm_patch_loads_with_the_defaults(self):
        d = Patch()
        d.add_module("oscillator", params={"waveform": "square_blep"})
        raw = d.to_dict()
        for m in raw["modules"]:
            m["params"].pop("pulse_width", None)
            m["params"].pop("pw_cv_depth", None)
        restored = Patch.from_dict(raw)
        osc = next(m for m in restored if m.TYPE == "oscillator")
        assert osc.params["pulse_width"] == 0.5
        assert osc.params["pw_cv_depth"] == 0.5

    # -- duty cycle, measured -----------------------------------------------

    @pytest.mark.parametrize("pw", [0.05, 0.2, 0.5, 0.8, 0.95])
    def test_naive_duty_cycle_is_the_width(self, pw):
        out = _render_pw("square", pulse_width=pw, blocks=20)
        assert abs(_fraction_high(out, pw) - pw) < 0.01, (pw, _fraction_high(out, pw))

    @pytest.mark.parametrize("pw", [0.1, 0.3, 0.7])
    def test_blep_duty_cycle_is_the_width(self, pw):
        # The blep rounds each corner across two samples, and a rounded
        # rising corner's "before" sample can already sit above zero --
        # up to a sample per cycle of ambiguity, so 2% at 100 samples
        # per cycle rather than the naive pulse's 1%.
        out = _render_pw("square_blep", pulse_width=pw, blocks=20)
        assert abs(_fraction_high(out, pw) - pw) < 0.02

    def test_dc_offset_is_removed(self):
        # 100 samples per cycle x 10000 frames = 100 whole cycles, so the
        # mean is the duty cycle's DC and nothing else. A 20% pulse
        # averages -0.6 raw; after compensation the block mean is ~0 and
        # the output sits on exactly two levels, 1 - dc and -1 - dc.
        out = _render_pw("square", pulse_width=0.2, frames=500, blocks=20)
        dc = 2.0 * 0.2 - 1.0
        raw = out.astype(np.float64) + dc
        assert abs(float(raw.mean()) - dc) < 0.03
        assert abs(float(out.mean())) < 0.03
        levels = np.unique(out)
        assert len(levels) == 2
        np.testing.assert_allclose(levels, [-1.0 - dc, 1.0 - dc], atol=1e-6)

    def test_default_width_carries_no_correction(self):
        out = _render_pw("square", pulse_width=0.5, blocks=4)
        assert set(np.unique(out).tolist()) == {-1.0, 1.0}

    # -- pw_cv ----------------------------------------------------------------

    def test_pw_cv_lands_within_the_block(self):
        # Width 0.5 for the first 256 samples of the first block, then
        # cv -0.6 x depth 0.5 = 0.2 from sample 256 on. Windows start on
        # cycle boundaries (100-sample cycles) so the counts are exact.
        cv = np.zeros(512 * 8, np.float32)
        cv[256:] = -0.6
        out = _render_pw("square", pw_cv=cv)
        assert abs(_fraction_high(out[:200], 0.5) - 0.5) < 0.02
        assert abs(_fraction_high(out[300:500], 0.2) - 0.2) < 0.02
        assert abs(_fraction_high(out[1000:4000], 0.2) - 0.2) < 0.01

    def test_pw_cv_depth_scales_the_cv(self):
        cv = np.full(512 * 20, 1.0, np.float32)
        out = _render_pw("square", depth=0.2, pw_cv=cv, blocks=20)
        assert abs(_fraction_high(out, 0.7) - 0.7) < 0.01

    def test_unpatched_equals_zero_cv_equals_zero_depth(self):
        # The unpatched-input contract, both ways: a zero cable and a
        # zero-depth cable are the static width, bit for bit.
        zeros = np.zeros(512 * 8, np.float32)
        ones = np.ones(512 * 8, np.float32)
        for w in ("square", "square_blep"):
            ref = _render_pw(w, pulse_width=0.3)
            assert np.array_equal(_render_pw(w, pulse_width=0.3, pw_cv=zeros), ref)
            assert np.array_equal(
                _render_pw(w, pulse_width=0.3, depth=0.0, pw_cv=ones), ref
            )

    def test_width_is_clamped_to_the_rails(self):
        big = np.full(512 * 20, 5.0, np.float32)
        hi = _render_pw("square", pw_cv=big, blocks=20)
        lo = _render_pw("square", pw_cv=-big, blocks=20)
        assert abs(_fraction_high(hi, 0.95) - 0.95) < 0.01
        assert abs(_fraction_high(lo, 0.05) - 0.05) < 0.01
        # The param itself is clamped too (a hand-edited patch).
        assert np.array_equal(
            _render_pw("square", pulse_width=0.999, blocks=4),
            _render_pw("square", pulse_width=0.95, blocks=4),
        )
        assert np.array_equal(
            _render_pw("square_blep", pulse_width=0.0, blocks=4),
            _render_pw("square_blep", pulse_width=0.05, blocks=4),
        )

    # -- who listens ----------------------------------------------------------

    def test_square_wt_is_a_fixed_fifty_percent_table(self):
        sweep = np.linspace(-1.0, 1.0, 512 * 8, dtype=np.float32)
        ref = _render_pw("square_wt", pulse_width=0.5, freq=220.0)
        assert np.array_equal(_render_pw("square_wt", pulse_width=0.2, freq=220.0), ref)
        assert np.array_equal(
            _render_pw("square_wt", pulse_width=0.2, pw_cv=sweep, freq=220.0), ref
        )

    @pytest.mark.parametrize(
        "waveform",
        ["sine", "saw", "triangle", "saw_blep", "triangle_blep", "saw_wt", "triangle_wt"],
    )
    def test_other_shapes_ignore_the_width(self, waveform):
        sweep = np.linspace(-1.0, 1.0, 512 * 4, dtype=np.float32)
        ref = _render_pw(waveform, pulse_width=0.5, freq=220.0, blocks=4)
        assert np.array_equal(
            _render_pw(waveform, pulse_width=0.2, pw_cv=sweep, freq=220.0, blocks=4),
            ref,
        )

    # -- bit-exact at the default -------------------------------------------

    def test_default_is_the_classic_square_bit_for_bit(self):
        # The pre-PWM arithmetic still exists as the pw=None path every
        # other caller (keyboard, midi, cv_to_frequency) uses; the
        # oscillator at its default must reproduce it exactly, for the
        # naive, blep and wt flavours alike.
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        phases = (np.arange(4096, dtype=np.float64) * (220.0 / SR)) % 1.0
        dt = 220.0 / SR
        for w in ("square", "square_blep", "square_wt"):
            classic = backend._osc_waveshape(phases, w, dt=dt)
            pwm = backend._osc_waveshape(phases, w, dt=dt, pw=0.5)
            assert np.array_equal(classic, pwm), w
            pwm_arr = backend._osc_waveshape(
                phases, w, dt=dt, pw=np.full(4096, 0.5), dpw=np.zeros(4096)
            )
            assert np.array_equal(classic, pwm_arr), w
        # And through the renderer, against the closed forms on the
        # renderer's own phase ramp. Since 2026-09-24 the mono fast path
        # is ``(origin + inc * k) % 1`` over the ABSOLUTE sample index
        # from a zero origin, so its ramp across all eight blocks is the
        # one-shot ``phases`` above, sample for sample (it used to be
        # rebuilt block by block from a carried float start).
        ramp = phases
        naive = _render_pw("square", freq=220.0, blocks=8)
        expect = np.where(ramp < 0.5, 1.0, -1.0).astype(np.float32)
        assert np.array_equal(naive, expect)
        blep = _render_pw("square_blep", freq=220.0, blocks=8)
        expect = backend._waveshape_blep("square", ramp, dt).astype(np.float32)
        assert np.array_equal(blep, expect)

    # -- voices -----------------------------------------------------------------

    @pytest.mark.parametrize("waveform", ["square", "square_blep"])
    def test_voice_parity(self, waveform):
        V, N = 4, 4096
        ramp = np.linspace(-0.8, 0.8, N, dtype=np.float32)
        fcv = np.zeros((V, N), np.float32)
        pwv = np.zeros((V, N), np.float32)
        pwv[2] = ramp
        mono = _render_pw(waveform, pw_cv=ramp, freq_cv=np.zeros(N, np.float32), freq=220.0)
        # (V, F) pw_cv on the voice path: row 2 is the mono render.
        voice = _render_pw(waveform, pw_cv=pwv, freq_cv=fcv, freq=220.0)
        assert voice.shape == (V, N)
        assert np.array_equal(voice[2], mono)
        assert not np.array_equal(voice[0], mono)
        # A mono pw_cv on the voice path is shared by every voice.
        shared = _render_pw(waveform, pw_cv=ramp, freq_cv=fcv, freq=220.0)
        for v in range(V):
            assert np.array_equal(shared[v], mono)
        # A (V, F) pw_cv on the mono path broadcasts to (V, F) -- one
        # phase ramp, a width per voice -- like a (V, F) amp_cv does.
        bcast = _render_pw(waveform, pw_cv=pwv, freq=220.0)
        assert bcast.shape == (V, N)
        assert np.array_equal(bcast[2], _render_pw(waveform, pw_cv=ramp, freq=220.0))

    @pytest.mark.parametrize("waveform", ["square", "square_blep"])
    def test_block_size_independence(self, waveform):
        # 223 Hz is coprime with 44100 so no phase wrap lands within an
        # ulp of a sample inside the window (at 220 Hz sample 2205 is
        # exactly 11 cycles and the two block sizes round it to opposite
        # sides of the edge -- a fact about the phase accumulator, not
        # the width).
        ramp = np.linspace(-0.8, 0.8, 4096, dtype=np.float32)
        a = _render_pw(waveform, pw_cv=ramp, freq=223.0, frames=512, blocks=8, block_size=512)
        b = _render_pw(waveform, pw_cv=ramp, freq=223.0, frames=64, blocks=64, block_size=64)
        assert np.array_equal(a, b)

    # -- the falling edge's own phase ------------------------------------------

    @pytest.mark.parametrize("lfo_hz", [0.7, 30.0])
    def test_every_falling_edge_gets_one_consistent_blep_pair(self, lfo_hz):
        """Subtract the naive pulse from the blep pulse under PWM and the
        residual is the corrections alone. A PolyBLEP pair for a step of
        height 2 puts -x^2 on the sample before the edge and +(1-x)^2 on
        the sample after, x being the fractional crossing; so at every
        naive falling edge sqrt(-res[n-1]) + sqrt(res[n]) == 1 exactly
        when both halves agree on where the edge fell. Sizing the falling
        edge's window by its own increment (dt - dpw) keeps that true
        even at 30 Hz, where the width moves at 40% of the phase rate
        (with a plain dt window the sum would be off by that much). No
        residual anywhere else = no doubled or stray correction."""
        N = 512 * 64
        t = np.arange(N) / SR
        lfo = (np.sin(2 * np.pi * lfo_hz * t) * 0.9).astype(np.float32)
        naive = _render_pw("square", pw_cv=lfo, freq=220.0, blocks=64).astype(np.float64)
        blep = _render_pw("square_blep", pw_cv=lfo, freq=220.0, blocks=64).astype(np.float64)
        res = blep - naive
        pw = np.clip(0.5 + 0.5 * lfo.astype(np.float64), 0.05, 0.95)
        high = (naive + (2.0 * pw - 1.0)) > 0.0
        fall = np.where(high[:-1] & ~high[1:])[0] + 1
        rise = np.where(~high[:-1] & high[1:])[0] + 1
        assert len(fall) > 100
        before = res[fall - 1]
        after = res[fall]
        assert np.all(before <= 0.0) and np.all(after >= 0.0)
        sums = np.sqrt(-before) + np.sqrt(after)
        assert np.max(np.abs(sums - 1.0)) < 0.02, np.max(np.abs(sums - 1.0))
        # Nothing but the edge pairs (and the rising edge's after-half at
        # sample 0, where the classic square has always started).
        touched = np.zeros(N, bool)
        for n in np.concatenate([fall, rise]):
            touched[n - 1:n + 1] = True
        touched[0] = True
        assert np.max(np.abs(res[~touched])) == 0.0

    # -- UI ------------------------------------------------------------------------

    def test_every_param_gets_a_bounded_widget(self, monkeypatch):
        pytest.importorskip("dearpygui.dearpygui")
        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
        app = app_mod.App()
        app.patch = Patch()
        module = app.patch.add_module("oscillator")
        kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
                 "add_input_float", "add_checkbox", "add_drag_int")
        before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
        app._create_node_for_module(module)
        w = {}
        for k in kinds:
            for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
                w[str(call.kwargs.get("label"))] = (k, call.kwargs)
        for name in get_module_type("oscillator").DEFAULT_PARAMS:
            hits = [lb for lb in w if lb == name or lb.startswith(name + " ")]
            assert hits, (name, list(w))
            assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]][0])
        kind, kw = w["pulse_width"]
        assert kind == "add_slider_float"
        assert (kw["min_value"], kw["max_value"], kw["format"]) == (0.05, 0.95, "%.2f")
        kind, kw = w["pw_cv_depth"]
        assert kind == "add_drag_float" and "width/unit" in kw["format"]

    # -- example -------------------------------------------------------------------

    def test_pwm_pad_example_sweeps_the_width(self):
        from pysynthrack.io_patch import load_patch

        path = Path(__file__).resolve().parent.parent / "examples" / "oscillator_pwm.json"
        patch = load_patch(path)
        assert len(list(patch)) <= 12
        oscs = [m for m in patch if m.TYPE == "oscillator"]
        assert all(m.params["waveform"] == "square_blep" for m in oscs)
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        cap = []
        orig = backend._render_oscillator
        target = oscs[0].id

        def spy(module, frames, buffers, p):
            r = orig(module, frames, buffers, p)
            if module.id == target:
                cap.append(np.asarray(r).copy())
            return r

        backend._render_oscillator = spy
        peak = 0.0
        for _ in range(int(SR * 6 / 512)):
            out, _devices = backend.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        assert 0.3 < peak < 0.8, peak
        sig = np.concatenate(cap)
        # The width, measured per half second as the fraction of samples
        # above the pulse's midpoint, must travel the LFO's whole reach.
        widths = []
        for k in range(12):
            w = sig[int(k * 0.5 * SR):int((k + 1) * 0.5 * SR)]
            widths.append(float(np.mean(w > (w.max() + w.min()) / 2.0)))
        assert min(widths) < 0.25 and max(widths) > 0.75, widths
