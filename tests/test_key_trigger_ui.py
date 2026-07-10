"""GUI-glue tests for KeyTrigger's raw-key routing + Learn button (headless).

These drive ``App._dispatch_raw_key`` / ``_on_key_learn`` / ``_bind_learned_key``
with DearPyGui mocked out (``app.dpg`` swapped for a MagicMock), so no window
is needed. The code<->name key map and the focus/modifier guards are dpg-only
and get a real-window eyeball; the module's own key filtering and gate/trigger/
latch DSP are covered headless in ``test_key_trigger.py``.

Skips cleanly where DearPyGui can't be imported.
"""
from __future__ import annotations

from unittest import mock

import pytest

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.modules  # noqa: F401  (registers module types)
import pysynthrack.ui.app as app_mod
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch


def _make_app(monkeypatch):
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    return app


def test_dispatch_fans_out_to_the_bound_key_only(monkeypatch):
    app = _make_app(monkeypatch)
    kt = app.patch.add_module("key_trigger", params={"key": "Q", "mode": "gate"})
    app._dispatch_raw_key("W", down=True)   # a different key
    assert kt.snapshot() == (False, 0)
    app._dispatch_raw_key("Q", down=True)   # the bound key
    assert kt.snapshot() == (True, 1)
    app._dispatch_raw_key("Q", down=False)
    assert kt.snapshot() == (False, 0)


def test_dispatch_skips_non_rawkey_modules(monkeypatch):
    app = _make_app(monkeypatch)
    kt = app.patch.add_module("key_trigger", params={"key": "Q"})
    app.patch.add_module("oscillator")      # no ACCEPTS_RAW_KEYS / raw_key_down
    app._dispatch_raw_key("Q", down=True)   # must not touch the oscillator
    assert kt.snapshot() == (True, 1)


def test_two_key_triggers_are_independent(monkeypatch):
    app = _make_app(monkeypatch)
    a = app.patch.add_module("key_trigger", params={"key": "Q"})
    b = app.patch.add_module("key_trigger", params={"key": "P"})
    app._dispatch_raw_key("Q", down=True)
    assert a.snapshot() == (True, 1)
    assert b.snapshot() == (False, 0)      # only its own key


def test_learn_binds_the_next_key(monkeypatch):
    app = _make_app(monkeypatch)
    kt = app.patch.add_module("key_trigger", params={"key": ""})
    app._on_key_learn(None, None, kt.id)   # arm Learn
    assert app._key_learn_target == kt.id
    app._bind_learned_key("5")             # a key press arrives
    assert kt.params["key"] == "5"
    assert app._key_learn_target is None   # Learn consumed
    app._dispatch_raw_key("5", down=True)  # and it now responds to that key
    assert kt.snapshot() == (True, 1)


def test_learn_click_again_cancels(monkeypatch):
    app = _make_app(monkeypatch)
    kt = app.patch.add_module("key_trigger", params={"key": ""})
    app._on_key_learn(None, None, kt.id)
    app._on_key_learn(None, None, kt.id)   # same button again = cancel
    assert app._key_learn_target is None
    assert kt.params["key"] == ""          # nothing bound


def test_learn_hands_over_between_nodes(monkeypatch):
    app = _make_app(monkeypatch)
    a = app.patch.add_module("key_trigger", params={"key": ""})
    b = app.patch.add_module("key_trigger", params={"key": ""})
    app._on_key_learn(None, None, a.id)
    app._on_key_learn(None, None, b.id)    # switch arming to b
    assert app._key_learn_target == b.id
    app._bind_learned_key("K")
    assert b.params["key"] == "K"
    assert a.params["key"] == ""           # a stayed unbound


def test_panic_releases_key_triggers(monkeypatch):
    app = _make_app(monkeypatch)
    kt = app.patch.add_module("key_trigger", params={"key": "Q"})
    app._dispatch_raw_key("Q", down=True)
    app._all_keyboards_notes_off()         # focus loss / audio stop
    assert kt.snapshot() == (False, 0)
