"""Compressor — a feed-forward dynamics processor with external sidechain.

Turns down the loud parts. The compressor watches a *detector* signal,
and whenever it rises above the ``threshold`` it pulls the gain down by a
fraction set by ``ratio`` — 2:1 halves every dB over the line, 20:1 is
effectively a limiter. ``attack`` and ``release`` set how fast the gain
chases the level (fast attack catches transients, slow release breathes),
``knee`` softens the bend around the threshold so compression eases in
instead of switching on, and ``gain`` is the make-up boost that brings the
now-quieter signal back up to level. ``mix`` blends the compressed signal
back against the dry one for **parallel** (New York) compression — squash
hard, then dial it in under the untouched original for density without
losing life.

**Sidechain.** The detector normally listens to the input itself
(feed-forward), but patch a signal into ``sidechain`` and it listens to
*that* instead while still gain-controlling ``in``. That is how you get
ducking: send a kick drum into a pad's ``sidechain`` and the pad pumps
down on every kick, carving a hole in the mix for the low end. Unpatched,
``sidechain`` is normalled to ``in`` (the ordinary compressor).

**Detector.** ``peak`` follows the instantaneous rectified level (catches
every transient — snappy, aggressive); ``rms`` averages energy over a
~10 ms window (hears loudness the way the ear does — smoother, more
musical). The attack/release smoothing lives on the *gain*, so switching
detector changes what the compressor reacts to, not how fast the gain
moves.

The ``gr`` output is the applied gain reduction as a control voltage
(0 = untouched, down toward −1 = fully pulled down): patch it into
another module's CV to duck it in lock-step with this one, or into a
meter to watch the compressor work.

Use cases:
  * Even out a bass or vocal: ``rms``, ratio ~3, soft knee, make-up to
    taste.
  * Sidechain pump: kick → ``sidechain`` of a pad/bass compressor for the
    classic ducking groove.
  * Parallel drum smash: ratio high, fast attack, ``mix`` ~0.3 under the
    dry kit.
  * Limiter: ratio 20, fast attack, low threshold to catch peaks.
  * Ducking bus: drive ``gr`` into a VCA or ``*_cv`` elsewhere so a whole
    group ducks with the compressed track.

Ports:
  * ``in`` (audio): the signal to compress. Unpatched -> silence out.
  * ``sidechain`` (audio): external detector key; normalled to ``in`` when
    unpatched.
  * ``threshold_cv`` (cv): added to ``threshold`` (block-meaned), scaled by
    ``threshold_cv_depth`` (dB per CV unit). Optional.
  * ``out`` (audio): the compressed (and optionally parallel-mixed) signal.
  * ``gr`` (cv): applied gain reduction, ``applied_gain - 1`` (0..−1).

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` input ->
  ``(F,)`` out through one detector + gain smoother; a voice-aware
  ``(V, F)`` input -> ``(V, F)`` out with per-voice detector and gain
  state, so a polyphonic source compresses without cross-talk. A mono
  ``sidechain`` broadcasts across the voices (one key for all — the usual
  ducking case); a ``(V, F)`` sidechain keys each voice independently.
  A single voice row is bit-identical to the mono path.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Detector families offered in the UI combo. ``peak`` = instantaneous
# rectified level (transient-accurate); ``rms`` = ~10 ms energy average
# (loudness-like, smoother).
DETECTOR_MODES = ("peak", "rms")


@register_module_type
class Compressor(Module):
    """Feed-forward compressor with external sidechain and a GR CV out.

    Parameters:
        threshold: Level above which compression starts, in dBFS
            (−60..0). Lower = more of the signal compressed.
        ratio: Compression ratio (1..20). 1 = off (no reduction), 4 =
            4:1, 20 ~= brickwall limiting.
        attack: Time for the gain to move most of the way toward a
            deeper reduction, in ms (0.1..250). Fast catches transients.
        release: Time for the gain to recover toward less reduction, in
            ms (5..2500). Slow = smooth, pumping; fast = lively.
        knee: Soft-knee width in dB (0..24) centred on the threshold.
            0 = hard knee (abrupt); larger eases compression in.
        gain: Make-up gain in dB (0..24) applied after compression.
        mix: Dry/wet blend (0..1). 1 = fully compressed; <1 = parallel
            compression (dry blended back in).
        detector: ``peak`` or ``rms`` (~10 ms window) level detection.
        threshold_cv_depth: dB of threshold shift per unit of
            ``threshold_cv``.

    Ports:
        in (in, audio): signal to compress. Unpatched -> silence.
        sidechain (in, audio): external key; normalled to ``in``.
        threshold_cv (in, cv): added to ``threshold`` (× depth).
        out (out, audio): compressed / parallel-mixed signal.
        gr (out, cv): applied gain reduction, ``applied_gain - 1`` (0..−1).
    """

    TYPE = "compressor"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "threshold": -18.0,
        "ratio": 2.0,
        "attack": 10.0,
        "release": 120.0,
        "knee": 6.0,
        "gain": 0.0,
        "mix": 1.0,
        "detector": "rms",
        "threshold_cv_depth": 12.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("sidechain", "in", "audio"),
        Port("threshold_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("gr", "out", "cv"),
    ]
