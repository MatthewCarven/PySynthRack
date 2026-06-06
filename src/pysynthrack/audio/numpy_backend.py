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

Anti-aliased oscillator shapes are available alongside the naive ones.
Every audio-rate shape (saw / square / triangle) ships in three flavours,
selected by the ``waveform`` string suffix:

  * ``saw`` / ``square`` / ``triangle`` -- naive (cheap, aliases above the
    fundamental; sometimes exactly the lo-fi character you want).
  * ``saw_blep`` / ``square_blep`` / ``triangle_blep`` -- PolyBLEP (saw,
    square) and PolyBLAMP (triangle) correction at the waveform's
    discontinuities. Cheap, integrates with the per-sample phase ramp, and
    tracks arbitrary/FM frequencies sample-accurately.
  * ``saw_wt`` / ``square_wt`` / ``triangle_wt`` -- band-limited wavetable.
    A per-octave mipmap of additively-synthesised tables (generated once,
    cached on the backend); the table whose harmonic set stays below
    Nyquist for the block's top frequency is chosen, then linearly
    interpolated. Strongest alias rejection; table is picked per block so
    extreme FM excursions fall back conservatively (fewer harmonics).

``sine`` is already band-limited, so it has only the one naive form. The
shaping is centralised in :meth:`_osc_waveshape`, which both the
Oscillator and CVToFrequency renderers (and, via the same call, the
Keyboard / MIDIInput note sources) route through.
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
        # Audio-callback crash protection. After the first uncaught
        # exception in render_block, _render_disabled goes True and the
        # callback returns silence forever after rather than re-raising
        # into sounddevice's audio thread (which usually kills the
        # stream with a less useful traceback). The crash is captured
        # exactly once via _crash_reported - both flags reset on
        # compile() so a recompile gets a fresh chance.
        self._render_disabled: bool = False
        self._crash_reported: bool = False
        # Lazily-built band-limited wavetable mipmaps for the ``*_wt``
        # oscillator shapes. Keyed by base shape ("saw"/"square"/
        # "triangle"); each value is a (NUM_WT_TABLES, WT_LEN) float64
        # array of per-octave tables. Built once on first use via
        # _get_wavetable; shared across every oscillator-like module.
        self._wavetables: dict[str, np.ndarray] = {}

    # ----- availability ----------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        return _HAS_SOUNDDEVICE

    # ----- compile ---------------------------------------------------------

    def compile(self, patch: Patch) -> None:
        with self._lock:
            self._patch = patch
            self._topo_order = self._topological_sort(patch)
            # Recompile = a fresh chance. Clear any sticky crash state
            # from the previous patch so audio resumes on the new graph.
            self._render_disabled = False
            self._crash_reported = False
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
        if self._render_disabled:
            outdata.fill(0.0)
            return
        try:
            out = self.render_block(frames)
        except BaseException as e:
            # First uncaught exception in render_block: capture a heavy
            # report, write it out, and disable rendering for the rest
            # of this stream. Calling describe_error from inside the
            # audio thread is fine - it never raises, and the cost is
            # paid once (subsequent blocks short-circuit at the
            # _render_disabled check above).
            self._handle_audio_crash(e)
            outdata.fill(0.0)
            return
        if out is None:
            outdata.fill(0.0)
            return
        outdata[:] = out

    def _handle_audio_crash(self, exc: BaseException) -> None:
        """Called from the audio callback on the first render_block
        failure. Captures a crash report, writes it to the user's
        profile crash directory, and sets the sticky disable flag so
        subsequent blocks return silence without re-attempting the
        broken render. Idempotent - the first call does the work,
        subsequent calls are no-ops (the flag check below)."""
        self._render_disabled = True
        if self._crash_reported:
            return
        self._crash_reported = True
        try:
            import sys as _sys
            from ..error_handler import describe_error
            from .._crash import write_crash_report
            report = describe_error(exc, include_locals=True)
            path = write_crash_report(report, source="audio_callback")
            if path:
                print(
                    f"[NumpyBackend] audio render crashed: "
                    f"{type(exc).__name__}: {exc}\n"
                    f"  Silenced for the rest of this stream. "
                    f"Report: {path}",
                    file=_sys.stderr,
                )
            else:
                print(
                    f"[NumpyBackend] audio render crashed "
                    f"({type(exc).__name__}: {exc}); "
                    f"crash report could not be written.",
                    file=_sys.stderr,
                )
        except BaseException:
            # Crash reporter itself failed. Last-ditch: print whatever
            # we can about the original exception so the audio thread
            # at least leaves a breadcrumb in stderr before silencing.
            import sys as _sys
            try:
                print(
                    f"[NumpyBackend] audio render crashed AND crash "
                    f"reporter failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=_sys.stderr,
                )
            except BaseException:
                pass

    # Speaker-family sinks and the stereo channels each one feeds.
    # (left, right) flags: SpeakerOutput is the v0.1 both-channels mono
    # sink; the Left/Right pair hard-pans for poor-man's stereo.
    _SPEAKER_CHANNELS = {
        "speaker_output": (True, True),
        "left_speaker_output": (True, False),
        "right_speaker_output": (False, True),
    }

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
            channels = self._SPEAKER_CHANNELS.get(module.TYPE)
            if channels is None:
                continue
            incoming = patch.cables_into(module.id)
            if not incoming:
                continue
            cable = incoming[0]
            src_buf = buffers.get((cable.src_module_id, cable.src_port))
            if src_buf is None:
                continue
            # Voice-aware source feeding the speaker: sum the voice axis
            # to mono before mixing. This is the "implicit sum at mono
            # sinks" rule from the voice-routing design -- the speaker
            # is the canonical end-of-graph mono boundary.
            if src_buf.ndim == 2:
                src_buf = src_buf.sum(axis=0)
            gain = float(module.params.get("gain", 1.0))
            mixed = (src_buf * gain).astype(np.float32)
            left, right = channels
            if left:
                out[:, 0] += mixed
            if right:
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
        if module.TYPE == "audio_to_cv":
            return self._render_audio_to_cv(module, frames, buffers, patch)
        if module.TYPE == "cv_to_audio":
            return self._render_cv_to_audio(module, frames, buffers, patch)
        if module.TYPE == "cv_to_frequency":
            return self._render_cv_to_frequency(module, frames, buffers, patch)
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
        if module.TYPE in self._SPEAKER_CHANNELS:
            return None  # speaker-family sink — drained by the speaker pass
        return None

    # ----- input port helper ----------------------------------------------

    @staticmethod
    def _input_buffer(
        patch, buffers, dst_module_id: int, dst_port: str, collapse: bool = True
    ):
        """Look up the buffer feeding a specific input port, or None.

        Voice-aware sources publish ``(MAX_VOICES, frames)`` buffers
        (slice 2 onwards: MIDIInput's ``out``, ``gate`` and ``pitch_cv``
        already do). By default this helper collapses such buffers to
        1D via ``sum(axis=0)`` so existing mono modules continue to
        work unchanged -- a polyphonic source feeding an un-migrated
        Filter or ADSR just sees the summed mix, exactly as if the
        source were the old self-summing MIDIInput.

        Voice-aware modules (slice 3+) pass ``collapse=False`` to
        receive the per-slot data and grow per-slot state of their
        own. Mono sinks (SpeakerOutput) do their own ndim check in
        the drain pass rather than going through this helper, so the
        collapse rule there is explicit and visible.
        """
        for cable in patch.cables_into(dst_module_id):
            if cable.dst_port == dst_port:
                buf = buffers.get((cable.src_module_id, cable.src_port))
                if buf is not None and collapse and buf.ndim == 2:
                    return buf.sum(axis=0)
                return buf
        return None

    def _render_oscillator(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Audio-rate oscillator with optional per-sample CV modulation.

        Shape-polymorphic. The branch is decided by ``freq_cv``'s shape,
        because phase state is what changes with frequency:

          * ``freq_cv`` 2D ``(V, F)`` -> voice-aware path. V independent
            phase accumulators, one per voice slot. Output ``(V, F)``.
            ``amp_cv`` (any shape) and the static ``amp`` param are
            applied via numpy broadcasting at the end.
          * ``freq_cv`` 1D ``(F,)`` or None -> mono path with a single
            phase accumulator. Output ``(F,)``. If ``amp_cv`` happens to
            be 2D ``(V, F)`` (e.g. a polyphonic ADSR feeding a single
            shared oscillator), the final ``wave * amp_cv`` broadcasts
            the mono waveform across every voice -- producing a ``(V, F)``
            result. That broadcast-by-amp case is the moral equivalent
            of the VCA broadcast rules from slice 3a, and it's exactly
            what you want when one carrier should be amplitude-shaped
            independently per voice.

        Two CV inputs (both optional):
          - ``freq_cv`` follows 1V/octave: instantaneous frequency for
            sample n is ``freq * 2 ** cv[n]``. Per-sample evaluation
            makes this true FM/vibrato -- phase is integrated from the
            instantaneous frequency, not a block-rate scalar.
          - ``amp_cv`` is linear multiplicative: ``amp * cv[n]``. A
            unipolar LFO here gives AM; bipolar would invert phase.
        """
        freq = float(module.params.get("freq", 440.0))
        amp = float(module.params.get("amp", 0.5))
        waveform = str(module.params.get("waveform", "sine"))

        # CV lookups only when called via the topo walk (which always
        # passes buffers + patch). Tests that drive the oscillator in
        # isolation pass None.
        if buffers is None or patch is None:
            freq_cv = None
            amp_cv = None
        else:
            # collapse=False so a voice-aware (V, F) freq_cv or amp_cv
            # reaches us with the voice axis intact. The mono branch
            # handles a 2D amp_cv via final broadcast.
            freq_cv = self._input_buffer(
                patch, buffers, module.id, "freq_cv", collapse=False
            )
            amp_cv = self._input_buffer(
                patch, buffers, module.id, "amp_cv", collapse=False
            )

        if freq_cv is not None and freq_cv.ndim == 2:
            return self._render_oscillator_voice(
                module, frames, freq, amp, waveform, freq_cv, amp_cv
            )
        return self._render_oscillator_mono(
            module, frames, freq, amp, waveform, freq_cv, amp_cv
        )

    # Wavetable mipmap parameters. WT_LEN is the per-table sample count;
    # NUM_WT_TABLES octave bands span WT_BASE_FREQ .. ~Nyquist.
    WT_LEN = 2048
    NUM_WT_TABLES = 11
    WT_BASE_FREQ = 20.0

    def _osc_waveshape(self, phases, waveform, dt=None):
        """Apply the waveform shaping function to a phase array.

        ``phases`` can be any shape (1D for mono, 2D for voice) -- all
        ops are elementwise (or shape-preserving) so the same code
        handles both. Returns an array of the same shape with values in
        roughly [-1, 1].

        The ``waveform`` string carries both the shape and the band-
        limiting method as ``"<base>_<method>"``:

          * no suffix (``"sine"``, ``"saw"``, ``"square"``, ``"triangle"``)
            -> naive shapes, unchanged from v0.2.
          * ``"_blep"`` -> PolyBLEP (saw, square) / PolyBLAMP (triangle)
            discontinuity correction. Needs ``dt`` (the per-sample phase
            increment, == freq / sample_rate) to size the correction
            window. ``dt`` may be a scalar (constant-frequency mono ramp)
            or an array broadcastable to ``phases`` (per-sample CV / FM).
          * ``"_wt"`` -> band-limited wavetable lookup. ``dt`` selects the
            mipmap band (per block, from the largest dt -> highest freq,
            the conservative choice).

        ``dt is None`` (isolated callers / unit tests that drive the
        helper without a frequency) gracefully degrades any anti-aliased
        shape to its naive form, since there is no frequency to band-limit
        against.
        """
        if "_" in waveform:
            base, method = waveform.rsplit("_", 1)
        else:
            base, method = waveform, "naive"

        if method == "blep" and dt is not None:
            return self._waveshape_blep(base, phases, dt)
        if method == "wt" and dt is not None:
            return self._waveshape_wt(base, phases, dt)
        # naive (or anti-aliased requested with no dt -> degrade to naive)
        return self._waveshape_naive(base, phases)

    @staticmethod
    def _waveshape_naive(base, phases):
        if base == "sine":
            return np.sin(2.0 * np.pi * phases)
        if base == "saw":
            return 2.0 * phases - 1.0
        if base == "square":
            return np.where(phases < 0.5, 1.0, -1.0)
        if base == "triangle":
            return 1.0 - 4.0 * np.abs(phases - 0.5)
        return np.zeros_like(phases)

    @staticmethod
    def _poly_blep(t, dt):
        """Two-sample PolyBLEP residual for a unit upward step at phase 0/1.

        Correction is non-zero only within ``dt`` of a wrap point. ``t``
        and ``dt`` broadcast together; returns an array shaped like ``t``.
        """
        t = np.asarray(t, dtype=np.float64)
        dt = np.broadcast_to(np.asarray(dt, dtype=np.float64), t.shape)
        safe = np.where(dt == 0.0, 1.0, dt)
        res = np.zeros_like(t)
        m1 = t < dt
        x = np.where(m1, t / safe, 0.0)
        res = np.where(m1, x + x - x * x - 1.0, res)
        m2 = t > 1.0 - dt
        x2 = np.where(m2, (t - 1.0) / safe, 0.0)
        res = np.where(m2, x2 * x2 + x2 + x2 + 1.0, res)
        return res

    @staticmethod
    def _poly_blamp(t, dt):
        """Two-sample PolyBLAMP residual (integral of PolyBLEP).

        Corrects slope discontinuities (triangle corners). Same broadcast
        rules as :meth:`_poly_blep`.
        """
        t = np.asarray(t, dtype=np.float64)
        dt = np.broadcast_to(np.asarray(dt, dtype=np.float64), t.shape)
        safe = np.where(dt == 0.0, 1.0, dt)
        res = np.zeros_like(t)
        m1 = t < dt
        x = np.where(m1, t / safe - 1.0, 0.0)
        res = np.where(m1, -1.0 / 3.0 * x * x * x, res)
        m2 = t > 1.0 - dt
        x2 = np.where(m2, (t - 1.0) / safe + 1.0, 0.0)
        res = np.where(m2, 1.0 / 3.0 * x2 * x2 * x2, res)
        return res

    def _waveshape_blep(self, base, phases, dt):
        """PolyBLEP saw/square, PolyBLAMP triangle. Sine has no edges."""
        phases = np.asarray(phases, dtype=np.float64)
        if base == "saw":
            return (2.0 * phases - 1.0) - self._poly_blep(phases, dt)
        if base == "square":
            v = np.where(phases < 0.5, 1.0, -1.0)
            v = v + self._poly_blep(phases, dt)
            v = v - self._poly_blep((phases + 0.5) % 1.0, dt)
            return v
        if base == "triangle":
            tri = 1.0 - 4.0 * np.abs(phases - 0.5)
            dtb = np.broadcast_to(np.asarray(dt, np.float64), phases.shape)
            # Naive triangle slope is +/-4; the slope change at each corner
            # is +/-8. PolyBLAMP rounds those corners.
            tri = tri + 8.0 * dtb * self._poly_blamp(phases, dt)
            tri = tri - 8.0 * dtb * self._poly_blamp((phases + 0.5) % 1.0, dt)
            return tri
        # sine / unknown -> naive (sine is already band-limited)
        return self._waveshape_naive(base, phases)

    def _get_wavetable(self, base):
        """Build (cached) the per-octave band-limited mipmap for ``base``.

        Returns a ``(NUM_WT_TABLES, WT_LEN)`` float64 array. Table ``j``
        is additively synthesised with every harmonic that stays below
        Nyquist for the *top* of octave band ``j`` (so the whole band is
        alias-free), then peak-normalised to +/-1.
        """
        cached = self._wavetables.get(base)
        if cached is not None:
            return cached

        L = self.WT_LEN
        ph = np.arange(L, dtype=np.float64) / L
        nyq = self.sample_rate / 2.0
        tables = np.zeros((self.NUM_WT_TABLES, L), dtype=np.float64)
        for j in range(self.NUM_WT_TABLES):
            f_high = self.WT_BASE_FREQ * (2.0 ** (j + 1))
            max_h = max(1, int(nyq / f_high))
            acc = np.zeros(L, dtype=np.float64)
            if base == "saw":
                for k in range(1, max_h + 1):
                    acc += (1.0 / k) * np.sin(2.0 * np.pi * k * ph)
                acc *= 2.0 / np.pi
            elif base == "square":
                for k in range(1, max_h + 1, 2):
                    acc += (1.0 / k) * np.sin(2.0 * np.pi * k * ph)
                acc *= 4.0 / np.pi
            elif base == "triangle":
                k = 1
                sign = 1.0
                while k <= max_h:
                    acc += sign * (1.0 / (k * k)) * np.sin(2.0 * np.pi * k * ph)
                    sign = -sign
                    k += 2
                acc *= 8.0 / (np.pi * np.pi)
            else:
                acc = np.sin(2.0 * np.pi * ph)
            peak = float(np.max(np.abs(acc))) or 1.0
            tables[j] = acc / peak

        self._wavetables[base] = tables
        return tables

    def _waveshape_wt(self, base, phases, dt):
        """Band-limited wavetable lookup with linear interpolation.

        ``dt`` selects the mipmap band from the block's top frequency
        (largest dt -> highest fundamental -> fewest-harmonics table, the
        conservative pick that never aliases within the block).
        """
        if base == "sine":
            return np.sin(2.0 * np.pi * np.asarray(phases, np.float64))
        tables = self._get_wavetable(base)
        # Representative frequency for band selection.
        dt_max = float(np.max(np.asarray(dt, dtype=np.float64)))
        freq = max(dt_max * self.sample_rate, self.WT_BASE_FREQ)
        j = int(np.clip(
            np.floor(np.log2(freq / self.WT_BASE_FREQ)),
            0,
            self.NUM_WT_TABLES - 1,
        ))
        tbl = tables[j]
        L = self.WT_LEN
        phases = np.asarray(phases, dtype=np.float64)
        pos = phases * L
        floor_pos = np.floor(pos)
        i0 = floor_pos.astype(np.int64) % L
        i1 = (i0 + 1) % L
        frac = pos - floor_pos
        return tbl[i0] * (1.0 - frac) + tbl[i1] * frac

    def _render_oscillator_mono(
        self, module, frames, freq, amp, waveform, freq_cv, amp_cv
    ):
        """Mono fast path -- scalar phase, vectorized phase ramp.

        Logic is unchanged from the pre-slice-3 implementation: scalar
        phase state, vectorized phase ramp via arange (no freq_cv) or
        cumsum (with mono freq_cv). The amp_cv multiplication at the
        end can broadcast a (F,) mono wave against a (V, F) voice
        amp_cv, producing (V, F) output -- the broadcast-by-amp case.
        """
        state = self._state.setdefault(module.id, {"phase": 0.0})
        # If state belongs to the voice branch (different keys),
        # discard and reinit to mono shape.
        if "phase_arr" in state:
            state.clear()
            state["phase"] = 0.0

        sr = self.sample_rate
        start_phase = state["phase"]
        if freq_cv is None:
            # Fast path: constant frequency, vectorized phase ramp.
            phase_inc = freq / sr
            phases = (start_phase + np.arange(frames, dtype=np.float64) * phase_inc) % 1.0
            state["phase"] = (start_phase + frames * phase_inc) % 1.0
            dt = phase_inc
        else:
            # Per-sample frequency from CV. Integrate phase one sample
            # at a time -- cheap in numpy via cumsum of per-sample
            # increments.
            inst_freq = freq * np.power(2.0, freq_cv.astype(np.float64))
            inst_inc = inst_freq / sr
            phases = (start_phase + np.cumsum(inst_inc)) % 1.0
            state["phase"] = float(phases[-1])
            dt = inst_inc

        wave = self._osc_waveshape(phases, waveform, dt=dt)
        wave = wave * amp
        if amp_cv is not None:
            # amp_cv may be (F,) (same shape, elementwise) or (V, F)
            # (broadcasts the mono wave across V voices, yielding a
            # (V, F) result). Both are valid.
            wave = wave * amp_cv.astype(np.float64)

        return wave.astype(np.float32)

    def _render_oscillator_voice(
        self, module, frames, freq, amp, waveform, freq_cv, amp_cv
    ):
        """Voice-aware path -- V independent phase accumulators.

        ``freq_cv`` is ``(V, F)``. Each voice integrates its own phase
        via per-row cumsum, and per-voice phase state persists across
        blocks. Output is ``(V, F)``. ``amp_cv`` (any shape) is applied
        via numpy broadcasting at the end:

          * (V, F) amp_cv -> elementwise per-voice AM.
          * (F,)  amp_cv -> mono amplitude broadcast across every voice.
          * None  amp_cv -> just the static ``amp`` param.

        Phases are kept per-voice so a slot that was silent in a prior
        block (freq_cv = 0 -> phase advances at the base ``freq``)
        still carries a sensible phase when it next becomes audible,
        instead of restarting from 0.0 every retrigger. MIDIInput
        zero-pads unused slots, so silent slots advance at the param's
        base frequency -- harmless because the per-voice ADSR/VCA
        downstream silences those slots.
        """
        V = freq_cv.shape[0]
        state = self._state.setdefault(module.id, {})

        # Reinit if state belongs to the mono branch or the voice
        # count changed.
        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.zeros(V, dtype=np.float64)

        sr = self.sample_rate
        start_phase = state["phase_arr"]  # (V,)

        # Per-sample per-voice instantaneous frequency from CV.
        inst_freq = freq * np.power(2.0, freq_cv.astype(np.float64))  # (V, F)
        inst_inc = inst_freq / sr  # (V, F)
        # cumsum along the time axis, add the start phase per voice.
        # start_phase[:, None] broadcasts the (V,) starts to (V, 1).
        phases = (start_phase[:, None] + np.cumsum(inst_inc, axis=1)) % 1.0  # (V, F)
        state["phase_arr"] = phases[:, -1].copy()

        wave = self._osc_waveshape(phases, waveform, dt=inst_inc)  # (V, F)
        wave = wave * amp
        if amp_cv is not None:
            # amp_cv (V, F) -> elementwise; amp_cv (F,) -> broadcasts
            # across the voice axis (same mono amp applied to every
            # voice). Both correct under numpy broadcasting rules.
            wave = wave * amp_cv.astype(np.float64)

        return wave.astype(np.float32)


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
        volume = float(module.params.get("volume", 0.5))

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

        audio *= volume

        return {"out": audio, "gate": gate}

    # ----- MIDI input rendering ------------------------------------------

    # Polyphonic voice count: matches VoiceSlots.MAX_VOICES. Kept local
    # as a module constant rather than imported to keep the backend
    # free of circular imports with the modules layer.
    _MAX_VOICES = 16

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
        volume = float(module.params.get("volume", 0.5))
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

            audio[i] = (wave * env_ramp * gain).astype(np.float32)

            # Gate: block-constant per slot. A within-block falling
            # edge produces a one-block delay before the gate drops,
            # which is the same behavior the pre-slice mono renderer
            # had (the global gate was also block-constant).
            gate[i] = 1.0 if gating else 0.0

        audio *= volume

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
        }

    # ----- filter rendering ----------------------------------------------

    def _render_filter(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Apply a Robert Bristow-Johnson biquad to the upstream signal.

        Shape-polymorphic. A 1D ``(F,)`` audio input drives a single
        biquad and emits ``(F,)`` -- the pre-slice-3 fast path. A 2D
        ``(V, F)`` audio input runs V parallel biquads (one per voice
        slot) and emits ``(V, F)``; each voice keeps its own filter
        memory so a per-voice ADSR can shape its own filter sweep
        without bleed.

        cutoff_cv handling within the voice branch:
          * ``(V, F)`` cutoff_cv -> per-voice block-mean -> V different
            cutoff frequencies, V coefficient sets, V biquad states.
          * ``(F,)`` cutoff_cv -> single block-mean -> one coefficient
            set broadcast across every voice (the "macro" filter sweep
            use case: one LFO modulates every voice's filter equally).
          * No cutoff_cv -> static cutoff from the param.

        Per-sample loop in Python -- slow per sample but constant-cost
        per block; a 512-sample block costs ~100us in the mono path.
        The voice path adds a (V,)-wide broadcast inside the same loop,
        which numpy makes essentially free (~1.5x the mono cost for
        V=16), still well under the ~11.6ms callback budget at 44.1 kHz.
        For multi-filter chains we'd reach for scipy.signal.lfilter
        (chunk-safe via ``zi``); we avoid scipy as a dep until the perf
        actually pinches.
        """
        # collapse=False so a voice-aware (V, F) audio input reaches us
        # with the voice axis intact. cutoff_cv is also fetched with
        # collapse=False so the voice branch can do per-voice block-
        # means (the mono branch ignores the voice axis via mean()).
        src_buf = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src_buf is None:
            return np.zeros(frames, dtype=np.float32)

        cutoff_cv = self._input_buffer(
            patch, buffers, module.id, "cutoff_cv", collapse=False
        )

        if src_buf.ndim == 2:
            return self._render_filter_voice(module, frames, src_buf, cutoff_cv)
        return self._render_filter_mono(module, frames, src_buf, cutoff_cv)

    def _filter_coeffs(self, mode, cutoff, q):
        """Compute RBJ biquad coefficients for one cutoff/Q pair.

        Returns ``(b0, b1, b2, a1n, a2n)`` already normalized by a0,
        or ``None`` if the mode is unknown (caller treats as passthrough).
        cutoff is clamped to (20 Hz, 0.45*sr) and q to (0.1, 20) here so
        callers don't need to.
        """
        sr = self.sample_rate
        cutoff = max(20.0, min(cutoff, sr * 0.45))
        q = max(0.1, min(q, 20.0))

        w0 = 2.0 * np.pi * cutoff / sr
        cos_w0 = float(np.cos(w0))
        sin_w0 = float(np.sin(w0))
        alpha = sin_w0 / (2.0 * q)

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
            return None

        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha

        b0 /= a0
        b1 /= a0
        b2 /= a0
        a1n = a1 / a0
        a2n = a2 / a0
        return b0, b1, b2, a1n, a2n

    def _render_filter_mono(self, module, frames, src_buf, cutoff_cv):
        """Mono fast path -- single biquad, scalar state, output ``(F,)``.

        Functionally unchanged from the pre-slice-3 implementation; the
        scalar inner loop is exactly the same so every existing Filter
        test passes bit-for-bit identically.
        """
        state = self._state.setdefault(
            module.id, {"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0}
        )
        # If state belongs to the voice branch from a previous call
        # (different audio shape), discard and reinit to mono shape.
        if "x1_arr" in state:
            state.clear()
            state.update({"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0})

        mode = str(module.params.get("mode", "lowpass"))
        cutoff = float(module.params.get("cutoff", 1000.0))
        q = float(module.params.get("resonance", 0.707))

        # CV-modulate the cutoff. 1V/octave: ``cutoff *= 2 ** mean(cv)``.
        # Block-mean keeps the biquad coefficient recomputation to one
        # pass per block; audio-rate cutoff mod would need per-sample
        # coefs (~9x cost in this tight scalar loop). If cutoff_cv is
        # 2D (a voice-aware source feeding a mono filter), mean over
        # both axes -- same effect as the old collapse=True path.
        if cutoff_cv is not None and cutoff_cv.size > 0:
            cutoff = cutoff * float(2.0 ** float(np.mean(cutoff_cv)))

        coeffs = self._filter_coeffs(mode, cutoff, q)
        if coeffs is None:
            return src_buf.astype(np.float32)  # unknown mode -> passthrough
        b0, b1, b2, a1n, a2n = coeffs

        x1 = state["x1"]
        x2 = state["x2"]
        y1 = state["y1"]
        y2 = state["y2"]

        out = np.empty(frames, dtype=np.float32)
        # Tight scalar loop. NumPy can't vectorize IIR (each sample
        # depends on the previous output). Python's still fast enough
        # at this size.
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

    def _render_filter_voice(self, module, frames, src_buf, cutoff_cv):
        """Voice-aware path -- V parallel biquads, output ``(V, F)``.

        Per-sample loop is still serial (the biquad recurrence is
        causal in time), but the per-voice updates inside each sample
        are vectorized across V via numpy broadcasting. The inner
        recurrence ``y0 = b0*x0 + b1*x1 + b2*x2 - a1n*y1 - a2n*y2`` is
        identical to the mono path -- the only difference is that
        ``x0..y2`` are ``(V,)`` arrays and ``b0..a2n`` are either
        scalars (one cutoff for all voices) or ``(V,)`` arrays (per-
        voice cutoffs). Broadcasting handles both.
        """
        V = src_buf.shape[0]
        state = self._state.setdefault(module.id, {})

        # Reinit if state belongs to the mono branch or the voice
        # count changed (latter is paranoia -- V is always
        # _MAX_VOICES today).
        needs_reinit = (
            "x1_arr" not in state
            or state["x1_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["x1_arr"] = np.zeros(V, dtype=np.float64)
            state["x2_arr"] = np.zeros(V, dtype=np.float64)
            state["y1_arr"] = np.zeros(V, dtype=np.float64)
            state["y2_arr"] = np.zeros(V, dtype=np.float64)

        mode = str(module.params.get("mode", "lowpass"))
        base_cutoff = float(module.params.get("cutoff", 1000.0))
        q = float(module.params.get("resonance", 0.707))

        # Per-voice cutoff when cutoff_cv is (V, F): each voice gets
        # its own block-mean. Otherwise single shared cutoff.
        per_voice_cutoff = (
            cutoff_cv is not None
            and cutoff_cv.ndim == 2
            and cutoff_cv.shape[0] == V
            and cutoff_cv.size > 0
        )

        if per_voice_cutoff:
            cv_block_mean = cutoff_cv.mean(axis=1)  # (V,)
            cutoff_per_voice = base_cutoff * np.power(2.0, cv_block_mean)
            sr = self.sample_rate
            cutoff_per_voice = np.clip(cutoff_per_voice, 20.0, sr * 0.45)
            q_clamped = max(0.1, min(q, 20.0))

            w0 = 2.0 * np.pi * cutoff_per_voice / sr  # (V,)
            cos_w0 = np.cos(w0)
            sin_w0 = np.sin(w0)
            alpha = sin_w0 / (2.0 * q_clamped)

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
                b1 = np.zeros(V, dtype=np.float64)
                b2 = -sin_w0 / 2.0
            else:
                return src_buf.astype(np.float32)  # unknown -> passthrough

            a0 = 1.0 + alpha
            a1 = -2.0 * cos_w0
            a2 = 1.0 - alpha
            b0 = b0 / a0
            b1 = b1 / a0
            b2 = b2 / a0
            a1n = a1 / a0
            a2n = a2 / a0
        else:
            cutoff = base_cutoff
            if cutoff_cv is not None and cutoff_cv.size > 0:
                # mean() over whatever shape: 1D collapses to scalar,
                # 2D shouldn't reach here but be safe.
                cutoff = cutoff * float(2.0 ** float(np.mean(cutoff_cv)))
            coeffs = self._filter_coeffs(mode, cutoff, q)
            if coeffs is None:
                return src_buf.astype(np.float32)
            b0, b1, b2, a1n, a2n = coeffs  # scalars

        x1 = state["x1_arr"]
        x2 = state["x2_arr"]
        y1 = state["y1_arr"]
        y2 = state["y2_arr"]

        out = np.empty((V, frames), dtype=np.float32)
        # Serial in time, vectorized across voices. Each iteration
        # does a (V,)-wide multiply-add; with V=16 and frames=512
        # numpy makes the per-iteration cost basically identical to
        # one scalar iteration.
        for n in range(frames):
            x0 = src_buf[:, n].astype(np.float64)  # (V,)
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2
            out[:, n] = y0
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0

        state["x1_arr"] = x1
        state["x2_arr"] = x2
        state["y1_arr"] = y1
        state["y2_arr"] = y2

        return out

    # ----- ADSR rendering -------------------------------------------------

    # Gate is treated as "high" once it crosses this threshold; this gives
    # us tolerance against fractional gate values (e.g. an LFO-style gate
    # in some future patching) without false triggers on numerical noise.
    _GATE_HIGH = 0.5

    # Integer phase codes for the vectorized state machine. The mono
    # fast path below still uses strings — we keep them separate so an
    # existing test that introspected the state (none do today, but they
    # could) sees unchanged behaviour.
    _ADSR_IDLE = 0
    _ADSR_ATTACK = 1
    _ADSR_DECAY = 2
    _ADSR_SUSTAIN = 3
    _ADSR_RELEASE = 4

    def _render_adsr(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sample-accurate ADSR driven by a gate signal.

        State machine: idle → attack → decay → sustain → release → idle.
        - Attack ramps linearly from current level to 1.0 over ``attack``
          seconds (so retriggering before full release picks up where the
          envelope was, no click).
        - Decay ramps from 1.0 to ``sustain`` over ``decay`` seconds.
        - Sustain holds at ``sustain`` while the gate stays high.
        - Release ramps from current level to 0.0 over ``release`` seconds.

        Shape-polymorphic. A 1D ``(F,)`` gate input drives the existing
        scalar state machine and emits ``(F,)``. A 2D ``(V, F)`` gate
        input (from voice-aware MIDIInput / Keyboard) maintains ``V``
        independent state machines and emits ``(V, F)`` — the per-voice
        envelopes that are the whole point of the polyphonic v0.4 work.

        All durations are clamped to a >= 1-sample minimum so any param
        edits remain numerically stable.
        """
        # collapse=False so a (V, F) gate buffer reaches us with its
        # voice axis intact. Mono sources still arrive as (F,).
        gate_buf = self._input_buffer(
            patch, buffers, module.id, "gate", collapse=False
        )
        sr = self.sample_rate

        attack_s = max(0.0, float(module.params.get("attack", 0.01)))
        decay_s = max(0.0, float(module.params.get("decay", 0.1)))
        sustain = max(0.0, min(1.0, float(module.params.get("sustain", 0.7))))
        release_s = max(0.0, float(module.params.get("release", 0.3)))

        attack_step = 1.0 / max(1.0, attack_s * sr)
        decay_step = (1.0 - sustain) / max(1.0, decay_s * sr)
        # Release time used to compute per-voice release_step at the
        # gate-fall edge (so a release from mid-attack still takes the
        # full release window).
        release_samples = max(1.0, release_s * sr)

        # Branch by gate shape. Voice-aware when gate has a leading axis.
        if gate_buf is not None and gate_buf.ndim == 2:
            return self._render_adsr_voice(
                module, frames, gate_buf,
                attack_step, decay_step, sustain, release_samples,
            )
        return self._render_adsr_mono(
            module, frames, gate_buf,
            attack_step, decay_step, sustain, release_samples,
        )

    def _render_adsr_mono(
        self, module, frames, gate_buf,
        attack_step, decay_step, sustain, release_samples,
    ):
        """Mono fast path — scalar state machine, output ``(F,)``.

        Unchanged from the pre-slice-3 implementation. Kept as the fast
        path so existing patches and the entire existing ADSR test
        suite continue to work bit-for-bit identically.
        """
        state = self._state.setdefault(
            module.id,
            {"phase": "idle", "level": 0.0, "prev_gate": False, "release_step": 0.0},
        )
        # If this slot of state belongs to the voice-aware path from a
        # previous call (different gate shape), discard and reinit.
        if "phase_arr" in state:
            state.clear()
            state.update(
                {"phase": "idle", "level": 0.0, "prev_gate": False, "release_step": 0.0}
            )

        out = np.empty(frames, dtype=np.float32)

        for n in range(frames):
            gate_high = (
                bool(gate_buf[n] > self._GATE_HIGH) if gate_buf is not None else False
            )

            if gate_high and not state["prev_gate"]:
                state["phase"] = "attack"
            elif not gate_high and state["prev_gate"]:
                state["release_step"] = state["level"] / release_samples
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

            state["level"] = level
            out[n] = level

        return out.astype(np.float32)

    def _render_adsr_voice(
        self, module, frames, gate_buf,
        attack_step, decay_step, sustain, release_samples,
    ):
        """Voice-aware path — V independent state machines in lockstep.

        Per-sample loop is still serial (the state machine is per-
        sample-causal), but the per-voice updates inside each sample
        are vectorized across V via numpy boolean masks. For V=16 that
        means the per-sample overhead is ~constant regardless of how
        many voices are active.

        Phase is encoded as an int code (see ``_ADSR_*`` constants) so
        the per-sample masks (``phase == _ADSR_ATTACK``) vectorize
        cleanly with numpy comparison ops.
        """
        V = gate_buf.shape[0]
        state = self._state.setdefault(module.id, {})

        # If state belongs to the mono branch (different keys), or the
        # voice count changed, reinitialise. The latter shouldn't happen
        # in practice (V is always MAX_VOICES = 16 today), but the
        # check costs nothing and protects against future shape drift.
        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.full(V, self._ADSR_IDLE, dtype=np.int32)
            state["level_arr"] = np.zeros(V, dtype=np.float64)
            state["prev_gate_arr"] = np.zeros(V, dtype=bool)
            state["release_step_arr"] = np.zeros(V, dtype=np.float64)

        phase = state["phase_arr"]
        level = state["level_arr"]
        prev_gate = state["prev_gate_arr"]
        release_step = state["release_step_arr"]

        out = np.empty((V, frames), dtype=np.float32)

        for n in range(frames):
            gate_high = gate_buf[:, n] > self._GATE_HIGH  # (V,) bool
            rising = gate_high & ~prev_gate
            falling = ~gate_high & prev_gate

            # Rising edges -> ATTACK.
            phase[rising] = self._ADSR_ATTACK
            # Falling edges -> RELEASE, with release_step set from the
            # current level so the tail takes the full release window
            # regardless of where in the envelope we were.
            release_step[falling] = level[falling] / release_samples
            phase[falling] = self._ADSR_RELEASE

            prev_gate = gate_high.copy()

            # Advance level per phase. Each mask is a vectorized "which
            # voices are in this state right now"; the updates apply to
            # exactly those voices.
            attack_mask = phase == self._ADSR_ATTACK
            level[attack_mask] += attack_step
            # Attack done -> DECAY.
            done_attack = attack_mask & (level >= 1.0)
            level[done_attack] = 1.0
            phase[done_attack] = self._ADSR_DECAY

            decay_mask = phase == self._ADSR_DECAY
            level[decay_mask] -= decay_step
            # Decay done -> SUSTAIN.
            done_decay = decay_mask & (level <= sustain)
            level[done_decay] = sustain
            phase[done_decay] = self._ADSR_SUSTAIN

            sustain_mask = phase == self._ADSR_SUSTAIN
            level[sustain_mask] = sustain

            release_mask = phase == self._ADSR_RELEASE
            level[release_mask] -= release_step[release_mask]
            # Release done -> IDLE.
            done_release = release_mask & (level <= 0.0)
            level[done_release] = 0.0
            phase[done_release] = self._ADSR_IDLE

            out[:, n] = level

        # Persist state for next block.
        state["phase_arr"] = phase
        state["level_arr"] = level
        state["prev_gate_arr"] = prev_gate
        state["release_step_arr"] = release_step

        return out.astype(np.float32)

    # ----- LFO rendering --------------------------------------------------

    def _render_lfo(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Low-frequency oscillator emitting a CV signal.

        Shape-polymorphic (slice 3b.2). The branch is decided by
        ``rate_cv``'s shape -- phase state is what changes with rate:

          * ``rate_cv`` 2D ``(V, F)`` -> voice-aware path. V independent
            phase accumulators, one per voice slot, each clocked at its
            own per-voice block-mean rate. Output ``(V, F)``.
          * ``rate_cv`` 1D ``(F,)`` or None -> mono path with a single
            phase accumulator. Output ``(F,)``.

        Phase state is per-module so multiple LFOs in one patch don't
        share state. ``random`` waveform is sample-and-hold: re-roll
        once per cycle when the phase wraps past 1.0, with per-voice
        S&H values in the voice branch so independently-clocked voices
        roll their own randoms on their own wrap edges.
        """
        # collapse=False so a voice-aware (V, F) rate_cv reaches us
        # with the voice axis intact. ``buffers``/``patch`` are None
        # when called from unit tests in isolation, in which case
        # rate_cv is unavailable -- same back-compat trick we use on
        # _render_oscillator.
        rate_cv = None
        if buffers is not None and patch is not None:
            rate_cv = self._input_buffer(
                patch, buffers, module.id, "rate_cv", collapse=False
            )

        if rate_cv is not None and rate_cv.ndim == 2:
            return self._render_lfo_voice(module, frames, rate_cv)
        return self._render_lfo_mono(module, frames, rate_cv)

    def _render_lfo_mono(self, module, frames, rate_cv):
        """Mono fast path -- scalar phase, vectorized phase ramp.

        Unchanged from the pre-slice-3b.2 implementation; the scalar
        ramp + waveshape is exactly the same so every existing LFO test
        passes bit-for-bit identically.
        """
        state = self._state.setdefault(
            module.id, {"phase": 0.0, "random_value": 0.0}
        )
        # If state belongs to the voice branch (different keys),
        # discard and reinit to mono shape.
        if "phase_arr" in state:
            state.clear()
            state.update({"phase": 0.0, "random_value": 0.0})

        waveform = str(module.params.get("waveform", "sine"))
        rate = float(module.params.get("rate", 4.0))
        depth = float(module.params.get("depth", 1.0))
        bipolar = bool(module.params.get("bipolar", False))

        sr = self.sample_rate
        # CV-modulate the rate: 1V/octave, block-mean. If rate_cv is
        # 2D for any reason it shouldn't reach this branch -- the
        # dispatcher routes (V, F) to the voice path. mean() over a
        # 1D slice is the same as the old code.
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * float(2.0 ** float(np.mean(rate_cv)))

        # Clamp to a safe range: 0.001 Hz floor (one cycle per ~17 min)
        # and an effective ceiling at Nyquist/2 -- beyond that an LFO
        # is just an audio oscillator and the user should reach for
        # ``oscillator``.
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
            # Map [-1, 1] -> [0, 1] (so a sine-LFO into a VCA gives a smooth
            # tremolo rather than an inverted-phase audio fight).
            wave = (wave + 1.0) * 0.5

        return (wave * depth).astype(np.float32)

    def _render_lfo_voice(self, module, frames, rate_cv):
        """Voice-aware path -- V independent phase accumulators.

        ``rate_cv`` is ``(V, F)``. Each voice gets its own block-mean
        rate (1V/oct) and accumulates phase at its own increment, with
        per-voice phase state persisting across blocks. Output is
        ``(V, F)``.

        Block-mean cadence on the per-voice rate matches the mono path
        -- one rate per block per voice. Audio-rate rate modulation
        would need per-sample increments via cumsum (cf. the oscillator
        voice path); the LFO is sub-audio by definition, so block-mean
        is the right cost/quality trade-off.

        Per-voice phase persists across blocks rather than resetting on
        retrigger -- mirrors the oscillator voice-path policy and
        avoids click on rate jumps. For ``random`` waveform each voice
        carries its own sample-and-hold value, re-rolled on its own
        phase wrap (independently-clocked voices roll independently).
        """
        V = rate_cv.shape[0]
        state = self._state.setdefault(module.id, {})

        # Reinit if state belongs to the mono branch or the voice
        # count changed.
        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.zeros(V, dtype=np.float64)
            state["random_arr"] = np.zeros(V, dtype=np.float64)

        waveform = str(module.params.get("waveform", "sine"))
        base_rate = float(module.params.get("rate", 4.0))
        depth = float(module.params.get("depth", 1.0))
        bipolar = bool(module.params.get("bipolar", False))

        sr = self.sample_rate
        depth_c = max(0.0, min(depth, 1.0))

        # Per-voice block-mean rate (1V/oct). Each voice gets its own
        # phase increment for this block.
        cv_block_mean = rate_cv.mean(axis=1)  # (V,)
        rate_per_voice = base_rate * np.power(
            2.0, cv_block_mean.astype(np.float64)
        )
        rate_per_voice = np.clip(rate_per_voice, 0.001, sr * 0.45)

        phase_inc_per_voice = rate_per_voice / sr  # (V,)
        start_phase = state["phase_arr"]  # (V,)

        # Per-voice phase ramps via broadcast: (V, 1) + (1, F) * (V, 1)
        # -> (V, F). Each row is a phase ramp clocked at that voice's
        # rate.
        step = np.arange(frames, dtype=np.float64)  # (F,)
        phases = (
            start_phase[:, None]
            + step[None, :] * phase_inc_per_voice[:, None]
        ) % 1.0  # (V, F)
        new_phase = (start_phase + frames * phase_inc_per_voice) % 1.0

        if waveform == "sine":
            wave = np.sin(2.0 * np.pi * phases)
        elif waveform == "triangle":
            wave = 1.0 - 4.0 * np.abs(phases - 0.5)
        elif waveform == "square":
            wave = np.where(phases < 0.5, 1.0, -1.0)
        elif waveform == "saw":
            wave = 2.0 * phases - 1.0
        elif waveform == "random":
            # Per-voice sample-and-hold. Each voice independently
            # detects its own phase wrap and re-rolls. Serial across
            # voices (S&H isn't vectorizable -- each row's output
            # depends on its own prior value on the wrap edges).
            wave = np.empty((V, frames), dtype=np.float64)
            random_arr = state["random_arr"]
            for v in range(V):
                row_phases = phases[v]
                row_start = float(start_phase[v])
                current = float(random_arr[v])
                if frames > 0:
                    if row_start == 0.0 and current == 0.0:
                        current = float(np.random.uniform(-1.0, 1.0))
                    diffs = np.diff(np.concatenate([[row_start], row_phases]))
                    for i in range(frames):
                        if diffs[i] < 0.0:
                            current = float(np.random.uniform(-1.0, 1.0))
                        wave[v, i] = current
                    random_arr[v] = current
            state["random_arr"] = random_arr
        else:
            wave = np.zeros((V, frames), dtype=np.float64)

        state["phase_arr"] = new_phase

        if not bipolar:
            wave = (wave + 1.0) * 0.5

        return (wave * depth_c).astype(np.float32)

    # ----- VCA rendering --------------------------------------------------

    def _render_vca(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Voltage-controlled amplifier: out = audio * cv * gain.

        Missing audio in → silence. Missing CV in → passthrough at unity
        (so a VCA with no envelope still behaves like a gain stage).

        Voice-aware: opts into ``collapse=False`` on both inputs so a
        polyphonic ADSR -> VCA -> Speaker chain preserves per-voice
        envelope identity. Numpy broadcasting handles every shape
        combination correctly:

          * (V, F) audio  × (V, F) cv → (V, F) element-wise.
          * (V, F) audio  × (F,)  cv → (V, F) — mono CV broadcasts
                                       across every voice (e.g. a
                                       channel-wide aftertouch VCA).
          * (F,)  audio   × (V, F) cv → (V, F) — mono audio sliced
                                       into voices by per-voice CV.
          * (F,)  audio   × (F,)  cv → (F,) mono fast path.

        VCA is stateless, so there's no per-voice state to track —
        broadcasting is the entire migration.
        """
        audio_in = self._input_buffer(
            patch, buffers, module.id, "audio", collapse=False
        )
        if audio_in is None:
            return np.zeros(frames, dtype=np.float32)
        cv_in = self._input_buffer(
            patch, buffers, module.id, "cv", collapse=False
        )
        gain = float(module.params.get("gain", 1.0))
        if cv_in is None:
            return (audio_in * gain).astype(np.float32)
        return (audio_in * cv_in * gain).astype(np.float32)

    # ----- AudioToCV rendering --------------------------------------------

    def _render_audio_to_cv(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Envelope follower: rectify input + asymmetric one-pole smoothing.

        Per-sample state (the smoother's current level feeds back into
        the next sample), so the DSP loop runs in Python -- same pattern
        as the scalar biquad in ``_render_filter_mono`` and the S&H
        branch in ``_render_lfo``. At 512-sample blocks the cost is in
        the same ballpark as those modules.

        Coefficients are derived from time constants:

            coef = 1 - exp(-1 / (time_seconds * sample_rate))

        A target rising above the current level uses ``attack_coef``;
        a target below uses ``release_coef``. Zero or negative time
        constants are clamped to "instant" (coef = 1.0).

        Voice-aware. Branches on the audio input's ``ndim``:

          * 1D ``(F,)`` audio -> scalar smoother state, output ``(F,)``.
          * 2D ``(V, F)`` audio -> per-voice smoother state stored as a
            length-V vector, output ``(V, F)``. Per-sample updates are
            vectorized across voices: three V-wide numpy ops per
            sample, no per-voice Python loop.

        Missing audio in -> silence out and the smoother state is left
        as-is (so reconnecting the cable doesn't snap back from a stale
        decayed level mid-transient).
        """
        audio_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if audio_in is None:
            return np.zeros(frames, dtype=np.float32)

        attack_ms = float(module.params.get("attack_ms", 5.0))
        release_ms = float(module.params.get("release_ms", 100.0))
        gain = float(module.params.get("gain", 1.0))

        sr = self.sample_rate
        attack_coef = 1.0 if attack_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(attack_ms, 1e-6) * 1e-3 * sr))
        )
        release_coef = 1.0 if release_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(release_ms, 1e-6) * 1e-3 * sr))
        )

        if audio_in.ndim == 2:
            return self._render_audio_to_cv_voice(
                module, frames, audio_in, attack_coef, release_coef, gain
            )
        return self._render_audio_to_cv_mono(
            module, frames, audio_in, attack_coef, release_coef, gain
        )

    def _render_audio_to_cv_mono(
        self, module, frames, audio_in, attack_coef, release_coef, gain
    ):
        """Scalar follower state, single smoother. Output ``(F,)``."""
        state = self._state.setdefault(module.id, {"level": 0.0})
        # Discard voice-branch state if we previously rendered (V, F).
        if "level_arr" in state:
            state.clear()
            state["level"] = 0.0

        out = np.empty(frames, dtype=np.float32)
        level = float(state["level"])
        abs_in = np.abs(audio_in).astype(np.float64)
        for n in range(frames):
            target = float(abs_in[n])
            coef = attack_coef if target > level else release_coef
            level += coef * (target - level)
            out[n] = level
        state["level"] = level
        return (out * gain).astype(np.float32)

    def _render_audio_to_cv_voice(
        self, module, frames, audio_in, attack_coef, release_coef, gain
    ):
        """Per-voice follower state. Output ``(V, F)``.

        ``audio_in`` is ``(V, F)``. Per-sample updates run vectorized
        across voices: ``abs`` + ``where`` + IIR step are each one
        numpy op over the length-V state vector, then we write a row
        of the output. F serial steps, no Python loop over voices.
        """
        V = audio_in.shape[0]
        state = self._state.setdefault(module.id, {})

        needs_reinit = (
            "level_arr" not in state or state["level_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["level_arr"] = np.zeros(V, dtype=np.float64)

        level = state["level_arr"]  # (V,) -- mutated across the loop.
        out = np.empty((V, frames), dtype=np.float32)
        abs_in = np.abs(audio_in).astype(np.float64)
        for n in range(frames):
            target = abs_in[:, n]  # (V,)
            coef = np.where(target > level, attack_coef, release_coef)
            level = level + coef * (target - level)
            out[:, n] = level
        state["level_arr"] = level
        return (out * gain).astype(np.float32)

    # ----- CVToAudio rendering --------------------------------------------

    def _render_cv_to_audio(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Signal-kind relabel: CV input -> audio output, optional gain.

        Stateless. The patch model forbids ``cv -> audio`` cables, so
        this module exists purely to satisfy the type system; the DSP
        is a buffer copy (multiplied by ``gain``).

        Voice-awareness is by shape preservation. The CV input arrives
        via :meth:`_input_buffer` with ``collapse=False`` so a
        ``(V, F)`` polyphonic CV (e.g. per-voice ADSR) reaches us with
        its voice axis intact, and the output keeps the same shape.
        Downstream Speaker drain collapses the voice axis at the
        mono boundary like it does for any other voice-aware audio.

        Missing cable -> silence (1D ``(F,)`` zeros). We can't know
        the intended voice count without an input, so the un-patched
        case always emits mono.

        No DC blocking. A constant CV (e.g. an ADSR's sustain level)
        produces a DC offset that the Speaker limiter clamps -- the
        user is trusted to patch a high-pass module if they need one.
        """
        cv_in = self._input_buffer(
            patch, buffers, module.id, "cv", collapse=False
        )
        if cv_in is None:
            return np.zeros(frames, dtype=np.float32)
        gain = float(module.params.get("gain", 1.0))
        return (cv_in * gain).astype(np.float32)

    # ----- CVToFrequency rendering ----------------------------------------

    def _render_cv_to_frequency(
        self, module, frames: int, buffers, patch
    ) -> np.ndarray:
        """Self-contained CV-controlled oscillator with three-point Hz map.

        Maps the incoming CV (clamped to [0, 1] in phase 1) to a per-
        sample instantaneous frequency via a piecewise interpolation
        between three anchor points: ``f0`` at CV=0, ``fm`` at CV=0.5,
        ``f1`` at CV=1.0. The ``mode`` param picks log-Hz interpolation
        (equal-octave splits, musical default) or linear-Hz (equal-Hz
        splits, deliberately bent). Phase is integrated from that
        instantaneous frequency via cumsum -- the same trick the
        Oscillator's freq_cv path uses, just applied to a different
        CV→Hz function.

        Shape-polymorphic on the CV input:
          * No CV cable or 1D ``(F,)`` CV -> mono path, single phase
            accumulator, output ``(F,)``. Unpatched CV falls back to
            the ``freq`` param (Oscillator-style behaviour — the
            module is a sound source, so it always produces sound).
          * 2D ``(V, F)`` CV -> voice-aware path, V independent phase
            accumulators (one per voice slot), output ``(V, F)``.

        Bipolar sources (e.g. an LFO with ``bipolar=True``) get their
        negative half clamped to f0 in phase 1; the planned phase 2
        adds a separate negative-side mapping with its own mode.
        """
        # collapse=False so a voice-aware (V, F) CV reaches us with the
        # voice axis intact.
        cv_in = self._input_buffer(
            patch, buffers, module.id, "cv", collapse=False
        )

        f0 = float(module.params.get("f0", 110.0))
        fm = float(module.params.get("fm", 440.0))
        f1 = float(module.params.get("f1", 1760.0))
        freq_fallback = float(module.params.get("freq", 440.0))
        waveform = str(module.params.get("waveform", "sine"))
        mode = str(module.params.get("mode", "log"))

        if cv_in is not None and cv_in.ndim == 2:
            return self._render_cv_to_frequency_voice(
                module, frames, cv_in, f0, fm, f1, waveform, mode
            )
        return self._render_cv_to_frequency_mono(
            module, frames, cv_in, f0, fm, f1, freq_fallback, waveform, mode
        )

    @staticmethod
    def _cv_to_hz(cv, f0, fm, f1, mode):
        """Piecewise interpolation of CV in [0, 1] to Hz via (f0, fm, f1).

        Shape-preserving: ``cv`` of any shape comes back as Hz of the
        same shape. Clamps the input to [0, 1] internally so callers
        don't need to.

        Lower segment (cv in [0, 0.5]): t = cv*2, blend f0->fm.
        Upper segment (cv in [0.5, 1.0]): t = (cv-0.5)*2, blend fm->f1.

        Log mode interpolates in log2-Hz so equal CV steps -> equal
        octave steps. Linear mode interpolates literal Hz.
        """
        cv = np.clip(cv.astype(np.float64), 0.0, 1.0)
        lower = cv < 0.5
        t = np.where(lower, cv * 2.0, (cv - 0.5) * 2.0)
        if mode == "log":
            # Guard log2 against zero/negative anchor values from the
            # user; clamp to 1e-6 Hz minimum (well below audible).
            lf0 = np.log2(max(f0, 1e-6))
            lfm = np.log2(max(fm, 1e-6))
            lf1 = np.log2(max(f1, 1e-6))
            log_hz = np.where(
                lower,
                lf0 + t * (lfm - lf0),
                lfm + t * (lf1 - lfm),
            )
            return np.power(2.0, log_hz)
        # linear (default fallback for any unknown mode string)
        return np.where(
            lower,
            f0 + t * (fm - f0),
            fm + t * (f1 - fm),
        )

    def _render_cv_to_frequency_mono(
        self, module, frames, cv_in, f0, fm, f1, freq_fallback, waveform, mode
    ):
        """Mono path -- single phase accumulator, output ``(F,)``.

        With no CV patched, the static ``freq`` param drives a vector-
        ized phase ramp (constant inc, arange). With CV patched, the
        per-sample CV is mapped to instantaneous Hz and phase is
        integrated via cumsum.
        """
        state = self._state.setdefault(module.id, {"phase": 0.0})
        # Discard voice-shaped state if it leaked over from a previous
        # voice-branch call on the same module id.
        if "phase_arr" in state:
            state.clear()
            state["phase"] = 0.0

        sr = self.sample_rate
        start_phase = state["phase"]

        if cv_in is None:
            # No CV cable -> static fallback frequency, vectorized ramp.
            phase_inc = freq_fallback / sr
            phases = (
                start_phase + np.arange(frames, dtype=np.float64) * phase_inc
            ) % 1.0
            state["phase"] = (start_phase + frames * phase_inc) % 1.0
            dt = phase_inc
        else:
            inst_freq = self._cv_to_hz(cv_in, f0, fm, f1, mode)  # (F,)
            inst_inc = inst_freq / sr
            phases = (start_phase + np.cumsum(inst_inc)) % 1.0
            state["phase"] = float(phases[-1])
            dt = inst_inc

        wave = self._osc_waveshape(phases, waveform, dt=dt)
        return wave.astype(np.float32)

    def _render_cv_to_frequency_voice(
        self, module, frames, cv_in, f0, fm, f1, waveform, mode
    ):
        """Voice-aware path -- V independent phase accumulators.

        ``cv_in`` is ``(V, F)``. Each voice slot integrates its own
        phase via per-row cumsum. Output is ``(V, F)``. Per-voice
        phase state persists across blocks so a slot that briefly
        goes silent (CV=0 -> f0) and comes back doesn't restart from
        zero phase mid-cycle.

        Silent slots advance at f0 (since cv=0 maps to f0); downstream
        VCA/ADSR gating silences those voices in practice. Same
        harmless behaviour as the Oscillator's voice path.
        """
        V = cv_in.shape[0]
        state = self._state.setdefault(module.id, {})

        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.zeros(V, dtype=np.float64)

        sr = self.sample_rate
        start_phase = state["phase_arr"]  # (V,)

        inst_freq = self._cv_to_hz(cv_in, f0, fm, f1, mode)  # (V, F)
        inst_inc = inst_freq / sr
        phases = (
            start_phase[:, None] + np.cumsum(inst_inc, axis=1)
        ) % 1.0  # (V, F)
        state["phase_arr"] = phases[:, -1].copy()

        wave = self._osc_waveshape(phases, waveform, dt=inst_inc)  # (V, F)
        return wave.astype(np.float32)

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

        Shape-polymorphic (slice 3b.2). A 1D ``(F,)`` audio input drives
        a single pair of cascaded biquads per branch and emits two 1D
        buffers -- the pre-slice fast path. A 2D ``(V, F)`` audio input
        runs V parallel pairs of cascaded biquads per branch (one set
        per voice slot) and emits two ``(V, F)`` buffers; each voice
        keeps its own biquad memory so a per-voice carrier upstream
        gets split cleanly without cross-talk.

        Two cascaded Butterworth (Q=1/sqrt(2)) biquads per branch. The
        shared ``a`` denominator and the LP/HP numerators are the
        standard RBJ cookbook coefficients; running them in series
        gives the LR4 magnitude response (-24 dB/oct, -6 dB at corner)
        and the phase relationship that lets low+high sum back to a
        flat magnitude. Coefficients are scalars (no frequency_cv yet
        on Crossover) so the voice branch shares one coeff set across
        all V parallel biquads -- only the per-voice (x1, x2, y1, y2)
        memory differs.
        """
        # collapse=False so a voice-aware (V, F) audio input reaches us
        # with the voice axis intact.
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            zero = np.zeros(frames, dtype=np.float32)
            return {"low": zero, "high": zero.copy()}

        if src.ndim == 2:
            return self._render_crossover_voice(module, frames, src)
        return self._render_crossover_mono(module, frames, src)

    def _crossover_coeffs(self, freq):
        """Compute the LR4 building-block biquad coefficients.

        Returns ``(lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n)``
        already normalized by a0. Shared between the mono and voice
        branches so the coefficient math lives in exactly one place.
        """
        sr = self.sample_rate
        freq = max(20.0, min(freq, sr * 0.45))
        q = 1.0 / (2.0 ** 0.5)  # Butterworth -> Q ~ 0.7071

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
        return lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n

    def _render_crossover_mono(self, module, frames, src):
        """Mono fast path -- scalar state, output 1D low + high.

        Functionally unchanged from the pre-slice-3b.2 implementation;
        the scalar inner loop is exactly the same so every existing
        Crossover test passes bit-for-bit identically.
        """
        state = self._state.setdefault(
            module.id,
            {
                "lp1_x1": 0.0, "lp1_x2": 0.0, "lp1_y1": 0.0, "lp1_y2": 0.0,
                "lp2_x1": 0.0, "lp2_x2": 0.0, "lp2_y1": 0.0, "lp2_y2": 0.0,
                "hp1_x1": 0.0, "hp1_x2": 0.0, "hp1_y1": 0.0, "hp1_y2": 0.0,
                "hp2_x1": 0.0, "hp2_x2": 0.0, "hp2_y1": 0.0, "hp2_y2": 0.0,
            },
        )
        # If state belongs to the voice branch from a previous call
        # (different audio shape), discard and reinit to mono shape.
        if "lp1_x1_arr" in state:
            state.clear()
            state.update(
                {
                    "lp1_x1": 0.0, "lp1_x2": 0.0, "lp1_y1": 0.0, "lp1_y2": 0.0,
                    "lp2_x1": 0.0, "lp2_x2": 0.0, "lp2_y1": 0.0, "lp2_y2": 0.0,
                    "hp1_x1": 0.0, "hp1_x2": 0.0, "hp1_y1": 0.0, "hp1_y2": 0.0,
                    "hp2_x1": 0.0, "hp2_x2": 0.0, "hp2_y1": 0.0, "hp2_y2": 0.0,
                }
            )

        freq = float(module.params.get("frequency", 1000.0))
        lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n = (
            self._crossover_coeffs(freq)
        )

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

    def _render_crossover_voice(self, module, frames, src):
        """Voice-aware path -- V parallel cascaded biquads, output (V, F).

        Inner per-sample loop is still serial in time (cascaded biquads
        are causal); the per-voice updates inside each sample are
        vectorized across V via numpy broadcasting. The inner recurrence
        is identical to the mono path -- the only difference is that
        ``x``, the intermediate stage outputs, and the (x1, x2, y1, y2)
        memories are ``(V,)`` arrays. Coefficients stay scalar because
        Crossover has no frequency_cv yet, so the same numbers apply to
        every voice; broadcast handles it.

        For V=16 and frames=512 the per-iteration cost is essentially
        identical to the mono path -- numpy makes a (V,)-wide multiply-
        add basically free at this size, same trade-off as the filter
        voice path.
        """
        V = src.shape[0]
        state = self._state.setdefault(module.id, {})

        # Reinit if state belongs to the mono branch or the voice
        # count changed (latter is paranoia -- V is always
        # _MAX_VOICES today).
        needs_reinit = (
            "lp1_x1_arr" not in state
            or state["lp1_x1_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            for k in (
                "lp1_x1_arr", "lp1_x2_arr", "lp1_y1_arr", "lp1_y2_arr",
                "lp2_x1_arr", "lp2_x2_arr", "lp2_y1_arr", "lp2_y2_arr",
                "hp1_x1_arr", "hp1_x2_arr", "hp1_y1_arr", "hp1_y2_arr",
                "hp2_x1_arr", "hp2_x2_arr", "hp2_y1_arr", "hp2_y2_arr",
            ):
                state[k] = np.zeros(V, dtype=np.float64)

        freq = float(module.params.get("frequency", 1000.0))
        lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n = (
            self._crossover_coeffs(freq)
        )

        low = np.empty((V, frames), dtype=np.float32)
        high = np.empty((V, frames), dtype=np.float32)

        lp1_x1 = state["lp1_x1_arr"]; lp1_x2 = state["lp1_x2_arr"]
        lp1_y1 = state["lp1_y1_arr"]; lp1_y2 = state["lp1_y2_arr"]
        lp2_x1 = state["lp2_x1_arr"]; lp2_x2 = state["lp2_x2_arr"]
        lp2_y1 = state["lp2_y1_arr"]; lp2_y2 = state["lp2_y2_arr"]
        hp1_x1 = state["hp1_x1_arr"]; hp1_x2 = state["hp1_x2_arr"]
        hp1_y1 = state["hp1_y1_arr"]; hp1_y2 = state["hp1_y2_arr"]
        hp2_x1 = state["hp2_x1_arr"]; hp2_x2 = state["hp2_x2_arr"]
        hp2_y1 = state["hp2_y1_arr"]; hp2_y2 = state["hp2_y2_arr"]

        for n in range(frames):
            x = src[:, n].astype(np.float64)  # (V,)
            # LP stage 1
            y = lp_b0 * x + lp_b1 * lp1_x1 + lp_b2 * lp1_x2 - a1n * lp1_y1 - a2n * lp1_y2
            lp1_x2 = lp1_x1; lp1_x1 = x
            lp1_y2 = lp1_y1; lp1_y1 = y
            # LP stage 2
            z = lp_b0 * y + lp_b1 * lp2_x1 + lp_b2 * lp2_x2 - a1n * lp2_y1 - a2n * lp2_y2
            lp2_x2 = lp2_x1; lp2_x1 = y
            lp2_y2 = lp2_y1; lp2_y1 = z
            low[:, n] = z
            # HP stage 1
            u = hp_b0 * x + hp_b1 * hp1_x1 + hp_b2 * hp1_x2 - a1n * hp1_y1 - a2n * hp1_y2
            hp1_x2 = hp1_x1; hp1_x1 = x
            hp1_y2 = hp1_y1; hp1_y1 = u
            # HP stage 2
            v = hp_b0 * u + hp_b1 * hp2_x1 + hp_b2 * hp2_x2 - a1n * hp2_y1 - a2n * hp2_y2
            hp2_x2 = hp2_x1; hp2_x1 = u
            hp2_y2 = hp2_y1; hp2_y1 = v
            high[:, n] = v

        state["lp1_x1_arr"] = lp1_x1; state["lp1_x2_arr"] = lp1_x2
        state["lp1_y1_arr"] = lp1_y1; state["lp1_y2_arr"] = lp1_y2
        state["lp2_x1_arr"] = lp2_x1; state["lp2_x2_arr"] = lp2_x2
        state["lp2_y1_arr"] = lp2_y1; state["lp2_y2_arr"] = lp2_y2
        state["hp1_x1_arr"] = hp1_x1; state["hp1_x2_arr"] = hp1_x2
        state["hp1_y1_arr"] = hp1_y1; state["hp1_y2_arr"] = hp1_y2
        state["hp2_x1_arr"] = hp2_x1; state["hp2_x2_arr"] = hp2_x2
        state["hp2_y1_arr"] = hp2_y1; state["hp2_y2_arr"] = hp2_y2

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
