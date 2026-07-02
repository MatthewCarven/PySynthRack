"""Tests for the LFO module."""
from __future__ import annotations

import numpy as np

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.lfo import LFO, LFO_WAVEFORMS


class TestLFOModel:
    def test_register_and_defaults(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        assert isinstance(lfo, LFO)
        assert lfo.params == {
            "waveform": "sine",
            "rate": 4.0,
            "depth": 1.0,
            "bipolar": False,
            "cv_depth": 1.0,
        }
        # v0.3 adds rate_cv input for modulation-matrix patches.
        assert [p.name for p in lfo.input_ports] == ["rate_cv"]
        assert lfo.input_ports[0].signal_kind == "cv"
        assert [p.name for p in lfo.output_ports] == ["cv"]
        assert lfo.output_ports[0].signal_kind == "cv"

    def test_waveforms_includes_random(self):
        for w in ("sine", "triangle", "square", "saw", "random"):
            assert w in LFO_WAVEFORMS

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "lfo",
            params={
                "waveform": "triangle",
                "rate": 0.5,
                "depth": 0.7,
                "bipolar": True,
            },
        )
        restored = Patch.from_dict(patch.to_dict())
        lfo = next(m for m in restored if m.TYPE == "lfo")
        assert lfo.params["waveform"] == "triangle"
        assert lfo.params["rate"] == 0.5
        assert lfo.params["depth"] == 0.7
        assert lfo.params["bipolar"] is True


class TestLFOBehavior:
    def _backend(self, sr=44100, block=512):
        return NumpyBackend(sample_rate=sr, block_size=block)

    def test_unipolar_output_stays_in_zero_to_depth(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 1.0, "bipolar": False},
        )
        backend = self._backend()
        backend.compile(patch)
        chunks = [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        out = np.concatenate(chunks)
        assert float(out.min()) >= -1e-5
        assert float(out.max()) <= 1.0 + 1e-5

    def test_bipolar_output_spans_negative_to_positive(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        chunks = [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        out = np.concatenate(chunks)
        assert float(out.min()) < -0.9
        assert float(out.max()) > 0.9

    def test_depth_scales_amplitude(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 0.3, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        chunks = [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        out = np.concatenate(chunks)
        assert float(np.max(np.abs(out))) <= 0.3 + 1e-5
        assert float(np.max(np.abs(out))) > 0.25

    def test_rate_matches_number_of_cycles(self):
        """A 2 Hz LFO over one second should complete ~2 cycles."""
        sr = 44100
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 2.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend(sr=sr, block=sr)
        backend.compile(patch)
        out = backend._render_lfo(lfo, frames=sr)
        zero_crossings = int(np.sum(np.diff(np.signbit(out)).astype(int)))
        assert 3 <= zero_crossings <= 5

    def test_phase_continuous_across_blocks(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 1.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend(block=512)
        backend.compile(patch)
        block1 = backend._render_lfo(lfo, frames=512)
        block2 = backend._render_lfo(lfo, frames=512)
        jump = abs(float(block2[0]) - float(block1[-1]))
        assert jump < 0.05

    def test_square_lfo_takes_two_values(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "square", "rate": 2.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        out = np.concatenate(
            [backend._render_lfo(lfo, frames=4096) for _ in range(4)]
        )
        uniques = np.unique(np.round(out, 4))
        assert set(uniques.tolist()) == {-1.0, 1.0}

    def test_random_waveform_is_finite_and_bounded(self):
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "random", "rate": 8.0, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        out = np.concatenate(
            [backend._render_lfo(lfo, frames=2048) for _ in range(4)]
        )
        assert np.all(np.isfinite(out))
        assert float(np.max(np.abs(out))) <= 1.0 + 1e-5

    def test_extreme_rate_clamps_safely(self):
        """A rate beyond Nyquist should clamp, not crash or NaN."""
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 1e9, "depth": 1.0, "bipolar": True},
        )
        backend = self._backend()
        backend.compile(patch)
        out = backend._render_lfo(lfo, frames=512)
        assert np.all(np.isfinite(out))


class TestLFOIntegration:
    def test_tremolo_through_vca_modulates_amplitude(self):
        """LFO -> VCA.cv at unipolar depth=1 with a held note should produce
        an audio envelope whose RMS varies over a cycle."""
        sr = 44100
        patch = Patch()
        kb = patch.add_module(
            "keyboard", params={"waveform": "sine", "volume": 1.0}
        )
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 4.0, "depth": 1.0, "bipolar": False},
        )
        vca = patch.add_module("vca", params={"gain": 1.0})
        spk = patch.add_module("speaker_output", params={"gain": 1.0})
        patch.connect(kb.id, "out", vca.id, "audio")
        patch.connect(lfo.id, "cv", vca.id, "cv")
        patch.connect(vca.id, "out", spk.id, "in")

        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)

        kb.note_on(60)
        _ = backend.render_block(sr)  # warm up past attack ramp
        block = backend.render_block(sr)

        left = block[:, 0].astype(np.float64)
        n_windows = 8
        win_len = len(left) // n_windows
        rms_vals = [
            float(np.sqrt(np.mean(left[i * win_len:(i + 1) * win_len] ** 2)))
            for i in range(n_windows)
        ]
        assert (max(rms_vals) - min(rms_vals)) > 0.05


class TestLFORateCV:
    """v0.3: LFO accepts a CV input on its rate (1V/octave, block-mean)."""

    def _walk_topo(self, backend, patch, frames=1024):
        """Render one block via the topo walk and return the buffer dict."""
        bufs = {}
        for mid in backend._topo_order:
            mod = patch.modules[mid]
            res = backend._render_module(mod, frames, bufs, patch)
            if isinstance(res, dict):
                for pn, b in res.items():
                    bufs[(mid, pn)] = b
            elif res is not None and mod.OUTPUT_PORTS:
                bufs[(mid, mod.OUTPUT_PORTS[0].name)] = res
        return bufs

    def _count_zero_crossings(self, wave: np.ndarray, threshold: float = 0.5) -> int:
        """Count crossings of `threshold` on the rising edge."""
        above = wave > threshold
        return int(np.sum(np.diff(above.astype(np.int8)) > 0))

    def test_no_cv_leaves_rate_alone(self):
        """No rate_cv patched → rate behaves exactly as before."""
        sr = 44100
        patch = Patch()
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 10.0, "depth": 1.0, "bipolar": True},
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        bufs = self._walk_topo(backend, patch, frames=sr)
        wave = bufs[(lfo.id, "cv")]
        # 10 Hz over 1 second = 10 cycles → 10 rising zero-crossings of 0.
        crossings = self._count_zero_crossings(wave, threshold=0.0)
        assert 9 <= crossings <= 11, crossings

    def test_positive_cv_doubles_rate(self):
        """rate_cv = +1.0 (constant) should double the LFO frequency."""
        sr = 44100
        patch = Patch()
        # Source LFO held at +1.0 with bipolar=True, depth=1, very slow rate
        # so the constant looks like DC across our measurement window.
        src = patch.add_module(
            "lfo",
            params={"waveform": "square", "rate": 0.001, "depth": 1.0, "bipolar": True},
        )
        # Target LFO whose rate we'll modulate.
        target = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 5.0, "depth": 1.0, "bipolar": True},
        )
        patch.connect(src.id, "cv", target.id, "rate_cv")
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        bufs = self._walk_topo(backend, patch, frames=sr)
        wave = bufs[(target.id, "cv")]
        # rate becomes 5 * 2^mean(cv). cv is ~+1 (held square, first half).
        # Expected rate ≈ 10 Hz → ~10 crossings over 1 sec.
        crossings = self._count_zero_crossings(wave, threshold=0.0)
        assert 8 <= crossings <= 12, f"expected ~10 crossings, got {crossings}"

    def test_negative_cv_halves_rate(self):
        sr = 44100
        patch = Patch()
        # Source LFO held at -1.0: bipolar=True square in its low half.
        # Phase=0 at compile, so we sit at +1 first. Use saw at 0.5 Hz
        # so over 1 sec the mean is ~0.0 — not what we want.
        # Easier: construct a constant CV via a unipolar=False square at
        # very low rate (0.001 Hz → ~0.5 cycle in 1 sec, so we get +1
        # throughout). To get -1, we'd need bipolar square inverted.
        #
        # Simplest path: use a saw at very low rate from phase=0 ramping
        # +1 → -1, and pick a window where saw is mostly negative. But
        # the cleanest test is a constant negative CV — we'll inject it
        # via the renderer directly using a stub buffer.
        target = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 20.0, "depth": 1.0, "bipolar": True},
        )
        backend = NumpyBackend(sample_rate=sr, block_size=sr)
        backend.compile(patch)
        # Inject a -1.0 constant CV into the renderer by faking a buffer.
        # The renderer reads via _input_buffer(patch, buffers, id, "rate_cv").
        # We need a cable in the patch so the lookup finds something, but
        # we can pre-populate the buffer slot ourselves.
        cv_buf = np.full(sr, -1.0, dtype=np.float32)
        # Stand up a fake source module to satisfy patch.connect, then
        # overwrite its buffer in our manual walk.
        fake = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 0.001, "depth": 1.0, "bipolar": False},
        )
        patch.connect(fake.id, "cv", target.id, "rate_cv")
        backend.compile(patch)
        # Manual topo walk, but override fake's output with cv_buf.
        bufs = {(fake.id, "cv"): cv_buf}
        order = [m for m in backend._topo_order if m != fake.id]
        for mid in order:
            mod = patch.modules[mid]
            res = backend._render_module(mod, sr, bufs, patch)
            if isinstance(res, dict):
                for pn, b in res.items():
                    bufs[(mid, pn)] = b
            elif res is not None and mod.OUTPUT_PORTS:
                bufs[(mid, mod.OUTPUT_PORTS[0].name)] = res
        wave = bufs[(target.id, "cv")]
        # rate becomes 20 * 2^-1 = 10 Hz → ~10 crossings.
        crossings = self._count_zero_crossings(wave, threshold=0.0)
        assert 8 <= crossings <= 12, f"expected ~10 crossings, got {crossings}"
