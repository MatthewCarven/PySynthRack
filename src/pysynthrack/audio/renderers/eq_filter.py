"""EQ / filter renderers: filter (with its ``_mono`` / ``_voice`` paths),
crossover, parametric EQ, motion EQ, sweep EQ, tilt EQ, loudness, vowel.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". Three coefficient helpers stay in the backend because other
families share them, and are reached through ``self``:
``_filter_coeffs`` (also the meter's K-weighting), ``_crossover_coeffs``
(also the rotary in ``modfx``) and ``_loud_shelf`` (also tape and the
meter). ``_finite_mean`` and ``_pow2_clipped`` are backend-wide helpers,
likewise via ``self``. The vowel renderer's lazy ``modules.vowel`` imports
gained a dot for the deeper package.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter


class EQFilterRenderers:
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

        resonance_cv (love pass 2026-09-19) follows the same three-way
        split on the Q: ``Q_eff = resonance * 2 ** (res_cv_depth *
        mean cv)``, clipped to the (0.1, 20) legal range by the same
        clamp the param goes through. A (V, F) source gives V Qs (and
        forces the per-voice coefficient path even when the cutoff is
        shared), an (F,) source one shared Q, and unpatched leaves the
        Q exactly ``resonance`` -- the render is bit-identical to the
        pre-love-pass module.

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
        resonance_cv = self._input_buffer(
            patch, buffers, module.id, "resonance_cv", collapse=False
        )

        if src_buf.ndim == 2:
            return self._render_filter_voice(
                module, frames, src_buf, cutoff_cv, resonance_cv
            )
        return self._render_filter_mono(
            module, frames, src_buf, cutoff_cv, resonance_cv
        )

    # The Q only matters inside its (0.1, 20) clamp -- log2(200) ~ 7.6
    # doublings end to end -- so the exponent is clipped to +/-64
    # before it hits ``2 **``. That never changes an in-range answer
    # (2 ** 64 * 0.1 is far past the rail) but keeps a pathological CV
    # (a cv_math product gone wild) from raising OverflowError in the
    # audio thread; the clamp then pins the Q at the rail as promised.
    _Q_CV_EXP_LIMIT = 64.0

    def _q_cv_ratio(self, module, cv_mean):
        """``2 ** (res_cv_depth * cv_mean)``: the resonance_cv multiplier.

        Scalar in, scalar out (a (V,) array in gives a (V,) array out --
        the voice path's per-voice Qs). Depth 0 returns exactly 1.0, so
        ``q * ratio`` is bit-identical to ``q`` -- "depth 0 disables the
        input without unpatching it". The exponent is taken in float64
        on purpose: a float32 block-mean times a Python float stays
        float32 under numpy's weak-scalar rule, and 2 ** 0.5 in float32
        put a per-voice Q one float32 ulp away from the mono path's.
        """
        depth = float(module.params.get("res_cv_depth", 1.0))
        expo = np.clip(
            depth * np.asarray(cv_mean, dtype=np.float64),
            -self._Q_CV_EXP_LIMIT,
            self._Q_CV_EXP_LIMIT,
        )
        if np.ndim(expo) == 0:
            return float(2.0 ** float(expo))
        return np.power(2.0, expo)

    def _render_filter_mono(
        self, module, frames, src_buf, cutoff_cv, resonance_cv=None
    ):
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
        # ``cutoff *= 2 ** (cv_depth * mean(cv))`` (the exponent clipped
        # first: ``_pow2_clipped``). Block-mean keeps
        # the biquad coefficient recomputation to one pass per block;
        # audio-rate cutoff mod would need a time-varying filter,
        # which a single lfilter call can't express. If cutoff_cv is
        # 2D (a voice-aware source feeding a mono filter), mean over
        # both axes -- same effect as the old collapse=True path.
        if cutoff_cv is not None and cutoff_cv.size > 0:
            cv_depth = float(module.params.get("cv_depth", 1.0))
            cutoff = cutoff * self._pow2_clipped(
                cv_depth * self._finite_mean(cutoff_cv)
            )

        # CV-modulate the Q the same way: doublings per CV unit scaled
        # by ``res_cv_depth`` -- ``q *= 2 ** (res_cv_depth * mean(cv))``.
        # _filter_coeffs clips the result to (0.1, 20), so a runaway CV
        # pins the Q at the rail instead of blowing the biquad up. Left
        # untouched when unpatched, so today's renders stay bit-exact.
        if resonance_cv is not None and resonance_cv.size > 0:
            q = q * self._q_cv_ratio(module, self._finite_mean(resonance_cv))

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

    def _render_filter_voice(
        self, module, frames, src_buf, cutoff_cv, resonance_cv=None
    ):
        """Voice-aware path -- V parallel biquads via lfilter, ``(V, F)``.

        Filter vectorization slice 4. Two shapes:

        * Shared coefficients (static cutoff, or a mono/macro
          cutoff_cv): one lfilter call filters all V rows along the
          time axis with ``zi`` of shape (V, 2) -- the 46x spike case.
        * Per-voice coefficients ((V, F) cutoff_cv -> V cutoffs, or
          (V, F) resonance_cv -> V Qs, or both): lfilter cannot vary
          coefficients across rows, so V independent single-row
          calls. Each row's recurrence still runs in C; smaller but
          real win. Whichever of the two is NOT per-voice stays a
          scalar and broadcasts across the (V,) coefficient arrays.

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
        # its own block-mean. Otherwise single shared cutoff. The Q
        # splits the same way on resonance_cv; either one being
        # per-voice takes the per-voice coefficient path.
        per_voice_cutoff = (
            cutoff_cv is not None
            and cutoff_cv.ndim == 2
            and cutoff_cv.shape[0] == V
            and cutoff_cv.size > 0
        )
        per_voice_q = (
            resonance_cv is not None
            and resonance_cv.ndim == 2
            and resonance_cv.shape[0] == V
            and resonance_cv.size > 0
        )

        cv_depth = float(module.params.get("cv_depth", 1.0))
        if per_voice_cutoff or per_voice_q:
            sr = self.sample_rate
            if per_voice_cutoff:
                cv_block_mean = self._finite_mean(cutoff_cv, axis=1)  # (V,)
                cutoff_per_voice = base_cutoff * self._pow2_clipped(cv_depth * cv_block_mean)
                cutoff_per_voice = np.clip(cutoff_per_voice, 20.0, sr * 0.45)
            else:
                # Shared cutoff (static, or a macro (F,) cutoff_cv) as
                # a scalar; it broadcasts against the per-voice Q.
                cutoff = base_cutoff
                if cutoff_cv is not None and cutoff_cv.size > 0:
                    cutoff = cutoff * self._pow2_clipped(
                        cv_depth * self._finite_mean(cutoff_cv)
                    )
                cutoff_per_voice = max(20.0, min(cutoff, sr * 0.45))
            if per_voice_q:
                q_clamped = np.clip(
                    q * self._q_cv_ratio(
                        module, self._finite_mean(resonance_cv, axis=1)
                    ),
                    0.1,
                    20.0,
                )  # (V,)
            else:
                if resonance_cv is not None and resonance_cv.size > 0:
                    q = q * self._q_cv_ratio(
                        module, self._finite_mean(resonance_cv)
                    )
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

            # a0 is (V,) whichever side varied, so every coefficient
            # comes out (V,) and the per-row lfilter loop below runs.
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
                cutoff = cutoff * self._pow2_clipped(
                    cv_depth * self._finite_mean(cutoff_cv)
                )
            if resonance_cv is not None and resonance_cv.size > 0:
                q = q * self._q_cv_ratio(module, self._finite_mean(resonance_cv))
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
            freq = freq * self._pow2_clipped(cv_depth * self._finite_mean(freq_cv))

        if src.ndim == 2:
            return self._render_crossover_voice(module, frames, src, freq)
        return self._render_crossover_mono(module, frames, src, freq)

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
                base = base * self._pow2_clipped(cv_depth * self._finite_mean(cv))
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
                base = base + gain_cv_depth * self._finite_mean(cv)
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
                base = base * self._pow2_clipped(q_cv_depth * self._finite_mean(cv))
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
            freq = freq * self._pow2_clipped(cv_depth * self._finite_mean(freq_cv))

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

    # ----- Loudness (equal-loudness contour) rendering --------------------

    _LOUD_F_LOW = 120.0      # low-shelf corner (Hz)
    _LOUD_F_HIGH = 8000.0    # high-shelf corner (Hz)
    _LOUD_BASS_MAX = 12.0    # auto bass boost (dB) at level -> 0
    _LOUD_TREBLE_MAX = 7.0   # auto treble boost (dB) at level -> 0

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
        cvs = self._finite_mean(level_cv) if level_cv is not None and level_cv.size else 0.0
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
        cvs = self._finite_mean(tilt_cv) if tilt_cv is not None and tilt_cv.size else 0.0
        tilt_eff = float(np.clip(tilt + cv_depth * cvs, -18.0, 18.0))
        coeffs = self._tilt_eq_coeffs(pivot, tilt_eff)

        if src.ndim == 2:
            return self._render_loudness_voice(module, frames, src, coeffs)
        return self._render_loudness_mono(module, frames, src, coeffs)

    # ----- Vowel (formant filter) ---------------------------------------------

    _VOWEL_RES_MIN = 0.25
    _VOWEL_RES_MAX = 4.0
    #: The formant-shift exponent (``formant``/12 + depth x mean cv,
    #: octaves) is clipped to +-4 BEFORE ``2 ** e`` -- the house overflow
    #: guard (the filter pass found an absurd CV can overflow the power).
    _VOWEL_SHIFT_OCT_LIMIT = 4.0
    #: ``cv_rate`` "sample": the grid the per-sample vowel is rounded to,
    #: in vowels. Five biquads cannot be rebuilt every sample, so the
    #: block is split into runs of constant quantised vowel instead.
    #: Measured (220 Hz sine, 30 Hz triangle across the whole range,
    #: against a true per-sample rebuild): 0.02 vowels is -62 dB and
    #: ~70 runs per 512-sample block; 0.05 is -55 dB, 0.1 only -43 dB
    #: and audibly steppy; 0.01 buys 6 dB more for twice the runs.
    #: 0.02 vowels is ~1% of a formant frequency, about 17 cents.
    _VOWEL_CV_STEP = 0.02

    def _vowel_coefs(self, voice, vowel, resonance, ratio, custom=None):
        """The five RBJ constant-peak bandpasses for ONE vowel position.

        Factored out of ``_render_vowel`` so the block path and the
        per-sample path build coefficients with the same arithmetic --
        the block path has to stay bit-identical to the shipped filter.
        Frequency and bandwidth are both multiplied by ``ratio`` (the
        formant shift), so Q = F/BW is preserved; a frequency past
        0.45 sr parks there. ``custom`` (or None) is the ``custom``
        voice's five frequencies.
        """
        from ...modules.vowel import N_FORMANTS, vowel_formants

        freqs, gains, bws = vowel_formants(voice, vowel, custom)
        sr = float(self.sample_rate)
        coefs = []
        for k in range(N_FORMANTS):
            f = min(freqs[k] * ratio, 0.45 * sr)
            q = max(0.1, (f / (bws[k] * ratio)) * resonance)
            w0 = 2.0 * np.pi * f / sr
            alpha = np.sin(w0) / (2.0 * q)
            a0 = 1.0 + alpha
            b = np.array([alpha / a0, 0.0, -alpha / a0])
            a = np.array([1.0, -2.0 * np.cos(w0) / a0, (1.0 - alpha) / a0])
            coefs.append((b, a, gains[k]))
        return coefs

    def _vowel_per_sample(self, st, x, cv, vowel, cv_depth, voice, resonance,
                          ratio, custom):
        """``cv_rate`` "sample": the vowel follows the CV sample by sample.

        Five biquads per sample is not affordable, so the MODULATION is
        quantised: the CV's contribution is rounded to
        ``_VOWEL_CV_STEP`` vowels, the block is split into runs where
        that rounded vowel is constant, and each run is one ``lfilter``
        call per formant with ``zi`` carried across the seam -- the
        filter state is never reset, which is why the seams do not
        click (measured: the max sample-to-sample step of a swept sine
        matches a true per-sample rebuild's to within 0.3%).

        The grid is relative to the KNOB, not absolute, so the knob is
        always exact: an idle jack (or ``cv_depth`` 0) rounds to step 0
        and gives the block-mean render bit for bit. The STEP COUNT is
        what gets clipped to the 0..4 range, so samples pinned at A or
        U collapse into one run instead of one each, and the cast to
        int cannot overflow on an absurd CV. Coefficients are cached
        per step (at most 201 of them) and the cache is dropped when
        anything else about the filter changes.
        """
        q = self._VOWEL_CV_STEP
        c = np.asarray(cv, dtype=np.float64)
        c = np.where(np.isfinite(c), c, 0.0)          # scrub before the clamp
        lo = int(math.ceil((0.0 - vowel) / q))
        hi = int(math.floor((4.0 - vowel) / q))
        gi = np.clip(np.rint(cv_depth * c / q), lo, hi).astype(np.int64)
        group = (voice, round(vowel, 9), round(resonance, 6), round(ratio, 9), custom)
        if st.get("group") != group:
            st["group"] = group
            st["cache"] = {}
        cache = st["cache"]
        cuts = np.flatnonzero(gi[1:] != gi[:-1]) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [gi.size]))
        wet = np.empty_like(x)
        for a, b in zip(starts, ends):
            step = int(gi[a])
            coefs = cache.get(step)
            if coefs is None:
                coefs = cache[step] = self._vowel_coefs(
                    voice, vowel + step * q, resonance, ratio, custom)
            seg = x[:, a:b]
            acc = np.zeros_like(seg)
            for k, (bb, aa, g) in enumerate(coefs):
                y, zf = lfilter(bb, aa, seg, axis=-1, zi=st["zi"][k])
                st["zi"][k] = zf
                acc += g * y
            wet[:, a:b] = acc
        last = int(gi[-1])
        st["key"] = (voice, round(vowel + last * q, 6), round(resonance, 6),
                     round(ratio, 9), custom)
        st["coefs"] = cache[last]
        return wet

    def _render_vowel(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Five-formant vowel filter (see modules/vowel.py).

        ``vowel_formants(voice, vowel_eff, custom)`` gives F1..F5 (Hz),
        linear gains and bandwidths for the block's effective vowel (the
        knob plus ``cv_depth`` x the block-mean CV, clamped 0..4); each
        formant is an RBJ constant-peak bandpass with Q = F/BW x
        ``resonance``, run by one ``lfilter`` call along the last axis
        (so a ``(V, F)`` input is V parallel filters with ``zi`` of shape
        (V, 2)) with the state carried across blocks.

        ``formant`` (semitones) + ``formant_cv_depth`` x the block-mean
        ``formant_cv`` (octaves) is the throat size: every formant's
        frequency AND bandwidth are multiplied by ``2 ** shift`` (the
        exponent clipped to +-4 first), so Q = F/BW is preserved -- the
        same vowel in a smaller (up) or larger (down) mouth. A frequency
        pushed past 0.45 sr parks there. Coefficients are rebuilt only
        when the (voice, vowel_eff, resonance, ratio) key changes. The
        five outputs are summed with the table's gains, ``gain`` dB
        applied, then ``out = dry (1 - mix) + wet mix``; at ``mix`` 0 the
        input buffer is returned untouched -- the effects neutral.

        ``cv_rate`` "sample" hands ``vowel_cv`` to
        ``_vowel_per_sample`` instead of averaging it -- the vowel then
        follows the CV per sample, quantised into runs (see there).
        ``voice`` "custom" reads ``f1``..``f5`` and passes them as the
        formant FREQUENCIES; the bandwidths and levels stay the tenor
        table's, so ``vowel`` goes on morphing the resonances.
        """
        from ...modules.vowel import (
            N_FORMANTS,
            VOWEL_CUSTOM_DEFAULT_FREQS,
            VOWEL_CUSTOM_FREQ_MAX,
            VOWEL_CUSTOM_FREQ_MIN,
            VOWEL_CV_RATES,
            VOWEL_VOICE_CHOICES,
            VOWEL_VOICE_CUSTOM,
        )

        x_in = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if x_in is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)

        def fparam(name, default, lo, hi):
            try:
                v = float(module.params.get(name, default))
            except (TypeError, ValueError):
                v = default
            return min(hi, max(lo, v))

        mix = fparam("mix", 1.0, 0.0, 1.0)
        if mix <= 0.0:
            self._state.pop(module.id, None)
            return x_in
        vowel = fparam("vowel", 0.0, 0.0, 4.0)
        voice = str(module.params.get("voice", "tenor"))
        if voice not in VOWEL_VOICE_CHOICES:
            voice = "tenor"
        # The custom voice's own formant frequencies. Read only for
        # ``custom`` so the table voices keep hashing to a key of None
        # -- the f1..f5 knobs must not invalidate their coefficients.
        custom = None
        if voice == VOWEL_VOICE_CUSTOM:
            custom = tuple(
                fparam(f"f{k + 1}", VOWEL_CUSTOM_DEFAULT_FREQS[k],
                       VOWEL_CUSTOM_FREQ_MIN, VOWEL_CUSTOM_FREQ_MAX)
                for k in range(N_FORMANTS)
            )
        resonance = fparam("resonance", 1.0, self._VOWEL_RES_MIN, self._VOWEL_RES_MAX)
        gain = 10.0 ** (fparam("gain", 6.0, -12.0, 24.0) / 20.0)
        cv_depth = fparam("cv_depth", 2.0, -10.0, 10.0)
        cv_rate = str(module.params.get("cv_rate", "block"))
        if cv_rate not in VOWEL_CV_RATES:
            cv_rate = "block"
        cv = self._input_buffer(patch, buffers, module.id, "vowel_cv")
        # "sample" needs a CV that is actually one value per frame; a
        # short or odd buffer falls soft back to the block mean, and so
        # does depth 0 (where the two are the same render anyway, and
        # the block path is the cheap one).
        per_sample = (
            cv_rate == "sample" and cv is not None and cv.ndim == 1
            and frames > 0 and cv.shape[0] == frames and cv_depth != 0.0
        )
        if cv is not None and cv.size and not per_sample:
            # The mean is taken in float64 for the same reason as
            # formant_cv below: np.mean of a float32 buffer ACCUMULATES in
            # float32, so a CONSTANT cv of 0.3 reads 0.29999998 over 64
            # samples and 0.30000001 over 512 -- an ulp that moves the
            # coefficients and breaks block-size independence. n * v is
            # exact in 53 bits, so the float64 mean of a constant is that
            # constant at any block size. A non-finite mean is ignored
            # (the knob alone), because Python's min/max do not propagate
            # NaN -- they silently collapse it to A.
            cv_mean = float(np.mean(cv, dtype=np.float64))
            if np.isfinite(cv_mean):
                vowel = min(4.0, max(0.0, vowel + cv_depth * cv_mean))
        # The throat size: semitones on the knob, octaves per unit on the
        # jack, one value per block for every voice (like vowel_cv). The
        # mean is taken in float64: a float32 accumulation of a CONSTANT
        # 0.3 gives 0.29999998 over 64 samples and 0.30000001 over 512,
        # an ulp that moves the coefficients and breaks block-size
        # independence; n * v is exact in 53 bits, so the float64 mean
        # of a constant is that constant at any block size.
        shift = fparam("formant", 0.0, -24.0, 24.0) / 12.0
        fcv = self._input_buffer(patch, buffers, module.id, "formant_cv")
        if fcv is not None and fcv.size:
            fcv_mean = float(np.mean(fcv, dtype=np.float64))
            if np.isfinite(fcv_mean):
                shift += fparam("formant_cv_depth", 1.0, -10.0, 10.0) * fcv_mean
        lim = self._VOWEL_SHIFT_OCT_LIMIT
        ratio = float(2.0 ** min(max(shift, -lim), lim))

        voiced = x_in.ndim == 2
        x = (x_in if voiced else x_in[None, :]).astype(np.float64)
        V = x.shape[0]

        st = self._state.get(module.id)
        if st is None or st.get("V") != V:
            st = self._state[module.id] = {
                "V": V, "key": None, "coefs": None, "group": None, "cache": {},
                "zi": np.zeros((N_FORMANTS, V, 2), dtype=np.float64),
            }
        if per_sample:
            wet = self._vowel_per_sample(st, x, cv, vowel, cv_depth, voice,
                                         resonance, ratio, custom)
        else:
            # Frequency and bandwidth scale together (constant Q); a ratio
            # of exactly 1.0 leaves both bit-identical to the table, so an
            # unshifted render is the pre-shift render. ``custom`` is None
            # for every table voice, so their key is the shipped key.
            key = (voice, round(vowel, 6), round(resonance, 6), round(ratio, 9),
                   custom)
            if st["key"] != key:
                st["key"] = key
                st["coefs"] = self._vowel_coefs(voice, vowel, resonance, ratio,
                                                custom)
            wet = np.zeros_like(x)
            for k, (b, a, g) in enumerate(st["coefs"]):
                y, zf = lfilter(b, a, x, axis=-1, zi=st["zi"][k])
                st["zi"][k] = zf
                wet += g * y
        wet *= gain
        out = wet if mix >= 1.0 else x * (1.0 - mix) + wet * mix
        result = out if voiced else out[0]
        return result.astype(np.float32)
