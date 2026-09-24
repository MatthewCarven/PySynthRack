# PySynthRack — Roadmap

Living list of what's next. Edit freely.

> Compacted 2026-07-03: the completed history — all of v0.1–v0.4 plus every
> shipped Later/wishlist and CV-coverage entry — moved **verbatim** to
> [TODO-ARCHIVE.md](TODO-ARCHIVE.md). Archived entries keep their follow-up
> notes; grep the archive before assuming an idea is new.

## The possibility bridge (opened 2026-08-15)

PythonBinaryPossibility's rack semantics as a module family. First brick
shipped; the rest queue behind Matthew's ears.

- [x] **`possibility_seq`** — shipped 2026-08-15: steps 0/1/?, modes
      loop/latch/dice, `balanced` shuffle-bag deal (the measured fix for
      coin-clump: one fair bar in fifteen audibly lopsided without it),
      seeded takes, `reroll` gate. Renderer pinned against the pure
      `collapse_pattern` reference; 20 tests; possibility_groove example.
      Full pytest RUN on the real checkout 2026-08-20: **2666 passed,
      1 skipped** — green, so the sandbox-partial-checkout caveat is
      cleared. **EARS PASSED 2026-08-20** — Matthew: "exactly what I was
      hoping for".
- [x] **Panel gesture** — SHIPPED 2026-08-20, after Matthew's ears on
      `possibility_groove.json` ("exactly what I was hoping for") and his
      "advance the panel". Sixteen colour-coded step cells replace the 36
      generic rows; click cycles `0 → 1 → ? → 0`, right-click opens that
      step's odds slider, hover explains the cell in words, steps past
      `steps` grey out, and the possibility readout (`4 ? -> 16 possible
      bars`) sits under the row. Cycle/count logic lives dpg-free in the
      module (`next_state`, `undecided_count`, `possibility_count`,
      `format_possibilities`) so panel and tests share one truth;
      20 tests; suite **2686**. **EYEBALL PASSED 2026-08-21** — Matthew
      recompiled and played it: "works really well :-{D". The
      right-click popup and the hover tooltip do get along on one
      widget (the open question headless testing couldn't answer).
- [x] **Param widgets didn't write the model until audio had run once**
      — FIXED 2026-08-21 on Matthew's yes ("yes for the pre-start param
      fix please"). Every knob/slider/combo/fader/transport edit made
      before the first **Start audio** was silently discarded, and a save
      right then wrote the OLD values: `App._on_param_changed` called
      `backend.set_param` alone, and `NumpyBackend.set_param` returns
      early while `self._patch is None` — which it is until Start
      compiles a patch in. All **ten** call sites now route through
      `App._set_module_param` (model first, backend notified after) — the
      generic callback plus `_on_fader_pitch`, `_on_fm_ratio_changed`,
      `_on_buffer_size_changed`, `_on_file_transport`, the WAV dialog,
      the playlist advance, and both velocity-curve writers. 13 tests
      including a save-round-trip on the headline data loss and a
      **tripwire** that counts `backend.set_param` call sites in the UI
      source, so a new callback can't quietly reintroduce it. Rule
      written into docs/architecture.md. Suite **2699**.
- [x] **`possibility_selector` — the selector, meta-possibility version —
      SHIPPED 2026-09-18, module #93** (Matthew: "the meta-possibility
      selector — collapse which module fires, the genuinely novel one").
      Spec written into MODULE_IDEAS.md and built to it the same day. A
      clocked 1-to-4 gate router whose routes can be undecided: each
      step's state is the SET of outputs it may go to — `"1"`–`"4"`
      decided, `"?"` any, a digit subset (`"13"` = kick or hat) some,
      `"0"` a rest — with possibility_seq's take model verbatim
      (loop/latch/dice, memo per take, reroll, reset, seed) and a
      weighted draw over the candidates (`weight1..4`, how the open
      steps lean; 0 removes an output from every `?` without touching a
      decided step). `balanced` generalises the coin's bag to a hand of
      cards: a fair open step is dealt to the least-used candidate —
      PBP's `RandomGeneratorPerfect` selector, which is where the name
      comes from. Two stepping contracts: `clock` patched = the
      sequencer reading (route k belongs to tick k, `in` says whether
      anything passes → a possibility_seq gate into `in` is WHETHER x
      WHICH); `clock` unpatched = the distributor reading (`in`'s own
      edges step it, route k belongs to the k-th hit); `in` unpatched
      routes the clock itself. Panel = the possibility panel one level
      up: cells coloured per route, click cycles `0→1→2→3→4→?→0`,
      right-click = four checkboxes writing the canonical subset string,
      `? leans to` sliders, the readout (`8 ? -> 65,536 possible bars`
      out of the box). Renderer pinned bit-exact against the pure
      `collapse_routes` reference in loop / balanced / dice; 56 tests
      across `tests/test_possibility_selector.py` (35) and
      `tests/test_selector_panel.py` (21). Example
      `possibility_selector_kit.json` (banked): whether x which drums +
      a four-string harp stepping on its own hits. Suite **3649**, 126
      examples, 93 modules. Test lessons: the clock's float phase lands
      an edge a sample apart between block sizes — compare a graph
      render's hit ROUTES as a sequence, keep the module's own
      block-size pin on hand-built pulses; a pulse train with an
      `offset` needs the reads offset too. **EYES wanted:** the route
      colours and the four-checkbox popup in a real window.
- [x] **Example: reroll divider** — SHIPPED 2026-09-18 as
      `examples/possibility_reroll_divider.json`. Not a second free-running
      clock but a [`clock_divider`](docs/MODULES.md#clock_divider) chain
      off the SAME sixteenth clock (`divn` 16 = a bar, then `div4` = four
      bars), so the re-deal lands on the downbeat by construction rather
      than by two clocks happening to agree. Three `latch`ed patterns:
      kick and snare hold a take for four bars and re-decide at bar 5; the
      hat re-deals every bar off the first divider (balanced, eight hits a
      bar, every bar). Three tests pin it (reroll edges ARE clock edges 0,
      64, 128; kick/snare identical inside each four, different across;
      hat 8/16 every bar and >= 6 distinct bars in 8). Test lesson: the
      clock's first edge is sample 0 of block 0 -- a pairwise edge detector
      misses step 1 and every bar reads one step late. Banked for ears.

## Planned — the quick-hit run (Matthew's pick, 2026-08-03)

Seven modules, full specs promoted into docs/MODULE_IDEAS.md ("The
quick-hit run" section — ports/params/DSP/tests each). Proposed as
three sessions per the working agreement; each module gets the full
polish standard (tests, example, MODULES.md entry, tripwires green).

- [x] **Session A — utility sweep (S×3)** — ALL SHIPPED 2026-08-03
      (same day as the plan). `logic`: gate algebra, five jacks live,
      zero params (comparator dropped — strict port kinds, schmitt's
      lane); unpatched-b + normalled-NAND contracts pinned. `mid_side`:
      sum/difference exact, width 0..2 + clamped width_cv, one-input =
      level-preserving mono passthrough (NOT half-level L+0 — design
      call). `octaver`: cumsum-parity flip-flops (÷2/÷4, no per-sample
      loop), audio_to_cv follower core (5/50 ms) gates the subs, tone
      one-pole, dry-only returns the input buffer itself. 33 tests;
      suite **2551**; examples `logic_offbeat_drums` (and=tresillo
      kick, xor=complementary hat), `mid_side_breathe` (LFO width),
      `octaver_bass_lead` (keys → bass under the lead). **Ears PASSED
      2026-08-05 on all three** — and `mid_side_breathe` was the
      sleeper hit: "pretty awesome, ima use this in music for sure."
      Later: logic 3-in variant? mid_side `side` HP trim; octaver
      glide/portamento tracking aid.
- [x] **Session B — patch bay & dust (M+S)** — BOTH SHIPPED 2026-08-04
      (same day as organ+chaos; Matthew: "And another two please").
      `matrix_mixer`: feedback landed on the EXISTING delayed-edge
      mechanism (the governor `fill` pattern generalized) —
      `_compute_late_edges` BFS at compile, `_is_delayed_edge` now
      instance-method, previous-block seed before the walk, fresh
      stash after; one-block loop latency pinned via geometric
      staircase; soft ceiling = transparent-below-0.95 + C1 tanh to
      1.0 (NOT plain tanh — 8% off a 0.5 signal was wrong for a
      default-ON mixer; noted deviation), so identity-bit-exact AND
      guardrail-on are both true; runaway pinned at ceiling / >100
      unbounded with it off; NOTE the ±1 gain clamp means loop gain
      >1 needs parallel return rows (documented). `vinyl`:
      absolute-window seeded noise (any block split = identical
      dust, bit-exact all-vices-on), Poisson crackle, 40 Hz RBJ
      rumble (LF/HF ~700), 0.55 Hz wobble measured at 0.559 Hz via
      Hilbert inst-freq (±24 ct full); all-zero = the input buffer
      itself; unpatched in = free noise bed (deviation). Perf: 1.0% /
      1.3% budget. 27 tests; suite **2616**; examples
      `matrix_feedback_echo.json` + `vinyl_dust.json`. **Ears PASSED
      2026-08-05** (matrix "unique"; vinyl "does exactly what it
      says"). Later: the organ-feedback example (queued above);
      shimmer example through the matrix; wobble/crackle cv ins.
- [x] **Session C — oscillator double (S–M + M)** — BOTH SHIPPED
      2026-08-04 (day part three; the QUICK-HIT RUN IS COMPLETE:
      A+B+C, seven modules in two days). `supersaw`: one blep call on
      (V,7,F), asymmetric table ±50 ct, per-(slot,saw) seeded free
      phases, RMS-normed blend, equal-power spread (0 → outs
      bit-identical, pinned); blend 0 = detune-inert center saw
      (pinned); 16-voice worst case RECORDED 4.87 ms = **45.6%**
      budget (112 blep saws — documented "spend it on the pad";
      fast-path idea queued below). `wavetable_morph`:
      `_bandlimit_frames` renders harmonic frames into per-octave mip
      stacks (one normaliser per frame — crossfade never pumps);
      analog stack endpoints + thirds land pure shapes (pinned);
      vowel + metallic stacks; single-cycle WAV import via the shared
      Browse dialog (rfft bins = harmonic series; bad path falls back
      bit-equal); +3 oct alias floor > 40 dB down (pinned);
      `position_cv_depth` added per conventions (spec omitted it —
      noted). 27 tests; suite **2645**; examples
      `supersaw_chord_wall.json` (0.66 peak) +
      `wavetable_vowel_talk.json` (0.48 peak). **Ears PASSED
      2026-08-05** (supersaw "awesome"; wavetable_morph "works
      well"). Later: supersaw fast path (arange constant-pitch /
      (V·7,F) reshape) if 45.6% bites; multi-frame WAV import (file
      → sliced frames); Browse-button close-look not separately
      exercised (shared dialog path).

## The sampler (opened 2026-08-22)

Spec in docs/MODULE_IDEAS.md § "New voices"; three slices, Matthew's pick
once the board cleared.

- [x] **Slice 1 — the voice** — SHIPPED 2026-08-22. `sampler` (Sources):
      `pitch_cv` + `gate` in, `out`; whole-file background load
      (`_SampleLoader`, the convolver's `_IRLoader` precedent), per-voice
      float64 playheads, 4-tap Hermite read (`_hermite4` verbatim from the
      resampler), `root`/`tune`/`fine`, `one_shot`/`gated`, `start`/`end`
      region, attack/release **declick ramps** (not an envelope — patch an
      adsr→vca for shaping), ~2 ms retrigger crossfade, `level`.
      **The neutral is bit-exact**: root pitch + full region + attack 0 +
      level 1 → rate exactly 1.0 → integer positions → the decoded file
      sample-for-sample; an octave up is a bit-exact `[::2]`. Block-size
      independent, pinned. 28 tests + 6 for the new `name_to_midi`;
      suite **2888**. Example `sampler_breaks.json` (three samplers on
      three regions of one loop, euclidean 4/2/6 — a breaks machine built
      out of `start`/`end` alone) with `examples/samples/
      generate_samples.py` making the audio (the `examples/irs/`
      precedent: generator in git, wavs ignored). **EARS PASSED
      2026-08-29** — Matthew: "definitely works now... and works well",
      after the media path fix (`aed768c`); the first attempt that day
      heard nothing and that was the path bug, not the module. The
      voice-not-a-transport design holds up in play, which was the whole
      argument for it being a new module rather than a `file_player`
      mode. Slices 2 and 3 are unblocked.
- [x] **Slice 2 — loop mode + the region UI** — SHIPPED 2026-08-30.
      `loop` joins the `mode` combo: `gated` underneath, but the playhead
      wraps `loop_end` back to `loop_start` while held. `loop_start` /
      `loop_end` are fractions **of the region**, not of the file, so
      moving `start`/`end` carries the loop along instead of stranding it;
      the playhead still starts at `start`, so everything before
      `loop_start` plays once as the attack. The wrap is one modulo on the
      existing affine position array, which keeps it exact on integers:
      **a unity-rate loop with `loop_xfade` 0 is a bit-exact tiling of the
      file**, so slice 1's spine survives and "period exactly the loop
      length" is an `array_equal`. `loop_xfade` **0..100 ms** (spec said
      1..100 — 0 is needed for the xfade-off A/B the spec's own test list
      asks for, and is a real setting besides), crossfading into the
      *previous lap*; on `pad_c4.wav` that takes the seam step 0.285 ->
      0.035, below the file's own natural 0.158. Inverted loop region
      plays as `gated` (not silence — it is a draggable slider). Three
      bounded sliders. 15 tests, suite **2984**. Example
      `sampler_mellotron.json` (keys -> loop sampler -> reverb) with a new
      sustaining `pad_c4.wav` in `generate_samples.py`. **Wants ears.**
- [x] **Sampler `mode` dropdown offered the FILTER's modes** — found and
      fixed 2026-08-30, a slice-1 defect. The shared `mode`/`mode_neg`
      combo branch in `_add_param_widget` runs before the per-TYPE blocks
      and its `else` hands out `FILTER_MODES`; the sampler's own block sat
      below it and was shadowed. So `gated` was **unreachable from the
      GUI** for all of slice 1 (the renderer rejects an unknown mode and
      falls back to `one_shot`), and `loop` would have been too. Survived
      the ears pass because `sampler_breaks.json` sets `one_shot` in JSON,
      and survived the tests because slice 1's sweep asserted
      `"mode" in combos` — which the *filter's* combo also satisfies.
      Second occurrence of this shape (`cv_to_frequency` did it until
      2026-06-07). A registry audit found the sampler was the only
      casualty; the other 14 are correct. New tripwire
      `tests/test_mode_combos.py` walks the registry and asserts every
      module's `mode` dropdown offers that module's own default — verified
      to fail on the pre-fix code. **Wants eyes** on the dropdown.
- [x] **Slice 3 — the stretch menu** — SHIPPED 2026-09-11, all six:
      `out_l`/`out_r` (per-channel passthrough bit-exact; a mono file is
      one read feeding all three outs); an on-load **mip chain** behind an
      `antialias` tickbox (default OFF — the crunch stayed the sound;
      2:1-decimated half-band levels, float32 above level 0, crossfaded
      between octaves so a glide never steps bandwidth — the fold is gone
      at an exact octave, ~8 dB down at a fifth, pinned as A/Bs; the
      neutral stays bit-exact with the box ticked); **`start_cv`** +
      `start_cv_depth` (−1…1, file/unit), read AT THE GATE EDGE and
      latched per hit, loop rides along per voice, a start past `end`
      drops the hit and leaves the sounding voice alone; **`reverse`**
      (starts at `end − 1`, unity is `data[::-1]` bit-exact, reversed loop
      tiles the region backwards, seam mirrors into the lap after);
      **`vel`** (latched at the edge, the retrigger tail keeps the old
      gain) plus a new **`velocity_cv` OUT on `midi_input`** so it has a
      source; a **waveform face** on the node (loader-built 200-column
      overview via a `sampler_overview` hook, start/end + loop markers,
      repaints only on change). Example `sampler_scrub.json`. 37 tests;
      suite **3027**. **Wants ears** (scrub), **eyes** (the face), and a
      real keyboard on `velocity_cv → vel`. The sampler is
      feature-complete against its spec.
- [ ] **EARS (meatthread0, banked 2026-09-11): `sampler_scrub.json` +
      `sampler_mellotron.json` + `drum_dynamics.json` +
      `modal_mallets.json` + `clock_divider_swing.json`** — Matthew:
      "bank sampler_scrub.json in my todo for now, and I'll test when I
      can do it properly, where I am is noisy." All five unheard (the
      modal one also wants eyes on its xy scope — the spread should draw
      a cloud, not a line). Run `python examples/samples/generate_samples.py`
      first. While there: eyes on the sampler node's waveform face and the
      new `reverse` / `antialias` tickboxes, and `gated` in the mode
      dropdown (never once selectable before 08-30). Unblocks nothing —
      the sampler is feature-complete; this is confirmation.
      **Added 2026-09-14:** `pitch_shifter_shimmer.json` (the built-in
      octave bloom — is the 6 kHz loop damping right, or does it want a
      knob?) and `pitch_shifter_harmonizer.json` (stereo triad — does
      the hard pan feel wide or just split?). Same session, same bank.
      And `chord_legato_inversions.json` (does the one-sample gate drop
      on `retrig` read as a clean re-articulation through the ADSR, or
      does it want a longer gap?). And `slew_clocked_glide.json` — change
      the clock's BPM while it plays; the glide should keep its fraction
      of a step. And `organ_leslie.json` (2026-09-14, module #91) — the
      pairing: does the drum's lag behind the horn read as a Leslie, or
      does it want the ramps trimmed? Is 0.7 depth / 0.8 spread the
      right default feel? **Added 2026-09-15:** `granular_cloud.json`
      (module #92, slice 1) — a pluck melody with an octave-up grain
      cloud 300 ms behind it. The synchronous train puts a sideband
      within ±30 Hz of the octave on held notes: does that read as "the
      granular sound" or as out of tune? (Slice 2's spray smears it.)
      And is `hann` at 25 × 80 ms — transparent — the right default,
      or should a fresh node sound granular out of the box?
      **Same day, slice 2:** `granular_haze.json` — the cloud proper
      (async, ±0.44 s position scatter, ±25 ct, full width). Does the
      constant-peak pan law read as wide or as lumpy? Is `spray_time` 1
      too random for a melody, and where does the sideband vanish
      (0.3?)? Try `seed` — is a different cloud audibly different?
      **2026-09-16, slice 3:** `granular_freeze.json` — play, **tap F**,
      the last 2 s hang while the LFO scans them; tap again. Does the
      freeze edge click (grains at the head hold their last sample)?
      And `granular_beat_repeat.json` — 2 s recording / 2 s held and
      re-cut at sixteenths; is `mix` 0.6 the right balance, or is the
      `mix` 1 "only the repeats" version the one? **Also re-listen to
      `clock_divider_swing.json`:** its LFO → `fills_cv` cable was
      wired to a port the LFO doesn't have (`out`; it is `cv`) since
      it shipped on 09-11, so the "fills breathe 2 → 6" never
      happened. Fixed 09-16; the snare pattern should now thicken and
      thin over ~30 s.
- [x] **`granular` — SLICE 3: freeze + position_cv — SHIPPED
      2026-09-16** (Matthew: "granular slice 3 please"). **The module is
      complete against its spec.** New: `freeze` gate port + `freeze`
      bool param (ORed, the resampler's `brake` idiom; the gate is
      per-sample so the held content is block-size independent) ·
      `position_cv` port + `position_cv_depth` (−1..1, buffer fractions
      per CV unit, default 1; read at each grain's onset and latched;
      a (V, F) CV averaged; sum clamped 0..1). Design: the ring is
      indexed in CAPTURED time (`state["c"]`, advances only while live)
      while onsets run on wall time (`absw`); per block `hw[j] = c0 +
      cumsum(live)[j] − 1` is the last-captured index as of sample j;
      the input is written only at live samples; a grain fired at j
      reads from `hw[j] − D`; frozen-at-onset grains need `rate × (L−1)`
      of head start (the head isn't moving); every read is clamped to
      `hw − 2` per sample (a no-op live, the HOLD when a freeze lands
      mid-grain — a ramp test shows no stale burst); the dry side of
      `mix` moved from a ring read to a 2-sample tail of the live
      input (bit-identical live, correct while frozen); a release
      resumes recording at `c` — captured time is continuous, no hole.
      **All 36 slice-2 reference arrays (six param sets × two block
      sizes × three outs, captured before editing) unchanged
      bit-exact.** 15 new tests (65 for the module): toggle stops `c`
      and tiles the last grain (periodic at the hop, bit-exact); gate
      ORs with toggle; a mid-block gate edge is bit-exact 64-vs-512 and
      `c` stops on the edge sample; captured-time continuity (`y[n] ==
      captured[c(n) − 24000]` after a freeze/release, bit-exact; the
      held buffer periodic during; the plain delay before); mid-grain
      hold vs stale; frozen-from-empty silent + block-exact; +12 frozen
      head start block-exact (toggle on a shared boundary); dry live
      while frozen; position_cv DC == position bit-exact; a step
      latches per grain; depth 0 / −1; (V, F) averaged + clamped; an
      LFO scrubs a frozen buffer (not a static loop); both examples.
      Suite **3416**. Two examples: `granular_freeze.json` (key_trigger
      F latch + LFO scan) and `granular_beat_repeat.json` (drum
      machine, 125 ms slices, shift_random re-cut, 15 BPM freeze
      clock). **Found on the way:** `Patch.from_dict` appends cables
      without checking the ports exist (`connect` does), so a typo'd
      cable in a saved patch loads clean and is silently inert — my
      beat-repeat wired a clock's non-existent `gate` and the freeze
      never engaged while the examples sweep passed. Added
      `test_example_cables_land_on_real_ports` to the sweep; it
      immediately caught `clock_divider_swing.json`'s dead LFO cable
      (fixed). The loader itself is below.
- [ ] **`Patch.from_dict` accepts cables to ports that don't exist —
      silently inert.** Found 2026-09-16 (above). `Patch.connect`
      validates; the JSON loader doesn't, so a hand-edited or
      version-drifted patch can carry a dead cable that renders as
      "nothing happens". Same shape as the media-path bug: fail soft
      with nothing to say. Options: (a) validate in `from_dict` and
      raise (breaks loading old patches after a port rename — bad);
      (b) drop the cable and REPORT it (status bar: "2 cables dropped:
      lfo#4.out → euclidean#3.fills_cv", the media-path precedent);
      (c) keep the cable in the model, flag it in the node editor
      (dashed/red) so a port rename is visible and re-cableable. (b)
      or (c); (c) is the one that helps the user fix it. Own small
      session. The examples are now guarded by the sweep tripwire.
- [x] **`granular` — SLICE 2: sprays, seed, stereo — SHIPPED
      2026-09-15** (Matthew: "granular slice 2 please", same day as
      slice 1). New params, ALL OFF at their defaults: `spray_time`
      0..1 (each interval = hop × a factor in 1 ± s; mean density
      preserved) · `spray_pitch` 0..1200 ct (±, sum clamped ±36 st) ·
      `spray_pos` 0..1 (± position units, clamped to the buffer) ·
      `width` 0..1 (per-grain pan, constant-peak `min(1, 1 ∓ pan)` so
      a centred grain is unity in both) · `seed` int (1). New ports
      `out_l` / `out_r` (dry centred; `out` hears every grain at
      unity). Grain *i* draws four uniforms from
      `default_rng([seed, i])` — interval, position, pitch, pan — so
      the cloud is a pure function of (seed, grain index): reproducible
      and block-independent to the bit; the pending grain's draws are
      cached across blocks. No draws at all while everything is zero →
      **the four slice-1 reference renders (two block sizes) are
      unchanged bit-exact**. Cap raised 64 → 96 in flight (spray_time
      clumps). 13 new tests (50 total): no draws + L is R is out at the
      defaults; same seed same cloud / different seed different;
      sprayed 64-vs-512 bit-exact on all three outs; intervals within
      [1−s, 1+s]·hop with the mean at hop; full spray merges some
      grains (fired vs runs seen — a run detector can't see an interval
      shorter than a grain); impulse copies land across position ±
      spray of the buffer (with 500 ms grains so they catch it) and at
      exactly position·buffer without spray; per-grain pitch ratios
      span ± the cents, each exact; constant-peak pan law per isolated
      grain; `out` bit-identical with width on or off; mix 0 dry on
      all three; the haze example plays in stereo. Suite **3275**.
      Cost ~2% sprayed, ~10% at the clumped extreme. **Slice 3**
      (open): `freeze` (gate + toggle — stop writing, keep reading;
      `position` becomes a scrub across the held buffer) +
      `position_cv` (voice-summed CV on position, read per grain at its
      onset — the sampler's edge-latch rule) + examples.
- [x] **`granular` — grain cloud, SLICE 1 — SHIPPED 2026-09-15, module
      #92** (Matthew: "granular slice 1 please" — the front of the build
      queue). Effects: `in` (voice sources summed — one buffer) → `out`.
      Params `buffer` 0.5..10 s (2) · `density` 0.5..100 /s (25) ·
      `size` 10..500 ms (80, rounded even) · `pitch` ±24 st · `position`
      0 (now)..1 (`buffer` s ago) · `window` hann|triangle|expo · `mix`.
      Ring capture at ABSOLUTE indices; a synchronous scheduler fires a
      grain every `sr/density` samples (float hop, carried exactly);
      each grain is `(onset, start, rate, len, shape, amp)` FROZEN at
      its onset so a knob reaches the next grain only; every grain in
      flight rendered as one `(G, F)` matrix op (Hermite read × window,
      masked, summed in spawn order — bit-exact block independence
      falls out); head start `2 + (rate−1)(L−1)` floors `position`
      (grain shortened if `buffer` is too short); level `1/max(1,
      density·size·w_mean)`; dry read from the ring 2 samples late.
      37 tests: model/walls; unpatched/silent/zero frames; **the
      neutral is BIT-EXACT** (hann at 50% tiles to one: out == in
      delayed 2); position 0.25 == 24000-sample delay bit-exact;
      fractional position lands an impulse; mix 0 bit-exact dry; mix
      0.5 the average; pitch exact INSIDE a grain (ZC period on the
      onset grid, ±12/+7/−5, 0.1%) and within ±density Hz ACROSS grains
      (the sideband, pinned as such); density counted as runs, size as
      run length, hop 48000/17 carried across 64-vs-512 exactly, a
      density rise lands within one new hop; dense hann/triangle on DC
      == 1.0 exactly, expo within 1%, sparse at natural level; the
      three window shapes; +24/500 ms causal (64 vs 512 bit-exact),
      0.5 s buffer shortens the grain; a pitch change leaves both rates
      in flight; a buffer resize keeps the history (the 48000-sample
      identity holds after it); block-size bit-exact with fractional
      everything + expo; voice sums; widgets (window combo = the
      module's, every param its own widget); the example plays. Suite
      **3260**. Cost ~1.5% at defaults, ~9% at 100/s × 500 ms.
      **Test-construction lessons, all four first-run failures:** a run
      detector is fooled by a tone's near-exact zeros (use the onset
      grid); the first grain reads two unwritten zeros (skip hop 0); a
      2× read has stride 2, so a one-sample impulse on the wrong parity
      is never read (the sampler's `[::2]` property — use two samples);
      check in-flight state a few blocks after a change, not at the end.
      **Slice 2** (open): spray scheduler — `spray_pos`, `spray_pitch`,
      `seed`, jittered onsets (per-grain `rate`/`start` are already
      vectors; the window is evaluated per grain by shape already) +
      stereo `width` with `out_l`/`out_r`. **Slice 3** (open): `freeze`
      gate/toggle + `position_cv` + examples. `seed` deferred to slice
      2: nothing in slice 1 is random (deviation from the MODULE_IDEAS
      cut, noted).
- [x] **`rotary` — the Leslie — SHIPPED 2026-09-14, module #91**
      (Matthew's pick off the build queue, the organ's partner since
      08-05). Effects: `in` (voice sources summed — one cabinet) + `fast`
      (gate: the switch while patched, majority level per block) →
      `out_l`/`out_r` + `out` (L+R)/2. LR4 crossover (the crossover
      module's `_crossover_coeffs`, zf-carried) → horn band / drum band
      → each into a delay ring read once per mic at
      `base − (r/c)·depth·cos θ` (a real Doppler: horn r 19 cm → 0.55 ms
      swing → ±2.3% at 6.7 Hz; drum 14 cm) and scaled by
      `1 − am·depth·(1 − cos θ)/2` (horn 0.8, drum 0.45). Rotor rates
      approach slow/fast/0 through a one-pole (lfilter, exact
      recurrence) with separate up/down taus (horn 1/1.5 s, drum 4.5/6 s,
      × `ramp`); the drum runs 0.85× and counter-rotates. Two mics at
      ±`spread`·90°. Integer centre delay so the delay-matched dry read
      is exact. 24 tests: model/walls; unpatched + silent; stop+depth 0
      is a flat MAGNITUDE in six bands (the LR4 sum is an allpass — a
      waveform correlation was the first, wrong, claim) with L == R;
      spread 0 mono while spinning; out bit-exact (L+R)/2; horn AM rate
      6.7 / 0.7 Hz via 10 ms peak envelopes, drum 0.85×, depth 0 flat,
      trough/crest 0.2 at depth 1, L/R envelopes anticorrelated at
      spread 1; Doppler swing ±2.3% by 8-cycle zero-crossing periods
      with the AM switched off (class attr), none at depth 0, flat peak
      envelope under pure Doppler (a 3 s Hilbert envelope showed ±40%
      — its own edge transients; measured wrong once, recorded); horn
      quick / drum lagging / ramp scales / stop coasts to rest / gate
      overrides the combo at 40% vs 60% duty; balance isolates a band
      >40 dB; crossover moves; mix 0 == input shifted by the integer
      centre delay, bit-exact; block-size independent (switch on a
      shared boundary); voice input sums; the example plays and goes
      fast AND slow. Suite **3221**. Cost 2.3%.

## The function generator (opened 2026-08-23)

Matthew's pick off the 2026-08-04 keep-list: "the highest patch-value-per-
line item on this list". Spec promoted from the one-liner and built the
same session.

- [x] **`function_generator`** — SHIPPED 2026-08-23. Modulation.
      `trig` + `rate_cv` in; `out` (cv) + **`eor`/`eoc`** (gate) out.
      Three modes off one shape: `trigger` (fire-and-forget, gate length
      ignored), `gate` (rise, hold at 1.0 while held, fall on release —
      an AR with a proper hold), `loop` (free-running; `trig` becomes a
      click-free sync). One bipolar `curve` knob bends both slopes via a
      power law `level = pos**k`, `k = 1+3c` / `1/(1-3c)`, so +c and −c
      are exact mirrors and the fall is the rise played backwards.
      `rate_cv` is 1 V/oct on the RATE, block-mean like the LFO's,
      ±5 octaves.
      **Stage progress is an integer sample counter**, not an
      accumulated float step — the organ's lone-8′ lesson, and it
      mattered: the float draft measured a loop period of 962 samples
      where 960 was asked for, compounding every cycle. Counted, the
      period is exactly `rise+fall` forever, and the test asserts the
      set of measured periods is `{960}` rather than a mean.
      Click-free by **solving the curve backwards** on every stage entry
      (`cnt = len·level^(1/k)`), so retriggers and mid-rise releases pick
      up from the current level.
      Perf: one scalar kernel shared by both shapes (the slew lesson —
      the vectorized-across-voices draft cost **117%** of a block's
      budget at 16 voices; per-voice scalars cost **34.6%**, 8.9% at a
      realistic 4 notes, 0.19% all-idle thanks to a skip for parked
      slots). Voice rows are bit-identical to mono *by construction*
      because it is literally the same function.
      40 tests; suite **2930**. Example `krell_machine.json`.
      **EARS PASSED 2026-08-29** — Matthew: "works fine".
- [x] **Sanction gate-rate feedback (`eoc → trig`) — THE FEEDBACK DOOR,
      GENERALIZED — SHIPPED 2026-09-16** (Matthew: "as recommended
      please Claude, execute the plan"). Built to the plan below,
      all four decisions as recommended. `_compute_late_edges` keeps
      pass 1 byte-for-byte (cables into a matrix, forward order) and
      adds pass 2: every remaining cable, in REVERSE `patch.cables`
      order, that can still reach its own source through the non-late
      graph goes late; a buffered sink's `fill` cable is skipped (it is
      already a delayed edge in its own right — the one fix pass 2
      needed, caught by the governor reference). Non-finite values in a
      late source are scrubbed to zero at the stash and counted in
      `backend._late_nonfinite`. **Eight reference renders + late sets
      + topo orders (the three matrix examples, the governor, and four
      loop-free examples), captured before editing: all identical
      after.** (`krell_machine.json` dropped from the set — it renders
      differently run to run on its own, unseeded noise/random LFO;
      not the door.) 21 tests in `tests/test_feedback_door.py`: matrix
      examples keep their single into-the-matrix late edge; the
      governor's fill is delayed but never late; last-drawn cable goes
      late and the forward chain sorts (and drawing the return first
      moves it — documented); a self-loop marks itself; two loops + a
      figure-eight fully sort; no cycle → no late edges; late set
      survives save/reload; the mixer-door staircase (a DC driver
      through `constant → cv_to_audio`, one generation per block at
      EVERY sample — the matrix test's twin); a fresh loop's first block
      reads silence; **`eoc → trig` fires forever with period exactly
      `cycle + block − 1` at 512 AND 64** (one block of loop latency
      less a sample of edge accounting); the starter alone is not a
      krell; the self-wah loop changes the render and stays ≤ 1.0, and
      the shipped example sorts the follower AFTER the filter with its
      cable late; a gain-2 loop stays finite and trips the counter, a
      tame one never does; closing/opening a loop while running
      recompiles cleanly; `krell_feedback.json` self-plays through the
      real self-patch (>10 notes in 20 s, breathing). Plus a new
      examples-sweep tripwire `test_example_has_no_severed_cycle`
      (Kahn's leftover tail empty for all 123 examples). Suite
      **3563**. Docs: Cabling rules gained a *Feedback loops*
      paragraph; the fg entry's "legal but inert" note and docstring
      rewritten; matrix_mixer "sanctioned" → "guarded" door;
      audio_to_cv's self-wah documented as a loop. Two smoke-test
      lessons: a self-loop `eoc → trig` alone NEVER starts — `trig`
      takes one cable, so the starter and the return are OR'd through
      `logic`, and the starter pulse must be SHORTER than the cycle or
      the OR never falls and there is no rising edge (a 0.6 s starter
      killed the first smoke); and a square wave is no DC driver for a
      staircase pin (sample 0 flips sign block to block). **Follow-up (UI) — SHIPPED 2026-09-16, same day** (Matthew:
      "late-cable UI follow-up please"): backend observables
      `feedback_cables(patch)` (the late set a compile would produce —
      a pure function of the patch, so the editor colours a loop the
      moment it is drawn, before Start) and `feedback_scrubs()`;
      `App._refresh_late_links` binds an amber, thicker node-link
      theme (`mvNodeCol_Link` / Hovered / Selected +
      `mvNodeStyleVar_LinkThickness` 4, built once) to exactly the
      late links and theme 0 to the rest, on every cable/module change
      and on load/new — never per frame; a toolbar `loops` slot beside
      `api` reads `loops --` / `loops N` / `loops N !K` (K scrubbed
      blocks, WARN colour) with a tooltip listing `type#id.port ->
      type#id.port (reads one block late)`; drawing the cable that
      closes a loop writes "Loop closed: ... reads one block late
      (feedback)" to the status bar. 11 tests
      (`tests/test_late_cable_ui.py`, mocked dpg with distinct ids via
      a context-manager stand-in for `node_attribute`/`theme`):
      observables agree with the compile; a loaded loop paints exactly
      its late link amber; readout + tooltip (ASCII); the status line
      once, not for a plain cable; deleting the late link clears
      everything; theme built once; scrubs turn it amber while
      running; a backend without the observables leaves the editor
      alone; new patch resets. Suite **3574**. Real-window eyeball
      wanted: the amber actually paints (DPG per-link theme binding
      is documented but untested here). **Unblocked:** fg Follow-on 4
      (`eor → trig` rise-only retrigger). Original entry:
      building the example, and it is a **compiler** job, not a module
      one, so it is queued rather than done. On hardware, `eoc` patched
      back to `trig` is *the* krell patch. Here that cable is legal and
      inert: `_is_delayed_edge` only honours a `matrix_mixer` late-read
      or a buffered sink's `fill`, so every other cycle is severed by
      the topological sort instead of delayed one block. The first draft
      of `krell_machine.json` rendered beautifully while firing once per
      starter-clock pulse — a patch that merely *looked* like a krell,
      which is why the example now carries a tripwire that counts notes.
      The fix is to generalize the sanctioned door beyond the matrix:
      let any cable that would close a cycle become a late-read, seeded
      from the previous block, with the one-block latency documented.
      That is the same machinery `_compute_late_edges` already has,
      widened from `dst is a matrix_mixer` to `any cycle-closing cable`.
      Worth a session of its own: it changes how EVERY patch compiles,
      so it wants its own tests and its own ears. Until then `loop` mode
      is the supported route and both the module docstring and
      MODULES.md say so plainly.
      **PLAN (2026-09-16, Matthew: "plan the feedback door with me
      please, then implement").** Survey first: today `_is_delayed_edge`
      honours (a) a buffered sink's `fill` and (b) cables INTO a
      `matrix_mixer` that `_compute_late_edges` marked (forward
      `patch.cables` order, BFS "can the matrix reach this cable's
      source?"). Everything else that closes a cycle is SEVERED: Kahn
      never emits the cycle members, they fall into the leftover tail
      in id order, and the consumer renders before its source with the
      buffer simply absent. Two shipped examples are inert because of
      it: the krell's `eoc → trig` (known, routed around with `loop`
      mode) and **`envelope_follower_wah.json` — the May self-wah has
      never wahed**: filter → audio_to_cv → filter.cutoff_cv sorts
      [keyboard, filter, audio_to_cv, speaker], and an A/B with the
      cable removed is `array_equal`. The matrix examples
      (`matrix_feedback_echo`, `organ_feedback_drone`, `organ_shimmer`)
      and `ring_governor_monitor` are the only live loops in the rack.
      *The change, in one sentence:* keep pass 1 exactly as it is (the
      matrix stays the guarded door, so every existing loop compiles to
      the SAME late set and renders bit-identically), then add pass 2
      — walk `patch.cables` in REVERSE order and mark late any cable
      whose destination can still reach its source through the
      non-late, non-fill graph. Reverse order means the cable that
      CLOSED the loop (drawn last, saved last) is the one that reads a
      block late, so the feed-forward path keeps zero latency (forward
      order would put the block on osc→delay→filter's forward leg
      instead of on filter→delay's return). One cable per cycle:
      marking it breaks the cycle for every other member. Self-loops
      (`eoc → trig`) are cycles of length one and mark themselves.
      Render side needs NOTHING new: the seed / stash in
      `render_block_multi` already keys on `_late_edges`. *Decisions
      for Matthew:* (1) reverse-order "last drawn" as the choice of
      late cable — vs first-drawn, vs preferring a particular jack
      kind; (2) safety — the general door has no soft ceiling (the
      matrix keeps its `soft_clip`; the speaker clips ±1; hardware has
      no guardrail either): scrub non-finite values at the stash and
      count them, and let the docs say "for audio loops, close through
      the matrix or put a limiter in the loop" — vs a global soft
      ceiling on every late edge (would colour audio loops); (3)
      examples — add `krell_feedback.json` (the true self-patch)
      ALONGSIDE `krell_machine.json` (ears passed on it) rather than
      converting it, and leave the self-wah example as is now that it
      works; (4) UI visibility as its own small follow-up: draw
      late-read cables differently (DPG per-link theme, feasibility
      first) + a status line on compile ("feedback: 2 cables read one
      block late") so the block of latency is never a mystery.
      *Build order:* (i) capture reference renders of the four live-loop
      examples + late sets BEFORE editing; (ii) pass 2 in
      `_compute_late_edges` + non-finite scrub at the stash; (iii)
      tests — exact late set for every existing loop example
      (unchanged); a non-matrix cycle marks exactly one cable, the
      last-drawn, with the forward chain still ordered; a `mixer`
      self-loop repeats the matrix's geometric-staircase latency pin
      through the new door; two independent loops and a figure-eight
      both fully sort; `fg.eoc → fg.trig` fires forever with period =
      cycle + one block (pinned at 512 AND 64 — block-size dependence
      is the door's documented property, as it already is for the
      matrix); the self-wah loop changes the render and stays bounded;
      a gain-2 loop stays finite (scrubbed) and the counter counts; a
      new examples-sweep tripwire "no severed cycle: Kahn's leftover
      tail is empty for every example"; (iv) examples
      `krell_feedback.json` + the self-wah gets a does-something
      tripwire; (v) docs — Cabling rules gain a "Feedback" paragraph,
      matrix_mixer "sanctioned door" → "guarded door", the fg docstring
      + entry drop the "legal but inert" caveat, audio_to_cv's
      self-wah, WORKLOG + this entry. Full suite watched: this changes
      compile for every patch.
- [x] **Follow-on 1: per-slope `curve_rise` / `curve_fall`** — SHIPPED
      2026-09-12 (Matthew's pick, the ears having liked it 08-29).
      Offsets ADDED to the shared `curve` for their slope, summed and
      clamped to ±1 — `curve` stays the "both" knob, the pair skews it,
      and both default 0 so the shipped render is untouched (verified
      array_equal against renders captured from the pre-change code:
      trigger/gate/loop, four curve settings). The kernel carries a
      `(rise, fall)` exponent pair and every backward solve on a stage
      entry uses the exponent of the stage being ENTERED. Pinned: rise
      midpoint = 0.5^k(curve+curve_rise) with the fall sample-for-sample
      unchanged (and vice versa); clamping matches the shared knob's;
      pluck-then-linger is not its own mirror; loop period unchanged;
      voice ≡ mono. "Click-free with different exponents" is measured
      honestly: the worst step in an interrupted render is no worse than
      the same curves' worst step UNINTERRUPTED — a k = 1/4 logarithmic
      slope has a genuine cliff at its end (0 → 0.12 in one sample at
      4800 samples), and that is the shape, not a discontinuity; the
      gentle case pins the step at the retrigger sample itself. 12
      tests; suite **3102**. No example needed.
- [x] **Follow-ons 2..3: `rise_cv` / `fall_cv` and `out_inv`** — SHIPPED
      2026-09-14 (Matthew's pick). The per-slope CVs are the same 1 V/oct
      law as `rate_cv` on one slope each; they SUM in octaves with
      `rate_cv` (Maths' per-channel jacks next to its "both") and the
      per-slope total is what clamps to ±5. An unpatched jack contributes
      exactly 0.0, so a rate_cv-only patch is bit-identical (432 reference
      arrays captured pre-edit: 3 modes × 4 curve settings × 4 rate_cv
      states × mono/voice × all jacks, all array_equal after). `out_inv`
      is `1 − out` on the finished block, outside the kernel — bit-exact,
      1.0 at rest and on a parked voice, so it is a ducker into a `vca`
      keyed off `trig`. Pinned: each CV moves only its own stage length
      (the other slope sample-for-sample unchanged); the octave sum;
      the ±5 clamp on the total; a loop period of rise/2 + fall·2
      driftless; a (V, F) CV collapses to one rate; voice ≡ mono. 17
      tests; suite **3119**. Perf unchanged (35% at 16 voices, 2.3%
      mono). No example needed. Docs carry the honest caveat: the CVs
      are collapsed to mono by SUM, so `velocity_cv → fall_cv` is a
      one-note-at-a-time trick until per-voice rates exist.
- [x] **Follow-on 4 — `eor`: what it is for — SHIPPED 2026-09-17**
      (Matthew: "fg follow-on 4 (eor → trig rise-only retrigger — an
      example and a paragraph now)"). **Deviation, measured first:**
      the literal self-patch `eor → trig` is a LATCH, not a cycle. The
      pulse arrives a block after the top, a few ms into the fall; the
      retrigger restarts a rise from ~0.99 that finishes in ~2 ms and
      fires eor again — at 512 the output pins at 0.98–1.0 with a
      613-sample ripple; at 64 the 2 ms pulse straddles two blocks, the
      OR never re-edges and the loop DIES (out = 0). Same in loop mode;
      gate mode dies at both. There is no musical reading of it, so
      the paragraph says so plainly (docstring + MODULES.md fg entry,
      "What `eor` is for") and a test pins it: self-patched eor→trig
      completes ≤ 1 cycle at 512 AND 64. What `eor` IS for is firing
      things at the PEAK, and the example is that:
      `fg_eor_swell_strike.json` — a slow fg (1.2 s / 1.8 s, curve
      −0.3, cycling through the krell loop with a random LFO on
      `rate_cv`) swells a C2 saw through `filter.cutoff_cv` + a VCA;
      `eor → pluck.trigger` strikes at the top of every swell and
      `eor → sample_hold.trig` grabs the strike's pentatonic pitch at
      that same instant. Test: ≥ 5 swells in 25 s, `out` == 1.0 at
      every eor, the pluck's peak after each eor > 0.3 and > 1.5×
      whatever was ringing before (a fast swell re-strikes a 2 s
      decay mid-ring — "quiet before" was the wrong claim, "louder
      after" is right; the example is unseeded, so the test was run
      three times). Suite **3584**. 124 examples. The fg's follow-on
      list is closed.
- [x] **Per-voice rise/fall rates** — SHIPPED 2026-09-14, same day it
      was surfaced (Matthew: "execute"). A `(V, F)` `rise_cv` / `fall_cv`
      against a `(V, F)` trigger with the same V gives each slot its own
      `(rise_len, fall_len, pulse_len)` from its own row's block-mean;
      `rate_cv` stays mono ("both" is one knob). The kernel is
      untouched — the stage lengths were already arguments — and the
      rate arithmetic moved into one `_fg_lengths` helper that both the
      mono law and the per-slot law call, so a voice row is bit-identical
      to the mono path fed that row's constant (pinned at three octave
      values, all four jacks). Any other shape (mono CV, voice CV into a
      mono trigger, mismatched V) is the summed mono law — pinned
      explicitly — and the 432-array reference sweep is `array_equal`.
      A 0 row renders as no CV, so `velocity_cv`'s parked slots stay
      parked (state checked, not just output). Loop-mode periods per
      voice (720 / 1440) driftless over 60 blocks. 11 tests (one of
      yesterday's rewritten: it pinned the summed behaviour); suite
      **3130**. Perf +0.8 points at 16 voices with a per-voice CV.
- [ ] **Question, not a defect: loop-mode voice slots that were never
      triggered stay parked** (noticed 2026-09-14). The voice path's
      idle-skip runs before the kernel's "kick out of idle", so a poly
      keyboard into a `loop`-mode fgen only loops the slots that have
      been played once; the mono path free-runs unpatched. Defensible
      (sixteen free-running kernels cost the full ~35% whether or not
      anyone is playing). Decide whether that asymmetry should be
      documented as-is or changed; do not change it silently.

## Nine follow-ons in parallel (2026-09-24, the sixth batch)

Matthew: "I am just going to retest every example … feel free to
invalidate previous examples … we can just reinvent whatever fails" —
so this batch took the follow-ons that had been parked BECAUSE they move
shipped renders. Nine agents, pushed first, cherry-picked in landing
order with no textual conflicts; two semantic ones the coordinator
fixed (c5467df, 3c3c2c0 — autopan was written against the old
`_mod_clock_sync` signature and a raw `.mean(axis=0)`). Suite **5261 →
5417**, 165 examples, **101 modules**. A before/after render of every
example (8 s, block 512, determinism-checked) attributed every move to
its commit — see the listening checklist.

- [x] **`chorus.rate_cv` per sample — SHIPPED (5c65bb1).** The CV's
      share of the sweep integrated on an int64 2^48-per-cycle grid
      (integer sums are partition-independent; the mask wrap is exact);
      526914/528000 differing samples → 0 at 64/128/512/1000, within
      6e-8 of the ideal integral. Only `chorus_lush` moved (3.3e-4).
- [x] **`pluck` `carry` default ON (65b3a4f).** 14 of 18 pluck examples
      moved, only at re-plucks. Patch files store every param, so a
      GUI-saved patch from 09-22..24 keeps `"carry": false`.
- [x] **Voice collapses in float64 (f36977e).** `_voice_sum` /
      `_voice_mean`, 24 sites + a tripwire; the win is voice-ORDER
      independence (reversing 8 voices moved 62% of samples by an ulp).
      `freeze.pitch_cv` through `_finite_mean`. The CV meter paints
      `nan`/`inf` and holds the bar; FIXED on the way: one NaN poisoned
      `_auto_range_fill`'s bounds for good.
- [x] **`autopan` — module #101 (73047ce spec, dd0c161 build).** Mono
      (placed by `law`) or stereo (balanced), sine/triangle/glided
      square LFO, `tremolo` as the L/R phase, clock sync, bit-exact at
      every block size. 49 tests.
- [x] **`rotary` + organ scanner (96bbd15).** Rotary was THREE bugs —
      the ring read, a per-block re-wrapped rotor angle, and a ring that
      CLEARED itself (rotors too) whenever the block length changed. The
      organ scanner was already exact.
- [x] **`clock_divider` gates never merge (83ae23c).** Prospective cap
      from the predicted next rise + a 1-sample fallback; merges 2519
      `divn` + 2256 `mult` → 0 across the sweep. No example audio moved.
- [x] **`freeze` — relative floor + 65536 (e5dd7c2).** −120 dB relative
      floor (a triad: 3 regions, was 4518) — which also fixed a
      knife-edge: one ulp of input swapped half the partials between L
      and R (peak-order sign alternation). Frames ~1/4 the cost; 65536
      back on the knob with a STAGED birth (hold starts 93 ms after the
      edge), worst block 31%.
- [x] **`delay` / `flanger` / `phaser` (f345a5e, fe3f58c).** Five
      mechanisms incl. two paths damping with different arithmetic and
      a flanger ring of `12 ms + frames` that CUT the deepest sweep at
      block 64 (up to 1.05). Pins now compare float64 internal state —
      the delay's old drift touched 1 output sample but 106817 of its
      line.
- [x] **`_render_oscillator` (02f3916).** Integer-count phase (the
      organ's numbers — the lone-8' pin is now equality), a carried
      unwrapped CV phase, `_wt` band per sample, seamless engine
      switching. 168/204 → 0/204. Moves ≤ 1.8e-7 except naive saw/square
      at integer-Hz pitches, whose wrap sample the old accumulator put
      one ulp on the wrong side every time — up to 1.6 on sparse edge
      samples, spread by any filter downstream (the biggest movers in
      the audit; the new edge is the correct one).

Follow-ons surfaced:
- [x] **The speaker output clips but doesn't scrub — a NaN reaches the
      audio device.** FIXED 2026-09-24 (next session). Scrub-then-clip on
      the master bus AND every routed device bus: only the non-finite
      samples go to 0 (inf too — clip would have made it full scale), a
      finite block is bit-identical, each scrubbed block counts in
      `sink_scrubs()`, and the status bar posts a line when the count
      rises while running. 15 tests (`tests/test_sink_scrub.py` + a class
      in `test_late_cable_ui.py`), including the real shape — a runaway
      bare loop whose blow-up block flows forward past the door's scrub.
      Self-tested: with the `nan_to_num` removed, 5 of the 9 backend
      tests fail. Suite **5436**.
- [ ] **EARS DECISION: `_wt` band follows the HIGHEST voice**, so a low
      voice in a chord loses harmonics. Per-voice bands are more correct
      but change how steady poly `_wt` patches sound.
- [ ] **`cv_to_frequency` / `keyboard` / `midi_input` still carry the
      per-block phase accumulator** (they share `_waveshape_wt`, so they
      got the per-sample band). `_osc_carried_phase` is reusable.
- [ ] **`flanger` / `phaser` `rate_cv` per sample** — the chorus's int64
      integrator is the pattern.
- [ ] **`chorus_lush`'s saw pad differs by up to 1.0 between block
      sizes** — upstream of the chorus (clock/sequencer edge timing?).
      Probe it.
- [ ] **Rings that reset on a block-size change:** the chorus's (length
      depends on `frames`) and the organ scanner's (only for a block
      larger than any before, > 4096). `_rotary_grow` shows how to keep
      the history.
- [ ] **`rotary` `fast` switch lands on a block boundary** (block-majority
      read). A vote over fixed 64-sample windows would make it exact at
      ~1.3 ms latency. A call, not a bug.
- [ ] **pluck's float32 time mean** at
      `set_coeffs(v, float(np.mean(p_row)))`; freeze's `width_cv` does
      its own float64 mean instead of `_finite_mean`. One-liners.
- [ ] **No epoch wrap on the no-CV oscillator/organ phase** — precision
      erodes to ~−116 dB after 24 h of running. Low priority.
- [ ] **`freeze`: stage 32768 too** (a 2048-sample delay, worst block
      ~25% from 46%) — shifts `freeze_drone_breathe`'s timing. Offered.
- [ ] **`clock_divider`:** clock swing 0.5 + divider swing 0.5 at n 1
      drops 47 of 96 gates (a late gate's offset lands past the next
      short edge and the on-time gate replaces it).
- [ ] **`autopan` follow-ons:** smooth-random shape, a clocked phase
      offset, a separate tremolo rate, `width` for a stereo pair, a
      `pan_cv` depth knob.
- [ ] **Example health (pre-existing, from the audit):** `organ_leslie`,
      `noise_hat` and `clock_divider_swing` hit full scale in their
      first 8 s; `resampler_tape_stop` carries a 0.083 DC offset.

## Eleven follow-ons in parallel (2026-09-22, the fifth batch)

Matthew: "Pick another 10 or items from the todo for existing modules
and lets run a few together ok please Claude?" — eleven agents (the
phaser/flanger pair travels together), every item taken from the lists
below. Pushed first, so the worktrees branched from HEAD. Four
conflicts, all trivial except one real one (two agents rewrote the same
`rate_cv` block; the move was taken and the `_finite_mean` fix
re-applied). Suite **4536 → 5265**, 164 examples. Ears/eyes are in the
listening checklist above.

- [x] **`vowel` — follow-on list CLOSED — SHIPPED 2026-09-22
      (845ea1c).** float64 block mean (the known finding: a constant CV
      read 0.29999998 at 64 and 0.30000001 at 512 — 3 of 4 constant CVs
      rendered differently before, none after; the four LFO-patched
      references moved ≤ 2.6e-7, −135 dB RMS, and the seven un-modulated
      ones stayed bit-exact); `cv_rate` `block|sample` (the per-sample
      mouth, CV quantised to 0.02 vowels AROUND THE KNOB so an idle jack
      is bit-identical to block mode — the step measured against a true
      per-sample oracle at −62 / −55 / −43 dB for 0.02 / 0.05 / 0.1;
      0.59x realtime against 0.010x); `voice` `custom` + `f1..f5` (your
      frequencies, the tenor table's bandwidths in Hz — so Q runs 7.4 →
      63 from 300 Hz to 2500 Hz and `resonance` is the knob that fixes a
      custom voice). 34 → 65 tests. `vowel_robot_talk.json` banked (25
      Hz, not 30–60: 45 Hz put the patch at 0.98x realtime).
- [x] **`noise` — `corner` + the stereo pair — SHIPPED 2026-09-22
      (53c9728).** Brown's leak 2..40 Hz; slope −6.00 dB/oct at every
      corner, 2–20 Hz energy +39.0 / +33.0 / +23.5 dB over 200–2000 Hz
      at 2 / 10 / 40; level flat (0.14 dB spread) because the RMS match
      is derived from the same pole. **Found: a live corner change bangs
      (step 2.2, ~4x full scale) unless the carried integrator state is
      renormalised** — done, and it cannot fire while the knob is still.
      21 reference renders bit-exact. 73 → 100 tests.
      `noise_stereo_pair.json` banked (corr 0.087 as shipped, 1.000 with
      equal seeds — pinned both ways).
- [x] **`clock` — `swing_cv` — SHIPPED 2026-09-22 (d89dadc).**
      `clamp(swing + depth·mean cv, 0, 0.75)`, float64 mean, **latched
      at each even period start** and held through the odd period after
      it so a moving CV never drags a pulse in flight. A 0.5 Hz LFO over
      10 s emits exactly the straight clock's 80 edges at every depth;
      even edges land on the straight clock's samples at 64 and 512, odd
      ones up to 44 samples (1 ms) apart with an identical count — the
      honest story for a block-rate CV. 33 clock rows bit-exact at
      default. `clock_swing_breathe.json` banked.
- [x] **`clock_divider` — steady gate lengths on a swung clock — SHIPPED
      2026-09-22 (d54912a).** Lengths came off the LAST interval:
      `divn` flapped 10750/5787 at 8 Hz / swing 0.3, and `div2/4/8`
      (always on even pulses, always measuring the short side) sat ~30%
      under their period (3858 where 5512 was right). Now the mean of
      the last two intervals — exact for a swung clock, exact for a
      steady one, and it flattens a straight clock's 2-sample rounding
      wobble too. Only falling edges move; every rising edge is
      bit-exact across 22 references. One shipped render moves:
      `cv_recorder_backwards` reads two divider gates as LEVELS
      (1.5e-4 peak over 120772 samples).
- [x] **FIXED 2026-09-24 (83ae23c) — `clock_divider`: a `divn` gate longer than the swing's short
      side still merges into the next one** (17 of 32 gates at `pw` 0.9,
      swing 0.3 — measured before and after the 09-22 pass). The
      scheduler cannot un-write an emitted sample, so the cure is a
      prospective length cap that knows the short side; a blunt
      `min(interval, interval_prev)` would wrongly shorten `div2/4/8`.
      Low priority — keep `pw` under the short side, or use `divn` for
      triggers.
- [x] **`freeze` — `width_cv`, `latch`, 32768 — SHIPPED 2026-09-22
      (c7ff869).** `width_cv` rotates the stored per-region scatter by
      the current width (free), corr(L,R) tracking `cos(w·pi/2)` at
      0.951 / 0.707 / 0.000; `latch` makes every rising edge TOGGLE the
      hold (cumsum parity XORed with the carried state, 64 = 512 = 1000
      pinned); `FREEZE_SIZES` gains 32768 (743 ms — an arpeggio frozen
      on its last note comes back with all three notes inside 30 dB).
      **65536 measured and left OFF the knob: 11.2 ms of synthesis every
      371 ms against an 11.61 ms block — a permanent near-overrun.**
      `_freeze_lock_index` vectorised on the way (integer-identical,
      fuzzed over 4025 spectra, 6 ms → 0.33 ms at 32768). 46 reference
      renders bit-exact. 49 → 67 tests. `freeze_drone_breathe.json`
      banked. **Lesson: the capture window is a Hann, so the instant at
      the gate edge is the QUIETEST part of what you hold.**
- [x] **DONE 2026-09-24 (e5dd7c2) — `freeze` follow-ons:** the capture spike is the layer's BIRTH
      block (frames 0..4 must all exist at once — 10.1 ms mono / 20.0 ms
      with width at 32768), so spreading the prefill or starting the
      read a hop later might put 65536 back on the knob; and
      `_freeze_lock_index` only rejects bins under an absolute 1e-12, so
      a pure triad still yields 3079 "peaks" at 32768 from its own
      numerical floor — a relative floor (−120 dB) would make the
      capture nearly free but changes the region map, so it needs its
      own reference-render pass.
- [x] **FIXED 2026-09-24 (f36977e) — `freeze.pitch_cv` has the same float32 block-mean bug** (found
      by the vowel agent while self-testing; the freeze renderer was
      excluded from the CV sweep's remit). One line, plus a
      reference-render check.
- [x] **`cv_recorder` — `speed_cv` + the one-shot — SHIPPED 2026-09-22
      (9a8e5cf).** The rate jack is QUANTISED onto the half-sample grid
      (`floor(base·2^(d·mean cv) + 0.5)` half-steps, clamped 1..8 =
      0.5x..4x in 0.25x steps, 0 unreachable) because an integer
      half-step is what makes every rate identical at block 50 and 250 —
      shipped and documented as a stepped control, a tape machine's
      speed selector with more notches. `play_mode` `gate|one_shot`: a
      rising edge runs exactly one lap of TRAVEL and holds the slot it
      just played. Added rule the spec missed: at `one_shot`, `rec` runs
      the head (a stopped head writes nothing, so recording would have
      been impossible). 32 reference arrays bit-exact. 44 → 185 tests.
      `cv_recorder_oneshot.json` banked. Note: a lap longer than the gap
      between triggers degrades into a retriggered sweep.
- [x] **`pluck` — `vel_position` — SHIPPED 2026-09-22 (d35bd0c).**
      A soft hit's pick slides towards the middle:
      `clamp(position + vel_position·(1 − vel)·(0.5 − position), 0, 1)`,
      latched at the edge, anchored at vel 1.0 so the knob is inert
      without a velocity source. `position` 0 is the comb's documented
      OFF and stays off (the spec's formula would have switched it on).
      **The centroid claim does not hold — a comb notches, it does not
      tilt:** the first null falls 2597 → 686 Hz at vel 0.3, and the
      2+3-over-1 balance 5.76 → 1.39. 88 reference renders bit-exact.
      45 → 64 tests. `pluck_touch.json` banked.
- [x] **`pluck` — the allpass-clear QUESTION, answered — `carry`
      SHIPPED default-OFF 2026-09-22 (4675958).** Measured with a zero
      burst: the clear steps the output by **51% of the ring** (51.4%
      into G4, 51.5% into C5; 28% elsewhere — it discards one sample of
      loop state), while carrying it gives a step of exactly 0 and
      **exact superposition** (two hits minus one = the second alone to
      one float32 ulp — the property the module always claimed).
      Tuning, peak, stability identical either way.
- [x] **FLIPPED 2026-09-24 (65b3a4f) — `pluck`: listen, then consider flipping `carry` to default ON.**
      Strictly better on every measured observable; shipped OFF only
      because ON changes the sound of every re-pluck. `pluck_touch.json`
      has it on — untick to A/B. If the ears agree: flip the default,
      recapture the pluck reference renders, document loudly.
- [x] **organ — the block-size FINDING CLOSED — SHIPPED 2026-09-22
      (4517234, 5b54a64, a3894c4).** Three accumulators, not one: the
      partial phase (parting at 0.221 s block-1000 vs 512, 5.96e-8 by
      30 s), **the percussion — the largest (9.69e-7), because it tested
      its 1e-6 cut-off once per pour so the tail died on a different
      sample per block size** — and a float32 block-mean that made a
      held pitch itself block-dependent. All three integer-counted or
      float64; 64/128/512/1000 bit-identical over 10 s, checked to 60 s.
      What moved: ≤ 1.013e-6 (8.5 ulps, −153 dBFS) from the phase and
      percussion work; up to 4.01e-4 (−86 dBFS) from the float64 mean —
      a 7e-5-cent pitch correction. Plus the scanner's rounded-triangle
      sweep (crest 1.12 / 3rd harmonic 0.287 vs the sine's 1.44 / 0.000;
      the swing table widened 1.4x to hold V3 at 41.8 cents) and a
      scanner phase that survives off → on. 33 → 37 tests.
- [x] **FIXED 2026-09-24 (02f3916) — `_render_oscillator` still carries the per-block
      `phase += frames·inc` accumulator the organ just dropped** — same
      drift, much wider blast radius (its mono fast path is the
      reference for the organ's lone-8' pin, which now measures the gap
      instead of asserting exactness over many blocks). Worth a scoped
      session.
- [x] **tape + chorus — the ulp FINDING CLOSED — SHIPPED 2026-09-22
      (063e7d5, 3f161fa).** THREE mechanisms: the ring read
      (`absidx - delay` rounds at the index's magnitude and the ring
      length depends on the block size — the whole of the drift path's
      error, 95/90/78 differing samples at 64/128/1000), the wow/flutter
      phase accumulators, and **scipy** (`lfilter` short-circuits
      `len(a) == 1` to `np.convolve` + a separate `+= zi`, splitting each
      65-tap sum at the block boundary — the shared 4x oversampler was
      never block-exact; fixed by carrying raw tail samples at 1.00x the
      cost, where forcing scipy's recursive path was 2.7x slower). The
      chorus shared both of the first two, worse (487 differing samples)
      while its docstring claimed exactness. Both now bit-exact at
      64/128/512/1000 over 4 s with everything on; shipped renders moved
      ≤ half a float32 ulp on 0.006% of samples. **The shared
      oversampler moved — `distortion` and `waveshaper` ride it; both
      suites and both reference renders are clean.** 72 → 83 tests.
      **Lesson: exposure is part of the assertion** — the old 20000-
      sample pin passed throughout because the bug needs ~0.5 s to show.
- [x] **FIXED 2026-09-24 (96bbd15 rotary/organ, f345a5e delay, fe3f58c flanger/phaser) — Four more renderers read a ring index the tape's old way** —
      `_render_rotary`, `_render_flanger`, `_render_delay_core` and the
      organ's scanner all form `absidx - delay`; `flanger`/`phaser` also
      carry the float LFO phase (the 09-22 phaser pass worked AROUND it
      for the clocked path only, and pinned the free-running drift at
      < 1e-6 / < 1e-5 so this fix will not break those tests). Rotary
      and organ both CLAIM block-size independence in their docstrings.
      One batched pass, same probe: 4 s at 64/128/512/1000.
- [x] **FIXED 2026-09-24 (5c65bb1) — `chorus.rate_cv` is the chorus's one remaining block-size
      dependence**, by design (a block-mean rate re-anchors the sweep
      every block). Closing it means a per-sample rate — a behaviour
      change, not a bug fix. Decide before doing.
- [x] **The linear-CV sweep — SHIPPED 2026-09-22 (6307908).** One
      `_finite_mean(cv, default=0.0)` beside `_pow2_clipped`, **39 call
      sites**. float64: **fifteen modules rendered differently at 64
      than at 512 with only a steady CV on a jack** (filter mono +
      per-voice, crossover, sweep_eq, motion_eq, tilt_eq, loudness,
      compressor, reverb, flanger, phaser, lfo, slew mono + voice,
      wavetable_morph, bowed, wind) — all exact now. The scrub: Python's
      `min`/`max` do not propagate NaN, so a clamp read a NaN as a rail
      (`loudness` rendered non-finite; `bitcrusher`'s `int(round(nan))`
      raised out of the audio thread). 120 of 156 examples bit-exact; of
      the 36 that moved, 34 are ulp-level. `tests/test_cv_scrub.py`, 187
      tests. **Lesson: monkeypatching the reduction back to float32
      restored all 36 moved renders bit-exactly — the cheapest possible
      proof that nothing but the dtype moved.**
- [ ] **EARS DECISION: `bowed_cello` (5.2e-2) and `wind_duet` (1.0e-4)
      moved above ulp level** — chaotic physical models amplifying a
      one-ulp input change over seconds. Both were NEVER block-size
      exact before (1.1e-5 / 6.0e-6 between 64 and 512) and both are now,
      so the new render is the exact one and the change is like a
      different bow-noise seed. Keep, or revert those two sites?
- [x] **FIXED 2026-09-24 (f36977e) — The voice-collapse means are still float32** (`_input_buffer`'s
      `sum(axis=0)` and the per-module `mean(axis=0)` sidechain/macro
      collapses) — a different reduction (across voices, per sample) that
      changes buffer dtypes, so it wants its own reference-render pass.
      Also `render_block_multi`'s CV meter readout (a UI number, not a
      clamp) — decide whether a NaN cable should paint 0 rather than nan.
- [x] **The patch loader fails soft AND SAYS SO — SHIPPED 2026-09-22
      (357d99b).** The 2026-09-16 defect closed: `Patch.from_dict` now
      runs every cable past `Patch.cable_problem` (what `connect`
      checks), drops the failures, and reports them on
      `patch.load_warnings` → GUI status line + console, CLI stderr. It
      never raises over a cable. All 156 examples load clean and lose no
      cable; 24 re-render bit-exact. Duplicate destinations deliberately
      NOT policed (choosing a winner would change how a patch plays).
      `tests/test_loader_warnings.py`, 180 tests; reverting the
      validation fails 13 of them. **Consequence: open a drifted patch,
      save, and the dead cable is gone from the file for good.**
- [ ] **Loader follow-on:** the editor could OFFER TO REPAIR a dropped
      cable (nearest-name port match) rather than only naming it.
- [x] **`phaser` + `flanger` — their FIRST love pass — SHIPPED
      2026-09-22 (af3ce58, 19b1724).** Both gained a `clock` jack +
      `division` (the sweep becomes a length in beats: 1.0007 / 0.5003 /
      0.2502 Hz measured against 1 / 0.5 / 0.25), `spread` (the L/R LFO
      offset the quarter-cycle quadrature hard-coded; default 0.5 IS
      that, and 0.5×0.5 is exact in binary so it is bit-exactly today;
      corr 1.000 / 0.698 / 0.675 phaser, 1.000 / 0.579 / 0.497 flanger)
      and `manual_cv` + `manual_depth` (phaser notch 1489 / 2943 / 5676
      Hz at cv −1 / 0 / +1; flanger tap 33 / 66 / 132 samples, and in
      through-zero it moves the REFERENCE tap too, so the crossing
      travels). **Found: a rate re-read once a block put 5.6e-2 between
      the 64- and 512-sample renders — audible** — so the locked phase
      is keyed to the absolute sample index and a clocked sweep is
      exactly bit-identical across block sizes, better than the
      free-running one. `rate`/`rate_cv` stand aside while the lock
      holds (a block-mean rate would re-anchor every block and put the
      block-dependence straight back). Two briefed features already
      existed — the flanger's through-zero and the phaser's `center` as
      its manual knob — reported rather than rebuilt. 22 reference
      renders bit-exact. 64 → 116 tests. `phaser_envelope_sweep.json` +
      `flanger_jet.json` banked. **Lesson: measure the right observable
      — a phaser's rate reads off a tone's AM, but a comb has a notch
      every 1/delay Hz, so the flanger's rate must be measured as a
      DELAY by cross-correlation lag.**

## Ten follow-ons in parallel (2026-09-20, the fourth batch)

Matthew: "Pick another 10 or items from the todo for existing modules
and lets run a few together" — pushed first, so the worktrees branched
from HEAD. Ten agents at once, cherry-picked in landing order (three
conflicts: two helpers at one anchor, adjacent index rows, appendix
bullets — both kept). Suite **4245 → 4536**, 156 examples. Ears/eyes
items are in the listening checklist below.

- [x] **`vowel` — `formant` + `formant_cv` — SHIPPED 2026-09-20
      (8f3bdc5).** Constant-Q shift (bw scales with F). A's peak 342 /
      661 / 1338 Hz at −12 / 0 / +12; Q 8.06 → 8.18 on the impulse
      response while the −3 dB bandwidth doubles 31 → 61 Hz; cv +1 ==
      knob +12 bit-exact; exponent clipped ±4; eight reference renders
      bit-exact. 22 → 34 tests. Example `vowel_giant_child.json`.
- [x] **CLOSED 2026-09-22 (845ea1c, box ticked 09-24) — `vowel` — FINDING: `vowel_cv`'s block mean is float32-
      accumulated**, so a constant non-dyadic CV differs by an ulp
      between block sizes (0.29999998 over 64, 0.30000001 over 512).
      `formant_cv` averages in float64; fixing `vowel_cv` the same way
      moves the LFO-patched reference renders — its own recipe pass.
      Probably true of every `float(np.mean(cv))` on a float32 buffer
      in the rack: a sweep candidate.
- [x] **`pluck` — `vel_color` — SHIPPED 2026-09-20 (e7eb4b5).**
      `clamp(color + vel_color·(vel − 1), 0, 1)` at the edge, per voice;
      anchored at vel 1.0 so the knob is inert without a velocity
      source (bit-exact pin). Centroid 3540 vs 5782 Hz (vel 0.3 vs 1.0,
      `vel_color` 1); the loop untouched (t60 0.5108 s both, damping
      0). 25 reference renders bit-exact. 34 → 45 tests. Example
      `pluck_velocity_color.json`. Follow-on idea: `vel_position`.
- [x] **`lfo` — `seed` — SHIPPED 2026-09-20 (9b440f1).** 0 = the global
      rng (today); N = a private generator consumed only at a wrap or a
      reset edge; **a reset re-seeds it** (the same random phrase every
      bar). Per-voice copies of one generator (a shared stream drawn
      voice-major is block-size dependent); `rate_cv` exponent clipped.
      19 reference renders bit-exact; an integer-period rate can put a
      wrap a sample apart between block sizes (the float phase, every
      waveform's trait — pinned ±1). 37 → 54 tests. Example
      `lfo_random_replay.json`. Note: a `phase` knob move counts as a
      reset and so re-seeds — decide whether that is wanted.
- [x] **`sample_hold` — `prob_cv` + `prob_cv_depth` — SHIPPED 2026-09-20
      (650f4e5).** The chance read at the edge's own sample; `prob` 0.5
      + cv 0.5 == `prob` 1.0 draw for draw; a `(V, F)` cv promotes the
      module to the voice path; 1e6 → 1, NaN → 0. Seven reference
      renders bit-exact. 62 → 89 tests. Example
      `sample_hold_prob_sweep.json` (a BIPOLAR LFO on prob_cv — a
      unipolar one never reaches "stuck"). Test lesson: a prefix match
      on a param name is not a match on the widget.
- [x] **`clock` — `swing` — SHIPPED 2026-09-20 (85c3eef).** The
      divider's convention (fraction of the period, the pulse keeps its
      width); parity carried in state, zeroed on every restart edge;
      odd pulses cut a sample before the next downbeat (the ceiling);
      backend clamp 0.75 = the divider's, slider 0..0.5. 8 Hz / 0.33:
      odd edges exactly 1819 late, even pulses bit-identical; 18
      reference renders bit-exact. 26 → 39 tests. Example
      `clock_swing.json` (the `div4` kick sits on even pulses only,
      pinned; the divider's gate LENGTHS wobble on a swung input —
      documented, triggers only). Follow-ons: `swing_cv`; the divider's
      gate-length wobble.
- [x] **CV-overflow audit — SHIPPED 2026-09-20 (a1b2530).**
      `_pow2_clipped` (±64, NaN → 0) beside `_gate_ramp_env`; 23 call
      sites routed (filter cutoff ×4, crossover, sweep_eq, motion_eq
      freq + Q, chorus/flanger/phaser/drift rate, bitcrusher rate,
      bowed/wind/organ/modal pitch, function_generator, slew,
      pitch_shifter) + the sampler's pure-Python `playback_rate`
      (`OCT_EXP_LIMIT`). Three "already clamped" paths read NaN as a
      rail (Python `min`/`max` don't propagate NaN). 46 reference
      renders bit-exact. `tests/test_cv_overflow.py`, 108 tests.
- [x] **CLOSED 2026-09-22 by the linear-CV sweep (6307908, box ticked 09-24) — NaN scrub for the LINEAR block-mean paths** — `bitcrusher.
      bits_cv` does `int(round(bits))` and raises ValueError on a NaN
      mean; motion_eq `gain_cv` and supersaw `detune_cv` clamps pass
      NaN through the same `min`/`max` wart; `_q_cv_ratio` clips but
      doesn't scrub. One `_finite_mean(cv)` helper and a sweep — the
      same shape as the power fix (and the float64-mean finding above
      could ride along).
- [x] **`sequencer` (+ `fader_seq`) — `reverse` gate — SHIPPED
      2026-09-20 (d1c13a6).** Read on the clock-edge sample, a per-edge
      flag into `next_step_index(..., reverse=)`; pendulum heading kept
      in the base frame; random untouched (same draw); reset unchanged,
      the next edge moves in the effective direction (a reset with the
      gate high lands on the reversed start — the mirror). 24 reference
      renders bit-exact; the step SEQUENCE pinned 64 vs 512. 41 → 64
      (+8 → 9 fader_seq) tests. Example `sequencer_reverse_bars.json`
      (a palindrome). Docs note: a reverse source whose edges land ON
      the clock's is a race between two float phases.
- [x] **`reverb` + `delay` — `freeze` tickbox — SHIPPED 2026-09-20
      (5a094f8, 30b8bb4).** `_freeze_gate_row`: cable OR tick, None when
      neither is live (the pre-freeze code by construction). Ticking at
      block k == a cable rising there, bit-exact, both delay paths. 33
      reference renders bit-exact. 75 → 82 tests. No example (a panel
      convenience).
- [x] **`freeze` — `width` + `decay` — SHIPPED 2026-09-20 (ce8ca65).**
      `out_l`/`out_r`; a deterministic QUADRATURE phase scatter per
      peak region (the spec's random per-bin scatter moved partials up
      to 16 dB — measured and rejected): corr(L, R) = cos(width·π/2),
      every partial at the mono's level, the fold −3.01 dB; `out` the
      untouched mono; `decay` = 10^(−3t/decay) from the integer count,
      rebased on a knob turn, dropped at −90 dB. Bit-exact at default;
      64 == 512 == 1000 with both live. 35 → 49 tests. Example
      `freeze_wide_wash.json` (`size` 16384). Follow-ons: `width_cv`; a
      seed-keyed which-partial-goes-left variant.
- [x] **`cv_recorder` — `play`, `reverse`, `speed` — SHIPPED
      2026-09-20 (69f3c4e).** The head is an integer in half-samples
      (0.5x / 1x / 2x = 1 / 2 / 4 steps; reverse = the sign) — bit-exact
      at every speed, 50 vs 250 with gate edges on odd samples; a
      stopped head writes nothing; a sync tick snaps a stopped head to
      0; record at 1x, play at any; a reversed take comes back
      time-reversed. A pure-Python old-engine reference carries the
      bit-exact pin. `speed` shares a name with the transient shaper's
      shared combo branch (the `mode` shadowing shape) — the recorder's
      combo sits in its TYPE block. 18 → 44 tests. Example
      `cv_recorder_backwards.json` (`replace`; the loop is created on
      the clock's SECOND tick, so the dividers are reset inside the
      take). Follow-ons: a `speed` gate/CV; `play` as a one-shot;
      save-in-patch (still the honest gap).

## The hundredth module (2026-09-20)

- [x] **`freeze` — SHIPPED 2026-09-20, module #100.** Effects. Matthew:
      "freeze please Claude - #100". Spec into MODULE_IDEAS first (§
      Character & space), three prototypes before the module. The
      spectral freeze: a phase-vocoder capture (two Hann frames a hop
      apart, true frequency per bin) **phase-locked to the spectral
      peaks** — the third prototype found an unlocked hold eats itself
      (a triad at 4096 lost two thirds of two partials by 10 s; locked
      it holds within 2% forever) — re-synthesised per hop, overlap-
      added, read from frozen time `size` so a stationary source's
      hold is its own continuation in phase. `size` 1024..16384 (4096;
      partials within ~3 bins merge and hold at their mean — a minor
      second at C3 needs 16384), `smear` (coherent → random-phase wash,
      seeded per frame), `pitch` + `pitch_cv` (a change of READ rate —
      exact, unity level; a resampled spectrum lost 4 dB), `level` +
      `dry` (not a mix — engaging never ducks the dry), `fade` (rise /
      fall / the crossfade between two holds — a re-freeze melts; four
      layers max), the `freeze` tickbox ORed with the gate. Bit-exact
      across block sizes with edges mid-stream; unfrozen returns the
      input buffer itself. 35 tests in `tests/test_freeze.py`. Example
      `freeze_chord_pad.json` (banked). Suite **4245**, 148 examples,
      **100 modules**. Follow-ons: stereo `width` (two smear seeds L/R);
      a `decay` (a hold that fades on its own); a `spread` /
      formant-preserving shift; `hold` as a latch/toggle; `size` above
      16384 for drones (cheap — the combo is the only limit).

## A third batch of five love passes in parallel (2026-09-19, night)

Matthew: "Ok choose another 5 modules that could use some love I should
have enough to cover it". Same recipe, five worktrees, cherry-picked in
landing order (one conflict, an adjacent import line, both kept). Suite
**4045 → 4206**, 147 examples. Ears/eyes items are in the listening
checklist below.

- [x] **`noise` — `brown` + `violet`, `seed`, `amp_cv` — SHIPPED
      2026-09-19 (e095a07).** Brown = a leaky integrator of the white
      (10 Hz corner) scaled by the exact `sqrt(1 - a^2)`; violet = the
      first difference. Measured slopes -5.85 / +6.15 dB/oct, RMS vs
      white +0.08 / +0.03 dB. `seed` 0 = the free-running global rng
      (today); N = a private `default_rng(N)`, the seed alone the key
      (two modules, one seed = one stream — correlated stereo on
      purpose). `amp_cv` knobless; a `(V, F)` cv broadcasts one stream
      with per-voice level. Ten examples bit-exact. 26 → 73 tests.
      Example `noise_brown_surf.json` (banked). Follow-ons: a `corner`
      knob (2–40 Hz) on brown for wind-vs-thunder if the 10 Hz leak
      reads wrong; a stereo-pair example (same seed L/R = mono,
      different = wide).
- [x] **`sample_hold` — `mode`, `prob` + `seed`, `glide` — SHIPPED
      2026-09-19 (9cd61a9).** Track-and-hold (follows while high,
      holds the sample before the fall); "sometimes" (one draw per edge
      per voice, time-major, consumed only when 0 < prob < 1 — prob 1
      and prob 0 never touch the rng); glide = the slew's one-pole
      premise, 0.99 at exactly `glide*sr` samples, 0 calls no filter.
      A live flip to `track` under a held gate follows in the next
      block. Seven reference renders bit-exact; 64 vs 512 exact in
      both paths. 24 → 62 tests. Example `sample_hold_sometimes.json`
      (banked; the glide sits BEFORE the quantizer, so each change is a
      50 ms zip through the scale — ears on whether it wants to be
      after). Follow-ons: `prob_cv` (needs a depth); a live `prob`
      change mid-window keeps the old verdict (documented, harmless).
- [x] **`clock` — `reset`, `run`, `bpm_cv` + `bpm_cv_depth` —
      SHIPPED 2026-09-19 (03c69a3).** A reset IS a fresh clock from
      that sample (edges 3000, 8512, 14024 = 3000 + the free-running
      clock's own); a reset inside a pulse extends it; `run` low holds
      with the phase untouched and a run rise is a reset (play on the
      downbeat); a reset while held zeros the phase and waits.
      `bpm_cv`: `bpm * 2^(depth * mean cv)`, exponent clipped to ±6
      BEFORE the power (1e6 pins at x64, finite). 14 reference renders
      bit-exact. Block size: bit-exact at 8 Hz; at an integer-period
      tempo one edge sits a sample apart (62052 vs 62053) — pinned ±1,
      documented. `tests/test_possibility_seq.py`'s `_render_clock` spy
      took the new 4-arg signature. 8 → 26 tests. Example
      `clock_transport.json` (banked): two bars on / one held, the
      bar-line reset audible as an A-B-C-A figure, a drift at 0.03
      dbl/unit breathing 117.0–118.9 BPM. Follow-ons: `swing` on the
      clock itself; `run` as a toggle (tap-on / tap-off).
- [x] **`organ` — `vibrato` (the scanner) — SHIPPED 2026-09-19
      (27f6a9a).** off | v1 v2 v3 | c1 c2 c3; one ring per voice, one
      integer-counted scanner phase for the console (bit-exact across
      block splits); 6.87 Hz, 0.35 / 0.7 / 1.1 ms p-p sine; measured
      V1 ±14, V2 ±27, V3 ±41 cents (hypothesis 13 / 26 / 40.6 — kept).
      C = 0.5 (dry + scanned). Every switch crossfades on a 40 ms
      integer ramp (largest step 0.98–1.0x the steady signal's own);
      the fade to off ends at exactly 1.0 dry and drops the state. Off
      allocates nothing; lone-8' pin survives; eight reference renders
      bit-exact. 18 → 33 tests. Example `organ_scanner.json` (banked).
      Follow-ons: the scanner's rounded-triangle sweep (a sine ships);
      a scanner phase that persists across off → on.
- [x] **CLOSED 2026-09-22 (4517234, box ticked 09-24) — `organ` — FINDING: the partial phase accumulator drifts a
      float32 ulp between 64 and 512 after ~0.55 s** (pre-existing; the
      existing pin spans 4096 frames). An integer-count phase like the
      scanner's would make it exact but changes the render bits —
      spec-on-pick, with the reference-render recipe.
- [x] **`tape` — `stop` gate, `stop_time` / `start_time` — SHIPPED
      2026-09-19 (aedd1b9).** Speed 1 → 0 linear over `stop_time`
      (Hilbert IF 329.9 / 219.8 / 109.9 Hz at 1/4 1/2 3/4 on 440), wet
      level follows the speed (0.749 / 0.499 / 0.250) to exact silence,
      spin-up on the fall, `_gate_ramp_env` counts, a mid-ramp release
      re-articulates, no click. Lag = the running sum of the deficit,
      reset at a restart that finds the head stationary, so after a
      cycle the wet runs `start_time/2` behind the dry (11024.5 samples
      after one, two, three cycles — bounded); a release before the
      halt keeps the lag, clamped to the ring's `(stop_n + start_n)/2`.
      Hiss scaled by speed too. Neutral + patched = bit-exact `src`
      until the first stop; seven reference renders bit-exact; the
      stop's own rows bit-exact 64 vs 512. 25 → 47 tests. Example
      `tape_stop_drop.json` (banked). Follow-ons: is linear the right
      coast, or slow-then-fast; save the transport across Stop?
- [x] **CLOSED 2026-09-22 (063e7d5, box ticked 09-24) — `tape` — FINDING: the shipped wow/flutter read (`absidx -
      delay`) and the sat → bump chain are block-exact only to a
      float32 ulp at rare samples** (4 in 4 s, gate unpatched) — the
      ring index rounds at its magnitude. Fix = split whole + fraction
      as the stop read does; moves the reference renders by <= 1 ulp,
      so its own pass with fresh references. The chorus core likely
      shares it.
- [ ] **Recipe note:** agent worktrees branch from the last PUSHED
      commit, not main's HEAD — push before a batch, or expect the
      agents' suite counts to run low and an import-line conflict.

## The keep-list, continued (2026-09-19, night)

- [x] **`vowel` — SHIPPED 2026-09-19, module #99.** Filters & EQ.
      Matthew: "vowel please Claude". Spec into MODULE_IDEAS first,
      built verbatim. The formant filter: five parallel RBJ constant-
      peak bandpasses at F1..F5 of a sung vowel from the classic
      five-formant table (five voice types x A E I O U x frequency /
      level dB / bandwidth, the Csound numbers), `vowel` a continuous
      0..4 knob (frequencies interpolate geometrically, bandwidths
      linearly, levels in dB — the pure helper `vowel_formants`),
      `vowel_cv` x `cv_depth` vowels per unit (block mean, clamped),
      `voice`, `resonance` (a Q multiplier), `gain` dB makeup (the
      bank is ~15 dB down on a saw), `mix` (0 = the input buffer itself,
      nothing runs). Voice-aware like `filter` (lfilter along the last
      axis, zi (5, V, 2)); coefficients cached on (voice, vowel,
      resonance); block-size exact at a constant vowel. Measured: noise
      through tenor A/E/I/O/U peaks at 666/397/285/378/361 Hz vs table
      F1 650/400/290/400/350. 22 tests in `tests/test_vowel.py`.
      Example `vowel_talk.json` (banked): a supersaw says A-E-I-O-U.
      Suite **4045**, 142 examples, 99 modules. Follow-ons: a
      `formant_cv` shift (all formants up/down together — a
      child/giant knob); per-sample vowel_cv for audio-rate mouth
      modulation; custom vowels (user F1..F5).

## Five more love passes in parallel (2026-09-19, evening)

Matthew: "pick out another 5 modules for extra love and lets run
subagents on them". Same recipe as the morning batch with per-module
scratch folders; five worktrees, cherry-picked in landing order (one
appendix-bullet conflict, both kept). Suite **3909 → 4019**, 141
examples. Ears/eyes items are in the listening checklist above.

- [x] **`supersaw` — `detune_cv` + `detune_cv_depth` — SHIPPED
      2026-09-19 (221b676).** Block-rate by design (the detune is a
      per-block frequency table; a block-to-block ratio change is a
      slope change, never a click); per-voice from a `(V, F)` source
      came free (the `mult` table grows a voice axis); the 1/sqrt(sum
      g^2) normalisation never depended on the detune, so the CV path
      inherits the no-pumping contract (0.18 max/min RMS across the
      sweep, the knob's own wobble). Depth is bipolar -2..2 (a falling
      riser costs nothing). 10 reference renders bit-exact. 13 → 26
      tests. Example `supersaw_detune_rise.json` (banked): a gate-mode
      fg sweeps 0.05 → 0.95 over 7.5 s while two lowpasses open.
- [x] **`mid_side` — `side_hp` (bass mono) — SHIPPED 2026-09-19
      (1c5b8ab).** The 2026-08-03 follow-up. A 12 dB/oct RBJ highpass
      (Q 0.707) on the SIDE only, before width and the decode; `mid`
      untouched; 0 = the filter is never CALLED (pinned with a
      raise-if-called monkeypatch). Measured on a 60 Hz + 1 kHz tone on
      `in_l` with the corner at 120: L-R at 60 Hz -12.3 dB (a Q-0.707
      2-pole is exactly -12.3 dB an octave under — the spec's "> 15"
      was wrong, pinned > 10 at 60 and > 15 at 40), 1 kHz -0.00 dB.
      State-carry lesson: scipy's own association of the DF-I
      recurrence is float64-exact where the house history->zi rebuild
      is off by 1e-13; 1/64/4096-sample blocks pinned bit-exact with
      512. 13 → 28 tests. Example `mid_side_bass_mono.json` (banked):
      a chorused pad + 55 Hz sub — the chorus smears the sub into the
      side (L-R/L+R 0.37 in 40–70 Hz), `side_hp` 120 brings it to
      0.076 (-13.8 dB, theory -13.7) with the pad's width untouched.
      `mid_side_breathe.json` (Matthew's favourite) bit-exact unchanged.
- [x] **`sequencer` — `direction` + `seed` — SHIPPED 2026-09-19
      (9d9c5fe).** forward | backward | pendulum | random, one pure
      helper `next_step_index` in the module that the renderer calls
      at each edge and the tests call directly. Pendulum plays the
      turnaround once (steps 5 → 1 2 3 4 5 4 3 2; steps 1 → 1 1 1);
      random draws one `integers(steps)` per edge (none at steps 1);
      **`reset` drops the Generator as well as rewinding, so a reset
      REPLAYS the same random phrase** — a random line is loopable per
      bar; reset lands on step 1 (forward/pendulum, heading up) or on
      `steps` (backward); a mid-run `steps` shrink treats the playing
      step as the new last step. `fader_seq` inherits both params by
      contract (its DEFAULT_PARAMS IS the sequencer's) and its panel
      grew the combo + seed. 5 reference renders bit-exact; the
      default is also pinned against a verbatim copy of the old inner
      loop. 13 → 41 tests. Example `sequencer_pendulum.json` (banked).
      Test lesson: `rng.bit_generator.state` before/after proves "no
      draw was consumed".
- [x] **`pluck` — `vel` — SHIPPED 2026-09-19 (61259c1).** A knobless
      multiplier on the BURST, latched at the trigger edge, the loop
      untouched (0.5 renders as exactly half of the same string —
      linear loop); a re-pluck adds a burst scaled by its own vel;
      voice-aware like the other jacks. Design decision from a probe:
      every hit also relocks the pitch and clears the allpass state,
      and that clear alone steps the output by 7–18% of the ring — an
      audible tick on a hit that should make no sound — so `vel <= 0`
      is a SILENT hit that leaves the string bit-exactly as it was.
      23 reference renders bit-exact. 21 → 34 tests. Example
      `pluck_velocity.json` (banked): hit peaks track the velocity bus
      within +-0.01, corr 0.975. Follow-ons: velocity → `color` (needs
      its own depth knob); and the question whether a hit should clear
      the allpass at all (changes every reference render — a recipe
      job of its own).
- [x] **`delay` — `freeze` gate — SHIPPED 2026-09-19 (02b7d6f).** The
      echo hangs: unity recirculation, `tone` bypassed, input muted
      from the line, dry/mix untouched. THE FINDING: a unity loop
      through the fractional linear-interp read is NOT lossless —
      9–11 dB lost in 10 s at any fraction (the two-tap lowpass eats
      the top octave) — so while held the read snaps to `round(time)`
      samples (latched per voice at the rising edge; `time_cv` and the
      knob stop moving it while held) and the loop is `buf[n] = buf[n -
      D]` bit for bit: energy conserved to 1e-9 dB over 10 s. One 10 ms
      `_gate_ramp_env` crossfades gain, bypass, mute AND read position
      (the position slides half a sample at most). 7 reference renders
      bit-exact; 64 vs 512 bit-exact. 22 → 43 tests. Example
      `delay_freeze_stutter.json` (banked). Test lessons: the reverb's
      click reference is TAUTOLOGICAL at the rise of a lossless loop
      (the loop captures the ramp's own writes and repeats them) — pin
      per edge, pre-only at the rise, max(pre, post) at the fall, and
      self-test the tripwire with the ramp collapsed; and lag-D
      correlation doesn't prove a stutter (an ordinary echo correlates
      at lag D) — persistence at lag k·D for k = 3..8 does.

## The keep-list, continued (2026-09-19, later)

- [x] **`cv_recorder` — SHIPPED 2026-09-19, module #98.** CV &
      Utilities. Matthew: "cv_recorder please Claude" (asked for once,
      interrupted for the subagent batch, asked for again). Spec into
      MODULE_IDEAS first, built verbatim plus one rule. The modulation
      looper: `in` (or the `value` knob when unpatched — the recorded
      GESTURE, ramped across each block), `rec` (record while high; the
      first edge creates the loop), `clear`, optional `clock`; `out`
      (the loop; while recording, what is written) + `pos` (0..1).
      Fixed-length: `length` seconds, or clock TICKS when clocked (16
      sixteenths = a bar). `mode` replace (punch-in) | overdub (old x
      `feedback` + new). Clocked: rec on/off edges honoured on the NEXT
      tick, hard sync to 0 every `length`-th tick, and — found while
      building — a rec edge that would create the loop waits for the
      clock's second tick (the period is unknown before it). Events
      (clear / rec / tick, in that priority at a shared sample) split
      the block into vectorized slices; a patched-`in` render is
      block-size independent (pinned 50 vs 250 with rec edges and a
      clear mid-stream). 18 tests in `tests/test_cv_recorder.py`.
      Example `cv_recorder_layers.json` (banked): every other bar
      overdubs an out-of-step LFO at feedback 0.6 onto a bar-long loop
      that moves a filter — it evolves, never accumulates. Suite
      **3909**, 136 examples, 98 modules. Follow-ons: a `play` gate or
      `mute`; half-/double-speed playback; a `reverse`; saving the loop
      in the patch (today it lives in backend state and is lost on
      Stop — the honest gap for a *performance* capture).

## Listening session checklist (banked, 2026-09-19)

Everything that shipped since the last quiet room, in the order that
makes sense to sit through. Load the file, press Start, and the note in
brackets says what to listen or look for. Tick as you go; anything that
sounds wrong, one line here is enough and Claude takes it from there.
(The slice-3 sampler and older items are still in "Later / wishlist".)

**First run `python examples/samples/generate_samples.py` once** (the
sampler examples need it).

*The sixth batch (2026-09-24) — start here.* An offline audit rendered
every example before and after the batch: **84 of 164 are bit-identical**
(their lines below stand as written), **~55 moved only at rounding level**
(below −88 dB — nothing to hear), and the rest are listed here by cause.
**29 examples render silent offline because they wait for a live key,
MIDI or the mic** — those need your hands either way.
(The audit is `tools/render_audit.py` — `render` / `compare` / `bisect`;
its docstring has the whole session.)
- [ ] `autopan_pluck_bounce.json` — **module #101.** Alternate eighth-note plucks land left, then right, with no click at the flip; the reverb wash stays centred. The first note plays centred (before the clock lock). Try `shape` triangle / sine, `law` compromise, `tremolo` 1 (a mono tremolo). EYES: the autopan panel.
- [ ] **Pluck re-plucks (`carry` now on by default)** — the loudest movers: `freeze_wide_wash` (the freeze holds the difference), `clock_transport`, `delay_freeze_stutter`, `sequencer_pendulum`, `sequencer_reverse_bars`, `pluck_velocity`, `pluck_velocity_color`, `possibility_selector_kit`, `pitch_shifter_shimmer`, `reverb_freeze_pad`. A re-plucked string should now continue under the new hit instead of stepping. If any of these sounds worse, untick `carry` on that node and say which.
- [ ] `freeze_drone_breathe.json` — the stereo image reshuffled (which partials lean left vs right; same levels and correlation). Then put `size` on **65536** on `freeze_chord_pad.json`: the hold starts 93 ms after the gate (by design), is it a longer, smoother moment?
- [ ] `chorus_lush.json` — its `rate_cv` is read per sample now: the sweep's speed glides instead of stepping.
- [ ] **The oscillator's edge samples** — naive saw/square at 110/220 Hz moved an edge sample per cycle (about 22 µs; the new one is the correct one). These should sound IDENTICAL; flag any that don't: `fat_saw`, `meter_levels`, `mixer_crossfade_verb`, `noise_gate_chop`, `limiter_brickwall`, `transient_shaper_snap`, `tilt_eq_seesaw`, `parametric_eq_bass`, `motion_eq_focus`, `phaser_sweep`, `freq_shifter_barberpole`, `flanger_jet_sweep`, `flanger_through_zero`, `clock_swing_breathe`, `distortion_drive`, `sidechain_pump`, `meter_stereo_master`.
- [ ] **Buffer slider, new behaviour:** play `flanger_jet_sweep.json` at buffer **64** — the deepest sweep used to be clipped there and now reaches full depth. Play `organ_leslie.json` and move the buffer slider while it runs — the rotor no longer resets.
- [ ] `clock_divider` — no example audio changed, but try `pw` 0.9 + `swing` 0.3 on `divn` into an ADSR: every gate should be its own note now.
- [ ] EYES: a CV meter on a broken (NaN) cable reads `nan` and holds its bar, and recovers when the cable does.
- [ ] Gain check (pre-existing): `organ_leslie`, `noise_hat`, `clock_divider_swing` hit full scale; `resampler_tape_stop` has a DC offset.


*The new modules (seven):*
- [ ] `freeze_chord_pad.json` — **module #100.** Each sus2 chord plays a second, then hangs as a glassy pad for the rest of the bar and melts into the next. Is it a pad or a buzz? Try `smear` 1 (the wash), `pitch` -12 (a sub-pad under the organ), `size` 16384 (a longer, smoother moment), and the `freeze` tickbox under a held keyboard chord. EYES: the `size (fft)` combo, the `freeze (or gate)` tickbox.
- [ ] `bowed_cello.json` — does it read as a bowed string? Try `pressure` 0.8 (crunch), `position` 0.06 (sul ponticello), `body` 0 with the scope (the raw sawtooth). The 88% gates should re-bow without a gap.
- [ ] `wind_duet.json` — flute over reed. Flute `breath` 0.8 should go sharp and airy; reed `breath` 0.05 must NOT speak and 1.0 should choke — both on purpose. Is the flute a flute?
- [ ] `drift_wander.json` — the filter breathes with no corners (the scope shows the wander); the pluck lands exactly when the wander turns; the drone's 36-cent walk reads as an old oscillator, not vibrato. Try `glide` 0 (pure S&H) and `walk` with `step` 0.05.
- [ ] `cv_math_delayed_vibrato.json` — no wobble at each note's attack, full at the sustain (scope on the `mult` jack). Move the filter's cable from `max` to `min` / `avg` / `diff`.
- [ ] `cv_recorder_layers.json` — the loop that moves the filter changes every OTHER bar and never piles up. Then the real test: unpatch `in`, cable a `constant` 1.0 into `rec`, and turn the `value` slider — your gesture should come back every bar.
- [ ] `vowel_talk.json` — the talking pad: does the supersaw say A-E-I-O-U as the LFO sweeps? Try `voice` soprano vs bass, `resonance` 2.5 (more vowel), and a noise source instead of the supersaw (a whisper). EYES: the `vowel (A E I O U)` slider and the `voice` combo.
- [ ] `possibility_selector_kit.json` — whether x which drums + the harp. EYES: cells paint one hue per output (red / blue / green / purple), amber for `?` and subsets; right-click a cell opens FOUR checkboxes (never seen in a window yet).
- [ ] `possibility_reroll_divider.json` — kick and snare hold a take for four bars then re-decide on bar 5; the hat re-deals every bar.

*The love passes (thirty-one):*
- [ ] `noise_stereo_pair.json` — two brown tides, one per side (corr 0.09). Set **both seeds the same** and it should collapse to dead centre. Then sweep the new `corner` knob live on a brown: 2 Hz (distant thunder) to 40 Hz (tight wind) — it should not click.
- [ ] `vowel_robot_talk.json` — the 25 Hz buzz: does the audio-rate mouth read as a robot or as noise? EYES: the `cv_rate` combo and the five `f1..f5` Hz drags under `voice` custom.
- [ ] `clock_swing_breathe.json` — straight at 0 s, full triplet at 10 s, straight at 20 s, forever, over a kick that must NOT flutter (that's the divider fix). EYES: the `swing_cv_depth` drag.
- [ ] `freeze_drone_breathe.json` — a 743 ms capture (`size` 32768) with `latch` on: one trigger holds it, the next releases. Does the longer window read as "an average of a phrase"? EYES: `latch`, `width_cv_depth`, the extended `size` combo.
- [ ] `cv_recorder_oneshot.json` — a gesture fired as a one-shot at a different rate each time. Flip `speed` and `play_mode` live. EYES: the `play_mode` combo, `speed_cv_depth`.
- [ ] `pluck_touch.json` — soft notes dull AND round, accents bright and plucky. **Then the decision: untick `carry` and A/B a re-pluck** — with it on, a hit no longer interrupts the ringing string.
- [ ] `phaser_envelope_sweep.json` — the envelope phaser (every note drags the notches up 2.5 octaves). `flanger_jet.json` — through-zero at `spread` 1, one sweep every two bars. EYES: `division` / `spread` / `manual_depth` on both nodes.
- [ ] **A-B the two chaotic ones** — `bowed_cello.json` and `wind_duet.json` render differently after the CV sweep (exactly, where they were block-dependent before). Do they still sound right? If yes, nothing to do; if not, say so and I'll revert those two sites.
- [ ] `vowel_giant_child.json` — giant → child over 25 s while the mouth talks; does constant-Q read as "the same vowel, a different throat"? EYES: the `formant` drag + `formant_cv_depth` in the vowel block.
- [ ] `pluck_velocity_color.json` — the accents ring bright, the soft picks thud; A/B by flipping `vel_color` 0.8 → 0. Then `midi_input.velocity_cv -> pluck.vel` live.
- [ ] `lfo_random_replay.json` — the same 12-step random wah every 2 s bar: does the written-down accident read musically? EYES: `seed` on the lfo node — SEEN 2026-09-20 in a screenshot of Matthew's LFO-into-LFO siren patch (two lfo nodes with `reset` jack, `phase` `0.00 cyc`, `seed` 0; the oscillator's `pw_cv` jack, `pulse_width` and `pw_cv_depth` `0.50 width/unit`): every widget paints, ASCII, bounded. The live-drag checks (phase re-anchoring on a running LFO) and all ears items remain.
- [ ] `sample_hold_prob_sweep.json` — the melody frees up around 5 s and gets stuck around 15 s, every 20 s. EYES: the `prob_cv_depth` drag and the new jack.
- [ ] `clock_swing.json` — a 0.3 shuffle on the hats against a dead-straight `div4` kick; does it swing? EYES: the `swing (0.33 = triplet)` slider.
- [ ] `sequencer_reverse_bars.json` — the palindrome: bar 1 up, bar 2 its mirror. Try `direction` pendulum on it (the gate turns it around at the bar lines). EYES: the third jack on both sequencer panels.
- [ ] `freeze_wide_wash.json` — each phrase blooms into a wide wash that dies in six seconds; `width` 0 vs 0.8 on the chord pad — wide, or just louder? EYES: the two new jacks, `width`, `decay (0 = forever)`.
- [ ] `cv_recorder_backwards.json` — the wobble freezes on each downbeat, alternate bars run backwards; flip `speed` to `2x` and `0.5x` live. EYES: `reverse (or gate)` + `speed (play; rec is 1x)`; a live checkbox flip mid-loop turns the loop around with no jump.
- [ ] EYES only: the `freeze (or gate)` tickbox on the reverb and delay panels — tick with nothing patched, the pad / stutter hangs; untick, it releases.
- [ ] `noise_brown_surf.json` — brown noise as surf: does the 10 Hz leak read as waves, or does it want a `corner` knob? Switch `color` to violet (hiss) and set two seeds the same on a stereo pair. EYES: the four-item `color` combo + `seed` on the noise node; pluck's `color` slider unchanged.
- [ ] `sample_hold_sometimes.json` — a random line that repeats notes (prob 0.6); the 50 ms glide is a zip through the scale — should it sit AFTER the quantizer instead? Flip `mode` to track with a long-pulse clock. EYES: the `mode` combo shows sample/track (not the filter's modes).
- [ ] `clock_transport.json` — two bars on, one held, the A-B-C-A figure snapping to the bar line; is the ±2% breath gentle or a stumble (0.02 if so)? EYES: three new jacks on every clock node in 57 patches — panels still look right?
- [ ] `organ_scanner.json` — the scanner on c3: is it a Hammond? Flip v1 / v3 / c1 live under a held chord — the 40 ms crossfade should be invisible. EYES: the `vibrato` combo.
- [ ] `tape_stop_drop.json` — dive / halt / spin-up every 8 s. Is linear-in-speed the right coast, or does a slow-then-fast feel more like a deck? At `mix` 0.5 the doubling after a stop is the documented physics.
- [ ] `lfo_retrigger.json` — every note opens at the top of its tremolo (unpatch `sequencer.gate -> lfo.reset` to hear it go back to drifting). EYES: drag the `phase` slider on a running LFO — it re-anchors live.
- [ ] `filter_resonance_sweep.json` — the peak breathes on its own cycle whatever the cutoff does. EYES: `res_cv_depth` reads "dbl/unit" beside `cv_depth`'s "oct/unit".
- [ ] `oscillator_pwm.json` — the PWM pad. No pumping at the width extremes (DC-compensated), no zipper under the faster LFO. EYES: `pulse_width` slider + `pw_cv_depth` on the oscillator panel.
- [ ] `adsr_velocity.json` — accents loud-loud-mid-soft-soft-soft-mid-loud, hard notes brighter too. Then the live one: `midi_input.velocity_cv -> adsr.vel` on the real keyboard.
- [ ] `reverb_freeze_pad.json` — the Cmaj7 strum hangs as a pad between strikes. Is the 10 ms catch fast enough; is the bypassed-damping pad too bright?
- [ ] `sequencer_pendulum.json` — the pendulum line on eighths against a `random` hat bar that REPLAYS every reset (its step pitches are the accents). EYES: the `direction` combo + `seed` on the sequencer node and above the fader bank on `fader_seq`.
- [ ] `delay_freeze_stutter.json` — the 187.5 ms dotted-sixteenth hold reads as a tumbling beat-repeat, the phrase over it clearly dry. Freeze a sustained tone whose period doesn't divide the delay: the lap seam should be a smooth wobble, not a tick.
- [ ] `supersaw_detune_rise.json` — the riser: does the exponential rise open late enough; does the filter's 2.8-octave sweep want more or less? EYES: `detune_cv_depth` is bipolar (-2..2 "det/unit") so a negative depth is a falling riser.
- [ ] `mid_side_bass_mono.json` — toggle `side_hp` 120 vs 0 live: the sub should snap to the middle and the pad should NOT narrow. (`mid_side_breathe.json` is bit-exact unchanged.)
- [ ] `pluck_velocity.json` — do the accents breathe; is 0.3 too quiet a floor? Then `midi_input.velocity_cv -> pluck.vel` on the keyboard.

*Still owed from earlier sessions:*
- [ ] `fg_eor_swell_strike.json` — swell then strike, one gesture? Is the 12 s starter poke audible?
- [ ] `krell_feedback.json` + `envelope_follower_wah.json` — the real krell self-patch and the self-wah (alive since the door). EYES: the closing cable paints AMBER and thicker, the toolbar reads `loops 1`, drawing a loop live puts "Loop closed: ..." in the status bar.
- [ ] `granular_cloud.json` / `granular_haze.json` / `granular_freeze.json` (tap F to freeze) / `granular_beat_repeat.json` — the sideband on held notes: "granular" or "out of tune"? Does the freeze edge click? Is `mix` 0.6 right on the beat repeat?
- [ ] `organ_leslie.json` — THE pairing; does the drum's lag behind the horn read as a Leslie?
- [ ] `pitch_shifter_shimmer.json` + `pitch_shifter_harmonizer.json` — the octave bloom; the stereo triad (wide, or just split?).
- [ ] `chord_legato_inversions.json`, `slew_clocked_glide.json` (change the BPM while it plays), `clock_divider_swing.json` (RE-LISTEN — the fills_cv cable was dead until 09-16; the snare should breathe 2 -> 6 fills over ~30 s), `drum_dynamics.json`, `modal_mallets.json` (xy scope draws a cloud when `spread` is up).
- [ ] Sampler: `sampler_mellotron.json`, `sampler_scrub.json`; EYES on the sampler's `mode` dropdown (one_shot / gated / loop — `gated` was unreachable until 08-30) and its waveform face; `midi_input.velocity_cv -> sampler.vel` on the keyboard.

## Five love passes in parallel (2026-09-19)

Matthew: "i've banked a bit of credit this session like can i get you to
fire up subagents for say 10 modules you decide upon to get some love?"
— then "Just5". Five subagents, one module each, each in its own git
worktree on the love-pass recipe (reference renders captured BEFORE
editing, the feature ships OFF at its default, bit-exact pinned),
cherry-picked onto main one at a time. Every pass brought its tests,
its MODULES.md entry and an example. Suite **3762 → 3886**, 135
examples. Ears/eyes items below are BANKED.

- [x] **`lfo` — `reset` gate + `phase` param — SHIPPED 2026-09-19
      (bda4f53).** A rising edge restarts the phase at `phase` ON THAT
      SAMPLE (segments between edges, vectorized per segment); `phase`
      is the free-running start too and re-anchors a running LFO when
      the knob moves; per-voice on the voice path, any-voice-high on
      the mono path; `random` re-rolls on reset. 62 reference renders
      bit-exact. Fixed in passing: `random` at a non-zero start phase
      used to output 0.0 until its first wrap. 15 → 37 tests.
      Example `lfo_retrigger.json` (banked): every note opens at the
      top of its tremolo. EYES: the `phase` slider re-anchors live.
      Follow-on: a `seed` for the `random` waveform (still global
      `np.random`).
- [x] **`filter` — `resonance_cv` + `res_cv_depth` — SHIPPED 2026-09-19
      (cd347f2).** Q doublings per unit (`motion_eq`'s convention),
      block mean like `cutoff_cv`, per-voice when the source is
      `(V, F)`; cv +1 at depth 1 IS the `resonance`-doubled render
      bit-exact; exponent clipped to +-64 so an absurd CV cannot raise
      OverflowError. 86 reference renders bit-exact. 29 → 51 tests.
      Example `filter_resonance_sweep.json` (banked): the peak breathes
      on its own nine-second cycle. LATENT TRAP found (not fixed):
      `cutoff_cv` and every other `2.0 ** (depth*cv)` block-mean path
      will raise OverflowError in the audio thread on a CV > ~1024 —
      the resonance path guards it; the cutoff family should too.
- [x] **`oscillator` — pulse-width modulation — SHIPPED 2026-09-19
      (cb6eea0).** `pulse_width` 0.05..0.95 + per-sample `pw_cv` x
      `pw_cv_depth` (width per unit) on `square` / `square_blep`,
      voice-aware like `freq_cv`; the pulse's DC `2pw - 1` removed per
      sample; the falling-edge PolyBLEP runs on the falling edge's OWN
      phase with a window sized by its own increment (exact for widths
      moving slower than the phase — every one of 163 edges under a
      30 Hz LFO got exactly one correction pair). Alias fraction at
      width 0.1: 0.0018 blep vs 0.108 naive. `square_wt` stays a fixed
      50% table (documented limit). 68 reference renders bit-exact.
      31 → 65 tests. Example `oscillator_pwm.json` (banked): the PWM
      pad. Test lesson: 220 Hz x 2205 samples is exactly 11 cycles —
      pin block-size independence at a frequency coprime with 44100.
- [x] **`adsr` — `vel` input — SHIPPED 2026-09-19 (115fc17).** A
      knobless multiplier read at the gate's rising edge and latched
      for the note (release included), the drums' rule; stages run in
      output space so the TIMES stay what the knobs say; a re-strike
      softer than the ringing level falls to the new peak at the
      full-velocity attack slope (no jump — max step == the attack
      slope); per-voice from a `(V, F)` vel, loudest-voice on a mono
      gate. 14 reference arrays bit-exact. 8 → 21 tests. Example
      `adsr_velocity.json` (banked): seeded accents. Live wish:
      `midi_input.velocity_cv -> adsr.vel` on a real keyboard.
      PRE-EXISTING (not vel's): the voice-path ADSR lands its
      attack/decay crossing +-1 sample across block sizes (one stage
      step); mono is bit-exact. Fixing it changes shipped renders.
- [x] **`reverb` — `freeze` gate — SHIPPED 2026-09-19 (5a9d3b4).**
      The tank's Sylvester-Hadamard mix is orthonormal, so a unity loop
      with the damping bypassed is lossless BY CONSTRUCTION: freeze gain
      exactly 1.0, energy conserved to < 0.01 dB over 10 s, held RMS
      within +-0.35 dB, the unfrozen tail at -188 dB by +5 s; a 10 ms
      integer-count ramp (`_gate_ramp_env`) crossfades in and out
      (edge steps 0.0005 vs the tail's own 0.013); input muted from the
      tank, dry path untouched. 8 reference renders bit-exact. 19 → 32
      tests. Example `reverb_freeze_pad.json` (banked): a Cmaj7 strum
      hangs as a pad every six seconds. Follow-on: a `freeze` PARAM
      (checkbox latch) beside the gate, like granular's.

## The keep-list, continued (2026-09-19)

- [x] **`cv_math` — SHIPPED 2026-09-19, module #97.** CV & Utilities
      (filed beside cv_scale/cv_offset, not under Modulation where the
      one-liner sat). Matthew: "cv_math please Claude". `logic` for
      voltages: `a`, `b` in → `min` `max` `avg` `diff` `mult` `rect`
      `inv` out, all live, zero params. `mult` is the addition to the
      one-liner — the CV x CV multiplier the rack had no jack for (an
      envelope on a vibrato's depth). An unpatched operand reads 0, so a
      lone `a` makes `max`/`min` its half-wave rectifiers and `diff` a
      bit-exact passthrough (the normalled trick, like logic's NAND).
      Shape-polymorphic with numpy broadcasting (poly x mono; two polys
      of different width line up on the smaller). Stateless, exact. 13
      tests. Example `cv_math_delayed_vibrato.json` (banked): vibrato
      LFO x ADSR through `mult` (absent at the attack, full at the
      sustain) + `max` of two slow LFOs on a filter. Suite **3762**, 130
      examples, 97 modules.
- [x] **`drift` — SHIPPED 2026-09-19, module #96.** Modulation. Matthew:
      "drift please Claude?" Spec into MODULE_IDEAS first, built to it
      verbatim. A smooth wandering random CV: targets every `1/rate` s
      (`smooth` = fresh uniform in +-1; `walk` = previous + `step` x
      N(0,1) reflected at +-1), a half-cosine glide over `glide` of the
      interval then a hold, each new curve starting FROM THE CURRENT
      VALUE so a tick mid-glide (rate_cv sweep, clock jitter) never
      jumps. Three outputs off one die: `cv` (smooth), `stepped` (the
      S&H twin), `trig` (1 ms pulse per tick, carried across blocks).
      `rate_cv` octaves per block; `clock` patched = ticks on edges,
      glide from the measured interval. The tick schedule is an
      INTEGER sample count (`last_tick + round(sr/rate)`), not a float
      phase, so renders are bit-exact at 64 vs 2048 (the clock module's
      phase accumulator is not — the selector test found that). 24
      tests in `tests/test_drift.py` incl. the mode-combo trap. Example
      `drift_wander.json` (banked): one drift breathing a filter,
      stepping a quantized pluck AND firing it; a 36-cent walk on the
      drone's pitch. Suite **3745**, 129 examples, 96 modules.

## The physical-modeling family, sustained (opened 2026-09-18)

Matthew's pick off the 2026-08-04 keep-list: "bowed/wind". The spec
was promoted from the one-liner into MODULE_IDEAS.md first (both
halves), then built — the selector's precedent.

- [x] **`bowed` — SHIPPED 2026-09-18, module #94.** Sources. Smith's
      bowed string as in STK `Bowed`: bridge (`position` x L) and nut
      delays meeting at a friction table, `dv * min(1, (|dv*slope| +
      0.75)^-4)` injected into both halves, bridge reflection
      -0.95*one-pole (`damping`), nut -1; the bow's two hands
      (`pressure` = the table's slope, `velocity` = bow speed) with
      `attack`/`release` integer-count ramps on the gate and per-sample
      `pressure_cv`/`velocity_cv` (x `cv_depth`, mono); a five-band
      body bank mixed by `body`. Advanced in vectorized chunks <= the
      bridge delay (the pluck precedent with a nonlinearity inside the
      loop — elementwise, so it chunks the same), slice buffers
      compacted once per block. Tuning: L = sr/f0 - the one-pole's
      EXACT phase delay at f0, fraction on the bridge side as a
      linear-interp read — within +-6 ct C2..C6 measured, +-10 pinned.
      Cost measured (one voice, 44.1 kHz): 0.5 ms of an 11.6 ms block
      at C4, 1.7 ms at C6 — a solo instrument by design; silent voices
      free. 27 tests in `tests/test_bowed.py` (sustain bounded, pitch,
      release to exact zeros, attack, the two hands, per-sample CVs,
      position/damping/body spectra, voices, mono == single voice,
      block-size independence with the bow landing AND lifting
      mid-stream, re-bow during release doesn't jump, early-out,
      widgets, the example). Example `bowed_cello.json` (banked): an
      eight-step line two octaves down through a slew + vibrato LFO,
      88% gates re-bowing each note, a slow swell on `velocity_cv`, a
      scope on the Helmholtz sawtooth. Suite **3680**, 127 examples.
      Prototyping findings: STK's flute is tuned to 1.5 periods ("we're
      overblowing here") — worth knowing before the wind half.
- [x] **`wind` — SHIPPED 2026-09-18, module #95.** Sources. The blown
      half: `model` flute | reed. Flute = STK Flute (jet delay 0.32 of a
      bore tuned to 1.5 periods x a measured 1.5% regime correction —
      the overblown register, STK's "we're overblowing here"; jet
      table x(x^2-1) clamped; 0.5/0.5 reflections; one-pole + DC block
      in the return; breath mapped 0.85..1.4 — the jet saturates and
      dies above ~1.5). Reed = STK Clarinet mouthpiece (one round-trip
      delay, reed table clip(0.7 - 0.3 dp), -0.95 x one-pole reflection;
      breath mapped 0.58..1.0 — it needs ~0.58 to speak and closes
      above ~1.1, both physical). Shared with `bowed`: the slice-buffer
      chunked loop (<= the jet / the bore), the integer-count gate ramp
      (`_gate_ramp_env`, extracted from bowed), per-block pitch, early-
      out. Breath noise = a per-(voice, note) seeded stream drawn per
      segment between rising edges, so a note is block-size exact;
      output DC-blocked. Tuning: reed +-5 ct C2..C6 measured (+-8
      pinned); flute +-4 ct C3..C6 at moderate breath (+-10 pinned),
      sharp blown hard low down. Cost < 0.5 ms/block per voice across
      the range. 32 tests in `tests/test_wind.py`. Example
      `wind_duet.json` (banked): a flute tune an octave up with vibrato
      and a breathing LFO over a chalumeau reed line at half speed.
      Suite **3716**, 128 examples, 95 modules. Deviation from the
      docstring's first draft: the "odd harmonics" clarinet claim did
      NOT survive measurement (the model's even harmonics come and go
      with breath and noise) — the docs say harmonic-rich vs nearly
      pure instead, and that is what the test pins. Trap: a stray -1
      sample carried over from the prototype's calibration knob made
      the flute 25 ct sharp at C6 — the prototype's `comp_extra=-1`
      had cancelled it; measure the MODULE, not the prototype.

## Media paths (2026-08-29)

- [x] **Relative media paths depended on the working directory** — FIXED
      2026-08-29, reported by Matthew ("didn't hear any audio? Maybe a
      file/path issue?"). `sampler_breaks.json` names its sample
      relatively and the backend resolved relative paths against the
      PROCESS CWD alone, so it played from the project root and rendered
      silence from anywhere else — `dist/` included. Never
      sampler-specific: `convolver` and the `file_player` examples share
      the same six call sites. `Patch.source_path` (stamped on load/save,
      never serialized) + one `_resolve_media_path` helper in front of
      all six: patch folder, its parent, cwd, resource root, first hit
      wins; absolute untouched; memoized per compile because the
      renderers use the result as a cache key. **And the silent-failure
      half**, which was worse than the path bug — `media_load_failures()`
      + a status-bar report, so a missing file says so once instead of
      leaving you to guess. 20 tests (`tests/test_media_paths.py`),
      including both shipped examples rendered from a foreign cwd. Suite
      **2950**. **VERIFIED in the real app 2026-08-29** — Matthew
      recompiled: "works fine now".
- [ ] **`disk_writer`'s output path is still cwd-relative** — deliberate
      and documented (it is a *destination*, not a lookup: there is no
      "search for where the user meant to write"), but worth revisiting
      if anyone is ever surprised by where their recording landed.

## Later / wishlist

- [x] **Every non-ASCII glyph painted as `?`** — FIXED 2026-08-21,
      spotted in Matthew's chaos_melody screenshot. DearPyGui's built-in
      font (ProggyClean) covers basic Latin only, so `◀`/`▶` on the jacks,
      em dashes in node titles and `→`/`…`/`≈` in status text all rendered
      as a replacement `?` — **336 port labels** across the 87 module
      types, plus 24 of 104 example patches, plus 11 UI strings. It had
      been that way since the node editor was written. Matthew's pick:
      **ASCII everywhere** (over bundling a TTF) — jacks now read
      `< in` / `x >`. Also swept the user-facing strings outside `ui/`:
      the cable-refusal ValueError and the audio-start RuntimeError both
      surface in the status bar, and two `cli.py` prints would *raise*
      UnicodeEncodeError when stdout is piped on Windows (locale codec) —
      a latent crash, not just mush. Tripwires: an AST walk over `ui/*.py`
      that flags any non-ASCII string constant **except docstrings**
      (developer prose keeps its typography), the same over every example
      patch's node names, and a self-test that proves the tripwire fails
      on a planted em dash. It caught two patches on its first run that a
      raw-text scan had missed — they stored the dash as a `\u2014`
      escape. Suite **2699 → 2816**.
      Later: if the plain `<`/`>` jacks ever grate, bundling a font is
      still open — the tripwire would need a matching exemption.
- [x] **GUI eyeball queue: EMPTY** — the scope face PASSED 2026-08-21
      ("Scope seems to work well"), `chaos_melody` sounded fine, and the
      chaos-x/y-into-scope-**xy** butterfly PASSED the same day
      ("Sweet"): `chaos.x/y -> cv_to_audio -> scope.in/in_r`, mode `xy`,
      `time_div` 500 ms/div, scope `gain` 0.50 — a clean Lorenz **wing**,
      correct attractor geometry (thin on the inner edge, fat on the
      outer). Settings matter and weren't obvious: the capture window is
      `time_div × 10 divisions`, so the default 10 ms/div shows a
      *tenth of one orbit* (an arc, not a loop) and the max 500 ms/div
      is a 5 s window ≈ 5 orbits at rate 1.0. Both wings at once needs
      more orbits in the window — raise chaos `rate` to ~2–3 — since
      lobe switches are irregular. Documented on the scope entry.
      Original entry: the scope's waveform face
      (60 fps repaint, trigger holds a saw still, dual/xy modes,
      freeze) and the chaos-x/y-into-scope-xy butterfly. Everything
      else is CLEARED: the 2026-08-04 six passed ears 2026-08-05, and
      the whole 2026-08-03 listening backlog passed 2026-08-05 too —
      `clockwork_groove` "something else… the most unique one i've
      seen so far, everything short of a drum machine but just as
      effective"; `chord_arp_factory` "works well, like a unique or
      random song"; `drum_machine` "is a drum machine";
      `pluck_strings` "cool" (Matthew's coming back to play more);
      `modal_bells` (also flagged for a longer revisit);
      `shift_random_melody` "another cool one"; `logic_offbeat_drums`
      :-{D; `mid_side_breathe` "pretty awesome, ima USE THIS IN
      MUSIC for sure"; `octaver_bass_lead` :-{D. Per-entry pendings
      below updated.
- [x] **Example: organ through the feedback door** — SHIPPED
      2026-08-21 as `examples/organ_feedback_drone.json`, Matthew's own
      idea from 2026-08-05 ("maybe feedback with the organ?") and his
      pick when the board cleared. Slow clock → 4-step sequencer
      spelling Cm7 (C G Eb Bb, FFT-verified) → organ (no percussion,
      it just holds) → matrix `in_1`; `out_1` → 700 ms delay → back
      into `in_2`, with the reverb OUTSIDE the loop as polish. `g21` is
      the whole patch: 0 dry, 0.65 sings (shipped), 0.9 blooms.
      Measured: peak 0.858 shipped with the per-2 s RMS breathing on an
      8 s cycle rather than growing; at g21 0.9/0.99/1.0 the soft
      ceiling pins it at exactly 1.000, finite — which is what makes
      "0.9 blooms" safe to write on the node. 11 tests (loop closed,
      regen row open, delay not double-regenerating, reverb out of the
      loop, cranked-ceiling sweep). Suite **2829**. NEEDS: a listen.
      **EARS PASSED 2026-08-21** ("That works well"; "definitely sounds
      organ like anyways" — fair, and diagnostic: at the shipped 0.65 the
      loop *supports* the organ rather than transforming it; 0.9 is where
      it stops sounding like an organ).
- [x] **Example: the shimmer variant** — SHIPPED 2026-08-21 as
      `examples/organ_shimmer.json`, Matthew's pick straight after the
      drone. Same head, same feedback door, with a `pitch_shifter` at
      **+12 inside the loop** so every lap returns an octave higher and
      the chord climbs away from itself. Two supporting changes, both
      load-bearing: `pulse_width` 0.45 (gaps for the cascade to bloom
      into) and delay `tone` 0.3 — the dark roll-off is what the climb
      dies against, otherwise it just accumulates hiss. Levels had to
      come down (organ 0.4→0.3, out 0.8→0.75, reverb mix 0.6→0.5): the
      first attempt pinned the output at 1.000, so a listener would have
      heard the ceiling instead of the effect. Shipped peak **0.836**,
      settles at 0.80× over 30 s, bounded at g21 0.85/0.99/1.0.
      Measured against the same loop with the shift at 0: **12×**
      one-to-two octaves up, **~41,000×** three and beyond. 11 tests.
      Suite **2842**. **EARS PASSED 2026-08-22** — Matthew: "sounded
      great". Both feedback examples are now heard, and the pair reads
      as intended: the drone supports the organ, the shimmer transforms
      it.
- [ ] **The 2026-08-04 brainstorm keep-list** — Matthew: "Can you add
      to the todo" (2026-08-04, with the full writeup pasted back).
      Twelve items, one-liners now living in docs/MODULE_IDEAS.md
      § "The 2026-08-04 brainstorm" — promote to a full spec when
      picked, the usual workflow. The menu:
      * Sources: ~~`bowed`/`wind`~~ (BOTH SHIPPED 2026-09-18, #94/#95 —
        see § "The physical-modeling family, sustained").
      * Modulation: `function_generator` — **SHIPPED 2026-08-23**, see
        § "The function generator" below,
        ~~`drift`~~ (SHIPPED 2026-09-19, #96 — see § "The keep-list,
        continued"), ~~`cv_math`~~ (SHIPPED 2026-09-19, #97, same §),
        `cv_recorder` (M — the modulation looper; nothing else
        captures performance).
      * Effects: ~~`rotary`~~ (SHIPPED 2026-09-14, module #91 — see below),
        `vowel` (S–M — formant filter bank, A–E–I–O–U morph),
        `freeze` (M — spectral freeze pad), ~~`autopan`~~ (SHIPPED
        2026-09-24, module #101 — see § "Nine follow-ons in parallel").
      * I/O: `midi_output` (M — gates/CV → notes/CCs; the rack as
        the brain of a hardware rig).
      * Endgame (architecture): **subpatch containers** (L — patches
        become modules; makes "running out" structurally impossible)
        and **snapshot morph** (M–L — knob scenes interpolated by
        one CV).
- [x] **`organ` + `chaos` — the fun pair** — BOTH SHIPPED 2026-08-04,
      built to the plan below in one session. organ: lone-8′ pin landed
      BIT-EXACT vs the sine oscillator (integer-counted gate ramp was
      the enabler); perc single-trigger pinned (legato doesn't
      re-fire); Nyquist mask = exact silence; 16-voice worst case
      RECORDED 2.82 ms = 26.5% of budget; drawbar fader bank shipped.
      chaos: all outs bit-exact across block splits incl. mid-block
      reset (absolute-sample control grid); 100k-sample soak clean;
      seeds decorrelate, same seed bit-identical; rate 50 = 2.2%
      budget. 34 tests; suite **2587**; examples `organ_jazz.json`
      (0.78 peak chord) + `chaos_melody.json` (self-playing, 0.63
      peak). **Ears PASSED 2026-08-05** (organ "beautiful!"; chaos
      "well organized chaos — I like it"); butterfly-in-scope-xy demo
      still worth a look next session. Later: organ foldback/
      leakage/scanner + the organ-feedback example (queued above);
      chaos audio-rate, rate_cv, guarded morph knob.
      Original spec'd entry follows (plan retained for the record):
      spec'd 2026-08-04 at
      Matthew's pick ("organ & chaos please"), second and third off the
      what's-left brainstorm; full specs in docs/MODULE_IDEAS.md.
      `organ` (Sources, S–M): nine-drawbar additive — bars fold into
      the voice axis for one vectorized sine call over (V·9, F)
      (supersaw idiom), 3 dB/step drawbar law, seeded key click,
      single-trigger percussion register (legato doesn't re-fire — THE
      test), Nyquist-masked partials, fader_seq fader-bank panel,
      default 888000000; neutral pin = lone 8′ drawbar ≡ oscillator
      sine. `chaos` (Modulation, S–M): Lorenz/Rössler attractor CV —
      RK4 on a rail-limited substep, decimated control grid + linear
      interp (the slew per-sample-Python lesson), x/y/z coherent CV
      outs + per-system gate (lorenz lobe-sign square, rossler z-spike
      bursts), seeded-deterministic (it isn't random at all),
      exact block-size independence; the demo is x/y → scope xy =
      the butterfly. Examples queued: organ→chorus→reverb with chord
      stabs; chaos→quantizer wandering melody.
      **BUILD PLAN (2026-08-04, Matthew: "knock something off the
      build queue or 2" → plan first, build next session).** Order:
      organ first (smaller payoff loop), chaos second, close-out last.
      *Organ phase*: (1) `modules/organ.py` — Sources; `pitch_cv`/
      `gate` (voice) → `out`; params `bar1..bar9` int 0..8 default
      888000000, `click`, `perc`/`perc_decay`/`perc_level`, `level`.
      (2) `_render_organ` in numpy_backend — per-voice block-mean
      pitch (pluck idiom); partials folded (V·9, F) into ONE sine
      eval; 3 dB/step gain law; constant-RMS norm g_i/√max(1, Σg²)
      (document); Nyquist mask; gate envelope = one-pole ~0.4 ms via
      lfilter (vectorized, click-free); click ticks = seeded per-hit
      bursts with a carry-tail buffer (drums whole-hit engine);
      percussion = module-wide from-silence single-trigger latch +
      per-voice decaying sine at 4′/2⅔′. (3) pyo punt. (4) tests: the
      lone-8′-≡-oscillator-sine pin (VERIFY the oscillator's phase
      convention in `_render_oscillator`/`_osc_waveshape` FIRST, then
      pick bit-exact vs <1e-6), 3 dB law exact, registration FFT,
      Nyquist mask alias-free, click seeded + click=0 declick bound,
      perc single-trigger (legato must NOT re-fire — THE test),
      voice ≡ mono, block independence, 16v×9 perf RECORDED.
      (5) app.py widgets — CHECK fader_seq's panel cost first: reuse
      the fader bank if cheap, else bounded int sliders now + panel
      as follow-up. (6) example `organ_jazz.json` (keys → organ →
      chorus → reverb), MODULES.md entry + index row, `__init__`
      export. *Chaos phase*: (1) `modules/chaos.py` — Modulation;
      `reset` in → `x`/`y`/`z` cv outs + `gate`; params `system`
      lorenz|rossler, `rate` 0.01..50 log, `range`, `bipolar`,
      `seed`. (2) renderer — RK4 at rail-limited substep (lorenz
      dt≤0.01, rossler ≤0.05), control grid every 16 samples keyed to
      ABSOLUTE sample count (any block split exact), np.interp to
      audio rate, carry state + prev/next control points; per-system
      bound normalization (lorenz x±20/y±27/z 0..50; rossler x±12/
      y±11/z 0..23), clip rails; seeded IC jitter + deterministic
      warmup (~1000 substeps); rate calibration T_orbit ≈ 0.76
      (lorenz) / 6.1 (rossler) so `rate` ≈ orbits/sec (pin loosely);
      gate: lorenz sign(x), rossler z > 3 raw (document both);
      non-finite → re-seed + tripwire counter. (3) tests: seed
      determinism bit-exact, reset ≡ fresh, block independence EXACT,
      long-run bounds + zero NaNs, ε-IC divergence horizon (the
      chaos test), rate↔zero-crossing loose pin, gate semantics both
      systems. (4) example `chaos_melody.json` — x → quantizer → osc,
      gate → adsr → vca, z → filter cutoff (self-playing); check
      port names against MODULES.md while wiring. (5) widgets +
      MODULES.md + export + punt. *Close-out*: full suite green,
      tripwires (docs coverage) green, WORKLOG + this entry updated,
      ONE commit for the pair (Session A precedent). Perf numbers in
      the worklog. Ears go to the meatthread0 queue as usual.
- [x] **`sampler` — keyboard-tracked pitched sample voice** — SLICE 1
      SHIPPED 2026-08-22 (`9355129`), **EARS PASSED 2026-08-29**
      ("works well"). Slices 2 and 3 are tracked in § "The sampler"
      above; this entry is the original spec-pick record. Spec'd
      2026-08-04 at Matthew's pick (":-{D") from the "what's left"
      brainstorm; full spec in docs/MODULE_IDEAS.md § New voices. The
      gap FilePlayer doesn't fill: per-voice playheads at
      pitch_cv-tracked rates over one whole-loaded recording (media.py
      decode, convolver off-thread load pattern, resampler `_hermite4`
      read, pluck voice contract — zero new infra). one_shot / gated /
      loop modes, root+tune+fine, start/end region, seam-crossfaded
      loop, declick ramps; neutral = root-pitch playback returns the
      decoded buffer **bit-exact**. M–L → three slices (core →
      loop+region UI → stretch: stereo/mip-antialias/start_cv/reverse).
      Examples queued: euclidean breaks machine; keys → loop mode →
      reverb mellotron (+ `chord` in front = 4-deep stack).
- [x] **`arpeggiator` + `chord` — the voice pair, both directions** —
      BOTH SHIPPED 2026-08-03 (day part seven; Matthew: "fun exercises
      of the voice architecture in opposite directions"). Arpeggiator:
      first poly→mono collapser — slot-keyed arrival-stamped held set
      (order = true as-played), sparse voice-edge event map (no 16×F
      scan), pitch sampled at the rise (pinned), up/down/updown-
      palindrome/order/random(seeded), octave stacks, hold latch with
      press-from-silence restart, measured gate_len (euclidean idiom),
      pitch holds through silence. Chord: mono→poly explorer — fixed
      (4, F) rows (shape-stable live toggles, 4× cheaper than 16),
      preset table + custom slot bank, continuous pitch broadcast,
      strum staggers enabled-row onsets in absolute samples (fall
      cancels unfired), spread = open voicing (0,+12,−12,0). Perf: arp
      worst-case 5.1% budget, chord 4.1% event / 0.7% steady. 48 tests;
      suite **2515**. Example `chord_arp_factory.json` — mono → poly →
      mono full circle, self-playing. **Ears PASSED 2026-08-05**
      ("works well, like a unique or random song"); chord slot-bank
      GUI feel not separately exercised. Swing lives in `clock_divider`
      (shipped 2026-09-11).
- [x] **Chord/arp extras — SHIPPED 2026-09-14** (Matthew's pick off the
      love list): chord `inversion` 0..3 (the n lowest sounding notes up
      an octave, slot identity kept so downstream per-voice state and
      the strum order are untouched; follows spread and skips disabled
      slots), chord `changed` gate out (~2 ms pulse on a root jump of
      ≥ half a semitone sample-to-sample or an interval-set edit, under
      a held gate only — fresh presses, glides and silence don't fire;
      carried across block joins), chord `retrig` (on `changed`, the
      rows drop one sample and re-strum, staggered from the next
      sample), arp internal clock (`bpm`/`division`, used only with
      `clock` unpatched — integer period, half-period high, `reset`
      re-phases it and forces a step by decree because the line may
      already be high there; a cabled clock makes both params inert,
      pinned array_equal). 52 reference arrays captured pre-edit: every
      chord array and every clocked-arp array is array_equal; only the
      unclocked-arp arrays differ, by design (an unclocked arp used to
      be dead; no shipped example had one). 23 tests; suite **3177**.
      Example `chord_legato_inversions.json` (banked for ears): a 24 s
      held gate with roots stepping under it once a second, maj7 in
      first inversion re-strummed 20 ms apart on every step, plus the
      arp on its internal 140 × 4 clock — measured: re-strums at 1.0,
      2.0, 3.0 s on all four rows 20 ms apart, arp period exactly 4725
      samples.
- [x] **Clockwork trio `euclidean` / `burst` / `bernoulli_gate`** — ALL
      SHIPPED 2026-08-03 (8aa49fb, the day's finale). Euclidean:
      arithmetic Bjorklund (tresillo verbatim, pinned), measured-step
      gate_len, accent layer intersected with hits. Burst: whole-burst
      scheduling at the edge (deterministic), 2^spread grid warp,
      (1−decay)^k env, clocked division mode. Bernoulli: whole-gate
      routing (A+B ≡ in exactly, pinned), p_cv at the edge, toggle mode,
      seeded. 25 tests; suite **2466**. Example `clockwork_groove.json`
      (the self-playing groove). **Ears PASSED 2026-08-05 — and how**:
      "something else… the most unique one i've seen so far,
      everything short of a drum machine but just as effective."
      Later: euclidean `fills_cv`; burst `count_cv`;
      bernoulli 3+-way sibling (`sequential_switch` is on the quick-hit
      list); a `clock_divider` to round out the clockwork family.
- [x] **Clockwork love pass** — SHIPPED 2026-09-11 (Matthew's pick after
      modal). **`euclidean.fills_cv`** (+ `fills_cv_depth`, 8 fills/unit)
      read at EACH clock edge, rounded, clamped 0..steps, pattern rebuilt
      at the tick (pinned: 3 + 8x0.25 = E(5,8); a mid-loop CV step
      switches patterns at the tick; clamps both ways). **`burst.count_cv`**
      (+ `count_cv_depth`, 8 gates/unit) read at the trigger edge and
      latched per burst (pinned: latched even when the CV drops after
      the edge; clamped 1..16). Both unpatched = unchanged (pinned
      array_equal). **`clock_divider`** — the 90th module, built to the
      MODULE_IDEAS spec verbatim: `div2`/`div4`/`div8`/`divn`(n)/`mult`(m),
      `swing` on divn (fraction of its period, 0.33 = triplet), `pw` as
      a fraction of each output's period, absolute-sample scheduling
      (burst precedent). Pinned: division counts exact over 1000 edges,
      every output on the downbeat, reset realigns all counters, swing
      timing to the sample, mult midpoints, mult re-tracks a halved
      tempo within one period, first gate mirrors the clock, block-size
      independent. 21 tests; suite **3088**. Example
      `clock_divider_swing.json` (swung hats + breathing backbeat +
      triplet ratchet rim). **Wants ears** — banked. Still on the
      clockwork list: bernoulli 3+-way sibling, `rotate_cv`.
- [x] **`modal` — struck resonator bank** — SHIPPED 2026-08-03 (13ef181,
      same day part five). bar/bell/membrane(Bessel-zeros)/string tables,
      modes 4..24, t60 decay + tilt, brightness, inharm stretch;
      pitch-group-batched lfilter engine — RECORDED: 16 unison × 24
      modes = 8.4% of budget, 16 distinct pitches = 29.6% (the spec's
      asked-for measurement). Drive lesson: b₀ = g·sinθ (strike-peak
      norm), NOT g(1−r) (integral norm — starved long decays to −60 dB).
      17 tests; `modal_bells.json`. **Ears PASSED 2026-08-05**
      (flagged for a longer revisit — Matthew's coming back to it).
      Later: strike-position macro (per-mode gain comb), stereo mode
      spread, `pitch_cv`-tracking excite filter.
- [x] **Modal love pass** — SHIPPED 2026-09-11 (Matthew's pick after the
      drums; he had flagged modal twice for a revisit). All three
      `Later:` items, every one OFF by default and **verified
      bit-identical to renders captured from the pre-change code** (four
      materials + a voiced case): **`position`** — the pluck's
      pick-position comb moved to the mode gains, `|sin(π·ratio·pos)|`,
      renormalized (edge = thin, not quiet; pinned: even harmonics >30 dB
      down at 0.5 on a string, level within 2x across positions, a
      null-everything position falls back to off); **`mallet`** —
      one-pole strike low-pass at `f0·2^(6(1−m))`, per-voice state
      (pinned: the same rolloff at the same harmonic numbers an octave
      apart, within 3 dB — a fixed-Hz filter would not; block-size
      independent); **`spread`** — `out_l`/`out_r` with per-mode
      golden-ratio pans, equal-power ×√2 so spread 0 outs ARE `out`
      (array_equal), L²+R² = 2·mono² per mode (pinned). One bite: the
      one-pole must carry the last OUTPUT and rebuild zi from it each
      block (the octaver's idiom) — carrying lfilter's zf double-applied
      (1−coef) and broke block-size independence. Cost 16v × 24 modes ×
      16 pitches: 31.4% → 39.4% all on. 17 tests; suite **3065**.
      Example `modal_mallets.json` (stereo marimba + xy scope). **Wants
      ears + eyes on the xy scope** — banked with the others.
- [x] **Drum voices `kick_drum` / `snare_drum` / `hat_drum`** — ALL
      SHIPPED 2026-08-03 (12e57ed, same commit — one shared engine:
      whole-hit buffers at the edge, seeded per hit, 2 ms retrigger
      declick fades). Kick analytic pitch-dive (pinned sample-exact) +
      click + tanh drive (no oversampling — LF-dominant, noted); snare
      185/330 modes + banded wires + snappy; hat 6-square metallic
      stack HP 7 kHz with closed-chokes-open. 19 tests; suite **2440**;
      `drum_machine.json` (backbeat groove, 0.89 peak). **Ears PASSED
      2026-08-05** ("drum_machine is a drum machine" — mission
      statement achieved). Later: velocity inputs, kick
      `pitch_cv`, hat `tone` (stack base), per-drum `out` gain_cv.
- [x] **Drums love pass** — SHIPPED 2026-09-11 (Matthew: "Drums please").
      Every drum: **`vel`** (cv), read AT THE TRIGGER EDGE and latched per
      hit, unpatched = 1 (pinned: unpatched renders bit-identical to
      before), negative = silence, a `(V, F)` source collapses to the
      **loudest voice at that sample** (max, not the house sum — a
      velocity bus is 0 on idle slots). Kick: velocity applied BEFORE
      `drive` (soft = clean, hard = saturated; pinned as a ratio), plus
      **`pitch_cv`**, the calibrated 1 V/oct bus like every pitched
      voice, latched per hit (pinned: +1 V ≡ tune +12, and a hit keeps
      its pitch while the CV moves). Hat: **`tone`** 200..1600 Hz stack
      base (400 unchanged; pinned by spectral flatness above the HP —
      centroid was the wrong observable, the band is the band). Per-drum
      `gain_cv` NOT added: `vel` covers accents, a VCA covers the rest.
      18 tests; suite **3046**. Example `drum_dynamics.json`. **Wants
      ears.**
- [x] **`pluck` — Karplus–Strong string voice** — SHIPPED 2026-08-03
      (same day, part four). Extended KS: allpass fractional delay +
      damping-phase compensation (**±5 ct C2..C6**, pinned), `decay` as a
      real pitch-independent t60 (±10%, pinned), `damping` loop LP blend,
      `color`/`position` exciter shaping (seeded per hit → deterministic),
      re-pluck ADDS into the linear loop (click-free superposition,
      pinned). Chunked ≤-loop-length ring advance vectorizes without a
      per-sample fallback; silent voices early-out to exact zeros. Two KS
      gotchas found + fixed/pinned: exciter DC rings as a near-undamped
      pedestal (zero-mean the burst), and a bright pluck's FFT global
      peak is an upper harmonic (measure the fundamental *partial*).
      21 tests; suite **2402**. Example `pluck_strings.json` (keys → 16
      strings → reverb). **Ears PASSED 2026-08-05** ("that one is
      cool" — flagged for a longer play session). Later ideas:
      stereo spread (per-voice pan), `pluck_cv` velocity input scaling
      the burst, a `mute` gate (palm-mute choke), sympathetic-string send.
- [x] **`scope` — oscilloscope pass-through tap** — SHIPPED 2026-08-03
      (same day, part three; Matthew: "lets continue with a scope").
      Bit-exact pass-through (meter precedent) + a 220×110 waveform face
      on the node: 10-division window (`time_div` 1..500 ms/div), spike-
      proof min/max columns, rising/falling/free trigger + level, external
      `trig` override, `cv` jack as fallback trace (no bridge needed),
      mono/dual/xy (goniometer), freeze, gain. Audio thread = ring memcpy
      only; all display maths in new dpg-free `ui/scope_math.py`
      (headless-tested end to end); GUI repaints one zig-zag polyline per
      trace per frame. 25 tests; suite **2380**. Example `scope_tap.json`
      (LFO-swept resonant filter on a saw — watch corners round off).
      **Pending (meatthread0):** real-GUI eyeball (face at 60 fps, trigger
      holds the saw still, dual/xy, freeze, CPU with 2+ scopes). Later
      ideas: `spectrum` sibling (FFT tap, spec in MODULE_IDEAS); ms/div
      readout text on the face; a trigger-level drag-line on the face.
- [x] **`quantizer` + `shift_random` — the generative pair** — SHIPPED
      2026-08-03 (Matthew picked the recommendation off MODULE_IDEAS.md).
      `quantizer` (CV & Utilities): CV → nearest scale note, 1 V/oct; ten
      scales + custom tickboxes (empty → chromatic fallback), root,
      post-quantize transpose, hysteresis (cents) anti-flutter in
      continuous mode, gated sample-on-edge mode, per-voice ~5 ms
      `changed` triggers, voice-aware, primed (no patch-load pulse).
      `shift_random` (Modulation): 16-bit Turing-machine loop — p=0
      locked, p=1 complemented loop (period 2×length, pinned), seed-
      deterministic (live re-roll), byte CV newest-bit-MSB, gate = bit 0,
      `write` forces ones. 43 tests; suite **2354**. Example
      `shift_random_melody.json` (endless melody box). Docs + tripwires
      + pyo punts + bounded UI all landed with it. **Ears PASSED
      2026-08-05** ("another cool one"); the custom-scale tickbox rows
      not separately exercised. Later ideas: note-name display on
      the quantizer node; a `changed`-driven strum helper; scale presets
      beyond the ten.
- [x] **Module polish — slice 1 (audit + docs/exports/widget gaps)** — done
      2026-08-03 (Matthew: "polish all the modules"). Registry-walking audit
      of all 64 types vs MODULES.md / `__all__` / widget dispatch / pyo punts
      / CV conventions. Fixed: `slew` + `warping_buffered_speaker_output`
      MODULES.md entries written (they had none) + `disk_writer` stub
      replaced; `FaderSeq` + both buffered sinks exported in `__all__`;
      `slew`/`key_trigger` added to the pyo punt notice; `slew.shape` and
      `transient_shaper.speed` text boxes → combos (the shaper's was the
      follow-up promised at ship); slew rise/fall + warping
      `brake_time`/`spinup_time` (0..30 s — the eyeball sweet spot is 10 s,
      past the old generic range) + `ratio_depth` got bounded/united drags.
      4 new tripwire tests (`test_docs_coverage.py`) make undocumented /
      unexported modules a test failure. Suite **2310**. **Pending
      (meatthread0):** real-GUI eyeball of the new combos/drags. See
      WORKLOG 2026-08-03.
- [x] **Module polish — slice 2: bounded-widget sweep** — done 2026-08-03,
      same day as slice 1 (Matthew: "lets continue"). Scope grew on
      contact: the audit's literal-mention heuristic had hidden that
      compressor / tape / freq_shifter / convolver had NO TYPE block at
      all, and two widgets were actively misleading — transient_shaper
      `attack`/`sustain` (bipolar ±1 gains stuck on the 0..5 s
      envelope-time / 0..1 sustain widgets: **the cut half was unreachable
      from the UI**) and compressor `attack`/`release` (ms params on the
      seconds branch — "10.000 s" for a 10 ms attack, drag clamped below
      the release default) + `gain` (make-up dB on the 0..2 linear
      slider). New blocks: compressor, transient_shaper, tape,
      freq_shifter, convolver (incl. the long-queued predelay 0..500 ms +
      tone), audio_to_cv, cv_to_frequency; extended: bitcrusher (int
      sliders + jitter/mix), midi_input (bend_range/mod_scale/
      pressure_scale). Audit section C (params on the bare generic drag)
      now EMPTY across all 64 types. Suite 2310. **Pending (meatthread0):**
      real-GUI feel pass over the reshaped nodes (listed in WORKLOG).
      See WORKLOG 2026-08-03 slice 2.
- [x] **Stream-health readout (`xrun` + `api`)** — built 2026-07-20, the
      measure-first slice under the output-skipping thread. PortAudio's
      `status` flags counted instead of printed (`_note_stream_status`),
      host API resolved on `start()` (`_resolve_host_api`), both exposed via
      `stream_health_snapshot()` and shown on the toolbar next to DSP% with a
      `diagnose()` tooltip that reads load and underflows *together*. Also
      removed a `print()` from both audio callbacks (stdout lock + I/O on the
      realtime thread). 35 tests. **Pending (meatthread0):** run it under load
      and report `DSP%` / `xrun` / `api` — that reading decides the next slice.

- [ ] **Off-thread audio rendering (render thread + ring buffer)** — SCOPED
      2026-07-20, gated on the readout above. `_fill_output` currently renders
      the whole graph inside the PortAudio callback, so there is zero slack for
      a scheduling spike. Move `render_block_multi` onto a dedicated thread
      feeding a sample-counted ring; the callback becomes a memcpy.
      `_DeviceOutput` is already exactly this pattern (ring + thin callback +
      underrun/drop telemetry + live on-node readout) — same shape, producer
      and consumer swapped. Queue depth as a toolbar slider beside the buffer
      size; **depth is added latency**, so the two trade against each other and
      async lets you go *smaller* on the device block and spend the budget on
      depth instead, which is the better deal. Companions: raise thread
      priority (`AvSetMmThreadCharacteristicsW("Pro Audio")` +
      `SetThreadPriority` via ctypes — without it the render lands on an
      *unboosted* thread and could regress), and `sys.setswitchinterval(0.001)`
      so per-sample loops can't hold the GIL for 5 ms. Only worth building if
      the readout shows underflows **without** overloads. Moderate/medium-risk:
      touches the duplex mic path (`_input_block` is read synchronously inside
      the callback today) and every `_device_outputs` push.

- [ ] **Host-API / device selection for the main stream** — cheap follow-up.
      The main stream opens with no `device`, `latency` or host-API hint, so
      PortAudio takes the system default (typically MME on Windows). Offer a
      host-API picker (WASAPI, optionally `sd.WasapiSettings(exclusive=True)`)
      and a `latency` hint. Possibly a bigger win than the render rewrite for a
      fraction of the work — the new `api` readout says whether it's worth it.

- [x] **Slew / lag / glide (`slew`)** — built 2026-07-18 (Matthew: "a CV that
      has time-based settings"; mapped the space, he picked the slew limiter).
      New CV & Utilities module `in`→`out`; `shape` toggle
      (`linear` constant-rate reaches target / `exponential` asymmetric
      one-pole eased to ~99%, both arriving in the same wall-clock via
      `_LN100`), independent `rise_time`/`fall_time` (sec), voice-aware,
      primed-to-input, instant at time 0. Per-sample recurrence (envelope
      shape; vectorise-later noted). Killer app = poly portamento
      (`cv_keyboard`→`slew`→osc `freq_cv`), example `slew_portamento.json`.
      16 tests (`test_slew.py`), suite **2270**. **CPU fix 2026-07-18:** an
      LFO into `in` spiked CPU (~20% of a core for one mono slew — per-sample
      numpy dispatch, the ADSR finding again). Fixed: symmetric exp → one
      vectorised `lfilter` (near-free, all voices); linear + asymmetric exp →
      pure-Python scalars. Mono linear 19.8% → 0.5%; 16-voice sym-exp → 0.5%;
      16-voice linear 7.4% (the remaining scan — analytic/JIT candidate).
      The glide re-play was covered by the 2026-08-21 GUI sweep.
- [x] **Slew v2 — SHIPPED 2026-09-14** (the last item on the love list):
      `rise_cv` / `fall_cv` (1 V/oct on that side's rate, ±5 oct,
      block-mean; PER VOICE when `(V, F)` against a `(V, F)` input via one
      1-D `np.mean` per row so a voice row fed a constant is bit-equal to
      the mono render — the first draft used `.mean(axis=1)` and differed
      by an ulp; one law for every row otherwise) and `clock` (gate in:
      while patched the two times are MULTIPLES OF THE CLOCK PERIOD, the
      period being the gap between the last two rising edges on an
      absolute counter carried across blocks; seconds until two edges,
      seconds again when unpatched with the old period forgotten). The
      symmetric-exponential lfilter fast path survives per-voice times as
      one lfilter per row (parity with the scalar recurrence pinned);
      everything else stays the scalar scan with per-row coefficients. 15
      reference arrays captured pre-edit (5 settings × mono steps / mono
      smooth / 4-voice) are array_equal with nothing patched. 16 tests
      (16 → 32 in the file); suite **3195**. Perf: mono linear 0.67% →
      0.90% with CV + clock; 16-voice with per-voice CVs ≈ +1 point over
      the scalar path. Example `slew_clocked_glide.json` (banked): an
      8-step line, exponential glide 0.6 × period, a slow bipolar LFO
      into `rise_cv`; measured period 12027 at 110 BPM and 6014 at 220.
      Test premise corrected on the way: `_reach` helpers measured the
      99.9% point, but an exponential time MEANS the 99% point (the
      rack's own `_LN100` convention); thresholds now 0.99 / 0.01.
      Still later: v3 a `moving`/EOC gate out.
- [x] **Bitcrusher CV (`bits_cv` + `rate_cv`)** — done 2026-07-17 (Matthew:
      "add a cv to the bit crusher"; picked both crush axes, sketched a 1V/bit
      scaler). Two CV inputs on `bitcrusher`, house `<param>_cv` naming
      (normalised his `cv_bits`/`cv_rate`). `bits_cv` additive via
      `bits_cv_depth` (default 1.0 = 1 bit/unit, his 1V/bit); `rate_cv`
      multiplicative `rate_div · 2^(rate_cv_depth · cv)` (default 1.0 =
      1 oct/unit, octave-even like `cutoff_cv`). Block-mean CV folded in before
      the neutral-skip test so CV wakes a transparent crusher; guarded so
      **unpatched = byte-identical** (bit-exact + block-size-independence
      guarantees intact, exact under constant CV). UI: depth knobs labelled
      `bit/unit` / `oct/unit`; ports auto-render; pyo unaffected. **8 CV tests**
      (`TestCV`) + updated model asserts; suite **2238**. Verified end-to-end via
      `render_block` (constant +4 → exactly `rate_div=16`). Docs: MODULES.md +
      `bitcrusher.py`. No eyeball queued (audio path is render-verified).
- [x] **FilePlayer seek / scrub bar** — done 2026-07-17 (Matthew: "give the
      file player some love… a seek bar for the file progress/position"). A
      draggable 0..1 bar under the transport row: thumb tracks the playhead,
      drag/click jumps the track. **Backend:** one hook `seek_file_player(id,
      fraction)` — twin of `rewind_file_player`, stores a frame in `state["seek"]`
      the renderer consumes next block (block-aligned; lands playing *or* paused).
      Fraction is along the known length (`total_frames`, else buffered
      `frames_ready`) so bar and `m:ss / m:ss` readout agree; no-op for unknown
      id / non-player / failed / undecoded. Rewind kept separate (seek-to-0 must
      work before any length is known). **UI:** a `format=""` slider driven by the
      playhead each frame *unless* the user is on it — polled via
      `dpg.is_item_active` (no handler registry to free): idle→drive thumb,
      drag→hands off + flag, release→commit the seek. A quick click that falls
      between two polls still commits via the slider callback's flag
      (`_on_file_seek`); `set_value` doesn't fire DPG callbacks so thumb updates
      don't self-seek. Bookkeeping torn down on delete + patch load. **12 tests**
      (`TestSeek` ×7 through the real renderer + `test_file_player_seek_ui.py` ×5
      headless glue); suite **2230**. Docs: MODULES.md + `fileplayer.py`.
      **Eyeball PASSED 2026-07-17** (Matthew: "works flawlessly") — drag/click
      seek, mid-drag thumb-follow then resume, and paused scrubbing all confirmed.
- [x] **`fm_op` — DX-style FM operator** — done 2026-07-11 (Matthew picked it
      off the module-ideas backlog; "new synthesis territory, small testable
      surface"). New `fm_op` source (Sources): one phase-modulation operator,
      `out = amp_cv · sin(2π·phase + index·pm + feedback·prev)`. Ports
      `pitch_cv` (1 V/oct, C4=0 V, per-sample) · `pm` (audio phase mod, scaled
      by `index` in **radians** — documented) · `amp_cv` (level; unpatched →
      unity) · `index_cv` (× `index_cv_depth`) → `out`. Params: `ratio`
      0.25..16 **snapped** to a harmonic table (UI combo, stored numeric) ·
      `fine` ±50 ct · `index` 0..10 · `index_cv_depth` · `feedback` 0..1 ·
      `fixed` + `freq` (note-independent carrier). **Dual engine** (delay
      precedent): `feedback = 0` vectorizes the block; `feedback > 0` runs a
      per-sample loop — bit-identical at 0. Voice-aware ((V,F) core, V=1 ≡
      mono), block-size independent < 1e-6 (ring_mod phase contract). Analytic
      FM verified: a unit sine into `pm` at 1:1 gives Bessel `J_k(β)` sidebands
      to float32. **Reconciliation:** the spec's port list omitted `index_cv`
      but listed `index_cv_depth`; a depth implies its CV input per conventions,
      and an index envelope is what makes FM evolve, so the input is provided
      (noted in the worklog). 29 tests (`test_fm_op.py`); suite 2081.
      `examples/fm_op_bell.json` (2-op) + `fm_op_epiano.json` (3-op) load +
      render at 0.6 peak. **Eyeball PASSED 2026-07-14** — Matthew ran
      `fm_op_bell.json` in the real GUI; the 2-op bell sings (reads as a
      warning/alarm bell, apt for 3.5:1). Node builds + renders + real audio
      out confirmed; 3-op e-piano + a `ratio`-combo close-look not separately
      A/B'd (same paths). Follow-ups offered, not started: a stereo `out_l`/
      `out_r` detune spread; a two-sample feedback average (DX7 anti-buzz);
      optional per-operator anti-alias.
- [x] **`buffered_specific_speaker_output` — per-sink output buffer size** — done
      2026-07-11 (Matthew's idea: copy the specific speaker, let it carry its own
      buffer size). New sink = `specific_stereo_speaker_output` + a `buffer_size`
      param setting the block size of *its own* secondary stream, independent of
      the global buffer (a flaky monitor can run roomy while the main mix stays
      tight). Reworked `_DeviceOutput` from a block ring to a **sample-counted**
      ring (lock-guarded) so push size ≠ pop size no longer means silence; unified
      the secondary-stream key on `(device, block_size)` so one device carries
      several streams at different sizes; `buffer_size` reconciles live like
      `device`. UI: pan/width/gain/cv_depth sliders + device combo/Refresh + a
      `buffer_size` dropdown. pyo silent-stub. 28 new tests + reworked ring tests;
      suite 2052. Also fixed the stale `SpecificStereoSpeakerOutput` "Slice 1"
      docstring (routing landed as Slice 2 ages ago). **Pending (meatthread0):**
      real-GUI eyeball of the node (combos render + apply) and real audio out of a
      *second* physical device at a custom buffer — neither is headless-testable.
- [x] **`buffered_specific_speaker_output` love: sizes past 1024 + ring readout**
      — done 2026-07-13 (Matthew: "buffer sizes larger than 1024" + "some kind of
      text on it that indicates buffer usage/availability"). (1) `buffer_size`
      dropdown now offers `SINK_BUFFER_SIZES` = global stops + **2048/4096/8192**
      (8192 = the backend's `_MAX_SINK_BLOCK` rail, ≈186 ms; sink-only — the
      global slider stays 64..1024 because the main block sets keyboard-to-ear
      latency). (2) Live on-node readout `buffer 47% (3852/8192)  under 0  drop 2`:
      `_DeviceOutput` grew lock-guarded underrun/drop counters + `telemetry()`,
      the backend a `snapshot_sink_buffers()` GUI hook keyed by module id, the
      app a per-frame `_update_sink_buffers` text tick (grey idle / green ok /
      1.5 s amber flash on a counter tick; FilePlayer-readout lifecycle pattern).
      Underruns arm only once the ring first fills one device block, so a clean
      Start at 8192 doesn't tick; `drop` = any push that lost audio (overwrite
      OR ring-smaller-than-main-block truncation — docs explain the tiny-ring
      case). 33 new/updated tests; suite 2114. Review workflow ran (ring-math +
      gui-lifecycle + consistency reviewers; concurrency reviewer and verifiers
      died to the session token cap — findings self-verified instead, all three
      confirmed and fixed). **Pending (meatthread0):** real-GUI eyeball — the
      extended dropdown applies, and the readout ticks/flashes on a real second
      device (headless can't drive PortAudio streams).
- [x] **Two desktop-rig crashes fixed (audio race + stale meter bar)** — done
      2026-07-11, from six crash logs Matthew sent. (A) GUI: deleting a
      CV-source node left a `(module_id, port) -> bar` entry in
      `_cv_meter_bars`, so the next `_update_cv_meters` frame called
      `set_value` on the freed item ("Item not found") and killed the GUI loop.
      Fix: prune the meter maps in `_on_delete_selected` + wrap the CV meter
      loop in try/except (self-heals) like `_draw_meter_channel` already does.
      (B) Audio: `render_block_multi` iterated the live `patch.modules` dict
      that the GUI thread mutates in place, raising "dictionary changed size
      during iteration" in the callback. Fix: snapshot the module map under the
      lock (`dict(patch.modules)`) and iterate the snapshot in both loops. 6
      tests (deterministic mid-loop delete verified to reproduce the pre-fix
      RuntimeError; + stress + meter-map pruning); suite 2021. **Follow-up not
      taken:** the audio thread still reads individual params/cables lock-free
      (benign — scalar/list reads, not the size-change class); a full
      copy-on-write patch swap would close even that, but it's a bigger refactor
      than the demonstrated bug warrants.
- [x] **New nodes no longer land on a lower node's slider** — done 2026-07-11
      (Matthew's one outstanding bug). Auto-placement cascaded each new node
      only ~60px down — under a node's height — so newcomers stacked on top;
      clicking the newcomer's title bar then clicked *through* to the slider
      underneath. Root cause of the click-through is an imnodes limit (a title
      bar is not an ImGui widget, so an overlapping slider wins the hover and
      imnodes yields the drag) — unfixable from dpg — but the overlap trigger
      is ours. New dpg-free `ui/node_layout.py` `find_free_position` scans for a
      clear slot (honours the preferred spot when free; margin-gap AABB test;
      falls back to preferred on a full canvas). `_create_node_for_module`
      routes the add-path spot through it via `_existing_node_rects` (un-zooms
      each rect); load-from-patch unchanged. 14 tests; suite 2015. **Caveat:**
      *manually* dragging nodes to overlap can still trigger it (imnodes, not
      us). **Pending:** real-window eyeball of the live placement.
- [x] **KeyTrigger — bind one key to a gate/trigger/latch** — done 2026-07-11
      (Matthew's idea: "drop in a single key at a time for a super complex
      setup"). New `key_trigger` source (Sources): one node listens for one
      physical key and emits `out` (gate). `mode` = gate / trigger / latch
      (Matthew: offer the choice, "flexibility is king"); `key` bound via a
      **Learn** button, stored as a portable name.
  - **Raw-key path** — the note keyboards route keys as MIDI notes through a
        home-row keymap, so non-note keys never reached a module. Added a
        parallel `ACCEPTS_RAW_KEYS` dispatch (`_on_raw_key_press/_release` +
        `_KEY_CODE_TO_NAME`) that delivers *any* bindable key by name; modules
        self-filter. Runs alongside the note path (both fire), own debounce
        set so it never tangles with the note/zoom `_held_keys`.
  - **Shortcuts win** (Matthew's call) — reserved keys (Delete/Backspace/…)
        are absent from the bindable map; a bound key defers while Ctrl/Alt is
        held or a text field is focused (typing guard on the two `input_text`
        sites). Bare letters/numbers/punct/space fire only in performance
        context. Overlap with note keys stays allowed (fan-out).
  - **DSP** — `_render_key_trigger`: gate = held; latch = press-parity toggle
        surviving key-up; trigger = fixed ~5 ms pulse carried across blocks
        (block-size independent). Renderer never reads `key` (module self-
        filters), so Learn just sets the model param.
  - Tests: 15 module (`test_key_trigger.py`) + 7 GUI-glue
        (`test_key_trigger_ui.py`, dpg mocked). Suite 2001 pass / 1 skip.
        `examples/key_trigger_latch_brake.json` (latch a key → resampler
        brake) loads + renders. **Pending:** real-window eyeball — the Learn
        button, the code→name map building off real `mvKey_*` constants, and
        the focus/modifier guards are dpg-only. Follow-ups not done: reorder/
        numpad keys; an optional built-in envelope like `cv_gates`.
- [x] **FilePlayer — file list / queue** — done 2026-07-10 (Matthew picked
      "extend FilePlayer" over a standalone node, and "stop when empty" over
      loop/hold). New `playlist` param (ordered list of paths). The node grows
      an **Up next** listbox + **Add to list...** (reuses the WAV picker) +
      **Clear**. When a one-shot track ends, `_advance_file_playlists` (a
      per-frame GUI poll, edge-triggered off the new `NumpyBackend.file_player_finished`)
      pops the head into `path`, plays it from 0:00, and removes it — draining
      to silence. Empty-path + queue kick-starts once running. Engine only ever
      sees an ordinary `path` change; `playlist` round-trips with the patch (list
      mutable-defaults now copied per-instance in `Module.__init__`). Tests:
      +2 in `test_file_player` (default/serialize), +5 `TestFinishedHook`, +4
      headless App-glue in `test_file_player_queue.py` (advance/drain/edge/
      kickstart); suite 1959. **Pending:** real-GUI eyeball (listbox + buttons;
      no headless path builds the node). Follow-ups surfaced, not started:
  - [x] **queue stalls on a bad/missing file** — done 2026-07-11. A queued
        path that fails to decode is now auto-skipped to the next good track.
        The fix is race-proof: rather than a bool `finished`/`failed` edge (a
        fast-failing file flips `failed` between two UI polls and the edge is
        missed → stall), the advancer keys 'advance once per track' on a new
        backend **decode generation** (`NumpyBackend.file_player_decode_gen`,
        bumped once per real decode (re)start at the render rebuild site — zero
        cost on steady-state playback, empty path never ticks). New
        `file_player_failed` hook + `now_ended = finished or failed`. Status
        line says "Skipped unreadable X → Y". Tests: +5 `TestFailedHook`,
        +1 queue skip.
  - [x] **remove a single queued item** — done 2026-07-11. A **Remove** button
        beside Clear drops the *selected* listbox row. Queue rows are now
        numbered (`1. name`) so a selection maps back to an unambiguous index
        even with duplicate basenames; a stale/empty selection is a no-op.
        Tests: +1 queue remove. (Reorder still not done — a further small add
        if wanted.)
  - [x] **next-track button** — done 2026-07-11 (Matthew's ask alongside the
        above; the queue gives it a reason to exist). A **>>|** transport
        button force-advances to the next queued track mid-play (reuses
        `_advance_playlist`); no-op with a status when the queue is empty.
        Tests: +2 queue (advances / empty no-op).
- [x] **Resampler — cubic Hermite read** — done 2026-07-10 (Matthew picked
      the fidelity direction). The ring read was 2-tap linear at all three
      sites (fast path + both declick taps); now 4-tap cubic Hermite
      (Catmull-Rom) via a shared `_hermite4` helper, outer taps clamped to
      the window ends (click-free boundary; never binds in the fast path).
      Bit-exact at integer read positions (frac=0 → the sample), so unity /
      octave / all existing bit-exact tests hold. Interpolator 17–38×
      tighter low-mid, ~3–7× near Nyquist; engine-level the audible win is
      concentrated in the high end (1.5 kHz THD unchanged, 12 kHz downshift
      ~1.5× cleaner). 5 new tests (`TestInterpolation`); suite 55. Open
      follow-ups (offered as further "love", not started):
  - [x] **anti-alias on pitch-up** — done 2026-07-10. New `antialias`
        toggle (default off, so lo-fi character + every existing render
        preserved). On, the input is low-passed at `Fs/(2·ratio)` (8th-order
        Butterworth, sos, 0.85 guard margin) into a *second ring* the up-shift
        wet read samples — band-limiting before the faster read, the only
        place that removes fold-over. Sidesteps the seam-declick core: the
        read just samples a different ring; dry tap + pitch-down + unity keep
        reading raw (bit-exact). Measured: band-limited saw +12 st −13→−25 dB
        alias, a folding 15 kHz tone's alias peak 0.86→0.11. 8 new tests
        (`TestAntialias`); suite 63 (resampler) / 1939 full.
  - [x] **stereo detune spread** — done 2026-07-10. New `spread` param
        (cents, default 0 = mono). Above 0 the module grows a detuned pair
        `out_l`/`out_r` (centre ∓ spread/2 cents) off their own drifting
        read heads → decorrelated stereo width from one mono source; `out`
        stays the centre pitch. Refactored the read into a per-channel
        helper (`_resampler_read_channel` + `ch`-suffixed seam state);
        centre channel bit-identical (all prior tests pass). Return type is
        the bare `out` array at spread 0 (drop-in mono) / `{out,out_l,out_r}`
        above. 9 new tests (`TestStereoSpread`), an example patch; suite 72
        (resampler) / 1948 full. **Resampler "love" arc complete** (cubic →
        anti-alias → stereo spread).
  - [x] **tape-stop / spin gesture** — done 2026-07-11. New `brake` param
        switch + `brake` gate input (ORed), `brake_time` / `spinup_time`
        (default 0.5 s / 0.25 s). Deceleration is linear in *speed*
        (constant-torque platter physics) applied in **ratio space** —
        the reason it's a feature and not a glide trick: glide ramps in
        semitone space, where a dead stop is −∞ st. The brake multiplies
        the playback ratio to an actual 0 (pitch dives, audio freezes;
        the existing low-edge seam machinery absorbs the ring lapping
        the frozen head). Module-wide (voices + spread channels = one
        transport); brake-released renders bit-exact untouched. 11 new
        tests (`TestBrake`; suite 83 resampler / 1970 full), UI
        checkbox + time drags, `examples/resampler_tape_stop.json`
        (clock-gated rhythmic stops). **No open resampler ideas left.**
- [x] **Ctrl+zoom keys debounced** — done 2026-07-10. Ctrl+= / Ctrl+- / Ctrl+0
      were on `add_key_press_handler` and cycled at the OS key-repeat rate when
      held; now `_debounce_key` (shared `_held_keys` gate, cleared on release)
      steps them once per press. `tests/test_zoom_key_debounce.py`; suite 1909.
      Real-window eyeball still worth it (key repeat isn't headless-testable).
- [x] **Scroll-to-adjust params** — done 2026-07-10 (step-sizing + fine/coarse
      modifiers same day). Mouse wheel over a param widget nudges it by its
      *displayed* precision snapped near 1% of range — a notch bumps the last
      digit shown (0.01 on a "%.2f", 100 Hz on a "%.0f" cutoff, 0.1 st on
      semitones). **Shift = ×10 (coarse), Ctrl = ÷10 (fine)**; over a knob
      Ctrl+wheel fine-adjusts, over empty canvas it still zooms. Ints ±1/±10;
      combos cycle; checkboxes toggle. Value math in `ui/param_scroll.py` (36
      tests). **Pending real-window eyeball** — the hover/wheel gesture, the
      Ctrl fine-vs-zoom priority, and whether a bare wheel over a slider also
      scrolls an enclosing panel (shouldn't in the node editor).
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
  - [x] pitch_shifter love pass — SHIPPED 2026-09-14 (Matthew's pick
        off the love list): `feedback` shimmer (previous block's main wet,
        one-pole damped at 6 kHz, soft-ceilinged at the matrix knee, added
        to the engine input; the dry tap and the LPC estimate read the raw
        `db` ring), `harmony` + `harmony_level` (a second `_GrainShifter`
        per voice, built lazily on level > 0 and torn down at 0; hears the
        raw input, not the loop; same `pitch_cv`; shares the whitening,
        re-colours through its own state), `spread` + `out_l`/`out_r`
        (main leans left, harmony right, dry centred; both jacks alias
        `out` when nothing to pan — resampler convention). The
        detect→regrain→process→splice block became `_ps_shift` and the
        re-colour + level valve `_ps_recolor`, both called by main and
        harmony; the renderer returns a dict now. 72 reference arrays
        (6 settings × tone/bass/noise × cv on/off × mono/voice) captured
        pre-edit are array_equal with all three knobs at default. Pinned:
        explicit zeros ≡ absence (mono, voice, formant on/off, every
        jack, and no `eng2` built); shimmer stacks 880/1760 Hz from 220
        that feedback 0 does not (A/B, >12 dB), monotonic per lap; hot
        loop bounded at the clamp; mix 0 with the loop running is
        bit-exact the raw dry; the loop is wired at unison too; harmony
        appears and scales 6.02 dB per halving; lazy build/teardown;
        pitch_cv moves the chord; the harmony does NOT hear the loop (its
        lap-fifth sits ≥60 dB under its own partial — first draft
        measured against the shimmer's noise floor and had to be
        rewritten honestly); spread 0 aliases, 1 hard-pans (>30 dB
        separation, L+R ≈ out), 0.5 fades the far channel 6.02 dB, dry
        identical in L and R; voice ≡ mono with everything on. 18 tests;
        suite **3152** (+2 examples). Cost mono: +7 st 2.4%, shimmer
        3.0%, harmony 4.2%, all + spread 4.7%, all + formant 8.4%. Two
        examples: `pitch_shifter_shimmer.json` (pluck at C3 → +12 fb
        0.75 → hall; laps at 1046/2093/4186 Hz measured +9/+9/+22 dB
        over feedback 0) and `pitch_shifter_harmonizer.json` (E3 saw →
        +4 / +7 spread 1 → L/R; third 69 dB L vs −19 dB R, fifth the
        reverse). **Wants ears** — banked with the rest.
        Not done, by choice: a stereo *detune* spread (the resampler's
        meaning of `spread`) — `spread` here pans the two shifted voices,
        which costs nothing and is the harmonizer's stereo field; a
        block-size-independent loop delay (the freq_shifter runs fixed
        chunks for this; the WSOLA engine's production timing depends on
        F, so it would not be bit-exact anyway — documented as "one block
        plus the engine's latency").
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
- [x] ~~**Ring governor Slice 2**~~ — **shipped 2026-07-16** (197fd53): governed
      push now pitch-preserving via a per-sink [L, R] _GrainShifter pair
      (WSOLA shift by ratio cancelled by the length resample). Engines stay
      in-circuit while cabled (warm-up paid at patch/Start, ~50 ms constant
      latency); unpatched push still bit-identical. 1 kHz-sine FFT test pins
      it. Slice 1 shipped 2026-07-16 (08d0c3e / 264eab6 / f61a80b).
- [x] ~~**Ring governor Slice 3**~~ — **shipped 2026-07-16** (ad57340 / ca8a554):
      `auto_govern` param runs the controller internally (law
      `1 + 0.5*(0.5 - fill)` = the canonical patch exactly), so the sink holds
      its own ring at half with no cables; a patched `ratio_cv` overrides it,
      off stays bit-identical. Governor feature COMPLETE (Slices 1/2/3).
- [x] ~~**Warping buffered sink (`warping_buffered_speaker_output`)**~~ —
      **built 2026-07-18** (Matthew's idea): the tape-warp sibling of the
      buffered sink. Same fill→ratio governor, but an audible VARISPEED
      actuator (the buffered WSOLA path *minus* the pitch-cancellation) so a
      starving ring slows the deck (pitch dives) and refills, and recovery
      spins it back up — an underrun becomes wow-and-flutter, not a click.
      Subclass of the buffered sink + a `_BUFFERED_SPEAKERS` family set; the
      buffered path stays bit-exact. Constant-torque `brake_time`/`spinup_time`
      slew replaces the one-pole (warp only); `auto_govern` defaults on. New
      test file (push pitch BENDS to F0/ratio, vs buffered holding F0), example
      `warping_buffer_tape.json`, suite 2253. See WORKLOG 2026-07-18.
- [x] **Real-GUI eyeball: warping sink** — PASSED 2026-07-18 (Matthew: "these
      settings seem to work well"). 2nd device (HD Audio), `auto_govern` on,
      `buffer_size` 1024, ring ~50% (4063/8192), 0 under / 0 drop. Sweet spot
      was `brake_time` = `spinup_time` = **10.0 s** — a slow, gentle tape drift
      rather than a dramatic dive. Possible later refinement: a continuous
      read-head varispeed if block-rate artifacts show at extreme ratios (the
      per-block version is clean at moderate swings).
- [ ] **Buffered sink: decouple cushion from device blocksize** — buffer_size
      8192 on the HD Audio box fails open() and the sink goes silently
      `buffer: idle` (screenshot-confirmed 2026-07-16). Fall back
      (blocksize=0 or step down) instead of silence, surface the fallback in
      the readout, and let buffer_size size the RING (the cushion) rather
      than the PortAudio callback block.
- [ ] **Real-GUI eyeball: governor patch** (meatthread0) — fill →
      CVOffset(−0.5) → CVScale → ratio_cv against a second device; watch
      fill hold ~50% and find the gain where it starts to warble.
- [x] **Real-GUI eyeball: FilePlayer seek bar** — PASSED 2026-07-17 (Matthew:
      "works flawlessly"). Drag/click seeks, the thumb follows the mouse
      mid-drag then resumes tracking the playhead, and paused scrubbing all
      confirmed in the real window.
