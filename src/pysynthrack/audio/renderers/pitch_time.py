"""Pitch/time renderers: granular, resampler, pitch shifter, tape.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". The shared backend helpers (``_voice_mean``, ``_finite_mean``,
``_pow2_clipped``, ``_gate_ramp_env``, ``_dist_curve``, ``_loud_shelf``,
``_input_buffer``) are reached through ``self``. The module-level DSP
pieces of ``numpy_backend`` -- ``_hermite4``, ``_brake_ramp``,
``_GrainShifter``, ``_lpc_coeffs``, ``_detect_period``, ``_Oversampler4``,
``_OS_LATENCY`` -- come in through lazy imports at their point of use,
never inside a per-sample loop (``numpy_backend`` imports this module,
so a top-level import would be circular).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import butter, lfilter, sosfilt


class PitchTimeRenderers:
    # ----- Granular rendering ---------------------------------------------

    # Slice 1: capture + a synchronous, deterministic grain stream, mono.
    # See modules/granular.py for the knob semantics.
    _GR_HEAD = 2              # samples of head start a read needs (Hermite +2 tap)
    _GR_MAX_GRAINS = 96       # in flight; 100/s x 0.5 s = 50 mean, spray_time clumps
    _GR_SPRAY_ST_MAX = 36.0   # |pitch + spray| never past three octaves
    _GR_MIN_LEN = 16          # shortest grain, samples (also the shortened floor)
    _GR_EXPO_ATTACK = 0.1     # expo window: linear rise over this fraction ...
    _GR_EXPO_FLOOR = 1e-3     # ... then an exponential decay to this (-60 dB)

    def _gr_window(self, kk, L, shape):
        """Window value at integer grain sample ``kk`` of a grain ``L`` long.

        Arrays broadcast (``kk`` (G, F), ``L`` (G, 1)). ``shape`` is an
        index into GRANULAR_WINDOWS. ``hann`` and ``triangle`` are the
        *periodic* forms (``k / L``, not ``k / (L - 1)``), which is what
        makes two of them at hop L/2 sum to one -- the neutral. ``expo``
        rises linearly over the first tenth and decays exponentially to
        -60 dB at the end: the classic "expodec" percussive grain.
        """
        if shape == 0:                      # hann
            return 0.5 - 0.5 * np.cos(2.0 * np.pi * kk / L)
        if shape == 1:                      # triangle
            return 1.0 - np.abs(2.0 * kk / L - 1.0)
        a = np.maximum(1.0, np.round(self._GR_EXPO_ATTACK * L))
        tail = np.maximum(1.0, L - 1.0 - a)
        rise = kk / a
        fall = np.exp(np.log(self._GR_EXPO_FLOOR) * (kk - a) / tail)
        return np.where(kk < a, rise, fall)

    def _gr_window_mean(self, state, L, shape):
        """Mean of the window over its length: the overlap normalizer.

        Exactly 0.5 for the two COLA windows (they tile to one at hop
        L/2, so their average is half by construction -- and using the
        closed form keeps the neutral's gain at exactly 1.0). ``expo`` is
        measured once per (L) and cached.
        """
        if shape != 2:
            return 0.5
        key = ("wmean", int(L))
        if key not in state:
            kk = np.arange(int(L), dtype=np.float64)
            state[key] = float(np.mean(self._gr_window(kk, float(L), 2)))
        return state[key]

    def _render_granular(self, module, frames: int, buffers, patch) -> dict:
        """Grain cloud over a live ring buffer: mono in -> out / out_l / out_r.

        Every block: (1) the input is written into the ring at absolute
        sample indices (``absw`` counts samples since the module was
        built; reads are absolute too, modulo the ring, so nothing here
        knows or cares where block boundaries fall); (2) the scheduler
        fires the grains whose onsets land in this block -- the next
        onset is the last one plus ``sr / density`` scaled by that
        grain's own ``spray_time`` draw -- each one a record of
        ``(onset, read start, rate, length, window, level, pan)`` frozen
        from the params (plus its own draws) at that moment; (3) every
        grain in flight is rendered as one ``(G, F)`` matrix op --
        ``k = n - onset``, ``pos = start + rate * k``, a 4-tap Hermite
        read of the ring at ``pos``, times the window at ``k``, masked
        to the grain's span -- and summed over G in spawn order, once
        unpanned for ``out`` and, when any grain in flight is panned,
        once per channel; (4) grains whose span has ended are dropped.

        Because a grain's parameters are fixed at its onset and the sums
        run in a fixed order, the render is block-size independent to
        the bit, and a knob turn reaches the *next* grain only -- no
        zipper, no clicks. The read start is ``onset - D`` where ``D``
        is the grain's ``position * buffer`` floored at the head start
        it needs: ``_GR_HEAD`` for the interpolator's forward taps plus
        ``(rate - 1) * (L - 1)`` when the grain reads faster than real
        time (else it would overtake the write head). If ``buffer`` is
        shorter than that head start the grain is shortened to fit.

        Freeze (slice 3): the ring is indexed in *captured* time -- a
        counter ``c`` that advances only while recording -- while grain
        onsets run on wall time ``absw``. Per sample, ``frozen = gate >
        0.5 or the freeze param``; the block's input is written only at
        the live samples, and ``hw[j]`` (the index of the last captured
        sample as of sample j) is what a grain fired at j reads back
        from: ``start = hw[j] - D``. Live, ``hw[j]`` is the sample's own
        index and everything reduces to slices 1-2 exactly; frozen, the
        head stands still, so the grain's required head start is
        ``rate * (L - 1)`` rather than ``(rate - 1) * (L - 1)``, and a
        read is clamped to ``hw - 2`` per sample so a grain that was
        running alongside the head when a freeze landed holds its last
        sample instead of running into stale data. Releasing the freeze
        resumes recording at ``c`` -- captured time is continuous, no
        hole. ``position_cv`` (times ``position_cv_depth``) is read at
        the grain's onset sample and latched.

        Randomness (slice 2): grain ``i`` draws four uniforms from
        ``default_rng([seed, i])`` -- interval factor, position offset,
        pitch offset, pan -- so the cloud is a pure function of the seed
        and the grain index, whatever the blocking. No draws are made at
        all while every spray and ``width`` is zero, and the arithmetic
        then reduces to the slice-1 stream exactly.

        Grains are scaled by ``1 / max(1, density * size * window_mean)``
        so a dense cloud sits at about the input's level. The dry side
        of ``mix`` is the live input ``_GR_HEAD`` samples late (a
        two-sample tail carried in state -- not a ring read, which would
        go stale while frozen), so at the neutral (position 0, pitch 0)
        dry and wet line up sample for sample; it is centred in
        ``out_l`` / ``out_r``. The pan law is constant-peak (``min(1,
        1 -/+ pan)``), so ``width`` 0 leaves the stereo outs
        bit-identical to ``out``.
        """
        from ...modules.granular import GRANULAR_WINDOWS

        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}
        pos_cv = self._input_buffer(patch, buffers, module.id, "position_cv", collapse=False)
        if pos_cv is not None and pos_cv.ndim == 2:
            pos_cv = self._voice_mean(pos_cv)
        fz_gate = self._input_buffer(patch, buffers, module.id, "freeze")

        sr = float(self.sample_rate)

        def _f(name, lo, hi, default):
            try:
                return float(np.clip(float(module.params.get(name, default)), lo, hi))
            except (TypeError, ValueError):
                return default

        try:
            toggle = float(module.params.get("freeze", False)) >= 0.5
        except (TypeError, ValueError):
            toggle = bool(module.params.get("freeze", False))
        cv_depth = _f("position_cv_depth", -1.0, 1.0, 1.0)
        buffer_s = _f("buffer", 0.5, 10.0, 2.0)
        density = _f("density", 0.5, 100.0, 25.0)
        size_ms = _f("size", 10.0, 500.0, 80.0)
        pitch = _f("pitch", -24.0, 24.0, 0.0)
        position = _f("position", 0.0, 1.0, 0.0)
        mix = _f("mix", 0.0, 1.0, 1.0)
        spray_time = _f("spray_time", 0.0, 1.0, 0.0)
        spray_pos = _f("spray_pos", 0.0, 1.0, 0.0)
        spray_pitch = _f("spray_pitch", 0.0, 1200.0, 0.0)
        width = _f("width", 0.0, 1.0, 0.0)
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        window = str(module.params.get("window", "hann"))
        shape = GRANULAR_WINDOWS.index(window) if window in GRANULAR_WINDOWS else 0
        sprayed = spray_time > 0.0 or spray_pos > 0.0 or spray_pitch > 0.0 or width > 0.0

        B = int(round(buffer_s * sr))                       # history, samples
        L_max = int(np.ceil(0.5 * sr)) + 2                  # the longest grain
        N = B + L_max + frames + 16

        state = self._state.setdefault(module.id, {})
        if state.get("N") != N:
            # (Re)build the ring. Keep whatever history the new size can
            # hold -- absolute indexing makes that a straight copy -- and
            # drop the grains in flight: their reads were sized for the
            # old ring.
            old_ring = state.get("ring")
            old_N = state.get("N")
            absw = int(state.get("absw", 0))
            c = int(state.get("c", absw))
            dry_tail = state.get("dry_tail")
            ring = np.zeros(N, dtype=np.float64)
            if old_ring is not None:
                keep = min(old_N, N, c)
                if keep > 0:
                    m = np.arange(c - keep, c)
                    ring[m % N] = old_ring[m % old_N]
            state.clear()
            state["N"] = N
            state["ring"] = ring
            state["absw"] = absw
            state["c"] = c                  # captured samples: the ring's clock
            state["dry_tail"] = np.zeros(self._GR_HEAD) if dry_tail is None else dry_tail
            state["onset"] = np.zeros(0, dtype=np.int64)
            state["start"] = np.zeros(0, dtype=np.float64)
            state["rate"] = np.zeros(0, dtype=np.float64)
            state["len"] = np.zeros(0, dtype=np.int64)
            state["shape"] = np.zeros(0, dtype=np.int64)
            state["amp"] = np.zeros(0, dtype=np.float64)
            state["pan"] = np.zeros(0, dtype=np.float64)
            state["last_t"] = None
            state["gi"] = 0                 # index of the next grain to fire
            state["pend"] = None            # (gi, draws) cached for that grain
        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out": e, "out_l": e, "out_r": e}

        ring = state["ring"]
        absw = int(state["absw"])
        n = absw + np.arange(frames, dtype=np.int64)

        # --- 1. capture (only while not frozen) ------------------------
        x = src.astype(np.float64)
        c0 = int(state["c"])
        if fz_gate is not None and fz_gate.shape[0] == frames:
            frozen = fz_gate > 0.5
            if toggle:
                frozen = np.ones(frames, dtype=bool)
        else:
            frozen = np.full(frames, toggle, dtype=bool)
        live = ~frozen
        # hw[j]: index (captured time) of the last sample captured as of
        # sample j -- its own index when live, the head when frozen.
        hw = c0 + np.cumsum(live) - 1
        if frozen.any():
            ring[hw[live] % N] = x[live]
        else:
            ring[hw % N] = x
        state["c"] = int(hw[-1]) + 1

        # --- 2. this block's nominal grain recipe -----------------------
        L0 = max(self._GR_MIN_LEN, int(round(size_ms * 1e-3 * sr)))
        L0 += L0 & 1                                        # even: 50% overlap is exact
        overlap = density * L0 / sr * self._gr_window_mean(state, L0, shape)
        amp = 1.0 / max(1.0, overlap)
        hop = sr / density

        def _draws(gi):
            """Grain gi's four uniforms (interval, position, pitch, pan),
            or None while nothing is sprayed. Cached for the pending
            grain so a block boundary never re-rolls it."""
            if not sprayed:
                return None
            pend = state["pend"]
            if pend is not None and pend[0] == gi:
                return pend[1]
            u = np.random.default_rng([seed, gi]).random(4)
            state["pend"] = (gi, u)
            return u

        def _interval(u):
            if u is None:
                return hop
            return hop * (1.0 + spray_time * (2.0 * u[0] - 1.0))

        # --- 3. fire the grains that land in this block -----------------
        gi = int(state["gi"])
        last_t = state["last_t"]
        if last_t is None:
            next_t = float(absw)
        else:
            next_t = last_t + _interval(_draws(gi))
            if next_t < absw:
                # Only after a density / spray_time change: the pending
                # grain is due now, not at the old interval.
                next_t = float(absw)
        end = absw + frames
        new = []
        while next_t < end:
            u = _draws(gi)
            onset = int(np.floor(next_t))
            j = onset - absw
            if u is None:
                st, p, pan = pitch, position, 0.0
            else:
                st = pitch + spray_pitch / 100.0 * (2.0 * u[2] - 1.0)
                st = max(-self._GR_SPRAY_ST_MAX, min(self._GR_SPRAY_ST_MAX, st))
                p = position + spray_pos * (2.0 * u[1] - 1.0)
                pan = width * (2.0 * u[3] - 1.0)
            if pos_cv is not None and pos_cv.shape[0] == frames:
                # Read at the onset sample and latched for this grain.
                p = p + cv_depth * float(pos_cv[j])
            p = min(1.0, max(0.0, p))
            rate = 2.0 ** (st / 12.0)
            L = L0
            # Live, the head advances with the grain (drift rate - 1);
            # frozen, it stands still (drift rate).
            ahead = max(0.0, rate - (0.0 if frozen[j] else 1.0))
            head = self._GR_HEAD + int(np.ceil(ahead * (L - 1)))
            if head > B and ahead > 0.0:
                # The buffer can't give this grain the head start it
                # needs at this rate: shorten it to what it can feed.
                L = max(self._GR_MIN_LEN, int((B - self._GR_HEAD) / ahead) + 1)
                L += L & 1
                head = self._GR_HEAD + int(np.ceil(ahead * (L - 1)))
            D = max(float(head), p * B)
            new.append((onset, float(hw[j]) - D, rate, L, pan))
            last_t = next_t
            gi += 1
            next_t += _interval(_draws(gi))
        state["last_t"] = last_t
        state["gi"] = gi
        if new:
            room = self._GR_MAX_GRAINS - state["onset"].shape[0]
            new = new[:max(0, room)]
        if new:
            k = len(new)
            cols = list(zip(*new))
            state["onset"] = np.concatenate([state["onset"], np.asarray(cols[0], dtype=np.int64)])
            state["start"] = np.concatenate([state["start"], np.asarray(cols[1], dtype=np.float64)])
            state["rate"] = np.concatenate([state["rate"], np.asarray(cols[2], dtype=np.float64)])
            state["len"] = np.concatenate([state["len"], np.asarray(cols[3], dtype=np.int64)])
            state["shape"] = np.concatenate([state["shape"], np.full(k, shape, dtype=np.int64)])
            state["amp"] = np.concatenate([state["amp"], np.full(k, amp)])
            state["pan"] = np.concatenate([state["pan"], np.asarray(cols[4], dtype=np.float64)])

        # --- 4. render every grain in flight ----------------------------
        G = state["onset"].shape[0]
        if G == 0:
            wet = np.zeros(frames, dtype=np.float64)
            wet_l = wet_r = wet
        else:
            g_on = state["onset"][:, None]
            g_len = state["len"][:, None]
            kk = n[None, :] - g_on                          # (G, F)
            valid = (kk >= 0) & (kk < g_len)
            kk = np.minimum(np.maximum(kk, 0), g_len - 1)
            pos = state["start"][:, None] + state["rate"][:, None] * kk
            # Never past the last captured sample (minus the Hermite's
            # forward taps). A no-op while live -- the head start
            # guarantees it -- and the hold when a freeze lands mid-grain.
            pos = np.minimum(pos, (hw - self._GR_HEAD)[None, :].astype(np.float64))
            i0 = np.floor(pos).astype(np.int64)
            frac = pos - i0
            from ..numpy_backend import _hermite4  # lazy: it imports this module
            y = _hermite4(
                ring[(i0 - 1) % N], ring[i0 % N], ring[(i0 + 1) % N], ring[(i0 + 2) % N], frac
            )
            w = np.empty_like(pos)
            g_shape = state["shape"]
            for si in np.unique(g_shape):
                rows = g_shape == si
                w[rows] = self._gr_window(kk[rows].astype(np.float64), g_len[rows].astype(np.float64), int(si))
            contrib = np.where(valid, w * y * state["amp"][:, None], 0.0)
            wet = contrib.sum(axis=0)
            pans = state["pan"]
            if np.any(pans != 0.0):
                # Constant-peak law: a centred grain is at unity in both
                # channels (so an unpanned grain's row is untouched).
                gl = np.minimum(1.0, 1.0 - pans)[:, None]
                gr = np.minimum(1.0, 1.0 + pans)[:, None]
                wet_l = (contrib * gl).sum(axis=0)
                wet_r = (contrib * gr).sum(axis=0)
            else:
                wet_l = wet_r = wet

            # --- 5. retire the grains whose span has ended -------------
            keep = (state["onset"] + state["len"]) > end
            if not keep.all():
                for key in ("onset", "start", "rate", "len", "shape", "amp", "pan"):
                    state[key] = state[key][keep]

        state["absw"] = end

        # --- mix: dry is the live input _GR_HEAD samples late -----------
        tail = state["dry_tail"]
        joined = np.concatenate([tail, x])
        state["dry_tail"] = joined[-self._GR_HEAD:].copy()
        if mix >= 1.0:
            out = wet.astype(np.float32)
            if wet_l is wet:
                return {"out": out, "out_l": out, "out_r": out}
            return {"out": out, "out_l": wet_l.astype(np.float32), "out_r": wet_r.astype(np.float32)}
        dry = joined[:frames]
        if mix <= 0.0:
            out = dry.astype(np.float32)
            return {"out": out, "out_l": out, "out_r": out}
        out = ((1.0 - mix) * dry + mix * wet).astype(np.float32)
        if wet_l is wet:
            return {"out": out, "out_l": out, "out_r": out}
        return {
            "out": out,
            "out_l": ((1.0 - mix) * dry + mix * wet_l).astype(np.float32),
            "out_r": ((1.0 - mix) * dry + mix * wet_r).astype(np.float32),
        }

    # ----- Resampler rendering --------------------------------------------

    # Looping-buffer window for the varispeed read head: the ``window``
    # param, in milliseconds, clamped to this range (engine-level, like
    # ``mix``). The read head trails the write head inside the window
    # and wraps within it, so the module keeps sounding forever on a
    # continuous signal. Longer = subtler loop texture but more latency
    # (the latency is half the window); shorter = tighter latency but a
    # stronger granular-repeat texture on non-unity shifts. That latency
    # is the unavoidable cost of varispeed on a live stream. The
    # frames*4 floor below still applies, so very small windows are
    # floored by the audio block size.
    _RESAMP_WINDOW_MS_MIN = 20.0
    _RESAMP_WINDOW_MS_MAX = 2000.0
    # The read head starts this fraction of the window behind the write
    # head. Centred (1/2) gives symmetric runway for pitch up (delay
    # shrinks) and pitch down (delay grows) before the first loop wrap.
    _RESAMP_INIT_FRAC = 0.5
    # Clamp the effective transpose so the playback ratio can't explode
    # (+/-60 st = +/-5 octaves -> ratio in [1/32, 32]).
    _RESAMP_MAX_ST = 60.0
    # Loop-seam declick. When the read head drifts inside a guard band
    # near either buffer edge (about to collide with the write head, or
    # to fall off the oldest sample), it jumps half a span back toward
    # the centre with a short equal-power crossfade between the old and
    # the jumped tap, instead of hard-wrapping with a click. This is the
    # nominal fade time; it shrinks automatically when extreme ratios
    # leave less old-tap runway than this.
    _RESAMP_XFADE_SEC = 0.008
    # Guard-band width as a fraction of the window (floored to the fade
    # length plus a margin, and to one block, so a jump always fires
    # with crossfade runway to spare and cross-block drift can't skip
    # past the band).
    _RESAMP_EDGE_FRAC = 0.06
    # Anti-alias (optional ``antialias`` toggle, off by default). Pitching
    # up reads the ring faster than it's written, shifting source content
    # above Nyquist where it folds back as aliasing (real tape is
    # inherently band-limited and never does this). With the toggle on, the
    # input is low-passed at Fs/(2*ratio) into a second ring the wet read
    # uses on up-shifts, so nothing folds. This is the Butterworth order;
    # the normalised cutoff (1/ratio) is floored to WN_MIN so the steepest
    # up-shifts get partial AA in a numerically safe range rather than a
    # degenerate filter (WN_MIN 0.05 -> full AA to ~+52 st, ratio ~20).
    _RESAMP_AA_ORDER = 8
    _RESAMP_AA_WN_MIN = 0.05
    # Cut a little below the ideal Fs/(2*ratio) so the filter's transition
    # band sits below Nyquist-after-scaling -- content that survives it then
    # still lands in-band instead of folding. Costs a sliver of the topmost
    # pitched-up octave for markedly less aliasing.
    _RESAMP_AA_MARGIN = 0.85

    def _render_resampler(self, module, frames: int, buffers, patch):
        """Varispeed pitch shifter: resample ``in`` at a pitch-derived rate.

        Shape-polymorphic. Branches on the *audio* input's ndim (the
        signal being resampled owns the looping buffer):

          * 1D ``(F,)`` audio -> one looping buffer, output ``(F,)``.
          * 2D ``(V, F)`` audio -> V looping buffers (one per voice slot)
            with per-voice read heads, output ``(V, F)``.

        A mono ``pitch_cv`` broadcasts across voices; a ``(V, F)``
        ``pitch_cv`` drives each voice independently. Missing audio in ->
        silence out, with the buffer/heads left as-is so reconnecting the
        cable doesn't snap.

        Always returns the ``{"out", "out_l", "out_r"}`` dict (the
        multi-output convention). At ``spread`` 0 the pair mirrors the
        centre ``out``, so wiring the stereo outs plays the mono signal
        rather than falling silent; above 0 they carry the detuned pair.
        """
        src = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}

        pitch_cv = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        # The brake is a transport gesture, module-wide: a voice-aware
        # gate collapses so *any* voice's gate high engages it.
        brake_gate = self._input_buffer(
            patch, buffers, module.id, "brake", collapse=False
        )
        if brake_gate is not None and brake_gate.ndim == 2:
            brake_gate = brake_gate.max(axis=0)

        if src.ndim == 2:
            V = src.shape[0]
            if pitch_cv is None:
                cv = None
            elif pitch_cv.ndim == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[0]))
            elif pitch_cv.shape[0] == V:
                cv = pitch_cv
            elif pitch_cv.shape[0] == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[1]))
            else:
                # Voice-count mismatch -> one shared transpose for all.
                cv = np.broadcast_to(
                    self._voice_mean(pitch_cv), (V, pitch_cv.shape[1])
                )
            result = self._render_resampler_core(
                module, frames, src, cv, brake_gate
            )
        else:
            # Mono audio. A 2D pitch_cv collapses to a single shared
            # transpose (mean over voices) -- summing pitch voltages would
            # be nonsense.
            if pitch_cv is not None and pitch_cv.ndim == 2:
                pitch_cv = self._voice_mean(pitch_cv)
            d = self._render_resampler_core(
                module,
                frames,
                src[np.newaxis, :],
                None if pitch_cv is None else pitch_cv[np.newaxis, :],
                brake_gate,
            )
            result = {name: buf[0] for name, buf in d.items()}

        # Always the {out, out_l, out_r} dict (the pair mirrors out at
        # spread 0), keyed onto the three output ports downstream.
        return result

    def _render_resampler_core(self, module, frames, src, cv, brake_gate=None):
        """Shared ``(V, F)`` varispeed engine.

        ``src`` is ``(V, F)`` audio; ``cv`` is ``(V, F)`` pitch CV or
        None; ``brake_gate`` is a ``(F,)`` gate for the tape-stop brake
        or None (already collapsed module-wide by the caller). The mono
        path calls this with ``V == 1``, so a single voice row is
        bit-identical to the mono render -- the float ops are the same
        per row regardless of V.

        Per output sample the read head advances by the playback ratio
        ``2 ** (st/12)`` (``st`` summed in semitone space and optionally
        glided), reading the per-voice ring buffer with 4-tap cubic
        Hermite (Catmull-Rom) interpolation -- a flat-passband read that
        keeps non-integer transposition clean, and returns the sample
        exactly at an integer position so unity ratio stays bit-exact.
        The whole block is vectorized: the read positions are the
        cumulative integral of the per-sample ratio.

        **Loop-seam declick.** At a non-unity ratio the read head
        eventually collides with the write head (pitch up) or falls off
        the oldest buffered sample (pitch down). Rather than
        hard-wrapping the read phase with a click (audio from a window
        apart butted together), the head watches a guard band near both
        edges and *jumps half a span* back toward the centre as soon as
        it drifts inside, equal-power crossfading from the old tap to
        the jumped tap (``_RESAMP_XFADE_SEC``, shortened when extreme
        ratios leave less runway). Far from the edges nothing fires and
        the legacy single-tap path runs bit-identically -- in particular
        unity ratio, where the head doesn't drift, stays a bit-exact
        delayed passthrough.

        **Dry/wet mix.** ``mix`` blends the varispeed signal against a
        dry tap read from the *same ring buffer* at the fixed initial
        delay, so dry and wet are sample-aligned at unity ratio and a
        mix sweep is a coherent blend with no slapback offset. ``mix=1``
        (the default) skips the dry read and is bit-identical to the
        wet-only render; ``mix=0`` is the delayed dry passthrough.

        **Anti-alias (optional).** With the ``antialias`` param on, a
        second ring holds the input low-passed at ``Fs/(2*ratio)``
        (Butterworth), and the wet read samples *that* ring whenever the
        block pitches up -- so source content that would shift past
        Nyquist and fold back as aliasing is removed before the faster
        read, the way tape's inherent band-limiting prevents it. Off by
        default (the raw read keeps the lo-fi/tape character and every
        existing render bit-for-bit). Unity and pitch-down keep reading
        the raw ring, and the dry tap always does, so those bit-exact
        paths are untouched; only the up-shift wet read changes.

        **Tape-stop brake.** With the ``brake`` param on or the
        ``brake`` gate high, a per-sample brake position ramps 1 -> 0
        over ``brake_time`` seconds (0 -> 1 over ``spinup_time`` on
        release) and *multiplies the playback ratio* -- deceleration
        linear in speed, the constant-torque physics of a real platter
        or capstan winding down. Working in ratio space is the point:
        glide ramps in semitone space, where a dead stop is minus
        infinity, unreachable; the brake scales the ratio to an actual
        zero, freezing the read head (pitch dives through the floor,
        then the output holds a constant -- silence through any AC
        path). While frozen the write head keeps filling the ring, so
        the ordinary low-edge seam machinery re-centres the head under
        its crossfade as the ring laps it -- crossfades between
        near-constant values, inaudible. The gesture is module-wide
        (every voice and spread channel brakes together, one transport)
        and sits after glide/pitch and before the anti-alias cutoff
        tracking (a braked read is slower, so AA correctly relaxes).
        Disengaged with the position fully recovered, the multiply is
        skipped entirely -- every existing render is bit-for-bit
        untouched. The ``mix`` dry tap is a fixed-lag ring read and
        keeps playing through a stop.
        """
        V = src.shape[0]
        sr = self.sample_rate
        window_ms = float(module.params.get("window", 200.0))
        window_ms = min(
            max(window_ms, self._RESAMP_WINDOW_MS_MIN),
            self._RESAMP_WINDOW_MS_MAX,
        )
        # 200.0/1000.0 is the same double as the old 0.2 literal, so the
        # default window reproduces the pre-param L bit-for-bit.
        L = int((window_ms / 1000.0) * sr)
        L = max(L, frames * 4, 8)   # always comfortably larger than a block
        span = L - 1                # loop span; keeps both interp taps valid

        state = self._state.setdefault(module.id, {})
        old = state.get("buf")
        if (
            old is not None
            and old.shape[0] == V
            and old.shape[1] != L
            and "xf_rem" in state
        ):
            # The ``window`` param changed mid-stream (same voice count):
            # rebuild the ring at the new length, preserving the most
            # recent audio so a slider drag doesn't punch a hole in the
            # sound. Sample at lag ``l`` moves from ``(w - l) % oldL`` to
            # ``L - l`` with the new write head at 0, so every head keeps
            # its absolute lag (clamped into the new window). If the new
            # geometry leaves a head inside a guard band, the ordinary
            # seam machinery re-centres it under an equal-power crossfade
            # on this very block. Only a shrink below a head's lag loses
            # the content under it (the tail it was reading no longer
            # exists); that one step rides the seam crossfade too, just
            # with new-window content.
            oldL = old.shape[1]
            keep = min(oldL, L)
            w = int(state["write_idx"])
            src_idx = (w + oldL - keep + np.arange(keep)) % oldL
            newbuf = np.zeros((V, L), dtype=np.float64)
            newbuf[:, L - keep:] = old[:, src_idx]
            state["buf"] = newbuf
            state["write_idx"] = 0
            np.clip(state["delay"], 1.0, float(L - 1), out=state["delay"])
            # In-flight seam fades reference the old geometry; drop them
            # (their old tap would read relocated content).
            state["xf_rem"][:] = 0
            state["xf_len"][:] = 1
            state["xf_off"][:] = 0.0
            # Stereo-spread channels ride the same geometry; drop them so
            # they re-seed aligned to the rebuilt centre head next read.
            for suf in ("_l", "_r"):
                for k in ("delay", "xf_rem", "xf_len", "xf_off", "seam_jumps"):
                    state.pop(k + suf, None)
        needs_reinit = (
            "buf" not in state or state["buf"].shape != (V, L)
        )
        if needs_reinit:
            state.clear()
            state["buf"] = np.zeros((V, L), dtype=np.float64)
            state["write_idx"] = 0
            init_delay = float(
                max(1, min(L - 1, int(self._RESAMP_INIT_FRAC * L)))
            )
            state["delay"] = np.full(V, init_delay, dtype=np.float64)
            state["last_st"] = np.zeros(V, dtype=np.float64)
        if "xf_rem" not in state:
            # Seam-crossfade + jump-count state (created lazily so a
            # reinit -- or a state carried across this feature's
            # introduction -- picks it up cleanly).
            state["xf_rem"] = np.zeros(V, dtype=np.int64)
            state["xf_len"] = np.ones(V, dtype=np.int64)
            state["xf_off"] = np.zeros(V, dtype=np.float64)
            state["seam_jumps"] = np.zeros(V, dtype=np.int64)

        buf = state["buf"]
        write_idx = int(state["write_idx"])
        # state["delay"]: (V,) float, the read head's lag in (1, L)
        last_st = state["last_st"]  # (V,) float, glide one-pole memory

        if frames == 0:
            return np.empty((V, 0), dtype=np.float32)

        # --- write the incoming block into each voice's ring buffer ---
        write_slots = (write_idx + np.arange(frames)) % L
        buf[:, write_slots] = src.astype(np.float64)
        # New write head; window_start (oldest readable) is congruent to it
        # mod L, so absolute index (window_start + j) lives at (head + j) % L.
        head = (write_idx + frames) % L

        # --- per-sample transpose, summed in semitone space ---
        semis = float(module.params.get("semitones", 0.0))
        cents = float(module.params.get("cents", 0.0))
        cv_depth = float(module.params.get("cv_depth", 12.0))
        glide = float(module.params.get("glide", 0.0))
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)

        base_st = semis + cents / 100.0
        target = np.full((V, frames), base_st, dtype=np.float64)
        if cv is not None:
            target += cv_depth * cv.astype(np.float64)

        if glide > 0.0:
            # One-pole glide: y[n] = coef*x[n] + (1-coef)*y[n-1].
            coef = 1.0 - float(np.exp(-1.0 / (glide * sr)))
            zi = ((1.0 - coef) * last_st)[:, np.newaxis]   # (V, 1)
            smoothed = lfilter(
                [coef], [1.0, -(1.0 - coef)], target, axis=-1, zi=zi
            )[0]
        else:
            smoothed = target
        last_st = smoothed[:, -1].copy()

        # --- per-channel playback ratios ---
        # ``out`` is the centre pitch (unchanged). A positive ``spread``
        # adds a stereo detune pair -- ``out_l`` a touch flat, ``out_r`` a
        # touch sharp (``spread`` cents, split half to each side) -- for
        # one-module chorus-style width from a mono source. spread == 0
        # keeps the module mono: one read, the centre, bit-for-bit what it
        # always produced.
        spread = float(module.params.get("spread", 0.0))
        if spread > 0.0:
            half = spread / 200.0    # cents -> +/- semitones per side
            chans = (
                ("out", "", 0.0),
                ("out_l", "_l", -half),
                ("out_r", "_r", half),
            )
        else:
            chans = (("out", "", 0.0),)
            for suf in ("_l", "_r"):     # drop stale spread state when mono
                for k in ("delay", "xf_rem", "xf_len", "xf_off", "seam_jumps"):
                    state.pop(k + suf, None)

        ratios = {}
        for name, _ch, off in chans:
            s = smoothed + off if off else smoothed
            ratios[name] = np.exp2(
                np.clip(s, -self._RESAMP_MAX_ST, self._RESAMP_MAX_ST) / 12.0
            )

        # --- tape-stop / spin-up brake (ratio space) ---
        # The brake position ramps 1 -> 0 (engaged) / 0 -> 1 (released),
        # linear in speed, and multiplies every channel's ratio -- a true
        # deceleration to a dead stop, which semitone-space glide can't
        # reach (a stop is -inf semitones). Module-wide: one transport,
        # shared by all voices and spread channels. Fully released with
        # the position recovered, the multiply is skipped -- brake-free
        # renders stay bit-for-bit what they always were.
        brake_on = float(module.params.get("brake", 0.0)) >= 0.5
        brake_pos = float(state.get("brake_pos", 1.0))
        if brake_gate is not None:
            gate = (brake_gate.astype(np.float64) >= 0.5) | brake_on
            engaged = bool(gate.any())
        else:
            gate = None
            engaged = brake_on
        if engaged or brake_pos < 1.0:
            # Slopes in position-units per sample; a zero time stops (or
            # recovers) within one sample.
            dn = 1.0 / max(
                float(module.params.get("brake_time", 0.5)) * sr, 1.0
            )
            up = 1.0 / max(
                float(module.params.get("spinup_time", 0.25)) * sr, 1.0
            )
            if gate is None:
                gate = np.full(frames, brake_on)
            from ..numpy_backend import _brake_ramp  # lazy: it imports this module
            factor, brake_pos = _brake_ramp(brake_pos, gate, dn, up)
            state["brake_pos"] = brake_pos
            for name in ratios:
                ratios[name] = ratios[name] * factor
        else:
            state["brake_pos"] = 1.0

        # --- optional anti-alias for pitch-up ---
        # Reading faster than the write shifts source content above Nyquist,
        # which folds back as aliasing; band-limiting the input *before* the
        # faster read is the fix. Maintain one band-limited ring of the
        # input (low-passed at Fs/(2*ratio)) shared by all channels, its
        # cutoff tracking the highest channel ratio; each channel reads that
        # ring on its own up-shift, the raw ring otherwise. Off by default
        # -> raw read, every existing render bit-for-bit. Pitch-down and
        # unity keep the raw ring (bit-exact); the dry tap always does.
        read_bufs = {name: buf for name, _, _ in chans}
        if float(module.params.get("antialias", 0.0)) >= 0.5:
            r_rep = max(1.0, max(float(r.max()) for r in ratios.values()))
            # Normalised cutoff is 1/ratio; floor it so extreme up-shifts
            # stay in a numerically safe Butterworth range (partial AA
            # rather than a degenerate filter).
            wn = min(
                0.98,
                max(self._RESAMP_AA_WN_MIN, self._RESAMP_AA_MARGIN / r_rep),
            )
            # Second-order-section form: high-order Butterworth in
            # transfer-function form is ill-conditioned at low cutoffs
            # (extreme up-shifts); sos stays numerically stable.
            sos = butter(self._RESAMP_AA_ORDER, wn, btype="low", output="sos")
            buf_aa = state.get("buf_aa")
            if buf_aa is None or buf_aa.shape != (V, L):
                # Seed from the raw ring so first use / a window change / a
                # live toggle never punches a wet dropout -- the recent tail
                # is present (unfiltered for one window, then all AA'd).
                buf_aa = buf.copy()
                state["aa_zi"] = np.zeros(
                    (sos.shape[0], V, 2), dtype=np.float64
                )
            filt, state["aa_zi"] = sosfilt(
                sos, src.astype(np.float64), axis=-1, zi=state["aa_zi"]
            )
            buf_aa[:, write_slots] = filt
            state["buf_aa"] = buf_aa
            for name, _, _ in chans:
                if float(ratios[name].max()) > 1.0 + 1e-9:
                    read_bufs[name] = buf_aa
        else:
            # AA off: drop the second ring so a later toggle-on re-seeds
            # from the up-to-date raw ring instead of reading a stale gap.
            state.pop("buf_aa", None)

        # --- loop-seam declick guard band (shared by all channels) ---
        x_nom = max(1, int(self._RESAMP_XFADE_SEC * sr))
        guard = int(max(self._RESAMP_EDGE_FRAC * L, x_nom + 8, frames + 8))
        guard = min(guard, span // 3)

        # --- read each channel through the one ring (own head + seam state) ---
        for _, ch, _ in chans:
            self._ensure_resampler_channel(state, ch, V)
        wets = {}
        for name, ch, _ in chans:
            wets[name], nd = self._resampler_read_channel(
                state, ch, read_bufs[name], ratios[name],
                head, L, span, guard, x_nom, frames, V,
            )
            state["delay" + ch] = nd

        # --- dry/wet mix: the dry tap is the raw centre ring read at the
        # fixed initial delay (shared across channels), so unity wet and
        # dry are sample-aligned and the dry stays full-band under spread.
        if mix < 1.0:
            init_lag = int(max(1, min(L - 1, int(self._RESAMP_INIT_FRAC * L))))
            dry_slots = (head + (L - init_lag) + np.arange(frames)) % L
            dry = buf[:, dry_slots]
            out = {
                name: (mix * w + (1.0 - mix) * dry).astype(np.float32)
                for name, w in wets.items()
            }
        else:
            out = {name: w.astype(np.float32) for name, w in wets.items()}

        # Always emit the stereo pair (chorus/reverb convention): at
        # spread 0 they mirror the centre, so a patch wired to out_l/out_r
        # plays the mono signal rather than falling silent until spread is
        # dialled up. Same array -> no extra read/copy on the mono path.
        if "out_l" not in out:
            out["out_l"] = out["out"]
            out["out_r"] = out["out"]

        state["buf"] = buf
        state["write_idx"] = head
        state["last_st"] = last_st
        return out

    def _ensure_resampler_channel(self, state, ch, V):
        """Lazily create a stereo-spread channel's read-head + crossfade
        state. The centre ("") is set up by the main reinit; ``"_l"`` /
        ``"_r"`` start *aligned* with the centre head (``state["delay"]``)
        so engaging ``spread`` mid-stream doesn't jump -- they then drift
        apart through their own detuned ratios.
        """
        if ch == "" or ("delay" + ch) in state:
            return
        state["delay" + ch] = state["delay"].copy()
        state["xf_rem" + ch] = np.zeros(V, dtype=np.int64)
        state["xf_len" + ch] = np.ones(V, dtype=np.int64)
        state["xf_off" + ch] = np.zeros(V, dtype=np.float64)
        state["seam_jumps" + ch] = np.zeros(V, dtype=np.int64)

    def _resampler_read_channel(
        self, state, ch, read_buf, ratio, head, L, span, guard, x_nom,
        frames, V,
    ):
        """Read one detune channel from the ring with seam-declick, using
        this channel's own read head (``state["delay"+ch]``) and crossfade
        state. ``ratio`` is the channel's (V, F) per-sample playback rate;
        ``read_buf`` the ring it samples (raw, or the anti-alias ring on an
        up-shift). Returns ``(wet (V, F) float64, new_delay (V,))``; the
        caller stores ``new_delay`` back under ``"delay"+ch``.
        """
        delay = state["delay" + ch]
        cum = np.cumsum(ratio, axis=-1)
        excum = cum - ratio                 # exclusive cumsum (offset per sample)
        offs = (L - delay)[:, np.newaxis] + excum
        sum_ratio = cum[:, -1]

        slow = bool(
            state["xf_rem" + ch].any()
            or (offs[:, 0] <= guard).any()
            or (offs.max(axis=-1) >= span - guard).any()
        )
        if not slow:
            # In-band fast path (see _render_resampler_core): the guard band
            # keeps the head clear of both edges, so the 4-tap Hermite read
            # never wraps; at an integer read position frac is exactly 0 and
            # Hermite returns the sample, so unity stays a bit-exact
            # delayed passthrough.
            phase = np.mod(offs, span)          # in [0, span)
            i0 = np.floor(phase).astype(np.int64)
            frac = phase - i0
            rows = np.arange(V)[:, np.newaxis]
            jm1 = (head + np.clip(i0 - 1, 0, L - 1)) % L
            j2 = (head + np.clip(i0 + 2, 0, L - 1)) % L
            from ..numpy_backend import _hermite4  # lazy: it imports this module
            wet = _hermite4(
                read_buf[rows, jm1],
                read_buf[rows, (head + i0) % L],
                read_buf[rows, (head + i0 + 1) % L],
                read_buf[rows, j2],
                frac,
            )
            # Carry the read head as a lag behind the (new) write head.
            new_delay = 1.0 + np.mod((delay + frames - sum_ratio) - 1.0, span)
        else:
            wet = np.empty((V, frames), dtype=np.float64)
            new_delay = np.empty(V, dtype=np.float64)
            for v in range(V):
                wet[v], new_delay[v] = self._resampler_voice_declick(
                    state, ch, v, read_buf[v], head, offs[v], ratio[v],
                    float(delay[v]), float(sum_ratio[v]),
                    frames, L, span, guard, x_nom,
                )
        return wet, new_delay

    def _resampler_voice_declick(
        self, state, ch, v, bufv, head, p, r, delay_v, sum_r,
        frames, L, span, guard, x_nom,
    ):
        """One voice's block with the read head inside the guard band (or
        a seam crossfade still in flight from the previous block).

        ``ch`` is the detune-channel suffix ("" centre, "_l"/"_r" the
        stereo spread) that keys this channel's own crossfade state.

        ``p`` is the voice's unwrapped read-position trajectory for this
        block (position in the window: 0 = oldest, ``span`` = newest) and
        ``r`` its per-sample ratio. Applies a half-span jump wherever the
        trajectory enters the guard band, records one equal-power
        crossfade per jump (old tap fading out, jumped tap fading in) and
        returns ``(float64 out, new end-of-block delay)``.

        Fade weights are a function of the sample index *within the
        fade*, so a fade that outlives the block is carried in ``state``
        and continues seamlessly next call. An old tap that runs out of
        valid content mid-fade (extreme ratios) is force-completed early
        -- by then its weight is already near zero. Seam events are
        always at least half a span of head travel apart, so fades never
        overlap; each jump also bumps ``seam_jumps[v]`` (an observable
        for tests and debugging).
        """
        half = 0.5 * span
        p = p.copy()
        fades = []          # (n0, n_here, t0, x_eff, back_off)
        jump_sum = 0.0      # net delay change from the jumps

        # Continue an in-flight fade from the previous block.
        rem = int(state["xf_rem" + ch][v])
        if rem > 0:
            x_eff = int(state["xf_len" + ch][v])
            off = float(state["xf_off" + ch][v])
            n_here = min(rem, frames)
            fades.append([0, n_here, x_eff - rem, x_eff, off])

        # Low edge: the head is on nearly-overwritten (oldest) content --
        # the pitch-down collision. Jump forward half a span; the old
        # tap, half a span behind the jumped head, fades out.
        if p[0] <= guard:
            p += half
            jump_sum -= half
            fades_adj = [f for f in fades if f[0] == 0]
            for f in fades_adj:
                # A carried fade's back tap rides the canonical
                # trajectory; keep it in place across the new jump.
                f[4] -= half
            fades.append([0, min(x_nom, frames), 0, x_nom, -half])
            state["seam_jumps" + ch][v] += 1

        # High edge: the head is about to collide with the write head --
        # the pitch-up collision. Jump back half a span at the first
        # offending sample (can recur in one block at extreme ratios).
        # The old tap runs on toward the newest sample, so the fade
        # shortens to the runway left at this ratio.
        while True:
            hits = np.nonzero(p >= span - guard)[0]
            if hits.size == 0:
                break
            n0 = int(hits[0])
            rmax = float(r[n0:n0 + x_nom].max())
            runway = (span - 1.0) - float(p[n0])
            x_eff = int(min(x_nom, max(1.0, runway / max(rmax, 1e-9))))
            p[n0:] -= half
            jump_sum += half
            fades.append([n0, min(x_eff, frames - n0), 0, x_eff, half])
            state["seam_jumps" + ch][v] += 1

        # Jumped main tap, 4-tap Hermite (same read as the fast path;
        # p is inside (0, span) by construction after the jumps, so the
        # outer taps only clamp right at the window ends).
        i0 = np.floor(p).astype(np.int64)
        frac = p - i0
        from ..numpy_backend import _hermite4  # lazy: it imports this module
        out = _hermite4(
            bufv[(head + np.clip(i0 - 1, 0, L - 1)) % L],
            bufv[(head + i0) % L],
            bufv[(head + i0 + 1) % L],
            bufv[(head + np.clip(i0 + 2, 0, L - 1)) % L],
            frac,
        )

        # Blend each fade's old tap back in, equal-power. A back tap that
        # has left the valid window keeps the new tap at full weight.
        state["xf_rem" + ch][v] = 0
        for n0, n_here, t0, x_eff, off in fades:
            if n_here <= 0:
                continue
            t = (t0 + np.arange(n_here, dtype=np.float64) + 1.0) / x_eff
            t = np.minimum(t, 1.0)
            pb = p[n0:n0 + n_here] + off
            valid = (pb >= 0.0) & (pb <= span - 1.0)
            pbc = np.clip(pb, 0.0, span - 1.0)
            ib = np.floor(pbc).astype(np.int64)
            fb = pbc - ib
            back = _hermite4(
                bufv[(head + np.clip(ib - 1, 0, L - 1)) % L],
                bufv[(head + ib) % L],
                bufv[(head + ib + 1) % L],
                bufv[(head + np.clip(ib + 2, 0, L - 1)) % L],
                fb,
            )
            w_old = np.where(valid, np.cos(0.5 * np.pi * t), 0.0)
            g_new = np.where(valid, np.sin(0.5 * np.pi * t), 1.0)
            seg = slice(n0, n0 + n_here)
            out[seg] = g_new * out[seg] + w_old * back
            if t0 + n_here < x_eff and n0 + n_here >= frames:
                # The fade outlives the block -- carry the remainder.
                state["xf_rem" + ch][v] = x_eff - (t0 + n_here)
                state["xf_len" + ch][v] = x_eff
                state["xf_off" + ch][v] = off

        new_delay = delay_v + frames - sum_r + jump_sum
        new_delay = 1.0 + float(np.mod(new_delay - 1.0, span))
        return out, new_delay

    # ----- PitchShifter rendering -----------------------------------------

    # Clamp the effective transpose so the playback ratio (and the stretch
    # ring sized from it) stays bounded. +/-36 st -> ratio in [1/8, 8].
    _PS_MAX_ST = 36.0
    # Shimmer loop: feedback is clamped here (a bounded loop, like the
    # freq_shifter's), and the recirculated block is damped by a one-pole
    # lowpass at this corner before it re-enters -- every lap climbs an
    # octave, so undamped laps sail past Nyquist and come back as alias
    # grit; damping makes the climb die against the top of hearing
    # instead. Then a soft ceiling (the matrix's knee) so a hot loop
    # rounds off rather than runs away.
    _PS_FB_MAX = 0.9
    _PS_FB_LP_HZ = 6000.0
    # Formant preservation (LPC whiten -> shift residual -> re-color).
    _PS_LPC_ORDER = 24          # envelope detail; ~speech-codec order at 44.1k
    _PS_LPC_WIN = 1024          # raw samples per envelope estimate
    # Pitch-synchronous deep-bass grain sizing: re-estimate the input
    # period this often, and grow the effective grain so it always holds
    # at least _PS_SYNC_PERIODS periods (capped; user grain is the floor).
    _PS_DETECT_EVERY = 2048
    _PS_DETECT_WIN = 4096
    _PS_SYNC_PERIODS = 2.5
    _PS_MAX_GRAIN_SEC = 0.15
    _PS_FMIN = 25.0

    def _render_pitch_shifter(self, module, frames, buffers, patch):
        """Granular WSOLA pitch shifter (time-preserving). Shape-polymorphic.

        Branches on the audio input's ndim: 1D -> one grain engine; 2D
        ``(V, F)`` -> V independent engines (one per voice slot). A mono
        ``pitch_cv`` broadcasts across voices; a ``(V, F)`` ``pitch_cv``
        drives each voice. Pitch CV is sampled per block (block-rate),
        summed in semitone space. Missing audio in -> silence.
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None:
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}
        pitch_cv = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        if src.ndim == 2:
            V = src.shape[0]
            if pitch_cv is None:
                cv = None
            elif pitch_cv.ndim == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[0]))
            elif pitch_cv.shape[0] == V:
                cv = pitch_cv
            elif pitch_cv.shape[0] == 1:
                cv = np.broadcast_to(pitch_cv, (V, pitch_cv.shape[1]))
            else:
                cv = np.broadcast_to(self._voice_mean(pitch_cv), (V, pitch_cv.shape[1]))
            return self._pitch_shifter_core(module, frames, src, cv)

        if pitch_cv is not None and pitch_cv.ndim == 2:
            pitch_cv = self._voice_mean(pitch_cv)
        outs = self._pitch_shifter_core(
            module,
            frames,
            src[np.newaxis, :],
            None if pitch_cv is None else pitch_cv[np.newaxis, :],
        )
        return {k: a[0] for k, a in outs.items()}

    def _pitch_shifter_core(self, module, frames, src, cv):
        """Shared ``(V, F)`` engine; mono runs with V=1 (bit-identical).

        Returns ``{"out", "out_l", "out_r"}``, each ``(V, F)``.

        Per voice and per block: (1) every ``_PS_DETECT_EVERY`` input
        samples the input period is re-estimated and, when the current
        grain holds fewer than ``_PS_SYNC_PERIODS`` periods (deep bass),
        the engine is rebuilt with a longer grain -- primed from the old
        engine's ring and equal-power crossfaded in over one block, with
        20% hysteresis so it never thrashes; (2) with
        ``formant_preserve`` on, an LPC envelope is estimated from the
        raw history, the input is whitened through ``A(z)``, the grain
        engine shifts the residual, and the output is re-colored through
        ``1/A(z)`` using the coefficient set from ~one grain ago (so the
        envelope rides with the content it described). Coefficient sets
        change per block with lfilter zf-carry -- the standard adaptive-
        filter compromise, smooth because envelopes evolve slowly.

        2026-09-14 love pass, all three OFF at their defaults so the
        shipped render is untouched:

        * ``feedback`` (shimmer): the previous block's main wet -- damped
          by a one-pole at ``_PS_FB_LP_HZ``, soft-ceilinged at the
          matrix knee -- is added to this block's engine input, so every
          lap is shifted again (at +12, an octave cascade). The loop is
          one block plus the engine's own latency long, so laps arrive
          a grain or two apart: a fast bloom, not a slow reverb-shimmer
          (for that, patch this into a delay in a matrix loop -- see
          ``organ_shimmer.json``). The dry tap and the LPC estimate read
          the RAW input via the ``db`` ring, never the recirculated one.
        * ``harmony`` / ``harmony_level``: a second engine per voice at
          ``harmony`` semitones (plus the same ``pitch_cv``, so the chord
          transposes together), fed the raw input -- NOT the feedback --
          and mixed at ``harmony_level``. Only built when the level is
          above zero; it shares the whitening and re-colours through its
          own synthesis state.
        * ``spread``: with a harmony present, pans the main wet toward
          ``out_l`` and the harmony toward ``out_r`` (the far channel
          fades by ``spread``); the dry stays centred. Without a
          harmony, or at 0, ``out_l`` and ``out_r`` are ``out``.
        """
        V = src.shape[0]
        sr = self.sample_rate
        semis = float(module.params.get("semitones", 0.0))
        cents = float(module.params.get("cents", 0.0))
        cv_depth = float(module.params.get("cv_depth", 12.0))
        mix = float(np.clip(float(module.params.get("mix", 1.0)), 0.0, 1.0))
        grain_ms = float(module.params.get("grain_size", 50.0))
        overlap = max(2, min(4, int(module.params.get("overlap", 2))))
        formant = bool(module.params.get("formant_preserve", False))
        Lg = max(8, int(round(grain_ms * 1e-3 * sr)))

        def _f(name, lo, hi, default):
            try:
                return float(np.clip(float(module.params.get(name, default)), lo, hi))
            except (TypeError, ValueError):
                return default

        feedback = _f("feedback", 0.0, self._PS_FB_MAX, 0.0)
        harmony_st = _f("harmony", -24.0, 24.0, 0.0)
        harmony_level = _f("harmony_level", 0.0, 1.0, 0.0)
        spread = _f("spread", 0.0, 1.0, 0.0)
        use_harm = harmony_level > 0.0

        from ..numpy_backend import _GrainShifter, _lpc_coeffs  # lazy: it imports this module
        head = max(16384, 16 * int(getattr(self, "block_size", 512)))
        state = self._state.setdefault(module.id, {})
        if state.get("V") != V or state.get("Lg") != Lg or state.get("ov") != overlap:
            state.clear()
            state["V"] = V
            state["Lg"] = Lg
            state["ov"] = overlap
            state["eng"] = [_GrainShifter(Lg, overlap, head) for _ in range(V)]
            state["last_det"] = [0] * V
            state["regrains"] = np.zeros(V, dtype=np.int64)
            state["lpc_zi_w"] = [None] * V     # whitening FIR state
            state["lpc_zi_s"] = [None] * V     # synthesis IIR state
            state["lpc_fifo"] = [[] for _ in range(V)]
            state["fb"] = [None] * V           # last block's main wet (shimmer)
            state["fb_lp"] = [0.0] * V         # one-pole state on the loop
        if use_harm and "eng2" not in state:
            # The harmony engine is built lazily, the first block the
            # level is above zero, so a patch that never turns it on
            # never pays for it.
            state["eng2"] = [_GrainShifter(Lg, overlap, head) for _ in range(V)]
            state["last_det2"] = [0] * V
            state["regrains2"] = np.zeros(V, dtype=np.int64)
            state["lpc_zi_s2"] = [None] * V
        elif not use_harm and "eng2" in state:
            for key in ("eng2", "last_det2", "regrains2", "lpc_zi_s2"):
                state.pop(key, None)

        if frames == 0:
            z = np.empty((V, 0), dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}

        # Feedback-path damping: one-pole lowpass coefficient.
        fb_a = 0.0
        if feedback > 0.0:
            fb_a = float(np.exp(-2.0 * np.pi * self._PS_FB_LP_HZ / float(sr)))

        base_st = semis + cents / 100.0
        out = np.empty((V, frames), dtype=np.float32)
        out_l = out
        out_r = out
        if use_harm and spread > 0.0:
            out_l = np.empty((V, frames), dtype=np.float32)
            out_r = np.empty((V, frames), dtype=np.float32)
        for v in range(V):
            cv_st = 0.0 if cv is None else cv_depth * self._finite_mean(cv[v])
            st = base_st if cv is None else base_st + cv_st
            # +/-_PS_MAX_ST, clipped in octave space by the shared door
            # (36 st is exactly 3.0 oct, so the rail lands where the
            # semitone clamp put it); a NaN mean reads as no shift.
            r = self._pow2_clipped(st / 12.0, self._PS_MAX_ST / 12.0)
            x = src[v].astype(np.float64)

            # --- shimmer: recirculate last block's main wet ---
            x_in = x
            if feedback > 0.0:
                prev = state["fb"][v]
                if prev is not None and prev.shape[0] != frames:
                    # Block size moved under us: pad or trim, once.
                    fix = np.zeros(frames)
                    n = min(frames, prev.shape[0])
                    fix[:n] = prev[:n]
                    prev = fix
                if prev is not None:
                    damped, zf = lfilter(
                        [1.0 - fb_a], [1.0, -fb_a], prev, zi=[state["fb_lp"][v]]
                    )
                    state["fb_lp"][v] = float(zf[0])
                    x_in = x + feedback * self._ps_soft_ceiling(damped)
            # The raw input rides the ``db`` ring whenever the engine is
            # fed something else (whitened, or with the loop mixed in),
            # so the dry tap and the LPC estimate stay true.
            x_dry = x if (formant or feedback > 0.0) else None

            eng = state["eng"][v]
            # --- formant preserve: whiten the engine input ---
            a_cur = None
            xw = x_in
            if formant:
                raw_tail = eng.history(self._PS_LPC_WIN, dry=True) if eng.db is not None \
                    else eng.history(self._PS_LPC_WIN)
                a_cur = _lpc_coeffs(raw_tail, self._PS_LPC_ORDER, sr)
                if a_cur is not None:
                    zi = state["lpc_zi_w"][v]
                    if zi is None:
                        zi = np.zeros(self._PS_LPC_ORDER)
                    xw, zf = lfilter(a_cur, [1.0], x_in, zi=zi)
                    state["lpc_zi_w"][v] = zf

            wet, eng, was_primed = self._ps_shift(
                state, "", v, xw, x_dry, r, frames, Lg, overlap, head, sr
            )

            # --- formant preserve: re-color with the envelope from ~one
            # grain ago (aligns the envelope with the content it described).
            a_del = None
            if formant:
                fifo = state["lpc_fifo"][v]
                # Envelopes estimated before the wet path primed describe
                # the onset transient, not streaming content -- feeding
                # them to the synthesis filter blasts the first wet block.
                fifo.append(a_cur if was_primed else None)
                lag_blocks = max(0, int(round(eng.Lg / float(frames))))
                while len(fifo) > lag_blocks + 1:
                    fifo.pop(0)
                a_del = fifo[0]
                if a_del is not None:
                    wet = self._ps_recolor(state, "lpc_zi_s", v, wet, a_del, x)

            if feedback > 0.0:
                state["fb"][v] = wet

            # --- harmonizer: a second engine on the raw (whitened) input ---
            harm = None
            if use_harm:
                st2 = harmony_st + cv_st
                r2 = self._pow2_clipped(st2 / 12.0, self._PS_MAX_ST / 12.0)
                xw2 = xw
                if feedback > 0.0:
                    # The harmony hears the raw input, not the loop.
                    xw2 = x
                    if formant and a_cur is not None:
                        # Whiten the raw input through the same envelope;
                        # the loop's whitening state belongs to the main.
                        zi2 = state.setdefault("lpc_zi_w2", [None] * V)[v]
                        if zi2 is None:
                            zi2 = np.zeros(self._PS_LPC_ORDER)
                        xw2, zf2 = lfilter(a_cur, [1.0], x, zi=zi2)
                        state["lpc_zi_w2"][v] = zf2
                harm, eng2, _wp2 = self._ps_shift(
                    state, "2", v, xw2, x if formant else None, r2, frames,
                    Lg, overlap, head, sr,
                )
                if formant and a_del is not None:
                    harm = self._ps_recolor(state, "lpc_zi_s2", v, harm, a_del, x)
                harm = harmony_level * harm

            Dc = eng.latency(r)  # exact wet latency -> phase-coherent dry/wet mix
            wet_sum = wet if harm is None else wet + harm
            if mix >= 1.0:
                blk = wet_sum
                dry = None
            elif mix <= 0.0:
                blk = eng.dry_tap(frames, Dc)
                dry = blk
            else:
                dry = (1.0 - mix) * eng.dry_tap(frames, Dc)
                blk = dry + mix * wet_sum
            out[v] = blk.astype(np.float32)
            if harm is not None and spread > 0.0:
                # Main leans left, harmony leans right: the far channel
                # fades by ``spread``; the dry stays put in both.
                far = 1.0 - spread
                lw = wet + far * harm
                rw = far * wet + harm
                if mix >= 1.0:
                    out_l[v] = lw.astype(np.float32)
                    out_r[v] = rw.astype(np.float32)
                elif mix <= 0.0:
                    out_l[v] = out[v]
                    out_r[v] = out[v]
                else:
                    out_l[v] = (dry + mix * lw).astype(np.float32)
                    out_r[v] = (dry + mix * rw).astype(np.float32)
        return {"out": out, "out_l": out_l, "out_r": out_r}

    def _ps_shift(self, state, sfx, v, xw, x_dry, r, frames, Lg, overlap, head, sr):
        """One engine slot for one block: the deep-bass regrain check,
        the shift itself, and the equal-power splice when the engine was
        just rebuilt. ``sfx`` picks the state keys ("" main, "2"
        harmony). Returns ``(wet, engine, was_primed)``."""
        engines = state["eng" + sfx]
        last_det = state["last_det" + sfx]
        eng = engines[v]
        old_eng = None
        if eng.iw - last_det[v] >= self._PS_DETECT_EVERY:
            last_det[v] = eng.iw
            # lazy: numpy_backend imports this module
            from ..numpy_backend import _detect_period, _GrainShifter
            tail = eng.history(self._PS_DETECT_WIN)
            period = _detect_period(tail, sr, fmin=self._PS_FMIN)
            want = Lg
            if period is not None:
                want = max(Lg, int(round(self._PS_SYNC_PERIODS * period)))
            want = min(want, int(self._PS_MAX_GRAIN_SEC * sr))
            cur = eng.Lg
            if want > cur * 1.2 or (cur > Lg and want < cur * 0.8):
                new_eng = _GrainShifter(want, overlap, head)
                hist = eng.history(new_eng.Lin - frames - 8)
                hist_dry = None
                if eng.db is not None:
                    hist_dry = eng.history(hist.shape[0], dry=True)
                if hist.shape[0]:
                    new_eng.process(hist, r, x_dry=hist_dry)  # prime; output discarded
                old_eng = eng
                eng = new_eng
                engines[v] = eng
                state["regrains" + sfx][v] += 1

        was_primed = eng.primed
        wet = eng.process(xw, r, x_dry=x_dry)
        if old_eng is not None:
            # Equal-power splice from the outgoing engine's output.
            wet_old = old_eng.process(
                xw, r, x_dry=x_dry if old_eng.db is not None else None
            )
            t = (np.arange(frames) + 1.0) / frames
            wet = np.sin(0.5 * np.pi * t) * wet + np.cos(0.5 * np.pi * t) * wet_old
        return wet, eng, was_primed

    def _ps_recolor(self, state, key, v, wet, a_del, x):
        """Formant re-colour through ``1/A(z)`` with carried state under
        ``state[key][v]``, plus the level safety valve: a recolored block
        should sit near the raw input's level (whiten -> shift ->
        re-color is level-preserving by construction), but an
        ill-conditioned estimate (attack edges, near-silence) can't be
        ruled out, so bound the block at 4x the raw RMS."""
        zi = state[key][v]
        if zi is None:
            zi = np.zeros(self._PS_LPC_ORDER)
        wet, zf = lfilter([1.0], a_del, wet, zi=zi)
        state[key][v] = zf
        raw_rms = float(np.sqrt((x ** 2).mean()))
        rec_rms = float(np.sqrt((wet ** 2).mean()))
        lim = 4.0 * max(raw_rms, 1e-4)
        if rec_rms > lim:
            wet = wet * (lim / rec_rms)
        return wet

    @staticmethod
    def _ps_soft_ceiling(x):
        """The matrix mixer's soft ceiling: identity below the knee, a
        tanh round-off above it, so a hot shimmer loop bends rather than
        blows up."""
        from ...modules.matrix_mixer import MATRIX_CLIP_KNEE
        knee = MATRIX_CLIP_KNEE
        mag = np.abs(x)
        over = mag > knee
        if not over.any():
            return x
        limited = knee + (1.0 - knee) * np.tanh((mag - knee) / (1.0 - knee))
        return np.where(over, np.sign(x) * limited, x)

    # ----- Tape rendering --------------------------------------------------

    # Fixed nominal delay of the tape head gap (ms). Wow/flutter/drift sway
    # the *read* around this centre; the dry path is delayed by the same
    # amount (plus any oversampler latency) so ``mix`` stays phase-coherent
    # instead of combing.
    _TAPE_NOMINAL_MS = 10.0
    # Longest delay the line can address (ms) -> sizes the ring and caps the
    # modulated read (nominal + the summed wow/flutter/drift throw, with room).
    _TAPE_MAX_MS = 24.0
    _TAPE_MIN_SAMP = 2.0            # read floor: both interp taps stay behind write
    # Peak modulation throw (ms) at full depth, per source.
    _TAPE_WOW_MS = 2.5
    _TAPE_FLUT_MS = 0.5
    _TAPE_DRIFT_MS = 3.0
    # Nominal modulation rates (Hz).
    _TAPE_WOW_HZ = 1.0
    _TAPE_FLUT_HZ = 9.0
    _TAPE_DRIFT_HZ = 0.35          # one-pole corner of the drift random-walk
    _TAPE_FLUT_LP_HZ = 18.0        # one-pole corner shaping the flutter noise
    _TAPE_FLUT_NOISE = 0.35        # noise fraction of the flutter modulation
    # Saturation drive at ``sat == 1`` (tanh on the shared 4x OS path).
    _TAPE_SAT_DRIVE_MAX = 6.0
    # Head-bump low shelf.
    _TAPE_BUMP_HZ = 60.0
    _TAPE_BUMP_MAX_DB = 6.0
    # ``hiss`` (dB): at/below OFF it is disabled; otherwise up to MAX.
    _TAPE_HISS_OFF_DB = -80.0
    _TAPE_HISS_MAX_DB = -30.0
    # Seeded, reproducible noise streams (offset by module.id). drift, flutter
    # and hiss each get their OWN generator, so every stream is an independent
    # 1:1 draw per output sample -> exactly block-size independent.
    _TAPE_DRIFT_SEED = 0x7A9E0
    _TAPE_FLUT_SEED = 0x7A9E1
    _TAPE_HISS_SEED = 0x7A9E2
    # The tape-stop transport: ``stop_time`` / ``start_time`` (s) are the
    # motor's coast-down and spin-up, clamped to this range. The ring grows
    # by half their sum when ``stop`` is patched (the lag a full cycle
    # leaves the head behind live, see ``_tape_stop_transport``).
    _TAPE_STOP_MIN_S = 0.05
    _TAPE_STOP_MAX_S = 8.0

    @classmethod
    def _tape_stop_transport(cls, gt, st: dict, stop_n: int, start_n: int,
                             cap: float):
        """The tape-stop transport: per sample, the head's ``env`` (0 = full
        speed, 1 = stationary) and its ``lag`` behind the live write head,
        from the ``stop`` gate row ``gt`` (bools). Mutates ``st``.

        ``env`` is ``_gate_ramp_env`` verbatim -- an integer-count linear
        ramp 0 -> 1 over ``stop_n`` samples from a rising edge (a coasting
        motor: constant deceleration, so the pitch falls linearly), 1 -> 0
        over ``start_n`` from a falling edge, a new edge mid-ramp
        re-articulating from the CURRENT level -- so the speed ``1 - env``
        lands on the same sample at any block size. The lag is the physics:
        a head reading at speed ``1 - env`` falls behind the live write by
        ``env`` per sample, so ``lag`` is the running sum of ``env``. It is
        carried across blocks by PREPENDING the carry to the block's
        ``np.cumsum`` -- ``cumsum([carry, e...])[1:]`` is one long cumsum
        bit for bit, where ``carry + cumsum(e)`` is not (it regroups the
        additions and the fractions are not dyadic). A falling edge that
        finds the head stationary (``env == 1.0`` at the sample before)
        resets the lag to 0: the head is silent there, so the jump back to
        live is inaudible -- and it is what keeps the lag bounded, ``(start_n
        - 1) / 2`` after every full stop instead of accumulating (a real
        deck loses that time too). A release before the halt keeps the lag
        (the head is still audible), so repeated partial stops do
        accumulate; the lag is clamped to ``cap`` (the ring's spare length)
        per sample, which also stops it growing a sample per sample while
        stationary. The clamp commutes with the running sum (``env >= 0``,
        so the sum is monotone) and the carry stays unclamped, so the fold
        is the same at any block size.
        """
        frames = len(gt)
        prev = bool(st["stop_prev"])
        env, on_c, off_c, env_off = cls._gate_ramp_env(
            gt, prev, int(st["stop_on"]), int(st["stop_off"]),
            float(st["stop_env_off"]), stop_n, start_n)
        # Falling edges that find the head stationary restart the lag.
        falls = np.flatnonzero(np.concatenate(([prev], gt[:-1])) & ~gt)
        before = np.where(falls > 0, env[np.maximum(falls - 1, 0)],
                          float(st["stop_env_last"]))
        resets = falls[before >= 1.0].tolist()
        lag = np.empty(frames, dtype=np.float64)
        carry = float(st["stop_lag"])
        pos = 0
        for r in resets + [frames]:
            if r > pos:
                seg = np.cumsum(np.concatenate(([carry], env[pos:r])))[1:]
                lag[pos:r] = seg
                carry = float(seg[-1])
            if r < frames:
                carry = 0.0
            pos = r
        st["stop_prev"] = bool(gt[-1])
        st["stop_on"] = on_c
        st["stop_off"] = off_c
        st["stop_env_off"] = env_off
        st["stop_env_last"] = float(env[-1])
        st["stop_lag"] = carry
        np.minimum(lag, cap, out=lag)
        return env, lag

    def _render_tape(self, module, frames: int, buffers, patch):
        """Tape character: wow/flutter/drift + saturation + hiss + head bump.

        Signal flow ``in -> wow/flutter/drift-modulated fractional delay ->
        saturation -> + hiss -> head-bump low shelf -> mix with the
        latency-matched dry``. The delay line reuses the chorus core (write
        the whole block, then read fractional taps behind the write head);
        with no feedback every read references an already-written sample, so
        the whole render vectorises and is exactly block-size independent:
        bit for bit at 64, 128, 512 or 1000 over four seconds with every
        flavour on. That exactness is three deliberate choices, each
        guarding a place where a partition would otherwise change a
        rounding -- the read splits the delay into whole samples and a
        fraction instead of forming ``absidx - delay`` (**a ring index
        rounds at its own magnitude**), the wow/flutter sines read an
        absolute sample INDEX rather than a carried float phase, and the
        oversampler's FIR carries raw tail samples rather than an
        ``lfilter`` ``zi``. The drift, flutter noise and hiss are each a
        *single* seeded generator drawn one sample per output sample and
        streamed through one-pole/biquad filters with carried ``zi`` -- so
        every stochastic path is block-size independent too, at any
        partition. One tape path is modelled: the modulation and hiss are shared
        across a polyphonic input's voices (each voice keeps its own delay
        line, oversampler and shelf state, so they never cross-talk), and a
        single voice row is bit-identical to the mono render.

        Neutral (``wow = flutter = drift = sat = bump = 0`` and ``hiss``
        off) short-circuits to a bit-exact passthrough with no state
        advance; ``mix <= 0`` is likewise bit-exact dry.

        ``stop`` (gate): the tape-stop. A ``(V, F)`` gate collapses to the
        house sum (any voice high stops the one transport every voice's ring
        shares). While the gate is patched the ring records live and the
        transport (``_tape_stop_transport``) yields a per-sample speed
        ``1 - env`` and a ``lag``; the read sits ``lag`` samples further
        behind the write head (a moving lag is the pitch dive / climb) and
        the wet -- tap and hiss -- is scaled by the speed (a head's EMF is
        proportional to tape speed: linear, so the stop fades to exact
        silence and the restart fades in, per sample, click-free). The dry
        never sees the gate: after a stop the wet runs the restart's lag
        behind it. The ring is allocated ``(stop_n + start_n) / 2`` longer
        only when ``stop`` is patched (grown, never shrunk, as the times
        rise). Neutral with ``stop`` patched: no nominal delay, no clip
        floor -- until a stop the head reads the sample it just wrote and
        the block short-circuits to ``src`` bit-exactly (the ring still
        records, so the stop has material); once a stop has happened the wet
        is a fractional read ``lag`` behind live and no longer the
        passthrough. Unpatched, or patched and never risen, every path
        below is the pre-stop code verbatim; ``mix <= 0`` keeps its early
        out, so the transport idles there (an edge that lands while the mix
        is dry is seen when it comes back).
        """
        src = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        if src is None or src.size == 0:
            return np.zeros(frames, dtype=np.float32)
        stop_gate = self._input_buffer(patch, buffers, module.id, "stop")
        stop_patched = stop_gate is not None and stop_gate.shape[-1] == frames

        wow = min(max(float(module.params.get("wow", 0.0)), 0.0), 1.0)
        flutter = min(max(float(module.params.get("flutter", 0.0)), 0.0), 1.0)
        drift = min(max(float(module.params.get("drift", 0.0)), 0.0), 1.0)
        sat = min(max(float(module.params.get("sat", 0.0)), 0.0), 1.0)
        hiss_db = float(module.params.get("hiss", self._TAPE_HISS_OFF_DB))
        bump_db = min(max(float(module.params.get("bump", 0.0)), 0.0),
                      self._TAPE_BUMP_MAX_DB)
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)

        sat_active = sat > 0.0
        hiss_active = hiss_db > self._TAPE_HISS_OFF_DB
        mod_active = wow > 0.0 or flutter > 0.0 or drift > 0.0
        bump_active = bump_db > 0.0

        neutral = not (mod_active or sat_active or hiss_active or bump_active)

        # Bit-exact dry: the blend keeps nothing wet, or the whole box is
        # neutral (a freshly added Tape is transparent). No state advance.
        # A patched ``stop`` keeps a neutral box recording (below).
        if mix <= 0.0:
            return src
        if neutral and not stop_patched:
            return src

        was_mono = src.ndim == 1
        x = np.atleast_2d(src).astype(np.float64)          # (V, F)
        v = x.shape[0]
        sr = self.sample_rate

        # Neutral + stop: no nominal delay -- the head reads what it just
        # wrote, so the untouched output is the input itself.
        D = 0 if neutral else int(round(self._TAPE_NOMINAL_MS * sr / 1000.0))
        L_mod = int(self._TAPE_MAX_MS * sr / 1000.0) + frames + 4
        L = L_mod
        from ..numpy_backend import _OS_LATENCY, _Oversampler4  # lazy: it imports this module
        comp = D + (_OS_LATENCY if sat_active else 0)      # dry latency comp
        stop_n = start_n = 0
        lag_cap = 0.0
        if stop_patched:
            t_stop = min(max(float(module.params.get("stop_time", 1.0)),
                             self._TAPE_STOP_MIN_S), self._TAPE_STOP_MAX_S)
            t_start = min(max(float(module.params.get("start_time", 0.5)),
                              self._TAPE_STOP_MIN_S), self._TAPE_STOP_MAX_S)
            stop_n = max(1, int(round(t_stop * sr)))
            start_n = max(1, int(round(t_start * sr)))
            # The lag at the halt of a full cycle is exactly (stop_n +
            # start_n) / 2 samples (the previous restart's (start_n - 1) / 2
            # plus the stop's (stop_n + 1) / 2); the ring holds that plus
            # the interpolation pair.
            lag_cap = float(math.ceil((stop_n + start_n) / 2.0))
            L = L_mod + int(lag_cap) + 2

        state = self._state.setdefault(module.id, {})
        buf = state.get("buf")
        if stop_patched and buf is not None and buf.shape[0] == v and buf.shape[1] > L:
            L = buf.shape[1]                                # grown earlier: keep it
        if (buf is None or buf.shape != (v, L) or state.get("comp") != comp):
            state.clear()
            state["buf"] = np.zeros((v, L), dtype=np.float64)
            state["write_idx"] = 0
            # The wow/flutter sines are keyed to an absolute sample count,
            # not a carried float phase: ``ph + frames * inc`` accumulates
            # a different rounding for every partition of the same stream,
            # so a carried phase is block-size dependent by construction.
            # An integer count is not (and both rates are constants here,
            # so there is no rate change to keep continuous).
            state["mod_idx"] = 0
            state["flut_zi"] = np.zeros(1)
            state["drift_zi"] = np.zeros(1)
            state["shelf"] = {}
            state["dry_tail"] = np.zeros((v, comp))
            state["comp"] = comp
            state["os4"] = _Oversampler4(v)
            mid = int(module.id)
            state["rng_drift"] = np.random.default_rng(self._TAPE_DRIFT_SEED + mid)
            state["rng_flut"] = np.random.default_rng(self._TAPE_FLUT_SEED + mid)
            state["rng_hiss"] = np.random.default_rng(self._TAPE_HISS_SEED + mid)
            # stop transport: gate at the last sample, the ramp counts since
            # the last edge, the level at the last falling edge, the env at
            # the last sample and the (unclamped) lag carry.
            state["stop_prev"] = False
            state["stop_on"] = 0
            state["stop_off"] = 0
            state["stop_env_off"] = 0.0
            state["stop_env_last"] = 0.0
            state["stop_lag"] = 0.0

        if frames == 0:
            e = np.empty((v, 0), dtype=np.float32)
            return e[0] if was_mono else e

        buf = state["buf"]
        wp = int(state["write_idx"])
        n = np.arange(frames, dtype=np.float64)

        # --- the stop transport (one per module: every voice's ring shares
        # it). ``env`` 0 = full speed .. 1 = stationary; ``lag`` samples
        # the head trails the live write. Engaged only once a stop has
        # happened (any lag): before that every path below runs the
        # pre-stop code verbatim, so a patched-but-low gate is bit-exact.
        stop_env = stop_lag = None
        if stop_patched:
            gt = stop_gate > self._GATE_HIGH
            env_row, lag_row = self._tape_stop_transport(
                gt, state, stop_n, start_n, lag_cap)
            if np.any(lag_row):
                stop_env, stop_lag = env_row, lag_row

        # --- modulation (shared across voices: one tape path) -------------
        # Both sines read an absolute sample index, never a carried phase:
        # ``ph + frames * inc`` rounds once per block, so the same stream
        # cut into 64s and into 512s accumulates a different phase and the
        # two renders drift apart by a float64 ulp within a second. The
        # index is an exact integer, so ``idx * inc`` rounds once, the same
        # way, at any block size. (Both rates are module constants -- there
        # is no rate change here that a phase carry would have to keep
        # continuous, which is why the plain index is enough.)
        mi = int(state["mod_idx"])
        nn = mi + n                                        # (F,) exact ints
        state["mod_idx"] = mi + frames

        # wow: slow sine.
        wow_inc = self._TAPE_WOW_HZ / sr
        wow_lfo = np.sin(2.0 * np.pi * ((nn * wow_inc) % 1.0))

        # flutter: fast sine + a little low-passed (band-limited) noise.
        flut_inc = self._TAPE_FLUT_HZ / sr
        flut_lfo = np.sin(2.0 * np.pi * ((nn * flut_inc) % 1.0))
        fn = state["rng_flut"].standard_normal(frames)
        kf = 1.0 - math.exp(-2.0 * math.pi * self._TAPE_FLUT_LP_HZ / sr)
        fn, state["flut_zi"] = lfilter([kf], [1.0, kf - 1.0], fn, zi=state["flut_zi"])
        fn = fn * math.sqrt((2.0 - kf) / kf)               # -> ~unit std
        flut_sig = ((1.0 - self._TAPE_FLUT_NOISE) * flut_lfo
                    + self._TAPE_FLUT_NOISE * fn)

        # drift: slow random walk = heavily low-passed white noise, unit-ish
        # std, hard-bounded so the read can never cross the write head.
        dn = state["rng_drift"].standard_normal(frames)
        kd = 1.0 - math.exp(-2.0 * math.pi * self._TAPE_DRIFT_HZ / sr)
        dn, state["drift_zi"] = lfilter([kd], [1.0, kd - 1.0], dn, zi=state["drift_zi"])
        drift_sig = np.clip(dn * math.sqrt((2.0 - kd) / kd), -1.0, 1.0)

        wow_s = self._TAPE_WOW_MS * sr / 1000.0
        flut_s = self._TAPE_FLUT_MS * sr / 1000.0
        drift_s = self._TAPE_DRIFT_MS * sr / 1000.0
        m = (wow * wow_s * wow_lfo
             + flutter * flut_s * flut_sig
             + drift * drift_s * drift_sig)                # (F,) samples

        # --- fractional-delay read (chorus core; no feedback) -------------
        absidx = wp + np.arange(frames)
        buf[:, absidx % L] = x
        state["write_idx"] = int((wp + frames) % L)
        if neutral:
            # Only a patched ``stop`` gets here. Until it has stopped once
            # the head reads the sample it just wrote: the ring records and
            # the block is the input, bit for bit.
            if stop_lag is None:
                return src
            delay = np.zeros(frames, dtype=np.float64)
        else:
            delay = D + m
            np.clip(delay, self._TAPE_MIN_SAMP, float(L_mod - 2), out=delay)
        # THE TRAP: a ring index rounds at its own magnitude. ``absidx -
        # delay`` looks exact and is not -- the ring is ``max_ms + frames``
        # long, so it wraps at a different absolute sample for every block
        # size, the index reaching the subtraction carries a different
        # magnitude, and the fraction that falls out is a float64 ulp
        # apart. One ulp there flips a float32 tie at the odd sample.
        # So never form ``index - delay``: split the delay into WHOLE
        # SAMPLES and a FRACTION, both functions of the (small) delay
        # alone. ``back = ceil(delay)`` and ``frac = back - delay`` are
        # identical at any block size -- and the subtraction is exact
        # (Sterbenz) for every delay the clip above allows. This is the
        # pattern the stop's read already used; it is the ordinary read's
        # now too.
        back = np.ceil(delay)
        frac = back - delay                # forward weight, in [0, 1)
        back = back.astype(np.int64)       # whole samples behind the write
        if stop_lag is not None:
            # The head trails live by the transport's lag; a lag that grows
            # by ``env`` per sample is a read advancing at ``1 - env``, and
            # that is the pitch dive / climb. Never ahead of the write, and
            # the ring was sized for the cap. Applied as whole samples and
            # a fraction SEPARATELY, for the same reason as the read above:
            # a lag subtracted from an index would round at the index's
            # magnitude, and at a half-sample lag that flips float32 ties.
            # Measured.
            lag_i = np.floor(stop_lag)
            frac = frac - (stop_lag - lag_i)
            back = back + lag_i.astype(np.int64)
            under = frac < 0.0
            frac[under] += 1.0
            back[under] += 1
        ri = absidx - back
        tap = (buf[:, ri % L] * (1.0 - frac)
               + buf[:, (ri + 1) % L] * frac)              # (V, F)

        wet = tap
        if stop_env is not None:
            # A head's EMF is proportional to tape speed: the wet follows the
            # speed linearly, per sample, to exact silence at the halt.
            speed = 1.0 - stop_env
            wet = wet * speed[None, :]
        # --- saturation (4x-oversampled tanh) -----------------------------
        if sat_active:
            drive = sat * self._TAPE_SAT_DRIVE_MAX
            os4 = state["os4"]
            wet = os4.down(self._dist_curve("soft", drive, os4.up(wet)))

        # --- hiss (calibrated noise floor, lives in the wet path) ---------
        if hiss_active:
            amp = 10.0 ** (min(hiss_db, self._TAPE_HISS_MAX_DB) / 20.0)
            hn = state["rng_hiss"].standard_normal(frames) * amp
            if stop_env is not None:
                hn = hn * speed                    # tape hiss: a still head reads none
            wet = wet + hn[None, :]

        # --- head-bump low shelf (~60 Hz), streaming per voice ------------
        if bump_active:
            b0, b1, b2, a1n, a2n = self._loud_shelf(
                self._TAPE_BUMP_HZ, bump_db, True
            )
            sh = state["shelf"]
            if sh.get("zi") is None or sh["zi"].shape[0] != v or sh.get("g") != bump_db:
                sh["zi"] = np.zeros((v, 2))
                sh["g"] = bump_db
            wet, sh["zi"] = lfilter(
                [b0, b1, b2], [1.0, a1n, a2n], wet, axis=-1, zi=sh["zi"]
            )

        # --- mix against the latency-matched dry (tail always advances) ---
        both = np.concatenate([state["dry_tail"], x], axis=-1)
        dry = both[:, :frames]
        state["dry_tail"] = both[:, frames:]
        out = wet if mix >= 1.0 else (1.0 - mix) * dry + mix * wet

        out32 = out.astype(np.float32)
        return out32[0] if was_mono else out32
