"""The app icon ships, holds every size Windows asks for, and is optional.

Reads the ``.ico`` directory header directly (no Pillow needed): a 6-byte
ICONDIR, then one 16-byte entry per image whose first byte is the width
(0 means 256)."""
from __future__ import annotations

import struct

from pysynthrack import _resources


def _ico_sizes(path):
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1), "not an .ico"
    return sorted(data[6 + 16 * i] or 256 for i in range(count))


def test_the_icon_ships_with_every_windows_size():
    icon = _resources.app_icon()
    assert icon is not None and icon.name == "icon.ico"
    assert _ico_sizes(icon) == [16, 24, 32, 48, 64, 128, 256]


def test_a_missing_icon_is_none_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(_resources, "resource_root", lambda: tmp_path)
    assert _resources.app_icon() is None
