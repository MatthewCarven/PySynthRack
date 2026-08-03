"""Pluck — an extended Karplus–Strong plucked string voice.

The classic: a delay line the length of one pitch period, fed back through
a gentle lowpass, excited by a short noise burst — physics-adjacent string
synthesis for the price of a ring buffer. Each ``trigger`` rising edge
plucks the string at the pitch on ``pitch_cv`` (1 V/oct, C4 = 0 V, the
``fm_op`` convention); polyphonic sources give **one independent string
per voice** — a ``cv_keyboard`` or MIDI keyboard turns it into a 16-string
instrument with zero extra patching.

The extensions over textbook KS:

  * **In-tune across the range** — the loop uses an allpass fractional
    delay plus compensation for the damping filter's phase delay, so the
    pitch lands within a few cents rather than quantizing to whole-sample
    loop lengths (the naive KS goes audibly sharp/flat above ~500 Hz).
  * **``decay`` in seconds** — the loop gain is derived from the pitch so
    ``decay`` reads as a real t60: a 2 s setting rings ~2 s whether you
    pluck C2 or C6 (textbook KS rings longer the lower the note).
  * **``damping``** — how dark the string is: 0 leaves the loop wide open
    (bright, wiry, long high partials), 1 is the full classic two-point
    average (each pass rolls off highs — nylon-ish).
  * **``color``** — the exciter's spectrum, noise → pick: 0 lowpasses the
    burst (soft thumb), 1 injects raw white noise (hard plectrum).
  * **``position``** — pick position as a comb on the exciter: the burst
    is delayed-and-subtracted by ``position``·period, notching the
    harmonics a real pluck at that spot along the string cancels. 0
    disables the comb.

Re-plucking a ringing string **adds** the new burst into the loop instead
of replacing it — physical (the string was still moving) and click-free.
Every burst is seeded from (module, voice, hit number), so renders are
deterministic and testable sample-for-sample.

Voice-awareness: shape follows the inputs — mono ``(F,)`` in gives mono
out, ``(V, F)`` gives per-voice strings with no crosstalk. Silent voices
early-out (a decayed string costs nothing until re-plucked). Pitch is
read per block (mean), so slides/vibrato track at block rate; the pluck
pitch itself is locked from the trigger sample. Numpy backend only;
silent stub under pyo.

Ports:
  * ``pitch_cv`` (cv): 1 V/oct pitch, C4 = 0 V. Unpatched → C4.
  * ``trigger`` (gate): pluck on each rising edge. No trigger, no sound.
  * ``out`` (audio): the string.

Params:
  * ``decay``: t60-style ring time in seconds, 0.1..30. Default 2.
  * ``damping``: loop lowpass blend 0..1. Default 0.5.
  * ``color``: exciter spectrum, 0 soft .. 1 bright. Default 0.7.
  * ``position``: pick-position comb, 0 off .. 1. Default 0.2.
  * ``level``: output level 0..1. Default 0.5.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Pluck(Module):
    """Karplus–Strong string: noise burst into a tuned feedback loop.

    Parameters:
        decay: Ring time (t60-ish) in seconds, 0.1..30. Default 2.
        damping: Loop lowpass blend, 0 (bright/wiry) .. 1 (classic
            two-point average, nylon-ish). Default 0.5.
        color: Exciter spectrum, 0 (lowpassed thumb) .. 1 (white-noise
            plectrum). Default 0.7.
        position: Pick-position comb on the exciter (fraction of the
            period); 0 disables. Default 0.2.
        level: Output level. Default 0.5.

    Ports:
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        trigger (in, gate): pluck per rising edge (per voice).
        out (out, audio): the string.
    """

    TYPE = "pluck"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "decay": 2.0,
        "damping": 0.5,
        "color": 0.7,
        "position": 0.2,
        "level": 0.5,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("trigger", "in", "gate"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
