# PySynthRack architecture

## Layers

```
              ┌────────────────────────┐
              │   ui/  (DearPyGui)     │   reads & mutates Patch
              └──────────┬─────────────┘
                         │
                         ▼
              ┌────────────────────────┐
              │   core/  (Patch model) │   pure Python, serializable
              │   modules/             │   module-type catalog
              │   io_patch/            │   JSON load / save
              └──────────┬─────────────┘
                         │   AudioBackend reads from here
                         ▼
              ┌────────────────────────┐
              │   audio/               │   pyo OR sounddevice+numpy
              │   AudioBackend         │   compile → start → stop
              └──────────┬─────────────┘
                         ▼
                 system audio device
```

The three layers don't import each other circularly:

- `core/`, `modules/`, `io_patch/` depend on nothing outside the standard library (and on each other in that order).
- `audio/` depends on `core/` + its DSP library (`pyo` or `sounddevice`+`numpy`).
- `ui/` depends on `core/`, `modules/`, `io_patch/`, `audio/`, and `dearpygui`.

That means a future scripted/non-GUI front-end can drive the synth by importing only `core` + `audio`.

## Why a pure-data model

A module is a description, not a renderer:

```python
class Oscillator(Module):
    TYPE = "oscillator"
    DEFAULT_PARAMS = {"waveform": "sine", "freq": 440.0, "amp": 0.5}
    INPUT_PORTS = []
    OUTPUT_PORTS = [Port("out", "out", "audio")]
```

That's the entire definition. There's no `render()` method. When you save a patch, you get back what's already there — `to_dict()` is essentially `__dict__` plus port introspection. When the audio engine runs, the backend reads the description and constructs its native form (`pyo.Sine` or a numpy phase accumulator).

Advantages:

- One source of truth — saved patches and live patches use the same representation.
- Backends can be swapped without changing the model or modules.
- Adding a new module type means writing a small class plus a renderer in each backend — no boilerplate for serialization, UI, or registration (a decorator handles the last bit).

Trade-off:

- Each backend has to know how to render every module type. As the module library grows we may want a plug-in style where modules ship their per-backend renderers themselves. Easy to migrate to later.

## Connection rules

- Cables go from an output port on one module to an input port on another.
- One input port can have at most one incoming cable. To sum many signals into one, use a `Combiner` module (coming in v0.3). This rule keeps the topology unambiguous.
- Output ports may fan out to many inputs via a `Splitter` (also v0.3).
- Signal kinds (`audio`, `cv`, `gate`) must match at both ends of a cable. v0.1 uses only `audio`, but the type system is in place for envelopes and MIDI later.

## Compile vs. set_param

The backend distinguishes between two kinds of changes:

- **Structural changes** (added/removed modules, added/removed cables, waveform-type changes) → call `backend.compile(patch)`. May briefly stop/restart the audio stream.
- **Parameter tweaks** (freq, amp, gain) → call `backend.set_param(module_id, name, value)`. Cheap, glitch-free, no recompile.

The UI decides which side a given user action falls on. Adding a cable in the node editor recompiles; dragging a frequency slider doesn't.

## Backend selection

`audio.pick_backend()` picks the first backend whose dependencies import cleanly. The default order is `pyo` then `numpy`. Force a specific backend with the `PYSYNTHRACK_BACKEND` environment variable — handy when debugging or when one backend is misbehaving.

## Threading model

- DearPyGui owns the GUI thread.
- `sounddevice` (numpy backend) and `pyo` both run audio on their own internal threads.
- The Patch is read by the audio callback and mutated by the GUI thread. A `threading.Lock` in `NumpyBackend` protects the patch reference and topo order across that boundary. Pyo's internal thread is fed via pyo's own objects, which are themselves thread-safe.

The model objects (`Module`, `Patch`) are *not* themselves thread-safe — they're mutated under the GUI's implicit single-threaded ownership. The lock exists only to make backend's view of them consistent during a callback.

## Anti-aliasing

The numpy backend's saw / square / triangle are naive (no band-limiting). They'll alias above ~5 kHz fundamental. PolyBLEP or BLIT-based oscillators will land alongside the filter module in v0.2.

pyo's `LFO` produces band-limited waves; the pyo backend doesn't share the aliasing problem.
