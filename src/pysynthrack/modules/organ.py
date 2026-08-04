"""Organ — a nine-drawbar additive organ voice (tonewheel lineage).

Nine sine partials per voice at the classic drawbar footages, each with
its own 0..8 level fader, summed under a constant-RMS normalisation.
Play it from any voice-routed pitch/gate source (``cv_keyboard``,
``midi_input``, ``chord``) — the 16-slot voice architecture makes it a
full polyphonic organ with zero extra patching.

The drawbars (`bar1`..`bar9`) sit at the classic footages::

    bar    1     2      3    4    5      6    7      8      9
    feet   16'   5 1/3' 8'   4'   2 2/3' 2'   1 3/5' 1 1/3' 1'
    ratio  0.5   1.5    1.0  2.0  3.0    4.0  5.0    6.0    8.0

``ratio`` is the partial's frequency as a multiple of the played pitch
(the 8' bar is the fundamental). Levels follow the hardware law: **~3 dB
per step**, ``level = 8`` is unity and ``0`` is silent —
``gain = 10^(-3 * (8 - level) / 20)``. The default registration is
888000000 (the jazz classic). The summed partials are normalised by
``1 / sqrt(max(1, sum(gain^2)))`` — constant RMS, so pulling more bars
changes timbre more than level and full registrations keep headroom.
The normaliser uses the *unmasked* gains, so a partial muted by the
Nyquist guard (below) simply goes missing rather than making the rest
louder.

An organ has **no envelope** — the gate is the articulation, exactly
like the instrument. Each gate edge moves the voice's amplitude along a
~1 ms linear ramp (reaching exactly 1.0 / 0.0, so a held note is
bit-transparent), and ``click`` adds the classic key-click contact
transient on top: a short seeded noise tick at each press (softer at
release). For shaped swells, patch a ``vca`` after it like any voice.

The percussion register is the other tonewheel signature: a
fast-decaying extra harmonic (2nd = 4', 3rd = 2 2/3') that fires
**only on a key struck from silence** — one shared generator, so legato
playing and additions to a held chord do NOT re-fire it. The percussion
strike is added to voice row 0 (it is monophonic hardware; document
quirk, not a bug).

Partials that would land at or above Nyquist are muted per block (a C8
fundamental puts the 1' bar past any reasonable sample rate) — masked,
never aliased. Tonewheel top-octave *foldback* is a possible later
authenticity extra.

Voice-awareness follows the inputs (the ``pluck`` contract): mono
``(F,)`` pitch/gate give mono out; ``(V, F)`` give per-voice organs.
Pitch is read per block (mean) — vibrato tracks at block rate. Phases,
ramp levels, click tails and the percussion state all carry across
blocks. Numpy backend only; silent stub under pyo.

Ports:
  * ``pitch_cv`` (cv, in): 1 V/oct, C4 = 0 V. Unpatched → C4.
  * ``gate`` (gate, in): key down/up per voice. Unpatched → silence.
  * ``out`` (audio, out): the organ.

Params:
  * ``bar1``..``bar9``: drawbar levels, integer 0..8. Default 888000000.
  * ``click``: key-click amount 0..1. Default 0.3.
  * ``perc``: percussion register — ``off`` | ``2nd`` | ``3rd``.
    Default ``off``.
  * ``perc_decay``: ``fast`` (~0.3 s) | ``slow`` (~1.0 s). Default
    ``fast``.
  * ``perc_level``: percussion strike level 0..1. Default 0.7.
  * ``level``: output level 0..1. Default 0.5.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Drawbar count / harmonic ratios / UI labels. Shared by the module, the
# numpy renderer, the drawbar panel and the tests (fader_seq
# FADER_RANGE_ST precedent: one dpg-free home for the numbers).
ORGAN_BARS = 9
ORGAN_RATIOS = (0.5, 1.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
ORGAN_FOOTAGES = (
    "16'", "5 1/3'", "8'", "4'", "2 2/3'", "2'", "1 3/5'", "1 1/3'", "1'"
)
# The jazz registration: 16' + 5 1/3' + 8' full, the rest off.
ORGAN_DEFAULT_BARS = (8, 8, 8, 0, 0, 0, 0, 0, 0)

PERC_MODES = ("off", "2nd", "3rd")
PERC_DECAYS = ("fast", "slow")


def _default_params() -> dict:
    params: dict = {
        f"bar{i + 1}": ORGAN_DEFAULT_BARS[i] for i in range(ORGAN_BARS)
    }
    params.update(
        {
            "click": 0.3,
            "perc": "off",
            "perc_decay": "fast",
            "perc_level": 0.7,
            "level": 0.5,
        }
    )
    return params


@register_module_type
class Organ(Module):
    """Nine-drawbar additive organ (see the module docstring).

    Parameters:
        bar1..bar9: Drawbar levels 0..8 (16' .. 1'), ~3 dB per step.
            Default 888000000.
        click: Key-click transient amount, 0..1. Default 0.3.
        perc: Percussion register, off | 2nd | 3rd. Default off.
        perc_decay: Percussion decay, fast | slow. Default fast.
        perc_level: Percussion strike level, 0..1. Default 0.7.
        level: Output level, 0..1. Default 0.5.

    Ports:
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        gate (in, gate): key down per voice. Unpatched → silence.
        out (out, audio): the organ.
    """

    TYPE = "organ"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("gate", "in", "gate"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
