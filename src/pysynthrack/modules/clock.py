"""Clock module — tempo to a steady gate pulse train.

The metronome of the rack. Turns a tempo (BPM) into a regular gate `out`
that other modules step off — most obviously a [`sequencer`](#sequencer)'s
`clock` input, but equally an [`adsr`](#adsr)/[`ad_envelope`](#ad_envelope)
trigger, a [`sample_hold`](#sample_hold) `trig`, or anything that wants a
beat. It free-runs whenever the transport is playing; there is no audio and
no input, just a phase accumulator emitting pulses.

The pulse rate is `bpm / 60 × division` Hz, where `division` is pulses per
beat — 1 = a pulse every quarter note, 2 = eighths, 4 = sixteenths. So the
default 120 BPM × 4 = 8 pulses/second. `pulse_width` is the duty cycle (the
fraction of each period the gate stays high); a downstream rising-edge
consumer only cares about the leading edge, but a wider pulse gives an
audible gate length when wired straight to a VCA.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Clock(Module):
    """Tempo-driven gate pulse generator.

    Parameters:
        bpm: Tempo in beats per minute.
        division: Pulses per beat (1 = quarter, 2 = eighth, 4 = sixteenth).
            The pulse frequency is ``bpm / 60 * division`` Hz.
        pulse_width: Duty cycle in (0, 1) — fraction of each pulse period
            the gate is high.
    """

    TYPE = "clock"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "bpm": 120.0,
        "division": 4.0,
        "pulse_width": 0.5,
    }
    INPUT_PORTS: list[Port] = []
    OUTPUT_PORTS = [Port("out", "out", "gate")]
