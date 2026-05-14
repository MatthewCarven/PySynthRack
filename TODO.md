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
- [ ] Voice routing manager (multiple oscillators → polyphony per Keyboard voice) — design pending: voice-aware signals vs. explicit voice fanout. Either way, this is a model-level change, not just a new module.
- [ ] MIDI follow-ups: pitch bend → freq_cv output port, sustain pedal (CC 64), mod wheel (CC 1) → mod_cv output, channel-aftertouch → pressure_cv.
- [ ] PolyBLEP or wavetable anti-aliased osc shapes (replace naive saw/square)
- [ ] CPU profile: pyo backend wired for the same modules so it's a drop-in fast path

## Later / wishlist

- [ ] Sample-and-hold module
- [ ] Noise generator (white / pink)
- [ ] Drum-friendly env (AD instead of ADSR)
- [ ] Stereo-aware speaker module (pan / width)
- [ ] Patch presets palette (factory + user banks)
- [ ] Undo / redo on patch edits
