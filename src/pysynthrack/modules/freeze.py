"""Freeze — a spectral freeze: hold a moment's spectrum as a pad, forever.

The rack already has a freeze that loops TIME: [`granular`](#granular)
re-reads a slice of its ring, so what you hold is a rhythm, a stutter,
a cloud of grains. This one holds a SPECTRUM. At the rising edge of
``freeze`` it takes the last ``size`` samples of the input, measures
the frequencies present and how loud each is, and then re-synthesises
that spectrum indefinitely with every partial's phase advancing at its
own true rate — a chord becomes a stationary pad with no loop seam and
no rhythm in it at all. It is the sound of the Freeze pedal, of the
spectral mode on a Clouds, of a sustain that never has to breathe. The
dry keeps passing at ``dry``: a freeze is something you play OVER, so
engaging it never ducks what you are playing.

``smear`` is the character knob. At 0 the hold is the coherent
phase-vocoder freeze: a sine freezes to the same sine, a chord to the
same chord, glassy and still. At 1 every synthesis frame gets random
phases — the identity of the sound (its spectrum) stays, its coherence
goes, and the hold becomes the classic spectral wash. ``pitch``
transposes the frozen layer (``pitch_cv`` at 1 V/oct); because the
frozen stream is stationary, the shift is a plain change of playback
rate — exact frequency, unity level, no formant tricks. ``size`` is the
resolution knob: bigger windows capture a longer moment (93 ms at 4096,
372 ms at 16384) and hold close harmony cleanly; partials closer than
about four bins (``4 * sr / size`` Hz — 43 Hz at 4096) fight for the
same bins and the hold loses them, which is why the default is 4096
and a triad at 1024 vanishes. ``fade`` is the layer's rise at the
edge, its fall at release, and the crossfade when you freeze again
while a hold is still sounding — a chord change under a held pedal
melts from the old chord into the new, it never cuts.

``width`` (love pass) opens the hold into stereo on ``out_l`` /
``out_r`` without touching what it is made of. The two channels are
the same frozen stream with every partial's phase rotated apart — a
quarter turn between L and R at ``width`` 1, the sign alternating
partial by partial — a *quadrature* scatter, one constant per spectral
peak region, fixed for the life of the layer. One phase per region
rather than per bin, because a partial's lobe spans four bins and
scattering those against each other changes the partial's level (that
is what ``smear`` does); a rotation of the whole lobe is a pure phase
shift, so each channel keeps every partial's level exactly and sounds,
alone, like the mono hold. Deterministic rather than random so the
numbers are laws, not luck: the L/R correlation is ``cos(width·π/2)``
(0 at ``width`` 1), and the mono fold ``(L + R)/2`` is the mono hold
itself at ``cos(width·π/4)`` — −3 dB at ``width`` 1, never a partial
cancelled. ``out`` stays the untouched mono: the pad on ``out`` is not
changed by ``width`` at all. A lone sine cannot be widened, only
turned; width is for chords and washes. ``smear``'s per-frame jitter is
the same for both channels, on top of the scatter.

``decay`` (love pass) lets the hold fade by itself — the layer's level
falls by 60 dB in ``decay`` seconds (``10^(−3t/decay)``, a per-sample
factor from the integer sample count since the layer was born, so it
is bit-exact at any block size), on top of the gate's ``fade``, and a
layer that has fallen below −90 dB is dropped even while the gate is
held high, so nothing runs for free. A re-freeze starts the new layer
at full level; a knob turn mid-hold continues the fall from the level
the layer has (never a jump); 0 = forever, as before.

Ports:
  * ``in`` (audio): the source. Mono — a ``(V, F)`` source is the house
    sum. Unpatched → silence, no state.
  * ``freeze`` (gate): rising edge captures, high holds, the fall
    releases. A ``(V, F)`` gate collapses to any-voice-high. ORed with
    the ``freeze`` tickbox.
  * ``pitch_cv`` (cv): 1 V/oct × ``pitch_cv_depth`` on the frozen layer,
    read per block.
  * ``out`` (audio): ``in * dry + frozen * level`` — the mono, untouched
    by ``width``.
  * ``out_l`` / ``out_r`` (audio): the stereo pair, ``in * dry + frozen_c
    * level``; at ``width`` 0 they are ``out`` itself, bit-exact.

Params:
  * ``size``: FFT window in samples, 1024 | 2048 | 4096 | 8192 | 16384.
    Default 4096.
  * ``freeze``: tickbox — hold from the panel. Default off.
  * ``smear``: 0 = coherent hold, 1 = random-phase wash. Default 0.
  * ``pitch``: transposition of the frozen layer in semitones, -24..24.
    Default 0.
  * ``pitch_cv_depth``: octaves per unit on ``pitch_cv``. Default 1.
  * ``level``: the frozen layer's level, 0..1. Default 0.7.
  * ``dry``: the live input's level, 0..1. Default 1 (unfrozen, the
    input passes bit-exactly).
  * ``fade``: rise / fall / crossfade time in ms, 1..2000. Default 60.
  * ``seed``: the smear's die. Default 1.
  * ``width``: the stereo scatter on ``out_l`` / ``out_r``, 0..1.
    Default 0 (mono).
  * ``decay``: the hold's own fade to −60 dB, in seconds; 0 = forever.
    Default 0.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

#: The FFT windows offered, in samples. Bigger = a longer moment captured
#: and cleaner close harmony; smaller = a snappier grab.
FREEZE_SIZES = (1024, 2048, 4096, 8192, 16384)


@register_module_type
class Freeze(Module):
    """Spectral freeze: hold a moment's spectrum as a pad (see the module
    docstring for the how and why).

    Parameters:
        size: FFT window in samples (one of ``FREEZE_SIZES``). Default 4096.
        freeze: Tickbox hold, ORed with the gate. Default False.
        smear: 0 = coherent phase-vocoder hold, 1 = random-phase wash.
            Default 0.
        pitch: Frozen-layer transposition in semitones, -24..24. Default 0.
        pitch_cv_depth: Octaves per unit on ``pitch_cv``. Default 1.
        level: Frozen-layer level, 0..1. Default 0.7.
        dry: Live-input level, 0..1. Default 1.
        fade: Rise / fall / crossfade in ms, 1..2000. Default 60.
        seed: The smear's die. Default 1.
        width: Quadrature stereo scatter on ``out_l`` / ``out_r``, 0..1.
            Default 0 (the pair is the mono).
        decay: The hold's own fade to −60 dB in seconds; 0 = forever.
            Default 0.

    Ports:
        in (in, audio): the source (mono; a ``(V, F)`` source is summed).
        freeze (in, gate): rising edge captures, high holds.
        pitch_cv (in, cv): 1 V/oct × ``pitch_cv_depth``, block mean.
        out (out, audio): ``in * dry + frozen * level`` (mono).
        out_l / out_r (out, audio): the stereo pair (== ``out`` at
            ``width`` 0).
    """

    TYPE = "freeze"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "size": 4096,
        "freeze": False,
        "smear": 0.0,
        "pitch": 0.0,
        "pitch_cv_depth": 1.0,
        "level": 0.7,
        "dry": 1.0,
        "fade": 60.0,
        "seed": 1,
        "width": 0.0,
        "decay": 0.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("freeze", "in", "gate"),
        Port("pitch_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
