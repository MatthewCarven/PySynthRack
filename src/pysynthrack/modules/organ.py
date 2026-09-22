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

The scanner is the third tonewheel signature: ``vibrato`` is the
console's six-way knob. The hardware is a short tapped delay line swept
by a rotating capacitor pickup on a 412 rpm motor — **~6.87 Hz**, the
one rate every Hammond vibrato ever had — and V1/V2/V3 switch in more of
the line (peak-to-peak sweep 0.35 / 0.7 / 1.1 ms, a sine here where the
real pickup traces a rounded triangle), so the pitch wobbles about
±13 / ±26 / ±41 cents (measured, not just predicted: a sinusoidal delay
of half-swing ``A`` deviates the pitch ratio by ``2*pi*f*A`` at its
peak). C1/C2/C3 are the *same* swept signal mixed with the un-delayed
dry in equal parts, ``0.5 * (dry + scanned)`` — the chorus: the
scanned copy sits a fraction of a millisecond behind the dry, so the
sum is a comb whose notches sweep with the scanner, and averaging
keeps the fundamental's level where V left it instead of jumping 6 dB.
The line starts at (almost) zero delay for every setting, as the real
scanner starts at tap 0, so the chorus thins to near-dry at the top of
each sweep and deepens on the way down.

The scanner takes the **finished voice sum** — partials, key click and
percussion (``level`` is already folded into the partial gains, so
before/after it is the same algebra). One mechanism per organ, as on
the console: a single scanner phase shared by every voice, each voice
row in its own delay ring so voices never cross-talk (the percussion
strike on row 0 rides through row 0's ring). ``off`` touches nothing —
no ring, no state, bit-exact with the pre-scanner organ. Every switch
(off → V, V1 → V3, V → C, anything → off) crossfades gains and sweep
depth over a ~40 ms integer-counted ramp, so flipping the knob under a
held chord is click-free; the line is dropped once the fade back to
dry has finished.

Voice-awareness follows the inputs (the ``pluck`` contract): mono
``(F,)`` pitch/gate give mono out; ``(V, F)`` give per-voice organs.
Pitch is read per block (mean, accumulated in float64) — vibrato tracks
at block rate. Phases, ramp levels, click tails and the percussion
state all carry across blocks. Numpy backend only; silent stub under
pyo.

**Block-size exactness.** Nothing in the voice carries a float that a
different block partition would round differently. A partial's phase
is an ORIGIN plus the integer count of samples since that origin —
``ph(k) = (origin + inc*k) % 1`` — and the origin is re-anchored only
when a voice's block-mean pitch changes, so a held note never
accumulates at all. The percussion strike's decay, phase and 1e-6
cut-off are likewise functions of the integer count since it fired,
applied per sample rather than once per rendered segment. Before
2026-09-22 both were per-block accumulators: the phase drifted a
float32 ulp inside a second or two and the percussion tail died on a
different sample at every block size. 64 / 128 / 512 / 1000 now render
the identical sample over ten seconds and beyond.

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
  * ``vibrato``: the scanner — ``off`` | ``v1`` | ``v2`` | ``v3`` |
    ``c1`` | ``c2`` | ``c3``. Default ``off``.
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
# The scanner knob: three vibrato depths, and the same three depths mixed
# with the dry as chorus. The digit is the depth index; the letter is
# whether the dry comes along.
ORGAN_VIBRATO = ("off", "v1", "v2", "v3", "c1", "c2", "c3")


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
            "vibrato": "off",
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
        vibrato: The scanner, off | v1 | v2 | v3 | c1 | c2 | c3.
            Default off.
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
