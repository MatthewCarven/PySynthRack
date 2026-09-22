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

**Transport** (the 2026-09-20 love pass): the head has a stop, a
direction and a speed, so the loop is an instrument and not just a tape.

  * ``play`` (gate): unpatched, the loop plays. Patched, the head runs
    while it is high and **stops** while it is low — ``out`` holds the
    slot under the head and ``pos`` holds with it (a frozen modulation:
    pause the wobble on the downbeat), and when ``play`` rises the loop
    resumes from that same slot. A stopped head **writes nothing**:
    ``rec`` is still honoured as a state (the loop is created if it does
    not exist, the recorder is armed), and the take begins the moment
    ``play`` rises. The alternative — a stopped head writing the same
    slot over and over — would in overdub pile the whole gesture into
    one sample, a spike rather than a punch-in. A clocked sync tick
    still snaps the stopped head to 0: the transport wins, so ``play``
    gated from the same clock resumes at the top of the bar.
  * ``reverse`` (checkbox, OR the ``reverse`` gate — the freeze /
    granular precedent): the head runs backwards, wrapping from 0 to
    the loop's end. A flip mid-loop keeps the head where it is and
    turns around — no jump. Recording in reverse writes backwards too:
    the take plays as performed while reverse stays on, and comes back
    **time-reversed** once it is released (the tape ran backwards
    under the record head). Clocked, the hard sync still snaps the head
    to 0 on the sync tick, whichever way it is running.
  * ``speed`` (``0.5x`` / ``1x`` / ``2x``): how fast the *playback* head
    moves. At ``2x`` the loop plays twice per length, at ``0.5x`` once
    per two lengths, read with linear interpolation between slots (a
    recorded ramp stays a ramp). **Recording always runs at 1x** —
    while ``rec`` is high the head advances one sample per sample
    whatever ``speed`` says, so the take is real time; ``speed`` resumes
    when ``rec`` falls. ("Record at 1x, play at any.") A ``rec`` edge
    at ``0.5x`` lands the head on the slot it is over first. A speed
    change mid-loop keeps the head where it is. The head is an integer
    count of half-samples from the last snap, so every speed is
    bit-exact across block sizes.

**The one-shot and the rate jack** (the 2026-09-22 love pass).

  * ``speed_cv`` (cv) multiplies the ``speed`` combo continuously —
    except that it cannot be continuous and stay bit-exact, so it is
    **quantised to the half-sample grid**: the rate is
    ``floor(base x 2 ** (speed_cv_depth x mean cv) + 0.5)`` half-steps
    per sample, clamped to **1..8** — the eight reachable rates are
    ``0.5x 1x 1.5x 2x 2.5x 3x 3.5x 4x``. An integer half-step is what
    makes the head an integer count from its last snap; a fractional
    rate would need a float phase, and a float phase is not the same
    number at block 50 as at block 250. The jack is read as a float64
    block mean (the house depth convention), the exponent clipped ±4
    before the power, and a non-finite CV reads as 0. A rate change
    keeps the head where it is, so a sweeping CV never jumps the loop
    — it only steps further per sample. The rate never reaches 0: the
    stop is what ``play`` is for.
  * ``play_mode`` (``gate`` / ``one_shot``). At ``gate`` (the default)
    ``play`` is the level described above. At ``one_shot`` the level is
    ignored and a **rising edge fires exactly one lap** from position 0
    (from the last slot when reverse is on), after which the head stops
    and **holds the last slot it played** — so a recorded ramp fires
    once and holds at its top, a one-shot envelope you performed. A new
    edge mid-lap restarts the lap from 0. A lap is one loop length of
    head *travel*, so at ``2x`` it takes half the time. A clocked sync
    tick snaps the head to 0 mid-lap like any other (the transport
    wins) and the lap's remaining travel is untouched. Because a
    stopped head writes nothing, ``rec`` would be impossible between
    shots — so at ``one_shot`` **recording runs the head** (at 1x, from
    where it is, as if ``play`` were high) and it stops again when
    ``rec`` falls. With ``play`` unpatched there is no edge to fire, so
    nothing plays: patch a trigger.

Mono (a polyphonic ``in`` collapses to the house sum). Renders are
block-size independent whenever ``in`` is patched — every event is an
integer sample position; the knob path is block-rate by nature.

Ports:
  * ``in`` (cv in): the signal to record. Unpatched → the ``value`` knob.
  * ``clock`` (gate in): optional — ``length`` in ticks, quantised rec,
    hard sync at the loop boundary.
  * ``rec`` (gate in): record while high.
  * ``clear`` (gate in): rising edge wipes the loop and rewinds.
  * ``play`` (gate in): optional — the head runs while high, stops
    while low (``out`` and ``pos`` hold). Unpatched = playing.
  * ``reverse`` (gate in): optional — high runs the head backwards
    (ORed with the ``reverse`` checkbox).
  * ``speed_cv`` (cv in): optional — multiplies ``speed`` by
    ``2 ** (speed_cv_depth * mean cv)``, quantised to the half-sample
    grid (0.5x…4x in 0.25x steps).
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
  * ``reverse``: run the head backwards (OR the ``reverse`` gate).
    Default off.
  * ``speed``: playback head rate, ``0.5x`` / ``1x`` / ``2x``; recording
    always runs at ``1x``. Default ``1x``.
  * ``speed_cv_depth``: rate doublings per unit of ``speed_cv``, −4..4.
    Default 1.
  * ``play_mode``: ``gate`` (``play`` is a level) or ``one_shot`` (a
    rising edge fires one lap). Default ``gate``.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

CV_RECORDER_MODES = ("replace", "overdub")
#: Playback head rates. Recording always runs at 1x whatever this says.
CV_RECORDER_SPEEDS = ("0.5x", "1x", "2x")
#: What the ``play`` jack means: a level, or a one-lap trigger. NOT
#: named ``mode`` -- that word hits the shared combo branch in the UI.
CV_RECORDER_PLAY_MODES = ("gate", "one_shot")


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
        reverse: Run the head backwards (OR the ``reverse`` gate).
            Default False.
        speed: Playback head rate, ``0.5x`` / ``1x`` / ``2x``; recording
            always runs at 1x. Default ``1x``.
        speed_cv_depth: Rate doublings per unit of ``speed_cv``, −4..4;
            the product is quantised to the half-sample grid (0.5x…4x
            in 0.25x steps). Default 1.0.
        play_mode: ``gate`` (``play`` is a level) or ``one_shot`` (a
            rising edge on ``play`` fires exactly one lap, then the
            head holds). Default ``gate``.

    Ports:
        in (in, cv): the signal to record; unpatched → ``value``.
        clock (in, gate): optional tempo lock (ticks, quantised rec, sync).
        rec (in, gate): record while high.
        clear (in, gate): rising edge wipes and rewinds.
        play (in, gate): optional — runs while high, stops while low.
        reverse (in, gate): optional — high runs the head backwards.
        speed_cv (in, cv): optional — multiplies the head rate
            (quantised to the half-sample grid).
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
        "reverse": False,
        "speed": "1x",
        "speed_cv_depth": 1.0,
        "play_mode": "gate",
    }
    INPUT_PORTS = [
        Port("in", "in", "cv"),
        Port("clock", "in", "gate"),
        Port("rec", "in", "gate"),
        Port("clear", "in", "gate"),
        Port("play", "in", "gate"),
        Port("reverse", "in", "gate"),
        Port("speed_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "cv"),
        Port("pos", "out", "cv"),
    ]
