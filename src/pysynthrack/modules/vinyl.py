"""Vinyl — surface noise and warp: the record, not the tape.

[`tape`](#tape)'s scrappy sibling, one knob per vice, all seeded:

  * ``crackle`` — dust ticks: seeded Poisson impulses (both the rate
    and the amplitude scale with the knob), each a short low-passed
    click with random sign and size. The classic run-in groove.
  * ``rumble`` — turntable-bearing rumble: seeded noise through a
    ~40 Hz resonant low-pass. Felt more than heard; mind the subs.
  * ``wobble`` — once-per-revolution pitch wobble at 0.55 Hz (33⅓ rpm)
    via a modulated fractional delay (the chorus/tape vibrato core) —
    up to ~±24 cents at full. Engaging it puts the signal on the
    turntable: the wet path rides a ~5 ms nominal delay, so the 0↔on
    edge of the knob is a patch-edit moment, not an automation lane.

All three at zero is a **bit-exact passthrough** (the input buffer
itself — the tape/octaver precedent). Everything is deterministic per
``seed`` and **exactly block-size independent**: the noise streams are
drawn per absolute-sample window (a seeded rng keyed by window index),
so any block split sees the identical dust.

One turntable: a polyphonic input sums to mono at the door (the house
collapse rule), and the output is mono. Surface noise is added after
the wobble (a 24-cent wobble on a click is imperceptible; documented
simplification). With nothing patched into ``in`` the module still
emits its crackle + rumble — a free surface-noise bed.

Ports:
  * ``in`` (audio, in): the record. Unpatched → the noise bed alone.
  * ``out`` (audio, out): the played record.

Params:
  * ``crackle``: dust amount, 0..1. Default 0.3.
  * ``rumble``: bearing rumble, 0..1. Default 0.2.
  * ``wobble``: 0.55 Hz pitch wobble depth, 0..1. Default 0.2.
  * ``seed``: which pressing of the record (≥ 0). Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Vinyl(Module):
    """Surface noise + 33⅓ rpm warp (see the module docstring).

    Parameters:
        crackle: Dust-tick rate/level, 0..1. Default 0.3.
        rumble: ~40 Hz bearing rumble level, 0..1. Default 0.2.
        wobble: 0.55 Hz pitch-wobble depth, 0..1. Default 0.2.
        seed: The pressing — deterministic character. Default 1.

    Ports:
        in (in, audio): the record. Unpatched → noise bed only.
        out (out, audio): the played record (mono).
    """

    TYPE = "vinyl"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "crackle": 0.3,
        "rumble": 0.2,
        "wobble": 0.2,
        "seed": 1,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
