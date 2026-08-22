"""The shimmer: same feedback door, with an octave in the loop.

Sibling of `test_organ_feedback_example.py` and the same job — the
examples sweep already proves it loads and renders, so these pin what
makes it *this* patch. The headline claim is spectral, not structural: if
every lap of the loop really goes up an octave, energy must pile up in
octave bands the dry organ barely reaches. A structural test alone would
pass on a patch where the shifter sat at 0 semitones and shimmered
nothing.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers module types for load)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.io_patch import load_patch

EXAMPLE = Path(__file__).parent.parent / "examples" / "organ_shimmer.json"

SR = 44100
BLOCK = 512


def _patch(regen=None, semitones=None):
    patch = load_patch(EXAMPLE)
    if regen is not None:
        next(m for m in patch if m.TYPE == "matrix_mixer").set_param("g21", regen)
    if semitones is not None:
        next(m for m in patch if m.TYPE == "pitch_shifter").set_param(
            "semitones", semitones
        )
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


def _band_energy(audio, lo, hi):
    mono = audio.mean(axis=1)
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
    freqs = np.fft.rfftfreq(len(mono), 1 / SR)
    return float(spectrum[(freqs >= lo) & (freqs < hi)].sum())


# ----- the loop, and the octave inside it ------------------------------------

def test_the_shifter_sits_inside_the_feedback_loop():
    """out_1 -> pitch_shifter -> delay -> in_2. If the shifter were merely
    hung off the output the patch would sound transposed once, not climb."""
    patch = _patch()
    matrix = next(m for m in patch if m.TYPE == "matrix_mixer")
    shifter = next(m for m in patch if m.TYPE == "pitch_shifter")
    delay = next(m for m in patch if m.TYPE == "delay")
    hops = {(c.src_module_id, c.dst_module_id) for c in patch.cables}
    assert (matrix.id, shifter.id) in hops, "the loop no longer feeds the shifter"
    assert (shifter.id, delay.id) in hops, "the shifter no longer feeds the delay"
    assert (delay.id, matrix.id) in hops, "the loop no longer returns to the matrix"
    ret = next(c for c in patch.cables
               if c.src_module_id == delay.id and c.dst_module_id == matrix.id)
    assert ret.dst_port == "in_2", "the return must land on the regen row"


def test_the_shift_is_a_whole_octave_up():
    shifter = next(m for m in _patch() if m.TYPE == "pitch_shifter")
    assert shifter.params["semitones"] == 12.0
    assert shifter.params["cents"] == 0.0
    assert shifter.params["mix"] == 1.0, "a send must be fully wet"


# ----- the claim: it climbs ---------------------------------------------------

@pytest.mark.parametrize(
    "lo,hi,floor",
    [
        (700, 2800, 4.0),        # one and two octaves above the chord
        (2800, 9000, 50.0),      # three and up — the dry loop barely reaches here
    ],
    ids=["one-two-octaves-up", "three-octaves-up"],
)
def test_octave_bands_fill_in_that_the_dry_loop_leaves_empty(lo, hi, floor):
    """Same patch, transposition set to 0 = the control. Everything else —
    loop gain, delay, reverb, organ — is identical, so any difference in the
    upper bands is the shifter's doing and nothing else's."""
    tail = slice(int(12 * SR), int(20 * SR))
    shimmered = _band_energy(_render(_patch(), 20.0)[tail], lo, hi)
    flat = _band_energy(_render(_patch(semitones=0.0), 20.0)[tail], lo, hi)
    ratio = shimmered / max(flat, 1e-12)
    assert ratio > floor, (
        f"{lo}-{hi} Hz only {ratio:.1f}x the un-shifted loop — the octaves "
        f"are not stacking"
    )


def test_the_delay_tone_still_rolls_the_climb_off():
    """The cascade has to die somewhere or it just accumulates hiss. A dark
    delay in the loop is what kills each successive octave; shipping it
    bright would leave the climb with no ceiling."""
    delay = next(m for m in _patch() if m.TYPE == "delay")
    assert delay.params["tone"] <= 0.4
    assert delay.params["feedback"] == 0.0, "the matrix owns the regeneration"


# ----- safety ------------------------------------------------------------------

def test_shipped_settings_leave_headroom():
    audio = _render(_patch(), 22.0)
    assert np.all(np.isfinite(audio))
    peak = float(np.abs(audio).max())
    assert peak < 0.95, (
        f"peaks at {peak} — into the soft ceiling's knee, so a listener hears "
        f"the limiter instead of the shimmer"
    )


def test_it_settles_instead_of_growing():
    audio = _render(_patch(), 30.0)
    half = len(audio) // 2
    early, late = _rms(audio[:half]), _rms(audio[half:])
    assert late < early * 1.25, f"still climbing at 30 s (early {early}, late {late})"


@pytest.mark.parametrize("regen", [0.85, 0.99, 1.0])
def test_the_ceiling_holds_a_cranked_shimmer(regen):
    """"0.85 never lands" is written on the node, so it must be safe to take
    the invitation — including with an octave multiplying every lap."""
    audio = _render(_patch(regen=regen), 20.0)
    assert np.all(np.isfinite(audio)), f"g21={regen} produced non-finite audio"
    assert np.abs(audio).max() <= 1.0 + 1e-6, f"g21={regen} broke the ceiling"


def test_soft_clip_is_on():
    matrix = next(m for m in _patch() if m.TYPE == "matrix_mixer")
    assert matrix.params["soft_clip"] is True
