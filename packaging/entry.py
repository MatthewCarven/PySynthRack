"""PyInstaller entry point with belt-and-braces crash protection.

The package-level catches (``ui.app.main`` and the audio callback wrapper)
can't see failures that happen before their try-blocks open -- import
errors, missing native DLLs, segfaults inside C extensions.  This script
wraps the whole thing and emits checkpoint markers so a silent native
crash leaves a forensic trail.

Layers of safety:

  1.  Null-stream guard.  PyInstaller windowed builds set ``sys.stdout``
      and ``sys.stderr`` to ``None``; we replace ``None`` with an
      ``os.devnull`` writer so existing ``print(file=sys.stderr)`` calls
      become no-ops instead of fatal ``AttributeError``s.

  2.  Startup ping + checkpoint log.  Before any package import we drop
      ``~/.pysynthrack/crashes/_last_startup.txt`` and then APPEND a
      line at every major step (probe-imports for numpy / sounddevice /
      mido / dearpygui, package import, main() call).  If the process
      dies silently from a native segfault, the LAST LINE in the file
      tells you exactly which dependency killed it.

  3.  Outer crash catch.  Any Python exception escaping
      ``pysynthrack.__main__.main()`` lands here.  We try the heavy
      ``describe_error`` + ``write_crash_report`` combo first, falling
      back to a pure-stdlib ``_emergency_dump`` only if that errors too.

Running locally:
    python packaging/entry.py            # equivalent to ``python -m pysynthrack``
    python packaging/entry.py --cli      # CLI mode

Building:
    pyinstaller pysynthrack.spec         # windowed build (no console)
    pyinstaller pysynthrack-cli.spec     # console build (CLI debugging)
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

_CRASH_DIR_NAME = ".pysynthrack"
_CRASH_SUBDIR = "crashes"
_PING_FILENAME = "_last_startup.txt"

# Will be set once we know where the crash dir is.  Used by ``_checkpoint``.
_PING_PATH: Path | None = None


# ---------------------------------------------------------------------------
# Step 1: stream guard.  Must run before any other code prints anything.
# ---------------------------------------------------------------------------

def _fix_streams() -> None:
    """Replace ``None`` stdout/stderr with ``os.devnull`` writers.

    PyInstaller's windowed mode (``console=False``) sets both streams to
    ``None``.  Anything that does ``print(..., file=sys.stderr)`` then
    raises ``AttributeError: 'NoneType' object has no attribute 'write'``
    and the process exits silently.
    """
    if sys.stdout is None or sys.stderr is None:
        try:
            null = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return
        if sys.stdout is None:
            sys.stdout = null
        if sys.stderr is None:
            sys.stderr = null


_fix_streams()


# ---------------------------------------------------------------------------
# Step 2: crash-dir helpers.  Self-contained -- do not import from the
# package, because the whole point is to survive a package import failure.
# ---------------------------------------------------------------------------

def _crash_dir() -> Path:
    return Path.home() / _CRASH_DIR_NAME / _CRASH_SUBDIR


def _startup_ping() -> None:
    """Drop the initial marker file.  ``_checkpoint`` appends to it."""
    global _PING_PATH
    try:
        d = _crash_dir()
        d.mkdir(parents=True, exist_ok=True)
        _PING_PATH = d / _PING_FILENAME
        with open(_PING_PATH, "w", encoding="utf-8") as f:
            f.write("PySynthRack startup ping\n")
            f.write(f"  time:       {datetime.now().isoformat()}\n")
            f.write(f"  frozen:     {bool(getattr(sys, 'frozen', False))}\n")
            f.write(f"  python:     {sys.version}\n")
            f.write(f"  executable: {sys.executable}\n")
            f.write(f"  argv:       {sys.argv}\n")
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                f.write(f"  _MEIPASS:   {meipass}\n")
            f.write("\nCheckpoints (last line = how far we got):\n")
    except BaseException:
        _PING_PATH = None  # ping itself can't write -- give up silently


def _checkpoint(label: str) -> None:
    """Append a timestamped line to the startup ping.

    Critically: ``f.flush()`` and ``os.fsync()`` so a native segfault
    immediately after this call can't lose the marker.  An expensive
    fsync per checkpoint is fine -- we only do a dozen at startup.
    """
    if _PING_PATH is None:
        return
    try:
        with open(_PING_PATH, "a", encoding="utf-8") as f:
            f.write(f"  [{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {label}\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except BaseException:
        pass


def _emergency_dump(exc: BaseException, source: str = "entry") -> str | None:
    """Last-resort crash writer using only stdlib."""
    try:
        d = _crash_dir()
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in source)
        path = d / f"crash_{ts}_{clean}_emergency.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write("PySynthRack emergency crash report\n")
            f.write("(written by packaging/entry.py -- pure stdlib fallback)\n\n")
            f.write(f"Source:      {source}\n")
            f.write(f"Time:        {datetime.now().isoformat()}\n")
            f.write(f"Frozen:      {bool(getattr(sys, 'frozen', False))}\n")
            f.write(f"Python:      {sys.version}\n")
            f.write(f"Executable:  {sys.executable}\n")
            f.write(f"Argv:        {sys.argv}\n")
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                f.write(f"_MEIPASS:    {meipass}\n")
            f.write("\nTraceback (most recent call last):\n")
            traceback.print_exception(
                type(exc), exc, exc.__traceback__, file=f
            )
        return str(path)
    except BaseException:
        return None


# ---------------------------------------------------------------------------
# Step 3: native-import probes.  Each one's an island so a Python-level
# ImportError records itself in the ping file and the next probe still
# runs.  A native segfault leaves the previous "about to import X" line
# as the last checkpoint, which IS the diagnosis.
# ---------------------------------------------------------------------------

def _probe(label: str, import_stmt: str) -> bool:
    """Try ``exec(import_stmt)`` and record the outcome.

    Returns ``True`` on success, ``False`` on a caught ImportError.  A
    segfault doesn't return -- it kills the process, and the
    "about to import" checkpoint we wrote just before is the trail.
    """
    _checkpoint(f"about to import {label}")
    try:
        exec(import_stmt, {})
    except BaseException as e:
        _checkpoint(f"import {label} FAILED: {type(e).__name__}: {e}")
        return False
    _checkpoint(f"import {label} ok")
    return True


# ---------------------------------------------------------------------------
# Step 4: path injection for source-mode runs.
# ---------------------------------------------------------------------------

if not getattr(sys, "frozen", False):
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.normpath(os.path.join(here, "..", "src"))
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)


# ---------------------------------------------------------------------------
# Step 5: actual entry -- ping, probe, import, run, catch.
# ---------------------------------------------------------------------------

def _main() -> int:
    _startup_ping()

    # Probe native deps one at a time so a segfault names itself by being
    # the last "about to import" line in the ping file.  We don't bail
    # on a Python ImportError here -- the package's existing fallbacks
    # (CLI mode if dearpygui is missing, mido missing -> no MIDI) handle
    # those gracefully.
    _probe("numpy", "import numpy")
    _probe("sounddevice", "import sounddevice")
    _probe("mido", "import mido")
    _probe("rtmidi", "import rtmidi")
    _probe("dearpygui", "import dearpygui.dearpygui")

    _checkpoint("about to import pysynthrack")
    try:
        from pysynthrack.__main__ import main as _pkg_main
    except BaseException as e:
        _checkpoint(f"pysynthrack import FAILED: {type(e).__name__}: {e}")
        _emergency_dump(e, source="import")
        return 1
    _checkpoint("pysynthrack import ok")

    _checkpoint("calling pysynthrack.main()")
    try:
        rc = _pkg_main()
        _checkpoint(f"pysynthrack.main() returned {rc!r}")
        return int(rc) if rc is not None else 0
    except SystemExit:
        raise
    except BaseException as e:
        _checkpoint(f"pysynthrack.main() RAISED: {type(e).__name__}: {e}")
        # Try the heavy report first; fall back to pure-stdlib on failure.
        try:
            from pysynthrack.error_handler import describe_error
            from pysynthrack._crash import write_crash_report
            report = describe_error(e, include_locals=True)
            write_crash_report(report, source="entry")
        except BaseException:
            _emergency_dump(e, source="entry")
        return 1


if __name__ == "__main__":
    sys.exit(_main())
