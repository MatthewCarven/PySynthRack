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


# ---------------------------------------------------------------------------
# Filter voice-aware path (slice 3b)
# ---------------------------------------------------------------------------


class TestFilterVoiceAware:
    """Filter slice-3b tests.

    The Filter renderer branches on its audio input's ndim. 1D -> the
    pre-slice-3 scalar biquad. 2D -> V parallel biquads, each with its
    own (x1, x2, y1, y2) memory. cutoff_cv can be 1D (shared cutoff
    across voices) or 2D (per-voice cutoff via per-row block-mean).
    """

    def _build_filter_patch(self, **filter_params):
        patch = Patch()
        flt = patch.add_module("filter", params=filter_params)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        return backend, flt, patch

    def _render(self, backend, flt, patch, audio, cutoff_cv=None, frames=512):
        from pysynthrack.core.patch import Cable
        AUDIO_SRC = 9101
        CV_SRC = 9102
        patch.cables[:] = [
            c for c in patch.cables
            if c.src_module_id not in (AUDIO_SRC, CV_SRC)
        ]
        patch.cables.append(Cable(
            src_module_id=AUDIO_SRC, src_port="out",
            dst_module_id=flt.id, dst_port="in",
        ))
        if cutoff_cv is not None:
            patch.cables.append(Cable(
                src_module_id=CV_SRC, src_port="out",
                dst_module_id=flt.id, dst_port="cutoff_cv",
            ))
        buffers = {(AUDIO_SRC, "out"): audio}
        if cutoff_cv is not None:
            buffers[(CV_SRC, "out")] = cutoff_cv
        return backend._render_filter(flt, frames, buffers, patch)

    def test_voice_audio_returns_voice_shape(self):
        # (16, F) audio in -> (16, F) audio out. Per-voice biquads
        # produce their own outputs; an unused (zero) voice produces
        # zero out.
        backend, flt, patch = self._build_filter_patch(
            mode="lowpass", cutoff=2000.0, resonance=0.707
        )
        audio = np.zeros((16, 512), dtype=np.float32)
        # White-ish noise in slot 3 only.
        rng = np.random.default_rng(0)
        audio[3, :] = rng.standard_normal(512).astype(np.float32) * 0.2
        # Run a few blocks so biquad memory settles.
        for _ in range(3):
            out = self._render(backend, flt, patch, audio)
        assert out.shape == (16, 512)
        # Slot 3 carries signal.
        assert float(np.max(np.abs(out[3]))) > 1e-3
        # Every other slot stayed at 0.
        for i in range(16):
            if i == 3:
                continue
            assert float(np.max(np.abs(out[i]))) == 0.0, (
                f"slot {i} should be silent, got max={float(np.max(np.abs(out[i])))}"
            )

    def test_per_voice_filter_memory_is_independent(self):
        # Two voices with the same input but at different start times:
        # voice 0 has been ringing for several blocks (filter memory
        # populated), voice 5 has a fresh impulse. Voice 0's output
        # should reflect prior filter state (decayed amplitude or
        # carryover), while voice 5's should look like a fresh impulse
        # response. The key proof: setting state via slot 0 must not
        # influence slot 5 -- per-voice memory means slot 5 starts
        # from x1=x2=y1=y2=0.
        backend, flt, patch = self._build_filter_patch(
            mode="lowpass", cutoff=1000.0, resonance=2.0  # resonant
        )
        # Warm up slot 0 with sustained signal for several blocks.
        warmup = np.zeros((16, 512), dtype=np.float32)
        warmup[0, :] = 1.0
        for _ in range(5):
            self._render(backend, flt, patch, warmup)

        # Now drive slot 5 with an impulse; slot 0 input goes to 0.
        impulse = np.zeros((16, 512), dtype=np.float32)
        impulse[5, 0] = 1.0
        out = self._render(backend, flt, patch, impulse)
        # Slot 5's impulse response should ring on its own from a
        # fresh state: nonzero output, peaks in the first ~50 samples.
        assert float(np.max(np.abs(out[5, :100]))) > 0.01
        # Slot 0 has zero input now but biquad memory from the
        # warmup -- it will decay on its own, but is independent of
        # slot 5's impulse. Specifically, the slot-5 impulse should
        # not appear in slot 0 (memory independence).
        # Since slot 0's memory decays naturally toward zero, by
        # frame 500 it should be much smaller than slot 5's peak.
        # The strict claim: slot 5's nonzero samples don't appear in
        # slot 0 (no cross-talk). We approximate by checking slot 0
        # is decaying monotonically-ish, not getting "kicked" by
        # slot 5's impulse.
        slot0_first_half = float(np.mean(np.abs(out[0, :256])))
        slot0_second_half = float(np.mean(np.abs(out[0, 256:])))
        # Decaying signal: second half average smaller than first.
        # The mere absence of slot-5 cross-talk is what this asserts;
        # if cross-talk leaked in, slot 0 would show a spike near
        # frame 0 mirroring the impulse.
        assert slot0_second_half <= slot0_first_half + 1e-6

    def test_mono_audio_still_returns_mono(self):
        # Backward compat: 1D in -> 1D out, scalar biquad path.
        backend, flt, patch = self._build_filter_patch(
            mode="lowpass", cutoff=1000.0
        )
        audio = np.ones(512, dtype=np.float32) * 0.5
        for _ in range(3):
            out = self._render(backend, flt, patch, audio)
        assert out.shape == (512,)
        assert out.ndim == 1
        # Lowpass on DC -> output settles to DC level (~0.5).
        assert float(out[-1]) == pytest.approx(0.5, abs=0.05)

    def test_voice_audio_mono_cutoff_cv_broadcasts(self):
        # Macro filter sweep: one LFO modulates every voice's filter
        # equally. cutoff_cv is (F,), audio is (V, F). Output (V, F).
        backend, flt, patch = self._build_filter_patch(
            mode="lowpass", cutoff=1000.0
        )
        audio = np.zeros((16, 512), dtype=np.float32)
        audio[1, :] = 0.5
        audio[7, :] = 0.5
        # +1 octave on cutoff CV (1V/oct).
        cv = np.full(512, 1.0, dtype=np.float32)
        out = self._render(backend, flt, patch, audio, cutoff_cv=cv)
        assert out.shape == (16, 512)
        # Both signaled slots should produce nonzero output; silent
        # slots should still be silent (per-voice memory).
        assert float(np.max(np.abs(out[1]))) > 1e-3
        assert float(np.max(np.abs(out[7]))) > 1e-3
        assert float(np.max(np.abs(out[2]))) == 0.0

    def test_voice_audio_per_voice_cutoff_cv(self):
        # Per-voice cutoff: cutoff_cv is (V, F). Different voices get
        # different cutoff frequencies. Slot 0 with cutoff +2 octaves
        # (=4000 Hz) should pass a 2 kHz tone more freely than slot 5
        # at cutoff -2 octaves (=250 Hz), which heavily attenuates it.
        backend, flt, patch = self._build_filter_patch(
            mode="lowpass", cutoff=1000.0, resonance=0.707
        )
        sr = 44100
        t = np.arange(512) / sr
        tone = (np.sin(2 * np.pi * 2000 * t) * 0.5).astype(np.float32)
        audio = np.zeros((16, 512), dtype=np.float32)
        audio[0, :] = tone
        audio[5, :] = tone

        cv = np.zeros((16, 512), dtype=np.float32)
        cv[0, :] = 2.0    # slot 0 cutoff -> 4000 Hz
        cv[5, :] = -2.0   # slot 5 cutoff -> 250 Hz

        # Run several blocks so the biquad reaches steady-state.
        for _ in range(6):
            out = self._render(backend, flt, patch, audio, cutoff_cv=cv)

        slot0_rms = float(np.sqrt(np.mean(out[0] ** 2)))
        slot5_rms = float(np.sqrt(np.mean(out[5] ** 2)))
        # Per-voice cutoff means slot 0 passes the tone much louder
        # than slot 5. Ratio should be substantial (>4x).
        assert slot0_rms > slot5_rms * 4.0, (
            f"per-voice cutoff did not differentiate voices: "
            f"slot0_rms={slot0_rms:.4f}, slot5_rms={slot5_rms:.4f}"
        )


# ---------------------------------------------------------------------------
# Oscillator voice-aware path (slice 3b)
# ---------------------------------------------------------------------------


class TestOscillatorVoiceAware:
    """Oscillator slice-3b tests.

    The Oscillator renderer branches on freq_cv's ndim. 1D or None ->
    the existing mono path with a single phase accumulator. 2D ->
    V independent phase accumulators emit (V, F). amp_cv broadcasts
    in either branch (so a mono carrier can be amp-shaped per voice).
    """

    def _build_osc_patch(self, **osc_params):
        patch = Patch()
        osc = patch.add_module("oscillator", params=osc_params)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        return backend, osc, patch

    def _render(
        self, backend, osc, patch,
        freq_cv=None, amp_cv=None, frames=512,
    ):
        from pysynthrack.core.patch import Cable
        FREQ_SRC = 9201
        AMP_SRC = 9202
        patch.cables[:] = [
            c for c in patch.cables
            if c.src_module_id not in (FREQ_SRC, AMP_SRC)
        ]
        buffers = {}
        if freq_cv is not None:
            patch.cables.append(Cable(
                src_module_id=FREQ_SRC, src_port="out",
                dst_module_id=osc.id, dst_port="freq_cv",
            ))
            buffers[(FREQ_SRC, "out")] = freq_cv
        if amp_cv is not None:
            patch.cables.append(Cable(
                src_module_id=AMP_SRC, src_port="out",
                dst_module_id=osc.id, dst_port="amp_cv",
            ))
            buffers[(AMP_SRC, "out")] = amp_cv
        return backend._render_oscillator(osc, frames, buffers, patch)

    def test_voice_freq_cv_returns_voice_shape(self):
        # (V, F) freq_cv -> (V, F) output. Each voice runs its own
        # phase accumulator. With freq_cv = 0 in every slot, all
        # voices should produce identical waveforms (same base freq,
        # same starting phase, same cumsum increments).
        backend, osc, patch = self._build_osc_patch(
            waveform="sine", freq=440.0, amp=0.5
        )
        freq_cv = np.zeros((16, 512), dtype=np.float32)
        out = self._render(backend, osc, patch, freq_cv=freq_cv)
        assert out.shape == (16, 512)
        # Every voice must produce the same waveform (per-voice
        # independence with identical inputs == identical outputs).
        for v in range(1, 16):
            np.testing.assert_allclose(out[v], out[0], atol=1e-6)

    def test_per_voice_pitch_via_freq_cv(self):
        # Distinct cv per voice -> distinct pitches. Voice 0 at 0V
        # (=440 Hz, base freq); voice 5 at +1V (=880 Hz, one octave
        # up). Count zero crossings to verify the pitch difference.
        backend, osc, patch = self._build_osc_patch(
            waveform="sine", freq=440.0, amp=0.5
        )
        freq_cv = np.zeros((16, 512), dtype=np.float32)
        freq_cv[0, :] = 0.0   # 440 Hz
        freq_cv[5, :] = 1.0   # 880 Hz
        # Run a few blocks; pitch is set every block, but voice
        # phases evolve so we just need one block worth.
        out = self._render(backend, osc, patch, freq_cv=freq_cv)
        # Zero crossings ~ frequency * duration. For a 512-sample
        # block at 44.1 kHz that's ~11.6 ms.
        # 440 Hz -> ~5.1 cycles -> ~10 zero crossings.
        # 880 Hz -> ~10.2 cycles -> ~20 zero crossings.
        def zc(buf):
            return int(np.sum(np.diff(np.sign(buf)) != 0))
        zc_v0 = zc(out[0])
        zc_v5 = zc(out[5])
        assert zc_v5 > zc_v0 * 1.5, (
            f"per-voice freq_cv did not produce different pitches: "
            f"v0 zc={zc_v0}, v5 zc={zc_v5}"
        )

    def test_per_voice_phase_persists_across_blocks(self):
        # Phase must accumulate across blocks per voice. Render two
        # blocks and verify the second block continues smoothly from
        # the first (no phase reset between blocks).
        backend, osc, patch = self._build_osc_patch(
            waveform="sine", freq=100.0, amp=1.0
        )
        freq_cv = np.zeros((16, 512), dtype=np.float32)
        b1 = self._render(backend, osc, patch, freq_cv=freq_cv)
        b2 = self._render(backend, osc, patch, freq_cv=freq_cv)
        # Continuity: the last sample of b1 and the first of b2 are
        # one phase increment apart, so they should be very close
        # (delta < what 100 Hz advances in one sample).
        for v in range(16):
            jump = abs(float(b2[v, 0]) - float(b1[v, -1]))
            # 100 Hz at 44.1 kHz -> dphi = 100/44100 ~ 0.00227.
            # |d sin(2pi*phi)| at that step is ~0.014 -- give some
            # margin for accumulated cumsum drift.
            assert jump < 0.05, (
                f"voice {v}: phase discontinuous between blocks "
                f"(jump={jump:.4f})"
            )

    def test_mono_freq_cv_with_voice_amp_cv_broadcasts(self):
        # Mono carrier amp-shaped per voice. freq_cv None -> single
        # phase, (F,) wave. amp_cv (V, F) -> wave * amp_cv broadcasts
        # to (V, F): each voice hears the same carrier at its own
        # amplitude. This is the cheap-poly trick.
        backend, osc, patch = self._build_osc_patch(
            waveform="sine", freq=440.0, amp=1.0
        )
        amp_cv = np.zeros((16, 512), dtype=np.float32)
        amp_cv[2, :] = 1.0
        amp_cv[9, :] = 0.5
        out = self._render(backend, osc, patch, freq_cv=None, amp_cv=amp_cv)
        assert out.shape == (16, 512)
        # Slot 2 at full amp, slot 9 at half, every other silent.
        assert float(np.max(np.abs(out[2]))) == pytest.approx(1.0, abs=0.05)
        assert float(np.max(np.abs(out[9]))) == pytest.approx(0.5, abs=0.05)
        for i in (0, 1, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15):
            assert float(np.max(np.abs(out[i]))) == 0.0

    def test_mono_freq_cv_returns_mono(self):
        # Backward compat: no freq_cv and no amp_cv -> the old (F,)
        # output shape, scalar phase accumulator.
        backend, osc, patch = self._build_osc_patch(
            waveform="sine", freq=440.0, amp=0.5
        )
        out = self._render(backend, osc, patch)
        assert out.shape == (512,)
        assert out.ndim == 1

    def test_voice_matches_mono_with_cv(self):
        # The voice path and the mono-with-freq_cv path both use
        # cumsum-based phase integration, so they should agree to
        # floating-point tolerance when fed the same effective CV.
        # (The mono no-CV path uses arange, off by one phase
        # increment -- pre-existing convention difference outside
        # the scope of slice 3b.)
        backend1, osc1, patch1 = self._build_osc_patch(
            waveform="saw", freq=440.0, amp=0.5
        )
        mono_cv = np.zeros(512, dtype=np.float32)
        mono = self._render(backend1, osc1, patch1, freq_cv=mono_cv)

        backend2, osc2, patch2 = self._build_osc_patch(
            waveform="saw", freq=440.0, amp=0.5
        )
        voice_cv = np.zeros((16, 512), dtype=np.float32)
        voice = self._render(backend2, osc2, patch2, freq_cv=voice_cv)
        # Voice 0 should match the mono-with-CV path bit-near.
        np.testing.assert_allclose(voice[0], mono, atol=1e-5)


# ---------------------------------------------------------------------------
# LFO voice-aware path (slice 3b.2)
# ---------------------------------------------------------------------------


class TestLFOVoiceAware:
    """LFO slice-3b.2 tests.

    The LFO renderer branches on rate_cv's ndim. None / 1D -> the
    pre-slice-3 scalar phase path; output (F,). 2D (V, F) -> V
    independent phase accumulators, each clocked at its own per-voice
    block-mean rate; output (V, F).
    """

    def _build_lfo_patch(self, **lfo_params):
        patch = Patch()
        lfo = patch.add_module("lfo", params=lfo_params)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        return backend, lfo, patch

    def _render(self, backend, lfo, patch, rate_cv=None, frames=512):
        from pysynthrack.core.patch import Cable
        CV_SRC = 9301
        patch.cables[:] = [
            c for c in patch.cables if c.src_module_id != CV_SRC
        ]
        buffers = {}
        if rate_cv is not None:
            patch.cables.append(Cable(
                src_module_id=CV_SRC, src_port="out",
                dst_module_id=lfo.id, dst_port="rate_cv",
            ))
            buffers[(CV_SRC, "out")] = rate_cv
        return backend._render_lfo(lfo, frames, buffers, patch)

    def test_voice_rate_cv_returns_voice_shape(self):
        # (V, F) rate_cv -> (V, F) output. Zero CV in every slot means
        # all voices clock at the base rate from the same starting
        # phase, so every voice produces an identical waveform.
        backend, lfo, patch = self._build_lfo_patch(
            waveform="sine", rate=4.0, depth=1.0, bipolar=True,
        )
        rate_cv = np.zeros((16, 512), dtype=np.float32)
        out = self._render(backend, lfo, patch, rate_cv=rate_cv)
        assert out.shape == (16, 512)
        for v in range(1, 16):
            np.testing.assert_allclose(out[v], out[0], atol=1e-6)

    def test_per_voice_rate_via_rate_cv(self):
        # Per-voice rate: voice 0 at 0V (=base rate), voice 5 at +2V
        # (=4x base rate, two octaves). After one block, voice 5
        # should have advanced ~4x more phase than voice 0.
        backend, lfo, patch = self._build_lfo_patch(
            waveform="saw", rate=4.0, depth=1.0, bipolar=True,
        )
        rate_cv = np.zeros((16, 512), dtype=np.float32)
        rate_cv[0, :] = 0.0
        rate_cv[5, :] = 2.0  # +2 octaves -> 16 Hz
        out = self._render(backend, lfo, patch, rate_cv=rate_cv)
        # Saw shape: phase is a ramp. Count zero crossings as a proxy
        # for how many cycles each voice completed.
        def zc(buf):
            return int(np.sum(np.diff(np.sign(buf)) != 0))
        zc_v0 = zc(out[0])
        zc_v5 = zc(out[5])
        # 4 Hz at 44.1 kHz over 512 samples -> ~0.046 cycles (likely 0
        # zero crossings in saw). 16 Hz -> ~0.186 cycles (also small).
        # Better test: voice 5's max phase should be larger than v0's.
        # Use sine instead so we get measurable values, or check that
        # the cumulative phase advance differs. Stick with the simpler
        # zero-crossing check + an env-amplitude check on a longer
        # render to make sure rates differ.
        # If both are 0 (sub-cycle), check final-sample values: at saw
        # ramps from -1 to +1 over one period, voice 5 should be 4x
        # further along its ramp than voice 0.
        if zc_v5 == zc_v0:
            # both are sub-cycle; compare endpoint phase progress.
            # saw runs -1 -> +1 then jumps back; voice 5's value
            # should be greater than voice 0's by roughly the rate
            # ratio in a small phase range.
            v0_end = float(out[0, -1])
            v5_end = float(out[5, -1])
            # voice 5 has rolled further into the saw ramp.
            assert v5_end > v0_end + 0.005, (
                f"voice 5 didn\'t advance further than voice 0: "
                f"v0={v0_end:.4f}, v5={v5_end:.4f}"
            )
        else:
            assert zc_v5 > zc_v0, (
                f"voice 5 should cycle more than voice 0: "
                f"zc0={zc_v0}, zc5={zc_v5}"
            )

    def test_per_voice_phase_persists_across_blocks(self):
        # Phase per voice must accumulate across blocks -- not reset.
        # Render two blocks; the second block's first sample should
        # continue smoothly from the first block's last sample.
        backend, lfo, patch = self._build_lfo_patch(
            waveform="sine", rate=1.0, depth=1.0, bipolar=True,
        )
        rate_cv = np.zeros((16, 512), dtype=np.float32)
        b1 = self._render(backend, lfo, patch, rate_cv=rate_cv)
        b2 = self._render(backend, lfo, patch, rate_cv=rate_cv)
        for v in range(16):
            jump = abs(float(b2[v, 0]) - float(b1[v, -1]))
            # 1 Hz at 44.1 kHz -> dphi = 1/44100 -> sin step ~ 1.4e-4.
            # Give generous margin for sub-sample interpolation.
            assert jump < 0.01, (
                f"voice {v}: phase discontinuous between blocks "
                f"(jump={jump:.5f})"
            )

    def test_mono_rate_cv_returns_mono(self):
        # Backward compat: 1D rate_cv -> scalar phase path, 1D output.
        backend, lfo, patch = self._build_lfo_patch(
            waveform="sine", rate=4.0, depth=1.0, bipolar=True,
        )
        rate_cv = np.zeros(512, dtype=np.float32)
        out = self._render(backend, lfo, patch, rate_cv=rate_cv)
        assert out.shape == (512,)
        assert out.ndim == 1

    def test_no_rate_cv_returns_mono(self):
        # No rate_cv connected -> mono fast path. Same shape as the
        # pre-slice LFO.
        backend, lfo, patch = self._build_lfo_patch(
            waveform="sine", rate=4.0, depth=1.0, bipolar=True,
        )
        out = self._render(backend, lfo, patch, rate_cv=None)
        assert out.shape == (512,)

    def test_unipolar_voice_output_stays_non_negative(self):
        # bipolar=False on the voice path applies the same [-1,1] ->
        # [0,1] mapping as the mono path. Verify per-voice output is
        # always >= 0 across many blocks.
        backend, lfo, patch = self._build_lfo_patch(
            waveform="sine", rate=8.0, depth=1.0, bipolar=False,
        )
        rate_cv = np.zeros((16, 512), dtype=np.float32)
        for _ in range(4):
            out = self._render(backend, lfo, patch, rate_cv=rate_cv)
        assert out.shape == (16, 512)
        assert float(np.min(out)) >= -1e-7, (
            f"unipolar LFO produced negative value: min={float(np.min(out))}"
        )

    def test_voice_matches_mono_at_zero_cv(self):
        # Voice path row 0 with rate_cv = 0 should match the mono path
        # with rate_cv = 0 to floating-point tolerance -- both reduce
        # to the same per-block phase ramp.
        backend1, lfo1, patch1 = self._build_lfo_patch(
            waveform="triangle", rate=2.5, depth=0.8, bipolar=True,
        )
        mono = self._render(
            backend1, lfo1, patch1, rate_cv=np.zeros(512, dtype=np.float32)
        )
        backend2, lfo2, patch2 = self._build_lfo_patch(
            waveform="triangle", rate=2.5, depth=0.8, bipolar=True,
        )
        voice = self._render(
            backend2, lfo2, patch2, rate_cv=np.zeros((16, 512), dtype=np.float32)
        )
        np.testing.assert_allclose(voice[0], mono, atol=1e-6)


# ---------------------------------------------------------------------------
# Crossover voice-aware path (slice 3b.2)
# ---------------------------------------------------------------------------


class TestCrossoverVoiceAware:
    """Crossover slice-3b.2 tests.

    The Crossover renderer branches on its audio input's ndim. 1D ->
    the pre-slice scalar cascaded-biquad path; outputs 1D low + high.
    2D (V, F) -> V parallel cascaded biquads per branch, each with its
    own (x1, x2, y1, y2) memory; outputs (V, F) low + (V, F) high.
    """

    def _build_xo_patch(self, **xo_params):
        patch = Patch()
        xo = patch.add_module("crossover", params=xo_params)
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        return backend, xo, patch

    def _render(self, backend, xo, patch, audio, frames=512):
        from pysynthrack.core.patch import Cable
        AUDIO_SRC = 9401
        patch.cables[:] = [
            c for c in patch.cables if c.src_module_id != AUDIO_SRC
        ]
        patch.cables.append(Cable(
            src_module_id=AUDIO_SRC, src_port="out",
            dst_module_id=xo.id, dst_port="in",
        ))
        buffers = {(AUDIO_SRC, "out"): audio}
        return backend._render_crossover(xo, frames, buffers, patch)

    def test_voice_audio_returns_voice_shape_for_both_outputs(self):
        # (V, F) audio in -> {"low": (V, F), "high": (V, F)}.
        backend, xo, patch = self._build_xo_patch(frequency=1000.0)
        sr = 44100
        t = np.arange(512) / sr
        # Mid-band test tone on slot 7 only.
        audio = np.zeros((16, 512), dtype=np.float32)
        audio[7, :] = (np.sin(2 * np.pi * 1000 * t) * 0.5).astype(np.float32)
        out = self._render(backend, xo, patch, audio)
        assert out["low"].shape == (16, 512)
        assert out["high"].shape == (16, 512)
        # Slot 7 has signal; every other slot is silent.
        for i in range(16):
            if i == 7:
                continue
            assert float(np.max(np.abs(out["low"][i]))) == 0.0
            assert float(np.max(np.abs(out["high"][i]))) == 0.0
        # Slot 7 should have both branches producing nonzero output
        # (1 kHz is right at the corner -- -6 dB on each branch).
        assert float(np.max(np.abs(out["low"][7]))) > 1e-3
        assert float(np.max(np.abs(out["high"][7]))) > 1e-3

    def test_per_voice_biquad_memory_is_independent(self):
        # Warm up slot 0 with sustained signal, then drive slot 5 with
        # an impulse and slot 0 with silence. Slot 5's impulse must
        # not leak into slot 0's output -- the proof of independent
        # per-voice biquad memory.
        backend, xo, patch = self._build_xo_patch(frequency=500.0)
        warmup = np.zeros((16, 512), dtype=np.float32)
        warmup[0, :] = 1.0
        for _ in range(5):
            self._render(backend, xo, patch, warmup)

        impulse = np.zeros((16, 512), dtype=np.float32)
        impulse[5, 0] = 1.0
        out = self._render(backend, xo, patch, impulse)

        # Slot 5 must respond to its own impulse.
        assert float(np.max(np.abs(out["low"][5, :100]))) > 1e-3
        # Slot 0 has no fresh input, only decaying memory from warmup.
        # The slot-5 impulse must NOT spike slot 0. Check that slot 0
        # decays smoothly (no sharp peak from cross-talk).
        slot0_first_quarter = float(np.mean(np.abs(out["low"][0, :128])))
        slot0_last_quarter = float(np.mean(np.abs(out["low"][0, 384:])))
        assert slot0_last_quarter <= slot0_first_quarter + 1e-6

    def test_low_plus_high_recombines_to_input(self):
        # The Linkwitz-Riley guarantee on the voice path: low + high
        # sums back to the (delayed) input, sample-accurate aside
        # from group delay. Verify on slot 3 with a sine in the pass
        # region of one branch.
        backend, xo, patch = self._build_xo_patch(frequency=2000.0)
        sr = 44100
        t = np.arange(512) / sr
        audio = np.zeros((16, 512), dtype=np.float32)
        audio[3, :] = (np.sin(2 * np.pi * 500 * t) * 0.5).astype(np.float32)
        # Let the biquad memory settle.
        for _ in range(4):
            out = self._render(backend, xo, patch, audio)
        recombined = out["low"][3] + out["high"][3]
        # Compare RMS to the input -- the recombined signal should
        # match the input in magnitude to within a few percent
        # (LR4 sum is flat in magnitude, modulo settling).
        input_rms = float(np.sqrt(np.mean(audio[3] ** 2)))
        recombined_rms = float(np.sqrt(np.mean(recombined ** 2)))
        assert abs(recombined_rms - input_rms) / input_rms < 0.05, (
            f"recombined RMS ({recombined_rms:.4f}) deviates from "
            f"input RMS ({input_rms:.4f})"
        )

    def test_mono_audio_still_returns_mono(self):
        # Backward compat: 1D in -> 1D low + 1D high.
        backend, xo, patch = self._build_xo_patch(frequency=1000.0)
        sr = 44100
        t = np.arange(512) / sr
        audio = (np.sin(2 * np.pi * 1000 * t) * 0.5).astype(np.float32)
        for _ in range(3):
            out = self._render(backend, xo, patch, audio)
        assert out["low"].shape == (512,)
        assert out["high"].shape == (512,)
        assert out["low"].ndim == 1
        assert out["high"].ndim == 1

    def test_voice_path_matches_mono_path_for_replicated_voice(self):
        # Per-voice rows are independent: feeding identical signal to
        # every slot should produce identical output rows that all
        # match the mono path bit-near.
        backend_voice, xo_v, patch_v = self._build_xo_patch(frequency=800.0)
        backend_mono, xo_m, patch_m = self._build_xo_patch(frequency=800.0)
        sr = 44100
        t = np.arange(512) / sr
        tone_mono = (np.sin(2 * np.pi * 600 * t) * 0.5).astype(np.float32)
        tone_voice = np.broadcast_to(tone_mono, (16, 512)).copy()
        # Run multiple blocks so the biquad memory tracks identically.
        for _ in range(4):
            out_v = self._render(backend_voice, xo_v, patch_v, tone_voice)
            out_m = self._render(backend_mono, xo_m, patch_m, tone_mono)
        # Every voice row should match the mono output to fp tolerance.
        for v in range(16):
            np.testing.assert_allclose(out_v["low"][v], out_m["low"], atol=1e-5)
            np.testing.assert_allclose(out_v["high"][v], out_m["high"], atol=1e-5)
