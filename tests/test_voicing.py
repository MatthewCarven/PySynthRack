"""Tests for the VoiceSlots polyphonic voice allocator.

VoiceSlots is the model-layer foundation for the v0.4 voice-routing
slice. These tests cover allocation, retrigger, release, sustain
pedal, voice steal, and snapshot semantics — everything the audio-
thread renderer will rely on for stable per-slot state continuity.

VoiceSlots does NOT own a lock; the owning module (MIDIInput) is
expected to hold its own lock around every mutation. These tests
exercise the data structure directly, single-threaded.
"""
from __future__ import annotations

import pytest

from pysynthrack.core.voicing import VoiceSlots, MAX_VOICES


class TestAllocation:
    def test_initial_state_all_empty(self):
        v = VoiceSlots()
        assert len(v.slots) == MAX_VOICES
        snap = v.snapshot()
        assert all(s["note"] == -1 for s in snap)
        assert all(not s["gating"] for s in snap)

    def test_first_note_lands_in_slot_zero(self):
        v = VoiceSlots()
        idx = v.allocate(60, 0.8)
        assert idx == 0
        snap = v.snapshot()
        assert snap[0]["note"] == 60
        assert snap[0]["velocity"] == 0.8
        assert snap[0]["held"] is True
        assert snap[0]["gating"] is True

    def test_distinct_notes_fill_consecutive_slots(self):
        v = VoiceSlots()
        for offset, note in enumerate((60, 64, 67)):
            assert v.allocate(note, 1.0) == offset
        snap = v.snapshot()
        assert snap[0]["note"] == 60
        assert snap[1]["note"] == 64
        assert snap[2]["note"] == 67
        # Remaining slots untouched.
        for s in snap[3:]:
            assert s["note"] == -1

    def test_retrigger_held_note_reuses_slot(self):
        # If the same note is allocated while still held, we reuse the
        # slot — this keeps the renderer's per-voice state continuous.
        v = VoiceSlots()
        idx_first = v.allocate(60, 0.5)
        idx_again = v.allocate(60, 1.0)
        assert idx_first == idx_again
        # Velocity should update to the new value.
        assert v.snapshot()[idx_again]["velocity"] == 1.0

    def test_released_note_reallocation_takes_fresh_slot(self):
        # Once a key is released, replaying the same note allocates a
        # FRESH slot — the previous voice keeps its release tail.
        v = VoiceSlots()
        idx_first = v.allocate(60, 1.0)
        v.release(60)
        idx_again = v.allocate(60, 1.0)
        assert idx_first != idx_again
        # The original slot still has note 60 marked, just not held.
        snap = v.snapshot()
        assert snap[idx_first]["note"] == 60
        assert snap[idx_first]["held"] is False
        assert snap[idx_again]["note"] == 60
        assert snap[idx_again]["held"] is True


class TestRelease:
    def test_release_marks_slot_unheld(self):
        v = VoiceSlots()
        idx = v.allocate(60, 1.0)
        v.release(60)
        snap = v.snapshot()
        assert snap[idx]["held"] is False
        assert snap[idx]["sustained"] is False
        assert snap[idx]["gating"] is False
        # Note number stays on the slot until it's stolen.
        assert snap[idx]["note"] == 60

    def test_release_of_unheld_note_is_silent(self):
        # No-op rather than raising — at the MIDI layer note_offs for
        # notes we never saw are common (race with the previous patch).
        v = VoiceSlots()
        v.release(60)
        assert all(s["note"] == -1 for s in v.snapshot())

    def test_release_only_affects_held_slot(self):
        # If the same note exists in two slots (one releasing, one
        # held — see test_released_note_reallocation_takes_fresh_slot),
        # release() must target the HELD instance.
        v = VoiceSlots()
        idx_old = v.allocate(60, 1.0)
        v.release(60)
        idx_new = v.allocate(60, 1.0)
        v.release(60)
        snap = v.snapshot()
        # Both slots should now be unheld for note 60.
        assert snap[idx_old]["held"] is False
        assert snap[idx_new]["held"] is False


class TestSustainPedal:
    def test_pedal_default_off(self):
        v = VoiceSlots()
        assert v.sustain_pedal is False

    def test_release_with_pedal_down_marks_sustained(self):
        # Key released while pedal down → slot stays gating.
        v = VoiceSlots()
        v.set_sustain(True)
        idx = v.allocate(60, 1.0)
        v.release(60)
        snap = v.snapshot()
        assert snap[idx]["held"] is False
        assert snap[idx]["sustained"] is True
        assert snap[idx]["gating"] is True

    def test_release_with_pedal_up_does_not_sustain(self):
        v = VoiceSlots()
        idx = v.allocate(60, 1.0)
        v.release(60)
        assert v.snapshot()[idx]["sustained"] is False

    def test_pedal_up_releases_sustained_voices(self):
        # Press, hold pedal, release key (slot now sustained), lift
        # pedal — slot transitions to fully released.
        v = VoiceSlots()
        v.set_sustain(True)
        idx = v.allocate(60, 1.0)
        v.release(60)
        assert v.snapshot()[idx]["gating"] is True
        v.set_sustain(False)
        snap = v.snapshot()
        assert snap[idx]["sustained"] is False
        assert snap[idx]["held"] is False
        assert snap[idx]["gating"] is False

    def test_pedal_does_not_affect_held_voices(self):
        # Holding the pedal while a key is still down must not change
        # that slot's held flag — it's still being physically held.
        v = VoiceSlots()
        idx = v.allocate(60, 1.0)
        v.set_sustain(True)
        snap = v.snapshot()
        assert snap[idx]["held"] is True
        assert snap[idx]["sustained"] is False  # only relevant after note_off
        # Now release the key — pedal should catch it.
        v.release(60)
        assert v.snapshot()[idx]["sustained"] is True

    def test_pedal_down_then_play_then_pedal_up(self):
        # Press pedal first, then play a note, then release the note,
        # then lift the pedal. The classic "puddle of pedal" workflow.
        v = VoiceSlots()
        v.set_sustain(True)
        idx = v.allocate(60, 1.0)
        v.release(60)
        # Sustained.
        assert v.snapshot()[idx]["gating"] is True
        # Lift pedal.
        v.set_sustain(False)
        assert v.snapshot()[idx]["gating"] is False


class TestVoiceSteal:
    def test_steal_picks_oldest_released_first(self):
        v = VoiceSlots()
        # Fill all 16 slots.
        for n in range(60, 60 + MAX_VOICES):
            v.allocate(n, 1.0)
        # Release a few, in order.
        v.release(60)  # oldest released
        v.release(61)
        v.release(62)
        # All slots occupied (note != -1) but three are released.
        # Allocate a new note — should steal the OLDEST released
        # (slot 0, note 60, lowest age among released).
        stolen_idx = v.allocate(99, 1.0)
        assert stolen_idx == 0
        snap = v.snapshot()
        assert snap[0]["note"] == 99
        assert snap[0]["held"] is True
        # The other released ones (slots 1, 2) are still releasing.
        assert snap[1]["note"] == 61
        assert snap[1]["held"] is False
        assert snap[2]["note"] == 62
        assert snap[2]["held"] is False

    def test_steal_prefers_released_over_sustained(self):
        # With one released voice and one sustained, the released one
        # gets stolen first.
        v = VoiceSlots()
        for n in range(60, 60 + MAX_VOICES):
            v.allocate(n, 1.0)
        # Make slot 0 sustained, slot 1 released.
        v.set_sustain(True)
        v.release(60)  # slot 0 sustained
        v.set_sustain(False)
        # Now slot 0 is released (sustained → released on pedal-up).
        # Re-arm sustain and release a different key.
        v.set_sustain(True)
        v.release(61)  # slot 1 sustained
        # Allocate a new note — released slot 0 should be stolen
        # (it's older than the sustained one).
        stolen_idx = v.allocate(99, 1.0)
        assert stolen_idx == 0

    def test_steal_falls_through_to_held(self):
        # All 16 keys physically down, no pedal. New note must steal
        # the oldest held voice — there's nowhere else to go.
        v = VoiceSlots()
        for n in range(60, 60 + MAX_VOICES):
            v.allocate(n, 1.0)
        # Every slot held.
        snap_before = v.snapshot()
        assert all(s["held"] for s in snap_before)
        stolen_idx = v.allocate(99, 1.0)
        # Should be slot 0 — oldest age.
        assert stolen_idx == 0
        assert v.snapshot()[0]["note"] == 99


class TestPanic:
    def test_all_notes_off_clears_every_slot(self):
        v = VoiceSlots()
        for n in (60, 64, 67):
            v.allocate(n, 1.0)
        v.all_notes_off()
        snap = v.snapshot()
        assert all(s["note"] == -1 for s in snap)
        assert all(not s["gating"] for s in snap)

    def test_all_notes_off_clears_sustained_voices(self):
        # Panic must clear sustained voices too — it's a "shut up
        # entirely" message.
        v = VoiceSlots()
        v.set_sustain(True)
        v.allocate(60, 1.0)
        v.release(60)  # sustained
        v.all_notes_off()
        assert all(not s["gating"] for s in v.snapshot())

    def test_all_notes_off_does_not_reset_pedal(self):
        # MIDI CC 123 (All Notes Off) doesn't touch physical-control
        # state. The pedal stays where the player left it.
        v = VoiceSlots()
        v.set_sustain(True)
        v.all_notes_off()
        assert v.sustain_pedal is True


class TestHeldNotesView:
    def test_held_notes_returns_only_held(self):
        v = VoiceSlots()
        v.allocate(60, 0.5)
        v.allocate(64, 1.0)
        v.release(64)
        # 64 is releasing — not held.
        assert v.held_notes() == {60: 0.5}

    def test_sustained_voices_are_not_held(self):
        # A note kept alive only by the pedal does NOT count as held.
        v = VoiceSlots()
        v.set_sustain(True)
        v.allocate(60, 1.0)
        v.release(60)
        assert v.held_notes() == {}


class TestSnapshot:
    def test_snapshot_is_always_full_length(self):
        v = VoiceSlots()
        snap = v.snapshot()
        assert len(snap) == MAX_VOICES
        v.allocate(60, 1.0)
        assert len(v.snapshot()) == MAX_VOICES

    def test_snapshot_is_a_copy(self):
        # Mutating a returned snapshot must not affect the allocator.
        v = VoiceSlots()
        v.allocate(60, 1.0)
        snap = v.snapshot()
        snap[0]["note"] = 99  # type: ignore[typeddict-item]
        snap[5]["gating"] = True  # type: ignore[typeddict-item]
        snap2 = v.snapshot()
        assert snap2[0]["note"] == 60
        assert snap2[5]["gating"] is False

    def test_gating_collapses_held_and_sustained(self):
        v = VoiceSlots()
        v.set_sustain(True)
        idx_held = v.allocate(60, 1.0)
        idx_sus = v.allocate(64, 1.0)
        v.release(64)
        snap = v.snapshot()
        assert snap[idx_held]["gating"] is True   # held
        assert snap[idx_sus]["gating"] is True    # sustained
