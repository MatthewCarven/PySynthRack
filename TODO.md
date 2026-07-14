# PySynthRack — Roadmap

Living list of what's next. Edit freely.

> Compacted 2026-07-03: the completed history — all of v0.1–v0.4 plus every
> shipped Later/wishlist and CV-coverage entry — moved **verbatim** to
> [TODO-ARCHIVE.md](TODO-ARCHIVE.md). Archived entries keep their follow-up
> notes; grep the archive before assuming an idea is new.

## Later / wishlist

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
