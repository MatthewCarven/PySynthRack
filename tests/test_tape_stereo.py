"""tape's stereo jacks: one tape, two tracks.

Matthew, 2026-11-19, after the stereo pitch_shifter: "can we do the same for
tape and parametric eq?" Two tape modules, one per side, are two machines:
flutter and drift draw from a generator seeded by each module's id, so with
any of them up the two sides wobble independently -- the image smears and
wanders. With ``in_r`` patched the module is one tape with two tracks: one
transport (one modulation curve, one stop) for both sides, each side's own
delay line, saturation, head bump and hiss. These tests pin:

  * exposure: two mono tapes fed the SAME signal come apart in time;
  * one stereo tape fed the same signal both sides (hiss off) gives the same
    output both sides, and that output is the mono tape's, bit for bit --
    stop gate and all;
  * each side, fed different material, is the mono tape on that side;
  * hiss: the left track's is the mono tape's, the right track's is its
    own (uncorrelated, same level);
  * block-size independence with everything on (the tape's house rule);
  * the bit-exact neutral and dry paths, shapes, a missing side, patching
    ``in_r`` in and out, and the real render path.
"""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
F = 512
ALL_ON = {"wow": 0.4, "flutter": 0.7, "drift": 1.0, "sat": 0.5, "bump": 4.0,
          "mix": 0.9, "stop_time": 0.3, "start_time": 0.2}


def _noise(seconds, seed, level=0.3):
    rng = np.random.default_rng(seed)
    return (level * rng.standard_normal(int(seconds * SR))).astype(np.float32)


def _gate(seconds):
    """Two tape-stops: 1.0-1.5 s and 2.6-2.8 s."""
    n = int(seconds * SR)
    g = np.zeros(n, dtype=np.float32)
    g[int(1.0 * SR):int(1.5 * SR)] = 1.0
    g[int(2.6 * SR):int(2.8 * SR)] = 1.0
    return g


def _stereo_rig(params, block=F, wire_left=True, wire_right=True, stop=True):
    # Module ids: src_l 1, src_r 2, tape 3, clock 4 -- matched by _mono_rig,
    # so the tape's seeded generators are the same in both.
    patch = Patch()
    src_l = patch.add_module("oscillator")
    src_r = patch.add_module("oscillator")
    tp = patch.add_module("tape", params=dict(params))
    clk = patch.add_module("clock")
    if wire_left:
        patch.connect(src_l.id, "out", tp.id, "in")
    if wire_right:
        patch.connect(src_r.id, "out", tp.id, "in_r")
    if stop:
        patch.connect(clk.id, "out", tp.id, "stop")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, b, tp, src_l, src_r, clk


def _mono_rig(params, block=F, stop=True, extra=False):
    """One mono tape with module id 3 (or 4 with ``extra``: a second,
    differently seeded machine)."""
    patch = Patch()
    src = patch.add_module("oscillator")
    patch.add_module("oscillator")            # id 2: keeps the tape at id 3
    if extra:
        patch.add_module("oscillator")
    tp = patch.add_module("tape", params=dict(params))
    clk = patch.add_module("clock")
    patch.connect(src.id, "out", tp.id, "in")
    if stop:
        patch.connect(clk.id, "out", tp.id, "stop")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, b, tp, src, clk


def _run_stereo(rig, xl, xr, g=None, block=F):
    patch, b, tp, src_l, src_r, clk = rig
    outs = {"out": [], "out_r": []}
    for k in range(xl.shape[-1] // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src_l.id, "out"): xl[..., sl], (src_r.id, "out"): xr[..., sl]}
        if g is not None:
            bufs[(clk.id, "out")] = g[sl]
        r = b._render_tape(tp, block, bufs, patch)
        for key in outs:
            outs[key].append(np.asarray(r[key]).copy())
    return {k: np.concatenate(v, axis=-1) for k, v in outs.items()}


def _run_mono(rig, x, g=None, block=F):
    patch, b, tp, src, clk = rig
    outs = []
    for k in range(x.shape[-1] // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): x[..., sl]}
        if g is not None:
            bufs[(clk.id, "out")] = g[sl]
        outs.append(np.asarray(b._render_tape(tp, block, bufs, patch)).copy())
    return np.concatenate(outs, axis=-1)


def _lags(a, b, win=4096, hop=4096, max_lag=400):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    lags = []
    for s in range(max_lag, len(a) - win - max_lag, hop):
        c = np.correlate(b[s - max_lag:s + win + max_lag], a[s:s + win], "valid")
        lags.append(int(np.argmax(c)) - max_lag)
    return np.array(lags)


class TestModel:
    def test_jacks(self):
        tp = Patch().add_module("tape")
        assert [(p.name, p.signal_kind) for p in tp.input_ports] == [
            ("in", "audio"), ("in_r", "audio"), ("stop", "gate")]
        assert [(p.name, p.signal_kind) for p in tp.output_ports] == [
            ("out", "audio"), ("out_r", "audio")]


class TestTwoMonoTapesComeApart:
    def test_the_same_signal_through_two_machines_wanders(self):
        # Exposure: if this stops showing a gap, the tests below prove nothing.
        x = _noise(6.0, 1)
        params = {"flutter": 0.7, "drift": 1.0, "wow": 0.4}
        a = _run_mono(_mono_rig(params, stop=False), x)
        b = _run_mono(_mono_rig(params, stop=False, extra=True), x)
        lags = _lags(a, b)
        assert np.max(np.abs(lags)) >= 20, f"two tapes never parted: {sorted(set(lags))}"


class TestOneStereoTape:
    def test_same_signal_both_sides_is_one_output_and_the_mono_tape(self):
        x = _noise(3.5, 2)
        g = _gate(3.5)
        y = _run_stereo(_stereo_rig(ALL_ON), x, x, g)
        np.testing.assert_array_equal(y["out"], y["out_r"])
        np.testing.assert_array_equal(y["out"], _run_mono(_mono_rig(ALL_ON), x, g))

    def test_each_side_is_the_mono_tape_on_that_side(self):
        xl, xr = _noise(3.5, 3), _noise(3.5, 4)
        g = _gate(3.5)
        y = _run_stereo(_stereo_rig(ALL_ON), xl, xr, g)
        np.testing.assert_array_equal(y["out"], _run_mono(_mono_rig(ALL_ON), xl, g))
        np.testing.assert_array_equal(y["out_r"], _run_mono(_mono_rig(ALL_ON), xr, g))

    def test_hiss_left_is_the_mono_tracks_right_is_its_own(self):
        x = _noise(2.0, 5)
        hissy = {**ALL_ON, "hiss": -40.0}
        y = _run_stereo(_stereo_rig(hissy, stop=False), x, x)
        clean = _run_stereo(_stereo_rig(ALL_ON, stop=False), x, x)
        np.testing.assert_array_equal(
            y["out"], _run_mono(_mono_rig(hissy, stop=False), x))
        h_l = (y["out"] - clean["out"]).astype(np.float64)
        h_r = (y["out_r"] - clean["out_r"]).astype(np.float64)
        rms_l, rms_r = np.sqrt(np.mean(h_l ** 2)), np.sqrt(np.mean(h_r ** 2))
        assert rms_l > 1e-4 and abs(rms_r / rms_l - 1.0) < 0.1
        corr = float(np.dot(h_l, h_r) / (np.linalg.norm(h_l) * np.linalg.norm(h_r)))
        assert abs(corr) < 0.05, corr

    def test_block_size_independent_with_everything_on(self):
        xl, xr = _noise(3.0, 6), _noise(3.0, 7)
        g = _gate(3.0)
        params = {**ALL_ON, "hiss": -45.0}
        a = _run_stereo(_stereo_rig(params, block=64), xl, xr, g, block=64)
        b = _run_stereo(_stereo_rig(params, block=1000), xl, xr, g, block=1000)
        n = min(a["out"].shape[-1], b["out"].shape[-1])
        np.testing.assert_array_equal(a["out"][:n], b["out"][:n])
        np.testing.assert_array_equal(a["out_r"][:n], b["out_r"][:n])


class TestBitExactPaths:
    def test_neutral_passes_both_sides_through(self):
        xl, xr = _noise(0.5, 8), _noise(0.5, 9)
        y = _run_stereo(_stereo_rig({}, stop=False), xl, xr)
        n = y["out"].shape[-1]
        np.testing.assert_array_equal(y["out"], xl[:n])
        np.testing.assert_array_equal(y["out_r"], xr[:n])

    def test_mix_zero_is_dry_both_sides(self):
        xl, xr = _noise(0.5, 10), _noise(0.5, 11)
        y = _run_stereo(_stereo_rig({**ALL_ON, "mix": 0.0}), xl, xr, _gate(0.5))
        n = y["out"].shape[-1]
        np.testing.assert_array_equal(y["out"], xl[:n])
        np.testing.assert_array_equal(y["out_r"], xr[:n])

    def test_in_r_empty_is_the_mono_tape(self):
        x = _noise(1.0, 12)
        rig = _stereo_rig(ALL_ON, wire_right=False, stop=False)
        patch, b, tp, src_l, src_r, clk = rig
        r = b._render_tape(tp, F, {(src_l.id, "out"): x[:F]}, patch)
        assert isinstance(r, np.ndarray) and r.ndim == 1


class TestEdges:
    def test_only_in_r_patched(self):
        xr = _noise(1.5, 13)
        y = _run_stereo(_stereo_rig(ALL_ON, wire_left=False, stop=False),
                        np.zeros_like(xr), xr)
        assert not y["out"].any()
        np.testing.assert_array_equal(
            y["out_r"], _run_mono(_mono_rig(ALL_ON, stop=False), xr))

    def test_voice_left_mono_right(self):
        xl, xr = _noise(1.5, 14), _noise(1.5, 15)
        L = np.stack([xl, 0.5 * xr]).astype(np.float32)
        y = _run_stereo(_stereo_rig(ALL_ON, stop=False), L, xr)
        assert y["out"].shape[0] == 2 and y["out_r"].ndim == 1
        np.testing.assert_array_equal(y["out"], _run_mono(_mono_rig(ALL_ON, stop=False), L))
        np.testing.assert_array_equal(
            y["out_r"], _run_mono(_mono_rig(ALL_ON, stop=False), xr))

    def test_patching_and_pulling_in_r(self):
        xl, xr = _noise(1.0, 16), _noise(1.0, 17)
        patch, b, tp, src_l, src_r, clk = _stereo_rig(ALL_ON, wire_right=False, stop=False)

        def block(k):
            sl = slice(k * F, (k + 1) * F)
            return b._render_tape(
                tp, F, {(src_l.id, "out"): xl[sl], (src_r.id, "out"): xr[sl]}, patch)

        for k in range(5):
            assert isinstance(block(k), np.ndarray)
        patch.connect(src_r.id, "out", tp.id, "in_r")
        for k in range(5, 15):
            r = block(k)
            assert np.all(np.isfinite(r["out"])) and np.all(np.isfinite(r["out_r"]))
        assert b._state[tp.id]["buf"].shape[0] == 2
        assert patch.disconnect(src_r.id, "out", tp.id, "in_r")
        for k in range(15, 20):
            r = block(k)
            assert isinstance(r, np.ndarray) and np.all(np.isfinite(r))
        assert b._state[tp.id]["buf"].shape[0] == 1

    def test_nothing_patched_is_mono_silence(self):
        # Nothing on in_r is mono mode, so this is the mono tape's silence.
        rig = _stereo_rig(ALL_ON, wire_left=False, wire_right=False, stop=False)
        patch, b, tp, *_ = rig
        r = b._render_tape(tp, 256, {}, patch)
        assert isinstance(r, np.ndarray) and r.shape == (256,) and not r.any()

    def test_in_r_patched_but_silent_upstream_is_stereo_silence(self):
        # in_r cabled to a source that publishes nothing this block.
        rig = _stereo_rig(ALL_ON, wire_left=False, stop=False)
        patch, b, tp, *_ = rig
        r = b._render_tape(tp, 256, {}, patch)
        assert r["out"].shape == (256,) and not r["out"].any() and not r["out_r"].any()


def test_through_render_block_into_a_stereo_speaker():
    patch = Patch()
    osc_l = patch.add_module("oscillator", params={"waveform": "sine", "freq": 220.0, "amp": 0.4})
    osc_r = patch.add_module("oscillator", params={"waveform": "sine", "freq": 330.0, "amp": 0.4})
    tp = patch.add_module("tape", params={"wow": 0.3, "flutter": 0.3, "drift": 0.5, "sat": 0.3})
    spk = patch.add_module("stereo_speaker_output")
    patch.connect(osc_l.id, "out", tp.id, "in")
    patch.connect(osc_r.id, "out", tp.id, "in_r")
    patch.connect(tp.id, "out", spk.id, "in_l")
    patch.connect(tp.id, "out_r", spk.id, "in_r")
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    y = np.concatenate([b.render_block(F) for _ in range(120)])[30 * F:]
    assert np.all(np.isfinite(y))
    for ch, hz in ((0, 220.0), (1, 330.0)):
        seg = y[:, ch].astype(np.float64)
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        peak = np.fft.rfftfreq(len(seg), 1.0 / SR)[spec.argmax()]
        assert abs(peak - hz) < 6.0, (ch, peak)
