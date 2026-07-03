# PySynthRack — Roadmap

Living list of what's next. Edit freely.

> Compacted 2026-07-03: the completed history — all of v0.1–v0.4 plus every
> shipped Later/wishlist and CV-coverage entry — moved **verbatim** to
> [TODO-ARCHIVE.md](TODO-ARCHIVE.md). Archived entries keep their follow-up
> notes; grep the archive before assuming an idea is new.

## Later / wishlist

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
      as fallback + oracle): voice 3.9x (10.9% -> 2.8% of block budget), mono 1.2x,
      bit-identical after the float32 cast. 35 equivalence tests; suite 1292 sandbox.

- [ ] Oldbeast real-time headroom (only if it becomes a real playing target):
      2026-07-03 profile at 24287c0 — means 33–64% of budget, p99 fits in every
      scenario, but blep-scenario tail spikes breach (worst 121.1%, 4/8000 blocks
      over). Levers, cheapest first: block size 512→1024 (doubles the budget),
      voice cap, a 3.12 venv. Note pyo can't install there anyway (3.14, no
      Windows wheels past 3.12, no MSVC). Filter/ADSR are not the cost — the
      remaining per-block time is osc paths + voice infrastructure.

- [ ] Patch presets palette (factory + user banks)

- [ ] Undo / redo on patch edits

- [ ] App icon for the packaged `.exe` -- add a `.ico` and reference it from `pysynthrack.spec` (EXE(icon=...))

- [ ] Code-signed build -- removes the SmartScreen "unrecognized publisher" prompt; only worth it if the synth ever leaves the hobby circle

## Follow-up threads extracted from archived entries

The recurring threads hoisted from shipped entries' follow-up notes; full
context under the matching entry in TODO-ARCHIVE.md. (Each archived module
entry also carries its own smaller follow-ups list.)

- [ ] Resampler: anti-alias LP before big up-shifts; `pitch_cv` could
      normal to a constant; WSOLA-style seam-position search (a blind
      crossfade can hit a brief anti-phase dip on tonal material).
      (~~window-size / low-latency param~~ — shipped 2026-07-03 as the
      `window` param, 20–2000 ms.)
- [ ] PitchShifter: vectorize the per-voice grain search if profiled hot;
      transient detection to sharpen attacks.
- [ ] Sample-and-hold: `slew` param; track-and-hold mode; normal `in` to the
      noise source (flagged in both the S&H and Noise entries).
- [ ] Meter round 4 (maybe): true gated *integrated* LUFS (400 ms block
      history + absolute/relative gates); loudness-range (LRA); numeric
      true-peak (4x oversampled) readout.
- [ ] FilePlayer: node hint showing whether ffmpeg was found.
- [ ] Exe slimming: pytest is baked into the 85.1 MB build — prune the
      spec's collected modules.

