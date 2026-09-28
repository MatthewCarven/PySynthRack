"""Physical-model and sample-playback renderers: pluck (Karplus-Strong),
bowed string, wind (flute / reed), modal, the drum voices (the shared
``_render_drum`` engine plus kick / snare / hat) and the sampler.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". The synthesis kernels are module-level functions of
``numpy_backend`` (``_pluck_exciter``, ``_kick_hit``, ``_snare_hit``,
``_hat_hit``, ``_hermite4`` -- tests import them from there, and the
resampler shares ``_hermite4``), so each renderer imports the one it
needs lazily at the top of its body, outside every loop (``numpy_backend``
imports this module, so a top-level import would be circular). Still in
the backend and reached through ``self``: ``_drum_edge_value`` (also read
by the ADSR), ``_gate_ramp_env`` / ``_finite_mean`` / ``_pow2_clipped`` /
``_GATE_HIGH``, and the sampler's lifecycle members that the engine, the
GUI or other tests call -- ``_new_sampler_state`` and
``_start_sample_loader`` (``compile`` pre-warms loads),
``sampler_overview`` (GUI hook), ``wait_for_sample_loads`` (test hook)
-- plus ``_new_sampler_voice`` / ``_sampler_advance``, which sit between
them as decorated statics a block boundary cannot anchor on.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter


class PhysicalRenderers:
    # ----- Pluck rendering -------------------------------------------------

    # Pitch clamps: below 20 Hz the ring outgrows its allocation; above
    # sr/4 the loop is too short for the machinery (and sounds like a
    # click anyway).
    _PLUCK_MIN_F0 = 20.0
    _PLUCK_C4 = 261.6255653005986
    # A decayed string below this output peak early-outs (ring zeroed so
    # the voice renders exact silence for free until re-plucked).
    _PLUCK_SILENCE = 1e-5
    _PLUCK_SEED = 0x504C5543  # "PLUC"

    # ----- Bowed string ------------------------------------------------------

    # Pitch clamps: the delay allocation below, and above ~2 kHz the
    # bridge-side delay (position x period) shrinks to a handful of
    # samples and the chunked loop degrades to per-sample cost.
    _BOW_MIN_F0 = 30.0
    _BOW_MAX_F0 = 2000.0
    _BOW_C4 = 261.6255653005986
    # Bridge reflection: one-pole lowpass, pole from ``damping``
    # (0.15 = wide open .. 0.7 = dark), fixed loss gain per round trip.
    _BOW_POLE_LO = 0.15
    _BOW_POLE_HI = 0.70
    _BOW_LOSS = 0.95
    # Friction table (STK BowTable): slope from ``pressure``, and the
    # bow velocity range from ``velocity``.
    _BOW_SLOPE_MAX = 5.0
    _BOW_SLOPE_RANGE = 4.0
    _BOW_VEL_MIN = 0.03
    _BOW_VEL_RANGE = 0.2
    # Body: parallel constant-peak bandpasses (Hz, Q, gain) -- an air
    # mode, the two main wood modes and the bridge hill -- mixed by
    # ``body``; _BOW_BODY_GAIN level-matches the bank to the raw bridge.
    _BOW_BODY = ((275.0, 4.0, 1.0), (460.0, 5.0, 0.9), (550.0, 6.0, 0.7),
                 (1100.0, 2.5, 0.6), (2200.0, 2.0, 0.4))
    _BOW_BODY_GAIN = 1.2
    _BOW_OUT_GAIN = 2.0
    # A lifted, decayed string below this output peak early-outs.
    _BOW_SILENCE = 1e-5

    # ----- Wind (flute / reed) ------------------------------------------------

    _WIND_C4 = 261.6255653005986
    _WIND_MAX_F0 = 2500.0
    # The flute's jet model loses its register below ~80 Hz; the reed
    # goes down to the contrabass.
    _WIND_MIN_F0 = {"flute": 80.0, "reed": 30.0}
    # Bore reflection one-pole pole ranges (damping 0..1).
    _WIND_POLE = {"flute": (0.5, 0.85), "reed": (0.3, 0.8)}
    # Breath pressure ranges (breath 0..1): the flute speaks in a window
    # (jet saturation kills it above ~1.5), the reed needs ~0.58 to speak
    # and closes above ~1.1 -- both measured 2026-09-18.
    _WIND_PRESSURE = {"flute": (0.85, 0.55), "reed": (0.58, 0.42)}
    # Flute: bore tuned to 1.5 periods (STK: "we're overblowing here"),
    # times a measured regime correction (the jet's phase pulls the
    # regime sharp by ~1.5%); jet at 0.32 of the bore; 0.5/0.5 jet/end
    # reflections; DC block in the return.
    _WIND_FLUTE_BORE = 1.5 * 1.015
    _WIND_FLUTE_JET = 0.32
    _WIND_FLUTE_REFL = 0.5
    # Reed: reflection -0.95 through the loss filter; table 0.7 - 0.3 dp.
    _WIND_REED_REFL = 0.95
    _WIND_REED_OFFSET = 0.7
    _WIND_REED_SLOPE = 0.3
    _WIND_DC_POLE = 0.995
    _WIND_MODEL_GAIN = {"flute": 0.3, "reed": 1.0}
    _WIND_OUT_GAIN = 2.0
    _WIND_SILENCE = 1e-5
    _WIND_SEED = 0x57494E44  # "WIND"

    # ----- sampler ----------------------------------------------------------

    # Retrigger declick: a voice re-struck while still sounding keeps its old
    # playhead running for this long under a falling ramp while the new one
    # fades in, so the jump never clicks (the drum-voice idiom).
    _SAMPLER_XFADE_MS = 2.0

    @staticmethod
    def _sampler_read(samples, positions):
        """Read a sample buffer at fractional ``positions`` (4-tap cubic).

        The resampler's ``_hermite4`` verbatim, which is the point: at an
        integer position it returns that sample *exactly* (the spline's
        constant term is ``p0``, untouched by float ops), so a unity-rate
        read is a bit-exact copy of the file and an octave jump is a
        bit-exact ``[::2]``. Neighbours are clamped to the buffer, which
        only ever affects the outer taps of the first and last sample.
        """
        n = samples.shape[0]
        base = np.floor(positions)
        i0 = base.astype(np.int64)
        t = positions - base
        top = n - 1
        from ..numpy_backend import _hermite4  # lazy: it imports this module

        return _hermite4(
            samples[np.clip(i0 - 1, 0, top)],
            samples[np.clip(i0, 0, top)],
            samples[np.clip(i0 + 1, 0, top)],
            samples[np.clip(i0 + 2, 0, top)],
            t,
        )

    @staticmethod
    def _sampler_reader(chain, rate, antialias):
        """Which mip levels a read at ``rate`` takes: ``[(buf, scale, w)]``.

        ``scale`` maps a position in the original to one in that level
        (``2**-k``, exact in float), ``w`` its weight. Without ``antialias``
        — or at and below unity, whatever the setting — it is level 0
        alone at scale 1, i.e. the original read, untouched: that is what
        keeps the neutral bit-exact with the chain switched on.
        """
        if not antialias or len(chain) < 2:
            return [(chain[0], 1.0, 1.0)]
        from ...modules.sampler import mip_blend

        k0, k1, frac = mip_blend(rate, len(chain) - 1)
        if frac <= 0.0:
            return [(chain[k0], 2.0 ** -k0, 1.0)]
        return [(chain[k0], 2.0 ** -k0, 1.0 - frac), (chain[k1], 2.0 ** -k1, frac)]

    def _sampler_read_mix(self, reader, positions):
        """``_sampler_read`` through a mip reader (one or two levels)."""
        if len(reader) == 1 and reader[0][1] == 1.0:
            return self._sampler_read(reader[0][0], positions)
        out = None
        for buf, scale, w in reader:
            part = self._sampler_read(buf, positions * scale)
            if w != 1.0:
                part = part * w
            out = part if out is None else out + part
        return out

    def _sampler_seam(self, block, reader, positions, loop, forward=True):
        """Crossfade the loop's tail into the lap before it, in place.

        Over the last ``xfade`` of the loop the read is mixed with
        ``read(pos - loop_length)`` — the same point one lap earlier, which
        is the material running *into* ``loop_start``. The weight reaches 1
        exactly as the playhead reaches ``loop_end``, so the wrap lands on
        what was already sounding and the seam is continuous instead of a
        step (the resampler's seam-declick lesson: fade between two reads,
        never cut). In reverse the roles mirror: the fade sits just above
        ``loop_start`` and reads one lap *later*, which is what runs into
        ``loop_end`` from above.

        A ``loop_start`` at the very beginning of the file has no previous
        lap; the read clamps to the file's first sample, which is exactly
        where the wrap is about to land, so the fade stays continuous.

        Only the faded samples are read twice — everything else keeps the
        single read it already had, which is what leaves ``xfade`` 0
        bit-exact rather than merely close.
        """
        lo, hi, length, xfade = loop
        if xfade <= 0.0:
            return
        if forward:
            weight = (positions - (hi - xfade)) / xfade
        else:
            weight = ((lo + xfade) - positions) / xfade
        idx = np.flatnonzero(weight > 0.0)
        if idx.size == 0:
            return
        w = weight[idx]
        lap = positions[idx] - length if forward else positions[idx] + length
        prev = self._sampler_read_mix(reader, lap)
        block[idx] = block[idx] * (1.0 - w) + prev * w

    def _sampler_segment(self, voice, chains, count, end_sample, loop_for,
                         antialias):
        """Render ``count`` samples of one voice and advance its playhead.

        Everything vectorizes because the playhead is affine within a
        segment: ``pos + rate·arange(n)``. Segments are cut at gate edges
        by the caller, which is the only place ``rate``/state can change.
        ``chains`` is one mip chain per output channel (one for a mono
        file, two for stereo) and the result is ``(C, count)`` float64 —
        the same positions read through each. ``loop_for(start)`` gives
        the loop ``(lo, hi, length, xfade)`` in sample units for a voice
        that began at ``start``, or None for the modes that don't loop.
        """
        C = len(chains)
        out = np.zeros((C, count), dtype=np.float64)
        if count <= 0:
            return out

        if voice["active"]:
            rate = voice["rate"]
            forward = rate >= 0.0
            loop = loop_for(voice["start"])
            positions, next_pos, live = self._sampler_advance(
                voice["pos"], rate, count, (voice["start"], end_sample), loop
            )
            if live > 0:
                for c, chain in enumerate(chains):
                    reader = self._sampler_reader(chain, rate, antialias)
                    out[c, :live] = self._sampler_read_mix(reader, positions[:live])
                    if loop is not None:
                        self._sampler_seam(out[c], reader, positions, loop, forward)
                if voice["gain"] != 1.0:
                    out[:, :live] *= voice["gain"]
            voice["pos"] = next_pos
            if live < count:
                # Ran off the end of the region: silent from here, and the
                # voice costs nothing until it is retriggered. A looping
                # voice never reaches this — it wrapped instead.
                voice["active"] = False
                voice["releasing"] = False
                voice["rel_left"] = 0

            # Declick ramp in (retrigger, or a non-zero `attack`).
            if voice["atk_left"] > 0:
                n = min(count, voice["atk_left"])
                total = float(voice["atk_total"])
                done = total - voice["atk_left"]
                out[:, :n] *= (done + 1.0 + np.arange(n, dtype=np.float64)) / total
                voice["atk_left"] -= n

            # Gated release: a linear ramp out, then the voice is done.
            if voice["releasing"] and voice["rel_left"] > 0:
                n = min(count, voice["rel_left"])
                total = float(voice["rel_total"])
                done = total - voice["rel_left"]
                out[:, :n] *= 1.0 - (done + 1.0 + np.arange(n, dtype=np.float64)) / total
                if n < count:
                    out[:, n:] = 0.0
                voice["rel_left"] -= n
                if voice["rel_left"] <= 0:
                    voice["active"] = False
                    voice["releasing"] = False

        # The abandoned playhead of a retriggered voice, fading out under
        # the new one. Added, not blended: two reads of the same buffer sum
        # linearly, so equal-and-opposite ramps cross without a notch.
        if voice["xf_left"] > 0:
            n = min(count, voice["xf_left"])
            rate = voice["xf_rate"]
            forward = rate >= 0.0
            # The abandoned playhead keeps doing what the voice was doing,
            # loop, region and all, so a retrigger near the seam doesn't
            # drop out.
            loop = loop_for(voice["xf_start"])
            positions, next_xf, live = self._sampler_advance(
                voice["xf_pos"], rate, n, (voice["xf_start"], end_sample), loop
            )
            if live > 0:
                total = float(voice["xf_total"])
                done = total - voice["xf_left"]
                ramp = 1.0 - (done + 1.0 + np.arange(live, dtype=np.float64)) / total
                if voice["xf_gain"] != 1.0:
                    ramp = ramp * voice["xf_gain"]
                for c, chain in enumerate(chains):
                    reader = self._sampler_reader(chain, rate, antialias)
                    tail = self._sampler_read_mix(reader, positions[:live])
                    if loop is not None:
                        self._sampler_seam(tail, reader, positions[:live], loop, forward)
                    out[c, :live] += tail * ramp
            voice["xf_pos"] = next_xf
            voice["xf_left"] -= n
        return out

    def _render_sampler(self, module, frames: int, buffers, patch):
        """Pitched sample playback with per-voice playheads.

        See ``modules/sampler.py`` for the contract. Structure: the sample
        is decoded whole on a background thread (``_SampleLoader``, the
        convolver precedent) and never touched by the audio thread until
        ready, so a fresh or changed ``path`` is silence rather than a
        dropout. Each voice owns a float64 playhead; a block is cut into
        segments at that voice's gate edges, and within a segment the
        playhead is affine, so the whole segment is one vectorized cubic
        read (``_sampler_read``) per output channel.

        Rate comes from ``playback_rate`` in the module file — one
        definition of what ``root`` means, shared with the tests. Pitch is
        read per block (mean), the ``pluck`` precedent, so glides and
        vibrato track at block rate. ``reverse`` is the same rate with its
        sign flipped, and the playhead starts at the region's end.

        ``loop`` mode is ``gated`` plus a wrapped playhead: the loop
        region is resolved per voice into absolute sample positions
        (``loop_start``/``loop_end`` are fractions **of the region**, so
        moving ``start``/``end`` — or a ``start_cv`` hit — carries the
        loop with them) and handed to the segment renderer, which does
        the wrapping and the seam crossfade. A collapsed or inverted loop
        region resolves to ``None`` and the voice simply plays as
        ``gated`` — kinder than silence for something you drag with a
        slider.

        ``start_cv`` and ``vel`` are read **at the edge sample** and
        latched into the voice: each hit lands where the CV said at the
        moment it fired, which is what a sequencer-driven slicer needs.

        Outputs: ``out`` (mono), ``out_l`` / ``out_r``. A mono file is one
        read feeding all three; a stereo file reads both channels and
        ``out`` is their half-sum.

        Neutral: root pitch, ``start`` 0, ``end`` 1, ``attack`` 0,
        ``level`` 1 makes the rate exactly 1.0, every read position an
        integer, and the output the decoded buffer **bit-exact** — with or
        without ``antialias``, which only ever changes a read *above*
        unity. Nothing is faded at the region end for the same reason — a
        sample that stops abruptly is the file's business, and ``end``
        plus a gated ``release`` are the tools for trimming it. A
        unity-rate loop with ``loop_xfade`` 0 keeps that property: it is a
        bit-exact *tiling* of the file.
        """
        from ...modules.sampler import SAMPLER_MODES, playback_rate

        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        gate = self._input_buffer(
            patch, buffers, module.id, "gate", collapse=False
        )
        start_cv = self._input_buffer(
            patch, buffers, module.id, "start_cv", collapse=False
        )
        vel = self._input_buffer(
            patch, buffers, module.id, "vel", collapse=False
        )

        state = self._state.setdefault(module.id, self._new_sampler_state())

        # --- resolve path -> loaded buffer (never blocks the audio thread) ---
        path = self._resolve_media_path(module.params.get("path", ""))
        if path == "":
            if state.get("samples") is not None or state.get("loaded_path"):
                state["samples"] = None
                state["chains"] = None
                state["overview"] = None
                state["loaded_path"] = None
            pend = state.get("pending")
            if pend is not None:
                pend["loader"].close()
                state["pending"] = None
        else:
            pend = state.get("pending")
            if path != state.get("loaded_path") and (
                pend is None or pend.get("path") != path
            ):
                if pend is not None:
                    pend["loader"].close()
                state["pending"] = {
                    "path": path,
                    "loader": self._start_sample_loader(path),
                }
            pend = state.get("pending")
            if pend is not None and pend["loader"].done:
                loader = pend["loader"]
                # Failure is remembered as "this path yields silence" rather
                # than retried every block — a missing file must not spin a
                # decode thread per block.
                ok = loader.ready
                state["samples"] = loader.samples if ok else None
                state["chains"] = loader.chains if ok else None
                state["overview"] = loader.overview if ok else None
                state["loaded_path"] = pend["path"]
                state["pending"] = None

        samples = state.get("samples")
        chains = state.get("chains")

        def silence(shape):
            z = np.zeros(shape, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}

        # No gate cable means nothing can ever start: silence, and drop the
        # voice bank so a reconnect starts clean.
        if gate is None or samples is None or samples.shape[0] < 1 or not chains:
            state["voices"] = []
            state["V"] = 0
            return silence(frames)

        voiced = gate.ndim == 2
        V = 1
        for sig in (pitch, gate, start_cv, vel):
            if sig is not None and sig.ndim == 2:
                voiced = True
                V = max(V, sig.shape[0])

        def row(sig, v):
            if sig is None:
                return None
            if sig.ndim == 2:
                return sig[v] if v < sig.shape[0] else sig[0]
            return sig

        # --- params ---------------------------------------------------------
        def _f(name, default, lo, hi):
            try:
                value = float(module.params.get(name, default))
            except (TypeError, ValueError):
                value = default
            return min(hi, max(lo, value))

        root = _f("root", 60.0, 0.0, 127.0)
        tune = _f("tune", 0.0, -12.0, 12.0)
        fine = _f("fine", 0.0, -50.0, 50.0)
        level = _f("level", 0.8, 0.0, 1.0)
        start_frac = _f("start", 0.0, 0.0, 1.0)
        end_frac = _f("end", 1.0, 0.0, 1.0)
        loop_start_frac = _f("loop_start", 0.0, 0.0, 1.0)
        loop_end_frac = _f("loop_end", 1.0, 0.0, 1.0)
        loop_xfade_ms = _f("loop_xfade", 10.0, 0.0, 100.0)
        attack_ms = _f("attack", 0.0, 0.0, 500.0)
        release_ms = _f("release", 10.0, 1.0, 2000.0)
        start_depth = _f("start_cv_depth", 1.0, -1.0, 1.0)
        reverse = bool(module.params.get("reverse", False))
        antialias = bool(module.params.get("antialias", False))
        mode = str(module.params.get("mode", "one_shot"))
        if mode not in SAMPLER_MODES:
            mode = "one_shot"

        n_samples = int(samples.shape[0])
        start_sample = float(start_frac) * n_samples
        end_sample = float(end_frac) * n_samples
        if end_sample <= start_sample:
            # A collapsed or inverted region plays nothing rather than
            # running backwards off the front of the buffer.
            return silence((V, frames) if voiced else frames)

        sr = float(self.sample_rate)
        attack_n = int(round(attack_ms * 1e-3 * sr))
        release_n = max(1, int(round(release_ms * 1e-3 * sr)))
        xfade_n = max(1, int(round(self._SAMPLER_XFADE_MS * 1e-3 * sr)))
        seam_n = float(int(round(loop_xfade_ms * 1e-3 * sr)))

        # The loop region, in absolute samples, for a voice that began at
        # `vstart`. `loop_start`/`loop_end` are fractions OF THE REGION,
        # not of the file, so dragging `start`/`end` (or a start_cv hit)
        # carries the loop along instead of stranding it. None means
        # "don't loop", which is also what a collapsed or inverted loop
        # region gets: play as `gated` rather than fall silent, because
        # this is a thing you drag with a slider.
        def loop_for(vstart):
            if mode != "loop":
                return None
            span = end_sample - vstart
            loop_lo = vstart + loop_start_frac * span
            loop_hi = vstart + loop_end_frac * span
            loop_len = loop_hi - loop_lo
            if loop_len <= 0.0:
                return None
            # Measured on the sample (so it covers the same slice of
            # waveform whatever the pitch) and clamped to the loop: there
            # is only one lap to fade into.
            return (loop_lo, loop_hi, loop_len, min(seam_n, loop_len))

        voices = state.get("voices") or []
        if state.get("V") != V or len(voices) != V:
            voices = [self._new_sampler_voice() for _ in range(V)]
            state["V"] = V
            state["voices"] = voices

        gate_high = self._GATE_HIGH
        C = len(chains)
        out = np.zeros((C, V, frames), dtype=np.float64)

        for v in range(V):
            voice = voices[v]
            g = np.asarray(row(gate, v)) > gate_high
            pv = row(pitch, v)
            cv = self._finite_mean(pv) if pv is not None else 0.0
            rate = playback_rate(cv, root, tune, fine)
            if reverse:
                rate = -rate
            sc = row(start_cv, v)
            vl = row(vel, v)

            prev = voice["prev_gate"]
            shifted = np.empty(frames, dtype=bool)
            shifted[0] = prev
            shifted[1:] = g[:-1]
            rising = np.flatnonzero(g & ~shifted)
            falling = (
                np.flatnonzero(~g & shifted) if mode in ("gated", "loop")
                else np.empty(0, dtype=np.int64)
            )
            voice["prev_gate"] = bool(g[-1]) if frames else prev

            events = sorted(
                [(int(i), "on") for i in rising] + [(int(i), "off") for i in falling]
            )

            row_out = out[:, v, :]
            cursor = 0
            for at, kind in events:
                if at > cursor:
                    row_out[:, cursor:at] = self._sampler_segment(
                        voice, chains, at - cursor, end_sample, loop_for, antialias
                    )
                    cursor = at
                if kind == "on":
                    # Where this hit starts: `start`, moved by start_cv AT
                    # THE EDGE SAMPLE (a slicer wants the value the
                    # sequencer had when it fired, not the block's mean).
                    sfrac = start_frac
                    if sc is not None:
                        sfrac = min(1.0, max(0.0, sfrac + start_depth * float(sc[at])))
                    vstart = sfrac * n_samples
                    if vstart >= end_sample:
                        # Pushed past the end: nothing to play. The hit is
                        # dropped rather than run backwards, and whatever
                        # was sounding is left alone.
                        continue
                    gain = 1.0
                    if vl is not None:
                        gain = max(0.0, float(vl[at]))
                    if voice["active"]:
                        # Hand the sounding playhead to the crossfade tail
                        # before the new one takes over.
                        voice["xf_pos"] = voice["pos"]
                        voice["xf_rate"] = voice["rate"]
                        voice["xf_start"] = voice["start"]
                        voice["xf_gain"] = voice["gain"]
                        voice["xf_left"] = xfade_n
                        voice["xf_total"] = xfade_n
                        ramp_in = max(attack_n, xfade_n)
                    else:
                        ramp_in = attack_n
                    voice["active"] = True
                    voice["start"] = vstart
                    # Reverse begins one sample inside the region's end, so
                    # a unity read is the forward read mirrored exactly.
                    voice["pos"] = (end_sample - 1.0) if reverse else vstart
                    voice["rate"] = rate
                    voice["gain"] = gain
                    voice["releasing"] = False
                    voice["rel_left"] = 0
                    voice["atk_left"] = ramp_in
                    voice["atk_total"] = max(1, ramp_in)
                else:  # gated fall
                    if voice["active"] and not voice["releasing"]:
                        voice["releasing"] = True
                        voice["rel_left"] = release_n
                        voice["rel_total"] = release_n
            if cursor < frames:
                row_out[:, cursor:] = self._sampler_segment(
                    voice, chains, frames - cursor, end_sample, loop_for, antialias
                )
            # A held voice tracks pitch between triggers (block-rate glide);
            # the retrigger above locks the rate at the edge sample.
            if voice["active"] and not voice["releasing"]:
                voice["rate"] = rate

        out *= level
        if C == 1:
            mono = out[0].astype(np.float32)
            if not voiced:
                mono = mono[0]
            return {"out": mono, "out_l": mono.copy(), "out_r": mono.copy()}
        left = out[0]
        right = out[1]
        mono = 0.5 * (left + right)
        if voiced:
            return {
                "out": mono.astype(np.float32),
                "out_l": left.astype(np.float32),
                "out_r": right.astype(np.float32),
            }
        return {
            "out": mono[0].astype(np.float32),
            "out_l": left[0].astype(np.float32),
            "out_r": right[0].astype(np.float32),
        }

    def _render_pluck(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Extended Karplus–Strong (see modules/pluck.py for the contract).

        Per voice, the string is a ring buffer advanced in CHUNKS of at
        most one loop length: within a chunk every read (the ``N`` and
        ``N+1`` taps) lands before this chunk's writes, so the whole
        chunk vectorizes — the damping one-zero is an array blend and the
        allpass fractional delay is one ``lfilter`` call with carried
        ``zi``. Low notes take one or two chunks per block; high notes
        degrade gracefully into more, smaller chunks instead of a
        per-sample loop.

        Tuning: the loop's effective delay is ``N_int + frac + d/2``
        samples — the damping blend ``(1−d/2) + (d/2)z⁻¹`` contributes a
        ``d/2``-sample phase delay, compensated when splitting
        ``sr/f0`` into integer + allpass fraction (``frac`` kept in
        [0.1, 1.1) so the allpass coefficient stays well-conditioned).
        Loop gain ``g = 10^(−3·N/(sr·decay))`` makes ``decay`` read as a
        real t60 independent of pitch.

        Triggers segment the block: at each rising edge the pluck pitch
        is locked from that sample, coefficients rebuilt, the allpass
        state carried (``carry``, the default; cleared with it off),
        and a seeded exciter burst is **added** into the ring (a
        re-pluck superposes on the ringing string — linear loop, so
        click-free by construction; exactly, with ``carry`` on).
        Between triggers an active voice
        re-reads the block-mean pitch each block (glides track at block
        rate). Determinism: each burst's rng is seeded from (module id,
        voice, hit number). Exact block-size independence holds under
        constant pitch (coefficients then rebuild identically).

        Velocity (2026-09-19, love pass): ``vel`` is a knobless
        multiplier on the BURST, read at the trigger's rising-edge sample
        and latched into that hit (the drums' / adsr's edge-latch rule);
        the loop -- decay, damping, tuning -- is untouched, so a soft hit
        is a quieter pluck that rings down the same way. Voice-aware like
        the other inputs: a ``(V, F)`` bus (``midi_input.velocity_cv``)
        latches per voice from its own row, a mono bus is shared, and a
        ``(V, F)`` bus on a mono pluck collapses to the loudest voice at
        the edge. Unpatched the code path is the old one verbatim (no
        multiply), and a bus holding 1.0 is bit-for-bit the same since
        ``x * 1.0 == x``. A non-positive velocity is a SILENT hit: not
        just a zero burst -- every hit also relocks the pitch and (with
        ``carry`` off) clears the allpass state; that clear alone steps
        the output by ~7-18% of the ring's amplitude (measured), an
        audible tick on a hit that is supposed to make no sound. So a
        silent hit leaves
        the string exactly as it was and only advances the hit counter.

        ``vel_color`` (2026-09-20, love pass): the pick's SPECTRUM follows
        the velocity too. The effective colour of a hit is
        ``clamp(color + vel_color * (vel - 1), 0, 1)``, computed at the
        edge from the SAME latched ``scale`` the burst is multiplied by,
        so it is per voice and per hit for free and can never move
        mid-note. The formula is anchored at vel 1.0 on purpose: a full
        hit (or an unpatched ``vel``) is ``color + 0.0``, which IS
        ``color`` in IEEE, so the knob cannot change the sound of a patch
        with no velocity source -- ``vel_color`` only ever DULLS a soft
        hit (and brightens a hit above 1.0, to the clamp). Only the
        exciter's one-pole sees it; the loop -- decay, damping, tuning --
        stays untouched, so a soft hit is quieter AND duller and still
        rings down exactly the same way.

        ``vel_position`` (2026-09-22, love pass): the pick's PLACE follows
        the velocity too -- a gentle finger-pluck lands nearer the middle
        of the string than a hard plectrum stroke near the bridge. The
        effective pick position of a hit is

            clamp(position + vel_position * (1 - vel) * (0.5 - position), 0, 1)

        -- an interpolation from ``position`` towards 0.5, by
        ``vel_position * (1 - vel)`` of the way. 0.5 is where the comb is
        dullest (``d = n/2``: the first null lands on the SECOND harmonic,
        so the fundamental dominates and the even partials go); ``position``
        small is near the bridge (the first null is at ``f0/position``, far
        up -- bright and nasal). Latched at the edge from the SAME velocity
        the burst rides, like ``vel_color``, so it is per voice and per hit.
        Anchored at vel 1.0 the same way -- ``(1 - 1.0)`` is ``0.0`` and
        ``position + 0.0 * x`` IS ``position`` in IEEE -- so a full hit, or
        an unpatched ``vel``, is exactly ``position`` whatever the knob
        says. A hit ABOVE velocity 1 slides the other way, towards the
        bridge, to the clamp. ``position`` 0 is the documented OFF for the
        comb and stays off: the knob moves a pick, it does not fit one.

        ``carry`` (2026-09-22, love pass; default ON since 2026-09-24):
        whether a hit
        CARRIES the loop's allpass (fractional-delay) state instead of
        clearing it. Measured, at C4, ``decay`` 3, ``damping`` 0, a
        re-pluck 0.5 s in:

          * the clear is a step of **51.3% of the ring's amplitude** at the
            same pitch, 51.4% into G4, 51.5% into C5 -- measured with a
            ZERO burst, so it is the artifact alone. The fraction depends
            on WHERE in the waveform the hit lands (the clear throws away
            one sample of loop state, and a sample is not an envelope):
            another re-pluck instant measures 28%, the velocity pass
            measured 7-18% on its own hits. It is never small;
          * carrying the state, that step is **exactly 0.000%** -- and the
            relock a hit performs is then bit-identical to the block-mean
            pitch follow the string does anyway, at any pitch;
          * carried, a re-pluck is EXACT superposition: two hits minus one
            hit equals the second hit alone to 3e-08 (one float32 ulp),
            against 51% with the clear. The module has always CLAIMED that
            ("the loop is linear, so plucks superpose") -- the clear is the
            one thing that made it false;
          * tuning is untouched either way (G4 -0.95 ct, C5 +0.84 ct,
            identical to the milli-hertz), and so is the peak; 200 rapid
            re-plucks over four octaves stay finite and bounded both ways.

        So the clear buys nothing and costs half the ring. It shipped as
        the default for two days (turning it off changes the sound of
        every patch that re-plucks a ringing string, so it waited for
        Matthew's go-ahead); since 2026-09-24 ``carry`` defaults True
        and False is the old behaviour, kept bit-exact.
        """
        from ..numpy_backend import _pluck_exciter  # lazy: it imports this module

        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        trig = self._input_buffer(
            patch, buffers, module.id, "trigger", collapse=False
        )
        if pitch is None and trig is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)
        # The velocity bus goes down whole: it is only ever READ at a
        # rising-edge sample, and each voice picks its own row below.
        vel = self._input_buffer(
            patch, buffers, module.id, "vel", collapse=False
        )

        voiced = (pitch is not None and pitch.ndim == 2) or (
            trig is not None and trig.ndim == 2
        )
        V = 1
        for sig in (pitch, trig):
            if sig is not None and sig.ndim == 2:
                V = max(V, sig.shape[0])

        def row(sig, v):
            if sig is None:
                return None
            if sig.ndim == 2:
                return sig[v] if v < sig.shape[0] else sig[0]
            return sig

        try:
            decay = float(module.params.get("decay", 2.0))
        except (TypeError, ValueError):
            decay = 2.0
        decay = min(30.0, max(0.1, decay))
        damping = min(1.0, max(0.0, float(module.params.get("damping", 0.5))))
        color = min(1.0, max(0.0, float(module.params.get("color", 0.7))))
        # vel_color: how far a soft hit's burst darkens from ``color``
        # (0 = off, the shipped sound). Applied per hit at the edge below,
        # beside the latched velocity it rides on.
        vel_color = min(
            1.0, max(0.0, float(module.params.get("vel_color", 0.0)))
        )
        # vel_position: how far a soft hit's pick slides towards the
        # middle of the string (0 = off, the shipped sound). Applied per
        # hit at the edge, from the same latched velocity.
        vel_position = min(
            1.0, max(0.0, float(module.params.get("vel_position", 0.0)))
        )
        # carry: keep the loop's allpass state through a hit (default
        # True, since 2026-09-24). False is the old behaviour: every
        # hit clears it, a 51%-of-the-ring step (docstring).
        carry = bool(module.params.get("carry", True))
        position = min(1.0, max(0.0, float(module.params.get("position", 0.2))))
        level = float(module.params.get("level", 0.5))

        sr = float(self.sample_rate)
        maxlen = int(sr / self._PLUCK_MIN_F0) + 4

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V or st.get("maxlen") != maxlen:
            st.clear()
            st.update(
                {
                    "V": V,
                    "maxlen": maxlen,
                    "ring": np.zeros((V, maxlen), dtype=np.float64),
                    "widx": np.zeros(V, dtype=np.int64),
                    "n_int": np.full(V, 100, dtype=np.int64),
                    "ap_c": np.zeros(V, dtype=np.float64),
                    "ap_z": np.zeros(V, dtype=np.float64),
                    "g": np.ones(V, dtype=np.float64),
                    "prev_trig": np.zeros(V, dtype=bool),
                    "hits": np.zeros(V, dtype=np.int64),
                    "active": np.zeros(V, dtype=bool),
                }
            )
        ring = st["ring"]

        a0 = 1.0 - damping / 2.0  # damping one-zero: a0 + a1·z⁻¹
        a1 = damping / 2.0

        def set_coeffs(v: int, cv_value: float) -> None:
            f0 = self._PLUCK_C4 * (2.0 ** float(cv_value))
            f0 = min(sr / 4.0, max(self._PLUCK_MIN_F0, f0))
            n_target = sr / f0
            n_eff = n_target - damping / 2.0  # compensate the filter delay
            n_int = int(n_eff - 0.1)
            frac = n_eff - n_int  # in [0.1, 1.1)
            if n_int < 2:
                n_int, frac = 2, max(0.1, n_eff - 2)
            st["n_int"][v] = n_int
            st["ap_c"][v] = (1.0 - frac) / (1.0 + frac)
            st["g"][v] = 10.0 ** (-3.0 * n_target / (sr * decay))

        out = np.zeros((V, frames), dtype=np.float64)
        gate_high = self._GATE_HIGH

        for v in range(V):
            p_row = row(pitch, v)
            t_row = row(trig, v)
            # This voice's velocity: its own row of a (V, F) bus (or the
            # shared mono bus) when the pluck is voiced; the whole buffer
            # when the pluck is mono, collapsing at the read to the
            # loudest voice at the edge (a velocity bus carries 0 on idle
            # slots, so the max is the key that was struck). None when
            # unpatched -- the old code path, untouched.
            if vel is None:
                v_row = None
            elif voiced:
                v_row = row(vel, v)
            else:
                v_row = vel

            if t_row is not None:
                gt = t_row > gate_high
                prev = np.empty_like(gt)
                prev[0] = bool(st["prev_trig"][v])
                prev[1:] = gt[:-1]
                edges = np.flatnonzero(gt & ~prev).tolist()
                st["prev_trig"][v] = bool(gt[-1])
            else:
                edges = []

            if not edges and not st["active"][v]:
                continue  # silent string: free

            if st["active"][v] and not edges:
                # Follow the block-mean pitch (glide/vibrato, block rate).
                set_coeffs(v, float(np.mean(p_row)) if p_row is not None else 0.0)

            # Segment the block at trigger edges: advance up to each edge
            # with the old state, then pluck and continue.
            bounds = edges + [frames]
            seg_start = 0
            for i, seg_end in enumerate(bounds):
                if seg_end > seg_start:
                    self._pluck_advance(
                        st, v, ring, out[v], seg_start, seg_end, a0, a1
                    )
                if i < len(edges):
                    e = edges[i]
                    if v_row is not None:
                        # Velocity: read AT the edge sample, latched into
                        # this hit. Non-positive = a silent hit: the
                        # string keeps ringing exactly as it was (no
                        # burst, no relock, no allpass clear -- see the
                        # docstring for why the clear alone would tick);
                        # only the hit counter advances (a hit is a hit).
                        scale = max(0.0, self._drum_edge_value(v_row, e, 1.0))
                        if scale <= 0.0:
                            st["hits"][v] += 1
                            seg_start = e
                            continue
                    cv_at = float(p_row[e]) if p_row is not None else 0.0
                    set_coeffs(v, cv_at)
                    n_int = int(st["n_int"][v])
                    rng = np.random.default_rng(
                        (self._PLUCK_SEED, module.id, v, int(st["hits"][v]))
                    )
                    st["hits"][v] += 1
                    hit_color = color
                    if v_row is not None and vel_color > 0.0:
                        # The pick's spectrum follows the latched
                        # velocity: a soft hit's burst is lowpassed
                        # harder, in proportion. Anchored at 1.0 so a
                        # full hit is ``color + 0.0`` -- bit-exactly
                        # ``color`` -- and the knob is inert without a
                        # velocity source. Clamped like ``color`` itself.
                        hit_color = min(
                            1.0, max(0.0, color + vel_color * (scale - 1.0))
                        )
                    hit_position = position
                    if (
                        v_row is not None
                        and vel_position > 0.0
                        and position > 0.0
                    ):
                        # The pick's PLACE follows the latched velocity: a
                        # soft hit lands nearer the middle of the string,
                        # where the comb is dullest and the fundamental
                        # strongest -- a finger, not a plectrum by the
                        # bridge. Anchored at 1.0 like ``vel_color``, so a
                        # full hit is ``position + 0.0 * x`` -- bit-exactly
                        # ``position`` -- and the knob is inert without a
                        # velocity source. ``position`` 0 is the comb's
                        # documented OFF and stays off: this knob moves a
                        # pick, it does not fit one.
                        hit_position = min(
                            1.0,
                            max(
                                0.0,
                                position
                                + vel_position * (1.0 - scale) * (0.5 - position),
                            ),
                        )
                    burst = _pluck_exciter(n_int, hit_color, hit_position, rng)
                    if v_row is not None:
                        # The burst scales; the loop does not, so a soft
                        # hit rings down the same way.
                        burst *= scale
                    # Add the burst into the last n_int ring positions so
                    # the loop reads it starting at the edge sample.
                    w = int(st["widx"][v])
                    start = (w - n_int) % st["maxlen"]
                    first = min(n_int, st["maxlen"] - start)
                    ring[v, start : start + first] += burst[:first]
                    if n_int > first:
                        ring[v, : n_int - first] += burst[first:]
                    if not carry:
                        # The allpass state is the one thing a hit does
                        # NOT superpose. Measured at 51% of the ring
                        # (docstring); ``carry`` keeps it, and then a
                        # re-pluck is exact superposition.
                        st["ap_z"][v] = 0.0
                    st["active"][v] = True
                    seg_start = e
            # Early-out bookkeeping: a decayed string goes fully silent.
            if st["active"][v] and not edges:
                if float(np.max(np.abs(out[v]))) < self._PLUCK_SILENCE:
                    st["active"][v] = False
                    ring[v, :] = 0.0
                    out[v, :] = 0.0
                    # Zero the allpass state with the ring, so a fresh
                    # pluck on a dead string is a fresh pluck under
                    # ``carry`` too. Render-neutral with ``carry`` off:
                    # nothing reads ``ap_z`` while a voice is inactive,
                    # and the next hit clears it anyway.
                    st["ap_z"][v] = 0.0

        out *= level
        result = out if voiced else out[0]
        return result.astype(np.float32)

    def _pluck_advance(
        self, st, v: int, ring, out_row, start: int, end: int, a0, a1
    ) -> None:
        """Advance one voice's string loop over out_row[start:end]."""
        if not st["active"][v]:
            return  # nothing ringing; output stays zero
        maxlen = int(st["maxlen"])
        n_int = int(st["n_int"][v])
        c = float(st["ap_c"][v])
        g = float(st["g"][v])
        w = int(st["widx"][v])
        z = float(st["ap_z"][v])
        row = ring[v]
        pos = start
        while pos < end:
            chunk = min(end - pos, n_int)
            # Delayed taps (all strictly before this chunk's writes).
            i0 = (np.arange(chunk) + (w - n_int)) % maxlen
            x0 = row[i0]
            x1 = row[(i0 - 1) % maxlen]
            vsig = a0 * x0 + a1 * x1
            y, zf = lfilter([c, 1.0], [1.0, c], vsig, zi=np.array([z]))
            z = float(zf[0])
            y *= g
            i_w = (np.arange(chunk) + w) % maxlen
            row[i_w] = y
            out_row[pos : pos + chunk] = y
            w = (w + chunk) % maxlen
            pos += chunk
        st["widx"][v] = w
        st["ap_z"][v] = z

    # ----- Bowed string rendering ------------------------------------------

    def _render_bowed(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Bowed-string waveguide (see modules/bowed.py for the contract).

        Per voice: two delay lines (bridge side ``beta x L``, nut side the
        rest) meeting at the bow. Each sample the string velocity at the
        bow is ``-lowpass(bridge_out) - nut_out``; the bow injects
        ``dv x table(dv)`` (STK's friction table, ``dv`` = bow velocity
        minus string velocity) into both halves. The loop is advanced in
        vectorized CHUNKS no longer than the shorter delay (the pluck
        precedent, now with a nonlinearity inside the loop -- elementwise,
        so it chunks the same way); the bridge one-pole is one ``lfilter``
        per chunk with carried ``zi``. Delay lines are slice buffers with
        history compacted once per block (no per-chunk modulo).

        Tuning: bridge + nut delay = ``sr/f0 - tau(f0)`` where tau is the
        one-pole's exact phase delay at f0; the fraction rides the bridge
        side as a linear-interpolation read (the nut side is integer).
        The bow envelope is an integer-count ramp per voice (attack from
        the gate's rising edge, release from the falling edge, a re-bow
        picking up from the current level), so renders are block-size
        independent at constant pitch and never click. Pressure and
        velocity CVs are per-sample arrays sliced per chunk. Pitch is
        read at the block's last rising edge if there is one, else the
        block mean (glides at block rate). Body: five constant-peak
        bandpasses in parallel, mixed by ``body``. A lifted string whose
        output has decayed early-outs to exact zeros.
        """
        pitch = self._input_buffer(patch, buffers, module.id, "pitch_cv", collapse=False)
        gate = self._input_buffer(patch, buffers, module.id, "gate", collapse=False)
        if pitch is None and gate is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)
        p_cv = self._input_buffer(patch, buffers, module.id, "pressure_cv")
        v_cv = self._input_buffer(patch, buffers, module.id, "velocity_cv")

        voiced = (pitch is not None and pitch.ndim == 2) or (
            gate is not None and gate.ndim == 2
        )
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

        def fparam(name, default, lo, hi):
            try:
                x = float(module.params.get(name, default))
            except (TypeError, ValueError):
                x = default
            return min(hi, max(lo, x))

        pressure = fparam("pressure", 0.5, 0.0, 1.0)
        velocity = fparam("velocity", 0.6, 0.0, 1.0)
        beta = fparam("position", 0.127, 0.05, 0.5)
        attack = fparam("attack", 0.05, 0.001, 10.0)
        release = fparam("release", 0.15, 0.001, 10.0)
        damping = fparam("damping", 0.5, 0.0, 1.0)
        body = fparam("body", 0.5, 0.0, 1.0)
        depth = fparam("cv_depth", 1.0, -10.0, 10.0)
        level = fparam("level", 0.5, 0.0, 1.0)

        sr = float(self.sample_rate)
        maxlen = int(sr / self._BOW_MIN_F0) + 8
        n_body = len(self._BOW_BODY)

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V or st.get("maxlen") != maxlen or st.get("block") != frames:
            st.clear()
            buflen = maxlen + frames + 4
            st.update({
                "V": V, "maxlen": maxlen, "block": frames, "buflen": buflen,
                "bridge": np.zeros((V, buflen), dtype=np.float64),
                "neck": np.zeros((V, buflen), dtype=np.float64),
                "w": np.full(V, maxlen, dtype=np.int64),
                "lp_z": np.zeros(V, dtype=np.float64),
                "body_z": np.zeros((V, n_body, 2), dtype=np.float64),
                "prev_gate": np.zeros(V, dtype=bool),
                "on_count": np.zeros(V, dtype=np.int64),
                "off_count": np.zeros(V, dtype=np.int64),
                "env_off": np.zeros(V, dtype=np.float64),
                "active": np.zeros(V, dtype=bool),
            })
        bridge = st["bridge"]
        neck = st["neck"]
        buflen = int(st["buflen"])

        pole = self._BOW_POLE_LO + (self._BOW_POLE_HI - self._BOW_POLE_LO) * damping
        lp_b = np.array([self._BOW_LOSS * (1.0 - pole)])
        lp_a = np.array([1.0, -pole])
        att_n = max(1, int(round(attack * sr)))
        rel_n = max(1, int(round(release * sr)))
        gate_high = self._GATE_HIGH

        # Per-sample bow hands (mono, shared by every voice).
        if p_cv is not None:
            slope = self._BOW_SLOPE_MAX - self._BOW_SLOPE_RANGE * np.clip(
                pressure + depth * p_cv.astype(np.float64), 0.0, 1.0)
        else:
            slope = np.full(frames, self._BOW_SLOPE_MAX - self._BOW_SLOPE_RANGE * pressure)
        if v_cv is not None:
            maxvel = self._BOW_VEL_MIN + self._BOW_VEL_RANGE * np.clip(
                velocity + depth * v_cv.astype(np.float64), 0.0, 1.0)
        else:
            maxvel = np.full(frames, self._BOW_VEL_MIN + self._BOW_VEL_RANGE * velocity)

        body_coefs = None
        if body > 0.0:
            body_coefs = [
                (self._bow_bp_coeffs(f, q), g) for f, q, g in self._BOW_BODY
            ]

        out = np.zeros((V, frames), dtype=np.float64)
        for v in range(V):
            p_row = row(pitch, v)
            g_row = row(gate, v)
            if g_row is not None:
                gt = g_row > gate_high
            else:
                gt = np.zeros(frames, dtype=bool)
            prev = bool(st["prev_gate"][v])
            rising = np.flatnonzero(gt & ~np.concatenate(([prev], gt[:-1])))
            if rising.size:
                st["active"][v] = True
            if not st["active"][v]:
                st["prev_gate"][v] = bool(gt[-1])
                continue  # a lifted, silent string is free

            # --- the bow envelope: integer-count ramps, segmented at edges
            env, on_count, off_count, env_off = self._gate_ramp_env(
                gt, prev, int(st["on_count"][v]), int(st["off_count"][v]),
                float(st["env_off"][v]), att_n, rel_n)
            st["on_count"][v] = on_count
            st["off_count"][v] = off_count
            st["env_off"][v] = env_off
            st["prev_gate"][v] = bool(gt[-1])
            bow_vel = maxvel * env

            # --- pitch -> delays (per block)
            if p_row is None:
                cv_val = 0.0
            elif rising.size:
                cv_val = float(p_row[rising[-1]])
            else:
                cv_val = self._finite_mean(p_row)
            f0 = self._BOW_C4 * self._pow2_clipped(cv_val)
            f0 = min(self._BOW_MAX_F0, max(self._BOW_MIN_F0, f0))
            w0 = 2.0 * np.pi * f0 / sr
            tau = float(np.arctan2(pole * np.sin(w0), 1.0 - pole * np.cos(w0)) / w0)
            L = sr / f0 - tau
            Ln = max(2, int(round((1.0 - beta) * L)))
            Lb = max(2.0, L - Ln)
            Db_i = int(Lb)
            Db_f = Lb - Db_i
            chunk_max = max(1, min(Db_i, Ln))

            # --- compact the slice buffers once per block
            w = int(st["w"][v])
            if w + frames > buflen:
                bridge[v, :maxlen] = bridge[v, w - maxlen:w]
                neck[v, :maxlen] = neck[v, w - maxlen:w]
                w = maxlen
            b_row = bridge[v]
            n_row = neck[v]
            z = np.array([st["lp_z"][v]])
            o_row = out[v]

            pos = 0
            while pos < frames:
                n = min(chunk_max, frames - pos)
                rb = w - Db_i
                b0 = b_row[rb:rb + n]
                bridge_out = b0 + Db_f * (b_row[rb - 1:rb - 1 + n] - b0)
                nut_out = n_row[w - Ln:w - Ln + n]
                filt, z = lfilter(lp_b, lp_a, bridge_out, zi=z)
                vd = bow_vel[pos:pos + n] + filt + nut_out   # bow - (-filt - nut)
                tbl = np.abs(vd * slope[pos:pos + n])
                tbl += 0.75
                np.power(tbl, -4.0, out=tbl)
                np.minimum(tbl, 1.0, out=tbl)
                new_vel = vd * tbl
                n_row[w:w + n] = new_vel - filt
                b_row[w:w + n] = new_vel - nut_out
                o_row[pos:pos + n] = bridge_out
                w += n
                pos += n
            st["w"][v] = w
            st["lp_z"][v] = float(z[0])

            # --- body resonances
            if body_coefs is not None:
                wet = np.zeros(frames, dtype=np.float64)
                for i, ((bc, ac), g) in enumerate(body_coefs):
                    y, zf = lfilter(bc, ac, o_row, zi=st["body_z"][v, i])
                    st["body_z"][v, i] = zf
                    wet += g * y
                o_row *= (1.0 - body)
                o_row += body * self._BOW_BODY_GAIN * wet

            # --- early-out: bow lifted, envelope gone, string decayed
            if not gt[-1] and env[-1] <= 0.0 and float(np.max(np.abs(o_row))) < self._BOW_SILENCE:
                st["active"][v] = False
                bridge[v, :] = 0.0
                neck[v, :] = 0.0
                st["lp_z"][v] = 0.0
                st["body_z"][v, :, :] = 0.0
                st["on_count"][v] = 0
                st["off_count"][v] = 0
                st["env_off"][v] = 0.0
                o_row[:] = 0.0

        out *= level * self._BOW_OUT_GAIN
        result = out if voiced else out[0]
        return result.astype(np.float32)

    def _bow_bp_coeffs(self, freq: float, q: float):
        """One RBJ constant-0dB-peak bandpass as (b, a) arrays."""
        w0 = 2.0 * np.pi * min(freq, 0.45 * self.sample_rate) / self.sample_rate
        alpha = np.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        b = np.array([alpha / a0, 0.0, -alpha / a0])
        a = np.array([1.0, -2.0 * np.cos(w0) / a0, (1.0 - alpha) / a0])
        return b, a

    # ----- Wind rendering ------------------------------------------------------

    def _render_wind(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Blown pipe, flute or reed (see modules/wind.py for the contract).

        Same machinery as the bowed string: per voice, slice-buffer delay
        lines compacted once per block, the loop advanced in vectorized
        chunks no longer than the shortest delay, an integer-count breath
        ramp from the gate edges (``_gate_ramp_env``), pitch read at the
        block's last rising edge or the block mean, early-out when the
        breath is off and the pipe has gone quiet.

        ``flute`` (STK Flute): ``temp = dcblock(-lowpass(bore_out))``;
        ``jet.write(breath - 0.5 temp)``; ``bore.write(jettable(jet_out)
        + 0.5 temp)`` with ``jettable(x) = clip(x(x^2 - 1))``; the bore is
        1.5 periods (the overblown register), the jet 0.32 of it; chunks
        <= the jet delay. ``reed`` (STK Clarinet mouthpiece):
        ``dp = -0.95 lowpass(line_out) - breath``; ``line.write(breath +
        dp * clip(0.7 - 0.3 dp))``; one round-trip delay = the period;
        chunks <= it. Both: the delay is ``sr/f0``-based minus the loss
        filter's exact phase delay at f0, fraction as a linear-interp
        read. Breath = ``maxp(breath[n]) * env[n] * (1 + noise * white)``
        with the white noise a per-(voice, note) seeded stream drawn per
        segment, so a note renders identically at any block size. The
        output is DC-blocked (the reed's line carries the breath pressure).
        """
        from ...modules.wind import WIND_MODELS

        pitch = self._input_buffer(patch, buffers, module.id, "pitch_cv", collapse=False)
        gate = self._input_buffer(patch, buffers, module.id, "gate", collapse=False)
        if pitch is None and gate is None:
            self._state.pop(module.id, None)
            return np.zeros(frames, dtype=np.float32)
        b_cv = self._input_buffer(patch, buffers, module.id, "breath_cv")

        voiced = (pitch is not None and pitch.ndim == 2) or (
            gate is not None and gate.ndim == 2
        )
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

        def fparam(name, default, lo, hi):
            try:
                x = float(module.params.get(name, default))
            except (TypeError, ValueError):
                x = default
            return min(hi, max(lo, x))

        model = str(module.params.get("model", "flute"))
        if model not in WIND_MODELS:
            model = "flute"
        breath = fparam("breath", 0.5, 0.0, 1.0)
        noise = fparam("noise", 0.15, 0.0, 1.0)
        attack = fparam("attack", 0.04, 0.001, 10.0)
        release = fparam("release", 0.1, 0.001, 10.0)
        damping = fparam("damping", 0.5, 0.0, 1.0)
        depth = fparam("cv_depth", 1.0, -10.0, 10.0)
        level = fparam("level", 0.5, 0.0, 1.0)
        try:
            seed = max(0, int(module.params.get("seed", 1)))
        except (TypeError, ValueError):
            seed = 1

        sr = float(self.sample_rate)
        maxlen = int(self._WIND_FLUTE_BORE * sr / min(self._WIND_MIN_F0.values())) + 8

        st = self._state.setdefault(module.id, {})
        if (st.get("V") != V or st.get("maxlen") != maxlen or st.get("block") != frames
                or st.get("model") != model):
            st.clear()
            buflen = maxlen + frames + 4
            st.update({
                "V": V, "maxlen": maxlen, "block": frames, "buflen": buflen, "model": model,
                "bore": np.zeros((V, buflen), dtype=np.float64),
                "jet": np.zeros((V, buflen), dtype=np.float64),
                "w": np.full(V, maxlen, dtype=np.int64),
                "lp_z": np.zeros(V, dtype=np.float64),
                "dc_z": np.zeros(V, dtype=np.float64),
                "odc_z": np.zeros(V, dtype=np.float64),
                "prev_gate": np.zeros(V, dtype=bool),
                "on_count": np.zeros(V, dtype=np.int64),
                "off_count": np.zeros(V, dtype=np.int64),
                "env_off": np.zeros(V, dtype=np.float64),
                "hits": np.zeros(V, dtype=np.int64),
                "rng": [None] * V,
                "active": np.zeros(V, dtype=bool),
            })
        bore = st["bore"]
        jet = st["jet"]
        buflen = int(st["buflen"])

        p_lo, p_hi = self._WIND_POLE[model]
        pole = p_lo + (p_hi - p_lo) * damping
        att_n = max(1, int(round(attack * sr)))
        rel_n = max(1, int(round(release * sr)))
        gate_high = self._GATE_HIGH
        pr_lo, pr_range = self._WIND_PRESSURE[model]
        if b_cv is not None:
            maxp = pr_lo + pr_range * np.clip(breath + depth * b_cv.astype(np.float64), 0.0, 1.0)
        else:
            maxp = np.full(frames, pr_lo + pr_range * breath)
        f_lo = self._WIND_MIN_F0[model]
        dc_b = np.array([1.0, -1.0])
        dc_a = np.array([1.0, -self._WIND_DC_POLE])
        if model == "flute":
            lp_b = np.array([-(1.0 - pole)])
        else:
            lp_b = np.array([1.0 - pole])
        lp_a = np.array([1.0, -pole])

        out = np.zeros((V, frames), dtype=np.float64)
        for v in range(V):
            p_row = row(pitch, v)
            g_row = row(gate, v)
            gt = (g_row > gate_high) if g_row is not None else np.zeros(frames, dtype=bool)
            prev = bool(st["prev_gate"][v])
            rising = np.flatnonzero(gt & ~np.concatenate(([prev], gt[:-1])))
            if rising.size:
                st["active"][v] = True
            if not st["active"][v]:
                st["prev_gate"][v] = bool(gt[-1])
                continue

            env, on_count, off_count, env_off = self._gate_ramp_env(
                gt, prev, int(st["on_count"][v]), int(st["off_count"][v]),
                float(st["env_off"][v]), att_n, rel_n)
            st["on_count"][v] = on_count
            st["off_count"][v] = off_count
            st["env_off"][v] = env_off
            st["prev_gate"][v] = bool(gt[-1])

            # Breath noise: a fresh seeded stream per note, drawn per
            # segment between rising edges (same consumption at any block
            # size). Silence before the first note draws nothing.
            white = np.zeros(frames, dtype=np.float64)
            seg_start = 0
            for e in rising.tolist() + [frames]:
                if e > seg_start and st["rng"][v] is not None:
                    white[seg_start:e] = st["rng"][v].uniform(-1.0, 1.0, e - seg_start)
                if e < frames:
                    st["hits"][v] += 1
                    st["rng"][v] = np.random.default_rng(
                        (self._WIND_SEED, seed, module.id, v, int(st["hits"][v])))
                seg_start = e
            breath_p = maxp * env * (1.0 + noise * white)

            if p_row is None:
                cv_val = 0.0
            elif rising.size:
                cv_val = float(p_row[rising[-1]])
            else:
                cv_val = self._finite_mean(p_row)
            f0 = self._WIND_C4 * self._pow2_clipped(cv_val)
            f0 = min(self._WIND_MAX_F0, max(f_lo, f0))
            w0 = 2.0 * np.pi * f0 / sr
            tau = float(np.arctan2(pole * np.sin(w0), 1.0 - pole * np.cos(w0)) / w0)
            if model == "flute":
                L = self._WIND_FLUTE_BORE * sr / f0 - tau
                L = max(4.0, L)
                Lj = max(2, int(round(self._WIND_FLUTE_JET * L)))
            else:
                L = max(4.0, sr / f0 - tau)
                Lj = 0
            L_i = int(L)
            L_f = L - L_i
            chunk_max = max(1, min(L_i, Lj) if Lj else L_i)

            w = int(st["w"][v])
            if w + frames > buflen:
                bore[v, :maxlen] = bore[v, w - maxlen:w]
                jet[v, :maxlen] = jet[v, w - maxlen:w]
                w = maxlen
            b_row = bore[v]
            j_row = jet[v]
            z = np.array([st["lp_z"][v]])
            zd = np.array([st["dc_z"][v]])
            o_row = out[v]
            refl = self._WIND_FLUTE_REFL
            r_refl = self._WIND_REED_REFL
            r_off = self._WIND_REED_OFFSET
            r_slope = self._WIND_REED_SLOPE

            pos = 0
            while pos < frames:
                n = min(chunk_max, frames - pos)
                rb = w - L_i
                b0 = b_row[rb:rb + n]
                line_out = b0 + L_f * (b_row[rb - 1:rb - 1 + n] - b0)
                filt, z = lfilter(lp_b, lp_a, line_out, zi=z)
                bp = breath_p[pos:pos + n]
                if model == "flute":
                    temp, zd = lfilter(dc_b, dc_a, filt, zi=zd)
                    j_row[w:w + n] = bp - refl * temp
                    jd = j_row[w - Lj:w - Lj + n]
                    jt = jd * (jd * jd - 1.0)
                    np.clip(jt, -1.0, 1.0, out=jt)
                    jt += refl * temp
                    b_row[w:w + n] = jt
                else:
                    dp = -r_refl * filt - bp
                    rt = r_off - r_slope * dp
                    np.clip(rt, -1.0, 1.0, out=rt)
                    b_row[w:w + n] = bp + dp * rt
                o_row[pos:pos + n] = b_row[w:w + n]
                w += n
                pos += n
            st["w"][v] = w
            st["lp_z"][v] = float(z[0])
            st["dc_z"][v] = float(zd[0])

            # Output DC block (the reed's line carries the breath pressure).
            y, zo = lfilter(dc_b, dc_a, o_row, zi=np.array([st["odc_z"][v]]))
            st["odc_z"][v] = float(zo[0])
            o_row[:] = y * self._WIND_MODEL_GAIN[model]

            if not gt[-1] and env[-1] <= 0.0 and float(np.max(np.abs(o_row))) < self._WIND_SILENCE:
                st["active"][v] = False
                bore[v, :] = 0.0
                jet[v, :] = 0.0
                st["lp_z"][v] = 0.0
                st["dc_z"][v] = 0.0
                st["odc_z"][v] = 0.0
                st["on_count"][v] = 0
                st["off_count"][v] = 0
                st["env_off"][v] = 0.0
                o_row[:] = 0.0

        out *= level * self._WIND_OUT_GAIN
        result = out if voiced else out[0]
        return result.astype(np.float32)

    # ----- Drum voices (kick / snare / hat) --------------------------------

    _DRUM_FADE_SECONDS = 0.002  # retrigger/choke declick ramp
    _DRUM_SEED = 0x44524D53  # "DRMS"

    def _drum_edges(self, st, key: str, gate) -> list[int]:
        """Rising-edge sample indices for one trigger jack, prev carried."""
        if gate is None:
            return []
        g = gate > self._GATE_HIGH
        prev = np.empty_like(g)
        prev[0] = bool(st.get("prev_" + key, False))
        prev[1:] = g[:-1]
        st["prev_" + key] = bool(g[-1])
        return np.flatnonzero(g & ~prev).tolist()

    def _drum_fade_slot(self, st, slot: str) -> None:
        """Start the 2 ms fade-out on every active play in ``slot``."""
        fade_total = max(1, int(self.sample_rate * self._DRUM_FADE_SECONDS))
        for play in st["plays"]:
            if play["slot"] == slot and play["fade"] is None:
                play["fade"] = [fade_total, fade_total]

    def _drum_advance(self, st, out: np.ndarray, start: int, end: int) -> None:
        """Mix every active play into ``out[start:end]``; prune finished."""
        span = end - start
        if span <= 0:
            return
        alive = []
        for play in st["plays"]:
            buf, pos = play["buf"], play["pos"]
            n = min(span, len(buf) - pos)
            if n > 0:
                seg = buf[pos : pos + n]
                if play["fade"] is not None:
                    rem, total = play["fade"]
                    gains = np.clip(
                        (rem - np.arange(n, dtype=np.float64)) / total, 0.0, 1.0
                    )
                    seg = seg * gains
                    play["fade"][0] = rem - n
                out[start : start + n] += seg
                play["pos"] = pos + n
            done = play["pos"] >= len(play["buf"]) or (
                play["fade"] is not None and play["fade"][0] <= 0
            )
            if not done:
                alive.append(play)
        st["plays"] = alive

    def _render_drum(self, module, frames: int, buffers, patch, make_hits):
        """Shared engine: precomputed hit buffers + declick crossfades.

        ``make_hits(edge_lists)`` receives the per-jack edge index lists
        and yields ``(edge_sample, slot, buffer)`` hits in time order.
        On each hit any play in the same slot fades over ~2 ms while the
        new buffer starts — the retrigger declick (and the hat's choke).
        Buffers are synthesized whole at the edge (seeded per hit), so
        output is deterministic and block-size independent by
        construction.
        """
        st = self._state.setdefault(
            module.id, {"plays": [], "hits": 0}
        )
        out = np.zeros(frames, dtype=np.float64)
        hits = sorted(make_hits(st), key=lambda h: h[0])
        pos = 0
        for e, slot, buf in hits:
            self._drum_advance(st, out, pos, e)
            self._drum_fade_slot(st, slot)
            st["plays"].append(
                {"buf": buf, "pos": 0, "fade": None, "slot": slot}
            )
            pos = e
        self._drum_advance(st, out, pos, frames)
        level = float(module.params.get("level", 0.7))
        return (out * level).astype(np.float32)

    def _render_kick(self, module, frames: int, buffers, patch) -> np.ndarray:
        from ..numpy_backend import _kick_hit  # lazy: it imports this module

        trig = self._input_buffer(patch, buffers, module.id, "trigger")
        vel = self._input_buffer(patch, buffers, module.id, "vel", collapse=False)
        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )
        p = module.params

        def make_hits(st):
            hits = []
            for e in self._drum_edges(st, "t", trig):
                rng = np.random.default_rng(
                    (self._DRUM_SEED, module.id, int(st["hits"]))
                )
                st["hits"] += 1
                # `vel` and `pitch_cv` are read AT THE EDGE and latched
                # into this hit (the sampler idiom): a hit is a hit.
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                cv = self._drum_edge_value(pitch, e, 0.0)
                tune = min(12.0, max(-12.0, float(p.get("tune", 0.0))))
                buf = _kick_hit(
                    self.sample_rate,
                    min(400.0, max(100.0, float(p.get("freq_start", 180.0)))),
                    min(80.0, max(30.0, float(p.get("freq_end", 50.0)))),
                    min(0.2, max(0.005, float(p.get("bend", 40.0)) * 1e-3)),
                    min(1.5, max(0.05, float(p.get("decay", 350.0)) * 1e-3)),
                    min(1.0, max(0.0, float(p.get("click", 0.3)))),
                    min(1.0, max(0.0, float(p.get("drive", 0.0)))),
                    tune + 12.0 * cv,
                    rng,
                    vel=velocity,
                )
                hits.append((int(e), "main", buf))
            return hits

        return self._render_drum(module, frames, buffers, patch, make_hits)

    def _render_snare(self, module, frames: int, buffers, patch) -> np.ndarray:
        from ..numpy_backend import _snare_hit  # lazy: it imports this module

        trig = self._input_buffer(patch, buffers, module.id, "trigger")
        vel = self._input_buffer(patch, buffers, module.id, "vel", collapse=False)
        p = module.params

        def make_hits(st):
            hits = []
            for e in self._drum_edges(st, "t", trig):
                rng = np.random.default_rng(
                    (self._DRUM_SEED, module.id, int(st["hits"]))
                )
                st["hits"] += 1
                buf = _snare_hit(
                    self.sample_rate,
                    min(0.5, max(0.02, float(p.get("tone_decay", 120.0)) * 1e-3)),
                    min(1.0, max(0.02, float(p.get("noise_decay", 200.0)) * 1e-3)),
                    min(1.0, max(0.0, float(p.get("snappy", 0.5)))),
                    min(12.0, max(-12.0, float(p.get("tune", 0.0)))),
                    rng,
                )
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                if velocity != 1.0:
                    buf = buf * velocity
                hits.append((int(e), "main", buf))
            return hits

        return self._render_drum(module, frames, buffers, patch, make_hits)

    def _render_hat(self, module, frames: int, buffers, patch) -> np.ndarray:
        from ...modules.drums import HAT_TONE_MAX, HAT_TONE_MIN
        from ..numpy_backend import _hat_hit  # lazy: it imports this module

        closed = self._input_buffer(patch, buffers, module.id, "closed_trigger")
        opened = self._input_buffer(patch, buffers, module.id, "open_trigger")
        vel = self._input_buffer(patch, buffers, module.id, "vel", collapse=False)
        p = module.params
        tune = min(12.0, max(-12.0, float(p.get("tune", 0.0))))
        try:
            tone = float(p.get("tone", 400.0))
        except (TypeError, ValueError):
            tone = 400.0
        tone = min(HAT_TONE_MAX, max(HAT_TONE_MIN, tone))

        def make_hits(st):
            hits = []
            for e in self._drum_edges(st, "c", closed):
                buf = _hat_hit(
                    self.sample_rate,
                    min(0.3, max(0.01, float(p.get("decay_closed", 60.0)) * 1e-3)),
                    tune,
                    tone,
                )
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                if velocity != 1.0:
                    buf = buf * velocity
                # A closed hit chokes BOTH slots (its own retrigger and
                # any ringing open hit) — the pedal coming down.
                hits.append((int(e), "closed", buf))
            for e in self._drum_edges(st, "o", opened):
                buf = _hat_hit(
                    self.sample_rate,
                    min(1.5, max(0.05, float(p.get("decay_open", 400.0)) * 1e-3)),
                    tune,
                    tone,
                )
                velocity = max(0.0, self._drum_edge_value(vel, e, 1.0))
                if velocity != 1.0:
                    buf = buf * velocity
                hits.append((int(e), "open", buf))
            return hits

        # Custom choke: run the shared engine but fade the open slot on
        # every closed hit too.
        st = self._state.setdefault(module.id, {"plays": [], "hits": 0})
        out = np.zeros(frames, dtype=np.float64)
        hits = sorted(make_hits(st), key=lambda h: h[0])
        pos = 0
        for e, slot, buf in hits:
            self._drum_advance(st, out, pos, e)
            self._drum_fade_slot(st, slot)
            if slot == "closed":
                self._drum_fade_slot(st, "open")
            st["plays"].append({"buf": buf, "pos": 0, "fade": None, "slot": slot})
            pos = e
        self._drum_advance(st, out, pos, frames)
        level = float(p.get("level", 0.6))
        return (out * level).astype(np.float32)

    # ----- Modal rendering -------------------------------------------------

    _MODAL_C4 = 261.6255653005986
    _MODAL_MAX_F_FRACTION = 0.45  # modes above this × sr are dropped

    def _render_modal(self, module, frames: int, buffers, patch) -> np.ndarray:
        """Two-pole resonator bank (see modules/modal.py for the contract).

        Each mode is the classic resonator ``y[n] = 2r·cosθ·y[n−1] −
        r²·y[n−2] + b₀·x[n]`` with ``r`` set from that mode's t60 and
        ``b₀ = gain·sinθ`` normalizing the STRIKE response (impulse-
        response peak ≈ gain, independent of decay — a ``(1−r)`` drive
        would normalize the ring-out integral instead and starve long
        decays to silence) — run as
        one ``lfilter`` per mode per *pitch group* (voices whose
        block-mean pitch matches share coefficients, so their rows batch
        into a single vectorized call — the slice-4 pattern; 16 unison
        voices cost the same as one). Coefficients rebuild each block
        from the block-mean pitch; ``zi`` carries per (voice, mode)
        across blocks, so the bank stays block-size independent under
        constant pitch. Quiet voices (silent excite + decayed state)
        early-out; modes past ``modes`` have their state zeroed so a
        live mode-count change can't resurrect stale ring-outs.

        Three things from the 2026-09-11 love pass, each skipped entirely
        at its default so the shipped render is untouched: ``position``
        combs the mode gains (``strike_comb``, renormalized); ``mallet``
        one-poles the excite per pitch group at ``f0·2^(6(1−m))`` with
        per-voice state (``lp``); ``spread`` accumulates a second pair of
        per-mode-panned sums for ``out_l``/``out_r`` (``mode_pans``,
        equal-power, ×√2 so centre ≡ mono). Returns a port dict.
        """
        from ...modules.modal import (
            MALLET_OCTAVES,
            MODAL_MAX_MODES,
            modal_ratios,
            mode_pans,
            strike_comb,
        )

        excite = self._input_buffer(
            patch, buffers, module.id, "excite", collapse=False
        )
        if excite is None:
            self._state.pop(module.id, None)
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}
        pitch = self._input_buffer(
            patch, buffers, module.id, "pitch_cv", collapse=False
        )

        voiced = excite.ndim == 2 or (pitch is not None and pitch.ndim == 2)
        V = 1
        for sig in (excite, pitch):
            if sig is not None and sig.ndim == 2:
                V = max(V, sig.shape[0])

        material = str(module.params.get("material", "bar"))
        modes = max(4, min(MODAL_MAX_MODES, int(module.params.get("modes", 12))))
        try:
            decay = float(module.params.get("decay", 2.0))
        except (TypeError, ValueError):
            decay = 2.0
        decay = min(30.0, max(0.1, decay))
        tilt = min(1.0, max(0.0, float(module.params.get("decay_tilt", 0.5))))
        bright = min(1.0, max(0.0, float(module.params.get("brightness", 0.5))))
        inharm = min(1.0, max(0.0, float(module.params.get("inharm", 0.0))))
        level = float(module.params.get("level", 0.5))

        def _knob(name):
            try:
                return min(1.0, max(0.0, float(module.params.get(name, 0.0))))
            except (TypeError, ValueError):
                return 0.0

        position = _knob("position")
        mallet = _knob("mallet")
        spread = _knob("spread")
        sr = float(self.sample_rate)

        st = self._state.setdefault(module.id, {})
        if st.get("V") != V:
            st.clear()
            st.update(
                {
                    "V": V,
                    "zi": np.zeros((V, MODAL_MAX_MODES, 2), dtype=np.float64),
                    "lp": np.zeros(V, dtype=np.float64),
                }
            )
        zi = st["zi"]
        zi[:, modes:, :] = 0.0  # stale modes stay dead
        lp = st["lp"]

        ratios = np.array(modal_ratios(material, modes)) ** (1.0 + 0.3 * inharm)
        gains = ratios ** (2.0 * (bright - 0.5))
        if position > 0.0:
            # Where the strike lands: comb the gains, then renormalize so
            # a strike near the edge is thin, not quiet.
            gains = gains * strike_comb(ratios, position)
        gains = gains / gains.sum()
        t60 = np.maximum(0.01, decay / (1.0 + 3.0 * tilt * (ratios - 1.0)))
        r = 10.0 ** (-3.0 / (sr * t60))

        # Stereo: per-mode equal-power pan gains, scaled so a centred mode
        # contributes 1.0 to each side (the outs equal `out` at spread 0).
        if spread > 0.0:
            theta = (mode_pans(modes, spread) + 1.0) * (np.pi / 4.0)
            gl = np.cos(theta) * np.sqrt(2.0)
            gr = np.sin(theta) * np.sqrt(2.0)

        # Per-voice pitch (block mean) → group voices sharing a value.
        def cv_for(v: int) -> float:
            if pitch is None:
                return 0.0
            row = pitch[v] if pitch.ndim == 2 and v < pitch.shape[0] else (
                pitch[0] if pitch.ndim == 2 else pitch
            )
            return self._finite_mean(row)

        x = excite if excite.ndim == 2 else excite[None, :]
        out = np.zeros((V, frames), dtype=np.float64)
        if spread > 0.0:
            out_l = np.zeros((V, frames), dtype=np.float64)
            out_r = np.zeros((V, frames), dtype=np.float64)
        groups: dict[float, list[int]] = {}
        for v in range(V):
            xv = x[v] if v < x.shape[0] else x[0]
            if (
                float(np.max(np.abs(xv))) < 1e-9
                and float(np.max(np.abs(zi[v, :modes]))) < 1e-7
            ):
                continue  # silent, rung out: free
            groups.setdefault(round(cv_for(v), 6), []).append(v)

        for cv_val, rows in groups.items():
            f0 = self._MODAL_C4 * self._pow2_clipped(cv_val)
            f0 = min(self._MODAL_MAX_F_FRACTION * sr, max(20.0, f0))
            freqs = f0 * ratios
            X = np.stack([x[v] if v < x.shape[0] else x[0] for v in rows]).astype(
                np.float64
            )
            if mallet > 0.0:
                # The mallet: a one-pole low-pass on the strike whose
                # cutoff tracks THIS group's pitch, so the same softness
                # reads the same across the keyboard. State per voice.
                fc = f0 * (2.0 ** (MALLET_OCTAVES * (1.0 - mallet)))
                fc = min(self._MODAL_MAX_F_FRACTION * sr, fc)
                coef = 1.0 - float(np.exp(-2.0 * np.pi * fc / sr))
                X, _zf = lfilter(
                    [coef], [1.0, coef - 1.0], X, axis=-1,
                    zi=((1.0 - coef) * lp[rows])[:, None],
                )
                # Carry the last OUTPUT (the octaver's idiom): the zi
                # above is rebuilt from it with the block's coefficient,
                # which is what keeps a pitch change from kicking the
                # filter -- lfilter's own zf already has (1-coef) in it.
                lp[rows] = X[:, -1]
            acc = np.zeros_like(X)
            if spread > 0.0:
                acc_l = np.zeros_like(X)
                acc_r = np.zeros_like(X)
            for i in range(modes):
                if freqs[i] >= self._MODAL_MAX_F_FRACTION * sr:
                    zi[rows, i, :] = 0.0
                    continue
                theta = 2.0 * np.pi * freqs[i] / sr
                a = [1.0, -2.0 * r[i] * np.cos(theta), r[i] * r[i]]
                b = [gains[i] * np.sin(theta)]
                y, zf = lfilter(b, a, X, axis=-1, zi=zi[rows, i, :])
                acc += y
                if spread > 0.0:
                    acc_l += y * gl[i]
                    acc_r += y * gr[i]
                zi[rows, i, :] = zf
            for k, v in enumerate(rows):
                out[v] = acc[k]
                if spread > 0.0:
                    out_l[v] = acc_l[k]
                    out_r[v] = acc_r[k]

        out *= level
        mono = (out if voiced else out[0]).astype(np.float32)
        if spread <= 0.0:
            return {"out": mono, "out_l": mono, "out_r": mono}
        out_l *= level
        out_r *= level
        return {
            "out": mono,
            "out_l": (out_l if voiced else out_l[0]).astype(np.float32),
            "out_r": (out_r if voiced else out_r[0]).astype(np.float32),
        }

    def _new_sampler_state(self) -> dict:
        return {"path": None, "loaded_path": None, "samples": None,
                "chains": None, "overview": None,
                "pending": None, "V": 0, "voices": []}

    @staticmethod
    def _new_sampler_voice() -> dict:
        return {
            "active": False,       # a playhead is running
            "pos": 0.0,            # playhead, in samples into the file
            "rate": 1.0,           # samples advanced per output sample (signed)
            "start": 0.0,          # this voice's region start (start_cv latched)
            "gain": 1.0,           # `vel` latched at the edge
            "releasing": False,    # gated fall -> ramping out
            "rel_left": 0,         # release samples still to serve
            "rel_total": 0,
            "atk_left": 0,         # declick ramp-in samples still to serve
            "atk_total": 0,
            "xf_left": 0,          # retrigger tail still to serve
            "xf_total": 0,
            "xf_pos": 0.0,         # the abandoned playhead
            "xf_rate": 1.0,
            "xf_start": 0.0,
            "xf_gain": 1.0,
            "prev_gate": False,
        }

    @staticmethod
    def _sampler_advance(pos, rate, count, region, loop):
        """Where the playhead reads, where it ends up, and how much counts.

        Returns ``(positions, next_pos, live)``. Without a ``loop`` the
        playhead is affine — ``pos + rate·arange(count)`` — and eventually
        walks off the region ``(lo, hi)``: forward off ``hi`` (positions
        ``< hi`` play), in reverse off ``lo`` (positions ``>= lo`` play).
        Either way the inside part is a *prefix*, so ``live`` says how much
        of the segment is still in the region.

        With a loop it wraps ``loop_end`` back to ``loop_start`` instead
        (or the other way round in reverse) and so never leaves the region
        at all (``live`` is the whole segment — that is what looping
        means). The wrap is one modulo on the same affine array, which
        keeps it a single vectorized expression *and* keeps it exact on
        integers: a unity-rate loop of an integer-bounded region is a
        bit-exact tiling of the file, not an approximation of one — and a
        reversed one is a bit-exact tiling of the region mirrored.
        """
        raw = pos + rate * np.arange(count, dtype=np.float64)
        end_raw = float(pos + rate * count)
        forward = rate >= 0.0
        if loop is None:
            if forward:
                live = int(np.searchsorted(raw, float(region[1])))
            else:
                live = int(np.count_nonzero(raw >= float(region[0])))
            return raw, end_raw, live
        lo, hi, length = loop[0], loop[1], loop[2]
        if forward:
            over = raw - hi
            positions = np.where(over < 0.0, raw, lo + np.mod(over, length))
            if end_raw >= hi:
                end_raw = float(lo + np.mod(end_raw - hi, length))
        else:
            under = raw - lo
            positions = np.where(under >= 0.0, raw, lo + np.mod(under, length))
            if end_raw < lo:
                end_raw = float(lo + np.mod(end_raw - lo, length))
        return positions, end_raw, count

    def _start_sample_loader(self, path):
        """Spawn a background whole-file decode (plus mip chain) for ``path``."""
        from ...modules.sampler import (
            HALFBAND_TAPS,
            MAX_SECONDS,
            MIP_LEVELS,
            MIP_MIN_SAMPLES,
            OVERVIEW_COLS,
        )
        from ..numpy_backend import _SampleLoader  # lazy: it imports this module

        return _SampleLoader(
            path, self.sample_rate, self._decode_audio, MAX_SECONDS,
            mip_levels=MIP_LEVELS, mip_min=MIP_MIN_SAMPLES,
            halfband_taps=HALFBAND_TAPS, overview_cols=OVERVIEW_COLS,
        )

    def sampler_overview(self, module_id: int):
        """(GUI hook) The loaded sample's waveform overview for the face.

        Returns ``(overview, loaded_path)`` — a ``(cols, 2)`` float32
        column min/max array built by the loader, and the path it belongs
        to (the face uses it as its "already painted?" key) — or
        ``(None, None)`` while nothing is loaded. GUI thread only; a
        couple of dict reads.
        """
        st = self._state.get(module_id)
        if not isinstance(st, dict):
            return None, None
        return st.get("overview"), st.get("loaded_path")

    def wait_for_sample_loads(self, timeout: float = 10.0) -> bool:
        """Block until every sampler's pending load finishes. Tests only.

        Never call from the audio thread. Returns True when every pending
        load finished with a usable buffer (no pending load counts as
        trivially ready, matching the render-silence contract).
        """
        import time as _time

        from ..numpy_backend import _SampleLoader  # lazy: it imports this module

        deadline = _time.monotonic() + float(timeout)
        ok = True
        for st in list(self._state.values()):
            if not isinstance(st, dict):
                continue
            pend = st.get("pending")
            loader = pend.get("loader") if isinstance(pend, dict) else None
            if loader is None or not isinstance(loader, _SampleLoader):
                continue
            remaining = max(0.0, deadline - _time.monotonic())
            ok = loader.wait(remaining) and ok
        return ok
