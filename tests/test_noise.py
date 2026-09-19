"""Tests for the Noise generator (white / pink / brown / violet, audio + cv source).

Coverage:
  - Model: registration, defaults (``seed`` 0), ports/signal kinds (one
    ``amp_cv`` cv input; dual `out` audio + `cv` cv), JSON round-trip,
    unknown param rejected, ``amp_cv`` is knobless (no depth param),
    type walls (audio out→audio sink legal, cv out→cv input legal,
    audio out→cv input illegal, cv out→audio sink illegal, audio →
    ``amp_cv`` illegal).
  - White: mono shape/dtype, hard-bounded to ±amp, ~zero mean, roughly
    flat spectrum, amp scaling, both jacks are the same stream; the
    default path is BIT-EXACT with the shipped arithmetic (the global
    rng's uniform, nothing else).
  - Pink: mono shape, steep low-vs-high spectral tilt (≈ −3 dB/oct),
    filter state (zi) carried across blocks, switching back to white
    drops the stale pink state, RMS ≈ white; the pink arithmetic is
    BIT-EXACT with the shipped filter.
  - Brown (love pass): −6 dB/oct ±1 dB between two octave-apart bands
    above the corner (a two-band A/B), RMS ≈ white ±1 dB, filter state
    carried and evolving, a colour switch drops the other colours'
    state, no DC wander (block means stay small).
  - Violet (love pass): +6 dB/oct ±1 dB, RMS ≈ white ±1 dB, peaks
    hard-bounded at sqrt(2)·amp, state carried.
  - Seed (love pass): seed N is reproducible across two backends and
    BIT-EXACT at block 64 vs 512 for every colour; seed 0 is the global
    rng (bit-exact under np.random.seed, and leaves no private die in
    state); a seeded stream ignores the global rng; changing the seed
    re-creates the generator; the same seed on two modules is the same
    stream (documented feature); a string seed is coerced.
  - amp_cv (love pass): unpatched == today bit-exact; a constant 1.0 is
    bit-identical to unpatched; a ramp gives a rising block RMS and is
    exactly noise·amp·cv; a (V, F) cv gives a (V, F) out whose rows are
    the one mono stream scaled per voice (and the same array on `cv`);
    a mono cv keeps the output mono; a gated integration render.
  - Randomness: consecutive blocks differ; two free-running modules draw
    independent streams.
  - UI: every param gets a bounded widget (mocked dpg); the colour combo
    carries exactly NOISE_COLORS; seed is a drag_int; pluck's ``color``
    (a 0..1 slider) is NOT shadowed by the noise combo.
  - Example: ``noise_brown_surf.json`` loads, is small, renders in the
    0.3..0.8 peak window, swells under its LFO, and -- being seeded -- is
    bit-identical on a second render.
  - Integration: white→filter→speaker renders audible audio; noise.cv→
    SampleHold (clocked) yields a bounded random staircase; noise.cv→
    CVToAudio→speaker (the audio-via-bridge path) renders.
"""
from __future__ import annotations

import math
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from scipy.signal import lfilter, welch

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type
from pysynthrack.modules.noise import NOISE_COLORS, Noise

SR = 44100


def _backend():
    return NumpyBackend(sample_rate=SR, block_size=512)


def _render(color="white", amp=1.0, frames=512, seed=0, noise_seed=0):
    """One ``_render_noise`` call on a fresh backend.

    ``seed`` seeds numpy's GLOBAL rng (the free-running die);
    ``noise_seed`` is the module's own ``seed`` param.
    """
    np.random.seed(seed)
    patch = Patch()
    nz = patch.add_module(
        "noise", params={"color": color, "amp": amp, "seed": noise_seed}
    )
    backend = _backend()
    backend.compile(patch)
    return backend._render_noise(nz, frames, {}, patch)


def _stream(color, frames, block, noise_seed, amp=1.0):
    """A seeded stream rendered ``block`` frames at a time, concatenated."""
    patch = Patch()
    nz = patch.add_module(
        "noise", params={"color": color, "amp": amp, "seed": noise_seed}
    )
    backend = NumpyBackend(sample_rate=SR, block_size=block)
    backend.compile(patch)
    return np.concatenate(
        [backend._render_noise(nz, block, {}, patch)["out"] for _ in range(frames // block)]
    )


def _bandpow(sig, sr, lo, hi):
    f, P = welch(sig, fs=sr, nperseg=4096)
    m = (f >= lo) & (f < hi)
    return float(P[m].mean())


def _band_db(sig, lo, hi, nperseg=8192):
    f, P = welch(sig, fs=SR, nperseg=nperseg)
    m = (f >= lo) & (f < hi)
    return 10.0 * math.log10(float(P[m].mean()))


def _octave_slope(sig):
    """dB/oct between the 400-800 and 800-1600 Hz bands: well above brown's
    10 Hz corner and well below Nyquist, where a one-pole / one-zero is
    within 0.1 dB of its asymptotic ±6 dB/oct."""
    return _band_db(sig, 800, 1600) - _band_db(sig, 400, 800)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        nz = patch.add_module("noise")
        assert isinstance(nz, Noise)
        assert nz.params == {"color": "white", "amp": 1.0, "seed": 0}

    def test_colors_constant(self):
        assert NOISE_COLORS == ("white", "pink", "brown", "violet")

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        nz = patch.add_module("noise")
        assert [(p.name, p.signal_kind) for p in nz.input_ports] == [("amp_cv", "cv")]
        assert [(p.name, p.signal_kind) for p in nz.output_ports] == [
            ("out", "audio"),
            ("cv", "cv"),
        ]

    def test_amp_cv_is_knobless(self):
        # The house exception: the CV IS the amplitude, no depth param.
        nz = Patch().add_module("noise")
        assert not any(k.endswith("depth") for k in nz.params)

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("noise", params={"color": "brown", "amp": 0.3, "seed": 42})
        restored = Patch.from_dict(patch.to_dict())
        nz = next(m for m in restored if m.TYPE == "noise")
        assert nz.params["color"] == "brown"
        assert nz.params["amp"] == 0.3
        assert nz.params["seed"] == 42

    def test_pre_love_pass_patch_loads_with_seed_default(self):
        # A patch saved before the love pass has no seed key.
        d = Patch()
        d.add_module("noise", params={"color": "pink"})
        raw = d.to_dict()
        for m in raw["modules"]:
            m["params"].pop("seed", None)
        restored = Patch.from_dict(raw)
        nz = next(m for m in restored if m.TYPE == "noise")
        assert nz.params["seed"] == 0
        assert nz.params["color"] == "pink"

    def test_unknown_param_rejected(self):
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("noise", params={"gain": 42})

    def test_audio_out_into_audio_sink_accepted(self):
        patch = Patch()
        nz = patch.add_module("noise")
        spk = patch.add_module("speaker_output")
        patch.connect(nz.id, "out", spk.id, "in")  # audio → audio

    def test_cv_out_into_cv_input_accepted(self):
        patch = Patch()
        nz = patch.add_module("noise")
        sh = patch.add_module("sample_hold")
        patch.connect(nz.id, "cv", sh.id, "in")  # cv → cv

    def test_audio_out_into_cv_input_rejected(self):
        patch = Patch()
        nz = patch.add_module("noise")
        sh = patch.add_module("sample_hold")
        with pytest.raises(ValueError):
            patch.connect(nz.id, "out", sh.id, "in")  # audio → cv

    def test_cv_out_into_audio_sink_rejected(self):
        patch = Patch()
        nz = patch.add_module("noise")
        spk = patch.add_module("speaker_output")
        with pytest.raises(ValueError):
            patch.connect(nz.id, "cv", spk.id, "in")  # cv → audio

    def test_amp_cv_accepts_cv_rejects_audio(self):
        patch = Patch()
        nz = patch.add_module("noise")
        lfo = patch.add_module("lfo")
        osc = patch.add_module("oscillator")
        patch.connect(lfo.id, "cv", nz.id, "amp_cv")  # cv → cv
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", nz.id, "amp_cv")  # audio → cv


# ----- White -----------------------------------------------------------------


class TestWhite:
    def test_shape_and_dtype(self):
        r = _render("white", frames=256)
        assert r["out"].shape == (256,)
        assert r["out"].dtype == np.float32

    def test_both_jacks_same_stream(self):
        r = _render("white")
        assert r["out"] is r["cv"]
        assert np.array_equal(r["out"], r["cv"])

    def test_hard_bounded_to_amp(self):
        r = _render("white", amp=1.0, frames=8192)
        assert r["out"].min() >= -1.0 and r["out"].max() <= 1.0
        r2 = _render("white", amp=0.25, frames=8192)
        assert np.abs(r2["out"]).max() <= 0.25

    def test_zero_mean(self):
        r = _render("white", frames=44100)
        assert abs(float(r["out"].mean())) < 0.02

    def test_roughly_flat_spectrum(self):
        sig = _render("white", frames=44100, seed=1)["out"]
        ratio = _bandpow(sig, 44100, 50, 500) / _bandpow(sig, 44100, 5000, 15000)
        assert 0.5 < ratio < 2.0  # flat-ish: low ≈ high

    def test_amp_scales_rms(self):
        full = _render("white", amp=1.0, frames=20000, seed=2)["out"].std()
        half = _render("white", amp=0.5, frames=20000, seed=2)["out"].std()
        assert abs(half / full - 0.5) < 0.02

    def test_default_path_is_the_shipped_arithmetic_bit_exact(self):
        """seed 0, amp_cv unpatched: the global rng's uniform, and nothing
        else, on both jacks -- the pre-love-pass render to the bit."""
        for amp in (1.0, 0.37):
            np.random.seed(21)
            expect = np.random.uniform(-1.0, 1.0, 2048).astype(np.float32)
            if amp != 1.0:
                expect = (expect * amp).astype(np.float32)
            got = _render("white", amp=amp, frames=2048, seed=21)["out"]
            assert np.array_equal(got, expect)


# ----- Pink ------------------------------------------------------------------


class TestPink:
    def test_shape_mono(self):
        r = _render("pink", frames=512)
        assert r["out"].shape == (512,)
        assert r["out"] is r["cv"]

    def test_low_frequency_tilt(self):
        sig = _render("pink", frames=44100, seed=3)["out"]
        ratio = _bandpow(sig, 44100, 50, 500) / _bandpow(sig, 44100, 5000, 15000)
        assert ratio > 5.0  # markedly more low-frequency energy than white

    def test_slope_near_minus_3_db_per_octave(self):
        sig = _render("pink", frames=44100 * 4, seed=4)["out"]
        f, P = welch(sig, fs=44100, nperseg=8192)

        def at(c):
            m = (f >= c / 1.2) & (f < c * 1.2)
            return 10 * math.log10(P[m].mean())

        slope = (at(10000) - at(100)) / math.log2(10000 / 100)
        assert -4.0 < slope < -2.0  # ≈ -3 dB/oct

    def test_zi_carried_across_blocks(self):
        np.random.seed(5)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "pink"})
        backend = _backend()
        backend.compile(patch)
        backend._render_noise(nz, 512, {}, patch)
        zi1 = backend._state[nz.id]["pink_zi"].copy()
        backend._render_noise(nz, 512, {}, patch)
        zi2 = backend._state[nz.id]["pink_zi"]
        assert not np.allclose(zi1, 0.0)      # state accrued
        assert not np.allclose(zi1, zi2)      # and evolves block to block

    def test_switch_back_to_white_drops_pink_state(self):
        np.random.seed(6)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "pink"})
        backend = _backend()
        backend.compile(patch)
        backend._render_noise(nz, 512, {}, patch)
        assert "pink_zi" in backend._state[nz.id]
        nz.set_param("color", "white")
        backend._render_noise(nz, 512, {}, patch)
        assert "pink_zi" not in backend._state[nz.id]

    def test_rms_matches_white(self):
        w = _render("white", frames=44100, seed=7)["out"].std()
        p = _render("pink", frames=44100, seed=7)["out"].std()
        assert 0.7 < p / w < 1.4  # RMS-normalised to roughly white's level

    def test_pink_arithmetic_unchanged_bit_exact(self):
        """The shipped pink: lfilter(_PINK_B, _PINK_A, white, zi=0) * _PINK_SCALE,
        state carried -- reproduced inline over two blocks."""
        np.random.seed(22)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "pink"})
        backend = _backend()
        backend.compile(patch)
        got = np.concatenate(
            [backend._render_noise(nz, 512, {}, patch)["out"] for _ in range(2)]
        )
        np.random.seed(22)
        zi = np.zeros(3, dtype=np.float64)
        parts = []
        for _ in range(2):
            white = np.random.uniform(-1.0, 1.0, 512).astype(np.float32)
            y, zi = lfilter(NumpyBackend._PINK_B, NumpyBackend._PINK_A, white, zi=zi)
            parts.append((y * NumpyBackend._PINK_SCALE).astype(np.float32))
        assert np.array_equal(got, np.concatenate(parts))


# ----- Brown -----------------------------------------------------------------


class TestBrown:
    def test_shape_mono_same_array_both_jacks(self):
        r = _render("brown", frames=512)
        assert r["out"].shape == (512,)
        assert r["out"].dtype == np.float32
        assert r["out"] is r["cv"]

    def test_slope_minus_6_db_per_octave(self):
        # Two-band A/B on a seeded 4 s stream: 400-800 vs 800-1600 Hz.
        sig = _stream("brown", SR * 4, 512, noise_seed=11)
        white = _stream("white", SR * 4, 512, noise_seed=11)
        assert abs(_octave_slope(white)) < 0.5            # the control is flat
        assert -7.0 < _octave_slope(sig) < -5.0           # -6 dB/oct +-1

    def test_much_more_low_than_high_energy(self):
        sig = _stream("brown", SR * 2, 512, noise_seed=12)
        pink = _stream("pink", SR * 2, 512, noise_seed=12)
        ratio_b = _bandpow(sig, SR, 50, 500) / _bandpow(sig, SR, 5000, 15000)
        ratio_p = _bandpow(pink, SR, 50, 500) / _bandpow(pink, SR, 5000, 15000)
        assert ratio_b > ratio_p > 5.0                    # steeper than pink

    def test_rms_matches_white_within_1_db(self):
        # 10 s: brown's RMS is dominated by its lowest octaves, so a short
        # window is a small sample of it (see the backend note).
        w = _stream("white", SR * 10, 4096, noise_seed=13).std()
        b = _stream("brown", SR * 10, 4096, noise_seed=13).std()
        assert abs(20.0 * math.log10(b / w)) < 1.0

    def test_no_dc_wander(self):
        # The 10 Hz leak: a true integrator would drift off; block means
        # over 1 s windows stay a small fraction of the RMS.
        sig = _stream("brown", SR * 8, 512, noise_seed=14)
        means = [abs(float(sig[i:i + SR].mean())) for i in range(0, len(sig), SR)]
        assert max(means) < 0.5 * float(sig.std())

    def test_zi_carried_across_blocks(self):
        np.random.seed(15)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "brown"})
        backend = _backend()
        backend.compile(patch)
        backend._render_noise(nz, 512, {}, patch)
        zi1 = backend._state[nz.id]["brown_zi"].copy()
        backend._render_noise(nz, 512, {}, patch)
        zi2 = backend._state[nz.id]["brown_zi"]
        assert zi1.shape == (1,)
        assert not np.allclose(zi1, 0.0)
        assert not np.allclose(zi1, zi2)

    def test_colour_switch_drops_other_state(self):
        np.random.seed(16)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "brown"})
        backend = _backend()
        backend.compile(patch)
        backend._render_noise(nz, 512, {}, patch)
        assert "brown_zi" in backend._state[nz.id]
        nz.set_param("color", "pink")
        backend._render_noise(nz, 512, {}, patch)
        st = backend._state[nz.id]
        assert "brown_zi" not in st and "pink_zi" in st
        nz.set_param("color", "violet")
        backend._render_noise(nz, 512, {}, patch)
        assert "pink_zi" not in st and "violet_zi" in st
        nz.set_param("color", "white")
        backend._render_noise(nz, 512, {}, patch)
        assert not any(k.endswith("_zi") for k in st)

    def test_amp_scales(self):
        full = _stream("brown", SR, 512, noise_seed=17, amp=1.0)
        half = _stream("brown", SR, 512, noise_seed=17, amp=0.5)
        assert np.allclose(half, full * 0.5, atol=1e-6)


# ----- Violet ----------------------------------------------------------------


class TestViolet:
    def test_shape_mono_same_array_both_jacks(self):
        r = _render("violet", frames=512)
        assert r["out"].shape == (512,)
        assert r["out"] is r["cv"]

    def test_slope_plus_6_db_per_octave(self):
        sig = _stream("violet", SR * 4, 512, noise_seed=31)
        assert 5.0 < _octave_slope(sig) < 7.0             # +6 dB/oct +-1

    def test_rms_matches_white_within_1_db(self):
        w = _stream("white", SR * 4, 512, noise_seed=32).std()
        v = _stream("violet", SR * 4, 512, noise_seed=32).std()
        assert abs(20.0 * math.log10(v / w)) < 1.0

    def test_peaks_bounded_at_root_two_amp(self):
        # |x[n] - x[n-1]| <= 2 for uniform +-1, times 1/sqrt(2).
        sig = _stream("violet", SR * 2, 512, noise_seed=33, amp=0.5)
        assert float(np.abs(sig).max()) <= 0.5 * math.sqrt(2.0) + 1e-6

    def test_zi_carried_across_blocks(self):
        # The first difference needs the previous block's last sample:
        # rendered in one go and in two halves the stream must agree at
        # the seam (a zero zi at the seam would put a spike there).
        whole = _stream("violet", 1024, 1024, noise_seed=34)
        halves = _stream("violet", 1024, 512, noise_seed=34)
        assert np.array_equal(whole, halves)


# ----- Seed ------------------------------------------------------------------


class TestSeed:
    @pytest.mark.parametrize("color", NOISE_COLORS)
    def test_seeded_stream_reproducible_across_backends(self, color):
        a = _stream(color, 4096, 512, noise_seed=101)
        b = _stream(color, 4096, 512, noise_seed=101)
        c = _stream(color, 4096, 512, noise_seed=102)
        assert np.array_equal(a, b)
        assert not np.array_equal(a, c)

    @pytest.mark.parametrize("color", NOISE_COLORS)
    def test_seeded_stream_bit_exact_64_vs_512(self, color):
        a = _stream(color, 4096, 64, noise_seed=103)
        b = _stream(color, 4096, 512, noise_seed=103)
        assert np.array_equal(a, b)

    def test_seed_zero_is_the_global_rng(self):
        np.random.seed(104)
        expect = np.random.uniform(-1.0, 1.0, 1024).astype(np.float32)
        got = _render("white", frames=1024, seed=104, noise_seed=0)["out"]
        assert np.array_equal(got, expect)

    def test_seed_zero_holds_no_private_die(self):
        patch = Patch()
        nz = patch.add_module("noise")
        backend = _backend()
        backend.compile(patch)
        backend._render_noise(nz, 512, {}, patch)
        assert "rng" not in backend._state.get(nz.id, {})

    def test_seeded_stream_ignores_the_global_rng(self):
        a = _render("white", frames=1024, seed=1, noise_seed=105)["out"]
        b = _render("white", frames=1024, seed=2, noise_seed=105)["out"]
        assert np.array_equal(a, b)

    def test_changing_the_seed_recreates_the_generator(self):
        patch = Patch()
        nz = patch.add_module("noise", params={"seed": 106})
        backend = _backend()
        backend.compile(patch)
        first = backend._render_noise(nz, 512, {}, patch)["out"].copy()
        nz.set_param("seed", 107)
        other = backend._render_noise(nz, 512, {}, patch)["out"].copy()
        assert not np.array_equal(first, other)
        nz.set_param("seed", 106)
        again = backend._render_noise(nz, 512, {}, patch)["out"]
        assert np.array_equal(again, first)          # restarted from the top

    def test_seed_back_to_zero_then_seeded_restarts(self):
        patch = Patch()
        nz = patch.add_module("noise", params={"seed": 108})
        backend = _backend()
        backend.compile(patch)
        first = backend._render_noise(nz, 512, {}, patch)["out"].copy()
        nz.set_param("seed", 0)
        backend._render_noise(nz, 512, {}, patch)
        assert "rng" not in backend._state[nz.id]
        nz.set_param("seed", 108)
        assert np.array_equal(backend._render_noise(nz, 512, {}, patch)["out"], first)

    def test_same_seed_on_two_modules_is_the_same_stream(self):
        # Documented feature: the seed alone is the key (correlated stereo).
        patch = Patch()
        n1 = patch.add_module("noise", params={"color": "brown", "seed": 109})
        n2 = patch.add_module("noise", params={"color": "brown", "seed": 109})
        n3 = patch.add_module("noise", params={"color": "brown", "seed": 110})
        backend = _backend()
        backend.compile(patch)
        a = backend._render_noise(n1, 512, {}, patch)["out"]
        b = backend._render_noise(n2, 512, {}, patch)["out"]
        c = backend._render_noise(n3, 512, {}, patch)["out"]
        assert np.array_equal(a, b)
        assert not np.array_equal(a, c)

    def test_string_seed_is_coerced(self):
        patch = Patch()
        nz = patch.add_module("noise", params={"seed": "111"})
        backend = _backend()
        backend.compile(patch)
        got = backend._render_noise(nz, 512, {}, patch)["out"]
        assert np.array_equal(got, _stream("white", 512, 512, noise_seed=111))


# ----- amp_cv ----------------------------------------------------------------


def _amp_cv_render(cv_buf, color="white", amp=1.0, noise_seed=201, frames=512):
    """Render one block with ``cv_buf`` on ``amp_cv`` (an lfo stands in as
    the source; its buffer is injected directly)."""
    patch = Patch()
    nz = patch.add_module("noise", params={"color": color, "amp": amp, "seed": noise_seed})
    lfo = patch.add_module("lfo")
    patch.connect(lfo.id, "cv", nz.id, "amp_cv")
    backend = _backend()
    backend.compile(patch)
    buffers = {}
    if cv_buf is not None:
        buffers[(lfo.id, "cv")] = cv_buf
    return backend._render_noise(nz, frames, buffers, patch)


class TestAmpCv:
    def test_unpatched_is_unity_bit_exact(self):
        # Port declared but nothing on it: identical to the bare module.
        got = _amp_cv_render(None, color="pink", amp=0.6)["out"]
        assert np.array_equal(got, _stream("pink", 512, 512, noise_seed=201, amp=0.6))

    def test_constant_one_is_bit_identical_to_unpatched(self):
        ones = np.ones(512, dtype=np.float32)
        got = _amp_cv_render(ones, color="brown", amp=0.6)["out"]
        assert np.array_equal(got, _stream("brown", 512, 512, noise_seed=201, amp=0.6))

    def test_ramp_gives_rising_block_rms_and_exact_product(self):
        ramp = np.linspace(0.0, 1.0, 512, dtype=np.float32)
        got = _amp_cv_render(ramp, color="white", amp=0.8)["out"]
        base = _stream("white", 512, 512, noise_seed=201, amp=0.8)
        assert np.array_equal(got, (base * ramp).astype(np.float32))   # after amp
        assert got.shape == (512,)
        q = [float(got[i * 128:(i + 1) * 128].std()) for i in range(4)]
        assert q[0] < q[1] < q[2] < q[3]                                  # the envelope

    def test_voice_cv_broadcasts_one_stream_per_voice(self):
        levels = np.array([1.0, 0.5, 0.25, 0.0], dtype=np.float32)
        cv = np.repeat(levels[:, None], 512, axis=1)                      # (4, 512)
        r = _amp_cv_render(cv, color="violet", amp=0.7)
        mono = _stream("violet", 512, 512, noise_seed=201, amp=0.7)
        assert r["out"].shape == (4, 512)
        assert r["out"] is r["cv"]                                        # same array on both jacks
        for v, lv in enumerate(levels):
            assert np.array_equal(r["out"][v], (mono * lv).astype(np.float32))
        assert np.all(r["out"][3] == 0.0)                                 # a silent voice

    def test_voice_cv_rows_are_the_same_noise(self):
        # ONE stream under per-voice levels, not V independent streams.
        cv = np.ones((3, 512), dtype=np.float32) * np.array([[1.0], [0.3], [1.0]], np.float32)
        out = _amp_cv_render(cv)["out"]
        assert np.array_equal(out[0], out[2])
        assert np.corrcoef(out[0], out[1])[0, 1] > 0.9999

    def test_mono_cv_keeps_output_mono(self):
        cv = np.full(512, 0.5, dtype=np.float32)
        out = _amp_cv_render(cv)["out"]
        assert out.shape == (512,) and out.dtype == np.float32

    def test_gated_by_an_lfo_in_a_real_graph(self):
        # noise → speaker, a slow unipolar square on amp_cv: the loud
        # half-cycles carry the noise, the quiet ones are silent.
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "white", "amp": 0.5, "seed": 202})
        lfo = patch.add_module("lfo", params={"waveform": "square", "rate": 2.0, "depth": 1.0, "bipolar": False})
        spk = patch.add_module("speaker_output", params={"gain": 0.8})
        patch.connect(lfo.id, "cv", nz.id, "amp_cv")
        patch.connect(nz.id, "out", spk.id, "in")
        backend = _backend()
        backend.compile(patch)
        rms = []
        for _ in range(SR // 512):
            out, _devices = backend.render_block_multi(512)
            assert np.isfinite(out).all()
            rms.append(float(out[:, 0].std()))
        rms = np.array(rms)
        assert rms.max() > 0.2
        assert (rms < 1e-6).sum() >= 0.3 * len(rms)     # the quiet halves are silent


# ----- Randomness ------------------------------------------------------------


class TestRandomness:
    def test_consecutive_blocks_differ(self):
        np.random.seed(8)
        patch = Patch()
        nz = patch.add_module("noise")
        backend = _backend()
        backend.compile(patch)
        a = backend._render_noise(nz, 512, {}, patch)["out"].copy()
        b = backend._render_noise(nz, 512, {}, patch)["out"].copy()
        assert not np.array_equal(a, b)

    def test_two_modules_independent(self):
        np.random.seed(9)
        patch = Patch()
        n1 = patch.add_module("noise")
        n2 = patch.add_module("noise")
        backend = _backend()
        backend.compile(patch)
        a = backend._render_noise(n1, 512, {}, patch)["out"]
        b = backend._render_noise(n2, 512, {}, patch)["out"]
        assert not np.array_equal(a, b)


# ----- UI ----------------------------------------------------------------------


def _widgets(monkeypatch, type_name, params=None):
    """Build one node of ``type_name`` against a mocked dpg; return
    label -> (widget kind, format, items)."""
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module(type_name, params=params)
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int", "add_input_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (
                k, call.kwargs.get("format"), call.kwargs.get("items")
            )
    return out


class TestUI:
    def test_every_param_gets_a_bounded_widget(self, monkeypatch):
        w = _widgets(monkeypatch, "noise")
        labels = list(w)
        for name in get_module_type("noise").DEFAULT_PARAMS:
            hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
            assert hits, (name, labels)
            assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])

    def test_colour_combo_carries_exactly_the_four_colours(self, monkeypatch):
        w = _widgets(monkeypatch, "noise", params={"color": "brown"})
        assert w["color"][0] == "add_combo"
        assert tuple(w["color"][2]) == NOISE_COLORS

    def test_seed_is_a_drag_int(self, monkeypatch):
        w = _widgets(monkeypatch, "noise", params={"seed": 7})
        assert w["seed"][0] == "add_drag_int"

    def test_pluck_colour_is_not_shadowed_by_the_noise_combo(self, monkeypatch):
        # pluck's ``color`` is a 0..1 exciter brightness slider; the
        # colour combo must stay the noise's own (matching a label is not
        # matching the widget).
        w = _widgets(monkeypatch, "pluck")
        assert w["color"][0] == "add_slider_float"


# ----- Example ---------------------------------------------------------------


def _render_example(seconds=10.0):
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "noise_brown_surf.json"
    patch = load_patch(path)
    b = _backend()
    b.compile(patch)
    outs = []
    for _ in range(int(SR * seconds / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        outs.append(np.asarray(out).copy())
    return patch, np.concatenate(outs)


class TestExample:
    def test_surf_example_is_small_seeded_brown_and_in_the_peak_window(self):
        patch, sig = _render_example()
        modules = list(patch)
        assert len(modules) <= 8
        nz = next(m for m in modules if m.TYPE == "noise")
        assert nz.params["color"] == "brown"
        assert int(nz.params["seed"]) != 0
        assert any(c.dst_module_id == nz.id and c.dst_port == "amp_cv" for c in patch.cables)
        peak = float(np.abs(sig).max())
        assert 0.3 < peak < 0.8, peak

    def test_surf_example_swells_under_its_lfo(self):
        _patch, sig = _render_example(10.0)
        half = SR // 2
        env = np.array([float(sig[i:i + half, 0].std()) for i in range(0, len(sig) - half, half)])
        assert env.max() / env.min() > 2.5      # waves roll in and draw back
        assert env.min() > 0.0                  # ...but never to silence (the offset)

    def test_surf_example_is_reproducible_run_to_run(self):
        # The only random element is the seeded noise: two renders agree
        # to the bit without any global seeding.
        _p1, a = _render_example(3.0)
        _p2, b = _render_example(3.0)
        assert np.array_equal(a, b)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_white_through_filter_to_speaker(self):
        np.random.seed(10)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "white", "amp": 0.8})
        filt = patch.add_module("filter", params={"mode": "lowpass", "cutoff": 1200.0, "resonance": 3.0})
        spk = patch.add_module("speaker_output", params={"gain": 0.8})
        patch.connect(nz.id, "out", filt.id, "in")
        patch.connect(filt.id, "out", spk.id, "in")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        peak = 0.0
        for _ in range(10):
            blk = backend.render_block(512)
            assert blk.shape == (512, 2)
            assert np.isfinite(blk).all()
            peak = max(peak, float(np.abs(blk).max()))
        assert peak > 0.0

    def test_noise_cv_into_samplehold_is_bounded_staircase(self):
        """noise.cv → S&H clocked by an LFO→Schmitt: held random steps."""
        np.random.seed(11)
        sr = 44100
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "white"})
        clk = patch.add_module("lfo", params={"waveform": "square", "rate": 8.0, "depth": 1.0, "bipolar": False})
        sch = patch.add_module("schmitt")
        sh = patch.add_module("sample_hold")
        patch.connect(nz.id, "cv", sh.id, "in")
        patch.connect(clk.id, "cv", sch.id, "in")
        patch.connect(sch.id, "gate", sh.id, "trig")
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        held = []
        for _ in range(sr // 512):  # ~1 s
            buffers = {}
            for mid in backend._topo_order:
                m = patch.modules.get(mid)
                if m is None:
                    continue
                res = backend._render_module(m, 512, buffers, patch)
                if res is None:
                    continue
                if isinstance(res, dict):
                    for port, buf in res.items():
                        buffers[(mid, port)] = buf
                elif m.OUTPUT_PORTS:
                    buffers[(mid, m.OUTPUT_PORTS[0].name)] = res
            held.append(buffers[(sh.id, "out")])
        out = np.concatenate(held)
        assert np.isfinite(out).all()
        assert out.min() >= -1.0 and out.max() <= 1.0     # white-bounded samples
        assert np.mean(np.diff(out) == 0) > 0.95          # piecewise-constant
        assert len(np.unique(out)) > 3                    # actually stepping

    def test_noise_cv_through_cvtoaudio_bridge(self):
        np.random.seed(12)
        patch = Patch()
        nz = patch.add_module("noise", params={"color": "pink", "amp": 0.5})
        bridge = patch.add_module("cv_to_audio", params={"gain": 1.0})
        spk = patch.add_module("speaker_output", params={"gain": 0.8})
        patch.connect(nz.id, "cv", bridge.id, "cv")
        patch.connect(bridge.id, "out", spk.id, "in")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        blk = backend.render_block(512)
        assert blk.shape == (512, 2)
        assert np.isfinite(blk).all()
        assert np.abs(blk).max() > 0.0
