"""Modulation-source renderers: LFO, ADSR, AD envelope, function generator,
slew, drift, chaos, sample & hold, noise, shift-register random.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". Each renderer came with its ``_mono`` / ``_voice`` paths and the
helpers and constants only it uses (the noise colour constants sat under
the ADSR section marker; they came with ``_render_noise``). The gate
threshold ``_GATE_HIGH`` stays on the backend -- a score of renderers in
every family share it -- as do ``_pow2_clipped``, ``_finite_mean``,
``_voice_mean`` / ``_voice_sum`` and the drums' ``_drum_edge_value`` (the
ADSR's velocity read): all reached through ``self``. ``_lfo_reset_edges``
lives here with the LFO; the clock (renderers/sequencing.py) reaches it
through ``self``. The function generator's two static helpers
(``_fg_fresh_state``, ``_fg_lengths``) name ``NumpyBackend`` and reach it
through a lazy import (``numpy_backend`` imports this module, so a
top-level import would be circular); both run per block or per voice,
never per sample.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter


class ModSourceRenderers:
    def _render_shift_random(self, module, frames: int, buffers, patch) -> dict:
        """Looping shift-register random CV (see modules/shift_random.py).

        A 16-bit register rotates one place per rising ``clock`` edge; the
        bit recirculating from position ``length - 1`` flips with
        ``probability`` (a ``write`` gate held high forces it to 1). The
        CV out is the first eight bits read as a byte — newest bit as MSB
        — scaled to ``range``; the gate out mirrors bit 0.

        Determinism: the register's initial fill and every flip decision
        come from a Generator seeded with the ``seed`` param, consumed
        once per clock edge — so the sequence is a pure function of the
        seed and the clock, block-size independent by construction.
        Changing ``seed`` live re-rolls the register on the spot (the
        state rebuild below). Outputs hold between edges; mono, like the
        clock that drives it.
        """
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        write = self._input_buffer(patch, buffers, module.id, "write")

        try:
            p = float(module.params.get("probability", 0.1))
        except (TypeError, ValueError):
            p = 0.1
        p = min(1.0, max(0.0, p))
        try:
            length = int(module.params.get("length", 8))
        except (TypeError, ValueError):
            length = 8
        length = min(16, max(2, length))
        try:
            span = float(module.params.get("range", 2.0))
        except (TypeError, ValueError):
            span = 2.0
        bipolar = bool(module.params.get("bipolar", False))
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)

        st = self._state.setdefault(module.id, {})
        if st.get("seed") != seed:
            rng = np.random.default_rng(seed)
            st["seed"] = seed
            st["rng"] = rng
            st["bits"] = [int(b) for b in rng.integers(0, 2, size=16)]
            st["prev_clock"] = False
        bits: list[int] = st["bits"]
        rng = st["rng"]
        prev_clock = bool(st["prev_clock"])

        def scaled(byte_bits: list[int]) -> float:
            # First 8 bits as a byte, newest (bits[0]) as MSB.
            value = 0
            for i in range(8):
                value = (value << 1) | byte_bits[i]
            v = value / 255.0
            return (v * 2.0 - 1.0) * span if bipolar else v * span

        cv_out = np.empty(frames, dtype=np.float32)
        gate_out = np.empty(frames, dtype=np.float32)
        cur_cv = scaled(bits)
        cur_gate = 1.0 if bits[0] else 0.0

        if clock is None:
            cv_out[:] = cur_cv
            gate_out[:] = cur_gate
            return {"cv": cv_out, "gate": gate_out}

        g = clock > self._GATE_HIGH
        prev_arr = np.empty_like(g)
        prev_arr[0] = prev_clock
        prev_arr[1:] = g[:-1]
        edges = np.flatnonzero(g & ~prev_arr)

        pos = 0
        for e in edges:
            cv_out[pos:e] = cur_cv
            gate_out[pos:e] = cur_gate
            incoming = bits[length - 1]
            if rng.random() < p:
                incoming ^= 1
            if write is not None and float(write[e]) > self._GATE_HIGH:
                incoming = 1
            bits[:] = [incoming] + bits[:15]
            cur_cv = scaled(bits)
            cur_gate = 1.0 if bits[0] else 0.0
            pos = int(e)
        cv_out[pos:] = cur_cv
        gate_out[pos:] = cur_gate

        st["prev_clock"] = bool(g[-1])
        return {"cv": cv_out, "gate": gate_out}

    # ----- Chaos rendering -------------------------------------------------

    # Control-grid decimation: the ODE is evaluated every DIV samples and
    # linearly interpolated to audio rate (CV-smooth, and the per-sample-
    # Python trap — the slew lesson — never opens).
    _CHAOS_DIV = 16
    # Attractor time per orbit (approximate), used to calibrate ``rate``
    # to ~orbits/second; and the RK4 stability rails (max dt per step).
    _CHAOS_T_ORBIT = {"lorenz": 0.76, "rossler": 6.1}
    _CHAOS_DT_RAIL = {"lorenz": 0.01, "rossler": 0.05}
    _CHAOS_IC = {"lorenz": (1.0, 1.0, 20.0), "rossler": (1.0, 1.0, 0.5)}
    # Normalisation (center, half-span) per axis — the attractors' known
    # bounds at the classic constants; outputs clip at the rails.
    _CHAOS_NORM = {
        "lorenz": ((0.0, 20.0), (0.0, 27.0), (25.0, 25.0)),
        "rossler": ((0.0, 12.0), (0.0, 12.0), (11.5, 11.5)),
    }
    _CHAOS_WARMUP = 1000       # substeps run at init to land on the attractor
    _CHAOS_ROSSLER_GATE_Z = 3.0

    @staticmethod
    def _chaos_deriv(system: str):
        if system == "rossler":
            def d(s):
                x, y, z = s
                return (-y - z, x + 0.2 * y, 0.2 + z * (x - 5.7))
            return d

        def d(s):  # lorenz: sigma=10, rho=28, beta=8/3
            x, y, z = s
            return (
                10.0 * (y - x),
                x * (28.0 - z) - y,
                x * y - (8.0 / 3.0) * z,
            )
        return d

    @staticmethod
    def _chaos_rk4(deriv, s, dt):
        k1 = deriv(s)
        s2 = (
            s[0] + 0.5 * dt * k1[0],
            s[1] + 0.5 * dt * k1[1],
            s[2] + 0.5 * dt * k1[2],
        )
        k2 = deriv(s2)
        s3 = (
            s[0] + 0.5 * dt * k2[0],
            s[1] + 0.5 * dt * k2[1],
            s[2] + 0.5 * dt * k2[2],
        )
        k3 = deriv(s3)
        s4 = (
            s[0] + dt * k3[0],
            s[1] + dt * k3[1],
            s[2] + dt * k3[2],
        )
        k4 = deriv(s4)
        c = dt / 6.0
        return (
            s[0] + c * (k1[0] + 2.0 * k2[0] + 2.0 * k3[0] + k4[0]),
            s[1] + c * (k1[1] + 2.0 * k2[1] + 2.0 * k3[1] + k4[1]),
            s[2] + c * (k1[2] + 2.0 * k2[2] + 2.0 * k3[2] + k4[2]),
        )

    def _render_chaos(self, module, frames: int, buffers, patch) -> dict:
        """Strange-attractor CV source (see modules/chaos.py).

        RK4 on a control grid keyed to the ABSOLUTE sample count: state
        holds the raw attractor values at the two control points
        bracketing the playhead (``prev``/``next``) plus an integer
        sample offset ``frac`` into the interval — every rendered
        sample interpolates at ``u = (frac + k) / DIV`` control units,
        so any block split lands on identical floats and block-size
        independence is exact. ``rate`` scales attractor time per
        control step; the RK4 substep count is derived from params
        only (never from the block size). A ``reset`` rising edge
        re-seeds mid-block, sample-accurate; the sample AT the reset
        is the seeded (warmed-up) starting point exactly.
        """
        reset = self._input_buffer(patch, buffers, module.id, "reset")

        system = str(module.params.get("system", "lorenz"))
        if system not in self._CHAOS_T_ORBIT:
            system = "lorenz"
        try:
            rate = float(module.params.get("rate", 1.0))
        except (TypeError, ValueError):
            rate = 1.0
        rate = min(50.0, max(0.01, rate))
        try:
            span = float(module.params.get("range", 2.0))
        except (TypeError, ValueError):
            span = 2.0
        bipolar = bool(module.params.get("bipolar", False))
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)

        sr = float(self.sample_rate)
        DIV = self._CHAOS_DIV
        deriv = self._chaos_deriv(system)
        rail = self._CHAOS_DT_RAIL[system]
        # Attractor time advanced per control step.
        d_tau = DIV * rate * self._CHAOS_T_ORBIT[system] / sr
        n_sub = max(1, int(np.ceil(d_tau / rail)))
        dt = d_tau / n_sub

        def seeded_start():
            rng = np.random.default_rng(seed)
            base = self._CHAOS_IC[system]
            jit = rng.standard_normal(3)
            s = (
                base[0] + 0.5 * float(jit[0]),
                base[1] + 0.5 * float(jit[1]),
                base[2] + 0.5 * float(jit[2]),
            )
            for _ in range(self._CHAOS_WARMUP):
                s = self._chaos_rk4(deriv, s, rail)
            return s

        def step_ctrl(s):
            for _ in range(n_sub):
                s = self._chaos_rk4(deriv, s, dt)
            if not all(np.isfinite(c) for c in s):
                # Should never happen inside the rails; recover quietly.
                st["blowups"] = st.get("blowups", 0) + 1
                return seeded_start()
            return s

        st = self._state.setdefault(module.id, {})
        if st.get("key") != (system, seed):
            st.clear()
            s0 = seeded_start()
            st.update(
                {
                    "key": (system, seed),
                    "prev": s0,
                    "next": step_ctrl(s0),
                    "frac": 0,
                    "prev_reset": False,
                    "blowups": st.get("blowups", 0),
                }
            )

        # Reset edges segment the block.
        if reset is not None:
            rg = reset > self._GATE_HIGH
            prev_arr = np.empty_like(rg)
            prev_arr[0] = bool(st["prev_reset"])
            prev_arr[1:] = rg[:-1]
            edges = np.flatnonzero(rg & ~prev_arr).tolist()
            st["prev_reset"] = bool(rg[-1])
        else:
            edges = []

        rx = np.empty(frames, dtype=np.float64)
        ry = np.empty(frames, dtype=np.float64)
        rz = np.empty(frames, dtype=np.float64)

        def render_segment(s0: int, e0: int) -> None:
            n = e0 - s0
            if n <= 0:
                return
            frac = int(st["frac"])
            needed = (frac + n) // DIV + 2
            ctrl = [st["prev"], st["next"]]
            while len(ctrl) < needed:
                ctrl.append(step_ctrl(ctrl[-1]))
            C = np.asarray(ctrl, dtype=np.float64)  # (M, 3)
            xp = np.arange(len(ctrl), dtype=np.float64)
            u = (frac + np.arange(n, dtype=np.float64)) / DIV
            rx[s0:e0] = np.interp(u, xp, C[:, 0])
            ry[s0:e0] = np.interp(u, xp, C[:, 1])
            rz[s0:e0] = np.interp(u, xp, C[:, 2])
            total = frac + n
            adv = total // DIV
            st["frac"] = total % DIV
            st["prev"] = ctrl[adv]
            st["next"] = ctrl[adv + 1]

        pos = 0
        for e in edges:
            render_segment(pos, e)
            s0 = seeded_start()
            st["prev"] = s0
            st["next"] = step_ctrl(s0)
            st["frac"] = 0
            pos = int(e)
        render_segment(pos, frames)

        # Normalise by the attractor's bounds, clip, map to range.
        norm = self._CHAOS_NORM[system]
        outs = []
        for raw, (center, half) in zip((rx, ry, rz), norm):
            nv = np.clip((raw - center) / half, -1.0, 1.0)
            if bipolar:
                outs.append((nv * span).astype(np.float32))
            else:
                outs.append(((nv + 1.0) * 0.5 * span).astype(np.float32))

        if system == "rossler":
            gate = (rz > self._CHAOS_ROSSLER_GATE_Z)
        else:
            gate = (rx > 0.0)
        gate_out = gate.astype(np.float32)

        return {"x": outs[0], "y": outs[1], "z": outs[2], "gate": gate_out}

    # Pink-noise generation: a 3rd-order IIR that tilts white noise to
    # -3 dB/oct (music-dsp standard coefficients), applied via
    # scipy.signal.lfilter with its state (zi) carried across blocks.
    # _PINK_SCALE RMS-matches the output to uniform white (std ~0.577)
    # so the `amp` param means the same level for both colors.
    _PINK_B = (0.049922035, -0.095993537, 0.050612699, -0.004408786)
    _PINK_A = (1.0, -2.494956002, 2.017265875, -0.522189400)
    _PINK_SCALE = 11.7027

    # Brown (red) noise is white through a LEAKY integrator: a one-pole
    # with its pole just under 1. A true integrator of white is a random
    # walk with unbounded variance (it wanders off as DC); the leak puts
    # a corner under it, and at the default 10 Hz that corner sits below
    # anything audible, so from ~20 Hz up the tilt is the textbook
    # -6 dB/oct. Love pass: the corner is the module's ``corner`` param
    # (2..40 Hz, NOISE_CORNER_*), this constant its default -- lower
    # lets the wander reach further down (2-20 Hz sits 39 dB over
    # 200-2000 Hz at 2 Hz against 23.5 dB at 40), the slope above it is
    # -6.00 dB/oct either way.
    # The pole is derived from the sample rate per render
    # (1 - 2*pi*fc/sr) and the RMS-match scale falls out of the same
    # number: a leaky integrator of white with variance s^2 has
    # stationary variance s^2 / (1 - a^2), so sqrt(1 - a^2) brings it
    # back to white's level -- 0.053362 at 44.1 kHz (measured 0.0541
    # +- 0.0011 over 8 seeds x 20 s: brown's RMS is dominated by its
    # lowest octaves, so even 20 s is a small sample of it).
    _BROWN_CORNER_HZ = 10.0
    # Violet is the first difference of white (+6 dB/oct). Its variance
    # is exactly twice white's (two independent samples per output), so
    # 1/sqrt(2) RMS-matches it (measured 0.70722 over 8 seeds x 20 s).
    _VIOLET_SCALE = 0.7071067811865476

    # Integer phase codes for the vectorized state machine. The mono
    # fast path below still uses strings — we keep them separate so an
    # existing test that introspected the state (none do today, but they
    # could) sees unchanged behaviour.
    # AD (trigger) envelope phases.
    _AD_IDLE = 0
    _AD_ATTACK = 1
    _AD_DECAY = 2
    _ADSR_IDLE = 0
    _ADSR_ATTACK = 1
    _ADSR_DECAY = 2
    _ADSR_SUSTAIN = 3
    _ADSR_RELEASE = 4

    def _render_adsr(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sample-accurate ADSR driven by a gate signal.

        State machine: idle → attack → decay → sustain → release → idle.
        - Attack ramps linearly from current level to 1.0 over ``attack``
          seconds (so retriggering before full release picks up where the
          envelope was, no click).
        - Decay ramps from 1.0 to ``sustain`` over ``decay`` seconds.
        - Sustain holds at ``sustain`` while the gate stays high.
        - Release ramps from current level to 0.0 over ``release`` seconds.

        Shape-polymorphic. A 1D ``(F,)`` gate input drives the existing
        scalar state machine and emits ``(F,)``. A 2D ``(V, F)`` gate
        input (from voice-aware MIDIInput / Keyboard) maintains ``V``
        independent state machines and emits ``(V, F)`` — the per-voice
        envelopes that are the whole point of the polyphonic v0.4 work.

        All durations are clamped to a >= 1-sample minimum so any param
        edits remain numerically stable.

        Velocity (2026-09-19): ``vel`` is a knobless multiplier on the
        whole envelope of a note, read at the rising-edge sample and
        latched until the next edge (the drums' / sampler's edge-latch
        rule). Both paths run their stages in *output* space -- peak
        ``scale``, plateau ``sustain * scale``, attack/decay steps scaled
        so the times stay what the knobs say -- which with ``scale``
        1.0 is the same arithmetic to the bit, so an unpatched ``vel``
        renders exactly the pre-vel envelope. See the paths for the
        re-struck-softer (fall-to-peak) rule.
        """
        # collapse=False so a (V, F) gate buffer reaches us with its
        # voice axis intact. Mono sources still arrive as (F,).
        gate_buf = self._input_buffer(
            patch, buffers, module.id, "gate", collapse=False
        )
        # Velocity bus, also voice-aware: it is only ever READ at a gate's
        # rising-edge sample (and latched for that note), so it goes down
        # whole and each path picks its own row / collapse rule.
        vel_buf = self._input_buffer(
            patch, buffers, module.id, "vel", collapse=False
        )
        sr = self.sample_rate

        attack_s = max(0.0, float(module.params.get("attack", 0.01)))
        decay_s = max(0.0, float(module.params.get("decay", 0.1)))
        sustain = max(0.0, min(1.0, float(module.params.get("sustain", 0.7))))
        release_s = max(0.0, float(module.params.get("release", 0.3)))

        attack_step = 1.0 / max(1.0, attack_s * sr)
        decay_step = (1.0 - sustain) / max(1.0, decay_s * sr)
        # Release time used to compute per-voice release_step at the
        # gate-fall edge (so a release from mid-attack still takes the
        # full release window).
        release_samples = max(1.0, release_s * sr)

        # Branch by gate shape. Voice-aware when gate has a leading axis.
        if gate_buf is not None and gate_buf.ndim == 2:
            return self._render_adsr_voice(
                module, frames, gate_buf,
                attack_step, decay_step, sustain, release_samples,
                vel_buf=vel_buf,
            )
        return self._render_adsr_mono(
            module, frames, gate_buf,
            attack_step, decay_step, sustain, release_samples,
            vel_buf=vel_buf,
        )

    def _render_adsr_mono(
        self, module, frames, gate_buf,
        attack_step, decay_step, sustain, release_samples,
        vel_buf=None,
    ):
        """Mono fast path — scalar state machine, output ``(F,)``.

        Unchanged from the pre-slice-3 implementation but for the
        velocity latch. Kept as the fast path so existing patches and
        the entire existing ADSR test suite continue to work bit-for-bit
        identically.

        Velocity: ``scale`` is latched at each rising edge from ``vel``
        (``max(0, vel[edge])``; a ``(V, F)`` bus collapses to its
        loudest voice at that sample -- the drums' rule, because a
        velocity bus carries 0 on idle slots; unpatched = 1.0). The
        stages then run in output space: peak ``scale``, plateau
        ``sustain * scale``, attack/decay steps ``* scale`` so a soft
        note's attack still takes ``attack`` seconds (gentler slope, same
        time). Every one of those products is exact at scale 1.0, which
        is what makes the unpatched path bit-identical to the old code.

        A note re-struck SOFTER than it is currently ringing would have
        to jump down to its new peak; instead the level falls to the
        peak at the full-velocity attack slope and decay carries on from
        there -- output stays continuous, and a velocity-0 retrigger fades
        a ringing note out over one attack time rather than cutting it.
        """
        state = self._state.setdefault(
            module.id,
            {"phase": "idle", "level": 0.0, "prev_gate": False,
             "release_step": 0.0, "scale": 1.0},
        )
        # If this slot of state belongs to the voice-aware path from a
        # previous call (different gate shape), discard and reinit.
        if "phase_arr" in state:
            state.clear()
            state.update(
                {"phase": "idle", "level": 0.0, "prev_gate": False,
                 "release_step": 0.0, "scale": 1.0}
            )
        scale = float(state.get("scale", 1.0))
        peak = scale
        sus = sustain * scale
        a_step = attack_step * scale
        d_step = decay_step * scale

        out = np.empty(frames, dtype=np.float32)

        for n in range(frames):
            gate_high = (
                bool(gate_buf[n] > self._GATE_HIGH) if gate_buf is not None else False
            )

            if gate_high and not state["prev_gate"]:
                # Latch this note's velocity AT the edge sample.
                scale = max(0.0, self._drum_edge_value(vel_buf, n, 1.0))
                peak = scale
                sus = sustain * scale
                a_step = attack_step * scale
                d_step = decay_step * scale
                state["phase"] = "attack"
            elif not gate_high and state["prev_gate"]:
                state["release_step"] = state["level"] / release_samples
                state["phase"] = "release"
            state["prev_gate"] = gate_high

            phase = state["phase"]
            level = state["level"]

            if phase == "attack":
                if level > peak:
                    # Re-struck softer than it rings: fall to the new
                    # peak at the full-velocity attack slope, no jump.
                    level -= attack_step
                    if level <= peak:
                        level = peak
                        state["phase"] = "decay"
                else:
                    level += a_step
                    if level >= peak:
                        level = peak
                        state["phase"] = "decay"
            elif phase == "decay":
                level -= d_step
                if level <= sus:
                    level = sus
                    state["phase"] = "sustain"
            elif phase == "sustain":
                level = sus
            elif phase == "release":
                level -= state["release_step"]
                if level <= 0.0:
                    level = 0.0
                    state["phase"] = "idle"

            state["level"] = level
            out[n] = level

        state["scale"] = scale
        return out.astype(np.float32)

    def _render_adsr_voice(
        self, module, frames, gate_buf,
        attack_step, decay_step, sustain, release_samples,
        vel_buf=None,
    ):
        """Voice-aware path — V independent state machines, vectorized.

        2026-06-07 rewrite. The original looped over samples in Python
        (vectorizing only across the 16 voices per sample), which made
        the ADSR own ~63% of the render block under cProfile. This
        version inverts the loop: each voice's gate row is split into
        runs at gate *edges* (rare — typically zero per block), and
        within a run the envelope from a known entry state is a
        deterministic piecewise-linear chain (attack→decay→sustain or
        release→idle) that numpy emits in a handful of array ops.

        Per-sample semantics preserved exactly:
        - edge transitions apply before that sample's level advance (a
          rising edge's sample already moves up by ``attack_step``);
        - stage crossings clamp on the crossing sample (attack's last
          sample outputs exactly 1.0, decay's outputs ``sustain``,
          release's 0.0); the next stage starts the following sample;
        - retrigger continues from the current level (no click);
        - falling edges set ``release_step`` from the level at the edge
          so the tail takes the full release window.

        Only divergence: a run computes ``L0 + k*step`` by multiply
        where the loop accumulated additions — float64 drift orders of
        magnitude below the float32 resolution that leaves this method.

        Velocity (2026-09-19): each voice latches its own ``scale`` at
        its rising edges -- from its own row of a ``(V, F)`` ``vel``
        (``midi_input.velocity_cv``), or from the shared row of a mono
        one -- and hands it to :meth:`_adsr_fill_run`, which runs the
        stages in output space. Unpatched every voice keeps 1.0.
        """
        V = gate_buf.shape[0]
        state = self._state.setdefault(module.id, {})

        # If state belongs to the mono branch (different keys), or the
        # voice count changed, reinitialise. The latter shouldn't happen
        # in practice (V is always MAX_VOICES = 16 today), but the
        # check costs nothing and protects against future shape drift.
        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.full(V, self._ADSR_IDLE, dtype=np.int32)
            state["level_arr"] = np.zeros(V, dtype=np.float64)
            state["prev_gate_arr"] = np.zeros(V, dtype=bool)
            state["release_step_arr"] = np.zeros(V, dtype=np.float64)
            state["scale_arr"] = np.ones(V, dtype=np.float64)

        phase = state["phase_arr"]
        level = state["level_arr"]
        prev_gate = state["prev_gate_arr"]
        release_step = state["release_step_arr"]
        scale_arr = state["scale_arr"]

        gate_high = gate_buf > self._GATE_HIGH  # (V, F) bool
        out = np.empty((V, frames), dtype=np.float64)

        for v in range(V):
            row = gate_high[v]
            # Edge samples: where the gate differs from the previous
            # sample, seeded with the carried cross-block prev_gate.
            shifted = np.empty(frames, dtype=bool)
            shifted[0] = prev_gate[v]
            shifted[1:] = row[:-1]
            edges = np.flatnonzero(row != shifted)

            ph = int(phase[v])
            lvl = float(level[v])
            rs = float(release_step[v])
            sc = float(scale_arr[v])
            # This voice's velocity row: its own on a (V, F) bus, the one
            # row of a mono bus, None when unpatched (scale stays 1.0).
            if vel_buf is None:
                vrow = None
            elif vel_buf.ndim == 2:
                vrow = vel_buf[v] if v < vel_buf.shape[0] else vel_buf[0]
            else:
                vrow = vel_buf

            if edges.size == 0:
                # Common case: no gate activity this block — one run.
                ph, lvl = self._adsr_fill_run(
                    out[v], 0, frames, ph, lvl, rs,
                    attack_step, decay_step, sustain, sc,
                )
            else:
                starts = (
                    edges if edges[0] == 0
                    else np.concatenate(([0], edges))
                )
                ends = np.append(starts[1:], frames)
                edge_set = set(int(e) for e in edges)
                for s, e in zip(starts, ends):
                    s = int(s)
                    if s in edge_set:
                        if row[s]:
                            # Rising -> attack from the current level
                            # (retrigger picks up where we were), with
                            # the velocity latched AT the edge sample.
                            if vrow is not None:
                                sc = max(0.0, float(vrow[s]))
                            ph = self._ADSR_ATTACK
                        else:
                            # Falling -> release over the full window
                            # from wherever the envelope is now.
                            rs = lvl / release_samples
                            ph = self._ADSR_RELEASE
                    ph, lvl = self._adsr_fill_run(
                        out[v], s, int(e), ph, lvl, rs,
                        attack_step, decay_step, sustain, sc,
                    )

            phase[v] = ph
            level[v] = lvl
            release_step[v] = rs
            scale_arr[v] = sc
            prev_gate[v] = bool(row[-1])

        return out.astype(np.float32)

    def _adsr_fill_run(
        self, seg, pos, end, ph, lvl, rs,
        attack_step, decay_step, sustain, scale=1.0,
    ):
        """Fill ``seg[pos:end]`` with the envelope trajectory from entry
        state ``(ph, lvl)``, following the natural stage chain. Returns
        the exit ``(ph, lvl)``.

        Mirrors the per-sample mask cascade exactly. In the mask
        implementation a stage *crossing* cascades within the same
        sample: the sample where attack reaches 1.0 immediately applies
        the decay update too, so the emitted value is
        ``max(1.0 - decay_step, sustain)`` — never a bare 1.0. (This is
        a deliberate divergence from the mono scalar path, which emits
        the clamped 1.0 and starts decay the *next* sample; the voice
        path has always cascaded and downstream tests encode it.)
        Likewise decay's crossing sample emits ``sustain`` and release's
        emits 0.0. Attack therefore contributes only its strictly-
        below-1.0 ramp samples; the crossing sample belongs to decay.

        Stage lengths are analytic (smallest k >= 1 crossing the
        target), so each stage is one ``arange`` + one clamp.

        ``scale`` is the note's latched velocity: the stages run in
        output space (peak ``scale``, plateau ``sustain * scale``,
        attack/decay steps ``* scale``). At 1.0 every product is exact
        and this is the pre-vel arithmetic to the bit. A run entered in
        attack ABOVE its peak (re-struck softer than it rings) falls to
        the peak at the full-velocity ``attack_step`` -- same shape as
        the rise, mirrored -- and cascades into decay on the crossing
        sample just as the rise does.
        """
        peak = scale
        sus = sustain * scale
        a_step = attack_step * scale
        d_step = decay_step * scale
        if ph == self._ADSR_ATTACK and pos < end:
            if lvl > peak:
                kf = max(1, int(np.ceil((lvl - peak) / attack_step)))
                k = min(kf - 1, end - pos)
                if k > 0:
                    seg[pos:pos + k] = np.maximum(
                        lvl - np.arange(1, k + 1, dtype=np.float64) * attack_step,
                        peak,
                    )
                    lvl = float(seg[pos + k - 1])
                    pos += k
            else:
                if lvl >= peak or a_step <= 0.0:
                    # Already at the peak (a retrigger at the top, or
                    # scale 0 where the peak IS 0): the crossing is this
                    # sample. The guard also keeps 0/0 out of the ceil.
                    ka = 1
                else:
                    ka = max(1, int(np.ceil((peak - lvl) / a_step)))
                k = min(ka - 1, end - pos)
                if k > 0:
                    seg[pos:pos + k] = np.minimum(
                        lvl + np.arange(1, k + 1, dtype=np.float64) * a_step,
                        peak,
                    )
                    lvl = float(seg[pos + k - 1])
                    pos += k
            if pos < end:
                # Crossing falls inside this run: cascade into decay,
                # which emits the crossing sample below.
                ph = self._ADSR_DECAY
                lvl = peak
        if ph == self._ADSR_DECAY and pos < end:
            if lvl <= sus or d_step <= 0.0:
                # Includes the mid-flight "sustain raised above current
                # level" case: the mask cascade clamps up to sustain on
                # the first sample; maximum() reproduces that.
                kd = 1
            else:
                kd = max(1, int(np.ceil((lvl - sus) / d_step)))
            k = min(kd, end - pos)
            seg[pos:pos + k] = np.maximum(
                lvl - np.arange(1, k + 1, dtype=np.float64) * d_step,
                sus,
            )
            lvl = float(seg[pos + k - 1])
            pos += k
            if k == kd:
                ph = self._ADSR_SUSTAIN
                lvl = sus
        if ph == self._ADSR_SUSTAIN and pos < end:
            seg[pos:end] = sus
            lvl = sus
            pos = end
        if ph == self._ADSR_RELEASE and pos < end:
            if rs > 0.0:
                kr = max(1, int(np.ceil(lvl / rs)))
                k = min(kr, end - pos)
                seg[pos:pos + k] = np.maximum(
                    lvl - np.arange(1, k + 1, dtype=np.float64) * rs,
                    0.0,
                )
                lvl = float(seg[pos + k - 1])
                pos += k
                if k == kr:
                    ph = self._ADSR_IDLE
                    lvl = 0.0
            elif lvl <= 0.0:
                seg[pos] = 0.0
                lvl = 0.0
                pos += 1
                ph = self._ADSR_IDLE
            else:
                # Degenerate zero-step release with positive level: the
                # per-sample cascade would hold here forever. Preserve.
                seg[pos:end] = lvl
                pos = end
        if ph == self._ADSR_IDLE and pos < end:
            seg[pos:end] = lvl
            pos = end
        return ph, lvl

    def _render_ad(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Trigger-style Attack/Decay envelope (percussion).

        State machine: idle -> attack -> decay -> idle. A rising edge on
        ``trig`` (re)enters attack from the current level; attack ramps
        to 1.0, decay ramps to 0.0, then it idles. The trigger going low
        is ignored -- the decay always runs to completion, which is what
        makes a momentary clock pulse produce a full hit.

        Shape-polymorphic like ADSR: a 1D ``(F,)`` trigger drives one
        envelope and emits ``(F,)``; a 2D ``(V, F)`` trigger maintains V
        independent envelopes and emits ``(V, F)``. The mono and voice
        paths share identical per-sample semantics (one stage update per
        sample, transitions take effect the following sample), so a
        voice row is bit-identical to the mono result for the same gate.
        Durations clamp to a >= 1-sample minimum for stability.
        """
        gate_buf = self._input_buffer(
            patch, buffers, module.id, "trig", collapse=False
        )
        sr = self.sample_rate
        attack_s = max(0.0, float(module.params.get("attack", 0.005)))
        decay_s = max(0.0, float(module.params.get("decay", 0.20)))
        attack_step = 1.0 / max(1.0, attack_s * sr)
        decay_step = 1.0 / max(1.0, decay_s * sr)

        if gate_buf is not None and gate_buf.ndim == 2:
            return self._render_ad_voice(
                module, frames, gate_buf, attack_step, decay_step
            )
        return self._render_ad_mono(
            module, frames, gate_buf, attack_step, decay_step
        )

    def _render_ad_mono(self, module, frames, gate_buf, attack_step, decay_step):
        """Mono scalar reference: one envelope, output ``(F,)``."""
        state = self._state.setdefault(
            module.id, {"phase": self._AD_IDLE, "level": 0.0, "prev_gate": False}
        )
        if "phase_arr" in state:  # was the voice branch -> reinit to mono
            state.clear()
            state.update({"phase": self._AD_IDLE, "level": 0.0, "prev_gate": False})

        out = np.empty(frames, dtype=np.float32)
        phase = state["phase"]
        level = state["level"]
        prev = state["prev_gate"]

        for n in range(frames):
            g = bool(gate_buf[n] > self._GATE_HIGH) if gate_buf is not None else False
            if g and not prev:
                phase = self._AD_ATTACK  # retrigger from current level
            prev = g

            if phase == self._AD_ATTACK:
                level += attack_step
                if level >= 1.0:
                    level = 1.0
                    phase = self._AD_DECAY
            elif phase == self._AD_DECAY:
                level -= decay_step
                if level <= 0.0:
                    level = 0.0
                    phase = self._AD_IDLE
            # idle: level holds at 0.0
            out[n] = level

        state["phase"] = phase
        state["level"] = level
        state["prev_gate"] = prev
        return out

    def _render_ad_voice(self, module, frames, gate_buf, attack_step, decay_step):
        """Voice path: V independent envelopes, output ``(V, F)``.

        Per-sample loop over the block, vectorized across the V voices.
        Each sample applies exactly one stage update per voice based on
        the phase *at the start of the sample* (after the edge check), so
        a voice that crosses attack->decay this sample still emits 1.0
        and only begins decaying next sample -- identical to the mono
        scalar path (asserted in the tests). A per-sample loop (like the
        Crossover voice path) rather than ADSR's run-splitting: simpler
        and plenty fast for the envelope; run-based vectorization is a
        later optimization if profiling ever flags it.
        """
        V = gate_buf.shape[0]
        state = self._state.setdefault(module.id, {})
        needs_reinit = (
            "phase_arr" not in state or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.full(V, self._AD_IDLE, dtype=np.int32)
            state["level_arr"] = np.zeros(V, dtype=np.float64)
            state["prev_gate_arr"] = np.zeros(V, dtype=bool)

        phase = state["phase_arr"]
        level = state["level_arr"]
        prev = state["prev_gate_arr"]

        gate_high = gate_buf > self._GATE_HIGH  # (V, F) bool
        out = np.empty((V, frames), dtype=np.float64)

        for n in range(frames):
            g = gate_high[:, n]
            rising = g & ~prev
            if rising.any():
                phase = np.where(rising, self._AD_ATTACK, phase)
            prev = g

            phase_now = phase  # stage to apply this sample (pre-transition)
            att = phase_now == self._AD_ATTACK
            dec = phase_now == self._AD_DECAY

            if att.any():
                lv = level[att] + attack_step
                top = lv >= 1.0
                lv = np.where(top, 1.0, lv)
                level[att] = lv
                # mark voices that topped out -> decay next sample
                idx = np.flatnonzero(att)
                phase[idx[top]] = self._AD_DECAY
            if dec.any():
                lv = level[dec] - decay_step
                bot = lv <= 0.0
                lv = np.where(bot, 0.0, lv)
                level[dec] = lv
                idx = np.flatnonzero(dec)
                phase[idx[bot]] = self._AD_IDLE

            out[:, n] = level

        state["phase_arr"] = phase
        state["level_arr"] = level
        state["prev_gate_arr"] = prev
        return out.astype(np.float32)

    # ----- Function generator ---------------------------------------------

    _FG_IDLE = 0
    _FG_RISE = 1
    _FG_HOLD = 2
    _FG_FALL = 3
    # EOR/EOC pulse width. Long enough to see on a scope and to survive a
    # Schmitt, short enough never to swamp a fast loop -- so it is also
    # capped at a quarter of the cycle (and never shorter than one sample).
    _FG_PULSE_S = 0.002

    @staticmethod
    def _fg_exponent(curve: float) -> float:
        """Map the bipolar ``curve`` knob to a power-law exponent k.

        The slope shape is ``level = pos ** k`` on the way up and its
        mirror ``(1 - pos) ** k`` on the way down, so one exponent bends
        both slopes and the fall is the rise played backwards.

        ``curve`` 0 -> k = 1 (a straight line). Positive is exponential
        (k = 1 + 3c, up to 4 -- a rise that starts slow and accelerates,
        a fall that drops fast then trails). Negative is logarithmic
        (k = 1/(1 - 3c), down to 1/4 -- a rise that leaps then eases, a
        fall that lingers then drops). The two halves are reciprocals at
        equal magnitude, so +c and -c are mirror images of each other.
        """
        c = min(1.0, max(-1.0, curve))
        return 1.0 + 3.0 * c if c >= 0.0 else 1.0 / (1.0 - 3.0 * c)

    @staticmethod
    def _fg_fresh_state() -> dict:
        from ..numpy_backend import NumpyBackend  # lazy: it imports this module
        return {"phase": NumpyBackend._FG_IDLE, "cnt": 0, "level": 0.0,
                "prev_gate": False, "eor_left": 0, "eoc_left": 0}

    def _render_function_generator(self, module, frames: int, buffers, patch) -> dict:
        """Rise/fall function generator with EOR/EOC (see modules/function_generator.py).

        State machine: idle -> rise -> [hold] -> fall -> idle, with the
        mode deciding what the trigger means and whether the cycle ends
        at idle or wraps straight back into the rise.

        Stage progress is an INTEGER sample counter against an integer
        stage length, never a float step accumulated per sample -- the
        organ's lone-8' lesson. Accumulating ``1/(t*sr)`` drifts about a
        sample per stage, which is invisible in an envelope and audible
        in a loop-mode clock, where it compounds every cycle. Counting
        makes a stage exactly ``round(t*sr)`` samples long, forever.

        Every stage *entry* solves the curve backwards instead --
        ``cnt = len * level**(1/k)`` for a rise, ``len * (1 -
        level**(1/k))`` for a fall -- so a retrigger or a release picks
        up from the current output level rather than jumping. That is
        what keeps it click-free at any point in the cycle, which the
        plain level-stepping of ``ad_envelope`` gets for free only
        because its curve is linear.

        Shape-polymorphic like ADSR/AD, branched on ``trig``: a 1D
        ``(F,)`` (or absent) trigger runs one function and emits ``(F,)``
        on all three jacks; a 2D ``(V, F)`` trigger runs V independent
        functions and emits ``(V, F)``. Both paths drive the SAME scalar
        kernel (:meth:`_fg_kernel`), so a voice row is bit-identical to
        the mono result by construction.

        Rates: ``rate_cv`` is always collapsed to mono -- "both" is one
        knob on the hardware, one tempo for the rack. ``rise_cv`` /
        ``fall_cv`` go PER VOICE when they arrive as ``(V, F)`` alongside
        a ``(V, F)`` trigger with the same V: each slot gets its own
        stage lengths (and its own pulse width), so velocity into
        ``fall_cv`` gives every note its own ring. Any other shape --
        a mono CV, a 2D CV into a mono trigger, a V that doesn't match
        -- collapses the way ``_input_buffer`` would (sum across slots),
        which is exactly the 2026-09-14 mono law, so nothing already
        cabled moves.

        ``out_inv`` is ``1 - out`` computed on the finished block, not
        in the kernel: one numpy subtraction per block (or per (V, F)
        array) is nothing, and keeping it out of the scalar loop keeps
        the loop -- and its bit-identity claims -- exactly as they were.
        """
        gate_buf = self._input_buffer(
            patch, buffers, module.id, "trig", collapse=False
        )
        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        rise_cv = self._input_buffer(
            patch, buffers, module.id, "rise_cv", collapse=False
        )
        fall_cv = self._input_buffer(
            patch, buffers, module.id, "fall_cv", collapse=False
        )

        sr = float(self.sample_rate)
        mode = str(module.params.get("mode", "trigger"))
        if mode not in ("trigger", "gate", "loop"):
            mode = "trigger"
        try:
            rise_s = float(module.params.get("rise", 0.05))
        except (TypeError, ValueError):
            rise_s = 0.05
        try:
            fall_s = float(module.params.get("fall", 0.5))
        except (TypeError, ValueError):
            fall_s = 0.5
        def _curve(name):
            try:
                return float(module.params.get(name, 0.0))
            except (TypeError, ValueError):
                return 0.0

        curve = _curve("curve")
        # Per-slope knobs are OFFSETS on the shared one, summed and
        # clamped inside _fg_exponent: at 0 they change nothing.
        curve_rise = curve + _curve("curve_rise")
        curve_fall = curve + _curve("curve_fall")
        rise_s = min(10.0, max(0.0, rise_s))
        fall_s = min(10.0, max(0.0, fall_s))

        # rate_cv is 1 V/oct on the RATE (+1 = twice as fast), block-mean
        # like the LFO's. rise_cv / fall_cv are the same law on one slope
        # each and SUM with it in octaves -- Maths' per-channel jacks
        # next to its "both" -- and the per-slope total is clamped to
        # +/-5 octaves (inside _fg_lengths) so a runaway CV cannot ask
        # for a sub-sample cycle or a half-hour one. An unpatched jack
        # contributes 0.0, so the sum, the clamp and the scale are
        # bit-identical to the rate_cv-only arithmetic this replaced.
        voiced = gate_buf is not None and gate_buf.ndim == 2
        V = gate_buf.shape[0] if voiced else 0

        def _octaves(cv):
            # The mono law: a (V, F) CV is summed across slots first,
            # exactly as _input_buffer's collapse would have done.
            if cv is None or cv.size == 0:
                return 0.0
            if cv.ndim == 2:
                cv = self._voice_sum(cv)
            return self._finite_mean(cv)

        def _per_slot(cv):
            # Per-voice octaves when the CV is (V, F) against a (V, F)
            # trigger with the same V; None means "use the mono law".
            if voiced and cv is not None and cv.ndim == 2 and cv.shape[0] == V:
                return self._finite_mean(cv, axis=1)
            return None

        both = _octaves(rate_cv)
        k = (self._fg_exponent(curve_rise), self._fg_exponent(curve_fall))
        inv_k = (1.0 / k[0], 1.0 / k[1])

        rise_rows = _per_slot(rise_cv)
        fall_rows = _per_slot(fall_cv)
        if rise_rows is None and fall_rows is None:
            # One rate law for every voice.
            lengths = self._fg_lengths(
                rise_s, fall_s, both + _octaves(rise_cv), both + _octaves(fall_cv), sr
            )
            if voiced:
                return self._render_fg_voice(
                    module, gate_buf, frames, mode, k, inv_k, [lengths] * V
                )
            return self._render_fg_mono(module, gate_buf, frames, mode, k, inv_k, *lengths)

        # Per-voice: a slot whose CV row is 0 gets the shared law's
        # lengths, so a parked slot (velocity_cv holds 0 there) is
        # untouched and the skip in _render_fg_voice still applies.
        rise_oct = both + _octaves(rise_cv) if rise_rows is None else None
        fall_oct = both + _octaves(fall_cv) if fall_rows is None else None
        lengths = [
            self._fg_lengths(
                rise_s, fall_s,
                rise_oct if rise_rows is None else both + float(rise_rows[v]),
                fall_oct if fall_rows is None else both + float(fall_rows[v]),
                sr,
            )
            for v in range(V)
        ]
        return self._render_fg_voice(module, gate_buf, frames, mode, k, inv_k, lengths)

    @staticmethod
    def _fg_lengths(rise_s, fall_s, rise_oct, fall_oct, sr):
        """``(rise_len, fall_len, pulse_len)`` in samples for one rate law.

        Each slope's octave total is clamped to +/-5 (``_pow2_clipped``,
        so a NaN total reads as no modulation) and applied as
        ``1 / 2**oct``; ``x * 1.0 == x`` exactly, but a zero total skips
        the multiply anyway. Pulse width: 2 ms, but never more than a
        quarter of the cycle (so a 20 ms loop still emits a
        distinguishable blip) and never less than one sample (so an
        instant rise still announces itself).
        """
        from ..numpy_backend import NumpyBackend  # lazy: it imports this module
        if rise_oct != 0.0:
            rise_s *= 1.0 / NumpyBackend._pow2_clipped(rise_oct, 5.0)
        if fall_oct != 0.0:
            fall_s *= 1.0 / NumpyBackend._pow2_clipped(fall_oct, 5.0)
        rise_len = max(1, int(round(rise_s * sr)))
        fall_len = max(1, int(round(fall_s * sr)))
        pulse_len = max(
            1,
            min(
                int(round(NumpyBackend._FG_PULSE_S * sr)),
                (rise_len + fall_len) // 4 or 1,
            ),
        )
        return rise_len, fall_len, pulse_len

    def _fg_kernel(
        self, frames, gate_row, mode, k, inv_k, rise_len, fall_len,
        pulse_len, st, out, eor, eoc,
    ):
        """One function, one block, pure-Python scalars, written in place.

        THE single implementation of the state machine: the mono path
        calls it once and the voice path calls it once per slot, so a
        voice row is bit-identical to the mono result by construction
        rather than by assertion.

        ``k`` / ``inv_k`` are ``(rise, fall)`` pairs: each slope has its
        own exponent (``curve`` plus that slope's ``curve_rise`` /
        ``curve_fall``), and every backward solve on a stage entry uses
        the exponent of the stage being ENTERED, so a retrigger or a
        release stays click-free when the two differ.

        Scalars rather than numpy-over-(V,) is the slew lesson applied:
        at a block's worth of samples a numpy op is overhead, not
        arithmetic. The vectorized-across-voices draft of this cost
        ~117% of one block's budget at 16 voices; per-voice scalars cost
        ~36%, and idle slots (see :meth:`_render_fg_voice`) skip out
        entirely.
        """
        phase = st["phase"]
        cnt = st["cnt"]
        level = st["level"]
        prev = st["prev_gate"]
        eor_left = st["eor_left"]
        eoc_left = st["eoc_left"]
        looping = mode == "loop"
        gating = mode == "gate"
        rise_next = self._FG_HOLD if gating else self._FG_FALL
        fall_next = self._FG_RISE if looping else self._FG_IDLE
        high = self._GATE_HIGH
        k_rise, k_fall = k
        inv_rise, inv_fall = inv_k

        # A loop-mode generator never waits to be asked: kick it out of
        # idle so a freshly placed module (or one just switched to loop)
        # starts cycling on its first block, the way ``clock`` does.
        if looping and phase == self._FG_IDLE:
            phase = self._FG_RISE
            cnt = int(round(rise_len * (level ** inv_rise))) if level > 0.0 else 0

        for n in range(frames):
            g = bool(gate_row[n] > high) if gate_row is not None else False
            rising = g and not prev
            prev = g

            if rising:
                # Every mode restarts the rise from the CURRENT level; in
                # loop mode that is what makes ``trig`` a click-free sync.
                cnt = int(round(rise_len * (level ** inv_rise))) if level > 0.0 else 0
                phase = self._FG_RISE
            elif gating and not g and (phase == self._FG_RISE or phase == self._FG_HOLD):
                # Gate released: fall from wherever the rise got to.
                pos = level ** inv_fall if level > 0.0 else 0.0
                cnt = int(round(fall_len * (1.0 - pos)))
                phase = self._FG_FALL

            if phase == self._FG_RISE:
                cnt += 1
                if cnt >= rise_len:
                    cnt = 0
                    level = 1.0
                    phase = rise_next
                    eor_left = pulse_len
                else:
                    level = (cnt / rise_len) ** k_rise
            elif phase == self._FG_HOLD:
                level = 1.0
            elif phase == self._FG_FALL:
                cnt += 1
                if cnt >= fall_len:
                    cnt = 0
                    level = 0.0
                    phase = fall_next
                    eoc_left = pulse_len
                else:
                    level = (1.0 - cnt / fall_len) ** k_fall
            else:  # idle: parked at 0 until the next trigger
                level = 0.0

            out[n] = level
            if eor_left > 0:
                eor[n] = 1.0
                eor_left -= 1
            if eoc_left > 0:
                eoc[n] = 1.0
                eoc_left -= 1

        st["phase"] = phase
        st["cnt"] = cnt
        st["level"] = level
        st["prev_gate"] = prev
        st["eor_left"] = eor_left
        st["eoc_left"] = eoc_left

    def _render_fg_mono(self, module, gate_buf, frames, *args):
        """Mono path: one function, ``(F,)`` on each jack."""
        state = self._state.setdefault(module.id, None)
        if state is None or "voices" in state:  # fresh, or was the voice branch
            state = self._fg_fresh_state()
            self._state[module.id] = state

        out = np.zeros(frames, dtype=np.float32)
        eor = np.zeros(frames, dtype=np.float32)
        eoc = np.zeros(frames, dtype=np.float32)
        self._fg_kernel(frames, gate_buf, *args, state, out, eor, eoc)
        return {"out": out, "out_inv": 1.0 - out, "eor": eor, "eoc": eoc}

    def _render_fg_voice(self, module, gate_buf, frames, mode, k, inv_k, lengths):
        """Voice path: V independent functions, ``(V, F)`` on each jack.

        ``lengths`` is one ``(rise_len, fall_len, pulse_len)`` tuple per
        slot -- V copies of the same tuple under the mono rate law, one
        each when ``rise_cv`` / ``fall_cv`` arrived per voice.

        One scalar kernel run per slot. A slot whose trigger row is all
        low and whose function has already finished is skipped outright
        -- in a 16-slot poly patch most slots are silent most of the
        time, and that skip is worth more than any vectorization.
        """
        V = gate_buf.shape[0]
        state = self._state.setdefault(module.id, None)
        if state is None or "voices" not in state or len(state["voices"]) != V:
            state = {"voices": [self._fg_fresh_state() for _ in range(V)]}
            self._state[module.id] = state
        voices = state["voices"]

        out = np.zeros((V, frames), dtype=np.float32)
        eor = np.zeros((V, frames), dtype=np.float32)
        eoc = np.zeros((V, frames), dtype=np.float32)
        any_high = (gate_buf > self._GATE_HIGH).any(axis=1)

        for v in range(V):
            st = voices[v]
            if (
                not any_high[v]
                and st["phase"] == self._FG_IDLE
                and st["eor_left"] == 0
                and st["eoc_left"] == 0
            ):
                # Nothing to do: parked at 0 with no gate and no pulse in
                # flight. The rows are already zeros.
                st["prev_gate"] = False
                st["level"] = 0.0
                continue
            self._fg_kernel(
                frames, gate_buf[v], mode, k, inv_k, *lengths[v],
                st, out[v], eor[v], eoc[v],
            )
        # A parked slot's row is 0.0, so its inverse is 1.0 -- the ducker
        # is OPEN on a silent voice, which is what "1 - out" means.
        return {"out": out, "out_inv": 1.0 - out, "eor": eor, "eoc": eoc}

    # ----- LFO rendering --------------------------------------------------

    def _render_lfo(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Low-frequency oscillator emitting a CV signal.

        Shape-polymorphic (slice 3b.2). The branch is decided by
        ``rate_cv``'s shape -- phase state is what changes with rate:

          * ``rate_cv`` 2D ``(V, F)`` -> voice-aware path. V independent
            phase accumulators, one per voice slot, each clocked at its
            own per-voice block-mean rate. Output ``(V, F)``.
          * ``rate_cv`` 1D ``(F,)`` or None -> mono path with a single
            phase accumulator. Output ``(F,)``.

        Phase state is per-module so multiple LFOs in one patch don't
        share state. ``random`` waveform is sample-and-hold: re-roll
        once per cycle when the phase wraps past 1.0, with per-voice
        S&H values in the voice branch so independently-clocked voices
        roll their own randoms on their own wrap edges.

        ``reset`` (gate, 2026-09-19): a rising edge restarts the phase
        at the ``phase`` param ON THAT SAMPLE -- the block is rendered
        in segments between edges, each segment a vectorized ramp from
        its own start phase, so a keyboard gate into ``reset`` gives
        every note a vibrato/tremolo that begins at the same place.
        ``phase`` is also where the free-running LFO starts, and moving
        the knob re-anchors the phase there at the next block (an
        implicit edge at sample 0), so the slider is audible without a
        cable. Unpatched ``reset`` + ``phase`` 0.0 is one segment
        starting from the carried phase: the pre-love-pass ramp,
        bit-exact. The ``random`` waveform rolls a fresh value on a
        reset edge as well as on a wrap.

        ``seed`` (2026-09-20): 0 (the default) is the old draw -- the
        ``random`` waveform rolls from numpy's GLOBAL rng, bit-exact
        under ``np.random.seed``. N != 0 holds a private
        ``default_rng(N)`` in state, re-created when the seed changes
        and consumed ONLY when a value is due (a wrap or a reset edge),
        in time order, so the seeded S&H is the same run to run and
        bit-exact across block sizes. A reset edge RE-SEEDS it (the
        sequencer's precedent): with a bar-rate reset on both the
        sequencer and the LFO, a random LFO plays the same random
        phrase every bar -- the musical point. The voice path keeps one
        generator per voice, every copy seeded alike, so each voice
        plays the phrase from its own reset (see _render_lfo_voice).
        Non-random waveforms never touch the generator.
        """
        # collapse=False so a voice-aware (V, F) rate_cv reaches us
        # with the voice axis intact. ``buffers``/``patch`` are None
        # when called from unit tests in isolation, in which case
        # rate_cv is unavailable -- same back-compat trick we use on
        # _render_oscillator.
        rate_cv = None
        reset = None
        if buffers is not None and patch is not None:
            rate_cv = self._input_buffer(
                patch, buffers, module.id, "rate_cv", collapse=False
            )
            # Also uncollapsed: the voice path resets each voice from
            # its own row; the mono path collapses it itself (sum ->
            # any-voice-high, the house rule from _input_buffer).
            reset = self._input_buffer(
                patch, buffers, module.id, "reset", collapse=False
            )

        if rate_cv is not None and rate_cv.ndim == 2:
            return self._render_lfo_voice(module, frames, rate_cv, reset)
        return self._render_lfo_mono(module, frames, rate_cv, reset)

    @staticmethod
    def _lfo_phase_segments(start, phase0, edges, frames, phase_inc):
        """Phase ramp for one block, restarted at ``phase0`` on each edge.

        ``edges`` are the sample indices (ascending) where a reset
        lands; the ramp before the first edge continues from ``start``.
        Returns ``(phases, end_phase)`` where ``end_phase`` is the phase
        the NEXT block continues from. With no edges this is exactly the
        one-segment expression the LFO always used --
        ``(start + arange(frames) * inc) % 1`` and
        ``(start + frames * inc) % 1`` -- which is what keeps the
        unpatched render bit-exact.
        """
        ramp = np.arange(frames, dtype=np.float64)
        phases = np.empty(frames, dtype=np.float64)
        bounds = [0, *(int(e) for e in edges), frames]
        seg_start = start
        end_phase = start
        for a, b in zip(bounds[:-1], bounds[1:]):
            n = b - a
            phases[a:b] = (seg_start + ramp[:n] * phase_inc) % 1.0
            end_phase = (seg_start + n * phase_inc) % 1.0
            # Every segment after the first begins at a reset edge.
            seg_start = phase0
        return phases, float(end_phase)

    def _lfo_reset_edges(self, reset_row, prev_high, frames):
        """Rising-edge sample indices of one gate row, carried across blocks.

        Returns ``(edges, last_high)``; ``prev_high`` is the row's level
        at the end of the previous block so an edge that straddles a
        block boundary still counts exactly once.
        """
        high = np.asarray(reset_row[:frames]) > self._GATE_HIGH
        if high.size == 0:
            return np.zeros(0, dtype=np.intp), bool(prev_high)
        prev = np.empty_like(high)
        prev[0] = prev_high
        prev[1:] = high[:-1]
        return np.flatnonzero(high & ~prev), bool(high[-1])

    @staticmethod
    def _lfo_seed(module):
        """The ``seed`` param as a non-negative int; 0 = the global rng."""
        try:
            seed = int(module.params.get("seed", 0))
        except (TypeError, ValueError):
            seed = 0
        return max(0, seed)

    @staticmethod
    def _lfo_roll(rng):
        """One S&H value in [-1, 1): the private generator when there is
        one, else numpy's global rng -- the exact pre-seed call, so
        ``seed`` 0 stays bit-exact under ``np.random.seed``."""
        if rng is None:
            return float(np.random.uniform(-1.0, 1.0))
        return float(rng.uniform(-1.0, 1.0))

    def _render_lfo_mono(self, module, frames, rate_cv, reset=None):
        """Mono fast path -- scalar phase, vectorized phase ramp.

        The scalar ramp + waveshape is the pre-slice-3b.2 code; the
        reset gate only splits the ramp into segments (see
        ``_lfo_phase_segments``), so with ``reset`` unpatched every
        existing LFO test passes bit-for-bit identically.
        """
        # ``phase`` wraps (1.0 is 0.0 again; -0.25 is 0.75) so a
        # hand-edited patch can't park the ramp outside a cycle.
        phase0 = float(module.params.get("phase", 0.0)) % 1.0
        fresh = {
            "phase": phase0,
            "random_value": 0.0,
            # First block rolls the S&H value regardless of where the
            # phase starts (the old ``phase == 0 and value == 0`` test
            # never fired for a non-zero ``phase``).
            "random_fresh": True,
            "reset_prev": False,
            "phase_param": phase0,
            "seed": None,
            "rng": None,
        }
        state = self._state.setdefault(module.id, dict(fresh))
        # If state belongs to the voice branch (different keys),
        # discard and reinit to mono shape.
        if "phase_arr" in state:
            state.clear()
            state.update(fresh)
        for key, value in fresh.items():
            state.setdefault(key, value)
        seed = self._lfo_seed(module)
        if state["seed"] != seed:
            # A live seed change re-rolls the stream on the spot; the
            # held value stays until the next event. 0 = no private
            # generator: the global rng, exactly the pre-seed draw.
            state["seed"] = seed
            state["rng"] = np.random.default_rng(seed) if seed else None

        waveform = str(module.params.get("waveform", "sine"))
        rate = float(module.params.get("rate", 4.0))
        depth = float(module.params.get("depth", 1.0))
        bipolar = bool(module.params.get("bipolar", False))

        sr = self.sample_rate
        # CV-modulate the rate: octaves per CV unit scaled by
        # ``cv_depth`` (default 1.0 = 1 V/oct), block-mean. If rate_cv
        # is 2D for any reason it shouldn't reach this branch -- the
        # dispatcher routes (V, F) to the voice path. mean() over a
        # 1D slice is the same as the old code.
        if rate_cv is not None and rate_cv.size > 0:
            cv_depth = float(module.params.get("cv_depth", 1.0))
            # The exponent is clipped to +-64 octaves BEFORE the power:
            # a Python-float ``2.0 ** 1e6`` raises OverflowError (the
            # filter pass found an absurd CV taking the render down),
            # and either bound is far outside the rate clamp below, so
            # the clip is a no-op for any sane CV. The mean comes from
            # _finite_mean, so a NaN cable reads as no modulation (the
            # voice path's np.clip used to propagate it into the phase)
            # and a constant CV is the same rate at every block size.
            octaves = float(
                np.clip(cv_depth * self._finite_mean(rate_cv), -64.0, 64.0)
            )
            rate = rate * float(2.0 ** octaves)

        # Clamp to a safe range: 0.001 Hz floor (one cycle per ~17 min)
        # and an effective ceiling at Nyquist/2 -- beyond that an LFO
        # is just an audio oscillator and the user should reach for
        # ``oscillator``.
        rate = max(0.001, min(rate, sr * 0.45))
        depth = max(0.0, min(depth, 1.0))

        phase_inc = rate / sr
        start_phase = state["phase"]

        # Reset edges this block. A (V, F) gate on the mono path sums
        # to any-voice-high, exactly as _input_buffer would collapse it.
        edges = np.zeros(0, dtype=np.intp)
        if reset is not None and frames > 0:
            if reset.ndim == 2:
                reset = self._voice_sum(reset)
            edges, state["reset_prev"] = self._lfo_reset_edges(
                reset, state["reset_prev"], frames
            )
        # A moved ``phase`` knob is a reset by decree at sample 0, so
        # the slider is audible on a free-running LFO too.
        if state["phase_param"] != phase0 and frames > 0:
            state["phase_param"] = phase0
            if edges.size == 0 or edges[0] != 0:
                edges = np.concatenate([[0], edges]).astype(np.intp)

        phases, new_phase = self._lfo_phase_segments(
            start_phase, phase0, edges, frames, phase_inc
        )

        if waveform == "sine":
            wave = np.sin(2.0 * np.pi * phases)
        elif waveform == "triangle":
            wave = 1.0 - 4.0 * np.abs(phases - 0.5)
        elif waveform == "square":
            wave = np.where(phases < 0.5, 1.0, -1.0)
        elif waveform == "saw":
            wave = 2.0 * phases - 1.0
        elif waveform == "random":
            # Sample-and-hold: detect each phase wrap and re-roll. A
            # reset edge rolls too (once, even when the jump to
            # ``phase0`` also reads as a wrap).
            if frames > 0:
                diffs = np.diff(np.concatenate([[start_phase], phases]))
                is_reset = np.zeros(frames, dtype=bool)
                is_reset[edges] = True
                wave = np.empty(frames, dtype=np.float64)
                current = state["random_value"]
                rng = state["rng"]
                if state["random_fresh"]:
                    current = self._lfo_roll(rng)
                    state["random_fresh"] = False
                for i in range(frames):
                    if diffs[i] < 0.0 or is_reset[i]:
                        if rng is not None and is_reset[i]:
                            # A reset edge re-seeds the private stream
                            # so the phrase replays from the top (the
                            # sequencer's precedent): a bar reset here
                            # plays the same random phrase every bar.
                            rng = state["rng"] = np.random.default_rng(seed)
                        current = self._lfo_roll(rng)
                    wave[i] = current
                state["random_value"] = current
            else:
                wave = np.zeros(0, dtype=np.float64)
        else:
            wave = np.zeros(frames, dtype=np.float64)

        state["phase"] = new_phase

        if not bipolar:
            # Map [-1, 1] -> [0, 1] (so a sine-LFO into a VCA gives a smooth
            # tremolo rather than an inverted-phase audio fight).
            wave = (wave + 1.0) * 0.5

        return (wave * depth).astype(np.float32)

    def _render_lfo_voice(self, module, frames, rate_cv, reset=None):
        """Voice-aware path -- V independent phase accumulators.

        ``rate_cv`` is ``(V, F)``. Each voice gets its own block-mean
        rate (1V/oct) and accumulates phase at its own increment, with
        per-voice phase state persisting across blocks. Output is
        ``(V, F)``.

        Block-mean cadence on the per-voice rate matches the mono path
        -- one rate per block per voice. Audio-rate rate modulation
        would need per-sample increments via cumsum (cf. the oscillator
        voice path); the LFO is sub-audio by definition, so block-mean
        is the right cost/quality trade-off.

        Per-voice phase persists across blocks rather than resetting on
        retrigger -- mirrors the oscillator voice-path policy and
        avoids click on rate jumps -- unless the patch says otherwise
        through ``reset``: a ``(V, F)`` gate resets each voice from its
        own row, a mono gate resets every voice. For ``random`` waveform
        each voice carries its own sample-and-hold value, re-rolled on
        its own phase wrap (independently-clocked voices roll
        independently) and on its own reset edges.

        Draw order under ``seed``: with seed 0 every voice rolls from
        the global rng, voice-major per block (voice 0's events this
        block in time order, then voice 1's, ...) -- the pre-seed
        shape, bit-exact. With seed N each voice holds ITS OWN copy of
        ``default_rng(N)``: the phrase is a property of the LFO and
        every voice plays it from its own reset (a ``(V, F)`` reset on
        voice v re-seeds voice v alone; voices reset together move in
        lockstep), each copy consumed only at that voice's events in
        time order, so a single voice row is bit-identical to the mono
        path and a block split never changes a draw. Independent voices
        under a seed are what seed 0 is for.
        """
        V = rate_cv.shape[0]
        phase0 = float(module.params.get("phase", 0.0)) % 1.0
        state = self._state.setdefault(module.id, {})

        # Reinit if state belongs to the mono branch or the voice
        # count changed.
        needs_reinit = (
            "phase_arr" not in state
            or state["phase_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["phase_arr"] = np.full(V, phase0, dtype=np.float64)
            state["random_arr"] = np.zeros(V, dtype=np.float64)
            state["random_fresh_arr"] = np.ones(V, dtype=bool)
            state["reset_prev_arr"] = np.zeros(V, dtype=bool)
            state["phase_param"] = phase0
        state.setdefault("random_fresh_arr", np.ones(V, dtype=bool))
        state.setdefault("reset_prev_arr", np.zeros(V, dtype=bool))
        state.setdefault("phase_param", phase0)
        seed = self._lfo_seed(module)
        if state.get("seed") != seed:
            # One private generator PER VOICE, every copy seeded alike
            # (see the docstring); a reinit above dropped the key, so a
            # changed voice count rebuilds them at the new V.
            state["seed"] = seed
            state["rng_arr"] = [
                np.random.default_rng(seed) if seed else None for _ in range(V)
            ]

        waveform = str(module.params.get("waveform", "sine"))
        base_rate = float(module.params.get("rate", 4.0))
        depth = float(module.params.get("depth", 1.0))
        bipolar = bool(module.params.get("bipolar", False))

        sr = self.sample_rate
        depth_c = max(0.0, min(depth, 1.0))

        # Per-voice block-mean rate (octaves/unit x cv_depth, default
        # 1 V/oct). Each voice gets its own phase increment for this
        # block.
        cv_depth = float(module.params.get("cv_depth", 1.0))
        cv_block_mean = self._finite_mean(rate_cv, axis=1)  # (V,) float64
        # Exponent clipped to +-64 octaves first, the mono path's rule.
        # np.power would only warn and hand the clamp below an inf, but
        # the two paths should agree on the bound. _finite_mean already
        # gave float64 rows with any non-finite row read as 0.
        octaves = np.clip(cv_depth * cv_block_mean, -64.0, 64.0)
        rate_per_voice = base_rate * np.power(2.0, octaves)
        rate_per_voice = np.clip(rate_per_voice, 0.001, sr * 0.45)

        phase_inc_per_voice = rate_per_voice / sr  # (V,)
        start_phase = state["phase_arr"]  # (V,)

        # Per-voice phase ramps via broadcast: (V, 1) + (1, F) * (V, 1)
        # -> (V, F). Each row is a phase ramp clocked at that voice's
        # rate.
        step = np.arange(frames, dtype=np.float64)  # (F,)
        phases = (
            start_phase[:, None]
            + step[None, :] * phase_inc_per_voice[:, None]
        ) % 1.0  # (V, F)
        new_phase = (start_phase + frames * phase_inc_per_voice) % 1.0

        # Reset edges, per voice. A mono gate is broadcast to every row;
        # a (V, F) gate resets each voice from its own row (a foreign
        # voice count -- not a real patching -- collapses to
        # any-voice-high and broadcasts). Rows with an edge get their
        # ramp rebuilt in segments; the rest keep the broadcast ramp,
        # so the common no-reset block costs nothing extra.
        edges_per_voice = [np.zeros(0, dtype=np.intp)] * V
        if reset is not None and frames > 0:
            if reset.ndim == 2 and reset.shape[0] != V:
                reset = self._voice_sum(reset)
            reset2d = (
                reset[:, :frames] if reset.ndim == 2
                else np.broadcast_to(reset[:frames], (V, frames))
            )
            prev_arr = state["reset_prev_arr"]
            for v in range(V):
                edges_per_voice[v], prev_arr[v] = self._lfo_reset_edges(
                    reset2d[v], prev_arr[v], frames
                )
        if state["phase_param"] != phase0 and frames > 0:
            # The moved knob: every voice re-anchors at sample 0.
            state["phase_param"] = phase0
            edges_per_voice = [
                e if (e.size and e[0] == 0)
                else np.concatenate([[0], e]).astype(np.intp)
                for e in edges_per_voice
            ]
        reset_rows = [v for v in range(V) if edges_per_voice[v].size]
        for v in reset_rows:
            phases[v], new_phase[v] = self._lfo_phase_segments(
                float(start_phase[v]), phase0, edges_per_voice[v],
                frames, float(phase_inc_per_voice[v]),
            )

        if waveform == "sine":
            wave = np.sin(2.0 * np.pi * phases)
        elif waveform == "triangle":
            wave = 1.0 - 4.0 * np.abs(phases - 0.5)
        elif waveform == "square":
            wave = np.where(phases < 0.5, 1.0, -1.0)
        elif waveform == "saw":
            wave = 2.0 * phases - 1.0
        elif waveform == "random":
            # Per-voice sample-and-hold. Each voice independently
            # detects its own phase wrap and re-rolls. Serial across
            # voices (S&H isn't vectorizable -- each row's output
            # depends on its own prior value on the wrap edges).
            wave = np.empty((V, frames), dtype=np.float64)
            random_arr = state["random_arr"]
            fresh_arr = state["random_fresh_arr"]
            rng_arr = state["rng_arr"]
            for v in range(V):
                row_phases = phases[v]
                row_start = float(start_phase[v])
                current = float(random_arr[v])
                rng = rng_arr[v]
                if frames > 0:
                    if fresh_arr[v]:
                        current = self._lfo_roll(rng)
                        fresh_arr[v] = False
                    diffs = np.diff(np.concatenate([[row_start], row_phases]))
                    is_reset = np.zeros(frames, dtype=bool)
                    is_reset[edges_per_voice[v]] = True
                    for i in range(frames):
                        if diffs[i] < 0.0 or is_reset[i]:
                            if rng is not None and is_reset[i]:
                                # This voice's reset replays the phrase
                                # on this voice alone.
                                rng = rng_arr[v] = np.random.default_rng(seed)
                            current = self._lfo_roll(rng)
                        wave[v, i] = current
                    random_arr[v] = current
            state["random_arr"] = random_arr
        else:
            wave = np.zeros((V, frames), dtype=np.float64)

        state["phase_arr"] = new_phase

        if not bipolar:
            wave = (wave + 1.0) * 0.5

        return (wave * depth_c).astype(np.float32)

    # ----- Slew rendering -------------------------------------------------

    # exp(-_LN100 / (t*sr)) is the one-pole coefficient whose step reaches
    # 99% in t seconds, so 'exponential' and 'linear' arrive in the same
    # wall-clock at equal rise/fall settings (only the curve differs).
    _LN100 = 4.605170185988092

    # slew: clock-sync period measured from the last two clock edges.
    _SLEW_MAX_OCT = 5.0

    def _render_slew(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Slew-limit ``in`` toward its target with independent rise/fall times.

        A per-sample recurrence (the output chases the input; ``y[n]`` depends
        on ``y[n-1]`` and, for the asymmetric case, on the direction of
        travel). Two engines:

          * SYMMETRIC exponential (rise == fall) is a plain LTI one-pole, so
            it runs as a single ``scipy.signal.lfilter`` over the whole block
            and every voice at once (C speed) -- the cheap common case,
            including one-time-knob glide. With per-voice times (a ``(V, F)``
            ``rise_cv``/``fall_cv``) it is one lfilter per row instead.
          * everything else (linear; asymmetric exponential) is the genuine
            scan, run in pure-Python scalar math per voice row. The earlier
            numpy-per-sample form paid a ufunc dispatch per sample (~20% of a
            core for one mono slew -- overhead, not arithmetic), so scalars
            are ~15x cheaper. Heavy polyphonic *linear* slew is the one case
            still worth an analytic/JIT pass later, the ADSR's escape hatch.

        Shape-polymorphic via ``collapse=False``: a mono ``(F,)`` in slews one
        running value, a voice ``(V, F)`` in slews one per voice slot (no
        crosstalk). The running value is carried across blocks and primed to
        the first input sample, so the output starts *at* the signal rather
        than swooping up from 0. Unpatched ``in`` -> silence (nothing to
        slew), and its state is dropped so a re-patch primes fresh.

        Two shapes (``shape`` param):
          * ``linear`` -- constant rate; ``rise_time`` / ``fall_time`` are
            seconds per 1.0 unit of change, so the step per sample is
            ``1/(t*sr)`` and the output *reaches* the target.
          * ``exponential`` -- asymmetric one-pole; the coefficient is chosen
            (via :data:`_LN100`) so the times read as ~time-to-99%. The pole
            depends on whether the target is above or below the current value,
            which is why it can't collapse to a single LTI ``lfilter`` call.
        A non-positive time means instant on that side.

        v2 (2026-09-14), everything OFF when unpatched so the shipped render
        is untouched:
          * ``clock`` (gate): while patched, the two times are read as
            MULTIPLES OF THE CLOCK PERIOD (1.0 = one beat of whatever is
            cabled) instead of seconds -- tempo-relative glide. The period
            is the distance between the last two rising edges, carried
            across blocks (the euclidean convention); until two edges have
            been seen the times fall back to seconds.
          * ``rise_cv`` / ``fall_cv`` (cv): 1 V/oct on the RATE, the
            function generator's law -- +1 halves the time, -1 doubles it,
            clamped to +-5 octaves, block-mean. Per voice when the CV is
            ``(V, F)`` against a ``(V, F)`` input (velocity -> glide time);
            otherwise one law for every row.
        """
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if cv_in is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)
        rise_cv = self._input_buffer(
            patch, buffers, module.id, "rise_cv", collapse=False
        )
        fall_cv = self._input_buffer(
            patch, buffers, module.id, "fall_cv", collapse=False
        )
        clock = self._input_buffer(patch, buffers, module.id, "clock")

        voiced = cv_in.ndim == 2
        x = (cv_in if voiced else cv_in[None, :]).astype(np.float64)  # (V, F)
        V = x.shape[0]

        # Per-voice running value, carried across blocks. Prime to the first
        # input sample on the first block (or when the voice count changes).
        state = self._state.setdefault(module.id, {})
        cur = state.get("cur")
        if cur is None or cur.shape[0] != V:
            cur = x[:, 0].copy()
        else:
            cur = cur.astype(np.float64, copy=True)

        sr = self.sample_rate
        shape = str(module.params.get("shape", "linear"))
        try:
            rise = float(module.params.get("rise_time", 0.1))
            fall = float(module.params.get("fall_time", 0.1))
        except (TypeError, ValueError):
            rise = fall = 0.1
        rise = max(0.0, rise)
        fall = max(0.0, fall)

        # --- clock sync: times become multiples of the measured period ----
        base = int(state.get("samples", 0))
        state["samples"] = base + frames
        if clock is not None:
            hi = clock > self._GATE_HIGH
            prev_c = bool(state.get("prev_clock", False))
            lag = np.empty(frames, dtype=bool)
            lag[0] = prev_c
            lag[1:] = hi[:-1]
            edges = np.flatnonzero(hi & ~lag)
            last_edge = int(state.get("last_edge", -1))
            interval = int(state.get("interval", 0))
            for n in edges.tolist():
                now = base + n
                if last_edge >= 0:
                    interval = now - last_edge
                last_edge = now
            state["prev_clock"] = bool(hi[-1])
            state["last_edge"] = last_edge
            state["interval"] = interval
            if interval > 0:
                period_s = interval / float(sr)
                rise *= period_s
                fall *= period_s
        else:
            state["prev_clock"] = False
            state["last_edge"] = -1
            state["interval"] = 0

        # --- rise_cv / fall_cv: 1 V/oct on the rate, per voice if (V, F) ---
        def _oct(cv):
            """Per-row octaves (V,) -- one shared value unless the CV is
            (V, F) against a (V, F) input. None when unpatched."""
            if cv is None or cv.size == 0:
                return None
            if cv.ndim == 2 and voiced and cv.shape[0] == V:
                # One 1-D mean per row -- the same reduction the mono
                # path runs, so a voice row fed a constant is bit-equal
                # to the mono render fed that constant.
                o = np.array(
                    [self._finite_mean(row) for row in cv], dtype=np.float64
                )
            else:
                o = np.full(
                    V, self._finite_mean(self._voice_sum(cv) if cv.ndim == 2 else cv)
                )
            return np.clip(o, -self._SLEW_MAX_OCT, self._SLEW_MAX_OCT)

        r_oct = _oct(rise_cv)
        f_oct = _oct(fall_cv)
        # The power through _pow2_clipped: _oct already clipped the
        # octaves, this scrubs a NaN row to "no modulation" (np.clip
        # passes NaN straight through).
        rise_v = (np.full(V, rise) if r_oct is None
                  else rise / self._pow2_clipped(r_oct, self._SLEW_MAX_OCT))
        fall_v = (np.full(V, fall) if f_oct is None
                  else fall / self._pow2_clipped(f_oct, self._SLEW_MAX_OCT))
        uniform = bool(np.all(rise_v == rise_v[0]) and np.all(fall_v == fall_v[0]))

        # Fast path: a SYMMETRIC exponential (rise == fall) is a plain LTI
        # one-pole, so the whole block -- every voice at once -- is a single
        # scipy.signal.lfilter call (C speed, GIL-releasing), with per-row zi
        # carrying the running value across blocks. zi = a*cur reproduces the
        # scalar recurrence's first sample exactly, so it stays bit-parity
        # with the loop below (which the asymmetric case still uses). With
        # per-voice times it is one call per row -- still C speed.
        if shape == "exponential" and bool(np.all(rise_v == fall_v)):
            if uniform:
                t = float(rise_v[0])
                a = 0.0 if t <= 0.0 else float(np.exp(-self._LN100 / (t * sr)))
                zi = (a * cur)[:, None]
                y, _zf = lfilter([1.0 - a], [1.0, -a], x, axis=-1, zi=zi)
            else:
                y = np.empty((V, frames), dtype=np.float64)
                for v in range(V):
                    t = float(rise_v[v])
                    a = 0.0 if t <= 0.0 else float(np.exp(-self._LN100 / (t * sr)))
                    y[v], _zf = lfilter([1.0 - a], [1.0, -a], x[v], zi=[a * cur[v]])
            state["cur"] = y[:, -1].copy()
            out = y if voiced else y[0]
            return out.astype(np.float32)

        # General path (linear any; asymmetric exponential): a per-sample
        # recurrence, run in PURE-PYTHON SCALARS per voice row. The previous
        # numpy-per-sample version paid ~one ufunc dispatch per sample (~20%
        # of a core for a single mono slew, V-independent -- overhead, not
        # arithmetic); scalar float math on a .tolist()'d row is ~15x cheaper.
        y = np.empty((V, frames), dtype=np.float64)
        if shape == "exponential":
            for v in range(V):
                rv = float(rise_v[v])
                fv = float(fall_v[v])
                a_rise = 0.0 if rv <= 0.0 else float(np.exp(-self._LN100 / (rv * sr)))
                a_fall = 0.0 if fv <= 0.0 else float(np.exp(-self._LN100 / (fv * sr)))
                xv = x[v].tolist()
                c = float(cur[v])
                row = [0.0] * frames
                for n in range(frames):
                    xn = xv[n]
                    a = a_rise if xn >= c else a_fall
                    c = a * c + (1.0 - a) * xn
                    row[n] = c
                y[v] = row
                cur[v] = c
        else:
            # Linear: max step per sample = 1/(t*sr); t<=0 -> instant (jump).
            for v in range(V):
                rv = float(rise_v[v])
                fv = float(fall_v[v])
                up = float("inf") if rv <= 0.0 else 1.0 / (rv * sr)
                dn = float("inf") if fv <= 0.0 else 1.0 / (fv * sr)
                xv = x[v].tolist()
                c = float(cur[v])
                row = [0.0] * frames
                for n in range(frames):
                    d = xv[n] - c
                    if d > up:
                        c += up
                    elif d < -dn:
                        c -= dn
                    else:
                        c = xv[n]
                    row[n] = c
                y[v] = row
                cur[v] = c

        state["cur"] = cur.copy()
        out = y if voiced else y[0]
        return out.astype(np.float32)

    # ----- Noise rendering ------------------------------------------------

    def _render_noise(self, module, frames: int, buffers=None, patch=None) -> dict:
        """White / pink / brown / violet noise; same stream on ``out`` and ``cv``.

        ``white`` is uniform ``[-1, 1]`` per sample (hard-bounded,
        bright). Every other colour is that white through one filter
        via ``scipy.signal.lfilter`` with its state ``zi`` carried in
        ``self._state`` across blocks (so the spectrum stays continuous
        at block seams and the stream is the same at any block size),
        then scaled to RMS-match white so ``amp`` means one level for
        all colours: ``pink`` through the class-level pinking IIR
        (``_PINK_SCALE``), ``brown`` through a leaky integrator whose
        corner is the ``corner`` param (2..40 Hz, default
        ``_BROWN_CORNER_HZ``; scale ``sqrt(1 - a^2)`` from the same
        pole, so the level does not move with the knob), ``violet``
        through a first difference (``_VIOLET_SCALE``). Switching colour
        drops the other colours' filter state.

        ``corner`` is brown's alone -- the other colours never read it.
        It is where the -6 dB/oct roll-off starts, not the tilt above
        it: measured, the 400-800 -> 800-1600 Hz slope is -6.00 dB/oct
        at 2, 10 and 40 Hz while the 2-20 Hz band moves from 39.0 dB
        over 200-2000 Hz (corner 2, distant thunder) to 23.5 dB (corner
        40, tight wind). Moving the knob renormalises the carried
        integrator state by the ratio of the old and new scales: the
        stationary level of the unscaled integrator is 1/scale, so
        carrying it across unchanged would step the output (a 2 -> 40 Hz
        jump measured a peak around 4x full scale) -- with the
        renormalisation the seam is smaller than a typical sample step
        and the stream settles into the new tilt over the new time
        constant.

        The die: ``seed`` 0 (default) draws from numpy's GLOBAL rng --
        the shipped free-running behaviour, so ``np.random.seed`` still
        pins a whole patch for a test. Any other seed is a private
        ``default_rng(seed)`` held in state and re-created when the seed
        changes: the stream is then the same run to run and the same at
        every block size (``Generator.uniform`` draws one word per
        sample, so chunking cannot move it). The seed alone is the key,
        deliberately not seed-plus-module-id: two modules with the same
        seed are the same stream (correlated stereo is a feature).

        ``amp_cv`` is a knobless per-sample amplitude (the CV IS the
        level, like ``vca.cv``), multiplied after the colour filter and
        ``amp``; unpatched = unity, bit-exact with the pre-CV render. A
        ``(V, F)`` source broadcasts the ONE mono stream to ``(V, F)``
        with a per-voice level -- every voice hears the same noise under
        its own envelope, not V independent streams.

        A source has no voice context of its own, so the output is
        otherwise mono ``(frames,)`` (like :class:`Constant`); a 1D
        signal broadcasts cleanly against any per-voice consumer. The
        same float32 array is returned under both port names --
        consumers treat buffers as read-only, exactly as fan-out from
        any single output already does.
        """
        from ...modules.noise import NOISE_CORNER_MAX, NOISE_CORNER_MIN

        color = str(module.params.get("color", "white"))
        amp = float(module.params.get("amp", 1.0))
        try:
            seed = int(module.params.get("seed", 0))
        except (TypeError, ValueError):
            seed = 0
        try:
            corner = float(module.params.get("corner", self._BROWN_CORNER_HZ))
        except (TypeError, ValueError):
            corner = self._BROWN_CORNER_HZ
        if not math.isfinite(corner):
            # min/max don't propagate NaN -- scrub before you clamp.
            corner = self._BROWN_CORNER_HZ
        corner = min(max(corner, NOISE_CORNER_MIN), NOISE_CORNER_MAX)

        state = self._state.setdefault(module.id, {})

        if seed == 0:
            # Free-running: the global rng, exactly as shipped. Drop any
            # private die so a later non-zero seed starts fresh.
            state.pop("seed", None)
            state.pop("rng", None)
            white = np.random.uniform(-1.0, 1.0, frames).astype(np.float32)
        else:
            if state.get("seed") != seed:
                state["seed"] = seed
                state["rng"] = np.random.default_rng(seed)
            white = state["rng"].uniform(-1.0, 1.0, frames).astype(np.float32)

        # One "<colour>_zi" per filtered colour; a colour switch at
        # runtime drops the others so a stale tail cannot leak into the
        # new colour (white carries none).
        for key in ("pink_zi", "brown_zi", "violet_zi"):
            if key != color + "_zi":
                state.pop(key, None)
        if color != "brown":
            # The renormalisation key rides with brown's own state.
            state.pop("brown_scale", None)

        if color == "pink":
            zi = state.get("pink_zi")
            if zi is None:
                zi = np.zeros(
                    max(len(self._PINK_A), len(self._PINK_B)) - 1, dtype=np.float64
                )
            filtered, zf = lfilter(self._PINK_B, self._PINK_A, white, zi=zi)
            state["pink_zi"] = zf
            sig = (filtered * self._PINK_SCALE).astype(np.float32)
        elif color == "brown":
            pole = 1.0 - 2.0 * math.pi * corner / float(self.sample_rate)
            scale = math.sqrt(1.0 - pole * pole)
            zi = state.get("brown_zi")
            if zi is None:
                zi = np.zeros(1, dtype=np.float64)
            prev_scale = state.get("brown_scale")
            if prev_scale is not None and prev_scale != scale:
                # The knob moved between blocks: renormalise the carried
                # state into the new scale so the output is continuous
                # (see the docstring -- otherwise the seam bangs).
                zi = zi * (prev_scale / scale)
            state["brown_scale"] = scale
            filtered, zf = lfilter((1.0,), (1.0, -pole), white, zi=zi)
            state["brown_zi"] = zf
            sig = (filtered * scale).astype(np.float32)
        elif color == "violet":
            zi = state.get("violet_zi")
            if zi is None:
                zi = np.zeros(1, dtype=np.float64)
            filtered, zf = lfilter((1.0, -1.0), (1.0,), white, zi=zi)
            state["violet_zi"] = zf
            sig = (filtered * self._VIOLET_SCALE).astype(np.float32)
        else:
            # Any unknown colour is white.
            sig = white

        if amp != 1.0:
            sig = (sig * amp).astype(np.float32)

        # Knobless per-sample amplitude; collapse=False so a (V, F)
        # envelope reaches us intact and broadcasts the stream to (V, F).
        amp_cv = None
        if patch is not None and buffers is not None:
            amp_cv = self._input_buffer(
                patch, buffers, module.id, "amp_cv", collapse=False
            )
        if amp_cv is not None:
            sig = (sig * amp_cv).astype(np.float32, copy=False)
        # Same array on both jacks (read-only downstream, like any fan-out).
        return {"out": sig, "cv": sig}

    # ----- SampleHold rendering -------------------------------------------

    def _render_sample_hold(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Sample ``in`` on each rising edge of ``trig``; hold between edges.

        Vectorized by forward-fill -- no per-sample loop, the same trick
        the Schmitt trigger uses. A rising edge is ``gate high and the
        previous sample low`` (the previous sample of the first frame is
        the gate state carried from the end of the last block). The held
        value at sample n is the input value sampled at the most recent
        edge at or before n, found with ``np.maximum.accumulate`` over
        edge positions; samples before the first edge keep the value
        carried from the previous block.

        Shape-polymorphic on the inputs (collapse=False keeps the voice
        axis):

          * mono ``(F,)`` in/trig -> ``(F,)`` out, scalar held value and
            scalar held-gate carried across blocks.
          * a ``(V, F)`` on either input -> ``(V, F)`` out, per-voice
            held values and per-voice edge detection; a mono partner
            broadcasts across the voice axis (shared clock + per-voice
            sources, or per-voice clocks + one shared source).

        Conventions: an unpatched ``in`` is treated as 0 (pure S&H, no
        internal noise). An unpatched ``trig`` produces no edges, so the
        output simply holds its last value (0 at startup).

        Love pass (2026-09-19), every knob OFF at its default so the
        shipped render is untouched bit for bit:

          * ``mode`` ``track``: track-and-hold -- the output FOLLOWS
            ``in`` while the gate is high and holds the last value it
            saw at the fall. The same forward-fill, keyed on the last
            HIGH sample instead of the last rising edge.
          * ``prob`` / ``seed``: a rising edge samples only if a private
            seeded die (``np.random.default_rng(seed)`` in state, the
            bernoulli convention) says so -- one draw per edge, in time
            order, consumed ONLY when 0 < prob < 1 so prob 1 is
            literally today's code (rng untouched) and prob 0 never
            samples. The voice path throws one die per edge PER VOICE,
            time-major / voice-minor within a sample, so a block split
            never reorders the draws. In ``track`` mode the die is
            thrown at the window's rising edge and decides the whole
            window (a losing window keeps holding straight through).
          * ``glide``: a one-pole lag on the output (the slew's
            time-to-99% premise, :data:`_LN100`), run as a single
            ``lfilter`` with the running value carried as ``zi`` per
            voice -- bit-exact across block sizes. ``glide`` 0 runs no
            filter at all (and drops the lag state, so re-enabling it
            primes to the current held value instead of swooping from
            0). In ``track`` mode the lag sits on the followed signal
            too: it lags ``out``, whatever ``out`` came from.

        Love pass (2026-09-20) -- the chance becomes a jack:

          * ``prob_cv`` / ``prob_cv_depth``: at every rising edge the
            effective probability is ``clip(prob + depth * cv[edge], 0,
            1)``, read at the EDGE'S OWN SAMPLE, not as a block mean --
            the decision is made once per edge, so the CV at that
            instant is the honest value (a block mean would let a CV
            that moved after the edge vote on it, and would make the
            verdict depend on where the block boundaries fall). The draw
            rule is unchanged: a die is consumed only when 0 < p < 1 at
            that edge, so a CV that pins p at 1 leaves the generator
            untouched (``prob`` 0.5 with +0.5 at depth 1 IS ``prob`` 1.0,
            draw for draw) and an unpatched jack IS the scalar path,
            operation for operation. A non-finite CV sample reads as 0
            (p = ``prob``). A ``(V, F)`` CV is read per voice at that
            voice's edge and carries the voice axis the way ``in`` and
            ``trig`` do; a mono CV broadcasts; a ``(V', F)`` CV whose V'
            disagrees with the module's is averaged to mono. In ``track``
            mode the open window's verdict is the die at its edge
            (``track_on``), as for any 0 < ``prob`` < 1 -- the "every
            window wins" shortcut only holds while the chance is static.
        """
        in_buf = self._input_buffer(patch, buffers, module.id, "in", collapse=False)
        trig_buf = self._input_buffer(patch, buffers, module.id, "trig", collapse=False)
        cv_buf = self._input_buffer(patch, buffers, module.id, "prob_cv", collapse=False)

        mode = str(module.params.get("mode", "sample"))
        if mode not in ("sample", "track"):
            mode = "sample"
        try:
            prob = float(module.params.get("prob", 1.0))
        except (TypeError, ValueError):
            prob = 1.0
        prob = min(1.0, max(0.0, prob))
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)
        try:
            depth = float(module.params.get("prob_cv_depth", 1.0))
        except (TypeError, ValueError):
            depth = 1.0
        if not np.isfinite(depth):
            depth = 1.0
        try:
            glide = float(module.params.get("glide", 0.0))
        except (TypeError, ValueError):
            glide = 0.0
        # None = no lag at all (the shipped path); else the one-pole
        # coefficient whose step reaches 99% in ``glide`` seconds.
        a_glide = (
            None if glide <= 0.0
            else float(np.exp(-self._LN100 / (glide * self.sample_rate)))
        )

        # Voice dimension is set by whichever input carries the voice axis.
        v_in = in_buf.shape[0] if (in_buf is not None and in_buf.ndim == 2) else None
        v_trig = trig_buf.shape[0] if (trig_buf is not None and trig_buf.ndim == 2) else None
        v_cv = cv_buf.shape[0] if (cv_buf is not None and cv_buf.ndim == 2) else None

        if v_in is None and v_trig is None and v_cv is None:
            return self._render_sample_hold_mono(
                module, frames, in_buf, trig_buf, mode, prob, seed, a_glide,
                cv_buf, depth,
            )
        V = v_in if v_in is not None else (v_trig if v_trig is not None else v_cv)
        return self._render_sample_hold_voice(
            module, frames, in_buf, trig_buf, V, mode, prob, seed, a_glide,
            cv_buf, depth,
        )

    @staticmethod
    def _sample_hold_rng(state, seed):
        """The module's private die, (re)seeded when ``seed`` changes.

        Lives in the per-module state so a mono<->voice reshape (which
        clears the state) re-seeds too -- a reshape is a re-patch.
        """
        if state.get("seed") != seed:
            state["seed"] = seed
            state["rng"] = np.random.default_rng(seed)
        return state["rng"]

    @staticmethod
    def _sample_hold_edge_keep(rising, prob, rng, cv=None, depth=1.0):
        """Which raw rising edges win their ``prob`` draw.

        Returns a bool array shaped like ``rising`` that is True at the
        edges that sample. The die is thrown ONLY when 0 < prob < 1 --
        prob 1 keeps every edge with the rng untouched (the default
        must be bit-exact with the pre-love-pass render), prob 0 keeps
        none. One draw per edge; a 2-D ``rising`` (V, F) is walked
        time-major, voice-minor (``np.nonzero`` on the transpose), so
        the draw sequence is a function of the edge history alone and
        a block split cannot reorder it. ``rng.random(n)`` yields the
        same doubles as n scalar ``rng.random()`` calls, so the
        vectorised throw is the per-edge throw.

        ``cv`` (``prob_cv``, ``(F,)`` or ``(V, F)``) makes the chance
        per edge: ``p = clip(prob + depth * cv[edge], 0, 1)`` at each
        edge's own sample, a non-finite sample reading as ``prob``.
        Only the edges with 0 < p < 1 draw, in the same order, so a CV
        that pins p at 1 (or 0) consumes nothing and a CV sitting at 0
        is the scalar path draw for draw. Only the edge samples of the
        CV are ever touched -- O(edges), not O(F).
        """
        if cv is None:
            if prob >= 1.0:
                return rising
            if prob <= 0.0:
                return np.zeros_like(rising)
            keep = np.zeros_like(rising)
            if rising.ndim == 1:
                pos = np.flatnonzero(rising)
                if pos.size:
                    keep[pos] = rng.random(pos.size) < prob
            else:
                ns, vs = np.nonzero(rising.T)          # sorted by n, then v
                if ns.size:
                    keep[vs, ns] = rng.random(ns.size) < prob
            return keep

        keep = np.zeros_like(rising)
        if rising.ndim == 1:
            pos = np.flatnonzero(rising)
            if not pos.size:
                return keep
            sel = (pos,)
            at_edge = cv[pos]
        else:
            ns, vs = np.nonzero(rising.T)              # sorted by n, then v
            if not ns.size:
                return keep
            sel = (vs, ns)
            at_edge = cv[vs, ns] if cv.ndim == 2 else cv[ns]
        p = prob + depth * at_edge.astype(np.float64)
        p = np.where(np.isfinite(p), p, prob)
        p = np.clip(p, 0.0, 1.0)
        win = p >= 1.0                                 # certain: no draw
        need = (p > 0.0) & (p < 1.0)
        if need.any():
            win[need] = rng.random(int(need.sum())) < p[need]
        keep[sel] = win
        return keep

    @staticmethod
    def _sample_hold_windows(g, rising, keep, carry, axis):
        """Track mode: the samples that are inside a WINNING gate window.

        A window is a run of high samples starting at a raw rising
        edge; it is active iff that edge won its draw. ``carry`` is the
        active flag of the window still open at the block start (per
        voice for the 2-D case) -- a window that started in an earlier
        block keeps its verdict. Forward-fill of the verdict over edge
        positions, the module's usual trick.
        """
        n = g.shape[-1]
        ar = np.arange(n) if axis == 0 else np.arange(n)[None, :]
        evt = np.where(rising, ar, -1)
        last = np.maximum.accumulate(evt, axis=axis)
        if axis == 0:
            verdict = np.where(last >= 0, keep[np.maximum(last, 0)], carry)
        else:
            verdict = np.where(
                last >= 0, np.take_along_axis(keep, np.maximum(last, 0), axis=1),
                carry[:, None],
            )
        return g & verdict

    @staticmethod
    def _sample_hold_lag(state, out, a, prime):
        """``glide``: one-pole lag on the S&H output, per voice.

        ``a`` None means glide is 0: return ``out`` untouched (no filter
        runs -- the shipped path) and drop the lag state, so the next
        time glide is turned on the lag PRIMES to the value the output
        is sitting on (``prime``: the held value(s) from before this
        block) rather than swooping up from 0. Otherwise one
        ``lfilter`` over the block with ``zi = a * y[-1]``, which
        reproduces the scalar recurrence's first sample exactly (the
        slew's idiom), so a 64-block and a 512-block render agree bit
        for bit. The running value, not zf, is carried: a live glide
        change alters ``a`` and the right zi is then ``a_new * y[-1]``.
        """
        if a is None:
            state.pop("lag", None)
            return out
        prime = np.asarray(prime, dtype=np.float64)
        lag = state.get("lag")
        if lag is None or np.shape(lag) != prime.shape:
            lag = prime.copy()
        x = out.astype(np.float64)
        if x.ndim == 1:
            y, _zf = lfilter([1.0 - a], [1.0, -a], x, zi=[a * float(lag)])
            state["lag"] = np.float64(y[-1])
        else:
            y, _zf = lfilter([1.0 - a], [1.0, -a], x, axis=-1, zi=(a * lag)[:, None])
            state["lag"] = y[:, -1].copy()
        return y.astype(np.float32)

    def _render_sample_hold_mono(
        self, module, frames, in_buf, trig_buf, mode, prob, seed, a_glide,
        cv_buf=None, depth=1.0,
    ) -> np.ndarray:
        """Mono path -- scalar held value + held-gate carried across blocks.

        ``track`` mode swaps the edge mask for the (winning-window) high
        mask in the same forward-fill; ``prob`` (per edge through
        ``prob_cv`` when patched) thins the edges first; ``glide`` lags
        whatever comes out. Every return path goes through the lag so an
        unpatched clock still lets a glide finish.
        """
        state = self._state.setdefault(module.id, {"held": 0.0, "prev_gate": False})
        # Drop voice-shaped state if it leaked from a previous voice call.
        if "held_arr" in state:
            state.clear()
            state["held"] = 0.0
            state["prev_gate"] = False
        rng = self._sample_hold_rng(state, seed)

        held = float(state["held"])

        if trig_buf is None:
            # No clock -> no edges -> hold the last value across the block.
            out = np.full(frames, held, dtype=np.float32)
            return self._sample_hold_lag(state, out, a_glide, held)

        in_arr = (
            np.zeros(frames, dtype=np.float32)
            if in_buf is None
            else in_buf.astype(np.float32)
        )

        g = trig_buf > self._GATE_HIGH                 # (F,) bool
        g_prev = np.empty(frames, dtype=bool)
        g_prev[0] = bool(state["prev_gate"])
        g_prev[1:] = g[:-1]
        rising = g & ~g_prev                           # (F,) bool
        # Handed over as is: only its edge samples are ever read.
        keep = self._sample_hold_edge_keep(rising, prob, rng, cv_buf, depth)

        if mode == "track":
            # Follow while inside a winning window; the fill below then
            # holds the last followed sample through the low stretches.
            # At a static prob 1 every window wins, so a window already
            # open at the block start (a live flip to track under a held
            # gate) is simply "the gate was high". With prob_cv patched
            # the open window's verdict is whatever its edge rolled.
            carry = (
                bool(state["prev_gate"]) if (cv_buf is None and prob >= 1.0)
                else bool(state.get("track_on", False))
            )
            active = self._sample_hold_windows(g, rising, keep, carry, axis=0)
            state["track_on"] = bool(active[-1])
            key = active
        else:
            key = keep

        idx = np.where(key, np.arange(frames), -1)
        last = np.maximum.accumulate(idx)              # (F,) most-recent edge, -1 before any
        sampled = np.where(last >= 0, in_arr[np.maximum(last, 0)], held)

        state["held"] = float(sampled[-1])
        state["prev_gate"] = bool(g[-1])
        return self._sample_hold_lag(state, sampled.astype(np.float32), a_glide, held)

    def _render_sample_hold_voice(
        self, module, frames, in_buf, trig_buf, V, mode, prob, seed, a_glide,
        cv_buf=None, depth=1.0,
    ) -> np.ndarray:
        """Voice path -- per-voice held values + per-voice held-gate.

        A mono input is broadcast across the V voice rows so a shared
        clock can sample per-voice sources, or per-voice clocks can
        sample one shared source. The love-pass knobs are all per voice:
        one ``prob`` die per edge per voice (time-major order), one
        ``track`` window verdict per voice, one ``glide`` lag per voice.
        ``prob_cv`` is per voice too: a ``(V, F)`` CV gives each voice
        its own chance at its own edge (and can be the input that puts
        the module on this path), a mono CV is one chance for every
        voice, and a ``(V', F)`` CV of the wrong V' is averaged to mono
        rather than crashing the audio thread on a broadcast.
        """
        state = self._state.setdefault(module.id, {})
        needs_reinit = (
            "held_arr" not in state or state["held_arr"].shape[0] != V
        )
        if needs_reinit:
            state.clear()
            state["held_arr"] = np.zeros(V, dtype=np.float64)
            state["gate_arr"] = np.zeros(V, dtype=bool)
            state["track_arr"] = np.zeros(V, dtype=bool)
        rng = self._sample_hold_rng(state, seed)

        held_arr = state["held_arr"]                   # (V,)
        prime = held_arr.copy()                        # the lag's seat if it has none

        if trig_buf is None:
            out = np.broadcast_to(
                held_arr[:, None].astype(np.float32), (V, frames)
            ).copy()
            return self._sample_hold_lag(state, out, a_glide, prime)

        if trig_buf.ndim == 1:
            trig_2d = np.broadcast_to(trig_buf, (V, frames))
        else:
            trig_2d = trig_buf

        if in_buf is None:
            in_2d = np.zeros((V, frames), dtype=np.float32)
        elif in_buf.ndim == 1:
            in_2d = np.broadcast_to(in_buf.astype(np.float32), (V, frames))
        else:
            in_2d = in_buf.astype(np.float32)

        g = trig_2d > self._GATE_HIGH                  # (V, F) bool
        prev_col = state["gate_arr"][:, None]          # (V, 1)
        g_prev = np.concatenate([prev_col, g[:, :-1]], axis=1)
        rising = g & ~g_prev                           # (V, F)
        if cv_buf is not None and cv_buf.ndim == 2 and cv_buf.shape[0] != V:
            cv = self._voice_mean(cv_buf)              # the wrong V: averaged
        else:
            cv = cv_buf                                # None, (F,) or (V, F)
        keep = self._sample_hold_edge_keep(rising, prob, rng, cv, depth)

        if mode == "track":
            carry = (
                state["gate_arr"] if (cv is None and prob >= 1.0)
                else state["track_arr"]
            )
            active = self._sample_hold_windows(g, rising, keep, carry, axis=1)
            state["track_arr"] = active[:, -1].copy()
            key = active
        else:
            key = keep

        idx = np.where(key, np.arange(frames)[None, :], -1)
        last = np.maximum.accumulate(idx, axis=1)      # (V, F)
        sampled_vals = np.take_along_axis(in_2d, np.maximum(last, 0), axis=1)
        out = np.where(last >= 0, sampled_vals, held_arr[:, None])

        state["held_arr"] = out[:, -1].copy()
        state["gate_arr"] = g[:, -1].copy()
        return self._sample_hold_lag(state, out.astype(np.float32), a_glide, prime)

    # ----- Drift rendering -------------------------------------------------

    _DRIFT_RATE_MIN = 0.02
    _DRIFT_RATE_MAX = 50.0
    _DRIFT_TRIG_SEC = 0.001

    def _render_drift(self, module, frames: int, buffers, patch) -> dict:
        """Smooth wandering random CV (see modules/drift.py).

        Ticks are an INTEGER schedule: ``next = last_tick + round(sr /
        rate_eff)`` with the rate re-read per block (``rate_cv`` block
        mean, octaves), so with a constant rate every tick lands on the
        same absolute sample at any block size; with ``clock`` patched
        the ticks are its rising edges and the glide is sized from the
        measured edge interval (a jump until one exists). At each tick
        the next target is drawn from ``default_rng(seed)`` -- uniform in
        ±1 (``smooth``) or the previous target plus ``step`` x N(0, 1)
        reflected at ±1 (``walk``) -- and a new segment starts FROM THE
        CURRENT VALUE (not the old target, so a tick arriving mid-glide
        never jumps). Between ticks the value follows a half-cosine over
        ``max(1, round(glide x interval))`` samples then holds; each
        segment is one vectorized expression in absolute sample indices.
        ``stepped`` holds the target; ``trig`` is high for min(1 ms, half
        the interval) from each tick, carried across blocks.
        """
        from ...modules.drift import DRIFT_MODES

        rate_cv = self._input_buffer(patch, buffers, module.id, "rate_cv")
        clock = self._input_buffer(patch, buffers, module.id, "clock")

        def fparam(name, default, lo, hi):
            try:
                x = float(module.params.get(name, default))
            except (TypeError, ValueError):
                x = default
            return min(hi, max(lo, x))

        mode = str(module.params.get("mode", "smooth"))
        if mode not in DRIFT_MODES:
            mode = "smooth"
        rate = fparam("rate", 0.5, self._DRIFT_RATE_MIN, self._DRIFT_RATE_MAX)
        glide = fparam("glide", 1.0, 0.0, 1.0)
        step = fparam("step", 0.25, 0.0, 1.0)
        depth = fparam("depth", 1.0, 0.0, 1.0)
        bipolar = bool(module.params.get("bipolar", True))
        cv_depth = fparam("cv_depth", 1.0, -10.0, 10.0)
        try:
            seed = max(0, int(module.params.get("seed", 1)))
        except (TypeError, ValueError):
            seed = 1

        sr = float(self.sample_rate)
        if rate_cv is not None:
            rate = rate * self._pow2_clipped(cv_depth * self._finite_mean(rate_cv))
            rate = min(self._DRIFT_RATE_MAX, max(self._DRIFT_RATE_MIN, rate))
        interval = max(1, int(round(sr / rate)))
        trig_len = max(1, int(round(self._DRIFT_TRIG_SEC * sr)))

        st = self._state.setdefault(module.id, {})
        if st.get("seed") != seed:
            st.clear()
            st.update({
                "seed": seed, "rng": np.random.default_rng(seed),
                "n": 0,                 # absolute sample index of this block's start
                "last_tick": -1,        # absolute sample of the last tick (-1: none yet)
                "a": 0.0, "b": 0.0,     # segment start value and target
                "glide_n": 1,
                "trig_rem": 0,
                "prev_clock": False,
                "last_edge": -1,
            })
        rng = st["rng"]
        n0 = int(st["n"])
        last_tick = int(st["last_tick"])
        a = float(st["a"])
        b = float(st["b"])
        glide_n = int(st["glide_n"])
        trig_rem = int(st["trig_rem"])

        # --- where the ticks fall in this block (relative indices)
        if clock is not None:
            gt = clock > self._GATE_HIGH
            prev = np.concatenate(([bool(st["prev_clock"])], gt[:-1]))
            ticks = np.flatnonzero(gt & ~prev).tolist()
            st["prev_clock"] = bool(gt[-1])
        else:
            ticks = []
            nxt = 0 if last_tick < 0 else last_tick + interval
            if nxt < n0:
                nxt = n0            # the rate shortened past the elapsed time: now
            while nxt < n0 + frames:
                ticks.append(nxt - n0)
                nxt += interval

        def segment(start, end):
            """Value of the current segment over [start, end) (relative)."""
            i = np.arange(start, end, dtype=np.float64) + (n0 - last_tick + 1)
            u = np.minimum(1.0, i / glide_n)
            return a + (b - a) * 0.5 * (1.0 - np.cos(np.pi * u))

        value = np.empty(frames, dtype=np.float64)
        stepped = np.empty(frames, dtype=np.float64)
        trig = np.zeros(frames, dtype=np.float32)
        if trig_rem > 0:
            trig[:min(frames, trig_rem)] = 1.0
        pos = 0
        for t in ticks + [frames]:
            if t > pos:
                if last_tick < 0:
                    value[pos:t] = 0.0
                    stepped[pos:t] = 0.0
                else:
                    value[pos:t] = segment(pos, t)
                    stepped[pos:t] = b
            if t < frames:
                # a tick at relative t: start the new segment from where we are
                if last_tick < 0:
                    cur = 0.0
                else:
                    cur = float(segment(t - 1, t)[0]) if t > 0 else a + (b - a) * 0.5 * (
                        1.0 - np.cos(np.pi * min(1.0, (n0 - last_tick) / glide_n)))
                if mode == "walk":
                    nb = b + step * float(rng.standard_normal())
                    while nb > 1.0 or nb < -1.0:
                        nb = 2.0 - nb if nb > 1.0 else -2.0 - nb
                else:
                    nb = float(rng.uniform(-1.0, 1.0))
                abs_t = n0 + t
                if clock is not None:
                    seg_int = abs_t - int(st["last_edge"]) if int(st["last_edge"]) >= 0 else 0
                    st["last_edge"] = abs_t
                else:
                    seg_int = interval
                a, b = cur, nb
                last_tick = abs_t
                glide_n = max(1, int(round(glide * seg_int))) if seg_int > 0 else 1
                trig_rem = min(trig_len, max(1, seg_int // 2)) if seg_int > 0 else trig_len
                trig[t:min(frames, t + trig_rem)] = 1.0
            pos = t
        # trig samples left over for the next block
        if ticks:
            trig_rem = max(0, ticks[-1] + trig_rem - frames)
        else:
            trig_rem = max(0, trig_rem - frames)

        st["n"] = n0 + frames
        st["last_tick"] = last_tick
        st["a"] = a
        st["b"] = b
        st["glide_n"] = glide_n
        st["trig_rem"] = trig_rem

        if bipolar:
            cv = value * depth
            held = stepped * depth
        else:
            cv = (value + 1.0) * 0.5 * depth
            held = (stepped + 1.0) * 0.5 * depth
        return {
            "cv": cv.astype(np.float32),
            "stepped": held.astype(np.float32),
            "trig": trig,
        }
