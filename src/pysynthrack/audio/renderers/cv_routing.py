"""CV / routing / utility renderers: vca, mixer, combiner, cv_combiner,
constant, cv_scale, cv_offset, cv_math, logic, audio_to_cv, cv_to_audio,
schmitt, cv_to_frequency, matrix_mixer, mid_side, autopan.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". Reached through ``self`` because other families share them,
so they live in the ``_shared`` mixin: the audio_to_cv envelope-follower engine
(``_ATC_*``, ``_audio_to_cv_block`` / ``_solve`` / ``_loop_mono`` /
``_loop_voice`` -- also used by the dynamics mixin, octaver and vocoder),
the oscillator waveshaper ``cv_to_frequency`` drives
(``_osc_waveshape``, in ``_waveshapes``), ``_vocoder_hp_coeffs`` (mid_side's bass-mono
highpass), and the generic ``_finite_mean`` / ``_pow2_clipped`` /
``_voice_mean`` / ``_GATE_HIGH``; autopan's clock sync comes from the
modfx mixin. The static ``_cv_to_hz_mapped`` calls
``NumpyBackend._cv_to_hz`` through a lazy import, once per block
(``numpy_backend`` imports this module, so a top-level import would be
circular).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter


class CVRoutingRenderers:
    # ----- MatrixMixer rendering -------------------------------------------

    def _render_matrix_mixer(self, module, frames: int, buffers, patch) -> dict:
        """4×4 bipolar gain matrix (see modules/matrix_mixer.py).

        ``out_c = clip(cv_c · Σ_r g_rc · in_r)``. Unpatched rows
        contribute nothing; an all-unpatched column is exact zeros. The
        identity default with nothing else patched is a bit-exact
        4-channel pass (1.0·x + nothing = x, and the soft ceiling is
        transparent below the knee). Mixed mono/(V, F) rows broadcast;
        the feedback plumbing (late-read seeding) lives in
        ``render_block_multi``/``_compute_late_edges``, not here — by
        the time this runs, every input buffer is defined.

        The ``soft_clip`` ceiling: identity below MATRIX_CLIP_KNEE,
        then ``knee + (1−knee)·tanh((|x|−knee)/(1−knee))`` — C1 at the
        knee (unit slope), saturating at exactly 1.0. A plain tanh
        would take 8 % off a 0.5 signal, unacceptable for a default-on
        mixer stage (deviation from the spec's literal "tanh on each
        out", noted in the worklog).
        """
        from ...modules.matrix_mixer import MATRIX_CLIP_KNEE, MATRIX_SIZE

        p = module.params
        soft = bool(p.get("soft_clip", True))

        ins = []
        for r in range(1, MATRIX_SIZE + 1):
            buf = self._input_buffer(
                patch, buffers, module.id, f"in_{r}", collapse=False
            )
            ins.append(None if buf is None else buf.astype(np.float64))
        out: dict[str, np.ndarray] = {}
        for c in range(1, MATRIX_SIZE + 1):
            acc = None
            for r in range(1, MATRIX_SIZE + 1):
                x = ins[r - 1]
                if x is None:
                    continue
                try:
                    g = float(p.get(f"g{r}{c}", 0.0))
                except (TypeError, ValueError):
                    g = 0.0
                g = min(1.0, max(-1.0, g))
                if g == 0.0:
                    continue
                term = x * g
                acc = term if acc is None else acc + term
            if acc is None:
                out[f"out_{c}"] = np.zeros(frames, dtype=np.float32)
                continue
            cv = self._input_buffer(
                patch, buffers, module.id, f"cv_{c}", collapse=False
            )
            if cv is not None:
                acc = acc * cv.astype(np.float64)
            if soft:
                knee = MATRIX_CLIP_KNEE
                over = np.abs(acc) > knee
                if over.any():
                    mag = np.abs(acc)
                    limited = knee + (1.0 - knee) * np.tanh(
                        (mag - knee) / (1.0 - knee)
                    )
                    acc = np.where(over, np.sign(acc) * limited, acc)
            out[f"out_{c}"] = acc.astype(np.float32)
        return out

    # ----- VCA rendering --------------------------------------------------

    def _render_vca(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Voltage-controlled amplifier: out = audio * cv * gain.

        Missing audio in → silence. Missing CV in → passthrough at unity
        (so a VCA with no envelope still behaves like a gain stage).

        Voice-aware: opts into ``collapse=False`` on both inputs so a
        polyphonic ADSR -> VCA -> Speaker chain preserves per-voice
        envelope identity. Numpy broadcasting handles every shape
        combination correctly:

          * (V, F) audio  × (V, F) cv → (V, F) element-wise.
          * (V, F) audio  × (F,)  cv → (V, F) — mono CV broadcasts
                                       across every voice (e.g. a
                                       channel-wide aftertouch VCA).
          * (F,)  audio   × (V, F) cv → (V, F) — mono audio sliced
                                       into voices by per-voice CV.
          * (F,)  audio   × (F,)  cv → (F,) mono fast path.

        VCA is stateless, so there's no per-voice state to track —
        broadcasting is the entire migration.
        """
        audio_in = self._input_buffer(
            patch, buffers, module.id, "audio", collapse=False
        )
        if audio_in is None:
            return np.zeros(frames, dtype=np.float32)
        cv_in = self._input_buffer(
            patch, buffers, module.id, "cv", collapse=False
        )
        gain = float(module.params.get("gain", 1.0))
        if cv_in is None:
            return (audio_in * gain).astype(np.float32)
        return (audio_in * cv_in * gain).astype(np.float32)

    def _render_audio_to_cv(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Envelope follower: rectify input + asymmetric one-pole smoothing.

        Coefficients are derived from time constants:

            coef = 1 - exp(-1 / (time_seconds * sample_rate))

        A target rising above the current level uses ``attack_coef``;
        a target below uses ``release_coef``. Zero or negative time
        constants are clamped to "instant" (coef = 1.0).

        The smoother's state feeds back into the next sample, and which
        coefficient applies depends on comparing the input against that
        evolving state — so unlike the biquads this is not expressible
        as one ``lfilter`` call. Both shape branches instead run the
        vectorized fixed-point solve in :meth:`_audio_to_cv_block`
        (details there), with the original per-sample loop kept as the
        fallback for degenerate coefficients.

        Voice-aware. Branches on the audio input's ``ndim``:

          * 1D ``(F,)`` audio -> scalar smoother state, output ``(F,)``.
          * 2D ``(V, F)`` audio -> per-voice smoother state stored as a
            length-V vector, output ``(V, F)``.

        Missing audio in -> silence out and the smoother state is left
        as-is (so reconnecting the cable doesn't snap back from a stale
        decayed level mid-transient).
        """
        audio_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if audio_in is None:
            return np.zeros(frames, dtype=np.float32)

        attack_ms = float(module.params.get("attack_ms", 5.0))
        release_ms = float(module.params.get("release_ms", 100.0))
        gain = float(module.params.get("gain", 1.0))

        sr = self.sample_rate
        attack_coef = 1.0 if attack_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(attack_ms, 1e-6) * 1e-3 * sr))
        )
        release_coef = 1.0 if release_ms <= 0.0 else 1.0 - float(
            np.exp(-1.0 / (max(release_ms, 1e-6) * 1e-3 * sr))
        )

        if audio_in.ndim == 2:
            return self._render_audio_to_cv_voice(
                module, frames, audio_in, attack_coef, release_coef, gain
            )
        return self._render_audio_to_cv_mono(
            module, frames, audio_in, attack_coef, release_coef, gain
        )

    def _render_audio_to_cv_mono(
        self, module, frames, audio_in, attack_coef, release_coef, gain
    ):
        """Scalar follower state, single smoother. Output ``(F,)``.

        Runs the shared block solve on a one-row view; falls back to
        the per-sample loop when the solve declines (degenerate
        coefficients, non-finite input, or a hypothetical pattern
        non-convergence). Equivalence with the old loop is pinned by
        ``TestAudioToCVBlockEquivalence`` against a verbatim oracle.
        """
        state = self._state.setdefault(module.id, {"level": 0.0})
        # Discard voice-branch state if we previously rendered (V, F).
        if "level_arr" in state:
            state.clear()
            state["level"] = 0.0

        level = float(state["level"])
        abs_in = np.abs(audio_in).astype(np.float64)
        y = self._audio_to_cv_block(
            abs_in[None, :],
            np.array([level], dtype=np.float64),
            attack_coef,
            release_coef,
        )
        if y is None:
            out64, level = self._audio_to_cv_loop_mono(
                abs_in, level, attack_coef, release_coef
            )
        else:
            out64 = y[0]
            level = float(out64[-1]) if frames else level
        state["level"] = float(level)
        out = out64.astype(np.float32)
        return (out * gain).astype(np.float32)

    def _render_audio_to_cv_voice(
        self, module, frames, audio_in, attack_coef, release_coef, gain
    ):
        """Per-voice follower state. Output ``(V, F)``.

        ``audio_in`` is ``(V, F)``. The shared block solve handles all
        voices at once (independent rows, one pattern array); the old
        sample-loop-with-voice-vectorized-steps survives only as the
        fallback for degenerate coefficients.
        """
        V = audio_in.shape[0]
        state = self._state.setdefault(module.id, {})

        needs_reinit = (
            "level_arr" not in state or state["level_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["level_arr"] = np.zeros(V, dtype=np.float64)

        level = state["level_arr"]  # (V,)
        abs_in = np.abs(audio_in).astype(np.float64)
        y = self._audio_to_cv_block(abs_in, level, attack_coef, release_coef)
        if y is None:
            out64, level = self._audio_to_cv_loop_voice(
                abs_in, level, attack_coef, release_coef
            )
        else:
            out64 = y
            if frames:
                level = y[:, -1].copy()
        state["level_arr"] = level
        out = out64.astype(np.float32)
        return (out * gain).astype(np.float32)

    # ----- CVToAudio rendering --------------------------------------------

    def _render_cv_to_audio(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Signal-kind relabel: CV input -> audio output, optional gain.

        Stateless. The patch model forbids ``cv -> audio`` cables, so
        this module exists purely to satisfy the type system; the DSP
        is a buffer copy (multiplied by ``gain``).

        Voice-awareness is by shape preservation. The CV input arrives
        via :meth:`_input_buffer` with ``collapse=False`` so a
        ``(V, F)`` polyphonic CV (e.g. per-voice ADSR) reaches us with
        its voice axis intact, and the output keeps the same shape.
        Downstream Speaker drain collapses the voice axis at the
        mono boundary like it does for any other voice-aware audio.

        Missing cable -> silence (1D ``(F,)`` zeros). We can't know
        the intended voice count without an input, so the un-patched
        case always emits mono.

        No DC blocking. A constant CV (e.g. an ADSR's sustain level)
        produces a DC offset that the Speaker limiter clamps -- the
        user is trusted to patch a high-pass module if they need one.
        """
        cv_in = self._input_buffer(
            patch, buffers, module.id, "cv", collapse=False
        )
        if cv_in is None:
            return np.zeros(frames, dtype=np.float32)
        gain = float(module.params.get("gain", 1.0))
        return (cv_in * gain).astype(np.float32)

    # ----- CVToFrequency rendering ----------------------------------------

    # ----- Schmitt rendering ------------------------------------------------

    def _render_schmitt(self, module, frames: int, buffers, patch) -> np.ndarray:
        """CV → gate Schmitt trigger with hysteresis.

        Rising through ``high`` (strict >) sets the gate; falling
        through ``low`` (strict <) clears it; inside the band the gate
        holds its previous state — the hysteresis that makes a wobbly
        CV usable as a clock without chatter.

        Vectorized by event forward-fill (no per-sample loop): each
        sample is classified +1 (above high), -1 (below low) or 0
        (deadband); the gate at sample n is "was the most recent
        nonzero event a +1", seeded with the carried cross-block
        state via ``np.maximum.accumulate`` over event positions.

        Shape-polymorphic on the CV input's ndim per the voice-aware
        convention: ``(F,)`` in → ``(F,)`` out with scalar held state;
        ``(V, F)`` in → ``(V, F)`` out with per-voice held state.
        Unpatched input emits a constant-low gate. Output is float32
        0.0 / 1.0 (comfortably astride the backend's ``_GATE_HIGH``).
        """
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )

        high = float(module.params.get("high", 0.6))
        # An inverted pair degenerates to a plain comparator at high.
        low = min(float(module.params.get("low", 0.4)), high)

        state = self._state.setdefault(module.id, {"gate": False})

        if cv_in is None:
            return np.zeros(frames, dtype=np.float32)

        if cv_in.ndim == 2:
            V = cv_in.shape[0]
            needs_reinit = (
                "gate_arr" not in state or state["gate_arr"].shape[0] != V
            )
            if needs_reinit:
                state.clear()
                state["gate_arr"] = np.zeros(V, dtype=bool)
            prev = state["gate_arr"][:, None]  # (V, 1)

            ev = np.where(cv_in > high, 1, np.where(cv_in < low, -1, 0))
            pos = np.where(ev != 0, np.arange(frames)[None, :], -1)
            last = np.maximum.accumulate(pos, axis=1)  # (V, F)
            picked = np.take_along_axis(ev, np.maximum(last, 0), axis=1)
            gate = np.where(last >= 0, picked > 0, prev)

            state["gate_arr"] = gate[:, -1].copy()
            return gate.astype(np.float32)

        # Mono path. Discard voice-shaped state if the input collapsed.
        if "gate_arr" in state:
            state.clear()
            state["gate"] = False
        prev_gate = bool(state["gate"])

        ev = np.where(cv_in > high, 1, np.where(cv_in < low, -1, 0))
        pos = np.where(ev != 0, np.arange(frames), -1)
        last = np.maximum.accumulate(pos)
        gate = np.where(last >= 0, ev[np.maximum(last, 0)] > 0, prev_gate)

        state["gate"] = bool(gate[-1])
        return gate.astype(np.float32)

    def _render_cv_to_frequency(
        self, module, frames: int, buffers, patch
    ) -> np.ndarray:
        """Self-contained CV-controlled oscillator with three-point Hz map.

        Maps the incoming CV (clamped to [0, 1] in phase 1) to a per-
        sample instantaneous frequency via a piecewise interpolation
        between three anchor points: ``f0`` at CV=0, ``fm`` at CV=0.5,
        ``f1`` at CV=1.0. The ``mode`` param picks log-Hz interpolation
        (equal-octave splits, musical default) or linear-Hz (equal-Hz
        splits, deliberately bent). Phase is integrated from that
        instantaneous frequency via cumsum -- the same trick the
        Oscillator's freq_cv path uses, just applied to a different
        CV→Hz function.

        Shape-polymorphic on the CV input:
          * No CV cable or 1D ``(F,)`` CV -> mono path, single phase
            accumulator, output ``(F,)``. Unpatched CV falls back to
            the ``freq`` param (Oscillator-style behaviour — the
            module is a sound source, so it always produces sound).
          * 2D ``(V, F)`` CV -> voice-aware path, V independent phase
            accumulators (one per voice slot), output ``(V, F)``.

        Bipolar CV (phase 2, 2026-06-07): with ``negative_enabled``,
        CV in [-1, 0) maps through an independent mirror curve --
        ``f0_neg`` at CV=0, ``fm_neg`` at CV=-0.5, ``f1_neg`` at
        CV=-1.0 -- with its own ``mode_neg``. CV exactly 0 belongs to
        the positive side; zero-crossing continuity is the user's
        choice (f0 == f0_neg for smooth, different for a step). When
        disabled (default), bipolar CV clamps to [0, 1] exactly as
        phase 1 shipped.
        """
        # collapse=False so a voice-aware (V, F) CV reaches us with the
        # voice axis intact.
        cv_in = self._input_buffer(
            patch, buffers, module.id, "cv", collapse=False
        )

        f0 = float(module.params.get("f0", 110.0))
        fm = float(module.params.get("fm", 440.0))
        f1 = float(module.params.get("f1", 1760.0))
        freq_fallback = float(module.params.get("freq", 440.0))
        waveform = str(module.params.get("waveform", "sine"))
        mode = str(module.params.get("mode", "log"))
        pos = (f0, fm, f1, mode)

        # Phase 2: independent negative-side curve, opt-in. ``neg`` is
        # None when disabled, which keeps the phase-1 [0, 1] clamp.
        neg = None
        if bool(module.params.get("negative_enabled", False)):
            neg = (
                float(module.params.get("f0_neg", f0)),
                float(module.params.get("fm_neg", 440.0)),
                float(module.params.get("f1_neg", 1760.0)),
                str(module.params.get("mode_neg", "log")),
            )

        if cv_in is not None and cv_in.ndim == 2:
            return self._render_cv_to_frequency_voice(
                module, frames, cv_in, pos, neg, waveform
            )
        return self._render_cv_to_frequency_mono(
            module, frames, cv_in, pos, neg, freq_fallback, waveform
        )

    @staticmethod
    def _cv_to_hz(cv, f0, fm, f1, mode):
        """Piecewise interpolation of CV in [0, 1] to Hz via (f0, fm, f1).

        Shape-preserving: ``cv`` of any shape comes back as Hz of the
        same shape. Clamps the input to [0, 1] internally so callers
        don't need to.

        Lower segment (cv in [0, 0.5]): t = cv*2, blend f0->fm.
        Upper segment (cv in [0.5, 1.0]): t = (cv-0.5)*2, blend fm->f1.

        Log mode interpolates in log2-Hz so equal CV steps -> equal
        octave steps. Linear mode interpolates literal Hz.
        """
        cv = np.clip(cv.astype(np.float64), 0.0, 1.0)
        lower = cv < 0.5
        t = np.where(lower, cv * 2.0, (cv - 0.5) * 2.0)
        if mode == "log":
            # Guard log2 against zero/negative anchor values from the
            # user; clamp to 1e-6 Hz minimum (well below audible).
            lf0 = np.log2(max(f0, 1e-6))
            lfm = np.log2(max(fm, 1e-6))
            lf1 = np.log2(max(f1, 1e-6))
            log_hz = np.where(
                lower,
                lf0 + t * (lfm - lf0),
                lfm + t * (lf1 - lfm),
            )
            return np.power(2.0, log_hz)
        # linear (default fallback for any unknown mode string)
        return np.where(
            lower,
            f0 + t * (fm - f0),
            fm + t * (f1 - fm),
        )

    @staticmethod
    def _cv_to_hz_mapped(cv, pos, neg):
        """Sign-aware CV→Hz dispatch.

        ``pos`` and ``neg`` are ``(f0, fm, f1, mode)`` tuples. With
        ``neg`` None (negative_enabled False) this is exactly the
        phase-1 positive mapping and its internal [0, 1] clamp.
        Otherwise cv >= 0 maps through ``pos`` and cv < 0 maps through
        ``neg`` on |cv|, so the negative anchors read naturally:
        f0_neg at CV=0⁻, fm_neg at CV=-0.5, f1_neg at CV=-1.0, and CV
        below -1 clamps to f1_neg via the shared [0, 1] clamp on the
        mirrored value.
        """
        from ..numpy_backend import NumpyBackend  # lazy: it imports this module

        if neg is None:
            return NumpyBackend._cv_to_hz(cv, *pos)
        cv64 = cv.astype(np.float64)
        pos_hz = NumpyBackend._cv_to_hz(cv64, *pos)
        neg_hz = NumpyBackend._cv_to_hz(-cv64, *neg)
        return np.where(cv64 >= 0.0, pos_hz, neg_hz)

    def _render_cv_to_frequency_mono(
        self, module, frames, cv_in, pos, neg, freq_fallback, waveform
    ):
        """Mono path -- single phase accumulator, output ``(F,)``.

        With no CV patched, the static ``freq`` param drives a vector-
        ized phase ramp (constant inc, arange). With CV patched, the
        per-sample CV is mapped to instantaneous Hz and phase is
        integrated via cumsum.
        """
        state = self._state.setdefault(module.id, {"phase": 0.0})
        # Discard voice-shaped state if it leaked over from a previous
        # voice-branch call on the same module id.
        if "phase_arr" in state:
            state.clear()
            state["phase"] = 0.0

        sr = self.sample_rate
        start_phase = state["phase"]

        if cv_in is None:
            # No CV cable -> static fallback frequency, vectorized ramp.
            phase_inc = freq_fallback / sr
            phases = (
                start_phase + np.arange(frames, dtype=np.float64) * phase_inc
            ) % 1.0
            state["phase"] = (start_phase + frames * phase_inc) % 1.0
            dt = phase_inc
        else:
            inst_freq = self._cv_to_hz_mapped(cv_in, pos, neg)  # (F,)
            inst_inc = inst_freq / sr
            phases = (start_phase + np.cumsum(inst_inc)) % 1.0
            state["phase"] = float(phases[-1])
            dt = inst_inc

        wave = self._osc_waveshape(phases, waveform, dt=dt)
        return wave.astype(np.float32)

    def _render_cv_to_frequency_voice(
        self, module, frames, cv_in, pos, neg, waveform
    ):
        """Voice-aware path -- V independent phase accumulators.

        ``cv_in`` is ``(V, F)``. Each voice slot integrates its own
        phase via per-row cumsum. Output is ``(V, F)``. Per-voice
        phase state persists across blocks so a slot that briefly
        goes silent (CV=0 -> f0) and comes back doesn't restart from
        zero phase mid-cycle.

        Silent slots advance at f0 (since cv=0 maps to f0); downstream
        VCA/ADSR gating silences those voices in practice. Same
        harmless behaviour as the Oscillator's voice path.
        """
        V = cv_in.shape[0]
        state = self._state.setdefault(module.id, {})

        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.zeros(V, dtype=np.float64)

        sr = self.sample_rate
        start_phase = state["phase_arr"]  # (V,)

        inst_freq = self._cv_to_hz_mapped(cv_in, pos, neg)  # (V, F)
        inst_inc = inst_freq / sr
        phases = (
            start_phase[:, None] + np.cumsum(inst_inc, axis=1)
        ) % 1.0  # (V, F)
        state["phase_arr"] = phases[:, -1].copy()

        wave = self._osc_waveshape(phases, waveform, dt=inst_inc)  # (V, F)
        return wave.astype(np.float32)

    # ----- Mixer rendering ------------------------------------------------

    def _render_mixer(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sum four audio inputs with per-channel gain trims and a master.

        Unconnected channels contribute silence. The signal is::

            out = master * sum_i (gain_i * cv_i * input_i)

        where ``cv_i`` is the channel's optional ``gain{i}_cv`` input
        (unpatched -> unity). The CV multiplies **per sample**, VCA-style
        — the CV *is* the channel's amplitude (knobless by the house
        rule, like ``vca.cv``), so an ADSR into ``gain2_cv`` swells
        channel 2 and a sequencer lane steps channels in and out.

        Output is clipped at the speaker stage, not here — so a hot
        mixer feeding a filter still has the headroom the filter needs.
        """
        master = float(module.params.get("master", 0.7))
        out = np.zeros(frames, dtype=np.float32)
        for idx in (1, 2, 3, 4):
            buf = self._input_buffer(patch, buffers, module.id, f"in{idx}")
            if buf is None:
                continue
            gain = float(module.params.get(f"gain{idx}", 1.0))
            ch = buf * gain
            cv = self._input_buffer(patch, buffers, module.id, f"gain{idx}_cv")
            if cv is not None and cv.size > 0:
                ch = ch * cv
            out += ch.astype(np.float32)
        return (out * master).astype(np.float32)

    # ----- Combiner rendering ---------------------------------------------

    def _render_combiner(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sum up to four audio inputs at unit gain. Unconnected = silence."""
        out = np.zeros(frames, dtype=np.float32)
        for idx in (1, 2, 3, 4):
            buf = self._input_buffer(patch, buffers, module.id, f"in{idx}")
            if buf is None:
                continue
            out += buf.astype(np.float32)
        return out

    # ----- CVCombiner rendering -------------------------------------------

    def _render_cv_combiner(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Combine up to four CV signals into one.

        ``mode="sum"`` (default) is the analog-modular convention — stacks
        adding linearly. ``mode="average"`` divides by the *connected*
        input count so blending modulators doesn't double the depth.
        """
        mode = str(module.params.get("mode", "sum"))
        out = np.zeros(frames, dtype=np.float32)
        count = 0
        for idx in (1, 2, 3, 4):
            buf = self._input_buffer(patch, buffers, module.id, f"in{idx}")
            if buf is None:
                continue
            out += buf.astype(np.float32)
            count += 1
        if mode == "average" and count > 0:
            out /= float(count)
        return out

    # ----- CV-utility trio (Constant / CVScale / CVOffset) ----------------

    def _render_constant(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Emit a steady CV level -- a hand-dialed DC source.

        No inputs; fills the block with the scalar ``value`` param.
        Always mono ``(frames,)``: a constant has no voice context of
        its own, and a 1D CV broadcasts cleanly against any per-voice
        ``(V, frames)`` consumer downstream.
        """
        value = float(module.params.get("value", 1.0))
        return np.full(frames, value, dtype=np.float32)

    def _render_cv_scale(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Multiply a CV by a fixed factor: ``out = in * scale``.

        Pure pointwise gain, so shape-polymorphic for free -- ``collapse=
        False`` keeps a voice-aware ``(V, F)`` input intact and a mono
        ``(F,)`` input stays mono. An unpatched input is treated as 0,
        so the output is silence (``0 * scale == 0``).
        """
        scale = float(module.params.get("scale", 1.0))
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if cv_in is None:
            return np.zeros(frames, dtype=np.float32)
        return (cv_in * scale).astype(np.float32)

    def _render_cv_offset(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Add a fixed DC level to a CV: ``out = in + offset``.

        Pure pointwise shift, shape-polymorphic for free. An unpatched
        input is treated as 0, so the output is a constant ``offset``
        (mono) -- which makes an unpatched CVOffset a quick DC source.
        A voice-aware ``(V, F)`` input keeps its shape, the scalar
        ``offset`` broadcasting across the voice axis.
        """
        offset = float(module.params.get("offset", 0.0))
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if cv_in is None:
            return np.full(frames, offset, dtype=np.float32)
        return (cv_in + offset).astype(np.float32)

    def _render_cv_math(self, module, frames: int, buffers, patch) -> dict:
        """Two-in CV algebra (see modules/cv_math.py).

        Pure elementwise math, stateless, exact. An unpatched operand
        reads 0 (so ``max``/``min`` of a lone ``a`` are its half-wave
        rectifiers -- the normalled trick, documented). Shape-polymorphic:
        operands are fetched with ``collapse=False`` and a mono partner
        broadcasts across a ``(V, F)`` one; outputs take the voiced shape
        if either operand is voiced, else mono.
        """
        a_in = self._input_buffer(patch, buffers, module.id, "a", collapse=False)
        b_in = self._input_buffer(patch, buffers, module.id, "b", collapse=False)
        if a_in is None and b_in is None:
            z = np.zeros(frames, dtype=np.float32)
            return {k: z for k in ("min", "max", "avg", "diff", "mult", "rect", "inv")}
        a = a_in.astype(np.float32, copy=False) if a_in is not None else np.zeros(frames, dtype=np.float32)
        b = b_in.astype(np.float32, copy=False) if b_in is not None else np.zeros(frames, dtype=np.float32)
        voiced = a.ndim == 2 or b.ndim == 2
        if voiced:
            if a.ndim == 1:
                a = a[None, :]
            if b.ndim == 1:
                b = b[None, :]
            if a.shape[0] != b.shape[0] and a.shape[0] != 1 and b.shape[0] != 1:
                # Two voiced operands of different widths: line them up on
                # the smaller count (the extra rows have no partner).
                v = min(a.shape[0], b.shape[0])
                a, b = a[:v], b[:v]
        out = {
            "min": np.minimum(a, b),
            "max": np.maximum(a, b),
            "avg": (a + b) * np.float32(0.5),
            "diff": a - b,
            "mult": a * b,
            "rect": np.abs(a),
            "inv": -a,
        }
        if voiced:
            # A mono-only-a function still comes back voiced-shaped when
            # the other operand is voiced, so every jack agrees on width.
            width = max(a.shape[0], b.shape[0])
            for k, arr in out.items():
                if arr.shape[0] != width:
                    out[k] = np.broadcast_to(arr, (width, frames)).copy()
        return {k: np.ascontiguousarray(v, dtype=np.float32) for k, v in out.items()}

    def _render_logic(self, module, frames: int, buffers, patch) -> dict:
        """Two-in gate algebra (see modules/logic.py).

        Pure elementwise boolean math on thresholded inputs — stateless,
        vectorized, exact. An unpatched operand reads low, which makes
        ``nand``/``not_a`` idle high (the normalled-NAND trick,
        documented). A voice-aware gate source collapses on fetch
        (any-voice-high after the house sum).
        """
        a_in = self._input_buffer(patch, buffers, module.id, "a")
        b_in = self._input_buffer(patch, buffers, module.id, "b")
        thresh = self._GATE_HIGH
        a = (a_in > thresh) if a_in is not None else np.zeros(frames, dtype=bool)
        b = (b_in > thresh) if b_in is not None else np.zeros(frames, dtype=bool)
        and_ = a & b
        return {
            "and": and_.astype(np.float32),
            "or": (a | b).astype(np.float32),
            "xor": (a ^ b).astype(np.float32),
            "nand": (~and_).astype(np.float32),
            "not_a": (~a).astype(np.float32),
        }

    # mid_side ``side_hp`` (bass mono): the corner of the side-only
    # highpass is held to this range when the param is on; 0 is off.
    _MID_SIDE_HP_MIN_HZ = 20.0
    _MID_SIDE_HP_MAX_HZ = 500.0

    def _mid_side_side_hp(self, module_id, freq, side):
        """One block of the bass-mono highpass over the side signal.

        The RBJ highpass (Q 0.707, the shared ``_vocoder_hp_coeffs``
        design -- 12 dB/oct, the mastering norm) with its coefficients
        cached under the module's state and rebuilt only when ``freq``
        moves. The carried state is the raw DF-I history (x1, x2, y1,
        y2), not lfilter's ``zf``: a live corner change then applies
        the new coefficients to the same last two samples (DF-I
        semantics, no restart click). The history is folded into the
        transposed-DF-II ``zi`` in scipy's OWN association order,
        ``(b1*x1 + (b2*x2 - a2*y2)) - a1*y1`` -- that, and not the
        house ``b1*x1 + b2*x2 - a1*y1 - a2*y2`` (off by ~1e-13 in
        float64), is what makes a 64-block render bit-exact with a
        512-block one.
        """
        st = self._state.setdefault(module_id, {})
        if st.get("hp_freq") != freq:
            st["hp_b"], st["hp_a"] = self._vocoder_hp_coeffs(freq)
            st["hp_freq"] = freq
            st.setdefault("hp_hist", (0.0, 0.0, 0.0, 0.0))
        n = side.shape[0]
        if n == 0:
            return side
        b = st["hp_b"]
        a = st["hp_a"]
        x1, x2, y1, y2 = st["hp_hist"]
        zi = np.array(
            [
                (b[1] * x1 + (b[2] * x2 - a[2] * y2)) - a[1] * y1,
                b[2] * x1 - a[2] * y1,
            ],
            dtype=np.float64,
        )
        out = lfilter(b, a, side, zi=zi)[0]
        if n >= 2:
            st["hp_hist"] = (
                float(side[-1]), float(side[-2]), float(out[-1]), float(out[-2])
            )
        else:
            st["hp_hist"] = (float(side[-1]), x1, float(out[-1]), y1)
        return out

    # ----- Autopan rendering ----------------------------------------------

    def _render_autopan(self, module, frames: int, buffers, patch) -> dict:
        """Panner + LFO: ``in_l``/``in_r`` -> ``out_l``/``out_r``.

        ONE input cabled (either jack) is a mono source PLACED by ``law``
        (power = the stereo sink's ``(cos, sin)`` of ``(p+1)*pi/4``;
        linear = ``((1-p)/2, (1+p)/2)``; compromise = their geometric
        mean); BOTH cabled is a stereo pair BALANCED with the sink's
        cosine taper (unity at centre, so pan 0 depth 0 is the inputs to
        the bit). The position is ``clip(pan + pan_cv + depth*lfo, -1,
        1)`` per sample; ``tremolo`` makes the right side read the LFO
        ``tremolo/2`` cycles later than the left (1 = the two gains
        equal: a mono tremolo).

        The LFO phase is keyed to an ABSOLUTE sample count -- ``(phi_a +
        (n - n_a) * rate/sr) mod 1``, re-anchored only when the
        effective rate changes -- so a steady-rate render is the same
        float at sample n whatever the block size (the integer-tick
        lesson; nothing accumulates). A locked ``clock`` hands the phase
        to :meth:`_mod_clock_sync`; while it holds, the free-running
        anchor follows the locked phase so an unpatch carries on from
        where the sweep was. The square is ``sin(pi/2 * clip(K*tri))``
        with ``K = 1/(2*rate*T)``: a raised-cosine glide of
        ``AUTOPAN_SQUARE_EDGE_S`` between sides at every rate.
        """
        from ...modules.autopan import (
            AUTOPAN_LAWS,
            AUTOPAN_RATE_MAX,
            AUTOPAN_RATE_MIN,
            AUTOPAN_SHAPES,
            AUTOPAN_SQUARE_EDGE_S,
        )

        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out_l": e, "out_r": e.copy()}

        params = module.params

        def fparam(name, default, lo, hi):
            try:
                v = float(params.get(name, default))
            except (TypeError, ValueError):
                v = default
            if not math.isfinite(v):
                v = default
            return min(max(v, lo), hi)

        sr = self.sample_rate
        pan = fparam("pan", 0.0, -1.0, 1.0)
        depth = fparam("depth", 0.7, 0.0, 1.0)
        rate = fparam("rate", 0.5, AUTOPAN_RATE_MIN, AUTOPAN_RATE_MAX)
        tremolo = fparam("tremolo", 0.0, 0.0, 1.0)
        division = fparam("division", 4.0, self._MOD_DIV_MIN, self._MOD_DIV_MAX)
        cv_depth = fparam("cv_depth", 1.0, -16.0, 16.0)
        shape = str(params.get("shape", "sine"))
        if shape not in AUTOPAN_SHAPES:
            shape = "sine"
        law = str(params.get("law", "power"))
        if law not in AUTOPAN_LAWS:
            law = "power"

        state = self._state.setdefault(module.id, {})
        # The absolute sample index of this block's first sample. Read it
        # BEFORE _mod_clock_sync, which owns the counter and advances it.
        base = int(state.get("samples", 0))

        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        if rate_cv is not None and rate_cv.size > 0:
            rate = rate * self._pow2_clipped(cv_depth * self._finite_mean(rate_cv))
        rate = min(max(rate, AUTOPAN_RATE_MIN), AUTOPAN_RATE_MAX)
        inc = rate / sr

        # Free-running phase: an anchor (sample, phase) and the increment
        # in force since it. A new rate re-anchors at this block's start.
        if "fr_n" not in state:
            state["fr_n"] = base
            state["fr_ph"] = 0.0
            state["fr_inc"] = inc
        elif state["fr_inc"] != inc:
            state["fr_ph"] = (
                state["fr_ph"] + (base - state["fr_n"]) * state["fr_inc"]
            ) % 1.0
            state["fr_n"] = base
            state["fr_inc"] = inc
        an = int(state["fr_n"])
        aph = float(state["fr_ph"])
        n = (base - an) + np.arange(frames, dtype=np.float64)
        free = (aph + n * inc) % 1.0

        sync = self._mod_clock_sync(
            module, frames, buffers, patch, state, division, free
        )
        if sync is None:
            ph = free
            eff_rate = rate
        else:
            ph, end_phase = sync
            eff_rate = sr / float(state["period"])
            state["fr_n"] = base + frames
            state["fr_ph"] = float(end_phase)
            state["fr_inc"] = inc

        if shape == "square":
            sq_k = max(1.0, 1.0 / (2.0 * eff_rate * AUTOPAN_SQUARE_EDGE_S))

        def lfo(phs):
            if shape == "sine":
                return np.sin(2.0 * np.pi * phs)
            tri = 1.0 - 4.0 * np.abs(((phs + 0.25) % 1.0) - 0.5)
            if shape == "triangle":
                return tri
            return np.sin((0.5 * np.pi) * np.clip(sq_k * tri, -1.0, 1.0))

        centre = pan
        pan_cv = self._input_buffer(
            patch, buffers, module.id, "pan_cv", collapse=False
        )
        if pan_cv is not None and pan_cv.size > 0:
            c = pan_cv.astype(np.float64)
            c = np.where(np.isfinite(c), c, 0.0)
            if c.ndim == 2:
                c = self._voice_mean(c)
            centre = pan + c

        pos_l = np.clip(centre + depth * lfo(ph), -1.0, 1.0)
        if tremolo > 0.0:
            pos_r = np.clip(
                centre + depth * lfo((ph + 0.5 * tremolo) % 1.0), -1.0, 1.0
            )
        else:
            pos_r = pos_l

        cabled = {c.dst_port for c in patch.cables_into(module.id)}
        l_on = "in_l" in cabled
        r_on = "in_r" in cabled
        if not (l_on or r_on):
            z = np.zeros(frames, dtype=np.float32)
            return {"out_l": z, "out_r": z.copy()}

        def audio(port):
            buf = self._input_buffer(patch, buffers, module.id, port)
            if buf is None:
                return np.zeros(frames, dtype=np.float64)
            return buf.astype(np.float64)

        if l_on and r_on:
            # Stereo pair: balance, the stereo sink's cosine taper.
            g_l = np.cos(np.maximum(pos_l, 0.0) * (0.5 * np.pi))
            g_r = np.cos(np.maximum(-pos_r, 0.0) * (0.5 * np.pi))
            x_l = audio("in_l")
            x_r = audio("in_r")
        else:
            x_l = x_r = audio("in_l" if l_on else "in_r")
            if law == "linear":
                g_l = 0.5 * (1.0 - pos_l)
                g_r = 0.5 * (1.0 + pos_r)
            else:
                g_l = np.cos((pos_l + 1.0) * (0.25 * np.pi))
                g_r = np.sin((pos_r + 1.0) * (0.25 * np.pi))
                if law == "compromise":
                    g_l = np.sqrt(np.maximum(g_l * 0.5 * (1.0 - pos_l), 0.0))
                    g_r = np.sqrt(np.maximum(g_r * 0.5 * (1.0 + pos_r), 0.0))
        return {
            "out_l": (x_l * g_l).astype(np.float32),
            "out_r": (x_r * g_r).astype(np.float32),
        }

    def _render_mid_side(self, module, frames: int, buffers, patch) -> dict:
        """M/S encode/decode + width + bass mono (see modules/mid_side.py).

        Standard sum/difference pair; ``width_cv`` adds per sample with
        the final width clamped 0..2. One patched input is treated as
        the mid itself (level preserved, width inert) rather than a
        half-level L+0 pair — the mono-passthrough contract.

        ``side_hp`` > 0 runs ``_mid_side_side_hp`` over the SIDE only,
        before ``width`` and before the decode: everything under the
        corner collapses to the middle, ``mid`` is untouched, and
        ``side``/``out_l``/``out_r`` all carry the filtered side. At 0
        the filter is not called at all -- the module is the stateless
        sum/difference pair it always was, bit-exact -- and any filter
        history is dropped so a corner switched back on starts from
        rest rather than resuming a stale tail. Mono passthrough and
        silence drop it for the same reason: the side was zero.
        """
        in_l = self._input_buffer(patch, buffers, module.id, "in_l")
        in_r = self._input_buffer(patch, buffers, module.id, "in_r")
        width_cv = self._input_buffer(patch, buffers, module.id, "width_cv")

        try:
            width = float(module.params.get("width", 1.0))
        except (TypeError, ValueError):
            width = 1.0
        width = min(2.0, max(0.0, width))
        try:
            side_hp = float(module.params.get("side_hp", 0.0))
        except (TypeError, ValueError):
            side_hp = 0.0
        if side_hp > 0.0:
            side_hp = min(
                self._MID_SIDE_HP_MAX_HZ, max(self._MID_SIDE_HP_MIN_HZ, side_hp)
            )
        else:
            side_hp = 0.0

        if in_l is None and in_r is None:
            self._state.pop(module.id, None)
            zeros = np.zeros(frames, dtype=np.float32)
            return {"mid": zeros, "side": zeros, "out_l": zeros, "out_r": zeros}
        if in_l is None or in_r is None:
            # Mono: the one input IS the mid; width has nothing to act on.
            self._state.pop(module.id, None)
            mono = (in_l if in_l is not None else in_r).astype(np.float32)
            zeros = np.zeros(frames, dtype=np.float32)
            return {"mid": mono, "side": zeros, "out_l": mono, "out_r": mono}

        mid = (in_l.astype(np.float64) + in_r) * 0.5
        side = (in_l.astype(np.float64) - in_r) * 0.5
        if side_hp > 0.0:
            side = self._mid_side_side_hp(module.id, side_hp, side)
        else:
            self._state.pop(module.id, None)
        if width_cv is not None:
            w = np.clip(width + width_cv.astype(np.float64), 0.0, 2.0)
        else:
            w = width
        ws = w * side
        return {
            "mid": mid.astype(np.float32),
            "side": side.astype(np.float32),
            "out_l": (mid + ws).astype(np.float32),
            "out_r": (mid - ws).astype(np.float32),
        }
