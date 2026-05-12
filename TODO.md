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
- [ ] ADSR envelope module
- [ ] LFO module
- [ ] Mixer module (N inputs, gain per input, master out)
- [ ] Gate/Trigger signal type so envelopes can be triggered

## v0.3 — Routing

- [ ] Splitter (one out → many)
- [ ] Combiner (many → one, summed)
- [ ] Linkwitz-Riley crossover (split at chosen Hz into low + high outputs)
- [ ] Disk-writer output (write to WAV)

## v0.4 — Playable

- [ ] MIDI input (keyboard play)
- [ ] Note→Voice routing
- [ ] Polyphony (voice manager around the patch)

## Maybe later

- [ ] Pattern sequencer module
- [ ] Sample player module
- [ ] Module palette categories + search
- [ ] Patch versioning / migration on load
- [ ] CV (control-voltage) vs audio signal distinction in the type system
