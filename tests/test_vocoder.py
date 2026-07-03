"""Tests for the Vocoder (channel vocoder: matched band banks + followers).

Coverage:
  - Model: registration, defaults, ports/kinds (audio ``mod`` +
    ``carrier`` -> ``out`` audio), JSON round-trip, unknown-param
    rejection, and the signal-kind type walls.
  - DSP: no carrier -> silence (regardless of the modulator); no
    modulator -> the bands close (silence at ``mix=1``); ``mix=0`` is a
    bit-exact carrier passthrough; the modulator's envelope gates the
    carrier (on/off level ratio); band selectivity -- a modulator tone
    at band i opens band i of the carrier and not a distant band j;
    ``width`` widens the bands (more cross-band leakage when wide);
    ``gain`` scales the wet path linearly and never the ``mix=0`` dry;
    the hiss path passes modulator sibilance as high-frequency noise
    (silent at ``hiss=0``); output is finite/bounded at extreme
    settings; ``bands`` snaps to a legal count (8/12/16/24) and a live
    band-count change survives (state reinit, no crash).
  - Block independence: DF-I filter history, follower levels and the
    noise generator's stream position all carry across blocks, so the
    output is bit-identical at any block size.
  - Voice: a voice-aware (2D) input on either jack is summed to mono,
    and a single-voice input is bit-identical to mono.
  - Integration: osc (mod) + osc (carrier) -> vocoder -> speaker
    renders audible audio through the compiled graph.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.vocoder import Vocoder

SR = 44100
F = 512

# The default band layout, mirrored here for picking test frequencies.
LO, HI, NBANDS = 120.0, 7500.0, 16
CENTRES = np.geomspace(LO, HI, NBANDS)


def _rig(params=None, block=F):
    patch = Patch()
    mo = patch.add_module("oscillator")
    ca = patch.add_module("oscillator")
    vo = patch.add_module("vocoder", params=params or {})
    patch.connect(mo.id, "out", vo.id, "mod")
    patch.connect(ca.id, "out", vo.id, "carrier")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, mo, ca, vo, b


def _run(patch, mo, ca, vo, b, mod, car, block=F):
    n = (mod.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {
            (mo.id, "out"): mod[..., sl].astype(np.float32),
            (ca.id, "out"): car[..., sl].astype(np.float32),
        }
        outs.append(b._render_vocoder(vo, block, bufs, patch))
    return np.concatenate(outs)


def _sine(freq, n, amp=1.0):
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _rich(n, base=110.0, harmonics=40, amp=0.3):
    """A bright, harmonically dense carrier (bounded saw-ish stack)."""
    t = np.arange(n) / SR
    x = sum(np.sin(2 * np.pi * base * k * t) / k for k in range(1, harmonics))
    return (amp * x).astype(np.float32)


def _hf_rms(x, above_hz=6000.0):
    sp = np.abs(np.fft.rfft(x))
    fr = np.fft.rfftfreq(len(x), 1.0 / SR)
    return float(np.sqrt(np.mean(sp[fr > above_hz] ** 2)))


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        vo = Patch().add_module("vocoder")
        assert isinstance(vo, Vocoder)
        assert vo.params == {
            "bands": 16,
            "freq_lo": 120.0,
            "freq_hi": 7500.0,
            "width": 1.0,
            "attack": 4.0,
            "release": 60.0,
            "hiss": 0.4,
            "gain": 1.0,
            "mix": 1.0,
        }

    def test_category(self):
        assert Vocoder.CATEGORY == "Effects"

    def test_ports_and_kinds(self):
        vo = Patch().add_module("vocoder")
        assert [(p.name, p.signal_kind) for p in vo.input_ports] == [
            ("mod", "audio"),
            ("carrier", "audio"),
        ]
        assert [(p.name, p.signal_kind) for p in vo.output_ports] == [
            ("out", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "vocoder",
            params={"bands": 24, "width": 1.8, "hiss": 0.9, "release": 250.0},
        )
        restored = Patch.from_dict(patch.to_dict())
        vo = next(m for m in restored if m.TYPE == "vocoder")
        assert vo.params["bands"] == 24
        assert vo.params["width"] == 1.8
        assert vo.params["hiss"] == 0.9
        assert vo.params["release"] == 250.0

    def test_unknown_param_rejected(self):
        # No ``depth`` (chorus/flanger/phaser) or ``drive`` (distortion).
        with pytest.raises(KeyError):
            Patch().add_module("vocoder", params={"depth": 0.5})

    def test_audio_into_both_jacks_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        vo = patch.add_module("vocoder")
        patch.connect(osc.id, "out", vo.id, "mod")     # no raise
        patch.connect(osc.id, "out", vo.id, "carrier")  # no raise

    def test_cv_into_mod_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        vo = patch.add_module("vocoder")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", vo.id, "mod")

    def test_cv_into_carrier_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        vo = patch.add_module("vocoder")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", vo.id, "carrier")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        vo = patch.add_module("vocoder")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(vo.id, "out", vca.id, "cv")


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_no_carrier_is_silent(self):
        patch = Patch()
        mo = patch.add_module("oscillator")
        vo = patch.add_module("vocoder")
        patch.connect(mo.id, "out", vo.id, "mod")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        bufs = {(mo.id, "out"): _sine(440.0, F)}
        o = b._render_vocoder(vo, F, bufs, patch)
        assert o.shape == (F,) and o.dtype == np.float32
        assert not np.any(o)

    def test_no_modulator_closes_the_bands(self):
        patch = Patch()
        ca = patch.add_module("oscillator")
        vo = patch.add_module("vocoder", params={"hiss": 0.0})
        patch.connect(ca.id, "out", vo.id, "carrier")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        car = _rich(F * 8)
        outs = []
        for k in range(8):
            bufs = {(ca.id, "out"): car[k * F:(k + 1) * F]}
            outs.append(b._render_vocoder(vo, F, bufs, patch))
        out = np.concatenate(outs)
        assert np.max(np.abs(out)) < 1e-6  # envelopes never open

    def test_mix_zero_is_bit_exact_carrier(self):
        patch, mo, ca, vo, b = _rig({"mix": 0.0, "hiss": 1.0, "gain": 4.0})
        mod = _sine(500.0, F * 4)
        car = _rich(F * 4)
        out = _run(patch, mo, ca, vo, b, mod, car)
        assert np.array_equal(out, car[: len(out)])

    def test_modulator_envelope_gates_the_carrier(self):
        patch, mo, ca, vo, b = _rig({"hiss": 0.0})
        n = SR  # 1 s: tone for the first half, silence after
        t = np.arange(n) / SR
        mod = (_sine(500.0, n) * (t < 0.5)).astype(np.float32)
        car = _rich(n)
        out = _run(patch, mo, ca, vo, b, mod, car)
        on = np.sqrt(np.mean(out[: int(0.45 * SR)] ** 2))
        off = np.sqrt(np.mean(out[int(0.7 * SR):] ** 2))
        assert on > 0.01
        assert on / max(off, 1e-12) > 20.0

    def test_band_selectivity(self):
        # Modulator tone at band 4's centre; carrier holds tones at band
        # 4 and band 12. The output should be dominated by band 4.
        fi, fj = CENTRES[4], CENTRES[12]
        patch, mo, ca, vo, b = _rig({"hiss": 0.0})
        n = F * 16
        mod = _sine(fi, n)
        car = (_sine(fi, n, 0.4) + _sine(fj, n, 0.4)).astype(np.float32)
        out = _run(patch, mo, ca, vo, b, mod, car)
        seg = out[F * 8:]  # settled
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1.0 / SR)

        def peak(f):
            m = (freqs > f * 0.9) & (freqs < f * 1.1)
            return spec[m].max()

        assert peak(fi) / peak(fj) > 4.0

    def test_width_widens_the_bands(self):
        # Modulator at band 4, carrier two bands up: a wide setting
        # leaks far more of the carrier through than a narrow one.
        fi, fk = CENTRES[4], CENTRES[6]
        n = F * 16
        mod = _sine(fi, n)
        car = _sine(fk, n)
        p1, m1, c1, v1, b1 = _rig({"hiss": 0.0, "width": 0.5})
        p2, m2, c2, v2, b2 = _rig({"hiss": 0.0, "width": 3.0})
        o_narrow = _run(p1, m1, c1, v1, b1, mod, car)
        o_wide = _run(p2, m2, c2, v2, b2, mod, car)
        rn = np.sqrt(np.mean(o_narrow[F * 8:] ** 2))
        rw = np.sqrt(np.mean(o_wide[F * 8:] ** 2))
        assert rw > 2.0 * rn

    def test_gain_scales_wet_linearly(self):
        mod = _sine(500.0, F * 4)
        car = _rich(F * 4)
        p1, m1, c1, v1, b1 = _rig({"gain": 1.0})
        p2, m2, c2, v2, b2 = _rig({"gain": 2.0})
        o1 = _run(p1, m1, c1, v1, b1, mod, car)
        o2 = _run(p2, m2, c2, v2, b2, mod, car)
        assert np.allclose(o2, 2.0 * o1, rtol=1e-5, atol=1e-7)

    def test_hiss_path_carries_sibilance(self):
        # A noisy (sibilant) modulator + a low sine carrier: with the
        # bands unable to say anything above the carrier's 150 Hz, any
        # high-frequency output comes from the hiss path alone.
        rng = np.random.default_rng(11)
        n = F * 16
        mod = rng.uniform(-0.5, 0.5, n).astype(np.float32)
        car = _sine(150.0, n)
        p1, m1, c1, v1, b1 = _rig({"hiss": 0.0})
        p2, m2, c2, v2, b2 = _rig({"hiss": 1.0})
        o_off = _run(p1, m1, c1, v1, b1, mod, car)
        o_on = _run(p2, m2, c2, v2, b2, mod, car)
        assert _hf_rms(o_on[F * 8:]) > 50.0 * max(_hf_rms(o_off[F * 8:]), 1e-9)

    def test_release_hangs_on_longer(self):
        n = SR
        t = np.arange(n) / SR
        mod = (_sine(500.0, n) * (t < 0.25)).astype(np.float32)
        car = _rich(n)
        p1, m1, c1, v1, b1 = _rig({"hiss": 0.0, "release": 5.0})
        p2, m2, c2, v2, b2 = _rig({"hiss": 0.0, "release": 500.0})
        o_short = _run(p1, m1, c1, v1, b1, mod, car)
        o_long = _run(p2, m2, c2, v2, b2, mod, car)
        window = slice(int(0.3 * SR), int(0.45 * SR))  # just after gate-off
        tail_short = np.sqrt(np.mean(o_short[window] ** 2))
        tail_long = np.sqrt(np.mean(o_long[window] ** 2))
        assert tail_long > 3.0 * tail_short

    def test_bands_snap_to_legal_counts(self):
        mod = _sine(500.0, F)
        car = _rich(F)
        for asked, snapped in ((13, 12), (100, 24), (1, 8), ("16", 16)):
            patch, mo, ca, vo, b = _rig({"bands": asked})
            _run(patch, mo, ca, vo, b, mod, car)
            st = b._state[vo.id]
            assert st["n_bands"] == snapped
            assert st["env"].shape == (snapped + 1,)

    def test_live_band_count_change(self):
        patch, mo, ca, vo, b = _rig()
        mod = _sine(500.0, F * 4)
        car = _rich(F * 4)
        _run(patch, mo, ca, vo, b, mod, car)
        vo.params["bands"] = 8  # live edit -> state reinit, no crash
        out = _run(patch, mo, ca, vo, b, mod, car)
        assert np.isfinite(out).all()
        assert b._state[vo.id]["n_bands"] == 8

    def test_extreme_settings_stay_bounded(self):
        params = {
            "bands": 24, "freq_lo": 500.0, "freq_hi": 12000.0,
            "width": 3.0, "attack": 0.1, "release": 1.0,
            "hiss": 1.0, "gain": 4.0, "mix": 1.0,
        }
        patch, mo, ca, vo, b = _rig(params)
        rng = np.random.default_rng(3)
        n = F * 8
        mod = rng.uniform(-1, 1, n).astype(np.float32)
        car = rng.uniform(-1, 1, n).astype(np.float32)
        out = _run(patch, mo, ca, vo, b, mod, car)
        assert np.isfinite(out).all()
        assert np.max(np.abs(out)) < 50.0

    def test_zero_frames(self):
        patch, mo, ca, vo, b = _rig()
        bufs = {
            (mo.id, "out"): np.empty(0, dtype=np.float32),
            (ca.id, "out"): np.empty(0, dtype=np.float32),
        }
        o = b._render_vocoder(vo, 0, bufs, patch)
        assert o.shape == (0,) and o.dtype == np.float32


# ----- Block independence ----------------------------------------------------


class TestBlockIndependence:
    def _material(self, n=8192):
        rng = np.random.default_rng(7)
        t = np.arange(n) / SR
        mod = (
            np.sin(2 * np.pi * 440 * t)
            * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
        ).astype(np.float32)
        car = rng.uniform(-0.5, 0.5, n).astype(np.float32)
        return mod, car

    @pytest.mark.parametrize("block", [512, 160, 4096])
    def test_bit_identical_at_any_block_size(self, block):
        mod, car = self._material()
        p1, m1, c1, v1, b1 = _rig(block=8192)
        ref = _run(p1, m1, c1, v1, b1, mod, car, block=8192)
        p2, m2, c2, v2, b2 = _rig(block=block)
        got = _run(p2, m2, c2, v2, b2, mod, car, block=block)
        n = len(got)
        assert np.array_equal(ref[:n], got)


# ----- Voice handling --------------------------------------------------------


class TestVoice:
    def test_voice_inputs_summed_to_mono(self):
        patch, mo, ca, vo, b = _rig()
        n = F * 4
        mod_v = np.stack([_sine(500.0, n, 0.5), _sine(500.0, n, 0.5)])
        car_v = np.stack([_rich(n, amp=0.15), _rich(n, amp=0.15)])
        out_v = _run(patch, mo, ca, vo, b, mod_v, car_v)

        p2, m2, c2, v2, b2 = _rig()
        out_m = _run(
            p2, m2, c2, v2, b2, mod_v.sum(axis=0), car_v.sum(axis=0)
        )
        assert np.allclose(out_v, out_m, atol=1e-6)

    def test_single_voice_row_matches_mono(self):
        patch, mo, ca, vo, b = _rig()
        n = F * 4
        mod = _sine(500.0, n)
        car = _rich(n)
        out_v = _run(patch, mo, ca, vo, b, mod[None, :], car[None, :])
        p2, m2, c2, v2, b2 = _rig()
        out_m = _run(p2, m2, c2, v2, b2, mod, car)
        assert np.array_equal(out_v, out_m)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_full_graph_renders_audio(self):
        patch = Patch()
        mo = patch.add_module("oscillator", params={"freq": 300.0})
        ca = patch.add_module("oscillator", params={"waveform": "saw", "freq": 110.0})
        vo = patch.add_module("vocoder")
        spk = patch.add_module("speaker_output")
        patch.connect(mo.id, "out", vo.id, "mod")
        patch.connect(ca.id, "out", vo.id, "carrier")
        patch.connect(vo.id, "out", spk.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        blocks = [b.render_block(F) for _ in range(8)]
        out = np.concatenate([bl for bl in blocks])
        assert np.isfinite(out).all()
        assert np.max(np.abs(out)) > 1e-4
