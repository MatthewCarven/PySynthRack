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
import time
import wave
from typing import Any

import numpy as np
from scipy.signal import butter, firwin, lfilter, resample_poly, sosfilt

from . import media
from scipy.io import wavfile

from ..core.patch import Patch
from ..modules.keyboard import midi_to_freq
from ..modules.cv_keyboard import CV_REFERENCE_NOTE, KEY_GATE_NAMES
from ..modules.cv_gates import KEY_CV_NAMES
from ..modules.fm_op import snap_ratio as _fm_snap_ratio
from .backend import AudioBackend

# Imported lazily so a missing PortAudio install doesn't crash module import.
try:
    import sounddevice as sd  # type: ignore
    _HAS_SOUNDDEVICE = True
except Exception:  # pragma: no cover - environment-dependent
    sd = None  # type: ignore[assignment]
    _HAS_SOUNDDEVICE = False


# Bounds for a buffered sink's own stream block size. PortAudio accepts an
# arbitrary blocksize, so these are just defensive rails against a corrupt or
# hostile ``buffer_size`` in a loaded patch — 16 keeps the callback rate sane,
# 8192 caps the secondary ring allocation (8 blocks * 8192 * 2ch * 4B ≈ 512 KB).
# The UI offers 64..8192 for the buffered sink (ui/buffer.SINK_BUFFER_SIZES),
# so its top stop sits exactly on the upper rail by design.
_MIN_SINK_BLOCK = 16
_MAX_SINK_BLOCK = 8192


class _DeviceOutput:
    """A secondary stereo OutputStream feeding one device-routed speaker
    sink, fed by the main audio callback through a sample-accurate ring.

    The graph is rendered once per block on the *main* stream's callback,
    which pushes this device's ``(frames, 2)`` block into the ring; this
    stream's own callback pops exactly the samples PortAudio asks for. The
    two streams run on independent PortAudio clocks, so the ring absorbs
    scheduling jitter and the slow relative drift between two unsynchronised
    devices.

    Crucially the ring is counted in **samples, not blocks**, so the
    secondary stream's block size may differ from the main stream's -- that
    is the whole point of the buffered sink, whose ``buffer_size`` opens this
    stream at its own PortAudio blocksize independent of the global one. A
    device that ran ahead (empty ring) has its block zero-padded; a device
    that fell behind (full ring) has its oldest samples overwritten
    (drop-oldest). Capacity is ``max_blocks`` *device* blocks, so the ring
    always holds at least one full device block however large it is relative
    to the main push size.

    Producer (main audio thread, :meth:`push`) and consumer (this device's
    PortAudio thread, :meth:`_callback`) share the read cursor and fill
    count, so a small ``threading.Lock`` guards each. It is held only for a
    bounded copy of at most one block of audio -- the right trade for the
    cue / monitor bus this sink is for, which is not the primary low-latency
    path, and far shorter than the render lock the main callback already
    holds. The trade is a few blocks of added latency on the second device."""

    def __init__(
        self, device: str, sample_rate: int, block_size: int,
        max_blocks: int = 8,
    ) -> None:
        self.device = device
        self._sample_rate = sample_rate
        self._block_size = block_size
        # Ring counted in samples so the push size (main block) and the pop
        # size (this device's block) may differ. Sized to hold max_blocks of
        # THIS device's block, so even a secondary buffer larger than the main
        # block always has room to fill one full pop.
        self._capacity = max(1, max_blocks) * max(1, int(block_size))
        self._ring = np.zeros((self._capacity, 2), dtype=np.float32)
        self._read = 0        # index of the oldest queued sample
        self._avail = 0       # samples currently queued, 0 .. capacity
        # Ring health counters for the GUI readout (see telemetry()), both
        # cumulative since open. An underrun is a device callback the ring
        # couldn't fully serve (its tail was zero-padded); a drop is a push
        # that lost audio — it overwrote unread samples (drop-oldest fired)
        # or was itself bigger than the whole ring (its head truncated away).
        # Guarded by the same lock as the cursors. Underruns only count once
        # _primed flips, which happens when the fill FIRST reaches one device
        # block: PortAudio fires callbacks to prime the stream before the
        # first render lands, and a device block larger than the main block
        # (the 2048/4096/8192 stops) needs many pushes before it can serve
        # one callback at all — both are startup fill-up, not a cushion
        # signal, and neither should read as trouble on a clean Start.
        self._underruns = 0
        self._drops = 0
        self._primed = False
        self._lock = threading.Lock()
        self._stream: Any = None

    def open(self) -> None:
        """Open and start the PortAudio stream on the named device."""
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=2,
            blocksize=self._block_size,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def push(self, block: np.ndarray) -> None:
        """Enqueue one rendered ``(frames, 2)`` block (main audio thread).

        Writes the block into the ring, dropping the oldest samples if it
        would overflow. A block larger than the whole ring keeps only its
        last ``capacity`` samples."""
        n = int(block.shape[0])
        if n <= 0:
            return
        cap = self._capacity
        with self._lock:
            dropped = n > cap                  # oversize block loses its head
            if n >= cap:
                block = block[n - cap:]
                n = cap
            w = (self._read + self._avail) % cap
            end = w + n
            if end <= cap:
                self._ring[w:end] = block
            else:                              # wraps the ring end
                k = cap - w
                self._ring[w:] = block[:k]
                self._ring[:n - k] = block[k:]
            new_avail = self._avail + n
            if new_avail > cap:
                # The write overran the oldest unread samples; advance the
                # read cursor past them (drop-oldest) and pin to capacity.
                self._read = (self._read + (new_avail - cap)) % cap
                new_avail = cap
                dropped = True
            self._avail = new_avail
            if dropped:
                self._drops += 1
            # Armed the moment the ring can serve one whole device block;
            # from here on a short callback is genuine starvation. (cap is
            # >= one device block by construction; the min is a rail against
            # a degenerate hand-constructed ring.)
            if not self._primed and new_avail >= min(self._block_size, cap):
                self._primed = True

    def _callback(self, outdata, frames, time_info, status) -> None:
        cap = self._capacity
        with self._lock:
            n = min(self._avail, frames)
            if n < frames and self._primed:
                self._underruns += 1
            if n > 0:
                r = self._read
                end = r + n
                if end <= cap:
                    outdata[:n] = self._ring[r:end]
                else:                          # wraps the ring end
                    k = cap - r
                    outdata[:k] = self._ring[r:]
                    outdata[k:n] = self._ring[:n - k]
                self._read = end % cap
                self._avail -= n
        if n < frames:
            outdata[n:] = 0.0                  # underrun: zero-pad the tail

    def telemetry(self) -> tuple[int, int, int, int]:
        """``(queued, capacity, underruns, drops)`` — the GUI readout hook.

        Queued/capacity are samples; the counters are cumulative events since
        open (see the __init__ notes for what each one means). The lock is
        held for four int reads, far shorter than push's bounded block copy,
        and this is called at GUI frame rate, so contention is negligible.
        """
        with self._lock:
            return (self._avail, self._capacity, self._underruns, self._drops)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        finally:
            self._stream = None
            with self._lock:
                self._read = 0
                self._avail = 0
                self._underruns = 0
                self._drops = 0
                self._primed = False


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


def _design_fs_hilbert(numtaps):
    """Windowed Type-III FIR Hilbert transformer (odd length).

    Antisymmetric with an *integer* group delay of ``(numtaps-1)//2``
    samples. The ideal impulse response ``2/(pi n)`` (nonzero on odd taps
    only) is Hamming-windowed: a flat passband and > 55 dB opposite-
    sideband rejection right across the audio band -- crucially holding
    that rejection down to low frequencies, where wider windows (Blackman,
    Kaiser) collapse against the Type-III DC null.
    """
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = np.zeros(numtaps)
    nz = n != 0
    h[nz] = (1.0 - np.cos(np.pi * n[nz])) / (np.pi * n[nz])
    h *= np.hamming(numtaps)
    return h


# FreqShifter (Bode single-sideband) Hilbert pair. 255 taps -> an integer
# group delay of 127 samples (~2.9 ms @ 44.1k), which is the module's wet
# latency; the dry path is delay-matched in the ``mix`` blend so a shift of
# 0 Hz stays phase-coherent instead of combing.
_FS_TAPS = 255
_FS_LATENCY = (_FS_TAPS - 1) // 2
_FS_HILBERT = _design_fs_hilbert(_FS_TAPS)


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


def _hermite4(pm1, p0, p1, p2, t):
    """4-point, 3rd-order Hermite (Catmull-Rom) fractional interpolation.

    ``t`` in [0, 1) is the read position between ``p0`` and ``p1``;
    ``pm1``/``p2`` are the outer neighbours (the two samples flanking
    that pair). Compared with 2-tap linear this holds the passband far
    flatter toward Nyquist and pushes the imaging/interpolation
    sidebands down ~20-30 dB, so non-integer transposition and detune
    stay clean instead of dull-and-gritty.

    Two facts the resampler leans on:
      * at ``t == 0`` this returns ``p0`` *exactly* (the constant term
        is ``p0``, untouched by float ops), so an integer-position read
        -- unity ratio, octave shifts -- is a bit-exact passthrough,
        same as linear was;
      * the spline is interpolating (``t == 1`` returns ``p1``) and
        C1-continuous, so seam crossfades of two Hermite reads stay
        click-free.

    Arrays broadcast elementwise; a scalar ``t`` works too.
    """
    c0 = p0
    c1 = 0.5 * (p1 - pm1)
    c2 = pm1 - 2.5 * p0 + 2.0 * p1 - 0.5 * p2
    c3 = 0.5 * (p2 - pm1) + 1.5 * (p0 - p1)
    return ((c3 * t + c2) * t + c1) * t + c0


def _brake_ramp(pos, gate, down, up):
    """Integrate the resampler's tape-stop brake position over one block.

    ``pos`` is the position entering the block (1 = full speed, 0 =
    stopped); ``gate`` a ``(F,)`` bool array (True = brake engaged);
    ``down``/``up`` the per-sample ramp slopes (position units per
    sample, both positive). Returns ``(factor (F,) float64, end pos)``
    where ``factor[n]`` is the position after sample ``n``'s step,
    clipped to [0, 1] -- a ramp linear in speed, the constant-torque
    way a platter or capstan actually winds down and back up.

    Vectorized segment-wise: within a run of equal gate values the
    position is just a clipped linear ramp, and a block rarely holds
    more than a couple of gate edges, so the Python loop is over
    segments, not samples.
    """
    n_frames = gate.shape[0]
    factor = np.empty(n_frames, dtype=np.float64)
    edges = np.flatnonzero(gate[1:] != gate[:-1]) + 1
    start = 0
    for end in (*edges, n_frames):
        slope = -down if gate[start] else up
        seg = pos + slope * np.arange(1.0, end - start + 1.0)
        np.clip(seg, 0.0, 1.0, out=seg)
        factor[start:end] = seg
        pos = float(seg[-1])
        start = end
    return factor, pos


def _detect_period(x: np.ndarray, sr: int, fmin: float = 25.0,
                   fmax: float = 800.0):
    """Autocorrelation fundamental-period estimate of 1D ``x``.

    Returns the period in samples (float, parabolic-refined) when a
    clear repeat exists, else None. The peak must reach half the
    zero-lag energy (rejects noise/silence), and the *smallest* lag
    within 90% of the best peak wins, so a perfectly periodic input
    doesn't alias to a subharmonic (2P, 3P, ... score just as well).
    """
    n = int(x.shape[0])
    lag_max = int(sr / fmin)
    lag_min = max(2, int(sr / fmax))
    if lag_max <= lag_min or n < lag_max + lag_min:
        return None
    xw = x - x.mean()
    if float(np.dot(xw, xw)) < 1e-9:
        return None
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    X = np.fft.rfft(xw, nfft)
    ac = np.fft.irfft(X * np.conj(X))[: lag_max + 2]
    ac = ac / (ac[0] + 1e-12)
    # Unbias the linear autocorrelation (fewer overlapping samples at
    # long lags would otherwise punish deep-bass periods).
    lags = np.arange(ac.shape[0])
    ac = ac * (n / np.maximum(n - lags, n / 4.0))
    seg = ac[lag_min:lag_max + 1]
    # Candidates are LOCAL peaks only -- the ACF of a low tone is still
    # high at lag_min (it hasn't decayed yet), so a plain threshold
    # scan would lock onto that shoulder instead of the true period.
    d1 = np.diff(seg)
    # Interior peaks only: a boundary sample on the ACF's initial decay
    # (still high at lag_min for a low tone) must never qualify.
    pk = 1 + np.nonzero((d1[:-1] > 0) & (d1[1:] <= 0))[0]
    if pk.size == 0:
        return None
    vals = seg[pk]
    m = float(vals.max())
    if m < 0.5:
        return None
    # Smallest peak lag within 90% of the best -- a perfectly periodic
    # input scores multiples of P equally; take P, not 2P/3P.
    k = int(pk[vals >= 0.9 * m][0]) + lag_min
    if 1 <= k < ac.shape[0] - 1:
        y0, y1, y2 = float(ac[k - 1]), float(ac[k]), float(ac[k + 1])
        den = y0 - 2.0 * y1 + y2
        d = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
        return float(k + float(np.clip(d, -0.5, 0.5)))
    return float(k)


def _lpc_coeffs(x: np.ndarray, order: int, sr: int):
    """Levinson-Durbin LPC of ``x`` -> full coeff vector [1, a1..ap].

    Autocorrelation method with a ~60 Hz Gaussian lag window and a tiny
    white-noise floor (keeps the recursion positive definite -> a
    stable synthesis filter), plus reflection-coefficient clamping as a
    second belt. Returns None when the block has no usable energy.
    ``A(z) = 1 + a1 z^-1 + ...`` whitens; ``1/A(z)`` re-colors.
    """
    n = int(x.shape[0])
    if n < 2 * order:
        return None
    xw = x * np.hanning(n)
    if float(np.dot(xw, xw)) < 1e-10:
        return None
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    X = np.fft.rfft(xw, nfft)
    rr = np.fft.irfft(X * np.conj(X))[: order + 1]
    lags = np.arange(order + 1)
    rr = rr * np.exp(-0.5 * (2.0 * np.pi * 60.0 * lags / sr) ** 2)
    rr[0] *= 1.0 + 1e-4
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = float(rr[0])
    for m in range(1, order + 1):
        acc = rr[m] + float(np.dot(a[1:m], rr[1:m][::-1]))
        k = -acc / err if err > 1e-12 else 0.0
        k = float(np.clip(k, -0.999, 0.999))
        prev = a[1:m].copy()
        a[1:m] += k * prev[::-1]
        a[m] = k
        err *= (1.0 - k * k)
    return a


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

    The analysis pointer lives on the **ideal float grid** (``a += Hs/r``
    per grain) and never absorbs the similarity-search offset -- the
    canonical WSOLA formulation. (An earlier revision accumulated the
    offset, which on periodic input turned a constant alignment residue
    into a systematic input-consumption drift: mild starvation pulled
    the pitch a few cents at some settings, and in the other direction
    the analysis pointer fell out of the ring and production deadlocked
    -- deep bass and several grain/ratio combos died to DC.) The NCC
    peak is refined parabolically and grains are extracted at the
    resulting *fractional* position, so overlap joins stay
    phase-continuous to sub-sample accuracy (sub-cent pitch).

    One engine handles one channel; the renderer keeps a list of these,
    one per voice slot, so a single voice is bit-identical to the mono
    render (same deterministic ops). ``db`` is an optional parallel ring
    of the *raw* input (fed via ``process(..., x_dry=...)``) so the dry
    tap stays true when the wet path is fed a whitened signal for
    formant preservation.
    """

    def __init__(self, grain: int, overlap: int, head: int) -> None:
        self.Lg = max(8, int(grain))
        self.Hs = max(1, self.Lg // max(1, int(overlap)))
        self.Lov = max(1, self.Lg - self.Hs)
        self.seek = max(1, self.Hs // 2)
        self.win = np.hanning(self.Lg)
        self.Lin = self.Lg + 2 * self.seek + head + 64
        self.Lstr = 2 * self.Lg + head + 64
        self.ib = np.zeros(self.Lin)        # engine-input ring
        self.db = None                       # raw ring for the dry tap
        self.ss = np.zeros(self.Lstr)       # stretched signal ring (OLA accum)
        self.sw = np.zeros(self.Lstr)       # stretched window-sum ring
        self.iw = 0                          # total input samples written
        self.onset = 0                       # synth index of next grain
        self.final = 0                       # stretched samples finalized
        self.a = 0.0                         # IDEAL analysis grid (abs input idx)
        self.tgt = np.zeros(self.Lov)        # similarity-search target
        self.have_tgt = False
        self.rp = 0.0                        # resample read ptr (abs stretched)
        self.zeroed = 0                      # stretched idx zeroed up to
        self.primed = False
        self.bias = 1e-3                     # smallest-shift tie-break bias

    def _produce_one(self, r: float) -> bool:
        Lg, Hs, Lov, seek = self.Lg, self.Hs, self.Lov, self.seek
        c = int(round(self.a))
        if c + seek + Lg + 2 > self.iw:              # not enough input yet
            return False
        if c - seek - 1 < self.iw - self.Lin + 2:
            # The grid fell off the back of the ring (only possible
            # after an abnormal stall -- with the ideal-grid pointer
            # this is a safety net, not a steady state). Snap forward.
            self.a = float(self.iw - self.Lin + 3 + seek)
            c = int(round(self.a))
            if c + seek + Lg + 2 > self.iw:
                return False
        if not self.have_tgt:
            p = float(c)
        else:
            seg = self.ib[(c - seek + np.arange(2 * seek + Lov + 1)) % self.Lin]
            dot = np.correlate(seg[:-1], self.tgt, "valid")          # (2*seek+1,)
            cs = np.concatenate([[0.0], np.cumsum(seg * seg)])
            nrm = np.sqrt(np.maximum(cs[Lov:] - cs[:-Lov], 1e-12))[: 2 * seek + 1]
            tn = float(np.linalg.norm(self.tgt)) + 1e-9
            ncc = dot / (nrm * tn) - self.bias * np.abs(np.arange(-seek, seek + 1)) / seek
            k = int(np.argmax(ncc))
            if 0 < k < 2 * seek:
                # Parabolic sub-sample refinement of the NCC peak.
                y0, y1, y2 = float(ncc[k - 1]), float(ncc[k]), float(ncc[k + 1])
                den = y0 - 2.0 * y1 + y2
                dfr = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
                dfr = float(np.clip(dfr, -0.5, 0.5))
            else:
                dfr = 0.0
            p = c + (k - seek) + dfr
        # Fractional grain extraction (linear interp) at position p.
        i0 = int(np.floor(p))
        fr = p - i0
        seg2 = self.ib[(i0 + np.arange(Lg + 1)) % self.Lin]
        g = seg2[:-1] * (1.0 - fr) + seg2[1:] * fr
        ring = (self.onset + np.arange(Lg)) % self.Lstr
        self.ss[ring] += self.win * g
        self.sw[ring] += self.win
        # Target = this grain's continuation Hs later (fractional too).
        ts = self.ib[(i0 + Hs + np.arange(Lov + 1)) % self.Lin]
        self.tgt = ts[:-1] * (1.0 - fr) + ts[1:] * fr
        self.have_tgt = True
        self.a += Hs / r      # ideal grid: search excursions never accumulate
        self.onset += Hs
        self.final = self.onset
        return True

    def process(self, x: np.ndarray, r: float, x_dry=None) -> np.ndarray:
        """Push one input block (1D float64), return the shifted block.

        ``x_dry``, when given, is written to the parallel raw ring so
        ``dry_tap`` reads the true input even when ``x`` is a whitened
        residual (formant-preserve mode).
        """
        F = x.shape[0]
        if F == 0:
            return np.zeros(0, dtype=np.float64)
        slots = (self.iw + np.arange(F)) % self.Lin
        self.ib[slots] = x
        if x_dry is not None:
            if self.db is None:
                self.db = np.zeros(self.Lin)
            self.db[slots] = x_dry
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

    def history(self, n: int, dry: bool = False) -> np.ndarray:
        """The last ``n`` input samples (raw ring when ``dry``), oldest
        first -- used to prime a replacement engine and for the LPC /
        period estimators."""
        n = int(min(n, self.iw, self.Lin - 4))
        src = self.db if (dry and self.db is not None) else self.ib
        return src[(self.iw - n + np.arange(n)) % self.Lin].copy()

    def dry_tap(self, F: int, Dc: float) -> np.ndarray:
        """Latency-compensated dry read of the most recent block. ``Dc`` is
        the wet-path latency in input samples (see :meth:`latency`), clamped
        to the ring's valid history so the read can never wrap onto stale
        samples when the grain is small relative to the block."""
        src = self.db if self.db is not None else self.ib
        Dc = min(max(float(Dc), 0.0), float(self.Lin - F - 4))
        dp = (self.iw - F) + np.arange(F) - Dc
        d0 = np.floor(dp).astype(np.int64)
        df = dp - d0
        return src[d0 % self.Lin] * (1.0 - df) + src[(d0 + 1) % self.Lin] * df

    def latency(self, r: float) -> float:
        """Exact input->output latency of the wet path, in input samples, so
        the dry tap can be delay-matched for a phase-coherent ``mix``.

        The block just emitted read the stretched signal at ``rp``, which maps
        back to input index ``rp / r`` (undoing the r x time-stretch); it was
        produced after consuming ``iw`` input samples, so an input sample takes
        ``iw - rp / r`` samples to appear in the wet output. Verified to the
        sample against a direct wet-vs-input cross-correlation at unison
        (corr 1.000 across grain/overlap settings). Before priming there is no
        wet yet -- fall back to the grain length.
        """
        if not self.primed:
            return float(self.Lg)
        return float(self.iw - self.rp / max(r, 1e-9))


class _PartitionedConvolver:
    """Uniformly-partitioned FFT convolution for one IR channel (overlap-save).

    Built for a *fixed* render block size ``B``. The IR is split into
    ``P = ceil(L / B)`` block-sized partitions, each transformed once at
    construction to a length ``N = 2B`` rfft (a B-sample block convolved with
    a B-sample partition is linear length ``2B - 1``, so ``N = 2B`` holds it
    with no time-aliasing). Each :meth:`process` call transforms the 2B window
    ``[previous block | current block]`` once, pushes that spectrum onto a
    frequency-domain delay line (FDL) of the last ``P`` input spectra,
    accumulates ``sum_p H[p] * FDL[p]`` (the frequency-domain multiply-add
    that *is* the partitioned convolution), inverse-transforms, and keeps the
    valid last-B overlap-save half.

    The overlap-save core is intrinsically zero-latency; a one-block output
    register defers each result by exactly one block, so the convolver
    presents a clean, fixed **one-block (B-sample) latency** that the module
    reports and delay-matches its dry path against. Everything is float64
    internally (numpy's FFT upcasts regardless); the caller casts the result.

    ``process`` is exact linear convolution for a given ``B`` up to FFT
    round-off, so streaming a signal through it equals
    ``scipy.signal.fftconvolve`` of the whole input to ~1e-6 -- the oracle the
    tests hold it to. Block size only changes the FFT round-off, never the
    math, so results across block sizes agree to the same tolerance (pinned,
    not bit-exact, because ``N = 2B`` differs).
    """

    __slots__ = ("B", "N", "L", "P", "H", "fdl", "prev_in", "out_reg")

    def __init__(self, ir, block: int) -> None:
        B = max(1, int(block))
        self.B = B
        self.N = 2 * B
        ir = np.asarray(ir, dtype=np.float64).ravel()
        if ir.size == 0:
            ir = np.zeros(1, dtype=np.float64)
        self.L = int(ir.size)
        P = max(1, (self.L + B - 1) // B)  # ceil(L / B)
        self.P = P
        bins = B + 1  # rfft length for an N = 2B transform
        H = np.zeros((P, bins), dtype=np.complex128)
        buf = np.zeros(self.N, dtype=np.float64)
        for p in range(P):
            seg = ir[p * B:(p + 1) * B]
            buf[:] = 0.0
            buf[:seg.size] = seg
            H[p] = np.fft.rfft(buf)
        self.H = H
        self.fdl = np.zeros((P, bins), dtype=np.complex128)
        self.prev_in = np.zeros(B, dtype=np.float64)
        self.out_reg = np.zeros(B, dtype=np.float64)

    def process(self, x) -> np.ndarray:
        """Convolve one length-B block; return length-B, delayed by one block."""
        B = self.B
        w = np.empty(self.N, dtype=np.float64)
        w[:B] = self.prev_in
        w[B:] = x
        X = np.fft.rfft(w)
        # Advance the frequency-domain delay line: newest spectrum at row 0.
        # np.roll returns a fresh array, so this is safe (an in-place slice
        # shift over overlapping memory is not). P is small; a ring-pointer
        # rewrite is a documented follow-up if the DSP budget calls for it.
        self.fdl = np.roll(self.fdl, 1, axis=0)
        self.fdl[0] = X
        acc = np.einsum("pk,pk->k", self.H, self.fdl)
        y = np.fft.irfft(acc, n=self.N)
        valid = y[B:]  # overlap-save: the last B samples are alias-free
        out = self.out_reg
        self.out_reg = valid
        self.prev_in = np.array(x, dtype=np.float64, copy=True)
        return out


# Convolver IR / wet-shaping bounds. `tone` is a wet low-pass whose maximum is
# a bypass; `predelay` is a wet-only delay; loaded IRs are length-capped by the
# DSP budget (the DSP% readout is the meter) and energy-normalised on load.
_CONV_TONE_MIN = 1000.0
_CONV_TONE_MAX = 20000.0           # at/above this the tone low-pass is OFF
_CONV_PREDELAY_MAX_MS = 500.0
_IR_MAX_SECONDS = 5.0              # IR length cap (truncate + short fade-out)


def _normalize_ir(left, right):
    """Energy-normalise a decoded IR so wet RMS ~ dry RMS, L/R image intact.

    Convolving white-ish input with an IR scales its RMS by the IR's L2 norm,
    so dividing by that norm makes the wet sit at roughly the dry's level (and
    stops a long/hot IR from blowing up). A *single* shared scale
    ``1 / max(||L||2, ||R||2)`` is applied to both channels: the louder gets
    unity RMS gain, the quieter keeps its relative level, so the stereo image
    survives. A silent IR (or the unit impulse, whose norm is 1) is unchanged.
    Returns ``(left, right, scale)``.
    """
    nl = float(np.sqrt(np.sum(left * left)))
    nr = float(np.sqrt(np.sum(right * right)))
    norm = max(nl, nr)
    if norm <= 1e-12:
        return left, right, 1.0
    scale = 1.0 / norm
    return left * scale, right * scale, scale


class _IRLoader:
    """Background IR decode + partition-FFT build for the Convolver.

    IRs load whole (they're short), but the decode (scipy WAV, or ffmpeg for
    everything else) and the per-channel partition FFTs must never run on the
    audio thread. Construction spawns a daemon worker that decodes ``path``
    to a contiguous ``(2, N)`` float32 via ``decode_fn`` (the backend's
    ``_decode_audio``: WAV fast path then ffmpeg), then builds one
    ``_PartitionedConvolver`` per IR channel for the given render block size
    -- sharing a single engine when the two channels are identical (a mono
    file), so a mono IR convolves once. The consumer polls ``done`` and then
    reads ``ready`` / ``failed`` + the engines; every field is a plain
    attribute (atomic under the GIL), so the audio thread needs no lock.

    IRs are not normalised here (a later slice); a hot IR is the user's to
    trim with ``gain``.
    """

    def __init__(self, path, target_sr, block, decode_fn) -> None:
        self.path = str(path)
        self.target_sr = int(target_sr)
        self.block = int(block)
        self._decode_fn = decode_fn
        self.ready = False
        self.failed = False
        self.done = False
        self.ir_l = None       # (N,) float64
        self.ir_r = None       # (N,) float64
        self.engine_l = None   # _PartitionedConvolver
        self.engine_r = None   # _PartitionedConvolver (is engine_l when mono)
        self.mono = False
        self.scale = 1.0
        self._thread = threading.Thread(
            target=self._work, daemon=True, name="IRLoad"
        )
        self._thread.start()

    def _work(self) -> None:
        try:
            stereo = self._decode_fn(self.path, self.target_sr)
            if stereo is None or stereo.shape[1] == 0:
                self.failed = True
                return
            left = np.ascontiguousarray(stereo[0], dtype=np.float64)
            right = np.ascontiguousarray(stereo[1], dtype=np.float64)
            # Cap length by the DSP budget (truncate + a short fade so the cut
            # doesn't click); the DSP% readout is the meter for IR length.
            cap = int(_IR_MAX_SECONDS * self.target_sr)
            if left.shape[0] > cap:
                left = np.array(left[:cap])
                right = np.array(right[:cap])
                fade = min(int(0.010 * self.target_sr), cap)
                if fade > 1:
                    ramp = np.linspace(1.0, 0.0, fade)
                    left[-fade:] *= ramp
                    right[-fade:] *= ramp
            # Energy-normalise on load (a shared scale preserves the L/R image).
            left, right, self.scale = _normalize_ir(left, right)
            self.mono = bool(np.array_equal(left, right))
            self.ir_l = left
            self.ir_r = right
            self.engine_l = _PartitionedConvolver(left, self.block)
            self.engine_r = (
                self.engine_l if self.mono
                else _PartitionedConvolver(right, self.block)
            )
            self.ready = True
        except Exception as exc:  # pragma: no cover - filesystem/codec-specific
            print(f"[Convolver] IR load failed for {self.path}: {exc}")
            self.failed = True
        finally:
            self.done = True

    def wait(self, timeout=None) -> bool:
        """Join the worker (tests / offline render only). True if usable."""
        self._thread.join(timeout)
        return self.ready and not self.failed

    def close(self) -> None:
        """No long-lived resource to kill; the daemon worker exits on its own."""
        return None


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
        # Secondary OutputStreams for device-routed speaker sinks (specific +
        # buffered), keyed by (device, block_size) so one device can carry
        # several streams at different buffer sizes. Opened in start()
        # (snapshotting each sink's selection), fed by the main callback,
        # closed in stop(). Empty in the common (no routed) case.
        self._device_outputs: dict[tuple[str, int], Any] = {}
        # Per-sink smoothed governor ratio (see _governed_ratio). Keyed by
        # module id; an entry exists only while that sink's ratio_cv is
        # cabled, so the unpatched path carries no state at all.
        self._sink_ratio: dict[int, float] = {}
        # Per-sink [L, R] _GrainShifter pair for the governed push's
        # pitch-preserving stage (see the actuation tail of
        # render_block_multi). Same lifecycle as _sink_ratio: created
        # lazily on the first governed block, dropped when ratio_cv is
        # uncabled, absent entirely on the unpatched path.
        self._sink_stretch: dict[int, list] = {}
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
        # DSP-load readout for the UI toolbar. After every rendered
        # block the audio thread updates a smoothed load figure (render
        # time over the block budget, frames / sample_rate), a
        # since-start() peak, and a count of over-budget blocks; the
        # GUI thread reads them via dsp_load_snapshot(). Same no-lock
        # discipline as the meters: float/int attribute assignment is
        # atomic under the GIL and a stale frame is harmless.
        self._dsp_load: float = 0.0
        self._dsp_load_peak: float = 0.0
        self._dsp_overloads: int = 0

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
            zero_ch = (0.0, 0.0, False, 0)
            self._audio_meter_state = {
                mid: (
                    zero_ch,
                    zero_ch
                    if any(c.dst_port == "in_r" for c in patch.cables_into(mid))
                    else None,
                    False,
                    "peak",
                    None,
                )
                for mid, m in patch.modules.items()
                if m.TYPE == "meter"
            }
            # Recompile resets the clip counters -- a fresh run starts
            # its "how many times did this clip" tally from zero.
            for mid, m in patch.modules.items():
                if m.TYPE == "meter" and mid in self._state:
                    st = self._state[mid]
                    for suffix in ("_l", "_r"):
                        if "clips" + suffix in st:
                            st["clips" + suffix] = 0
                            st["over_tail" + suffix] = False
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
                    # Likewise a file_player mid-decode: kill its ffmpeg
                    # and let the worker thread exit before dropping it.
                    if self._state_types.get(mid) == "file_player":
                        dec = self._state[mid].get("decoder")
                        if dec is not None:
                            dec.close()
                    # Likewise a convolver mid-IR-load: release its loader
                    # before dropping the state.
                    if self._state_types.get(mid) == "convolver":
                        pend = self._state[mid].get("pending")
                        if isinstance(pend, dict) and pend.get("loader") is not None:
                            pend["loader"].close()
                    self._state.pop(mid, None)
                    if mid not in live_types:
                        self._state_types.pop(mid, None)
            # Record the current type for every live module so the next
            # compile can compare against it.
            self._state_types = dict(live_types)

            # FilePlayer lifecycle: kick each player's background decode
            # NOW, on the compile (UI) thread, so by the time the stream
            # renders its first block the file is usually already sounding
            # — and a big video's ffmpeg decode never runs on (or blocks)
            # the audio thread. Path unchanged -> keep the existing
            # decoder and its decoded audio.
            for mid, m in patch.modules.items():
                if m.TYPE != "file_player":
                    continue
                st = self._state.setdefault(
                    mid, {"path": None, "decoder": None, "pos": 0, "seek": None}
                )
                fp_path = str(m.params.get("path", ""))
                if st.get("decoder") is None or st.get("path") != fp_path:
                    old_dec = st.get("decoder")
                    if old_dec is not None:
                        old_dec.close()
                    st["path"] = fp_path
                    st["decoder"] = self._start_file_decoder(fp_path)
                    st["pos"] = 0
                    st["seek"] = None

            # Convolver lifecycle: kick each convolver's background IR load
            # NOW, on the compile (UI) thread, so the decode + partition FFTs
            # never run on (or block) the audio thread. Path unchanged -> keep
            # the loaded IR (or an in-flight loader) as-is.
            for mid, m in patch.modules.items():
                if m.TYPE != "convolver":
                    continue
                st = self._state.setdefault(mid, self._new_convolver_state())
                cv_path = str(m.params.get("path", ""))
                pend = st.get("pending")
                if cv_path and cv_path != st.get("loaded_path") and (
                    pend is None or pend.get("path") != cv_path
                ):
                    if pend is not None and pend.get("loader") is not None:
                        pend["loader"].close()
                    st["pending"] = {
                        "path": cv_path,
                        "loader": self._start_ir_loader(cv_path, self.block_size),
                    }

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
        # A live recompile may add or remove routed
        # specific_stereo_speaker_output sinks; reconcile the secondary
        # streams so they follow without a Stop/Start (outside the render
        # lock -- a device open must not stall the audio thread).
        if self._running:
            self._sync_device_outputs()

    @staticmethod
    def _topological_sort(patch: Patch) -> list[int]:
        """Kahn's algorithm — sources first, sinks last.

        Cables leaving a DELAYED port (see :meth:`_is_delayed_edge`) are
        ignored for ordering: their value is seeded from the previous
        block's state before anything renders, so they impose no
        within-block ordering — and counting them would poison every
        module downstream of a governor feedback patch into the
        unordered leftover tail below (Kahn never emits a cycle member,
        so its whole chain would fall through in creation order).
        """
        in_degree: dict[int, int] = {mid: 0 for mid in patch.modules}
        for cable in patch.cables:
            if NumpyBackend._is_delayed_edge(patch, cable):
                continue
            in_degree[cable.dst_module_id] = in_degree.get(cable.dst_module_id, 0) + 1
        ready = [mid for mid, deg in in_degree.items() if deg == 0]
        order: list[int] = []
        while ready:
            mid = ready.pop(0)
            order.append(mid)
            for cable in patch.cables_out_of(mid):
                if NumpyBackend._is_delayed_edge(patch, cable):
                    continue
                in_degree[cable.dst_module_id] -= 1
                if in_degree[cable.dst_module_id] == 0:
                    ready.append(cable.dst_module_id)
        for mid in patch.modules:
            if mid not in order:
                order.append(mid)
        return order

    @staticmethod
    def _is_delayed_edge(patch: Patch, cable) -> bool:
        """True for cables carrying a one-block-DELAYED signal — today,
        the buffered sink's ``fill`` cv out, seeded into the buffer
        store from the previous block's ring state before the render
        loop runs. Such a cable is a real signal path but not a
        within-block dependency, which is exactly what lets a governor
        patch (fill -> controller chain -> ratio_cv) close its feedback
        loop while the rest of the graph still sorts deterministically.
        """
        src = patch.modules.get(cable.src_module_id)
        return (
            src is not None
            and src.TYPE == NumpyBackend._BUFFERED_SPECIFIC_SPEAKER
            and cable.src_port == "fill"
        )

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
        # Fresh load stats per run (the toolbar readout greys out while
        # stopped, so a stale figure would never be seen anyway).
        self._dsp_load = 0.0
        self._dsp_load_peak = 0.0
        self._dsp_overloads = 0
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
                self._sync_device_outputs()
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
        self._sync_device_outputs()
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
        # Close any secondary device-output streams opened for routed
        # specific_stereo_speaker_output sinks.
        for _dev_out in list(self._device_outputs.values()):
            _dev_out.close()
        self._device_outputs = {}
        # Governor state is per-run: drop the smoothing memory and stretch
        # engines so the next Start begins at unity ratio with fresh grains.
        self._sink_ratio.clear()
        self._sink_stretch.clear()
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

    def _wanted_streams(self) -> set[tuple[str, int]]:
        """Distinct ``(device, block_size)`` stream keys wanted by the routed
        speaker sinks (specific + buffered). Empty in the common no-routed
        case. See :meth:`_stream_key` for how a sink maps to a key."""
        if self._patch is None:
            return set()
        keys: set[tuple[str, int]] = set()
        for m in self._patch.modules.values():
            key = self._stream_key(m)
            if key is not None:
                keys.add(key)
        return keys

    def _sync_device_outputs(self) -> None:
        """Reconcile the open secondary streams against the routed sinks'
        current selections: open a stream for any newly-wanted ``(device,
        block_size)``, close any no longer used, and leave the rest running.
        This is what makes a device *or buffer-size* change take effect LIVE --
        only the affected stream is rebuilt, no Stop/Start. Called at start()
        (from an empty set, so it opens all) and from set_param / compile
        while running.

        Runs on the GUI thread. A fresh dict is built and swapped into
        ``_device_outputs`` in one assignment; the audio thread only ever reads
        that reference, so it sees the old or the new map whole, never a
        half-updated one (the meters' no-lock discipline). A removed stream is
        closed only AFTER the swap, so the audio thread has stopped iterating
        to it; a late push lands in an unread ring, which is harmless. A stream
        that fails to open is logged and skipped (its sink stays silent) and
        retried on the next change."""
        wanted = self._wanted_streams()
        current = self._device_outputs
        if wanted == set(current):
            return
        new: dict[tuple[str, int], Any] = {}
        for key in wanted:
            existing = current.get(key)
            if existing is not None:
                new[key] = existing            # keep the already-running stream
                continue
            dev, block_size = key
            try:
                dev_out = _DeviceOutput(dev, self.sample_rate, block_size)
                dev_out.open()
                new[key] = dev_out
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    'device-routed speaker: could not open output device %r '
                    'at buffer %d (%s); that sink will be silent.',
                    dev, block_size, e,
                )
        to_close = [d for key, d in current.items() if key not in new]
        self._device_outputs = new             # atomic swap for the audio thread
        for d in to_close:
            d.close()

    # ----- live params -----------------------------------------------------

    def set_param(self, module_id: int, name: str, value: Any) -> None:
        if self._patch is None or module_id not in self._patch.modules:
            return
        module = self._patch.get(module_id)
        module.set_param(name, value)
        # Live switch: when a routed speaker sink's device (or the buffered
        # sink's buffer_size) changes while running, reconcile the secondary
        # streams so only the affected one is rebuilt -- no Stop/Start. Any
        # other param (or module type) never touches the stream set.
        if (
            self._running
            and name in ('device', 'buffer_size')
            and module.TYPE in self._ROUTED_SPEAKERS
        ):
            self._sync_device_outputs()

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

    # Exponential smoothing for the DSP-load readout. Per-block render
    # times are spiky (GC pauses, OS scheduling); 0.9 over 512-sample
    # blocks settles in a few tenths of a second while still tracking
    # patch edits promptly.
    _DSP_LOAD_SMOOTH = 0.9

    def _fill_output(self, outdata: np.ndarray, frames: int) -> None:
        if self._render_disabled:
            outdata.fill(0.0)
            return
        t0 = time.perf_counter()
        try:
            out, device_blocks = self.render_block_multi(frames)
        except BaseException as e:
            # First uncaught exception in render_block: capture a heavy
            # report, write it out, and disable rendering for the rest
            # of this stream. Calling describe_error from inside the
            # audio thread is fine - it never raises, and the cost is
            # paid once (subsequent blocks short-circuit at the
            # _render_disabled check above). Load stats are left alone:
            # a crashed render's timing means nothing.
            self._handle_audio_crash(e)
            outdata.fill(0.0)
            return
        elapsed = time.perf_counter() - t0
        if out is None:
            outdata.fill(0.0)
        else:
            outdata[:] = out
        # Hand each routed sink's block to its stream's ring. Only streams
        # that opened successfully appear in _device_outputs; a block for a
        # failed/absent stream is dropped (that sink is silent).
        if self._device_outputs:
            for _key, _dev_out in self._device_outputs.items():
                _blk = device_blocks.get(_key)
                if _blk is not None:
                    _dev_out.push(_blk)
        # --- DSP-load bookkeeping (audio thread; see the __init__ notes)
        if frames > 0:
            load = elapsed * self.sample_rate / frames
            k = self._DSP_LOAD_SMOOTH
            self._dsp_load = k * self._dsp_load + (1.0 - k) * load
            if load > self._dsp_load_peak:
                self._dsp_load_peak = load
            if load > 1.0:
                self._dsp_overloads += 1

    def dsp_load_snapshot(self) -> tuple[float, float, int]:
        """``(smoothed, peak, overloads)`` DSP load since start().

        Load is render time over the block budget (``frames /
        sample_rate`` seconds): 0.5 means half the budget went to
        rendering, above 1.0 the block missed real time (an audible
        underrun risk); ``overloads`` counts such blocks. GUI-thread
        safe -- plain attribute reads, see the __init__ notes.
        """
        return self._dsp_load, self._dsp_load_peak, self._dsp_overloads

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
            from .._crash import write_crash_report, explicit_write
            # Guard so the global crash observer (if installed) doesn't also
            # write this report -- we write it here with the precise
            # "audio_callback" source tag.
            with explicit_write():
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
    # The stereo sinks are drained separately (pan/width/pan_cv need more
    # than a channel-flag pair; see _drain_stereo_speaker). All three share
    # the drain. The plain stereo speaker always lands on the master bus; the
    # two device-targetable members land there too until a ``device`` is
    # chosen, which pulls them onto a secondary stream (see _stream_key).
    _STEREO_SPEAKERS = frozenset({
        "stereo_speaker_output",
        "specific_stereo_speaker_output",
        "buffered_specific_speaker_output",
    })
    # Device-targetable sinks: a non-empty ``device`` pulls them off the
    # master bus onto their own secondary OutputStream. The buffered variant
    # additionally carries its own ``buffer_size`` (that stream's block size);
    # the plain one runs at the global block size. Secondary streams are
    # therefore keyed by (device, block_size) so the two never collide on a
    # shared device — see _stream_key.
    _SPECIFIC_STEREO_SPEAKER = "specific_stereo_speaker_output"
    _BUFFERED_SPECIFIC_SPEAKER = "buffered_specific_speaker_output"
    _ROUTED_SPEAKERS = frozenset({
        "specific_stereo_speaker_output",
        "buffered_specific_speaker_output",
    })
    # One-pole smoothing applied per block to a governed sink's stretch
    # ratio. 0.2 settles in ~a dozen blocks (~0.15 s at 512/44.1k): slow
    # enough that a twitchy governor patch reads as drift correction
    # rather than vibrato, fast enough to track real clock drift, which
    # moves over seconds.
    _SINK_RATIO_SMOOTH = 0.2
    # Loop gain of the BUILT-IN governor (auto_govern): the ratio target
    # is 1 + gain*(0.5 - fill). 0.5 reproduces the recommended
    # fill -> cv_offset(-0.5) -> cv_scale(-2) -> ratio_cv patch exactly —
    # that chain feeds ratio_cv = 2*(0.5 - fill), and the default
    # ratio_depth 0.25 makes the target 1 + 0.25*ratio_cv = 1 +
    # 0.5*(0.5 - fill) — so 'auto' and the canonical patch behave alike.
    _AUTO_GOVERN_GAIN = 0.5

    def _sink_block_size(self, module) -> int:
        """PortAudio block size for a device-routed sink's own stream.

        The buffered sink carries its own ``buffer_size`` (clamped to a sane
        range against a corrupt patch value); every other routed sink uses the
        global block size.
        """
        if module.TYPE == self._BUFFERED_SPECIFIC_SPEAKER:
            try:
                raw = int(module.params.get("buffer_size", self.block_size))
            except (TypeError, ValueError):
                raw = int(self.block_size)
            return max(_MIN_SINK_BLOCK, min(_MAX_SINK_BLOCK, raw))
        return int(self.block_size)

    def _stream_key(self, module) -> tuple[str, int] | None:
        """The ``(device, block_size)`` key of the secondary stream this sink
        wants, or ``None`` if it stays on the master bus.

        ``None`` when the module is not a device-targetable sink or its
        ``device`` is empty (the AUTO_DEVICE default → master bus). Two sinks
        with the same key share one stream (their audio sums into one bus);
        differing keys — a different device, or the same device at a different
        buffer size — get independent streams.
        """
        if module.TYPE not in self._ROUTED_SPEAKERS:
            return None
        dev = str(module.params.get("device", "")).strip()
        if not dev:
            return None
        return (dev, self._sink_block_size(module))

    def render_block(self, frames: int) -> np.ndarray | None:
        """Master-bus render of one block (the (frames, 2) stereo output).

        Thin wrapper over :meth:`render_block_multi` returning only the
        master bus, preserving the pure-function contract offline tests
        rely on. Speaker-family sinks and any specific_stereo_speaker_output
        left on the default device land here; a sink routed to a named
        device does not (its audio goes to that device's own bus)."""
        return self.render_block_multi(frames)[0]

    def render_block_multi(
        self, frames: int
    ) -> tuple[np.ndarray | None, dict[tuple[str, int], np.ndarray]]:
        """Render one block, returning ``(master, device_blocks)``.

        The graph is walked exactly once. ``master`` is the main stereo bus
        (all speaker sinks plus every stereo speaker on the default device).
        ``device_blocks`` maps each routed sink's ``(device, block_size)`` key
        (see :meth:`_stream_key`) to its own clipped (frames, 2) bus, which the
        live path hands to that stream's secondary OutputStream. No routed sink
        -> empty dict and byte-for-byte the old render."""
        with self._lock:
            patch = self._patch
            order = list(self._topo_order)
            cv_ports = list(self._cv_output_ports)
            # Snapshot the module map too. The GUI thread mutates
            # ``patch.modules`` in place (add/remove a node), and the second
            # render loop below iterates it — without an atomic snapshot a
            # concurrent edit raises "dictionary changed size during iteration"
            # in the audio callback (the RuntimeError seen in the wild). The
            # ``dict(...)`` copy is a single GIL-atomic step, so it is safe even
            # though the writer doesn't hold this lock.
            modules = dict(patch.modules) if patch is not None else {}
        if patch is None:
            return None, {}

        # Port-keyed buffer store. A single module may emit multiple outputs
        # (Keyboard publishes both audio and gate).
        buffers: dict[tuple[int, str], np.ndarray] = {}

        # Governor seed: each buffered sink publishes the PREVIOUS block's
        # ring fill on its 'fill' cv out before anything renders — the
        # one-block-delayed feedback source the topological sort ignores
        # (see _is_delayed_edge), so a fill -> controller -> ratio_cv
        # governor patch reads a defined value wherever it sorted. With no
        # live stream (transport stopped, device empty, failed open) the
        # seed is a neutral 0.5: zero error against the half-full setpoint,
        # so an idle governor patch commands no stretch.
        for module in modules.values():
            if module.TYPE != self._BUFFERED_SPECIFIC_SPEAKER:
                continue
            buffers[(module.id, "fill")] = np.full(
                frames, self._sink_fill(module), dtype=np.float32
            )
        for module_id in order:
            module = modules.get(module_id)
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
        device_blocks: dict[tuple[str, int], np.ndarray] = {}
        governed: dict[tuple[str, int], tuple[int, float]] = {}
        for module in modules.values():
            if module.TYPE in self._STEREO_SPEAKERS:
                target = out
                key = self._stream_key(module)
                if key is not None:
                    target = device_blocks.get(key)
                    if target is None:
                        target = np.zeros((frames, 2), dtype=np.float32)
                        device_blocks[key] = target
                self._drain_stereo_speaker(module, frames, buffers, patch, target)
                if (
                    key is not None
                    and module.TYPE == self._BUFFERED_SPECIFIC_SPEAKER
                ):
                    ratio = self._governed_ratio(module, patch, buffers)
                    if ratio is not None:
                        governed[key] = (module.id, ratio)
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
        for blk in device_blocks.values():
            np.clip(blk, -1.0, 1.0, out=blk)
        # Governor actuation: PITCH-PRESERVING time-stretch of each
        # governed stream's block to frames * ratio before it is handed
        # to the ring. Two stages whose pitch effects cancel exactly:
        # a streaming WSOLA shift UP by `ratio` (_GrainShifter — the
        # pitch shifter's engine, one per channel, state persistent
        # across blocks), then the Slice-1 linear resample DOWN by the
        # same ratio to set the pushed length. Net: unity pitch, length
        # frames * ratio — the ring counts in samples with push size
        # free to differ from pop size, so the variable-length push is
        # already legal. While ratio_cv is CABLED the engines stay
        # in-circuit even at ratio 1.0 — bypassing at unity would spend
        # the ~one-grain warm-up (zeros) at the exact moment the
        # governor first corrects, mid-performance; this way it is paid
        # once at patch/Start, and the wet path keeps one constant
        # ~grain (50 ms) latency. Uncabled sinks never reach here (see
        # _governed_ratio), keeping the unpatched push bit-identical.
        if governed and frames > 0:
            src_pos = np.arange(frames, dtype=np.float64)
            for key, (mid, ratio) in governed.items():
                blk = device_blocks.get(key)
                if blk is None:
                    continue
                engines = self._sink_stretch.get(mid)
                if engines is None:
                    # The pitch shifter's house defaults: 50 ms grain,
                    # overlap 2, and headroom for any block size the
                    # main stream can run.
                    grain = max(8, int(round(0.05 * self.sample_rate)))
                    head = max(16384, 16 * int(getattr(self, "block_size", 512)))
                    engines = [
                        _GrainShifter(grain, 2, head),
                        _GrainShifter(grain, 2, head),
                    ]
                    self._sink_stretch[mid] = engines
                shifted = np.empty((frames, 2), dtype=np.float64)
                shifted[:, 0] = engines[0].process(
                    blk[:, 0].astype(np.float64), ratio
                )
                shifted[:, 1] = engines[1].process(
                    blk[:, 1].astype(np.float64), ratio
                )
                m = max(1, int(round(frames * ratio)))
                if m == frames:
                    # Sub-0.5-sample rounding: skip the resample (the
                    # residual pitch offset is a few cents at worst).
                    stretched = shifted.astype(np.float32)
                else:
                    dst_pos = np.arange(m, dtype=np.float64) * (frames / m)
                    stretched = np.empty((m, 2), dtype=np.float32)
                    stretched[:, 0] = np.interp(dst_pos, src_pos, shifted[:, 0])
                    stretched[:, 1] = np.interp(dst_pos, src_pos, shifted[:, 1])
                # The OLA reconstruction can overshoot the pre-clipped
                # bus by a hair; keep the push inside the rails.
                np.clip(stretched, -1.0, 1.0, out=stretched)
                device_blocks[key] = stretched
        return out, device_blocks

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

    def _sink_fill(self, module) -> float:
        """This buffered sink's current ring fill fraction (0..1), or a
        neutral 0.5 when it has no live stream (device empty, transport
        stopped, or a failed open). Read from the same telemetry the GUI
        readout uses, so it is effectively one block old — exactly the
        delay the governor is built around. Shared by the ``fill`` cv-out
        seeding and the built-in ``auto_govern`` controller so the two can
        never disagree about how full the ring is."""
        key = self._stream_key(module)
        if key is not None:
            dev_out = self._device_outputs.get(key)
            if dev_out is not None:
                queued, capacity, _u, _d = dev_out.telemetry()
                if capacity > 0:
                    return queued / capacity
        return 0.5

    def _governed_ratio(self, module, patch, buffers) -> float | None:
        """The smoothed varispeed ratio the ring governor commands, or
        ``None`` when the sink is ungoverned (the bit-exact pre-governor
        path; smoothing + stretch state are dropped so a re-patch starts
        from unity). Two ways to be governed, patch first:

        * ``ratio_cv`` CABLED — the patch drives it: the cv buffer
          collapses to its mean and maps through ``1 + cv * ratio_depth``.
        * else ``auto_govern`` ON — the built-in controller drives it from
          the sink's own ring fill: ``1 + gain*(0.5 - fill)`` with
          :data:`_AUTO_GOVERN_GAIN`, i.e. the canonical patch, no cables.

        Either target clamps to a safe 0.5..2 and one-pole smooths
        (:data:`_SINK_RATIO_SMOOTH`) so a twitchy loop reads as drift
        correction, not vibrato. Sinks sharing one (device, buffer_size)
        stream: the last drained sink's ratio wins — a governor should own
        its stream.
        """
        cabled = any(
            c.dst_port == "ratio_cv" for c in patch.cables_into(module.id)
        )
        if cabled:
            cv = self._input_buffer(
                patch, buffers, module.id, "ratio_cv", collapse=False
            )
            level = float(np.mean(cv)) if cv is not None and cv.size else 0.0
            try:
                depth = float(module.params.get("ratio_depth", 0.25))
            except (TypeError, ValueError):
                depth = 0.25
            target = 1.0 + level * depth
        elif module.params.get("auto_govern"):
            # Low ring (fill < 0.5) -> positive error -> ratio > 1 -> push
            # MORE samples to refill; high ring drains it. Same sign the
            # patch recipe inverts to by hand.
            target = 1.0 + self._AUTO_GOVERN_GAIN * (0.5 - self._sink_fill(module))
        else:
            self._sink_ratio.pop(module.id, None)
            self._sink_stretch.pop(module.id, None)
            return None
        target = min(max(target, 0.5), 2.0)
        prev = self._sink_ratio.get(module.id, 1.0)
        ratio = prev + self._SINK_RATIO_SMOOTH * (target - prev)
        self._sink_ratio[module.id] = ratio
        return ratio

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

    def reset_meter_clips(self, module_id: int) -> None:
        """GUI hook: zero one Meter's clip counters (both channels).

        Takes the backend lock so it can't interleave with a render
        mid-block; worst case a same-block clip event lands after the
        reset and the counter shows it -- which is the truth anyway.
        """
        with self._lock:
            st = self._state.get(module_id)
            if st:
                for suffix in ("_l", "_r"):
                    st["clips" + suffix] = 0
                    st["over_tail" + suffix] = False

    def snapshot_file_positions(self) -> dict[int, tuple[float, float]]:
        """GUI hook: each ``file_player``'s playhead as ``(elapsed, total)``
        seconds, keyed by module id.

        While a file is still decoding, ``total`` is the *buffered* length
        so far — the readout's right-hand number grows as ffmpeg works
        through a long file, which doubles as a free loading indicator —
        and becomes the true duration once the decode finishes. ``0.0``
        for an empty/unreadable path; ``elapsed`` is clamped to ``total``
        once a one-shot has run off the end. The lock is taken only to
        copy the state mapping so a concurrent ``compile`` can't resize it
        mid-iteration -- ``pos`` itself is written by the audio thread
        without the lock, but an int read is atomic under the GIL and a
        marginally stale playhead is harmless for a readout.
        """
        with self._lock:
            items = list(self._state.items())
            types = dict(self._state_types)
        sr = float(self.sample_rate)
        out: dict[int, tuple[float, float]] = {}
        for mid, st in items:
            if types.get(mid) != "file_player":
                continue
            dec = st.get("decoder")
            if dec is None or dec.failed:
                out[mid] = (0.0, 0.0)
                continue
            n = int(dec.total_frames) if dec.done else int(dec.frames_ready)
            if n == 0:
                out[mid] = (0.0, 0.0)
                continue
            elapsed = min(int(st.get("pos", 0)), n) / sr
            out[mid] = (elapsed, n / sr)
        return out

    def snapshot_sink_buffers(self) -> dict[int, tuple[int, int, int, int]]:
        """GUI hook: hand-off-ring telemetry for each device-routed speaker
        sink, keyed by module id.

        Each value is that sink's secondary stream's ``(queued, capacity,
        underruns, drops)`` (see :meth:`_DeviceOutput.telemetry`). A sink
        with no live stream — transport stopped, ``device`` empty (master
        bus), or the open failed — simply has no entry, which the UI shows
        as idle. Sinks sharing one ``(device, block_size)`` stream report
        the *same* tuple, sampled once, so their readouts always agree.

        GUI-thread safe: ``_device_outputs`` is read as one reference (the
        atomic-swap discipline in :meth:`_sync_device_outputs`), patch
        edits happen on this same thread, and telemetry() takes each ring's
        lock only for four int reads.
        """
        outs = self._device_outputs
        if not outs or self._patch is None:
            return {}
        result: dict[int, tuple[int, int, int, int]] = {}
        per_key: dict[tuple[str, int], tuple[int, int, int, int]] = {}
        for m in self._patch.modules.values():
            key = self._stream_key(m)
            if key is None:
                continue
            if key not in per_key:
                dev_out = outs.get(key)
                if dev_out is None:
                    continue                    # stream failed to open
                per_key[key] = dev_out.telemetry()
            result[m.id] = per_key[key]
        return result

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
        if module.TYPE == "key_trigger":
            return self._render_key_trigger(module, frames)
        if module.TYPE == "clock":
            return self._render_clock(module, frames)
        if module.TYPE in ("sequencer", "fader_seq"):
            # fader_seq is the Sequencer with a different front panel —
            # identical param contract, one engine (see modules/fader_seq.py).
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
        if module.TYPE == "fm_op":
            return self._render_fm_op(module, frames, buffers, patch)
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
        if module.TYPE == "vocoder":
            return self._render_vocoder(module, frames, buffers, patch)
        if module.TYPE == "delay":
            return self._render_delay(module, frames, buffers, patch)
        if module.TYPE == "reverb":
            return self._render_reverb(module, frames, buffers, patch)
        if module.TYPE == "loudness":
            return self._render_loudness(module, frames, buffers, patch)
        if module.TYPE == "compressor":
            return self._render_compressor(module, frames, buffers, patch)
        if module.TYPE == "limiter":
            return self._render_limiter(module, frames, buffers, patch)
        if module.TYPE == "noise_gate":
            return self._render_noise_gate(module, frames, buffers, patch)
        if module.TYPE == "transient_shaper":
            return self._render_transient_shaper(module, frames, buffers, patch)
        if module.TYPE == "distortion":
            return self._render_distortion(module, frames, buffers, patch)
        if module.TYPE == "ring_mod":
            return self._render_ring_mod(module, frames, buffers, patch)
        if module.TYPE == "freq_shifter":
            return self._render_freq_shifter(module, frames, buffers, patch)
        if module.TYPE == "bitcrusher":
            return self._render_bitcrusher(module, frames, buffers, patch)
        if module.TYPE == "waveshaper":
            return self._render_waveshaper(module, frames, buffers, patch)
        if module.TYPE == "tape":
            return self._render_tape(module, frames, buffers, patch)
        if module.TYPE == "convolver":
            return self._render_convolver(module, frames, buffers, patch)
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
            or module.TYPE in self._STEREO_SPEAKERS
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

    # Block-path tuning for the envelope follower's vectorized solve.
    #
    # _ATC_COEF_MAX: a smoothing coefficient above this (time constant
    #   under ~0.1 samples, i.e. attack/release below ~0.003 ms — also
    #   the ms<=0 "instant" clamp at exactly 1.0) makes the cumprod
    #   solve numerically degenerate, so those settings take the
    #   per-sample loop instead.
    # _ATC_CHUNK / _ATC_MIN_TAIL_P: the fixed-pattern solve divides by a
    #   running cumprod, which decays monotonically; the whole-block
    #   single shot is used while the block's final cumprod stays above
    #   _ATC_MIN_TAIL_P, otherwise the solve runs in _ATC_CHUNK-sample
    #   chunks, which bounds the decay to (1e-4)**64 = 1e-256 per chunk
    #   — clear of float64 underflow with headroom.
    # _ATC_MAX_ITER: pattern fixed-point iterations before conceding to
    #   the loop. The 2026-07-03 spike measured mean ~4 / p95 12 across
    #   sines, noise, AM, bursts, and DC at musical and extreme
    #   coefficient pairs; the observed max was 18, on the pathological
    #   corner of a 0.01 ms attack following a 110 Hz sine. The cap is
    #   correctness insurance, not a tuning knob — capping out just
    #   means the loop renders that block.
    _ATC_COEF_MAX = 1.0 - 1e-4
    _ATC_CHUNK = 64
    _ATC_MIN_TAIL_P = 1e-250
    _ATC_MAX_ITER = 24

    def _render_audio_to_cv(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Envelope follower: rectify input + asymmetric one-pole smoothing.

        Coefficients are derived from time constants:

            coef = 1 - exp(-1 / (time_seconds * sample_rate))

        A target rising above the current level uses ``attack_coef``;
        a target below uses ``release_coef``. Zero or negative time
        constants are clamped to "instant" (coef = 1.0).

        The smoother's state feeds back into the next sample, and which
        coefficient applies depends on comparing the input against that
        evolving state — so unlike the biquads this is not expressible
        as one ``lfilter`` call. Both shape branches instead run the
        vectorized fixed-point solve in :meth:`_audio_to_cv_block`
        (details there), with the original per-sample loop kept as the
        fallback for degenerate coefficients.

        Voice-aware. Branches on the audio input's ``ndim``:

          * 1D ``(F,)`` audio -> scalar smoother state, output ``(F,)``.
          * 2D ``(V, F)`` audio -> per-voice smoother state stored as a
            length-V vector, output ``(V, F)``.

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
        """Scalar follower state, single smoother. Output ``(F,)``.

        Runs the shared block solve on a one-row view; falls back to
        the per-sample loop when the solve declines (degenerate
        coefficients, non-finite input, or a hypothetical pattern
        non-convergence). Equivalence with the old loop is pinned by
        ``TestAudioToCVBlockEquivalence`` against a verbatim oracle.
        """
        state = self._state.setdefault(module.id, {"level": 0.0})
        # Discard voice-branch state if we previously rendered (V, F).
        if "level_arr" in state:
            state.clear()
            state["level"] = 0.0

        level = float(state["level"])
        abs_in = np.abs(audio_in).astype(np.float64)
        y = self._audio_to_cv_block(
            abs_in[None, :],
            np.array([level], dtype=np.float64),
            attack_coef,
            release_coef,
        )
        if y is None:
            out64, level = self._audio_to_cv_loop_mono(
                abs_in, level, attack_coef, release_coef
            )
        else:
            out64 = y[0]
            level = float(out64[-1]) if frames else level
        state["level"] = float(level)
        out = out64.astype(np.float32)
        return (out * gain).astype(np.float32)

    def _render_audio_to_cv_voice(
        self, module, frames, audio_in, attack_coef, release_coef, gain
    ):
        """Per-voice follower state. Output ``(V, F)``.

        ``audio_in`` is ``(V, F)``. The shared block solve handles all
        voices at once (independent rows, one pattern array); the old
        sample-loop-with-voice-vectorized-steps survives only as the
        fallback for degenerate coefficients.
        """
        V = audio_in.shape[0]
        state = self._state.setdefault(module.id, {})

        needs_reinit = (
            "level_arr" not in state or state["level_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["level_arr"] = np.zeros(V, dtype=np.float64)

        level = state["level_arr"]  # (V,)
        abs_in = np.abs(audio_in).astype(np.float64)
        y = self._audio_to_cv_block(abs_in, level, attack_coef, release_coef)
        if y is None:
            out64, level = self._audio_to_cv_loop_voice(
                abs_in, level, attack_coef, release_coef
            )
        else:
            out64 = y
            if frames:
                level = y[:, -1].copy()
        state["level_arr"] = level
        out = out64.astype(np.float32)
        return (out * gain).astype(np.float32)

    def _audio_to_cv_block(self, t, level0, attack_coef, release_coef):
        """Vectorized asymmetric one-pole via monotone pattern iteration.

        ``t`` is the rectified input ``(V, F)`` float64 (mono passes a
        one-row view), ``level0`` the carried per-row state ``(V,)``.
        Returns the float64 trajectory ``(V, F)``, or ``None`` to tell
        the caller to take the per-sample loop instead.

        Why this shape: the recurrence

            level[n] = level[n-1] + c[n] * (t[n] - level[n-1]),
            c[n] = attack if t[n] > level[n-1] else release

        picks its coefficient by comparing against its own evolving
        state, so no single fixed filter computes it. But each step is
        ``max(combo_A, combo_R)(level[n-1])`` when attack >= release
        (``min`` when attack < release), because the two convex combos
        differ by ``(A - R) * (t - level)``. Two consequences, both in
        exact arithmetic:

          * solving ANY fixed coefficient pattern as a linear
            time-varying one-pole brackets the true trajectory from
            below (above for A < R);
          * re-deriving the pattern from a solved trajectory and
            solving again moves monotonically toward the true
            trajectory, and a self-consistent pattern IS the true
            solution, exactly.

        So: guess a pattern (attack wherever the rectified input rises
        above its predecessor), solve, re-derive, repeat until the
        pattern stops changing — typically 2-6 iterations, each a
        handful of whole-array numpy ops. One extra stop condition:
        where the trajectory plateaus (DC, a saturated burst), the
        solved level can alternate by one float64 ulp between
        iterations, flipping razor-tie comparisons forever without the
        values moving — so two consecutive trajectories that are equal
        after the float32 cast also count as converged (the output is
        identical either way, and the carried float64 state differs by
        ulps at most). The fixed-pattern solve

            y[n] = a[n] * y[n-1] + b[n],  a = 1 - c,  b = c * t

        vectorizes as ``y = P * (l0 + cumsum(b / P))`` with
        ``P = cumprod(a)``. Every term is nonnegative (rectified
        input, level in [0, max]), so there is no cancellation; the
        only hazard is ``P`` underflowing, which the chunked variant
        in :meth:`_audio_to_cv_solve` bounds and the ``_ATC_COEF_MAX``
        guard cuts off entirely.

        Float caveat (same class as the ADSR voice rewrite): the solve
        reassociates the arithmetic, so trajectories can differ from
        the loop's by float64 round-off — orders of magnitude below
        the float32 resolution that leaves the renderer. The
        equivalence tests pin this at < 1e-6 after the cast; the
        2026-07-03 spike observed max diff 0.0 (bit-identical after
        the cast) across the whole signal x coefficient grid. In-repo
        renderer timing (sandbox, F=512, 1 kHz sine): mono ~102 ->
        ~86 us/block (~1.2x — renderer overhead dominates mono);
        16-voice ~1.27 -> ~0.33 ms/block (~3.9x, 10.9% -> 2.8% of the
        11.6 ms block budget — the voice loop was the actual target).

        Declines (returns ``None``) when: either coefficient exceeds
        ``_ATC_COEF_MAX`` (includes the ms<=0 instant clamp at 1.0,
        where ``a = 0`` breaks the cumprod algebra), the input is not
        finite (the loop is the defined NaN semantics), the pattern
        has not settled after ``_ATC_MAX_ITER`` rounds, or the result
        is non-finite (belt and braces).
        """
        if (
            attack_coef > self._ATC_COEF_MAX
            or release_coef > self._ATC_COEF_MAX
            or not np.isfinite(t).all()
        ):
            return None
        l0 = level0[:, None]
        if attack_coef == release_coef:
            # Plain time-invariant one-pole: one solve, no iteration.
            y = self._audio_to_cv_solve(
                np.full_like(t, 1.0 - attack_coef), attack_coef * t, l0
            )
            return y if np.isfinite(y).all() else None

        prev = np.empty_like(t)
        prev[:, 0:1] = l0
        prev[:, 1:] = t[:, :-1]
        pattern = np.where(t > prev, attack_coef, release_coef)
        y_prev32 = None
        for _ in range(self._ATC_MAX_ITER):
            y = self._audio_to_cv_solve(1.0 - pattern, pattern * t, l0)
            prev[:, 1:] = y[:, :-1]
            new_pattern = np.where(t > prev, attack_coef, release_coef)
            y32 = y.astype(np.float32)
            if np.array_equal(new_pattern, pattern) or (
                y_prev32 is not None and np.array_equal(y32, y_prev32)
            ):
                return y if np.isfinite(y).all() else None
            pattern = new_pattern
            y_prev32 = y32
        return None

    def _audio_to_cv_solve(self, a, b, l0):
        """Exact linear time-varying one-pole ``y[n] = a[n]*y[n-1] + b[n]``.

        Whole-block cumprod/cumsum when the running product stays above
        ``_ATC_MIN_TAIL_P`` (always true for musical time constants:
        even a 0.02 ms attack keeps the 512-sample tail around 1e-100);
        otherwise the same algebra chunk by chunk, carrying the level
        across chunk seams. ``a`` and ``b`` are ``(V, F)``; ``l0`` is
        ``(V, 1)``.
        """
        P = np.cumprod(a, axis=-1)
        if P.size == 0 or float(P[..., -1].min()) > self._ATC_MIN_TAIL_P:
            return P * (l0 + np.cumsum(b / P, axis=-1))
        F = a.shape[-1]
        out = np.empty_like(a)
        cur = l0
        pos = 0
        while pos < F:
            end = min(pos + self._ATC_CHUNK, F)
            Pc = np.cumprod(a[..., pos:end], axis=-1)
            out[..., pos:end] = Pc * (
                cur + np.cumsum(b[..., pos:end] / Pc, axis=-1)
            )
            cur = out[..., end - 1 : end]
            pos = end
        return out

    def _audio_to_cv_loop_mono(self, abs_in, level, attack_coef, release_coef):
        """Per-sample reference loop (pre-vectorization semantics).

        Fallback for the degenerate corners the block solve declines.
        Returns ``(trajectory_f64, final_level)``.
        """
        frames = abs_in.shape[0]
        out = np.empty(frames, dtype=np.float64)
        for n in range(frames):
            target = float(abs_in[n])
            coef = attack_coef if target > level else release_coef
            level += coef * (target - level)
            out[n] = level
        return out, level

    def _audio_to_cv_loop_voice(self, abs_in, level, attack_coef, release_coef):
        """Per-sample voice loop (pre-vectorization semantics).

        Vectorized across voices per sample, serial in time — the shape
        the voice branch always had. Fallback only.
        Returns ``(trajectory_f64, final_level_arr)``.
        """
        V, frames = abs_in.shape
        out = np.empty((V, frames), dtype=np.float64)
        for n in range(frames):
            target = abs_in[:, n]  # (V,)
            coef = np.where(target > level, attack_coef, release_coef)
            level = level + coef * (target - level)
            out[:, n] = level
        return out, level

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
        """Mono fast path -- per-stage ``scipy.signal.lfilter``, 1D out.

        Filter vectorization slice 5: the per-sample Python cascade is
        gone; each of the four biquad stages (LP1, LP2, HP1, HP2) is
        one lfilter call running its time recurrence in C (~7x on the
        2026-07-03 spike). Same state design as ``_render_filter_mono``
        (slice 3): persisted state stays the raw DF-I history
        ``(x1, x2, y1, y2)`` per stage -- coefficient-independent, so a
        block-mean freq_cv that changes the coefficients between blocks
        behaves exactly as the old loop -- converted to the equivalent
        transposed-DF-II ``zi`` at block start (the lfiltic identity,
        inlined) and read back off the stage input/output buffer tails
        after.

        Why per-stage lfilter and not one ``sosfilt`` over the 2-section
        cascade: sosfilt returns only the final output, but the
        intermediate stage-1 signal's tails ARE the coefficient-
        independent DF-I history for stage 1's outputs and stage 2's
        inputs. Recovering them algebraically from sosfilt's ``zf``
        divides by a2 and costs bit-exactness; running the stages
        separately reads them straight off the buffers. The
        intermediates stay float64 between stages, exactly like the
        old loop.

        Equivalence vs the old loop (``TestCrossoverLfilterEquivalence``):
        bit-identical after the float32 cast on noise across static and
        per-block-swept freqs; on pure sines the high branch shows
        <= ~5e-13 absolute drift confined to samples below ~-130 dBFS
        (float64 reassociation between DF-I and transposed DF-II -- the
        ADSR-rewrite drift class; tests pin < 1e-6).
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

        if frames == 0:
            empty = np.empty(0, dtype=np.float32)
            return {"low": empty, "high": empty.copy()}

        lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n = (
            self._crossover_coeffs(freq)
        )
        a = np.array([1.0, a1n, a2n])
        lp_b = np.array([lp_b0, lp_b1, lp_b2])
        hp_b = np.array([hp_b0, hp_b1, hp_b2])
        x = src.astype(np.float64)

        def zi(b, stg):
            # lfiltic identity: DF-I history -> transposed-DF-II zi.
            return np.array(
                [
                    b[1] * state[stg + "_x1"] + b[2] * state[stg + "_x2"]
                    - a1n * state[stg + "_y1"] - a2n * state[stg + "_y2"],
                    b[2] * state[stg + "_x1"] - a2n * state[stg + "_y1"],
                ],
                dtype=np.float64,
            )

        def carry(stg, xin, yout):
            # Read the DF-I history back off the buffer tails; a
            # 1-frame block shifts the carried x1/y1 into x2/y2.
            state[stg + "_x2"] = (
                float(xin[-2]) if frames >= 2 else state[stg + "_x1"]
            )
            state[stg + "_x1"] = float(xin[-1])
            state[stg + "_y2"] = (
                float(yout[-2]) if frames >= 2 else state[stg + "_y1"]
            )
            state[stg + "_y1"] = float(yout[-1])

        lp1, _ = lfilter(lp_b, a, x, zi=zi(lp_b, "lp1"))
        lp2, _ = lfilter(lp_b, a, lp1, zi=zi(lp_b, "lp2"))
        hp1, _ = lfilter(hp_b, a, x, zi=zi(hp_b, "hp1"))
        hp2, _ = lfilter(hp_b, a, hp1, zi=zi(hp_b, "hp2"))
        carry("lp1", x, lp1)
        carry("lp2", lp1, lp2)
        carry("hp1", x, hp1)
        carry("hp2", hp1, hp2)

        return {
            "low": lp2.astype(np.float32),
            "high": hp2.astype(np.float32),
        }

    def _render_crossover_voice(self, module, frames, src, freq):
        """Voice-aware path -- per-stage lfilter over (V, F), (V, F) out.

        Filter vectorization slice 5, voice shape (~34x on the
        2026-07-03 spike; the old per-sample voice cascade was the
        single most expensive render path at 61% of the 11.6 ms block
        budget). Coefficients are scalar by design (block-mean freq,
        see ``_render_crossover``), so one lfilter call per stage
        filters all V rows along the time axis with ``zi`` of shape
        (V, 2) -- the same shared-coefficient shape as filter slice 4.

        State design and the per-stage-vs-sosfilt rationale are the
        mono path's (see ``_render_crossover_mono``), vectorized: the
        persisted DF-I history is one (V,) float64 array per stage
        field, the zi conversion and tail read-back are the same two
        lfiltic-identity expressions broadcast across V.
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

        if frames == 0:
            empty = np.empty((V, 0), dtype=np.float32)
            return {"low": empty, "high": empty.copy()}

        lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n = (
            self._crossover_coeffs(freq)
        )
        a = np.array([1.0, a1n, a2n])
        lp_b = np.array([lp_b0, lp_b1, lp_b2])
        hp_b = np.array([hp_b0, hp_b1, hp_b2])
        x = src.astype(np.float64)

        def zi(b, stg):
            # lfiltic identity broadcast across V -> (V, 2).
            return np.stack(
                [
                    b[1] * state[stg + "_x1_arr"] + b[2] * state[stg + "_x2_arr"]
                    - a1n * state[stg + "_y1_arr"] - a2n * state[stg + "_y2_arr"],
                    b[2] * state[stg + "_x1_arr"] - a2n * state[stg + "_y1_arr"],
                ],
                axis=-1,
            )

        def carry(stg, xin, yout):
            # .copy() so the carried (V,) tails don't pin the whole
            # (V, F) block buffers alive as views.
            state[stg + "_x2_arr"] = (
                xin[:, -2].copy() if frames >= 2 else state[stg + "_x1_arr"]
            )
            state[stg + "_x1_arr"] = xin[:, -1].copy()
            state[stg + "_y2_arr"] = (
                yout[:, -2].copy() if frames >= 2 else state[stg + "_y1_arr"]
            )
            state[stg + "_y1_arr"] = yout[:, -1].copy()

        lp1, _ = lfilter(lp_b, a, x, axis=-1, zi=zi(lp_b, "lp1"))
        lp2, _ = lfilter(lp_b, a, lp1, axis=-1, zi=zi(lp_b, "lp2"))
        hp1, _ = lfilter(hp_b, a, x, axis=-1, zi=zi(hp_b, "hp1"))
        hp2, _ = lfilter(hp_b, a, hp1, axis=-1, zi=zi(hp_b, "hp2"))
        carry("lp1", x, lp1)
        carry("lp2", lp1, lp2)
        carry("hp1", x, hp1)
        carry("hp2", hp1, hp2)

        return {
            "low": lp2.astype(np.float32),
            "high": hp2.astype(np.float32),
        }


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
                                   gains_override=None, qs_override=None):
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
        if qs_override is not None:
            qs = qs_override  # MotionEQ: per-band CV-squeezed Qs
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
                                    gains_override=None, qs_override=None):
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
        if qs_override is not None:
            qs = qs_override  # MotionEQ: per-band CV-squeezed Qs
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
        clamped +/-24), and the Q is
        ``band{i}_q * 2 ** (q_cv_depth * mean(band{i}_q_cv))`` (riding
        the cascade's 0.1..20 clip), all block-meaned (one coefficient
        set per block, shared across voices -- the Crossover's
        macro-sweep policy). An unpatched CV leaves that band at its
        static value, so with nothing patched MotionEQ is bit-identical
        to a ParametricEQ with the same params.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        cv_depth = float(module.params.get("cv_depth", 1.0))
        gain_cv_depth = float(module.params.get("gain_cv_depth", 6.0))
        q_cv_depth = float(module.params.get("q_cv_depth", 1.0))
        base_freqs, base_gains, base_qs = self._peq_band_params(module)
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

        # Per-band Q CV: multiplicative like the freq sweep -- Q is a
        # ratio-like quantity, so the natural unit is a doubling
        # (q * 2**(q_cv_depth * mean cv)), block-meaned. No clamp here:
        # _peq_coeffs already clips Q to (0.1, 20), the same rail the
        # static param rides. Unpatched = exact static Q.
        mod_qs = []
        for i, base in enumerate(base_qs, start=1):
            cv = self._input_buffer(
                patch, buffers, module.id, f"band{i}_q_cv"
            )
            if cv is not None and cv.size > 0:
                base = base * float(2.0 ** (q_cv_depth * float(np.mean(cv))))
            mod_qs.append(base)

        if src.ndim == 2:
            return self._render_parametric_eq_voice(
                module, frames, src,
                freqs_override=mod_freqs, gains_override=mod_gains,
                qs_override=mod_qs,
            )
        return self._render_parametric_eq_mono(
            module, frames, src,
            freqs_override=mod_freqs, gains_override=mod_gains,
            qs_override=mod_qs,
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

    # ----- Compressor rendering -------------------------------------------

    # RMS detector averaging window (ms): ~10 ms hears a few cycles of a
    # bass note as one loudness without smearing transients across the beat.
    _COMP_RMS_MS = 10.0
    # dB-conversion floor so a silent block never hits log10(0). -180 dBFS
    # sits far below any musical signal.
    _COMP_LEVEL_FLOOR = 1e-9

    @staticmethod
    def _compressor_reduction_db(level_db, threshold, ratio, knee):
        """Static gain computer: input level (dB) -> gain reduction (dB >= 0).

        The standard soft-knee compressor law, returning the *positive*
        attenuation to apply (0 = no reduction). With slope
        ``s = 1 - 1/ratio`` (0 at ratio 1, -> 1 as ratio -> inf), knee
        width ``W`` dB centred on the threshold, and ``over = level - T``:

          * ``2*over < -W``   -> below the knee   -> 0
          * ``2*|over| <= W``  -> inside the knee  -> ``s*(over + W/2)**2/(2W)``
          * else              -> above the knee   -> ``s*over``

        The pieces meet with matching value and slope at the knee edges
        (C1-continuous); ``W = 0`` collapses to the hard-knee hinge.
        Vectorized over ``level_db`` (any shape), so a whole ``(V, F)``
        block resolves in one pass with no Python loop.
        """
        over = level_db - threshold
        slope = 1.0 - 1.0 / ratio
        W = float(knee)
        if W > 0.0:
            return np.select(
                [2.0 * over < -W, 2.0 * over <= W],
                [np.zeros_like(over), slope * (over + 0.5 * W) ** 2 / (2.0 * W)],
                default=slope * over,
            )
        return np.where(over > 0.0, slope * over, 0.0)

    def _render_compressor(self, module, frames: int, buffers, patch):
        """Feed-forward compressor with external sidechain (see modules/compressor.py).

        Shape-polymorphic like the other effects. Branches on the ``in``
        audio's ndim: 1D ``(F,)`` -> single detector + gain smoother,
        ``(F,)`` out; 2D ``(V, F)`` -> per-voice state, ``(V, F)`` out. The
        mono path is the ``V == 1`` case of the same core, so a single voice
        row is bit-identical to mono.

        Emits two outputs: ``out`` (the gain-applied, optionally parallel-
        mixed audio) and ``gr`` (applied gain reduction as a 0..-1 CV,
        ``applied_gain - 1``).
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "gr": z.copy()}

        ratio = float(module.params.get("ratio", 2.0))
        makeup_db = float(module.params.get("gain", 0.0))
        mix = float(module.params.get("mix", 1.0))

        # Neutral short-circuit: ratio 1 (no reduction) + no make-up + fully
        # wet -> the signal is untouched, so skip the detector entirely and
        # hand back a bit-exact copy of the input (gr = 0).
        if ratio == 1.0 and makeup_db == 0.0 and mix == 1.0:
            out = src.astype(np.float32, copy=True)
            return {"out": out, "gr": np.zeros_like(out)}

        # Sidechain key: normalled to ``in`` when unpatched (ordinary
        # feed-forward); an external cable overrides it.
        key = self._input_buffer(
            patch, buffers, module.id, "sidechain", collapse=False
        )

        # threshold_cv: block-meaned dB offset (one value per block), like the
        # rack's other dB-domain CV macros.
        threshold = float(module.params.get("threshold", -18.0))
        tcv = self._input_buffer(patch, buffers, module.id, "threshold_cv")
        if tcv is not None and tcv.size:
            depth = float(module.params.get("threshold_cv_depth", 12.0))
            threshold += depth * float(np.mean(tcv))

        if src.ndim == 2:
            V = src.shape[0]
            key = self._compressor_align_key(key, src, V)
            return self._render_compressor_core(module, frames, src, key, threshold)

        # Mono. A 2D key collapses to the summed mix (you key off the whole
        # signal, not one voice); an absent key normals to ``in``.
        if key is None:
            key = src
        elif key.ndim == 2:
            key = key.sum(axis=0)
        res = self._render_compressor_core(
            module, frames, src[np.newaxis, :], key[np.newaxis, :], threshold
        )
        return {"out": res["out"][0], "gr": res["gr"][0]}

    @staticmethod
    def _compressor_align_key(key, src, V):
        """Broadcast the optional sidechain key onto the ``in`` voice shape."""
        if key is None:
            return src  # normalled: each voice keys off its own signal
        if key.ndim == 1:
            return np.broadcast_to(key, (V, key.shape[0]))
        if key.shape[0] == V:
            return key
        if key.shape[0] == 1:
            return np.broadcast_to(key, (V, key.shape[1]))
        return np.broadcast_to(key.mean(axis=0), (V, key.shape[1]))

    def _render_compressor_core(self, module, frames, src, key, threshold):
        """Shared ``(V, F)`` feed-forward compressor engine.

        Detector on ``key`` -> level in dB -> gain computer (log-domain soft
        knee) -> attack/release smoothing of the *gain reduction* -> linear
        multiply of ``src`` + make-up + parallel mix. Zero latency, so
        ``mix`` needs no delay compensation.

        The gain smoothing reuses the envelope follower's vectorized
        asymmetric one-pole: the reduction (in dB, >= 0) rises when
        compression deepens -> attack, and falls when it eases -> release,
        which is exactly the "attack where the target rises above the
        state" recurrence :meth:`_audio_to_cv_block` solves as a monotone
        fixed point. Reduction is non-negative, so the solve's no-
        cancellation precondition holds. Both the RMS detector one-pole
        (via ``lfilter`` + carried ``zi``) and the gain smoother carry
        per-voice state, so the render is block-size independent (bit-exact
        for the detector; to float64 round-off for the reassociated gain
        solve, like the follower).
        """
        V = src.shape[0]
        sr = self.sample_rate

        ratio = max(float(module.params.get("ratio", 2.0)), 1.0)
        attack_ms = float(module.params.get("attack", 10.0))
        release_ms = float(module.params.get("release", 120.0))
        knee = max(float(module.params.get("knee", 6.0)), 0.0)
        makeup_db = float(module.params.get("gain", 0.0))
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)
        detector = str(module.params.get("detector", "rms"))

        state = self._state.setdefault(module.id, {})
        if "red" not in state or state["red"].shape[0] != V:
            state.clear()
            state["red"] = np.zeros(V, dtype=np.float64)  # carried reduction (dB)
            state["ms"] = np.zeros(V, dtype=np.float64)   # carried RMS mean-square

        if frames == 0:
            e = np.empty((V, 0), dtype=np.float32)
            return {"out": e, "gr": e.copy()}

        x = src.astype(np.float64)  # gain is applied to this
        k = key.astype(np.float64)  # the detector reads this

        # --- detector: key -> linear level (V, F) ---
        if detector == "rms":
            tau = self._COMP_RMS_MS * 1e-3
            a = 1.0 - float(np.exp(-1.0 / (max(tau, 1e-9) * sr)))
            a = min(max(a, 0.0), 1.0)
            zi = ((1.0 - a) * state["ms"])[:, np.newaxis]  # (V, 1)
            ms = lfilter([a], [1.0, -(1.0 - a)], k * k, axis=-1, zi=zi)[0]
            state["ms"] = ms[:, -1].copy()
            level = np.sqrt(np.maximum(ms, 0.0))
        else:  # peak: instantaneous rectified level, no detector smoothing
            level = np.abs(k)

        level_db = 20.0 * np.log10(np.maximum(level, self._COMP_LEVEL_FLOOR))

        # --- gain computer: level -> target reduction (dB >= 0) ---
        red_target = self._compressor_reduction_db(level_db, threshold, ratio, knee)

        # --- attack/release smoothing of the reduction envelope ---
        attack_coef = 1.0 if attack_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(attack_ms, 1e-6) * 1e-3 * sr))
        )
        release_coef = 1.0 if release_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(release_ms, 1e-6) * 1e-3 * sr))
        )
        level0 = state["red"]
        y = self._audio_to_cv_block(red_target, level0, attack_coef, release_coef)
        if y is None:
            # Degenerate coefficients (instant clamp / non-finite): the
            # per-sample reference loop defines the corner semantics.
            red, level0 = self._audio_to_cv_loop_voice(
                red_target, level0.copy(), attack_coef, release_coef
            )
            state["red"] = level0
        else:
            red = y
            state["red"] = y[:, -1].copy()

        # --- apply: reduction (dB) -> linear gain, make-up, parallel mix ---
        g = np.power(10.0, -red / 20.0)  # applied gain, <= 1
        makeup = 10.0 ** (makeup_db / 20.0)
        wet = x * g * makeup
        out = x * (1.0 - mix) + wet * mix
        gr = g - 1.0  # applied gain reduction as a 0..-1 CV

        return {"out": out.astype(np.float32), "gr": gr.astype(np.float32)}

    # Limiter: |x| floor so the C/|x| gain never divides by zero.
    _LIM_LEVEL_FLOOR = 1e-9

    def _render_limiter(self, module, frames: int, buffers, patch):
        """Brickwall lookahead peak limiter (see modules/limiter.py).

        Shape-polymorphic like the other effects: 1D ``(F,)`` in -> one
        detector + gain envelope + delay line, ``(F,)`` out; 2D ``(V, F)``
        in -> per-voice state, ``(V, F)`` out. The mono path is the
        ``V == 1`` case of the same core, so a single voice row is
        bit-identical to mono.

        Fixed latency of ``L = round(lookahead_ms * sr / 1000)`` samples
        (clamped to >= 1): the audio is delayed by L while the gain is
        computed L samples ahead, so the attack ramp lands on each peak.
        The latency is constant for a given ``lookahead`` and independent
        of block size.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        sr = self.sample_rate
        ceiling_db = float(module.params.get("ceiling", -1.0))
        release_ms = float(module.params.get("release", 80.0))
        lookahead_ms = float(module.params.get("lookahead", 5.0))

        ceiling = float(10.0 ** (ceiling_db / 20.0))  # linear ceiling
        look = max(int(round(lookahead_ms * 1e-3 * sr)), 1)  # latency in samples

        if src.ndim == 2:
            return self._render_limiter_core(module, frames, src, ceiling, look, release_ms)
        res = self._render_limiter_core(
            module, frames, src[np.newaxis, :], ceiling, look, release_ms
        )
        return res[0]

    def _render_limiter_core(self, module, frames, src, ceiling, look, release_ms):
        """Shared ``(V, F)`` lookahead-limiter engine.

        Pipeline, per voice: instantaneous target gain
        ``t = min(1, ceiling/|x|)`` -> slope-limited lookahead anticipation
        (the gain ramps down at <= 1/look per sample so it reaches the
        target exactly on the peak) -> one-pole release (instant on the way
        down, ``release``-paced on the way up) -> a final per-sample clamp
        to ``ceiling/|x|`` that makes the wall hard to the last ULP ->
        multiply into the ``look``-delayed audio.

        State carried across blocks (per voice): the ``look``-sample
        audio/detector history (the delay line + lookahead) and the release
        gain-reduction level. So the render is block-size independent:
        latency is exactly ``look`` regardless of block size, and the
        signal matches a single big-block render to float round-off (the
        anticipation's min-scan reassociates the ``+ i/look`` term, like the
        compressor's gain solve).
        """
        V = src.shape[0]
        sr = self.sample_rate

        state = self._state.setdefault(module.id, {})
        if "hist" not in state or state["hist"].shape != (V, look):
            state.clear()
            # Delay line + lookahead buffer, and the carried release reduction.
            state["hist"] = np.zeros((V, look), dtype=np.float64)
            state["red"] = np.zeros(V, dtype=np.float64)

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        x = src.astype(np.float64)
        buf = np.concatenate([state["hist"], x], axis=1)  # (V, look + F)
        M = buf.shape[1]
        absb = np.abs(buf)

        # Neutral short-circuit: nothing in the buffer reaches the ceiling
        # and the release envelope is fully open -> gain is identically 1,
        # so the output is the look-delayed input, bit-exact.
        if not state["red"].any() and float(absb.max(initial=0.0)) <= ceiling:
            out = buf[:, :frames].astype(np.float32)
            state["hist"] = buf[:, frames:].copy()
            return out

        # Instantaneous target gain t = min(1, ceiling/|x|), <= 1.
        t = np.minimum(1.0, ceiling / np.maximum(absb, self._LIM_LEVEL_FLOOR))

        # Lookahead anticipation: A[i] = min_j (t[i+j] + j/look) -- a linear
        # ramp of slope 1/look into each dip that lands on the trough.
        # Computed as a reversed running min: reverse t, take the minimum
        # accumulate of (t' - q/look), add q/look back, reverse again. Every
        # emitted sample (i < F) has its whole forward window inside buf, so
        # its anticipation is exact; only the non-emitted tail sees an edge.
        slope = 1.0 / look
        q = np.arange(M, dtype=np.float64)
        mc = np.minimum.accumulate(t[:, ::-1] - q * slope, axis=1)
        A = (mc + q * slope)[:, ::-1][:, :frames]  # (V, F), <= t

        # One-pole release on the gain *reduction* (red = 1 - gain): it rises
        # instantly to meet a deeper dip (attack_coef 1.0) and falls back
        # with the release one-pole. Instant attack breaks the vectorized
        # solver's algebra (coef 1 -> a = 0), so this uses the per-sample
        # voice loop -- the same fallback the compressor's smoother uses.
        rel_coef = 1.0 if release_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(release_ms, 1e-6) * 1e-3 * sr))
        )
        red, red_final = self._audio_to_cv_loop_voice(
            1.0 - A, state["red"].copy(), 1.0, rel_coef
        )
        state["red"] = red_final
        g = 1.0 - red  # (V, F), <= A <= t

        # Hard ceiling to the last ULP, then apply to the delayed audio.
        delayed = buf[:, :frames]  # x delayed by look
        g = np.minimum(g, ceiling / np.maximum(np.abs(delayed), self._LIM_LEVEL_FLOOR))
        out = g * delayed

        state["hist"] = buf[:, frames:].copy()
        return out.astype(np.float32)

    # ----- Noise gate ------------------------------------------------------

    # Threshold / range floor in dB: at this minimum the control is a
    # bypass -- threshold here means "always open", range here means
    # "full mute" (linear gain 0 rather than 10**(-80/20)).
    _GATE_THRESHOLD_MIN = -80.0
    # |key| floor before the dB conversion, so a silent key is a finite
    # (very negative) dB level instead of -inf.
    _GATE_LEVEL_FLOOR = 1e-9
    # Detector = an instant-attack, one-pole-release peak follower on the
    # rectified key. The release smooths the within-cycle dips of the
    # rectified waveform so the Schmitt sees an envelope, not the carrier;
    # 10 ms holds the envelope up between peaks down to ~50 Hz. Opening is
    # instant (the follower jumps to each new peak) so transients aren't
    # missed -- the *gain* attack/release shapes the audible edges.
    _GATE_DET_RELEASE_MS = 10.0

    def _render_noise_gate(self, module, frames: int, buffers, patch):
        """Hold-and-hysteresis downward gate (see modules/noise_gate.py).

        Shape-polymorphic like the other effects. Branches on the ``in``
        audio's ndim: 1D ``(F,)`` -> single detector + gate state machine +
        gain smoother, ``(F,)`` out; 2D ``(V, F)`` -> per-voice state,
        ``(V, F)`` out. The mono path is the ``V == 1`` case of the same
        core, so a single voice row is bit-identical to mono.

        Emits ``out`` (the gated audio) and ``open`` (a 0/1 gate CV that is
        high exactly while the gate is open -- a free gate-extractor).
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "open": z.copy()}

        # Neutral bypass: threshold at its floor -> always open -> the
        # signal passes untouched, so skip the detector entirely and hand
        # back a bit-exact copy of the input (open = 1 throughout).
        threshold = float(module.params.get("threshold", -45.0))
        if threshold <= self._GATE_THRESHOLD_MIN:
            out = src.astype(np.float32, copy=True)
            return {"out": out, "open": np.ones_like(out)}

        # Sidechain key: normalled to ``in`` when unpatched; an external
        # cable keys the gate off that signal instead (still gating ``in``).
        key = self._input_buffer(
            patch, buffers, module.id, "sidechain", collapse=False
        )

        if src.ndim == 2:
            V = src.shape[0]
            key = self._gate_align_key(key, src, V)
            return self._render_noise_gate_core(module, frames, src, key)

        # Mono. A 2D key collapses to the summed mix (key off the whole
        # signal, not one voice); an absent key normals to ``in``.
        if key is None:
            key = src
        elif key.ndim == 2:
            key = key.sum(axis=0)
        res = self._render_noise_gate_core(
            module, frames, src[np.newaxis, :], key[np.newaxis, :]
        )
        return {"out": res["out"][0], "open": res["open"][0]}

    @staticmethod
    def _gate_align_key(key, src, V):
        """Broadcast the optional sidechain key onto the ``in`` voice shape.

        Same policy as the compressor's sidechain alignment: unpatched
        normals to ``in`` (each voice keys off itself); a mono key
        broadcasts to every voice; a matching ``(V, F)`` key keys per
        voice; any other voice count collapses to its mean.
        """
        if key is None:
            return src
        if key.ndim == 1:
            return np.broadcast_to(key, (V, key.shape[0]))
        if key.shape[0] == V:
            return key
        if key.shape[0] == 1:
            return np.broadcast_to(key, (V, key.shape[1]))
        return np.broadcast_to(key.mean(axis=0), (V, key.shape[1]))

    def _render_noise_gate_core(self, module, frames, src, key):
        """Shared ``(V, F)`` gate engine: detector -> Schmitt+hold -> gain.

        A single per-sample voice loop (vectorized across voices, serial in
        time -- the same shape the limiter's release envelope uses) carries
        four pieces of per-voice state across blocks:

          * ``env``  -- the peak-follower detector level (linear),
          * ``open`` -- the Schmitt gate state (bool),
          * ``hold`` -- samples of hold remaining, and
          * ``gain`` -- the smoothed applied gain.

        Because every stage is a plain sample-by-sample recurrence with its
        state carried exactly, the render is **block-size independent and
        bit-exact** (no reassociation, unlike the compressor's vectorized
        gain solve): a big-block render equals a many-small-block render to
        the last bit. The loop is O(F) Python per block; vectorizing the
        Schmitt/hold timer is a possible future optimisation (as the
        envelope follower's own loop was later vectorized).

        Gate logic per sample: the follower opens instantly to a new peak
        and releases with a one-pole; the Schmitt opens above ``threshold``
        and only closes ``hysteresis`` dB below it; ``hold`` keeps it open
        for a minimum time after the level drops under the close threshold;
        the decision drives a target gain of 1 (open) or the ``range`` floor
        (closed), which the attack/release one-pole ramps toward.
        """
        V = src.shape[0]
        sr = self.sample_rate

        threshold = float(module.params.get("threshold", -45.0))
        hysteresis = max(float(module.params.get("hysteresis", 4.0)), 0.0)
        attack_ms = float(module.params.get("attack", 1.0))
        hold_ms = max(float(module.params.get("hold", 40.0)), 0.0)
        release_ms = float(module.params.get("release", 150.0))
        range_db = float(module.params.get("range", -80.0))

        open_thr = threshold
        close_thr = threshold - hysteresis
        # range at its floor means a hard mute (gain 0); above it, a
        # linear duck to 10**(range/20) -- the expander-style gentle gate.
        floor = (
            0.0 if range_db <= self._GATE_THRESHOLD_MIN
            else 10.0 ** (range_db / 20.0)
        )
        hold_samples = float(max(int(round(hold_ms * 1e-3 * sr)), 0))

        det_rel = 1.0 - float(
            np.exp(-1.0 / (max(self._GATE_DET_RELEASE_MS, 1e-6) * 1e-3 * sr))
        )
        atk = 1.0 if attack_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(attack_ms, 1e-6) * 1e-3 * sr))
        )
        rel = 1.0 if release_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(release_ms, 1e-6) * 1e-3 * sr))
        )

        state = self._state.setdefault(module.id, {})
        if "gain" not in state or state["gain"].shape[0] != V:
            state.clear()
            state["env"] = np.zeros(V, dtype=np.float64)   # detector level
            state["open"] = np.zeros(V, dtype=bool)        # Schmitt state
            state["hold"] = np.zeros(V, dtype=np.float64)  # hold remaining
            # Gate powers up closed: gain starts at the (current) floor.
            state["gain"] = np.full(V, float(floor), dtype=np.float64)

        if frames == 0:
            e = np.empty((V, 0), dtype=np.float32)
            return {"out": e, "open": e.copy()}

        x = src.astype(np.float64)
        k = np.abs(key.astype(np.float64))

        env = state["env"]
        is_open = state["open"]
        hold_ctr = state["hold"]
        gain = state["gain"]
        floor_v = float(floor)

        gate_open = np.empty((V, frames), dtype=bool)
        g_out = np.empty((V, frames), dtype=np.float64)

        for n in range(frames):
            a = k[:, n]
            # Peak follower: instant attack (jump to a new peak), one-pole
            # release (decay toward the current rectified sample).
            env = np.where(a > env, a, env + det_rel * (a - env))
            env_db = 20.0 * np.log10(np.maximum(env, self._GATE_LEVEL_FLOOR))

            hot = env_db > open_thr        # rise above -> open
            quiet = env_db < close_thr     # fall below close thr -> may close
            is_open = is_open | hot
            # While the level supports "open" (>= close thr), keep the hold
            # timer primed; only a genuine dip below it spends the timer.
            hold_ctr = np.where(is_open & ~quiet, hold_samples, hold_ctr)
            counting = is_open & quiet & (hold_ctr > 0.0)
            hold_ctr = np.where(counting, hold_ctr - 1.0, hold_ctr)
            close_now = is_open & quiet & (hold_ctr <= 0.0)
            is_open = is_open & ~close_now
            gate_open[:, n] = is_open

            # Ramp the gain toward its target (1 open / floor closed):
            # attack coefficient while rising (opening), release while
            # falling (closing) -- the asymmetric one-pole.
            target = np.where(is_open, 1.0, floor_v)
            coef = np.where(target > gain, atk, rel)
            gain = gain + coef * (target - gain)
            g_out[:, n] = gain

        state["env"] = env
        state["open"] = is_open
        state["hold"] = hold_ctr
        state["gain"] = gain

        out = (x * g_out).astype(np.float32)
        return {"out": out, "open": gate_open.astype(np.float32)}

    # ----- Transient shaper -----------------------------------------------

    # dB-conversion floor so a silent block never hits log10(0); -180 dBFS,
    # far below any musical signal (matches the compressor/gate floor).
    _TS_LEVEL_FLOOR = 1e-9
    # The attack/sustain knobs run -1..+1 and top out at +/- this many dB.
    _TS_MAX_DB = 12.0
    # Soft-saturation scale (dB) mapping the follower difference onto a 0..1
    # activation: a difference of _TS_SENS_DB gives ~63% of full effect, so
    # a few dB of transient already reaches most of the knob's range.
    _TS_SENS_DB = 4.0
    # Gain-smoothing one-pole time constant (ms) applied before the multiply
    # so a sharp transient's gain step doesn't zipper.
    _TS_SMOOTH_MS = 2.0
    # Per-``speed`` (fast_ms, slow_ms) follower time constants. Fast follower
    # tracks the onset; slow follower lags, so their dB gap is the transient.
    _TS_SPEEDS = {
        "fast": (0.5, 20.0),
        "med": (2.0, 50.0),
        "slow": (5.0, 120.0),
    }

    @staticmethod
    def _ts_coef(ms, sr):
        """One-pole smoothing coefficient for a time constant in ms.

        ``ms <= 0`` clamps to an instant follower (coef 1.0); otherwise the
        standard ``1 - exp(-1 / (tau * sr))``, the same conversion the
        compressor and gate use.
        """
        if ms <= 0.0:
            return 1.0
        return 1.0 - float(np.exp(-1.0 / (max(ms, 1e-6) * 1e-3 * sr)))

    def _ts_follow(self, t, level0, coef):
        """Symmetric one-pole follower via the shared fixed-point core.

        Reuses :meth:`_audio_to_cv_block` with the same coefficient for
        attack and release, which makes it take the time-invariant one-pole
        branch (a single vectorized solve, no pattern iteration); the
        per-sample voice loop is the fallback for the degenerate corners the
        block solve declines. ``t`` is the ``(V, F)`` float64 input (the
        rectified signal for the detectors, the linear gain for the
        smoother), ``level0`` the carried per-voice state ``(V,)``. Returns
        the ``(V, F)`` trajectory.
        """
        y = self._audio_to_cv_block(t, level0, coef, coef)
        if y is None:
            y, _ = self._audio_to_cv_loop_voice(t, level0.copy(), coef, coef)
        return y

    def _render_transient_shaper(self, module, frames: int, buffers, patch):
        """Threshold-free attack/sustain shaper (see modules/transient_shaper.py).

        Shape-polymorphic like the other effects. Branches on the ``in``
        audio's ndim: 1D ``(F,)`` -> single follower pair + gain smoother,
        ``(F,)`` out; 2D ``(V, F)`` -> per-voice state, ``(V, F)`` out. The
        mono path is the ``V == 1`` case of the same core, so a single voice
        row is bit-identical to mono.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        attack = float(module.params.get("attack", 0.0))
        sustain = float(module.params.get("sustain", 0.0))

        # Neutral short-circuit: no attack/sustain move -> unity gain
        # everywhere, so skip the followers and hand back a bit-exact copy of
        # the input. Independent of ``speed`` and voice count.
        if attack == 0.0 and sustain == 0.0:
            return src.astype(np.float32, copy=True)

        if src.ndim == 2:
            return self._render_transient_shaper_core(module, frames, src)
        return self._render_transient_shaper_core(
            module, frames, src[np.newaxis, :]
        )[0]

    def _render_transient_shaper_core(self, module, frames, src):
        """Shared ``(V, F)`` transient-shaper engine.

        Two envelope followers (fast, slow) on ``|in|`` via the shared
        follower core; their difference in dB isolates the transient
        (positive on attacks, negative on decays, ~zero in steady state).
        The positive part scales the ``attack`` gain and the negative part
        the ``sustain`` gain -- each soft-saturated to top out near
        +/- ``_TS_MAX_DB`` -- the two sum in dB, a short one-pole smooths the
        linear gain, and it multiplies ``src``.

        Threshold-free / level-invariant: the control signal is a dB
        *difference* (i.e. a ratio), so scaling the input leaves the gain
        unchanged above the log floor. Both followers and the gain smoother
        carry per-voice state, so the render is block-size independent to
        float64 round-off -- the shared follower's reassociated cumprod
        solve, the same class as the compressor's gain smoother (< 1e-6
        after the float32 cast), not the bit-exact per-sample recurrence the
        gate uses.
        """
        V = src.shape[0]
        sr = self.sample_rate

        attack = float(module.params.get("attack", 0.0))
        sustain = float(module.params.get("sustain", 0.0))
        speed = str(module.params.get("speed", "med"))
        fast_ms, slow_ms = self._TS_SPEEDS.get(speed, self._TS_SPEEDS["med"])

        state = self._state.setdefault(module.id, {})
        if "fast" not in state or state["fast"].shape[0] != V:
            state["fast"] = np.zeros(V, dtype=np.float64)   # fast follower level
            state["slow"] = np.zeros(V, dtype=np.float64)   # slow follower level
            state["gain"] = np.ones(V, dtype=np.float64)    # smoothed applied gain

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        x = src.astype(np.float64)
        rect = np.abs(x)

        fast_coef = self._ts_coef(fast_ms, sr)
        slow_coef = self._ts_coef(slow_ms, sr)
        fast_env = self._ts_follow(rect, state["fast"], fast_coef)
        slow_env = self._ts_follow(rect, state["slow"], slow_coef)
        state["fast"] = fast_env[:, -1].copy()
        state["slow"] = slow_env[:, -1].copy()

        # dB difference isolates the transient; being a ratio it is
        # level-invariant, which is what makes the shaper threshold-free.
        fast_db = 20.0 * np.log10(np.maximum(fast_env, self._TS_LEVEL_FLOOR))
        slow_db = 20.0 * np.log10(np.maximum(slow_env, self._TS_LEVEL_FLOOR))
        diff = fast_db - slow_db

        # positive part -> attack gain, negative part -> sustain gain; soft-
        # saturated so each knob approaches +/- _TS_MAX_DB at a strong
        # transient and is exactly 0 in its off-region (diff of the wrong
        # sign), so ``attack`` moves only onsets and ``sustain`` only tails.
        atk_act = 1.0 - np.exp(-np.maximum(diff, 0.0) / self._TS_SENS_DB)
        sus_act = 1.0 - np.exp(-np.maximum(-diff, 0.0) / self._TS_SENS_DB)
        gain_db = (
            attack * self._TS_MAX_DB * atk_act
            + sustain * self._TS_MAX_DB * sus_act
        )
        target = np.power(10.0, gain_db / 20.0)

        # smooth the linear gain (short one-pole) before multiplying; the
        # smoother's input is already level-invariant, so the smoothed gain
        # is too.
        smooth_coef = self._ts_coef(self._TS_SMOOTH_MS, sr)
        gain = self._ts_follow(target, state["gain"], smooth_coef)
        state["gain"] = gain[:, -1].copy()

        return (x * gain).astype(np.float32)

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


    # ----- Vocoder rendering ------------------------------------------------

    _VOCODER_BANDS = (8, 12, 16, 24)
    _VOCODER_SIBILANCE_HZ = 5000.0   # hiss-path highpass corner

    def _render_vocoder(self, module, frames: int, buffers, patch):
        """Channel vocoder: two matched bandpass banks + per-band followers.

        The modulator and the carrier (both summed to mono -- you vocode
        the mix, the same collapse rule as the modulation trio) each run
        through N parallel RBJ constant-peak bandpasses built from the
        same coefficients. The modulator's rectified band outputs feed
        the asymmetric one-pole follower bank -- all N bands plus the
        sibilance row are independent rows, so the whole bank is ONE
        call to the AudioToCV monotone-pattern block solve
        (:meth:`_audio_to_cv_block`), with the per-sample loop as its
        usual degenerate-coefficient fallback. The carrier's band
        outputs are multiplied by those envelopes and summed.

        The hiss path is the same trick one band further up: the
        modulator through a highpass at ~5 kHz -> follower -> gates a
        matching highpassed white-noise stream into the wet sum,
        restoring the consonants the bands can't see.

        State per module: DF-I history ``(x1, x2, y1, y2)`` as ``(N,)``
        arrays for each bank (the parametric_eq pattern -- raw history
        is coefficient-independent, so live edits of width/range/bands
        behave cleanly), scalar DF-I history for the two highpasses,
        the ``(N+1,)`` follower levels, and the noise Generator (a
        stream, so any block split draws the identical sequence).
        Everything carries across blocks -> block-size independent.
        ``mix=0`` returns the carrier bit-exact (states still advance,
        so riding the mix knob up doesn't snap from stale filters).
        """
        carrier = self._input_buffer(patch, buffers, module.id, "carrier")
        if carrier is None:
            return np.zeros(frames, dtype=np.float32)
        mod = self._input_buffer(patch, buffers, module.id, "mod")

        n_bands = int(round(float(module.params.get("bands", 16))))
        if n_bands not in self._VOCODER_BANDS:
            n_bands = min(self._VOCODER_BANDS, key=lambda v: abs(v - n_bands))
        freq_lo = min(max(float(module.params.get("freq_lo", 120.0)), 50.0), 500.0)
        freq_hi = min(max(float(module.params.get("freq_hi", 7500.0)), 2000.0), 12000.0)
        freq_hi = min(freq_hi, 0.45 * self.sample_rate)
        width = min(max(float(module.params.get("width", 1.0)), 0.3), 3.0)
        attack_ms = min(max(float(module.params.get("attack", 4.0)), 0.1), 100.0)
        release_ms = min(max(float(module.params.get("release", 60.0)), 1.0), 500.0)
        hiss = min(max(float(module.params.get("hiss", 0.4)), 0.0), 1.0)
        gain = min(max(float(module.params.get("gain", 1.0)), 0.0), 4.0)
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)

        state = self._state.setdefault(module.id, {})
        if state.get("n_bands") != n_bands:
            state.clear()
            state["n_bands"] = n_bands
            for bank in ("m", "c"):                      # mod / carrier banks
                for k in ("x1", "x2", "y1", "y2"):
                    state[bank + k] = np.zeros(n_bands, dtype=np.float64)
            for hp in ("hm", "hn"):                      # mod / noise highpass
                for k in ("x1", "x2", "y1", "y2"):
                    state[hp + k] = np.zeros(1, dtype=np.float64)
            state["env"] = np.zeros(n_bands + 1, dtype=np.float64)
            state["rng"] = np.random.default_rng(0xB0C0DE + module.id)

        if frames == 0:
            return np.empty(0, dtype=np.float32)

        dry = carrier.astype(np.float32, copy=True)      # (F,)
        car64 = carrier.astype(np.float64)
        mod64 = (
            np.zeros(frames, dtype=np.float64)
            if mod is None
            else mod.astype(np.float64)
        )

        # Band layout: log-spaced centres, Q from the adjacent-band
        # spacing scaled by ``width`` (width 1 = bands meet at their
        # -3 dB points; wider = overlap, narrower = gaps).
        centres = np.geomspace(freq_lo, max(freq_hi, freq_lo * 1.5), n_bands)
        ratio = centres[-1] / centres[0]
        bw_oct = np.log2(ratio) / max(n_bands - 1, 1) * width
        q = 1.0 / (2.0 * np.sinh(0.5 * np.log(2.0) * bw_oct))
        q = min(max(float(q), 0.5), 40.0)

        b0, b1, b2, a1n, a2n = self._vocoder_bp_coeffs(centres, q)

        mod_bands = np.empty((n_bands, frames), dtype=np.float64)
        car_bands = np.empty((n_bands, frames), dtype=np.float64)
        for k in range(n_bands):
            bk = np.array([b0[k], b1[k], b2[k]])
            ak = np.array([1.0, a1n[k], a2n[k]])
            mod_bands[k] = self._vocoder_biquad(
                state, "m", k, bk, ak, mod64, frames
            )
            car_bands[k] = self._vocoder_biquad(
                state, "c", k, bk, ak, car64, frames
            )

        # Sibilance detector + noise colour: one shared highpass design.
        hb, ha = self._vocoder_hp_coeffs(self._VOCODER_SIBILANCE_HZ)
        sib = self._vocoder_biquad(
            state, "h", 0, hb, ha, mod64, frames, key_prefix="hm"
        )
        noise = state["rng"].uniform(-1.0, 1.0, frames)
        noise_hp = self._vocoder_biquad(
            state, "h", 0, hb, ha, noise, frames, key_prefix="hn"
        )

        # Follower bank: N band rows + the sibilance row, one solve.
        sr = self.sample_rate
        att = 1.0 - float(np.exp(-1.0 / (attack_ms * 1e-3 * sr)))
        rel = 1.0 - float(np.exp(-1.0 / (release_ms * 1e-3 * sr)))
        t = np.abs(np.vstack([mod_bands, sib[None, :]]))     # (N+1, F)
        level = state["env"]
        env = self._audio_to_cv_block(t, level, att, rel)
        if env is None:
            env, level = self._audio_to_cv_loop_voice(t, level, att, rel)
        else:
            level = env[:, -1].copy()
        state["env"] = level

        wet = (car_bands * env[:n_bands]).sum(axis=0)
        wet += hiss * env[n_bands] * noise_hp
        wet *= gain

        if mix <= 0.0:
            return dry
        out = (1.0 - mix) * dry.astype(np.float64) + mix * wet
        return out.astype(np.float32)

    def _vocoder_biquad(
        self, state, bank, k, b, a, x, frames, key_prefix=None
    ):
        """One biquad over ``x`` with the house DF-I history carry.

        ``state[prefix + {x1,x2,y1,y2}][k]`` holds the raw DF-I history
        for slot ``k`` of the named bank; it's converted to the
        transposed-DF-II ``zi`` (the lfiltic identity, inlined -- the
        same lines as parametric_eq), the biquad runs in C via
        ``lfilter``, and the new history is read off the tails.
        """
        p = key_prefix or bank
        x1 = state[p + "x1"]; x2 = state[p + "x2"]
        y1 = state[p + "y1"]; y2 = state[p + "y2"]
        zi = np.array(
            [
                b[1] * x1[k] + b[2] * x2[k] - a[1] * y1[k] - a[2] * y2[k],
                b[2] * x1[k] - a[2] * y1[k],
            ],
            dtype=np.float64,
        )
        out = lfilter(b, a, x, zi=zi)[0]
        new_x1 = x[-1]
        new_x2 = x[-2] if frames >= 2 else x1[k]
        new_y1 = out[-1]
        new_y2 = out[-2] if frames >= 2 else y1[k]
        x1[k] = new_x1; x2[k] = new_x2
        y1[k] = new_y1; y2[k] = new_y2
        return out

    def _vocoder_bp_coeffs(self, centres, q):
        """RBJ constant-0dB-peak bandpass coefficients, vectorized over bands."""
        w0 = 2.0 * np.pi * centres / self.sample_rate
        alpha = np.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        b0 = alpha / a0
        b1 = np.zeros_like(b0)
        b2 = -alpha / a0
        a1n = (-2.0 * np.cos(w0)) / a0
        a2n = (1.0 - alpha) / a0
        return b0, b1, b2, a1n, a2n

    def _vocoder_hp_coeffs(self, freq):
        """RBJ highpass (Q = 0.707) at ``freq``, as (b, a) arrays."""
        freq = min(freq, 0.45 * self.sample_rate)
        w0 = 2.0 * np.pi * freq / self.sample_rate
        cw = np.cos(w0)
        alpha = np.sin(w0) / (2.0 * 0.70710678)
        a0 = 1.0 + alpha
        b = np.array([(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]) / a0
        a = np.array([1.0, (-2.0 * cw) / a0, (1.0 - alpha) / a0])
        return b, a

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

    # Looping-buffer window for the varispeed read head: the ``window``
    # param, in milliseconds, clamped to this range (engine-level, like
    # ``mix``). The read head trails the write head inside the window
    # and wraps within it, so the module keeps sounding forever on a
    # continuous signal. Longer = subtler loop texture but more latency
    # (the latency is half the window); shorter = tighter latency but a
    # stronger granular-repeat texture on non-unity shifts. That latency
    # is the unavoidable cost of varispeed on a live stream. The
    # frames*4 floor below still applies, so very small windows are
    # floored by the audio block size.
    _RESAMP_WINDOW_MS_MIN = 20.0
    _RESAMP_WINDOW_MS_MAX = 2000.0
    # The read head starts this fraction of the window behind the write
    # head. Centred (1/2) gives symmetric runway for pitch up (delay
    # shrinks) and pitch down (delay grows) before the first loop wrap.
    _RESAMP_INIT_FRAC = 0.5
    # Clamp the effective transpose so the playback ratio can't explode
    # (+/-60 st = +/-5 octaves -> ratio in [1/32, 32]).
    _RESAMP_MAX_ST = 60.0
    # Loop-seam declick. When the read head drifts inside a guard band
    # near either buffer edge (about to collide with the write head, or
    # to fall off the oldest sample), it jumps half a span back toward
    # the centre with a short equal-power crossfade between the old and
    # the jumped tap, instead of hard-wrapping with a click. This is the
    # nominal fade time; it shrinks automatically when extreme ratios
    # leave less old-tap runway than this.
    _RESAMP_XFADE_SEC = 0.008
    # Guard-band width as a fraction of the window (floored to the fade
    # length plus a margin, and to one block, so a jump always fires
    # with crossfade runway to spare and cross-block drift can't skip
    # past the band).
    _RESAMP_EDGE_FRAC = 0.06
    # Anti-alias (optional ``antialias`` toggle, off by default). Pitching
    # up reads the ring faster than it's written, shifting source content
    # above Nyquist where it folds back as aliasing (real tape is
    # inherently band-limited and never does this). With the toggle on, the
    # input is low-passed at Fs/(2*ratio) into a second ring the wet read
    # uses on up-shifts, so nothing folds. This is the Butterworth order;
    # the normalised cutoff (1/ratio) is floored to WN_MIN so the steepest
    # up-shifts get partial AA in a numerically safe range rather than a
    # degenerate filter (WN_MIN 0.05 -> full AA to ~+52 st, ratio ~20).
    _RESAMP_AA_ORDER = 8
    _RESAMP_AA_WN_MIN = 0.05
    # Cut a little below the ideal Fs/(2*ratio) so the filter's transition
    # band sits below Nyquist-after-scaling -- content that survives it then
    # still lands in-band instead of folding. Costs a sliver of the topmost
    # pitched-up octave for markedly less aliasing.
    _RESAMP_AA_MARGIN = 0.85

    def _render_resampler(self, module, frames: int, buffers, patch):
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

        Always returns the ``{"out", "out_l", "out_r"}`` dict (the
        multi-output convention). At ``spread`` 0 the pair mirrors the
        centre ``out``, so wiring the stereo outs plays the mono signal
        rather than falling silent; above 0 they carry the detuned pair.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}

        pitch_cv = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        # The brake is a transport gesture, module-wide: a voice-aware
        # gate collapses so *any* voice's gate high engages it.
        brake_gate = self._input_buffer(
            patch, buffers, module.id, "brake", collapse=False
        )
        if brake_gate is not None and brake_gate.ndim == 2:
            brake_gate = brake_gate.max(axis=0)

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
            result = self._render_resampler_core(
                module, frames, src, cv, brake_gate
            )
        else:
            # Mono audio. A 2D pitch_cv collapses to a single shared
            # transpose (mean over voices) -- summing pitch voltages would
            # be nonsense.
            if pitch_cv is not None and pitch_cv.ndim == 2:
                pitch_cv = pitch_cv.mean(axis=0)
            d = self._render_resampler_core(
                module,
                frames,
                src[np.newaxis, :],
                None if pitch_cv is None else pitch_cv[np.newaxis, :],
                brake_gate,
            )
            result = {name: buf[0] for name, buf in d.items()}

        # Always the {out, out_l, out_r} dict (the pair mirrors out at
        # spread 0), keyed onto the three output ports downstream.
        return result

    def _render_resampler_core(self, module, frames, src, cv, brake_gate=None):
        """Shared ``(V, F)`` varispeed engine.

        ``src`` is ``(V, F)`` audio; ``cv`` is ``(V, F)`` pitch CV or
        None; ``brake_gate`` is a ``(F,)`` gate for the tape-stop brake
        or None (already collapsed module-wide by the caller). The mono
        path calls this with ``V == 1``, so a single voice row is
        bit-identical to the mono render -- the float ops are the same
        per row regardless of V.

        Per output sample the read head advances by the playback ratio
        ``2 ** (st/12)`` (``st`` summed in semitone space and optionally
        glided), reading the per-voice ring buffer with 4-tap cubic
        Hermite (Catmull-Rom) interpolation -- a flat-passband read that
        keeps non-integer transposition clean, and returns the sample
        exactly at an integer position so unity ratio stays bit-exact.
        The whole block is vectorized: the read positions are the
        cumulative integral of the per-sample ratio.

        **Loop-seam declick.** At a non-unity ratio the read head
        eventually collides with the write head (pitch up) or falls off
        the oldest buffered sample (pitch down). Rather than
        hard-wrapping the read phase with a click (audio from a window
        apart butted together), the head watches a guard band near both
        edges and *jumps half a span* back toward the centre as soon as
        it drifts inside, equal-power crossfading from the old tap to
        the jumped tap (``_RESAMP_XFADE_SEC``, shortened when extreme
        ratios leave less runway). Far from the edges nothing fires and
        the legacy single-tap path runs bit-identically -- in particular
        unity ratio, where the head doesn't drift, stays a bit-exact
        delayed passthrough.

        **Dry/wet mix.** ``mix`` blends the varispeed signal against a
        dry tap read from the *same ring buffer* at the fixed initial
        delay, so dry and wet are sample-aligned at unity ratio and a
        mix sweep is a coherent blend with no slapback offset. ``mix=1``
        (the default) skips the dry read and is bit-identical to the
        wet-only render; ``mix=0`` is the delayed dry passthrough.

        **Anti-alias (optional).** With the ``antialias`` param on, a
        second ring holds the input low-passed at ``Fs/(2*ratio)``
        (Butterworth), and the wet read samples *that* ring whenever the
        block pitches up -- so source content that would shift past
        Nyquist and fold back as aliasing is removed before the faster
        read, the way tape's inherent band-limiting prevents it. Off by
        default (the raw read keeps the lo-fi/tape character and every
        existing render bit-for-bit). Unity and pitch-down keep reading
        the raw ring, and the dry tap always does, so those bit-exact
        paths are untouched; only the up-shift wet read changes.

        **Tape-stop brake.** With the ``brake`` param on or the
        ``brake`` gate high, a per-sample brake position ramps 1 -> 0
        over ``brake_time`` seconds (0 -> 1 over ``spinup_time`` on
        release) and *multiplies the playback ratio* -- deceleration
        linear in speed, the constant-torque physics of a real platter
        or capstan winding down. Working in ratio space is the point:
        glide ramps in semitone space, where a dead stop is minus
        infinity, unreachable; the brake scales the ratio to an actual
        zero, freezing the read head (pitch dives through the floor,
        then the output holds a constant -- silence through any AC
        path). While frozen the write head keeps filling the ring, so
        the ordinary low-edge seam machinery re-centres the head under
        its crossfade as the ring laps it -- crossfades between
        near-constant values, inaudible. The gesture is module-wide
        (every voice and spread channel brakes together, one transport)
        and sits after glide/pitch and before the anti-alias cutoff
        tracking (a braked read is slower, so AA correctly relaxes).
        Disengaged with the position fully recovered, the multiply is
        skipped entirely -- every existing render is bit-for-bit
        untouched. The ``mix`` dry tap is a fixed-lag ring read and
        keeps playing through a stop.
        """
        V = src.shape[0]
        sr = self.sample_rate
        window_ms = float(module.params.get("window", 200.0))
        window_ms = min(
            max(window_ms, self._RESAMP_WINDOW_MS_MIN),
            self._RESAMP_WINDOW_MS_MAX,
        )
        # 200.0/1000.0 is the same double as the old 0.2 literal, so the
        # default window reproduces the pre-param L bit-for-bit.
        L = int((window_ms / 1000.0) * sr)
        L = max(L, frames * 4, 8)   # always comfortably larger than a block
        span = L - 1                # loop span; keeps both interp taps valid

        state = self._state.setdefault(module.id, {})
        old = state.get("buf")
        if (
            old is not None
            and old.shape[0] == V
            and old.shape[1] != L
            and "xf_rem" in state
        ):
            # The ``window`` param changed mid-stream (same voice count):
            # rebuild the ring at the new length, preserving the most
            # recent audio so a slider drag doesn't punch a hole in the
            # sound. Sample at lag ``l`` moves from ``(w - l) % oldL`` to
            # ``L - l`` with the new write head at 0, so every head keeps
            # its absolute lag (clamped into the new window). If the new
            # geometry leaves a head inside a guard band, the ordinary
            # seam machinery re-centres it under an equal-power crossfade
            # on this very block. Only a shrink below a head's lag loses
            # the content under it (the tail it was reading no longer
            # exists); that one step rides the seam crossfade too, just
            # with new-window content.
            oldL = old.shape[1]
            keep = min(oldL, L)
            w = int(state["write_idx"])
            src_idx = (w + oldL - keep + np.arange(keep)) % oldL
            newbuf = np.zeros((V, L), dtype=np.float64)
            newbuf[:, L - keep:] = old[:, src_idx]
            state["buf"] = newbuf
            state["write_idx"] = 0
            np.clip(state["delay"], 1.0, float(L - 1), out=state["delay"])
            # In-flight seam fades reference the old geometry; drop them
            # (their old tap would read relocated content).
            state["xf_rem"][:] = 0
            state["xf_len"][:] = 1
            state["xf_off"][:] = 0.0
            # Stereo-spread channels ride the same geometry; drop them so
            # they re-seed aligned to the rebuilt centre head next read.
            for suf in ("_l", "_r"):
                for k in ("delay", "xf_rem", "xf_len", "xf_off", "seam_jumps"):
                    state.pop(k + suf, None)
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
        if "xf_rem" not in state:
            # Seam-crossfade + jump-count state (created lazily so a
            # reinit -- or a state carried across this feature's
            # introduction -- picks it up cleanly).
            state["xf_rem"] = np.zeros(V, dtype=np.int64)
            state["xf_len"] = np.ones(V, dtype=np.int64)
            state["xf_off"] = np.zeros(V, dtype=np.float64)
            state["seam_jumps"] = np.zeros(V, dtype=np.int64)

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
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)

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

        # --- per-channel playback ratios ---
        # ``out`` is the centre pitch (unchanged). A positive ``spread``
        # adds a stereo detune pair -- ``out_l`` a touch flat, ``out_r`` a
        # touch sharp (``spread`` cents, split half to each side) -- for
        # one-module chorus-style width from a mono source. spread == 0
        # keeps the module mono: one read, the centre, bit-for-bit what it
        # always produced.
        spread = float(module.params.get("spread", 0.0))
        if spread > 0.0:
            half = spread / 200.0    # cents -> +/- semitones per side
            chans = (
                ("out", "", 0.0),
                ("out_l", "_l", -half),
                ("out_r", "_r", half),
            )
        else:
            chans = (("out", "", 0.0),)
            for suf in ("_l", "_r"):     # drop stale spread state when mono
                for k in ("delay", "xf_rem", "xf_len", "xf_off", "seam_jumps"):
                    state.pop(k + suf, None)

        ratios = {}
        for name, ch, off in chans:
            s = smoothed + off if off else smoothed
            ratios[name] = np.exp2(
                np.clip(s, -self._RESAMP_MAX_ST, self._RESAMP_MAX_ST) / 12.0
            )

        # --- tape-stop / spin-up brake (ratio space) ---
        # The brake position ramps 1 -> 0 (engaged) / 0 -> 1 (released),
        # linear in speed, and multiplies every channel's ratio -- a true
        # deceleration to a dead stop, which semitone-space glide can't
        # reach (a stop is -inf semitones). Module-wide: one transport,
        # shared by all voices and spread channels. Fully released with
        # the position recovered, the multiply is skipped -- brake-free
        # renders stay bit-for-bit what they always were.
        brake_on = float(module.params.get("brake", 0.0)) >= 0.5
        brake_pos = float(state.get("brake_pos", 1.0))
        if brake_gate is not None:
            gate = (brake_gate.astype(np.float64) >= 0.5) | brake_on
            engaged = bool(gate.any())
        else:
            gate = None
            engaged = brake_on
        if engaged or brake_pos < 1.0:
            # Slopes in position-units per sample; a zero time stops (or
            # recovers) within one sample.
            dn = 1.0 / max(
                float(module.params.get("brake_time", 0.5)) * sr, 1.0
            )
            up = 1.0 / max(
                float(module.params.get("spinup_time", 0.25)) * sr, 1.0
            )
            if gate is None:
                gate = np.full(frames, brake_on)
            factor, brake_pos = _brake_ramp(brake_pos, gate, dn, up)
            state["brake_pos"] = brake_pos
            for name in ratios:
                ratios[name] = ratios[name] * factor
        else:
            state["brake_pos"] = 1.0

        # --- optional anti-alias for pitch-up ---
        # Reading faster than the write shifts source content above Nyquist,
        # which folds back as aliasing; band-limiting the input *before* the
        # faster read is the fix. Maintain one band-limited ring of the
        # input (low-passed at Fs/(2*ratio)) shared by all channels, its
        # cutoff tracking the highest channel ratio; each channel reads that
        # ring on its own up-shift, the raw ring otherwise. Off by default
        # -> raw read, every existing render bit-for-bit. Pitch-down and
        # unity keep the raw ring (bit-exact); the dry tap always does.
        read_bufs = {name: buf for name, _, _ in chans}
        if float(module.params.get("antialias", 0.0)) >= 0.5:
            r_rep = max(1.0, max(float(r.max()) for r in ratios.values()))
            # Normalised cutoff is 1/ratio; floor it so extreme up-shifts
            # stay in a numerically safe Butterworth range (partial AA
            # rather than a degenerate filter).
            wn = min(
                0.98,
                max(self._RESAMP_AA_WN_MIN, self._RESAMP_AA_MARGIN / r_rep),
            )
            # Second-order-section form: high-order Butterworth in
            # transfer-function form is ill-conditioned at low cutoffs
            # (extreme up-shifts); sos stays numerically stable.
            sos = butter(self._RESAMP_AA_ORDER, wn, btype="low", output="sos")
            buf_aa = state.get("buf_aa")
            if buf_aa is None or buf_aa.shape != (V, L):
                # Seed from the raw ring so first use / a window change / a
                # live toggle never punches a wet dropout -- the recent tail
                # is present (unfiltered for one window, then all AA'd).
                buf_aa = buf.copy()
                state["aa_zi"] = np.zeros(
                    (sos.shape[0], V, 2), dtype=np.float64
                )
            filt, state["aa_zi"] = sosfilt(
                sos, src.astype(np.float64), axis=-1, zi=state["aa_zi"]
            )
            buf_aa[:, write_slots] = filt
            state["buf_aa"] = buf_aa
            for name, _, _ in chans:
                if float(ratios[name].max()) > 1.0 + 1e-9:
                    read_bufs[name] = buf_aa
        else:
            # AA off: drop the second ring so a later toggle-on re-seeds
            # from the up-to-date raw ring instead of reading a stale gap.
            state.pop("buf_aa", None)

        # --- loop-seam declick guard band (shared by all channels) ---
        x_nom = max(1, int(self._RESAMP_XFADE_SEC * sr))
        guard = int(max(self._RESAMP_EDGE_FRAC * L, x_nom + 8, frames + 8))
        guard = min(guard, span // 3)

        # --- read each channel through the one ring (own head + seam state) ---
        for _, ch, _ in chans:
            self._ensure_resampler_channel(state, ch, V)
        wets = {}
        for name, ch, _ in chans:
            wets[name], nd = self._resampler_read_channel(
                state, ch, read_bufs[name], ratios[name],
                head, L, span, guard, x_nom, frames, V,
            )
            state["delay" + ch] = nd

        # --- dry/wet mix: the dry tap is the raw centre ring read at the
        # fixed initial delay (shared across channels), so unity wet and
        # dry are sample-aligned and the dry stays full-band under spread.
        if mix < 1.0:
            init_lag = int(max(1, min(L - 1, int(self._RESAMP_INIT_FRAC * L))))
            dry_slots = (head + (L - init_lag) + np.arange(frames)) % L
            dry = buf[:, dry_slots]
            out = {
                name: (mix * w + (1.0 - mix) * dry).astype(np.float32)
                for name, w in wets.items()
            }
        else:
            out = {name: w.astype(np.float32) for name, w in wets.items()}

        # Always emit the stereo pair (chorus/reverb convention): at
        # spread 0 they mirror the centre, so a patch wired to out_l/out_r
        # plays the mono signal rather than falling silent until spread is
        # dialled up. Same array -> no extra read/copy on the mono path.
        if "out_l" not in out:
            out["out_l"] = out["out"]
            out["out_r"] = out["out"]

        state["buf"] = buf
        state["write_idx"] = head
        state["last_st"] = last_st
        return out

    def _ensure_resampler_channel(self, state, ch, V):
        """Lazily create a stereo-spread channel's read-head + crossfade
        state. The centre ("") is set up by the main reinit; ``"_l"`` /
        ``"_r"`` start *aligned* with the centre head (``state["delay"]``)
        so engaging ``spread`` mid-stream doesn't jump -- they then drift
        apart through their own detuned ratios.
        """
        if ch == "" or ("delay" + ch) in state:
            return
        state["delay" + ch] = state["delay"].copy()
        state["xf_rem" + ch] = np.zeros(V, dtype=np.int64)
        state["xf_len" + ch] = np.ones(V, dtype=np.int64)
        state["xf_off" + ch] = np.zeros(V, dtype=np.float64)
        state["seam_jumps" + ch] = np.zeros(V, dtype=np.int64)

    def _resampler_read_channel(
        self, state, ch, read_buf, ratio, head, L, span, guard, x_nom,
        frames, V,
    ):
        """Read one detune channel from the ring with seam-declick, using
        this channel's own read head (``state["delay"+ch]``) and crossfade
        state. ``ratio`` is the channel's (V, F) per-sample playback rate;
        ``read_buf`` the ring it samples (raw, or the anti-alias ring on an
        up-shift). Returns ``(wet (V, F) float64, new_delay (V,))``; the
        caller stores ``new_delay`` back under ``"delay"+ch``.
        """
        delay = state["delay" + ch]
        cum = np.cumsum(ratio, axis=-1)
        excum = cum - ratio                 # exclusive cumsum (offset per sample)
        offs = (L - delay)[:, np.newaxis] + excum
        sum_ratio = cum[:, -1]

        slow = bool(
            state["xf_rem" + ch].any()
            or (offs[:, 0] <= guard).any()
            or (offs.max(axis=-1) >= span - guard).any()
        )
        if not slow:
            # In-band fast path (see _render_resampler_core): the guard band
            # keeps the head clear of both edges, so the 4-tap Hermite read
            # never wraps; at an integer read position frac is exactly 0 and
            # Hermite returns the sample, so unity stays a bit-exact
            # delayed passthrough.
            phase = np.mod(offs, span)          # in [0, span)
            i0 = np.floor(phase).astype(np.int64)
            frac = phase - i0
            rows = np.arange(V)[:, np.newaxis]
            jm1 = (head + np.clip(i0 - 1, 0, L - 1)) % L
            j2 = (head + np.clip(i0 + 2, 0, L - 1)) % L
            wet = _hermite4(
                read_buf[rows, jm1],
                read_buf[rows, (head + i0) % L],
                read_buf[rows, (head + i0 + 1) % L],
                read_buf[rows, j2],
                frac,
            )
            # Carry the read head as a lag behind the (new) write head.
            new_delay = 1.0 + np.mod((delay + frames - sum_ratio) - 1.0, span)
        else:
            wet = np.empty((V, frames), dtype=np.float64)
            new_delay = np.empty(V, dtype=np.float64)
            for v in range(V):
                wet[v], new_delay[v] = self._resampler_voice_declick(
                    state, ch, v, read_buf[v], head, offs[v], ratio[v],
                    float(delay[v]), float(sum_ratio[v]),
                    frames, L, span, guard, x_nom,
                )
        return wet, new_delay

    def _resampler_voice_declick(
        self, state, ch, v, bufv, head, p, r, delay_v, sum_r,
        frames, L, span, guard, x_nom,
    ):
        """One voice's block with the read head inside the guard band (or
        a seam crossfade still in flight from the previous block).

        ``ch`` is the detune-channel suffix ("" centre, "_l"/"_r" the
        stereo spread) that keys this channel's own crossfade state.

        ``p`` is the voice's unwrapped read-position trajectory for this
        block (position in the window: 0 = oldest, ``span`` = newest) and
        ``r`` its per-sample ratio. Applies a half-span jump wherever the
        trajectory enters the guard band, records one equal-power
        crossfade per jump (old tap fading out, jumped tap fading in) and
        returns ``(float64 out, new end-of-block delay)``.

        Fade weights are a function of the sample index *within the
        fade*, so a fade that outlives the block is carried in ``state``
        and continues seamlessly next call. An old tap that runs out of
        valid content mid-fade (extreme ratios) is force-completed early
        -- by then its weight is already near zero. Seam events are
        always at least half a span of head travel apart, so fades never
        overlap; each jump also bumps ``seam_jumps[v]`` (an observable
        for tests and debugging).
        """
        half = 0.5 * span
        p = p.copy()
        fades = []          # (n0, n_here, t0, x_eff, back_off)
        jump_sum = 0.0      # net delay change from the jumps

        # Continue an in-flight fade from the previous block.
        rem = int(state["xf_rem" + ch][v])
        if rem > 0:
            x_eff = int(state["xf_len" + ch][v])
            off = float(state["xf_off" + ch][v])
            n_here = min(rem, frames)
            fades.append([0, n_here, x_eff - rem, x_eff, off])

        # Low edge: the head is on nearly-overwritten (oldest) content --
        # the pitch-down collision. Jump forward half a span; the old
        # tap, half a span behind the jumped head, fades out.
        if p[0] <= guard:
            p += half
            jump_sum -= half
            fades_adj = [f for f in fades if f[0] == 0]
            for f in fades_adj:
                # A carried fade's back tap rides the canonical
                # trajectory; keep it in place across the new jump.
                f[4] -= half
            fades.append([0, min(x_nom, frames), 0, x_nom, -half])
            state["seam_jumps" + ch][v] += 1

        # High edge: the head is about to collide with the write head --
        # the pitch-up collision. Jump back half a span at the first
        # offending sample (can recur in one block at extreme ratios).
        # The old tap runs on toward the newest sample, so the fade
        # shortens to the runway left at this ratio.
        while True:
            hits = np.nonzero(p >= span - guard)[0]
            if hits.size == 0:
                break
            n0 = int(hits[0])
            rmax = float(r[n0:n0 + x_nom].max())
            runway = (span - 1.0) - float(p[n0])
            x_eff = int(min(x_nom, max(1.0, runway / max(rmax, 1e-9))))
            p[n0:] -= half
            jump_sum += half
            fades.append([n0, min(x_eff, frames - n0), 0, x_eff, half])
            state["seam_jumps" + ch][v] += 1

        # Jumped main tap, 4-tap Hermite (same read as the fast path;
        # p is inside (0, span) by construction after the jumps, so the
        # outer taps only clamp right at the window ends).
        i0 = np.floor(p).astype(np.int64)
        frac = p - i0
        out = _hermite4(
            bufv[(head + np.clip(i0 - 1, 0, L - 1)) % L],
            bufv[(head + i0) % L],
            bufv[(head + i0 + 1) % L],
            bufv[(head + np.clip(i0 + 2, 0, L - 1)) % L],
            frac,
        )

        # Blend each fade's old tap back in, equal-power. A back tap that
        # has left the valid window keeps the new tap at full weight.
        state["xf_rem" + ch][v] = 0
        for n0, n_here, t0, x_eff, off in fades:
            if n_here <= 0:
                continue
            t = (t0 + np.arange(n_here, dtype=np.float64) + 1.0) / x_eff
            t = np.minimum(t, 1.0)
            pb = p[n0:n0 + n_here] + off
            valid = (pb >= 0.0) & (pb <= span - 1.0)
            pbc = np.clip(pb, 0.0, span - 1.0)
            ib = np.floor(pbc).astype(np.int64)
            fb = pbc - ib
            back = _hermite4(
                bufv[(head + np.clip(ib - 1, 0, L - 1)) % L],
                bufv[(head + ib) % L],
                bufv[(head + ib + 1) % L],
                bufv[(head + np.clip(ib + 2, 0, L - 1)) % L],
                fb,
            )
            w_old = np.where(valid, np.cos(0.5 * np.pi * t), 0.0)
            g_new = np.where(valid, np.sin(0.5 * np.pi * t), 1.0)
            seg = slice(n0, n0 + n_here)
            out[seg] = g_new * out[seg] + w_old * back
            if t0 + n_here < x_eff and n0 + n_here >= frames:
                # The fade outlives the block -- carry the remainder.
                state["xf_rem" + ch][v] = x_eff - (t0 + n_here)
                state["xf_len" + ch][v] = x_eff
                state["xf_off" + ch][v] = off

        new_delay = delay_v + frames - sum_r + jump_sum
        new_delay = 1.0 + float(np.mod(new_delay - 1.0, span))
        return out, new_delay

    # ----- PitchShifter rendering -----------------------------------------

    # Clamp the effective transpose so the playback ratio (and the stretch
    # ring sized from it) stays bounded. +/-36 st -> ratio in [1/8, 8].
    _PS_MAX_ST = 36.0
    # Formant preservation (LPC whiten -> shift residual -> re-color).
    _PS_LPC_ORDER = 24          # envelope detail; ~speech-codec order at 44.1k
    _PS_LPC_WIN = 1024          # raw samples per envelope estimate
    # Pitch-synchronous deep-bass grain sizing: re-estimate the input
    # period this often, and grow the effective grain so it always holds
    # at least _PS_SYNC_PERIODS periods (capped; user grain is the floor).
    _PS_DETECT_EVERY = 2048
    _PS_DETECT_WIN = 4096
    _PS_SYNC_PERIODS = 2.5
    _PS_MAX_GRAIN_SEC = 0.15
    _PS_FMIN = 25.0

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
        """Shared ``(V, F)`` engine; mono runs with V=1 (bit-identical).

        Per voice and per block: (1) every ``_PS_DETECT_EVERY`` input
        samples the input period is re-estimated and, when the current
        grain holds fewer than ``_PS_SYNC_PERIODS`` periods (deep bass),
        the engine is rebuilt with a longer grain -- primed from the old
        engine's ring and equal-power crossfaded in over one block, with
        20% hysteresis so it never thrashes; (2) with
        ``formant_preserve`` on, an LPC envelope is estimated from the
        raw history, the input is whitened through ``A(z)``, the grain
        engine shifts the residual, and the output is re-colored through
        ``1/A(z)`` using the coefficient set from ~one grain ago (so the
        envelope rides with the content it described). Coefficient sets
        change per block with lfilter zf-carry -- the standard adaptive-
        filter compromise, smooth because envelopes evolve slowly.
        """
        V = src.shape[0]
        sr = self.sample_rate
        semis = float(module.params.get("semitones", 0.0))
        cents = float(module.params.get("cents", 0.0))
        cv_depth = float(module.params.get("cv_depth", 12.0))
        mix = float(np.clip(float(module.params.get("mix", 1.0)), 0.0, 1.0))
        grain_ms = float(module.params.get("grain_size", 50.0))
        overlap = max(2, min(4, int(module.params.get("overlap", 2))))
        formant = bool(module.params.get("formant_preserve", False))
        Lg = max(8, int(round(grain_ms * 1e-3 * sr)))

        head = max(16384, 16 * int(getattr(self, "block_size", 512)))
        state = self._state.setdefault(module.id, {})
        if state.get("V") != V or state.get("Lg") != Lg or state.get("ov") != overlap:
            state.clear()
            state["V"] = V
            state["Lg"] = Lg
            state["ov"] = overlap
            state["eng"] = [_GrainShifter(Lg, overlap, head) for _ in range(V)]
            state["last_det"] = [0] * V
            state["regrains"] = np.zeros(V, dtype=np.int64)
            state["lpc_zi_w"] = [None] * V     # whitening FIR state
            state["lpc_zi_s"] = [None] * V     # synthesis IIR state
            state["lpc_fifo"] = [[] for _ in range(V)]
        engines = state["eng"]

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        base_st = semis + cents / 100.0
        out = np.empty((V, frames), dtype=np.float32)
        for v in range(V):
            st = base_st if cv is None else base_st + cv_depth * float(np.mean(cv[v]))
            st = max(-self._PS_MAX_ST, min(self._PS_MAX_ST, st))
            r = 2.0 ** (st / 12.0)
            eng = engines[v]
            x = src[v].astype(np.float64)

            # --- pitch-synchronous grain sizing (deep bass) ---
            old_eng = None
            if eng.iw - state["last_det"][v] >= self._PS_DETECT_EVERY:
                state["last_det"][v] = eng.iw
                tail = eng.history(self._PS_DETECT_WIN)
                period = _detect_period(tail, sr, fmin=self._PS_FMIN)
                want = Lg
                if period is not None:
                    want = max(Lg, int(round(self._PS_SYNC_PERIODS * period)))
                want = min(want, int(self._PS_MAX_GRAIN_SEC * sr))
                cur = eng.Lg
                if want > cur * 1.2 or (cur > Lg and want < cur * 0.8):
                    new_eng = _GrainShifter(want, overlap, head)
                    hist = eng.history(new_eng.Lin - frames - 8)
                    hist_dry = None
                    if eng.db is not None:
                        hist_dry = eng.history(hist.shape[0], dry=True)
                    if hist.shape[0]:
                        new_eng.process(hist, r, x_dry=hist_dry)  # prime; output discarded
                    old_eng = eng
                    eng = new_eng
                    engines[v] = eng
                    state["regrains"][v] += 1

            # --- formant preserve: whiten the engine input ---
            a_cur = None
            xw = x
            if formant:
                raw_tail = eng.history(self._PS_LPC_WIN, dry=True) if eng.db is not None \
                    else eng.history(self._PS_LPC_WIN)
                a_cur = _lpc_coeffs(raw_tail, self._PS_LPC_ORDER, sr)
                if a_cur is not None:
                    zi = state["lpc_zi_w"][v]
                    if zi is None:
                        zi = np.zeros(self._PS_LPC_ORDER)
                    xw, zf = lfilter(a_cur, [1.0], x, zi=zi)
                    state["lpc_zi_w"][v] = zf
            x_dry = x if formant else None

            was_primed = eng.primed
            wet = eng.process(xw, r, x_dry=x_dry)
            if old_eng is not None:
                # Equal-power splice from the outgoing engine's output.
                wet_old = old_eng.process(
                    xw, r, x_dry=x_dry if old_eng.db is not None else None
                )
                t = (np.arange(frames) + 1.0) / frames
                wet = np.sin(0.5 * np.pi * t) * wet + np.cos(0.5 * np.pi * t) * wet_old

            # --- formant preserve: re-color with the envelope from ~one
            # grain ago (aligns the envelope with the content it described).
            if formant:
                fifo = state["lpc_fifo"][v]
                # Envelopes estimated before the wet path primed describe
                # the onset transient, not streaming content -- feeding
                # them to the synthesis filter blasts the first wet block.
                fifo.append(a_cur if was_primed else None)
                lag_blocks = max(0, int(round(eng.Lg / float(frames))))
                while len(fifo) > lag_blocks + 1:
                    fifo.pop(0)
                a_del = fifo[0]
                if a_del is not None:
                    zi = state["lpc_zi_s"][v]
                    if zi is None:
                        zi = np.zeros(self._PS_LPC_ORDER)
                    wet, zf = lfilter([1.0], a_del, wet, zi=zi)
                    state["lpc_zi_s"][v] = zf
                    # Safety valve: a recolored block should sit near the
                    # raw input's level (whiten -> shift -> re-color is
                    # level-preserving by construction). An ill-conditioned
                    # estimate (attack edges, near-silence) can't be ruled
                    # out, so bound the block at 4x the raw RMS.
                    raw_rms = float(np.sqrt((x ** 2).mean()))
                    rec_rms = float(np.sqrt((wet ** 2).mean()))
                    lim = 4.0 * max(raw_rms, 1e-4)
                    if rec_rms > lim:
                        wet = wet * (lim / rec_rms)

            Dc = eng.latency(r)  # exact wet latency -> phase-coherent dry/wet mix
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
        path = str(module.params.get("path", ""))
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

    # ----- ring modulator ----------------------------------------------------

    # Base seed for the jitter RNG. Combined with the module id so two
    # bitcrushers wobble on independent but individually reproducible
    # streams (a fresh render re-seeds from here, hence deterministic).
    _BITCRUSHER_JITTER_SEED = 0x0B17C

    def _render_bitcrusher(self, module, frames: int, buffers, patch):
        """Bitcrusher: mid-tread bit-depth quantize + sample-hold decimation.

        Signal flow ``in -> decimate -> quantize -> [dc filter] -> mix``.
        Both crush stages are skipped at their neutral settings, so
        ``bits == 24`` and ``rate_div == 1`` (with ``dc_filter`` off)
        returns the input untouched -- a bit-exact passthrough at any
        ``mix``; ``mix <= 0`` is likewise bit-exact dry. Quantize is a
        pointwise ``round(x*2^(bits-1))/2^(bits-1)``; decimation is a
        deliberately aliased sample-and-hold (no anti-image filter). The
        hold phase (a global sample offset plus the per-voice held value,
        and the seeded jitter boundary stream) lives in ``self._state``,
        so holds stay continuous across block joins and every path here is
        *exactly* block-size independent. Shape-polymorphic: the ``(V, F)``
        core runs with ``V == 1`` for a mono ``in``, so one voice row is
        bit-identical to the mono render and voices stay independent.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None or src.size == 0:
            return np.zeros(frames, dtype=np.float32)

        bits = int(round(float(module.params.get("bits", 24))))
        rate_div = int(round(float(module.params.get("rate_div", 1))))
        jitter = min(max(float(module.params.get("jitter", 0.0)), 0.0), 1.0)
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)
        dc_filter = bool(module.params.get("dc_filter", False))

        bits = min(max(bits, 1), 24)
        rate_div = min(max(rate_div, 1), 64)

        quant_active = bits < 24
        decim_active = rate_div > 1

        # Bit-exact dry: mix folds everything to the input, or there is
        # simply nothing to do (both crush ops skipped, no DC filter).
        if mix <= 0.0:
            return src
        if not quant_active and not decim_active and not dc_filter:
            return src

        was_mono = src.ndim == 1
        x = np.atleast_2d(src).astype(np.float64)          # (V, F)
        v = x.shape[0]

        wet = x
        if decim_active:
            wet = self._bitcrush_decimate(module, wet, rate_div, jitter, v, frames)
        if quant_active:
            levels = 2.0 ** (bits - 1)
            wet = np.round(wet * levels) / levels
        if dc_filter:
            wet = self._bitcrush_dcblock(module, wet, v)

        if mix >= 1.0:
            out = wet
        else:
            out = (1.0 - mix) * x + mix * wet

        out32 = out.astype(np.float32)
        return out32[0] if was_mono else out32

    def _bitcrush_decimate(self, module, x, N, jitter, v, frames):
        """Sample-and-hold decimation: hold every ~N-th input sample.

        Deliberately aliased -- there is no anti-image filter, because the
        folded-back content is the whole point of the sound. With
        ``jitter == 0`` the holds are perfectly periodic and located by
        integer division of the global sample index (``//N``); with
        jitter the hold length wobbles around ``N`` on a seeded stream and
        boundaries are located by ``searchsorted`` over their cumulative
        sum. Either way the value that spans a block boundary is carried
        per voice in ``self._state`` (with the global sample ``offset``),
        so the result is exactly block-size independent.
        """
        state = self._state.setdefault(module.id, {})
        offset = int(state.get("dec_offset", 0))
        held = state.get("dec_held")
        if held is None or held.shape[0] != v:
            held = np.zeros(v, dtype=np.float64)
            # a voice-count change invalidates any jitter boundary stream
            state.pop("dec_bounds", None)
            state.pop("dec_rng", None)

        g = offset + np.arange(frames)                     # global indices

        if jitter <= 0.0:
            src_global = (g // N) * N
        else:
            bounds = state.get("dec_bounds")
            rng = state.get("dec_rng")
            if bounds is None or rng is None:
                rng = np.random.default_rng(
                    self._BITCRUSHER_JITTER_SEED + int(module.id)
                )
                # first hold starts at the active boundary at/under offset
                bounds = [(offset // N) * N]
            last = bounds[-1]
            limit = offset + frames - 1
            while last <= limit:
                # hold length wobbles in [1, ~2N], symmetric around N
                step = int(round(N * (1.0 + jitter * (2.0 * rng.random() - 1.0))))
                if step < 1:
                    step = 1
                last += step
                bounds.append(last)
            barr = np.asarray(bounds, dtype=np.int64)
            idx = np.searchsorted(barr, g, side="right") - 1
            src_global = barr[idx]
            # prune boundaries below the one still in effect (bound memory)
            active = int(barr[idx[-1]])
            keep = int(np.searchsorted(barr, active, side="left"))
            state["dec_bounds"] = bounds[keep:]
            state["dec_rng"] = rng

        src_local = src_global - offset                    # <= frames - 1
        neg = src_local < 0
        gather = x[:, np.clip(src_local, 0, frames - 1)]   # (V, F)
        out = np.where(neg[None, :], held[:, None], gather)

        state["dec_offset"] = offset + frames
        state["dec_held"] = out[:, -1].copy()
        return out

    def _bitcrush_dcblock(self, module, wet, v):
        """One-pole DC blocker on the crushed signal.

        ``y[n] = x[n] - x[n-1] + R*y[n-1]`` with the pole ``R`` set from a
        ~20 Hz high-pass corner, stripping any offset the quantizer /
        decimator introduces. Per-voice ``x[n-1]`` / ``y[n-1]`` persist in
        ``self._state`` and the recurrence runs sample-serially, so it is
        exact and block-size independent (off by default -- the only
        per-sample loop here, paid for only when enabled).
        """
        sr = float(self.sample_rate)
        R = float(np.exp(-2.0 * np.pi * 20.0 / sr))        # ~20 Hz corner
        state = self._state.setdefault(module.id, {})
        xp = state.get("dc_xprev")
        yp = state.get("dc_yprev")
        if xp is None or xp.shape[0] != v:
            xp = np.zeros(v, dtype=np.float64)
            yp = np.zeros(v, dtype=np.float64)
        else:
            xp = xp.copy()
            yp = yp.copy()

        frames = wet.shape[1]
        out = np.empty_like(wet)
        for n in range(frames):
            xn = wet[:, n]
            yn = xn - xp + R * yp
            out[:, n] = yn
            xp = xn
            yp = yn

        state["dc_xprev"] = np.asarray(xp, dtype=np.float64).copy()
        state["dc_yprev"] = np.asarray(yp, dtype=np.float64).copy()
        return out

    def _render_ring_mod(self, module, frames: int, buffers, patch):
        """Ring modulator: ``out = in x carrier``.

        The carrier is an external audio cable on ``carrier`` when patched,
        otherwise an internal per-voice phase-accumulated sine at ``freq``
        (1 V/oct via ``freq_cv`` x ``freq_cv_depth``). Multiplying two
        signals keeps only their sum/difference frequencies -> the metallic,
        inharmonic bell/robot timbre.

        Shape-polymorphic: the ``(V, F)`` core runs with ``V == 1`` for a
        mono ``in``, so a single voice row is bit-identical to the mono
        render, and per-voice carrier phase keeps voices independent.
        ``mix`` <= 0 returns the input untouched (bit-exact dry, no phase
        advance), the same contract as chorus/distortion. The dry and
        external-carrier paths are exactly block-size independent; the
        internal sine integrates phase per sample (continuous across
        blocks) and matches across block sizes to within float phase-wrap
        rounding.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None or src.size == 0:
            return np.zeros(frames, dtype=np.float32)

        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)
        if mix <= 0.0:
            return src  # bit-exact dry bypass (no carrier, no phase advance)

        was_mono = src.ndim == 1
        x = np.atleast_2d(src).astype(np.float64)          # (V, F)
        v = x.shape[0]

        carrier = self._input_buffer(
            patch, buffers, module.id, "carrier", collapse=False
        )
        if carrier is not None and carrier.size:
            c = self._ring_match_voices(carrier, v, frames)     # external
        else:
            c = self._ring_internal_carrier(module, frames, v, patch, buffers)

        wet = x * c
        if mix >= 1.0:
            out = wet
        else:
            out = (1.0 - mix) * x + mix * wet

        out32 = out.astype(np.float32)
        return out32[0] if was_mono else out32

    @staticmethod
    def _ring_match_voices(buf, v, frames):
        """Coerce an input buffer to ``(v, frames)`` float64.

        A mono ``(F,)`` / single-row buffer broadcasts across voices; a
        buffer with ``v`` rows is used as-is; a mismatched voice count is
        summed to mono then broadcast (summing a modulator's voltages is
        the least-surprising fallback, mirroring the delay's ``time_cv``
        rule). The frame axis is trimmed / zero-padded to ``frames`` so an
        odd-length isolated render stays in bounds.
        """
        c = np.atleast_2d(np.asarray(buf, dtype=np.float64))
        if c.shape[0] == v:
            pass
        elif c.shape[0] == 1:
            c = np.broadcast_to(c, (v, c.shape[1]))
        else:
            c = np.broadcast_to(c.sum(axis=0, keepdims=True), (v, c.shape[1]))
        if c.shape[1] > frames:
            c = c[:, :frames]
        elif c.shape[1] < frames:
            c = np.pad(c, ((0, 0), (0, frames - c.shape[1])))
        return c

    def _ring_internal_carrier(self, module, frames, v, patch, buffers):
        """Per-voice phase-accumulated internal sine carrier, ``(v, F)``.

        Instantaneous frequency = ``freq`` x 2 ** (``freq_cv_depth`` x
        ``freq_cv``), integrated per sample (true 1 V/oct carrier FM) with
        per-voice phase persisted in ``self._state`` so a swept carrier
        stays continuous across blocks. Phase is an exclusive prefix sum,
        so the first sample of a fresh module sits at phase 0 (sin -> 0):
        a deterministic, testable starting waveform.
        """
        sr = self.sample_rate
        freq = min(max(float(module.params.get("freq", 440.0)), 1.0), 5000.0)
        depth = float(module.params.get("freq_cv_depth", 1.0))

        freq_cv = self._input_buffer(
            patch, buffers, module.id, "freq_cv", collapse=False
        )

        state = self._state.setdefault(module.id, {})
        ph0 = state.get("phase")
        if ph0 is None or ph0.shape[0] != v:
            ph0 = np.zeros(v, dtype=np.float64)

        if freq_cv is None or not getattr(freq_cv, "size", 0) or depth == 0.0:
            inc = np.full((v, frames), freq / sr, dtype=np.float64)
        else:
            cv = self._ring_match_voices(freq_cv, v, frames)
            inc = (freq * np.power(2.0, depth * cv)) / sr           # (v, F)

        csum = np.cumsum(inc, axis=1)                               # inclusive
        phases = (ph0[:, None] + csum - inc) % 1.0                  # exclusive
        state["phase"] = (ph0 + csum[:, -1]) % 1.0
        return np.sin(2.0 * np.pi * phases)

    # C4 (MIDI 60) in Hz — the fm_op carrier pitch at pitch_cv = 0 V (1 V/oct).
    _FM_REF_HZ = 261.6256

    def _render_fm_op(self, module, frames: int, buffers, patch):
        """One DX-style phase-modulation FM operator (a self-contained voice).

        Per sample: ``core = sin(2*pi*phase + index*pm + feedback*core_prev)``
        and ``out = amp_cv * core``. ``phase`` integrates the carrier
        frequency (C4 * 2**pitch_cv * ratio * 2**(fine/1200), or a fixed
        ``freq`` in ``fixed`` mode) per sample, exclusive-prefix so a fresh
        module starts at phase 0. ``index`` scales the audio-rate ``pm``
        input in *radians* (peak phase deviation for full-scale pm), boosted
        per-sample by ``index_cv`` * ``index_cv_depth`` (floored at 0).

        Shape-polymorphic: the ``(V, F)`` core runs with ``V == 1`` for all-
        mono inputs, so a single voice row is bit-identical to the mono
        render; per-voice phase and feedback state keep voices independent.
        ``V`` follows the widest voice-aware input (``pitch_cv`` / ``pm`` /
        ``amp_cv`` / ``index_cv``); a mono input broadcasts across voices.

        Dual engine (delay precedent): ``feedback == 0`` has no sample-to-
        sample dependency, so the whole block vectorizes; ``feedback > 0``
        needs the sequential per-sample recurrence (V-vectorized, F-looped).
        The two paths are bit-identical at ``feedback == 0`` (``0 * prev``
        adds nothing). Phase integrates continuously across blocks (state
        carried), so the output is block-size independent to within float
        phase-wrap rounding (< 1e-6), the ring_mod internal-sine contract.
        """
        p = module.params
        fine = float(p.get("fine", 0.0))
        index = float(p.get("index", 1.0))
        index_cv_depth = float(p.get("index_cv_depth", 1.0))
        feedback = min(max(float(p.get("feedback", 0.0)), 0.0), 1.0)
        fixed = bool(p.get("fixed", False))

        pitch_cv = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        pm = self._input_buffer(patch, buffers, module.id, "pm", collapse=False)
        amp_cv = self._input_buffer(
            patch, buffers, module.id, "amp_cv", collapse=False
        )
        index_cv = self._input_buffer(
            patch, buffers, module.id, "index_cv", collapse=False
        )

        # Voice count = widest voice-aware input; mono inputs broadcast.
        v = 1
        was_mono = True
        for buf in (pitch_cv, pm, amp_cv, index_cv):
            if buf is not None and getattr(buf, "ndim", 1) == 2:
                v = max(v, buf.shape[0])
                was_mono = False

        if frames == 0:
            z = np.zeros((v, 0), dtype=np.float32)
            return z[0] if was_mono else z

        sr = self.sample_rate

        # Carrier phase increment per sample, (v, F).
        if fixed:
            freq = float(p.get("freq", 220.0))
            inc = np.full((v, frames), freq / sr, dtype=np.float64)
        else:
            base = (
                self._FM_REF_HZ
                * _fm_snap_ratio(float(p.get("ratio", 1.0)))
                * (2.0 ** (fine / 1200.0))
            )
            if pitch_cv is None or not getattr(pitch_cv, "size", 0):
                inc = np.full((v, frames), base / sr, dtype=np.float64)
            else:
                cv = self._ring_match_voices(pitch_cv, v, frames)
                inc = (base * np.power(2.0, cv)) / sr

        state = self._state.setdefault(module.id, {})
        ph0 = state.get("phase")
        if ph0 is None or ph0.shape[0] != v:
            ph0 = np.zeros(v, dtype=np.float64)
        csum = np.cumsum(inc, axis=1)
        phase = (ph0[:, None] + csum - inc) % 1.0            # exclusive prefix
        state["phase"] = (ph0 + csum[:, -1]) % 1.0
        theta = 2.0 * np.pi * phase                          # (v, F) radians

        # Phase-modulation term: pm * effective index (radians).
        if pm is None or not getattr(pm, "size", 0):
            pm_arg = np.zeros((v, frames), dtype=np.float64)
        else:
            pm_v = self._ring_match_voices(pm, v, frames)
            if (
                index_cv is None
                or not getattr(index_cv, "size", 0)
                or index_cv_depth == 0.0
            ):
                eff_index = index
            else:
                icv = self._ring_match_voices(index_cv, v, frames)
                eff_index = np.maximum(index + index_cv_depth * icv, 0.0)
            pm_arg = eff_index * pm_v

        arg = theta + pm_arg                                 # everything but fb

        fb_prev = state.get("fb")
        if fb_prev is None or fb_prev.shape[0] != v:
            fb_prev = np.zeros(v, dtype=np.float64)

        if feedback <= 0.0:
            core = np.sin(arg)
        else:
            core = np.empty((v, frames), dtype=np.float64)
            prev = fb_prev
            for n in range(frames):
                prev = np.sin(arg[:, n] + feedback * prev)
                core[:, n] = prev
        # Persist the last output sample so feedback (if enabled later, or
        # this block) continues seamlessly across block boundaries.
        state["fb"] = core[:, -1].copy()

        if amp_cv is None or not getattr(amp_cv, "size", 0):
            out = core
        else:
            out = core * self._ring_match_voices(amp_cv, v, frames)

        out32 = out.astype(np.float32)
        return out32[0] if was_mono else out32

    def _render_freq_shifter(self, module, frames: int, buffers, patch):
        """Bode single-sideband frequency shifter -> up/down sidebands.

        The input is split into an analytic (quadrature) pair by a 255-tap
        Type-III FIR Hilbert transformer (group delay ``_FS_LATENCY`` = 127
        samples, ~2.9 ms) and rotated by a complex sine at the shift
        frequency. The two real projections of that rotation are the two
        sidebands: ``out_up`` moves every partial *up* by ``shift`` Hz,
        ``out_down`` *down* by the same amount (the conjugate sideband).
        Because the shift is an addition of hertz, not a ratio, harmonic
        input becomes inharmonic -- the metallic/barberpole character.

        Shape-polymorphic: the ``(V, F)`` core runs with ``V == 1`` for a
        mono ``in``, so a single voice row is bit-identical to the mono
        render; per-voice Hilbert / delay-line / carrier-phase state keeps
        voices independent. ``mix`` <= 0 returns the input untouched on
        both outputs (bit-exact dry, no latency, no state advance) -- the
        chorus/ring_mod contract. Otherwise the wet is 127 samples late and
        the dry is delay-matched, so at ``shift == 0`` the wet *is* the
        delayed dry and the blend is transparent.

        Processed in fixed ``_FS_LATENCY``-sample chunks so the ``feedback``
        recirculation of ``out_up`` only ever reads already-computed output:
        the recurrence is causal and boundary-independent, which makes the
        result block-size independent (bit-exact after the float32 cast, the
        FIR streamed via ``lfilter`` with carried ``zi``). ``feedback`` is
        clamped to 0.9 for a bounded loop.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        z = np.zeros(frames, dtype=np.float32)
        if src is None or src.size == 0:
            return {"out_up": z, "out_down": z.copy()}

        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)
        if mix <= 0.0:
            return {"out_up": src, "out_down": src}  # bit-exact dry bypass

        was_mono = src.ndim == 1
        x = np.atleast_2d(src).astype(np.float64)              # (V, F)
        v, F = x.shape
        if F == 0:
            return {"out_up": z, "out_down": z.copy()}

        sr = self.sample_rate
        shift = min(max(float(module.params.get("shift", 0.0)), -2000.0), 2000.0)
        depth = float(module.params.get("shift_cv_depth", 200.0))
        feedback = min(max(float(module.params.get("feedback", 0.0)), 0.0), 0.9)

        # Instantaneous shift in Hz, (V, F). ``shift_cv`` is a LINEAR Hz
        # control (a shift is an addition, not a 1 V/oct ratio), scaled by
        # ``shift_cv_depth``. Clamp the total below Nyquist so a hot CV
        # can't drive the rotation into gross aliasing.
        cv = self._input_buffer(patch, buffers, module.id, "shift_cv", collapse=False)
        if cv is not None and getattr(cv, "size", 0) and depth != 0.0:
            c = self._ring_match_voices(cv, v, F)
            shift_hz = shift + depth * c
        else:
            shift_hz = np.full((v, F), shift, dtype=np.float64)
        nyq = 0.5 * sr
        np.clip(shift_hz, -nyq, nyq, out=shift_hz)
        omega = (2.0 * np.pi / sr) * shift_hz                  # rad/sample

        h = _FS_HILBERT
        L = _FS_LATENCY
        state = self._state.setdefault(module.id, {})
        if state.get("v") != v:
            state.clear()
            state["v"] = v
            state["zi"] = np.zeros((v, len(h) - 1), dtype=np.float64)
            state["real"] = np.zeros((v, L), dtype=np.float64)   # delayed x_in
            state["dry"] = np.zeros((v, L), dtype=np.float64)    # delayed orig in
            state["fb"] = np.zeros((v, L), dtype=np.float64)     # last L out_up
            state["phase"] = np.zeros(v, dtype=np.float64)       # carrier phase

        zi = state["zi"]
        real = state["real"]
        dry_line = state["dry"]
        fb_line = state["fb"]
        phase = state["phase"]

        up = np.empty((v, F), dtype=np.float64)
        down = np.empty((v, F), dtype=np.float64)
        drybuf = np.empty((v, F), dtype=np.float64)
        j = 0
        while j < F:
            cl = min(L, F - j)
            in_c = x[:, j:j + cl]
            om_c = omega[:, j:j + cl]
            # cl <= L, so out_up[n-L] for this chunk sits entirely in
            # fb_line -- the loop only reads already-computed output.
            if feedback != 0.0:
                x_in = in_c + feedback * fb_line[:, :cl]
            else:
                x_in = in_c
            # analytic pair: streamed Hilbert + the L-delayed real of x_in.
            x_h, zi = lfilter(h, [1.0], x_in, axis=-1, zi=zi)
            rbuf = np.concatenate([real, x_in], axis=-1)
            x_d = rbuf[:, :cl]
            real = rbuf[:, cl:]
            # carrier phase, exclusive prefix so a fresh module starts at 0.
            cumo = np.cumsum(om_c, axis=-1)
            ph = phase[:, None] + cumo - om_c
            cph = np.cos(ph)
            sph = np.sin(ph)
            phase = (phase + cumo[:, -1]) % (2.0 * np.pi)
            up_c = x_d * cph - x_h * sph
            down_c = x_d * cph + x_h * sph
            up[:, j:j + cl] = up_c
            down[:, j:j + cl] = down_c
            # advance the dry (original ``in``) delay line for the mix.
            dbuf = np.concatenate([dry_line, in_c], axis=-1)
            drybuf[:, j:j + cl] = dbuf[:, :cl]
            dry_line = dbuf[:, cl:]
            if feedback != 0.0:
                fb_line = np.concatenate([fb_line, up_c], axis=-1)[:, -L:]
            j += cl

        state["zi"] = zi
        state["real"] = real
        state["dry"] = dry_line
        state["fb"] = fb_line
        state["phase"] = phase

        if mix >= 1.0:
            out_up, out_down = up, down
        else:
            out_up = (1.0 - mix) * drybuf + mix * up
            out_down = (1.0 - mix) * drybuf + mix * down

        up32 = out_up.astype(np.float32)
        down32 = out_down.astype(np.float32)
        if was_mono:
            return {"out_up": up32[0], "out_down": down32[0]}
        return {"out_up": up32, "out_down": down32}

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

    # ----- Tape rendering --------------------------------------------------

    # Fixed nominal delay of the tape head gap (ms). Wow/flutter/drift sway
    # the *read* around this centre; the dry path is delayed by the same
    # amount (plus any oversampler latency) so ``mix`` stays phase-coherent
    # instead of combing.
    _TAPE_NOMINAL_MS = 10.0
    # Longest delay the line can address (ms) -> sizes the ring and caps the
    # modulated read (nominal + the summed wow/flutter/drift throw, with room).
    _TAPE_MAX_MS = 24.0
    _TAPE_MIN_SAMP = 2.0            # read floor: both interp taps stay behind write
    # Peak modulation throw (ms) at full depth, per source.
    _TAPE_WOW_MS = 2.5
    _TAPE_FLUT_MS = 0.5
    _TAPE_DRIFT_MS = 3.0
    # Nominal modulation rates (Hz).
    _TAPE_WOW_HZ = 1.0
    _TAPE_FLUT_HZ = 9.0
    _TAPE_DRIFT_HZ = 0.35          # one-pole corner of the drift random-walk
    _TAPE_FLUT_LP_HZ = 18.0        # one-pole corner shaping the flutter noise
    _TAPE_FLUT_NOISE = 0.35        # noise fraction of the flutter modulation
    # Saturation drive at ``sat == 1`` (tanh on the shared 4x OS path).
    _TAPE_SAT_DRIVE_MAX = 6.0
    # Head-bump low shelf.
    _TAPE_BUMP_HZ = 60.0
    _TAPE_BUMP_MAX_DB = 6.0
    # ``hiss`` (dB): at/below OFF it is disabled; otherwise up to MAX.
    _TAPE_HISS_OFF_DB = -80.0
    _TAPE_HISS_MAX_DB = -30.0
    # Seeded, reproducible noise streams (offset by module.id). drift, flutter
    # and hiss each get their OWN generator, so every stream is an independent
    # 1:1 draw per output sample -> exactly block-size independent.
    _TAPE_DRIFT_SEED = 0x7A9E0
    _TAPE_FLUT_SEED = 0x7A9E1
    _TAPE_HISS_SEED = 0x7A9E2

    def _render_tape(self, module, frames: int, buffers, patch):
        """Tape character: wow/flutter/drift + saturation + hiss + head bump.

        Signal flow ``in -> wow/flutter/drift-modulated fractional delay ->
        saturation -> + hiss -> head-bump low shelf -> mix with the
        latency-matched dry``. The delay line reuses the chorus core (write
        the whole block, then read fractional taps at ``absidx - delay``);
        with no feedback every read references an already-written sample, so
        the whole render vectorises and is exactly block-size independent.
        The wow/flutter sines carry their phase in state; the drift, flutter
        noise and hiss are each a *single* seeded generator drawn one sample
        per output sample and streamed through one-pole/biquad filters with
        carried ``zi`` -- so every stochastic path is block-size independent
        too. One tape path is modelled: the modulation and hiss are shared
        across a polyphonic input's voices (each voice keeps its own delay
        line, oversampler and shelf state, so they never cross-talk), and a
        single voice row is bit-identical to the mono render.

        Neutral (``wow = flutter = drift = sat = bump = 0`` and ``hiss``
        off) short-circuits to a bit-exact passthrough with no state
        advance; ``mix <= 0`` is likewise bit-exact dry.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None or src.size == 0:
            return np.zeros(frames, dtype=np.float32)

        wow = min(max(float(module.params.get("wow", 0.0)), 0.0), 1.0)
        flutter = min(max(float(module.params.get("flutter", 0.0)), 0.0), 1.0)
        drift = min(max(float(module.params.get("drift", 0.0)), 0.0), 1.0)
        sat = min(max(float(module.params.get("sat", 0.0)), 0.0), 1.0)
        hiss_db = float(module.params.get("hiss", self._TAPE_HISS_OFF_DB))
        bump_db = min(max(float(module.params.get("bump", 0.0)), 0.0),
                      self._TAPE_BUMP_MAX_DB)
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)

        sat_active = sat > 0.0
        hiss_active = hiss_db > self._TAPE_HISS_OFF_DB
        mod_active = wow > 0.0 or flutter > 0.0 or drift > 0.0
        bump_active = bump_db > 0.0

        # Bit-exact dry: the blend keeps nothing wet, or the whole box is
        # neutral (a freshly added Tape is transparent). No state advance.
        if mix <= 0.0:
            return src
        if not (mod_active or sat_active or hiss_active or bump_active):
            return src

        was_mono = src.ndim == 1
        x = np.atleast_2d(src).astype(np.float64)          # (V, F)
        v = x.shape[0]
        sr = self.sample_rate

        D = int(round(self._TAPE_NOMINAL_MS * sr / 1000.0))
        L = int(self._TAPE_MAX_MS * sr / 1000.0) + frames + 4
        comp = D + (_OS_LATENCY if sat_active else 0)      # dry latency comp

        state = self._state.setdefault(module.id, {})
        buf = state.get("buf")
        if (buf is None or buf.shape != (v, L) or state.get("comp") != comp):
            state.clear()
            state["buf"] = np.zeros((v, L), dtype=np.float64)
            state["write_idx"] = 0
            state["wow_ph"] = 0.0
            state["flut_ph"] = 0.0
            state["flut_zi"] = np.zeros(1)
            state["drift_zi"] = np.zeros(1)
            state["shelf"] = {}
            state["dry_tail"] = np.zeros((v, comp))
            state["comp"] = comp
            state["os4"] = _Oversampler4(v)
            mid = int(module.id)
            state["rng_drift"] = np.random.default_rng(self._TAPE_DRIFT_SEED + mid)
            state["rng_flut"] = np.random.default_rng(self._TAPE_FLUT_SEED + mid)
            state["rng_hiss"] = np.random.default_rng(self._TAPE_HISS_SEED + mid)

        if frames == 0:
            e = np.empty((v, 0), dtype=np.float32)
            return e[0] if was_mono else e

        buf = state["buf"]
        wp = int(state["write_idx"])
        n = np.arange(frames, dtype=np.float64)

        # --- modulation (shared across voices: one tape path) -------------
        # wow: slow sine.
        wow_inc = self._TAPE_WOW_HZ / sr
        wow_lfo = np.sin(2.0 * np.pi * (state["wow_ph"] + n * wow_inc))
        state["wow_ph"] = float((state["wow_ph"] + frames * wow_inc) % 1.0)

        # flutter: fast sine + a little low-passed (band-limited) noise.
        flut_inc = self._TAPE_FLUT_HZ / sr
        flut_lfo = np.sin(2.0 * np.pi * (state["flut_ph"] + n * flut_inc))
        state["flut_ph"] = float((state["flut_ph"] + frames * flut_inc) % 1.0)
        fn = state["rng_flut"].standard_normal(frames)
        kf = 1.0 - math.exp(-2.0 * math.pi * self._TAPE_FLUT_LP_HZ / sr)
        fn, state["flut_zi"] = lfilter([kf], [1.0, kf - 1.0], fn, zi=state["flut_zi"])
        fn = fn * math.sqrt((2.0 - kf) / kf)               # -> ~unit std
        flut_sig = ((1.0 - self._TAPE_FLUT_NOISE) * flut_lfo
                    + self._TAPE_FLUT_NOISE * fn)

        # drift: slow random walk = heavily low-passed white noise, unit-ish
        # std, hard-bounded so the read can never cross the write head.
        dn = state["rng_drift"].standard_normal(frames)
        kd = 1.0 - math.exp(-2.0 * math.pi * self._TAPE_DRIFT_HZ / sr)
        dn, state["drift_zi"] = lfilter([kd], [1.0, kd - 1.0], dn, zi=state["drift_zi"])
        drift_sig = np.clip(dn * math.sqrt((2.0 - kd) / kd), -1.0, 1.0)

        wow_s = self._TAPE_WOW_MS * sr / 1000.0
        flut_s = self._TAPE_FLUT_MS * sr / 1000.0
        drift_s = self._TAPE_DRIFT_MS * sr / 1000.0
        m = (wow * wow_s * wow_lfo
             + flutter * flut_s * flut_sig
             + drift * drift_s * drift_sig)                # (F,) samples

        delay = D + m
        np.clip(delay, self._TAPE_MIN_SAMP, float(L - 2), out=delay)

        # --- fractional-delay read (chorus core; no feedback) -------------
        absidx = wp + np.arange(frames)
        buf[:, absidx % L] = x
        rp = absidx - delay                                # (F,)
        i0 = np.floor(rp).astype(np.int64)
        frac = rp - i0
        tap = (buf[:, i0 % L] * (1.0 - frac)
               + buf[:, (i0 + 1) % L] * frac)              # (V, F)
        state["write_idx"] = int((wp + frames) % L)

        wet = tap
        # --- saturation (4x-oversampled tanh) -----------------------------
        if sat_active:
            drive = sat * self._TAPE_SAT_DRIVE_MAX
            os4 = state["os4"]
            wet = os4.down(self._dist_curve("soft", drive, os4.up(wet)))

        # --- hiss (calibrated noise floor, lives in the wet path) ---------
        if hiss_active:
            amp = 10.0 ** (min(hiss_db, self._TAPE_HISS_MAX_DB) / 20.0)
            hn = state["rng_hiss"].standard_normal(frames) * amp
            wet = wet + hn[None, :]

        # --- head-bump low shelf (~60 Hz), streaming per voice ------------
        if bump_active:
            b0, b1, b2, a1n, a2n = self._loud_shelf(
                self._TAPE_BUMP_HZ, bump_db, True
            )
            sh = state["shelf"]
            if sh.get("zi") is None or sh["zi"].shape[0] != v or sh.get("g") != bump_db:
                sh["zi"] = np.zeros((v, 2))
                sh["g"] = bump_db
            wet, sh["zi"] = lfilter(
                [b0, b1, b2], [1.0, a1n, a2n], wet, axis=-1, zi=sh["zi"]
            )

        # --- mix against the latency-matched dry (tail always advances) ---
        both = np.concatenate([state["dry_tail"], x], axis=-1)
        dry = both[:, :frames]
        state["dry_tail"] = both[:, frames:]
        out = wet if mix >= 1.0 else (1.0 - mix) * dry + mix * wet

        out32 = out.astype(np.float32)
        return out32[0] if was_mono else out32

    def _render_convolver(self, module, frames: int, buffers, patch):
        """Partitioned-FFT convolution (IR reverb / cab): mono-in, stereo out.

        Voices are summed to mono before convolving -- convolution is linear,
        so per-voice-then-sum equals sum-then-convolve, and the mono sum is
        far cheaper (one FFT stream, not V). A stereo IR convolves that mono
        input through its left channel into ``out_l`` and its right into
        ``out_r`` (a mono IR drives both, and is convolved once); the
        decorrelation in the IR is the stereo image.

        The IR is decoded + partition-built on a background thread
        (``_IRLoader``), kicked at compile() and on any live ``path`` edit, so
        a new/changed IR never blocks the audio thread -- the convolver keeps
        the previous IR (or a transparent unit impulse) sounding until the new
        one is ready. The wet path carries a fixed one-block latency; the dry
        path is delay-matched by the same block inside ``mix`` so dry and wet
        stay phase-coherent. ``gain`` trims the wet only, so ``mix = 0`` is a
        bit-exact dry bypass (FFT skipped) whatever ``gain`` is.
        """
        state = self._state.setdefault(module.id, self._new_convolver_state())

        # --- resolve path -> active engines (never blocks the audio thread) ---
        path = str(module.params.get("path", ""))
        if path == "":
            # Transparent insert: drop any IR + cancel a pending load.
            if state.get("ir_l") is not None or state.get("loaded_path") is not None:
                state["ir_l"] = state["ir_r"] = None
                state["engine_l"] = state["engine_r"] = None
                state["loaded_path"] = None
            pend = state.get("pending")
            if pend is not None:
                if pend.get("loader") is not None:
                    pend["loader"].close()
                state["pending"] = None
        else:
            pend = state.get("pending")
            if path != state.get("loaded_path") and (
                pend is None or pend.get("path") != path
            ):
                # New/changed IR: kick a background load, keep current engines
                # (previous IR or transparent) sounding until it is ready.
                state["pending"] = {
                    "path": path, "loader": self._start_ir_loader(path, frames)
                }

        # Adopt a finished load (ready -> new engines; failed -> keep current).
        pend = state.get("pending")
        if pend is not None:
            loader = pend.get("loader")
            if loader is None or loader.done:
                if loader is not None and loader.ready:
                    state["ir_l"] = loader.ir_l
                    state["ir_r"] = loader.ir_r
                    state["engine_l"] = loader.engine_l
                    state["engine_r"] = loader.engine_r
                    state["block"] = loader.block
                    state["loaded_path"] = pend["path"]
                state["pending"] = None

        # --- ensure engines exist and match the current block size ---
        if state.get("engine_l") is None or state.get("block") != frames:
            ir_l = state.get("ir_l")
            if ir_l is None:  # transparent: shared unit-impulse engine
                imp = _PartitionedConvolver(np.array([1.0], dtype=np.float64), frames)
                state["engine_l"] = imp
                state["engine_r"] = imp
            else:
                ir_r = state.get("ir_r")
                state["engine_l"] = _PartitionedConvolver(ir_l, frames)
                if ir_r is None or np.array_equal(ir_l, ir_r):
                    state["engine_r"] = state["engine_l"]
                else:
                    state["engine_r"] = _PartitionedConvolver(ir_r, frames)
            state["block"] = frames
            state["dry_prev"] = np.zeros(frames, dtype=np.float32)

        # --- mono dry (collapse=True sums any voice rows: conv is linear) ---
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            x = np.zeros(frames, dtype=np.float32)
        else:
            x = np.asarray(src, dtype=np.float32).reshape(-1)
            if x.shape[0] < frames:  # defensive; buffers are frames-long
                x = np.concatenate(
                    [x, np.zeros(frames - x.shape[0], dtype=np.float32)]
                )
            elif x.shape[0] > frames:
                x = x[:frames]

        # dry delayed by one block (== engine latency) to align with the wet
        dry_prev = state.get("dry_prev")
        if dry_prev is None or dry_prev.shape[0] != frames:
            dry_prev = np.zeros(frames, dtype=np.float32)
        dry_delayed = dry_prev
        state["dry_prev"] = x

        mix = float(module.params.get("mix", 1.0))
        # Neutral bypass: mix <= 0 -> bit-exact delayed dry, FFT skipped.
        if mix <= 0.0:
            out = np.array(dry_delayed, dtype=np.float32, copy=True)
            return {"out_l": out, "out_r": np.array(out, copy=True)}

        gain = float(module.params.get("gain", 1.0))
        tone_hz = min(max(float(module.params.get("tone", _CONV_TONE_MAX)),
                          _CONV_TONE_MIN), _CONV_TONE_MAX)
        predelay_ms = min(max(float(module.params.get("predelay", 0.0)), 0.0),
                          _CONV_PREDELAY_MAX_MS)
        pd = int(round(predelay_ms * 1e-3 * self.sample_rate))

        eng_l = state["engine_l"]
        eng_r = state["engine_r"]
        shared = eng_r is eng_l  # mono IR -> convolve once (the FFTs are shared)
        wet_l = eng_l.process(x).astype(np.float32)
        wet_r = wet_l if shared else eng_r.process(x).astype(np.float32)
        # Per-channel wet shaping (tone low-pass -> predelay), then wet gain.
        wet_l = self._shape_conv_wet(wet_l, state, "l", tone_hz, pd, frames)
        wet_r = self._shape_conv_wet(wet_r, state, "r", tone_hz, pd, frames)
        if gain != 1.0:
            g = np.float32(gain)
            wet_l = wet_l * g
            wet_r = wet_r * g

        if mix >= 1.0:
            out_l = np.ascontiguousarray(wet_l, dtype=np.float32)
            out_r = np.ascontiguousarray(wet_r, dtype=np.float32)
        else:
            m = np.float32(mix)
            dm = np.float32(1.0 - mix)
            out_l = (m * wet_l + dm * dry_delayed).astype(np.float32)
            out_r = (m * wet_r + dm * dry_delayed).astype(np.float32)
        if out_r is out_l:  # distinct arrays for the two ports
            out_r = np.array(out_l, copy=True)
        return {"out_l": out_l, "out_r": out_r}

    @staticmethod
    def _new_convolver_state():
        return {
            "engine_l": None, "engine_r": None, "ir_l": None, "ir_r": None,
            "loaded_path": None, "pending": None, "block": None,
            "dry_prev": None,
            "tone_zi_l": None, "tone_zi_r": None,
            "pd_buf_l": None, "pd_buf_r": None,
        }

    def _shape_conv_wet(self, wet, state, ch, tone_hz, predelay, frames):
        """Shape one wet channel: tone low-pass (off at max) then predelay.

        Both are wet-only and cheap, so they run per channel even when the
        convolution engine is shared (a mono IR) -- the mirrored per-channel
        state (``tone_zi_<ch>`` / ``pd_buf_<ch>``) keeps the two identical. The
        tone one-pole carries its state across blocks; predelay is a
        per-channel FIFO of ``predelay`` samples, an intentional wet delay on
        top of the module's one-block latency so the reverb starts behind the
        dry.
        """
        if tone_hz < _CONV_TONE_MAX:
            a = 1.0 - float(np.exp(-2.0 * np.pi * tone_hz / self.sample_rate))
            zi = state.get("tone_zi_" + ch)
            if zi is None:
                zi = np.zeros(1, dtype=np.float64)
            filtered, zi = lfilter(
                [a], [1.0, -(1.0 - a)], np.asarray(wet, dtype=np.float64), zi=zi
            )
            state["tone_zi_" + ch] = zi
            wet = filtered
        if predelay > 0:
            buf = state.get("pd_buf_" + ch)
            if buf is None or buf.shape[0] != predelay:
                buf = np.zeros(predelay, dtype=np.float64)
            combined = np.concatenate([buf, np.asarray(wet, dtype=np.float64)])
            wet = combined[:frames]
            state["pd_buf_" + ch] = combined[frames:]
        return np.asarray(wet, dtype=np.float32)

    def _start_ir_loader(self, path, block):
        """Spawn a background IR decode+build for ``path`` (None if empty)."""
        if not path:
            return None
        return _IRLoader(path, self.sample_rate, block, self._decode_audio)

    def wait_for_ir_loads(self, timeout: float = 10.0) -> bool:
        """Block until every convolver's pending IR load finishes.

        Tests / offline render only -- never call from the audio thread.
        Returns True when every pending loader finished with a usable IR
        (no pending load counts as trivially ready).
        """
        import time as _time

        deadline = _time.monotonic() + float(timeout)
        ok = True
        for st in list(self._state.values()):
            if not isinstance(st, dict):
                continue
            pend = st.get("pending")
            loader = pend.get("loader") if isinstance(pend, dict) else None
            if loader is None:
                continue
            remaining = max(0.0, deadline - _time.monotonic())
            ok = bool(loader.wait(remaining)) and ok
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
