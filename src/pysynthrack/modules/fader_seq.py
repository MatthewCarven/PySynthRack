"""FaderSeq module — the Sequencer with a hardware-style fader-bank panel.

Same engine as [`sequencer`](#sequencer), different front panel. Where the
original lists its sixteen steps as labelled parameter rows, `fader_seq`
draws them the way the classic slider step sequencers did (Korg SQ-10
lineage): a horizontal bank of vertical pitch faders, one per step, with
nothing under each but its step number and an on/off tickbox. Minimal
footprint, melody-at-a-glance — the fader heights *are* the tune.

Deliberately a **new module type**, not a UI mode on Sequencer (house
precedent: cv_gates vs cv_keyboard) — saved patches say which panel they
want, and the original stays untouched.

Engine sharing is by *contract*, not inheritance: this class publishes the
exact same param names (``steps``, ``step{i}_pitch``, ``step{i}_on``) and
ports (``clock``/``reset`` in, ``cv``/``gate`` out) as Sequencer, and the
numpy backend routes both TYPEs through the one ``_render_sequencer``
renderer. The param dict is imported from ``sequencer`` rather than copied
so the contract cannot silently drift; behaviour is pinned by the
bit-identical A/B test in ``tests/test_fader_seq.py``.

The faders are quantized to integer semitones over ±12 (the UI slider's
doing — the engine happily renders any float a patch file supplies, so a
hand-edited JSON can still go microtonal or beyond the panel range; the
slider clamps only what the mouse does). 1V/oct ``cv`` (semitones / 12,
C4 = 0 V), gate pulses on enabled steps, rests keep their tick, ``reset``
rewinds — all exactly as documented on the original.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Shared param-layout contract with the original panel. MAX_STEPS and the
# default C-major scale come from sequencer so the two can't drift apart.
from .sequencer import MAX_STEPS, _default_params

# UI fader range in semitones (±). The engine is range-free; this constant
# only bounds the on-screen sliders (and lives here, dpg-free, so tests and
# the UI agree on one number).
FADER_RANGE_ST = 12


@register_module_type
class FaderSeq(Module):
    """Clock-driven step sequencer with a fader-bank panel.

    Parameters (identical contract to :class:`Sequencer`):
        steps: Active loop length, 1..16.
        step{i}_pitch: Pitch of step *i* in semitones (1V/oct ``cv`` =
            ``semitones / 12``; 0 = C4). The panel's faders write integer
            semitones in [-12, +12]; the engine accepts any float.
        step{i}_on: Whether step *i* fires its gate (``False`` = rest).
    """

    TYPE = "fader_seq"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [
        Port("clock", "in", "gate"),
        Port("reset", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("cv", "out", "cv"),
        Port("gate", "out", "gate"),
    ]
