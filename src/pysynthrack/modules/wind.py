"""Wind — a blown-pipe physical model (flute or reed).

The blown half of the rack's sustained physical-modeling pair (the other
is [`bowed`](#bowed)). A column of air in a pipe is a delay line; what
keeps it singing is the player's breath through a nonlinearity at the
mouth — an air jet flapping across an embouchure hole, or a reed slapping
against a mouthpiece — and the note is the pipe's own resonance, held for
as long as ``gate`` is high. ``model`` picks the mouth:

  * ``flute`` — Cook's slide flute (STK ``Flute``): breath pressure minus
    the pipe's reflection drives a **jet** delay line whose output passes
    through the cubic jet table ``x(x² − 1)`` and back into the bore; a
    lowpass and a DC blocker sit in the bore's return. The bore is tuned
    to one and a half periods so the pipe speaks in its overblown
    register — STK's own trick — which is why it sounds like a flute
    rather than a whistle. Breathy and hollow; blow hard at the bottom of
    the range and it goes sharp, as a flute does.
  * ``reed`` — the STK ``Clarinet`` mouthpiece: one round-trip delay line
    with a lowpass loss, and the **reed table** ``clip(0.7 − 0.3·Δp)``
    where ``Δp`` is the pressure difference across the reed. It needs
    enough pressure before it speaks (a real reed does too), and too much
    closes it (also real). A closed-pipe voice: hollow low down, reedy
    and bright as you blow harder, and harmonic-rich where the flute is
    nearly pure.

``breath`` is the player's pressure, mapped for each model so 0..1 runs
from just-speaking to hard-blown; ``noise`` adds breath noise (seeded
per voice and per note, so renders are deterministic); ``attack`` /
``release`` are the breath ramps on the gate; ``damping`` is the pipe's
loss lowpass — darker as it rises. ``breath_cv`` adds ``cv_depth`` × CV to
``breath`` **per sample** (mono, shared by every voice): an envelope for
a swell, a slow LFO for a breathing player, a fast one for flutter.

Pitch is 1 V/oct on ``pitch_cv`` (C4 = 0 V), read per block; a
polyphonic source gives one independent pipe per voice, and silent
voices cost nothing. The flute's home range is ~C3 upward (a real flute
starts at C4; below ~80 Hz the jet model loses its register), the reed
goes down to the contrabass. The loop is advanced in vectorized chunks no
longer than the shortest delay (the jet for the flute, the whole bore
for the reed), so both models are cheap — a few percent of a block per
voice.

Ports:
  * ``pitch_cv`` (cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
  * ``gate`` (gate): breath on while high (per voice).
  * ``breath_cv`` (cv): adds to ``breath`` per sample (× ``cv_depth``).
  * ``out`` (audio): the pipe.

Params:
  * ``model``: ``flute`` / ``reed``. Default ``flute``.
  * ``breath``: blowing pressure, 0..1. Default 0.5.
  * ``noise``: breath noise, 0..1. Default 0.15.
  * ``attack``: breath ramp in on the gate, seconds. Default 0.04.
  * ``release``: breath ramp out, seconds. Default 0.1.
  * ``damping``: pipe losses / darkness, 0..1. Default 0.5.
  * ``cv_depth``: level per CV unit on ``breath_cv``. Default 1.
  * ``level``: output level 0..1. Default 0.5.
  * ``seed``: breath-noise seed (non-negative int). Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

WIND_MODELS = ("flute", "reed")


@register_module_type
class Wind(Module):
    """Blown pipe: a bore delay line kept singing by a jet or a reed.

    Parameters:
        model: ``flute`` (jet + bore, overblown register) or ``reed``
            (clarinet mouthpiece, closed pipe). Default ``flute``.
        breath: Blowing pressure, 0..1. Default 0.5.
        noise: Breath noise, 0..1. Default 0.15.
        attack: Breath ramp in, seconds. Default 0.04.
        release: Breath ramp out, seconds. Default 0.1.
        damping: Pipe losses (darkness), 0..1. Default 0.5.
        cv_depth: Level per CV unit on ``breath_cv``. Default 1.0.
        level: Output level. Default 0.5.
        seed: Breath-noise seed. Default 1.

    Ports:
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        gate (in, gate): breath on while high (per voice).
        breath_cv (in, cv): per-sample breath modulation.
        out (out, audio): the pipe.
    """

    TYPE = "wind"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "model": "flute",
        "breath": 0.5,
        "noise": 0.15,
        "attack": 0.04,
        "release": 0.1,
        "damping": 0.5,
        "cv_depth": 1.0,
        "level": 0.5,
        "seed": 1,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("gate", "in", "gate"),
        Port("breath_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
