"""Slew — the CV slew limiter / lag / glide.

Pins the contract: independent rise/fall times, the two shapes (linear
constant-rate reaches the target; exponential eases to ~99% in the same
wall-clock), instant on a zero time, prime-to-input (no startup swoop),
voice-aware per-voice slew with mono==single-voice parity, block-size
independence, and unpatched → silence.

Runs the renderer directly with hand-fed input blocks (no audio hardware),
injecting the upstream buffer into the store the same way render_block_multi
would — so an arbitrary mono (F,) or voice (V, F) input can be driven.
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend

from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules import slew as _slew  # noqa: F401
from pysynthrack.core.module import get_module_type, all_module_types

SR = 1000  # 1 kHz keeps sample-count math exact and readable


def _driver(shape="linear", rise=0.1, fall=0.1, block=64):
    """A backend + a `constant → slew` patch, plus the slew's src id.

    Feed input by calling `.step(block_array)` with a mono (F,) or voice
    (V, F) array; it injects that as the upstream buffer and returns the
    slew's output for that block. State carries across calls."""
    patch = Patch()
    slew = patch.add_module(
        "slew", params={"shape": shape, "rise_time": rise, "fall_time": fall}
    )
    src = patch.add_module("constant")
    patch.connect(src.id, "out", slew.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(block_array):
        arr = np.asarray(block_array, dtype=np.float32)
        F = arr.shape[-1]
        return b._render_slew(patch.get(slew.id), F, {(src.id, "out"): arr}, patch)

    step.backend = b
    step.slew = slew
    return step


def _ramp_to(target, blocks, F):
    """[zeros block (primes at 0), then `blocks` blocks of `target`]."""
    return [np.zeros(F, dtype=np.float32)] + [
        np.full(F, target, dtype=np.float32) for _ in range(blocks)
    ]


# ----- registration ----------------------------------------------------------


class TestRegistration:
    def test_registered_in_cv_utilities(self):
        cls = get_module_type("slew")
        assert cls.CATEGORY == "CV & Utilities"
        assert "slew" in all_module_types()

    def test_ports_and_params(self):
        cls = get_module_type("slew")
        assert [p.name for p in cls.INPUT_PORTS] == ["in"]
        assert [p.name for p in cls.OUTPUT_PORTS] == ["out"]
        assert cls.DEFAULT_PARAMS["shape"] == "linear"
        assert cls.DEFAULT_PARAMS["rise_time"] == 0.1
        assert cls.DEFAULT_PARAMS["fall_time"] == 0.1


# ----- linear: constant rate, reaches the target -----------------------------


class TestLinear:
    def test_up_reaches_in_rise_time(self):
        # rise 0.05 s @ 1 kHz → step 1/50, a unit step reaches in ~50 samples.
        d = _driver("linear", rise=0.05, fall=0.2)
        d(np.zeros(64, dtype=np.float32))  # prime at 0
        y = np.concatenate([d(np.ones(64, dtype=np.float32)) for _ in range(2)])
        reach = int(np.argmax(y >= 0.999))
        assert 49 <= reach <= 51
        assert y.max() <= 1.0 + 1e-6  # never overshoots

    def test_fall_is_independent_and_slower(self):
        # fall 0.2 s = 4× the rise → ~200 samples down for a unit drop.
        d = _driver("linear", rise=0.05, fall=0.2)
        d(np.ones(64, dtype=np.float32))  # prime at 1
        y = np.concatenate([d(np.zeros(64, dtype=np.float32)) for _ in range(5)])
        reach = int(np.argmax(y <= 0.001))
        assert 198 <= reach <= 202

    def test_constant_rate_equal_steps(self):
        # Successive samples move by the same amount (a straight ramp).
        d = _driver("linear", rise=0.1, fall=0.1)
        d(np.zeros(32, dtype=np.float32))
        y = d(np.ones(32, dtype=np.float32))
        diffs = np.diff(y[:10])
        assert np.allclose(diffs, diffs[0], atol=1e-9)

    def test_slews_toward_negative_targets(self):
        # A downward move to a negative value uses fall_time and reaches it.
        d = _driver("linear", rise=0.5, fall=0.05)
        d(np.zeros(64, dtype=np.float32))
        y = np.concatenate([d(np.full(64, -1.0, dtype=np.float32)) for _ in range(2)])
        reach = int(np.argmax(y <= -0.999))
        assert 49 <= reach <= 51


# ----- exponential: one-pole ease to ~99% in the same wall-clock -------------


class TestExponential:
    def test_reaches_99pct_in_rise_time(self):
        d = _driver("exponential", rise=0.05, fall=0.05)
        d(np.zeros(64, dtype=np.float32))
        y = np.concatenate([d(np.ones(64, dtype=np.float32)) for _ in range(2)])
        r99 = int(np.argmax(y >= 0.99))
        assert 47 <= r99 <= 53

    def test_monotonic_and_no_overshoot(self):
        d = _driver("exponential", rise=0.05, fall=0.05)
        d(np.zeros(64, dtype=np.float32))
        y = np.concatenate([d(np.ones(64, dtype=np.float32)) for _ in range(2)])
        assert np.all(np.diff(y) >= -1e-9)  # never turns back
        assert y.max() <= 1.0 + 1e-9        # asymptotic, never exceeds target

    def test_curve_differs_from_linear_midway(self):
        # Same arrival time, different shape: exponential is already past the
        # linear ramp's halfway point at the midpoint of the glide.
        lin = _driver("linear", rise=0.1, fall=0.1)
        exp = _driver("exponential", rise=0.1, fall=0.1)
        for drv in (lin, exp):
            drv(np.zeros(128, dtype=np.float32))
        yl = lin(np.ones(128, dtype=np.float32))
        ye = exp(np.ones(128, dtype=np.float32))
        mid = 50  # ~half of the 0.1 s @ 1 kHz glide
        assert ye[mid] > yl[mid]


# ----- instant / prime / unpatched -------------------------------------------


class TestEdges:
    def test_zero_time_passes_through_instantly(self):
        d = _driver("linear", rise=0.0, fall=0.0)
        d(np.zeros(16, dtype=np.float32))
        y = d(np.ones(16, dtype=np.float32))
        assert np.allclose(y, 1.0)

    def test_primes_to_first_input_no_swoop(self):
        # First-ever block of a constant 0.7 emerges AT 0.7 immediately — the
        # running value primes to the input, it does not glide up from 0.
        d = _driver("linear", rise=0.5, fall=0.5)
        y = d(np.full(64, 0.7, dtype=np.float32))
        assert np.allclose(y, 0.7)

    def test_unpatched_is_silent(self):
        # No cable into `in` → nothing to slew → zeros, and no state kept.
        patch = Patch()
        s = patch.add_module("slew")
        b = NumpyBackend(sample_rate=SR, block_size=32)
        b.compile(patch)
        out = b._render_slew(patch.get(s.id), 32, {}, patch)
        assert out.shape == (32,) and not out.any()
        assert s.id not in b._state


# ----- voice-awareness -------------------------------------------------------


class TestVoiceAware:
    def test_voice_input_keeps_shape_and_slews_per_voice(self):
        d = _driver("linear", rise=0.05, fall=0.05, block=64)
        d(np.zeros((2, 64), dtype=np.float32))  # prime both voices at 0
        # Voice 0 jumps to 1.0, voice 1 to 0.5 — independent slews.
        block = np.zeros((2, 64), dtype=np.float32)
        block[0] = 1.0
        block[1] = 0.5
        y = np.concatenate([d(block) for _ in range(2)], axis=1)
        assert y.shape == (2, 128)
        # Same constant rate, so voice 1 (half the distance) reaches first.
        assert int(np.argmax(y[1] >= 0.499)) < int(np.argmax(y[0] >= 0.999))

    def test_mono_equals_single_voice(self):
        seq = [np.zeros(48, dtype=np.float32),
               np.ones(48, dtype=np.float32),
               np.full(48, -0.3, dtype=np.float32)]
        mono = _driver("linear", rise=0.07, fall=0.09, block=48)
        voice = _driver("linear", rise=0.07, fall=0.09, block=48)
        m = np.concatenate([mono(b) for b in seq])
        v = np.concatenate([voice(b[None, :])[0] for b in seq])
        assert np.allclose(m, v, atol=1e-6)


# ----- block-size independence ----------------------------------------------


class TestBlockSizeIndependence:
    def test_same_result_at_two_block_sizes(self):
        # A long ramp fed as 32- vs 64-sample blocks yields the same signal.
        full = np.concatenate([
            np.zeros(64, dtype=np.float32),
            np.ones(128, dtype=np.float32),
            np.full(128, -0.5, dtype=np.float32),
        ])

        def render(block):
            d = _driver("exponential", rise=0.08, fall=0.13, block=block)
            outs = []
            for i in range(0, full.shape[0], block):
                outs.append(d(full[i:i + block]))
            return np.concatenate(outs)

        assert np.allclose(render(32), render(64), atol=1e-6)
