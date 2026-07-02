"""TiltEQ — a one-knob spectral seesaw: tilt the whole balance with CV.

The third of the animated-EQ trio, and the simplest: one control tilts
the entire spectral balance about a pivot frequency. Positive tilt
boosts the lows and cuts the highs by the same amount (warmer/darker);
negative tilt does the opposite (brighter/thinner). ``tilt_cv`` drives
it, so an LFO makes the sound breathe dark<->bright, an envelope (via
:class:`AudioToCV`) opens the top end with playing dynamics, and a
:class:`Sequencer` lane steps the timbre per note — voltage-controlled
brightness, one wire.

Where the trio sits:
  * :class:`SweepEQ` — one resonant band whose *position* moves
    (auto-wah).
  * :class:`MotionEQ` — four peaking bells, each centre CV-swept.
  * TiltEQ — no bands at all: the *whole spectrum* seesaws about
    ``pivot``. Reach for it when you want "more bass / less treble" as a
    single modulatable dimension rather than surgical bands.

Implementation is two opposed RBJ shelving biquads about ``pivot`` —
exactly the :class:`Loudness` module's shelf pair, but cornered at the
same frequency with mirrored gains (lows ``+g`` dB, highs ``-g`` dB), so
the response passes through ~0 dB at the pivot and tilts smoothly either
side. Hardware convention (Tonelux-style): ``tilt`` in dB is what the
lows gain and the highs lose, so the total low<->high spread is twice
the knob. At an effective tilt of 0 dB both shelves are identity and the
module is a bit-exact passthrough.

CV: effective tilt is ``tilt + cv_depth * mean(tilt_cv)`` dB — summed in
dB space, block-meaned like the Crossover / mod-FX (one coefficient set
per block, shared across voices), clamped to +/-18 dB. As the CV rises
the lows boost and the highs cut.

Shape-polymorphic (mono ``(F,)`` and per-voice ``(V, F)``, each voice
its own biquad memory); a single voice row is bit-identical to mono. The
tilt is one global control — a ``(V, F)`` ``tilt_cv`` is averaged, like
the Loudness level.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class TiltEQ(Module):
    """A CV-controlled tilt EQ (bass<->treble seesaw about a pivot).

    Parameters:
        pivot: Pivot frequency in Hz — the point the balance seesaws
            about (response stays ~0 dB there). Clamped to (20 Hz,
            0.45 * sample_rate) by the renderer.
        tilt: Static base tilt in dB. Positive = lows up, highs down
            (warmer); negative = brighter. 0 = flat (bit-exact
            passthrough with no CV).
        cv_depth: dB of tilt per unit of ``tilt_cv``. Default 6.0, so a
            bipolar LFO at full depth seesaws +/-6 dB.

    Ports:
        in (in, audio): the signal to tilt. Unpatched -> silence.
        tilt_cv (in, cv): added to ``tilt``, scaled by ``cv_depth``.
            Optional.
        out (out, audio): the tilted signal.
    """

    TYPE = "tilt_eq"
    DEFAULT_PARAMS = {
        "pivot": 1000.0,
        "tilt": 0.0,
        "cv_depth": 6.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("tilt_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
