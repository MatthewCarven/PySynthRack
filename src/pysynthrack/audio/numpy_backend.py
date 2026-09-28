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
shaping is centralised in :meth:`_osc_waveshape` (in the
``renderers/_waveshapes.py`` mixin), which both the
Oscillator and CVToFrequency renderers (and, via the same call, the
Keyboard / MIDIInput note sources) route through. The Oscillator alone
also hands it a pulse width for ``square`` / ``square_blep`` (per-sample
PWM, DC-compensated, the falling edge's blep riding its own phase);
``square_wt`` is a fixed 50% table by design.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import butter, firwin, lfilter, sosfilt

from ..core.patch import Patch
from .backend import AudioBackend
from .renderers import (
    ClockworkRenderers,
    ColourRenderers,
    CVRoutingRenderers,
    DynamicsRenderers,
    EQFilterRenderers,
    IORenderers,
    MediaHelpers,
    ModFXRenderers,
    ModSourceRenderers,
    OscillatorRenderers,
    PhysicalRenderers,
    PitchTimeRenderers,
    ReverbDelayRenderers,
    SequencingRenderers,
    SharedHelpers,
    SpectralRenderers,
    WaveshapeHelpers,
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
    CVRoutingRenderers,
    PhysicalRenderers,
    WaveshapeHelpers,
    MediaHelpers,
    SharedHelpers,
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

    # Module TYPE -> the renderer that renders it (a mixin method). The
    # note sources in _FRAMES_ONLY render from their own state and take
    # ``(module, frames)``; every other renderer takes ``(module, frames,
    # buffers, patch)``. A type with no entry renders nothing here: the
    # speaker-family sinks are drained by the speaker pass.
    # tests/test_render_dispatch.py checks this against the registry.
    _RENDERERS = {
        "oscillator": "_render_oscillator",
        "keyboard": "_render_keyboard",
        "cv_keyboard": "_render_cv_keyboard",
        "cv_gates": "_render_cv_gates",
        "key_trigger": "_render_key_trigger",
        "clock": "_render_clock",
        # fader_seq is the Sequencer with a different front panel —
        # identical param contract, one engine (see modules/fader_seq.py).
        "sequencer": "_render_sequencer",
        "fader_seq": "_render_sequencer",
        "shift_random": "_render_shift_random",
        "chaos": "_render_chaos",
        "organ": "_render_organ",
        "matrix_mixer": "_render_matrix_mixer",
        "vinyl": "_render_vinyl",
        "supersaw": "_render_supersaw",
        "wavetable_morph": "_render_wavetable_morph",
        "possibility_seq": "_render_possibility_seq",
        "possibility_selector": "_render_possibility_selector",
        "drift": "_render_drift",
        "sampler": "_render_sampler",
        "euclidean": "_render_euclidean",
        "burst": "_render_burst",
        "bernoulli_gate": "_render_bernoulli",
        "clock_divider": "_render_clock_divider",
        "arpeggiator": "_render_arpeggiator",
        "chord": "_render_chord",
        "cv_math": "_render_cv_math",
        "vowel": "_render_vowel",
        "freeze": "_render_freeze",
        "cv_recorder": "_render_cv_recorder",
        "logic": "_render_logic",
        "mid_side": "_render_mid_side",
        "autopan": "_render_autopan",
        "octaver": "_render_octaver",
        "midi_input": "_render_midi_input",
        "filter": "_render_filter",
        "adsr": "_render_adsr",
        "ad_envelope": "_render_ad",
        "function_generator": "_render_function_generator",
        "vca": "_render_vca",
        "audio_to_cv": "_render_audio_to_cv",
        "cv_to_audio": "_render_cv_to_audio",
        "schmitt": "_render_schmitt",
        "cv_to_frequency": "_render_cv_to_frequency",
        "fm_op": "_render_fm_op",
        "bowed": "_render_bowed",
        "wind": "_render_wind",
        "pluck": "_render_pluck",
        "modal": "_render_modal",
        "kick_drum": "_render_kick",
        "snare_drum": "_render_snare",
        "hat_drum": "_render_hat",
        "lfo": "_render_lfo",
        "mixer": "_render_mixer",
        "combiner": "_render_combiner",
        "cv_combiner": "_render_cv_combiner",
        "constant": "_render_constant",
        "cv_scale": "_render_cv_scale",
        "cv_offset": "_render_cv_offset",
        "slew": "_render_slew",
        "quantizer": "_render_quantizer",
        "scope": "_render_scope",
        "sample_hold": "_render_sample_hold",
        "noise": "_render_noise",
        "crossover": "_render_crossover",
        "parametric_eq": "_render_parametric_eq",
        "motion_eq": "_render_motion_eq",
        "sweep_eq": "_render_sweep_eq",
        "tilt_eq": "_render_tilt_eq",
        "meter": "_render_meter",
        "chorus": "_render_chorus",
        "rotary": "_render_rotary",
        "granular": "_render_granular",
        "flanger": "_render_flanger",
        "phaser": "_render_phaser",
        "vocoder": "_render_vocoder",
        "delay": "_render_delay",
        "reverb": "_render_reverb",
        "loudness": "_render_loudness",
        "compressor": "_render_compressor",
        "limiter": "_render_limiter",
        "noise_gate": "_render_noise_gate",
        "transient_shaper": "_render_transient_shaper",
        "distortion": "_render_distortion",
        "ring_mod": "_render_ring_mod",
        "freq_shifter": "_render_freq_shifter",
        "bitcrusher": "_render_bitcrusher",
        "waveshaper": "_render_waveshaper",
        "tape": "_render_tape",
        "convolver": "_render_convolver",
        "resampler": "_render_resampler",
        "pitch_shifter": "_render_pitch_shifter",
        "disk_writer": "_render_disk_writer",
        "file_player": "_render_file_player",
        "mic_input": "_render_mic_input",
    }
    _FRAMES_ONLY = frozenset({
        "keyboard",
        "cv_keyboard",
        "cv_gates",
        "key_trigger",
        "midi_input",
    })

    def _render_module(self, module, frames, buffers, patch):
        name = self._RENDERERS.get(module.TYPE)
        if name is None:
            return None  # a speaker-family sink (or an unknown type)
        if module.TYPE in self._FRAMES_ONLY:
            return getattr(self, name)(module, frames)
        return getattr(self, name)(module, frames, buffers, patch)
