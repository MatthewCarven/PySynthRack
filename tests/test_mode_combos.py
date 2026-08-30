"""Every ``mode`` dropdown must offer *that module's* modes.

The bug this pins, which has now happened twice. ``_add_param_widget`` has
one shared branch for ``mode``/``mode_neg`` that runs **before** the
per-module TYPE blocks, and its ``else`` arm hands out the *filter's*
items. So a module whose ``mode`` is handled in a TYPE block further down
is silently shadowed, and its panel offers ``lowpass / highpass /
bandpass`` instead of its own modes:

* ``cv_to_frequency`` did it until 2026-06-07;
* ``sampler`` did it from the day it shipped (2026-08-22) until
  2026-08-30 — which meant ``gated`` was unreachable from the GUI for the
  whole of slice 1, because the renderer rejects a mode it doesn't know
  and quietly falls back to ``one_shot``. Nothing crashed, nothing looked
  wrong, and the module's own widget test passed the whole time: it
  asserted ``"mode" in combos``, and the *filter's* combo carries that
  label too. Matching a label is not the same as matching the widget.

So this checks the contents, not the label, and it walks the registry
rather than naming modules — a new module with a ``mode`` param is covered
the day it registers, whether or not anyone remembers this file.

The assertion is deliberately weak on purpose: a module's own default must
appear in its own dropdown. It cannot tell you the list is *complete*, but
it catches the entire class of "this combo belongs to another module",
which is the only way this has ever actually broken.
"""
from __future__ import annotations

from unittest import mock

import pytest

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.modules  # noqa: F401  (registers module types)
import pysynthrack.ui.app as app_mod
from pysynthrack.core import Patch
from pysynthrack.core.module import all_module_types

MODE_PARAMS = ("mode", "mode_neg")


def _types_with_a_mode():
    out = []
    for type_name, cls in sorted(all_module_types().items()):
        params = getattr(cls, "DEFAULT_PARAMS", {}) or {}
        for name in MODE_PARAMS:
            if name in params:
                out.append((type_name, name, str(params[name])))
    return out


TYPES_WITH_A_MODE = _types_with_a_mode()


def _combo_items(monkeypatch, type_name, param_name):
    """Build one module's node under a mocked dpg; return the items of the
    combo it made for ``param_name`` (None if it made none)."""
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module(type_name)
    # Only the calls made for THIS node — constructing the App itself also
    # builds combos, and one of them is the filter's.
    before = len(app_mod.dpg.add_combo.call_args_list)
    app._create_node_for_module(module)
    calls = app_mod.dpg.add_combo.call_args_list[before:]
    for call in calls:
        # Labels may be decorated ("root (the note the sample is)"), so
        # match on the param name appearing in the label.
        if param_name in str(call.kwargs.get("label")):
            return list(call.kwargs.get("items") or [])
    return None


def test_the_registry_actually_has_modules_with_modes():
    # Guard against a walk that silently finds nothing, which would make
    # every parametrized case below vacuously pass.
    assert len(TYPES_WITH_A_MODE) > 10, TYPES_WITH_A_MODE


@pytest.mark.parametrize(
    "type_name,param_name,default",
    TYPES_WITH_A_MODE,
    ids=[f"{t}.{p}" for t, p, _d in TYPES_WITH_A_MODE],
)
def test_a_mode_combo_offers_its_own_modules_modes(
    monkeypatch, type_name, param_name, default
):
    items = _combo_items(monkeypatch, type_name, param_name)
    assert items is not None, (
        f"{type_name}.{param_name} got no combo at all — it fell through to "
        f"a text box or a drag, and a mode is a choice"
    )
    assert default in items, (
        f"{type_name}.{param_name} defaults to {default!r} but its dropdown "
        f"offers {items} — that combo belongs to another module"
    )


def test_the_sampler_offers_its_three_modes(monkeypatch):
    """The regression itself, named. `loop` is slice 2's whole deliverable
    and it reaches the user through this one dropdown."""
    from pysynthrack.modules.sampler import SAMPLER_MODES

    assert _combo_items(monkeypatch, "sampler", "mode") == list(SAMPLER_MODES)
