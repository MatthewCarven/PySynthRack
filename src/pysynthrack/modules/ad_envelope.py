"""AD envelope — a trigger-style Attack/Decay envelope for percussion.

A two-stage envelope built for drums and plucks: a trigger fires it and
it plays out a full attack-then-decay shape on its own, *ignoring how
long the trigger is held*. That fire-and-forget behaviour is the whole
point — feed it a momentary clock pulse (an LFO→Schmitt, a keyboard or
MIDI gate) and every hit gets the same snappy A→D contour, with no
sustain stage to hold the tail open the way an ADSR would.

Behaviour:
  - Trigger rises → enter Attack from the current level (a retrigger
    mid-decay picks up where it was, so there's no click). Ramps to 1.0
    over ``attack`` seconds.
  - Attack done → Decay ramps to 0.0 over ``decay`` seconds, then idles
    at 0 until the next trigger. The trigger going low does nothing; the
    decay always runs to completion.

Tone-wise:
  - Short attack + short decay = hats, clicks, blips.
  - Short attack + medium decay into a VCA = kicks/toms (pair with a
    low sine); into a filter cutoff = a plucky "blip" sweep.

For a held-note envelope with a sustain stage, use [adsr] instead. Like
ADSR, the durations are parameters; CV over them is a later upgrade.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class ADEnvelope(Module):
    """Attack/Decay (trigger) envelope generator.

    Parameters:
        attack: Attack time in seconds, current level → 1.0 (0 → instant).
        decay: Decay time in seconds, 1.0 → 0.0 (0 → instant).
    """

    TYPE = "ad_envelope"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "attack": 0.005,
        "decay": 0.20,
    }
    INPUT_PORTS = [Port("trig", "in", "gate")]
    OUTPUT_PORTS = [Port("cv", "out", "cv")]
