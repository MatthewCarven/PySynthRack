"""Tape — "put it on tape": wow, flutter, drift, saturation, hiss and a
head bump, in one pass.

A single module that stamps the character of an analog tape machine onto
whatever you feed it. Six independent flavours of that character, layered
in the order a real deck imposes them:

  * **wow** — slow pitch waver (~1 Hz). A worn capstan or an off-centre
    reel makes the tape speed drift up and down a couple of times a
    second; because a moving playback speed is a moving pitch, the sound
    sways gently sharp and flat. Subtle at low settings, seasick at high.
  * **flutter** — fast pitch waver (~9 Hz) *plus a little noise*. The
    quicker, rougher cousin of wow (scrape flutter, roller chatter): a
    shimmer/warble rather than a sway.
  * **drift** — very slow, random speed wander. Not periodic like wow —
    the pitch centre ambles around over seconds, the sound of a machine
    that never quite holds its speed.
  * **sat** — tape saturation. The oxide can only be magnetised so far,
    so peaks round over into a soft ``tanh`` curve: gentle harmonic
    warmth at low drive, obvious crunch high. Runs on the shared 4x
    oversampling path so the added harmonics don't alias.
  * **hiss** — the noise floor. Analog tape is never truly silent; dial
    in a calibrated bed of hiss from off up to −30 dB for that
    ever-present analog "air".
  * **bump** — the *head bump*, a broad low-shelf lift around 60 Hz that
    tape and the playback head impose on the low end — the reason tape
    "sounds bigger" down low.

``mix`` blends the dry input against the full tape pass. The wow/flutter/
drift all modulate one short **fractional-delay line** (a moving delay is
a moving pitch), whose fixed ~10 ms nominal delay is latency-compensated
in the dry path so ``mix`` stays phase-coherent rather than combing.

**The tape-stop.** A ``stop`` gate is the DJ / tape-stop gesture: on its
rising edge the motor coasts down -- the play head's speed ramps 1 -> 0
over ``stop_time`` (linear in speed, a constant deceleration, so the
pitch falls linearly) and the audio dives to a halt; while the gate is
held and the head is stationary the wet is **silent**; on the falling
edge the speed ramps 0 -> 1 over ``start_time`` and the audio climbs
back up to pitch. Both ramps are per-sample and the wet **level follows
the speed** (a magnetic head's EMF is proportional to tape speed --
linear), so the stop fades to exact silence and the restart fades in
with no click. A new edge mid-ramp re-articulates from the *current*
speed: a short tap dips the pitch and recovers.

The physics, and the doubling: the head reads a ring that keeps
recording the live input, so reading at speed ``s(t)`` it falls behind
live by the integral of ``1 - s`` -- and at speeds <= 1 that lag can
never shrink. A stationary head is silent, so the moment the restart
begins the head is jumped back to live (inaudible): the lag resets to 0
at every restart, grows to ``start_time / 2`` during the spin-up and
stays there until the next stop -- bounded, never accumulating. A real
deck loses that time too. So **after a stop the wet runs
``start_time / 2`` behind the dry**: at ``mix = 1`` you simply hear the
tape a little late, at ``mix < 1`` you hear a doubling, because the
dry is live. If the gate falls *before* the head has fully stopped, the
head is still audible, so there is no jump: the restart ramps up from
the current speed and the lag is kept -- repeated partial stops let the
doubling grow, up to ``(stop_time + start_time) / 2`` (the ring's
capacity), after which the head rides the ring's tail at normal speed
until the next full stop resets it.

With ``stop`` patched a **neutral** tape (all knobs at zero) is still a
bit-exact passthrough until the first stop -- the head reads the sample
it just wrote (no nominal delay) while the ring records, so a stop that
comes later has material; after a stop the wet is a fractional read
``start_time / 2`` behind live, and no longer the passthrough. Unpatched,
nothing changes: today's ring and code path exactly.

Neutral: ``wow = flutter = drift = sat = bump = 0`` and ``hiss`` off is a
**bit-exact passthrough** — a freshly added Tape does nothing until you
turn a knob. ``mix = 0`` is likewise bit-exact dry.

Signal flow: ``in → wow/flutter/drift-modulated delay → saturation →
+ hiss → head-bump shelf → mix with (latency-matched) dry``.

Controls:
  * ``wow`` — 0..1, depth of the slow ~1 Hz pitch sway.
  * ``flutter`` — 0..1, depth of the fast ~9 Hz waver (with a little
    noise).
  * ``drift`` — 0..1, amount of slow random speed wander.
  * ``sat`` — 0..1, tape-saturation drive (``tanh``, 4x oversampled).
  * ``hiss`` — noise-floor level in dB, −80 (off) … −30 (max). Scaled by
    ``mix`` (it lives in the wet path), so ``mix = 0`` is silent.
  * ``bump`` — 0..6 dB low-shelf head bump around 60 Hz.
  * ``mix`` — dry/wet, 0 (bit-exact dry) … 1 (fully "on tape").
  * ``stop_time`` — seconds for the head to coast from full speed to a
    halt after the ``stop`` gate rises, 0.05 … 8 (default 1).
  * ``start_time`` — seconds for the head to spin back up to full speed
    after the gate falls, 0.05 … 8 (default 0.5).

The wow/flutter/drift and hiss model a *single* tape path, so a
polyphonic input shares one common motion and hiss bed across its voices
(each voice keeps its own delay line and filter state, so they don't
cross-talk); a single voice row is bit-identical to the mono render.

Use cases:
  * Glue and warmth on a drum or synth bus: a touch of ``sat`` and
    ``bump`` with ``mix`` around 0.5.
  * Lo-fi "old cassette" wash: ``wow``/``flutter`` up, a little ``hiss``.
  * A slow, unstable "broken machine" pad: ``drift`` high, everything
    else low.
  * The tape-stop drop: a slow clock or a key's gate into ``stop`` -- the beat
    dives, halts, and spins back up (``mix = 1`` so the dry doesn't give
    the game away).

Ports:
  * ``in`` (audio): signal to tape. Voice-aware; a single voice row is
    bit-identical to mono. Unpatched → silence.
  * ``stop`` (gate): the tape-stop. High = coast to a halt over
    ``stop_time``, low = spin back up over ``start_time``. One transport
    for every voice: a ``(V, F)`` gate collapses to any-voice-high.
    Unpatched → today's code path exactly.
  * ``out`` (audio): the taped (and dry-blended) signal.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Tape(Module):
    """Tape character: wow/flutter/drift, saturation, hiss and head bump.

    Parameters:
        wow: Slow (~1 Hz) pitch-sway depth, 0 (none) … 1 (wide).
        flutter: Fast (~9 Hz) pitch-waver depth (plus a little noise),
            0 … 1.
        drift: Slow random speed-wander amount, 0 … 1.
        sat: Tape-saturation drive (``tanh`` on the 4x oversampling
            path), 0 (clean) … 1 (crunch).
        hiss: Noise-floor level in dB, −80 (off) … −30 (max). Lives in
            the wet path, so it is scaled by ``mix``.
        bump: Low-shelf "head bump" around 60 Hz, 0 … 6 dB.
        mix: Dry/wet balance, dry (0) … fully taped (1). 0 is a
            bit-exact dry passthrough.
        stop_time: Seconds for the head to coast to a halt after the
            ``stop`` gate rises, 0.05 … 8.
        start_time: Seconds for the head to spin back up after the gate
            falls, 0.05 … 8.

    Ports:
        in (in, audio): signal to tape. Unpatched → silence.
        stop (in, gate): the tape-stop; high halts, low restarts. A
            ``(V, F)`` gate collapses to any-voice-high. Unpatched → the
            pre-stop code path exactly.
        out (out, audio): taped (and dry-blended) signal.
    """

    TYPE = "tape"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "wow": 0.0,
        "flutter": 0.0,
        "drift": 0.0,
        "sat": 0.0,
        "hiss": -80.0,
        "bump": 0.0,
        "mix": 1.0,
        "stop_time": 1.0,
        "start_time": 0.5,
    }
    INPUT_PORTS = [Port("in", "in", "audio"), Port("stop", "in", "gate")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
