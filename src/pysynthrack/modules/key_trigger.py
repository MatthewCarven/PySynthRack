"""KeyTrigger — bind one computer key to a gate / trigger / latch output.

A single-purpose controller: one node listens for one physical key and puts
out a control signal when you press it. Drop as many as you like — one per
key — and wire each independently, so a busy patch reads as a swarm of small
labelled "this key does this one thing" nodes instead of one fat keyboard
with a spaghetti of cables. Fan-out is free (one output feeds any number of
cables), so a single key can drive a whole rack of destinations at once.

Where :class:`Keyboard` / :class:`CVKeyboard` / :class:`CVGates` route the
home-row keys as *notes* (a fixed C4-up keymap), KeyTrigger binds **any**
single key — letters, the number row, punctuation, space — because the UI
feeds it *raw* key events by name rather than note-mapped ones (the
``ACCEPTS_RAW_KEYS`` flag below; see ``ui/app.py``). Bind a key the note
keyboards don't use and it's a dedicated trigger; bind one they do and both
respond (fan-out is free).

The single ``out`` jack is a gate-kind signal in {0, 1}; ``mode`` chooses how
a press shapes it:

* ``gate``    — high while the key is physically held (momentary).
* ``trigger`` — a short fixed pulse on each press edge, for clocking a
  :class:`Sequencer`, resetting a :class:`Clock`, or firing an
  :class:`ADEnvelope`.
* ``latch``   — each press *toggles* the output; it then holds through
  key-up until the next press (tap-on / tap-off — e.g. latch the
  resampler brake, or a mute).

No pitch, no velocity, no envelope: KeyTrigger only tracks whether its one
key is down and how many times it has been pressed. Envelope shaping is a
downstream :class:`ADSR` / :class:`ADEnvelope` away; the trigger pulse shape
lives in the numpy backend.

Parameters:
    key: Name of the bound physical key (e.g. ``"Q"``, ``"5"``,
        ``"Semicolon"``, ``"Space"``). ``""`` = unbound (idles at 0). Set via
        the node's **Learn** button and stored as a portable key *name*, not
        a backend-specific key code, so a saved patch rebinds correctly on
        any machine.
    mode: Output behaviour — one of :data:`KEY_TRIGGER_MODES`.

Runtime state (not serialized):
    _held: True while the bound key is physically down. The UI mutates it via
        ``raw_key_down`` / ``raw_key_up`` on key events; the renderer
        snapshots it once per block.
    _presses: Count of press edges since the last :meth:`snapshot`, so the
        renderer can act on the *edge* (trigger pulse / latch toggle) even
        when a key is tapped and released inside a single block.
"""
from __future__ import annotations

import threading

from ..core.module import Module, register_module_type
from ..core.port import Port

# The three output behaviours. Exposed as a module-level constant so the UI's
# shared mode-combo can import it (exactly like FILTER_MODES etc.), keeping
# the vocabulary defined in one place.
KEY_TRIGGER_MODES = ("gate", "trigger", "latch")


@register_module_type
class KeyTrigger(Module):
    """One bound computer key → a gate / trigger / latch control signal."""

    TYPE = "key_trigger"
    CATEGORY = "Sources"
    # Flags this as a module the UI feeds *raw* physical key events to (by
    # key name), as opposed to ACCEPTS_COMPUTER_KEYS which routes the home-row
    # keys as MIDI notes. The UI dispatches by this flag, not by concrete
    # type, so both key paths can coexist in one patch.
    ACCEPTS_RAW_KEYS = True
    DEFAULT_PARAMS = {
        "key": "",       # unbound until Learn assigns a key name
        "mode": "gate",  # one of KEY_TRIGGER_MODES
    }
    INPUT_PORTS: list[Port] = []  # source — no inputs
    OUTPUT_PORTS = [Port("out", "out", "gate")]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._held = False
        self._presses = 0
        self._lock = threading.Lock()

    # ----- transport (UI raw-key routing calls these) ---------------------

    def _matches(self, key_name: str) -> bool:
        """True when ``key_name`` is this node's bound key (never for the
        unbound empty default, so an un-Learned node ignores every key)."""
        bound = str(self.params.get("key", "") or "")
        return bool(bound) and key_name == bound

    def raw_key_down(self, key_name: str) -> None:
        """A physical key went down. If it's the bound key, mark it held and
        record the press edge. The UI debounces OS auto-repeat, so this is
        one call per real press (a held key is not a stream of edges)."""
        if not self._matches(key_name):
            return
        with self._lock:
            self._held = True
            self._presses += 1

    def raw_key_up(self, key_name: str) -> None:
        """The bound key went up → drop the held gate (the latch, if any,
        stays — only a press toggles it)."""
        if not self._matches(key_name):
            return
        with self._lock:
            self._held = False

    def all_notes_off(self) -> None:
        """Panic / focus-loss: release the key and drop any pending edge.
        (The latch level lives in the backend and is left as-is, matching how
        a keyboard panic doesn't rewrite downstream envelope state.)"""
        with self._lock:
            self._held = False
            self._presses = 0

    def snapshot(self) -> tuple[bool, int]:
        """Return ``(held, presses_since_last_call)`` and reset the press
        counter — the renderer consumes the edge here, once per block."""
        with self._lock:
            held, presses = self._held, self._presses
            self._presses = 0
            return held, presses
