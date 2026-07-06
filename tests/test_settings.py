"""Tests for the global settings store (``pysynthrack.settings``).

All reads/writes target a ``tmp_path`` or an env-overridden path, so the real
user config file is never touched.
"""

from pysynthrack import settings as s


# ----- settings_path ------------------------------------------------------

def test_path_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom.json"
    monkeypatch.setenv("PYSYNTHRACK_SETTINGS", str(target))
    assert s.settings_path() == target


def test_path_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("PYSYNTHRACK_SETTINGS", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert s.settings_path() == tmp_path / "PySynthRack" / "settings.json"


def test_path_falls_back_to_xdg_then_home(monkeypatch, tmp_path):
    monkeypatch.delenv("PYSYNTHRACK_SETTINGS", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert s.settings_path() == tmp_path / "PySynthRack" / "settings.json"


# ----- load_settings (total: never raises) --------------------------------

def test_load_missing_returns_empty(tmp_path):
    assert s.load_settings(tmp_path / "nope.json") == {}


def test_load_corrupt_returns_empty(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    assert s.load_settings(bad) == {}


def test_load_non_dict_returns_empty(tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    assert s.load_settings(arr) == {}


def test_load_directory_path_returns_empty(tmp_path):
    # A path that is a directory raises IsADirectoryError (OSError) -> {}.
    assert s.load_settings(tmp_path) == {}


# ----- save_settings ------------------------------------------------------

def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    s.save_settings({"buffer_size": 256, "note": "hi"}, path)
    assert s.load_settings(path) == {"buffer_size": 256, "note": "hi"}


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "c.json"
    s.save_settings({"k": 1}, path)
    assert path.is_file()


def test_save_leaves_no_tmp_sidecar(tmp_path):
    path = tmp_path / "s.json"
    s.save_settings({"k": 1}, path)
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_overwrites_existing(tmp_path):
    path = tmp_path / "s.json"
    s.save_settings({"buffer_size": 64}, path)
    s.save_settings({"buffer_size": 1024}, path)
    assert s.load_settings(path) == {"buffer_size": 1024}
