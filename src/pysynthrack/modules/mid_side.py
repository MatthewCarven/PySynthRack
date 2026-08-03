"""MidSide — M/S encode/decode + stereo width, all outs live.

The missing stereo utility beside :class:`StereoSpeakerOutput`: feed a
stereo pair into ``in_l``/``in_r`` and every view is available at once
— ``mid``/``side`` are the M/S encode (process them separately and
recombine however you like), ``out_l``/``out_r`` are the decode with a
``width`` knob in the middle. No mode combo; unused jacks cost
nothing.

The math is the standard sum/difference pair:

    M = (L + R) / 2        L' = M + w·S
    S = (L − R) / 2        R' = M − w·S

``width`` 0..2: 0 collapses to dual mono (``out_l ≡ out_r ≡ mid``),
1 is unity (decode ≡ input, bit-close), 2 doubles the side signal —
wider than the room. ``width_cv`` adds per sample (final width clamped
0..2), so an LFO breathes the stereo field and an envelope can duck
width on transients.

Mono-friendly: with only ``in_l`` patched (or only ``in_r``), the one
input **is** the mid (``S = 0``, level preserved — not halved by the
missing channel) and width is inert; the module passes through. Both
unpatched → silence. Stateless; audio inputs are mono consumers (a
voice-aware source collapses on fetch, house convention).

Ports:
  * ``in_l``, ``in_r`` (audio, in): the stereo pair (either alone =
    mono passthrough).
  * ``width_cv`` (cv, in): added to ``width`` per sample.
  * ``mid``, ``side`` (audio, out): the M/S encode.
  * ``out_l``, ``out_r`` (audio, out): the width-processed decode.

Params:
  * ``width``: side gain in the decode, 0..2. Default 1.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class MidSide(Module):
    """M/S encode/decode + width: mid/side and out_l/out_r, all live.

    Parameters:
        width: Side gain in the decode, 0..2 (0 = mono, 1 = unity,
            2 = extra wide). ``width_cv`` adds, clamped 0..2. Default 1.

    Ports:
        in_l (in, audio): left (alone = mono passthrough).
        in_r (in, audio): right.
        width_cv (in, cv): per-sample width modulation.
        mid (out, audio): (L + R) / 2.
        side (out, audio): (L − R) / 2.
        out_l (out, audio): mid + width·side.
        out_r (out, audio): mid − width·side.
    """

    TYPE = "mid_side"
    CATEGORY = "Routing & VCA"
    DEFAULT_PARAMS = {
        "width": 1.0,
    }
    INPUT_PORTS = [
        Port("in_l", "in", "audio"),
        Port("in_r", "in", "audio"),
        Port("width_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("mid", "out", "audio"),
        Port("side", "out", "audio"),
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
