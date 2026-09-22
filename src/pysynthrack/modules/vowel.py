"""Vowel — a formant filter: five resonances shaped like a sung vowel.

A voice is a buzz (the vocal folds) through a shape (the throat and
mouth), and the shape is what makes a vowel: a handful of resonances —
*formants* — at frequencies the mouth's geometry sets. Move the mouth and
the formants move; that is the whole difference between "ah" and "ee".
This module is the shape without the buzz: five parallel resonant
bandpasses at the formant frequencies of a sung vowel, applied to
whatever you feed it — a [`supersaw`](#supersaw) becomes a choir, a
[`noise`](#noise) a whisper, a drum loop something that talks. The
[`vocoder`](#vocoder) measures a shape from a real voice; this one
carries the shapes already, and lets you slide between them.

The shapes are the classic five-formant table — frequency, level and
bandwidth of F1–F5 for A, E, I, O, U, for five voice types (soprano,
alto, countertenor, tenor, bass) — the same numbers every formant
synthesizer and the Csound manual carry. ``vowel`` is a continuous 0..4
knob: 0 is A, 1 E, 2 I, 3 O, 4 U, and 1.5 is halfway from E to I —
frequencies interpolate geometrically, bandwidths linearly, levels in
dB. ``vowel_cv`` moves it (``cv_depth`` vowels per unit, read per block —
or per SAMPLE at ``cv_rate`` ``sample``), so a slow LFO is the
talking-filter cliché, an envelope is a mouth opening on every note and
a 30 Hz one is a robot. ``voice`` picks the table (or ``custom``, five
formant frequencies of your own); ``resonance``
multiplies every formant's Q (1 = the table's bandwidths, higher =
narrower, more vowel, more ring); ``gain`` is makeup (a formant bank
passes only what sits near its peaks, so the wet is ~15 dB down on a
saw); ``mix`` blends the dry in, and at 0 the filter is not run at all
— the effects neutral, bit-exact dry.

``formant`` is the throat size — the child / giant knob. Every formant
frequency is multiplied by ``2 ** (formant / 12)``: up, the mouth gets
smaller (a child at +12, a chipmunk at +24); down, larger (a giant at
−12). The bandwidths scale by the SAME ratio, so each formant's Q
(F/BW) — its resonant character — is preserved: a constant-Q shift is
"the same vowel, a different throat", where a constant-bandwidth shift
would sharpen the resonances going up and blur them going down (a
60 Hz band is a fifth of a 300 Hz formant but a fortieth of a 2400 Hz
one). ``formant_cv`` moves it — ``formant_cv_depth`` octaves per unit,
block mean like ``vowel_cv`` — so a slow bipolar LFO at depth 1 grows
the voice from a giant to a child and back, and the effective shift is
``formant/12 + depth × mean cv`` octaves, clipped to ±4 before the
power (the house overflow guard). A formant pushed past 0.45 × the
sample rate just parks there (its bandwidth keeps scaling, so a parked
formant is a little broader than the table's Q would make it — it is
out past 19 kHz anyway). At ``formant`` 0 with nothing on the jack the
ratio is exactly 1 and the render is bit-identical to the unshifted
filter.

``cv_rate`` is how often ``vowel_cv`` is read. ``block`` (the default)
takes the block's mean: one vowel per buffer, cheap, and bit-exact at
any block size. ``sample`` follows the CV per SAMPLE — the audio-rate
mouth, where a 30 Hz LFO on the jack is a buzzing ring-mod-ish vowel
instead of a smear. Rebuilding five biquads every sample is not
affordable, so the modulation is QUANTISED: the CV's contribution is
rounded to 0.02-vowel steps around the knob, the block is split into
runs where that rounded vowel is constant, and each run is one
``lfilter`` call with the filter state carried across the seam. The
step was picked by measurement — 0.02 vowels is about 1% of a formant
frequency (17 cents) and sits 62 dB below a true per-sample rebuild on
a 30 Hz sweep, where 0.1 vowels is only 43 dB down and audibly steppy.
Because the grid is relative to the knob, the knob itself is always
exact: with the jack idle (or ``cv_depth`` 0) ``sample`` is the
``block`` render bit for bit, and nothing clicks at the seams because
the biquad state is carried, not reset. It costs what it costs — a
30 Hz sweep is ~70 runs per 512-sample block, about 0.6x realtime for
this module alone against 0.01x for ``block``, and past roughly 100 Hz
of modulation every sample is its own run.

``voice`` ``custom`` is your own mouth. ``f1``..``f5`` (Hz) replace the
table's five formant FREQUENCIES; the bandwidths and levels stay the
TENOR table's at the current ``vowel`` position, which is what the knob
then morphs. So a custom voice is a fixed formant chord and ``vowel``
shapes its resonances rather than moving them — five parallel
resonators you tune by hand, with ``resonance`` as the global Q knob
and ``formant`` still scaling frequency and bandwidth together. The
bandwidths being the table's in HZ (not scaled to your frequencies)
means a formant dragged up gets relatively narrower and one dragged
down relatively broader; ``resonance`` is the knob for that. The
defaults are tenor A — 650 / 1080 / 2650 / 2900 / 3250 — so ``custom``
starts out sounding like the tenor's A.

Voice-aware like [`filter`](#filter): a ``(V, F)`` input gives ``(V, F)``
out with one filter state per voice row; a single voice row is
bit-identical to mono. Coefficients are rebuilt only when the effective
vowel, the voice type, the resonance or the formant ratio changes;
between changes the five biquads carry their state across blocks, so a
render is block-size independent at a constant vowel and shift.

Ports:
  * ``in`` (audio): the source. Unpatched → silence.
  * ``vowel_cv`` (cv): adds ``cv_depth`` × CV to ``vowel`` — the block
    mean at ``cv_rate`` ``block``, per sample (quantised to 0.02-vowel
    steps) at ``sample``.
  * ``formant_cv`` (cv): adds ``formant_cv_depth`` × mean CV octaves to
    the formant shift per block.
  * ``out`` (audio): the vowel.

Params:
  * ``vowel``: 0..4 — A, E, I, O, U and everything between. Default 0.
  * ``voice``: soprano / alto / countertenor / tenor / bass. Default tenor.
  * ``resonance``: Q multiplier on every formant, 0.25..4. Default 1.
  * ``gain``: makeup in dB, −12..24. Default 6.
  * ``mix``: dry/wet, 0..1. Default 1 (0 = bit-exact dry).
  * ``cv_depth``: vowels per CV unit on ``vowel_cv``. Default 2.
  * ``cv_rate``: block / sample — how often ``vowel_cv`` is read.
    Default block (the cheap, bit-exact one).
  * ``f1``..``f5``: the ``custom`` voice's five formant frequencies in
    Hz, 50..8000. Ignored unless ``voice`` is ``custom``. Defaults
    650 / 1080 / 2650 / 2900 / 3250 (tenor A).
  * ``formant``: throat size in semitones, −24..24 (up = smaller, a
    child; down = larger, a giant). Default 0 (bit-exact unshifted).
  * ``formant_cv_depth``: octaves per CV unit on ``formant_cv``.
    Default 1 (1 V/oct).
"""
from __future__ import annotations

import math

from ..core.module import Module, register_module_type
from ..core.port import Port

VOWEL_NAMES = ("a", "e", "i", "o", "u")
#: The voice types the formant table carries.
VOWEL_VOICES = ("soprano", "alto", "countertenor", "tenor", "bass")
#: What the ``voice`` combo offers: the table's voices plus ``custom``,
#: which takes its frequencies from the ``f1``..``f5`` params instead.
VOWEL_VOICE_CUSTOM = "custom"
VOWEL_VOICE_CHOICES = VOWEL_VOICES + (VOWEL_VOICE_CUSTOM,)
#: How often ``vowel_cv`` is read. ``block`` is the block mean (the
#: shipped behaviour, bit-exact at any block size); ``sample`` follows
#: the CV per sample, quantised into runs of constant vowel.
VOWEL_CV_RATES = ("block", "sample")
#: The ``custom`` voice's default formant frequencies: tenor A.
VOWEL_CUSTOM_DEFAULT_FREQS = (650.0, 1080.0, 2650.0, 2900.0, 3250.0)
#: The custom formant frequency knobs' bounds, Hz.
VOWEL_CUSTOM_FREQ_MIN = 50.0
VOWEL_CUSTOM_FREQ_MAX = 8000.0

#: The classic five-formant table: per voice type, per vowel,
#: ``(frequencies Hz, levels dB, bandwidths Hz)`` for F1..F5.
FORMANTS = {
    "soprano": {
        "a": ((800, 1150, 2900, 3900, 4950), (0, -6, -32, -20, -50), (80, 90, 120, 130, 140)),
        "e": ((350, 2000, 2800, 3600, 4950), (0, -20, -15, -40, -56), (60, 100, 120, 150, 200)),
        "i": ((270, 2140, 2950, 3900, 4950), (0, -12, -26, -26, -44), (60, 90, 100, 120, 120)),
        "o": ((450, 800, 2830, 3800, 4950), (0, -11, -22, -22, -50), (70, 80, 100, 130, 135)),
        "u": ((325, 700, 2700, 3800, 4950), (0, -16, -35, -40, -60), (50, 60, 170, 180, 200)),
    },
    "alto": {
        "a": ((800, 1150, 2800, 3500, 4950), (0, -4, -20, -36, -60), (80, 90, 120, 130, 140)),
        "e": ((400, 1600, 2700, 3300, 4950), (0, -24, -30, -35, -60), (60, 80, 120, 150, 200)),
        "i": ((350, 1700, 2700, 3700, 4950), (0, -20, -30, -36, -60), (50, 100, 120, 150, 200)),
        "o": ((450, 800, 2830, 3500, 4950), (0, -9, -16, -28, -55), (70, 80, 100, 130, 135)),
        "u": ((325, 700, 2530, 3500, 4950), (0, -12, -30, -40, -64), (50, 60, 170, 180, 200)),
    },
    "countertenor": {
        "a": ((660, 1120, 2750, 3000, 3350), (0, -6, -23, -24, -38), (80, 90, 120, 130, 140)),
        "e": ((440, 1800, 2700, 3000, 3300), (0, -14, -18, -20, -20), (70, 80, 100, 120, 120)),
        "i": ((270, 1850, 2900, 3350, 3590), (0, -24, -24, -36, -36), (40, 90, 100, 120, 120)),
        "o": ((430, 820, 2700, 3000, 3300), (0, -10, -26, -22, -34), (40, 80, 100, 120, 120)),
        "u": ((370, 630, 2750, 3000, 3400), (0, -20, -23, -30, -34), (40, 60, 100, 120, 120)),
    },
    "tenor": {
        "a": ((650, 1080, 2650, 2900, 3250), (0, -6, -7, -8, -22), (80, 90, 120, 130, 140)),
        "e": ((400, 1700, 2600, 3200, 3580), (0, -14, -12, -14, -20), (70, 80, 100, 120, 120)),
        "i": ((290, 1870, 2800, 3250, 3540), (0, -15, -18, -20, -30), (40, 90, 100, 120, 120)),
        "o": ((400, 800, 2600, 2800, 3000), (0, -10, -12, -12, -26), (70, 80, 100, 130, 135)),
        "u": ((350, 600, 2700, 2900, 3300), (0, -20, -17, -14, -26), (40, 60, 100, 120, 120)),
    },
    "bass": {
        "a": ((600, 1040, 2250, 2450, 2750), (0, -7, -9, -9, -20), (60, 70, 110, 120, 130)),
        "e": ((400, 1620, 2400, 2800, 3100), (0, -12, -9, -12, -18), (40, 80, 100, 120, 120)),
        "i": ((250, 1750, 2600, 3050, 3340), (0, -30, -16, -22, -28), (60, 90, 100, 120, 120)),
        "o": ((400, 750, 2400, 2600, 2900), (0, -11, -21, -20, -40), (40, 80, 100, 120, 120)),
        "u": ((350, 600, 2400, 2675, 2950), (0, -20, -32, -28, -36), (40, 80, 100, 120, 120)),
    },
}

N_FORMANTS = 5


def vowel_formants(voice: str, vowel: float, custom_freqs=None):
    """The five formants for a continuous ``vowel`` position 0..4.

    Interpolates between the two neighbouring table rows: frequencies
    geometrically (a formant slides in pitch, not in Hz), bandwidths
    linearly, levels linearly in dB. Returns ``(freqs, gains, bws)`` as
    lists of five floats — gains LINEAR (the dB already undone). Out-of-
    range positions clamp, so a CV past U holds U; an unknown voice
    falls back to ``tenor`` — and so does ``custom``, which is not in
    the table.

    ``custom_freqs``, when given, REPLACES the five interpolated
    frequencies (the caller passes the ``f1``..``f5`` params for the
    ``custom`` voice). The bandwidths and levels still come from the
    table — tenor's, since ``custom`` falls back to it — so ``vowel``
    goes on morphing the resonances of a formant chord whose pitches
    are yours. With ``custom_freqs`` None the helper is exactly what it
    always was.
    """
    table = FORMANTS.get(voice, FORMANTS["tenor"])
    v = min(float(len(VOWEL_NAMES) - 1), max(0.0, float(vowel)))
    i0 = min(len(VOWEL_NAMES) - 2, int(math.floor(v)))
    t = v - i0
    fa, da, ba = table[VOWEL_NAMES[i0]]
    fb, db, bb = table[VOWEL_NAMES[i0 + 1]]
    freqs, gains, bws = [], [], []
    for k in range(N_FORMANTS):
        if custom_freqs is None:
            freqs.append(math.exp((1.0 - t) * math.log(fa[k]) + t * math.log(fb[k])))
        else:
            freqs.append(float(custom_freqs[k]))
        gains.append(10.0 ** (((1.0 - t) * da[k] + t * db[k]) / 20.0))
        bws.append((1.0 - t) * ba[k] + t * bb[k])
    return freqs, gains, bws


@register_module_type
class Vowel(Module):
    """Formant filter: five resonances shaped like a sung vowel, morphable.

    Parameters:
        vowel: 0..4 — A, E, I, O, U and everything between. Default 0.
        voice: Table — soprano / alto / countertenor / tenor / bass, or
            ``custom`` (frequencies from ``f1``..``f5``). Default tenor.
        resonance: Q multiplier on every formant, 0.25..4. Default 1.
        gain: Makeup in dB, −12..24. Default 6.
        mix: Dry/wet, 0..1 (0 = bit-exact dry). Default 1.
        cv_depth: Vowels per CV unit on ``vowel_cv``. Default 2.
        cv_rate: ``block`` (the block mean, bit-exact) or ``sample``
            (per-sample, quantised to 0.02-vowel runs). Default block.
        f1..f5: The ``custom`` voice's formant frequencies in Hz,
            50..8000. Ignored for the table voices. Defaults tenor A.
        formant: Throat size in semitones, −24..24 — every formant's
            frequency and bandwidth × ``2 ** (formant / 12)`` (Q kept).
            Default 0.
        formant_cv_depth: Octaves per CV unit on ``formant_cv``.
            Default 1.

    Ports:
        in (in, audio): the source (voice-aware).
        vowel_cv (in, cv): moves ``vowel`` — block mean, or per sample
            at ``cv_rate`` ``sample``.
        formant_cv (in, cv): moves the formant shift, block mean.
        out (out, audio): the vowel.
    """

    TYPE = "vowel"
    CATEGORY = "Filters & EQ"
    DEFAULT_PARAMS = {
        "vowel": 0.0,
        "voice": "tenor",
        "resonance": 1.0,
        "gain": 6.0,
        "mix": 1.0,
        "cv_depth": 2.0,
        "cv_rate": "block",
        "formant": 0.0,
        "formant_cv_depth": 1.0,
        "f1": VOWEL_CUSTOM_DEFAULT_FREQS[0],
        "f2": VOWEL_CUSTOM_DEFAULT_FREQS[1],
        "f3": VOWEL_CUSTOM_DEFAULT_FREQS[2],
        "f4": VOWEL_CUSTOM_DEFAULT_FREQS[3],
        "f5": VOWEL_CUSTOM_DEFAULT_FREQS[4],
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("vowel_cv", "in", "cv"),
        Port("formant_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
