"""I/O renderers: the note sources and the patch's ins and outs.

Keyboard, CV keyboard, CV gates, key trigger, MIDI input, mic input,
file player, scope, meter and disk writer, moved verbatim out of
``numpy_backend.py`` (2026-09-28) into a mixin that ``NumpyBackend``
inherits -- see docs/architecture.md, "Renderer families". The file
player's UI hooks (rewind / seek / finished / failed / decode_gen,
``wait_for_file_decodes``) and ``scope_window`` came along; the UI still
reaches them on the backend instance. So did ``_resolve_mic_input``,
``_start_file_decoder`` and ``_close_disk_writer_state``, which the
engine's ``start`` / ``compile`` / ``stop`` call through ``self``.

Shared helpers stay in the backend and are reached through ``self``:
``_osc_waveshape`` (keyboard / MIDI voices), ``_MAX_VOICES``, the meter's
``_meter_k_coeffs`` / ``_loud_shelf`` / ``_filter_coeffs`` biquads,
``_resolve_media_path``, ``_load_wav``, ``_disk_writer_worker`` and
``_voice_sum``. ``_resolve_mic_input`` reads the backend's optional
``sd`` (sounddevice) through a lazy import, so a test that swaps
``numpy_backend.sd`` still reaches it (``numpy_backend`` imports this
module, so a top-level import would be circular).
"""
from __future__ import annotations

import math
import queue
import threading

import numpy as np
from scipy.signal import lfilter

from ...modules.cv_gates import KEY_CV_NAMES
from ...modules.cv_keyboard import CV_REFERENCE_NOTE, KEY_GATE_NAMES
from ...modules.keyboard import midi_to_freq
from .. import media


class IORenderers:
    # ----- keyboard rendering ---------------------------------------------

    _KB_ATTACK_S = 0.005
    _KB_RELEASE_S = 0.020

    def _render_keyboard(self, module, frames: int) -> dict[str, np.ndarray]:
        """Voice-aware keyboard renderer.

        Slice 4 mirror of :meth:`_render_midi_input`. Emits per-slot
        ``(MAX_VOICES, frames)`` buffers on ``out`` and ``gate``;
        downstream voice-aware modules carry the per-voice identity
        through to the speaker, where the implicit sum at the mono
        sink mixes them back to stereo. Un-migrated mono consumers
        see a collapsed-to-1D view via ``_input_buffer``'s default
        ``collapse=True`` (the sum-on-fetch path established in slice
        2), so older patches still work without changes.

        Differences from :meth:`_render_midi_input` are all in what
        Keyboard *doesn't* have:

          * No velocity. Every voice plays at unit gain (the velocity
            param exists on MIDIInput because hardware sends it; a
            computer keyboard has no way to express it).
          * No pitch bend. ``freq = midi_to_freq(note)`` directly, no
            ``freq_multiplier`` knob.
          * No mod wheel, no aftertouch, no sustain pedal. Keyboard's
            port set stays at ``out`` + ``gate`` only.

        Per-slot state lives in ``self._state[module.id]`` as numpy
        arrays indexed by slot, same shape as MIDIInput's:

          * ``phase``     -- oscillator phase, [0, 1).
          * ``env``       -- per-slot attack/release ramp level, [0, 1].
          * ``last_note`` -- the MIDI note this slot was rendering on
                            the previous block; resets phase + env on
                            slot reassignment so the new voice starts
                            cleanly rather than picking up the previous
                            voice's phase mid-cycle.
          * ``releasing`` -- bool: True while the slot is ramping its
                            envelope down. Latched on the gate-fall
                            edge, cleared on gate-rise (retrigger
                            before the tail finished).

        Silent slots (``note == -1``) write zeros and reset their
        state so the next allocation starts clean.

        Gate semantics shift from the pre-slice-4 "global block-
        constant" model to per-voice block-constant: a chord with
        notes in slots 0 and 5 raises ``gate[0]`` and ``gate[5]``
        independently, with no interaction. A subsequent note_on for
        a third note rises a new gate edge in its own slot, leaving
        the existing slots' gate values unchanged. This is the
        polyphonic behaviour an ADSR per voice needs to fire one
        envelope per note rather than retriggering on every chord
        change.
        """
        state = self._state.setdefault(
            module.id,
            {
                "phase": np.zeros(self._MAX_VOICES, dtype=np.float64),
                "env": np.zeros(self._MAX_VOICES, dtype=np.float64),
                "last_note": np.full(self._MAX_VOICES, -1, dtype=np.int32),
                "releasing": np.zeros(self._MAX_VOICES, dtype=bool),
            },
        )

        sr = self.sample_rate
        waveform = str(module.params.get("waveform", "sine"))
        amp = float(module.params.get("amp", 0.5))

        slots = module.snapshot_voice_slots()  # length _MAX_VOICES

        attack_samples = max(1, int(self._KB_ATTACK_S * sr))
        release_samples = max(1, int(self._KB_RELEASE_S * sr))

        audio = np.zeros((self._MAX_VOICES, frames), dtype=np.float32)
        gate = np.zeros((self._MAX_VOICES, frames), dtype=np.float32)

        for i, slot in enumerate(slots):
            note = int(slot["note"])
            if note == -1:
                # Slot empty -- reset state so the next allocation gets
                # a clean phase/env, and emit silence for this voice.
                state["phase"][i] = 0.0
                state["env"][i] = 0.0
                state["last_note"][i] = -1
                state["releasing"][i] = False
                continue

            # Detect slot reassignment: a new note was allocated into
            # this slot since the last block. Reset phase/env so the
            # new voice starts cleanly rather than picking up the
            # previous voice's phase mid-cycle.
            if int(state["last_note"][i]) != note:
                state["phase"][i] = 0.0
                state["env"][i] = 0.0
                state["releasing"][i] = False
                state["last_note"][i] = note

            gating = bool(slot["gating"])

            # Edge transitions on the gate.
            if not gating and not bool(state["releasing"][i]):
                # Falling edge: start the release ramp.
                state["releasing"][i] = True
            elif gating and bool(state["releasing"][i]):
                # Rising edge (retrigger before tail finished): cancel
                # the release. The env-step branch below picks attack
                # automatically because env < 1.
                state["releasing"][i] = False

            # Phase ramp for this voice.
            freq = midi_to_freq(note)
            phase_inc = freq / sr
            start_phase = float(state["phase"][i])
            phases = (
                start_phase + np.arange(frames, dtype=np.float64) * phase_inc
            ) % 1.0
            state["phase"][i] = (start_phase + frames * phase_inc) % 1.0

            # Waveform. Routed through the shared shaper so the note
            # sources get the same naive / PolyBLEP / wavetable shapes as
            # the Oscillator. dt is this voice's constant per-sample phase
            # increment.
            wave = self._osc_waveshape(phases, waveform, dt=phase_inc)

            # Envelope ramp -- same short attack/release as the old
            # mono renderer, just per-slot now.
            env_start = float(state["env"][i])
            if bool(state["releasing"][i]):
                delta = -1.0 / release_samples
            elif env_start < 1.0:
                delta = 1.0 / attack_samples
            else:
                delta = 0.0
            env_ramp = env_start + np.arange(frames, dtype=np.float64) * delta
            np.clip(env_ramp, 0.0, 1.0, out=env_ramp)
            state["env"][i] = float(
                max(0.0, min(1.0, env_start + frames * delta))
            )

            audio[i] = (wave * env_ramp).astype(np.float32)

            # Gate: block-constant per slot. A within-block falling
            # edge produces a one-block delay before the gate drops,
            # matching the MIDIInput renderer's behaviour and the
            # pre-slice keyboard's per-block gate granularity.
            gate[i] = 1.0 if gating else 0.0

        audio *= amp

        return {"out": audio, "gate": gate}

    # ----- CV keyboard rendering ------------------------------------------

    def _render_cv_keyboard(self, module, frames: int) -> dict[str, np.ndarray]:
        """Voice-aware CV/gate controller renderer.

        Emits per-slot ``pitch_cv`` + ``gate`` of shape
        ``(MAX_VOICES, frames)``, plus twelve mono per-pitch-class gate
        jacks ``key_c`` .. ``key_b``. Unlike :meth:`_render_keyboard`,
        CVKeyboard has no audio, no envelope, and no phase/last_note
        state -- it is a controller, so the voice is built downstream.

        ``pitch_cv`` is 1V/octave with C4 (MIDI 60) = 0 V. It is held for
        any non-empty slot, including a released-but-still-tailing voice,
        so a downstream ADSR release stays in tune; it only returns to 0
        when the slot is reused (note == -1). ``gate[i]`` is high while
        voice slot ``i`` is physically held (block-constant, like the
        Keyboard/MIDIInput renderers). Each ``key_*`` jack is high while
        any held voice is that pitch class (octave-folded), and stays 1D
        because it is a channel-wide boolean.
        """
        slots = module.snapshot_voice_slots()  # length _MAX_VOICES

        pitch_cv = np.zeros((self._MAX_VOICES, frames), dtype=np.float32)
        gate = np.zeros((self._MAX_VOICES, frames), dtype=np.float32)
        # Per-pitch-class "any held voice is this pc" flags, index 0 == C.
        pc_on = [False] * 12

        for i, slot in enumerate(slots):
            note = int(slot["note"])
            if note == -1:
                continue
            # 1V/oct, held through the release tail until the slot reuses.
            pitch_cv[i, :] = (note - CV_REFERENCE_NOTE) / 12.0
            if bool(slot["gating"]):
                gate[i, :] = 1.0
                pc_on[note % 12] = True

        result: dict[str, np.ndarray] = {"pitch_cv": pitch_cv, "gate": gate}
        for pc, name in enumerate(KEY_GATE_NAMES):
            result[name] = (
                np.ones(frames, dtype=np.float32)
                if pc_on[pc]
                else np.zeros(frames, dtype=np.float32)
            )
        return result

    def _render_cv_gates(self, module, frames: int) -> dict[str, np.ndarray]:
        """Per-key ADSR bank for the CVGates controller.

        Each of the 17 keys drives an independent attack/decay/sustain/
        release state machine that shares the module's four envelope params.
        The held state is block-constant (snapshotted once per block, like
        every other keyboard renderer), so an envelope's rising/falling edge
        lands on the first sample of the block in which the key changed --
        identical to patching a keyboard gate into a standalone ADSR. Keys
        that are up, idle, and already at 0 short-circuit to a fresh zero
        buffer without running the per-sample loop, so a bank with two keys
        held costs two envelopes, not seventeen.

        Output: one mono ``(frames,)`` cv buffer per key, keyed by jack name.
        """
        down = module.snapshot_down()  # length NUM_KEYS

        sr = self.sample_rate
        attack_s = max(0.0, float(module.params.get("attack", 0.01)))
        decay_s = max(0.0, float(module.params.get("decay", 0.10)))
        sustain = max(0.0, min(1.0, float(module.params.get("sustain", 0.80))))
        release_s = max(0.0, float(module.params.get("release", 0.30)))

        attack_step = 1.0 / max(1.0, attack_s * sr)
        decay_step = (1.0 - sustain) / max(1.0, decay_s * sr)
        release_samples = max(1.0, release_s * sr)

        # Per-key envelope state, keyed by module id. One dict per key with
        # the same fields the mono ADSR uses. Rebuilt if the slot count ever
        # mismatches (defensive against a stale/foreign state shape from a
        # previous compile).
        state = self._state.setdefault(module.id, {})
        keys = state.get("keys")
        if not isinstance(keys, list) or len(keys) != len(down):
            keys = [
                {"phase": "idle", "level": 0.0, "prev_gate": False,
                 "release_step": 0.0}
                for _ in range(len(down))
            ]
            state["keys"] = keys

        result: dict[str, np.ndarray] = {}
        for i, name in enumerate(KEY_CV_NAMES):
            ks = keys[i]
            gate_high = bool(down[i])
            # Fully idle key (up, at rest, level 0): skip the loop.
            if (
                not gate_high
                and not ks["prev_gate"]
                and ks["phase"] == "idle"
                and ks["level"] == 0.0
            ):
                result[name] = np.zeros(frames, dtype=np.float32)
                continue
            result[name] = self._adsr_key_block(
                ks, gate_high, frames,
                attack_step, decay_step, sustain, release_samples,
            )
        return result

    def _adsr_key_block(
        self, ks, gate_high, frames,
        attack_step, decay_step, sustain, release_samples,
    ):
        """Advance one key's ADSR over ``frames`` samples under a
        block-constant gate, mutating ``ks`` in place and returning a
        ``(frames,)`` float32 buffer.

        The state machine is identical to :meth:`_render_adsr_mono` (idle ->
        attack -> decay -> sustain -> release -> idle); the only difference
        is the gate is a single block-constant bool rather than a per-sample
        buffer, which is exactly how every keyboard gate already behaves. A
        release captured mid-attack still takes the full release window (no
        snap), and a key re-pressed mid-release attacks from its current
        level (no click).
        """
        out = np.empty(frames, dtype=np.float32)
        for n in range(frames):
            if gate_high and not ks["prev_gate"]:
                ks["phase"] = "attack"
            elif not gate_high and ks["prev_gate"]:
                ks["release_step"] = ks["level"] / release_samples
                ks["phase"] = "release"
            ks["prev_gate"] = gate_high

            phase = ks["phase"]
            level = ks["level"]
            if phase == "attack":
                level += attack_step
                if level >= 1.0:
                    level = 1.0
                    ks["phase"] = "decay"
            elif phase == "decay":
                level -= decay_step
                if level <= sustain:
                    level = sustain
                    ks["phase"] = "sustain"
            elif phase == "sustain":
                level = sustain
            elif phase == "release":
                level -= ks["release_step"]
                if level <= 0.0:
                    level = 0.0
                    ks["phase"] = "idle"
            ks["level"] = level
            out[n] = level
        return out

    # ----- key_trigger ----------------------------------------------------

    # Trigger-mode pulse width: a fixed high period per press, long enough for
    # any downstream edge detector (schmitt / clock / sequencer / AD) to catch
    # reliably, carried across block boundaries so the length is block-size
    # independent. Floored at one sample.
    _KEY_TRIGGER_PULSE_SECONDS = 0.005

    def _render_key_trigger(self, module, frames: int) -> np.ndarray:
        """One bound key → a mono ``(frames,)`` gate signal in {0, 1}.

        ``gate``    high while the key is held (block-constant, like every
                    keyboard renderer — a key change lands on the first sample
                    of the next block).
        ``latch``   each press toggles the output, which then holds through
                    key-up until the next press (an even number of presses in
                    one block nets no change).
        ``trigger`` each press emits a fixed ~5 ms pulse from the block head,
                    carried across blocks so its length is block-size
                    independent; a merely-held key does not re-pulse.

        An unbound key never receives an event, so it idles at 0.
        """
        held, presses = module.snapshot()
        mode = str(module.params.get("mode", "gate"))
        out = np.zeros(frames, dtype=np.float32)

        if mode == "latch":
            state = self._state.setdefault(module.id, {})
            if presses & 1:  # an odd number of presses this block flips it
                state["latched"] = not state.get("latched", False)
            if state.get("latched", False):
                out[:] = 1.0
        elif mode == "trigger":
            state = self._state.setdefault(module.id, {})
            pulse = int(state.get("pulse", 0))
            if presses:
                # (Re)arm the pulse; a press restarts it from the block head.
                pulse = max(
                    1,
                    int(round(self.sample_rate * self._KEY_TRIGGER_PULSE_SECONDS)),
                )
            n = min(pulse, frames)
            if n > 0:
                out[:n] = 1.0
                pulse -= n
            state["pulse"] = pulse
        else:  # "gate" (default; also the fallback for an unknown mode)
            if held:
                out[:] = 1.0

        return out

    def _render_midi_input(self, module, frames: int) -> dict[str, np.ndarray]:
        """Voice-aware MIDI renderer.

        Emits per-slot audio, gate, and pitch_cv buffers of shape
        ``(_MAX_VOICES, frames)``. Mod-wheel and channel-aftertouch CV
        stay 1D ``(frames,)`` -- they're channel-wide by MIDI spec
        (one value per channel, applied identically to every voice),
        so they don't need a voice axis.

        Per-slot state lives in ``self._state[module.id]`` as numpy
        arrays indexed by slot:

          * ``phase``    -- oscillator phase, [0, 1).
          * ``env``      -- envelope ramp level, [0, 1].
          * ``last_note`` -- the MIDI note this slot was rendering on
                            the previous block; used to detect "slot
                            reassigned to a new note" and reset phase
                            + env on the boundary so the new voice
                            starts from zero rather than picking up
                            mid-cycle.
          * ``releasing`` -- bool: True while the slot is ramping its
                            envelope down. Latched on the gate-fall
                            edge, cleared on gate-rise (retrigger
                            before the tail finished).

        Silent slots (``note == -1``) write zeros and reset their state
        so the next allocation starts clean.

        Downstream consumers of these buffers fall into two camps. A
        mono consumer (any un-migrated stateful module, or the speaker
        sink) goes through ``_input_buffer`` with the default
        ``collapse=True``, which sums the voice axis on fetch -- net
        effect is identical to the pre-slice-1 self-summing MIDIInput.
        A voice-aware consumer (slice 3+) passes ``collapse=False`` and
        grows its own per-slot state.
        """
        state = self._state.setdefault(
            module.id,
            {
                "phase": np.zeros(self._MAX_VOICES, dtype=np.float64),
                "env": np.zeros(self._MAX_VOICES, dtype=np.float64),
                "last_note": np.full(self._MAX_VOICES, -1, dtype=np.int32),
                "releasing": np.zeros(self._MAX_VOICES, dtype=bool),
            },
        )

        sr = self.sample_rate
        waveform = str(module.params.get("waveform", "sine"))
        amp = float(module.params.get("amp", 0.5))
        velocity_sensitive = bool(module.params.get("velocity_sensitive", True))

        slots = module.snapshot_voice_slots()  # length _MAX_VOICES

        # Channel-wide (mono) modulation values. Pitch bend is applied
        # to every voice identically here; when polyphonic pitch bend
        # lands later, this becomes a per-slot value but the buffer
        # shape doesn't need to change because pitch_cv is ALREADY
        # (V, frames) -- only the values per row change.
        pitch_bend = float(module.snapshot_pitch_bend())
        bend_range = float(module.params.get("bend_range", 2.0))
        pitch_cv_value = pitch_bend * bend_range / 12.0
        freq_multiplier = float(2.0 ** pitch_cv_value)

        mod_wheel = float(module.snapshot_mod_wheel())
        mod_scale = float(module.params.get("mod_scale", 1.0))
        mod_cv_value = mod_wheel * mod_scale

        aftertouch = float(module.snapshot_aftertouch())
        pressure_scale = float(module.params.get("pressure_scale", 1.0))
        pressure_cv_value = aftertouch * pressure_scale

        attack_samples = max(1, int(self._KB_ATTACK_S * sr))
        release_samples = max(1, int(self._KB_RELEASE_S * sr))

        # Output buffers. Audio + gate are per-slot; pitch_cv is
        # per-slot for shape-stability with future polyphonic bend.
        audio = np.zeros((self._MAX_VOICES, frames), dtype=np.float32)
        gate = np.zeros((self._MAX_VOICES, frames), dtype=np.float32)
        pitch_cv = np.full(
            (self._MAX_VOICES, frames), pitch_cv_value, dtype=np.float32
        )
        # Per-slot velocity, block-constant, 0 for an empty slot. Always
        # the real velocity: `velocity_sensitive` is about the built-in
        # tone, and a consumer that wants it flat can leave this unpatched.
        velocity_cv = np.zeros((self._MAX_VOICES, frames), dtype=np.float32)

        for i, slot in enumerate(slots):
            note = int(slot["note"])
            if note == -1:
                # Slot empty -- reset state so the next allocation gets
                # a clean phase/env, and emit silence for this voice.
                state["phase"][i] = 0.0
                state["env"][i] = 0.0
                state["last_note"][i] = -1
                state["releasing"][i] = False
                # gate[i] and audio[i] are already zero from np.zeros.
                continue

            # Detect slot reassignment: a new note was allocated into
            # this slot since the last block. Reset phase/env so the
            # new voice starts cleanly rather than picking up the
            # previous voice's phase mid-cycle.
            if int(state["last_note"][i]) != note:
                state["phase"][i] = 0.0
                state["env"][i] = 0.0
                state["releasing"][i] = False
                state["last_note"][i] = note

            gating = bool(slot["gating"])

            # Edge transitions on the gate.
            if not gating and not bool(state["releasing"][i]):
                # Falling edge: start the release ramp.
                state["releasing"][i] = True
            elif gating and bool(state["releasing"][i]):
                # Rising edge (retrigger before tail finished): cancel
                # the release and resume attacking from the current
                # env level. The env-step branch below picks attack
                # automatically because env < 1.
                state["releasing"][i] = False

            # Phase ramp for this voice.
            freq = midi_to_freq(note) * freq_multiplier
            phase_inc = freq / sr
            start_phase = float(state["phase"][i])
            phases = (
                start_phase + np.arange(frames, dtype=np.float64) * phase_inc
            ) % 1.0
            state["phase"][i] = (start_phase + frames * phase_inc) % 1.0

            # Waveform. Routed through the shared shaper so the note
            # sources get the same naive / PolyBLEP / wavetable shapes as
            # the Oscillator. dt is this voice's constant per-sample phase
            # increment.
            wave = self._osc_waveshape(phases, waveform, dt=phase_inc)

            # Envelope ramp -- same short attack/release as the old
            # mono renderer, just per-slot now.
            env_start = float(state["env"][i])
            if bool(state["releasing"][i]):
                delta = -1.0 / release_samples
            elif env_start < 1.0:
                delta = 1.0 / attack_samples
            else:
                delta = 0.0
            env_ramp = env_start + np.arange(frames, dtype=np.float64) * delta
            np.clip(env_ramp, 0.0, 1.0, out=env_ramp)
            state["env"][i] = float(
                max(0.0, min(1.0, env_start + frames * delta))
            )

            # Velocity gain. Always present in slot state; the
            # velocity_sensitive param decides whether to apply it.
            gain = float(slot["velocity"]) if velocity_sensitive else 1.0
            velocity_cv[i] = float(slot["velocity"])

            audio[i] = (wave * env_ramp * gain).astype(np.float32)

            # Gate: block-constant per slot. A within-block falling
            # edge produces a one-block delay before the gate drops,
            # which is the same behavior the pre-slice mono renderer
            # had (the global gate was also block-constant).
            gate[i] = 1.0 if gating else 0.0

        audio *= amp

        # Channel-wide CV: stay 1D since they apply identically to
        # every voice.
        mod_cv = np.full(frames, mod_cv_value, dtype=np.float32)
        pressure_cv = np.full(frames, pressure_cv_value, dtype=np.float32)

        return {
            "out": audio,
            "gate": gate,
            "pitch_cv": pitch_cv,
            "mod_cv": mod_cv,
            "pressure_cv": pressure_cv,
            "velocity_cv": velocity_cv,
        }

    def _render_mic_input(self, module, frames: int, buffers=None, patch=None):
        """Publish the latest captured input block as stereo audio.

        Reads ``self._input_block`` (set by the duplex callback). A
        2-channel device maps to left/right; a mono device duplicates to
        both. No input (output-only stream, or before the first capture)
        renders silence. A block shorter than ``frames`` is zero-padded,
        a longer one truncated, so a momentary size mismatch can never
        raise on the audio thread.
        """
        left = np.zeros(frames, dtype=np.float32)
        right = np.zeros(frames, dtype=np.float32)
        block = self._input_block
        if block is not None and getattr(block, "ndim", 0) == 2 and block.shape[0] > 0:
            n = min(frames, block.shape[0])
            if block.shape[1] >= 2:
                left[:n] = block[:n, 0]
                right[:n] = block[:n, 1]
            else:
                mono = block[:n, 0]
                left[:n] = mono
                right[:n] = mono
            gain = float(module.params.get("gain", 1.0))
            if gain != 1.0:
                left *= gain
                right *= gain
        return {"left": left, "right": right}

    def _resolve_mic_input(self, module):
        """(device, in_channels) for opening the duplex input.

        ``device``: None for the system default (the ``""`` sentinel), or
        the device-name string passed straight to sounddevice. Channels
        are clamped to 1..2 from the device's reported input capability,
        defaulting to mono if the query fails.
        """
        dev_name = str(module.params.get("device", ""))
        in_device = None if dev_name == "" else dev_name
        in_channels = 1
        from ..numpy_backend import sd
        try:
            info = sd.query_devices(in_device, "input")
            in_channels = max(1, min(2, int(info.get("max_input_channels", 1))))
        except Exception:
            in_channels = 1
        return in_device, in_channels

    # Frames of decoded audio required before a still-decoding file starts
    # sounding from the top. Once the decoder reports ``done`` the gate is
    # moot (a fully decoded short file plays no matter how small it is).
    _FP_PREBUFFER_SECONDS = 0.5

    def _render_file_player(self, module, frames: int, buffers=None, patch=None):
        """Stream a background-decoded file to the ``left``/``right`` outs.

        The audio thread NEVER decodes: a ``media.StreamingDecoder`` is
        kicked off-thread at compile() (or here, non-blocking, after a live
        path edit) and this renderer only consumes what the worker has
        already published. Playback starts once ~0.5 s is buffered (or the
        decode has finished, whichever is first); if the playhead ever
        catches the decoder it holds — partial block, then silence — and
        resumes when more data lands, so nothing is skipped. ``loop`` wraps
        with modular indexing only once the total length is known; until
        then it plays linearly like a one-shot.

        Transport: ``armed`` False silences and parks at the start (the
        re-arm-replays contract); ``playing`` False holds the playhead
        where it is (tape-style pause); a pending ``seek`` (the node's
        Rewind button, via ``rewind_file_player``) is honoured whether
        playing or paused. Both ports are always returned (zeros when
        idle) so downstream wiring stays defined.
        """
        state = self._state.setdefault(
            module.id, {"path": None, "decoder": None, "pos": 0, "seek": None}
        )
        path = self._resolve_media_path(module.params.get("path", ""))
        if state.get("decoder") is None or state.get("path") != path:
            # First arrival, or a live path edit between compiles. Starting
            # a decoder is just a thread spawn — safe on the audio thread.
            old_dec = state.get("decoder")
            if old_dec is not None:
                old_dec.close()
            state["path"] = path
            new_dec = self._start_file_decoder(path)
            state["decoder"] = new_dec
            state["pos"] = 0
            state["seek"] = None
            if new_dec is not None:
                # Bump the decode "generation" — a monotonic identity the GUI
                # queue-advancer uses to tell a freshly (re)started decode from
                # the last one, so a bad file whose decode fails *between* two
                # UI polls is still skipped exactly once (a bool finished/failed
                # edge can be missed in that window). An empty path yields no
                # decoder, so an idle player's generation never ticks.
                state["decode_gen"] = state.get("decode_gen", 0) + 1

        left = np.zeros(frames, dtype=np.float32)
        right = np.zeros(frames, dtype=np.float32)
        silence = {"left": left, "right": right}

        decoder = state["decoder"]
        armed = bool(module.params.get("armed", True))
        if not armed or decoder is None or decoder.failed:
            if not armed:
                state["pos"] = 0  # re-arming replays from the top
                state["seek"] = None
            return silence

        # A rewind/seek request from the UI thread: consume it whether
        # playing or paused, so Rewind works as tape transport.
        seek = state.get("seek")
        if seek is not None:
            state["pos"] = int(seek)
            state["seek"] = None

        if not bool(module.params.get("playing", True)):
            return silence  # paused: hold the playhead, output silence

        ready = int(decoder.frames_ready)
        done = bool(decoder.done)
        pos = int(state["pos"])
        if ready == 0:
            return silence
        if pos == 0 and not done:
            # Prebuffer gate at the very start only — steady-state decode
            # outruns realtime by a wide margin, so mid-file underruns are
            # already the rare case and get the hold-and-resume treatment.
            if ready < int(self.sample_rate * self._FP_PREBUFFER_SECONDS):
                return silence

        samples = decoder.buffer
        gain = float(module.params.get("gain", 1.0))
        loop = bool(module.params.get("loop", False))

        if loop and done:
            n = int(decoder.total_frames)
            idx = (np.arange(frames) + pos) % n
            left[:] = samples[0, idx]
            right[:] = samples[1, idx]
            state["pos"] = (pos + frames) % n
        else:
            # Linear playback bounded by the decode watermark. Covers the
            # one-shot case (park at the end once done) and the
            # still-decoding case for both modes (hold at ``ready`` and
            # resume when more frames land; a loop wraps only once the
            # total is known).
            if pos < ready:
                take = min(frames, ready - pos)
                left[:take] = samples[0, pos:pos + take]
                right[:take] = samples[1, pos:pos + take]
                state["pos"] = pos + take
            # else: parked (one-shot done) or waiting on the decoder.

        if gain != 1.0:
            left *= gain
            right *= gain
        return {"left": left, "right": right}

    def _start_file_decoder(self, path):
        """Spawn a background decoder for ``path`` (None for an empty path)."""
        if not path:
            return None
        return media.StreamingDecoder(
            path, self.sample_rate, full_decode=self._load_wav
        )

    def rewind_file_player(self, module_id: int) -> None:
        """UI hook: seek a file_player back to 0:00 (playing or paused).

        Sets a flag the renderer consumes at the next block boundary, so
        the jump is block-aligned and thread-safe (one reference store,
        atomic under the GIL) — same pattern as ``reset_meter_clips``.
        """
        state = self._state.get(module_id)
        if state is not None and self._state_types.get(module_id) == "file_player":
            state["seek"] = 0

    def seek_file_player(self, module_id: int, fraction: float) -> None:
        """UI hook: seek a file_player to a fractional position in [0, 1].

        ``fraction`` is a 0..1 position along the *known* length — the true
        duration once decoded, or the buffered length while a long file is
        still streaming (the same quantity ``snapshot_file_positions``
        reports as ``total``, so the node's seek bar and its
        ``elapsed / total`` readout always agree). Like
        ``rewind_file_player`` it only stores a frame index the renderer
        consumes at the next block boundary (one reference store, atomic
        under the GIL), so scrubbing is block-aligned and lands whether the
        player is playing or paused. A no-op for an unknown id, a
        non-file_player, or a player with nothing decoded yet (there is
        nowhere to seek to).
        """
        if self._state_types.get(module_id) != "file_player":
            return
        state = self._state.get(module_id)
        if state is None:
            return
        decoder = state.get("decoder")
        if decoder is None or decoder.failed:
            return
        length = (
            int(decoder.total_frames) if decoder.done
            else int(decoder.frames_ready)
        )
        if length <= 0:
            return
        frac = min(1.0, max(0.0, float(fraction)))
        state["seek"] = int(frac * length)

    def file_player_finished(self, module_id: int) -> bool:
        """UI hook: True once a one-shot ``file_player`` has run off its end.

        The GUI polls this each frame to auto-advance a queued playlist — a
        False→True transition means the current track just finished, so the
        node should load its next queued file. Returns False for a looping
        player (its playhead wraps modulo the length and never lands past
        the total), an unarmed one (parked at 0), an empty/unreadable path
        (no decoder, or a failed one), or a track still mid-file or
        mid-decode.

        Reads audio-thread state without the lock: the int/bool reads are
        atomic under the GIL and a block-late answer only delays the queue
        poke by one block, which is inaudible.
        """
        if self._state_types.get(module_id) != "file_player":
            return False
        state = self._state.get(module_id)
        if not state:
            return False
        decoder = state.get("decoder")
        if decoder is None or decoder.failed or not decoder.done:
            return False
        total = int(decoder.total_frames)
        if total <= 0:
            return False
        # A one-shot parks at pos == total_frames; a loop wraps modulo total
        # so its pos is always < total; disarming resets pos to 0. So
        # ``pos >= total`` means exactly "a non-looping, armed track ran off
        # the end" without having to re-read the loop/armed params here.
        return int(state.get("pos", 0)) >= total

    def file_player_failed(self, module_id: int) -> bool:
        """UI hook: True once a ``file_player`` decode has terminally failed.

        A queued path that can't be decoded (missing, unreadable, or not
        audio) finishes as ``done`` **and** ``failed`` with zero frames. The
        GUI polls this alongside ``file_player_finished`` so a bad track
        auto-skips to the next queued file instead of stalling the playlist
        on it (a failed track never reports ``finished``, so without this the
        queue would stop dead). Returns False for an empty path (no decoder),
        a still-decoding one, or a healthy track.

        Lock-free like ``file_player_finished``: the bool reads are atomic
        under the GIL and a block-late answer only delays the skip one block.
        """
        if self._state_types.get(module_id) != "file_player":
            return False
        state = self._state.get(module_id)
        if not state:
            return False
        decoder = state.get("decoder")
        return bool(decoder is not None and decoder.done and decoder.failed)

    def file_player_decode_gen(self, module_id: int) -> int:
        """UI hook: a counter that ticks each time this ``file_player``
        (re)starts a real decode.

        The GUI queue-advancer keys its 'advance once per track' decision on
        this identity rather than on a raw finished/failed bool edge: a
        missing/unreadable queued file can finish decoding (as *failed*) in
        the gap between two UI polls, so the edge would be missed and the
        queue would stall — but the generation still differs from the track
        it's replacing, so the skip fires exactly once. Idle players (empty
        path → no decoder) stay at 0.
        """
        if self._state_types.get(module_id) != "file_player":
            return 0
        state = self._state.get(module_id)
        if not state:
            return 0
        return int(state.get("decode_gen", 0))

    def wait_for_file_decodes(self, timeout: float = 10.0) -> bool:
        """Block until every file_player decode finishes. Tests/offline only.

        Never call from the audio thread. Returns True when every decoder
        reached ``done`` without failing (an empty path counts as trivially
        ready, matching its render-silence contract).
        """
        import time as _time

        deadline = _time.monotonic() + float(timeout)
        ok = True
        for mid, st in list(self._state.items()):
            if self._state_types.get(mid) != "file_player":
                continue
            dec = st.get("decoder")
            if dec is None:
                continue
            remaining = max(0.0, deadline - _time.monotonic())
            ok = dec.wait(remaining) and ok
        return ok

    # Meter fall time: seconds for the peak bar to drop ~20 dB (a factor of
    # ten). The per-meter ``release`` param overrides this default. The fall
    # is derived from the block duration, so its wall-clock rate is the same
    # at any block size. Smaller = snappier / more reactive.
    _METER_RELEASE_DEFAULT = 0.4
    _METER_RELEASE_MIN = 0.02
    _METER_RELEASE_MAX = 4.0
    # Peak-hold tick: how long the tick sits at the most recent peak
    # before it starts to fall (at the ``release`` rate). DAW-style.
    _METER_HOLD_SEC = 1.5
    # Clip lamp: stays lit this long after any sample reaches 0 dBFS.
    _METER_CLIP_SEC = 2.0
    # RMS mode: EMA time constant of the mean-square average. ~300 ms
    # gives a VU-ish loudness ballistics.
    _METER_RMS_SEC = 0.3
    # LUFS-ish loudness modes: BS.1770-style K-weighting (2nd-order
    # highpass + high shelf, both plain RBJ biquads -- hence the -ish)
    # into a mean-square window, displayed as -0.691 + 10*log10(msq).
    # ``lufs_m`` is the 400 ms momentary window, ``lufs_s`` the 3 s
    # short-term one (EMA time constants, window-ish).
    _METER_K_HP_HZ = 38.0
    _METER_K_HP_Q = 0.5
    _METER_K_SHELF_HZ = 1681.0
    _METER_K_SHELF_DB = 4.0
    _METER_LUFS_OFFSET = -0.691
    _METER_LUFS_M_SEC = 0.4
    _METER_LUFS_S_SEC = 3.0

    def _meter_channel(self, state, suffix, src, frames, release, mode):
        """Advance one meter channel's indicators by one block.

        ``src`` is the channel's input buffer (1D mono or 2D
        voice-aware) or None for a silent block. State lives in
        ``state`` under ``env/hold/hold_age/clip_age/rms_sq`` keys with
        ``suffix`` appended, so the L and R channels are fully
        independent. Returns the published ``(level, hold, clip)``
        triple: linear amps for the bar and the peak-hold tick, plus
        whether the clip lamp is lit.

        The bar (``level``): in ``peak`` mode it is the historical
        fast-attack / time-based-release peak envelope -- bit-identical
        to the pre-``mode`` Meter. In ``rms`` mode it is
        ``sqrt(EMA(mean(x^2)))`` with a ~300 ms time constant; on a 2D
        voice buffer the mean-square is taken per voice and the loudest
        voice wins (mirroring peak's max-over-voices -- a plain mean
        would be diluted ~16x by the zero-padded slots).

        The peak-hold tick: instant attack to the block peak, holds for
        ``_METER_HOLD_SEC``, then falls exactly like the peak envelope
        (so it always reads >= the peak bar). It is driven by the
        *peak* in both modes -- that is the point: read a transient's
        true level even while the bar shows the RMS average.

        The clip lamp: any sample at or above 0 dBFS (|x| >= 1.0)
        lights it for ``_METER_CLIP_SEC``. Hold and clip ages are
        counted in samples, so their wall-clock timing is block-size
        independent.

        The ``lufs_m``/``lufs_s`` modes run the raw signal through the
        K-weighting pair (highpass + high shelf, fixed coefficients, zi
        carried exactly) into a 400 ms / 3 s mean-square EMA; the bar
        value is the linear equivalent of ``-0.691 + 10*log10(msq)`` so
        the existing dBFS pipeline displays LUFS numbers directly. On a
        2D voice buffer the loudest voice wins, mirroring rms mode.

        The clip counter tallies clip EVENTS on the raw signal in every
        mode: one unbroken run of samples at >= 0 dBFS is one event (a
        run spanning a block boundary counts once -- the tail state
        carries). Returns ``(level, hold, clip, clips)``.
        """
        peak = 0.0 if src is None or src.size == 0 else float(np.max(np.abs(src)))
        coeff = 0.1 ** (frames / self.sample_rate / release)

        # Clip-event counter (all modes, raw signal). Collapse voices to
        # a per-time-position "any voice over" line, then count rising
        # edges, carrying the run state across the block boundary.
        tail = bool(state["over_tail" + suffix])
        if src is not None and src.size:
            a = np.abs(src)
            over_t = (a if a.ndim == 1 else a.max(axis=0)) >= 1.0
            if over_t.any():
                prev = np.concatenate(([tail], over_t[:-1]))
                state["clips" + suffix] += int(np.sum(over_t & ~prev))
            state["over_tail" + suffix] = bool(over_t[-1])
        else:
            state["over_tail" + suffix] = False

        # Peak envelope (the bar in ``peak`` mode; kept warm in ``rms``
        # mode too so switching modes never restarts from silence).
        env = state["env" + suffix]
        if peak >= env:
            env = peak  # instant attack
        else:
            # Time-based release: fall a factor of ten (~20 dB) every
            # ``release`` seconds, independent of the block size.
            env = peak + (env - peak) * coeff
        state["env" + suffix] = env

        if mode == "rms":
            if src is None or src.size == 0:
                mean_sq = 0.0
            else:
                sq = np.square(src.astype(np.float64, copy=False))
                if sq.ndim == 2:
                    mean_sq = float(np.max(np.mean(sq, axis=-1)))
                else:
                    mean_sq = float(np.mean(sq))
            k = math.exp(-frames / (self.sample_rate * self._METER_RMS_SEC))
            rms_sq = mean_sq + (state["rms_sq" + suffix] - mean_sq) * k
            state["rms_sq" + suffix] = rms_sq
            level = math.sqrt(rms_sq)
        elif mode in ("lufs_m", "lufs_s"):
            if src is None or src.size == 0:
                mean_sq = 0.0
            else:
                y = self._meter_kweight(state, suffix, src)
                sq = np.square(y)
                if sq.ndim == 2:
                    mean_sq = float(np.max(np.mean(sq, axis=-1)))
                else:
                    mean_sq = float(np.mean(sq))
            tau = (self._METER_LUFS_M_SEC if mode == "lufs_m"
                   else self._METER_LUFS_S_SEC)
            k = math.exp(-frames / (self.sample_rate * tau))
            lufs_sq = mean_sq + (state["lufs_sq" + suffix] - mean_sq) * k
            state["lufs_sq" + suffix] = lufs_sq
            if lufs_sq > 1e-18:
                lufs_db = self._METER_LUFS_OFFSET + 10.0 * math.log10(lufs_sq)
                level = 10.0 ** (lufs_db / 20.0)
            else:
                level = 0.0
        else:
            level = env

        # Peak-hold tick.
        hold = state["hold" + suffix]
        hold_age = state["hold_age" + suffix]
        if peak >= hold:
            hold = peak
            hold_age = 0
        else:
            hold_age += frames
            if hold_age > int(self.sample_rate * self._METER_HOLD_SEC):
                hold = peak + (hold - peak) * coeff
        state["hold" + suffix] = hold
        state["hold_age" + suffix] = hold_age

        # Clip lamp.
        clip_age = 0 if peak >= 1.0 else state["clip_age" + suffix] + frames
        state["clip_age" + suffix] = clip_age
        clip = clip_age < int(self.sample_rate * self._METER_CLIP_SEC)

        return (level, hold, clip, int(state["clips" + suffix]))

    def _meter_kweight(self, state, suffix, src):
        """K-weight one channel's block (fixed coeffs, exact zi carry).

        Two cascaded RBJ biquads approximating BS.1770's pre-filter: a
        2nd-order highpass (~38 Hz, Q 0.5) and a +4 dB high shelf
        (~1681 Hz). Coefficients are fixed per sample rate, so plain
        lfilter zi carry is exact. State follows the input shape; a
        mono<->voice change resets the filter (worth a one-block
        loudness blip, same policy as the grain engines).
        """
        kc = getattr(self, "_meter_k_coeffs", None)
        if kc is None:
            hp = self._filter_coeffs("highpass", self._METER_K_HP_HZ,
                                     self._METER_K_HP_Q)
            sh = self._loud_shelf(self._METER_K_SHELF_HZ,
                                  self._METER_K_SHELF_DB, False)
            kc = self._meter_k_coeffs = (hp, sh)
        x = src.astype(np.float64, copy=False)
        want = (2,) if x.ndim == 1 else (x.shape[0], 2)
        for stage, (b0, b1, b2, a1n, a2n) in enumerate(kc):
            key = f"kz{stage}{suffix}"
            zi = state.get(key)
            if zi is None or zi.shape != want:
                zi = np.zeros(want)
            x, zf = lfilter([b0, b1, b2], [1.0, a1n, a2n], x, axis=-1, zi=zi)
            state[key] = zf
        return x

    def _render_meter(self, module, frames: int, buffers, patch):
        """Level-meter tap: pass audio through, track level indicators.

        Both inputs are forwarded untouched (``in`` -> ``out``, ``in_r``
        -> ``out_r``; same array, same shape -- mono or voice-aware), so
        a Meter is transparent inline. Alongside, each patched channel
        runs the indicator bundle in ``_meter_channel`` (bar level in
        the module's ``mode``, peak-hold tick, clip lamp); the latest
        triples are published to ``_audio_meter_state`` -- and the first
        channel's bar to ``_audio_levels``, the historical scalar hook
        -- for the GUI. Computing here on the audio thread means a short
        transient registers even between UI frames: the meter latency is
        block-rate, not frame-rate.

        ``in_r`` is optional. Unpatched, the right slot publishes None
        (the GUI hides the second bar), ``out_r`` renders silence and no
        R state advances -- a mono Meter behaves exactly as it always
        did.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None or src.size == 0:
            out = np.zeros(frames, dtype=np.float32)
            src = None
        else:
            out = src  # pass-through (read-only downstream, like any fan-out)

        release = float(module.params.get("release", self._METER_RELEASE_DEFAULT))
        release = min(max(release, self._METER_RELEASE_MIN), self._METER_RELEASE_MAX)
        mode = str(module.params.get("mode", "peak"))
        if mode not in ("rms", "lufs_m", "lufs_s"):
            mode = "peak"  # unknown values fall back to the default
        link = bool(module.params.get("stereo_link", False))

        state = self._state.setdefault(module.id, {})
        for suffix in ("_l", "_r"):
            if "env" + suffix not in state:
                state["env" + suffix] = 0.0
                state["hold" + suffix] = 0.0
                state["hold_age" + suffix] = 0
                # Start far past the lamp window so a fresh meter is unlit.
                state["clip_age" + suffix] = 1 << 62
                state["rms_sq" + suffix] = 0.0
            if "clips" + suffix not in state:
                state["clips" + suffix] = 0
                state["over_tail" + suffix] = False
                state["lufs_sq" + suffix] = 0.0

        left = self._meter_channel(state, "_l", src, frames, release, mode)

        if any(c.dst_port == "in_r" for c in patch.cables_into(module.id)):
            src_r = self._input_buffer(
                patch, buffers, module.id, "in_r", collapse=False
            )
            if src_r is None or src_r.size == 0:
                out_r = np.zeros(frames, dtype=np.float32)
                src_r = None
            else:
                out_r = src_r
            right = self._meter_channel(state, "_r", src_r, frames, release, mode)
        else:
            out_r = np.zeros(frames, dtype=np.float32)
            right = None

        # Stereo link ("master readout"): the bars stay per-channel, but
        # the pair shares one hold tick, one clip lamp and one numeric
        # reading -- the louder channel in peak/rms, the CHANNEL-ENERGY
        # SUM in the LUFS modes (that is how BS.1770 defines a stereo
        # loudness; per-channel linear levels are 10^(LUFS/20), so the
        # combined linear value is simply the root-sum-square).
        pair_level = None
        if link and right is not None:
            hold_pair = max(left[1], right[1])
            clip_pair = left[2] or right[2]
            if mode in ("lufs_m", "lufs_s"):
                pair_level = math.sqrt(left[0] ** 2 + right[0] ** 2)
            else:
                pair_level = max(left[0], right[0])
            left = (left[0], hold_pair, clip_pair, left[3])
            right = (right[0], hold_pair, clip_pair, right[3])

        self._audio_levels[module.id] = left[0]
        self._audio_meter_state[module.id] = (
            left, right, link and right is not None, mode, pair_level
        )

        return {"out": out, "out_r": out_r}

    # ----- Scope rendering -------------------------------------------------

    # Capture-ring length: the longest window (500 ms/div × 10 div = 5 s)
    # plus a second of history for the trigger search.
    _SCOPE_RING_SECONDS = 6.0

    def _render_scope(self, module, frames: int, buffers, patch):
        """Oscilloscope tap: bit-exact pass-through + capture rings.

        ``in`` -> ``out`` and ``in_r`` -> ``out_r`` are forwarded
        untouched (same array, same shape — the Meter precedent), so a
        Scope is transparent inline. Alongside, the module keeps rolling
        capture rings the GUI reads via :meth:`scope_window`:

          * ``t1`` — the main trace: ``in`` summed across voices, or the
            ``cv`` input when ``in`` is unpatched (the fallback trace).
          * ``t2`` — ``in_r`` summed across voices (dual / xy modes).
          * ``tg`` — the external ``trig`` gate, captured so the GUI's
            trigger search aligns with the trace rings sample-for-sample.

        Rings are lazily created per patched jack and share one write
        position, so they stay aligned; all display maths (window,
        trigger, min/max columns) happens GUI-side in ui/scope_math.py —
        the audio thread only memcpys. The GUI may read a ring mid-write
        and catch a torn frame; that's one glitchy visual frame, not
        audio (meter precedent).
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        out = src if src is not None else np.zeros(frames, dtype=np.float32)

        if any(c.dst_port == "in_r" for c in patch.cables_into(module.id)):
            src_r = self._input_buffer(
                patch, buffers, module.id, "in_r", collapse=False
            )
        else:
            src_r = None
        out_r = src_r if src_r is not None else np.zeros(frames, dtype=np.float32)

        trig = self._input_buffer(patch, buffers, module.id, "trig")

        # Main trace: `in` wins; `cv` is the fallback (collapse sums a
        # voice-aware source either way — what a mono consumer hears).
        if src is not None:
            t1 = self._voice_sum(src) if src.ndim == 2 else src
        else:
            t1 = self._input_buffer(patch, buffers, module.id, "cv")
        t2 = (self._voice_sum(src_r) if src_r.ndim == 2 else src_r) if src_r is not None else None

        st = self._state.setdefault(module.id, {})
        ringlen = int(self.sample_rate * self._SCOPE_RING_SECONDS)
        if st.get("ringlen") != ringlen:
            st.clear()
            st.update({"ringlen": ringlen, "pos": 0, "filled": 0})
        pos = int(st["pos"])

        for key, sig in (("t1", t1), ("t2", t2), ("tg", trig)):
            if sig is None:
                continue
            ring = st.get("ring_" + key)
            if ring is None:
                ring = st["ring_" + key] = np.zeros(ringlen, dtype=np.float32)
            n = min(int(sig.shape[-1]), ringlen)
            chunk = sig[-n:].astype(np.float32, copy=False)
            first = min(n, ringlen - pos)
            ring[pos : pos + first] = chunk[:first]
            if n > first:
                ring[: n - first] = chunk[first:]

        st["pos"] = (pos + frames) % ringlen
        st["filled"] = min(ringlen, int(st["filled"]) + frames)

        return {"out": out, "out_r": out_r}

    def scope_window(self, module_id: int, n: int) -> dict | None:
        """(GUI hook) The last ``n`` captured samples per scope trace.

        Returns ``{"t1": arr|None, "t2": arr|None, "tg": arr|None}`` with
        each array oldest→newest and identically aligned (the rings share
        one write position), clipped to what has been captured so far —
        or None while the scope has no state / nothing captured. Called
        from the GUI thread each frame; the copy is a couple of slices.
        """
        st = self._state.get(module_id)
        if not st or "pos" not in st:
            return None
        ringlen = int(st["ringlen"])
        n = min(int(n), int(st["filled"]))
        if n <= 0:
            return None
        pos = int(st["pos"])
        start = (pos - n) % ringlen
        out: dict = {}
        for key in ("t1", "t2", "tg"):
            ring = st.get("ring_" + key)
            if ring is None:
                out[key] = None
                continue
            if start + n <= ringlen:
                out[key] = ring[start : start + n].copy()
            else:
                k = ringlen - start
                buf = np.empty(n, dtype=np.float32)
                buf[:k] = ring[start:]
                buf[k:] = ring[: n - k]
                out[key] = buf
        return out

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
