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

import queue
import threading
import wave
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
        # MIDIInput modules currently owned by this backend (module_id →
        # instance). Tracked separately from ``_state`` because the MIDI
        # port lives on the module instance and needs explicit teardown
        # when the module leaves the patch or the backend stops.
        self._midi_inputs: dict[int, Any] = {}
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
                drop = (
                    mid not in live_types
                    or self._state_types.get(mid) != live_types[mid]
                )
                if drop:
                    # If this state belongs to a disk_writer, close
                    # its file/thread before discarding it — otherwise
                    # the worker would leak across recompiles.
                    if self._state_types.get(mid) == "disk_writer":
                        self._close_disk_writer_state(self._state[mid])
                    self._state.pop(mid, None)
                    if mid not in live_types:
                        self._state_types.pop(mid, None)
            # Record the current type for every live module so the next
            # compile can compare against it.
            self._state_types = dict(live_types)

            # MIDIInput lifecycle: ensure every midi_input module in the new
            # patch has its mido port open, and close ports for any
            # midi_input modules that left the patch since last compile.
            new_midi_ids = {
                mid for mid, m in patch.modules.items() if m.TYPE == "midi_input"
            }
            for mid in list(self._midi_inputs.keys()):
                if mid not in new_midi_ids:
                    try:
                        self._midi_inputs[mid].stop_midi()
                    except Exception:
                        pass
                    del self._midi_inputs[mid]
            for mid in new_midi_ids:
                module = patch.modules[mid]
                prev = self._midi_inputs.get(mid)
                # If the id maps to a different instance now (patch swap),
                # close the previous one before tracking the new one.
                if prev is not None and prev is not module:
                    try:
                        prev.stop_midi()
                    except Exception:
                        pass
                self._midi_inputs[mid] = module
                # ``start_midi`` is idempotent and re-opens on device change.
                try:
                    module.start_midi()
                except Exception as e:
                    # Don't let a flaky MIDI stack break compile() — the
                    # module logs internally and renders silence.
                    import logging
                    logging.getLogger(__name__).warning(
                        "MIDIInput %s start failed: %s", mid, e
                    )

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
        # Close any active disk writers so their WAV headers get
        # finalized when the user hits Stop on the transport.
        for mid in list(self._state.keys()):
            if self._state_types.get(mid) == "disk_writer":
                self._close_disk_writer_state(self._state[mid])
        # Close any open MIDI ports so the next start() reopens cleanly.
        # The module instances stay alive (they're owned by the patch),
        # so the next compile() will reopen the port via start_midi().
        for mid, module in list(self._midi_inputs.items()):
            try:
                module.stop_midi()
            except Exception:
                pass

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
            return self._render_oscillator(module, frames, buffers, patch)
        if module.TYPE == "keyboard":
            return self._render_keyboard(module, frames)
        if module.TYPE == "midi_input":
            return self._render_midi_input(module, frames)
        if module.TYPE == "filter":
            return self._render_filter(module, frames, buffers, patch)
        if module.TYPE == "adsr":
            return self._render_adsr(module, frames, buffers, patch)
        if module.TYPE == "vca":
            return self._render_vca(module, frames, buffers, patch)
        if module.TYPE == "lfo":
            return self._render_lfo(module, frames, buffers, patch)
        if module.TYPE == "mixer":
            return self._render_mixer(module, frames, buffers, patch)
        if module.TYPE == "combiner":
            return self._render_combiner(module, frames, buffers, patch)
        if module.TYPE == "cv_combiner":
            return self._render_cv_combiner(module, frames, buffers, patch)
        if module.TYPE == "crossover":
            return self._render_crossover(module, frames, buffers, patch)
        if module.TYPE == "disk_writer":
            return self._render_disk_writer(module, frames, buffers, patch)
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

    def _render_oscillator(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Audio-rate oscillator with optional per-sample CV modulation.

        Two CV inputs (both optional):
          - ``freq_cv`` follows 1V/octave convention: the effective
            frequency for sample n is ``freq * 2 ** cv[n]``. Per-sample
            evaluation makes this true FM/vibrato — phase is integrated
            from the instantaneous frequency, not a block-rate scalar.
          - ``amp_cv`` is linear multiplicative: ``amp * cv[n]``. A
            unipolar LFO here gives AM; bipolar would invert phase.
        """
        state = self._state.setdefault(module.id, {"phase": 0.0})
        freq = float(module.params.get("freq", 440.0))
        amp = float(module.params.get("amp", 0.5))
        waveform = str(module.params.get("waveform", "sine"))
        sr = self.sample_rate

        # CV lookups are only available when the renderer is called
        # via the topo walk (which always passes buffers + patch).
        # Tests that drive the oscillator in isolation pass None.
        if buffers is None or patch is None:
            freq_cv = None
            amp_cv = None
        else:
            freq_cv = self._input_buffer(patch, buffers, module.id, "freq_cv")
            amp_cv = self._input_buffer(patch, buffers, module.id, "amp_cv")

        start_phase = state["phase"]
        if freq_cv is None:
            # Fast path: constant frequency, vectorized phase ramp.
            phase_inc = freq / sr
            phases = (start_phase + np.arange(frames, dtype=np.float64) * phase_inc) % 1.0
            state["phase"] = (start_phase + frames * phase_inc) % 1.0
        else:
            # Per-sample frequency from CV. Integrate phase one sample at
            # a time — cheap in numpy via cumsum of per-sample increments.
            inst_freq = freq * np.power(2.0, freq_cv.astype(np.float64))
            inst_inc = inst_freq / sr
            # The first sample uses start_phase; subsequent samples add
            # the per-sample increment. ``cumsum`` gives the running total.
            phases = (start_phase + np.cumsum(inst_inc)) % 1.0
            state["phase"] = float(phases[-1])

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

        wave = wave * amp
        if amp_cv is not None:
            wave = wave * amp_cv.astype(np.float64)

        return wave.astype(np.float32)


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

    # ----- MIDI input rendering ------------------------------------------

    def _render_midi_input(self, module, frames: int) -> dict[str, np.ndarray]:
        """MIDI-driven self-polyphonic voice renderer.

        Mirrors ``_render_keyboard`` exactly in structure — same per-voice
        phase tracking, same short attack/release ramps to kill clicks,
        same global gate semantics. The only differences are:

          * ``snapshot_active_notes()`` returns ``{note: velocity}`` not a
            set, so we can scale each voice by its note-on velocity.
          * When ``velocity_sensitive`` is False, velocities are ignored
            and every voice plays at gain 1.0 — useful when a controller
            has a poor velocity curve, or for organ-style untouched-
            dynamics patches.

        Voice routing into per-voice downstream chains (the v0.4 voice
        manager) is deliberately not the job of this renderer; chord
        summing happens here and the rest of the patch sees one mono
        stream.
        """
        state = self._state.setdefault(module.id, {"voices": {}})
        voices: dict[int, dict] = state["voices"]

        sr = self.sample_rate
        waveform = str(module.params.get("waveform", "sine"))
        volume = float(module.params.get("volume", 0.5))
        velocity_sensitive = bool(module.params.get("velocity_sensitive", True))

        active = module.snapshot_active_notes()  # dict[int, float]

        for note, vel in active.items():
            voice = voices.get(note)
            if voice is None:
                voices[note] = {
                    "phase": 0.0,
                    "env": 0.0,
                    "releasing": False,
                    "velocity": vel,
                }
            else:
                voice["releasing"] = False
                # Update velocity on retrigger so the next attack uses the
                # newer note-on dynamics. The release tail will keep the
                # old velocity, which is correct musically.
                voice["velocity"] = vel
        for note in list(voices):
            if note not in active:
                voices[note]["releasing"] = True

        attack_samples = max(1, int(self._KB_ATTACK_S * sr))
        release_samples = max(1, int(self._KB_RELEASE_S * sr))

        # Pitch wheel: 1V/oct CV value applied identically to every
        # internal voice, and emitted on the pitch_cv output port for
        # downstream consumers. Block-constant - the wheel moves much
        # more slowly than the audio block rate (~12 ms at 512 samples /
        # 44.1 kHz), so per-sample smoothing isn't audibly useful here.
        pitch_bend = float(module.snapshot_pitch_bend())
        bend_range = float(module.params.get("bend_range", 2.0))
        pitch_cv_value = pitch_bend * bend_range / 12.0
        freq_multiplier = float(2.0 ** pitch_cv_value)

        # Mod wheel: unipolar [0, 1] times mod_scale. Linear emission -
        # consumers like cutoff_cv apply their own 2**cv shaping, amp_cv
        # is linear, etc. We don't bake any shaping in here.
        mod_wheel = float(module.snapshot_mod_wheel())
        mod_scale = float(module.params.get("mod_scale", 1.0))
        mod_cv_value = mod_wheel * mod_scale

        # Channel aftertouch: same emission shape as mod wheel. Unipolar
        # [0, 1] times pressure_scale. Channel-pressure only -- one
        # value per channel, applied identically to all held voices.
        aftertouch = float(module.snapshot_aftertouch())
        pressure_scale = float(module.params.get("pressure_scale", 1.0))
        pressure_cv_value = aftertouch * pressure_scale

        audio = np.zeros(frames, dtype=np.float32)

        for note, voice in list(voices.items()):
            freq = midi_to_freq(note) * freq_multiplier
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

            # Velocity scales the voice gain. Always set in voice state;
            # the param decides whether it's actually applied.
            gain = float(voice["velocity"]) if velocity_sensitive else 1.0
            audio += (wave * env_ramp * gain).astype(np.float32)

            if voice["releasing"] and voice["env"] <= 1e-5:
                del voices[note]

        audio *= volume

        gate_value = 1.0 if active else 0.0
        gate = np.full(frames, gate_value, dtype=np.float32)
        pitch_cv = np.full(frames, pitch_cv_value, dtype=np.float32)
        mod_cv = np.full(frames, mod_cv_value, dtype=np.float32)
        pressure_cv = np.full(frames, pressure_cv_value, dtype=np.float32)

        return {
            "out": audio,
            "gate": gate,
            "pitch_cv": pitch_cv,
            "mod_cv": mod_cv,
            "pressure_cv": pressure_cv,
        }

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

        # CV-modulate the cutoff. 1V/octave: ``cutoff *= 2 ** mean(cv)``.
        # Block-mean keeps the biquad coefficient recomputation to one
        # pass per block; audio-rate cutoff mod would need per-sample
        # coefs (~9x cost in this tight scalar loop).
        cutoff_cv = self._input_buffer(patch, buffers, module.id, "cutoff_cv")
        if cutoff_cv is not None and cutoff_cv.size > 0:
            cutoff = cutoff * float(2.0 ** float(np.mean(cutoff_cv)))

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

    def _render_lfo(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
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
        # CV-modulate the rate: 1V/octave, block-mean.
        # ``buffers``/``patch`` are None when called from unit tests in
        # isolation, in which case rate_cv is unavailable — same back-
        # compat trick we use on _render_oscillator.
        if buffers is not None and patch is not None:
            rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
            if rate_cv is not None and rate_cv.size > 0:
                rate = rate * float(2.0 ** float(np.mean(rate_cv)))

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

    # ----- Combiner rendering ---------------------------------------------

    def _render_combiner(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sum up to four audio inputs at unit gain. Unconnected = silence."""
        out = np.zeros(frames, dtype=np.float32)
        for idx in (1, 2, 3, 4):
            buf = self._input_buffer(patch, buffers, module.id, f"in{idx}")
            if buf is None:
                continue
            out += buf.astype(np.float32)
        return out

    # ----- CVCombiner rendering -------------------------------------------

    def _render_cv_combiner(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Combine up to four CV signals into one.

        ``mode="sum"`` (default) is the analog-modular convention — stacks
        adding linearly. ``mode="average"`` divides by the *connected*
        input count so blending modulators doesn't double the depth.
        """
        mode = str(module.params.get("mode", "sum"))
        out = np.zeros(frames, dtype=np.float32)
        count = 0
        for idx in (1, 2, 3, 4):
            buf = self._input_buffer(patch, buffers, module.id, f"in{idx}")
            if buf is None:
                continue
            out += buf.astype(np.float32)
            count += 1
        if mode == "average" and count > 0:
            out /= float(count)
        return out

    # ----- Crossover rendering --------------------------------------------

    def _render_crossover(self, module, frames: int, buffers, patch) -> dict:
        """Linkwitz-Riley 4th-order two-way split: low + high outputs.

        Two cascaded Butterworth (Q=1/√2) biquads per branch. The shared
        ``a`` denominator and the LP/HP numerators are the standard RBJ
        cookbook coefficients; running them in series gives the LR4
        magnitude response (-24 dB/oct, -6 dB at corner) and the phase
        relationship that lets low+high sum back to a flat magnitude.
        """
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            zero = np.zeros(frames, dtype=np.float32)
            return {"low": zero, "high": zero.copy()}

        state = self._state.setdefault(
            module.id,
            {
                "lp1_x1": 0.0, "lp1_x2": 0.0, "lp1_y1": 0.0, "lp1_y2": 0.0,
                "lp2_x1": 0.0, "lp2_x2": 0.0, "lp2_y1": 0.0, "lp2_y2": 0.0,
                "hp1_x1": 0.0, "hp1_x2": 0.0, "hp1_y1": 0.0, "hp1_y2": 0.0,
                "hp2_x1": 0.0, "hp2_x2": 0.0, "hp2_y1": 0.0, "hp2_y2": 0.0,
            },
        )

        sr = self.sample_rate
        freq = float(module.params.get("frequency", 1000.0))
        freq = max(20.0, min(freq, sr * 0.45))
        q = 1.0 / (2.0 ** 0.5)  # Butterworth -> Q ≈ 0.7071

        w0 = 2.0 * np.pi * freq / sr
        cos_w0 = float(np.cos(w0))
        sin_w0 = float(np.sin(w0))
        alpha = sin_w0 / (2.0 * q)
        a0 = 1.0 + alpha
        a1n = (-2.0 * cos_w0) / a0
        a2n = (1.0 - alpha) / a0

        lp_b0 = ((1.0 - cos_w0) / 2.0) / a0
        lp_b1 = (1.0 - cos_w0) / a0
        lp_b2 = ((1.0 - cos_w0) / 2.0) / a0
        hp_b0 = ((1.0 + cos_w0) / 2.0) / a0
        hp_b1 = (-(1.0 + cos_w0)) / a0
        hp_b2 = ((1.0 + cos_w0) / 2.0) / a0

        low = np.empty(frames, dtype=np.float32)
        high = np.empty(frames, dtype=np.float32)

        lp1_x1 = state["lp1_x1"]; lp1_x2 = state["lp1_x2"]
        lp1_y1 = state["lp1_y1"]; lp1_y2 = state["lp1_y2"]
        lp2_x1 = state["lp2_x1"]; lp2_x2 = state["lp2_x2"]
        lp2_y1 = state["lp2_y1"]; lp2_y2 = state["lp2_y2"]
        hp1_x1 = state["hp1_x1"]; hp1_x2 = state["hp1_x2"]
        hp1_y1 = state["hp1_y1"]; hp1_y2 = state["hp1_y2"]
        hp2_x1 = state["hp2_x1"]; hp2_x2 = state["hp2_x2"]
        hp2_y1 = state["hp2_y1"]; hp2_y2 = state["hp2_y2"]

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

        state["lp1_x1"] = lp1_x1; state["lp1_x2"] = lp1_x2
        state["lp1_y1"] = lp1_y1; state["lp1_y2"] = lp1_y2
        state["lp2_x1"] = lp2_x1; state["lp2_x2"] = lp2_x2
        state["lp2_y1"] = lp2_y1; state["lp2_y2"] = lp2_y2
        state["hp1_x1"] = hp1_x1; state["hp1_x2"] = hp1_x2
        state["hp1_y1"] = hp1_y1; state["hp1_y2"] = hp1_y2
        state["hp2_x1"] = hp2_x1; state["hp2_x2"] = hp2_x2
        state["hp2_y1"] = hp2_y1; state["hp2_y2"] = hp2_y2

        return {"low": low, "high": high}

    # ----- DiskWriter rendering -------------------------------------------

    def _render_disk_writer(self, module, frames: int, buffers, patch):
        """Enqueue blocks of audio for the worker thread to write to disk."""
        state = self._state.setdefault(
            module.id,
            {
                "queue": None,
                "thread": None,
                "stop_event": None,
                "path": None,
                "dropped_blocks": 0,
            },
        )

        armed = bool(module.params.get("armed", True))
        if not armed:
            # Tear down so re-arming starts a fresh take with a fresh file.
            self._close_disk_writer_state(state)
            return None

        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            return None

        path = str(module.params.get("path", "recording.wav"))
        if state["queue"] is None or state["path"] != path:
            # First arrival, or path changed — (re)start the writer.
            self._close_disk_writer_state(state)
            state["path"] = path
            state["queue"] = queue.Queue(maxsize=64)
            state["stop_event"] = threading.Event()
            t = threading.Thread(
                target=self._disk_writer_worker,
                args=(state["queue"], state["stop_event"], path, self.sample_rate),
                daemon=True,
                name=f"DiskWriter-{module.id}",
            )
            state["thread"] = t
            t.start()

        # Non-blocking enqueue. Drop on backlog rather than glitch the
        # audio thread; bumped counter is visible in tests / debug.
        try:
            state["queue"].put_nowait(src.astype(np.float32).copy())
        except queue.Full:
            state["dropped_blocks"] += 1

        return None  # sink

    def _close_disk_writer_state(self, state) -> None:
        """Signal the writer thread to drain and join. Idempotent."""
        ev = state.get("stop_event")
        if ev is not None:
            ev.set()
        t = state.get("thread")
        if t is not None:
            t.join(timeout=2.0)
        state["queue"] = None
        state["thread"] = None
        state["stop_event"] = None
        state["path"] = None

    @staticmethod
    def _disk_writer_worker(q, stop_event, path, sample_rate) -> None:
        """Write queued blocks to a mono 16-bit WAV until stop is set.

        On stop we drain anything still in the queue before closing so
        the final block of a take always lands.
        """
        try:
            wf = wave.open(path, "wb")
        except Exception as exc:  # pragma: no cover - filesystem-specific
            print(f"[DiskWriter] cannot open {path}: {exc}")
            return
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(int(sample_rate))
        try:
            while not stop_event.is_set() or not q.empty():
                try:
                    block = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                clipped = np.clip(block, -1.0, 1.0)
                ints = (clipped * 32767.0).astype(np.int16)
                wf.writeframes(ints.tobytes())
        finally:
            wf.close()
