"""Sequencing renderers: clock, sequencer, possibility sequencer and
selector, arpeggiator, chord, CV recorder, quantizer.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". The module-layer names these renderers used through
``numpy_backend``'s top-level imports (the quantizer's scale tables, the
sequencer's direction list and ``next_step_index``, and
``DIVIDER_MAX_SWING``, which ``_CLOCK_MAX_SWING`` needs at class-definition
time) are imported here instead, straight from ``pysynthrack.modules``
under the same aliases -- nothing in ``modules`` imports the audio
layer, so there is no cycle, and ``next_step_index`` runs once per
clock edge, too hot for a lazy import. The gate threshold ``_GATE_HIGH``
stays on the backend and the clock's reset-edge scan
``_lfo_reset_edges`` lives with the LFO in renderers/mod_sources.py; both
are reached through ``self``.
"""
from __future__ import annotations

import numpy as np

from ...modules.clockwork import DIVIDER_MAX_SWING
from ...modules.quantizer import (
    CUSTOM_KEYS as _Q_CUSTOM_KEYS,
)
from ...modules.quantizer import (
    QUANTIZER_ROOTS as _Q_ROOTS,
)
from ...modules.quantizer import (
    SCALE_INTERVALS as _Q_SCALES,
)
from ...modules.sequencer import (
    SEQ_DIRECTIONS as _SEQ_DIRECTIONS,
)
from ...modules.sequencer import (
    next_step_index as _seq_next_step_index,
)


class SequencingRenderers:
    # ----- clock / sequencer ---------------------------------------------

    # Sequencer step ceiling — mirrors modules.sequencer.MAX_STEPS. Kept
    # local to keep the backend free of a modules-layer import for a single
    # integer (same pattern as _MAX_VOICES).
    _SEQ_MAX_STEPS = 16

    # ``bpm_cv``'s exponent is clipped here BEFORE ``2 **`` -- x64 either
    # way is already past any musical tempo, and the filter pass found
    # that ``2.0 ** (depth * cv)`` on an absurd CV (a cv_math product
    # gone wild) raises OverflowError in the audio thread.
    _BPM_CV_EXP_LIMIT = 6.0
    # The clock's ``swing`` clamps where the divider's does (0.75); the
    # panel stops at 0.5, the hard shuffle.
    _CLOCK_MAX_SWING = DIVIDER_MAX_SWING

    def _render_clock(self, module, frames: int, buffers=None, patch=None) -> np.ndarray:
        """Tempo-driven gate pulse train, fully vectorized.

        Pulse frequency is ``bpm / 60 * division`` Hz. A float64 phase
        accumulator carries across blocks so pulses stay phase-continuous
        (no drift, no seam at block boundaries); the gate is high for the
        first ``pulse_width`` fraction of each unit phase period. Returns a
        mono ``(frames,)`` gate buffer.

        The phase is evaluated at samples 1..frames of each segment, so a
        fresh clock at phase 0 emits a rising edge on its very first
        sample (the downstream sequencer then plays step 1 immediately).

        The transport (love pass, 2026-09-19) -- three optional inputs,
        read only when ``buffers``/``patch`` are given (unit tests still
        call with two arguments and get the free-running clock):

          * ``reset`` (gate): a rising edge restarts the period AT that
            sample. The block is rendered in segments between edges (the
            LFO's ``reset`` idiom): every segment after the first starts
            from phase 0 evaluated at samples 1..n -- exactly what a
            brand-new clock does on its first block -- so a reset IS a
            fresh clock from that sample: the gate goes high on the reset
            sample itself and the following pulses count from it. A reset
            while the gate is already high just extends the pulse. The
            previous reset sample is carried in state so an edge that
            straddles a block boundary counts exactly once.
          * ``run`` (gate): unpatched = running. Patched: the clock runs
            while it is high and is HELD while low -- output low and the
            phase frozen (a held sample advances nothing), so a downstream
            sequencer keeps its step. A rising edge on ``run`` is a reset
            too: the first pulse lands on the sample play starts, the
            downbeat. A reset edge while held zeros the phase and emits
            nothing until run rises (which re-zeros it, so the held reset
            is inaudible by itself -- the sequencer's own ``reset`` is the
            jack that rewinds a stopped pattern).
          * ``bpm_cv`` (cv) + ``bpm_cv_depth``: tempo doublings per unit,
            ``bpm * 2 ** (depth * mean cv)``, block-rate like every
            block-mean CV here (a ``(V, F)`` source is averaged). The
            exponent is clipped to +/-``_BPM_CV_EXP_LIMIT`` before the
            power and a non-finite mean is read as 0.

        Gates are mono: a ``(V, F)`` source on ``reset`` or ``run`` is
        collapsed by ``_input_buffer`` to its voice sum, i.e. any-voice-
        high (the house rule).

        ``swing`` (love pass, 2026-09-20): every second pulse is late by
        ``swing`` of a period -- the clock_divider's convention (a
        fraction of the period, the pulse keeps its width). It is a phase
        offset on the ODD periods: the parity of each sample's period is
        ``floor(unwrapped phase)`` plus a carried parity (the integer part
        is lost when the phase wraps at the block end, so the parity is
        state of its own, reset to 0 with every restart edge -- the pulse
        on a reset sample is an even one), and on an odd period the gate
        is ``swing <= frac < swing + pw`` instead of ``frac < pw``. Even
        periods use the very same ``frac`` values as the straight clock,
        so the even pulses are bit-identical to it, sample for sample; a
        fraction of the phase is a fraction of the CURRENT period, so
        ``bpm_cv`` swings with the tempo for free. The ceiling: a sample
        is the last of its period when the next one's ``frac`` wraps
        (the next block's first sample is ``mod(end_phase + inc, 1)``,
        computed here exactly as that block will), and an odd pulse is
        forced low there -- cut one sample before the next even edge --
        so a late pulse can never swallow the downbeat; past ``swing + pw
        = 1`` the odd pulse is shorter than the even one, never longer.
        Held samples (``run`` low) repeat the previous ``frac``, so they
        never read as a wrap and the parity freezes with the phase.

        ``swing_cv`` + ``swing_cv_depth`` (love pass, 2026-09-22): the
        effective swing is ``clamp(swing + depth * mean cv, 0,
        _CLOCK_MAX_SWING)``, the mean taken in float64 (a float32 mean
        of a CONSTANT cv is block-size sensitive at the ulp -- the
        resonance_cv finding) and a non-finite mean read as 0. The
        swing is the phase offset the ODD periods are read at, so a
        value that changed mid-period would move a pulse's rising edge
        -- forward into a second edge, or past the period's end into no
        edge at all. So it is LATCHED: the block's value takes effect at
        the first EVEN period start in the block and is held from there,
        and the value in force before that start carries in state
        (``swing_held``). An even period and the odd period after it
        therefore always share one swing value -- the beat and its
        offbeat -- and the odd pulse in flight keeps the value its
        period began with. One rising edge per period, whatever the CV
        does (pinned: a 0.5 Hz LFO on ``swing_cv`` over 10 s emits
        exactly the straight clock's edge count). A restart edge is an
        even period start, so a reset latches the current value too.

        Bit-exactness at default is by construction: with no edges the
        block is one segment, ``np.mod(phase0 + inc * arange(1, F + 1),
        1)`` and ``np.mod(phase0 + inc * F, 1)`` -- the pre-love-pass
        code, operation for operation. A ``run`` cable that is high from
        sample 0 is one edge there, and phase 0 from sample 0 IS the fresh
        clock, so that too is bit-exact (both pinned in tests/test_clock.py).
        At ``swing`` 0 with ``swing_cv`` unpatched the odd-period branch
        is never built and the gate is the same ``frac < pw`` -- the
        parity bookkeeping is one floor per segment on the way to state
        -- so the recipe's renders are unchanged (pinned); with a
        non-zero ``swing`` and no cable the latched value is that same
        constant on every sample, so the comparison is the old scalar's.
        """
        bpm = max(1e-6, float(module.params.get("bpm", 120.0)))
        division = max(1e-6, float(module.params.get("division", 4.0)))
        pw = min(0.999, max(0.001, float(module.params.get("pulse_width", 0.5))))
        try:
            swing = float(module.params.get("swing", 0.0))
        except (TypeError, ValueError):
            swing = 0.0
        swing = min(self._CLOCK_MAX_SWING, max(0.0, swing))
        if not np.isfinite(swing):
            swing = 0.0

        reset = run = bpm_cv = swing_cv = None
        if buffers is not None and patch is not None:
            reset = self._input_buffer(patch, buffers, module.id, "reset")
            run = self._input_buffer(patch, buffers, module.id, "run")
            # Uncollapsed so a (V, F) CV is averaged, not summed V times.
            bpm_cv = self._input_buffer(
                patch, buffers, module.id, "bpm_cv", collapse=False
            )
            swing_cv = self._input_buffer(
                patch, buffers, module.id, "swing_cv", collapse=False
            )

        if swing_cv is not None and swing_cv.size > 0:
            try:
                s_depth = float(module.params.get("swing_cv_depth", 0.5))
            except (TypeError, ValueError):
                s_depth = 0.5
            if not np.isfinite(s_depth):
                s_depth = 0.0
            # float64 again: a float32 mean of a constant CV moves at the
            # ulp with the block size, and this one sets an EDGE.
            shift = s_depth * float(np.mean(np.asarray(swing_cv, dtype=np.float64)))
            if not np.isfinite(shift):
                shift = 0.0
            swing = min(self._CLOCK_MAX_SWING, max(0.0, swing + shift))

        if bpm_cv is not None and bpm_cv.size > 0:
            depth = float(module.params.get("bpm_cv_depth", 1.0))
            # float64 on purpose (the resonance_cv lesson): a float32
            # mean times a Python float stays float32 under numpy's
            # weak-scalar rule and lands the tempo an ulp off.
            expo = depth * float(np.mean(np.asarray(bpm_cv, dtype=np.float64)))
            if not np.isfinite(expo):
                expo = 0.0
            expo = max(-self._BPM_CV_EXP_LIMIT, min(self._BPM_CV_EXP_LIMIT, expo))
            bpm = bpm * 2.0 ** expo

        freq = bpm / 60.0 * division  # pulses per second
        inc = freq / self.sample_rate

        st = self._state.setdefault(module.id, {"phase": 0.0})
        phase0 = float(st.get("phase", 0.0))
        parity0 = int(st.get("parity", 0)) & 1
        # The swing in force at the block's first sample. A fresh clock
        # has no history, so it starts already holding this block's
        # value (nothing to interpolate from, and the very first period
        # is an even one anyway).
        held0 = float(st.get("swing_held", swing))

        # Restart edges this block: every reset edge, plus every run rise.
        # ``_lfo_reset_edges`` is the generic carried-across-blocks
        # rising-edge finder; ``running`` is the per-sample hold mask
        # (None = unpatched = always running).
        edges = np.zeros(0, dtype=np.intp)
        running = None
        if reset is not None:
            edges, st["reset_prev"] = self._lfo_reset_edges(
                reset, st.get("reset_prev", False), frames
            )
        if run is not None:
            running = np.asarray(run[:frames]) > self._GATE_HIGH
            run_edges, st["run_prev"] = self._lfo_reset_edges(
                run, st.get("run_prev", False), frames
            )
            if run_edges.size:
                edges = np.union1d(edges, run_edges).astype(np.intp)

        # One vectorized ramp per segment. ``cnt`` is how many samples the
        # phase has advanced at each sample of the segment, counting the
        # sample itself: 1..n free-running, or the running count under a
        # ``run`` gate (a held sample repeats the previous count, so the
        # phase freezes). cumsum of a 0/1 mask is exact, so a run cable
        # that never falls yields the very same ramp as no cable.
        ramp = np.arange(1, frames + 1, dtype=np.float64)
        frac = np.empty(frames, dtype=np.float64)
        swung = swing > 0.0 or held0 > 0.0
        if swung:
            odd = np.zeros(frames, dtype=bool)   # sample sits in an odd period
            last = np.zeros(frames, dtype=bool)  # sample is the last of its period
            # Where an EVEN period begins: the latch points for the
            # block's swing value.
            even_start = np.zeros(frames, dtype=bool)
        bounds = [0, *(int(e) for e in edges), frames]
        seg_start = phase0
        parity = parity0
        end_phase = phase0
        end_parity = parity0
        for seg_i, (a, b) in enumerate(zip(bounds[:-1], bounds[1:])):
            n = b - a
            if running is None:
                cnt = ramp[:n]
                adv = n
            else:
                cnt = np.cumsum(running[a:b], dtype=np.float64)
                adv = float(cnt[-1]) if n else 0.0
            unwrapped = seg_start + inc * cnt
            frac[a:b] = np.mod(unwrapped, 1.0)
            end_unwrapped = seg_start + inc * adv
            end_phase = np.mod(end_unwrapped, 1.0)
            # The period's parity: laps completed since the segment
            # start, plus the parity the segment started in. ``frac`` is
            # ``unwrapped - floor(unwrapped)`` exactly, so this floor and
            # that mod agree on which side of the crossing a sample sits.
            end_parity = (parity + int(np.floor(end_unwrapped))) & 1
            if swung and n:
                lap = np.floor(unwrapped).astype(np.int64)
                odd[a:b] = ((lap + parity) & 1).astype(bool)
                # Last-of-period: the next sample's frac wraps below this
                # one's. For the segment's final sample that next frac is
                # what the following block computes first (``end_phase +
                # inc * 1``), evaluated here the same way. A held sample
                # repeats its predecessor's frac, so it is never a wrap.
                nxt = np.mod(end_phase + inc, 1.0)
                last[a:b - 1] = frac[a + 1:b] < frac[a:b - 1]
                last[b - 1] = nxt < frac[b - 1]
                # Period starts: a lap the previous sample had not yet
                # completed. The segment's own first sample starts one
                # when the lap is already past 0 (the phase wrapped
                # right at the seam) -- or, in a restart segment, by
                # decree: a reset IS a fresh even period.
                starts = np.empty(n, dtype=bool)
                starts[0] = True if seg_i else bool(lap[0] > 0)
                if n > 1:
                    starts[1:] = lap[1:] != lap[:-1]
                even_start[a:b] = starts & ~odd[a:b]
            # Every segment after the first begins at a restart edge: a
            # fresh clock, phase 0, and the pulse there is an even one.
            seg_start = 0.0
            parity = 0

        gate = frac < pw
        held_end = swing
        if swung:
            # The latch: this block's swing takes effect at the first
            # EVEN period start; before it, the value carried in state.
            # Constant when nothing moves (no cable, or a steady CV), so
            # the comparison below is the old scalar's, value for value.
            hit = np.flatnonzero(even_start)
            lat = np.empty(frames, dtype=np.float64)
            if hit.size:
                lat[:hit[0]] = held0
                lat[hit[0]:] = swing
            else:
                lat[:] = held0
                held_end = held0
            # Odd periods: the same pulse, ``swing`` of a period later,
            # cut one sample before the next even edge (the ceiling).
            late = (frac >= lat) & (frac < lat + pw) & ~last
            gate = np.where(odd, late, gate)
        if running is not None:
            gate &= running
        st["phase"] = float(end_phase)
        st["parity"] = int(end_parity)
        st["swing_held"] = float(held_end)
        return gate.astype(np.float32)

    def _render_sequencer(self, module, frames: int, buffers, patch) -> dict:
        """Clock-driven step sequencer → 1V/oct ``cv`` + ``gate``.

        Advances one step per rising edge of the ``clock`` gate; a rising
        edge on ``reset`` rewinds so the next clock plays the pattern's
        start. The step index starts at -1 (*before the start*) so the
        first clock pulse lands on step 1 (index 0) in ``forward`` and
        ``pendulum``, and on the LAST step in ``backward``; which index
        follows which is the one pure rule ``next_step_index`` in
        modules/sequencer.py (forward wraps modulo ``steps``, backward
        counts down, pendulum bounces without repeating the turnaround
        steps, random draws). ``cv`` holds the current step's pitch
        (``semitones / 12``) for the whole step — sample-and-hold, so the
        note stays in tune while an envelope rings out after the gate
        falls. ``gate`` is high while the clock is high *and* the current
        step is enabled (a disabled step is a rest). Mono output.

        ``reverse`` is a gate read ON the clock edge sample and handed to
        the rule as a per-edge flag: high = take this step in the
        reversed direction (forward as backward, backward as forward,
        pendulum turning around; random unaffected). It is not state —
        the stored pendulum heading stays in the base frame (the rule
        XORs the gate in and out), so the gate can rise or fall between
        any two edges and the pattern carries on from the current step
        the other way. Read only at edges, so block-size independent for
        the same reason the random stream is; a ``(V, F)`` source on it
        collapses to any-voice-high via ``_input_buffer``'s sum, like
        ``clock`` and ``reset``. Unpatched, the flag is False on every
        edge and the render is bit-exact with the pre-reverse engine.

        ``random`` draws one ``integers(steps)`` per clock edge from a
        Generator seeded with ``seed``. The Generator lives in the module
        state and is built lazily on the first draw, rebuilt when ``seed``
        changes, and DROPPED on a reset edge — so a reset replays the same
        phrase from the top (a reproducible "random" line the rest of the
        patch can lean on). Draws happen only at edges, so the stream is
        block-size independent by construction. A ``forward`` render is
        bit-exact with the pre-direction engine (the recipe's reference
        renders are pinned in tests/test_sequencer.py).

        Per-sample because it is an edge-driven counter; cheap (one int
        compare + a couple of lookups per sample) and clear, matching the
        ADSR/Schmitt style in this backend.
        """
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        reset = self._input_buffer(patch, buffers, module.id, "reset")
        reverse = self._input_buffer(patch, buffers, module.id, "reverse")

        steps = int(module.params.get("steps", 8))
        steps = max(1, min(self._SEQ_MAX_STEPS, steps))
        pitches = [
            float(module.params.get(f"step{i}_pitch", 0.0))
            for i in range(1, self._SEQ_MAX_STEPS + 1)
        ]
        ons = [
            bool(module.params.get(f"step{i}_on", True))
            for i in range(1, self._SEQ_MAX_STEPS + 1)
        ]
        direction = str(module.params.get("direction", "forward"))
        if direction not in _SEQ_DIRECTIONS:
            direction = "forward"
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)

        st = self._state.setdefault(
            module.id,
            {"idx": -1, "cv": 0.0, "prev_clock": False, "prev_reset": False,
             "asc": True, "rng": None, "seed": seed},
        )
        idx = int(st["idx"])
        cur_cv = float(st["cv"])
        prev_clock = bool(st["prev_clock"])
        prev_reset = bool(st["prev_reset"])
        asc = bool(st.get("asc", True))
        rng = st.get("rng")
        if st.get("seed") != seed:
            # A live seed change re-rolls the stream on the spot; the
            # position is kept (only the dice are new).
            rng = None
            st["seed"] = seed

        gate_high = self._GATE_HIGH
        cv_out = np.empty(frames, dtype=np.float32)
        gate_out = np.empty(frames, dtype=np.float32)

        for n in range(frames):
            c = bool(clock[n] > gate_high) if clock is not None else False
            r = bool(reset[n] > gate_high) if reset is not None else False

            if r and not prev_reset:
                # Back to the pattern's start for this direction (the
                # rule reads -1 as "before the start"), heading up, and
                # the random stream restarts so the same phrase replays.
                idx = -1
                asc = True
                rng = None
            prev_reset = r

            if c and not prev_clock:
                if direction == "random" and rng is None and steps > 1:
                    rng = np.random.default_rng(seed)
                rv = bool(reverse[n] > gate_high) if reverse is not None else False
                idx, asc = _seq_next_step_index(idx, steps, direction, asc, rng, rv)
                cur_cv = pitches[idx] / 12.0
            prev_clock = c

            cv_out[n] = cur_cv
            gate_out[n] = 1.0 if (c and idx >= 0 and ons[idx]) else 0.0

        st["idx"] = idx
        st["cv"] = cur_cv
        st["prev_clock"] = prev_clock
        st["prev_reset"] = prev_reset
        st["asc"] = asc
        st["rng"] = rng
        return {"cv": cv_out, "gate": gate_out}

    def _render_possibility_seq(self, module, frames: int, buffers, patch) -> dict:
        """Clock-driven sequencer with undecided steps (see
        modules/possibility_seq.py — PythonBinaryPossibility's step
        semantics, ported).

        Per-sample edge loop, the sequencer precedent: three gate inputs'
        edges must interleave in sample order (a reroll landing mid-bar
        redraws the take for the steps still to come). Step advance and the
        gate contract match the Sequencer exactly: idx starts at -1 so the
        first clock plays step 1, and ``gate`` is high while the clock is
        high and the current step fires.

        The take model: decided steps ("0"/"1") are read live from params
        every step, so panel edits land immediately. Undecided steps are
        rolled through ``resolve_step`` — in ``latch``/``loop`` the roll is
        memoized per step (one take), the memo clearing on a loop wrap
        (``loop``), a ``reroll`` edge, or a ``seed`` change; in ``dice``
        nothing is memoized. The balanced bag (counts of rests/hits dealt)
        lives alongside the memo and clears with it, so a ``loop``/``latch``
        bar is dealt exactly like the reference ``collapse_pattern`` — in
        ``dice`` the bag persists across ticks instead, dealing fair steps
        evenly through *time* (consecutive fair rolls pair up).

        Determinism: one ``default_rng(seed)`` consumed only when a real
        choice exists, so the sequence of takes is a pure function of the
        seed and the edge history — block-size independent by construction.
        """
        from ...modules.possibility_seq import (
            MAX_STEPS as _PSEQ_MAX,
        )
        from ...modules.possibility_seq import (
            POSSIBILITY_MODES,
            STEP_STATES,
            resolve_step,
        )

        clock = self._input_buffer(patch, buffers, module.id, "clock")
        reset = self._input_buffer(patch, buffers, module.id, "reset")
        reroll = self._input_buffer(patch, buffers, module.id, "reroll")

        try:
            steps = int(module.params.get("steps", 16))
        except (TypeError, ValueError):
            steps = 16
        steps = max(1, min(_PSEQ_MAX, steps))
        mode = str(module.params.get("mode", "loop"))
        if mode not in POSSIBILITY_MODES:
            mode = "loop"
        balanced = bool(module.params.get("balanced", False))
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)

        states = []
        odds = []
        for i in range(1, _PSEQ_MAX + 1):
            s = str(module.params.get(f"step{i}_state", "0"))
            states.append(s if s in STEP_STATES else "0")
            try:
                p = float(module.params.get(f"step{i}_p", 0.5))
            except (TypeError, ValueError):
                p = 0.5
            odds.append(min(1.0, max(0.0, p)))

        st = self._state.setdefault(module.id, {})
        if st.get("seed") != seed:
            st["seed"] = seed
            st["rng"] = np.random.default_rng(seed)
            st["memo"] = {}
            st["bag"] = [0, 0]
            st["idx"] = -1
            st["fires"] = False
            st["prev_clock"] = False
            st["prev_reset"] = False
            st["prev_reroll"] = False
        rng = st["rng"]
        memo: dict = st["memo"]
        bag: list = st["bag"]
        idx = int(st["idx"])
        fires = bool(st["fires"])
        prev_clock = bool(st["prev_clock"])
        prev_reset = bool(st["prev_reset"])
        prev_reroll = bool(st["prev_reroll"])

        gate_high = self._GATE_HIGH
        gate_out = np.zeros(frames, dtype=np.float32)

        for n in range(frames):
            c = bool(clock[n] > gate_high) if clock is not None else False
            r = bool(reset[n] > gate_high) if reset is not None else False
            rr = bool(reroll[n] > gate_high) if reroll is not None else False

            if r and not prev_reset:
                idx = -1  # rewind; the take is kept
            prev_reset = r

            if rr and not prev_reroll:
                memo.clear()  # a fresh take for every ? still to come
                bag[0] = bag[1] = 0
            prev_reroll = rr

            if c and not prev_clock:
                nxt = (idx + 1) % steps
                if mode == "loop" and nxt == 0 and idx != -1:
                    memo.clear()  # the pattern wrapped: a fresh take
                    bag[0] = bag[1] = 0
                idx = nxt
                state = states[idx]
                if state != "?":
                    fires = state == "1"  # decided steps read live
                elif mode == "dice":
                    fires = resolve_step(state, odds[idx], balanced, bag,
                                         rng.random)
                elif idx in memo:
                    fires = bool(memo[idx])
                else:
                    fires = resolve_step(state, odds[idx], balanced, bag,
                                         rng.random)
                    memo[idx] = fires
            prev_clock = c

            if c and idx >= 0 and fires:
                gate_out[n] = 1.0

        st["idx"] = idx
        st["fires"] = fires
        st["prev_clock"] = prev_clock
        st["prev_reset"] = prev_reset
        st["prev_reroll"] = prev_reroll
        return {"gate": gate_out}

    def _render_possibility_selector(self, module, frames: int, buffers, patch) -> dict:
        """Clocked 1-to-4 gate router with undecided routes (see
        modules/possibility_selector.py -- the possibility register one
        level up: a symbol per step instead of a bit).

        Per-sample edge loop on tolist()'d rows (the clock_divider
        precedent): the stepper, reset and reroll edges must interleave in
        sample order. Two stepping contracts: with ``clock`` patched it is
        the stepper and ``in`` is the routed signal; with ``clock``
        unpatched ``in`` is both (route k applies to the k-th hit). With
        ``in`` unpatched the clock itself is routed. Step advance matches
        the sequencer: idx starts at -1 so the first edge plays step 1;
        ``out{k}`` is high while the signal is high and the current step
        routes to k (nothing passes before the first step).

        The take model mirrors possibility_seq: rests and decided steps are
        read live from params every step (panel edits land immediately);
        open steps go through ``resolve_route`` -- memoized per step in
        ``loop``/``latch`` (one take), the memo clearing on a loop wrap
        (``loop``), a ``reroll`` edge, or a ``seed`` change; in ``dice``
        nothing is memoized. The balanced bag (deal count per output)
        lives beside the memo and clears with it, so a ``loop``/``latch``
        bar is dealt exactly like the reference ``collapse_routes``; in
        ``dice`` it persists and deals evenly through time.

        Determinism: one ``default_rng(seed)`` consumed only on a real
        choice -- the sequence of takes is a pure function of the seed and
        the edge history, block-size independent by construction.
        """
        from ...modules.possibility_selector import (
            MAX_STEPS as _PSEL_MAX,
        )
        from ...modules.possibility_selector import (
            N_OUTS as _PSEL_OUTS,
        )
        from ...modules.possibility_selector import (
            POSSIBILITY_MODES,
            parse_route,
            resolve_route,
        )

        sig_in = self._input_buffer(patch, buffers, module.id, "in")
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        reset = self._input_buffer(patch, buffers, module.id, "reset")
        reroll = self._input_buffer(patch, buffers, module.id, "reroll")
        stepper = clock if clock is not None else sig_in
        sig = sig_in if sig_in is not None else clock

        try:
            steps = int(module.params.get("steps", 16))
        except (TypeError, ValueError):
            steps = 16
        steps = max(1, min(_PSEL_MAX, steps))
        mode = str(module.params.get("mode", "loop"))
        if mode not in POSSIBILITY_MODES:
            mode = "loop"
        balanced = bool(module.params.get("balanced", False))
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)
        weights = []
        for k in range(1, _PSEL_OUTS + 1):
            try:
                w = float(module.params.get(f"weight{k}", 1.0))
            except (TypeError, ValueError):
                w = 1.0
            weights.append(min(1.0, max(0.0, w)))
        states = [str(module.params.get(f"step{i}_state", "0"))
                  for i in range(1, _PSEL_MAX + 1)]
        cands = [parse_route(s) for s in states]

        st = self._state.setdefault(module.id, {})
        if st.get("seed") != seed:
            st["seed"] = seed
            st["rng"] = np.random.default_rng(seed)
            st["memo"] = {}
            st["bag"] = [0] * _PSEL_OUTS
            st["idx"] = -1
            st["route"] = 0
            st["prev_step"] = False
            st["prev_reset"] = False
            st["prev_reroll"] = False
        rng = st["rng"]
        rand = rng.random
        memo: dict = st["memo"]
        bag: list = st["bag"]
        idx = int(st["idx"])
        route = int(st["route"])
        prev_step = bool(st["prev_step"])
        prev_reset = bool(st["prev_reset"])
        prev_reroll = bool(st["prev_reroll"])

        gh = self._GATE_HIGH
        c_row = (stepper > gh).tolist() if stepper is not None else [False] * frames
        r_row = (reset > gh).tolist() if reset is not None else [False] * frames
        rr_row = (reroll > gh).tolist() if reroll is not None else [False] * frames
        active = [0] * frames  # the output each sample would pass to (0 = none)

        for n in range(frames):
            r = r_row[n]
            if r and not prev_reset:
                idx = -1  # rewind; the take is kept
            prev_reset = r

            rr = rr_row[n]
            if rr and not prev_reroll:
                memo.clear()  # a fresh take for every open step still to come
                for k in range(_PSEL_OUTS):
                    bag[k] = 0
            prev_reroll = rr

            c = c_row[n]
            if c and not prev_step:
                nxt = (idx + 1) % steps
                if mode == "loop" and nxt == 0 and idx != -1:
                    memo.clear()  # the pattern wrapped: a fresh take
                    for k in range(_PSEL_OUTS):
                        bag[k] = 0
                idx = nxt
                cs = cands[idx]
                if len(cs) <= 1:
                    route = cs[0] if cs else 0  # decided steps read live
                elif mode == "dice":
                    route = resolve_route(states[idx], weights, balanced, bag, rand)
                elif idx in memo:
                    route = int(memo[idx])
                else:
                    route = resolve_route(states[idx], weights, balanced, bag, rand)
                    memo[idx] = route
            prev_step = c

            if idx >= 0:
                active[n] = route

        st["idx"] = idx
        st["route"] = route
        st["prev_step"] = prev_step
        st["prev_reset"] = prev_reset
        st["prev_reroll"] = prev_reroll

        out = {}
        if sig is None:
            for k in range(1, _PSEL_OUTS + 1):
                out[f"out{k}"] = np.zeros(frames, dtype=np.float32)
            return out
        high = sig > gh
        act = np.asarray(active, dtype=np.int8)
        for k in range(1, _PSEL_OUTS + 1):
            out[f"out{k}"] = ((act == k) & high).astype(np.float32)
        return out

    # ----- Quantizer rendering --------------------------------------------

    # ``changed`` trigger length — mirrors the key_trigger pulse feel.
    _QUANTIZER_PULSE_SECONDS = 0.005

    def _render_quantizer(self, module, frames: int, buffers, patch) -> dict:
        """Snap a pitch CV to the nearest allowed note (see modules/quantizer.py).

        The CV is 1 V/oct semitone space (× 12), quantized against a table
        of allowed notes across ±5 octaves built from ``root`` + ``scale``
        (or the custom tickboxes; an empty custom set falls back to
        chromatic). ``transpose`` is added AFTER quantization.

        Modes:
          * continuous (``gate`` unpatched) — every sample quantizes.
            ``hysteresis`` (cents) makes the held note sticky: a new note
            wins only once the input is more than the margin closer to it
            than to the held one. Fast paths keep the common cases
            vectorized (hysteresis 0 → pure searchsorted; a block whose
            stateless nearest never leaves the held note → constant); only
            a block that actually crosses a boundary with hysteresis on
            pays the pure-Python scalar scan (the slew lesson: scalars,
            not per-sample numpy).
          * gated (``gate`` patched) — sample-and-quantize on rising edges
            only (no hysteresis; edges are discrete events), holding in
            between. The gate is read mono and applies to every voice.

        ``changed`` emits a ~5 ms trigger per held-note change, per voice,
        carried across blocks (block-size independent). The held note is
        primed to the first input sample so a fresh patch doesn't fire a
        spurious trigger. Shape-polymorphic: mono in → mono outs, (V, F)
        in → (V, F) outs with per-voice held/pulse state.
        """
        cv_in = self._input_buffer(
            patch, buffers, module.id, "in", collapse=False
        )
        if cv_in is None:
            self._state.pop(module.id, None)
            zeros = np.zeros(frames, dtype=np.float32)
            return {"out": zeros, "changed": zeros}
        gate_in = self._input_buffer(patch, buffers, module.id, "gate")

        voiced = cv_in.ndim == 2
        x = (cv_in if voiced else cv_in[None, :]).astype(np.float64) * 12.0
        V = x.shape[0]

        # --- allowed-note table (rebuilt per block; 121 entries max) ---
        scale = str(module.params.get("scale", "major"))
        if scale == "custom":
            pcs = [
                i for i, key in enumerate(_Q_CUSTOM_KEYS)
                if bool(module.params.get(key, True))
            ]
            if not pcs:  # all unticked: chromatic fallback, never wedged
                pcs = list(range(12))
        else:
            pcs = list(_Q_SCALES.get(scale, _Q_SCALES["chromatic"]))
        root_name = str(module.params.get("root", "C"))
        root = _Q_ROOTS.index(root_name) if root_name in _Q_ROOTS else 0
        allowed = sorted({(pc + root) % 12 for pc in pcs})
        table = np.array(
            [n for n in range(-60, 61) if n % 12 in allowed], dtype=np.float64
        )

        try:
            hyst = float(module.params.get("hysteresis", 10.0))
        except (TypeError, ValueError):
            hyst = 10.0
        hyst_st = min(50.0, max(0.0, hyst)) / 100.0  # cents → semitones
        try:
            transpose = float(module.params.get("transpose", 0.0))
        except (TypeError, ValueError):
            transpose = 0.0

        def nearest(vals: np.ndarray) -> np.ndarray:
            """Nearest allowed note per element (ties round down)."""
            idx = np.clip(np.searchsorted(table, vals), 1, len(table) - 1)
            left = table[idx - 1]
            right = table[idx]
            return np.where(vals - left <= right - vals, left, right)

        # --- per-voice state: held note (st), pulse samples remaining ---
        st = self._state.setdefault(module.id, {})
        held = st.get("held")
        pulse = st.get("pulse")
        if held is None or held.shape[0] != V:
            held = nearest(x[:, 0]).astype(np.float64)  # primed: no trigger
            pulse = np.zeros(V, dtype=np.int64)
        else:
            held = held.astype(np.float64, copy=True)
            pulse = pulse.copy()
        plen = max(1, int(round(self.sample_rate * self._QUANTIZER_PULSE_SECONDS)))

        held_series = np.empty((V, frames), dtype=np.float64)
        changed = np.zeros((V, frames), dtype=np.float32)
        # events[v] = sample indices where the held note switched this block
        events: list[list[int]] = [[] for _ in range(V)]

        if gate_in is not None:
            # Gated: quantize only on rising edges, mono gate for all voices.
            g = gate_in > self._GATE_HIGH
            prev_arr = np.empty_like(g)
            prev_arr[0] = bool(st.get("prev_gate", False))
            prev_arr[1:] = g[:-1]
            edge_idx = np.flatnonzero(g & ~prev_arr)
            st["prev_gate"] = bool(g[-1])
            for v in range(V):
                pos = 0
                h = held[v]
                for e in edge_idx:
                    held_series[v, pos:e] = h
                    cand = float(nearest(x[v, e : e + 1])[0])
                    if cand != h:
                        h = cand
                        events[v].append(int(e))
                    pos = int(e)
                held_series[v, pos:] = h
                held[v] = h
        elif hyst_st <= 0.0:
            # Continuous, no hysteresis: pure vectorized nearest.
            cand = nearest(x)
            held_series[:] = cand
            for v in range(V):
                row = cand[v]
                if row[0] != held[v]:
                    events[v].append(0)
                steps = np.flatnonzero(row[1:] != row[:-1])
                events[v].extend((steps + 1).tolist())
                held[v] = row[-1]
        else:
            # Continuous with hysteresis. Fast path: a block whose
            # stateless nearest never leaves the held note can't switch.
            cand = nearest(x)
            for v in range(V):
                if np.all(cand[v] == held[v]):
                    held_series[v] = held[v]
                    continue
                # Pure-Python scalars on tolist()'d rows (the slew lesson:
                # per-sample numpy indexing is dispatch overhead, not math).
                xv = x[v].tolist()
                cv_row = cand[v].tolist()
                h = float(held[v])
                row = [0.0] * frames
                for n in range(frames):
                    c = cv_row[n]
                    if c != h and (abs(xv[n] - h) - abs(xv[n] - c)) > hyst_st:
                        h = c
                        events[v].append(n)
                    row[n] = h
                held_series[v] = row
                held[v] = h

        # --- paint the ~5 ms `changed` pulses (carried across blocks) ---
        for v in range(V):
            n = min(int(pulse[v]), frames)  # tail of a previous block's pulse
            if n > 0:
                changed[v, :n] = 1.0
                pulse[v] -= n
            for e in events[v]:
                n = min(plen, frames - e)
                changed[v, e : e + n] = 1.0
                pulse[v] = plen - n  # a later event re-arms the carry

        st["held"] = held.copy()
        st["pulse"] = pulse

        out = (held_series + transpose) / 12.0
        if not voiced:
            return {
                "out": out[0].astype(np.float32),
                "changed": changed[0],
            }
        return {"out": out.astype(np.float32), "changed": changed}

    # ----- Arpeggiator / Chord (the voice-architecture pair) ---------------

    def _render_arpeggiator(self, module, frames: int, buffers, patch) -> dict:
        """Poly→mono clocked note collapser (see modules/arpeggiator.py).

        Voice-gate edges are vectorized into a sparse per-sample event
        map (a rise carries the pitch read at that sample), so the
        frames loop below walks scalars only (euclidean precedent) —
        no 16×F per-sample voice scan. The held set is keyed by voice
        slot with an arrival stamp, making ``order`` true as-played
        order even when the allocator reuses slots; falls at a sample
        are applied before rises so a press from silence under ``hold``
        clears the latch first (the classic performance latch). The
        sequence is rebuilt from the held set at each clock edge
        (mid-arp joins/leaves take effect on the next step, position
        kept by wrapped index); ``updown`` plays the palindrome with
        endpoints unrepeated. The output gate runs ``gate_len`` of the
        measured clock period (last two edges, carried across blocks;
        until an interval exists it mirrors the clock high — the
        euclidean convention). Pitch holds between steps and after the
        last release so downstream release tails stay in tune.
        ``random`` draws one seeded rng value per step (block-size
        independent; changing ``seed`` re-rolls live).

        Internal clock (2026-09-14): with ``clock`` UNPATCHED the arp
        free-runs at ``bpm`` x ``division`` steps per minute -- an
        integer-period counter (the fgen lesson; never an accumulated
        float step), half-period high so the first step's gate mirror
        has a width. A ``reset`` rise re-phases it so the very next
        sample is a step. A patched ``clock`` ignores both params, so
        every existing patch is untouched.
        """
        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        gates = self._input_buffer(
            patch, buffers, module.id, "gate", collapse=False
        )
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        reset = self._input_buffer(patch, buffers, module.id, "reset")

        mode = str(module.params.get("mode", "up"))
        try:
            bpm = float(module.params.get("bpm", 120.0))
        except (TypeError, ValueError):
            bpm = 120.0
        try:
            division = float(module.params.get("division", 4.0))
        except (TypeError, ValueError):
            division = 4.0
        bpm = min(300.0, max(20.0, bpm))
        division = min(16.0, max(0.25, division))
        octaves = max(1, min(4, int(module.params.get("octaves", 1))))
        try:
            gate_len = float(module.params.get("gate_len", 0.5))
        except (TypeError, ValueError):
            gate_len = 0.5
        gate_len = min(0.95, max(0.05, gate_len))
        hold = bool(module.params.get("hold", False))
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)

        st = self._state.setdefault(
            module.id,
            {
                "held": {},  # voice slot -> (pitch cv, arrival stamp)
                "stamp": 0, "pos": -1, "cur_cv": 0.0,
                "prev_gates": None, "prev_clock": False, "prev_reset": False,
                "last_edge": -1, "interval": 0, "samples": 0,
                "gate_rem": 0, "gate_mirror": False,
                "int_phase": 0,  # internal clock: samples since its last edge
            },
        )
        if st.get("seed") != seed:
            st["seed"] = seed
            st["rng"] = np.random.default_rng(seed)
        rng = st["rng"]
        held: dict[int, tuple[float, int]] = st["held"]
        stamp = int(st["stamp"])
        pos = int(st["pos"])
        cur_cv = float(st["cur_cv"])
        prev_c = bool(st["prev_clock"])
        prev_r = bool(st["prev_reset"])
        last_edge = int(st["last_edge"])
        interval = int(st["interval"])
        base = int(st["samples"])
        gate_rem = int(st["gate_rem"])
        gate_mirror = bool(st["gate_mirror"])

        # --- voice gate edges → sparse event map (falls, rises) -----------
        events: dict[int, tuple[list[int], list[tuple[int, float]]]] = {}
        if gates is None:
            # Unpatched gate: nothing is ever held (and a live unpatch
            # drops the latch — there is no source to hold from).
            held.clear()
            pos = -1
            st["prev_gates"] = None
            phys_n = 0
        else:
            g2 = (gates if gates.ndim == 2 else gates[None, :]) > self._GATE_HIGH
            V = g2.shape[0]
            prev_g = st["prev_gates"]
            if prev_g is None or prev_g.shape[0] != V:
                prev_g = np.zeros(V, dtype=bool)
            if not hold:
                # A live hold→off toggle drops latched-but-released notes.
                for v in [v for v in held if v >= V or not prev_g[v]]:
                    del held[v]
                if not held:
                    pos = -1
            phys_n = int(prev_g.sum())
            shifted = np.empty_like(g2)
            shifted[:, 0] = prev_g
            shifted[:, 1:] = g2[:, :-1]
            if pitch is None:
                pitch_at = lambda v, n: 0.0  # noqa: E731 — tiny closure
            elif pitch.ndim == 2:
                p_rows = pitch.shape[0]
                pitch_at = lambda v, n: float(  # noqa: E731
                    pitch[v if v < p_rows else 0, n]
                )
            else:
                pitch_at = lambda v, n: float(pitch[n])  # noqa: E731
            for v, n in zip(*np.nonzero(~g2 & shifted)):
                events.setdefault(int(n), ([], []))[0].append(int(v))
            for v, n in zip(*np.nonzero(g2 & ~shifted)):
                events.setdefault(int(n), ([], []))[1].append(
                    (int(v), pitch_at(int(v), int(n)))
                )
            st["prev_gates"] = g2[:, -1].copy()

        def build_seq() -> list[float]:
            """Held set → the expanded note sequence for this mode."""
            items = list(held.values())
            if not items:
                return []
            if mode == "order":
                bank = [cv for cv, s in sorted(items, key=lambda t: t[1])]
            elif mode == "down":
                bank = sorted((cv for cv, _ in items), reverse=True)
            else:  # up / updown / random share the ascending bank
                bank = sorted(cv for cv, _ in items)
            octs = reversed(range(octaves)) if mode == "down" else range(octaves)
            seq = [cv + k for k in octs for cv in bank]
            if mode == "updown" and len(seq) > 2:
                seq = seq + seq[-2:0:-1]
            return seq

        thresh = self._GATE_HIGH
        r_row = (reset > thresh).tolist() if reset is not None else [False] * frames
        forced: set[int] = set()   # internal-clock steps forced by a reset
        if clock is not None:
            c_row = (clock > thresh).tolist()
        else:
            # Internal clock: an integer-period counter, high for the
            # first half of each period, re-phased to 0 at every reset
            # rise so that sample IS a step (reset is handled before the
            # clock inside the walk below, so it plays note 1).
            period = max(1, int(round(self.sample_rate * 60.0 / (bpm * division))))
            half = max(1, period // 2)
            phase = int(st.get("int_phase", 0)) % period
            c_bool = np.zeros(frames, dtype=bool)
            r_edges = [
                n for n in range(frames)
                if r_row[n] and not (r_row[n - 1] if n > 0 else prev_r)
            ]
            seg_start = 0
            for stop in r_edges + [frames]:
                if stop > seg_start:
                    idx = (phase + np.arange(stop - seg_start)) % period
                    c_bool[seg_start:stop] = idx < half
                    phase = (phase + (stop - seg_start)) % period
                if stop < frames:
                    phase = 0  # the reset sample starts a fresh period
                seg_start = stop
            st["int_phase"] = phase
            c_row = c_bool.tolist()
            # The line may already be high at the reset sample (each
            # step holds half a period), so the re-phase is a step by
            # decree, not by edge.
            forced = set(r_edges)
        p_list = [0.0] * frames
        g_list = [0.0] * frames

        for n in range(frames):
            ev = events.get(n)
            if ev is not None:
                falls, rises = ev
                for v in falls:
                    phys_n -= 1
                    if not hold:
                        held.pop(v, None)
                if rises:
                    if hold and phys_n == 0 and held:
                        held.clear()  # press from silence: new chord
                        pos = -1
                    for v, cv in rises:
                        phys_n += 1
                        held[v] = (cv, stamp)
                        stamp += 1
                if not held:
                    pos = -1  # next chord restarts from note 1
            r = r_row[n]
            if r and not prev_r:
                pos = -1
            prev_r = r
            c = c_row[n]
            if (c and not prev_c) or n in forced:
                now = base + n
                if last_edge >= 0:
                    interval = now - last_edge
                last_edge = now
                seq = build_seq()
                if seq:
                    if mode == "random":
                        cur_cv = seq[int(rng.integers(len(seq)))]
                    else:
                        pos = (pos + 1) % len(seq)
                        cur_cv = seq[pos]
                    if interval > 0:
                        gate_rem = max(1, int(round(gate_len * interval)))
                        gate_mirror = False
                    else:
                        gate_mirror = True
            prev_c = c
            if gate_mirror and not c:
                gate_mirror = False
            if gate_rem > 0:
                g_list[n] = 1.0
                gate_rem -= 1
            elif gate_mirror:
                g_list[n] = 1.0
            p_list[n] = cur_cv

        st.update(
            stamp=stamp, pos=pos, cur_cv=cur_cv,
            prev_clock=prev_c, prev_reset=prev_r,
            last_edge=last_edge, interval=interval, samples=base + frames,
            gate_rem=gate_rem, gate_mirror=gate_mirror,
        )
        return {
            "pitch_cv": np.asarray(p_list, dtype=np.float32),
            "gate": np.asarray(g_list, dtype=np.float32),
        }

    # Chord's fixed voice count: four interval slots → four rows, always
    # (disabled slots stay gate-low) so downstream per-voice state never
    # re-shapes when a slot is toggled live.
    _CHORD_ROWS = 4
    # ``changed``: a root jump this big (V) between consecutive samples
    # counts as a new chord -- half a semitone, so any sequencer /
    # quantizer step fires it and no glide ever does. Pulse ~2 ms.
    _CHORD_CHANGE_V = 1.0 / 24.0
    _CHORD_PULSE_S = 0.002

    def _render_chord(self, module, frames: int, buffers, patch) -> dict:
        """Mono→poly chord explorer (see modules/chord.py).

        Pitch rows are pure broadcast: ``in + interval`` per slot,
        every sample (glides chord along; release tails stay in tune).
        Gates are stateful only for ``strum``: an input rise schedules
        row k's onset ``k×strum`` ms out (k counting enabled rows),
        absolute-sample times carried across blocks (burst precedent);
        a fall drops every row together and cancels unfired onsets.
        Blocks with no edges and no pending onsets take the vectorized
        path — the per-sample scalar walk only runs around note events.

        2026-09-14 love pass: ``inversion`` moves the lowest enabled
        note up an octave, n times (rows keep their slot identity, so
        downstream per-voice state and the strum order are untouched);
        ``changed`` (gate out, mono) pulses ~2 ms when the sounding
        chord changes under a held gate -- the root jumping half a
        semitone or more between consecutive samples (a sequencer
        step; glides don't count) or the interval set changing
        (preset / inversion / spread / a slot edit) -- so a legato root
        walk can re-fire an envelope; ``retrig`` makes the module do
        that itself: the gate rows drop for one sample and re-strum.
        Both off, the pitch and gate rows are what they were.
        """
        from ...modules.chord import (
            CHORD_ENABLE_KEYS,
            CHORD_INTERVAL_KEYS,
            CHORD_PRESETS,
            CHORD_SPREAD_OFFSETS,
        )

        pitch = self._input_buffer(patch, buffers, module.id, "pitch_cv")
        gate = self._input_buffer(patch, buffers, module.id, "gate")
        R = self._CHORD_ROWS

        preset = str(module.params.get("preset", "major"))
        row = CHORD_PRESETS.get(preset)
        if row is None or preset == "custom":
            semis = []
            enabled = []
            for ik, ek in zip(CHORD_INTERVAL_KEYS, CHORD_ENABLE_KEYS):
                try:
                    iv = float(module.params.get(ik, 0.0))
                except (TypeError, ValueError):
                    iv = 0.0
                semis.append(min(24.0, max(-24.0, iv)))
                enabled.append(bool(module.params.get(ek, True)))
        else:
            semis = [float(iv) if iv is not None else 0.0 for iv in row]
            enabled = [iv is not None for iv in row]
        if bool(module.params.get("spread", False)):
            semis = [s + o for s, o in zip(semis, CHORD_SPREAD_OFFSETS)]
        try:
            inversion = int(module.params.get("inversion", 0))
        except (TypeError, ValueError):
            inversion = 0
        for _ in range(min(3, max(0, inversion))):
            # The lowest sounding note goes up an octave (ties: lowest
            # slot). Slot identity is kept -- only its pitch moves.
            low = None
            for k in range(R):
                if enabled[k] and (low is None or semis[k] < semis[low]):
                    low = k
            if low is None:
                break
            semis[low] += 12.0
        retrig = bool(module.params.get("retrig", False))
        try:
            strum = float(module.params.get("strum", 0.0))
        except (TypeError, ValueError):
            strum = 0.0
        strum_samps = int(round(min(200.0, max(0.0, strum)) * 1e-3 * self.sample_rate))

        # --- pitch rows: pure broadcast, stateless ------------------------
        offsets = np.asarray(semis, dtype=np.float64)[:, None] / 12.0
        if pitch is None:
            out_pitch = np.tile(offsets, (1, frames)).astype(np.float32)
        else:
            out_pitch = (
                pitch.astype(np.float64)[None, :] + offsets
            ).astype(np.float32)

        # --- gate rows: stateful only for strum ---------------------------
        st = self._state.setdefault(
            module.id,
            {"prev_gate": False, "active": [False] * R, "pending": [],
             "samples": 0, "prev_pitch": None, "prev_semis": None,
             "chg_rem": 0},
        )
        base = int(st["samples"])
        st["samples"] = base + frames
        out_gate = np.zeros((R, frames), dtype=np.float32)
        out_chg = np.zeros(frames, dtype=np.float32)

        # --- ``changed``: the sounding chord moved under a held gate ------
        # Root jumps (half a semitone or more sample-to-sample) and
        # interval-set edits (which land at sample 0 of the block), masked
        # to samples where the gate was ALREADY high -- a fresh press is
        # its own trigger. The pulse is carried across blocks.
        semis_key = tuple(semis) + tuple(enabled)
        prev_semis = st["prev_semis"]
        st["prev_semis"] = semis_key
        chg_mask = np.zeros(frames, dtype=bool)
        if pitch is not None:
            p64 = pitch.astype(np.float64)
            prev_p = st["prev_pitch"]
            lag = np.empty(frames)
            lag[0] = p64[0] if prev_p is None else prev_p
            lag[1:] = p64[:-1]
            chg_mask |= np.abs(p64 - lag) >= self._CHORD_CHANGE_V
            st["prev_pitch"] = float(p64[-1])
        else:
            st["prev_pitch"] = None
        if prev_semis is not None and semis_key != prev_semis:
            chg_mask[0] = True
        pulse = max(1, int(round(self._CHORD_PULSE_S * self.sample_rate)))
        chg_rem = int(st["chg_rem"])
        outs = {"pitch_cv": out_pitch, "gate": out_gate, "changed": out_chg}

        if gate is None:
            st["prev_gate"] = False
            st["active"] = [False] * R
            st["pending"] = []
            st["chg_rem"] = 0
            return outs

        g = gate > self._GATE_HIGH
        prev = bool(st["prev_gate"])
        g_lag = np.empty(frames, dtype=bool)
        g_lag[0] = prev
        g_lag[1:] = g[:-1]
        chg_mask &= g & g_lag
        chg_at = np.flatnonzero(chg_mask).tolist()
        if chg_at or chg_rem > 0:
            for n in range(frames):
                if chg_mask[n]:
                    chg_rem = pulse
                if chg_rem > 0:
                    out_chg[n] = 1.0
                    chg_rem -= 1
        st["chg_rem"] = chg_rem
        restrum = set(chg_at) if retrig else set()

        active: list[bool] = [a and e for a, e in zip(st["active"], enabled)]
        pending: list[list[int]] = st["pending"]  # [row, start_abs]
        rows_on = [k for k in range(R) if enabled[k]]

        changed = bool(g[0]) != prev or bool(np.any(g[1:] != g[:-1]))
        if strum_samps == 0 and not pending and not restrum:
            # No stagger: enabled rows mirror the input gate verbatim.
            g_f32 = g.astype(np.float32)
            for k in rows_on:
                out_gate[k] = g_f32
            st["prev_gate"] = bool(g[-1])
            st["active"] = [k in rows_on and bool(g[-1]) for k in range(R)]
            return outs
        if not changed and not pending and not restrum:
            # Steady block: rows hold their level.
            for k in range(R):
                if active[k]:
                    out_gate[k, :] = 1.0
            st["prev_gate"] = bool(g[-1])
            st["active"] = active
            return outs

        # Note-event block: scalar walk (rare — once per press/release).
        g_row = g.tolist()
        gl = [[0.0] * frames for _ in range(R)]
        for n in range(frames):
            gn = g_row[n]
            now = base + n
            if gn and not prev:
                pending = [
                    [k, now + i * strum_samps] for i, k in enumerate(rows_on)
                ]
            elif prev and not gn:
                active = [False] * R
                pending = []
            elif n in restrum:
                # Re-strum: every row drops for this one sample and the
                # stagger restarts from the next -- a fresh rising edge
                # for whatever the rows drive.
                active = [False] * R
                pending = [
                    [k, now + 1 + i * strum_samps] for i, k in enumerate(rows_on)
                ]
            prev = gn
            if pending:
                still = []
                for entry in pending:
                    if entry[1] <= now:
                        active[entry[0]] = True
                    else:
                        still.append(entry)
                pending = still
            for k in range(R):
                if active[k]:
                    gl[k][n] = 1.0
        for k in range(R):
            out_gate[k] = gl[k]
        st["prev_gate"] = prev
        st["active"] = active
        st["pending"] = pending
        return outs

    # ----- CV recorder (the modulation looper) --------------------------------

    _CVREC_LEN_MIN = 0.05
    _CVREC_LEN_MAX = 60.0
    #: Playback head rate in HALF-samples per sample, by ``speed``. The
    #: head is an integer in half-samples so 0.5x is a whole step too;
    #: recording always steps 2 (1x).
    _CVREC_RATE2 = {"0.5x": 1, "1x": 2, "2x": 4}
    #: ``speed_cv`` multiplies the combo's rate and is QUANTISED back to
    #: the same half-sample grid -- 1..8 half-steps per sample, i.e.
    #: 0.5x .. 4x in 0.25x steps. A fractional rate would need a float
    #: phase, and a float phase is not the same number at block 50 as
    #: at 250; an integer step is. 0 is not reachable (the stop is what
    #: ``play`` is for).
    _CVREC_RATE2_MIN = 1
    _CVREC_RATE2_MAX = 8
    #: The rate exponent is clipped to +-this before ``2 ** e`` (the
    #: house CV-overflow guard: an absurd CV must not overflow a power).
    _CVREC_SPEED_OCT_LIMIT = 4.0

    def _render_cv_recorder(self, module, frames: int, buffers, patch) -> dict:
        """Fixed-length CV looper (see modules/cv_recorder.py).

        The block is walked as segments between EVENTS -- clear edges, rec
        edges, clock ticks, reverse edges and play edges, in that priority
        at a shared sample -- and each segment is one vectorized pass over
        the loop buffer: read ``out = buf[head]``, and while recording
        write ``buf[head] = x`` (replace) or ``feedback * buf[head] + x``
        (overdub) and read the written value back.

        The head ``h`` is an integer in HALF-samples (0 <= h < 2L). The
        three speeds are then 1 / 2 / 4 half-samples per sample, reverse
        is the sign, and a segment's positions are ``h + d * arange(n)``
        -- an integer count of steps from the last snap, so every speed
        is bit-exact across block sizes (a float phase would not be). An
        odd position (0.5x only) reads the mean of the two slots it sits
        between. Recording always steps 2 (1x -- the take is real time)
        so every write position is a slot; a rec edge floors an odd head
        to its slot first. Chunks end at the wrap, so the positions
        within one write are distinct (an overdub through duplicate
        fancy indices would not accumulate). With ``play`` patched and
        low the head does not move: ``out`` is the slot under it, ``pos``
        holds, and nothing is written.

        The loop exists from the first honoured rec edge (position 0
        then, running forever until clear); its sample length is fixed at
        creation: ``round(length * sr)`` free-running, or
        ``round(length * interval)`` with a clock (period = the distance
        between its last two rising edges, the slew/euclidean idiom; if
        no period is known yet, seconds). Clocked, rec edges are PENDING
        until the next tick (quantised punch-in) and the head hard-syncs
        to 0 on every ``length``-th tick from the loop's start -- at any
        speed, in either direction, and while stopped (the transport
        wins, so a ``play`` gated from the same clock resumes at the top
        of the bar); a rec edge that would CREATE the loop waits until
        the period is known (the clock's second tick), since the buffer
        cannot be sized before that. ``clear`` wipes, rewinds, stops
        recording and drops any pending edge; the position holds at 0
        until the next rec edge. With ``in`` unpatched the ``value`` knob
        is the input, ramped linearly across the block from the previous
        block's value.

        ``speed_cv`` (block mean, float64, non-finite scrubbed to 0)
        multiplies the combo's rate by ``2 ** (depth * cv)`` -- with the
        exponent clipped +-4 and the PRODUCT quantised straight back onto
        the half-sample grid, ``floor(base2 * 2 ** e + 0.5)`` clamped to
        1..8 half-steps per sample (0.5x..4x in 0.25x steps). Quantising
        is the whole point: an integer step keeps the head an integer
        count from its last snap, which is what makes every rate
        bit-exact across block sizes. A rate change does not move the
        head (it is absolute state, not a phase x rate), so a sweeping
        CV never jumps the loop.

        ``play_mode`` ``one_shot`` reinterprets the ``play`` jack: its
        LEVEL is ignored and a rising edge sets the head to 0 (the last
        slot under reverse) and ``st["shot"]`` to ``2L`` half-samples of
        travel -- exactly one lap, whatever the rate. ``run`` shortens a
        chunk to the travel left and, when the lap ends, steps the head
        BACK one step so the held slot is the one just played (a ramp
        holds at its top, not at its foot). A sync tick still snaps the
        head to 0 and leaves the travel alone: the transport wins. A
        stopped head writes nothing, so between shots ``rec`` could
        never write -- hence at ``one_shot`` recording itself makes the
        head move (``moving = shot is not None or recording``), and a
        lap that ends while recording just clears the shot.
        """
        from ...modules.cv_recorder import (
            CV_RECORDER_MODES,
            CV_RECORDER_PLAY_MODES,
            CV_RECORDER_SPEEDS,
        )

        cv_in = self._input_buffer(patch, buffers, module.id, "in")
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        rec = self._input_buffer(patch, buffers, module.id, "rec")
        clear = self._input_buffer(patch, buffers, module.id, "clear")
        play = self._input_buffer(patch, buffers, module.id, "play")
        rev_gate = self._input_buffer(patch, buffers, module.id, "reverse")
        speed_cv = self._input_buffer(patch, buffers, module.id, "speed_cv",
                                      collapse=False)

        def fparam(name, default, lo, hi):
            try:
                x = float(module.params.get(name, default))
            except (TypeError, ValueError):
                x = default
            return min(hi, max(lo, x))

        length = fparam("length", 4.0, self._CVREC_LEN_MIN, self._CVREC_LEN_MAX)
        mode = str(module.params.get("mode", "overdub"))
        if mode not in CV_RECORDER_MODES:
            mode = "overdub"
        feedback = fparam("feedback", 1.0, 0.0, 1.0)
        value = fparam("value", 0.0, -1.0, 1.0)
        speed = str(module.params.get("speed", "1x"))
        if speed not in CV_RECORDER_SPEEDS:
            speed = "1x"
        rate2 = self._CVREC_RATE2[speed]
        if speed_cv is not None:
            # The rate jack: block mean in float64 (float32 accumulates),
            # non-finite scrubbed BEFORE the clamp (min/max would pass a
            # NaN straight through), exponent clipped, then quantised
            # back onto the half-sample grid.
            c = float(np.mean(np.asarray(speed_cv, dtype=np.float64)))
            if not np.isfinite(c):
                c = 0.0
            e = self._CVREC_SPEED_OCT_LIMIT
            e = min(e, max(-e, fparam("speed_cv_depth", 1.0, -4.0, 4.0) * c))
            rate2 = int(np.floor(rate2 * (2.0 ** e) + 0.5))
            rate2 = min(self._CVREC_RATE2_MAX, max(self._CVREC_RATE2_MIN, rate2))
        play_mode = str(module.params.get("play_mode", "gate"))
        if play_mode not in CV_RECORDER_PLAY_MODES:
            play_mode = "gate"
        one_shot = play_mode == "one_shot"
        try:
            rev_param = float(module.params.get("reverse", False)) >= 0.5
        except (TypeError, ValueError):
            rev_param = bool(module.params.get("reverse", False))
        sr = float(self.sample_rate)
        gh = self._GATE_HIGH

        st = self._state.get(module.id)
        if st is None:
            st = self._state[module.id] = {
                "buf": np.zeros(0, dtype=np.float64), "L": 0, "h": 0,
                "exists": False, "recording": False, "pending": None,
                "prev_rec": False, "prev_clear": False, "prev_clock": False,
                "last_edge": -1, "interval": 0, "n": 0, "ticks": 0,
                "prev_value": value,
                "reverse": False, "playing": True, "shot": None,
            }

        # --- the input: the cable, or the knob ramped across the block
        if cv_in is not None:
            x = cv_in.astype(np.float64)
        else:
            pv = float(st["prev_value"])
            x = pv + (value - pv) * (np.arange(1, frames + 1, dtype=np.float64) / frames)
        st["prev_value"] = value

        # --- events in this block: (sample, priority, kind)
        events = []
        if clear is not None:
            cr = clear > gh
            prev = np.concatenate(([bool(st["prev_clear"])], cr[:-1]))
            events += [(int(t), 0, "clear") for t in np.flatnonzero(cr & ~prev)]
            st["prev_clear"] = bool(cr[-1])
        if rec is not None:
            rr = rec > gh
            prev = np.concatenate(([bool(st["prev_rec"])], rr[:-1]))
            changes = np.flatnonzero(rr != prev)
            events += [(int(t), 1, "rec_on" if rr[t] else "rec_off") for t in changes]
            st["prev_rec"] = bool(rr[-1])
        clocked = clock is not None
        if clocked:
            ck = clock > gh
            prev = np.concatenate(([bool(st["prev_clock"])], ck[:-1]))
            events += [(int(t), 2, "tick") for t in np.flatnonzero(ck & ~prev)]
            st["prev_clock"] = bool(ck[-1])
        # Reverse and play are LEVELS (the checkbox OR the gate; the gate
        # or "playing"), so their events are every change from the
        # previous sample's level -- a checkbox flip lands on the block's
        # first sample, a gate edge on its own sample.
        rv = np.full(frames, rev_param, dtype=bool)
        if rev_gate is not None:
            rv |= rev_gate > gh
        prev = np.concatenate(([bool(st["reverse"])], rv[:-1]))
        events += [(int(t), 3, "rev_on" if rv[t] else "rev_off") for t in np.flatnonzero(rv != prev)]
        if play is not None:
            pl = play > gh
            prev = np.concatenate(([bool(st["playing"])], pl[:-1]))
            events += [(int(t), 4, "play_on" if pl[t] else "play_off") for t in np.flatnonzero(pl != prev)]
        elif not st["playing"]:
            events.append((0, 4, "play_on"))       # unpatched = playing
        events.sort()

        n0 = int(st["n"])
        length_ticks = max(1, int(round(length)))
        out = np.zeros(frames, dtype=np.float64)
        pos = np.zeros(frames, dtype=np.float64)

        def start_loop():
            """The first honoured rec edge: size the buffer, position 0."""
            if clocked and int(st["interval"]) > 0:
                L = int(round(length * int(st["interval"])))
            else:
                L = int(round(length * sr))
            L = max(1, L)
            st["buf"] = np.zeros(L, dtype=np.float64)
            st["L"] = L
            st["h"] = 0
            st["exists"] = True
            st["ticks"] = 0

        def apply_rec(on: bool):
            if on and not st["exists"]:
                start_loop()
            st["recording"] = bool(on) and bool(st["exists"])
            if st["recording"]:
                st["h"] = int(st["h"]) & ~1        # a rec edge lands the head on its slot

        def read(buf, L, idx):
            """The loop at half-sample positions ``idx``: a slot, or the
            mean of the two slots an odd position sits between."""
            slot = idx >> 1
            y = buf[slot]
            odd = (idx & 1).astype(bool)
            if odd.any():
                nxt = (slot[odd] + 1) % L
                y[odd] = 0.5 * (y[odd] + buf[nxt])
            return y

        def hold(a: int, b: int, buf, L: int, h: int):
            """out/pos over [a:b) from a head that is not moving."""
            out[a:b] = read(buf, L, np.array([h]))[0]
            pos[a:b] = (h * 0.5) / L

        def run(a: int, b: int):
            """Advance the loop over out[a:b] (relative samples)."""
            if b <= a or not st["exists"]:
                return
            buf = st["buf"]
            L = int(st["L"])
            L2 = 2 * L
            h = int(st["h"])
            recording = bool(st["recording"])
            shot = st["shot"]
            # At one_shot the play LEVEL is ignored: the head moves while
            # a lap is in flight, or while recording (a stopped head
            # writes nothing, so rec would otherwise never reach the tape).
            moving = (shot is not None or recording) if one_shot else bool(st["playing"])
            if not moving:
                # A stopped head: the slot under it, held; nothing written.
                hold(a, b, buf, L, h)
                return
            d = 2 if recording else rate2          # record at 1x, play at any
            if st["reverse"]:
                d = -d
            i = a
            while i < b:
                # Run to the wrap: every position in the chunk is distinct.
                if d > 0:
                    n = min(b - i, (L2 - 1 - h) // d + 1)
                else:
                    n = min(b - i, h // (-d) + 1)
                ends = False
                if shot is not None:
                    left = -(-shot // abs(d))      # samples of lap remaining
                    if left <= n:
                        n, ends = int(left), True
                idx = h + d * np.arange(n)
                if recording:
                    slot = idx >> 1
                    seg = x[i:i + n]
                    if mode == "replace":
                        buf[slot] = seg
                    else:
                        layer = buf[slot] * feedback
                        layer += seg
                        buf[slot] = layer
                    out[i:i + n] = buf[slot]
                else:
                    out[i:i + n] = read(buf, L, idx)
                pos[i:i + n] = (idx * 0.5) / L
                h = (h + d * n) % L2
                i += n
                if shot is not None:
                    shot -= abs(d) * n
                    if ends:
                        shot = None
                        if not recording:
                            # Step back onto the slot just played, so the
                            # hold is the lap's END (a ramp holds at its top).
                            h = (h - d) % L2
                            st["h"], st["shot"] = h, None
                            if i < b:
                                hold(i, b, buf, L, h)
                            return
            st["h"] = h
            st["shot"] = shot

        seg_start = 0
        for t, _prio, kind in events:
            run(seg_start, t)
            seg_start = t
            if kind == "clear":
                st["buf"] = np.zeros(0, dtype=np.float64)
                st["L"] = 0
                st["h"] = 0
                st["exists"] = False
                st["recording"] = False
                st["pending"] = None
                st["ticks"] = 0
                st["shot"] = None
            elif kind in ("rec_on", "rec_off"):
                on = kind == "rec_on"
                if clocked:
                    st["pending"] = on          # honoured on the next tick
                else:
                    apply_rec(on)
            elif kind in ("rev_on", "rev_off"):
                st["reverse"] = kind == "rev_on"     # the head turns around where it is
            elif kind in ("play_on", "play_off"):
                st["playing"] = kind == "play_on"
                if one_shot and kind == "play_on":
                    # A one-lap trigger: rewind and arm 2L half-samples
                    # of travel (a retrigger mid-lap restarts from 0).
                    if st["exists"]:
                        L2 = 2 * int(st["L"])
                        st["h"] = (L2 - 2) % L2 if st["reverse"] else 0
                        st["shot"] = L2
                    else:
                        st["shot"] = None
            else:  # tick
                abs_t = n0 + t
                if int(st["last_edge"]) >= 0:
                    st["interval"] = abs_t - int(st["last_edge"])
                st["last_edge"] = abs_t
                created = False
                # A pending edge waits for the clock's period to be known
                # (its second tick) when it would CREATE the loop -- the
                # buffer cannot be sized before that. Edges on an existing
                # loop are honoured on any tick.
                if st["pending"] is not None and (
                        st["exists"] or int(st["interval"]) > 0):
                    was = bool(st["exists"])
                    apply_rec(bool(st["pending"]))
                    st["pending"] = None
                    created = bool(st["exists"]) and not was
                if st["exists"] and not created:
                    st["ticks"] = int(st["ticks"]) + 1
                    if int(st["ticks"]) % length_ticks == 0:
                        st["h"] = 0                # hard sync at the loop boundary
        run(seg_start, frames)
        st["n"] = n0 + frames

        return {"out": out.astype(np.float32), "pos": pos.astype(np.float32)}
