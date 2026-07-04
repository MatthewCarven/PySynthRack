"""Tests for the TransientShaper (threshold-free attack/sustain rebalance).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in -> audio
    out), JSON round-trip, unknown-param rejection, signal-kind walls,
    CATEGORY.
  - Neutral: attack == sustain == 0 is a bit-exact passthrough (the
    follower pair is skipped) for any speed / voice count.
  - Separation: on a synthetic click + decay tail, ``attack`` moves the
    click energy while leaving the tail exactly alone, and ``sustain``
    moves the tail while leaving the click exactly alone (the two act on
    disjoint regions of the follower-difference sign).
  - Level invariance (the "threshold-free" trick): the same input 20 dB
    quieter is shaped identically -- the output just scales with the
    input, because the control signal is a dB *difference* (a ratio).
  - Steady state: a held constant-amplitude tone is left ~untouched once
    the followers converge (difference -> 0).
  - Block size: one big block == many small blocks to float round-off
    (the shared follower's reassociated solve, like the compressor).
  - Voice: a single voice row is bit-identical to mono; voices shape
    independently with no cross-talk.
  - Robustness / integration: silence stays silence, DC and speeds render
    finite, osc -> shaper -> speaker renders finite audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.transient_shaper import TransientShaper

SR = 44100
F = 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _patch(**params):
    patch = Patch()
    osc = patch.add_module("oscillator")
    ts = patch.add_module("transient_shaper", params=params or None)
    patch.connect(osc.id, "out", ts.id, "in")
    return patch, osc, ts


def _run(b, ts, patch, src_id, x, block=F):
    """Render ``x`` (1D or (V, F)) through the shaper block by block."""
    n = (x.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src_id, "out"): x[..., sl].astype(np.float32)}
        outs.append(b._render_transient_shaper(ts, block, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _sine(n, freq=2000.0, amp=1.0):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


def _click_tail(n, amp=0.5, freq=2000.0, rise_ms=1.0, decay_ms=40.0):
    """A sharp attack (fast linear rise) then an exponential decay tail.

    The onset is a transient (fast follower leads -> ``attack`` region);
    the decay is sustain (fast follower trails -> ``sustain`` region).
    """
    env = np.zeros(n)
    rise = int(rise_ms * 1e-3 * SR)
    env[:rise] = np.linspace(0.0, 1.0, rise)
    env[rise:] = np.exp(-np.arange(n - rise) / (decay_ms * 1e-3 * SR))
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / SR) * env).astype(
        np.float32
    )


def _energy(o, sl):
    return float(np.sum(o.astype(np.float64)[..., sl] ** 2))


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        ts = Patch().add_module("transient_shaper")
        assert isinstance(ts, TransientShaper)
        assert ts.params == {"attack": 0.0, "sustain": 0.0, "speed": "med"}

    def test_ports_and_signal_kinds(self):
        ts = Patch().add_module("transient_shaper")
        assert [(p.name, p.signal_kind) for p in ts.input_ports] == [
            ("in", "audio"),
        ]
        assert [(p.name, p.signal_kind) for p in ts.output_ports] == [
            ("out", "audio"),
        ]

    def test_category_is_effects(self):
        assert TransientShaper.CATEGORY == "Effects"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "transient_shaper",
            params={"attack": 0.6, "sustain": -0.4, "speed": "fast"},
        )
        restored = Patch.from_dict(patch.to_dict())
        ts = next(m for m in restored if m.TYPE == "transient_shaper")
        assert ts.params["attack"] == 0.6
        assert ts.params["sustain"] == -0.4
        assert ts.params["speed"] == "fast"

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("transient_shaper", params={"threshold": -20.0})

    def test_signal_kind_walls(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        lfo = patch.add_module("lfo")
        ts = patch.add_module("transient_shaper")
        vca = patch.add_module("vca")
        # audio -> in OK
        patch.connect(osc.id, "out", ts.id, "in")
        # cv -> audio in rejected
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", ts.id, "in")
        # out is audio: -> cv in rejected
        with pytest.raises(ValueError):
            patch.connect(ts.id, "out", vca.id, "cv")


# ----- Neutral (bit-exact passthrough) --------------------------------------


class TestNeutral:
    def test_neutral_is_bit_exact_passthrough(self):
        patch, osc, ts = _patch(attack=0.0, sustain=0.0)
        b = _backend()
        b.compile(patch)
        x = np.random.randn(F).astype(np.float32)
        out = b._render_transient_shaper(ts, F, {(osc.id, "out"): x}, patch)
        assert np.array_equal(out, x)

    def test_neutral_bit_exact_every_speed(self):
        # The short-circuit is param-only, so ``speed`` is irrelevant.
        x = np.random.randn(F * 3).astype(np.float32)
        for speed in ("fast", "med", "slow"):
            patch, osc, ts = _patch(attack=0.0, sustain=0.0, speed=speed)
            b = _backend()
            b.compile(patch)
            out = _run(b, ts, patch, osc.id, x)
            assert np.array_equal(out, x[: len(out)])

    def test_silence_stays_silence(self):
        patch, osc, ts = _patch(attack=1.0, sustain=1.0)
        b = _backend()
        b.compile(patch)
        x = np.zeros(F * 4, dtype=np.float32)
        out = _run(b, ts, patch, osc.id, x)
        assert np.array_equal(out, np.zeros_like(out))


# ----- Attack / sustain separation ------------------------------------------


class TestSeparation:
    # Click window: the onset (follower difference > 0 -> attack region).
    # Tail window: deep in the decay (difference < 0 -> sustain region),
    # chosen past the crossover so each knob's off-region is exactly unity.
    CLICK = slice(0, int(0.006 * SR))
    TAIL = slice(int(0.08 * SR), int(0.25 * SR))

    def _renders(self, x, **params):
        patch, osc, ts = _patch(**params)
        b = _backend(block=x.shape[-1])
        b.compile(patch)
        return _run(b, ts, patch, osc.id, x, block=x.shape[-1])

    def test_attack_moves_click_not_tail(self):
        x = _click_tail(F * 60)
        base_c, base_t = _energy(x[None], self.CLICK), _energy(x[None], self.TAIL)
        up = self._renders(x, attack=1.0, sustain=0.0)
        dn = self._renders(x, attack=-1.0, sustain=0.0)
        # Attack boosts / cuts the onset...
        assert _energy(up, self.CLICK) / base_c > 2.0
        assert _energy(dn, self.CLICK) / base_c < 0.7
        # ...and leaves the (sustain-region) tail exactly alone.
        assert _energy(up, self.TAIL) / base_t == pytest.approx(1.0, abs=1e-3)
        assert _energy(dn, self.TAIL) / base_t == pytest.approx(1.0, abs=1e-3)

    def test_sustain_moves_tail_not_click(self):
        x = _click_tail(F * 60)
        base_c, base_t = _energy(x[None], self.CLICK), _energy(x[None], self.TAIL)
        up = self._renders(x, attack=0.0, sustain=1.0)
        dn = self._renders(x, attack=0.0, sustain=-1.0)
        # Sustain boosts / cuts the tail...
        assert _energy(up, self.TAIL) / base_t > 2.0
        assert _energy(dn, self.TAIL) / base_t < 0.7
        # ...and leaves the (attack-region) click exactly alone.
        assert _energy(up, self.CLICK) / base_c == pytest.approx(1.0, abs=1e-3)
        assert _energy(dn, self.CLICK) / base_c == pytest.approx(1.0, abs=1e-3)

    def test_speed_changes_the_shaping(self):
        # The three speeds pick different follower pairs, so a fixed
        # attack boost lands differently -- just assert they differ and
        # all stay finite.
        x = _click_tail(F * 60)
        outs = [self._renders(x, attack=1.0, speed=s) for s in ("fast", "med", "slow")]
        assert all(np.all(np.isfinite(o)) for o in outs)
        assert not np.allclose(outs[0], outs[2])


# ----- Level invariance (threshold-free) ------------------------------------


class TestLevelInvariance:
    def test_same_shaping_20db_down(self):
        # The control signal is a dB difference (a ratio), so a quieter
        # copy is shaped identically: the output scales with the input.
        xf = _click_tail(F * 60, amp=0.5)
        xq = (xf * 0.1).astype(np.float32)  # -20 dB
        params = dict(attack=1.0, sustain=-1.0, speed="med")

        pf, of_, tf = _patch(**params)
        bf = _backend(block=xf.shape[-1]); bf.compile(pf)
        out_f = _run(bf, tf, pf, of_.id, xf, block=xf.shape[-1])

        pq, oq, tq = _patch(**params)
        bq = _backend(block=xq.shape[-1]); bq.compile(pq)
        out_q = _run(bq, tq, pq, oq.id, xq, block=xq.shape[-1])

        # Rescale the quiet render back up: it must match the loud one.
        np.testing.assert_allclose(10.0 * out_q, out_f, rtol=1e-4, atol=1e-5)

    def test_steady_tone_left_untouched(self):
        # A held constant-amplitude tone: once the followers converge the
        # difference is ~0, so the shaper is transparent (to a small
        # per-cycle ripple).
        x = _sine(F * 80, freq=2000.0, amp=0.5)
        patch, osc, ts = _patch(attack=1.0, sustain=-1.0, speed="med")
        b = _backend(); b.compile(patch)
        out = _run(b, ts, patch, osc.id, x)
        settled = slice(-8 * F, None)
        rms_in = np.sqrt(np.mean(x[: len(out)][settled].astype(np.float64) ** 2))
        rms_out = np.sqrt(np.mean(out[settled].astype(np.float64) ** 2))
        assert rms_out / rms_in == pytest.approx(1.0, abs=0.01)


# ----- Block-size independence -----------------------------------------------


class TestBlockSize:
    def test_big_block_equals_small_blocks(self):
        x = _click_tail(F * 40, amp=0.6)
        params = dict(attack=1.0, sustain=1.0, speed="med")

        pa, oa, ta = _patch(**params)
        ba = _backend(block=x.shape[-1]); ba.compile(pa)
        big = _run(ba, ta, pa, oa.id, x, block=x.shape[-1])

        pb, ob, tb = _patch(**params)
        bb = _backend(block=128); bb.compile(pb)
        small = _run(bb, tb, pb, ob.id, x, block=128)

        m = min(big.shape[-1], small.shape[-1])
        # Reassociated cumprod solve (shared follower), like the
        # compressor's gain smoother: equal to float round-off.
        np.testing.assert_allclose(big[:m], small[:m], atol=1e-6)


# ----- Voice -----------------------------------------------------------------


class TestVoice:
    def test_single_voice_row_matches_mono(self):
        params = dict(attack=0.7, sustain=-0.5, speed="fast")
        x = (np.random.randn(F) * 0.3).astype(np.float32)

        pm, om, tm = _patch(**params)
        bm = _backend(); bm.compile(pm)
        rm = bm._render_transient_shaper(tm, F, {(om.id, "out"): x}, pm)

        pv, ov, tv = _patch(**params)
        bv = _backend(); bv.compile(pv)
        stereo = np.stack([x, x]).astype(np.float32)
        rv = bv._render_transient_shaper(tv, F, {(ov.id, "out"): stereo}, pv)

        assert rv.shape == (2, F)
        assert np.array_equal(rv[0], rm)
        assert np.array_equal(rv[0], rv[1])

    def test_voices_shape_independently(self):
        # Two different signals in two voices: each row is shaped exactly
        # as if rendered alone (no cross-talk between voice states).
        params = dict(attack=1.0, sustain=-0.5, speed="med")
        a = _click_tail(F * 30, amp=0.5)
        b_sig = _sine(F * 30, freq=1500.0, amp=0.2)

        pa, oa, ta = _patch(**params)
        ba = _backend(block=a.shape[-1]); ba.compile(pa)
        solo_a = _run(ba, ta, pa, oa.id, a, block=a.shape[-1])

        pb, ob, tb = _patch(**params)
        bb = _backend(block=b_sig.shape[-1]); bb.compile(pb)
        solo_b = _run(bb, tb, pb, ob.id, b_sig, block=b_sig.shape[-1])

        pv, ov, tv = _patch(**params)
        bv = _backend(block=a.shape[-1]); bv.compile(pv)
        stereo = np.stack([a, b_sig]).astype(np.float32)
        rv = bv._render_transient_shaper(tv, a.shape[-1], {(ov.id, "out"): stereo}, pv)

        assert np.array_equal(rv[0], solo_a)
        assert np.array_equal(rv[1], solo_b)


# ----- Robustness / integration ---------------------------------------------


class TestRobustness:
    def test_dc_and_speeds_finite(self):
        for speed in ("fast", "med", "slow"):
            patch, osc, ts = _patch(attack=1.0, sustain=1.0, speed=speed)
            b = _backend(); b.compile(patch)
            x = np.full(F * 4, 0.5, dtype=np.float32)
            out = _run(b, ts, patch, osc.id, x)
            assert np.all(np.isfinite(out))

    def test_unknown_speed_falls_back(self):
        # A free-text ``speed`` the renderer doesn't know maps to "med"
        # rather than crashing.
        patch, osc, ts = _patch(attack=1.0, speed="ludicrous")
        b = _backend(); b.compile(patch)
        x = _click_tail(F * 8)
        out = _run(b, ts, patch, osc.id, x)
        assert np.all(np.isfinite(out))

    def test_osc_shaper_speaker_renders_finite(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"amp": 0.6})
        ts = patch.add_module("transient_shaper", params={"attack": 0.8})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", ts.id, "in")
        patch.connect(ts.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(20):
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0
