"""CVRecorder — a modulation looper: record a CV or a knob gesture, loop it.

Nothing else in the rack captures *performance*. Every modulator here is
generated — an LFO's shape, an envelope's curve, a sequencer's steps, a
drift's wander — and none of them is *you turning a knob*. This module
records a control voltage for a fixed loop length, then plays it back
forever: a filter sweep you performed once becomes the filter's motion
for the rest of the piece; a hand-drawn pan becomes a pattern; an
incoming LFO caught mid-bar becomes a rhythmic modulation that repeats in
time with the song.

It is the fixed-length kind of looper (the "N bars" kind): ``length`` is
a setting, ``rec`` writes into it, the loop plays from the moment it
exists, ``clear`` wipes it.

  * ``in`` is the signal to record. **Leave it unpatched and the module's
    own ``value`` knob is the input** — a hand on the slider while ``rec``
    is high is the recorded gesture. The knob is read once per block and
    ramped linearly across it from the previous block's value, so a
    recorded turn has no steps in it.
  * ``rec`` (gate): record while high. The first rising edge is when the
    loop starts to exist — the position ramp starts at 0 then and runs
    forever until ``clear``. What happens to the buffer while recording
    depends on ``mode``: ``replace`` overwrites (a punch-in: only the
    stretch where ``rec`` was high changes), ``overdub`` (the default)
    adds the input to what is there, the old layer first scaled by
    ``feedback`` (1 = layers pile up; 0.5 = every pass halves what was
    there, so the loop keeps evolving instead of accumulating).
  * ``out`` is the loop — and while recording, the value being written
    (in overdub, old + new), so what you hear is what you keep.
  * ``pos`` is the loop position as a 0..1 ramp — a free sync signal for
    anything that wants to know where the bar is.
  * ``clear`` (gate): a rising edge wipes the buffer and rewinds; the
    position then holds at 0 until the next ``rec``, so the next take
    starts at the top.

**Clocked.** Patch a clock and ``length`` is read as **clock ticks**
instead of seconds (16 sixteenths = a bar); the loop's sample length is
fixed from the clock's period (measured from its last two rising edges)
at the moment the loop is created. Two things then keep it musical:
``rec`` edges — on and off — are honoured on the *next* tick (quantised
punch-in: press slightly late and the loop still starts on the bar), and
the position hard-syncs to 0 on every ``length``-th tick counted from the
loop's start, so the loop never drifts from the transport. A tempo change
after the loop exists crops or wraps the fixed buffer until the next sync
— documented, not fought; ``clear`` and re-record to re-measure.

Mono (a polyphonic ``in`` collapses to the house sum). Renders are
block-size independent whenever ``in`` is patched — every event is an
integer sample position; the knob path is block-rate by nature.

Ports:
  * ``in`` (cv in): the signal to record. Unpatched → the ``value`` knob.
  * ``clock`` (gate in): optional — ``length`` in ticks, quantised rec,
    hard sync at the loop boundary.
  * ``rec`` (gate in): record while high.
  * ``clear`` (gate in): rising edge wipes the loop and rewinds.
  * ``out`` (cv out): the loop (while recording, what is being written).
  * ``pos`` (cv out): loop position, 0..1.

Params:
  * ``length``: loop length — seconds, or clock ticks while ``clock`` is
    patched. 0.05..60. Default 4.
  * ``mode``: ``replace`` / ``overdub``. Default ``overdub``.
  * ``feedback``: overdub — what the old layer is scaled by before the
    new input is added, 0..1. Default 1.
  * ``value``: the gesture knob, −1..1, the input when ``in`` is
    unpatched. Default 0.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

CV_RECORDER_MODES = ("replace", "overdub")


@register_module_type
class CVRecorder(Module):
    """Fixed-length CV looper: record while ``rec`` is high, loop forever.

    Parameters:
        length: Loop length in seconds (clock ticks while ``clock`` is
            patched), 0.05..60. Default 4.0.
        mode: ``replace`` (punch-in overwrites) or ``overdub`` (adds to the
            existing layer scaled by ``feedback``). Default ``overdub``.
        feedback: Overdub scaling of the existing layer, 0..1. Default 1.0.
        value: The gesture knob — the input while ``in`` is unpatched,
            −1..1. Default 0.0.

    Ports:
        in (in, cv): the signal to record; unpatched → ``value``.
        clock (in, gate): optional tempo lock (ticks, quantised rec, sync).
        rec (in, gate): record while high.
        clear (in, gate): rising edge wipes and rewinds.
        out (out, cv): the loop.
        pos (out, cv): loop position 0..1.
    """

    TYPE = "cv_recorder"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {
        "length": 4.0,
        "mode": "overdub",
        "feedback": 1.0,
        "value": 0.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "cv"),
        Port("clock", "in", "gate"),
        Port("rec", "in", "gate"),
        Port("clear", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "cv"),
        Port("pos", "out", "cv"),
    ]
