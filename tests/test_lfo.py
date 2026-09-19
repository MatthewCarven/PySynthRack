"""Tests for the LFO module."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type
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
            "phase": 0.0,
        }
        # v0.3 adds rate_cv input for modulation-matrix patches; the
        # 2026-09-19 love pass adds the reset gate.
        assert [p.name for p in lfo.input_ports] == ["rate_cv", "reset"]
        assert lfo.input_ports[0].signal_kind == "cv"
        assert lfo.input_ports[1].signal_kind == "gate"
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


# ----- reset gate + phase param (2026-09-19 love pass) -----------------------

SR = 44100


def _phase_val(waveform, phase, bipolar=True, depth=1.0):
    """The LFO's output at exactly ``phase`` -- what a reset sample reads."""
    if waveform == "sine":
        w = np.sin(2.0 * np.pi * phase)
    elif waveform == "saw":
        w = 2.0 * phase - 1.0
    elif waveform == "triangle":
        w = 1.0 - 4.0 * abs(phase - 0.5)
    else:
        w = 1.0 if phase < 0.5 else -1.0
    if not bipolar:
        w = (w + 1.0) * 0.5
    return np.float32(w * depth)


class _Rig:
    """An LFO with a (fake) reset source and optional (fake) rate_cv
    source, driven block by block with hand-made buffers -- the same
    stub-buffer trick TestLFORateCV uses, so every edge lands where the
    test says it does."""

    def __init__(self, block=512, voice=False, **params):
        self.patch = Patch()
        self.block = block
        self.clk = self.patch.add_module("clock")
        self.src = self.patch.add_module("lfo") if voice else None
        self.lfo = self.patch.add_module("lfo", params=params)
        self.patch.connect(self.clk.id, "out", self.lfo.id, "reset")
        if voice:
            self.patch.connect(self.src.id, "cv", self.lfo.id, "rate_cv")
        self.backend = NumpyBackend(sample_rate=SR, block_size=block)
        self.backend.compile(self.patch)

    def render(self, reset=None, rate_cv=None, frames=None):
        frames = self.block if frames is None else frames
        bufs = {}
        if reset is not None:
            bufs[(self.clk.id, "out")] = np.asarray(reset, dtype=np.float32)
        if rate_cv is not None:
            bufs[(self.src.id, "cv")] = np.asarray(rate_cv, dtype=np.float32)
        return self.backend._render_lfo(self.lfo, frames, bufs, self.patch)

    @property
    def state(self):
        return self.backend._state[self.lfo.id]


def _gate(frames, *edges, voices=None, row=None):
    """A gate buffer that goes high at each edge and low at the next."""
    g = np.zeros(frames if voices is None else (voices, frames), np.float32)
    for k, e in enumerate(edges):
        end = edges[k + 1] if k + 1 < len(edges) else frames
        lo = e + max(1, (end - e) // 2)
        if voices is None:
            g[e:lo] = 1.0
        else:
            g[row, e:lo] = 1.0
    return g


class TestLFOReset:
    def test_reset_lands_on_the_exact_sample(self):
        # Sine at phase 0.25 = the peak: out[edge] is exactly 1.0, the
        # sample before is not, and the sample after has advanced one
        # increment FROM 0.25 -- the ramp restarts, it doesn't just jump.
        rig = _Rig(waveform="sine", rate=4.0, bipolar=True, phase=0.25)
        rig.render(reset=np.zeros(512))          # free-run one block first
        out = rig.render(reset=_gate(512, 200))
        inc = 4.0 / SR
        assert out[200] == np.float32(1.0)
        assert out[199] != np.float32(1.0)
        assert out[201] == np.float32(np.sin(2 * np.pi * (0.25 + inc)))
        assert rig.state["phase"] == pytest.approx((0.25 + (512 - 200) * inc) % 1.0)
        # And the next block continues from there, not from the old ramp.
        nxt = rig.render(reset=np.zeros(512))
        assert nxt[0] == pytest.approx(np.sin(2 * np.pi * (0.25 + (512 - 200) * inc)), abs=1e-6)

    @pytest.mark.parametrize("waveform,phase", [
        ("sine", 0.25), ("saw", 0.5), ("triangle", 0.75), ("square", 0.5),
    ])
    def test_phase_param_moves_the_start(self, waveform, phase):
        rig = _Rig(waveform=waveform, rate=2.0, bipolar=True, phase=phase)
        out = rig.render()
        assert out[0] == _phase_val(waveform, phase)
        # ...and a reset jumps to the same place.
        out = rig.render(reset=_gate(512, 77))
        assert out[77] == _phase_val(waveform, phase)

    def test_phase_zero_is_the_old_start(self):
        rig = _Rig(waveform="sine", rate=2.0, bipolar=True)
        assert rig.render()[0] == np.float32(0.0)

    def test_phase_wraps(self):
        # 1.25 cycles is 0.25 cycles; a hand-edited patch can't park
        # the ramp outside a cycle.
        rig = _Rig(waveform="sine", rate=2.0, bipolar=True, phase=1.25)
        assert rig.render()[0] == np.float32(1.0)

    def test_unpatched_reset_and_phase_zero_are_bit_exact_with_no_reset(self):
        # The recipe as a claim: a reset cable that never rises, and an
        # explicit phase 0.0, render exactly what an LFO with neither does.
        p = Patch()
        plain = p.add_module("lfo", params={"waveform": "saw", "rate": 3.3, "bipolar": True})
        b = NumpyBackend(sample_rate=SR, block_size=512)
        b.compile(p)
        ref = np.concatenate([b._render_lfo(plain, 512, {}, p) for _ in range(20)])
        rig = _Rig(waveform="saw", rate=3.3, bipolar=True, phase=0.0)
        out = np.concatenate([rig.render(reset=np.zeros(512)) for _ in range(20)])
        assert np.array_equal(ref, out)
        # Same with the cable present but no buffer published (unpatched
        # in the topo sense).
        rig2 = _Rig(waveform="saw", rate=3.3, bipolar=True)
        out2 = np.concatenate([rig2.render() for _ in range(20)])
        assert np.array_equal(ref, out2)

    def test_mid_stream_reset_restarts_the_lfo_from_phase(self):
        # Blocks 0-2 free-run; block 3 resets at sample 137. Before the
        # edge the output is the free-running LFO to the bit; from the
        # edge on it is a FRESH LFO started at ``phase``.
        rig = _Rig(waveform="sine", rate=5.0, bipolar=True, phase=0.25)
        free = _Rig(waveform="sine", rate=5.0, bipolar=True, phase=0.25)
        fresh = _Rig(waveform="sine", rate=5.0, bipolar=True, phase=0.25)
        blocks, free_blocks = [], []
        for k in range(6):
            g = _gate(512, 137) if k == 3 else np.zeros(512)
            blocks.append(rig.render(reset=g))
            free_blocks.append(free.render())
        out = np.concatenate(blocks)
        ref_free = np.concatenate(free_blocks)
        ref_fresh = np.concatenate([fresh.render() for _ in range(3)])
        edge = 3 * 512 + 137
        assert np.array_equal(out[:edge], ref_free[:edge])
        assert out[edge] == np.float32(1.0)
        # The block boundaries fall differently for the fresh LFO, so
        # float64 phase accumulation differs in the last bits: allclose.
        assert np.allclose(out[edge:], ref_fresh[:len(out) - edge], atol=1e-6)
        assert not np.allclose(out[edge:edge + 512], ref_free[edge:edge + 512], atol=1e-2)

    def test_edge_held_across_a_block_boundary_counts_once(self):
        rig = _Rig(waveform="sine", rate=4.0, bipolar=True, phase=0.25)
        rig.render(reset=np.zeros(512))
        a = rig.render(reset=np.ones(512))    # edge at sample 0 of this block
        assert a[0] == np.float32(1.0)
        b = rig.render(reset=np.ones(512))    # still high: NOT a new edge
        inc = 4.0 / SR
        assert b[0] == pytest.approx(np.sin(2 * np.pi * (0.25 + 512 * inc)), abs=1e-6)
        assert b[0] != np.float32(1.0)

    def test_two_edges_in_one_block(self):
        rig = _Rig(waveform="saw", rate=3.0, bipolar=True, phase=0.5)
        out = rig.render(reset=_gate(512, 100, 300))
        assert out[100] == np.float32(0.0)
        assert out[300] == np.float32(0.0)
        inc = 3.0 / SR
        assert out[299] == pytest.approx(2 * ((0.5 + 199 * inc) % 1.0) - 1, abs=1e-6)

    def test_block_size_independent_with_a_reset_mid_stream(self):
        # 64 vs 512 over 4096 samples, reset at sample 1000 (mid-block for
        # both: 1000 % 64 == 40, 1000 % 512 == 488).
        outs = {}
        for block in (64, 512):
            rig = _Rig(block=block, waveform="sine", rate=6.0, bipolar=True, phase=0.25)
            gate = np.zeros(4096, np.float32)
            gate[1000:1400] = 1.0
            outs[block] = np.concatenate([
                rig.render(reset=gate[i:i + block]) for i in range(0, 4096, block)
            ])
        assert outs[64][1000] == np.float32(1.0)
        assert outs[512][1000] == np.float32(1.0)
        assert np.allclose(outs[64], outs[512], atol=1e-6)

    def test_moving_the_phase_knob_reanchors_a_free_running_lfo(self):
        rig = _Rig(waveform="sine", rate=4.0, bipolar=True, phase=0.0)
        rig.render()
        rig.render()
        rig.lfo.params["phase"] = 0.25
        out = rig.render()
        assert out[0] == np.float32(1.0)
        # Only once: the next block carries on from there.
        assert rig.render()[0] != np.float32(1.0)

    def test_random_waveform_rerolls_on_reset(self):
        # 1 Hz: no wrap inside a 512-sample block, so the only step is
        # the one the reset makes. Global np.random is what the waveform
        # draws from, so seed it (the unseeded-example lesson).
        np.random.seed(11)
        rig = _Rig(waveform="random", rate=1.0, bipolar=True)
        first = rig.render(reset=np.zeros(512))
        assert np.unique(first).size == 1
        out = rig.render(reset=_gate(512, 100))
        assert np.unique(out[:100]).size == 1 and out[0] == first[0]
        assert np.unique(out[100:]).size == 1
        assert out[100] != out[99]

    def test_random_first_roll_happens_at_any_start_phase(self):
        # Pre-love-pass the first roll only fired from phase 0.0 exactly;
        # a non-zero ``phase`` would have output 0.0 until the first wrap.
        np.random.seed(5)
        rig = _Rig(waveform="random", rate=1.0, bipolar=True, phase=0.3)
        out = rig.render()
        assert np.unique(out).size == 1 and out[0] != 0.0


class TestLFOResetVoiceAware:
    def _cv(self, voices=3, block=512):
        return np.zeros((voices, block), np.float32)

    def test_voice_row_resets_only_its_own_voice(self):
        rig = _Rig(voice=True, waveform="sine", rate=4.0, bipolar=True, phase=0.25)
        rig.render(rate_cv=self._cv(), reset=self._cv())
        ref = _Rig(voice=True, waveform="sine", rate=4.0, bipolar=True, phase=0.25)
        ref.render(rate_cv=self._cv(), reset=self._cv())
        out = rig.render(rate_cv=self._cv(), reset=_gate(512, 100, voices=3, row=1))
        base = ref.render(rate_cv=self._cv(), reset=self._cv())
        assert out.shape == (3, 512)
        assert out[1, 100] == np.float32(1.0)
        assert not np.array_equal(out[1], base[1])
        assert np.array_equal(out[0], base[0])
        assert np.array_equal(out[2], base[2])

    def test_mono_reset_resets_every_voice(self):
        rig = _Rig(voice=True, waveform="sine", rate=4.0, bipolar=True, phase=0.25)
        rig.render(rate_cv=self._cv(), reset=np.zeros(512))
        out = rig.render(rate_cv=self._cv(), reset=_gate(512, 50))
        assert np.all(out[:, 50] == np.float32(1.0))
        assert not np.any(out[:, 49] == np.float32(1.0))

    def test_single_voice_row_is_bit_identical_to_mono(self):
        gate = _gate(512, 333)
        mono = _Rig(waveform="triangle", rate=3.0, bipolar=True, phase=0.6)
        poly = _Rig(voice=True, waveform="triangle", rate=3.0, bipolar=True, phase=0.6)
        for g in (np.zeros(512), gate, np.zeros(512)):
            m = mono.render(reset=g)
            v = poly.render(rate_cv=self._cv(voices=1), reset=g)
            assert v.shape == (1, 512)
            assert np.array_equal(m, v[0])

    def test_voice_reset_on_the_mono_path_collapses_to_any_voice_high(self):
        # No rate_cv -> mono path; a (V, F) reset with an edge in row 2
        # only still resets the (single) LFO: the house sum.
        rig = _Rig(waveform="sine", rate=4.0, bipolar=True, phase=0.25)
        rig.render(reset=np.zeros(512))
        out = rig.render(reset=_gate(512, 120, voices=4, row=2))
        assert out.ndim == 1
        assert out[120] == np.float32(1.0)

    def test_voice_random_rerolls_per_row(self):
        np.random.seed(21)
        rig = _Rig(voice=True, waveform="random", rate=1.0, bipolar=True)
        first = rig.render(rate_cv=self._cv(), reset=self._cv())
        out = rig.render(rate_cv=self._cv(), reset=_gate(512, 100, voices=3, row=1))
        assert np.unique(out[1, 100:]).size == 1 and out[1, 100] != out[1, 99]
        assert np.array_equal(out[0], np.full(512, first[0, 0], np.float32))
        assert np.array_equal(out[2], np.full(512, first[2, 0], np.float32))


# ----- UI ---------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("lfo")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("format"))
    return out


def test_every_param_gets_a_bounded_widget(monkeypatch):
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("lfo").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["phase"][0] == "add_slider_float"
    assert w["phase"][1].endswith(" cyc")
    assert "oct/unit" in w["cv_depth"][1]
    assert w["rate"][1].endswith(" Hz")


# ----- example ------------------------------------------------------------------


def test_the_retrigger_example_opens_every_note_at_the_tremolo_peak():
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "lfo_retrigger.json"
    patch = load_patch(path)
    lfo = next(m for m in patch if m.TYPE == "lfo")
    assert lfo.params["phase"] == 0.25
    b = NumpyBackend(sample_rate=44100, block_size=512)
    b.compile(patch)
    lfo_cap, gate_cap = [], []
    orig_l, orig_s = b._render_lfo, b._render_sequencer

    def spy_l(module, frames, buffers=None, p=None):
        r = orig_l(module, frames, buffers, p)
        lfo_cap.append(np.asarray(r).copy())
        return r

    def spy_s(module, frames, buffers, p):
        r = orig_s(module, frames, buffers, p)
        gate_cap.append(np.asarray(r["gate"]).copy())
        return r

    b._render_lfo, b._render_sequencer = spy_l, spy_s
    peak = 0.0
    for _ in range(int(44100 * 6 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8, peak
    trem = np.concatenate(lfo_cap)
    gate = np.concatenate(gate_cap) > 0.5
    edges = np.flatnonzero(gate[1:] & ~gate[:-1]) + 1
    # 90 BPM, one gate per beat with a rest on step 6: 7 notes in 6 s
    # after the first (which is at sample 0).
    assert len(edges) >= 6
    # Unipolar sine at phase 0.25 reads exactly 1.0 on every note-on;
    # 7 Hz over a 667 ms beat is 4.67 cycles, so free-running it would
    # sit at ~0.25 there (and it does, one sample earlier).
    assert np.all(trem[edges] == np.float32(1.0))
    assert np.all(trem[edges - 1] < 0.6)
