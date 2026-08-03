"""Modal — a struck/blown resonator bank (bars, bells, membranes, strings).

Physical-modelling's other half: where :class:`Pluck` is a string you
pluck, Modal is a *body* you strike. Feed anything into ``excite`` — a
``burst`` of noise, a ``cv_gates`` envelope through ``cv_to_audio``, a
drum click, even a full mix — and a bank of two-pole resonators rings at
``pitch × ratio[i]`` like the modes of a real vibrating object. The mode
ratios come from the physics textbooks, not any product's tables:

  * ``bar`` — a free-free bar (xylophone/marimba family): mode
    frequencies ∝ β_k², the classic 1 : 2.76 : 5.40 : 8.93 series.
  * ``bell`` — the stylized minor-third bell stack (hum ½, prime 1,
    tierce 1.2, quint 1.5, nominal 2 …), extended upward with a gentle
    stretch.
  * ``membrane`` — a circular drumhead: ratios are Bessel-function zeros
    (1 : 1.59 : 2.14 : 2.30 …), computed, not tabulated.
  * ``string`` — plain harmonics 1..N (with ``inharm`` for piano-style
    stretch).

``modes`` sets how many resonators ring (4..24). ``decay`` is the t60 of
the lowest mode; ``decay_tilt`` makes higher modes die faster (0 =
uniform — glassy; 1 = strongly damped highs — woody). ``brightness``
tilts the mode gains (0 dark … 1 trebly). ``inharm`` stretches the ratio
table upward — a little turns a string piano-ish, a lot turns anything
into a clangorous plate.

Voice-aware: a ``(V, F)`` excite (or pitch) gives every voice its own
resonator bank; a mono excite striking per-voice pitches broadcasts —
``cv_gates``' seventeen enveloped strikes into seventeen bells is the
killer patch. Voices sharing a pitch share filter coefficients and are
batched into single vectorized filter calls (the slice-4 lfilter
pattern); silent, rung-out voices early-out. Pitch is read per block
(mean), 1 V/oct with C4 = 0 V; unpatched pitch rings at C4. Modes that
would land above ~0.45·sr are dropped, not aliased. Numpy backend only;
silent stub under pyo.

Ports:
  * ``excite`` (audio): the strike/breath. Unpatched → silence.
  * ``pitch_cv`` (cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
  * ``out`` (audio): the ringing body.

Params:
  * ``material``: ``bar`` | ``bell`` | ``membrane`` | ``string``.
  * ``modes``: resonator count, 4..24. Default 12.
  * ``decay``: lowest-mode t60 in seconds, 0.1..30. Default 2.
  * ``decay_tilt``: how much faster high modes die, 0..1. Default 0.5.
  * ``brightness``: mode-gain tilt, 0 dark .. 1 bright. Default 0.5.
  * ``inharm``: ratio stretch, 0..1. Default 0.
  * ``level``: output level. Default 0.5.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from ..core.module import Module, register_module_type
from ..core.port import Port

MODAL_MATERIALS = ("bar", "bell", "membrane", "string")

MODAL_MAX_MODES = 24

# Free-free bar: the first β_k·L solutions of cos(βL)·cosh(βL) = 1;
# asymptotically (k + 1.5)π. Mode frequencies scale as β².
_BAR_BETAS = (4.7300407, 7.8532046, 10.9956078, 14.1371655, 17.2787597)

# Stylized minor-third bell partials (textbook idealization): hum, prime,
# tierce, quint, nominal, then upper partials continuing the series.
_BELL_BASE = (0.5, 1.0, 1.2, 1.5, 2.0, 2.5, 2.67, 3.0, 3.35, 4.0)


@lru_cache(maxsize=64)
def modal_ratios(material: str, count: int) -> tuple[float, ...]:
    """The first ``count`` mode-frequency ratios for a material (r₀ = ...).

    Every table is normalized so the LOWEST mode is ratio 1.0 (the bell's
    hum note included — its prime then sits at 2.0 internally, keeping
    ``pitch_cv`` attached to the lowest audible mode like the others).
    Cached; pure.
    """
    count = max(1, min(MODAL_MAX_MODES, int(count)))
    if material == "bar":
        betas = list(_BAR_BETAS)
        k = len(betas)
        while len(betas) < count:
            betas.append((k + 1.5) * np.pi)
            k += 1
        r = np.array(betas[:count]) ** 2
    elif material == "bell":
        vals = list(_BELL_BASE)
        step, n = 0.5, 0
        while len(vals) < count:
            # continue upward with a gently stretching spacing
            vals.append(vals[-1] + step + 0.06 * n)
            n += 1
        r = np.array(vals[:count])
    elif material == "membrane":
        from scipy.special import jn_zeros

        zeros: list[float] = []
        for m in range(0, 9):
            zeros.extend(jn_zeros(m, 8).tolist())
        zeros.sort()
        r = np.array(zeros[:count])
    else:  # "string" (and any unknown value)
        r = np.arange(1, count + 1, dtype=np.float64)
    r = r / r[0]
    return tuple(float(x) for x in r)


@register_module_type
class Modal(Module):
    """Resonator bank: strike ``excite``, ring at ``pitch × ratio[i]``.

    Parameters:
        material: ``bar`` | ``bell`` | ``membrane`` | ``string``.
        modes: Resonator count, 4..24. Default 12.
        decay: Lowest-mode t60 (seconds), 0.1..30. Default 2.
        decay_tilt: Higher modes die faster, 0..1. Default 0.5.
        brightness: Mode-gain tilt, 0 dark .. 1 bright. Default 0.5.
        inharm: Ratio stretch, 0..1. Default 0.
        level: Output level. Default 0.5.

    Ports:
        excite (in, audio): the strike. Unpatched → silence.
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        out (out, audio): the ringing body.
    """

    TYPE = "modal"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "material": "bar",
        "modes": 12,
        "decay": 2.0,
        "decay_tilt": 0.5,
        "brightness": 0.5,
        "inharm": 0.0,
        "level": 0.5,
    }
    INPUT_PORTS = [
        Port("excite", "in", "audio"),
        Port("pitch_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
