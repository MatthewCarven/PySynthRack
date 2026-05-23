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
- [ ] LeftSpeakerOut module — mono audio in routed exclusively to the left channel of the output device. Backend drain mixes any LeftSpeakerOut sinks into the left bus only; right bus stays silent for that node. Patches can place a Left + Right pair to get hard-panned stereo without a stereo Speaker module.
- [ ] RightSpeakerOut module — mirror of LeftSpeakerOut; audio in routed exclusively to the right channel. Compose with LeftSpeakerOut for stereo patches.
- [ ] PolyBLEP or wavetable anti-aliased osc shapes (replace naive saw/square)
- [ ] CPU profile: pyo backend wired for the same modules so it's a drop-in fast path

## Later / wishlist

- [x] `AudioToCV` envelope follower — shipped 2026-05-23. Rectifies the input + asymmetric one-pole (attack_ms / release_ms / gain) smoother; voice-aware shape-polymorphic on the audio input's ndim. Bridges the audio→cv signal-kind wall so the filter's own output can drive `cutoff_cv` (self-wah), a kick can sidechain a pad's VCA, etc. Example: `examples/envelope_follower_wah.json`. 14 new tests; full suite 304 passing (one pre-existing test_adsr failure noted below, untouched).
- [x] `CVToAudio` — shipped 2026-05-23. Signal-kind passthrough (no DSP, just a type-tag relabel) with one `gain` param. Voice-aware by shape preservation. Unlocks audio-rate LFO as a primary tone source with built-in FM via `rate_cv`, percussive clicks from fast envelopes, and CV-oscilloscope-via-WAV recording. Example: `examples/lfo_oscillator.json` (220 Hz carrier LFO with 5.5 Hz vibrato modulator into the carrier's `rate_cv`). 13 new tests; full suite 317 passing.
- [ ] Signal-kind bridge modules — remaining: `Schmitt` (CV threshold crossing emits a gate edge for chaining envelopes off a CV signal). The last of the three originally-proposed bridges; lower urgency than the audio↔CV pair but cheap to add when chained-envelope patches need it.
- [x] `CVToFrequency` phase 1 — shipped 2026-05-23. Self-contained CV-controlled oscillator. Three-point CV→Hz mapping (`f0` at CV=0, `fm` at CV=0.5, `f1` at CV=1.0) with `mode` param (`"log"` default for musical octaves — equal-octave splits, `"linear"` for bent sweeps — equal-Hz splits), `waveform` (sine/triangle/square/saw), and a `freq` fallback for unpatched CV. Bipolar CV is clamped to [0, 1] (phase 2 adds the negative-side mapping). Voice-aware via shape-polymorphism on the CV input — (V, F) CV → V independent phase accumulators. Example: `examples/cvtofreq_blip.json` (synthesized-kick pitch envelope). 22 new tests; full suite 339 passing.
- [ ] `CVToFrequency` phase 2 — negative-side mirror. Adds `negative_enabled: bool` (default False, preserves phase-1 clamp behaviour), `f0_neg` / `fm_neg` / `f1_neg`, and `mode_neg` (independent of `mode` — lets the user mix log on positive side with linear on negative side, or vice versa). When enabled, CV in [-1, 0] uses the negative mapping. Zero-crossing is the user's responsibility (set `f0` == `f0_neg` for smooth, set different for a deliberate step). See `memory/project_cvtofrequency_plan.md`.
- [ ] Drive-by: `tests/test_adsr.py::test_no_nan_with_zero_durations` references an undefined `sr` (missing `sr = 44100` line at the top of the test). Pre-existing — not introduced by AudioToCV work, but worth a one-line fix.
- [ ] CV utility modules: `Constant` (params: value; outputs a fixed CV — useful as a CVCombiner input to bias another modulator), `CVScale` (cv in × gain param → cv out), `CVOffset` (cv in + offset param → cv out). The MIDIInput's `bend_range` / `mod_scale` patterns hint at the need; these utilities let any source feed any destination with arbitrary scale/offset without baking the knob into the source module.
- [ ] Sample-and-hold module
- [ ] Noise generator (white / pink)
- [ ] Drum-friendly env (AD instead of ADSR)
- [ ] Stereo-aware speaker module (pan / width)
- [ ] Patch presets palette (factory + user banks)
- [ ] Undo / redo on patch edits
- [ ] Per-key velocity calibration on MIDIInput — `velocity_curve: dict[int, float]` mapping MIDI note → velocity multiplier, applied after the 0-127 normalisation. Niche but exactly the kind of fix that's only possible because the synth lives in code: budget keybeds drift key-by-key due to manufacturing variance, and a "play every key at the same intended force, capture the offsets" calibration flow papers over it perfectly. Could ship as a small "Calibrate keys" dialog on the MIDIInput node.
- [ ] Refresh-devices button on the MIDIInput node — today the device combo snapshots `available_devices()` at widget creation; installing `[midi]` after the app is open leaves the dropdown stale until the patch is reopened.
- [ ] App icon for the packaged `.exe` -- add a `.ico` and reference it from `pysynthrack.spec` (EXE(icon=...))
- [ ] Code-signed build -- removes the SmartScreen "unrecognized publisher" prompt; only worth it if the synth ever leaves the hobby circle
