"""Tests for the CVKeyboard module + its numpy-backend renderer.

CVKeyboard (2026-07-01) is the controller sibling of :class:`Keyboard`:
the computer keys emit CV/gate only (no internal oscillator) so the voice
is built downstream. It shares Keyboard's VoiceSlots note-ingest machinery
and the ``ACCEPTS_COMPUTER_KEYS`` UI-routing marker, but emits a 1V/oct
per-voice ``pitch_cv`` (C4 = 0 V), a per-voice ``gate``, and twelve
per-pitch-class gate jacks ``key_c`` .. ``key_b``.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.cv_keyboard import (
    CVKeyboard,
    CV_REFERENCE_NOTE,
    KEY_GATE_NAMES,
)
from pysynthrack.modules.keyboard import Keyboard

MAX_VOICES = 16
FRAMES = 64


def _backend() -> NumpyBackend:
    be = NumpyBackend()
    be.sample_rate = 48000
    return be


class TestModel:
    def test_register_and_construct(self):
        patch = Patch()
        kb = patch.add_module("cv_keyboard")
        assert isinstance(kb, CVKeyboard)
        # Controller: octave is the only param (no waveform / volume).
        assert kb.params == {"octave": 4}
        assert kb.input_ports == []

    def test_output_ports(self):
        patch = Patch()
        kb = patch.add_module("cv_keyboard")
        names = [p.name for p in kb.output_ports]
        assert names == ["pitch_cv", "gate", *KEY_GATE_NAMES]
        kinds = {p.name: p.signal_kind for p in kb.output_ports}
        assert kinds["pitch_cv"] == "cv"
        assert kinds["gate"] == "gate"
        for name in KEY_GATE_NAMES:
            assert kinds[name] == "gate"
        # Twelve pitch classes, named in sharp spelling.
        assert len(KEY_GATE_NAMES) == 12
        assert KEY_GATE_NAMES[0] == "key_c" and KEY_GATE_NAMES[-1] == "key_b"

    def test_accepts_computer_keys_marker(self):
        # Both keyboard-family modules carry the UI-routing marker so the
        # app routes physical key events to either.
        assert getattr(CVKeyboard, "ACCEPTS_COMPUTER_KEYS", False) is True
        assert getattr(Keyboard, "ACCEPTS_COMPUTER_KEYS", False) is True

    def test_note_ingest_and_snapshot(self):
        patch = Patch()
        kb = patch.add_module("cv_keyboard")
        kb.note_on(60)
        kb.note_on(64)
        assert kb.snapshot_active_notes() == {60, 64}
        kb.note_off(60)
        assert kb.snapshot_active_notes() == {64}
        kb.all_notes_off()
        assert kb.snapshot_active_notes() == set()

    def test_snapshot_voice_slots_length(self):
        patch = Patch()
        kb = patch.add_module("cv_keyboard")
        slots = kb.snapshot_voice_slots()
        assert len(slots) == MAX_VOICES
        assert all(int(s["note"]) == -1 for s in slots)


class TestRenderer:
    def _render(self, kb):
        return _backend()._render_cv_keyboard(kb, FRAMES)

    def test_silent_when_no_keys(self):
        kb = Patch().add_module("cv_keyboard")
        out = self._render(kb)
        assert out["pitch_cv"].shape == (MAX_VOICES, FRAMES)
        assert out["gate"].shape == (MAX_VOICES, FRAMES)
        assert np.all(out["pitch_cv"] == 0.0)
        assert np.all(out["gate"] == 0.0)
        for name in KEY_GATE_NAMES:
            assert out[name].shape == (FRAMES,)
            assert np.all(out[name] == 0.0)

    def test_output_dict_keys_exact(self):
        kb = Patch().add_module("cv_keyboard")
        out = self._render(kb)
        assert set(out.keys()) == {"pitch_cv", "gate", *KEY_GATE_NAMES}

    @pytest.mark.parametrize(
        "note,expected_cv",
        [
            (60, 0.0),          # C4 reference
            (72, 1.0),          # C5 = +1 octave
            (48, -1.0),         # C3 = -1 octave
            (67, 7.0 / 12.0),   # G4
            (69, 9.0 / 12.0),   # A4
        ],
    )
    def test_pitch_cv_is_1v_per_oct(self, note, expected_cv):
        assert CV_REFERENCE_NOTE == 60
        kb = Patch().add_module("cv_keyboard")
        kb.note_on(note)
        out = self._render(kb)
        slots = kb.snapshot_voice_slots()
        row = next(i for i, s in enumerate(slots) if int(s["note"]) == note)
        assert np.allclose(out["pitch_cv"][row], expected_cv)
        assert np.all(out["gate"][row] == 1.0)

    def test_per_voice_gate_independence(self):
        kb = Patch().add_module("cv_keyboard")
        kb.note_on(60)
        kb.note_on(64)
        out = self._render(kb)
        slots = kb.snapshot_voice_slots()
        gating_rows = [i for i, s in enumerate(slots) if int(s["note"]) != -1]
        assert len(gating_rows) == 2
        for i in gating_rows:
            assert np.all(out["gate"][i] == 1.0)
        # every other row stays silent
        silent = [i for i in range(MAX_VOICES) if i not in gating_rows]
        for i in silent:
            assert np.all(out["gate"][i] == 0.0)

    def test_key_gates_fire_per_pitch_class(self):
        kb = Patch().add_module("cv_keyboard")
        kb.note_on(60)  # C4
        kb.note_on(64)  # E4
        out = self._render(kb)
        assert np.all(out["key_c"] == 1.0)
        assert np.all(out["key_e"] == 1.0)
        # untouched pitch classes stay low
        for name in KEY_GATE_NAMES:
            if name not in ("key_c", "key_e"):
                assert np.all(out[name] == 0.0), name

    def test_key_gate_octave_folding(self):
        # Pressing C in two octaves lights the single key_c jack.
        kb = Patch().add_module("cv_keyboard")
        kb.note_on(60)  # C4
        kb.note_on(72)  # C5
        out = self._render(kb)
        assert np.all(out["key_c"] == 1.0)
        # Both voices carry their own (correct) pitch CV.
        slots = kb.snapshot_voice_slots()
        cvs = sorted(
            float(out["pitch_cv"][i, 0])
            for i, s in enumerate(slots)
            if int(s["note"]) != -1
        )
        assert np.allclose(cvs, [0.0, 1.0])

    def test_release_holds_pitch_drops_gate(self):
        kb = Patch().add_module("cv_keyboard")
        kb.note_on(60)  # C4
        kb.note_on(72)  # C5
        kb.note_off(60)  # release C4; C5 still held
        out = self._render(kb)
        slots = kb.snapshot_voice_slots()
        c4_row = next(i for i, s in enumerate(slots) if int(s["note"]) == 60)
        # released voice: pitch held (in tune for the ADSR release tail),
        # gate fallen.
        assert np.allclose(out["pitch_cv"][c4_row], 0.0)
        assert np.all(out["gate"][c4_row] == 0.0)
        # key_c still high because C5 is still physically held.
        assert np.all(out["key_c"] == 1.0)

    def test_all_notes_off_clears_everything(self):
        kb = Patch().add_module("cv_keyboard")
        kb.note_on(60)
        kb.note_on(64)
        kb.all_notes_off()
        out = self._render(kb)
        assert np.all(out["gate"] == 0.0)
        assert np.all(out["pitch_cv"] == 0.0)
        for name in KEY_GATE_NAMES:
            assert np.all(out[name] == 0.0)


class TestDispatch:
    def test_dispatch_routes_to_cv_keyboard(self):
        # The backend's TYPE dispatch reaches the new renderer.
        patch = Patch()
        kb = patch.add_module("cv_keyboard")
        kb.note_on(60)
        out = _backend()._render_module(kb, FRAMES, {}, patch)
        assert isinstance(out, dict)
        assert set(out.keys()) == {"pitch_cv", "gate", *KEY_GATE_NAMES}


class TestIntegration:
    def test_pitch_cv_drives_external_oscillator_in_tune(self):
        # The headline: pitch_cv -> Oscillator.freq_cv plays in tune.
        # The voice must pass through a gate-driven VCA, exactly like a
        # hardware modular: an oscillator drones on every voice slot, and
        # the gate/VCA is what articulates (and silences the idle voices,
        # which sit at pitch_cv = 0 = the C4 reference).
        patch = Patch()
        kb = patch.add_module("cv_keyboard")
        osc = patch.add_module("oscillator")
        osc.params.update(waveform="saw", freq=261.6256, amp=0.5)
        adsr = patch.add_module("adsr")
        adsr.params.update(attack=0.001, decay=0.02, sustain=1.0, release=0.05)
        vca = patch.add_module("vca")
        spk = patch.add_module("speaker_output")
        patch.connect(kb.id, "pitch_cv", osc.id, "freq_cv")
        patch.connect(osc.id, "out", vca.id, "audio")
        patch.connect(kb.id, "gate", adsr.id, "gate")
        patch.connect(adsr.id, "cv", vca.id, "cv")
        patch.connect(vca.id, "out", spk.id, "in")

        be = _backend()
        be.compile(patch)
        kb.note_on(72)  # C5 -> expect ~523 Hz
        buf = [be.render_block(256) for _ in range(200)]
        sig = np.concatenate(buf)[-16384:]
        if sig.ndim == 2:
            sig = sig.mean(axis=1)
        sig = sig - sig.mean()
        spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
        freqs = np.fft.rfftfreq(len(sig), 1.0 / 48000)
        dominant = freqs[int(np.argmax(spec))]
        assert abs(dominant - 523.25) < 6.0, dominant


class TestExamplePatch:
    def test_example_loads_and_renders(self):
        from pysynthrack._resources import examples_dir
        from pysynthrack.io_patch import load_patch

        path = examples_dir() / "cv_keyboard_external_voice.json"
        patch = load_patch(str(path))
        be = _backend()
        be.compile(patch)
        # find the cv_keyboard
        kb = next(m for m in patch.modules.values() if m.TYPE == "cv_keyboard")
        kb.note_on(60)  # C -> pitched voice + per-key noise burst
        peak = max(float(np.max(np.abs(be.render_block(256)))) for _ in range(120))
        assert peak > 0.02
