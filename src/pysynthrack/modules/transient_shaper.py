"""Transient shaper — attack/sustain rebalance, threshold-free.

A transient shaper reshapes the *dynamic envelope* of a sound without
caring how loud it is. Push ``attack`` up and every note's onset snaps
harder — the pluck of a string, the click of a kick, the pick of a
guitar — while a cut softens those same attacks. Push ``sustain`` up and
the body and tail bloom — the ring of a snare, the room on a drum kit,
the decay of a piano — while a cut dries them out, tightening a boomy
kit or shortening a reverb tail without a gate. It is the go-to tool for
"more snap" / "less room" moves that a compressor can only fake.

**Threshold-free (the classic trick).** Unlike a compressor or gate,
there is *no threshold* to set and the effect is **level-independent** —
a quiet ghost note is shaped exactly like a loud accent, and turning the
input up or down doesn't change what the shaper does. The mechanism is a
pair of envelope followers on the rectified input, one **fast** and one
**slow**. Their *difference in dB* isolates the transient: when a note
attacks, the fast follower leaps ahead of the slow one (difference goes
**positive** → an attack is happening); as the note decays, the fast
follower drops below the slow one (difference goes **negative** → this is
sustain). In steady state the two followers agree and the difference is
zero, so a held tone is left untouched. Because a dB *difference* is a
ratio, it is the same whatever the absolute level — that is what makes
the shaper threshold-free and level-invariant.

The positive part of that difference drives the ``attack`` gain and the
negative part drives the ``sustain`` gain; the two are summed in dB,
smoothed, and multiplied back onto the signal. ``speed`` sets how quick
the follower pair is — ``fast`` for tight percussion, ``slow`` for
sustained or bass material, ``med`` in between.

Use cases:
  * Add snap to a drum loop (``attack`` up) or tame a clicky kick
    (``attack`` down).
  * Dry out an over-roomy kit or shorten a ringing snare (``sustain``
    down) — a gate-free "less room" move.
  * Bring out the body of a bass or pad (``sustain`` up) without a
    compressor's pumping.
  * Reshape a sampled loop's groove: emphasise or soften its hits
    independently of their level.

Ports:
  * ``in`` (audio): the signal to shape. Unpatched -> silence out.
  * ``out`` (audio): the reshaped signal.

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` input ->
  ``(F,)`` out through one follower pair + gain smoother; a voice-aware
  ``(V, F)`` input -> ``(V, F)`` out with per-voice follower and gain
  state, so each voice is shaped on its own dynamics without cross-talk.
  A single voice row is bit-identical to the mono path. With ``attack``
  and ``sustain`` both 0 the module short-circuits to a bit-exact
  passthrough (the followers are skipped).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Follower-pair responsiveness offered in the UI combo. Each entry is the
# ``(fast_ms, slow_ms)`` time-constant pair the detector uses; the dB
# difference of the two followers is the transient-isolating signal.
TRANSIENT_SHAPER_SPEEDS = ("fast", "med", "slow")


@register_module_type
class TransientShaper(Module):
    """Attack/sustain rebalance via a fast/slow envelope-follower pair.

    Parameters:
        attack: Attack-portion gain in the range −1..+1 (0 = untouched).
            +1 boosts note onsets by up to +12 dB, −1 cuts them by up to
            −12 dB. Acts only where the signal is transient (the fast
            follower leads the slow one).
        sustain: Sustain-portion gain in the range −1..+1 (0 = untouched).
            +1 lifts the body/tail by up to +12 dB, −1 dries it by up to
            −12 dB. Acts only where the signal is decaying (the fast
            follower trails the slow one).
        speed: Follower-pair responsiveness — ``fast`` (tight
            percussion), ``med`` (general), or ``slow`` (bass / sustained
            material).

    Ports:
        in (in, audio): signal to shape. Unpatched -> silence.
        out (out, audio): reshaped signal.
    """

    TYPE = "transient_shaper"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "attack": 0.0,
        "sustain": 0.0,
        "speed": "med",
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
    ]
