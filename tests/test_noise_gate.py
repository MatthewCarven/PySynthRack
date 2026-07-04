"""Tests for the NoiseGate (hold-and-hysteresis downward gate + gate CV).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in +
    sidechain; audio out + cv open), JSON round-trip, unknown-param
    rejection, signal-kind walls.
  - Neutral: threshold at its floor (-80) is a bit-exact passthrough with
    ``open`` high throughout.
  - Gating: a signal above threshold passes at unity; below it the output
    is pulled to the ``range`` floor (full mute at -80, a partial duck
    above it).
  - Hysteresis: the Schmitt gap stops a boundary-level signal chattering
    (far fewer gate transitions than with hysteresis 0).
  - Hold: after the level drops the gate stays open for the hold time, so
    the close is delayed by ~hold ms relative to hold 0.
  - open CV: 0/1 only, and it matches the audible gating (high where the
    signal passes, low where it is cut).
  - Sidechain: an external key gates ``in``; unpatched it normals to
    ``in``; a silent key holds the gate shut on a hot input.
  - Block size: one big block == many small blocks, bit-exact (every stage
    is a sample-by-sample recurrence with carried state), open included.
  - Voice: a single voice row is bit-identical to mono; voices gate
    independently.
  - Integration: osc -> gate -> speaker renders finite audio, and ``open``
    drives another module's CV.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.noise_gate import NoiseGate

SR = 44100
F = 512


def _backend(block=F):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _patch(**params):
    patch = Patch()
    osc = patch.add_module("oscillator")
    gate = patch.add_module("noise_gate", params=params or None)
    patch.connect(osc.id, "out", gate.id, "in")
    return patch, osc, gate


def _run(b, gate, patch, src_id, x, sc_id=None, sc=None, block=F):
    """Render ``x`` (1D or (V,F)) through the gate, block by block.

    Returns (out, open) concatenated along the time axis.
    """
    n = (x.shape[-1] // block) * block
    outs, opens = [], []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src_id, "out"): x[..., sl].astype(np.float32)}
        if sc_id is not None:
            bufs[(sc_id, "out")] = sc[..., sl].astype(np.float32)
        r = b._render_noise_gate(gate, block, bufs, patch)
        outs.append(r["out"])
        opens.append(r["open"])
    return np.concatenate(outs, axis=-1), np.concatenate(opens, axis=-1)


def _sine(n, freq=300.0, amp=1.0):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / SR)).astype(np.float32)


def _transitions(op):
    return int(np.abs(np.diff(op)).sum())


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        g = Patch().add_module("noise_gate")
        assert isinstance(g, NoiseGate)
        assert g.params == {
            "threshold": -45.0,
            "hysteresis": 4.0,
            "attack": 1.0,
            "hold": 40.0,
            "release": 150.0,
            "range": -80.0,
        }

    def test_ports_and_signal_kinds(self):
        g = Patch().add_module("noise_gate")
        assert [(p.name, p.signal_kind) for p in g.input_ports] == [
            ("in", "audio"),
            ("sidechain", "audio"),
        ]
        assert [(p.name, p.signal_kind) for p in g.output_ports] == [
            ("out", "audio"),
            ("open", "cv"),
        ]

    def test_category_is_effects(self):
        assert NoiseGate.CATEGORY == "Effects"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "noise_gate",
            params={"threshold": -55.0, "hold": 120.0, "range": -24.0},
        )
        restored = Patch.from_dict(patch.to_dict())
        g = next(m for m in restored if m.TYPE == "noise_gate")
        assert g.params["threshold"] == -55.0
        assert g.params["hold"] == 120.0
        assert g.params["range"] == -24.0

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("noise_gate", params={"ratio": 4.0})

    def test_signal_kind_walls(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        lfo = patch.add_module("lfo")
        gate = patch.add_module("noise_gate")
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        # audio -> in / sidechain OK
        patch.connect(osc.id, "out", gate.id, "in")
        patch.connect(osc.id, "out", gate.id, "sidechain")
        # cv -> audio in rejected (both in and sidechain are audio)
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", gate.id, "in")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", gate.id, "sidechain")
        # open is a cv out: -> cv in OK, -> audio in rejected
        patch.connect(gate.id, "open", vca.id, "cv")
        with pytest.raises(ValueError):
            patch.connect(gate.id, "open", spk.id, "in")
        # out is audio: -> cv in rejected
        with pytest.raises(ValueError):
            patch.connect(gate.id, "out", vca.id, "cv")


# ----- Neutral ---------------------------------------------------------------


class TestNeutral:
    def test_threshold_min_is_bit_exact_passthrough(self):
        patch, osc, gate = _patch(threshold=-80.0)
        b = _backend()
        b.compile(patch)
        x = np.random.randn(F).astype(np.float32)
        r = b._render_noise_gate(gate, F, {(osc.id, "out"): x}, patch)
        assert np.array_equal(r["out"], x)
        assert np.all(r["open"] == 1.0)

    def test_silence_stays_silence(self):
        patch, osc, gate = _patch(threshold=-45.0)
        b = _backend()
        b.compile(patch)
        x = np.zeros(F * 4, dtype=np.float32)
        out, _ = _run(b, gate, patch, osc.id, x)
        assert np.array_equal(out, np.zeros_like(out))


# ----- Gating: open passes, closed ducks to range ---------------------------


class TestGating:
    def test_above_threshold_passes_at_unity(self):
        # A tone well above threshold: after the attack ramp the gate is
        # fully open, so the output equals the input.
        patch, osc, gate = _patch(threshold=-40.0, range=-80.0, attack=1.0)
        b = _backend()
        b.compile(patch)
        x = _sine(F * 40, amp=0.5)
        out, opn = _run(b, gate, patch, osc.id, x)
        tail = slice(-8 * F, None)
        # Fully open and unity gain once settled.
        assert np.all(opn[tail] == 1.0)
        np.testing.assert_allclose(out[tail], x[: len(out)][tail], atol=1e-6)

    def test_full_mute_floor(self):
        # in steady, keyed by a sidechain that goes silent -> the closed
        # gate at range -80 pulls the output to zero.
        n = F * 80
        steady = _sine(n, amp=0.5)
        key = np.zeros(n, dtype=np.float32)
        key[: F * 20] = _sine(n, amp=0.5)[: F * 20]  # opens, then silent
        patch = Patch()
        insrc = patch.add_module("oscillator")
        ksrc = patch.add_module("oscillator")
        gate = patch.add_module(
            "noise_gate",
            params={"threshold": -30.0, "range": -80.0, "release": 60.0,
                    "hold": 0.0},
        )
        patch.connect(insrc.id, "out", gate.id, "in")
        patch.connect(ksrc.id, "out", gate.id, "sidechain")
        b = _backend()
        b.compile(patch)
        out, opn = _run(b, gate, patch, insrc.id, steady, sc_id=ksrc.id, sc=key)
        closed_tail = slice(-8 * F, None)
        assert np.all(opn[closed_tail] == 0.0)
        assert np.max(np.abs(out[closed_tail])) < 1e-4       # muted

    def test_range_partial_duck(self):
        # range -12 dB: a closed gate ducks the (still-present) input to
        # ~0.251x rather than silencing it (expander-style).
        n = F * 120
        floor = 10 ** (-12 / 20)
        steady = _sine(n, amp=0.5)
        key = np.zeros(n, dtype=np.float32)
        key[: F * 20] = _sine(n, amp=0.5)[: F * 20]
        patch = Patch()
        insrc = patch.add_module("oscillator")
        ksrc = patch.add_module("oscillator")
        gate = patch.add_module(
            "noise_gate",
            params={"threshold": -30.0, "range": -12.0, "release": 60.0,
                    "hold": 0.0},
        )
        patch.connect(insrc.id, "out", gate.id, "in")
        patch.connect(ksrc.id, "out", gate.id, "sidechain")
        b = _backend()
        b.compile(patch)
        out, opn = _run(b, gate, patch, insrc.id, steady, sc_id=ksrc.id, sc=key)
        open_reg = slice(F * 5, F * 15)
        closed_reg = slice(-8 * F, None)
        rms_open = np.sqrt(np.mean(out[open_reg] ** 2))
        rms_closed = np.sqrt(np.mean(out[closed_reg] ** 2))
        assert np.all(opn[closed_reg] == 0.0)
        assert rms_closed == pytest.approx(rms_open * floor, rel=0.05)


# ----- Hysteresis: no chatter -----------------------------------------------


class TestHysteresis:
    def test_hysteresis_kills_boundary_chatter(self):
        # A low sine whose follower envelope ripples across the threshold
        # every cycle. With hold disabled, hysteresis is the only thing
        # holding the gate steady.
        x = _sine(F * 80, freq=40.0, amp=0.5)

        p0, o0, g0 = _patch(threshold=-9.0, hysteresis=0.0, hold=0.0)
        b0 = _backend(); b0.compile(p0)
        _, op_none = _run(b0, g0, p0, o0.id, x)

        p1, o1, g1 = _patch(threshold=-9.0, hysteresis=12.0, hold=0.0)
        b1 = _backend(); b1.compile(p1)
        _, op_hyst = _run(b1, g1, p1, o1.id, x)

        # No-hysteresis chatters hard; a wide Schmitt band settles it.
        assert _transitions(op_none) > 20
        assert _transitions(op_hyst) <= 2
        assert _transitions(op_hyst) < _transitions(op_none)

    def test_hold_also_bridges_dips(self):
        # With hysteresis 0 but a generous hold, the same boundary signal
        # is held open through its sub-hold dips (hold is the other
        # anti-chatter mechanism).
        x = _sine(F * 80, freq=40.0, amp=0.5)
        patch, osc, gate = _patch(threshold=-9.0, hysteresis=0.0, hold=60.0)
        b = _backend(); b.compile(patch)
        _, opn = _run(b, gate, patch, osc.id, x)
        assert _transitions(opn) <= 2


# ----- Hold ------------------------------------------------------------------


class TestHold:
    def _close_index(self, hold_ms):
        burst_end = F * 10
        n = F * 60
        key = np.zeros(n, dtype=np.float32)
        key[:burst_end] = _sine(n, amp=0.5)[:burst_end]  # burst then silence
        patch, osc, gate = _patch(
            threshold=-30.0, hysteresis=4.0, hold=hold_ms, release=150.0
        )
        b = _backend(); b.compile(patch)
        _, opn = _run(b, gate, patch, osc.id, key)
        after = opn[burst_end:]
        return burst_end + int(np.argmax(after == 0.0))

    def test_hold_delays_close_by_its_time(self):
        c0 = self._close_index(0.0)
        c300 = self._close_index(300.0)
        expected = 0.3 * SR
        # The detector-decay lag before the level crosses the close
        # threshold is identical in both runs, so their difference isolates
        # the hold timer.
        assert abs((c300 - c0) - expected) < 0.05 * expected

    def test_zero_hold_closes_promptly(self):
        # With no hold the gate closes as soon as the detector falls below
        # the close threshold (a few detector time-constants, not seconds).
        c0 = self._close_index(0.0)
        assert (c0 - F * 10) < 0.1 * SR


# ----- open CV ---------------------------------------------------------------


class TestOpenCV:
    def test_open_is_binary(self):
        x = _sine(F * 40, amp=0.5)
        x[F * 15:F * 25] = 0.0
        patch, osc, gate = _patch(threshold=-30.0)
        b = _backend(); b.compile(patch)
        _, opn = _run(b, gate, patch, osc.id, x)
        assert set(np.unique(opn)).issubset({0.0, 1.0})

    def test_open_matches_audible_gating(self):
        # Burst then silence, full mute: where open is high the signal
        # passes (unity), where it is low the output is cut.
        n = F * 60
        x = np.zeros(n, dtype=np.float32)
        x[: F * 20] = _sine(n, amp=0.5)[: F * 20]
        patch, osc, gate = _patch(
            threshold=-30.0, range=-80.0, attack=1.0, release=60.0, hold=0.0
        )
        b = _backend(); b.compile(patch)
        out, opn = _run(b, gate, patch, osc.id, x)
        open_reg = slice(F * 3, F * 15)      # settled open
        shut_reg = slice(-6 * F, None)       # settled closed
        assert np.all(opn[open_reg] == 1.0)
        assert np.all(opn[shut_reg] == 0.0)
        np.testing.assert_allclose(out[open_reg], x[open_reg], atol=1e-6)
        assert np.max(np.abs(out[shut_reg])) < 1e-4


# ----- Sidechain -------------------------------------------------------------


class TestSidechain:
    def test_external_key_gates_input(self):
        # A steady pad (its own level above threshold) is chopped by a
        # rhythmic key: it only passes while the key is loud.
        n = F * 80
        pad = _sine(n, freq=220.0, amp=0.5)
        key = np.full(n, 1e-4, dtype=np.float32)
        key[F * 20:F * 40] = _sine(n, amp=0.6)[F * 20:F * 40]  # loud middle
        patch = Patch()
        padsrc = patch.add_module("oscillator")
        ksrc = patch.add_module("oscillator")
        gate = patch.add_module(
            "noise_gate",
            params={"threshold": -20.0, "range": -80.0, "attack": 1.0,
                    "release": 40.0, "hold": 10.0},
        )
        patch.connect(padsrc.id, "out", gate.id, "in")
        patch.connect(ksrc.id, "out", gate.id, "sidechain")
        b = _backend(); b.compile(patch)
        out, opn = _run(b, gate, patch, padsrc.id, pad, sc_id=ksrc.id, sc=key)
        during = slice(F * 25, F * 38)
        before = slice(F * 5, F * 15)
        assert np.all(opn[during] == 1.0)                 # open on the key
        assert np.mean(opn[before]) == 0.0                # shut off the key
        assert np.sqrt(np.mean(out[during] ** 2)) > 0.3   # pad passes
        assert np.max(np.abs(out[before])) < 1e-4         # pad muted

    def test_sidechain_normals_to_input(self):
        # No key patched -> the detector keys off ``in``, so a hot input
        # opens its own gate.
        patch, osc, gate = _patch(threshold=-30.0, range=-80.0)
        b = _backend(); b.compile(patch)
        x = _sine(F * 40, amp=0.5)
        out, opn = _run(b, gate, patch, osc.id, x)
        tail = slice(-8 * F, None)
        assert np.all(opn[tail] == 1.0)
        np.testing.assert_allclose(out[tail], x[: len(out)][tail], atol=1e-6)

    def test_silent_key_holds_gate_shut(self):
        # A hot ``in`` but a silent external key -> the gate never opens
        # (proves the detector switched from ``in`` to the sidechain).
        patch = Patch()
        osc = patch.add_module("oscillator")
        ksrc = patch.add_module("oscillator")
        gate = patch.add_module(
            "noise_gate", params={"threshold": -30.0, "range": -80.0}
        )
        patch.connect(osc.id, "out", gate.id, "in")
        patch.connect(ksrc.id, "out", gate.id, "sidechain")
        b = _backend(); b.compile(patch)
        x = _sine(F * 20, amp=0.7)
        silent = np.zeros(F * 20, dtype=np.float32)
        out, opn = _run(b, gate, patch, osc.id, x, sc_id=ksrc.id, sc=silent)
        assert np.all(opn == 0.0)
        assert np.max(np.abs(out)) < 1e-4


# ----- Block-size independence (bit-exact) ----------------------------------


class TestBlockSize:
    def test_big_block_equals_small_blocks_bit_exact(self):
        # A varying-amplitude signal so the gate opens, holds and closes.
        n = F * 40
        env = np.concatenate([
            np.linspace(0.0, 0.6, n // 4),
            np.full(n // 4, 0.6),
            np.linspace(0.6, 0.0, n // 4),
            np.zeros(n - 3 * (n // 4)),
        ]).astype(np.float32)
        x = (_sine(n, amp=1.0) * env).astype(np.float32)
        params = dict(threshold=-35.0, hysteresis=5.0, attack=2.0,
                      hold=30.0, release=120.0, range=-80.0)

        pa, oa, ga = _patch(**params)
        ba = _backend(block=n); ba.compile(pa)
        big_out, big_op = _run(ba, ga, pa, oa.id, x, block=n)

        pb, ob, gb = _patch(**params)
        bb = _backend(block=128); bb.compile(pb)
        small_out, small_op = _run(bb, gb, pb, ob.id, x, block=128)

        m = min(big_out.shape[-1], small_out.shape[-1])
        # Every stage is a sample-by-sample recurrence with carried state,
        # so there is no reassociation: bit-exact, not just close.
        assert np.array_equal(big_out[:m], small_out[:m])
        assert np.array_equal(big_op[:m], small_op[:m])


# ----- Voice -----------------------------------------------------------------


class TestVoice:
    def test_single_voice_row_matches_mono(self):
        params = dict(threshold=-35.0, hysteresis=5.0, hold=20.0)
        x = (np.random.randn(F) * 0.3).astype(np.float32)

        pm, om, gm = _patch(**params)
        bm = _backend(); bm.compile(pm)
        rm = bm._render_noise_gate(gm, F, {(om.id, "out"): x}, pm)

        pv, ov, gv = _patch(**params)
        bv = _backend(); bv.compile(pv)
        stereo = np.stack([x, x]).astype(np.float32)
        rv = bv._render_noise_gate(gv, F, {(ov.id, "out"): stereo}, pv)

        assert rv["out"].shape == (2, F)
        assert np.array_equal(rv["out"][0], rm["out"])
        assert np.array_equal(rv["open"][0], rm["open"])
        assert np.array_equal(rv["out"][0], rv["out"][1])

    def test_voices_gate_independently(self):
        # Loud voice opens and passes; quiet voice stays shut and is muted
        # (sidechain normalled, so each voice keys off its own signal).
        n = F * 40
        loud = _sine(n, amp=0.5)
        quiet = _sine(n, amp=0.001)     # ~-60 dB, below threshold
        x = np.stack([loud, quiet]).astype(np.float32)
        patch, osc, gate = _patch(threshold=-30.0, range=-80.0)
        b = _backend(); b.compile(patch)
        out, opn = _run(b, gate, patch, osc.id, x)
        tail = slice(-8 * F, None)
        assert np.all(opn[0, tail] == 1.0)                 # loud: open
        assert np.all(opn[1, tail] == 0.0)                 # quiet: shut
        assert np.max(np.abs(out[1, tail])) < 1e-4         # quiet muted
        assert np.sqrt(np.mean(out[0, tail] ** 2)) > 0.3   # loud passes


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_gate_speaker_renders_finite(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"amp": 0.6})
        gate = patch.add_module("noise_gate", params={"threshold": -40.0})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", gate.id, "in")
        patch.connect(gate.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(20):
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0

    def test_open_drives_vca(self):
        # open (cv) -> vca.cv gates a second signal in lock-step: a crude
        # audio-triggered VCA. Just needs to render finite and non-silent.
        patch = Patch()
        keysrc = patch.add_module("oscillator", params={"amp": 0.6})
        tone = patch.add_module("oscillator", params={"amp": 0.5})
        gate = patch.add_module("noise_gate", params={"threshold": -40.0})
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        patch.connect(keysrc.id, "out", gate.id, "in")
        patch.connect(gate.id, "open", vca.id, "cv")
        patch.connect(tone.id, "out", vca.id, "audio")
        patch.connect(vca.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        peak = 0.0
        for _ in range(30):
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0
