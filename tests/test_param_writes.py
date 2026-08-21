"""Every UI param write must reach the *model*, compiled backend or not.

The bug these pin: `App._on_param_changed` (and every hand-written param
callback beside it) used to call `backend.set_param` alone, and
`NumpyBackend.set_param` returns early while `self._patch is None` — which
it is until **Start audio** compiles a patch in. So on a freshly opened
patch, every knob, slider, combo, fader and transport button edit made
before pressing Start was silently discarded, and a save at that moment
wrote the OLD values back to disk.

The fix routes all of them through `App._set_module_param`: model first,
backend notified after. Once a patch *is* compiled the two orders are
identical, so the "compiled" tests below are the no-regression half and the
"before Start" tests are the bug half.

Driven with DearPyGui mocked out (`app.dpg` -> MagicMock), the
`test_key_trigger_ui.py` pattern. Skips cleanly where DPG can't import.
"""
from __future__ import annotations

from unittest import mock

import pytest

pytest.importorskip("dearpygui.dearpygui")

import pysynthrack.modules  # noqa: F401  (registers module types)
import pysynthrack.ui.app as app_mod
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.io_patch.patch_io import load_patch, save_patch


def _make_app(monkeypatch, compiled=False):
    """An App with dpg mocked out.

    ``compiled=False`` is the state the GUI is actually in from launch
    until the first **Start audio** — the backend holds no patch.
    """
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=48000, block_size=512)
    app.patch = Patch()
    if compiled:
        app.backend.compile(app.patch)
    return app


# ----- the generic callback --------------------------------------------------

def test_edit_before_start_reaches_the_model(monkeypatch):
    app = _make_app(monkeypatch)
    osc = app.patch.add_module("oscillator")
    assert app.backend._patch is None            # nothing compiled yet
    app._on_param_changed(None, 220.0, (osc.id, "freq"))
    assert osc.params["freq"] == 220.0


def test_edit_after_start_still_reaches_the_model(monkeypatch):
    """The no-regression half: the compiled path is unchanged."""
    app = _make_app(monkeypatch)
    osc = app.patch.add_module("oscillator")
    app.backend.compile(app.patch)
    app._on_param_changed(None, 330.0, (osc.id, "freq"))
    assert osc.params["freq"] == 330.0


def test_edit_before_start_survives_a_save(monkeypatch, tmp_path):
    """The headline data loss: tweak a knob on a freshly opened patch, save,
    and the file used to come back with the value you started with."""
    app = _make_app(monkeypatch)
    osc = app.patch.add_module("oscillator")
    app._on_param_changed(None, 111.0, (osc.id, "freq"))
    path = tmp_path / "before_start.json"
    save_patch(app.patch, path)
    assert load_patch(path).get(osc.id).params["freq"] == 111.0


def test_a_string_param_lands_too(monkeypatch):
    app = _make_app(monkeypatch)
    osc = app.patch.add_module("oscillator")
    app._on_param_changed(None, "square", (osc.id, "waveform"))
    assert osc.params["waveform"] == "square"


def test_unknown_param_is_reported_not_raised(monkeypatch):
    app = _make_app(monkeypatch)
    osc = app.patch.add_module("oscillator")
    app._on_param_changed(None, 1.0, (osc.id, "not_a_param"))   # must not raise
    assert "not_a_param" not in osc.params


def test_write_to_a_deleted_module_is_survivable(monkeypatch):
    app = _make_app(monkeypatch)
    osc = app.patch.add_module("oscillator")
    app.patch.remove_module(osc.id)
    app._on_param_changed(None, 1.0, (osc.id, "freq"))          # must not raise


def test_device_branch_still_clears_the_sink_baseline(monkeypatch):
    """The one piece of extra bookkeeping hanging off _on_param_changed."""
    app = _make_app(monkeypatch)
    sink = app.patch.add_module("buffered_specific_speaker_output")
    app._sink_buffer_last[sink.id] = (1, 2)
    app._sink_buffer_flash[sink.id] = 99.0
    app._on_param_changed(None, "", (sink.id, "device"))
    assert sink.id not in app._sink_buffer_last
    assert sink.id not in app._sink_buffer_flash


# ----- the hand-written callbacks -------------------------------------------

def test_fader_pitch_before_start(monkeypatch):
    app = _make_app(monkeypatch)
    seq = app.patch.add_module("fader_seq")
    app._on_fader_pitch(None, 7, (seq.id, 2))
    assert seq.params["step2_pitch"] == 7.0
    assert isinstance(seq.params["step2_pitch"], float)   # the shared JSON shape


def test_fm_ratio_before_start_is_snapped(monkeypatch):
    app = _make_app(monkeypatch)
    fm = app.patch.add_module("fm_op")
    app._on_fm_ratio_changed(None, "2.0", (fm.id, "ratio"))
    assert fm.params["ratio"] == pytest.approx(2.0)


def test_sink_buffer_size_before_start_is_coerced(monkeypatch):
    app = _make_app(monkeypatch)
    sink = app.patch.add_module("buffered_specific_speaker_output")
    app._on_buffer_size_changed(None, "4096", (sink.id, "buffer_size"))
    assert sink.params["buffer_size"] == 4096
    assert isinstance(sink.params["buffer_size"], int)


def test_file_transport_before_start(monkeypatch):
    app = _make_app(monkeypatch)
    fp = app.patch.add_module("file_player")
    app._on_file_transport(None, None, (fp.id, "play"))
    assert fp.params["playing"] is True
    app._on_file_transport(None, None, (fp.id, "stop"))
    assert fp.params["playing"] is False


def test_velocity_multiplier_before_start(monkeypatch):
    app = _make_app(monkeypatch)
    midi = app.patch.add_module("midi_input")
    app._on_vel_mult_changed(None, 1.25, (midi.id, 60))
    assert app.patch.get(midi.id).params["velocity_curve"][60] == pytest.approx(1.25)


def test_no_ui_callback_writes_the_backend_directly(monkeypatch):
    """Tripwire: `_set_module_param` is the single param-write door. If a new
    callback goes straight to `backend.set_param`, it reintroduces the bug —
    this counts the call sites in the source instead of waiting for a bug
    report."""
    import inspect

    source = inspect.getsource(app_mod)
    assert source.count("self.backend.set_param(") == 1, (
        "param writes must go through App._set_module_param, which is the "
        "only place allowed to call backend.set_param"
    )
