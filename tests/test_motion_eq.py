"""Tests for MotionEQ — the 4-band EQ with per-band centre-frequency CV."""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.motion_eq import MotionEQ


SR = 44100

# A fixed band layout with real gains so spectral motion is measurable.
_BANDS = {
    "band1_freq": 120.0, "band1_gain": 6.0, "band1_q": 1.5,
    "band2_freq": 500.0, "band2_gain": -4.0, "band2_q": 2.0,
    "band3_freq": 1800.0, "band3_gain": 8.0, "band3_q": 1.0,
    "band4_freq": 6000.0, "band4_gain": -3.0, "band4_q": 1.2,
}


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def _build(bands=None, cv_depth=1.0, cvs=None, tone=110.0, wave="saw", amp=0.4):
    """osc → motion_eq; ``cvs`` = {band_index: constant CV value}."""
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": wave, "freq": tone, "amp": amp}
    )
    params = dict(bands or _BANDS)
    params["cv_depth"] = cv_depth
    m = patch.add_module("motion_eq", params=params)
    patch.connect(osc.id, "out", m.id, "in")
    if cvs:
        for i, val in cvs.items():
            c = patch.add_module("constant", params={"value": float(val)})
            patch.connect(c.id, "out", m.id, f"band{i}_freq_cv")
    return patch, osc, m


def _cap(patch, *ids, N=4096, warm=True):
    be = NumpyBackend(sample_rate=SR, block_size=N)
    be.compile(patch)

    def once():
        bufs = {}
        for mid in be._topo_order:
            mm = patch.modules[mid]
            r = be._render_module(mm, N, bufs, patch)
            if isinstance(r, dict):
                for k, v in r.items():
                    bufs[(mid, k)] = v
            elif r is not None and mm.OUTPUT_PORTS:
                bufs[(mid, mm.OUTPUT_PORTS[0].name)] = r
        return bufs

    if warm:
        once()
    bufs = once()
    return tuple(bufs[(i, "out")] for i in ids)


class TestMotionEQModel:
    def test_register_and_defaults(self):
        patch = Patch()
        m = patch.add_module("motion_eq")
        assert isinstance(m, MotionEQ)
        assert m.params["cv_depth"] == 1.0
        for i in range(1, 5):
            assert f"band{i}_freq" in m.params
            assert f"band{i}_gain" in m.params
            assert f"band{i}_q" in m.params
        assert m.params["gain_cv_depth"] == 6.0
        assert m.params["q_cv_depth"] == 1.0
        assert len(m.params) == 15  # 4*3 band params + 3 shared depths
        names = [p.name for p in m.input_ports]
        assert names == ["in", "band1_freq_cv", "band2_freq_cv",
                         "band3_freq_cv", "band4_freq_cv",
                         "band1_gain_cv", "band2_gain_cv",
                         "band3_gain_cv", "band4_gain_cv",
                         "band1_q_cv", "band2_q_cv",
                         "band3_q_cv", "band4_q_cv"]
        assert m.input_ports[0].signal_kind == "audio"
        assert all(p.signal_kind == "cv" for p in m.input_ports[1:])
        assert [p.name for p in m.output_ports] == ["out"]

    def test_unpatched_input_is_silence(self):
        patch = Patch()
        m = patch.add_module("motion_eq")
        be = NumpyBackend(sample_rate=SR, block_size=128)
        be.compile(patch)
        out = be._render_motion_eq(m, 128, {}, patch)
        assert out.shape == (128,)
        assert np.all(out == 0.0)


class TestMotionEQEquivalence:
    def test_no_cv_matches_parametric_eq(self):
        """With nothing patched into any freq_cv, MotionEQ is bit-identical
        to a ParametricEQ with the same band params."""
        patch_m, osc_m, m = _build()
        (motion,) = _cap(patch_m, m.id, warm=False)

        patch_p = Patch()
        osc_p = patch_p.add_module(
            "oscillator", params={"waveform": "saw", "freq": 110.0, "amp": 0.4}
        )
        peq = patch_p.add_module("parametric_eq", params=dict(_BANDS))
        patch_p.connect(osc_p.id, "out", peq.id, "in")
        (para,) = _cap(patch_p, peq.id, warm=False)

        assert np.array_equal(motion, para)

    def test_all_bands_flat_is_passthrough_even_under_cv(self):
        """All gains at 0 dB → every band is identity, so the output equals
        the dry input no matter where the CV sweeps the centres."""
        flat = {f"band{i}_{p}": v for i in range(1, 5)
                for p, v in (("freq", 200.0 * i), ("gain", 0.0), ("q", 1.5))}
        patch, osc, m = _build(bands=flat, cv_depth=1.0,
                               cvs={1: 1.0, 2: -1.0, 3: 0.5, 4: -0.5})
        dry, out = _cap(patch, osc.id, m.id, warm=False)
        assert np.allclose(out, dry, atol=1e-6)


class TestMotionEQPerBandCV:
    def test_band_cv_equals_static_shift_and_isolates(self):
        """+1.0 CV at unit depth on band 2 moves *only* band 2's centre
        500 → 1000 Hz: bit-identical to a static band2_freq=1000, and
        different from the no-CV render (something actually moved)."""
        p1, _, m1 = _build(cvs={2: 1.0}, cv_depth=1.0)
        (swept,) = _cap(p1, m1.id, warm=False)

        moved = dict(_BANDS); moved["band2_freq"] = 1000.0
        p2, _, m2 = _build(bands=moved)  # no CV
        (static,) = _cap(p2, m2.id, warm=False)
        assert np.allclose(swept, static, atol=1e-6)

        p0, _, m0 = _build()  # no CV at all
        (nocv,) = _cap(p0, m0.id, warm=False)
        assert not np.allclose(swept, nocv, atol=1e-4)

    def test_two_bands_move_independently(self):
        """CV on band1 and band3 at once equals a static EQ with just those
        two centres moved — bands are independent."""
        p, _, m = _build(cvs={1: 1.0, 3: -1.0}, cv_depth=1.0)
        (swept,) = _cap(p, m.id, warm=False)

        moved = dict(_BANDS)
        moved["band1_freq"] = 120.0 * 2.0    # +1 oct
        moved["band3_freq"] = 1800.0 / 2.0   # -1 oct
        p2, _, m2 = _build(bands=moved)
        (static,) = _cap(p2, m2.id, warm=False)
        assert np.allclose(swept, static, atol=1e-6)

    def test_shared_cv_depth_scales_all_bands(self):
        """One cv_depth scales every band's sweep: depth=2 with +0.5 CV on
        all four bands doubles every centre."""
        p, _, m = _build(cv_depth=2.0, cvs={1: 0.5, 2: 0.5, 3: 0.5, 4: 0.5})
        (swept,) = _cap(p, m.id, warm=False)
        moved = {f"band{i}_{k}": (_BANDS[f"band{i}_freq"] * 2.0 if k == "freq"
                                  else _BANDS[f"band{i}_{k}"])
                 for i in range(1, 5) for k in ("freq", "gain", "q")}
        p2, _, m2 = _build(bands=moved)
        (static,) = _cap(p2, m2.id, warm=False)
        assert np.allclose(swept, static, atol=1e-6)

    def test_cv_depth_zero_disables(self):
        p, _, m = _build(cv_depth=0.0, cvs={1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0})
        (off,) = _cap(p, m.id, warm=False)
        p0, _, m0 = _build()
        (nocv,) = _cap(p0, m0.id, warm=False)
        assert np.allclose(off, nocv, atol=1e-6)


class TestMotionEQBehavior:
    def test_sweeping_a_boost_band_tracks_a_tone(self):
        """A single +12 dB band at 500 Hz boosts a 1000 Hz tone much more
        once its centre is swept up onto it (+1.0 CV → 1000 Hz)."""
        one = {"band1_freq": 500.0, "band1_gain": 12.0, "band1_q": 3.0,
               "band2_freq": 8000.0, "band2_gain": 0.0, "band2_q": 1.0,
               "band3_freq": 9000.0, "band3_gain": 0.0, "band3_q": 1.0,
               "band4_freq": 10000.0, "band4_gain": 0.0, "band4_q": 1.0}
        pb, _, mb = _build(bands=one, tone=1000.0, wave="sine")
        (rb,) = _cap(pb, mb.id)
        ps, _, ms = _build(bands=one, tone=1000.0, wave="sine", cvs={1: 1.0})
        (rs,) = _cap(ps, ms.id)
        assert _rms(rs) > 1.5 * _rms(rb)


class TestMotionEQVoice:
    @pytest.mark.parametrize("cv_band", [1, 3])
    def test_voice_row_matches_mono(self, cv_band):
        from pysynthrack.core.patch import Cable
        N = 512
        t = np.arange(N) / SR
        tone = (np.sin(2 * np.pi * 600.0 * t) * 0.4).astype(np.float32)
        cv = np.full(N, 0.5, dtype=np.float32)
        A, C = 7001, 7002

        def render(audio):
            patch = Patch()
            m = patch.add_module("motion_eq", params=dict(_BANDS, cv_depth=1.0))
            patch.cables.append(Cable(
                src_module_id=A, src_port="out",
                dst_module_id=m.id, dst_port="in"))
            patch.cables.append(Cable(
                src_module_id=C, src_port="out",
                dst_module_id=m.id, dst_port=f"band{cv_band}_freq_cv"))
            be = NumpyBackend(sample_rate=SR, block_size=N)
            be.compile(patch)
            return be._render_motion_eq(
                m, N, {(A, "out"): audio, (C, "out"): cv}, patch)

        mono = render(tone)
        va = np.zeros((16, N), dtype=np.float32)
        va[9] = tone
        voice = render(va)
        assert voice.shape == (16, N)
        assert np.allclose(voice[9], mono, atol=1e-6)
        for i in range(16):
            if i == 9:
                continue
            assert float(np.max(np.abs(voice[i]))) == 0.0


class TestMotionEQBlockIndependence:
    def test_split_render_matches_whole(self):
        from pysynthrack.core.patch import Cable
        rng = np.random.default_rng(9)
        big = (rng.standard_normal(1024) * 0.3).astype(np.float32)
        A, C = 7101, 7102
        cvfull = np.full(1024, 0.7, dtype=np.float32)

        def run(chunks):
            patch = Patch()
            m = patch.add_module("motion_eq", params=dict(_BANDS, cv_depth=1.0))
            patch.cables.append(Cable(
                src_module_id=A, src_port="out",
                dst_module_id=m.id, dst_port="in"))
            patch.cables.append(Cable(
                src_module_id=C, src_port="out",
                dst_module_id=m.id, dst_port="band2_freq_cv"))
            be = NumpyBackend(sample_rate=SR, block_size=512)
            be.compile(patch)
            outs = []
            off = 0
            for c in chunks:
                n = len(c)
                outs.append(be._render_motion_eq(
                    m, n, {(A, "out"): c, (C, "out"): cvfull[off:off + n]}, patch))
                off += n
            return np.concatenate(outs)

        split = run([big[:512], big[512:]])
        whole = run([big])
        assert np.allclose(split, whole, atol=1e-6)
