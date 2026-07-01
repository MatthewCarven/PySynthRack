"""Meter — a level indicator you patch any audio signal into.

One audio input and a **pass-through** audio output, so a Meter can be
dropped inline in a chain (`source → meter → speaker`) without changing
the sound, or hung off a fan-out cable purely to watch a level. The node
shows the signal's recent peak level in dBFS (−90 → 0), updated about as
fast as audio blocks arrive.

It's a monitoring tap, not a processor: the audio passes through
untouched (same samples, same shape — mono or voice-aware). The "recent
maximum" reading is a fast-attack / adjustable-release peak envelope
computed on the audio thread (see ``NumpyBackend._render_meter``), so
short transients register even between UI repaints, and the bar falls
back afterwards at a rate you set with ``release``.

The ``release`` knob is how quickly the bar falls back: it's roughly the
time (in seconds) for the reading to drop about 20 dB after a peak. Small
values (a tenth of a second or so) make the meter snappy and reactive —
good for catching transients and spotting clipping — while larger values
hold peaks longer for an easier read. Attack is always instant.

Use it to compare source levels at a glance — e.g. a MicInput against a
FilePlayer — before they hit a mixer, or to spot a stage that's clipping
or far too quiet.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Meter(Module):
    """Audio level meter (peak, dBFS). Pass-through: ``in`` → ``out``.

    The displayed level is a fast-attack / adjustable-release peak
    envelope rendered by the backend; ``release`` sets the fall time.

    Parameters:
        release: Fall time in seconds — roughly how long the bar takes to
            drop ~20 dB after a peak. Smaller = snappier / more reactive
            (catches transients and clipping); larger holds peaks longer.
    """

    TYPE = "meter"
    DEFAULT_PARAMS = {"release": 0.4}
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
