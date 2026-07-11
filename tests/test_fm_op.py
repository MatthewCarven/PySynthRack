"""Tests for the FMOperator module (one DX-style phase-modulation operator).

Coverage:
  - Model: registration/defaults, ports & signal kinds (pitch_cv/pm/amp_cv/
    index_cv -> out), category, JSON round-trip, unknown param rejected,
    ``snap_ratio`` / ``RATIO_TABLE``.
  - Frequency: unpatched carrier at C4 * ratio; ``ratio`` scales pitch and
    snaps to the table; ``fine`` detunes in cents; ``pitch_cv`` is 1 V/oct;
    ``fixed`` mode runs at ``freq`` Hz and ignores ``pitch_cv``.
  - FM correctness: a sine into ``pm`` at a 1:1 ratio produces the analytic
    Bessel sideband amplitudes J_k(beta); ``index`` scales beta.
  - Feedback: the fb=0 vectorized path is bit-identical to a per-sample
    oracle; ``feedback`` > 0 brightens (adds partials).
  - index_cv: raises/lowers the effective index (depth 0 disables; floored
    at 0). amp_cv: linear output level (unpatched -> unity).
  - Invariants: a single voice row is bit-identical to mono; voices are
    independent; (V, F) shape preserved; block-size independent (< 1e-6);
    extremes finite and bounded; zero frames handled.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.special import jn

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.fm_op import RATIO_TABLE, FMOperator, snap_ratio

SR, F = 44100, 512
C4 = 261.6256  # carrier pitch at pitch_cv = 0 V


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _render(patch, op, n, block=F, feeds=None):
    """Render ``n`` samples of ``op`` in ``block`` chunks.

    ``feeds`` maps a source module id to a full-length ``(n,)`` or ``(V, n)``
    array; each block slice is published on that source's ``out`` port.
    """
    b = _backend(block)
    b.compile(patch)
    outs = []
    i = 0
    while i < n:
        fr = min(block, n - i)
        bufs = {}
        if feeds:
            for sid, arr in feeds.items():
                bufs[(sid, "out")] = arr[..., i:i + fr]
        outs.append(b._render_fm_op(op, fr, bufs, patch))
        i += fr
    return np.concatenate(outs, axis=-1)


def _feed(patch, op, port, kind="cv"):
    """Add a dummy source feeding ``op.port`` and return its id (for feeds)."""
    src = patch.add_module("constant" if kind == "cv" else "oscillator")
    patch.connect(src.id, "out", op.id, port)
    return src.id


def _sine(freq, amp=1.0, n=F * 128):
    t = np.arange(n) / SR
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _peak_hz(x, n=16384):
    seg = x[-n:] * np.hanning(n)
    sp = np.abs(np.fft.rfft(seg))
    return np.argmax(sp) * SR / n


def _line_spectrum(x):
    """Leakage-free line amplitudes over exactly 1 s (integer-Hz components
    complete whole cycles, so the rectangular FFT has no spectral leakage)."""
    seg = x[-SR:]
    return np.abs(np.fft.rfft(seg)) * 2.0 / SR


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        op = Patch().add_module("fm_op")
        assert isinstance(op, FMOperator)
        assert op.params == {
            "ratio": 1.0,
            "fine": 0.0,
            "index": 1.0,
            "index_cv_depth": 1.0,
            "feedback": 0.0,
            "fixed": False,
            "freq": 220.0,
        }

    def test_category(self):
        assert FMOperator.CATEGORY == "Sources"

    def test_ports_and_signal_kinds(self):
        op = Patch().add_module("fm_op")
        ins = {p.name: p.signal_kind for p in op.input_ports}
        outs = {p.name: p.signal_kind for p in op.output_ports}
        assert ins == {
            "pitch_cv": "cv",
            "pm": "audio",
            "amp_cv": "cv",
            "index_cv": "cv",
        }
        assert outs == {"out": "audio"}

    def test_json_round_trip(self):
        op = Patch().add_module(
            "fm_op",
            params={"ratio": 3.5, "index": 4.0, "feedback": 0.5, "fixed": True},
        )
        clone = FMOperator.from_dict(op.to_dict())
        assert clone.params == op.params

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("fm_op", params={"bogus": 1.0})

    def test_snap_ratio_nearest(self):
        assert snap_ratio(1.0) == 1.0
        assert snap_ratio(1.4) == 1.5      # nearer 1.5 than 1.25/1.0
        assert snap_ratio(3.4) == 3.5
        assert snap_ratio(0.0) == 0.25     # clamps onto the low end
        assert snap_ratio(999.0) == 16.0   # clamps onto the high end

    def test_ratio_table_sane(self):
        assert RATIO_TABLE[0] == 0.25 and RATIO_TABLE[-1] == 16.0
        assert list(RATIO_TABLE) == sorted(RATIO_TABLE)
        assert len(set(RATIO_TABLE)) == len(RATIO_TABLE)


# ----- Frequency -------------------------------------------------------------


class TestFrequency:
    def test_unpatched_carrier_at_c4(self):
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.0})
        y = _render(patch, op, F * 128)
        assert _peak_hz(y) == pytest.approx(C4, abs=3.0)
        assert y.dtype == np.float32

    def test_ratio_scales_pitch(self):
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 2.0})
        assert _peak_hz(_render(patch, op, F * 128)) == pytest.approx(
            2 * C4, abs=4.0
        )

    def test_ratio_snaps_to_table(self):
        # 1.4 is off-table -> snaps to 1.5 -> carrier at 1.5 * C4.
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.4})
        assert _peak_hz(_render(patch, op, F * 128)) == pytest.approx(
            1.5 * C4, abs=4.0
        )

    def test_fine_detune_cents(self):
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.0, "fine": 50.0})
        expected = C4 * 2.0 ** (50.0 / 1200.0)
        assert _peak_hz(_render(patch, op, F * 128)) == pytest.approx(
            expected, abs=4.0
        )

    def test_pitch_cv_one_volt_per_octave(self):
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.0})
        pid = _feed(patch, op, "pitch_cv")
        y = _render(patch, op, F * 128, feeds={pid: np.ones(F * 128, np.float32)})
        assert _peak_hz(y) == pytest.approx(2 * C4, abs=6.0)  # +1 V = +1 oct

    def test_fixed_mode_ignores_pitch_cv(self):
        patch = Patch()
        op = patch.add_module("fm_op", params={"fixed": True, "freq": 800.0})
        pid = _feed(patch, op, "pitch_cv")
        # A +1 V pitch cv would double a tracking oscillator; fixed ignores it.
        y = _render(patch, op, F * 128, feeds={pid: np.ones(F * 128, np.float32)})
        assert _peak_hz(y) == pytest.approx(800.0, abs=4.0)


# ----- FM correctness (Bessel) ----------------------------------------------


class TestBessel:
    def _render_pm(self, index, fc=1000.0, fm=100.0, amp=1.0, index_cv=None,
                   depth=1.0):
        patch = Patch()
        op = patch.add_module(
            "fm_op",
            params={"fixed": True, "freq": fc, "index": index,
                    "index_cv_depth": depth},
        )
        pid = _feed(patch, op, "pm", kind="audio")
        n = F * 128
        feeds = {pid: _sine(fm, amp, n)}
        if index_cv is not None:
            iid = _feed(patch, op, "index_cv")
            feeds[iid] = np.full(n, index_cv, np.float32)
        return _render(patch, op, n, feeds=feeds)

    def test_bessel_sidebands(self):
        # PM by a unit sine at index beta -> sideband k has amplitude |J_k(beta)|.
        for beta in (1.0, 2.0, 3.0):
            sp = _line_spectrum(self._render_pm(beta))
            meas = np.array([sp[int(1000 + k * 100)] for k in range(6)])
            ref = np.array([abs(jn(k, beta)) for k in range(6)])
            assert np.max(np.abs(meas - ref)) < 5e-3

    def test_index_scales_beta(self):
        # Higher index -> a larger modulation index beta -> the first sideband
        # (J_1) grows (J_1 is monotonic up to beta ~1.84).
        j_lo = _line_spectrum(self._render_pm(0.5))[1000 + 100]
        j_hi = _line_spectrum(self._render_pm(1.5))[1000 + 100]
        assert j_hi > j_lo
        assert j_lo == pytest.approx(abs(jn(1, 0.5)), abs=5e-3)
        assert j_hi == pytest.approx(abs(jn(1, 1.5)), abs=5e-3)


# ----- Feedback --------------------------------------------------------------


class TestFeedback:
    def test_fb0_matches_per_sample_oracle(self):
        # The vectorized fb=0 path must equal a hand-written per-sample loop.
        patch = Patch()
        op = patch.add_module(
            "fm_op", params={"ratio": 1.0, "index": 3.0, "feedback": 0.0}
        )
        pid = _feed(patch, op, "pm", kind="audio")
        n = F * 64
        mod = _sine(173.0, 0.7, n)
        y = _render(patch, op, n, feeds={pid: mod})

        # Oracle: same phase accumulation + index*pm, looped, fb term = 0.
        ph = 0.0
        ref = []
        i = 0
        while i < n:
            fr = min(F, n - i)
            inc = np.full(fr, C4 / SR)
            csum = np.cumsum(inc)
            phase = (ph + csum - inc) % 1.0
            ph = (ph + csum[-1]) % 1.0
            core = np.sin(2 * np.pi * phase + 3.0 * mod[i:i + fr].astype(np.float64))
            ref.append(core.astype(np.float32))
            i += fr
        assert np.array_equal(y, np.concatenate(ref))

    def test_feedback_adds_partials(self):
        # A lone operator with no pm: fb=0 is a pure sine (one partial);
        # feedback injects harmonics above the fundamental. Fixed carrier at
        # an integer 1000 Hz so the leakage-free line spectrum is clean (its
        # harmonics land on bins too).
        def hi_energy(fb):
            patch = Patch()
            op = patch.add_module(
                "fm_op", params={"fixed": True, "freq": 1000.0, "feedback": fb}
            )
            sp = _line_spectrum(_render(patch, op, F * 128))
            return float(np.sum(sp[1500:]))  # everything above the fundamental
        assert hi_energy(0.0) < 1e-3
        assert hi_energy(0.8) > 10 * hi_energy(0.0) + 1e-3


# ----- index_cv & amp_cv -----------------------------------------------------


class TestIndexCV:
    def _sideband_energy(self, index, index_cv=None, depth=1.0):
        patch = Patch()
        op = patch.add_module(
            "fm_op",
            params={"fixed": True, "freq": 1000.0, "index": index,
                    "index_cv_depth": depth},
        )
        pid = _feed(patch, op, "pm", kind="audio")
        n = F * 128
        feeds = {pid: _sine(100.0, 1.0, n)}
        if index_cv is not None:
            iid = _feed(patch, op, "index_cv")
            feeds[iid] = np.full(n, index_cv, np.float32)
        sp = _line_spectrum(_render(patch, op, n, feeds=feeds))
        # everything but the carrier bin
        e = float(np.sum(sp)) - sp[1000]
        return e

    def test_index_cv_raises_index(self):
        base = self._sideband_energy(1.0)
        boosted = self._sideband_energy(1.0, index_cv=1.0, depth=3.0)
        assert boosted > base

    def test_index_cv_depth_zero_disables(self):
        a = self._sideband_energy(2.0)
        b = self._sideband_energy(2.0, index_cv=5.0, depth=0.0)
        assert b == pytest.approx(a, abs=1e-6)

    def test_effective_index_floored_at_zero(self):
        # index 1, cv -5, depth 1 -> eff index max(1-5,0) = 0 -> no sidebands,
        # a pure carrier equal to the index=0 render.
        floored = self._sideband_energy(1.0, index_cv=-5.0, depth=1.0)
        assert floored == pytest.approx(0.0, abs=1e-4)


class TestAmpCV:
    def test_amp_cv_scales_output(self):
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.0})
        aid = _feed(patch, op, "amp_cv")
        n = F * 16
        half = _render(patch, op, n, feeds={aid: np.full(n, 0.5, np.float32)})
        patch2 = Patch()
        op2 = patch2.add_module("fm_op", params={"ratio": 1.0})
        full = _render(patch2, op2, n)
        assert np.allclose(half, 0.5 * full, atol=1e-6)

    def test_amp_cv_unpatched_is_unity(self):
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.0})
        y = _render(patch, op, F * 16)
        assert float(np.max(np.abs(y))) == pytest.approx(1.0, abs=1e-3)


# ----- Invariants ------------------------------------------------------------


class TestInvariants:
    def test_single_voice_row_matches_mono(self):
        # Mono pm (1D) vs a (1, N) pm must give bit-identical audio.
        n = F * 8
        mod = _sine(200.0, 0.6, n)

        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.0, "index": 2.0})
        pid = _feed(patch, op, "pm", kind="audio")
        mono = _render(patch, op, n, feeds={pid: mod})

        patch2 = Patch()
        op2 = patch2.add_module("fm_op", params={"ratio": 1.0, "index": 2.0})
        pid2 = _feed(patch2, op2, "pm", kind="audio")
        voice = _render(patch2, op2, n, feeds={pid2: mod[np.newaxis, :]})

        assert mono.ndim == 1 and voice.ndim == 2 and voice.shape[0] == 1
        assert np.array_equal(mono, voice[0])

    def test_voices_are_independent(self):
        # Per-voice pitch_cv -> each row sits at its own frequency.
        n = F * 128
        pitch = np.zeros((3, n), np.float32)
        pitch[1] = 1.0    # +1 oct
        pitch[2] = -1.0   # -1 oct
        patch = Patch()
        op = patch.add_module("fm_op", params={"ratio": 1.0})
        pid = _feed(patch, op, "pitch_cv")
        y = _render(patch, op, n, feeds={pid: pitch})
        assert y.shape == (3, n)
        assert _peak_hz(y[0]) == pytest.approx(C4, abs=4.0)
        assert _peak_hz(y[1]) == pytest.approx(2 * C4, abs=6.0)
        assert _peak_hz(y[2]) == pytest.approx(C4 / 2, abs=3.0)

    def test_block_size_independent(self):
        # Same audio (< 1e-6) at 512 vs 256, feedback engaged.
        n = F * 128
        mod = _sine(140.0, 0.5, n)

        def run(block):
            patch = Patch()
            op = patch.add_module(
                "fm_op", params={"ratio": 1.0, "index": 2.0, "feedback": 0.6}
            )
            pid = _feed(patch, op, "pm", kind="audio")
            return _render(patch, op, n, block=block, feeds={pid: mod})

        assert np.max(np.abs(run(512) - run(256))) < 1e-6

    def test_extremes_finite_and_bounded(self):
        # Max index + max feedback + full-scale pm + huge index_cv: still
        # finite, and |out| <= 1 (unity amp; sin is bounded).
        n = F * 32
        patch = Patch()
        op = patch.add_module(
            "fm_op",
            params={"ratio": 16.0, "index": 10.0, "index_cv_depth": 50.0,
                    "feedback": 1.0},
        )
        pid = _feed(patch, op, "pm", kind="audio")
        iid = _feed(patch, op, "index_cv")
        y = _render(patch, op, n, feeds={
            pid: _sine(3000.0, 1.0, n),
            iid: np.full(n, 5.0, np.float32),
        })
        assert np.all(np.isfinite(y))
        assert float(np.max(np.abs(y))) <= 1.0 + 1e-6

    def test_zero_frames(self):
        patch = Patch()
        op = patch.add_module("fm_op")
        b = _backend()
        b.compile(patch)
        out = b._render_fm_op(op, 0, {}, patch)
        assert out.shape == (0,)


# ----- Examples --------------------------------------------------------------


class TestExamples:
    @pytest.mark.parametrize("name", ["fm_op_bell", "fm_op_epiano"])
    def test_example_loads_and_renders(self, name):
        """Each shipped fm_op example must load and produce audio within
        headroom (a clock triggers the note envelopes; render long enough to
        catch a hit and its decay)."""
        from pathlib import Path

        from pysynthrack.io_patch import load_patch

        patch = load_patch(Path(__file__).parent.parent / "examples" / f"{name}.json")
        assert sum(1 for m in patch if m.TYPE == "fm_op") >= 2

        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(300):  # ~3.5 s
            blk = b.render_block(F)
            if blk is None:
                continue
            assert np.all(np.isfinite(blk))
            peak = max(peak, float(np.max(np.abs(blk))))
        assert peak > 0.05          # audible
        assert peak <= 1.0          # speaker headroom
