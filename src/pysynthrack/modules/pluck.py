"""Pluck — an extended Karplus–Strong plucked string voice.

The classic: a delay line the length of one pitch period, fed back through
a gentle lowpass, excited by a short noise burst — physics-adjacent string
synthesis for the price of a ring buffer. Each ``trigger`` rising edge
plucks the string at the pitch on ``pitch_cv`` (1 V/oct, C4 = 0 V, the
``fm_op`` convention); polyphonic sources give **one independent string
per voice** — a ``cv_keyboard`` or MIDI keyboard turns it into a 16-string
instrument with zero extra patching.

The extensions over textbook KS:

  * **In-tune across the range** — the loop uses an allpass fractional
    delay plus compensation for the damping filter's phase delay, so the
    pitch lands within a few cents rather than quantizing to whole-sample
    loop lengths (the naive KS goes audibly sharp/flat above ~500 Hz).
  * **``decay`` in seconds** — the loop gain is derived from the pitch so
    ``decay`` reads as a real t60: a 2 s setting rings ~2 s whether you
    pluck C2 or C6 (textbook KS rings longer the lower the note).
  * **``damping``** — how dark the string is: 0 leaves the loop wide open
    (bright, wiry, long high partials), 1 is the full classic two-point
    average (each pass rolls off highs — nylon-ish).
  * **``color``** — the exciter's spectrum, noise → pick: 0 lowpasses the
    burst (soft thumb), 1 injects raw white noise (hard plectrum).
  * **``position``** — pick position as a comb on the exciter: the burst
    is delayed-and-subtracted by ``position``·period, notching the
    harmonics a real pluck at that spot along the string cancels. 0
    disables the comb. Small is near the bridge (the first null
    sits at ``f0/position``, far up — bright and nasal); 0.5 is
    the middle of the string, where the first null lands on the
    second harmonic and the fundamental dominates — the dullest,
    roundest pluck.

Re-plucking a ringing string **adds** the new burst into the loop instead
of replacing it — physical (the string was still moving) and click-free;
with ``carry`` on (the default) it is exact superposition, see below.
Every burst is seeded from (module, voice, hit number), so renders are
deterministic and testable sample-for-sample.

**Velocity.** ``vel`` is a knobless multiplier on the burst — the house
``vel`` convention (the drums, the sampler, the adsr): the bus is read
**at the trigger's rising-edge sample** and latched into that hit, so a
velocity that moves mid-note changes nothing until the next pluck, and
``scale = max(0, vel[edge])``. Only the burst scales; the loop is
untouched, so a soft hit is a quieter pluck that rings down in exactly
the same time — how a real string behaves when picked gently.
``midi_input.velocity_cv → vel`` is the point: a harder key plays
louder, per voice. A non-positive velocity is a **silent hit** — the
string keeps ringing exactly as it was (no burst, and none of the pitch
relock a hit performs, nor the allpass clear it performs with ``carry``
off, which alone would tick audibly); the hit counter still advances.
Unpatched, the render is bit-for-bit what it was before the input
existed.

**Velocity colour.** A real pick's *spectrum* follows the velocity too
— a gentle stroke is duller as well as quieter — and ``vel_color``
(0..1, default 0) is how much of that the string does. The effective
colour of a hit is ``clamp(color + vel_color · (vel − 1), 0, 1)``,
computed at the same edge and from the same latched velocity the burst
is scaled by, so it is per voice and per hit and never moves mid-note.
The formula is anchored at velocity **1.0**: a full hit (or an
unpatched ``vel``) is exactly ``color`` whatever the knob says, so
``vel_color`` cannot change the sound of a patch with no velocity
source — it only ever *dulls* a softer hit, in proportion (at
``vel_color`` 1 and ``color`` 0.8, a 0.3-velocity pick is coloured
0.1; a hit above 1.0 brightens, to the clamp). Only the exciter's
lowpass sees it: the loop — decay, damping, tuning — is untouched (t60
identical at ``damping`` 0, measured), so a soft hit rings down the
same way; at higher damping a duller burst simply has less HF for the
loop to eat, which is the string being a string.

**Velocity position.** A gentle pluck also lands somewhere *else*:
a finger falls nearer the middle of the string, a hard plectrum
bites near the bridge. ``vel_position`` (0..1, default 0) is how
much of that the string does — the hit's pick position is
``clamp(position + vel_position · (1 — vel) · (0.5 — position), 0, 1)``,
an interpolation from ``position`` towards **0.5** by
``vel_position·(1 — vel)`` of the way, latched at the edge from the
same velocity the burst rides. 0.5 is the middle of the string: the
comb's first null lands on the second harmonic, so the fundamental
dominates and the even partials go — round and woody. Anchored at
velocity **1.0** like ``vel_color``, so a full hit (or an unpatched
``vel``) is exactly ``position`` whatever the knob says; a hit above
1.0 slides the other way, towards the bridge, to the clamp.
``position`` 0 is the comb's documented OFF and stays off — the knob
moves a pick, it does not fit one. With ``vel_color`` up too a soft
note is quieter, duller **and** rounder while an accent stays loud,
bright and plucky. The loop is untouched, as always: same t60.

**``carry`` (default on): a hit keeps the string's allpass state.**
Every hit relocks the pitch; with ``carry`` on it keeps the loop's
fractional-delay (allpass) state through the hit, so the relock is
bit-identical to the block-mean pitch follow the string does between
hits anyway, the step at the hit is **exactly 0**, and a re-pluck is
**exact superposition** — two hits minus one hit is the second hit
alone to one float32 ulp, the property this module has always
claimed. Untick it for the old behaviour (the default until
2026-09-24): every hit **clears** that state, and measured with a
zero burst the clear *alone* steps the output by **51%** of the ring
at the same pitch (51.4% into G4, 51.5% into C5 at the same instant;
28% at another re-pluck, 7-18% on the velocity pass's hits -- it
throws away one sample of loop state, and how big that sample is
depends on where in the waveform the hit lands): a slightly harder,
less even re-attack. Tuning, peak and stability are identical either
way (the loop's allpass always has |c| < 1, so a carried state
decays rather than accumulating). A patch that stores
``"carry": false`` -- including one saved from the app while the
old default stood, since the app saves every param -- keeps the old
sound.

Voice-awareness: shape follows the inputs — mono ``(F,)`` in gives mono
out, ``(V, F)`` gives per-voice strings with no crosstalk. ``vel``
follows the same rule: a ``(V, F)`` bus latches per voice from its own
row, a mono bus is shared by every string, and a ``(V, F)`` bus on a
mono pluck collapses to the loudest voice at the edge (an idle slot's
velocity is 0, so the max is the key that was struck). Silent voices
early-out (a decayed string costs nothing until re-plucked). Pitch is
read per block (mean), so slides/vibrato track at block rate; the pluck
pitch itself is locked from the trigger sample. Numpy backend only;
silent stub under pyo.

Ports:
  * ``pitch_cv`` (cv): 1 V/oct pitch, C4 = 0 V. Unpatched → C4.
  * ``trigger`` (gate): pluck on each rising edge. No trigger, no sound.
  * ``vel`` (cv): burst multiplier, read at the edge and latched;
    ``max(0, ·)``, ≤ 0 is a silent hit. Unpatched → 1 (old code path).
  * ``out`` (audio): the string.

Params:
  * ``decay``: t60-style ring time in seconds, 0.1..30. Default 2.
  * ``damping``: loop lowpass blend 0..1. Default 0.5.
  * ``color``: exciter spectrum, 0 soft .. 1 bright. Default 0.7.
  * ``vel_color``: how much a soft hit darkens ``color``, 0..1; a hit
    at velocity 1.0 is always exactly ``color``. Default 0 (off).
  * ``position``: pick-position comb, 0 off .. 1. Default 0.2.
  * ``vel_position``: how far a soft hit's pick slides towards the
    middle of the string (0.5), 0..1; a hit at velocity 1.0 is
    always exactly ``position``, and ``position`` 0 stays off.
    Default 0 (off).
  * ``carry``: keep the loop's allpass state through a hit, so a
    re-pluck is exact superposition. Default True; False is the
    old behaviour -- every hit clears it, a 51%-of-the-ring step.
  * ``level``: output level 0..1. Default 0.5.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Pluck(Module):
    """Karplus–Strong string: noise burst into a tuned feedback loop.

    Parameters:
        decay: Ring time (t60-ish) in seconds, 0.1..30. Default 2.
        damping: Loop lowpass blend, 0 (bright/wiry) .. 1 (classic
            two-point average, nylon-ish). Default 0.5.
        color: Exciter spectrum, 0 (lowpassed thumb) .. 1 (white-noise
            plectrum). Default 0.7.
        vel_color: How much a soft hit darkens ``color``, 0..1 -- the
            hit's colour is ``clamp(color + vel_color * (vel - 1))``;
            a full-velocity (or unpatched) hit is exactly ``color``.
            Default 0.
        position: Pick-position comb on the exciter (fraction of the
            period); 0 disables. Small = near the bridge (bright),
            0.5 = the middle of the string (round). Default 0.2.
        vel_position: How far a soft hit's pick slides towards the
            middle of the string -- the hit's position is
            ``clamp(position + vel_position * (1 - vel) * (0.5 -
            position), 0, 1)``; a full-velocity (or unpatched) hit
            is exactly ``position``, and ``position`` 0 stays off.
            Default 0.
        carry: Keep the loop's allpass (fractional-delay) state
            through a hit, so a re-pluck is exact superposition on
            the ringing string (measured). Default True; False is
            the old behaviour -- every hit clears the state, a
            51%-of-the-ring step and a slightly harder re-attack.
        level: Output level. Default 0.5.

    Ports:
        pitch_cv (in, cv): 1 V/oct, C4 = 0 V. Unpatched → C4.
        trigger (in, gate): pluck per rising edge (per voice).
        vel (in, cv): burst multiplier latched at the edge (per voice);
            max(0, ·). Unpatched → 1.
        out (out, audio): the string.
    """

    TYPE = "pluck"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "decay": 2.0,
        "damping": 0.5,
        "color": 0.7,
        "vel_color": 0.0,
        "position": 0.2,
        "vel_position": 0.0,
        "carry": True,
        "level": 0.5,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("trigger", "in", "gate"),
        Port("vel", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
