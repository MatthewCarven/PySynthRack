"""Tests for ``AudioBackend.set_block_size`` across both backends.

These construct the backend objects directly (their ``__init__`` does not
touch the optional pyo / sounddevice libraries, so they run without either
installed) and never open a stream, so no audio hardware is required.
"""

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.audio.pyo_backend import PyoBackend


# ----- numpy (inherits the base record-only implementation) ---------------

def test_numpy_set_block_size_records_value():
    be = NumpyBackend(block_size=512)
    be.set_block_size(128)
    assert be.block_size == 128


def test_numpy_set_block_size_coerces_to_int():
    be = NumpyBackend(block_size=512)
    be.set_block_size(256.0)
    assert be.block_size == 256
    assert isinstance(be.block_size, int)


# ----- pyo (overrides to reboot the server) -------------------------------

class _FakeServer:
    """Minimal stand-in for a booted pyo Server."""

    def __init__(self) -> None:
        self.stopped = False
        self.shutdown_called = False

    def stop(self) -> None:
        self.stopped = True

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_pyo_set_block_size_unchanged_is_noop():
    be = PyoBackend(block_size=512)
    server = _FakeServer()
    be._server = server
    be._objects[1] = object()
    be.set_block_size(512)
    # Same size: server and objects left intact, nothing torn down.
    assert be._server is server
    assert server.stopped is False
    assert be._objects  # not cleared


def test_pyo_set_block_size_change_tears_server_down():
    be = PyoBackend(block_size=512)
    server = _FakeServer()
    be._server = server
    be._objects[1] = object()
    be._sinks.append(object())

    be.set_block_size(256)

    assert be.block_size == 256
    assert be._server is None            # dropped so next compile reboots
    assert server.stopped is True
    assert server.shutdown_called is True
    assert be._objects == {}             # objects belonged to old server
    assert be._sinks == []


def test_pyo_set_block_size_change_without_server():
    # No server booted yet (e.g. size chosen before first Start): just records.
    be = PyoBackend(block_size=512)
    assert be._server is None
    be.set_block_size(1024)
    assert be.block_size == 1024
    assert be._server is None


def test_pyo_set_block_size_stops_if_running():
    be = PyoBackend(block_size=512)
    be._server = _FakeServer()
    be._running = True
    be.set_block_size(64)
    assert be._running is False
    assert be.block_size == 64
