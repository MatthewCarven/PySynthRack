"""ADSR envelope module — gate in, CV out.

Classic attack / decay / sustain / release shape, sample-accurate. The
envelope listens to a gate signal (high = note held, low = released)
and emits a 0-to-1 CV signal that downstream modules (typically a VCA,
also a Filter cutoff once we wire CV to params) shape by.

Behaviour at the edges of the gate:
  - Gate rises  → enter Attack from the current level. Ramps to 1.0 over
    ``attack`` seconds, then Decay ramps to ``sustain`` over ``decay``
    seconds, then holds until the gate falls.
  - Gate falls  → enter Release. The level at the time of release is
    captured and ramped to 0 over ``release`` seconds. This means a key
    that's released mid-attack still takes the full release time — no
    snap.

Velocity (``vel``, 2026-09-19 love pass). A cv input that scales the
whole envelope of a note — attack peak, sustain plateau and the release
tail alike — the way a velocity-sensitive VCA does after an analog
envelope: ``cv = shape × vel``. It is read **at the gate's rising-edge
sample and latched for that note**, the house edge-latch rule the drums
and the sampler follow (a note is one note; a velocity bus that moves
mid-note must not warp the envelope until the next key-down). Negative
values clamp to 0 (a silent note); values above 1 are honoured, so a hot
bus gives a hot envelope. Unpatched it is exactly 1.0 and the module is
bit-for-bit what it was before the input existed.

Timing does not change with velocity: a soft note's attack still takes
``attack`` seconds to reach its (lower) peak, so its slope is gentler.
The one place velocity meets the retrigger rule ("attack from the
current level, no click") is a note re-struck *softer* than it is
currently ringing: the level would have to jump down to the new peak.
Instead it falls to the new peak at the full-velocity attack slope and
then decays as usual — the output stays continuous, and a velocity of 0
on a ringing note fades it out over one attack time rather than cutting
it.

Voice-aware: a ``(V, F)`` ``vel`` latches per voice from its own row
(``midi_input.velocity_cv`` is exactly that), a mono ``vel`` is shared
by every voice, and a ``(V, F)`` ``vel`` on a mono gate collapses to the
loudest voice at the edge sample, as the drums do.

The four timing parameters live on the module; modulation inputs
(CV → attack, etc.) are a later upgrade.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class ADSR(Module):
    """Attack / decay / sustain / release envelope generator.

    Parameters:
        attack: Attack time in seconds (0 → instant).
        decay: Decay time in seconds from 1.0 down to ``sustain``.
        sustain: Sustain level in [0, 1].
        release: Release time in seconds from the gate-fall level down to 0.

    Inputs:
        gate (gate): note on/off; voice-aware.
        vel (cv): velocity multiplier on the envelope's amplitude, read at
            the gate's rising edge and latched for that note; ``max(0, cv)``.
            Unpatched → 1.0 (a knobless multiplier, like ``vca.cv``).
    """

    TYPE = "adsr"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "attack": 0.01,
        "decay": 0.10,
        "sustain": 0.70,
        "release": 0.30,
    }
    INPUT_PORTS = [
        Port("gate", "in", "gate"),
        Port("vel", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("cv", "out", "cv")]
