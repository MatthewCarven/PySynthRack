"""The pitch_shifter's stereo mode keeps its two sides together.

Matthew, 2026-11-19: a song patch (file_player -> one EQ per side -> one
pitch_shifter per side -> one tape per side) fell in and out of stereo
sync a few times per song, worse when he moved the pitch (he transposes
songs down to sing them). The cause: each pitch_shifter's WSOLA engine
makes two choices from its OWN side's audio --

  * when to regrain on deep bass (a longer grain, so a different delay),
  * where to splice each grain whenever the pitch is not exactly 0 --

so two of them, one per side, part by up to tens of milliseconds.

The fix is an ``in_r`` jack. With it patched the module runs one
``_GrainShifterPair`` per voice (one splice search for both sides, the
buffered sink's 2026-11-11 fix) and makes ONE regrain decision for both.
These tests pin:

  * the defect is real and visible in this material through two mono
    modules (so the stereo tests are known to be able to see it);
  * through one stereo module the sides stay together, at 0 st across
    regrains and while the pitch is moving;
  * a module with ``in_r`` left empty is the mono module, untouched;
  * the same signal on both sides comes out as the mono module renders it;
  * the jack semantics: ``out`` / ``out_l`` = the ``in`` side, ``out_r`` =
    the ``in_r`` side, ``spread`` has no say;
  * voices, a missing side, switching the cable in and out.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import (
    NumpyBackend,
    _GrainShifter,
    _GrainShifterPair,
)
from pysynthrack.core import Patch

SR = 44100
F = 512
# Two independent engines can sit a regrained grain apart (up to 150 ms
# grains), so look a long way either side.
MAX_LAG = 3000


def _backend():
    return NumpyBackend(sample_rate=SR, block_size=F)


def _material(seconds: float, seed: int = 20261119):
    """A stereo song stand-in: one noise common to both sides (something to
    line the sides up on), a different noise on each (something to disagree
    about), and a 33 Hz bass that sits LEFT in the first half and RIGHT in
    the second -- deep enough to make the engine regrain, on one side at a
    time."""
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    common = 0.04 * rng.standard_normal(n)
    bass = 0.6 * np.sin(2 * np.pi * 33.0 * t)
    half = n // 2
    bl = np.where(np.arange(n) < half, bass, 0.0)
    br = np.where(np.arange(n) >= half, bass, 0.0)
    # The bass dominates, as it does through Matthew's +24 dB @ 20 Hz EQs;
    # louder broadband noise would hide the period from the detector.
    left = common + 0.02 * rng.standard_normal(n) + bl
    right = common + 0.02 * rng.standard_normal(n) + br
    return left.astype(np.float32), right.astype(np.float32)


def _noise_pair(seconds: float, seed: int = 7):
    """No bass (so no regrain): common noise plus a different noise each."""
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    common = 0.3 * rng.standard_normal(n)
    return (
        (common + 0.15 * rng.standard_normal(n)).astype(np.float32),
        (common + 0.15 * rng.standard_normal(n)).astype(np.float32),
    )


def _knob(seconds: float):
    """pitch_cv as Matthew turning the offset knob to sing a song lower:
    rest at 0, ease down to -3 st over two seconds, hold, come back up a
    semitone. In CV units at the default cv_depth (12 st per unit)."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    st = np.interp(t, [0.0, 1.0, 3.0, 5.0, 6.0, 1e9], [0.0, 0.0, -3.0, -3.0, -2.0, -2.0])
    return (st / 12.0).astype(np.float32)


def _side_lags(left, right, start: int = SR, win: int = 4096, hop: int = 4096):
    """Per window: how many samples the right side trails the left (the peak
    of their cross-correlation), and how strong that peak is (0..1).

    Compared on the first difference: a gentle high-pass that keeps the
    shared noise and all but removes a one-sided bass, which would
    otherwise swamp the correlation."""
    left = np.diff(np.asarray(left, dtype=np.float64))
    right = np.diff(np.asarray(right, dtype=np.float64))
    lags, peaks = [], []
    for s in range(max(start, MAX_LAG), len(left) - win - MAX_LAG, hop):
        ref = left[s:s + win]
        c = np.correlate(right[s - MAX_LAG:s + win + MAX_LAG], ref, "valid")
        i = int(np.argmax(c))
        seg = right[s - MAX_LAG + i:s - MAX_LAG + i + win]
        lags.append(i - MAX_LAG)
        peaks.append(float(c[i] / (np.linalg.norm(ref) * np.linalg.norm(seg) + 1e-12)))
    return np.array(lags), np.array(peaks)


def _stereo_rig(params=None, with_cv=False, wire_left=True, wire_right=True):
    """One pitch_shifter; placeholder sources on ``in`` / ``in_r`` (their
    buffers are injected per block)."""
    patch = Patch()
    src_l = patch.add_module("oscillator")
    src_r = patch.add_module("oscillator")
    ps = patch.add_module("pitch_shifter", params=params or {})
    if wire_left:
        patch.connect(src_l.id, "out", ps.id, "in")
    if wire_right:
        patch.connect(src_r.id, "out", ps.id, "in_r")
    cv = None
    if with_cv:
        cv = patch.add_module("lfo")
        patch.connect(cv.id, "cv", ps.id, "pitch_cv")
    b = _backend()
    b.compile(patch)
    return patch, b, ps, src_l, src_r, cv


def _run_stereo(rig, xl, xr, cv=None):
    patch, b, ps, src_l, src_r, cvsrc = rig
    outs = {"out": [], "out_l": [], "out_r": []}
    for k in range(xl.shape[-1] // F):
        sl = slice(k * F, (k + 1) * F)
        bufs = {(src_l.id, "out"): xl[..., sl], (src_r.id, "out"): xr[..., sl]}
        if cv is not None:
            bufs[(cvsrc.id, "cv")] = cv[..., sl]
        r = b._render_pitch_shifter(ps, F, bufs, patch)
        for key in outs:
            outs[key].append(np.asarray(r[key], dtype=np.float64).copy())
    return {k: np.concatenate(v, axis=-1) for k, v in outs.items()}


def _run_mono(x, params=None, cv=None):
    """The mono module, as shipped: one pitch_shifter, ``in`` only."""
    patch = Patch()
    src = patch.add_module("oscillator")
    ps = patch.add_module("pitch_shifter", params=params or {})
    patch.connect(src.id, "out", ps.id, "in")
    cvsrc = None
    if cv is not None:
        cvsrc = patch.add_module("lfo")
        patch.connect(cvsrc.id, "cv", ps.id, "pitch_cv")
    b = _backend()
    b.compile(patch)
    outs = []
    for k in range(x.shape[-1] // F):
        sl = slice(k * F, (k + 1) * F)
        bufs = {(src.id, "out"): x[..., sl]}
        if cv is not None:
            bufs[(cvsrc.id, "cv")] = cv[..., sl]
        outs.append(np.asarray(b._render_pitch_shifter(ps, F, bufs, patch)["out"],
                               dtype=np.float64).copy())
    return np.concatenate(outs, axis=-1), b._state[ps.id]


# ----- the model ----------------------------------------------------------


class TestModel:
    def test_in_r_is_an_audio_input_after_in(self):
        ps = Patch().add_module("pitch_shifter")
        assert [(p.name, p.signal_kind) for p in ps.input_ports] == [
            ("in", "audio"),
            ("in_r", "audio"),
            ("pitch_cv", "cv"),
        ]

    def test_audio_into_in_r_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        ps = patch.add_module("pitch_shifter")
        patch.connect(osc.id, "out", ps.id, "in_r")

    def test_cv_into_in_r_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        ps = patch.add_module("pitch_shifter")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ps.id, "in_r")


# ----- exposure: two mono modules DO come apart ---------------------------


class TestTwoMonoModulesComeApart:
    """What Matthew's patch did. If these stop failing to stay together,
    the material no longer shows the defect and the tests below prove
    nothing."""

    def test_at_unity_the_deep_bass_regrain_parts_them(self):
        xl, xr = _material(8.0)
        yl, sl = _run_mono(xl)
        yr, sr = _run_mono(xr)
        assert int(sl["regrains"][0]) >= 1 and int(sr["regrains"][0]) >= 1
        lags, peaks = _side_lags(yl, yr)
        good = peaks > 0.5
        assert good.sum() > 10
        assert np.max(np.abs(lags[good])) > SR // 1000, (
            "two mono pitch_shifters never parted by even 1 ms on one-sided "
            "deep bass: the material no longer shows the defect"
        )

    def test_turning_the_pitch_parts_them(self):
        xl, xr = _noise_pair(8.0)
        cv = _knob(8.0)
        yl, sl = _run_mono(xl, cv=cv)
        yr, sr = _run_mono(xr, cv=cv)
        assert int(sl["regrains"][0]) == 0 and int(sr["regrains"][0]) == 0
        lags, peaks = _side_lags(yl, yr, start=2 * SR)
        good = peaks > 0.3
        assert good.sum() > 10
        assert np.max(np.abs(lags[good])) > SR // 1000


# ----- the fix: one stereo module stays together --------------------------


class TestOneStereoModuleStaysTogether:
    def test_at_unity_across_regrains(self):
        xl, xr = _material(8.0)
        rig = _stereo_rig()
        y = _run_stereo(rig, xl, xr)
        st = rig[1]._state[rig[2].id]["stereo"]
        assert int(st["regrains"][0]) >= 1, "the shared engine never regrained"
        lags, peaks = _side_lags(y["out_l"], y["out_r"])
        good = peaks > 0.5
        assert good.sum() > 10
        assert np.all(lags[good] == 0), f"sides parted: lags {sorted(set(lags[good]))}"

    def test_while_the_pitch_moves(self):
        xl, xr = _noise_pair(8.0)
        cv = _knob(8.0)
        rig = _stereo_rig(with_cv=True)
        y = _run_stereo(rig, xl, xr, cv=cv)
        lags, peaks = _side_lags(y["out_l"], y["out_r"], start=2 * SR)
        good = peaks > 0.3
        assert good.sum() > 10
        assert np.all(lags[good] == 0), f"sides parted: lags {sorted(set(lags[good]))}"

    def test_both_bass_and_pitch_together(self):
        # Matthew's real case: a song with deep bass, transposed down.
        xl, xr = _material(8.0, seed=3)
        cv = _knob(8.0)
        rig = _stereo_rig(with_cv=True)
        y = _run_stereo(rig, xl, xr, cv=cv)
        lags, peaks = _side_lags(y["out_l"], y["out_r"], start=2 * SR)
        good = peaks > 0.3
        assert good.sum() > 10
        assert np.all(lags[good] == 0)

    def test_the_two_engines_share_grain_and_delay_every_block(self):
        xl, xr = _material(6.0)
        rig = _stereo_rig()
        patch, b, ps, src_l, src_r, _ = rig
        for k in range(xl.shape[0] // F):
            sl = slice(k * F, (k + 1) * F)
            b._render_pitch_shifter(
                ps, F, {(src_l.id, "out"): xl[sl], (src_r.id, "out"): xr[sl]}, patch
            )
            pair = b._state[ps.id]["stereo"]["pair"][0]
            left, right = pair.engines
            assert left.Lg == right.Lg
            assert left.latency(1.0) == right.latency(1.0)

    def test_a_shared_regrain_follows_the_deeper_side(self):
        # Bass on the LEFT only for the whole run: the pair must still grow
        # its grain (the deeper side decides for both).
        n = 5 * SR
        rng = np.random.default_rng(1)
        t = np.arange(n) / SR
        xl = (0.6 * np.sin(2 * np.pi * 33.0 * t) + 0.05 * rng.standard_normal(n)).astype(np.float32)
        xr = (0.3 * rng.standard_normal(n)).astype(np.float32)
        rig = _stereo_rig()
        _run_stereo(rig, xl, xr)
        st = rig[1]._state[rig[2].id]["stereo"]
        assert int(st["regrains"][0]) >= 1
        assert st["pair"][0].engines[0].Lg > int(round(0.05 * SR))


# ----- compatibility --------------------------------------------------------


class TestCompatibility:
    def test_in_r_unpatched_is_the_mono_module(self):
        # Nothing on in_r: the shipped mono engine list, no stereo state.
        xl, _ = _material(3.0)
        y, st = _run_mono(xl, params={"semitones": -2.0})
        assert "stereo" not in st
        assert all(type(e) is _GrainShifter for e in st["eng"])
        assert np.all(np.isfinite(y))

    @pytest.mark.parametrize("params", [
        {},
        {"semitones": -3.0},
        {"semitones": 4.0, "formant_preserve": True},
        {"semitones": 7.0, "feedback": 0.4, "harmony": 12.0, "harmony_level": 0.5,
         "mix": 0.7},
    ])
    def test_same_signal_both_sides_is_the_mono_render(self, params):
        x, _ = _material(4.0)
        mono, _st = _run_mono(x, params=params)
        y = _run_stereo(_stereo_rig(params), x, x)
        # Identical inputs through a pair: identical outputs, exactly.
        np.testing.assert_array_equal(y["out_l"], y["out_r"])
        # And the pair's joint score reduces to the lone score -- to within
        # the last-place rounding the pair's docstring describes.
        np.testing.assert_allclose(y["out_l"], mono, rtol=0, atol=1e-6)

    def test_out_is_the_left_side_and_spread_has_no_say(self):
        xl, xr = _noise_pair(3.0)
        params = {"semitones": 5.0, "harmony": 12.0, "harmony_level": 0.6}
        y0 = _run_stereo(_stereo_rig(params), xl, xr)
        y1 = _run_stereo(_stereo_rig({**params, "spread": 1.0}), xl, xr)
        np.testing.assert_array_equal(y0["out"], y0["out_l"])
        np.testing.assert_array_equal(y0["out_l"], y1["out_l"])
        np.testing.assert_array_equal(y0["out_r"], y1["out_r"])
        assert not np.allclose(y0["out_l"], y0["out_r"])

    def test_pitch_moves_on_both_sides(self):
        n = int(1.5 * SR)
        t = np.arange(n) / SR
        xl = (0.8 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        xr = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        y = _run_stereo(_stereo_rig({"semitones": 12.0}), xl, xr)
        for key in ("out_l", "out_r"):
            seg = y[key][int(0.5 * n):int(0.92 * n)]
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            hz = np.fft.rfftfreq(len(seg), 1.0 / SR)[spec.argmax()]
            assert hz == pytest.approx(880.0, rel=0.03), key


# ----- edges ----------------------------------------------------------------


class TestEdges:
    def test_only_in_r_patched_leaves_the_left_silent(self):
        _, xr = _noise_pair(2.0)
        rig = _stereo_rig({"semitones": -2.0}, wire_left=False)
        y = _run_stereo(rig, np.zeros_like(xr), xr)
        assert not y["out_l"].any()
        assert not y["out"].any()
        mono, _st = _run_mono(xr, params={"semitones": -2.0})
        np.testing.assert_allclose(y["out_r"], mono, rtol=0, atol=1e-6)

    def test_voices_pair_up_row_by_row(self):
        xl, xr = _noise_pair(2.0)
        V = 3
        # Voice 0 differs per side; voices 1 and 2 are the same both sides.
        L = np.stack([xl, xl * 0.5, xr]).astype(np.float32)
        R = np.stack([xr, xl * 0.5, xr]).astype(np.float32)
        y = _run_stereo(_stereo_rig({"semitones": -2.0}), L, R)
        assert y["out_l"].shape == (V, L.shape[1] // F * F)
        np.testing.assert_array_equal(y["out_l"][1], y["out_r"][1])
        np.testing.assert_array_equal(y["out_l"][2], y["out_r"][2])
        assert not np.array_equal(y["out_l"][0], y["out_r"][0])

    def test_one_voice_row_is_the_flat_render(self):
        xl, xr = _noise_pair(2.0)
        flat = _run_stereo(_stereo_rig({"semitones": 3.0}), xl, xr)
        rows = _run_stereo(_stereo_rig({"semitones": 3.0}), xl[None, :], xr[None, :])
        np.testing.assert_array_equal(rows["out_l"][0], flat["out_l"])
        np.testing.assert_array_equal(rows["out_r"][0], flat["out_r"])

    def test_mixed_shapes_collapse_to_mono_rows(self):
        xl, xr = _noise_pair(1.0)
        L = np.stack([xl, xl]).astype(np.float32)        # voice-aware left
        y = _run_stereo(_stereo_rig(), L, xr)             # mono right
        assert y["out_l"].ndim == 1 and y["out_r"].ndim == 1

    def test_patching_and_unpatching_in_r_rebuilds_cleanly(self):
        xl, xr = _noise_pair(1.0)
        patch, b, ps, src_l, src_r, _ = _stereo_rig(wire_right=False)

        def block(k):
            sl = slice(k * F, (k + 1) * F)
            return b._render_pitch_shifter(
                ps, F, {(src_l.id, "out"): xl[sl], (src_r.id, "out"): xr[sl]}, patch
            )

        for k in range(10):
            block(k)
        assert "stereo" not in b._state[ps.id]
        patch.connect(src_r.id, "out", ps.id, "in_r")
        for k in range(10, 20):
            r = block(k)
            assert np.all(np.isfinite(r["out_r"]))
        assert "stereo" in b._state[ps.id] and "eng" not in b._state[ps.id]
        assert patch.disconnect(src_r.id, "out", ps.id, "in_r")
        for k in range(20, 30):
            r = block(k)
            assert np.all(np.isfinite(r["out"]))
        assert "stereo" not in b._state[ps.id]

    def test_disconnected_both_is_silence(self):
        rig = _stereo_rig(wire_left=False, wire_right=False)
        patch, b, ps, *_ = rig
        r = b._render_pitch_shifter(ps, 256, {}, patch)
        assert r["out"].shape == (256,) and not r["out"].any()


# ----- the pair still behaves as it did for the sink ------------------------


def test_the_pair_takes_a_dry_ring_and_reads_it_back():
    # The pitch_shifter feeds the pair a whitened signal plus the raw input
    # (formant preserve, shimmer); the dry ring must carry the raw one.
    pair = _GrainShifterPair(2205, 2, 16384)
    rng = np.random.default_rng(0)
    for _ in range(20):
        xl, xr = rng.standard_normal(F), rng.standard_normal(F)
        dl, dr = rng.standard_normal(F), rng.standard_normal(F)
        pair.process(xl, xr, 1.0, x_dry_l=dl, x_dry_r=dr)
    left, right = pair.engines
    np.testing.assert_array_equal(left.history(F, dry=True), dl)
    np.testing.assert_array_equal(right.history(F, dry=True), dr)
    np.testing.assert_array_equal(left.history(F), xl)


# ----- the real render path -------------------------------------------------


def test_stereo_pitch_shifter_through_render_block():
    # Two different tones in, one per side, a fifth down, into a stereo
    # speaker: each channel carries its own side, shifted.
    patch = Patch()
    osc_l = patch.add_module("oscillator", params={"waveform": "sine", "freq": 440.0, "amp": 0.5})
    osc_r = patch.add_module("oscillator", params={"waveform": "sine", "freq": 660.0, "amp": 0.5})
    ps = patch.add_module("pitch_shifter", params={"semitones": -7.0})
    spk = patch.add_module("stereo_speaker_output")
    patch.connect(osc_l.id, "out", ps.id, "in")
    patch.connect(osc_r.id, "out", ps.id, "in_r")
    patch.connect(ps.id, "out_l", spk.id, "in_l")
    patch.connect(ps.id, "out_r", spk.id, "in_r")
    b = _backend()
    b.compile(patch)
    blocks = [b.render_block(F) for _ in range(160)]
    assert all(blk is not None and np.all(np.isfinite(blk)) for blk in blocks)
    y = np.concatenate(blocks[60:])
    for ch, hz in ((0, 440.0 * 2 ** (-7 / 12)), (1, 660.0 * 2 ** (-7 / 12))):
        seg = y[:, ch].astype(np.float64)
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        peak = np.fft.rfftfreq(len(seg), 1.0 / SR)[spec.argmax()]
        assert peak == pytest.approx(hz, rel=0.02), ch
