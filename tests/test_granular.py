"""Granular — grain cloud over a live ring buffer (slice 1: mono, synchronous).

Coverage:
  - Model: registration in Effects, defaults, ports/signal kinds, JSON
    round-trip, unknown-param rejection, type walls (cv -> in refused).
  - Silence: unpatched -> zeros; silence in -> silence out; zero frames.
  - The neutral: hann grains at 50% overlap (the defaults) tile to one,
    so pitch 0 / position 0 is the input delayed by the 2-sample head
    start -- BIT-EXACT in float32. `position` 0.25 of a 2 s buffer is
    the input delayed by exactly 24000 samples; a fractional position
    lands an impulse where it should.
  - mix 0 is the dry, head-start delayed, bit-exact; mix 0.5 is the
    average of the mix-0 and mix-1 renders.
  - Pitch: INSIDE a grain the period is 2^(st/12) exact (zero-crossing
    period over the central half of isolated grains, +12 / +7 / -12 /
    -5); ACROSS grains a synchronous train puts sidebands at +-density,
    so on a pure tone the spectral peak lands within +-density Hz of the
    target -- the classic un-hidden granular artefact, documented.
  - Scheduler: `density` grains per second (counted as runs on a DC
    input); a density rise takes effect within one new hop; `size` is
    the grain length; the hop is fractional and carried across blocks.
  - Normalization: dense hann / triangle clouds sit at exactly 1.0 on
    DC; expo within 1%; sparse grains play at their natural level.
  - Windows: hann is 0.5 - 0.5cos (periodic), triangle peaks at 1 in the
    middle, expo peaks in its first tenth and decays to -60 dB.
  - Head start: at +24 st / 500 ms the reads stay causal (64 vs 512
    bit-exact); with a 0.5 s buffer the grain is shortened to fit.
  - Knob turns reach the NEXT grain: after a pitch change both rates are
    in flight; a buffer resize keeps the history (the delayed-input
    identity holds again once the new grains take over).
  - Block-size independence is bit-exact (64 vs 512) with fractional
    pitch / density / position and the expo window; voice inputs sum.
  - UI: the window combo offers GRANULAR_WINDOWS; every param gets its
    own widget (no fallthrough to a text box).
  - Example granular_cloud.json loads, compiles, renders, is audible.

Slice 2 (sprays, seed, stereo):
  - OFF at the defaults: no draws are made while every spray and width
    is zero (the pend cache stays None) and out_l / out_r ARE out.
  - Seeded and reproducible: same seed -> all three outs array_equal;
    a different seed differs; sprayed renders are block-size
    independent to the bit (64 vs 512, every spray + width on).
  - spray_time: intervals within [1 - s, 1 + s] x hop, mean ~ hop.
  - spray_pos: an impulse's copies land across position +- spray_pos
    of the buffer, and use the range.
  - spray_pitch: isolated grains' periods spread across +- the cents,
    each one an exact ratio.
  - width: constant-peak law (per isolated grain max(L, R) peak == out
    peak; L and R differ); `out` is bit-identical with width on or off
    (it hears every grain at unity); width 0 -> L is R is out even with
    the other sprays on; mix 0 -> all three are the dry.
  - Example granular_haze.json plays in stereo.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.io_patch import load_patch
from pysynthrack.modules.granular import GRANULAR_WINDOWS, Granular

SR = 48000
F = 512
HEAD = NumpyBackend._GR_HEAD


def _rig(params=None, block=F):
    patch = Patch()
    osc = patch.add_module("oscillator")
    gr = patch.add_module("granular", params=params or {})
    patch.connect(osc.id, "out", gr.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, osc, gr, b


KEYS = ("out", "out_l", "out_r")


def _run_all(params, x, block=F, change=None):
    """Render x (F,) or (V, F) through a fresh granular.

    ``change`` = (block_index, {param: value}) applies a param edit before
    that block. Returns ({out, out_l, out_r}, x trimmed, backend, module).
    """
    patch, osc, gr, b = _rig(params, block)
    n = x.shape[-1] // block
    outs = {k: [] for k in KEYS}
    for k in range(n):
        if change is not None and k == change[0]:
            for key, val in change[1].items():
                gr.params[key] = val
        sl = slice(k * block, (k + 1) * block)
        r = b._render_granular(gr, block, {(osc.id, "out"): x[..., sl]}, patch)
        for key in KEYS:
            outs[key].append(r[key])
    y = {k: np.concatenate(v) for k, v in outs.items()}
    return y, x[..., : y["out"].shape[-1]], b, gr


def _run(params, x, block=F, change=None):
    """The mono ``out`` of _run_all: (out, x trimmed, backend, module)."""
    y, x, b, gr = _run_all(params, x, block, change)
    return y["out"], x, b, gr


def _noise(secs, seed=0, amp=0.3):
    rng = np.random.RandomState(seed)
    return (amp * rng.randn(int(secs * SR))).astype(np.float32)


def _tone(freq, secs, amp=0.5):
    t = np.arange(int(secs * SR))
    return (amp * np.sin(2 * np.pi * freq * t / SR)).astype(np.float32)


def _runs(y, thresh=1e-9):
    """(start, end) of each contiguous |y| > thresh run."""
    on = np.abs(y) > thresh
    edges = np.diff(np.concatenate([[0], on.astype(np.int8), [0]]))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return list(zip(starts, ends))


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        gr = Patch().add_module("granular")
        assert isinstance(gr, Granular)
        assert gr.CATEGORY == "Effects"
        assert gr.params == {
            "buffer": 2.0, "density": 25.0, "spray_time": 0.0, "size": 80.0,
            "pitch": 0.0, "spray_pitch": 0.0, "position": 0.0, "spray_pos": 0.0,
            "window": "hann", "width": 0.0, "mix": 1.0, "seed": 1,
        }
        assert GRANULAR_WINDOWS == ("hann", "triangle", "expo")

    def test_ports_and_signal_kinds(self):
        gr = Patch().add_module("granular")
        assert [(p.name, p.signal_kind) for p in gr.input_ports] == [("in", "audio")]
        assert [(p.name, p.signal_kind) for p in gr.output_ports] == [
            ("out", "audio"), ("out_l", "audio"), ("out_r", "audio"),
        ]

    def test_json_round_trip_and_unknown_param(self):
        from pysynthrack.io_patch import patch_from_json, patch_to_json
        patch = Patch()
        patch.add_module("granular", params={"window": "expo", "pitch": 7.0, "density": 40.0})
        back = patch_from_json(patch_to_json(patch))
        gr = next(iter(back))
        assert gr.params["window"] == "expo"
        assert gr.params["pitch"] == pytest.approx(7.0)
        assert gr.params["density"] == pytest.approx(40.0)
        with pytest.raises(Exception):
            Patch().add_module("granular", params={"spray": 0.5})

    def test_type_walls(self):
        patch = Patch()
        gr = patch.add_module("granular")
        lfo = patch.add_module("lfo")
        spk = patch.add_module("speaker_output")
        patch.connect(gr.id, "out", spk.id, "in")             # audio -> audio
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", gr.id, "in")          # cv -> audio


# ----- Silence ---------------------------------------------------------------


class TestSilence:
    def test_unpatched_and_silent_inputs(self):
        patch = Patch()
        gr = patch.add_module("granular")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        r = b._render_granular(gr, F, {}, patch)
        for key in KEYS:
            assert r[key].shape == (F,) and r[key].dtype == np.float32 and not r[key].any()
        y, *_ = _run({"pitch": 12.0, "density": 100.0}, np.zeros(F * 40, np.float32))
        assert not y.any()

    def test_zero_frames(self):
        patch, osc, gr, b = _rig()
        r = b._render_granular(gr, 0, {(osc.id, "out"): np.zeros(0, np.float32)}, patch)
        assert all(r[key].shape == (0,) for key in KEYS)


# ----- The neutral -----------------------------------------------------------


class TestNeutral:
    def test_defaults_are_the_input_delayed_by_the_head_start_bit_exact(self):
        """hann at 50% overlap tiles to one: out[n] == in[n - HEAD]."""
        x = _noise(2.0)
        y, x, b, gr = _run({}, x)
        skip = SR // 4                          # past the first (fade-in) grain
        assert np.array_equal(y[skip:], x[skip - HEAD:-HEAD])

    def test_position_quarter_of_two_seconds_is_24000_samples(self):
        x = _noise(3.0, seed=1)
        y, x, b, gr = _run({"position": 0.25}, x)
        D = 24000
        assert np.array_equal(y[SR:], x[SR - D:-D])

    def test_fractional_position_lands_an_impulse_where_it_should(self):
        x = np.zeros(2 * SR, np.float32)
        x[SR // 2] = 1.0
        # 0.1 of 96000 = 9600.000000000002 -> a fractional read, so the
        # impulse is Hermite-spread over 4 taps; its peak sits at D.
        y, x, b, gr = _run({"position": 0.1}, x)
        peak = int(np.argmax(np.abs(y)))
        assert abs(peak - (SR // 2 + 9600)) <= 1
        assert abs(y).max() > 0.9               # the tiles still sum to ~1

    def test_mix_zero_is_the_head_start_delayed_dry_bit_exact(self):
        x = _noise(1.0, seed=2)
        y, x, b, gr = _run({"mix": 0.0, "pitch": 7.0, "density": 60.0}, x)
        assert np.array_equal(y[HEAD:], x[:-HEAD])

    def test_mix_half_is_the_average(self):
        x = _noise(1.0, seed=3)
        params = {"pitch": 5.0, "position": 0.2}
        wet, *_ = _run({**params, "mix": 1.0}, x)
        dry, *_ = _run({**params, "mix": 0.0}, x)
        half, *_ = _run({**params, "mix": 0.5}, x)
        assert np.allclose(half, 0.5 * dry + 0.5 * wet, atol=1e-6)
        assert not np.array_equal(wet, dry)


# ----- Pitch -----------------------------------------------------------------


def _isolated_grain_periods(y, L, hop):
    """Zero-crossing period (samples) over the central half of each grain.

    Grains sit on the scheduler's grid (onset ``m * hop``, ``hop`` an
    integer here), so the grid is used directly: a run detector would be
    fooled by the tone's own near-exact zeros (``sin(11*pi*k)`` ~ 1e-16).
    """
    out = []
    m = 1
    while (m + 1) * hop <= y.shape[0]:
        s = m * hop
        seg = y[s + L // 4: s + L - L // 4].astype(np.float64)
        zc = np.flatnonzero((seg[1:] >= 0) & (seg[:-1] < 0))
        if len(zc) >= 4:
            out.append((zc[-1] - zc[0]) / (len(zc) - 1))
        m += 1
    return out


class TestPitch:
    @pytest.mark.parametrize("st", [12.0, 7.0, -12.0, -5.0])
    def test_inside_a_grain_the_period_is_exact(self, st):
        """Isolated grains (hop > size) on a 220 Hz tone: the period over
        the grain's middle is SR / (220 * 2^(st/12)) to a tenth of a %."""
        L = 9600                                   # 200 ms
        x = _tone(220.0, 3.0)
        y, x, b, gr = _run({"pitch": st, "density": 4.0, "size": 200.0}, x)
        pers = _isolated_grain_periods(y, L, SR // 4)     # hop = 12000
        assert len(pers) >= 6
        want = SR / (220.0 * 2 ** (st / 12.0))
        for p in pers:
            assert abs(p / want - 1.0) < 1e-3, (st, p, want)

    def test_across_grains_the_peak_is_within_one_density_of_the_target(self):
        """A synchronous train chops the phase at every hop, so the energy
        sits on a sideband within +-density Hz of the transposed tone --
        the un-hidden granular sound (the pitch_shifter's WSOLA is what
        removes it). The test pins that the peak is THAT close, not on
        the source, and not at the neutral's 0-sideband."""
        x = _tone(440.0, 2.0)
        for st, density in ((12.0, 25.0), (7.0, 40.0)):
            y, x2, b, gr = _run({"pitch": st, "density": density}, x)
            seg = y[SR // 2:].astype(np.float64)
            spec = np.abs(np.fft.rfft(seg * np.hanning(seg.shape[0])))
            fr = np.fft.rfftfreq(seg.shape[0], 1.0 / SR)
            peak = fr[spec.argmax()]
            target = 440.0 * 2 ** (st / 12.0)
            assert abs(peak - target) <= density, (st, peak, target)
            assert abs(peak - 440.0) > 3 * density


# ----- Scheduler -------------------------------------------------------------


class TestScheduler:
    def test_density_is_grains_per_second(self):
        dc = np.ones(3 * SR, np.float32)
        for density, size in ((10.0, 20.0), (4.0, 100.0), (2.5, 50.0)):
            y, *_ = _run({"density": density, "size": size}, dc)
            runs = [(s, e) for s, e in _runs(y) if s >= SR and e <= 3 * SR - F]
            per_s = len(runs) / ((3 * SR - F - SR) / SR)
            assert abs(per_s - density) <= 1.0, (density, per_s)

    def test_size_is_the_grain_length(self):
        dc = np.ones(2 * SR, np.float32)
        for size in (20.0, 100.0, 333.0):
            y, *_ = _run({"density": 2.0, "size": size}, dc)
            L = int(round(size * 1e-3 * SR))
            L += L & 1
            lens = [e - s for s, e in _runs(y) if s >= SR // 2]
            assert lens and all(abs(n - (L - 1)) <= 1 for n in lens), (size, lens)

    def test_fractional_hop_is_carried_exactly_across_blocks(self):
        """density 17 -> hop 2823.529... samples; onsets stay on the exact
        accumulated grid whether rendered in 64s or 512s."""
        dc = np.ones(2 * SR, np.float32)
        a, *_ = _run({"density": 17.0, "size": 10.0}, dc, block=512)
        c, *_ = _run({"density": 17.0, "size": 10.0}, dc, block=64)
        n = min(a.shape[0], c.shape[0])
        assert np.array_equal(a[:n], c[:n])
        starts = np.array([s for s, e in _runs(a)])
        hops = np.diff(starts[1:])           # grain 0 starts a sample late (reads zeros)
        assert len(hops) > 20
        assert set(hops.tolist()) <= {2823, 2824}

    def test_a_density_rise_takes_effect_within_one_new_hop(self):
        dc = np.ones(3 * SR, np.float32)
        k0 = (3 * SR // 2) // F                  # change at ~1.5 s
        y, *_ = _run({"density": 2.0, "size": 10.0}, dc, change=(k0, {"density": 50.0}))
        t0 = k0 * F
        first = min(s for s, e in _runs(y) if s >= t0)
        assert first - t0 <= SR / 50.0 + 1
        after = [s for s, e in _runs(y) if t0 <= s < t0 + SR]
        assert abs(len(after) - 50) <= 2


# ----- Normalization and windows ---------------------------------------------


class TestLevel:
    def test_dense_cola_windows_sit_at_exactly_one(self):
        dc = np.ones(3 * SR, np.float32)
        for params in ({"density": 100.0, "size": 500.0},
                       {"density": 40.0, "size": 100.0, "window": "triangle"},
                       {}):
            y, *_ = _run(params, dc)
            assert np.array_equal(y[SR:], np.ones_like(y[SR:])), params

    def test_dense_expo_sits_near_one(self):
        dc = np.ones(3 * SR, np.float32)
        y, *_ = _run({"density": 60.0, "size": 200.0, "window": "expo"}, dc)
        assert abs(float(y[SR:].mean()) - 1.0) < 0.01
        assert y[SR:].max() < 1.2

    def test_sparse_grains_play_at_their_natural_level(self):
        dc = np.ones(2 * SR, np.float32)
        y, *_ = _run({"density": 2.0, "size": 50.0}, dc)
        assert y[SR // 2:].max() == pytest.approx(1.0, abs=1e-6)


class TestWindows:
    def _grain(self, window, size_ms=100.0):
        dc = np.ones(2 * SR, np.float32)
        y, *_ = _run({"density": 2.0, "size": size_ms, "window": window}, dc)
        L = int(round(size_ms * 1e-3 * SR))
        s, e = [r for r in _runs(y) if r[0] >= SR // 2][0]
        # w[0] == 0 for hann/triangle/expo, so the run starts one late.
        return y[s - 1: s - 1 + L].astype(np.float64), L

    def test_hann_is_periodic_hann(self):
        w, L = self._grain("hann")
        k = np.arange(L)
        assert np.allclose(w, 0.5 - 0.5 * np.cos(2 * np.pi * k / L), atol=1e-6)

    def test_triangle_peaks_at_one_in_the_middle(self):
        w, L = self._grain("triangle")
        assert w[L // 2] == pytest.approx(1.0, abs=1e-6)
        assert np.allclose(w[: L // 2], 2.0 * np.arange(L // 2) / L, atol=1e-6)

    def test_expo_is_a_percussive_grain(self):
        w, L = self._grain("expo")
        assert int(np.argmax(w)) <= L // 10
        assert w.max() == pytest.approx(1.0, abs=1e-6)
        assert w[-1] < 2e-3 and w[-1] > 0.0
        assert np.all(np.diff(w[L // 10 + 1:]) < 0)       # monotone decay


# ----- Head start ------------------------------------------------------------


class TestHeadStart:
    def test_fast_reads_stay_causal_at_the_extreme(self):
        """+24 st, 500 ms grains: each grain needs 1.5 s of head start. If
        the reads ever crossed the write head the 64- and 512-block
        renders would diverge (they would read different unwritten
        data)."""
        x = _noise(4.0, seed=5)
        a, *_ = _run({"pitch": 24.0, "size": 500.0, "density": 8.0}, x, block=512)
        c, *_ = _run({"pitch": 24.0, "size": 500.0, "density": 8.0}, x, block=64)
        n = min(a.shape[0], c.shape[0])
        assert np.array_equal(a[:n], c[:n])
        assert np.abs(a[2 * SR:]).max() > 0.05

    def test_position_is_floored_at_the_head_start(self):
        """At +12, an 80 ms grain needs 2 + 3839 samples; position 0 reads
        from THERE, not from now: an impulse comes out ~3841 samples late
        (played at 2x, so its copy sits at onset + (D - ...) -- we just
        pin that nothing arrives before the floor)."""
        x = np.zeros(2 * SR, np.float32)
        x[SR // 2: SR // 2 + 2] = 1.0        # two wide: a 2x read has stride 2
        y, *_ = _run({"pitch": 12.0}, x)
        nz = np.flatnonzero(np.abs(y) > 1e-6)
        assert nz.size > 0
        assert int(nz[0]) >= SR // 2 + 2

    def test_short_buffer_shortens_the_grain(self):
        dc = np.ones(3 * SR, np.float32)
        y, x, b, gr = _run({"pitch": 24.0, "size": 500.0, "density": 1.0, "buffer": 0.5}, dc)
        lens = [e - s for s, e in _runs(y) if s >= SR]
        assert lens
        assert all(n < 0.4 * SR for n in lens), lens          # shorter than 500 ms
        assert all(n > 0.1 * SR for n in lens), lens          # but still a grain
        assert np.all(np.isfinite(y))


# ----- Knob turns ------------------------------------------------------------


class TestKnobs:
    def test_a_pitch_change_reaches_the_next_grain_only(self):
        x = _tone(300.0, 2.0)
        k0 = x.shape[0] // F - 12            # 12 blocks (~0.13 s) before the end
        y, x, b, gr = _run({"size": 400.0, "density": 10.0}, x, change=(k0, {"pitch": 12.0}))
        # 400 ms grains fired before the change are still in flight, and
        # at 10/s at least one has fired since: both rates present.
        rates = set(np.round(b._state[gr.id]["rate"], 6).tolist())
        assert len(rates) == 2 and 1.0 in rates
        assert np.all(np.isfinite(y))

    def test_a_buffer_resize_keeps_the_history(self):
        """position 0.25: 2 s buffer -> 24000 back; 4 s -> 48000 back.
        After the resize the new grains read history captured BEFORE it,
        so the delayed-input identity holds again once they take over."""
        x = _noise(4.0, seed=6)
        k0 = (2 * SR) // F
        y, x, b, gr = _run({"position": 0.25}, x, change=(k0, {"buffer": 4.0}))
        t0 = k0 * F
        D = 48000
        seg = slice(t0 + SR // 4, y.shape[0])
        assert np.array_equal(y[seg], x[seg.start - D: seg.stop - D])
        # and before the change it was the 24000-sample delay
        pre = slice(SR, t0)
        assert np.array_equal(y[pre], x[pre.start - 24000: pre.stop - 24000])


# ----- Shape and blocks ------------------------------------------------------


class TestShape:
    def test_block_size_independent_bit_exact(self):
        x = _noise(2.0, seed=7)
        params = {"pitch": 5.3, "position": 0.31, "density": 17.0, "size": 90.0, "window": "expo"}
        a, *_ = _run(params, x, block=512)
        c, *_ = _run(params, x, block=64)
        n = min(a.shape[0], c.shape[0])
        assert np.array_equal(a[:n], c[:n])
        assert np.abs(a[SR // 2:]).max() > 0.05

    def test_voice_input_is_summed(self):
        x = _noise(0.5, seed=8)
        v = np.stack([x * 0.5, x * 0.5, np.zeros_like(x)])
        a, *_ = _run({"pitch": 3.0}, x)
        c, *_ = _run({"pitch": 3.0}, v)
        assert np.allclose(a, c, atol=1e-6)

    def test_output_is_float32_and_finite(self):
        y, *_ = _run({"pitch": -24.0, "density": 100.0, "size": 500.0, "window": "expo"}, _noise(1.0, seed=9))
        assert y.dtype == np.float32 and np.all(np.isfinite(y))


# ----- Slice 2: sprays, seed, stereo ------------------------------------------


SPRAYED = {
    "pitch": 5.0, "density": 30.0, "size": 120.0, "position": 0.3,
    "spray_time": 0.7, "spray_pos": 0.2, "spray_pitch": 300.0, "width": 1.0,
    "seed": 5,
}


class TestSprayOff:
    def test_no_draws_and_stereo_is_mono_at_the_defaults(self):
        x = _noise(1.0, seed=20)
        y, x, b, gr = _run_all({"pitch": 7.3, "density": 17.0, "window": "expo"}, x)
        assert b._state[gr.id]["pend"] is None            # never rolled a die
        assert np.array_equal(y["out_l"], y["out"])
        assert np.array_equal(y["out_r"], y["out"])
        assert np.array_equal(b._state[gr.id]["pan"], np.zeros_like(b._state[gr.id]["pan"]))


class TestSeed:
    def test_same_seed_same_cloud_different_seed_different(self):
        x = _noise(1.5, seed=21)
        a, *_ = _run_all(SPRAYED, x)
        c, *_ = _run_all(SPRAYED, x)
        d, *_ = _run_all({**SPRAYED, "seed": 6}, x)
        for key in KEYS:
            assert np.array_equal(a[key], c[key])
            assert not np.array_equal(a[key], d[key])

    def test_sprayed_render_is_block_size_independent_bit_exact(self):
        x = _noise(2.0, seed=22)
        a, *_ = _run_all(SPRAYED, x, block=512)
        c, *_ = _run_all(SPRAYED, x, block=64)
        for key in KEYS:
            n = min(a[key].shape[0], c[key].shape[0])
            assert np.array_equal(a[key][:n], c[key][:n]), key
        assert np.abs(a["out"][SR // 2:]).max() > 0.05

    def test_a_seed_change_reaches_the_next_grain_only(self):
        x = _noise(1.0, seed=23)
        k0 = x.shape[0] // F - 12
        y, x, b, gr = _run_all({**SPRAYED, "size": 400.0, "density": 10.0}, x,
                               change=(k0, {"seed": 99}))
        assert np.all(np.isfinite(y["out"]))


class TestSprayTime:
    def test_intervals_are_the_hop_scaled_within_one_plus_minus_spray(self):
        dc = np.ones(10 * SR, np.float32)
        hop = SR / 20.0
        y, *_ = _run({"density": 20.0, "size": 10.0, "spray_time": 0.5}, dc)
        starts = np.array([s for s, e in _runs(y) if s > SR])
        iv = np.diff(starts).astype(np.float64)
        assert len(iv) > 150
        assert iv.min() >= 0.5 * hop - 2 and iv.max() <= 1.5 * hop + 2
        assert iv.max() - iv.min() > 0.6 * hop           # it actually scatters
        assert abs(iv.mean() - hop) < 0.1 * hop          # density is preserved

    def test_full_spray_puts_some_grains_closer_than_a_grain(self):
        """A run detector can't see an interval shorter than the grain
        (overlapping DC grains merge into one run), so count grains
        FIRED against runs SEEN: at spray 1 some merged; at spray 0
        none did."""
        dc = np.ones(10 * SR, np.float32)
        y, x, b, gr = _run({"density": 20.0, "size": 10.0, "spray_time": 1.0}, dc)
        fired = b._state[gr.id]["gi"]
        assert len(_runs(y)) < 0.9 * fired
        y0, x, b0, gr0 = _run({"density": 20.0, "size": 10.0}, dc)
        assert len(_runs(y0)) >= b0._state[gr0.id]["gi"] - 2


class TestSprayPos:
    def test_impulse_copies_scatter_across_position_plus_minus_spray(self):
        """Long grains (500 ms) so about half of the grains in range
        catch the impulse; each copy lands at imp + its own D, D drawn
        from position +- spray_pos of the buffer: [24000, 72000]."""
        x = np.zeros(4 * SR, np.float32)
        imp = SR // 2
        x[imp: imp + 2] = 1.0
        y, *_ = _run({"position": 0.5, "spray_pos": 0.25, "density": 40.0, "size": 500.0}, x)
        nz = np.flatnonzero(np.abs(y) > 1e-4)
        assert nz.size > 20
        lo, hi = imp + 24000, imp + 72000 + 1
        assert nz.min() >= lo - 3 and nz.max() <= hi + 3
        assert nz.max() - nz.min() > 0.35 * 96000        # uses most of the range
        # and with no spray every copy lands at exactly position * buffer
        y0, *_ = _run({"position": 0.5, "density": 40.0, "size": 500.0}, x)
        nz0 = np.flatnonzero(np.abs(y0) > 1e-4)
        assert nz0.size > 0
        assert nz0.min() >= imp + 48000 and nz0.max() <= imp + 48000 + 1


class TestSprayPitch:
    def test_isolated_grains_spread_across_the_cents(self):
        """density 4 / size 200 ms on 220 Hz, spray +-700 ct: each grain's
        period is SOME exact ratio; over 30 grains they span the range."""
        L = 9600
        x = _tone(220.0, 8.0)
        y, *_ = _run({"density": 4.0, "size": 200.0, "spray_pitch": 700.0, "seed": 2}, x)
        pers = np.array(_isolated_grain_periods(y, L, SR // 4))
        assert len(pers) >= 25
        ratios = (SR / 220.0) / pers                  # playback rate per grain
        lo, hi = 2 ** (-7 / 12), 2 ** (7 / 12)
        assert ratios.min() >= lo * 0.995 and ratios.max() <= hi * 1.005
        assert ratios.min() < 0.8 and ratios.max() > 1.25
        assert len(set(np.round(ratios, 3))) > 20      # not a few repeated values


class TestStereo:
    def test_constant_peak_pan_law_on_isolated_grains(self):
        dc = np.ones(4 * SR, np.float32)
        y, *_ = _run_all({"density": 2.0, "size": 50.0, "width": 1.0}, dc)
        peaks = []
        for s, e in _runs(y["out"]):
            if s < SR:
                continue
            pl, pr, po = y["out_l"][s:e].max(), y["out_r"][s:e].max(), y["out"][s:e].max()
            assert max(pl, pr) == pytest.approx(po, abs=1e-6)     # the loud side is unity
            assert min(pl, pr) <= po + 1e-6
            peaks.append((pl, pr))
        assert len(peaks) >= 5
        assert any(abs(a - b) > 0.3 for a, b in peaks)          # some grains are off-centre
        assert not np.array_equal(y["out_l"], y["out_r"])

    def test_out_hears_every_grain_at_unity_whatever_the_width(self):
        x = _noise(1.5, seed=24)
        a, *_ = _run_all({**SPRAYED, "width": 0.0}, x)
        c, *_ = _run_all({**SPRAYED, "width": 1.0}, x)
        assert np.array_equal(a["out"], c["out"])
        assert np.array_equal(a["out_l"], a["out"]) and np.array_equal(a["out_r"], a["out"])
        assert not np.array_equal(c["out_l"], c["out"])

    def test_mix_zero_is_the_dry_on_all_three(self):
        x = _noise(1.0, seed=25)
        y, x, b, gr = _run_all({**SPRAYED, "mix": 0.0}, x)
        for key in KEYS:
            assert np.array_equal(y[key][HEAD:], x[:-HEAD])

    def test_mix_half_blends_each_channel_with_the_centred_dry(self):
        x = _noise(1.0, seed=26)
        wet, *_ = _run_all(SPRAYED, x)
        dry, *_ = _run_all({**SPRAYED, "mix": 0.0}, x)
        half, *_ = _run_all({**SPRAYED, "mix": 0.5}, x)
        for key in KEYS:
            assert np.allclose(half[key], 0.5 * dry[key] + 0.5 * wet[key], atol=1e-6)


# ----- UI --------------------------------------------------------------------


class TestUI:
    def _widgets(self, monkeypatch):
        pytest.importorskip("dearpygui.dearpygui")
        import pysynthrack.ui.app as app_mod
        monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
        monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
        app = app_mod.App()
        app.patch = Patch()
        module = app.patch.add_module("granular")
        kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
                 "add_input_float", "add_checkbox", "add_drag_int")
        before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
        app._create_node_for_module(module)
        out = {}
        for k in kinds:
            for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
                out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("items"))
        return out

    def test_window_combo_offers_the_module_s_windows(self, monkeypatch):
        w = self._widgets(monkeypatch)
        assert w["window"] == ("add_combo", list(GRANULAR_WINDOWS))

    def test_every_param_gets_its_own_widget(self, monkeypatch):
        w = self._widgets(monkeypatch)
        labels = list(w)
        for name in Granular.DEFAULT_PARAMS:
            hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
            assert hits, (name, labels)
            assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])


# ----- Example ---------------------------------------------------------------


class TestExample:
    def test_granular_cloud_example_plays(self):
        path = Path(__file__).resolve().parent.parent / "examples" / "granular_cloud.json"
        patch = load_patch(path)
        gr = next(m for m in patch if m.TYPE == "granular")
        assert gr.params["pitch"] == 12.0
        b = NumpyBackend(sample_rate=44100, block_size=512)
        b.compile(patch)
        peak = 0.0
        for k in range(int(6 * 44100 / 512)):
            out, _ = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            peak = max(peak, float(np.abs(out).max()))
        assert np.asarray(out).shape[1] == 2
        assert peak > 0.05
        assert b._state[gr.id]["onset"].shape[0] >= 1          # grains in flight

    def test_granular_haze_example_plays_in_stereo(self):
        path = Path(__file__).resolve().parent.parent / "examples" / "granular_haze.json"
        patch = load_patch(path)
        gr = next(m for m in patch if m.TYPE == "granular")
        assert gr.params["spray_time"] == 1.0 and gr.params["width"] == 1.0
        b = NumpyBackend(sample_rate=44100, block_size=512)
        b.compile(patch)
        outs = []
        for k in range(int(6 * 44100 / 512)):
            out, _ = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
            outs.append(np.asarray(out))
        o = np.concatenate(outs)
        assert o.shape[1] == 2
        assert np.abs(o).max() > 0.05
        assert not np.array_equal(o[:, 0], o[:, 1])
