"""Where ``disk_writer`` recordings land, and what they are called.

A recording is a *destination*, not a lookup, so it does not follow the
media-path rule (samples and IRs resolve against the patch's folder).
Recording while an example is open would then litter ``examples/``,
and resolving against the process working directory -- the old rule --
scattered takes wherever the app happened to be launched from.

Instead a **relative** ``path`` is a name inside the recordings folder,
``<Music>/PySynthRack/`` (created on first use), and an **absolute**
path is used exactly as given. ``<Music>`` is the user's Music folder as
Windows reports it (the known-folder lookup, so a Music folder that
OneDrive or the user has moved is honoured), else ``~/Music``.

``PYSYNTHRACK_RECORDINGS_DIR`` overrides the whole folder -- the test
suite points it at a temp dir so the examples sweep never writes into
anyone's real Music folder.

Everything here is pure path arithmetic except :func:`music_dir`'s one
cached OS call and :func:`unique_path`'s ``exists`` probe; the writer
thread, not the audio callback, does the filesystem work.
"""
from __future__ import annotations

import functools
import os
import sys
import time
from pathlib import Path

#: Environment variable that replaces the whole recordings folder.
RECORDINGS_ENV = "PYSYNTHRACK_RECORDINGS_DIR"

#: The app's folder inside the user's Music folder.
APP_FOLDER = "PySynthRack"

#: What an empty ``path`` records to.
DEFAULT_NAME = "recording.wav"

#: ``time.strftime`` format of the timestamp tickbox: sortable, ASCII,
#: and free of the colons Windows refuses in a filename.
STAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"

# FOLDERID_Music, {4BD8D571-6D19-48D3-BE97-422220080E43}.
_FOLDERID_MUSIC = (
    0x4BD8D571, 0x6D19, 0x48D3,
    (0xBE, 0x97, 0x42, 0x22, 0x20, 0x08, 0x0E, 0x43),
)


def _windows_music_dir() -> Path | None:
    """The Music known folder via ``SHGetKnownFolderPath``, or None.

    Any failure (no shell32, a missing folder, an exotic policy) returns
    None so the caller falls back to ``~/Music``.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        d1, d2, d3, d4 = _FOLDERID_MUSIC
        guid = _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
        # Private prototypes, so nothing here changes the argtypes of the
        # process-wide ``windll`` function objects.
        get_path = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p),
        )(("SHGetKnownFolderPath", ctypes.windll.shell32))
        free = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(
            ("CoTaskMemFree", ctypes.windll.ole32)
        )
        out = ctypes.c_void_p()
        hr = get_path(ctypes.byref(guid), 0, None, ctypes.byref(out))
        try:
            if hr != 0 or not out.value:
                return None
            return Path(ctypes.wstring_at(out.value))
        finally:
            if out.value:
                free(out)
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def music_dir() -> Path:
    """The user's Music folder (looked up once per process)."""
    if sys.platform == "win32":
        found = _windows_music_dir()
        if found is not None:
            return found
    return Path.home() / "Music"


def recordings_dir() -> Path:
    """The folder relative ``disk_writer`` paths land in."""
    override = os.environ.get(RECORDINGS_ENV)
    if override:
        return Path(override)
    return music_dir() / APP_FOLDER


def stamped_name(name: str, when: float) -> str:
    """``take.wav`` -> ``take_2026-10-10_17-45-03.wav`` at local time ``when``.

    The stamp goes before the suffix; a name with no suffix just gains
    the stamp on the end.
    """
    p = Path(name)
    stamp = time.strftime(STAMP_FORMAT, time.localtime(when))
    return f"{p.stem}_{stamp}{p.suffix}"


def resolve_recording_path(
    path: str, *, timestamp: bool = False, when: float | None = None
) -> Path:
    """Where a take of a ``disk_writer`` with this ``path`` is written.

    Relative -> inside :func:`recordings_dir` (sub-folders allowed);
    absolute (or ``~``-rooted) -> as given. With ``timestamp`` the file
    name gains the local time ``when`` (default: now), so each take gets
    its own file. Pure: nothing is created or probed.
    """
    raw = str(path).strip() or DEFAULT_NAME
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = recordings_dir() / p
    if timestamp:
        p = p.with_name(stamped_name(p.name, time.time() if when is None else when))
    return p


def is_in_recordings_dir(path: Path) -> bool:
    """True when ``path`` was resolved into the recordings folder (so the
    writer may create the folders it needs)."""
    try:
        Path(path).relative_to(recordings_dir())
    except ValueError:
        return False
    return True


def unique_path(path: Path) -> Path:
    """``path``, or ``stem_2.wav``, ``stem_3.wav``... if it already exists.

    Only timestamped takes use this: two takes inside the same second
    must not overwrite each other. Untimestamped takes overwrite their
    one name on purpose (no modal dialogs mid-take).
    """
    path = Path(path)
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def as_patch_path(chosen: str) -> str:
    """What a Browse pick stores in the patch.

    Inside the recordings folder -> the relative name (forward slashes),
    so the patch still records "into Music" on another machine; anywhere
    else -> the absolute path as picked.
    """
    p = Path(chosen)
    try:
        return p.relative_to(recordings_dir()).as_posix()
    except ValueError:
        return str(p)
