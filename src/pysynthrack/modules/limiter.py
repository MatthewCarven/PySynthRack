"""Limiter — a brickwall lookahead peak limiter.

The "demo can't clip" module. A limiter is the last thing in the chain:
it guarantees the output never rises above a hard ``ceiling``, no matter
how hot the signal arriving at its input. Unlike the compressor (which
eases gain down by a *ratio* around a threshold), the limiter is an
absolute wall — whatever it takes to keep the peak at or under the
ceiling, it does, instantly and completely.

**Lookahead.** A naive peak limiter that reacted only to the current
sample would have to clamp gain the instant a transient arrived, which
sounds like distortion (a hard corner in the gain). This limiter instead
*delays the audio* by a short ``lookahead`` window and watches the
un-delayed signal, so by the time a loud sample reaches the output the
gain has already been eased down to meet it. The descent is spread as a
linear ramp across the lookahead window, landing exactly on the peak —
no corner, no overshoot. After the peak passes, gain recovers with a
one-pole ``release``.

Because the audio is delayed, the limiter has a **fixed latency equal to
the lookahead** (``round(lookahead_ms * sr / 1000)`` samples). That delay
is constant for a given ``lookahead`` and independent of block size, so a
dry/wet parallel path elsewhere in the rack can be compensated by the
same amount.

**Neutral.** When the signal never reaches the ceiling the gain is
exactly 1.0 and the output is the input, delayed by the lookahead and
otherwise untouched — a bit-exact (delayed) passthrough.

Use cases:
  * Master bus safety: drop one in front of the speaker so a patch can be
    pushed loud without ever clipping the output.
  * Transparent level maximisation: pull ``ceiling`` down a dB, drive the
    input harder, and the loudness comes up while the peaks stay put.
  * Taming a spiky source (a resonant filter, a hot sample) before it
    hits a downstream stage that assumes headroom.

Ports:
  * ``in`` (audio): the signal to limit. Unpatched -> silence out.
  * ``out`` (audio): the limited signal, delayed by ``lookahead``.

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` input ->
  ``(F,)`` out through one detector + gain envelope + delay line; a
  voice-aware ``(V, F)`` input -> ``(V, F)`` out with per-voice state, so
  each voice is limited independently (no cross-voice ducking). A single
  voice row is bit-identical to the mono path.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Limiter(Module):
    """Brickwall lookahead peak limiter (see module docstring).

    Parameters:
        ceiling: Absolute output ceiling in dBFS (−20..0). The output
            peak never exceeds this. −1 leaves a hair of headroom.
        release: One-pole recovery time in ms (20..1000) — how fast the
            gain returns to unity after a peak eases off. Short = lively
            but can pump; long = smooth and transparent.
        lookahead: Attack window in ms (1..10). The gain ramp is spread
            across this window so it lands on the peak, and it sets the
            module's fixed processing latency.

    Ports:
        in (in, audio): signal to limit. Unpatched -> silence.
        out (out, audio): limited signal, delayed by ``lookahead``.
    """

    TYPE = "limiter"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "ceiling": -1.0,
        "release": 80.0,
        "lookahead": 5.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
    ]
