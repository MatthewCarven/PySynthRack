"""``--render``: a patch rendered offline to a WAV file.

Everything here runs with no audio device: ``render_cli`` builds the numpy
backend itself and pulls ``render_block``, so the tests are the proof that
the render path needs neither PortAudio nor a sound card.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from pysynthrack.__main__ import main
from pysynthrack.cli import RENDER_SAMPLE_RATE, render_cli

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
HELLO = str(EXAMPLES / "hello_sine.json")


def _read(path):
    """(params, samples as float in -1..1 with shape (frames, channels))."""
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        raw = wav.readframes(params.nframes)
    data = np.frombuffer(raw, dtype="<i2").reshape(-1, params.nchannels)
    return params, data.astype(np.float64) / 32767.0


def test_render_writes_a_stereo_16_bit_wav_of_the_asked_length(tmp_path):
    out = tmp_path / "hello.wav"
    assert render_cli(HELLO, str(out), 0.5) == 0
    params, data = _read(out)
    assert params.nchannels == 2
    assert params.sampwidth == 2
    assert params.framerate == RENDER_SAMPLE_RATE
    assert params.nframes == round(0.5 * RENDER_SAMPLE_RATE)
    assert data.shape == (params.nframes, 2)


def test_render_is_the_patch_and_not_noise(tmp_path):
    """hello_sine is a 440 Hz sine: the file's spectrum peaks there."""
    out = tmp_path / "hello.wav"
    assert render_cli(HELLO, str(out), 1.0) == 0
    _params, data = _read(out)
    left = data[:, 0]
    assert 0.05 < np.max(np.abs(left)) < 0.99
    spectrum = np.abs(np.fft.rfft(left * np.hanning(len(left))))
    peak_hz = np.argmax(spectrum) * RENDER_SAMPLE_RATE / len(left)
    assert abs(peak_hz - 440.0) < 2.0


@pytest.mark.parametrize("seconds", [0.3, 0.01, 1.0 / RENDER_SAMPLE_RATE, 0.777])
def test_length_is_exact_when_it_is_not_a_whole_number_of_blocks(tmp_path, seconds):
    out = tmp_path / "odd.wav"
    assert render_cli(HELLO, str(out), seconds) == 0
    params, _data = _read(out)
    assert params.nframes == int(round(seconds * RENDER_SAMPLE_RATE))


def test_mono_is_the_mean_of_the_two_sides(tmp_path):
    stereo, mono = tmp_path / "s.wav", tmp_path / "m.wav"
    assert render_cli(HELLO, str(stereo), 0.25) == 0
    assert render_cli(HELLO, str(mono), 0.25, mono=True) == 0
    sp, sd = _read(stereo)
    mp, md = _read(mono)
    assert mp.nchannels == 1
    assert mp.nframes == sp.nframes
    # Each file is rounded to 16 bits on its own, so allow one step.
    assert np.max(np.abs(md[:, 0] - sd.mean(axis=1))) <= 1.5 / 32767.0


def test_two_renders_of_one_patch_are_byte_identical(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    assert render_cli(HELLO, str(a), 0.4) == 0
    assert render_cli(HELLO, str(b), 0.4) == 0
    assert a.read_bytes() == b.read_bytes()


def test_a_clock_fires_on_the_first_sample(tmp_path):
    """The README's advice for one-shots: a clock's first pulse is sample 0,
    so a clocked envelope sounds from the very start of the file and, at one
    pulse a second, only once in a short render."""
    patch = tmp_path / "blip.json"
    patch.write_text(json.dumps({
        "version": 1, "next_id": 6,
        "modules": [
            {"id": 1, "type": "clock", "name": "c",
             "params": {"bpm": 60.0, "division": 1.0, "pulse_width": 0.1}},
            {"id": 2, "type": "ad_envelope", "name": "e",
             "params": {"attack": 0.001, "decay": 0.1}},
            {"id": 3, "type": "oscillator", "name": "o",
             "params": {"waveform": "square", "freq": 880.0, "amp": 0.5}},
            {"id": 4, "type": "vca", "name": "v", "params": {}},
            {"id": 5, "type": "speaker_output", "name": "out", "params": {"gain": 1.0}},
        ],
        "cables": [
            {"src_module_id": 1, "src_port": "out", "dst_module_id": 2, "dst_port": "trig"},
            {"src_module_id": 2, "src_port": "cv", "dst_module_id": 4, "dst_port": "cv"},
            {"src_module_id": 3, "src_port": "out", "dst_module_id": 4, "dst_port": "audio"},
            {"src_module_id": 4, "src_port": "out", "dst_module_id": 5, "dst_port": "in"},
        ],
    }))
    out = tmp_path / "blip.wav"
    assert render_cli(str(patch), str(out), 0.5, mono=True) == 0
    _params, data = _read(out)
    mono = np.abs(data[:, 0])
    assert mono[:100].max() > 0.01           # sounding within about 2 ms
    assert mono.max() > 0.3                  # the envelope opened
    assert mono[int(0.2 * RENDER_SAMPLE_RATE):].max() < 0.01   # and closed: one hit


@pytest.mark.parametrize("seconds", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_a_missing_or_impossible_length_is_refused_and_writes_nothing(tmp_path, seconds, capsys):
    out = tmp_path / "no.wav"
    assert render_cli(HELLO, str(out), seconds) == 5
    assert not out.exists()
    assert "error" in capsys.readouterr().err


def test_a_missing_patch_is_exit_code_2(tmp_path, capsys):
    out = tmp_path / "no.wav"
    assert render_cli(str(tmp_path / "nope.json"), str(out), 1.0) == 2
    assert not out.exists()
    assert "patch file not found" in capsys.readouterr().err


def test_a_missing_output_folder_is_refused(tmp_path, capsys):
    out = tmp_path / "no_such_folder" / "x.wav"
    assert render_cli(HELLO, str(out), 0.1) == 5
    assert "output folder does not exist" in capsys.readouterr().err


def test_the_pyo_backend_is_refused(tmp_path, capsys):
    out = tmp_path / "no.wav"
    assert render_cli(HELLO, str(out), 0.1, backend_name="pyo") == 3
    assert not out.exists()
    assert "numpy backend" in capsys.readouterr().err


def test_silence_is_written_but_warned_about(tmp_path, capsys):
    """A patch with nothing reaching a speaker still renders: a silent file
    of the right length, and a warning saying so."""
    patch = tmp_path / "empty.json"
    patch.write_text('{"version": 1, "next_id": 1, "modules": [], "cables": []}')
    out = tmp_path / "silent.wav"
    assert render_cli(str(patch), str(out), 0.1) == 0
    params, data = _read(out)
    assert params.nframes == round(0.1 * RENDER_SAMPLE_RATE)
    assert not np.any(data)
    assert "silent" in capsys.readouterr().err


def test_the_command_line_reaches_the_renderer(tmp_path):
    """``--render`` alone is enough: no ``--cli``, and no GUI is imported."""
    out = tmp_path / "cli.wav"
    code = main(["--render", str(out), "--patch", HELLO, "--seconds", "0.2", "--mono"])
    assert code == 0
    params, _data = _read(out)
    assert params.nchannels == 1
    assert params.nframes == round(0.2 * RENDER_SAMPLE_RATE)


def test_the_command_line_without_seconds_is_an_error(tmp_path):
    out = tmp_path / "cli.wav"
    assert main(["--render", str(out), "--patch", HELLO]) == 5
    assert not out.exists()
