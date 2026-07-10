# PySynthRack — Roadmap

Living list of what's next. Edit freely.

> Compacted 2026-07-03: the completed history — all of v0.1–v0.4 plus every
> shipped Later/wishlist and CV-coverage entry — moved **verbatim** to
> [TODO-ARCHIVE.md](TODO-ARCHIVE.md). Archived entries keep their follow-up
> notes; grep the archive before assuming an idea is new.

## Later / wishlist

- [x] **Pitch shifter — phase-coherent mix** — done 2026-07-10. The dry/wet
      `mix` dry tap is now delay-matched to the WSOLA engine's *exact* wet
      latency (`iw − rp/r`, measured to the sample) instead of an approximate
      one-grain guess that under-compensated ~50 ms at the defaults — so a
      partial mix (stacked harmony, few-cents detune-thicken) blends
      time-aligned signals instead of combing. New `_GrainShifter.latency()` +
      a `dry_tap` ring clamp; `test_mix_is_phase_coherent_at_unison` (fails on
      the old comp at corr −0.007, passes now); suite 1886. Surfaced during the
      review, still open:
  - [x] pitch_shifter: reconcile the `overlap` range — done 2026-07-10.
        Clamped the engine to 2..4 to match the UI slider + docstring (the
        out-of-range values were only reachable via hand-edited JSON, and
        overlap=1 was a degenerate no-overlap path). `test_overlap_clamped_to_2_4`
        locks it (1 ≡ 2, 8 ≡ 4).
  - [ ] pitch_shifter enhancement ideas (offered as "love" directions
        2026-07-10, not started — the mix fix was chosen): a `feedback` path
        for octave-cascade **shimmer** (freq_shifter-style block-safe
        feedback); a **harmonizer** — multiple simultaneous shift intervals
        and/or a stereo `out_l`/`out_r` detune spread.
- [x] **Error-handler integration** — done 2026-07-06/07. Upgraded the vendored
      `error_handler.py` to the upstream superset + vendored its 157-test suite;
      wired global crash logging (`_crash.install_crash_logging`: threading +
      unraisable hooks → `~/.pysynthrack/crashes/` via an observer, explicit
      sites guarded by `explicit_write`); GUI init now suppresses the traceback,
      logs to the folder, and exits non-zero. **Pending:** real-window eyeball
      of the suppressed GUI-crash path.
- [x] **Settings / layout persistence** — both slices shipped 2026-07-06.
      Slice 1: global `settings.json` (`%APPDATA%\PySynthRack`) persists buffer
      size across launches. Slice 2: per-patch window **size + position** in
      `patch.ui["window"]`, off-screen-safe restore (`ui/window_geometry.py` +
      viewport/Win32 glue in app.py; tests in `test_window_geometry`). **Caveat:**
      DPG 2.3.1 can't report maximized state, so maximized isn't captured (a
      maximized window still restores to full size+position). **Pending:**
      real-window eyeball for the visual restore + off-screen clamp.
- [x] **Buffer-size control** — shipped 2026-07-06. Toolbar "Buffer" slider
      (64/128/256/384/512/768/1024 frames, default 512), applied globally to
      the backend at Start; greys while running. `ui/buffer.py` helpers +
      `AudioBackend.set_block_size` (numpy record-only; pyo reboots its
      Server). Tests: `test_ui_buffer`, `test_backend_block_size`; suite green
      at 1690. **Pending:** real-GUI eyeball (no headless path builds the
      toolbar); verify the pyo Server reboot on a machine with pyo; optional
      cross-launch persistence (shared with zoom).
- [x] **Specific stereo speaker output** — shipped 2026-07-06 (all slices +
      live switching). A `stereo_speaker_output` clone with a `device` param
      that routes the sink to a named physical output (cue/monitor bus). Slice
      1: module + live device picker (drained to master, bit-exact). Slice 2:
      real per-device routing via a secondary `sd.OutputStream` per device fed
      by a GIL-atomic `deque` ring (`render_block_multi` splits master vs
      per-device buses; empty `device` = master, bit-exact). Live switch:
      `_sync_device_outputs` reconciler rebuilds only the affected stream on a
      `device` change while running — no Stop/Start. 40 tests, suite 1666.
      Caveat: two PortAudio streams aren't sample-clock-synced (the ring
      absorbs drift; monitor/cue bus, not phase-locked). Follow-ups: per-device
      underrun counter in the UI; warn when two sinks pick the same device
      (they sum); per-device gain trim / monitor-mix helper.
- [x] **Convolver** (IR reverb / cab) — COMPLETE, all three slices shipped
      2026-07-06. Slice 1: mono partitioned-FFT overlap-save core (oracle vs
      `scipy.fftconvolve`; one-block latency, dry-comped). Slice 2: IR file
      load (off-thread `_IRLoader`, Browse) + true stereo (per-channel engine).
      Slice 3: predelay + tone (wet-only), energy-normalise + length-cap on
      load, license-clean synthetic example IRs (generator script). Follow-ups
      (optional): switch the FDL `np.roll` to a ring-buffer write pointer if
      DSP% calls for it; bounded sliders in app.py for `predelay` (0..500 ms)
      and `tone` (1k..20k); `mix_cv` / `predelay_cv` (each with a depth param),
      a wet/dry latency report on the node, an optional zero-latency
      (first-partition-direct) mode, IR-load status/errors on the node, more
      example IRs (spring, cab).
- [x] **Tape** — shipped 2026-07-06 ("put it on tape": wow/flutter/drift
      pitch instability on a chorus-core modulated delay, tanh saturation on
      the shared 4x oversampling infra, calibrated hiss, ~60 Hz low-shelf head
      bump, mix with latency-comped dry; neutral bit-exact passthrough; exactly
      block-size independent incl. the seeded noise streams). Stretch /
      follow-ups from the brief: **Poisson dropouts** (seeded random
      level-drops for aging-oxide gaps); **stereo azimuth error** (small
      inter-channel delay/HF skew — would make `tape` a stereo `out_l`/`out_r`
      module like chorus); a **`vinyl` sibling** S-module (rumble +
      click/crackle + wow). Also possible: `wow_cv` / a `flutter`-rate knob
      (each new `*_cv` gets its depth param per the conventions).
- [x] **Bitcrusher** — shipped 2026-07-05 (bit-depth quantize + sample-rate
      decimation, seeded jitter wobble, mix, optional DC blocker; neutral
      bits=24 ∧ rate_div=1 bit-exact; every path exactly block-size
      independent). Possible follow-ups: a `bits_cv` / `rate_cv` input (each
      with the usual depth param) to modulate the crush from an envelope/LFO; a
      `sample_rate` readout in Hz alongside `rate_div`; an anti-alias
      (pre-decimation low-pass) toggle for a cleaner downsample.
- [x] **Frequency shifter** — shipped 2026-07-05 (Bode single-sideband:
      255-tap FIR Hilbert pair → analytic signal × complex sine; `out_up` /
      `out_down` sidebands, shift −2000..+2000 Hz, linear-Hz `shift_cv`, `mix`,
      `feedback` barberpole; 127-sample latency-matched dry; block independent
      even with feedback). Possible follow-ups: a `range`/`odd` barberpole
      variant that fixes the glide direction regardless of shift sign; internal
      LFO for hands-free shift sweep; stereo-decorrelated single-output mode; a
      mix-normal so an unpatched `out_down` folds back for a fatter mono. Pairs
      with the planned `fm_op` / `modal` as the inharmonic corner alongside
      `ring_mod`.
- [x] **Ring modulator** — shipped 2026-07-05 (`ring_mod`, Effects):
      `out = in × carrier`, external carrier or internal per-voice sine
      (freq 1..5000, freq_cv 1 V/oct × freq_cv_depth), mix=0 bit-exact dry.
      Pairs with the planned `fm_op` / `modal` as the inharmonic corner.
      Possible follow-ups: internal-carrier waveform choices (saw/square for
      buzzier sidebands); a `carrier_bias` knob to fade the original back in
      (AM ↔ ring-mod continuum); stereo out.
- [x] **Transient shaper** — shipped 2026-07-05 (threshold-free attack/sustain
      rebalance: two followers on `|in|`, their dB difference drives ±12 dB
      attack/sustain gains, `speed` fast/med/slow; attack=sustain=0 bit-exact
      passthrough; level-invariant; single row ≡ mono). Follow-up: optional
      `ui/app.py` fast/med/slow `speed` combo (renders as a text box until
      then); the DSP is done.
- [x] **Noise gate** — shipped 2026-07-05 (hold-and-hysteresis downward gate;
      threshold/hysteresis/attack/hold/release/range, sidechain, `open` gate
      CV out; single per-sample voice loop, block-size **bit-exact**).
      Possible follow-ups: vectorize the Schmitt/hold timer if profiled hot; a
      detector-mode toggle (peak vs ~10 ms RMS key); a `lookahead` so the open
      ramp can pre-empt transients; a de-ess mode (`sidechain` through a
      highpass).
- [x] **Limiter** — shipped 2026-07-04 (brickwall lookahead peak limiter:
      `ceiling`/`release`/`lookahead`, slope-limited lookahead anticipation +
      one-pole release, fixed latency = lookahead, bit-exact delayed
      passthrough under the ceiling; 25 tests, suite 1408). Possible
      follow-ups: **true-peak** limiting via the shared 4× oversampling infra —
      the deferred stretch goal; a `link` toggle to gain-reduce all voices
      together (master-bus feel) instead of per-voice; an optional
      gain-reduction `gr` CV out like the compressor's, for metering.
- [x] **Compressor** — shipped 2026-07-04 (feed-forward dynamics + external
      sidechain; peak/rms detector, soft knee, parallel `mix`, make-up, a `gr`
      CV out; ratio=1 ∧ gain=0 ∧ mix=1 = bit-exact passthrough; block-size
      independent; per-voice, single row ≡ mono). Stretch/follow-ups: lookahead
      (adds latency comp), program-dependent release, `ratio_cv`. Unblocks the
      **multi-band compressor** idea in `crossover.py` (split → 3× compressor →
      sum). Flagged: `gr` = linear `applied_gain − 1` (one reading of "0..−1
      scaled from dB"); `threshold_cv_depth` default 12 dB/unit.
- [x] **Vocoder** — shipped 2026-07-03 (channel vocoder: 8/12/16/24 bands,
      width/attack/release, hiss sibilance path, mix=0 bit-exact carrier).
      Possible follow-ups: stereo out (decorrelated odd/even bands), a
      `formant` band-shift knob (analysis centres offset from synthesis),
      carrier normal to `noise` when unpatched, per-band level trims.
- [x] **Filter vectorization** — **thread CLOSED 2026-07-03** (slice 6 verdict
      below; every per-sample biquad recurrence now runs in C). Originally:
      (optional — only if patches grow past current
      headroom). `_render_filter_voice` is the dominant remaining cost as a
      per-sample biquad loop. Decision (2026-06-09): use `scipy.signal.lfilter` —
      pure-numpy voice batching is already spent (the voice axis is vectorized; only
      the serial *time* loop remains, and lfilter is the one lever that moves it to C).
  - [x] Slice 1 — spike (sandbox, throwaway): lfilter vs the DF-I loop, zf→zi
        cross-block state. Equivalence bit-identical (max err ~1e-14, mono + 16-voice);
        speedup 17.5x mono, 46.2x voice (voice 17.1% → 0.4% of the 11.6 ms block
        budget, in-sandbox). Green — proceed.
  - [x] Slice 2 — add scipy to deps (pyproject/requirements); verify install on the
        3.12 build venv and that the PyInstaller exe still builds + how much it grows.
        Build is Matthew's to run → ends in a hand-off. **Closed 2026-06-10:** scipy
        installed in build venv, exe builds clean at 23.1 MB. Size delta deferred to
        slice 3 by construction — PyInstaller bundles only imported modules and nothing
        imports scipy yet; 23.1 MB is the baseline to compare against. **Measured
        2026-06-12: 54,206,720 bytes (51.7 MB) once scipy is imported — +28.6 MB
        on the 23.1 MB baseline, well under Matthew's ~256 MB budget.**
        **Claude's half done 2026-06-10:** `scipy>=1.11` added to pyproject + requirements;
        `build.ps1` pre-flight now checks scipy; specs need no change (PyInstaller has a
        built-in scipy hook, only `pyo` is excluded); cp312 win_amd64 wheel confirmed
        (scipy 1.17.1, ~36 MB wheel). **Pending Matthew:** `uv pip install scipy` in the
        build venv → `.\build.ps1` → note exe size delta → commit.
  - [x] Slice 3 — **shipped 2026-06-12.** `_render_filter_mono` → one lfilter call.
        Deliberate deviation from the spike's zf→zi carry (see WORKLOG): persisted
        state stays the raw DF-I history (x1,x2,y1,y2) — coefficient-independent,
        so per-block cutoff_cv coefficient changes behave exactly as the old loop —
        converted to the equivalent DF-IIt `zi` at block start (lfiltic identity,
        inlined) and read back off the buffer tails after. Result is *bit-identical*
        to the old loop: max err 0.0 after the float32 cast, across all modes,
        per-block CV sweeps, and frames=1 blocks. 7 new tests in
        TestFilterMonoLfilterEquivalence with the verbatim old loop as oracle;
        suite 410 (+18 mido). First production import of scipy — exe size delta
        becomes measurable at the next build (expected +30–40 MB on 23.1 MB
        baseline; budget ~256 MB).
  - [x] ~~Transient~~ — cleared 2026-06-12: `git push --force-with-lease` landed;
        local main == origin/main, junk history gone.
  - [x] Slice 4 — **shipped 2026-06-12** (same session as slice 3). `_render_filter_voice`
        → lfilter. Shared coefficients: one call filtering all 16 rows along the time
        axis, zi (V, 2). Per-voice cutoffs ((V,F) cutoff_cv): 16 single-row calls
        (lfilter can't vary coeffs across rows). Same raw-history state design as
        slice 3, vectorized — (V,) history arrays → broadcast zi conversion, identical
        code for scalar and (V,) coeffs. Bit-identical to the old loop (max err 0.0,
        all modes, macro + per-voice CV per-block sweeps, frames=1, mono↔voice
        reinit). Sandbox timing: shared 0.06 ms/blk (~33x vs 1.98), per-voice
        0.19 ms/blk (~10x). 9 new tests in TestFilterVoiceLfilterEquivalence with
        the verbatim old voice loop as oracle; suite 419 (+18 mido).
  - [x] Slice 5 — **shipped 2026-07-03.** Crossover cascade → per-stage lfilter
        (4 calls: LP1/LP2/HP1/HP2), NOT one sosfilt over the 2-section cascade —
        sosfilt can't return the intermediate stage signal whose tails are the
        coefficient-independent DF-I history (recovering it from zf divides by a2
        and costs bit-exactness). Same raw-history state design as slices 3/4,
        keys unchanged. Bit-identical on noise; pure-sine high branch drifts
        ≤ ~5e-13, confined below ~−130 dBFS (the ADSR-rewrite float64
        reassociation class; tests pin < 1e-6 + drift confinement). Sandbox
        timing: mono 7.1x, voice 34.2x — the old voice cascade was 60.9% of the
        11.6 ms block budget, now 1.8%. 9 new tests in
        TestCrossoverLfilterEquivalence with the verbatim old loops as oracles;
        suite 1315 sandbox (+18 mido).
  - [x] Slice 6 — **shipped 2026-07-03 (close-out).** Native re-profile on both
        Windows boxes at 24287c0. Main machine (py 3.12.13/np 2.4.5): worst block
        12% of budget, 0/8000 over — like-for-like vs the 2026-06-07 close-out,
        mean 29–33% → 4.9–8.9%, worst 42% → 12%. Oldbeast (py 3.14.4/np 2.4.6):
        means 33–64%, p99 under budget everywhere, but blep-scenario tail spikes
        breach (worst 121%, 4/8000 over) — a capacity question for that box, not
        a filter question (see Later item). Verdict: filter vectorization DONE,
        thread closed; pyo ladder stays resolved at step 2 on the primary box.

- [x] ~~`_render_audio_to_cv_voice` per-sample Python loop~~ — **shipped 2026-07-03**
      (Matthew's pick). Monotone pattern fixed-point solve (exact on convergence, loop kept
      as fallback + oracle): voice 3.9x (10.9% → 2.8% of block budget), mono 1.2x,
      bit-identical after the float32 cast. 35 equivalence tests; suite 1292 sandbox.
- [ ] **Module ideas backlog** — see [docs/MODULE_IDEAS.md](docs/MODULE_IDEAS.md)
      (written 2026-07-04: ~26 paste-ready specs + quick hits across dynamics,
      generative, new voices, character FX, visualization). Pick items into
      this list as they're chosen; suggested first five at the bottom of the doc.
