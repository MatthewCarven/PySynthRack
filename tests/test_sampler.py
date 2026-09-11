"""Sampler — pitched, gate-triggered sample playback (slices 1 and 2).

The contract these pin, in order of importance:

* **The neutral is bit-exact.** Root pitch, full region, no attack, level 1
  gives rate exactly 1.0, every read position an integer, and the 4-tap
  Hermite's constant term is ``p0`` untouched — so the output *is* the
  decoded file, sample for sample. A source's passthrough contract, and
  the strongest statement available about an interpolating read.
* **An octave up is `[::2]`, also bit-exact** — the strong version: a rate
  of exactly 2.0 still lands on integers.
* Everything else (mode behaviour, region, declick ramps, voices) is
  measured against that spine.
* **Slice 2's loop keeps the spine.** A unity-rate loop with the seam
  crossfade off is a bit-exact *tiling* of the file, so "the period is
  exactly the loop length" is an ``array_equal``, not a tolerance. The
  crossfade is the one claim that cannot be stated that way — it is about
  a discontinuity, so it is measured with a two-render A/B against the same
  loop with the fade switched off.

Note the tests drive ``_render_sampler`` directly with hand-built gate and
pitch buffers rather than going through a whole patch: that is the only way
to script an exact gate edge at a chosen sample, which most of these
assertions are about. The load path is exercised for real — files on disk,
decoded by the same background loader the app uses.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.sampler import (
    CV_REFERENCE_NOTE,
    MAX_SECONDS,
    SAMPLER_MODES,
    Sampler,
    playback_rate,
)

SR = 44100


# ----- the rate law (pure, no audio) -----------------------------------------

def test_playing_the_root_note_is_exactly_unity():
    """Not 'about 1.0' — exactly, or the neutral below cannot be bit-exact."""
    assert playback_rate(0.0, CV_REFERENCE_NOTE, 0.0, 0.0) == 1.0


def test_root_moves_the_note_that_reads_unity():
    # Sample recorded at C3 (48): playing C3 is unity, playing C4 is double.
    assert playback_rate(-1.0, 48, 0.0, 0.0) == pytest.approx(1.0)
    assert playback_rate(0.0, 48, 0.0, 0.0) == pytest.approx(2.0)


def test_tune_and_fine_stack_on_top():
    assert playback_rate(0.0, 60, 12.0, 0.0) == pytest.approx(2.0)
    assert playback_rate(0.0, 60, -12.0, 0.0) == pytest.approx(0.5)
    assert playback_rate(0.0, 60, 0.0, 50.0) == pytest.approx(2.0 ** (0.5 / 12))
    # An octave of tune and a semitone down of cv cancel to a seventh.
    assert playback_rate(-1 / 12, 60, 12.0, 0.0) == pytest.approx(2.0 ** (11 / 12))


def test_module_shape():
    assert Sampler.TYPE == "sampler"
    assert Sampler.CATEGORY == "Sources"
    assert [p.name for p in Sampler.INPUT_PORTS] == [
        "pitch_cv", "gate", "start_cv", "vel"
    ]
    assert [p.name for p in Sampler.OUTPUT_PORTS] == ["out", "out_l", "out_r"]
    assert Sampler.DEFAULT_PARAMS["mode"] in SAMPLER_MODES
    assert MAX_SECONDS > 0


# ----- harness ----------------------------------------------------------------

@pytest.fixture(scope="module")
def noise_wav(tmp_path_factory):
    """A jagged mono file: any interpolation slop or off-by-one shows up."""
    rng = np.random.default_rng(7)
    data = (rng.standard_normal(4000) * 0.3).astype(np.float32)
    data[500] = 0.9        # a lone spike, so a smeared read is obvious
    path = tmp_path_factory.mktemp("samples") / "noise.wav"
    wavfile.write(path, SR, data)
    return str(path), data


@pytest.fixture(scope="module")
def sine_wav(tmp_path_factory):
    """One second of 440 Hz, for the pitch and declick measurements."""
    t = np.arange(SR, dtype=np.float64) / SR
    data = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    path = tmp_path_factory.mktemp("samples") / "sine.wav"
    wavfile.write(path, SR, data)
    return str(path), data


def _render_all(params, gate, pitch=None, chunks=1, start_cv=None, vel=None):
    """Render a sampler with exact gate / pitch / start_cv / vel buffers.

    Returns the renderer's port dict (``out``, ``out_l``, ``out_r``), each
    concatenated across ``chunks`` blocks.
    """
    frames = gate.shape[-1]
    patch = Patch()
    smp = patch.add_module("sampler", params=params)
    trig = patch.add_module("key_trigger")
    patch.connect(trig.id, "out", smp.id, "gate")
    feeds = {}
    for port, buf in (("pitch_cv", pitch), ("start_cv", start_cv), ("vel", vel)):
        if buf is not None:
            src = patch.add_module("constant")
            patch.connect(src.id, "out", smp.id, port)
            feeds[(src.id, "out")] = buf
    backend = NumpyBackend(sample_rate=SR, block_size=frames // chunks)
    backend.compile(patch)
    backend.wait_for_sample_loads()
    n = frames // chunks
    parts = {"out": [], "out_l": [], "out_r": []}
    for k in range(chunks):
        sl = (Ellipsis, slice(k * n, (k + 1) * n))
        bufs = {(trig.id, "out"): gate[sl]}
        for key, buf in feeds.items():
            bufs[key] = buf[sl]
        res = backend._render_sampler(smp, n, bufs, patch)
        for name in parts:
            parts[name].append(res[name])
    return {name: np.concatenate(chunks_, axis=-1) for name, chunks_ in parts.items()}


def _render(params, gate, pitch=None, chunks=1, **feeds):
    """The mono ``out`` of ``_render_all`` — what slices 1 and 2 pinned."""
    return _render_all(params, gate, pitch=pitch, chunks=chunks, **feeds)["out"]


def _hit(frames, at=0, width=32, voices=None):
    shape = (voices, frames) if voices else (frames,)
    gate = np.zeros(shape, dtype=np.float32)
    gate[..., at:at + width] = 1.0
    return gate


NEUTRAL = {"root": 60.0, "tune": 0.0, "fine": 0.0, "mode": "one_shot",
           "start": 0.0, "end": 1.0, "attack": 0.0, "level": 1.0}


# ----- the spine: bit-exactness -----------------------------------------------

def test_the_neutral_is_the_file_bit_exact(noise_wav):
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path), _hit(8000))
    assert np.array_equal(out[:len(data)], data)


def test_after_the_region_it_is_exactly_silent(noise_wav):
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path), _hit(8000))
    assert np.all(out[len(data):] == 0.0)


def test_an_octave_up_is_every_other_sample_bit_exact(noise_wav):
    """Rate exactly 2.0 still lands on integers, so this is a decimation,
    not an approximation of one."""
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path, root=48.0), _hit(8000))
    assert np.array_equal(out[:len(data) // 2], data[::2])


def test_block_size_does_not_change_a_single_sample(noise_wav):
    path, _data = noise_wav
    gate = _hit(8192)
    one = _render(dict(NEUTRAL, path=path), gate, chunks=1)
    many = _render(dict(NEUTRAL, path=path), gate, chunks=16)
    assert np.array_equal(one, many)


def test_level_scales_and_nothing_else(noise_wav):
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path, level=0.5), _hit(8000))
    assert np.allclose(out[:len(data)], data * 0.5, atol=1e-7)


# ----- pitch ------------------------------------------------------------------

def test_seven_semitones_up_lands_on_the_right_frequency(sine_wav):
    path, _data = sine_wav
    frames = 30000
    gate = _hit(frames, width=20000)
    pitch = np.full(frames, 7.0 / 12.0, dtype=np.float32)
    out = _render(dict(NEUTRAL, path=path, mode="gated"), gate, pitch=pitch)
    seg = out[2000:18000].astype(np.float64)
    spectrum = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    peak = freqs[np.argmax(spectrum)]
    want = 440.0 * 2 ** (7 / 12)
    cents = 1200 * np.log2(peak / want)
    assert abs(cents) < 10, f"{peak:.1f} Hz vs {want:.1f} Hz ({cents:.1f} cents)"


# ----- modes ------------------------------------------------------------------

def test_one_shot_ignores_the_gate_falling(noise_wav):
    """A two-sample trigger must still play the whole region — the drum
    idiom, and the reason a euclidean can drive this."""
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path), _hit(8000, width=2))
    assert np.array_equal(out[:len(data)], data)


def test_gated_stops_on_the_fall(sine_wav):
    path, _data = sine_wav
    release_ms = 10.0
    gate = _hit(20000, width=5000)
    out = _render(dict(NEUTRAL, path=path, mode="gated", release=release_ms), gate)
    release_n = int(round(release_ms * 1e-3 * SR))
    assert np.all(out[5000 + release_n:] == 0.0), "sound outlived the release"
    assert np.any(np.abs(out[5000:5000 + release_n]) > 0), "release was instant"


def test_the_gated_release_does_not_click(sine_wav):
    """The fall lands mid-waveform on purpose: without a ramp the step to
    zero would be the sample's own value."""
    path, data = sine_wav
    gate = _hit(20000, width=5000)
    out = _render(dict(NEUTRAL, path=path, mode="gated", release=10.0), gate)
    step_if_cut = abs(float(data[5000]))
    worst = float(np.abs(np.diff(out[4995:5600])).max())
    assert worst < step_if_cut / 4, (
        f"release step {worst} vs {step_if_cut} for a hard cut"
    )


# ----- region -----------------------------------------------------------------

def test_start_and_end_select_an_exact_slice(noise_wav):
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path, start=0.25, end=0.5), _hit(8000))
    n = len(data)
    expected = data[n // 4: n // 2]
    assert np.array_equal(out[:len(expected)], expected)
    assert np.all(out[len(expected):] == 0.0)


def test_an_inverted_region_is_silence_not_a_backwards_read(noise_wav):
    path, _data = noise_wav
    out = _render(dict(NEUTRAL, path=path, start=0.8, end=0.2), _hit(4000))
    assert np.all(out == 0.0)


# ----- declick ----------------------------------------------------------------

def test_attack_ramps_in_from_silence(noise_wav):
    path, data = noise_wav
    attack_ms = 5.0
    out = _render(dict(NEUTRAL, path=path, attack=attack_ms), _hit(8000))
    n = int(round(attack_ms * 1e-3 * SR))
    # Ramped region is strictly quieter than the raw file; past it, identical.
    assert abs(out[0]) < abs(data[0]) or data[0] == 0
    assert np.array_equal(out[n:len(data)], data[n:])


def test_retriggering_a_sounding_voice_does_not_click(sine_wav):
    """The old playhead keeps running under a falling ramp while the new one
    fades in, so the jump back to `start` is crossfaded rather than cut."""
    path, data = sine_wav
    gate = np.zeros(20000, dtype=np.float32)
    gate[:20] = 1.0
    gate[8000:8020] = 1.0
    out = _render(dict(NEUTRAL, path=path), gate)
    natural = float(np.abs(np.diff(data[:20000])).max())
    worst = float(np.abs(np.diff(out[7950:8200])).max())
    assert worst < natural * 2, f"retrigger step {worst} vs natural {natural}"


# ----- voices -----------------------------------------------------------------

def test_voices_are_independent(noise_wav):
    path, data = noise_wav
    frames = 8000
    gate = np.zeros((2, frames), dtype=np.float32)
    gate[0, :32] = 1.0            # slot 0 fires at 0
    gate[1, 3000:3032] = 1.0      # slot 1 fires later
    pitch = np.zeros((2, frames), dtype=np.float32)
    pitch[1, :] = 1.0             # ...and an octave up
    out = _render(dict(NEUTRAL, path=path), gate, pitch=pitch)
    assert out.shape == (2, frames)
    assert np.array_equal(out[0, :len(data)], data)
    assert np.all(out[1, :3000] == 0.0), "slot 1 sounded before its gate"
    assert np.array_equal(out[1, 3000:3000 + len(data) // 2], data[::2])


def test_one_voice_is_bit_identical_to_a_mono_render(noise_wav):
    """Voiced slot 0 and the mono path must be the same code, not two."""
    path, _data = noise_wav
    frames = 8000
    gate = _hit(frames)
    pitch = np.zeros(frames, dtype=np.float32)
    mono = _render(dict(NEUTRAL, path=path), gate, pitch=pitch)
    voiced = _render(
        dict(NEUTRAL, path=path),
        gate[None, :].repeat(1, axis=0),
        pitch=pitch[None, :],
    )
    assert np.array_equal(voiced[0], mono)


# ----- the load path ----------------------------------------------------------

def test_a_missing_file_is_silence_not_an_exception(tmp_path):
    out = _render(dict(NEUTRAL, path=str(tmp_path / "nope.wav")), _hit(2000))
    assert np.all(out == 0.0)


def test_an_empty_path_is_silence(noise_wav):
    out = _render(dict(NEUTRAL, path=""), _hit(2000))
    assert np.all(out == 0.0)


def test_no_gate_cable_is_silence(noise_wav):
    """Nothing can start a voice, so the module is silent by contract."""
    path, _data = noise_wav
    patch = Patch()
    smp = patch.add_module("sampler", params=dict(NEUTRAL, path=path))
    backend = NumpyBackend(sample_rate=SR, block_size=512)
    backend.compile(patch)
    backend.wait_for_sample_loads()
    out = backend._render_sampler(smp, 512, {}, patch)["out"]
    assert np.all(out == 0.0)


def test_a_stereo_file_is_summed_to_mono(tmp_path):
    left = np.linspace(-0.5, 0.5, 1000).astype(np.float32)
    right = np.full(1000, 0.25, dtype=np.float32)
    path = tmp_path / "stereo.wav"
    wavfile.write(path, SR, np.stack([left, right], axis=1))
    out = _render(dict(NEUTRAL, path=str(path)), _hit(4000))
    assert np.allclose(out[:1000], 0.5 * (left + right), atol=1e-6)


def test_a_patch_round_trips_with_its_sample(tmp_path, noise_wav):
    """A saved patch must remember its path — the whole voice depends on it."""
    from pysynthrack.io_patch import load_patch, save_patch

    path, _data = noise_wav
    patch = Patch()
    smp = patch.add_module("sampler", params=dict(NEUTRAL, path=path, root=55.0))
    out = tmp_path / "p.json"
    save_patch(patch, out)
    back = load_patch(out).get(smp.id)
    assert back.params["path"] == path
    assert back.params["root"] == 55.0


# ----- loop mode (slice 2) ----------------------------------------------------

@pytest.fixture(scope="module")
def ramp_wav(tmp_path_factory):
    """A rising ramp. Wrapping its end back to its start is a full-scale
    step — the loudest possible seam — so whether the crossfade works is
    not a matter of opinion."""
    data = np.linspace(0.0, 1.0, 4000, endpoint=False).astype(np.float32)
    path = tmp_path_factory.mktemp("samples") / "ramp.wav"
    wavfile.write(path, SR, data)
    return str(path), data


def _held(frames, voices=None):
    """A gate that never falls — a key held down."""
    shape = (voices, frames) if voices else (frames,)
    return np.ones(shape, dtype=np.float32)


#: The loop neutral: whole region, no seam fade, so every read position is
#: still an integer and the output can be compared with ``array_equal``.
LOOP = dict(NEUTRAL, mode="loop", loop_start=0.0, loop_end=1.0,
            loop_xfade=0.0)


def test_loop_joined_the_modes_without_moving_the_default():
    assert "loop" in SAMPLER_MODES
    assert Sampler.DEFAULT_PARAMS["mode"] == "one_shot", "default changed"
    for name in ("loop_start", "loop_end", "loop_xfade"):
        assert name in Sampler.DEFAULT_PARAMS


def test_a_held_loop_is_a_bit_exact_tiling_of_the_file(noise_wav):
    """The strong form of "the steady-state period is exactly the loop
    length": at unity rate the wrap still lands on integers, so four laps
    are the file four times over, sample for sample."""
    path, data = noise_wav
    out = _render(dict(LOOP, path=path), _held(4 * len(data)))
    assert np.array_equal(out, np.tile(data, 4))


def test_an_octave_up_loops_every_other_sample(noise_wav):
    """Rate 2.0 also lands on integers, so a transposed loop is exact too
    — slice 1's statement about a one-shot, now wrapped."""
    path, data = noise_wav
    out = _render(dict(LOOP, path=path, root=48.0), _held(4 * len(data) // 2))
    assert np.array_equal(out, np.tile(data[::2], 4))


def test_the_loop_is_measured_inside_the_playback_region(noise_wav):
    """`loop_start`/`loop_end` are fractions OF THE REGION, so moving
    `start` carries the loop along instead of stranding it."""
    path, data = noise_wav
    half = len(data) // 2
    out = _render(dict(LOOP, path=path, start=0.5, end=1.0), _held(4 * half))
    assert np.array_equal(out, np.tile(data[half:], 4))


def test_everything_before_the_loop_plays_once_as_the_attack(noise_wav):
    """The onset is what makes a recording an instrument: the playhead
    still starts at `start`, so the strike sounds once and only the loop
    repeats."""
    path, data = noise_wav
    n = len(data)
    out = _render(
        dict(LOOP, path=path, loop_start=0.25, loop_end=0.5), _held(12000)
    )
    expected = np.concatenate([data[:n // 2]] + [data[n // 4:n // 2]] * 10)
    assert np.array_equal(out, expected[:12000])


def test_a_loop_outlives_the_region_a_gated_voice_dies_at(noise_wav):
    """The whole point of the mode. The gated render is the control: same
    file, same gate, and it has been silent for a quarter of a second by
    the time the loop is still going."""
    path, data = noise_wav
    gate = np.zeros(20000, dtype=np.float32)
    gate[:15000] = 1.0
    gated = _render(dict(NEUTRAL, path=path, mode="gated"), gate)
    looped = _render(dict(LOOP, path=path), gate)
    assert np.all(gated[len(data):] == 0.0), "the control did not run out"
    assert np.any(np.abs(looped[14000:15000]) > 0.0), "the loop ran out"


def test_the_loop_still_lets_go_of_the_key(noise_wav):
    """`loop` is `gated` underneath: it sustains, it does not run away."""
    path, _data = noise_wav
    release_ms = 10.0
    gate = np.zeros(20000, dtype=np.float32)
    gate[:15000] = 1.0
    out = _render(dict(LOOP, path=path, release=release_ms), gate)
    release_n = int(round(release_ms * 1e-3 * SR))
    assert np.all(out[15000 + release_n:] == 0.0), "the loop outlived its gate"
    assert np.any(np.abs(out[15000:15000 + release_n]) > 0), "release was instant"


def test_the_seam_crossfade_removes_the_wrap_step(ramp_wav):
    """A claim about a discontinuity needs two renders: the same loop with
    the fade off and on. The ramp wraps 1.0 -> 0.0, which is exactly the
    click `loop_xfade` exists to hide."""
    path, _data = ramp_wav
    gate = _held(12000)
    hard = _render(dict(LOOP, path=path, loop_xfade=0.0), gate)
    faded = _render(dict(LOOP, path=path, loop_xfade=10.0), gate)
    hard_step = float(np.abs(np.diff(hard)).max())
    faded_step = float(np.abs(np.diff(faded)).max())
    assert hard_step > 0.9, "the control did not actually step"
    assert faded_step < hard_step / 50, (
        f"seam step {faded_step} with the fade vs {hard_step} without"
    )


def test_the_crossfaded_seam_is_as_smooth_as_the_recording(noise_wav):
    """Not merely "smaller". The fade ends on the lap before, so the wrap
    lands between two ADJACENT samples of the file: the seam step is an
    ordinary step plus the fade's own slew, and nothing more."""
    path, data = noise_wav
    natural = float(np.abs(np.diff(data)).max())
    xfade_ms = 10.0
    out = _render(
        dict(LOOP, path=path, loop_start=0.25, loop_end=0.5,
             loop_xfade=xfade_ms),
        _held(12000),
    )
    # The fade itself slews between two reads at most 2*peak apart, over
    # its whole length -- a few thousandths next to a real sample step.
    slew = 2.0 * float(np.abs(data).max()) / (xfade_ms * 1e-3 * SR)
    worst = float(np.abs(np.diff(out[2500:])).max())
    assert worst <= natural + slew, f"{worst} vs {natural} natural + {slew}"


def test_the_crossfade_cannot_outrun_the_loop(noise_wav):
    """100 ms of fade asked of a 4.5 ms loop is clamped to the loop rather
    than reading laps that were never played."""
    path, data = noise_wav
    short = dict(LOOP, path=path, loop_start=0.0, loop_end=0.05)
    loop_n = 0.05 * len(data)
    asked_too_much = _render(dict(short, loop_xfade=100.0), _held(8000))
    exactly_the_loop = _render(
        dict(short, loop_xfade=loop_n / SR * 1000.0), _held(8000)
    )
    assert np.array_equal(asked_too_much, exactly_the_loop)


def test_an_inverted_loop_region_plays_as_gated(noise_wav):
    """Not silence, the way an inverted *playback* region is. This is a
    slider you can drag past its partner, and going quiet would be a trap
    rather than an answer."""
    path, _data = noise_wav
    gate = np.zeros(12000, dtype=np.float32)
    gate[:8000] = 1.0
    gated = _render(dict(NEUTRAL, path=path, mode="gated"), gate)
    inverted = _render(dict(LOOP, path=path, loop_start=0.8, loop_end=0.2), gate)
    assert np.array_equal(inverted, gated)


def test_the_loop_survives_block_joins_bit_exact(noise_wav):
    """Wrap and crossfade are both derived from the playhead position, so
    where the block boundaries fall must not matter — including when a
    seam lands inside a block."""
    path, _data = noise_wav
    gate = _held(12288)
    params = dict(LOOP, path=path, loop_start=0.13, loop_end=0.77,
                  loop_xfade=7.0)
    one = _render(params, gate, chunks=1)
    many = _render(params, gate, chunks=48)
    assert np.array_equal(one, many)


def test_loops_do_not_bleed_between_voices(noise_wav):
    """The mellotron claim is a polyphonic one: sixteen keys, sixteen
    independent laps."""
    path, data = noise_wav
    frames = 8000
    gate = np.zeros((2, frames), dtype=np.float32)
    gate[0, :] = 1.0                  # slot 0 held from the first sample
    gate[1, 2000:] = 1.0              # slot 1 joins later
    pitch = np.zeros((2, frames), dtype=np.float32)
    out = _render(dict(LOOP, path=path), gate, pitch=pitch)
    assert np.array_equal(out[0], np.tile(data, 2))
    assert np.all(out[1, :2000] == 0.0), "slot 1 sounded before its gate"
    assert np.array_equal(out[1, 2000:2000 + len(data)], data)


def test_the_other_modes_ignore_the_loop_params(noise_wav):
    """A tripwire on slice 1's neutral: the loop knobs must not leak into
    `one_shot` or `gated`, or the bit-exactness above stops meaning
    anything the moment someone drags a loop slider."""
    path, _data = noise_wav
    gate = _hit(8000)
    for mode in ("one_shot", "gated"):
        plain = _render(dict(NEUTRAL, path=path, mode=mode), gate)
        meddled = _render(
            dict(NEUTRAL, path=path, mode=mode, loop_start=0.3,
                 loop_end=0.6, loop_xfade=55.0),
            gate,
        )
        assert np.array_equal(plain, meddled), mode


# ----- slice 3: stereo, the mip chain, start_cv, reverse, vel ----------------

def test_slice_3_defaults_did_not_move_the_sound():
    """New params default OFF: a slice-2 patch renders exactly as it did."""
    assert Sampler.DEFAULT_PARAMS["reverse"] is False
    assert Sampler.DEFAULT_PARAMS["antialias"] is False
    assert Sampler.DEFAULT_PARAMS["start_cv_depth"] == 1.0


# -- stereo --------------------------------------------------------------------

def test_a_mono_file_feeds_all_three_outs_identically(noise_wav):
    path, data = noise_wav
    res = _render_all(dict(NEUTRAL, path=path), _hit(8000))
    assert np.array_equal(res["out"][:len(data)], data)
    assert np.array_equal(res["out_l"], res["out"])
    assert np.array_equal(res["out_r"], res["out"])


def test_a_stereo_file_keeps_its_channels_apart_bit_exact(tmp_path):
    """`out_l` is the left channel verbatim, `out_r` the right — the
    passthrough contract now holds per channel — and `out` is still the
    half-sum the mono days promised."""
    rng = np.random.default_rng(3)
    left = (rng.standard_normal(1000) * 0.3).astype(np.float32)
    right = (rng.standard_normal(1000) * 0.3).astype(np.float32)
    path = tmp_path / "stereo.wav"
    wavfile.write(path, SR, np.stack([left, right], axis=1))
    res = _render_all(dict(NEUTRAL, path=str(path)), _hit(4000))
    assert np.array_equal(res["out_l"][:1000], left)
    assert np.array_equal(res["out_r"][:1000], right)
    assert np.allclose(res["out"][:1000], 0.5 * (left + right), atol=1e-7)
    assert not np.array_equal(res["out_l"], res["out_r"])


def test_stereo_voices_keep_their_rows(tmp_path):
    left = np.linspace(-0.5, 0.5, 1000).astype(np.float32)
    right = np.linspace(0.5, -0.5, 1000).astype(np.float32)
    path = tmp_path / "stereo.wav"
    wavfile.write(path, SR, np.stack([left, right], axis=1))
    gate = np.zeros((3, 3000), dtype=np.float32)
    gate[0, 0:10] = 1.0
    gate[2, 1000:1010] = 1.0
    res = _render_all(dict(NEUTRAL, path=str(path)), gate)
    assert res["out_l"].shape == (3, 3000)
    assert np.array_equal(res["out_l"][0, :1000], left)
    assert np.array_equal(res["out_r"][2, 1000:2000], right)
    assert np.all(res["out_l"][1] == 0.0)


# -- the mip chain (`antialias`) ------------------------------------------------

def test_mip_blend_is_the_original_at_and_below_unity():
    from pysynthrack.modules.sampler import mip_blend
    assert mip_blend(1.0, 6) == (0, 0, 0.0)
    assert mip_blend(0.5, 6) == (0, 0, 0.0)
    assert mip_blend(0.999, 6) == (0, 0, 0.0)
    assert mip_blend(4.0, 0) == (0, 0, 0.0), "no chain -> the original"


def test_mip_blend_lands_on_a_single_level_at_exact_octaves():
    from pysynthrack.modules.sampler import mip_blend
    assert mip_blend(2.0, 6) == (1, 1, 0.0)
    assert mip_blend(4.0, 6) == (2, 2, 0.0)
    assert mip_blend(-2.0, 6) == (1, 1, 0.0), "reverse reads the same level"


def test_mip_blend_crossfades_between_octaves_and_saturates():
    from pysynthrack.modules.sampler import mip_blend
    k0, k1, frac = mip_blend(3.0, 6)
    assert (k0, k1) == (1, 2)
    assert frac == pytest.approx(np.log2(1.5))
    assert mip_blend(1000.0, 3) == (3, 3, 0.0), "past the chain: the top level"


def test_antialias_leaves_the_neutral_bit_exact(noise_wav):
    """The whole point of level 0 being the untouched original."""
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path, antialias=True), _hit(8000))
    assert np.array_equal(out[:len(data)], data)


def test_antialias_leaves_pitch_down_alone(noise_wav):
    path, _data = noise_wav
    plain = _render(dict(NEUTRAL, path=path, root=72.0), _hit(9000))
    clean = _render(dict(NEUTRAL, path=path, root=72.0, antialias=True), _hit(9000))
    assert np.array_equal(plain, clean)


def test_antialias_octave_up_is_the_first_mip_level_verbatim(noise_wav):
    """Rate 2.0 from level 1 lands on integers just as the naive `[::2]`
    did — the exactness moved up the chain rather than being lost."""
    path, data = noise_wav
    patch = Patch()
    smp = patch.add_module("sampler", params=dict(NEUTRAL, path=path, root=48.0,
                                                  antialias=True))
    trig = patch.add_module("key_trigger")
    patch.connect(trig.id, "out", smp.id, "gate")
    backend = NumpyBackend(sample_rate=SR, block_size=8000)
    backend.compile(patch)
    backend.wait_for_sample_loads()
    out = backend._render_sampler(smp, 8000, {(trig.id, "out"): _hit(8000)}, patch)["out"]
    level1 = backend._state[smp.id]["chains"][0][1]
    assert level1.dtype == np.float32
    assert len(level1) == len(data) // 2
    assert np.array_equal(out[:len(level1)], level1)


@pytest.fixture(scope="module")
def bright_wav(tmp_path_factory):
    """A tone at 0.7 * Nyquist: legal to play, but an octave up it would
    fold — exactly the content the mip chain exists to remove."""
    t = np.arange(SR, dtype=np.float64) / SR
    f0 = 0.7 * SR / 2
    data = (0.5 * np.sin(2 * np.pi * f0 * t)).astype(np.float32)
    path = tmp_path_factory.mktemp("samples") / "bright.wav"
    wavfile.write(path, SR, data)
    return str(path), f0


def _tone_db(seg, freq):
    spectrum = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    i = int(np.argmin(np.abs(freqs - freq)))
    return 20.0 * np.log10(max(spectrum[i - 2:i + 3].max(), 1e-12))


def test_antialias_removes_the_fold_at_an_octave_up(bright_wav):
    """A spectral claim needs a two-render A/B: the same octave-up read
    with the chain off and on. Off, the 0.7-Nyquist tone folds to 0.6;
    on, level 1 never had anything above half Nyquist to fold."""
    path, f0 = bright_wav
    gate = _hit(20000, width=20000)
    alias_hz = SR - 2 * f0
    crunchy = _render(dict(NEUTRAL, path=path, root=48.0, mode="gated"), gate)
    clean = _render(dict(NEUTRAL, path=path, root=48.0, mode="gated",
                         antialias=True), gate)
    off = _tone_db(crunchy[1000:17000].astype(np.float64), alias_hz)
    on = _tone_db(clean[1000:17000].astype(np.float64), alias_hz)
    assert off > -20, f"the control did not alias ({off:.1f} dB)"
    assert on < off - 40, f"alias {off:.1f} dB off -> {on:.1f} dB on"


def test_antialias_attenuates_the_fold_between_octaves(bright_wav):
    """Seven semitones is rate 1.498: between level 0 and level 1, so the
    read is a crossfade and the fold is attenuated by level 0's weight
    (about 7.6 dB), not gone. Honest about what a mip blend is."""
    path, f0 = bright_wav
    gate = _hit(20000, width=20000)
    rate = 2 ** (7 / 12)
    alias_hz = SR - rate * f0
    pitch = np.full(20000, 7.0 / 12.0, dtype=np.float32)
    crunchy = _render(dict(NEUTRAL, path=path, mode="gated"), gate, pitch=pitch)
    clean = _render(dict(NEUTRAL, path=path, mode="gated", antialias=True),
                    gate, pitch=pitch)
    off = _tone_db(crunchy[1000:17000].astype(np.float64), alias_hz)
    on = _tone_db(clean[1000:17000].astype(np.float64), alias_hz)
    assert off > -20
    assert on < off - 5, f"alias {off:.1f} dB off -> {on:.1f} dB on"


def test_antialias_keeps_the_wanted_tone_where_it_was(sine_wav):
    """The two levels are read in phase (a centred half-band filter has
    no delay), so their crossfade sums to unity in the passband: 440 Hz
    played up a fifth is still the same fifth at the same level."""
    path, _data = sine_wav
    frames = 30000
    gate = _hit(frames, width=20000)
    pitch = np.full(frames, 7.0 / 12.0, dtype=np.float32)
    want = 440.0 * 2 ** (7 / 12)
    plain = _render(dict(NEUTRAL, path=path, mode="gated"), gate, pitch=pitch)
    clean = _render(dict(NEUTRAL, path=path, mode="gated", antialias=True),
                    gate, pitch=pitch)
    seg_p = plain[2000:18000].astype(np.float64)
    seg_c = clean[2000:18000].astype(np.float64)
    spectrum = np.abs(np.fft.rfft(seg_c * np.hanning(len(seg_c))))
    freqs = np.fft.rfftfreq(len(seg_c), 1 / SR)
    peak = freqs[np.argmax(spectrum)]
    assert abs(1200 * np.log2(peak / want)) < 10
    assert abs(_tone_db(seg_c, want) - _tone_db(seg_p, want)) < 1.0


def test_the_chain_stops_on_a_short_file(tmp_path):
    from pysynthrack.modules.sampler import MIP_LEVELS, MIP_MIN_SAMPLES
    data = np.linspace(-0.5, 0.5, 40).astype(np.float32)
    path = tmp_path / "tiny.wav"
    wavfile.write(path, SR, data)
    patch = Patch()
    smp = patch.add_module("sampler", params=dict(NEUTRAL, path=str(path)))
    trig = patch.add_module("key_trigger")
    patch.connect(trig.id, "out", smp.id, "gate")
    backend = NumpyBackend(sample_rate=SR, block_size=64)
    backend.compile(patch)
    backend.wait_for_sample_loads()
    backend._render_sampler(smp, 64, {(trig.id, "out"): _hit(64)}, patch)
    chain = backend._state[smp.id]["chains"][0]
    assert 1 < len(chain) <= MIP_LEVELS + 1
    assert all(len(level) >= MIP_MIN_SAMPLES for level in chain[1:])
    for k, level in enumerate(chain):
        assert len(level) == len(data) // (2 ** k)


# -- start_cv ------------------------------------------------------------------

def test_start_cv_moves_the_hit_and_stays_bit_exact(noise_wav):
    """Depth 1, half a volt: the hit starts halfway through the file, and
    at unity rate it is the second half of the file verbatim."""
    path, data = noise_wav
    n = len(data)
    cv = np.full(6000, 0.5, dtype=np.float32)
    out = _render(dict(NEUTRAL, path=path), _hit(6000), start_cv=cv)
    assert np.array_equal(out[:n // 2], data[n // 2:])
    assert np.all(out[n // 2:] == 0.0)


def test_start_cv_depth_scales_the_offset(noise_wav):
    path, data = noise_wav
    n = len(data)
    cv = np.full(6000, 1.0, dtype=np.float32)
    out = _render(dict(NEUTRAL, path=path, start_cv_depth=0.25), _hit(6000),
                  start_cv=cv)
    assert np.array_equal(out[:n - n // 4], data[n // 4:])


def test_start_cv_adds_to_the_start_knob(noise_wav):
    path, data = noise_wav
    n = len(data)
    cv = np.full(6000, 0.25, dtype=np.float32)
    out = _render(dict(NEUTRAL, path=path, start=0.25), _hit(6000), start_cv=cv)
    assert np.array_equal(out[:n // 2], data[n // 2:])


def test_start_cv_is_read_at_the_edge_not_the_block_mean(noise_wav):
    """A sequencer step and the gate that fires it land together; the
    slice point must be what the CV said AT that sample, whatever it does
    for the rest of the block."""
    path, data = noise_wav
    n = len(data)
    cv = np.zeros(6000, dtype=np.float32)
    cv[100:] = 0.5          # the CV steps to 0.5 exactly at the hit...
    cv[3000:] = 0.9         # ...and moves again while the hit is sounding
    out = _render(dict(NEUTRAL, path=path), _hit(6000, at=100), start_cv=cv)
    assert np.array_equal(out[100:100 + n // 2], data[n // 2:])


def test_a_start_pushed_past_the_end_fires_nothing(noise_wav):
    """Not a backwards read and not an exception: the hit is dropped."""
    path, _data = noise_wav
    cv = np.full(6000, 0.9, dtype=np.float32)
    out = _render(dict(NEUTRAL, path=path, end=0.5), _hit(6000), start_cv=cv)
    assert np.all(out == 0.0)


def test_a_dropped_hit_leaves_the_sounding_voice_alone(noise_wav):
    """A retrigger that has nowhere to start should not kill the note that
    was playing — the slicer keeps going."""
    path, data = noise_wav
    n = len(data)
    cv = np.zeros(6000, dtype=np.float32)
    cv[2000:] = 1.0
    gate = _hit(6000, width=10)
    gate[2000:2010] = 1.0
    out = _render(dict(NEUTRAL, path=path), gate, start_cv=cv)
    assert np.array_equal(out[:n], data)


def test_start_cv_is_per_voice(noise_wav):
    path, data = noise_wav
    n = len(data)
    cv = np.zeros((2, 6000), dtype=np.float32)
    cv[1] = 0.5
    out = _render(dict(NEUTRAL, path=path), _hit(6000, voices=2), start_cv=cv)
    assert np.array_equal(out[0, :n], data)
    assert np.array_equal(out[1, :n // 2], data[n // 2:])


def test_the_loop_rides_along_with_start_cv(noise_wav):
    """`loop_start`/`loop_end` are fractions of THE VOICE'S region — the
    one that started where the CV said — so a scrubbed loop is a tiling
    of the file from that point, not from the knob."""
    path, data = noise_wav
    n = len(data)
    cv = np.full(3 * (n // 2), 0.5, dtype=np.float32)
    out = _render(dict(LOOP, path=path), _held(3 * (n // 2)), start_cv=cv)
    assert np.array_equal(out, np.tile(data[n // 2:], 3))


# -- reverse -------------------------------------------------------------------

def test_reverse_is_the_file_backwards_bit_exact(noise_wav):
    """The reverse neutral: rate exactly -1.0 from the last sample walks
    every integer position down to 0."""
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path, reverse=True), _hit(8000))
    assert np.array_equal(out[:len(data)], data[::-1])
    assert np.all(out[len(data):] == 0.0)


def test_reverse_plays_the_region_mirrored(noise_wav):
    path, data = noise_wav
    n = len(data)
    a, b = n // 4, n // 2
    out = _render(dict(NEUTRAL, path=path, start=0.25, end=0.5, reverse=True),
                  _hit(4000))
    assert np.array_equal(out[:b - a], data[a:b][::-1])
    assert np.all(out[b - a:] == 0.0)


def test_reverse_an_octave_up_is_every_other_sample_backwards(noise_wav):
    path, data = noise_wav
    out = _render(dict(NEUTRAL, path=path, root=48.0, reverse=True), _hit(8000))
    want = data[::-1][::2]
    assert np.array_equal(out[:len(want)], want)


def test_a_reversed_loop_tiles_the_region_backwards(noise_wav):
    path, data = noise_wav
    n = len(data)
    a, b = n // 4, n // 2
    out = _render(dict(LOOP, path=path, start=0.25, end=0.5, reverse=True),
                  _held(4 * (b - a)))
    assert np.array_equal(out, np.tile(data[a:b][::-1], 4))


def test_the_reversed_seam_crossfade_removes_the_wrap_step(ramp_wav):
    """Mirror of the forward A/B: a reversed ramp wraps 0.0 -> 1.0 at
    `loop_start`, and the fade — reading one lap LATER now — hides it."""
    path, _data = ramp_wav
    gate = _held(12000)
    hard = _render(dict(LOOP, path=path, loop_xfade=0.0, reverse=True), gate)
    faded = _render(dict(LOOP, path=path, loop_xfade=10.0, reverse=True), gate)
    hard_step = float(np.abs(np.diff(hard)).max())
    faded_step = float(np.abs(np.diff(faded)).max())
    assert hard_step > 0.9, "the control did not actually step"
    assert faded_step < hard_step / 50


def test_reverse_survives_block_joins_bit_exact(noise_wav):
    """At unity, exactly (the forward test's claim, mirrored)."""
    path, _data = noise_wav
    gate = _held(12288)
    params = dict(LOOP, path=path, loop_start=0.13, loop_end=0.77,
                  loop_xfade=7.0, reverse=True)
    one = _render(params, gate, chunks=1)
    many = _render(params, gate, chunks=48)
    assert np.array_equal(one, many)


def test_a_fractional_rate_survives_block_joins_to_rounding(noise_wav):
    """Off the integers a playhead handed across a block boundary is
    `pos + rate*count` rather than one long `arange`, so the claim is
    "to float rounding" — but it must be THAT close, with reverse, the
    mip crossfade and a seam all in play. A state slip would be 1e-1."""
    path, _data = noise_wav
    gate = _held(12288)
    params = dict(LOOP, path=path, loop_start=0.13, loop_end=0.77,
                  loop_xfade=7.0, reverse=True, antialias=True, root=55.0)
    one = _render(params, gate, chunks=1)
    many = _render(params, gate, chunks=48)
    assert np.allclose(one, many, atol=1e-5)


def test_reverse_still_lets_go_of_the_key(sine_wav):
    path, _data = sine_wav
    gate = np.zeros(20000, dtype=np.float32)
    gate[:15000] = 1.0
    out = _render(dict(NEUTRAL, path=path, mode="gated", reverse=True,
                       release=10.0), gate)
    release_n = int(round(10.0 * 1e-3 * SR))
    assert np.all(out[15000 + release_n:] == 0.0)
    assert np.any(np.abs(out[15000:15000 + release_n]) > 0)


# -- vel -----------------------------------------------------------------------

def test_vel_scales_the_hit(noise_wav):
    path, data = noise_wav
    vel = np.full(6000, 0.5, dtype=np.float32)
    out = _render(dict(NEUTRAL, path=path), _hit(6000), vel=vel)
    assert np.allclose(out[:len(data)], 0.5 * data, atol=1e-7)


def test_vel_is_latched_at_the_edge(noise_wav):
    """Velocity is a property of the hit, not a tremolo: what the CV does
    after the edge changes nothing."""
    path, data = noise_wav
    vel = np.full(6000, 0.5, dtype=np.float32)
    vel[1000:] = 0.0
    out = _render(dict(NEUTRAL, path=path), _hit(6000), vel=vel)
    assert np.allclose(out[:len(data)], 0.5 * data, atol=1e-7)


def test_vel_unpatched_is_unity_and_negative_is_silence(noise_wav):
    path, data = noise_wav
    assert np.array_equal(
        _render(dict(NEUTRAL, path=path), _hit(6000))[:len(data)], data
    )
    vel = np.full(6000, -0.5, dtype=np.float32)
    out = _render(dict(NEUTRAL, path=path), _hit(6000), vel=vel)
    assert np.all(out == 0.0)


def test_vel_is_per_voice(noise_wav):
    path, data = noise_wav
    vel = np.zeros((2, 6000), dtype=np.float32)
    vel[0] = 1.0
    vel[1] = 0.25
    out = _render(dict(NEUTRAL, path=path), _hit(6000, voices=2), vel=vel)
    assert np.array_equal(out[0, :len(data)], data)
    assert np.allclose(out[1, :len(data)], 0.25 * data, atol=1e-7)


def test_a_retrigger_tail_keeps_its_own_velocity(sine_wav):
    """The 2 ms crossfade out of a re-struck voice carries the OLD hit's
    gain; a quiet retrigger under a loud note must not step the tail."""
    path, _data = sine_wav
    frames = 6000
    gate = _hit(frames, width=10)
    gate[3000:3010] = 1.0
    vel = np.full(frames, 1.0, dtype=np.float32)
    vel[2000:] = 0.0
    out = _render(dict(NEUTRAL, path=path), gate, vel=vel)
    # After the retrigger the new hit is silent (vel 0); all that sounds is
    # the old tail ramping down from full level -- which must be smooth.
    tail = out[3000:3000 + 200].astype(np.float64)
    assert np.abs(tail[0]) > 0.0
    assert float(np.abs(np.diff(tail)).max()) < 0.05


# -- the face's data -------------------------------------------------------------

def test_the_overview_hook_describes_the_loaded_file(noise_wav):
    from pysynthrack.modules.sampler import OVERVIEW_COLS
    path, data = noise_wav
    patch = Patch()
    smp = patch.add_module("sampler", params=dict(NEUTRAL, path=path))
    trig = patch.add_module("key_trigger")
    patch.connect(trig.id, "out", smp.id, "gate")
    backend = NumpyBackend(sample_rate=SR, block_size=512)
    assert backend.sampler_overview(smp.id) == (None, None)
    backend.compile(patch)
    backend.wait_for_sample_loads()
    backend._render_sampler(smp, 512, {(trig.id, "out"): _hit(512)}, patch)
    overview, loaded = backend.sampler_overview(smp.id)
    assert loaded is not None and loaded.endswith("noise.wav")
    assert overview.shape == (OVERVIEW_COLS, 2)
    assert np.all(overview[:, 0] <= overview[:, 1])
    assert float(overview[:, 1].max()) == pytest.approx(float(data.max()), abs=1e-6)
    assert float(overview[:, 0].min()) == pytest.approx(float(data.min()), abs=1e-6)
    # The lone spike at sample 500 lands in column 25 (4000 / 200 = 20 a column).
    assert float(overview[25, 1]) == pytest.approx(0.9, abs=1e-6)


# ----- the node's widgets -----------------------------------------------------

pytest.importorskip("dearpygui.dearpygui")


def _ui_app(monkeypatch):
    from unittest import mock

    import pysynthrack.ui.app as app_mod

    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = NumpyBackend(sample_rate=SR, block_size=512)
    app.patch = Patch()
    return app, app_mod


def test_every_sampler_param_gets_a_purpose_built_widget(monkeypatch):
    """No sampler param should fall through to a free-range drag: `root` is a
    note, `mode` is a choice, and the rest are bounded."""
    app, app_mod = _ui_app(monkeypatch)
    module = app.patch.add_module("sampler")
    app._create_node_for_module(module)
    combos = [c.kwargs.get("label") for c in app_mod.dpg.add_combo.call_args_list]
    sliders = [
        c.kwargs.get("label") for c in app_mod.dpg.add_slider_float.call_args_list
    ]
    assert any("root" in str(label) for label in combos)
    assert "mode" in combos
    for name in ("tune", "fine", "start", "end"):
        assert name in sliders, f"{name} has no bounded slider"
    for name in ("attack", "release", "loop_start", "loop_end", "loop_xfade"):
        assert any(name in str(label) for label in sliders), (
            f"{name} has no bounded slider"
        )


def test_the_mode_combo_offers_the_loop(monkeypatch):
    """The mellotron mode has to be reachable without hand-editing JSON."""
    app, app_mod = _ui_app(monkeypatch)
    module = app.patch.add_module("sampler")
    app._create_node_for_module(module)
    modes = [
        c.kwargs.get("items") for c in app_mod.dpg.add_combo.call_args_list
        if c.kwargs.get("label") == "mode"
    ]
    # Match on the whole item list: the mock also carries combos built
    # while the App itself was constructed, and `loop` alone would happily
    # match the function generator's mode combo.
    assert list(SAMPLER_MODES) in modes, modes


def test_the_path_field_gets_the_shared_browse_button(monkeypatch):
    app, app_mod = _ui_app(monkeypatch)
    module = app.patch.add_module("sampler")
    app._create_node_for_module(module)
    buttons = [c.kwargs.get("label") for c in app_mod.dpg.add_button.call_args_list]
    assert "Browse..." in buttons


def test_the_root_combo_stores_a_midi_number(monkeypatch):
    """The panel reads musically ('C3'); the patch stays numeric so the
    renderer's rate maths never parses a string."""
    app, _app_mod = _ui_app(monkeypatch)
    module = app.patch.add_module("sampler")
    app._on_sampler_root_changed(None, "C3", (module.id, "root"))
    assert module.params["root"] == 48.0
    assert isinstance(module.params["root"], float)


def test_an_unparseable_root_leaves_the_instrument_alone(monkeypatch):
    app, _app_mod = _ui_app(monkeypatch)
    module = app.patch.add_module("sampler")
    before = module.params["root"]
    app._on_sampler_root_changed(None, "not a note", (module.id, "root"))
    assert module.params["root"] == before


def test_slice_3_params_get_their_widgets(monkeypatch):
    """`reverse` / `antialias` are tickboxes; `start_cv_depth` is bounded
    and labelled with its unit, not a free-range drag."""
    app, app_mod = _ui_app(monkeypatch)
    module = app.patch.add_module("sampler")
    app._create_node_for_module(module)
    boxes = [c.kwargs.get("label") for c in app_mod.dpg.add_checkbox.call_args_list]
    assert "reverse" in boxes
    assert "antialias" in boxes
    sliders = [
        c.kwargs.get("label") for c in app_mod.dpg.add_slider_float.call_args_list
    ]
    assert any("start_cv_depth" in str(label) for label in sliders)
    drags = [c.kwargs.get("label") for c in app_mod.dpg.add_drag_float.call_args_list]
    assert not any("start_cv_depth" in str(label) for label in drags)


def test_the_node_gets_a_waveform_face(monkeypatch):
    app, app_mod = _ui_app(monkeypatch)
    module = app.patch.add_module("sampler")
    app._create_node_for_module(module)
    face = app._sampler_faces.get(module.id)
    assert face is not None
    assert set(face["markers"]) == {"start", "end", "loop_start", "loop_end"}
    assert app_mod.dpg.drawlist.called


def test_the_face_paints_the_file_and_its_markers(monkeypatch, noise_wav):
    """With a loaded sample the face gets the envelope polyline and the
    region markers land at start/end; loop markers show only in loop mode
    and sit INSIDE the region (they are fractions of it)."""
    import itertools

    path, _data = noise_wav
    app, app_mod = _ui_app(monkeypatch)
    # A MagicMock hands every draw_* call the SAME return value, which
    # would fold the four markers into one key. Give each item its own id.
    ids = itertools.count(1000)
    for name in ("draw_line", "draw_polyline", "draw_text"):
        getattr(app_mod.dpg, name).side_effect = lambda *a, **k: next(ids)
    module = app.patch.add_module(
        "sampler", params=dict(NEUTRAL, path=path, start=0.25, end=0.75,
                               mode="loop", loop_start=0.5, loop_end=1.0)
    )
    trig = app.patch.add_module("key_trigger")
    app.patch.connect(trig.id, "out", module.id, "gate")
    app._create_node_for_module(module)
    app.backend.compile(app.patch)
    app.backend.wait_for_sample_loads()
    app.backend._render_sampler(module, 64, {(trig.id, "out"): _hit(64)}, app.patch)

    app_mod.dpg.configure_item.reset_mock()
    app._update_sampler_faces()
    face = app._sampler_faces[module.id]
    calls = {c.args[0]: c.kwargs for c in app_mod.dpg.configure_item.call_args_list}
    assert len(calls[face["wave"]]["points"]) > 100
    w = float(app._SFACE_W) - 1.0
    assert calls[face["markers"]["start"]]["p1"][0] == pytest.approx(0.25 * w)
    assert calls[face["markers"]["end"]]["p1"][0] == pytest.approx(0.75 * w)
    # loop_start 0.5 of a 0.25..0.75 region is the file's 0.5 mark.
    assert calls[face["markers"]["loop_start"]]["p1"][0] == pytest.approx(0.5 * w)
    assert calls[face["markers"]["loop_start"]]["show"] is True

    # Nothing moved -> nothing repainted.
    app_mod.dpg.configure_item.reset_mock()
    app._update_sampler_faces()
    assert not app_mod.dpg.configure_item.called

    # Leave loop mode -> the loop markers hide, the region ones stay.
    module.params["mode"] = "one_shot"
    app._update_sampler_faces()
    calls = {c.args[0]: c.kwargs for c in app_mod.dpg.configure_item.call_args_list}
    assert calls[face["markers"]["loop_start"]]["show"] is False
    assert calls[face["markers"]["start"]]["show"] is True


def test_a_face_with_no_sample_says_so(monkeypatch):
    import itertools

    app, app_mod = _ui_app(monkeypatch)
    ids = itertools.count(1000)
    for name in ("draw_line", "draw_polyline", "draw_text"):
        getattr(app_mod.dpg, name).side_effect = lambda *a, **k: next(ids)
    module = app.patch.add_module("sampler")
    app._create_node_for_module(module)
    app.backend.compile(app.patch)
    app_mod.dpg.configure_item.reset_mock()
    app._update_sampler_faces()
    face = app._sampler_faces[module.id]
    calls = {c.args[0]: c.kwargs for c in app_mod.dpg.configure_item.call_args_list}
    assert calls[face["caption"]]["text"] == "no sample"
    assert all(
        calls[item]["show"] is False for item in face["markers"].values()
    )
