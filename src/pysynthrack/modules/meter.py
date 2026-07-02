"""Meter — a level indicator you patch any audio signal into.

One or two audio inputs with **pass-through** outputs, so a Meter can be
dropped inline in a chain (`source → meter → speaker`) without changing
the sound, or hung off a fan-out cable purely to watch a level. The node
shows the signal's recent level in dBFS (−90 → 0), updated about as fast
as audio blocks arrive.

It's a monitoring tap, not a processor: the audio passes through
untouched (same samples, same shape — mono or voice-aware). ``in`` → ``out``
is the first channel; patch the optional ``in_r`` to meter a stereo pair
(``in_r`` → ``out_r``) and the node grows a second bar. Leave ``in_r``
unpatched and the Meter is exactly the single-channel meter it always was.

Each channel's bar is drawn from an envelope computed on the audio thread
(see ``NumpyBackend._render_meter``), so short transients register even
between UI repaints. What the bar shows depends on ``mode``:

* ``peak`` (default) — fast-attack / adjustable-release peak envelope:
  the classic "recent maximum" reading. Attack is instant; ``release``
  is roughly the time (in seconds) for the reading to drop about 20 dB.
* ``rms`` — a ~300 ms RMS average: reads closer to perceived loudness
  and sits lower than peak on transient material (a sine reads its
  amplitude minus ~3 dB).

Two extra indicators ride on top of the bar in either mode:

* a **peak-hold tick** — a marker that sits at the most recent peak for
  ~1.5 s before falling at the ``release`` rate (DAW-style), so you can
  read a transient's true level after the bar has fallen;
* a **clip lamp** — lights the moment any sample reaches 0 dBFS
  (|sample| ≥ 1.0) and stays lit for ~2 s, so a momentary overload
  can't slip past between glances.

Use it to compare source levels at a glance — e.g. a MicInput against a
FilePlayer — before they hit a mixer, to watch a stereo effect's two
channels, or to spot a stage that's clipping or far too quiet.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# What the level bar shows. ``peak`` is the historical fast-attack /
# slow-release peak envelope (bit-identical to the pre-``mode`` Meter);
# ``rms`` is a ~300 ms root-mean-square average (loudness-ish).
METER_MODES = ("peak", "rms")


@register_module_type
class Meter(Module):
    """Audio level meter (dBFS). Pass-through: ``in`` → ``out``, ``in_r`` → ``out_r``.

    The displayed level is rendered by the backend per channel: a
    fast-attack / adjustable-release peak envelope (``mode="peak"``) or a
    ~300 ms RMS average (``mode="rms"``). A peak-hold tick (~1.5 s hold,
    then falls at the ``release`` rate) and a clip lamp (lit ~2 s after
    any sample reaches 0 dBFS) ride on top of the bar in either mode.
    ``in_r`` is optional — patch it and the node meters a stereo pair.

    Parameters:
        release: Fall time in seconds — roughly how long the bar takes to
            drop ~20 dB after a peak. Smaller = snappier / more reactive
            (catches transients and clipping); larger holds peaks longer.
            Also sets how fast the peak-hold tick falls once its hold
            time expires.
        mode: ``"peak"`` (default, the classic recent-maximum bar) or
            ``"rms"`` (average level, reads closer to loudness).
    """

    TYPE = "meter"
    DEFAULT_PARAMS = {"release": 0.4, "mode": "peak"}
    INPUT_PORTS = [Port("in", "in", "audio"), Port("in_r", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio"), Port("out_r", "out", "audio")]
