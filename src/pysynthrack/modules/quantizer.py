"""Quantizer — snap a CV to the nearest note of a scale.

The missing link between random/LFO/sequencer voltages and *melody*: feed
any wandering CV into ``in`` and ``out`` is the same voltage snapped to the
nearest allowed pitch — 1 V/oct, C4 = 0 V, the house pitch convention —
so whatever consumes it (oscillator ``freq_cv``, ``cv_to_frequency``,
``fm_op`` ``pitch_cv``) lands *in tune*.

Two modes, picked by patching:

  * **Continuous** (``gate`` unpatched) — every sample is quantized. A
    ``hysteresis`` band (cents) around the currently held note stops the
    output fluttering when the input hovers exactly on a boundary between
    two allowed notes: the quantizer only moves to a new note once the
    input is *more than the margin* closer to it than to the held one.
  * **Gated** (``gate`` patched) — sample-and-quantize on each rising
    edge only, holding in between: a clocked melody from a free-running
    LFO, drift-proof.

``changed`` emits a short (~5 ms) trigger every time the held note
actually changes — patch it to an envelope so each new note articulates,
no separate clock required.

The scale is ``root`` + ``scale``; ``custom`` reads the twelve per-pitch-
class tickboxes instead (an empty custom set falls back to chromatic so
the module can never wedge silent). ``transpose`` shifts the *output* in
semitones after quantization, so the shifted result can leave the scale —
it is a transposition, not a scale rotation.

Neutral: ``chromatic`` + ``hysteresis 0`` + ``transpose 0`` is semitone
rounding — deliberately NOT a passthrough; a quantizer that never snaps
isn't one.

Voice-awareness:
  Shape-polymorphic per the house convention: a mono ``(F,)`` CV in gives
  mono outs; a voice ``(V, F)`` CV in gives ``(V, F)`` outs with a held
  note and a ``changed`` pulse per voice slot (a polyphonic source
  quantizes without crosstalk). The held note is primed to the first
  input sample on the first block, so loading a patch doesn't fire a
  spurious ``changed``. An unpatched ``in`` emits 0 and drops state.

Ports:
  * ``in`` (cv): the pitch CV to quantize. Unpatched → 0 out.
  * ``gate`` (gate): optional; when patched, quantize only on rising edges.
  * ``out`` (cv): the quantized (and transposed) pitch CV.
  * ``changed`` (gate): ~5 ms trigger per new held note.

Params:
  * ``root``: scale root, ``C`` … ``B``. Default ``C``.
  * ``scale``: one of ``QUANTIZER_SCALES``. Default ``major``.
  * ``hysteresis``: boundary margin in cents, 0..50. Default 10.
  * ``transpose``: output shift in semitones, −24..+24. Default 0.
  * ``custom_c`` … ``custom_b``: the twelve pitch-class tickboxes read
    when ``scale`` is ``custom``. Default all on (chromatic).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Scale interval tables (semitones above the root). ``custom`` is resolved
# from the twelve tickbox params at render time.
QUANTIZER_SCALES = (
    "chromatic",
    "major",
    "minor",
    "harmonic minor",
    "pentatonic major",
    "pentatonic minor",
    "dorian",
    "mixolydian",
    "blues",
    "whole tone",
    "custom",
)

SCALE_INTERVALS: dict[str, tuple[int, ...]] = {
    "chromatic": tuple(range(12)),
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "harmonic minor": (0, 2, 3, 5, 7, 8, 11),
    "pentatonic major": (0, 2, 4, 7, 9),
    "pentatonic minor": (0, 3, 5, 7, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "blues": (0, 3, 5, 6, 7, 10),
    "whole tone": (0, 2, 4, 6, 8, 10),
}

QUANTIZER_ROOTS = (
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
)

# The twelve custom-scale tickbox params, in pitch-class order. The UI
# and the renderer both walk this tuple so the two can never drift.
CUSTOM_KEYS = (
    "custom_c", "custom_cs", "custom_d", "custom_ds", "custom_e",
    "custom_f", "custom_fs", "custom_g", "custom_gs", "custom_a",
    "custom_as", "custom_b",
)


@register_module_type
class Quantizer(Module):
    """Snap a pitch CV to the nearest note of a scale (1 V/oct, C4 = 0 V).

    Parameters:
        root: Scale root, ``C`` … ``B``. Default ``C``.
        scale: Scale name from ``QUANTIZER_SCALES``; ``custom`` reads the
            twelve tickboxes. Default ``major``.
        hysteresis: Cents of stickiness around the held note in
            continuous mode (kills boundary flutter). 0..50, default 10.
        transpose: Semitones added to the *output* after quantization,
            −24..+24. Default 0.
        custom_c .. custom_b: Pitch-class tickboxes for ``custom``.
            All-off falls back to chromatic.

    Ports:
        in (in, cv): pitch CV to quantize. Unpatched → 0.
        gate (in, gate): optional — quantize on rising edges only.
        out (out, cv): quantized pitch CV.
        changed (out, gate): ~5 ms trigger per new held note.
    """

    TYPE = "quantizer"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {
        "root": "C",
        "scale": "major",
        "hysteresis": 10.0,
        "transpose": 0.0,
        **{key: True for key in CUSTOM_KEYS},
    }
    INPUT_PORTS = [
        Port("in", "in", "cv"),
        Port("gate", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "cv"),
        Port("changed", "out", "gate"),
    ]
