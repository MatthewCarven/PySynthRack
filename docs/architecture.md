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

### Renderer families (the split, started 2026-09-25)

`audio/numpy_backend.py` grew to ~22k lines with every renderer on one
class. Families now move out one at a time into `audio/renderers/<family>.py`
as **mixin classes** that `NumpyBackend` inherits:

```python
class NumpyBackend(ClockworkRenderers, AudioBackend): ...
```

A move is verbatim (only relative-import depth changes), so `self` is still
the backend — `self._state`, `self._input_buffer`, `self._GATE_HIGH` all
resolve as before — and `NumpyBackend._render_x` is still found by attribute
lookup, so tests that call or monkeypatch renderers need no changes. Each
move is proven behaviour-neutral by `tools/render_audit.py` (every example
bit-identical) plus the full suite. Source-scanning tripwires must cover the
package: `test_every_voice_collapse_goes_through_a_door` scans the backend
**and** every module under `audio/renderers/`.

A renderer that names `NumpyBackend` itself (usually a static helper calling
another static on the backend) reaches it through a **lazy import** at the
point of use, since `numpy_backend` imports the mixin module. The mechanics
live in `tools/split_renderers.py` (`analyse` a block, then `move` it from a
JSON config); its docstring lists the four checks every move must pass.

Moved so far: **clockwork** (euclidean, burst, bernoulli gate, clock divider)
and **dynamics** (compressor, limiter, noise gate, transient shaper).

## Connection rules

- Cables go from an output port on one module to an input port on another.
- One input port can have at most one incoming cable. To sum many signals into one, use a `Combiner` (audio) or `CVCombiner` (CV) module, or a `Mixer`/`MatrixMixer`. This rule keeps the topology unambiguous.
- Output ports may fan out to many inputs directly — no splitter module is needed; a cable list, not a port field, records the connections.
- Signal kinds (`audio`, `cv`, `gate`) must match at both ends of a cable, and all three are in active use. Bridge modules (`audio_to_cv`, `cv_to_audio`, `cv_to_frequency`, `schmitt`) convert between them.

## Compile vs. set_param

The backend distinguishes between two kinds of changes:

- **Structural changes** (added/removed modules, added/removed cables, waveform-type changes) → call `backend.compile(patch)`. May briefly stop/restart the audio stream.
- **Parameter tweaks** (freq, amp, gain) → call `backend.set_param(module_id, name, value)`. Cheap, glitch-free, no recompile.

The UI decides which side a given user action falls on. Adding a cable in the node editor recompiles; dragging a frequency slider doesn't.

**Param writes go to the model first.** Every UI param edit runs through
`App._set_module_param`, which sets the value on the `Module` and *then*
notifies the backend. That order matters: a backend holds no patch until
`compile()` is called (which the UI only does at Start), so a backend-first
write is dropped on the floor for the whole time a patch sits open and
un-started. The model is the source of truth; `backend.set_param` is the
live-update notification on top of it, not the place the value lives.

## Backend selection

`audio.pick_backend()` picks the first backend whose dependencies import cleanly. The default order is `pyo` then `numpy`. Force a specific backend with the `PYSYNTHRACK_BACKEND` environment variable — handy when debugging or when one backend is misbehaving.

## Threading model

- DearPyGui owns the GUI thread.
- `sounddevice` (numpy backend) and `pyo` both run audio on their own internal threads.
- The Patch is read by the audio callback and mutated by the GUI thread. A `threading.Lock` in `NumpyBackend` protects the patch reference and topo order across that boundary. Pyo's internal thread is fed via pyo's own objects, which are themselves thread-safe.

The model objects (`Module`, `Patch`) are *not* themselves thread-safe — they're mutated under the GUI's implicit single-threaded ownership. The lock exists only to make backend's view of them consistent during a callback.

## Anti-aliasing

The numpy backend ships three oscillator families. The plain `saw` / `square` /
`triangle` waveforms are naive (no band-limiting) and alias above roughly a
5 kHz fundamental — kept because their harshness is sometimes what you want.
The `*_blep` waveforms apply PolyBLEP/PolyBLAMP correction at each
discontinuity, and the `*_wt` waveforms read per-octave band-limited mipmapped
wavetables. Derived sources built on that infrastructure (`supersaw`,
`wavetable_morph`) are band-limited by construction.

The pyo backend is parked/stubbed for most module types; the numpy backend is
the reference implementation.
