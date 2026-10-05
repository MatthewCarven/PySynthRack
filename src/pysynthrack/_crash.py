"""Crash-report writer used by PySynthRack's two structured catch points.

Also writes the patch loader's reports (``write_load_report``): what
``Patch.from_dict`` had to drop. Those land in the same folder, so there
is one place to look when something went wrong, and the windowed build
(which has no console) still leaves the detail somewhere readable.

Used by ``ui/app.py``'s outermost ``try/except`` around ``App().run()``
and by ``numpy_backend._audio_callback`` when a render raises. Writes
the heavy/labeled ``for_claude()`` output of ``describe_error()`` to a
file in the user's profile directory so the report survives even if
the process exits immediately afterwards (DPG hard-exits, sounddevice
killing the audio thread, etc.).

The function never raises. If the user profile path isn't writable
(no home dir, permission denied, full disk, anything else), it
returns ``None`` rather than propagating - the calling site already
has to handle the "we couldn't write a crash file" case anyway.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Crash files live under ~/.pysynthrack/crashes/ on every platform.
# Path.home() returns USERPROFILE on Windows, $HOME elsewhere - one
# code path covers both.
_CRASH_DIR_NAME = ".pysynthrack"
_CRASH_SUBDIR = "crashes"


def crash_dir() -> Path:
    """Return the directory where crash reports are written.

    Does NOT create it - callers can use this purely for inspection
    (e.g. "where does my crash file end up?" in the UI).
    """
    return Path.home() / _CRASH_DIR_NAME / _CRASH_SUBDIR


def _safe_fragment(text: Any) -> str:
    """``text`` as a filename fragment: alphanumerics, hyphen and
    underscore kept, anything else an underscore, empty -> "unknown"."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(text))
    return safe or "unknown"


def write_crash_report(report: Any, source: str = "unknown") -> str | None:
    """Write ``report.for_claude()`` to a timestamped file in the user's
    profile crash directory. Returns the file path as a string on
    success, or ``None`` on any failure.

    ``report`` is expected to be an :class:`pysynthrack.error_handler.
    ErrorReport`, but anything with a ``for_claude()`` method that
    returns a string works. ``source`` is a short tag baked into the
    filename so multiple crashes from different sites in the same
    session are distinguishable; current callers pass ``"gui"`` or
    ``"audio_callback"``.

    Filename shape::

        ~/.pysynthrack/crashes/crash_2026-05-15_08-45-12_gui.txt

    Never raises. Failure modes that produce ``None``:

      * Home directory unknown or unwritable
      * Crash dir creation fails (permissions, full disk)
      * ``report.for_claude()`` raises (falls back to ``str(report)``)
      * ``write_text`` fails (disk full, encoding error, anything)

    The fallback chain for the body is:

      1. ``report.for_claude()`` - the intended path
      2. ``str(report)`` - if step 1 raises
      3. literal placeholder string - if step 2 also raises

    Step 3 still produces a (small) file; the caller knows something
    catastrophic happened by looking at the contents.
    """
    try:
        cdir = crash_dir()
        cdir.mkdir(parents=True, exist_ok=True)
    except BaseException:
        logger.warning(
            "Could not create crash directory; crash report not persisted."
        )
        return None

    try:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = cdir / f"crash_{ts}_{_safe_fragment(source)}.txt"
    except BaseException:
        logger.warning("Could not build crash filename; crash report not persisted.")
        return None

    # Body: try for_claude() first, fall back through str(), then a
    # literal placeholder. A useless file is still better than no file
    # because the timestamp/source filename tells the user that
    # _something_ went wrong here.
    try:
        text = report.for_claude()
    except BaseException:
        try:
            text = str(report)
        except BaseException:
            text = "<crash report object could not be formatted>"

    try:
        path.write_text(text, encoding="utf-8")
    except BaseException:
        logger.warning("Could not write crash report file %s", path)
        return None

    return str(path)


def write_load_report(patch_path: str, warnings: list[str]) -> str | None:
    """Write what the patch loader dropped to the crash folder.

    ``warnings`` is ``patch.load_warnings``: one line per cable
    ``Patch.from_dict`` left out. The GUI puts only the count on its
    one-line status bar, and the windowed build has no console for the
    detail, so the detail goes here. Returns the file path as a string,
    or ``None`` on any failure -- never raises, like
    ``write_crash_report``. Filename shape::

        ~/.pysynthrack/crashes/load_2026-09-28_10-11-12_mypatch.txt
    """
    try:
        cdir = crash_dir()
        cdir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        stem = Path(str(patch_path)).stem
        path = cdir / f"load_{now.strftime('%Y-%m-%d_%H-%M-%S')}_{_safe_fragment(stem)}.txt"
        lines = [
            "PySynthRack load report",
            f"Patch: {patch_path}",
            f"When:  {now.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"Dropped {len(warnings)} dead cable(s) on load:",
            *(f"  - {w}" for w in warnings),
            "",
            "Each one names a module or port the patch doesn't have, or a",
            "connection the editor would refuse (a hand edit, or a port",
            "renamed since the patch was saved). The rest of the patch loaded",
            "and plays. Saving the patch now writes it WITHOUT these cables,",
            "so re-cable them in the editor first if you want to keep them.",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except BaseException:
        logger.warning("Could not write the load report; it went to the console only.")
        return None


# ---------------------------------------------------------------------------
# Global crash logging: route UNCAUGHT crashes to the crash folder.
#
# The explicit catch points (ui.app.main for the GUI, numpy_backend for the
# audio callback) already write their own file with a precise ``source`` tag.
# ``install_crash_logging`` adds a safety net for everything else -- crashes on
# worker threads (threading.excepthook) and "exception ignored in __del__"
# (sys.unraisablehook) -- by registering an observer on the error handler that
# writes each such report to the same folder.
#
# The error handler fires observers for EVERY report it builds, including the
# ones the explicit sites build. To avoid two files per crash, an explicit site
# wraps its ``describe_error`` call in ``explicit_write()``; the observer sees
# the thread-local flag and skips, leaving the explicit write as the single,
# precisely-tagged file. The flag is thread-local, so a background-thread crash
# happening during an explicit write on another thread is still captured.
# ---------------------------------------------------------------------------

_explicit = threading.local()
_installed = False


class explicit_write:
    """Mark the current thread as performing an explicit crash write.

    Used by the GUI / audio catch points around their ``describe_error`` call
    so the global observer skips that report (they write their own, precisely
    tagged, file). Thread-local and re-entrant-safe for our usage.
    """

    def __enter__(self):
        _explicit.active = True
        return self

    def __exit__(self, *exc):
        _explicit.active = False
        return False


def _crash_observer(report: Any) -> None:
    """Error-handler observer: persist background / uncaught crash reports.

    Skips reports an explicit catch point is already writing (see
    ``explicit_write``) so each crash yields exactly one file. Never raises --
    ``write_crash_report`` never raises, and the handler also guards every
    observer call.
    """
    if getattr(_explicit, "active", False):
        return
    write_crash_report(report, source="uncaught")


def install_crash_logging() -> bool:
    """Route uncaught crashes to the crash folder.

    Registers the folder-writing observer and wires the ``threading`` +
    ``unraisable`` hooks. ``excepthook`` is intentionally left alone: the GUI
    main thread is handled by ``ui.app.main``'s own try/except (suppress + exit).

    Idempotent; returns True on success, False if wiring failed. Never raises.
    """
    global _installed
    if _installed:
        return True
    try:
        from .error_handler import install, register_observer

        register_observer(_crash_observer)
        install(
            hooks=("threading", "unraisable"),
            style="concise",
            include_locals=True,
        )
        _installed = True
        return True
    except Exception:
        logger.warning("Could not install global crash logging.", exc_info=True)
        return False


def uninstall_crash_logging() -> None:
    """Reverse ``install_crash_logging`` (restore prior hooks, drop the
    observer). Mainly for tests and clean shutdown. Never raises."""
    global _installed
    try:
        from .error_handler import uninstall, unregister_observer

        uninstall()
        unregister_observer(_crash_observer)
    except Exception:
        pass
    finally:
        _installed = False
