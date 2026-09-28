"""Spectral renderers: vocoder, convolver, freeze.

Moved verbatim out of ``numpy_backend.py`` (2026-09-28) into a mixin that
``NumpyBackend`` inherits -- see docs/architecture.md, "Renderer
families". Three convolver/vocoder helpers stay on the backend because
code outside this family calls them, and are reached through ``self``:
``_vocoder_hp_coeffs`` (mid_side's bass-mono highpass shares it) and
``_new_convolver_state`` / ``_start_ir_loader`` (``compile()`` pre-starts
IR loads). Freeze reaches the AudioToCV follower (``_audio_to_cv_block``,
``_audio_to_cv_loop_voice``) and ``_gate_ramp_env`` the same way. The
convolver's module-level pieces of ``numpy_backend`` --
``_PartitionedConvolver`` and the ``_CONV_*`` limits -- come in through
lazy imports at their point of use (``numpy_backend`` imports this
module, so a top-level import would be circular).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter


class SpectralRenderers:
    # ----- Vocoder rendering ------------------------------------------------

    _VOCODER_BANDS = (8, 12, 16, 24)
    _VOCODER_SIBILANCE_HZ = 5000.0   # hiss-path highpass corner

    def _render_vocoder(self, module, frames: int, buffers, patch):
        """Channel vocoder: two matched bandpass banks + per-band followers.

        The modulator and the carrier (both summed to mono -- you vocode
        the mix, the same collapse rule as the modulation trio) each run
        through N parallel RBJ constant-peak bandpasses built from the
        same coefficients. The modulator's rectified band outputs feed
        the asymmetric one-pole follower bank -- all N bands plus the
        sibilance row are independent rows, so the whole bank is ONE
        call to the AudioToCV monotone-pattern block solve
        (:meth:`_audio_to_cv_block`), with the per-sample loop as its
        usual degenerate-coefficient fallback. The carrier's band
        outputs are multiplied by those envelopes and summed.

        The hiss path is the same trick one band further up: the
        modulator through a highpass at ~5 kHz -> follower -> gates a
        matching highpassed white-noise stream into the wet sum,
        restoring the consonants the bands can't see.

        State per module: DF-I history ``(x1, x2, y1, y2)`` as ``(N,)``
        arrays for each bank (the parametric_eq pattern -- raw history
        is coefficient-independent, so live edits of width/range/bands
        behave cleanly), scalar DF-I history for the two highpasses,
        the ``(N+1,)`` follower levels, and the noise Generator (a
        stream, so any block split draws the identical sequence).
        Everything carries across blocks -> block-size independent.
        ``mix=0`` returns the carrier bit-exact (states still advance,
        so riding the mix knob up doesn't snap from stale filters).
        """
        carrier = self._input_buffer(patch, buffers, module.id, "carrier")
        if carrier is None:
            return np.zeros(frames, dtype=np.float32)
        mod = self._input_buffer(patch, buffers, module.id, "mod")

        n_bands = int(round(float(module.params.get("bands", 16))))
        if n_bands not in self._VOCODER_BANDS:
            n_bands = min(self._VOCODER_BANDS, key=lambda v: abs(v - n_bands))
        freq_lo = min(max(float(module.params.get("freq_lo", 120.0)), 50.0), 500.0)
        freq_hi = min(max(float(module.params.get("freq_hi", 7500.0)), 2000.0), 12000.0)
        freq_hi = min(freq_hi, 0.45 * self.sample_rate)
        width = min(max(float(module.params.get("width", 1.0)), 0.3), 3.0)
        attack_ms = min(max(float(module.params.get("attack", 4.0)), 0.1), 100.0)
        release_ms = min(max(float(module.params.get("release", 60.0)), 1.0), 500.0)
        hiss = min(max(float(module.params.get("hiss", 0.4)), 0.0), 1.0)
        gain = min(max(float(module.params.get("gain", 1.0)), 0.0), 4.0)
        mix = min(max(float(module.params.get("mix", 1.0)), 0.0), 1.0)

        state = self._state.setdefault(module.id, {})
        if state.get("n_bands") != n_bands:
            state.clear()
            state["n_bands"] = n_bands
            for bank in ("m", "c"):                      # mod / carrier banks
                for k in ("x1", "x2", "y1", "y2"):
                    state[bank + k] = np.zeros(n_bands, dtype=np.float64)
            for hp in ("hm", "hn"):                      # mod / noise highpass
                for k in ("x1", "x2", "y1", "y2"):
                    state[hp + k] = np.zeros(1, dtype=np.float64)
            state["env"] = np.zeros(n_bands + 1, dtype=np.float64)
            state["rng"] = np.random.default_rng(0xB0C0DE + module.id)

        if frames == 0:
            return np.empty(0, dtype=np.float32)

        dry = carrier.astype(np.float32, copy=True)      # (F,)
        car64 = carrier.astype(np.float64)
        mod64 = (
            np.zeros(frames, dtype=np.float64)
            if mod is None
            else mod.astype(np.float64)
        )

        # Band layout: log-spaced centres, Q from the adjacent-band
        # spacing scaled by ``width`` (width 1 = bands meet at their
        # -3 dB points; wider = overlap, narrower = gaps).
        centres = np.geomspace(freq_lo, max(freq_hi, freq_lo * 1.5), n_bands)
        ratio = centres[-1] / centres[0]
        bw_oct = np.log2(ratio) / max(n_bands - 1, 1) * width
        q = 1.0 / (2.0 * np.sinh(0.5 * np.log(2.0) * bw_oct))
        q = min(max(float(q), 0.5), 40.0)

        b0, b1, b2, a1n, a2n = self._vocoder_bp_coeffs(centres, q)

        mod_bands = np.empty((n_bands, frames), dtype=np.float64)
        car_bands = np.empty((n_bands, frames), dtype=np.float64)
        for k in range(n_bands):
            bk = np.array([b0[k], b1[k], b2[k]])
            ak = np.array([1.0, a1n[k], a2n[k]])
            mod_bands[k] = self._vocoder_biquad(
                state, "m", k, bk, ak, mod64, frames
            )
            car_bands[k] = self._vocoder_biquad(
                state, "c", k, bk, ak, car64, frames
            )

        # Sibilance detector + noise colour: one shared highpass design.
        hb, ha = self._vocoder_hp_coeffs(self._VOCODER_SIBILANCE_HZ)
        sib = self._vocoder_biquad(
            state, "h", 0, hb, ha, mod64, frames, key_prefix="hm"
        )
        noise = state["rng"].uniform(-1.0, 1.0, frames)
        noise_hp = self._vocoder_biquad(
            state, "h", 0, hb, ha, noise, frames, key_prefix="hn"
        )

        # Follower bank: N band rows + the sibilance row, one solve.
        sr = self.sample_rate
        att = 1.0 - float(np.exp(-1.0 / (attack_ms * 1e-3 * sr)))
        rel = 1.0 - float(np.exp(-1.0 / (release_ms * 1e-3 * sr)))
        t = np.abs(np.vstack([mod_bands, sib[None, :]]))     # (N+1, F)
        level = state["env"]
        env = self._audio_to_cv_block(t, level, att, rel)
        if env is None:
            env, level = self._audio_to_cv_loop_voice(t, level, att, rel)
        else:
            level = env[:, -1].copy()
        state["env"] = level

        wet = (car_bands * env[:n_bands]).sum(axis=0)
        wet += hiss * env[n_bands] * noise_hp
        wet *= gain

        if mix <= 0.0:
            return dry
        out = (1.0 - mix) * dry.astype(np.float64) + mix * wet
        return out.astype(np.float32)

    def _vocoder_biquad(
        self, state, bank, k, b, a, x, frames, key_prefix=None
    ):
        """One biquad over ``x`` with the house DF-I history carry.

        ``state[prefix + {x1,x2,y1,y2}][k]`` holds the raw DF-I history
        for slot ``k`` of the named bank; it's converted to the
        transposed-DF-II ``zi`` (the lfiltic identity, inlined -- the
        same lines as parametric_eq), the biquad runs in C via
        ``lfilter``, and the new history is read off the tails.
        """
        p = key_prefix or bank
        x1 = state[p + "x1"]; x2 = state[p + "x2"]
        y1 = state[p + "y1"]; y2 = state[p + "y2"]
        zi = np.array(
            [
                b[1] * x1[k] + b[2] * x2[k] - a[1] * y1[k] - a[2] * y2[k],
                b[2] * x1[k] - a[2] * y1[k],
            ],
            dtype=np.float64,
        )
        out = lfilter(b, a, x, zi=zi)[0]
        new_x1 = x[-1]
        new_x2 = x[-2] if frames >= 2 else x1[k]
        new_y1 = out[-1]
        new_y2 = out[-2] if frames >= 2 else y1[k]
        x1[k] = new_x1; x2[k] = new_x2
        y1[k] = new_y1; y2[k] = new_y2
        return out

    def _vocoder_bp_coeffs(self, centres, q):
        """RBJ constant-0dB-peak bandpass coefficients, vectorized over bands."""
        w0 = 2.0 * np.pi * centres / self.sample_rate
        alpha = np.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        b0 = alpha / a0
        b1 = np.zeros_like(b0)
        b2 = -alpha / a0
        a1n = (-2.0 * np.cos(w0)) / a0
        a2n = (1.0 - alpha) / a0
        return b0, b1, b2, a1n, a2n

    def _render_convolver(self, module, frames: int, buffers, patch):
        """Partitioned-FFT convolution (IR reverb / cab): mono-in, stereo out.

        Voices are summed to mono before convolving -- convolution is linear,
        so per-voice-then-sum equals sum-then-convolve, and the mono sum is
        far cheaper (one FFT stream, not V). A stereo IR convolves that mono
        input through its left channel into ``out_l`` and its right into
        ``out_r`` (a mono IR drives both, and is convolved once); the
        decorrelation in the IR is the stereo image.

        The IR is decoded + partition-built on a background thread
        (``_IRLoader``), kicked at compile() and on any live ``path`` edit, so
        a new/changed IR never blocks the audio thread -- the convolver keeps
        the previous IR (or a transparent unit impulse) sounding until the new
        one is ready. The wet path carries a fixed one-block latency; the dry
        path is delay-matched by the same block inside ``mix`` so dry and wet
        stay phase-coherent. ``gain`` trims the wet only, so ``mix = 0`` is a
        bit-exact dry bypass (FFT skipped) whatever ``gain`` is.
        """
        state = self._state.setdefault(module.id, self._new_convolver_state())

        # --- resolve path -> active engines (never blocks the audio thread) ---
        path = self._resolve_media_path(module.params.get("path", ""))
        if path == "":
            # Transparent insert: drop any IR + cancel a pending load.
            if state.get("ir_l") is not None or state.get("loaded_path") is not None:
                state["ir_l"] = state["ir_r"] = None
                state["engine_l"] = state["engine_r"] = None
                state["loaded_path"] = None
            pend = state.get("pending")
            if pend is not None:
                if pend.get("loader") is not None:
                    pend["loader"].close()
                state["pending"] = None
        else:
            pend = state.get("pending")
            if path != state.get("loaded_path") and (
                pend is None or pend.get("path") != path
            ):
                # New/changed IR: kick a background load, keep current engines
                # (previous IR or transparent) sounding until it is ready.
                state["pending"] = {
                    "path": path, "loader": self._start_ir_loader(path, frames)
                }

        # Adopt a finished load (ready -> new engines; failed -> keep current).
        pend = state.get("pending")
        if pend is not None:
            loader = pend.get("loader")
            if loader is None or loader.done:
                if loader is not None and loader.ready:
                    state["ir_l"] = loader.ir_l
                    state["ir_r"] = loader.ir_r
                    state["engine_l"] = loader.engine_l
                    state["engine_r"] = loader.engine_r
                    state["block"] = loader.block
                    state["loaded_path"] = pend["path"]
                state["pending"] = None

        # --- ensure engines exist and match the current block size ---
        if state.get("engine_l") is None or state.get("block") != frames:
            from ..numpy_backend import _PartitionedConvolver  # lazy: it imports this module
            ir_l = state.get("ir_l")
            if ir_l is None:  # transparent: shared unit-impulse engine
                imp = _PartitionedConvolver(np.array([1.0], dtype=np.float64), frames)
                state["engine_l"] = imp
                state["engine_r"] = imp
            else:
                ir_r = state.get("ir_r")
                state["engine_l"] = _PartitionedConvolver(ir_l, frames)
                if ir_r is None or np.array_equal(ir_l, ir_r):
                    state["engine_r"] = state["engine_l"]
                else:
                    state["engine_r"] = _PartitionedConvolver(ir_r, frames)
            state["block"] = frames
            state["dry_prev"] = np.zeros(frames, dtype=np.float32)

        # --- mono dry (collapse=True sums any voice rows: conv is linear) ---
        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None:
            x = np.zeros(frames, dtype=np.float32)
        else:
            x = np.asarray(src, dtype=np.float32).reshape(-1)
            if x.shape[0] < frames:  # defensive; buffers are frames-long
                x = np.concatenate(
                    [x, np.zeros(frames - x.shape[0], dtype=np.float32)]
                )
            elif x.shape[0] > frames:
                x = x[:frames]

        # dry delayed by one block (== engine latency) to align with the wet
        dry_prev = state.get("dry_prev")
        if dry_prev is None or dry_prev.shape[0] != frames:
            dry_prev = np.zeros(frames, dtype=np.float32)
        dry_delayed = dry_prev
        state["dry_prev"] = x

        mix = float(module.params.get("mix", 1.0))
        # Neutral bypass: mix <= 0 -> bit-exact delayed dry, FFT skipped.
        if mix <= 0.0:
            out = np.array(dry_delayed, dtype=np.float32, copy=True)
            return {"out_l": out, "out_r": np.array(out, copy=True)}

        gain = float(module.params.get("gain", 1.0))
        # lazy: numpy_backend imports this module
        from ..numpy_backend import _CONV_PREDELAY_MAX_MS, _CONV_TONE_MAX, _CONV_TONE_MIN
        tone_hz = min(max(float(module.params.get("tone", _CONV_TONE_MAX)),
                          _CONV_TONE_MIN), _CONV_TONE_MAX)
        predelay_ms = min(max(float(module.params.get("predelay", 0.0)), 0.0),
                          _CONV_PREDELAY_MAX_MS)
        pd = int(round(predelay_ms * 1e-3 * self.sample_rate))

        eng_l = state["engine_l"]
        eng_r = state["engine_r"]
        shared = eng_r is eng_l  # mono IR -> convolve once (the FFTs are shared)
        wet_l = eng_l.process(x).astype(np.float32)
        wet_r = wet_l if shared else eng_r.process(x).astype(np.float32)
        # Per-channel wet shaping (tone low-pass -> predelay), then wet gain.
        wet_l = self._shape_conv_wet(wet_l, state, "l", tone_hz, pd, frames)
        wet_r = self._shape_conv_wet(wet_r, state, "r", tone_hz, pd, frames)
        if gain != 1.0:
            g = np.float32(gain)
            wet_l = wet_l * g
            wet_r = wet_r * g

        if mix >= 1.0:
            out_l = np.ascontiguousarray(wet_l, dtype=np.float32)
            out_r = np.ascontiguousarray(wet_r, dtype=np.float32)
        else:
            m = np.float32(mix)
            dm = np.float32(1.0 - mix)
            out_l = (m * wet_l + dm * dry_delayed).astype(np.float32)
            out_r = (m * wet_r + dm * dry_delayed).astype(np.float32)
        if out_r is out_l:  # distinct arrays for the two ports
            out_r = np.array(out_l, copy=True)
        return {"out_l": out_l, "out_r": out_r}

    def _shape_conv_wet(self, wet, state, ch, tone_hz, predelay, frames):
        """Shape one wet channel: tone low-pass (off at max) then predelay.

        Both are wet-only and cheap, so they run per channel even when the
        convolution engine is shared (a mono IR) -- the mirrored per-channel
        state (``tone_zi_<ch>`` / ``pd_buf_<ch>``) keeps the two identical. The
        tone one-pole carries its state across blocks; predelay is a
        per-channel FIFO of ``predelay`` samples, an intentional wet delay on
        top of the module's one-block latency so the reverb starts behind the
        dry.
        """
        from ..numpy_backend import _CONV_TONE_MAX  # lazy: it imports this module
        if tone_hz < _CONV_TONE_MAX:
            a = 1.0 - float(np.exp(-2.0 * np.pi * tone_hz / self.sample_rate))
            zi = state.get("tone_zi_" + ch)
            if zi is None:
                zi = np.zeros(1, dtype=np.float64)
            filtered, zi = lfilter(
                [a], [1.0, -(1.0 - a)], np.asarray(wet, dtype=np.float64), zi=zi
            )
            state["tone_zi_" + ch] = zi
            wet = filtered
        if predelay > 0:
            buf = state.get("pd_buf_" + ch)
            if buf is None or buf.shape[0] != predelay:
                buf = np.zeros(predelay, dtype=np.float64)
            combined = np.concatenate([buf, np.asarray(wet, dtype=np.float64)])
            wet = combined[:frames]
            state["pd_buf_" + ch] = combined[frames:]
        return np.asarray(wet, dtype=np.float32)

    def wait_for_ir_loads(self, timeout: float = 10.0) -> bool:
        """Block until every convolver's pending IR load finishes.

        Tests / offline render only -- never call from the audio thread.
        Returns True when every pending loader finished with a usable IR
        (no pending load counts as trivially ready).
        """
        import time as _time

        deadline = _time.monotonic() + float(timeout)
        ok = True
        for st in list(self._state.values()):
            if not isinstance(st, dict):
                continue
            pend = st.get("pending")
            loader = pend.get("loader") if isinstance(pend, dict) else None
            if loader is None:
                continue
            remaining = max(0.0, deadline - _time.monotonic())
            ok = bool(loader.wait(remaining)) and ok
        return ok

    # ----- Freeze rendering ------------------------------------------------------

    #: How many frozen layers may sound at once (a re-freeze fades the old
    #: one out; a flurry of re-triggers drops the oldest beyond this).
    _FREEZE_MAX_LAYERS = 4
    #: The pitch exponent is clipped to +-4 octaves BEFORE ``2 ** e`` (the
    #: filter pass found an absurd CV can overflow the power).
    _FREEZE_PITCH_OCT_LIMIT = 4.0
    #: Periodic Hann windows by size, built once.
    _FREEZE_WINDOWS: dict = {}

    @classmethod
    def _freeze_window(cls, n: int) -> np.ndarray:
        w = cls._FREEZE_WINDOWS.get(n)
        if w is None:
            w = np.hanning(n + 1)[:-1].astype(np.float64)
            cls._FREEZE_WINDOWS[n] = w
        return w

    @staticmethod
    def _freeze_lock_index(mag: np.ndarray, floor: float = 1e-12) -> np.ndarray:
        """The region map: for every bin, the index of the spectral peak
        whose region of influence (out to the valley on either side) it
        belongs to. A spectrum with no peak at all maps every bin to
        itself. Both the phase lock and the stereo scatter are one value
        per region, read through this map. A peak must stand above
        ``floor`` (absolute); the capture passes a RELATIVE one,
        ``_FREEZE_PEAK_FLOOR_DB`` under the frame's loudest bin.

        Vectorized (2026-09-22, the long-windows pass) and INTEGER-identical
        to the Python loop it replaces -- the loop was the whole cost of a
        capture at a big window (17 ms of the 23 ms at 65536, 6 ms of 7.6 ms
        at 32768) because a real spectrum has a peak every three or four
        bins in its numerical floor: 9526 regions at 65536. The two tricks:

        * The valley between peaks ``a`` and ``b`` is the first argmin over
          the HALF-OPEN ``[a, b)``, not the closed ``[a, b]`` the loop
          wrote, because two peaks are never adjacent (a peak needs
          ``mag[k] > mag[k-1]`` and ``mag[k] >= mag[k+1]``, which forbids
          one at ``k+1``), so ``mag[b-1] < mag[b]`` with ``b-1`` inside the
          segment and the minimum is never AT ``b``. Half-open segments are
          exactly what ``reduceat`` reduces.
        * First-index-of-minimum per segment in two passes: ``reduceat``
          for the minimum, then ``reduceat`` again over the bin's own index
          where it equals that minimum (a sentinel elsewhere) -- which is
          ``argmin``'s first-wins tie-break by construction.

        Fuzzed against the loop over 4025 spectra (random, all-ties,
        all-zero, all-below-threshold, sparse, and real triad / sine /
        noise / silence / DC rffts at every window size): zero mismatches.
        """
        k_n = mag.shape[0]
        up = np.concatenate(([False], mag[1:] > mag[:-1]))
        down = np.concatenate((mag[:-1] >= mag[1:], [False]))
        peaks = np.flatnonzero(up & down & (mag > floor))
        if peaks.size == 0:
            return np.arange(k_n)
        if peaks.size == 1:
            return np.full(k_n, peaks[0], dtype=peaks.dtype)
        p0, p1 = int(peaks[0]), int(peaks[-1])
        seg_min = np.repeat(np.minimum.reduceat(mag, peaks)[:-1], np.diff(peaks))
        sub = mag[p0:p1]
        pos = np.where(sub == seg_min, np.arange(p0, p1), k_n)
        bounds = np.empty(peaks.size + 1, dtype=np.intp)
        bounds[0] = 0
        bounds[1:-1] = np.minimum.reduceat(pos, peaks[:-1] - p0)
        bounds[-1] = k_n
        return np.repeat(peaks, np.diff(bounds))

    @classmethod
    def _freeze_lock(cls, mag: np.ndarray, w_true: np.ndarray) -> np.ndarray:
        """Identity phase locking (Laroche & Dolson): every bin in a
        spectral peak's region of influence (out to the valley on either
        side) advances at the PEAK's true frequency.

        Without it the bins of one partial's lobe that a neighbouring
        partial has contaminated carry a frequency of their own, dephase
        against the lobe's centre over seconds, and the hold slowly eats
        itself -- a C-E-G triad at a 4096 window had lost two thirds of
        two partials by 10 s. Locked, the same triad holds within 2%
        forever, and a lone sine is unchanged.
        """
        return w_true[cls._freeze_lock_index(mag)]

    #: A spectral peak must stand within this many dB of the frame's
    #: loudest bin to anchor a region (2026-09-24). Below it is the
    #: window's own numerical floor: a pure triad at 32768 found 4518
    #: "peaks" under the old absolute 1e-12 and finds its 3 partials at
    #: -120 dB, at every window size; white / pink noise, a detuned saw
    #: chord and a vowel keep EVERY peak they had (a dense spectrum's
    #: peaks are all within ~60 dB of its top). -100 dB started to thin
    #: the vowel's top octave, so the floor sits a clean 20 dB lower.
    _FREEZE_PEAK_FLOOR_DB = -120.0

    #: The staged birth (2026-09-24): at these window sizes a capture is
    #: taken AT the edge but the layer is born this many samples LATER,
    #: and the capture's work -- two FFTs, the analysis, the four frames
    #: that overlap the first read -- is spread over the blocks in
    #: between instead of landing in one (at 65536 it is ~11 ms mono,
    #: ~13 ms wide in one block, against an 11.61 ms budget at 512).
    #: Every layer then hears the gate this many samples late, so the
    #: whole wet path is the undelayed one shifted by the delay; the read
    #: starts at frozen time ``n + delay`` so the hold is still the live
    #: input's continuation, in phase. 0 (every other size) = shipped.
    _FREEZE_BIRTH_DELAY = {65536: 4096}
    #: The capture's stages before the first frame: rfft, rfft, analysis.
    _FREEZE_CAPTURE_STAGES = 3
    _FREEZE_WINDOWS15: dict = {}

    @classmethod
    def _freeze_window15(cls, n: int) -> np.ndarray:
        """The synthesis window with the Hann^2 overlap sum (1.5) divided out."""
        w = cls._FREEZE_WINDOWS15.get(n)
        if w is None:
            w = cls._freeze_window(n) / 1.5
            cls._FREEZE_WINDOWS15[n] = w
        return w

    @classmethod
    def _freeze_analyse(cls, spec_a: np.ndarray, spec_b: np.ndarray, n: int,
                        hop: int) -> dict:
        """The capture's analysis, from the two frames' spectra.

        Magnitudes AND phases are ``spec_b`` itself (the later frame, the
        one ending at the edge). Each REGION's true frequency comes from
        its peak's phase difference across the hop (the phase-vocoder
        estimate ``w_bin + princarg(dphi - w_bin*hop)/hop``), so a
        partial between two bins holds at its real pitch instead of
        beating between them, and every bin of the region advances at it
        (``_freeze_lock``). What is stored is the per-frame ROTOR
        ``exp(i * w_true * hop)``: frame ``j + 1`` is frame ``j`` times
        the rotor, one complex multiply instead of an ``exp`` and a
        ``mod`` over every bin (the cost of a frame at 65536 was 2.1 ms
        of transcendentals around a 0.46 ms irfft). The phase work runs on
        the peaks only, since only a peak's frequency is ever used.
        ``jside``: ``1j * (+1 / -1)`` per bin, alternating by peak REGION
        -- the quadrature the stereo scatter adds (``width``).
        """
        mag = np.abs(spec_b)
        top = float(mag.max()) if mag.size else 0.0
        floor = 1e-12
        if np.isfinite(top):
            floor = max(floor, top * 10.0 ** (cls._FREEZE_PEAK_FLOOR_DB / 20.0))
        idx = cls._freeze_lock_index(mag, floor)
        # idx is non-decreasing (a repeat of the sorted peaks), so a
        # region's ordinal is a running count of where it changes
        new = np.empty(idx.shape[0], dtype=bool)
        new[:1] = True
        new[1:] = idx[1:] != idx[:-1]
        ordinal = np.cumsum(new) - 1
        pk = idx[new]
        w_pk = 2.0 * np.pi * pk / n
        dphi = np.angle(spec_b[pk]) - np.angle(spec_a[pk]) - w_pk * hop
        dphi = np.mod(dphi + np.pi, 2.0 * np.pi) - np.pi
        rot = np.exp(1j * ((w_pk + dphi / hop) * hop))[ordinal]
        jside = (1j * (1.0 - 2.0 * (np.arange(pk.shape[0]) % 2)))[ordinal]
        return {"X": spec_b, "rot": rot, "jside": jside}

    @classmethod
    def _freeze_capture(cls, seg: np.ndarray, n: int, hop: int) -> dict:
        """Analyse the ``n + hop`` samples before a freeze edge: two Hann
        frames one hop apart through ``_freeze_analyse`` (in one go; the
        staged birth runs the same three stages across blocks)."""
        w = cls._freeze_window(n)
        return cls._freeze_analyse(np.fft.rfft(seg[:n] * w),
                                   np.fft.rfft(seg[hop:hop + n] * w), n, hop)

    @staticmethod
    def _freeze_new_layer(spec: dict, n: int, hop: int, delay: int) -> dict:
        """The spectral half of a layer: the rotor stream advanced to the
        first frame the read ever touches. The read starts at frozen time
        ``n + delay`` and frame ``j`` fills ``[j*hop, j*hop + n)``, so the
        frames before ``delay // hop + 1`` are never heard -- frame 0,
        which the shipped birth made and never read, is skipped."""
        j0 = delay // hop + 1
        X, rot = spec["X"], spec["rot"]
        for _ in range(j0):
            X = X * rot
        return {
            "X": X, "rot": rot, "jside": spec["jside"],
            "syn": np.zeros(4 * n, dtype=np.float64), "origin": 0,
            "j0": j0, "j_next": j0,
            # the stereo channels are made only once width is up
            "syn_l": None, "syn_r": None,
        }

    @classmethod
    def _freeze_frames(cls, layer: dict, j_stop: int, n: int, hop: int, width: float,
                       ratio: float, smear: float, seed: int) -> None:
        """Synthesise frames ``j_next .. j_stop - 1`` into the layer's
        overlap-add buffers (frozen time, offset by ``origin``).

        Frame ``j`` is ``irfft(X_j) * hann / 1.5`` with ``X_{j+1} = X_j *
        rot``; bins above Nyquist / r are zeroed when r > 1; ``smear``
        multiplies in ``exp(i * smear * jitter_j)`` from
        ``default_rng([seed, j])``. ``width``: L / R are the frame rotated
        by ``+/- side * width * pi/4`` per region, which by linearity is
        ``cos(t) * M +/- sin(t) * Q`` with ``Q = irfft(1j * side * X_j)``
        -- two inverse FFTs for the pair instead of two more of their own
        (and none at all while the scale is 0: the channels are the mono
        frame). When width first comes up on a live layer its channels
        start as copies of the mono stream and diverge from here on,
        across the overlap -- no step."""
        j = int(layer["j_next"])
        if j >= j_stop:
            return
        syn = layer["syn"]
        syn_l, syn_r = layer["syn_l"], layer["syn_r"]
        wide = width > 0.0 or syn_l is not None
        if wide and syn_l is None:
            if j == int(layer["j0"]):          # nothing written yet
                syn_l, syn_r = np.zeros_like(syn), np.zeros_like(syn)
            else:
                syn_l, syn_r = syn.copy(), syn.copy()
        origin = int(layer["origin"])
        w15 = cls._freeze_window15(n)
        X, rot = layer["X"], layer["rot"]
        k_n = X.shape[0]
        keep = None
        if ratio > 1.0:
            keep = np.arange(k_n) < int(k_n / ratio)
        theta = width * np.pi / 4.0
        c, s = np.cos(theta), np.sin(theta)
        while j < j_stop:
            lo = j * hop - origin
            end = lo + n
            if end > syn.shape[0]:
                grow = end - syn.shape[0] + 4 * n
                syn = np.concatenate([syn, np.zeros(grow, dtype=np.float64)])
                if wide:
                    syn_l = np.concatenate([syn_l, np.zeros(grow, dtype=np.float64)])
                    syn_r = np.concatenate([syn_r, np.zeros(grow, dtype=np.float64)])
            xs = X if keep is None else np.where(keep, X, 0.0)
            if smear > 0.0:
                jit = np.random.default_rng([seed, j]).uniform(-np.pi, np.pi, k_n)
                xs = xs * np.exp(1j * (smear * jit))
            frame = np.fft.irfft(xs, n) * w15
            syn[lo:end] += frame
            if wide:
                if s == 0.0:
                    syn_l[lo:end] += frame
                    syn_r[lo:end] += frame
                else:
                    # the same jitter, rotated apart: L leads, R lags
                    cm = c * frame
                    sq = s * (np.fft.irfft(layer["jside"] * xs, n) * w15)
                    syn_l[lo:end] += cm + sq
                    syn_r[lo:end] += cm - sq
            X = X * rot
            j += 1
        layer["X"], layer["j_next"] = X, j
        layer["syn"], layer["syn_l"], layer["syn_r"] = syn, syn_l, syn_r

    @classmethod
    def _freeze_birth_step(cls, pc: dict, n: int, hop: int, delay: int, width: float,
                           ratio: float, smear: float, seed: int) -> None:
        """One stage of a staged birth: rfft, rfft, analysis, then one
        prefill frame per stage until the frames under the first read
        exist."""
        s = int(pc["stage"])
        if s == 0:
            pc["spec_a"] = np.fft.rfft(pc["seg"][:n] * cls._freeze_window(n))
        elif s == 1:
            pc["spec_b"] = np.fft.rfft(pc["seg"][hop:hop + n] * cls._freeze_window(n))
            pc["seg"] = None
        elif s == 2:
            spec = cls._freeze_analyse(pc.pop("spec_a"), pc.pop("spec_b"), n, hop)
            pc["layer"] = cls._freeze_new_layer(spec, n, hop, delay)
        else:
            layer = pc["layer"]
            cls._freeze_frames(layer, int(layer["j_next"]) + 1, n, hop, width,
                               ratio, smear, seed)
        pc["stage"] = s + 1

    def _render_freeze(self, module, frames: int, buffers, patch) -> dict:
        """Spectral freeze (see modules/freeze.py for the contract).
        Returns ``{"out", "out_l", "out_r"}``.

        The input rolls through a history of ``size + hop`` samples. A
        rising edge of the gate (OR the tickbox) captures a LAYER from
        that history (``_freeze_capture``) and the layer re-synthesises
        its spectrum forever: frame ``j`` is ``mag * exp(i(phi + j *
        w_true * hop [+ smear * jitter_j]))`` (``w_true`` phase-locked to
        the spectral peaks) through an inverse FFT and a synthesis Hann,
        overlap-added at the hop into a rolling buffer
        in the layer's own "frozen time" (Hann^2 at 75% overlap sums to
        1.5, divided out, so a stationary input freezes at unity). The
        jitter is ``default_rng([seed, j])`` keyed by the frame index and
        frames are generated in order on demand, so the stream is the
        same whatever the block partition.

        2026-09-24: the phase advance is a stored ROTOR (``X_{j+1} = X_j *
        exp(i w_true hop)``, ``_freeze_analyse``) instead of an ``exp``
        and a ``mod`` over every bin per frame, ``width`` is two inverse
        FFTs by quadrature instead of three (``_freeze_frames``), frame 0
        (never read) is skipped and the buffer shift copies only the live
        span -- renders move at the float32 LSB (-140 dB and below). Peaks
        must stand within ``_FREEZE_PEAK_FLOOR_DB`` of the frame's top, so
        a triad's region map is its three partials, not thousands of
        numerical-floor "peaks" -- which also means one ulp of input can
        no longer flip a floor peak and swap half the partials between L
        and R (it did: -13 dB of change on a 32768 organ hold's out_l).
        At the sizes in ``_FREEZE_BIRTH_DELAY`` (65536) the birth is
        STAGED: the capture is taken at the real edge, finished over the
        next ``delay`` samples (``_freeze_birth_step``, one stage per 512
        block), and every layer hears the gate ``delay`` samples late.

        Pitch is a change of READ rate on that stationary stream (linear
        interpolation), never a resampled spectrum -- exact frequency,
        unity level; the read position is ``p_base + (m - m_base) * r``
        with ``m`` an integer count of output samples since the layer
        was born and a rebase whenever ``r`` changes, so a constant ratio
        is bit-exact across block sizes and a ratio change never jumps.
        Bins above Nyquist / r are zeroed at synthesis when r > 1.

        Each layer has its own integer-count ``_gate_ramp_env`` (attack =
        release = ``fade``). A new edge while a layer still sounds
        creates a new layer and forces the old one into its release from
        its current level -- the two crossfade, a re-freeze melts. A
        layer whose envelope has reached 0 is dropped. With no layer
        alive and no edge this block the render returns ``src`` itself
        at ``dry`` 1.0 (bit-exact passthrough) while the history rolls.

        ``width`` (love pass): the stereo pair is the same stream with a
        QUADRATURE phase scatter, one constant per peak region -- every
        partial is rotated ``+width * pi/4`` in L and ``-width * pi/4``
        in R, the sign alternating region by region (``side`` from the
        capture). One phase per REGION, not per bin, because a partial's
        Hann lobe spans four bins and a per-bin scatter breaks the
        lobe's coherence exactly as ``smear`` does (measured: partials
        moved by up to 16 dB on a triad, 36 dB on a saw); a per-region
        rotation is a pure phase shift of the partial, so each channel
        keeps every partial's level to 0.01 dB. Deterministic rather
        than random so the numbers are laws, not luck: ``corr(L, R) =
        cos(width * pi/2)`` (0 at width 1) and the fold ``(L + R)/2`` is
        the mono hold itself at ``cos(width * pi/4)`` (-3.01 dB at width
        1, no partial ever cancelled). The scatter is applied at frame
        synthesis, so a layer born at width 0 costs nothing extra and
        ``out_l``/``out_r`` ARE the mono buffer; when width first rises
        on a live layer its channel buffers start as copies of the mono
        stream and diverge from the next frame across the overlap (no
        step), and a layer that has channels keeps them until it dies
        (turning width back to 0 melts them back to mono the same way).
        ``out`` is always the untouched mono. The smear jitter, when
        on, is the same for both channels.

        ``width_cv`` (love pass 2): the scatter CONSTANTS are fixed per
        layer at capture -- what the CV moves is the scale, ``clamp(width
        + width_cv_depth * mean cv, 0, 1)`` with the mean taken in
        float64 (a float32 mean of a constant CV is block-size sensitive
        at the ulp), read per block and multiplied into the stored
        per-region ``side`` when a frame is synthesised. That is why it
        costs nothing: the same one rotation per frame that ``width``
        already paid for, at a different angle. It is also why it cannot
        click -- the frames run one window ahead of the read, so a change
        reaches the ears through the overlap-add, a crossfade from the
        old scatter to the new across ``size`` samples (93 ms at 4096,
        743 ms at 32768). Unpatched the mean is 0.0 and the effective
        width is the knob, added exactly: bit-exact with the pre-CV
        render.

        ``latch`` (love pass 2): with it off (shipped) the gate row IS
        the cable, high holds and the fall releases. With it on, the
        cable's RISING edges toggle a state that becomes the gate row --
        parity from a ``cumsum`` of the edge mask XORed with the state
        carried in, so the toggle count is exact across any block
        partition and a pair of edges inside one block is a hold and a
        release. The ``freeze`` tickbox still overrides the row to all-high
        (the switch beats the pedal), and the latch state keeps running
        underneath it, so unticking hands the hold back to whatever the
        latch last said. Engaging ``latch`` mid-hold adopts the previous
        block's gate value as the latch state, so the flip is silent
        rather than a release.

        ``decay`` (love pass): the hold fades by itself -- a per-sample
        factor ``g_base * 10 ** (-3 (m - m_gbase) / (decay sr))`` from
        the INTEGER count ``m`` since birth (-60 dB in ``decay`` seconds;
        block-size exact because it is a function of the count, never a
        carried multiplication), on top of the gate envelope. A knob
        turn mid-hold rebases (the fall so far folds into ``g_base``,
        the new rate runs from now) so the level never jumps; 0 =
        forever (turned to 0 mid-fall, the level holds where it is). A
        layer below -90 dB is dropped even while the gate is high, so
        nothing runs for free; a new edge starts a new layer at full.
        """
        from ...modules.freeze import FREEZE_SIZES

        src = self._input_buffer(patch, buffers, module.id, "in")
        if src is None or src.size == 0:
            self._state.pop(module.id, None)
            z = np.zeros(frames, dtype=np.float32)
            return {"out": z, "out_l": z, "out_r": z}
        if frames == 0:
            e = np.empty(0, dtype=np.float32)
            return {"out": e, "out_l": e, "out_r": e}

        sr = self.sample_rate
        size = int(module.params.get("size", 4096))
        if size not in FREEZE_SIZES:
            size = 4096
        n = size
        hop = n // 4
        delay = int(self._FREEZE_BIRTH_DELAY.get(n, 0))
        tick = bool(module.params.get("freeze", False))
        latch = bool(module.params.get("latch", False))
        smear = min(max(float(module.params.get("smear", 0.0)), 0.0), 1.0)
        pitch = float(module.params.get("pitch", 0.0))
        depth = float(module.params.get("pitch_cv_depth", 1.0))
        level = max(0.0, float(module.params.get("level", 0.7)))
        dry = max(0.0, float(module.params.get("dry", 1.0)))
        fade_ms = max(0.0, float(module.params.get("fade", 60.0)))
        fade_n = max(1, int(round(fade_ms * sr / 1000.0)))
        seed = abs(int(module.params.get("seed", 1)))
        width = float(module.params.get("width", 0.0))
        width = min(max(width, 0.0), 1.0) if np.isfinite(width) else 0.0
        w_depth = float(module.params.get("width_cv_depth", 1.0))
        if not np.isfinite(w_depth):
            w_depth = 0.0
        decay_s = float(module.params.get("decay", 0.0))
        if not np.isfinite(decay_s) or decay_s <= 0.0:
            decay_s = 0.0          # forever

        st = self._state.setdefault(module.id, {})
        if st.get("n") != n:
            # A new window size restarts the history (and any hold with it).
            st.clear()
            st["n"] = n
            st["hist"] = np.zeros(n + hop, dtype=np.float64)
            st["prev"] = False
            st["t"] = 0
            st["layers"] = []
            # the latch's own machine: the cable's last sample (for its
            # rising edges) and the toggle state it has arrived at
            st["prev_raw"] = False
            st["latched"] = False
            st["latch_on"] = latch
            # the staged birth (``_FREEZE_BIRTH_DELAY``): captures taken at
            # the edge and finished over the next ``delay`` samples, and
            # the gate row the layers hear, ``delay`` samples late
            st["pending"] = []
            st["gdelay"] = np.zeros(delay, dtype=bool)
            st["prev_d"] = False

        x = src.astype(np.float64)
        full = np.concatenate([st["hist"], x])
        st["hist"] = full[-(n + hop):]
        t_abs = int(st["t"])
        st["t"] = t_abs + frames

        # --- the gate: the cable (momentary, or latched by ``latch``),
        # then the tickbox forced over it; a (V, F) cable is the house sum
        gate = self._input_buffer(patch, buffers, module.id, "freeze")
        if gate is not None and gate.shape[0] == frames:
            gt = gate > self._GATE_HIGH
        else:
            gt = np.zeros(frames, dtype=bool)
        prev_raw = bool(st.get("prev_raw", False))
        st["prev_raw"] = bool(gt[-1])
        if latch:
            if not st.get("latch_on", False):
                # engaging the latch adopts the hold that is sounding, so
                # the flip itself never releases anything
                st["latched"] = bool(st["prev"])
            edges = gt & ~np.concatenate(([prev_raw], gt[:-1]))
            # parity of the toggles so far, carried across blocks
            gt = np.logical_xor(bool(st.get("latched", False)),
                                (np.cumsum(edges) & 1).astype(bool))
            st["latched"] = bool(gt[-1])
        st["latch_on"] = latch
        if tick:
            gt = np.ones(frames, dtype=bool)
        prev = bool(st["prev"])
        st["prev"] = bool(gt[-1])
        rising = np.flatnonzero(gt & ~np.concatenate(([prev], gt[:-1])))

        # --- the staged birth: the capture is taken at the edge (the
        # history is the edge's), the layer is born ``delay`` samples
        # later, and every layer hears the gate that late -- the wet path
        # is the undelayed one, shifted. delay 0: the row itself.
        pending = st["pending"]
        if delay:
            for i in rising.tolist():
                pending.append({"seg": full[i:i + n + hop].copy(), "a": t_abs + i,
                                "stage": 0})
            gcat = np.concatenate([st["gdelay"], gt])
            gt = gcat[:frames]
            st["gdelay"] = gcat[frames:]
            prev_d = bool(st["prev_d"])
            st["prev_d"] = bool(gt[-1])
            rising = np.flatnonzero(gt & ~np.concatenate(([prev_d], gt[:-1])))

        layers = st["layers"]
        if not layers and not pending and rising.size == 0:
            # Nothing frozen, nothing starting: the neutral.
            if dry == 1.0:
                return {"out": src, "out_l": src, "out_r": src}
            out = (x * dry).astype(np.float32)
            return {"out": out, "out_l": out, "out_r": out}

        # --- pitch ratio (block mean, 1 V/oct, clipped before the power)
        cv = self._input_buffer(patch, buffers, module.id, "pitch_cv", collapse=False)
        cv_mean = 0.0
        if cv is not None and cv.size:
            cv_mean = self._finite_mean(cv)
        lim = self._FREEZE_PITCH_OCT_LIMIT
        ratio = float(2.0 ** min(max(pitch / 12.0 + depth * cv_mean, -lim), lim))

        # --- the stereo field's scale (block mean in float64 -- a float32
        # mean of a constant CV moves with the block size at the ulp).
        # Unpatched the mean is 0.0 and ``width`` is the knob, exactly.
        wcv = self._input_buffer(patch, buffers, module.id, "width_cv", collapse=False)
        if wcv is not None and wcv.size:
            wcv_mean = float(np.mean(wcv, dtype=np.float64))
            if not np.isfinite(wcv_mean):
                wcv_mean = 0.0        # scrub before the clamp: min/max pass NaN
            width = min(max(width + w_depth * wcv_mean, 0.0), 1.0)

        # --- staged captures: each is finished (its first frames made)
        # by its birth, the work spread evenly over the ``delay`` samples
        n_stages = self._FREEZE_CAPTURE_STAGES + (n + delay) // hop - delay // hop
        t_end = t_abs + frames
        for pc in pending:
            need = min(n_stages, -(-n_stages * (t_end - int(pc["a"])) // max(delay, 1)))
            while pc["stage"] < need:
                self._freeze_birth_step(pc, n, hop, delay, width, ratio, smear, seed)

        # --- new layers at this block's rising edges: each one cuts the
        # gate row of every older layer at its index (forced release).
        for i in rising.tolist():
            for old in layers:
                old["active"] = False
                if old["cut"] is None:
                    old["cut"] = i
            if len(layers) >= self._FREEZE_MAX_LAYERS:
                layers.pop(0)
            if delay:
                pc = pending.pop(0)
                while pc["stage"] < n_stages:
                    self._freeze_birth_step(pc, n, hop, delay, width, ratio, smear, seed)
                layer = pc["layer"]
            else:
                seg = full[i:i + n + hop]  # the n + hop samples before edge i
                layer = self._freeze_new_layer(self._freeze_capture(seg, n, hop), n, hop, 0)
            layer.update({
                # the read starts at frozen time ``n + delay`` = the edge
                # plus the birth delay, so a stationary input's hold is its
                # own continuation, in phase (frames 1..4 cover it from the
                # first sample)
                "m0": t_abs + i, "p_base": float(n + delay), "m_base": 0, "ratio": ratio,
                # the self-decay's rebase point (a knob turn folds the
                # fall so far into g_base and restarts the count)
                "decay": decay_s, "g_base": 1.0, "m_gbase": 0,
                "prev": False, "on": 0, "off": 0, "env_off": 0.0,
                "active": True, "born": i, "cut": None,
            })
            layers.append(layer)

        # --- render every layer, drop the ones that have faded out
        idx = np.arange(frames)
        wet = np.zeros(frames, dtype=np.float64)
        # the stereo accumulators exist only once a layer with channels
        # has been read, so a mono block's out_l / out_r ARE its out
        wet_l = wet_r = None
        drop_below = 10.0 ** -4.5      # -90 dB: the self-decayed layer is done
        keep = []
        for layer in layers:
            row = gt.copy()
            born = layer.pop("born", None)
            if born is not None:
                row &= idx >= born
            if not layer["active"]:
                cut = layer["cut"]
                if cut is None:
                    row[:] = False
                else:
                    row &= idx < cut
                    layer["cut"] = None
            env, on_c, off_c, env_off = self._gate_ramp_env(
                row, bool(layer["prev"]), int(layer["on"]), int(layer["off"]),
                float(layer["env_off"]), fade_n, fade_n)
            layer["prev"] = bool(row[-1])
            layer["on"], layer["off"], layer["env_off"] = on_c, off_c, env_off

            # read positions: an integer sample count since birth, times r
            m = np.maximum(0, t_abs + idx - int(layer["m0"]))
            if ratio != layer["ratio"]:
                m_now = max(0, t_abs - int(layer["m0"]))
                layer["p_base"] = layer["p_base"] + (m_now - layer["m_base"]) * layer["ratio"]
                layer["m_base"] = m_now
                layer["ratio"] = ratio
            p = layer["p_base"] + (m - layer["m_base"]) * ratio
            p_lo = int(np.floor(p[0]))
            p_hi = int(np.floor(p[-1])) + 1
            # keep the buffers' origin near the read -- never past the next
            # frame's start, so every frame still to come fits. Only the
            # LIVE span (up to the last frame's end) is copied, into fresh
            # zeros whose pages the coming frames fault in as they write:
            # the whole-buffer shift was a 3 x 2 MB spike at 65536, wide.
            origin = int(layer["origin"])
            if p_lo - origin >= layer["syn"].shape[0] // 2:
                shift = min(p_lo, int(layer["j_next"]) * hop) - origin
                if shift > 0:
                    j_w = int(layer["j_next"])
                    hi = (j_w - 1) * hop + n - origin if j_w > int(layer["j0"]) else 0
                    live = max(0, hi - shift)
                    for key in ("syn", "syn_l", "syn_r"):
                        buf = layer[key]
                        if buf is None:
                            continue
                        fresh = np.zeros(buf.shape[0], dtype=np.float64)
                        fresh[:live] = buf[shift:shift + live]
                        layer[key] = fresh
                    origin += shift
                    layer["origin"] = origin
            # generate frames until the buffer covers p_hi (frame j fills
            # frozen time [j*hop, j*hop + n); full overlap up to j_next*hop)
            self._freeze_frames(layer, p_hi // hop + 1, n, hop, width, ratio, smear, seed)
            syn, syn_l, syn_r = layer["syn"], layer["syn_l"], layer["syn_r"]
            wide = syn_l is not None

            # the self-decay: a per-sample factor from the integer count
            # since birth (rebased when the knob moves, so no jump)
            if decay_s != layer["decay"]:
                m_now = max(0, t_abs - int(layer["m0"]))
                if layer["decay"] > 0.0:
                    layer["g_base"] *= 10.0 ** (-3.0 * (m_now - layer["m_gbase"]) / (layer["decay"] * sr))
                layer["m_gbase"] = m_now
                layer["decay"] = decay_s
            g_last = 1.0
            if decay_s > 0.0:
                g = layer["g_base"] * 10.0 ** (-3.0 * (m - layer["m_gbase"]) / (decay_s * sr))
                env = env * g
                g_last = float(g[-1])
            elif layer["g_base"] != 1.0:
                env = env * layer["g_base"]

            i0 = np.floor(p).astype(np.int64) - origin
            frac = p - np.floor(p)
            val = syn[i0] * (1.0 - frac) + syn[i0 + 1] * frac
            if wide:
                if wet_l is None:
                    wet_l, wet_r = wet.copy(), wet.copy()
                wet_l += (syn_l[i0] * (1.0 - frac) + syn_l[i0 + 1] * frac) * env
                wet_r += (syn_r[i0] * (1.0 - frac) + syn_r[i0 + 1] * frac) * env
            elif wet_l is not None:
                wet_l += val * env
                wet_r += val * env
            wet += val * env

            done = (not row[-1]) and env[-1] == 0.0 and off_c >= fade_n
            if not done and g_last < drop_below:
                done = True        # decayed below -90 dB: nothing left to hold
            if not done:
                keep.append(layer)
        st["layers"] = keep

        out = (x * dry + wet * level).astype(np.float32)
        if wet_l is None:
            return {"out": out, "out_l": out, "out_r": out}
        return {
            "out": out,
            "out_l": (x * dry + wet_l * level).astype(np.float32),
            "out_r": (x * dry + wet_r * level).astype(np.float32),
        }
