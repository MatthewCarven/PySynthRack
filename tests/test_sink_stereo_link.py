"""The governed buffered sink keeps its two sides together.

Matthew, 2026-11-11: under load, left and right of one buffered speaker
drifted apart and usually stayed apart. The cause (docs/channel-timing-scope.md):
the ring governor stretched its push through two independent WSOLA engines,
one per side, and each chose its splice from its own side's waveform. A hard
swing of the ratio (which is what a starved ring asks for) made the sides
splice differently, up to 25 ms either way, and at rest each side kept the
offset it had landed on.

The fix (docs/channel-timing-plan.md) is ``_GrainShifterPair``: two engines,
ONE search. These tests pin the four things that makes true:

  * the two sides come out together, whatever the ratio does -- and the same
    material through two lone engines really does come apart, so the test
    is known to be able to see the defect;
  * a source that is the same on both sides, or silent on one, sounds as it
    did through a lone engine;
  * sides that are out of phase with each other still find the splice;
  * pitch is still held on both sides.

No audio hardware is touched: engines are driven directly, and the sink runs
through ``render_block_multi`` on a backend whose stream never opens.
"""
from __future__ import annotations

import inspect
import re

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import (
    NumpyBackend,
    _GrainShifter,
    _GrainShifterPair,
)
from pysynthrack.core.patch import Patch

# Importing the module files registers their types with the Patch factory.
from pysynthrack.modules import combiner as _combiner  # noqa: F401
from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules import noise as _noise  # noqa: F401
from pysynthrack.modules import output as _output  # noqa: F401

SR = 44100
BLOCK = 512
GRAIN = int(round(0.05 * SR))     # the sink's 50 ms grain
HEAD = 16384
SINK = "buffered_specific_speaker_output"

# Each side may sit up to half a hop (551 samples) off the ideal grid, so
# two independent sides can be up to 1102 samples (25 ms) apart.
MAX_LAG = 1200


def _swing(i: float) -> float:
    """The governor's ratio by (512-sample) block index: restless around 1,
    one hard swing up to 1.19 and back (a starved ring refilling), and a
    smaller dip below 1 (the overshoot draining)."""
    return (1.0 + 0.004 * np.sin(i * 0.37)
            + 0.19 * np.exp(-(((i - 150) / 18.0) ** 2))
            - 0.07 * np.exp(-(((i - 430) / 14.0) ** 2)))


def _stereo_material(seconds: float, seed: int = 20261111):
    """A stereo pair with something to line up on and something to disagree
    about: one noise common to both sides, plus a different noise on each."""
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    common = 0.30 * rng.standard_normal(n)
    return common + 0.15 * rng.standard_normal(n), common + 0.15 * rng.standard_normal(n)


def _side_lags(left, right, start: int = SR, win: int = 4096, hop: int = 2048):
    """Per window: how many samples the right side trails the left (the peak
    of their cross-correlation), and how strong that peak is (0..1)."""
    left = np.asarray(left, dtype=np.float64)
    right = np.asarray(right, dtype=np.float64)
    lags, peaks = [], []
    for s in range(max(start, MAX_LAG), len(left) - win - MAX_LAG, hop):
        ref = left[s:s + win]
        c = np.correlate(right[s - MAX_LAG:s + win + MAX_LAG], ref, "valid")
        i = int(np.argmax(c))
        seg = right[s - MAX_LAG + i:s - MAX_LAG + i + win]
        lags.append(i - MAX_LAG)
        peaks.append(float(c[i] / (np.linalg.norm(ref) * np.linalg.norm(seg) + 1e-12)))
    return np.array(lags), np.array(peaks)


def _run_pair(xl, xr, ratio=_swing):
    pair = _GrainShifterPair(GRAIN, 2, HEAD)
    outs_l, outs_r = [], []
    for i in range(len(xl) // BLOCK):
        s = slice(i * BLOCK, (i + 1) * BLOCK)
        yl, yr = pair.process(xl[s], xr[s], ratio(i))
        outs_l.append(yl)
        outs_r.append(yr)
    return np.concatenate(outs_l), np.concatenate(outs_r)


def _run_lone(x, ratio=_swing):
    eng = _GrainShifter(GRAIN, 2, HEAD)
    return np.concatenate(
        [eng.process(x[i * BLOCK:(i + 1) * BLOCK], ratio(i)) for i in range(len(x) // BLOCK)]
    )


# ----- the two sides stay together -------------------------------------------


class TestTheSidesStayTogether:
    def test_two_lone_engines_come_apart(self):
        # Exposure first: this material and this ratio path must be able to
        # SHOW the defect, or the next test proves nothing. Two engines that
        # each search their own side is what the sink did until 2026-11-11.
        xl, xr = _stereo_material(8.0)
        lags, _peaks = _side_lags(_run_lone(xl), _run_lone(xr))
        assert np.max(np.abs(lags)) > SR // 1000, (
            "two independent engines never parted by even 1 ms: the material "
            "or the ratio path no longer exercises the defect"
        )
        # And it sticks: well after the last swing the sides are still apart
        # in most windows.
        late = lags[-20:]
        assert np.mean(late != 0) > 0.5

    def test_the_pair_never_parts(self):
        xl, xr = _stereo_material(8.0)
        yl, yr = _run_pair(xl, xr)
        lags, peaks = _side_lags(yl, yr)
        assert len(lags) > 100
        assert np.all(lags == 0), (
            f"left and right parted by up to {np.max(np.abs(lags))} samples"
        )
        # The peak at zero is the common part of the material, not an
        # accident of a flat correlation.
        assert peaks.min() > 0.5

    def test_the_pair_stretches_like_the_lone_engine_did(self):
        # Linking must not turn the stretch off: on the swing the pair's
        # output differs from its output at a steady ratio of 1.
        xl, xr = _stereo_material(4.0)
        yl, _ = _run_pair(xl, xr)
        flat, _ = _run_pair(xl, xr, ratio=lambda i: 1.0)
        assert not np.array_equal(yl, flat)

    @pytest.mark.parametrize("block", [64, 512, 1000])
    def test_the_governed_sink_never_parts(self, block):
        # The same claim through the real sink: three noises make a stereo
        # pair with a shared part, a constant on ratio_cv is swung by hand,
        # and what the sink would push to its device is measured. At three
        # main buffer sizes: the governor smooths its ratio once per block,
        # so each size walks the engines along a different path.
        patch = Patch()
        sink = patch.add_module(SINK, params={"device": "LinkDev"})
        common = patch.add_module("noise", params={"seed": 101, "amp": 0.30})
        only_l = patch.add_module("noise", params={"seed": 202, "amp": 0.15})
        only_r = patch.add_module("noise", params={"seed": 303, "amp": 0.15})
        mix_l = patch.add_module("combiner")
        mix_r = patch.add_module("combiner")
        const = patch.add_module("constant", params={"value": 0.0})
        patch.connect(common.id, "out", mix_l.id, "in1")
        patch.connect(only_l.id, "out", mix_l.id, "in2")
        patch.connect(common.id, "out", mix_r.id, "in1")
        patch.connect(only_r.id, "out", mix_r.id, "in2")
        patch.connect(mix_l.id, "out", sink.id, "in_l")
        patch.connect(mix_r.id, "out", sink.id, "in_r")
        patch.connect(const.id, "out", sink.id, "ratio_cv")
        b = NumpyBackend(sample_rate=SR, block_size=block)
        b.compile(patch)
        pushed = []
        ratios = []
        for i in range(int(8.0 * SR / block)):
            # ratio = 1 + cv * ratio_depth (0.25); the sink smooths it.
            # _swing counts in 512-sample blocks: keep its shape in time.
            const.params["value"] = (_swing(i * block / BLOCK) - 1.0) / 0.25
            _out, blocks = b.render_block_multi(block)
            (blk,) = blocks.values()
            pushed.append(blk.copy())
            ratios.append(b._sink_ratio[sink.id])
        y = np.concatenate(pushed)
        # The swing really reached the engines, and really changed the push.
        assert max(ratios) > 1.15 and min(ratios) < 0.96
        assert {p.shape[0] for p in pushed} != {block}
        lags, peaks = _side_lags(y[:, 0], y[:, 1])
        assert len(lags) > 100
        assert np.all(lags == 0), (
            f"the sink's two sides parted by up to {np.max(np.abs(lags))} samples"
        )
        assert peaks.min() > 0.5


# ----- nothing else changed ---------------------------------------------------


class TestWhatDidNotChange:
    # "Same as a lone engine" is checked to a hair, not to the bit. The joint
    # score and the lone score are the same number written two ways, and
    # their last bits can differ; when that nudges a grain it moves it by
    # one float step of its position, about 1e-11 of a sample.
    TOL = 1e-9

    @pytest.mark.parametrize(
        "gain_l, gain_r",
        [
            (1.0, 1.0),                                    # the same wire twice
            (np.cos(np.pi / 4), np.sin(np.pi / 4)),      # in_l alone, panned centre
            (1.0, 0.25),                                   # panned well left
        ],
    )
    def test_same_signal_on_both_sides(self, gain_l, gain_r):
        rng = np.random.default_rng(5)
        x = 0.3 * rng.standard_normal(int(6.0 * SR))
        yl, yr = _run_pair(x * gain_l, x * gain_r)
        assert np.max(np.abs(yl - _run_lone(x * gain_l))) < self.TOL
        assert np.max(np.abs(yr - _run_lone(x * gain_r))) < self.TOL
        assert float(np.max(np.abs(yl))) > 0.05            # primed, not silent

    def test_a_silent_side_follows_the_live_side(self):
        rng = np.random.default_rng(6)
        x = 0.3 * rng.standard_normal(int(6.0 * SR))
        yl, yr = _run_pair(x, np.zeros_like(x))
        assert np.max(np.abs(yl - _run_lone(x))) < self.TOL
        assert not yr.any()

    def test_both_sides_silent_stays_silent(self):
        z = np.zeros(int(2.0 * SR))
        yl, yr = _run_pair(z, z)
        assert not yl.any() and not yr.any()
        assert np.all(np.isfinite(yl)) and np.all(np.isfinite(yr))

    def test_out_of_phase_sides_still_find_the_splice(self):
        # Right is left upside down. Each side's correlation is taken
        # against its own target and then added, so the two agree; a search
        # of left + right would be searching silence. The arithmetic is the
        # same on both sides up to sign, so this one IS exact.
        rng = np.random.default_rng(7)
        x = 0.3 * rng.standard_normal(int(6.0 * SR))
        yl, yr = _run_pair(x, -x)
        assert np.array_equal(yr, -yl)
        assert np.max(np.abs(yl - _run_lone(x))) < self.TOL

    def test_pitch_is_held_on_both_sides(self):
        n = int(4.0 * SR)
        t = np.arange(n) / SR
        yl, yr = _run_pair(
            0.4 * np.sin(2 * np.pi * 1000.0 * t),
            0.4 * np.sin(2 * np.pi * 1500.0 * t),
            ratio=lambda i: 1.25,
        )
        # The engine shifts pitch UP by the ratio; the sink's length
        # resample brings it back down. Undo it the same way here.
        for y, want in ((yl, 1000.0), (yr, 1500.0)):
            m = int(round(len(y) * 1.25))
            back = np.interp(np.arange(m) * (len(y) / m), np.arange(len(y)), y)
            seg = back[SR:SR + 65536] * np.hanning(65536)
            peak = np.fft.rfftfreq(65536, 1.0 / SR)[int(np.argmax(np.abs(np.fft.rfft(seg))))]
            assert abs(peak - want) < 3.0, f"{want:.0f} Hz came out at {peak:.1f} Hz"


# ----- the tripwire -----------------------------------------------------------

_BARE_ENGINE = re.compile(r"(?<![A-Za-z0-9_])_GrainShifter\(")


def test_the_tripwire_can_see_a_bare_engine():
    # Self-test: the pattern must catch what the sink used to do, and must
    # not mistake the pair for it.
    old = "engines = [_GrainShifter(grain, 2, head), _GrainShifter(grain, 2, head)]"
    assert len(_BARE_ENGINE.findall(old)) == 2
    assert not _BARE_ENGINE.search("pair = _GrainShifterPair(grain, 2, head)")


def test_the_sink_builds_a_pair_and_never_a_bare_engine():
    """Count the doors in the source: two independent engines on one stereo
    signal is the defect, and it would come back silently -- every governor
    test but the ones above feeds the sink from ``in_l`` alone."""
    src = inspect.getsource(NumpyBackend.render_block_multi)
    assert "_GrainShifterPair(" in src, "the governed push no longer builds a pair"
    assert not _BARE_ENGINE.search(src), (
        "render_block_multi builds a bare _GrainShifter: the sink's two sides "
        "must share one search (use _GrainShifterPair)"
    )
