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

---

## 2026-05-14 — v0.4 starts: MIDI Input module

**Result.** MIDI keyboards play any existing patch — install the `[midi]`
extra, drop a MIDIInput node in place of a Keyboard node, and you have a
playable instrument. 172 tests passing (140 prior + 32 new). 13 modules,
16 example patches. v0.4 voice routing manager is a deliberate separate
slice; design pending.

**Sequencing choice.** The roadmap bundled "MIDI input" with "voice
routing manager" as one item, but they're very different jobs. MIDI
input as a self-polyphonic mirror of Keyboard is a single-module change
with no model-level impact. Voice routing — making each note into its
own signal path — is a model-level rewrite (either voice-aware signal
carriers, or explicit voice fanout). Splitting them lets MIDI ship now;
voice routing gets a proper design pass before it lands.

**MIDIInput module — what it is.** Same shape as Keyboard: no input
ports, two outputs (`out` audio, `gate` gate), self-polyphonic voice
tracking inside the module. The only structural difference is that
`active_notes` is a `dict[int, float]` instead of a `set[int]` — the
value is normalised note-on velocity. Renderer mirrors `_render_keyboard`
exactly with one extra line of velocity scaling per voice.

**Threading model.** mido owns its own IO thread. `start_midi()` opens
the port and registers `self._on_message` as the callback; mido invokes
the callback on its IO thread for every incoming message. The callback
mutates `active_notes` under `self._lock`. The audio thread takes a
snapshot copy each block via `snapshot_active_notes()` so it never
iterates a dict the MIDI thread might be writing. Exactly the lock
pattern Keyboard already uses for computer-key events; nothing novel,
which is the point.

**Message handling, what's in scope.** `note_on` with velocity > 0 adds
the note. `note_on` with velocity == 0 is treated as `note_off` (the
running-status optimization most controllers use — saves a status byte).
`note_off` removes the note. CC 123 (All Notes Off) clears everything.
Channel filter applies: param `channel=0` is omni; 1–16 filters on the
matching mido 0-indexed channel. Out of scope for this slice: pitch
bend, sustain pedal (CC 64), mod wheel (CC 1), aftertouch. Each of
those is a natural fit for new CV output ports (`pitch_cv`, `mod_cv`,
`pressure_cv`) and lands in a v0.4 follow-up.

**Octave shift.** Applied at note ingest time, not at render time. A
`note_on(60)` with `octave_shift=1` stores 72 in `active_notes`. A
subsequent `note_off(60)` resolves to the same shifted note and clears
it. Notes shifted outside the MIDI range (0..127) are dropped silently
rather than wrapping or clipping — voicing C-1 with `octave_shift=-1`
gets you nothing, not a wrong note.

**Velocity sensitivity.** Two-state param: `True` (default) scales each
voice by its normalised velocity; `False` plays every voice at unity.
Useful for organ-style patches where dynamic expression doesn't belong,
or for controllers with bad velocity curves. The velocity is always
stored in voice state — the param decides whether to apply it, so the
toggle takes effect immediately without disrupting active voices.

**Lifecycle wiring.** Tracked on the backend via `self._midi_inputs:
dict[int, MIDIInput]`. On `compile()`: new patch's MIDIInput modules
get their ports opened (idempotent if already open with the right
device); old ones that left the patch get their ports closed. On
`stop()`: every tracked MIDIInput's port is closed so the next start()
reopens cleanly. The module instances live on the patch, so closing the
port is the right teardown — we don't drop the module, just its OS
resource. Same lifecycle pattern as DiskWriter (own process resource,
explicit teardown hooks), generalised to a tracked-instances dict.

**Optional dependency handling.** `mido` and `python-rtmidi` are an
opt-in `[midi]` extra because `python-rtmidi` is a C extension and can
fail to build on locked-down systems. The module *imports cleanly*
without them (import-guarded with a `_MIDO_AVAILABLE` flag), so the
registry still sees MIDIInput, the UI palette still shows it, the JSON
loader can still create instances. The missing-dep error is reported
only when `start_midi()` is actually called — log warning, return,
render silence. This means a patch saved with a MIDIInput node loads
fine on a machine without mido; you just won't get notes.

**UI wiring.** Four new param widget branches in `_add_param_widget`:
`device` (combo populated by `available_devices()`, with `""` at the
top for auto-pick), `octave_shift` (int slider ±4), `channel` (int
slider 0..16), and `velocity_sensitive` falls through to the existing
bool checkbox branch. The device combo snapshots devices at widget
creation; user can recompile (delete + re-add the node, or reopen the
patch) to refresh after hot-plugging. Could add a refresh button later
if hot-plug refresh becomes annoying.

**Tests — 32 new, all pass headless.** Metadata sanity (5), direct
note_on/off ingest including thread-safety stress (11),
`mido.Message`-driven callback handling (6), channel filter (2),
rendering through the numpy backend (6), optional-dep guardrails (2).
The mido-message tests skip if mido isn't installed in the test env;
the rest don't require it. No real MIDI hardware is needed for any
test — we pass `mido.Message` objects directly into the callback.

**Example patches — 2 new.**

* `midi_simple.json` — MIDIInput → SpeakerOutput. The hello-world. One
  cable, plays the configured waveform whenever a note is held.
* `midi_lead.json` — MIDIInput → LP filter (cutoff modulated by ADSR
  off the MIDI gate) → VCA (gain modulated by a second ADSR off the
  same gate) → SpeakerOutput. The "proper" played-by-MIDI lead patch
  with a filter envelope and amp envelope, both triggered by the global
  gate. Tuned volume=0.35 because resonance=4 + a saw was clipping at
  the speaker; that headroom is the cost of the resonant peak.

**Bugs hit & fixed.**

* **VCA's audio input is named `audio`, not `in`.** First draft of
  `midi_lead.json` connected the filter to `vca.in` and got silence.
  Surfaced because the renderer returns silence when its declared input
  port has no cable. Fix: use `vca.audio`.
* **Edit-tool truncation, again.** The first save of midi_lead.json
  had its final `}` chopped by the Edit tool — same Windows-mount bug
  that bit us on numpy_backend.py and WORKLOG.md during v0.3. Rebuilt
  the file via bash heredoc. The memory note on this is still current.

**Counts.** Modules: 12 → 13. Examples: 14 → 16. Tests: 140 → 172.
v0.4 first slice shipped — next stop voice routing manager (design
pending), then anti-aliased oscillators, then porting the rest of the
graph into pyo.

---

## 2026-05-15 — MIDI Input confirmed end-to-end on real hardware

First played note through a real MIDI controller — Matthew's USB
keyboard plugged into Windows, `[midi]` extra installed, GUI launched,
`midi_lead.json` loaded, device picked from the populated dropdown,
keys pressed, audio out. Closes the loop on the v0.4 first slice; no
code changes needed from the headless tests.

**Install-day friction we should remember for future docs.**

* The `[midi]` extra is genuinely opt-in — on a fresh pull from the
  v0.4 commit, the device dropdown is empty until `uv pip install -e
  ".[midi]"` runs. The graceful-fallback design means it doesn't
  *break*, but a first-time user can mistake the empty list for "no
  devices" rather than "no library". The README install section calls
  out the extra explicitly; we should keep that prominent.

* The GUI's device combo snapshots `available_devices()` at widget
  creation. If a user installs `[midi]` while the app is already
  running, the dropdown won't repopulate until the patch is reopened
  (or the MIDIInput node deleted + re-added). A "refresh devices"
  button on the MIDIInput node would close this hole; small follow-up
  for the next UI sweep, but not urgent.

* Once a device is selected and the transport is started, silence
  before the first key-press is the correct idle state — `midi_lead`
  is gated through ADSRs off the MIDI gate, so the audio path is
  zero-amplitude until something plays. Worth keeping in mind for
  troubleshooting walk-throughs: "no sound at idle" is the design,
  not a bug.

---

## 2026-05-15 (continued) — Pitch bend on MIDIInput

First of the v0.4 MIDI follow-up slices. The wheel now does two things:

* **Internal:** each held voice's frequency is multiplied by
  ``2 ** (bend_normalized * bend_range / 12)`` at render time. With the
  default ``bend_range=2.0`` semitones (GM standard), a fully deflected
  wheel takes a note +-200 cents. Custom ``bend_range`` lets a patch
  widen this for dive-bomb leads (12.0 = +-octave) or narrow it for
  subtle expressive vibrato (0.5).
* **External:** a new ``pitch_cv`` output port emits the same
  ``bend_normalized * bend_range / 12`` value as a block-constant CV
  signal. Wire it to a filter's ``cutoff_cv`` for the classic "wheel
  opens the timbre" trick, or sum it through a CVCombiner with another
  modulator.

**Normalization.** mido's ``msg.pitch`` is signed 14-bit in
``[-8192, 8191]``; we divide by 8192.0 and clamp to ``[-1, 1]``. The
``+8191`` case gives ``0.99988``, which is the standard MIDI-spec
asymmetry. Wheel at rest = 0.

**State location.** ``self._pitch_bend`` lives on the MIDIInput module
under the same ``self._lock`` that guards ``active_notes``. Same
callback-thread / audio-thread split as note state -- mido callback
writes, audio thread snapshots once per block. ``stop_midi()`` resets
it to 0 so a recompile starts from neutral; ``all_notes_off()``
(CC 123 panic) deliberately does NOT touch it, because the physical
wheel position is independent of held-note state.

**Block-rate, not per-sample.** The ``pitch_cv`` buffer is constant
within a block. At 512 samples / 44.1 kHz that's an update every
~11.6 ms -- well below the threshold of audibility for any natural
wheel motion. Per-sample smoothing would be nice for staircase-free
vibrato via the wheel and is worth revisiting if anyone reports
stepping artefacts; for now block-rate matches the rest of our CV
consumers.

**Files added/changed:**

- ``src/pysynthrack/modules/midiinput.py`` -- ``pitch_cv`` output port,
  ``bend_range`` param (default 2.0), pitch-bend state +
  lock-protected ``set_pitch_bend`` / ``snapshot_pitch_bend``,
  ``pitchwheel`` message handling, wheel reset on ``stop_midi()``.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``_render_midi_input``
  applies the bend to internal voice frequencies (one float multiply
  per voice per block) and emits the ``pitch_cv`` buffer in the
  returned dict.
- ``examples/pitch_bend.json`` -- MIDIInput (saw) -> LP filter ->
  speaker, with ``pitch_cv`` -> ``cutoff_cv`` so the wheel bends the
  notes AND sweeps the filter cutoff in lockstep. Default
  bend_range=2.0; tweak the MIDIInput's ``bend_range`` param for wider
  sweeps.
- ``tests/test_midi_input.py`` -- replaced ``test_pitchwheel_is_ignored``
  with real handler tests; added a ``TestPitchBend`` class for direct
  API (no-mido) tests; added rendering tests for ``pitch_cv`` emission,
  ``bend_range`` scaling, and verified the internal frequency actually
  shifts via zero-crossing count.

**Verified in sandbox:** 174 tests pass headless + 9 mido-skipped
(mido absent in CI). End-to-end smoke render of ``pitch_bend.json``
confirms: load round-trips, idle silent, note-on audible, ``pitch_cv``
value tracks ``bend_normalized * bend_range / 12`` to within numeric
precision.

**Mount write postmortem.** Hit a new truncation flavour today -- the
``Write`` tool itself silently capped ``midiinput.py`` at exactly the
original byte size (10613) when the new content was longer. The file
on disk ended mid-comment despite the tool reporting success.
Fall-through to the proven pattern (Python ``open('w').write(content)``
via bash heredoc) worked first try. The newly-saved mount-write
protocol memory already covers this class of failure; the lesson
reinforced is: trust nothing through the Windows mount until
``stat -c %s`` confirms the byte count.

**Counts.** Modules: 13 (unchanged). Examples: 16 -> 17. Tests:
172 -> 183 (174 passing + 9 skipped).

**Up next:** mod wheel (CC 1 -> ``mod_cv``), then channel aftertouch
(``pressure_cv``), then the backend-affine submenu refactor before the
voice-routing slice lands.

---

## 2026-05-15 (later) — Mod wheel on MIDIInput

Second of the v0.4 MIDI follow-up slices, lands hot off the heels of
pitch bend. Same shape, same threading, same demo-and-test pattern.

**Feature.** MIDIInput now emits a ``mod_cv`` output port carrying CC 1
(mod wheel) as a normalized ``[0, 1]`` CV signal scaled by a new
``mod_scale`` param (default 1.0). Unlike pitch bend, the mod wheel
does NOT bend the internal voices -- it just emits the CV. The
convention is "mod wheel is a depth knob for downstream effects": the
source publishes a clean normalized signal, the downstream consumer
(filter, LFO, amp) decides what to do with it.

**Asymmetry vs. pitch_cv.** ``pitch_cv`` emits in 1V/oct units
(semitones / 12) because its destinations all use 2**cv shaping
(oscillator freq, filter cutoff). ``mod_cv`` is raw normalized x scale
because its destinations are heterogeneous (cutoff_cv uses 2**cv,
amp_cv is linear, future depth_cv inputs would be anything). The
``mod_scale`` param lets a patch pre-scale the wheel range without
needing a generic CV-scaler module yet -- though one is on the
wishlist (`CVScale` in the new "CV utility modules" entry).

**CC 1 handler.** New ``elif msg.type == "control_change" and msg.control == 1``
branch in ``_on_message``, parallel to the existing CC 123 handler.
``msg.value / 127.0`` lands in ``[0, 1]`` directly. The CC 123 comment
now mentions mod wheel alongside pitch wheel as state that's NOT
cleared by panic (consistent with hardware semantics -- a panic message
doesn't move the physical controls).

**State.** ``self._mod_wheel: float = 0.0`` joins ``self._pitch_bend``
under ``self._lock``. Reset to 0 on ``stop_midi()`` for the same reason
-- a stale wheel value from a previous session shouldn't leak into the
next compile. The CC 123 panic still leaves it alone.

**Files added/changed:**

- ``src/pysynthrack/modules/midiinput.py`` -- ``mod_cv`` output port,
  ``mod_scale`` param (default 1.0), mod-wheel state +
  ``set_mod_wheel`` / ``snapshot_mod_wheel`` lock-protected accessors,
  CC 1 message handling, wheel reset on ``stop_midi()``. Docstrings
  and message-semantics list updated to reflect the new port and
  message handling.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``_render_midi_input``
  reads ``mod_wheel`` + ``mod_scale`` and emits the ``mod_cv``
  block-constant buffer; return dict now has four keys (``out``,
  ``gate``, ``pitch_cv``, ``mod_cv``).
- ``examples/mod_wheel_filter.json`` -- MIDIInput (saw, ``mod_scale=2.0``)
  -> LP filter (cutoff 200 Hz, resonance 2.0) -> speaker, with
  ``mod_cv`` -> ``cutoff_cv``. Wheel from 0 -> 1 sweeps the cutoff
  200 -> 800 Hz (4x under 2**cv), a satisfying two-octave brightness
  open.
- ``tests/test_midi_input.py`` -- port-list assertion expanded;
  ``TestModWheel`` class (default zero, round-trips, clamps unipolar);
  CC 1 message tests under the mido-skipif guard; render tests
  verifying ``mod_cv`` buffer shape, ``mod_scale`` scaling, default
  passthrough behaviour, and that mod wheel does NOT modulate the
  internal audio (only emits cv).

**Verified in sandbox:** 183 tests pass + 11 mido-skipped. Smoke
render of ``mod_wheel_filter.json``: filter output zero-crossing rate
doubles from closed wheel (6 per block) to open wheel (12 per block),
confirming the cutoff actually opens audibly.

**Counts.** Modules: 13 (unchanged). Examples: 17 -> 18. Tests:
183 -> 194 (183 passing + 11 skipped).

**Up next:** channel aftertouch (``pressure_cv``) -- exactly the same
shape with ``msg.type == "aftertouch"`` and ``msg.value / 127.0``,
plus a new ``pressure_scale`` param mirroring ``mod_scale``. Then
voice routing.

---

## 2026-05-15 (even later) — Channel aftertouch on MIDIInput

Third of the v0.4 MIDI follow-up slices. Genuinely was "mod wheel one
more time" -- copy-paste of the same lock/state/CV-emission pattern
with the message-type and param-name swapped. Worth flagging so future
abstractions can pick this up: pitch_cv, mod_cv, pressure_cv all share
the same "MIDI-source-publishes-normalized-scaled-CV" shape and could
collapse into a single helper if a fourth lookalike ever ships.

**Feature.** ``pressure_cv`` output port carrying channel aftertouch
(mido ``msg.type == "aftertouch"``) as ``aftertouch_normalized x
pressure_scale``. Default scale is 1.0; the demo uses 2.0 so a fully
pressed key takes a downstream ``cutoff_cv`` consumer two octaves up
under standard 2**cv shaping. Unipolar, [0, 1], same shape as mod
wheel.

**Channel vs. polyphonic aftertouch.** ``aftertouch`` in mido is
*channel* aftertouch -- one value per channel, applied identically to
every held voice. ``polytouch`` is polyphonic aftertouch -- one value
per note, the controller emits which-note + pressure separately. We
ship channel aftertouch in this slice (scalar, fits the existing CV
emission shape exactly) and defer polytouch to the voice-routing
slice (it needs per-voice CV signals to express faithfully -- this
is the smoking gun for voice-aware signals, and having
``pressure_cv`` as a scalar already in place makes it obvious where
the per-voice version slots in).

**CC 123 panic.** Now mentions aftertouch alongside pitch wheel and
mod wheel as state that's NOT cleared by the all-notes-off panic --
all three are physical-controller positions that don't reset when
notes are released. ``stop_midi()`` does reset them, since the next
compile is logically a fresh session.

**Files added/changed:**

- ``src/pysynthrack/modules/midiinput.py`` -- ``pressure_cv`` output
  port, ``pressure_scale`` param (default 1.0), aftertouch state +
  ``set_aftertouch`` / ``snapshot_aftertouch`` lock-protected
  accessors, ``aftertouch`` message handling, reset on ``stop_midi()``.
  Docstring extended; trailing "other messages ignored" comment
  updated to flag that sustain pedal (CC 64) and polyphonic
  aftertouch (``polytouch``) are now the only remaining MIDI input
  features, both blocked on voice routing.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``_render_midi_input``
  reads aftertouch + pressure_scale and emits ``pressure_cv``
  block-constant buffer. Return dict has five keys now (``out``,
  ``gate``, ``pitch_cv``, ``mod_cv``, ``pressure_cv``).
- ``examples/aftertouch_filter.json`` -- MIDIInput (square,
  ``pressure_scale=2.0``) -> LP filter (cutoff 250 Hz, resonance 3.0)
  -> speaker, with ``pressure_cv`` -> ``cutoff_cv``. Higher Q than
  the mod wheel demo so the resonance peak walks across the square
  wave's odd harmonics one by one as pressure increases -- distinctly
  more "vocal" character than the mod wheel sweep. Pressing a held
  key opens the filter.
- ``tests/test_midi_input.py`` -- outputs port-list assertion expanded
  to five entries; ``TestAftertouch`` class for direct-API tests;
  aftertouch message tests under the mido skipif; render tests for
  ``pressure_cv`` emission, scale handling, and that aftertouch only
  emits CV (does not modulate internal audio).

**Verified in sandbox:** 192 tests pass + 13 mido-skipped. Smoke
render of ``aftertouch_filter.json``: ``pressure_cv`` reads 2.0 at
full pressure with ``pressure_scale=2.0``; return dict carries all
five keys; cutoff_cv consumer gets the right value (250 Hz ->
1000 Hz cutoff at full pressure under 2**cv).

**Pattern note for refactor candidate.** Pitch_cv, mod_cv, and
pressure_cv now share three identical shape elements: (a) a
normalized state float under ``self._lock``, (b) a ``set_X`` /
``snapshot_X`` accessor pair with clamp, (c) a renderer line
``X_value = X_normalized * X_scale`` emitting a block-constant
buffer. If a fourth CV source ever lands (poly-pressure, ribbon,
breath controller), pulling out a ``_CVSource`` helper becomes
worth it. For three, it's not -- the duplication is visible and
honest, and the differences (bend_range divides by 12, pitch
applies internally) would muddy a premature abstraction.

**Counts.** Modules: 13 (unchanged). Examples: 18 -> 19. Tests:
194 -> 205 (192 passing + 13 skipped).

**Up next:** voice routing. All three scalar MIDI CV ports are in
place; the only MIDI features still pending are sustain pedal
(per-voice state, lands during voice routing) and polyphonic
aftertouch (per-voice CV signal, the textbook motivation for
voice-aware signals). Time to write the short voice routing RFC.

---

## 2026-05-15 (evening) — Error handler integrated at GUI + audio panic paths

Matthew dropped in his ``error_handler.py`` plus the QUICKSTART/GUIDE
docs. After a code review pass (it is genuinely well-built -- the
never-raises contract is real, partial-failure tracking is the killer
feature, ContextVar-scoped redactors handle thread/asyncio safety
correctly, duck-typed ExceptionGroup support works pre-3.11), wired it
into the two catch points where rich crash context actually earns its
keep.

**Integration points.**

* ``ui/app.py:main()`` -- the GUI's outermost entry. Wrapped the
  ``App().run()`` call in a ``try / except BaseException``. On any
  uncaught exception, ``describe_error(e, include_locals=True)`` runs,
  ``write_crash_report(report, source="gui")`` writes the heavy
  ``for_claude()`` output to ``~/.pysynthrack/crashes/``, the path is
  printed to stderr so the user can find the file, then the original
  exception re-raises so the normal "non-zero exit, traceback in
  terminal" behaviour is preserved. The crash file is additive, not a
  replacement.
* ``audio/numpy_backend.py:_audio_callback`` -- the realtime thread.
  Two new sticky flags on ``NumpyBackend``: ``_render_disabled`` and
  ``_crash_reported``, both reset on ``compile()``. The callback wraps
  the ``render_block`` call: on first uncaught exception it calls
  ``_handle_audio_crash`` (capture, write file, disable rendering),
  then every block after that short-circuits at the
  ``_render_disabled`` check and returns silence without re-attempting
  the broken render. Avoids the "1000 crash files per second" failure
  mode that's the obvious risk when you put rich error reporting in an
  audio callback.

**File placement.** Moved ``error_handler.py`` from the project root
to ``src/pysynthrack/error_handler.py`` so it ships with
``pip install -e .`` and is importable as
``from pysynthrack.error_handler import describe_error``. Updated the
QUICKSTART/GUIDE imports to match.

**Crash file path.** ``~/.pysynthrack/crashes/crash_<timestamp>_<source>.txt``
on every platform (``Path.home()`` resolves to ``USERPROFILE`` on
Windows, ``$HOME`` elsewhere). Filename source is sanitised to safe
chars so platform filename rules can't bite. The writer never raises
-- mkdir failure, write failure, ``for_claude()`` failure all return
``None`` and the caller continues.

**Files added/changed:**

- ``src/pysynthrack/error_handler.py`` -- MOVED from project root.
- ``src/pysynthrack/_crash.py`` (new) -- ``write_crash_report`` and
  ``crash_dir`` helpers. Leading underscore = internal-but-stable.
- ``src/pysynthrack/ui/app.py`` -- ``main()`` wrapped with crash
  reporter; on failure writes ``crash_<ts>_gui.txt`` and re-raises.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``__init__`` adds two
  sticky flags; ``compile()`` resets them; ``_audio_callback`` wraps
  the render call; new ``_handle_audio_crash`` helper.
- ``tests/test_crash.py`` (new) -- 11 tests covering filename shape,
  path round-trip, directory creation, source sanitisation,
  for_claude/str/placeholder fallback chain, unwritable-home survival,
  and an integration test that runs describe_error through the writer
  and confirms the resulting file contains the exception type.
- ``tests/test_backend_crash.py`` (new) -- 8 tests covering: callback
  returns silence on first crash, exactly one crash file per session,
  subsequent blocks short-circuit, compile resets flags, init
  defaults, crash helper survives writer failure, normal operation
  unaffected by the wrapper.
- ``error_handler_QUICKSTART.md`` / ``error_handler_GUIDE.md`` --
  import paths updated from bare ``error_handler`` to
  ``pysynthrack.error_handler`` to match the new location.

**Verified in sandbox:** 211 tests pass + 13 mido-skipped (was
192 passing). All 19 new tests green.

**Counts.** Modules: 13 (unchanged). Examples: 19 (unchanged).
Tests: 205 -> 224 (211 passing + 13 skipped).

**What this changes for users.** When the GUI hard-exits (the DPG
node-editor orphan-link bug class), the user now has a crash file
they can paste into a chat instead of squinting at a dead terminal.
When the audio thread blows up mid-DSP-experiment (NaN, shape
mismatch, port-name typo), they get a clear "audio render crashed,
silenced for the rest of this stream, report: <path>" message and a
file with full traceback + locals + caller context, instead of an
opaque PortAudio error pointing at the C extension boundary.

**What this does NOT change.** Existing ad-hoc ``try/except`` calls
in App.py (per-callback error logging, the ``_set_status`` fallback,
etc.) stay exactly as they were -- the outermost catch is the safety
net of last resort, not a replacement for inline handling. Same with
``set_param`` validation, file-dialog error reporting, recompile
guards. The error handler is opt-in heavy machinery for the catch
sites that earn it.
