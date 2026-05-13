"""NumpyBackend — fallback audio engine using sounddevice + numpy.

Topologically walks the patch each audio callback and synthesizes samples
into a stereo output buffer. Per-module persistent state (oscillator
phase, keyboard voice envelopes, filter biquad memory, ADSR phase) lives
in ``self._state``.

Buffer addressing is port-keyed: ``buffers[(module_id, port_name)]``.
This lets modules emit multiple outputs (the Keyboard emits both an
audio buffer and a gate buffer), and downstream lookups go through the
specific cable's ``src_port`` and ``dst_port`` so a VCA can read its
``audio`` and ``cv`` inputs by name.

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
        # Parallel map from module_id → module TYPE that owned the state.
        # Used in compile() to discard state when a patch swap reuses the
        # same id for a different module type (e.g. patch A id=1 is an
        # oscillator, patch B id=1 is a keyboard). Without this guard the
        # oscillator's phase dict would leak into the keyboard renderer
        # and KeyError on a missing schema key.
        self._state_types: dict[int, str] = {}
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
            # Drop state for modules that no longer exist, or whose type
            # has changed since the previous compile (the patch-swap case
            # — two patches both numbering from id=1 with different module
            # types in those slots).
            live_types = {mid: m.TYPE for mid, m in patch.modules.items()}
            for mid in list(self._state.keys()):
                if mid not in live_types:
                    self._state.pop(mid, None)
                    self._state_types.pop(mid, None)
                elif self._state_types.get(mid) != live_types[mid]:
                    self._state.pop(mid, None)
            # Record the current type for every live module so the next
            # compile can compare against it.
            self._state_types = dict(live_types)

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
        out = self.render_block(frames)
        if out is None:
            outdata.fill(0.0)
            return
        outdata[:] = out

    def render_block(self, frames: int) -> np.ndarray | None:
        """Pure-function rendering of one block. Used by the audio callback
        and by offline tests (no PortAudio device needed)."""
        with self._lock:
            patch = self._patch
            order = list(self._topo_order)
        if patch is None:
            return None

        # Port-keyed buffer store. A single module may emit multiple outputs
        # (Keyboard publishes both audio and gate).
        buffers: dict[tuple[int, str], np.ndarray] = {}
        for module_id in order:
            module = patch.modules.get(module_id)
            if module is None:
                continue
            result = self._render_module(module, frames, buffers, patch)
            if result is None:
                continue
            if isinstance(result, dict):
                for port_name, buf in result.items():
                    buffers[(module_id, port_name)] = buf
            else:
                # Legacy single-output convention. Stash under the module's
                # first declared output port name (every existing single-out
                # module declares exactly one).
                if module.OUTPUT_PORTS:
                    buffers[(module_id, module.OUTPUT_PORTS[0].name)] = result

        out = np.zeros((frames, 2), dtype=np.float32)
        for module in patch.modules.values():
            if module.TYPE != "speaker_output":
                continue
            incoming = patch.cables_into(module.id)
            if not incoming:
                continue
            cable = incoming[0]
            src_buf = buffers.get((cable.src_module_id, cable.src_port))
            if src_buf is None:
                continue
            gain = float(module.params.get("gain", 1.0))
            mixed = (src_buf * gain).astype(np.float32)
            out[:, 0] += mixed
            out[:, 1] += mixed

        np.clip(out, -1.0, 1.0, out=out)
        return out

    # ----- per-module rendering -------------------------------------------

    def _render_module(self, module, frames, buffers, patch):
        if module.TYPE == "oscillator":
            return self._render_oscillator(module, frames)
        if module.TYPE == "keyboard":
            return self._render_keyboard(module, frames)
        if module.TYPE == "filter":
            return self._render_filter(module, frames, buffers, patch)
        if module.TYPE == "adsr":
            return self._render_adsr(module, frames, buffers, patch)
        if module.TYPE == "vca":
            return self._render_vca(module, frames, buffers, patch)
        if module.TYPE == "lfo":
            return self._render_lfo(module, frames)
        if module.TYPE == "mixer":
            return self._render_mixer(module, frames, buffers, patch)
        if module.TYPE == "speaker_output":
            return None  # sink — drained by the speaker pass
        return None

    # ----- input port helper ----------------------------------------------

    @staticmethod
    def _input_buffer(patch, buffers, dst_module_id: int, dst_port: str):
        """Look up the buffer feeding a specific input port, or None."""
        for cable in patch.cables_into(dst_module_id):
            if cable.dst_port == dst_port:
                return buffers.get((cable.src_module_id, cable.src_port))
        return None

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

    def _render_keyboard(self, module, frames: int) -> dict[str, np.ndarray]:
        """Returns both the audio buffer and a gate buffer.

        Gate semantics: high while any key is held, low while idle. This is
        the master-envelope mode — new notes pressed during a held chord do
        not retrigger the gate. The per-voice attack/release ramps that
        live inside this function prevent click on note-on/off; they are
        independent of any external ADSR plugged into the gate.
        """
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

        audio = np.zeros(frames, dtype=np.float32)

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

            audio += (wave * env_ramp).astype(np.float32)

            if voice["releasing"] and voice["env"] <= 1e-5:
                del voices[note]

        audio *= volume

        gate_value = 1.0 if active else 0.0
        gate = np.full(frames, gate_value, dtype=np.float32)

        return {"out": audio, "gate": gate}

    # ----- filter rendering ----------------------------------------------

    def _render_filter(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Apply a Robert Bristow-Johnson biquad to the upstream signal.

        Per-sample loop in Python — slow per sample but constant-cost per
        block; a 512-sample block costs ~100µs which is a tiny fraction of
        the ~11.6ms callback budget at 44.1 kHz. For multi-filter chains
        we'd reach for scipy.signal.lfilter (chunk-safe via ``zi``); we
        avoid scipy as a dep until the perf actually pinches.
        """
        src_buf = self._input_buffer(patch, buffers, module.id, "in")
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

    # ----- ADSR rendering -------------------------------------------------

    # Gate is treated as "high" once it crosses this threshold; this gives
    # us tolerance against fractional gate values (e.g. an LFO-style gate
    # in some future patching) without false triggers on numerical noise.
    _GATE_HIGH = 0.5

    def _render_adsr(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sample-accurate ADSR driven by a gate signal.

        State machine: idle → attack → decay → sustain → release → idle.
        - Attack ramps linearly from current level to 1.0 over ``attack``
          seconds (so retriggering before full release picks up where the
          envelope was, no click).
        - Decay ramps from 1.0 to ``sustain`` over ``decay`` seconds.
        - Sustain holds at ``sustain`` while the gate stays high.
        - Release ramps from current level to 0.0 over ``release`` seconds.

        All durations are clamped to a >= 1-sample minimum so any param
        edits remain numerically stable.
        """
        gate_buf = self._input_buffer(patch, buffers, module.id, "gate")
        sr = self.sample_rate

        attack_s = max(0.0, float(module.params.get("attack", 0.01)))
        decay_s = max(0.0, float(module.params.get("decay", 0.1)))
        sustain = max(0.0, min(1.0, float(module.params.get("sustain", 0.7))))
        release_s = max(0.0, float(module.params.get("release", 0.3)))

        # Sample-step deltas. +inf when duration is zero would jump in one
        # sample; clamp to a single-sample step so the state machine still
        # advances cleanly.
        attack_step = 1.0 / max(1.0, attack_s * sr)
        decay_step = (1.0 - sustain) / max(1.0, decay_s * sr)
        # Release step is recomputed at gate-fall using the level at the
        # moment of release, so a release from mid-attack still takes the
        # full release time.
        state = self._state.setdefault(
            module.id,
            {"phase": "idle", "level": 0.0, "prev_gate": False, "release_step": 0.0},
        )

        out = np.empty(frames, dtype=np.float32)

        for n in range(frames):
            gate_high = (
                bool(gate_buf[n] > self._GATE_HIGH) if gate_buf is not None else False
            )

            # Edge detection.
            if gate_high and not state["prev_gate"]:
                state["phase"] = "attack"
            elif not gate_high and state["prev_gate"]:
                # Gate fell — compute a release step that drops the *current*
                # level over the release-time window. Avoids the snap that
                # would happen if we used (sustain / time).
                state["release_step"] = state["level"] / max(1.0, release_s * sr)
                state["phase"] = "release"
            state["prev_gate"] = gate_high

            phase = state["phase"]
            level = state["level"]

            if phase == "attack":
                level += attack_step
                if level >= 1.0:
                    level = 1.0
                    state["phase"] = "decay"
            elif phase == "decay":
                level -= decay_step
                if level <= sustain:
                    level = sustain
                    state["phase"] = "sustain"
            elif phase == "sustain":
                level = sustain
            elif phase == "release":
                level -= state["release_step"]
                if level <= 0.0:
                    level = 0.0
                    state["phase"] = "idle"
            # idle → level stays 0

            state["level"] = level
            out[n] = level

        return out.astype(np.float32)

    # ----- LFO rendering --------------------------------------------------

    def _render_lfo(self, module, frames: int) -> np.ndarray:
        """Low-frequency oscillator emitting a CV signal.

        Phase state is per-module (so multiple LFOs in one patch don't
        share state). ``random`` waveform is sample-and-hold: re-roll
        once per cycle when the phase wraps past 1.0.
        """
        state = self._state.setdefault(
            module.id, {"phase": 0.0, "random_value": 0.0}
        )

        waveform = str(module.params.get("waveform", "sine"))
        rate = float(module.params.get("rate", 4.0))
        depth = float(module.params.get("depth", 1.0))
        bipolar = bool(module.params.get("bipolar", False))

        sr = self.sample_rate
        # Clamp to a safe range: 0.001 Hz floor (one cycle per ~17 min) and
        # an effective ceiling at Nyquist/2 — beyond that an LFO is just
        # an audio oscillator and the user should reach for ``oscillator``.
        rate = max(0.001, min(rate, sr * 0.45))
        depth = max(0.0, min(depth, 1.0))

        phase_inc = rate / sr
        start_phase = state["phase"]
        phases = (start_phase + np.arange(frames, dtype=np.float64) * phase_inc) % 1.0
        new_phase = (start_phase + frames * phase_inc) % 1.0

        if waveform == "sine":
            wave = np.sin(2.0 * np.pi * phases)
        elif waveform == "triangle":
            wave = 1.0 - 4.0 * np.abs(phases - 0.5)
        elif waveform == "square":
            wave = np.where(phases < 0.5, 1.0, -1.0)
        elif waveform == "saw":
            wave = 2.0 * phases - 1.0
        elif waveform == "random":
            # Sample-and-hold: detect each phase wrap and re-roll.
            if frames > 0:
                diffs = np.diff(np.concatenate([[start_phase], phases]))
                wave = np.empty(frames, dtype=np.float64)
                current = state["random_value"]
                if start_phase == 0.0 and state["random_value"] == 0.0:
                    current = float(np.random.uniform(-1.0, 1.0))
                for i in range(frames):
                    if diffs[i] < 0.0:
                        current = float(np.random.uniform(-1.0, 1.0))
                    wave[i] = current
                state["random_value"] = current
            else:
                wave = np.zeros(0, dtype=np.float64)
        else:
            wave = np.zeros(frames, dtype=np.float64)

        state["phase"] = new_phase

        if not bipolar:
            # Map [-1, 1] → [0, 1] (so a sine-LFO into a VCA gives a smooth
            # tremolo rather than an inverted-phase audio fight).
            wave = (wave + 1.0) * 0.5

        return (wave * depth).astype(np.float32)

    # ----- VCA rendering --------------------------------------------------

    def _render_vca(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Voltage-controlled amplifier: out = audio * cv * gain.

        Missing audio in → silence. Missing CV in → passthrough at unity
        (so a VCA with no envelope still behaves like a gain stage).
        """
        audio_in = self._input_buffer(patch, buffers, module.id, "audio")
        if audio_in is None:
            return np.zeros(frames, dtype=np.float32)
        cv_in = self._input_buffer(patch, buffers, module.id, "cv")
        gain = float(module.params.get("gain", 1.0))
        if cv_in is None:
            return (audio_in * gain).astype(np.float32)
        return (audio_in * cv_in * gain).astype(np.float32)

    # ----- Mixer rendering ------------------------------------------------

    def _render_mixer(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sum four audio inputs with per-channel gain trims and a master.

        Unconnected channels contribute silence. The signal is::

            out = master * sum_i (gain_i * input_i)

        Output is clipped at the speaker stage, not here — so a hot
        mixer feeding a filter still has the headroom the filter needs.
        """
        master = float(module.params.get("master", 0.7))
        out = np.zeros(frames, dtype=np.float32)
        for idx in (1, 2, 3, 4):
            buf = self._input_buffer(patch, buffers, module.id, f"in{idx}")
            if buf is None:
                continue
            gain = float(module.params.get(f"gain{idx}", 1.0))
            out += (buf * gain).astype(np.float32)
        return (out * master).astype(np.float32)
