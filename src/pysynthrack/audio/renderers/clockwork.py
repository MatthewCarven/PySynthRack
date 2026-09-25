"""Clockwork renderers: euclidean, burst, bernoulli gate, clock divider.

The first family split out of ``numpy_backend.py`` (2026-09-25). Methods
are moved verbatim into a mixin that ``NumpyBackend`` inherits, so ``self``
is the backend: ``self._state``, ``self._input_buffer``, ``self._GATE_HIGH``
and ``self.sample_rate`` resolve exactly as before, and
``NumpyBackend._render_euclidean`` etc. are still found by attribute lookup
(tests that call or monkeypatch them are untouched).
"""
from __future__ import annotations

import numpy as np


class ClockworkRenderers:
    # ----- Clockwork trio (euclidean / burst / bernoulli) ------------------

    def _render_euclidean(self, module, frames: int, buffers, patch) -> dict:
        """Euclidean rhythm gate (see modules/clockwork.py).

        Per-sample edge loop on tolist()'d rows (sequencer precedent,
        slew scalar lesson). The step length is measured from the last
        two clock edges (carried across blocks); an active step's gate
        then runs ``gate_len`` of that measurement, also carried across
        blocks — until an interval exists the gate simply mirrors the
        clock's own high time. ``accent`` is the ``accent_fills`` layer
        intersected with the main pattern (accents always land on hits).
        A reset rising edge realigns so the next clock plays step 1.
        ``fills_cv`` is read AT each clock edge (rounded, clamped to
        0..steps) and the pattern is rebuilt there — the step that fires
        is decided by the fill count the CV had when it ticked.
        """
        from ...modules.clockwork import euclidean_pattern

        clock = self._input_buffer(patch, buffers, module.id, "clock")
        reset = self._input_buffer(patch, buffers, module.id, "reset")
        fills_cv = self._input_buffer(patch, buffers, module.id, "fills_cv")

        steps = int(module.params.get("steps", 16))
        fills = int(module.params.get("fills", 4))
        rotate = int(module.params.get("rotate", 0))
        accent_fills = int(module.params.get("accent_fills", 0))
        try:
            gate_len = float(module.params.get("gate_len", 0.5))
        except (TypeError, ValueError):
            gate_len = 0.5
        gate_len = min(1.0, max(0.05, gate_len))
        try:
            fills_depth = float(module.params.get("fills_cv_depth", 8.0))
        except (TypeError, ValueError):
            fills_depth = 8.0

        pattern = euclidean_pattern(steps, fills, rotate)
        acc_layer = euclidean_pattern(steps, accent_fills, rotate)
        n_steps = len(pattern)
        cv_row = fills_cv.tolist() if fills_cv is not None else None
        cur_fills = fills

        st = self._state.setdefault(
            module.id,
            {
                "idx": -1, "prev_clock": False, "prev_reset": False,
                "last_edge": -1, "interval": 0, "samples": 0,
                "gate_rem": 0, "acc_rem": 0,
                "gate_mirror": False, "acc_mirror": False,
            },
        )
        idx = int(st["idx"])
        prev_c = bool(st["prev_clock"])
        prev_r = bool(st["prev_reset"])
        last_edge = int(st["last_edge"])
        interval = int(st["interval"])
        base = int(st["samples"])
        gate_rem = int(st["gate_rem"])
        acc_rem = int(st["acc_rem"])
        gate_mirror = bool(st["gate_mirror"])
        acc_mirror = bool(st["acc_mirror"])

        thresh = self._GATE_HIGH
        c_row = (clock > thresh).tolist() if clock is not None else [False] * frames
        r_row = (reset > thresh).tolist() if reset is not None else [False] * frames

        gate_out = np.zeros(frames, dtype=np.float32)
        acc_out = np.zeros(frames, dtype=np.float32)
        g_list = [0.0] * frames
        a_list = [0.0] * frames

        for n in range(frames):
            c = c_row[n]
            r = r_row[n]
            if r and not prev_r:
                idx = -1  # next clock edge plays step 1
            prev_r = r
            if c and not prev_c:
                now = base + n
                if last_edge >= 0:
                    interval = now - last_edge
                last_edge = now
                idx = (idx + 1) % n_steps
                if cv_row is not None:
                    want = fills + int(round(fills_depth * cv_row[n]))
                    want = max(0, min(n_steps, want))
                    if want != cur_fills:
                        cur_fills = want
                        pattern = euclidean_pattern(steps, cur_fills, rotate)
                if pattern[idx]:
                    if interval > 0:
                        gate_rem = max(1, int(round(gate_len * interval)))
                        gate_mirror = False
                    else:
                        gate_mirror = True
                    if acc_layer[idx]:
                        if interval > 0:
                            acc_rem = gate_rem
                            acc_mirror = False
                        else:
                            acc_mirror = True
            prev_c = c
            if gate_mirror and not c:
                gate_mirror = False
            if acc_mirror and not c:
                acc_mirror = False
            if gate_rem > 0:
                g_list[n] = 1.0
                gate_rem -= 1
            elif gate_mirror:
                g_list[n] = 1.0
            if acc_rem > 0:
                a_list[n] = 1.0
                acc_rem -= 1
            elif acc_mirror:
                a_list[n] = 1.0

        gate_out[:] = g_list
        acc_out[:] = a_list
        st.update(
            idx=idx, prev_clock=prev_c, prev_reset=prev_r,
            last_edge=last_edge, interval=interval, samples=base + frames,
            gate_rem=gate_rem, acc_rem=acc_rem,
            gate_mirror=gate_mirror, acc_mirror=acc_mirror,
        )
        return {"gate": gate_out, "accent": acc_out}

    def _render_burst(self, module, frames: int, buffers, patch) -> dict:
        """Ratchet generator (see modules/clockwork.py).

        Free-running bursts are SCHEDULED whole at the trigger edge
        (absolute sample times — deterministic, block-size independent):
        gate k starts at ``(count/rate)·(k/count)^e`` with
        ``e = 2^spread`` warping the grid, runs 40% of its local
        interval, and carries ``env = (1−decay)^k``. With ``clock``
        patched the pending gates instead land on every ``division``-th
        clock edge, mirroring the clock's high time. A retrigger
        restarts the burst (pending events dropped).
        """
        trig = self._input_buffer(patch, buffers, module.id, "trigger")
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        count_cv = self._input_buffer(patch, buffers, module.id, "count_cv")

        count_knob = max(1, min(16, int(module.params.get("count", 3))))
        try:
            count_depth = float(module.params.get("count_cv_depth", 8.0))
        except (TypeError, ValueError):
            count_depth = 8.0
        cv_row = count_cv.tolist() if count_cv is not None else None
        count = count_knob
        try:
            rate = float(module.params.get("rate", 8.0))
        except (TypeError, ValueError):
            rate = 8.0
        rate = min(50.0, max(0.5, rate))
        division = max(1, min(8, int(module.params.get("division", 1))))
        decay = min(1.0, max(0.0, float(module.params.get("decay", 0.3))))
        spread = min(1.0, max(-1.0, float(module.params.get("spread", 0.0))))
        sr = float(self.sample_rate)

        st = self._state.setdefault(
            module.id,
            {
                "samples": 0, "prev_trig": False, "prev_clock": False,
                "events": [],  # [start_abs, len, env] pending/active
                "pending_clock": 0,  # gates still to fire on clock edges
                "edges_seen": 0, "env_val": 0.0, "gate_until": -1,
                "mirror": False, "k": 0,
            },
        )
        base = int(st["samples"])
        thresh = self._GATE_HIGH
        t_row = (trig > thresh).tolist() if trig is not None else [False] * frames
        c_row = (clock > thresh).tolist() if clock is not None else [False] * frames
        prev_t = bool(st["prev_trig"])
        prev_c = bool(st["prev_clock"])
        clocked = clock is not None

        gate = [0.0] * frames
        env = [0.0] * frames
        events = st["events"]

        for n in range(frames):
            t = t_row[n]
            c = c_row[n]
            now = base + n
            if t and not prev_t:
                events.clear()
                if cv_row is not None:
                    # `count` is a property of THIS burst: read at the
                    # trigger edge, rounded, clamped like the knob.
                    count = max(1, min(16, count_knob + int(round(count_depth * cv_row[n]))))
                if clocked:
                    st["pending_clock"] = count
                    st["edges_seen"] = 0
                    st["k"] = 0
                else:
                    e = 2.0 ** spread
                    dur = count / rate
                    times = [dur * ((k / count) ** e) for k in range(count)]
                    times.append(dur)
                    for k in range(count):
                        start = now + int(round(times[k] * sr))
                        length = max(
                            1, int(round(0.4 * (times[k + 1] - times[k]) * sr))
                        )
                        events.append([start, length, (1.0 - decay) ** k])
            prev_t = t
            if clocked and c and not prev_c and st["pending_clock"] > 0:
                if st["edges_seen"] % division == 0:
                    k = st["k"]
                    st["k"] = k + 1
                    st["pending_clock"] -= 1
                    # Mirror the clock high; env holds while it does.
                    st["mirror"] = True
                    st["env_val"] = (1.0 - decay) ** k
                st["edges_seen"] += 1
            prev_c = c
            if st["mirror"]:
                if c:
                    gate[n] = 1.0
                    env[n] = st["env_val"]
                else:
                    st["mirror"] = False
            for ev in events:
                if ev[0] <= now < ev[0] + ev[1]:
                    gate[n] = 1.0
                    env[n] = ev[2]
                    break

        st["events"] = [ev for ev in events if ev[0] + ev[1] > base + frames]
        st["samples"] = base + frames
        st["prev_trig"] = prev_t
        st["prev_clock"] = prev_c
        return {
            "gate": np.array(gate, dtype=np.float32),
            "env": np.array(env, dtype=np.float32),
        }

    def _render_clock_divider(self, module, frames: int, buffers, patch) -> dict:
        """Clock divider / multiplier (see modules/clockwork.py).

        Per-sample edge loop on tolist()'d rows with an absolute sample
        counter (the burst precedent), so every gate is placed at an
        exact sample and the result is block-size independent. Edges
        since the last reset are counted; edge ``i`` fires ``div2`` /
        ``div4`` / ``div8`` / ``divn`` when ``i % k == 0``, so the first
        edge after a reset is the downbeat on every output. Gates are
        pulses of ``pw × (that output's period)``, scheduled as
        ``[start, length]`` events from the last measured input interval;
        before an interval exists (the very first edge) they mirror the
        clock's high time instead, the euclidean's rule.

        ``swing`` delays every second ``divn`` gate by ``swing × n ×
        interval`` — scheduled, since it no longer sits on an edge.
        ``mult`` fires on each edge and schedules ``m − 1`` more at
        ``interval × k / m``; the pending ones are dropped when the next
        real edge arrives early (a tempo change), so multiplication is
        approximate for exactly one period.

        **Gate lengths off the AVERAGE period (2026-09-22).** The
        divisions' gate lengths used to come from the *last* measured
        interval, and a swung clock's intervals alternate long/short —
        so ``divn`` on an odd ``n`` flapped between two lengths (a 2:1
        flutter at ``swing`` 0.3) and ``div2``/``div4``/``div8``, which
        always land on the even pulses and so always measure the SHORT
        interval, sat systematically ~30% under their true period. The
        lengths now come from ``avg`` — the mean of the last TWO
        measured intervals, which is exactly the straight period of a
        swung clock and exactly the last interval of a steady one, so a
        steady clock's render is untouched. Only the FALLING edges
        move: every start (the divisions' own edges, and ``divn``'s
        swing offset, which is a *position*) still comes from the real
        clock edges and the last real interval. ``mult`` keeps the real
        interval for both, because its job is to subdivide the period
        that actually happened.

        One guard falls out of the same pass: scheduling a gate now
        truncates any earlier gate of the same output to end a sample
        before it (the clock's own ceiling rule). What it frees is
        everything the stale gate would have covered after the new one
        starts, so the gates behind it come back.

        **A gate never merges into the next one (2026-09-24).** The
        truncation alone could not stop a merge on an ON-TIME gate: the
        sample before it has already been emitted high. So a merge is
        now prevented when a gate STARTS -- the ``ahead`` predictor
        places this output's next rising edge (the next ``k`` input
        intervals, alternating ``interval_prev`` / ``interval`` -- exact
        for any period-2 clock, straight or swung -- plus, for ``divn``,
        the next gate's own swing offset off the interval it will see;
        for ``mult``, the next real edge) and the length is capped to end
        at least ONE sample before it. One sample, because every edge
        detector in the backend compares against the previous sample:
        one low sample is a falling edge, and a bigger floor would move
        gates that never collided. The cap only binds where the shipped
        render merged (the prediction is exact on a steady clock), so
        ``swing`` 0 or a ``pw`` under the short side renders bit-exact.
        When the prediction is WRONG (a tempo change, a reset, the first
        interval of a swung clock) the early-edge fallback catches it: a
        gate due now whose output was emitted high on the previous
        sample (carried across blocks in ``last_high``) starts one
        sample late, so the stale gate ends a sample before the new
        rising edge. Measured over pw 0.5..0.95 x clock swing 0/0.3/0.5
        x divider swing 0/0.3/0.5 x ``n`` 1/3/4/5 on the real 8 Hz
        clock: 2519 merged ``divn`` gates and 2256 merged ``mult``
        gates -> 0 and 0 (``div2``/``div4``/``div8`` never merged -- an
        even division spans ``k`` x the mean exactly). The 0.3-swung
        ``n`` 3 ``pw`` 0.9 case: 15 of 32 ``divn`` gates merged -> none.

        **A late gate past the next edge keeps its place (2026-09-25).**
        With clock swing AND divider swing both at 0.5 on ``n`` 1, a late
        gate's offset (half the LONG interval) lands after the next
        (short) edge, and the on-time gate scheduled there used to
        supersede it -- 47 of 96 dropped at every ``pw``. Now a gate
        already scheduled two or more samples after a new one is KEPT
        and the new one ends a sample before it rises: both keep their
        own rising edge, and the swing offset stays a position. Over the
        real-clock sweep (clock swing 0/0.3/0.5 x ``pw`` 0.5..0.95 x
        divider swing 0/0.3/0.5 x ``n`` 1/3/4/5) that took 188 missing
        ``divn`` rises of 6192 to 0; every render without a crossing is
        bit-identical.
        """
        clock = self._input_buffer(patch, buffers, module.id, "clock")
        reset = self._input_buffer(patch, buffers, module.id, "reset")

        def _int(name, default, lo, hi):
            try:
                return max(lo, min(hi, int(module.params.get(name, default))))
            except (TypeError, ValueError):
                return default

        def _flt(name, default, lo, hi):
            try:
                return max(lo, min(hi, float(module.params.get(name, default))))
            except (TypeError, ValueError):
                return default

        n_div = _int("n", 3, 1, 32)
        m_mult = _int("m", 2, 2, 4)
        swing = _flt("swing", 0.0, 0.0, 0.75)
        pw = _flt("pw", 0.5, 0.05, 0.95)

        names = ("div2", "div4", "div8", "divn", "mult")
        divisors = {"div2": 2, "div4": 4, "div8": 8, "divn": n_div, "mult": 1}

        st = self._state.setdefault(
            module.id,
            {
                "samples": 0, "prev_clock": False, "prev_reset": False,
                "count": 0, "last_edge": -1, "interval": 0, "divn_emitted": 0,
                "interval_prev": 0,
                "events": {k: [] for k in names},
                "mirror": {k: False for k in names},
                "last_high": {k: False for k in names},
            },
        )
        base = int(st["samples"])
        prev_c = bool(st["prev_clock"])
        prev_r = bool(st["prev_reset"])
        count = int(st["count"])
        last_edge = int(st["last_edge"])
        interval = int(st["interval"])
        interval_prev = int(st.get("interval_prev", 0))
        divn_emitted = int(st["divn_emitted"])
        events = st["events"]
        mirror = st["mirror"]
        last_high = st.setdefault("last_high", {k: False for k in names})

        thresh = self._GATE_HIGH
        c_row = (clock > thresh).tolist() if clock is not None else [False] * frames
        r_row = (reset > thresh).tolist() if reset is not None else [False] * frames
        outs = {k: [0.0] * frames for k in names}

        def ahead(j):
            """(sum, last) of the next ``j`` input intervals, predicted.

            A swung clock's intervals alternate long/short, so the one
            after ``interval`` is predicted to be ``interval_prev`` and
            the one after that ``interval`` again; a steady clock has
            both equal and this is just ``j x interval``. Exact for any
            period-2 clock; a tempo change can make it wrong, and the
            early-edge fallback in ``pulse`` catches that."""
            nxt = interval_prev if interval_prev > 0 else interval
            return ((j + 1) // 2 * nxt + j // 2 * interval,
                    nxt if j % 2 else interval)

        def pulse(name, start, period, n, next_start=None):
            """Schedule a gate of pw x period at `start` (absolute).

            Three rules keep every gate its own rising edge:

            * **Prospective cap.** ``next_start`` is where this output's
              NEXT gate is predicted to rise; the length is capped to end
              at least one sample before it (only when the cap is at
              least one sample -- a gate that cannot fit before the next
              one is left to the fallback). The scheduler cannot un-write
              a sample it has emitted, so a merge has to be prevented
              when the gate STARTS.
            * **Early-edge fallback.** A gate due now whose output was
              emitted high on the previous sample (the prediction was
              wrong: a tempo change, a reset) starts one sample late
              instead, so that sample reads low and the stale gate ends
              a sample before the new rising edge.
            * Any earlier gate of the same output is truncated to end one
              sample before this one (and dropped if that leaves
              nothing) -- the clock's own "cut a sample before the next
              edge" ceiling, applied here. A gate already scheduled at
              least two samples LATER (a swung ``divn`` gate reaching
              past this one) is kept, and this one is cut to end a
              sample before it; one at or a sample after is superseded.
            """
            now_ = base + n
            if start <= now_ and (outs[name][n - 1] > 0.0 if n else last_high[name]):
                start = now_ + 1
            length = int(round(pw * period))
            if next_start is not None and next_start - start - 1 >= 1:
                length = min(length, next_start - start - 1)
            row = events[name]
            kept = []
            for ev in row:
                if ev[0] >= start:
                    if ev[0] - start >= 2:
                        # Already scheduled LATER than us -- a swung divn
                        # gate whose offset reaches past this on-time one.
                        # Both keep their rising edge: we end a sample
                        # before it starts.
                        length = min(length, ev[0] - start - 1)
                        kept.append(ev)
                    continue           # at (or a sample after) us: superseded
                ev[1] = min(ev[1], start - ev[0] - 1)
                if ev[1] > 0:
                    kept.append(ev)
            kept.append([start, max(1, length)])
            events[name] = kept

        for n in range(frames):
            c = c_row[n]
            r = r_row[n]
            now = base + n
            if r and not prev_r:
                count = 0
                divn_emitted = 0
                for k in names:
                    events[k].clear()
            prev_r = r
            if c and not prev_c:
                if last_edge >= 0:
                    interval_prev = interval
                    interval = now - last_edge
                last_edge = now
                known = interval > 0
                # The divider's own measured average period: the mean of
                # the last TWO intervals. On a swung clock the intervals
                # alternate long/short and this is the straight period
                # exactly; on a steady one both are the same number and
                # it IS the last interval, so nothing moves.
                avg = ((interval + interval_prev) / 2.0 if interval_prev > 0
                       else float(interval))
                for name in ("div2", "div4", "div8"):
                    if count % divisors[name] == 0:
                        if known:
                            k_div = divisors[name]
                            pulse(name, now, k_div * avg, n,
                                  now + ahead(k_div)[0])
                        else:
                            mirror[name] = True
                if count % n_div == 0:
                    # The swing offset is a POSITION: it stays on the
                    # last real interval, so no edge moves. The length
                    # comes from the average, capped to end a sample
                    # before the NEXT divn gate: n predicted intervals
                    # on, plus that gate's own swing offset if it is a
                    # late one (measured off the interval it will see).
                    period = n_div * interval
                    late = (divn_emitted % 2 == 1) and swing > 0.0 and known
                    if known:
                        span, last_iv = ahead(n_div)
                        next_start = now + span
                        if divn_emitted % 2 == 0 and swing > 0.0:
                            next_start += int(round(swing * n_div * last_iv))
                        start = now + int(round(swing * period)) if late else now
                        pulse("divn", start, n_div * avg, n, next_start)
                    else:
                        mirror["divn"] = True
                    divn_emitted += 1
                # mult: this edge, plus m-1 scheduled from the last
                # interval; anything still pending from the previous
                # period is dropped (the edge came early or late).
                events["mult"] = [ev for ev in events["mult"] if ev[0] <= now]
                if known:
                    sub = interval / m_mult
                    nxt_edge = now + ahead(1)[0]
                    for k in range(m_mult):
                        pulse("mult", now + int(round(k * sub)), sub, n, nxt_edge)
                else:
                    mirror["mult"] = True
                count += 1
            prev_c = c
            for name in names:
                if mirror[name]:
                    if c:
                        outs[name][n] = 1.0
                    else:
                        mirror[name] = False
                for ev in events[name]:
                    if ev[0] <= now < ev[0] + ev[1]:
                        outs[name][n] = 1.0
                        break

        end = base + frames
        for name in names:
            events[name] = [ev for ev in events[name] if ev[0] + ev[1] > end]
            if frames:
                last_high[name] = outs[name][-1] > 0.0
        st.update(
            samples=end, prev_clock=prev_c, prev_reset=prev_r, count=count,
            last_edge=last_edge, interval=interval, interval_prev=interval_prev,
            divn_emitted=divn_emitted, events=events, mirror=mirror,
        )
        return {k: np.array(v, dtype=np.float32) for k, v in outs.items()}

    def _render_bernoulli(self, module, frames: int, buffers, patch) -> dict:
        """Probability gate router (see modules/clockwork.py).

        The routing decision latches on each rising edge (one seeded rng
        draw per gate — deterministic, block-size independent) and the
        whole gate mirrors ``in`` on the chosen output until it falls.
        ``toggle`` mode flips outputs with probability p instead of
        picking independently.
        """
        gate_in = self._input_buffer(patch, buffers, module.id, "in")
        p_cv = self._input_buffer(patch, buffers, module.id, "p_cv")

        try:
            prob = float(module.params.get("probability", 0.5))
        except (TypeError, ValueError):
            prob = 0.5
        mode = str(module.params.get("mode", "independent"))
        try:
            seed = int(module.params.get("seed", 1))
        except (TypeError, ValueError):
            seed = 1
        seed = max(0, seed)

        st = self._state.setdefault(module.id, {})
        if st.get("seed") != seed:
            st["seed"] = seed
            st["rng"] = np.random.default_rng(seed)
            st["prev"] = False
            st["choice_a"] = True
        rng = st["rng"]

        out_a = np.zeros(frames, dtype=np.float32)
        out_b = np.zeros(frames, dtype=np.float32)
        if gate_in is None:
            return {"out_a": out_a, "out_b": out_b}

        high = gate_in > self._GATE_HIGH
        prev_arr = np.empty_like(high)
        prev_arr[0] = bool(st["prev"])
        prev_arr[1:] = high[:-1]
        edges = np.flatnonzero(high & ~prev_arr).tolist()
        st["prev"] = bool(high[-1])

        choice_a = bool(st["choice_a"])
        bounds = edges + [frames]
        pos = 0
        for i, seg_end in enumerate(bounds):
            seg = slice(pos, seg_end)
            masked = np.where(high[seg], gate_in[seg], 0.0)
            (out_a if choice_a else out_b)[seg] = masked
            if i < len(edges):
                e = edges[i]
                p = prob + (float(p_cv[e]) if p_cv is not None else 0.0)
                p = min(1.0, max(0.0, p))
                draw = float(rng.random())
                if mode == "toggle":
                    if draw < p:
                        choice_a = not choice_a
                else:
                    choice_a = draw < p
                pos = e
        st["choice_a"] = choice_a
        return {"out_a": out_a, "out_b": out_b}
