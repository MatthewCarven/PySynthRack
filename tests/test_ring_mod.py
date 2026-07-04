"""Tests for the RingMod module (in x carrier cross-multiplication).

Coverage:
  - Model: registration/defaults, ports & signal kinds (in/carrier/
    freq_cv -> out), category, JSON round-trip, unknown param rejected,
    signal-kind walls.
  - Bypass & identity: disconnected input -> silence; mix=0 is a
    bit-exact dry passthrough (and advances no carrier phase).
  - DSP: the internal carrier is a sine at ``freq`` starting at phase 0;
    out == in x carrier for an external carrier; ring modulation keeps
    the sum/difference bands and suppresses the input's own partials.
  - Controls: ``freq`` sets the internal carrier pitch; ``freq_cv`` shifts
    it 1 V/oct scaled by ``freq_cv_depth`` (and depth 0 disables it); a
    patched ``carrier`` overrides the internal sine (``freq`` ignored);
    ``mix`` blends dry against the modulated signal.
  - Invariants: a single voice row is bit-identical to mono; voices are
    independent; (V, F) shape preserved; block-size independent (dry and
    external-carrier bit-exact, internal sine to <1e-6); extremes finite.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.ring_mod import RingMod

SR, F = 44100, 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(**params):
    """oscillator -> ring_mod.in"""
    patch = Patch()
    src = patch.add_module("oscillator")
    rm = patch.add_module("ring_mod", params=params)
    patch.connect(src.id, "out", rm.id, "in")
    b = _backend()
    b.compile(patch)
    return patch, src, rm, b


def _rig_carrier(**params):
    """oscillator -> ring_mod.in ; oscillator -> ring_mod.carrier"""
    patch = Patch()
    src = patch.add_module("oscillator")
    car = patch.add_module("oscillator")
    rm = patch.add_module("ring_mod", params=params)
    patch.connect(src.id, "out", rm.id, "in")
    patch.connect(car.id, "out", rm.id, "carrier")
    b = _backend()
    b.compile(patch)
    return patch, src, car, rm, b


def _rig_cv(**params):
    """oscillator -> ring_mod.in ; constant -> ring_mod.freq_cv"""
    patch = Patch()
    src = patch.add_module("oscillator")
    cv = patch.add_module("constant")
    rm = patch.add_module("ring_mod", params=params)
    patch.connect(src.id, "out", rm.id, "in")
    patch.connect(cv.id, "out", rm.id, "freq_cv")
    b = _backend()
    b.compile(patch)
    return patch, src, cv, rm, b


def _run(b, patch, rm, src, sig, block=F, carrier=None, car=None, cv=None, cvsrc=None):
    """Render ``sig`` through ``rm`` in ``block`` chunks (bare-array out)."""
    outs = []
    n = sig.shape[-1]
    for i in range(0, n, block):
        blk = sig[..., i:i + block]
        bufs = {(src.id, "out"): blk}
        if carrier is not None:
            bufs[(car.id, "out")] = carrier[..., i:i + block]
        if cv is not None:
            bufs[(cvsrc.id, "out")] = cv[..., i:i + block]
        outs.append(b._render_ring_mod(rm, blk.shape[-1], bufs, patch))
    return np.concatenate(outs, axis=-1)


def _sine(freq, amp=0.5, n=F * 16):
    t = np.arange(n) / SR
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _spectrum(x, n=8192):
    seg = x[-n:]
    return np.abs(np.fft.rfft(seg * np.hanning(n)))


def _mag(sp, freq, n=8192):
    return sp[round(freq * n / SR)]


def _peak_hz(x, n=8192):
    sp = _spectrum(x, n)
    return np.argmax(sp) * SR / n


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        rm = Patch().add_module("ring_mod")
        assert isinstance(rm, RingMod)
        assert rm.params == {"freq": 440.0, "freq_cv_depth": 1.0, "mix": 1.0}

    def test_category(self):
        assert RingMod.CATEGORY == "Effects"

    def test_ports_and_signal_kinds(self):
        rm = Patch().add_module("ring_mod")
        assert [(p.name, p.signal_kind) for p in rm.input_ports] == [
            ("in", "audio"), ("carrier", "audio"), ("freq_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in rm.output_ports] == [
            ("out", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("ring_mod", params={"freq": 123.0, "mix": 0.25})
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == "ring_mod")
        assert mod.params["freq"] == 123.0
        assert mod.params["mix"] == 0.25

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("ring_mod", params={"depth": 1.0})

    def test_signal_kind_walls(self):
        patch = Patch()
        cvmod = patch.add_module("constant")   # cv out
        osc = patch.add_module("oscillator")   # audio out
        rm = patch.add_module("ring_mod")
        with pytest.raises(Exception):
            patch.connect(cvmod.id, "out", rm.id, "in")       # cv -> audio in
        with pytest.raises(Exception):
            patch.connect(osc.id, "out", rm.id, "freq_cv")    # audio -> cv in
        # audio -> audio carrier is fine
        patch.connect(osc.id, "out", rm.id, "carrier")


# ----- Bypass & identity -----------------------------------------------------


class TestBypass:
    def test_disconnected_input_silent(self):
        patch = Patch()
        rm = patch.add_module("ring_mod")
        b = _backend()
        b.compile(patch)
        out = b._render_ring_mod(rm, F, {}, patch)
        assert np.array_equal(out, np.zeros(F, dtype=np.float32))

    def test_mix0_bit_exact_dry(self):
        x = _sine(300.0, amp=0.5, n=F * 8)
        for block in (F, 333):
            patch, src, rm, b = _rig(mix=0.0)
            out = _run(b, patch, rm, src, x, block=block)
            assert np.array_equal(out, x)

    def test_mix0_does_not_advance_carrier(self):
        # A bypassed (mix=0) render leaves no phase state, so raising mix
        # afterwards starts the carrier cleanly at phase 0.
        x = _sine(300.0, n=F * 4)
        patch, src, rm, b = _rig(mix=0.0)
        _run(b, patch, rm, src, x)
        assert rm.id not in b._state or "phase" not in b._state.get(rm.id, {})


# ----- DSP -------------------------------------------------------------------


class TestDSP:
    def test_internal_carrier_is_sine_at_freq(self):
        # Multiply a DC input by the internal carrier -> the carrier itself.
        patch, src, rm, b = _rig(mix=1.0, freq=440.0)
        ones = np.ones(F * 16, dtype=np.float32)
        out = _run(b, patch, rm, src, ones)
        assert abs(_peak_hz(out) - 440.0) < 5.0
        # Exclusive-prefix phase -> first sample sits at phase 0 exactly.
        assert out[0] == 0.0

    def test_out_equals_in_times_external_carrier(self):
        xin = _sine(300.0, amp=0.5, n=F * 4)
        car = _sine(440.0, amp=0.5, n=F * 4)
        patch, src, carmod, rm, b = _rig_carrier(mix=1.0)
        out = _run(b, patch, rm, src, xin, carrier=car, car=carmod)
        expected = (xin.astype(np.float64) * car.astype(np.float64)).astype(np.float32)
        assert np.array_equal(out, expected)

    def test_sum_and_difference_bands(self):
        # in @1200 Hz x carrier @200 Hz -> energy at 1000 & 1400, not 1200/200.
        n = 16384
        xin = _sine(1200.0, amp=0.6, n=n)
        car = _sine(200.0, amp=1.0, n=n)
        patch, src, carmod, rm, b = _rig_carrier(mix=1.0)
        out = _run(b, patch, rm, src, xin, carrier=car, car=carmod)
        sp = _spectrum(out)
        assert _mag(sp, 1000.0) > 20 * _mag(sp, 1200.0)
        assert _mag(sp, 1400.0) > 20 * _mag(sp, 1200.0)
        assert _mag(sp, 1000.0) > 20 * _mag(sp, 200.0)


# ----- Controls --------------------------------------------------------------


class TestControls:
    def test_freq_sets_carrier_pitch(self):
        ones = np.ones(F * 16, dtype=np.float32)
        p1, s1, r1, b1 = _rig(mix=1.0, freq=200.0)
        p2, s2, r2, b2 = _rig(mix=1.0, freq=800.0)
        assert abs(_peak_hz(_run(b1, p1, r1, s1, ones)) - 200.0) < 5.0
        assert abs(_peak_hz(_run(b2, p2, r2, s2, ones)) - 800.0) < 5.0

    def test_freq_cv_one_octave_up(self):
        # constant.value defaults to 0; set it to +1 -> +freq_cv_depth octaves.
        ones = np.ones(F * 16, dtype=np.float32)
        patch, src, cvmod, rm, b = _rig_cv(mix=1.0, freq=300.0, freq_cv_depth=1.0)
        cvmod.set_param("value", 1.0)
        b.compile(patch)
        cv = np.ones(F * 16, dtype=np.float32)
        out = _run(b, patch, rm, src, ones, cv=cv, cvsrc=cvmod)
        assert abs(_peak_hz(out) - 600.0) < 6.0

    def test_freq_cv_depth_zero_disables(self):
        ones = np.ones(F * 16, dtype=np.float32)
        patch, src, cvmod, rm, b = _rig_cv(mix=1.0, freq=300.0, freq_cv_depth=0.0)
        cvmod.set_param("value", 1.0)
        b.compile(patch)
        cv = np.ones(F * 16, dtype=np.float32)
        out = _run(b, patch, rm, src, ones, cv=cv, cvsrc=cvmod)
        assert abs(_peak_hz(out) - 300.0) < 6.0   # unchanged despite cv

    def test_external_carrier_overrides_freq(self):
        xin = _sine(300.0, amp=0.5, n=F * 4)
        car = _sine(440.0, amp=0.5, n=F * 4)
        pa, sa, ca, ra, ba = _rig_carrier(mix=1.0, freq=200.0)
        pb, sb, cb, rb, bb = _rig_carrier(mix=1.0, freq=2000.0)
        oa = _run(ba, pa, ra, sa, xin, carrier=car, car=ca)
        ob = _run(bb, pb, rb, sb, xin, carrier=car, car=cb)
        assert np.array_equal(oa, ob)   # freq ignored when carrier patched

    def test_mix_blends_dry_and_wet(self):
        xin = _sine(300.0, amp=0.5, n=F * 4)
        car = _sine(440.0, amp=0.5, n=F * 4)
        pw, sw, cw, rw, bw = _rig_carrier(mix=1.0)
        ph, sh, ch, rh, bh = _rig_carrier(mix=0.5)
        wet = _run(bw, pw, rw, sw, xin, carrier=car, car=cw)
        half = _run(bh, ph, rh, sh, xin, carrier=car, car=ch)
        expected = (0.5 * xin.astype(np.float64)
                    + 0.5 * wet.astype(np.float64)).astype(np.float32)
        assert np.allclose(half, expected, atol=1e-6)


# ----- Invariants ------------------------------------------------------------


class TestInvariants:
    def test_single_voice_row_equals_mono(self):
        x = _sine(250.0, amp=0.4, n=F * 4)
        pa, sa, ra, ba = _rig(mix=1.0, freq=440.0)
        mono = _run(ba, pa, ra, sa, x)
        pb, sb, rb, bb = _rig(mix=1.0, freq=440.0)
        voice = _run(bb, pb, rb, sb, x[np.newaxis, :])
        assert voice.shape[0] == 1
        assert np.array_equal(voice[0], mono)

    def test_voices_independent(self):
        x = np.stack([_sine(250.0, 0.4, F * 4), _sine(377.0, 0.4, F * 4)])
        patch, src, rm, b = _rig(mix=1.0, freq=440.0)
        out = _run(b, patch, rm, src, x.astype(np.float32))
        assert out.shape == (2, F * 4)
        # Row 0 matches an independent mono render of the same row.
        pm, sm, rmn, bm = _rig(mix=1.0, freq=440.0)
        mono0 = _run(bm, pm, rmn, sm, x[0].astype(np.float32))
        assert np.array_equal(out[0], mono0)
        assert not np.array_equal(out[0], out[1])

    def test_block_size_independence(self):
        x = _sine(250.0, amp=0.4, n=12000)
        # Dry (mix=0): exact at any block size.
        outs = []
        for block in (512, 4096, 333):
            p, s, r, bk = _rig(mix=0.0)
            outs.append(_run(bk, p, r, s, x, block=block))
        m = min(len(o) for o in outs)
        assert np.array_equal(outs[0][:m], outs[1][:m])
        assert np.array_equal(outs[0][:m], outs[2][:m])
        # Internal sine (mix=1): matches across block sizes to <1e-6.
        wets = []
        for block in (512, 4096, 333):
            p, s, r, bk = _rig(mix=1.0, freq=440.0)
            wets.append(_run(bk, p, r, s, x, block=block))
        m = min(len(o) for o in wets)
        assert np.allclose(wets[0][:m], wets[1][:m], atol=1e-6)
        assert np.allclose(wets[0][:m], wets[2][:m], atol=1e-6)

    def test_external_carrier_block_exact(self):
        xin = _sine(300.0, amp=0.5, n=8000)
        car = _sine(440.0, amp=0.5, n=8000)
        outs = []
        for block in (512, 4096, 333):
            p, s, c, r, bk = _rig_carrier(mix=1.0)
            outs.append(_run(bk, p, r, s, xin, carrier=car, car=c, block=block))
        m = min(len(o) for o in outs)
        assert np.array_equal(outs[0][:m], outs[1][:m])
        assert np.array_equal(outs[0][:m], outs[2][:m])

    def test_extremes_finite(self):
        x = (np.ones(F * 2, dtype=np.float32) * 10.0)
        patch, src, rm, b = _rig(mix=1.0, freq=5000.0)
        out = _run(b, patch, rm, src, x)
        assert np.all(np.isfinite(out))
