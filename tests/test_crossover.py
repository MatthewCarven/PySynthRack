"""Tests for the Linkwitz-Riley crossover."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.crossover import Crossover


SR = 44100


def _build_patch_with_test_tone(freq: float, amp: float = 0.5):
    """Helper: oscillator → crossover, return (patch, osc, xo)."""
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": "sine", "freq": freq, "amp": amp},
    )
    xo = patch.add_module("crossover", params={"freq": 1000.0})
    patch.connect(osc.id, "out", xo.id, "in")
    return patch, osc, xo


def _capture_xo_outputs(patch, xo, backend, frames):
    """Drive the topo and pluck out the crossover's low/high buffers."""
    bufs = {}
    for mid in backend._topo_order:
        mod = patch.modules[mid]
        res = backend._render_module(mod, frames, bufs, patch)
        if isinstance(res, dict):
            for pn, b in res.items():
                bufs[(mid, pn)] = b
        elif res is not None and mod.OUTPUT_PORTS:
            bufs[(mid, mod.OUTPUT_PORTS[0].name)] = res
    return bufs[(xo.id, "low")], bufs[(xo.id, "high")]


class TestCrossoverModel:
    def test_register_and_defaults(self):
        patch = Patch()
        xo = patch.add_module("crossover")
        assert isinstance(xo, Crossover)
        assert xo.params == {"freq": 1000.0, "cv_depth": 1.0}
        assert [p.name for p in xo.input_ports] == ["in", "freq_cv"]
        assert xo.input_ports[0].signal_kind == "audio"
        assert xo.input_ports[1].signal_kind == "cv"
        assert [p.name for p in xo.output_ports] == ["low", "high"]
        assert all(p.signal_kind == "audio" for p in xo.output_ports)

    def test_unpatched_input_yields_silence_on_both(self):
        patch = Patch()
        xo = patch.add_module("crossover")
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        out = backend._render_crossover(xo, frames=64, buffers={}, patch=patch)
        assert isinstance(out, dict)
        assert np.all(out["low"] == 0.0)
        assert np.all(out["high"] == 0.0)


class TestCrossoverBehavior:
    def test_lf_tone_lands_mostly_in_low_branch(self):
        """A 100 Hz sine well below the 1 kHz corner should come out the
        ``low`` port at roughly full amplitude and the ``high`` port near
        silence."""
        patch, _, xo = _build_patch_with_test_tone(freq=100.0, amp=0.5)
        backend = NumpyBackend(sample_rate=SR, block_size=4096)
        backend.compile(patch)
        # Render two blocks: first warms up the biquads, second is steady-state.
        _ = _capture_xo_outputs(patch, xo, backend, frames=4096)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=4096)
        rms_low = float(np.sqrt(np.mean(low ** 2)))
        rms_high = float(np.sqrt(np.mean(high ** 2)))
        # Source RMS ≈ 0.5 / sqrt(2) ≈ 0.354. Low branch should be close.
        assert rms_low > 0.25, f"LF RMS in low branch too small: {rms_low}"
        # High branch should be very small at 3.3 octaves below corner
        # (LR4 = -24 dB/oct → about -80 dB).
        assert rms_high < 0.02, f"LF leaked into high branch: {rms_high}"

    def test_hf_tone_lands_mostly_in_high_branch(self):
        patch, _, xo = _build_patch_with_test_tone(freq=8000.0, amp=0.5)
        backend = NumpyBackend(sample_rate=SR, block_size=4096)
        backend.compile(patch)
        _ = _capture_xo_outputs(patch, xo, backend, frames=4096)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=4096)
        rms_low = float(np.sqrt(np.mean(low ** 2)))
        rms_high = float(np.sqrt(np.mean(high ** 2)))
        assert rms_high > 0.25, f"HF RMS in high branch too small: {rms_high}"
        assert rms_low < 0.02, f"HF leaked into low branch: {rms_low}"

    def test_at_corner_both_branches_are_minus_six_db(self):
        """At the LR4 corner each branch should be -6 dB (half amplitude)
        relative to the source. Tolerate ±25% to absorb the finite
        block / numerical noise."""
        patch, _, xo = _build_patch_with_test_tone(freq=1000.0, amp=0.5)
        backend = NumpyBackend(sample_rate=SR, block_size=8192)
        backend.compile(patch)
        _ = _capture_xo_outputs(patch, xo, backend, frames=8192)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=8192)
        rms_low = float(np.sqrt(np.mean(low ** 2)))
        rms_high = float(np.sqrt(np.mean(high ** 2)))
        source_rms = 0.5 / (2.0 ** 0.5)  # ≈ 0.354
        target = source_rms * 0.5  # -6 dB
        assert 0.75 * target < rms_low < 1.25 * target, rms_low
        assert 0.75 * target < rms_high < 1.25 * target, rms_high

    def test_low_plus_high_summed_back_through_combiner(self):
        """An LR4 with low+high routed into a Combiner reconstructs a
        signal whose RMS is within ~10% of the original — the LR4 phase
        relationship is built precisely for this clean recombination."""
        sr = SR
        patch = Patch()
        osc = patch.add_module(
            "oscillator", params={"waveform": "sine", "freq": 1500.0, "amp": 0.5},
        )
        xo = patch.add_module("crossover", params={"freq": 1000.0})
        comb = patch.add_module("combiner")
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(osc.id, "out", xo.id, "in")
        patch.connect(xo.id, "low", comb.id, "in1")
        patch.connect(xo.id, "high", comb.id, "in2")
        patch.connect(comb.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=8192)
        backend.compile(patch)
        _ = backend.render_block(8192)  # warm
        block = backend.render_block(8192)
        rms = float(np.sqrt(np.mean(block[:, 0] ** 2)))
        source_rms = 0.5 / (2.0 ** 0.5)
        # Allow generous slack — LR4 sums to all-pass magnitude in theory,
        # but block-rate measurement and the clip in the speaker stage
        # introduce small deviations.
        assert 0.85 * source_rms < rms < 1.15 * source_rms, rms

    def test_extreme_frequency_clamps_safely(self):
        patch, _, xo = _build_patch_with_test_tone(freq=440.0)
        # Crank to absurd values; renderer should clamp without NaN/inf.
        xo.set_param("freq", 1e9)
        backend = NumpyBackend(sample_rate=SR, block_size=512)
        backend.compile(patch)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=512)
        assert np.all(np.isfinite(low))
        assert np.all(np.isfinite(high))
        xo.set_param("freq", 0.0001)
        backend.compile(patch)
        low, high = _capture_xo_outputs(patch, xo, backend, frames=512)
        assert np.all(np.isfinite(low))
        assert np.all(np.isfinite(high))


# --------------------------------------------------------------------------
# freq_cv: CV-swept split point (added 2026-07-02)
# --------------------------------------------------------------------------

def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def _build_swept(tone: float, corner: float, cv_value=None,
                 cv_depth: float = 1.0, amp: float = 0.5):
    """osc(tone) → crossover(corner, cv_depth); optional constant → freq_cv.

    Returns (patch, xo). When ``cv_value`` is None the freq_cv jack is
    left unpatched (the static-corner path).
    """
    patch = Patch()
    osc = patch.add_module(
        "oscillator", params={"waveform": "sine", "freq": tone, "amp": amp},
    )
    xo = patch.add_module(
        "crossover", params={"freq": corner, "cv_depth": cv_depth},
    )
    patch.connect(osc.id, "out", xo.id, "in")
    if cv_value is not None:
        c = patch.add_module("constant", params={"value": float(cv_value)})
        patch.connect(c.id, "out", xo.id, "freq_cv")
    return patch, xo


def _run(patch, xo, frames=4096, warm=True):
    """Compile, optionally warm one block, return the steady low/high."""
    backend = NumpyBackend(sample_rate=SR, block_size=frames)
    backend.compile(patch)
    if warm:
        _capture_xo_outputs(patch, xo, backend, frames)
    return _capture_xo_outputs(patch, xo, backend, frames)


class TestCrossoverFreqCVWiring:
    def test_zero_cv_is_a_noop(self):
        """A constant 0.0 into freq_cv leaves the corner exactly at the
        static ``freq`` — bit-identical to an unpatched crossover."""
        base_low, base_high = _run(*_build_swept(1500.0, 1000.0), warm=False)
        cv_low, cv_high = _run(
            *_build_swept(1500.0, 1000.0, cv_value=0.0), warm=False
        )
        assert np.allclose(base_low, cv_low, atol=1e-6)
        assert np.allclose(base_high, cv_high, atol=1e-6)

    def test_unpatched_matches_plain_param(self):
        """No freq_cv connection at all → identical to the historical
        param-only crossover (regression guard for the static path)."""
        low_a, high_a = _run(*_build_swept(800.0, 1000.0), warm=False)
        # A second, independent build must be deterministic + identical.
        low_b, high_b = _run(*_build_swept(800.0, 1000.0), warm=False)
        assert np.array_equal(low_a, low_b)
        assert np.array_equal(high_a, high_b)


class TestCrossoverFreqCVMath:
    def test_positive_unit_cv_doubles_corner(self):
        """freq=1000, cv_depth=1.0, CV=+1.0 → 1000·2^(1·1) = 2000 Hz.
        Must equal a static crossover pinned at 2000 Hz."""
        swept_low, swept_high = _run(
            *_build_swept(1500.0, 1000.0, cv_value=1.0, cv_depth=1.0),
            warm=False,
        )
        static_low, static_high = _run(
            *_build_swept(1500.0, 2000.0), warm=False
        )
        assert np.allclose(swept_low, static_low, atol=1e-6)
        assert np.allclose(swept_high, static_high, atol=1e-6)

    def test_negative_unit_cv_halves_corner(self):
        """CV=-1.0 at unit depth → 1000·2^(-1) = 500 Hz == static 500."""
        swept_low, swept_high = _run(
            *_build_swept(700.0, 1000.0, cv_value=-1.0, cv_depth=1.0),
            warm=False,
        )
        static_low, static_high = _run(
            *_build_swept(700.0, 500.0), warm=False
        )
        assert np.allclose(swept_low, static_low, atol=1e-6)
        assert np.allclose(swept_high, static_high, atol=1e-6)

    def test_cv_depth_scales_the_sweep(self):
        """cv_depth multiplies the exponent: depth=2, CV=+0.5 → 2^(1) =
        2000 Hz; depth=0 disables the CV entirely (stays 1000 Hz)."""
        deep_low, deep_high = _run(
            *_build_swept(1500.0, 1000.0, cv_value=0.5, cv_depth=2.0),
            warm=False,
        )
        at2k_low, at2k_high = _run(*_build_swept(1500.0, 2000.0), warm=False)
        assert np.allclose(deep_low, at2k_low, atol=1e-6)
        assert np.allclose(deep_high, at2k_high, atol=1e-6)

        zero_depth_low, zero_depth_high = _run(
            *_build_swept(1500.0, 1000.0, cv_value=1.0, cv_depth=0.0),
            warm=False,
        )
        at1k_low, at1k_high = _run(*_build_swept(1500.0, 1000.0), warm=False)
        assert np.allclose(zero_depth_low, at1k_low, atol=1e-6)
        assert np.allclose(zero_depth_high, at1k_high, atol=1e-6)


class TestCrossoverFreqCVBehavior:
    def test_positive_cv_moves_tone_into_low_band(self):
        """A 1500 Hz tone sits ABOVE a 1000 Hz corner (high branch wins).
        Sweep the corner up to 2000 Hz with +1.0 CV and the tone is now
        BELOW it — the low branch takes over."""
        low0, high0 = _run(*_build_swept(1500.0, 1000.0))
        assert _rms(high0) > _rms(low0)  # baseline: high dominates

        low1, high1 = _run(
            *_build_swept(1500.0, 1000.0, cv_value=1.0, cv_depth=1.0)
        )
        assert _rms(low1) > _rms(high1)  # swept up: low dominates now

    def test_negative_cv_moves_tone_into_high_band(self):
        """A 700 Hz tone sits BELOW a 1000 Hz corner (low wins). Sweep the
        corner down to 500 Hz with -1.0 CV and the tone is now above it."""
        low0, high0 = _run(*_build_swept(700.0, 1000.0))
        assert _rms(low0) > _rms(high0)  # baseline: low dominates

        low1, high1 = _run(
            *_build_swept(700.0, 1000.0, cv_value=-1.0, cv_depth=1.0)
        )
        assert _rms(high1) > _rms(low1)  # swept down: high dominates now

    def test_low_plus_high_still_recombine_flat_under_cv(self):
        """LR4 flat-sum property must survive the swept corner: at the
        CV-shifted frequency the two branches still reconstruct the input
        magnitude (RMS of low+high ≈ RMS of the source)."""
        patch, xo = _build_swept(1200.0, 1000.0, cv_value=1.0, cv_depth=1.0)
        low, high = _run(patch, xo)
        recombined = low + high
        # Source: 0.5-amp sine → RMS ≈ 0.3536.
        assert abs(_rms(recombined) - 0.3536) < 0.02


class TestCrossoverFreqCVVoice:
    def test_voice_row_matches_mono_under_cv(self):
        """The house invariant: a (V, F) input split under a shared
        block-mean freq_cv must give, on each active voice row, exactly
        the mono result for that row's signal at the same corner."""
        from pysynthrack.core.patch import Cable
        frames = 512
        c = 1.0  # → corner doubles to 2000 Hz at unit depth
        t = np.arange(frames) / SR
        tone = (np.sin(2 * np.pi * 1500.0 * t) * 0.5).astype(np.float32)
        cv = np.full(frames, c, dtype=np.float32)

        AUDIO_SRC, CV_SRC = 9401, 9402

        def render(audio):
            patch = Patch()
            xo = patch.add_module(
                "crossover", params={"freq": 1000.0, "cv_depth": 1.0}
            )
            patch.cables.append(Cable(
                src_module_id=AUDIO_SRC, src_port="out",
                dst_module_id=xo.id, dst_port="in",
            ))
            patch.cables.append(Cable(
                src_module_id=CV_SRC, src_port="out",
                dst_module_id=xo.id, dst_port="freq_cv",
            ))
            backend = NumpyBackend(sample_rate=SR, block_size=frames)
            backend.compile(patch)
            buffers = {(AUDIO_SRC, "out"): audio, (CV_SRC, "out"): cv}
            return backend._render_crossover(xo, frames, buffers, patch)

        mono = render(tone)                        # (F,) low/high
        voice_audio = np.zeros((16, frames), dtype=np.float32)
        voice_audio[5] = tone
        voice = render(voice_audio)                # (V, F) low/high

        assert voice["low"].shape == (16, frames)
        assert np.allclose(voice["low"][5], mono["low"], atol=1e-6)
        assert np.allclose(voice["high"][5], mono["high"], atol=1e-6)
        # Every other slot stayed silent.
        for i in range(16):
            if i == 5:
                continue
            assert float(np.max(np.abs(voice["low"][i]))) == 0.0
            assert float(np.max(np.abs(voice["high"][i]))) == 0.0


def _reference_crossover_mono(backend, blocks, freqs):
    """The pre-slice-5 per-sample cascade, kept verbatim as the oracle.

    This is the exact mono implementation ``_render_crossover_mono``
    used before filter vectorization slice 5 replaced it with per-stage
    ``scipy.signal.lfilter`` calls: scalar Python recurrence through the
    four biquad stages (LP1, LP2, HP1, HP2), float64 math, float32
    outputs, raw per-stage (x1, x2, y1, y2) history carried across
    blocks and coefficients recomputed per block.
    """
    lp1_x1 = lp1_x2 = lp1_y1 = lp1_y2 = 0.0
    lp2_x1 = lp2_x2 = lp2_y1 = lp2_y2 = 0.0
    hp1_x1 = hp1_x2 = hp1_y1 = hp1_y2 = 0.0
    hp2_x1 = hp2_x2 = hp2_y1 = hp2_y2 = 0.0
    lows, highs = [], []
    for src, freq in zip(blocks, freqs):
        lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n = (
            backend._crossover_coeffs(freq)
        )
        frames = len(src)
        low = np.empty(frames, dtype=np.float32)
        high = np.empty(frames, dtype=np.float32)
        for n in range(frames):
            x = float(src[n])
            # LP stage 1
            y = lp_b0 * x + lp_b1 * lp1_x1 + lp_b2 * lp1_x2 - a1n * lp1_y1 - a2n * lp1_y2
            lp1_x2 = lp1_x1; lp1_x1 = x
            lp1_y2 = lp1_y1; lp1_y1 = y
            # LP stage 2
            z = lp_b0 * y + lp_b1 * lp2_x1 + lp_b2 * lp2_x2 - a1n * lp2_y1 - a2n * lp2_y2
            lp2_x2 = lp2_x1; lp2_x1 = y
            lp2_y2 = lp2_y1; lp2_y1 = z
            low[n] = z
            # HP stage 1
            u = hp_b0 * x + hp_b1 * hp1_x1 + hp_b2 * hp1_x2 - a1n * hp1_y1 - a2n * hp1_y2
            hp1_x2 = hp1_x1; hp1_x1 = x
            hp1_y2 = hp1_y1; hp1_y1 = u
            # HP stage 2
            v = hp_b0 * u + hp_b1 * hp2_x1 + hp_b2 * hp2_x2 - a1n * hp2_y1 - a2n * hp2_y2
            hp2_x2 = hp2_x1; hp2_x1 = u
            hp2_y2 = hp2_y1; hp2_y1 = v
            high[n] = v
        lows.append(low)
        highs.append(high)
    return np.concatenate(lows), np.concatenate(highs)


def _reference_crossover_voice(backend, blocks, freqs):
    """The pre-slice-5 per-sample voice cascade, verbatim as the oracle.

    (V,) numpy arrays for the per-stage history, scalar coefficients
    broadcast across voices, serial in time -- exactly the loop
    ``_render_crossover_voice`` ran before slice 5.
    """
    V = blocks[0].shape[0]
    st = {}
    for k in (
        "lp1_x1", "lp1_x2", "lp1_y1", "lp1_y2",
        "lp2_x1", "lp2_x2", "lp2_y1", "lp2_y2",
        "hp1_x1", "hp1_x2", "hp1_y1", "hp1_y2",
        "hp2_x1", "hp2_x2", "hp2_y1", "hp2_y2",
    ):
        st[k] = np.zeros(V, dtype=np.float64)
    lows, highs = [], []
    for src, freq in zip(blocks, freqs):
        lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n = (
            backend._crossover_coeffs(freq)
        )
        frames = src.shape[1]
        low = np.empty((V, frames), dtype=np.float32)
        high = np.empty((V, frames), dtype=np.float32)
        lp1_x1 = st["lp1_x1"]; lp1_x2 = st["lp1_x2"]
        lp1_y1 = st["lp1_y1"]; lp1_y2 = st["lp1_y2"]
        lp2_x1 = st["lp2_x1"]; lp2_x2 = st["lp2_x2"]
        lp2_y1 = st["lp2_y1"]; lp2_y2 = st["lp2_y2"]
        hp1_x1 = st["hp1_x1"]; hp1_x2 = st["hp1_x2"]
        hp1_y1 = st["hp1_y1"]; hp1_y2 = st["hp1_y2"]
        hp2_x1 = st["hp2_x1"]; hp2_x2 = st["hp2_x2"]
        hp2_y1 = st["hp2_y1"]; hp2_y2 = st["hp2_y2"]
        for n in range(frames):
            x = src[:, n].astype(np.float64)
            y = lp_b0 * x + lp_b1 * lp1_x1 + lp_b2 * lp1_x2 - a1n * lp1_y1 - a2n * lp1_y2
            lp1_x2 = lp1_x1; lp1_x1 = x
            lp1_y2 = lp1_y1; lp1_y1 = y
            z = lp_b0 * y + lp_b1 * lp2_x1 + lp_b2 * lp2_x2 - a1n * lp2_y1 - a2n * lp2_y2
            lp2_x2 = lp2_x1; lp2_x1 = y
            lp2_y2 = lp2_y1; lp2_y1 = z
            low[:, n] = z
            u = hp_b0 * x + hp_b1 * hp1_x1 + hp_b2 * hp1_x2 - a1n * hp1_y1 - a2n * hp1_y2
            hp1_x2 = hp1_x1; hp1_x1 = x
            hp1_y2 = hp1_y1; hp1_y1 = u
            v = hp_b0 * u + hp_b1 * hp2_x1 + hp_b2 * hp2_x2 - a1n * hp2_y1 - a2n * hp2_y2
            hp2_x2 = hp2_x1; hp2_x1 = u
            hp2_y2 = hp2_y1; hp2_y1 = v
            high[:, n] = v
        st["lp1_x1"] = lp1_x1; st["lp1_x2"] = lp1_x2
        st["lp1_y1"] = lp1_y1; st["lp1_y2"] = lp1_y2
        st["lp2_x1"] = lp2_x1; st["lp2_x2"] = lp2_x2
        st["lp2_y1"] = lp2_y1; st["lp2_y2"] = lp2_y2
        st["hp1_x1"] = hp1_x1; st["hp1_x2"] = hp1_x2
        st["hp1_y1"] = hp1_y1; st["hp1_y2"] = hp1_y2
        st["hp2_x1"] = hp2_x1; st["hp2_x2"] = hp2_x2
        st["hp2_y1"] = hp2_y1; st["hp2_y2"] = hp2_y2
        lows.append(low)
        highs.append(high)
    return np.concatenate(lows, axis=-1), np.concatenate(highs, axis=-1)


def _fresh_xo(frames=512):
    patch = Patch()
    xo = patch.add_module("crossover")
    backend = NumpyBackend(sample_rate=SR, block_size=frames)
    return backend, xo


def _run_new_mono(backend, xo, blocks, freqs):
    lows, highs = [], []
    for b, f in zip(blocks, freqs):
        out = backend._render_crossover_mono(xo, len(b), b, f)
        lows.append(out["low"]); highs.append(out["high"])
    return np.concatenate(lows), np.concatenate(highs)


def _run_new_voice(backend, xo, blocks, freqs):
    lows, highs = [], []
    for b, f in zip(blocks, freqs):
        out = backend._render_crossover_voice(xo, b.shape[1], b, f)
        lows.append(out["low"]); highs.append(out["high"])
    return np.concatenate(lows, axis=-1), np.concatenate(highs, axis=-1)


_SWEEP = (500.0, 800.0, 1000.0, 2500.0, 150.0, 8000.0, 20.0, 19845.0)


class TestCrossoverLfilterEquivalence:
    """Slice 5: the per-stage-lfilter paths must match the old cascade.

    Same contract as the filter slices: raw coefficient-independent
    DF-I history carried across blocks, so per-block freq(-cv) changes
    reproduce the old loop. On noise the match is bit-identical after
    the float32 cast; on pure sines the high branch drifts by <= ~5e-13
    absolute, confined to samples below ~-130 dBFS (float64
    reassociation between DF-I and transposed DF-II -- the documented
    ADSR-rewrite drift class). We assert < 1e-6 rather than == so a
    future scipy that reorders float ops doesn't break the suite
    spuriously, plus one test pinning the drift's confinement.
    """

    def test_multiblock_equivalence_static_freq(self):
        backend, xo = _fresh_xo()
        rng = np.random.default_rng(42)
        blocks = [
            (rng.standard_normal(512) * 0.5).astype(np.float32) for _ in range(8)
        ]
        freqs = [1000.0] * 8
        gl, gh = _run_new_mono(backend, xo, blocks, freqs)
        rl, rh = _reference_crossover_mono(backend, blocks, freqs)
        assert gl.dtype == np.float32 and gh.dtype == np.float32
        assert np.max(np.abs(gl.astype(np.float64) - rl.astype(np.float64))) < 1e-6
        assert np.max(np.abs(gh.astype(np.float64) - rh.astype(np.float64))) < 1e-6

    def test_equivalence_with_per_block_freq_sweep(self):
        """Coefficients change between blocks (the freq_cv cadence);
        raw-history carry must reproduce the old loop -- this is the
        case a persisted zf would get wrong."""
        backend, xo = _fresh_xo()
        rng = np.random.default_rng(7)
        blocks = [
            (rng.standard_normal(512) * 0.5).astype(np.float32) for _ in range(8)
        ]
        gl, gh = _run_new_mono(backend, xo, blocks, _SWEEP)
        rl, rh = _reference_crossover_mono(backend, blocks, _SWEEP)
        assert np.max(np.abs(gl.astype(np.float64) - rl.astype(np.float64))) < 1e-6
        assert np.max(np.abs(gh.astype(np.float64) - rh.astype(np.float64))) < 1e-6

    def test_single_sample_blocks(self):
        """frames=1 exercises the history-tail edge case (x2/y2 must
        come from the carried state, not the one-sample buffer)."""
        backend, xo = _fresh_xo()
        rng = np.random.default_rng(3)
        ones = [rng.standard_normal(1).astype(np.float32) for _ in range(64)]
        freqs = [1000.0 + 100.0 * i for i in range(64)]
        gl, gh = _run_new_mono(backend, xo, ones, freqs)
        rl, rh = _reference_crossover_mono(backend, ones, freqs)
        assert np.max(np.abs(gl.astype(np.float64) - rl.astype(np.float64))) < 1e-6
        assert np.max(np.abs(gh.astype(np.float64) - rh.astype(np.float64))) < 1e-6

    def test_split_render_matches_whole_render(self):
        """Intrinsic continuity check, no oracle: two 512-sample blocks
        back to back must equal the same 1024 samples in one call."""
        rng = np.random.default_rng(11)
        big = (rng.standard_normal(1024) * 0.5).astype(np.float32)
        b1, x1 = _fresh_xo()
        sl, sh = _run_new_mono(b1, x1, [big[:512], big[512:]], [700.0, 700.0])
        b2, x2 = _fresh_xo()
        wl, wh = _run_new_mono(b2, x2, [big], [700.0])
        assert np.max(np.abs(sl.astype(np.float64) - wl.astype(np.float64))) < 1e-6
        assert np.max(np.abs(sh.astype(np.float64) - wh.astype(np.float64))) < 1e-6

    def test_sine_drift_confined_below_audibility(self):
        """The razor case the noise grids miss: a pure 110 Hz sine's
        high branch lands at ~1e-7 magnitudes where DF-I vs transposed-
        DF-II float64 reassociation can flip a float32 ulp. Pin that
        any differing sample sits below ~-100 dBFS in the oracle."""
        backend, xo = _fresh_xo()
        t = np.arange(512 * 8) / SR
        sine = (0.5 * np.sin(2 * np.pi * 110.0 * t)).astype(np.float32)
        blocks = [sine[i * 512:(i + 1) * 512] for i in range(8)]
        gl, gh = _run_new_mono(backend, xo, blocks, _SWEEP)
        rl, rh = _reference_crossover_mono(backend, blocks, _SWEEP)
        assert np.max(np.abs(gl.astype(np.float64) - rl.astype(np.float64))) < 1e-6
        assert np.max(np.abs(gh.astype(np.float64) - rh.astype(np.float64))) < 1e-6
        differing = gh != rh
        if np.any(differing):
            assert np.max(np.abs(rh[differing])) < 1e-5

    def test_voice_multiblock_mixed_content(self):
        """16 voices -- sines, noise, and silent rows -- through a
        per-block freq sweep; one lfilter call per stage must match
        the old (V,)-broadcast per-sample loop."""
        backend, xo = _fresh_xo()
        rng = np.random.default_rng(42)
        V, total = 16, 512 * 8
        t = np.arange(total) / SR
        vb = np.empty((V, total), dtype=np.float32)
        for v in range(V):
            if v % 3 == 0:
                vb[v] = (0.5 * np.sin(2 * np.pi * (110.0 * (v + 1)) * t)).astype(
                    np.float32
                )
            elif v % 3 == 1:
                vb[v] = (rng.standard_normal(total) * 0.3).astype(np.float32)
            else:
                vb[v] = 0.0
        blocks = [vb[:, i * 512:(i + 1) * 512] for i in range(8)]
        gl, gh = _run_new_voice(backend, xo, blocks, _SWEEP)
        rl, rh = _reference_crossover_voice(backend, blocks, _SWEEP)
        assert gl.shape == (V, total) and gh.shape == (V, total)
        assert np.max(np.abs(gl.astype(np.float64) - rl.astype(np.float64))) < 1e-6
        assert np.max(np.abs(gh.astype(np.float64) - rh.astype(np.float64))) < 1e-6
        # Silent rows must stay exactly silent (no cross-voice leakage).
        for v in range(2, V, 3):
            assert float(np.max(np.abs(gl[v]))) == 0.0
            assert float(np.max(np.abs(gh[v]))) == 0.0

    def test_voice_single_sample_blocks(self):
        """frames=1 in the voice shape -- exercises the array-tail
        shift and the carry aliasing (x2_arr rebound to old x1_arr)."""
        backend, xo = _fresh_xo()
        rng = np.random.default_rng(5)
        ones = [
            (rng.standard_normal((16, 1)) * 0.5).astype(np.float32)
            for _ in range(48)
        ]
        freqs = [500.0 * (1.0 + 0.05 * i) for i in range(48)]
        gl, gh = _run_new_voice(backend, xo, ones, freqs)
        rl, rh = _reference_crossover_voice(backend, ones, freqs)
        assert np.max(np.abs(gl.astype(np.float64) - rl.astype(np.float64))) < 1e-6
        assert np.max(np.abs(gh.astype(np.float64) - rh.astype(np.float64))) < 1e-6

    def test_mono_voice_reinit_shape_switch(self):
        """Mono -> voice -> mono renders on one module id must reinit
        state to the new shape each time (no stale-shape crash), same
        contract as before slice 5."""
        backend, xo = _fresh_xo(256)
        rng = np.random.default_rng(9)
        mono_blk = rng.standard_normal(256).astype(np.float32)
        voice_blk = rng.standard_normal((16, 256)).astype(np.float32)
        out_m = backend._render_crossover_mono(xo, 256, mono_blk, 1000.0)
        assert out_m["low"].shape == (256,)
        out_v = backend._render_crossover_voice(xo, 256, voice_blk, 1000.0)
        assert out_v["low"].shape == (16, 256)
        out_m2 = backend._render_crossover_mono(xo, 256, mono_blk, 1000.0)
        assert out_m2["low"].shape == (256,)
        # Post-reinit mono render matches a fresh module bit-for-bit.
        b2, x2 = _fresh_xo(256)
        fresh = b2._render_crossover_mono(x2, 256, mono_blk, 1000.0)
        assert np.array_equal(out_m2["low"], fresh["low"])
        assert np.array_equal(out_m2["high"], fresh["high"])

    def test_state_keys_unchanged(self):
        """Persisted-state key names survive slice 5 (mono scalars,
        voice ``*_arr`` float64 arrays) -- the same compatibility pin
        the audio_to_cv vectorization carries."""
        backend, xo = _fresh_xo(64)
        blk = np.ones(64, dtype=np.float32)
        backend._render_crossover_mono(xo, 64, blk, 1000.0)
        st = backend._state[xo.id]
        assert set(st) == {
            f"{stg}_{fld}"
            for stg in ("lp1", "lp2", "hp1", "hp2")
            for fld in ("x1", "x2", "y1", "y2")
        }
        assert all(isinstance(v, float) for v in st.values())
        vblk = np.ones((16, 64), dtype=np.float32)
        backend._render_crossover_voice(xo, 64, vblk, 1000.0)
        st = backend._state[xo.id]
        assert set(st) == {
            f"{stg}_{fld}_arr"
            for stg in ("lp1", "lp2", "hp1", "hp2")
            for fld in ("x1", "x2", "y1", "y2")
        }
        assert all(
            v.dtype == np.float64 and v.shape == (16,) for v in st.values()
        )
