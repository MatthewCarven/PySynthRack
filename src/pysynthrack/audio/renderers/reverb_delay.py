"""Time-based effect renderers: reverb and delay.

Moved verbatim out of ``numpy_backend.py`` (2026-09-26) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". Both freezes reach their gate helpers through ``self``:
``_freeze_gate_row``, which joined this mixin in the endgame, and
``_gate_ramp_env`` from ``_shared``.
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
    # Shortest hop, in samples, the block path runs in. A delay shorter
    # than a block runs as hops no longer than the delay; below this a hop
    # (an ``lfilter`` call and a fancy-indexed read and write) costs more
    # than the per-sample loop, so delays of 2 and 3 samples keep the loop.
    # Measured on a 512 block, one voice, per block: 5 ms 0.15 ms in hops
    # against 9.4 ms per sample, 1 ms 0.6 against 8.9, 12 samples 1.5
    # against 9.2, 4 samples 3.9 against 9.1 (the block's budget is 11.6).
    # The spec's run (2026-11-07) had the loop ahead at 3 samples (13.7 in
    # hops against 10.5); the build's (11-11) had hops ahead even there
    # (7.8 against 9.5), so 4 is the conservative end.
    _DELAY_HOP_FLOOR = 4

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
        # A (V, F) clock collapses to the house sum, like the freeze gate.
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        if src is None:
            # Nothing to echo, but a patched clock keeps being measured,
            # so the echo is already on time when the audio arrives (and
            # one unpatched meanwhile drops its measurement, as below).
            if clock is not None:
                self._delay_clock_edges(
                    self._state.setdefault(module.id, {}), clock, frames)
            else:
                st = self._state.get(module.id)
                if st is not None and "ck_n" in st:
                    self._delay_clock_edges(st, None, frames)
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
            return self._render_delay_core(
                module, frames, src, cv, fz_gate, clock)

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
            clock,
        )
        return out[0]

    def _render_delay_core(self, module, frames, src, cv, fz=None, clock=None):
        """Shared ``(V, F)`` feedback-delay engine.

        Per output sample: read the line ``delay`` samples back with linear
        interpolation; low-pass that read for the feedback path so each
        recirculation darkens (the analog voicing); write ``in + feedback *
        damped`` into the line; and mix the *un-damped* read into the dry
        signal. The mono path calls this with ``V == 1``, so a single voice
        row is bit-identical to the mono render -- the float ops are the
        same per row regardless of V.

        The path rule (2026-11-11): the block runs in hops no longer than
        its shortest delay, so within a hop every read predates the hop's
        writes and the hop vectorizes, the damping one-pole running via
        ``lfilter`` with its state carried. A delay of a block or more is a
        single block-length hop. Only a delay under ``_DELAY_HOP_FLOOR``
        (4) samples somewhere in the block takes the per-sample loop, where
        a hop would cost more than the loop. Exact at any block size
        (2026-09-24): the read splits the delay into whole samples and a
        fraction rather than forming ``index - delay`` (a ring index rounds
        at its own magnitude), and both paths spell the damping one-pole as
        ``lfilter`` evaluates it, since which path a block takes depends on
        the block size.

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
            # The clock's keys (``ck_*``) survive the re-init, as the
            # modulation effects' ``_MOD_CLOCK_KEYS`` do: a voice-count
            # change keeps the span it measured and the time it locked.
            ck = {k: v for k, v in state.items() if k.startswith("ck_")}
            state.clear()
            state.update(ck)
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

        # --- freeze blend: one 0..1 row per block, or None when the gate
        # is unpatched or has not risen (both paths below then run their
        # pre-freeze code verbatim, so the feature ships bit-exact OFF).
        # The row is the gate cable ORed with the ``freeze`` tickbox
        # (``_freeze_gate_row``): unpatched and un-ticked never enters
        # here; the tick alone rises at sample 0 of the block it is seen
        # on and, cleared, releases through the same ramp as a gate fall.
        # The envelope comes FIRST: the clock rows below need to know
        # which samples are held (or releasing), because a retarget waits
        # them out.
        e = None
        ramp_n = max(1, int(round(self._DELAY_FREEZE_RAMP_S * sr)))
        gt = self._freeze_gate_row(
            fz, frames, bool(module.params.get("freeze", False)), state, ramp_n)
        busy = None
        if gt is not None:
            prev = bool(state["fz_prev"])
            env, fz_on, fz_off, fz_env = self._gate_ramp_env(
                gt, prev, int(state["fz_on"]), int(state["fz_off"]),
                float(state["fz_env"]), ramp_n, ramp_n)
            busy = gt | (env > 0.0)

        # --- clock sync: the read time per sample, or None when ``clock``
        # is unpatched and no fade is live (the lines after run verbatim).
        # ``old`` / ``xf`` are the outgoing read and the crossfade weight
        # while a fade is live this block, else None. The door: a module
        # that has never seen a clock, or has faded home from one, calls
        # neither the measurement nor the rows.
        sync = None
        span0 = int(state.get("ck_span", 0))
        if clock is not None or "ck_n" in state:
            events = self._delay_clock_edges(state, clock, frames)
        else:
            events = []
        if (clock is not None or state.get("ck_sync", False)
                or int(state.get("ck_fn", -1)) >= 0):
            sync = self._delay_sync_rows(
                module, frames, state, events, span0, clock is not None,
                time_samp, busy, ramp_n)

        # Per-sample delay in samples, (V, F), clamped into the line.
        old = xf = None
        if sync is None:
            if cv is None:
                dly = np.full((V, frames), time_samp, dtype=np.float64)
            else:
                dly = time_samp + cv_depth_samp * cv.astype(np.float64)
        else:
            # Locked, ``time`` has stepped aside, but ``time_cv`` still
            # adds on top: wow and dub throws on a synced echo. Before the
            # lock the row IS ``time_samp``, so these are the same bits.
            new, old_row, xf = sync
            cvs = None if cv is None else cv_depth_samp * cv.astype(np.float64)
            if cvs is None:
                dly = np.broadcast_to(new, (V, frames)).copy()
            else:
                dly = new + cvs
            if old_row is not None:
                if cvs is None:
                    old = np.broadcast_to(old_row, (V, frames)).copy()
                else:
                    old = old_row + cvs
                np.clip(old, min_s, max_s, out=old)
        np.clip(dly, min_s, max_s, out=dly)

        if gt is not None:
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
        low = float(dly.min())
        if old is not None:
            # A fade is live: the outgoing read, split the same way. The
            # two reads blend as ``old * (1 - c) + new * c`` with ``c`` an
            # integer count over the fade, spelled identically on both
            # paths below, since which one a block takes depends on its
            # size. A locked read with no cv is whole samples (frac 0).
            back_o = np.ceil(old)
            fracs_o = back_o - old
            backs_o = back_o.astype(np.int64)
            low = min(low, float(old.min()))
        # The damping one-pole's coefficients, spelled the way ``lfilter``
        # evaluates them, so the per-sample path below computes the same
        # bits as the fast path's ``lfilter`` (``g * d + (1 - g) * lp``,
        # not ``lp + g * (d - lp)``, which rounds differently). Which path
        # a block takes depends on the block size, so the two must agree.
        c1 = 1.0 - g
        rows = np.arange(V)
        # Which path: the block runs in HOPS no longer than its shortest
        # delay (the reverb's idiom). Within a hop every read lands at
        # least a hop back, so no read depends on a sample written in the
        # same hop and the hop vectorizes. A delay of a block or more is
        # one hop the length of the block -- the old fast path, verbatim.
        # Below ``_DELAY_HOP_FLOOR`` samples a hop costs more than the
        # per-sample loop it replaces, so 2- and 3-sample delays keep that
        # loop. The two compute the same bits (the trap notes above), and
        # so do hops of any length: ``lp`` carried into ``zi`` between
        # hops is what ``lfilter`` carries between samples. With a fade
        # live, the hop is no longer than the nearer of the two reads.
        hop = self._delay_hop(low, frames)
        if hop:
            out = np.empty((V, frames), dtype=np.float64)
            pos = 0
            while pos < frames:
                h = min(hop, frames - pos)
                sl = slice(pos, pos + h)
                absidx = wp + np.arange(h)                   # (h,) absolute
                i0 = absidx[np.newaxis, :] - backs[:, sl]    # (V, h) whole
                frac = fracs[:, sl]
                d = (
                    buf[rows[:, None], i0 % L] * (1.0 - frac)
                    + buf[rows[:, None], (i0 + 1) % L] * frac
                )
                if old is not None:
                    i0o = absidx[np.newaxis, :] - backs_o[:, sl]
                    fo = fracs_o[:, sl]
                    do = (
                        buf[rows[:, None], i0o % L] * (1.0 - fo)
                        + buf[rows[:, None], (i0o + 1) % L] * fo
                    )
                    cs = xf[sl]
                    d = do * (1.0 - cs) + d * cs
                zi = (c1 * lp)[:, np.newaxis]                # (V, 1)
                damped = lfilter([g], [1.0, -c1], d, axis=-1, zi=zi)[0]
                xs = x[:, sl]
                if e is None:
                    buf[rows[:, None], absidx % L] = xs + feedback * damped
                else:
                    # Frozen or ramping: per sample, the fed-back signal
                    # slides from the damped read to the raw read, the loop
                    # gain from ``feedback`` to exactly 1.0 and the input to
                    # silence -- each as ``a * (1 - e) + b * e``, exact at
                    # both ends, so at e == 1 the write is ``buf[wp] = d =
                    # buf[wp - D]`` bit for bit. Convex blends on the way
                    # in: the loop gain never exceeds 1, so the ramp cannot
                    # grow the loop either.
                    eh = e[sl][np.newaxis, :]
                    om = 1.0 - eh
                    fb_sig = damped * om + d * eh
                    gain = feedback * om + eh
                    buf[rows[:, None], absidx % L] = xs * om + gain * fb_sig
                out[:, sl] = xs * (1.0 - mix) + d * mix
                lp = damped[:, -1].copy()
                wp = (wp + h) % L
                pos += h
        else:
            # Per-sample path: a delay of 2 or 3 samples somewhere in the
            # block, so the feedback recirculation is sequential.
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
                if old is not None:
                    i0o = wp - backs_o[:, n]
                    fo = fracs_o[:, n]
                    do = (
                        buf[rows, i0o % L] * (1.0 - fo)
                        + buf[rows, (i0o + 1) % L] * fo
                    )
                    d = do * (1.0 - xf[n]) + d * xf[n]
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

    def _delay_hop(self, low: float, frames: int) -> int:
        """The delay's hop length for a block whose shortest read is
        ``low`` samples back, or 0 for the per-sample loop.

        A read ``low`` back lands before the hop's first write as long as
        the hop is at most ``int(low)`` long (``frac`` > 0 puts the second
        tap one sample nearer, still behind; at ``frac`` == 0 that tap
        weighs 0). A read a block or more back is one block-length hop.
        """
        hop = int(low) if low < frames else frames
        if hop >= self._DELAY_HOP_FLOOR or hop >= frames:
            return hop
        return 0

    def _delay_clock_edges(self, state, clock, frames: int):
        """Measure the delay's ``clock``: the spans that changed this block,
        as ``[(sample in block, new span), ...]``.

        Edges are found the way :meth:`_render_slew` finds them (a lagged
        compare against ``_GATE_HIGH``, the last sample carried in
        ``ck_prev``) and keyed to an absolute sample counter (``ck_n``),
        so every quantity here is an integer and the same at any block
        size. ``span`` is the distance between the last three rising
        edges, TWO ticks (``clock_divider``'s ``avg``, doubled), so a
        tempo that isn't a whole number of samples -- whose intervals
        alternate, 5512 / 5513 at 120 BPM x4 -- or a swung clock, which
        alternates by design, measures one steady span instead of
        retargeting nearly every tick. The deadband (``span // 1024``, at
        least 2 samples) lets the span move only for a real change.

        The break rule: an interval that breaks the two-tick pattern (it
        differs from the one two ticks back by more than the deadband)
        restarts the measurement from that edge, so no span is ever
        measured across a ``reset``, a short stop or a tempo step, and
        the echo holds its time meanwhile. ``ck_rearm`` switches the test
        off until the next span is taken, so a tempo that keeps moving
        still gets measured. This departs from the rack's last-two-edges
        rule on purpose: an echo has no phase to follow, only a length.

        ``clock`` None (unpatched): the measurement is dropped, so it
        starts afresh the next time a clock is patched (three edges to
        lock, ``time`` ruling meanwhile); nothing is returned.
        """
        if clock is None:
            for key in ("ck_n", "ck_prev", "ck_e1", "ck_e2", "ck_e3",
                        "ck_span", "ck_rearm"):
                state.pop(key, None)
            return []
        base = int(state.get("ck_n", 0))
        state["ck_n"] = base + frames
        hi = clock > self._GATE_HIGH
        lag = np.empty(frames, dtype=bool)
        lag[0] = bool(state.get("ck_prev", False))
        lag[1:] = hi[:-1]
        state["ck_prev"] = bool(hi[-1])
        e1 = int(state.get("ck_e1", -1))
        e2 = int(state.get("ck_e2", -1))
        e3 = int(state.get("ck_e3", -1))
        span = int(state.get("ck_span", 0))
        rearm = bool(state.get("ck_rearm", False))
        events = []
        for n in np.flatnonzero(hi & ~lag).tolist():
            now = base + n
            dead = max(2, span // 1024)
            if (span > 0 and e3 >= 0 and not rearm
                    and self._delay_breaks_pattern(now, e1, e2, e3, dead)):
                rearm = True                 # a break: measure afresh from here
                e1, e2, e3 = now, -1, -1
                continue
            if e2 >= 0:
                s = now - e2
                rearm = False
                if span == 0 or abs(s - span) > dead:
                    span = s                 # a real change: retarget here
                    events.append((n, span))
            e3, e2, e1 = e2, e1, now
        state["ck_e1"], state["ck_e2"], state["ck_e3"] = e1, e2, e3
        state["ck_span"] = span
        state["ck_rearm"] = rearm
        return events

    @staticmethod
    def _delay_breaks_pattern(now: int, e1: int, e2: int, e3: int,
                              dead: int) -> bool:
        """The break test: the newest interval (``now - e1``) against the
        one two ticks back (``e2 - e3``), so a swung clock, whose
        neighbouring intervals differ by design, stays on pattern."""
        return abs((now - e1) - (e2 - e3)) > dead

    def _delay_sync_target(self, module, span: int) -> float:
        """The locked echo, in whole samples: ``division`` ticks of a
        measured two-tick ``span``, times ``feel``, floor 2.

        A target past the 2 s line folds down by OCTAVES rather than
        clamping, so it stays on the grid: a 100 BPM quarter clock at
        ``division`` 4 asks for 2.4 s and gets 1.2 s, on the beat, where a
        clamp would give 2.0 s, 3.33 beats, and drift every repeat. A pure
        function of integers and two params, so nothing carries a float.
        """
        from ...modules.delay import DELAY_FEEL_RATIOS

        try:
            division = float(module.params.get("division", 1.0))
        except (TypeError, ValueError):
            division = 1.0
        if not np.isfinite(division):
            division = 1.0
        division = min(max(division, self._MOD_DIV_MIN), self._MOD_DIV_MAX)
        num, den = DELAY_FEEL_RATIOS.get(
            str(module.params.get("feel", "straight")), (1, 1))
        line = self._DELAY_MAX_MS * self.sample_rate / 1000.0
        t = span * division * num / (2 * den)
        while t > line:
            t *= 0.5
        return float(max(2, int(round(t))))

    def _delay_sync_rows(self, module, frames: int, state, events, span0: int,
                         patched: bool, time_samp: float, busy, ramp_n: int):
        """The delay's read time per sample while the clock drives it:
        ``(new, old, c)`` -- the incoming read ``(frames,)``, and while a
        fade is live this block the outgoing read and the crossfade
        weight (else None, None). None when ``clock`` is unpatched and no
        fade is live: the caller then runs its pre-sync lines verbatim.

        Before the lock (patched, fewer than three edges) the row is
        ``time_samp`` itself, the knob jumping as it always has, so the
        render is bit-exact with unpatched. A change of target -- the
        first lock, a new span, a ``division`` or ``feel`` move, the
        unpatch (home to ``time``) -- crossfades the two reads over
        ``ramp_n`` samples (the freeze's 10 ms), weighted ``k / ramp_n``
        from an integer count, starting on the edge sample that measured
        it or at the block head. One fade runs at a time; a new target
        waits its turn, so a dragged ``division`` moves in 10 ms steps.
        ``busy`` (the freeze held or releasing) makes a retarget wait until
        it has run out; a fade already in flight finishes.

        State: ``ck_cur`` (the read time when no fade is live),
        ``ck_sync`` (that time came from the clock), and the fade:
        ``ck_fn`` (samples into it, -1 = none), ``ck_from`` / ``ck_to``
        and ``ck_tosync``.
        """
        synced = bool(state.get("ck_sync", False))
        fn = int(state.get("ck_fn", -1))
        if not patched and not synced and fn < 0:
            return None
        cur = state.get("ck_cur")
        cur = time_samp if cur is None else float(cur)
        src_t = float(state.get("ck_from", cur))
        dst_t = float(state.get("ck_to", cur))
        tosync = bool(state.get("ck_tosync", False))
        new = np.empty(frames, dtype=np.float64)
        old = np.empty(frames, dtype=np.float64)
        c = np.ones(frames, dtype=np.float64)
        faded = False
        n, ev, span = 0, 0, span0
        while n < frames:
            while ev < len(events) and events[ev][0] <= n:
                span = events[ev][1]
                ev += 1
            locked = patched and span > 0
            nxt = events[ev][0] if ev < len(events) else frames
            if fn < 0:
                want = self._delay_sync_target(module, span) if locked else time_samp
                if not locked and not synced:
                    cur = want                   # free: the knob jumps, as today
                elif want == cur:
                    synced = locked
                elif busy is not None and busy[n]:
                    # held or releasing: the retarget waits for the first
                    # free sample (this segment keeps the current read)
                    free = np.flatnonzero(~busy[n:nxt])
                    if len(free):
                        nxt = n + int(free[0])
                else:
                    src_t, dst_t, tosync, fn = cur, want, locked, 0
            if fn >= 0:
                end = min(nxt, n + (ramp_n - fn))
                new[n:end] = dst_t
                old[n:end] = src_t
                c[n:end] = self._delay_xfade_weights(fn, end - n, ramp_n)
                faded = True
                fn += end - n
                if fn >= ramp_n:
                    fn = -1
                    cur = dst_t
                    synced = tosync
                n = end
            else:
                new[n:nxt] = cur
                old[n:nxt] = cur
                n = nxt
        state["ck_cur"] = cur
        state["ck_sync"] = synced
        state["ck_fn"] = fn
        state["ck_from"] = src_t
        state["ck_to"] = dst_t
        state["ck_tosync"] = tosync
        if faded:
            return new, old, c
        return new, None, None

    @staticmethod
    def _delay_xfade_weights(fn: int, m: int, ramp_n: int):
        """The next ``m`` crossfade weights of a fade ``fn`` samples in:
        ``k / ramp_n`` for k = fn + 1 .. fn + m, each from an exact
        integer, so a weight is the same number at any block size (a
        weight carried as a float, ``c += 1 / ramp_n``, would round
        differently cut into 64s than into 512s). The last is exactly 1."""
        return np.arange(fn + 1, fn + 1 + m, dtype=np.float64) / ramp_n

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
