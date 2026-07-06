"""Global (machine-scoped) application settings, persisted as JSON.

This is the home for preferences that describe *this machine / this install*
rather than a particular patch: the audio buffer size today; audio device,
preferred backend, and a default window size are the obvious future tenants.
Such settings must NOT ride inside portable patch files — a buffer size that
is glitch-free on one machine underruns on a weaker one, and sharing a patch
should not impose your latency on someone else. Per-patch UI state (node
positions, zoom, and — planned — window geometry) lives in ``patch.ui``
instead; see ``core/patch.py``.

The store is a plain ``dict`` serialised to a JSON file in the platform config
directory (``%APPDATA%\\PySynthRack`` on Windows, ``$XDG_CONFIG_HOME`` or
``~/.config`` elsewhere). Reads are *total*: a missing, unreadable, or corrupt
file yields ``{}`` rather than raising, so a bad settings file can never stop
the app from launching. Writes are atomic (temp file + ``os.replace``) so an
interrupted save can't leave a half-written file behind.

Set ``PYSYNTHRACK_SETTINGS`` to point the store at an explicit path (used by
the tests, and handy for a portable install).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_APP_DIR = "PySynthRack"
_FILE = "settings.json"
_ENV_OVERRIDE = "PYSYNTHRACK_SETTINGS"


def settings_path() -> Path:
    """Resolve the settings file path for this platform.

    ``PYSYNTHRACK_SETTINGS`` wins if set; otherwise the platform config dir:
    ``%APPDATA%`` on Windows, then ``$XDG_CONFIG_HOME``, then ``~/.config``.
    """
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    base = (
        os.environ.get("APPDATA")
        or os.environ.get("XDG_CONFIG_HOME")
        or str(Path.home() / ".config")
    )
    return Path(base) / _APP_DIR / _FILE


def load_settings(path: Path | str | None = None) -> dict[str, Any]:
    """Load the settings dict. Missing / corrupt / non-dict file -> ``{}``."""
    p = Path(path) if path is not None else settings_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # OSError: missing file, is-a-directory, permission denied.
        # ValueError: malformed JSON (JSONDecodeError is a subclass).
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict[str, Any], path: Path | str | None = None) -> None:
    """Write ``data`` as pretty JSON, creating parent dirs. Atomic replace.

    May raise ``OSError`` if the directory can't be created or written —
    callers that must not crash on a read-only location should guard the call.
    """
    p = Path(path) if path is not None else settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)
