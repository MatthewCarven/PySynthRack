"""Colour renderers: distortion, waveshaper (wavefolder), bitcrusher, ring
modulator, frequency shifter, vinyl, octaver.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". Shared helpers live in the ``_shared`` mixin and are reached
through ``self``: ``_dist_curve`` and its ``_TUBE_BIAS`` (tape's saturation uses
the curve too), ``_ring_match_voices`` (``fm_op`` uses it too), and the
octaver's ``_audio_to_cv_block`` / ``_audio_to_cv_loop_mono`` core, with
the generic ``_finite_mean`` / ``_pow2_clipped``. Module-level
primitives of ``numpy_backend`` come in through lazy imports at the point
of use, once per block (``numpy_backend`` imports this module, so a
top-level import would be circular): the 4x oversampler, its latency and
``_dc_block`` for distortion and waveshaper, and the Hilbert FIR
(``_FS_HILBERT`` / ``_FS_LATENCY``) for the frequency shifter.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter


class ColourRenderers:
    # ----- Vinyl rendering -------------------------------------------------

    _VINYL_WINDOW = 4096          # absolute-sample noise window (seeding)
    _VINYL_TICK_RATE = 30.0       # dust ticks/second at crackle = 1
    _VINYL_TICK_KERNEL = (0.5, 1.0, -0.4, 0.15)  # LP-shaped click
    _VINYL_CRACKLE_AMP = 0.35
    _VINYL_RUMBLE_AMP = 0.15
    _VINYL_RUMBLE_F = 40.0
    _VINYL_RUMBLE_Q = 1.8
    _VINYL_WOBBLE_F = 0.55        # Hz — once per 33 1/3 rpm revolution
    _VINYL_WOBBLE_D = 0.005       # nominal delay (s) under the wobble
    _VINYL_WOBBLE_A = 0.004       # max modulation depth (s) at wobble 1

    def _render_vinyl(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Surface noise + warp (see modules/vinyl.py).

        Determinism/block-independence design: both noise streams are
        drawn per absolute-sample WINDOW — a ``default_rng([seed,
        window_index, stream])`` re-derives the same dust for any block
        split — and the wobble LFO phase comes from the absolute sample
        counter. The only carried DSP state is the wobble's input
        history and the rumble filter's ``zi`` (fed by the windowed
        white stream, so it too is split-invariant). All-zero knobs
        return the input buffer itself, bit-exact.
        """
        x = self._input_buffer(patch, buffers, module.id, "in")

        p = module.params
        crackle = min(1.0, max(0.0, float(p.get("crackle", 0.3))))
        rumble = min(1.0, max(0.0, float(p.get("rumble", 0.2))))
        wobble = min(1.0, max(0.0, float(p.get("wobble", 0.2))))
        try:
            seed = max(0, int(p.get("seed", 1)))
        except (TypeError, ValueError):
            seed = 1

        sr = float(self.sample_rate)
        hist_len = int(
            np.ceil((self._VINYL_WOBBLE_D + self._VINYL_WOBBLE_A) * sr)
        ) + 8

        st = self._state.setdefault(module.id, {})
        if st.get("hist_len") != hist_len:
            st.clear()
            st.update(
                {
                    "hist_len": hist_len,
                    "pos": 0,
                    "hist": np.zeros(hist_len, dtype=np.float64),
                    "rumble_zi": None,
                }
            )
        pos = int(st["pos"])

        x_arr = (
            np.zeros(frames, dtype=np.float64)
            if x is None
            else x.astype(np.float64)
        )
        # History always advances (engaging wobble mid-run has context).
        joined = np.concatenate((st["hist"], x_arr))
        st["hist"] = joined[-hist_len:].copy()
        st["pos"] = pos + frames

        if crackle == 0.0 and rumble == 0.0 and wobble == 0.0:
            # Bit-exact passthrough (the input buffer itself).
            return x if x is not None else np.zeros(frames, dtype=np.float32)

        if wobble > 0.0:
            n = np.arange(frames, dtype=np.float64)
            t = (pos + n) / sr
            delay = self._VINYL_WOBBLE_D + (
                self._VINYL_WOBBLE_A * wobble
            ) * np.sin(2.0 * np.pi * self._VINYL_WOBBLE_F * t)
            read = hist_len + n - delay * sr
            y = np.interp(read, np.arange(len(joined), dtype=np.float64), joined)
        else:
            y = x_arr.copy()

        W = self._VINYL_WINDOW
        kernel = np.asarray(self._VINYL_TICK_KERNEL, dtype=np.float64)
        K = len(kernel)

        if crackle > 0.0:
            lam = self._VINYL_TICK_RATE * crackle * W / sr
            for w in range(pos // W, (pos + frames - 1) // W + 1):
                rng = np.random.default_rng([seed, w, 1])
                count = int(rng.poisson(lam))
                if count == 0:
                    continue
                offs = rng.integers(0, W - K, size=count)
                amps = rng.uniform(0.25, 1.0, size=count) * np.where(
                    rng.random(count) < 0.5, -1.0, 1.0
                )
                for o, a in zip(offs, amps):
                    g = w * W + int(o) - pos  # tick start within block
                    s, e = max(g, 0), min(g + K, frames)
                    if s >= e:
                        continue
                    y[s:e] += kernel[s - g : e - g] * (
                        a * self._VINYL_CRACKLE_AMP * crackle
                    )

        if rumble > 0.0:
            white = np.empty(frames, dtype=np.float64)
            for w in range(pos // W, (pos + frames - 1) // W + 1):
                w_noise = np.random.default_rng([seed, w, 2]).standard_normal(W)
                s = max(w * W, pos)
                e = min((w + 1) * W, pos + frames)
                white[s - pos : e - pos] = w_noise[s - w * W : e - w * W]
            # RBJ resonant low-pass at ~40 Hz (the bearing).
            w0 = 2.0 * np.pi * self._VINYL_RUMBLE_F / sr
            alpha = np.sin(w0) / (2.0 * self._VINYL_RUMBLE_Q)
            cw = np.cos(w0)
            a0 = 1.0 + alpha
            b = np.array([(1 - cw) / 2, 1 - cw, (1 - cw) / 2]) / a0
            a = np.array([1.0, -2.0 * cw / a0, (1.0 - alpha) / a0])
            zi = st.get("rumble_zi")
            if zi is None:
                zi = np.zeros(2, dtype=np.float64)
            low, zf = lfilter(b, a, white, zi=zi)
            st["rumble_zi"] = zf
            y += low * (self._VINYL_RUMBLE_AMP * rumble)
        else:
            st["rumble_zi"] = None

        return y.astype(np.float32)

    _DIST_DRIVE_MIN = 0.01
    _DIST_DRIVE_MAX = 60.0
    _DIST_TONE_BYPASS = 19999.0  # tone at/above this -> filter out of circuit

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
        *exactly* block-size independent (with the CV inputs absent or
        constant -- a time-varying ``bits_cv`` / ``rate_cv`` uses each
        block's mean, so it tracks the block boundaries, exactly as the
        Filter's ``cutoff_cv`` does). Those optional CV inputs shift
        ``bits`` / ``rate_div`` by their block-mean (scaled by the matching
        ``*_cv_depth``) before the neutral-skip test, so CV can wake an
        otherwise-neutral crusher. Shape-polymorphic: the ``(V, F)`` core
        runs with ``V == 1`` for a mono ``in``, so one voice row is
        bit-identical to the mono render and voices stay independent.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None or src.size == 0:
            return np.zeros(frames, dtype=np.float32)

        bits = float(module.params.get("bits", 24))
        rate_div = float(module.params.get("rate_div", 1))
        jitter = min(max(float(module.params.get("jitter", 0.0)), 0.0), 1.0)
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)
        dc_filter = bool(module.params.get("dc_filter", False))

        # Bit-exact dry: mix folds everything to the input. Return before
        # touching the CV inputs so the dry contract is truly untouched.
        if mix <= 0.0:
            return src

        # CV-modulate the two crush amounts from the block-mean of each CV
        # input (one value per block, like the Filter's cutoff_cv). Unpatched
        # inputs leave bits/rate_div at their params, so an un-CV'd crusher is
        # byte-identical to before and stays exactly block-size independent
        # under a constant CV. ``bits`` shifts additively in its already-
        # logarithmic unit (each bit == one octave of quantiser levels);
        # ``rate_div`` shifts multiplicatively (octave-even decimation sweeps).
        bits_cv = self._input_buffer(
            patch, buffers, module.id, "bits_cv", collapse=False
        )
        if bits_cv is not None and bits_cv.size > 0:
            depth = float(module.params.get("bits_cv_depth", 1.0))
            bits = bits + depth * self._finite_mean(bits_cv)
        rate_cv = self._input_buffer(
            patch, buffers, module.id, "rate_cv", collapse=False
        )
        if rate_cv is not None and rate_cv.size > 0:
            depth = float(module.params.get("rate_cv_depth", 1.0))
            rate_div = rate_div * self._pow2_clipped(
                depth * self._finite_mean(rate_cv)
            )

        bits = int(round(bits))
        rate_div = int(round(rate_div))
        bits = min(max(bits, 1), 24)
        rate_div = min(max(rate_div, 1), 64)

        quant_active = bits < 24
        decim_active = rate_div > 1

        # Nothing to do: both crush ops skipped and no DC filter -> dry.
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

        from ..numpy_backend import _FS_HILBERT, _FS_LATENCY

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

        from ..numpy_backend import _OS_FACTOR, _OS_LATENCY, _dc_block, _Oversampler4

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

        from ..numpy_backend import _OS_FACTOR, _OS_LATENCY, _dc_block, _Oversampler4

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

    # Octaver envelope-follower time constants (the audio_to_cv core with
    # fixed, musical values): fast attack so note onsets land, slower
    # release so tails decay naturally instead of chattering.
    _OCTAVER_ATTACK_S = 0.005
    _OCTAVER_RELEASE_S = 0.05

    def _render_octaver(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Flip-flop sub-octave generator (see modules/octaver.py).

        Rising zero-crossings are found vectorized; the ÷2 flip-flop is
        the running parity of their cumsum (carried across blocks), and
        the ÷4 flip-flop is the parity of the FIRST flip-flop's rising
        edges — no per-sample loop anywhere. Each ±1 square rides the
        input's envelope (the shared ``_audio_to_cv_block`` asymmetric
        one-pole, fixed 5 ms / 50 ms), the summed subs go through one
        one-pole low-pass at ``tone`` (state carried), and the result
        mixes under the dry. ``dry`` 1 + subs 0 returns the input
        buffer itself — bit-exact passthrough (state still advances so
        re-enabling a sub doesn't restart the flip-flops).
        """
        audio_in = self._input_buffer(patch, buffers, module.id, "in")
        if audio_in is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)

        try:
            dry = float(module.params.get("dry", 1.0))
        except (TypeError, ValueError):
            dry = 1.0
        dry = min(1.0, max(0.0, dry))
        try:
            sub1 = float(module.params.get("sub1", 0.5))
        except (TypeError, ValueError):
            sub1 = 0.5
        sub1 = min(1.0, max(0.0, sub1))
        try:
            sub2 = float(module.params.get("sub2", 0.0))
        except (TypeError, ValueError):
            sub2 = 0.0
        sub2 = min(1.0, max(0.0, sub2))
        try:
            tone = float(module.params.get("tone", 800.0))
        except (TypeError, ValueError):
            tone = 800.0
        tone = min(2000.0, max(200.0, tone))

        st = self._state.setdefault(
            module.id,
            {"prev_pos": False, "ff1": 0, "ff2": 0, "level": 0.0, "lp": 0.0},
        )

        x = audio_in.astype(np.float64)
        pos = x > 0.0
        prev = np.empty_like(pos)
        prev[0] = bool(st["prev_pos"])
        prev[1:] = pos[:-1]
        crossings = (pos & ~prev).astype(np.int64)

        # ÷2: parity of the crossing count; ÷4: parity of ff1's rises.
        ff1 = (int(st["ff1"]) + np.cumsum(crossings)) & 1
        ff1_prev = np.empty_like(ff1)
        ff1_prev[0] = int(st["ff1"])
        ff1_prev[1:] = ff1[:-1]
        ff1_rises = ((ff1 == 1) & (ff1_prev == 0)).astype(np.int64)
        ff2 = (int(st["ff2"]) + np.cumsum(ff1_rises)) & 1
        st["prev_pos"] = bool(pos[-1])
        if frames:
            st["ff1"] = int(ff1[-1])
            st["ff2"] = int(ff2[-1])

        # Envelope: the shared asymmetric one-pole (fixed constants).
        sr = self.sample_rate
        attack_coef = 1.0 - float(np.exp(-1.0 / (self._OCTAVER_ATTACK_S * sr)))
        release_coef = 1.0 - float(np.exp(-1.0 / (self._OCTAVER_RELEASE_S * sr)))
        abs_in = np.abs(x)
        env = self._audio_to_cv_block(
            abs_in[None, :],
            np.array([float(st["level"])], dtype=np.float64),
            attack_coef,
            release_coef,
        )
        if env is None:
            env_row, level = self._audio_to_cv_loop_mono(
                abs_in, float(st["level"]), attack_coef, release_coef
            )
        else:
            env_row = env[0]
            level = float(env_row[-1]) if frames else float(st["level"])
        st["level"] = level

        subs = (
            sub1 * (ff1.astype(np.float64) * 2.0 - 1.0)
            + sub2 * (ff2.astype(np.float64) * 2.0 - 1.0)
        ) * env_row

        if sub1 == 0.0 and sub2 == 0.0:
            st["lp"] = 0.0  # tone filter idles; flip-flops kept warm above
            if dry == 1.0:
                return audio_in  # bit-exact passthrough
            return (x * dry).astype(np.float32)

        # One-pole LP on the summed subs (rounds the squares).
        coef = 1.0 - float(np.exp(-2.0 * np.pi * tone / sr))
        filtered, zf = lfilter(
            [coef], [1.0, coef - 1.0], subs,
            zi=np.array([(1.0 - coef) * float(st["lp"])], dtype=np.float64),
        )
        if frames:
            st["lp"] = float(filtered[-1])

        return (x * dry + filtered).astype(np.float32)
