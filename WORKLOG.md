# Worklog

Running log of decisions and progress. Newest first.

---

## 2026-05-13 (v0.3 starts) — CV-modulatable params

The big sound-design unlock: LFOs and envelopes can now sweep param
values through dedicated CV input ports on existing modules. The
filter cutoff is the obvious one (wah, filter envelope); oscillator
freq and amp open up vibrato/FM and AM/tremolo.

**1V/octave convention.** For frequency-domain params (cutoff, freq),
``effective = base * 2 ** cv``. A CV of +1 doubles the value, -1
halves it. This matches the standard modular-synth ergonomics — a
unipolar 0..1 envelope sweeps one octave up; a bipolar ±1 LFO swings
one octave each way. Amp CV is linear instead: ``amp * cv``, because
loudness perception is linear in this range and the multiplicative
shape lets a unipolar LFO act as a VCA-style amp modulator.

**Per-sample vs per-block.** Different modulation domains have
different sensitivity to update rate:
- ``oscillator.freq_cv`` and ``oscillator.amp_cv`` are evaluated per
  sample. Per-sample frequency integration (cumsum of inst-increments)
  is essentially free in NumPy and gives true vibrato/FM. Block-rate
  would alias the LFO into staircases at the block boundary.
- ``filter.cutoff_cv`` uses block-mean CV. Per-sample cutoff would
  need fresh biquad coefficients every sample — ~9x cost in the
  current scalar IIR loop. Block-mean is audibly fine at production
  block sizes (512–1024 samples); the LFO cycle has to be much
  shorter than the block for the mean to wash out, which would only
  happen at audio-rate "modulation" (i.e. FM cutoff), which is a
  different regime than what users want here.

**Cabling.** Adding ports is backward-compatible — old patches reference
ports that still exist (``in``, ``out``, ``gate``) and ignore the new
CV inputs. The patch model's signal-kind check ensures audio cables
can't accidentally land on a CV input.

**Files added/changed:**

- ``src/pysynthrack/modules/filter.py`` — Filter gains ``cutoff_cv``
  input (signal_kind ``cv``).
- ``src/pysynthrack/modules/oscillator.py`` — Oscillator gains
  ``freq_cv`` and ``amp_cv`` inputs.
- ``src/pysynthrack/audio/numpy_backend.py`` —
  ``_render_oscillator`` does per-sample 2^cv frequency integration
  via cumsum, plus per-sample linear amp multiplication. CV args on
  ``_render_oscillator`` are optional so existing test call sites
  (which pass just ``module, frames``) still work.
  ``_render_filter`` applies block-mean cutoff CV before the biquad
  coefficient pass.
- ``examples/wah.json`` — keyboard (saw) → bandpass filter ← LFO@1.5 Hz
  bipolar depth 1.5 on cutoff. Classic auto-wah.
- ``examples/filter_envelope.json`` — keyboard (saw) → lowpass filter
  ← ADSR (0.005/0.4/0.2/0.6) on cutoff. The acid bassline shape.
- ``examples/vibrato.json`` — oscillator ← LFO@5.5 Hz bipolar depth
  0.04 on freq. ~28 cents either side, gentle vibrato.
- ``tests/test_cv_modulation.py`` — 11 new tests: filter no-cv path
  is no-op, +1/-1/-5 octave shifts, end-to-end LFO sweep produces
  RMS swing; oscillator freq_cv at +1/-1 doubles/halves cycle count,
  phase continuity across blocks; amp_cv at 0/0.5 mutes/halves.
- ``tests/test_filter.py`` — updated input_ports assertion to expect
  ``["in", "cutoff_cv"]``.

**Verified in sandbox:** 108 tests pass (97 prior + 11 new).
End-to-end smoke render of the three example patches:
- ``wah.json``: per-block RMS swings 0.18–0.38 over LFO cycles.
- ``filter_envelope.json``: RMS 0.32→0.63 as envelope opens.
- ``vibrato.json``: RMS stable (vibrato changes pitch not amplitude);
  ear test on Matthew's side will confirm it's audibly modulated.

**Sound-design pairings to try (Matthew):**
- Open ``wah.json``, play a sustained note, drag the bandpass
  resonance up for a louder wah.
- ``filter_envelope.json`` with the keyboard set to a saw and decay
  long — bouncy filtered notes.
- ``vibrato.json`` — try cranking depth to 0.5 for tape-warble; rate
  to 30 Hz for a metallic FM tone (the LFO is now operating at
  audio-rate frequency modulation territory).
- Chain: LFO → freq_cv on osc, and a second LFO → cutoff_cv on a
  filter further down the chain. Two modulators at different rates.

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
  TODO.

---

## 2026-05-14 — v0.3 Routing pass: Combiner, CVCombiner, Crossover, DiskWriter, LFO.rate_cv

**Result.** v0.3 closes out the way it set out to: every routing item on
the roadmap is built or consciously ruled out, the modulation matrix
got its bonus rate_cv, and the synth can now record itself to disk. 140
tests passing (110 prior + 30 new), and every example patch — old and
new — still loads and plays.

**Splitter: built nothing, on purpose.** The audit said it best: the
Patch model already permits multiple cables from a single output port
(only inputs are mono — see `Patch.connect`'s "destination not already
occupied" check). The numpy backend keys its buffer cache by
`(src_module_id, src_port)`, so any number of downstream consumers
reading the same source key receive the same array — fanout is free.
DPG's node editor allows multiple links per output by default. So a
Splitter module would only add an empty box with one in and four
identical outs — overhead with no new capability. The TODO entry is
ticked with a "architecturally redundant" annotation so future-me
doesn't try to build it again. The new `examples/fan_out.json` patches
one keyboard into three different filter chains via three cables from
the same output, demonstrating the fanout explicitly.

**Combiner (audio).** The lighter sibling of Mixer. Four audio inputs,
plain unit-gain sum, one audio output, no per-channel widgets in the
way. Useful when you want a structural sum (parallel filter paths
re-joining, crossover low+high stitched back together) rather than a
level-balance task. Mixer's docstring already pre-declared the
contract before this module existed; I just honoured it.

**CVCombiner.** The new module that fills the real architectural gap.
Each input jack accepts one cable (mono input convention), but
modular CV is *additive* — wanting LFO + ADSR co-modulating filter
cutoff has been a thing since 1965. CVCombiner takes four CV inputs
and emits their sum (default) or their average over the connected
inputs. Sum is the analog-modular convention; average is the right
choice when you want shared control without the depth doubling. Two
unipolar LFO squares of depth=1.0 sum to 2.0 in sum mode; the same
two average to 1.0. Tests verify both modes and the unconnected-
inputs-don't-affect-divisor invariant on average mode.

**Crossover — Linkwitz-Riley 4th order.** Two cascaded RBJ biquads
per branch at Q=1/√2 (Butterworth), at the same corner frequency.
Each branch is 4th order so phase rotates a clean 360° per side,
which is why low + high recombines flat in magnitude through a
Combiner. Tests cover (a) low-freq tones land in the low branch and
silence the high; (b) high-freq tones, vice versa; (c) at the corner
both branches sit at -6 dB (LR's signature); (d) summing low + high
through a Combiner reconstructs the source RMS within ±15%; (e)
extreme frequency values clamp without NaN. The new
`two_way_crossover.json` shows the canonical use: split a saw at
800 Hz, LP the low band, BP the high band, recombine — instant
multi-band shaping.

**DiskWriter.** A sink module. Audio in, nothing out, written to disk
as a 16-bit mono WAV at the backend's sample rate. Threading model
explained in the docstring: the audio callback hands blocks to a
bounded `queue.Queue`, a daemon worker thread pops and writes via the
stdlib `wave` module. The callback never blocks on filesystem I/O —
if the queue fills (very unlikely, 64 blocks ≈ 750 ms of latency), the
incoming block is dropped and a counter is bumped rather than the
audio thread stalling. Lifecycle hooks: the writer opens the file
lazily on first audio arrival; `armed=False` skips and tears down any
active worker; the path being edited mid-take closes the old file and
starts a new one (a manual punch-in); `backend.stop()` and
`backend.compile()` both close any active writer state so the WAV
header gets finalised and no thread leaks. Tests verify all of these:
disarmed creates no file, armed-but-unpatched creates no file, normal
recording writes the right number of frames and RMS matches the
source, mid-take path swap produces both files, disarm closes the
file, and a recompile swap that drops the disk_writer state closes
it cleanly.

**LFO.rate_cv — modulation matrix territory.** A second LFO (or ADSR)
can now modulate this LFO's rate. 1V/oct, block-mean evaluation,
same trade-off as filter cutoff_cv. Together with the existing
freq_cv / amp_cv / cutoff_cv ports this means CV can route to nearly
every continuous parameter that matters. The new
`examples/mod_matrix.json` shows the classic "breathing vibrato" —
a 0.3 Hz LFO modulating a 5 Hz vibrato LFO's rate, which itself
modulates oscillator freq.

**Backend wiring.** `_render_module` dispatches to four new
renderers. `_render_lfo` now accepts optional buffers/patch so it can
look up rate_cv when called from the topo walk (same back-compat
trick as `_render_oscillator`). `compile()` no longer just drops
state — when a disk_writer entry is being discarded (module removed,
or recompiled type changed) it calls `_close_disk_writer_state` first
so the file handle and thread don't leak across recompiles.
`stop()` walks the state map and closes any active writers so the
WAV header is finalised when the user hits Stop on the transport.

**UI wiring.** The Add Module menu pulls from `all_module_types()`
so the four new modules appeared in the palette for free. Three
small param-widget tweaks: the `mode` combo dispatches on module
type (cv_combiner → sum/average, filter → LP/HP/BP), `frequency` is
treated like `freq`/`cutoff` (drag float in Hz), and `path` falls
through to the existing input_text fallback. Boolean `armed` already
got a checkbox via the existing bool branch.

**Edit-tool truncation, again.** Hit the same file truncation issue
three times on this pass — numpy_backend.py, test_lfo.py, and
WORKLOG.md all got chopped mid-line by Edit. Switched all non-trivial
rewrites to Python scripts via `mcp__workspace__bash` (read whole
file → in-memory transform → write whole file → AST parse). The
memory entry on this is already current.

**File hygiene.** The disk_writer smoke test left a `take_01.wav` in
the project root (2 seconds, 88,064 samples, mono 16-bit at 44.1k —
exactly the smoke-test render length). The sandbox can't delete files
on the Windows mount, so it's still there waiting to be removed
manually.

**Architecture notes:**
- LR4 crossover is a clean candidate for "extract a biquad helper"
  if a 4th module ever reaches for one. Today we have: Filter
  (single biquad), Crossover (four biquads), and that's the threshold
  where DRY starts to win. Holding off until that 4th caller arrives.
- DiskWriter is the first module that owns process resources (a
  thread + a file handle) rather than pure numerical state. The
  cleanup hooks live in `_close_disk_writer_state` so the pattern
  is reusable if (say) a future MIDI input module needs its own
  worker thread.
- CV summing happens to be cheap because every CV buffer is
  float32 same-length — `out += buf` is one numpy fused-multiply.
- Fanout was a deliberate v0.1 design choice (port-keyed buffers)
  paying dividends in v0.3 with zero new code. Worth keeping in
  mind when the v0.4 polyphony refactor lands.

**Counts.** Modules: 8 → 12. Examples: 9 → 14. Tests: 110 → 140.
LOC of numpy_backend.py: ~656 → ~902. v0.3 is shipped — next stop
v0.4 (MIDI, real polyphony, anti-aliased osc shapes).
