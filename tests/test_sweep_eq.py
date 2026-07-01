"""Tests for SweepEQ — the CV-swept resonant band (auto-wah)."""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.sweep_eq import SweepEQ, SWEEP_EQ_MODES


SR = 44100


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def _build(mode="bandpass", tone=800.0, freq=800.0, gain=12.0, q=4.0,
           cv_depth=1.0, mix=1.0, cv_value=None, amp=0.5):
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": "sine", "freq": tone, "amp": amp}
    )
    se = patch.add_module("sweep_eq", params={
        "mode": mode, "freq": freq, "gain": gain, "q": q,
        "cv_depth": cv_depth, "mix": mix,
    })
    patch.connect(osc.id, "out", se.id, "in")
    if cv_value is not None:
        c = patch.add_module("constant", params={"value": float(cv_value)})
        patch.connect(c.id, "out", se.id, "freq_cv")
    return patch, osc, se


def _cap(patch, *ids, N=4096, warm=True):
    """Compile, optionally warm one block, return the steady `out` of each id."""
    be = NumpyBackend(sample_rate=SR, block_size=N)
    be.compile(patch)

    def once():
        bufs = {}
        for mid in be._topo_order:
            m = patch.modules[mid]
            r = be._render_module(m, N, bufs, patch)
            if isinstance(r, dict):
                for k, v in r.items():
                    bufs[(mid, k)] = v
            elif r is not None and m.OUTPUT_PORTS:
                bufs[(mid, m.OUTPUT_PORTS[0].name)] = r
        return bufs

    if warm:
        once()
    bufs = once()
    return tuple(bufs[(i, "out")] for i in ids)


class TestSweepEQModel:
    def test_register_and_defaults(self):
        patch = Patch()
        se = patch.add_module("sweep_eq")
        assert isinstance(se, SweepEQ)
        assert se.params == {
            "mode": "bandpass", "freq": 800.0, "gain": 12.0,
            "q": 4.0, "cv_depth": 1.0, "mix": 1.0,
        }
        assert [p.name for p in se.input_ports] == ["in", "freq_cv"]
        assert se.input_ports[0].signal_kind == "audio"
        assert se.input_ports[1].signal_kind == "cv"
        assert [p.name for p in se.output_ports] == ["out"]
        assert se.output_ports[0].signal_kind == "audio"

    def test_modes_list(self):
        assert set(SWEEP_EQ_MODES) == {"bandpass", "lowpass", "peak"}

    def test_unpatched_input_is_silence(self):
        patch = Patch()
        se = patch.add_module("sweep_eq")
        be = NumpyBackend(sample_rate=SR, block_size=256)
        be.compile(patch)
        out = be._render_sweep_eq(se, 256, {}, patch)
        assert out.shape == (256,)
        assert np.all(out == 0.0)


def _one(**kw):
    patch, osc, se = _build(**kw)
    (out,) = _cap(patch, se.id)
    return out


class TestSweepEQVoicings:
    def test_bandpass_is_resonant_at_centre(self):
        """A tone at the centre passes (resonant boost, peak gain ~ Q);
        a tone far above the centre is strongly rejected."""
        centre = _rms(_one(mode="bandpass", tone=800.0, freq=800.0, q=4.0))
        far = _rms(_one(mode="bandpass", tone=4000.0, freq=800.0, q=4.0))
        assert centre > 1.0          # ~Q * 0.354 with Q=4
        assert far < 0.15
        assert centre > 8 * far

    def test_lowpass_passes_below_cuts_above(self):
        below = _rms(_one(mode="lowpass", tone=200.0, freq=800.0, q=2.0))
        above = _rms(_one(mode="lowpass", tone=4000.0, freq=800.0, q=2.0))
        assert below > 0.3
        assert above < 0.05

    def test_peak_boosts_band_but_passes_rest(self):
        """The peak voicing's signature: it boosts the band at the centre
        but leaves off-band signal at ~unity (unlike the filters, which
        reject off-band). Dry 0.5-amp sine ~ 0.354 RMS."""
        centre = _rms(_one(mode="peak", tone=800.0, freq=800.0, gain=12.0, q=4.0))
        off = _rms(_one(mode="peak", tone=4000.0, freq=800.0, gain=12.0, q=4.0))
        assert centre > 1.0                    # boosted ~4x
        assert abs(off - 0.354) < 0.02         # rest passes ~untouched

    def test_peak_cut_attenuates_centre(self):
        """Negative peak gain notches the centre band."""
        boosted = _rms(_one(mode="peak", tone=800.0, freq=800.0, gain=0.0, q=4.0))
        cut = _rms(_one(mode="peak", tone=800.0, freq=800.0, gain=-18.0, q=4.0))
        assert cut < boosted * 0.5


class TestSweepEQMix:
    def test_mix_zero_is_bit_exact_dry(self):
        """mix=0 → output is exactly the dry input, whatever the mode."""
        for mode in SWEEP_EQ_MODES:
            patch, osc, se = _build(mode=mode, tone=1234.0, mix=0.0)
            dry, wetmix = _cap(patch, osc.id, se.id, warm=False)
            assert np.array_equal(dry, wetmix), f"mix=0 not dry for {mode}"

    def test_peak_zero_gain_full_wet_is_passthrough(self):
        """A peak band at 0 dB is transparent, so mix=1 → the input."""
        patch, osc, se = _build(mode="peak", tone=1234.0, gain=0.0, mix=1.0)
        dry, out = _cap(patch, osc.id, se.id, warm=False)
        assert np.allclose(out, dry, atol=1e-6)

    def test_mix_half_is_average_of_wet_and_dry(self):
        """mix=0.5 reconstructs from the wet (mix=1) and dry (mix=0) renders."""
        wet = _one(mode="bandpass", tone=800.0, freq=800.0, mix=1.0)
        dry = _one(mode="bandpass", tone=800.0, freq=800.0, mix=0.0)
        half = _one(mode="bandpass", tone=800.0, freq=800.0, mix=0.5)
        assert np.allclose(half, 0.5 * wet + 0.5 * dry, atol=1e-6)

    def test_unknown_mode_passthrough(self):
        patch, osc, se = _build(mode="notamode", tone=440.0)
        dry, out = _cap(patch, osc.id, se.id, warm=False)
        assert np.array_equal(dry, out)


class TestSweepEQFreqCV:
    def test_positive_cv_moves_centre_up(self):
        """bandpass at 800 Hz: a 1600 Hz tone barely passes. With +1.0 CV
        (unit depth → centre 1600 Hz) that tone now sits in the band."""
        no_cv = _rms(_one(mode="bandpass", tone=1600.0, freq=800.0, q=4.0))
        swept = _rms(_one(mode="bandpass", tone=1600.0, freq=800.0, q=4.0,
                          cv_value=1.0, cv_depth=1.0))
        assert swept > 4 * no_cv

    def test_unit_cv_equals_static_doubled_centre(self):
        """freq=800, cv_depth=1, CV=+1 → centre 1600 Hz, identical to a
        static freq=1600 sweep_eq (same input)."""
        swept = _one(mode="bandpass", tone=1500.0, freq=800.0,
                     cv_value=1.0, cv_depth=1.0)
        static = _one(mode="bandpass", tone=1500.0, freq=1600.0)
        assert np.allclose(swept, static, atol=1e-6)

    def test_cv_depth_zero_disables_cv(self):
        off = _one(mode="bandpass", tone=1500.0, freq=800.0,
                   cv_value=1.0, cv_depth=0.0)
        static = _one(mode="bandpass", tone=1500.0, freq=800.0)
        assert np.allclose(off, static, atol=1e-6)

    def test_cv_depth_scales_exponent(self):
        """depth=2, CV=+0.5 → 2^(2·0.5)=2 → centre 1600 Hz."""
        deep = _one(mode="lowpass", tone=1000.0, freq=800.0,
                    cv_value=0.5, cv_depth=2.0)
        static = _one(mode="lowpass", tone=1000.0, freq=1600.0)
        assert np.allclose(deep, static, atol=1e-6)


class TestSweepEQVoice:
    @pytest.mark.parametrize("mode", ["bandpass", "lowpass", "peak"])
    def test_voice_row_matches_mono(self, mode):
        """(V, F) input under a shared freq_cv: the active voice row equals
        the mono render of the same signal; other rows stay silent."""
        from pysynthrack.core.patch import Cable
        N = 512
        c = 0.7
        t = np.arange(N) / SR
        tone = (np.sin(2 * np.pi * 900.0 * t) * 0.5).astype(np.float32)
        cv = np.full(N, c, dtype=np.float32)
        A, C = 9501, 9502

        def render(audio):
            patch = Patch()
            se = patch.add_module("sweep_eq", params={
                "mode": mode, "freq": 800.0, "gain": 10.0, "q": 4.0,
                "cv_depth": 1.0, "mix": 0.8,
            })
            patch.cables.append(Cable(
                src_module_id=A, src_port="out",
                dst_module_id=se.id, dst_port="in"))
            patch.cables.append(Cable(
                src_module_id=C, src_port="out",
                dst_module_id=se.id, dst_port="freq_cv"))
            be = NumpyBackend(sample_rate=SR, block_size=N)
            be.compile(patch)
            return be._render_sweep_eq(
                se, N, {(A, "out"): audio, (C, "out"): cv}, patch)

        mono = render(tone)
        va = np.zeros((16, N), dtype=np.float32)
        va[5] = tone
        voice = render(va)
        assert voice.shape == (16, N)
        assert np.allclose(voice[5], mono, atol=1e-6)
        for i in range(16):
            if i == 5:
                continue
            assert float(np.max(np.abs(voice[i]))) == 0.0


class TestSweepEQBlockIndependence:
    def test_split_render_matches_whole(self):
        """Rendering as two 512 blocks equals one 1024 render (state carry)."""
        rng = np.random.default_rng(5)
        big = (rng.standard_normal(1024) * 0.4).astype(np.float32)
        from pysynthrack.core.patch import Cable
        A = 9601

        def run(blocks):
            patch = Patch()
            se = patch.add_module("sweep_eq", params={
                "mode": "bandpass", "freq": 900.0, "q": 5.0, "mix": 1.0})
            patch.cables.append(Cable(
                src_module_id=A, src_port="out",
                dst_module_id=se.id, dst_port="in"))
            be = NumpyBackend(sample_rate=SR, block_size=512)
            be.compile(patch)
            outs = []
            for b in blocks:
                outs.append(be._render_sweep_eq(se, len(b), {(A, "out"): b}, patch))
            return np.concatenate(outs)

        split = run([big[:512], big[512:]])
        whole = run([big])
        assert np.allclose(split, whole, atol=1e-6)
