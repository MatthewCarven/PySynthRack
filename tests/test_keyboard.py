"""Tests for the Keyboard module + its numpy-backend renderer."""
from __future__ import annotations

import math

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.keyboard import (
    Keyboard,
    midi_to_freq,
    midi_to_name,
    semitone_to_midi,
)


class TestNoteMath:
    @pytest.mark.parametrize(
        "midi,expected_hz",
        [(69, 440.0), (60, 261.6256), (72, 523.2511), (57, 220.0), (81, 880.0)],
    )
    def test_midi_to_freq(self, midi, expected_hz):
        assert math.isclose(midi_to_freq(midi), expected_hz, rel_tol=1e-4)

    def test_octave_4_a_key_is_middle_c(self):
        # Home-row A in octave 4 should produce middle C (MIDI 60).
        assert semitone_to_midi(4, 0) == 60

    def test_octave_4_h_key_is_a4(self):
        # Home-row H (semitone 9) in octave 4 should produce A4 (MIDI 69).
        assert semitone_to_midi(4, 9) == 69

    def test_name_round_trip(self):
        assert midi_to_name(60) == "C4"
        assert midi_to_name(69) == "A4"
        assert midi_to_name(72) == "C5"


class TestKeyboardModel:
    def test_register_and_construct(self):
        patch = Patch()
        kb = patch.add_module("keyboard")
        assert isinstance(kb, Keyboard)
        assert kb.params == {"octave": 4, "waveform": "sine", "volume": 0.5}
        assert kb.input_ports == []
        assert [p.name for p in kb.output_ports] == ["out"]

    def test_note_on_off_mutates_active_set(self):
        patch = Patch()
        kb = patch.add_module("keyboard")
        kb.note_on(60)
        kb.note_on(64)
        assert kb.snapshot_active_notes() == {60, 64}
        kb.note_off(60)
        assert kb.snapshot_active_notes() == {64}
        kb.all_notes_off()
        assert kb.snapshot_active_notes() == set()

    def test_active_notes_not_serialized(self):
        patch = Patch()
        kb = patch.add_module("keyboard")
        kb.note_on(60)
        data = kb.to_dict()
        # The JSON shape must NOT contain transient runtime state.
        assert "active_notes" not in data
        assert "active_notes" not in data.get("params", {})


class TestKeyboardRendering:
    def _make_patch_with_keyboard(self) -> tuple[Patch, Keyboard]:
        patch = Patch()
        kb = patch.add_module("keyboard", params={"volume": 0.5})
        out = patch.add_module("speaker_output")
        patch.connect(kb.id, "out", out.id, "in")
        return patch, kb

    def test_silent_when_no_keys_held(self):
        patch, _kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        kb = next(m for m in patch if m.TYPE == "keyboard")
        buf = backend._render_keyboard(kb, frames=512)
        assert np.allclose(buf, 0.0)

    def test_press_then_release_produces_then_decays_signal(self):
        """Press a key, render one block — should hear something. Release,
        render another block — should still hear something (release ramp).
        Render many more blocks — should decay to silence."""
        patch, kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)

        kb.note_on(69)  # A4 = 440 Hz
        # Render a few blocks so envelope reaches sustain.
        for _ in range(5):
            buf = backend._render_keyboard(kb, frames=512)
        assert np.max(np.abs(buf)) > 0.1, "expected audible signal while held"

        kb.note_off(69)
        # Right after release, the envelope ramps down — still audible.
        buf_release = backend._render_keyboard(kb, frames=512)
        assert np.max(np.abs(buf_release)) > 0.0

        # Far enough after release, voice should be reaped and silent.
        for _ in range(50):
            buf_after = backend._render_keyboard(kb, frames=512)
        assert np.allclose(buf_after, 0.0), "voice should be silent after release"

    def test_polyphony_sums_voices(self):
        """Two notes at once should give more energy than one note alone."""
        patch, kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=2048)
        backend.compile(patch)

        # Warm up the envelope on one note.
        kb.note_on(60)
        for _ in range(20):
            single = backend._render_keyboard(kb, frames=2048)
        single_rms = float(np.sqrt(np.mean(single**2)))

        kb.note_on(64)  # add another note — chord
        for _ in range(20):
            both = backend._render_keyboard(kb, frames=2048)
        both_rms = float(np.sqrt(np.mean(both**2)))

        assert both_rms > single_rms, (
            f"polyphonic sum should be louder: single={single_rms:.4f}, "
            f"both={both_rms:.4f}"
        )

    def test_attack_ramp_avoids_click(self):
        """The first sample of the first block after note_on should be near
        zero (envelope hasn't ramped up yet). This is what prevents the
        characteristic 'tick' of an abrupt waveform start."""
        patch, kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=64)
        backend.compile(patch)
        kb.note_on(60)
        buf = backend._render_keyboard(kb, frames=64)
        # First few samples should be small relative to peak.
        # (Attack is 5ms = 220 samples at 44.1k, so the first 64-sample
        # block should not have reached full amplitude.)
        assert abs(buf[0]) < 0.05
