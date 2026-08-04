"""MatrixMixer — a 4×4 bipolar gain matrix, and the door to feedback.

Four audio inputs, four audio outputs, sixteen gains: ``out_c`` is the
sum of every ``in_r`` scaled by ``g{r}{c}`` (row = input, column =
output), each gain bipolar −1..+1 (negative = phase flip). The default
is the identity diagonal — a bit-exact 4-channel pass. A `cv_{c}` input
scales its whole output column (duck a bus from an envelope; one jack
per column, not per node — sixteen CV jacks would be soup).

**Feedback patching** is the real feature. The backend topo-sorts a DAG,
so a loop (matrix → delay → back into the matrix) cannot order — the
matrix is the sanctioned door: at compile time, any cable INTO a
matrix_mixer that would close a cycle is marked a **late-read**: the
matrix reads that source's *previous block* (one block of feedback
latency — the standard software-modular answer; at 48 kHz / 512 that's
~10.7 ms in the loop). Feed-forward cables through the matrix stay
zero-latency, and the rest of the graph sorts exactly as before. The
first block of a fresh loop reads silence (pinned).

``soft_clip`` (default on) is the stability guardrail, shaped so it
never colors a sane mix: **transparent below 0.95** (bit-exact — a
plain tanh would attenuate a 0.5 signal by 8 %, unacceptable for a
default-on mixer stage), then a C1-continuous tanh section that
saturates at exactly 1.0. A runaway loop (|gain| > 1 around the cycle)
lands on the ceiling instead of the rails; switch it off and the same
loop grows without bound (also pinned — that's the knob doing its job).

Ports:
  * ``in_1``..``in_4`` (audio, in): the rows. Unpatched → silent row.
  * ``cv_1``..``cv_4`` (cv, in): per-COLUMN level scale (linear
    multiply). Unpatched → 1.
  * ``out_1``..``out_4`` (audio, out): the columns.

Params:
  * ``g11``..``g44``: gain from ``in_r`` to ``out_c``, −1..+1. Default
    identity (``g11``=``g22``=``g33``=``g44``=1, rest 0).
  * ``soft_clip``: the output ceiling, on/off. Default on.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

MATRIX_SIZE = 4
# Soft-ceiling knee: transparent (bit-exact) below this, C1 tanh above,
# saturating at 1.0. Shared by the renderer and the tests.
MATRIX_CLIP_KNEE = 0.95


def _default_params() -> dict:
    params: dict = {}
    for r in range(1, MATRIX_SIZE + 1):
        for c in range(1, MATRIX_SIZE + 1):
            params[f"g{r}{c}"] = 1.0 if r == c else 0.0
    params["soft_clip"] = True
    return params


@register_module_type
class MatrixMixer(Module):
    """4×4 bipolar gain matrix with sanctioned feedback (see docstring).

    Parameters:
        g{r}{c}: Gain from in_r to out_c, −1..+1. Default identity.
        soft_clip: Transparent-below-0.95 output ceiling. Default True.

    Ports:
        in_1..in_4 (in, audio): rows.
        cv_1..cv_4 (in, cv): per-column level scale.
        out_1..out_4 (out, audio): columns.
    """

    TYPE = "matrix_mixer"
    CATEGORY = "Routing & VCA"
    DEFAULT_PARAMS = _default_params()
    INPUT_PORTS = [
        Port(f"in_{i}", "in", "audio") for i in range(1, MATRIX_SIZE + 1)
    ] + [Port(f"cv_{i}", "in", "cv") for i in range(1, MATRIX_SIZE + 1)]
    OUTPUT_PORTS = [
        Port(f"out_{i}", "out", "audio") for i in range(1, MATRIX_SIZE + 1)
    ]
