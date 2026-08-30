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

**Pitch-up aliases, deliberately.** Reading faster than 1.0 shifts the whole
spectrum up and anything that was near Nyquist folds. That crunch is the
sound of every classic sampler and it is left in; a mip-chain for clean
pitch-up is a later slice. Pitch *down* is clean — the 4-tap cubic read is
the resampler's, so non-integer rates stay smooth rather than gritty.

**``attack``/``release`` are declick ramps, not an envelope.** They exist so
a region boundary that lands mid-waveform doesn't click. For real shaping
patch an [`adsr`](#adsr) into a [`vca`](#vca), the way every other source in
the rack expects — raise ``attack`` only if your ``start`` point ticks.

Loading happens on a background thread (the convolver's ``_IRLoader``
precedent), so a fresh or changed ``path`` never blocks the audio thread:
the module is simply silent until the sample is ready, and a missing or
unreadable file stays silent rather than raising — saved patches always
load, whatever became of the audio on disk. Multi-channel files are summed
to mono on load (stereo out is a later slice). Long files are capped (see
``MAX_SECONDS``): the limit is RAM, not DSP — a minute of mono at 48 kHz is
about 11 MB.

Voice-awareness: shape follows the inputs — mono ``(F,)`` in gives mono out,
``(V, F)`` gives per-voice playheads with no crosstalk, and a finished voice
early-outs so it costs nothing until retriggered. Pitch is read per block
(mean), so slides and vibrato track at block rate — the ``pluck``
precedent. Numpy backend only; silent stub under pyo.

Ports:
  * ``pitch_cv`` (cv): 1 V/oct, C4 = 0 V. Unpatched → C4 → the root note.
  * ``gate`` (gate): rising edge starts playback from ``start``.
  * ``out`` (audio): the voice.

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
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

#: Playback modes. ``loop`` is ``gated`` with the playhead wrapping the
#: loop region, which is what turns a finite recording into a sustain.
SAMPLER_MODES = ("one_shot", "gated", "loop")

#: MIDI note the 1 V/oct convention calls 0 V — C4, as everywhere else.
CV_REFERENCE_NOTE = 60

#: Longest sample kept, in seconds. RAM-bound, not DSP-bound: a minute of
#: mono float64 at 48 kHz is ~23 MB in the buffer. Anything longer is
#: truncated (with a short fade so the cut doesn't click).
MAX_SECONDS = 120.0

#: Root-note combo range, inclusive, as MIDI note numbers (C0..C8).
ROOT_MIN_NOTE = 12
ROOT_MAX_NOTE = 108


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
    """
    semitones = (
        (CV_REFERENCE_NOTE + 12.0 * float(cv)) - float(root_note)
        + float(tune) + float(fine) / 100.0
    )
    return 2.0 ** (semitones / 12.0)


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

    Ports:
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        gate (in, gate): rising edge starts playback (per voice).
        out (out, audio): the voice.
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
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("gate", "in", "gate"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
