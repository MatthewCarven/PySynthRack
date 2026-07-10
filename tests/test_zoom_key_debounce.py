"""The Ctrl+zoom keys must step once per physical press, not cycle at the OS
key-repeat rate. Exercises App._debounce_key directly against a stand-in that
holds just the _held_keys set -- the actual key-repeat wiring is eyeball-
verified in the real window (dpg key events aren't headless-drivable), like the
rest of the UI glue.

Imports pysynthrack.ui.app, so it is collected only where dearpygui is present
(the UI-test convention in this project)."""
from __future__ import annotations

from pysynthrack.ui.app import App


class _Stub:
    """Minimal stand-in exposing only what _debounce_key touches."""

    def __init__(self):
        self._held_keys = set()


def test_first_press_fires_then_repeats_are_suppressed():
    s = _Stub()
    assert App._debounce_key(s, 42) is True     # initial press acts
    assert App._debounce_key(s, 42) is False    # OS auto-repeat suppressed
    assert App._debounce_key(s, 42) is False


def test_release_rearms_next_press():
    s = _Stub()
    assert App._debounce_key(s, 42) is True
    s._held_keys.discard(42)                    # global key-release clears it
    assert App._debounce_key(s, 42) is True     # a fresh press re-fires


def test_distinct_keys_are_independent():
    s = _Stub()
    assert App._debounce_key(s, 1) is True
    assert App._debounce_key(s, 2) is True      # different key, its own edge
    assert App._debounce_key(s, 1) is False     # first key still held
