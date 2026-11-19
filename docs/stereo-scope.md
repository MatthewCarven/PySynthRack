# Stereo: which modules pull left and right apart (scope, 2026-11-19)

Scope only. Nothing under `src/`, `tests/` or `examples/` changed for it.
The decisions at the end are Matthew's. Read in the code at cf9024d;
nothing here was measured except where it says so.

**Status:** item 1 (disk_writer) built the same evening -- see WORKLOG.

Matthew, after the stereo pitch_shifter, tape and parametric_eq landed:
"can you scope any other modules that might need a stereo upgrade for
balancing the left and right tracks".

## Summary

A module run one copy per side keeps the sides together only if both
copies do exactly the same thing to their side. Four things break that:

1. **It decides timing from its own audio.** Each copy hears different
   audio, so each decides differently. (The pitch_shifter's splices and
   regrains; fixed 2026-11-19.)
2. **It draws its own random numbers.** Seeded per module, so each copy
   gets a different stream. (The tape's flutter and drift; fixed.)
3. **It keeps its own clock.** An LFO or internal oscillator that counts
   from when *its* state began is in step with its twin only if both
   began on the same block, and only until one copy's rate knob moves.
4. **It decides level from its own audio.** Dynamics: a loud hit on one
   side turns down only that side, so the image swings. Not timing, but
   exactly "balancing the left and right tracks".

A fixed filter or a fixed curve cannot do any of these: two copies with
the same settings and the same CV treat their sides identically. For those
a stereo jack is convenience only, as it was for parametric_eq.

| Priority | Modules | What goes wrong with one per side | Workaround today |
|---|---|---|---|
| **1** | `disk_writer` | Each writer has its own queue; under load each drops blocks on its own, so a left file and a right file can part by a whole block (11.6 ms at 512) and never rejoin | None live (`--render` writes stereo offline) |
| **2** | `limiter`, `transient_shaper` | Level decided per side (cause 4): one-sided peaks or hits pull the image | None (no sidechain) |
| **2** | `compressor`, `noise_gate` | Same (cause 4); a gate can cut one side and not the other | Patch the same sidechain (e.g. a combiner of L + R) into both |
| **3** | `chorus`, `flanger`, `phaser` | LFO counts from each module's own start (cause 3); also mono in, so a stereo source must be summed or doubled | flanger / phaser: patch one clock into both. chorus: none |
| **3** | `octaver` | The sub-octave flip-flop toggles on each side's own zero crossings (cause 1): the two subs can come out in opposite polarity, hollow when summed | None |
| **4** | `bitcrusher` | With `jitter` up, the hold length wobbles on a stream seeded per module (cause 2): each side's steps land at different moments | `jitter` 0 |
| **4** | `freq_shifter`, `ring_mod` (internal carrier) | Carrier phase counts from each module's own start (cause 3); doubled, the sides become phase-rotated copies of each other if the copies started on different blocks | ring_mod: patch one external carrier into both. freq_shifter: none |
| **5** | `reverb`, `convolver`, `delay`, `freeze`, `granular`, `resampler` | Nothing drifts (deterministic, or keyed to the `seed` knob, so same seed = same grains), but all are mono in: a stereo source is summed before it, losing its image | Double them with matching settings (and seed) |
| **6** | `filter`, `crossover`, `motion_eq`, `sweep_eq`, `tilt_eq`, `vowel`, `loudness`, `distortion`, `waveshaper`, `vca` | Nothing: same settings and CV, same treatment. A stereo jack would be one set of knobs per source | Not needed |
| — | `rotary` | Mono in by design (a Leslie takes one signal); rotor counts from its own start | Feed it mono; leave as is |

Already stereo-safe: `pitch_shifter`, `tape`, `parametric_eq` (in_r, as of
today), `meter`, `scope`, `autopan`, `mid_side`, every stereo sink.
`vinyl` and `vocoder` noise is per module and should be (each side's
crackle and hiss is its own); `vinyl` is keyed to its `seed` knob, so two
copies on the same seed crackle identically.

## Details

### 1. disk_writer — the one that bites when recording

`renderers/io.py:1356-1361`: each writer is mono, with its own 64-block
queue and its own thread; when its queue is full the callback drops the
block and counts it in `dropped_blocks`, which nothing displays. Two
writers recording left and right drop independently, so under load the
two files can part by a block and stay parted. (First noted in
`channel-timing-scope.md`, "Found on the way"; read in the code, not
reproduced.) The `recordingleft.wav` / `recordingright.wav` pair in the
project folder is exactly this setup.

**Fix (S–M, one session):** `in_r` on disk_writer writing ONE two-channel
WAV through one queue and one thread, so a dropped block drops both sides
together and the file can never go out of step. Show `dropped_blocks` on
the node while it records. `in_r` empty: the mono writer, unchanged.

### 2. The dynamics: link the detector

All four decide gain from a level detector on their own input
(`renderers/dynamics.py`). The standard stereo answer is a *linked*
detector: both sides feed one detector (the louder of the two, or their
sum), and the one gain is applied to both, so the image holds.

**Fix (S each, one session for all four):** `in_r` / `out_r` on each, with
the detector reading `max(|L|, |R|)` and one gain for both sides. A
`link` amount (0 = independent per side, 1 = fully linked) is common on
hardware and cheap here. Compressor and noise_gate keep their external
`sidechain` (it overrides the internal detector, as now). `in_r` empty:
unchanged.

### 3. The modulation trio, and the octaver

**chorus / flanger / phaser** (`renderers/modfx.py`, `_mod_free_phase`):
the LFO phase is anchored per module ("keyed to an integer sample COUNT
since the last rate change") and starts from rest when the module's state
is built. Two copies built together stay in step until one copy's rate
knob moves. They are also mono in, stereo out: today a stereo source is
summed into them. **Fix (M, one session for the three — they share the
modfx helpers):** `in_r`, with each side swept by the same LFO (the left
through the left voice, the right through the right voice), so a stereo
source keeps its image and both sides move as one.

**octaver** (`renderers/colour.py`): rising zero crossings drive a ÷2
flip-flop per input. Two copies each toggle on their own side's
crossings. **Fix (S):** `in_r` with one flip-flop driven by the sum (or
the louder side), so both subs share polarity.

### 4. Small ones

**bitcrusher** (`renderers/colour.py:289`): with `jitter` up, the hold
length wobbles on `default_rng(_BITCRUSHER_JITTER_SEED + module.id)`.
**Fix (S):** `in_r` sharing the hold boundaries.

**freq_shifter / ring_mod**: the internal carrier counts from the
module's own start. **Fix (S each):** `in_r` sharing the carrier.

### 5. Mono-in effects that would collapse a stereo source

reverb, convolver, delay, freeze, granular and resampler do not drift when
doubled (deterministic, or keyed to `seed`), but they sum a stereo source
to mono when used once. A true stereo input is a design call per module
(the reverb would feed L and R into different FDN inputs; the convolver
would want a true-stereo IR). Worth doing as the patches ask for it, not
as a batch.

### 6. Convenience only

The rest of the EQ and filter family, the curves and the VCA: an `in_r`
is one set of knobs per stereo source, like parametric_eq. Batchable (S
each); motion_eq, sweep_eq and tilt_eq were already noted in TODO.md.

## Recommendation

1. **disk_writer stereo first.** It is the only one that can silently
   ruin a take, and Matthew records left and right pairs.
2. **The linked dynamics next** (limiter, transient_shaper, compressor,
   noise_gate, one session). Directly the "balancing" problem.
3. **The modulation trio**, then the octaver and the small ones, as
   patches ask for them.
4. Leave rotary as it is; take the mono-in effects and the convenience
   jacks one at a time.

## Found on the way

- **Tape seed overlap.** The three tape generators are seeded
  `0x7A9E0 + id`, `0x7A9E1 + id`, `0x7A9E2 + id`, so tape #4's drift stream
  is tape #3's flutter stream, and tape #5's drift is tape #3's hiss. Two
  tapes in one patch therefore share random numbers across roles. Harmless
  to the ear as far as reading goes (different filters on each), but not
  what the seeding meant; the stereo hiss added today uses a seed sequence
  that cannot collide. Fix if it ever matters: the same `[seed, id, role]`
  sequence form for all three.
- `disk_writer`'s `dropped_blocks` is still not shown anywhere (TODO,
  "Found during that scope", item b).
