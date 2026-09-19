"""Clock module — tempo to a steady gate pulse train.

The metronome of the rack. Turns a tempo (BPM) into a regular gate `out`
that other modules step off — most obviously a [`sequencer`](#sequencer)'s
`clock` input, but equally an [`adsr`](#adsr)/[`ad_envelope`](#ad_envelope)
trigger, a [`sample_hold`](#sample_hold) `trig`, or anything that wants a
beat. It free-runs whenever the transport is playing; there is no audio,
just a phase accumulator emitting pulses — and, since the 2026-09-19 love
pass, a transport of its own.

The pulse rate is `bpm / 60 × division` Hz, where `division` is pulses per
beat — 1 = a pulse every quarter note, 2 = eighths, 4 = sixteenths. So the
default 120 BPM × 4 = 8 pulses/second. `pulse_width` is the duty cycle (the
fraction of each period the gate stays high); a downstream rising-edge
consumer only cares about the leading edge, but a wider pulse gives an
audible gate length when wired straight to a VCA.

The transport is three optional inputs. `reset` (gate): a rising edge
restarts the period AT that sample — the phase becomes 0 there, so the gate
goes high on the reset sample itself (a new pulse lands on the downbeat,
sample-accurate) and the following pulses count from it; a reset IS a fresh
clock from that sample. `run` (gate): unpatched the clock runs, as it always
did; patched, it runs while `run` is high and is HELD while low — output
low, phase frozen — so a downstream sequencer keeps its step, and a rising
edge on `run` restarts the phase at 0 so the first pulse lands on the sample
play starts. A reset edge while held zeros the phase but emits nothing until
run rises (which re-zeros it anyway). `bpm_cv` (cv) scales the tempo by
`2 ** (bpm_cv_depth * cv)` — tempo doublings per unit, the 1 V/oct style,
block-mean like every block-rate CV in the rack, exponent clipped to ±6.
Gates are mono: a `(V, F)` source on `reset` or `run` collapses to
any-voice-high; a `(V, F)` source on `bpm_cv` is averaged.

`swing` (2026-09-20) is the shuffle: every SECOND pulse is late by `swing`
of a period — the [`clock_divider`](#clock_divider)'s convention, the same
units (a fraction of the period, 0.33 the triplet feel, 0.5 the hard
shuffle) and the same rule (the odd pulse keeps its width; the even pulses
do not move). The even pulses — the downbeat and every other one — sit
exactly where the straight clock puts them, sample for sample; the odd
pulses' rising edges move later by `swing × period`, a fraction of the
CURRENT period, so `bpm_cv` swings with the tempo. A `reset` (or a `run`
rise) restarts the even/odd count: the pulse on the reset sample is an even
one. The ceiling: an odd pulse is cut one sample before the next even edge,
so a late pulse can never swallow the downbeat — past `swing + pulse_width
= 1` the odd pulse is shorter than the even one, never longer. 0 is the
straight clock, bit-exact.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# The panel's swing ceiling: 0.5 is the hard shuffle (the odd pulse lands
# halfway to the next even one); the backend clamps at the divider's 0.75.
CLOCK_MAX_SWING_UI = 0.5


@register_module_type
class Clock(Module):
    """Tempo-driven gate pulse generator.

    Parameters:
        bpm: Tempo in beats per minute.
        division: Pulses per beat (1 = quarter, 2 = eighth, 4 = sixteenth).
            The pulse frequency is ``bpm / 60 * division`` Hz.
        pulse_width: Duty cycle in (0, 1) — fraction of each pulse period
            the gate is high.
        bpm_cv_depth: Tempo doublings per CV unit on ``bpm_cv`` (1.0 = a
            CV of +1 doubles the tempo, -1 halves it). 0 disables the
            input without unpatching it.
        swing: Every second pulse is delayed by this fraction of the
            period (0.33 = triplet swing, 0.5 = the hard shuffle; the
            panel stops at 0.5, the backend takes the divider's 0.75).
            The even pulses never move; 0 = straight.
    """

    TYPE = "clock"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "bpm": 120.0,
        "division": 4.0,
        "pulse_width": 0.5,
        "bpm_cv_depth": 1.0,
        "swing": 0.0,
    }
    INPUT_PORTS = [
        Port("reset", "in", "gate"),
        Port("run", "in", "gate"),
        Port("bpm_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "gate")]
