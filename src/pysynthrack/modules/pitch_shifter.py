"""PitchShifter — a time-preserving (granular WSOLA) pitch shifter.

The speed-preserving cousin of the [resampler]. Where the resampler is
varispeed (pitch and speed move together, like tape), this shifts pitch
while the *speed and duration stay put* — transpose a held note, a loop,
or live playing without it getting faster or slower.

It works by **WSOLA** (waveform-similarity overlap-add): the audio is
sliced into short overlapping grains and overlapped back together at the
original rate (that's what preserves duration), with each grain
resampled to move the pitch. The "waveform-similarity" part is the trick
that makes it clean on tonal material — each grain is nudged to the
position where it best lines up with the previous one, so the overlap
joins are phase-continuous and you don't get the beating/doubling
artefacts a naive granular shifter produces on a held tone.

Pitch is set musically in **semitones** (C to D = +2) with a **cents**
fine-tune, and a `pitch_cv` input adds to that — summed in *semitone*
space, scaled by `cv_depth` (12 = one octave per CV unit). Pitch CV is
sampled per processing block (not per sample), which is ample for
vibrato and slides.

The grain engine is exposed: **grain_size** trades transient sharpness
(short grains) against smoothness on sustained/low material (long
grains — bass needs longer grains so a grain spans a full cycle), and
**overlap** sets how many grains overlap (more = smoother and denser,
at more CPU). A dry/wet **mix** blends the original with the shifted
signal, so one module covers full transposition, subtle detune-
thickening, and parallel harmonies (e.g. +7 semitones at 50% = a fifth
stacked over the dry).

Like any grain/overlap shifter it adds latency (roughly one to two
grains, depending on ``overlap``); the dry/wet **mix is internally
delay-matched** to that exact latency, so partial blends and parallel
harmonies stay phase-coherent — a near-unison detune thickens instead
of hollowing out. Pushed to extremes or on very low material with short
grains it takes on a characteristic granular smear — part of the sound.

For pitch shifting where speed *should* follow (tape/turntable, sample
transposition), use the [resampler] instead.

``formant_preserve`` keeps the *timbre* (spectral envelope) in place
while the pitch moves: an LPC envelope is estimated from the input,
the grain engine shifts the whitened residual, and the envelope is
re-applied afterwards. Off (the default) you get the classic
"chipmunk/giant" coloration where formants travel with the pitch; on,
a shifted voice or resonant patch keeps its vowel/body. Estimation is
time-domain (Levinson-Durbin, order 24) — no FFT phase games.

**Shimmer** (``feedback``, added 2026-09-14): the shifted output is fed
back into the input, so every lap is shifted *again*. At +12 that is the
octave cascade — a note blooms into a cloud that climbs away from itself,
each lap an octave higher and ``feedback`` quieter. The loop is short (one
block plus the engine's own latency, a grain or two), so this is the fast,
spectral kind of shimmer, not the slow cathedral kind; for that, put the
shifter in a [matrix_mixer] loop with a [delay] — see
``examples/organ_shimmer.json``. The loop is damped above ~6 kHz so the
climb dies against the top of hearing rather than folding back as alias
grit, and soft-ceilinged so a hot setting bends instead of blowing up.
The dry side of ``mix`` always hears the *original* input, never the
recirculated one. Feedback is clamped to 0.9.

**Harmonizer** (``harmony`` / ``harmony_level``, same day): a second
shifted voice at ``harmony`` semitones, mixed in at ``harmony_level`` on
top of the main shift. ``semitones`` +4 and ``harmony`` +7 over the dry is
a major triad from one module; +7 and +12 is a power chord; −12 and +12
is an octave stack. ``pitch_cv`` moves both, so the chord transposes as
one. The harmony hears the raw input, not the feedback loop — it stays a
clean interval over whatever the shimmer is doing. It costs a second
grain engine per voice, built the first time the level rises above 0.

**Stereo** (``spread``, same day; ``out_l`` / ``out_r``): with a harmony
present, ``spread`` pans the main shift toward ``out_l`` and the harmony
toward ``out_r`` — the far channel fades by ``spread``, the dry stays
centred in both. At 1 the two voices are hard-panned: patch ``out_l`` /
``out_r`` to the [left_speaker_output] / [right_speaker_output] and the
interval opens across the field. Without a harmony, or at 0, both jacks
are ``out`` — so a patch wired stereo plays mono rather than falling
silent until the knob moves.

Deep bass takes care of itself: the engine watches the input period
and, when the configured grain is too short to hold ~2.5 cycles (low
E and below at the default 50 ms), grows its working grain
automatically — ``grain_size`` acts as the floor. Pitch accuracy is
sub-cent across the range: the analysis clock runs on an exact grid
and grain joins are aligned to sub-sample precision.

Ports:
  * ``in`` (audio): the signal to transpose. Unpatched -> silence out.
  * ``pitch_cv`` (cv): added to the semitone amount, scaled by
    ``cv_depth``. Optional; unpatched means 0.
  * ``out`` (audio): the pitch-shifted signal (main + harmony, mono sum).
  * ``out_l`` / ``out_r`` (audio): the stereo pair — main leans left,
    harmony leans right by ``spread``. Identical to ``out`` when there is
    no harmony or ``spread`` is 0.

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. Mono ``(F,)`` audio ->
  mono ``(F,)`` out with one grain engine. Voice-aware ``(V, F)`` audio
  -> ``(V, F)`` out with one independent grain engine per voice slot
  (a mono ``pitch_cv`` broadcasts; a ``(V, F)`` ``pitch_cv`` drives each
  voice). A single voice row is bit-identical to the mono render.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class PitchShifter(Module):
    """Granular (WSOLA) time-preserving pitch shifter.

    Parameters:
        semitones: Coarse transpose in semitones (C->D = +2). 0 = unity.
        cents: Fine-tune in cents (100 = 1 semitone), added to semitones.
        cv_depth: Semitones per unit of ``pitch_cv`` (default 12.0 =
            one octave per unit).
        mix: Dry/wet blend, 0.0 = dry (original) .. 1.0 = wet (shifted).
        grain_size: Grain length in milliseconds (longer = smoother on
            sustained/low material, shorter = sharper transients).
        overlap: Number of overlapping grains (2-4). Higher = smoother
            and denser at more CPU.
        formant_preserve: Keep the spectral envelope (timbre) in place
            while pitch moves (LPC whiten -> shift -> re-color).
            Default False = classic varispeed-style coloration.
        feedback: Shimmer. 0 .. 0.9: recirculates the shifted output into
            the input so every lap is shifted again (an octave cascade
            at +12). Damped and soft-ceilinged. Default 0 = off.
        harmony: Semitones for a second shifted voice, -24 .. +24.
            Default 0 (a unison, silent until ``harmony_level`` rises).
        harmony_level: Level of the harmony voice, 0 .. 1. Default 0 =
            off (no second engine is built).
        spread: 0 .. 1. With a harmony present, pans the main shift
            toward ``out_l`` and the harmony toward ``out_r``. Default 0.

    Ports:
        in (in, audio): signal to transpose. Unpatched -> silence.
        pitch_cv (in, cv): added to the transpose, scaled by cv_depth
            (moves the harmony too).
        out (out, audio): the pitch-shifted signal, main + harmony.
        out_l / out_r (out, audio): the stereo pair; ``out`` when there
            is no harmony or ``spread`` is 0.
    """

    TYPE = "pitch_shifter"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "semitones": 0.0,
        "cents": 0.0,
        "cv_depth": 12.0,
        "mix": 1.0,
        "grain_size": 50.0,
        "overlap": 2,
        "formant_preserve": False,
        "feedback": 0.0,
        "harmony": 0.0,
        "harmony_level": 0.0,
        "spread": 0.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("pitch_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
