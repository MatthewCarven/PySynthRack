# Worklog

Running log of decisions and progress. Newest first.

---

## 2026-05-13 (v0.2 ships) — Mixer module

Closing v0.2 with the missing summing point. The mixer takes four audio
inputs, applies a per-channel gain trim, sums them, and applies a master
gain before output.

**Why fixed 4 channels, not N.** Flat JSON schema, predictable UI,
covers the typical patches we'll build during v0.2 (layered oscillators,
detuned saws, osc + sub + noise, dual-keyboard splits). The v0.3
``Combiner`` will handle unbounded-N pure summation — different concept,
no per-channel trims, lives in the routing-primitives bucket.

**Cabling.** One cable per input jack — same rule as every other module.
To bus more than four sources, chain mixers (mixer-of-mixers).

**Param ranges.** Channel gains and master live in [0, 2], slightly hot
so users can lift a quiet channel without leaving the slider. Speaker
output still clips at ±1, so over-driving the mixer is a hard ceiling,
not an explosion.

**UI tweak.** The slider-float branch was extended so any param named
``gain*`` or ``master`` lands in the 0-to-2 range. Previously only the
bare name ``"gain"`` qualified, so mixer's ``gain1``-``gain4`` would
have fallen into the generic drag-float.

**Files added/changed:**

- ``src/pysynthrack/modules/mixer.py`` (new) — Mixer class +
  MIXER_INPUT_NAMES / MIXER_GAIN_NAMES tuples
- ``src/pysynthrack/modules/__init__.py`` — register Mixer
- ``src/pysynthrack/audio/numpy_backend.py`` — ``_render_mixer``
  (port-lookup sum × master)
- ``src/pysynthrack/audio/pyo_backend.py`` — friendly skip for mixer
- ``src/pysynthrack/ui/app.py`` — slider widget covers any ``gain*`` /
  ``master`` param at 0..2
- ``examples/fat_saw.json`` — three saws detuned ±1.5 Hz around 220 Hz
  through the mixer, then a lowpass with some resonance. Stored
  positions show the mixer fanning into a single bus.
- ``tests/test_mixer.py`` — 13 new tests (model, port shape, JSON
  round-trip, signal-kind rejection, one-cable-per-jack, render
  silence/sum/per-channel/master arithmetic, four-input contribution,
  disconnected-channel silence, end-to-end render of fat_saw.json)

**Verified in sandbox:** 95 tests pass (82 prior + 13 new).
``examples/fat_saw.json`` loads, renders finite non-silent audio
through the full chain, and the speaker-stage clip keeps output ≤ 1.0.

**v0.2 SHIPPED.** Module library now: Oscillator, Keyboard, Filter,
ADSR, VCA, LFO, Mixer, SpeakerOutput. Drag-cable UI, JSON save/load
with node positions, CLI fallback, 95-test safety net. From oscillator-
to-speaker on Friday to a playable subtractive synth one week later.

**Pending from Matthew:**
- Open ``examples/fat_saw.json``, hit Start audio, you should hear a
  fat detuned sustained chord-ish drone. Sweep the filter cutoff
  while it plays for the classic supersaw lead motion.
- Try chaining mixers: 3 oscillators into mixer A, mixer A + keyboard
  + LFO into mixer B for a polyphonic-with-pad layer.

**Sandbox note:** continued to use bash heredoc + AST-parse-after-each-
write for every file edit this pass; zero truncation incidents.
Memory ``feedback_edit_tool_truncation.md`` covers the pattern.

---

## 2026-05-12 — v0.1 scaffold

**Decisions made with Matthew:**

- Project name: **PySynthRack**.
- Located in the existing `Python Synthesizer` workspace folder under `C:\Users\Admin\Desktop\-=Programming=-\Python Synthesiser 2\`.
- Audio stack: **abstract the backend** — both `pyo` and `sounddevice + numpy` implementations behind one interface. pyo preferred, numpy fallback. Auto-pick at startup based on what's installed.
- Python: whatever's on PATH (project pins `>=3.9`).
- UI: DearPyGui (built-in node editor with cable drawing).

**Architecture pattern picked:**

Modules are **pure model objects** (type + params + declared ports). They don't render audio themselves. Each `AudioBackend` implementation walks the patch and builds its own native graph — `PyoBackend` constructs `pyo.Sine` etc.; `NumpyBackend` builds a callback that walks the topology each buffer.

Why this shape:
- DSP knowledge sits in the backend, not duplicated across module classes.
- The model layer is trivially serializable (it's already a dict-of-dicts).
- Swapping backends doesn't require touching the UI or modules.

Trade-off: adding a new module type means adding a renderer for it in each backend. For a small, fixed module library that's fine; if the module set explodes later we may want a plugin-style registration where each module ships its own per-backend renderers.

**Scaffolded:**
- `src/pysynthrack/` package with `core/`, `audio/`, `modules/`, `ui/`, `io_patch/` subpackages
- `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `README.md`
- `tests/`, `examples/`, `docs/`

**Built in this pass:**
- `src/pysynthrack/core/` — `Port`, `Module` (with type registry decorator), `Patch` (graph + validation + serialization), `Cable`
- `src/pysynthrack/audio/` — `AudioBackend` ABC, `PyoBackend`, `NumpyBackend`, `pick_backend()` auto-selector (with `PYSYNTHRACK_BACKEND` override)
- `src/pysynthrack/modules/` — `Oscillator` (sine/saw/square/triangle), `SpeakerOutput`
- `src/pysynthrack/io_patch/` — `save_patch`, `load_patch`, JSON string helpers
- `src/pysynthrack/ui/app.py` — DearPyGui node editor, palette via menu, file open/save dialogs, transport button, inline param widgets per node
- `examples/hello_sine.json` — 440 Hz sine → speaker
- `tests/` — 24 headless tests covering model rules, JSON round-trip, oscillator DSP correctness, phase continuity, topo sort
- `docs/architecture.md` — layering, why pure-data model, compile-vs-set_param contract

**Verified in the sandbox:**
- All Python files compile (`py_compile`)
- 24/24 headless tests pass
- UI module imports cleanly with stubs

**Not yet verified (needs your Windows machine):**
- DearPyGui actually renders the node editor
- pyo install works on your Python
- A 440 Hz sine actually comes out of the speakers

## 2026-05-12 (later) — install hotfix + CLI fallback

Matthew's first install attempt failed. Two root causes:

1. **DearPyGui has no wheel for his Python** — pip's `(from versions: none)` is conclusive. Probably Python 3.13 or 3.14 where DPG hasn't published wheels yet. The original requirement `dearpygui>=1.10,<3.0` made it worse (excluded 2.x).
2. **`No module named pysynthrack`** after install — original README told user to `pip install -r requirements.txt`, which installs deps but never installs the project. Should have been `pip install -e .`.

**Fixes shipped:**

- `pyproject.toml`: moved DearPyGui to an optional dependency under `[gui]` extra. `pip install -e .` now succeeds with just numpy + sounddevice; `pip install -e ".[gui]"` adds the GUI. `[pyo]` and `[all]` extras also added.
- `src/pysynthrack/__main__.py`: added argparse with `--cli`, `--patch`, `--seconds`, `--backend` flags. GUI mode automatically falls back to CLI mode if DearPyGui isn't installed, with an informative stderr message.
- `src/pysynthrack/cli.py`: new headless runner. Loads a patch, prints the module/cable summary, picks a backend, starts audio, waits for Enter (or `--seconds`).
- README: rewrote install + run sections. Documents `[gui]`/`[pyo]`/`[all]` extras and CLI usage.

**Verified in sandbox:**

- All 24 tests still pass.
- CLI module imports cleanly without DearPyGui installed.
- `__main__.py` correctly detects missing DPG and routes to CLI fallback.

**Pending from Matthew:**

- ✅ Install succeeded with `pip install -e .` (2026-05-12).
- ✅ CLI mode plays sound. v0.1 audio goal hit.
- ✅ GUI install working (2026-05-12). Path: `uv python install 3.12` → `uv venv --python 3.12 .venv` → `uv pip install -e ".[gui]"`. pyo skipped (no Windows wheels, no MSVC build tools); numpy backend covers v0.1.

**v0.1 SHIPPED** — model, both backends, oscillator, output, JSON I/O, drag-cable GUI, CLI mode, 24 tests passing, and verified on Matthew's machine. From zero to playable synth in one session.

---

## 2026-05-12 (v0.2 starts) — Keyboard module

First v0.2 module shipped: `Keyboard` lets the computer keyboard play polyphonic notes through the synth.

**Layout** — one octave per home row, black keys on the QWERTY row above (FL Studio / Ableton typewriter style):

```
   W E   T Y U   O P
  A S D F G H J K L ;
   C# D# F# G# A#  C# D# (over)
  C  D  E  F  G  A  B  C  D  E
```

A/W/S/E/D/F/T/G/Y/H/U/J = chromatic C through B in the selected octave; K onwards spills into the next octave.

**Params** (all inline on the node, per the UX decision):
- `octave`: int slider 0–8, default 4 (so home-row A = middle C / MIDI 60)
- `waveform`: sine / saw / square / triangle (shared definition with the Oscillator module)
- `volume`: 0–1 master gain for the whole keyboard

**Architecture choices:**

- Keyboard owns its own `active_notes: set[int]` (transient, not serialized to JSON). UI mutates it via `note_on` / `note_off` under a `threading.Lock`. The audio thread reads via `snapshot_active_notes()` which returns a copy under the same lock. This keeps the pure-data model design intact (params is just spec) while giving the audio thread a safe view of live keyboard state.
- Polyphony is per-voice: each pressed note gets a voice dict with its own phase + envelope level. Voices are reaped once their release ramp returns to ~0.
- 5 ms linear attack + 20 ms linear release ramps prevent the click that would otherwise happen on every note edge. Not a full ADSR — that's a separate v0.2 module.
- Global DPG `handler_registry()` catches all key events. OS auto-repeat is debounced via `_held_keys` so holding A is one note, not a stream. All-notes-off is fired on audio-stop and patch-clear to prevent stuck notes.
- Pyo backend prints "not yet supported" for the keyboard type and produces silence — the dynamic voice allocation pattern doesn't map cleanly onto pyo's static-graph model without a separate Voice-manager design. Punted to v0.3.

**Files added/changed:**

- `src/pysynthrack/modules/keyboard.py` (new) — Keyboard class + midi/note helpers
- `src/pysynthrack/audio/numpy_backend.py` — `_render_keyboard` with envelope ramps and voice reaping
- `src/pysynthrack/audio/pyo_backend.py` — friendly "not yet supported" hint
- `src/pysynthrack/ui/app.py` — key handlers, int slider for octave, all-notes-off on stop
- `examples/keyboard_play.json` — keyboard wired to speaker (saw, octave 4)
- `tests/test_keyboard.py` — 15 new tests covering note math, model behaviour, polyphony, envelope ramp

**Verified in sandbox:** 39 tests pass (24 from v0.1 + 15 new), UI compiles and imports.

**Pending from Matthew:** run `python -m pysynthrack`, File → Open → `examples/keyboard_play.json`, hit Start audio, tap A/S/D/F/G/H/J — should hear a saw chord follow your typing.

---

## 2026-05-13 (v0.2 continued) — LFO + silent-exit bugfix + node positions

Three changes landed together because Matthew flagged the bug and the
missing positions while asking for the LFO; all three are small.

**Silent-exit on second Open (the bug).** DearPyGui's node editor keeps
its children in two slots: links in slot 0, nodes in slot 1. The
original `_clear_editor` only iterated slot 1, so opening a second
patch left orphan links pointing at attribute IDs from the now-deleted
nodes. Next frame, DPG hard-exits the process with no Python traceback.
Fix is one line: `dpg.delete_item(EDITOR_TAG, children_only=True)` —
clears every slot, no orphans. Defensive fallback (per-slot loop) kept
in case a future DPG release tightens the contract. While here, also
reset `_next_node_pos = [40, 40]` on clear so a fresh-loaded patch
without saved positions lays out from the top-left again.

**Node positions persist in JSON.** Added an optional opaque `ui` dict
to `Patch` (round-trips through `to_dict`/`from_dict`, omitted from
output when empty so the schema bump is invisible to callers that don't
use it). At save time `_capture_node_positions` snapshots
`dpg.get_item_pos(node_id)` for every live node into
`patch.ui["node_positions"]`. At load time `_load_patch_from` reads it
back and passes it to `_create_node_for_module(module, pos=...)`. The
staggered placement still kicks in for any module whose ID isn't in the
map — so legacy patches and freshly-added nodes both behave sensibly.
Positions are JSON-string-keyed (`{"1": [x, y]}`) because JSON object
keys are strings; converted at the call site.

**LFO module.** Output is CV (so it cannot be patched into audio
inputs by mistake). Five waveforms: sine, triangle, square, saw,
random (sample-and-hold — re-rolls on each phase wrap). Three params:
`rate` (Hz, clamped 0.001 to 0.45·sr), `depth` (0–1), and `bipolar`
(bool). Unipolar is the default: the wave is shaped into [0, depth]
so an LFO → VCA chain produces tremolo without the inverted-phase
audio fight you'd get from raw [-1, 1] modulation. Flip `bipolar` for
pitch / cutoff sweeps once those become CV-routable.

**Architecture notes:**
- LFO and Oscillator share the same per-block phase-accumulator
  pattern; if we ship more waveform-driven modules a shared
  `waveform_sample(phases, kind)` helper is worth pulling out. Held
  off for now — three callers don't justify the indirection yet.
- Pyo backend logs "not yet supported" for `lfo`, matching the
  established pattern.
- A CV mixer/multiplier would let LFO and ADSR co-modulate a VCA. It's
  on the v0.3 list along with the rest of the routing primitives.
- Filter has no CV input on its `cutoff` param yet, so LFO → filter
  cutoff doesn't work in v0.2. Added "CV-modulatable params" to v0.3
  T