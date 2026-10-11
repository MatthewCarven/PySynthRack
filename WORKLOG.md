# Worklog

Running log of decisions and progress. Newest first.

> Compacted 2026-10-11: everything before September — all of July and
> August — moved **verbatim** to [WORKLOG-ARCHIVE.md](WORKLOG-ARCHIVE.md),
> along with the old "Where things stand" snapshot (begun 2026-08-29), which
> the one below replaces. Nothing was edited, merged or dropped; grep the
> archive before assuming a decision is new. The live log now starts at
> 2026-09-11 (no entry falls between 2026-08-30 and then). The first
> compaction (2026-07-20) moved May and June the same way.

---

## Where things stand (snapshot, 2026-10-11)

A marker, not the record; the entries below are the record. Each figure
was counted at df7b46c for this snapshot, not copied from an entry.

- **Rack:** 101 module types in the registry, in seven categories:
  Sources 22, Filters & EQ 8, Effects 24, Modulation 18, Routing & VCA 7,
  CV & Utilities 14, Outputs 8. The newest is `autopan`, #101
  (2026-09-24).
- **Examples:** 165 patches (`examples/*.json`).
- **Tests:** 5470 collected. The last full run logged below is 5469
  passed, 1 skipped (2026-10-10).
- **What's open is in TODO.md**, by section: the listening checklist
  (banked 2026-09-19; the backend-split eyeball items lead it), Open
  defects, Decisions waiting on Matthew, Plan together first, Follow-ons
  and small items, the Build queue (the keep-list and the ideas backlog),
  Later / wishlist, and Standing notes. The module menu is
  docs/MODULE_IDEAS.md.
- **The newest entries, newest first:** `disk_writer` records into Music,
  a file per take if you tick it (2026-10-10); MODULE_IDEAS.md realigned
  and TODO.md consolidated (2026-10-10); the backend split, finished
  2026-09-28: 14 family mixins plus 3 shared ones and a `_RENDERERS`
  table, `numpy_backend.py` 21.8k to 2.8k lines; the app icon and the
  tooling pass (CI, ruff to zero, the Python 3.10 floor), both
  2026-09-25; the sink's NaN scrub and the sixth parallel batch, which
  brought `autopan` and put 80 moved example renders on the listening
  checklist (2026-09-24).
- **Since the last entry** (from git and TODO.md, no entry of its own):
  `ring_governor_monitor.json` ships at loop gain -4 (cc3bd06) after the
  governor eyeball passed on a shared clock. The second-clock run is
  open in TODO.md and waits on an HDMI display.

---

## 2026-10-12 — the seventh batch: eight agents, and three things found sideways

Matthew: "Pick tasks that you can run subagents in worktrees todo please
Claude". He pushed first ("git push / Everything up-to-date"), so every
worktree started at df7b46c. Eight agents, each on different files, all
landed by cherry-pick in arrival order, with no conflicts. Each landing ran
its own tests on main. The full suite and a before/after render audit
ran on the combined result.

- **Worklog tidy** (c0e6603, 62522ac): July and August moved verbatim to
  WORKLOG-ARCHIVE.md (8,487 → 2,650 lines) under a fresh snapshot;
  `error_handler_*.md` moved to docs/ (nothing referenced them).
- **`adsr` voice path** (f1e0fbd): exact across block sizes, and a voice
  row now IS mono. It used to fold the attack's last sample into decay,
  running one sample ahead. The new tests fail 26 ways on the old code.
- **Buffered sink, Phase 1** (78c8ab9): a refused block size steps down to
  blocksize 0 and says so on the node and in the status bar. A device
  that opens at no size says so in red instead of `idle`. The fake
  `sounddevice` tests caught 20 of 20 mutations. Phase 2 is Matthew's call.
- **Small fixes** (014b3bc, 303f90e, 844aa92, 4c10453, c79dd76):
  - pyo's stub list is now a constant with a registry tripwire.
  - pluck's float32 mean is fixed, and so is its overflow (beyond the
    brief: `2.0 ** 1e6` raised in the audio thread).
  - freeze `width_cv` now goes through `_finite_mean`.
  - MODULES.md's "Adding a new module" is the full how-to.
- **Keyboard / MIDI / cv_to_frequency phase** (ceaa100, 572eee6): the
  oscillator's engines became shared helpers. Beyond the brief, the
  voices' float-carried envelope ramp now counts from each slope.
- **Scope afterglow** (680fe6d, 90dd189): cheap, measured on a real
  headless DPG context (+0.05 ms per frame at 8 frames).
- **`chorus_lush`** (95f13ab, 0b49cb9): the cause was the CLOCK's float
  phase, re-wrapped per block, not the chorus. Then the LFO had the same
  fault, and the random LFO never re-rolled on a wrap at a block's first
  sample.
- **Rings + modfx rate_cv** (5c84a0f, 35e64cb): chorus and organ-scanner
  rings now keep their history (`_ring_regrow`), and flanger/phaser read
  `rate_cv` per sample. The agent corrected the brief: the buffer slider
  is disabled while running, so the reset happened on Stop → slider →
  Start.

**Found sideways, and fixed by Claude:**
1. **render_audit wrote into Matthew's real Music folder** (ae84db3).
   Yesterday's disk_writer change sends relative takes there, and the
   audit renders `record_a_take` / `mic_karaoke_recorder`. The conftest
   redirect covered the suite, not the tool. Now `render()` points the
   folder at `<out>/_recordings` for the run and restores it afterwards,
   with a test. Three junk takes from the agents' audits are in
   `~/Music/PySynthRack` (created 2026-10-12 00:02) awaiting Matthew's OK
   to delete. Lesson: **a redirect for the test suite is not a redirect
   for every tool that renders examples.**
2. **The scope's repaint cost ~100 ms per trace per frame** at 500 ms/div
   (`np.minimum.at` over 220,500 samples), on the UI thread, holding the
   GIL that the audio callback needs. `reduceat` does the same job in
   ~0.1 ms, bit-identical: pinned against the old algorithm over 8 sizes
   × 3 column counts, with NaN and ±inf (20e123a). Found by the afterglow
   agent's profiling. It may be part of Matthew's GUI-lockup audio
   freezes, which is now a hands-on check.
3. Eight agents at once drove Windows' commit memory down to 0.6 GB
   free. One agent's test run hit a `MemoryError` and passed on rerun,
   and one audit file came out corrupt. Eight is about the ceiling for
   this machine.

**Suite on the combined batch: 5759 passed, 1 skipped** (5469 before;
9.5 min).

**Render audit vs df7b46c:** 72 of 165 moved, 93 bit-identical, 1 new.
The listening block at the top of TODO.md groups them by cause, loudest
first, using `render_audit bisect` over the batch's audio commits.
Decisions for Matthew are filed in TODO: ADSR exact stage times, sink
Phase 2, the A440 edge flip, the clock's short first period, and the
afterglow fade.

---

## 2026-10-10 — disk_writer: recordings go to Music, takes can be timestamped

Matthew: "go ahead and build the disk_writer please". Spec settled the same
day: "Music folder please, and yes a browse button too".

- **`audio/recordings.py`** (new, dpg-free): a relative `path` resolves
  into `<Music>/PySynthRack/`. `<Music>` comes from `SHGetKnownFolderPath`
  with private ctypes prototypes, so a OneDrive-moved Music folder is
  honoured; the fallback is `~/Music`. Absolute paths are used as given
  and their folders are not created. `PYSYNTHRACK_RECORDINGS_DIR`
  overrides the whole folder.
- **`timestamp` param** (default OFF): each take (Start, re-arm, path
  change, or ticking the box) opens `<stem>_YYYY-MM-DD_HH-MM-SS.wav`, and
  a same-second take steps aside to `_2`. Off is one name, overwritten,
  as before.
- **The writer thread does all the filesystem work** (making folders,
  the name probe, the open); the audio callback only does path
  arithmetic. Each take is logged to `NumpyBackend.recording_log()`, and
  the status bar says `Recording -> <full path>` or `Recording FAILED -
  cannot open ...`.
- **UI:** a save-style Browse... (`.wav` filter only, since dpg's `.*`
  filter glues `.*` onto a typed name). It's rebuilt per click so it
  opens in this writer's folder with its name filled in. A pick inside
  the recordings folder is stored relative, so the patch records into
  Music on any machine. The tickbox reads "timestamp (new file per take)".
- **Found on the way:**
  1. `wave.open(name)` that fails leaves a half-built `Wave_write` whose
     `__del__` raises an unraisable `AttributeError`, which the crash
     hooks log as a crash. Every failed take would have written a bogus
     crash report. The worker now opens the file itself and hands
     `wave` the handle; a `filterwarnings("error::...Unraisable...")`
     mark is the tripwire.
  2. The examples sweep renders `record_a_take.json` and
     `mic_karaoke_recorder.json`, both armed with relative names. That's
     how `take_01.wav` and `recordingleft/right.wav` (dated 2026-09-24,
     the last full suite run) landed in the repo root. The new
     `tests/conftest.py` points the recordings folder at a session temp
     dir, and after this run `~/Music/PySynthRack` still doesn't exist.
- Tests: 22 new in `test_disk_writer.py` (30 total), covering the paths,
  stamps, uniqueness, per-take files, the log, the failure path, and the
  UI with mocked dpg. Suite **5469 passed, 1 skipped** (8.5 min, sharing
  the machine with Matthew's running app).
- **The governor eyeball got its answer the same session**
  (`ring_governor_monitor.json`, details in TODO): -4 is steady and holds
  the buffer at 50–66%, -8 is the edge (drops, inaudible), and positive
  breaks apart as it should. Open: ship the example at -4, and was that
  Realtek also the main output?

---

## 2026-10-10 — the plan and the TODO, brought back into line

Matthew: "one to inspect the plan, or what's left of it after I've thrown
around every whim … and one to consolidate the todo". Two agents in
worktrees, one file set each, cherry-picked onto main.

- **docs/MODULE_IDEAS.md realigned** (f90097b). Checked against the
  registry (101 types): 47 shipped from the menu, 5 shipped off it (the
  three speaker-output siblings, `key_trigger`, `possibility_seq`), and 49
  predate it. Nothing on it was ever declined. Open items now lead:
  `pitch_detector` and `spectrum` (spec'd), the keep-list's `midi_output`,
  subpatch containers and snapshot morph, and the quick hits. The 47
  shipped specs moved verbatim to an appendix, each with one added line
  (date, how the build differed, MODULES.md link). The **submit preamble
  was wrong** since the backend split: it sent renderers to
  `numpy_backend.py`. It now names the family mixins, the `_RENDERERS`
  row, the pyo stub, the `ui/app.py` widget block and its shared `mode`
  branch, ASCII labels, the shared CV/voice helpers and the seven
  registry-wide tripwires. I checked each name against the code.
- **TODO.md consolidated** (f4185c0): 3,032 lines → ~440, 0 ticked items
  left, 169 open. 168 items moved verbatim to TODO-ARCHIVE.md
  § "Consolidation of 2026-10-10" under their old section headings. Of
  those, 155 were already done, 11 were Matthew's 2026-10-05 inline
  listening notes ("done love the marimbra" and others), now recorded
  in his words as EARS PASSED, and 2 were duplicates. Nothing was
  archived as "unwanted" on inference. The ones that look stale are
  listed for his call in the session report, not moved. Six open
  threads were lifted out of archived entries ("Hoisted 2026-10-10"). His
  `granular_haze` question ("same as granular haze but different
  samples?") stays open in the checklist, unanswered.
- **Checked twice:** each agent ran its own line-multiset check, and I
  re-ran an independent one on the TODO pair. Every non-blank line from
  before is still present except the deliberate edits (open → ticked,
  `##` → `###` under the archive section, the rewritten verdict lines,
  one repointed intro line). The old archive survives intact and in order.
- **Found on the way:** README said the matrix mixer is 8×8; it's 4×4
  (0bd7a14). The backend still prefers pyo when it is installed. That's
  documented in README's "Forcing a backend", but it's a trap now that
  pyo is parked, so it's filed as a decision. pyo's silent-stub notice
  misses `freeze` and `function_generator`, and MODULES.md's status note
  and "Adding a new module" have drifted. All three are in TODO.
- **Matthew's calls, the same day:**
  - `sequential_switch` dropped: it's patchable (selector → adsr → vca
    → mixer).
  - `looper` kept: nothing layers live audio.
  - Subpatch containers and snapshot morph are off the module menu.
    Subpatch goes to a new TODO § "Plan together first", alongside
    off-thread rendering and host-API selection, which he wants to plan
    in a session because his desktop's GUI lockups freeze the output.
  - Scope afterglow revived as an opt-in, since the estimate says it's
    cheap.
  - `disk_writer` un-parked, with a timestamp-filename tickbox.
  - `midi_output` is wanted for MIDI-based patches.
  - The epoch-wrap item got a plain-English explanation and stays, low
    priority.
  - He's running the governor-patch eyeball now.

---

## 2026-09-28 — the backend split's endgame: helpers and the dispatch table

Matthew: "tie up the rest of the backend split". Every helper left on the
class moved. First I mapped who uses each one (which mixins, the engine,
and anything outside the audio package).

- **One family's helpers joined that family:** the oscillator phase carry,
  the sampler and convolver lifecycle hooks, `_freeze_gate_row`,
  `_MAX_VOICES` and the disk-writer thread.
- **Helpers several families share went to three underscore mixins:**
  `_shared` (input lookup, gates, voice collapse, CV maths, envelope
  follower, biquads, distortion curve), `_waveshapes` (the
  BLEP/wavetable core) and `_media` (path resolution and decoding).

The tool gained an append mode for adding to an existing mixin. Blocks
anchored on a decorated `def` now carry its decorators with them, which
removes the "can't anchor on a bare `@staticmethod`" leftovers. `EOF`
ends a block at the end of the file.

**Lesson:** moves applied in sequence eat each other's anchors. The last
two configs ended on members an earlier move had taken, and `move`
refused cleanly. The shared block was one contiguous run by then.

`_render_module`'s 93-branch if-chain is now a `_RENDERERS` table
(TYPE → method name) plus a `_FRAMES_ONLY` set, generated from the chain
rather than retyped. `tests/test_render_dispatch.py` checks the table
against the module registry and each renderer's signature, so a new
module that forgets its entry now fails a test instead of rendering
silence.

**Proof:**

- All 101 of HEAD's backend members are byte-identical where they now
  live, except 5 that gained the lazy imports.
- Decorators: 294/294.
- Full suite: 5429 passed.
- render_audit: 165/165 identical, after the moves and again after the
  table.

`numpy_backend.py` now holds only the engine plus the module-level DSP
kernels that tests import by name: 2.8k lines, down from 21.8k.

## 2026-09-28 — the backend split, finished with five agents

Matthew: "keep going on the backend split" then "fire up some subagents
... 5 for about an hour". The file couldn't take five editors at once, so
the work split in two: each agent took two families of the plan in its
OWN worktree -- found the exact blocks, what's shared, the subs -- applied
them there with the tool and ran that family's tests (with
`PYTHONPATH=src`, since the editable install points at the main
checkout), then handed back an `expect`-guarded config. I applied the ten
configs in sequence on the branch: each pair a commit, each checked for
decorators (294/294 unchanged vs before the split began), missing
imports (AST over tests/src/tools), and its targeted tests.

The agents found two real bugs in my tool, both fixed and guarded: ruff
wraps `from .renderers import (...)` past 100 columns and the one-line
regex then misparsed it (the next move would have written a broken
import); and an end anchor on a decorated `def` line carries the
`@staticmethod` away to decorate the wrong method -- invisible to ruff and
to a text diff. `move` now reads/writes both forms and refuses a boundary
between a decorator and its def. The `expect` guard caught one real
drift while applying: with slew gone, a cv_routing block ran on into the
shared `_crossover_coeffs`; refused, anchor tightened.

Result: 14 mixins; `numpy_backend.py` 19.0k -> 4.2k lines (21.8k before
the split), 77 methods left, all engine or shared helpers. Full suite
5429 passed; render_audit of all 165 examples vs the pre-session commit
moved 0, both after six families and at the end.

## 2026-09-26 — backend split: dynamics, and a tool for the rest

Matthew: "keep going on the backend split". Dynamics went second:
compressor, limiter, noise gate and transient shaper, 671 lines, 13
methods plus their 11 class constants -- called only from
`_render_module`. One wrinkle: the two static sidechain key-align helpers
call `NumpyBackend._voice_mean` BY NAME, and the mixin can't import the
backend at top level (the backend imports it). A lazy import sits in the
one fallback branch that needs it (a key whose voice count matches
neither `in` nor mono), which no test reached -- so one now does, for
both helpers. Checks: no class-level name on both sides (a clash would
let the backend's copy win silently), the block diff is exactly the two
lazy imports, `render_audit` HEAD (rendered from a git worktree via
`--repo`, not a stash this time) vs working tree over all 165 examples.

The mechanics are now `tools/split_renderers.py`: `analyse` prints what
a block would move, what it needs imported and who calls into it; `move`
does the cut from a JSON config whose `subs` are the only edits allowed
inside the block. Validated by replaying the dynamics move in a
throwaway worktree: byte-identical to the hand move on all three files.

*Later the same day: mod-FX.* Chorus, rotary, flanger and phaser plus the
shared clock-sync helpers -- not contiguous (chorus sits inside the reverb
section, granular between rotary and the flanger), so the tool grew
multi-block moves. Its analysis also showed a blind spot: constants
assigned by tuple unpacking (`_ROT_HORN_UP, _ROT_HORN_DOWN = 1.0, 1.5`)
weren't counted as members, which would have hidden a clash; fixed, and
`move` now refuses a clash itself -- proven by planting a duplicate
`_ROT_WRAP` on the backend in a throwaway worktree (refused, nothing
written). `autopan` stays in the backend and reaches `_mod_clock_sync`
and `_MOD_DIV_*` through `self`. Diff vs HEAD: one import gains a dot.
490 targeted tests, then render_audit and the full suite.

*Then reverb + delay* (533 lines, three blocks: delay's constants sit
before the vocoder and its renderers after it). The reverb section also
held `_CHORUS_MAX_MS`, a chorus constant -- dropped from the moved text by
a `sub` and added beside `_CHORUS_PH_BITS` in modfx, so it's defined once
where it's used. The block diff vs HEAD is exactly that line.

## 2026-09-25 — the app icon

Matthew spotted the 🎛️ emoji, then made his own: a screenshot of the rack
(`packaging/icon_source.png`, 774×793). Downscaled previews showed the
whole window reads at 256/128 and is grey mush at 32/16, so the `.ico`
carries different art per size (`tools/make_icon.py`, rerunnable): the
canvas cropped past the chrome at 256/128, a tighter crop on the osc and
LFOs at 64/48, and a drawn glyph at 32/24/16 -- two nodes (one with the
selected blue title bar), a cable between yellow jacks, colours sampled
from the screenshot. First glyph draft had the nodes touching, which
crushed the cable into a zigzag; spreading them fixed it. Wired into both
specs (`icon=` + bundled as data) and the viewport's `small_icon` /
`large_icon` via `_resources.app_icon()` (returns `None` if absent, so the
app never depends on it). 2 tests read the ICO header directly. Needs a
real Windows build to see it in the taskbar.

## 2026-09-25 — the tooling love pass

Matthew asked where the project could use love, then "Lets go with your
suggestions". Health check first: every test green, so the work was in
the tooling around the code.

*The suite needed the GUI to run at all.* In a fresh `.[dev]` environment
(no DearPyGui) 9 test files failed to collect -- `ui/__init__.py` imported
`app.py`, which imports dpg, so even `ui.zoom` needed it -- and pytest
aborts the whole run on a collection error. A module `__getattr__` makes
`App`/`main` lazy; `test_zoom_key_debounce` now importorskips like its
siblings (its docstring already said it would).

*CI* didn't exist. `.github/workflows/ci.yml`: ruff, then pytest `-n auto`
on 3.10 and 3.12 with `[dev,gui]`; `libportaudio2` because sounddevice
won't import without the library. xdist verified safe on the full suite.

*ruff 494 → 0.* Decision: E702 (`a = x; b = y`), E731, E741 are house
style, so ignored in config rather than rewritten; B905 too (`zip(strict=)`
changes behaviour). The rest fixed. Two things worth remembering: an
"unused" import removed by `--fix` was a re-export the backend imports
lazily (`POSSIBILITY_MODES` from `possibility_selector`) -- 22 tests broke
and only a second full run caught it; and one unused local was a weak
test (`test_limiter` computed the ceiling, asserted `red0 > 0.3`; the
reduction is 1 - C to 6e-5, now pinned). `test_error_handler.py` is kept
a clean upstream copy via per-file ignores.

*Python floor 3.9 → 3.10* (deviation from "just fix lint", noted here):
3.9 is EOL, and error_handler's suggestions read `NameError.name` /
`AttributeError.obj`, which 3.9 doesn't have. Measured: 3.10 5416 passed,
3.11 5426 passed, 0 failed.

*`clock_divider` -- the last documented residue.* Clock swing 0.5 +
divider swing 0.5 on `n` 1: intervals alternate 1.5P / 0.5P, so a late
gate (0.75P on) lands after the next edge, and `pulse()` superseded any
event scheduled at or after a new one -- 47 of 96 gates dropped. Now an
event two or more samples later is kept and the new gate is cut to end a
sample before it: the swing offset stays a position, every gate its own
rising edge. Census (real 8 Hz clock, swing 0/0.3/0.5 × pw 0.5–0.95 ×
divider swing 0/0.3/0.5 × n 1/3/4/5): 188 missing of 6192 → 0, no rise
moved > 1 sample, `mult` untouched; 54-case bit-compare differs only in
the crossing case; `render_audit` over the nine divider examples: moved 0.
New test fails without the fix. Musically the pair lands a quarter period
apart -- a flam, which is what those settings ask for; worth a listen if
Matthew ever runs both swings that high.

*The split, started.* Mixins in `audio/renderers/`: a family's renderers
move verbatim into a class `NumpyBackend` inherits, so every `self.` and
every `NumpyBackend._render_x` (tests call and monkeypatch these) resolves
unchanged. Clockwork went first -- 587 lines, dependencies just `np` and
four `self` attributes, one lazy import one dot deeper. Proof: a textual
diff of the moved block against HEAD (only that import differs), MRO
resolution checked, `render_audit` over all 165 examples moved 0. One
trap found before it bit: `test_every_voice_collapse_goes_through_a_door`
scanned only `numpy_backend.py`'s source, so moved code would have escaped
it silently; it now scans the whole package (and asserts it found the
renderer modules), and a planted raw `.sum(axis=0)` in the moved file
fails it.

**Next:** the dynamics family, then the mod-FX, per the TODO.

## 2026-09-24 — every changed example on the listening checklist

Matthew: "throw every changed example on my todo please ... and I'll start
testing". The sixth batch's checklist named the loud movers by cause but
summarised the rest as "~55 rounding-only"; he asked for all of them. The
audit's renders hadn't been kept, so it was re-run: `render_audit.py
render` at 9ec4462 (the commit before the batch, via a throwaway detached
worktree) twice, and at HEAD (which includes the NaN fix, 3bf0be4), then
`compare --noise`. Result: **80 moved, 84 bit-identical, 1 new, no
example nondeterministic**; the NaN fix moved nothing, as designed. Same
figures as the batch's own audit.

The checklist's sixth-batch block is now one checkbox per example, eight
groups loudest-first with each one's dB: the new module; 17 oscillator-edge
movers (should sound identical); 11 pluck `carry` movers; chorus_lush; 3
below hearing (-88..-95 dB); 48 rounding-only (-131 dB and quieter, listed
anyway because he asked for every one); the 25 silent-offline examples that
contain a touched module (need his keys/MIDI/mic; the 4 silent ones with no
touched module are left off); and the behaviour checks, plus an EYES item
for today's NaN status line. Causes are attributed by which touched module
types each example contains (a heuristic, not a fresh bisect; it matches
the causes the batch's own audit named).

---

## 2026-09-24 — the sink scrubs NaN before it clips

The top follow-on from the sixth batch, on Matthew's yes. The output stage
ended in `np.clip(out, -1.0, 1.0)` for the master bus and each routed
device bus — and `np.clip` passes NaN straight through and turns +/-inf
into a full-scale sample. So a module blowing up anywhere upstream handed
the audio device garbage. The feedback door already scrubbed what a loop
feeds BACK, but the block a loop blew up in still flowed FORWARD to the
speaker unscrubbed; the test that proves the fix reproduces exactly that
(a gain-2 bare mixer loop, 300 blocks).

**The fix** (`render_block_multi`): for each bus, if any sample is
non-finite, `nan_to_num` it in place (nan/inf/-inf all to 0 — silence is
the only safe value for a sample that is wrong, same convention as the
door), then clip. Only the bad samples change; a finite block takes one
`isfinite().all()` and is otherwise untouched, so no example render moved
(the examples sweep is green). One increment of `_sink_nonfinite` per
render that scrubbed anything; `sink_scrubs()` is the lock-free
observable.

**Saying so** (the media-path lesson — a subsystem that must fail soft
needs something else to say it failed): `App._update_sink_scrubs`, driven
from the DSP-load tick, posts a status line whenever the count rises
while running ("Output: N block(s) carried NaN/inf and were silenced
before the speakers ... A CV meter on the chain reads nan where it
starts" — the CV meter learned to paint `nan` in the sixth batch). Silent
while the count holds; restarts from zero for a fresh backend.

**Design calls:** per-sample zeroing, not muting the whole block (a lone
NaN shouldn't cost 10 ms of audio, and the tests pin that the rest of the
block is bit-identical); a status line rather than a new toolbar slot
(it's a fault report, not a running readout — the `loops !K` slot shows
the door's scrubs because those belong to a visible cable).

15 tests: 9 in `tests/test_sink_scrub.py` (master + routed bus, blocks
not samples, block sizes 64/1000, the runaway loop) and 6 in
`test_late_cable_ui.py::TestSinkScrubStatus` (posts on rise only, ASCII,
driven by the tick, absent-observable safe). Self-tested by removing the
`nan_to_num`: 5 of the 9 fail (the 4 that pass are the clean and counting
ones). Suite **5436 passed, 1 skipped** — 7 min 16 s this run against the
usual ~90 s; probably a busy machine — the change adds one isfinite() per bus per block.

---

## 2026-09-24 — nine follow-ons in parallel (the sixth batch)

Matthew, planning a full day of re-listening: "feel free to invalidate
previous examples … I don't want a previous position to hold us back
here, we can just reinvent whatever fails". That unlocked the part of
the follow-on list parked precisely because each item moved shipped
renders and wanted its own reference-render pass. Nine agents ("all
nine"), pushed first, cherry-picked in landing order with no textual
conflicts. Two SEMANTIC conflicts surfaced only at the merge, both in
the new module: `autopan` had been written against `_mod_clock_sync`'s
old `(rate, phase0)` signature, which the flanger/phaser pass changed to
take the caller's free-running phase line (c5467df), and it collapsed a
poly `pan_cv` with a raw `.mean(axis=0)`, which the reductions pass's
new tripwire rejects (3c3c2c0). **Lesson: parallel agents that share a
helper can each be green alone and wrong together — the full suite on
the merged main is where that shows, and it did.** Suite **5261 →
5417**, 165 examples, **101 modules**.

**The audit.** Before launching, every example was rendered twice (8 s,
block 512) — all 164 identical run to run, so any later movement is the
batch's. After merging: 84 bit-identical, ~55 moved only at rounding
level, the rest attributed commit by commit (the big movers rendered at
each of the eleven steps). Every large unexplained move was the
oscillator's: naive saw/square at integer-Hz pitches, whose exact wrap
sample the old accumulator put one ulp on the wrong side of the edge
every time — sparse (< 1.2% of samples above 1e-2), spread by any filter
downstream, and the new placement is the correct one. 29 examples
render silent offline because they wait for a key, MIDI or the mic.

What landed (details in TODO § "Nine follow-ons in parallel"):
* **chorus `rate_cv` per sample** (5c65bb1) — integrated on an int64
  grid: a wrapped FLOAT running sum is never partition-independent.
* **pluck `carry` default ON** (65b3a4f) — re-plucks are superposition.
* **voice collapses in float64** (f36977e) — order-independence, 24
  sites + tripwire; `freeze.pitch_cv`; the CV meter says `nan`, and a
  sticky NaN auto-range bug fixed on the way.
* **`autopan`, module #101** (73047ce spec, dd0c161) — the keep-list's
  panner; a triangle's float32 output hides a phase accumulator, so the
  float64 anchor got its own pin.
* **rotary** (96bbd15) — three bugs, incl. a ring that wiped itself and
  the rotors on any block-size change (the buffer slider!). The organ
  scanner was already exact.
* **clock_divider** (83ae23c) — gates never merge (2519 + 2256 → 0).
* **freeze** (e5dd7c2) — relative −120 dB floor; fixed a one-ulp
  knife-edge that swapped half the partials between L and R; 65536 back
  on the knob with a staged 93 ms birth.
* **delay / flanger / phaser** (f345a5e, fe3f58c) — five mechanisms; the
  flanger CUT its deepest sweep at block 64. Pins compare internal
  float64 state: the delay's drift touched 1 output sample and 106817 of
  its line.
* **oscillator** (02f3916) — integer-count phase, the organ's numbers;
  168/204 → 0/204.

**Deviation noted (standing principle):** the pluck `carry` flip and the
bowed/wind-style "moved above ulp" renders were ears decisions; Matthew's
"invalidate previous examples" made them his re-listen's, not a
blocker. The two EARS decisions that remain are on the checklist
(bowed/wind A/B; the new `_wt` per-voice band question).

## 2026-09-22 — eleven follow-ons in parallel (the fifth batch)

Matthew, after a lost afternoon: "Pick another 10 or items from the todo
for existing modules and lets run a few together ok please Claude?" —
eleven agents in the end (the phaser and flanger travel as a pair),
every item lifted from the TODO's own follow-on and finding lists.
Everything was pushed first, so the worktrees branched from HEAD.
Cherry-picked in landing order; four conflicts, all trivial (adjacent
import lines, adjacent CV-depth rows, appendix bullets — both sides
kept each time) except one that needed care: two agents rewrote the
same `rate_cv` block in the phaser and flanger, one adding the house
`_finite_mean`, the other moving the block below the clamps — resolved
by taking the move and re-applying the fix. Full suite on the merged
main: **5265 passed**, 164 examples. Eight examples banked. Mid-batch
Matthew offered the last agent an out — "if it wants to just write a
plan for the next pass that is cool too" — which was relayed; it did
not need it.

**The theme of the batch: a finding is usually more than one bug.** Two
of the three exactness items named one mechanism and turned out to have
three. The tape's "ulp at rare samples" was (1) the ring read
(`absidx - delay` rounds at the index's magnitude, and the ring length
depends on the block size — the whole of the drift path's error), (2)
the wow/flutter phase accumulators (`ph + frames*inc` rounds once a
block, so partitions part within a second), and (3) **scipy**:
`lfilter` short-circuits `len(a) == 1` to `np.convolve` plus a separate
`+= zi`, splitting each 65-tap sum at the block boundary — so the
shared 4x oversampler was never block-exact. Fixed by carrying raw tail
samples, exact at 1.00x the cost (forcing scipy's recursive path was
also exact but 2.7x slower). The chorus had it worse — 487 differing
samples — while its docstring claimed exactness outright. The organ's
"phase accumulator" was likewise three: the phase, the percussion
(which tested its 1e-6 cut-off once per pour, so the strike's tail died
on a different sample at every block size — the LARGEST of the three at
9.7e-7), and a float32 block-mean making a held pitch itself
block-dependent.

**What landed** (each with tests, docs, reference renders bit-exact at
the default unless stated):

* **noise `corner`** (53c9728) — brown's leak becomes a knob (2..40 Hz).
  The slope stays −6.00 dB/oct at every corner while the 2–20 Hz energy
  moves ~15 dB. Unasked-for and right: a live corner change BANGS (a 4x
  peak) unless the carried integrator state is renormalised by
  `scale_old/scale_new`; it cannot fire while the knob stands still, so
  the default stays bit-exact. `noise_stereo_pair.json` banked.
* **the patch loader** (357d99b) — dead cables now DROP and SAY SO:
  `Patch.cable_problem` checks what `connect` checks, the survivors
  load, the casualties land on `patch.load_warnings` → the GUI status
  line + console, the CLI's stderr. All 156 examples clean; 24 re-render
  bit-exact. Duplicate destinations are deliberately NOT policed —
  picking a winner would change how an existing patch plays. Known
  one-way consequence: open a drifted patch, save, and the dead cable is
  gone from the file.
* **cv_recorder `speed_cv` + one-shot** (9a8e5cf) — the rate jack ships
  STEPPED (0.5x–4x in 0.25x notches) on purpose: an integer half-step is
  what makes every rate identical at block 50 and 250. It also caught
  the spec's arithmetic (1..8 half-steps is 0.5x–4x, not the 0.25x–2x I
  wrote). `cv_recorder_oneshot.json` banked.
* **the CV sweep** (6307908) — one `_finite_mean` helper, 39 call sites.
  **Fifteen modules rendered differently at 64 than at 512 with nothing
  but a steady CV on a jack** (a float32 mean of a float32 buffer reads
  a held 0.3 as 0.29999998 over 64 samples, 0.30000001 over 512); all
  fifteen are exact now. The NaN wart is closed before any clamp —
  `loudness` genuinely rendered non-finite on a NaN CV, `bitcrusher`
  raised out of the audio thread. 120 of 156 examples bit-exact; the 36
  that moved all patch a routed CV, 34 by an ulp. The two exceptions are
  the chaotic models amplifying that ulp: **`bowed_cello` 5.2e-2 and
  `wind_duet` 1.0e-4** — both were NEVER block-size exact before and are
  now, so the new render is the exact one; flagged for ears.
* **freeze `width_cv` + `latch` + 32768** (c7ff869) — stopped at 32768,
  not 65536, and showed its working: at 65536 the steady-state synthesis
  is 11.2 ms every 371 ms against an 11.61 ms block, a permanent
  near-overrun. Half that bill was paid by vectorising the peak-region
  map (integer-identical, fuzzed over 4025 spectra, 6 ms → 0.33 ms),
  which speeds up every window. It rejected a cheaper `width_cv` design
  because it would step at block boundaries, and corrected its own
  hypothesis: the capture window is a Hann, so the instant AT the gate
  edge is the quietest part of what you hold — aim the edge a beat late.
* **vowel: the follow-on list CLOSED** (845ea1c) — float64 mean,
  `cv_rate` `block|sample`, `voice` `custom` + `f1..f5`. The per-sample
  mouth quantises the CV to 0.02-vowel steps AROUND THE KNOB (measured
  against a true per-sample oracle: 0.02 → −62 dB, 0.05 → −55, 0.1 → −43
  and steppy), so flipping `cv_rate` on a still patch is bit-for-bit
  silent. Its click tripwire calibrates against BLOCK mode rather than a
  magic number — and found block mode's once-a-buffer coefficient jump
  is the bigger discontinuity (0.00498 vs 0.00367).
* **tape + chorus exactness** (063e7d5, 3f161fa) — above. Both bit-exact
  at 64/128/512/1000 over four seconds with everything on; shipped
  renders moved at most HALF a float32 ulp on 0.006% of samples.
* **clock `swing_cv` + the divider's gate lengths** (d89dadc, d54912a) —
  the swing is LATCHED at each even period start and held through the
  odd period after it, so a moving CV never drags a pulse already in
  flight into a double edge or none (a 0.5 Hz LFO over 10 s emits
  exactly the straight clock's 80 edges at every depth). The divider
  took its gate length from the LAST interval, and a swung clock
  alternates long/short — `divn` flapped 10750/5787 samples and
  `div2/4/8`, always landing on even pulses, always measured the SHORT
  side and sat ~30% under their true period. Lengths come off the mean
  of the last two intervals now; even a straight clock's 2-sample
  rounding wobble flattens. One shipped render moves
  (`cv_recorder_backwards` reads divider gates as levels, 1.5e-4 peak);
  every rising edge in all 22 references is identical.
* **organ: the finding CLOSED** (4517234, 5b54a64, a3894c4) — above,
  plus the scanner's rounded-triangle sweep (`arcsin(0.98 sin)/arcsin
  0.98`: crest 1.12, third harmonic 0.287 against the sine's 1.44/0.000,
  A/B'd against the same renderer with the rounding off) and a scanner
  phase that survives off → on. The swing table was retuned 1.4x wider
  to hold V3 at 41.8 cents, because the cents are the contract.
* **pluck `vel_position` + `carry`** (d35bd0c, 4675958) — and the open
  allpass-clear QUESTION, answered: the clear every hit performs steps
  the output by **51% of the ring** (it throws away one sample of loop
  state, and a sample is not an envelope), while carrying it makes the
  step exactly 0 and a re-pluck **exact superposition** — two hits minus
  one equals the second alone to one float32 ulp, the property the
  module always claimed and the clear made false. Tuning, peak and
  stability identical. Shipped default OFF anyway: turning it on changes
  every re-pluck, which is an ears call. `vel_position` corrected the
  spec twice (`position` 0 is the comb's documented OFF and my formula
  would have switched it back on; and the centroid claim doesn't hold —
  a comb notches rather than tilts, so it reported the first null
  falling 2597 → 686 Hz at velocity 0.3).
* **phaser + flanger** (af3ce58, 19b1724) — the rack's two untouched
  modulation effects: a `clock` jack with `division` (the sweep becomes
  a length in beats, 1.0007/0.5003/0.2502 Hz measured against
  1/0.5/0.25), `spread` (the L/R offset the quarter-cycle quadrature had
  been hard-coding — 0.5 IS that, and `0.5*0.5` is exact in binary, so
  the default is bit-exactly today), and `manual_cv` + depth. The find:
  a rate re-read once a block put **5.6e-2** between the 64- and
  512-sample renders — audible — because the sweep rides a float phase
  accumulator; keying the LOCKED phase to the absolute sample index
  makes a clocked sweep exactly bit-identical, better than the
  free-running one. Two of the briefed six features already existed
  (the flanger's through-zero, the phaser's `center` as its manual
  knob); it said so and shipped what was actually missing.

**Eight examples banked:** `noise_stereo_pair`, `cv_recorder_oneshot`,
`freeze_drone_breathe`, `vowel_robot_talk`, `clock_swing_breathe`,
`pluck_touch`, `phaser_envelope_sweep`, `flanger_jet`. **Thirty-six
love passes in four days.** Suite **5265**, 164 examples, 100 modules.

---

## 2026-09-20 — ten follow-ons in parallel (the fourth batch)

Matthew: "Pick another 10 or items from the todo for existing modules
and lets run a few together ok please Claude?" — and he pushed first,
so this batch's worktrees branched from main's HEAD (d557ea7) and the
import-line conflict of batch 3 never happened. Ten agents at once,
picked from the follow-on lists: nine love passes and one cross-cutting
fix. All ten landed; cherry-picked in landing order; three conflicts
(two new helpers inserted at the same spot beside `_gate_ramp_env`,
adjacent index rows, appendix bullets — both kept each time). Full
suite on the merged main: **4536 passed**, 156 examples. Eight
examples banked. ~30 min wall-clock, ~2M tokens.

**What landed** (each with tests, docs, reference renders bit-exact at
the default):

* **vowel `formant` + `formant_cv`** (8f3bdc5) — the child/giant knob,
  constant-Q (bandwidths scale with the frequencies: "the same vowel, a
  different throat"). A's peak 342 / 661 / 1338 Hz at −12 / 0 / +12;
  Q 8.06 → 8.18 while the bandwidth doubles. **Finding:** `np.mean` of
  a float32 buffer accumulates in float32, so a CONSTANT cv of 0.3
  differs by an ulp between 64 and 512 samples; the new path averages
  in float64; `vowel_cv`'s own mean is a follow-on (it moves renders).
* **pluck `vel_color`** (e7eb4b5) — soft hits duller: `color +
  vel_color·(vel − 1)`, anchored at vel 1 so the knob is inert without a
  velocity source. Centroid 3540 vs 5782 Hz at vel 0.3 vs 1.0. The
  agent sabotaged its own branch: 9 of 11 new tests bite.
* **lfo `seed`** (9b440f1) — a seeded `random` that a `reset` replays
  (the sequencer's precedent): the same random wah every bar. Per-voice
  COPIES of one generator, because a shared stream drawn voice-major
  is block-size dependent. 19 reference renders bit-exact.
* **sample_hold `prob_cv`** (650f4e5) — the chance read AT THE EDGE'S
  OWN SAMPLE, not a block mean (a block mean would make the verdict
  depend on where the buffer boundaries fall); `prob` 0.5 + cv 0.5 IS
  `prob` 1.0 draw for draw. Lesson: a prefix match on a param name is
  not a match on the widget (`prob` matched `prob_cv_depth` first).
* **clock `swing`** (85c3eef) — every second pulse late by `swing` of
  the CURRENT period (so `bpm_cv` swings with the tempo for free);
  even pulses bit-identical to the straight clock; a late pulse is cut
  a sample before the next downbeat, never swallowing it. 8 Hz / 0.33:
  odd edges exactly 1819 late. The divider's `/4` sits on even pulses
  only. 18 reference renders bit-exact.
* **the CV-overflow audit** (a1b2530) — `_pow2_clipped` beside
  `_gate_ramp_env`, 23 call sites (filter cutoff ×4, crossover,
  sweep_eq, motion_eq, chorus/flanger/phaser/drift rate, bitcrusher,
  bowed/wind/organ/modal pitch, and the sampler's pure-Python
  `playback_rate`, which really did raise at 1e6). **Finding:** three
  "already clamped" paths read NaN as a RAIL — Python's `min`/`max`
  don't propagate NaN, they hand back whichever bound sits first
  (function_generator read −5 oct, pitch_shifter +36 st) — scrub before
  you clamp. 46 reference renders bit-exact; 108 tests, self-tested by
  swapping the bare power back (15 of 19 raise).
* **sequencer `reverse` gate** (d1c13a6) — a per-edge flip, not a mode;
  the pendulum's heading stays in the base frame (XOR the gate in and
  out) so dropping the gate turns it around rather than leaping; random
  untouched (same draw). `fader_seq` got the jack by hand and now pins
  its full port list. 24 reference renders bit-exact. Lesson: two
  float-phase clocks at related tempos race at coincident edges — the
  example's reverse gate is an LFO square through a schmitt, offset
  half an eighth before the bar line.
* **reverb + delay `freeze` tickbox** (5a094f8, 30b8bb4) — one
  `_freeze_gate_row` helper: cable OR tick, and None when neither is
  live so the pre-freeze code runs by construction. Ticking at block k
  is bit-for-bit a cable rising there. 33 reference renders bit-exact.
* **freeze `width` + `decay`** (ce8ca65) — the hundredth's first love
  pass, and a spec corrected by measurement: my random per-bin phase
  scatter moves partials up to 16 dB (a lobe spans four bins — that is
  what `smear` does). The agent shipped a deterministic QUADRATURE
  scatter per peak region: corr(L, R) = cos(width·π/2) exactly (0.924 /
  0.707 / 0.383 / 0.000 at 0.25 / 0.5 / 0.75 / 1), every partial at the
  mono's level, the mono fold a fixed −3.01 dB. `decay` = 10^(−3t/decay)
  from the integer count since birth, dropped at −90 dB; the "−60 dB at
  2 s vs 0.2 s" hunch was −54 (0.2 s in, the layer is already 6 dB
  down) — pin the law against the forever render.
* **cv_recorder `play`, `reverse`, `speed`** (69f3c4e) — the head is
  now an integer in HALF-samples (0.5x / 1x / 2x = 1 / 2 / 4 steps,
  reverse = the sign), so every speed is bit-exact across block sizes;
  a stopped head writes nothing; record at 1x, play at any; a reversed
  take comes back time-reversed once released. A pure-Python
  old-engine reference carries the bit-exact pin. Lesson: the loop is
  created on the clock's SECOND tick, so a divider counting from tick 0
  is a sixteenth off forever — reset the dividers inside the take.

**Eight examples banked:** `vowel_giant_child`, `pluck_velocity_color`,
`lfo_random_replay`, `sample_hold_prob_sweep`, `clock_swing`,
`sequencer_reverse_bars`, `freeze_wide_wash`, `cv_recorder_backwards`.
Twenty-five love passes in twenty-four hours. Suite **4536**, 156
examples, 100 modules.

---

## 2026-09-20 — freeze: the hundredth module

Matthew: "freeze please Claude - #100 like :-{D … Congratulations
Claude #100!" — the keep-list's spectral freeze, picked as the
hundredth module, in the small hours after the third love-pass batch.
Spec into MODULE_IDEAS first (§ Character & space), then three scratch
prototypes before a line of the module — and the third one changed the
design.

**What it is.** The rack already had a freeze that loops TIME
(`granular` re-reads a slice of its ring: a rhythm, a stutter, a
cloud). This one holds a SPECTRUM: at the gate's rising edge it takes
the last `size` samples, measures the frequencies present and their
levels, and re-synthesises that spectrum forever with every partial's
phase advancing at its own true rate — a chord becomes a stationary pad
with no loop seam and no rhythm in it. The Freeze pedal; Clouds'
spectral mode. `dry` and `level` instead of a `mix`, because a freeze
is something you play OVER — engaging it must never duck the dry.
`smear` 0 → 1 slides from the glassy coherent hold to the random-phase
wash; `pitch` + `pitch_cv` transpose the layer; `fade` is the rise, the
fall, and the crossfade between two holds (a re-freeze melts, never
cuts — a new layer captures, the old eases out from its current level;
four may sound at once); the `freeze` tickbox ORs with the gate.

**How it's built, and what the prototypes found.** (1) The
phase-vocoder capture — two Hann frames a hop apart, magnitudes from
the later, true frequency per bin from the pair's phase difference —
holds a non-bin sine at its real pitch with no beating (441.30 Hz at
0.500, first time). (2) Pitch by resampling the SPECTRUM widens every
lobe and loses ~4 dB an octave up (0.29 of 0.50 at +12), so pitch is a
change of READ RATE on the frozen stream, which is stationary — exact
frequency, unity level (659.26 / 220.00 / 880.00 at 0.500). (3) The one
that mattered: a C-E-G triad at a 4096 window held within 2% at first
and had lost two thirds of two partials by 10 s — the bins one
partial's lobe shares with a neighbour carry a frequency of their own
and dephase over seconds; the hold eats itself. **Identity phase
locking** (Laroche & Dolson: every bin in a peak's region of influence
advances at the peak's frequency) fixes it completely: the same triad
holds within 2% at 2 s and 10 s, even a 2048 window holds it within 6%,
and a lone sine is unchanged. What is left of the resolution rule is
honest: partials closer than ~3 bins merge into one lobe and hold as
a single tone at their mean (a minor second at C3 needs 16384). Then
the read starts at frozen time `size` = the edge itself, so a
stationary source's hold is its own continuation IN PHASE (dry +
frozen of the same sine = exactly 2×, pinned). Frames are generated in
order on demand into a rolling buffer; the read position is an integer
count of output samples times the ratio, rebased on a ratio change;
each layer has its own integer-count `_gate_ramp_env` — so the whole
thing is bit-exact 64 vs 512 vs 1000 with edges mid-stream at pitch +7
and smear 0.5, and a knob turn mid-hold glides without a step.
Unfrozen with nothing sounding, the render returns the input buffer
itself. Test lessons: a Hilbert envelope RINGS into a silent region
(measure silence with `abs`, the envelope inside the active part); a
gate high from sample 0 freezes the silence before the input — an
honest capture of nothing, so the tickbox test toggles mid-render.

**Measured.** Sine hold 441.30 Hz / 0.500 / RMS identical at 2 s and
5 s with the input silent from 1.5 s; triad partials 0.1808 / 0.1771 /
0.1999 against 0.1802 / 0.1802 / 0.2000; smear 1 keeps the peak bin,
correlation 0.055, −6.7 dB; the re-freeze swap's largest sample step
is a 660 Hz sine's own; +24 st on a 10 kHz partial folds nothing back.
35 tests. Example `freeze_chord_pad.json` (banked): a bar clock plays
four sus2 chords on the organ for a second each and the clock's
`not_a` freezes the instant each chord's gate falls — the capture is
the sustain, held as a pad for the rest of the bar, easing out under
the next chord. **Suite 4245**, 148 examples, **100 modules.**

---

## 2026-09-19 — a third batch of five love passes in parallel

Matthew: "Ok choose another 5 modules that could use some love I
should have enough to cover it". Same recipe, third time: five
subagents in their own worktrees, per-module scratch folders, reference
renders captured before any edit, the feature OFF at its default and
pinned bit-exact, cherry-picked onto main in landing order (one
conflict — an adjacent import line — both kept). Full suite on the
merged main: **4206 passed**, 147 examples. One process note: the
worktrees branched from the last PUSHED commit (69b81a2), one behind
main's vowel, so the agents' suite counts were ~26 low; harmless, the
count comes from the merged main.

The picks, and why: two utilities that sit in almost every patch (the
clock had no inputs at all; the S&H had no params at all), a source
whose lack of a seed has cost a `np.random.seed` in every test that
touched it, the showpiece effect the tape module was missing, and the
instrument Matthew called beautiful.

**What landed** (each with tests, docs, an example, reference renders
bit-exact at the default):

* **noise `brown` + `violet`, `seed`, `amp_cv`** (e095a07). Brown is
  a LEAKY integrator of the white (a 10 Hz corner, so it rolls off at
  −6 dB/oct from 20 Hz up but cannot wander off as DC); its RMS-match
  scale is derived, not fitted — the stationary variance of a leaky
  integrator is σ²/(1−a²), so `sqrt(1−a²)` (0.0534 at 44.1 k; measured
  0.0541 ± 0.0011 over 8 seeds × 20 s, brown's RMS living in its
  lowest octaves). Violet was the promised two-liner. Measured slopes
  −5.85 / +6.15 dB/oct. `seed` 0 is today's free-running global rng;
  N is a private `default_rng(N)` — the seed alone is the key, so two
  modules with one seed are deliberately the same stream (correlated
  stereo, documented as the feature it is). `amp_cv` is knobless by
  the house rule; a `(V, F)` cv broadcasts ONE stream with per-voice
  level. All ten noise-bearing examples bit-exact. 26 → 73 tests.
* **sample_hold `mode`, `prob` + `seed`, `glide`** (9cd61a9). Track-
  and-hold is the same forward-fill keyed on the last HIGH sample
  instead of the last edge; "sometimes" is the bernoulli convention —
  one draw per edge in time order, consumed only when 0 < prob < 1, so
  prob 1 never touches the rng (pinned via `bit_generator.state`) and
  prob 0 never samples; the voice path draws per edge per voice,
  time-major. Glide is the slew's one-pole premise, landing on 0.99 at
  exactly `glide·sr` samples; 0 calls no filter. **Test lesson: the
  agent guessed a pinned count (104) before measuring — the real
  `default_rng(3).random(200) < 0.5` count is 95. Compute the literal,
  never guess it.** 24 → 62 tests.
* **clock `reset`, `run`, `bpm_cv`** (03c69a3). The block renders in
  segments between restart edges (the LFO's idiom, `_lfo_reset_edges`
  reused as-is) and every segment after an edge starts from phase 0 —
  which is exactly what a fresh clock does on its first block, so **a
  reset IS a fresh clock from that sample**: measured edges 3000,
  8512, 14024 = 3000 + the free-running clock's own. `run` low holds
  (output low, the carried phase provably untouched); a run rise is a
  reset too, so play starts on the downbeat. `bpm_cv` clips the
  exponent to ±6 BEFORE the power (the filter pass's overflow lesson).
  Block size: at 8 Hz the whole gate stream is bit-exact 64 vs 512
  with edges mid-stream; at an integer-period tempo one edge sat a
  sample apart (62052 vs 62053) — pinned ±1 and documented as a
  sample of slop, honestly. 14 reference renders (12 clock-driven
  examples) bit-exact. Test lesson: a reset placed INSIDE a pulse
  produces no rising edge (it extends the pulse) — compute the pulse
  windows before asserting on edges. 8 → 26 tests.
* **organ `vibrato` — the scanner** (27f6a9a). off | v1 v2 v3 | c1 c2
  c3 over the finished `(V, F)` voice sum, one ring per voice, ONE
  scanner phase for the whole console — an integer sample counter, so
  the sweep is bit-exact across any block split. 412 rpm → 6.87 Hz;
  0.35 / 0.7 / 1.1 ms peak-to-peak; **measured V1 ±14, V2 ±27, V3 ±41
  cents at 6.83 Hz against a hypothesis of 13 / 26 / 40.6 — the
  constants stayed.** C is `0.5·(dry + scanned)`, the comb between
  them being the chorus. Every knob switch crossfades dry, wet and
  depth on one 40 ms integer ramp (a hard depth change is a real
  click); the fade back to `off` ends at exactly `1.0·dry` and drops
  the state. Lone-8′ pin survives. **Finding (pre-existing):** the
  organ's own partial phase accumulator drifts a float32 ulp between
  64 and 512 after ~0.55 s — the existing pin spans 4096 frames. An
  integer-count phase would fix it but changes render bits: its own
  pass. 18 → 33 tests.
* **tape `stop` — the tape-stop** (aedd1b9). Speed 1 → 0 over
  `stop_time` (linear — a coasting motor; Hilbert IF 329.9 / 219.8 /
  109.9 Hz at ¼ ½ ¾ of the ramp on 440), the wet level follows the
  speed (a head's EMF ∝ tape speed; 0.749 / 0.499 / 0.250 measured)
  to exact silence, spin-up on the fall, `_gate_ramp_env` integer
  counts, no click. The physics as specced: the lag is the running
  sum of the speed deficit, reset to 0 at a restart that finds the
  head stationary (silent, so inaudible), so after a cycle the wet
  runs `start_time/2` behind the dry (cross-correlated 11024.5
  samples after one, two, three cycles — bounded, not accumulating).
  **Three exactness lessons, all measured:** (1) a carried running
  sum must be `cumsum([carry, e…])[1:]` — `carry + cumsum(e)` regroups
  the additions; (2) subtracting a half-sample lag from a ring index
  rounds at the INDEX's magnitude, which differs per block size, and
  flips float32 ties at 3.5% of samples — apply whole samples and the
  fraction separately; (3) a longer ring alone shifts the shipped
  read by an ulp for the same reason, so the arithmetic keeps running
  in the pre-stop index space and only the lookup uses the long ring —
  that is what made "patched-but-never-risen == unpatched" bit-exact
  in the modulated case. **Finding (pre-existing):** the shipped
  wow/flutter read and the sat → bump chain are block-exact only to a
  float32 ulp at rare samples (4 in 4 s) — documented, pinned to an
  ulp, a follow-on that moves the reference renders. 25 → 47 tests.

**Five examples banked:** `noise_brown_surf.json`,
`sample_hold_sometimes.json`, `clock_transport.json`,
`organ_scanner.json`, `tape_stop_drop.json` — on the listening
checklist. **Fifteen love passes in a day.** Suite **4206**, 147
examples, 99 modules.

---

## 2026-09-19 — vowel: the shape without the buzz (module #99)

Matthew: "vowel please Claude". Spec into MODULE_IDEAS first, built
verbatim; prototyped in a scratch script before the module (the table,
the morph, the levels) — the prototype's peaks landed on the table's
F1s first time, and the module's did too.

**What it is.** A voice is a buzz through a shape; the shape is the
vowel. This is the shape without the buzz: five parallel RBJ
constant-peak bandpasses at F1..F5 of a sung vowel, from the classic
five-formant table — five voice types × A E I O U × frequency, level
and bandwidth, the numbers every formant synth carries — and a
continuous `vowel` knob 0..4 that slides between them (frequencies
geometrically, bandwidths linearly, levels in dB; one pure helper,
`vowel_formants`, the renderer and the tests share). `vowel_cv` moves
it per block; `voice`, `resonance` (a Q multiplier on the table's
bandwidths), `gain` (makeup — the bank is ~15 dB down on a saw, which
is what a formant filter is), `mix` (0 = the input buffer itself,
nothing runs — the effects neutral). Voice-aware like `filter`: one
`lfilter` per formant along the last axis with `zi` of shape (V, 2),
coefficients cached on the (voice, vowel, resonance) key, block-size
exact at a constant vowel.

**Measured.** White noise through tenor A/E/I/O/U peaks at
666/397/285/378/361 Hz against F1s of 650/400/290/400/350; F2 shows
as a local maximum in every vowel; `resonance` 0.5/1/2 gives
peak-to-skirt 6.7/12.4/22.5; the A→E midpoint's F1 is √(650·400) =
510 Hz. 22 tests. Example `vowel_talk.json` (banked): a wide supersaw
on two long notes through the tenor table, a 0.09 Hz unipolar
triangle on `vowel_cv` at depth 4 so the mouth slides A → U and back
over eleven seconds — the first draft peaked at 0.27 with 8 dB of
makeup; 14 dB brought it to 0.54. **Suite 4045**, 142 examples, **99
modules.** One more and it's a hundred.

---

## 2026-09-19 — five more love passes in parallel

Matthew: "Ok Claude pick out another 5 modules for extra love and lets
run subagents on them please" — and, mid-flight, "can I get a todo of
what I need to test next session?" (the listening checklist, now at
the top of TODO.md, 7ddf627). Same recipe as the morning batch with
the one process fix applied — per-module scratch folders — and it
held: no overwritten scripts, five clean reports, one appendix-bullet
conflict on the way in. Full suite on the merged main: **4019
passed**, 141 examples.

**What landed** (each with tests, docs, an example, reference renders
bit-exact at the default):

* **supersaw `detune_cv`** (221b676) — the trance riser. Block-rate by
  design (the detune is a per-block frequency table, so a ratio change
  is a slope change, never a click); per-voice came free; the amp
  normalisation never depended on the detune, so the sweep does not
  pump. Depth is bipolar — a falling riser for nothing.
* **mid_side `side_hp`** (1c5b8ab) — bass mono, the 08-03 follow-up on
  Matthew's favourite module. 12 dB/oct RBJ on the side only; at 0 the
  filter is never called (pinned by a raise-if-called monkeypatch, not
  a 0 Hz filter). The spec's "> 15 dB at 60 Hz under 120" was wrong —
  a Q-0.707 2-pole is −12.3 dB an octave under by definition; the
  agent measured exactly that and pinned the true numbers.
* **sequencer `direction`** (9d9c5fe) — forward/backward/pendulum/
  random with a seed, one pure helper the renderer and the tests
  share; `reset` restarts the random stream so a random phrase
  REPLAYS per bar; `fader_seq` inherited both by contract.
* **pluck `vel`** (61259c1) — latched at the trigger edge on the
  burst only, the loop untouched. A probe found every hit's allpass
  clear ticks 7–18% of the ring, so a zero-velocity hit is silent AND
  leaves the string bit-exactly alone — the honest reading of "keeps
  ringing as it was".
* **delay `freeze`** (02b7d6f) — the echo hangs. The finding: a unity
  loop through a fractional linear-interp read loses 9–11 dB in 10 s
  at ANY fraction (the two-tap lowpass eats the top octave), so the
  held read snaps to `round(time)` samples and the loop is bit-for-bit
  lossless (1e-9 dB over 10 s); the 10 ms ramp slides the read
  position too, half a sample at most.

**Lessons this batch added.** (1) Two hold gates, two different
physics: the reverb's Hadamard tank is lossless at unity by
construction; the delay's interpolated read is not — the same
one-paragraph spec got two right answers because both agents
MEASURED before designing. (2) A click tripwire can be tautological:
at the rise of a lossless loop the loop captures the ramp's own
writes and repeats them, so any switch "passes" — pin per edge and
self-test the tripwire with the ramp collapsed. (3) A spec's numeric
threshold is a hypothesis, not a requirement — when the filter's own
definition says −12.3 dB, pin −12.3 dB. (4) `rng.bit_generator.state`
before/after is the cheap proof that no draw was consumed. (5)
`Path.write_text` on Windows turns an LF file CRLF — two agents hit
it and normalised before committing; patch scripts write bytes or
`newline="\n"`.

---

## 2026-09-19 — cv_recorder: the modulation looper (module #98)

Matthew: "cv_recorder please Claude" — asked for before the subagent
batch, interrupted, asked for again after. Spec into MODULE_IDEAS
first, built to it.

**What it is.** Every modulator in the rack is generated; none of them
is *you turning a knob*. This records a CV for a fixed loop length and
plays it back forever. The looper is the fixed-length kind ("N bars"):
`length` is a setting, `rec` writes while high (the first edge creates
the loop — the position ramp starts at 0 then and runs until `clear`),
`mode` replace punches in / overdub adds to the old layer scaled by
`feedback`, `out` is the loop and, while recording, what is being
written; `pos` is a 0..1 ramp. With `in` unpatched the module's own
`value` knob is the input — read once per block and ramped linearly
across it from the previous block's value, so a recorded hand
movement has no steps. That is the "knob gesture" of the one-liner.

**Clocked.** With a clock, `length` is ticks (16 sixteenths = a bar)
and the loop's sample length is fixed from the clock's measured period
at creation; rec edges — on and off — are honoured on the next tick
(quantised punch-in), and the position hard-syncs to 0 on every
`length`-th tick from the loop's start so it never drifts from the
transport. One rule the building added: a rec edge that would CREATE
the loop before the clock's period is known (only one tick seen)
waits for the second tick — the buffer cannot be sized before that.
The example's rec gate is high from t=0 and the loop starts one
sixteenth in; documented in the entry.

**How it runs.** The block is walked as segments between events —
clear, rec edges, clock ticks, in that priority at a shared sample —
and each segment is a couple of numpy slices over the buffer (split at
the wrap). Every event is an integer sample position, so a
patched-`in` render is block-size independent, pinned at 50 vs 250
samples with rec edges and a clear landing mid-stream. 18 tests:
record then bit-exact playback, out-while-recording is the written
value, replace punches in only where rec was high, overdub sums with
feedback scaling the old layer, clear wipes/rewinds/holds, the pos
ramp, the clocked length and quantised edges and the boundary re-sync
under a tempo change, the deferred creation, the knob ramp, the mode
combo, the example. Example `cv_recorder_layers.json` (banked): every
other bar overdubs a 0.37 Hz triangle onto a bar-long loop at feedback
0.6 — the loop that moves the filter evolves (bar RMS 0.54 → 0.24 →
0.43 → 0.34 across the first eight bars) instead of accumulating.
**Suite 3909**, 136 examples, **98 modules.**

**The honest gap**, on the TODO: the loop lives in backend state and
is lost on Stop — a *performance* capture that does not survive the
session. Saving it in the patch (a `loop` blob param) is the natural
follow-on, with `play`/`mute`, half-/double-speed and `reverse`.

---

## 2026-09-19 — five love passes in parallel

Matthew: "i've banked a bit of credit this session like can i get you
to fire up subagents for say 10 modules you decide upon to get some
love?" — then "Just5". So five: one subagent per module, each in its
own git worktree (`PYTHONPATH=src` so the worktree's code is the one
imported past the editable install), each handed the love-pass recipe
and a one-paragraph spec, each committing on its own branch. The
coordinator cherry-picked the five onto main in landing order and
wrote these records; the only conflicts were in MODULES.md — two
appendix bullets and two adjacent index rows — resolved by keeping
both. Full suite on the merged result: **3886 passed**, 135 examples.

**What landed** (each with tests, docs, an example, reference renders
bit-exact at the default):

* **lfo `reset` + `phase`** (bda4f53). A rising edge restarts the
  phase at `phase` on that sample — the block is rendered in segments
  between edges — so a keyboard gate into `reset` makes every note's
  vibrato start from the same place; `phase` is also the free-running
  start and re-anchors a running LFO when the knob moves. Per-voice on
  the voice path, any-voice-high on the mono path; `random` re-rolls
  on reset. Found and fixed in passing: `random` at a non-zero start
  phase output 0.0 until its first wrap. 62 renders bit-exact.
* **filter `resonance_cv`** (cd347f2). Q doublings per unit, block
  mean like `cutoff_cv`, per-voice from a `(V, F)` source; cv +1 at
  depth 1 is the resonance-doubled render bit-exact. Found: every
  `2.0 ** (depth·cv)` block-mean path in the backend will raise
  OverflowError on an absurd CV — the new path clips the exponent;
  the cutoff family is on the TODO. 86 renders bit-exact.
* **oscillator PWM** (cb6eea0). `pulse_width` + per-sample `pw_cv` on
  `square`/`square_blep`, DC-compensated; the falling-edge PolyBLEP
  runs on the falling edge's own phase with its own increment, exact
  for widths moving slower than the phase (every edge under a 30 Hz
  LFO got one correction pair). Alias fraction at width 0.1: 0.0018
  vs 0.108 naive. `square_wt` stays 50%. 68 renders bit-exact.
* **adsr `vel`** (115fc17). Edge-latched per note like the drums';
  stages run in output space so the times stay what the knobs say; a
  softer re-strike falls at the attack slope instead of jumping.
  Pre-existing and now pinned: the voice-path ADSR's crossing lands
  ±1 sample across block sizes. 14 arrays bit-exact.
* **reverb `freeze`** (5a9d3b4). The tank's Hadamard mix is
  orthonormal, so unity feedback with the damping bypassed is lossless
  by construction — energy conserved to < 0.01 dB over 10 s, no gain
  hunting; a 10 ms integer-count ramp in and out; input muted from the
  tank, dry untouched. 8 renders bit-exact.

**Lessons from running five at once.** (1) The scratchpad directory is
shared across parallel agents: three of the five had a patch script
overwritten by a sibling after it had run — harmless this time,
because each had already applied it, but the next batch gets
per-module scratch folders in the prompt. (2) Cherry-pick, not merge:
linear history, each pass keeps its own message, and the conflicts
were confined to MODULES.md's shared lists. (3) The recipe scales:
every agent captured its references from the pristine HEAD source
(one used `git archive` to be sure) and every one reported bit-exact;
the unseeded examples (noise, `random` LFO, pluck bursts) had to be
rendered under `np.random.seed` to be comparable at all — the same
finding as the swell-strike test, now from five directions. (4) Each
agent's test lessons are in its TODO entry; the recurring one is
"the observable must be able to see the thing" — a click window
must run until the changed writes reach the taps, white noise cannot
detect a click, a duty-cycle count is biased a sample per cycle.

---

## 2026-09-19 — cv_math: logic for voltages (module #97)

Matthew: "cv_math please Claude I look forward to testing all these
components". The keep-list's "logic-for-CVs, zero params" — spec into
MODULE_IDEAS first (filed under CV & Utilities beside `cv_scale` /
`cv_offset` rather than Modulation, where the one-liner sat), built
verbatim plus one jack.

**What it is.** `logic`'s shape exactly: two operands in, every
function out at once, no mode combo, swap cables not settings. `min`
and `max` are the analog AND/OR (an LFO ducked by an envelope; two slow
LFOs making a shape neither has alone), `avg` the blend, `diff` the
difference (an LFO minus its slewed self is its rate of change),
`rect` |a|, `inv` −a — and **`mult`**, the one jack the one-liner
didn't name and the one the rack most lacked: a CV×CV multiplier. The
`vca` takes audio, `cv_scale` a constant; nothing multiplied two
voltages, so "an envelope on a vibrato's depth" — delayed vibrato, the
oldest synth trick there is — needed a workaround. Now it's a cable.

**The normalled trick, again.** An unpatched operand reads 0, so a lone
`a` makes `max` its positive half-wave and `min` its negative one
(half-wave rectifiers for free, the way `logic`'s NAND idles high),
`avg` is a/2, `diff` is `a` bit-exact, `mult` is silence. Documented
and pinned. Shape-polymorphic via numpy broadcasting: a `(V, F)`
operand against a mono one gives `(V, F)` on every jack (the a-only
functions broadcast too, so all seven agree on width); two polys of
different width line up on the smaller. Stateless — no `_state` entry
ever, pinned.

**13 tests**, `examples/cv_math_delayed_vibrato.json` (banked): a
vibrato LFO × the note's ADSR through `mult`, summed into the pitch
with a `cv_combiner` — no wobble at the 0.6 s attack, full at the
sustain (pinned: the first half-second's wobble under a third of the
later one's) — and a second `cv_math` taking the `max` of two slow
LFOs onto a filter cutoff, with a scope on the delayed vibrato.
MODULES.md entry (a jack table), index row, appendix; README 97
modules (CV & Utilities 13). **Suite 3762**, 130 examples, **97
modules.** An S that stayed S: forty minutes, no surprises.

---

## 2026-09-19 — drift: the random that stumbles (module #96)

Matthew: "drift please Claude?" Spec into MODULE_IDEAS.md first, then
built to it — verbatim, for once; an S-sized item that stayed S.

**What it is.** The rack's random sources all had corners or a plan
(the LFO's `random` and `sample_hold` step, `shift_random` loops,
`chaos` orbits). This stumbles: a new target every `1/rate` seconds —
`smooth` draws fresh across ±1, `walk` adds a gaussian `step` to the
last target and reflects off the edges — and the output travels to it
along a half-cosine over `glide` of the interval, then holds. Three
outputs off the one die: `cv` (the wander), `stepped` (the held
targets — so a filter can drift while a quantized pitch steps to the
same values), `trig` (a 1 ms pulse per new value — a random clock at
the same pace). `rate_cv` in octaves per block; `clock` patched puts
the draws on its edges with the glide sized from the measured beat.

**Two design calls worth keeping.** (1) *The tick schedule is an
integer sample count* — `next = last_tick + round(sr/rate)` — not a
float phase accumulator like the LFO and clock use. The selector's
graph test had shown the clock's phase lands an edge a sample apart
between block sizes; drift's ticks land on the same absolute sample at
64 and at 2048, and the whole render is bit-exact, pinned in four
configurations. (2) *A new curve starts from the current value, not
the old target.* With `glide` 1 the value has arrived when the next
tick comes, but a `rate_cv` sweep or a jittery clock can bring a tick
mid-glide; starting from where the value actually is means nothing
ever jumps. Pinned by speeding the rate 20× under a long glide and
checking the seam.

**One trap dodged.** `mode` is the param name, and `mode` goes through
the shared combo branch in `_add_param_widget` that has bitten two
modules before — the arm was added there (not in a TYPE block) and
`test_mode_combos` plus a drift-specific test check the dropdown
offers smooth/walk, not the filter's modes.

**24 tests** (`tests/test_drift.py`): the schedule; an octave of
`rate_cv` doubles the ticks; `trig` 1 ms wide, one per tick, carried
across a block boundary; bit-exact block independence; seed
determinism; `smooth` targets uniform (sd ≈ 1/√3, mean ≈ 0); `walk`
reflected, ~`step` moves, lag-1 correlation > 0.8 where `smooth`'s is
< 0.2; `glide` 0 == `stepped`; `glide` 1 the half-cosine exactly, no
jumps; partial glide arrives early and holds; the mid-glide tick; the
clock's edges and measured interval; scaling; depth 0; the combo; the
example. Example `drift_wander.json` (banked): one drift breathing a
resonant lowpass over a low saw, its stepped twin through a pentatonic
quantizer into a pluck that its trig fires — a note at every place the
wander turns towards — plus a second drift in `walk` mode, 36 cents
deep, as the old-oscillator pitch drift on the drone, and a scope on
the wander itself. **Suite 3745**, 129 examples, **96 modules.**

---

## 2026-09-18 — wind: the pipe that sustains (module #95)

The blown half of "bowed/wind", built to the spec written alongside
`bowed`'s. One module, two mouths: `model` = `flute` (Cook's slide
flute, STK *Flute*) or `reed` (the STK *Clarinet* mouthpiece), sharing
the bore machinery, the breath ramp and the breath noise.

**The flute** is a jet delay (0.32 of the bore) feeding the cubic jet
table `x(x² − 1)` into a bore tuned to **1.5 periods** — STK's own
"we're overblowing here", the reason it sounds like a flute and not a
whistle — with a one-pole and a DC blocker in the return and 0.5/0.5
jet/end reflections. It speaks in a pressure window: below ~0.9 it
barely oscillates, above ~1.5 the jet table saturates and it dies, so
`breath` 0..1 maps to 0.85..1.4 and every setting speaks. Its regime
sits ~1.5% sharp of the bore, so the bore carries a measured 1.015
correction; ±4 ct C3..C6 at moderate breath, sharp when blown hard
low down (a flute does that), and clamped at 80 Hz below (the jet
loses its register). **The reed** is one round-trip delay with a
one-pole loss and the reed table `clip(0.7 − 0.3·Δp)`; it needs ~0.58
of pressure to speak and closes above ~1.1 — both physical — so
`breath` maps to 0.58..1.0. In tune to ±5 ct C2..C6 out of the box.

**Shared with `bowed`**, now as one helper: the integer-count gate
ramp (`_gate_ramp_env`, extracted from the bowed renderer with its
tests as the guard), the slice-buffer chunked loop (chunks ≤ the jet
for the flute, ≤ the bore for the reed — both cheap, under 0.5 ms a
block per voice), per-block pitch, early-out. New: breath noise as a
**per-(voice, note) seeded stream drawn per segment between rising
edges** — the same samples come out at any block size, and the next
note re-seeds, so the early-out point (which IS block-size dependent)
never leaks into a later note. The output is DC-blocked because the
reed's line carries the breath pressure.

**Two things the measurements changed.** (1) The docstring's first
draft called the reed "odd harmonics, the chalumeau and the clarion"
— the physical clarinet story. Measured, the model's even harmonics
come and go with breath and noise (h2/h1 from 0.09 to 1.05 across
settings), so the claim came out and the test pins what is true: on
one note the reed is harmonic-rich and the flute nearly pure (upper
harmonics 3× the flute's, relative to the fundamental). (2) The
module's flute was 25 ct sharp at C6 while the prototype was +2: the
prototype's calibration runs had used `comp_extra = −1`, cancelling a
`− 1.0` in its bore formula, and the module had copied the formula
without the cancellation. One stray sample at a 64-sample bore. The
lesson is old and worth repeating: **measure the module, not the
prototype** — the prototype's numbers are for deciding, the module's
for pinning.

**32 tests** (`tests/test_wind.py`), `examples/wind_duet.json`
(banked: a flute tune an octave up with a vibrato LFO summed in and a
0.15 Hz LFO on `breath_cv` so the player breathes, over a chalumeau
reed line at half speed off a `clock_divider`, through a chamber —
first draft peaked at 0.97, trimmed to 0.63). MODULES.md entry, index
row, CV-depth row, appendix; README 95 modules (Sources 22);
MODULE_IDEAS spec marked shipped with the as-built notes. **Suite
3716**, 128 examples, **95 modules.** The physical-modeling family is
complete: `pluck`, `modal`, `bowed`, `wind`.

**Also in this commit:** the full run caught
`test_the_swell_strike_example_fires_the_pluck_at_every_peak` failing
about one run in four — the example is unseeded (noise + random LFO
draw from numpy's global RNG) and a fast swell re-struck a pluck still
ringing at 0.65. "Three green runs" was not enough. The test now
seeds `np.random` before rendering; four consecutive greens, and the
claim is unchanged.

---

## 2026-09-18 — bowed: the string that sustains (module #94)

Matthew: "bowed/wind please Claude thanks!" — the first pick off the
2026-08-04 keep-list since the function generator. Spec-on-pick: both
halves were promoted from the one-liner into MODULE_IDEAS.md (ports,
params, DSP, tests), then `bowed` was built to its spec. `wind` follows.

**What it is.** The rack's physical-modeling family had two struck
instruments — `pluck` (a string) and `modal` (a resonator) — and no
sustained one. This is Smith's digital-waveguide bowed string as in
STK's *Bowed*: a bridge-side delay (`position` × the period) and a
nut-side delay meet at the bow; the string velocity there is
`−lowpass(bridge_out) − nut_out`, the bow injects `dv · table(dv)` into
both halves with `table(x) = min(1, (|x·slope| + 0.75)^−4)` — the
friction curve: stick below the threshold, slip above it, and the
stick-slip cycle at the string's own period is the note (the
Helmholtz sawtooth at the bridge, which the example puts on a scope).
The bow's two hands are `pressure` (the table's slope) and `velocity`
(bow speed, ramped by integer-count `attack`/`release` on the gate),
each with a per-sample CV; `damping` is the bridge one-pole; `body` a
five-band bank (275/460/550/1100/2200 Hz constant-peak bandpasses).

**How it runs.** The pluck's chunked loop, with a nonlinearity inside:
the loop is advanced in vectorized chunks no longer than the shortest
delay (the bridge side), and since the friction table is elementwise
it chunks exactly like the linear loop did. Two changes over the pluck
idiom that are worth keeping: delay lines are *slice buffers* (history
compacted once per block, plain slices per chunk, no fancy-indexed
modulo — the prototype went from 1.0 to 0.5 ms a block at C4), and
the nut delay is integer with the whole fraction on the bridge side,
so one of the two reads is a bare slice. Cost, one voice at 44.1 kHz:
0.5 ms of an 11.6 ms block at C4, 0.9 at C5, 1.7 at C6 (chunks shrink
with the period). A solo instrument by design; silent voices early-out.

**Tuning.** Loop delay = `sr/f0 − τ(f0)` with τ the one-pole's *exact*
phase delay at f0 (the ω→0 approximation was 10 ct sharp at C4, 50 at
C6 — the residual is a near-constant sample, so the exact formula
matters more the higher you go). Measured −3/−1/+1/+1/+6 ct C2..C6;
±10 pinned. At `damping` 1 with the pole at 0.8 the note went +47 ct
at C5 (the nonlinear loop picks its own regime under heavy loss), so
the pole range is 0.15..0.7 and the pin holds across it.

**Findings worth keeping.** (1) A prototype before the module: the
bowed string worked first time from the STK recipe; the flute did NOT
— it sat on a DC fixed point until the bore was retuned to **1.5
periods** (STK's `setFrequency` says "we're overblowing here") and
the breath pressure raised to STK's ~1.4, after which it speaks in a
pressure window (jet saturation kills it above ~1.5); the reed speaks
above a threshold and over-pressure closes it. Both physical, both
now in the `wind` spec. (2) The re-bow contract: a gate rising during
the release restarts the attack counter *from the current envelope
level* (`on_count = env × attack_samples`), so a fast re-articulation
never jumps — pinned by a diff-of-samples test around the re-bow.
(3) Block-size independence under a gate that lands AND lifts
mid-stream holds because every ramp is an integer count from its
edge (the organ's lesson), the one-pole is one `lfilter` with carried
`zi`, and the chunk math is elementwise.

**27 tests**, `examples/bowed_cello.json` (banked: an eight-step line
two octaves down through a `slew` and a vibrato LFO summed in a
`cv_combiner`, 88% gates re-bowing every note, a 0.07 Hz triangle on
`velocity_cv` swelling the bow, a scope on the bridge, a hall).
MODULES.md entry + index row + CV-depth rows + appendix; README 94
modules (Sources 21); MODULE_IDEAS specs for both halves. **Suite
3680**, 127 examples, **94 modules.**

---

## 2026-09-18 — possibility_selector: the router collapses (module #93)

Matthew: "... and the meta-possibility selector — collapse which module
fires, the genuinely novel one". The TODO sketch (2026-08-15) said it
earns a spec once the first module has been played; it has ("exactly
what I was hoping for"), so the spec went into MODULE_IDEAS.md first and
the module was built to it.

**What it is.** `possibility_seq` is a register of bits with holes in
it — each step says *whether* a hit fires. This is the same register
one level up: each step says *which* of four outputs the incoming gate
goes to, and a step can be left open. A step's state is the **set** of
outputs it may go to — `"1"`–`"4"` decided, `"?"` all four, a digit
subset like `"13"` some (kick or hat, never snare), `"0"` a rest — so
the possibility count is the product of each open step's choices, up
to 4^16. The take model is possibility_seq's verbatim (loop / latch /
dice, memo per take, reroll, reset, seed → deterministic, block-size
independent) with `resolve_route` in place of `resolve_step`: a
weighted draw over the candidates — `weight1..4` say how the open steps
*lean*, and a zero weight takes that output out of every `?` without
touching a decided step.

**`balanced`, generalised.** The seq's fix for coin clumping deals
fair `?`s from a two-value shuffle-bag. Here a fair open step (its
candidates equally weighted) is dealt to the candidate used *least* so
far, ties broken by the die — which is exactly PythonBinaryPossibility's
`RandomGeneratorPerfect` selector (width 1), and is where the module's
name comes from. Sixteen fair `?`s over four outputs come out four of
each, every seed, one of each in every consecutive four. Decided steps
don't touch the bag (they weren't dealt), same as the seq.

**Two stepping contracts**, so one cable is enough either way. `clock`
patched: the sequencer reading — route *k* belongs to clock tick *k*,
`in` only says whether anything passes, and a held `in` switches
outputs mid-gate as the step moves. A possibility_seq gate into `in`
with the same clock on both is a **whether × which** kit: the sum of
the four outputs is exactly the seq's gate, pinned through the real
graph at 512 and 64. `clock` unpatched: the distributor reading —
`in`'s own edges step the register, route *k* belongs to the *k*-th
hit whenever it arrives. `in` unpatched: the clock itself is routed.

**The panel** is the possibility panel one level up, built beside it
rather than after ears this time because sixteen text inputs for
route strings would have been unusable: cells coloured per route (one
hue per output, grey rest, the family's amber for anything open, the
label carrying a subset's digits), click cycles `0→1→2→3→4→?→0`, a
subset sits in the `?` slot (next click is a rest), right-click opens
four checkboxes that write the canonical state string (`format_route`:
all ticked is `?`, one is decided, none is a rest — the popup ticks
repaint from the model too), four `? leans to` sliders, the readout.
The `_pseq_theme` global-theme economy is reused with its own colour
table.

**Example `possibility_selector_kit.json`** (banked): a WHETHER seq
(odd sixteenths hit, evens `?`) into a clocked WHICH selector — kick 1,
snare 2, closed hat 3, open hat 4, the open steps leaning to the hats,
step 8 `"34"`, step 12 `"13"`, step 16 `"24"`; beside it a
four-string harp — a sparse gate into a second selector with no clock,
so every *hit* walks an eight-note register (C3 E3 G3 B3 ? G3 ? C3)
whose two open strings are dealt `balanced` and re-drawn each pass.
The seeded render: the drum outputs sum exactly to the seq's gate and
every drum gets played; every harp pass reads 1 2 3 4 x 3 y 1 with
x ≠ y.

**Test lessons.** (1) The clock's float64 phase accumulator lands an
edge a sample apart between block sizes now and then (600-sample pulses
at 48 kHz: nine of 95,744 samples differ) — a graph-level block-size
claim must compare the *sequence of hit routes*, and the module's own
block-size pin stays on hand-built pulses. (2) A pulse train built
with an `offset` needs its reads offset too; "all rests" was the gaps,
not the module — assert `count(0) == 0` on the read before comparing
takes. (3) Two seconds of a 240 bpm graph gave 24 hits and out4 never
came up; render four.

**56 tests** (`tests/test_possibility_selector.py` 35,
`tests/test_selector_panel.py` 21). MODULES.md entry + index row +
appendix line, the seq entry points at its sibling, README 93 modules
(Modulation 17), MODULE_IDEAS spec marked shipped. **Suite 3649**, 126
examples, **93 modules**. The possibility bridge's follow-on list is
closed. **EYES wanted:** the route colours and the four-checkbox popup
in a real window (the possibility panel's popup + tooltip coexistence is
proven; this adds checkboxes inside the popup).

---

## 2026-09-18 — the reroll divider: a groove you keep for four bars

Matthew: "the possibility follow-ons (a reroll-divider example, and the
meta-possibility selector ...)". This is the first half.

The TODO said "slow clock into `reroll`". A second free-running
`clock` at `division` 0.0625 would do it and would stay in phase only
because both clocks start at phase 0 — a coincidence, not a contract.
The example uses a [`clock_divider`](docs/MODULES.md#clock_divider)
chain off the *same* sixteenth clock instead: `divn` 16 (one pulse a
bar) into a second divider's `div4` (one every four bars). Both fire
on their edge 0, so the re-deal lands exactly on the downbeat of bars
1, 5, 9 … by construction, and in the sequencer's sample loop the
reroll edge is read before the clock edge of the same sample, so step
1 of the new bar is already the new take.

Three `latch`ed patterns off one clock: a kick with a decided floor and
four `?`s, a snare with the backbeat held and four ghosts, and a
balanced all-`?` hat. Kick and snare take the four-bar reroll — a
groove you keep long enough to learn, then lose; the hat takes the
per-bar pulse from the first divider and re-deals every bar, eight
hits a bar every bar. The seeded render: kick bars 1–4
`x..xx.x.x...x...`, bars 5–8 `x..xx...x...x.x.`; snare likewise; hat a
different eight-of-sixteen every bar.

**One test lesson.** The clock's first rising edge is sample 0 of
block 0 (the renderer starts at phase 0 so a downstream sequencer
plays step 1 immediately). A pairwise edge detector
(`buf[1:] > 0.5 & buf[:-1] <= 0.5`) misses it, every bar reads one
step late, and the decided floor appears on steps 4/8/12/16. Prepend
sample 0 when the buffer starts high.

Three tests in `tests/test_possibility_seq.py` (reroll edges are clock
edges 0, 64, 128; kick/snare identical within each four bars and
different across; hat 8/16 every bar and ≥ 6 distinct bars in 8).
MODULES.md: the possibility_seq patching paragraph and an appendix
line. **125 examples.** Banked for ears.

---

## 2026-09-17 — fg follow-on 4: what `eor` is for (and the patch that is a latch)

Matthew: "fg follow-on 4 (eor → trig rise-only retrigger — an example
and a paragraph now), please Claude" — the last item on the function
generator's follow-on list, unblocked by the door.

**Measured before building, and the measurement changed the job.** A
self-patched `eor → trig` (through the door, OR'd with a starter) is
not a rise-only retrigger; it is a latch. `eor` fires at the top of
the rise; the pulse reaches `trig` a block later, a few milliseconds
into the fall, where the level is ~0.99; a retrigger there restarts a
rise that finishes in ~2 ms and fires `eor` again. At 512 the output
pins between 0.98 and 1.0 with a 613-sample ripple, forever. At 64 it
is worse: the 2 ms pulse straddles two blocks, the OR input never
falls between them, the edge detector sees no new edge, and the loop
dies to zero. Gate mode dies at both sizes; loop mode matches
trigger mode. There is no musical reading of any of that, so the
paragraph says it plainly and a test pins it (≤ 1 completed cycle at
512 and 64). The deviation from the item as phrased is noted here per
the standing principle: the honest version shipped, not the named one.

**What `eor` is for.** Firing things at the *peak*: a pluck's trigger
(strike at the top of a swell), a sample_hold's trig (grab a voltage at
the loudest moment), a second fg's trig (chain an attack into another
shape — a four-stage envelope from two modules), a sequencer's clock
(`eoc`'s clock shifted by the rise time). The docstring and the
MODULES.md entry now carry that list beside the `eoc → trig` note.

**Example `fg_eor_swell_strike.json`** (banked): a slow fg — 1.2 s up,
1.8 s down, `curve` −0.3 (the swelling, orchestral shape) — cycling
through the krell loop with a random LFO on `rate_cv`, swells a C2
saw through `filter.cutoff_cv` and a VCA; at the top of every swell
`eor` fires a pluck whose pentatonic pitch a sample_hold grabbed at
that same instant. Swell, then strike. Ten swells in thirty seconds,
every `eor` at level 1.000, every pluck going from silence to 0.7.

**One test construction.** "The pluck is quiet before each eor" held
in isolation and failed in the full run: the example is unseeded, a
fast swell can come 1.5 s after the last one, and a 2.2 s pluck is
still ringing at 0.15 then. The claim is *a strike*: the peak after
`eor` is above 0.3 and 1.5× whatever was ringing before. Three runs
green.

**Suite 3584.** 124 examples. The function generator's follow-on
list — per-slope curves, per-slope CVs, `out_inv`, per-voice rates,
and this — is closed.

**Yours: 9 commits to push.**

---

## 2026-09-16 (last) — the late-cable UI: the block of latency, visible

Matthew: "late-cable UI follow-up please Claude thank you!" — the small
follow-up the door asked for.

**What it does.** The cable that closes a loop is drawn **amber** and
thicker the moment it is drawn — before Start, because the backend now
exposes `feedback_cables(patch)`, the late set a compile *would*
produce, a pure function of the patch. The status bar says "Loop
closed: delay#3.out -> mixer#2.in2 reads one block late (feedback)".
The toolbar gains a `loops` slot beside `api`: `loops --`, `loops N`,
or — while running — `loops N !K` in the warning colour when K blocks
of a loop went non-finite and were scrubbed (`feedback_scrubs()`, the
counter the door added). Hover it for the list of late cables.

**How.** One dpg theme, built lazily and once (`mvNodeCol_Link` /
`LinkHovered` / `LinkSelected` in the nodes category plus
`mvNodeStyleVar_LinkThickness` 4), bound per link with
`bind_item_theme` — the possibility panel's per-cell precedent. The
refresh runs on every cable or module change and on load/new, never
per frame; the readout's per-frame tick only touches dpg when its text
changes. A backend without the observables (the pyo stub) leaves the
editor untouched, the getattr-guarded discipline the DSP readout set.

**Tests.** 11 in `tests/test_late_cable_ui.py`, mocked dpg handing out
distinct ids — the context managers (`node_attribute`, `theme`, …) get
a tiny stand-in whose `__enter__` returns the next id, which is what
lets `_port_to_attr` and the link map stay distinguishable. One test
construction: "a plain cable stays quiet" cannot be read off the *last*
status value (nothing overwrote the earlier "Loop closed"); count the
writes instead.

**Suite 3574.** One eyeball for Matthew: does the amber actually
paint? DPG documents per-link theme colours; nothing here runs a real
window.

---

## 2026-09-16 (last) — the feedback door, generalized: built

Matthew: "as recommended please Claude, execute the plan please." Built
to the plan in the entry below, all four decisions as recommended.

**The change.** `_compute_late_edges` keeps pass 1 byte-for-byte —
cables into a `matrix_mixer`, forward order — and adds pass 2: every
remaining cable, walked in *reverse* `patch.cables` order, that can
still reach its own source through the non-late, non-fill graph goes
late. The render side needed nothing: the seed and stash in
`render_block_multi` already key on `_late_edges`. One addition there:
a late source that produced a non-finite value is scrubbed to zero
before it is stashed, and counted (`_late_nonfinite`), so a loop that
blows past float range costs one silent block instead of poisoning the
graph forever.

**The recipe held, and caught the one bug.** Eight reference renders —
the three matrix loops, the governor, four loop-free examples — with
their late sets and topological orders, captured before editing. After
the first cut the governor's late set had grown by its own `fill`
cable: pass 2 saw a cable whose destination reaches its source and
marked it, not knowing that a fill cable is already a delayed edge
seeded from the sink itself. Skipping fill cables in pass 2 was the
fix; all eight then matched exactly, render, late set and order. The
krell example had to be dropped from the reference set — it renders
differently run to run on its own (unseeded noise / random LFO), which
is not the door's doing but is worth knowing.

**What it opens.** `eoc → trig` fires forever — the real krell — with
a period of exactly `cycle + block − 1` samples at 512 *and* 64 (one
block of loop latency, less a sample of edge accounting; block-size
dependence is the door's documented property, as it was for the
matrix). The May self-wah wahs: its loop changes the render and stays
under 1.0, and the shipped example now sorts the follower *after* the
filter with the return cable late. A `mixer` self-loop with a DC
driver walks the geometric staircase one generation per block at every
sample — the matrix test's twin through the new door. Two loops and a
figure-eight all sort. Closing or opening a loop while running
recompiles cleanly.

**Two things the smoke tests taught.** A bare `eoc → trig` self-loop
never *starts* — nothing pokes it — and `trig` takes one cable, so the
starter and the return are OR'd through `logic`; and the starter pulse
must be shorter than the fg's cycle, or the OR never falls and the
returning `eoc` is not a rising edge (my first starter was 0.6 s long
and the smoke "fired once", exactly the failure the door exists to
end). And a square wave is no DC driver for a staircase pin: sample 0
flips sign block to block. `constant → cv_to_audio` is.

**Example `krell_feedback.json`** (banked): the krell's dice, quantizer
and voice with the engine in `trigger` mode, its `eoc` OR'd with a
12-second starter (bpm 20, division 0.25, pulse 0.01 — all panel-
reachable) back into `trig`. `krell_machine.json` stays on `loop` mode
as the no-latency version; its tripwire's message no longer says the
cable "does not close in this rack".

**21 tests** in `tests/test_feedback_door.py`, plus a new examples-sweep
tripwire — Kahn's leftover tail must be empty for every example (before
today the self-wah sat in that tail for four months). **Suite 3563.**
123 examples. Docs: a *Feedback loops* paragraph under Cabling rules;
the function generator's "legal but inert" note and docstring
rewritten; the matrix is the "guarded" door now, not the "sanctioned"
one; the self-wah is documented as the loop it is.

**Follow-up, its own small session:** draw late-read cables
differently in the node editor and say "feedback: N cables read one
block late" on compile — `_late_edges` and `_late_nonfinite` are the
data. And fg Follow-on 4 (`eor → trig`) is unblocked.

**Yours: 7 commits to push.**

---

## 2026-09-16 (later) — the feedback door: planned, not built

Matthew: "plan the feedback door with me please Claude then after we
can implement it?" — so this entry is the plan, written down before a
line moves, with the decisions marked for him. The plan itself lives in
TODO.md under "Sanction gate-rate feedback".

**Survey.** The compiler honours two delayed paths: a buffered sink's
`fill` and cables into a `matrix_mixer` that `_compute_late_edges`
marks (forward cable order, BFS from the matrix back to the cable's
source). Every other cycle is *severed*: Kahn never emits its members,
they fall into the leftover tail in id order, and a consumer renders
before its source with the buffer absent. A sweep of all 122 examples
found exactly one severed loop and three live ones: **the May self-wah
(`envelope_follower_wah.json`) has never wahed** — its filter →
audio_to_cv → cutoff_cv loop sorts [keyboard, filter, audio_to_cv,
speaker], and an A/B with the cable removed is `array_equal`. The three
matrix examples and the governor are the only loops that close.

**The change.** Keep pass 1 untouched — the matrix stays the guarded
door, so every existing loop compiles to the same late set and renders
bit-identically — and add pass 2: walk the cables in *reverse* order
and mark late any cable whose destination can still reach its source
through the non-late graph. Reverse, because the cable that closed the
loop is the one that should carry the block of latency; forward order
would put it on the feed-forward leg. One cable per cycle; self-loops
mark themselves; the render side already seeds and stashes by
`_late_edges` and needs nothing. Decisions for Matthew: the choice of
late cable (last-drawn), safety (scrub non-finite at the stash + count,
no global ceiling — the matrix keeps its `soft_clip`), examples (a new
`krell_feedback.json` beside the existing krell; the self-wah simply
starts working), and UI visibility of late cables as a follow-up.

---

## 2026-09-16 — `granular` slice 3: freeze and the scrub; the module is complete

Matthew: "granular slice 3 please Claude." Three slices in two days,
each its own commit, each verified against reference renders captured
from the previous one before a line was edited.

**What it adds.** `freeze` — a gate port and a bool param, ORed, the
resampler's `brake` idiom — stops the buffer *recording* while the
grains keep *reading*, so the last `buffer` seconds hang and `position`
scrubs across them. `position_cv` with `position_cv_depth` (fractions
of the buffer per CV unit, default 1 — house rule, one row added to
the CV-depth map) moves the read point, read at each grain's onset and
latched for that grain — the sampler's `start_cv` edge-latch rule,
because a block-mean would smear a sequencer's step across the grains
that straddle it.

**Captured time.** The design decision that made everything else fall
out: the ring is indexed in *captured* time — a counter `c` that
advances only while recording — while grain onsets run on wall time.
Per block, `hw[j] = c0 + cumsum(live)[j] − 1` is the index of the last
sample captured as of sample *j*; the input is written only at the
live samples; a grain fired at *j* reads from `hw[j] − D`. Live,
`hw[j]` is the sample's own index and the arithmetic IS slice 2's (all
36 reference arrays — six param sets × two block sizes × three outs —
`array_equal` after the edit). Frozen, the head stands still, so the
head start a fast grain needs becomes `rate × (L − 1)` rather than
`(rate − 1) × (L − 1)`. And a release resumes recording at `c`: the
timeline is continuous, like a tape, no hole and no duplicate — pinned
by a test that reconstructs `captured = x[live]` and asserts `y[n] ==
captured[c(n) − 24000]` bit-exact after a freeze/release cycle.

**The per-sample freeze.** The gate is read per sample, not per block:
the freeze lands on the exact sample the gate rises, the captured count
stops there (asserted), and a mid-block edge renders bit-exact in 64s
and 512s. That is what makes "hold the last two seconds" mean the same
two seconds whatever the buffer-size slider says.

**The one artefact, chosen.** A grain that was running alongside the
head (position 0, rate 1) when a freeze lands would overtake the
now-stationary head and read stale data — the ring a lap ago, which
can be anything. Three options: cut the grain (a click), let it read
stale (a burst of unrelated audio), or clamp its read to the head so
it holds its last sample under the rest of its window (a short DC
hold, continuous in value). The clamp — `pos = min(pos, hw − 2)` per
sample, a no-op while live because the head start guarantees it. The
ramp test shows the output staying within 0.85–0.88 of a 0.875 head
value; stale would have read ~0.3. Grains further back than one grain
length are untouched, which is nearly all of them.

**The dry moved.** `mix`'s dry side was a ring read two samples back;
frozen, that goes stale. It is now a two-sample tail of the live input
carried in state — bit-identical live (both are the float64 of the
same float32 samples), correct while frozen, and the reference check
is what proves the "bit-identical" half.

**Found on the way: cables to nowhere.** The beat-repeat example wired
a clock's non-existent `gate` out into `freeze`; the freeze never
engaged and the examples sweep passed. `Patch.connect` validates port
names; `Patch.from_dict` does not — it appends whatever the JSON says,
and `_input_buffer` simply never finds a buffer for the dead cable.
Fail soft with nothing to say, the media-path shape again. Added
`test_example_cables_land_on_real_ports` to the sweep, which
immediately caught a second one: **`clock_divider_swing.json` (banked
for ears since 09-11) had `lfo.out → euclidean.fills_cv`, and an LFO's
port is `cv`** — its "fills breathe 2 → 6" never happened. Fixed
(A/B confirms the LFO now changes the render); Matthew's ears item
updated. The loader's own behaviour is a TODO with three options;
(c) keep-and-flag in the node editor is the one that helps.

**Examples** (both banked): `granular_freeze.json` — a `key_trigger`
latch on `freeze` (**tap F**) and a 0.07 Hz triangle LFO on
`position_cv` over a pluck melody: play, tap, the last two seconds hang
and the LFO scans them; tap again. `granular_beat_repeat.json` — the
drum machine into `density` 32 × `size` 62.5 ms (exact 125 ms slices),
a `shift_random` at sixteenths picking the slice point, a 15 BPM clock
on `freeze`: two seconds recording, two seconds held and re-cut. (A
sequencer's gate only follows the clock's pulse width, so it froze 10%
of the time on the first try; the slow clock is the honest switch.)

**Three test constructions again**, all mine: I compared against the
untrimmed input (the helper returns a trimmed copy for a reason); a
param change lands *before* its block, so the capture count stops one
block earlier than I first asserted; and 2 s is not a multiple of 512,
so "toggle at 2 s" toggled at different samples in 64s and 512s — pick
a boundary both sizes share, the rotary's lesson relearned.

**15 new tests; 65 for the module; suite 3416 (122 of those are the new cable-port tripwire, one per example).** 122 examples. The
module is complete against its MODULE_IDEAS spec: three slices,
`8cea850` / `af6a851` / this.

**Yours: 5 commits to push.**

---

## 2026-09-15 (later) — `granular` slice 2: the sprays, the seed, and stereo

Matthew: "granular slice 2 please Claude" — within a message of slice
1 landing, the same rhythm as the 14th.

**What it adds.** The four sprays that turn a grain *stream* into a
grain *cloud*, a `seed`, and `out_l` / `out_r`. Each spray is a random
amount added per grain: `spray_time` scales each inter-onset interval
by a factor in 1 ± s (0 = the synchronous stream with its sideband, 1
= fully asynchronous, mean density preserved either way); `spray_pos`
scatters the read point in `position` units; `spray_pitch` in cents;
`width` pans each grain to a random spot within ±width. All four ship
at zero.

**The recipe held.** Reference renders captured from the shipped
slice-1 code first — four param sets, two block sizes — and after the
edit all eight are `array_equal`. That is not luck: while every spray
and `width` is zero the renderer makes *no draws at all* and the
scalar arithmetic is literally the slice-1 arithmetic (`st = pitch`,
`p = position`, `interval = hop`), so the stream is the same stream.
Every existing `granular_cloud.json` render is unchanged.

**Randomness that survives blocking.** Grain *i* draws its four
uniforms from `default_rng([seed, i])` — a fresh generator keyed by
the absolute grain index, not a stream advanced per block — so the
cloud is a pure function of (seed, index) and the sprayed 64-vs-512
test is bit-exact on all three outs. The pending grain's draws are
cached across blocks (`state["pend"]`), because its interval factor
decides *when* it fires and a block boundary must not re-roll it. A
`default_rng` per grain costs ~30 µs; at 100 grains/s that is 0.3% of
realtime, which is why I did not build the chunked-table version.

**Stereo without changing `out`.** The pan law is constant-peak —
`gl = min(1, 1 − pan)`, `gr = min(1, 1 + pan)`, the rotary's `balance`
law — so a centred grain is at unity in both channels and `width` 0
leaves `out_l` and `out_r` bit-identical to `out` (the renderer skips
the two extra sums and hands back the same array). `out` always hears
every grain at unity, whatever `width` is; a test pins that `out` is
`array_equal` with width 0 and width 1. The dry side of `mix` is
centred.

**Three first-run test failures, all constructions.** (1) A run
detector cannot see an interval shorter than a grain — two overlapping
DC grains merge into one run — so "full spray puts grains closer than
a grain" is tested as *grains fired* (the state's `gi`) versus *runs
seen*. (2) With 10 ms grains scattered over ±0.5 s an impulse is
caught by a grain about 1% of the time; most seeds see zero copies.
Five-hundred-millisecond grains catch it half the time. (3) The widget
collector did not list `add_drag_int`, so the `seed` drag looked
missing.

**Example `granular_haze.json`** (banked): a D dorian pluck melody into
`spray_time` 1, `spray_pos` 0.22, `spray_pitch` 25 ct, `width` 1,
`position` 0.25, `mix` 0.7 straight to L/R — every note dissolves into
a stereo wash a quarter-second behind itself. Banked questions: does
the constant-peak law read as wide or lumpy; where does the sideband
vanish as `spray_time` comes up; is a different `seed` audibly a
different cloud.

**Slice 3 is specced in TODO:** `freeze` (gate + toggle — stop
writing, keep reading; `position` scrubs the held buffer) and
`position_cv` (voice-summed, read per grain at its onset — the
sampler's edge-latch rule).

**13 new tests; 50 for the module; suite 3275.** 120 examples. Cost
~2% sprayed, ~10% at the clumped extreme; `_GR_MAX_GRAINS` 64 → 96.

**Yours: 4 commits to push.**

---

## 2026-09-15 — `granular` slice 1: the cloud, module #92

Matthew: "granular slice 1 please Claude and thank you!" — the next
item off the build queue, and the first of the three slices
`docs/MODULE_IDEAS.md` had pre-cut. Proposed alone, built alone; slices
2 and 3 are their own sessions.

**What it is.** The rack's other grain module, with the opposite
temperament to the pitch_shifter: that one hides its grains (WSOLA
picks their positions so the joins vanish), this one exposes them. The
input is captured into a ring `buffer` seconds long; a scheduler fires
a grain every `sr / density` samples, `size` ms long, read from
`position` seconds back at `2^(pitch/12)` times real speed, shaped by
`window` (hann / triangle / expo); the output is every grain in flight
overlap-added, blended with the dry by `mix`. Mono, `in` → `out`,
Effects. Slice 2 adds the spray (`spray_pos` / `spray_pitch` / `seed`,
jittered onsets) and stereo; slice 3 adds `freeze` and `position_cv`.

**Three decisions that make it exact.** (1) *Absolute indexing*: the
ring is written and read at absolute sample counts modulo its length,
so no part of the renderer knows where a block boundary falls. (2)
*Grains are frozen at their onset*: each is a record of `(onset, read
start, rate, length, window, level)` taken from the params the moment
it fires — a knob turn reaches the next grain, never one in flight.
No zipper, no click, and block-size independence is structural rather
than asserted: the 64-vs-512 test is `array_equal`, with fractional
pitch, density, position and the expo window. (3) *One matrix op*:
every grain in flight is rendered as a `(G, F)` array — `k = n −
onset`, `pos = start + rate·k`, the resampler's 4-tap Hermite read of
the ring, times the window at `k`, masked to the span, summed over G
in spawn order. At the defaults G is 2–3 and it costs ~1.5% of
realtime; at the extreme (100 grains/s × 500 ms = 50 in flight) ~9%.
The per-grain Python loop I nearly wrote would have been ~3× that.

**The neutral is bit-exact.** Periodic hann grains at 50% overlap tile
to exactly one, so the defaults (25/s × 80 ms = 2.0 overlap, `pitch`
0, `position` 0) render the input delayed by the two-sample head start
— and after the float32 cast that is `array_equal`, not `allclose`.
The window mean for the two COLA shapes is the closed-form 0.5 rather
than a measured 0.49999…, and the overlap is computed as `density × L
/ sr` with L an integer, so the normalizer at the defaults is exactly
1.0 rather than 1 − 2 ulp. `position` 0.25 of the 2 s buffer is the
input delayed by exactly 24000 samples; a dense hann or triangle cloud
on DC is exactly 1.0. The `size` is rounded to an even sample count so
"50% overlap" means it.

**The head start.** A grain reading faster than real time would
overtake the write head (at +24 st a 500 ms grain consumes 2 s of
history in its 0.5 s of life), so `position` is floored at `2 + (rate
− 1)(L − 1)` samples and, if the buffer is shorter than that, the
grain is shortened to what the buffer can feed. The `2` is the
Hermite's `+2` tap; the dry side of `mix` is the ring read back two
samples late, so at the neutral dry and wet line up sample for sample
and `mix` 0 is bit-exact. The causality test is the block-size test
at +24 / 500 ms — if a read ever crossed the head, the 64- and
512-block renders would see different unwritten data and diverge.

**The finding worth an ear: the synchronous sideband.** Inside every
grain the pitch is exact (zero-crossing period over the middle of
isolated grains, ±12 / +7 / −5 st, within 0.1%). *Across* grains a
synchronous train chops the tone's phase at every hop, so on a held
440 Hz at +12 the spectral peak lands at ~890, not 880 — the energy
sits on a sideband within ±`density` Hz of the target. That is the
classic un-hidden granular pitch shifter and exactly what the
pitch_shifter's WSOLA exists to remove; the test pins it *as* the
mechanism (within ±density, and not on the source). Slice 2's spray
will smear it. It is also the question banked for Matthew's ears: does
it read as the granular sound, or as out of tune?

**Four first-run test failures, none a renderer bug** — recorded
because each is a reusable trap. A run detector (|y| > 1e-9) was
fooled by the tone's own near-exact zeros (`sin(11πk)` ≈ 1e-16) and
found no grain long enough — measure on the scheduler's onset grid
instead. The very first grain reads two never-written zeros, so its
run starts a sample late — skip hop 0 when checking the hop grid. A 2×
read has an integer stride of 2, so a one-sample impulse on the wrong
parity is never read at all — the sampler's `+12 ≡ buffer[::2]`
property, seen from the other side; use a two-sample impulse. And
"both rates in flight after a pitch change" has to be checked a dozen
blocks after the change, not at the end of the render when the old
grains have long expired.

**Deviations from the MODULE_IDEAS cut, noted.** Category Effects, not
Sources — it needs `in`. `seed` deferred to slice 2 — nothing in slice
1 is random, and a knob that does nothing is worse than a knob that
arrives with the thing it seeds. Default `density` 25 / `size` 80
rather than 12 / 80 so the neutral IS the default (transparent until a
knob moves) — the honest question of whether a fresh node should sound
granular out of the box is in the ears bank.

**Example `granular_cloud.json`** (banked): clock → shift_random →
quantizer (C pentatonic minor) → pluck, into the granular at `pitch`
+12, `density` 30, `size` 120 ms, `position` 0.15 (300 ms back), `mix`
0.6, through a hall to L/R. Every pluck gets an octave-up cloud
trailing behind it. The doc's Patching paragraph names the stutter
(`pitch` 0, `density` 8, `size` 40) and the rain-of-taps (`pitch` −12,
`size` 400, `expo`) settings.

**37 tests; suite 3260.** 119 examples, 92 modules. README's module
counts brought up from a stale 88 while passing (Effects 23,
Modulation 16).

**Yours: 3 commits to push** (the two from the 14th are still local —
`git log origin/main..main` says so, whatever this file says).

---

## 2026-09-14 (last) — `rotary`: the Leslie, module #91

Matthew: "rotary (the Leslie) please Claude :-{D" — the first pick off
the build queue with the love list empty, and the one that has been
"the organ's destined partner" in every note since 2026-08-05.

**What it is.** A rotary cabinet, built as the thing rather than as a
chorus with a story: an LR4 crossover (the crossover module's own
coefficients, zf-carried); a horn band and a drum band each into a
delay ring; per rotor and per mic, a **real Doppler** — the tap sits at
`base − (r/c)·depth·cos θ` samples, the mouth's distance to the mic —
and a **cos-of-angle amplitude** (the horn beams at 0.8, the drum
baffle at 0.45). The numbers are physical: the horn mouth ~19 cm off
axis gives a 0.55 ms swing, which at 6.7 Hz is ±2.3% of pitch, about
40 cents — what a 122 does. Two virtual mics at ±`spread`·90°. `out`
is `(L + R)/2` bit-exact; a `(V, F)` source is summed, because a
cabinet is one physical thing.

**The rotors are the point.** Each has an angle integrating a rate,
and the rate approaches its target (slow / fast / 0) through a
one-pole with separate up and down time constants — exponential, the
way a belt-driven motor accelerates — run as an lfilter so the ramp is
the exact per-sample recurrence and block-size independent. Horn: 1 s
up, 1.5 s down. Drum: 4.5 s up, 6 s down, at 0.85× the horn's rate,
and turning the other way. The horn already whirling while the bass is
still gathering itself IS the Leslie, and `ramp` scales all four times
if the ears want it tighter. `stop` coasts both to rest and freezes
the image. A patched `fast` gate is the switch (block-majority level)
and the combo is ignored while it is cabled.

**Three measurement premises corrected before they became tests.**
(1) The horn band's *Hilbert* envelope over a 3 s slice swung ±40%
with the AM disabled — I nearly went hunting for a bug in the tap.
Ten-millisecond peak envelopes were flat to 0.001; the swing was the
Hilbert transform's own edge transients. Tests use peak envelopes for
AM and 8-cycle-averaged zero-crossing periods for the Doppler, and one
test now asserts the flat-under-pure-Doppler claim by name. (2)
`stop` + `depth` 0 "is the input through a fixed delay" — false as a
waveform: the LR4 low + high sum is an *allpass*, flat in magnitude
and phase-rotated; correlation read 0.86. The test compares band
energies (six bands, ±0.5 dB). (3) Block-size independence with the
speed switch mid-run: the switch is a per-block level read, so it has
to sit on a boundary both sizes share (512 × 94); then the physics
matches to 1e-5.

**A design tweak the tests forced:** the centre delay is an integer
number of samples (ceil of 2 ms + the horn's swing) — the
delay-matched dry read for `mix` < 1 is then exact, and `mix` 0
renders the input shifted by that integer, bit-exact.

**Example `organ_leslie.json`** (banked): a self-playing maj7 organ
(clock → sequencer → quantizer → chord, first inversion → organ, 888 4)
through the rotary to L/R, with a 5 BPM clock at 50% duty on `fast`
flipping it every six seconds. The test drives it 12 s and asserts the
horn rate went above 5 Hz and below 1.5 Hz — it went fast AND slow.

**24 tests; suite 3221.** 118 examples, 91 modules. Cost ~2.3% of a
block at 48 k / 512.

**Yours: 1 commit to push** (origin moved to ab0bb26 while this was
being built — the other three went up mid-session).

---

## 2026-09-14 (late) — slew v2: rise_cv / fall_cv + clock sync

Matthew: "slew v2 (rise_cv/fall_cv + clock sync) please Claude and
thank you!" The last item on the love list, filed the day the module
was born (2026-07-18).

**`rise_cv` / `fall_cv`.** 1 V/oct on that side's rate — the function
generator's law, so the two modules read the same way: +1 halves the
time, −1 doubles it, ±5 octaves, block-mean. Per voice when the CV is
`(V, F)` against a `(V, F)` input (velocity into `fall_cv`: hard notes
glide differently from soft), one law for every row otherwise. The
per-row octave is one 1-D `np.mean` per row, not `.mean(axis=1)` —
the first draft used the axis form and a voice row fed a constant
differed from the mono render by an ulp (different reduction order);
one reduction per row is the same op the mono path runs, so the
bit-equality is structural again.

**Clock sync = tempo-relative times.** With a `clock` cabled, the two
times are multiples of its period (1.0 = one pulse; 0.6 = most of a
sequencer step) instead of seconds. The period is the gap between the
last two rising edges on an absolute sample counter carried across
blocks (the euclidean convention — a 150-sample period measured across
64-sample blocks reads 150). Until two edges have been seen the times
are seconds; unpatch the clock and they are seconds again, with the old
period forgotten. Clock and CV compose: 0.5 × 200-sample period = 100,
`rise_cv` +1 makes it 50. The other reading of "clock sync" — sample
the input on clock edges, then glide — is `sample_hold → slew` already,
so I didn't build it in.

**Engines.** The symmetric-exponential lfilter fast path survives:
uniform times → one call over every voice as before; per-voice times
with rise == fall per row → one lfilter per row (still C speed; parity
with the scalar recurrence pinned). Everything else is the scalar scan
with per-row coefficients. With nothing patched the coefficient
arithmetic is literally the old expressions on the old floats, and the
15-array reference sweep is `array_equal`.

**A test premise corrected.** My reach helpers looked for the 99.9%
point and the exponential cases came out 1.5× long — because an
exponential time in this module *means* the 99% point (`_LN100`). The
helpers now use 0.99 / 0.01 and say why. Same lesson, third time
today: know what the number means before writing the threshold.

**Example `slew_clocked_glide.json`** (banked): an 8-step line through
an exponential slew at 0.6 × period, the clock cabled, a slow bipolar
LFO into `rise_cv` breathing the rise speed ±1 octave. Measured period
12027 samples at 110 BPM and 6014 at 220 — the glide keeps its fraction
of a step when the tempo moves, which is the whole point.

**16 tests (16 → 32 in the file), suite 3195.** Perf: mono linear
0.67% → 0.90% with CV + clock; 16-voice per-voice CVs about a point
over the scalar path.

**The love list is empty.** Six passes in one day: fgen ×3 (well, two
today), pitch_shifter, chord/arp, slew. Nine examples in the ears
bank. Next is the build queue.

**Yours: 3 commits to push.**

---

## 2026-09-14 (night) — chord/arp extras: inversion, changed + retrig, internal clock

Matthew: "chord/arp extras (inversion, changed re-strum, arp internal
clock) please Claude :-{D". The three "Later" notes from the day the
pair shipped (2026-08-03), all at once.

**Chord `inversion`.** 0..3: n times, the lowest *sounding* note goes
up an octave (ties: lowest slot). `major` 0/4/7/12 → 12/4/7/12 (E in
the bass, root doubled up top) → 12/16/7/12 (G in the bass) →
12/16/19/12. It runs after spread and skips disabled slots, so a
spread major inverts slot 3 (the −5) first and a `5` power chord
inverts over three voices. **Rows keep their slot identity** — only a
slot's pitch moves. That was the design call: re-sorting rows by pitch
would have re-shaped downstream per-voice state on every inversion
change and altered the strum order; keeping the slot means an
inversion step is a pitch change and nothing else.

**Chord `changed` + `retrig`.** The re-strum trigger. `changed` is a
mono gate out that pulses ~2 ms when the sounding chord changes under
a held gate: the root jumping ≥ half a semitone between consecutive
samples (any sequencer/quantizer step; no glide ever — a one-octave
glide over 100 ms at 48 k moves 0.0002 V a sample), or the interval
set changing (preset, inversion, spread, a slot edit — lands at sample
0 of the block it arrives in). Masked to samples where the gate was
*already* high: a fresh press is its own trigger and doesn't double up.
`retrig` makes the module act on it: every row drops for exactly that
one sample and the strum stagger restarts from the next, so whatever
the rows drive gets a fresh rising edge — a legato root walk becomes
articulated with no extra cable. Both are carried across block joins
(pulse remainder, pending onsets — the burst/strum machinery already
there). With `retrig` off the fast paths (mirror / steady) are taken
exactly as before; the `changed` jack is computed vectorised up front
either way.

**Arp internal clock.** `bpm` × `division` steps per minute (the
`clock` module's pair), used only when `clock` is unpatched. Integer
period — 97 bpm × 3 at SR 1000 asks for 206.2 samples, gets 206 every
time, never 205/207 (the fgen lesson) — high for half a period so the
first step's gate mirror has a width. `reset` re-phases the counter to
0 at the reset sample, and that sample is a step **by decree**, not by
edge: the test caught that the line was still high from the previous
step (each step holds half a period), so a rising-edge detector saw
nothing. The fix is a `forced` set of reset samples OR'd into the edge
test. A cabled clock makes both params inert, pinned `array_equal`.

**The one intended behaviour change.** An arpeggiator with nothing on
`clock` used to be dead; now it free-runs at 120 × 4. The 52-array
reference sweep shows exactly that split: every chord array and every
clocked-arp array `array_equal`, the six unclocked-arp pairs different.
No shipped example had an unclocked arp (checked all 115).

**Two test premises corrected before they lied.** (1) My `_edges`
helper appended the sample-0 edge at the *end* of the list. (2) The
reset test expected a visible gate edge at the reset sample with
`gate_len` 0.5 — the previous step's gate was still high there, so
the re-phase showed in the pitch only; the test now uses 0.2 so the
edge is observable, and the docstring says why.

**Example `chord_legato_inversions.json`** (banked): a 24-second held
gate (a 2.4 bpm clock at 98% duty) with a quantized root walk stepping
under it once a second; the chord is maj7, first inversion, 20 ms
strum, `retrig` on — measured re-strums at 1.0 / 2.0 / 3.0 s on all
four rows 20 ms apart; the arp takes the same four rows on its internal
140 × 4 clock — measured period 4725 samples exactly. The question for
the ears is whether a one-sample gate drop reads as a clean
re-articulation through the ADSR or wants a longer gap.

**23 tests (chord 20 → 34, arp 28 → 37), suite 3177.**

**Yours: 1 commit to push.**

---

## 2026-09-14 (evening) — pitch_shifter: shimmer, harmonizer, stereo spread

Matthew: "pitch_shifter shimmer/harmonizer please Claude." The
2026-07-10 love directions, taken up at last. (He pushed 91b8dfb while
I was reading the engine.)

**Three knobs, all off by default, all on the same module.**

* **`feedback`** — the shimmer. The previous block's main wet, damped
  by a one-pole at 6 kHz and soft-ceilinged at the matrix knee, is added
  to this block's engine input, so every lap is shifted again: at +12
  an octave cascade. The loop is one block plus the engine's own
  latency long (a grain or two), so this is the fast spectral bloom,
  not the slow cathedral shimmer — `organ_shimmer.json` with its delay
  in a matrix loop is still the route to that, and the docs say so. The
  damping is the thing that makes it usable: undamped, laps sail past
  Nyquist and come back as alias grit; damped, the climb dies against
  the top of hearing. `_PS_FB_LP_HZ` is a constant, like the matrix
  knee — a `damp` knob is a follow-on if the ears want it.
* **`harmony` / `harmony_level`** — a second `_GrainShifter` per voice
  at `harmony` semitones, mixed at the level. It hears the *raw* input,
  not the loop, so it stays a clean interval over whatever the shimmer
  is doing. Same `pitch_cv`, so the chord transposes as one. Built
  lazily the first block the level rises and torn down at 0, so a
  patch that never turns it on never pays for it.
* **`spread` + `out_l` / `out_r`** — with a harmony present, the main
  leans left and the harmony right (the far channel fades by `spread`);
  the dry stays centred. Without a harmony both jacks *are* `out` — the
  resampler's "wired stereo plays mono" convention. I chose the pan over
  the resampler's ±cents detune meaning of `spread` deliberately: the
  pan costs nothing and is what a harmonizer's stereo field is; a
  detune spread would be two more engines for a chorus the [chorus]
  already does.

**The refactor underneath.** The per-engine "detect period → regrain
→ process → equal-power splice" block became `_ps_shift(state, sfx,
…)`, and the re-colour + level valve became `_ps_recolor`; main and
harmony both call them with their own state keys. The main path's
operations are unchanged — 72 reference arrays (six settings including
formant on, tone/bass/noise, cv on/off, mono and voice, 40 blocks
each) captured before the first edit are `array_equal` after. The
renderer now returns `{"out", "out_l", "out_r"}` like the resampler;
eleven direct calls in the test file grew a `["out"]`.

**A test that measured the wrong thing, caught before it lied.** "The
harmony does not hear the loop" — my first draft asserted that the
+7-over-the-first-lap partial (659 Hz) was not raised by feedback, and
it failed: −82 dB → −52 dB. But −52 dB against peaks at +70 dB is the
shimmer's own noise floor, and it is there with no harmony at all. The
honest claim is that a harmony fed the loop *would* put a real partial
there (~+60 dB), so the test now asserts the lap-fifth sits ≥ 60 dB
under the harmony's own partial. Same lesson as the fgen curves: know
what the number means before you write it.

**Two examples, both banked for ears.** `pitch_shifter_shimmer.json`
— a slow pluck dropped to C3 (a constant −1 V into its `pitch_cv`; at
C4 the laps ran out of room) into +12 at `feedback` 0.75, through a
hall to L/R: laps at 1046/2093/4186 Hz measure +9/+9/+22 dB over the
same patch with feedback 0. `pitch_shifter_harmonizer.json` — a saw at
E3, +4 / +7 at level 0.9, `spread` 1: root 73.5 dB in both channels,
third 69 dB left vs −19 right, fifth the reverse. A stereo major triad
from one module.

**18 tests (44 → 62 in the file); suite 3152.** Cost, mono at 512: +7 st 2.4% → shimmer 3.0% → harmony 4.2% →
all + spread 4.7% → all + formant 8.4%. Four voices: 8.7% → 18.2%
(32.9% with formant).

**Yours: 1 commit to push.** *Pushed the same day — 91b8dfb..7de1be3.*

---

## 2026-09-14 (later) — function generator: per-voice rise/fall rates

Matthew: "per-voice fgen rates — execute please Claude." The caveat
from the morning's pass, closed the same day. (He also pushed through
0eeedd9 while I was capturing references — `git log origin/main..main`
is empty as of this entry's commit going in.)

**What changed, and what didn't.** `rise_cv` / `fall_cv` are now read
with `collapse=False`. When one arrives `(V, F)` alongside a `(V, F)`
trigger with the same V, each slot gets its own `(rise_len, fall_len,
pulse_len)` from its own row's block-mean; `rate_cv` is still collapsed
— "both" is one knob on the hardware and one tempo in the rack. The
kernel is untouched: the stage lengths were already arguments, which is
why this was small. The rate arithmetic (octave clamp, `1/2**oct`,
integer lengths, pulse width) moved into one `_fg_lengths` helper that
the mono law and the per-slot law both call — so "a voice row is
bit-identical to the mono path fed that row's constant" is structural
again, and the test pins it at three octave values on all four jacks.

**Every other shape is the summed mono law from this morning.** A mono
CV; a voice CV into a mono trigger (no voice functions to give rates
to); a V that doesn't match. `_octaves` sums a 2D CV across slots
before the mean, exactly as `_input_buffer`'s collapse did, and the
432-array reference sweep is `array_equal` — nothing already cabled
moved. Two of those fallbacks are pinned explicitly, because they are
the kind of thing that silently changes when someone "tidies" later.

**The parked-slot detail.** `velocity_cv` holds 0 on a slot with
nothing playing. A 0 row solves to the shared law's lengths, so that
row renders exactly as with no CV cabled, and the idle-skip in the
voice path still applies — the test checks the slot's *state* is still
`_FG_IDLE`, not just that its output is zeros.

**One of yesterday's tests was rewritten, not deleted.** It pinned
"a (V, F) rise_cv is summed to one rate" — true then, wrong now by
design. It now pins the same claim for `rate_cv`, which is the jack
that should stay that way, and says where the per-voice tests live.

**Observation, not fixed:** in `loop` mode a voice slot that has never
been triggered stays parked (the idle-skip runs before the kernel's
"kick out of idle"), whereas the mono path free-runs unpatched. A poly
keyboard into a loop-mode fgen only loops the slots that have been
played once. Defensible — sixteen free-running kernels would cost the
whole 35% whether or not anyone is playing — but it is an asymmetry
worth knowing about. Noted in TODO as a question, not a defect.

**11 tests (82 in the file), suite 3130.** Perf: 35.8% at 16 voices
with a random per-voice `fall_cv`, 35.0% without — the sixteen
`_fg_lengths` calls and one `mean(axis=1)` are noise.

**Yours: 1 commit to push.**

---

## 2026-09-14 — function generator: per-slope CVs and the inverted out

Matthew: "fgen follow-ons 2..4 — rise_cv/fall_cv as separate CVs (the
'both' CV already exists as rate_cv), and an out_inv jack." Follow-on 4
(`eor → trig`) is the compiler job and stays queued; 2 and 3 shipped.

**Reference first, again.** 432 arrays captured from the shipped code
before the first edit — three modes, four curve settings (including the
per-slope pair), four `rate_cv` states (unpatched / 0 / +1 / −0.7), two
consecutive blocks so state carries, mono and voice, every jack — and
all 432 `array_equal` afterwards. Fourth outing for the recipe; it
takes five minutes and it is the only thing that lets "nothing moved"
be a statement rather than a hope.

**The rate law.** `rise_cv` / `fall_cv` are `rate_cv`'s 1 V/oct on one
slope each, and they **sum in octaves** with it — that is what Maths
does with its per-channel jacks next to "both", and it is what makes
the three compose: `rate_cv` sets the tempo, the pair skews it. The
clamp moved from the "both" value to the per-slope *total*, which is
where it has to be once there are two contributions; with the new jacks
unpatched the total IS the old value, so the clamp, the scale and every
downstream sample are bit-identical (the reference sweep proves it, and
a test pins the cabled-at-zero case too). `rise_s *= 1/(2**oct)` runs
only when the octave sum is non-zero — `x * 1.0 == x` exactly anyway,
but there is no point doing it.

**`out_inv` is outside the kernel.** `1 − out` on the finished block,
one numpy subtraction per block or per `(V, F)` array. Keeping it out
of the scalar loop means the loop, and its mono ≡ voice bit-identity
claim, are untouched; the 16-voice cost measured 35.3% of a block
(was 34.6%; noise), mono 2.3% (unchanged). A parked slot's row is 0.0
so its inverse is 1.0 — the ducker is OPEN on a silent voice, which is
what "1 − out" has to mean, and the test says so in words. Verified
end to end through `render_block`: clock → `trig`, `out_inv → vca.cv`
on an oscillator, LFO into `fall_cv` — a 13:1 duck that lands on the
trigger sample and swells back over the fall.

**The honest caveat, written into the docs rather than discovered by
Matthew.** The obvious patch for `fall_cv` is velocity → longer ring.
`midi_input.velocity_cv` is `(V, F)`, and these jacks collapse to mono
the way `rate_cv` does — by `_input_buffer`'s **sum** across slots. One
note at a time that is exactly the trick; a held chord sums the
velocities and shortens *every* voice's fall by octaves. I kept the
pair mono for this pass (it is the spec as given, and it is what
"same law as `rate_cv`" means) and queued **per-voice rise/fall rates**
in TODO as its own small item: the kernel already takes
`rise_len`/`fall_len` as arguments, so the voice path can compute them
per slot when the CV is 2D. `rate_cv` should stay mono — "both" is one
knob on the hardware.

**Tests: 17 new, 54 → 71 in the file, suite 3119.** Each CV moves only
its own stage length with the other slope sample-for-sample unchanged;
the octave sum (`rate_cv` +1 with `rise_cv` +1 is ×4 on the rise while
`fall_cv` −1 cancels the fall back to the knob); the ±5 clamp on the
*total* (+4 and +4 gives ÷32 not ÷256, −3 and −3 gives ×32); a
loop-mode period of `rise/2 + fall·2` with zero drift over 40 blocks
(the integer-counter claim survives the split); a `(V, F)` CV collapses
to one rate for all voices; voice ≡ mono with all three CVs cabled;
`out_inv` bit-exact against `float32(1) − out` in every mode, sums with
`out` to exactly 1.0, hits 0.0 on the `eor` sample and 1.0 on the `eoc`
sample, 1.0 at rest and on parked slots, and is a cv jack that reaches
a `vca` and is refused by an audio sink. The `_free` helper grew the
new jack — it had a hard-coded three.

**Docs.** MODULES.md index row + port table + three patching recipes
(ducker, velocity-dependent decay with the caveat, wandering clock with
fixed swing). Module docstring likewise. No example patch: the ducker
is a one-cable idea and the existing krell example is unchanged.

**Yours: 3 commits to push** (30b9384, 624a3ef and this one). *Pushed
the same day — 830ce19..0eeedd9.*

---

## 2026-09-12 — function generator: one curve knob per slope

Matthew: "function generator follow-ons — curve_rise/curve_fall (one
knob per slope, like Maths) please". The first of the four follow-ons
queued when the module shipped.

**The call: offsets, not replacements.** `curve` must survive — a saved
patch with an unknown param name raises at load (`Module.__init__` is
strict; `clock_divider_swing.json` reminded me the same day when I gave
an `lfo` an `amp` it never had). So `curve_rise` / `curve_fall` are
*added* to `curve` for their slope and the sum clamped to ±1: `curve`
stays the "both" knob, the pair skews it. Both default 0 → the kernel
sees the same exponent on both slopes → the render is `array_equal` to
references captured from the shipped code before I touched anything
(trigger / gate / loop, four curve settings, all three jacks). The
love-pass recipe, third time.

**The kernel change is small and exactly where it should be:** `k` and
`inv_k` became `(rise, fall)` pairs, and every backward solve on a
stage entry uses the exponent of the stage being *entered* — the rise's
when a trigger restarts the rise, the fall's when a gate releases.
That is what keeps it click-free when the two slopes disagree.

**What "click-free" honestly means here, and a test that nearly lied.**
My first draft asserted `max |diff| < 0.01` on a release with
`curve_rise` +1 into `curve_fall` −1 and got 0.012; then 0.058 on a
retrigger into a k = 1/4 logarithmic rise. Neither is a discontinuity
in the *solve* — a logarithmic slope at k = 1/4 over 4800 samples
genuinely goes 0 → 0.12 in its first sample (`(1/4800)^0.25`), and
lands at 0.12 → 0 in its last. Any level below 0.12 maps to counter 0,
so the next sample *is* 0.12: that is the shape. The honest claim is
"the interrupted render's worst step is no worse than the same curves'
worst step uninterrupted" (counting the leap from rest), plus a gentle
case (±0.5) that pins the step at the retrigger sample itself under
0.005. Pinned that way. The general lesson — decide what the claim IS
before writing the number — is [[dsp-test-design-lessons]] again.

12 tests, suite **3102**. No example — the pair is a refinement, and
`krell_machine.json` already shows the module. Follow-ons 2..4 remain:
`rise_cv` / `fall_cv`, `out_inv`, and `eor → trig` once the feedback
door generalizes.

**Environment note, not code:** numpy refused to import this session —
`OpenBLAS error: Memory allocation still failed after 10 retries` —
with ~960 MB physical free and Matthew's own `syncplay` and `densitas`
processes resident. `OPENBLAS_NUM_THREADS=1` in the shell fixed it for
every run here (fewer per-thread buffers). Nothing in the repo changed
for it; worth remembering if a future session sees the same wall.

---

## 2026-09-11 (fourth pass) — clockwork love: two CV jacks and the divider

Matthew's pick after modal. Two of the three `Later:` items were CV
inputs on shipped modules; the third was a module that had been sitting
in MODULE_IDEAS with a full spec since 2026-08-04.

**`euclidean.fills_cv` / `burst.count_cv`** — the edge-latch idiom, a
fourth and fifth time. Both carry a `*_cv_depth` per the house rule
(8 per unit, so 0..1 V sweeps eight). The euclidean reads its CV at
*each clock edge* and rebuilds the pattern there when the count
changed — so the step that fires is decided by the fill count the CV
had when the clock ticked, and a slow LFO into it breathes a rhythm
from sparse to dense. The burst reads at the *trigger* edge and latches
for the burst: the count is a property of the ratchet. Unpatched, both
render `array_equal` to before.

**`clock_divider`** — module #90, built to the spec verbatim:
`div2`/`div4`/`div8`, a `divn` with `swing`, a `mult` with `m`. The
design calls: (1) edges since reset are *counted* and edge `i` fires
division `k` when `i mod k = 0`, so the first edge after a reset is the
downbeat on every output at once — the way a hardware divider's reset
works; (2) gates are pulses of `pw × that output's period`, scheduled
at absolute sample positions from the last measured interval (the
burst's precedent), which is what makes "division counts exact over
1000 edges" an exact integer assertion and block-size independence an
`array_equal`; (3) before an interval exists — the very first edge —
every output mirrors the clock's own high time, the euclidean's rule,
so there is no silent first beat; (4) `swing` is a fraction of the
`divn` period applied to every second emitted gate (0.33 = triplet,
0.5 = dotted), scheduled rather than mirrored since it no longer sits on
an edge; (5) `mult` fires on the edge and schedules `m − 1` more at
`interval·k/m`, and any still pending are *dropped* when the next real
edge arrives — a tempo change costs exactly one period of stale
subdivisions, as the spec asked for and the test pins (halve the
tempo: two fast edges later the midpoints are on the new grid).

**The example is the reason to build it:** `clock_divider_swing.json`
takes a straight sixteenth clock and gets swung closed hats out of
`divn(1)` + `swing 0.33`, the kick off `div4`, open hats off `div8`,
and a triplet ratchet on a rimshot from `mult(3)` clocking a `burst`
whose `env` feeds the rim's new `vel`. Plus a 30-second LFO into the
snare euclidean's `fills_cv`, 2 fills up to 6 and back.

21 tests, all green first run (the divider's scheduling is the burst's,
already debugged). Suite **3088**. Wants ears — banked.

**Still on the clockwork list:** a 3+-way bernoulli sibling
(`sequential_switch` is on the quick-hit list) and a `rotate_cv` for the
euclidean, which would be the same edge-latch shape again.

---

## 2026-09-11 (later still) — modal love: where, with what, and in stereo

Matthew's pick after the drums — the module he had flagged twice for a
longer revisit. The three `Later:` items from the 2026-08-03 ship note,
each a knob that is OFF at 0:

* **`position`** — the strike-position comb. The pluck already had this
  idea as a delay-and-subtract on its exciter; on a resonator bank the
  same physics lands on the mode gains directly: `|sin(π·ratio·pos)|`,
  a mode silent when the strike sits on its node. Renormalized after,
  so striking near the edge is *thin*, not *quiet* — a slider end that
  went silent would be a trap (the sampler's inverted-loop lesson). For
  a harmonic string it is the textbook plucked-string spectrum (0.5
  kills every even harmonic, pinned >30 dB); for bar/bell/membrane the
  ratio stands in for the mode's spatial period, which is a stylization
  and documented as one. A position that nulls everything (1.0 on a
  string) falls back to off rather than dividing by zero.
* **`mallet`** — the pitch-tracking excite filter. One-pole low-pass on
  the strike at `f0 · 2^(6·(1−m))`: six octaves above the fundamental
  at a nudge, the fundamental itself at 1. Tracking is the point: a felt
  mallet should read as the same softness across the keyboard, and a
  fixed-Hz filter leaves the top notes duller. Pinned as exactly that
  claim — the same rolloff between harmonic 1 and 4 an octave apart,
  within 3 dB (the first draft measured 5.4 dB apart and I nearly
  loosened the tolerance; the culprit was the *noise burst's* own
  spectral ripple at the different measurement frequencies, not the
  filter — an impulse strike has a flat spectrum and the claim holds at
  well under 3 dB. Measure the mechanism, not the stimulus.)
* **`spread`** — stereo. `out` is untouched (still the mono sum, still
  the array the tests pin); `out_l`/`out_r` accumulate a second pair of
  sums with per-mode equal-power pans. Two calls worth keeping: pans
  follow a golden-ratio scatter by mode index rather than odd-left /
  even-right (which is a lattice you can hear), and the gains carry a
  √2 so a centred mode contributes 1.0 to each side — at spread 0 the
  outs ARE `out`, bit-identical, the chorus/resampler contract. Pinned:
  L²+R² = 2·mono² per mode, and each mode's side follows its pan.

**The verification that matters:** before touching anything I rendered
four materials and a voiced case with the shipped code and saved them;
after, the default render is `array_equal` to every one. The knobs at 0
skip their code paths entirely — no "multiply by 1.0", no "add zeros".

**One bite.** The mallet one-pole broke block-size independence on the
first run: I carried lfilter's `zf` as the state and rebuilt `zi` as
`(1−coef)·state` next block — but `zf` already *is* `(1−coef)·y_last`,
so the factor was applied twice. The octaver's idiom is to carry the
last OUTPUT and rebuild `zi` from it, which also survives a coefficient
change between blocks (a pitch glide) without kicking the filter. Same
shape as [[dsp-test-design-lessons]]'s "know what the state variable IS".

**Cost:** 16 voices × 24 modes at 16 distinct pitches, 31.4% of budget
before, 39.4% with all three on (the spread's two extra multiply-adds
per mode are most of it). 17 tests; suite **3065**. Example
`modal_mallets.json` — a stereo marimba with an xy scope across L/R so
the spread is something you can *see*: banked with the other ears items.

---

## 2026-09-11 (later) — drums love: velocity, a tuned kick, a hat with a tone knob

Matthew banked the sampler ears pass ("where I am is noisy") and asked
for "modules that need some love". I walked every `Later:` note on a
shipped module and offered seven; recommended the drums because
`midi_input.velocity_cv` had just been born and the trio had no way to
hear it. "Drums please Claude :-{D".

**What landed.** `vel` on kick, snare and hat; `pitch_cv` on the kick;
`tone` on the hat. 18 tests, suite **3046**. Example `drum_dynamics.json`.

**The calls:**

* *`vel` is the sampler's edge-latch idiom, verbatim.* Read at the
  trigger edge, latched into the hit, unpatched = 1. The whole-hit
  buffer design made it trivial: the buffer is synthesized at the edge,
  so "latched" is free — multiply once and it is a property of that hit
  forever. Pinned: unpatched is `array_equal` with the pre-change render
  (every jack, every drum), what the CV does after the edge changes
  nothing, negative is silence.
* *A `(V, F)` velocity bus collapses to the MAX at the edge sample, not
  the house sum.* The house rule sums voice rows for mono consumers,
  which is right for audio and wrong for velocity — sixteen slots'
  velocities added together is nonsense, and idle slots carry 0, so the
  max is precisely the key that was just struck. One helper
  (`_drum_edge_value`) does it for both `vel` and `pitch_cv`. Pinned
  with a 4-row bus (0, 0, 0.6, 0.3) → the 0.6 hit.
* *Kick velocity goes in BEFORE the drive.* `tanh(g·vel·body)/tanh(g)`:
  a soft hit stays clean, a hard one saturates — the way the circuit
  would. Pinned as a ratio: clean scales linearly (allclose), driven
  half-velocity RMS sits between 0.5 and 0.95 of full. At `vel` 1.0 the
  multiply is skipped entirely so the closed-form pin is untouched.
* *Kick `pitch_cv` is the calibrated bus*, no depth knob — the
  oscillator/pluck/fm_op/sampler convention, and the CV-depth table says
  so. Stacked on `tune` as `tune + 12·cv`, latched per hit. Pinned: +1 V
  and `tune` 12 are the same hit to 1e-6, and a hit keeps its pitch while
  the CV moves an octave underneath it (the second hit takes the new
  pitch; compared past the 2 ms retrigger fade, which was the first
  draft's mistake — I'd written the expectation as a plain sum).
* *Hat `tone` is the stack base, 200..1600.* The HP at 7 kHz is fixed, so
  what changes is the DENSITY of partials above it — and that showed up
  in the test: the spectral **centroid** barely moves (12.54 kHz vs
  12.57 kHz between 400 and 200 Hz; the band is the band) and my first
  draft asserted the wrong direction. Spectral **flatness** above 7 kHz
  is the right observable — 0.49 / 0.41 / 0.36 / 0.20 for 200 / 400 /
  800 / 1600 Hz, monotone — and that is what the docs now say the knob
  does: trashy vs thin, not dark vs bright. **Measure the observable the
  mechanism actually moves** — [[dsp-test-design-lessons]] again.
* *No per-drum `gain_cv`.* The old note listed it; `vel` covers accents
  and a VCA covers continuous level. Two multipliers on one drum would
  be a trap. Noted in TODO rather than built.

**Love candidates still on the list** (from the 2026-09-11 sweep of
`Later:` notes): **modal** (strike-position macro, stereo mode spread,
pitch-tracking excite filter — Matthew flagged it twice for a revisit);
**clockwork** (`euclidean.fills_cv`, `burst.count_cv`, `clock_divider`);
**function generator** (`curve_rise`/`curve_fall`, separate rise/fall
CVs, `out_inv`); **pitch_shifter** (feedback shimmer, harmonizer /
stereo spread); **chord/arp** (`inversion`, `changed` re-strum, arp
internal clock); **slew v2** (`rise_cv`/`fall_cv` + clock sync).

**Wants ears:** `drum_dynamics.json` — banked with the sampler patches.

---

## 2026-09-11 — sampler slice 3: the stretch menu, all of it

Matthew: "can we run sampler slice 3 then I want to focus on the modules
for a while". Slice 3 was the spec's stretch menu — six items — and all
six shipped in one commit, the way slice 2 did. The sampler is now
feature-complete against `docs/MODULE_IDEAS.md`.

**What landed.** `out_l`/`out_r` beside `out`; an on-load mip chain
behind an `antialias` tickbox; `start_cv` (+ `start_cv_depth`); `reverse`;
`vel`; and a waveform face on the node. Plus one port on a neighbour:
`midi_input` grew a `velocity_cv` out, because nothing in the rack had
ever *emitted* velocity — the MIDI module applied it to its own tone and
kept it to itself — and a `vel` input with nothing to patch into it would
have been a dead jack. Example `sampler_scrub.json`: a `shift_random` into
`start_cv` re-cuts `breaks.wav` on every sixteenth, plus a sparse reversed
stab a fifth up off the same CV. 36 new sampler tests + 1 on midi_input;
suite **3027**.

**The design calls, and why:**

* *`antialias` defaults OFF.* Slice 1 documented the crunch as the sound
  and the ears passed on it; a tickbox keeps the shipped sound and adds
  the clean one. Same reasoning as leaving the region end unfaded.
* *The chain is decimated, not just filtered.* A first draft kept
  full-length lowpassed copies — simplest to read (same positions) but 6
  copies × 46 MB for a 120 s file. Decimating 2:1 per level costs under
  half the original in total AND gives the exactness back: level `k`
  sample `j` sits on original position `j·2^k`, so a read at rate `2^k`
  from level `k` is that level verbatim (pinned: octave-up with the chain
  on is `chains[0][1]` array_equal). Levels above 0 are float32 — they
  are filtered copies and make no exactness claim, so they need not cost
  double. Level 0 stays the float64 original, which is why the neutral
  is bit-exact with the box ticked (pinned).
* *Between octaves it crossfades two levels* (the wavetable_morph rule)
  rather than snapping to the conservative one — the oscillator's
  ceil-pick would halve the bandwidth the moment a note went one cent
  above the root, which is fine for a saw table and awful for a
  recording. The cost is honesty: at an exact octave the fold is gone
  (>40 dB on the test tone), at a fifth it is attenuated by level 0's
  weight (~8 dB) and no more. Both are pinned as two-render A/Bs, and a
  third A/B pins that the wanted tone keeps its frequency and level
  (within 1 dB) — the two levels are read in phase because the half-band
  FIR is centred, so their crossfade sums to unity in the passband.
  Documented as "honest rather than perfect".
* *`start_cv` is read AT THE EDGE SAMPLE and latched*, not block-mean. A
  sequencer step and the gate it fires land on the same sample; the
  slice point has to be what the CV said *then*. Pinned with a CV that
  steps at the hit and moves again mid-hit. A start pushed past `end`
  drops the hit and leaves the sounding voice alone (pinned) — a slicer
  that went quiet on a bad CV value would be a trap.
* *The loop rides along with `start_cv`.* Slice 2 made the loop a
  fraction of the region; slice 3 makes the region per voice (each hit
  has its own `start`), so the loop is resolved per voice per block from
  that voice's start. Live drags of `loop_start` still move a held note,
  and a scrubbed loop tiles the file from where *that hit* began
  (pinned: `tile(data[n//2:], 3)`).
* *Reverse starts one sample inside `end`.* `end` is exclusive going
  forward (positions `< end` play), so `end − 1` is the mirror of
  `start`, and a unity reverse is `data[::-1]` bit-exact (pinned), a
  reversed region is the region mirrored, a reversed loop is a
  bit-exact tiling of the region backwards, and reverse-an-octave-up is
  `data[::-1][::2]`. The loop wrap is the same modulo on the affine
  array: `lo + mod(raw − lo, L)` for positions below `lo`. The seam
  crossfade mirrors — fade zone just above `loop_start`, reading one lap
  LATER (`pos + L`), which is what runs into `loop_end` from above.
  Pinned with the ramp A/B mirrored: hard step >0.9, faded <1/50 of it.
* *`vel` is latched at the edge and the retrigger tail keeps the OLD
  gain.* Velocity is a property of the hit. The 2 ms crossfade tail of a
  re-struck voice now carries `xf_gain` (and `xf_start`, so it keeps its
  own region and loop too) — a quiet note under a loud one must not step
  the tail (pinned: max sample delta in the tail < 0.05).
* *Stereo: `out` is still the mono sum, read once for a mono file.* The
  loader keeps one chain when the file's two rows are identical (every
  mono file, and a dual-mono one) and two otherwise; a stereo file reads
  both and `out` is their half-sum. Per-channel passthrough is bit-exact
  (pinned). Copies rather than the same array for `out_l`/`out_r`, the
  precedent elsewhere in the backend.
* *The face repaints only on change.* It is a picture of a file, not a
  scope: `_update_sampler_faces` keys on (path, region, loop, mode,
  reverse) and does nothing when the key matches (pinned: second call
  makes no `configure_item`). The overview (200 min/max columns) is built
  by the loader, off-thread, and handed over through a
  `sampler_overview(id)` hook — the scope_window shape.

**Two things that bit.**

* `np.convolve(x, taps, mode="same")` returns the *kernel's* length when
  the signal is shorter than the kernel — a 40-sample file came back 63
  long, misaligned. Fixed with zero-pad-by-half + `mode="valid"`, which
  is exactly N out for N in, centred. The short-file test caught it.
* The MagicMock-distinct-ids lesson, again: `dpg.draw_line` returns the
  same mock for every call, so four markers collapsed into one dict key
  and the face test read the wrong line. `side_effect = itertools.count`
  on the draw_* calls. (Third time this has bitten a widget test — it
  is in the memory file and it still cost a cycle.)

**Cost.** At 16 voices, 512-sample blocks: mono naive 9.6% of budget
(unchanged), mono `antialias` between octaves 17.5%, stereo naive 17.4%,
stereo `antialias` between octaves 31.3% — the worst case is the
two-reads-per-channel one and still sits below the supersaw's 45.6%.

**Wants a human:** ears on `sampler_scrub.json` (run
`generate_samples.py`; the same breaks.wav); eyes on the sampler node's
waveform face (markers should sit at start/end, loop markers appear only
in `loop` mode and inside the region, "<< reverse" caption when ticked);
and `midi_input.velocity_cv → sampler.vel` with a real keyboard, which
the tests can't do.
