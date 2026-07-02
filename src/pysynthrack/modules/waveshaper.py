"""Waveshaper — a wavefolder: bend the waveform back on itself.

The west-coast sibling of the Distortion pedal. Where distortion
*flattens* a waveform against the rails (harmonics from squashing),
a folder *reflects* it — signal that would exceed the rails folds back
toward zero, and keeps folding as you push harder. The timbre change is
much more dramatic: a plain sine through a rising ``fold`` sweeps from
pure tone through nasal, brassy, metallic, to shimmering comb-like
spectra. This is the classic Buchla/Serge way to build complex timbres
from simple sources — start with a sine, fold, filter.

``fold`` is the push into the folder (1 = just touching the rails for a
full-scale signal; higher = more folds). ``mode`` picks the reflection:

* ``triangle`` — hard geometric reflection at ±1 (Serge-ish, bright,
  exact: below the rails the signal passes through *unchanged*).
* ``sine`` — the transfer curve is a sine (Buchla-ish, rounder: the
  fold points are smooth rather than creased, and the curve gently
  colours the signal even below the rails).

``symmetry`` slides the signal off-centre before folding, so the top
folds before the bottom — even harmonics, growl, and movement when you
modulate it (the DC this creates is blocked internally). ``fold_cv``
modulates the fold amount per sample — a slow LFO here is *the*
wavefolder patch: timbre that breathes. ``mix`` blends dry back in
(0 = bit-exact passthrough).

**Oversampled 4×** like the Distortion: folding is savagely bright, and
without oversampling the upper folds alias into inharmonic hash. Same
streaming polyphase pair, same ~16-sample (~0.4 ms) compensated
latency.

Use it: sine or triangle oscillator in, ``fold`` 2–8, LFO or envelope
on ``fold_cv``, low-pass after to taste. Feed it the noise source for
texture instead of pitch.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Fold flavours. ``triangle`` reflects hard at the rails (bright,
# passthrough-exact below them); ``sine`` folds along a sine transfer
# curve (rounder, always colours).
WAVESHAPER_MODES = ("triangle", "sine")


@register_module_type
class Waveshaper(Module):
    """Wavefolder (triangle / sine), 4x oversampled.

    Parameters:
        fold: Fold amount (0..16). 1 = a full-scale signal just reaches
            the rails (triangle mode passes it unchanged); higher folds
            it back repeatedly.
        symmetry: Pre-fold offset (-1..1). Off-centre folding adds even
            harmonics; the resulting DC is blocked internally.
        mode: ``triangle`` (hard reflection) or ``sine`` (smooth fold).
        mix: Dry/wet. 0 = bit-exact passthrough.
        cv_depth: Fold units added per unit of ``fold_cv``.

    Ports:
        in (audio): signal to fold. Unpatched -> silence.
        fold_cv (cv): per-sample fold modulation, scaled by
            ``cv_depth``. Optional.
        out (audio): the folded signal.
    """

    TYPE = "waveshaper"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "fold": 1.0,
        "symmetry": 0.0,
        "mode": "triangle",
        "mix": 1.0,
        "cv_depth": 4.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio"), Port("fold_cv", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
