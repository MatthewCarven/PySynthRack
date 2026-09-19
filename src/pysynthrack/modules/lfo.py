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

Retrigger (2026-09-19 love pass):
  * ``reset`` (gate): a rising edge restarts the phase at ``phase`` on
    that very sample — not at the next block boundary — so
    ``cv_keyboard.gate → lfo.reset`` gives every note a vibrato or
    tremolo that begins from the same place, the way a synth's
    key-synced LFO does. Voice-aware: on the per-voice path a ``(V, F)``
    gate resets each voice from its own row and a mono gate resets
    every voice; on the mono path a ``(V, F)`` gate collapses to
    any-voice-high. The ``random`` waveform rolls a fresh value on a
    reset edge, so a retriggered S&H starts each note on a new step.
  * ``phase`` (0..1 cycles, wraps): where the LFO starts — both the
    free-running start and the point ``reset`` jumps to. 0.25 on a
    sine is the peak (a tremolo that opens on the note), 0.5 on a saw
    is the zero crossing. Moving the knob re-anchors a free-running
    LFO there at the next block, so it is audible without a cable.
    Unpatched ``reset`` at ``phase`` 0.0 is exactly the old free-run.

Seeded random (2026-09-20 love pass):
  * ``seed`` (int): 0, the default, is the old ``random`` -- every new
    value comes from numpy's global rng, so the sequence is different
    every run. N != 0 gives the LFO a private ``default_rng(N)``,
    consumed only when a value is due (a cycle wrap or a reset edge),
    so a seeded S&H plays the same sequence run after run and is
    bit-exact whatever the block size. **A ``reset`` edge re-seeds it**
    -- the sequencer's precedent -- which is the musical point: put a
    bar-rate pulse on the sequencer's ``reset`` AND the LFO's, and a
    random LFO on a filter plays the SAME random phrase every bar, a
    written-down accident instead of a new one. With ``seed`` 0 a reset
    only re-anchors the phase, as before. On the per-voice path each
    voice holds its own copy of the same generator and replays the
    phrase from its own reset. Non-random waveforms never draw.
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
        phase: Start phase in cycles, 0..1 (wraps). The free-running
            start and the phase a ``reset`` edge jumps to. Default 0.0.
        seed: The ``random`` waveform's stream. 0 (default) = numpy's
            global rng, a fresh sequence every run; N != 0 = a private
            seeded generator that a ``reset`` edge restarts, so the
            same random phrase replays from every reset.
    """

    TYPE = "lfo"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = {
        "waveform": "sine",
        "rate": 4.0,
        "depth": 1.0,
        "bipolar": False,
        "cv_depth": 1.0,
        "phase": 0.0,
        "seed": 0,
    }
    INPUT_PORTS = [
        # Octaves per CV unit on the rate, scaled by ``cv_depth``
        # (default 1.0 = 1 V/oct). Block-mean evaluation (same
        # trade-off as filter cutoff_cv): cheap, fine for sub-audio
        # modulators.
        Port("rate_cv", "in", "cv"),
        # Rising edge restarts the phase at ``phase`` on that sample.
        # Sample-accurate (the block is rendered in segments between
        # edges) because a tremolo that starts a few ms late on every
        # note is exactly the kind of thing ears notice.
        Port("reset", "in", "gate"),
    ]
    OUTPUT_PORTS = [Port("cv", "out", "cv")]
