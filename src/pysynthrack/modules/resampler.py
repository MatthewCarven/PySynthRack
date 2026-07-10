"""Resampler — a varispeed pitch shifter (tape/turntable style).

Transposes whatever audio you feed it by *resampling* — reading the
signal back at a different rate. Like a turntable or a tape machine,
pitch and speed move together: pitch it up and it plays back faster,
pitch it down and it slows. That coupling is the whole character — it's
the cheapest, cleanest way to shift pitch, with no FFT and no phase
vocoder, and it's exactly what you want for sample transposition and
lo-fi tape effects.

Set the amount musically in **semitones** (``C`` to ``D`` is +2), with a
**cents** fine-tune on top. A ``pitch_cv`` input adds to that, summed in
*semitone* space and then exponentiated to a ratio, so pitch CV stays
musical — an LFO gives vibrato, an envelope gives pitch dives, a
sequencer/keyboard CV transposes per step. ``cv_depth`` sets how many
semitones one CV unit is worth (default 12 = one octave per unit, i.e.
1V/oct-style).

How it stays alive on a live signal: a resampler reading at a different
rate than it's fed can't keep a continuous stream in sync forever, so
internally it runs a short **looping buffer** of recent audio — the
``window`` param, in milliseconds. The read head wraps within that
window, so the module keeps making sound indefinitely on any source — a
live oscillator, the mic, the file player — at the cost of a faint
granular-repeat texture on extreme shifts. Each wrap of that window is
a short equal-power crossfade rather than a splice, so the loop seam
doesn't click. The unavoidable consequence of varispeed is a little
latency — half the window (the read head trails the write head); that
buffer is what lets you glide and modulate the pitch freely. ``window``
is the trade-off knob: shorten it toward 20 ms for tight live-input
latency (the loop texture gets stronger and more granular), stretch it
toward 2 s for the subtlest texture on big shifts (latency grows to
match). The default 200 ms (~100 ms latency) is the old fixed
behaviour. Changing it live keeps the most recent audio, so tweaking
the slider doesn't punch a hole in the sound.

``glide`` smooths pitch changes: with it above zero, slider and CV jumps
ramp instead of stepping, giving portamento and tape-stop sweeps. At 0
the pitch follows instantly.

``mix`` blends the shifted signal against the dry input (1 = fully
wet, the default; 0 = dry). The dry tap is time-aligned with the
unity-pitch wet signal, so sweeping the mix blends coherently instead
of adding a slapback echo — at 50% with a small detune you get
instant chorus-style thickening in one module.

``antialias`` (off by default) is the clean-vs-gritty switch for
*pitching up*. Reading the buffer faster than it's written shifts the
source's highs past Nyquist, where they fold back as aliasing — real
tape never does this because it's inherently band-limited. Turn it on
and the input is low-passed to match the shift before the read, so
up-shifts stay clean; leave it off to keep the raw, aliased lo-fi
character that suits the sci-fi/tape sound. Pitching down and unity
never fold, so they're untouched either way.

Use cases:
  * Transpose a sample/loop from the FilePlayer — turn one hit into a
    melodic run by sequencing ``pitch_cv``.
  * Tape-stop / vinyl-brake risers by gliding the pitch down to a
    crawl.
  * Detune/thicken: +10 cents at 50% ``mix`` for chorusy width,
    without needing a parallel path.
  * Sci-fi / formant-shift vocal and drum mangling at extreme settings,
    where the loop texture becomes part of the sound.

For pitch shifting that keeps the *speed* fixed (a true time-preserving
shifter) you'd want a granular or phase-vocoder engine — a heavier
build for a later module. This one is deliberately the tape kind.

Ports:
  * ``in`` (audio): the signal to transpose. Unpatched -> silence out.
  * ``pitch_cv`` (cv): added to the semitone amount, scaled by
    ``cv_depth``. Optional; unpatched means 0 (no modulation).
  * ``out`` (audio): the resampled signal.

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` audio in
  -> mono ``(F,)`` out with one looping buffer. A voice-aware ``(V, F)``
  audio in -> ``(V, F)`` out with one looping buffer per voice slot and
  per-voice read heads, so a polyphonic carrier upstream is transposed
  without cross-talk. A mono ``pitch_cv`` broadcasts across voices (one
  shared transpose), a ``(V, F)`` ``pitch_cv`` drives each voice
  independently. A single voice row is bit-identical to the mono path.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Resampler(Module):
    """Varispeed (pitch+speed) resampling pitch shifter.

    Parameters:
        semitones: Coarse transpose in semitones (C->D = +2). 0 = unity.
        cents: Fine-tune in cents (100 cents = 1 semitone), added to
            ``semitones``.
        cv_depth: Semitones per unit of ``pitch_cv`` (default 12.0 =
            one octave per unit, 1V/oct-style).
        glide: Portamento time in seconds for pitch changes (0 =
            instant). Smooths slider and CV moves into ramps.
        mix: Dry/wet blend (0 = dry, 1 = fully wet, default 1.0). The
            dry tap is latency-compensated to line up with unity-pitch
            wet, so the blend is coherent.
        window: Looping-buffer window in milliseconds (20-2000, default
            200). Latency is half the window, so shorter = tighter
            latency but a stronger granular-repeat texture on non-unity
            shifts; longer = subtler texture, more latency. Floored at
            four audio blocks. Changing it live keeps the most recent
            audio, so a slider drag doesn't drop out.
        antialias: Off (0) by default. When on (1), band-limits the
            input before the read so **pitching up** doesn't fold source
            content past Nyquist into aliasing -- a cleaner up-shift, at
            the cost of the raw/lo-fi character (leave it off for the
            gritty tape/sci-fi sound). Pitch-down and unity are
            unaffected (nothing folds there); the dry side of ``mix``
            stays full-band.

    Ports:
        in (in, audio): signal to transpose. Unpatched -> silence.
        pitch_cv (in, cv): added to the transpose, scaled by cv_depth.
        out (out, audio): the resampled signal.
    """

    TYPE = "resampler"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "semitones": 0.0,
        "cents": 0.0,
        "cv_depth": 12.0,
        "glide": 0.0,
        "mix": 1.0,
        "window": 200.0,
        "antialias": False,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("pitch_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
