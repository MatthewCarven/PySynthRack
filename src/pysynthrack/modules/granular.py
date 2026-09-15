"""Granular — a grain-cloud texture engine over a live-captured buffer.

The rack's other grain module, the [pitch_shifter], hides its grains: it
picks their positions for you (WSOLA) so the joins are seamless and the
result sounds like the input, transposed. This one **exposes** them. The
input is captured continuously into a ring buffer ``buffer`` seconds long,
and a scheduler fires **grains** — short windowed reads of that history,
each played back at its own rate — ``density`` times a second, ``size``
milliseconds long, from ``position`` seconds ago, transposed by
``pitch``. The output is the overlap-add of every grain in flight.

That is the whole machine, and the sounds come from where you park the
knobs:

  * the **neutral**: ``hann`` grains at 50% overlap (``density`` ×
    ``size`` = 2, the defaults) sum to exactly one, so at ``pitch`` 0 the
    output is the input, delayed by ``position`` — a grain delay you can
    then bend;
  * ``pitch`` up or down is a granular pitch shifter of the classic,
    un-hidden kind — periodic, a little metallic at high ``density``,
    smeared at low, and *that* is the sound people patch these for;
  * ``density`` down until the grains no longer touch is a stutter / a
    tremolo at the grain rate; ``size`` short is a buzz at that rate;
  * ``position`` up reads further into the past — a delay line whose
    delay you can smear (slice 3 puts a CV on it);
  * ``window`` shapes each grain: ``hann`` (smooth), ``triangle``
    (brighter joins), ``expo`` (a percussive attack-decay grain — the
    classic "expodec" — that turns a pad into a rain of taps).

**Slice 1 (this):** capture + a synchronous, deterministic grain stream,
mono. Slice 2 adds the spray scheduler (``spray_pos`` / ``spray_pitch``
/ ``seed``, jittered onsets) and stereo (``width``, ``out_l``/``out_r``);
slice 3 adds ``freeze`` and ``position_cv``.

Rules of the road, all of them so a patch is exactly reproducible:

  * every grain's rate, length, window and level are fixed at the moment
    it fires — turning a knob affects the *next* grain, never one in
    flight (no zipper, no clicks, and the render is block-size
    independent to the bit);
  * a grain reading faster than real time (``pitch`` > 0) would overtake
    the write head, so ``position`` is floored at the head start it
    needs (``(rate − 1) × size``); if the buffer is shorter than that,
    the grain is shortened instead. Reading slower than real time needs
    no such care;
  * grains are level-normalized by their expected overlap (``density``
    × ``size`` × the window's mean), so a dense cloud is not a loud one;
    sparse grains play at their natural level.

Ports:
  * ``in`` (audio): the signal captured into the buffer. A ``(V, F)``
    voice source is summed — one buffer. Unpatched → silence.
  * ``out`` (audio): the cloud, blended with the dry input by ``mix``.

Params:
  * ``buffer``: seconds of history the grains can read from, 0.5 … 10.
    Default 2. Resizing keeps the history it can and drops the grains in
    flight (one grain's worth of dropout).
  * ``density``: grains per second, 0.5 … 100. Default 25.
  * ``size``: grain length in ms, 10 … 500. Default 80. (Rounded to an
    even sample count so 50% overlap is exact.)
  * ``pitch``: transposition in semitones, −24 … +24. Default 0.
  * ``position``: how far back the grains read, 0 (now) … 1 (``buffer``
    seconds ago). Default 0.
  * ``window``: ``hann`` | ``triangle`` | ``expo``. Default ``hann``.
  * ``mix``: dry/wet, 0 … 1. Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Valid ``window`` values; the UI combo renders these in this order.
GRANULAR_WINDOWS = ("hann", "triangle", "expo")


@register_module_type
class Granular(Module):
    """Grain cloud over a live-captured ring buffer (slice 1: mono, synchronous).

    Parameters:
        buffer: History length, seconds, 0.5..10. Default 2.
        density: Grains per second, 0.5..100. Default 25.
        size: Grain length, ms, 10..500. Default 80.
        pitch: Transposition, semitones, -24..24. Default 0.
        position: Read point, 0 (now)..1 (``buffer`` s ago). Default 0.
        window: ``"hann"``, ``"triangle"`` or ``"expo"``. Default ``"hann"``.
        mix: Dry/wet, 0..1. Default 1.

    Ports:
        in (in, audio): the signal (voice sources are summed).
        out (out, audio): the cloud, blended by ``mix``.
    """

    TYPE = "granular"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "buffer": 2.0,
        "density": 25.0,
        "size": 80.0,
        "pitch": 0.0,
        "position": 0.0,
        "window": "hann",
        "mix": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
