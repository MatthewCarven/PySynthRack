"""Every LINEAR block-mean CV read is float64 and scrubbed before a clamp.

The octave paths got their door on 2026-09-20 (``_pow2_clipped``, see
``test_cv_overflow.py``). The linear ones -- the reads that feed a
``min``/``max`` clamp, an ``int()`` or a shape decision -- did not, and
they carry two warts:

* **float32 accumulation.** The buffers are float32 and ``np.mean`` of a
  float32 array accumulates in float32, so a CONSTANT CV of 0.3 read
  0.29999998 over 64 samples and 0.30000001 over 512. A block-mean's
  one job is to be a block-mean; a held knob-offset was not block-size
  exact, and fifteen modules rendered differently at 64 than at 512
  with nothing but a steady voltage on a jack.
* **the ``min``/``max`` NaN wart.** Python's ``min``/``max`` do NOT
  propagate NaN -- every comparison against NaN is False -- so
  ``max(0.0, nan)`` hands back ``0.0`` and a "clamp" silently reads a
  NaN as a rail, while ``min(max(nan, lo), hi)`` propagates it instead:
  the SAME idiom, two different wrong answers, decided by argument
  order. ``np.clip`` passes NaN straight through, and
  ``int(round(nan))`` raises ``ValueError`` out of the audio thread --
  which is how ``bitcrusher.bits_cv`` took a render down.

Both now live behind one door, ``NumpyBackend._finite_mean``. This file
pins the helper, the property it buys (a constant CV is block-size
exact), the scrub (NaN / +-1e6 render finite and raise nothing), and
counts the doors in the routed renderers' own source.

The renderers are driven with the ``test_cv_overflow.py`` fake patch --
a patch whose ``cables_into`` returns cable objects pointing at buffer
keys -- so each case is one module and no topo walk.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest
from scipy.io import wavfile

import pysynthrack.modules  # noqa: F401  (registers every type)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch

SR = 44100
TOTAL = 4096          # samples per case: 64 blocks of 64, or 8 of 512
SMALL, LARGE = 64, 512
SRC = 99              # the fake source module every feed cable points at
V = 4                 # voice count for the (V, F) cases
CONST_CV = 0.3        # non-dyadic on purpose: float32 cannot hold it
ABSURD = [float("nan"), 1e6, -1e6]
ABSURD_IDS = ["nan", "1e6", "-1e6"]


# ----- the fake patch ---------------------------------------------------------

class _Cable:
    def __init__(self, src_port, dst_port):
        self.src_module_id = SRC
        self.src_port = src_port
        self.dst_port = dst_port


class _FakePatch:
    def __init__(self, cables):
        self._cables = cables

    def cables_into(self, module_id):
        return self._cables


def _render(mtype, params, feeds, block=LARGE, seed=0):
    """Drive ``_render_<mtype>`` for ``TOTAL`` samples at ``block``.

    Returns ``{out_name: array}`` with the blocks concatenated along the
    time axis, so two block sizes are directly comparable."""
    p = Patch()
    m = p.add_module(mtype)
    for k, v in params.items():
        m.params[k] = v
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    if mtype == "sampler":
        b.wait_for_sample_loads()
    fp = _FakePatch([_Cable(port, port) for port in feeds])
    fn = getattr(b, f"_render_{mtype}")
    np.random.seed(seed)      # unseeded modules draw from the global rng
    acc: dict[str, list] = {}
    for k in range(TOTAL // block):
        sl = (Ellipsis, slice(k * block, (k + 1) * block))
        bufs = {
            (SRC, port): np.ascontiguousarray(sig[sl], dtype=np.float32)
            for port, sig in feeds.items()
        }
        res = fn(m, block, bufs, fp)
        if not isinstance(res, dict):
            res = {"out": res}
        for name, arr in res.items():
            acc.setdefault(name, []).append(np.asarray(arr).copy())
    return {k: np.concatenate(v, axis=-1) for k, v in acc.items()}


# ----- signals ----------------------------------------------------------------

def _sine(freq=220.0, amp=0.3, voices=None):
    t = np.arange(TOTAL, dtype=np.float64) / SR
    if voices is None:
        return amp * np.sin(2.0 * np.pi * freq * t)
    return np.stack([amp * np.sin(2.0 * np.pi * freq * (1 + 0.1 * v) * t)
                     for v in range(voices)])


def _noise(amp=0.3, voices=None):
    rng = np.random.default_rng(11)
    shape = (TOTAL,) if voices is None else (voices, TOTAL)
    return amp * rng.standard_normal(shape)


def _gate(voices=None):
    """High from sample 0, so the rising edge lands on the first sample
    of the first block at EVERY block size (an edge inside a block would
    make the block-size comparison about edge placement, not the mean)."""
    g = np.ones(TOTAL)
    return g if voices is None else np.stack([g] * voices)


def _impulse(voices=None):
    x = np.zeros(TOTAL)
    x[10] = 0.8
    return x if voices is None else np.stack([x] * voices)


def _const(value, voices=None):
    shape = (TOTAL,) if voices is None else (voices, TOTAL)
    return np.full(shape, value, dtype=np.float64)


# ----- the routed modules -----------------------------------------------------

class _Case:
    def __init__(self, name, mtype, cv_ports, carrier, params=None,
                 voices=None, block_exact=True, repaired=False,
                 const=CONST_CV):
        self.name = name
        self.mtype = mtype
        self.cv_ports = cv_ports
        self.carrier = carrier
        self.params = params or {}
        self.voices = voices
        # The "sane" level this case's port is fed. 0.3 everywhere
        # except a port read as a LEVEL rather than a depth (rotary's
        # ``fast``), where 0.3 is just the un-cabled state.
        self.const = const
        # False where the module is block-size dependent for reasons of
        # its own, so the constant-CV pin cannot speak about it.
        self.block_exact = block_exact
        # True where the float32 mean actually broke the pin before the
        # fix -- the witnesses, re-checked against the old reduction.
        self.repaired = repaired

    def feeds(self, cv_value):
        f = dict(self.carrier)
        for port in self.cv_ports:
            f[port] = _const(cv_value, self.voices)
        return f


CASES = [
    _Case("filter_mono", "filter", ["cutoff_cv", "resonance_cv"],
          {"in": _sine()}, params={"cutoff": 800.0}, repaired=True),
    _Case("filter_voice", "filter", ["cutoff_cv", "resonance_cv"],
          {"in": _sine(voices=V)}, params={"cutoff": 800.0}, voices=V,
          repaired=True),
    _Case("crossover", "crossover", ["freq_cv"], {"in": _noise()},
          repaired=True),
    _Case("sweep_eq", "sweep_eq", ["freq_cv"], {"in": _noise()},
          repaired=True),
    _Case("motion_eq", "motion_eq",
          ["band1_freq_cv", "band2_gain_cv", "band3_q_cv"], {"in": _noise()},
          params={"band1_gain": 9.0, "band2_gain": -9.0}, repaired=True),
    _Case("tilt_eq", "tilt_eq", ["tilt_cv"], {"in": _noise()}, repaired=True),
    _Case("loudness", "loudness", ["level_cv"], {"in": _noise()},
          repaired=True),
    _Case("compressor", "compressor", ["threshold_cv"], {"in": _sine(amp=0.9)},
          params={"ratio": 4.0}, repaired=True),
    _Case("reverb", "reverb", ["decay_cv", "damping_cv", "mix_cv"],
          {"in": _impulse()}, repaired=True),
    _Case("flanger", "flanger", ["rate_cv"], {"in": _sine()}, repaired=True),
    _Case("phaser", "phaser", ["rate_cv"], {"in": _sine()}, repaired=True),
    _Case("lfo_mono", "lfo", ["rate_cv"], {}, repaired=True),
    _Case("lfo_voice", "lfo", ["rate_cv"], {}, voices=V),
    _Case("slew_mono", "slew", ["rise_cv", "fall_cv"], {"in": _sine()},
          params={"rise": 0.01, "fall": 0.02}, repaired=True),
    _Case("slew_voice", "slew", ["rise_cv", "fall_cv"],
          {"in": _sine(voices=V)}, params={"rise": 0.01, "fall": 0.02},
          voices=V, repaired=True),
    _Case("function_generator", "function_generator",
          ["rate_cv", "rise_cv", "fall_cv"], {"trig": _gate()},
          params={"rise": 0.02, "fall": 0.05}),
    _Case("supersaw_mono", "supersaw", ["detune_cv"], {}),
    # The voice path's own phase is a per-sample cumsum, which drifts by
    # ~3e-11 between block sizes whatever the detune does; the pin is
    # skipped, the scrub still checked.
    _Case("supersaw_voice", "supersaw", ["detune_cv"],
          {"freq_cv": _const(0.0, V)}, voices=V, block_exact=False),
    _Case("wavetable_morph", "wavetable_morph", ["position_cv"], {},
          repaired=True),
    _Case("bitcrusher", "bitcrusher", ["bits_cv", "rate_cv"], {"in": _sine()},
          params={"bits": 12.0, "rate_div": 4.0}),
    # Grain scheduling is block-size dependent by construction (0.54
    # between 64 and 512 with no CV at all), so only the scrub is pinned.
    _Case("pitch_shifter", "pitch_shifter", ["pitch_cv"], {"in": _sine()},
          params={"grain_size": 10.0}, block_exact=False),
    _Case("drift", "drift", ["rate_cv"], {}),
    _Case("bowed", "bowed", ["pitch_cv"], {"gate": _gate()}, repaired=True),
    _Case("wind", "wind", ["pitch_cv"], {"gate": _gate()}, repaired=True),
    _Case("modal", "modal", ["pitch_cv"], {"excite": _impulse()}),
    # ``fast`` is a LEVEL, read as the block's majority: 0.3 is just
    # "slow", the un-cabled state, so this case is fed a held gate.
    _Case("rotary", "rotary", ["fast"], {"in": _sine()}, const=1.0),
]
CASE_IDS = [c.name for c in CASES]
REPAIRED = [c for c in CASES if c.repaired]
REPAIRED_IDS = [c.name for c in REPAIRED]


def _old_float32_mean(cv, default=0.0, axis=None):
    """The expression every routed site ran before the fix: ``np.mean``
    with no dtype (so float32 in, float32 accumulation) and no scrub."""
    a = np.asarray(cv)
    if a.size == 0:
        return float(default)
    if axis is None:
        return float(np.mean(a))
    return a.mean(axis=axis)


# ----- the helper -------------------------------------------------------------

def test_helper_averages_in_float64():
    f = NumpyBackend._finite_mean
    exact = float(np.float32(CONST_CV))
    for n in (64, 100, 128, 333, 512, 1024):
        buf = np.full(n, CONST_CV, dtype=np.float32)
        assert f(buf) == exact, f"{n}: the mean of a constant is not that constant"


def test_the_float32_mean_it_replaces_is_not_block_size_exact():
    """The wart, stated as a test: this is what every routed site did."""
    small = np.full(SMALL, CONST_CV, dtype=np.float32)
    large = np.full(LARGE, CONST_CV, dtype=np.float32)
    assert float(np.mean(small)) != float(np.mean(large))
    assert NumpyBackend._finite_mean(small) == NumpyBackend._finite_mean(large)


def test_helper_returns_a_python_float_on_the_scalar_path():
    got = NumpyBackend._finite_mean(np.zeros(8, dtype=np.float32))
    assert isinstance(got, float)
    assert not isinstance(got, np.generic)


def test_helper_is_the_plain_mean_for_finite_input():
    rng = np.random.default_rng(3)
    for n in (7, 64, 512):
        buf = rng.standard_normal(n).astype(np.float32)
        assert NumpyBackend._finite_mean(buf) == float(np.mean(buf, dtype=np.float64))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")],
                         ids=["nan", "inf", "-inf"])
def test_helper_reads_a_non_finite_mean_as_the_default(bad):
    buf = np.full(64, bad, dtype=np.float32)
    assert NumpyBackend._finite_mean(buf) == 0.0
    assert NumpyBackend._finite_mean(buf, default=1.0) == 1.0
    # one poisoned sample poisons its own block mean, and reads as the
    # default too -- "no modulation", not a rail
    mixed = np.full(64, 0.5, dtype=np.float32)
    mixed[17] = bad
    assert NumpyBackend._finite_mean(mixed) == 0.0


def test_helper_axis_path_scrubs_row_by_row():
    rows = np.array([[0.3, 0.3], [np.nan, 1.0], [2.0, 4.0], [np.inf, 0.0]],
                    dtype=np.float32)
    got = NumpyBackend._finite_mean(rows, axis=1)
    assert got.dtype == np.float64
    assert got.shape == (4,)
    # a poisoned voice reads as no modulation; its neighbours are untouched
    assert got[0] == float(np.float32(0.3))
    assert got[1] == 0.0
    assert got[2] == 3.0
    assert got[3] == 0.0
    assert NumpyBackend._finite_mean(rows, default=-1.0, axis=1)[1] == -1.0


def test_helper_handles_an_empty_buffer():
    assert NumpyBackend._finite_mean(np.zeros(0, dtype=np.float32)) == 0.0
    assert NumpyBackend._finite_mean(np.zeros(0, dtype=np.float32),
                                     default=0.25) == 0.25
    got = NumpyBackend._finite_mean(np.zeros((3, 0), dtype=np.float32), axis=1)
    assert got.shape == (3,) and np.array_equal(got, np.zeros(3))


def test_the_minmax_nan_wart_is_real():
    """Why the scrub has to happen BEFORE the clamp, not inside it.

    ``min``/``max`` compare, and every comparison against NaN is False,
    so the same clamp idiom gives two different wrong answers depending
    on which side the NaN is passed. ``np.clip`` propagates, and
    ``int(round(...))`` raises."""
    nan = float("nan")
    assert max(0.0, nan) == 0.0                 # silently pinned to a rail
    assert np.isnan(min(max(nan, 0.0), 1.0))    # silently poisoned
    assert np.isnan(np.clip(nan, 0.0, 1.0))
    with pytest.raises(ValueError):
        int(round(nan))


# ----- the property the float64 mean buys ------------------------------------

@pytest.mark.parametrize("case", [c for c in CASES if c.block_exact],
                         ids=[c.name for c in CASES if c.block_exact])
def test_constant_cv_is_block_size_exact(case):
    """A steady voltage on a jack must render the same at 64 as at 512."""
    small = _render(case.mtype, case.params, case.feeds(case.const), SMALL)
    large = _render(case.mtype, case.params, case.feeds(case.const), LARGE)
    assert set(small) == set(large)
    for name in sorted(small):
        a, b = np.asarray(small[name]), np.asarray(large[name])
        assert a.shape == b.shape, f"{case.name}.{name}: shape moved"
        assert np.array_equal(a, b), (
            f"{case.name}.{name}: max|64-512| = "
            f"{np.abs(a.astype(np.float64) - b.astype(np.float64)).max():.3e}"
        )


@pytest.mark.parametrize("case", REPAIRED, ids=REPAIRED_IDS)
def test_the_old_float32_mean_fails_that_pin(case, monkeypatch):
    """The tripwire self-test: with the pre-fix reduction swapped back
    in, every one of these modules renders differently at 64 than at
    512 under the same constant CV. If this stops failing, the pin above
    has stopped proving anything."""
    monkeypatch.setattr(NumpyBackend, "_finite_mean",
                        staticmethod(_old_float32_mean))
    small = _render(case.mtype, case.params, case.feeds(case.const), SMALL)
    large = _render(case.mtype, case.params, case.feeds(case.const), LARGE)
    assert any(not np.array_equal(small[k], large[k]) for k in small), (
        f"{case.name}: the old float32 mean was block-size exact here, so "
        f"this case is not a witness for the fix"
    )


# ----- the scrub --------------------------------------------------------------

@pytest.mark.parametrize("bad", ABSURD, ids=ABSURD_IDS)
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_absurd_cv_renders_finite_and_raises_nothing(case, bad):
    outs = _render(case.mtype, case.params, case.feeds(bad))
    assert outs, f"{case.name}: the renderer produced nothing"
    for name, arr in outs.items():
        assert np.all(np.isfinite(arr)), (
            f"{case.name}.{name}: non-finite output at cv={bad}")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_sane_cv_still_modulates(case):
    """So the two pins above are not comparing two silent renders."""
    with_cv = _render(case.mtype, case.params, case.feeds(case.const))
    without = _render(case.mtype, case.params, dict(case.carrier))
    assert any(
        not np.array_equal(with_cv[k], without.get(k, np.zeros(0)))
        for k in with_cv
    ), f"{case.name}: the CV changed nothing"


# ----- bitcrusher: the ValueError that started this --------------------------

def test_bitcrusher_bits_cv_nan_no_longer_raises():
    """``bits = int(round(bits))`` on a NaN mean raised ValueError out of
    the audio thread. The scrub means the knob's own bit depth stands."""
    nan_out = _render("bitcrusher", {"bits": 8.0}, {"in": _sine(),
                                                    "bits_cv": _const(float("nan"))})
    plain = _render("bitcrusher", {"bits": 8.0}, {"in": _sine()})
    assert np.array_equal(nan_out["out"], plain["out"])


# ----- counting the doors ----------------------------------------------------

# Every renderer this pass routed, and the block-mean CV ports it reads.
ROUTED_SOURCES = {
    "_governed_ratio": ["ratio_cv"],
    "_render_supersaw": ["detune_cv"],
    "_render_wavetable_morph": ["position_cv"],
    "_render_filter_mono": ["cutoff_cv", "resonance_cv"],
    "_render_filter_voice": ["cutoff_cv", "resonance_cv"],
    "_render_function_generator": ["rate_cv", "rise_cv", "fall_cv"],
    "_render_lfo_mono": ["rate_cv"],
    "_render_lfo_voice": ["rate_cv"],
    "_render_slew": ["rise_cv", "fall_cv"],
    "_render_crossover": ["freq_cv"],
    "_render_motion_eq": ["band{i}_freq_cv", "band{i}_gain_cv", "band{i}_q_cv"],
    "_render_sweep_eq": ["freq_cv"],
    "_render_loudness": ["level_cv"],
    "_render_tilt_eq": ["tilt_cv"],
    "_render_compressor": ["threshold_cv"],
    "_render_reverb": ["decay_cv", "damping_cv", "mix_cv"],
    "_render_rotary": ["fast"],
    "_render_flanger": ["rate_cv"],
    "_render_phaser": ["rate_cv"],
    "_pitch_shifter_core": ["pitch_cv"],
    "_render_bitcrusher": ["bits_cv", "rate_cv"],
    "_render_sampler": ["pitch_cv"],
    "_render_bowed": ["pitch_cv"],
    "_render_wind": ["pitch_cv"],
    "_render_drift": ["rate_cv"],
    "_render_modal": ["pitch_cv"],
}


@pytest.mark.parametrize("fname", sorted(ROUTED_SOURCES))
def test_routed_renderer_reads_its_block_mean_through_the_door(fname):
    """Count the doors in the source, not the behaviour: a new block-mean
    CV read added to one of these renderers with a bare ``np.mean`` (or a
    bare ``.mean(axis=...)``) is exactly the regression this pass fixed,
    and a behavioural test would not notice it until someone changed the
    block size."""
    src = inspect.getsource(getattr(NumpyBackend, fname))
    assert "_finite_mean(" in src, f"{fname}: no block-mean door"
    assert "float(np.mean(" not in src, (
        f"{fname}: a bare float(np.mean(...)) block mean is back")
    assert ".mean(axis=1)" not in src, (
        f"{fname}: a bare .mean(axis=1) block mean is back")


def test_the_helper_is_documented_as_the_linear_twin():
    doc = NumpyBackend._finite_mean.__doc__ or ""
    assert "float64" in doc
    assert "min" in doc and "max" in doc and "NaN" in doc


# ----- the sampler (a file, so its own fixture) ------------------------------

@pytest.fixture(scope="module")
def sine_wav(tmp_path_factory):
    t = np.arange(SR, dtype=np.float64) / SR
    data = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    path = tmp_path_factory.mktemp("cv_scrub") / "sine.wav"
    wavfile.write(path, SR, data)
    return str(path)


def test_sampler_constant_pitch_cv_is_block_size_exact(sine_wav):
    feeds = {"gate": _gate(), "pitch_cv": _const(CONST_CV)}
    small = _render("sampler", {"path": sine_wav}, feeds, SMALL)
    large = _render("sampler", {"path": sine_wav}, feeds, LARGE)
    for name in sorted(small):
        assert np.array_equal(small[name], large[name]), f"sampler.{name} moved"
    assert any(np.max(np.abs(a)) > 0.1 for a in small.values()), (
        "the sampler played nothing, so the pin is vacuous")


@pytest.mark.parametrize("bad", ABSURD, ids=ABSURD_IDS)
def test_sampler_absurd_pitch_cv_renders_finite(sine_wav, bad):
    feeds = {"gate": _gate(), "pitch_cv": _const(bad)}
    for arr in _render("sampler", {"path": sine_wav}, feeds).values():
        assert np.all(np.isfinite(arr))
