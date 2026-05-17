"""Tests for the crash-report writer used by the GUI and audio-callback
catch points.

We monkeypatch ``Path.home`` to a tmp dir so tests don't pollute the
real user profile. The helper never raises, so the assertions are
mostly about: did it write the right file, did it return the right
path, does it survive a hostile environment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pysynthrack._crash import crash_dir, write_crash_report
from pysynthrack.error_handler import describe_error


class _FakeReport:
    """Stand-in for ErrorReport with a controllable for_claude()."""

    def __init__(self, text="HEAVY REPORT BODY", raise_in_for_claude=False, raise_in_str=False):
        self._text = text
        self._raise_for_claude = raise_in_for_claude
        self._raise_str = raise_in_str

    def for_claude(self):
        if self._raise_for_claude:
            raise RuntimeError("for_claude exploded")
        return self._text

    def __str__(self):
        if self._raise_str:
            raise RuntimeError("__str__ exploded")
        return f"<FakeReport text={self._text!r}>"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect Path.home() to tmp_path for the duration of one test."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class TestCrashDir:
    def test_crash_dir_path(self, home):
        d = crash_dir()
        assert d == home / ".pysynthrack" / "crashes"

    def test_crash_dir_does_not_create(self, home):
        # Pure inspection - it does not mkdir.
        crash_dir()
        assert not (home / ".pysynthrack").exists()


class TestWriteCrashReport:
    def test_writes_file_with_for_claude_body(self, home):
        report = _FakeReport("DEEP REPORT")
        path = write_crash_report(report, source="gui")
        assert path is not None
        p = Path(path)
        assert p.exists()
        assert p.read_text(encoding="utf-8") == "DEEP REPORT"

    def test_creates_directory_if_missing(self, home):
        assert not (home / ".pysynthrack").exists()
        path = write_crash_report(_FakeReport(), source="gui")
        assert path is not None
        assert (home / ".pysynthrack" / "crashes").is_dir()

    def test_filename_includes_timestamp_and_source(self, home):
        path = write_crash_report(_FakeReport(), source="audio_callback")
        assert path is not None
        name = Path(path).name
        assert name.startswith("crash_")
        assert name.endswith("_audio_callback.txt")
        # 4-digit year somewhere in the filename.
        assert any(c.isdigit() for c in name)

    def test_source_sanitization(self, home):
        # Unsafe chars get replaced with underscores so filenames stay
        # legal on every platform.
        path = write_crash_report(_FakeReport(), source="some/path:weird*name")
        assert path is not None
        name = Path(path).name
        for forbidden in "/\\:*?\"<>|":
            assert forbidden not in name

    def test_empty_source_becomes_unknown(self, home):
        path = write_crash_report(_FakeReport(), source="")
        assert path is not None
        assert "_unknown.txt" in Path(path).name

    def test_falls_back_to_str_when_for_claude_raises(self, home):
        report = _FakeReport(raise_in_for_claude=True)
        path = write_crash_report(report, source="gui")
        assert path is not None
        body = Path(path).read_text(encoding="utf-8")
        assert "FakeReport" in body  # the __str__ fallback fires

    def test_falls_back_to_placeholder_when_everything_raises(self, home):
        # for_claude AND __str__ both raise - still returns a path,
        # body is the literal placeholder.
        report = _FakeReport(raise_in_for_claude=True, raise_in_str=True)
        path = write_crash_report(report, source="gui")
        assert path is not None
        body = Path(path).read_text(encoding="utf-8")
        assert body == "<crash report object could not be formatted>"

    def test_returns_none_when_home_unwritable(self, tmp_path, monkeypatch):
        # Point Path.home at a file (not a directory) so mkdir fails.
        bogus_home = tmp_path / "im-a-file"
        bogus_home.write_text("nope")
        monkeypatch.setattr(Path, "home", lambda: bogus_home)
        result = write_crash_report(_FakeReport(), source="gui")
        assert result is None


class TestEndToEndWithRealReport:
    """Integration: feed a real describe_error result through the writer."""

    def test_real_error_report_round_trip(self, home):
        try:
            int("not a number")
        except ValueError as e:
            report = describe_error(e)
            path = write_crash_report(report, source="test")
            assert path is not None
            body = Path(path).read_text(encoding="utf-8")
            # The heavy edition should mention the exception type and
            # the message somewhere.
            assert "ValueError" in body
            assert "invalid literal" in body
