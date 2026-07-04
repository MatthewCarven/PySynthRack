"""Tests for the Limiter (brickwall lookahead peak limiter).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in -> audio
    out), JSON round-trip, unknown-param rejection.
  - Brickwall: the output never exceeds the ceiling on impulse trains,
    0 dBFS squares, hot sines and hot noise, across several ceilings.
  - Neutral: a signal under the ceiling is a bit-exact passthrough,
    delayed by the lookahead (the resampler-unity precedent).
  - Latency: the delay equals round(lookahead_ms * sr / 1000) samples and
    is constant across block sizes.
  - Release: after a loud section ends, the gain recovers with the
    nominal one-pole release time constant.
  - Block size: one big block == many small blocks (to float round-off),
    with identical latency.
  - Voice: a single voice row is bit-identical to mono; voices limit
    independently.
  - Integration: osc -> limiter -> speaker renders finite audio and a hot
    oscillator is held under the ceiling.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.limiter import Limiter

SR = 44100
F = 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _look_samples(lookahead_ms):
    return max(int(round(lookahead_ms * 1e-3 * SR)), 1)


def _ceiling_lin(ceiling_db):
    return float(10.0 ** (ceiling_db / 20.0))


def _patch(**params):
    patch = Patch()
    osc = patch.add_module("oscillator")
    lim = patch.add_module("limiter", params=params or None)
    patch.connect(osc.id, "out", lim.id, "in")
    return patch, osc, lim


def _run(b, lim, patch, src_id, x, block=F):
    """Render ``x`` (1D or (V, F)) block by block; return the concatenation."""
    n = (x.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src_id, "out"): x[..., sl].astype(np.float32)}
        outs.append(b._run_or_render(lim, block, bufs, patch))
    return np.concatenate(outs, axis=-1)


# NumpyBackend has no public single-module render entry; the tests call the
# private renderer directly (as the compressor tests do). Give it a short
# alias so the harness reads cleanly.
NumpyBackend._run_or_render = NumpyBackend._render_limiter


def _sine(n, freq=1000.0, amp=1.0):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        lim = Patch().add_module("limiter")
        assert isinstance(lim, Limiter)
        assert lim.params == {"ceiling": -1.0, "release": 80.0, "lookahead": 5.0}

    def test_ports_and_signal_kinds(self):
        lim = Patch().add_module("limiter")
        assert [(p.name, p.signal_kind) for p in lim.input_ports] == [("in", "audio")]
        assert [(p.name, p.signal_kind) for p in lim.output_ports] == [("out", "audio")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("limiter", params={"ceiling": -6.0, "release": 250.0,
                                            "lookahead": 2.0})
        restored = Patch.from_dict(patch.to_dict())
        lim = next(m for m in restored if m.TYPE == "limiter")
        assert lim.params["ceiling"] == -6.0
        assert lim.params["release"] == 250.0
        assert lim.params["lookahead"] == 2.0

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("limiter", params={"threshold": -6.0})

    def test_category_is_effects(self):
        assert Limiter.CATEGORY == "Effects"


# ----- Brickwall: never exceeds the ceiling ---------------------------------


class TestBrickwall:
    @pytest.mark.parametrize("ceiling_db", [0.0, -1.0, -6.0, -12.0])
    def test_impulse_train_never_exceeds(self, ceiling_db):
        patch, osc, lim = _patch(ceiling=ceiling_db)
        b = _backend()
        b.compile(patch)
        x = np.zeros(F * 40, dtype=np.float32)
        x[1000::2000] = 1.0        # +full-scale impulses
        x[1500::2000] = -1.0       # -full-scale impulses
        out = _run(b, lim, patch, osc.id, x)
        C = _ceiling_lin(ceiling_db)
        assert np.max(np.abs(out)) <= C + 1e-6

    def test_zero_dbfs_square_never_exceeds(self):
        patch, osc, lim = _patch(ceiling=-1.0)
        b = _backend()
        b.compile(patch)
        n = F * 40
        x = np.where(np.arange(n) % 200 < 100, 1.0, -1.0).astype(np.float32)
        out = _run(b, lim, patch, osc.id, x)
        assert np.max(np.abs(out)) <= _ceiling_lin(-1.0) + 1e-6

    def test_hot_sine_never_exceeds(self):
        patch, osc, lim = _patch(ceiling=-3.0)
        b = _backend()
        b.compile(patch)
        x = _sine(F * 40, freq=220.0, amp=4.0)   # +12 dBFS drive
        out = _run(b, lim, patch, osc.id, x)
        assert np.max(np.abs(out)) <= _ceiling_lin(-3.0) + 1e-6

    def test_hot_noise_never_exceeds(self):
        patch, osc, lim = _patch(ceiling=-1.0, lookahead=3.0)
        b = _backend()
        b.compile(patch)
        rng = np.random.default_rng(0)
        x = (rng.standard_normal(F * 40) * 3.0).astype(np.float32)
        out = _run(b, lim, patch, osc.id, x)
        assert np.max(np.abs(out)) <= _ceiling_lin(-1.0) + 1e-6

    def test_small_blocks_also_never_exceed(self):
        # The guarantee must survive the block-boundary state carry.
        patch, osc, lim = _patch(ceiling=-1.0)
        b = _backend(block=97)
        b.compile(patch)
        x = _sine(97 * 200, freq=330.0, amp=5.0)
        out = _run(b, lim, patch, osc.id, x, block=97)
        assert np.max(np.abs(out)) <= _ceiling_lin(-1.0) + 1e-6


# ----- Neutral: bit-exact delayed passthrough -------------------------------


class TestNeutral:
    def test_under_ceiling_is_bit_exact_delayed_passthrough(self):
        patch, osc, lim = _patch(ceiling=-1.0, lookahead=5.0)
        b = _backend()
        b.compile(patch)
        L = _look_samples(5.0)
        # A tone comfortably under the ceiling (peak 0.5 < ~0.891).
        x = _sine(F * 20, freq=440.0, amp=0.5)
        out = _run(b, lim, patch, osc.id, x)
        expected = np.concatenate([np.zeros(L, np.float32), x])[: out.shape[-1]]
        assert np.array_equal(out, expected)

    def test_silence_stays_silence(self):
        patch, osc, lim = _patch()
        b = _backend()
        b.compile(patch)
        x = np.zeros(F * 4, dtype=np.float32)
        out = _run(b, lim, patch, osc.id, x)
        assert np.array_equal(out, np.zeros_like(out))


# ----- Latency ---------------------------------------------------------------


class TestLatency:
    @pytest.mark.parametrize("lookahead_ms", [1.0, 5.0, 10.0])
    def test_delay_equals_lookahead_samples(self, lookahead_ms):
        patch, osc, lim = _patch(lookahead=lookahead_ms)
        b = _backend()
        b.compile(patch)
        L = _look_samples(lookahead_ms)
        # Sub-ceiling impulse -> passes through undistorted, purely delayed.
        x = np.zeros(F * 8, dtype=np.float32)
        x[2000] = 0.5
        out = _run(b, lim, patch, osc.id, x)
        assert int(np.argmax(np.abs(out))) == 2000 + L

    def test_latency_constant_across_block_sizes(self):
        x = np.zeros(4096 * 4, dtype=np.float32)
        x[5000] = 0.5
        L = _look_samples(5.0)
        peaks = []
        for block in (512, 256, 4096, 333):
            patch, osc, lim = _patch(lookahead=5.0)
            b = _backend(block=block)
            b.compile(patch)
            out = _run(b, lim, patch, osc.id, x, block=block)
            peaks.append(int(np.argmax(np.abs(out))))
        assert peaks == [5000 + L] * len(peaks)


# ----- Release ---------------------------------------------------------------


class TestRelease:
    def test_recovery_time_matches_release(self):
        release_ms = 200.0
        patch, osc, lim = _patch(ceiling=-6.0, release=release_ms, lookahead=5.0)
        b = _backend()
        b.compile(patch)
        L = _look_samples(5.0)
        C = _ceiling_lin(-6.0)
        N = 40000
        # DC-style step: hard above the ceiling, then well under it. |x| is
        # constant in each half so the gain settles, then releases cleanly.
        x = np.concatenate([np.full(N, 1.0, np.float32),
                            np.full(N, 0.2, np.float32)])
        out = _run(b, lim, patch, osc.id, x)
        xd = np.concatenate([np.zeros(L, np.float32), x])[: out.shape[-1]]
        # Recovery region begins where the low content reaches the output.
        start = N + L
        gain = out[start:2 * N] / xd[start:2 * N]
        red = 1.0 - gain
        red0 = red[0]
        assert red0 > 0.3            # was clamped down to the ceiling
        # First sample where the reduction has decayed to 1/e of its start.
        idx = int(np.argmax(red <= red0 / np.e))
        rel_samples = release_ms * 1e-3 * SR
        assert 0.7 * rel_samples <= idx <= 1.4 * rel_samples

    def test_longer_release_recovers_slower(self):
        def recovery_idx(release_ms):
            patch, osc, lim = _patch(ceiling=-6.0, release=release_ms)
            b = _backend()
            b.compile(patch)
            L = _look_samples(5.0)
            N = 60000
            x = np.concatenate([np.full(N, 1.0, np.float32),
                                np.full(N, 0.2, np.float32)])
            out = _run(b, lim, patch, osc.id, x)
            xd = np.concatenate([np.zeros(L, np.float32), x])[: out.shape[-1]]
            start = N + L
            red = 1.0 - out[start:2 * N] / xd[start:2 * N]
            return int(np.argmax(red <= red[0] / np.e))
        assert recovery_idx(500.0) > 2 * recovery_idx(100.0)


# ----- Block-size independence ----------------------------------------------


class TestBlockSize:
    def test_big_block_matches_small_blocks(self):
        x = _sine(F * 30, freq=277.0, amp=3.0).astype(np.float32)

        patch1, osc1, lim1 = _patch(ceiling=-1.0)
        b1 = _backend(block=F * 30)
        b1.compile(patch1)
        big = _run(b1, lim1, patch1, osc1.id, x, block=F * 30)

        patch2, osc2, lim2 = _patch(ceiling=-1.0)
        b2 = _backend(block=128)
        b2.compile(patch2)
        small = _run(b2, lim2, patch2, osc2.id, x, block=128)

        n = min(big.shape[-1], small.shape[-1])
        # Latency identical; signal identical to float round-off (the
        # anticipation min-scan reassociates the +i/look term).
        np.testing.assert_allclose(big[:n], small[:n], atol=1e-6)


# ----- Voice-awareness -------------------------------------------------------


class TestVoice:
    def test_single_voice_row_bit_identical_to_mono(self):
        x = _sine(F * 10, freq=200.0, amp=3.0)

        pm, om, lm = _patch(ceiling=-2.0)
        bm = _backend(); bm.compile(pm)
        mono = _run(bm, lm, pm, om.id, x)

        pv, ov, lv = _patch(ceiling=-2.0)
        bv = _backend(); bv.compile(pv)
        xv = x[np.newaxis, :]            # (1, T) voice-aware
        voice = _run(bv, lv, pv, ov.id, xv)
        assert np.array_equal(voice[0], mono)

    def test_voices_limit_independently(self):
        T = F * 10
        loud = _sine(T, freq=200.0, amp=4.0)     # needs heavy limiting
        quiet = _sine(T, freq=200.0, amp=0.3)    # under the ceiling
        xv = np.stack([loud, quiet]).astype(np.float32)
        patch, osc, lim = _patch(ceiling=-1.0, lookahead=5.0)
        b = _backend(); b.compile(patch)
        out = _run(b, lim, patch, osc.id, xv)
        C = _ceiling_lin(-1.0)
        L = _look_samples(5.0)
        assert np.max(np.abs(out[0])) <= C + 1e-6          # loud row limited
        # Quiet row untouched: bit-exact delayed passthrough (no cross-duck).
        expected_q = np.concatenate([np.zeros(L, np.float32), quiet])[: out.shape[-1]]
        assert np.array_equal(out[1], expected_q)


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_limiter_speaker_renders_finite(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"amp": 3.0})
        lim = patch.add_module("limiter", params={"ceiling": -1.0})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", lim.id, "in")
        patch.connect(lim.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        out = b.render_block(F)
        assert out is not None
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) <= _ceiling_lin(-1.0) + 1e-6
