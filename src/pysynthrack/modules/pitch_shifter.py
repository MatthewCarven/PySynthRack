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

Like any grain/overlap shifter it adds some latency (about one grain)
and, pushed to extremes or on very low material with short grains, takes
on a characteristic granular smear — part of the sound.

For pitch shifting where speed *should* follow (tape/turntable, sample
transposition), use the [resampler] instead.

Ports:
  * ``in`` (audio): the signal to transpose. Unpatched -> silence out.
  * ``pitch_cv`` (cv): added to the semitone amount, scaled by
    ``cv_depth``. Optional; unpatched means 0.
  * ``out`` (audio): the pitch-shifted signal.

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

    Ports:
        in (in, audio): signal to transpose. Unpatched -> silence.
        pitch_cv (in, cv): added to the transpose, scaled by cv_depth.
        out (out, audio): the pitch-shifted signal.
    """

    TYPE = "pitch_shifter"
    DEFAULT_PARAMS = {
        "semitones": 0.0,
        "cents": 0.0,
        "cv_depth": 12.0,
        "mix": 1.0,
        "grain_size": 50.0,
        "overlap": 2,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("pitch_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
