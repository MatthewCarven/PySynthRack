"""Anti-aliased oscillator shapes — PolyBLEP / PolyBLAMP + wavetable.

Covers the v0.4 anti-aliasing work: every audio-rate shape ships in
three flavours selected by the ``waveform`` suffix (naive, ``_blep``,
``_wt``), shared via :meth:`NumpyBackend._osc_waveshape` across the
Oscillator, CVToFrequency, Keyboard and MIDIInput renderers.

The headline property is spectral: at a high fundamental the band-
limited shapes put far less energy into non-harmonic (aliased) bins
than the naive shape. We measure that directly with an FFT rather than
asserting on exact sample values, because the whole point of band-
limiting is that the time-domain waveform changes.
"""
import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch, Cable
from pysynthrack.modules.oscillator import WAVEFORMS

SR = 44100


def _alias_fraction(sig, freq, sr=SR):
    """Fraction of spectral energy NOT sitting on a harmonic of ``freq``."""
    sig = np.asarray(sig, dtype=np.float64)
    n = len(sig)
    w = np.hanning(n)
    spec = np.abs(np.fft.rfft(sig * w))
    fbins = np.fft.rfftfreq(n, 1.0 / sr)
    is_harmonic = np.zeros(len(fbins), dtype=bool)
    k = 1
    while freq * k < sr / 2:
        idx = int(np.argmin(np.abs(fbins - freq * k)))
        lo, hi = max(0, idx - 2), min(len(fbins), idx + 3)
        is_harmonic[lo:hi] = True
        k += 1
    total = float(np.sum(spec ** 2))
    alias = float(np.sum(spec[~is_harmonic] ** 2))
    return alias / total if total > 0 else 0.0


def _render_osc_mono(waveform, freq=2200.0, frames=1 << 14):
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": waveform, "freq": freq, "amp": 1.0}
    )
    backend = NumpyBackend(sample_rate=SR, block_size=512)
    backend.compile(patch)
    return backend._render_oscillator(osc, frames, {}, patch)


class TestWaveformVocabulary:
    def test_all_expected_shapes_present(self):
        for w in (
            "sine",
            "saw", "square", "triangle",
            "saw_blep", "square_blep", "triangle_blep",
            "saw_wt", "square_wt", "triangle_wt",
        ):
            assert w in WAVEFORMS

    def test_naive_saw_unchanged(self):
        out = _render_osc_mono("saw", freq=440.0, frames=512)
        phase_inc = 440.0 / SR
        phases = (np.arange(512) * phase_inc) % 1.0
        expected = (2.0 * phases - 1.0).astype(np.float32)
        np.testing.assert_allclose(out, expected, atol=1e-6)

    def test_naive_square_unchanged(self):
        out = _render_osc_mono("square", freq=440.0, frames=512)
        phases = (np.arange(512) * (440.0 / SR)) % 1.0
        expected = np.where(phases < 0.5, 1.0, -1.0).astype(np.float32)
        np.testing.assert_allclose(out, expected, atol=1e-6)


class TestAliasingReduction:
    @pytest.mark.parametrize("base", ["saw", "square"])
    def test_blep_reduces_aliasing(self, base):
        naive = _render_osc_mono(base)
        blep = _render_osc_mono(f"{base}_blep")
        a_naive = _alias_fraction(naive, 2200.0)
        a_blep = _alias_fraction(blep, 2200.0)
        assert a_blep < a_naive / 5.0, (
            f"{base}_blep alias={a_blep:.4f} vs naive={a_naive:.4f}"
        )

    @pytest.mark.parametrize("base", ["saw", "square"])
    def test_wt_reduces_aliasing(self, base):
        naive = _render_osc_mono(base)
        wt = _render_osc_mono(f"{base}_wt")
        a_naive = _alias_fraction(naive, 2200.0)
        a_wt = _alias_fraction(wt, 2200.0)
        assert a_wt < a_naive / 5.0, (
            f"{base}_wt alias={a_wt:.4f} vs naive={a_naive:.4f}"
        )

    def test_triangle_blep_no_worse_than_naive(self):
        naive = _render_osc_mono("triangle")
        blep = _render_osc_mono("triangle_blep")
        assert _alias_fraction(blep, 2200.0) <= _alias_fraction(naive, 2200.0) + 1e-4
        assert 0.5 < np.max(np.abs(blep)) <= 1.05

    def test_anti_aliased_shapes_are_finite_and_bounded(self):
        for w in ("saw_blep", "square_blep", "triangle_blep",
                  "saw_wt", "square_wt", "triangle_wt"):
            out = _render_osc_mono(w)
            assert np.all(np.isfinite(out))
            assert np.max(np.abs(out)) <= 1.1, w


class TestWaveshapeHelper:
    def test_dt_none_degrades_to_naive(self):
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        phases = np.linspace(0, 1, 256, endpoint=False)
        for base in ("saw", "square", "triangle"):
            naive = backend._osc_waveshape(phases, base, dt=None)
            blep_nodt = backend._osc_waveshape(phases, f"{base}_blep", dt=None)
            wt_nodt = backend._osc_waveshape(phases, f"{base}_wt", dt=None)
            np.testing.assert_allclose(blep_nodt, naive, atol=1e-9)
            np.testing.assert_allclose(wt_nodt, naive, atol=1e-9)

    def test_wavetable_cache_built_once(self):
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        assert "saw" not in backend._wavetables
        t1 = backend._get_wavetable("saw")
        t2 = backend._get_wavetable("saw")
        assert t1 is t2
        assert t1.shape == (backend.NUM_WT_TABLES, backend.WT_LEN)

    def test_unknown_method_suffix_is_naive_shape(self):
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        phases = np.linspace(0, 1, 64, endpoint=False)
        out = backend._osc_waveshape(phases, "sine", dt=0.01)
        np.testing.assert_allclose(out, np.sin(2 * np.pi * phases), atol=1e-9)


class TestVoiceAwareAntiAliasing:
    @pytest.mark.parametrize("waveform", ["saw_blep", "square_wt", "triangle_blep"])
    def test_voice_freq_cv_preserves_shape(self, waveform):
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": waveform, "freq": 440.0, "amp": 1.0}
        )
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        FREQ_SRC = 9301
        patch.cables.append(Cable(
            src_module_id=FREQ_SRC, src_port="out",
            dst_module_id=osc.id, dst_port="freq_cv",
        ))
        freq_cv = np.zeros((16, 512), dtype=np.float32)
        freq_cv[5, :] = 1.0
        out = backend._render_oscillator(
            osc, 512, {(FREQ_SRC, "out"): freq_cv}, patch
        )
        assert out.shape == (16, 512)
        assert np.all(np.isfinite(out))


class TestCvToFrequencyAntiAliasing:
    @pytest.mark.parametrize("waveform", ["saw_blep", "square_wt"])
    def test_renders_finite_audio(self, waveform):
        patch = Patch()
        cvf = patch.add_module(
            "cv_to_frequency",
            params={"waveform": waveform, "freq": 2000.0},
        )
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        out = backend._render_cv_to_frequency(cvf, 1 << 13, {}, patch)
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) > 0.1


class TestKeyboardAntiAliasing:
    def test_blep_reduces_aliasing_vs_naive(self):
        from pysynthrack.modules.keyboard import midi_to_freq

        def render(waveform):
            patch = Patch()
            kb = patch.add_module(
                "keyboard", params={"waveform": waveform, "volume": 1.0}
            )
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            kb.note_on(101)
            for _ in range(8):
                backend._render_keyboard(kb, frames=512)
            blocks = [backend._render_keyboard(kb, frames=512)["out"]
                      for _ in range(16)]
            voice = np.concatenate([b.sum(axis=0) for b in blocks])
            return voice, midi_to_freq(101)

        naive, f = render("saw")
        blep, _ = render("saw_blep")
        assert _alias_fraction(blep, f) < _alias_fraction(naive, f) / 3.0

    def test_new_waveforms_render(self):
        for w in ("square_wt", "triangle_blep"):
            patch = Patch()
            kb = patch.add_module("keyboard", params={"waveform": w, "volume": 1.0})
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            kb.note_on(69)
            for _ in range(5):
                out = backend._render_keyboard(kb, frames=512)["out"]
            assert out.shape == (backend._MAX_VOICES, 512)
            assert np.max(np.abs(out)) > 0.05, w


class TestMidiInputAntiAliasing:
    def test_new_waveforms_render(self):
        for w in ("saw_blep", "saw_wt"):
            patch = Patch()
            mi = patch.add_module("midi_input", params={"waveform": w, "volume": 1.0})
            backend = NumpyBackend(sample_rate=SR, block_size=512)
            backend.compile(patch)
            mi.note_on(72, velocity=100)
            for _ in range(5):
                res = backend._render_midi_input(mi, frames=512)
            out = res["out"]
            assert out.shape == (backend._MAX_VOICES, 512)
            assert np.all(np.isfinite(out))
            assert np.max(np.abs(out)) > 0.05, w
