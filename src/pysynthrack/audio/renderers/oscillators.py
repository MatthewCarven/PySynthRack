"""Oscillator renderers: oscillator, supersaw, wavetable morph, FM operator, organ.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". The oscillator's mono/voice paths and the wavetable-morph
stack builders came along; the phase / wave-shaping core they share with
keyboard, MIDI input and CV-to-frequency (``_osc_waveshape``,
``_waveshape_blep`` / ``_waveshape_wt`` / ``_get_wavetable``, the
``WT_*`` mipmap constants, ``_osc_pw_increment``) stays in the backend and
is reached through ``self``, as are ``_osc_carried_phase`` /
``_OSC_EPOCH``, ``_input_buffer``, ``_ring_match_voices`` and
``_pow2_clipped``. The pulse-width bounds and the FM ratio snap are
imported from their ``modules`` source, as the backend did.
"""
from __future__ import annotations

import numpy as np
from scipy.io import wavfile

from ...modules.fm_op import snap_ratio as _fm_snap_ratio
from ...modules.oscillator import PULSE_WIDTH_MAX as _PW_MAX
from ...modules.oscillator import PULSE_WIDTH_MIN as _PW_MIN


class OscillatorRenderers:
    def _render_oscillator(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Audio-rate oscillator with optional per-sample CV modulation.

        Shape-polymorphic. The branch is decided by ``freq_cv``'s shape,
        because phase state is what changes with frequency:

          * ``freq_cv`` 2D ``(V, F)`` -> voice-aware path. V independent
            phase accumulators, one per voice slot. Output ``(V, F)``.
            ``amp_cv`` (any shape) and the static ``amp`` param are
            applied via numpy broadcasting at the end.
          * ``freq_cv`` 1D ``(F,)`` or None -> mono path with a single
            phase accumulator. Output ``(F,)``. If ``amp_cv`` happens to
            be 2D ``(V, F)`` (e.g. a polyphonic ADSR feeding a single
            shared oscillator), the final ``wave * amp_cv`` broadcasts
            the mono waveform across every voice -- producing a ``(V, F)``
            result. That broadcast-by-amp case is the moral equivalent
            of the VCA broadcast rules from slice 3a, and it's exactly
            what you want when one carrier should be amplitude-shaped
            independently per voice.

        Three CV inputs (all optional):
          - ``freq_cv`` follows 1V/octave: instantaneous frequency for
            sample n is ``freq * 2 ** cv[n]``. Per-sample evaluation
            makes this true FM/vibrato -- phase is integrated from the
            instantaneous frequency, not a block-rate scalar.
          - ``amp_cv`` is linear multiplicative: ``amp * cv[n]``. A
            unipolar LFO here gives AM; bipolar would invert phase.
          - ``pw_cv`` moves the pulse width of the square shapes:
            ``pw[n] = clip(pulse_width + pw_cv_depth * cv[n], 0.05,
            0.95)`` per sample. Like ``amp_cv`` it broadcasts: a
            ``(V, F)`` source on the mono path yields ``(V, F)`` output
            (one phase, a width per voice); a mono source on the voice
            path is shared by every voice. Only ``square`` and
            ``square_blep`` listen (``square_wt`` is a fixed 50% table);
            the other shapes are handed the width and ignore it.

        Every path is block-size exact (2026-09-24): no phase is ever a
        per-block float accumulator -- see :meth:`_render_oscillator_mono`
        (origin + integer count / carried running sum) and
        :meth:`_osc_carried_phase`; the ``_wt`` band is picked per sample
        (:meth:`_waveshape_wt`). No block-mean is taken anywhere in the
        oscillator, so there is no float32 ``np.mean`` to audit.
        """
        freq = float(module.params.get("freq", 440.0))
        amp = float(module.params.get("amp", 0.5))
        waveform = str(module.params.get("waveform", "sine"))
        pulse_width = min(max(
            float(module.params.get("pulse_width", 0.5)), _PW_MIN), _PW_MAX)
        pw_depth = float(module.params.get("pw_cv_depth", 0.5))

        # CV lookups only when called via the topo walk (which always
        # passes buffers + patch). Tests that drive the oscillator in
        # isolation pass None.
        if buffers is None or patch is None:
            freq_cv = None
            amp_cv = None
            pw_cv = None
        else:
            # collapse=False so a voice-aware (V, F) freq_cv or amp_cv
            # reaches us with the voice axis intact. The mono branch
            # handles a 2D amp_cv via final broadcast.
            freq_cv = self._input_buffer(
                patch, buffers, module.id, "freq_cv", collapse=False
            )
            amp_cv = self._input_buffer(
                patch, buffers, module.id, "amp_cv", collapse=False
            )
            pw_cv = self._input_buffer(
                patch, buffers, module.id, "pw_cv", collapse=False
            )

        # The width is a scalar when pw_cv is unpatched (the static
        # param, so the default path allocates nothing extra) and a
        # per-sample array when patched.
        if pw_cv is None:
            pw = pulse_width
        else:
            pw = np.clip(
                pulse_width + pw_depth * pw_cv.astype(np.float64),
                _PW_MIN, _PW_MAX,
            )

        if freq_cv is not None and freq_cv.ndim == 2:
            return self._render_oscillator_voice(
                module, frames, freq, amp, waveform, freq_cv, amp_cv, pw
            )
        return self._render_oscillator_mono(
            module, frames, freq, amp, waveform, freq_cv, amp_cv, pw
        )

    # ----- Supersaw rendering ----------------------------------------------

    def _render_supersaw(self, module, frames: int, buffers, patch) -> dict:
        """Seven detuned PolyBLEP saws per voice (see modules/supersaw.py).

        The whole stack is ONE `_waveshape_blep` call: phases fold into
        a (V, 7, F) block (per-sample dt rides along for the BLEP
        window), then two einsums over the saw axis produce the L/R
        buses through per-saw equal-power pan gains. Free-running
        initial phases are seeded per (voice slot, saw index) — the
        supersaw signature, deterministic per slot so patches recall.
        Gains are RMS-normalised so blend/detune moves don't pump; at
        ``spread`` 0 the L and R gain vectors are identical, so the two
        outs are bit-identical.

        ``detune_cv`` is a BLOCK-RATE control on the detune knob --
        ``detune_eff = clip(detune + detune_cv_depth * mean cv, 0, 1)``
        -- because the detune is a per-block frequency table here
        (seven ratios, one exponentiation, then the phase ramps ride
        them); a per-sample detune would mean a (V, 7, F) exponent
        every block for a knob nobody moves at audio rate. The phases
        are carried across blocks, so a block-to-block change in the
        ratios is a slope change, not a discontinuity: a sweep is
        click-free. Per voice when the source is ``(V, F)`` on a
        voice-aware stack (the mean over axis 1 is a ``(V,)`` vector,
        broadcast over the seven offsets -- the ``mult`` table simply
        grows a voice axis); otherwise the mean over everything, one
        detune shared. The gain normalisation never depended on the
        detune, so the CV path pumps exactly as much as the knob does:
        not at all. Unpatched, ``detune_eff`` IS the knob float and
        every expression below is the pre-CV one, bit for bit.
        """
        from ...modules.supersaw import (
            SUPERSAW_MAX_CENTS,
            SUPERSAW_N,
            SUPERSAW_OFFSETS,
            SUPERSAW_PAN,
        )

        freq = float(module.params.get("freq", 261.6256))
        detune = min(1.0, max(0.0, float(module.params.get("detune", 0.35))))
        blend = min(1.0, max(0.0, float(module.params.get("blend", 0.75))))
        spread = min(1.0, max(0.0, float(module.params.get("spread", 0.5))))
        amp = float(module.params.get("amp", 0.5))

        freq_cv = self._input_buffer(
            patch, buffers, module.id, "freq_cv", collapse=False
        )
        amp_cv = self._input_buffer(
            patch, buffers, module.id, "amp_cv", collapse=False
        )
        detune_cv = self._input_buffer(
            patch, buffers, module.id, "detune_cv", collapse=False
        )

        voiced = freq_cv is not None and freq_cv.ndim == 2
        V = freq_cv.shape[0] if voiced else 1

        # Block-mean detune offset. Depth 0 short-circuits (the knob
        # float, untouched -- "disables without unpatching", and a
        # non-finite cable can't leak through 0 * inf). The mean is
        # taken in float64 so the scalar path lands on the same float
        # a knob set to ``detune + depth * mean`` would.
        detune_depth = float(module.params.get("detune_cv_depth", 1.0))
        detune_eff = detune
        if detune_cv is not None and detune_cv.size > 0 and detune_depth != 0.0:
            dcv = detune_cv.astype(np.float64)
            if voiced and dcv.ndim == 2 and dcv.shape[0] == V:
                # A detune per voice: (V,), clipped to the knob's rails.
                detune_eff = np.clip(
                    detune + detune_depth * self._finite_mean(dcv, axis=1),
                    0.0,
                    1.0,
                )
            else:
                detune_eff = min(
                    1.0, max(0.0, detune + detune_depth * self._finite_mean(dcv))
                )

        sr = float(self.sample_rate)
        offsets = np.asarray(SUPERSAW_OFFSETS, dtype=np.float64)
        if np.ndim(detune_eff) == 0:
            mult = 2.0 ** (offsets * SUPERSAW_MAX_CENTS * detune_eff / 1200.0)  # (7,)
            mult3 = mult[None, :, None]  # (1, 7, 1)
        else:
            # Same expression, same operation order, one axis wider:
            # a single-voice row is bit-identical to the scalar path.
            mult = 2.0 ** (
                offsets[None, :] * SUPERSAW_MAX_CENTS * detune_eff[:, None] / 1200.0
            )  # (V, 7)
            mult3 = mult[:, :, None]  # (V, 7, 1)

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V:
            st.clear()
            phases0 = np.empty((V, SUPERSAW_N), dtype=np.float64)
            for v in range(V):
                for i in range(SUPERSAW_N):
                    # Fixed per-(slot, saw) seeds: free phases, recalled.
                    phases0[v, i] = float(
                        np.random.default_rng([93, v, i]).random()
                    )
            st.update({"V": V, "phases": phases0})
        phases0 = st["phases"]  # (V, 7)

        if freq_cv is None:
            inc = np.broadcast_to(freq * mult3 / sr, (V, SUPERSAW_N, 1))
            ramp = np.arange(frames, dtype=np.float64)
            phases = (phases0[:, :, None] + inc * (ramp + 1.0)) % 1.0
            st["phases"] = phases[:, :, -1].copy()
            dt = inc
        else:
            cv = freq_cv.astype(np.float64)
            if cv.ndim == 1:
                cv = cv[None, :]
            inst = freq * np.power(2.0, cv)  # (V, F)
            inc = inst[:, None, :] * mult3 / sr  # (V, 7, F)
            phases = (
                phases0[:, :, None] + np.cumsum(inc, axis=2)
            ) % 1.0
            st["phases"] = phases[:, :, -1].copy()
            dt = inc

        wave = self._waveshape_blep("saw", phases, dt)  # (V, 7, F)

        gains = np.full(SUPERSAW_N, blend, dtype=np.float64)
        gains[3] = 1.0 - 0.5 * blend  # the center saw
        norm = 1.0 / np.sqrt(float(np.sum(gains * gains)))
        theta = (np.pi / 4.0) * (
            1.0 + spread * np.asarray(SUPERSAW_PAN, dtype=np.float64)
        )
        gl = gains * norm * np.cos(theta)
        gr = gains * norm * np.sin(theta)

        out_l = np.einsum("vsf,s->vf", wave, gl) * amp
        out_r = np.einsum("vsf,s->vf", wave, gr) * amp
        if amp_cv is not None:
            out_l = out_l * amp_cv.astype(np.float64)
            out_r = out_r * amp_cv.astype(np.float64)

        out_l = out_l.astype(np.float32)
        out_r = out_r.astype(np.float32)
        if not voiced and out_l.ndim == 2 and out_l.shape[0] == 1:
            out_l, out_r = out_l[0], out_r[0]
        return {"out_l": out_l, "out_r": out_r}

    # ----- WavetableMorph rendering ----------------------------------------

    def _morph_stack(self, name: str) -> np.ndarray:
        """Build (cached) a morph stack: (n_frames, NUM_WT_TABLES, WT_LEN).

        Frames are defined in the harmonic domain (amplitude + phase per
        harmonic) and each frame is mip-banded exactly like
        :meth:`_get_wavetable` — band ``j`` keeps only the harmonics
        below Nyquist for the top of octave ``j``. Each frame is
        normalised by ONE scalar (its fullest band's peak) so the
        position crossfade never pumps between bands.
        """
        stacks = getattr(self, "_morph_stacks", None)
        if stacks is None:
            stacks = self._morph_stacks = {}
        cached = stacks.get(name)
        if cached is not None:
            return cached

        H = 128  # harmonics carried per frame
        k = np.arange(1, H + 1, dtype=np.float64)

        def frame(amps, phs=None):
            a = np.zeros(H, dtype=np.float64)
            a[: len(amps)] = amps
            p = np.zeros(H, dtype=np.float64)
            if phs is not None:
                p[: len(phs)] = phs
            return a, p

        frames_spec: list[tuple[np.ndarray, np.ndarray]] = []
        if name == "vowel":
            # Generic vowel formant bumps at harmonic positions
            # (physics-textbook shapes; center ≈ formant / f0 at ~130 Hz).
            vowels = ((6.0, 11.0), (4.0, 18.0), (2.5, 21.0), (4.0, 7.0), (2.5, 5.0))
            for f1, f2 in vowels:
                amps = (
                    np.exp(-0.5 * ((k - f1) / 1.8) ** 2)
                    + 0.6 * np.exp(-0.5 * ((k - f2) / 2.5) ** 2)
                ) / k ** 0.3
                frames_spec.append(frame(amps))
        elif name == "metallic":
            rng = np.random.default_rng(417)  # fixed: part of the sound
            sets = (
                (1, 6, 13),
                (1, 5, 11, 19),
                (1, 7, 15, 26, 38),
                (1, 9, 17, 29, 44, 61),
            )
            for harmonics in sets:
                amps = np.zeros(H)
                phs = np.zeros(H)
                for h in harmonics:
                    amps[h - 1] = 1.0 / np.sqrt(h)
                    phs[h - 1] = float(rng.uniform(0, 2 * np.pi))
                frames_spec.append(frame(amps, phs))
        else:  # "analog": sine → triangle → saw → square
            sine = np.zeros(H)
            sine[0] = 1.0
            tri = np.zeros(H)
            sign = 1.0
            for kk in range(1, H + 1, 2):
                tri[kk - 1] = sign / (kk * kk)
                sign = -sign
            saw = 1.0 / k
            sq = np.zeros(H)
            sq[0::2] = 1.0 / k[0::2]
            for amps in (sine, tri, saw, sq):
                frames_spec.append(frame(amps))

        stack = self._bandlimit_frames(frames_spec)
        stacks[name] = stack
        return stack

    def _bandlimit_frames(self, frames_spec) -> np.ndarray:
        """Additively render harmonic-domain frames into per-octave
        mip bands: (n_frames, NUM_WT_TABLES, WT_LEN)."""
        L = self.WT_LEN
        ph = np.arange(L, dtype=np.float64) / L
        nyq = self.sample_rate / 2.0
        n_frames = len(frames_spec)
        stack = np.zeros((n_frames, self.NUM_WT_TABLES, L), dtype=np.float64)
        for fi, (amps, phs) in enumerate(frames_spec):
            H = len(amps)
            for j in range(self.NUM_WT_TABLES):
                f_high = self.WT_BASE_FREQ * (2.0 ** (j + 1))
                max_h = max(1, int(nyq / f_high))
                acc = np.zeros(L, dtype=np.float64)
                for kk in range(1, min(H, max_h) + 1):
                    a = amps[kk - 1]
                    if a == 0.0:
                        continue
                    acc += a * np.sin(
                        2.0 * np.pi * kk * ph + phs[kk - 1]
                    )
                stack[fi, j] = acc
            # One normaliser per FRAME (its fullest band) so the
            # position crossfade never pumps between mip bands.
            peak = float(np.max(np.abs(stack[fi, 0]))) or 1.0
            stack[fi] /= peak
        return stack

    def _morph_file_stack(self, path: str) -> np.ndarray | None:
        """Load (cached) a single-cycle WAV as a 1-frame morph stack, or
        None if unreadable — the caller falls back to the built-in."""
        stacks = getattr(self, "_morph_file_stacks", None)
        if stacks is None:
            stacks = self._morph_file_stacks = {}
        if path in stacks:
            return stacks[path]
        stack = None
        try:
            _sr, data = wavfile.read(path)
            cycle = np.asarray(data, dtype=np.float64)
            if cycle.ndim == 2:
                cycle = cycle.mean(axis=1)
            if cycle.size >= 8:
                peak = float(np.max(np.abs(cycle))) or 1.0
                cycle = cycle / peak
                # Spectral resample of the cycle: its rfft bins ARE the
                # harmonic series of the table.
                spec = np.fft.rfft(cycle)
                H = min(len(spec) - 1, 128)
                amps = np.abs(spec[1 : H + 1]) * (2.0 / cycle.size)
                phs = np.angle(spec[1 : H + 1]) + np.pi / 2.0
                stack = self._bandlimit_frames([(amps, phs)])
        except Exception:
            stack = None
        stacks[path] = stack
        return stack

    @staticmethod
    def _wt_lookup(tbl, phases):
        """Linear-interp table read (the `_waveshape_wt` core)."""
        L = len(tbl)
        pos = np.asarray(phases, dtype=np.float64) * L
        floor_pos = np.floor(pos)
        i0 = floor_pos.astype(np.int64) % L
        i1 = (i0 + 1) % L
        frac = pos - floor_pos
        return tbl[i0] * (1.0 - frac) + tbl[i1] * frac

    def _render_wavetable_morph(
        self, module, frames: int, buffers, patch
    ) -> np.ndarray:
        """Scanning wavetable oscillator (see modules/wavetable_morph.py).

        Per voice: phase accumulates exactly like the oscillator
        (arange fast path / cumsum under CV); the mip band comes from
        the block's largest dt (the `_waveshape_wt` rule); the scan
        position (param + depth·CV, block-mean per voice) crossfades
        the two adjacent frames of the stack. File stacks override the
        built-in ``table`` when they load; a bad path falls back
        silently so patches always load.
        """
        from ...modules.wavetable_morph import WT_STACKS

        p = module.params
        freq = float(p.get("freq", 261.6256))
        position = min(1.0, max(0.0, float(p.get("position", 0.0))))
        depth = float(p.get("position_cv_depth", 1.0))
        amp = float(p.get("amp", 0.5))
        table = str(p.get("table", "analog"))
        if table not in WT_STACKS:
            table = "analog"
        path = str(p.get("file", "") or "")

        stack = self._morph_file_stack(path) if path else None
        if stack is None:
            stack = self._morph_stack(table)
        n_frames = stack.shape[0]

        freq_cv = self._input_buffer(
            patch, buffers, module.id, "freq_cv", collapse=False
        )
        pos_cv = self._input_buffer(
            patch, buffers, module.id, "position_cv", collapse=False
        )
        amp_cv = self._input_buffer(
            patch, buffers, module.id, "amp_cv", collapse=False
        )

        voiced = freq_cv is not None and freq_cv.ndim == 2
        V = freq_cv.shape[0] if voiced else 1

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V:
            st.clear()
            st.update({"V": V, "phases": np.zeros(V, dtype=np.float64)})
        phases0 = st["phases"]

        sr = float(self.sample_rate)
        if freq_cv is None:
            inc0 = freq / sr
            ramp = np.arange(frames, dtype=np.float64)
            phases = (phases0[:, None] + inc0 * (ramp[None, :] + 1.0)) % 1.0
            dt_max = np.full(V, inc0)
        else:
            cv = freq_cv.astype(np.float64)
            if cv.ndim == 1:
                cv = cv[None, :]
            inc = freq * np.power(2.0, cv) / sr  # (V, F)
            phases = (phases0[:, None] + np.cumsum(inc, axis=1)) % 1.0
            dt_max = inc.max(axis=1)
        st["phases"] = phases[:, -1].copy()

        def pos_row(v):
            if pos_cv is None:
                return position
            row = pos_cv[v] if pos_cv.ndim == 2 and v < pos_cv.shape[0] else (
                pos_cv[0] if pos_cv.ndim == 2 else pos_cv
            )
            return min(1.0, max(0.0, position + depth * self._finite_mean(row)))

        out = np.empty((V, frames), dtype=np.float64)
        for v in range(V):
            f_rep = max(float(dt_max[v]) * sr, self.WT_BASE_FREQ)
            j = int(
                np.clip(
                    np.floor(np.log2(f_rep / self.WT_BASE_FREQ)),
                    0,
                    self.NUM_WT_TABLES - 1,
                )
            )
            scaled = pos_row(v) * (n_frames - 1)
            i0 = min(int(np.floor(scaled)), n_frames - 1)
            i1 = min(i0 + 1, n_frames - 1)
            fr = scaled - i0
            wave = self._wt_lookup(stack[i0, j], phases[v])
            if fr > 0.0 and i1 != i0:
                wave = wave * (1.0 - fr) + fr * self._wt_lookup(
                    stack[i1, j], phases[v]
                )
            out[v] = wave

        out = out * amp
        if amp_cv is not None:
            out = out * amp_cv.astype(np.float64)
        out32 = out.astype(np.float32)
        return out32 if voiced else out32[0]

    def _render_oscillator_mono(
        self, module, frames, freq, amp, waveform, freq_cv, amp_cv, pw=None
    ):
        """Mono fast path -- scalar phase, vectorized phase ramp.

        Two phase engines, both exact across block sizes (2026-09-24;
        the organ's 2026-09-22 fix was the model):

          * no ``freq_cv`` -- constant frequency. The ramp is a phase
            ORIGIN plus the INTEGER count of samples since it,
            ``ph(k) = (origin + inc * k) % 1``, re-anchored only when
            the increment changes (a knob move). A held note never
            accumulates at all, so sample k is the same number whichever
            block it lands in -- and the same number the organ's lone 8'
            computes.
          * mono ``freq_cv`` -- per-sample frequency, integrated by
            :meth:`_osc_carried_phase` (a sequential running sum that
            carries across blocks unwrapped and wraps only at absolute
            epoch boundaries), so a held CV, a stepped sequence and
            vibrato all render identically at any block size.

        A per-block ``phase += frames * inc`` (the old fast path) or a
        per-block ``start + cumsum(inc)`` wrapped at every block end (the
        old CV path) rounds differently under a different partition and
        drifted a float32 ulp within ~0.1-2 s.

        ``state["phase"]`` is always the phase of the last sample
        rendered (what a switch between the two engines, or a reader,
        picks up); the engines' own state is ``origin`` / ``count`` /
        ``inc`` (fast) and ``carry`` + the absolute sample count ``n``
        (CV). Patching or unpatching ``freq_cv`` switches engine, and the
        incoming one carries on one step from that last sample, so the
        switch is seamless (the old pair disagreed on whether the stored
        phase was the last sample's or the next's, and skipped or
        repeated a sample at every switch).

        The amp_cv multiplication at the end can broadcast a (F,) mono
        wave against a (V, F) voice amp_cv, producing (V, F) output --
        the broadcast-by-amp case. A (V, F) ``pw`` broadcasts the same
        way (one phase ramp, a width per voice) -- the broadcast-by-width
        case.
        """
        state = self._state.setdefault(module.id, {"phase": 0.0})
        # If state belongs to the voice branch (different keys),
        # discard and reinit to mono shape.
        if "phase_arr" in state:
            state.clear()
            state["phase"] = 0.0

        sr = self.sample_rate
        n_abs = int(state.get("n", 0))
        if freq_cv is None:
            # Fast path: constant frequency, origin + integer count.
            phase_inc = freq / sr
            prev_inc = state.get("inc")
            if prev_inc != phase_inc:
                # Re-anchor so the origin is the NEXT sample's phase.
                if "carry" in state:
                    # Arriving from the CV engine: one step on from the
                    # last sample it rendered.
                    origin = (float(state["phase"]) + phase_inc) % 1.0
                elif prev_inc is None:
                    origin = 0.0  # fresh state
                else:
                    # A new frequency: where the old ramp was heading
                    # (the organ's re-anchor, expression for expression).
                    origin = (
                        state["origin"] + prev_inc * float(state["count"])
                    ) % 1.0
                state["origin"] = origin
                state["count"] = 0
                state["inc"] = phase_inc
            state.pop("carry", None)
            origin = state["origin"]
            k = float(state["count"]) + np.arange(frames, dtype=np.float64)
            phases = (origin + phase_inc * k) % 1.0
            state["count"] += frames
            if frames:
                state["phase"] = float(phases[-1])  # the last sample's phase
            dt = phase_inc
        else:
            # Per-sample frequency from CV, integrated sample by sample
            # (a carried sequential sum -- see _osc_carried_phase).
            inst_freq = freq * np.power(2.0, freq_cv.astype(np.float64))
            inst_inc = inst_freq / sr
            carry = state.get("carry")
            if carry is None:
                # Fresh (0.0) or arriving from the fast engine: carry on
                # from the last sample it rendered, click-free.
                carry = float(state["phase"])
            phases, carry = self._osc_carried_phase(
                carry, n_abs, inst_inc, self._OSC_EPOCH
            )
            state["carry"] = float(carry)
            state["phase"] = float(phases[-1])
            state["inc"] = None  # the fast engine re-anchors on return
            dt = inst_inc
        state["n"] = n_abs + frames

        dpw = None
        if isinstance(pw, np.ndarray):
            dpw = self._osc_pw_increment(state, pw)
        elif pw is not None:
            # Keep the history current while the width is static so a
            # cable patched in later diffs against the right value.
            state["pw_last"] = np.float64(pw)
        wave = self._osc_waveshape(phases, waveform, dt=dt, pw=pw, dpw=dpw)
        wave = wave * amp
        if amp_cv is not None:
            # amp_cv may be (F,) (same shape, elementwise) or (V, F)
            # (broadcasts the mono wave across V voices, yielding a
            # (V, F) result). Both are valid.
            wave = wave * amp_cv.astype(np.float64)

        return wave.astype(np.float32)

    def _render_oscillator_voice(
        self, module, frames, freq, amp, waveform, freq_cv, amp_cv, pw=None
    ):
        """Voice-aware path -- V independent phase accumulators.

        ``freq_cv`` is ``(V, F)``. Each voice integrates its own phase
        via per-row cumsum, and per-voice phase state persists across
        blocks. Output is ``(V, F)``. ``amp_cv`` (any shape) is applied
        via numpy broadcasting at the end:

          * (V, F) amp_cv -> elementwise per-voice AM.
          * (F,)  amp_cv -> mono amplitude broadcast across every voice.
          * None  amp_cv -> just the static ``amp`` param.

        ``pw`` follows the same three cases: a (V, F) width per voice,
        an (F,) width shared by every voice (materialised to (V, F) so
        its per-sample increment carries per-voice history), or the
        static param.

        Phases are kept per-voice so a slot that was silent in a prior
        block (freq_cv = 0 -> phase advances at the base ``freq``)
        still carries a sensible phase when it next becomes audible,
        instead of restarting from 0.0 every retrigger. MIDIInput
        zero-pads unused slots, so silent slots advance at the param's
        base frequency -- harmless because the per-voice ADSR/VCA
        downstream silences those slots.

        Each voice integrates through :meth:`_osc_carried_phase` (a
        sequential running sum carried unwrapped across blocks, wrapped
        only at absolute epoch boundaries), so every voice renders the
        identical sample at any block size -- held chord, sequence or
        vibrato (2026-09-24; the per-block ``start + cumsum`` wrapped at
        every block end drifted a float32 ulp within ~0.1 s).
        """
        V = freq_cv.shape[0]
        state = self._state.setdefault(module.id, {})

        # Reinit if state belongs to the mono branch or the voice
        # count changed.
        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.zeros(V, dtype=np.float64)
            state["carry_arr"] = np.zeros(V, dtype=np.float64)
            state["n"] = 0

        sr = self.sample_rate

        # Per-sample per-voice instantaneous frequency from CV.
        inst_freq = freq * np.power(2.0, freq_cv.astype(np.float64))  # (V, F)
        inst_inc = inst_freq / sr  # (V, F)
        phases, state["carry_arr"] = self._osc_carried_phase(
            state["carry_arr"], state["n"], inst_inc, self._OSC_EPOCH
        )  # (V, F)
        state["phase_arr"] = phases[:, -1].copy()
        state["n"] += frames

        dpw = None
        if isinstance(pw, np.ndarray):
            if pw.ndim == 1:
                pw = np.broadcast_to(pw[None, :], phases.shape)
            dpw = self._osc_pw_increment(state, pw)
        elif pw is not None:
            state["pw_last"] = np.full(V, pw, dtype=np.float64)
        wave = self._osc_waveshape(phases, waveform, dt=inst_inc, pw=pw, dpw=dpw)  # (V, F)
        wave = wave * amp
        if amp_cv is not None:
            # amp_cv (V, F) -> elementwise; amp_cv (F,) -> broadcasts
            # across the voice axis (same mono amp applied to every
            # voice). Both correct under numpy broadcasting rules.
            wave = wave * amp_cv.astype(np.float64)

        return wave.astype(np.float32)

    # C4 (MIDI 60) in Hz — the fm_op carrier pitch at pitch_cv = 0 V (1 V/oct).
    _FM_REF_HZ = 261.6256

    def _render_fm_op(self, module, frames: int, buffers, patch):
        """One DX-style phase-modulation FM operator (a self-contained voice).

        Per sample: ``core = sin(2*pi*phase + index*pm + feedback*core_prev)``
        and ``out = amp_cv * core``. ``phase`` integrates the carrier
        frequency (C4 * 2**pitch_cv * ratio * 2**(fine/1200), or a fixed
        ``freq`` in ``fixed`` mode) per sample, exclusive-prefix so a fresh
        module starts at phase 0. ``index`` scales the audio-rate ``pm``
        input in *radians* (peak phase deviation for full-scale pm), boosted
        per-sample by ``index_cv`` * ``index_cv_depth`` (floored at 0).

        Shape-polymorphic: the ``(V, F)`` core runs with ``V == 1`` for all-
        mono inputs, so a single voice row is bit-identical to the mono
        render; per-voice phase and feedback state keep voices independent.
        ``V`` follows the widest voice-aware input (``pitch_cv`` / ``pm`` /
        ``amp_cv`` / ``index_cv``); a mono input broadcasts across voices.

        Dual engine (delay precedent): ``feedback == 0`` has no sample-to-
        sample dependency, so the whole block vectorizes; ``feedback > 0``
        needs the sequential per-sample recurrence (V-vectorized, F-looped).
        The two paths are bit-identical at ``feedback == 0`` (``0 * prev``
        adds nothing). Phase integrates continuously across blocks (state
        carried), so the output is block-size independent to within float
        phase-wrap rounding (< 1e-6), the ring_mod internal-sine contract.
        """
        p = module.params
        fine = float(p.get("fine", 0.0))
        index = float(p.get("index", 1.0))
        index_cv_depth = float(p.get("index_cv_depth", 1.0))
        feedback = min(max(float(p.get("feedback", 0.0)), 0.0), 1.0)
        fixed = bool(p.get("fixed", False))

        pitch_cv = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        pm = self._input_buffer(patch, buffers, module.id, "pm", collapse=False)
        amp_cv = self._input_buffer(
            patch, buffers, module.id, "amp_cv", collapse=False
        )
        index_cv = self._input_buffer(
            patch, buffers, module.id, "index_cv", collapse=False
        )

        # Voice count = widest voice-aware input; mono inputs broadcast.
        v = 1
        was_mono = True
        for buf in (pitch_cv, pm, amp_cv, index_cv):
            if buf is not None and getattr(buf, "ndim", 1) == 2:
                v = max(v, buf.shape[0])
                was_mono = False

        if frames == 0:
            z = np.zeros((v, 0), dtype=np.float32)
            return z[0] if was_mono else z

        sr = self.sample_rate

        # Carrier phase increment per sample, (v, F).
        if fixed:
            freq = float(p.get("freq", 220.0))
            inc = np.full((v, frames), freq / sr, dtype=np.float64)
        else:
            base = (
                self._FM_REF_HZ
                * _fm_snap_ratio(float(p.get("ratio", 1.0)))
                * (2.0 ** (fine / 1200.0))
            )
            if pitch_cv is None or not getattr(pitch_cv, "size", 0):
                inc = np.full((v, frames), base / sr, dtype=np.float64)
            else:
                cv = self._ring_match_voices(pitch_cv, v, frames)
                inc = (base * np.power(2.0, cv)) / sr

        state = self._state.setdefault(module.id, {})
        ph0 = state.get("phase")
        if ph0 is None or ph0.shape[0] != v:
            ph0 = np.zeros(v, dtype=np.float64)
        csum = np.cumsum(inc, axis=1)
        phase = (ph0[:, None] + csum - inc) % 1.0            # exclusive prefix
        state["phase"] = (ph0 + csum[:, -1]) % 1.0
        theta = 2.0 * np.pi * phase                          # (v, F) radians

        # Phase-modulation term: pm * effective index (radians).
        if pm is None or not getattr(pm, "size", 0):
            pm_arg = np.zeros((v, frames), dtype=np.float64)
        else:
            pm_v = self._ring_match_voices(pm, v, frames)
            if (
                index_cv is None
                or not getattr(index_cv, "size", 0)
                or index_cv_depth == 0.0
            ):
                eff_index = index
            else:
                icv = self._ring_match_voices(index_cv, v, frames)
                eff_index = np.maximum(index + index_cv_depth * icv, 0.0)
            pm_arg = eff_index * pm_v

        arg = theta + pm_arg                                 # everything but fb

        fb_prev = state.get("fb")
        if fb_prev is None or fb_prev.shape[0] != v:
            fb_prev = np.zeros(v, dtype=np.float64)

        if feedback <= 0.0:
            core = np.sin(arg)
        else:
            core = np.empty((v, frames), dtype=np.float64)
            prev = fb_prev
            for n in range(frames):
                prev = np.sin(arg[:, n] + feedback * prev)
                core[:, n] = prev
        # Persist the last output sample so feedback (if enabled later, or
        # this block) continues seamlessly across block boundaries.
        state["fb"] = core[:, -1].copy()

        if amp_cv is None or not getattr(amp_cv, "size", 0):
            out = core
        else:
            out = core * self._ring_match_voices(amp_cv, v, frames)

        out32 = out.astype(np.float32)
        return out32[0] if was_mono else out32

    # ----- Organ rendering -------------------------------------------------

    _ORGAN_C4 = 261.6255653005986
    _ORGAN_RAMP_S = 0.001    # gate ramp time (reaches exactly 0/1)
    _ORGAN_CLICK_S = 0.004   # key-click burst length
    _ORGAN_CLICK_AMP = 0.15  # click peak scale at click = 1
    _ORGAN_PERC_T60 = {"fast": 0.3, "slow": 1.0}
    _ORGAN_PERC_RATIO = {"2nd": 2.0, "3rd": 3.0}
    # The scanner (see modules/organ.py): a 412 rpm motor sweeps the line
    # once per turn. The line centre is the half-swing plus a margin
    # (never below 2 samples, so a read never crosses the write head);
    # every switch crossfades gains and depth over an integer-counted
    # ramp.
    _ORGAN_SCAN_HZ = 412.0 / 60.0
    # The pickup traces a ROUNDED TRIANGLE across the taps, not a sine
    # (2026-09-22): a sine driven through an arcsine,
    #     s(ph) = arcsin(R * sin(2*pi*ph)) / arcsin(R),
    # the exact triangle as R -> 1 and a plain sine as R -> 0. At 0.98
    # the argument never reaches arcsin's singularity, so s is analytic
    # -- rounded corners, no kink to tick -- while the pitch deviation
    # (the sweep's SLOPE) is 88% of the way from a sine to the
    # flat-topped square a linear sweep would make: crest factor 1.095
    # against a sine's 1.414 and a square's 1.0, third harmonic 0.272
    # against a square's 1/3 (measured on the shape; see the module
    # docstring for the measurement on the rendered audio).
    _ORGAN_SCAN_ROUND = 0.98
    # Peak-to-peak sweep per depth index (v1/c1, v2/c2, v3/c3), expressed
    # as the SINE-equivalent swing, because what is pinned is the cents:
    # V3 lands at +/-41. The rounded triangle's peak slope is arcsin(R)/R
    # = 1.398x shallower than a sine's, so the line is scaled by that in
    # _organ_scanner and actually swings 0.49 / 0.98 / 1.54 ms. Keeping
    # the swing instead of the cents would have dropped V3 to +/-29.
    _ORGAN_SCAN_SWING_MS = (0.35, 0.70, 1.10)
    _ORGAN_SCAN_MARGIN_S = 1e-4
    _ORGAN_SCAN_FADE_S = 0.04
    _ORGAN_SCAN_MIN_BLOCK = 4096  # ring headroom: never resized by a short block

    def _render_organ(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Nine-drawbar additive organ (see modules/organ.py).

        Per voice the nine partials render as ONE vectorized sine call
        over a (9, F) phase-ramp block — pitch is read per block (mean
        in float64, the pluck idiom) so within a block every partial's
        frequency is constant and the phase ramp is a plain ``arange``
        (the oscillator's constant-frequency indexing, ``phases[0] =
        start`` — which is what makes the lone-8' drawbar bit-exact
        against a mono sine oscillator over a block).

        What the ramp is added to is a phase ORIGIN plus an INTEGER
        count of samples since that origin, not a per-block float
        accumulator: ``ph(k) = (origin + inc * k) % 1`` for the absolute
        index ``k``. The origin is re-anchored only when a voice's
        increment changes, so a held pitch never accumulates at all and
        every block partition renders the identical sample — a float
        ``phase += inc * frames`` carried per block drifted a float32
        ulp within a second or two (2026-09-22 fix; the scanner's own
        integer-counted phase was the model). Phase state ages for
        every voice every block whether audible or not (the
        oscillator's rule), so activity gating never moves phases; only
        the sine evaluation is skipped for silent voices.

        The gate envelope is an integer-counted linear ramp:
        ``env = clamp(count ± n, 0..R) / R`` with the COUNT carried as
        an int — exactly reaching 0.0/1.0 and bit-exact across any
        block split (a float level would drift in the ramp region).
        Key clicks are seeded per (module, voice, hit) noise bursts
        poured through a per-voice carry tail (the drums whole-hit
        idiom). The percussion register is a single module-wide
        generator: a rising edge fires it only when NO voice's gate was
        high on the previous sample (from-silence single trigger —
        legato and chord additions don't re-fire), and the strike is a
        decaying sine added to voice row 0 (monophonic hardware). A
        re-qualifying strike replaces a still-ringing one. Its decay
        and phase are integer-counted from the strike too, for the same
        reason (``_organ_perc_pour``).

        Partials at or above Nyquist get a zero gain for that voice
        this block (masked, never aliased); the constant-RMS
        normaliser uses the unmasked gains so masking never makes the
        remaining partials louder.

        The scanner (``vibrato``) runs last, over the finished ``(V, F)``
        voice sum, in ``_organ_scanner``; at ``off`` with no line alive
        it is never called, so the pre-scanner render is untouched.
        """
        from ...modules.organ import ORGAN_BARS, ORGAN_RATIOS, ORGAN_VIBRATO

        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        gate = self._input_buffer(
            patch, buffers, module.id, "gate", collapse=False
        )
        if gate is None:
            # No keys patched: silence, and nothing worth remembering.
            self._state.pop(module.id, None)
            if pitch is not None and pitch.ndim == 2:
                return np.zeros((pitch.shape[0], frames), dtype=np.float32)
            return np.zeros(frames, dtype=np.float32)

        voiced = gate.ndim == 2 or (pitch is not None and pitch.ndim == 2)
        V = 1
        for sig in (pitch, gate):
            if sig is not None and sig.ndim == 2:
                V = max(V, sig.shape[0])

        def row(sig, v):
            if sig is None:
                return None
            if sig.ndim == 2:
                return sig[v] if v < sig.shape[0] else sig[0]
            return sig

        p = module.params
        gains = np.zeros(ORGAN_BARS, dtype=np.float64)
        for i in range(ORGAN_BARS):
            try:
                bar = int(p.get(f"bar{i + 1}", 0))
            except (TypeError, ValueError):
                bar = 0
            bar = min(8, max(0, bar))
            if bar > 0:
                gains[i] = 10.0 ** (-3.0 * (8 - bar) / 20.0)
        click = min(1.0, max(0.0, float(p.get("click", 0.3))))
        perc_mode = str(p.get("perc", "off"))
        perc_ratio = self._ORGAN_PERC_RATIO.get(perc_mode)
        perc_t60 = self._ORGAN_PERC_T60.get(
            str(p.get("perc_decay", "fast")), 0.3
        )
        perc_level = min(1.0, max(0.0, float(p.get("perc_level", 0.7))))
        level = float(p.get("level", 0.5))

        # Constant-RMS normaliser over the UNMASKED gains (see docstring).
        mul = level / np.sqrt(max(1.0, float(np.sum(gains * gains))))

        sr = float(self.sample_rate)
        ramp = max(1, int(round(sr * self._ORGAN_RAMP_S)))
        click_len = max(1, int(round(sr * self._ORGAN_CLICK_S)))
        ratios = np.asarray(ORGAN_RATIOS, dtype=np.float64)

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V or st.get("click_len") != click_len:
            st.clear()
            st.update(
                {
                    "V": V,
                    "click_len": click_len,
                    # Phase ORIGIN per (voice, partial) + the integer count
                    # of samples since that origin + the increment the
                    # origin was set for (see the phase-ramp block below).
                    "phases": np.zeros((V, ORGAN_BARS), dtype=np.float64),
                    "phase_n": np.zeros(V, dtype=np.int64),
                    "phase_inc": np.zeros((V, ORGAN_BARS), dtype=np.float64),
                    # The module clock: samples rendered, free-running
                    # whatever the vibrato knob says. It is the scanner's
                    # phase and its ring write index (see below).
                    "n": 0,
                    "count": np.zeros(V, dtype=np.int64),  # ramp position 0..R
                    "prev_gate": np.zeros(V, dtype=bool),
                    "hits": np.zeros(V, dtype=np.int64),
                    "click_tail": np.zeros((V, click_len), dtype=np.float64),
                    "prev_any": False,
                    "perc_amp": 0.0,  # the strike's amplitude AT the strike
                    "perc_n": 0,      # integer samples since the strike
                    "perc_inc": 0.0,
                }
            )
        phases = st["phases"]

        # Per-voice block frequency (block-mean pitch, pluck idiom). The
        # mean accumulates in float64: a float32 accumulator sums a
        # constant CV to a slightly different total per block LENGTH, so
        # the "constant pitch" the caller thinks it sent would arrive as
        # a different frequency under a different block size.
        freqs = np.empty(V, dtype=np.float64)
        for v in range(V):
            p_row = row(pitch, v)
            cv = float(np.mean(p_row, dtype=np.float64)) if p_row is not None else 0.0
            freqs[v] = self._ORGAN_C4 * self._pow2_clipped(cv)
        incs = (freqs[:, None] * ratios[None, :]) / sr  # (V, 9)

        # --- the phase ORIGIN: re-anchor only when the pitch moves -----
        # A per-block float accumulator (``phase += inc * frames``) takes
        # a different rounding path under a different block partition and
        # drifts a float32 ulp inside a second. Instead every block's
        # ramp is ``origin + inc * k`` with ``k`` the ABSOLUTE integer
        # sample index since the origin, so sample k is the same number
        # whichever block it lands in. The origin only moves when this
        # voice's increment changes (a new block-mean pitch) — under a
        # held pitch it never moves at all, and the render is exact for
        # as long as you care to run it.
        ph_inc = st["phase_inc"]
        phase_n = st["phase_n"]
        moved = np.flatnonzero(~np.all(incs == ph_inc, axis=1))
        if moved.size:
            phases[moved] = (
                phases[moved] + ph_inc[moved] * phase_n[moved][:, None]
            ) % 1.0
            phase_n[moved] = 0
            ph_inc[moved] = incs[moved]

        gate_high = self._GATE_HIGH
        G = np.empty((V, frames), dtype=bool)
        for v in range(V):
            G[v] = row(gate, v) > gate_high
        any_high = G.any(axis=0)

        out = np.zeros((V, frames), dtype=np.float64)
        arange_f = np.arange(frames, dtype=np.float64)

        # Percussion decay per sample (needed for carried tails even
        # when the register is off — a live toggle mustn't click).
        perc_g = 10.0 ** (-3.0 / (sr * perc_t60))

        all_rises: list[tuple[int, int]] = []  # (sample, voice)

        for v in range(V):
            gt = G[v]
            prev = np.empty_like(gt)
            prev[0] = bool(st["prev_gate"][v])
            prev[1:] = gt[:-1]
            rises = np.flatnonzero(gt & ~prev)
            falls = np.flatnonzero(~gt & prev)
            st["prev_gate"][v] = bool(gt[-1])
            for e in rises:
                all_rises.append((int(e), v))

            # --- gate envelope: integer-counted linear ramp ------------
            count = int(st["count"][v])
            seg_bounds = np.concatenate(
                (
                    [0],
                    np.flatnonzero(gt[1:] != gt[:-1]) + 1,
                    [frames],
                )
            )
            env = np.empty(frames, dtype=np.float64)
            for si in range(len(seg_bounds) - 1):
                s, e = int(seg_bounds[si]), int(seg_bounds[si + 1])
                n = np.arange(1, e - s + 1, dtype=np.int64)
                if gt[s]:
                    cnt = np.minimum(ramp, count + n)
                else:
                    cnt = np.maximum(0, count - n)
                env[s:e] = cnt / ramp
                count = int(cnt[-1])
            st["count"][v] = count

            # --- the partials (skip the sin for silent voices) ---------
            audible = count > 0 or env.any()
            if audible:
                mask = (freqs[v] * ratios) < (sr * 0.5)
                g_eff = gains * mask
                k = float(phase_n[v]) + arange_f  # absolute sample index
                ph = (phases[v][:, None] + incs[v][:, None] * k[None, :]) % 1.0
                tone = np.einsum(
                    "pf,p->f", np.sin(2.0 * np.pi * ph), g_eff
                )
                out[v] = tone * mul * env

            # --- key clicks (seeded bursts + carry tail) ---------------
            tail = st["click_tail"][v]
            has_events = click > 0.0 and (len(rises) or len(falls))
            if has_events or tail.any():
                scratch = np.zeros(frames + click_len, dtype=np.float64)
                scratch[:click_len] += tail
                if click > 0.0:
                    events = [(int(e), 1.0) for e in rises]
                    events += [(int(e), 0.5) for e in falls]
                    for e, scale in sorted(events):
                        rng = np.random.default_rng(
                            [int(module.id), v, int(st["hits"][v])]
                        )
                        st["hits"][v] += 1
                        burst = np.diff(
                            rng.standard_normal(click_len + 1)
                        ) * np.exp(
                            -np.arange(click_len) / (0.2 * click_len)
                        )
                        scratch[e : e + click_len] += (
                            burst * (click * self._ORGAN_CLICK_AMP * scale)
                        )
                out[v] += scratch[:frames]
                st["click_tail"][v] = scratch[frames:]

        # Every voice's phase origin ages by the whole block, audible or
        # not (the oscillator's rule: activity gating never moves phase).
        phase_n += frames

        # --- percussion: one module-wide from-silence generator --------
        if perc_ratio is not None:
            cursor = 0
            fired_at = -1
            for e, v in sorted(all_rises):
                was_quiet = (
                    not st["prev_any"] if e == 0 else not any_high[e - 1]
                )
                if was_quiet and e != fired_at:
                    inc = perc_ratio * freqs[v] / sr
                    if inc < 0.5:
                        # Render the old strike up to the new one, then
                        # replace it (single generator).
                        self._organ_perc_pour(st, out[0], cursor, e, perc_g)
                        st["perc_amp"] = perc_level * level
                        st["perc_n"] = 0
                        st["perc_inc"] = inc
                        cursor = e
                        fired_at = e
            self._organ_perc_pour(st, out[0], cursor, frames, perc_g)
        elif st["perc_amp"] > 0.0:
            # Register switched off live: let the ringing strike finish.
            self._organ_perc_pour(st, out[0], 0, frames, perc_g)
        st["prev_any"] = bool(any_high[-1])

        # --- the scanner vibrato / chorus ------------------------------
        vib = str(p.get("vibrato", "off"))
        if vib not in ORGAN_VIBRATO:
            vib = "off"
        # The module clock runs whatever the knob says, so the motor
        # never stops: it is the scanner's phase AND its ring write
        # index, and a scanner switched in mid-note picks the sweep up
        # where it would have been instead of restarting at tap 0.
        n0 = int(st["n"])
        st["n"] = n0 + frames
        if vib != "off" or "scan_buf" in st:
            out = self._organ_scanner(st, out, vib, frames, sr, n0)

        out32 = out.astype(np.float32)
        return out32 if voiced else out32[0]

    def _organ_scanner(self, st, out, vib, frames, sr, n0):
        """The Hammond scanner over the finished voice sum ``out`` (V, F).

        A modulated fractional delay, the chorus/tape idiom: write the
        whole block into a per-voice ring, then read linear-interpolated
        taps ``delay(t)`` behind the write head -- as whole samples back
        (``ceil(delay)``) plus a fraction (``back - delay``), functions of
        the small delay alone, so the read's precision does not decay
        with the module clock's magnitude the way ``absidx - delay`` did
        (2026-09-24). There is no feedback, so every read lands on a
        sample already written, the whole read vectorises over (V, F),
        and the render is block-size exact (every setting pinned at 64 /
        128 / 512 / 1000 over 4 s at 48 kHz). The
        scanner phase is an integer sample counter (``n0``, the caller's
        module clock, which is also the write index) -- ``phase = (n * f
        / sr) % 1`` depends only on ``n``, so it is bit-exact across any
        block split where a float phase accumulator would not be. One
        counter for all voices: one scanner per console.

        That counter belongs to the CALLER and free-runs at ``off`` as
        well, so the sweep survives off -> on: the motor does not stop
        turning when the knob is down, and switching the scanner in
        under a held chord joins the sweep in progress rather than
        starting it from tap 0 (2026-09-22). Two organs given the same
        notes and switched on at different moments therefore render
        identically once the fade is done.

        ``delay = A * (1 + s) + margin`` -- the line starts at (almost)
        zero delay for every depth, the way the real pickup starts at
        tap 0, and ``A`` is the half-swing of the setting. ``s`` is the
        ROUNDED TRIANGLE the pickup really traces,
        ``arcsin(R*sin(2*pi*ph)) / arcsin(R)`` at ``R`` =
        ``_ORGAN_SCAN_ROUND``: a sine's corners pulled out towards a
        triangle's without ever reaching them, so the pitch deviation
        -- which is the sweep's slope -- is flat-topped instead of
        sinusoidal while every derivative stays continuous. ``A`` is
        scaled by ``arcsin(R)/R`` to hold the cents where the sine put
        them. Three values
        crossfade on any switch -- dry gain, wet gain, ``A`` -- along one
        integer-counted linear ramp (the gate-ramp idiom: ``from + (to -
        from) * count / R``, snapping to ``to`` exactly at ``R``). V
        settings target ``(0, 1, A)``, C settings ``(0.5, 0.5, A)``, and
        ``off`` targets ``(1, 0, A)`` -- the line then keeps running
        until the fade completes, at which point ``1.0 * dry + 0.0 * wet``
        IS the dry, bit for bit, and the state is dropped. A fresh line
        rests at ``(1, 0, A)`` too, so switching on from off fades the
        wet in over the zeroed ring rather than reading a hole.
        """
        V = out.shape[0]
        swings = self._ORGAN_SCAN_SWING_MS
        # arcsin(R)/R: the rounded triangle's peak slope is that much
        # shallower than a sine's, so the line swings that much wider
        # and the cents land where the sine left them.
        rnd = self._ORGAN_SCAN_ROUND
        slope = float(np.arcsin(rnd) / rnd)
        a_max = 0.5 * swings[-1] * sr / 1000.0 * slope
        margin = max(2.0, sr * self._ORGAN_SCAN_MARGIN_S)
        fade = max(1, int(round(sr * self._ORGAN_SCAN_FADE_S)))
        span = int(np.ceil(2.0 * a_max + margin)) + 4

        if vib != "off":
            a = 0.5 * swings[int(vib[1]) - 1] * sr / 1000.0 * slope
            g = 0.5 if vib[0] == "c" else 1.0
            target = np.array([1.0 - g, g, a], dtype=np.float64)
        else:
            target = None

        buf = st.get("scan_buf")
        if buf is None or buf.shape[1] < span + frames:
            # Sized for the deepest setting and a generous block, so
            # neither a depth change nor a short last block reallocates;
            # only a block LONGER than any before grows it (zeroed ring,
            # a one-off hole -- the app never changes block size
            # mid-run).
            L = span + max(frames, self._ORGAN_SCAN_MIN_BLOCK)
            buf = np.zeros((V, L), dtype=np.float64)
            rest = np.array(
                [1.0, 0.0, target[2] if target is not None else 0.0]
            )
            st["scan_buf"] = buf
            st["scan_cur"] = rest.copy()
            st["scan_from"] = rest.copy()
            st["scan_to"] = rest.copy()
            st["scan_count"] = fade
        if target is None:
            # Fade back to dry; the depth stays where it was.
            target = np.array([1.0, 0.0, float(st["scan_to"][2])])
        if not np.array_equal(target, st["scan_to"]):
            st["scan_from"] = st["scan_cur"].copy()
            st["scan_to"] = target
            st["scan_count"] = 0

        frm = st["scan_from"]
        to = st["scan_to"]
        n = np.arange(1, frames + 1, dtype=np.int64)
        cnt = np.minimum(fade, int(st["scan_count"]) + n)
        u = cnt / fade
        vals = np.where(
            cnt[None, :] >= fade,
            to[:, None],
            frm[:, None] + (to - frm)[:, None] * u[None, :],
        )  # (3, F): dry gain, wet gain, half-swing in samples
        st["scan_count"] = int(cnt[-1])
        st["scan_cur"] = vals[:, -1].copy()
        dry_g, wet_g, a_t = vals

        L = buf.shape[1]
        absidx = int(n0) + np.arange(frames, dtype=np.int64)
        ph = (absidx * (self._ORGAN_SCAN_HZ / sr)) % 1.0
        sweep = np.arcsin(rnd * np.sin(2.0 * np.pi * ph)) / np.arcsin(rnd)
        delay = a_t * (1.0 + sweep) + margin
        np.clip(delay, 2.0, float(L - 2), out=delay)

        buf[:, absidx % L] = out
        # Whole samples back and a fraction, both functions of the small
        # delay alone (``back - delay`` is exact by Sterbenz: the clip
        # keeps delay >= 2, so back <= 2 * delay). ``absidx - delay``
        # rounded at the module CLOCK's magnitude instead: the same at
        # any block size (the clock is absolute), but coarser the longer
        # the organ has been running -- a fraction quantised to 3e-8 of a
        # sample after an hour at 48 kHz, 5e-7 after a day.
        back = np.ceil(delay)
        frac = back - delay
        i0 = absidx - back.astype(np.int64)
        wet = buf[:, i0 % L] * (1.0 - frac) + buf[:, (i0 + 1) % L] * frac

        res = dry_g * out + wet_g * wet
        if vib == "off" and st["scan_count"] >= fade:
            for key in ("scan_buf", "scan_cur", "scan_from",
                        "scan_to", "scan_count"):
                st.pop(key, None)
        return res

    def _organ_perc_pour(self, st, row, start, end, perc_g) -> None:
        """Render the percussion strike into ``row[start:end]``, carrying
        the strike's amplitude and the integer count of samples since it
        fired. The envelope's first poured sample is the full strike
        amplitude (a strike fired at ``start`` sounds AT ``start``).

        Both the decay and the phase are functions of that absolute
        count — ``amp0 * g**k`` and ``(inc * k) % 1`` — never of a
        carried float: ``g**n1 * g**n2`` is not ``g**(n1 + n2)`` in
        binary, so a per-segment carry made the strike (and its
        1e-6 cut-off, which used to be tested once per pour) depend on
        where the block boundaries happened to fall. The cut-off is now
        per SAMPLE, so it lands on the same sample at any block size.
        """
        amp0 = float(st["perc_amp"])
        n = end - start
        if amp0 <= 1e-6 or n <= 0:
            if amp0 <= 1e-6:
                st["perc_amp"] = 0.0
            return
        k = float(st["perc_n"]) + np.arange(n, dtype=np.float64)
        env = amp0 * np.power(perc_g, k)
        env[env <= 1e-6] = 0.0
        ph = (st["perc_inc"] * k) % 1.0
        row[start:end] += np.sin(2.0 * np.pi * ph) * env
        st["perc_n"] = int(st["perc_n"]) + n
        if amp0 * (perc_g ** float(st["perc_n"])) <= 1e-6:
            st["perc_amp"] = 0.0  # nothing left to pour but zeros
