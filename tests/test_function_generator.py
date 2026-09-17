"""Tests for the function generator (Maths-style rise/fall + EOR/EOC).

Coverage:
  - Model: registration, defaults, ports/signal kinds, JSON round-trip,
    unknown-param rejection, type walls (gate -> trig legal, cv -> trig
    illegal, cv-out -> audio-sink illegal).
  - Curve: the exponent map is 1 at 0, reciprocal at +-c, and the
    measured half-rise level matches the power law it claims.
  - Trigger mode: no trigger -> silence; a trigger reaches exactly 1.0
    and returns to exactly 0.0; gate LENGTH is ignored; the rise and
    fall lengths are exact to the SAMPLE (the integer-counter claim,
    checked across a block boundary so accumulation would show); output
    stays in [0, 1].
  - Gate mode: holds at 1.0 while held, falls on release, and a release
    MID-RISE falls from the level it reached (click-free, measured as a
    bounded step rather than by eye).
  - Loop mode: free-runs with nothing patched, the eoc period is exactly
    rise+fall samples with ZERO drift over many cycles (the tripwire the
    float-step version would fail), and trig is a click-free sync.
  - EOR/EOC: land on the exact sample the stage completes, are ~2 ms,
    and shrink rather than swamp a fast loop.
  - rate_cv: 1 V/oct on the rate -- +1 halves the cycle, -1 doubles it.
  - rise_cv / fall_cv (2026-09-14): the same law on one slope each; the
    OTHER slope is sample-for-sample untouched; they sum in octaves with
    rate_cv and the per-slope total clamps at +-5; unpatched they render
    array_equal to the rate_cv-only code.
  - Per-voice rates (2026-09-14, second pass): a (V, F) rise_cv / fall_cv
    against a (V, F) trigger gives each slot its own stage lengths --
    each row bit-identical to the mono path fed that row's constant; a
    0 row is untouched (velocity_cv on a parked slot); rate_cv stays one
    tempo for all; a mono trigger or a mismatched V falls back to the
    summed mono law; loop periods differ per voice and never drift.
  - out_inv (2026-09-14): 1 - out bit-exact on every sample, both shapes;
    a parked voice reads 1.0 (the ducker is open on a silent slot).
  - Voice DSP: a single-voice row is bit-identical to the mono path in
    every mode and at several curves; voices run independently; mono
    <-> voice state reinit.
  - Integration: the krell example self-plays (it must NOT be a patch
    that merely looks like one -- see the eoc -> trig finding in the
    2026-08-23 worklog entry).
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.function_generator import (
    FUNCTION_GENERATOR_MODES,
    FunctionGenerator,
)

SR = 48000


def _backend(block=512):
    return NumpyBackend(sample_rate=SR, block_size=block)


def _rig(**params):
    """schmitt -> function_generator, compiled. Returns (patch, src, fg, b)."""
    patch = Patch()
    src = patch.add_module("schmitt")
    fg = patch.add_module("function_generator", params=params)
    patch.connect(src.id, "gate", fg.id, "trig")
    b = _backend()
    b.compile(patch)
    return patch, src, fg, b


def _drive(b, patch, src, fg, gate):
    """Render one block of `gate` through the generator; returns the dict."""
    return b._render_function_generator(
        fg, gate.shape[-1], {(src.id, "gate"): gate.astype(np.float32)}, patch
    )


def _free(fg, b, patch, frames, blocks=1):
    """Render an UNPATCHED generator (loop mode) for `blocks` blocks."""
    outs = {"out": [], "out_inv": [], "eor": [], "eoc": []}
    for _ in range(blocks):
        r = b._render_function_generator(fg, frames, {}, patch)
        for key in outs:
            outs[key].append(np.asarray(r[key]).copy())
    return {k: np.concatenate(v) for k, v in outs.items()}


def _pulse(F, at=0, width=1):
    g = np.zeros(F, dtype=np.float32)
    g[at:at + width] = 1.0
    return g


def _rising(x):
    """Sample indices where a gate signal goes low -> high."""
    hi = np.asarray(x) > 0.5
    return np.flatnonzero(hi[1:] & ~hi[:-1]) + 1 + (0 if not hi[0] else 0)


def _first_high(x):
    hits = np.flatnonzero(np.asarray(x) > 0.5)
    return int(hits[0]) if hits.size else -1


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        fg = Patch().add_module("function_generator")
        assert isinstance(fg, FunctionGenerator)
        assert fg.params == {
            "mode": "trigger", "rise": 0.05, "fall": 0.5, "curve": 0.0,
            "curve_rise": 0.0, "curve_fall": 0.0,
        }

    def test_ports_and_signal_kinds(self):
        fg = Patch().add_module("function_generator")
        assert [(p.name, p.signal_kind) for p in fg.input_ports] == [
            ("trig", "gate"), ("rate_cv", "cv"), ("rise_cv", "cv"), ("fall_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in fg.output_ports] == [
            ("out", "cv"), ("out_inv", "cv"), ("eor", "gate"), ("eoc", "gate"),
        ]

    def test_mode_list_matches_what_the_renderer_accepts(self):
        # The UI combo renders FUNCTION_GENERATOR_MODES; anything outside
        # it falls back to "trigger". Pin the list so the two can't drift.
        assert FUNCTION_GENERATOR_MODES == ("trigger", "gate", "loop")

    def test_json_round_trip(self):
        from pysynthrack.io_patch import patch_from_json, patch_to_json

        patch = Patch()
        patch.add_module(
            "function_generator",
            params={"mode": "loop", "rise": 0.2, "fall": 1.5, "curve": -0.6},
        )
        back = patch_from_json(patch_to_json(patch))
        fg = next(iter(back))
        assert fg.params["mode"] == "loop"
        assert fg.params["rise"] == pytest.approx(0.2)
        assert fg.params["fall"] == pytest.approx(1.5)
        assert fg.params["curve"] == pytest.approx(-0.6)

    def test_unknown_param_rejected(self):
        with pytest.raises(Exception):
            Patch().add_module("function_generator", params={"sustain": 0.5})

    def test_type_walls(self):
        patch = Patch()
        fg = patch.add_module("function_generator")
        clock = patch.add_module("clock")
        lfo = patch.add_module("lfo")
        spk = patch.add_module("speaker_output")
        patch.connect(clock.id, "out", fg.id, "trig")          # gate -> gate
        patch.connect(lfo.id, "cv", fg.id, "rate_cv")          # cv -> cv
        with pytest.raises(Exception):
            patch.connect(lfo.id, "cv", fg.id, "trig")         # cv -> gate
        with pytest.raises(Exception):
            patch.connect(fg.id, "out", spk.id, "in")          # cv -> audio


# ----- The curve knob --------------------------------------------------------


class TestCurve:
    def test_exponent_map(self):
        k = NumpyBackend._fg_exponent
        assert k(0.0) == 1.0                       # straight line
        assert k(1.0) == 4.0                       # steepest exponential
        assert k(-1.0) == 0.25                     # steepest logarithmic
        # +c and -c are reciprocals: the two halves mirror each other.
        for c in (0.25, 0.5, 1.0):
            assert k(c) * k(-c) == pytest.approx(1.0)
        # Out-of-range values clamp rather than explode.
        assert k(9.0) == k(1.0)
        assert k(-9.0) == k(-1.0)

    @pytest.mark.parametrize("curve", [-1.0, -0.5, 0.0, 0.5, 1.0])
    def test_half_rise_level_matches_the_power_law(self, curve):
        """Measure the claim: at the halfway point of the rise the output
        is 0.5 ** k. Testing the SHAPE, not merely that it rises."""
        rise_n = 480
        # Sample n carries counter n+1, so out[half-1] is the counter at
        # exactly half the stage -- pin the value, not a neighbourhood.
        half = rise_n // 2
        patch, src, fg, b = _rig(
            mode="trigger", rise=rise_n / SR, fall=0.01, curve=curve
        )
        out = _drive(b, patch, src, fg, _pulse(half))["out"]
        k = NumpyBackend._fg_exponent(curve)
        assert float(out[-1]) == pytest.approx(0.5 ** k, abs=1e-6)

    def test_fall_is_the_rise_mirrored(self):
        """Same curve, equal times: the fall read backwards is the rise."""
        n = 480
        patch, src, fg, b = _rig(
            mode="trigger", rise=n / SR, fall=n / SR, curve=0.8
        )
        out = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        rise, fall = out[:n], out[n:2 * n]
        np.testing.assert_allclose(rise[:-1], fall[::-1][1:], atol=2e-3)


# ----- Trigger mode ----------------------------------------------------------


class TestTriggerMode:
    def test_silence_without_a_trigger(self):
        patch, src, fg, b = _rig(mode="trigger", rise=0.01, fall=0.01)
        r = _drive(b, patch, src, fg, np.zeros(2048, dtype=np.float32))
        assert not np.any(r["out"])
        assert not np.any(r["eor"])
        assert not np.any(r["eoc"])

    def test_full_shape_and_exact_stage_lengths(self):
        """The integer-counter claim: a stage is exactly round(t*sr)
        samples, and stays exact across a block boundary."""
        rise_n, fall_n = 480, 960
        patch, src, fg, b = _rig(
            mode="trigger", rise=rise_n / SR, fall=fall_n / SR
        )
        # 4 blocks of 512 -> the rise ends inside block 0, the fall inside
        # block 2, so any per-block reset or drift would show.
        gate = _pulse(4 * 512)
        r = _drive(b, patch, src, fg, gate)
        assert _first_high(r["eor"]) == rise_n - 1
        assert _first_high(r["eoc"]) == rise_n + fall_n - 1
        assert r["out"][rise_n - 1] == pytest.approx(1.0)
        assert r["out"][rise_n + fall_n - 1] == 0.0
        assert np.all(r["out"] >= 0.0) and np.all(r["out"] <= 1.0)
        assert not np.any(r["out"][rise_n + fall_n:])  # idles, does not restart

    def test_gate_length_is_ignored(self):
        """A 1-sample trigger and a 1000-sample gate give the same shape."""
        kw = dict(mode="trigger", rise=0.005, fall=0.02)
        F = 4096
        p1, s1, f1, b1 = _rig(**kw)
        p2, s2, f2, b2 = _rig(**kw)
        short = _drive(b1, p1, s1, f1, _pulse(F, width=1))["out"]
        long = _drive(b2, p2, s2, f2, _pulse(F, width=1000))["out"]
        np.testing.assert_array_equal(short, long)

    def test_instant_rise(self):
        patch, src, fg, b = _rig(mode="trigger", rise=0.0, fall=0.01)
        r = _drive(b, patch, src, fg, _pulse(2048))
        assert r["out"][0] == pytest.approx(1.0)
        assert _first_high(r["eor"]) == 0

    def test_retrigger_mid_fall_climbs_from_where_it_was(self):
        patch, src, fg, b = _rig(mode="trigger", rise=0.01, fall=0.2)
        F = 8192
        gate = _pulse(F, at=0)
        gate[3000] = 1.0  # second trigger, deep in the long fall
        out = _drive(b, patch, src, fg, gate)["out"]
        level_before = float(out[2999])
        # It climbs (does not restart from zero) and never steps by more
        # than one sample's worth of ramp -- that is "click-free" measured.
        assert 0.0 < level_before < 1.0
        assert out[3000] > level_before
        assert float(np.max(np.abs(np.diff(out)))) < 0.02


# ----- Gate mode -------------------------------------------------------------


class TestGateMode:
    def test_holds_at_full_then_falls_on_release(self):
        rise_n, fall_n = 240, 480
        hold_until = 3000
        patch, src, fg, b = _rig(
            mode="gate", rise=rise_n / SR, fall=fall_n / SR
        )
        gate = _pulse(8192, at=0, width=hold_until)
        r = _drive(b, patch, src, fg, gate)
        out = r["out"]
        assert _first_high(r["eor"]) == rise_n - 1
        # Flat at full for the whole held stretch.
        np.testing.assert_allclose(out[rise_n:hold_until], 1.0, atol=1e-6)
        # Released at `hold_until`; the fall is exact from there.
        assert _first_high(r["eoc"]) == hold_until + fall_n - 1
        assert out[hold_until + fall_n - 1] == 0.0

    def test_release_mid_rise_falls_from_the_level_reached(self):
        rise_n, fall_n = 2000, 1000
        release = 800
        patch, src, fg, b = _rig(
            mode="gate", rise=rise_n / SR, fall=fall_n / SR
        )
        gate = _pulse(8192, at=0, width=release)
        r = _drive(b, patch, src, fg, gate)
        out = r["out"]
        peak = float(out[release - 1])
        assert 0.0 < peak < 1.0                       # never reached the top
        assert not np.any(r["eor"])                   # so no end-of-rise
        assert out[release] < peak                    # turned around
        assert float(np.max(np.abs(np.diff(out)))) < 0.01   # click-free
        # It falls from `peak`, so it lands EARLY: a partial fall.
        assert 0 < _first_high(r["eoc"]) < release + fall_n


# ----- Loop mode -------------------------------------------------------------


class TestLoopMode:
    def test_free_runs_with_nothing_patched(self):
        patch = Patch()
        fg = patch.add_module(
            "function_generator",
            params={"mode": "loop", "rise": 0.01, "fall": 0.01},
        )
        b = _backend()
        b.compile(patch)
        r = _free(fg, b, patch, 512, blocks=8)
        assert r["out"].max() == pytest.approx(1.0)
        assert r["out"].min() == 0.0
        assert np.any(r["eor"]) and np.any(r["eoc"])

    def test_period_is_exact_and_never_drifts(self):
        """The tripwire the float-step version fails.

        A per-sample `pos += 1/(t*sr)` accumulates about a sample of
        error per stage, so the loop period comes out 962 instead of 960
        and every cycle compounds it. Counting samples cannot drift --
        so assert ONE period value across 40 blocks, not an approximate
        mean.
        """
        rise_n, fall_n = 480, 480
        patch = Patch()
        fg = patch.add_module(
            "function_generator",
            params={"mode": "loop", "rise": rise_n / SR, "fall": fall_n / SR},
        )
        b = _backend()
        b.compile(patch)
        r = _free(fg, b, patch, 512, blocks=40)
        edges = _rising(r["eoc"])
        assert len(edges) > 15, "expected many cycles to measure"
        assert set(np.diff(edges).tolist()) == {rise_n + fall_n}

    def test_trig_is_a_click_free_sync(self):
        rise_n, fall_n = 1000, 1000
        patch, src, fg, b = _rig(
            mode="loop", rise=rise_n / SR, fall=fall_n / SR
        )
        gate = np.zeros(8192, dtype=np.float32)
        gate[1500] = 1.0          # mid-fall: a sync while it is on its way down
        r = _drive(b, patch, src, fg, gate)
        out = r["out"]
        assert out[1500] > out[1499]                        # turned back upward
        assert float(np.max(np.abs(np.diff(out)))) < 0.01   # no jump
        # ...and it keeps cycling afterwards.
        assert len(_rising(r["eoc"][1500:])) >= 2


# ----- EOR / EOC -------------------------------------------------------------


class TestEndOfPulses:
    def test_pulse_is_about_two_milliseconds(self):
        patch, src, fg, b = _rig(mode="trigger", rise=0.05, fall=0.5)
        r = _drive(b, patch, src, fg, _pulse(8192))
        width = int((r["eor"] > 0.5).sum())
        assert width == int(round(0.002 * SR))

    def test_pulse_shrinks_rather_than_swamping_a_fast_loop(self):
        """A 4 ms cycle must not emit a 2 ms pulse -- it is capped at a
        quarter of the cycle so eor and eoc stay distinguishable."""
        rise_n = fall_n = 96          # 4 ms total at 48 kHz
        patch = Patch()
        fg = patch.add_module(
            "function_generator",
            params={"mode": "loop", "rise": rise_n / SR, "fall": fall_n / SR},
        )
        b = _backend()
        b.compile(patch)
        r = _free(fg, b, patch, 512, blocks=4)
        width = int(np.diff(_rising(r["eoc"]))[0])
        assert width == rise_n + fall_n                  # period intact
        assert 1 <= (r["eoc"] > 0.5).sum() / len(_rising(r["eoc"])) <= (
            (rise_n + fall_n) // 4 + 1
        )
        # eor and eoc never merge into one continuous gate.
        assert not np.all((r["eor"] > 0.5) | (r["eoc"] > 0.5))


# ----- rate_cv ---------------------------------------------------------------


class TestRateCV:
    @pytest.mark.parametrize("octaves,factor", [(1.0, 0.5), (-1.0, 2.0), (0.0, 1.0)])
    def test_one_volt_per_octave_on_the_rate(self, octaves, factor):
        rise_n = fall_n = 480
        patch = Patch()
        const = patch.add_module("constant", params={"value": octaves})
        fg = patch.add_module(
            "function_generator",
            params={"mode": "trigger", "rise": rise_n / SR, "fall": fall_n / SR},
        )
        src = patch.add_module("schmitt")
        patch.connect(const.id, "out", fg.id, "rate_cv")
        patch.connect(src.id, "gate", fg.id, "trig")
        b = _backend()
        b.compile(patch)
        F = 4096
        cv = np.full(F, octaves, dtype=np.float32)
        r = b._render_function_generator(
            fg, F,
            {(src.id, "gate"): _pulse(F), (const.id, "out"): cv},
            patch,
        )
        expected = int(round((rise_n + fall_n) * factor))
        assert _first_high(r["eoc"]) == pytest.approx(expected - 1, abs=2)


# ----- Voice path ------------------------------------------------------------


def _voice_rig(**params):
    patch = Patch()
    src = patch.add_module("schmitt")
    fg = patch.add_module("function_generator", params=params)
    patch.connect(src.id, "gate", fg.id, "trig")
    b = _backend()
    b.compile(patch)
    return patch, src, fg, b


class TestVoicePath:
    @pytest.mark.parametrize("mode", ["trigger", "gate", "loop"])
    @pytest.mark.parametrize("curve", [-1.0, 0.0, 0.7])
    def test_voice_row_is_bit_identical_to_mono(self, mode, curve):
        gate = np.zeros(4000, dtype=np.float32)
        gate[10:2000] = 1.0
        gate[2500:2600] = 1.0
        kw = dict(mode=mode, rise=0.005, fall=0.02, curve=curve)

        pm, sm, fm, bm = _rig(**kw)
        mono = _drive(bm, pm, sm, fm, gate)

        pv, sv, fv, bv = _voice_rig(**kw)
        stacked = np.stack([gate, np.zeros_like(gate)])
        voice = bv._render_function_generator(
            fv, gate.shape[-1], {(sv.id, "gate"): stacked}, pv
        )
        for jack in ("out", "out_inv", "eor", "eoc"):
            assert voice[jack].shape == (2, gate.shape[-1])
            np.testing.assert_array_equal(voice[jack][0], mono[jack])

    def test_voices_are_independent(self):
        patch, src, fg, b = _voice_rig(mode="trigger", rise=0.002, fall=0.01)
        F = 4096
        g = np.zeros((3, F), dtype=np.float32)
        g[0, 0] = 1.0
        g[1, 1000] = 1.0
        # voice 2 never triggers
        r = b._render_function_generator(fg, F, {(src.id, "gate"): g}, patch)
        assert _first_high(r["eor"][0]) < _first_high(r["eor"][1])
        assert not np.any(r["out"][2])
        assert not np.any(r["eor"][2])

    def test_mono_and_voice_state_reinit(self):
        """Re-patching between shapes must not carry stale state."""
        patch, src, fg, b = _voice_rig(mode="trigger", rise=0.002, fall=0.01)
        F = 1024
        g2 = np.zeros((2, F), dtype=np.float32)
        g2[0, 0] = 1.0
        b._render_function_generator(fg, F, {(src.id, "gate"): g2}, patch)
        assert "voices" in b._state[fg.id]
        # Now a mono buffer: state flips shape without exploding.
        mono = b._render_function_generator(
            fg, F, {(src.id, "gate"): _pulse(F)}, patch
        )
        assert "voices" not in b._state[fg.id]
        assert mono["out"].shape == (F,)
        # And a DIFFERENT voice count reinits again.
        g4 = np.zeros((4, F), dtype=np.float32)
        g4[3, 0] = 1.0
        r = b._render_function_generator(fg, F, {(src.id, "gate"): g4}, patch)
        assert r["out"].shape == (4, F)
        assert np.any(r["out"][3]) and not np.any(r["out"][0])


# ----- Integration -----------------------------------------------------------


class TestKrellExample:
    def test_the_krell_example_actually_self_plays(self):
        """Not just "it renders" -- it must fire many times on its own.

        The first draft of this example used `eoc -> logic -> trig` and
        rendered perfectly while firing once per starter-clock pulse,
        because (until 2026-09-16) this rack severed any feedback cycle
        that did not pass through the matrix_mixer door. A patch that
        merely LOOKS like a krell is the failure mode worth a tripwire.
        The door is general now and `krell_feedback.json` IS the
        self-patch (see test_feedback_door.py); this example stays on
        `loop` mode -- the ears passed on it -- as the no-latency route.
        """
        from pathlib import Path

        from pysynthrack.io_patch import load_patch

        path = Path(__file__).parent.parent / "examples" / "krell_machine.json"
        patch = load_patch(path)
        fg = next(m for m in patch if m.TYPE == "function_generator")
        assert fg.params["mode"] == "loop", (
            "this krell free-runs on loop mode (the self-patched version "
            "is krell_feedback.json)"
        )
        b = NumpyBackend(sample_rate=44100, block_size=256)
        b.compile(patch)

        seen = []
        original = b._render_function_generator

        def spy(module, frames, buffers, patch_ref):
            result = original(module, frames, buffers, patch_ref)
            if module.id == fg.id:
                seen.append(np.asarray(result["out"]).copy())
            return result

        b._render_function_generator = spy
        for _ in range(int(44100 * 20 / 256)):
            out, _devices = b.render_block_multi(256)
            if out is not None:
                assert np.all(np.isfinite(out))

        env = np.concatenate(seen)
        starts = np.flatnonzero((env[:-1] == 0.0) & (env[1:] > 0.0)) + 1
        assert len(starts) > 10, f"only {len(starts)} notes in 20 s"
        # ...and the pace actually varies (rate_cv is doing something).
        gaps = np.diff(starts) / 44100.0
        assert gaps.max() > gaps.min() * 1.3, "the pace never breathes"


# ----- eor: what it is for, and the patch that is a latch (2026-09-17) ---------


class TestEor:
    def _self_patched_eor_cycles(self, block):
        """A trigger-mode fg with eor OR'd (with a 30 ms starter) back into
        its own trig, through the feedback door. Returns the number of
        complete cycles (eoc pulses) in 3 s."""
        p = Patch()
        fg = p.add_module("function_generator", params={"mode": "trigger", "rise": 0.1, "fall": 0.5})
        clk = p.add_module("clock", params={"bpm": 1.0, "division": 1.0, "pulse_width": 0.0005})
        lg = p.add_module("logic")
        p.connect(clk.id, "out", lg.id, "a")
        p.connect(fg.id, "eor", lg.id, "b")
        p.connect(lg.id, "or", fg.id, "trig")
        b = NumpyBackend(sample_rate=48000, block_size=block)
        b.compile(p)
        assert b._late_edges == {(lg.id, "or", fg.id, "trig")}
        seen = []
        orig = b._render_function_generator

        def spy(module, frames, buffers, patch_ref):
            r = orig(module, frames, buffers, patch_ref)
            if module.id == fg.id:
                seen.append(np.asarray(r["eoc"]).copy())
            return r

        b._render_function_generator = spy
        for _ in range(int(3 * 48000 / block)):
            b.render_block_multi(block)
        e = np.concatenate(seen)
        return int(((e[1:] > 0.5) & (e[:-1] <= 0.5)).sum())

    @pytest.mark.parametrize("block", [512, 64])
    def test_self_patched_eor_into_trig_is_a_latch_not_a_cycle(self, block):
        """The docs say it, this pins it: eor back into the same trig never
        completes a second cycle -- the retrigger lands a few ms into
        the fall and pins the output at the top (512), or the 2 ms pulse
        straddles two blocks and the loop dies (64). Either way, at most
        the starter's one cycle. The krell is eoc."""
        assert self._self_patched_eor_cycles(block) <= 1

    def test_the_swell_strike_example_fires_the_pluck_at_every_peak(self):
        """fg_eor_swell_strike.json: the fg cycles through the krell loop,
        and at every eor the fg is at 1.0 and the pluck goes from silent
        to struck."""
        from pathlib import Path

        from pysynthrack.io_patch import load_patch

        path = Path(__file__).parent.parent / "examples" / "fg_eor_swell_strike.json"
        patch = load_patch(path)
        fg = next(m for m in patch if m.TYPE == "function_generator")
        pl = next(m for m in patch if m.TYPE == "pluck")
        assert any(c.src_port == "eor" and c.dst_module_id == pl.id for c in patch.cables_into(pl.id))
        b = NumpyBackend(sample_rate=44100, block_size=512)
        b.compile(patch)
        env, eor, pluck = [], [], []
        orig_fg = b._render_function_generator
        orig_pl = b._render_pluck

        def spy_fg(module, frames, buffers, patch_ref):
            r = orig_fg(module, frames, buffers, patch_ref)
            if module.id == fg.id:
                env.append(np.asarray(r["out"]).copy())
                eor.append(np.asarray(r["eor"]).copy())
            return r

        def spy_pl(module, frames, buffers, patch_ref):
            r = orig_pl(module, frames, buffers, patch_ref)
            pluck.append(np.asarray(r["out"] if isinstance(r, dict) else r).copy())
            return r

        b._render_function_generator = spy_fg
        b._render_pluck = spy_pl
        for _ in range(int(44100 * 25 / 512)):
            out, _devices = b.render_block_multi(512)
            assert out is not None and np.all(np.isfinite(out))
        env = np.concatenate(env)
        eor = np.concatenate(eor)
        pl_env = np.abs(np.concatenate(pluck))
        eors = np.flatnonzero((eor[1:] > 0.5) & (eor[:-1] <= 0.5)) + 1
        assert len(eors) >= 5, f"only {len(eors)} swells in 25 s"
        for t in eors:
            assert env[t] == pytest.approx(1.0, abs=1e-6)          # eor IS the peak
            # A strike: the pluck's peak right after eor is well above
            # whatever was still ringing right before it (a fast swell
            # can re-strike a 2 s decay mid-ring -- "quiet before" is
            # not the claim, "louder after" is).
            before = pl_env[max(0, t - 3000):t].max() if t > 0 else 0.0
            after = pl_env[t:t + 3000].max()
            assert after > 0.3 and after > 1.5 * before, (t, before, after)
        gaps = np.diff(eors) / 44100.0
        assert gaps.max() > gaps.min() * 1.3, "the pace never breathes"


# ----- Per-slope curves (2026-09-12) ------------------------------------------


class TestPerSlopeCurves:
    def test_defaults_are_off(self):
        fg = Patch().add_module("function_generator")
        assert fg.params["curve_rise"] == 0.0
        assert fg.params["curve_fall"] == 0.0

    def test_zero_offsets_change_nothing(self):
        """Explicit zeros render exactly as their absence -- the shared
        knob still rules when the pair is centred."""
        g = np.zeros(4096, np.float32)
        g[10:900] = 1.0
        g[2000:2100] = 1.0
        for mode, curve in (("trigger", 0.7), ("gate", -0.6), ("loop", 0.4)):
            patch, src, fg, b = _rig(mode=mode, rise=0.02, fall=0.05, curve=curve)
            plain = _drive(b, patch, src, fg, g)
            patch, src, fg, b = _rig(mode=mode, rise=0.02, fall=0.05, curve=curve,
                                     curve_rise=0.0, curve_fall=0.0)
            explicit = _drive(b, patch, src, fg, g)
            for key in ("out", "eor", "eoc"):
                assert np.array_equal(plain[key], explicit[key]), (mode, key)

    @pytest.mark.parametrize("curve,offset", [(0.0, 0.5), (0.3, -0.8), (-0.5, 1.0)])
    def test_curve_rise_bends_only_the_rise(self, curve, offset):
        """The rise follows k(curve + curve_rise) at its midpoint; the fall
        is sample-for-sample what it was with the offset at 0."""
        n = 480
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR,
                                 curve=curve, curve_rise=offset)
        out = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        k = NumpyBackend._fg_exponent(curve + offset)
        assert float(out[n // 2 - 1]) == pytest.approx(0.5 ** k, abs=1e-6)
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR, curve=curve)
        plain = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        assert np.array_equal(out[n:], plain[n:])
        if k != NumpyBackend._fg_exponent(curve):
            assert not np.array_equal(out[:n], plain[:n])

    @pytest.mark.parametrize("curve,offset", [(0.0, 0.5), (0.3, -0.8), (-0.5, 1.0)])
    def test_curve_fall_bends_only_the_fall(self, curve, offset):
        n = 480
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR,
                                 curve=curve, curve_fall=offset)
        out = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        k = NumpyBackend._fg_exponent(curve + offset)
        # fall sample n + m carries counter m + 1: at m + 1 = n/2 the level
        # is (1 - 1/2) ** k
        assert float(out[n + n // 2 - 1]) == pytest.approx(0.5 ** k, abs=1e-6)
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR, curve=curve)
        plain = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        assert np.array_equal(out[:n], plain[:n])

    def test_offsets_are_clamped_with_the_shared_knob(self):
        """curve 1 + curve_rise 1 is still the steepest exponential, not
        beyond it: the sum clamps to +/-1 exactly as the shared knob does."""
        n = 480
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR,
                                 curve=1.0, curve_rise=1.0, curve_fall=-3.0)
        out = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR,
                                 curve=1.0, curve_fall=-2.0)
        ref = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        assert np.array_equal(out, ref)

    def test_pluck_then_linger_is_not_its_own_mirror(self):
        """An exponential rise into a logarithmic fall: the shapes differ,
        by design -- the mirror property holds only when the effective
        exponents match."""
        n = 480
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR,
                                 curve=0.0, curve_rise=0.8, curve_fall=-0.8)
        out = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        rise, fall = out[:n], out[n:2 * n]
        assert float(np.max(np.abs(rise[:-1] - fall[::-1][1:]))) > 0.2
        # rise: slow start (below linear); fall: lingers (above linear-mirror)
        assert float(rise[n // 2 - 1]) < 0.5
        assert float(fall[n // 2 - 1]) > 0.5

    def _worst_step(self, out):
        """Worst sample-to-sample step, counting the first sample's step
        up from rest (a logarithmic rise leaps on its very first sample,
        and that leap is part of the shape)."""
        x = np.concatenate([[0.0], np.asarray(out, dtype=np.float64)])
        return float(np.max(np.abs(np.diff(x))))

    def test_release_mid_rise_stays_click_free_with_different_exponents(self):
        """The backward solve on the fall entry uses the FALL's exponent,
        so a gate released mid-rise picks up at the current level even
        when the two slopes disagree. "Click-free" measured honestly: the
        worst step in the interrupted render is no worse than the worst
        step the same curves take UNINTERRUPTED -- a logarithmic slope at
        k = 1/4 has a genuine cliff at its ends, and that is the shape,
        not a discontinuity."""
        n = 4800
        params = dict(mode="gate", rise=n / SR, fall=n / SR,
                      curve=0.0, curve_rise=1.0, curve_fall=-1.0)
        patch, src, fg, b = _rig(**params)
        g = np.zeros(3 * n, np.float32)
        g[:n // 3] = 1.0
        cut = _drive(b, patch, src, fg, g)["out"]
        patch, src, fg, b = _rig(**params)
        g = np.zeros(3 * n, np.float32)
        g[:n + 10] = 1.0
        whole = _drive(b, patch, src, fg, g)["out"]
        assert self._worst_step(cut) <= self._worst_step(whole) + 1e-9
        top = float(cut[n // 3 - 1])
        assert 0.0 < top < 1.0
        assert float(cut[n // 3]) <= top

    def test_retrigger_mid_fall_stays_click_free_with_different_exponents(self):
        n = 4800
        params = dict(mode="trigger", rise=n / SR, fall=n / SR,
                      curve=0.0, curve_rise=-1.0, curve_fall=1.0)
        patch, src, fg, b = _rig(**params)
        g = np.zeros(3 * n, np.float32)
        g[0] = 1.0
        g[n + n // 2] = 1.0             # deep in the fall
        cut = _drive(b, patch, src, fg, g)["out"]
        patch, src, fg, b = _rig(**params)
        g = np.zeros(3 * n, np.float32)
        g[0] = 1.0
        whole = _drive(b, patch, src, fg, g)["out"]
        assert self._worst_step(cut) <= self._worst_step(whole) + 1e-9
        # and with gentler curves the retrigger really is seamless
        params = dict(mode="trigger", rise=n / SR, fall=n / SR,
                      curve=0.0, curve_rise=-0.5, curve_fall=0.5)
        patch, src, fg, b = _rig(**params)
        g = np.zeros(3 * n, np.float32)
        g[0] = 1.0
        g[n + n // 2] = 1.0
        cut = _drive(b, patch, src, fg, g)["out"].astype(np.float64)
        at = n + n // 2
        assert abs(float(cut[at] - cut[at - 1])) < 0.005
        assert abs(float(cut[at + 1] - cut[at])) < 0.005

    def test_loop_period_is_unchanged_by_the_pair(self):
        """Curves shape the stages; they never change their length."""
        patch, _src, fg, b = _rig(mode="loop", rise=480 / SR, fall=480 / SR,
                                  curve=0.2, curve_rise=0.9, curve_fall=-0.7)
        r = _free(fg, b, patch, 512, blocks=20)
        edges = _rising(r["eoc"])
        assert set(np.diff(edges).tolist()) == {960}

    def test_voice_rows_match_mono_with_the_pair(self):
        n = 480
        params = dict(mode="trigger", rise=n / SR, fall=n / SR,
                      curve=0.1, curve_rise=0.6, curve_fall=-0.4)
        patch, src, fg, b = _rig(**params)
        mono = _drive(b, patch, src, fg, _pulse(2 * n))["out"]
        patch, src, fg, b = _rig(**params)
        g = np.zeros((3, 2 * n), np.float32)
        g[1, 0] = 1.0
        voiced = _drive(b, patch, src, fg, g)["out"]
        assert np.array_equal(voiced[1], mono)
        assert np.all(voiced[0] == 0.0) and np.all(voiced[2] == 0.0)


# ----- Per-slope CVs and the inverted out (2026-09-14) ----------------------


def _cv_rig(rate=None, rise_cv=None, fall_cv=None, **params):
    """schmitt -> trig, plus a constant into each requested CV jack.

    Returns (patch, src, fg, buffers-with-the-CVs, backend); callers add
    the gate row and render. The constants are real modules so the cables
    exist and ``_input_buffer`` finds them the way it would live.
    """
    patch = Patch()
    src = patch.add_module("schmitt")
    fg = patch.add_module("function_generator", params=params)
    patch.connect(src.id, "gate", fg.id, "trig")
    bufs = {}
    for jack, value in (("rate_cv", rate), ("rise_cv", rise_cv), ("fall_cv", fall_cv)):
        if value is None:
            continue
        const = patch.add_module("constant", params={"value": value})
        patch.connect(const.id, "out", fg.id, jack)
        bufs[(const.id, "out")] = np.full(8192 * 8, value, np.float32)
    b = _backend()
    b.compile(patch)
    return patch, src, fg, bufs, b


def _stage_lengths(r):
    """(rise samples, fall samples) from the eor / eoc pulse onsets of a
    single trigger fired at sample 0."""
    eor = _first_high(r["eor"])
    eoc = _first_high(r["eoc"])
    return eor + 1, eoc - eor


class TestPerSlopeCVs:
    RISE = 480
    FALL = 960

    def _render(self, F=8192, **kw):
        patch, src, fg, bufs, b = _cv_rig(
            mode="trigger", rise=self.RISE / SR, fall=self.FALL / SR, **kw
        )
        bufs = {k: v[:F] for k, v in bufs.items()}
        bufs[(src.id, "gate")] = _pulse(F)
        return b._render_function_generator(fg, F, bufs, patch)

    def test_unpatched_is_array_equal_to_rate_cv_only(self):
        """The new jacks contribute exactly 0.0 octaves when absent: with
        only rate_cv cabled the render is bit-identical to a rig where
        the pair is cabled and sitting at 0."""
        for rate in (None, 0.0, 1.0, -0.7):
            plain = self._render(rate=rate)
            zeroed = self._render(rate=rate, rise_cv=0.0, fall_cv=0.0)
            for key in ("out", "out_inv", "eor", "eoc"):
                assert np.array_equal(plain[key], zeroed[key]), (rate, key)

    @pytest.mark.parametrize("octaves,factor", [(1.0, 0.5), (-1.0, 2.0), (2.0, 0.25)])
    def test_rise_cv_scales_only_the_rise(self, octaves, factor):
        r = self._render(rise_cv=octaves)
        rise_n, fall_n = _stage_lengths(r)
        assert rise_n == int(round(self.RISE * factor))
        assert fall_n == self.FALL
        # And the fall is sample-for-sample the plain fall.
        plain = self._render()
        p_rise, _ = _stage_lengths(plain)
        assert np.array_equal(
            r["out"][rise_n:rise_n + self.FALL], plain["out"][p_rise:p_rise + self.FALL]
        )

    @pytest.mark.parametrize("octaves,factor", [(1.0, 0.5), (-1.0, 2.0), (2.0, 0.25)])
    def test_fall_cv_scales_only_the_fall(self, octaves, factor):
        r = self._render(fall_cv=octaves)
        rise_n, fall_n = _stage_lengths(r)
        assert rise_n == self.RISE
        assert fall_n == int(round(self.FALL * factor))
        plain = self._render()
        assert np.array_equal(r["out"][:self.RISE], plain["out"][:self.RISE])

    def test_they_sum_in_octaves_with_rate_cv(self):
        """rate_cv +1 and rise_cv +1 is a rise four times as fast, and
        fall_cv -1 on top of rate_cv +1 cancels back to the knob's own
        fall -- the hardware arithmetic, both + per-slope."""
        r = self._render(rate=1.0, rise_cv=1.0, fall_cv=-1.0)
        rise_n, fall_n = _stage_lengths(r)
        assert rise_n == self.RISE // 4
        assert fall_n == self.FALL

    def test_per_slope_total_is_clamped_at_five_octaves(self):
        """rate_cv +4 and rise_cv +4 is clamped to +5 on the rise (x1/32),
        while the fall, at +4, is honoured as asked (x1/16)."""
        rise_n, fall_n = _stage_lengths(self._render(rate=4.0, rise_cv=4.0))
        assert rise_n == max(1, int(round(self.RISE / 32)))
        assert fall_n == int(round(self.FALL / 16))
        # And downward: -3 and -3 clamps to -5 (x32), not x64.
        rise_n, fall_n = _stage_lengths(
            self._render(F=8192 * 8, rate=-3.0, fall_cv=-3.0)
        )
        assert rise_n == self.RISE * 8
        assert fall_n == self.FALL * 32

    def test_loop_period_follows_the_pair(self):
        """In loop mode the eoc period is rise/2**a + fall/2**b, exact and
        driftless -- the integer-counter claim survives the split."""
        patch = Patch()
        fg = patch.add_module(
            "function_generator",
            params={"mode": "loop", "rise": 480 / SR, "fall": 480 / SR},
        )
        c1 = patch.add_module("constant", params={"value": 1.0})
        c2 = patch.add_module("constant", params={"value": -1.0})
        patch.connect(c1.id, "out", fg.id, "rise_cv")
        patch.connect(c2.id, "out", fg.id, "fall_cv")
        b = _backend()
        b.compile(patch)
        bufs = {
            (c1.id, "out"): np.full(512, 1.0, np.float32),
            (c2.id, "out"): np.full(512, -1.0, np.float32),
        }
        eoc = np.concatenate([
            np.asarray(b._render_function_generator(fg, 512, bufs, patch)["eoc"]).copy()
            for _ in range(40)
        ])
        edges = _rising(eoc)
        assert len(edges) >= 10
        assert set(np.diff(edges).tolist()) == {240 + 960}

    def test_voice_rate_cv_is_collapsed_to_one_tempo(self):
        """rate_cv is the "both" knob: a (V, F) rate_cv is summed to mono
        and every voice gets the same lengths -- the sum's, not a row's.
        (rise_cv / fall_cv are the per-voice pair; see TestPerVoiceRates.)"""
        patch = Patch()
        src = patch.add_module("schmitt")
        fg = patch.add_module(
            "function_generator",
            params={"mode": "trigger", "rise": 480 / SR, "fall": 480 / SR},
        )
        lfo = patch.add_module("lfo")
        patch.connect(src.id, "gate", fg.id, "trig")
        patch.connect(lfo.id, "cv", fg.id, "rate_cv")
        b = _backend()
        b.compile(patch)
        F = 4096
        g = np.zeros((2, F), np.float32)
        g[0, 0] = 1.0
        g[1, 0] = 1.0
        cv = np.zeros((2, F), np.float32)
        cv[0] = 1.0   # rows sum to +1 octave: both voices rise in 240
        r = b._render_function_generator(
            fg, F, {(src.id, "gate"): g, (lfo.id, "cv"): cv}, patch
        )
        assert _first_high(r["eor"][0]) == 239
        assert _first_high(r["eor"][1]) == 239
        assert np.array_equal(r["out"][0], r["out"][1])

    def test_voice_rows_match_mono_with_the_pair(self):
        F = 4096
        kw = dict(mode="trigger", rise=480 / SR, fall=960 / SR,
                  rate=0.5, rise_cv=-0.5, fall_cv=1.5)
        patch, src, fg, bufs, b = _cv_rig(**kw)
        bufs = {k: v[:F] for k, v in bufs.items()}
        mono = b._render_function_generator(
            fg, F, {**bufs, (src.id, "gate"): _pulse(F)}, patch
        )
        patch, src, fg, bufs, b = _cv_rig(**kw)
        bufs = {k: v[:F] for k, v in bufs.items()}
        g = np.zeros((3, F), np.float32)
        g[2, 0] = 1.0
        voiced = b._render_function_generator(fg, F, {**bufs, (src.id, "gate"): g}, patch)
        for key in ("out", "out_inv", "eor", "eoc"):
            assert np.array_equal(voiced[key][2], mono[key]), key


class TestOutInv:
    def test_is_one_minus_out_bit_exact(self):
        g = np.zeros(4096, np.float32)
        g[10:900] = 1.0
        g[2000:2100] = 1.0
        for mode, curve in (("trigger", 0.7), ("gate", -0.6), ("loop", 0.3)):
            patch, src, fg, b = _rig(mode=mode, rise=0.02, fall=0.05, curve=curve)
            r = _drive(b, patch, src, fg, g)
            assert r["out_inv"].dtype == np.float32
            assert np.array_equal(r["out_inv"], np.float32(1.0) - r["out"]), mode
            assert float(r["out_inv"].min()) >= 0.0 and float(r["out_inv"].max()) <= 1.0

    def test_sits_at_one_dips_to_zero_at_eor_and_recovers_by_eoc(self):
        n = 480
        patch, src, fg, b = _rig(mode="trigger", rise=n / SR, fall=n / SR)
        r = _drive(b, patch, src, fg, _pulse(4 * n))
        eor = _first_high(r["eor"])
        eoc = _first_high(r["eoc"])
        assert float(r["out_inv"][eor]) == 0.0
        assert float(r["out_inv"][eoc]) == 1.0
        assert np.all(r["out_inv"][eoc:] == 1.0)
        # Idle before a trigger is 1.0 too: the ducker is open at rest.
        patch, src, fg, b = _rig(mode="trigger")
        assert np.all(_drive(b, patch, src, fg, np.zeros(512, np.float32))["out_inv"] == 1.0)

    def test_sums_with_out_to_exactly_one(self):
        patch, src, fg, b = _rig(mode="loop", rise=0.01, fall=0.03, curve=0.8)
        r = _free(fg, b, patch, 512, blocks=8)
        assert np.all(r["out"] + r["out_inv"] == 1.0)

    def test_voice_path_and_parked_slots(self):
        patch, src, fg, b = _voice_rig(mode="trigger", rise=0.002, fall=0.01)
        F = 2048
        g = np.zeros((3, F), np.float32)
        g[1, 0] = 1.0
        r = b._render_function_generator(fg, F, {(src.id, "gate"): g}, patch)
        assert r["out_inv"].shape == (3, F)
        assert np.array_equal(r["out_inv"], np.float32(1.0) - r["out"])
        # Parked slots read 1.0 -- a silent voice does not duck anything.
        assert np.all(r["out_inv"][0] == 1.0) and np.all(r["out_inv"][2] == 1.0)
        assert float(r["out_inv"][1].min()) == 0.0

    def test_out_inv_is_a_cv_jack_that_reaches_a_vca(self):
        patch = Patch()
        fg = patch.add_module("function_generator")
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        patch.connect(fg.id, "out_inv", vca.id, "cv")          # cv -> cv
        with pytest.raises(Exception):
            patch.connect(fg.id, "out_inv", spk.id, "in")      # cv -> audio


# ----- Per-voice rates (2026-09-14, second pass) -----------------------------


def _pv_rig(rise=480, fall=960, mode="trigger", rate=None, jacks=("rise_cv", "fall_cv")):
    """schmitt -> trig, an lfo (a voice-capable cv source) into each of
    `jacks`, and optionally a constant into rate_cv. Returns
    (patch, src, fg, lfo, const, b); the caller supplies the buffers."""
    patch = Patch()
    src = patch.add_module("schmitt")
    fg = patch.add_module(
        "function_generator", params={"mode": mode, "rise": rise / SR, "fall": fall / SR}
    )
    lfo = patch.add_module("lfo")
    patch.connect(src.id, "gate", fg.id, "trig")
    for jack in jacks:
        patch.connect(lfo.id, "cv", fg.id, jack)
    const = None
    if rate is not None:
        const = patch.add_module("constant", params={"value": rate})
        patch.connect(const.id, "out", fg.id, "rate_cv")
    b = _backend()
    b.compile(patch)
    return patch, src, fg, lfo, const, b


def _rows(F, values):
    return np.stack([np.full(F, v, np.float32) for v in values])


def _stage_lengths_row(r, v):
    eor = _first_high(r["eor"][v])
    eoc = _first_high(r["eoc"][v])
    return eor + 1, eoc - eor


class TestPerVoiceRates:
    RISE = 480
    FALL = 960

    def test_each_voice_gets_its_own_rise(self):
        patch, src, fg, lfo, _c, b = _pv_rig(jacks=("rise_cv",))
        F = 8192
        g = np.zeros((3, F), np.float32)
        g[:, 0] = 1.0
        cv = _rows(F, [1.0, 0.0, -1.0])
        r = b._render_function_generator(
            fg, F, {(src.id, "gate"): g, (lfo.id, "cv"): cv}, patch
        )
        assert [_stage_lengths_row(r, v) for v in range(3)] == [
            (240, self.FALL), (480, self.FALL), (960, self.FALL),
        ]

    def test_each_voice_gets_its_own_fall(self):
        patch, src, fg, lfo, _c, b = _pv_rig(jacks=("fall_cv",))
        F = 8192
        g = np.zeros((3, F), np.float32)
        g[:, 0] = 1.0
        cv = _rows(F, [2.0, 0.0, -1.0])
        r = b._render_function_generator(
            fg, F, {(src.id, "gate"): g, (lfo.id, "cv"): cv}, patch
        )
        assert [_stage_lengths_row(r, v) for v in range(3)] == [
            (self.RISE, 240), (self.RISE, 960), (self.RISE, 1920),
        ]

    @pytest.mark.parametrize("octaves", [1.0, -0.5, 2.5])
    def test_a_voice_row_is_bit_identical_to_mono_fed_that_constant(self, octaves):
        """The per-voice law is the mono law applied per slot: row v with
        CV c renders exactly what the mono path renders with c on a
        constant into the same jacks (rate_cv too)."""
        F = 8192
        patch, src, fg, bufs, b = _cv_rig(
            mode="trigger", rise=self.RISE / SR, fall=self.FALL / SR,
            rate=0.25, rise_cv=octaves, fall_cv=octaves,
        )
        bufs = {k: v[:F] for k, v in bufs.items()}
        mono = b._render_function_generator(
            fg, F, {**bufs, (src.id, "gate"): _pulse(F)}, patch
        )
        patch, src, fg, lfo, const, b = _pv_rig(
            rise=self.RISE, fall=self.FALL, rate=0.25
        )
        g = np.zeros((3, F), np.float32)
        g[1, 0] = 1.0
        cv = _rows(F, [0.7, octaves, -3.0])
        r = b._render_function_generator(
            fg, F, {
                (src.id, "gate"): g, (lfo.id, "cv"): cv,
                (const.id, "out"): np.full(F, 0.25, np.float32),
            }, patch,
        )
        for key in ("out", "out_inv", "eor", "eoc"):
            assert np.array_equal(r[key][1], mono[key]), key

    def test_rate_cv_is_one_tempo_under_per_voice_slopes(self):
        """rate_cv +1 on top of per-voice rise_cv rows [+1, 0]: rises of
        120 and 240 (x4 and x2), falls both 480 (x2)."""
        patch, src, fg, lfo, const, b = _pv_rig(rate=1.0, jacks=("rise_cv",))
        F = 8192
        g = np.zeros((2, F), np.float32)
        g[:, 0] = 1.0
        r = b._render_function_generator(
            fg, F, {
                (src.id, "gate"): g, (lfo.id, "cv"): _rows(F, [1.0, 0.0]),
                (const.id, "out"): np.full(F, 1.0, np.float32),
            }, patch,
        )
        assert _stage_lengths_row(r, 0) == (120, 480)
        assert _stage_lengths_row(r, 1) == (240, 480)

    def test_zero_row_is_the_knob_and_parked_slots_stay_parked(self):
        """velocity_cv holds 0 on a slot with nothing playing: that row
        renders exactly as with no CV cabled, and a parked slot is still
        skipped (all zeros, no state churn)."""
        F = 4096
        patch, src, fg, b = _voice_rig(mode="trigger", rise=self.RISE / SR, fall=self.FALL / SR)
        g = np.zeros((3, F), np.float32)
        g[0, 0] = 1.0
        plain = b._render_function_generator(fg, F, {(src.id, "gate"): g}, patch)
        patch, src, fg, lfo, _c, b = _pv_rig(rise=self.RISE, fall=self.FALL)
        cv = _rows(F, [0.0, 3.0, 0.0])
        r = b._render_function_generator(
            fg, F, {(src.id, "gate"): g, (lfo.id, "cv"): cv}, patch
        )
        for key in ("out", "out_inv", "eor", "eoc"):
            assert np.array_equal(r[key], plain[key]), key
        assert b._state[fg.id]["voices"][1]["phase"] == NumpyBackend._FG_IDLE

    def test_loop_mode_periods_differ_per_voice_and_never_drift(self):
        """Two voices synced once, then free-running with rise_cv rows
        [+1, -1]: eoc periods of 240+480 and 960+480, every cycle."""
        patch, src, fg, lfo, _c, b = _pv_rig(rise=480, fall=480, mode="loop", jacks=("rise_cv",))
        F = 512
        eoc = [[], []]
        for blk in range(60):
            g = np.zeros((2, F), np.float32)
            if blk == 0:
                g[:, 0] = 1.0
            r = b._render_function_generator(
                fg, F, {(src.id, "gate"): g, (lfo.id, "cv"): _rows(F, [1.0, -1.0])}, patch
            )
            for v in range(2):
                eoc[v].append(np.asarray(r["eoc"][v]).copy())
        periods = [set(np.diff(_rising(np.concatenate(eoc[v]))).tolist()) for v in range(2)]
        assert periods == [{720}, {1440}]

    def test_mono_trigger_with_a_voice_cv_uses_the_summed_law(self):
        """No voice axis on the trigger means no per-voice functions to
        give rates to: the (V, F) CV collapses by sum, as _input_buffer
        would -- the 2026-09-14 mono law, unchanged."""
        patch, src, fg, lfo, _c, b = _pv_rig(jacks=("rise_cv",))
        F = 8192
        cv = _rows(F, [0.5, 0.5])     # sums to +1 octave
        r = b._render_function_generator(
            fg, F, {(src.id, "gate"): _pulse(F), (lfo.id, "cv"): cv}, patch
        )
        assert r["out"].shape == (F,)
        assert _stage_lengths(r) == (240, self.FALL)

    def test_mismatched_voice_count_falls_back_to_the_summed_law(self):
        patch, src, fg, lfo, _c, b = _pv_rig(jacks=("rise_cv",))
        F = 8192
        g = np.zeros((2, F), np.float32)
        g[:, 0] = 1.0
        cv = _rows(F, [1.0, 0.0, 0.0])   # 3 rows against 2 voices: sum = +1
        r = b._render_function_generator(
            fg, F, {(src.id, "gate"): g, (lfo.id, "cv"): cv}, patch
        )
        assert _stage_lengths_row(r, 0) == (240, self.FALL)
        assert _stage_lengths_row(r, 1) == (240, self.FALL)

    def test_only_one_jack_per_voice_the_other_shared(self):
        """rise_cv per voice, fall_cv a mono constant: the fall is the
        shared law on every slot while the rise is each row's own."""
        patch = Patch()
        src = patch.add_module("schmitt")
        fg = patch.add_module(
            "function_generator",
            params={"mode": "trigger", "rise": self.RISE / SR, "fall": self.FALL / SR},
        )
        lfo = patch.add_module("lfo")
        const = patch.add_module("constant", params={"value": 1.0})
        patch.connect(src.id, "gate", fg.id, "trig")
        patch.connect(lfo.id, "cv", fg.id, "rise_cv")
        patch.connect(const.id, "out", fg.id, "fall_cv")
        b = _backend()
        b.compile(patch)
        F = 8192
        g = np.zeros((2, F), np.float32)
        g[:, 0] = 1.0
        r = b._render_function_generator(
            fg, F, {
                (src.id, "gate"): g, (lfo.id, "cv"): _rows(F, [1.0, -1.0]),
                (const.id, "out"): np.full(F, 1.0, np.float32),
            }, patch,
        )
        assert _stage_lengths_row(r, 0) == (240, 480)
        assert _stage_lengths_row(r, 1) == (960, 480)
