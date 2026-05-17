"""Crash-report writer used by PySynthRack's two structured catch points.

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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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


def write_crash_report(report: Any, source: str = "unknown") -> Optional[str]:
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
        # Sanitize source to a safe filename fragment - alphanumerics,
        # hyphen, underscore only. Anything else becomes an underscore.
        safe_source = "".join(
            c if (c.isalnum() or c in "-_") else "_" for c in str(source)
        )
        if not safe_source:
            safe_source = "unknown"
        path = cdir / f"crash_{ts}_{safe_source}.txt"
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
