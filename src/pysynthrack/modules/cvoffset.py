"""CVOffset — add a fixed DC level to a CV signal.

A pointwise shift on a control signal: ``out = in + offset``. Where
:class:`CVScale` changes a modulator's *depth*, ``CVOffset`` changes
its *centre* — it slides the whole signal up or down. The two compose
into a full affine map (``scale`` then ``offset``); kept separate so
each does one obvious thing.

  * Re-centre a bipolar source: an LFO swinging ±1 fed through
    ``offset = 1.0`` becomes a 0..2 unipolar signal — handy for a VCA
    amp CV that should never go negative (no phase inversion).
  * Bias a destination: nudge a filter cutoff CV up by a fixed amount
    so the sweep sits higher.
  * Manual DC: with nothing patched in, the input is treated as 0, so
    an unpatched ``CVOffset`` simply emits a constant ``offset`` — a
    quick stand-in for a :class:`Constant` when you're already adding
    a bias stage.

Param:
  * ``offset``: the level added to every sample. Default 0.0
    (transparent pass-through).

Voice-awareness:
  Pure pointwise arithmetic, so it's shape-polymorphic for free with
  no per-voice state. A 1D ``(frames,)`` CV yields ``(frames,)``; a
  voice-aware ``(V, frames)`` CV yields ``(V, frames)``, the scalar
  ``offset`` broadcasting across the voice axis. An unpatched input is
  treated as 0, so the output is a constant ``offset``.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class CVOffset(Module):
    """Add a fixed DC level to a CV: ``out = in + offset``.

    Parameters:
        offset: Level added to every sample. Default 0.0.

    Ports:
        in (in, cv): the CV to offset. Unpatched is treated as 0, so
            the output becomes a constant ``offset``.
        out (out, cv): the offset CV.
    """

    TYPE = "cv_offset"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {"offset": 0.0}
    INPUT_PORTS = [Port("in", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "cv")]
