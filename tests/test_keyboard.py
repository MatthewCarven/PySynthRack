"""Tests for the Keyboard module + its numpy-backend renderer.

Slice 4 (2026-05-23): Keyboard renderer mirrors MIDIInput's per-slot
``(MAX_VOICES, frames)`` shape on both ``out`` and ``gate``. Tests that
called ``_render_keyboard`` directly were updated to expect the new
shape; the public Keyboard model API (``note_on`` / ``note_off`` /
``all_notes_off`` / ``snapshot_active_notes``) is unchanged.
"""
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
        # v0.2 (post-ADSR): keyboard exposes both audio out and gate out.
        out_port_names = [p.name for p in kb.output_ports]
        assert out_port_names == ["out", "gate"]
        gate_port = next(p for p in kb.output_ports if p.name == "gate")
        assert gate_port.signal_kind == "gate"

    def test_note_on_off_mutates_active_set(self):
        patch = Patch()
        kb = patch.add_module("keyboard")
        kb.note_on(60)
        kb.note_on(64)
        # snapshot_active_notes() still returns a set (slice 4 keeps
        # this contract for the UI). Internally it pulls from
        # VoiceSlots.held_notes().keys().
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
        # Post slice 4 the runtime state lives on ``voices``
        # (VoiceSlots), but the same rule applies -- no runtime
        # voice state in the serialized form.
        assert "active_notes" not in data
        assert "active_notes" not in data.get("params", {})
        assert "voices" not in data
        assert "voices" not in data.get("params", {})

    def test_snapshot_voice_slots_returns_max_voices_entries(self):
        # Slice 4: renderer hook. The renderer iterates exactly
        # MAX_VOICES (=16) slots; empty slots have note=-1, gating=False.
        patch = Patch()
        kb = patch.add_module("keyboard")
        kb.note_on(60)
        slots = kb.snapshot_voice_slots()
        assert len(slots) == 16
        # First allocation lands in slot 0.
        assert slots[0]["note"] == 60
        assert slots[0]["gating"] is True
        # Every other slot is empty.
        for i in range(1, 16):
            assert slots[i]["note"] == -1
            assert slots[i]["gating"] is False


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
        result = backend._render_keyboard(kb, frames=512)
        # Slice 4: both out and gate are (V, F). Silent everywhere.
        assert result["out"].shape == (16, 512)
        assert result["gate"].shape == (16, 512)
        assert np.allclose(result["out"], 0.0)
        assert np.allclose(result["gate"], 0.0)

    def test_press_then_release_produces_then_decays_signal(self):
        """Press a key, render one block — should hear something. Release,
        render another block — should still hear something (release ramp).
        Render many more blocks — should decay to silence.

        Slice 4: audio is (V, F). Use ``np.max(np.abs(buf))`` across the
        whole buffer to detect any per-voice activity."""
        patch, kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)

        kb.note_on(69)  # A4 = 440 Hz
        # Render a few blocks so envelope reaches sustain.
        for _ in range(5):
            buf = backend._render_keyboard(kb, frames=512)["out"]
        assert np.max(np.abs(buf)) > 0.1, "expected audible signal while held"

        kb.note_off(69)
        # Right after release, the envelope ramps down — still audible.
        buf_release = backend._render_keyboard(kb, frames=512)["out"]
        assert np.max(np.abs(buf_release)) > 0.0

        # Far enough after release, the slot's tail decays fully to zero.
        for _ in range(50):
            buf_after = backend._render_keyboard(kb, frames=512)["out"]
        assert np.allclose(buf_after, 0.0), "voice should be silent after release"

    def test_polyphony_sums_voices(self):
        """Two notes at once should give more energy than one note alone.

        Slice 4: with per-slot (V, F) audio, each note occupies its own
        slot row. Sum across the voice axis to get the mono mix the
        speaker would render, then compare RMS."""
        patch, kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=2048)
        backend.compile(patch)

        # Warm up the envelope on one note.
        kb.note_on(60)
        for _ in range(20):
            single = backend._render_keyboard(kb, frames=2048)["out"]
        single_mono = single.sum(axis=0)
        single_rms = float(np.sqrt(np.mean(single_mono ** 2)))

        kb.note_on(64)  # add another note — chord
        for _ in range(20):
            both = backend._render_keyboard(kb, frames=2048)["out"]
        both_mono = both.sum(axis=0)
        both_rms = float(np.sqrt(np.mean(both_mono ** 2)))

        assert both_rms > single_rms, (
            f"polyphonic sum should be louder: single={single_rms:.4f}, "
            f"both={both_rms:.4f}"
        )
        # Sanity: each note should occupy its own slot, not pile into one.
        # After the second note_on, slot 1 has a nonzero row.
        assert float(np.max(np.abs(both[0]))) > 0.0
        assert float(np.max(np.abs(both[1]))) > 0.0

    def test_attack_ramp_avoids_click(self):
        """The first sample of the slot-0 row after note_on should be near
        zero (envelope hasn't ramped up yet). This is what prevents the
        characteristic 'tick' of an abrupt waveform start.

        Voice-aware shape (slice 4): the audio buffer is (V, F). The
        first allocated note lands in slot 0, so we check ``buf[0, 0]``."""
        patch, kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=64)
        backend.compile(patch)
        kb.note_on(60)
        buf = backend._render_keyboard(kb, frames=64)["out"]
        assert buf.shape == (16, 64)
        # First few samples of slot 0 should be small relative to peak.
        # (Attack is 5ms = 220 samples at 44.1k, so the first 64-sample
        # block should not have reached full amplitude.)
        assert abs(float(buf[0, 0])) < 0.05

    def test_gate_per_voice_high_when_held_low_when_idle(self):
        """Gate output is per-voice (slice 4). A held key drives its own
        slot's gate high; other slots stay low. New notes allocate fresh
        slots without retriggering existing ones -- the per-voice
        granularity that lets a downstream ADSR fire one envelope per
        note rather than one shared envelope per chord."""
        patch, kb = self._make_patch_with_keyboard()
        backend = NumpyBackend(sample_rate=44100, block_size=128)
        backend.compile(patch)

        idle = backend._render_keyboard(kb, frames=128)["gate"]
        assert idle.shape == (16, 128)
        # No notes held -> every slot's gate is low.
        assert np.allclose(idle, 0.0)

        kb.note_on(60)
        held = backend._render_keyboard(kb, frames=128)["gate"]
        # First note allocates slot 0: slot 0 gate goes high, every
        # other slot stays low.
        assert np.allclose(held[0], 1.0)
        for i in range(1, 16):
            assert np.allclose(held[i], 0.0), f"slot {i} unexpectedly gated"

        kb.note_on(64)
        chord = backend._render_keyboard(kb, frames=128)["gate"]
        # Second note allocates slot 1. Both slots 0 and 1 gate high;
        # the others stay low. Slot 0's gate did NOT drop in between
        # (no retrigger of held voices).
        assert np.allclose(chord[0], 1.0)
        assert np.allclose(chord[1], 1.0)
        for i in range(2, 16):
            assert np.allclose(chord[i], 0.0)

        kb.all_notes_off()
        released = backend._render_keyboard(kb, frames=128)["gate"]
        # Panic clears every slot -> every gate goes low.
        assert np.allclose(released, 0.0)
