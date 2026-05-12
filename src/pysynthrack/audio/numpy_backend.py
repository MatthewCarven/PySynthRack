"""NumpyBackend — fallback audio engine using sounddevice + numpy.

Topologically walks the patch each audio callback and synthesizes samples
into a stereo output buffer. Per-module persistent state (oscillator
phase, keyboard voice envelopes, filter biquad memory) lives in
``self._state``.

Anti-aliasing for saw/square/triangle is intentionally absent in v0.2 —
PolyBLEP / wavetable upgrade planned. The naive shapes are fine for now,
and the filter helps mask aliasing artefacts above its cutoff.
"""
from __future__ import annotations

import threading
from typing import Any

import numpy as np

from ..core.patch import Patch
from ..modules.keyboard import midi_to_freq
from .backend import AudioBackend

# Imported lazily so a missing PortAudio install doesn't crash module import.
try:
    import sounddevice as sd  # type: ignore
    _HAS_SOUNDDEVICE = True
except Exception:  # pragma: no cover - environment-dependent
    sd = None  # type: ignore[assignment]
    _HAS_SOUNDDEVICE = False


class NumpyBackend(AudioBackend):
    """Pure-Python fallback. Slower than pyo but works wherever numpy does."""

    name = "numpy"

    def __init__(self, sample_rate: int = 44100, block_size: int = 512) -> None:
        super().__init__(sample_rate=sample_rate, block_size=block_size)
        self._patch: Patch | None = None
        self._topo_order: list[int] = []
        self._state: dict[int, dict[str, Any]] = {}
        self._stream: Any = None
        # GUI thread writes the patch reference; audio thread reads it.
        self._lock = threading.Lock()

    # ----- availability ----------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        return _HAS_SOUNDDEVICE

    # ----- compile ---------------------------------------------------------

    def compile(self, patch: Patch) -> None:
        with self._lock:
            self._patch = patch
            self._topo_order = self._topological_sort(patch)
            live_ids = set(patch.modules.keys())
            self._state = {k: v for k, v in self._state.items() if k in live_ids}

    @staticmethod
    def _topological_sort(patch: Patch) -> list[int]:
        """Kahn's algorithm — sources first, sinks last."""
        in_degree: dict[int, int] = {mid: 0 for mid in patch.modules}
        for cable in patch.cables:
            in_degree[cable.dst_module_id] = in_degree.get(cable.dst_module_id, 0) + 1
        ready = [mid for mid, deg in in_degree.items() if deg == 0]
        order: list[int] = []
        while ready:
            mid = ready.pop(0)
            order.append(mid)
            for cable in patch.cables_out_of(mid):
                in_degree[cable.dst_module_id] -= 1
                if in_degree[cable.dst_module_id] == 0:
                    ready.append(cable.dst_module_id)
        for mid in patch.modules:
            if mid not in order:
                order.append(mid)
        return order

    # ----- start / stop ----------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if not _HAS_SOUNDDEVICE:
            raise RuntimeError(
                "sounddevice is not installed — cannot start NumpyBackend. "
                "Install with: pip install sounddevice"
            )
        if self._patch is None:
            raise RuntimeError("Call compile(patch) before start().")
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=2,
            blocksize=self.block_size,
            dtype="float32",
            callback=self._audio_callback,
        )
        self._stream.start()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None
            self._running = False

    # ----- live params -----------------------------------------------------

    def set_param(self, module_id: int, name: str, value: Any) -> None:
        if self._patch is None or module_id not in self._patch.modules:
            return
        self._patch.get(module_id).set_param(name, value)

    # ----- audio thread ----------------------------------------------------

    def _audio_callback(self, outdata: np.ndarray, frames: int, time, status) -> None:
        if status:
            print(f"[NumpyBackend] stream status: {status}")
        with self._lock:
            patch = self._patch
            order = list(self._topo_order)
        if patch is None:
            outdata.fill(0.0)
            return

        buffers: dict[int, np.ndarray] = {}
        for module_id in order:
            module = patch.modules.get(module_id)
            if module is None:
                continue
            buffers[module_id] = self._render_module(module, frames, buffers, patch)

        out = np.zeros((frames, 2), dtype=np.float32)
        for module in patch.modules.values():
            if module.TYPE != "speaker_output":
                continue
            incoming = patch.cables_into(module.id)
            if not incoming:
                continue
            src_buf = buffers.get(incoming[0].src_module_id)
            if src_buf is None:
                continue
            gain = float(module.params.get("gain", 1.0))
            mixed = (src_buf * gain).astype(np.float32)
            out[:, 0] += mixed
            out[:, 1] += mixed

        np.clip(out, -1.0, 1.0, out=out)
        outdata[:] = out

    # ----- per-module rendering -------------------------------------------

    def _render_module(self, module, frames, buffers, patch) -> np.ndarray:
        if module.TYPE == "oscillator":
            return self._render_oscillator(module, frames)
        if module.TYPE == "keyboard":
            return self._render_keyboard(module, frames)
        if module.TYPE == "filter":
            return self._render_filter(module, frames, buffers, patch)
        if module.TYPE == "speaker_output":
            return np.zeros(frames, dtype=np.float32)
        return np.zeros(frames, dtype=np.float32)

    def _render_oscillator(self, module, frames: int) -> np.ndarray:
        state = self._state.setdefault(module.id, {"phase": 0.0})
        freq = float(module.params.get("freq", 440.0))
        amp = float(module.params.get("amp", 0.5))
        waveform = str(module.params.get("waveform", "sine"))
        sr = self.sample_rate

        phase_inc = freq / sr
        start_phase = state["phase"]
        phases = (start_phase + np.arange(frames, dtype=np.float64) * phase_inc) % 1.0
        state["phase"] = (start_phase + frames * phase_inc) % 1.0

        if waveform == "sine":
            wave = np.sin(2.0 * np.pi * phases)
        elif waveform == "saw":
            wave = 2.0 * phases - 1.0
        elif waveform == "square":
            wave = np.where(phases < 0.5, 1.0, -1.0)
        elif waveform == "triangle":
            wave = 1.0 - 4.0 * np.abs(phases - 0.5)
        else:
            wave = np.zeros(frames, dtype=np.float64)

        return (wave * amp).astype(np.float32)

    # ----- keyboard rendering ---------------------------------------------

    _KB_ATTACK_S = 0.005
    _KB_RELEASE_S = 0.020

    def _render_keyboard(self, module, frames: int) -> np.ndarray:
        state = self._state.setdefault(module.id, {"voices": {}})
        voices: dict[int, dict] = state["voices"]

        sr = self.sample_rate
        waveform = str(module.params.get("waveform", "sine"))
        volume = float(module.params.get("volume", 0.5))

        active = module.snapshot_active_notes()

        for note in active:
            voice = voices.get(note)
            if voice is None:
                voices[note] = {"phase": 0.0, "env": 0.0, "releasing": False}
            else:
                voice["releasing"] = False
        for note in list(voices):
            if note not in active:
                voices[note]["releasing"] = True

        attack_samples = max(1, int(self._KB_ATTACK_S * sr))
        release_samples = max(1, int(self._KB_RELEASE_S * sr))

        out = np.zeros(frames, dtype=np.float32)

        for note, voice in list(voices.items()):
            freq = midi_to_freq(note)
            phase_inc = freq / sr
            phases = (
                voice["phase"] + np.arange(frames, dtype=np.float64) * phase_inc
            ) % 1.0
            voice["phase"] = (voice["phase"] + frames * phase_inc) % 1.0

            if waveform == "sine":
                wave = np.sin(2.0 * np.pi * phases)
            elif waveform == "saw":
                wave = 2.0 * phases - 1.0
            elif waveform == "square":
                wave = np.where(phases < 0.5, 1.0, -1.0)
            elif waveform == "triangle":
                wave = 1.0 - 4.0 * np.abs(phases - 0.5)
            else:
                wave = np.zeros(frames, dtype=np.float64)

            if voice["releasing"]:
                delta = -1.0 / release_samples
            elif voice["env"] < 1.0:
                delta = 1.0 / attack_samples
            else:
                delta = 0.0
            env_ramp = voice["env"] + np.arange(frames, dtype=np.float64) * delta
            np.clip(env_ramp, 0.0, 1.0, out=env_ramp)
            voice["env"] = float(max(0.0, min(1.0, voice["env"] + frames * delta)))

            out += (wave * env_ramp).astype(np.float32)

            if voice["releasing"] and voice["env"] <= 1e-5:
                del voices[note]

        return (out * volume).astype(np.float32)

    # ----- filter rendering ----------------------------------------------

    def _render_filter(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Apply a Robert Bristow-Johnson biquad to the upstream signal.

        Per-sample loop in Python — slow per sample but constant-cost per
        block; a 512-sample block costs ~100µs which is a tiny fraction of
        the ~11.6ms callback budget at 44.1 kHz. For multi-filter chains
        we'd reach for scipy.signal.lfilter (chunk-safe via ``zi``); we
        avoid scipy as a dep until the perf actually pinches.
        """
        incoming = patch.cables_into(module.id)
        if not incoming:
            return np.zeros(frames, dtype=np.float32)
        src_buf = buffers.get(incoming[0].src_module_id)
        if src_buf is None:
            return np.zeros(frames, dtype=np.float32)

        state = self._state.setdefault(
            module.id, {"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0}
        )

        mode = str(module.params.get("mode", "lowpass"))
        cutoff = float(module.params.get("cutoff", 1000.0))
        q = float(module.params.get("resonance", 0.707))

        sr = self.sample_rate
        # Clamp to a stable range. Above 0.45*sr the filter goes wild.
        cutoff = max(20.0, min(cutoff, sr * 0.45))
        q = max(0.1, min(q, 20.0))

        w0 = 2.0 * np.pi * cutoff / sr
        cos_w0 = float(np.cos(w0))
        sin_w0 = float(np.sin(w0))
        alpha = sin_w0 / (2.0 * q)

        # RBJ audio EQ cookbook coefficients.
        if mode == "lowpass":
            b0 = (1.0 - cos_w0) / 2.0
            b1 = 1.0 - cos_w0
            b2 = (1.0 - cos_w0) / 2.0
        elif mode == "highpass":
            b0 = (1.0 + cos_w0) / 2.0
            b1 = -(1.0 + cos_w0)
            b2 = (1.0 + cos_w0) / 2.0
        elif mode == "bandpass":
            b0 = sin_w0 / 2.0
            b1 = 0.0
            b2 = -sin_w0 / 2.0
        else:
            return src_buf.astype(np.float32)  # unknown → passthrough

        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha

        # Normalize.
        b0 /= a0
        b1 /= a0
        b2 /= a0
        a1n = a1 / a0
        a2n = a2 / a0

        x1 = state["x1"]
        x2 = state["x2"]
        y1 = state["y1"]
        y2 = state["y2"]

        out = np.empty(frames, dtype=np.float32)
        # Tight scalar loop. NumPy can't vectorize IIR (each sample depends
        # on the previous output). Python's still fast enough at this size.
        for n in range(frames):
            x0 = float(src_buf[n])
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2
            out[n] = y0
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0

        state["x1"] = x1
        state["x2"] = x2
        state["y1"] = y1
        state["y2"] = y2

        return out
