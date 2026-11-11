# Channel timing: scope (2026-11-11)

Scope only. Nothing is built, and nothing under `src/`, `tests/` or
`examples/` changed. The decision at the end is Matthew's.

## Summary

**What drifts.** The left and right sides of one **buffered speaker**
(`buffered_specific_speaker_output`) with its ring governor on
(`auto_govern` ticked, or `ratio_cv` cabled). Matthew confirmed this in the
session: left against right, on the buffered speaker, heard live, on a wired
output, only under load, and it usually stays drifted.

**Why.** When the governor stretches the audio it hands to the device, it
uses two independent time-stretch engines, one per side. Each engine decides
where to splice by looking only at its own side's waveform. When load
starves the ring the governor stretches hard, each side splices somewhere
different, and once the governor settles each side keeps the offset it ended
up with. The gap can be anything up to 25 ms either way.

**Measured.** Reproduced offline through the real render path, and on real
PortAudio streams on the laptop (silently). On the real streams the two sides
sat 2.5 ms apart after Start, moved during a one-second lockup, settled
10 ms apart, and were still 10 ms apart 28 seconds later when the run ended.

**Not the cause.** The `clock` module, the device streams, the hand-off
ring and the device itself all carry left and right together and cannot
separate them. A tick-based clock or timestamps would not touch this.

**Recommended.** Option 1 below: make the two engines share one splice
decision. One session, and the gap becomes zero by construction. Until then
there are two workarounds that need no code (Option 0).

## The symptom

Matthew's request: "Implementing tick based timing control or other
timestamping to ensure multiple channels from the same device keep channel
timing, sometimes a channel drifts behind or forward, this might be as simple
as ramping up the clock module or could be pretty heavy".

The code alone could not settle which channels he meant, so I asked. His
answers, in order:

| Question | Answer |
|---|---|
| Which channels drift? | Left vs right of a single stereo speaker node |
| (volunteered) | "only happens under load" |
| (volunteered) | "but once drifted usually dont recover" |
| Which speaker node? | Buffered speaker |
| Heard where? | Live, while playing |
| Through what? | Wired (onboard / USB) |

## The cause

The buffered speaker pushes its audio to its own output stream through a
ring. Its governor reads how full the ring is and stretches or shrinks each
pushed block to hold the ring at half: ratio = `1 + 0.5 * (0.5 - fill)`,
smoothed, clamped to 0.5..2 (`_governed_ratio`, `numpy_backend.py:2644`).

To stretch without bending pitch, the push goes through a WSOLA
time-stretcher (`_GrainShifter`, `numpy_backend.py:720`). WSOLA cuts the
signal into overlapping 50 ms grains and, for each grain, searches up to
12.5 ms either side of the ideal position for the spot that lines up best
with the previous grain (`_produce_one`, lines 775-823; the choice is
`p = c + (k - seek) + dfr` at line 806).

The sink builds **two** of these engines, one for left and one for right
(lines 2504-2516), and runs them separately (lines 2517-2522). They share
the ideal grid, because both get the same ratio and the same sample counts.
They do not share the search: each side picks its own spot from its own
waveform.

That gives three behaviours, all measured below:

1. **Same signal on both sides: no gap, ever.** Identical waveforms make
   identical choices. Every shipped example and every governor test feeds
   this sink from `in_l` alone, which is why it was never seen.
2. **Governor near rest: whatever gap exists is mostly kept.** With the
   ratio near 1 each grain's best match is simply the continuation of the
   grain before it, so each side carries its current offset forward. The
   grid still creeps a little, so now and then a side runs out of range and
   jumps, which can change the gap or, by luck, close it. That fits
   "usually" don't recover.
3. **Governor working hard: the sides part.** When the ratio moves, the
   grid slides against the material (about 27 samples per grain at ratio
   1.025). Each side runs out of search range at its own moment and jumps
   to a new spot chosen from its own content. Different content, different
   jumps.

Load is what makes the governor work hard: a late main callback starves the
ring, the ratio swings up to refill it and back down again. So the gap
appears under load (3) and then stays (2). The same thing happens at Start,
when the ring fills from empty.

Each side can sit up to 551 samples (12.5 ms) from the grid, so the gap
between them is bounded at 25 ms either way.

## What I measured

All offline runs go through the real `render_block_multi` and the real
`_DeviceOutput` ring; no audio device is opened. The grain gap is read from
the engines themselves, through a two-line hook on the real `_produce_one`
that records where each grain was taken. It is exact, per grain. Where a
probe WAV is used, both sides carry the same click every 250 ms over a
different background, and the click is also located in the pushed audio as
a cross-check.

**1. Held ratio, 20 s, click probe.** Gap between the left and right click:

| Source | Ratio 1.004 | Ratio 1.02 |
|---|---|---|
| Same signal both sides | 0 | 0 |
| A different tone per side | -3.6 to +5.0 ms; over 1 ms on 64% of clicks | -5.7 to +3.6 ms; 65% |
| Independent noise per side | 0 on paired clicks; 5 of 77 clicks missing on one side | -19.6 to +20.7 ms; 84% |
| Any source, warping sink | 0 | 0 |
| Any source, governor off | 0 | 0 |

**2. Load, then recovery, 60 s, load at 20 s.** Grain gap, left minus right:

| Source | Load | Before (5-20 s) | During (20-28 s) | After (35-60 s) |
|---|---|---|---|---|
| Noise probe | none | 0.00 ms | 0.00 | 0.00 |
| Noise probe | one 250 ms stall | 0.00 | -21.4 to +17.4 ms | median +1.9 ms, up to +9.0; 4 clicks lost on one side |
| Noise probe | 5 s at 12% overload | 0.00 | -23.7 to +21.1 ms | 0.00 (this one came back) |
| `ambient_bloom` | 5 s at 12% overload | -0.04 | up to +15.3 ms | median -1.5 ms, over 1 ms throughout |
| `mid_side_breathe` | none | +8.0 ms, left by the Start transient | stepped to -1.1 ms with no load | -1.3 ms |
| `mid_side_breathe` | one 250 ms stall | +8.0 | -6.7 to +11.3 ms | median +2.2 ms |
| `stereo_field_pluck` | 5 s at 12% overload | -3.2 to +2.8 ms | -12.9 to +13.1 ms | -3.3 to +1.0 ms |

`stereo_field_pluck` (a chorus into the sink) shows a few milliseconds of
gap even with no load: its two sides are different pitched material, and
such material lets the engines hop between equally good spots.

**3. How often one stall leaves a lasting gap.** 24 trials on the noise
probe, one stall each of 60 to 500 ms, gap taken over the 10 to 30 s after
it: a lasting gap over 1 ms (as a median) in 8 of 24. Largest lasting gap
9.4 ms; largest single-grain gap in that window 15.1 ms. A real session
under load has many such events, and each one rolls again.

**4. Real streams.** The laptop's default output through MME, with the
buffered sink on "Microsoft Sound Mapper - Output" (the same sound card).
Every pushed block was replaced by zeros at the ring, so it was silent, but
the ring, both device callbacks and the governor ran for real. Three
lockups of about one second each were injected by holding Python's GIL.

- At rest the ratio moved between 0.978 and 1.025 and the ring between 33%
  and 72% full. Over the whole run 92% of pushed blocks were a different
  length from the 512 rendered.
- The gap was +2.49 ms from Start and held there for 15 s.
- The first lockup moved it; it settled at -10.0 ms.
- It was still -9.9 ms through two more lockups, to the end of the run.
- The ring counted no underruns and no drops, so the node's readout showed
  nothing wrong. (The lockups themselves would be heard as gaps.)

**5. Latency of the governed path.** At rest the push lags the bus by 4096
to 4928 samples (93 to 112 ms) depending on the main block size. The module
docstring says about 50 ms (`output.py:287-289`).

Numbers in 2 and 3 vary a little from run to run, because `file_player`
decodes on a background thread and the material lands at a slightly
different moment each time. The pattern does not vary.

## What it is not

| Reading | Verdict | Why |
|---|---|---|
| The `clock` module drifting against audio | Not possible | The clock counts whole samples inside the render (`renderers/sequencing.py:57`, since 2026-10-12). Both sides of a sink come out of one render. "Ramping up the clock" cannot change this. |
| Left vs right of a plain stereo speaker, a left + right speaker pair, the specific-device speaker, or the warping speaker | Not possible in the app | Left and right travel as the two columns of one array, through one ring counted in frames, into one interleaved stream. The warping sink resamples both columns at the same positions (measured: 0). |
| The device, driver or cable | Not needed to explain it | The gap is already in the audio before it reaches PortAudio. |
| Two speaker nodes on one sound card sliding apart | Real, but a different problem | See "Found on the way". Matthew's symptom is inside one node. |
| MIDI channels, or input channels | Not what he hears | He chose left vs right, live. |

## Options, cheapest to heaviest

| # | Option | Fixes | Does not fix | Size | Touches |
|---|---|---|---|---|---|
| 0 | Workarounds, no code | The gap, today | Nothing in the code | none | his patch |
| 1 | **Link the pair**: one splice decision for both sides | The gap, completely, including at Start | The governor's restlessness; the stretch artefacts themselves | S, one session | `_GrainShifter`, the sink's actuation block |
| 2 | Calm the governor at rest | Needless stretching at rest (92% of blocks today) | The gap, if the engines stay independent | S, half to one session | `_governed_ratio`, governor tests |
| 3 | Timestamped ring ("other timestamping") | Restlessness; makes a second-clock device solid; mostly removes the need for WSOLA | The gap by itself; load | M, two sessions plus his second device | `_DeviceOutput`, the governor |
| 4 | Off-thread rendering | The load events that trigger the gap | The cause; the Start transient | L, three or more sessions, plan first | main stream, mic path, every push |
| 5 | Host-API selection | Fewer and shorter starvation events | The cause | S to M, one or two sessions | stream opening, device pickers |

### 0. Workarounds, no code

- **Untick `auto_govern`** (and leave `ratio_cv` unpatched) when the
  buffered speaker plays through the same sound card as the main output.
  There is no second clock to correct there. Left and right are then rigid.
  Under load the ring drops or pads instead of stretching, so load can
  click, and the node's `under` / `drop` counts will say so.
- **Or use the warping buffered speaker** with long `brake_time` and
  `spinup_time` (10 s was his sweet spot on 2026-07-18). Left and right are
  rigid; under load the pitch sags a little instead.
- **To pull drifted sides back together:** untick `auto_govern`, wait a
  second, tick it again. That discards both engines and builds fresh ones.
  Tried once offline: a 1.8 ms gap went to 0. It costs a short hiccup.
  Stop and Start is not a reliable reset, because the Start transient can
  leave its own gap (2.5 ms and 8 ms above).

### 1. Link the pair (recommended)

Search once, cut twice. Score each candidate spot across both sides
together, pick one spot, and take both sides' grains from it. This is how
stereo time-stretchers normally work, because keeping the image matters
more than each side's ideal splice.

- The gap is zero by construction, at Start and under any load.
- Score by adding the two sides' correlations, not by searching their sum.
  Material that is out of phase between sides cancels in a sum.
- Risk to the house invariant: low. The pitch shifter uses the same engine
  class, so the build splits `_produce_one` into "search" and "cut" and
  must leave the single-engine path bit-identical. `tools/render_audit.py`
  proves that. The governed push is not a render: it depends on how full
  the ring is at that moment, it is not in the audit (example sinks have no
  device), and the ungoverned push never reaches this code.
- Risk to the sound: a shared splice is a compromise when the sides differ
  a lot, so a splice may be slightly more audible on one side. That needs
  his ears.
- Tests: a gap-is-zero test on independent noise through a ratio sweep and
  a stall. It must be shown to fail on today's code first.

### 2. Calm the governor at rest

On real streams the governor never rests. The fill is read once per block
and jumps by a whole device block (an eighth of the ring) depending on
whether the device callback happened to land first. The control law turns
that into a ratio wandering 2.5% either way. A dead band around half full,
or a fill averaged over a second, would let the ratio sit at exactly 1,
where the stretcher is a pure delay (measured: output identical to input).

It reduces how often the sides get a chance to part. It does not stop them
parting under real load, so it complements Option 1 and does not replace
it. It changes the governor's law, which tests pin
(`test_auto_matches_the_canonical_patch_gain`), so it needs a decision on
whether the built-in controller may differ from the cabled one.

### 3. Timestamped ring

This is where timestamps belong: at the hand-off between the two streams,
not in the clock module. Stamp every push and every device callback, fit
the two rates with a slow loop, and steer by the fitted rate instead of by
a once-a-block fill reading. This is the standard design for bridging two
audio clocks. Corrections become a few parts per million and smooth. At
that size a plain resample, shared by both sides, is inaudible (100 ppm is
under a fifth of a cent), so WSOLA would only be needed for load, if at all.

It is the proper design for "Governor on a second clock". It does not fix
left against right by itself, and it does not remove load. PortAudio's own
timing fields are weak on MME, so the stamps would be taken in the
callbacks, where lockups delay them; the loop has to filter that.

### 4. Off-thread rendering

Already in TODO under "Plan together first". It would remove most of the
load events that trigger the gap. It does not remove the cause: the Start
transient and any real clock correction still split the sides. A render
thread also starves when a lockup holds Python's GIL, which the TODO
already notes and measurement 4 shows.

### 5. Host-API selection

Already in TODO under "Later / wishlist". Both streams opened on MME here,
with reported latencies of 209 ms (main) and 186 ms (sink). WASAPI lists
10 ms for the same card. Less buffering jitter means fewer starved rings.
It does not fix the cause.

## Recommendation

1. **Build Option 1.** One session. It removes the reported symptom at its
   cause and leaves every render alone.
2. **Use Option 0 until then.** If the buffered speaker is on the same card
   as the main output, unticking `auto_govern` may simply be the right
   permanent setting.
3. **Take Options 2 and 3 to the governor's own planning**, alongside the
   "Governor on a second clock" check. Option 2 is cheap and could ride
   with Option 1 if he wants the governor quieter at rest.
4. **Leave Options 4 and 5 where they are**, with what this scope adds
   below.

## How this meets the TODO

- **Plan together first: off-thread rendering + host-API.** Not
  duplicated. This scope adds a requirement and a measurement. The
  requirement: link the pair first, because anything that reuses the
  governed ring inherits the gap. The measurement: a one-second
  GIL-holding lockup moved left against right by 12 ms with no underrun
  counted, so the buffer readout alone will not show this problem.
- **Does the GUI-lockup audio freeze still happen?** Those lockups are the
  "load" here.
- **Stream-health reading under load.** Still the first measurement for
  that plan. Add the buffered node's `buffer NN%  under N  drop N` line to
  what gets read.
- **Governor on a second clock.** Run it with stereo material and listen
  for left against right. On a shared clock the governor already wanders
  2.5% at rest; expect more on a second card.

## Checks on Matthew's desktop

These need his machine, his ears and his load. I could not run them.

1. **Confirm the cause (three minutes).** Play the patch that drifts.
   Provoke load until left and right part. On the buffered speaker untick
   `auto_govern` (or pull the `ratio_cv` cable), count two, tick it again.
   Expected: a short hiccup, then the sides are together.
2. **Workaround A.** Leave `auto_govern` unticked and `ratio_cv` unpatched.
   Provoke load. Expected: left and right stay together; listen for clicks
   and watch `under` / `drop` on the node.
3. **Workaround B.** Swap the node for a warping buffered speaker on the
   same device and `buffer_size`, with `brake_time` and `spinup_time` at
   10. Provoke load. Expected: the sides stay together and the pitch sags.
4. **Control.** With the governor on, feed the same signal to `in_l` and
   `in_r`. Provoke load. Expected: no movement between the sides.
5. **Numbers for the planning session.** While provoking load, read
   together: the toolbar's `DSP%`, `xrun` and `api`, and the node's
   `buffer NN%  under N  drop N`, before, during and after. Note how far
   the buffer % swings while nothing is wrong.

## Open questions for Matthew

1. Is the buffered speaker on the same sound card as the main output, or
   on a second one? On the same card it needs no governor at all.
2. Under load, which would you rather hear on that speaker: a pitch-held
   stretch (today), a pitch sag (the warping sink), or a click?
3. Is `auto_govern` ticked, or is `fill -> ratio_cv` cabled? The fix is the
   same; it matters for Option 2.
4. What usually causes the load: a GUI lockup, a heavy patch, or other
   programs?
5. The governed path adds about 100 ms on top of the ring and the host
   buffer. Is that acceptable on that speaker?

## Found on the way

Not part of this scope. Listed so they are not lost.

- **Device names that cannot open.** The sink's dropdown lists each name
  once, but a name that exists under more than one host API will not open:
  on the laptop, 3 of 12 ("Speakers (Realtek(R) Audio)", "Headphones
  (Realtek(R) Audio)", "Speakers (ZOOM H and F Series Audio)") fail with
  "Multiple output devices found". The rest land on MME, DirectSound or
  WDM-KS by accident of naming. Checked by enumeration and by constructing
  a stream on two of the names; none was opened.
- **`disk_writer` drops blocks without saying so.** Each writer is mono
  with its own 64-block queue and thread; a full queue drops the block and
  counts it in `dropped_blocks`, which nothing displays
  (`renderers/io.py:1356-1361`). Two writers recording left and right can
  part by a block under load and never rejoin. Read, not reproduced.
- **Two speaker nodes on one sound card.** Each device stream has its own
  ring, and an ungoverned ring has nothing pulling it back to a set level.
  In a model driven through the real ring, the offset between the main
  output and a second stream went from 4 ms to 57 ms over two minutes of
  lockups, and never returned. The governor holds it steady, about 50 ms
  behind at a 512 buffer. A model, not a measurement of his hardware.
- **Governed latency** is 93 to 112 ms, not the documented 50 ms
  (measurement 5).
- **The governor never rests** on real streams (measurement 4).

## Method and limits

- Offline measurements used the real backend code with a consumer standing
  in for the device callback. Real-stream measurement 4 ran on the laptop
  (Realtek, MME), not on the desktop where Matthew hears the problem.
- The Zoom H2n was not opened or probed.
- I have not heard the gap. The numbers say 1 to 10 ms is typical after
  load; that range reads as the image pulling to one side, and above about
  10 ms as a flam between the sides.
- One example (`organ_jazz`) is silent without a player and was left out.
- The probe scripts are kept in the session notes folder
  (`channel-timing-scope/`, with a README), outside the repo.
