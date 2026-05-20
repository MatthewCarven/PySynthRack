"""Tests for the MIDIInput module.

These tests deliberately do not require a real MIDI device. We exercise
the callback by handing the module ``mido.Message`` objects directly,
which is what the mido IO thread would do at runtime. The only
real-hardware integration we can't unit-test is the open-port handshake
itself; that's covered by the manual smoke test in WORKLOG when a
device is actually plugged in.

If mido isn't installed in the test environment the message-parsing
tests skip (the module itself still imports fine — that's covered by
the "no mido" test).
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch
from pysynthrack.modules.midiinput import (
    AUTO_DEVICE,
    MIDIInput,
    _MIDO_AVAILABLE,
    available_devices,
)

try:
    import mido  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    mido = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Module metadata and basic construction
# ---------------------------------------------------------------------------


class TestMIDIInputMetadata:
    def test_type_string(self):
        m = MIDIInput(module_id=1)
        assert m.TYPE == "midi_input"

    def test_default_params(self):
        m = MIDIInput(module_id=1)
        assert m.params["device"] == AUTO_DEVICE
        assert m.params["channel"] == 0
        assert m.params["octave_shift"] == 0
        assert m.params["velocity_sensitive"] is True
        assert m.params["waveform"] == "sine"
        assert m.params["volume"] == 0.5

    def test_no_input_ports(self):
        m = MIDIInput(module_id=1)
        assert m.INPUT_PORTS == []

    def test_outputs_audio_gate_and_all_cvs(self):
        m = MIDIInput(module_id=1)
        ports = [(p.name, p.signal_kind) for p in m.OUTPUT_PORTS]
        assert ports == [
            ("out", "audio"),
            ("gate", "gate"),
            ("pitch_cv", "cv"),
            ("mod_cv", "cv"),
            ("pressure_cv", "cv"),
        ]

    def test_default_bend_range(self):
        m = MIDIInput(module_id=1)
        assert m.params["bend_range"] == 2.0

    def test_default_mod_scale(self):
        m = MIDIInput(module_id=1)
        assert m.params["mod_scale"] == 1.0

    def test_default_pressure_scale(self):
        m = MIDIInput(module_id=1)
        assert m.params["pressure_scale"] == 1.0

    def test_starts_with_no_active_notes(self):
        m = MIDIInput(module_id=1)
        assert m.snapshot_active_notes() == {}


# ---------------------------------------------------------------------------
# Direct note_on / note_off API
# ---------------------------------------------------------------------------


class TestNoteIngest:
    def test_note_on_records_velocity(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 0.75)
        assert m.snapshot_active_notes() == {60: 0.75}

    def test_note_off_removes(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m.note_off(60)
        assert m.snapshot_active_notes() == {}

    def test_note_on_velocity_zero_acts_as_note_off(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m.note_on(60, 0.0)  # running-status note-off
        assert m.snapshot_active_notes() == {}

    def test_all_notes_off_clears(self):
        m = MIDIInput(module_id=1)
        for n in (60, 64, 67):
            m.note_on(n, 1.0)
        m.all_notes_off()
        assert m.snapshot_active_notes() == {}

    def test_velocity_is_clamped(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 2.0)  # over the top
        assert m.snapshot_active_notes()[60] == 1.0

    def test_octave_shift_up(self):
        m = MIDIInput(module_id=1)
        m.params["octave_shift"] = 1
        m.note_on(60, 1.0)
        assert m.snapshot_active_notes() == {72: 1.0}

    def test_octave_shift_down(self):
        m = MIDIInput(module_id=1)
        m.params["octave_shift"] = -2
        m.note_on(60, 1.0)
        assert m.snapshot_active_notes() == {36: 1.0}

    def test_octave_shift_note_off_matches(self):
        # If we shift up by 1 on note_on(60) → 72, then note_off(60)
        # should also resolve to 72 so the held note actually goes away.
        m = MIDIInput(module_id=1)
        m.params["octave_shift"] = 1
        m.note_on(60, 1.0)
        m.note_off(60)
        assert m.snapshot_active_notes() == {}

    def test_octave_shift_clamps_out_of_range(self):
        # Shifting C-1 (note 0) down by 1 octave goes negative — drop the note.
        m = MIDIInput(module_id=1)
        m.params["octave_shift"] = -1
        m.note_on(5, 1.0)  # would land at -7 if applied
        assert m.snapshot_active_notes() == {}

    def test_snapshot_is_a_copy(self):
        # Mutating the snapshot dict should not affect internal state.
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        snap = m.snapshot_active_notes()
        snap[99] = 0.5
        assert m.snapshot_active_notes() == {60: 1.0}

    def test_concurrent_writes_are_safe(self):
        # 100 threads each hammering note_on/off; we just want it not to
        # raise or corrupt the dict (we don't check final state — order
        # is racy on purpose).
        m = MIDIInput(module_id=1)

        def worker():
            for n in range(50):
                m.note_on(n, 0.5)
                m.note_off(n)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # If we get here without an exception, the lock did its job.
        # State could be any subset of (60..) depending on interleaving.
        assert isinstance(m.snapshot_active_notes(), dict)


# ---------------------------------------------------------------------------
# Pitch bend (direct API - no mido required)
# ---------------------------------------------------------------------------


class TestPitchBend:
    def test_default_pitch_bend_is_zero(self):
        m = MIDIInput(module_id=1)
        assert m.snapshot_pitch_bend() == 0.0

    def test_set_pitch_bend_round_trips(self):
        m = MIDIInput(module_id=1)
        m.set_pitch_bend(0.5)
        assert m.snapshot_pitch_bend() == 0.5
        m.set_pitch_bend(-0.25)
        assert m.snapshot_pitch_bend() == -0.25

    def test_set_pitch_bend_clamps_above_one(self):
        m = MIDIInput(module_id=1)
        m.set_pitch_bend(2.0)
        assert m.snapshot_pitch_bend() == 1.0

    def test_set_pitch_bend_clamps_below_minus_one(self):
        m = MIDIInput(module_id=1)
        m.set_pitch_bend(-3.5)
        assert m.snapshot_pitch_bend() == -1.0

    def test_concurrent_pitch_bend_safe(self):
        # 8 threads each ramping the wheel; we just want no exception
        # and the final value to be in [-1, 1].
        m = MIDIInput(module_id=1)

        def worker():
            for i in range(200):
                m.set_pitch_bend((i - 100) / 100.0)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = m.snapshot_pitch_bend()
        assert -1.0 <= final <= 1.0


# ---------------------------------------------------------------------------
# Mod wheel (direct API - no mido required)
# ---------------------------------------------------------------------------


class TestModWheel:
    def test_default_mod_wheel_is_zero(self):
        m = MIDIInput(module_id=1)
        assert m.snapshot_mod_wheel() == 0.0

    def test_set_mod_wheel_round_trips(self):
        m = MIDIInput(module_id=1)
        m.set_mod_wheel(0.5)
        assert m.snapshot_mod_wheel() == 0.5
        m.set_mod_wheel(1.0)
        assert m.snapshot_mod_wheel() == 1.0

    def test_set_mod_wheel_clamps_above_one(self):
        m = MIDIInput(module_id=1)
        m.set_mod_wheel(3.5)
        assert m.snapshot_mod_wheel() == 1.0

    def test_set_mod_wheel_clamps_below_zero(self):
        # Unipolar - negative values are clamped to 0, not allowed through.
        m = MIDIInput(module_id=1)
        m.set_mod_wheel(-0.5)
        assert m.snapshot_mod_wheel() == 0.0


# ---------------------------------------------------------------------------
# Channel aftertouch (direct API - no mido required)
# ---------------------------------------------------------------------------


class TestAftertouch:
    def test_default_aftertouch_is_zero(self):
        m = MIDIInput(module_id=1)
        assert m.snapshot_aftertouch() == 0.0

    def test_set_aftertouch_round_trips(self):
        m = MIDIInput(module_id=1)
        m.set_aftertouch(0.6)
        assert m.snapshot_aftertouch() == 0.6
        m.set_aftertouch(1.0)
        assert m.snapshot_aftertouch() == 1.0

    def test_set_aftertouch_clamps_above_one(self):
        m = MIDIInput(module_id=1)
        m.set_aftertouch(2.5)
        assert m.snapshot_aftertouch() == 1.0

    def test_set_aftertouch_clamps_below_zero(self):
        # Unipolar - aftertouch can't go below 0.
        m = MIDIInput(module_id=1)
        m.set_aftertouch(-0.3)
        assert m.snapshot_aftertouch() == 0.0


# ---------------------------------------------------------------------------
# Sustain pedal (direct API and CC 64)
# ---------------------------------------------------------------------------


class TestSustainPedalDirect:
    """Sustain pedal state mutation via the direct set_sustain API.

    These tests bypass the mido callback so they run even without the
    [midi] extra installed. Behavior under MIDI messages is covered in
    TestSustainPedalViaCC further down.
    """

    def test_default_pedal_off(self):
        m = MIDIInput(module_id=1)
        assert m.snapshot_sustain_pedal() is False

    def test_set_sustain_round_trips(self):
        m = MIDIInput(module_id=1)
        m.set_sustain(True)
        assert m.snapshot_sustain_pedal() is True
        m.set_sustain(False)
        assert m.snapshot_sustain_pedal() is False

    def test_note_off_with_pedal_down_keeps_voice_gating(self):
        # Press a note, depress pedal, release the key — the slot
        # should still be gating (gate stays high). snapshot_active_notes
        # is the held-keys view, so it drops the note; snapshot_voice_slots
        # is what the renderer will use, and there the gating flag stays
        # true.
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m.set_sustain(True)
        m.note_off(60)
        # Key is up — held-keys view is now empty.
        assert m.snapshot_active_notes() == {}
        # But the slot is still sustained.
        slots = m.snapshot_voice_slots()
        gating = [s for s in slots if s["gating"]]
        assert len(gating) == 1
        assert gating[0]["note"] == 60
        assert gating[0]["sustained"] is True

    def test_pedal_up_drops_sustained_voices(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m.set_sustain(True)
        m.note_off(60)
        # Slot sustained — still gating.
        assert any(s["gating"] for s in m.snapshot_voice_slots())
        # Lift the pedal.
        m.set_sustain(False)
        # Nothing should be gating now.
        assert not any(s["gating"] for s in m.snapshot_voice_slots())

    def test_pedal_does_not_affect_held_keys(self):
        # Pedal while keys are still down must leave them held.
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m.set_sustain(True)
        m.set_sustain(False)
        # Note 60 was never released — should still be held.
        assert m.snapshot_active_notes() == {60: 1.0}


class TestVoiceSlotsSnapshot:
    """The new snapshot_voice_slots() API used by the polyphonic renderer."""

    def test_initial_snapshot_is_all_empty(self):
        m = MIDIInput(module_id=1)
        slots = m.snapshot_voice_slots()
        assert len(slots) == 16
        assert all(s["note"] == -1 for s in slots)
        assert all(not s["gating"] for s in slots)

    def test_three_notes_populate_three_slots(self):
        m = MIDIInput(module_id=1)
        for n in (60, 64, 67):
            m.note_on(n, 1.0)
        slots = m.snapshot_voice_slots()
        gating = [s for s in slots if s["gating"]]
        assert len(gating) == 3
        assert {s["note"] for s in gating} == {60, 64, 67}

    def test_octave_shift_is_applied_to_slot_note(self):
        m = MIDIInput(module_id=1)
        m.params["octave_shift"] = 1
        m.note_on(60, 1.0)
        slots = m.snapshot_voice_slots()
        gating = [s for s in slots if s["gating"]]
        assert len(gating) == 1
        assert gating[0]["note"] == 72


# ---------------------------------------------------------------------------
# Sustain pedal via MIDI CC 64
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _MIDO_AVAILABLE, reason="mido not installed")
class TestSustainPedalViaCC:
    def test_cc64_value_127_engages_pedal(self):
        m = MIDIInput(module_id=1)
        m._on_message(
            mido.Message("control_change", control=64, value=127, channel=0)
        )
        assert m.snapshot_sustain_pedal() is True

    def test_cc64_value_64_engages_pedal(self):
        # MIDI spec: 64 is the on/off boundary; >= 64 = on.
        m = MIDIInput(module_id=1)
        m._on_message(
            mido.Message("control_change", control=64, value=64, channel=0)
        )
        assert m.snapshot_sustain_pedal() is True

    def test_cc64_value_63_releases_pedal(self):
        m = MIDIInput(module_id=1)
        # First engage.
        m._on_message(
            mido.Message("control_change", control=64, value=127, channel=0)
        )
        # Then send 63 — should be treated as off.
        m._on_message(
            mido.Message("control_change", control=64, value=63, channel=0)
        )
        assert m.snapshot_sustain_pedal() is False

    def test_cc64_value_0_releases_pedal(self):
        m = MIDIInput(module_id=1)
        m._on_message(
            mido.Message("control_change", control=64, value=127, channel=0)
        )
        m._on_message(
            mido.Message("control_change", control=64, value=0, channel=0)
        )
        assert m.snapshot_sustain_pedal() is False

    def test_full_pedal_workflow_via_messages(self):
        # Note on, pedal down, note off (sustained), pedal up.
        m = MIDIInput(module_id=1)
        m._on_message(mido.Message("note_on", note=60, velocity=100, channel=0))
        m._on_message(
            mido.Message("control_change", control=64, value=127, channel=0)
        )
        m._on_message(mido.Message("note_off", note=60, velocity=64, channel=0))
        # Sustained — still gating.
        assert any(s["gating"] for s in m.snapshot_voice_slots())
        # Pedal up.
        m._on_message(
            mido.Message("control_change", control=64, value=0, channel=0)
        )
        assert not any(s["gating"] for s in m.snapshot_voice_slots())


# ---------------------------------------------------------------------------
# MIDI message handling via the callback path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _MIDO_AVAILABLE, reason="mido not installed")
class TestOnMessage:
    def test_note_on_via_message(self):
        m = MIDIInput(module_id=1)
        msg = mido.Message("note_on", note=60, velocity=100, channel=0)
        m._on_message(msg)
        snap = m.snapshot_active_notes()
        assert 60 in snap
        assert abs(snap[60] - 100 / 127) < 1e-6

    def test_note_on_velocity_zero_via_message(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m._on_message(mido.Message("note_on", note=60, velocity=0, channel=0))
        assert m.snapshot_active_notes() == {}

    def test_note_off_via_message(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m._on_message(mido.Message("note_off", note=60, velocity=64, channel=0))
        assert m.snapshot_active_notes() == {}

    def test_all_notes_off_cc123(self):
        m = MIDIInput(module_id=1)
        for n in (60, 64, 67):
            m.note_on(n, 1.0)
        m._on_message(
            mido.Message("control_change", control=123, value=0, channel=0)
        )
        assert m.snapshot_active_notes() == {}

    def test_other_cc_is_ignored(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        # CC 5 (portamento time) is genuinely unhandled — must not
        # disturb note state or any controller-state mirror.
        m._on_message(
            mido.Message("control_change", control=5, value=127, channel=0)
        )
        assert m.snapshot_active_notes() == {60: 1.0}

    def test_cc1_updates_mod_wheel(self):
        m = MIDIInput(module_id=1)
        # Full mod wheel: value=127 -> 127/127 = 1.0
        m._on_message(mido.Message("control_change", control=1, value=127, channel=0))
        assert m.snapshot_mod_wheel() == 1.0
        # Mid mod wheel: value=64 -> 64/127 ~= 0.504
        m._on_message(mido.Message("control_change", control=1, value=64, channel=0))
        assert abs(m.snapshot_mod_wheel() - 64 / 127) < 1e-6
        # Wheel at rest
        m._on_message(mido.Message("control_change", control=1, value=0, channel=0))
        assert m.snapshot_mod_wheel() == 0.0

    def test_cc1_does_not_clear_notes(self):
        # Wheel motion is independent of held-note state.
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m._on_message(mido.Message("control_change", control=1, value=100, channel=0))
        assert m.snapshot_active_notes() == {60: 1.0}

    def test_aftertouch_updates_pressure(self):
        m = MIDIInput(module_id=1)
        # Full pressure: value=127 -> 1.0
        m._on_message(mido.Message("aftertouch", value=127, channel=0))
        assert m.snapshot_aftertouch() == 1.0
        # Half pressure
        m._on_message(mido.Message("aftertouch", value=64, channel=0))
        assert abs(m.snapshot_aftertouch() - 64 / 127) < 1e-6
        # Release
        m._on_message(mido.Message("aftertouch", value=0, channel=0))
        assert m.snapshot_aftertouch() == 0.0

    def test_aftertouch_does_not_clear_notes(self):
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m._on_message(mido.Message("aftertouch", value=100, channel=0))
        assert m.snapshot_active_notes() == {60: 1.0}

    def test_pitchwheel_updates_pitch_bend(self):
        m = MIDIInput(module_id=1)
        # Full positive deflection: pitch=8191 -> 8191/8192 ~= 0.99988
        m._on_message(mido.Message("pitchwheel", pitch=8191, channel=0))
        assert abs(m.snapshot_pitch_bend() - 8191 / 8192) < 1e-6
        # Full negative deflection: pitch=-8192 -> -1.0 exactly
        m._on_message(mido.Message("pitchwheel", pitch=-8192, channel=0))
        assert m.snapshot_pitch_bend() == -1.0
        # Wheel at rest
        m._on_message(mido.Message("pitchwheel", pitch=0, channel=0))
        assert m.snapshot_pitch_bend() == 0.0

    def test_pitchwheel_does_not_clear_notes(self):
        # The wheel position is independent of held-note state - a bend
        # while a note is held must not silence the note.
        m = MIDIInput(module_id=1)
        m.note_on(60, 1.0)
        m._on_message(mido.Message("pitchwheel", pitch=4096, channel=0))
        assert m.snapshot_active_notes() == {60: 1.0}


# ---------------------------------------------------------------------------
# Channel filtering
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _MIDO_AVAILABLE, reason="mido not installed")
class TestChannelFilter:
    def test_omni_accepts_all_channels(self):
        m = MIDIInput(module_id=1)
        m.params["channel"] = 0  # omni
        for ch in (0, 5, 15):
            m._on_message(mido.Message("note_on", note=60 + ch, velocity=100, channel=ch))
        assert set(m.snapshot_active_notes().keys()) == {60, 65, 75}

    def test_specific_channel_filters(self):
        m = MIDIInput(module_id=1)
        m.params["channel"] = 3  # accept only channel 3 (mido channel 2)
        m._on_message(mido.Message("note_on", note=60, velocity=100, channel=0))
        m._on_message(mido.Message("note_on", note=64, velocity=100, channel=2))
        m._on_message(mido.Message("note_on", note=67, velocity=100, channel=5))
        # Only the channel=2 (mido) → channel 3 (user) note made it through.
        assert set(m.snapshot_active_notes().keys()) == {64}


# ---------------------------------------------------------------------------
# Rendering through the numpy backend
# ---------------------------------------------------------------------------


def _build_simple_patch() -> tuple[Patch, MIDIInput]:
    """MIDIInput → SpeakerOutput, the simplest playable patch."""
    patch = Patch()
    midi = patch.add_module("midi_input")
    spk = patch.add_module("speaker_output")
    patch.connect(midi.id, "out", spk.id, "in")
    return patch, midi


def _run_blocks(backend: NumpyBackend, n: int = 10) -> np.ndarray:
    """Render N blocks and return the last one (lets the attack ramp settle)."""
    out = np.zeros((backend.block_size, 2), dtype=np.float32)
    for _ in range(n):
        out = backend.render_block(backend.block_size)
    return out


class TestRendering:
    def test_silent_when_no_notes_held(self):
        patch, midi = _build_simple_patch()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        out = _run_blocks(backend)
        assert np.max(np.abs(out)) < 1e-6

    def test_audio_when_note_held(self):
        patch, midi = _build_simple_patch()
        midi.params["volume"] = 0.7
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        midi.note_on(69, 1.0)  # A4 at full velocity
        out = _run_blocks(backend, n=15)
        peak = float(np.max(np.abs(out)))
        # Peak should reach close to volume (0.7) after the ramp.
        assert 0.5 < peak < 0.8

    def test_velocity_scales_amplitude(self):
        patch, midi = _build_simple_patch()
        midi.params["volume"] = 1.0
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)

        midi.note_on(69, 1.0)
        _ = _run_blocks(backend, n=20)
        peak_full = float(np.max(np.abs(_run_blocks(backend, n=2))))

        midi.all_notes_off()
        _ = _run_blocks(backend, n=20)  # let the release tail decay
        midi.note_on(69, 0.5)
        _ = _run_blocks(backend, n=20)
        peak_half = float(np.max(np.abs(_run_blocks(backend, n=2))))

        # Half velocity should produce roughly half the amplitude.
        ratio = peak_half / peak_full
        assert 0.4 < ratio < 0.6, f"velocity ratio {ratio:.2f} out of expected band"

    def test_velocity_sensitive_off_ignores_velocity(self):
        patch, midi = _build_simple_patch()
        midi.params["volume"] = 1.0
        midi.params["velocity_sensitive"] = False
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)

        midi.note_on(69, 0.3)  # low velocity
        _ = _run_blocks(backend, n=20)
        peak = float(np.max(np.abs(_run_blocks(backend, n=2))))
        # With velocity ignored, peak should still hit ~volume (1.0).
        assert peak > 0.9

    def test_gate_high_when_notes_held(self):
        _, midi = _build_simple_patch()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        midi.note_on(60, 1.0)
        result = backend._render_midi_input(midi, 512)
        assert float(np.max(result["gate"])) == pytest.approx(1.0)
        assert float(np.min(result["gate"])) == pytest.approx(1.0)

    def test_gate_low_when_idle(self):
        _, midi = _build_simple_patch()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert float(np.max(result["gate"])) == pytest.approx(0.0)


    def test_pitch_cv_emitted_when_centered(self):
        # Wheel at rest -> pitch_cv buffer is all zeros, same length as frames.
        _, midi = _build_simple_patch()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert "pitch_cv" in result
        assert result["pitch_cv"].shape == (512,)
        assert float(np.max(np.abs(result["pitch_cv"]))) == 0.0

    def test_pitch_cv_value_matches_bend_range(self):
        # bend=+1.0, range=2 semitones -> cv = 2/12 = 0.16666...
        _, midi = _build_simple_patch()
        midi.params["bend_range"] = 2.0
        midi.set_pitch_bend(1.0)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        expected = 2.0 / 12.0
        assert abs(float(result["pitch_cv"][0]) - expected) < 1e-6
        # Block-constant - check the last sample too.
        assert abs(float(result["pitch_cv"][-1]) - expected) < 1e-6

    def test_pitch_cv_scales_with_bend_range(self):
        # Larger bend_range -> larger cv at full deflection.
        _, midi = _build_simple_patch()
        midi.params["bend_range"] = 12.0  # one octave each way
        midi.set_pitch_bend(1.0)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        # 12 semitones / 12 = 1.0 (one octave in 1V/oct units)
        assert abs(float(result["pitch_cv"][0]) - 1.0) < 1e-6

    def test_bend_shifts_internal_pitch(self):
        # A note rendered with the wheel up should have audibly higher
        # frequency than the same note with the wheel at rest. Detect
        # via zero-crossing rate: higher freq -> more zero crossings.
        patch, midi = _build_simple_patch()
        midi.params["waveform"] = "sine"
        midi.params["volume"] = 0.7
        midi.params["bend_range"] = 12.0  # exaggerate so the test is robust
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        midi.note_on(60, 1.0)
        _ = _run_blocks(backend, n=20)  # let envelope settle

        midi.set_pitch_bend(0.0)
        flat = _run_blocks(backend, n=2)[:, 0]
        midi.set_pitch_bend(1.0)
        bent = _run_blocks(backend, n=2)[:, 0]

        def zero_crossings(x):
            return int(np.sum(np.diff(np.signbit(x)) != 0))

        flat_zc = zero_crossings(flat)
        bent_zc = zero_crossings(bent)
        # Up an octave should roughly double the zero-crossing count.
        # Allow generous slack since the block straddles the bend change.
        assert bent_zc > flat_zc * 1.4, (
            f"pitch bend did not shift internal frequency: flat={flat_zc} bent={bent_zc}"
        )


    def test_mod_cv_zero_when_idle(self):
        # Wheel at rest -> mod_cv buffer is all zeros, same length as frames.
        _, midi = _build_simple_patch()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert "mod_cv" in result
        assert result["mod_cv"].shape == (512,)
        assert float(np.max(np.abs(result["mod_cv"]))) == 0.0

    def test_mod_cv_matches_mod_scale(self):
        # wheel=1.0, mod_scale=2.0 -> mod_cv buffer is filled with 2.0.
        _, midi = _build_simple_patch()
        midi.params["mod_scale"] = 2.0
        midi.set_mod_wheel(1.0)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert abs(float(result["mod_cv"][0]) - 2.0) < 1e-6
        # Block-constant - check the last sample too.
        assert abs(float(result["mod_cv"][-1]) - 2.0) < 1e-6

    def test_mod_cv_default_scale_is_passthrough(self):
        # Default mod_scale=1.0, so cv tracks the wheel value verbatim.
        _, midi = _build_simple_patch()
        midi.set_mod_wheel(0.4)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert abs(float(result["mod_cv"][0]) - 0.4) < 1e-6

    def test_mod_wheel_does_not_affect_internal_audio(self):
        # Unlike pitch bend, mod wheel only emits cv - it does not modify
        # the internal voice frequencies. Set the wheel and confirm the
        # audio peak doesn't change beyond rendering noise.
        patch, midi = _build_simple_patch()
        midi.params["waveform"] = "sine"
        midi.params["volume"] = 0.7
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        midi.note_on(60, 1.0)
        _ = _run_blocks(backend, n=20)

        midi.set_mod_wheel(0.0)
        peak_idle = float(np.max(np.abs(_run_blocks(backend, n=2))))
        midi.set_mod_wheel(1.0)
        peak_full = float(np.max(np.abs(_run_blocks(backend, n=2))))

        assert abs(peak_full - peak_idle) < 0.02, (
            f"mod wheel should not affect audio: idle={peak_idle:.4f} full={peak_full:.4f}"
        )


    def test_pressure_cv_zero_when_idle(self):
        _, midi = _build_simple_patch()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert "pressure_cv" in result
        assert result["pressure_cv"].shape == (512,)
        assert float(np.max(np.abs(result["pressure_cv"]))) == 0.0

    def test_pressure_cv_matches_pressure_scale(self):
        _, midi = _build_simple_patch()
        midi.params["pressure_scale"] = 2.5
        midi.set_aftertouch(1.0)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert abs(float(result["pressure_cv"][0]) - 2.5) < 1e-6
        assert abs(float(result["pressure_cv"][-1]) - 2.5) < 1e-6

    def test_pressure_cv_default_scale_is_passthrough(self):
        _, midi = _build_simple_patch()
        midi.set_aftertouch(0.7)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        result = backend._render_midi_input(midi, 512)
        assert abs(float(result["pressure_cv"][0]) - 0.7) < 1e-6

    def test_aftertouch_does_not_affect_internal_audio(self):
        # Like mod wheel, aftertouch only emits cv - it does not modify
        # the internal voice frequencies or volumes.
        patch, midi = _build_simple_patch()
        midi.params["waveform"] = "sine"
        midi.params["volume"] = 0.7
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        midi.note_on(60, 1.0)
        _ = _run_blocks(backend, n=20)

        midi.set_aftertouch(0.0)
        peak_idle = float(np.max(np.abs(_run_blocks(backend, n=2))))
        midi.set_aftertouch(1.0)
        peak_full = float(np.max(np.abs(_run_blocks(backend, n=2))))
        assert abs(peak_full - peak_idle) < 0.02


# ---------------------------------------------------------------------------
# Optional-dep guardrails
# ---------------------------------------------------------------------------


class TestOptionalDep:
    def test_available_devices_returns_list(self):
        # Even when mido is absent the function returns [], not raises.
        result = available_devices()
        assert isinstance(result, list)

    def test_start_midi_without_mido_does_not_crash(self):
        # If mido is missing this is a no-op with a log warning; if mido
        # is present but no hardware is plugged in we get the empty-list
        # path. Either way, no exception should escape.
        m = MIDIInput(module_id=1)
        m.start_midi()  # should not raise
        m.stop_midi()  # should not raise
