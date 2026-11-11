# Channel timing, Option 1: the build plan (2026-11-11)

**Built the same day.** Matthew read this plan, got the listening pair and
said "Build it". The last section, "How the build went", has what was done
and what was verified. Everything above it is the plan as it was written,
before any code changed.

Matthew picked Option 1 from
[channel-timing-scope.md](channel-timing-scope.md) to plan: make the
buffered speaker's two stretch engines share one splice decision, so left
and right cannot part. A working prototype sits outside the repo and every
claim in the plan that says "measured" comes from it.

## What gets built

One change in behaviour:

| | Today | After |
|---|---|---|
| Governed buffered speaker, different material per side | Each side picks its own splice. Load leaves the sides up to 25 ms apart, and they stay there. | Both sides are cut at the same spot. The gap is zero by construction, at Start and under any load. |
| Same signal on both sides, or one side silent | No gap | No gap, and the same audio as today |
| Governor off, warping speaker, every other sink, every render | | Unchanged |

Size: **S, one session.** About 120 lines in `numpy_backend.py` and about
200 lines of tests.

## The design

Three pieces, all in `src/pysynthrack/audio/numpy_backend.py`. Line numbers
are as of commit e61daa4; on today's `main` the sink block sits three lines
lower.

**1. Split the engine, changing nothing.** `_GrainShifter._produce_one`
(lines 775-823) becomes four small steps, with the same statements in the
same order:

- `_grid()`: where the next grain ideally sits, or "not enough input yet".
- `_terms(c)`: the raw search numbers around that spot (the correlation
  against the previous grain's continuation, and the window energy).
- `_pick(ncc)`: the best spot in a score curve, with its sub-sample
  refinement.
- `_place(p, r)`: cut the grain at `p`, overlap-add it, set the next
  target, advance the grid.

`process` (lines 824-875) gets its read-out half lifted into `_read(F, r)`.
A lone engine still runs grid, terms, pick, place, exactly as before. The
pitch shifter keeps using it untouched.

**2. Add `_GrainShifterPair`.** Two engines, one search. For each grain it
asks both engines for their terms, combines them into one score, picks one
spot, and has both engines place their grain there:

    score[k] = (dot_L[k] + dot_R[k])
               / (sqrt(e_L[k] + e_R[k]) * (sqrt(t_L^2 + t_R^2) + 1e-9))
               - the same small bias toward the grid the lone engine uses

That is the lone engine's score with the stereo frame treated as one
vector.

**3. Point the sink at it.** The actuation block in `render_block_multi`
(lines 2493-2537) builds one pair per governed sink where it builds two
engines today (lines 2504-2516), and calls `pair.process(left, right,
ratio)` where it calls the two engines in turn (lines 2517-2522).
`_sink_stretch` holds the pair. The governor's law, the smoothing, the
length resample, the clip, the ring and the warping sink are not touched.

## Why that score

| Property | Measured |
|---|---|
| Out-of-phase sides still agree on the cut | With right = minus left, each side's splice matched its own previous grain at 1.000 (median). Searching the sum of the sides instead scored 0.001: the sum is silence, so it cuts blind. |
| Same signal on both sides gives today's audio | Identical in every sample over 20 s, for identical sides and for `in_l` alone at centre (the two sides then differ by the pan gains). |
| One side silent gives the lone engine's audio | Identical in every sample over 20 s. |
| The louder side leads | By construction: the sides' correlations are added before normalising. |

Averaging the two sides' own scores ("equal say") was also tried. On five
materials it chose as well as the joint score, including with one side
60 dB down. The joint score is the one planned because of the first three
rows. The equalities in rows two and three are measured, not guaranteed:
the arithmetic differs in its last bits and happens to round to the same
grain positions.

## What the prototype showed

The prototype rebuilds the engine in split form as a subclass, adds the
pair, and replays captures from the real render path. A capture is what the
sink's two engines were actually fed (each block's left and right, and the
governor's ratio) during a 30 s run with a 250 ms stall at 8 s and 12%
overload from 16 to 21 s.

| Check | Result |
|---|---|
| Split engine against the shipped engine, lone, 24 cases (2 signals, 4 ratio paths, block 64 / 512 / 1000) | Identical in every sample |
| Replaying a capture through two split engines against what the real sink pushed, 5 captures | Identical in every sample |
| Left/right gap, linked | 0 in every grain |
| Left/right gap, today, same captures | Up to 24 ms either way during load |
| Pitch through ratio 1.25 (1000 Hz left, 1500 Hz right) | 999.9 Hz and 1499.9 Hz |
| Cost of the stretch stage | 3.1% of real time linked, 3.2% today |

**Does sharing a splice cost quality?** For every grain, how well each
side's cut lines up with that side's own previous grain (1 is perfect);
tenth-percentile values, left / right:

| Material | Today | Linked |
|---|---|---|
| Independent noise per side | 0.835 / 0.870 | 0.912 / 0.915 |
| A different tone per side | 1.000 / 1.000 | 0.999 / 0.998 |
| `ambient_bloom` | 1.000 / 1.000 | 1.000 / 1.000 |
| `mid_side_breathe` | 0.996 / 0.997 | 0.982 / 0.981 |
| `stereo_field_pluck` | 1.000 / 1.000 | 0.999 / 0.999 |

Medians are 1.000 everywhere. The level ripple of a steady tone was the
same to two decimals (0.26 dB left, 0.04 dB right, both ways). By these
numbers the shared splice costs next to nothing. The numbers do not hear;
the listening pair below is for that.

## Listening before the build

Two 30 s files, made from one capture of `mid_side_breathe` through the
governed buffered speaker with the load above. They differ only in the
stretch stage.

- `mid_side_breathe_A_today.wav`: what the sink pushes today.
- `mid_side_breathe_B_linked.wav`: the same through the linked pair.

In A the left side runs ahead of the right for most of the file:

| Time in the file | Left minus right, A | B |
|---|---|---|
| 0-4 s | settling (the Start transient) | 0 |
| 4-8 s | +8.0 ms | 0 |
| 8-10 s | the stall: moving | 0 |
| 10-14 s | +11.5 ms | 0 |
| 14-16 s | +2.4 ms (it stepped with no load) | 0 |
| 16-22 s | the overload: moving, up to 16 ms | 0 |
| 24-30 s | +9.8 ms | 0 |

Listen for two things: whether B's image holds still where A's leans, and
whether B's stretching at 8-10 s and 16-22 s sounds any rougher than A's.
This case was chosen as a clear one: of six stall moments tried it left the
largest gap. `ambient_bloom` under the same load left almost none.

The files are in the session notes folder under
`channel-timing-scope/listen/`, outside the repo.

## Build steps

One session, four commits. Work from `main`'s head, since
`numpy_backend.py` has moved since this plan was written.

1. **Baseline.** `python tools/render_audit.py render audit/before` and
   `audit/before_b`; `compare` must say "moved: 0". Add a pin test for the
   lone engine: a seeded signal through `_GrainShifter.process` at three
   ratio paths and block sizes 64, 512 and 1000, compared against
   checksums computed from the code as it stands. Commit the pin while it
   passes on the old code.
2. **Split the engine.** No behaviour change. The pin, `test_pitch_shifter.py`
   (843 lines of it) and the governor tests stay green; `render_audit.py
   compare` says "moved: 0", the five pitch shifter examples included.
   Commit.
3. **Add the pair and switch the sink.** Add the tests below. Commit.
4. **Docs.** `modules/output.py`: say the stretch is linked across the two
   sides, and correct the latency (it reads "~50 ms"; measured 93 to
   112 ms depending on the main block size). `docs/MODULES.md` if its
   entry repeats either claim. WORKLOG and TODO as usual. Commit.

Then the full suite (plain `pytest -q`, about 7.5 minutes) and a last
audit compare.

## Tests

New file `tests/test_sink_stereo_link.py`, no audio hardware:

1. **The gap is zero.** A stereo source with the same click on both sides
   over independent noise, through the governed sink in
   `render_block_multi`, with a ratio path that swings to 1.19 and back.
   Every click lands on the same output sample on both sides, and none is
   missing from one side. This must be shown to fail on the code before
   step 3 (today it fails by up to 24 ms), and the WORKLOG should record
   that it did.
2. **Same signal on both sides is unchanged.** The pair's output equals a
   lone engine's, sample for sample, for identical sides and for `in_l`
   alone at centre.
3. **A silent side follows the live side.** The live side equals a lone
   engine's output; the silent side stays zero.
4. **Out-of-phase sides.** With right = minus left, the right output is
   exactly minus the left output.
5. **Pitch is held on both sides** through ratio 1.25.
6. **Tripwire.** A source scan: the sink's actuation block builds a
   `_GrainShifterPair` and never a bare `_GrainShifter`, so two independent
   engines cannot come back by accident.

Existing tests need no changes. Three of them assert `_sink_stretch == {}`
when the sink is ungoverned, which still holds.

## Risks

| Risk | Cover |
|---|---|
| The split moves the pitch shifter's renders | The pin test, the pitch shifter suite, and the render audit. The prototype's split is already identical in 24 cases. |
| A shared splice sounds rougher on very different sides | Measured as negligible; the listening pair; his ears on his own patch after the build. |
| The governed push changes for mono sources | Measured identical. Test 2 pins it. |
| `test_voice_collapse` reads the backend's source during a run | Do not edit `numpy_backend.py` while the suite runs (a standing note in TODO). |
| Line numbers here are stale | They are from e61daa4. Find the code by name: `_produce_one`, `process`, "Governor actuation". |

## What this does not change

- **The governor still never rests.** On real streams it stretched 92% of
  blocks with nothing wrong. That is Option 2 in the scope.
- **The latency of the governed path** (about 100 ms on top of the ring
  and the host buffer). Only the documentation is corrected.
- **The load itself.** Matthew, 2026-11-11: other programs taking too much
  CPU are the usual cause. Linking the pair stops load from separating the
  sides; it does not stop the stretching or the dropouts load causes. One
  thing he can try today, untested by me: in Task Manager's Details tab,
  set the priority of PySynthRack's `python.exe` to High (the one using
  CPU; the venv's launcher has a second, idle one). If the audio thread is
  losing the CPU to other programs, that should help, and the result is
  worth bringing to the off-thread planning session.

## Calls for Matthew

1. **The score: louder side leads, or equal say?** Planned: louder side
   leads (the joint score). Measured the same on five materials; the joint
   score is exact for mono and for out-of-phase sides.
2. **Correct the latency wording in this build?** Planned: yes, words
   only.
3. **Calm the governor (Option 2) in the same session?** Planned: no. It
   changes the governor's law and deserves its own listen.

## After the build: his checks

On the desktop, with the patch that drifts:

1. Provoke load the usual way. Left and right should stay together.
2. Listen to the stretching under load against how it sounded before.
3. Read the buffered node's `buffer NN%  under N  drop N` and the
   toolbar's `DSP%`, `xrun` and `api` while the other programs are busy.
   Those numbers go to the off-thread planning session.

## The prototype

In the session notes folder under `channel-timing-scope/`, outside the
repo: `p1_linked_pair.py` (the split engine, the pair, and the sink's
actuation tail), `p2_plan_measures.py` (every table above) and
`p3_ab_files.py` (the listening pair). The build can lift the split and the
pair from `p1` nearly as they stand.

## How the build went (2026-11-11)

Built as planned, in five commits on `claude/channel-timing-scope-afe939`:

| Commit | What |
|---|---|
| 315b684 | `tests/test_grain_shifter_pin.py`: the lone engine pinned, passing on the unchanged code |
| 2d83480 | The engine split, no behaviour change |
| 16c5ba8 | `_GrainShifterPair`, the sink switched to it, `tests/test_sink_stereo_link.py` |
| 865c729 | The sink's docstring and its MODULES.md entry |
| 02b9b6f | The sink test at three main buffer sizes |

**What was verified**

| Check | Result |
|---|---|
| Pin: live engine against the engine before the split, 24 cases plus carried state and the dry ring | Identical in every sample |
| The pin can fail: four deliberate breakages of the split, at run time | Caught in 24, 24, 13 and 24 of its 24 cases |
| Render audit, every example, before against after | 167 examples, moved 0 |
| Full suite | 5,914 passed, 5 skipped (the usual five), 11 minutes. That run predates the sink test's two extra buffer sizes; its file was re-run alone afterwards: 15 passed |
| New tests against the old wiring (pair added, sink still on two engines) | The sink test failed (sides 616 samples apart, then held at 228) and so did the tripwire; the other eleven passed |
| New tests after the switch | 15 passed |
| Built sink on the real render path under load (a 250 ms stall, then 12% overload), five stereo sources | Both sides cut at identical positions in all 1,227 grains of each |
| Built sink on real PortAudio streams (laptop, MME, silent), three one-second lockups, ring from empty to full | Identical positions in all 2,290 grains |
| The scope's held-ratio click probe, repeated | A different tone per side: 0 (was up to 5.7 ms). Independent noise: within 3 samples, the detector's resolution (was up to 20.7 ms, with clicks missing on one side) |
| Latency of the governed path | Unchanged: 93 to 112 ms |

**Where it left the plan**

- **The pin is a frozen copy, not checksums.** The engine's search runs
  through BLAS, whose last bit differs between machines, and CI runs on
  Linux. The pin keeps the pre-split `_produce_one` and `process` verbatim
  in a subclass inside the test and compares the live engine with it in
  one process.
- **"Same as a lone engine" is asserted to 1e-9, not to the bit.** It
  measured identical, but the joint score and the lone score round
  differently in the last place, so bit equality is not guaranteed on
  every machine. The out-of-phase test is exact.
- **The sink test runs at three main buffer sizes** (64, 512, 1000), which
  the plan did not ask for.
- **ruff was not run.** It is not installed here and was not downloaded.
  The new files were checked by hand for unused imports, trailing
  whitespace and import order; CI's lint job is the first real run.

**Still open**

- Matthew's ears: the listening pair, then his own patch under load on
  the desktop ("After the build: his checks", above).
- Landing on `main` and pushing.
