"""Sampler — keyboard-tracked pitched sample playback.

The gap [`file_player`](#file_player) doesn't fill. FilePlayer is a
**transport**: one playhead, one speed, transport buttons — you point it at
a track and it plays. This is a **voice**: per-voice playheads, each running
at a rate set by ``pitch_cv``, started by a ``gate`` edge. Load one
recording, tell it what note the recording *is*, and the rack's 16-slot
voice architecture turns it into a mellotron, a rompler or a breaks machine
without any extra patching — a ``cv_keyboard`` or ``midi_input`` in front
gives you sixteen independent playheads for the price of a cubic read.

``root`` is the whole trick: it says which note the sample was recorded at.
Play that note and the rate is exactly 1.0 — the file comes back untouched.
Play an octave up and it reads every other sample. Everything else is
``tune`` (±12 semitones) and ``fine`` (±50 cents) on top.

``mode`` decides what a gate means:

* ``one_shot`` (default) — a rising edge fires the whole region and the
  gate's length is ignored, the drum-voice idiom. Right for hits, breaks
  and anything you trigger from a [`euclidean`](#euclidean) or a clock.
* ``gated`` — the sample sounds while the gate is high and fades on the
  fall (``release``). Right for played notes on a keyboard.
* ``loop`` — ``gated``, plus the playhead wraps ``loop_end`` back to
  ``loop_start`` instead of running out, so a two-second recording sustains
  for as long as you hold the key. The mellotron mode.

**The loop is where a recording becomes an instrument.** ``loop_start`` and
``loop_end`` are fractions of the *region* (not of the file), so moving
``start``/``end`` carries the loop with them. The playhead starts at
``start`` as always, so everything before ``loop_start`` plays once as the
note's attack and only the loop itself repeats — point ``loop_start`` past
the bow scrape or the mallet strike and you get a natural onset over an
endless sustain.

``loop_xfade`` hides the seam: over the last stretch of the loop the read
crossfades into the *previous lap* — the same signal one loop length
earlier, which is the material running into ``loop_start``. Where a bare
wrap would step from one waveform to an unrelated one (a click), the two
are faded across and arrive at ``loop_start`` already matching. It is
measured on the *sample*, not the clock, so it covers the same slice of
waveform whatever the pitch, and it is clamped to the loop length (you
cannot fade over more than there is). Set it to 0 for a hard seam — right
when the loop is already cut on a zero crossing, and the only way to keep
a loop bit-exact. A ``loop_start`` sitting at the very beginning of the
file has no previous lap to fade into, so the fade runs into the file's
first sample instead: still continuous, since that is exactly where the
wrap lands.

A collapsed or inverted loop region (``loop_end`` at or below
``loop_start``) is not an error and not silence — it simply doesn't loop,
and the voice plays as ``gated``.

**Pitch-up aliases, deliberately — unless you ask it not to.** Reading
faster than 1.0 shifts the whole spectrum up and anything that was near
Nyquist folds. That crunch is the sound of every classic sampler and it is
the default. Tick ``antialias`` and the read comes from an on-load
**mip chain** instead: the loader keeps half-band-filtered, 2:1-decimated
copies of the sample, one per octave, and a read above unity rate takes
the copy whose bandwidth fits the rate (crossfading between the two
nearest octaves so nothing steps as a note slides). At the root and below
it still reads the untouched original, so the neutral stays bit-exact
either way. Pitch *down* is clean in both cases — the 4-tap cubic read is
the resampler's, so non-integer rates stay smooth rather than gritty.

``start_cv`` is what makes a breaks machine. It moves ``start`` by
``start_cv_depth`` of the file per volt, sampled **at the gate edge** —
each hit lands wherever the CV says, so a sequencer or a
[`shift_random`](#shift_random) into it re-slices a loop on every trigger.
The loop region rides along (it is measured from wherever this voice
started), and a start pushed past ``end`` fires nothing rather than
running backwards.

``reverse`` plays the same region backwards: the playhead starts at ``end``
and runs down to ``start``, and a loop wraps the other way. At unity rate
it is an exact mirror of the forward read. ``vel`` is a level multiplier
sampled at the gate edge — patch [`midi_input`](#midi_input)'s
``velocity_cv`` for velocity, or any stepped CV for accents; unpatched it
is 1.

**``attack``/``release`` are declick ramps, not an envelope.** They exist so
a region boundary that lands mid-waveform doesn't click. For real shaping
patch an [`adsr`](#adsr) into a [`vca`](#vca), the way every other source in
the rack expects — raise ``attack`` only if your ``start`` point ticks.

Loading happens on a background thread (the convolver's ``_IRLoader``
precedent), so a fresh or changed ``path`` never blocks the audio thread:
the module is simply silent until the sample is ready, and a missing or
unreadable file stays silent rather than raising — saved patches always
load, whatever became of the audio on disk. ``out`` is the mono sum;
``out_l`` / ``out_r`` carry a stereo file's channels (a mono file feeds
all three identically, so patching only ``out`` costs one read). Long
files are capped (see ``MAX_SECONDS``): the limit is RAM, not DSP — a
minute of mono at 48 kHz is about 11 MB, plus half as much again for the
mip chain and a stereo file's second channel.

Voice-awareness: shape follows the inputs — mono ``(F,)`` in gives mono out,
``(V, F)`` gives per-voice playheads with no crosstalk, and a finished voice
early-outs so it costs nothing until retriggered. Pitch is read per block
(mean), so slides and vibrato track at block rate — the ``pluck``
precedent. Numpy backend only; silent stub under pyo.

Ports:
  * ``pitch_cv`` (cv): 1 V/oct, C4 = 0 V. Unpatched → C4 → the root note.
  * ``gate`` (gate): rising edge starts playback from ``start``.
  * ``start_cv`` (cv): moves ``start`` by ``start_cv_depth`` per unit,
    sampled at the gate edge.
  * ``vel`` (cv): level multiplier, sampled at the gate edge. Unpatched → 1.
  * ``out`` (audio): the voice, mono.
  * ``out_l`` / ``out_r`` (audio): the voice's stereo channels.

Params:
  * ``path``: the sample file. Browse, or type a path.
  * ``root``: which note the recording is, as a MIDI note number
    (60 = C4). Playing this note reads the file at rate 1.0.
  * ``tune`` / ``fine``: ±12 semitones / ±50 cents on top.
  * ``mode``: ``one_shot`` / ``gated`` / ``loop``.
  * ``start`` / ``end``: playback region, 0..1 of the file.
  * ``loop_start`` / ``loop_end``: loop region, 0..1 *of the region*.
  * ``loop_xfade``: seam crossfade in ms (0 = a hard seam).
  * ``attack`` / ``release``: declick ramps in ms.
  * ``level``: output level.
  * ``start_cv_depth``: fraction of the file per CV unit on ``start_cv``.
  * ``reverse``: play the region backwards.
  * ``antialias``: read pitch-up from the mip chain instead of aliasing.
"""
from __future__ import annotations

import math

from ..core.module import Module, register_module_type
from ..core.port import Port

#: Playback modes. ``loop`` is ``gated`` with the playhead wrapping the
#: loop region, which is what turns a finite recording into a sustain.
SAMPLER_MODES = ("one_shot", "gated", "loop")

#: MIDI note the 1 V/oct convention calls 0 V — C4, as everywhere else.
CV_REFERENCE_NOTE = 60

#: ``playback_rate`` clips its octave exponent to +/- this before the
#: power (the backend's ``_OCT_EXP_LIMIT``): far past any playable pitch,
#: so a no-op musically, but it keeps an absurd ``pitch_cv`` from raising
#: OverflowError in the audio thread.
OCT_EXP_LIMIT = 64.0

#: Longest sample kept, in seconds. RAM-bound, not DSP-bound: a minute of
#: mono float64 at 48 kHz is ~23 MB in the buffer. Anything longer is
#: truncated (with a short fade so the cut doesn't click).
MAX_SECONDS = 120.0

#: Root-note combo range, inclusive, as MIDI note numbers (C0..C8).
ROOT_MIN_NOTE = 12
ROOT_MAX_NOTE = 108

#: Mip chain (``antialias``): how many 2:1 octaves the loader builds above
#: the original. Six covers a rate of 64x — five octaves of pitch CV plus
#: the tune knob — and the chain stops early on a short file (a level
#: with fewer than ``MIP_MIN_SAMPLES`` samples is not worth reading).
MIP_LEVELS = 6
MIP_MIN_SAMPLES = 16
#: Taps in the half-band lowpass each level is filtered with before it is
#: decimated. Odd, so the filter is centred and level ``k`` sample ``j``
#: sits exactly on original position ``j * 2**k``.
HALFBAND_TAPS = 63

#: Columns in the waveform overview the loader builds for the node face.
OVERVIEW_COLS = 200


def playback_rate(cv: float, root_note: float, tune: float,
                  fine: float) -> float:
    """Read rate for one voice: 1.0 means "the file, untouched".

    ``cv`` is 1 V/oct with C4 = 0 V, so the played note is
    ``60 + 12·cv``; the rate is two to the power of however many
    semitones that sits above ``root``, plus ``tune`` and ``fine``.

    Kept here, dpg- and numpy-free, so the renderer, the tests and the
    docs can't drift on what "root" means. Playing the root note with no
    tune returns **exactly** 1.0 (the exponent is exactly 0.0), which is
    what makes the module's neutral bit-exact rather than merely close.

    The octave exponent is clipped to +/-``OCT_EXP_LIMIT`` before the
    power and a non-finite ``cv`` reads as 0 -- the backend's
    ``_pow2_clipped`` rule, restated here because this file stays
    numpy-free: a ``constant`` at 1e6 on ``pitch_cv`` used to raise
    OverflowError from this line in the audio thread. A no-op for any
    playable pitch (``min``/``max`` hand the exponent back untouched).
    """
    cv = float(cv)
    if not math.isfinite(cv):
        cv = 0.0
    semitones = (
        (CV_REFERENCE_NOTE + 12.0 * cv) - float(root_note)
        + float(tune) + float(fine) / 100.0
    )
    octaves = min(max(semitones / 12.0, -OCT_EXP_LIMIT), OCT_EXP_LIMIT)
    return 2.0 ** octaves


def mip_blend(rate: float, levels: int) -> tuple[int, int, float]:
    """Which mip levels a read at ``rate`` takes, and how they are mixed.

    Returns ``(k0, k1, frac)``: the read is ``(1 - frac)`` of level ``k0``
    plus ``frac`` of level ``k1``, where level ``k`` is the original
    half-band-filtered and decimated ``k`` times. Level ``k`` holds
    everything below ``Nyquist / 2**k``, so a read at rate ``2**k`` from it
    is alias-free; in between octaves the two nearest levels crossfade by
    the fractional octave so a glide never steps between bandwidths (the
    wavetable_morph rule). At or below unity — and with no chain at all —
    it is level 0 alone: the original, untouched, which is what keeps the
    neutral bit-exact with ``antialias`` on.
    """
    rate = abs(float(rate))
    if rate <= 1.0 or levels <= 0:
        return 0, 0, 0.0
    octaves = math.log2(rate)
    k0 = int(math.floor(octaves))
    if k0 >= levels:
        return levels, levels, 0.0
    frac = octaves - k0
    if frac <= 0.0:
        return k0, k0, 0.0
    return k0, k0 + 1, frac


@register_module_type
class Sampler(Module):
    """Pitched, gate-triggered sample playback with per-voice playheads.

    Parameters:
        path: Sample file (WAV via the zero-dependency fast path; mp3 /
            flac / ogg / m4a and video audio need the ``[media]`` extra).
            Empty or unreadable → silence, never an error.
        root: The note the recording *is*, as a MIDI note number
            (60 = C4). Playing it reads the file at rate 1.0.
        tune: Semitone shift on top, ±12. Default 0.
        fine: Cent shift on top, ±50. Default 0.
        mode: ``one_shot`` (edge fires the whole region, gate length
            ignored), ``gated`` (sounds while high, fades on the fall) or
            ``loop`` (gated, wrapping the loop region while held).
        start: Region start, 0..1 of the file. Default 0.
        end: Region end, 0..1 of the file. Default 1.
        loop_start: Loop start, 0..1 *of the region*. Default 0.
        loop_end: Loop end, 0..1 of the region. Default 1. At or below
            ``loop_start`` means "don't loop" — the voice plays as
            ``gated``.
        loop_xfade: Seam crossfade in ms, measured on the sample and
            clamped to the loop length. 0 is a hard seam. Default 10.
        attack: Declick ramp in at the start, ms. Default 0.
        release: Declick ramp out on a gated release, ms. Default 10.
        level: Output level. Default 0.8.
        start_cv_depth: Fraction of the file ``start`` moves per unit of
            ``start_cv``. Default 1.0 (0 V = ``start``, 1 V = the end).
        reverse: Play the region backwards. Default False.
        antialias: Read pitch-up from the on-load mip chain instead of
            letting it alias. Default False — the classic crunch.

    Ports:
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        gate (in, gate): rising edge starts playback (per voice).
        start_cv (in, cv): offsets ``start``, sampled at the gate edge.
        vel (in, cv): level multiplier, sampled at the gate edge.
        out (out, audio): the voice, mono.
        out_l / out_r (out, audio): the voice's stereo channels.
    """

    TYPE = "sampler"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "path": "",
        "root": 60.0,
        "tune": 0.0,
        "fine": 0.0,
        "mode": "one_shot",
        "start": 0.0,
        "end": 1.0,
        "loop_start": 0.0,
        "loop_end": 1.0,
        "loop_xfade": 10.0,
        "attack": 0.0,
        "release": 10.0,
        "level": 0.8,
        "start_cv_depth": 1.0,
        "reverse": False,
        "antialias": False,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("gate", "in", "gate"),
        Port("start_cv", "in", "cv"),
        Port("vel", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
