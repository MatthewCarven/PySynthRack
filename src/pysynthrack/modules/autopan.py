"""Autopan -- the stereo motion utility (a panner with its own LFO).

Two VCAs and a law. A mono source is *placed* between the speakers; a
stereo pair is *balanced*; and an internal LFO swings the position back
and forth -- free-running at ``rate``, or locked to a clock so one full
left-right-left cycle takes ``division`` ticks.

**Mono or stereo.** Cable ONE input (either jack) and the module treats
it as a mono source and places it with the chosen ``law``:

* ``power`` (the default) -- sin/cos, -3 dB at centre, constant POWER
  everywhere, so a sweep keeps an even loudness. The same placement the
  ``stereo_speaker_output`` sink uses.
* ``compromise`` -- -4.5 dB at centre, the geometric mean of the other
  two (the console-pan compromise).
* ``linear`` -- -6 dB at centre, constant AMPLITUDE: the two sides
  always sum to the source, so a pan collapsed to mono never moves.

Cable BOTH and it is a stereo pair under a BALANCE control: unity on
both sides at centre, the far side fading with the stereo sink's cosine
taper as the position moves away from it (``law`` does not apply). At
``pan`` 0 and ``depth`` 0 a stereo pair passes through bit-exactly.

**The LFO.** ``position = clip(pan + pan_cv + depth * lfo, -1, 1)``.
``shape`` is ``sine``, ``triangle`` (constant speed, a hang at each
side) or ``square`` -- which jumps from side to side, but never
instantly: the jump is a raised-cosine glide lasting 10 ms at every
rate, so a hard ping-pong does not click. The LFO phase is keyed to an
absolute sample count rather than accumulated, so a render at a steady
rate is bit-identical at any block size.

**Tempo sync.** Patch a gate into ``clock`` and the cycle stops being a
speed in Hz and becomes a length in ticks: one full L -> R -> L swing
every ``division`` ticks (4 = a swing per bar of four, 1 = per beat,
0.5 = a ping-pong on every eighth). The period is measured between the
last two rising edges (the phaser/flanger sync), and until two edges
have arrived ``rate`` still drives it.

**Tremolo, folded in.** ``tremolo`` is the phase between the two sides:
the right channel reads the LFO ``tremolo / 2`` cycles after the left.
At 0 both sides read the same position -- a plain autopan. At 1 they
read it half a cycle apart, which for these shapes mirrors it, so the
two gains are EQUAL and rise and fall together: a mono tremolo (from
silence up to unity, around the -3 dB centre). In between, the sound
swirls -- part pan, part throb. Constant power is a property of
``tremolo`` 0 only; a tremolo is meant to change the level.

Ports:
  * ``in_l`` / ``in_r`` (audio): the source. One cabled = mono, both =
    a stereo pair. A polyphonic (voice-aware) source is summed -- a
    pan is linear, so panning the mix IS panning every voice.
  * ``pan_cv`` (cv): added to the position per sample, 1:1 (a +-1 LFO
    sweeps the whole field; put a ``cv_scale`` in front for less). A
    polyphonic source is averaged. A non-finite sample reads as 0.
  * ``rate_cv`` (cv): 1 V/oct * ``cv_depth`` on ``rate``, read per
    block. Steps aside while the clock lock holds.
  * ``clock`` (gate): tempo sync -- a cycle every ``division`` ticks.
  * ``out_l`` / ``out_r`` (audio): the placed pair.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

AUTOPAN_SHAPES = ("sine", "triangle", "square")
AUTOPAN_LAWS = ("power", "compromise", "linear")
AUTOPAN_RATE_MIN = 0.01
AUTOPAN_RATE_MAX = 20.0
# The square's side-to-side glide, seconds: a raised-cosine crossing this
# long at every rate (the pan moves across it as a 50 Hz sine would).
AUTOPAN_SQUARE_EDGE_S = 0.010


@register_module_type
class Autopan(Module):
    """Panner + LFO: mono placement or stereo balance, swept, synced.

    Parameters:
        pan: The manual position, -1 (hard left) .. 1 (hard right). The
            centre the LFO swings around.
        depth: LFO swing in pan units, 0 (a static panner) .. 1.
        rate: LFO rate in Hz (0.01 .. 20) -- the free-running speed and
            the fallback while ``clock`` is unpatched or not yet locked.
        shape: ``sine`` | ``triangle`` | ``square`` (a 10 ms glide).
        tremolo: Phase between the sides, 0 (autopan) .. 1 (a mono
            tremolo, both gains moving together).
        law: Mono placement law -- ``power`` (-3 dB centre, constant
            power), ``compromise`` (-4.5 dB), ``linear`` (-6 dB,
            constant amplitude). A stereo pair is balanced instead.
        division: Clock ticks per full cycle (0.25 .. 64), used only
            while ``clock`` is patched.
        cv_depth: Octaves of LFO-rate shift per unit of ``rate_cv``.

    Ports:
        in_l (in, audio): left, or the mono source.
        in_r (in, audio): right, or the mono source.
        pan_cv (in, cv): per-sample position offset, 1:1.
        rate_cv (in, cv): 1 V/oct * ``cv_depth`` on ``rate``.
        clock (in, gate): tempo sync.
        out_l (out, audio): left.
        out_r (out, audio): right.
    """

    TYPE = "autopan"
    CATEGORY = "Routing & VCA"
    DEFAULT_PARAMS = {
        "pan": 0.0,
        "depth": 0.7,
        "rate": 0.5,
        "shape": "sine",
        "tremolo": 0.0,
        "law": "power",
        "division": 4.0,
        "cv_depth": 1.0,
    }
    INPUT_PORTS = [
        Port("in_l", "in", "audio"),
        Port("in_r", "in", "audio"),
        Port("pan_cv", "in", "cv"),
        Port("rate_cv", "in", "cv"),
        Port("clock", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
