"""Sampler — pitched, gate-triggered sample playback (slice 1).

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
    assert [p.name for p in Sampler.INPUT_PORTS] == ["pitch_cv", "gate"]
    assert [p.name for p in Sampler.OUTPUT_PORTS] == ["out"]
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


def _render(params, gate, pitch=None, chunks=1):
    """Render a sampler with an exact gate (and optional pitch) buffer."""
    frames = gate.shape[-1]
    patch = Patch()
    smp = patch.add_module("sampler", params=params)
    trig = patch.add_module("key_trigger")
    patch.connect(trig.id, "out", smp.id, "gate")
    src = None
    if pitch is not None:
        src = patch.add_module("constant")
        patch.connect(src.id, "out", smp.id, "pitch_cv")
    backend = NumpyBackend(sample_rate=SR, block_size=frames // chunks)
    backend.compile(patch)
    backend.wait_for_sample_loads()
    n = frames // chunks
    out = []
    for k in range(chunks):
        sl = (Ellipsis, slice(k * n, (k + 1) * n))
        bufs = {(trig.id, "out"): gate[sl]}
        if pitch is not None:
            bufs[(src.id, "out")] = pitch[sl]
        out.append(backend._render_sampler(smp, n, bufs, patch))
    return np.concatenate(out, axis=-1)


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
    out = backend._render_sampler(smp, 512, {}, patch)
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
    assert any("attack" in str(label) for label in sliders)
    assert any("release" in str(label) for label in sliders)


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
