"""The organ-feedback drone: the loop must stay closed, and stay safe.

`test_examples.py` already proves every shipped patch loads, compiles and
renders finite. This pins the two claims that make *this* patch what it
is, in the `ring_governor` tradition: the regenerating cycle really is a
cycle (a future edit can't quietly sever it and leave something that only
looks like feedback), and the matrix's soft ceiling really does hold when
the regen is wound past unity — which is the whole reason the patch can be
shipped with a "0.9 blooms" invitation written on the node.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers module types for load)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.io_patch import load_patch

EXAMPLE = Path(__file__).parent.parent / "examples" / "organ_feedback_drone.json"

SR = 44100
BLOCK = 512


def _patch(regen=None):
    patch = load_patch(EXAMPLE)
    if regen is not None:
        next(m for m in patch if m.TYPE == "matrix_mixer").set_param("g21", regen)
    return patch


def _render(patch, seconds):
    backend = NumpyBackend(sample_rate=SR, block_size=BLOCK)
    backend.compile(patch)
    out = []
    for _ in range(int(seconds * SR / BLOCK)):
        block, _devices = backend.render_block_multi(BLOCK)
        if block is not None:
            out.append(np.array(block, copy=True))
    return np.concatenate(out, axis=0)


def _rms(x):
    return float(np.sqrt((x.astype(np.float64) ** 2).mean()))


# ----- the loop --------------------------------------------------------------

def test_the_feedback_cycle_is_closed():
    """matrix out_1 -> delay -> matrix in_2: out of the matrix and back in."""
    patch = _patch()
    matrix = next(m for m in patch if m.TYPE == "matrix_mixer")
    delay = next(m for m in patch if m.TYPE == "delay")
    out_to_delay = [
        c for c in patch.cables
        if c.src_module_id == matrix.id and c.dst_module_id == delay.id
    ]
    delay_to_in = [
        c for c in patch.cables
        if c.src_module_id == delay.id and c.dst_module_id == matrix.id
    ]
    assert out_to_delay, "the organ drone lost its send into the delay"
    assert delay_to_in, "the organ drone lost its return into the matrix"
    assert delay_to_in[0].dst_port == "in_2", "the return must land on the regen row"


def test_the_regen_row_is_actually_open():
    """g21 is the door the docs and the node name point at. If a future edit
    zeroes it the patch still renders, still sounds like an organ, and is
    silently no longer a feedback demo."""
    matrix = next(m for m in _patch() if m.TYPE == "matrix_mixer")
    assert matrix.params["g21"] > 0.0


def test_the_delay_does_not_regenerate_on_its_own():
    """The matrix owns the regeneration; the delay is a plain send (its own
    `feedback` at 0, `mix` fully wet). Two feedback paths in one loop would
    make the g21 knob lie about what it does."""
    delay = next(m for m in _patch() if m.TYPE == "delay")
    assert delay.params["feedback"] == 0.0
    assert delay.params["mix"] == 1.0


def test_the_reverb_stays_outside_the_loop():
    """Polish, not regen — nothing may return from the reverb into the matrix."""
    patch = _patch()
    reverb = next(m for m in patch if m.TYPE == "reverb")
    matrix = next(m for m in patch if m.TYPE == "matrix_mixer")
    returns = [
        c for c in patch.cables
        if c.src_module_id == reverb.id and c.dst_module_id == matrix.id
    ]
    assert not returns, "the reverb was patched back into the loop"


# ----- it sings, and it doesn't run away -------------------------------------

def test_the_regen_audibly_adds_energy():
    """With the door shut it's a dry organ; open it and the loop fills in."""
    dry = _rms(_render(_patch(regen=0.0), 8.0))
    wet = _rms(_render(_patch(), 8.0))
    assert wet > dry * 1.15, f"regen barely changed the sound (dry {dry}, wet {wet})"


def test_shipped_settings_leave_headroom():
    audio = _render(_patch(), 24.0)
    assert np.all(np.isfinite(audio))
    peak = float(np.abs(audio).max())
    assert peak < 0.95, f"shipped patch peaks at {peak} — too close to the rail"


def test_it_settles_instead_of_growing():
    """A drone, not a swell to the ceiling: the second half must not be
    meaningfully louder than the first."""
    audio = _render(_patch(), 24.0)
    half = len(audio) // 2
    early, late = _rms(audio[:half]), _rms(audio[half:])
    assert late < early * 1.25, f"still growing at 24 s (early {early}, late {late})"


@pytest.mark.parametrize("regen", [0.9, 0.99, 1.0])
def test_the_soft_ceiling_holds_a_cranked_loop(regen):
    """The invitation on the node ("0.9 blooms") has to be safe to accept.
    The matrix's soft ceiling is what makes it so — bounded and finite even
    with the loop wound to unity."""
    audio = _render(_patch(regen=regen), 20.0)
    assert np.all(np.isfinite(audio)), f"g21={regen} produced non-finite audio"
    assert np.abs(audio).max() <= 1.0 + 1e-6, f"g21={regen} broke the ceiling"


def test_soft_clip_is_on():
    """The guardrail the cranked-loop test depends on. Turning it off is a
    legitimate thing for a user to try; shipping it off is not."""
    matrix = next(m for m in _patch() if m.TYPE == "matrix_mixer")
    assert matrix.params["soft_clip"] is True
