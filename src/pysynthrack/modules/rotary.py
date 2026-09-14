"""Rotary — the Leslie: a spinning horn and a spinning drum, in stereo.

The ``organ``'s destined partner. A rotary cabinet splits the signal at a
crossover, sends the treble to a **horn** that spins on a vertical axis
and the bass to a **drum** (a rotating baffle around a fixed woofer),
and the room hears three things at once from each rotor: a **tremolo**
(the horn is loud when it points at you, quiet when it points away), a
**Doppler vibrato** (it is coming toward you, then going away — the
pitch rises and falls), and a **stereo swirl** (two ears, or two mics,
hear those two things at different moments). Chorus and phaser are
imitations of this; this is the thing itself.

The two rotors are not the same machine. The horn is light and quick:
it spins at ~0.7 Hz on **slow** (the *chorale* — a slow, churchy sway)
and ~6.7 Hz on **fast** (the *tremolo* — the gospel shimmer), and it
gets from one to the other in about a second. The drum is heavy: it
turns a touch slower (×0.85), takes four or five seconds to spin up and
six to coast down, and spins the *other way*. That difference — the
horn already whirling while the bass is still gathering itself — **is**
the Leslie sound, and it is why the cabinet is played as much as the
organ is. Flip ``speed`` (or gate ``fast``) at the top of a phrase and
let the two rotors chase each other. ``stop`` is the brake: both rotors
coast to a halt wherever they are, and the image freezes there.

How it is built:

  * a Linkwitz–Riley 4th-order crossover (the [crossover] module's,
    reused) at ``crossover`` Hz — 800 Hz is the classic 122;
  * per rotor, an angle that integrates a rate that *ramps* toward its
    target (exponential approach — belt-driven motors accelerate that
    way), with separate accelerate and decelerate time constants,
    scaled by ``ramp``;
  * per rotor and per mic, an **amplitude** that follows ``cos`` of the
    angle between the mouth and the mic (the horn beams; the drum, being
    a baffle, less so), and a **fractional delay** that follows the
    mouth's distance to the mic — a real Doppler, not a pitch LFO: the
    horn mouth is ~19 cm off-axis, so its delay swings ~0.55 ms and at
    6.7 Hz that is ±40 cents; the drum's ~14 cm baffle, less;
  * two virtual mics, ``spread`` × 90° either side of the front, hearing
    both rotors — at ``spread`` 0 they coincide and the output is mono,
    at 1 they are 180° apart and the swirl is as wide as it gets;
  * ``balance`` tilts horn against drum (−1 = drum only, +1 = horn only);
    ``depth`` scales both the tremolo and the Doppler (0 = the cabinet
    stands still and you hear only the crossover + a fixed delay).

``mix`` blends the raw input back in, delay-matched to the rotors' centre
delay so a half-and-half blend thickens rather than combs.

Ports:
  * ``in`` (audio): the signal. A ``(V, F)`` voice source is summed —
    a cabinet is one physical thing. Unpatched → silence.
  * ``fast`` (gate): while patched it *is* the speed switch — high =
    fast, low = slow — and the ``speed`` combo is ignored. Patch a slow
    clock to hear the rotors chase each other every few seconds.
  * ``out_l`` / ``out_r`` (audio): the two mics.
  * ``out`` (audio): the mono sum, ``(L + R) / 2``.

Params:
  * ``speed``: ``slow`` | ``fast`` | ``stop``. Default ``slow``.
  * ``slow_rate`` / ``fast_rate``: the horn's Hz on each setting;
    the drum runs at 0.85× either. Defaults 0.7 / 6.7.
  * ``ramp``: scales every spin-up / coast-down time (horn ~1 s up,
    1.5 s down; drum ~4.5 s up, 6 s down). 0.25 … 4. Default 1.
  * ``depth``: tremolo + Doppler intensity, 0 … 1. Default 0.7.
  * ``spread``: mic separation, 0 (mono) … 1 (180°). Default 0.7.
  * ``balance``: drum ↔ horn, −1 … +1. Default 0.
  * ``crossover``: split frequency, Hz. Default 800.
  * ``mix``: dry/wet, 0 … 1. Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Valid ``speed`` values; the UI combo renders these in this order.
ROTARY_SPEEDS = ("slow", "fast", "stop")


@register_module_type
class Rotary(Module):
    """Leslie rotary cabinet: horn + drum, tremolo + Doppler, stereo.

    Parameters:
        speed: ``"slow"`` (chorale), ``"fast"`` (tremolo) or ``"stop"``
            (brake). Default ``"slow"``. Ignored while ``fast`` is patched.
        slow_rate: Horn Hz on slow, 0.1..3. Default 0.7.
        fast_rate: Horn Hz on fast, 2..12. Default 6.7.
        ramp: Spin-up / coast-down time scale, 0.25..4. Default 1.
        depth: Tremolo + Doppler intensity, 0..1. Default 0.7.
        spread: Mic separation, 0 (mono)..1 (180 deg). Default 0.7.
        balance: Drum (-1) .. horn (+1) tilt. Default 0.
        crossover: Horn/drum split, Hz. Default 800.
        mix: Dry/wet, 0..1. Default 1.

    Ports:
        in (in, audio): the signal (voice sources are summed).
        fast (in, gate): high = fast, low = slow; overrides ``speed``.
        out_l / out_r (out, audio): the two mics.
        out (out, audio): (L + R) / 2.
    """

    TYPE = "rotary"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "speed": "slow",
        "slow_rate": 0.7,
        "fast_rate": 6.7,
        "ramp": 1.0,
        "depth": 0.7,
        "spread": 0.7,
        "balance": 0.0,
        "crossover": 800.0,
        "mix": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("fast", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
        Port("out", "out", "audio"),
    ]
