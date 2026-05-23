"""AudioToCV module — envelope follower.

Bridges the ``audio`` and ``cv`` signal kinds, which the patch model
otherwise walls off from each other. Rectifies an audio input and
smooths it with an asymmetric one-pole filter (separate attack and
release time constants), emitting a non-negative CV signal that
follows the audio's amplitude envelope.

Use cases:
  * Self-modulating filter: ``filter.out → audio_to_cv → filter.cutoff_cv``
    closes the filter when its own output gets loud (compressor-like
    ducking baked into the filter).
  * Sidechain ducking: ``bass_drum → audio_to_cv → vca.cv`` on the
    pad's VCA — the pad ducks under each kick.
  * Audio-rate-to-control bridge: drive any ``*_cv`` port from the
    envelope of any audio signal.

Params:
  * ``attack_ms``: One-pole attack time in milliseconds. Smaller =
    snappier follow; larger = lazier rise. 5 ms is a good default for
    a "tight" follower; 20–50 ms feels more "musical" for ducking.
  * ``release_ms``: One-pole release time in milliseconds. 100 ms
    gives a classic envelope-follower decay; longer makes a smoother,
    more averaged CV.
  * ``gain``: Output scaler. The follower's raw output is in roughly
    the same range as the input audio's peak amplitude (~0–1 for
    normalised audio); ``gain`` lets you bring quiet sources up.

Voice-awareness:
  Shape-polymorphic, decided by the audio input's ``ndim``. A 1D
  audio in produces a 1D CV out; a voice-aware ``(V, F)`` audio in
  produces ``(V, F)`` CV out, with per-voice smoother state. This
  matches the per-voice convention adopted across the stateful DSP
  modules in voice routing slice 3b.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class AudioToCV(Module):
    """Envelope follower: audio in → CV out.

    Parameters:
        attack_ms: One-pole attack time constant in milliseconds.
            Time for the smoother to reach ~63% of a step-up in the
            input amplitude.
        release_ms: One-pole release time constant in milliseconds.
            Time for the smoother to fall ~63% from its current
            level toward a lower input amplitude.
        gain: Linear multiplier applied to the smoothed envelope on
            the way out. 1.0 leaves the envelope at roughly the
            audio's peak scale.
    """

    TYPE = "audio_to_cv"
    DEFAULT_PARAMS = {
        "attack_ms": 5.0,
        "release_ms": 100.0,
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("cv", "out", "cv")]
