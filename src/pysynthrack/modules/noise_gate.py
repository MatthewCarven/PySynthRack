"""Noise gate — a hold-and-hysteresis downward gate with a gate-CV out.

The opposite move from the compressor: instead of turning the loud parts
down, the gate turns the *quiet* parts down (or off). It watches a
detector (an envelope follower on the key), and while that level sits
above the ``threshold`` the gate is **open** and the signal passes
untouched; when the level falls away the gate **closes** and the output
is pulled down to the ``range`` floor. That is how you kill the hiss,
hum, amp buzz, or headphone bleed in the gaps between notes, tighten a
boomy drum by chopping its tail, or turn a sustained pad into staccato
stabs.

**Hysteresis (no chatter).** A single-threshold gate chatters: a signal
hovering right at the line flips the gate open/closed dozens of times a
second, an ugly stutter. The fix is two thresholds — a Schmitt trigger.
The gate *opens* when the level rises above ``threshold`` but only
*closes* once it falls ``hysteresis`` dB below that. Inside the band the
gate holds its state, so a signal parked at the boundary stays put.

**Hold.** Even with hysteresis, a signal that dips briefly (the quiet
moment inside a word, the gap between two hits of a roll) would snap the
gate shut and swallow the next attack. ``hold`` keeps the gate open for
a minimum time after the level drops below the close threshold, so short
dips are bridged; only a gap longer than ``hold`` actually closes it.

**Attack / release.** Once the open/closed decision is made, the gain
doesn't jump — it ramps. ``attack`` is how fast the gate opens (fast, so
transients aren't dulled) and ``release`` how fast it closes (slower, so
tails fade instead of clicking off). ``range`` sets how far down a
*closed* gate pulls: −80 dB is a full mute (silence in the gaps), while
a shallower value (say −12 dB) only ducks the noise floor — an
**expander**-style gentle gate that never fully cuts.

**Sidechain.** The detector normally listens to the input itself, but
patch a signal into ``sidechain`` and the gate opens and closes on
*that* while still gating ``in``. Key a pad off a hi-hat for rhythmic
chops, or off a kick so the bass only speaks on the beat. Unpatched,
``sidechain`` is normalled to ``in`` (the ordinary gate).

**The ``open`` output.** A 0/1 control voltage that is high exactly when
the gate is open — a free gate-signal *extractor*. Run any audio through
the noise gate and ``open`` becomes a clean gate that tracks its
dynamics: drive an ADSR, a VCA, a clock's reset, or another module's CV
so the rest of the patch plays in lock-step with "is the signal
present". A crude beat detector, an auto-rhythm generator, an audio →
trigger bridge.

Use cases:
  * Silence the hiss/hum between phrases on a vocal, guitar, or synth.
  * Tighten drums: short ``hold`` + fast ``release`` chops the room tail.
  * Expander: ``range`` −6..−12 dB gently ducks the noise floor without
    the hard on/off of a full-mute gate.
  * Sidechain gate: key a sustained sound off a rhythmic one for
    stutter/trance-gate effects.
  * Envelope/trigger extraction: ``open`` → ADSR gate / VCA / clock for
    self-playing patches driven by an audio signal's dynamics.

Ports:
  * ``in`` (audio): the signal to gate. Unpatched -> silence out.
  * ``sidechain`` (audio): external detector key; normalled to ``in``
    when unpatched.
  * ``out`` (audio): the gated signal.
  * ``open`` (cv): 0.0 / 1.0, high while the gate is open (a free
    gate-extractor for generative patching).

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` input ->
  ``(F,)`` out through one detector + gate state machine + gain smoother;
  a voice-aware ``(V, F)`` input -> ``(V, F)`` out with per-voice
  detector, Schmitt/hold and gain state, so each voice gates on its own
  dynamics without cross-talk. A mono ``sidechain`` broadcasts across the
  voices (one key for all); a ``(V, F)`` sidechain keys each voice
  independently. ``open`` matches the input's shape. A single voice row
  is bit-identical to the mono path.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class NoiseGate(Module):
    """Hold-and-hysteresis downward gate with a gate-CV output.

    Parameters:
        threshold: Open level in dBFS (−80..0). The gate opens when the
            detector rises above this. At its minimum (−80) the gate is
            always open — a bit-exact bypass.
        hysteresis: Schmitt gap in dB (0..24). The gate closes only once
            the detector falls this far below ``threshold``; the gap
            stops a boundary-level signal chattering.
        attack: Open ramp time in ms (0.1..50). Fast so transients keep
            their edge.
        hold: Minimum open time in ms (0..500) after the level drops
            below the close threshold, so brief dips don't snap the gate
            shut.
        release: Close ramp time in ms (5..2000). Slower than attack so
            tails fade instead of clicking off.
        range: Closed-gate floor in dB (−80..0). −80 = full mute; a
            higher value only ducks (expander-style), never fully cuts.

    Ports:
        in (in, audio): signal to gate. Unpatched -> silence.
        sidechain (in, audio): external key; normalled to ``in``.
        out (out, audio): the gated signal.
        open (out, cv): 0.0 / 1.0, high while the gate is open.
    """

    TYPE = "noise_gate"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "threshold": -45.0,
        "hysteresis": 4.0,
        "attack": 1.0,
        "hold": 40.0,
        "release": 150.0,
        "range": -80.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
        Port("sidechain", "in", "audio"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
        Port("open", "out", "cv"),
    ]
