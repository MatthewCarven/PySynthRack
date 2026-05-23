"""Tests for the ADSR envelope module."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.adsr import ADSR


def _adsr_with_gate_source(attack=0.01, decay=0.1, sustain=0.5, release=0.1):
    """Build a patch: keyboard(gate) → adsr. Returns (patch, kb, adsr)."""
    patch = Patch()
    kb = patch.add_module("keyboard")
    env = patch.add_module(
        "adsr",
        params={"attack": attack, "decay": decay, "sustain": sustain, "release": release},
    )
    patch.connect(kb.id, "gate", env.id, "gate")
    return patch, kb, env


def _render_cv(backend, patch, env, frames: int) -> np.ndarray:
    """Render one block through the keyboard's gate into the ADSR.

    The backend's _render_module path produces both the gate and the
    envelope CV in the right order; we collect just the envelope's output.

    Slice 4: Keyboard now emits a per-voice (V, F) gate buffer, so the
    ADSR (which is voice-aware as of slice 3a) returns a per-voice (V, F)
    envelope. These tests press one note at a time, so summing across the
    voice axis collapses to a 1D envelope identical in shape to the pre-
    slice-4 mono path -- the same implicit-sum-at-mono-sinks rule the
    SpeakerOutput uses.
    """
    kb = next(m for m in patch if m.TYPE == "keyboard")
    buffers: dict = {}
    kb_out = backend._render_keyboard(kb, frames=frames)
    buffers[(kb.id, "out")] = kb_out["out"]
    buffers[(kb.id, "gate")] = kb_out["gate"]
    cv = backend._render_adsr(env, frames, buffers, patch)
    if cv.ndim == 2:
        cv = cv.sum(axis=0)
    return cv


class TestADSRModel:
    def test_register_and_defaults(self):
        patch = Patch()
        env = patch.add_module("adsr")
        assert isinstance(env, ADSR)
        assert env.params == {
            "attack": 0.01,
            "decay": 0.10,
            "sustain": 0.70,
            "release": 0.30,
        }
        assert [p.name for p in env.input_ports] == ["gate"]
        assert env.input_ports[0].signal_kind == "gate"
        assert [p.name for p in env.output_ports] == ["cv"]
        assert env.output_ports[0].signal_kind == "cv"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "adsr",
            params={"attack": 0.5, "decay": 0.2, "sustain": 0.3, "release": 1.0},
        )
        restored = Patch.from_dict(patch.to_dict())
        env = next(m for m in restored if m.TYPE == "adsr")
        assert env.params["attack"] == 0.5
        assert env.params["decay"] == 0.2
        assert env.params["sustain"] == 0.3
        assert env.params["release"] == 1.0


class TestADSRBehavior:
    def test_idle_with_no_gate_is_zero(self):
        """A patched-but-not-triggered ADSR should hold at 0."""
        patch = Patch()
        env = patch.add_module("adsr")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        out = backend._render_adsr(env, 512, {}, patch)
        assert np.allclose(out, 0.0)

    def test_attack_reaches_one_after_attack_time(self):
        """With attack=10ms at 44.1kHz, the envelope should hit 1.0 within
        ~440 samples of gate-on."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(attack=0.01, decay=0.1, sustain=0.7)
        backend = NumpyBackend(sample_rate=sr, block_size=2048)
        backend.compile(patch)
        kb.note_on(60)
        # Render enough samples to span attack + start of decay.
        cv = _render_cv(backend, patch, env, frames=2048)
        peak = float(np.max(cv))
        assert peak >= 0.99, f"attack peak was {peak:.3f}"

    def test_sustain_holds_value_while_gate_high(self):
        """After attack+decay, the envelope should sit on the sustain level."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(
            attack=0.005, decay=0.01, sustain=0.4, release=0.1
        )
        backend = NumpyBackend(sample_rate=sr, block_size=4096)
        backend.compile(patch)
        kb.note_on(60)
        # First render: passes through attack + decay.
        _ = _render_cv(backend, patch, env, frames=4096)
        # Second render: should be entirely sustain.
        cv = _render_cv(backend, patch, env, frames=4096)
        # All values should be close to sustain.
        assert float(np.min(cv)) >= 0.39
        assert float(np.max(cv)) <= 0.41

    def test_release_decays_to_zero(self):
        """Releasing the gate ramps the envelope back to 0."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(
            attack=0.001, decay=0.001, sustain=0.5, release=0.05
        )
        backend = NumpyBackend(sample_rate=sr, block_size=2048)
        backend.compile(patch)
        kb.note_on(60)
        # Settle at sustain.
        _ = _render_cv(backend, patch, env, frames=2048)
        kb.all_notes_off()
        # 50ms release at 44.1kHz = 2205 samples. Render 4096 to fully decay.
        cv = _render_cv(backend, patch, env, frames=4096)
        # Last sample should be ~0 — env has had time to release fully.
        assert abs(float(cv[-1])) < 1e-3

    def test_no_retrigger_during_held_gate(self):
        """A second note on top of a held one should NOT reset the envelope.
        Master-envelope semantics: the gate already-high stays high."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(attack=0.05, decay=0.1, sustain=0.8)
        backend = NumpyBackend(sample_rate=sr, block_size=4096)
        backend.compile(patch)
        kb.note_on(60)
        # Render through attack + decay → settled at sustain.
        for _ in range(3):
            cv = _render_cv(backend, patch, env, frames=4096)
        before_chord = float(cv[-1])
        # Add second note; gate stays high so envelope shouldn't snap back.
        kb.note_on(64)
        cv = _render_cv(backend, patch, env, frames=4096)
        # Sustain value must remain — no attack restart.
        assert abs(float(cv[0]) - before_chord) < 0.05

    def test_no_nan_with_zero_durations(self):
        """An ADSR with all-zero times shouldn't divide by zero."""
        patch, kb, env = _adsr_with_gate_source(
            attack=0.0, decay=0.0, sustain=0.5, release=0.0
        )
        backend = NumpyBackend(sample_rate=sr, block_size=512)
        backend.compile(patch)
        kb.note_on(60)
        cv = _render_cv(backend, patch, env, frames=512)
        assert not np.any(np.isnan(cv))
        assert not np.any(np.isinf(cv))
