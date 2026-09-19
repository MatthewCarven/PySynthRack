"""Noise — a white / pink / brown / violet noise generator (audio + CV source).

A random-signal source with two output jacks carrying the *same* noise
stream: an ``out`` (audio) jack to drive filters and speakers directly
(snares, hats, wind, breath, surf), and a ``cv`` jack to drive
modulation directly (the textbook random-voltage source — patch it into
:class:`SampleHold`'s ``in`` for stepped random pitches). Two jacks
rather than one bridge: noise is equally at home as audio or as
control, so it's offered as both, the way Keyboard exposes ``out`` and
``gate``.

Colors (param ``color``) — every colour is the one white stream through
a different tilt, RMS-normalised so ``amp`` means the same level for
all of them:
  * ``white`` (default) — flat power across the spectrum; uniform
    ``[-1, 1]`` per sample. Bright, hissy; the raw material for hats
    and the classic S&H source.
  * ``pink`` — equal power *per octave* (−3 dB/oct), the spectral
    tilt of rushing water, rain, and most natural noise. White through
    a 3rd-order pinking IIR (``scipy.signal.lfilter``, filter state
    carried across blocks for spectral continuity). Darker, smoother;
    gentler as a modulation source.
  * ``brown`` (red) — −6 dB/oct, the tilt of surf, thunder and wind
    heard through a wall: a *leaky* integrator of the white (a one-pole
    with a 10 Hz corner, so it rolls off as −6 dB/oct from ~20 Hz up
    without wandering off as DC the way a true random walk would).
    Deep and rumbling; as a CV it is a slow, smooth wander.
  * ``violet`` — +6 dB/oct, the first difference of the white: thin,
    airy hiss with almost nothing below 1 kHz. The mirror of brown;
    useful for breath and cymbal tops.

Param ``amp`` (default 1.0) scales both outputs. White is hard-bounded
to ``±amp``; the filtered colours are RMS-matched to white, so their
occasional peaks run past ``±amp`` (pink and brown to roughly ``2·amp``
over a few seconds, violet to ``1.4·amp`` — normal for filtered noise;
the speaker limiter handles the audio path, and CV destinations are
scaled to taste with :class:`CVScale`/:class:`CVOffset`).

Param ``seed`` (int, default 0) picks the die. ``0`` is free-running:
the stream comes from numpy's global generator and is different every
run (the shipped behaviour). Any other value keys a private generator
held in the module's state, so the stream is *the same every run* and
the same at every buffer size — a seeded snare or a seeded S&H melody
comes back identical after a reload. Two noise modules with the same
seed produce the **same** stream by design (the seed is the whole key,
not seed-plus-module): patch one to a left channel and its twin to the
right for correlated stereo, or give them different seeds for two
independent streams. Changing the seed re-creates the generator.

Input ``amp_cv`` (cv) is a per-sample linear amplitude, knobless by the
house rule (the CV *is* the amplitude, like ``vca.cv``): ``out =
noise · amp · amp_cv[n]``, unpatched = unity. Multiplied *after* the
colour filter, so a fast gate here chops the noise cleanly. A
``(V, F)`` source — a polyphonic ADSR — broadcasts the one noise stream
to ``(V, F)`` with a per-voice level: every voice hears the same noise
under its own envelope (one stream, not V independent ones). A mono CV
keeps the output mono.

Output is otherwise mono ``(frames,)`` — a source has no voice context
of its own (like :class:`Constant`), and a 1D signal broadcasts cleanly
against any per-voice consumer downstream.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


NOISE_COLORS = ("white", "pink", "brown", "violet")


@register_module_type
class Noise(Module):
    """White / pink / brown / violet noise source; ``out`` (audio) + ``cv`` (cv).

    Parameters:
        color: ``"white"`` (flat), ``"pink"`` (−3 dB/oct), ``"brown"``
            (−6 dB/oct) or ``"violet"`` (+6 dB/oct). Default ``"white"``.
        amp: Linear level applied to both outputs. Default 1.0.
        seed: 0 = free-running (numpy's global generator); any other
            int keys a private generator so the stream repeats run to
            run. Default 0.

    Ports:
        amp_cv (in, cv): per-sample linear amplitude (knobless; the CV
            is the level). Unpatched = unity. A ``(V, F)`` source
            broadcasts the one stream to ``(V, F)``.
        out (out, audio): the noise as audio.
        cv (out, cv): the same noise as control voltage.
    """

    TYPE = "noise"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {"color": "white", "amp": 1.0, "seed": 0}
    INPUT_PORTS = [
        Port("amp_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("cv", "out", "cv"),
    ]
