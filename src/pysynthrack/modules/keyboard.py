"""Keyboard module — computer keys play notes polyphonically.

A Keyboard owns a thread-safe 16-slot :class:`VoiceSlots` allocator. The
UI mutates that allocator in response to DearPyGui key-press / key-
release events; the audio backend reads it each block to decide which
voices to render.

Each voice synthesizes one waveform at the note's frequency, with its
own short attack/release ramp to prevent clicks on note-on / note-off.
A full ADSR is a separate module.

Voice-aware output (slice 4, 2026-05-23).  The renderer emits per-slot
``(MAX_VOICES, frames)`` buffers on both ``out`` and ``gate`` -- the
same shape MIDIInput already publishes. Downstream voice-aware modules
(ADSR, VCA, Filter, Oscillator, LFO, Crossover) carry the per-voice
identity through to the speaker, where the implicit sum at the mono
sink mixes them back to stereo. Un-migrated mono consumers see a
collapsed-to-1D view via ``_input_buffer``'s default ``collapse=True``,
so older patches still work.

Public API stays narrow on purpose: Keyboard is computer-keyboard
input, not a MIDI controller. No velocity (every note is unit gain),
no pitch wheel, no sustain pedal, no mod CV. The richer controls live
on :class:`MIDIInput`. The two modules share the same renderer shape
and ``VoiceSlots`` plumbing, so anything voice-aware downstream
behaves identically regardless of the source.
"""
from __future__ import annotations

import threading

from ..core.module import Module, register_module_type
from ..core.port import Port
from ..core.voicing import VoiceSlots, VoiceSnapshot


# Standard MIDI: C4 (middle C) = note 60. The formula below puts:
#   octave=4, semitone=0  → note 60 (C4)
#   octave=4, semitone=9  → note 69 (A4 = 440 Hz)
def semitone_to_midi(octave: int, semitone: int) -> int:
    return 12 * (octave + 1) + semitone


def midi_to_freq(midi_note: int) -> float:
    """A4 (note 69) = 440 Hz. Twelve-tone equal temperament."""
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def midi_to_name(midi_note: int) -> str:
    """e.g. 60 → 'C4', 69 → 'A4'."""
    octave = (midi_note // 12) - 1
    return f"{_NOTE_NAMES[midi_note % 12]}{octave}"


@register_module_type
class Keyboard(Module):
    """Polyphonic computer-keyboard input.

    Parameters:
        octave: Base octave for the home row (default 4 → home row starts at C4).
        waveform: Voice waveform. Naive sine / saw / square /
            triangle, PolyBLEP/PolyBLAMP *_blep, or wavetable *_wt
            (see oscillator.WAVEFORMS).
        volume: Master output level in [0, 1].

    Runtime state (not serialized):
        voices: :class:`VoiceSlots` allocator backing the held-note
            state. Held notes and released-but-not-yet-stolen voices
            both live here. The slot index is what the voice-aware
            renderer keys its per-voice state off (phase, envelope,
            last-note for slot-reassignment detection).
    """

    TYPE = "keyboard"
    # Marks this as a module the UI feeds physical key events to.
    # CVKeyboard sets the same flag; the UI routes by this flag rather
    # than by concrete type (see ui/app.py key handlers).
    ACCEPTS_COMPUTER_KEYS = True
    DEFAULT_PARAMS = {
        "octave": 4,
        "waveform": "sine",
        "volume": 0.5,
    }
    INPUT_PORTS: list[Port] = []
    # Voice-aware outputs (slice 4): the renderer emits per-slot
    # ``(MAX_VOICES, frames)`` buffers on both ports. Mirrors the
    # MIDIInput shape so anything downstream that already handles
    # MIDIInput handles Keyboard identically.
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("gate", "out", "gate"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 16-slot voice allocator. Replaces the flat ``active_notes``
        # set that earlier versions used -- the slot index is what the
        # voice-aware renderer keys its per-voice state off.
        # ``snapshot_active_notes()`` is preserved as a thin proxy over
        # ``voices.held_notes()`` so existing UI callers and tests
        # don't notice the change.
        self.voices: VoiceSlots = VoiceSlots()
        self._lock = threading.Lock()

    # ----- transport ------------------------------------------------------

    def note_on(self, midi_note: int) -> None:
        """Press a key. Allocates (or retriggers) a voice slot.

        Keyboard always allocates at unit velocity -- the velocity
        param exists on :class:`MIDIInput` because hardware controllers
        send it, but a computer keyboard has no way to express it.
        """
        with self._lock:
            self.voices.allocate(int(midi_note), 1.0)

    def note_off(self, midi_note: int) -> None:
        """Release a key. The slot transitions to released; its
        release tail keeps playing until the slot is reused."""
        with self._lock:
            self.voices.release(int(midi_note))

    def all_notes_off(self) -> None:
        """Panic -- clear every slot to empty. Used for stop-transport
        and the GUI's escape-from-stuck-notes shortcut."""
        with self._lock:
            self.voices.all_notes_off()

    def snapshot_active_notes(self) -> set[int]:
        """Return the set of currently-held MIDI notes.

        Stable across the slice-4 migration: the UI keeps treating
        this as ``set[int]``. Only physically-held keys appear here --
        a slot that's been released but is still emitting its tail
        does NOT appear in this set (the key is up).
        """
        with self._lock:
            return set(self.voices.held_notes().keys())

    def snapshot_voice_slots(self) -> list[VoiceSnapshot]:
        """Return a 16-element per-slot snapshot for the voice-aware
        renderer. Slot index is the addressable voice id; empty slots
        are present with ``note=-1`` and ``gating=False``. Mirrors
        :meth:`MIDIInput.snapshot_voice_slots`."""
        with self._lock:
            return self.voices.snapshot()
