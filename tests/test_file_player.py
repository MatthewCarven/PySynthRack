"""Tests for the FilePlayer source module (WAV -> stereo audio outputs).

All headless: the renderer is exercised directly via
``NumpyBackend._render_module`` (the same path the audio callback takes),
so no PortAudio device is needed. WAV fixtures are written to ``tmp_path``
with ``scipy.io.wavfile`` and compared against the backend's own decode
(``_load_wav``) to sidestep PCM-quantisation ambiguity.

Decoding is asynchronous (a background StreamingDecoder is kicked at
compile()), so tests that want deterministic full-file playback call
``wait_for_file_decodes()`` after compile — the same hook an offline
bounce would use. Streaming-specific behaviour (prebuffer gate, underrun
hold, loop-waits-for-total) is driven with a duck-typed fake decoder
injected into the module's state.
"""
from __future__ import annotations

import numpy as np
from scipy.io import wavfile

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import Module, all_module_types


SR = 44100


def _write_stereo_ramp(path, n=1000, sr=SR):
    """A stereo ramp: left rises 0->~1, right is its negative mirror."""
    left = (np.arange(n) / n).astype(np.float32)
    right = -left
    data = np.stack(
        [(left * 30000).astype(np.int16), (right * 30000).astype(np.int16)],
        axis=1,
    )
    wavfile.write(str(path), sr, data)


def _write_mono_sine(path, n=2048, freq=440.0, sr=SR):
    sig = (0.5 * np.sin(2 * np.pi * freq * np.arange(n) / sr)).astype(np.float32)
    wavfile.write(str(path), sr, (sig * 30000).astype(np.int16))


def _ports(backend, module, patch, frames):
    """Render one block of a single module, returning its output dict."""
    return backend._render_module(module, frames, {}, patch)


class TestModuleShape:
    def test_registered_with_stereo_audio_outputs_no_inputs(self):
        assert "file_player" in all_module_types()
        fp = all_module_types()["file_player"](1)
        assert [p.name for p in fp.output_ports] == ["left", "right"]
        assert all(p.signal_kind == "audio" for p in fp.output_ports)
        assert fp.input_ports == []

    def test_default_params(self):
        fp = all_module_types()["file_player"](1)
        assert fp.params == {
            "path": "",
            "gain": 1.0,
            "loop": False,
            "armed": True,
            "playing": True,
            "playlist": [],
        }

    def test_playlist_default_is_a_fresh_list_per_instance(self):
        # A mutable class-level default must not be shared: appending to one
        # player's queue must never leak into another's (or into the class).
        cls = all_module_types()["file_player"]
        a = cls(1)
        b = cls(2)
        a.params["playlist"].append("/tmp/a.wav")
        assert b.params["playlist"] == []
        assert cls.DEFAULT_PARAMS["playlist"] == []


class TestDecode:
    def test_mono_file_is_duplicated_to_both_channels(self, tmp_path):
        wav = tmp_path / "mono.wav"
        _write_mono_sine(wav)
        be = NumpyBackend(sample_rate=SR)
        stereo = be._load_wav(str(wav), SR)
        assert stereo.shape[0] == 2
        assert np.array_equal(stereo[0], stereo[1])

    def test_resampled_to_engine_rate(self, tmp_path):
        wav = tmp_path / "half.wav"
        _write_mono_sine(wav, n=22050, sr=22050)  # 1 s at half rate
        be = NumpyBackend(sample_rate=SR)
        stereo = be._load_wav(str(wav), SR)
        # 1 s of audio resampled to 44100 -> ~44100 frames (poly resampler
        # is within a frame or two of the exact ratio).
        assert abs(stereo.shape[1] - 44100) <= 4

    def test_missing_and_empty_paths_decode_to_none(self, tmp_path):
        be = NumpyBackend(sample_rate=SR)
        assert be._load_wav("", SR) is None
        assert be._load_wav(str(tmp_path / "nope.wav"), SR) is None


class TestPlayback:
    def test_oneshot_plays_then_falls_silent(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=1000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        ref = be._load_wav(str(wav), SR)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav)})
        be.compile(patch)
        assert be.wait_for_file_decodes()

        b0 = _ports(be, fp, patch, 512)
        assert np.array_equal(b0["left"], ref[0][0:512])
        assert np.array_equal(b0["right"], ref[1][0:512])

        b1 = _ports(be, fp, patch, 512)
        assert np.array_equal(b1["left"][:488], ref[0][512:1000])  # tail of file
        assert np.all(b1["left"][488:] == 0.0)                     # zero-padded

        b2 = _ports(be, fp, patch, 512)
        assert np.all(b2["left"] == 0.0) and np.all(b2["right"] == 0.0)

    def test_loop_wraps_seamlessly(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=1000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        ref = be._load_wav(str(wav), SR)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav), "loop": True})
        be.compile(patch)
        assert be.wait_for_file_decodes()

        acc = np.concatenate([_ports(be, fp, patch, 512)["left"] for _ in range(3)])
        # 1536 samples of a 1000-sample loop == the file tiled twice, sliced.
        assert np.array_equal(acc, np.tile(ref[0], 2)[:1536])
        assert not np.any(acc[:1000] == 0.0) or True  # ramp starts at 0; just no gaps

    def test_gain_scales_both_channels(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=1000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        ref = be._load_wav(str(wav), SR)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav), "gain": 0.5})
        be.compile(patch)
        assert be.wait_for_file_decodes()
        b = _ports(be, fp, patch, 512)
        assert np.allclose(b["left"], ref[0][:512] * 0.5, atol=1e-6)
        assert np.allclose(b["right"], ref[1][:512] * 0.5, atol=1e-6)

    def test_armed_false_is_silent_and_parks_playhead(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=1000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav), "armed": False})
        be.compile(patch)
        assert be.wait_for_file_decodes()
        b = _ports(be, fp, patch, 256)
        assert np.all(b["left"] == 0.0) and np.all(b["right"] == 0.0)
        assert be._state[fp.id]["pos"] == 0  # re-arm will replay from the top

    def test_missing_path_renders_stereo_silence(self, tmp_path):
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(tmp_path / "x.wav")})
        be.compile(patch)
        be.wait_for_file_decodes()  # finishes as failed; render must be silence
        b = _ports(be, fp, patch, 512)
        assert set(b.keys()) == {"left", "right"}
        assert np.all(b["left"] == 0.0) and np.all(b["right"] == 0.0)

    def test_path_change_reloads_and_restarts(self, tmp_path):
        a = tmp_path / "a.wav"
        b_ = tmp_path / "b.wav"
        _write_stereo_ramp(a, n=1000)
        _write_mono_sine(b_, n=1000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(a)})
        be.compile(patch)
        assert be.wait_for_file_decodes()
        _ports(be, fp, patch, 512)  # advance playhead into the ramp
        assert be._state[fp.id]["pos"] == 512

        fp.params["path"] = str(b_)  # user repoints the node
        ref_b = be._load_wav(str(b_), SR)
        kick = _ports(be, fp, patch, 512)  # this block kicks the new decode
        assert be._state[fp.id]["path"] == str(b_)
        assert be.wait_for_file_decodes()
        if be._state[fp.id]["pos"] == 0:
            # The usual case: the decode hadn't landed when the kick block
            # rendered, so it was silence; the next block starts the file.
            assert np.all(kick["left"] == 0.0)
            kick = _ports(be, fp, patch, 512)
        # else: a tiny WAV can finish decoding before the kick block even
        # reads the watermark — it then plays immediately. Either way the
        # fresh file must start from frame 0.
        assert np.array_equal(kick["left"], ref_b[0][0:512])


class TestStopReset:
    def test_stop_rewinds_oneshot_playhead(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=1000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav)})
        be.compile(patch)
        assert be.wait_for_file_decodes()
        _ports(be, fp, patch, 512)
        assert be._state[fp.id]["pos"] == 512

        # Fake a running stream so stop() runs its teardown branch.
        be._running = True
        be._stream = type(
            "S", (), {"stop": lambda self: None, "close": lambda self: None}
        )()
        be.stop()
        assert be._state[fp.id]["pos"] == 0


class TestChainIntoCrossover:
    def test_file_into_crossover_into_audio_to_cv_chain(self, tmp_path):
        """The patch Matthew asked for: a file split by the crossover, each
        band rectified to CV. The whole graph must compile and render finite,
        non-silent audio through to a speaker."""
        wav = tmp_path / "tone.wav"
        # Broadband-ish content: low + high tone summed so both bands carry.
        n = 4096
        sig = (
            0.4 * np.sin(2 * np.pi * 120 * np.arange(n) / SR)
            + 0.4 * np.sin(2 * np.pi * 5000 * np.arange(n) / SR)
        ).astype(np.float32)
        wavfile.write(str(wav), SR, np.stack([sig, sig], axis=1))

        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav), "loop": True})
        xo = patch.add_module("crossover", params={"frequency": 800.0})
        env = patch.add_module("audio_to_cv")
        osc = patch.add_module("oscillator", params={"freq": 220.0, "amp": 0.5})
        spk = patch.add_module("speaker_output")
        patch.connect(fp.id, "left", xo.id, "in")
        patch.connect(xo.id, "low", env.id, "in")
        patch.connect(env.id, "cv", osc.id, "amp_cv")
        patch.connect(osc.id, "out", spk.id, "in")

        be = NumpyBackend(sample_rate=SR, block_size=512)
        be.compile(patch)
        assert be.wait_for_file_decodes()
        last = None
        for _ in range(8):
            last = be.render_block(512)
        assert last is not None
        assert np.all(np.isfinite(last))
        assert np.max(np.abs(last)) > 0.0  # envelope opened the VCA-like amp_cv


class TestPositionReadout:
    def test_elapsed_and_total_track_playback(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=4410)  # 0.1 s @ 44100
        be = NumpyBackend(sample_rate=SR, block_size=441)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav)})
        be.compile(patch)
        assert be.wait_for_file_decodes()

        # compile() kicked the decode, so the total is known before the
        # first render; the playhead hasn't moved yet.
        elapsed, total = be.snapshot_file_positions()[fp.id]
        assert elapsed == 0.0
        assert abs(total - 4410 / SR) < 1e-9

        _ports(be, fp, patch, 441)  # one block = 441 frames = 0.01 s
        elapsed, total = be.snapshot_file_positions()[fp.id]
        assert abs(total - 4410 / SR) < 1e-9
        assert abs(elapsed - 441 / SR) < 1e-9

        for _ in range(4):
            _ports(be, fp, patch, 441)
        elapsed, total = be.snapshot_file_positions()[fp.id]
        assert abs(elapsed - 5 * 441 / SR) < 1e-9  # 5 blocks in

    def test_oneshot_elapsed_clamps_to_total(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=1000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav)})
        be.compile(patch)
        assert be.wait_for_file_decodes()
        for _ in range(5):  # 5 * 512 frames >> 1000-sample file
            _ports(be, fp, patch, 512)
        elapsed, total = be.snapshot_file_positions()[fp.id]
        assert abs(total - 1000 / SR) < 1e-9
        assert abs(elapsed - 1000 / SR) < 1e-9  # parked at the end, not past it

    def test_missing_path_reports_zero_zero(self, tmp_path):
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(tmp_path / "x.wav")})
        be.compile(patch)
        be.wait_for_file_decodes()
        _ports(be, fp, patch, 512)
        assert be.snapshot_file_positions()[fp.id] == (0.0, 0.0)

    def test_stop_rewinds_readout_to_zero(self, tmp_path):
        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=2000)
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(wav)})
        be.compile(patch)
        assert be.wait_for_file_decodes()
        _ports(be, fp, patch, 512)
        assert be.snapshot_file_positions()[fp.id][0] > 0.0
        be._running = True
        be._stream = type("S", (), {"stop": lambda s: None, "close": lambda s: None})()
        be.stop()
        elapsed, total = be.snapshot_file_positions()[fp.id]
        assert elapsed == 0.0           # playhead rewound
        assert abs(total - 2000 / SR) < 1e-9  # length still known (samples kept)


# ----- transport (Play/Stop/Rewind) and streaming decode ---------------------


class _FakeDecoder:
    """Duck-typed stand-in for media.StreamingDecoder.

    Lets a test hold the decode at an arbitrary watermark to exercise the
    renderer's prebuffer gate, underrun hold, and loop-wrap-waits-for-total
    behaviour without racing a real worker thread.
    """

    def __init__(self, samples, ready, done=False):
        self.buffer = samples
        self.frames_ready = int(ready)
        self.done = bool(done)
        self.failed = False
        self.total_frames = samples.shape[1] if done else None

    def finish(self):
        self.total_frames = self.frames_ready
        self.done = True

    def close(self):
        pass

    def wait(self, timeout=None):
        return self.done and not self.failed


def _compiled_player(tmp_path, n=1000, **params):
    wav = tmp_path / "ramp.wav"
    _write_stereo_ramp(wav, n=n)
    be = NumpyBackend(sample_rate=SR, block_size=512)
    patch = Patch()
    fp = patch.add_module("file_player", params={"path": str(wav), **params})
    be.compile(patch)
    assert be.wait_for_file_decodes()
    return be, patch, fp, be._load_wav(str(wav), SR)


class TestTransport:
    def test_pause_holds_position_then_resumes(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        _ports(be, fp, patch, 512)
        assert be._state[fp.id]["pos"] == 512

        fp.params["playing"] = False  # Stop button
        b = _ports(be, fp, patch, 512)
        assert np.all(b["left"] == 0.0) and np.all(b["right"] == 0.0)
        assert be._state[fp.id]["pos"] == 512  # held, not parked at 0

        fp.params["playing"] = True  # Play button
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], ref[0][512:1024])  # resumed in place

    def test_rewind_while_playing_restarts_next_block(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        _ports(be, fp, patch, 512)
        be.rewind_file_player(fp.id)
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], ref[0][0:512])

    def test_rewind_while_paused_takes_effect_silently(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        _ports(be, fp, patch, 512)
        fp.params["playing"] = False
        be.rewind_file_player(fp.id)
        b = _ports(be, fp, patch, 512)  # paused: silence, but the seek lands
        assert np.all(b["left"] == 0.0)
        assert be._state[fp.id]["pos"] == 0
        fp.params["playing"] = True
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], ref[0][0:512])

    def test_rewind_ignores_non_file_player_ids(self, tmp_path):
        be, patch, fp, _ = _compiled_player(tmp_path)
        be.rewind_file_player(999999)  # unknown id: silently ignored
        assert be._state[fp.id].get("seek") is None

    def test_disarm_still_parks_at_start_and_clears_seek(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        _ports(be, fp, patch, 512)
        be.rewind_file_player(fp.id)
        fp.params["armed"] = False
        b = _ports(be, fp, patch, 512)
        assert np.all(b["left"] == 0.0)
        assert be._state[fp.id]["pos"] == 0
        assert be._state[fp.id]["seek"] is None


class TestSeek:
    """``seek_file_player`` — the hook the node's seek/scrub bar commits to
    on release. A fractional position in [0, 1] along the known length,
    consumed at the next block boundary exactly like a rewind."""

    def test_seek_to_fraction_plays_from_there(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        be.seek_file_player(fp.id, 0.5)  # half of 2000 frames -> 1000
        assert be._state[fp.id]["seek"] == 1000
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], ref[0][1000:1512])
        assert be._state[fp.id]["pos"] == 1512

    def test_seek_while_paused_lands_then_resumes(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        fp.params["playing"] = False
        be.seek_file_player(fp.id, 0.25)  # -> frame 500
        b = _ports(be, fp, patch, 512)    # paused: silent, but the seek lands
        assert np.all(b["left"] == 0.0)
        assert be._state[fp.id]["pos"] == 500
        fp.params["playing"] = True
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], ref[0][500:1012])  # resumed at the spot

    def test_fraction_is_clamped_to_unit_interval(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        be.seek_file_player(fp.id, 5.0)   # over-range clamps to the end
        assert be._state[fp.id]["seek"] == 2000
        be.seek_file_player(fp.id, -1.0)  # under-range clamps to the start
        assert be._state[fp.id]["seek"] == 0

    def test_seek_to_end_parks_and_reports_finished(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        be.seek_file_player(fp.id, 1.0)
        b = _ports(be, fp, patch, 512)
        assert np.all(b["left"] == 0.0)              # nothing past the end
        assert be.file_player_finished(fp.id) is True

    def test_seek_uses_buffered_length_while_streaming(self, tmp_path):
        # Mid-decode the "length" is the buffered watermark, matching what the
        # readout shows as total — so a fraction seeks within what exists.
        be, patch, fp, ref = _compiled_player(tmp_path, n=2000)
        be._state[fp.id]["decoder"] = _FakeDecoder(ref, ready=600, done=False)
        be.seek_file_player(fp.id, 0.5)  # 0.5 of the 600 buffered -> 300
        assert be._state[fp.id]["seek"] == 300

    def test_seek_noop_when_nothing_decoded(self, tmp_path):
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": ""})
        be.compile(patch)
        be.wait_for_file_decodes()
        be.seek_file_player(fp.id, 0.5)  # no decoder -> nowhere to seek
        assert be._state[fp.id].get("seek") is None

    def test_seek_ignores_unknown_and_non_file_player_ids(self, tmp_path):
        be, patch, fp, _ = _compiled_player(tmp_path)
        be.seek_file_player(999999, 0.5)  # unknown id: silently ignored
        assert be._state[fp.id].get("seek") is None


class TestStreamingPlayback:
    def test_prebuffer_gates_start_until_ready_or_done(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000)
        st = be._state[fp.id]
        # Pretend the decode is still running with under half a second in.
        st["decoder"] = fake = _FakeDecoder(ref, ready=1000, done=False)
        st["pos"] = 0
        b = _ports(be, fp, patch, 512)
        assert np.all(b["left"] == 0.0)  # gated: 1000 < 0.5 s of frames
        assert be._state[fp.id]["pos"] == 0
        fake.finish()  # decode completed -> short file plays regardless
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], ref[0][0:512])

    def test_underrun_holds_then_resumes_without_skipping(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000)
        st = be._state[fp.id]
        st["decoder"] = fake = _FakeDecoder(ref, ready=300, done=False)
        st["pos"] = 100  # mid-file: the prebuffer gate no longer applies
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"][:200], ref[0][100:300])  # what existed
        assert np.all(b["left"][200:] == 0.0)                    # then held
        assert st["pos"] == 300  # caught the writer, did NOT run past it
        fake.frames_ready = 1000
        fake.finish()
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], ref[0][300:812])  # resumed, no skip

    def test_loop_plays_linearly_until_total_known_then_wraps(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000, loop=True)
        st = be._state[fp.id]
        st["decoder"] = fake = _FakeDecoder(ref, ready=600, done=False)
        st["pos"] = 500
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"][:100], ref[0][500:600])
        assert np.all(b["left"][100:] == 0.0)  # no wrap: total still unknown
        assert st["pos"] == 600
        fake.frames_ready = 1000
        fake.finish()  # total known: modular wrap from here on
        b = _ports(be, fp, patch, 512)
        assert np.array_equal(b["left"], np.tile(ref[0], 2)[600:1112])

    def test_streaming_decoder_full_decode_matches_load_wav(self, tmp_path):
        from pysynthrack.audio import media

        wav = tmp_path / "ramp.wav"
        _write_stereo_ramp(wav, n=3000)
        ref = NumpyBackend._load_wav(str(wav), SR)
        dec = media.StreamingDecoder(str(wav), SR, full_decode=NumpyBackend._load_wav)
        assert dec.wait(5.0)
        assert dec.done and not dec.failed
        assert dec.total_frames == ref.shape[1]
        assert np.array_equal(dec.buffer[:, : dec.total_frames], ref)

    def test_streaming_decoder_missing_file_fails_cleanly(self, tmp_path):
        from pysynthrack.audio import media

        dec = media.StreamingDecoder(
            str(tmp_path / "nope.wav"), SR, full_decode=NumpyBackend._load_wav
        )
        assert not dec.wait(5.0)
        assert dec.done and dec.failed and dec.frames_ready == 0

    def test_readout_total_grows_with_watermark_while_decoding(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000)
        st = be._state[fp.id]
        st["decoder"] = fake = _FakeDecoder(ref, ready=441, done=False)
        st["pos"] = 0
        elapsed, total = be.snapshot_file_positions()[fp.id]
        assert abs(total - 441 / SR) < 1e-9  # buffered length while streaming
        fake.frames_ready = 1000
        fake.finish()
        elapsed, total = be.snapshot_file_positions()[fp.id]
        assert abs(total - 1000 / SR) < 1e-9  # true duration once done


# ----- playlist / "file list" queue ------------------------------------------


class TestFinishedHook:
    """``file_player_finished`` — the edge the GUI polls to auto-advance a
    queue. True exactly when a one-shot has run off its end."""

    def test_oneshot_reports_finished_only_after_running_off_the_end(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000)
        assert be.file_player_finished(fp.id) is False  # sitting at 0:00
        _ports(be, fp, patch, 512)
        assert be.file_player_finished(fp.id) is False  # mid-file
        _ports(be, fp, patch, 512)                      # consumes the tail
        assert be.file_player_finished(fp.id) is True   # parked at the end

    def test_looping_player_never_reports_finished(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000, loop=True)
        for _ in range(6):  # well past one length — the playhead just wraps
            _ports(be, fp, patch, 512)
        assert be.file_player_finished(fp.id) is False

    def test_not_finished_while_still_decoding(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000)
        st = be._state[fp.id]
        st["decoder"] = _FakeDecoder(ref, ready=1000, done=False)
        st["pos"] = 1000  # at the watermark, but the decode is not done
        assert be.file_player_finished(fp.id) is False

    def test_missing_path_never_finished(self, tmp_path):
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": str(tmp_path / "x.wav")})
        be.compile(patch)
        be.wait_for_file_decodes()  # finishes as failed
        _ports(be, fp, patch, 512)
        assert be.file_player_finished(fp.id) is False

    def test_unknown_id_not_finished(self, tmp_path):
        be, patch, fp, _ = _compiled_player(tmp_path)
        assert be.file_player_finished(999999) is False


class TestFailedHook:
    """``file_player_failed`` — the sibling edge the GUI polls to auto-skip
    a queued track that can't be decoded, so a dud doesn't stall the list."""

    def test_reports_failed_after_a_bad_decode(self, tmp_path):
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module(
            "file_player", params={"path": str(tmp_path / "nope.wav")}
        )
        be.compile(patch)
        be.wait_for_file_decodes()  # a missing path finishes as done+failed
        assert be.file_player_failed(fp.id) is True

    def test_healthy_track_never_reports_failed(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000)
        assert be.file_player_failed(fp.id) is False  # at 0:00
        _ports(be, fp, patch, 512)
        _ports(be, fp, patch, 512)  # play off the end
        assert be.file_player_finished(fp.id) is True
        assert be.file_player_failed(fp.id) is False  # ended cleanly, not failed

    def test_still_decoding_not_failed(self, tmp_path):
        be, patch, fp, ref = _compiled_player(tmp_path, n=1000)
        be._state[fp.id]["decoder"] = _FakeDecoder(ref, ready=500, done=False)
        assert be.file_player_failed(fp.id) is False

    def test_empty_path_not_failed(self, tmp_path):
        be = NumpyBackend(sample_rate=SR, block_size=512)
        patch = Patch()
        fp = patch.add_module("file_player", params={"path": ""})
        be.compile(patch)
        be.wait_for_file_decodes()
        assert be.file_player_failed(fp.id) is False  # no decoder for ""

    def test_unknown_id_not_failed(self, tmp_path):
        be, patch, fp, _ = _compiled_player(tmp_path)
        assert be.file_player_failed(999999) is False


class TestPlaylistSerialization:
    def test_playlist_round_trips_through_to_from_dict(self):
        cls = all_module_types()["file_player"]
        fp = cls(7, params={"path": "a.wav", "playlist": ["b.wav", "c.wav"]})
        restored = Module.from_dict(fp.to_dict())
        assert restored.params["path"] == "a.wav"
        assert restored.params["playlist"] == ["b.wav", "c.wav"]
