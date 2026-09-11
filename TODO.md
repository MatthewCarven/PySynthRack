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
- [ ] **The selector, meta-possibility version** — a register over WHICH
      module fires: collapse the router itself. Sketch only; earns a spec
      in MODULE_IDEAS.md when the first module has been played.
- [ ] **Example: reroll divider** — slow clock into `reroll` so a latched
      pattern re-decides every four bars; pairs with `clockwork_groove`.

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
- [ ] **Sanction gate-rate feedback (`eoc → trig`)** — FOUND while
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
- [ ] **Follow-ons, if the ears like it** — a per-slope `curve_rise` /
      `curve_fall` pair (Maths has one knob per slope); a `both`-style
      second CV in for `rise` and `fall` separately; an `out_inv` jack;
      `eor`-into-`trig` as a rise-only retrigger once feedback closes.

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
      * Sources: `bowed`/`wind` (M–L, sustained-excitation waveguide
        — completes pluck/modal's physical family).
      * Modulation: `function_generator` — **SHIPPED 2026-08-23**, see
        § "The function generator" below,
        `drift` (S — smooth wandering random; chaos orbits, drift
        stumbles), `cv_math` (S — logic-for-CVs, zero params),
        `cv_recorder` (M — the modulation looper; nothing else
        captures performance).
      * Effects: `rotary` (M — Leslie, the organ's destined partner),
        `vowel` (S–M — formant filter bank, A–E–I–O–U morph),
        `freeze` (M — spectral freeze pad), `autopan` (S — the rack
        still has no dedicated panner).
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
      GUI feel not separately exercised. Later: chord
      `inversion` knob; chord `changed` re-strum trigger; arp internal
      clock; swing lives in the future `clock_divider`.
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
      **Still pending (meatthread0):** re-play the glide now CPU's clear — tune
      rise/fall by ear, A/B the shapes. Later: v2 `rise_cv`/`fall_cv` +
      clock-sync; v3 a `moving`/EOC gate out. See WORKLOG 2026-07-18.
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
