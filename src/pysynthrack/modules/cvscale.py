"""CVScale — multiply a CV signal by a fixed factor (an attenuverter).

A pointwise gain on a control signal: ``out = in * scale``. In modular
terms this is the classic *attenuverter* — attenuate when |scale| < 1,
amplify when |scale| > 1, and invert when scale is negative.

Why it matters: modulation sources ship at fixed depths (an LFO swings
±depth, an ADSR rises to its sustain), but a destination often wants
*less* of that swing, or the mirror image of it. ``CVScale`` is the
trim. Pair it with :class:`CVOffset` (``in * scale`` then ``+ offset``)
to place any modulator into any range — the two compose into a full
affine map, kept as two small orthogonal utilities in the modular
spirit rather than one combined module.

  * Attenuate: ``scale = 0.3`` tames a full-depth LFO into a gentle
    wobble.
  * Invert: ``scale = -1.0`` flips an envelope so it ducks instead of
    swells (feed it a VCA for sidechain-style gain dips).
  * Boost: ``scale = 2.0`` widens a shy modulator.

Param:
  * ``scale``: the multiplier. Default 1.0 (unity pass-through).

Voice-awareness:
  Pure pointwise arithmetic, so it's shape-polymorphic for free with
  no per-voice state. A 1D ``(frames,)`` CV yields ``(frames,)``; a
  voice-aware ``(V, frames)`` CV yields ``(V, frames)``. An unpatched
  input is treated as 0 and yields silence (``0 * scale == 0``).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class CVScale(Module):
    """Scale a CV by a fixed factor: ``out = in * scale``.

    Parameters:
        scale: Multiplier applied to every sample. Default 1.0. Use
            <1 to attenuate, >1 to amplify, negative to invert.

    Ports:
        in (in, cv): the CV to scale. Unpatched is treated as 0.
        out (out, cv): the scaled CV.
    """

    TYPE = "cv_scale"
    DEFAULT_PARAMS = {"scale": 1.0}
    INPUT_PORTS = [Port("in", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "cv")]
