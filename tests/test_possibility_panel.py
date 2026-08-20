"""PossibilitySeq's front panel — the one-gesture step face (headless).

Two halves. The first tests the dpg-free helpers in the module file
(`next_state`, `undecided_count`, `possibility_count`,
`format_possibilities`) — the panel and these tests share one truth, so
the on-screen readout can't drift from the semantics. The second drives
`App._build_possibility_panel` and its callbacks with DearPyGui mocked out
(`app.dpg` swapped for a MagicMock), the `test_key_trigger_ui.py` pattern,
so the click-to-cycle gesture and the model writes are pinned without a
window. Colours, popup placement and hover behaviour are dpg-only and get
a real-window eyeball.

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
from pysynthrack.modules.possibility_seq import (
    MAX_STEPS,
    STEP_STATES,
    format_possibilities,
    next_state,
    possibility_count,
    undecided_count,
)


# ----- the dpg-free helpers --------------------------------------------------

def test_next_state_cycles_zero_one_undecided():
    """The panel's one gesture: 0 -> 1 -> ? -> 0, and round again."""
    assert next_state("0") == "1"
    assert next_state("1") == "?"
    assert next_state("?") == "0"


def test_next_state_covers_every_legal_state_in_three_clicks():
    seen = []
    state = "0"
    for _ in range(len(STEP_STATES)):
        seen.append(state)
        state = next_state(state)
    assert sorted(seen) == sorted(STEP_STATES)
    assert state == "0"          # back where it started


def test_next_state_rescues_a_junk_state():
    """A hand-edited patch file with a bad state still cycles sensibly."""
    assert next_state("banana") == "0"
    assert next_state("") == "0"


def test_undecided_count_ignores_steps_past_the_loop_length():
    states = list("??00" + "?" * 12)
    assert undecided_count(states, 16) == 14
    assert undecided_count(states, 4) == 2      # the parked ?s don't count
    assert undecided_count(states, 2) == 2
    assert undecided_count(states, 1) == 1


def test_possibility_count_is_two_to_the_undecided():
    assert possibility_count(list("1010" * 4), 16) == 1        # nothing open
    assert possibility_count(list("?010" * 4), 16) == 2 ** 4
    assert possibility_count(list("?" * 16), 16) == 65536


def test_possibility_count_matches_the_factory_pattern():
    """The default pattern advertises sixteen bars — the module docstring's
    claim, pinned so the two can't drift."""
    default = PossibilitySeqDefaults()
    states = [default[f"step{i}_state"] for i in range(1, MAX_STEPS + 1)]
    assert possibility_count(states, 16) == 16


def PossibilitySeqDefaults():
    from pysynthrack.core.module import get_module_type
    return dict(get_module_type("possibility_seq").DEFAULT_PARAMS)


def test_format_possibilities_reads_as_english():
    assert format_possibilities(list("1010" * 4), 16) == "no ? -> 1 bar, decided"
    assert format_possibilities(list("?010" * 4), 16) == "4 ? -> 16 possible bars"
    # Thousands separator, so 65536 doesn't read as a phone number.
    assert format_possibilities(list("?" * 16), 16) == "16 ? -> 65,536 possible bars"


# ----- the panel itself ------------------------------------------------------

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.ui.app as app_mod  # noqa: E402


def _mock_dpg():
    """A dpg stand-in that hands out *distinct* widget ids.

    A bare MagicMock returns one shared object from every ``add_button``
    call, which would make sixteen cells indistinguishable — and hide the
    very mix-ups these tests are for.
    """
    dpg = mock.MagicMock()
    ids = itertools.count(1000)
    for factory in ("add_button", "add_text", "add_slider_int",
                    "add_slider_float", "add_checkbox", "add_combo",
                    "add_drag_int"):
        getattr(dpg, factory).side_effect = lambda *a, **k: next(ids)
    return dpg


def _make_app(monkeypatch, compiled=True):
    """An App with dpg mocked out. ``compiled=False`` reproduces the state
    the GUI is actually in before the first Start: the backend holds no
    patch, so its ``set_param`` is a no-op."""
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", _mock_dpg())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    module = app.patch.add_module("possibility_seq")
    if compiled:
        app.backend.compile(app.patch)
    app._build_possibility_panel(module)
    return app, module


def _pattern(module):
    return "".join(module.params[f"step{i}_state"] for i in range(1, MAX_STEPS + 1))


def test_panel_builds_one_cell_per_step(monkeypatch):
    app, module = _make_app(monkeypatch)
    assert len(app._pseq_cells[module.id]) == MAX_STEPS
    assert module.id in app._pseq_count_labels


def test_panel_replaces_the_generic_param_rows(monkeypatch):
    """The whole point: the node draws a panel, not 36 labelled rows. Built
    through the real node path so the dispatch branch is covered too."""
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", _mock_dpg())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    module = app.patch.add_module("possibility_seq")
    app._create_node_for_module(module)
    assert len(app._pseq_cells[module.id]) == MAX_STEPS
    # Not one generic param widget registered for this module.
    assert [v for v in app._param_widgets.values() if v[0] == module.id] == []


def test_clicking_a_cell_cycles_that_step_only(monkeypatch):
    app, module = _make_app(monkeypatch)
    before = _pattern(module)
    assert before[0] == "1"                     # factory pattern starts on a hit
    app._on_possibility_step(None, None, (module.id, 1))
    assert module.params["step1_state"] == "?"
    assert _pattern(module)[1:] == before[1:]   # neighbours untouched


def test_three_clicks_return_a_step_to_where_it_started(monkeypatch):
    app, module = _make_app(monkeypatch)
    start = module.params["step3_state"]
    for _ in range(3):
        app._on_possibility_step(None, None, (module.id, 3))
    assert module.params["step3_state"] == start


def test_a_click_sticks_before_the_first_start(monkeypatch):
    """Regression pin: the backend's set_param is a no-op until a patch has
    been compiled into it (which only happens at Start), so the panel writes
    the model itself. Without that, a cell clicked on a freshly opened patch
    would flip on screen and be saved as its old state."""
    app, module = _make_app(monkeypatch, compiled=False)
    assert app.backend._patch is None           # nothing compiled yet
    app._on_possibility_step(None, None, (module.id, 1))
    assert module.params["step1_state"] == "?"  # the model recorded it anyway


def test_odds_popup_writes_the_step_probability(monkeypatch):
    app, module = _make_app(monkeypatch)
    app._on_possibility_odds(None, 0.2, (module.id, 2))
    assert module.params["step2_p"] == pytest.approx(0.2)
    # Only that step's odds moved.
    assert module.params["step3_p"] == pytest.approx(0.5)


def test_settings_row_writes_mode_balanced_and_seed(monkeypatch):
    app, module = _make_app(monkeypatch)
    app._on_possibility_param(None, "latch", (module.id, "mode"))
    app._on_possibility_param(None, True, (module.id, "balanced"))
    app._on_possibility_param(None, 42, (module.id, "seed"))
    assert module.params["mode"] == "latch"
    assert module.params["balanced"] is True
    assert module.params["seed"] == 42
    assert isinstance(module.params["seed"], int)   # not a float from the drag


def test_steps_slider_writes_an_int(monkeypatch):
    app, module = _make_app(monkeypatch)
    app._on_possibility_steps(None, 8, (module.id, "steps"))
    assert module.params["steps"] == 8
    assert isinstance(module.params["steps"], int)


def test_count_readout_tracks_the_pattern(monkeypatch):
    """The headline number: it follows both the ?s and the loop length."""
    app, module = _make_app(monkeypatch)
    label = app._pseq_count_labels[module.id]
    dpg = app_mod.dpg

    def _last_count_text():
        for call in reversed(dpg.set_value.call_args_list):
            if call.args and call.args[0] == label:
                return call.args[1]
        return None

    app._refresh_possibility_panel(module.id)
    assert _last_count_text() == "4 ? -> 16 possible bars"   # factory pattern
    # Halving the loop parks the back eight steps -> two ?s left.
    app._on_possibility_steps(None, 8, (module.id, "steps"))
    assert _last_count_text() == "2 ? -> 4 possible bars"
    # Deciding one of those shrinks it again.
    app._on_possibility_step(None, None, (module.id, 2))     # ? -> 0
    assert _last_count_text() == "1 ? -> 2 possible bars"


def test_cells_repaint_from_the_model(monkeypatch):
    """Every cell's label is redrawn from the patch, so the face can never
    show a pattern the model doesn't hold."""
    app, module = _make_app(monkeypatch)
    dpg = app_mod.dpg
    cells = app._pseq_cells[module.id]
    dpg.set_item_label.reset_mock()
    app._refresh_possibility_panel(module.id)
    drawn = {c.args[0]: c.args[1] for c in dpg.set_item_label.call_args_list}
    for i, cell in enumerate(cells, start=1):
        assert drawn[cell] == module.params[f"step{i}_state"]


def test_a_second_node_gets_its_own_cells(monkeypatch):
    app, first = _make_app(monkeypatch)
    second = app.patch.add_module("possibility_seq")
    app._build_possibility_panel(second)
    assert app._pseq_cells[first.id] != app._pseq_cells[second.id]
    app._on_possibility_step(None, None, (second.id, 1))
    assert first.params["step1_state"] != second.params["step1_state"]


def test_callbacks_survive_a_deleted_module(monkeypatch):
    """A stale callback (node already gone) must not raise."""
    app, module = _make_app(monkeypatch)
    app.patch.remove_module(module.id)
    app._on_possibility_step(None, None, (module.id, 1))
    app._on_possibility_steps(None, 4, (module.id, "steps"))
    app._on_possibility_odds(None, 0.3, (module.id, 1))
    app._refresh_possibility_panel(module.id)


# ----- panel -> engine -------------------------------------------------------

def test_a_clicked_pattern_is_what_the_engine_plays(monkeypatch):
    """The whole chain: click cells into an all-rest bar with one hit on
    step 1, and only step 1 fires. Pins the panel's writes against the
    renderer that reads them."""
    app, module = _make_app(monkeypatch)
    app._on_possibility_steps(None, 4, (module.id, "steps"))
    # Drive every step to "0", then step 1 up to "1".
    for i in range(1, 5):
        while module.params[f"step{i}_state"] != "0":
            app._on_possibility_step(None, None, (module.id, i))
    app._on_possibility_step(None, None, (module.id, 1))
    assert module.params["step1_state"] == "1"

    clk = app.patch.add_module("clock")
    app.patch.connect(clk.id, "out", module.id, "clock")
    backend = NumpyBackend()
    backend.compile(app.patch)
    step, width, n = 20, 4, 4
    clock = np.zeros(n * step, dtype=np.float32)
    for k in range(n):
        clock[k * step: k * step + width] = 1.0
    out = backend._render_possibility_seq(
        module, len(clock), {(clk.id, "out"): clock}, app.patch)
    fired = [bool(out["gate"][k * step] > 0.5) for k in range(n)]
    assert fired == [True, False, False, False]
