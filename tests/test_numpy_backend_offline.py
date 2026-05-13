"""Offline tests of the numpy backend's DSP — no audio device required.

We construct a Patch, prime the backend, then call its render method
directly with a synthetic buffer. This catches phase errors, NaNs, etc.
without depending on PortAudio being installed.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch


def _osc_to_output_patch(waveform: str, freq: float = 440.0, amp: float = 0.5) -> Patch:
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": waveform, "freq": freq, "amp": amp}
    )
    out = patch.add_module("speaker_output", params={"gain": 1.0})
    patch.connect(osc.id, "out", out.id, "in")
    return patch


@pytest.mark.parametrize("waveform", ["sine", "saw", "square", "triangle"])
def test_oscillator_produces_signal_in_range(waveform):
    patch = _osc_to_output_patch(waveform, freq=440.0, amp=0.5)
    backend = NumpyBackend(sample_rate=44100, block_size=1024)
    backend.compile(patch)
    osc = next(m for m in patch if m.TYPE == "oscillator")
    buf = backend._render_oscillator(osc, frames=1024)
    assert buf.dtype == np.float32
    assert buf.shape == (1024,)
    # Amplitude must respect the amp parameter (within rounding).
    assert np.max(np.abs(buf)) <= 0.5 + 1e-5
    # Signal should not be flat zero.
    assert np.std(buf) > 0


def test_sine_frequency_is_correct():
    """440 Hz sine should have ~440 cycles per second.

    Count zero-crossings in 1 second — should be ~880 (one per half-cycle).
    """
    patch = _osc_to_output_patch("sine", freq=440.0, amp=0.5)
    backend = NumpyBackend(sample_rate=44100, block_size=44100)
    backend.compile(patch)
    osc = next(m for m in patch if m.TYPE == "oscillator")
    buf = backend._render_oscillator(osc, frames=44100)
    # Count sign changes — should be 2 per cycle, so ~880 for 440 Hz.
    zero_crossings = np.sum(np.diff(np.signbit(buf)).astype(int))
    assert 870 <= zero_crossings <= 890


def test_oscillator_phase_is_continuous_across_blocks():
    """Rendering two consecutive blocks should produce a continuous sine."""
    patch = _osc_to_output_patch("sine", freq=440.0, amp=0.5)
    backend = NumpyBackend(sample_rate=44100, block_size=512)
    backend.compile(patch)
    osc = next(m for m in patch if m.TYPE == "oscillator")

    block1 = backend._render_oscillator(osc, frames=512)
    block2 = backend._render_oscillator(osc, frames=512)

    # If phase resets between blocks, block2[0] would equal block1[0]. With
    # continuity, block2[0] should match what block1[512] *would* be.
    # We don't know that exactly, but we can verify there's no huge jump
    # between the last sample of block1 and the first of block2.
    jump = abs(block2[0] - block1[-1])
    # 440 Hz at 44.1 kHz: per-sample delta peaks around 2π * 440 / 44100 * amp
    # ≈ 0.031. Tolerate up to 4x that.
    assert jump < 0.13


def test_topological_sort_handles_disconnected_modules():
    patch = Patch()
    osc1 = patch.add_module("oscillator")
    _osc2 = patch.add_module("oscillator")  # unwired
    out = patch.add_module("speaker_output")
    patch.connect(osc1.id, "out", out.id, "in")
    backend = NumpyBackend()
    backend.compile(patch)
    # All three modules should appear in the topo order even though osc2 is
    # disconnected.
    assert len(backend._topo_order) == 3


def test_compile_drops_state_when_module_type_changes():
    """Regression: opening a second patch that reuses module IDs for
    different types would leak the first patch's state into the second
    patch's renderer (e.g. oscillator phase dict surviving into the
    keyboard renderer, which then KeyError'd on ``state["voices"]``)."""
    import pysynthrack.modules  # noqa: F401
    from pysynthrack.audio.numpy_backend import NumpyBackend
    from pysynthrack.core import Patch

    backend = NumpyBackend(sample_rate=44100, block_size=64)

    # Patch A: id=1 is an oscillator. Drive it so it accumulates state.
    patch_a = Patch()
    osc = patch_a.add_module("oscillator")
    out_a = patch_a.add_module("speaker_output")
    patch_a.connect(osc.id, "out", out_a.id, "in")
    backend.compile(patch_a)
    backend._render_oscillator(osc, 64)
    assert "phase" in backend._state[osc.id]

    # Patch B: id=1 is now a keyboard (same id, different type). Compile
    # must drop the stale oscillator state — otherwise the keyboard
    # renderer reads ``state["voices"]`` from {"phase": …} and crashes.
    patch_b = Patch()
    kb = patch_b.add_module("keyboard")
    out_b = patch_b.add_module("speaker_output")
    patch_b.connect(kb.id, "out", out_b.id, "in")
    assert kb.id == osc.id, "test relies on the id collision"
    backend.compile(patch_b)

    # State for that id should either be gone or already keyboard-shaped.
    state_after = backend._state.get(kb.id)
    assert state_after is None or "voices" in state_after

    # And rendering must not raise.
    result = backend._render_keyboard(kb, 64)
    assert "out" in result and "gate" in result


def test_compile_preserves_state_when_type_stays():
    """Compile across an unchanged module type should preserve continuity
    (oscillator phase carries forward across cable adds, no clicks)."""
    import pysynthrack.modules  # noqa: F401
    from pysynthrack.audio.numpy_backend import NumpyBackend
    from pysynthrack.core import Patch

    backend = NumpyBackend(sample_rate=44100, block_size=64)
    patch = Patch()
    osc = patch.add_module("oscillator")
    out = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", out.id, "in")

    backend.compile(patch)
    backend._render_oscillator(osc, 64)
    phase_before = backend._state[osc.id]["phase"]
    assert phase_before != 0.0  # advanced past origin

    # Recompile (e.g. user added another cable) — phase must survive.
    backend.compile(patch)
    assert backend._state[osc.id]["phase"] == phase_before
