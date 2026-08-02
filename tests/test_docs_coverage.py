"""Drift tripwires: every registered module is documented and exported.

Found during the 2026-08-03 polish audit: `slew` and
`warping_buffered_speaker_output` shipped without a MODULES.md entry, and
three classes were missing from ``modules.__all__``. These tests make that
class of drift a test failure instead of an audit finding — a new module
isn't done until its docs heading, index row and export exist.
"""
import re
from pathlib import Path

import pysynthrack.modules as modules_pkg  # noqa: F401 — populate the registry
from pysynthrack.core.module import all_module_types

MODULES_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "MODULES.md"
).read_text(encoding="utf-8")


def _doc_headings() -> set[str]:
    # Entries render as ``#### `type_name` `` (h3 also accepted, matching
    # the audit that found the gaps).
    return set(re.findall(r"^#{3,4}\s+`([a-z0-9_]+)`", MODULES_MD, re.M))


def test_every_registered_type_has_a_docs_entry():
    missing = sorted(set(all_module_types()) - _doc_headings())
    assert not missing, (
        f"registered module types without a MODULES.md entry: {missing} — "
        "add a `#### `<type>`` section (and an index row)"
    )


def test_every_docs_entry_is_a_registered_type():
    stale = sorted(_doc_headings() - set(all_module_types()))
    assert not stale, (
        f"MODULES.md documents types that aren't registered: {stale} — "
        "renamed or removed module? Update the docs"
    )


def test_every_registered_type_has_an_index_row():
    # The index table links every entry as [`type`](#type).
    rows = set(re.findall(r"\[`([a-z0-9_]+)`\]\(#\1\)", MODULES_MD))
    missing = sorted(set(all_module_types()) - rows)
    assert not missing, (
        f"module types missing from the MODULES.md index table: {missing}"
    )


def test_every_registered_class_is_exported():
    exported = set(modules_pkg.__all__)
    registered = {cls.__name__ for cls in all_module_types().values()}
    missing = sorted(registered - exported)
    assert not missing, (
        f"registered module classes missing from modules.__all__: {missing}"
    )
    # And nothing in __all__ that doesn't resolve.
    for name in exported:
        assert hasattr(modules_pkg, name), f"__all__ names unknown {name!r}"
