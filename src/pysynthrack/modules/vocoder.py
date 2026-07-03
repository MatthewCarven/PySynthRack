"""Vocoder — a voice speaks through an instrument (the classic robot voice).

A **channel vocoder** makes one signal wear the spectral shape of another.
Two inputs: the **modulator** (usually a voice — a mic, a file player) and
the **carrier** (usually a synth — a fat saw chord, noise, strings). Both
are split into the same set of **bands** by two matched banks of bandpass
filters. In each band, an **envelope follower** measures how loud the
modulator is *right now*, and that level becomes the gain of the carrier's
matching band. Sum the bands and the carrier "talks": wherever the voice
has energy (the moving formant peaks that distinguish *ee* from *ah*), the
carrier is let through; wherever the voice is quiet, the carrier is shut.

The intelligibility of speech lives almost entirely in those slow band
envelopes (tens of Hz), not in the voice's own pitch — which is exactly
what the vocoder throws away. The output's pitch is the **carrier's**
pitch. Play a chord and the voice speaks in harmony; sweep the carrier and
the voice glides. That separation is the whole instrument.

Consonants are the catch: *s*, *t*, *f*, *k* are noise bursts with little
energy in the vocal bands, so a plain band vocoder mumbles. The **hiss**
path fixes that: a dedicated high-band follower watches the modulator
above the band range and gates a burst of filtered noise into the output
whenever a sibilant lands — consonants ride in on noise, as they do in a
real throat.

Controls:
  * ``bands`` — how many analysis/synthesis bands: 8, 12, 16 or 24.
    Fewer bands is the lo-fi, more robotic sound; more bands keeps
    speech clearer. (16 is the classic hardware sweet spot.)
  * ``freq_lo`` / ``freq_hi`` — the frequency range the bands cover, in
    Hz. Band centres are spaced evenly in pitch (log-spaced) across this
    span. Speech lives roughly 100 Hz – 8 kHz.
  * ``width`` — the bandwidth of every band, as a multiple of the
    "touching" width where adjacent bands meet. Narrow (< 1) is precise,
    hollow, more robotic; wide (> 1) smears neighbouring bands together
    for a softer, breathier read.
  * ``attack`` / ``release`` — the envelope followers' speed, in ms.
    Fast attack catches consonant onsets; the release sets how long each
    band hangs on after the voice moves off it. Long release blurs words
    into a pad-like wash (also a sound worth having).
  * ``hiss`` — level of the sibilance/noise path, 0 (off) .. 1. Raise it
    until *s* and *t* cut through, lower it for the smoother, dumber
    robot.
  * ``gain`` — wet-path makeup gain. Band filtering costs level; use
    this to bring the vocoded signal back up (it scales the wet path
    only, never the dry carrier).
  * ``mix`` — dry carrier (0) .. vocoded (1). At 0 the output is a
    bit-exact carrier passthrough. A vocoder is normally played fully
    wet — the default is 1.

Use cases:
  * Mic into ``mod``, a fat saw chord into ``carrier`` — the classic
    robot choir. (Watch the speaker volume with an open mic.)
  * A drum loop as the modulator and a pad as the carrier — the drums
    "play" the pad's spectrum, a rhythmic gate with tone.
  * ``noise`` as the carrier for a whispered, unpitched ghost voice.
  * Long ``release`` + wide ``width`` for a droning vowel pad that only
    loosely follows the voice.

Ports:
  * ``mod`` (audio): the modulator — the signal whose spectral envelope
    is measured (the voice). A polyphonic source is summed to mono
    first. Unpatched -> the bands all close (silence at ``mix=1``).
  * ``carrier`` (audio): the signal that gets shaped (the instrument).
    A polyphonic source is summed to mono first. Unpatched -> silence.
  * ``out`` (audio): the vocoded result, mono.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Vocoder(Module):
    """Channel vocoder (modulator + carrier in, mono out).

    Parameters:
        bands: Number of analysis/synthesis bands — 8, 12, 16 or 24.
            Fewer = lo-fi robot; more = clearer speech.
        freq_lo: Centre of the lowest band in Hz (50 .. 500).
        freq_hi: Centre of the highest band in Hz (2000 .. 12000).
        width: Band bandwidth as a multiple of the adjacent-band spacing
            (0.3 .. 3). Narrow = precise/robotic, wide = smeared/soft.
        attack: Envelope-follower attack in ms (0.1 .. 100).
        release: Envelope-follower release in ms (1 .. 500).
        hiss: Sibilance (noise) path level, 0 .. 1. Restores the
            consonants a pure band vocoder loses.
        gain: Wet-path makeup gain (0 .. 4). Never touches the dry
            carrier.
        mix: Dry carrier (0) .. vocoded (1). 0 is a bit-exact carrier
            passthrough; a vocoder normally plays fully wet.

    Ports:
        mod (in, audio): the modulator (voice). Voice sources summed to
            mono. Unpatched -> bands close.
        carrier (in, audio): the carrier (instrument). Voice sources
            summed to mono. Unpatched -> silence.
        out (out, audio): the vocoded signal, mono.
    """

    TYPE = "vocoder"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "bands": 16,
        "freq_lo": 120.0,
        "freq_hi": 7500.0,
        "width": 1.0,
        "attack": 4.0,
        "release": 60.0,
        "hiss": 0.4,
        "gain": 1.0,
        "mix": 1.0,
    }
    INPUT_PORTS = [
        Port("mod", "in", "audio"),
        Port("carrier", "in", "audio"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
