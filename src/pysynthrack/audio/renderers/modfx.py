"""Modulation-effect renderers: chorus, rotary (Leslie), flanger, phaser,
and the shared clock-sync helpers (``_mod_clock_sync``, ``_mod_free_phase``).

Moved verbatim out of ``numpy_backend.py`` (2026-09-26) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". ``autopan`` (still in the backend) reaches the clock-sync
helpers through ``self``.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter


class ModFXRenderers:
    def _render_chorus(self, module, frames: int, buffers, patch):
        """Detuned multi-voice stereo chorus: mono in -> out_l / out_r.

        A bank of short delay lines is swept by an internal sine LFO (one
        evenly-spaced phase slice per voice) and read back with linear
        interpolation; the moving delay detunes each copy, and the copies
        are panned across the stereo field so the two channels decorrelate
        (width). There is no feedback -- a fed-back chorus is a flanger --
        so no read this block depends on a sample written this block, the
        whole render vectorizes, and it is exactly block-size independent:
        bit for bit at 64, 128, 512 or 1000 over four seconds. That needs
        two deliberate choices, each guarding a place where a partition
        would otherwise change a rounding -- the read splits the delay
        into whole samples and a fraction instead of forming ``absidx -
        delay`` (**a ring index rounds at its own magnitude**), and the
        sweep counts samples since the last rate change rather than
        carrying a float phase (re-anchoring on a change, so a rate move
        stays continuous). ``rate_cv`` is read PER SAMPLE (since
        2026-09-24; it was a block mean, so a modulated rate moved in
        block-sized steps and re-anchored every block): the knob's sweep
        above is kept exactly as it was, and what the CV adds -- the
        instantaneous rate minus the knob's -- is integrated on an
        integer phase grid (:meth:`_chorus_rate_cv_phase`), which is
        associative, so a modulated sweep is bit-exact across block
        sizes too, and a zero CV adds exactly nothing.
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

        # rate_cv: 1 V/oct on the LFO rate, read PER SAMPLE (see the sweep
        # below). ``rate`` here stays the KNOB's rate -- the CV's share of
        # the sweep is integrated separately so an unpatched jack, or a
        # zero CV, leaves the knob's sweep bit-for-bit as it was.
        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size == 0:
            rate_cv = None
        rate_knob = rate
        rate = min(max(rate, 0.01), 20.0)

        max_ms = self._CHORUS_MAX_MS
        L = int(max_ms * sr / 1000.0) + frames + 4

        state = self._state.setdefault(module.id, {})
        if "buf" not in state or state["buf"].shape != (L,):
            state.clear()
            state["buf"] = np.zeros(L, dtype=np.float64)
            state["write_idx"] = 0
            # The LFO's phase is an ANCHOR plus an integer sample count
            # since the last rate change, not a float carried block by
            # block (see below). ``inc`` starts impossible so the first
            # block anchors.
            state["phase"] = 0.0
            state["ph_n"] = 0
            state["inc"] = -1.0

        buf = state["buf"]
        wp = int(state["write_idx"])

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
        # The sweep is keyed to an integer sample COUNT since the last rate
        # change, never to a phase carried block by block: ``ph + frames *
        # inc`` rounds once per block, so the same stream cut into 64s and
        # into 512s accumulates a different phase and the two renders drift
        # apart within a second -- and one ulp on the delay flips a float32
        # tie at the odd sample. ``k * inc`` from an exact integer rounds
        # once, the same way, at every block size. A rate change (the
        # ``rate`` knob, or ``rate_cv``) re-anchors instead: the phase
        # reached so far is frozen into the anchor and the count restarts,
        # so the sweep stays continuous -- a jump would click -- and each
        # constant-rate run is exact. ``rate_cv`` never re-anchors: its
        # share is added on top, from its own integer accumulator.
        if inc != state["inc"]:
            state["phase"] = float(
                (state["phase"] + state["ph_n"] * state["inc"]) % 1.0)
            state["ph_n"] = 0
            state["inc"] = inc
        phase0 = float(state["phase"])
        k = state["ph_n"] + n                                          # (F,)
        ph = phase0 + offs[:, None] + k[None, :] * inc                 # (V, F)
        if rate_cv is not None:
            # + exactly 0.0 per sample for a zero CV: bit-exact unpatched
            ph = ph + self._chorus_rate_cv_phase(
                state, rate_cv, rate_knob, inc, cv_depth, frames)[None, :]
        ph %= 1.0
        lfo = np.sin(2.0 * np.pi * ph)                                # (V, F)

        delay = base_samp[:, None] + sweep_samp * lfo                 # (V, F)
        np.clip(delay, 2.0, float(L - 2), out=delay)

        # Write the whole block, then read the taps. With no feedback a tap
        # that lands inside this block just reads an input sample already
        # written -- correct, and identical at any block size.
        absidx = wp + np.arange(frames)
        buf[absidx % L] = x
        # THE TRAP: a ring index rounds at its own magnitude. ``absidx -
        # delay`` looks exact and is not -- the ring is ``max_ms + frames``
        # long, so it wraps at a different absolute sample for every block
        # size, and the fraction that falls out of the subtraction is a
        # float64 ulp apart. So never form ``index - delay``: split the
        # delay into WHOLE SAMPLES and a FRACTION, both functions of the
        # (small) delay alone and so identical at any block size.
        back = np.ceil(delay)                                        # (V, F)
        frac = back - delay                # forward weight, in [0, 1)
        i0 = absidx[None, :] - back.astype(np.int64)                 # (V, F)
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
        state["ph_n"] += frames

        dry = (1.0 - mix) * x
        out_l = (dry + mix * wet_l).astype(np.float32)
        out_r = (dry + mix * wet_r).astype(np.float32)
        return {"out_l": out_l, "out_r": out_r}

    # The rate_cv integrator's grid: 2**48 steps per LFO cycle. The
    # largest increment (20 Hz at 8 kHz) is ~2**40 steps, so a block of a
    # million samples still sums far inside int64, and one step is
    # 3.6e-15 of a cycle -- the grid's rounding is ~1e-12 cycles after
    # minutes of modulation, far under anything a delay line can show.
    _CHORUS_PH_BITS = 48
    _CHORUS_MAX_MS = 40.0  # longest chorus delay (ms); sizes the ring

    def _chorus_rate_cv_phase(self, state, rate_cv, rate_knob: float,
                              inc: float, cv_depth: float, frames: int):
        """The chorus sweep's rate_cv share, per sample, in cycles [0, 1).

        The instantaneous rate is ``knob * 2 ** (cv_depth * cv[n])``,
        clamped to the knob's 0.01..20 Hz, per sample. The knob's own
        sweep (``inc`` per sample) is already counted by the caller's
        absolute-sample phase, so what is integrated here is only the
        DEVIATION ``rate[n] / sr - inc`` -- exactly 0.0 at a zero CV,
        which is what keeps a patched-but-silent jack bit-identical to an
        unpatched one.

        THE TRAP this dodges: a float running sum is not partition-
        independent once it is wrapped. ``carry + cumsum(inc)`` rounds
        differently per partition (``cumsum([carry, *inc])[1:]`` would
        not), but an unwrapped float phase loses a bit of precision every
        time it doubles, and wrapping it (``% 1.0``) at a block boundary
        changes every later rounding -- and the boundaries are the
        partition. So the deviation is rounded ONCE, per sample and on
        its own, to an integer grid of 2**48 steps per cycle, and summed
        in int64: integer addition is exact and associative, so the sum
        at a sample is the same number however the stream was cut, and
        the wrap (``& (2**48 - 1)``, two's complement, so a negative
        deviation wraps too) is exact as well. Sample ``n`` sees the sum
        of the deviations BEFORE it, the same convention as the knob's
        ``k * inc``.
        """
        bits = self._CHORUS_PH_BITS
        mask = (1 << bits) - 1
        cv = np.asarray(rate_cv, dtype=np.float64)
        # _pow2_clipped scrubs a non-finite exponent to 0 (no modulation)
        # and rails a huge one; the clamp matches the knob's range.
        r = np.clip(rate_knob * self._pow2_clipped(cv_depth * cv), 0.01, 20.0)
        q = np.rint((r / float(self.sample_rate) - inc) * float(1 << bits))
        run = np.cumsum(q.astype(np.int64))
        acc0 = int(state.get("rc_acc", 0))
        acc = np.empty(frames, dtype=np.int64)
        acc[0] = acc0
        acc[1:] = acc0 + run[:-1]
        state["rc_acc"] = int((acc0 + int(run[-1])) & mask)
        return (acc & mask).astype(np.float64) * (1.0 / float(1 << bits))

    # ----- Rotary (Leslie) rendering ---------------------------------------

    # Rotor geometry and motor character (see modules/rotary.py). Radii are
    # the mouth's off-axis distance in metres; c is the speed of sound; the
    # Doppler delay swing per rotor is r/c seconds at depth 1. AM depths
    # are how far the level drops when the mouth faces away (the horn
    # beams, the drum baffle less). Ramp times are seconds at ramp = 1:
    # the horn is light, the drum is heavy and lags -- which IS the sound.
    _ROT_C = 343.0
    _ROT_HORN_R = 0.19
    _ROT_DRUM_R = 0.14
    _ROT_HORN_AM = 0.80
    _ROT_DRUM_AM = 0.45
    _ROT_DRUM_RATIO = 0.85          # drum rate / horn rate, both settings
    _ROT_HORN_UP, _ROT_HORN_DOWN = 1.0, 1.5
    _ROT_DRUM_UP, _ROT_DRUM_DOWN = 4.5, 6.0
    _ROT_BASE_MS = 2.0              # centre delay above the Doppler swing
    _ROT_MIN_BLOCK = 4096           # ring headroom: a short block never resizes it
    _ROT_WRAP = 1 << 16             # rotor sums lose whole turns every 2**16 samples

    def _render_rotary(self, module, frames: int, buffers, patch) -> dict:
        """Leslie rotary cabinet: mono in -> out_l / out_r (+ mono out).

        Signal path: LR4 crossover (the crossover module's coefficients,
        zf-carried) -> horn band and drum band -> each into its own delay
        ring, read twice (one tap per mic) at a Doppler-modulated
        fractional delay and scaled by a cos-of-angle amplitude -> summed
        per mic. Every rotor has an angle that integrates a rate; the
        rate approaches its target (slow / fast / 0) through a one-pole
        with separate up and down time constants, run as an lfilter so
        the ramp is the exact per-sample recurrence and block-size
        independent. The horn turns positive, the drum negative (they
        counter-rotate on the real cabinet).

        Per mic ``c`` at angle ``mu_c`` and rotor angle ``th``:
          ``cosang = cos(th - mu_c)``
          ``gain   = 1 - am * depth * (1 - cosang) / 2``     (1 facing, 1-am away)
          ``delay  = base - (r/c) * depth * cosang``  samples (nearer = shorter)
        The delay's derivative is the Doppler: ``(r/c) * depth * 2*pi*f *
        sin`` -- the horn at 6.7 Hz and depth 1 swings about +/-2.3%,
        ~40 cents, which is what a 122 does.

        No feedback anywhere, so every read this block lands on a sample
        already written, and the render is block-size independent BIT
        FOR BIT: 64 / 128 / 512 / 1000, or any irregular partition, over
        four seconds with both mics, every band and the speed switching
        (2026-09-24; it used to be exact only "up to the angle wrap's
        rounding", which was a float32 ulp at a handful of samples per
        four seconds). Three choices hold that:

        * the ring is read as whole samples back plus a fraction
          (``back = ceil(delay)``, ``frac = back - delay``), functions of
          the small delay alone -- never ``absidx - delay``, which rounds
          at the index's magnitude, and the index wrapped at a different
          sample for every block size (the tape's 2026-09-22 fix);
        * each rotor's angle is ONE sequential running sum of its rate
          carried across blocks, with whole turns taken off only at
          absolute multiples of ``_ROT_WRAP`` -- not a per-block
          ``th0 + cumsum(f)`` re-wrapped at every block end;
        * the ring is indexed by an absolute sample clock and only ever
          grows (``_rotary_grow`` keeps its history), where it used to
          be ``hist + frames`` long and was CLEARED -- rotors and all --
          whenever a block of a new length arrived.

        The one block-quantised thing left is by design: the ``fast``
        gate is read as the block's majority level, so its switch lands
        on a block boundary. The dry side of ``mix`` is the raw input
        delayed by the rotors' centre delay so a blend thickens instead
        of combing. A ``(V, F)`` input is summed -- a cabinet is one
        physical thing.
        """
        from ...modules.rotary import ROTARY_SPEEDS

        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z, "out": z}
        fast_gate = self._input_buffer(patch, buffers, module.id, "fast")

        sr = float(self.sample_rate)

        def _f(name, lo, hi, default):
            try:
                return float(np.clip(float(module.params.get(name, default)), lo, hi))
            except (TypeError, ValueError):
                return default

        speed = str(module.params.get("speed", "slow"))
        if speed not in ROTARY_SPEEDS:
            speed = "slow"
        if fast_gate is not None and fast_gate.size > 0:
            # A level, read as the block's majority: a held gate is fast,
            # a low one slow, and the combo has no say while it is cabled.
            speed = "fast" if self._finite_mean(fast_gate) > 0.5 else "slow"
        slow_rate = _f("slow_rate", 0.1, 3.0, 0.7)
        fast_rate = _f("fast_rate", 2.0, 12.0, 6.7)
        ramp = _f("ramp", 0.25, 4.0, 1.0)
        depth = _f("depth", 0.0, 1.0, 0.7)
        spread = _f("spread", 0.0, 1.0, 0.7)
        balance = _f("balance", -1.0, 1.0, 0.0)
        xover = _f("crossover", 100.0, 4000.0, 800.0)
        mix = _f("mix", 0.0, 1.0, 1.0)

        # Delay rings: centre + full swing, plus the block and a margin.
        horn_swing = self._ROT_HORN_R / self._ROT_C * sr
        drum_swing = self._ROT_DRUM_R / self._ROT_C * sr
        # Centre delay: an INTEGER number of samples (>= 2 ms + the horn's
        # full swing), so the delay-matched dry tap is an exact read.
        base = float(int(np.ceil(self._ROT_BASE_MS * 1e-3 * sr + horn_swing)))
        # How far back a read can reach: the centre delay, the full swing
        # and the interpolator's second tap, with a margin.
        hist = int(base + horn_swing) + 8

        state = self._state.setdefault(module.id, {})
        if state.get("hist") != hist:
            state.clear()
            state["hist"] = hist
            # Sized for a generous block up front, so a short block never
            # reallocates -- the ring used to be ``hist + frames`` long and
            # CLEARED (rotors, history and all) whenever a block of a new
            # length arrived. It only ever grows, and keeps its history
            # when it does (``_rotary_grow``).
            L = hist + max(frames, self._ROT_MIN_BLOCK)
            state["L"] = L
            state["horn_buf"] = np.zeros(L, dtype=np.float64)
            state["drum_buf"] = np.zeros(L, dtype=np.float64)
            state["dry_buf"] = np.zeros(L, dtype=np.float64)
            # Absolute samples written: the ring's clock. Slots are
            # ``n % L``; reads are integer offsets back from it.
            state["n"] = 0
            # Each rotor's angle is a running sum of its rate in Hz x
            # samples: ``th = 2*pi*acc/sr`` (see ``_spin``).
            state["horn_acc"] = 0.0
            state["drum_acc"] = 0.0
            state["horn_f"] = 0.0
            state["drum_f"] = 0.0
            state["xo_zi"] = None
        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e, "out": e}
        if state["L"] < hist + frames:
            self._rotary_grow(state, hist + frames)
        L = int(state["L"])

        x = src.astype(np.float64)

        # --- crossover: LR4 = two cascaded Butterworth biquads per band ---
        lp_b0, lp_b1, lp_b2, hp_b0, hp_b1, hp_b2, a1n, a2n = self._crossover_coeffs(xover)
        a = np.array([1.0, a1n, a2n])
        lp_b = np.array([lp_b0, lp_b1, lp_b2])
        hp_b = np.array([hp_b0, hp_b1, hp_b2])
        zi = state["xo_zi"]
        if zi is None:
            zi = [np.zeros(2) for _ in range(4)]
        lp1, zi[0] = lfilter(lp_b, a, x, zi=zi[0])
        low, zi[1] = lfilter(lp_b, a, lp1, zi=zi[1])
        hp1, zi[2] = lfilter(hp_b, a, x, zi=zi[2])
        high, zi[3] = lfilter(hp_b, a, hp1, zi=zi[3])
        state["xo_zi"] = zi

        # --- rotors: rate ramps toward its target, angle integrates it ---
        if speed == "fast":
            horn_t, drum_t = fast_rate, fast_rate * self._ROT_DRUM_RATIO
        elif speed == "slow":
            horn_t, drum_t = slow_rate, slow_rate * self._ROT_DRUM_RATIO
        else:
            horn_t = drum_t = 0.0

        n0 = int(state["n"])
        wrap = self._ROT_WRAP

        def _spin(rotor, target, t_up, t_down, sign):
            f0 = float(state[rotor + "_f"])
            # Re-read at every block start, and that is still exact: the
            # one-pole's step ``k * (target - y)`` rounds away once it is
            # under half an ulp of ``y``, so the rate stalls ~ulp/(2k)
            # SHORT of its target and never reaches or crosses it
            # (measured: 300 random rates / targets / ramps / sample
            # rates, 40 time constants each, none did) -- the comparison
            # gives the same answer at every sample of a glide.
            tau = (t_up if target > f0 else t_down) * ramp
            k = 1.0 - float(np.exp(-1.0 / (tau * sr)))
            # y[n] = y[n-1] + k*(target - y[n-1])  ==  one-pole toward target
            f, _zf = lfilter([k], [1.0, -(1.0 - k)], np.full(frames, target), zi=[(1.0 - k) * f0])
            # The angle is a sequential running sum of the rate carried
            # across blocks -- ``cumsum([carry, f])`` adds sample by
            # sample, the same additions at any partition -- never a
            # per-block ``th0 + cumsum(f)`` re-wrapped at the block end,
            # which rounds differently for every block size. Whole turns
            # (``sr`` Hz x samples) come off only at absolute multiples
            # of ``_ROT_WRAP``, where the subtraction is exact
            # (Sterbenz), so the sum stays small at the same samples
            # whatever the blocking.
            acc = float(state[rotor + "_acc"])
            tot = np.empty(frames, dtype=np.float64)
            s0 = 0
            w = (-n0) % wrap
            while True:
                e = min(w, frames)
                if e > s0:
                    run = np.cumsum(np.concatenate(([acc], f[s0:e])))
                    tot[s0:e] = run[1:]
                    acc = float(run[-1])
                if w >= frames:
                    break
                acc -= float(np.floor(acc / sr)) * sr
                s0, w = w, w + wrap
            state[rotor + "_acc"] = acc
            state[rotor + "_f"] = float(f[-1])
            return sign * 2.0 * np.pi * tot / sr

        horn_th = _spin("horn", horn_t, self._ROT_HORN_UP, self._ROT_HORN_DOWN, +1.0)
        drum_th = _spin("drum", drum_t, self._ROT_DRUM_UP, self._ROT_DRUM_DOWN, -1.0)

        # --- write the bands, then read each rotor once per mic --------
        absidx = n0 + np.arange(frames, dtype=np.int64)
        slots = absidx % L
        state["horn_buf"][slots] = high
        state["drum_buf"][slots] = low
        state["dry_buf"][slots] = x
        state["n"] = n0 + frames

        mu = spread * 0.5 * np.pi           # mic half-angle
        mics = (+mu, -mu)                   # (left, right)

        def _tap(buf, th, swing, am):
            outs = []
            for m in mics:
                cosang = np.cos(th - m)
                gain = 1.0 - am * depth * (1.0 - cosang) * 0.5
                delay = base - swing * depth * cosang
                # Whole samples back and a fraction, both functions of the
                # small delay alone -- never ``absidx - delay``, which
                # rounds at the INDEX's magnitude (a ring index rounds at
                # its own magnitude) and so lands a float64 ulp apart for
                # the same sample under a different block partition.
                # ``back - delay`` is exact by Sterbenz: delay >= base -
                # swing >= 2 ms of samples, so back <= 2 * delay.
                back = np.ceil(delay)
                frac = back - delay
                i0 = absidx - back.astype(np.int64)
                tap = buf[i0 % L] * (1.0 - frac) + buf[(i0 + 1) % L] * frac
                outs.append(gain * tap)
            return outs

        horn_l, horn_r = _tap(state["horn_buf"], horn_th, horn_swing, self._ROT_HORN_AM)
        drum_l, drum_r = _tap(state["drum_buf"], drum_th, drum_swing, self._ROT_DRUM_AM)

        horn_g = min(1.0, 1.0 + balance)
        drum_g = min(1.0, 1.0 - balance)
        wet_l = horn_g * horn_l + drum_g * drum_l
        wet_r = horn_g * horn_r + drum_g * drum_r

        if mix < 1.0:
            # Dry, delay-matched to the rotors' (integer) centre delay.
            dry = state["dry_buf"][(absidx - int(base)) % L]
            wet_l = (1.0 - mix) * dry + mix * wet_l
            wet_r = (1.0 - mix) * dry + mix * wet_r

        out_l = wet_l.astype(np.float32)
        out_r = wet_r.astype(np.float32)
        return {"out_l": out_l, "out_r": out_r, "out": 0.5 * (out_l + out_r)}

    @staticmethod
    def _rotary_grow(state, L_new: int) -> None:
        """Lengthen the rotary's rings to ``L_new``, keeping their history.

        Slots are absolute sample index mod ``L``, so a new length moves
        every sample's slot: the last ``hist`` samples written (all a
        read can reach) are re-homed at their new slots. Only a block
        longer than any before gets here -- the app never changes block
        size mid-run -- and the render carries on as if nothing happened.
        """
        L_old = int(state["L"])
        n = int(state["n"])
        idx = np.arange(max(0, n - int(state["hist"])), n, dtype=np.int64)
        for key in ("horn_buf", "drum_buf", "dry_buf"):
            new = np.zeros(L_new, dtype=np.float64)
            new[idx % L_new] = state[key][idx % L_old]
            state[key] = new
        state["L"] = L_new

    # ----- Modulation-effect clock sync ------------------------------------

    # ``division`` rail for the modulation effects' ``clock`` jack: how many
    # clock ticks one LFO sweep may be stretched over (a sixteenth of a
    # tick to sixteen bars of four).
    _MOD_DIV_MIN = 0.25
    _MOD_DIV_MAX = 64.0
    # State keys :meth:`_mod_clock_sync` owns. A module that re-inits its
    # DSP state (the phaser on a ``stages`` change, the flanger on a
    # ``through_zero`` flip) carries these across the clear, so flipping a
    # knob mid-stream doesn't throw away a period it has already measured.
    _MOD_CLOCK_KEYS = ("samples", "prev_clock", "last_edge", "interval",
                       "anchor", "period")

    def _mod_free_phase(self, state, frames: int, rate: float):
        """The free-running LFO phase of a modulation sweep, ``(frames,)``.

        Keyed to an integer sample COUNT since the last rate change, never
        to a phase carried block by block: ``ph + frames * inc`` rounds
        once per block, so the same stream cut into 64s and into 512s
        accumulated a different phase and the two renders drifted apart
        (~1e-8 over a second on the flanger, audible to nobody, but not the
        same bits). ``k * inc`` from an exact integer ``k`` rounds once, the
        same way, at every block size -- the chorus's fix (3f161fa). A rate
        change (the knob, or ``rate_cv``) re-anchors instead: the phase
        reached so far is frozen into ``phase`` and the count restarts, so
        the sweep stays continuous and each constant-rate run is exact.
        ``rate_cv`` is a per-BLOCK mean by design, so a MOVING cv
        re-anchors every block and is the one input that does depend on
        the block size; a steady one reads the same float64 at every block
        size (``_finite_mean``), so it never re-anchors.

        State: ``phase`` (the anchor), ``ph_n`` (samples since it) and
        ``inc`` (the increment in force; start it impossible, -1.0, so the
        first block anchors). The caller advances ``ph_n`` by ``frames``
        after the block -- or, when :meth:`_mod_clock_sync` drove the
        block, re-anchors at the locked sweep's end phase.
        """
        inc = rate / self.sample_rate
        if inc != state["inc"]:
            state["phase"] = float(
                (state["phase"] + state["ph_n"] * state["inc"]) % 1.0)
            state["ph_n"] = 0
            state["inc"] = inc
        k = state["ph_n"] + np.arange(frames, dtype=np.float64)
        return (state["phase"] + k * inc) % 1.0

    def _mod_clock_sync(self, module, frames: int, buffers, patch, state,
                        division: float, free):
        """Absolute-sample LFO phase for a clock-synced modulation sweep.

        Returns ``None`` while ``clock`` is unpatched, or patched but still
        short of the two rising edges a period needs -- the caller then
        runs its free-running phase line (``free``, the ``(frames,)``
        array :meth:`_mod_free_phase` gave it). Otherwise returns
        ``(phase, end_phase)``: a ``(frames,)`` array of LFO phase, one
        value per sample, and the phase just past the block.

        Edges are found the way :meth:`_render_slew` finds them (a lagged
        compare against ``_GATE_HIGH``, the previous block's last sample
        carried in ``prev_clock``) and keyed to a running ABSOLUTE sample
        counter, so the measured ``interval`` is the same integer whatever
        the block size. What is new here is that the *phase* is keyed that
        way too:

            phase[n] = ((n - anchor) / period) % 1.0,
            period = interval * division samples

        Nothing accumulates, so the phase at sample n is the same number
        at any block size -- the integer-tick lesson. (A float phase
        accumulator is not, which is why the free-running line is keyed
        to a sample count too -- :meth:`_mod_free_phase`; a rate re-read
        once a block would have drifted ~5e-2, which is audible.) When
        the measured period changes, the anchor is re-derived at THAT
        EDGE's own sample from the phase there, so the sweep keeps its
        place rather than jumping. The block where the lock engages is
        the one block that is part free-running (before the edge, read
        off ``free``) and part locked.

        While the lock holds, ``rate`` and ``rate_cv`` step aside entirely
        -- the sweep length is the cable's. ``free`` is passed in only to
        place the free-running stretch before the lock engages.
        """
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        base = int(state.get("samples", 0))
        state["samples"] = base + frames
        if clock is None:
            state["prev_clock"] = False
            state["last_edge"] = -1
            state["interval"] = 0
            state["anchor"] = -1
            state["period"] = 0.0
            return None

        hi = clock > self._GATE_HIGH
        lag = np.empty(frames, dtype=bool)
        lag[0] = bool(state.get("prev_clock", False))
        lag[1:] = hi[:-1]
        state["prev_clock"] = bool(hi[-1])

        last_edge = int(state.get("last_edge", -1))
        interval = int(state.get("interval", 0))
        anchor = int(state.get("anchor", -1))
        period = float(state.get("period", 0.0))

        # Split the block at every edge that CHANGES the period. Each
        # stretch carries the (anchor, period) in force across it; a
        # period of 0 means "not locked yet, run free".
        segs = []
        start = 0
        for n in np.flatnonzero(hi & ~lag).tolist():
            now = base + n
            new_interval = now - last_edge if last_edge >= 0 else interval
            last_edge = now
            if new_interval <= 0 or new_interval == interval:
                continue
            if n > start:
                segs.append((start, n, anchor, period))
            if period > 0.0:
                at = ((now - anchor) / period) % 1.0
            else:
                at = float(free[n])
            interval = new_interval
            period = interval * division
            anchor = now - int(round(at * period))
            start = n
        segs.append((start, frames, anchor, period))

        state["last_edge"] = last_edge
        state["interval"] = interval
        state["anchor"] = anchor
        state["period"] = period
        if period <= 0.0:
            return None

        ph = np.empty(frames, dtype=np.float64)
        for (lo, hi_i, an, pe) in segs:
            if hi_i <= lo:
                continue
            if pe > 0.0:
                idx = np.arange(lo, hi_i, dtype=np.float64)
                ph[lo:hi_i] = ((base + idx - an) / pe) % 1.0
            else:
                ph[lo:hi_i] = free[lo:hi_i]
        return ph, float(((base + frames - anchor) / period) % 1.0)

    # ----- Flanger rendering ----------------------------------------------

    # Longest delay the flanger line can address, in milliseconds. The comb
    # is a *short* modulated delay, so this ring is tiny; it sizes the
    # buffer and caps the swept delay. The deepest standard sweep is
    # ``manual`` 10 + ``_FLANGER_SWEEP_MS`` 4 = 14 ms, so 16 leaves the cap
    # a margin it never reaches. (It was 12 ms + ``frames``: the cap moved
    # with the block size, and at 64 it bit into the deepest sweeps that
    # 512 let through -- a render that depended on the block size.)
    _FLANGER_MAX_MS = 16.0
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

        Love pass (2026-09-22), all three OFF at their defaults so the
        shipped render is bit-identical: a ``clock`` jack that locks the
        sweep to the cable (one comb sweep every ``division`` ticks, via
        :meth:`_mod_clock_sync`); ``spread``, which is the L/R LFO phase
        offset the quarter-cycle quadrature used to hard-code (0.5 is that
        quadrature, 0 collapses the pair to one comb, 1 counter-sweeps);
        and ``manual_cv``, a per-sample octave jack on the centre delay so
        an envelope can sweep the comb with ``depth`` at 0 -- in
        through-zero mode it moves the reference tap too, so the crossing
        itself travels.
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
        spread = float(module.params.get("spread", 0.5))
        division = float(module.params.get("division", 4.0))
        manual_depth = float(module.params.get("manual_depth", 1.0))
        depth = min(max(depth, 0.0), 1.0)
        mix = min(max(mix, 0.0), 1.0)
        feedback = min(max(feedback, -0.95), 0.95)   # bipolar, below runaway
        manual_ms = min(max(manual_ms, 0.1), 10.0)
        polarity = min(max(polarity, -1.0), 1.0)
        spread = min(max(spread, 0.0), 1.0)
        division = min(max(division, self._MOD_DIV_MIN), self._MOD_DIV_MAX)

        # Through-zero sweeps the moving tap out to ~2x the centre delay, so
        # its line is longer. The recirculation is per-sample, so the ring
        # only has to hold the longest delay -- NOT ``+ frames``: a length
        # (and so a clamp) that moved with the block size made the deepest
        # sweeps block-size dependent, and a block-size change mid-stream
        # re-initialised the line.
        max_ms = self._FLANGER_TZ_MAX_MS if tz_on else self._FLANGER_MAX_MS
        L = int(max_ms * sr / 1000.0) + 4

        state = self._state.setdefault(module.id, {})
        if (
            "buf" not in state
            or state["buf"].shape != (2, L)
            or state.get("tz") != tz_on
        ):
            keep = {k: state[k] for k in self._MOD_CLOCK_KEYS if k in state}
            state.clear()
            state.update(keep)
            state["buf"] = np.zeros((2, L), dtype=np.float64)
            state["write_idx"] = 0
            # LFO: anchor phase + integer count since the last rate change
            # (``_mod_free_phase``); ``inc`` -1 makes the first block anchor.
            state["phase"] = 0.0
            state["ph_n"] = 0
            state["inc"] = -1.0
            state["tz"] = tz_on

        buf = state["buf"]
        wp = int(state["write_idx"])

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        # rate_cv: 1 V/oct on the LFO rate, block-mean -- a sub-audio LFO,
        # so one rate per block is the right cost/quality trade-off (the
        # same cadence the chorus and LFO modules use for their rate_cv).
        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * self._pow2_clipped(cv_depth * self._finite_mean(rate_cv))
        rate = min(max(rate, 0.01), 20.0)

        x = src.astype(np.float64)                        # (F,)

        # manual_cv: the centre delay as a jack -- 1 V/oct x ``manual_depth``
        # on the millisecond knob, read PER SAMPLE so an envelope or a pedal
        # can sweep the comb with the LFO's ``depth`` at 0. Unpatched leaves
        # the centre the scalar it always was, so the shipped render is
        # untouched. A voice source is summed to mono, like ``in``.
        manual_cv = self._input_buffer(patch, buffers, module.id, "manual_cv")
        if manual_cv is not None and manual_cv.size > 0:
            man = np.clip(
                manual_ms * self._pow2_clipped(
                    manual_depth * manual_cv.astype(np.float64)
                ),
                0.1, 10.0,
            )                                             # (F,) milliseconds
        else:
            man = None

        # One sine LFO; ``spread`` sets how far apart the L and R phases
        # run (0 = together, 0.5 = the shipped quarter-cycle quadrature,
        # 1 = a half cycle apart), so the two combs sweep out of step.
        # ``clock`` patched and locked replaces the free-running phase line
        # with an absolute-sample one (one sweep per ``division`` ticks).
        # Free-running, the phase is an integer sample count since the last
        # rate change (``_mod_free_phase``), so it is the same bits at any
        # block size.
        offs = np.array([0.0, 0.5 * spread])      # L / R LFO phase offset
        free = self._mod_free_phase(state, frames, rate)          # (F,)
        sync = self._mod_clock_sync(
            module, frames, buffers, patch, state, division, free
        )
        pb = free if sync is None else sync[0]
        ph = (pb[None, :] + offs[:, None]) % 1.0                  # (2, F)
        lfo = np.sin(2.0 * np.pi * ph)                            # (2, F)

        out = np.empty((2, frames), dtype=np.float64)
        rows = np.arange(2)
        dry_gain = 1.0 - mix

        if not tz_on:
            # ---- Standard positive-delay flanger (unchanged path) --------
            manual_samp = (manual_ms if man is None else man) * sr / 1000.0
            sweep_samp = (self._FLANGER_SWEEP_MS * depth) * sr / 1000.0
            delay = manual_samp + sweep_samp * lfo                    # (2, F)
            np.clip(delay, self._FLANGER_MIN_SAMP, float(L - 2), out=delay)
            # THE TRAP (the chorus's and the tape's): a ring index rounds
            # at its own magnitude, so ``wp - delay`` -- ``wp`` wraps at a
            # different absolute sample for every block size -- hands back
            # a fraction a float64 ulp apart. Split the delay into WHOLE
            # samples and a FRACTION instead, both functions of the small
            # delay alone (``back - delay`` is exact: Sterbenz, delay >= 2).
            back = np.ceil(delay)                                     # (2, F)
            fracs = back - delay               # forward weight, in [0, 1)
            backs = back.astype(np.int64)
            for i in range(frames):
                i0 = wp - backs[:, i]                          # (2,)
                frac = fracs[:, i]
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
            move_min = self._FLANGER_TZ_MOVE_MIN
            if man is None:
                D0 = manual_ms * sr / 1000.0
                d_ref = min(max(D0, self._FLANGER_MIN_SAMP), float(L - 2))
                d_ref_seq = np.full(frames, d_ref)
                sweep_samp = depth * max(D0 - move_min, 0.0)
            else:
                # manual_cv moves the REFERENCE tap too, so the crossing
                # itself travels: the zero is wherever the envelope put it.
                D0 = man * sr / 1000.0                            # (F,)
                d_ref_seq = np.clip(D0, self._FLANGER_MIN_SAMP, float(L - 2))
                sweep_samp = depth * np.maximum(D0 - move_min, 0.0)
            dm = D0 + sweep_samp * lfo                                # (2, F)
            np.clip(dm, move_min, float(L - 2), out=dm)
            # Both taps split into whole samples + a fraction, never
            # ``wp - delay`` (see the standard path above).
            back_a = np.ceil(d_ref_seq)                               # (F,)
            fas = (back_a - d_ref_seq).tolist()
            backs_a = back_a.astype(np.int64).tolist()
            back = np.ceil(dm)                                        # (2, F)
            fracs = back - dm
            backs = back.astype(np.int64)
            for i in range(frames):
                # reference tap (same delay both channels, own rows)
                ja = wp - backs_a[i]
                fa = fas[i]
                a = (
                    buf[rows, ja % L] * (1.0 - fa)
                    + buf[rows, (ja + 1) % L] * fa
                )                                              # (2,)
                # swept moving tap (per channel)
                i0 = wp - backs[:, i]                          # (2,)
                frac = fracs[:, i]
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
        if sync is None:
            state["ph_n"] += frames
        else:
            # The locked sweep drove this block: re-anchor the free-running
            # line at its end, so pulling the cable carries on from there.
            state["phase"] = sync[1]
            state["ph_n"] = 0

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

        Love pass (2026-09-22), all three OFF at their defaults so the
        shipped render is bit-identical: a ``clock`` jack that locks the
        sweep to the cable (one notch sweep every ``division`` ticks, via
        :meth:`_mod_clock_sync`); ``spread``, which is the L/R LFO phase
        offset the quarter-cycle quadrature used to hard-code (0.5 is that
        quadrature, 0 collapses the pair to one chain, 1 counter-sweeps);
        and ``manual_cv``, a per-sample octave jack on ``center`` so an
        envelope or a pedal can sweep the notches with ``depth`` at 0 --
        the classic envelope phaser.
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
        spread = float(module.params.get("spread", 0.5))
        division = float(module.params.get("division", 4.0))
        manual_depth = float(module.params.get("manual_depth", 1.0))
        stages = int(round(float(module.params.get("stages", 6))))
        if stages not in self._PHASER_STAGES:
            stages = min(self._PHASER_STAGES, key=lambda v: abs(v - stages))
        depth = min(max(depth, 0.0), 1.0)
        mix = min(max(mix, 0.0), 1.0)
        feedback = min(max(feedback, -0.95), 0.95)   # bipolar, below runaway
        center = min(max(center, self._PHASER_CENTER_MIN), self._PHASER_CENTER_MAX)
        spread = min(max(spread, 0.0), 1.0)
        division = min(max(division, self._MOD_DIV_MIN), self._MOD_DIV_MAX)

        state = self._state.setdefault(module.id, {})
        if "s" not in state or state["s"].shape != (2, stages):
            keep = {k: state[k] for k in self._MOD_CLOCK_KEYS if k in state}
            state.clear()
            state.update(keep)
            state["s"] = np.zeros((2, stages), dtype=np.float64)   # allpass memory
            state["yprev"] = np.zeros(2, dtype=np.float64)         # feedback memory
            # LFO: anchor phase + integer count since the last rate change
            # (``_mod_free_phase``); ``inc`` -1 makes the first block anchor.
            state["phase"] = 0.0
            state["ph_n"] = 0
            state["inc"] = -1.0

        s = state["s"]
        yprev = state["yprev"]

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        # rate_cv: 1 V/oct on the LFO rate, block-mean -- a sub-audio LFO,
        # so one rate per block is the right cost/quality trade-off (the
        # same cadence the chorus and flanger use for their rate_cv).
        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * self._pow2_clipped(cv_depth * self._finite_mean(rate_cv))
        rate = min(max(rate, 0.01), 20.0)

        x = src.astype(np.float64)                        # (F,)

        # One sine LFO; ``spread`` sets how far apart the L and R phases
        # run (0 = together, 0.5 = the shipped quarter-cycle quadrature,
        # 1 = a half cycle apart), so the two notch chains sweep out of
        # step. ``clock`` patched and locked replaces the free-running
        # phase line with an absolute-sample one (one sweep per
        # ``division`` ticks). Free-running, the phase is an integer
        # sample count since the last rate change (``_mod_free_phase``),
        # so it is the same bits at any block size.
        offs = np.array([0.0, 0.5 * spread])      # L / R LFO phase offset
        free = self._mod_free_phase(state, frames, rate)          # (F,)
        sync = self._mod_clock_sync(
            module, frames, buffers, patch, state, division, free
        )
        pb = free if sync is None else sync[0]
        ph = (pb[None, :] + offs[:, None]) % 1.0                  # (2, F)
        lfo = np.sin(2.0 * np.pi * ph)                            # (2, F)

        # Exponential (musical) sweep of the break frequency: +/- depth*2
        # octaves around ``center``, clamped well inside Nyquist so the
        # allpass coefficient stays finite.
        octs = self._PHASER_MAX_OCT * depth
        # manual_cv: the notch position as a jack -- 1 V/oct x
        # ``manual_depth`` on ``center``, read PER SAMPLE so an envelope or
        # a pedal can sweep the notches with the LFO's ``depth`` at 0 (the
        # classic envelope phaser). Unpatched leaves ``center`` the scalar
        # it always was, so the shipped render is untouched. A voice source
        # is summed to mono, like ``in``.
        manual_cv = self._input_buffer(patch, buffers, module.id, "manual_cv")
        if manual_cv is not None and manual_cv.size > 0:
            base = np.clip(
                center * self._pow2_clipped(
                    manual_depth * manual_cv.astype(np.float64)
                ),
                self._PHASER_CENTER_MIN, self._PHASER_CENTER_MAX,
            )[None, :]                                            # (1, F)
        else:
            base = center
        fc = base * (2.0 ** (octs * lfo))                         # (2, F)
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
        if sync is None:
            state["ph_n"] += frames
        else:
            # The locked sweep drove this block: re-anchor the free-running
            # line at its end, so pulling the cable carries on from there.
            state["phase"] = sync[1]
            state["ph_n"] = 0

        dry = (1.0 - mix) * x
        out_l = (dry + mix * wet[0]).astype(np.float32)
        out_r = (dry + mix * wet[1]).astype(np.float32)
        return {"out_l": out_l, "out_r": out_r}
