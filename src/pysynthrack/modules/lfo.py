"""LFO module — low-frequency oscillator emitting a CV signal.

An LFO is shaped just like an oscillator but typically runs below the
audible range and is used to modulate parameters or scale other signals.
Patched into a VCA's CV input it gives tremolo; patched into a filter's
cutoff it gives wah.

Output is signal-kind ``cv`` so it can plug into VCA.cv and future
CV-modulatable ports, but cannot accidentally route to an audio input.

``bipolar=True`` swings between -1 and +1 (multiplied by ``depth``),
which is the natural shape for pitch / cutoff modulation. ``bipolar=
False`` maps the same wave into the [0, depth] range — the right shape
when feeding a VCA, since negative gain would invert the audio. Default
is unipolar precisely so the LFO → VCA tremolo case "just works".

CV inputs (v0.3):
  * ``rate_cv``: 1V/octave on the rate. ``effective_rate = rate *
    2 ** mean(rate_cv)`` per block. Lets a second LFO (or ADSR) drive
    this LFO's frequency — proper modulation matrix territory:
    accelerating wobble, envelope-swept vibrato, etc.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

LFO_WAVEFORMS = ("sine", "triangle", "square", "saw", "random")


@register_module_type
class LFO(Module):
    """Low-frequency oscillator. Outputs CV.

    Parameters:
        waveform: ``"sine"`` / ``"triangle"`` / ``"square"`` / ``"saw"`` /
            ``"random"`` (sample-and-hold — re-rolls once per cycle).
        rate: Frequency in Hz. Tremolo lives ~3–8 Hz; slow filter sweeps
            ~0.05–1 Hz; audio-rate FM ~20+ Hz.
        depth: Output amplitude in [0, 1]. Pre-scales the wave before the
            bipolar/unipolar shaping.
        bipolar: True → output in [-depth, +depth]. False → [0, depth].
        cv_depth: Octaves the rate moves per unit of ``rate_cv``.
            Default 1.0 = 1 V/oct (the pre-cv_depth fixed behaviour);
            0 disables the CV.
    """

    TYPE = "lfo"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "waveform": "sine",
        "rate": 4.0,
        "depth": 1.0,
        "bipolar": False,
        "cv_depth": 1.0,
    }
    INPUT_PORTS = [
        # Octaves per CV unit on the rate, scaled by ``cv_depth``
        # (default 1.0 = 1 V/oct). Block-mean evaluation (same
        # trade-off as filter cutoff_cv): cheap, fine for sub-audio
        # modulators.
        Port("rate_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("cv", "out", "cv")]
