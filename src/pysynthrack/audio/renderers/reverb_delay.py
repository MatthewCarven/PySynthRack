"""Time-based effect renderers: reverb and delay.

Moved verbatim out of ``numpy_backend.py`` (2026-09-26) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". Both freezes reach the backend's shared gate helpers
(``_freeze_gate_row``, ``_gate_ramp_env``) through ``self``.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter


class ReverbDelayRenderers:
    # ----- Reverb rendering -----------------------------------------------

    # Eight delay-line lengths (samples at 44.1 kHz, near-prime so the
    # modes don't line up and ring) for the largest "hall" size; `size`
    # scales them down toward a small room. Time-scaled to the real
    # sample rate at render time.
    _REVERB_BASE = (1103, 1321, 1543, 1759, 1987, 2203, 2423, 2647)
    _REVERB_OUT = 0.30   # wet output trim (tuned so wet ~ dry level)
    # Freeze blend ramp (s): the loop gain, the damping bypass and the
    # input mute all follow the ``freeze`` gate through one linear ramp
    # this long from each edge -- short enough to catch the moment, long
    # enough that the switch is inaudible inside the tail.
    _REVERB_FREEZE_RAMP_S = 0.010

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

        ``freeze`` (gate): while high the tank holds its tail as a pad.
        A per-sample blend ``f`` (0 = normal, 1 = frozen) follows the
        gate through an integer-count ramp (``_gate_ramp_env``, so the
        edge lands on the same sample at any block size) and, per sample,
        crossfades the damped read back toward the raw read, lifts every
        line's decay gain toward exactly 1.0 and mutes the injection.
        The Hadamard matrix is orthonormal, so the unity loop is lossless:
        the held tail neither decays nor grows. The damping one-pole keeps
        tracking the raw read while bypassed, so its state is warm the
        moment the gate falls. Unpatched, the hop loop below is the
        pre-freeze code verbatim (bit-exact); the dry path never sees the
        gate at all. The ``freeze`` param (the panel tickbox) is ORed
        with the gate: on, the row is all-high from the first sample of
        the block it is seen on (a rising edge there, ramp and all);
        off again, it releases like a gate fall.
        """
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z.copy()}
        # A (V, F) gate collapses to the house sum: any voice high freezes.
        fz_gate = self._input_buffer(patch, buffers, module.id, "freeze")

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
            decay = decay + cv_depth * self._finite_mean(decay_cv)
        damping_cv = self._input_buffer(patch, buffers, module.id, "damping_cv")
        if damping_cv is not None and damping_cv.size > 0:
            damping = damping + cv_depth * self._finite_mean(damping_cv)
        mix_cv = self._input_buffer(patch, buffers, module.id, "mix_cv")
        if mix_cv is not None and mix_cv.size > 0:
            mix = mix + cv_depth * self._finite_mean(mix_cv)

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
            # freeze ramp: (gate at the last sample, on/off counts since
            # the last edge, level at the last falling edge)
            state["fz_prev"] = False
            state["fz_on"] = 0
            state["fz_off"] = 0
            state["fz_env"] = 0.0

        buf = state["buf"]
        wp = int(state["write_idx"])
        lpz = state["lpz"]

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        # --- freeze blend: one 0..1 row per block, or None when nothing
        # asks for it (the hop loop then runs its pre-freeze code). The
        # row is the gate cable ORed with the ``freeze`` tickbox
        # (``_freeze_gate_row``): unpatched and un-ticked never enters
        # here; the tick alone rises at sample 0 of the block it is seen
        # on and, cleared, releases through the same ramp as a gate fall.
        f = None
        ramp_n = max(1, int(round(self._REVERB_FREEZE_RAMP_S * sr)))
        gt = self._freeze_gate_row(
            fz_gate, frames, bool(module.params.get("freeze", False)),
            state, ramp_n)
        if gt is not None:
            f, fz_on, fz_off, fz_env = self._gate_ramp_env(
                gt, bool(state["fz_prev"]), int(state["fz_on"]),
                int(state["fz_off"]), float(state["fz_env"]), ramp_n, ramp_n)
            state["fz_prev"] = bool(gt[-1])
            state["fz_on"] = fz_on
            state["fz_off"] = fz_off
            state["fz_env"] = fz_env

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
            xin = xd[pos:pos + c]
            gl = g[:, np.newaxis]
            if f is not None and np.any(f[pos:pos + c]):
                # Frozen or ramping: per sample, the damped read slides
                # back to the raw read, every line's gain to exactly 1.0
                # and the injection to silence. A convex blend of a
                # signal and its own low-pass never exceeds the signal,
                # so the loop gain is <= 1 on the way in as well.
                fh = f[pos:pos + c][np.newaxis, :]
                Sd = Sd + (S - Sd) * fh
                gl = gl + (1.0 - gl) * fh
                xin = xin * (1.0 - fh[0])
            fb = A @ (gl * Sd)                                        # (N, c)
            buf[rows[:, None], idx % Lmax] = xin[np.newaxis, :] + fb
            wp += c
            pos += c

        state["write_idx"] = int(wp % Lmax)
        state["lpz"] = lpz

        dry = (1.0 - mix) * x
        out_l = (dry + mix * wet_l).astype(np.float32)
        out_r = (dry + mix * wet_r).astype(np.float32)
        return {"out_l": out_l, "out_r": out_r}

    # ----- Delay rendering ------------------------------------------------

    # Longest delay the line can address, in milliseconds. The ring buffer
    # is sized from this; both the ``time`` slider and the ``time_cv``
    # modulation are clamped to it.
    _DELAY_MAX_MS = 2000.0
    # Shortest delay, in samples. >= 2 keeps both linear-interpolation taps
    # strictly behind the write head (never reads the sample being written).
    _DELAY_MIN_SAMP = 2.0
    # Freeze blend ramp (s): the loop gain, the damping bypass, the input
    # mute and the read position all follow the ``freeze`` gate through
    # one integer-count linear ramp this long from each edge -- the
    # reverb's 10 ms, for the same reason: short enough to catch the
    # moment, long enough that the switch is inaudible inside the echo.
    _DELAY_FREEZE_RAMP_S = 0.010

    def _render_delay(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Analog-voiced feedback delay (echo) with a damped feedback path.

        Shape-polymorphic like the other effects. Branches on the audio
        input's ndim: 1D ``(F,)`` -> one delay line, ``(F,)`` out; 2D
        ``(V, F)`` -> one delay line per voice slot, ``(V, F)`` out. A mono
        ``time_cv`` broadcasts across voices; a ``(V, F)`` ``time_cv``
        modulates each independently. Missing audio in -> silence, with the
        line left intact so reconnecting the cable doesn't snap the tail.

        ``freeze`` (gate): while high the echo hangs -- see the core below.
        A ``(V, F)`` gate collapses to the house sum (any voice high freezes)
        and the one row is shared by every voice's line.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            return np.zeros(frames, dtype=np.float32)

        time_cv = self._input_buffer(
            patch, buffers, module.id, "time_cv", collapse=False
        )
        fz_gate = self._input_buffer(patch, buffers, module.id, "freeze")

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
                    self._voice_mean(time_cv), (V, time_cv.shape[1])
                )
            return self._render_delay_core(module, frames, src, cv, fz_gate)

        # Mono audio. A 2D time_cv collapses to one shared modulation
        # (mean over voices) -- summing time voltages would be nonsense.
        if time_cv is not None and time_cv.ndim == 2:
            time_cv = self._voice_mean(time_cv)
        out = self._render_delay_core(
            module,
            frames,
            src[np.newaxis, :],
            None if time_cv is None else time_cv[np.newaxis, :],
            fz_gate,
        )
        return out[0]

    def _render_delay_core(self, module, frames, src, cv, fz=None):
        """Shared ``(V, F)`` feedback-delay engine.

        Per output sample: read the line ``delay`` samples back with linear
        interpolation; low-pass that read for the feedback path so each
        recirculation darkens (the analog voicing); write ``in + feedback *
        damped`` into the line; and mix the *un-damped* read into the dry
        signal. The mono path calls this with ``V == 1``, so a single voice
        row is bit-identical to the mono render -- the float ops are the
        same per row regardless of V.

        Per-sample (not block-vectorized) because the feedback recirculation
        is sequential when the delay is shorter than a block; when every
        read this block lands at least a block back, a vectorized fast path
        runs instead. Exact at any block size (2026-09-24): the read splits
        the delay into whole samples and a fraction rather than forming
        ``index - delay`` (a ring index rounds at its own magnitude), and
        both paths spell the damping one-pole as ``lfilter`` evaluates it,
        since which path a block takes depends on the block size.

        ``fz`` (the ``freeze`` gate row, or None): while high the echo
        hangs. A per-sample blend ``e`` (0 = normal, 1 = frozen) follows
        the gate through an integer-count ramp (``_gate_ramp_env``, so the
        edge lands on the same sample at any block size) and, per sample,
        slides the fed-back signal from the damped read to the raw read,
        the loop gain from ``feedback`` to exactly 1.0, the input into the
        line to silence, and the read position to a WHOLE number of
        samples. That last one is what makes the hold lossless: a linear-
        interpolated read is a two-tap low-pass, and a unity loop through
        it loses ~9-11 dB in 10 s on a bright echo (measured, any fraction
        from 0.1 to 0.5), while an integer read is ``buf[wp] = buf[wp - D]``
        bit for bit -- the held loop neither decays nor grows. The held
        delay ``D = round(delay at the rising edge)`` is latched per voice
        at a fresh rise (one the ramp meets fully released) and ``time_cv``
        / the knob no longer move the read while held; a re-rise inside
        the 10 ms release keeps the previous hold so the read position
        never jumps mid-blend. The damping one-pole keeps tracking the raw
        read while bypassed, so it is warm the moment the gate falls. The
        dry path and ``mix`` never see the gate. Unpatched (or never
        risen), both paths below run their pre-freeze code verbatim. The
        ``freeze`` param (the panel tickbox) is ORed with the gate: on,
        the row is all-high from the first sample of the block it is seen
        on (a rising edge there, ramp and latch and all); off again, it
        releases like a gate fall.
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
            # freeze: ramp state (gate at the last sample, on/off counts
            # since the last edge, level at the last falling edge, level
            # at the last sample) and the held delay per voice (samples,
            # whole-valued; latched at a fresh rising edge).
            state["fz_prev"] = False
            state["fz_on"] = 0
            state["fz_off"] = 0
            state["fz_env"] = 0.0
            state["fz_last"] = 0.0
            state["fz_dly"] = np.zeros(V, dtype=np.float64)

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

        # --- freeze blend: one 0..1 row per block, or None when the gate
        # is unpatched or has not risen (both paths below then run their
        # pre-freeze code verbatim, so the feature ships bit-exact OFF).
        # The row is the gate cable ORed with the ``freeze`` tickbox
        # (``_freeze_gate_row``): unpatched and un-ticked never enters
        # here; the tick alone rises at sample 0 of the block it is seen
        # on and, cleared, releases through the same ramp as a gate fall.
        e = None
        ramp_n = max(1, int(round(self._DELAY_FREEZE_RAMP_S * sr)))
        gt = self._freeze_gate_row(
            fz, frames, bool(module.params.get("freeze", False)), state, ramp_n)
        if gt is not None:
            prev = bool(state["fz_prev"])
            env, fz_on, fz_off, fz_env = self._gate_ramp_env(
                gt, prev, int(state["fz_on"]), int(state["fz_off"]),
                float(state["fz_env"]), ramp_n, ramp_n)
            # The held delay latches at a FRESH rising edge (one the ramp
            # meets fully released): the per-sample delay at that sample,
            # rounded to whole samples, per voice. It is a per-sample row
            # so two fresh rises in one block hold two different times --
            # the same ones a smaller block would hold.
            fz_dly = state["fz_dly"]
            fzd = None
            rises = np.flatnonzero(gt & ~np.concatenate(([prev], gt[:-1])))
            if len(rises):
                fzd = np.empty((V, frames), dtype=np.float64)
                pos = 0
                for n_r in rises.tolist():
                    before = float(env[n_r - 1]) if n_r > 0 else float(state["fz_last"])
                    if before == 0.0:
                        fzd[:, pos:n_r] = fz_dly[:, np.newaxis]
                        fz_dly = np.round(dly[:, n_r])
                        pos = n_r
                fzd[:, pos:] = fz_dly[:, np.newaxis]
                state["fz_dly"] = fz_dly
            state["fz_prev"] = bool(gt[-1])
            state["fz_on"] = fz_on
            state["fz_off"] = fz_off
            state["fz_env"] = fz_env
            state["fz_last"] = float(env[-1])
            if np.any(env):
                e = env
                if fzd is None:
                    fzd = fz_dly[:, np.newaxis]
                # The read slides to the held whole-sample delay on the
                # same ramp (at most half a sample over 10 ms -- a pitch
                # nudge, not a click) and stays there: at e == 1 this is
                # exactly ``fzd``, and time_cv no longer moves it.
                dly = dly * (1.0 - e) + fzd * e

        # THE TRAP (the chorus's and the tape's): a ring index rounds at its
        # own magnitude. ``wp + n - dly`` looks exact and is not -- the fast
        # path forms it from an index that has not wrapped yet this block,
        # the per-sample path from one that wrapped at a block boundary, so
        # the same sample read a fraction a float64 ulp apart depending on
        # the block size. Split the delay into WHOLE samples and a FRACTION
        # instead, both functions of the small delay alone (``back - dly``
        # is exact: Sterbenz, dly >= 2), and index the ring with integers.
        # At a whole-sample delay (the held freeze) ``frac`` is exactly 0.
        back = np.ceil(dly)                                  # (V, F)
        fracs = back - dly                   # forward weight, in [0, 1)
        backs = back.astype(np.int64)
        # The damping one-pole's coefficients, spelled the way ``lfilter``
        # evaluates them, so the per-sample path below computes the same
        # bits as the fast path's ``lfilter`` (``g * d + (1 - g) * lp``,
        # not ``lp + g * (d - lp)``, which rounds differently). Which path
        # a block takes depends on the block size, so the two must agree.
        c1 = 1.0 - g
        rows = np.arange(V)
        if float(dly.min()) >= frames:
            # Fast path: every read this block lands at least one block back,
            # so no read depends on a sample written this block and the whole
            # block vectorizes. The damping one-pole runs via ``lfilter`` with
            # its state carried in ``zi``. This is the common echo case
            # (any musical delay time is many blocks long).
            absidx = wp + np.arange(frames)                  # (F,) absolute
            i0 = absidx[np.newaxis, :] - backs               # (V, F) whole
            frac = fracs
            d = (
                buf[rows[:, None], i0 % L] * (1.0 - frac)
                + buf[rows[:, None], (i0 + 1) % L] * frac
            )
            zi = (c1 * lp)[:, np.newaxis]                    # (V, 1)
            damped = lfilter([g], [1.0, -c1], d, axis=-1, zi=zi)[0]
            if e is None:
                buf[rows[:, None], absidx % L] = x + feedback * damped
            else:
                # Frozen or ramping: per sample, the fed-back signal slides
                # from the damped read to the raw read, the loop gain from
                # ``feedback`` to exactly 1.0 and the input to silence --
                # each as ``a * (1 - e) + b * e``, exact at both ends, so at
                # e == 1 the write is ``buf[wp] = d = buf[wp - D]`` bit for
                # bit. Convex blends on the way in: the loop gain never
                # exceeds 1, so the ramp cannot grow the loop either.
                eh = e[np.newaxis, :]
                om = 1.0 - eh
                fb_sig = damped * om + d * eh
                gain = feedback * om + eh
                buf[rows[:, None], absidx % L] = x * om + gain * fb_sig
            out = x * (1.0 - mix) + d * mix
            lp = damped[:, -1].copy()
            wp = (wp + frames) % L
        else:
            # Per-sample path: the delay dips below a block (short or heavily
            # modulated), so the feedback recirculation is sequential.
            out = np.empty((V, frames), dtype=np.float64)
            if e is not None:
                om = 1.0 - e
                gain = feedback * om + e
            for n in range(frames):
                i0 = wp - backs[:, n]                        # (V,)
                frac = fracs[:, n]
                d = (
                    buf[rows, i0 % L] * (1.0 - frac)
                    + buf[rows, (i0 + 1) % L] * frac
                )
                lp = g * d + c1 * lp                         # damped feedback
                if e is None:
                    buf[rows, wp % L] = x[:, n] + feedback * lp
                else:
                    # the same blends as the fast path, one sample at a time
                    buf[rows, wp % L] = (
                        x[:, n] * om[n] + gain[n] * (lp * om[n] + d * e[n])
                    )
                out[:, n] = x[:, n] * (1.0 - mix) + d * mix
                wp += 1
            wp = wp % L

        state["write_idx"] = int(wp)
        state["lp"] = lp
        return out.astype(np.float32)
