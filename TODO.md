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
- [ ] Voice routing manager — **Option A: voice-aware signals** committed 2026-05-15, design questions resolved 2026-05-20 (fixed 16 slots zero-padded, mono fast path preserved).
      Slice 1 shipped 2026-05-20: `VoiceSlots` allocator in `core/voicing.py`, MIDIInput backed by it, sustain pedal (CC 64), `snapshot_voice_slots()` API ready for the renderer. Slice 2 (next): polyphonic `_render_midi_input` emitting `(16, frames)` for out/gate/pitch_cv + mono sinks summing the voice axis. Slice 3: Oscillator/ADSR/Filter/LFO/Crossover go shape-polymorphic.
- [x] MIDI follow-up: pitch bend → `pitch_cv` output port + internal voice bend, GM-standard `bend_range=2.0` (shipped 2026-05-15)
- [x] MIDI follow-up: mod wheel (CC 1) → `mod_cv` output port + `mod_scale` param (shipped 2026-05-15)
- [x] MIDI follow-up: channel aftertouch → `pressure_cv` output port + `pressure_scale` param (shipped 2026-05-15)
- [x] Error handler integrated at GUI outermost catch + audio callback panic path; crashes written to `~/.pysynthrack/crashes/` (shipped 2026-05-15)
- [x] Single-file Windows `.exe` build (PyInstaller) -- `build.ps1` + `pysynthrack.spec`, examples bundled read-only, console variant `build_cli.ps1` for debugging (shipped 2026-05-17)
- [x] MIDI follow-up: sustain pedal (CC 64) — shipped 2026-05-20 with voice routing slice 1. Per-slot `sustained` flag on `VoiceSlots`; pedal-down causes note_off to mark the slot sustained instead of releasing; pedal-up drops every sustained voice in one pass.
- [ ] PolyBLEP or wavetable anti-aliased osc shapes (replace naive saw/square)
- [ ] CPU profile: pyo backend wired for the same modules so it's a drop-in fast path

## Later / wishlist

- [ ] Signal-kind bridge modules: `AudioToCV` envelope follower (RMS the audio into a CV value, e.g. for self-modulating filter cutoff), `CVToAudio` (let you hear an LFO / envelope through the speaker for debugging slow modulators), `Schmitt` (CV threshold crossing emits a gate edge for chaining envelopes off a CV signal). Orthogonal to backend choice; cheap to add whenever DSP exploration sounds fun.
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
