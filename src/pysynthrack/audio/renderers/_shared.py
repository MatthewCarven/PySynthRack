"""Shared helpers: what more than one renderer family leans on.

The input-port lookup (``_input_buffer``), the gate threshold and gate
ramps, the voice collapses (``_voice_sum`` / ``_voice_mean``), the
overflow-safe CV maths (``_pow2_clipped`` / ``_finite_mean``), the
envelope follower the dynamics and CV families share, the biquad
designers and the distortion curve -- moved verbatim out of
``numpy_backend.py`` (2026-09-28) into a mixin ``NumpyBackend``
inherits. The engine's speaker drain and sink governor use some of
them too, all through ``self``. Two read ``NumpyBackend.X`` by name
(so a test that patches the backend class still reaches them); that
needs a lazy import, since ``numpy_backend`` imports this module.
"""
from __future__ import annotations

import math

import numpy as np


class SharedHelpers:
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
                    from ..numpy_backend import NumpyBackend  # lazy: it imports this module

                    return NumpyBackend._voice_sum(buf)
                return buf
        return None

    # ----- biquads --------------------------------------------------------

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

    # ----- gates ----------------------------------------------------------

    # Gate is treated as "high" once it crosses this threshold; this gives
    # us tolerance against fractional gate values (e.g. an LFO-style gate
    # in some future patching) without false triggers on numerical noise.
    _GATE_HIGH = 0.5

    # ----- envelope follower (audio_to_cv and the dynamics) ---------------

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

    # ----- distortion curve, ring-mod voices ------------------------------

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
            from ..numpy_backend import NumpyBackend  # lazy: it imports this module

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

    # ----- gate ramps, CV maths, voice collapse ---------------------------

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

    # ----- drum triggers --------------------------------------------------

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
