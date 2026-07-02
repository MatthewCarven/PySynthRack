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

import math
import queue
import threading
import wave
from typing import Any

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

from . import media
from scipy.io import wavfile

from ..core.patch import Patch
from ..modules.keyboard import midi_to_freq
from ..modules.cv_keyboard import CV_REFERENCE_NOTE, KEY_GATE_NAMES
from ..modules.cv_gates import KEY_CV_NAMES
from .backend import AudioBackend

# Imported lazily so a missing PortAudio install doesn't crash module import.
try:
    import sounddevice as sd  # type: ignore
    _HAS_SOUNDDEVICE = True
except Exception:  # pragma: no cover - environment-dependent
    sd = None  # type: ignore[assignment]
    _HAS_SOUNDDEVICE = False


# ---------------------------------------------------------------------------
# 4x oversampling for nonlinear stages (Distortion, Waveshaper)
#
# A saturating or folding curve generates harmonics with no bandwidth
# limit; run at the native rate, everything past Nyquist folds straight
# back into the audible band as inharmonic hash. So the nonlinear step
# runs at 4x: zero-stuff -> low-pass -> curve -> low-pass -> decimate.
# Both low-passes are the same linear-phase FIR run STREAMING via
# lfilter with per-voice zi carry, so the result is block-size
# independent and voice rows are fully independent. The FIR length is
# chosen so the total group delay is an integer number of BASE-rate
# samples (2 * 32 taps / factor 4 = 16), letting a dry path be
# delay-compensated exactly.
# ---------------------------------------------------------------------------

_OS_FACTOR = 4
_OS_TAPS = 65  # (65-1)/2 = 32 -> 8 base samples per filter, 16 total
_OS_LATENCY = 2 * ((_OS_TAPS - 1) // 2) // _OS_FACTOR
_OS_FIR = firwin(_OS_TAPS, 0.9 / _OS_FACTOR)  # band edge ~10% under base Nyquist


class _Oversampler4:
    """Streaming 4x up/down pair for one module's voice bank.

    ``up`` zero-stuffs (x4 gain restored) and low-passes; ``down``
    low-passes and takes every 4th sample. Filter state is carried per
    voice across blocks. Because the block length is decimated as
    ``[..., ::4]`` and 4*F is always divisible by 4, the decimation
    phase is identical for every block size.
    """

    def __init__(self, voices: int):
        self._zi_up = np.zeros((voices, _OS_TAPS - 1))
        self._zi_dn = np.zeros((voices, _OS_TAPS - 1))

    @property
    def voices(self) -> int:
        return self._zi_up.shape[0]

    def up(self, x):
        """(V, F) base-rate -> (V, 4F) oversampled."""
        v, f = x.shape
        stuffed = np.zeros((v, f * _OS_FACTOR))
        stuffed[:, ::_OS_FACTOR] = x * _OS_FACTOR
        y, self._zi_up = lfilter(_OS_FIR, [1.0], stuffed, axis=-1, zi=self._zi_up)
        return y

    def down(self, y):
        """(V, 4F) oversampled -> (V, F) base-rate."""
        z, self._zi_dn = lfilter(_OS_FIR, [1.0], y, axis=-1, zi=self._zi_dn)
        return z[:, ::_OS_FACTOR]


# One-pole DC blocker (y[n] = x[n] - x[n-1] + R*y[n-1]) for asymmetric
# curves: they shift the waveform's average off zero, and that offset
# would eat headroom downstream. ~3.5 Hz corner at 44.1 kHz.
_DC_R = 0.9995


def _dc_block(x, zi):
    """Streaming DC blocker. x (V, F); zi (V, 1) carried by the caller."""
    return lfilter([1.0, -1.0], [1.0, -_DC_R], x, axis=-1, zi=zi)


class _GrainShifter:
    """One voice's streaming WSOLA pitch-shift engine (time-preserving).

    Time-stretches the input by the pitch ratio via waveform-similarity
    overlap-add (grains nudged to where they best line up with the
    previous one, so overlap joins stay phase-continuous), then resamples
    by the same ratio to restore the original duration -- the net result
    is a pitch shift that keeps the speed/length. Block-streaming: all
    state (input ring, stretched-output ring, analysis + read pointers)
    persists across :meth:`process` calls. See modules/pitch_shifter.py
    for the musical description.

    One engine handles one channel; the renderer keeps a list of these,
    one per voice slot, so a single voice is bit-identical to the mono
    render (same deterministic ops).
    """

    def __init__(self, grain: int, overlap: int, head: int) -> None:
        self.Lg = max(8, int(grain))
        self.Hs = max(1, self.Lg // max(1, int(overlap)))
        self.Lov = max(1, self.Lg - self.Hs)
        self.seek = max(1, self.Hs // 2)
        self.win = np.hanning(self.Lg)
        self.Lin = self.Lg + self.seek + head + 64
        self.Lstr = 2 * self.Lg + head + 64
        self.ib = np.zeros(self.Lin)        # input ring
        self.ss = np.zeros(self.Lstr)       # stretched signal ring (OLA accum)
        self.sw = np.zeros(self.Lstr)       # stretched window-sum ring
        self.iw = 0                          # total input samples written
        self.onset = 0                       # synth index of next grain
        self.final = 0                       # stretched samples finalized
        self.a = 0.0                         # analysis pointer (abs input idx)
        self.tgt = np.zeros(self.Lov)        # similarity-search target
        self.have_tgt = False
        self.rp = 0.0                        # resample read ptr (abs stretched)
        self.zeroed = 0                      # stretched idx zeroed up to
        self.primed = False
        self.bias = 1e-3                     # smallest-shift tie-break bias

    def _produce_one(self, r: float) -> bool:
        Lg, Hs, Lov, seek = self.Lg, self.Hs, self.Lov, self.seek
        Ha = max(1, int(round(Hs / r)))
        c = int(round(self.a))
        if c + seek + Lg > self.iw:                      # not enough input yet
            return False
        if c - seek < self.iw - self.Lin + 2:            # would underflow ring
            return False
        if not self.have_tgt:
            d0 = 0
        else:
            seg = self.ib[(c - seek + np.arange(2 * seek + Lov)) % self.Lin]
            dot = np.correlate(seg, self.tgt, "valid")               # (2*seek+1,)
            cs = np.concatenate([[0.0], np.cumsum(seg * seg)])
            nrm = np.sqrt(np.maximum(cs[Lov:] - cs[:-Lov], 1e-12))[: 2 * seek + 1]
            tn = float(np.linalg.norm(self.tgt)) + 1e-9
            ncc = dot / (nrm * tn) - self.bias * np.abs(np.arange(-seek, seek + 1)) / seek
            d0 = int(np.argmax(ncc)) - seek
        sidx = c + d0
        if sidx + Lg > self.iw:
            sidx = self.iw - Lg
        ring = (self.onset + np.arange(Lg)) % self.Lstr
        self.ss[ring] += self.win * self.ib[(sidx + np.arange(Lg)) % self.Lin]
        self.sw[ring] += self.win
        self.tgt = self.ib[(sidx + Hs + np.arange(Lov)) % self.Lin].copy()
        self.have_tgt = True
        self.a = sidx + Ha
        self.onset += Hs
        self.final = self.onset
        return True

    def process(self, x: np.ndarray, r: float) -> np.ndarray:
        """Push one input block (1D float64), return the shifted block."""
        F = x.shape[0]
        if F == 0:
            return np.zeros(0, dtype=np.float64)
        self.ib[(self.iw + np.arange(F)) % self.Lin] = x
        self.iw += F
        if not self.primed:
            while self.final < self.Lg:
                if not self._produce_one(r):
                    break
            if self.final >= self.Lg:
                self.primed = True
                self.rp = 0.0
                self.zeroed = 0
        out = np.zeros(F, dtype=np.float64)
        if self.primed:
            need = self.rp + r * F + 2.0
            guard = 0
            while self.final < need:
                if not self._produce_one(r):
                    break
                guard += 1
                if guard > 20000:
                    break
            pos = self.rp + np.arange(F) * r
            pos = np.minimum(pos, self.final - 1.0001)   # underrun guard
            i0 = np.floor(pos).astype(np.int64)
            fr = pos - i0
            w0 = self.sw[i0 % self.Lstr]
            w1 = self.sw[(i0 + 1) % self.Lstr]
            v0 = np.where(w0 > 1e-6, self.ss[i0 % self.Lstr] / np.where(w0 > 1e-6, w0, 1.0), 0.0)
            v1 = np.where(w1 > 1e-6, self.ss[(i0 + 1) % self.Lstr] / np.where(w1 > 1e-6, w1, 1.0), 0.0)
            out = v0 * (1.0 - fr) + v1 * fr
            self.rp = min(self.rp + r * F, float(self.final))
            tz = int(np.floor(self.rp)) - 1
            if tz > self.zeroed:
                sl = np.arange(self.zeroed, tz) % self.Lstr
                self.ss[sl] = 0.0
                self.sw[sl] = 0.0
                self.zeroed = tz
        return out

    def dry_tap(self, F: int, Dc: int) -> np.ndarray:
        """Latency-compensated dry read of the most recent block."""
        dp = (self.iw - F) + np.arange(F) - Dc
        d0 = np.floor(dp).astype(np.int64)
        df = dp - d0
        return self.ib[d0 % self.Lin] * (1.0 - df) + self.ib[(d0 + 1) % self.Lin] * df


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
        # CV meter levels for the UI. The audio thread writes one
        # scalar (block-mean) per cv-kind output port into a fresh dict
        # each block, then swaps the reference in atomically; the GUI
        # thread reads a snapshot. No lock — a stale meter frame is
        # harmless, and reference assignment is atomic under the GIL.
        # ``_cv_output_ports`` is the precomputed (module_id, port) list
        # of cv outputs, rebuilt each compile so render_block doesn't
        # have to re-derive signal kinds per block.
        self._cv_output_ports: list[tuple[int, str]] = []
        self._meter_levels: dict[tuple[int, str], float] = {}
        # Latest per-Meter-module peak envelope (linear amplitude),
        # written by the audio thread, read by the GUI as dB. Keys are
        # created in compile() (GUI thread) so the audio thread only ever
        # updates values -- snapshot_audio_levels can copy without a lock.
        self._audio_levels: dict[int, float] = {}
        # Latest per-Meter channel triples ``(level, hold, clip)`` --
        # bar level (in the module's mode), peak-hold tick, clip lamp --
        # as ``(left, right)`` with ``right`` None while ``in_r`` is
        # unpatched. Same no-lock discipline as ``_audio_levels``: keys
        # made in compile(), the audio thread swaps immutable tuples.
        self._audio_meter_state: dict[int, tuple] = {}
        # Latest captured input block (frames, channels) from the
        # duplex stream's callback, or None on an output-only stream.
        # MicInput's renderer reads it; reset on stop so a stale block
        # can't leak into the next run.
        self._input_block: np.ndarray | None = None

    # ----- availability ----------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        return _HAS_SOUNDDEVICE

    # ----- compile ---------------------------------------------------------

    def compile(self, patch: Patch) -> None:
        with self._lock:
            self._patch = patch
            self._topo_order = self._topological_sort(patch)
            # Precompute which output ports carry CV, for the UI meters.
            cv_ports: list[tuple[int, str]] = []
            for mid, module in patch.modules.items():
                for port in module.output_ports:
                    if port.signal_kind == "cv":
                        cv_ports.append((mid, port.name))
            self._cv_output_ports = cv_ports
            self._meter_levels = {}
            self._audio_levels = {
                mid: 0.0
                for mid, m in patch.modules.items()
                if m.TYPE == "meter"
            }
            zero_ch = (0.0, 0.0, False)
            self._audio_meter_state = {
                mid: (
                    zero_ch,
                    zero_ch
                    if any(c.dst_port == "in_r" for c in patch.cables_into(mid))
                    else None,
                )
                for mid, m in patch.modules.items()
                if m.TYPE == "meter"
            }
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
        # Full-duplex only when a mic module is present; otherwise the
        # cheaper output-only stream (no input device / permission
        # needed). A duplex open that fails (no device, rate mismatch,
        # permission denied) falls back to output-only so the rest of
        # the patch still plays and MicInput just renders silence.
        mic_modules = [
            m for m in self._patch.modules.values() if m.TYPE == "mic_input"
        ]
        if mic_modules:
            in_device, in_channels = self._resolve_mic_input(mic_modules[0])
            try:
                self._stream = sd.Stream(
                    samplerate=self.sample_rate,
                    blocksize=self.block_size,
                    device=(in_device, None),
                    channels=(in_channels, 2),
                    dtype="float32",
                    callback=self._duplex_callback,
                )
                self._stream.start()
                self._running = True
                return
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "MicInput: duplex stream open failed (%s); falling "
                    "back to output-only — mic will be silent.", e
                )
                self._stream = None
                self._input_block = None
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
        # Drop any captured input so a stale block can't leak into the
        # next start() (which may be output-only).
        self._input_block = None
        # Close any active disk writers so their WAV headers get
        # finalized when the user hits Stop on the transport.
        for mid in list(self._state.keys()):
            if self._state_types.get(mid) == "disk_writer":
                self._close_disk_writer_state(self._state[mid])
        # Reset file-player playheads so the next start() replays a
        # one-shot from the top instead of resuming past its end.
        for mid in list(self._state.keys()):
            if self._state_types.get(mid) == "file_player":
                self._state[mid]["pos"] = 0
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
        self._fill_output(outdata, frames)

    def _duplex_callback(
        self, indata: np.ndarray, outdata: np.ndarray, frames: int, time, status
    ) -> None:
        """Full-duplex callback: stash the captured input, then render.

        ``indata`` is (frames, in_channels) and is only valid for the
        duration of this call, which is fine — MicInput's renderer reads
        it synchronously within the same render_block below.
        """
        if status:
            print(f"[NumpyBackend] stream status: {status}")
        self._input_block = indata
        self._fill_output(outdata, frames)

    def _fill_output(self, outdata: np.ndarray, frames: int) -> None:
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
    # The stereo sink is drained separately (pan/width/pan_cv need more
    # than a channel-flag pair; see _drain_stereo_speaker).
    _STEREO_SPEAKER = "stereo_speaker_output"

    def render_block(self, frames: int) -> np.ndarray | None:
        """Pure-function rendering of one block. Used by the audio callback
        and by offline tests (no PortAudio device needed)."""
        with self._lock:
            patch = self._patch
            order = list(self._topo_order)
            cv_ports = list(self._cv_output_ports)
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

        # CV meters: one block-mean scalar per cv output port. Cheap
        # (a handful of ports), and only touches buffers already built.
        # Voice-aware (V, F) buffers collapse via a full mean. Build a
        # fresh dict and swap the reference so the GUI never sees a
        # half-updated map.
        if cv_ports:
            levels: dict[tuple[int, str], float] = {}
            for key in cv_ports:
                buf = buffers.get(key)
                if buf is not None and buf.size:
                    levels[key] = float(np.mean(buf))
            self._meter_levels = levels

        out = np.zeros((frames, 2), dtype=np.float32)
        for module in patch.modules.values():
            if module.TYPE == self._STEREO_SPEAKER:
                self._drain_stereo_speaker(module, frames, buffers, patch, out)
                continue
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

    def _drain_stereo_speaker(self, module, frames, buffers, patch, out):
        """Mix one StereoSpeakerOutput into the master bus, in place.

        Two source modes, decided by whether ``in_r`` is cabled:

        * MONO (``in_l`` only): constant-power pan. The pan position p
          (param + cv_depth * pan_cv, clamped to [-1, 1]) maps to an
          angle theta = (p + 1) * pi/4, and the source lands as
          (cos theta, sin theta) -- equal power everywhere, -3 dB in
          the middle, so a sweep doesn't pump.
        * STEREO (``in_r`` cabled): width first, then balance. Width is
          mid/side -- M = (L+R)/2, S = (L-R)/2 * width -- skipped
          entirely at width == 1 so the default is bit-exact. Balance
          attenuates only the far side with a cosine taper
          (gL = cos(max(p, 0) * pi/2), gR mirrored): unity at centre,
          smooth fade to one side at the extremes.

        ``pan_cv`` and ``width_cv`` are per-sample, both scaled by the
        shared ``cv_depth`` (the Reverb's paired-CV convention; a (V, F)
        buffer is averaged across voices -- pan and width are single
        global controls, like Loudness's level_cv). The width == 1
        skip only applies while ``width_cv`` is silent, so the
        bit-exact default survives until a cable actually modulates
        the width. Audio inputs sum their voice axis (the implicit-sum
        rule). ``gain`` is applied last; the master bus clip at +-1
        happens in render_block for all sinks together. Stateless, so
        block-size independence is structural.
        """
        left = self._input_buffer(patch, buffers, module.id, "in_l", collapse=False)
        right = self._input_buffer(patch, buffers, module.id, "in_r", collapse=False)
        r_cabled = any(
            c.dst_port == "in_r" for c in patch.cables_into(module.id)
        )
        if left is None and right is None:
            return
        if left is not None and left.ndim == 2:
            left = left.sum(axis=0)
        if right is not None and right.ndim == 2:
            right = right.sum(axis=0)

        pan = float(module.params.get("pan", 0.0))
        width = float(module.params.get("width", 1.0))
        width = min(max(width, 0.0), 2.0)
        gain = float(module.params.get("gain", 1.0))
        cv_depth = float(module.params.get("cv_depth", 1.0))

        cv = self._input_buffer(patch, buffers, module.id, "pan_cv", collapse=False)
        if cv is not None and cv.size and cv_depth != 0.0:
            if cv.ndim == 2:
                cv = cv.mean(axis=0)
            p = np.clip(pan + cv_depth * cv, -1.0, 1.0)
        else:
            p = min(max(pan, -1.0), 1.0)

        wcv = self._input_buffer(
            patch, buffers, module.id, "width_cv", collapse=False
        )
        if wcv is not None and wcv.size and cv_depth != 0.0:
            if wcv.ndim == 2:
                wcv = wcv.mean(axis=0)
            w = np.clip(width + cv_depth * wcv, 0.0, 2.0)
            width_active = True  # vector width: mid/side always runs
        else:
            w = width
            width_active = width != 1.0

        if not r_cabled:
            # Mono source: constant-power placement.
            mono = left if left is not None else np.zeros(frames, dtype=np.float32)
            theta = (p + 1.0) * (np.pi / 4.0)
            l_mix = mono * np.cos(theta) * gain
            r_mix = mono * np.sin(theta) * gain
        else:
            l_buf = left if left is not None else np.zeros(frames, dtype=np.float32)
            r_buf = right if right is not None else np.zeros(frames, dtype=np.float32)
            if width_active:
                mid = (l_buf + r_buf) * 0.5
                side = (l_buf - r_buf) * (0.5 * w)
                l_buf = mid + side
                r_buf = mid - side
            g_l = np.cos(np.maximum(p, 0.0) * (np.pi / 2.0))
            g_r = np.cos(np.maximum(-p, 0.0) * (np.pi / 2.0))
            l_mix = l_buf * g_l * gain
            r_mix = r_buf * g_r * gain

        out[:, 0] += l_mix
        out[:, 1] += r_mix

    def snapshot_meter_levels(self) -> dict[tuple[int, str], float]:
        """GUI hook: a copy of the latest per-cv-port block-mean levels.

        Keyed by ``(module_id, output_port_name)``. Empty until the
        first block renders, or when no patch carries CV outputs. The
        copy keeps the caller isolated from the audio thread's next
        reference swap.
        """
        return dict(self._meter_levels)

    def snapshot_audio_levels(self) -> dict[int, float]:
        """GUI hook: latest per-Meter-module peak envelope (linear amp).

        Keyed by module_id. Values are a fast-attack/slow-decay peak of
        the meter's input, 0..~1; the GUI converts to dBFS. Keys are
        stable between compiles (created on the GUI thread), so this
        copy never races the audio thread's value writes.
        """
        return dict(self._audio_levels)

    def snapshot_audio_meters(self) -> dict[int, tuple]:
        """GUI hook: latest per-Meter channel meter triples.

        Keyed by module_id; each value is ``(left, right)`` where a
        channel is ``(level, hold, clip)`` -- the bar level in the
        module's ``mode``, the peak-hold tick level (both linear amps;
        the GUI converts to dBFS) and whether the clip lamp is lit --
        and ``right`` is None while ``in_r`` is unpatched (the GUI hides
        the second bar). Keys are stable between compiles (created on
        the GUI thread) and the audio thread swaps whole immutable
        tuples, so this copy never races the writes.
        """
        return dict(self._audio_meter_state)

    def snapshot_file_positions(self) -> dict[int, tuple[float, float]]:
        """GUI hook: each ``file_player``'s playhead as ``(elapsed, total)``
        seconds, keyed by module id.

        ``total`` is ``0.0`` until the file has been decoded (lazily, on the
        first render) and for an empty/unreadable path; ``elapsed`` is
        clamped to ``total`` once a one-shot has run off the end. The lock
        is taken only to copy the state mapping so a concurrent ``compile``
        can't resize it mid-iteration -- ``pos`` itself is written by the
        audio thread without the lock, but an int read is atomic under the
        GIL and a marginally stale playhead is harmless for a readout.
        """
        with self._lock:
            items = list(self._state.items())
            types = dict(self._state_types)
        sr = float(self.sample_rate)
        out: dict[int, tuple[float, float]] = {}
        for mid, st in items:
            if types.get(mid) != "file_player":
                continue
            samples = st.get("samples")
            if samples is None or samples.shape[1] == 0:
                out[mid] = (0.0, 0.0)
                continue
            n = samples.shape[1]
            elapsed = min(int(st.get("pos", 0)), n) / sr
            out[mid] = (elapsed, n / sr)
        return out

    # ----- per-module rendering -------------------------------------------

    def _render_module(self, module, frames, buffers, patch):
        if module.TYPE == "oscillator":
            return self._render_oscillator(module, frames, buffers, patch)
        if module.TYPE == "keyboard":
            return self._render_keyboard(module, frames)
        if module.TYPE == "cv_keyboard":
            return self._render_cv_keyboard(module, frames)
        if module.TYPE == "cv_gates":
            return self._render_cv_gates(module, frames)
        if module.TYPE == "clock":
            return self._render_clock(module, frames)
        if module.TYPE == "sequencer":
            return self._render_sequencer(module, frames, buffers, patch)
        if module.TYPE == "midi_input":
            return self._render_midi_input(module, frames)
        if module.TYPE == "filter":
            return self._render_filter(module, frames, buffers, patch)
        if module.TYPE == "adsr":
            return self._render_adsr(module, frames, buffers, patch)
        if module.TYPE == "ad_envelope":
            return self._render_ad(module, frames, buffers, patch)
        if module.TYPE == "vca":
            return self._render_vca(module, frames, buffers, patch)
        if module.TYPE == "audio_to_cv":
            return self._render_audio_to_cv(module, frames, buffers, patch)
        if module.TYPE == "cv_to_audio":
            return self._render_cv_to_audio(module, frames, buffers, patch)
        if module.TYPE == "schmitt":
            return self._render_schmitt(module, frames, buffers, patch)
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
        if module.TYPE == "constant":
            return self._render_constant(module, frames, buffers, patch)
        if module.TYPE == "cv_scale":
            return self._render_cv_scale(module, frames, buffers, patch)
        if module.TYPE == "cv_offset":
            return self._render_cv_offset(module, frames, buffers, patch)
        if module.TYPE == "sample_hold":
            return self._render_sample_hold(module, frames, buffers, patch)
        if module.TYPE == "noise":
            return self._render_noise(module, frames, buffers, patch)
        if module.TYPE == "crossover":
            return self._render_crossover(module, frames, buffers, patch)
        if module.TYPE == "parametric_eq":
            return self._render_parametric_eq(module, frames, buffers, patch)
        if module.TYPE == "motion_eq":
            return self._render_motion_eq(module, frames, buffers, patch)
        if module.TYPE == "sweep_eq":
            return self._render_sweep_eq(module, frames, buffers, patch)
        if module.TYPE == "tilt_eq":
            return self._render_tilt_eq(module, frames, buffers, patch)
        if module.TYPE == "meter":
            return self._render_meter(module, frames, buffers, patch)
        if module.TYPE == "chorus":
            return self._render_chorus(module, frames, buffers, patch)
        if module.TYPE == "flanger":
            return self._render_flanger(module, frames, buffers, patch)
        if module.TYPE == "phaser":
            return self._render_phaser(module, frames, buffers, patch)
        if module.TYPE == "delay":
            return self._render_delay(module, frames, buffers, patch)
        if module.TYPE == "reverb":
            return self._render_reverb(module, frames, buffers, patch)
        if module.TYPE == "loudness":
            return self._render_loudness(module, frames, buffers, patch)
        if module.TYPE == "distortion":
            return self._render_distortion(module, frames, buffers, patch)
        if module.TYPE == "waveshaper":
            return self._render_waveshaper(module, frames, buffers, patch)
        if module.TYPE == "resampler":
            return self._render_resampler(module, frames, buffers, patch)
        if module.TYPE == "pitch_shifter":
            return self._render_pitch_shifter(module, frames, buffers, patch)
        if module.TYPE == "disk_writer":
            return self._render_disk_writer(module, frames, buffers, patch)
        if module.TYPE == "file_player":
            return self._render_file_player(module, frames, buffers, patch)
        if module.TYPE == "mic_input":
            return self._render_mic_input(module, frames, buffers, patch)
        if (
            module.TYPE in self._SPEAKER_CHANNELS
            or module.TYPE == self._STEREO_SPEAKER
        ):
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

    # ----- clock / sequencer ---------------------------------------------

    # Sequencer step ceiling — mirrors modules.sequencer.MAX_STEPS. Kept
    # local to keep the backend free of a modules-layer import for a single
    # integer (same pattern as _MAX_VOICES).
    _SEQ_MAX_STEPS = 16

    def _render_clock(self, module, frames: int) -> np.ndarray:
        """Tempo-driven gate pulse train, fully vectorized.

        Pulse frequency is ``bpm / 60 * division`` Hz. A float64 phase
        accumulator carries across blocks so pulses stay phase-continuous
        (no drift, no seam at block boundaries); the gate is high for the
        first ``pulse_width`` fraction of each unit phase period. Returns a
        mono ``(frames,)`` gate buffer.
        """
        bpm = max(1e-6, float(module.params.get("bpm", 120.0)))
        division = max(1e-6, float(module.params.get("division", 4.0)))
        pw = min(0.999, max(0.001, float(module.params.get("pulse_width", 0.5))))

        freq = bpm / 60.0 * division  # pulses per second
        inc = freq / self.sample_rate

        st = self._state.setdefault(module.id, {"phase": 0.0})
        phase0 = float(st.get("phase", 0.0))

        # Phase at samples 1..frames (so a fresh clock at phase 0 emits a
        # rising edge on the very first sample — the downstream sequencer
        # then plays step 1 immediately).
        n = np.arange(1, frames + 1, dtype=np.float64)
        frac = np.mod(phase0 + inc * n, 1.0)
        gate = (frac < pw).astype(np.float32)

        st["phase"] = float(np.mod(phase0 + inc * frames, 1.0))
        return gate

    def _render_sequencer(self, module, frames: int, buffers, patch) -> dict:
        """Clock-driven step sequencer → 1V/oct ``cv`` + ``gate``.

        Advances one step per rising edge of the ``clock`` gate; a rising
        edge on ``reset`` rewinds so the next clock plays step 1. The step
        index starts at -1 so the first clock pulse lands on step 1
        (index 0), and wraps modulo ``steps``. ``cv`` holds the current
        step's pitch (``semitones / 12``) for the whole step — sample-and-
        hold, so the note stays in tune while an envelope rings out after
        the gate falls. ``gate`` is high while the clock is high *and* the
        current step is enabled (a disabled step is a rest). Mono output.

        Per-sample because it is an edge-driven counter; cheap (one int
        compare + a couple of lookups per sample) and clear, matching the
        ADSR/Schmitt style in this backend.
        """
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        reset = self._input_buffer(patch, buffers, module.id, "reset")

        steps = int(module.params.get("steps", 8))
        steps = max(1, min(self._SEQ_MAX_STEPS, steps))
        pitches = [
            float(module.params.get(f"step{i}_pitch", 0.0))
            for i in range(1, self._SEQ_MAX_STEPS + 1)
        ]
        ons = [
            bool(module.params.get(f"step{i}_on", True))
            for i in range(1, self._SEQ_MAX_STEPS + 1)
        ]

        st = self._state.setdefault(
            module.id,
            {"idx": -1, "cv": 0.0, "prev_clock": False, "prev_reset": False},
        )
        idx = int(st["idx"])
        cur_cv = float(st["cv"])
        prev_clock = bool(st["prev_clock"])
        prev_reset = bool(st["prev_reset"])

        gate_high = self._GATE_HIGH
        cv_out = np.empty(frames, dtype=np.float32)
        gate_out = np.empty(frames, dtype=np.float32)

        for n in range(frames):
            c = bool(clock[n] > gate_high) if clock is not None else False
            r = bool(reset[n] > gate_high) if reset is not None else False

            if r and not prev_reset:
                idx = -1  # next clock edge plays step 1
            prev_reset = r

            if c and not prev_clock:
                idx = (idx + 1) % steps
                cur_cv = pitches[idx] / 12.0
            prev_clock = c

            cv_out[n] = cur_cv
            gate_out[n] = 1.0 if (c and idx >= 0 and ons[idx]) else 0.0

        st["idx"] = idx
        st["cv"] = cur_cv
        st["prev_clock"] = prev_clock
        st["prev_reset"] = prev_reset
        return {"cv": cv_out, "gate": gate_out}

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

        Both paths run through ``scipy.signal.lfilter`` (filter
        vectorization slices 3+4, 2026-06-12): the serial time
        recurrence executes in C. The voice path filters all V rows in
        one call when the coefficients are shared, or falls back to V
        single-row calls when a (V, F) cutoff_cv gives each voice its
        own coefficients (lfilter can't vary coefficients across rows).
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
        """Mono fast path -- single biquad via ``scipy.signal.lfilter``.

        Filter vectorization slice 3: the per-sample Python loop is
        gone; one lfilter call runs the biquad's time recurrence in C
        (~17x on the 2026-06-09 spike). Output is float64 round-off
        identical to the old loop (see
        ``TestFilterMonoLfilterEquivalence``).

        State stays the raw DF-I history ``(x1, x2, y1, y2)`` rather
        than lfilter's ``zf``. Raw history is coefficient-independent,
        so a block-mean cutoff_cv that changes the coefficients between
        blocks behaves exactly as the old loop did; ``zf`` is defined
        relative to one coefficient set and would diverge on changes.
        At block start the history is converted to the equivalent
        transposed-DF-II initial condition::

            zi1 = b1*x1 + b2*x2 - a1n*y1 - a2n*y2
            zi2 = b2*x1 - a2n*y1

        (this is what ``scipy.signal.lfiltic`` computes -- inlined to
        keep the hot path allocation-light), and after the block the
        history is read back off the input/output tails.
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

        # CV-modulate the cutoff: octaves per CV unit scaled by
        # ``cv_depth`` (default 1.0 = the classic 1 V/oct) --
        # ``cutoff *= 2 ** (cv_depth * mean(cv))``. Block-mean keeps
        # the biquad coefficient recomputation to one pass per block;
        # audio-rate cutoff mod would need a time-varying filter,
        # which a single lfilter call can't express. If cutoff_cv is
        # 2D (a voice-aware source feeding a mono filter), mean over
        # both axes -- same effect as the old collapse=True path.
        if cutoff_cv is not None and cutoff_cv.size > 0:
            cv_depth = float(module.params.get("cv_depth", 1.0))
            cutoff = cutoff * float(2.0 ** (cv_depth * float(np.mean(cutoff_cv))))

        coeffs = self._filter_coeffs(mode, cutoff, q)
        if coeffs is None:
            return src_buf.astype(np.float32)  # unknown mode -> passthrough
        b0, b1, b2, a1n, a2n = coeffs

        if frames == 0:
            return np.empty(0, dtype=np.float32)

        x1 = state["x1"]
        x2 = state["x2"]
        y1 = state["y1"]
        y2 = state["y2"]

        zi = np.array(
            [
                b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2,
                b2 * x1 - a2n * y1,
            ],
            dtype=np.float64,
        )
        x = src_buf.astype(np.float64)
        out64, _zf = lfilter(
            np.array([b0, b1, b2]), np.array([1.0, a1n, a2n]), x, zi=zi
        )

        state["x1"] = float(x[-1])
        state["x2"] = float(x[-2]) if frames >= 2 else x1
        state["y1"] = float(out64[-1])
        state["y2"] = float(out64[-2]) if frames >= 2 else y1

        return out64.astype(np.float32)

    def _render_filter_voice(self, module, frames, src_buf, cutoff_cv):
        """Voice-aware path -- V parallel biquads via lfilter, ``(V, F)``.

        Filter vectorization slice 4. Two shapes:

        * Shared coefficients (static cutoff, or a mono/macro
          cutoff_cv): one lfilter call filters all V rows along the
          time axis with ``zi`` of shape (V, 2) -- the 46x spike case.
        * Per-voice coefficients ((V, F) cutoff_cv -> V cutoffs):
          lfilter cannot vary coefficients across rows, so V
          independent single-row calls. Each row's recurrence still
          runs in C; smaller but real win.

        State design is the mono path's, vectorized (see
        ``_render_filter_mono``): persisted state is the raw DF-I
        history arrays ``(x1_arr, x2_arr, y1_arr, y2_arr)``, each
        ``(V,)`` float64 -- coefficient-independent, so per-block
        cutoff_cv coefficient changes behave exactly as the old loop.
        Converted to the transposed-DF-II ``zi`` at block start (the
        same two lfiltic-identity expressions; numpy broadcasting makes
        the code identical for scalar and ``(V,)`` coefficients) and
        read back off the buffer tails after.
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

        cv_depth = float(module.params.get("cv_depth", 1.0))
        if per_voice_cutoff:
            cv_block_mean = cutoff_cv.mean(axis=1)  # (V,)
            cutoff_per_voice = base_cutoff * np.power(2.0, cv_depth * cv_block_mean)
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
                cutoff = cutoff * float(2.0 ** (cv_depth * float(np.mean(cutoff_cv))))
            coeffs = self._filter_coeffs(mode, cutoff, q)
            if coeffs is None:
                return src_buf.astype(np.float32)
            b0, b1, b2, a1n, a2n = coeffs  # scalars

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        x1 = state["x1_arr"]
        x2 = state["x2_arr"]
        y1 = state["y1_arr"]
        y2 = state["y2_arr"]

        # Raw history -> transposed-DF-II initial conditions. Same
        # identity as the mono path; broadcasting covers both scalar
        # and (V,) coefficients.
        zi1 = b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2  # (V,)
        zi2 = b2 * x1 - a2n * y1                        # (V,)
        x = src_buf.astype(np.float64)                  # (V, F)

        if np.ndim(b0) == 0:
            # Shared coefficients: filter all V rows in one C call.
            out64, _zf = lfilter(
                np.array([b0, b1, b2]),
                np.array([1.0, a1n, a2n]),
                x,
                axis=1,
                zi=np.stack([zi1, zi2], axis=1),
            )
        else:
            # Per-voice coefficients: one C call per row.
            out64 = np.empty((V, frames), dtype=np.float64)
            for v in range(V):
                out64[v], _zf = lfilter(
                    np.array([b0[v], b1[v], b2[v]]),
                    np.array([1.0, a1n[v], a2n[v]]),
                    x[v],
                    zi=np.array([zi1[v], zi2[v]]),
                )

        state["x1_arr"] = x[:, -1].copy()
        state["x2_arr"] = x[:, -2].copy() if frames >= 2 else x1
        state["y1_arr"] = out64[:, -1].copy()
        state["y2_arr"] = out64[:, -2].copy() if frames >= 2 else y1

        return out64.astype(np.float32)

    # ----- ADSR rendering -------------------------------------------------

    # Gate is treated as "high" once it crosses this threshold; this gives
    # us tolerance against fractional gate values (e.g. an LFO-style gate
    # in some future patching) without false triggers on numerical noise.
    _GATE_HIGH = 0.5

    # Pink-noise generation: a 3rd-order IIR that tilts white noise to
    # -3 dB/oct (music-dsp standard coefficients), applied via
    # scipy.signal.lfilter with its state (zi) carried across blocks.
    # _PINK_SCALE RMS-matches the output to uniform white (std ~0.577)
    # so the `amp` param means the same level for both colors.
    _PINK_B = (0.049922035, -0.095993537, 0.050612699, -0.004408786)
    _PINK_A = (1.0, -2.494956002, 2.017265875, -0.522189400)
    _PINK_SCALE = 11.7027

    # Integer phase codes for the vectorized state machine. The mono
    # fast path below still uses strings — we keep them separate so an
    # existing test that introspected the state (none do today, but they
    # could) sees unchanged behaviour.
    # AD (trigger) envelope phases.
    _AD_IDLE = 0
    _AD_ATTACK = 1
    _AD_DECAY = 2
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
        """Voice-aware path — V independent state machines, vectorized.

        2026-06-07 rewrite. The original looped over samples in Python
        (vectorizing only across the 16 voices per sample), which made
        the ADSR own ~63% of the render block under cProfile. This
        version inverts the loop: each voice's gate row is split into
        runs at gate *edges* (rare — typically zero per block), and
        within a run the envelope from a known entry state is a
        deterministic piecewise-linear chain (attack→decay→sustain or
        release→idle) that numpy emits in a handful of array ops.

        Per-sample semantics preserved exactly:
        - edge transitions apply before that sample's level advance (a
          rising edge's sample already moves up by ``attack_step``);
        - stage crossings clamp on the crossing sample (attack's last
          sample outputs exactly 1.0, decay's outputs ``sustain``,
          release's 0.0); the next stage starts the following sample;
        - retrigger continues from the current level (no click);
        - falling edges set ``release_step`` from the level at the edge
          so the tail takes the full release window.

        Only divergence: a run computes ``L0 + k*step`` by multiply
        where the loop accumulated additions — float64 drift orders of
        magnitude below the float32 resolution that leaves this method.
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

        gate_high = gate_buf > self._GATE_HIGH  # (V, F) bool
        out = np.empty((V, frames), dtype=np.float64)

        for v in range(V):
            row = gate_high[v]
            # Edge samples: where the gate differs from the previous
            # sample, seeded with the carried cross-block prev_gate.
            shifted = np.empty(frames, dtype=bool)
            shifted[0] = prev_gate[v]
            shifted[1:] = row[:-1]
            edges = np.flatnonzero(row != shifted)

            ph = int(phase[v])
            lvl = float(level[v])
            rs = float(release_step[v])

            if edges.size == 0:
                # Common case: no gate activity this block — one run.
                ph, lvl = self._adsr_fill_run(
                    out[v], 0, frames, ph, lvl, rs,
                    attack_step, decay_step, sustain,
                )
            else:
                starts = (
                    edges if edges[0] == 0
                    else np.concatenate(([0], edges))
                )
                ends = np.append(starts[1:], frames)
                edge_set = set(int(e) for e in edges)
                for s, e in zip(starts, ends):
                    s = int(s)
                    if s in edge_set:
                        if row[s]:
                            # Rising -> attack from the current level
                            # (retrigger picks up where we were).
                            ph = self._ADSR_ATTACK
                        else:
                            # Falling -> release over the full window
                            # from wherever the envelope is now.
                            rs = lvl / release_samples
                            ph = self._ADSR_RELEASE
                    ph, lvl = self._adsr_fill_run(
                        out[v], s, int(e), ph, lvl, rs,
                        attack_step, decay_step, sustain,
                    )

            phase[v] = ph
            level[v] = lvl
            release_step[v] = rs
            prev_gate[v] = bool(row[-1])

        return out.astype(np.float32)

    def _adsr_fill_run(
        self, seg, pos, end, ph, lvl, rs,
        attack_step, decay_step, sustain,
    ):
        """Fill ``seg[pos:end]`` with the envelope trajectory from entry
        state ``(ph, lvl)``, following the natural stage chain. Returns
        the exit ``(ph, lvl)``.

        Mirrors the per-sample mask cascade exactly. In the mask
        implementation a stage *crossing* cascades within the same
        sample: the sample where attack reaches 1.0 immediately applies
        the decay update too, so the emitted value is
        ``max(1.0 - decay_step, sustain)`` — never a bare 1.0. (This is
        a deliberate divergence from the mono scalar path, which emits
        the clamped 1.0 and starts decay the *next* sample; the voice
        path has always cascaded and downstream tests encode it.)
        Likewise decay's crossing sample emits ``sustain`` and release's
        emits 0.0. Attack therefore contributes only its strictly-
        below-1.0 ramp samples; the crossing sample belongs to decay.

        Stage lengths are analytic (smallest k >= 1 crossing the
        target), so each stage is one ``arange`` + one clamp.
        """
        if ph == self._ADSR_ATTACK and pos < end:
            ka = max(1, int(np.ceil((1.0 - lvl) / attack_step)))
            k = min(ka - 1, end - pos)
            if k > 0:
                seg[pos:pos + k] = np.minimum(
                    lvl + np.arange(1, k + 1, dtype=np.float64) * attack_step,
                    1.0,
                )
                lvl = float(seg[pos + k - 1])
                pos += k
            if pos < end:
                # Crossing falls inside this run: cascade into decay,
                # which emits the crossing sample below.
                ph = self._ADSR_DECAY
                lvl = 1.0
        if ph == self._ADSR_DECAY and pos < end:
            if lvl <= sustain or decay_step <= 0.0:
                # Includes the mid-flight "sustain raised above current
                # level" case: the mask cascade clamps up to sustain on
                # the first sample; maximum() reproduces that.
                kd = 1
            else:
                kd = max(1, int(np.ceil((lvl - sustain) / decay_step)))
            k = min(kd, end - pos)
            seg[pos:pos + k] = np.maximum(
                lvl - np.arange(1, k + 1, dtype=np.float64) * decay_step,
                sustain,
            )
            lvl = float(seg[pos + k - 1])
            pos += k
            if k == kd:
                ph = self._ADSR_SUSTAIN
                lvl = sustain
        if ph == self._ADSR_SUSTAIN and pos < end:
            seg[pos:end] = sustain
            lvl = sustain
            pos = end
        if ph == self._ADSR_RELEASE and pos < end:
            if rs > 0.0:
                kr = max(1, int(np.ceil(lvl / rs)))
                k = min(kr, end - pos)
                seg[pos:pos + k] = np.maximum(
                    lvl - np.arange(1, k + 1, dtype=np.float64) * rs,
                    0.0,
                )
                lvl = float(seg[pos + k - 1])
                pos += k
                if k == kr:
                    ph = self._ADSR_IDLE
                    lvl = 0.0
            elif lvl <= 0.0:
                seg[pos] = 0.0
                lvl = 0.0
                pos += 1
                ph = self._ADSR_IDLE
            else:
                # Degenerate zero-step release with positive level: the
                # per-sample cascade would hold here forever. Preserve.
                seg[pos:end] = lvl
                pos = end
        if ph == self._ADSR_IDLE and pos < end:
            seg[pos:end] = lvl
            pos = end
        return ph, lvl

    def _render_ad(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Trigger-style Attack/Decay envelope (percussion).

        State machine: idle -> attack -> decay -> idle. A rising edge on
        ``trig`` (re)enters attack from the current level; attack ramps
        to 1.0, decay ramps to 0.0, then it idles. The trigger going low
        is ignored -- the decay always runs to completion, which is what
        makes a momentary clock pulse produce a full hit.

        Shape-polymorphic like ADSR: a 1D ``(F,)`` trigger drives one
        envelope and emits ``(F,)``; a 2D ``(V, F)`` trigger maintains V
        independent envelopes and emits ``(V, F)``. The mono and voice
        paths share identical per-sample semantics (one stage update per
        sample, transitions take effect the following sample), so a
        voice row is bit-identical to the mono result for the same gate.
        Durations clamp to a >= 1-sample minimum for stability.
        """
        gate_buf = self._input_buffer(
            patch, buffers, module.id, "trig", collapse=False
        )
        sr = self.sample_rate
        attack_s = max(0.0, float(module.params.get("attack", 0.005)))
        decay_s = max(0.0, float(module.params.get("decay", 0.20)))
        attack_step = 1.0 / max(1.0, attack_s * sr)
        decay_step = 1.0 / max(1.0, decay_s * sr)

        if gate_buf is not None and gate_buf.ndim == 2:
            return self._render_ad_voice(
                module, frames, gate_buf, attack_step, decay_step
            )
        return self._render_ad_mono(
            module, frames, gate_buf, attack_step, decay_step
        )

    def _render_ad_mono(self, module, frames, gate_buf, attack_step, decay_step):
        """Mono scalar reference: one envelope, output ``(F,)``."""
        state = self._state.setdefault(
            module.id, {"phase": self._AD_IDLE, "level": 0.0, "prev_gate": False}
        )
        if "phase_arr" in state:  # was the voice branch -> reinit to mono
            state.clear()
            state.update({"phase": self._AD_IDLE, "level": 0.0, "prev_gate": False})

        out = np.empty(frames, dtype=np.float32)
        phase = state["phase"]
        level = state["level"]
        prev = state["prev_gate"]

        for n in range(frames):
            g = bool(gate_buf[n] > self._GATE_HIGH) if gate_buf is not None else False
            if g and not prev:
                phase = self._AD_ATTACK  # retrigger from current level
            prev = g

            if phase == self._AD_ATTACK:
                level += attack_step
                if level >= 1.0:
                    level = 1.0
                    phase = self._AD_DECAY
            elif phase == self._AD_DECAY:
                level -= decay_step
                if level <= 0.0:
                    level = 0.0
                    phase = self._AD_IDLE
            # idle: level holds at 0.0
            out[n] = level

        state["phase"] = phase
        state["level"] = level
        state["prev_gate"] = prev
        return out

    def _render_ad_voice(self, module, frames, gate_buf, attack_step, decay_step):
        """Voice path: V independent envelopes, output ``(V, F)``.

        Per-sample loop over the block, vectorized across the V voices.
        Each sample applies exactly one stage update per voice based on
        the phase *at the start of the sample* (after the edge check), so
        a voice that crosses attack->decay this sample still emits 1.0
        and only begins decaying next sample -- identical to the mono
        scalar path (asserted in the tests). A per-sample loop (like the
        Crossover voice path) rather than ADSR's run-splitting: simpler
        and plenty fast for the envelope; run-based vectorization is a
        later optimization if profiling ever flags it.
        """
        V = gate_buf.shape[0]
        state = self._state.setdefault(module.id, {})
        needs_reinit = (
            "phase_arr" not in state or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.full(V, self._AD_IDLE, dtype=np.int32)
            state["level_arr"] = np.zeros(V, dtype=np.float64)
            state["prev_gate_arr"] = np.zeros(V, dtype=bool)

        phase = state["phase_arr"]
        level = state["level_arr"]
        prev = state["prev_gate_arr"]

        gate_high = gate_buf > self._GATE_HIGH  # (V, F) bool
        out = np.empty((V, frames), dtype=np.float64)

        for n in range(frames):
            g = gate_high[:, n]
            rising = g & ~prev
            if rising.any():
                phase = np.where(rising, self._AD_ATTACK, phase)
            prev = g

            phase_now = phase  # stage to apply this sample (pre-transition)
            att = phase_now == self._AD_ATTACK
            dec = phase_now == self._AD_DECAY

            if att.any():
                lv = level[att] + attack_step
                top = lv >= 1.0
                lv = np.where(top, 1.0, lv)
                level[att] = lv
                # mark voices that topped out -> decay next sample
                idx = np.flatnonzero(att)
                phase[idx[top]] = self._AD_DECAY
            if dec.any():
                lv = level[dec] - decay_step
                bot = lv <= 0.0
                lv = np.where(bot, 0.0, lv)
                level[dec] = lv
                idx = np.flatnonzero(dec)
                phase[idx[bot]] = self._AD_IDLE

            out[:, n] = level

        state["phase_arr"] = phase
        state["level_arr"] = level
        state["prev_gate_arr"] = prev
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
        # CV-modulate the rate: octaves per CV unit scaled by
        # ``cv_depth`` (default 1.0 = 1 V/oct), block-mean. If rate_cv
        # is 2D for any reason it shouldn't reach this branch -- the
        # dispatcher routes (V, F) to the voice path. mean() over a
        # 1D slice is the same as the old code.
        if rate_cv is not None and rate_cv.size > 0:
            cv_depth = float(module.params.get("cv_depth", 1.0))
            rate = rate * float(2.0 ** (cv_depth * float(np.mean(rate_cv))))

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

        # Per-voice block-mean rate (octaves/unit x cv_depth, default
        # 1 V/oct). Each voice gets its own phase increment for this
        # block.
        cv_depth = float(module.params.get("cv_depth", 1.0))
        cv_block_mean = rate_cv.mean(axis=1)  # (V,)
        rate_per_voice = base_rate * np.power(
            2.0, cv_depth * cv_block_mean.astype(np.float64)
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

    # ----- Schmitt rendering ------------------------------------------------

    def _render_schmitt(self, module, frames: int, buffers, patch) -> np.ndarray:
        """CV → gate Schmitt trigger with hysteresis.

        Rising through ``high`` (strict >) sets the gate; falling
        through ``low`` (strict <) clears it; inside the band the gate
        holds its previous state — the hysteresis that makes a wobbly
        CV usable as a clock without chatter.

        Vectorized by event forward-fill (no per-sample loop): each
        sample is classified +1 (above high), -1 (below low) or 0
        (deadband); the gate at sample n is "was the most recent
        nonzero event a +1", seeded with the carried cross-block
        state via ``np.maximum.accumulate`` over event positions.

        Shape-polymorphic on the CV input's ndim per the voice-aware
        convention: ``(F,)`` in → ``(F,)`` out with scalar held state;
        ``(V, F)`` in → ``(V, F)`` out with per-voice held state.
        Unpatched input emits a constant-low gate. Output is float32
        0.0 / 1.0 (comfortably astride the backend's ``_GATE_HIGH``).
        """
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )

        high = float(module.params.get("high", 0.6))
        # An inverted pair degenerates to a plain comparator at high.
        low = min(float(module.params.get("low", 0.4)), high)

        state = self._state.setdefault(module.id, {"gate": False})

        if cv_in is None:
            return np.zeros(frames, dtype=np.float32)

        if cv_in.ndim == 2:
            V = cv_in.shape[0]
            needs_reinit = (
                "gate_arr" not in state or state["gate_arr"].shape[0] != V
            )
            if needs_reinit:
                state.clear()
                state["gate_arr"] = np.zeros(V, dtype=bool)
            prev = state["gate_arr"][:, None]  # (V, 1)

            ev = np.where(cv_in > high, 1, np.where(cv_in < low, -1, 0))
            pos = np.where(ev != 0, np.arange(frames)[None, :], -1)
            last = np.maximum.accumulate(pos, axis=1)  # (V, F)
            picked = np.take_along_axis(ev, np.maximum(last, 0), axis=1)
            gate = np.where(last >= 0, picked > 0, prev)

            state["gate_arr"] = gate[:, -1].copy()
            return gate.astype(np.float32)

        # Mono path. Discard voice-shaped state if the input collapsed.
        if "gate_arr" in state:
            state.clear()
            state["gate"] = False
        prev_gate = bool(state["gate"])

        ev = np.where(cv_in > high, 1, np.where(cv_in < low, -1, 0))
        pos = np.where(ev != 0, np.arange(frames), -1)
        last = np.maximum.accumulate(pos)
        gate = np.where(last >= 0, ev[np.maximum(last, 0)] > 0, prev_gate)

        state["gate"] = bool(gate[-1])
        return gate.astype(np.float32)

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

        Bipolar CV (phase 2, 2026-06-07): with ``negative_enabled``,
        CV in [-1, 0) maps through an independent mirror curve --
        ``f0_neg`` at CV=0, ``fm_neg`` at CV=-0.5, ``f1_neg`` at
        CV=-1.0 -- with its own ``mode_neg``. CV exactly 0 belongs to
        the positive side; zero-crossing continuity is the user's
        choice (f0 == f0_neg for smooth, different for a step). When
        disabled (default), bipolar CV clamps to [0, 1] exactly as
        phase 1 shipped.
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
        pos = (f0, fm, f1, mode)

        # Phase 2: independent negative-side curve, opt-in. ``neg`` is
        # None when disabled, which keeps the phase-1 [0, 1] clamp.
        neg = None
        if bool(module.params.get("negative_enabled", False)):
            neg = (
                float(module.params.get("f0_neg", f0)),
                float(module.params.get("fm_neg", 440.0)),
                float(module.params.get("f1_neg", 1760.0)),
                str(module.params.get("mode_neg", "log")),
            )

        if cv_in is not None and cv_in.ndim == 2:
            return self._render_cv_to_frequency_voice(
                module, frames, cv_in, pos, neg, waveform
            )
        return self._render_cv_to_frequency_mono(
            module, frames, cv_in, pos, neg, freq_fallback, waveform
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

    @staticmethod
    def _cv_to_hz_mapped(cv, pos, neg):
        """Sign-aware CV→Hz dispatch.

        ``pos`` and ``neg`` are ``(f0, fm, f1, mode)`` tuples. With
        ``neg`` None (negative_enabled False) this is exactly the
        phase-1 positive mapping and its internal [0, 1] clamp.
        Otherwise cv >= 0 maps through ``pos`` and cv < 0 maps through
        ``neg`` on |cv|, so the negative anchors read naturally:
        f0_neg at CV=0⁻, fm_neg at CV=-0.5, f1_neg at CV=-1.0, and CV
        below -1 clamps to f1_neg via the shared [0, 1] clamp on the
        mirrored value.
        """
        if neg is None:
            return NumpyBackend._cv_to_hz(cv, *pos)
        cv64 = cv.astype(np.float64)
        pos_hz = NumpyBackend._cv_to_hz(cv64, *pos)
        neg_hz = NumpyBackend._cv_to_hz(-cv64, *neg)
        return np.where(cv64 >= 0.0, pos_hz, neg_hz)

    def _render_cv_to_frequency_mono(
        self, module, frames, cv_in, pos, neg, freq_fallback, waveform
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
            inst_freq = self._cv_to_hz_mapped(cv_in, pos, neg)  # (F,)
            inst_inc = inst_freq / sr
            phases = (start_phase + np.cumsum(inst_inc)) % 1.0
            state["phase"] = float(phases[-1])
            dt = inst_inc

        wave = self._osc_waveshape(phases, waveform, dt=dt)
        return wave.astype(np.float32)

    def _render_cv_to_frequency_voice(
        self, module, frames, cv_in, pos, neg, waveform
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

        inst_freq = self._cv_to_hz_mapped(cv_in, pos, neg)  # (V, F)
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

            out = master * sum_i (gain_i * cv_i * input_i)

        where ``cv_i`` is the channel's optional ``gain{i}_cv`` input
        (unpatched -> unity). The CV multiplies **per sample**, VCA-style
        — the CV *is* the channel's amplitude (knobless by the house
        rule, like ``vca.cv``), so an ADSR into ``gain2_cv`` swells
        channel 2 and a sequencer lane steps channels in and out.

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
            ch = buf * gain
            cv = self._input_buffer(patch, buffers, module.id, f"gain{idx}_cv")
            if cv is not None and cv.size > 0:
                ch = ch * cv
            out += ch.astype(np.float32)
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

    # ----- CV-utility trio (Constant / CVScale / CVOffset) ----------------

    def _render_constant(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Emit a steady CV level -- a hand-dialed DC source.

        No inputs; fills the block with the scalar ``value`` param.
        Always mono ``(frames,)``: a constant has no voice context of
        its own, and a 1D CV broadcasts cleanly against any per-voice
        ``(V, frames)`` consumer downstream.
        """
        value = float(module.params.get("value", 1.0))
        return np.full(frames, value, dtype=np.float32)

    def _render_cv_scale(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Multiply a CV by a fixed factor: ``out = in * scale``.

        Pure pointwise gain, so shape-polymorphic for free -- ``collapse=
        False`` keeps a voice-aware ``(V, F)`` input intact and a mono
        ``(F,)`` input stays mono. An unpatched input is treated as 0,
        so the output is silence (``0 * scale == 0``).
        """
        scale = float(module.params.get("scale", 1.0))
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if cv_in is None:
            return np.zeros(frames, dtype=np.float32)
        return (cv_in * scale).astype(np.float32)

    def _render_cv_offset(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Add a fixed DC level to a CV: ``out = in + offset``.

        Pure pointwise shift, shape-polymorphic for free. An unpatched
        input is treated as 0, so the output is a constant ``offset``
        (mono) -- which makes an unpatched CVOffset a quick DC source.
        A voice-aware ``(V, F)`` input keeps its shape, the scalar
        ``offset`` broadcasting across the voice axis.
        """
        offset = float(module.params.get("offset", 0.0))
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if cv_in is None:
            return np.full(frames, offset, dtype=np.float32)
        return (cv_in + offset).astype(np.float32)

    # ----- Noise rendering ------------------------------------------------

    def _render_noise(self, module, frames: int, buffers=None, patch=None) -> dict:
        """White or pink noise; same stream on the ``out`` and ``cv`` jacks.

        ``white`` is uniform ``[-1, 1]`` per sample (hard-bounded,
        bright). ``pink`` filters that white through the class-level
        pinking IIR via ``scipy.signal.lfilter`` -- the filter state
        ``zi`` is carried in ``self._state`` across blocks so the
        spectrum stays continuous at block seams -- then scaled by
        ``_PINK_SCALE`` to RMS-match white. Both are multiplied by the
        ``amp`` param.

        A source has no voice context of its own, so the output is
        always mono ``(frames,)`` (like :class:`Constant`); a 1D signal
        broadcasts cleanly against any per-voice consumer. The same
        float32 array is returned under both port names -- consumers
        treat buffers as read-only, exactly as fan-out from any single
        output already does.
        """
        color = str(module.params.get("color", "white"))
        amp = float(module.params.get("amp", 1.0))

        white = np.random.uniform(-1.0, 1.0, frames).astype(np.float32)

        if color == "pink":
            state = self._state.setdefault(module.id, {})
            zi = state.get("pink_zi")
            if zi is None:
                zi = np.zeros(
                    max(len(self._PINK_A), len(self._PINK_B)) - 1, dtype=np.float64
                )
            filtered, zf = lfilter(self._PINK_B, self._PINK_A, white, zi=zi)
            state["pink_zi"] = zf
            sig = (filtered * self._PINK_SCALE).astype(np.float32)
        else:
            # Any non-pink color is white. Drop stale pink state if the
            # color was switched at runtime.
            st = self._state.get(module.id)
            if st is not None:
                st.pop("pink_zi", None)
            sig = white

        if amp != 1.0:
            sig = (sig * amp).astype(np.float32)
        # Same array on both jacks (read-only downstream, like any fan-out).
        return {"out": sig, "cv": sig}

    # ----- SampleHold rendering -------------------------------------------

    def _render_sample_hold(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sample ``in`` on each rising edge of ``trig``; hold between edges.

        Vectorized by forward-fill -- no per-sample loop, the same trick
        the Schmitt trigger uses. A rising edge is ``gate high and the
        previous sample low`` (the previous sample of the first frame is
        the gate state carried from the end of the last block). The held
        value at sample n is the input value sampled at the most recent
        edge at or before n, found with ``np.maximum.accumulate`` over
        edge positions; samples before the first edge keep the value
        carried from the previous block.

        Shape-polymorphic on the inputs (collapse=False keeps the voice
        axis):

          * mono ``(F,)`` in/trig -> ``(F,)`` out, scalar held value and
            scalar held-gate carried across blocks.
          * a ``(V, F)`` on either input -> ``(V, F)`` out, per-voice
            held values and per-voice edge detection; a mono partner
            broadcasts across the voice axis (shared clock + per-voice
            sources, or per-voice clocks + one shared source).

        Conventions: an unpatched ``in`` is treated as 0 (pure S&H, no
        internal noise). An unpatched ``trig`` produces no edges, so the
        output simply holds its last value (0 at startup).
        """
        in_buf = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        trig_buf = self._input_buffer(patch, buffers, module.id, "trig", collapse=False)

        # Voice dimension is set by whichever input carries the voice axis.
        v_in = in_buf.shape[0] if (in_buf is not None and in_buf.ndim == 2) else None
        v_trig = trig_buf.shape[0] if (trig_buf is not None and trig_buf.ndim == 2) else None

        if v_in is None and v_trig is None:
            return self._render_sample_hold_mono(module, frames, in_buf, trig_buf)
        V = v_in if v_in is not None else v_trig
        return self._render_sample_hold_voice(module, frames, in_buf, trig_buf, V)

    def _render_sample_hold_mono(self, module, frames, in_buf, trig_buf) -> np.ndarray:
        """Mono path -- scalar held value + held-gate carried across blocks."""
        state = self._state.setdefault(module.id, {"held": 0.0, "prev_gate": False})
        # Drop voice-shaped state if it leaked from a previous voice call.
        if "held_arr" in state:
            state.clear()
            state["held"] = 0.0
            state["prev_gate"] = False

        held = float(state["held"])

        if trig_buf is None:
            # No clock -> no edges -> hold the last value across the block.
            return np.full(frames, held, dtype=np.float32)

        in_arr = (
            np.zeros(frames, dtype=np.float32)
            if in_buf is None
            else in_buf.astype(np.float32)
        )

        g = trig_buf > self._GATE_HIGH                 # (F,) bool
        g_prev = np.empty(frames, dtype=bool)
        g_prev[0] = bool(state["prev_gate"])
        g_prev[1:] = g[:-1]
        rising = g & ~g_prev                           # (F,) bool

        idx = np.where(rising, np.arange(frames), -1)
        last = np.maximum.accumulate(idx)              # (F,) most-recent edge, -1 before any
        sampled = np.where(last >= 0, in_arr[np.maximum(last, 0)], held)

        state["held"] = float(sampled[-1])
        state["prev_gate"] = bool(g[-1])
        return sampled.astype(np.float32)

    def _render_sample_hold_voice(self, module, frames, in_buf, trig_buf, V) -> np.ndarray:
        """Voice path -- per-voice held values + per-voice held-gate.

        A mono input is broadcast across the V voice rows so a shared
        clock can sample per-voice sources, or per-voice clocks can
        sample one shared source.
        """
        state = self._state.setdefault(module.id, {})
        needs_reinit = (
            "held_arr" not in state or state["held_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["held_arr"] = np.zeros(V, dtype=np.float64)
            state["gate_arr"] = np.zeros(V, dtype=bool)

        held_arr = state["held_arr"]                   # (V,)

        if trig_buf is None:
            return np.broadcast_to(
                held_arr[:, None].astype(np.float32), (V, frames)
            ).copy()

        if trig_buf.ndim == 1:
            trig_2d = np.broadcast_to(trig_buf, (V, frames))
        else:
            trig_2d = trig_buf

        if in_buf is None:
            in_2d = np.zeros((V, frames), dtype=np.float32)
        elif in_buf.ndim == 1:
            in_2d = np.broadcast_to(in_buf.astype(np.float32), (V, frames))
        else:
            in_2d = in_buf.astype(np.float32)

        g = trig_2d > self._GATE_HIGH                  # (V, F) bool
        prev_col = state["gate_arr"][:, None]          # (V, 1)
        g_prev = np.concatenate([prev_col, g[:, :-1]], axis=1)
        rising = g & ~g_prev                           # (V, F)

        idx = np.where(rising, np.arange(frames)[None, :], -1)
        last = np.maximum.accumulate(idx, axis=1)      # (V, F)
        sampled_vals = np.take_along_axis(in_2d, np.maximum(last, 0), axis=1)
        out = np.where(last >= 0, sampled_vals, held_arr[:, None])

        state["held_arr"] = out[:, -1].copy()
        state["gate_arr"] = g[:, -1].copy()
        return out.astype(np.float32)

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
        flat magnitude. Coefficients come from one block-mean freq (a
        static param or a block-meaned ``freq_cv``) so the voice branch
        shares one coeff set across all V parallel biquads -- only the
        per-voice (x1, x2, y1, y2) memory differs.
        """
        # collapse=False so a voice-aware (V, F) audio input reaches us
        # with the voice axis intact.
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            zero = np.zeros(frames, dtype=np.float32)
            return {"low": zero, "high": zero.copy()}

        # freq_cv sweeps the split point 1 V/oct, scaled by cv_depth
        # (octaves per unit) and block-meaned -- the same cadence as the
        # Filter's cutoff_cv and the modulation FX' rate_cv. A single mean
        # over all axes yields one coefficient set shared across voices:
        # the crossover keeps scalar coefficients by design (the voice
        # branch broadcasts them), so a voice-aware freq_cv drives one
        # macro sweep rather than per-voice split points.
        freq = float(module.params.get("freq", 1000.0))
        freq_cv = self._input_buffer(patch, buffers, module.id, "freq_cv")
        if freq_cv is not None and freq_cv.size > 0:
            cv_depth = float(module.params.get("cv_depth", 1.0))
            freq = freq * float(2.0 ** (cv_depth * float(np.mean(freq_cv))))

        if src.ndim == 2:
            return self._render_crossover_voice(module, frames, src, freq)
        return self._render_crossover_mono(module, frames, src, freq)

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

    def _render_crossover_mono(self, module, frames, src, freq):
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

    def _render_crossover_voice(self, module, frames, src, freq):
        """Voice-aware path -- V parallel cascaded biquads, output (V, F).

        Inner per-sample loop is still serial in time (cascaded biquads
        are causal); the per-voice updates inside each sample are
        vectorized across V via numpy broadcasting. The inner recurrence
        is identical to the mono path -- the only difference is that
        ``x``, the intermediate stage outputs, and the (x1, x2, y1, y2)
        memories are ``(V,)`` arrays. Coefficients stay scalar because
        the (block-mean) crossover freq is one number per block, so the
        same coefficients apply to every voice; broadcast handles it.

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

    # ----- ParametricEQ rendering -----------------------------------------

    def _peq_band_params(self, module):
        """Read the per-band (freq, gain_dB, Q) lists off a ParametricEQ.

        Band-count-agnostic: walks ``band{i}_freq`` until one is
        missing, so the module can grow/shrink bands without touching
        the renderer. Returns three equal-length Python lists.
        """
        freqs, gains, qs = [], [], []
        i = 1
        while f"band{i}_freq" in module.params:
            freqs.append(float(module.params[f"band{i}_freq"]))
            gains.append(float(module.params[f"band{i}_gain"]))
            qs.append(float(module.params[f"band{i}_q"]))
            i += 1
        return freqs, gains, qs

    def _peq_coeffs(self, freqs, gains_db, qs):
        """RBJ peaking-EQ biquad coefficients for N bands at once.

        Returns five ``(N,)`` float64 arrays ``(b0, b1, b2, a1n, a2n)``
        already normalized by a0. freq is clamped to (20 Hz,
        0.45*sample_rate) and Q to (0.1, 20). A band at 0 dB gain
        yields identity coefficients (b == a), i.e. an exact
        passthrough -- so unused bands are tonally free.
        """
        sr = self.sample_rate
        f0 = np.clip(np.asarray(freqs, dtype=np.float64), 20.0, sr * 0.45)
        q = np.clip(np.asarray(qs, dtype=np.float64), 0.1, 20.0)
        A = np.power(10.0, np.asarray(gains_db, dtype=np.float64) / 40.0)

        w0 = 2.0 * np.pi * f0 / sr
        cos_w0 = np.cos(w0)
        alpha = np.sin(w0) / (2.0 * q)

        b0 = 1.0 + alpha * A
        b1 = -2.0 * cos_w0
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha / A
        return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    def _render_parametric_eq(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Cascade of N peaking biquads applied to the upstream signal.

        Shape-polymorphic, matching Filter/Crossover. A 1D ``(F,)``
        input runs one cascade and emits ``(F,)``; a 2D ``(V, F)``
        input runs V parallel cascades (one per voice slot, each with
        its own biquad memory) and emits ``(V, F)``. Coefficients are
        param-only (no CV yet) so the same set applies to every voice.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            return np.zeros(frames, dtype=np.float32)
        if src.ndim == 2:
            return self._render_parametric_eq_voice(module, frames, src)
        return self._render_parametric_eq_mono(module, frames, src)

    def _render_parametric_eq_mono(self, module, frames, src, freqs_override=None,
                                   gains_override=None):
        """Mono path -- N cascaded peaking biquads via ``lfilter``.

        Same state design as ``_render_filter_mono`` (filter
        vectorization slices 3+4): persisted state is the raw DF-I
        history ``(x1, x2, y1, y2)``, one entry per band, each a
        ``(N,)`` float64 array. Raw history is coefficient-independent,
        so editing a band's freq/gain/Q between blocks behaves
        cleanly. At each stage the history is converted to the
        transposed-DF-II ``zi`` (the lfiltic identity, inlined), the
        biquad runs in C, and the new history is read off the
        input/output tails. The output of stage k feeds stage k+1.
        """
        freqs, gains, qs = self._peq_band_params(module)
        if freqs_override is not None:
            freqs = freqs_override  # MotionEQ: per-band CV-swept centres
        if gains_override is not None:
            gains = gains_override  # MotionEQ: per-band CV-pushed gains
        n_bands = len(freqs)
        b0, b1, b2, a1n, a2n = self._peq_coeffs(freqs, gains, qs)

        state = self._state.setdefault(module.id, {})
        x1 = state.get("x1")
        needs_reinit = (
            x1 is None
            or x1.ndim != 1
            or x1.shape[0] != n_bands
        )
        if needs_reinit:
            state.clear()
            for k in ("x1", "x2", "y1", "y2"):
                state[k] = np.zeros(n_bands, dtype=np.float64)

        if frames == 0:
            return np.empty(0, dtype=np.float32)

        x1 = state["x1"]; x2 = state["x2"]
        y1 = state["y1"]; y2 = state["y2"]

        x = src.astype(np.float64)
        for k in range(n_bands):
            zi = np.array(
                [
                    b1[k] * x1[k] + b2[k] * x2[k] - a1n[k] * y1[k] - a2n[k] * y2[k],
                    b2[k] * x1[k] - a2n[k] * y1[k],
                ],
                dtype=np.float64,
            )
            out = lfilter(
                np.array([b0[k], b1[k], b2[k]]),
                np.array([1.0, a1n[k], a2n[k]]),
                x,
                zi=zi,
            )[0]
            new_x1 = x[-1]
            new_x2 = x[-2] if frames >= 2 else x1[k]
            new_y1 = out[-1]
            new_y2 = out[-2] if frames >= 2 else y1[k]
            x1[k] = new_x1; x2[k] = new_x2
            y1[k] = new_y1; y2[k] = new_y2
            x = out

        return x.astype(np.float32)

    def _render_parametric_eq_voice(self, module, frames, src, freqs_override=None,
                                    gains_override=None):
        """Voice-aware path -- V parallel cascades, output ``(V, F)``.

        The cascade is the mono path vectorized across voices. Because
        coefficients are shared (no CV), each stage filters all V rows
        in one ``lfilter`` call with ``zi`` of shape ``(V, 2)``. State
        is the DF-I history per band per voice: four ``(N, V)`` float64
        arrays. Each row holds one band's per-voice memory, kept
        independent so a per-voice carrier upstream is EQ'd without
        cross-talk.
        """
        V = src.shape[0]
        freqs, gains, qs = self._peq_band_params(module)
        if freqs_override is not None:
            freqs = freqs_override  # MotionEQ: per-band CV-swept centres
        if gains_override is not None:
            gains = gains_override  # MotionEQ: per-band CV-pushed gains
        n_bands = len(freqs)
        b0, b1, b2, a1n, a2n = self._peq_coeffs(freqs, gains, qs)

        state = self._state.setdefault(module.id, {})
        x1 = state.get("x1")
        needs_reinit = (
            x1 is None
            or x1.ndim != 2
            or x1.shape != (n_bands, V)
        )
        if needs_reinit:
            state.clear()
            for k in ("x1", "x2", "y1", "y2"):
                state[k] = np.zeros((n_bands, V), dtype=np.float64)

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        x1 = state["x1"]; x2 = state["x2"]
        y1 = state["y1"]; y2 = state["y2"]

        x = src.astype(np.float64)  # (V, F)
        for k in range(n_bands):
            zi = np.stack(
                [
                    b1[k] * x1[k] + b2[k] * x2[k] - a1n[k] * y1[k] - a2n[k] * y2[k],
                    b2[k] * x1[k] - a2n[k] * y1[k],
                ],
                axis=-1,
            )  # (V, 2)
            out = lfilter(
                np.array([b0[k], b1[k], b2[k]]),
                np.array([1.0, a1n[k], a2n[k]]),
                x,
                axis=-1,
                zi=zi,
            )[0]
            new_x1 = x[:, -1].copy()
            new_x2 = x[:, -2].copy() if frames >= 2 else x1[k].copy()
            new_y1 = out[:, -1].copy()
            new_y2 = out[:, -2].copy() if frames >= 2 else y1[k].copy()
            x1[k] = new_x1; x2[k] = new_x2
            y1[k] = new_y1; y2[k] = new_y2
            x = out

        return x.astype(np.float32)

    # ----- MotionEQ rendering ---------------------------------------------

    def _render_motion_eq(self, module, frames: int, buffers, patch) -> np.ndarray:
        """4-band peaking EQ with a per-band centre-frequency CV sweep.

        Reuses ParametricEQ's cascade wholesale: the only difference is
        that each band's centre and gain are CV-modulated before the
        coefficients are built. For band ``i`` the centre is
        ``band{i}_freq * 2 ** (cv_depth * mean(band{i}_freq_cv))`` and
        the gain is
        ``band{i}_gain + gain_cv_depth * mean(band{i}_gain_cv)`` (dB,
        clamped +/-24), both block-meaned (one coefficient set per
        block, shared across voices -- the Crossover's macro-sweep
        policy). Q stays static. An unpatched CV leaves that band at
        its static value, so with nothing patched MotionEQ is
        bit-identical to a ParametricEQ with the same params.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        cv_depth = float(module.params.get("cv_depth", 1.0))
        gain_cv_depth = float(module.params.get("gain_cv_depth", 6.0))
        base_freqs, base_gains, _qs = self._peq_band_params(module)
        mod_freqs = []
        for i, base in enumerate(base_freqs, start=1):
            cv = self._input_buffer(
                patch, buffers, module.id, f"band{i}_freq_cv"
            )
            if cv is not None and cv.size > 0:
                base = base * float(2.0 ** (cv_depth * float(np.mean(cv))))
            mod_freqs.append(base)

        # Per-band gain CV: additive in dB (the tilt_eq convention),
        # block-meaned like the freq sweep, clamped to the knob range
        # (±24 dB) so a hot CV can't push a bell into absurd gain. An
        # unpatched band keeps its exact static gain (bit-identical).
        mod_gains = []
        for i, base in enumerate(base_gains, start=1):
            cv = self._input_buffer(
                patch, buffers, module.id, f"band{i}_gain_cv"
            )
            if cv is not None and cv.size > 0:
                base = base + gain_cv_depth * float(np.mean(cv))
                base = min(max(base, -24.0), 24.0)
            mod_gains.append(base)

        if src.ndim == 2:
            return self._render_parametric_eq_voice(
                module, frames, src,
                freqs_override=mod_freqs, gains_override=mod_gains,
            )
        return self._render_parametric_eq_mono(
            module, frames, src,
            freqs_override=mod_freqs, gains_override=mod_gains,
        )

    # ----- SweepEQ rendering ----------------------------------------------

    def _sweep_eq_coeffs(self, mode, freq, gain_db, q):
        """One RBJ biquad's coefficients for the SweepEQ's current mode.

        ``peak`` borrows ParametricEQ's peaking bell (``gain`` in dB);
        ``bandpass``/``lowpass`` borrow the Filter's cookbook. Returns
        ``(b0, b1, b2, a1n, a2n)`` scalars normalized by a0, or ``None``
        for an unknown mode (the caller treats that as a dry
        passthrough). freq/Q clamping happens inside the borrowed
        helpers, so the sweep is as stable as the modules it reuses.
        """
        if mode == "peak":
            b0, b1, b2, a1n, a2n = self._peq_coeffs([freq], [gain_db], [q])
            return (float(b0[0]), float(b1[0]), float(b2[0]),
                    float(a1n[0]), float(a2n[0]))
        if mode in ("bandpass", "lowpass"):
            return self._filter_coeffs(mode, freq, q)
        return None

    def _render_sweep_eq(self, module, frames: int, buffers, patch) -> np.ndarray:
        """A single CV-swept resonant biquad with a dry/wet mix.

        Shape-polymorphic like Filter/ParametricEQ. ``freq_cv`` sweeps
        the centre frequency 1 V/oct (block-mean * ``cv_depth``), one
        coefficient set per block shared across voices -- the Crossover's
        macro-sweep policy. ``mode`` picks the voicing (bandpass/lowpass
        filter, or a peaking EQ bell); ``mix`` blends the result against
        the dry input.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        freq = float(module.params.get("freq", 800.0))
        freq_cv = self._input_buffer(patch, buffers, module.id, "freq_cv")
        if freq_cv is not None and freq_cv.size > 0:
            cv_depth = float(module.params.get("cv_depth", 1.0))
            freq = freq * float(2.0 ** (cv_depth * float(np.mean(freq_cv))))

        mode = str(module.params.get("mode", "bandpass"))
        gain = float(module.params.get("gain", 0.0))
        q = float(module.params.get("q", 4.0))
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)

        coeffs = self._sweep_eq_coeffs(mode, freq, gain, q)
        if coeffs is None:
            return src.astype(np.float32)  # unknown mode -> dry passthrough

        if src.ndim == 2:
            return self._render_sweep_eq_voice(module, frames, src, coeffs, mix)
        return self._render_sweep_eq_mono(module, frames, src, coeffs, mix)

    def _render_sweep_eq_mono(self, module, frames, src, coeffs, mix):
        """Mono path -- one biquad via ``lfilter``, then blend with dry.

        Raw DF-I history state (x1, x2, y1, y2), coefficient-independent
        so a swept ``freq_cv`` changing coefficients between blocks stays
        clean -- the same discipline as ``_render_filter_mono``. ``mix``
        blends wet/dry: 0.0 is a bit-exact dry bypass, and a ``peak`` band
        at 0 dB gain is a bit-exact passthrough at mix 1.0.
        """
        b0, b1, b2, a1n, a2n = coeffs
        state = self._state.setdefault(
            module.id, {"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0}
        )
        if "x1_arr" in state:  # was voice-shaped -> reinit to mono
            state.clear()
            state.update({"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0})

        if frames == 0:
            return np.empty(0, dtype=np.float32)

        x1 = state["x1"]; x2 = state["x2"]
        y1 = state["y1"]; y2 = state["y2"]
        zi = np.array(
            [b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2, b2 * x1 - a2n * y1],
            dtype=np.float64,
        )
        x = src.astype(np.float64)
        wet, _zf = lfilter(
            np.array([b0, b1, b2]), np.array([1.0, a1n, a2n]), x, zi=zi
        )
        state["x1"] = float(x[-1])
        state["x2"] = float(x[-2]) if frames >= 2 else x1
        state["y1"] = float(wet[-1])
        state["y2"] = float(wet[-2]) if frames >= 2 else y1

        out = mix * wet + (1.0 - mix) * x
        return out.astype(np.float32)

    def _render_sweep_eq_voice(self, module, frames, src, coeffs, mix):
        """Voice path -- V parallel biquads (shared coeffs, one lfilter
        over all rows), then blend with dry. Per-voice raw-history state;
        a single voice row is bit-identical to the mono path.
        """
        b0, b1, b2, a1n, a2n = coeffs
        V = src.shape[0]
        state = self._state.setdefault(module.id, {})
        needs_reinit = "x1_arr" not in state or state["x1_arr"].shape[0] != V
        if needs_reinit:
            state.clear()
            for k in ("x1_arr", "x2_arr", "y1_arr", "y2_arr"):
                state[k] = np.zeros(V, dtype=np.float64)

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        x1 = state["x1_arr"]; x2 = state["x2_arr"]
        y1 = state["y1_arr"]; y2 = state["y2_arr"]
        zi1 = b1 * x1 + b2 * x2 - a1n * y1 - a2n * y2  # (V,)
        zi2 = b2 * x1 - a2n * y1                        # (V,)
        x = src.astype(np.float64)                      # (V, F)
        wet, _zf = lfilter(
            np.array([b0, b1, b2]), np.array([1.0, a1n, a2n]),
            x, axis=1, zi=np.stack([zi1, zi2], axis=1),
        )
        state["x1_arr"] = x[:, -1].copy()
        state["x2_arr"] = x[:, -2].copy() if frames >= 2 else x1
        state["y1_arr"] = wet[:, -1].copy()
        state["y2_arr"] = wet[:, -2].copy() if frames >= 2 else y1

        out = mix * wet + (1.0 - mix) * x
        return out.astype(np.float32)

    # ----- DiskWriter rendering -------------------------------------------

    # ----- Loudness (equal-loudness contour) rendering --------------------

    _LOUD_F_LOW = 120.0      # low-shelf corner (Hz)
    _LOUD_F_HIGH = 8000.0    # high-shelf corner (Hz)
    _LOUD_BASS_MAX = 12.0    # auto bass boost (dB) at level -> 0
    _LOUD_TREBLE_MAX = 7.0   # auto treble boost (dB) at level -> 0

    def _loud_shelf(self, f0, gain_db, low):
        """One RBJ shelving biquad (normalised). 0 dB -> identity."""
        sr = self.sample_rate
        A = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * np.pi * min(max(f0, 20.0), sr * 0.45) / sr
        cw = np.cos(w0)
        alpha = np.sin(w0) / 2.0 * np.sqrt(2.0)   # shelf slope S = 1
        tsa = 2.0 * np.sqrt(A) * alpha
        Am1 = A - 1.0
        Ap1 = A + 1.0
        if low:
            b0 = A * (Ap1 - Am1 * cw + tsa)
            b1 = 2.0 * A * (Am1 - Ap1 * cw)
            b2 = A * (Ap1 - Am1 * cw - tsa)
            a0 = Ap1 + Am1 * cw + tsa
            a1 = -2.0 * (Am1 + Ap1 * cw)
            a2 = Ap1 + Am1 * cw - tsa
        else:
            b0 = A * (Ap1 + Am1 * cw + tsa)
            b1 = -2.0 * A * (Am1 + Ap1 * cw)
            b2 = A * (Ap1 + Am1 * cw - tsa)
            a0 = Ap1 - Am1 * cw + tsa
            a1 = 2.0 * (Am1 - Ap1 * cw)
            a2 = Ap1 - Am1 * cw - tsa
        return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    def _loudness_coeffs(self, level_eff, bass_db, treble_db):
        """Two shelves (low, high). Auto equal-loudness boost from the level
        (bass rises faster than treble) plus the manual dB trims."""
        inv = 1.0 - level_eff
        bass_total = float(np.clip(self._LOUD_BASS_MAX * inv + bass_db, -18.0, 18.0))
        treb_total = float(np.clip(self._LOUD_TREBLE_MAX * inv + treble_db, -18.0, 18.0))
        lo = self._loud_shelf(self._LOUD_F_LOW, bass_total, True)
        hi = self._loud_shelf(self._LOUD_F_HIGH, treb_total, False)
        return tuple(np.array([lo[k], hi[k]], dtype=np.float64) for k in range(5))

    def _render_loudness(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Low + high shelving cascade with a level-driven auto curve.

        Shape-polymorphic like ParametricEQ. The contour is one global
        control: a ``(V, F)`` ``level_cv`` is averaged to a single scalar so
        every voice shares the same shelves; a single voice row is
        bit-identical to mono.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        level_cv = self._input_buffer(
            patch, buffers, module.id, "level_cv", collapse=False
        )
        level = float(module.params.get("level", 0.5))
        bass = float(module.params.get("bass", 0.0))
        treble = float(module.params.get("treble", 0.0))
        cv_depth = float(module.params.get("cv_depth", 1.0))
        cvs = float(np.mean(level_cv)) if level_cv is not None and level_cv.size else 0.0
        level_eff = min(max(level + cv_depth * cvs, 0.0), 1.0)
        coeffs = self._loudness_coeffs(level_eff, bass, treble)

        if src.ndim == 2:
            return self._render_loudness_voice(module, frames, src, coeffs)
        return self._render_loudness_mono(module, frames, src, coeffs)

    def _render_loudness_mono(self, module, frames, src, coeffs):
        b0, b1, b2, a1n, a2n = coeffs
        n_bands = b0.shape[0]
        state = self._state.setdefault(module.id, {})
        x1 = state.get("x1")
        if x1 is None or x1.ndim != 1 or x1.shape[0] != n_bands:
            state.clear()
            for k in ("x1", "x2", "y1", "y2"):
                state[k] = np.zeros(n_bands, dtype=np.float64)
        if frames == 0:
            return np.empty(0, dtype=np.float32)
        x1 = state["x1"]; x2 = state["x2"]; y1 = state["y1"]; y2 = state["y2"]
        x = src.astype(np.float64)
        for k in range(n_bands):
            zi = np.array(
                [b1[k] * x1[k] + b2[k] * x2[k] - a1n[k] * y1[k] - a2n[k] * y2[k],
                 b2[k] * x1[k] - a2n[k] * y1[k]],
                dtype=np.float64,
            )
            out = lfilter(
                np.array([b0[k], b1[k], b2[k]]),
                np.array([1.0, a1n[k], a2n[k]]), x, zi=zi,
            )[0]
            nx1 = x[-1]; nx2 = x[-2] if frames >= 2 else x1[k]
            ny1 = out[-1]; ny2 = out[-2] if frames >= 2 else y1[k]
            x1[k] = nx1; x2[k] = nx2; y1[k] = ny1; y2[k] = ny2
            x = out
        return x.astype(np.float32)

    def _render_loudness_voice(self, module, frames, src, coeffs):
        V = src.shape[0]
        b0, b1, b2, a1n, a2n = coeffs
        n_bands = b0.shape[0]
        state = self._state.setdefault(module.id, {})
        x1 = state.get("x1")
        if x1 is None or x1.ndim != 2 or x1.shape != (n_bands, V):
            state.clear()
            for k in ("x1", "x2", "y1", "y2"):
                state[k] = np.zeros((n_bands, V), dtype=np.float64)
        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)
        x1 = state["x1"]; x2 = state["x2"]; y1 = state["y1"]; y2 = state["y2"]
        x = src.astype(np.float64)
        for k in range(n_bands):
            zi = np.stack(
                [b1[k] * x1[k] + b2[k] * x2[k] - a1n[k] * y1[k] - a2n[k] * y2[k],
                 b2[k] * x1[k] - a2n[k] * y1[k]],
                axis=-1,
            )
            out = lfilter(
                np.array([b0[k], b1[k], b2[k]]),
                np.array([1.0, a1n[k], a2n[k]]), x, axis=-1, zi=zi,
            )[0]
            nx1 = x[:, -1].copy(); nx2 = x[:, -2].copy() if frames >= 2 else x1[k].copy()
            ny1 = out[:, -1].copy(); ny2 = out[:, -2].copy() if frames >= 2 else y1[k].copy()
            x1[k] = nx1; x2[k] = nx2; y1[k] = ny1; y2[k] = ny2
            x = out
        return x.astype(np.float32)

    # ----- TiltEQ rendering ------------------------------------------------

    def _tilt_eq_coeffs(self, pivot, tilt_db):
        """Two opposed shelves about one pivot: lows +tilt dB, highs -tilt.

        Same RBJ shelf as Loudness (`_loud_shelf`), same (2, ...) coeff
        layout, so the generic loudness cascade renderers run it as-is.
        At tilt 0 both shelves are identity -> bit-exact passthrough.
        """
        lo = self._loud_shelf(pivot, tilt_db, True)
        hi = self._loud_shelf(pivot, -tilt_db, False)
        return tuple(np.array([lo[k], hi[k]], dtype=np.float64) for k in range(5))

    def _render_tilt_eq(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Spectral tilt about a pivot, CV-controlled (bass<->treble seesaw).

        Effective tilt is ``tilt + cv_depth * mean(tilt_cv)`` dB (block-
        meaned, one coefficient set per block shared across voices --
        the Crossover's macro-sweep policy), clamped to +/-18 dB.
        Positive CV boosts the lows and cuts the highs. Delegates to the
        Loudness cascade renderers (they are generic biquad chains keyed
        by module id), so shape-polymorphism, DF-I state discipline and
        the bit-exact identity at 0 dB are literally the same code.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        tilt_cv = self._input_buffer(
            patch, buffers, module.id, "tilt_cv", collapse=False
        )
        pivot = float(module.params.get("pivot", 1000.0))
        tilt = float(module.params.get("tilt", 0.0))
        cv_depth = float(module.params.get("cv_depth", 6.0))
        cvs = float(np.mean(tilt_cv)) if tilt_cv is not None and tilt_cv.size else 0.0
        tilt_eff = float(np.clip(tilt + cv_depth * cvs, -18.0, 18.0))
        coeffs = self._tilt_eq_coeffs(pivot, tilt_eff)

        if src.ndim == 2:
            return self._render_loudness_voice(module, frames, src, coeffs)
        return self._render_loudness_mono(module, frames, src, coeffs)

    # ----- Reverb rendering -----------------------------------------------

    # Eight delay-line lengths (samples at 44.1 kHz, near-prime so the
    # modes don't line up and ring) for the largest "hall" size; `size`
    # scales them down toward a small room. Time-scaled to the real
    # sample rate at render time.
    _REVERB_BASE = (1103, 1321, 1543, 1759, 1987, 2203, 2423, 2647)
    _REVERB_OUT = 0.30   # wet output trim (tuned so wet ~ dry level)
    _CHORUS_MAX_MS = 40.0  # longest chorus delay (ms); sizes the ring

    def _render_reverb(self, module, frames: int, buffers, patch):
        """Stereo FDN reverb: mono in -> decorrelated out_l / out_r.

        Eight delay lines are cross-mixed every sample by an orthonormal
        (Hadamard) feedback matrix and re-injected with a per-line decay
        gain and a shared damping low-pass, so a mono input blooms into a
        dense, decaying stereo tail. ``out_l`` and ``out_r`` tap the lines
        through two orthogonal sign patterns, so the channels are
        decorrelated (width). A polyphonic input is summed to mono first.

        Block-size independent: a feedback delay only recirculates within
        a block when a line is shorter than the block, so the network is
        processed in hops no longer than the shortest line -- within a hop
        every read predates the hop's writes, so it vectorizes, and the
        damping one-pole runs via ``lfilter`` with its state carried.
        """
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z.copy()}

        sr = self.sample_rate
        size = float(module.params.get("size", 0.5))
        decay = float(module.params.get("decay", 0.5))
        damping = float(module.params.get("damping", 0.5))
        mix = float(module.params.get("mix", 0.3))

        # CV on the three safe macros (size would sweep the delay-line
        # lengths and click): additive in level units scaled by the
        # shared cv_depth, block-meaned -- one macro value per block,
        # clamped 0..1 below exactly like the static params.
        cv_depth = float(module.params.get("cv_depth", 1.0))
        decay_cv = self._input_buffer(patch, buffers, module.id, "decay_cv")
        if decay_cv is not None and decay_cv.size > 0:
            decay = decay + cv_depth * float(np.mean(decay_cv))
        damping_cv = self._input_buffer(patch, buffers, module.id, "damping_cv")
        if damping_cv is not None and damping_cv.size > 0:
            damping = damping + cv_depth * float(np.mean(damping_cv))
        mix_cv = self._input_buffer(patch, buffers, module.id, "mix_cv")
        if mix_cv is not None and mix_cv.size > 0:
            mix = mix + cv_depth * float(np.mean(mix_cv))

        size = min(max(size, 0.0), 1.0)
        decay = min(max(decay, 0.0), 1.0)
        damping = min(max(damping, 0.0), 1.0)
        mix = min(max(mix, 0.0), 1.0)

        base = np.array(self._REVERB_BASE, dtype=np.float64) * (sr / 44100.0)
        N = base.shape[0]
        Lmax = int(base.max()) + 2
        scale = 0.25 + 0.75 * size
        L = np.clip(np.round(base * scale).astype(np.int64), 32, Lmax - 2)

        # Input diffusion: 4 series Schroeder allpasses smear the input
        # into a dense burst before it enters the FDN, so the tail fills in
        # smoothly instead of sounding like a handful of separate echoes.
        DD = np.round(np.array([113.0, 167.0, 251.0, 337.0]) * (sr / 44100.0))
        DD = np.maximum(DD.astype(np.int64), 8)
        Ld = int(DD.max()) + 2
        kd = 0.6

        state = self._state.setdefault(module.id, {})
        if (
            "buf" not in state
            or state["buf"].shape != (N, Lmax)
            or state.get("dbuf") is None
            or state["dbuf"].shape != (4, Ld)
        ):
            state.clear()
            state["buf"] = np.zeros((N, Lmax), dtype=np.float64)
            state["write_idx"] = 0
            state["lpz"] = np.zeros(N, dtype=np.float64)
            state["dbuf"] = np.zeros((4, Ld), dtype=np.float64)
            state["dwp"] = 0

        buf = state["buf"]
        wp = int(state["write_idx"])
        lpz = state["lpz"]

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        x = src.astype(np.float64)

        # --- input diffusion: run x through 4 series allpasses ---
        dbuf = state["dbuf"]
        dwp = int(state["dwp"])
        xd = np.empty(frames, dtype=np.float64)
        hop_d = int(DD.min())
        dpos = 0
        while dpos < frames:
            c = min(hop_d, frames - dpos)
            u = x[dpos:dpos + c].copy()
            j = np.arange(c)
            wcols = (dwp + j) % Ld
            for sidx in range(4):
                wdel = dbuf[sidx, (dwp + j - DD[sidx]) % Ld]
                w = u + kd * wdel
                u = -kd * w + wdel
                dbuf[sidx, wcols] = w
            xd[dpos:dpos + c] = u
            dwp += c
            dpos += c
        state["dwp"] = dwp % Ld

        # Per-line decay gain so every line reaches the same RT60.
        rt60 = 0.2 * (60.0 ** decay)             # 0.2 s .. 12 s
        g = np.power(10.0, -3.0 * L / (rt60 * sr))
        np.clip(g, 0.0, 0.9995, out=g)

        # Shared damping one-pole: cutoff sweeps ~18 kHz (open) -> ~1 kHz.
        fc = 18000.0 * (1000.0 / 18000.0) ** damping
        a = 1.0 - float(np.exp(-2.0 * np.pi * fc / sr))

        # Orthonormal feedback matrix (Sylvester-Hadamard) + two orthogonal
        # output taps for the decorrelated L/R pair.
        H2 = np.array([[1.0, 1.0], [1.0, -1.0]])
        H8 = np.kron(H2, np.kron(H2, H2))        # (8, 8), +/-1
        A = H8 / np.sqrt(float(N))
        tap_l = H8[1]
        tap_r = H8[2]
        out_scale = self._REVERB_OUT / np.sqrt(float(N))

        wet_l = np.empty(frames, dtype=np.float64)
        wet_r = np.empty(frames, dtype=np.float64)
        rows = np.arange(N)
        hop = int(L.min())
        pos = 0
        while pos < frames:
            c = min(hop, frames - pos)
            idx = wp + np.arange(c)
            readpos = (idx[np.newaxis, :] - L[:, np.newaxis]) % Lmax   # (N, c)
            S = buf[rows[:, None], readpos]                            # (N, c)
            wet_l[pos:pos + c] = (tap_l @ S) * out_scale
            wet_r[pos:pos + c] = (tap_r @ S) * out_scale
            Sd = lfilter(
                [a], [1.0, -(1.0 - a)], S, axis=-1,
                zi=(lpz * (1.0 - a))[:, np.newaxis],
            )[0]
            lpz = Sd[:, -1].copy()
            fb = A @ (g[:, np.newaxis] * Sd)                          # (N, c)
            xin = xd[pos:pos + c]
            buf[rows[:, None], idx % Lmax] = xin[np.newaxis, :] + fb
            wp += c
            pos += c

        state["write_idx"] = int(wp % Lmax)
        state["lpz"] = lpz

        dry = (1.0 - mix) * x
        out_l = (dry + mix * wet_l).astype(np.float32)
        out_r = (dry + mix * wet_r).astype(np.float32)
        return {"out_l": out_l, "out_r": out_r}

    def _render_chorus(self, module, frames: int, buffers, patch):
        """Detuned multi-voice stereo chorus: mono in -> out_l / out_r.

        A bank of short delay lines is swept by an internal sine LFO (one
        evenly-spaced phase slice per voice) and read back with linear
        interpolation; the moving delay detunes each copy, and the copies
        are panned across the stereo field so the two channels decorrelate
        (width). There is no feedback -- a fed-back chorus is a flanger --
        so no read this block depends on a sample written this block, the
        whole render vectorizes, and it is exactly block-size independent.
        A polyphonic input is summed to mono first.
        """
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z.copy()}

        sr = self.sample_rate
        rate = float(module.params.get("rate", 0.6))
        depth = float(module.params.get("depth", 0.5))
        voices = int(round(float(module.params.get("voices", 3))))
        mix = float(module.params.get("mix", 0.5))
        cv_depth = float(module.params.get("cv_depth", 1.0))
        depth = min(max(depth, 0.0), 1.0)
        mix = min(max(mix, 0.0), 1.0)
        voices = min(max(voices, 1), 6)

        # rate_cv: 1 V/oct on the LFO rate, block-mean (a sub-audio LFO, so
        # one rate per block is the right cost/quality trade-off -- the same
        # cadence the LFO module uses for its own rate_cv).
        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * float(2.0 ** (cv_depth * float(np.mean(rate_cv))))
        rate = min(max(rate, 0.01), 20.0)

        max_ms = self._CHORUS_MAX_MS
        L = int(max_ms * sr / 1000.0) + frames + 4

        state = self._state.setdefault(module.id, {})
        if "buf" not in state or state["buf"].shape != (L,):
            state.clear()
            state["buf"] = np.zeros(L, dtype=np.float64)
            state["write_idx"] = 0
            state["phase"] = 0.0

        buf = state["buf"]
        wp = int(state["write_idx"])
        phase0 = float(state["phase"])

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        x = src.astype(np.float64)                        # (F,)

        # Per-voice base delays spread across ~12..24 ms, plus a shared
        # sweep of up to +/-8 ms scaled by depth. The minimum stays well
        # positive so a read never crosses the write head.
        if voices > 1:
            base_ms = np.linspace(12.0, 24.0, voices)
        else:
            base_ms = np.array([18.0])
        base_samp = base_ms * sr / 1000.0                 # (V,)
        sweep_samp = (8.0 * depth) * sr / 1000.0          # scalar

        # One sine LFO sliced into V evenly-spaced phase offsets, so the
        # voices detune against each other (and the channels decorrelate).
        inc = rate / sr
        n = np.arange(frames, dtype=np.float64)
        offs = np.arange(voices, dtype=np.float64) / voices           # (V,)
        ph = (phase0 + offs[:, None] + n[None, :] * inc) % 1.0         # (V, F)
        lfo = np.sin(2.0 * np.pi * ph)                                # (V, F)
        new_phase = (phase0 + frames * inc) % 1.0

        delay = base_samp[:, None] + sweep_samp * lfo                 # (V, F)
        np.clip(delay, 2.0, float(L - 2), out=delay)

        # Write the whole block, then read the taps. With no feedback a tap
        # that lands inside this block just reads an input sample already
        # written -- correct, and identical at any block size.
        absidx = wp + np.arange(frames)
        buf[absidx % L] = x
        rp = absidx[None, :] - delay                                 # (V, F)
        i0 = np.floor(rp).astype(np.int64)
        frac = rp - i0
        tap = buf[i0 % L] * (1.0 - frac) + buf[(i0 + 1) % L] * frac  # (V, F)

        # Equal-power pan spread; per-channel normalisation keeps the wet
        # level ~ the dry level for any voice count.
        pos = (np.arange(voices, dtype=np.float64) + 0.5) / voices
        ang = pos * (np.pi / 2.0)
        gl = np.cos(ang)
        gr = np.sin(ang)
        nl = 1.0 / np.sqrt(float(np.sum(gl * gl)))
        nr = 1.0 / np.sqrt(float(np.sum(gr * gr)))
        wet_l = (gl @ tap) * nl                                      # (F,)
        wet_r = (gr @ tap) * nr

        state["write_idx"] = int((wp + frames) % L)
        state["phase"] = new_phase

        dry = (1.0 - mix) * x
        out_l = (dry + mix * wet_l).astype(np.float32)
        out_r = (dry + mix * wet_r).astype(np.float32)
        return {"out_l": out_l, "out_r": out_r}

    # ----- Flanger rendering ----------------------------------------------

    # Longest delay the flanger line can address, in milliseconds. The comb
    # is a *short* modulated delay, so this ring is tiny; it sizes the
    # buffer and caps the swept delay.
    _FLANGER_MAX_MS = 12.0
    # Largest sweep amplitude (ms) at depth == 1, added around ``manual``.
    _FLANGER_SWEEP_MS = 4.0
    # Shortest delay, in samples. >= 2 keeps both linear-interpolation taps
    # strictly behind the write head (never reads the sample being written),
    # so the delay stays positive -- a *standard* flanger, not through-zero.
    _FLANGER_MIN_SAMP = 2.0
    # Through-zero mode sizes its line from a larger bound: the moving
    # tap sweeps out to ~2x the centre delay (2 * manual_max + margin).
    _FLANGER_TZ_MAX_MS = 22.0
    # Floor (samples) for the through-zero moving tap, keeping the
    # fed-back read a few samples behind the write head so regeneration
    # stays stable when the sweep runs the tap right up to "now".
    _FLANGER_TZ_MOVE_MIN = 4.0

    def _render_flanger(self, module, frames: int, buffers, patch):
        """Swept resonant comb flanger: mono in -> out_l / out_r.

        Two short delay lines (one per channel) are swept by an internal
        sine LFO whose L and R phases sit a quarter-cycle apart, and each
        line feeds a fraction of its own output back in (bipolar
        regeneration). Summing the swept, fed-back delay with the dry
        signal is the moving, ringing comb -- the flanger. Because the
        delay is always far shorter than a block, a read this sample can
        depend on a sample written this sample, so the recirculation runs
        per-sample (the delay's short-time path); the LFO phase and the
        ring state carry across blocks, so the render is still exactly
        block-size independent. A polyphonic input is summed to mono first.

        With ``through_zero`` enabled the module instead keeps a fixed
        reference tap at the centre delay and sweeps a second moving tap
        around it, so their relative delay passes through zero (and goes
        negative) each time the LFO crosses zero -- the tape "jet". The
        ``polarity`` knob picks the crossing character (+1 additive bloom,
        -1 subtractive null). ``mix == 0`` stays a bit-exact dry copy in
        either mode, and the standard path is left byte-for-byte unchanged.
        """
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z.copy()}

        sr = self.sample_rate
        rate = float(module.params.get("rate", 0.3))
        depth = float(module.params.get("depth", 0.7))
        manual_ms = float(module.params.get("manual", 1.5))
        feedback = float(module.params.get("feedback", 0.5))
        mix = float(module.params.get("mix", 0.5))
        cv_depth = float(module.params.get("cv_depth", 1.0))
        tz_on = float(module.params.get("through_zero", 0.0)) >= 0.5
        polarity = float(module.params.get("polarity", 1.0))
        depth = min(max(depth, 0.0), 1.0)
        mix = min(max(mix, 0.0), 1.0)
        feedback = min(max(feedback, -0.95), 0.95)   # bipolar, below runaway
        manual_ms = min(max(manual_ms, 0.1), 10.0)
        polarity = min(max(polarity, -1.0), 1.0)

        # rate_cv: 1 V/oct on the LFO rate, block-mean -- a sub-audio LFO,
        # so one rate per block is the right cost/quality trade-off (the
        # same cadence the chorus and LFO modules use for their rate_cv).
        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * float(2.0 ** (cv_depth * float(np.mean(rate_cv))))
        rate = min(max(rate, 0.01), 20.0)

        # Through-zero sweeps the moving tap out to ~2x the centre delay, so
        # its line is longer; standard mode keeps the original length so its
        # state and output stay byte-for-byte unchanged.
        max_ms = self._FLANGER_TZ_MAX_MS if tz_on else self._FLANGER_MAX_MS
        L = int(max_ms * sr / 1000.0) + frames + 4

        state = self._state.setdefault(module.id, {})
        if (
            "buf" not in state
            or state["buf"].shape != (2, L)
            or state.get("tz") != tz_on
        ):
            state.clear()
            state["buf"] = np.zeros((2, L), dtype=np.float64)
            state["write_idx"] = 0
            state["phase"] = 0.0
            state["tz"] = tz_on

        buf = state["buf"]
        wp = int(state["write_idx"])
        phase0 = float(state["phase"])

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        x = src.astype(np.float64)                        # (F,)

        # One sine LFO, L and R a quarter-cycle apart, so the two combs
        # sweep out of step (stereo width).
        inc = rate / sr
        n = np.arange(frames, dtype=np.float64)
        offs = np.array([0.0, 0.25])                      # quadrature L / R
        ph = (phase0 + offs[:, None] + n[None, :] * inc) % 1.0    # (2, F)
        lfo = np.sin(2.0 * np.pi * ph)                            # (2, F)
        new_phase = (phase0 + frames * inc) % 1.0

        out = np.empty((2, frames), dtype=np.float64)
        rows = np.arange(2)
        dry_gain = 1.0 - mix

        if not tz_on:
            # ---- Standard positive-delay flanger (unchanged path) --------
            manual_samp = manual_ms * sr / 1000.0
            sweep_samp = (self._FLANGER_SWEEP_MS * depth) * sr / 1000.0
            delay = manual_samp + sweep_samp * lfo                    # (2, F)
            np.clip(delay, self._FLANGER_MIN_SAMP, float(L - 2), out=delay)
            for i in range(frames):
                rp = wp - delay[:, i]                          # (2,)
                i0 = np.floor(rp).astype(np.int64)
                frac = rp - i0
                d = (
                    buf[rows, i0 % L] * (1.0 - frac)
                    + buf[rows, (i0 + 1) % L] * frac
                )                                              # (2,)
                buf[rows, wp % L] = x[i] + feedback * d
                out[:, i] = x[i] * dry_gain + d * mix
                wp += 1
        else:
            # ---- Through-zero (tape) flanger -----------------------------
            # Fixed reference tap at D0 = manual, plus a moving tap swept
            # +/- around it. The relative delay (moving - reference) crosses
            # zero as the LFO crosses zero, so the comb notches sweep to
            # infinity and the comb flips polarity there -- the tape jet.
            # ``polarity`` blends the crossing: +1 additive bloom, -1 null.
            # Feedback taps the moving read (floored at _FLANGER_TZ_MOVE_MIN
            # so it stays stable when the tap nears the write head).
            D0 = manual_ms * sr / 1000.0
            move_min = self._FLANGER_TZ_MOVE_MIN
            d_ref = min(max(D0, self._FLANGER_MIN_SAMP), float(L - 2))
            sweep_samp = depth * max(D0 - move_min, 0.0)
            dm = D0 + sweep_samp * lfo                                # (2, F)
            np.clip(dm, move_min, float(L - 2), out=dm)
            for i in range(frames):
                # fixed reference tap (same delay both channels, own rows)
                rpa = wp - d_ref
                ja = int(np.floor(rpa))
                fa = rpa - ja
                a = (
                    buf[rows, ja % L] * (1.0 - fa)
                    + buf[rows, (ja + 1) % L] * fa
                )                                              # (2,)
                # swept moving tap (per channel)
                rp = wp - dm[:, i]                             # (2,)
                i0 = np.floor(rp).astype(np.int64)
                frac = rp - i0
                b = (
                    buf[rows, i0 % L] * (1.0 - frac)
                    + buf[rows, (i0 + 1) % L] * frac
                )                                              # (2,)
                wet = 0.5 * (a + polarity * b)                 # (2,)
                buf[rows, wp % L] = x[i] + feedback * b
                out[:, i] = x[i] * dry_gain + wet * mix
                wp += 1

        wp = wp % L
        state["write_idx"] = int(wp)
        state["phase"] = new_phase

        out_l = out[0].astype(np.float32)
        out_r = out[1].astype(np.float32)
        return {"out_l": out_l, "out_r": out_r}


    # ----- Phaser rendering -----------------------------------------------

    # Centre-frequency sweep limits (Hz). ``center`` is clamped to this
    # band; the swept break frequency is additionally clamped below Nyquist
    # so the allpass coefficient never degenerates.
    _PHASER_CENTER_MIN = 100.0
    _PHASER_CENTER_MAX = 6000.0
    # Sweep width at depth == 1, in octaves each side of ``center``.
    _PHASER_MAX_OCT = 2.0
    # Allowed allpass stage counts (two, three or four notches).
    _PHASER_STAGES = (4, 6, 8)

    def _render_phaser(self, module, frames: int, buffers, patch):
        """Swept allpass-notch phaser: mono in -> out_l / out_r.

        The mono-summed input runs through a chain of first-order allpass
        stages whose break frequency an internal sine LFO sweeps (L and R
        phases a quarter-cycle apart for stereo width). Each allpass leaves
        magnitude flat and only rotates phase; summing the chain output back
        with the dry signal is what carves the moving notches (one notch per
        stage pair). A fraction of the last stage's output is fed back to
        the chain input (bipolar resonance); that one-sample feedback makes
        a read depend on the sample just written, so the cascade runs
        per-sample -- but the LFO phase, the allpass state and the feedback
        memory carry across blocks, so the render is exactly block-size
        independent. A polyphonic input is summed to mono first, and
        ``mix == 0`` is a bit-exact dry passthrough on both channels.
        """
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z.copy()}

        sr = self.sample_rate
        rate = float(module.params.get("rate", 0.5))
        depth = float(module.params.get("depth", 0.6))
        center = float(module.params.get("center", 800.0))
        feedback = float(module.params.get("feedback", 0.4))
        mix = float(module.params.get("mix", 0.5))
        cv_depth = float(module.params.get("cv_depth", 1.0))
        stages = int(round(float(module.params.get("stages", 6))))
        if stages not in self._PHASER_STAGES:
            stages = min(self._PHASER_STAGES, key=lambda v: abs(v - stages))
        depth = min(max(depth, 0.0), 1.0)
        mix = min(max(mix, 0.0), 1.0)
        feedback = min(max(feedback, -0.95), 0.95)   # bipolar, below runaway
        center = min(max(center, self._PHASER_CENTER_MIN), self._PHASER_CENTER_MAX)

        # rate_cv: 1 V/oct on the LFO rate, block-mean -- a sub-audio LFO,
        # so one rate per block is the right cost/quality trade-off (the
        # same cadence the chorus and flanger use for their rate_cv).
        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * float(2.0 ** (cv_depth * float(np.mean(rate_cv))))
        rate = min(max(rate, 0.01), 20.0)

        state = self._state.setdefault(module.id, {})
        if "s" not in state or state["s"].shape != (2, stages):
            state.clear()
            state["s"] = np.zeros((2, stages), dtype=np.float64)   # allpass memory
            state["yprev"] = np.zeros(2, dtype=np.float64)         # feedback memory
            state["phase"] = 0.0

        s = state["s"]
        yprev = state["yprev"]
        phase0 = float(state["phase"])

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        x = src.astype(np.float64)                        # (F,)

        # One sine LFO, L and R a quarter-cycle apart, so the two notch
        # chains sweep out of step (stereo width).
        inc = rate / sr
        n = np.arange(frames, dtype=np.float64)
        offs = np.array([0.0, 0.25])                      # quadrature L / R
        ph = (phase0 + offs[:, None] + n[None, :] * inc) % 1.0    # (2, F)
        lfo = np.sin(2.0 * np.pi * ph)                            # (2, F)
        new_phase = (phase0 + frames * inc) % 1.0

        # Exponential (musical) sweep of the break frequency: +/- depth*2
        # octaves around ``center``, clamped well inside Nyquist so the
        # allpass coefficient stays finite.
        octs = self._PHASER_MAX_OCT * depth
        fc = center * (2.0 ** (octs * lfo))                       # (2, F)
        np.clip(fc, 20.0, sr * 0.45, out=fc)
        tanv = np.tan(np.pi * fc / sr)
        a = (tanv - 1.0) / (tanv + 1.0)                          # (2, F) in (-1, 1)

        # Per-sample allpass cascade with one-sample feedback. Both channels
        # advance together as a length-2 vector; the inner loop is the
        # cascade. ``s`` holds each stage's memory, ``yprev`` the last chain
        # output fed back. Writing wet, then mixing with dry, gives the
        # notches; mix == 0 leaves ``out`` a bit-exact dry copy.
        wet = np.empty((2, frames), dtype=np.float64)
        for i in range(frames):
            ai = a[:, i]                                   # (2,)
            v = x[i] + feedback * yprev                    # (2,)
            for k in range(stages):
                y = ai * v + s[:, k]
                s[:, k] = v - ai * y
                v = y
            yprev = v
            wet[:, i] = v

        state["s"] = s
        state["yprev"] = yprev
        state["phase"] = new_phase

        dry = (1.0 - mix) * x
        out_l = (dry + mix * wet[0]).astype(np.float32)
        out_r = (dry + mix * wet[1]).astype(np.float32)
        return {"out_l": out_l, "out_r": out_r}


    # ----- Delay rendering ------------------------------------------------

    # Longest delay the line can address, in milliseconds. The ring buffer
    # is sized from this; both the ``time`` slider and the ``time_cv``
    # modulation are clamped to it.
    _DELAY_MAX_MS = 2000.0
    # Shortest delay, in samples. >= 2 keeps both linear-interpolation taps
    # strictly behind the write head (never reads the sample being written).
    _DELAY_MIN_SAMP = 2.0

    def _render_delay(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Analog-voiced feedback delay (echo) with a damped feedback path.

        Shape-polymorphic like the other effects. Branches on the audio
        input's ndim: 1D ``(F,)`` -> one delay line, ``(F,)`` out; 2D
        ``(V, F)`` -> one delay line per voice slot, ``(V, F)`` out. A mono
        ``time_cv`` broadcasts across voices; a ``(V, F)`` ``time_cv``
        modulates each independently. Missing audio in -> silence, with the
        line left intact so reconnecting the cable doesn't snap the tail.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        time_cv = self._input_buffer(
            patch, buffers, module.id, "time_cv", collapse=False
        )

        if src.ndim == 2:
            V = src.shape[0]
            if time_cv is None:
                cv = None
            elif time_cv.ndim == 1:
                cv = np.broadcast_to(time_cv, (V, time_cv.shape[0]))
            elif time_cv.shape[0] == V:
                cv = time_cv
            elif time_cv.shape[0] == 1:
                cv = np.broadcast_to(time_cv, (V, time_cv.shape[1]))
            else:
                cv = np.broadcast_to(
                    time_cv.mean(axis=0), (V, time_cv.shape[1])
                )
            return self._render_delay_core(module, frames, src, cv)

        # Mono audio. A 2D time_cv collapses to one shared modulation
        # (mean over voices) -- summing time voltages would be nonsense.
        if time_cv is not None and time_cv.ndim == 2:
            time_cv = time_cv.mean(axis=0)
        out = self._render_delay_core(
            module,
            frames,
            src[np.newaxis, :],
            None if time_cv is None else time_cv[np.newaxis, :],
        )
        return out[0]

    def _render_delay_core(self, module, frames, src, cv):
        """Shared ``(V, F)`` feedback-delay engine.

        Per output sample: read the line ``delay`` samples back with linear
        interpolation; low-pass that read for the feedback path so each
        recirculation darkens (the analog voicing); write ``in + feedback *
        damped`` into the line; and mix the *un-damped* read into the dry
        signal. The mono path calls this with ``V == 1``, so a single voice
        row is bit-identical to the mono render -- the float ops are the
        same per row regardless of V.

        Per-sample (not block-vectorized) because the feedback recirculation
        is sequential when the delay is shorter than a block; the constant-
        delay >= block case could be vectorized later (see WORKLOG).
        """
        V = src.shape[0]
        sr = self.sample_rate

        time_ms = float(module.params.get("time", 300.0))
        feedback = float(module.params.get("feedback", 0.4))
        tone = float(module.params.get("tone", 0.5))
        mix = float(module.params.get("mix", 0.35))
        cv_depth_ms = float(module.params.get("cv_depth", 50.0))

        feedback = min(max(feedback, 0.0), 0.98)   # stay below runaway
        mix = min(max(mix, 0.0), 1.0)
        tone = min(max(tone, 0.0), 1.0)

        max_samp = self._DELAY_MAX_MS * sr / 1000.0
        L = max(int(max_samp) + 4, frames + 4)

        state = self._state.setdefault(module.id, {})
        if "buf" not in state or state["buf"].shape != (V, L):
            state.clear()
            state["buf"] = np.zeros((V, L), dtype=np.float64)
            state["write_idx"] = 0
            state["lp"] = np.zeros(V, dtype=np.float64)

        buf = state["buf"]
        wp = int(state["write_idx"])
        lp = state["lp"]

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        # Damping one-pole coefficient from the tone knob: a log-swept
        # cutoff from ~200 Hz (dark) to ~18 kHz (bright), sample-rate
        # independent. tone == 1 -> wide open (essentially no damping).
        fc = 200.0 * (18000.0 / 200.0) ** tone
        g = 1.0 - float(np.exp(-2.0 * np.pi * fc / sr))
        g = min(max(g, 0.0), 1.0)

        x = src.astype(np.float64)                    # (V, F)
        time_samp = time_ms * sr / 1000.0
        cv_depth_samp = cv_depth_ms * sr / 1000.0
        min_s = self._DELAY_MIN_SAMP
        max_s = float(L - 2)

        # Per-sample delay in samples, (V, F), clamped into the line.
        if cv is None:
            dly = np.full((V, frames), time_samp, dtype=np.float64)
        else:
            dly = time_samp + cv_depth_samp * cv.astype(np.float64)
        np.clip(dly, min_s, max_s, out=dly)

        rows = np.arange(V)
        if float(dly.min()) >= frames:
            # Fast path: every read this block lands at least one block back,
            # so no read depends on a sample written this block and the whole
            # block vectorizes. The damping one-pole runs via ``lfilter`` with
            # its state carried in ``zi``. This is the common echo case
            # (any musical delay time is many blocks long).
            absidx = wp + np.arange(frames)                  # (F,) absolute
            rp = absidx[np.newaxis, :] - dly                 # (V, F) read pos
            i0 = np.floor(rp).astype(np.int64)
            frac = rp - i0
            d = (
                buf[rows[:, None], i0 % L] * (1.0 - frac)
                + buf[rows[:, None], (i0 + 1) % L] * frac
            )
            zi = ((1.0 - g) * lp)[:, np.newaxis]             # (V, 1)
            damped = lfilter([g], [1.0, -(1.0 - g)], d, axis=-1, zi=zi)[0]
            buf[rows[:, None], absidx % L] = x + feedback * damped
            out = x * (1.0 - mix) + d * mix
            lp = damped[:, -1].copy()
            wp = (wp + frames) % L
        else:
            # Per-sample path: the delay dips below a block (short or heavily
            # modulated), so the feedback recirculation is sequential.
            out = np.empty((V, frames), dtype=np.float64)
            for n in range(frames):
                rp = wp - dly[:, n]                          # (V,)
                i0 = np.floor(rp).astype(np.int64)
                frac = rp - i0
                d = (
                    buf[rows, i0 % L] * (1.0 - frac)
                    + buf[rows, (i0 + 1) % L] * frac
                )
                lp = lp + g * (d - lp)                       # damped feedback
                buf[rows, wp % L] = x[:, n] + feedback * lp
                out[:, n] = x[:, n] * (1.0 - mix) + d * mix
                wp += 1
            wp = wp % L

        state["write_idx"] = int(wp)
        state["lp"] = lp
        return out.astype(np.float32)

    # ----- Resampler rendering --------------------------------------------

    # Looping-buffer window for the varispeed read head, in seconds. The
    # read head trails the write head inside this window and wraps within
    # it, so the module keeps sounding forever on a continuous signal.
    # Longer = subtler loop texture but more latency; this latency is the
    # unavoidable cost of varispeed on a live stream.
    _RESAMP_WINDOW_SEC = 0.2
    # The read head starts this fraction of the window behind the write
    # head. Centred (1/2) gives symmetric runway for pitch up (delay
    # shrinks) and pitch down (delay grows) before the first loop wrap.
    _RESAMP_INIT_FRAC = 0.5
    # Clamp the effective transpose so the playback ratio can't explode
    # (+/-60 st = +/-5 octaves -> ratio in [1/32, 32]).
    _RESAMP_MAX_ST = 60.0

    def _render_resampler(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Varispeed pitch shifter: resample ``in`` at a pitch-derived rate.

        Shape-polymorphic. Branches on the *audio* input's ndim (the
        signal being resampled owns the looping buffer):

          * 1D ``(F,)`` audio -> one looping buffer, output ``(F,)``.
          * 2D ``(V, F)`` audio -> V looping buffers (one per voice slot)
            with per-voice read heads, output ``(V, F)``.

        A mono ``pitch_cv`` broadcasts across voices; a ``(V, F)``
        ``pitch_cv`` drives each voice independently. Missing audio in ->
        silence out, with the buffer/heads left as-is so reconnecting the
        cable doesn't snap.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        pitch_cv = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )

        if src.ndim == 2:
            V = src.shape[0]
            if pitch_cv is None:
                cv = None
            elif pitch_cv.ndim == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[0]))
            elif pitch_cv.shape[0] == V:
                cv = pitch_cv
            elif pitch_cv.shape[0] == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[1]))
            else:
                # Voice-count mismatch -> one shared transpose for all.
                cv = np.broadcast_to(
                    pitch_cv.mean(axis=0), (V, pitch_cv.shape[1])
                )
            return self._render_resampler_core(module, frames, src, cv)

        # Mono audio. A 2D pitch_cv collapses to a single shared transpose
        # (mean over voices) -- summing pitch voltages would be nonsense.
        if pitch_cv is not None and pitch_cv.ndim == 2:
            pitch_cv = pitch_cv.mean(axis=0)
        out = self._render_resampler_core(
            module,
            frames,
            src[np.newaxis, :],
            None if pitch_cv is None else pitch_cv[np.newaxis, :],
        )
        return out[0]

    def _render_resampler_core(self, module, frames, src, cv):
        """Shared ``(V, F)`` varispeed engine.

        ``src`` is ``(V, F)`` audio; ``cv`` is ``(V, F)`` pitch CV or
        None. The mono path calls this with ``V == 1``, so a single voice
        row is bit-identical to the mono render -- the float ops are the
        same per row regardless of V.

        Per output sample the read head advances by the playback ratio
        ``2 ** (st/12)`` (``st`` summed in semitone space and optionally
        glided), reading the per-voice ring buffer with linear
        interpolation and wrapping inside the loop window. The whole block
        is vectorized: the read positions are the cumulative integral of
        the per-sample ratio, wrapped with a single ``np.mod``.
        """
        V = src.shape[0]
        sr = self.sample_rate
        L = int(self._RESAMP_WINDOW_SEC * sr)
        L = max(L, frames * 4, 8)   # always comfortably larger than a block
        span = L - 1                # loop span; keeps both interp taps valid

        state = self._state.setdefault(module.id, {})
        needs_reinit = (
            "buf" not in state or state["buf"].shape != (V, L)
        )
        if needs_reinit:
            state.clear()
            state["buf"] = np.zeros((V, L), dtype=np.float64)
            state["write_idx"] = 0
            init_delay = float(
                max(1, min(L - 1, int(self._RESAMP_INIT_FRAC * L)))
            )
            state["delay"] = np.full(V, init_delay, dtype=np.float64)
            state["last_st"] = np.zeros(V, dtype=np.float64)

        buf = state["buf"]
        write_idx = int(state["write_idx"])
        delay = state["delay"]      # (V,) float, the read head's lag in (1, L)
        last_st = state["last_st"]  # (V,) float, glide one-pole memory

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        # --- write the incoming block into each voice's ring buffer ---
        write_slots = (write_idx + np.arange(frames)) % L
        buf[:, write_slots] = src.astype(np.float64)
        # New write head; window_start (oldest readable) is congruent to it
        # mod L, so absolute index (window_start + j) lives at (head + j) % L.
        head = (write_idx + frames) % L

        # --- per-sample transpose, summed in semitone space ---
        semis = float(module.params.get("semitones", 0.0))
        cents = float(module.params.get("cents", 0.0))
        cv_depth = float(module.params.get("cv_depth", 12.0))
        glide = float(module.params.get("glide", 0.0))

        base_st = semis + cents / 100.0
        target = np.full((V, frames), base_st, dtype=np.float64)
        if cv is not None:
            target += cv_depth * cv.astype(np.float64)

        if glide > 0.0:
            # One-pole glide: y[n] = coef*x[n] + (1-coef)*y[n-1].
            coef = 1.0 - float(np.exp(-1.0 / (glide * sr)))
            zi = ((1.0 - coef) * last_st)[:, np.newaxis]   # (V, 1)
            smoothed = lfilter(
                [coef], [1.0, -(1.0 - coef)], target, axis=-1, zi=zi
            )[0]
        else:
            smoothed = target
        last_st = smoothed[:, -1].copy()

        np.clip(smoothed, -self._RESAMP_MAX_ST, self._RESAMP_MAX_ST, out=smoothed)
        ratio = np.exp2(smoothed / 12.0)   # (V, F) playback rate per sample

        # --- read positions: cumulative integral of ratio, wrapped ---
        cum = np.cumsum(ratio, axis=-1)
        excum = cum - ratio                 # exclusive cumsum (offset per sample)
        offs = (L - delay)[:, np.newaxis] + excum
        phase = np.mod(offs, span)          # in [0, span)
        i0 = np.floor(phase).astype(np.int64)
        frac = phase - i0

        rows = np.arange(V)[:, np.newaxis]
        s0 = buf[rows, (head + i0) % L]
        s1 = buf[rows, (head + i0 + 1) % L]
        out = (s0 * (1.0 - frac) + s1 * frac).astype(np.float32)

        # --- carry the read head as a lag behind the (new) write head ---
        # Over the block the head moved by `frames`, the read by sum(ratio),
        # so the new lag is delay + frames - sum(ratio). Wrap into [1, L) by
        # integer span steps (the loop) -- this preserves the fractional
        # read phase, so unity ratio stays perfectly click-free.
        sum_ratio = cum[:, -1]
        new_delay = 1.0 + np.mod((delay + frames - sum_ratio) - 1.0, span)

        state["buf"] = buf
        state["write_idx"] = head
        state["delay"] = new_delay
        state["last_st"] = last_st
        return out

    # ----- PitchShifter rendering -----------------------------------------

    # Clamp the effective transpose so the playback ratio (and the stretch
    # ring sized from it) stays bounded. +/-36 st -> ratio in [1/8, 8].
    _PS_MAX_ST = 36.0

    def _render_pitch_shifter(self, module, frames, buffers, patch):
        """Granular WSOLA pitch shifter (time-preserving). Shape-polymorphic.

        Branches on the audio input's ndim: 1D -> one grain engine; 2D
        ``(V, F)`` -> V independent engines (one per voice slot). A mono
        ``pitch_cv`` broadcasts across voices; a ``(V, F)`` ``pitch_cv``
        drives each voice. Pitch CV is sampled per block (block-rate),
        summed in semitone space. Missing audio in -> silence.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            return np.zeros(frames, dtype=np.float32)
        pitch_cv = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        if src.ndim == 2:
            V = src.shape[0]
            if pitch_cv is None:
                cv = None
            elif pitch_cv.ndim == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[0]))
            elif pitch_cv.shape[0] == V:
                cv = pitch_cv
            elif pitch_cv.shape[0] == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[1]))
            else:
                cv = np.broadcast_to(pitch_cv.mean(axis=0), (V, pitch_cv.shape[1]))
            return self._pitch_shifter_core(module, frames, src, cv)

        if pitch_cv is not None and pitch_cv.ndim == 2:
            pitch_cv = pitch_cv.mean(axis=0)
        out = self._pitch_shifter_core(
            module,
            frames,
            src[np.newaxis, :],
            None if pitch_cv is None else pitch_cv[np.newaxis, :],
        )
        return out[0]

    def _pitch_shifter_core(self, module, frames, src, cv):
        """Shared ``(V, F)`` engine; mono runs with V=1 (bit-identical)."""
        V = src.shape[0]
        sr = self.sample_rate
        semis = float(module.params.get("semitones", 0.0))
        cents = float(module.params.get("cents", 0.0))
        cv_depth = float(module.params.get("cv_depth", 12.0))
        mix = float(np.clip(float(module.params.get("mix", 1.0)), 0.0, 1.0))
        grain_ms = float(module.params.get("grain_size", 50.0))
        overlap = max(1, min(8, int(module.params.get("overlap", 2))))
        Lg = max(8, int(round(grain_ms * 1e-3 * sr)))

        head = max(16384, 16 * int(getattr(self, "block_size", 512)))
        state = self._state.setdefault(module.id, {})
        if state.get("V") != V or state.get("Lg") != Lg or state.get("ov") != overlap:
            state.clear()
            state["V"] = V
            state["Lg"] = Lg
            state["ov"] = overlap
            state["eng"] = [_GrainShifter(Lg, overlap, head) for _ in range(V)]
        engines = state["eng"]

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        Dc = Lg  # approximate latency compensation for the dry tap
        base_st = semis + cents / 100.0
        out = np.empty((V, frames), dtype=np.float32)
        for v in range(V):
            st = base_st if cv is None else base_st + cv_depth * float(np.mean(cv[v]))
            st = max(-self._PS_MAX_ST, min(self._PS_MAX_ST, st))
            r = 2.0 ** (st / 12.0)
            eng = engines[v]
            wet = eng.process(src[v].astype(np.float64), r)
            if mix >= 1.0:
                blk = wet
            elif mix <= 0.0:
                blk = eng.dry_tap(frames, Dc)
            else:
                blk = (1.0 - mix) * eng.dry_tap(frames, Dc) + mix * wet
            out[v] = blk.astype(np.float32)
        return out

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
        try:
            info = sd.query_devices(in_device, "input")
            in_channels = max(1, min(2, int(info.get("max_input_channels", 1))))
        except Exception:
            in_channels = 1
        return in_device, in_channels

    def _render_file_player(self, module, frames: int, buffers=None, patch=None):
        """Stream a decoded WAV file to the ``left`` / ``right`` audio outs.

        Decodes lazily on first use (and re-decodes after a path change)
        into ``self._state``; every block thereafter is a slice of the
        in-memory array, so there is no per-block disk I/O. One-shot by
        default -- ``loop`` wraps the playhead with modular indexing so a
        block straddling the loop point reads seamlessly. Both ports are
        always returned (zeros when idle/finished) so downstream wiring is
        defined whether or not the file is sounding.
        """
        state = self._state.setdefault(
            module.id, {"path": None, "samples": None, "pos": 0}
        )
        path = str(module.params.get("path", ""))
        if state["samples"] is None or state["path"] != path:
            # First arrival, or the user pointed at a different file.
            state["path"] = path
            state["samples"] = self._decode_audio(path, self.sample_rate)
            state["pos"] = 0

        armed = bool(module.params.get("armed", True))
        samples = state["samples"]
        if not armed or samples is None or samples.shape[1] == 0:
            if not armed:
                state["pos"] = 0  # re-arming replays from the top
            return {
                "left": np.zeros(frames, dtype=np.float32),
                "right": np.zeros(frames, dtype=np.float32),
            }

        n = samples.shape[1]
        pos = int(state["pos"])
        gain = float(module.params.get("gain", 1.0))
        loop = bool(module.params.get("loop", False))

        left = np.zeros(frames, dtype=np.float32)
        right = np.zeros(frames, dtype=np.float32)

        if loop:
            idx = (np.arange(frames) + pos) % n
            left[:] = samples[0, idx]
            right[:] = samples[1, idx]
            state["pos"] = (pos + frames) % n
        else:
            if pos < n:
                take = min(frames, n - pos)
                left[:take] = samples[0, pos:pos + take]
                right[:take] = samples[1, pos:pos + take]
                # Park at n once finished -> silence on every later block.
                state["pos"] = pos + take
            # else: already past the end; both buffers stay zero.

        if gain != 1.0:
            left *= gain
            right *= gain
        return {"left": left, "right": right}

    # ----- distortion -------------------------------------------------------

    _DIST_DRIVE_MIN = 0.01
    _DIST_DRIVE_MAX = 60.0
    _DIST_TONE_BYPASS = 19999.0  # tone at/above this -> filter out of circuit
    _TUBE_BIAS = 0.25  # asymmetry of the tube curve (even-harmonic content)

    @staticmethod
    def _dist_curve(mode: str, drive, u):
        """Apply one saturation curve at the oversampled rate.

        ``u`` is the (V, 4F) oversampled input; ``drive`` is a positive
        scalar or a (V, 4F) per-sample array (CV-modulated). All three
        curves are normalised so full-scale input maps to full-scale
        output, and all tend to the identity as drive -> 0:

          soft: tanh(d*u)/tanh(d)            (odd harmonics, smooth)
          hard: clip(d*u, -1, 1)             (odd harmonics, buzzy)
          tube: biased tanh, zero-through    (even + odd harmonics)

        The tube curve is tanh(d*u + c) - tanh(c) with a CONSTANT bias
        c (not scaled by drive): the positive and negative halves bend
        at different points, which is what generates even harmonics.
        It is normalised by whichever rail is larger, so output stays
        in [-1, 1] for |u| <= 1, passes exactly through zero, and tends
        to the identity as d -> 0. The small DC the asymmetry creates
        on a symmetric signal is removed by the caller's DC blocker.
        """
        if mode == "hard":
            return np.clip(drive * u, -1.0, 1.0)
        if mode == "tube":
            c = NumpyBackend._TUBE_BIAS
            tc = math.tanh(c)
            pos_rail = np.tanh(drive + c) - tc
            neg_rail = np.tanh(drive - c) + tc
            return (np.tanh(drive * u + c) - tc) / np.maximum(pos_rail, neg_rail)
        # soft (default)
        return np.tanh(drive * u) / np.tanh(drive)

    def _render_distortion(self, module, frames: int, buffers, patch):
        """Drive pedal: 4x-oversampled saturation with tone and mix.

        Chain: up 4x -> curve (per-sample drive = drive + cv_depth *
        drive_cv, clamped) -> down 4x -> DC blocker (tube mode only) ->
        one-pole tone low-pass (bypassed at 20 kHz) -> level -> blend
        with a 16-sample delay-compensated dry tap.

        Shape-polymorphic: mono runs the same (V, F) core with V=1, so a
        single voice row is bit-identical to mono; per-voice filter and
        oversampler state keeps voices fully independent. ``mix`` <= 0
        returns the input untouched (bit-exact, no state advance) --
        the same contract as the chorus/flanger/phaser dry path.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None or src.size == 0:
            return {"out": np.zeros(frames, dtype=np.float32)}

        mix = float(module.params.get("mix", 1.0))
        if mix <= 0.0:
            return {"out": src}  # bit-exact bypass

        was_mono = src.ndim == 1
        x = np.atleast_2d(src).astype(np.float64)
        v = x.shape[0]

        state = self._state.setdefault(module.id, {})
        os4 = state.get("os4")
        if os4 is None or os4.voices != v:
            state["os4"] = os4 = _Oversampler4(v)
            state["dc_zi"] = np.zeros((v, 1))
            state["tone_zi"] = np.zeros((v, 1))
            state["dry_tail"] = np.zeros((v, _OS_LATENCY))

        drive = float(module.params.get("drive", 4.0))
        mode = str(module.params.get("mode", "soft"))
        if mode not in ("soft", "hard", "tube"):
            mode = "soft"
        tone = float(module.params.get("tone", 20000.0))
        level = float(module.params.get("level", 1.0))
        cv_depth = float(module.params.get("cv_depth", 5.0))

        cv = self._input_buffer(patch, buffers, module.id, "drive_cv", collapse=False)
        if cv is not None and cv.size and cv_depth != 0.0:
            d = drive + cv_depth * np.atleast_2d(cv).astype(np.float64)
            d = np.clip(d, self._DIST_DRIVE_MIN, self._DIST_DRIVE_MAX)
            d = np.broadcast_to(d, x.shape)
            # Zero-order hold up to the oversampled rate (control signal:
            # its own images are far below audio significance).
            d_up = np.repeat(d, _OS_FACTOR, axis=-1)
        else:
            d_up = min(max(drive, self._DIST_DRIVE_MIN), self._DIST_DRIVE_MAX)

        shaped = self._dist_curve(mode, d_up, os4.up(x))
        wet = os4.down(shaped)

        if mode == "tube":
            wet, state["dc_zi"] = _dc_block(wet, state["dc_zi"])

        if tone < self._DIST_TONE_BYPASS:
            # One-pole low-pass, streaming (zi carried per voice).
            a0 = 1.0 - math.exp(-2.0 * math.pi * max(tone, 20.0) / self.sample_rate)
            wet, state["tone_zi"] = lfilter(
                [a0], [1.0, a0 - 1.0], wet, axis=-1, zi=state["tone_zi"]
            )

        if level != 1.0:
            wet = wet * level

        if mix >= 1.0:
            out = wet
            # Keep the dry tail warm so sweeping mix down mid-stream
            # doesn't splice in a stale block.
            buf = np.concatenate([state["dry_tail"], x], axis=-1)
            state["dry_tail"] = buf[:, frames:]
        else:
            buf = np.concatenate([state["dry_tail"], x], axis=-1)
            dry = buf[:, :frames]
            state["dry_tail"] = buf[:, frames:]
            out = (1.0 - mix) * dry + mix * wet

        out32 = out.astype(np.float32)
        return {"out": out32[0] if was_mono else out32}

    # ----- waveshaper (wavefolder) -------------------------------------------

    _FOLD_MAX = 32.0

    @staticmethod
    def _fold_curve(mode: str, u):
        """Fold ``u`` (any real) back into [-1, 1].

        triangle: exact geometric reflection at the rails — the
        periodic triangle function of u, which is the IDENTITY for
        |u| <= 1 and reflects beyond (period 4: 1 -> 1, 2 -> 0,
        3 -> -1, ...). Vectorised via one mod.

        sine: sin(pi/2 * u) — smooth trigonometric folding; for
        |u| <= 1 it is a gentle S-curve rather than the identity
        (that's the mode's character, not an error).
        """
        if mode == "sine":
            return np.sin((np.pi / 2.0) * u)
        return np.abs(np.mod(u - 1.0, 4.0) - 2.0) - 1.0

    def _render_waveshaper(self, module, frames: int, buffers, patch):
        """Wavefolder: 4x-oversampled fold with symmetry and mix.

        Chain: up 4x -> u = fold_total * x + symmetry (fold_total =
        fold + cv_depth * fold_cv per sample, clamped 0.._FOLD_MAX) ->
        fold curve -> down 4x -> DC blocker (only when symmetry != 0;
        a centred fold generates no DC and triangle mode's below-rails
        passthrough stays exact) -> blend with the delay-compensated
        dry tap. Same oversampler/latency/mix contract as Distortion:
        shape-polymorphic, per-voice state, mix <= 0 returns the input
        bit-exactly.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None or src.size == 0:
            return {"out": np.zeros(frames, dtype=np.float32)}

        mix = float(module.params.get("mix", 1.0))
        if mix <= 0.0:
            return {"out": src}  # bit-exact bypass

        was_mono = src.ndim == 1
        x = np.atleast_2d(src).astype(np.float64)
        v = x.shape[0]

        state = self._state.setdefault(module.id, {})
        os4 = state.get("os4")
        if os4 is None or os4.voices != v:
            state["os4"] = os4 = _Oversampler4(v)
            state["dc_zi"] = np.zeros((v, 1))
            state["dry_tail"] = np.zeros((v, _OS_LATENCY))

        fold = float(module.params.get("fold", 1.0))
        symmetry = float(module.params.get("symmetry", 0.0))
        mode = str(module.params.get("mode", "triangle"))
        if mode not in ("triangle", "sine"):
            mode = "triangle"
        cv_depth = float(module.params.get("cv_depth", 4.0))

        cv = self._input_buffer(patch, buffers, module.id, "fold_cv", collapse=False)
        if cv is not None and cv.size and cv_depth != 0.0:
            g = fold + cv_depth * np.atleast_2d(cv).astype(np.float64)
            g = np.clip(g, 0.0, self._FOLD_MAX)
            g = np.broadcast_to(g, x.shape)
            g_up = np.repeat(g, _OS_FACTOR, axis=-1)  # zero-order hold
        else:
            g_up = min(max(fold, 0.0), self._FOLD_MAX)

        u = g_up * os4.up(x)
        if symmetry != 0.0:
            u = u + symmetry
        wet = os4.down(self._fold_curve(mode, u))

        if symmetry != 0.0:
            wet, state["dc_zi"] = _dc_block(wet, state["dc_zi"])

        if mix >= 1.0:
            out = wet
            buf = np.concatenate([state["dry_tail"], x], axis=-1)
            state["dry_tail"] = buf[:, frames:]
        else:
            buf = np.concatenate([state["dry_tail"], x], axis=-1)
            dry = buf[:, :frames]
            state["dry_tail"] = buf[:, frames:]
            out = (1.0 - mix) * dry + mix * wet

        out32 = out.astype(np.float32)
        return {"out": out32[0] if was_mono else out32}

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
        """
        peak = 0.0 if src is None or src.size == 0 else float(np.max(np.abs(src)))
        coeff = 0.1 ** (frames / self.sample_rate / release)

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

        return (level, hold, clip)

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
        if mode != "rms":
            mode = "peak"  # unknown values fall back to the default

        state = self._state.setdefault(module.id, {})
        for suffix in ("_l", "_r"):
            if "env" + suffix not in state:
                state["env" + suffix] = 0.0
                state["hold" + suffix] = 0.0
                state["hold_age" + suffix] = 0
                # Start far past the lamp window so a fresh meter is unlit.
                state["clip_age" + suffix] = 1 << 62
                state["rms_sq" + suffix] = 0.0

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

        self._audio_levels[module.id] = left[0]
        self._audio_meter_state[module.id] = (left, right)

        return {"out": out, "out_r": out_r}

    def _decode_audio(self, path, target_sr):
        """Decode any supported media file to ``(2, N)`` float32 or None.

        WAV takes the zero-dependency scipy fast path. Anything that
        isn't a readable WAV — mp3/flac/ogg/m4a, the audio track of a
        video (mp4/mkv/mov/webm), or even a 24-bit WAV scipy can't
        open — falls back to ffmpeg when it's available (bundled via
        the ``[media]`` extra, or a system ffmpeg). ``None`` on total
        failure, so the player renders silence rather than raising.
        """
        samples = self._load_wav(path, target_sr)
        if samples is not None:
            return samples
        return media.decode_with_ffmpeg(path, target_sr)

    @staticmethod
    def _load_wav(path, target_sr):
        """Decode a WAV file to a contiguous ``(2, N)`` float32 array.

        Returns ``None`` on any failure (empty/missing path, unreadable or
        unsupported encoding) so the audio thread renders silence rather
        than raising. Integer PCM is normalised to [-1, 1] by dtype; mono is
        duplicated to stereo; >2 channels keep the first two; the audio is
        resampled to ``target_sr`` when the file's native rate differs (a
        one-time cost at load, not per block). 24-bit PCM is unsupported by
        scipy and surfaces here as a caught read error -> silence.
        """
        import os

        if not path or not os.path.isfile(path):
            return None
        try:
            file_sr, data = wavfile.read(path)
        except Exception as exc:  # pragma: no cover - filesystem/codec-specific
            print(f"[FilePlayer] cannot read {path}: {exc}")
            return None

        data = np.asarray(data)
        if data.dtype == np.int16:
            flo = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            flo = data.astype(np.float32) / 2147483648.0
        elif data.dtype == np.uint8:
            flo = (data.astype(np.float32) - 128.0) / 128.0
        elif data.dtype in (np.float32, np.float64):
            flo = data.astype(np.float32)
        else:  # pragma: no cover - exotic dtype; best-effort peak-normalise
            flo = data.astype(np.float32)
            peak = float(np.max(np.abs(flo))) or 1.0
            flo = flo / peak

        # -> (channels, N)
        chans = flo[np.newaxis, :] if flo.ndim == 1 else flo.T

        if int(file_sr) != int(target_sr) and chans.shape[1] > 0:
            from math import gcd
            g = gcd(int(file_sr), int(target_sr))
            up = int(target_sr) // g
            down = int(file_sr) // g
            chans = resample_poly(chans, up, down, axis=1).astype(np.float32)

        if chans.shape[0] == 1:
            stereo = np.repeat(chans, 2, axis=0)
        elif chans.shape[0] >= 2:
            stereo = chans[:2]
        else:
            return None
        return np.ascontiguousarray(stereo, dtype=np.float32)

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
