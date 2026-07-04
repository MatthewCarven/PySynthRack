"""Frequency shifter — Bode-style single-sideband spectral shift.

Where a [`pitch_shifter`](#pitch_shifter) *multiplies* every partial's
frequency by a ratio (harmonics stay harmonic — the sound keeps its
identity, just higher or lower), a **frequency shifter** *adds a fixed
number of hertz to every partial*. A 100/200/300 Hz harmonic series
shifted up 50 Hz becomes 150/250/350 — no longer integer multiples of a
common fundamental, so the ear stops hearing "a note" and starts hearing
**inharmonic, metallic clang**. It's a fundamentally different animal
from the pitch shifter: small shifts give shimmer, phasing and a hollow
"detune that never resolves"; larger shifts give bell-like and robotic
timbres; and with feedback the endlessly-recirculating shift becomes the
classic **barberpole / Shepard** glide that seems to rise (or fall)
forever.

The shift is done the analog Bode/Moog way — a **single-sideband**
modulation. The input is split into a quadrature (90°) pair with an FIR
Hilbert transformer to form the analytic signal, which is then rotated by
a complex sine at the shift frequency. Taking the two real projections of
that rotation gives the **two sidebands at once**: ``out_up`` moves every
partial *up* by ``shift`` Hz, ``out_down`` moves every partial *down* by
the same amount (they are mirror images — the conjugate sideband). Patch
whichever you want, or both for a widening dual-shift.

The Hilbert FIR has a fixed group delay (~2.9 ms), so the module runs
that far behind its input. The dry path in the ``mix`` blend is delayed
to match, so dry and wet stay phase-coherent — at ``shift = 0`` the wet
*is* the delayed dry, and the blend is transparent rather than combing.

Controls:
  * ``shift`` — shift amount in Hz, −2000..+2000. Positive sends
    ``out_up`` up and ``out_down`` down; negative swaps them. A few Hz is
    slow phasing/shimmer; tens–hundreds of Hz is inharmonic metal;
    negative shifts hollow a sound out toward DC.
  * ``shift_cv_depth`` — Hz of shift per unit of ``shift_cv`` (200).
    ``shift_cv`` is a *linear Hz* control (not 1 V/oct — a frequency
    shift is an addition, not a ratio), so an LFO here sweeps the clang
    and an envelope makes it dive.
  * ``mix`` — dry/wet, 0 (bit-exact dry passthrough) .. 1 (full shift).
  * ``feedback`` — 0..0.9, recirculates ``out_up`` back into the input so
    each pass is shifted again: the barberpole/endless-glide texture.
    0 is a clean single shift.

Ports:
  * ``in`` (audio): signal to shift. Voice-aware; a single voice row is
    bit-identical to the mono render. Unpatched -> silence.
  * ``shift_cv`` (cv): linear Hz shift modulation, scaled by
    ``shift_cv_depth``. Optional; unpatched means a static ``shift``.
  * ``out_up`` (audio): every partial shifted **up** by ``shift`` Hz.
  * ``out_down`` (audio): every partial shifted **down** by ``shift`` Hz
    (the opposite sideband).

Use cases:
  * Metallic / bell tones: a few hundred Hz of shift on a sustained tone.
  * Barberpole glide: raise ``feedback`` and listen to ``out_up`` climb
    forever (or ``out_down`` for the falling version).
  * Stereo width: ``out_up`` and ``out_down`` into L/R for a shimmering,
    decorrelated spread from a mono source.
  * Feedback howl-tamer / drone: tiny shifts (~1–5 Hz) break up static
    resonances that would otherwise ring.

Pairs with [`ring_mod`](#ring_mod) as the other inharmonic corner of the
rack — ring mod keeps *sum and difference* of two signals, the frequency
shifter keeps *one shifted sideband* of one signal.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class FreqShifter(Module):
    """Single-sideband (Bode) frequency shifter: up/down sidebands out.

    Parameters:
        shift: Shift amount in Hz (−2000 .. +2000). Added to every
            partial's frequency (not a ratio). 0 -> the wet is the
            delay-matched dry.
        shift_cv_depth: Hz of shift per unit of ``shift_cv`` (linear Hz,
            not 1 V/oct — a shift is an addition). Default 200.
        mix: Dry/wet balance, dry (0) -> wet (1). 0 is a bit-exact dry
            passthrough on both outputs.
        feedback: 0 .. 0.9. Recirculates ``out_up`` into the input for
            the barberpole/endless-shift texture. 0 is a single shift.

    Ports:
        in (in, audio): signal to shift. Unpatched -> silence.
        shift_cv (in, cv): linear-Hz shift modulation (x
            ``shift_cv_depth``). Optional.
        out_up (out, audio): partials shifted up by ``shift`` Hz.
        out_down (out, audio): partials shifted down by ``shift`` Hz.
    """

    TYPE = "freq_shifter"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "shift": 0.0,
        "shift_cv_depth": 200.0,
        "mix": 1.0,
        "feedback": 0.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("shift_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out_up", "out", "audio"),
        Port("out_down", "out", "audio"),
    ]
