"""Dead cables are DROPPED and REPORTED, not loaded silently inert.

Found 2026-09-16 while building granular: ``granular_beat_repeat.json``
wired a clock's non-existent ``gate`` out into ``freeze``, the patch
loaded without a murmur, the freeze never engaged, and the only symptom
was "nothing happens". ``Patch.from_dict`` appended cables without
looking at them; ``Patch.connect`` had always checked. Since 2026-09-22
the loader runs every cable past ``Patch.cable_problem`` -- the same
checks ``connect`` makes -- drops the failures, and collects a line each
in ``patch.load_warnings``. It still never RAISES: a patch with one dead
cable must open and play the rest. Fail-soft only helps if something
says so, so the GUI puts the count on the status line and the list on
the console (the lesson from the relative-media-path fix).

Coverage: each of the four failure kinds (missing port, missing module,
mismatched signal kinds, a backwards cable) is dropped -- exactly it,
and every good cable survives -- and reported with type, id, port and
reason; an unreadable cable entry too; a clean patch reports nothing and
keeps every cable, shipped examples included; a dead-cable patch renders
without exception and renders EXACTLY as if the cable had never been
written; the deliberate non-check (a duplicate destination) is left
alone; the warnings and the status line are ASCII; and the GUI's load
path names the count.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401 -- registers module types for load
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Cable, Patch
from pysynthrack.io_patch import load_patch, patch_to_json

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


# ----- fixtures -------------------------------------------------------------


def _base() -> dict:
    """A small, entirely legal patch dict.

    ``lfo.cv -> oscillator.freq_cv`` is the cable the dead-cable cases
    corrupt; the other three carry audio to the speaker so the patch is
    still worth rendering with it gone.
    """
    p = Patch()
    osc = p.add_module("oscillator", params={"freq": 220.0, "amp": 0.5})
    lfo = p.add_module("lfo", params={"rate": 3.0})
    vca = p.add_module("vca")
    const = p.add_module("constant", params={"value": 1.0})
    spk = p.add_module("speaker_output")
    p.connect(lfo.id, "cv", osc.id, "freq_cv")      # cable 0 -- the victim
    p.connect(osc.id, "out", vca.id, "audio")
    p.connect(const.id, "out", vca.id, "cv")
    p.connect(vca.id, "out", spk.id, "in")
    data = p.to_dict()
    data["_ids"] = {
        "osc": osc.id, "lfo": lfo.id, "vca": vca.id,
        "const": const.id, "spk": spk.id,
    }
    return data


def _with_first_cable(**changes) -> tuple[dict, dict]:
    """``_base()`` with cable 0's fields overridden. Returns (data, ids)."""
    data = _base()
    ids = data.pop("_ids")
    data["cables"][0] = dict(data["cables"][0], **changes)
    return data, ids


def _without_first_cable() -> dict:
    data = _base()
    data.pop("_ids")
    data["cables"] = data["cables"][1:]
    return data


def _render(data: dict, blocks: int = 24, block: int = 256) -> np.ndarray:
    np.random.seed(4242)
    patch = Patch.from_dict(data)
    backend = NumpyBackend(sample_rate=44100, block_size=block)
    backend.compile(patch)
    outs = []
    for _ in range(blocks):
        out, _devices = backend.render_block_multi(block)
        outs.append(np.asarray(out).copy())
    return np.concatenate(outs, axis=0)


# ----- the four failure kinds -----------------------------------------------


class TestDroppedCables:
    def test_a_cable_to_a_nonexistent_port_is_dropped_and_reported(self):
        data, ids = _with_first_cable(dst_port="frequency_cv")
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 3
        assert all(c.dst_port != "frequency_cv" for c in patch.cables)
        assert len(patch.load_warnings) == 1
        line = patch.load_warnings[0]
        assert "oscillator" in line and f"#{ids['osc']}" in line
        assert "frequency_cv" in line and "no in-port" in line
        # and it names the ports that DO exist, so the fix is obvious
        assert "freq_cv" in line

    def test_a_cable_from_a_nonexistent_port_is_dropped_and_reported(self):
        data, ids = _with_first_cable(src_port="sub")
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 3
        line, = patch.load_warnings
        assert "lfo" in line and "no out-port" in line and "'sub'" in line

    def test_a_cable_to_a_nonexistent_module_is_dropped_and_reported(self):
        data, _ids = _with_first_cable(dst_module_id=9999)
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 3
        line, = patch.load_warnings
        assert "no module with id 9999" in line

    def test_a_cable_from_a_nonexistent_module_is_dropped_and_reported(self):
        data, _ids = _with_first_cable(src_module_id=9999)
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 3
        line, = patch.load_warnings
        assert "no module with id 9999" in line

    def test_mismatched_signal_kinds_are_dropped_and_reported(self):
        """``connect`` rejects kind mismatches; so does the loader now.

        A clock's ``out`` is a GATE and an oscillator's ``freq_cv`` is
        CV -- both ports are real, so only the kind check catches this.
        """
        data = _base()
        ids = data.pop("_ids")
        data["modules"].append(
            {"id": 90, "type": "clock", "name": "clock", "params": {}}
        )
        data["cables"][0] = {
            "src_module_id": 90, "src_port": "out",
            "dst_module_id": ids["osc"], "dst_port": "freq_cv",
        }
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 3
        line, = patch.load_warnings
        assert "incompatible signal kinds" in line
        assert "gate -> cv" in line

    def test_a_backwards_cable_is_dropped_and_says_so(self):
        """Source port naming an IN-jack, destination port naming an OUT.

        ``get_port`` looks in one direction only, so a reversed cable
        reads as a missing port -- worth saying which way round it is.
        """
        data, ids = _with_first_cable(src_port="rate_cv")   # an lfo INPUT
        patch = Patch.from_dict(data)
        line, = patch.load_warnings
        assert "no out-port 'rate_cv'" in line
        assert "IN-port" in line and "backwards" in line

        data, ids = _with_first_cable(
            dst_module_id=ids["vca"], dst_port="out"        # a vca OUTPUT
        )
        patch = Patch.from_dict(data)
        line, = patch.load_warnings
        assert "no in-port 'out'" in line
        assert "OUT-port" in line and "backwards" in line

    def test_an_unreadable_cable_entry_is_dropped_not_raised(self):
        data = _base()
        data.pop("_ids")
        data["cables"].insert(0, {"src_module_id": 1, "src_port": "cv"})
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 4
        line, = patch.load_warnings
        assert "cable 0" in line and "unreadable" in line

    def test_only_the_dead_cable_goes(self):
        """Every OTHER cable survives, in order, unchanged."""
        good = Patch.from_dict(_without_first_cable())
        data, _ids = _with_first_cable(dst_port="nope")
        dead = Patch.from_dict(data)
        assert dead.cables == good.cables
        assert dead.load_warnings and not good.load_warnings

    def test_two_dead_cables_give_two_lines(self):
        data = _base()
        ids = data.pop("_ids")
        data["cables"][0] = dict(data["cables"][0], dst_port="frequency_cv")
        data["cables"][1] = dict(data["cables"][1], dst_module_id=4242)
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 2
        assert len(patch.load_warnings) == 2

    def test_every_warning_is_plain_ascii(self):
        for changes in (
            {"dst_port": "frequency_cv"}, {"src_port": "sub"},
            {"dst_module_id": 9999}, {"src_port": "rate_cv"},
        ):
            data, _ids = _with_first_cable(**changes)
            patch = Patch.from_dict(data)
            for line in patch.load_warnings:
                assert line.isascii(), line


# ----- what a VALID patch does (i.e. nothing new) ---------------------------


class TestCleanPatches:
    def test_a_clean_patch_reports_nothing_and_keeps_every_cable(self):
        data = _base()
        data.pop("_ids")
        patch = Patch.from_dict(data)
        assert patch.load_warnings == []
        assert len(patch.cables) == len(data["cables"]) == 4

    def test_a_patch_built_in_memory_starts_with_no_warnings(self):
        assert Patch().load_warnings == []

    def test_warnings_do_not_travel_in_the_json(self):
        data, _ids = _with_first_cable(dst_port="nope")
        patch = Patch.from_dict(data)
        assert patch.load_warnings
        assert "load_warnings" not in patch.to_dict()
        # and a re-load of what we just wrote is clean -- the dead cable
        # is gone for good once the patch is saved again
        again = Patch.from_dict(patch.to_dict())
        assert again.load_warnings == []

    def test_warnings_are_not_part_of_patch_identity(self):
        """A dropped cable must not make the patch differ from the same
        patch loaded from a tidied file -- ``load_warnings`` describes
        this LOAD, not the instrument, so it is out of ``==`` and
        ``repr`` the way ``source_path`` is out of the JSON."""
        import dataclasses

        compared = {f.name for f in dataclasses.fields(Patch) if f.compare}
        assert "load_warnings" not in compared
        assert "cables" in compared and "modules" in compared
        data, _ids = _with_first_cable(dst_port="nope")
        dead = Patch.from_dict(data)
        tidy = Patch.from_dict(_without_first_cable())
        assert dead.to_dict() == tidy.to_dict()
        assert "load_warnings" not in repr(dead)

    def test_a_duplicate_destination_is_left_alone(self):
        """``connect``'s one-cable-per-jack rule is deliberately NOT a
        loader check: which of two claimants survives is a decision that
        would change how an existing patch plays, so the loader keeps
        both and lets the editor sort it out."""
        data = _base()
        ids = data.pop("_ids")
        data["cables"].append({
            "src_module_id": ids["const"], "src_port": "out",
            "dst_module_id": ids["osc"], "dst_port": "freq_cv",
        })
        patch = Patch.from_dict(data)
        assert len(patch.cables) == 5
        assert patch.load_warnings == []

    @pytest.mark.parametrize(
        "path", sorted(EXAMPLES_DIR.glob("*.json")), ids=lambda p: p.stem
    )
    def test_no_shipped_example_loses_a_cable(self, path):
        """The other side of ``test_examples.py``'s port tripwire: not
        only are the shipped examples' cables real, the loader agrees --
        it drops none of them and reports nothing. Guards against an
        over-eager validator quietly thinning a working patch."""
        patch = load_patch(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert patch.load_warnings == []
        assert len(patch.cables) == len(raw.get("cables", []))


# ----- it still plays -------------------------------------------------------


class TestStillRenders:
    def test_a_dead_cable_patch_compiles_and_renders(self):
        data, _ids = _with_first_cable(dst_port="frequency_cv")
        out = _render(data)
        assert np.all(np.isfinite(out))
        assert np.abs(out).max() > 0.01, "the rest of the patch went quiet"

    def test_it_renders_EXACTLY_as_if_the_cable_were_never_written(self):
        data, _ids = _with_first_cable(dst_port="frequency_cv")
        assert np.array_equal(_render(data), _render(_without_first_cable()))

    def test_and_the_LIVE_cable_would_have_sounded_different(self):
        """Self-test for the test above: if the vibrato made no audible
        difference either way, 'silent where the dead cable was' would
        be true of a broken validator too."""
        alive = _render(_base())
        assert not np.array_equal(alive, _render(_without_first_cable()))


# ----- the status line ------------------------------------------------------

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.ui.app as app_mod  # noqa: E402


class _Ctx:
    """Stands in for ``dpg.node(...)`` & friends, yielding a distinct id."""

    def __init__(self, ident):
        self.ident = ident

    def __enter__(self):
        return self.ident

    def __exit__(self, *exc):
        return False


def _mock_dpg():
    dpg = mock.MagicMock()
    ids = itertools.count(2000)
    for ctx in ("node", "node_attribute", "theme", "theme_component", "group",
                "tooltip", "node_editor", "window", "menu_bar", "menu",
                "child_window", "table", "table_row", "drawlist", "plot"):
        getattr(dpg, ctx).side_effect = lambda *a, **k: _Ctx(next(ids))
    for factory in ("add_node_link", "add_text", "add_button",
                    "add_slider_float", "add_drag_float", "add_combo",
                    "add_checkbox", "add_drag_int", "add_progress_bar",
                    "add_input_text", "add_spacer"):
        getattr(dpg, factory).side_effect = lambda *a, **k: next(ids)
    dpg.does_item_exist.return_value = True
    return dpg


def _status(dpg) -> str:
    vals = [
        c.args for c in dpg.set_value.call_args_list
        if c.args and c.args[0] == app_mod.STATUS_TEXT_TAG
    ]
    return vals[-1][1] if vals else ""


def _open(monkeypatch, tmp_path, data, name="patch.json"):
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    dpg = _mock_dpg()
    monkeypatch.setattr(app_mod, "dpg", dpg)
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    path = tmp_path / name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    app._load_patch_from(str(path))
    return app, dpg


class TestStatusLine:
    def test_a_clean_load_says_exactly_what_it_always_said(
        self, monkeypatch, tmp_path, capsys
    ):
        data = _base()
        data.pop("_ids")
        _app, dpg = _open(monkeypatch, tmp_path, data, "clean.json")
        assert _status(dpg) == "   Loaded: clean.json"
        assert "dead cable" not in capsys.readouterr().out

    def test_a_dead_cable_load_puts_the_count_on_the_status_line(
        self, monkeypatch, tmp_path, capsys
    ):
        data, _ids = _with_first_cable(dst_port="frequency_cv")
        app, dpg = _open(monkeypatch, tmp_path, data, "broken.json")
        line = _status(dpg)
        assert line.isascii(), line
        assert "broken.json" in line
        assert "1 dead cable dropped" in line
        assert "see console" in line
        assert len(app.patch.cables) == 3
        # one status line; the console carries the detail
        assert "\n" not in line
        out = capsys.readouterr().out
        assert "broken.json: dropped 1 dead cable(s) on load:" in out
        assert "frequency_cv" in out and "oscillator" in out

    def test_two_dead_cables_pluralise(self, monkeypatch, tmp_path, capsys):
        data = _base()
        ids = data.pop("_ids")
        data["cables"][0] = dict(data["cables"][0], dst_port="frequency_cv")
        data["cables"][1] = dict(data["cables"][1], dst_module_id=4242)
        _app, dpg = _open(monkeypatch, tmp_path, data, "worse.json")
        assert "2 dead cables dropped" in _status(dpg)
        assert len(capsys.readouterr().out.splitlines()) >= 3

    def test_the_editor_draws_only_the_surviving_cables(
        self, monkeypatch, tmp_path
    ):
        data, _ids = _with_first_cable(dst_port="frequency_cv")
        app, _dpg = _open(monkeypatch, tmp_path, data, "broken.json")
        assert len(app._link_to_cable) == 3
        assert set(app._link_to_cable.values()) == set(app.patch.cables)


# ----- the helper on its own ------------------------------------------------


class TestCableProblem:
    def test_a_good_cable_has_no_problem(self):
        p = Patch()
        osc = p.add_module("oscillator")
        spk = p.add_module("speaker_output")
        assert p.cable_problem(Cable(osc.id, "out", spk.id, "in")) is None

    def test_the_message_names_both_ends(self):
        p = Patch()
        osc = p.add_module("oscillator")
        spk = p.add_module("speaker_output")
        line = p.cable_problem(Cable(osc.id, "out", spk.id, "input"))
        assert line is not None
        assert f"oscillator#{osc.id}.out" in line
        assert f"speaker_output#{spk.id}.input" in line
