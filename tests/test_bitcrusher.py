"""Tests for the Bitcrusher module (bit-depth quantize + sample-rate decimation).

Coverage:
  - Model: registration/defaults, ports & signal kinds (in -> out, both
    audio), category, JSON round-trip (incl. the ``dc_filter`` bool),
    unknown param rejected, signal-kind wall on ``in``.
  - Bypass & identity: disconnected input -> silence; the neutral setting
    ``bits=24 & rate_div=1`` is a bit-exact passthrough at any mix;
    ``mix=0`` is bit-exact dry even with the crush engaged.
  - Quantize: the step is exactly ``round(x*2^(bits-1))/2^(bits-1)``;
    output lands on the quantizer grid; ``bits=1`` collapses to {-1,0,1}.
  - Decimate: the hold pattern is exactly ``in[(n//N)*N]`` and stays that
    way across block joins; each hold is a flat plateau of length N.
  - Jitter: seeded -> reproducible; wobbles the hold lengths around N;
    changes the result vs no jitter; is inert without decimation.
  - DC filter: strips a DC offset the crush introduces; off by default.
  - Controls: fewer bits -> coarser; larger rate_div -> fewer transitions;
    ``mix`` blends dry against wet.
  - Invariants: a single voice row is bit-identical to mono; voices are
    independent; (V, F) shape preserved; every path is *exactly*
    block-size independent; extremes stay finite and bounded.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.bitcrusher import Bitcrusher

SR, F = 44100, 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(**params):
    """oscillator -> bitcrusher.in"""
    patch = Patch()
    src = patch.add_module("oscillator")
    bc = patch.add_module("bitcrusher", params=params)
    patch.connect(src.id, "out", bc.id, "in")
    b = _backend()
    b.compile(patch)
    return patch, src, bc, b


def _run(b, patch, bc, src, sig, block=F):
    """Render ``sig`` through ``bc`` in ``block`` chunks (bare-array out)."""
    outs = []
    n = sig.shape[-1]
    for i in range(0, n, block):
        blk = sig[..., i:i + block]
        outs.append(b._render_bitcrusher(bc, blk.shape[-1], {(src.id, "out"): blk}, patch))
    return np.concatenate(outs, axis=-1)


def _sine(freq, amp=0.5, n=F * 8):
    t = np.arange(n) / SR
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _ramp(n=F * 4):
    # strictly increasing & distinct -> hold plateaus are unambiguous
    return (np.arange(n, dtype=np.float32) / n)


def _change_lengths(x):
    """Run lengths of the constant plateaus in a 1D signal."""
    idx = np.where(np.diff(x) != 0)[0] + 1
    bounds = np.concatenate([[0], idx, [len(x)]])
    return np.diff(bounds)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        bc = Patch().add_module("bitcrusher")
        assert isinstance(bc, Bitcrusher)
        assert bc.params == {
            "bits": 24, "rate_div": 1, "jitter": 0.0, "mix": 1.0, "dc_filter": False,
        }

    def test_category(self):
        assert Bitcrusher.CATEGORY == "Effects"

    def test_ports_and_signal_kinds(self):
        bc = Patch().add_module("bitcrusher")
        assert [(p.name, p.signal_kind) for p in bc.input_ports] == [("in", "audio")]
        assert [(p.name, p.signal_kind) for p in bc.output_ports] == [("out", "audio")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("bitcrusher", params={"bits": 8, "rate_div": 4, "dc_filter": True})
        restored = Patch.from_dict(patch.to_dict())
        mod = next(m for m in restored if m.TYPE == "bitcrusher")
        assert mod.params["bits"] == 8
        assert mod.params["rate_div"] == 4
        assert mod.params["dc_filter"] is True

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("bitcrusher", params={"depth": 1.0})

    def test_signal_kind_wall_on_in(self):
        patch = Patch()
        cvmod = patch.add_module("constant")   # cv out
        osc = patch.add_module("oscillator")   # audio out
        bc = patch.add_module("bitcrusher")
        with pytest.raises(Exception):
            patch.connect(cvmod.id, "out", bc.id, "in")   # cv -> audio in
        patch.connect(osc.id, "out", bc.id, "in")          # audio -> audio ok


# ----- Bypass & identity -----------------------------------------------------


class TestBypass:
    def test_disconnected_input_silent(self):
        patch = Patch()
        bc = patch.add_module("bitcrusher")
        b = _backend()
        b.compile(patch)
        out = b._render_bitcrusher(bc, F, {}, patch)
        assert np.array_equal(out, np.zeros(F, dtype=np.float32))

    def test_neutral_bit_exact(self):
        # bits=24 & rate_div=1 -> both crush ops skipped -> dry, at any mix.
        x = _sine(300.0, n=F * 8)
        for mix in (1.0, 0.5, 0.3):
            patch, src, bc, b = _rig(bits=24, rate_div=1, mix=mix)
            assert np.array_equal(_run(b, patch, bc, src, x), x)

    def test_mix0_bit_exact_dry(self):
        # dry passthrough even with the crush fully engaged, any block size.
        x = _sine(300.0, n=F * 8)
        for block in (F, 333):
            patch, src, bc, b = _rig(bits=3, rate_div=8, jitter=0.5, mix=0.0)
            assert np.array_equal(_run(b, patch, bc, src, x, block=block), x)


# ----- Quantize --------------------------------------------------------------


class TestQuantize:
    def test_quantization_step_exact(self):
        x = _sine(220.0, amp=0.8, n=F * 4)
        patch, src, bc, b = _rig(bits=5, mix=1.0)
        out = _run(b, patch, bc, src, x)
        lv = 2.0 ** (5 - 1)
        expected = (np.round(x.astype(np.float64) * lv) / lv).astype(np.float32)
        assert np.array_equal(out, expected)

    def test_output_on_quantizer_grid(self):
        x = _sine(330.0, amp=0.9, n=F * 4)
        patch, src, bc, b = _rig(bits=6, mix=1.0)
        out = _run(b, patch, bc, src, x).astype(np.float64)
        step = 1.0 / (2.0 ** (6 - 1))
        # every sample is an integer multiple of the quantizer step
        assert np.allclose(out / step, np.round(out / step), atol=1e-4)

    def test_bits1_sanity(self):
        x = _sine(300.0, amp=0.8, n=F * 4)
        patch, src, bc, b = _rig(bits=1, mix=1.0)
        out = _run(b, patch, bc, src, x)
        assert set(np.unique(out)).issubset({-1.0, 0.0, 1.0})

    def test_fewer_bits_is_coarser(self):
        x = _sine(440.0, amp=0.7, n=F * 4)
        p3, s3, b3c, b3 = _rig(bits=3, mix=1.0)
        p10, s10, b10c, b10 = _rig(bits=10, mix=1.0)
        err3 = np.abs(_run(b3, p3, b3c, s3, x) - x).mean()
        err10 = np.abs(_run(b10, p10, b10c, s10, x) - x).mean()
        assert err3 > err10 > 0.0


# ----- Decimate --------------------------------------------------------------


class TestDecimate:
    def test_hold_pattern_exact(self):
        r = _ramp(F)
        patch, src, bc, b = _rig(rate_div=4, mix=1.0)
        out = _run(b, patch, bc, src, r)
        assert np.array_equal(out, r[(np.arange(F) // 4) * 4])

    def test_hold_pattern_across_block_joins(self):
        r = _ramp(4000)
        single = _run(*_prime(_rig(rate_div=6, mix=1.0)), r, block=4000)
        for block in (512, 333, 101):
            split = _run(*_prime(_rig(rate_div=6, mix=1.0)), r, block=block)
            assert np.array_equal(split, single)
        # and it equals the analytic hold pattern
        assert np.array_equal(single, r[(np.arange(4000) // 6) * 6])

    def test_holds_are_flat_plateaus(self):
        r = _ramp(F)
        N = 5
        patch, src, bc, b = _rig(rate_div=N, mix=1.0)
        out = _run(b, patch, bc, src, r)
        # within each group of N samples the output is constant
        for k in range(0, F // N):
            seg = out[k * N:(k + 1) * N]
            assert np.all(seg == seg[0])

    def test_larger_rate_div_fewer_transitions(self):
        r = _ramp(F * 2)
        p2, s2, c2, b2 = _rig(rate_div=2, mix=1.0)
        p16, s16, c16, b16 = _rig(rate_div=16, mix=1.0)
        t2 = np.count_nonzero(np.diff(_run(b2, p2, c2, s2, r)))
        t16 = np.count_nonzero(np.diff(_run(b16, p16, c16, s16, r)))
        assert t2 > t16


# ----- Jitter ----------------------------------------------------------------


class TestJitter:
    def test_seeded_reproducible(self):
        x = _sine(250.0, n=8000)
        a = _run(*_prime(_rig(rate_div=5, jitter=0.7, mix=1.0)), x)
        b = _run(*_prime(_rig(rate_div=5, jitter=0.7, mix=1.0)), x)
        assert np.array_equal(a, b)

    def test_jitter_changes_result(self):
        x = _sine(250.0, n=8000)
        j = _run(*_prime(_rig(rate_div=5, jitter=0.7, mix=1.0)), x)
        n = _run(*_prime(_rig(rate_div=5, jitter=0.0, mix=1.0)), x)
        assert not np.array_equal(j, n)

    def test_jitter_wobbles_hold_lengths(self):
        r = _ramp(8000)                 # distinct samples -> plateaus == holds
        out = _run(*_prime(_rig(rate_div=8, jitter=0.8, mix=1.0)), r)
        lens = _change_lengths(out)
        assert lens.min() >= 1
        assert lens.std() > 0.0         # not perfectly periodic
        assert 4.0 < lens.mean() < 12.0  # still centred around N=8

    def test_no_jitter_holds_are_uniform(self):
        r = _ramp(8000)
        out = _run(*_prime(_rig(rate_div=8, jitter=0.0, mix=1.0)), r)
        lens = _change_lengths(out)
        # every interior plateau is exactly N=8 wide
        assert np.all(lens[1:-1] == 8)

    def test_jitter_inert_without_decimation(self):
        # rate_div=1 -> no decimation -> jitter has nothing to act on.
        x = _sine(300.0, n=F * 4)
        patch, src, bc, b = _rig(rate_div=1, jitter=0.9, bits=24, mix=1.0)
        assert np.array_equal(_run(b, patch, bc, src, x), x)


# ----- DC filter -------------------------------------------------------------


class TestDCFilter:
    def test_removes_dc_offset(self):
        # A constant input has pure DC; the blocker should decay it to ~0.
        const = np.full(20000, 0.5, dtype=np.float32)
        patch, src, bc, b = _rig(bits=24, rate_div=1, dc_filter=True, mix=1.0)
        out = _run(b, patch, bc, src, const)
        assert abs(out[-2000:].mean()) < 1e-2

    def test_off_by_default_keeps_dc(self):
        const = np.full(8000, 0.5, dtype=np.float32)
        patch, src, bc, b = _rig(bits=24, rate_div=1, mix=1.0)  # dc_filter default False
        out = _run(b, patch, bc, src, const)
        assert np.allclose(out, const)


# ----- Controls --------------------------------------------------------------


class TestControls:
    def test_mix_blends_dry_and_wet(self):
        x = _sine(300.0, amp=0.6, n=F * 4)
        wet = _run(*_prime(_rig(bits=4, rate_div=6, mix=1.0)), x)
        half = _run(*_prime(_rig(bits=4, rate_div=6, mix=0.5)), x)
        expected = (0.5 * x.astype(np.float64) + 0.5 * wet.astype(np.float64)).astype(np.float32)
        assert np.allclose(half, expected, atol=1e-6)


# ----- Invariants ------------------------------------------------------------


class TestInvariants:
    def test_single_voice_row_equals_mono(self):
        x = _sine(250.0, amp=0.5, n=F * 4)
        mono = _run(*_prime(_rig(bits=6, rate_div=5, jitter=0.4, mix=1.0)), x)
        voice = _run(*_prime(_rig(bits=6, rate_div=5, jitter=0.4, mix=1.0)), x[np.newaxis, :])
        assert voice.shape[0] == 1
        assert np.array_equal(voice[0], mono)

    def test_voices_independent(self):
        x = np.stack([_sine(250.0, 0.5, F * 4), _sine(377.0, 0.5, F * 4)]).astype(np.float32)
        patch, src, bc, b = _rig(bits=6, rate_div=5, mix=1.0)
        out = _run(b, patch, bc, src, x)
        assert out.shape == (2, F * 4)
        mono0 = _run(*_prime(_rig(bits=6, rate_div=5, mix=1.0)), x[0])
        assert np.array_equal(out[0], mono0)
        assert not np.array_equal(out[0], out[1])

    def test_block_size_independence_exact(self):
        x = _sine(250.0, amp=0.5, n=12000)
        combos = [
            dict(bits=6, rate_div=5, mix=0.8),                       # quant + decim + blend
            dict(bits=8, rate_div=7, jitter=0.7, mix=1.0),           # + jitter
            dict(bits=5, rate_div=3, jitter=0.5, dc_filter=True, mix=1.0),  # + dc filter
        ]
        for params in combos:
            ref = _run(*_prime(_rig(**params)), x, block=512)
            for block in (4096, 333, 101):
                cur = _run(*_prime(_rig(**params)), x, block=block)
                assert np.array_equal(ref, cur), f"{params} @ block {block}"

    def test_extremes_finite(self):
        x = _sine(1234.0, amp=1.0, n=8000)
        out = _run(*_prime(_rig(bits=1, rate_div=64, jitter=1.0, dc_filter=True, mix=1.0)), x)
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) < 10.0


# helper: unpack a _rig() tuple into the _run() call order (b, patch, bc, src)
def _prime(rig_tuple):
    patch, src, bc, b = rig_tuple
    return b, patch, bc, src
