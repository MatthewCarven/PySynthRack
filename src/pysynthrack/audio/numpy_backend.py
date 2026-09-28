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
Keyboard / MIDIInput note sources) route through. The Oscillator alone
also hands it a pulse width for ``square`` / ``square_blep`` (per-sample
PWM, DC-compensated, the falling edge's blep riding its own phase);
``square_wt`` is a fixed 50% table by design.
"""
from __future__ import annotations

import math
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, firwin, lfilter, resample_poly, sosfilt

from .._resources import resource_root
from ..core.patch import Patch
from . import media
from .backend import AudioBackend
from .renderers import (
    ClockworkRenderers,
    ColourRenderers,
    DynamicsRenderers,
    EQFilterRenderers,
    IORenderers,
    ModFXRenderers,
    ModSourceRenderers,
    OscillatorRenderers,
    PitchTimeRenderers,
    ReverbDelayRenderers,
    SequencingRenderers,
    SpectralRenderers,
)

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
    voice across blocks as the raw TAIL SAMPLES, prepended to the next
    block, not as an ``lfilter`` ``zi``: scipy short-circuits ``len(a)
    == 1`` to ``np.convolve`` plus a separate ``+= zi``, which splits
    each output's 65-term sum at the block boundary and so associates
    the additions differently for every block size (~1e-15, measured;
    enough to flip a float32 tie a couple of times in 4 s once the head
    bump's IIR carries it along). Prepending the tail gives every output
    one whole, unsplit window -- identical at any block size, and the
    same cost. Because the block length is decimated as ``[..., ::4]``
    and 4*F is always divisible by 4, the decimation phase is identical
    for every block size too.
    """

    def __init__(self, voices: int):
        self._hist_up = np.zeros((voices, _OS_TAPS - 1))
        self._hist_dn = np.zeros((voices, _OS_TAPS - 1))

    @property
    def voices(self) -> int:
        return self._hist_up.shape[0]

    @staticmethod
    def _fir(x, hist):
        """One streaming FIR pass: (V, N) in, (V, N) out + the new tail."""
        t = _OS_TAPS - 1
        ext = np.concatenate([hist, x], axis=-1)
        return lfilter(_OS_FIR, [1.0], ext, axis=-1)[:, t:], ext[:, -t:].copy()

    def up(self, x):
        """(V, F) base-rate -> (V, 4F) oversampled."""
        v, f = x.shape
        stuffed = np.zeros((v, f * _OS_FACTOR))
        stuffed[:, ::_OS_FACTOR] = x * _OS_FACTOR
        y, self._hist_up = self._fir(stuffed, self._hist_up)
        return y

    def down(self, y):
        """(V, 4F) oversampled -> (V, F) base-rate."""
        z, self._hist_dn = self._fir(y, self._hist_dn)
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


def _pluck_exciter(n: int, color: float, position: float, rng) -> np.ndarray:
    """One seeded pluck burst: shaped noise, peak-normalized, length ``n``.

    ``color`` blends the burst's spectrum from lowpassed (soft thumb) to
    raw white noise (hard plectrum) via a one-pole whose coefficient
    rises with color². ``position`` applies the pick-position comb — the
    burst minus itself delayed ``position·n`` samples, which notches the
    harmonics a pluck at that point along the string cancels (0 = off).
    Peak-normalizing last keeps the pluck level independent of the
    shaping. Deterministic per ``rng`` — the caller seeds per hit.
    """
    e = rng.uniform(-1.0, 1.0, n)
    if color < 1.0:
        a = 0.04 + 0.96 * color * color
        e = lfilter([a], [1.0, -(1.0 - a)], e)
    if position > 0.0:
        d = int(round(position * n))
        if 1 <= d < n:
            delayed = np.zeros(n)
            delayed[d:] = e[:-d]
            e = e - delayed
    # Zero-mean BEFORE normalizing: the loop recirculates DC almost
    # undamped (damping and allpass are both unity at DC), so a random
    # burst mean would ring as a slowly-decaying pedestal that dwarfs
    # the string's fundamental. Classic KS gotcha.
    e = e - float(np.mean(e))
    peak = float(np.max(np.abs(e)))
    if peak > 0.0:
        e = e / peak
    return e


def _kick_hit(sr, freq_start, freq_end, bend_s, decay_s, click, drive, tune, rng,
              vel=1.0):
    """One complete kick hit, synthesized closed-form (deterministic).

    Phase is the exact integral of the exponential pitch dive
    ``f(t) = fe + (fs − fe)·e^(−t/τ)``, so the trajectory is analytic
    (testable) and DC-free by construction (a sine of a smooth phase).
    ``click`` adds a 2 ms zero-meaned noise transient; ``drive`` is a
    normalized tanh (no oversampling — the kick is LF-dominant). ``vel``
    scales the hit BEFORE the drive, so a soft hit stays clean and a hard
    one saturates; at 1.0 it is not applied at all, keeping the pinned
    closed-form render exact.
    """
    k = 2.0 ** (tune / 12.0)
    fs, fe = freq_start * k, freq_end * k
    tau = max(1e-4, bend_s)
    length = max(16, int(sr * (decay_s * 80.0 / 60.0 + 4.0 * tau)))
    t = np.arange(length) / sr
    phase = 2.0 * np.pi * (fe * t + (fs - fe) * tau * (1.0 - np.exp(-t / tau)))
    env = np.exp(-t * (3.0 * np.log(10.0)) / max(1e-3, decay_s))
    body = np.sin(phase) * env
    if click > 0.0:
        n = max(2, int(0.002 * sr))
        burst = rng.uniform(-1.0, 1.0, n)
        burst -= burst.mean()
        body[:n] += click * 0.8 * burst
    if vel != 1.0:
        body *= vel
    if drive > 0.0:
        g = 1.0 + 6.0 * drive
        body = np.tanh(g * body) / np.tanh(g)
    return body


def _snare_hit(sr, tone_decay_s, noise_decay_s, snappy, tune, rng):
    """One complete snare hit: two head modes + band-passed wire noise."""
    k = 2.0 ** (tune / 12.0)
    length = max(16, int(sr * max(tone_decay_s, noise_decay_s) * 80.0 / 60.0))
    t = np.arange(length) / sr
    ln1000 = 3.0 * np.log(10.0)
    tone = (
        0.6 * np.sin(2.0 * np.pi * 185.0 * k * t)
        + 0.4 * np.sin(2.0 * np.pi * 330.0 * k * t)
    ) * np.exp(-t * ln1000 / max(1e-3, tone_decay_s))
    noise = rng.uniform(-1.0, 1.0, length)
    hi = min(8000.0, 0.45 * sr)
    lo = min(800.0, 0.5 * hi)
    sos = butter(2, [lo / (sr / 2.0), hi / (sr / 2.0)], btype="band", output="sos")
    noise = sosfilt(sos, noise)
    peak = float(np.max(np.abs(noise)))
    if peak > 0.0:
        noise = noise / peak
    noise = noise * np.exp(-t * ln1000 / max(1e-3, noise_decay_s))
    return (1.0 - snappy) * tone + snappy * noise


def _hat_hit(sr, decay_s, tune, tone=400.0):
    """One complete hat hit: six detuned squares, high-passed, enveloped.

    Deterministic with no rng — the metallic stack IS the noise. The
    squares alias mildly; hats are noise-like, so it reads as character.
    ``tone`` is the stack's base frequency (400 Hz is the classic);
    ``tune`` shifts it in semitones on top.
    """
    k = 2.0 ** (tune / 12.0)
    base = float(tone) * k
    ratios = (1.0, 1.342, 1.523, 1.782, 2.011, 2.312)
    length = max(16, int(sr * decay_s * 80.0 / 60.0))
    t = np.arange(length) / sr
    sq = np.zeros(length)
    for r_ in ratios:
        sq += np.sign(np.sin(2.0 * np.pi * base * r_ * t))
    cutoff = min(7000.0, 0.4 * sr)
    sos = butter(4, cutoff / (sr / 2.0), btype="high", output="sos")
    sq = sosfilt(sos, sq)
    peak = float(np.max(np.abs(sq)))
    if peak > 0.0:
        sq = sq / peak
    return sq * np.exp(-t * (3.0 * np.log(10.0)) / max(1e-3, decay_s))


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


class _SampleLoader:
    """Background whole-file decode for the Sampler.

    The ``_IRLoader`` pattern, minus the FFT build: a daemon worker decodes
    ``path`` through ``decode_fn`` (the backend's ``_decode_audio`` — WAV
    fast path, then ffmpeg), sums it to mono and hands over a contiguous
    ``(N,)`` float64 buffer. The consumer polls ``done`` then reads
    ``ready`` / ``failed`` / ``samples``; every field is a plain attribute
    (atomic under the GIL), so the audio thread needs no lock and never
    waits on a decode.

    Whole-load rather than streaming (the FilePlayer's approach) because a
    sampler needs random access: sixteen voices may be reading sixteen
    different places at sixteen different rates.

    The mono sum is written as ``0.5 * (l + r)`` deliberately. A mono file
    decodes to two identical rows, and halving their sum is *exact* in
    IEEE arithmetic — so a mono source survives the load bit-for-bit,
    which is what lets the module's unity read be bit-exact rather than
    merely close.

    Slice 3 adds three things, all built here so the audio thread never
    pays for them:

    * ``chains`` — the read banks. One chain per channel the renderer
      reads: ``[mono]`` when the file's two rows are identical (every mono
      file, and any stereo file that happens to be dual-mono), else
      ``[left, right]``. Each chain is a list of mip levels: level 0 is
      the float64 original, level ``k`` is level ``k-1`` half-band
      lowpassed (``firwin``, Blackman, ``halfband_taps`` taps, centred)
      and decimated 2:1, kept as float32 — it is a filtered copy and makes
      no exactness claims, so it need not cost double. Level ``k`` sample
      ``j`` sits exactly on original position ``j * 2**k``, which is what
      lets a read at rate ``2**k`` be that level verbatim. The chain stops
      at ``mip_levels`` or when a level would drop below ``mip_min``
      samples. Memory for the chain is under half the original again
      (``1/2 + 1/4 + ...`` at half the width).
    * ``stereo`` — True when ``chains`` holds two channels.
    * ``overview`` — ``(cols, 2)`` float32 column min/max of the mono, for
      the node's waveform face (the scope's envelope-polyline shape).
    """

    def __init__(self, path, target_sr, decode_fn, max_seconds,
                 mip_levels=0, mip_min=16, halfband_taps=63,
                 overview_cols=200) -> None:
        self.path = str(path)
        self.target_sr = int(target_sr)
        self.max_seconds = float(max_seconds)
        self._decode_fn = decode_fn
        self.mip_levels = int(mip_levels)
        self.mip_min = int(mip_min)
        self.halfband_taps = int(halfband_taps) | 1   # odd, so it is centred
        self.overview_cols = int(overview_cols)
        self.ready = False
        self.failed = False
        self.done = False
        self.truncated = False
        self.samples = None      # (N,) float64, mono
        self.chains = None       # [[level0, level1, ...], ...] per channel
        self.stereo = False
        self.overview = None     # (cols, 2) float32 min/max
        self._thread = threading.Thread(
            target=self._work, daemon=True, name="SampleLoad"
        )
        self._thread.start()

    @staticmethod
    def _cap(row, cap, sr):
        """Truncate to ``cap`` samples with a short fade so the cut is quiet."""
        row = np.array(row[:cap])
        fade = min(int(0.010 * sr), cap)
        if fade > 1:
            row[-fade:] *= np.linspace(1.0, 0.0, fade)
        return row

    def _mip_chain(self, level0):
        """Level 0 plus up to ``mip_levels`` half-band-decimated octaves."""
        chain = [np.ascontiguousarray(level0, dtype=np.float64)]
        if self.mip_levels <= 0:
            return chain
        taps = firwin(self.halfband_taps, 0.5, window="blackman")
        half = self.halfband_taps // 2
        cur = chain[0]
        for _ in range(self.mip_levels):
            if cur.shape[0] // 2 < self.mip_min:
                break
            # Zero-pad by half the kernel and convolve "valid": exactly N
            # out for N in, centred. (mode="same" would hand back the
            # KERNEL's length for a file shorter than the kernel.)
            cur = np.convolve(np.pad(cur, half), taps, mode="valid")[::2]
            chain.append(np.ascontiguousarray(cur, dtype=np.float32))
        return chain

    def _build_overview(self, mono):
        n = int(mono.shape[0])
        cols = max(1, min(self.overview_cols, n))
        bounds = np.linspace(0, n, cols + 1).astype(np.int64)
        starts = bounds[:-1]
        lo = np.minimum.reduceat(mono, starts)
        hi = np.maximum.reduceat(mono, starts)
        return np.stack([lo, hi], axis=1).astype(np.float32)

    def _work(self) -> None:
        try:
            stereo = self._decode_fn(self.path, self.target_sr)
            if stereo is None or stereo.shape[1] == 0:
                self.failed = True
                return
            left = np.asarray(stereo[0], dtype=np.float64)
            right = np.asarray(stereo[1], dtype=np.float64)
            cap = int(self.max_seconds * self.target_sr)
            if cap > 0 and left.shape[0] > cap:
                left = self._cap(left, cap, self.target_sr)
                right = self._cap(right, cap, self.target_sr)
                self.truncated = True
            mono = 0.5 * (left + right)
            self.samples = np.ascontiguousarray(mono, dtype=np.float64)
            self.overview = self._build_overview(self.samples)
            if np.array_equal(left, right):
                # A mono recording (or dual-mono): one chain, and the
                # mono sum IS the file, bit-for-bit.
                self.chains = [self._mip_chain(self.samples)]
                self.stereo = False
            else:
                self.chains = [self._mip_chain(left), self._mip_chain(right)]
                self.stereo = True
            self.ready = True
        except Exception as exc:  # pragma: no cover - filesystem/codec-specific
            print(f"[Sampler] sample load failed for {self.path}: {exc}")
            self.failed = True
        finally:
            self.done = True

    def wait(self, timeout=None) -> bool:
        """Join the worker (tests / offline render only). True if usable."""
        self._thread.join(timeout)
        return self.ready and not self.failed

    def close(self) -> None:
        """No long-lived resource to kill; the daemon worker exits by itself."""
        return None


class NumpyBackend(
    ClockworkRenderers,
    DynamicsRenderers,
    ModFXRenderers,
    ReverbDelayRenderers,
    ModSourceRenderers,
    SequencingRenderers,
    EQFilterRenderers,
    ColourRenderers,
    PitchTimeRenderers,
    SpectralRenderers,
    OscillatorRenderers,
    IORenderers,
    AudioBackend,
):
    """Pure-Python fallback. Slower than pyo but works wherever numpy does."""

    name = "numpy"

    def __init__(self, sample_rate: int = 44100, block_size: int = 512) -> None:
        super().__init__(sample_rate=sample_rate, block_size=block_size)
        self._patch: Patch | None = None
        self._topo_order: list[int] = []
        # Where the compiled patch was loaded from (its folder), and the
        # per-compile memo of resolved media paths. See
        # :meth:`_resolve_media_path` -- the memo must live at least as
        # long as a compile generation because the renderers use the
        # resolved string as a "still the right file?" cache key.
        self._patch_dir: str | None = None
        self._media_path_cache: dict[tuple[str, str | None], str] = {}
        # Matrix feedback: cables marked late-read at compile, and the
        # previous block's buffers for their source ports (seeded into
        # the store before each render walk). _late_prev survives live
        # recompiles so an edit near a running loop doesn't drop a block.
        self._late_edges: set[tuple[int, str, int, str]] = set()
        self._late_prev: dict[tuple[int, str], np.ndarray] = {}
        # Blocks in which a late-read source produced a non-finite value
        # and was scrubbed to zero before being stashed (a feedback loop
        # that blew past float range). A counter, not a silencer: the
        # loop keeps running, the readout can say it happened.
        self._late_nonfinite: int = 0
        # Blocks in which a bus bound for an audio device carried a
        # non-finite sample (NaN / inf from anything upstream) and was
        # scrubbed before the clip -- np.clip passes NaN straight through,
        # so without this the device would be handed garbage. Counted so
        # the status bar can say it happened (see :meth:`sink_scrubs`).
        self._sink_nonfinite: int = 0
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
        # Stream-health counters, the other half of the DSP readout.
        # _dsp_overloads counts blocks whose *render* missed the budget;
        # these count what PortAudio itself reports about the *device*:
        # an output_underflow means the device ran dry before this
        # callback delivered samples, i.e. the callback arrived late.
        # The two together tell throughput apart from jitter -- see
        # _note_stream_status for why that distinction is the whole
        # point of this readout. Same no-lock discipline as above.
        self._xruns: int = 0
        self._input_xruns: int = 0
        # Name of the host API the main stream actually opened on
        # ("MME", "Windows WASAPI", ...), resolved once in start().
        # Empty while stopped or if the lookup fails.
        self._host_api: str = ""

    # ----- availability ----------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        return _HAS_SOUNDDEVICE

    # ----- compile ---------------------------------------------------------

    def compile(self, patch: Patch) -> None:
        with self._lock:
            self._patch = patch
            # Media paths resolve relative to the patch's own folder (see
            # _resolve_media_path). Recompute both here: a different patch
            # means a different folder, and dropping the memo is what lets
            # a file that was missing last time be found once it appears.
            source = getattr(patch, "source_path", None)
            self._patch_dir = str(Path(source).resolve().parent) if source else None
            self._media_path_cache = {}
            # Feedback door: any cable that would close a cycle becomes
            # a LATE-READ (previous-block buffer, one block of loop
            # latency) — cables into a matrix_mixer first, then whatever
            # still closes a loop — computed BEFORE the sort so the sort
            # can ignore them and the rest of the graph orders as ever.
            self._late_edges = self._compute_late_edges(patch)
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
                fp_path = self._resolve_media_path(m.params.get("path", ""))
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
                cv_path = self._resolve_media_path(m.params.get("path", ""))
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

            # Sampler lifecycle: same reasoning as the convolver above --
            # kick the whole-file decode on the compile (UI) thread so the
            # audio thread never waits on a disk read. Path unchanged ->
            # keep the loaded buffer (or the in-flight loader) as-is.
            for mid, m in patch.modules.items():
                if m.TYPE != "sampler":
                    continue
                st = self._state.setdefault(mid, self._new_sampler_state())
                sp_path = self._resolve_media_path(m.params.get("path", ""))
                pend = st.get("pending")
                if sp_path and sp_path != st.get("loaded_path") and (
                    pend is None or pend.get("path") != sp_path
                ):
                    if pend is not None and pend.get("loader") is not None:
                        pend["loader"].close()
                    st["pending"] = {
                        "path": sp_path,
                        "loader": self._start_sample_loader(sp_path),
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

    def _topological_sort(self, patch: Patch) -> list[int]:
        """Kahn's algorithm — sources first, sinks last.

        Cables carrying a DELAYED signal (see :meth:`_is_delayed_edge`)
        are ignored for ordering: their value is seeded from the
        previous block's state before anything renders, so they impose
        no within-block ordering — and counting them would poison every
        module downstream of a governor feedback patch into the
        unordered leftover tail below (Kahn never emits a cycle member,
        so its whole chain would fall through in creation order).
        """
        in_degree: dict[int, int] = {mid: 0 for mid in patch.modules}
        for cable in patch.cables:
            if self._is_delayed_edge(patch, cable):
                continue
            in_degree[cable.dst_module_id] = in_degree.get(cable.dst_module_id, 0) + 1
        ready = [mid for mid, deg in in_degree.items() if deg == 0]
        order: list[int] = []
        while ready:
            mid = ready.pop(0)
            order.append(mid)
            for cable in patch.cables_out_of(mid):
                if self._is_delayed_edge(patch, cable):
                    continue
                in_degree[cable.dst_module_id] -= 1
                if in_degree[cable.dst_module_id] == 0:
                    ready.append(cable.dst_module_id)
        for mid in patch.modules:
            if mid not in order:
                order.append(mid)
        return order

    def _is_delayed_edge(self, patch: Patch, cable) -> bool:
        """True for cables carrying a one-block-DELAYED signal.

        Two kinds: the buffered sink's ``fill`` cv out (the governor
        loop), and any cable compile marked a LATE-READ (a cable that
        would close a cycle -- see :meth:`_compute_late_edges`). Both
        are real signal paths but not within-block dependencies: their
        values are seeded from the previous block before the render
        walk, which is exactly what lets a feedback loop close while
        the rest of the graph still sorts deterministically.
        """
        src = patch.modules.get(cable.src_module_id)
        if (
            src is not None
            and src.TYPE in NumpyBackend._BUFFERED_SPEAKERS
            and cable.src_port == "fill"
        ):
            return True
        return (
            cable.src_module_id,
            cable.src_port,
            cable.dst_module_id,
            cable.dst_port,
        ) in self._late_edges

    def _compute_late_edges(
        self, patch: Patch
    ) -> set[tuple[int, str, int, str]]:
        """Find every cable that would close a cycle, in two passes.

        A cable "closes a cycle" when its destination can already reach
        its source through the live graph (delayed edges -- fill outs
        and late-reads marked earlier in this very scan -- excluded).
        Such a cable becomes a LATE-READ: its consumer sees the
        previous block's buffer, and the sort ignores it. Marking one
        cable breaks the cycle for every other member, so each loop
        costs exactly one block of latency, at that cable.

        **Pass 1 -- the matrix door (unchanged since 2026-08-04).** Cables
        into a ``matrix_mixer`` (audio rows AND column CVs -- a cycle
        through ``audio_to_cv`` into a cv jack would poison the sort
        just the same), in ``patch.cables`` order. Kept first and kept
        identical so every loop that already closed through the matrix
        compiles to the same late set and renders bit-for-bit as
        before; the matrix stays the *guarded* door, the one with a
        soft ceiling.

        **Pass 2 -- every other loop (2026-09-16).** The remaining cables
        in REVERSE ``patch.cables`` order: the cable that closed the
        loop -- drawn last, saved last -- is the one that reads a block
        late, so the feed-forward path keeps zero latency (forward
        order would put the block on ``osc -> delay -> filter``'s
        forward leg instead of ``filter -> delay``'s return). A
        self-loop (``eoc -> trig``) is a cycle of length one and marks
        itself. Cycles that pass 1 already broke are not cycles here.

        Deterministic either way: the same patch, saved and reloaded,
        gets the same late set (JSON keeps cable order).
        """
        late: set[tuple[int, str, int, str]] = set()

        def _closes_cycle(cable) -> bool:
            # BFS: can this cable's destination reach its source?
            target = cable.src_module_id
            seen = {cable.dst_module_id}
            frontier = [cable.dst_module_id]
            while frontier:
                mid = frontier.pop()
                for out_cable in patch.cables_out_of(mid):
                    out_key = (
                        out_cable.src_module_id,
                        out_cable.src_port,
                        out_cable.dst_module_id,
                        out_cable.dst_port,
                    )
                    if out_key in late:
                        continue
                    src_mod = patch.modules.get(out_cable.src_module_id)
                    if (
                        src_mod is not None
                        and src_mod.TYPE in NumpyBackend._BUFFERED_SPEAKERS
                        and out_cable.src_port == "fill"
                    ):
                        continue
                    nxt = out_cable.dst_module_id
                    if nxt == target:
                        return True
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append(nxt)
            return False

        def _key(cable):
            return (
                cable.src_module_id,
                cable.src_port,
                cable.dst_module_id,
                cable.dst_port,
            )

        # Pass 1: the matrix door, forward order.
        matrix_ids = {
            mid for mid, m in patch.modules.items() if m.TYPE == "matrix_mixer"
        }
        if matrix_ids:
            for cable in patch.cables:
                if cable.dst_module_id not in matrix_ids:
                    continue
                if _closes_cycle(cable):
                    late.add(_key(cable))
        # Pass 2: everything else, reverse order. A buffered sink's
        # ``fill`` cable is already a delayed edge in its own right (it
        # is seeded from the sink, not from _late_prev) -- never mark it.
        for cable in reversed(patch.cables):
            key = _key(cable)
            if key in late:
                continue
            src_mod = patch.modules.get(cable.src_module_id)
            if (
                src_mod is not None
                and src_mod.TYPE in NumpyBackend._BUFFERED_SPEAKERS
                and cable.src_port == "fill"
            ):
                continue
            if _closes_cycle(cable):
                late.add(key)
        return late

    # ----- feedback-door observables (for the UI) -----------------------------

    def feedback_cables(self, patch: Patch) -> set[tuple[int, str, int, str]]:
        """The cables that read one block late if ``patch`` compiled now.

        A pure function of the patch (:meth:`_compute_late_edges` -- no
        lock, no state touched), so the node editor can colour a loop
        the moment it is drawn, running or not, and the answer is the
        same one the next compile will give.
        """
        return self._compute_late_edges(patch)

    def feedback_scrubs(self) -> int:
        """Blocks so far in which a late-read source went non-finite and
        was scrubbed to silence before being fed back (a loop that blew
        past float range -- see :meth:`render_block_multi`). Lock-free:
        one int, read by the GUI tick."""
        return int(self._late_nonfinite)

    def sink_scrubs(self) -> int:
        """Blocks so far in which an output bus (master or a routed
        device's) carried a NaN / inf sample and had it silenced before it
        reached the device -- a module upstream is producing garbage. Only
        the bad samples are zeroed; the rest of the block plays. Lock-free:
        one int, read by the GUI tick."""
        return int(self._sink_nonfinite)

    # ----- start / stop ----------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if not _HAS_SOUNDDEVICE:
            raise RuntimeError(
                "sounddevice is not installed - cannot start NumpyBackend. "
                "Install with: pip install sounddevice"
            )
        if self._patch is None:
            raise RuntimeError("Call compile(patch) before start().")
        # Fresh load stats per run (the toolbar readout greys out while
        # stopped, so a stale figure would never be seen anyway).
        self._dsp_load = 0.0
        self._dsp_load_peak = 0.0
        self._dsp_overloads = 0
        self._xruns = 0
        self._input_xruns = 0
        self._host_api = ""
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
                self._host_api = self._resolve_host_api()
                self._sync_device_outputs()
                self._running = True
                return
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "MicInput: duplex stream open failed (%s); falling "
                    "back to output-only - mic will be silent.", e
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
        self._host_api = self._resolve_host_api()
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
        for module in list(self._midi_inputs.values()):
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
            self._note_stream_status(status)
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
            self._note_stream_status(status)
        self._input_block = indata
        self._fill_output(outdata, frames)

    def _note_stream_status(self, status: Any) -> None:
        """Tally one PortAudio status report (audio thread).

        ``status`` is a ``sounddevice.CallbackFlags``. The flag worth
        counting is ``output_underflow``: PortAudio sets it when the
        device ran dry *before* this callback handed over samples --
        that is, the callback itself arrived late. Read against
        ``_dsp_overloads`` it separates the two failure modes that both
        sound like a click:

        * underflows climbing while overloads stay at zero -- the render
          fits the budget comfortably and the *dispatch* was late (OS
          scheduling, GIL contention, host-API jitter). Rendering ahead
          into a queue fixes this, because the work can be banked early.
        * underflows and overloads climbing together -- genuine
          throughput overload. Deeper buffering only postpones the
          glitch; the patch has to get cheaper.

        Deliberately counters and not a ``print``: writing to stdout from
        the audio thread takes a lock and does I/O on the one thread that
        must never block, so the old logging made a glitch storm worse
        exactly when it mattered. Plain int increments, read unlocked by
        the GUI (see the __init__ notes) -- a stale count is harmless.
        """
        if getattr(status, "output_underflow", False):
            self._xruns += 1
        if getattr(status, "input_overflow", False):
            self._input_xruns += 1

    def _resolve_host_api(self) -> str:
        """Name of the host API the open stream actually landed on.

        The main stream opens with no ``device`` or ``latency`` hint, so
        PortAudio picks the system default -- which on Windows is
        typically MME, whose buffering and callback scheduling are
        markedly worse than WASAPI's. Worth *showing* rather than
        assuming: it decides whether a jitter problem is ours to fix in
        the render path or the host API's to fix by picking another one.

        Never raises -- a failed lookup just yields "" and the readout
        stays blank.
        """
        if sd is None or self._stream is None:
            return ""
        try:
            device = self._stream.device
            if isinstance(device, (tuple, list)):
                device = device[-1]      # duplex reports (input, output)
            info = sd.query_devices(device)
            return str(sd.query_hostapis(info["hostapi"])["name"])
        except Exception:
            return ""

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

    def stream_health_snapshot(self) -> tuple[int, int, str]:
        """``(xruns, input_xruns, host_api)`` for the toolbar readout.

        ``xruns`` counts device output underflows since start(),
        ``input_xruns`` input overflows on the duplex stream, and
        ``host_api`` names the API the stream opened on ("" while
        stopped or if the lookup failed). GUI-thread safe -- plain
        attribute reads, see the __init__ notes.
        """
        return self._xruns, self._input_xruns, self._host_api

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

            from .._crash import explicit_write, write_crash_report
            from ..error_handler import describe_error
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
        "warping_buffered_speaker_output",
    })
    # Device-targetable sinks: a non-empty ``device`` pulls them off the
    # master bus onto their own secondary OutputStream. The buffered variant
    # additionally carries its own ``buffer_size`` (that stream's block size);
    # the plain one runs at the global block size. Secondary streams are
    # therefore keyed by (device, block_size) so the two never collide on a
    # shared device — see _stream_key.
    _SPECIFIC_STEREO_SPEAKER = "specific_stereo_speaker_output"
    _BUFFERED_SPECIFIC_SPEAKER = "buffered_specific_speaker_output"
    _WARPING_BUFFERED_SPEAKER = "warping_buffered_speaker_output"
    # The buffered *family*: sinks that carry their own ``buffer_size``,
    # publish a delayed ``fill`` cv out, and run the ring governor. The plain
    # buffered sink hides its correction (pitch-preserving WSOLA); the warping
    # sibling exposes it (audible varispeed). Every place that treats "a sink
    # with its own ring + governor" the same checks this set; only the
    # actuator and the ratio slew branch on which member it is.
    _BUFFERED_SPEAKERS = frozenset({
        "buffered_specific_speaker_output",
        "warping_buffered_speaker_output",
    })
    _ROUTED_SPEAKERS = frozenset({
        "specific_stereo_speaker_output",
        "buffered_specific_speaker_output",
        "warping_buffered_speaker_output",
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
        if module.TYPE in self._BUFFERED_SPEAKERS:
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
            late_srcs = {
                (src_id, src_port)
                for (src_id, src_port, _, _) in self._late_edges
            }
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
            if module.TYPE not in self._BUFFERED_SPEAKERS:
                continue
            buffers[(module.id, "fill")] = np.full(
                frames, self._sink_fill(module), dtype=np.float32
            )
        # Matrix feedback seed: every late-read source port publishes the
        # PREVIOUS block's buffer before anything renders — the matrix
        # (sorted early, its cycle edge ignored) consumes the stale value;
        # the source renders later and overwrites the key, so every
        # feed-forward consumer still reads fresh. A fresh loop's first
        # block (or a block-size change) reads silence.
        for key in late_srcs:
            prev = self._late_prev.get(key)
            if prev is None or prev.shape[-1] != frames:
                prev = np.zeros(frames, dtype=np.float32)
            buffers[key] = prev
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

        # Stash the fresh late-read source buffers for the next block's
        # seed (the source rendered after its consumer and overwrote its
        # key above, so this is this block's real output). A loop that
        # blew past float range would otherwise seed inf/NaN into next
        # block and poison everything downstream forever; scrub it to
        # zero and count it -- the loop keeps running from the dry
        # input, the readout can say it happened. (The matrix door's
        # soft_clip is the guardrail that keeps this from ever firing;
        # a bare loop has none, like hardware.)
        for key in late_srcs:
            buf = buffers.get(key)
            if buf is not None:
                if not np.all(np.isfinite(buf)):
                    buf = np.nan_to_num(buf, nan=0.0, posinf=0.0, neginf=0.0)
                    self._late_nonfinite += 1
                self._late_prev[key] = buf

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
                    levels[key] = float(np.mean(buf, dtype=np.float64))
            self._meter_levels = levels

        out = np.zeros((frames, 2), dtype=np.float32)
        device_blocks: dict[tuple[str, int], np.ndarray] = {}
        # key -> (module_id, smoothed ratio, is_warp). is_warp picks the
        # actuator in the loop below: varispeed (pitch bends) vs WSOLA (held).
        governed: dict[tuple[str, int], tuple[int, float, bool]] = {}
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
                    and module.TYPE in self._BUFFERED_SPEAKERS
                ):
                    ratio = self._governed_ratio(module, patch, buffers)
                    if ratio is not None:
                        # Carry which actuator this stream wants: the warping
                        # sibling bends pitch (varispeed), the plain buffered
                        # sink holds it (WSOLA). See the actuation loop below.
                        governed[key] = (
                            module.id,
                            ratio,
                            module.TYPE == self._WARPING_BUFFERED_SPEAKER,
                        )
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
                src_buf = self._voice_sum(src_buf)
            gain = float(module.params.get("gain", 1.0))
            mixed = (src_buf * gain).astype(np.float32)
            left, right = channels
            if left:
                out[:, 0] += mixed
            if right:
                out[:, 1] += mixed

        # Scrub, then clip. np.clip passes NaN straight through (and turns
        # inf into a full-scale sample), so one non-finite sample anywhere
        # upstream would go to the device as garbage. Zero just the bad
        # samples -- a finite block is untouched, bit for bit -- and count
        # the block so the status bar can say so: a sink that must never
        # raise needs something else to SAY it failed.
        scrubbed = False
        for blk in (out, *device_blocks.values()):
            if not np.isfinite(blk).all():
                np.nan_to_num(blk, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                scrubbed = True
            np.clip(blk, -1.0, 1.0, out=blk)
        if scrubbed:
            self._sink_nonfinite += 1
        # Governor actuation: time-stretch each governed stream's block to
        # frames * ratio before it is handed to the ring (the ring counts in
        # samples, so a push size that differs from the pop size is already
        # legal). Both flavours share the length resample below; they differ
        # only in what feeds it:
        #
        #   * PITCH-PRESERVING (plain buffered sink) — a streaming WSOLA shift
        #     UP by `ratio` (_GrainShifter, one per channel, state persistent
        #     across blocks) cancels the resample's pitch move, so the push
        #     holds pitch. The engines stay in-circuit while governed even at
        #     ratio 1.0 — bypassing at unity would spend the ~one-grain warm-up
        #     (zeros) exactly when the governor first corrects — so the wet
        #     path keeps one constant ~grain (50 ms) latency.
        #   * VARISPEED (warping sibling) — no shift: the raw block is resampled
        #     straight to length frames * ratio, so pitch bends WITH the ratio
        #     (ratio > 1 = longer = slower = lower — the tape running out of
        #     juice). Stateless: per-block resampling of contiguous blocks is
        #     seam-continuous, so it never allocates a _GrainShifter.
        #
        # Ungoverned sinks never reach here (see _governed_ratio), keeping the
        # unpatched push bit-identical.
        if governed and frames > 0:
            src_pos = np.arange(frames, dtype=np.float64)
            for key, (mid, ratio, is_warp) in governed.items():
                blk = device_blocks.get(key)
                if blk is None:
                    continue
                if is_warp:
                    # Varispeed: feed the raw block to the resample — the
                    # pitch is meant to move. No persistent engine state.
                    base = blk.astype(np.float64)
                else:
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
                    base = np.empty((frames, 2), dtype=np.float64)
                    base[:, 0] = engines[0].process(
                        blk[:, 0].astype(np.float64), ratio
                    )
                    base[:, 1] = engines[1].process(
                        blk[:, 1].astype(np.float64), ratio
                    )
                m = max(1, int(round(frames * ratio)))
                if m == frames:
                    # Sub-0.5-sample rounding: skip the resample (the
                    # residual pitch offset is a few cents at worst).
                    stretched = base.astype(np.float32)
                else:
                    dst_pos = np.arange(m, dtype=np.float64) * (frames / m)
                    stretched = np.empty((m, 2), dtype=np.float32)
                    stretched[:, 0] = np.interp(dst_pos, src_pos, base[:, 0])
                    stretched[:, 1] = np.interp(dst_pos, src_pos, base[:, 1])
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
            left = self._voice_sum(left)
        if right is not None and right.ndim == 2:
            right = self._voice_sum(right)

        pan = float(module.params.get("pan", 0.0))
        width = float(module.params.get("width", 1.0))
        width = min(max(width, 0.0), 2.0)
        gain = float(module.params.get("gain", 1.0))
        cv_depth = float(module.params.get("cv_depth", 1.0))

        cv = self._input_buffer(patch, buffers, module.id, "pan_cv", collapse=False)
        if cv is not None and cv.size and cv_depth != 0.0:
            if cv.ndim == 2:
                cv = self._voice_mean(cv)
            p = np.clip(pan + cv_depth * cv, -1.0, 1.0)
        else:
            p = min(max(pan, -1.0), 1.0)

        wcv = self._input_buffer(
            patch, buffers, module.id, "width_cv", collapse=False
        )
        if wcv is not None and wcv.size and cv_depth != 0.0:
            if wcv.ndim == 2:
                wcv = self._voice_mean(wcv)
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
            level = self._finite_mean(cv) if cv is not None and cv.size else 0.0
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
        if module.TYPE == self._WARPING_BUFFERED_SPEAKER:
            # Tape-transport slew: coast toward target at a constant rate
            # (constant-torque feel), asymmetric between braking (ratio
            # rising — the ring starves, pitch dives) and spin-up (falling —
            # recovering). A one-pole would ease-out of every correction; a
            # bounded step gives the linear capstan glide the resampler brake
            # is named for. The plain buffered sink keeps its one-pole, so
            # that path stays bit-exact.
            ratio = self._brake_slew(module, prev, target)
        else:
            ratio = prev + self._SINK_RATIO_SMOOTH * (target - prev)
        self._sink_ratio[module.id] = ratio
        return ratio

    # Full swing of the governed ratio (the 0.5..2.0 clamp), used to turn the
    # warping sink's brake_time / spinup_time seconds into a per-block step.
    _RATIO_SWING = 2.0 - 0.5

    def _brake_slew(self, module, prev: float, target: float) -> float:
        """Move ``prev`` toward ``target`` at the warping sink's constant
        tape-transport rate, capped per block.

        ``brake_time`` sets the rate while the ratio RISES (the ring is
        starving — the deck brakes and the pitch dives); ``spinup_time`` sets
        the fall (recovering — the deck winds back up). Each is the seconds a
        full-scale ratio swing (:data:`_RATIO_SWING`) would take, so the
        per-block cap is ``swing * block / (seconds * sample_rate)``. A
        non-positive time means an instant jump (no slew)."""
        delta = target - prev
        if delta == 0.0:
            return target
        rising = delta > 0.0
        try:
            secs = float(module.params.get(
                "brake_time" if rising else "spinup_time",
                0.5 if rising else 0.25,
            ))
        except (TypeError, ValueError):
            secs = 0.5 if rising else 0.25
        if secs <= 0.0:
            return target
        max_step = self._RATIO_SWING * self.block_size / (secs * self.sample_rate)
        if abs(delta) <= max_step:
            return target
        return prev + (max_step if rising else -max_step)

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
            return self._render_clock(module, frames, buffers, patch)
        if module.TYPE in ("sequencer", "fader_seq"):
            # fader_seq is the Sequencer with a different front panel —
            # identical param contract, one engine (see modules/fader_seq.py).
            return self._render_sequencer(module, frames, buffers, patch)
        if module.TYPE == "shift_random":
            return self._render_shift_random(module, frames, buffers, patch)
        if module.TYPE == "chaos":
            return self._render_chaos(module, frames, buffers, patch)
        if module.TYPE == "organ":
            return self._render_organ(module, frames, buffers, patch)
        if module.TYPE == "matrix_mixer":
            return self._render_matrix_mixer(module, frames, buffers, patch)
        if module.TYPE == "vinyl":
            return self._render_vinyl(module, frames, buffers, patch)
        if module.TYPE == "supersaw":
            return self._render_supersaw(module, frames, buffers, patch)
        if module.TYPE == "wavetable_morph":
            return self._render_wavetable_morph(module, frames, buffers, patch)
        if module.TYPE == "possibility_seq":
            return self._render_possibility_seq(module, frames, buffers, patch)
        if module.TYPE == "possibility_selector":
            return self._render_possibility_selector(module, frames, buffers, patch)
        if module.TYPE == "drift":
            return self._render_drift(module, frames, buffers, patch)
        if module.TYPE == "sampler":
            return self._render_sampler(module, frames, buffers, patch)
        if module.TYPE == "euclidean":
            return self._render_euclidean(module, frames, buffers, patch)
        if module.TYPE == "burst":
            return self._render_burst(module, frames, buffers, patch)
        if module.TYPE == "bernoulli_gate":
            return self._render_bernoulli(module, frames, buffers, patch)
        if module.TYPE == "clock_divider":
            return self._render_clock_divider(module, frames, buffers, patch)
        if module.TYPE == "arpeggiator":
            return self._render_arpeggiator(module, frames, buffers, patch)
        if module.TYPE == "chord":
            return self._render_chord(module, frames, buffers, patch)
        if module.TYPE == "cv_math":
            return self._render_cv_math(module, frames, buffers, patch)
        if module.TYPE == "vowel":
            return self._render_vowel(module, frames, buffers, patch)
        if module.TYPE == "freeze":
            return self._render_freeze(module, frames, buffers, patch)
        if module.TYPE == "cv_recorder":
            return self._render_cv_recorder(module, frames, buffers, patch)
        if module.TYPE == "logic":
            return self._render_logic(module, frames, buffers, patch)
        if module.TYPE == "mid_side":
            return self._render_mid_side(module, frames, buffers, patch)
        if module.TYPE == "autopan":
            return self._render_autopan(module, frames, buffers, patch)
        if module.TYPE == "octaver":
            return self._render_octaver(module, frames, buffers, patch)
        if module.TYPE == "midi_input":
            return self._render_midi_input(module, frames)
        if module.TYPE == "filter":
            return self._render_filter(module, frames, buffers, patch)
        if module.TYPE == "adsr":
            return self._render_adsr(module, frames, buffers, patch)
        if module.TYPE == "ad_envelope":
            return self._render_ad(module, frames, buffers, patch)
        if module.TYPE == "function_generator":
            return self._render_function_generator(module, frames, buffers, patch)
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
        if module.TYPE == "bowed":
            return self._render_bowed(module, frames, buffers, patch)
        if module.TYPE == "wind":
            return self._render_wind(module, frames, buffers, patch)
        if module.TYPE == "pluck":
            return self._render_pluck(module, frames, buffers, patch)
        if module.TYPE == "modal":
            return self._render_modal(module, frames, buffers, patch)
        if module.TYPE == "kick_drum":
            return self._render_kick(module, frames, buffers, patch)
        if module.TYPE == "snare_drum":
            return self._render_snare(module, frames, buffers, patch)
        if module.TYPE == "hat_drum":
            return self._render_hat(module, frames, buffers, patch)
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
        if module.TYPE == "slew":
            return self._render_slew(module, frames, buffers, patch)
        if module.TYPE == "quantizer":
            return self._render_quantizer(module, frames, buffers, patch)
        if module.TYPE == "scope":
            return self._render_scope(module, frames, buffers, patch)
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
        if module.TYPE == "rotary":
            return self._render_rotary(module, frames, buffers, patch)
        if module.TYPE == "granular":
            return self._render_granular(module, frames, buffers, patch)
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
        1D via :meth:`_voice_sum` so existing mono modules continue to
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
                    return NumpyBackend._voice_sum(buf)
                return buf
        return None

    # Samples per phase-wrap epoch of :meth:`_osc_carried_phase`. The
    # running sum is wrapped back into [0, 1) only at ABSOLUTE multiples
    # of this count, so the wrap lands on the same sample at any block
    # size; between wraps the sum reaches at most ``inc * 65536`` cycles
    # (~33k at Nyquist), far inside float64's exact-enough range.
    _OSC_EPOCH = 1 << 16

    @staticmethod
    def _osc_carried_phase(carry, n0, inc, epoch):
        """Integrate per-sample phase increments, exact across blocks.

        ``inc`` is ``(..., F)`` float64 (per-sample cycles per sample),
        ``carry`` the ``(...)`` running phase before this block's first
        sample, ``n0`` the absolute index of that sample. Returns
        ``(phases, carry)``: the wrapped ``(..., F)`` phase of every
        sample and the carry for the next block.

        ``np.cumsum`` is a strictly sequential accumulation, so
        ``cumsum([carry, inc...])`` performs exactly the additions a
        sample-at-a-time loop would -- the value at sample n does not
        depend on where the block boundaries fell, PROVIDED the carry
        is never rounded or wrapped at a block boundary. So the sum
        runs unwrapped and is wrapped (``x - floor(x)``, exact in
        binary) only at absolute multiples of ``epoch`` samples; a block
        that straddles one is summed in two segments. The old
        ``(start + cumsum(inc)) % 1`` -- a fresh sum per block, added to
        a start wrapped at every block end -- took a different rounding
        path per partition and drifted a float32 ulp within ~0.1 s under
        a held CV.
        """
        F = inc.shape[-1]
        carry = np.asarray(carry, dtype=np.float64)
        to_edge = epoch - n0 % epoch
        if F < to_edge:
            # The common case: no epoch wrap inside this block.
            seg = np.cumsum(
                np.concatenate((carry[..., None], inc), axis=-1), axis=-1
            )
            return seg[..., 1:] % 1.0, seg[..., -1]
        cum = np.empty_like(inc)
        pos = 0
        while pos < F:
            to_edge = epoch - (n0 + pos) % epoch
            end = min(F, pos + to_edge)
            seg = np.cumsum(
                np.concatenate((carry[..., None], inc[..., pos:end]), axis=-1),
                axis=-1,
            )
            cum[..., pos:end] = seg[..., 1:]
            carry = seg[..., -1]
            if end - pos == to_edge:
                carry = carry - np.floor(carry)  # the epoch wrap
            pos = end
        return cum % 1.0, carry

    @staticmethod
    def _osc_pw_increment(state, pw):
        """Per-sample change of an array pulse width, continuous across
        blocks.

        The falling edge of ``square_blep`` is corrected on the falling
        edge's own phase, ``(phase - pw) mod 1``, which advances by
        ``dt - dpw`` per sample rather than ``dt``. Sizing that edge's
        correction window with its own increment is what keeps the
        "sample before" and "sample after" halves of a PolyBLEP pair in
        agreement when the width moves: both then measure the crossing
        in the same units, exactly as the rising edge's pair does under
        per-sample FM. The first sample's increment reads against the
        previous block's last width (``state["pw_last"]``), so a sweep
        that crosses a block boundary is not seen as a jump there.

        Returns an array shaped like ``pw``. For a scalar width (pw_cv
        unpatched) the caller passes ``None`` instead -- the increment
        is zero and the window is plain ``dt``, the pre-PWM arithmetic.
        """
        pw = np.asarray(pw, dtype=np.float64)
        last = pw[..., -1].copy()
        prev = state.get("pw_last")
        if prev is None or np.shape(prev) != np.shape(last):
            # First block, or the voice count / path changed: no history
            # to diff against, so the first sample's increment is zero.
            prev = pw[..., 0]
        dpw = np.empty_like(pw)
        dpw[..., 0] = pw[..., 0] - prev
        dpw[..., 1:] = np.diff(pw, axis=-1)
        state["pw_last"] = last
        return dpw

    # Wavetable mipmap parameters. WT_LEN is the per-table sample count;
    # NUM_WT_TABLES octave bands span WT_BASE_FREQ .. ~Nyquist.
    WT_LEN = 2048
    NUM_WT_TABLES = 11
    WT_BASE_FREQ = 20.0

    def _osc_waveshape(self, phases, waveform, dt=None, pw=None, dpw=None):
        """Apply the waveform shaping function to a phase array.

        ``phases`` can be any shape (1D for mono, 2D for voice) -- all
        ops are elementwise (or shape-preserving) so the same code
        handles both. Returns an array of the same shape with values in
        roughly [-1, 1].

        ``pw`` is the pulse width of the square shapes: ``None`` (every
        caller but the Oscillator -- the classic 50% square), a scalar,
        or an array broadcastable to ``phases`` (per-sample PWM). ``dpw``
        is the per-sample width increment that goes with an array
        ``pw`` (see :meth:`_osc_pw_increment`); ``None`` means the width
        is constant. Only ``square`` / ``square_blep`` read them --
        ``square_wt`` is a fixed 50% table -- and at ``pw`` 0.5 the
        square arithmetic reduces to the pre-PWM expressions bit for bit.

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

        if base != "square" or method == "wt":
            # Only the two pulse shapes have a width; square_wt is the
            # 50% mipmap by design (and would still be if it degraded to
            # naive for want of a dt).
            pw = dpw = None
        if method == "blep" and dt is not None:
            return self._waveshape_blep(base, phases, dt, pw=pw, dpw=dpw)
        if method == "wt" and dt is not None:
            return self._waveshape_wt(base, phases, dt)
        # naive (or anti-aliased requested with no dt -> degrade to naive)
        return self._waveshape_naive(base, phases, pw=pw)

    @staticmethod
    def _waveshape_naive(base, phases, pw=None):
        if base == "sine":
            return np.sin(2.0 * np.pi * phases)
        if base == "saw":
            return 2.0 * phases - 1.0
        if base == "square":
            if pw is None:
                return np.where(phases < 0.5, 1.0, -1.0)
            # High while the phase is below the width, then the DC a
            # non-50% pulse carries (mean = 2pw - 1) taken out per
            # sample so a slow PWM sweep does not pump the speaker. At
            # pw 0.5 the offset is exactly 0.0 and ``v - 0.0`` is ``v``.
            return np.where(phases < pw, 1.0, -1.0) - (2.0 * pw - 1.0)
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

    def _waveshape_blep(self, base, phases, dt, pw=None, dpw=None):
        """PolyBLEP saw/square, PolyBLAMP triangle. Sine has no edges."""
        phases = np.asarray(phases, dtype=np.float64)
        if base == "saw":
            return (2.0 * phases - 1.0) - self._poly_blep(phases, dt)
        if base == "square":
            if pw is None:
                v = np.where(phases < 0.5, 1.0, -1.0)
                v = v + self._poly_blep(phases, dt)
                v = v - self._poly_blep((phases + 0.5) % 1.0, dt)
                return v
            # The rising edge sits at phase 0 whatever the width; the
            # falling edge sits at phase ``pw``, so its correction runs
            # on the falling edge's own phase ``(phase - pw) mod 1``.
            # When the width moves, that phase advances by ``dt - dpw``
            # per sample, and the correction window must be that
            # increment (not ``dt``) for the two halves of the blep pair
            # to agree on where the edge fell -- see _osc_pw_increment.
            # The window is capped at half a cycle: a width that jumps
            # by more than that in one sample (a square LFO on pw_cv)
            # is a step the blep can only soften, and past 0.5 its two
            # half-windows would overlap and fight. With ``pw`` 0.5 and
            # no ``dpw`` this is ``(phases + 0.5) % 1.0`` with window
            # ``dt`` -- the classic expression, bit for bit.
            v = np.where(phases < pw, 1.0, -1.0)
            v = v + self._poly_blep(phases, dt)
            fall_dt = dt if dpw is None else np.minimum(dt - dpw, 0.5)
            v = v - self._poly_blep((phases + (1.0 - pw)) % 1.0, fall_dt)
            return v - (2.0 * pw - 1.0)
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

        ``dt`` selects the mipmap band from the top frequency AT EACH
        SAMPLE (largest dt across the leading axes -- every voice of a
        voice block -> highest fundamental -> fewest-harmonics table, the
        conservative pick that never aliases). A scalar ``dt`` is one
        band for the block.

        Per sample, not per block (2026-09-24): the block's max dt made
        the band a function of where the block boundaries fell -- a
        block straddling a note change played the lower note's tail on
        the higher note's table, so a sequence rendered differently (by
        up to ~0.18) at every block size. Under a steady pitch the
        per-sample pick is the old per-block pick, bit for bit (the old
        ``floor(log2(.))`` and the exact exponent differ only for a
        frequency within an ulp below a band edge's power of two).
        """
        if base == "sine":
            return np.sin(2.0 * np.pi * np.asarray(phases, np.float64))
        tables = self._get_wavetable(base)
        L = self.WT_LEN
        phases = np.asarray(phases, dtype=np.float64)
        pos = phases * L
        floor_pos = np.floor(pos)
        i0 = floor_pos.astype(np.int64) % L
        i1 = (i0 + 1) % L
        frac = pos - floor_pos
        dt = np.asarray(dt, dtype=np.float64)

        # Band = floor(log2(freq / WT_BASE_FREQ)), clipped -- taken from
        # frexp's exponent, which is exact (x = m * 2**e with m in
        # [0.5, 1) -> floor(log2 x) = e - 1), so the scalar and the
        # per-sample pick can never disagree by a libm ulp on a band edge
        # the way two log2 implementations might.
        sr, base_f, top = self.sample_rate, self.WT_BASE_FREQ, self.NUM_WT_TABLES - 1

        def band(dt_scalar):
            e = math.frexp(max(dt_scalar * sr, base_f) / base_f)[1]
            return min(max(e - 1, 0), top)

        if dt.ndim == 0:
            j = band(float(dt))
        else:
            # Per-sample band: the max over every leading axis (voices).
            dt_top = dt if dt.ndim == 1 else dt.reshape(-1, dt.shape[-1]).max(axis=0)
            j = band(float(dt_top.max()))
            if band(float(dt_top.min())) != j:
                # The band changes inside this block: pick it per sample
                # (the band is monotonic in dt, so equal ends mean one
                # band throughout and the cheap gather below).
                _m, e = np.frexp(np.maximum(dt_top * sr, base_f) / base_f)
                jj = np.clip(e - 1, 0, top).astype(np.intp)
                jb = np.broadcast_to(jj, phases.shape)
                return tables[jb, i0] * (1.0 - frac) + tables[jb, i1] * frac
        tbl = tables[j]
        return tbl[i0] * (1.0 - frac) + tbl[i1] * frac

    # ----- MatrixMixer rendering -------------------------------------------

    def _render_matrix_mixer(self, module, frames: int, buffers, patch) -> dict:
        """4×4 bipolar gain matrix (see modules/matrix_mixer.py).

        ``out_c = clip(cv_c · Σ_r g_rc · in_r)``. Unpatched rows
        contribute nothing; an all-unpatched column is exact zeros. The
        identity default with nothing else patched is a bit-exact
        4-channel pass (1.0·x + nothing = x, and the soft ceiling is
        transparent below the knee). Mixed mono/(V, F) rows broadcast;
        the feedback plumbing (late-read seeding) lives in
        ``render_block_multi``/``_compute_late_edges``, not here — by
        the time this runs, every input buffer is defined.

        The ``soft_clip`` ceiling: identity below MATRIX_CLIP_KNEE,
        then ``knee + (1−knee)·tanh((|x|−knee)/(1−knee))`` — C1 at the
        knee (unit slope), saturating at exactly 1.0. A plain tanh
        would take 8 % off a 0.5 signal, unacceptable for a default-on
        mixer stage (deviation from the spec's literal "tanh on each
        out", noted in the worklog).
        """
        from ..modules.matrix_mixer import MATRIX_CLIP_KNEE, MATRIX_SIZE

        p = module.params
        soft = bool(p.get("soft_clip", True))

        ins = []
        for r in range(1, MATRIX_SIZE + 1):
            buf = self._input_buffer(
                patch, buffers, module.id, f"in_{r}", collapse=False
            )
            ins.append(None if buf is None else buf.astype(np.float64))
        out: dict[str, np.ndarray] = {}
        for c in range(1, MATRIX_SIZE + 1):
            acc = None
            for r in range(1, MATRIX_SIZE + 1):
                x = ins[r - 1]
                if x is None:
                    continue
                try:
                    g = float(p.get(f"g{r}{c}", 0.0))
                except (TypeError, ValueError):
                    g = 0.0
                g = min(1.0, max(-1.0, g))
                if g == 0.0:
                    continue
                term = x * g
                acc = term if acc is None else acc + term
            if acc is None:
                out[f"out_{c}"] = np.zeros(frames, dtype=np.float32)
                continue
            cv = self._input_buffer(
                patch, buffers, module.id, f"cv_{c}", collapse=False
            )
            if cv is not None:
                acc = acc * cv.astype(np.float64)
            if soft:
                knee = MATRIX_CLIP_KNEE
                over = np.abs(acc) > knee
                if over.any():
                    mag = np.abs(acc)
                    limited = knee + (1.0 - knee) * np.tanh(
                        (mag - knee) / (1.0 - knee)
                    )
                    acc = np.where(over, np.sign(acc) * limited, acc)
            out[f"out_{c}"] = acc.astype(np.float32)
        return out

    # ----- MIDI input rendering ------------------------------------------

    # Polyphonic voice count: matches VoiceSlots.MAX_VOICES. Kept local
    # as a module constant rather than imported to keep the backend
    # free of circular imports with the modules layer.
    _MAX_VOICES = 16

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

    # ----- ADSR rendering -------------------------------------------------

    # Gate is treated as "high" once it crosses this threshold; this gives
    # us tolerance against fractional gate values (e.g. an LFO-style gate
    # in some future patching) without false triggers on numerical noise.
    _GATE_HIGH = 0.5

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

    # ----- DiskWriter rendering -------------------------------------------

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

    # ----- media path resolution ------------------------------------------

    def _resolve_media_path(self, path) -> str:
        """Turn a patch's media ``path`` param into one that actually opens.

        Patches store media paths (a sampler's sample, a convolver's IR, a
        file player's track) as written by whoever made them, which in
        practice means RELATIVE -- and until 2026-08-29 a relative path was
        resolved against the process working directory alone. That made a
        patch's audio depend on where the app happened to be launched from:
        `examples/sampler_breaks.json` played perfectly from the project
        root and rendered pure silence from anywhere else, including from
        `dist/`. Silence, not an error, because every loader here fails
        soft so the audio thread never raises.

        So a relative path is now tried against an ordered list of bases
        and the FIRST ONE THAT EXISTS wins:

          1. the patch's own folder -- the DAW convention, and what makes a
             patch plus its samples portable as a unit;
          2. that folder's parent -- because the shipped examples live in
             ``examples/`` while naming their media from the project root
             (``examples/samples/breaks.wav``), and rewriting them would
             break every patch a user has already saved in that style;
          3. the process working directory -- the historical behaviour, so
             nothing that worked before stops working;
          4. the resource root -- the install/bundle directory, which is
             where the examples and their media live in a frozen build.

        An ABSOLUTE path is returned untouched: the user named an exact
        file and second-guessing them would be worse than failing. An empty
        path stays empty (an unpatched slot, not an error). If nothing
        matches, the original string comes back unchanged so the failure
        message names what the patch actually asked for.

        Results are cached per compile generation. That is not (only) about
        the stat calls -- the renderers use this string as the cache key
        for "is the loaded buffer still the right one", so it MUST be
        stable within a compile or they would reload forever. The cache is
        dropped on every ``compile()``, so moving a missing file into place
        and hitting recompile finds it.
        """
        raw = str(path or "")
        if not raw:
            return ""
        key = (raw, self._patch_dir)
        hit = self._media_path_cache.get(key)
        if hit is not None:
            return hit

        resolved = raw
        candidate = Path(raw)
        if not candidate.is_absolute():
            bases: list[Path] = []
            if self._patch_dir:
                patch_dir = Path(self._patch_dir)
                bases.append(patch_dir)
                bases.append(patch_dir.parent)
            bases.append(Path.cwd())
            try:
                bases.append(resource_root())
            except Exception:  # pragma: no cover - defensive; never fatal
                pass
            for base in bases:
                try:
                    found = base / candidate
                    if found.is_file():
                        resolved = str(found)
                        break
                except (OSError, ValueError):  # pragma: no cover - odd paths
                    continue

        self._media_path_cache[key] = resolved
        return resolved

    def media_load_failures(self) -> list[tuple[int, str, str]]:
        """UI hook: every module whose media file did not load.

        Returns ``(module_id, module_type, path_as_written)`` for each
        ``sampler`` / ``convolver`` whose background load has finished
        and come back empty -- a missing file, an unreadable one, or an
        encoding nothing here can decode.

        This exists because the loaders all fail SOFT: they must, or a
        typo'd path would raise on the audio thread. The cost of that
        choice is that a missing sample is indistinguishable from silence
        unless someone asks, and on 2026-08-29 it cost a listening pass --
        `sampler_breaks.json` played nothing and looked perfectly healthy
        doing it. So the GUI asks, once per failure, and says so.

        The reported path is the one WRITTEN IN THE PATCH, not the
        resolved one: it is what the user typed and what they will go
        looking for. Resolution having failed is precisely the news.

        ``file_player`` is deliberately not included -- it has its own
        per-module hook (:meth:`file_player_failed`) that the playlist
        advancer already uses to skip a dud track, and folding it in here
        would report every skipped file twice.
        """
        patch = self._patch
        if patch is None:
            return []
        out: list[tuple[int, str, str]] = []
        for mid, module in patch.modules.items():
            if module.TYPE not in ("sampler", "convolver"):
                continue
            state = self._state.get(mid)
            if not state:
                continue
            # A load that has RESOLVED (loaded_path set, nothing pending)
            # but produced no audio is a failure. Still-pending or never
            # attempted is not news yet.
            if state.get("pending") is not None or not state.get("loaded_path"):
                continue
            loaded = (
                state.get("samples") if module.TYPE == "sampler"
                else state.get("ir_l")
            )
            if loaded is None:
                out.append((mid, module.TYPE, str(module.params.get("path", ""))))
        return out

    # ----- distortion -------------------------------------------------------

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

    @staticmethod
    def _new_convolver_state():
        return {
            "engine_l": None, "engine_r": None, "ir_l": None, "ir_r": None,
            "loaded_path": None, "pending": None, "block": None,
            "dry_prev": None,
            "tone_zi_l": None, "tone_zi_r": None,
            "pd_buf_l": None, "pd_buf_r": None,
        }

    def _start_ir_loader(self, path, block):
        """Spawn a background IR decode+build for ``path`` (None if empty)."""
        if not path:
            return None
        return _IRLoader(path, self.sample_rate, block, self._decode_audio)

    # ----- Pluck rendering -------------------------------------------------

    # Pitch clamps: below 20 Hz the ring outgrows its allocation; above
    # sr/4 the loop is too short for the machinery (and sounds like a
    # click anyway).
    _PLUCK_MIN_F0 = 20.0
    _PLUCK_C4 = 261.6255653005986
    # A decayed string below this output peak early-outs (ring zeroed so
    # the voice renders exact silence for free until re-plucked).
    _PLUCK_SILENCE = 1e-5
    _PLUCK_SEED = 0x504C5543  # "PLUC"

    # ----- Bowed string ------------------------------------------------------

    # Pitch clamps: the delay allocation below, and above ~2 kHz the
    # bridge-side delay (position x period) shrinks to a handful of
    # samples and the chunked loop degrades to per-sample cost.
    _BOW_MIN_F0 = 30.0
    _BOW_MAX_F0 = 2000.0
    _BOW_C4 = 261.6255653005986
    # Bridge reflection: one-pole lowpass, pole from ``damping``
    # (0.15 = wide open .. 0.7 = dark), fixed loss gain per round trip.
    _BOW_POLE_LO = 0.15
    _BOW_POLE_HI = 0.70
    _BOW_LOSS = 0.95
    # Friction table (STK BowTable): slope from ``pressure``, and the
    # bow velocity range from ``velocity``.
    _BOW_SLOPE_MAX = 5.0
    _BOW_SLOPE_RANGE = 4.0
    _BOW_VEL_MIN = 0.03
    _BOW_VEL_RANGE = 0.2
    # Body: parallel constant-peak bandpasses (Hz, Q, gain) -- an air
    # mode, the two main wood modes and the bridge hill -- mixed by
    # ``body``; _BOW_BODY_GAIN level-matches the bank to the raw bridge.
    _BOW_BODY = ((275.0, 4.0, 1.0), (460.0, 5.0, 0.9), (550.0, 6.0, 0.7),
                 (1100.0, 2.5, 0.6), (2200.0, 2.0, 0.4))
    _BOW_BODY_GAIN = 1.2
    _BOW_OUT_GAIN = 2.0
    # A lifted, decayed string below this output peak early-outs.
    _BOW_SILENCE = 1e-5

    # ----- Wind (flute / reed) ------------------------------------------------

    _WIND_C4 = 261.6255653005986
    _WIND_MAX_F0 = 2500.0
    # The flute's jet model loses its register below ~80 Hz; the reed
    # goes down to the contrabass.
    _WIND_MIN_F0 = {"flute": 80.0, "reed": 30.0}
    # Bore reflection one-pole pole ranges (damping 0..1).
    _WIND_POLE = {"flute": (0.5, 0.85), "reed": (0.3, 0.8)}
    # Breath pressure ranges (breath 0..1): the flute speaks in a window
    # (jet saturation kills it above ~1.5), the reed needs ~0.58 to speak
    # and closes above ~1.1 -- both measured 2026-09-18.
    _WIND_PRESSURE = {"flute": (0.85, 0.55), "reed": (0.58, 0.42)}
    # Flute: bore tuned to 1.5 periods (STK: "we're overblowing here"),
    # times a measured regime correction (the jet's phase pulls the
    # regime sharp by ~1.5%); jet at 0.32 of the bore; 0.5/0.5 jet/end
    # reflections; DC block in the return.
    _WIND_FLUTE_BORE = 1.5 * 1.015
    _WIND_FLUTE_JET = 0.32
    _WIND_FLUTE_REFL = 0.5
    # Reed: reflection -0.95 through the loss filter; table 0.7 - 0.3 dp.
    _WIND_REED_REFL = 0.95
    _WIND_REED_OFFSET = 0.7
    _WIND_REED_SLOPE = 0.3
    _WIND_DC_POLE = 0.995
    _WIND_MODEL_GAIN = {"flute": 0.3, "reed": 1.0}
    _WIND_OUT_GAIN = 2.0
    _WIND_SILENCE = 1e-5
    _WIND_SEED = 0x57494E44  # "WIND"

    # ----- sampler ----------------------------------------------------------

    # Retrigger declick: a voice re-struck while still sounding keeps its old
    # playhead running for this long under a falling ramp while the new one
    # fades in, so the jump never clicks (the drum-voice idiom).
    _SAMPLER_XFADE_MS = 2.0

    @staticmethod
    def _sampler_read(samples, positions):
        """Read a sample buffer at fractional ``positions`` (4-tap cubic).

        The resampler's ``_hermite4`` verbatim, which is the point: at an
        integer position it returns that sample *exactly* (the spline's
        constant term is ``p0``, untouched by float ops), so a unity-rate
        read is a bit-exact copy of the file and an octave jump is a
        bit-exact ``[::2]``. Neighbours are clamped to the buffer, which
        only ever affects the outer taps of the first and last sample.
        """
        n = samples.shape[0]
        base = np.floor(positions)
        i0 = base.astype(np.int64)
        t = positions - base
        top = n - 1
        return _hermite4(
            samples[np.clip(i0 - 1, 0, top)],
            samples[np.clip(i0, 0, top)],
            samples[np.clip(i0 + 1, 0, top)],
            samples[np.clip(i0 + 2, 0, top)],
            t,
        )

    @staticmethod
    def _sampler_reader(chain, rate, antialias):
        """Which mip levels a read at ``rate`` takes: ``[(buf, scale, w)]``.

        ``scale`` maps a position in the original to one in that level
        (``2**-k``, exact in float), ``w`` its weight. Without ``antialias``
        — or at and below unity, whatever the setting — it is level 0
        alone at scale 1, i.e. the original read, untouched: that is what
        keeps the neutral bit-exact with the chain switched on.
        """
        if not antialias or len(chain) < 2:
            return [(chain[0], 1.0, 1.0)]
        from ..modules.sampler import mip_blend

        k0, k1, frac = mip_blend(rate, len(chain) - 1)
        if frac <= 0.0:
            return [(chain[k0], 2.0 ** -k0, 1.0)]
        return [(chain[k0], 2.0 ** -k0, 1.0 - frac), (chain[k1], 2.0 ** -k1, frac)]

    def _sampler_read_mix(self, reader, positions):
        """``_sampler_read`` through a mip reader (one or two levels)."""
        if len(reader) == 1 and reader[0][1] == 1.0:
            return self._sampler_read(reader[0][0], positions)
        out = None
        for buf, scale, w in reader:
            part = self._sampler_read(buf, positions * scale)
            if w != 1.0:
                part = part * w
            out = part if out is None else out + part
        return out

    def _new_sampler_state(self) -> dict:
        return {"path": None, "loaded_path": None, "samples": None,
                "chains": None, "overview": None,
                "pending": None, "V": 0, "voices": []}

    @staticmethod
    def _new_sampler_voice() -> dict:
        return {
            "active": False,       # a playhead is running
            "pos": 0.0,            # playhead, in samples into the file
            "rate": 1.0,           # samples advanced per output sample (signed)
            "start": 0.0,          # this voice's region start (start_cv latched)
            "gain": 1.0,           # `vel` latched at the edge
            "releasing": False,    # gated fall -> ramping out
            "rel_left": 0,         # release samples still to serve
            "rel_total": 0,
            "atk_left": 0,         # declick ramp-in samples still to serve
            "atk_total": 0,
            "xf_left": 0,          # retrigger tail still to serve
            "xf_total": 0,
            "xf_pos": 0.0,         # the abandoned playhead
            "xf_rate": 1.0,
            "xf_start": 0.0,
            "xf_gain": 1.0,
            "prev_gate": False,
        }

    @staticmethod
    def _sampler_advance(pos, rate, count, region, loop):
        """Where the playhead reads, where it ends up, and how much counts.

        Returns ``(positions, next_pos, live)``. Without a ``loop`` the
        playhead is affine — ``pos + rate·arange(count)`` — and eventually
        walks off the region ``(lo, hi)``: forward off ``hi`` (positions
        ``< hi`` play), in reverse off ``lo`` (positions ``>= lo`` play).
        Either way the inside part is a *prefix*, so ``live`` says how much
        of the segment is still in the region.

        With a loop it wraps ``loop_end`` back to ``loop_start`` instead
        (or the other way round in reverse) and so never leaves the region
        at all (``live`` is the whole segment — that is what looping
        means). The wrap is one modulo on the same affine array, which
        keeps it a single vectorized expression *and* keeps it exact on
        integers: a unity-rate loop of an integer-bounded region is a
        bit-exact tiling of the file, not an approximation of one — and a
        reversed one is a bit-exact tiling of the region mirrored.
        """
        raw = pos + rate * np.arange(count, dtype=np.float64)
        end_raw = float(pos + rate * count)
        forward = rate >= 0.0
        if loop is None:
            if forward:
                live = int(np.searchsorted(raw, float(region[1])))
            else:
                live = int(np.count_nonzero(raw >= float(region[0])))
            return raw, end_raw, live
        lo, hi, length = loop[0], loop[1], loop[2]
        if forward:
            over = raw - hi
            positions = np.where(over < 0.0, raw, lo + np.mod(over, length))
            if end_raw >= hi:
                end_raw = float(lo + np.mod(end_raw - hi, length))
        else:
            under = raw - lo
            positions = np.where(under >= 0.0, raw, lo + np.mod(under, length))
            if end_raw < lo:
                end_raw = float(lo + np.mod(end_raw - lo, length))
        return positions, end_raw, count

    def _sampler_seam(self, block, reader, positions, loop, forward=True):
        """Crossfade the loop's tail into the lap before it, in place.

        Over the last ``xfade`` of the loop the read is mixed with
        ``read(pos - loop_length)`` — the same point one lap earlier, which
        is the material running *into* ``loop_start``. The weight reaches 1
        exactly as the playhead reaches ``loop_end``, so the wrap lands on
        what was already sounding and the seam is continuous instead of a
        step (the resampler's seam-declick lesson: fade between two reads,
        never cut). In reverse the roles mirror: the fade sits just above
        ``loop_start`` and reads one lap *later*, which is what runs into
        ``loop_end`` from above.

        A ``loop_start`` at the very beginning of the file has no previous
        lap; the read clamps to the file's first sample, which is exactly
        where the wrap is about to land, so the fade stays continuous.

        Only the faded samples are read twice — everything else keeps the
        single read it already had, which is what leaves ``xfade`` 0
        bit-exact rather than merely close.
        """
        lo, hi, length, xfade = loop
        if xfade <= 0.0:
            return
        if forward:
            weight = (positions - (hi - xfade)) / xfade
        else:
            weight = ((lo + xfade) - positions) / xfade
        idx = np.flatnonzero(weight > 0.0)
        if idx.size == 0:
            return
        w = weight[idx]
        lap = positions[idx] - length if forward else positions[idx] + length
        prev = self._sampler_read_mix(reader, lap)
        block[idx] = block[idx] * (1.0 - w) + prev * w

    def _sampler_segment(self, voice, chains, count, end_sample, loop_for,
                         antialias):
        """Render ``count`` samples of one voice and advance its playhead.

        Everything vectorizes because the playhead is affine within a
        segment: ``pos + rate·arange(n)``. Segments are cut at gate edges
        by the caller, which is the only place ``rate``/state can change.
        ``chains`` is one mip chain per output channel (one for a mono
        file, two for stereo) and the result is ``(C, count)`` float64 —
        the same positions read through each. ``loop_for(start)`` gives
        the loop ``(lo, hi, length, xfade)`` in sample units for a voice
        that began at ``start``, or None for the modes that don't loop.
        """
        C = len(chains)
        out = np.zeros((C, count), dtype=np.float64)
        if count <= 0:
            return out

        if voice["active"]:
            rate = voice["rate"]
            forward = rate >= 0.0
            loop = loop_for(voice["start"])
            positions, next_pos, live = self._sampler_advance(
                voice["pos"], rate, count, (voice["start"], end_sample), loop
            )
            if live > 0:
                for c, chain in enumerate(chains):
                    reader = self._sampler_reader(chain, rate, antialias)
                    out[c, :live] = self._sampler_read_mix(reader, positions[:live])
                    if loop is not None:
                        self._sampler_seam(out[c], reader, positions, loop, forward)
                if voice["gain"] != 1.0:
                    out[:, :live] *= voice["gain"]
            voice["pos"] = next_pos
            if live < count:
                # Ran off the end of the region: silent from here, and the
                # voice costs nothing until it is retriggered. A looping
                # voice never reaches this — it wrapped instead.
                voice["active"] = False
                voice["releasing"] = False
                voice["rel_left"] = 0

            # Declick ramp in (retrigger, or a non-zero `attack`).
            if voice["atk_left"] > 0:
                n = min(count, voice["atk_left"])
                total = float(voice["atk_total"])
                done = total - voice["atk_left"]
                out[:, :n] *= (done + 1.0 + np.arange(n, dtype=np.float64)) / total
                voice["atk_left"] -= n

            # Gated release: a linear ramp out, then the voice is done.
            if voice["releasing"] and voice["rel_left"] > 0:
                n = min(count, voice["rel_left"])
                total = float(voice["rel_total"])
                done = total - voice["rel_left"]
                out[:, :n] *= 1.0 - (done + 1.0 + np.arange(n, dtype=np.float64)) / total
                if n < count:
                    out[:, n:] = 0.0
                voice["rel_left"] -= n
                if voice["rel_left"] <= 0:
                    voice["active"] = False
                    voice["releasing"] = False

        # The abandoned playhead of a retriggered voice, fading out under
        # the new one. Added, not blended: two reads of the same buffer sum
        # linearly, so equal-and-opposite ramps cross without a notch.
        if voice["xf_left"] > 0:
            n = min(count, voice["xf_left"])
            rate = voice["xf_rate"]
            forward = rate >= 0.0
            # The abandoned playhead keeps doing what the voice was doing,
            # loop, region and all, so a retrigger near the seam doesn't
            # drop out.
            loop = loop_for(voice["xf_start"])
            positions, next_xf, live = self._sampler_advance(
                voice["xf_pos"], rate, n, (voice["xf_start"], end_sample), loop
            )
            if live > 0:
                total = float(voice["xf_total"])
                done = total - voice["xf_left"]
                ramp = 1.0 - (done + 1.0 + np.arange(live, dtype=np.float64)) / total
                if voice["xf_gain"] != 1.0:
                    ramp = ramp * voice["xf_gain"]
                for c, chain in enumerate(chains):
                    reader = self._sampler_reader(chain, rate, antialias)
                    tail = self._sampler_read_mix(reader, positions[:live])
                    if loop is not None:
                        self._sampler_seam(tail, reader, positions[:live], loop, forward)
                    out[c, :live] += tail * ramp
            voice["xf_pos"] = next_xf
            voice["xf_left"] -= n
        return out

    def _render_sampler(self, module, frames: int, buffers, patch):
        """Pitched sample playback with per-voice playheads.

        See ``modules/sampler.py`` for the contract. Structure: the sample
        is decoded whole on a background thread (``_SampleLoader``, the
        convolver precedent) and never touched by the audio thread until
        ready, so a fresh or changed ``path`` is silence rather than a
        dropout. Each voice owns a float64 playhead; a block is cut into
        segments at that voice's gate edges, and within a segment the
        playhead is affine, so the whole segment is one vectorized cubic
        read (``_sampler_read``) per output channel.

        Rate comes from ``playback_rate`` in the module file — one
        definition of what ``root`` means, shared with the tests. Pitch is
        read per block (mean), the ``pluck`` precedent, so glides and
        vibrato track at block rate. ``reverse`` is the same rate with its
        sign flipped, and the playhead starts at the region's end.

        ``loop`` mode is ``gated`` plus a wrapped playhead: the loop
        region is resolved per voice into absolute sample positions
        (``loop_start``/``loop_end`` are fractions **of the region**, so
        moving ``start``/``end`` — or a ``start_cv`` hit — carries the
        loop with them) and handed to the segment renderer, which does
        the wrapping and the seam crossfade. A collapsed or inverted loop
        region resolves to ``None`` and the voice simply plays as
        ``gated`` — kinder than silence for something you drag with a
        slider.

        ``start_cv`` and ``vel`` are read **at the edge sample** and
        latched into the voice: each hit lands where the CV said at the
        moment it fired, which is what a sequencer-driven slicer needs.

        Outputs: ``out`` (mono), ``out_l`` / ``out_r``. A mono file is one
        read feeding all three; a stereo file reads both channels and
        ``out`` is their half-sum.

        Neutral: root pitch, ``start`` 0, ``end`` 1, ``attack`` 0,
        ``level`` 1 makes the rate exactly 1.0, every read position an
        integer, and the output the decoded buffer **bit-exact** — with or
        without ``antialias``, which only ever changes a read *above*
        unity. Nothing is faded at the region end for the same reason — a
        sample that stops abruptly is the file's business, and ``end``
        plus a gated ``release`` are the tools for trimming it. A
        unity-rate loop with ``loop_xfade`` 0 keeps that property: it is a
        bit-exact *tiling* of the file.
        """
        from ..modules.sampler import SAMPLER_MODES, playback_rate

        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        gate = self._input_buffer(
            patch, buffers, module.id, "gate", collapse=False
        )
        start_cv = self._input_buffer(
            patch, buffers, module.id, "start_cv", collapse=False
        )
        vel = self._input_buffer(
            patch, buffers, module.id, "vel", collapse=False
        )

        state = self._state.setdefault(module.id, self._new_sampler_state())

        # --- resolve path -> loaded buffer (never blocks the audio thread) ---
        path = self._resolve_media_path(module.params.get("path", ""))
        if path == "":
            if state.get("samples") is not None or state.get("loaded_path"):
                state["samples"] = None
                state["chains"] = None
                state["overview"] = None
                state["loaded_path"] = None
            pend = state.get("pending")
            if pend is not None:
                pend["loader"].close()
                state["pending"] = None
        else:
            pend = state.get("pending")
            if path != state.get("loaded_path") and (
                pend is None or pend.get("path") != path
            ):
                if pend is not None:
                    pend["loader"].close()
                state["pending"] = {
                    "path": path,
                    "loader": self._start_sample_loader(path),
                }
            pend = state.get("pending")
            if pend is not None and pend["loader"].done:
                loader = pend["loader"]
                # Failure is remembered as "this path yields silence" rather
                # than retried every block — a missing file must not spin a
                # decode thread per block.
                ok = loader.ready
                state["samples"] = loader.samples if ok else None
                state["chains"] = loader.chains if ok else None
                state["overview"] = loader.overview if ok else None
                state["loaded_path"] = pend["path"]
                state["pending"] = None

        samples = state.get("samples")
        chains = state.get("chains")

        def silence(shape):
            z = np.zeros(shape, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}

        # No gate cable means nothing can ever start: silence, and drop the
        # voice bank so a reconnect starts clean.
        if gate is None or samples is None or samples.shape[0] < 1 or not chains:
            state["voices"] = []
            state["V"] = 0
            return silence(frames)

        voiced = gate.ndim == 2
        V = 1
        for sig in (pitch, gate, start_cv, vel):
            if sig is not None and sig.ndim == 2:
                voiced = True
                V = max(V, sig.shape[0])

        def row(sig, v):
            if sig is None:
                return None
            if sig.ndim == 2:
                return sig[v] if v < sig.shape[0] else sig[0]
            return sig

        # --- params ---------------------------------------------------------
        def _f(name, default, lo, hi):
            try:
                value = float(module.params.get(name, default))
            except (TypeError, ValueError):
                value = default
            return min(hi, max(lo, value))

        root = _f("root", 60.0, 0.0, 127.0)
        tune = _f("tune", 0.0, -12.0, 12.0)
        fine = _f("fine", 0.0, -50.0, 50.0)
        level = _f("level", 0.8, 0.0, 1.0)
        start_frac = _f("start", 0.0, 0.0, 1.0)
        end_frac = _f("end", 1.0, 0.0, 1.0)
        loop_start_frac = _f("loop_start", 0.0, 0.0, 1.0)
        loop_end_frac = _f("loop_end", 1.0, 0.0, 1.0)
        loop_xfade_ms = _f("loop_xfade", 10.0, 0.0, 100.0)
        attack_ms = _f("attack", 0.0, 0.0, 500.0)
        release_ms = _f("release", 10.0, 1.0, 2000.0)
        start_depth = _f("start_cv_depth", 1.0, -1.0, 1.0)
        reverse = bool(module.params.get("reverse", False))
        antialias = bool(module.params.get("antialias", False))
        mode = str(module.params.get("mode", "one_shot"))
        if mode not in SAMPLER_MODES:
            mode = "one_shot"

        n_samples = int(samples.shape[0])
        start_sample = float(start_frac) * n_samples
        end_sample = float(end_frac) * n_samples
        if end_sample <= start_sample:
            # A collapsed or inverted region plays nothing rather than
            # running backwards off the front of the buffer.
            return silence((V, frames) if voiced else frames)

        sr = float(self.sample_rate)
        attack_n = int(round(attack_ms * 1e-3 * sr))
        release_n = max(1, int(round(release_ms * 1e-3 * sr)))
        xfade_n = max(1, int(round(self._SAMPLER_XFADE_MS * 1e-3 * sr)))
        seam_n = float(int(round(loop_xfade_ms * 1e-3 * sr)))

        # The loop region, in absolute samples, for a voice that began at
        # `vstart`. `loop_start`/`loop_end` are fractions OF THE REGION,
        # not of the file, so dragging `start`/`end` (or a start_cv hit)
        # carries the loop along instead of stranding it. None means
        # "don't loop", which is also what a collapsed or inverted loop
        # region gets: play as `gated` rather than fall silent, because
        # this is a thing you drag with a slider.
        def loop_for(vstart):
            if mode != "loop":
                return None
            span = end_sample - vstart
            loop_lo = vstart + loop_start_frac * span
            loop_hi = vstart + loop_end_frac * span
            loop_len = loop_hi - loop_lo
            if loop_len <= 0.0:
                return None
            # Measured on the sample (so it covers the same slice of
            # waveform whatever the pitch) and clamped to the loop: there
            # is only one lap to fade into.
            return (loop_lo, loop_hi, loop_len, min(seam_n, loop_len))

        voices = state.get("voices") or []
        if state.get("V") != V or len(voices) != V:
            voices = [self._new_sampler_voice() for _ in range(V)]
            state["V"] = V
            state["voices"] = voices

        gate_high = self._GATE_HIGH
        C = len(chains)
        out = np.zeros((C, V, frames), dtype=np.float64)

        for v in range(V):
            voice = voices[v]
            g = np.asarray(row(gate, v)) > gate_high
            pv = row(pitch, v)
            cv = self._finite_mean(pv) if pv is not None else 0.0
            rate = playback_rate(cv, root, tune, fine)
            if reverse:
                rate = -rate
            sc = row(start_cv, v)
            vl = row(vel, v)

            prev = voice["prev_gate"]
            shifted = np.empty(frames, dtype=bool)
            shifted[0] = prev
            shifted[1:] = g[:-1]
            rising = np.flatnonzero(g & ~shifted)
            falling = (
                np.flatnonzero(~g & shifted) if mode in ("gated", "loop")
                else np.empty(0, dtype=np.int64)
            )
            voice["prev_gate"] = bool(g[-1]) if frames else prev

            events = sorted(
                [(int(i), "on") for i in rising] + [(int(i), "off") for i in falling]
            )

            row_out = out[:, v, :]
            cursor = 0
            for at, kind in events:
                if at > cursor:
                    row_out[:, cursor:at] = self._sampler_segment(
                        voice, chains, at - cursor, end_sample, loop_for, antialias
                    )
                    cursor = at
                if kind == "on":
                    # Where this hit starts: `start`, moved by start_cv AT
                    # THE EDGE SAMPLE (a slicer wants the value the
                    # sequencer had when it fired, not the block's mean).
                    sfrac = start_frac
                    if sc is not None:
                        sfrac = min(1.0, max(0.0, sfrac + start_depth * float(sc[at])))
                    vstart = sfrac * n_samples
                    if vstart >= end_sample:
                        # Pushed past the end: nothing to play. The hit is
                        # dropped rather than run backwards, and whatever
                        # was sounding is left alone.
                        continue
                    gain = 1.0
                    if vl is not None:
                        gain = max(0.0, float(vl[at]))
                    if voice["active"]:
                        # Hand the sounding playhead to the crossfade tail
                        # before the new one takes over.
                        voice["xf_pos"] = voice["pos"]
                        voice["xf_rate"] = voice["rate"]
                        voice["xf_start"] = voice["start"]
                        voice["xf_gain"] = voice["gain"]
                        voice["xf_left"] = xfade_n
                        voice["xf_total"] = xfade_n
                        ramp_in = max(attack_n, xfade_n)
                    else:
                        ramp_in = attack_n
                    voice["active"] = True
                    voice["start"] = vstart
                    # Reverse begins one sample inside the region's end, so
                    # a unity read is the forward read mirrored exactly.
                    voice["pos"] = (end_sample - 1.0) if reverse else vstart
                    voice["rate"] = rate
                    voice["gain"] = gain
                    voice["releasing"] = False
                    voice["rel_left"] = 0
                    voice["atk_left"] = ramp_in
                    voice["atk_total"] = max(1, ramp_in)
                else:  # gated fall
                    if voice["active"] and not voice["releasing"]:
                        voice["releasing"] = True
                        voice["rel_left"] = release_n
                        voice["rel_total"] = release_n
            if cursor < frames:
                row_out[:, cursor:] = self._sampler_segment(
                    voice, chains, frames - cursor, end_sample, loop_for, antialias
                )
            # A held voice tracks pitch between triggers (block-rate glide);
            # the retrigger above locks the rate at the edge sample.
            if voice["active"] and not voice["releasing"]:
                voice["rate"] = rate

        out *= level
        if C == 1:
            mono = out[0].astype(np.float32)
            if not voiced:
                mono = mono[0]
            return {"out": mono, "out_l": mono.copy(), "out_r": mono.copy()}
        left = out[0]
        right = out[1]
        mono = 0.5 * (left + right)
        if voiced:
            return {
                "out": mono.astype(np.float32),
                "out_l": left.astype(np.float32),
                "out_r": right.astype(np.float32),
            }
        return {
            "out": mono[0].astype(np.float32),
            "out_l": left[0].astype(np.float32),
            "out_r": right[0].astype(np.float32),
        }

    def _start_sample_loader(self, path):
        """Spawn a background whole-file decode (plus mip chain) for ``path``."""
        from ..modules.sampler import (
            HALFBAND_TAPS,
            MAX_SECONDS,
            MIP_LEVELS,
            MIP_MIN_SAMPLES,
            OVERVIEW_COLS,
        )

        return _SampleLoader(
            path, self.sample_rate, self._decode_audio, MAX_SECONDS,
            mip_levels=MIP_LEVELS, mip_min=MIP_MIN_SAMPLES,
            halfband_taps=HALFBAND_TAPS, overview_cols=OVERVIEW_COLS,
        )

    def sampler_overview(self, module_id: int):
        """(GUI hook) The loaded sample's waveform overview for the face.

        Returns ``(overview, loaded_path)`` — a ``(cols, 2)`` float32
        column min/max array built by the loader, and the path it belongs
        to (the face uses it as its "already painted?" key) — or
        ``(None, None)`` while nothing is loaded. GUI thread only; a
        couple of dict reads.
        """
        st = self._state.get(module_id)
        if not isinstance(st, dict):
            return None, None
        return st.get("overview"), st.get("loaded_path")

    def wait_for_sample_loads(self, timeout: float = 10.0) -> bool:
        """Block until every sampler's pending load finishes. Tests only.

        Never call from the audio thread. Returns True when every pending
        load finished with a usable buffer (no pending load counts as
        trivially ready, matching the render-silence contract).
        """
        import time as _time

        deadline = _time.monotonic() + float(timeout)
        ok = True
        for st in list(self._state.values()):
            if not isinstance(st, dict):
                continue
            pend = st.get("pending")
            loader = pend.get("loader") if isinstance(pend, dict) else None
            if loader is None or not isinstance(loader, _SampleLoader):
                continue
            remaining = max(0.0, deadline - _time.monotonic())
            ok = loader.wait(remaining) and ok
        return ok

    def _render_pluck(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Extended Karplus–Strong (see modules/pluck.py for the contract).

        Per voice, the string is a ring buffer advanced in CHUNKS of at
        most one loop length: within a chunk every read (the ``N`` and
        ``N+1`` taps) lands before this chunk's writes, so the whole
        chunk vectorizes — the damping one-zero is an array blend and the
        allpass fractional delay is one ``lfilter`` call with carried
        ``zi``. Low notes take one or two chunks per block; high notes
        degrade gracefully into more, smaller chunks instead of a
        per-sample loop.

        Tuning: the loop's effective delay is ``N_int + frac + d/2``
        samples — the damping blend ``(1−d/2) + (d/2)z⁻¹`` contributes a
        ``d/2``-sample phase delay, compensated when splitting
        ``sr/f0`` into integer + allpass fraction (``frac`` kept in
        [0.1, 1.1) so the allpass coefficient stays well-conditioned).
        Loop gain ``g = 10^(−3·N/(sr·decay))`` makes ``decay`` read as a
        real t60 independent of pitch.

        Triggers segment the block: at each rising edge the pluck pitch
        is locked from that sample, coefficients rebuilt, the allpass
        state carried (``carry``, the default; cleared with it off),
        and a seeded exciter burst is **added** into the ring (a
        re-pluck superposes on the ringing string — linear loop, so
        click-free by construction; exactly, with ``carry`` on).
        Between triggers an active voice
        re-reads the block-mean pitch each block (glides track at block
        rate). Determinism: each burst's rng is seeded from (module id,
        voice, hit number). Exact block-size independence holds under
        constant pitch (coefficients then rebuild identically).

        Velocity (2026-09-19, love pass): ``vel`` is a knobless
        multiplier on the BURST, read at the trigger's rising-edge sample
        and latched into that hit (the drums' / adsr's edge-latch rule);
        the loop -- decay, damping, tuning -- is untouched, so a soft hit
        is a quieter pluck that rings down the same way. Voice-aware like
        the other inputs: a ``(V, F)`` bus (``midi_input.velocity_cv``)
        latches per voice from its own row, a mono bus is shared, and a
        ``(V, F)`` bus on a mono pluck collapses to the loudest voice at
        the edge. Unpatched the code path is the old one verbatim (no
        multiply), and a bus holding 1.0 is bit-for-bit the same since
        ``x * 1.0 == x``. A non-positive velocity is a SILENT hit: not
        just a zero burst -- every hit also relocks the pitch and (with
        ``carry`` off) clears the allpass state; that clear alone steps
        the output by ~7-18% of the ring's amplitude (measured), an
        audible tick on a hit that is supposed to make no sound. So a
        silent hit leaves
        the string exactly as it was and only advances the hit counter.

        ``vel_color`` (2026-09-20, love pass): the pick's SPECTRUM follows
        the velocity too. The effective colour of a hit is
        ``clamp(color + vel_color * (vel - 1), 0, 1)``, computed at the
        edge from the SAME latched ``scale`` the burst is multiplied by,
        so it is per voice and per hit for free and can never move
        mid-note. The formula is anchored at vel 1.0 on purpose: a full
        hit (or an unpatched ``vel``) is ``color + 0.0``, which IS
        ``color`` in IEEE, so the knob cannot change the sound of a patch
        with no velocity source -- ``vel_color`` only ever DULLS a soft
        hit (and brightens a hit above 1.0, to the clamp). Only the
        exciter's one-pole sees it; the loop -- decay, damping, tuning --
        stays untouched, so a soft hit is quieter AND duller and still
        rings down exactly the same way.

        ``vel_position`` (2026-09-22, love pass): the pick's PLACE follows
        the velocity too -- a gentle finger-pluck lands nearer the middle
        of the string than a hard plectrum stroke near the bridge. The
        effective pick position of a hit is

            clamp(position + vel_position * (1 - vel) * (0.5 - position), 0, 1)

        -- an interpolation from ``position`` towards 0.5, by
        ``vel_position * (1 - vel)`` of the way. 0.5 is where the comb is
        dullest (``d = n/2``: the first null lands on the SECOND harmonic,
        so the fundamental dominates and the even partials go); ``position``
        small is near the bridge (the first null is at ``f0/position``, far
        up -- bright and nasal). Latched at the edge from the SAME velocity
        the burst rides, like ``vel_color``, so it is per voice and per hit.
        Anchored at vel 1.0 the same way -- ``(1 - 1.0)`` is ``0.0`` and
        ``position + 0.0 * x`` IS ``position`` in IEEE -- so a full hit, or
        an unpatched ``vel``, is exactly ``position`` whatever the knob
        says. A hit ABOVE velocity 1 slides the other way, towards the
        bridge, to the clamp. ``position`` 0 is the documented OFF for the
        comb and stays off: the knob moves a pick, it does not fit one.

        ``carry`` (2026-09-22, love pass; default ON since 2026-09-24):
        whether a hit
        CARRIES the loop's allpass (fractional-delay) state instead of
        clearing it. Measured, at C4, ``decay`` 3, ``damping`` 0, a
        re-pluck 0.5 s in:

          * the clear is a step of **51.3% of the ring's amplitude** at the
            same pitch, 51.4% into G4, 51.5% into C5 -- measured with a
            ZERO burst, so it is the artifact alone. The fraction depends
            on WHERE in the waveform the hit lands (the clear throws away
            one sample of loop state, and a sample is not an envelope):
            another re-pluck instant measures 28%, the velocity pass
            measured 7-18% on its own hits. It is never small;
          * carrying the state, that step is **exactly 0.000%** -- and the
            relock a hit performs is then bit-identical to the block-mean
            pitch follow the string does anyway, at any pitch;
          * carried, a re-pluck is EXACT superposition: two hits minus one
            hit equals the second hit alone to 3e-08 (one float32 ulp),
            against 51% with the clear. The module has always CLAIMED that
            ("the loop is linear, so plucks superpose") -- the clear is the
            one thing that made it false;
          * tuning is untouched either way (G4 -0.95 ct, C5 +0.84 ct,
            identical to the milli-hertz), and so is the peak; 200 rapid
            re-plucks over four octaves stay finite and bounded both ways.

        So the clear buys nothing and costs half the ring. It shipped as
        the default for two days (turning it off changes the sound of
        every patch that re-plucks a ringing string, so it waited for
        Matthew's go-ahead); since 2026-09-24 ``carry`` defaults True
        and False is the old behaviour, kept bit-exact.
        """
        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        trig = self._input_buffer(
            patch, buffers, module.id, "trigger", collapse=False
        )
        if pitch is None and trig is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)
        # The velocity bus goes down whole: it is only ever READ at a
        # rising-edge sample, and each voice picks its own row below.
        vel = self._input_buffer(
            patch, buffers, module.id, "vel", collapse=False
        )

        voiced = (pitch is not None and pitch.ndim == 2) or (
            trig is not None and trig.ndim == 2
        )
        V = 1
        for sig in (pitch, trig):
            if sig is not None and sig.ndim == 2:
                V = max(V, sig.shape[0])

        def row(sig, v):
            if sig is None:
                return None
            if sig.ndim == 2:
                return sig[v] if v < sig.shape[0] else sig[0]
            return sig

        try:
            decay = float(module.params.get("decay", 2.0))
        except (TypeError, ValueError):
            decay = 2.0
        decay = min(30.0, max(0.1, decay))
        damping = min(1.0, max(0.0, float(module.params.get("damping", 0.5))))
        color = min(1.0, max(0.0, float(module.params.get("color", 0.7))))
        # vel_color: how far a soft hit's burst darkens from ``color``
        # (0 = off, the shipped sound). Applied per hit at the edge below,
        # beside the latched velocity it rides on.
        vel_color = min(
            1.0, max(0.0, float(module.params.get("vel_color", 0.0)))
        )
        # vel_position: how far a soft hit's pick slides towards the
        # middle of the string (0 = off, the shipped sound). Applied per
        # hit at the edge, from the same latched velocity.
        vel_position = min(
            1.0, max(0.0, float(module.params.get("vel_position", 0.0)))
        )
        # carry: keep the loop's allpass state through a hit (default
        # True, since 2026-09-24). False is the old behaviour: every
        # hit clears it, a 51%-of-the-ring step (docstring).
        carry = bool(module.params.get("carry", True))
        position = min(1.0, max(0.0, float(module.params.get("position", 0.2))))
        level = float(module.params.get("level", 0.5))

        sr = float(self.sample_rate)
        maxlen = int(sr / self._PLUCK_MIN_F0) + 4

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V or st.get("maxlen") != maxlen:
            st.clear()
            st.update(
                {
                    "V": V,
                    "maxlen": maxlen,
                    "ring": np.zeros((V, maxlen), dtype=np.float64),
                    "widx": np.zeros(V, dtype=np.int64),
                    "n_int": np.full(V, 100, dtype=np.int64),
                    "ap_c": np.zeros(V, dtype=np.float64),
                    "ap_z": np.zeros(V, dtype=np.float64),
                    "g": np.ones(V, dtype=np.float64),
                    "prev_trig": np.zeros(V, dtype=bool),
                    "hits": np.zeros(V, dtype=np.int64),
                    "active": np.zeros(V, dtype=bool),
                }
            )
        ring = st["ring"]

        a0 = 1.0 - damping / 2.0  # damping one-zero: a0 + a1·z⁻¹
        a1 = damping / 2.0

        def set_coeffs(v: int, cv_value: float) -> None:
            f0 = self._PLUCK_C4 * (2.0 ** float(cv_value))
            f0 = min(sr / 4.0, max(self._PLUCK_MIN_F0, f0))
            n_target = sr / f0
            n_eff = n_target - damping / 2.0  # compensate the filter delay
            n_int = int(n_eff - 0.1)
            frac = n_eff - n_int  # in [0.1, 1.1)
            if n_int < 2:
                n_int, frac = 2, max(0.1, n_eff - 2)
            st["n_int"][v] = n_int
            st["ap_c"][v] = (1.0 - frac) / (1.0 + frac)
            st["g"][v] = 10.0 ** (-3.0 * n_target / (sr * decay))

        out = np.zeros((V, frames), dtype=np.float64)
        gate_high = self._GATE_HIGH

        for v in range(V):
            p_row = row(pitch, v)
            t_row = row(trig, v)
            # This voice's velocity: its own row of a (V, F) bus (or the
            # shared mono bus) when the pluck is voiced; the whole buffer
            # when the pluck is mono, collapsing at the read to the
            # loudest voice at the edge (a velocity bus carries 0 on idle
            # slots, so the max is the key that was struck). None when
            # unpatched -- the old code path, untouched.
            if vel is None:
                v_row = None
            elif voiced:
                v_row = row(vel, v)
            else:
                v_row = vel

            if t_row is not None:
                gt = t_row > gate_high
                prev = np.empty_like(gt)
                prev[0] = bool(st["prev_trig"][v])
                prev[1:] = gt[:-1]
                edges = np.flatnonzero(gt & ~prev).tolist()
                st["prev_trig"][v] = bool(gt[-1])
            else:
                edges = []

            if not edges and not st["active"][v]:
                continue  # silent string: free

            if st["active"][v] and not edges:
                # Follow the block-mean pitch (glide/vibrato, block rate).
                set_coeffs(v, float(np.mean(p_row)) if p_row is not None else 0.0)

            # Segment the block at trigger edges: advance up to each edge
            # with the old state, then pluck and continue.
            bounds = edges + [frames]
            seg_start = 0
            for i, seg_end in enumerate(bounds):
                if seg_end > seg_start:
                    self._pluck_advance(
                        st, v, ring, out[v], seg_start, seg_end, a0, a1
                    )
                if i < len(edges):
                    e = edges[i]
                    if v_row is not None:
                        # Velocity: read AT the edge sample, latched into
                        # this hit. Non-positive = a silent hit: the
                        # string keeps ringing exactly as it was (no
                        # burst, no relock, no allpass clear -- see the
                        # docstring for why the clear alone would tick);
                        # only the hit counter advances (a hit is a hit).
                        scale = max(0.0, self._drum_edge_value(v_row, e, 1.0))
                        if scale <= 0.0:
                            st["hits"][v] += 1
                            seg_start = e
                            continue
                    cv_at = float(p_row[e]) if p_row is not None else 0.0
                    set_coeffs(v, cv_at)
                    n_int = int(st["n_int"][v])
                    rng = np.random.default_rng(
                        (self._PLUCK_SEED, module.id, v, int(st["hits"][v]))
                    )
                    st["hits"][v] += 1
                    hit_color = color
                    if v_row is not None and vel_color > 0.0:
                        # The pick's spectrum follows the latched
                        # velocity: a soft hit's burst is lowpassed
                        # harder, in proportion. Anchored at 1.0 so a
                        # full hit is ``color + 0.0`` -- bit-exactly
                        # ``color`` -- and the knob is inert without a
                        # velocity source. Clamped like ``color`` itself.
                        hit_color = min(
                            1.0, max(0.0, color + vel_color * (scale - 1.0))
                        )
                    hit_position = position
                    if (
                        v_row is not None
                        and vel_position > 0.0
                        and position > 0.0
                    ):
                        # The pick's PLACE follows the latched velocity: a
                        # soft hit lands nearer the middle of the string,
                        # where the comb is dullest and the fundamental
                        # strongest -- a finger, not a plectrum by the
                        # bridge. Anchored at 1.0 like ``vel_color``, so a
                        # full hit is ``position + 0.0 * x`` -- bit-exactly
                        # ``position`` -- and the knob is inert without a
                        # velocity source. ``position`` 0 is the comb's
                        # documented OFF and stays off: this knob moves a
                        # pick, it does not fit one.
                        hit_position = min(
                            1.0,
                            max(
                                0.0,
                                position
                                + vel_position * (1.0 - scale) * (0.5 - position),
                            ),
                        )
                    burst = _pluck_exciter(n_int, hit_color, hit_position, rng)
                    if v_row is not None:
                        # The burst scales; the loop does not, so a soft
                        # hit rings down the same way.
                        burst *= scale
                    # Add the burst into the last n_int ring positions so
                    # the loop reads it starting at the edge sample.
                    w = int(st["widx"][v])
                    start = (w - n_int) % st["maxlen"]
                    first = min(n_int, st["maxlen"] - start)
                    ring[v, start : start + first] += burst[:first]
                    if n_int > first:
                        ring[v, : n_int - first] += burst[first:]
                    if not carry:
                        # The allpass state is the one thing a hit does
                        # NOT superpose. Measured at 51% of the ring
                        # (docstring); ``carry`` keeps it, and then a
                        # re-pluck is exact superposition.
                        st["ap_z"][v] = 0.0
                    st["active"][v] = True
                    seg_start = e
            # Early-out bookkeeping: a decayed string goes fully silent.
            if st["active"][v] and not edges:
                if float(np.max(np.abs(out[v]))) < self._PLUCK_SILENCE:
                    st["active"][v] = False
                    ring[v, :] = 0.0
                    out[v, :] = 0.0
                    # Zero the allpass state with the ring, so a fresh
                    # pluck on a dead string is a fresh pluck under
                    # ``carry`` too. Render-neutral with ``carry`` off:
                    # nothing reads ``ap_z`` while a voice is inactive,
                    # and the next hit clears it anyway.
                    st["ap_z"][v] = 0.0

        out *= level
        result = out if voiced else out[0]
        return result.astype(np.float32)

    def _pluck_advance(
        self, st, v: int, ring, out_row, start: int, end: int, a0, a1
    ) -> None:
        """Advance one voice's string loop over out_row[start:end]."""
        if not st["active"][v]:
            return  # nothing ringing; output stays zero
        maxlen = int(st["maxlen"])
        n_int = int(st["n_int"][v])
        c = float(st["ap_c"][v])
        g = float(st["g"][v])
        w = int(st["widx"][v])
        z = float(st["ap_z"][v])
        row = ring[v]
        pos = start
        while pos < end:
            chunk = min(end - pos, n_int)
            # Delayed taps (all strictly before this chunk's writes).
            i0 = (np.arange(chunk) + (w - n_int)) % maxlen
            x0 = row[i0]
            x1 = row[(i0 - 1) % maxlen]
            vsig = a0 * x0 + a1 * x1
            y, zf = lfilter([c, 1.0], [1.0, c], vsig, zi=np.array([z]))
            z = float(zf[0])
            y *= g
            i_w = (np.arange(chunk) + w) % maxlen
            row[i_w] = y
            out_row[pos : pos + chunk] = y
            w = (w + chunk) % maxlen
            pos += chunk
        st["widx"][v] = w
        st["ap_z"][v] = z

    # ----- Bowed string rendering ------------------------------------------

    def _render_bowed(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Bowed-string waveguide (see modules/bowed.py for the contract).

        Per voice: two delay lines (bridge side ``beta x L``, nut side the
        rest) meeting at the bow. Each sample the string velocity at the
        bow is ``-lowpass(bridge_out) - nut_out``; the bow injects
        ``dv x table(dv)`` (STK's friction table, ``dv`` = bow velocity
        minus string velocity) into both halves. The loop is advanced in
        vectorized CHUNKS no longer than the shorter delay (the pluck
        precedent, now with a nonlinearity inside the loop -- elementwise,
        so it chunks the same way); the bridge one-pole is one ``lfilter``
        per chunk with carried ``zi``. Delay lines are slice buffers with
        history compacted once per block (no per-chunk modulo).

        Tuning: bridge + nut delay = ``sr/f0 - tau(f0)`` where tau is the
        one-pole's exact phase delay at f0; the fraction rides the bridge
        side as a linear-interpolation read (the nut side is integer).
        The bow envelope is an integer-count ramp per voice (attack from
        the gate's rising edge, release from the falling edge, a re-bow
        picking up from the current level), so renders are block-size
        independent at constant pitch and never click. Pressure and
        velocity CVs are per-sample arrays sliced per chunk. Pitch is
        read at the block's last rising edge if there is one, else the
        block mean (glides at block rate). Body: five constant-peak
        bandpasses in parallel, mixed by ``body``. A lifted string whose
        output has decayed early-outs to exact zeros.
        """
        pitch = self._input_buffer(patch, buffers, module.id, "pitch_cv", collapse=False)
        gate = self._input_buffer(patch, buffers, module.id, "gate", collapse=False)
        if pitch is None and gate is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)
        p_cv = self._input_buffer(patch, buffers, module.id, "pressure_cv")
        v_cv = self._input_buffer(patch, buffers, module.id, "velocity_cv")

        voiced = (pitch is not None and pitch.ndim == 2) or (
            gate is not None and gate.ndim == 2
        )
        V = 1
        for sig in (pitch, gate):
            if sig is not None and sig.ndim == 2:
                V = max(V, sig.shape[0])

        def row(sig, v):
            if sig is None:
                return None
            if sig.ndim == 2:
                return sig[v] if v < sig.shape[0] else sig[0]
            return sig

        def fparam(name, default, lo, hi):
            try:
                x = float(module.params.get(name, default))
            except (TypeError, ValueError):
                x = default
            return min(hi, max(lo, x))

        pressure = fparam("pressure", 0.5, 0.0, 1.0)
        velocity = fparam("velocity", 0.6, 0.0, 1.0)
        beta = fparam("position", 0.127, 0.05, 0.5)
        attack = fparam("attack", 0.05, 0.001, 10.0)
        release = fparam("release", 0.15, 0.001, 10.0)
        damping = fparam("damping", 0.5, 0.0, 1.0)
        body = fparam("body", 0.5, 0.0, 1.0)
        depth = fparam("cv_depth", 1.0, -10.0, 10.0)
        level = fparam("level", 0.5, 0.0, 1.0)

        sr = float(self.sample_rate)
        maxlen = int(sr / self._BOW_MIN_F0) + 8
        n_body = len(self._BOW_BODY)

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V or st.get("maxlen") != maxlen or st.get("block") != frames:
            st.clear()
            buflen = maxlen + frames + 4
            st.update({
                "V": V, "maxlen": maxlen, "block": frames, "buflen": buflen,
                "bridge": np.zeros((V, buflen), dtype=np.float64),
                "neck": np.zeros((V, buflen), dtype=np.float64),
                "w": np.full(V, maxlen, dtype=np.int64),
                "lp_z": np.zeros(V, dtype=np.float64),
                "body_z": np.zeros((V, n_body, 2), dtype=np.float64),
                "prev_gate": np.zeros(V, dtype=bool),
                "on_count": np.zeros(V, dtype=np.int64),
                "off_count": np.zeros(V, dtype=np.int64),
                "env_off": np.zeros(V, dtype=np.float64),
                "active": np.zeros(V, dtype=bool),
            })
        bridge = st["bridge"]
        neck = st["neck"]
        buflen = int(st["buflen"])

        pole = self._BOW_POLE_LO + (self._BOW_POLE_HI - self._BOW_POLE_LO) * damping
        lp_b = np.array([self._BOW_LOSS * (1.0 - pole)])
        lp_a = np.array([1.0, -pole])
        att_n = max(1, int(round(attack * sr)))
        rel_n = max(1, int(round(release * sr)))
        gate_high = self._GATE_HIGH

        # Per-sample bow hands (mono, shared by every voice).
        if p_cv is not None:
            slope = self._BOW_SLOPE_MAX - self._BOW_SLOPE_RANGE * np.clip(
                pressure + depth * p_cv.astype(np.float64), 0.0, 1.0)
        else:
            slope = np.full(frames, self._BOW_SLOPE_MAX - self._BOW_SLOPE_RANGE * pressure)
        if v_cv is not None:
            maxvel = self._BOW_VEL_MIN + self._BOW_VEL_RANGE * np.clip(
                velocity + depth * v_cv.astype(np.float64), 0.0, 1.0)
        else:
            maxvel = np.full(frames, self._BOW_VEL_MIN + self._BOW_VEL_RANGE * velocity)

        body_coefs = None
        if body > 0.0:
            body_coefs = [
                (self._bow_bp_coeffs(f, q), g) for f, q, g in self._BOW_BODY
            ]

        out = np.zeros((V, frames), dtype=np.float64)
        for v in range(V):
            p_row = row(pitch, v)
            g_row = row(gate, v)
            if g_row is not None:
                gt = g_row > gate_high
            else:
                gt = np.zeros(frames, dtype=bool)
            prev = bool(st["prev_gate"][v])
            rising = np.flatnonzero(gt & ~np.concatenate(([prev], gt[:-1])))
            if rising.size:
                st["active"][v] = True
            if not st["active"][v]:
                st["prev_gate"][v] = bool(gt[-1])
                continue  # a lifted, silent string is free

            # --- the bow envelope: integer-count ramps, segmented at edges
            env, on_count, off_count, env_off = self._gate_ramp_env(
                gt, prev, int(st["on_count"][v]), int(st["off_count"][v]),
                float(st["env_off"][v]), att_n, rel_n)
            st["on_count"][v] = on_count
            st["off_count"][v] = off_count
            st["env_off"][v] = env_off
            st["prev_gate"][v] = bool(gt[-1])
            bow_vel = maxvel * env

            # --- pitch -> delays (per block)
            if p_row is None:
                cv_val = 0.0
            elif rising.size:
                cv_val = float(p_row[rising[-1]])
            else:
                cv_val = self._finite_mean(p_row)
            f0 = self._BOW_C4 * self._pow2_clipped(cv_val)
            f0 = min(self._BOW_MAX_F0, max(self._BOW_MIN_F0, f0))
            w0 = 2.0 * np.pi * f0 / sr
            tau = float(np.arctan2(pole * np.sin(w0), 1.0 - pole * np.cos(w0)) / w0)
            L = sr / f0 - tau
            Ln = max(2, int(round((1.0 - beta) * L)))
            Lb = max(2.0, L - Ln)
            Db_i = int(Lb)
            Db_f = Lb - Db_i
            chunk_max = max(1, min(Db_i, Ln))

            # --- compact the slice buffers once per block
            w = int(st["w"][v])
            if w + frames > buflen:
                bridge[v, :maxlen] = bridge[v, w - maxlen:w]
                neck[v, :maxlen] = neck[v, w - maxlen:w]
                w = maxlen
            b_row = bridge[v]
            n_row = neck[v]
            z = np.array([st["lp_z"][v]])
            o_row = out[v]

            pos = 0
            while pos < frames:
                n = min(chunk_max, frames - pos)
                rb = w - Db_i
                b0 = b_row[rb:rb + n]
                bridge_out = b0 + Db_f * (b_row[rb - 1:rb - 1 + n] - b0)
                nut_out = n_row[w - Ln:w - Ln + n]
                filt, z = lfilter(lp_b, lp_a, bridge_out, zi=z)
                vd = bow_vel[pos:pos + n] + filt + nut_out   # bow - (-filt - nut)
                tbl = np.abs(vd * slope[pos:pos + n])
                tbl += 0.75
                np.power(tbl, -4.0, out=tbl)
                np.minimum(tbl, 1.0, out=tbl)
                new_vel = vd * tbl
                n_row[w:w + n] = new_vel - filt
                b_row[w:w + n] = new_vel - nut_out
                o_row[pos:pos + n] = bridge_out
                w += n
                pos += n
            st["w"][v] = w
            st["lp_z"][v] = float(z[0])

            # --- body resonances
            if body_coefs is not None:
                wet = np.zeros(frames, dtype=np.float64)
                for i, ((bc, ac), g) in enumerate(body_coefs):
                    y, zf = lfilter(bc, ac, o_row, zi=st["body_z"][v, i])
                    st["body_z"][v, i] = zf
                    wet += g * y
                o_row *= (1.0 - body)
                o_row += body * self._BOW_BODY_GAIN * wet

            # --- early-out: bow lifted, envelope gone, string decayed
            if not gt[-1] and env[-1] <= 0.0 and float(np.max(np.abs(o_row))) < self._BOW_SILENCE:
                st["active"][v] = False
                bridge[v, :] = 0.0
                neck[v, :] = 0.0
                st["lp_z"][v] = 0.0
                st["body_z"][v, :, :] = 0.0
                st["on_count"][v] = 0
                st["off_count"][v] = 0
                st["env_off"][v] = 0.0
                o_row[:] = 0.0

        out *= level * self._BOW_OUT_GAIN
        result = out if voiced else out[0]
        return result.astype(np.float32)

    def _bow_bp_coeffs(self, freq: float, q: float):
        """One RBJ constant-0dB-peak bandpass as (b, a) arrays."""
        w0 = 2.0 * np.pi * min(freq, 0.45 * self.sample_rate) / self.sample_rate
        alpha = np.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        b = np.array([alpha / a0, 0.0, -alpha / a0])
        a = np.array([1.0, -2.0 * np.cos(w0) / a0, (1.0 - alpha) / a0])
        return b, a

    @staticmethod
    def _gate_ramp_env(gt, prev: bool, on_count: int, off_count: int,
                       env_off: float, att_n: int, rel_n: int):
        """A linear attack/release ramp driven by a gate row, as integer
        counts from the edges (bit-exact across block sizes).

        While the gate is high the envelope is ``min(1, on_count/att_n)``
        with ``on_count`` counting samples since the rising edge; after a
        falling edge it is ``env_off * max(0, 1 - off_count/rel_n)`` from
        the level it had. A rising edge mid-release restarts the attack
        count FROM the current level (``on_count = env * att_n``) so a
        fast re-articulation never jumps. Returns the per-sample envelope
        and the carried ``(on_count, off_count, env_off)``.
        """
        frames = len(gt)
        env = np.empty(frames, dtype=np.float64)
        changes = np.flatnonzero(gt != np.concatenate(([prev], gt[:-1])))
        bounds = changes.tolist() + [frames]
        seg_start = 0
        cur = bool(prev)
        for seg_end in bounds:
            n_seg = seg_end - seg_start
            if n_seg > 0:
                k = np.arange(1, n_seg + 1, dtype=np.float64)
                if cur:
                    env[seg_start:seg_end] = np.minimum(1.0, (on_count + k) / att_n)
                    on_count += n_seg
                else:
                    env[seg_start:seg_end] = env_off * np.maximum(
                        0.0, 1.0 - (off_count + k) / rel_n)
                    off_count += n_seg
            if seg_end < frames:
                # an edge at seg_end: the state flips for the next segment
                if cur:
                    env_off = float(env[seg_end - 1]) if seg_end > 0 else (
                        min(1.0, on_count / att_n) if on_count else 0.0)
                    off_count = 0
                else:
                    cur_env = float(env[seg_end - 1]) if seg_end > 0 else (
                        env_off * max(0.0, 1.0 - off_count / rel_n))
                    on_count = int(cur_env * att_n)
                cur = not cur
            seg_start = seg_end
        return env, on_count, off_count, env_off

    # Every block-mean octave path -- ``base * 2 ** (depth * mean cv)`` --
    # clips its exponent here BEFORE the power. +/-64 octaves is far past
    # any rail the result then meets (a cutoff clamp, a rate clamp, a Q
    # rail), so it is a no-op for every value a knob or a sane CV can
    # produce; what it stops is an absurd CV (a ``constant`` at 1e6, a
    # cv_math product gone wild, a runaway loop's scrub) raising
    # OverflowError from a Python-float power in the audio thread. The
    # filter's ``resonance_cv`` pass found the trap (2026-09-19); the
    # clock (``_BPM_CV_EXP_LIMIT``, +/-6) and the freeze
    # (``_FREEZE_PITCH_OCT_LIMIT``, +/-4) keep their own, tighter limits.
    _OCT_EXP_LIMIT = 64.0

    @staticmethod
    def _pow2_clipped(exponent, limit: float = _OCT_EXP_LIMIT):
        """``2.0 ** exponent`` with the exponent clipped to ``+/-limit``.

        The one door every block-mean octave exponent goes through
        before its power. A scalar in gives a Python float out -- the
        bit-exact twin of the bare ``2.0 ** e`` for any ``|e| <=
        limit``, since ``min``/``max`` hand back ``e`` itself. A
        ``(V,)`` array in (the voice paths' per-row exponents) gives
        an array out, ``np.power`` on the clipped array with the dtype
        untouched -- and since 2026-09-22 every block mean reaching
        here comes from :meth:`_finite_mean`, so those rows are float64
        and land on the same float the mono path computes. A non-finite
        exponent reads as 0.0: a NaN block-mean (a NaN cable, a 0/0
        upstream) must mean "no modulation", not poison every
        coefficient downstream -- the render stays finite and the
        module sits at its knob.
        """
        if np.ndim(exponent) == 0:
            e = float(exponent)
            if not math.isfinite(e):
                e = 0.0
            return 2.0 ** min(max(e, -limit), limit)
        e = np.asarray(exponent)
        e = np.where(np.isfinite(e), e, 0.0)
        return np.power(2.0, np.clip(e, -limit, limit))

    @staticmethod
    def _finite_mean(cv, default: float = 0.0, axis=None):
        """The block mean of a CV buffer: float64, non-finite scrubbed.

        The LINEAR twin of :meth:`_pow2_clipped` -- the one door every
        block-mean CV read goes through before a clamp, an ``int()`` or
        a shape decision sees it. Two warts live here so they stop
        living at thirty-odd call sites.

        **float64.** The buffers are float32 and ``np.mean`` of a
        float32 array accumulates in float32, so a CONSTANT CV of 0.3
        read 0.29999998 over 64 samples and 0.30000001 over 512: a held
        offset was not block-size exact, which is the one thing a
        block-mean is supposed to be. ``dtype=np.float64`` makes the
        mean of a constant that constant at every block size, and puts
        a per-voice row on exactly the float the mono path computes.

        **The min/max NaN wart.** Python's ``min``/``max`` do NOT
        propagate NaN -- every comparison against NaN is False, so
        ``min(max(x, lo), hi)`` hands back whichever bound it is passed
        first and a "clamp" silently reads a NaN as a rail.
        ``np.clip`` is no better (it passes NaN straight through) and
        ``int(round(nan))`` raises ValueError out of the audio thread,
        which is how ``bitcrusher.bits_cv`` took a render down. So a
        non-finite mean is read as ``default`` HERE: a NaN cable means
        "no modulation", the module sits at its knob, the render stays
        finite -- the same rule ``_pow2_clipped`` keeps for the octave
        paths.

        ``axis=None`` gives a Python float (the scalar paths).  With an
        ``axis`` it gives the reduced float64 array, scrubbed row by
        row, so one poisoned voice cannot take the other fifteen with
        it. An empty buffer reads as ``default`` too -- callers guard
        on ``size`` first, but a door should not have a hole in it.
        """
        a = np.asarray(cv)
        if a.size == 0:
            if axis is None:
                return float(default)
            keep = tuple(
                n for i, n in enumerate(a.shape) if i != axis % max(a.ndim, 1)
            )
            return np.full(keep, float(default), dtype=np.float64)
        m = np.mean(a, axis=axis, dtype=np.float64)
        if axis is None:
            m = float(m)
            return m if math.isfinite(m) else float(default)
        return np.where(np.isfinite(m), m, np.float64(default))

    @staticmethod
    def _voice_sum(buf):
        """Collapse a ``(V, F)`` buffer to ``(F,)`` by summing the voices.

        The house poly->mono rule (``_input_buffer``'s collapse, the
        mono sinks, the sidechain keys), accumulated in float64 and
        cast back to the buffer's own dtype, so nothing downstream sees
        a new dtype. ``buf.sum(axis=0)`` on a float32 buffer adds the
        rows one at a time IN float32, which makes the mix depend on
        the voice ORDER: reversing eight sounding voices moved 62% of
        the samples by an ulp. The float64 sum of <= 16 float32 values
        is exact (short of a ~2^25 spread in magnitude between voices),
        so the cast rounds once and the collapse is order-
        independent -- the same mix whichever slot the allocator
        handed a note. Two voices were already exact (a float32 add
        of two float32s IS the rounded exact sum); three or more
        sounding voices differ by rounding only (<= 1.4e-6 at 16).

        NOT scrubbed: a collapse is a mix, and a NaN voice poisons
        the mix exactly as a NaN on a mono cable would. The doors
        that must not see a NaN scrub where they consume it
        (``_finite_mean``, ``_pow2_clipped``, the late-cable scrub).
        """
        if buf.dtype.kind != "f":
            return buf.sum(axis=0)
        return buf.sum(axis=0, dtype=np.float64).astype(buf.dtype, copy=False)

    @staticmethod
    def _voice_mean(buf):
        """Collapse a ``(V, F)`` CV to ``(F,)`` by averaging the voices.

        The shared-modulation collapse (a time / pitch / position CV
        whose voice count does not match -- summing voltages would be
        nonsense). Float64 accumulate, cast back, not scrubbed: see
        :meth:`_voice_sum`. Division by a non-power-of-two V is also
        rounded once rather than twice.
        """
        if buf.dtype.kind != "f":
            return buf.mean(axis=0)
        return buf.mean(axis=0, dtype=np.float64).astype(buf.dtype, copy=False)

    def _freeze_gate_row(self, gate, frames: int, tick: bool, state, ramp_n: int):
        """The reverb's / delay's freeze row: the ``freeze`` gate cable
        ORed with the ``freeze`` tickbox, or None when the pre-freeze
        code should run instead.

        Cable patched: the bool row (a ``(V, F)`` cable is already the
        house sum), all-high while the tick is on. Cable unpatched:
        all-high while the tick is on -- the first block it is seen on
        carries a rising edge at sample 0, exactly like a cable rising
        there, ramp and all -- and all-low while the ramp state is still
        live (the tick was on at the block's last sample, or the release
        from the last fall has not run out yet), so clearing the tick
        releases like a gate fall. Otherwise None: the unpatched,
        un-ticked module never enters the freeze machinery, so it is
        bit-exact with the pre-freeze render by construction. ``state``
        carries ``fz_prev`` / ``fz_off`` / ``fz_env`` as ``_gate_ramp_env``
        left them; a run-out release (``fz_off >= ramp_n``) is the same
        state a fresh module starts in as far as the next edge can tell.
        """
        if gate is not None and gate.shape[0] == frames:
            gt = gate > self._GATE_HIGH
            if tick:
                gt = np.ones(frames, dtype=bool)
            return gt
        if tick:
            return np.ones(frames, dtype=bool)
        live = bool(state["fz_prev"]) or (
            float(state["fz_env"]) > 0.0 and int(state["fz_off"]) < ramp_n
        )
        if live:
            return np.zeros(frames, dtype=bool)
        return None

    # ----- Wind rendering ------------------------------------------------------

    def _render_wind(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Blown pipe, flute or reed (see modules/wind.py for the contract).

        Same machinery as the bowed string: per voice, slice-buffer delay
        lines compacted once per block, the loop advanced in vectorized
        chunks no longer than the shortest delay, an integer-count breath
        ramp from the gate edges (``_gate_ramp_env``), pitch read at the
        block's last rising edge or the block mean, early-out when the
        breath is off and the pipe has gone quiet.

        ``flute`` (STK Flute): ``temp = dcblock(-lowpass(bore_out))``;
        ``jet.write(breath - 0.5 temp)``; ``bore.write(jettable(jet_out)
        + 0.5 temp)`` with ``jettable(x) = clip(x(x^2 - 1))``; the bore is
        1.5 periods (the overblown register), the jet 0.32 of it; chunks
        <= the jet delay. ``reed`` (STK Clarinet mouthpiece):
        ``dp = -0.95 lowpass(line_out) - breath``; ``line.write(breath +
        dp * clip(0.7 - 0.3 dp))``; one round-trip delay = the period;
        chunks <= it. Both: the delay is ``sr/f0``-based minus the loss
        filter's exact phase delay at f0, fraction as a linear-interp
        read. Breath = ``maxp(breath[n]) * env[n] * (1 + noise * white)``
        with the white noise a per-(voice, note) seeded stream drawn per
        segment, so a note renders identically at any block size. The
        output is DC-blocked (the reed's line carries the breath pressure).
        """
        from ..modules.wind import WIND_MODELS

        pitch = self._input_buffer(patch, buffers, module.id, "pitch_cv", collapse=False)
        gate = self._input_buffer(patch, buffers, module.id, "gate", collapse=False)
        if pitch is None and gate is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)
        b_cv = self._input_buffer(patch, buffers, module.id, "breath_cv")

        voiced = (pitch is not None and pitch.ndim == 2) or (
            gate is not None and gate.ndim == 2
        )
        V = 1
        for sig in (pitch, gate):
            if sig is not None and sig.ndim == 2:
                V = max(V, sig.shape[0])

        def row(sig, v):
            if sig is None:
                return None
            if sig.ndim == 2:
                return sig[v] if v < sig.shape[0] else sig[0]
            return sig

        def fparam(name, default, lo, hi):
            try:
                x = float(module.params.get(name, default))
            except (TypeError, ValueError):
                x = default
            return min(hi, max(lo, x))

        model = str(module.params.get("model", "flute"))
        if model not in WIND_MODELS:
            model = "flute"
        breath = fparam("breath", 0.5, 0.0, 1.0)
        noise = fparam("noise", 0.15, 0.0, 1.0)
        attack = fparam("attack", 0.04, 0.001, 10.0)
        release = fparam("release", 0.1, 0.001, 10.0)
        damping = fparam("damping", 0.5, 0.0, 1.0)
        depth = fparam("cv_depth", 1.0, -10.0, 10.0)
        level = fparam("level", 0.5, 0.0, 1.0)
        try:
            seed = max(0, int(module.params.get("seed", 1)))
        except (TypeError, ValueError):
            seed = 1

        sr = float(self.sample_rate)
        maxlen = int(self._WIND_FLUTE_BORE * sr / min(self._WIND_MIN_F0.values())) + 8

        st = self._state.setdefault(module.id, {})
        if (st.get("V") != V or st.get("maxlen") != maxlen or st.get("block") != frames
                or st.get("model") != model):
            st.clear()
            buflen = maxlen + frames + 4
            st.update({
                "V": V, "maxlen": maxlen, "block": frames, "buflen": buflen, "model": model,
                "bore": np.zeros((V, buflen), dtype=np.float64),
                "jet": np.zeros((V, buflen), dtype=np.float64),
                "w": np.full(V, maxlen, dtype=np.int64),
                "lp_z": np.zeros(V, dtype=np.float64),
                "dc_z": np.zeros(V, dtype=np.float64),
                "odc_z": np.zeros(V, dtype=np.float64),
                "prev_gate": np.zeros(V, dtype=bool),
                "on_count": np.zeros(V, dtype=np.int64),
                "off_count": np.zeros(V, dtype=np.int64),
                "env_off": np.zeros(V, dtype=np.float64),
                "hits": np.zeros(V, dtype=np.int64),
                "rng": [None] * V,
                "active": np.zeros(V, dtype=bool),
            })
        bore = st["bore"]
        jet = st["jet"]
        buflen = int(st["buflen"])

        p_lo, p_hi = self._WIND_POLE[model]
        pole = p_lo + (p_hi - p_lo) * damping
        att_n = max(1, int(round(attack * sr)))
        rel_n = max(1, int(round(release * sr)))
        gate_high = self._GATE_HIGH
        pr_lo, pr_range = self._WIND_PRESSURE[model]
        if b_cv is not None:
            maxp = pr_lo + pr_range * np.clip(breath + depth * b_cv.astype(np.float64), 0.0, 1.0)
        else:
            maxp = np.full(frames, pr_lo + pr_range * breath)
        f_lo = self._WIND_MIN_F0[model]
        dc_b = np.array([1.0, -1.0])
        dc_a = np.array([1.0, -self._WIND_DC_POLE])
        if model == "flute":
            lp_b = np.array([-(1.0 - pole)])
        else:
            lp_b = np.array([1.0 - pole])
        lp_a = np.array([1.0, -pole])

        out = np.zeros((V, frames), dtype=np.float64)
        for v in range(V):
            p_row = row(pitch, v)
            g_row = row(gate, v)
            gt = (g_row > gate_high) if g_row is not None else np.zeros(frames, dtype=bool)
            prev = bool(st["prev_gate"][v])
            rising = np.flatnonzero(gt & ~np.concatenate(([prev], gt[:-1])))
            if rising.size:
                st["active"][v] = True
            if not st["active"][v]:
                st["prev_gate"][v] = bool(gt[-1])
                continue

            env, on_count, off_count, env_off = self._gate_ramp_env(
                gt, prev, int(st["on_count"][v]), int(st["off_count"][v]),
                float(st["env_off"][v]), att_n, rel_n)
            st["on_count"][v] = on_count
            st["off_count"][v] = off_count
            st["env_off"][v] = env_off
            st["prev_gate"][v] = bool(gt[-1])

            # Breath noise: a fresh seeded stream per note, drawn per
            # segment between rising edges (same consumption at any block
            # size). Silence before the first note draws nothing.
            white = np.zeros(frames, dtype=np.float64)
            seg_start = 0
            for e in rising.tolist() + [frames]:
                if e > seg_start and st["rng"][v] is not None:
                    white[seg_start:e] = st["rng"][v].uniform(-1.0, 1.0, e - seg_start)
                if e < frames:
                    st["hits"][v] += 1
                    st["rng"][v] = np.random.default_rng(
                        (self._WIND_SEED, seed, module.id, v, int(st["hits"][v])))
                seg_start = e
            breath_p = maxp * env * (1.0 + noise * white)

            if p_row is None:
                cv_val = 0.0
            elif rising.size:
                cv_val = float(p_row[rising[-1]])
            else:
                cv_val = self._finite_mean(p_row)
            f0 = self._WIND_C4 * self._pow2_clipped(cv_val)
            f0 = min(self._WIND_MAX_F0, max(f_lo, f0))
            w0 = 2.0 * np.pi * f0 / sr
            tau = float(np.arctan2(pole * np.sin(w0), 1.0 - pole * np.cos(w0)) / w0)
            if model == "flute":
                L = self._WIND_FLUTE_BORE * sr / f0 - tau
                L = max(4.0, L)
                Lj = max(2, int(round(self._WIND_FLUTE_JET * L)))
            else:
                L = max(4.0, sr / f0 - tau)
                Lj = 0
            L_i = int(L)
            L_f = L - L_i
            chunk_max = max(1, min(L_i, Lj) if Lj else L_i)

            w = int(st["w"][v])
            if w + frames > buflen:
                bore[v, :maxlen] = bore[v, w - maxlen:w]
                jet[v, :maxlen] = jet[v, w - maxlen:w]
                w = maxlen
            b_row = bore[v]
            j_row = jet[v]
            z = np.array([st["lp_z"][v]])
            zd = np.array([st["dc_z"][v]])
            o_row = out[v]
            refl = self._WIND_FLUTE_REFL
            r_refl = self._WIND_REED_REFL
            r_off = self._WIND_REED_OFFSET
            r_slope = self._WIND_REED_SLOPE

            pos = 0
            while pos < frames:
                n = min(chunk_max, frames - pos)
                rb = w - L_i
                b0 = b_row[rb:rb + n]
                line_out = b0 + L_f * (b_row[rb - 1:rb - 1 + n] - b0)
                filt, z = lfilter(lp_b, lp_a, line_out, zi=z)
                bp = breath_p[pos:pos + n]
                if model == "flute":
                    temp, zd = lfilter(dc_b, dc_a, filt, zi=zd)
                    j_row[w:w + n] = bp - refl * temp
                    jd = j_row[w - Lj:w - Lj + n]
                    jt = jd * (jd * jd - 1.0)
                    np.clip(jt, -1.0, 1.0, out=jt)
                    jt += refl * temp
                    b_row[w:w + n] = jt
                else:
                    dp = -r_refl * filt - bp
                    rt = r_off - r_slope * dp
                    np.clip(rt, -1.0, 1.0, out=rt)
                    b_row[w:w + n] = bp + dp * rt
                o_row[pos:pos + n] = b_row[w:w + n]
                w += n
                pos += n
            st["w"][v] = w
            st["lp_z"][v] = float(z[0])
            st["dc_z"][v] = float(zd[0])

            # Output DC block (the reed's line carries the breath pressure).
            y, zo = lfilter(dc_b, dc_a, o_row, zi=np.array([st["odc_z"][v]]))
            st["odc_z"][v] = float(zo[0])
            o_row[:] = y * self._WIND_MODEL_GAIN[model]

            if not gt[-1] and env[-1] <= 0.0 and float(np.max(np.abs(o_row))) < self._WIND_SILENCE:
                st["active"][v] = False
                bore[v, :] = 0.0
                jet[v, :] = 0.0
                st["lp_z"][v] = 0.0
                st["dc_z"][v] = 0.0
                st["odc_z"][v] = 0.0
                st["on_count"][v] = 0
                st["off_count"][v] = 0
                st["env_off"][v] = 0.0
                o_row[:] = 0.0

        out *= level * self._WIND_OUT_GAIN
        result = out if voiced else out[0]
        return result.astype(np.float32)

    # ----- Session A utilities (logic / mid_side / octaver) ----------------

    def _render_cv_math(self, module, frames: int, buffers, patch) -> dict:
        """Two-in CV algebra (see modules/cv_math.py).

        Pure elementwise math, stateless, exact. An unpatched operand
        reads 0 (so ``max``/``min`` of a lone ``a`` are its half-wave
        rectifiers -- the normalled trick, documented). Shape-polymorphic:
        operands are fetched with ``collapse=False`` and a mono partner
        broadcasts across a ``(V, F)`` one; outputs take the voiced shape
        if either operand is voiced, else mono.
        """
        a_in = self._input_buffer(patch, buffers, module.id, "a", collapse=False)
        b_in = self._input_buffer(patch, buffers, module.id, "b", collapse=False)
        if a_in is None and b_in is None:
            z = np.zeros(frames, dtype=np.float32)
            return {k: z for k in ("min", "max", "avg", "diff", "mult", "rect", "inv")}
        a = a_in.astype(np.float32, copy=False) if a_in is not None else np.zeros(frames, dtype=np.float32)
        b = b_in.astype(np.float32, copy=False) if b_in is not None else np.zeros(frames, dtype=np.float32)
        voiced = a.ndim == 2 or b.ndim == 2
        if voiced:
            if a.ndim == 1:
                a = a[None, :]
            if b.ndim == 1:
                b = b[None, :]
            if a.shape[0] != b.shape[0] and a.shape[0] != 1 and b.shape[0] != 1:
                # Two voiced operands of different widths: line them up on
                # the smaller count (the extra rows have no partner).
                v = min(a.shape[0], b.shape[0])
                a, b = a[:v], b[:v]
        out = {
            "min": np.minimum(a, b),
            "max": np.maximum(a, b),
            "avg": (a + b) * np.float32(0.5),
            "diff": a - b,
            "mult": a * b,
            "rect": np.abs(a),
            "inv": -a,
        }
        if voiced:
            # A mono-only-a function still comes back voiced-shaped when
            # the other operand is voiced, so every jack agrees on width.
            width = max(a.shape[0], b.shape[0])
            for k, arr in out.items():
                if arr.shape[0] != width:
                    out[k] = np.broadcast_to(arr, (width, frames)).copy()
        return {k: np.ascontiguousarray(v, dtype=np.float32) for k, v in out.items()}

    def _render_logic(self, module, frames: int, buffers, patch) -> dict:
        """Two-in gate algebra (see modules/logic.py).

        Pure elementwise boolean math on thresholded inputs — stateless,
        vectorized, exact. An unpatched operand reads low, which makes
        ``nand``/``not_a`` idle high (the normalled-NAND trick,
        documented). A voice-aware gate source collapses on fetch
        (any-voice-high after the house sum).
        """
        a_in = self._input_buffer(patch, buffers, module.id, "a")
        b_in = self._input_buffer(patch, buffers, module.id, "b")
        thresh = self._GATE_HIGH
        a = (a_in > thresh) if a_in is not None else np.zeros(frames, dtype=bool)
        b = (b_in > thresh) if b_in is not None else np.zeros(frames, dtype=bool)
        and_ = a & b
        return {
            "and": and_.astype(np.float32),
            "or": (a | b).astype(np.float32),
            "xor": (a ^ b).astype(np.float32),
            "nand": (~and_).astype(np.float32),
            "not_a": (~a).astype(np.float32),
        }

    # mid_side ``side_hp`` (bass mono): the corner of the side-only
    # highpass is held to this range when the param is on; 0 is off.
    _MID_SIDE_HP_MIN_HZ = 20.0
    _MID_SIDE_HP_MAX_HZ = 500.0

    def _mid_side_side_hp(self, module_id, freq, side):
        """One block of the bass-mono highpass over the side signal.

        The RBJ highpass (Q 0.707, the shared ``_vocoder_hp_coeffs``
        design -- 12 dB/oct, the mastering norm) with its coefficients
        cached under the module's state and rebuilt only when ``freq``
        moves. The carried state is the raw DF-I history (x1, x2, y1,
        y2), not lfilter's ``zf``: a live corner change then applies
        the new coefficients to the same last two samples (DF-I
        semantics, no restart click). The history is folded into the
        transposed-DF-II ``zi`` in scipy's OWN association order,
        ``(b1*x1 + (b2*x2 - a2*y2)) - a1*y1`` -- that, and not the
        house ``b1*x1 + b2*x2 - a1*y1 - a2*y2`` (off by ~1e-13 in
        float64), is what makes a 64-block render bit-exact with a
        512-block one.
        """
        st = self._state.setdefault(module_id, {})
        if st.get("hp_freq") != freq:
            st["hp_b"], st["hp_a"] = self._vocoder_hp_coeffs(freq)
            st["hp_freq"] = freq
            st.setdefault("hp_hist", (0.0, 0.0, 0.0, 0.0))
        n = side.shape[0]
        if n == 0:
            return side
        b = st["hp_b"]
        a = st["hp_a"]
        x1, x2, y1, y2 = st["hp_hist"]
        zi = np.array(
            [
                (b[1] * x1 + (b[2] * x2 - a[2] * y2)) - a[1] * y1,
                b[2] * x1 - a[2] * y1,
            ],
            dtype=np.float64,
        )
        out = lfilter(b, a, side, zi=zi)[0]
        if n >= 2:
            st["hp_hist"] = (
                float(side[-1]), float(side[-2]), float(out[-1]), float(out[-2])
            )
        else:
            st["hp_hist"] = (float(side[-1]), x1, float(out[-1]), y1)
        return out

    # ----- Autopan rendering ----------------------------------------------

    def _render_autopan(self, module, frames: int, buffers, patch) -> dict:
        """Panner + LFO: ``in_l``/``in_r`` -> ``out_l``/``out_r``.

        ONE input cabled (either jack) is a mono source PLACED by ``law``
        (power = the stereo sink's ``(cos, sin)`` of ``(p+1)*pi/4``;
        linear = ``((1-p)/2, (1+p)/2)``; compromise = their geometric
        mean); BOTH cabled is a stereo pair BALANCED with the sink's
        cosine taper (unity at centre, so pan 0 depth 0 is the inputs to
        the bit). The position is ``clip(pan + pan_cv + depth*lfo, -1,
        1)`` per sample; ``tremolo`` makes the right side read the LFO
        ``tremolo/2`` cycles later than the left (1 = the two gains
        equal: a mono tremolo).

        The LFO phase is keyed to an ABSOLUTE sample count -- ``(phi_a +
        (n - n_a) * rate/sr) mod 1``, re-anchored only when the
        effective rate changes -- so a steady-rate render is the same
        float at sample n whatever the block size (the integer-tick
        lesson; nothing accumulates). A locked ``clock`` hands the phase
        to :meth:`_mod_clock_sync`; while it holds, the free-running
        anchor follows the locked phase so an unpatch carries on from
        where the sweep was. The square is ``sin(pi/2 * clip(K*tri))``
        with ``K = 1/(2*rate*T)``: a raised-cosine glide of
        ``AUTOPAN_SQUARE_EDGE_S`` between sides at every rate.
        """
        from ..modules.autopan import (
            AUTOPAN_LAWS,
            AUTOPAN_RATE_MAX,
            AUTOPAN_RATE_MIN,
            AUTOPAN_SHAPES,
            AUTOPAN_SQUARE_EDGE_S,
        )

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        params = module.params

        def fparam(name, default, lo, hi):
            try:
                v = float(params.get(name, default))
            except (TypeError, ValueError):
                v = default
            if not math.isfinite(v):
                v = default
            return min(max(v, lo), hi)

        sr = self.sample_rate
        pan = fparam("pan", 0.0, -1.0, 1.0)
        depth = fparam("depth", 0.7, 0.0, 1.0)
        rate = fparam("rate", 0.5, AUTOPAN_RATE_MIN, AUTOPAN_RATE_MAX)
        tremolo = fparam("tremolo", 0.0, 0.0, 1.0)
        division = fparam("division", 4.0, self._MOD_DIV_MIN, self._MOD_DIV_MAX)
        cv_depth = fparam("cv_depth", 1.0, -16.0, 16.0)
        shape = str(params.get("shape", "sine"))
        if shape not in AUTOPAN_SHAPES:
            shape = "sine"
        law = str(params.get("law", "power"))
        if law not in AUTOPAN_LAWS:
            law = "power"

        state = self._state.setdefault(module.id, {})
        # The absolute sample index of this block's first sample. Read it
        # BEFORE _mod_clock_sync, which owns the counter and advances it.
        base = int(state.get("samples", 0))

        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * self._pow2_clipped(cv_depth * self._finite_mean(rate_cv))
        rate = min(max(rate, AUTOPAN_RATE_MIN), AUTOPAN_RATE_MAX)
        inc = rate / sr

        # Free-running phase: an anchor (sample, phase) and the increment
        # in force since it. A new rate re-anchors at this block's start.
        if "fr_n" not in state:
            state["fr_n"] = base
            state["fr_ph"] = 0.0
            state["fr_inc"] = inc
        elif state["fr_inc"] != inc:
            state["fr_ph"] = (
                state["fr_ph"] + (base - state["fr_n"]) * state["fr_inc"]
            ) % 1.0
            state["fr_n"] = base
            state["fr_inc"] = inc
        an = int(state["fr_n"])
        aph = float(state["fr_ph"])
        n = (base - an) + np.arange(frames, dtype=np.float64)
        free = (aph + n * inc) % 1.0

        sync = self._mod_clock_sync(
            module, frames, buffers, patch, state, division, free
        )
        if sync is None:
            ph = free
            eff_rate = rate
        else:
            ph, end_phase = sync
            eff_rate = sr / float(state["period"])
            state["fr_n"] = base + frames
            state["fr_ph"] = float(end_phase)
            state["fr_inc"] = inc

        if shape == "square":
            sq_k = max(1.0, 1.0 / (2.0 * eff_rate * AUTOPAN_SQUARE_EDGE_S))

        def lfo(phs):
            if shape == "sine":
                return np.sin(2.0 * np.pi * phs)
            tri = 1.0 - 4.0 * np.abs(((phs + 0.25) % 1.0) - 0.5)
            if shape == "triangle":
                return tri
            return np.sin((0.5 * np.pi) * np.clip(sq_k * tri, -1.0, 1.0))

        centre = pan
        pan_cv = self._input_buffer(
            patch, buffers, module.id, "pan_cv", collapse=False
        )
        if pan_cv is not None and pan_cv.size > 0:
            c = pan_cv.astype(np.float64)
            c = np.where(np.isfinite(c), c, 0.0)
            if c.ndim == 2:
                c = self._voice_mean(c)
            centre = pan + c

        pos_l = np.clip(centre + depth * lfo(ph), -1.0, 1.0)
        if tremolo > 0.0:
            pos_r = np.clip(
                centre + depth * lfo((ph + 0.5 * tremolo) % 1.0), -1.0, 1.0
            )
        else:
            pos_r = pos_l

        cabled = {c.dst_port for c in patch.cables_into(module.id)}
        l_on = "in_l" in cabled
        r_on = "in_r" in cabled
        if not (l_on or r_on):
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z.copy()}

        def audio(port):
            buf = self._input_buffer(patch, buffers, module.id, port)
            if buf is None:
                return np.zeros(frames, dtype=np.float64)
            return buf.astype(np.float64)

        if l_on and r_on:
            # Stereo pair: balance, the stereo sink's cosine taper.
            g_l = np.cos(np.maximum(pos_l, 0.0) * (0.5 * np.pi))
            g_r = np.cos(np.maximum(-pos_r, 0.0) * (0.5 * np.pi))
            x_l = audio("in_l")
            x_r = audio("in_r")
        else:
            x_l = x_r = audio("in_l" if l_on else "in_r")
            if law == "linear":
                g_l = 0.5 * (1.0 - pos_l)
                g_r = 0.5 * (1.0 + pos_r)
            else:
                g_l = np.cos((pos_l + 1.0) * (0.25 * np.pi))
                g_r = np.sin((pos_r + 1.0) * (0.25 * np.pi))
                if law == "compromise":
                    g_l = np.sqrt(np.maximum(g_l * 0.5 * (1.0 - pos_l), 0.0))
                    g_r = np.sqrt(np.maximum(g_r * 0.5 * (1.0 + pos_r), 0.0))
        return {
            "out_l": (x_l * g_l).astype(np.float32),
            "out_r": (x_r * g_r).astype(np.float32),
        }

    def _render_mid_side(self, module, frames: int, buffers, patch) -> dict:
        """M/S encode/decode + width + bass mono (see modules/mid_side.py).

        Standard sum/difference pair; ``width_cv`` adds per sample with
        the final width clamped 0..2. One patched input is treated as
        the mid itself (level preserved, width inert) rather than a
        half-level L+0 pair — the mono-passthrough contract.

        ``side_hp`` > 0 runs ``_mid_side_side_hp`` over the SIDE only,
        before ``width`` and before the decode: everything under the
        corner collapses to the middle, ``mid`` is untouched, and
        ``side``/``out_l``/``out_r`` all carry the filtered side. At 0
        the filter is not called at all -- the module is the stateless
        sum/difference pair it always was, bit-exact -- and any filter
        history is dropped so a corner switched back on starts from
        rest rather than resuming a stale tail. Mono passthrough and
        silence drop it for the same reason: the side was zero.
        """
        in_l = self._input_buffer(patch, buffers, module.id, "in_l")
        in_r = self._input_buffer(patch, buffers, module.id, "in_r")
        width_cv = self._input_buffer(patch, buffers, module.id, "width_cv")

        try:
            width = float(module.params.get("width", 1.0))
        except (TypeError, ValueError):
            width = 1.0
        width = min(2.0, max(0.0, width))
        try:
            side_hp = float(module.params.get("side_hp", 0.0))
        except (TypeError, ValueError):
            side_hp = 0.0
        if side_hp > 0.0:
            side_hp = min(
                self._MID_SIDE_HP_MAX_HZ, max(self._MID_SIDE_HP_MIN_HZ, side_hp)
            )
        else:
            side_hp = 0.0

        if in_l is None and in_r is None:
            self._state.pop(module.id, None)
            zeros = np.zeros(frames, dtype=np.float32)
            return {"mid": zeros, "side": zeros, "out_l": zeros, "out_r": zeros}
        if in_l is None or in_r is None:
            # Mono: the one input IS the mid; width has nothing to act on.
            self._state.pop(module.id, None)
            mono = (in_l if in_l is not None else in_r).astype(np.float32)
            zeros = np.zeros(frames, dtype=np.float32)
            return {"mid": mono, "side": zeros, "out_l": mono, "out_r": mono}

        mid = (in_l.astype(np.float64) + in_r) * 0.5
        side = (in_l.astype(np.float64) - in_r) * 0.5
        if side_hp > 0.0:
            side = self._mid_side_side_hp(module.id, side_hp, side)
        else:
            self._state.pop(module.id, None)
        if width_cv is not None:
            w = np.clip(width + width_cv.astype(np.float64), 0.0, 2.0)
        else:
            w = width
        ws = w * side
        return {
            "mid": mid.astype(np.float32),
            "side": side.astype(np.float32),
            "out_l": (mid + ws).astype(np.float32),
            "out_r": (mid - ws).astype(np.float32),
        }

    # ----- Drum voices (kick / snare / hat) --------------------------------

    _DRUM_FADE_SECONDS = 0.002  # retrigger/choke declick ramp
    _DRUM_SEED = 0x44524D53  # "DRMS"

    def _drum_edges(self, st, key: str, gate) -> list[int]:
        """Rising-edge sample indices for one trigger jack, prev carried."""
        if gate is None:
            return []
        g = gate > self._GATE_HIGH
        prev = np.empty_like(g)
        prev[0] = bool(st.get("prev_" + key, False))
        prev[1:] = g[:-1]
        st["prev_" + key] = bool(g[-1])
        return np.flatnonzero(g & ~prev).tolist()

    @staticmethod
    def _drum_edge_value(sig, at: int, default: float) -> float:
        """A CV's value AT the trigger edge sample, for latching into a hit.

        A drum is one drum, so a voice-aware ``(V, F)`` source collapses
        here — to the **loudest voice at that sample**, not the house sum:
        a velocity bus carries 0 on idle slots, so the max is the key that
        was just struck, whereas sixteen velocities added together would
        be nonsense. Unpatched → ``default``.
        """
        if sig is None:
            return default
        if sig.ndim == 2:
            return float(np.max(sig[:, at]))
        return float(sig[at])

    def _drum_fade_slot(self, st, slot: str) -> None:
        """Start the 2 ms fade-out on every active play in ``slot``."""
        fade_total = max(1, int(self.sample_rate * self._DRUM_FADE_SECONDS))
        for play in st["plays"]:
            if play["slot"] == slot and play["fade"] is None:
                play["fade"] = [fade_total, fade_total]

    def _drum_advance(self, st, out: np.ndarray, start: int, end: int) -> None:
        """Mix every active play into ``out[start:end]``; prune finished."""
        span = end - start
        if span <= 0:
            return
        alive = []
        for play in st["plays"]:
            buf, pos = play["buf"], play["pos"]
            n = min(span, len(buf) - pos)
            if n > 0:
                seg = buf[pos : pos + n]
                if play["fade"] is not None:
                    rem, total = play["fade"]
                    gains = np.clip(
                        (rem - np.arange(n, dtype=np.float64)) / total, 0.0, 1.0
                    )
                    seg = seg * gains
                    play["fade"][0] = rem - n
                out[start : start + n] += seg
                play["pos"] = pos + n
            done = play["pos"] >= len(play["buf"]) or (
                play["fade"] is not None and play["fade"][0] <= 0
            )
            if not done:
                alive.append(play)
        st["plays"] = alive

    def _render_drum(self, module, frames: int, buffers, patch, make_hits):
        """Shared engine: precomputed hit buffers + declick crossfades.

        ``make_hits(edge_lists)`` receives the per-jack edge index lists
        and yields ``(edge_sample, slot, buffer)`` hits in time order.
        On each hit any play in the same slot fades over ~2 ms while the
        new buffer starts — the retrigger declick (and the hat's choke).
        Buffers are synthesized whole at the edge (seeded per hit), so
        output is deterministic and block-size independent by
        construction.
        """
        st = self._state.setdefault(
            module.id, {"plays": [], "hits": 0}
        )
        out = np.zeros(frames, dtype=np.float64)
        hits = sorted(make_hits(st), key=lambda h: h[0])
        pos = 0
        for e, slot, buf in hits:
            self._drum_advance(st, out, pos, e)
            self._drum_fade_slot(st, slot)
            st["plays"].append(
                {"buf": buf, "pos": 0, "fade": None, "slot": slot}
            )
            pos = e
        self._drum_advance(st, out, pos, frames)
        level = float(module.params.get("level", 0.7))
        return (out * level).astype(np.float32)

    def _render_kick(self, module, frames: int, buffers, patch) -> np.ndarray:
        trig = self._input_buffer(patch, buffers, module.id, "trigger")
        vel = self._input_buffer(patch, buffers, module.id, "vel", collapse=False)
        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        p = module.params

        def make_hits(st):
            hits = []
            for e in self._drum_edges(st, "t", trig):
                rng = np.random.default_rng(
                    (self._DRUM_SEED, module.id, int(st["hits"]))
                )
                st["hits"] += 1
                # `vel` and `pitch_cv` are read AT THE EDGE and latched
                # into this hit (the sampler idiom): a hit is a hit.
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                cv = self._drum_edge_value(pitch, e, 0.0)
                tune = min(12.0, max(-12.0, float(p.get("tune", 0.0))))
                buf = _kick_hit(
                    self.sample_rate,
                    min(400.0, max(100.0, float(p.get("freq_start", 180.0)))),
                    min(80.0, max(30.0, float(p.get("freq_end", 50.0)))),
                    min(0.2, max(0.005, float(p.get("bend", 40.0)) * 1e-3)),
                    min(1.5, max(0.05, float(p.get("decay", 350.0)) * 1e-3)),
                    min(1.0, max(0.0, float(p.get("click", 0.3)))),
                    min(1.0, max(0.0, float(p.get("drive", 0.0)))),
                    tune + 12.0 * cv,
                    rng,
                    vel=velocity,
                )
                hits.append((int(e), "main", buf))
            return hits

        return self._render_drum(module, frames, buffers, patch, make_hits)

    def _render_snare(self, module, frames: int, buffers, patch) -> np.ndarray:
        trig = self._input_buffer(patch, buffers, module.id, "trigger")
        vel = self._input_buffer(patch, buffers, module.id, "vel", collapse=False)
        p = module.params

        def make_hits(st):
            hits = []
            for e in self._drum_edges(st, "t", trig):
                rng = np.random.default_rng(
                    (self._DRUM_SEED, module.id, int(st["hits"]))
                )
                st["hits"] += 1
                buf = _snare_hit(
                    self.sample_rate,
                    min(0.5, max(0.02, float(p.get("tone_decay", 120.0)) * 1e-3)),
                    min(1.0, max(0.02, float(p.get("noise_decay", 200.0)) * 1e-3)),
                    min(1.0, max(0.0, float(p.get("snappy", 0.5)))),
                    min(12.0, max(-12.0, float(p.get("tune", 0.0)))),
                    rng,
                )
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                if velocity != 1.0:
                    buf = buf * velocity
                hits.append((int(e), "main", buf))
            return hits

        return self._render_drum(module, frames, buffers, patch, make_hits)

    def _render_hat(self, module, frames: int, buffers, patch) -> np.ndarray:
        from ..modules.drums import HAT_TONE_MAX, HAT_TONE_MIN

        closed = self._input_buffer(patch, buffers, module.id, "closed_trigger")
        opened = self._input_buffer(patch, buffers, module.id, "open_trigger")
        vel = self._input_buffer(patch, buffers, module.id, "vel", collapse=False)
        p = module.params
        tune = min(12.0, max(-12.0, float(p.get("tune", 0.0))))
        try:
            tone = float(p.get("tone", 400.0))
        except (TypeError, ValueError):
            tone = 400.0
        tone = min(HAT_TONE_MAX, max(HAT_TONE_MIN, tone))

        def make_hits(st):
            hits = []
            for e in self._drum_edges(st, "c", closed):
                buf = _hat_hit(
                    self.sample_rate,
                    min(0.3, max(0.01, float(p.get("decay_closed", 60.0)) * 1e-3)),
                    tune,
                    tone,
                )
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                if velocity != 1.0:
                    buf = buf * velocity
                # A closed hit chokes BOTH slots (its own retrigger and
                # any ringing open hit) — the pedal coming down.
                hits.append((int(e), "closed", buf))
            for e in self._drum_edges(st, "o", opened):
                buf = _hat_hit(
                    self.sample_rate,
                    min(1.5, max(0.05, float(p.get("decay_open", 400.0)) * 1e-3)),
                    tune,
                    tone,
                )
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                if velocity != 1.0:
                    buf = buf * velocity
                hits.append((int(e), "open", buf))
            return hits

        # Custom choke: run the shared engine but fade the open slot on
        # every closed hit too.
        st = self._state.setdefault(module.id, {"plays": [], "hits": 0})
        out = np.zeros(frames, dtype=np.float64)
        hits = sorted(make_hits(st), key=lambda h: h[0])
        pos = 0
        for e, slot, buf in hits:
            self._drum_advance(st, out, pos, e)
            self._drum_fade_slot(st, slot)
            if slot == "closed":
                self._drum_fade_slot(st, "open")
            st["plays"].append({"buf": buf, "pos": 0, "fade": None, "slot": slot})
            pos = e
        self._drum_advance(st, out, pos, frames)
        level = float(p.get("level", 0.6))
        return (out * level).astype(np.float32)

    # ----- Modal rendering -------------------------------------------------

    _MODAL_C4 = 261.6255653005986
    _MODAL_MAX_F_FRACTION = 0.45  # modes above this × sr are dropped

    def _render_modal(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Two-pole resonator bank (see modules/modal.py for the contract).

        Each mode is the classic resonator ``y[n] = 2r·cosθ·y[n−1] −
        r²·y[n−2] + b₀·x[n]`` with ``r`` set from that mode's t60 and
        ``b₀ = gain·sinθ`` normalizing the STRIKE response (impulse-
        response peak ≈ gain, independent of decay — a ``(1−r)`` drive
        would normalize the ring-out integral instead and starve long
        decays to silence) — run as
        one ``lfilter`` per mode per *pitch group* (voices whose
        block-mean pitch matches share coefficients, so their rows batch
        into a single vectorized call — the slice-4 pattern; 16 unison
        voices cost the same as one). Coefficients rebuild each block
        from the block-mean pitch; ``zi`` carries per (voice, mode)
        across blocks, so the bank stays block-size independent under
        constant pitch. Quiet voices (silent excite + decayed state)
        early-out; modes past ``modes`` have their state zeroed so a
        live mode-count change can't resurrect stale ring-outs.

        Three things from the 2026-09-11 love pass, each skipped entirely
        at its default so the shipped render is untouched: ``position``
        combs the mode gains (``strike_comb``, renormalized); ``mallet``
        one-poles the excite per pitch group at ``f0·2^(6(1−m))`` with
        per-voice state (``lp``); ``spread`` accumulates a second pair of
        per-mode-panned sums for ``out_l``/``out_r`` (``mode_pans``,
        equal-power, ×√2 so centre ≡ mono). Returns a port dict.
        """
        from ..modules.modal import (
            MALLET_OCTAVES,
            MODAL_MAX_MODES,
            modal_ratios,
            mode_pans,
            strike_comb,
        )

        excite = self._input_buffer(
            patch, buffers, module.id, "excite", collapse=False
        )
        if excite is None:
            self._state.pop(module.id, None)
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}
        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )

        voiced = excite.ndim == 2 or (pitch is not None and pitch.ndim == 2)
        V = 1
        for sig in (excite, pitch):
            if sig is not None and sig.ndim == 2:
                V = max(V, sig.shape[0])

        material = str(module.params.get("material", "bar"))
        modes = max(4, min(MODAL_MAX_MODES, int(module.params.get("modes", 12))))
        try:
            decay = float(module.params.get("decay", 2.0))
        except (TypeError, ValueError):
            decay = 2.0
        decay = min(30.0, max(0.1, decay))
        tilt = min(1.0, max(0.0, float(module.params.get("decay_tilt", 0.5))))
        bright = min(1.0, max(0.0, float(module.params.get("brightness", 0.5))))
        inharm = min(1.0, max(0.0, float(module.params.get("inharm", 0.0))))
        level = float(module.params.get("level", 0.5))

        def _knob(name):
            try:
                return min(1.0, max(0.0, float(module.params.get(name, 0.0))))
            except (TypeError, ValueError):
                return 0.0

        position = _knob("position")
        mallet = _knob("mallet")
        spread = _knob("spread")
        sr = float(self.sample_rate)

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V:
            st.clear()
            st.update(
                {
                    "V": V,
                    "zi": np.zeros((V, MODAL_MAX_MODES, 2), dtype=np.float64),
                    "lp": np.zeros(V, dtype=np.float64),
                }
            )
        zi = st["zi"]
        zi[:, modes:, :] = 0.0  # stale modes stay dead
        lp = st["lp"]

        ratios = np.array(modal_ratios(material, modes)) ** (1.0 + 0.3 * inharm)
        gains = ratios ** (2.0 * (bright - 0.5))
        if position > 0.0:
            # Where the strike lands: comb the gains, then renormalize so
            # a strike near the edge is thin, not quiet.
            gains = gains * strike_comb(ratios, position)
        gains = gains / gains.sum()
        t60 = np.maximum(0.01, decay / (1.0 + 3.0 * tilt * (ratios - 1.0)))
        r = 10.0 ** (-3.0 / (sr * t60))

        # Stereo: per-mode equal-power pan gains, scaled so a centred mode
        # contributes 1.0 to each side (the outs equal `out` at spread 0).
        if spread > 0.0:
            theta = (mode_pans(modes, spread) + 1.0) * (np.pi / 4.0)
            gl = np.cos(theta) * np.sqrt(2.0)
            gr = np.sin(theta) * np.sqrt(2.0)

        # Per-voice pitch (block mean) → group voices sharing a value.
        def cv_for(v: int) -> float:
            if pitch is None:
                return 0.0
            row = pitch[v] if pitch.ndim == 2 and v < pitch.shape[0] else (
                pitch[0] if pitch.ndim == 2 else pitch
            )
            return self._finite_mean(row)

        x = excite if excite.ndim == 2 else excite[None, :]
        out = np.zeros((V, frames), dtype=np.float64)
        if spread > 0.0:
            out_l = np.zeros((V, frames), dtype=np.float64)
            out_r = np.zeros((V, frames), dtype=np.float64)
        groups: dict[float, list[int]] = {}
        for v in range(V):
            xv = x[v] if v < x.shape[0] else x[0]
            if (
                float(np.max(np.abs(xv))) < 1e-9
                and float(np.max(np.abs(zi[v, :modes]))) < 1e-7
            ):
                continue  # silent, rung out: free
            groups.setdefault(round(cv_for(v), 6), []).append(v)

        for cv_val, rows in groups.items():
            f0 = self._MODAL_C4 * self._pow2_clipped(cv_val)
            f0 = min(self._MODAL_MAX_F_FRACTION * sr, max(20.0, f0))
            freqs = f0 * ratios
            X = np.stack([x[v] if v < x.shape[0] else x[0] for v in rows]).astype(
                np.float64
            )
            if mallet > 0.0:
                # The mallet: a one-pole low-pass on the strike whose
                # cutoff tracks THIS group's pitch, so the same softness
                # reads the same across the keyboard. State per voice.
                fc = f0 * (2.0 ** (MALLET_OCTAVES * (1.0 - mallet)))
                fc = min(self._MODAL_MAX_F_FRACTION * sr, fc)
                coef = 1.0 - float(np.exp(-2.0 * np.pi * fc / sr))
                X, _zf = lfilter(
                    [coef], [1.0, coef - 1.0], X, axis=-1,
                    zi=((1.0 - coef) * lp[rows])[:, None],
                )
                # Carry the last OUTPUT (the octaver's idiom): the zi
                # above is rebuilt from it with the block's coefficient,
                # which is what keeps a pitch change from kicking the
                # filter -- lfilter's own zf already has (1-coef) in it.
                lp[rows] = X[:, -1]
            acc = np.zeros_like(X)
            if spread > 0.0:
                acc_l = np.zeros_like(X)
                acc_r = np.zeros_like(X)
            for i in range(modes):
                if freqs[i] >= self._MODAL_MAX_F_FRACTION * sr:
                    zi[rows, i, :] = 0.0
                    continue
                theta = 2.0 * np.pi * freqs[i] / sr
                a = [1.0, -2.0 * r[i] * np.cos(theta), r[i] * r[i]]
                b = [gains[i] * np.sin(theta)]
                y, zf = lfilter(b, a, X, axis=-1, zi=zi[rows, i, :])
                acc += y
                if spread > 0.0:
                    acc_l += y * gl[i]
                    acc_r += y * gr[i]
                zi[rows, i, :] = zf
            for k, v in enumerate(rows):
                out[v] = acc[k]
                if spread > 0.0:
                    out_l[v] = acc_l[k]
                    out_r[v] = acc_r[k]

        out *= level
        mono = (out if voiced else out[0]).astype(np.float32)
        if spread <= 0.0:
            return {"out": mono, "out_l": mono, "out_r": mono}
        out_l *= level
        out_r *= level
        return {
            "out": mono,
            "out_l": (out_l if voiced else out_l[0]).astype(np.float32),
            "out_r": (out_r if voiced else out_r[0]).astype(np.float32),
        }

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
