"""Ring modulator — metallic, clangorous amplitude cross-multiplication.

A ring modulator multiplies two signals sample-by-sample. Where a
[`vca`](#vca) multiplies audio by a (mostly positive) control envelope,
a ring mod multiplies audio by another *bipolar audio* signal — the
**carrier** — and the result keeps only the **sum and difference**
frequencies of the two inputs, none of the originals. Feed a 200 Hz tone
a 440 Hz carrier and you hear 240 Hz and 640 Hz (and the sums/differences
of their harmonics), not 200 or 440. Because those sidebands are almost
never harmonically related to the input, the ear reads the output as
**inharmonic** — bells, gongs, robot voices, the Dalek growl,
detuned-metal clang.

The carrier is either **external** — patch any audio source into
``carrier`` and the module multiplies the two cables, classic
two-oscillator ring mod — or, when ``carrier`` is left unpatched, an
**internal sine** at ``freq``. The internal carrier is a per-voice
phase-accumulated sine (1 V/oct pitch modulation via ``freq_cv`` scaled
by ``freq_cv_depth``), so the module is self-contained: drop it after an
oscillator, dial ``freq``, and get the metallic timbre with no extra
patching.

``mix`` blends the dry input against the modulated output; at
``mix = 0`` the output is a **bit-exact dry passthrough**, so the knob
sweeps cleanly from clean to fully ring-modulated.

Controls:
  * ``freq`` — internal carrier pitch in Hz (1 .. 5000). Ignored while
    ``carrier`` is patched. Low carriers (< ~30 Hz) give tremolo/growl;
    higher carriers throw the sidebands wide for clangorous metal.
  * ``freq_cv_depth`` — octaves of internal-carrier pitch shift per unit
    of ``freq_cv`` (1 V/oct at 1.0).
  * ``mix`` — dry/wet, 0 (bit-exact dry) .. 1 (fully modulated).

A ``freq_cv`` input sweeps the internal carrier (1 V/oct * ``freq_cv_depth``),
so an envelope or LFO makes the metallic pitch move — evolving, vocal
ring-mod. It is bypassed while an external ``carrier`` is patched (sweep
that source's own pitch instead).

Ports:
  * ``in`` (audio): the signal to modulate. Voice-aware; a single voice
    row is bit-identical to the mono render. Unpatched -> silence.
  * ``carrier`` (audio): external carrier. Unpatched -> internal sine at
    ``freq``. Patched -> the two cables multiply and ``freq`` /
    ``freq_cv`` are bypassed.
  * ``freq_cv`` (cv): 1 V/oct pitch modulation of the internal carrier,
    scaled by ``freq_cv_depth``. Optional.
  * ``out`` (audio): ``in`` x carrier, blended with dry by ``mix``.

Use cases:
  * Bells / mallets: a decaying sine (or pluck) into ``in``, a fixed
    ``freq`` around 200–900 Hz — instant tuned metal.
  * Robot voice: speech / a vocal sample into ``in``, ``freq`` ~ 50–150 Hz.
  * Two-oscillator clang: a second [`oscillator`](#oscillator) into
    ``carrier`` and sweep its pitch for evolving inharmonic sweeps.

Pairs with the planned ``fm_op`` and ``modal`` modules as the metallic /
inharmonic corner of the effects rack.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class RingMod(Module):
    """Ring modulator: ``out = in x carrier`` (internal sine or external).

    Parameters:
        freq: Internal carrier frequency in Hz (1 .. 5000). Only used
            when ``carrier`` is unpatched.
        freq_cv_depth: Octaves of internal-carrier pitch shift per unit
            of ``freq_cv`` (1 V/oct at 1.0).
        mix: Dry/wet balance, dry (0) -> modulated (1). 0 is a bit-exact
            dry passthrough.

    Ports:
        in (in, audio): signal to modulate. Unpatched -> silence.
        carrier (in, audio): external carrier; unpatched -> internal
            sine at ``freq``.
        freq_cv (in, cv): 1 V/oct internal-carrier pitch mod
            (x ``freq_cv_depth``). Bypassed while ``carrier`` is patched.
        out (out, audio): modulated (and dry-blended) signal.
    """

    TYPE = "ring_mod"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "freq": 440.0,
        "freq_cv_depth": 1.0,
        "mix": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("carrier", "in", "audio"),
        Port("freq_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
