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

**The sprays (slice 2)** are what turn a grain *stream* into a grain
*cloud*. Each is a random amount added per grain, drawn from a stream
seeded by ``seed`` and keyed by the grain's index, so the cloud is
exactly reproducible — same seed, same input, same cloud, whatever the
block size:

  * ``spray_time``: onset jitter. Each interval between grains is the
    nominal hop scaled by a random factor in ``1 ± spray_time`` — 0 is
    the synchronous stream (with its pitched sideband at ``density``
    Hz), 1 is fully asynchronous (intervals from 0 to two hops, mean
    unchanged). Even 0.3 smears the sideband into a haze;
  * ``spray_pos``: read-point scatter, in ``position`` units (a fraction
    of ``buffer``), symmetric about ``position`` and clamped to the
    buffer. Smears *time*: the last few hundred ms of a pluck become a
    wash;
  * ``spray_pitch``: transposition scatter in cents, symmetric about
    ``pitch``. 10–30 ct is a chorus; 1200 is an octave cloud;
  * ``width``: per-grain stereo scatter on ``out_l`` / ``out_r``. Each
    grain is panned to a random spot within ``±width``; at 0 both
    channels are ``out``.

**Freeze and the scrub (slice 3).** ``freeze`` — the param switch, or a
high ``freeze`` gate, either one — stops the buffer *recording* while
the grains keep *reading*, so whatever was in the last ``buffer``
seconds is held, and ``position`` becomes a scrub across it: 0 is the
last sample captured, 1 the oldest. The buffer's timeline is
*captured* time, like a tape: release the freeze and recording resumes
from where it stopped, seamlessly, with no hole. ``position_cv`` (with
``position_cv_depth`` in fractions of the buffer per CV unit) moves the
read point — read once per grain at the grain's onset, so a sequencer
into it re-cuts a frozen phrase at every step and an LFO into it scans
the held buffer. Grains that were reading right at the head when a
freeze lands hold their last sample for the rest of their window;
grains further back are untouched.

**Slice 1:** capture + a synchronous, deterministic grain stream, mono.
**Slice 2:** the sprays, ``seed``, and stereo. **Slice 3 (this):**
``freeze`` and ``position_cv``. The module is complete against its
spec.

Rules of the road, all of them so a patch is exactly reproducible:

  * every grain's onset, rate, read point, length, window, level and pan
    are fixed at the moment it fires — turning a knob affects the *next*
    grain, never one in flight (no zipper, no clicks, and the render is
    block-size independent to the bit);
  * a grain reading faster than real time (``pitch`` > 0) would overtake
    the write head, so its read point is floored at the head start it
    needs (``(rate − 1) × size`` live; ``rate × size`` while frozen,
    since the head is not moving); if the buffer is shorter than that,
    the grain is shortened instead. Reading slower than real time needs
    no such care;
  * grains are level-normalized by their expected overlap (``density``
    × ``size`` × the window's mean), so a dense cloud is not a loud one;
    sparse grains play at their natural level;
  * the pan law is constant-peak (a centred grain is at unity in both
    channels, a hard-panned one at unity in one and zero in the other),
    so ``width`` 0 leaves ``out_l`` and ``out_r`` bit-identical to
    ``out``, and ``out`` always hears every grain at unity.

Ports:
  * ``in`` (audio): the signal captured into the buffer. A ``(V, F)``
    voice source is summed — one buffer. Unpatched → silence.
  * ``position_cv`` (cv): added to ``position`` × ``position_cv_depth``,
    read at each grain's onset sample and latched for that grain. A
    ``(V, F)`` source is averaged.
  * ``freeze`` (gate): high holds the buffer (ORed with the ``freeze``
    param). Per-sample: the freeze lands on the exact sample the gate
    rises, so the held content does not depend on the block size.
  * ``out`` (audio): the cloud, every grain at unity, blended with the
    dry input by ``mix``.
  * ``out_l`` / ``out_r`` (audio): the cloud with each grain panned by
    its own draw within ``±width``; the dry stays centred.

Params:
  * ``buffer``: seconds of history the grains can read from, 0.5 … 10.
    Default 2. Resizing keeps the history it can and drops the grains in
    flight (one grain's worth of dropout).
  * ``density``: grains per second, 0.5 … 100. Default 25.
  * ``spray_time``: onset jitter, 0 (synchronous) … 1 (asynchronous).
    Default 0.
  * ``size``: grain length in ms, 10 … 500. Default 80. (Rounded to an
    even sample count so 50% overlap is exact.)
  * ``pitch``: transposition in semitones, −24 … +24. Default 0.
  * ``spray_pitch``: transposition scatter, ± cents, 0 … 1200. Default 0.
  * ``position``: how far back the grains read, 0 (now) … 1 (``buffer``
    seconds ago). Default 0.
  * ``spray_pos``: read-point scatter, ± in ``position`` units, 0 … 1.
    Default 0.
  * ``position_cv_depth``: fractions of the buffer per CV unit on
    ``position_cv``, −1 … 1. Default 1 (a 0..1 CV sweeps the buffer).
  * ``window``: ``hann`` | ``triangle`` | ``expo``. Default ``hann``.
  * ``width``: stereo scatter, 0 (every grain centred) … 1. Default 0.
  * ``freeze``: hold the buffer (ORed with the ``freeze`` gate). Default
    off.
  * ``mix``: dry/wet, 0 … 1. Default 1.
  * ``seed``: the random stream. Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Valid ``window`` values; the UI combo renders these in this order.
GRANULAR_WINDOWS = ("hann", "triangle", "expo")


@register_module_type
class Granular(Module):
    """Grain cloud over a live-captured ring buffer (slices 1 + 2 + 3).

    Parameters:
        buffer: History length, seconds, 0.5..10. Default 2.
        density: Grains per second, 0.5..100. Default 25.
        spray_time: Onset jitter, 0 (synchronous)..1 (asynchronous). Default 0.
        size: Grain length, ms, 10..500. Default 80.
        pitch: Transposition, semitones, -24..24. Default 0.
        spray_pitch: Transposition scatter, +- cents, 0..1200. Default 0.
        position: Read point, 0 (now)..1 (``buffer`` s ago). Default 0.
        spray_pos: Read-point scatter, +- in position units, 0..1. Default 0.
        position_cv_depth: Buffer fractions per CV unit, -1..1. Default 1.
        window: ``"hann"``, ``"triangle"`` or ``"expo"``. Default ``"hann"``.
        width: Per-grain stereo scatter, 0..1. Default 0.
        freeze: Hold the buffer (ORed with the gate). Default False.
        mix: Dry/wet, 0..1. Default 1.
        seed: Random stream seed (int). Default 1.

    Ports:
        in (in, audio): the signal (voice sources are summed).
        position_cv (in, cv): read point offset, latched per grain at its onset.
        freeze (in, gate): high holds the buffer (ORed with the param).
        out (out, audio): the cloud, every grain at unity, blended by ``mix``.
        out_l / out_r (out, audio): the cloud with per-grain pans.
    """

    TYPE = "granular"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "buffer": 2.0,
        "density": 25.0,
        "spray_time": 0.0,
        "size": 80.0,
        "pitch": 0.0,
        "spray_pitch": 0.0,
        "position": 0.0,
        "spray_pos": 0.0,
        "position_cv_depth": 1.0,
        "window": "hann",
        "width": 0.0,
        "freeze": False,
        "mix": 1.0,
        "seed": 1,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("position_cv", "in", "cv"),
        Port("freeze", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
