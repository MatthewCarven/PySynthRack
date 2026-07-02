"""CVToAudio module — signal-kind passthrough from CV to audio.

The dual of :class:`AudioToCV`. The patch model walls off
``cv → audio`` cables at :meth:`Patch.connect` (signal-kind check),
so a CV signal can never directly drive an audio input. CVToAudio
is the relabel: same float32 samples, ``cv`` input port, ``audio``
output port, optional ``gain`` scaler.

There is no DSP transformation. The bytes in the input buffer are
copied (possibly multiplied by ``gain``) into the output buffer.

Use cases the bridge unlocks:
  * **Audio-rate LFO as an oscillator.** An LFO at 200 Hz has the
    same audio content as a 200 Hz oscillator -- it just lives on
    the wrong side of the signal-kind wall. CVToAudio brings it
    over, and because the LFO's ``rate_cv`` is 1V/oct, you also
    get a built-in FM input: patch a second LFO into the first
    LFO's ``rate_cv`` to make a two-operator FM tone source.
  * **Percussive clicks.** A fast ADSR (e.g. 1 ms attack, 5 ms
    decay) is a single audible transient when sent through
    CVToAudio. Foundation of synthesized kick drums.
  * **CV oscilloscope via DiskWriter.** Send any modulator
    through CVToAudio into the DiskWriter; the resulting .wav
    is a visual record of the modulator shape over time.

Pitch is governed by the *rate of variation* of the CV signal,
not its instantaneous value. A constant CV is DC and produces no
audible tone -- the speaker limiter clamps it silently. To raise
the pitch of a CVToAudio-fed tone, raise the LFO's rate (or
modulate its ``rate_cv``), not its depth.

No DC blocking by default. Modular convention is that the user
adds a high-pass module if the patch needs one; the synth already
trusts the user with self-oscillating filters, audio-rate FM, and
sum-past-unity CVCombiners, and adding silent safety here would
be inconsistent.

Voice-awareness is by shape preservation: input shape passes
through unchanged. Stateless module, no per-voice state to track.
A 1D ``(F,)`` CV in produces a 1D ``(F,)`` audio out; a 2D
``(V, F)`` CV in produces a 2D ``(V, F)`` audio out. Downstream
sinks (Speaker) collapse the voice axis the same way they do for
voice-aware audio from anywhere else.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class CVToAudio(Module):
    """Pass a CV signal into the audio domain.

    Parameters:
        gain: Linear scaler applied to the CV before it's emitted as
            audio. 1.0 leaves the signal at its native amplitude
            (typically [-1, 1] bipolar or [0, 1] unipolar). Useful
            for boosting a low-depth modulator to a hearable level.
    """

    TYPE = "cv_to_audio"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("cv", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
