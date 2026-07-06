"""Tests for the global crash-logging wiring (``_crash.install_crash_logging``).

install_crash_logging registers an observer on the error handler and wires the
threading / unraisable hooks, so uncaught background crashes get written to the
crash folder. The explicit catch points guard their own reports with
``explicit_write()`` so the observer skips them (one file per crash).

These mutate global process state (hooks + observer registry), so an autouse
fixture restores it after every test and Path.home is redirected to tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pysynthrack import _crash
from pysynthrack.error_handler import describe_error


@pytest.fixture(autouse=True)
def _restore_global_state(tmp_path, monkeypatch):
    """Redirect the crash dir to tmp_path and guarantee teardown restores the
    handler hooks + observer registry no matter how the test exits."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield
    _crash.uninstall_crash_logging()


def _raise_and_describe(msg="kaboom in a worker"):
    try:
        raise ValueError(msg)
    except ValueError as e:
        return describe_error(e)


def test_install_returns_true_and_is_idempotent():
    assert _crash.install_crash_logging() is True
    # Second call is a no-op that still reports success.
    assert _crash.install_crash_logging() is True


def test_observer_writes_uncaught_report_to_folder():
    _crash.install_crash_logging()
    _raise_and_describe()  # a plain describe_error fires the observer
    files = list(_crash.crash_dir().glob("crash_*_uncaught.txt"))
    assert len(files) == 1
    assert "ValueError" in files[0].read_text(encoding="utf-8")


def test_explicit_write_guard_skips_observer():
    _crash.install_crash_logging()
    with _crash.explicit_write():
        _raise_and_describe()  # observer should skip this one
    cdir = _crash.crash_dir()
    if cdir.exists():
        assert list(cdir.glob("*_uncaught.txt")) == []


def test_explicit_write_resets_after_block():
    _crash.install_crash_logging()
    with _crash.explicit_write():
        pass
    # Flag cleared -> a subsequent uncaught report is written again.
    _raise_and_describe()
    assert len(list(_crash.crash_dir().glob("crash_*_uncaught.txt"))) == 1


def test_uninstall_stops_the_observer():
    _crash.install_crash_logging()
    _crash.uninstall_crash_logging()
    _raise_and_describe()  # observer no longer registered
    cdir = _crash.crash_dir()
    if cdir.exists():
        assert list(cdir.glob("*_uncaught.txt")) == []


def test_no_observer_without_install():
    # Without install_crash_logging, a bare describe_error writes nothing.
    _raise_and_describe()
    cdir = _crash.crash_dir()
    if cdir.exists():
        assert list(cdir.iterdir()) == []
