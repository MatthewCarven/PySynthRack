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

Ports:
  * ``in`` (audio): signal to tape. Voice-aware; a single voice row is
    bit-identical to mono. Unpatched → silence.
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

    Ports:
        in (in, audio): signal to tape. Unpatched → silence.
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
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
