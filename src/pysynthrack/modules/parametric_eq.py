"""ParametricEQ — a 4-band parametric equaliser (peaking bells).

One mono audio input, one mono audio output. Four independent peaking
("bell") bands, each with its own centre frequency, gain, and Q. Unlike
a fixed graphic EQ, every band's centre is a free parameter you can
sweep anywhere across the spectrum — so the same module covers a
low-end shelf-style scoop (the default 25/50/100/250 Hz layout) or a
wide-open four-point tone sculpt up at presence frequencies.

How it works: each band is a Robert Bristow-Johnson peaking biquad. The
four biquads run in series (cascade) in the numpy backend; a band left
at 0 dB has identity coefficients and is therefore *exactly*
transparent, so unused bands cost nothing tonally. Gain is in decibels
(0 = flat, + boosts, − cuts), and Q sets how wide each bell is — low Q
is a broad gentle tilt, high Q is a narrow surgical notch/peak.

Tone-wise:
  * Four low bands (the 25/50/100/250 Hz defaults) = bass-shaping EQ:
    rumble control, kick weight, low-mid mud.
  * Spread the centres across the spectrum for a general tone control.
  * High-Q cut on a single band = de-resonate a ringing source.

Centre frequencies are clamped to (20 Hz, 0.45·sample_rate) and Q to
(0.1, 20) by the renderer to keep the biquads numerically stable across
param edits — the same discipline as the Filter and Crossover modules.

There are no CV inputs yet: bands are set by parameter, like Crossover.
(Per-band freq/gain CV could follow if a patch wants animated EQ.)
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Number of bands. The backend and UI both derive the band list by
# walking ``band{i}_*`` params, so bumping this (and DEFAULT_PARAMS) is
# the only edit needed to add/remove bands.
EQ_BANDS = 4

# Default centres: a low-end-focused layout (Matthew's brief — bass
# sculpting around 25/50/100/250 Hz), but every centre sweeps the full
# 20 Hz–20 kHz range.
_DEFAULT_FREQS = (25.0, 50.0, 100.0, 250.0)


def _default_params() -> dict[str, float]:
    params: dict[str, float] = {}
    for i, f in enumerate(_DEFAULT_FREQS, start=1):
        params[f"band{i}_freq"] = float(f)
        params[f"band{i}_gain"] = 0.0  # dB, flat
        params[f"band{i}_q"] = 1.0
    return params


@register_module_type
class ParametricEQ(Module):
    """4-band peaking parametric EQ (mono in/out).

    Parameters (per band ``i`` in 1..4):
        band{i}_freq: Centre frequency in Hz (20 … 0.45·sample-rate).
        band{i}_gain: Boost/cut in dB (0 = flat/transparent).
        band{i}_q:    Q factor (band width); ~0.7 broad, high = narrow.
    """

    TYPE = "parametric_eq"
    CATEGORY = "Filters & EQ"
    EQ_BANDS = EQ_BANDS
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
