"""WavetableMorph — a scanning wavetable oscillator (the `*_wt` infra
grown into an instrument).

A stack of single-cycle tables; ``position`` scans through it,
crossfading adjacent frames — turn the knob (or patch ``position_cv``)
and the timbre *morphs*. Every frame is mip-mapped exactly like the
oscillator's ``*_wt`` shapes (band-limited additive tables per octave,
band picked from the block's top frequency), so the scan stays
alias-free right up the keyboard.

Built-in stacks (``table``):

  * ``analog`` — sine → triangle → saw → square: the subtractive
    classics as one knob. Position 0 is a pure sine; 1 is a square;
    the thirds land the triangle and saw exactly.
  * ``vowel`` — five formant frames (A → E → I → O → U): harmonic
    bumps at generic vowel formant positions (physics-textbook
    shapes, not sampled from anything).
  * ``metallic`` — sparse high-harmonic sets with fixed seeded
    phases: clangy, bell-adjacent frames that morph from glassy to
    gnarly.

``file`` imports a **single-cycle WAV** (Browse on the node — the
FilePlayer picker): the cycle is spectrally resampled to the table
length, mip-banded like the built-ins, and replaces the stack as a
single frame (``position`` inert until a multi-frame import format
exists — a possible later slice). An unreadable/missing file quietly
falls back to the ``table`` stack, so patches always load.

Voice-aware via ``freq_cv`` (1 V/oct around ``freq``, per-sample);
``position_cv`` (× ``position_cv_depth``, the house depth convention)
adds to ``position`` and is read per block per voice. ``amp_cv``
broadcast-multiplies at the end.

Ports:
  * ``freq_cv`` (cv, in): 1 V/oct around ``freq``. Optional;
    voice-aware.
  * ``position_cv`` (cv, in): adds to ``position`` ×
    ``position_cv_depth``. Optional; block-rate, per voice.
  * ``amp_cv`` (cv, in): linear level. Optional.
  * ``out`` (audio, out): the morphing oscillator.

Params:
  * ``freq``: base frequency in Hz. Default 261.6256 (C4).
  * ``position``: scan point 0..1. Default 0.
  * ``position_cv_depth``: position units per CV unit. Default 1.
  * ``table``: ``analog`` | ``vowel`` | ``metallic``. Default
    ``analog``.
  * ``file``: single-cycle WAV path ('' = use ``table``). Default ''.
  * ``amp``: output level. Default 0.5.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

WT_STACKS = ("analog", "vowel", "metallic")


@register_module_type
class WavetableMorph(Module):
    """Scanning wavetable oscillator (see the module docstring).

    Parameters:
        freq: Base frequency in Hz. Default 261.6256 (C4).
        position: Scan point 0..1. Default 0.
        position_cv_depth: Position units per CV unit. Default 1.
        table: analog | vowel | metallic. Default analog.
        file: Single-cycle WAV path ('' = built-in stack). Default ''.
        amp: Output level. Default 0.5.

    Ports:
        freq_cv (in, cv): 1 V/oct around freq (per-sample). Optional.
        position_cv (in, cv): adds to position × depth. Optional.
        amp_cv (in, cv): linear level. Optional.
        out (out, audio): the morphing oscillator.
    """

    TYPE = "wavetable_morph"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "freq": 261.6256,
        "position": 0.0,
        "position_cv_depth": 1.0,
        "table": "analog",
        "file": "",
        "amp": 0.5,
    }
    INPUT_PORTS = [
        Port("freq_cv", "in", "cv"),
        Port("position_cv", "in", "cv"),
        Port("amp_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
