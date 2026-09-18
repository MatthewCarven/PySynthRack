"""PossibilitySelector's front panel — the possibility panel one level up.

Same two halves as `test_possibility_panel.py`: the dpg-free helpers in the
module file (`next_state`, `undecided_count`, `possibility_count`,
`format_possibilities`, plus `parse_route` / `format_route` that the popup
writes through), then `App._build_selector_panel` and its callbacks driven
with DearPyGui mocked out, so the click-to-cycle gesture, the candidate
popup and the model writes are pinned without a window. Colours, popup
placement and hover behaviour are dpg-only and get a real-window eyeball.

Skips cleanly where DearPyGui can't be imported.
"""
from __future__ import annotations

import itertools
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.possibility_selector import (
    MAX_STEPS,
    N_OUTS,
    ROUTE_STATES,
    format_possibilities,
    next_state,
    possibility_count,
    undecided_count,
)


# ----- the dpg-free helpers --------------------------------------------------

def test_next_state_cycles_rest_each_output_then_open():
    """The panel's one gesture: 0 -> 1 -> 2 -> 3 -> 4 -> ? -> 0."""
    assert next_state("0") == "1"
    assert next_state("1") == "2"
    assert next_state("2") == "3"
    assert next_state("3") == "4"
    assert next_state("4") == "?"
    assert next_state("?") == "0"


def test_next_state_covers_every_cycle_state_in_six_clicks():
    seen = []
    state = "0"
    for _ in range(len(ROUTE_STATES)):
        seen.append(state)
        state = next_state(state)
    assert sorted(seen) == sorted(ROUTE_STATES)
    assert state == "0"


def test_next_state_treats_a_subset_as_open_and_rescues_junk():
    # A popup-written subset sits in the ? slot: the next click is a rest.
    assert next_state("13") == "0"
    assert next_state("234") == "0"
    assert next_state("banana") == "1"      # junk parses as a rest -> 1
    assert next_state("") == "1"


def test_undecided_count_counts_subsets_and_ignores_parked_steps():
    states = ["?", "13", "1", "0"] + ["?"] * 12
    assert undecided_count(states, 16) == 14
    assert undecided_count(states, 4) == 2
    assert undecided_count(states, 2) == 2
    assert undecided_count(states, 1) == 1


def test_possibility_count_is_the_product_of_the_open_choices():
    assert possibility_count(["1", "2", "3", "4"] * 4, 16) == 1
    assert possibility_count(["?", "1", "1", "1"] * 4, 16) == 4 ** 4
    assert possibility_count(["13", "1", "1", "1"] * 4, 16) == 2 ** 4
    assert possibility_count(["?", "13", "234", "0"], 4) == 4 * 2 * 3
    assert possibility_count(["?"] * 16, 16) == 4 ** 16


def test_possibility_count_matches_the_factory_pattern():
    """The docstring promises 65,536 bars out of the box; pinned."""
    from pysynthrack.core.module import get_module_type

    default = dict(get_module_type("possibility_selector").DEFAULT_PARAMS)
    states = [default[f"step{i}_state"] for i in range(1, MAX_STEPS + 1)]
    assert possibility_count(states, 16) == 65536


def test_format_possibilities_reads_as_english():
    assert format_possibilities(["1", "2", "3", "4"] * 4, 16) == "no ? -> 1 bar, decided"
    assert format_possibilities(["?", "1", "1", "1"] * 4, 16) == "4 ? -> 256 possible bars"
    assert format_possibilities(["?"] * 16, 16) == "16 ? -> 4,294,967,296 possible bars"


# ----- the panel itself ------------------------------------------------------

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.ui.app as app_mod  # noqa: E402


def _mock_dpg():
    """A dpg stand-in handing out *distinct* widget ids (the possibility
    panel's lesson: a bare MagicMock returns one object from every
    ``add_button`` call and sixteen cells become indistinguishable)."""
    dpg = mock.MagicMock()
    ids = itertools.count(1000)
    for factory in ("add_button", "add_text", "add_slider_int",
                    "add_slider_float", "add_checkbox", "add_combo",
                    "add_drag_int"):
        getattr(dpg, factory).side_effect = lambda *a, **k: next(ids)
    return dpg


def _make_app(monkeypatch, compiled=True):
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", _mock_dpg())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    module = app.patch.add_module("possibility_selector")
    if compiled:
        app.backend.compile(app.patch)
    app._build_selector_panel(module)
    return app, module


def _pattern(module):
    return [module.params[f"step{i}_state"] for i in range(1, MAX_STEPS + 1)]


def test_panel_builds_one_cell_per_step_and_four_checkboxes_each(monkeypatch):
    app, module = _make_app(monkeypatch)
    assert len(app._psel_cells[module.id]) == MAX_STEPS
    assert module.id in app._psel_count_labels
    dpg = app_mod.dpg
    # sixteen popups x four outputs, plus the balanced checkbox.
    boxes = [c for c in dpg.add_checkbox.call_args_list
             if str(c.kwargs.get("label", "")).startswith("out")]
    assert len(boxes) == MAX_STEPS * N_OUTS
    assert {c.kwargs["label"] for c in boxes} == {"out1", "out2", "out3", "out4"}


def test_panel_replaces_the_generic_param_rows(monkeypatch):
    """Built through the real node path so the dispatch branch is covered."""
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", _mock_dpg())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    module = app.patch.add_module("possibility_selector")
    app._create_node_for_module(module)
    assert len(app._psel_cells[module.id]) == MAX_STEPS
    assert [v for v in app._param_widgets.values() if v[0] == module.id] == []


def test_clicking_a_cell_cycles_that_step_only(monkeypatch):
    app, module = _make_app(monkeypatch)
    before = _pattern(module)
    assert before[0] == "1"                     # factory: kick on the downbeat
    app._on_selector_step(None, None, (module.id, 1))
    assert module.params["step1_state"] == "2"
    assert _pattern(module)[1:] == before[1:]


def test_six_clicks_return_a_step_to_where_it_started(monkeypatch):
    app, module = _make_app(monkeypatch)
    start = module.params["step3_state"]
    for _ in range(len(ROUTE_STATES)):
        app._on_selector_step(None, None, (module.id, 3))
    assert module.params["step3_state"] == start


def test_a_click_sticks_before_the_first_start(monkeypatch):
    """The pre-Start param rule: the panel writes the model itself."""
    app, module = _make_app(monkeypatch, compiled=False)
    assert app.backend._patch is None
    app._on_selector_step(None, None, (module.id, 1))
    assert module.params["step1_state"] == "2"


def test_candidate_popup_writes_a_canonical_subset(monkeypatch):
    app, module = _make_app(monkeypatch)
    assert module.params["step2_state"] == "?"              # factory: open
    app._on_selector_candidate(None, False, (module.id, 2, 2))   # drop the snare
    assert module.params["step2_state"] == "134"
    app._on_selector_candidate(None, False, (module.id, 2, 4))
    assert module.params["step2_state"] == "13"
    app._on_selector_candidate(None, False, (module.id, 2, 1))
    assert module.params["step2_state"] == "3"              # one left: decided
    app._on_selector_candidate(None, False, (module.id, 2, 3))
    assert module.params["step2_state"] == "0"              # none: a rest
    for k in (4, 3, 2, 1):
        app._on_selector_candidate(None, True, (module.id, 2, k))
    assert module.params["step2_state"] == "?"              # all four: plain ?
    assert module.params["step3_state"] == "3"              # the neighbour is untouched


def test_the_popup_and_the_click_cycle_agree(monkeypatch):
    # Click a subset: it sits in the ? slot, so the next click is a rest.
    app, module = _make_app(monkeypatch)
    app._on_selector_candidate(None, False, (module.id, 2, 1))   # "234"
    app._on_selector_step(None, None, (module.id, 2))
    assert module.params["step2_state"] == "0"


def test_settings_row_writes_mode_balanced_and_seed(monkeypatch):
    app, module = _make_app(monkeypatch)
    app._on_selector_param(None, "latch", (module.id, "mode"))
    app._on_selector_param(None, True, (module.id, "balanced"))
    app._on_selector_param(None, 42, (module.id, "seed"))
    assert module.params["mode"] == "latch"
    assert module.params["balanced"] is True
    assert module.params["seed"] == 42
    assert isinstance(module.params["seed"], int)


def test_steps_slider_and_weight_sliders_write(monkeypatch):
    app, module = _make_app(monkeypatch)
    app._on_selector_steps(None, 8, (module.id, "steps"))
    assert module.params["steps"] == 8
    assert isinstance(module.params["steps"], int)
    app._on_selector_weight(None, 0.25, (module.id, 4))
    assert module.params["weight4"] == pytest.approx(0.25)
    assert module.params["weight1"] == pytest.approx(1.0)


def test_count_readout_tracks_the_register(monkeypatch):
    app, module = _make_app(monkeypatch)
    label = app._psel_count_labels[module.id]
    dpg = app_mod.dpg

    def _last_count_text():
        for call in reversed(dpg.set_value.call_args_list):
            if call.args and call.args[0] == label:
                return call.args[1]
        return None

    app._refresh_selector_panel(module.id)
    assert _last_count_text() == "8 ? -> 65,536 possible bars"   # factory
    app._on_selector_steps(None, 8, (module.id, "steps"))
    assert _last_count_text() == "4 ? -> 256 possible bars"
    app._on_selector_candidate(None, False, (module.id, 2, 1))   # ? -> 234
    assert _last_count_text() == "4 ? -> 192 possible bars"
    app._on_selector_step(None, None, (module.id, 2))            # 234 -> 0
    assert _last_count_text() == "3 ? -> 64 possible bars"


def test_cells_repaint_from_the_model_including_the_popup_ticks(monkeypatch):
    app, module = _make_app(monkeypatch)
    dpg = app_mod.dpg
    cells = app._psel_cells[module.id]
    module.params["step5_state"] = "31"           # a hand-edited file
    dpg.set_item_label.reset_mock()
    dpg.set_value.reset_mock()
    app._refresh_selector_panel(module.id)
    drawn = {c.args[0]: c.args[1] for c in dpg.set_item_label.call_args_list}
    assert drawn[cells[4]] == "13"                # canonical on the face
    assert drawn[cells[0]] == "1"
    ticks = {c.args[0]: c.args[1] for c in dpg.set_value.call_args_list
             if isinstance(c.args[0], str) and c.args[0].startswith("psel_cand_")}
    assert ticks[f"psel_cand_{module.id}_5_1"] is True
    assert ticks[f"psel_cand_{module.id}_5_2"] is False
    assert ticks[f"psel_cand_{module.id}_5_3"] is True
    assert ticks[f"psel_cand_{module.id}_5_4"] is False


def test_a_second_node_gets_its_own_cells(monkeypatch):
    app, first = _make_app(monkeypatch)
    second = app.patch.add_module("possibility_selector")
    app._build_selector_panel(second)
    assert app._psel_cells[first.id] != app._psel_cells[second.id]
    app._on_selector_step(None, None, (second.id, 1))
    assert first.params["step1_state"] != second.params["step1_state"]


def test_callbacks_survive_a_deleted_module(monkeypatch):
    app, module = _make_app(monkeypatch)
    app.patch.remove_module(module.id)
    app._on_selector_step(None, None, (module.id, 1))
    app._on_selector_candidate(None, True, (module.id, 1, 1))
    app._on_selector_steps(None, 4, (module.id, "steps"))
    app._on_selector_weight(None, 0.3, (module.id, 1))
    app._refresh_selector_panel(module.id)


# ----- panel -> engine -------------------------------------------------------

def test_a_clicked_register_is_what_the_engine_routes(monkeypatch):
    """Click a four-step register into 2, 0, 13, 4 and read the outputs."""
    app, module = _make_app(monkeypatch)
    app._on_selector_steps(None, 4, (module.id, "steps"))
    for i in range(1, 5):
        while module.params[f"step{i}_state"] != "0":
            app._on_selector_step(None, None, (module.id, i))
    for _ in range(2):
        app._on_selector_step(None, None, (module.id, 1))           # 0 -> 1 -> 2
    app._on_selector_candidate(None, True, (module.id, 3, 1))       # 0 -> 1
    app._on_selector_candidate(None, True, (module.id, 3, 3))       # 1 -> 13
    for _ in range(4):
        app._on_selector_step(None, None, (module.id, 4))           # 0 -> 4
    assert _pattern(module)[:4] == ["2", "0", "13", "4"]

    src = app.patch.add_module("clock")
    app.patch.connect(src.id, "out", module.id, "in")
    backend = NumpyBackend()
    backend.compile(app.patch)
    step, width, n = 20, 4, 8
    gate = np.zeros(n * step, dtype=np.float32)
    for k in range(n):
        gate[k * step: k * step + width] = 1.0
    out = backend._render_possibility_selector(
        module, len(gate), {(src.id, "out"): gate}, app.patch)
    routes = []
    for k in range(n):
        hit = [j for j in range(1, 5) if out[f"out{j}"][k * step] > 0.5]
        routes.append(hit[0] if hit else 0)
    for bar in (routes[:4], routes[4:]):
        assert bar[0] == 2 and bar[1] == 0 and bar[3] == 4
        assert bar[2] in (1, 3)                     # the open step, either string
