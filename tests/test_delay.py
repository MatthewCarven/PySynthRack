"""Tests for the Delay (analog-voiced feedback echo).

Coverage:
  - Model: registration, defaults, ports/signal kinds (audio in, time_cv
    in, audio out), JSON round-trip, unknown-param rejection, type walls.
  - DSP: disconnected -> silence; mix=0 is a bit-exact passthrough; a
    single tap lands exactly `time` samples late; feedback gives decaying
    repeats; the feedback is clamped so runaway settings stay bounded; the
    tone knob damps the repeats (dark < bright high-frequency tail);
    block-spanning echoes are continuous across blocks.
  - Paths: the vectorized fast path (delay >= block) and the per-sample
    path (delay < block) agree bit-for-bit.
  - Voice: a single-voice row is bit-identical to mono; voices echo
    independently via per-voice time_cv.
  - time_cv: positive CV lengthens the delay; cv_depth scales it.
  - Integration: osc -> delay -> speaker renders audible, finite audio.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.delay import Delay

SR = 44100
F = 512


def _rig(params=None, with_cv=False, block=F):
    """oscillator -> delay (optionally constant -> time_cv), compiled."""
    patch = Patch()
    src = patch.add_module("oscillator")
    dl = patch.add_module("delay", params=params or {})
    patch.connect(src.id, "out", dl.id, "in")
    cvsrc = None
    if with_cv:
        cvsrc = patch.add_module("constant")
        patch.connect(cvsrc.id, "out", dl.id, "time_cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    return patch, src, dl, cvsrc, b


def _run(b, patch, src, dl, signal, cvsrc=None, cv=None, block=F):
    """Render ``signal`` (1D or (V,F)) through the delay, block by block."""
    n = (signal.shape[-1] // block) * block
    outs = []
    for k in range(n // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(src.id, "out"): signal[..., sl].astype(np.float32)}
        if cvsrc is not None and cv is not None:
            bufs[(cvsrc.id, "out")] = cv[..., sl].astype(np.float32)
        outs.append(b._render_delay(dl, block, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _impulse(n, at=0):
    x = np.zeros(n, dtype=np.float32)
    x[at] = 1.0
    return x


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        dl = Patch().add_module("delay")
        assert isinstance(dl, Delay)
        assert dl.params == {
            "time": 300.0,
            "feedback": 0.4,
            "tone": 0.5,
            "mix": 0.35,
            "cv_depth": 50.0,
        }

    def test_ports_and_signal_kinds(self):
        dl = Patch().add_module("delay")
        assert [(p.name, p.signal_kind) for p in dl.input_ports] == [
            ("in", "audio"),
            ("time_cv", "cv"),
        ]
        assert [(p.name, p.signal_kind) for p in dl.output_ports] == [
            ("out", "audio"),
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("delay", params={"time": 125.0, "feedback": 0.7})
        restored = Patch.from_dict(patch.to_dict())
        dl = next(m for m in restored if m.TYPE == "delay")
        assert dl.params["time"] == 125.0
        assert dl.params["feedback"] == 0.7

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("delay", params={"wet": 0.5})

    def test_audio_into_in_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        dl = patch.add_module("delay")
        patch.connect(osc.id, "out", dl.id, "in")

    def test_cv_into_time_cv_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        dl = patch.add_module("delay")
        patch.connect(lfo.id, "cv", dl.id, "time_cv")

    def test_cv_into_audio_in_rejected(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        dl = patch.add_module("delay")
        with pytest.raises(ValueError):
            patch.connect(lfo.id, "cv", dl.id, "in")

    def test_audio_into_time_cv_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        dl = patch.add_module("delay")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", dl.id, "time_cv")

    def test_audio_out_into_cv_sink_rejected(self):
        patch = Patch()
        dl = patch.add_module("delay")
        vca = patch.add_module("vca")
        with pytest.raises(ValueError):
            patch.connect(dl.id, "out", vca.id, "cv")


# ----- Mono DSP --------------------------------------------------------------


class TestMonoDSP:
    def test_disconnected_is_silent(self):
        patch = Patch()
        dl = patch.add_module("delay")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        out = b._render_delay(dl, F, {}, patch)
        assert out.shape == (F,)
        assert not np.any(out)

    def test_frames_zero_empty(self):
        patch, src, dl, _, b = _rig()
        out = b._render_delay_core(dl, 0, np.zeros((1, 0), np.float32), None)
        assert out.shape == (1, 0)

    def test_mix_zero_is_exact_passthrough(self):
        # Both paths: long delay (fast) and short delay (per-sample).
        for block, time_ms in ((128, 50.0), (700, 2.0)):
            patch, src, dl, _, b = _rig(
                {"mix": 0.0, "feedback": 0.8, "time": time_ms}, block=block
            )
            x = np.random.randn(block * 2).astype(np.float32)
            out = _run(b, patch, src, dl, x, block=block)
            assert np.array_equal(out, x[: len(out)])

    def test_single_tap_timing(self):
        # feedback 0, mix 1 -> one clean echo exactly `time` samples late.
        T = 256  # < block -> per-sample path
        time_ms = T / SR * 1000.0
        patch, src, dl, _, b = _rig(
            {"feedback": 0.0, "mix": 1.0, "tone": 1.0, "time": time_ms}
        )
        out = _run(b, patch, src, dl, _impulse(F))
        assert int(np.argmax(np.abs(out))) == T
        assert out[T] == pytest.approx(1.0, abs=1e-3)
        assert np.max(np.abs(out[:T])) < 1e-6  # no pre-echo

    def test_tap_timing_spans_blocks(self):
        # Delay longer than a block -> echo appears in a later block.
        T = 700  # > block(512) -> fast path, lands in block 2
        time_ms = T / SR * 1000.0
        patch, src, dl, _, b = _rig(
            {"feedback": 0.0, "mix": 1.0, "tone": 1.0, "time": time_ms}
        )
        out = _run(b, patch, src, dl, _impulse(F * 3))
        assert int(np.argmax(np.abs(out))) == T
        assert out[T] == pytest.approx(1.0, abs=1e-3)

    def test_feedback_decaying_repeats(self):
        T = 300
        time_ms = T / SR * 1000.0
        fb = 0.5
        patch, src, dl, _, b = _rig(
            {"feedback": fb, "mix": 1.0, "tone": 1.0, "time": time_ms}
        )
        out = _run(b, patch, src, dl, _impulse(F * 4))
        taps = [out[k * T] for k in range(1, 4)]
        # Strictly decreasing, each no larger than the feedback fraction of
        # the previous (damping only ever removes energy).
        assert taps[0] > taps[1] > taps[2] > 0
        for prev, cur in zip(taps, taps[1:]):
            assert 0.0 < cur <= prev * fb + 1e-6

    def test_runaway_feedback_is_bounded(self):
        # Absurd feedback must be clamped -> output stays finite/bounded.
        patch, src, dl, _, b = _rig(
            {"feedback": 9.0, "mix": 0.5, "time": 5.0}
        )
        x = np.random.randn(SR // 4).astype(np.float32)
        out = _run(b, patch, src, dl, x, block=256)
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) < 50.0

    def test_tone_damps_repeats(self):
        # An alternating (high-frequency) signal: the dark tone should bleed
        # far less high-frequency energy into the tail than the bright one.
        T = 200
        time_ms = T / SR * 1000.0
        nyq = np.tile([1.0, -1.0], F * 2).astype(np.float32)
        common = {"feedback": 0.7, "mix": 1.0, "time": time_ms}
        p1, s1, d1, _, b1 = _rig({**common, "tone": 0.95})
        p2, s2, d2, _, b2 = _rig({**common, "tone": 0.05})
        bright = _run(b1, p1, s1, d1, nyq.copy())
        dark = _run(b2, p2, s2, d2, nyq.copy())
        tail = slice(3 * T, None)
        assert np.sum(dark[tail] ** 2) < 0.6 * np.sum(bright[tail] ** 2)


# ----- Path equivalence ------------------------------------------------------


class TestPaths:
    def test_fast_and_per_sample_paths_agree(self):
        # Same delay, same input: render once in a single big block (delay <
        # block -> per-sample) and once in small blocks (delay >= block ->
        # vectorized). The two must match bit-for-bit.
        x = np.random.randn(1800).astype(np.float32)
        T = 300
        time_ms = T / SR * 1000.0
        params = {"time": time_ms, "feedback": 0.6, "tone": 0.4, "mix": 0.5}
        pa, sa, da, _, ba = _rig(params, block=1800)   # per-sample
        per_sample = _run(ba, pa, sa, da, x, block=1800)
        pb, sb, db, _, bb = _rig(params, block=100)    # fast path
        fast = _run(bb, pb, sb, db, x, block=100)
        assert np.array_equal(per_sample, fast)


# ----- Voice DSP -------------------------------------------------------------


class TestVoiceDSP:
    def test_single_voice_row_matches_mono(self):
        patch, src, dl, _, b = _rig({"feedback": 0.6, "time": 4.0})
        x = np.random.randn(F).astype(np.float32)
        mono = b._render_delay_core(dl, F, x[np.newaxis, :], None)
        # fresh state for the voice render
        b2 = NumpyBackend(sample_rate=SR, block_size=F)
        b2.compile(patch)
        voice = b2._render_delay_core(
            dl, F, np.stack([x, x]).astype(np.float32), None
        )
        assert np.array_equal(voice[0], mono[0])
        assert np.array_equal(voice[0], voice[1])

    def test_voices_echo_independently_via_cv(self):
        # Two voices, different per-voice time_cv -> echoes at different
        # places. Impulse in each voice, feedback 0, mix 1.
        V = 2
        n = F * 3
        x = np.zeros((V, n), dtype=np.float32)
        x[:, 0] = 1.0
        cv = np.zeros((V, n), dtype=np.float32)
        cv[0] = 0.0
        cv[1] = 1.0  # voice 1 gets +cv_depth ms of extra delay
        patch = Patch()
        src = patch.add_module("oscillator")
        cvs = patch.add_module("constant")
        dl = patch.add_module(
            "delay",
            params={"feedback": 0.0, "mix": 1.0, "tone": 1.0,
                    "time": 200 / SR * 1000.0, "cv_depth": 300 / SR * 1000.0},
        )
        patch.connect(src.id, "out", dl.id, "in")
        patch.connect(cvs.id, "out", dl.id, "time_cv")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        outs = []
        for k in range(n // F):
            sl = slice(k * F, (k + 1) * F)
            bufs = {(src.id, "out"): x[:, sl], (cvs.id, "out"): cv[:, sl]}
            outs.append(b._render_delay(dl, F, bufs, patch))
        out = np.concatenate(outs, axis=-1)
        assert out.ndim == 2 and out.shape[0] == V
        peak0 = int(np.argmax(np.abs(out[0])))
        peak1 = int(np.argmax(np.abs(out[1])))
        assert peak0 == 200
        assert peak1 == 500  # 200 base + 300 from cv


# ----- time_cv ---------------------------------------------------------------


class TestTimeCV:
    def test_positive_cv_lengthens_delay(self):
        n = F * 3
        base_T = 250
        patch, src, dl, cvs, b = _rig(
            {"feedback": 0.0, "mix": 1.0, "tone": 1.0,
             "time": base_T / SR * 1000.0, "cv_depth": 200 / SR * 1000.0},
            with_cv=True,
        )
        imp = _impulse(n)
        cv = np.ones(n, dtype=np.float32)  # +1 unit -> +200 samples
        out = _run(b, patch, src, dl, imp, cvsrc=cvs, cv=cv)
        assert int(np.argmax(np.abs(out))) == base_T + 200


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_delay_speaker_makes_sound(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        dl = patch.add_module("delay", params={"mix": 0.5, "feedback": 0.4})
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", dl.id, "in")
        patch.connect(dl.id, "out", spk.id, "in")
        b = NumpyBackend(sample_rate=SR, block_size=F)
        b.compile(patch)
        peak = 0.0
        for _ in range(40):  # clear the delay's priming latency
            block = b.render_block(F)
            assert block is not None and np.all(np.isfinite(block))
            peak = max(peak, float(np.abs(block).max()))
        assert peak > 0.0
