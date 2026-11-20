"""Backend preference: numpy first (Matthew, 2026-11-20).

With pyo first, anyone who took the README's optional pyo step got a GUI
where most modules were silent stubs. numpy now wins whenever it is
available; pyo is one ``--backend pyo`` / ``PYSYNTHRACK_BACKEND=pyo`` away.
"""
from __future__ import annotations

import pytest

from pysynthrack import audio
from pysynthrack.audio import NumpyBackend, PyoBackend, pick_backend


def test_numpy_comes_first():
    assert audio._PREFERENCE == ("numpy", "pyo")


def test_numpy_wins_even_when_pyo_is_installed(monkeypatch):
    monkeypatch.delenv("PYSYNTHRACK_BACKEND", raising=False)
    monkeypatch.setattr(NumpyBackend, "is_available", classmethod(lambda cls: True))
    monkeypatch.setattr(PyoBackend, "is_available", classmethod(lambda cls: True))
    assert isinstance(pick_backend(), NumpyBackend)


def test_pyo_is_the_fallback(monkeypatch):
    monkeypatch.delenv("PYSYNTHRACK_BACKEND", raising=False)
    monkeypatch.setattr(NumpyBackend, "is_available", classmethod(lambda cls: False))
    monkeypatch.setattr(PyoBackend, "is_available", classmethod(lambda cls: True))
    monkeypatch.setattr(PyoBackend, "__init__", lambda self, **kw: None)
    assert isinstance(pick_backend(), PyoBackend)


def test_asking_for_pyo_still_gets_pyo(monkeypatch):
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "pyo")
    monkeypatch.setattr(PyoBackend, "is_available", classmethod(lambda cls: True))
    monkeypatch.setattr(PyoBackend, "__init__", lambda self, **kw: None)
    assert isinstance(pick_backend(), PyoBackend)


def test_the_command_line_option_sets_the_same_switch(monkeypatch):
    from pysynthrack.__main__ import _build_parser
    assert _build_parser().parse_args(["--backend", "pyo"]).backend == "pyo"
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--backend", "jack"])
