"""Octaver — zero-crossing flip-flop sub-octaves under the dry signal.

The dirty analog bass trick (Boss OC-2 lineage): every rising
zero-crossing of the input toggles a flip-flop, producing a square
wave at **half** the input frequency; a second flip-flop toggling on
the first gives **quarter**. Each square is amplitude-ridden by the
input's own envelope (so silence stays silent and playing dynamics
survive), rounded by a one-pole low-pass (``tone``), and mixed under
the dry at ``sub1``/``sub2`` gains. A lead line in, an instant bass
line underneath.

Being a zero-crossing tracker it is proudly monophonic-minded and
imperfect on complex material — chords and bright timbres make the
flip-flops stutter. That's the charm (the hardware does it too); feed
it single-note lines for clean tracking. The envelope follower is the
``audio_to_cv`` asymmetric one-pole (fast attack, slower release) so
note onsets land and tails decay naturally.

``dry`` 1 + subs 0 is a bit-exact passthrough. All state (flip-flop
phases, the previous sample's sign, follower level, tone-filter
memory) carries across blocks — block-size independent.

Ports:
  * ``in`` (audio, in): the signal to track. Unpatched → silence.
  * ``out`` (audio, out): dry + filtered subs.

Params:
  * ``dry``: dry level, 0..1. Default 1.
  * ``sub1``: −1 octave square level, 0..1. Default 0.5.
  * ``sub2``: −2 octave square level, 0..1. Default 0.
  * ``tone``: one-pole low-pass cutoff on the subs, 200..2000 Hz.
    Default 800.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Octaver(Module):
    """Flip-flop sub-octave generator: −1/−2 oct squares under the dry.

    Parameters:
        dry: Dry signal level, 0..1. Default 1.
        sub1: −1 octave level, 0..1. Default 0.5.
        sub2: −2 octave level, 0..1. Default 0.
        tone: Low-pass cutoff on the subs in Hz, 200..2000. Default 800.

    Ports:
        in (in, audio): signal to track (monophonic lines track best).
        out (out, audio): dry + enveloped, filtered subs.
    """

    TYPE = "octaver"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "dry": 1.0,
        "sub1": 0.5,
        "sub2": 0.0,
        "tone": 800.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
    ]
    OUTPUT_PORTS = [
        Port("out", "out", "audio"),
    ]
