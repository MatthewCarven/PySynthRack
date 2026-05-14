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

    def test_outputs_audio_and_gate(self):
        m = MIDIInput(module_id=1)
        ports = [(p.name, p.signal_kind) for p in m.OUTPUT_PORTS]
        assert ports == [("out", "audio"), ("gate", "gate")]

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
        # CC 64 (sustain) is intentionally not handled in this slice;
        # it must not clear active notes.
        m._on_message(
            mido.Message("control_change", control=64, value=127, channel=0)
        )
        assert m.snapshot_active_notes() == {60: 1.0}

    def test_pitchwheel_is_ignored(self):
        # Pitch bend isn't wired yet — landing it would need a freq_cv
        # output. For now it should be silently dropped.
        m = MIDIInput(module_id=1)
        m._on_message(mido.Message("pitchwheel", pitch=8000, channel=0))
        assert m.snapshot_active_notes() == {}


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
