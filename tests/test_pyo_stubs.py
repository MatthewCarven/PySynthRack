"""Every registered module type is either built by pyo or says it is silent.

The pyo backend is parked: it builds the oscillator and the three plain
speaker sinks, and every other type is a silent stub that prints one
"... will be silent. Run with PYSYNTHRACK_BACKEND=numpy" line at compile
time, so a patch that goes quiet under pyo says why. The line comes from
a hand-kept list (``_SILENT_STUB_TYPES``) plus a few types with their own
message, and a type on neither returns ``None`` without a word -- silent
AND unexplained. By 2026-10-10 four had slipped through that way
(``file_player``, ``mic_input``, ``function_generator``, ``freeze``).

So this walks the registry rather than naming modules: a new type is
covered the day it registers, whether or not anyone remembers the list.
pyo is not installed here, so ``pyo`` is swapped for a ``MagicMock``;
nothing is booted and no audio runs. ``_build_module`` is a pure
dispatcher, which is what makes that a fair test of it.
"""
from __future__ import annotations

import contextlib
import io
from unittest import mock

import pytest

import pysynthrack.modules  # noqa: F401  (registers every type)
from pysynthrack.audio import pyo_backend
from pysynthrack.audio.pyo_backend import (
    _SILENT_STUB_TYPES,
    _SPEAKER_TYPES,
    PyoBackend,
)
from pysynthrack.core.module import all_module_types

TYPES = sorted(all_module_types())


def _build(type_name):
    """``_build_module`` for one fresh module of ``type_name``, under a
    mocked pyo. Returns ``(built_object, printed_text)``."""
    module = all_module_types()[type_name](0)
    backend = PyoBackend()
    buf = io.StringIO()
    with mock.patch.object(pyo_backend, "pyo", mock.MagicMock()), \
            contextlib.redirect_stdout(buf):
        obj = backend._build_module(module)
    return obj, buf.getvalue()


@pytest.mark.parametrize("type_name", TYPES)
def test_every_type_is_built_or_announced_silent(type_name):
    """Built (an object back), a speaker sink (built later, in
    ``_finalize_speaker_output``), or a stub that NAMES itself as silent."""
    obj, printed = _build(type_name)
    if type_name in _SPEAKER_TYPES:
        assert obj is None and printed == "", (
            f"{type_name}: a speaker is built in _finalize_speaker_output")
        return
    if obj is not None:
        assert printed == "", f"{type_name}: built AND announced silent"
        return
    assert f"{type_name} module not yet supported" in printed, (
        f"{type_name}: pyo builds nothing and says nothing -- add it to "
        f"_SILENT_STUB_TYPES in audio/pyo_backend.py")
    assert "will be silent" in printed
    assert "PYSYNTHRACK_BACKEND=numpy" in printed


def test_the_stub_list_has_no_duplicates_and_no_stale_names():
    registered = set(all_module_types())
    assert len(_SILENT_STUB_TYPES) == len(set(_SILENT_STUB_TYPES))
    stale = sorted(set(_SILENT_STUB_TYPES) - registered)
    assert not stale, f"stubbed but not registered: {stale}"
    assert set(_SPEAKER_TYPES) <= registered


def test_the_stub_list_does_not_shadow_a_built_type():
    """The generic tuple must not swallow a type pyo actually builds."""
    assert not set(_SILENT_STUB_TYPES) & set(_SPEAKER_TYPES)
    assert "oscillator" not in _SILENT_STUB_TYPES
    obj, printed = _build("oscillator")
    assert obj is not None and printed == ""


def test_the_tripwire_catches_a_missing_type(monkeypatch):
    """Self-test: drop a type from the list and the walk must notice."""
    victim = "freeze"
    assert victim in _SILENT_STUB_TYPES
    monkeypatch.setattr(
        pyo_backend, "_SILENT_STUB_TYPES",
        tuple(t for t in _SILENT_STUB_TYPES if t != victim),
    )
    obj, printed = _build(victim)
    assert obj is None and printed == ""
