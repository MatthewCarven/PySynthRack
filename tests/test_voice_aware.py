"""End-to-end tests for the voice-aware downstream modules.

Slice 3a: ADSR and VCA opt into the voice-aware buffer protocol.
A (V, frames) gate drives V independent ADSR state machines; the VCA
multiplies element-wise (or broadcasts mono CV across voices); the
speaker sums the voice axis at the very end. These tests prove the
chain works end-to-end and that mono fallback still holds.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch


# ---------------------------------------------------------------------------
# ADSR voice-aware path
# ---------------------------------------------------------------------------


class TestADSRVoiceAware:
    """Direct _render_adsr tests with a synthetic (V, frames) gate.

    Avoids going through MIDIInput so the gate shape is controlled
    precisely; MIDIInput-integrated cases are in TestPolyphonicChain.
    """

    def _make_backend_with_adsr(self, **adsr_params):
        patch = Patch()
        env = patch.add_module("adsr", params=adsr_params)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        return backend, env, patch

    def _render_with_gate(self, backend, env, patch, gate, frames=512):
        # Wire the gate buffer in by hand. Use a sentinel src module id
        # for the buffer key; the patch has no cable to it, so we have
        # to short-circuit the lookup. Simpler approach: add a fake
        # cable from a fake source. Easiest of all: just inject into
        # buffers and call _render_adsr directly, bypassing render_block.
        # The renderer reads the gate via _input_buffer which scans the
        # patch's cables; we add a real cable and put the gate buffer
        # under that source key.
        SRC_ID = 9999
        SRC_PORT = "gate_src"
        # Patch.connect needs both endpoints to exist on the patch;
        # easier to mutate the cable list directly for this test fixture.
        from pysynthrack.core.patch import Cable
        # Remove any prior fake cable left from a previous call.
        patch.cables[:] = [c for c in patch.cables if c.src_module_id != SRC_ID]
        patch.cables.append(
            Cable(
                src_module_id=SRC_ID, src_port=SRC_PORT,
                dst_module_id=env.id, dst_port="gate",
            )
        )
        buffers = {(SRC_ID, SRC_PORT): gate}
        return backend._render_adsr(env, frames, buffers, patch)

    def test_voice_aware_gate_produces_voice_aware_output(self):
        # 16-row gate: only slot 3 gating. Output shape (16, frames),
        # slot 3 rises, every other slot stays at 0.
        backend, env, patch = self._make_backend_with_adsr(
            attack=0.001, decay=0.01, sustain=0.8, release=0.05
        )
        gate = np.zeros((16, 512), dtype=np.float32)
        gate[3, :] = 1.0  # only slot 3 held
        # Run several blocks to let attack complete.
        for _ in range(8):
            out = self._render_with_gate(backend, env, patch, gate)
        assert out.shape == (16, 512)
        # Slot 3 should have risen to roughly the sustain level.
        assert float(out[3, -1]) == pytest.approx(0.8, abs=0.05)
        # Every other slot stayed at 0.
        for i in range(16):
            if i == 3:
                continue
            assert float(np.max(np.abs(out[i]))) == 0.0, (
                f"slot {i} should be silent: max={float(np.max(np.abs(out[i])))}"
            )

    def test_per_voice_state_independence(self):
        # Two voices gating, one released: the released voice's tail
        # should be falling while the held voices stay at sustain.
        backend, env, patch = self._make_backend_with_adsr(
            attack=0.001, decay=0.005, sustain=0.7, release=0.1
        )
        # Phase 1: both slots 0 and 5 gating, let them reach sustain.
        gate = np.zeros((16, 512), dtype=np.float32)
        gate[0, :] = 1.0
        gate[5, :] = 1.0
        for _ in range(8):
            self._render_with_gate(backend, env, patch, gate)
        # Phase 2: release slot 5, hold slot 0.
        gate2 = np.zeros((16, 512), dtype=np.float32)
        gate2[0, :] = 1.0
        # Slot 5 gate=0 now
        out = self._render_with_gate(backend, env, patch, gate2)
        # Slot 0 still around sustain.
        assert float(out[0, -1]) == pytest.approx(0.7, abs=0.1)
        # Slot 5 is in release — its tail value should be LOWER than
        # its starting value, and lower than slot 0.
        assert float(out[5, -1]) < float(out[0, -1])

    def test_mono_gate_still_produces_mono_output(self):
        # 1D gate -> existing scalar path, 1D output. Backward compat.
        backend, env, patch = self._make_backend_with_adsr(
            attack=0.001, decay=0.005, sustain=0.5, release=0.05
        )
        gate = np.ones(512, dtype=np.float32)
        for _ in range(8):
            out = self._render_with_gate(backend, env, patch, gate)
        assert out.shape == (512,)
        assert out.ndim == 1
        # Reached sustain.
        assert float(out[-1]) == pytest.approx(0.5, abs=0.05)


# ---------------------------------------------------------------------------
# VCA voice-aware path
# ---------------------------------------------------------------------------


class TestVCAVoiceAware:
    """VCA broadcasting tests. VCA is stateless so the migration is
    purely about preserving input shapes through the multiplier.
    """

    def _build_vca_patch(self):
        patch = Patch()
        vca = patch.add_module("vca")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        return backend, vca, patch

    def _render(self, backend, vca, patch, audio, cv, frames=512):
        from pysynthrack.core.patch import Cable
        AUDIO_SRC = 9001
        CV_SRC = 9002
        patch.cables[:] = [
            c for c in patch.cables
            if c.src_module_id not in (AUDIO_SRC, CV_SRC)
        ]
        patch.cables.append(Cable(
            src_module_id=AUDIO_SRC, src_port="out",
            dst_module_id=vca.id, dst_port="audio",
        ))
        if cv is not None:
            patch.cables.append(Cable(
                src_module_id=CV_SRC, src_port="out",
                dst_module_id=vca.id, dst_port="cv",
            ))
        buffers = {(AUDIO_SRC, "out"): audio}
        if cv is not None:
            buffers[(CV_SRC, "out")] = cv
        return backend._render_vca(vca, frames, buffers, patch)

    def test_voice_audio_times_voice_cv_is_voice_aware(self):
        backend, vca, patch = self._build_vca_patch()
        audio = np.ones((16, 512), dtype=np.float32) * 0.5
        cv = np.zeros((16, 512), dtype=np.float32)
        cv[2, :] = 1.0  # only slot 2 modulated
        out = self._render(backend, vca, patch, audio, cv)
        assert out.shape == (16, 512)
        assert float(np.max(np.abs(out[2]))) == pytest.approx(0.5)
        for i in range(16):
            if i == 2:
                continue
            assert float(np.max(np.abs(out[i]))) == 0.0

    def test_voice_audio_times_mono_cv_broadcasts(self):
        # Mono CV applies identically to every voice — channel-wide
        # modulation (e.g. aftertouch driving a global VCA).
        backend, vca, patch = self._build_vca_patch()
        audio = np.ones((16, 512), dtype=np.float32)
        cv = np.full(512, 0.25, dtype=np.float32)
        out = self._render(backend, vca, patch, audio, cv)
        assert out.shape == (16, 512)
        assert float(np.max(out)) == pytest.approx(0.25)
        assert float(np.min(out)) == pytest.approx(0.25)

    def test_mono_audio_times_mono_cv_stays_mono(self):
        # Backward compat: the all-mono path still returns 1D.
        backend, vca, patch = self._build_vca_patch()
        audio = np.ones(512, dtype=np.float32)
        cv = np.full(512, 0.5, dtype=np.float32)
        out = self._render(backend, vca, patch, audio, cv)
        assert out.shape == (512,)

    def test_voice_audio_no_cv_passthrough_preserves_shape(self):
        # No CV input -> VCA acts as a unity-gain gain stage. Voice-
        # aware audio in stays voice-aware out.
        backend, vca, patch = self._build_vca_patch()
        audio = np.ones((16, 512), dtype=np.float32) * 0.7
        out = self._render(backend, vca, patch, audio, None)
        assert out.shape == (16, 512)
        assert float(np.max(out)) == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# End-to-end: MIDIInput -> ADSR -> VCA -> Speaker (canonical poly voice)
# ---------------------------------------------------------------------------


class TestPolyphonicChain:
    """The canonical polyphonic synth voice — per-voice envelopes."""

    def _build_poly_chain(self, attack=0.001, sustain=0.7, release=0.5):
        """Patch: MIDIInput.out -> VCA.audio,
                  MIDIInput.gate -> ADSR.gate,
                  ADSR.cv -> VCA.cv,
                  VCA.out -> SpeakerOutput.in
        """
        patch = Patch()
        midi = patch.add_module(
            "midi_input",
            params={"volume": 0.5, "waveform": "sine"},
        )
        env = patch.add_module(
            "adsr",
            params={
                "attack": attack,
                "decay": 0.01,
                "sustain": sustain,
                "release": release,
            },
        )
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        patch.connect(midi.id, "out", vca.id, "audio")
        patch.connect(midi.id, "gate", env.id, "gate")
        patch.connect(env.id, "cv", vca.id, "cv")
        patch.connect(vca.id, "out", spk.id, "in")
        return patch, midi, env, vca, spk

    def _run(self, backend, blocks=15):
        last = None
        for _ in range(blocks):
            last = backend.render_block(512)
        return last

    def test_chain_renders_audio(self):
        # Smoke test: a note through the whole chain produces audio.
        patch, midi, env, vca, spk = self._build_poly_chain(
            attack=0.001, sustain=0.7, release=0.05
        )
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        midi.note_on(60, 1.0)
        out = self._run(backend, blocks=20)
        peak = float(np.max(np.abs(out)))
        assert peak > 0.2, f"poly chain peak too low: {peak:.3f}"

    def test_released_voice_decays_while_held_voice_sustains(self):
        # The headline polyphony test. Two notes, release one. With
        # per-voice envelopes the released voice's tail decays
        # independently while the held voice stays at sustain. With
        # a global envelope (pre-slice-3), releasing one note while
        # holding another would not change the envelope at all.
        patch, midi, env, vca, spk = self._build_poly_chain(
            attack=0.001, sustain=0.7, release=0.3,
        )
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)

        # Press two notes, let them reach sustain.
        midi.note_on(60, 1.0)
        midi.note_on(67, 1.0)
        self._run(backend, blocks=30)
        peak_two = float(np.max(np.abs(self._run(backend, blocks=4))))

        # Release one of them.
        midi.note_off(67)
        # Run enough blocks for the release to mostly complete (0.3s
        # at 44.1 kHz / 512 = ~26 blocks).
        self._run(backend, blocks=30)
        peak_one_after_release = float(np.max(np.abs(self._run(backend, blocks=4))))

        # If per-voice envelopes work, releasing one note while the
        # other is held should drop the peak roughly to single-voice
        # level. Single voice at sustain=0.7 with volume=0.5 gives
        # ~0.35; two voices summing (in phase or partially) gives
        # ~0.45-0.7. So after-release should be clearly less than
        # before-release.
        assert peak_one_after_release < peak_two * 0.9, (
            f"released voice did not decay: peak_two={peak_two:.3f}, "
            f"peak_after={peak_one_after_release:.3f}"
        )
        # And the held voice should still be audible.
        assert peak_one_after_release > 0.15, (
            f"held voice fell silent after partial release: "
            f"peak_after={peak_one_after_release:.3f}"
        )

    def test_no_notes_silent(self):
        patch, midi, env, vca, spk = self._build_poly_chain()
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        out = self._run(backend, blocks=5)
        assert float(np.max(np.abs(out))) < 1e-6
