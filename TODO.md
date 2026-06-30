# PySynthRack — Roadmap

Living list of what's next. Edit freely.

## v0.1 — Hello, sine wave (target: Friday)

- [x] Project scaffold (folders, requirements, pyproject, README)
- [x] Core model: Port, Module, Patch
- [x] AudioBackend interface
- [x] PyoBackend implementation
- [x] NumpyBackend implementation (fallback)
- [x] Oscillator module (sine / saw / square / triangle)
- [x] SpeakerOutput module
- [x] DearPyGui app with node editor + transport
- [x] JSON save / load
- [x] `examples/hello_sine.json` ships as the default open patch
- [x] Headless unit tests for core model and patch I/O (24 tests pass)
- [x] Verify on Windows: install, run, hear tone (CLI mode, 2026-05-12) ✅
- [x] GUI install (uv venv on Python 3.12 + `uv pip install -e ".[gui]"`, 2026-05-12) ✅

**v0.1 complete.**

## v0.2 — Sound design basics

- [x] Keyboard module (computer keys → polyphonic notes, octave selector, waveform, volume) — 2026-05-12
- [x] Filter module (RBJ biquad: LP / HP / BP, cutoff, resonance) — 2026-05-12
- [x] Delete key removes selected cables/nodes — 2026-05-12
- [x] Gate signal type wired through model + numpy backend — 2026-05-13
- [x] ADSR envelope module (gate in, CV out, A/D/S/R params) — 2026-05-13
- [x] VCA module (audio × CV multiplier so ADSR is actually audible) — 2026-05-13
- [x] LFO module (sine / tri / square / saw / random; rate / depth / bipolar) — 2026-05-13
- [x] Node positions persist in patch JSON (UI metadata block) — 2026-05-13
- [x] Silent-exit on second Open fixed (orphan link cleanup in node editor) — 2026-05-13
- [x] Mixer module (4 inputs, per-channel gain, master out) — 2026-05-13

**v0.2 complete.**

## v0.3 — Routing & polish

- [x] CV-modulatable params: `freq_cv` + `amp_cv` on Oscillator, `cutoff_cv` on Filter (1V/oct) — 2026-05-13
- [x] Splitter — **not built (architecturally redundant)**. The Patch model already allows
      multiple cables from a single output port; the numpy backend's port-keyed buffer cache
      means any number of consumers reading `(src_id, src_port)` share the same array.
      Just drag multiple cables from one output. — verified 2026-05-14
- [x] Combiner (4 audio in → 1 audio out, plain sum, no per-channel gain) — 2026-05-14
- [x] CVCombiner (4 CV in → 1 CV out, sum or average mode — lets LFO + ADSR both modulate the same param) — 2026-05-14
- [x] Linkwitz-Riley crossover (LR4 = two cascaded biquads per branch; split at chosen Hz into low + high outputs) — 2026-05-14
- [x] WAV disk-writer (records the master bus to a `.wav` while transport is running, queue-based to keep audio callback non-blocking) — 2026-05-14
- [x] LFO.rate_cv (CV input on LFO rate — modulation matrix territory: one LFO modulates another's rate) — 2026-05-14

**v0.3 complete.**

## v0.4 — Performance & polyphony

- [x] MIDI input (mido + python-rtmidi) — self-polyphonic mirror of Keyboard, mono `out` + global `gate`. Plugs into any existing patch. — 2026-05-14
- [x] Voice routing manager — **Option A: voice-aware signals**, fixed 16 slots zero-padded, mono fast path preserved. Committed 2026-05-15, all slices shipped 2026-05-23.
      Slice 1 shipped 2026-05-20: `VoiceSlots` allocator in `core/voicing.py`, MIDIInput backed by it, sustain pedal (CC 64).
      Slice 2 shipped 2026-05-20: polyphonic `_render_midi_input` emits `(16, frames)` for out/gate/pitch_cv, speaker drain sums the voice axis, `_input_buffer` auto-collapses 2D → 1D for un-migrated consumers. Chord through MIDIInput → Speaker is audibly polyphonic.
      Slice 3a shipped 2026-05-20: ADSR + VCA now voice-aware. Canonical MIDIInput → ADSR → VCA → Speaker chain produces per-voice envelopes — releasing one note in a chord decays only that voice.
      Slice 3b.1 shipped 2026-05-23: Filter + Oscillator voice-aware. Filter runs V parallel biquads with per-slot (x1,x2,y1,y2) memory; cutoff_cv accepts (V,F) for per-voice cutoffs or (F,) for macro sweep. Oscillator runs V independent phase accumulators when freq_cv is (V,F); a mono freq_cv with a (V,F) amp_cv produces a cheap-poly per-voice-amp-shaped carrier via broadcasting. 11 new tests in TestFilterVoiceAware + TestOscillatorVoiceAware; 271 tests pass.
      Slice 3b.2 shipped 2026-05-23: LFO + Crossover voice-aware. LFO branches on rate_cv ndim — 2D rate_cv → V independent phase accumulators clocked at per-voice block-mean rate, per-voice S&H state for `random` waveform; mono fast path unchanged. Crossover branches on audio ndim — 2D in → V parallel cascaded biquads per branch with per-slot memory, outputs {"low": (V,F), "high": (V,F)}. 12 new tests in TestLFOVoiceAware + TestCrossoverVoiceAware; 283 tests pass. Slice 3b complete — all 6 stateful DSP modules voice-aware.
      Slice 4 shipped 2026-05-23: Keyboard migrated to MIDIInput's self-polyphonic shape. `voices: VoiceSlots` replaces the flat active_notes set; renderer emits per-slot (16, frames) on out and gate. Public API stays narrow — `note_on(midi_note)` (unit velocity, no second arg), `note_off`, `all_notes_off`, `snapshot_active_notes` still returns `set[int]` for the UI; new `snapshot_voice_slots` is the renderer hook. 8 new tests (3 TestKeyboardVoiceAware + 4 TestKeyboardPolyphonicChain + 1 snapshot_voice_slots model test); 290 tests pass. **Voice routing complete** — both note sources publish identical (V, F) per-voice signals.
- [x] MIDI follow-up: pitch bend → `pitch_cv` output port + internal voice bend, GM-standard `bend_range=2.0` (shipped 2026-05-15)
- [x] MIDI follow-up: mod wheel (CC 1) → `mod_cv` output port + `mod_scale` param (shipped 2026-05-15)
- [x] MIDI follow-up: channel aftertouch → `pressure_cv` output port + `pressure_scale` param (shipped 2026-05-15)
- [x] Error handler integrated at GUI outermost catch + audio callback panic path; crashes written to `~/.pysynthrack/crashes/` (shipped 2026-05-15)
- [x] Single-file Windows `.exe` build (PyInstaller) -- `build.ps1` + `pysynthrack.spec`, examples bundled read-only, console variant `build_cli.ps1` for debugging (shipped 2026-05-17)
- [x] MIDI follow-up: sustain pedal (CC 64) — shipped 2026-05-20 with voice routing slice 1. Per-slot `sustained` flag on `VoiceSlots`; pedal-down causes note_off to mark the slot sustained instead of releasing; pedal-up drops every sustained voice in one pass.
- [x] LeftSpeakerOut + RightSpeakerOut modules — shipped 2026-06-06. `left_speaker_output` /
      `right_speaker_output` sink types (mono `in`, `gain`), numpy drain generalised via a
      `_SPEAKER_CHANNELS` (left, right) mask table; voice-aware sources still collapse via the
      implicit-sum-at-mono-sinks rule, per pinned channel. Pyo backend's speaker finalize routes
      them with `.out(chnl=0/1)` so its v0.1 surface stays coherent. UI picks both up automatically
      from the registry. Example: `examples/stereo_hard_pan.json` (two `saw_blep` detuned 220.0 vs
      221.5 hard-panned L/R — binaural-beat shimmer on headphones). 11 new tests in
      `tests/test_speaker_outputs.py`; full suite 371 passing.
- [x] PolyBLEP / wavetable anti-aliased osc shapes — **shipped 2026-06-04**, offered
      *alongside* the naive shapes rather than replacing them (naive kept for cheap
      lo-fi character). Each audio shape now has three forms selected by the `waveform`
      string suffix: naive (`saw` / `square` / `triangle`), PolyBLEP/PolyBLAMP (`saw_blep`
      / `square_blep` / `triangle_blep`), band-limited wavetable (`saw_wt` / `square_wt`
      / `triangle_wt`); `sine` stays naive-only (already band-limited). Centralised in
      `_osc_waveshape(phases, waveform, dt)` so the Oscillator, CVToFrequency *and* the
      Keyboard/MIDIInput note sources all share one implementation. FFT tests confirm
      ~30-40x (PolyBLEP) and up to ~800x (wavetable, high fundamental) alias-energy
      reduction for saw/square; 20 new tests in `tests/test_antialiasing.py`, full suite 360.
- [x] ~~CPU profile: pyo backend wired for the same modules~~ — **deferred off v0.4,
      2026-06-06** (decision with Matthew). A pyo backend is a re-implementation of every
      module's semantics against pyo's object graph, not a port — and it only pays off at
      near-total module coverage since one backend runs the whole patch. Moved to wishlist
      below as profile-first. v0.4 closes with the speaker pair.

## Later / wishlist

- [x] CPU profile numpy with real patches — **closed 2026-06-07, numpy keeps up.**
      Native after ADSR vectorization: 29–33% mean, worst 42%, 0/2000 misses (was
      97–102% mean, 1999/2000 misses). **Pyo backend parked indefinitely — no
      performance case.** Remains a feature question only. History:
      `tools/profile_numpy.py` shipped (4 scenarios, worst-block verdict). Sandbox first
      read: mean 77-81% of budget with worst-block deadline misses — but cProfile shows
      `_render_adsr_voice` (63%) + `_render_filter_voice` (22%) own the block via
      per-sample Python loops, so **vectorizing two functions is the cheap intermediate
      before any pyo work**. Awaiting Matthew's native Windows run for the real numbers.
      **Native verdict 2026-06-07: 97–102% mean CPU, saw_blep missed 1999/2000
      deadlines** → ladder step 2 engaged. ADSR vectorized same day (run-splitting at
      gate edges, analytic stage chains; 117x on the function, 3.2x overall in sandbox —
      21–26% mean, worst 30%, zero misses). Suite 371 green, mono path untouched.
      Model/JSON/UI stay engine-agnostic; pyo stubs stay.
- [ ] **Filter vectorization** (optional — only if patches grow past current
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
  - [ ] **Slice 5 ← NEXT** (optional, separable) — crossover: same cascaded-biquad shape, sosfilt
        fits. Own slice, own tests; droppable without affecting the filter work.
  - [ ] Slice 6 — re-profile on native Windows for the real numbers; update
        WORKLOG/TODO; decide whether filter vectorization can be marked done.

- [x] `AudioToCV` envelope follower — shipped 2026-05-23. Rectifies the input + asymmetric one-pole (attack_ms / release_ms / gain) smoother; voice-aware shape-polymorphic on the audio input's ndim. Bridges the audio→cv signal-kind wall so the filter's own output can drive `cutoff_cv` (self-wah), a kick can sidechain a pad's VCA, etc. Example: `examples/envelope_follower_wah.json`. 14 new tests; full suite 304 passing (one pre-existing test_adsr failure noted below, untouched).
- [x] `CVToAudio` — shipped 2026-05-23. Signal-kind passthrough (no DSP, just a type-tag relabel) with one `gain` param. Voice-aware by shape preservation. Unlocks audio-rate LFO as a primary tone source with built-in FM via `rate_cv`, percussive clicks from fast envelopes, and CV-oscilloscope-via-WAV recording. Example: `examples/lfo_oscillator.json` (220 Hz carrier LFO with 5.5 Hz vibrato modulator into the carrier's `rate_cv`). 13 new tests; full suite 317 passing.
- [x] Signal-kind bridge modules — complete. `Schmitt` shipped 2026-06-07 (cv→gate,
      two-threshold hysteresis, vectorized event forward-fill, voice-aware; example:
      `examples/schmitt_lfo_clock.json` self-playing LFO-clocked pluck; 20 tests, suite 403).
      Bridge trio done: AudioToCV, CVToAudio, Schmitt — every signal-kind wall has a door.
- [ ] `_render_audio_to_cv_voice` still has a per-sample Python loop (asymmetric one-pole,
      genuinely recursive — not run-splittable like the ADSR was). Cold path today; revisit
      only if follower-heavy patches profile hot.
- [x] `CVToFrequency` phase 1 — shipped 2026-05-23. Self-contained CV-controlled oscillator. Three-point CV→Hz mapping (`f0` at CV=0, `fm` at CV=0.5, `f1` at CV=1.0) with `mode` param (`"log"` default for musical octaves — equal-octave splits, `"linear"` for bent sweeps — equal-Hz splits), `waveform` (sine/triangle/square/saw), and a `freq` fallback for unpatched CV. Bipolar CV is clamped to [0, 1] (phase 2 adds the negative-side mapping). Voice-aware via shape-polymorphism on the CV input — (V, F) CV → V independent phase accumulators. Example: `examples/cvtofreq_blip.json` (synthesized-kick pitch envelope). 22 new tests; full suite 339 passing.
- [x] `CVToFrequency` phase 2 — shipped 2026-06-07. `negative_enabled` (default False, phase-1 clamp preserved), `f0_neg`/`fm_neg`/`f1_neg`, independent `mode_neg`; CV=0 belongs to the positive side, crossing continuity is the user's choice, beyond ±1 clamps. Drive-by: fixed the UI mode combo offering filter items on this node since phase 1. Example: `examples/cvtofreq_bipolar_pendulum.json` (log upswing, linear downswing). 12 new tests; suite 383.
- [x] Drive-by: `tests/test_adsr.py::test_no_nan_with_zero_durations` undefined `sr` — fixed 2026-06-04 (inlined `sample_rate=44100`). Suite now fully green.
- [x] **CV source meters** — shipped 2026-06-12. Each cv-kind *output* port draws a
      0..1 progress bar under its node jack, auto-ranged per source (instant-attack /
      slow-release window, constant sources park mid-scale) with the live value as the
      bar overlay. Backend captures one block-mean scalar per cv output into
      `_meter_levels` (atomic dict swap, no lock) exposed via `snapshot_meter_levels`;
      UI swapped `start_dearpygui` for a manual render loop that ticks the bars each
      frame. 7 headless tests in `tests/test_cv_meters.py`; suite 426. Possible
      follow-ups: a fixed-range toggle for sources you know are 0..1, or input-side
      meters too.
- [x] **FilePlayer (WAV source)** — shipped 2026-06-28. `file_player`:
      streams a WAV into the patch as a stereo (`left`/`right`) source so a
      recorded track can be crossover-split and used as a modulation source
      (Matthew's `track → crossover → low/high → AudioToCV → Osc/CVToFreq`
      patch). WAV-only (scipy.io.wavfile, no new deps), one-shot default +
      `loop` toggle, `gain`/`armed`, resamples to the engine rate on load.
      Decode is lazy into backend state; one-shot zero-pads + parks, loop
      wraps with modular indexing, `stop()` rewinds. No UI/pyo changes
      (generic param widgets + auto output jacks; pyo silent-stub). 13 tests
      in `tests/test_file_player.py`; suite 439. Example:
      `examples/file_crossover_split.json`. Follow-ups: a retrigger/`gate`
      input for one-shot replays, 24-bit WAV support (scipy can't), a
      background loader to avoid the first-block decode hiccup.
- [x] **MicInput (live capture source)** — shipped 2026-06-28. `mic_input`:
      live microphone (or any input device) as a stereo (`left`/`right`)
      audio source, so a voice can be crossover-split and used as a
      modulation source (beatbox: low→AudioToCV→sub amp, high→AudioToCV→
      CVToFrequency). Stereo out, selectable device dropdown. Backend opens
      a full-duplex `sd.Stream` only when a mic module is present (else
      output-only), with graceful fallback; `_duplex_callback` stashes the
      input block, `_render_mic_input` maps it to L/R. 15 tests in
      `tests/test_mic_input.py`; suite 458. Example:
      `examples/mic_beatbox_crossover.json`. Follow-ups: input level meter,
      a refresh-devices button (shared with MIDIInput’s same need),
      mono-out variant if the stereo dupling is ever unwanted.
- [x] **CV utility trio (Constant / CVScale / CVOffset)** — shipped 2026-06-30.
      Three small, composable CV utilities. `constant` (no inputs → fixed `value`
      on a `cv` output; default 1.0): a patchable DC level — manual knob, tuned
      drone via `cv_to_frequency`, fixed VCA gain. `cv_scale` (`in` × `scale` →
      `out`): the attenuverter — attenuate (<1), amplify (>1), invert (<0).
      `cv_offset` (`in` + `offset` → `out`): slides a signal's centre; unpatched
      it's a constant `offset` (DC source). Scale+offset compose into a full
      affine map, kept as two orthogonal modules in the modular spirit.
      CVScale/CVOffset are shape-polymorphic for free (pure pointwise ops, no
      per-voice state): mono stays mono, `(V, F)` stays `(V, F)`. No new deps;
      generic param widgets (soft ±10 drag for value/scale/offset) + auto CV
      meters on the `out` jacks; pyo silent-stub. 26 tests in
      `tests/test_cv_utilities.py`; suite 484 (+18 mido). Example:
      `examples/cv_utility_demo.json` (LFO→scale→offset rhythmic cutoff sweep +
      Constant→CVToFrequency drone). Follow-ups: CV-modulatable amounts
      (`scale_cv` / `offset_cv`) if a mod-matrix ever wants them; a combined
      affine node if the two-module chain proves common.
- [x] **Sample-and-hold (`sample_hold`)** — shipped 2026-06-30. Samples `in` (cv)
      on each rising edge of `trig` (gate) and holds it until the next — the
      classic staircase. No params (pure stepped S&H; `slew`/glide is an easy
      follow-up). Unpatched `in` samples 0 (no internal noise — that's the
      Noise generator's job); unpatched `trig` holds the last value. Trigger is
      a `gate` input so Schmitt / Keyboard / MIDI / ADSR gates all clock it.
      Vectorized rising-edge forward-fill (same trick as Schmitt); shape-
      polymorphic — mono `(F,)` or per-voice `(V, F)` with per-voice held value
      + held-gate carried across blocks, a mono partner broadcasting across the
      voice axis. No new deps; no UI change (param-less, auto CV meter on `out`);
      pyo silent-stub. 24 tests in `tests/test_sample_hold.py`; suite 508
      (+18 mido). Example `examples/sample_hold_arp.json` (random LFO → S&H
      clocked by an LFO→Schmitt → CVScale→CVOffset → CVToFrequency: a self-
      playing stepped arp). Follow-ups: `slew` param; track-and-hold mode (hold
      while gate high); pairs naturally with the Noise generator when it lands.
- [x] **Noise generator (`noise`)** — shipped 2026-06-30. White or pink noise
      source with no inputs and *two* output jacks carrying the same stream:
      `out` (audio, into filters/speaker for drums/wind) and `cv` (into
      Sample-and-Hold / modulation) — Matthew picked dual jacks over a single
      kind so neither use needs a bridge. `color` param (white/pink) + `amp`.
      White is uniform ±1; pink filters white through a 3rd-order pinking IIR
      via `scipy.signal.lfilter` (zi carried across blocks, −3 dB/oct measured),
      RMS-normalised to white so `amp` means the same level for both. Mono
      source (like Constant). No new deps; UI gets a `color` combo; pyo silent-
      stub. 26 tests in `tests/test_noise.py`; suite 534 (+18 mido). Example
      `examples/noise_hat.json` (white → HP filter → VCA, ADSR clocked by
      LFO→Schmitt: a self-playing hi-hat). Follow-ups: S&H could normal its
      `in` to a noise source now that one exists; a `seed` param for
      reproducible patches; brown/blue colors.
- [x] **Parametric EQ (`parametric_eq`)** — shipped 2026-06-30. Four peaking (bell) bands on one mono signal, each with adjustable centre freq (full 20 Hz–20 kHz; defaults 25/50/100/250 Hz), gain (dB, 0 = transparent), and Q. RBJ peaking biquads cascaded; coefficient-independent DF-I state like the Filter module; shape-polymorphic (mono + voice). Started as a 64-band log/linear graphic EQ idea, scoped down to parametric — no array param, no custom slider bank. 27 tests in `tests/test_parametric_eq.py`; suite 561 (+18 mido). Example `examples/parametric_eq_bass.json`. Follow-ups: per-band freq/gain CV for an *animated* EQ (same gap as Crossover); shelf band types (low/high shelf on the ends) if a tilt control is wanted; an optional band-count selector now that the band list is data-driven.
- [x] **FilePlayer ffmpeg decode (mp3/flac/ogg + video audio)** — shipped 2026-06-30. The FilePlayer reads anything ffmpeg can decode, including the audio track of video files (mp4/mkv/mov/webm), while WAV stays on the zero-dep scipy path. New `audio/media.py` (`find_ffmpeg`: bundled imageio-ffmpeg → system PATH, cached; `decode_with_ffmpeg`: subprocess f32le pipe → (2,N)); backend `_decode_audio` tries WAV then ffmpeg; Browse dialog filter widened; optional `[media]` extra + guarded spec collection so the binary ships in the exe. 16 tests in `tests/test_media.py` (6 skip without ffmpeg); suite 577 (+18 mido) with ffmpeg present. Follow-ups: streaming/chunked decode for long files (today it loads the whole file into memory); decode off the audio thread so first play never stalls; node hint showing whether ffmpeg was found.
- [x] **Meter module (`meter`)** — shipped 2026-06-30. A level indicator you patch any audio signal into: `in` passes through to `out` untouched, and the node shows the recent peak in dBFS on a fixed −90..0 bar (fixed so two meters are directly comparable — e.g. mic vs file player). Matthew chose a dedicated module over auto-meters on every jack, and dB floored at −90. Peak envelope (instant attack / `_METER_DECAY=0.985` slow fall) computed on the audio thread for block-rate latency; `snapshot_audio_levels()` feeds the UI, keys pre-created in compile() so the snapshot needs no lock. 18 tests in `tests/test_meter.py` (+ headless UI check); suite 595 (+18 mido). Example `examples/meter_levels.json`. Follow-ups: peak-hold tick; switchable RMS mode; stereo/2-channel meter; clip indicator at 0 dBFS.
- [x] **AD envelope (`ad_envelope`)** — shipped 2026-06-30. Trigger-style Attack/Decay percussion envelope: `trig` (gate) rising edge → A→D contour that runs to completion regardless of trigger length (no sustain), `cv` out. Params attack/decay. Retrigger picks up from the current level (no click); voice path bit-identical to mono. Pairs with the noise drums / LFO→Schmitt clock. 20 tests in `tests/test_ad_envelope.py`; suite 615 (+18 mido). Example `examples/ad_kick.json` (clocked sine kick). Follow-ups: curve/shape param (exp vs linear); pitch-envelope kicks via cv_to_frequency; velocity scaling; run-based voice vectorization if profiled hot.
- [x] **Resampler (`resampler`)** — shipped 2026-06-30. Varispeed pitch shifter (tape/turntable: pitch + speed coupled), Matthew's dream module. Semitone slider (±24) + `cents` fine-tune + `pitch_cv` (summed in *semitone* space, scaled by `cv_depth`, default 12 = one octave/unit) + optional `glide` (one-pole portamento, default 0 = instant). **Linear** interpolation; **looping ring buffer** so it runs forever on a live stream — the read head wraps inside a ~0.2 s window (faint granular-repeat texture on extreme shifts; ~90 ms latency, intrinsic to varispeed on a continuous signal). Shape-polymorphic (mono + per-voice buffers); a single voice row is **bit-identical** to mono. Unity (0 st, no glide) is a bit-exact delayed passthrough. 22 tests `tests/test_resampler.py`; suite **637** sandbox (+18 mido), +22 from 615. Example `examples/resampler_tape_wobble.json` (saw → varispeed with an LFO wobbling the pitch). Follow-ups: declick crossfade at the loop seam; a **pitch-only** sibling (granular / phase-vocoder) that keeps speed fixed; window-size / low-latency param; dry/wet mix; anti-alias LP before big up-shifts; `pitch_cv` could normal to a constant.
- [x] **PitchShifter (`pitch_shifter`)** — shipped 2026-06-30. The time-preserving cousin of the resampler (shift pitch, keep speed). Engine chosen in-convo after the plain overlap-add prototype combed tonal material: **WSOLA** (waveform-similarity overlap-add) — grains nudged to the best-correlating position so overlap joins stay phase-continuous → clean on the tonal waveforms this synth makes. Streaming two-stage (WSOLA time-stretch by r into a ring, then resample by r to restore duration), per-voice `_GrainShifter` engines (voice row bit-identical to mono). Params: `semitones`(±24) + `cents` + `pitch_cv` (block-rate, `cv_depth` semitones/unit), `mix` (dry/wet → detune/harmony), `grain_size` (ms) and `overlap` (2–4) expose the grain engine. ~one-grain latency. 24 tests `tests/test_pitch_shifter.py`; suite **661** sandbox (+18 mido), +24 from 637. Example `examples/pitch_shifter_harmony.json` (saw → +7 st @ 50% mix = a fifth). Delivered as `pitch_shifter.patch` STACKED on `resampler.patch`. Follow-ups: formant-preserve option; pitch-synchronous grain sizing for deep bass; vectorize the per-voice search if profiled hot; transient detection to sharpen attacks; tighten octave accuracy (~12 cents sharp at +12 on pure sine).
- [x] **CV Keyboard (`cv_keyboard`)** — shipped 2026-07-01. The controller sibling of `keyboard`: the computer keys emit **CV + gate only** (no internal oscillator), so the voice is built downstream — same keys, a different sound each patch, like a hardware modular keyboard. Matthew picked **both** output styles, **voice-aware**, as a **new module**. Outputs: per-voice `pitch_cv` (**1V/oct, C4 = 0 V**) for an external `oscillator.freq_cv` / `cv_to_frequency`; per-voice `gate`; and **twelve per-pitch-class gate jacks** `key_c`…`key_b` (octave-folded — C4 and C5 both raise `key_c`) so a different module fires per key ("all the keys are CV outs"). Shares Keyboard's VoiceSlots note-ingest (copied, not inherited) and a new `ACCEPTS_COMPUTER_KEYS` marker the UI routes physical keys by (replaces the `isinstance(Keyboard)` checks, so both keyboards play together). pitch_cv holds through the release tail so an ADSR release stays in tune; idle voice slots sit at 0 V (C4) and must be silenced by the gate/VCA, exactly like a real oscillator drone. No new deps; UI shows only an `octave` selector + auto CV meter on `pitch_cv`; pyo silent-stub. 20 tests in `tests/test_cv_keyboard.py`; suite **681** sandbox (+18 mido), +20 from 661. Example `examples/cv_keyboard_external_voice.json` (pitch_cv → saw osc → ADSR/VCA, plus `key_c` → a noise snare). Follow-ups: per-key **pitch_cv-per-voice** is done, but a velocity/aftertouch CV is not (computer keys can't express it — that's the MIDIInput lane); optional absolute per-key gates (17 home-row keys) instead of 12 pitch classes; a `cv_reference` param to move 0 V off C4; mono/last-note mode for vintage single-osc leads.
- [ ] Stereo-aware speaker module (pan / width)
- [ ] Patch presets palette (factory + user banks)
- [ ] Undo / redo on patch edits
- [ ] Per-key velocity calibration on MIDIInput — `velocity_curve: dict[int, float]` mapping MIDI note → velocity multiplier, applied after the 0-127 normalisation. Niche but exactly the kind of fix that's only possible because the synth lives in code: budget keybeds drift key-by-key due to manufacturing variance, and a "play every key at the same intended force, capture the offsets" calibration flow papers over it perfectly. Could ship as a small "Calibrate keys" dialog on the MIDIInput node.
- [ ] Refresh-devices button on the MIDIInput node — today the device combo snapshots `available_devices()` at widget creation; installing `[midi]` after the app is open leaves the dropdown stale until the patch is reopened.
- [ ] App icon for the packaged `.exe` -- add a `.ico` and reference it from `pysynthrack.spec` (EXE(icon=...))
- [ ] Code-signed build -- removes the SmartScreen "unrecognized publisher" prompt; only worth it if the synth ever leaves the hobby circle
