"""CVKeyboard module — computer keys as a polyphonic CV/gate controller.

Where :class:`Keyboard` is a self-contained *sound source* (each key
synthesizes a waveform internally and the module emits audio), CVKeyboard
is a *controller*: the keys emit control voltage only, and you build the
voice yourself out in the patch (oscillator -> filter -> VCA -> whatever).
That's the whole point — the same keys can drive a different sound every
patch, exactly like a hardware modular keyboard putting out 1V/oct + gate.

The two share their entire input side: a thread-safe 16-slot
:class:`VoiceSlots` allocator that the UI mutates on DearPyGui key-press /
key-release events. Both classes set ``ACCEPTS_COMPUTER_KEYS = True`` so
the UI routes physical keystrokes to either without caring about the
concrete type. The note-ingest methods (:meth:`note_on`, :meth:`note_off`,
:meth:`all_notes_off`, :meth:`snapshot_voice_slots`) are copied verbatim
from Keyboard rather than inherited, so CVKeyboard does not also drag in
Keyboard's audio params (``waveform``/``volume``) or its audio ``out``
port — the only thing the two genuinely share is "computer keys mutate a
VoiceSlots allocator", and that's a handful of lines.

Outputs (all voice-aware where it matters):

  * ``pitch_cv`` (cv, per-voice ``(MAX_VOICES, frames)``) — the held note
    expressed as a 1V/octave control voltage with **C4 (MIDI 60) = 0 V**.
    Each semitone is 1/12. Wire it into an :class:`Oscillator`'s
    ``freq_cv`` (set the oscillator's base ``freq`` to C4 = 261.6256 Hz to
    track in tune) or into :class:`CVToFrequency`. Pitch persists through a
    voice's release tail (it only zeroes when the slot is reused), so an
    ADSR release stays on pitch. A mono consumer that sums the voice axis
    will get nonsense (summed pitch CV), exactly the same caveat MIDIInput's
    ``pitch_cv`` already carries — feed voice-aware consumers.
  * ``gate`` (gate, per-voice ``(MAX_VOICES, frames)``) — high while the
    key is physically held, falling on release. One gate per voice slot, so
    a per-voice ADSR fires one envelope per note (no chord retrigger).
  * twelve per-pitch-class gate jacks ``key_c`` .. ``key_b`` (gate, mono
    ``(frames,)``) — "all the keys are CV outs". Each is high while ANY held
    voice is that pitch class (octave-folded: pressing C4 or C5 both raise
    ``key_c``). Patch ``key_c`` into one drum/voice, ``key_e`` into another,
    etc. — a different sound per key. These are channel-wide booleans, so
    they stay 1D like MIDIInput's mod/pressure CV.

No velocity (a computer keyboard can't express it — same as Keyboard), no
pitch wheel, no internal oscillator. The richer hardware controls live on
:class:`MIDIInput`.
"""
from __future__ import annotations

import threading

from ..core.module import Module, register_module_type
from ..core.port import Port
from ..core.voicing import VoiceSlots, VoiceSnapshot

# Re-exported so callers (UI, tests) can keep importing the note helpers
# from one place; CVKeyboard reuses Keyboard's note math rather than
# duplicating it.
from .keyboard import midi_to_name, semitone_to_midi  # noqa: F401

# 1V/octave reference: the note that maps to 0 V on ``pitch_cv``. C4 is the
# conventional musical anchor (middle C) and matches midi_to_freq's centre.
CV_REFERENCE_NOTE = 60  # MIDI C4

# Per-pitch-class gate output names, indexed by ``note % 12`` (0 = C). Mirrors
# the note-name order in keyboard._NOTE_NAMES (sharps spelled "s", e.g. C# ->
# "cs"). The renderer and the OUTPUT_PORTS list below both walk this tuple, so
# the jack count is data-driven from one place.
KEY_GATE_NAMES = (
    "key_c", "key_cs", "key_d", "key_ds", "key_e", "key_f",
    "key_fs", "key_g", "key_gs", "key_a", "key_as", "key_b",
)


@register_module_type
class CVKeyboard(Module):
    """Polyphonic computer-keyboard CV/gate controller.

    Parameters:
        octave: Base octave for the home row (default 4 → home row starts
            at C4, the same mapping :class:`Keyboard` uses). The UI reads
            this to turn a physical key into a MIDI note.

    Runtime state (not serialized):
        voices: :class:`VoiceSlots` allocator backing the held-note state,
            identical to :class:`Keyboard`'s. The slot index is the
            addressable voice id the voice-aware renderer keys off.
    """

    TYPE = "cv_keyboard"
    # Marks this as a module the UI feeds physical key events to. Set on
    # Keyboard too; the UI routes by this flag, not by concrete type.
    ACCEPTS_COMPUTER_KEYS = True
    DEFAULT_PARAMS = {
        "octave": 4,
    }
    INPUT_PORTS: list[Port] = []
    # Voice-aware pitch_cv + gate (per-slot (MAX_VOICES, frames)), then the
    # twelve per-pitch-class gate jacks (mono). Built from KEY_GATE_NAMES so
    # the port list and the renderer never drift apart.
    OUTPUT_PORTS = [
        Port("pitch_cv", "out", "cv"),
        Port("gate", "out", "gate"),
        *[Port(name, "out", "gate") for name in KEY_GATE_NAMES],
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Same 16-slot allocator Keyboard uses; the UI mutates it via the
        # note_on/note_off methods below on key events.
        self.voices: VoiceSlots = VoiceSlots()
        self._lock = threading.Lock()

    # ----- transport (copied from Keyboard) -------------------------------

    def note_on(self, midi_note: int) -> None:
        """Press a key. Allocates (or retriggers) a voice slot at unit
        velocity (a computer keyboard can't express velocity)."""
        with self._lock:
            self.voices.allocate(int(midi_note), 1.0)

    def note_off(self, midi_note: int) -> None:
        """Release a key. The slot transitions to released; its pitch_cv
        holds until the slot is reused so the release tail stays in tune."""
        with self._lock:
            self.voices.release(int(midi_note))

    def all_notes_off(self) -> None:
        """Panic — clear every slot to empty."""
        with self._lock:
            self.voices.all_notes_off()

    def snapshot_active_notes(self) -> set[int]:
        """Return the set of currently-held MIDI notes (held keys only —
        a released-but-tailing slot does not appear here)."""
        with self._lock:
            return set(self.voices.held_notes().keys())

    def snapshot_voice_slots(self) -> list[VoiceSnapshot]:
        """Return a 16-element per-slot snapshot for the voice-aware
        renderer. Empty slots have ``note=-1`` and ``gating=False``."""
        with self._lock:
            return self.voices.snapshot()
