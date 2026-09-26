"""Dynamics renderers: compressor, limiter, noise gate, transient shaper.

Moved verbatim out of ``numpy_backend.py`` (2026-09-26) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". The two static key-align helpers reach ``NumpyBackend._voice_mean``
through a lazy import, placed in the one fallback branch that needs it
(``numpy_backend`` imports this module, so a top-level import would be
circular).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter


class DynamicsRenderers:
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
            threshold += depth * self._finite_mean(tcv)

        if src.ndim == 2:
            V = src.shape[0]
            key = self._compressor_align_key(key, src, V)
            return self._render_compressor_core(module, frames, src, key, threshold)

        # Mono. A 2D key collapses to the summed mix (you key off the whole
        # signal, not one voice); an absent key normals to ``in``.
        if key is None:
            key = src
        elif key.ndim == 2:
            key = self._voice_sum(key)
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
        from ..numpy_backend import NumpyBackend  # lazy: it imports this module

        return np.broadcast_to(NumpyBackend._voice_mean(key), (V, key.shape[1]))

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
            key = self._voice_sum(key)
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
        from ..numpy_backend import NumpyBackend  # lazy: it imports this module

        return np.broadcast_to(NumpyBackend._voice_mean(key), (V, key.shape[1]))

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
