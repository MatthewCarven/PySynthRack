"""Every block-mean octave exponent is clipped before its power.

The trap (found by the filter's ``resonance_cv`` pass, 2026-09-19): a
block-mean CV path of the form ``base * 2.0 ** (depth * mean(cv))`` is a
PYTHON-FLOAT power, and an absurd CV -- a ``constant`` at 1e6, a cv_math
product gone wild, a runaway loop's non-finite scrub, a NaN mean --
raises ``OverflowError`` or propagates NaN out of the render, and the
audio thread dies. Three paths already clipped (filter ``resonance_cv``,
clock ``bpm_cv``, freeze ``pitch_cv``); this file covers the one helper
the rest now go through, ``NumpyBackend._pow2_clipped``, and every
routed module:

* the helper: clip, NaN -> 1.0, sane values bit-exact against the bare
  ``2.0 ** e``, arrays keep their dtype;
* every routed renderer fed 1e6, -1e6 and NaN on its octave CV port(s)
  returns finite output and raises nothing;
* every routed renderer fed a SANE CV renders bit-exact against the
  pre-change expression -- the helper monkeypatched back to the bare
  ``2.0 ** e`` -- with a call counter so a case whose feed never reaches
  the power cannot pass vacuously;
* the sampler's ``playback_rate`` (numpy-free, so it restates the rule)
  gets the same three checks.

The renderers are driven directly with a fake patch whose
``cables_into`` returns cable objects pointing at buffer keys (the
``test_freeze.py`` idiom), so each case is one module, no topo walk.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.io import wavfile

import pysynthrack.modules  # noqa: F401  (registers every type)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch
from pysynthrack.modules.sampler import OCT_EXP_LIMIT, playback_rate

SR = 44100
FRAMES = 512
BLOCKS = 4
SRC = 99          # the fake source module every feed cable points at
V = 4             # voice count for the (V, F) cases
SANE_CV = 0.37    # not a power-of-two edge, inside every tighter limit
ABSURD = [1e6, -1e6, float("nan")]
ABSURD_IDS = ["1e6", "-1e6", "nan"]


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


def _render(mtype, params, feeds, seed=0):
    """Drive ``_render_<mtype>`` for BLOCKS blocks; returns the per-block
    results (an ndarray or a dict of ndarrays each, as the renderer
    hands them back). ``feeds`` maps an input-port name to a full-length
    signal, ``(N,)`` or ``(V, N)``, sliced per block along the last axis."""
    p = Patch()
    m = p.add_module(mtype)
    for k, v in params.items():
        m.params[k] = v
    b = NumpyBackend(sample_rate=SR, block_size=FRAMES)
    b.compile(p)
    if mtype == "sampler":
        b.wait_for_sample_loads()
    fp = _FakePatch([_Cable(port, port) for port in feeds])
    render = getattr(b, f"_render_{mtype}")
    np.random.seed(seed)   # unseeded modules draw from the global rng
    outs = []
    for k in range(BLOCKS):
        sl = (Ellipsis, slice(k * FRAMES, (k + 1) * FRAMES))
        bufs = {
            (SRC, port): np.ascontiguousarray(sig[sl], dtype=np.float32)
            for port, sig in feeds.items()
        }
        res = render(m, FRAMES, bufs, fp)
        if isinstance(res, dict):
            outs.append({k2: np.asarray(v).copy() for k2, v in res.items()})
        else:
            outs.append(np.asarray(res).copy())
    return outs


def _flat(outs):
    """Every array a render produced, in a stable order."""
    arrays = []
    for res in outs:
        if isinstance(res, dict):
            arrays.extend(res[k] for k in sorted(res))
        else:
            arrays.append(res)
    return arrays


# ----- signals ----------------------------------------------------------------

N = FRAMES * BLOCKS


def _sine(freq=220.0, amp=0.3, voices=None):
    t = np.arange(N, dtype=np.float64) / SR
    x = amp * np.sin(2.0 * np.pi * freq * t)
    if voices is None:
        return x
    return np.stack([amp * np.sin(2.0 * np.pi * freq * (1 + 0.1 * v) * t)
                     for v in range(voices)])


def _noise(amp=0.3, voices=None):
    rng = np.random.default_rng(11)
    shape = (N,) if voices is None else (voices, N)
    return amp * rng.standard_normal(shape)


def _gate(on_until=3 * FRAMES, voices=None):
    g = np.zeros(N)
    g[:on_until] = 1.0
    if voices is None:
        return g
    return np.stack([g] * voices)


def _pulse(at=8, width=32, voices=None):
    g = np.zeros(N)
    g[at:at + width] = 1.0
    if voices is None:
        return g
    return np.stack([g] * voices)


def _impulse(at=10, voices=None):
    x = np.zeros(N)
    x[at] = 0.8
    if voices is None:
        return x
    return np.stack([x] * voices)


def _const(value, voices=None):
    shape = (N,) if voices is None else (voices, N)
    return np.full(shape, value, dtype=np.float64)


# ----- the routed modules -----------------------------------------------------

class _Case:
    """One routed renderer: its type, params, its carrier feeds (audio /
    gate / excite) and the octave CV ports the fix clips."""

    def __init__(self, name, mtype, cv_ports, carrier, params=None, voices=None):
        self.name = name
        self.mtype = mtype
        self.cv_ports = cv_ports
        self.carrier = carrier      # dict port -> signal
        self.params = params or {}
        self.voices = voices

    def feeds(self, cv_value):
        f = dict(self.carrier)
        for port in self.cv_ports:
            f[port] = _const(cv_value, self.voices)
        return f


CASES = [
    _Case("filter_mono", "filter", ["cutoff_cv"], {"in": _sine()},
          params={"cutoff": 800.0}),
    _Case("filter_voice", "filter", ["cutoff_cv"], {"in": _sine(voices=V)},
          params={"cutoff": 800.0}, voices=V),
    _Case("crossover", "crossover", ["freq_cv"], {"in": _noise()}),
    _Case("motion_eq", "motion_eq",
          ["band1_freq_cv", "band2_q_cv"], {"in": _noise()},
          params={"band1_gain": 9.0, "band2_gain": -9.0}),
    _Case("sweep_eq", "sweep_eq", ["freq_cv"], {"in": _noise()}),
    _Case("chorus", "chorus", ["rate_cv"], {"in": _sine()}),
    _Case("flanger", "flanger", ["rate_cv"], {"in": _sine()}),
    _Case("phaser", "phaser", ["rate_cv"], {"in": _sine()}),
    _Case("bitcrusher", "bitcrusher", ["rate_cv"], {"in": _sine()},
          params={"rate_div": 4.0}),
    _Case("bowed", "bowed", ["pitch_cv"], {"gate": _gate()}),
    _Case("wind", "wind", ["pitch_cv"], {"gate": _gate()}),
    _Case("drift", "drift", ["rate_cv"], {}),
    _Case("organ", "organ", ["pitch_cv"], {"gate": _gate()}),
    _Case("modal", "modal", ["pitch_cv"], {"excite": _impulse()}),
    _Case("function_generator_mono", "function_generator",
          ["rate_cv", "rise_cv", "fall_cv"], {"trig": _pulse()},
          params={"rise": 0.02, "fall": 0.05}),
    _Case("function_generator_voice", "function_generator",
          ["rise_cv", "fall_cv"], {"trig": _pulse(voices=V)},
          params={"rise": 0.02, "fall": 0.05}, voices=V),
    # 10 ms grains so the wet path clears its latency inside BLOCKS;
    # harmony on so the second engine's power (r2) is exercised too.
    _Case("pitch_shifter", "pitch_shifter", ["pitch_cv"], {"in": _sine()},
          params={"grain_size": 10.0, "harmony": 7.0, "harmony_level": 0.5}),
    _Case("slew_mono", "slew", ["rise_cv", "fall_cv"], {"in": _pulse(width=600)},
          params={"rise": 0.01, "fall": 0.02}),
    _Case("slew_voice", "slew", ["rise_cv", "fall_cv"],
          {"in": _pulse(width=600, voices=V)},
          params={"rise": 0.01, "fall": 0.02}, voices=V),
]
CASE_IDS = [c.name for c in CASES]


# ----- the helper -------------------------------------------------------------

def test_helper_clips_the_exponent_to_the_default_limit():
    f = NumpyBackend._pow2_clipped
    lim = NumpyBackend._OCT_EXP_LIMIT
    assert lim == 64.0
    assert f(1e6) == 2.0 ** lim
    assert f(-1e6) == 2.0 ** -lim
    assert f(lim + 1.0) == 2.0 ** lim
    assert f(-lim - 1.0) == 2.0 ** -lim
    assert f(lim) == 2.0 ** lim          # the rail itself is inside


def test_helper_honours_a_tighter_limit():
    f = NumpyBackend._pow2_clipped
    assert f(10.0, 5.0) == 2.0 ** 5.0
    assert f(-10.0, 5.0) == 2.0 ** -5.0
    assert f(2.5, 5.0) == 2.0 ** 2.5


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")],
                         ids=["nan", "inf", "-inf"])
def test_helper_reads_a_non_finite_exponent_as_no_modulation(bad):
    # A NaN block-mean must mean "no modulation" -- exactly 1.0, so
    # ``base * ratio`` is bit-identical to ``base`` -- not poison.
    assert NumpyBackend._pow2_clipped(bad) == 1.0


def test_helper_is_bit_exact_against_the_bare_power_for_sane_values():
    f = NumpyBackend._pow2_clipped
    es = np.concatenate([
        np.linspace(-64.0, 64.0, 4097),
        np.array([0.0, 0.5, -0.5, 1.0 / 3.0, 0.37, -7.125, 12.0, 63.999]),
    ])
    for e in es.tolist():
        assert f(e) == 2.0 ** e
        assert isinstance(f(e), float)
    # a numpy scalar in is still a Python float out
    assert f(np.float32(0.5)) == 2.0 ** 0.5
    assert isinstance(f(np.float64(0.25)), float)


def test_helper_array_branch_is_bit_exact_and_keeps_dtype():
    f = NumpyBackend._pow2_clipped
    e64 = np.array([-3.0, -0.25, 0.0, 0.37, 2.5, 64.0])
    out = f(e64)
    assert out.dtype == np.float64
    assert np.array_equal(out, 2.0 ** e64)
    e32 = np.array([-3.0, -0.25, 0.0, 0.37, 2.5], dtype=np.float32)
    out32 = f(e32)
    assert out32.dtype == np.float32          # weak-scalar rule kept
    assert np.array_equal(out32, np.power(2.0, e32))
    # the per-row scrub: only the bad rows move
    mixed = np.array([0.5, 1e6, np.nan, -1e6, -np.inf])
    got = f(mixed)
    assert np.all(np.isfinite(got))
    assert got[0] == 2.0 ** 0.5
    assert got[1] == 2.0 ** 64.0
    assert got[2] == 1.0
    assert got[3] == 2.0 ** -64.0
    assert got[4] == 1.0


# ----- every routed renderer --------------------------------------------------

@pytest.mark.parametrize("bad", ABSURD, ids=ABSURD_IDS)
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_absurd_cv_renders_finite_and_raises_nothing(case, bad):
    outs = _render(case.mtype, case.params, case.feeds(bad))
    arrays = _flat(outs)
    assert arrays, f"{case.name}: the renderer produced nothing"
    for arr in arrays:
        assert np.all(np.isfinite(arr)), f"{case.name}: non-finite output at cv={bad}"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_sane_cv_is_bit_exact_against_the_pre_change_power(case, monkeypatch):
    """The clip is a no-op inside the rail: the routed render equals the
    render with the helper swapped back for the bare ``2.0 ** e`` --
    the expression every one of these sites ran before the fix."""
    calls = []
    real = NumpyBackend._pow2_clipped

    def counting(exponent, limit=NumpyBackend._OCT_EXP_LIMIT):
        calls.append(1)
        return real(exponent, limit)

    monkeypatch.setattr(NumpyBackend, "_pow2_clipped", staticmethod(counting))
    routed = _render(case.mtype, case.params, case.feeds(SANE_CV))
    assert calls, f"{case.name}: the CV never reached the power (vacuous case)"

    monkeypatch.setattr(
        NumpyBackend, "_pow2_clipped",
        staticmethod(lambda exponent, limit=64.0: 2.0 ** exponent),
    )
    bare = _render(case.mtype, case.params, case.feeds(SANE_CV))

    a, b = _flat(routed), _flat(bare)
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x.shape == y.shape
        assert x.dtype == y.dtype
        assert np.array_equal(x, y), f"{case.name}: routed render differs from the bare power"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_sane_cv_actually_modulates(case):
    """The counter above proves the power ran; this proves the CV moved
    something, so the bit-exact pin is comparing modulated renders."""
    with_cv = _flat(_render(case.mtype, case.params, case.feeds(SANE_CV)))
    without = _flat(_render(case.mtype, case.params, dict(case.carrier)))
    assert any(not np.array_equal(x, y) for x, y in zip(with_cv, without)), (
        f"{case.name}: the sane CV changed nothing")


# ----- the sampler (playback_rate lives in modules/sampler.py) ----------------

def test_sampler_playback_rate_is_bit_exact_for_playable_pitches():
    import itertools
    for cv, root, tune, fine in itertools.product(
        (-2.0, -0.3, 0.0, 0.25, 1.0, 3.5), (48, 60, 72),
        (-12.0, 0.0, 7.0), (-50.0, 0.0, 33.0),
    ):
        raw = 2.0 ** (((60 + 12.0 * cv) - root + tune + fine / 100.0) / 12.0)
        assert playback_rate(cv, root, tune, fine) == raw
    assert playback_rate(0.0, 60, 0.0, 0.0) == 1.0     # the neutral, exactly


def test_sampler_playback_rate_clips_and_scrubs():
    assert OCT_EXP_LIMIT == NumpyBackend._OCT_EXP_LIMIT
    assert math.isfinite(playback_rate(1e6, 60, 0.0, 0.0))
    assert playback_rate(1e6, 60, 0.0, 0.0) == 2.0 ** OCT_EXP_LIMIT
    assert playback_rate(-1e6, 60, 0.0, 0.0) == 2.0 ** -OCT_EXP_LIMIT
    # a NaN cv reads as 0 V: the root note, the file untouched
    assert playback_rate(float("nan"), 60, 0.0, 0.0) == 1.0
    assert playback_rate(float("nan"), 48, 0.0, 0.0) == 2.0


@pytest.fixture(scope="module")
def sine_wav(tmp_path_factory):
    t = np.arange(SR, dtype=np.float64) / SR
    data = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    path = tmp_path_factory.mktemp("cv_overflow") / "sine.wav"
    wavfile.write(path, SR, data)
    return str(path)


@pytest.mark.parametrize("bad", ABSURD, ids=ABSURD_IDS)
def test_sampler_absurd_pitch_cv_renders_finite(sine_wav, bad):
    feeds = {"gate": _pulse(width=64), "pitch_cv": _const(bad)}
    outs = _render("sampler", {"path": sine_wav}, feeds)
    for arr in _flat(outs):
        assert np.all(np.isfinite(arr))


def test_sampler_sane_pitch_cv_plays(sine_wav):
    feeds = {"gate": _gate(), "pitch_cv": _const(SANE_CV)}
    outs = _flat(_render("sampler", {"path": sine_wav}, feeds))
    assert any(np.max(np.abs(a)) > 0.1 for a in outs)
