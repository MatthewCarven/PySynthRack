"""Scope — an oscilloscope tap you patch inline anywhere.

A pass-through waveform display: audio (or a CV) goes in, the *identical*
signal comes out (bit-exact, the Meter precedent), and the node draws the
last few divisions of it every GUI frame. The debugging and demo force
multiplier — every "what is this module actually doing to the wave?"
question becomes a glance.

Traces. ``in`` is the main trace (with ``out`` its untouched pass-through);
``in_r`` is a second trace for ``dual``/``xy`` modes (``out_r`` passes it
through). The ``cv`` jack is the *fallback* main trace: patch it alone —
an LFO, an envelope, a quantizer output — and the scope draws that
instead, no ``cv_to_audio`` bridge needed (``in`` wins when both are
patched; the cv jack has no pass-through — CV fans out from its source).

Display. A classic 10-division face: the window is ``time_div`` ms per
division × 10, each pixel column showing its slice's min/max so a
one-sample click can never hide between pixels. ``gain`` scales
vertically (±1 fills the face at 1.0). ``freeze`` holds the current
picture while the audio runs on.

Trigger. ``rising`` / ``falling`` align the sweep to the last ``level``
crossing that fits a full window, so periodic signals hold still;
``free`` shows the newest window regardless (envelopes, one-shots). A
patched ``trig`` gate overrides the level trigger: the sweep aligns to
its last rising edge instead (clock-locked displays).

Modes. ``mono`` draws the main trace; ``dual`` stacks ``in`` and
``in_r`` in one face; ``xy`` plots ``in`` (x) against ``in_r`` (y) — the
goniometer: a mono signal on both = a diagonal line, stereo width opens
it into a cloud, quadrature LFOs draw circles.

Voice-aware sources pass through shape-intact; the display sums voices
(what a downstream mono consumer hears). Numpy backend only; under pyo
the node is a silent stub like the other display taps.

Ports:
  * ``in`` (audio): main trace + pass-through.
  * ``in_r`` (audio): second trace (dual/xy) + pass-through.
  * ``cv`` (cv): fallback main trace when ``in`` is unpatched.
  * ``trig`` (gate): external trigger; overrides the level trigger.
  * ``out`` / ``out_r`` (audio): the untouched inputs.

Params:
  * ``time_div``: ms per horizontal division (×10 divisions), 1..500.
    Default 10 (a 100 ms window).
  * ``gain``: vertical scale, 0.1..10. Default 1.
  * ``trigger``: ``free`` | ``rising`` | ``falling``. Default ``rising``.
  * ``level``: trigger level, −1..1. Default 0.
  * ``freeze``: hold the current picture. Default off.
  * ``mode``: ``mono`` | ``dual`` | ``xy``. Default ``mono``.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

SCOPE_TRIGGER_MODES = ("free", "rising", "falling")
SCOPE_MODES = ("mono", "dual", "xy")


@register_module_type
class Scope(Module):
    """Oscilloscope pass-through tap (bit-exact through, min/max display).

    Parameters:
        time_div: ms per division (10 divisions across). 1..500, default 10.
        gain: Vertical scale, 0.1..10. Default 1.
        trigger: ``free`` | ``rising`` | ``falling``. Default ``rising``.
        level: Trigger level, −1..1. Default 0.
        freeze: Hold the current picture. Default False.
        mode: ``mono`` | ``dual`` | ``xy``. Default ``mono``.

    Ports:
        in (in, audio): main trace; passed through on ``out`` bit-exact.
        in_r (in, audio): second trace; passed through on ``out_r``.
        cv (in, cv): fallback main trace when ``in`` is unpatched.
        trig (in, gate): external trigger (overrides the level trigger).
        out (out, audio): untouched ``in``.
        out_r (out, audio): untouched ``in_r``.
    """

    TYPE = "scope"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {
        "time_div": 10.0,
        "gain": 1.0,
        "trigger": "rising",
        "level": 0.0,
        "freeze": False,
        "mode": "mono",
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("in_r", "in", "audio"),
        Port("cv", "in", "cv"),
        Port("trig", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
