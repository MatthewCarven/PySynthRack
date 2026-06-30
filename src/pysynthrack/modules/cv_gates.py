"""CVGates module — computer keys as a bank of enveloped CV gates.

Where :class:`CVKeyboard` turns the computer keyboard into a 1V/octave
*pitch* controller, CVGates ignores pitch entirely: it gives every physical
key its own CV output that idles at 0 V and, while the key is held, rises
through a shared ADSR envelope toward 1. It is an *amplitude / trigger*
controller — patch one key's output into the ``amp_cv`` of three
oscillators (or three VCAs) and a single keystroke envelopes all three
together, exactly like a modular gate fanned out across a mult. (Fan-out is
free: the patch model already lets one output port feed any number of
cables, so you just drag three cables off the one jack.)

The keyboard layout is the same home-row span the rest of the app uses:
the 17 physical keys ``A`` .. ``;`` map to C4 up through E5. Each key drives
its **own** envelope generator; the four envelope parameters are shared
across the whole bank (one attack/decay/sustain/release for all keys —
per-key knobs would be 17×4 controls). Holding C while tapping E does not
disturb C's envelope.

Outputs: seventeen mono ``(frames,)`` cv jacks, one per key, labelled by
the note each key plays (``c4`` .. ``e5``). They are plain control voltages
in [0, 1], so each gets the usual CV output meter and can drive any CV
input — most usefully an :class:`Oscillator` ``amp_cv`` or a :class:`VCA`.

No pitch, no velocity, no per-voice gate machinery: a key is either down
(its envelope attacks → decays → sustains) or up (its envelope releases to
0). The envelope shape itself lives in the numpy backend; this module only
tracks which of the 17 keys are physically held.
"""
from __future__ import annotations

import threading

from ..core.module import Module, register_module_type
from ..core.port import Port

# The 17 home-row keys span C4 (MIDI 60) up to E5 (MIDI 76). The UI maps
# physical keys A..; to semitone offsets 0..16 and calls ``note_on`` with
# ``semitone_to_midi(octave, semitone)``; with the default octave 4 that is
# MIDI 60..76. CVGates deliberately exposes no ``octave`` param (pitch is
# irrelevant to a gate bank), so the UI's ``params.get("octave", 4)`` lookup
# falls back to 4 and a press of physical key *i* always lands on output *i*.
KEY_BASE_NOTE = 60   # MIDI C4 == output index 0
NUM_KEYS = 17        # C4 .. E5 inclusive

# Output jack names, index == semitone offset from C4. Sharps spelled "s"
# (``cs4`` == C#4) to keep them valid-ish identifiers, matching CVKeyboard's
# ``key_*`` spelling. The OUTPUT_PORTS list and the renderer both walk this
# tuple, so the jack count is defined in exactly one place.
KEY_CV_NAMES = (
    "c4", "cs4", "d4", "ds4", "e4", "f4", "fs4", "g4", "gs4",
    "a4", "as4", "b4", "c5", "cs5", "d5", "ds5", "e5",
)
assert len(KEY_CV_NAMES) == NUM_KEYS


@register_module_type
class CVGates(Module):
    """Computer-keyboard bank of per-key enveloped CV gates.

    Parameters:
        attack: Attack time in seconds (0 → instant) for the 0 → 1 ramp.
        decay: Decay time in seconds from 1.0 down to ``sustain``.
        sustain: Held level in [0, 1] while a key stays down.
        release: Release time in seconds from the key-up level down to 0.

    Runtime state (not serialized):
        _down: 17 booleans, one per key, ``True`` while that physical key is
            held. The UI mutates it through ``note_on`` / ``note_off`` on
            key events; the renderer snapshots it once per block. The ADSR
            state itself lives in the backend, keyed by module id.
    """

    TYPE = "cv_gates"
    # Marks this as a module the UI feeds physical key events to — the same
    # flag Keyboard and CVKeyboard set. The UI routes by this flag, not by
    # concrete type.
    ACCEPTS_COMPUTER_KEYS = True
    DEFAULT_PARAMS = {
        "attack": 0.01,
        "decay": 0.10,
        "sustain": 0.80,
        "release": 0.30,
    }
    INPUT_PORTS: list[Port] = []
    OUTPUT_PORTS = [Port(name, "out", "cv") for name in KEY_CV_NAMES]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # One held-flag per key; the UI mutates these on key events and the
        # renderer snapshots them once per block.
        self._down: list[bool] = [False] * NUM_KEYS
        self._lock = threading.Lock()

    # ----- transport (UI key routing calls these) -------------------------

    @staticmethod
    def _index_for(midi_note: int) -> int | None:
        """Map a routed MIDI note to a key index, or ``None`` if it falls
        outside the 17-key span (keys above/below the home row are ignored,
        exactly like an out-of-range note on a short hardware keyboard)."""
        idx = int(midi_note) - KEY_BASE_NOTE
        return idx if 0 <= idx < NUM_KEYS else None

    def note_on(self, midi_note: int) -> None:
        """Press a key → its envelope (re)enters attack on the next block."""
        idx = self._index_for(midi_note)
        if idx is None:
            return
        with self._lock:
            self._down[idx] = True

    def note_off(self, midi_note: int) -> None:
        """Release a key → its envelope enters release on the next block."""
        idx = self._index_for(midi_note)
        if idx is None:
            return
        with self._lock:
            self._down[idx] = False

    def all_notes_off(self) -> None:
        """Panic — release every key."""
        with self._lock:
            self._down = [False] * NUM_KEYS

    def snapshot_down(self) -> list[bool]:
        """Return a copy of the 17 held-key booleans for the renderer."""
        with self._lock:
            return list(self._down)
