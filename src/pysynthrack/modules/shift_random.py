"""ShiftRandom — looping shift-register random CV (the generative classic).

A 16-bit shift register clocked by a gate. Each rising ``clock`` edge the
register rotates one place: the bit falling off the loop end (set by
``length``) comes back around to the front — and with ``probability`` it is
flipped on the way. The first eight bits, newest bit as the most
significant, form a byte read as ``value / 255``, scaled by ``range``:

    cv = (register[0..7] as byte) / 255 × range          (unipolar)
    cv = ((byte / 255) × 2 − 1) × range                  (bipolar)

``probability`` is the money knob. At 0 the register is a **locked loop**:
the same ``length``-step pattern repeats forever, a found melody. At 1
every incoming bit is a fresh coin flip — pure random. In between the loop
*mostly* repeats and occasionally mutates, which is where the musical
magic lives (≈0.1 keeps a motif recognisable for minutes while it slowly
walks somewhere new).

``gate`` mirrors register bit 0 — high while the newest bit is set, held
between clocks — a free rhythm output that mutates in lockstep with the
melody. ``write`` (optional) forces the incoming bit high while held, a
performance "seed it NOW" handle.

All randomness comes from ``seed``: the register's initial contents and
every coin flip are drawn from a generator seeded with it, so a patch
reloads with the exact character it was saved with, and renders are
deterministic (testable). Changing ``seed`` re-rolls the register on the
spot. ``length`` changes take effect at the next clock (the register
itself always keeps 16 bits of history, so shortening and re-lengthening
the loop recovers the old tail — the hardware behaviour).

Mono: the clock has no voice context, so outputs are plain ``(frames,)``
buffers (Constant/Noise precedent); they broadcast against any per-voice
consumer. Between clock edges both outputs hold — the CV is stepped, so
follow it with ``slew`` for glides or ``quantizer`` for melody.

Ports:
  * ``clock`` (gate): advance one step per rising edge. Unpatched → holds.
  * ``write`` (gate): while high, the incoming bit is forced to 1.
  * ``cv`` (cv out): the register byte scaled to ``range``.
  * ``gate`` (gate out): register bit 0, held between clocks.

Params:
  * ``probability``: chance the looping bit flips, 0..1. Default 0.1.
  * ``length``: loop length in steps, 2..16. Default 8.
  * ``range``: CV span. Unipolar 0..range; bipolar ±range. Default 2.0.
  * ``bipolar``: centre the output on 0. Default off.
  * ``seed``: RNG seed (non-negative int). Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class ShiftRandom(Module):
    """Looping shift-register random CV — a lockable, mutating step source.

    Parameters:
        probability: Chance (0..1) that the recirculating bit flips on
            each clock. 0 = locked loop, 1 = coin flips. Default 0.1.
        length: Loop length in clocks, 2..16. Default 8.
        range: Output span. Default 2.0.
        bipolar: When True the CV is centred on 0 (±range); when False
            it spans 0..range. Default False.
        seed: Seed for the register init and every flip decision;
            deterministic per seed. Default 1.

    Ports:
        clock (in, gate): rotate one step per rising edge.
        write (in, gate): while high, the incoming bit is forced to 1.
        cv (out, cv): register byte scaled to ``range``.
        gate (out, gate): register bit 0, held between clocks.
    """

    TYPE = "shift_random"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "probability": 0.1,
        "length": 8,
        "range": 2.0,
        "bipolar": False,
        "seed": 1,
    }
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("write", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("cv", "out", "cv"),
        Port("gate", "out", "gate"),
    ]
