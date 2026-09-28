"""Waveshape helpers: the oscillator core that several families draw on.

``_osc_waveshape`` and what it dispatches to -- the naive shapes, the
PolyBLEP / PolyBLAMP corrections and the band-limited wavetable mipmaps
(``WT_LEN`` / ``NUM_WT_TABLES`` / ``WT_BASE_FREQ``) -- moved verbatim out
of ``numpy_backend.py`` (2026-09-28) into a mixin ``NumpyBackend``
inherits. The oscillators, the keyboard / MIDI voices (``io``) and
CV-to-frequency (``cv_routing``) all reach it through ``self``; the
wavetable cache lives on the backend (``self._wavetables``).
"""
from __future__ import annotations

import math

import numpy as np


class WaveshapeHelpers:
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
