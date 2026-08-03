"""Drum voices — kick, snare and hat: trigger-driven percussion sources.

Three small synth voices that turn the clockwork modules (``clock``,
``sequencer``, ``shift_random``'s gate…) into a groovebox. All three
share the same engine shape: on each trigger rising edge the *complete
hit* is synthesized into a buffer — seeded per (module, hit), so renders
are deterministic and block-size independent by construction — and
playback just advances through it. A retrigger fades the old tail over
~2 ms while the new hit starts (the declick ramp), so machine-gun
triggers never click.

``kick_drum`` — the pitch-envelope kick: a sine that dives from
``freq_start`` to ``freq_end`` over ``bend`` ms (exponential, the
constant the analog circuits give), amplitude decaying over ``decay``
ms. ``click`` mixes a 2 ms noise transient into the attack;``drive``
pushes the body into a tanh for saturation grit (plain tanh, no
oversampling — the kick's energy is low-frequency, so foldover is
negligible; deviation from the spec's oversampled version noted in the
worklog).

``snare_drum`` — two detuned sine modes (~185/330 Hz, the drum's head
resonances) under a band-passed noise layer (the wires). ``tone_decay``
and ``noise_decay`` shape the two independently; ``snappy`` balances
them (0 = all shell, 1 = all wires).

``hat_drum`` — six detuned square waves (the classic metallic stack)
high-passed near 7 kHz. One module, two jacks: ``closed_trigger`` and
``open_trigger`` share the voice, and a closed hit **chokes** a ringing
open hit (the 2 ms fade), exactly like a real pedal. The squares alias a
little by construction — hats are noise-like and the grit reads as
character, not error.

All three: ``tune`` shifts everything ±12 semitones, ``level`` trims the
output. Mono voices (a drum is one drum; fan the trigger out to several
modules for flams). Numpy backend only; silent stubs under pyo.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class KickDrum(Module):
    """Pitch-envelope kick: a diving sine + click transient + drive.

    Parameters:
        freq_start: Attack pitch in Hz, 100..400. Default 180.
        freq_end: Body pitch in Hz, 30..80. Default 50.
        bend: Pitch-dive time constant in ms, 5..200. Default 40.
        decay: Amplitude t60 in ms, 50..1500. Default 350.
        click: 2 ms attack-noise mix, 0..1. Default 0.3.
        drive: tanh saturation amount, 0..1 (0 = clean). Default 0.
        tune: Semitone shift, ±12. Default 0.
        level: Output level. Default 0.7.

    Ports:
        trigger (in, gate): hit on each rising edge.
        out (out, audio): the kick.
    """

    TYPE = "kick_drum"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "freq_start": 180.0,
        "freq_end": 50.0,
        "bend": 40.0,
        "decay": 350.0,
        "click": 0.3,
        "drive": 0.0,
        "tune": 0.0,
        "level": 0.7,
    }
    INPUT_PORTS = [Port("trigger", "in", "gate")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]


@register_module_type
class SnareDrum(Module):
    """Snare: two detuned head modes + band-passed wire noise.

    Parameters:
        tone_decay: Shell-mode t60 in ms, 20..500. Default 120.
        noise_decay: Wire-noise t60 in ms, 20..1000. Default 200.
        snappy: Shell/wire balance, 0..1. Default 0.5.
        tune: Semitone shift, ±12. Default 0.
        level: Output level. Default 0.7.

    Ports:
        trigger (in, gate): hit on each rising edge.
        out (out, audio): the snare.
    """

    TYPE = "snare_drum"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "tone_decay": 120.0,
        "noise_decay": 200.0,
        "snappy": 0.5,
        "tune": 0.0,
        "level": 0.7,
    }
    INPUT_PORTS = [Port("trigger", "in", "gate")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]


@register_module_type
class HatDrum(Module):
    """Hi-hat: six detuned squares, high-passed; closed chokes open.

    Parameters:
        decay_closed: Closed-hit t60 in ms, 10..300. Default 60.
        decay_open: Open-hit t60 in ms, 50..1500. Default 400.
        tune: Semitone shift, ±12. Default 0.
        level: Output level. Default 0.6.

    Ports:
        closed_trigger (in, gate): tight hit; chokes a ringing open hit.
        open_trigger (in, gate): ringing hit.
        out (out, audio): the hat.
    """

    TYPE = "hat_drum"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "decay_closed": 60.0,
        "decay_open": 400.0,
        "tune": 0.0,
        "level": 0.6,
    }
    INPUT_PORTS = [
        Port("closed_trigger", "in", "gate"),
        Port("open_trigger", "in", "gate"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
