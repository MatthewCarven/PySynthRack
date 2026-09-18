"""Bowed — a bowed-string physical model (sustained-excitation waveguide).

The third member of the rack's physical-modeling family. [`pluck`](#pluck)
strikes a string once and lets it ring; [`modal`](#modal) strikes a
resonator; this one keeps a bow on the string for as long as ``gate`` is
high, so the note *sustains* and swells and speaks — a cello line, a
scraped double bass, a saw-toothed viola drone, and past the sweet spot
the squeal and crunch a real bow makes when it is pressed too hard.

The model is the classic digital waveguide bowed string (Smith 1986, as
in STK's ``Bowed``): two delay lines meet at the bow — the bridge side
(``position`` of the string) and the nut side (the rest) — and at the
meeting point a **friction table** turns the difference between the bow's
velocity and the string's velocity into the velocity the bow injects back
into both halves. Below a threshold the string sticks to the bow and is
dragged; above it the string slips; the stick-slip cycle at the string's
own period is the note. The bridge end reflects through a one-pole
lowpass (``damping``, the string's losses) and the nut end inverts. What
you hear is the wave arriving at the bridge, optionally coloured by a small
bank of body resonances (``body``).

The bow's two hands:

  * ``velocity`` — how fast the bow moves. Mostly loudness, and the speed
    at which the note speaks. Its envelope is ``attack``/``release``:
    the bow lands over ``attack`` seconds after the gate rises and lifts
    over ``release`` seconds after it falls (the string then rings down
    on its own losses, quickly).
  * ``pressure`` — how hard the bow is pressed. The friction table's
    slope: light is airy and whistly (the string slips early), medium is
    the full Helmholtz tone, heavy is raw and scratchy, and hard pressure
    at high velocity is the crunch.

``position`` is where the bow sits along the string as a fraction of its
length: near the bridge (0.05–0.1) is bright and nasal, sul tasto (0.3+)
is soft and hollow. ``damping`` darkens the string. Pitch is 1 V/oct on
``pitch_cv`` (C4 = 0 V, the ``pluck`` convention) read per block, so a
glide or vibrato on the CV bends the note as it plays; a polyphonic source
gives one independent string per voice.

``pressure_cv`` and ``velocity_cv`` add ``cv_depth`` × CV to the two
hands **per sample** (mono, shared by every voice) — an LFO on
``pressure_cv`` is bow-pressure tremolo, an envelope on ``velocity_cv``
is a swell — so the bow can be played, not just switched.

Cost: the loop is advanced in vectorized chunks no longer than the
shortest delay (the bridge side), so a high note close to the bridge
costs more than a low note — roughly 7% of a 512-sample block at C4 for
one voice, a quarter at C6. It is a solo instrument by design; silent
voices cost nothing.

Ports:
  * ``pitch_cv`` (cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
  * ``gate`` (gate): the bow is on the string while high (per voice).
  * ``pressure_cv`` (cv): adds to ``pressure`` per sample (× ``cv_depth``).
  * ``velocity_cv`` (cv): adds to ``velocity`` per sample (× ``cv_depth``).
  * ``out`` (audio): the string at the bridge, through the body.

Params:
  * ``pressure``: bow force, 0..1. Default 0.5.
  * ``velocity``: bow speed, 0..1. Default 0.6.
  * ``position``: bow position along the string, 0.05..0.5. Default 0.127.
  * ``attack``: bow-landing time in seconds. Default 0.05.
  * ``release``: bow-lifting time in seconds. Default 0.15.
  * ``damping``: string losses / darkness, 0..1. Default 0.5.
  * ``body``: body-resonance mix, 0 (raw bridge) .. 1. Default 0.5.
  * ``cv_depth``: level per CV unit for the two ``*_cv`` inputs. Default 1.
  * ``level``: output level 0..1. Default 0.5.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Bowed(Module):
    """Bowed string: two waveguide halves meeting at a friction table.

    Parameters:
        pressure: Bow force, 0..1 (light .. crunch). Default 0.5.
        velocity: Bow speed, 0..1 (loudness). Default 0.6.
        position: Bow position as a fraction of the string, 0.05..0.5.
            Default 0.127.
        attack: Bow-landing ramp in seconds. Default 0.05.
        release: Bow-lifting ramp in seconds. Default 0.15.
        damping: String losses (darkness), 0..1. Default 0.5.
        body: Body-resonance mix, 0..1. Default 0.5.
        cv_depth: Level per CV unit on ``pressure_cv`` / ``velocity_cv``.
            Default 1.0.
        level: Output level. Default 0.5.

    Ports:
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        gate (in, gate): bow on the string while high (per voice).
        pressure_cv (in, cv): per-sample bow-force modulation.
        velocity_cv (in, cv): per-sample bow-speed modulation.
        out (out, audio): the string.
    """

    TYPE = "bowed"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "pressure": 0.5,
        "velocity": 0.6,
        "position": 0.127,
        "attack": 0.05,
        "release": 0.15,
        "damping": 0.5,
        "body": 0.5,
        "cv_depth": 1.0,
        "level": 0.5,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("gate", "in", "gate"),
        Port("pressure_cv", "in", "cv"),
        Port("velocity_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
