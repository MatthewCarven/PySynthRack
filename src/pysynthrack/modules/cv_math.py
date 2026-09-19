"""CVMath — two-input CV algebra, every jack live at once.

[`logic`](#logic) for voltages. Two CV operands in, seven functions out,
all computed every block — no mode combo, you swap cables, not settings.
The rack could already add CVs ([`cv_combiner`](#cv_combiner)), scale and
offset them ([`cv_scale`](#cv_scale), [`cv_offset`](#cv_offset)) and slew
them ([`slew`](#slew)); what it could not do is *compare* two of them or
*multiply* them, and those are the moves that make one modulator shape
another:

  * ``min`` / ``max`` — the analog AND / OR. ``max`` of two slow LFOs is
    a shape neither has alone; ``min`` of an envelope and an LFO is the
    LFO ducked by the envelope.
  * ``avg`` — ``(a + b) / 2``, the blend that never overshoots either.
  * ``diff`` — ``a − b``. An LFO minus its slewed self is its rate of
    change; a pitch minus another is an interval.
  * ``mult`` — ``a × b``. The CV×CV multiplier the rack had no other jack
    for: an envelope on a vibrato's depth (delayed vibrato — nothing at
    the attack, full at the sustain), a slow LFO fading a fast one in
    and out, a gate-shaped CV.
  * ``rect`` — ``|a|``, full-wave. A bipolar LFO folded into a unipolar
    one at twice the rate.
  * ``inv`` — ``−a``. The flip (swap cables for ``−b``).

Zero parameters — deliberately, like ``logic``. An unpatched operand
reads **0**, which is the normalled trick again: with only ``a`` patched,
``max`` is its positive half-wave and ``min`` its negative one (half-wave
rectifiers for free), ``avg`` is ``a/2``, ``diff`` is ``a`` itself,
``mult`` is silence. Both unpatched: everything is 0.

Stateless, elementwise, exact. Shape-polymorphic: mono CVs give mono
outs; a voice-aware ``(V, F)`` operand gives ``(V, F)`` outs, a mono
partner broadcasting across the voice axis (a per-voice pitch times a
mono envelope, for instance). A single voice row is bit-identical to
mono.

Ports:
  * ``a``, ``b`` (cv, in): the two operands. Unpatched → 0.
  * ``min``, ``max``, ``avg``, ``diff``, ``mult`` (cv, out): the pair
    functions.
  * ``rect``, ``inv`` (cv, out): the one-operand functions, of ``a``.

Params: none.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class CVMath(Module):
    """Two-in CV algebra: min / max / avg / diff / mult / rect / inv, all live.

    Parameters: none.

    Ports:
        a (in, cv): operand A. Unpatched → 0.
        b (in, cv): operand B. Unpatched → 0.
        min (out, cv): min(a, b).
        max (out, cv): max(a, b).
        avg (out, cv): (a + b) / 2.
        diff (out, cv): a − b.
        mult (out, cv): a × b.
        rect (out, cv): |a|.
        inv (out, cv): −a.
    """

    TYPE = "cv_math"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS: dict = {}
    INPUT_PORTS = [
        Port("a", "in", "cv"),
        Port("b", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("min", "out", "cv"),
        Port("max", "out", "cv"),
        Port("avg", "out", "cv"),
        Port("diff", "out", "cv"),
        Port("mult", "out", "cv"),
        Port("rect", "out", "cv"),
        Port("inv", "out", "cv"),
    ]
