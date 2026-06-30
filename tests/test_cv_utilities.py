"""Tests for the CV-utility trio: Constant / CVScale / CVOffset.

Coverage:
  - Model: registration, defaults, ports/signal kinds, JSON round-trip,
    unknown param rejected, type walls (cv↔cv legal; audio into a cv
    input illegal; a cv output into an audio sink illegal).
  - Constant: emits the scalar ``value`` every sample, mono shape,
    custom/negative values, ignores any stray buffers (no inputs).
  - CVScale: ``out = in * scale`` (attenuate / amplify / invert),
    unpatched → silence, shape-polymorphic (mono stays mono, (V, F)
    stays (V, F) and is scaled per row).
  - CVOffset: ``out = in + offset``, unpatched → constant ``offset``
    (DC-source behaviour), transparent at 0.0, shape-polymorphic with
    the scalar broadcasting across the voice axis.
  - Integration: LFO (±1 bipolar) → CVScale(0.5) → CVOffset(0.5)
    lands in 0..1 centred on 0.5 and drives an oscillator's amp_cv;
    Constant → CVToFrequency turns a dialed number into a fixed pitch.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.patch import Cable
from pysynthrack.modules.constant import Constant
from pysynthrack.modules.cvoffset import CVOffset
from pysynthrack.modules.cvscale import CVScale


def _backend():
    return NumpyBackend(sample_rate=44100, block_size=512)


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        assert isinstance(patch.add_module("constant"), Constant)
        assert isinstance(patch.add_module("cv_scale"), CVScale)
        assert isinstance(patch.add_module("cv_offset"), CVOffset)
        assert patch.add_module("constant").params == {"value": 1.0}
        assert patch.add_module("cv_scale").params == {"scale": 1.0}
        assert patch.add_module("cv_offset").params == {"offset": 0.0}

    def test_ports_and_signal_kinds(self):
        patch = Patch()
        const = patch.add_module("constant")
        scale = patch.add_module("cv_scale")
        offset = patch.add_module("cv_offset")
        # Constant: no inputs, one cv output.
        assert const.input_ports == []
        assert [(p.name, p.signal_kind) for p in const.output_ports] == [("out", "cv")]
        # Scale / Offset: one cv input, one cv output.
        for m in (scale, offset):
            assert [(p.name, p.signal_kind) for p in m.input_ports] == [("in", "cv")]
            assert [(p.name, p.signal_kind) for p in m.output_ports] == [("out", "cv")]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("constant", params={"value": -2.5})
        patch.add_module("cv_scale", params={"scale": 0.25})
        patch.add_module("cv_offset", params={"offset": 0.5})
        restored = Patch.from_dict(patch.to_dict())
        by_type = {m.TYPE: m for m in restored}
        assert by_type["constant"].params["value"] == -2.5
        assert by_type["cv_scale"].params["scale"] == 0.25
        assert by_type["cv_offset"].params["offset"] == 0.5

    def test_unknown_param_rejected(self):
        patch = Patch()
        with pytest.raises(KeyError):
            patch.add_module("constant", params={"level": 1.0})
        with pytest.raises(KeyError):
            patch.add_module("cv_scale", params={"gain": 1.0})
        with pytest.raises(KeyError):
            patch.add_module("cv_offset", params={"bias": 1.0})

    def test_cv_chain_connections_accepted(self):
        patch = Patch()
        lfo = patch.add_module("lfo")
        scale = patch.add_module("cv_scale")
        offset = patch.add_module("cv_offset")
        const = patch.add_module("constant")
        osc = patch.add_module("oscillator")
        patch.connect(lfo.id, "cv", scale.id, "in")        # cv → cv
        patch.connect(scale.id, "out", offset.id, "in")    # cv → cv
        patch.connect(offset.id, "out", osc.id, "amp_cv")  # cv → cv
        patch.connect(const.id, "out", osc.id, "freq_cv")  # cv → cv

    def test_audio_into_cv_input_rejected(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        scale = patch.add_module("cv_scale")
        with pytest.raises(ValueError):
            patch.connect(osc.id, "out", scale.id, "in")  # audio → cv

    def test_cv_output_into_audio_sink_rejected(self):
        patch = Patch()
        const = patch.add_module("constant")
        spk = patch.add_module("speaker_output")
        with pytest.raises(ValueError):
            patch.connect(const.id, "out", spk.id, "in")  # cv → audio


# ----- Constant --------------------------------------------------------------


class TestConstant:
    def test_emits_default_value(self):
        patch = Patch()
        const = patch.add_module("constant")
        backend = _backend(); backend.compile(patch)
        out = backend._render_constant(const, 256, {}, patch)
        assert out.shape == (256,)
        assert out.dtype == np.float32
        assert np.all(out == 1.0)

    def test_custom_and_negative_values(self):
        patch = Patch()
        backend = _backend()
        for v in (0.0, 0.5, 2.0, -1.0, -3.25):
            const = patch.add_module("constant", params={"value": v})
            backend.compile(patch)
            out = backend._render_constant(const, 64, {}, patch)
            assert np.all(out == np.float32(v))

    def test_ignores_stray_buffers(self):
        # Constant has no inputs; nothing in the buffer store affects it.
        patch = Patch()
        const = patch.add_module("constant", params={"value": 0.7})
        backend = _backend(); backend.compile(patch)
        out = backend._render_constant(const, 32, {(999, "out"): np.ones(32)}, patch)
        assert np.allclose(out, 0.7)


# ----- CVScale ---------------------------------------------------------------


class TestCVScale:
    def _render(self, params, cv):
        patch = Patch()
        scale = patch.add_module("cv_scale", params=params)
        backend = _backend(); backend.compile(patch)
        patch.cables.append(Cable(77, "out", scale.id, "in"))
        cv = np.asarray(cv, dtype=np.float32)
        return backend._render_cv_scale(scale, cv.shape[-1], {(77, "out"): cv}, patch)

    def test_attenuate(self):
        out = self._render({"scale": 0.5}, np.full(100, 1.0))
        assert np.allclose(out, 0.5)
        assert out.dtype == np.float32

    def test_amplify(self):
        out = self._render({"scale": 2.0}, np.full(64, 0.4))
        assert np.allclose(out, 0.8)

    def test_invert(self):
        cv = np.linspace(-1.0, 1.0, 50).astype(np.float32)
        out = self._render({"scale": -1.0}, cv)
        assert np.allclose(out, -cv)

    def test_zero_scale_is_silence(self):
        out = self._render({"scale": 0.0}, np.full(40, 0.9))
        assert np.allclose(out, 0.0)

    def test_unpatched_input_is_silence(self):
        patch = Patch()
        scale = patch.add_module("cv_scale", params={"scale": 5.0})
        backend = _backend(); backend.compile(patch)
        out = backend._render_cv_scale(scale, 128, {}, patch)
        assert out.shape == (128,)
        assert np.all(out == 0.0)

    def test_voice_aware_shape_preserved_and_scaled(self):
        cv = np.stack([np.full(80, 0.2), np.full(80, -0.6)]).astype(np.float32)
        out = self._render({"scale": 0.5}, cv)
        assert out.shape == (2, 80)
        assert np.allclose(out[0], 0.1)
        assert np.allclose(out[1], -0.3)

    def test_mono_stays_mono(self):
        out = self._render({"scale": 1.0}, np.full(16, 0.3))
        assert out.ndim == 1


# ----- CVOffset --------------------------------------------------------------


class TestCVOffset:
    def _render(self, params, cv):
        patch = Patch()
        offset = patch.add_module("cv_offset", params=params)
        backend = _backend(); backend.compile(patch)
        patch.cables.append(Cable(77, "out", offset.id, "in"))
        cv = np.asarray(cv, dtype=np.float32)
        return backend._render_cv_offset(offset, cv.shape[-1], {(77, "out"): cv}, patch)

    def test_adds_offset(self):
        out = self._render({"offset": 0.5}, np.full(100, 0.2))
        assert np.allclose(out, 0.7)
        assert out.dtype == np.float32

    def test_negative_offset(self):
        cv = np.linspace(0.0, 1.0, 50).astype(np.float32)
        out = self._render({"offset": -0.25}, cv)
        assert np.allclose(out, cv - 0.25)

    def test_transparent_at_zero(self):
        cv = np.sin(np.linspace(0, 6.28, 64)).astype(np.float32)
        out = self._render({"offset": 0.0}, cv)
        assert np.allclose(out, cv)

    def test_unpatched_input_is_constant_offset(self):
        patch = Patch()
        offset = patch.add_module("cv_offset", params={"offset": 0.42})
        backend = _backend(); backend.compile(patch)
        out = backend._render_cv_offset(offset, 256, {}, patch)
        assert out.shape == (256,)
        assert np.allclose(out, 0.42)

    def test_voice_aware_shape_preserved_and_offset_broadcast(self):
        cv = np.stack([np.full(80, 0.2), np.full(80, -0.6)]).astype(np.float32)
        out = self._render({"offset": 1.0}, cv)
        assert out.shape == (2, 80)
        assert np.allclose(out[0], 1.2)
        assert np.allclose(out[1], 0.4)

    def test_mono_stays_mono(self):
        out = self._render({"offset": 0.1}, np.full(16, 0.3))
        assert out.ndim == 1


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def _run(self, patch, backend, blocks, taps):
        """Render ``blocks`` blocks through the topo order, returning the
        concatenated buffer for each (module_id, port) in ``taps``."""
        out = {k: [] for k in taps}
        for _ in range(blocks):
            buffers = {}
            for mid in backend._topo_order:
                m = patch.modules.get(mid)
                if m is None:
                    continue
                result = backend._render_module(m, 512, buffers, patch)
                if result is None:
                    continue
                if isinstance(result, dict):
                    for port, buf in result.items():
                        buffers[(mid, port)] = buf
                elif m.OUTPUT_PORTS:
                    buffers[(mid, m.OUTPUT_PORTS[0].name)] = result
            for k in taps:
                out[k].append(buffers[k])
        return {k: np.concatenate(v) for k, v in out.items()}

    def test_scale_then_offset_recenters_lfo_to_unipolar(self):
        """±1 bipolar LFO → CVScale(0.5) → CVOffset(0.5) → 0..1, mean ~0.5."""
        patch = Patch()
        lfo = patch.add_module(
            "lfo", params={"waveform": "sine", "rate": 5.0, "depth": 1.0, "bipolar": True}
        )
        scale = patch.add_module("cv_scale", params={"scale": 0.5})
        offset = patch.add_module("cv_offset", params={"offset": 0.5})
        patch.connect(lfo.id, "cv", scale.id, "in")
        patch.connect(scale.id, "out", offset.id, "in")
        backend = _backend(); backend.compile(patch)

        taps = [(offset.id, "out")]
        res = self._run(patch, backend, blocks=(44100 // 512), taps=taps)
        shaped = res[(offset.id, "out")]
        assert np.isfinite(shaped).all()
        assert shaped.min() >= -1e-4         # stays unipolar
        assert shaped.max() <= 1.0 + 1e-4
        assert abs(float(shaped.mean()) - 0.5) < 0.05  # centred on the offset

    def test_scale_offset_chain_shapes_oscillator_amp(self):
        """The same chain into an oscillator's amp_cv produces audible,
        finite, bounded audio at the speaker — exercises dispatch +
        port-keyed buffer store end to end."""
        patch = Patch()
        lfo = patch.add_module(
            "lfo", params={"waveform": "sine", "rate": 6.0, "depth": 1.0, "bipolar": True}
        )
        scale = patch.add_module("cv_scale", params={"scale": 0.5})
        offset = patch.add_module("cv_offset", params={"offset": 0.5})
        osc = patch.add_module("oscillator", params={"waveform": "sine", "freq": 220.0})
        spk = patch.add_module("speaker_output", params={"gain": 0.8})
        patch.connect(lfo.id, "cv", scale.id, "in")
        patch.connect(scale.id, "out", offset.id, "in")
        patch.connect(offset.id, "out", osc.id, "amp_cv")
        patch.connect(osc.id, "out", spk.id, "in")
        backend = _backend(); backend.compile(patch)
        block = backend.render_block(512)
        assert block.shape == (512, 2)
        assert np.isfinite(block).all()
        assert np.abs(block).max() > 0.0     # not silent

    def test_constant_drives_cv_to_frequency_pitch(self):
        """Constant(0.5) → CVToFrequency: a dialed number becomes a fixed
        pitch. value=0.5 hits the module's ``fm`` anchor (mid)."""
        patch = Patch()
        const = patch.add_module("constant", params={"value": 0.5})
        c2f = patch.add_module(
            "cv_to_frequency",
            params={"f0": 110.0, "fm": 440.0, "f1": 1760.0, "mode": "log", "waveform": "sine"},
        )
        patch.connect(const.id, "out", c2f.id, "cv")
        backend = _backend(); backend.compile(patch)
        taps = [(c2f.id, "out")]
        res = self._run(patch, backend, blocks=4, taps=taps)
        wave = res[(c2f.id, "out")]
        assert np.isfinite(wave).all()
        assert np.abs(wave).max() > 0.1   # it sings
