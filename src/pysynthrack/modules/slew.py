"""Slew — a slew limiter / lag / glide for control signals.

Feeds a CV in and chases it over *time*: when the input jumps, the output
ramps toward it rather than snapping. Independent **rise** and **fall** times
(the classic slew limiter, not a symmetric lag) let a rising edge glide at one
speed and a falling edge at another. It is the general "inertia" tool the CV
shelf was missing — the time-shaped counterpart to the pointwise
:class:`CVOffset` / :class:`CVScale`.

Two characters, picked by ``shape``:

  * ``linear`` — the true slew limiter: the output moves at a *constant rate*
    and REACHES the target in finite time, giving straight, snappy glides
    (triangle-ish shapes). ``rise_time`` / ``fall_time`` are read as **seconds
    per 1.0 unit of change**, so a bigger jump takes proportionally longer —
    a rate, exactly like an analog slew.
  * ``exponential`` — the classic lag/portamento feel: the output eases toward
    the target, fast at first then asymptotic, never quite arriving. Here the
    times are read as the **~time to 99% of a jump**, chosen so the two shapes
    "arrive" in about the same wall-clock at the same setting — flip ``shape``
    and the glide keeps its length, only its curve changes.

A non-positive time means *instant* on that side (no slew), so
``rise_time = 0`` with a slow ``fall_time`` gives an instant-attack,
slow-release "peak follower", and vice versa.

What it's for:

  * **Polyphonic portamento** — wire a keyboard / ``cv_keyboard`` pitch CV
    through ``Slew`` into ``cv_to_frequency`` → oscillator and every voice
    glides independently between notes (voice-aware, per-voice state).
  * **Tame a twitchy modulator** — lag a stepped ``sequencer`` into smooth
    sweeps, or round the corners off a fast square LFO.
  * **Shape from a gate** — feed a raw gate in and the rise/fall times turn it
    into a sloped envelope, a poor-man's AR with independent up/down.
  * **Add weight** — a touch of slew on a filter-cutoff CV gives the sweep
    physical inertia.

Params:
  * ``shape``: ``"linear"`` (constant-rate, reaches the target) or
    ``"exponential"`` (one-pole ease, asymptotic). Default ``"linear"``.
  * ``rise_time``: how long an UPWARD move takes (seconds). Linear reads it
    per 1.0 unit; exponential reads it as ~time-to-99%. 0 = instant up.
    Default 0.1.
  * ``fall_time``: the same for a DOWNWARD move. 0 = instant down. Default 0.1.

Voice-awareness:
  Shape-polymorphic, per the v0.4 convention. A mono ``(F,)`` CV in → ``(F,)``
  out with one running value; a voice-aware ``(V, F)`` CV in → ``(V, F)`` out
  with one running value per voice slot, so polyphonic glide never crosstalks
  between voices. The running value is carried across blocks (block-size
  independent) and **primed to the first input sample** on the first block, so
  the output starts *at* the incoming level rather than swooping up from zero.
  An unpatched input has nothing to slew and emits 0.

Ports:
  * ``in`` (cv): the control signal to slew. Unpatched → 0 out.
  * ``out`` (cv): the time-limited signal.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Slew(Module):
    """Slew limiter / lag / glide for CV, with independent rise & fall times.

    Parameters:
        shape: ``"linear"`` (constant-rate, reaches target) or
            ``"exponential"`` (one-pole ease, asymptotic). Default
            ``"linear"``.
        rise_time: Seconds for an upward move — per 1.0 unit in ``linear``,
            ~time-to-99% in ``exponential``. 0 = instant. Default 0.1.
        fall_time: The same for a downward move. 0 = instant. Default 0.1.

    Ports:
        in (in, cv): the CV to slew. Unpatched is treated as 0.
        out (out, cv): the time-limited CV.
    """

    TYPE = "slew"
    CATEGORY = "CV & Utilities"
    DEFAULT_PARAMS = {
        "shape": "linear",
        "rise_time": 0.1,
        "fall_time": 0.1,
    }
    INPUT_PORTS = [Port("in", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "cv")]
