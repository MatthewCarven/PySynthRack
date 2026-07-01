"""MotionEQ — a 4-band parametric EQ whose band centres you sweep with CV.

The full "animated EQ": four peaking bells, each with its own centre
frequency, gain and Q like :class:`ParametricEQ` — but every band also
has a dedicated CV input (``band1_freq_cv`` … ``band4_freq_cv``) that
sweeps *that band's centre frequency*. Patch four LFOs (or envelopes, a
sequencer, a keyboard) in and you get four peaks/notches gliding
independently around the spectrum — spectral motion you can't get from a
static EQ.

Where the siblings sit:
  * :class:`ParametricEQ` — the static four-band base (no CV). Reach for
    it when you want a fixed tone shape.
  * :class:`SweepEQ` — one swept resonant band (auto-wah), switchable
    band-pass / low-pass / peak.
  * MotionEQ — four independently swept peaking bells at once.

Each band is a Robert Bristow-Johnson peaking biquad; the four run in
series (a cascade), reusing ParametricEQ's exact coefficient math and
DF-I state, so a band left at 0 dB is *exactly* transparent (unused
bands cost nothing) and the sound matches ParametricEQ when no CV moves.

CV: each ``band{i}_freq_cv`` sweeps band *i*'s centre 1 V/oct — the
centre is ``band{i}_freq * 2 ** (cv_depth * mean(band{i}_freq_cv))``,
block-meaned like the Crossover / mod-FX. A single **shared** ``cv_depth``
(octaves per CV unit) scales all four sweeps; per-band sensitivity is
still reachable by putting a :class:`CVScale` on any individual CV input.
Gain and Q stay static params (freq is the animated dimension). Leave a
band's ``freq_cv`` unpatched and that band stays at its static centre.

Shape-polymorphic (mono ``(F,)`` and per-voice ``(V, F)``, each voice its
own biquad memory); centres are one coefficient set per block shared
across voices (a macro sweep), like the Crossover.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Band count. Backend and UI both derive the band list by walking
# ``band{i}_*`` params / ``band{i}_freq_cv`` ports, so this is the only
# number to change to add/remove bands.
EQ_BANDS = 4

# Default centres spread across the spectrum (a general-purpose animated
# EQ, not the bass-focused ParametricEQ default). Gains start at 0 dB
# (flat/transparent) — dial in gain to make the moving bands audible.
_DEFAULT_FREQS = (120.0, 500.0, 1800.0, 6000.0)


def _default_params() -> dict[str, float]:
    params: dict[str, float] = {}
    for i, f in enumerate(_DEFAULT_FREQS, start=1):
        params[f"band{i}_freq"] = float(f)
        params[f"band{i}_gain"] = 0.0  # dB, flat
        params[f"band{i}_q"] = 1.0
    params["cv_depth"] = 1.0  # octaves per CV unit, shared by all bands
    return params


_INPUT_PORTS = [Port("in", "in", "audio")] + [
    Port(f"band{i}_freq_cv", "in", "cv") for i in range(1, EQ_BANDS + 1)
]


@register_module_type
class MotionEQ(Module):
    """4-band peaking EQ with a per-band centre-frequency CV input.

    Parameters (per band ``i`` in 1..4):
        band{i}_freq: Centre frequency in Hz (20 … 0.45·sample-rate),
            the value a static band sits at and the base the CV sweeps
            around.
        band{i}_gain: Boost/cut in dB (0 = flat/transparent).
        band{i}_q:    Q factor (band width).
    Plus one shared:
        cv_depth: Octaves each ``band{i}_freq_cv`` sweeps its band's
            centre per CV unit (1 V/oct). Default 1.0.

    Ports:
        in (in, audio): the signal to EQ.
        band{i}_freq_cv (in, cv): sweeps band i's centre; optional.
        out (out, audio): the equalised signal.
    """

    TYPE = "motion_eq"
    EQ_BANDS = EQ_BANDS
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = _INPUT_PORTS
    OUTPUT_PORTS = [Port("out", "out", "audio")]
