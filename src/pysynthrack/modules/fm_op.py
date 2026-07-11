"""FM operator — one DX-style phase-modulation oscillator.

A single **operator**: a sine oscillator whose phase is modulated by an
audio-rate input. That one primitive is the whole of DX-style FM synthesis —
two operators (one modulating the other) make a bell or a bass, three make
nearly any electric-piano / brass / clang timbre. The rack builds those stacks
by patching one ``fm_op``'s ``out`` into another's ``pm`` input; each operator
carries its own amplitude envelope (via ``amp_cv``) exactly like a DX7 voice.

Per output sample the operator computes::

    core = sin(2*pi*phase + index*pm + feedback*core_prev)
    out  = amp_cv * core

where ``phase`` integrates the carrier frequency (so 1 V/oct ``pitch_cv``
gives true, per-sample-accurate pitch), ``pm`` is the audio-rate phase
modulator, and ``core_prev`` is the previous output sample (self-feedback).

**The radians scaling of ``index``.** ``pm`` is added *directly* into the sine
argument, which is in radians. So ``index`` is the peak phase deviation, in
radians, produced by a full-scale (+/-1) ``pm`` signal: a sine of amplitude
``A`` into ``pm`` at ``index = I`` gives a classic FM modulation index of
``beta = I * A``. At ``ratio 1:1`` that yields the textbook Bessel sideband
amplitudes ``J_k(beta)`` around the carrier — the module's analytic test.

**Frequency.** In the normal (non-``fixed``) mode the carrier tracks the note:
``freq = 261.6256 Hz (C4) * 2**pitch_cv * ratio * 2**(fine/1200)``. ``ratio``
snaps to the nearest entry of :data:`RATIO_TABLE` (a harmonic-leaning set with
a couple of inharmonic bell ratios), so hand-dialled values land on musical
partials; ``fine`` detunes +/-50 cents for beating / thickening. In ``fixed``
mode the carrier ignores ``pitch_cv`` (and ``ratio`` / ``fine``) and runs at a
constant ``freq`` Hz — DX-style fixed operators for formant-ish or
clangorous fixed partials.

**Feedback.** ``feedback`` (0..1) feeds the previous output sample back into
the phase, a self-phase-modulation that brightens a lone operator toward a
saw-like spectrum (peak self-modulation ~1 radian at ``feedback = 1``). Any
``feedback > 0`` makes the sample recurrence sequential, so the renderer drops
from the vectorized block path to a per-sample loop (the delay module's
dual-engine precedent); the two paths are bit-identical at ``feedback = 0``.

**index_cv.** ``index_cv`` modulates the FM index from an envelope or LFO —
the single most important gesture in FM, since the index *is* the brightness.
Effective index = ``max(index + index_cv_depth * index_cv, 0)``. (The spec's
port list omitted this input but listed ``index_cv_depth``; a depth param
implies its CV input per the project conventions, and an index envelope is
what makes FM tones evolve, so the input is provided.)

Ports:
  * ``pitch_cv`` (in, cv): 1 V/oct, C4 = 0 V. Per-sample carrier pitch.
    Unpatched -> the base pitch (C4 * ``ratio`` * ``fine``). Ignored in
    ``fixed`` mode.
  * ``pm`` (in, audio): audio-rate phase modulator, scaled by ``index``
    (+ ``index_cv``). Unpatched -> a pure carrier sine. Voice-aware.
  * ``amp_cv`` (in, cv): linear output amplitude (drive it from an
    ``adsr`` / ``cv_gates`` — the operator's level envelope). Unpatched ->
    unity (the operator is a Source, so it sounds with nothing patched).
  * ``index_cv`` (in, cv): FM-index modulation, scaled by ``index_cv_depth``.
    Optional.
  * ``out`` (out, audio): the operator output.

Use cases:
  * 2-op bell: operator B (``ratio`` ~3.5, a decaying ``amp_cv``) into
    operator A's ``pm`` (``ratio`` 1) — instant tuned metal.
  * 3-op e-piano: a high-ratio (~14) tine modulator + a 1:1 body modulator
    stacked into a 1:1 carrier; short index envelopes give the DX Rhodes bark.
  * Self-feedback bass: one operator, ``ratio`` 1, ``feedback`` ~0.6 — a
    bright saw-ish tone with no second oscillator.

Pairs with ``ring_mod`` and ``freq_shifter`` as the inharmonic / metallic
corner of the rack; unlike those effects it is a full self-contained voice.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Frequencies ``ratio`` snaps to (nearest entry). Harmonic-leaning — integer
# and simple-fraction partials — with 3.5 and 1.5/2.5 for inharmonic bell /
# metallic colours. Hand-edited off-table values snap here in the renderer, so
# every audible ratio is a deliberate one.
RATIO_TABLE = (
    0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0,
    5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 14.0, 16.0,
)


def snap_ratio(value: float) -> float:
    """Snap ``value`` to the nearest entry of :data:`RATIO_TABLE`.

    Nearest by absolute difference; out-of-range values clamp onto the
    nearest end. Shared by the renderer (so any stored value snaps) and the
    UI combo (so the panel offers exactly the table)."""
    return min(RATIO_TABLE, key=lambda r: abs(r - float(value)))


@register_module_type
class FMOperator(Module):
    """One DX-style phase-modulation FM operator (a self-contained voice).

    Parameters:
        ratio: Carrier frequency as a multiple of the note pitch, snapped to
            the nearest :data:`RATIO_TABLE` entry (0.25 .. 16, default 1).
            Ignored in ``fixed`` mode.
        fine: Fine detune in cents (+/-50, default 0). Ignored in ``fixed``
            mode.
        index: FM modulation index — peak phase deviation in radians for a
            full-scale ``pm`` input (0 .. 10, default 1). See the module
            docstring for the radians scaling.
        index_cv_depth: Index change per unit of ``index_cv`` (default 1.0).
            Effective index = ``max(index + index_cv_depth * index_cv, 0)``.
        feedback: Self phase-modulation, 0 .. 1 (default 0). > 0 engages the
            per-sample engine.
        fixed: When True the carrier runs at a constant ``freq`` Hz and
            ignores ``pitch_cv`` / ``ratio`` / ``fine``.
        freq: Fixed-mode carrier frequency in Hz (default 220.0). Only used
            when ``fixed`` is True.

    Ports:
        pitch_cv (in, cv): 1 V/oct carrier pitch (C4 = 0 V). Unpatched ->
            base pitch. Ignored while ``fixed``.
        pm (in, audio): audio-rate phase modulator (x effective index).
        amp_cv (in, cv): linear output level. Unpatched -> unity.
        index_cv (in, cv): index modulation (x ``index_cv_depth``).
        out (out, audio): operator output.
    """

    TYPE = "fm_op"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "ratio": 1.0,
        "fine": 0.0,
        "index": 1.0,
        "index_cv_depth": 1.0,
        "feedback": 0.0,
        "fixed": False,
        "freq": 220.0,
    }
    INPUT_PORTS = [
        Port("pitch_cv", "in", "cv"),
        Port("pm", "in", "audio"),
        Port("amp_cv", "in", "cv"),
        Port("index_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
