"""Tests for module CATEGORY grouping (Add-module menu submenus).

Every registered module type declares a CATEGORY class attribute placing it
in one of the known Add-menu groups, and grouped_module_types() partitions
the registry: every type appears exactly once, categories come out in
CATEGORY_ORDER, and names are sorted within each group.
"""
import pysynthrack.modules  # noqa: F401 — populate the registry
from pysynthrack.core.module import (
    CATEGORY_ORDER,
    Module,
    all_module_types,
    grouped_module_types,
    register_module_type,
)


def test_every_type_has_known_category():
    for type_name, cls in all_module_types().items():
        assert cls.CATEGORY in CATEGORY_ORDER, (
            f"{type_name!r} has CATEGORY {cls.CATEGORY!r}, which is not in "
            f"CATEGORY_ORDER — add it to the list or pick an existing group"
        )


def test_groups_partition_registry():
    grouped = grouped_module_types()
    seen = [t for _, names in grouped for t in names]
    assert sorted(seen) == sorted(all_module_types())
    assert len(seen) == len(set(seen))


def test_group_order_and_inner_sort():
    grouped = grouped_module_types()
    cats = [c for c, _ in grouped]
    known = [c for c in cats if c in CATEGORY_ORDER]
    assert known == [c for c in CATEGORY_ORDER if c in cats]
    for _, names in grouped:
        assert names == sorted(names)
        assert names, "empty categories should be skipped"


def test_no_empty_known_category_emitted():
    # Every CATEGORY_ORDER entry currently has at least one module; if one
    # is ever emptied it should vanish from the menu rather than render
    # as a dead submenu.
    grouped = dict(grouped_module_types())
    for cat in CATEGORY_ORDER:
        assert grouped.get(cat, ["nonempty"]) != []


def test_unknown_category_lands_in_trailing_group():
    class _Oddball(Module):
        TYPE = "_test_oddball"
        # deliberately NOT setting CATEGORY — base default is "Other"

    register_module_type(_Oddball)
    try:
        grouped = grouped_module_types()
        cats = [c for c, _ in grouped]
        assert "Other" in cats
        assert cats.index("Other") > max(
            cats.index(c) for c in cats if c in CATEGORY_ORDER
        )
        assert "_test_oddball" in dict(grouped)["Other"]
    finally:
        from pysynthrack.core import module as module_mod

        module_mod._REGISTRY.pop("_test_oddball", None)
