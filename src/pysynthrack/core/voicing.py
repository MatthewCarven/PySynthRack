"""VoiceSlots — 16-slot polyphonic voice allocator.

Used by polyphonic note sources (MIDIInput, Keyboard) to map held MIDI
notes to stable slot indices 0..15. The slot index is what downstream
voice-aware renderers key their per-voice state off (oscillator phase,
ADSR stage, filter biquad memory, etc.), so a single voice's state
stays continuous across audio blocks as long as that voice occupies the
same slot.

Slot lifecycle
--------------
A slot is in exactly one of these states at any given time:

* **Empty.**  ``note == -1`` — never been used, or freed by panic.
* **Held.**  Key is currently down. ``held=True``, ``sustained=False``.
* **Sustained.**  Key was released while the sustain pedal was down.
  ``held=False``, ``sustained=True``. The renderer keeps the gate high
  here, exactly as if the key were still pressed.
* **Released.**  Key released, sustain not engaged on this slot.
  ``held=False``, ``sustained=False``, but ``note != -1`` so the
  renderer can keep emitting the release tail using state attached
  to this slot index. The slot becomes available for stealing.

Slots only return to empty via :meth:`all_notes_off` (panic) or by
being stolen for a new note. A released slot continues to be addressed
by its prior ``note`` so re-playing the same note allocates a fresh
slot rather than retriggering the dying voice.

Voice steal
-----------
When all 16 slots are occupied and a new note arrives,
:meth:`allocate` evicts in this order:

1. Oldest released slot (key up, no sustain).
2. Oldest sustained slot (key up, pedal down).
3. Oldest held slot (key down). Worst case — the user is holding more
   than 16 keys at once, which is physically rare on a single keyboard.

"Oldest" means the slot with the lowest ``age`` — a monotonically
increasing counter incremented every time a slot is (re)allocated.

Threading
---------
This class does NOT hold its own lock. The owner (MIDIInput) holds
``self._lock`` around every public method here, including
:meth:`snapshot`. That keeps lock ownership single-sourced and avoids
nested-lock pitfalls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

MAX_VOICES = 16


@dataclass
class _Slot:
    """One voice slot. ``note == -1`` means the slot has never been used."""

    note: int = -1
    velocity: float = 0.0
    held: bool = False
    sustained: bool = False
    # Monotonic timestamp of last allocation. Used to pick the oldest
    # slot when stealing. 0 == never allocated.
    age: int = 0


class VoiceSnapshot(TypedDict):
    """Per-slot view returned by :meth:`VoiceSlots.snapshot`.

    The renderer reads this off the audio thread. ``gating`` collapses
    held + sustained into the single "should the gate signal be high
    on this block?" answer the envelope generator cares about.
    """

    note: int
    velocity: float
    held: bool
    sustained: bool
    gating: bool


class VoiceSlots:
    """16-slot polyphonic voice allocator.

    All mutation methods assume the caller holds the appropriate lock
    (see module docstring). The class itself is pure data.
    """

    MAX_VOICES = MAX_VOICES

    def __init__(self) -> None:
        self.slots: list[_Slot] = [_Slot() for _ in range(MAX_VOICES)]
        self._age_counter: int = 0
        # Sustain pedal (MIDI CC 64) — True while the pedal is depressed.
        # On note_off while True, the affected slot transitions to
        # ``sustained`` instead of ``released``. On pedal-up, all
        # sustained slots transition to released in one shot.
        self.sustain_pedal: bool = False

    # ----- allocation -----------------------------------------------------

    def allocate(self, note: int, velocity: float) -> int:
        """Allocate (or retrigger) a slot for ``note`` and return its index.

        If ``note`` is already held in some slot, that slot is returned
        with the velocity updated — retriggering a held key does not
        consume a fresh voice. Otherwise the first empty slot wins; if
        none is empty, voice-steal kicks in (see class docstring).
        """
        # Retrigger an already-held slot.
        for idx, slot in enumerate(self.slots):
            if slot.note == note and slot.held:
                slot.velocity = velocity
                self._age_counter += 1
                slot.age = self._age_counter
                return idx

        # First empty slot.
        for idx, slot in enumerate(self.slots):
            if slot.note == -1:
                return self._fill(idx, note, velocity)

        # Steal: oldest released first, then oldest sustained, then
        # oldest held. Within each tier, smaller ``age`` wins.
        def _oldest(predicate) -> int | None:
            candidates = [
                (idx, slot.age)
                for idx, slot in enumerate(self.slots)
                if predicate(slot)
            ]
            if not candidates:
                return None
            return min(candidates, key=lambda iv: iv[1])[0]

        idx = _oldest(lambda s: not s.held and not s.sustained)
        if idx is None:
            idx = _oldest(lambda s: s.sustained)
        if idx is None:
            # All slots held. Pick the oldest held.
            idx = min(range(MAX_VOICES), key=lambda i: self.slots[i].age)
        return self._fill(idx, note, velocity)

    def _fill(self, idx: int, note: int, velocity: float) -> int:
        self._age_counter += 1
        slot = self.slots[idx]
        slot.note = note
        slot.velocity = velocity
        slot.held = True
        slot.sustained = False
        slot.age = self._age_counter
        return idx

    # ----- release --------------------------------------------------------

    def release(self, note: int) -> None:
        """Key released for ``note``.

        If the sustain pedal is down, the slot transitions to
        ``sustained`` (gate stays high). Otherwise it drops to released
        — the renderer can keep emitting its envelope tail until the
        slot is reused. A note that isn't currently held is silently
        ignored (it may be releasing already, or never have arrived).
        """
        for slot in self.slots:
            if slot.note == note and slot.held:
                slot.held = False
                if self.sustain_pedal:
                    slot.sustained = True

    def set_sustain(self, on: bool) -> None:
        """Sustain pedal up/down (MIDI CC 64).

        On pedal-up, every sustained slot transitions to released in a
        single pass — the gate falls on those voices on the next block.
        Held slots are unaffected by either edge.
        """
        self.sustain_pedal = bool(on)
        if not self.sustain_pedal:
            for slot in self.slots:
                if slot.sustained:
                    slot.sustained = False
                    # ``held`` is already False (we got here via
                    # release()); the slot is now plain released.

    def all_notes_off(self) -> None:
        """Panic — clear every slot to empty.

        Mirrors the MIDI CC 123 contract: held-note state is cleared
        but physical-control state (sustain pedal, mod wheel, pitch
        bend) is NOT reset here. The caller resets pedal state
        separately if appropriate (typically only on ``stop_midi``).
        """
        for slot in self.slots:
            slot.note = -1
            slot.velocity = 0.0
            slot.held = False
            slot.sustained = False
            slot.age = 0

    # ----- read-side helpers (for tests and the audio thread) -------------

    def held_notes(self) -> dict[int, float]:
        """``{note: velocity}`` for slots whose key is currently down.

        Mirrors the pre-voice-routing ``active_notes`` semantics. A note
        that's only sustained (key up, pedal down) does NOT appear here
        — it's no longer being held by a finger. Used by
        ``snapshot_active_notes`` to keep that public API stable.
        """
        return {
            slot.note: slot.velocity
            for slot in self.slots
            if slot.note != -1 and slot.held
        }

    def snapshot(self) -> list[VoiceSnapshot]:
        """Return a thread-safe per-slot copy for the audio renderer.

        The list is always length ``MAX_VOICES`` and slot ``i`` is
        always at index ``i``; empty slots are present with ``note=-1``
        and ``gating=False``. The renderer can iterate this as a fixed
        loop of 16 without bookkeeping for "which slots are alive".
        """
        return [
            VoiceSnapshot(
                note=slot.note,
                velocity=slot.velocity,
                held=slot.held,
                sustained=slot.sustained,
                gating=slot.note != -1 and (slot.held or slot.sustained),
            )
            for slot in self.slots
        ]
