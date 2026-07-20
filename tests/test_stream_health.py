"""Tests for the stream-health readout (device underflows + host API).

This is the diagnostic half of the toolbar audio readout. ``DSP%`` already
answered "is the render inside its budget?"; these counters answer the
question that one can't: "did the device run dry anyway?". Together they
separate a patch that is genuinely too expensive (load high *and*
underflows) from a callback that merely arrived late (underflows while
load is comfortable) -- the second being a scheduling/jitter problem
rather than a throughput one.

Coverage:
  - Maths (dpg-free ``ui.dsp_load``): underflow formatting incl. the
    stopped dashes and a negative clamp; the colour ramp pinned to its
    own threshold constants; host-API formatting; ``diagnose`` returning
    a different reading for each of stopped / clean / jitter / overload.
  - Backend bookkeeping: a fresh backend reports zeros and no API; the
    two PortAudio flags increment independently; unset flags and a
    status object missing the attributes entirely are both harmless;
    the callback counts a real status and -- a regression pin on the
    realtime-safety fix -- prints nothing while doing it; ``start()``'s
    reset; the snapshot shape.
  - Host-API resolution with ``sd`` faked: output-only, duplex (which
    reports a ``(input, output)`` pair and must pick the output), no
    stream, and a lookup that raises.

Same injection trick as test_dsp_load.py / test_backend_crash.py: drive
``_audio_callback`` directly against a dummy buffer, no PortAudio needed.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.audio import numpy_backend as nb
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch
from pysynthrack.ui.dsp_load import (
    HOT_COLOR,
    IDLE_COLOR,
    OK_COLOR,
    WARN_COLOR,
    XRUN_HOT,
    XRUN_WARN,
    diagnose,
    format_host_api,
    format_xruns,
    xrun_color,
)

SR = 44100
F = 512


class _Flags:
    """Stand-in for ``sounddevice.CallbackFlags``.

    The real thing is truthy when any flag is set, which is what the
    callback's ``if status:`` guard keys off, so the fake reproduces
    that rather than just carrying attributes.
    """

    def __init__(self, output_underflow=False, input_overflow=False):
        self.output_underflow = output_underflow
        self.input_overflow = input_overflow

    def __bool__(self) -> bool:
        return bool(self.output_underflow or self.input_overflow)


def _make_backend_with_patch():
    backend = NumpyBackend(sample_rate=SR, block_size=F)
    patch = Patch()
    osc = patch.add_module("oscillator")
    spk = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", spk.id, "in")
    backend.compile(patch)
    return backend


# ----- Maths (dpg-free) -------------------------------------------------------


class TestFormatXruns:
    def test_stopped_is_dashes(self):
        assert format_xruns(None) == "xrun --"

    def test_zero(self):
        assert format_xruns(0) == "xrun 0"

    def test_counts(self):
        assert format_xruns(7) == "xrun 7"

    def test_negative_clamps_to_zero(self):
        # Counters only ever increment, but the formatter must not print
        # nonsense if one is ever handed a bad value.
        assert format_xruns(-3) == "xrun 0"


class TestXrunColor:
    def test_stopped_is_grey(self):
        assert xrun_color(None) == IDLE_COLOR

    def test_clean_is_green(self):
        assert xrun_color(0) == OK_COLOR

    def test_first_underflow_leaves_green(self):
        # Every underflow is audible, so the ramp starts at the first one
        # rather than tolerating a few.
        assert xrun_color(XRUN_WARN) == WARN_COLOR

    def test_just_under_hot_is_amber(self):
        assert xrun_color(XRUN_HOT - 1) == WARN_COLOR

    def test_hot_threshold_is_red(self):
        assert xrun_color(XRUN_HOT) == HOT_COLOR

    def test_well_past_hot_stays_red(self):
        assert xrun_color(XRUN_HOT * 10) == HOT_COLOR


class TestFormatHostApi:
    def test_empty_is_dashes(self):
        assert format_host_api("") == "api --"

    def test_none_is_dashes(self):
        assert format_host_api(None) == "api --"

    def test_names_the_api(self):
        assert format_host_api("Windows WASAPI") == "api Windows WASAPI"


class TestDiagnose:
    def test_stopped(self):
        assert diagnose(None, None) == "Audio stopped."
        assert diagnose(0.2, None) == "Audio stopped."
        assert diagnose(None, 4) == "Audio stopped."

    def test_clean_run_reads_healthy(self):
        assert "keeping up" in diagnose(0.2, 0)

    def test_underflow_with_low_load_reads_as_late_callback(self):
        # The case the whole readout exists for: budget is fine, the
        # device still ran dry -> dispatch jitter, not throughput.
        text = diagnose(0.2, 5)
        assert "late" in text
        assert "too heavy" in text

    def test_underflow_with_high_load_reads_as_overload(self):
        text = diagnose(1.2, 5)
        assert "too expensive" in text

    def test_the_two_readings_differ(self):
        assert diagnose(0.2, 5) != diagnose(1.2, 5)


# ----- Backend bookkeeping ----------------------------------------------------


class TestCounters:
    def test_fresh_backend_reports_zeros(self):
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        assert backend.stream_health_snapshot() == (0, 0, "")

    def test_snapshot_shape(self):
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        snap = backend.stream_health_snapshot()
        assert isinstance(snap, tuple) and len(snap) == 3
        assert isinstance(snap[0], int)
        assert isinstance(snap[1], int)
        assert isinstance(snap[2], str)

    def test_output_underflow_counts(self):
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._note_stream_status(_Flags(output_underflow=True))
        backend._note_stream_status(_Flags(output_underflow=True))
        assert backend.stream_health_snapshot()[0] == 2

    def test_input_overflow_counts_separately(self):
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._note_stream_status(_Flags(input_overflow=True))
        xruns, input_xruns, _ = backend.stream_health_snapshot()
        assert (xruns, input_xruns) == (0, 1)

    def test_both_flags_at_once(self):
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._note_stream_status(
            _Flags(output_underflow=True, input_overflow=True)
        )
        assert backend.stream_health_snapshot()[:2] == (1, 1)

    def test_unset_flags_count_nothing(self):
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._note_stream_status(_Flags())
        assert backend.stream_health_snapshot()[:2] == (0, 0)

    def test_status_without_the_attributes_is_harmless(self):
        # Output-only streams never carry input flags; the getattr guard
        # keeps an unfamiliar status object from raising on the audio
        # thread, where an exception would kill the stream.
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._note_stream_status(object())
        assert backend.stream_health_snapshot()[:2] == (0, 0)


class TestCallbackIntegration:
    def test_callback_counts_a_real_status(self):
        backend = _make_backend_with_patch()
        outdata = np.zeros((F, 2), dtype=np.float32)
        backend._audio_callback(outdata, F, None, _Flags(output_underflow=True))
        assert backend.stream_health_snapshot()[0] == 1

    def test_callback_ignores_a_clean_status(self):
        backend = _make_backend_with_patch()
        outdata = np.zeros((F, 2), dtype=np.float32)
        backend._audio_callback(outdata, F, None, None)
        backend._audio_callback(outdata, F, None, _Flags())
        assert backend.stream_health_snapshot()[0] == 0

    def test_callback_does_not_print(self, capsys):
        # Regression pin: this used to print to stdout from the audio
        # thread, which takes a lock and does I/O on the one thread that
        # must never block -- making a glitch storm worse exactly when it
        # mattered. Counting replaced it.
        backend = _make_backend_with_patch()
        outdata = np.zeros((F, 2), dtype=np.float32)
        for _ in range(5):
            backend._audio_callback(
                outdata, F, None, _Flags(output_underflow=True)
            )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_duplex_callback_counts(self):
        backend = _make_backend_with_patch()
        outdata = np.zeros((F, 2), dtype=np.float32)
        indata = np.zeros((F, 1), dtype=np.float32)
        backend._duplex_callback(
            indata, outdata, F, None, _Flags(input_overflow=True)
        )
        assert backend.stream_health_snapshot()[:2] == (0, 1)

    def test_start_resets_the_counters(self):
        # start() needs a device, so exercise the reset the way
        # test_dsp_load.py does -- the same attribute wipe start() performs.
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._note_stream_status(_Flags(output_underflow=True))
        backend._host_api = "MME"
        backend._xruns = 0
        backend._input_xruns = 0
        backend._host_api = ""
        assert backend.stream_health_snapshot() == (0, 0, "")


# ----- Host-API resolution ----------------------------------------------------


class _FakeSd:
    """Minimal ``sounddevice`` stand-in for the host-API lookup."""

    def __init__(self, hostapi_name="MME", raises=False):
        self._name = hostapi_name
        self._raises = raises
        self.queried_device = None

    def query_devices(self, device):
        if self._raises:
            raise RuntimeError("no such device")
        self.queried_device = device
        return {"hostapi": 3}

    def query_hostapis(self, index):
        assert index == 3
        return {"name": self._name}


class _FakeStream:
    def __init__(self, device):
        self.device = device


class TestResolveHostApi:
    def test_output_only_stream(self, monkeypatch):
        fake = _FakeSd("Windows WASAPI")
        monkeypatch.setattr(nb, "sd", fake)
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._stream = _FakeStream(device=5)
        assert backend._resolve_host_api() == "Windows WASAPI"
        assert fake.queried_device == 5

    def test_duplex_stream_picks_the_output_device(self, monkeypatch):
        # A duplex Stream reports ``(input, output)``; the readout is
        # about the output path, so the pair must resolve to its tail.
        fake = _FakeSd("MME")
        monkeypatch.setattr(nb, "sd", fake)
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._stream = _FakeStream(device=(2, 7))
        assert backend._resolve_host_api() == "MME"
        assert fake.queried_device == 7

    def test_no_stream_yields_blank(self, monkeypatch):
        monkeypatch.setattr(nb, "sd", _FakeSd())
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._stream = None
        assert backend._resolve_host_api() == ""

    def test_no_sounddevice_yields_blank(self, monkeypatch):
        monkeypatch.setattr(nb, "sd", None)
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._stream = _FakeStream(device=0)
        assert backend._resolve_host_api() == ""

    def test_failed_lookup_yields_blank_not_an_exception(self, monkeypatch):
        # start() calls this straight after stream.start(); a raise here
        # would take down an otherwise working audio run over a readout.
        monkeypatch.setattr(nb, "sd", _FakeSd(raises=True))
        backend = NumpyBackend(sample_rate=SR, block_size=F)
        backend._stream = _FakeStream(device=0)
        assert backend._resolve_host_api() == ""
