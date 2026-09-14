# Worklog

Running log of decisions and progress. Newest first.

> Compacted 2026-07-20: everything before 2026-07-01 — all of May and June —
> moved **verbatim** to [WORKLOG-ARCHIVE.md](WORKLOG-ARCHIVE.md). Nothing was
> edited, merged or dropped; grep the archive before assuming a decision is new.
> Both files were also re-sorted newest-first, which the log had drifted out of:
> it had accreted into four out-of-order blocks, three of them oldest-first.

---

## Where things stand (snapshot, 2026-08-29)

A marker for whoever picks this up next — the entries below are the record,
this is just the state they add up to.

**Rack:** 90 module types across seven categories (89 until
`clock_divider` on 2026-09-11). Suite **2984 passed, 1
skipped** (~90 s) at this snapshot; see the dated updates below. 109 example patches, all of which load, compile and
render under the examples sweep. (The 2026-08-29 snapshot said 105; it was
already three light before slice 2 added one -- count with
`ls examples/*.json | wc -l` rather than trusting the line above.)

> Updated 2026-08-30: sampler slice 2 (loop mode) shipped, and with it a
> fix for a slice-1 defect — the sampler's `mode` dropdown had been
> offering the *filter's* modes since it shipped, which left `gated`
> unreachable from the GUI. New tripwire `tests/test_mode_combos.py`.
> **Two things want a human:** ears on `sampler_mellotron.json`, and a
> look at the sampler's mode dropdown now that it lists its own modes.
>
> Updated 2026-09-11: **sampler slice 3 shipped** — the sampler is
> feature-complete against its spec. Suite **3027 passed, 1 skipped**
> (~108 s). 110 examples. Three more things want a human: ears on
> `sampler_scrub.json` (the slicer — `start_cv` was the prize), eyes on
> the sampler node's new waveform face, and a real MIDI keyboard into
> `midi_input.velocity_cv → sampler.vel`. Matthew's steer: **modules
> for a while** — next picks come off the build queue in TODO.
>
> Later the same day: **drums love pass shipped** — `vel` on all three,
> kick `pitch_cv`, hat `tone`. Suite **3046**. 111 examples
> (`drum_dynamics.json`, wants ears — banked with the sampler ones;
> Matthew is somewhere noisy). Remaining love candidates in the
> 2026-09-11 drums entry below.
>
> And then **modal love pass shipped** — `position`, `mallet`, `spread`
> (+ `out_l`/`out_r`). Suite **3065**. 112 examples (`modal_mallets.json`,
> banked).
>
> And **clockwork love pass shipped** — `euclidean.fills_cv`,
> `burst.count_cv`, and **`clock_divider`, module #90**. Suite **3088**.
> 113 examples (`clock_divider_swing.json`, banked).
>
> 2026-09-12: **function generator `curve_rise` / `curve_fall`
> shipped** (follow-on 1 of 4). Suite **3102**.
>
> 2026-09-14: **function generator `rise_cv` / `fall_cv` + `out_inv`
> shipped** (follow-ons 2 and 3; 4 waits on the feedback door). Suite
> **3119**. Pushed through 0eeedd9 the same day. Then **per-voice
> rise/fall rates shipped** in a second pass. Suite **3130**. Pushed
> through 91b8dfb. Then **pitch_shifter shimmer / harmonizer / stereo
> spread shipped**. Suite **3152**, 115 examples (two new, banked for
> ears). Pushed through 7de1be3. Then **chord/arp extras shipped**
> (inversion, `changed` + `retrig`, arp internal clock). Suite
> **3177**, 116 examples. Next: slew v2 — the last item on the love
> list.

**Outstanding: nothing on the MODULE board** — every module is heard and
seen, as of 2026-08-21. Two older non-module items are still open and are
easy to lose, so they are named here rather than left in the tail of
TODO.md:

* **Buffered sink: `buffer_size` 8192 fails `open()` and the sink goes
  silently `buffer: idle`** (screenshot-confirmed 2026-07-16, TODO
  "Later/wishlist"). A real defect, and note the family resemblance to
  the media-path bug fixed today: *another* subsystem failing soft with
  nothing to say so. Fall back instead of going quiet, and surface the
  fallback in the readout.
* **Real-GUI eyeball: the governor patch** (meatthread0) — fill ->
  CVOffset(-0.5) -> CVScale -> ratio_cv against a second device. Never
  ticked; the 2026-08-21 "eyeball queue empty" covered modules and
  examples, not this hand-built patch.

Both 2026-08 modules passed on 2026-08-29: the **function generator**
("works fine") and the **sampler** ("definitely works now... and works
well", once the media path fix let it find its samples). That second one
settles the design question too — the voice-not-a-transport positioning
holds up in play, which was the whole argument for the sampler being a
new module rather than a `file_player` mode. Slices 2 and 3 are
unblocked whenever Matthew wants them.

One architectural finding is queued rather than fixed: feedback cycles
close ONLY through a `matrix_mixer` (or a buffered sink's `fill`), so a
self-patched `eoc → trig` is legal but inert. See the 2026-08-23 entry
and the TODO item; it is a compiler change and wants its own session.

Everything else is clear: no open defects, all 18 example patches heard,
every GUI face seen. The eyeball queue emptied for the first time on
2026-08-21.

**What the 2026-08-20/22 run added:** the possibility panel (sixteen
click-to-cycle cells); the pre-Start param-write fix (every widget edit
before the first Start used to be silently discarded); ASCII on screen
(336 port labels had been rendering as `?` since the node editor was
written); the organ-feedback pair (drone + shimmer); and sampler slice 1.
Three tripwires came out of it and are worth knowing about before adding
code: `tests/test_ui_glyphs.py` (nothing non-ASCII reaches a screen),
the `backend.set_param` call-site count in `tests/test_param_writes.py`,
and the docs-coverage sweep that has been there longer.

**Next up, in the order most likely to be picked:** `rotary` (the organ's
partner), granular (three pre-sliced pieces), `clock_divider`, the
possibility follow-ons, the 2026-08-04 keep-list — Matthew wants modules
for a while (2026-09-11). The feedback-door generalization waits for its
own session. Full menu in TODO.md and docs/MODULE_IDEAS.md.

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

---

## 2026-08-30 — sampler slice 2: loop mode, and the dropdown nobody could reach

Matthew picked slice 2 off the queue. It shipped, and on the way it turned
up a defect in slice 1 that had been sitting in plain sight since the day
it shipped.

### The loop

`loop` joins the `mode` combo: `gated` underneath — it sustains while held
and releases on the fall — but the playhead wraps `loop_end` back to
`loop_start` instead of running out. Three new params: `loop_start` /
`loop_end` (0..1) and `loop_xfade` (ms).

**`loop_start`/`loop_end` are fractions of the REGION, not of the file.**
That was the one design call worth making deliberately: it means dragging
`start`/`end` carries the loop along instead of stranding it outside the
region, which is what you want the moment you use both. The playhead still
starts at `start`, so everything before `loop_start` plays once as the
attack and only the loop repeats — that is what lets a bowed or struck
sample keep its onset and still sustain forever.

**The wrap is one modulo on the affine array.** The segment renderer
already computed `pos + rate*arange(n)`; looping adds
`where(over < 0, raw, lo + mod(over, L))` and nothing else, so it stays a
single vectorized expression *and* stays exact on integers. That last part
matters more than it sounds: **a unity-rate loop with the crossfade off is
a bit-exact tiling of the file.** Slice 1's whole spine is that the neutral
is bit-exact, and the loop keeps it — so "the steady-state period is
exactly the loop length" is an `array_equal` in the tests, not a tolerance.
An octave up is `np.tile(data[::2], 4)`, also exact.

**The seam crossfade reads the previous lap.** Over the last `loop_xfade`
of the loop the read is mixed with `read(pos - loop_length)` — the same
signal one lap earlier, i.e. the material running *into* `loop_start`. The
weight reaches 1 exactly as the playhead reaches `loop_end`, so the wrap
lands on what was already sounding. Measured on the shipped `pad_c4.wav`
at the shipped settings:

```
worst sample step at the seam:  hard 0.28532  ->  crossfaded 0.03528
the recording's own worst step:              0.15780
```

The faded seam is *quieter than the material around it*. Two more calls
worth not re-litigating: the fade is measured on the **sample**, not the
clock, so it covers the same slice of waveform whatever the pitch; and it
is clamped to the loop length, because there is only one lap to fade into.

**Deviation from the spec, noted per the standing principle:** MODULE_IDEAS
specified `loop_xfade` as 1..100 ms. It ships as **0..100**. The spec's own
test line asks for "seam spike-free vs xfade=off", which needs 0 to be
reachable — and 0 is independently worth having, both as a real musical
setting (a loop already cut on a zero crossing) and as the only way to keep
a loop bit-exact. The A/B test that proves the crossfade works is exactly
that pair of renders.

Two more forgiving-rather-than-clever calls: an inverted loop region plays
as `gated` rather than falling silent (unlike an inverted *playback*
region, which is silence) — this is a slider you can drag past its partner,
and going quiet would be a trap; and the retrigger crossfade tail wraps too,
so a re-struck voice near the seam does not drop out.

15 new tests, plus 17 in the new tripwire file below (suite **2950 ->
2984**). Example `sampler_mellotron.json`:
keys -> loop-mode sampler -> reverb, holding a chord on three seconds of
pad indefinitely. `generate_samples.py` gained `build_pad` / `pad_c4.wav` —
the percussive samples had nothing to loop, being over long before you let
go of the key. Its partials are deliberately not phase-aligned across the
loop points: hiding a seam that does not match is the entire job of
`loop_xfade`.

### The dropdown nobody could reach

The new "does the mode combo offer `loop`?" test failed, and not for the
reason expected. The sampler's `mode` combo was offering
**`lowpass / highpass / bandpass`**.

`_add_param_widget` has one shared branch for `mode`/`mode_neg` that runs
*before* the per-module TYPE blocks, and whose `else` arm hands out the
**filter's** items. `function_generator`'s TYPE block sits above that
branch and works; `sampler`'s sits below it and was shadowed. So from
2026-08-22 to today the sampler's mode dropdown listed three filter
responses, picking one wrote a value the renderer rejects, and the renderer
falls back to `one_shot` — which means **`gated` was unreachable from the
GUI for the whole of slice 1**, and `loop` would have been too.

Nothing crashed and nothing looked wrong. The reason it survived the ears
pass is that `sampler_breaks.json` sets `one_shot` in JSON, so the mode
that worked was the only one anyone used. And the reason it survived the
tests is worse: slice 1's widget sweep asserted `"mode" in combos` — and
the *filter's* combo carries that label too. **Matching a label is not
matching the widget.** Same family as the count-the-doors lesson, one level
along: the tripwire was looking at the right kind of thing and never
checked whose it was.

It has also happened before. The comment in that very branch records
`cv_to_frequency` listing the filter's items until 2026-06-07. Second
occurrence, same shape.

**Fixed, and then closed off properly.** A registry-walking audit found
`sampler` was the *only* casualty — the other 14 mode combos are correct,
which is worth knowing rather than guessing. The fix is one arm on the
shared chain plus deleting the block it was shadowing; the durable half is
`tests/test_mode_combos.py`, which walks the registry and asserts every
module's `mode` dropdown contains that module's own default. It fails on
the pre-fix code (checked — a tripwire that cannot fail is decoration), and
it covers a module the day it registers whether or not anyone remembers the
file exists. The shared branch now carries an ADD YOUR MODULE HERE comment
naming the shadowing hazard.

Its assertion is deliberately weak — "your default is in your own list" —
because that is enough to catch the entire class of "this combo belongs to
another module", which is the only way this has ever actually broken.

**Still wanted: ears on the mellotron patch, and eyes on the mode dropdown**
(meatthread0). Run `python examples/samples/generate_samples.py` first —
the wavs are gitignored. Worth checking `gated` while you are in there: it
has never once been selectable.

---

## 2026-08-29 — the sampler wasn't broken, the working directory was

Matthew went to listen to the sampler and heard nothing: *"didn't hear any
audio? Maybe a file/path issue?"* — with `krell_machine.json` playing fine
in the same sitting. That contrast was the whole diagnosis: the krell patch
touches no files.

**What it was.** `examples/sampler_breaks.json` stores its sample as a
*relative* path, `examples/samples/breaks.wav`, and the backend resolved
relative paths against the **process working directory** and nothing else.
So the patch was audible from the project root and silent from anywhere
else. Reproduced exactly:

```
cwd = project root  ->  peak 0.515   (plays)
cwd = home          ->  peak 0.0     (silence)
```

His wavs were present and correct all along; he had run the generator. He
was about to move the file, which would only have helped if he had guessed
the right destination — so the diagnosis was worth more than the fix would
have been on its own.

**It was never sampler-specific.** `convolver` (`examples/irs/hall.wav`)
and the `file_player` examples (`take_01.wav`, the two recordings) carry
relative paths through the same six call sites and break identically. This
had been latent since the convolver shipped; nobody had launched from
anywhere but the root.

**The fix: resolve against the patch, not the process.** `Patch` gains a
`source_path`, stamped by `load_patch` and `save_patch` and deliberately
*not* serialized — it describes where the file is, not what it contains, so
a copied patch must not carry the original's folder. `compile()` captures
that folder, and one helper, `_resolve_media_path`, now stands in front of
all six reads. A relative path is tried against, in order:

1. the patch's own folder — the DAW convention, and what makes a patch plus
   its samples portable as a unit;
2. that folder's parent — because the shipped examples live in `examples/`
   while naming their media from the project root, and rewriting them would
   break every patch a user has already saved in that style;
3. the working directory — the historical behaviour, so nothing that worked
   before stops working;
4. the resource root — where the examples and their media live in a frozen
   build, which is exactly the case Matthew was heading for.

First hit wins. Absolute paths are returned untouched: the user named an
exact file, and second-guessing them is worse than failing. Nothing found
comes back unchanged so the failure names what the patch actually asked
for. Results are memoized per compile — not for the stat calls but because
**the renderers use the resolved string as a "still the right file?" cache
key**, so an unstable answer would reload every block forever; the memo is
dropped on every `compile()`, so dropping a missing file into place and
recompiling finds it.

**The second half, which is the real lesson.** The loaders all fail *soft* —
they must, or a typo'd path would raise on the audio thread — and the price
of that is a missing file being indistinguishable from a patch that is
working correctly and happens to be quiet. That is what cost the listening
pass, and it is worse than the path bug: Matthew had to guess. So
`media_load_failures()` reports any sampler/convolver whose load finished
empty, and the GUI names it in the status bar once per failure, quoting the
path **as written in the patch** because that is what he will go looking
for. Cleared on load and on recompile, so a fix gets a fresh verdict.

I had also written the old behaviour into the docs as though it were
expected — *"plays silently rather than failing"*. Documenting a wart is
not the same as deciding it is a feature; that note is gone.

**Testing note.** The first version of the identical-audio test compared
two renders sample-for-sample and failed on the first ~5,500 samples. Not
the fix: the decode runs on a daemon thread, so how many blocks render
before a sample is available is disk timing. The test now settles the
loaders before rendering — the claim is about the audio, not the race.
Testing the kind of claim you are making, again.

`tests/test_media_paths.py`: 20 tests, including the two shipped examples
rendered from a foreign cwd (the regression itself), the four-base search
order, the not-serialized rule, and a missing sample being *reportable*
rather than silent. Suite **2950 passed, 1 skipped**.

**VERIFIED in the real app 2026-08-29** — Matthew recompiled and reported
back: "works fine now". `sampler_breaks.json` makes sound where it made
none. Note this confirms the *path fix*, not the sampler: slice 1 still
has not had a proper musical listen, which is now the only thing on the
board.

---

## 2026-08-23 — the function generator, and the door that isn't open

Board was clear bar the sampler's ears (Matthew was out; that listen is
still pending). He picked `function_generator` off the 2026-08-04
keep-list, where it had sat with the note "probably the highest
patch-value-per-line-of-code item on this list". It earns that: it is one
shape with three modes and two announcement jacks, and which *module* it
is depends entirely on how you cable it.

**The design, which is a positioning argument again.** A rise/fall ramp is
not new — `ad_envelope` is one. What makes this the west-coast module is
that it tells you where it is. `eor` pulses when the rise completes, `eoc`
when the fall does, and those two jacks turn a ramp into an envelope, an
LFO, a clock, or a gate delay depending on what you plug them into. The
three modes are three answers to "what does the gate mean?" — `trigger`
ignores its length, `gate` holds at full while it lasts, `loop` doesn't
need one at all and treats a trigger as a sync instead.

`curve` is one bipolar knob for both slopes, `level = pos**k` with
`k = 1+3c` for c ≥ 0 and `1/(1-3c)` below, so +c and −c are exact
reciprocals: the two halves of the knob are mirror images, and the fall
is always the rise played backwards. Positive is the natural percussive
shape (slow-then-fast up, fast-then-trailing down), negative the swelling
orchestral one. Tested by *measuring the shape* — the half-rise level is
pinned to `0.5**k` at five curve settings, not merely "it goes up".

**Two things went wrong, and both were worth the trip.**

*Integer counters, not float steps.* The first draft advanced stage
progress with `pos += 1/(t·sr)` per sample. It looked perfect. Then the
loop-mode period measured **962 samples where 960 was asked for** —
accumulation error, about a sample per stage, invisible in an envelope
and compounding every cycle in a clock. This is the organ's lone-8′
lesson arriving from a new direction: count integers, divide once. A
stage is now exactly `round(t·sr)` samples, forever, and the test asserts
`set(periods) == {960}` rather than a mean — the tripwire is only a
tripwire if it would have caught the bug, and this one demonstrably
would. Stage *entry* still needs the curve inverted (`cnt =
len·level^(1/k)`), which is what makes a retrigger deep in the fall, or a
gate released mid-rise, pick up from the level it is actually at instead
of jumping. Click-free is measured as a bounded per-sample step, not
eyeballed.

*The voice path was 117% of the block budget.* Vectorized across the V
slots, per-sample, like `ad_envelope`'s — and it cost more than a whole
block's worth of time on its own. The slew lesson, again and harder: at a
block's worth of samples a numpy op is overhead, not arithmetic, and this
state machine does about ten of them per sample. Rewritten as ONE scalar
kernel that the mono path calls once and the voice path calls once per
slot: **34.6%** at 16 sounding voices, 8.9% at a realistic four, and
**0.19%** when every slot is parked (idle slots skip the loop entirely,
which in a poly patch is most of them, most of the time). The refactor
also made the bit-identity claim structural rather than asserted — a
voice row equals the mono result because it is literally the same
function, and the tests now pin that across three modes and three curves
rather than hoping.

**And one thing that isn't a bug, but is a wall.** The headline patch for
a module like this is the krell: `eoc` back into `trig`, and the machine
plays itself forever. That cable is legal here — the editor accepts it,
the patch compiles, the audio renders, everything looks right. It does
not fire. `_is_delayed_edge` honours exactly two feedback paths: a cable
compile marked a `matrix_mixer` late-read, and a buffered sink's `fill`
out. Every other cycle is *severed* by the topological sort, not delayed
one block. My first `krell_machine.json` had a starter clock OR'd with
the returning `eoc`, rendered beautifully, and fired **once every twelve
seconds** — the starter alone. A patch that merely looks like a krell.

So the example was rebuilt around `loop` mode, which is the supported
route to the same machine and is precisely why the mode exists: it
free-runs, a stepped-random LFO into `rate_cv` breathes the pace, and
`eoc` grabs a fresh pitch through a sample & hold each cycle. Measured
over 20 s it fires 34 times with gaps from 0.53 s to 1.44 s. The example
carries a tripwire that **counts the notes and checks the pace varies**,
because "it renders" was exactly the check that let the broken version
through. Both the module docstring and the MODULES.md entry say plainly
that `eoc → trig` will not close in this rack and why.

Generalizing the door — letting any cycle-closing cable become a
late-read, which is `_compute_late_edges` widened from "dst is a matrix"
to "closes a cycle" — is queued in TODO as its own item. It changes how
every patch compiles, so it wants its own session, its own tests and its
own ears, not a corner of this one.

Shipped: the module, the three-mode renderer + shared kernel, the UI
panel (mode combo, two time drags, a bipolar curve slider), the
MODULES.md entry + index row, `examples/krell_machine.json`, and 40
tests. Suite **2930 passed, 1 skipped**. Both tripwires green (ASCII on
screen, `backend.set_param` call sites). Needs: a listen.

---

## 2026-08-22 — the sampler, slice 1: a voice, not a transport

Board clear, Matthew picked the sampler. The 2026-08-04 spec sliced it
three ways; this is slice 1 — the voice itself. Slices 2 (loop mode) and
3 (the stretch menu) are on TODO, per the working agreement about not
attempting a large job in one pass.

**The positioning, which is the whole design.** `file_player` is a
*transport*: one playhead, one speed, buttons. `sampler` is a *voice*:
per-voice playheads, each at a rate set by `pitch_cv`, started by a gate
edge. That single distinction is why it needed to be a new module rather
than a FilePlayer mode — and why the rack's existing 16-slot voice
architecture does most of the work. A `cv_keyboard` in front gives
sixteen independent playheads for free.

`root` — the note the recording *is* — is the trick that makes it an
instrument rather than a tape player. Play the root and the rate is
exactly 1.0.

**Bit-exactness was the design target, not a nice-to-have.** The spec
asked for a neutral: root pitch, full region, attack 0, level 1 → the
decoded buffer *verbatim*. Everything downstream was chosen to keep that
true. The read is `_hermite4` from the resampler verbatim, whose spline
constant term is `p0` untouched by float ops, so an integer position
returns that sample exactly. The mono sum is written `0.5 * (l + r)`
because a mono file decodes to two identical rows and halving their sum
is exact in IEEE arithmetic — a lazier `mean(axis=0)` would have been
the same value here but the explicit halving is the version that's
*obviously* exact. `level` 1.0 multiplies by exactly 1.0. Result: the
output is the file, sample for sample, and an octave up is a bit-exact
`[::2]` — the strong version, since rate 2.0 also lands on integers.
Both pinned, plus block-size independence.

**Structure.** Whole-file background load (`_SampleLoader`, the
convolver's `_IRLoader` pattern minus the FFT build) kicked at
`compile()` on the UI thread — the first cut kicked it lazily in the
renderer instead, and the neutral test failed with silence because
`wait_for_sample_loads()` had nothing to wait on yet. Whole-load rather
than streaming because a sampler needs random access: sixteen voices
may be reading sixteen places at sixteen rates. Each block is cut into
segments at that voice's gate edges; within a segment the playhead is
affine (`pos + rate·arange(n)`), so the whole segment is one vectorized
gather. Region end is found with `searchsorted` rather than a mask —
the playhead only moves forward, so the live part is a prefix.

**Two design calls worth recording.** (1) *Nothing is faded at the region
end.* A fade there would be kinder to abrupt samples but would break the
bit-exact neutral, and the neutral is worth more — `end` and a gated
`release` are the tools for trimming. (2) *Pitch-up aliases, deliberately*
— that crunch is the sound of every classic sampler (the bitcrusher
precedent: some grit is the point). Pitch down is clean. A mip chain for
clean pitch-up is slice 3.

`attack`/`release` are declick ramps and are documented as such, twice,
because the names invite the other reading. Real shaping is adsr→vca like
every other source. Retriggering a sounding voice hands the old playhead
to a ~2 ms falling tail while the new one fades in — added, not blended,
since two reads of one buffer sum linearly, so equal-and-opposite ramps
cross without a notch. Measured: max sample delta 0.034 across a
retrigger versus 0.031 for the waveform's own steepest slope.

**A helper that was missing.** The `root` combo shows note names and
stores MIDI numbers (the fm_op ratio-combo precedent), which needed
`name_to_midi` — `midi_to_name`'s inverse, which the codebase never had.
Put beside it in `keyboard.py`, accepts flats and lowercase (a human
typing a note name isn't thinking about either), and returns **None**
rather than guessing on junk, so the callback leaves the instrument
alone instead of silently retuning it. 6 tests including a 0..127
round-trip.

**Example.** `sampler_breaks.json`: three samplers pointed at three
regions of one synthetic drum loop, each fired by its own euclidean — a
breaks machine built out of `start`/`end` alone, which is a better
advertisement for the region controls than any single-slice demo. First
attempt used 4+5+9 pulses and half-second slices and came out a *wall*:
30 of 31 sixteenths filled. Thinned to 4+2+6 with a shorter hat slice
and it reads as a groove — `XX..XXX.XX..XXX.` — peak 0.516. Audio comes
from `examples/samples/generate_samples.py` (synthetic kick/snare/hats
plus a marimba C4 for the pitched case), following the `examples/irs/`
precedent exactly: generator in git, wavs gitignored, and the patch
loads and plays *silently* until you run it rather than failing.

28 tests, suite **2842 → 2888**. MODULES.md entry + index row + appendix;
README 87 → 88 modules.

**Pending:** Matthew's ears on `sampler_breaks.json` (run the generator
first). Slices 2 and 3 queued.

---

## 2026-08-21 — the shimmer: an octave inside the loop

The drone passed ears ("That works well"), with a caveat worth keeping:
*"definitely sounds organ like anyways."* Fair, and diagnostic — at the
shipped `g21` 0.65 the loop **supports** the organ rather than
transforming it. 0.9 is where it stops sounding like an organ and starts
sounding like a room that won't let go. So the sequel wanted to be a
patch whose transformation is unmistakable at its *default*. Matthew
picked exactly that: the shimmer.

**The change is one module.** Same clock → sequencer → organ head, same
matrix door, but the loop now reads `out_1 → pitch_shifter (+12) → delay
→ in_2`. Every lap comes back an octave up, so the chord stacks octaves
and climbs away from the note that started it. That one-module delta is
the point pedagogically: the two sibling patches differ by exactly the
thing being taught.

Two supporting changes, both load-bearing rather than taste.
`pulse_width` 0.8 → **0.45**, so the organ stops between chords and the
cascade has a gap to bloom into — with the note still sounding you hear
a thicker organ, not a climb. And the delay's `tone` down to **0.3**: a
dark loop is what each successive octave dies against. Left bright, the
cascade never resolves into anything, it just accumulates hiss. The
climb needs a ceiling to lose against, and a low-pass in the loop is it.

**The levels were wrong first time, and measuring caught it.** The first
pass reused the drone's levels and peaked at **1.000** — pinned against
the soft ceiling for most of its length. Not unsafe (that is the ceiling
doing its job) but wrong to ship: the listener would hear the limiter
working, not the shimmer. An octave-multiplying loop simply puts more
energy in than a flat one does. Swept organ level × output gain × reverb
mix and landed on 0.30 / 0.75 / 0.50 → peak **0.836**, with the
30-second halves at 0.137 then 0.110 (0.80×), so it settles rather than
climbs. The regen sweep stays finite and bounded at 0.85, 0.99 and 1.0.

**Testing a spectral claim spectrally.** A structural test — "the
shifter is in the loop" — passes just as happily on a patch whose
shifter sits at 0 semitones and shimmers nothing. So the headline test
renders the patch twice, identical but for `semitones` (12 vs 0), and
compares octave-band energy in the tail. Everything else (loop gain,
delay, reverb, organ) is held fixed, so any difference upstairs is the
shifter's doing and nothing else's. Shipped margins: **12×** in the
700–2800 Hz band, **~41,000×** above 2800 Hz, where the dry loop
essentially never goes. Thresholds set at 4× and 50× — far under the
measurement, so the test fails on a broken shimmer without being
brittle about ordinary DSP tweaks.

11 tests, suite **2829 → 2842**. `docs/MODULES.md`: an appendix entry,
cross-links from `matrix_mixer`, and — the useful one — from
`pitch_shifter` itself, whose entry previously stopped at "stack a
fifth" and never mentioned that the module's most striking use is
putting it somewhere its own output comes back.

**EARS PASSED 2026-08-22** — Matthew: "sounded great". Both halves of
the feedback pair are now heard, and they land the way the pair was
designed to: the drone *supports* the organ (his "sounds organ like
anyways"), the shimmer *transforms* it. The caveat on the first one was
what shaped the second, which is the useful shape for a two-patch set —
ship the supportive version, listen, then ship the one that answers what
the listening revealed.

That also empties the board: no defects open, every example heard, every
GUI face seen.

---

## 2026-08-21 — the organ through the feedback door

Board clear, so Matthew picked the small high-joy one: his own idea from
2026-08-05, "maybe feedback with the organ?". Shipped as
`examples/organ_feedback_drone.json`.

**The patch.** Slow clock (60 bpm, division 0.5 — one chord every two
beats, 80% pulse width) → a 4-step sequencer spelling **Cm7**: C, G, Eb,
Bb → organ, percussion off, drawbars 16'/5⅓'/8'/4'/2⅔' so there's upper
harmonic material for the loop to chew on. Organ into `matrix in_1`;
`out_1` → a 700 ms delay → back into `in_2`. Reverb hangs off `out_1`
**outside** the loop as polish. The delay's own `feedback` is 0 and its
`mix` fully wet — it's a plain send, so `g21` is the only regeneration
in the patch and the knob can't lie about what it does. That's the
`matrix_feedback_echo` precedent, held open under a *sustained* source
instead of a transient, which is the whole difference: a kick through
that door gives you echoes, an organ gives you a pad that keeps arriving
after the note stops.

**Measured rather than hoped.** Shipped at `g21` 0.65: peak **0.858**,
and the per-2-second RMS breathes on an 8-second cycle (matching the
four-step progression) rather than climbing — 0.246, 0.169, 0.131,
0.181, 0.262, 0.176 … it settles into the loop instead of swelling to
the rail. Then the sweep that matters, because the node label invites
you to turn it up: at `g21` **0.9, 0.99 and 1.0** the output is finite
and pinned at exactly **1.000**. The matrix's soft ceiling holds a loop
wound to unity. That measurement is what makes "0.9 blooms" shippable
text rather than a trap.

Also FFT-verified the musical claim before writing it on the node —
rendered the organ in isolation (loop removed, straight to a speaker)
and read the strongest partial per step: 261.3 Hz C4, 196.2 Hz G3,
311.2 Hz D#4, 466.2 Hz A#4. Pitch classes C/G/Eb/Bb, so the label is
true. Worth doing: a node name that names a chord is a claim, and the
16' drawbar makes the dominant partial jump octave between steps, so
eyeballing the sequencer numbers wouldn't have confirmed what you
actually hear.

**Tests** (11, `tests/test_organ_feedback_example.py`) follow the
`ring_governor` pattern — pin what makes the patch *this* patch, since
the examples sweep already covers loads-and-renders. The loop is closed
(out of the matrix, through the delay, back into the regen row); the
regen row is open (a future edit zeroing `g21` would leave something
that still renders, still sounds like an organ, and is silently no
longer a feedback demo); the delay doesn't regenerate on its own; the
reverb stays out of the loop; regen audibly adds energy over the dry
door; shipped settings leave headroom; it settles rather than grows; and
the cranked-ceiling sweep at 0.9/0.99/1.0. Suite **2816 → 2829**.

`docs/MODULES.md` gains an appendix entry and a cross-link from
`matrix_mixer`, which previously pointed only at the kick demo.

**Pending:** Matthew's ears. The sequel, if he likes it: a shimmer
variant with a `pitch_shifter` at +12 inside the loop — same door,
brighter room — already noted on TODO.

---

## 2026-08-21 — the butterfly lands: the eyeball queue is empty

Matthew wired the goniometer by hand — `chaos.x`/`y` through two
`cv_to_audio` bridges into `scope.in`/`in_r` (the bridges are needed:
chaos's outs are cv, the scope's traces are audio) — and after three
setting nudges it drew a clean Lorenz **wing**: correct attractor
geometry, thin on the inner edge and fattening outward. "Sweet." That
clears the last item on the eyeball queue, which is now **empty for the
first time** — every module in the rack has been heard and seen.

The screenshots also confirmed the ASCII sweep in the wild: jacks reading
`< reset`, `x >`, `out >` instead of the old `?` soup.

**Worth writing down, because none of it was obvious from the panel.**
The scope's capture window is `time_div × 10 divisions`, so the default
10 ms/div is a **100 ms** window — at 1 orbit/s that is a tenth of one
orbit, which draws a short arc and looks broken rather than merely
zoomed-in. The max 500 ms/div gives 5 s ≈ 5 orbits. And chaos's default
`range` 2.00 bipolar swings to ±2 while the scope face clamps at ±1, so
the wings flatten against the edges until you drop `range` to 1.00 or
scope `gain` to 0.50 (Matthew took the gain route). Three settings
between "looks broken" and "looks like the textbook picture" is a lot to
ask of a first-time viewer; the MODULES.md chaos entry's cheerful "patch
x/y into a scope in xy mode — the butterfly, live on the node" was
underselling the setup required, so both entries now say what to set.

**A limit found, not a bug.** xy mode decimates to **200 points** per
frame (`_SCOPE_COLS`), so the points-per-orbit budget trades directly
against how many orbits fit in the window: ~5 orbits reads as a smooth
loop, ~20 orbits would show both wings but at ~10 points each it goes
angular. And there is no phosphor persistence — the trace is the live
window, not an accumulating image — so the classic dense butterfly
(built from thousands of overlaid passes) isn't reachable as things
stand. Both wings at once needs a lucky lobe switch inside the window,
which rate ~2–3 makes likely.

That suggests a **scope persistence/afterglow** option (accumulate N
frames, fade the old ones) as a genuinely nice small feature. Offered it;
Matthew didn't take it up, so it is NOT on the TODO — noting it here so
it's findable if the view ever nags him.

---

## 2026-08-21 — the jacks were never labelled `?`: a font, not a bug in the words

Matthew sent a screenshot of `chaos_melody` running so I could check the
scope. The scope was fine — live trace, DSP 7%, xrun 0. What the shot
*also* showed, on every single node, was this:

    ? reset          x ?
    ? in             y ?
    ? gate           z ?

Those are meant to be `◀ reset` and `x ▶`. DearPyGui's built-in font
(ProggyClean) covers basic Latin only, so every codepoint above U+00FF
paints as a replacement `?`. Nobody had ever mentioned it, which is the
interesting part: it had been true since the node editor was written, and
you stop seeing it. It took a screenshot — a *photograph* of the app
rather than the app itself — to make it visible.

Blast radius, measured rather than guessed: **336 port labels** across the
87 module types (i.e. every jack on every node in every patch), **24 of
104** example patches with em dashes / `→` / `≈` in their node titles, and
11 strings in `ui/app.py`. On `possibility_seq`, whose entire idea is a
`?`, the jacks reading `?` was actively confusing.

**The fork, and Matthew's call.** Two real options: bundle a TTF with wide
coverage and register a dpg font (keeps the pretty triangles, costs a
~300–700 KB file, a licence, and a PyInstaller spec change), or go ASCII.
He picked ASCII, so jacks now read `< in` and `x >`, em dashes become `-`,
`→` becomes `->`, `…` becomes `...`, `≈` becomes `~`.

**The sweep went wider than `ui/`,** because "reaches a screen" is not the
same as "lives in the UI layer". `core/patch.py`'s cable-refusal
ValueError and `numpy_backend`'s sounddevice RuntimeError both arrive in
the status bar via `_set_status(f"...: {exc}")`. And two `cli.py` prints
were a genuine latent *crash*, not just mush: piping stdout on Windows
encodes with the locale codec (cp1252), where `→` raises
UnicodeEncodeError and takes the run down. I hit exactly that while
investigating, in my own shell, which is how I noticed.

**Tripwires,** because this is the failure mode that hides: an em dash
typed into a status message looks perfect in the editor and only turns to
mush on screen, so the check has to be mechanical. `tests/
test_ui_glyphs.py` walks the **AST** of every `ui/*.py` (plus `cli.py` and
`core/patch.py`) and flags any non-ASCII string constant — excluding
docstrings precisely rather than by regex guesswork, since developer prose
keeps its typography and only *display* strings matter. Same check over
every example patch's node names. Plus a self-test that plants an em dash
in a temp file and proves the tripwire fires on the display string and
*not* on the docstring — a tripwire nobody has seen fail is a tripwire
nobody trusts.

It earned its keep immediately: it caught two patches
(`ring_governor_auto`, `ring_governor_monitor`) that my raw-text scan had
declared clean, because they store the dash as a `\u2014` **escape** — the
file bytes are ASCII, the parsed name isn't. Checking the artifact instead
of the source text is the whole lesson.

Suite **2699 → 2816** (117 of the new ones are the per-file parametrised
sweeps). Still unseen: the chaos-x/y-into-scope-**xy** butterfly — the
screenshot has the scope in `mono` mode, so that view is still owed, and
it needs two `cv_to_audio` bridges since chaos's outs are cv and the
scope's traces are audio.

---

## 2026-08-21 — the pre-Start param bug: ten doors, one door

Matthew: "yes for the pre-start param fix please". So the bug found while
building the possibility panel gets its proper fix.

**What it was.** `App._on_param_changed` — the callback behind nearly
every widget in the app — called `backend.set_param` and nothing else.
`NumpyBackend.set_param` opens with `if self._patch is None ... return`,
and the backend only receives a patch when **Start audio** compiles one.
So from launch until the first Start, every knob, slider, combo, fader
and transport button edit went nowhere: the widget moved, the model
didn't, and a save at that moment wrote the values the patch was opened
with. Nine more hand-written callbacks had the same shape.

**The fix** is the order, not the mechanism: `App._set_module_param`
writes `module.set_param(...)` first and *then* notifies the backend.
Once a patch is compiled the two orders are identical — the backend's
`set_param` performs the same assignment — so the change only touches
the case that was already broken. All ten sites now go through it, and
`backend.set_param` appears exactly once in `ui/app.py`, inside the
helper. The helper returns True when the *model* took the value: a
backend that refuses is reported but doesn't stop a caller mirroring the
new value into its widget, which is what the file-player transport and
the fader tooltip actually want to know.

Sites moved: the generic `_on_param_changed`, `_on_fader_pitch`,
`_on_fm_ratio_changed`, `_on_buffer_size_changed`, `_on_file_transport`,
the WAV dialog's path write, `_advance_playlist`, `_set_vel_curve` and
`_on_vel_mult_changed`. Two behaviour details worth noting: the playlist
advance's error text unifies from "Queue error" to "Param error" (one
door, one message), and the transport/dialog callbacks now mirror their
widget whenever the *model* accepted the value rather than bailing on any
backend hiccup — strictly the more correct read.

**Tests.** `tests/test_param_writes.py`, 13. Both halves of the claim:
edits land before Start (the bug) *and* still land after it (the
no-regression). The headline one saves a patch edited before Start and
reloads it — the data-loss scenario, end to end. Then one per
hand-written callback, plus the unknown-param and deleted-module paths,
plus the `device` branch's sink-baseline bookkeeping which hangs off the
generic callback and had to survive the rewrite.

The one I'd keep: a **tripwire** that reads `ui/app.py`'s own source and
asserts `self.backend.set_param(` appears exactly once. The bug wasn't
hard to fix, it was hard to *notice* — a new callback written in the
obvious style would silently reintroduce it, and no behavioural test
would catch that until someone lost work. Counting the doors is the
cheap way to keep there being one.

Suite **2686 → 2699**. Rule written into docs/architecture.md's
compile-vs-set_param section, since that's where a future contributor
would look for it.

Also today: **scope face eyeball PASSED** — Matthew: "Scope seems to work
well" — and `chaos_melody` "sounded fine". That empties the GUI eyeball
queue apart from the chaos-x/y-into-scope-xy *butterfly* view specifically,
which he didn't mention either way; leaving it queued rather than
assuming.

---

## 2026-08-20 — the possibility panel: sixteen cells, one gesture

Matthew played `possibility_groove.json` — "exactly what I was hoping
for" — and asked to advance the panel. That's the TODO item the module
shipped with: the node was rendering **36 generic parameter rows**, which
is an absurd face for a module whose whole idea is a pattern you can read
at a glance.

**The face.** Sixteen step cells in a row, each showing `0` / `1` / `?`
and colour-coded — hit bright green, rest dark grey, undecided amber.
Amber for the `?`s is deliberate: they're what the module is *for*, so
they should be the first thing the eye lands on. One gesture, straight
from the source project's rack: **click a cell to cycle `0 → 1 → ? →
0`**. **Right-click** opens that step's odds (a `fires` slider, 0..1) in
a popup; the odds are kept whatever state the cell is in, so a value
dialled now survives the cycle back round to `?` — no hidden mode where
the control vanishes. Hover gives the cell in words ("step 3: undecided —
fires 20% of takes") plus the two gestures, so the panel teaches itself.
`steps` / `mode` / `balanced` / `seed` sit above the row (balanced gets a
tooltip explaining the bag), and underneath is the **possibility
readout** — `4 ? -> 16 possible bars`, or `no ? -> 1 bar, decided`. That
readout is the number the module is actually about, and it was free once
the panel existed. Steps past `steps` grey out via a fourth "off" colour:
parked, not played, and they don't widen the space — the count ignores
them for the same reason.

**Where the logic lives.** `next_state`, `undecided_count`,
`possibility_count` and `format_possibilities` went into
`modules/possibility_seq.py`, dpg-free, alongside `collapse_pattern` —
the same instinct as the pure reference the renderer is pinned against,
and the `FADER_RANGE_ST`/`scope_math` precedent. The UI imports them, the
tests import them, so the on-screen count can't drift from the semantics.
`next_state` rescues a junk state to `"0"` rather than getting stuck,
which is what a hand-edited patch file deserves.

The panel repaints on the *model*, not on what it thinks it did:
`_refresh_possibility_panel` re-reads every cell's state and odds from
`module.params` after any change and rewrites label, colour and tooltip.
One path, so the face can never show a pattern the patch doesn't hold.
Themes are cached per colour and shared by every node — sixteen cells a
node would otherwise leak a theme apiece on every patch load. (First use
of dpg themes in the app; they degrade to default button colours if
`dpg.theme()` ever fails, since the label and tooltip already carry the
state.)

**A real bug fell out of building it, and it isn't the panel's.**
`App._on_param_changed` — the callback behind nearly every widget in the
app — only calls `backend.set_param`, and `NumpyBackend.set_param`
returns early while `self._patch is None`. The backend only gets a patch
when **Start audio** compiles one. So on a freshly opened patch, *every*
knob edit made before pressing Start is silently discarded, and a save at
that moment writes the old values. Verified directly: set `freq` to 220
before compile, model still reads 440.

The panel doesn't go through that path — it writes the model first and
then notifies the backend (`App._set_module_param`), because a cell that
flipped colour and then reverted on the next repaint would have looked
like a panel bug rather than the app-wide one it is. A regression test
pins it (`test_a_click_sticks_before_the_first_start`).

I did **not** fix `_on_param_changed` globally. It's a one-line swap to
the same helper and the two paths are identical whenever a patch *is*
compiled — so the change only touches the currently-broken case — but
it alters behaviour behind every widget in the app, which is Matthew's
call rather than a docs-drift freebie. Filed in TODO with the fix
written out, including the other hand-written callbacks
(`_on_fader_pitch`, `_on_buffer_size_changed`, `_on_fm_ratio_changed`,
the quantizer/organ ones) that go backend-only the same way.

**Tests / docs.** `tests/test_possibility_panel.py`, 20 tests in two
halves: the dpg-free helpers (cycle order, junk rescue, parked steps
excluded from the count, `2**k`, the readout's exact wording including
the thousands separator on 65,536), then the panel driven with dpg
mocked out — the `test_key_trigger_ui.py` pattern. Test lesson worth
keeping: a bare `MagicMock` returns **the same object** from every
`add_button()` call, so sixteen cells come back indistinguishable and a
cell-mix-up test passes vacuously; `_mock_dpg()` gives the widget
factories an `itertools.count` side-effect so ids are distinct, which is
what real dpg does. Two tests only became meaningful after that. The
suite also builds the node through the *real* `_create_node_for_module`
path and asserts zero generic param widgets are registered for the
module — the "36 rows are gone" claim, pinned. Plus a panel→engine test:
click cells into a one-hit bar, render, assert only step 1 fires. Suite
**2666 → 2686**. `docs/MODULES.md` gains a **The panel** section on the
entry, the `fader_seq` precedent.

**EYEBALL PASSED 2026-08-21.** Matthew recompiled and played it:
"works really well :-{D". That answers the one question the headless
tests couldn't — the right-click popup and the hover tooltip *do* get
along sharing a cell, so the odds stay on right-click and don't need a
different affordance. Colours and cell size at zoom confirmed fine too.
Nothing outstanding on the panel.

---

## 2026-08-20 — orientation pass: suite verified green, README + architecture de-drifted

A familiarisation session, no new DSP. Three things came out of it worth
recording.

**The outstanding possibility_seq caveat is closed.** The 2026-08-15 entry
shipped from a cloud sandbox against a partial checkout and asked for one
`pytest` on the real tree before trusting it. Run here on the project venv:
**2666 passed, 1 skipped in 148 s** (the skip is the `<3.11` graceful-skip
branch in `test_error_handler.py`, expected). No sandbox/real divergence.
TODO entry updated; only the listen is still outstanding.

**README was two months stale on its headline numbers.** It claimed *61
modules* and *~2,050 tests*; the registry actually holds **87** types
(Sources 19, Filters & EQ 7, Effects 21, Modulation 14, Routing & VCA 6,
CV & Utilities 12, Outputs 8 — counted off `all_module_types()`), and the
suite is 2,666. `docs/MODULES.md` was NOT stale — its index has all 87 rows,
which is the reassuring direction for the drift to run in: the reference doc
kept pace and only the shop window fell behind. Rewrote the seven category
bullets so everything shipped since June is actually visible to a reader
(supersaw, wavetable_morph, fm_op, organ, pluck, modal, the drum voices,
vinyl, octaver, matrix_mixer, mid_side, possibility_seq, the clockwork trio,
logic, chaos, shift_random, quantizer, chord, slew, scope, the buffered/
warping sinks) and put per-category counts in so the next drift is visible at
a glance.

**`docs/architecture.md` had three claims that time had falsified**: the
cabling rules promised a `Combiner` "coming in v0.3" and a `Splitter` "also
v0.3" (combiner/cv_combiner shipped long ago; fan-out needs no splitter at
all — cables live on the Patch, so one output feeds many inputs directly),
and said "v0.1 uses only `audio`" when all three kinds plus four bridge
modules are load-bearing. The anti-aliasing section still read as if
band-limiting were a future v0.2 item; rewrote it to describe the three
oscillator families that actually exist (naive / `*_blep` PolyBLEP+PolyBLAMP
/ `*_wt` mipmap) and noted that the derived sources inherit band-limiting by
construction. The layering diagram and the compile-vs-set_param split were
both still accurate and left alone.

**For Matthew:** `_to_delete/git-locks/` (20 stale `.lock`/`tmp_obj_*` files
from an interrupted git operation) is sitting untracked in the project root.
It's yours to bin — I've left it alone rather than delete a folder you
staged. Two commits are also still unpushed as of this session's start.

---

## 2026-08-15 — possibility_seq: the rack learns to be undecided

The bridge module from PythonBinaryPossibility landed: a step sequencer
whose steps are "0", "1" or "?" — undecided, resolved only when the music
needs an answer. Full polish standard: module + numpy renderer + pyo
silent stub + MODULES.md entry and index row + `examples/
possibility_groove.json` + 20 tests, tripwires (docs coverage, categories,
examples sweep) green.

The semantics came over from the other project, not the code — **ported,
not imported**, so core/ and modules/ stay dependency-free (PBP is flat
and unpackaged by its own deliberate convention). The pure reference
`collapse_pattern` lives in the module file (euclidean_pattern precedent)
and the renderer is pinned against it for exact take equivalence, so the
two projects can never quietly drift apart on what a "?" means.

Design notes worth keeping. `mode` is when the ?s decide: `loop` = a
fresh take every wrap; `latch` = one take held until a `reroll` edge (a
gate input — patch a slow clock in and the pattern re-decides itself
every N bars); `dice` = no memory. `balanced` deals fair ?s from a
width-1 shuffle-bag instead of coins — the measured switch: in the source
project an independent coin leaves one fair 16-step bar in fifteen
audibly lopsided (sd 1.95), the bag pins every bar to its share (sd
0.00), and that mechanism passed a formal admission test (E4, 2026-08-14)
the same week. Positioning is clean: `sequencer` owns decided patterns,
`bernoulli_gate` the all-random stream, `shift_random` the mutating loop
— this is the pattern with holes in it. All randomness from `seed`,
consumed only when a real choice exists; takes are a pure function of
seed + edge history, block-size independent (test-pinned).

The demo patch is three of them off one clock: a kick decided on the
floor with ?s on the and, a latched snare backbeat with 0.2 ghost notes,
and a fully-undecided balanced hat — into the drum voices and the mixer.
An 8-second offline render sounded like a groovebox making its mind up,
which is the point.

Honest scope note: built and tested in the cloud sandbox against a
partial checkout — 114 tests across the module's neighborhood (sequencer,
clockwork, shift_random, drums, examples, tripwires) all green, but the
FULL suite has not run here. **Matthew: one `pytest` on the real checkout
before trusting it**, per the working agreement. Follow-ups (panel
gesture, possibility-count display) are on TODO.

## 2026-08-05 — the WHOLE listening backlog clears (nine for nine)

Matthew went straight through the 2026-08-03 backlog in one sitting —
every example passed: **clockwork_groove** "something else… the most
unique one i've seen so far, everything short of a drum machine but
just as effective" (the 'fun one' flag was right);
**chord_arp_factory** "works well, like a unique or random song";
**drum_machine** "is a drum machine" (mission statement achieved);
**pluck_strings** "cool" (longer play session coming);
**modal_bells** (longer revisit coming); **shift_random_melody**
"another cool one"; **logic_offbeat_drums** :-{D;
**mid_side_breathe** — the sleeper hit — "pretty awesome, ima use
this in music for sure"; **octaver_bass_lead** :-{D.

That closes EVERY pending-ears item in the rack: fifteen examples
across two days, all passed, zero issues raised. Worth savoring:
mid_side (a Session A utility, the humblest module of the batch) is
the one headed for actual music. Remaining eyeball queue is down to
two GUI items: the scope face (60 fps, trigger, dual/xy, freeze) and
the chaos-butterfly-in-scope-xy demo. Per-entry pendings in TODO
updated; the backlog queue entry replaced by a scope-face-only one.

## 2026-08-05 — ears PASSED on all six of the 08-04 modules

Matthew recompiled and played the whole day-of-six: **organ**
("beautiful!"), **matrix_mixer** ("unique"), **chaos** ("well
organized chaos — I like it"), **supersaw** ("awesome"),
**wavetable_morph** ("works well"), **vinyl** ("does exactly what it
says"). All six examples confirmed by ear; no issues raised. That
clears every pending-ears item from 2026-08-04.

Surfaced idea (Matthew's): **feedback with the organ** — the organ's
sustained tones through the matrix's regenerating loop. Queued in
TODO as an example-patch idea (organ → matrix → delay/reverb → back;
the soft ceiling makes it safe to let it sing).

Still queued for next PySynthRack session (Matthew: "i'll pick these
up next time"): the 2026-08-03 listening backlog —
`clockwork_groove.json`, `chord_arp_factory.json` (+ chord slot-bank
feel), `drum_machine.json`, `pluck_strings.json`, `modal_bells.json`,
`shift_random_melody.json`, Session A's three
(`logic_offbeat_drums`, `mid_side_breathe`, `octaver_bass_lead`) —
plus the **scope real-GUI face** eyeball (60 fps, trigger, dual/xy,
freeze).

## 2026-08-04 — the brainstorm keep-list lands on the roadmap

Matthew pasted the "what's left" brainstorm back and asked for all of
it on the todo: bowed/wind, function_generator, drift, cv_math,
cv_recorder, rotary, vowel, freeze, autopan, midi_output, subpatch
containers, snapshot morph — twelve items. Filed the house way:
one-liners into docs/MODULE_IDEAS.md § "The 2026-08-04 brainstorm"
(the canonical menu, where specs grow when picked) + a single
keep-list entry in TODO § Later / wishlist pointing at it. Added
cross-references the original writeup predated: drift vs chaos
(stumbles vs orbits), vowel vs wavetable_morph's vowel stack (filter
vs source), rotary as the organ's destined partner.
## quick-hit run is COMPLETE (A+B+C, seven modules)

Matthew: "Lets continue with session C please … loving this project
and your work" (and a reminder to keep the co-author tag on commits —
it's on every one). The oscillator double, and with it the 2026-08-03
seven-module plan closes: Session A (logic/mid_side/octaver), B
(matrix_mixer/vinyl), C (supersaw/wavetable_morph) all shipped. Suite
**2616 → 2645** (27 new tests).

**supersaw** (Sources). Seven PolyBLEP saws per voice through ONE
`_waveshape_blep` call on a folded (V, 7, F) phase block — the
spec's einsum idea worked exactly as written, per-saw equal-power pan
gains collapsing the saw axis into L/R buses. The classic asymmetric
detune table (unit-normalised, center at index 3) scaled to ±50 ct;
free phases seeded per (slot, saw) → deterministic renders, and slot
0 of a (1, F) render ≡ mono (pinned). Design pins that came out
clean: blend 0 = the center saw alone, so detune CHANGES NOTHING
(bit-equal renders across detune — a lovely negative-space test);
spread 0 → L/R gain vectors identical → outs bit-identical. Test
lesson (the third of its kind this week): detuned saws don't put
energy BETWEEN harmonics — each harmonic becomes a CLUSTER whose
skirt widens with detune; measure around harmonic 8, not at 1.5·f0
(first probe read dead flat and the DSP was fine). Perf RECORDED:
16 voices = 112 blep saws = **4.87 ms = 45.6 % of the 48 k/512
budget** — heavy by design, documented as "spend it on the pad".

**wavetable_morph** (Sources). The `*_wt` mipmap infra grown into an
instrument: `_bandlimit_frames` renders harmonic-domain frames into
(n_frames, 11 bands, 2048) stacks — the same additive per-octave
construction as `_get_wavetable`, one normaliser per FRAME (its
fullest band) so the position crossfade never pumps between bands.
Three built-ins: analog (sine→tri→saw→square — endpoints AND the
interior thirds land the pure shapes, all pinned spectrally), vowel
(five generic formant-bump frames), metallic (sparse seeded-phase
sets). Single-cycle WAV import = the cycle's own rfft bins ARE the
harmonic series (spectral resample for free), mip-banded like the
built-ins; bad path falls back to `table` silently (pinned
bit-equal). Browse shares the FilePlayer dialog — one TYPE-aware line
at the write-back site (`file` vs `path`). `position_cv_depth` added
per the house depth convention (the spec omitted it; conventions
rule wins — noted). Alias pin: +3 oct on the square end keeps
non-harmonic bins > 40 dB down. Click-free-sweep test lesson: the
band-limited square's own edge IS 1.225/sample — compare the sweep's
max delta against the stack's edgiest static frame, not a guessed
constant. Perf: 16 voices mid-crossfade 1.02 ms = 9.6 %.

Examples: `supersaw_chord_wall.json` (shift_random → quantizer →
chord → supersaw → per-voice adsr/VCA pair, stereo; first draft
railed at 1.0, amp 0.35 → 0.2, now 0.66 peak) and
`wavetable_vowel_talk.json` (LFO sweeps the vowel stack's position —
it talks; 0.48 peak). Docs/index/punts/exports/widgets in the same
commit; tripwires green. **Pending (meatthread0):** ears on both
(the chord wall is the payoff of the whole voice architecture; the
vowel talker demos position_cv), GUI eyeball of the Browse button on
wavetable_morph. Later ideas: supersaw fast path if the 45.6 %
worst case ever bites a real patch (constant-pitch arange or a
(V·7, F) reshape); multi-frame WAV import (slice a long file into
frames — the granular-adjacent idea); wavetable stack editor.

Matthew: "And another two please" (after pushing through 2fddab8) —
the plan of record said Session B, and Session B it was. Suite
**2587 → 2616** (27 new tests + the 4 governor topo tests refactored).

**matrix_mixer** (Routing & VCA) — the architecture question answered
better than feared: the backend ALREADY had the mechanism. The ring
governor's `fill` out established "delayed edges" (real signal, not a
within-block dependency; seeded from previous-block state before the
walk; ignored by Kahn). The matrix late-read is that pattern
generalized: `_compute_late_edges` at compile BFSes each cable into a
matrix_mixer (audio rows AND column cvs) asking "can the matrix reach
this cable's source?" — yes → the cable is marked late;
`_is_delayed_edge` (now an instance method consulting the compiled
set — the 4 governor tests' static call sites updated) makes the sort
skip it; `render_block_multi` seeds each late source's previous-block
buffer before the walk and stashes the fresh one after. One block of
loop latency, feed-forward stays zero-latency, first block reads
silence, `_late_prev` survives live recompiles. Demonstrated end to
end: a matrix→delay→matrix loop compiles, sorts (src < matrix <
delay), regenerates echoes, and the one-block latency is pinned by a
geometric-staircase test on a direct self-loop.

Two design calls to note. (1) **The soft ceiling is NOT a plain
tanh** (spec deviation, standing-principle): tanh(0.5) = 0.462 — a
default-ON guardrail that shaves 8 % off a normal signal is wrong for
a mixer stage. Shipped instead: identity below 0.95, then a
C1-continuous tanh section saturating at exactly 1.0 — so "identity =
bit-exact" AND "soft_clip default on" are BOTH true, runaway loops
land on the ceiling (pinned at 1.0), and off = unbounded growth
(pinned at >100 after 100 blocks; observed 6e7). (2) The ±1 gain
clamp (spec's own range) means a SINGLE gain can't push loop gain
past unity — discovered via a probe that refused to explode (g=1.3
silently clamped; every number checked out at 1.0). Loop gain > 1
needs parallel return paths (one return into two rows) — documented
in MODULES.md, and the growth tests build it that way.

**vinyl** (Effects) — dessert as planned. Determinism design: both
noise streams draw from `default_rng([seed, window_index, stream])`
per absolute-sample 4096-window, so ANY block split re-derives
identical dust — block independence bit-exact with all vices on (the
only carried DSP state is the wobble history and the rumble filter
zi, itself fed by the split-invariant stream). Crackle = seeded
Poisson ticks (rate AND size scale with the knob); rumble = white →
RBJ resonant LP 40 Hz (LF/HF ratio ~700 measured); wobble = 0.55 Hz
fractional-delay wobble, ±24 ct at full — pinned the spec's way:
Hilbert instantaneous-frequency of a tone shows the deviation cycling
at **0.559 Hz measured** (33⅓ rpm = 0.555…; well inside the pin).
All-zero knobs return the input buffer ITSELF. Unpatched input still
emits the noise bed (deviation: more useful than silence — a free
surface-noise generator). Test-calibration lesson repeated from the
organ: wobble depth at full is FM index ~11, so the spectrum is a
blob, not discrete sidebands — measure instantaneous pitch, not
spectral peaks.

Perf: matrix full-16-gain 0.108 ms = 1.0 % budget; vinyl all-vices
0.142 ms = 1.3 %. Examples: `matrix_feedback_echo.json` (kick through
a regenerating echo network, 0.60 peak, the loop's cable
late-marked) + `vinyl_dust.json` (lo-fi melody box, 0.70 peak). UI:
4×4 drag grid with row/col headers + vinyl sliders. Docs/punts/
exports/tripwires all in the same commit. **Pending (meatthread0):**
ears on both examples (crank `g21` toward 0.9 and listen to the
ceiling catch it); GUI eyeball of the gain grid. Later: shimmer
example (reverb + pitch_shifter through the matrix — the patch class
Session B unlocked); per-node cv if a real patch demands it;
`wobble`/`crackle` cv ins (+ depths per conventions).

Matthew: "ok back, execute the plan please" — the TODO build plan run
top to bottom, one session, one commit. Suite **2551 → 2587** (34 new
tests), all green including the docs tripwires.

**organ** (Sources). The checkpoint paid off big: the oscillator's
constant-frequency mono path uses an `arange` phase ramp with
`phases[0] = start`, so the organ's per-block-constant pitch could
mirror it exactly — and the lone-8′-drawbar pin landed **BIT-EXACT**
against the sine oscillator (max diff 0.0), the strong version of the
spec's "else < 1e-6" fallback. The enabling design call: the gate
envelope is an **integer-counted linear ramp** (`env = clamp(count ±
n)/R` with the count carried as an int) — it reaches exactly 0/1 so
held notes are bit-transparent, AND it's bit-exact across any block
split, where a float-accumulated level would drift in the ramp region.
Partials fold into one (9, F) sine eval per voice (per-block pitch,
pluck idiom); phases advance every block regardless of audibility (the
oscillator's rule) so activity gating never moves phase. Clicks are
seeded per (module, voice, hit) with drums-style carry tails; the
percussion register is ONE module-wide generator with a from-silence
qualification (any-voice-high on the previous sample, carried across
blocks) — legato provably doesn't re-fire (pinned), and a mid-block
re-fire replaces the ringing strike via a pour-cursor (a first-draft
bug: the second pour restarted at 0; caught before tests). Nyquist
mask pinned the sharp way: a pitch that puts a lone 1′ bar past sr/2
renders EXACT silence. Perf RECORDED: 16 unison voices, full
registration + click + perc = **2.82 ms/block = 26.5 % of the 48k/512
budget** (modal's neighborhood — 144 sines is real work; fine).
Fader-bank drawbar panel shipped (fader_seq lineage, checkpoint said
cheap and it was); example `organ_jazz.json` (keys → organ → chorus →
reverb), chord-held headroom re-checked after a 1.0-peak first draft
(level 0.5 → 0.3 in the example; 0.78 peak now).

**chaos** (Modulation). The absolute-sample control grid did what it
promised: **all four outs bit-exact across block splits** (64 vs 1024
vs single-frame blocks), including through a mid-block reset — and the
reset lands ON the seeded warmed-up start, sample-accurate (pinned to
`fresh[0]` equality). RK4 in pure-Python floats on 3-tuples (the slew
lesson: no numpy scalar dispatch in the hot loop) at rail dt
(lorenz ≤ 0.01, rossler ≤ 0.05), substep count derived from params
only; control points interpolated with np.interp at
`u = (frac + k)/DIV` so identical integers → identical floats → exact
splits by construction. Soak: 100k samples both systems, zero
non-finite, zero blowups, bounds honored. The chaos test itself: seeds
1 vs 2 decorrelate (|corr| < 0.9) while same-seed renders are
bit-identical. Rate calibration loose-pinned via z-peak counts (~20
orbits at rate 10 over 2 s, accepted 8..45). Gates: lorenz ≡ sign(x)
elementwise (pinned), rossler duty 0.5–40 % sparse bursts (pinned).
Perf: rate 50 = **0.24 ms/block = 2.2 %** — negligible. Example
`chaos_melody.json` self-plays at 0.63 peak (x→quantizer→osc,
gate→adsr→vca, z→cutoff — one orbit runs the whole patch).

Both: pyo punts, `__init__` exports, MODULES.md index rows + full
entries, bounded widgets (organ combos/sliders + the drawbar bank;
chaos combo/drags, seed drag) — the polish standard in the same
commit. **Pending (meatthread0):** ears on both examples (the organ
wants real keys — headless verified via injected note_on chord), and a
GUI eyeball of the drawbar bank + the butterfly in scope xy mode.
Later ideas: organ foldback + leakage hum + vibrato scanner (chorus
covers for now); chaos audio-rate mode, `rate_cv`, the guarded ρ/c
morph knob.

Matthew: "lets knock something off the build queue or 2 maybe?" — I
picked the fun pair (both S–M, one session, the Session A shape) and
started the research sweep (module/export/renderer conventions
re-read; octaver + __init__ confirmed as the shape to copy). Mid-read
Matthew called for a plan instead ("actually knock together a plan" +
"i have to suspend soon to change rooms") — so the full build plan is
written into the TODO entry (§ Later / wishlist, the organ+chaos
bullet): organ first then chaos, phase-by-phase file list, the two
verify-before-claiming checkpoints (the oscillator phase convention
before the lone-8′ pin; fader_seq's panel cost before promising the
fader bank), the chaos numeric constants (dt rails, T_orbit
calibrations, bound tables), and the close-out standard (suite +
tripwires + ONE commit for the pair). Nothing was built — no source
files touched; the tree is docs-only ahead of origin. Next session:
open the TODO entry and execute top to bottom.

Matthew pushed everything through 94cea1b (origin current), then picked
two more off the brainstorm: "organ & chaos please". Both spec'd into
docs/MODULE_IDEAS.md at the house standard, same treatment as the
sampler — spec only, build when picked.

**organ** (Sources, S–M). Nine drawbars at the classic footages
(ratios ½ 1½ 1 2 3 4 5 6 8), integer 0..8 levels on a ~3 dB/step law,
default 888000000. The implementation hook: fold the nine partials
into the voice axis — one vectorized sine call over (V·9, F), the
supersaw-spec idiom — so the whole instrument is one `_osc_waveshape`-
class evaluation plus weights. Design calls: gate-on/gate-off with NO
envelope (that IS an organ; `click` covers the onset — seeded
contact-bounce tick, or a 1 ms declick ramp at click=0); percussion
register is single-trigger from silence (legato doesn't re-fire — the
musically load-bearing classic, and the test that matters); partials
above Nyquist masked (foldback = authenticity stretch); panel is the
fader_seq fader-bank with faders-up-is-louder (documented deviation
from hardware drawbar direction); vibrato scanner deliberately
excluded — the chorus module is the patch. Neutral pin: lone 8′
drawbar = a pure sine, pinned against the oscillator's own sine
render. Perf to RECORD at 16 voices × 9 partials.

**chaos** (Modulation, S–M). Lorenz/Rössler strange-attractor CV — the
corner no random source covers: shift_random loops, lfo-random steps,
drift (unbuilt) smooths noise; chaos *orbits*, never repeating yet
fully seed-deterministic (the house randomness rule satisfied by
having no randomness at all). Design calls: RK4 at a rail-limited
internal substep, evaluated on a ~16-sample control grid + linear
interp to audio rate — the slew per-sample-Python trap never opens;
substep + control-point carry makes block-size independence EXACT.
Three coherent outs (x/y/z of one orbit) + a per-system gate with two
genuinely different rhythm characters: lorenz = lobe-sign square that
hangs unpredictably, rossler = sparse z-spike bursts. Per-system
bound normalization, warmup transient skip, non-finite tripwire. The
sell-it demo: x/y into scope xy mode — the butterfly on the node.
Stretch explicitly gated: a ρ/c morph knob risks fixed-point collapse
(frozen CV) and needs a guard before it's offered.

Queued in TODO § Later / wishlist as one combined entry ("the fun
pair"). Index line updated. Spec-only session; next natural step when
picked: organ first (smaller, instant payoff), chaos after.

Matthew asked what modules were even left ("i think we are running out
of options here which is a good thing :-{D"). Answer: not close — the
obvious holes are filled, but whole veins remain. Surveyed the roster
first (waveshaper already IS a wavefolder, noise already does pink —
the two classic suggestions that would have been duplicates), then
pitched genuinely new territory: sources (`sampler`, `organ` drawbars,
`bowed`/`wind` waveguide, `chaos`), modulation (`function_generator`
with EOR/EOC — the Maths move, `drift` smooth random, `cv_math`,
`cv_recorder`), effects (`rotary`, `vowel`, spectral `freeze`,
`autopan` — there is no dedicated panner in the rack), `midi_output`,
and the endgame pair (subpatch containers, snapshot morph). Also noted:
Session B's matrix_mixer feedback quietly turns shimmer reverb into a
*patch*.

Matthew picked **`sampler`** (":-{D"). Spec written into
docs/MODULE_IDEAS.md § New voices at the house standard — the framing
that makes it tractable is that every hard part already shipped:
media.py any-format decode, the convolver's off-thread whole-file load
(silent until ready, patches always load), the resampler's `_hermite4`
cubic read (bit-exact at integer positions — which is exactly what
makes the neutral pinnable), and pluck's per-voice contract
(1 V/oct, per-block pitch, silent-voice early-out). Design calls worth
recording: it's a *voice*, not a transport (the FilePlayer distinction);
mono-sum on load, stereo is stretch; `attack` defaults 0 so the neutral
stays bit-exact (declick ramps, not an envelope — adsr→vca is the
shaping lane); pitch-up aliasing kept as the sampler sound (bitcrusher
"deliberately aliased" precedent), mip-chain cleanliness is stretch;
the strong test is +12 st ≡ buffer[::2] bit-exact (integer stride
dodges the interpolator entirely). M–L, three slices. Queued in
TODO.md § Later / wishlist; Index line updated.

Not started — spec only, per the backlog's own workflow. Next natural
step when picked: slice 1 (core one_shot/gated + unity neutral).

Matthew: "Lets go with Session A please" — the utility sweep from the
quick-hit plan, built to the specs written in part eight. Three small
modules, one commit, zero new infra, all vectorized (no per-sample
loops anywhere in the three).

**logic** (Modulation). Pure boolean elementwise on thresholded
inputs; five outs (`and`/`or`/`xor`/`nand`/`not_a`) live at once, zero
params (the comparator-mode drop from the plan held up). The contracts
worth pinning turned out to be the *unpatched* ones: b-low → `or`/
`xor` pass `a` and `nand` idles high (the normalled-NAND trick), and a
(V, F) gate source collapsing to any-voice-high. The example turned
into a genuinely groovy trick: clock pulse_width 0.9 with euclidean
gate_len 0.45 makes `and` = the tresillo (kick) and `xor` = rest-step
beats PLUS a mid-step edge after each hit (hat shuffle) — asymmetric
widths chosen deliberately so every xor edge is well-separated (no
1-sample ghost-trigger risk from equal-width rounding).

**mid_side** (Routing & VCA). Sum/difference exact; `width_cv` adds
per sample clamped 0..2. One design call: a single patched input IS
the mid (level preserved, width inert) — not a half-level (L+0)/2
pair; the mono-passthrough contract is friendlier than the literal
math and is pinned. Decode-at-width-1 ≡ input is pinned bit-close.

**octaver** (Effects). The fun implementation: both flip-flops are
**cumsum parity** — ÷2 is the running parity of rising-zero-crossing
counts, ÷4 is the parity of the first flip-flop's rises — so the whole
tracker vectorizes with no scalar loop, state carried as two parity
bits + prev-sign. Envelope = the shared `_audio_to_cv_block`
asymmetric one-pole with fixed 5 ms/50 ms constants (silence stays
silent, tails release — both pinned). Subs sum → one one-pole LP
(`tone`), mixed under dry; dry-only returns the input buffer itself
(bit-exact, fan-out precedent) while the flip-flops keep advancing so
re-enabling a sub doesn't restart them. Test lesson: a one-pole is
6 dB/oct — the tone test's first threshold (0.25×) demanded more
attenuation at 3× cutoff than |H| = 1/√(1+9) ≈ 0.32 can give;
threshold corrected to 0.4× with the analytic note inline.

**Tests / examples / docs.** 33 tests (logic 8, mid_side 13, octaver
12): truth tables, unpatched contracts, M/S identities + clamps, FFT
f/2+f/4, tone slope, envelope gating + release tail, bit-exact
passthroughs, block-size independence, and a full-graph render each
(logic → kick/hat drums; mid_side → stereo sink with LFO width;
octaver under a played cv_keyboard voice). Suite **2551** (2515 + 33
+ 3 examples). Examples: `logic_offbeat_drums.json`,
`mid_side_breathe.json` (0.15 Hz bipolar LFO sweeps width 0..2),
`octaver_bass_lead.json`. MODULES.md entries + index rows; ideas doc
marked shipped; pyo stubs; UI arms (mid_side width, octaver mix/tone;
logic draws nothing — no params).

**Pending (meatthread0):** ears on all three examples. **Next:**
Session B (matrix_mixer + vinyl — the feedback architecture) on
Matthew's word, or granular/clock_divider.

## 2026-08-03 — planning: the quick-hit run specced and sliced (part eight)

Matthew asked for a todo/plan covering seven wishlist modules:
supersaw, mid_side, logic, vinyl, octaver, matrix_mixer,
wavetable_morph. Planning pass only — full specs promoted from the
quick-hit bullets into a new **"The quick-hit run"** section of
docs/MODULE_IDEAS.md (ports/params/DSP/tests each), and TODO.md got a
**Planned** block slicing them into three sessions per the working
agreement:

* **A — utility sweep** (S×3): `logic` + `mid_side` + `octaver`. Zero
  new infra; the clockwork-trio session shape.
* **B — patch bay & dust**: `matrix_mixer` (M) + `vinyl` (S).
* **C — oscillator double**: `supersaw` (S–M) + `wavetable_morph` (M),
  sharing the anti-aliasing/mipmap infra; demo = `chord` → both.

Design calls made while speccing (the ones worth arguing with):

* **`logic` drops the "comparator mode"** from the old bullet: port
  kinds are strict (`is_compatible_with` requires kind equality), so a
  cv out *cannot* cable into a gate in — the threshold knob would be
  unreachable dead weight, and cv→gate is exactly what `schmitt`
  ships for. Result: a zero-param module, all five jacks live.
* **`matrix_mixer` is the only real architecture work in the run**:
  feedback patching vs the topo-sorted DAG. Chosen plan: compile-time
  SCC detection marks only the cycle-closing cables into a matrix as
  late-reads (previous-block buffer → one-block feedback latency,
  documented); feed-forward stays zero-latency; tanh soft_clip
  default-on as the stability guardrail. Also deviated from "every
  node CV-able" (16 jacks of soup) to per-output-column CV (4 jacks).
* **`supersaw` folds its 7 saws into the voice axis** so the existing
  `_osc_waveshape("saw_blep")` vectorizes over (V·7, F) in one call;
  free phases are seeded-random per allocation (phase-locked saws
  buzz — the supersaw signature is the drift).

No code touched; suite untouched at 2515. **Next:** Session A on
Matthew's word — or granular/clock_divider first if he'd rather (both
still queued from the sign-off list).

## 2026-08-03 — arpeggiator + chord: the voice architecture in both directions (part seven)

Matthew called the pair off the wishlist: `arpeggiator` (poly→mono
collapser) and `chord` (mono→poly explorer) — "both fun exercises of
the voice architecture in opposite directions". Both to the
MODULE_IDEAS spec, one commit.

**arpeggiator** (Modulation). First module to *consume* `(V, F)` voice
buffers and emit mono: poly `pitch_cv` + `gate` in (cv_keyboard /
midi_input), `clock` + `reset` in, mono line out. The design center is
the held-set bookkeeping: keyed by **voice slot** with an **arrival
stamp**, so `order` mode is true as-played order even across slot reuse
(a slot-index sort would lie after the allocator recycles). Voice-gate
edges are vectorized into a sparse per-sample event map — a rise
carries the pitch read *at that sample* — so the frames loop stays
scalar-only (euclidean precedent) with no 16×F per-sample scan; pitch
is therefore **sampled at the rise** and later source wobble doesn't
retune a held note (pinned). Modes: up / down / updown (palindrome,
endpoints unrepeated — pinned) / order / random (seeded, one draw per
step, block-independent). `octaves` stacks passes (descending stacks
descend from the top — pinned). Sequence rebuilt from the held set at
each clock edge; position kept by wrapped index (the join/leave
semantics are pinned, including the wrapped-index double-note on a
leave). Chord empties → silence, rewind, and **pitch holds** so
downstream release tails stay in tune (cv_keyboard convention).
`hold` latches with falls-before-rises event ordering, so a press from
silence clears the latch *and rewinds* — the classic performance
latch; a live hold→off toggle prunes to the physically held. Gate runs
`gate_len` of the measured clock period, mirroring before two edges
(euclidean idiom verbatim). Mono 1D inputs work as V = 1 — one finger
+ octaves 2 is an octave arp.

**chord** (CV & Utilities). Mono `pitch_cv` + `gate` → `(4, F)` voice
rows, each `in + interval`. Deliberate shape call: **4 rows, always**
— not 16 (4× cheaper for every downstream per-voice consumer) and not
enabled-count (disabled slots stay gate-low so downstream per-voice
state never re-shapes on a live toggle). Presets fill the four slots
(`major`…`aug`, `5` = power chord with slot 4 off); `custom` reads
`interval_n`/`enable_n` (quantizer-tickbox-bank UI precedent, drawn as
checkbox+drag rows). Pitch is a pure stateless broadcast every sample
— glides chord along, release tails stay in tune. Gates are stateful
only for `strum`: onsets staggered `k × strum` ms over *enabled* rows
in absolute sample time (burst precedent, block joins pinned), falls
together, fall cancels unfired onsets (pinned). Fast paths: strum-0
mirrors the input verbatim; steady blocks fill by active flags; only
note-event blocks pay the 4×F scalar walk. `spread` opens the voicing
(0, +12, −12, 0 — major → the open −5/0/12/16, documented exactly).

**Perf** (block 256 @ 44.1 kHz, measured): arp worst-case (16 voices
all gating, 8 clock edges per block, octaves 4) **5.1 %** of budget;
chord note-event blocks **4.1 %**, steady vector path **0.7 %**.

**Tests / example / docs.** 48 tests (`test_arpeggiator.py` 25,
`test_chord.py` 23): every mode's exact walk, octave stacks, joins/
leaves mid-arp, hold latch + press-from-silence + live-toggle prune,
rise-sampled pitch, measured gate_len + pre-interval mirror, preset
rows exact, custom enables, spread, strum stagger/cancel/enabled-only,
strum across block joins, both block-size independent, and a full-graph
render each (arp: cv_keyboard chord → osc/adsr/vca; chord: (4, F) rows
driving a real voice-aware chain). Suite **2515** (2466 + 48 + the new
example). Example `chord_arp_factory.json` — the full circle:
sequencer root walk → quantizer (A minor) → **chord** (m7, spread,
25 ms strum) → **arpeggiator** (updown, 10 steps per chord) → saw →
ADSR → VCA. The two new modules exercising *each other*: mono → poly →
mono, self-playing. MODULES.md index rows + full entries; MODULE_IDEAS
marked shipped; pyo silent-stub tuple; UI: shared mode-combo arm +
octaves/gate_len/seed, chord preset combo + slot bank + strum ms.

**Pending (meatthread0):** ears on `chord_arp_factory.json` — and the
GUI feel of the chord slot bank. **Later:** arp `rate`-less clockless
mode (internal clock)? `swing` on the arp clock is really a
`clock_divider`/shuffle job (wishlist); chord `inversion` knob;
chord root-row `changed` trigger out for re-strums.

## 2026-08-03 — the clockwork trio: the drums play themselves (part six, day's end)

Matthew ("love this project!") called the clockwork trio as the day's
last build: `euclidean` + `burst` + `bernoulli_gate`, one commit
(8aa49fb), one family file (`modules/clockwork.py`, the drums.py
precedent).

**euclidean.** The arithmetic Bjorklund: `hit[i] = ((i+rotate)·fills)
mod steps < fills` — no recursion, and E(3,8) is the tresillo verbatim
(pinned; E(5,8) pinned as a rotation of the canonical Bjorklund set,
which is the accepted equivalence). The engineering wrinkle was
`gate_len`: a fraction of a *step*, but the step length is the clock's
business — so the renderer measures the interval between the last two
edges (carried across blocks) and holds each hit that fraction of it,
mirroring the clock's high time until a measurement exists. `accent` =
the sparser `accent_fills` layer INTERSECTED with the main pattern
(an accent on a rest is useless — design call, documented). Per-sample
loop on tolist()'d rows (sequencer precedent + slew scalar lesson).

**burst.** Ratchets: free-running bursts are scheduled WHOLE at the
trigger edge in absolute sample time — deterministic and block-size
independent by construction (the drum-buffer idea applied to gates).
`spread` warps the grid with a `2^spread` exponent on normalized
positions (accelerando ↔ ritardando, pinned by interval monotonicity);
`env` rides `(1−decay)^k` while gate k is high — a VCA patched from it
gets decaying ratchets with no envelope module. Clocked mode fires on
every `division`-th edge, mirroring the clock's high period. Retrigger
restarts (documented hardware behaviour, pinned).

**bernoulli_gate.** Whole-gate routing with the decision latched at the
rising edge — the segment-fill approach means `out_a + out_b`
reconstructs the input EXACTLY (pinned), gate lengths preserved across
block joins. One seeded rng draw per gate (reproducible; `p_cv` shifts
the coin at the edge; `toggle` mode flips-on-heads — p=1 alternates
strictly, pinned).

25 tests; suite **2466** pass / 1 skip. Example
`examples/clockwork_groove.json` — the self-playing groove: E(3,16)
kick, eighth-note hats coin-split closed/open (open choked by the next
closed), and the backbeat sequencer triggering 3-hit decaying snare
ratchets. 0.72 peak over four rendered seconds, and it actually grooves.

**Day ledger (2026-08-03):** housekeeping + polish audit slices 1–2,
quantizer + shift_random, scope, pluck, modal, the drum trio, the
clockwork trio — **13 modules shipped in one day**, suite 2306 → 2466,
every one documented, tested, exampled, and tripwire-protected.

**Pending (meatthread0):** push; then the fun part — load
`clockwork_groove.json` and listen to it play itself (nudge
`probability`, `fills`, `rotate` live). All the day's real-GUI/ears
eyeballs queued in their TODO entries.

## 2026-08-03 — modal + the drum trio: strike section (same day, part five)

Matthew: continue with pluck's siblings. Two commits — `modal` (13ef181)
and `kick_drum`/`snare_drum`/`hat_drum` (12e57ed).

**modal — the struck resonator bank.** Two-pole resonators at
`pitch × ratio[i]`; ratio tables from the physics shelf, not products:
free-free bar (β² series, exact first five betas then the (k+1.5)π
asymptote), stylized minor-third bell, circular membrane (**Bessel
zeros computed via scipy.special.jn_zeros**, not tabulated), plain
harmonics. `decay` = lowest-mode t60, `decay_tilt` kills highs faster,
`brightness` tilts gains, `inharm` stretches ratios (exponent
1 + 0.3·inharm). Engine = the slice-4 pattern: one lfilter per mode per
PITCH GROUP — voices sharing a block-mean pitch batch into one
vectorized call over their rows. **Measured (512 @ 44.1k): 16 unison
voices × 24 modes = 0.98 ms/block (8.4% of budget); worst case 16
distinct pitches × 24 modes = 3.44 ms (29.6%)** — the number the spec
asked to record. Rung-out voices early-out; stale modes zeroed on live
mode-count changes; modes above 0.45·sr dropped, not aliased.

*The lesson of the day:* the first drive normalization `b₀ = g(1−r)`
made the example render at −60 dB — it normalizes the ring-out
INTEGRAL, so long decays are fed proportionally less. A struck body
wants the STRIKE normalized: `b₀ = g·sinθ` (impulse-response peak ≈ g,
decay-independent). Also two test traps: the tilt comparison must use
the decay-rate *ratio* (drive normalization skews absolute tail
energies), and the inharm check needs ±3% windows — stretched mode 3
(1091 Hz) sits close enough to plain 4×C4 (1046 Hz) to spoof a sloppy
window. 17 tests; `examples/modal_bells.json` (noise-burst mallet via
gated VCA → 16-mode bell, per-voice bells from cv_keyboard).

**The drum trio — one shared engine.** Each hit is synthesized WHOLE
into a buffer at the trigger edge (seeded per module+hit), playback
advances through active buffers, and any retrigger fades the old tail
over ~2 ms — deterministic, bit-exact block-size independent, and
declicked by construction. kick = analytic pitch-dive sine (phase is
the exact integral of the exponential bend — pinned sample-exact
against the closed form) + 2 ms click + normalized tanh drive (NOT the
oversampling infra: kick is LF-dominant, foldover negligible —
deviation noted per the working agreement). snare = 185/330 Hz head
modes + band-passed wire noise, snappy balance. hat = six detuned
squares HP'd at 7 kHz (deterministic — the stack IS the noise; mild
square aliasing reads as character), closed/open sharing the voice with
the closed hit CHOKING open via the same fade — the pedal. 19 tests;
`examples/drum_machine.json` (three clocks + a backbeat sequencer →
kick/snare/hats → mixer; 0.89 peak over two rendered bars).

Suite **2440** pass / 1 skip (2402 + 17 modal + 19 drums + 2 example
loads). Day tally: suite 2306 → 2440.

**Pending (meatthread0):** ears on all four — `modal_bells.json`
(materials A/B, inharm cranked), `drum_machine.json` (the groove:
kick drive, snappy, open-hat chokes if you wire an open pattern).
**Remaining backlog siblings:** `granular` (L, sliced in the ideas
doc), `euclidean`/`burst`/`bernoulli_gate` (the clockwork trio that
makes the drums *play themselves* interestingly), `arpeggiator`/
`chord`, `spectrum`, and the quick hits.

## 2026-08-03 — pluck: the Karplus–Strong string (same day, part four)

Matthew pushed everything (089a695..8a3eede on origin) and picked
`pluck` off the menu — the extended Karplus–Strong string from
MODULE_IDEAS. A `cv_keyboard`'s 16 voices = 16 independent strings free.

**The loop.** Per voice: a ring buffer one pitch period long, fed back
through a damping one-zero and an allpass fractional delay, gain-scaled
per pass. The extensions that make it an instrument, not a demo:

  * **Tuning.** The blend `(1−d)·dry + d·avg` collapses to the one-zero
    `(1−d/2) + (d/2)z⁻¹`, whose low-frequency phase delay is exactly
    d/2 samples — compensated when splitting `sr/f0` into integer +
    allpass fraction (frac held in [0.1, 1.1) so the allpass coefficient
    stays conditioned). Pinned: **±5 cents C2..C6**.
  * **`decay` = real t60**: per-pass gain `g = 10^(−3·N/(sr·decay))` —
    the ring time reads in seconds at any pitch (textbook KS rings
    longer the lower the note). Pinned within 10% (at damping 0, where
    the formula is exact; damping adds HF loss on top, monotone-pinned
    separately).
  * **Exciter**: seeded noise per (module, voice, hit) — deterministic
    renders — shaped by `color` (one-pole LP: thumb → plectrum) and
    `position` (pick-position comb: burst minus itself delayed
    pos·period). **Zero-meaned before normalizing** — the fix of the
    session: the loop is unity-gain at DC (damping and allpass both
    pass DC), so a random burst mean rang as a slowly-decaying pedestal
    that buried the fundamental (the first pitch tests measured 1.7 Hz).
    Classic KS gotcha, now a comment + the contract test.
  * **Re-pluck ADDS into the ring** rather than replacing — the loop is
    linear so plucks superpose: physically right and click-free by
    construction (pinned: the retrigger step stays in the same class as
    a fresh pluck's own attack).

**Engine.** Chunked ring advance: chunks of ≤ one loop length mean every
read (the N and N+1 taps) lands before the chunk's writes, so each chunk
vectorizes — damping is an array blend, the allpass is one `lfilter`
with carried `zi`. Low notes = 1–2 chunks/block; high notes degrade into
more, smaller chunks instead of a per-sample loop (the spec's "dual
engine" made unnecessary). Decayed voices early-out to exact zeros
(ring zeroed, so a silent 16-voice pluck costs nothing). Block-size
independent bit-exact under constant pitch (pinned); pitch re-read per
block (mean) for glides, locked from the trigger sample for the pluck.

**Test-side lesson #2:** the naive "FFT global peak = fundamental"
failed even after the DC fix — a bright pluck's strongest partial was
harmonic 5. The tuning tests now measure the *fundamental partial*
(local peak within ±6% of expected, parabolic-interpolated), plus a
separate fundamental-energy-present guard.

21 tests (`test_pluck.py`); suite **2402** pass / 1 skip (+21 +1
example). Example `examples/pluck_strings.json` (keys → pluck → a little
reverb → stereo out) renders a C-major chord at 0.71 peak, natural
decay. Docs entry + index row; MODULE_IDEAS SHIPPED annotation; pyo
punt; UI block (decay s + four 0..1 sliders).

**Pending (meatthread0):** the ears eyeball — play it! Chords, damping
0 vs 1, color/position feels, fast trills on one key (the re-pluck
superposition), and long-decay low notes. `modal` and the drum voices
remain the natural siblings on the backlog.

## 2026-08-03 — scope: the waveform on the node (same day, part three)

Matthew: "lets continue with a scope". The MODULE_IDEAS spec, adapted to
the house architecture — the force-multiplier module: every "what is this
module doing to the wave?" question becomes a glance at the node.

**Design.** A pass-through tap (the meter precedent, extended): `in` →
`out` and `in_r` → `out_r` bit-exact (same array, voice shapes intact),
with the display fed off capture rings beside the path. The spec said
"audio or cv" on one jack — the port model enforces kind equality, so the
scope instead grew a dedicated **`cv` jack as the fallback main trace**
(`in` wins when both patched): LFOs/envelopes/quantizer steps display
with no `cv_to_audio` bridge. `trig` (gate) is an external trigger that
overrides the level trigger — clock-locked sweeps.

**Split across layers, deliberately.** The audio thread only memcpys
into per-jack capture rings (6 s: the 500 ms/div × 10 div max window + 1 s
of trigger-search history; rings lazily created, one shared write pos so
they stay sample-aligned). The backend exposes a raw-data hook
`scope_window(id, n)` (tail reconstruction, two slices). ALL display
maths — window sizing, trigger search, min/max decimation, polyline
geometry — lives in the new dpg-free **`ui/scope_math.py`** (zoom.py
precedent), called GUI-side per frame: `build_snapshot()` orchestrates
(trig-ring override → level trigger → free-run end-aligned; mono/dual
columns or xy decimation). This deviates from the spec's "render thread
writes decimated columns" — GUI-side compute keeps the audio thread
lean, and the whole pipeline (renderer → scope_window → build_snapshot →
geometry) tests headless. A GUI read can catch a torn ring frame:
one glitchy visual frame, self-heals (meter precedent, documented).

**Decimation.** `minmax_columns` buckets every sample into exactly one
column via `floor(i·columns/n)` and takes per-column min+max with
`np.minimum.at`/`maximum.at` — a one-sample click can never fall between
pixels (pinned by test); short windows zero-order-hold. The face draws
ONE zig-zag polyline (max then min per column) instead of per-column
lines — 2·columns points, cheap. `xy` mode is the goniometer: `in` vs
`in_r`, stride-decimated.

**Trigger.** `find_trigger_index` picks the LAST level crossing that
still fits a full window (newest aligned sweep); rising/falling via
vectorised crossing masks. Phase-lock pinned by test — with a lesson:
the first version used a sine whose zeros landed exactly ON sample
points, and ±2e-15 rounding flipped which sample "crossed". Real signals
don't sit on the boundary; the test now offsets phase. Free-run (or no
crossing) shows the end-aligned newest window.

**UI.** The node carries a 220×110 drawlist face (dark green, centre
gridlines, green main trace + amber second trace); `_update_scopes` in
the frame loop asks the hook, runs scope_math, reconfigures the
polylines (per-scope try/except self-heal; `_scope_displays` pruned on
delete + patch load — the meter-bar lifecycle). `freeze` simply skips
the repaint: the picture stops, the audio doesn't. Params: time_div
1..500 ms/div, gain 0.1..10×, trigger combo, level slider, mode via the
shared mode-combo branch (scope arm added).

**Tests.** 25 (`test_scope.py`): bit-exact pass-through mono + voice
(same-array identity), cv-fallback trace, ring accumulation/tail/cap,
block-size-independent capture, spike-proof decimation, exact column
counts, trigger rising/falling/free/none/phase-lock/external-override,
snapshot shapes for all three modes, geometry (gain scaling, clipping,
flat-column single point). Suite **2380** pass / 1 skip (+25 +1 example).
Example `examples/scope_tap.json`: saw → resonant LP (LFO-swept cutoff)
→ scope → speaker; renders 0.64 peak with a live snapshot verified
headless.

**Pending (meatthread0):** the real-GUI eyeball — the face drawing at
60 fps, trigger holding a 110 Hz saw still, the LFO sweep visibly
rounding the corners off, dual/xy modes, freeze, and CPU staying calm
with a couple of scopes in the patch.

## 2026-08-03 — quantizer + shift_random: the rack writes its own melodies

With the polish arc closed, Matthew took the recommendation off the module
backlog: the generative pair. Both specs came from docs/MODULE_IDEAS.md
(adapted, not followed verbatim — deviations below) and both shipped in
one session with the docs-coverage tripwires doing their job (the build
literally can't ship an undocumented module now).

**`quantizer` (CV & Utilities).** CV → nearest scale note, 1 V/oct C4=0.
Ten built-in scales + `custom` (twelve pitch-class tickboxes, empty set →
chromatic fallback so it can't wedge); `root`; `transpose` applied AFTER
quantization (a transposition, not a scale rotation — can leave the
scale, documented + pinned). Two modes by patching: continuous (every
sample; `hysteresis` cents make the held note sticky — a new note must be
more-than-margin closer, killing boundary flutter) and gated (rising
edges only, drift-proof clocked melodies; gate read mono, applies to all
voices). `changed` fires a ~5 ms per-voice trigger per new held note,
carried across blocks; held note primed to the first input so patch load
fires nothing. Voice-aware (V,F) with per-voice held/pulse state.
**Engine notes:** allowed-note table ±5 oct via `searchsorted`; hyst=0
fully vectorized; hyst>0 fast-path skips any block whose stateless
nearest never leaves the held note, else a pure-Python scalar scan on
`.tolist()`'d rows (the slew CPU lesson applied from day one). Ties round
down, documented in the nearest() helper.

**`shift_random` (Modulation).** The Turing-machine-style looping shift
register: 16 bits, rotate per clock rising edge, the bit recirculating
from position `length` flips with `probability` — 0 = locked loop
(a found melody), 1 = every bit flips (the complemented loop, period
exactly 2×length — pinned by test), ~0.1 = a motif that slowly mutates.
CV = first 8 bits as a byte (newest = MSB, documented) / 255 × `range`
(`bipolar` centres it); `gate` mirrors bit 0 held between clocks — a free
rhythm line. `write` held high forces incoming bits to 1 (performance
handle). All randomness from `seed` (register fill + every flip):
deterministic, block-size independent by construction (RNG consumed once
per edge), live seed change re-rolls. Mono like the clock that drives it.

**Spec deviations (worklog-noted per the working agreement):** the spec
gave no `probability` default — chose 0.1 (locked-ish, musical) and
documented; `write` semantics pinned as force-to-1-while-high; the byte
mapping's bit order pinned as newest-bit-MSB. The quantizer's gated mode
skips hysteresis (edges are discrete events — nothing to flutter).

**UI.** quantizer: root/scale combos, hysteresis 0..50 ct, transpose
±24 st, and the twelve custom tickboxes drawn as two compact rows of six
(the bank renders when the first tickbox param comes up; the rest draw
nothing — a new widget pattern worth remembering). shift_random:
probability/length/range/seed with bounds; bipolar rides the generic
checkbox.

**Tests.** 43 new (`test_quantizer.py` 22 + `test_shift_random.py` 21):
scale membership per built-in scale, root shift, semitone-rounding
neutral, hysteresis holds ±4 ct wobble at a boundary (and flutters at
hyst 0), changed-pulse length + cross-block carry + no-spurious-priming,
gated edge/hold/no-retrigger, custom tickboxes + chromatic fallback,
post-quantize transpose, voice independence + mono≡single-voice,
block-size independence (both), p=0 exact period, p=1 period 2×length
(both after the 16-clock drain of the initial fill through the register —
the tests initially read the byte too early, a good reminder the byte
carries history), seeded reproducibility + live re-roll, write fill,
uni/bipolar mapping, held-high no-retrigger, unpatched holds.
Suite **2354** pass / 1 skip (+43 new + the example picked up by the
examples loader test).

**Example.** `examples/shift_random_melody.json` — the endless melody
box: clock → shift_random → quantizer (pent. minor, −12 st) → osc;
register gate → ADSR → VCA. Renders at 0.39 peak, rhythm rests and all.

**Gaps left on the shelf, deliberately:** pyo runs both as silent stubs
(punt list, notice printed); a scale-aware *display* (note names on the
node) would be a nice later touch; MODULE_IDEAS.md got SHIPPED
annotations for both (+ the missing one on slew, drift found in passing).

**Pending (meatthread0):** real-GUI eyeball — load
`shift_random_melody.json`, listen to the loop mutate at a few
`probability` settings, flip scales/roots live, and check the custom
tickbox rows render sanely at zoom.

## 2026-08-03 — module polish slice 2: the bounded-widget sweep (same day)

Matthew said continue, so slice 2 followed slice 1 straight away. The
audit's "seven modules on generic drags" under-counted: its literal-mention
heuristic saw params like compressor `attack` "mentioned" in app.py, but
the mentions were *other* modules' TYPE-gated branches. Implementing
verified every param of the touched modules end-to-end and found two
genuinely **misleading widgets**, not just missing bounds:

  * **transient_shaper `attack` / `sustain`** — bipolar −1..+1 rebalance
    gains (±12 dB at the rails), but `attack` hit the generic
    envelope-TIME branch (0..5 s, `%.3f s` — wrong unit, wrong semantic)
    and `sustain` the generic 0..1 level slider. Neither could reach the
    **cut half of its range from the UI at all.** Now ±1 sliders.
  * **compressor** — `attack`/`release` are in **ms** (defaults 10/120)
    but hit the generic *seconds* branch: a 10 ms attack displayed as
    "10.000 s" and the drag clamped at 5 — under the release's own
    default — while `gain` (make-up, dB) sat on the generic 0..2 *linear*
    slider. Full block now: threshold −60..0 dBFS, ratio `%.1f:1` 1..20,
    attack 0.1..250 ms, release 5..2500 ms, knee 0..24 dB, gain 0..24 dB,
    mix 0..1, threshold_cv_depth 0..24 dB/unit.

New TYPE blocks: **compressor**, **transient_shaper**, **tape**
(wow/flutter/drift/sat 0..1, hiss −80..−30 dB, bump 0..6 dB, mix),
**freq_shifter** (shift ±2000 Hz, shift_cv_depth Hz/unit, feedback 0..0.9
— the engine's stability clamp — mix), **convolver** (predelay 0..500 ms —
the follow-up queued since it shipped — tone 1k..20k Hz, mix),
**audio_to_cv** (attack_ms 0.1..500 / release_ms 1..2000, honest ms
labels), **cv_to_frequency** (all six Hz anchors, f0/fm/f1 + `_neg` trio,
the EQ-style 20..20k drag). Extended blocks: **bitcrusher** (bits 1..24 +
rate_div 1..64 int sliders, jitter + mix 0..1), **midi_input** (bend_range
0..24 st, mod_scale/pressure_scale 0..4 ×).

Checked-and-already-fine along the way: vocoder / limiter / meter /
noise_gate had correct ms-or-seconds branches all along; every other
`mix`/`feedback` lives inside its module's own block; `mix` has NO generic
branch (it was landing on the bare unbounded drag wherever a block missed
it — bitcrusher, convolver, freq_shifter, tape — all now covered).

The audit's section C — params falling to the bare generic widget — is now
**empty across all 64 types**. Suite 2310 pass / 1 skip, unchanged (widget
branches are dpg-only per house pattern; no render path touched).

**Pending (meatthread0):** one real-GUI pass over the reshaped nodes —
compressor (honest ms + dB now), transient_shaper (bipolar sliders), tape,
freq_shifter, convolver, bitcrusher, cv_to_frequency, midi_input, and
slice 1's slew/speed combos — to confirm the ranges feel right in play.

## 2026-08-03 — module polish: audit + slice 1 (docs, exports, widget gaps)

Matthew asked for a polish pass across all the modules. Before starting, the
2026-07-20 session's loose ends were landed (previous commit): the worklog
compaction (May–June → WORKLOG-ARCHIVE.md), the stream-health doc snippets
merged into WORKLOG/TODO, the applied `0001-*.patch` + snippets file removed,
and a stale `.git/index.lock` (dated Jul 20, no git process alive) cleared.

**The audit.** A registry-walking script cross-checked all 64 module types
against MODULES.md headings + index rows, `modules.__all__`, app.py's
`_add_param_widget` dispatch (literal + pattern branches), the pyo punt
list, and the CV/depth conventions. Findings fixed this slice:

  * **Docs drift** — `slew` and `warping_buffered_speaker_output` had no
    MODULES.md entry at all, and `disk_writer` was still a "_To document._"
    stub. All three written (index rows + full entries, house style per
    section).
  * **Exports** — `modules.__all__` was missing `FaderSeq`,
    `BufferedSpecificSpeakerOutput` and `WarpingBufferedSpeakerOutput`
    (the sinks weren't even imported by name). Fixed.
  * **pyo punt list** — missing `slew` and `key_trigger`. Benign (the
    dispatch falls through to the same silent `None`) but those two skipped
    the courtesy "not supported in pyo" console notice. Added.
  * **Text-box params** — `slew.shape` rendered as a raw input_text (type
    `exponential` by hand); `transient_shaper.speed` likewise — the exact
    follow-up the TODO promised when the shaper shipped. Both are combos
    now (new `SLEW_SHAPES` constant; `TRANSIENT_SHAPER_SPEEDS` already
    existed). `slew.rise_time`/`fall_time` also got real drags
    (0..10 s, `%.3f s`, fine speed) instead of the unbounded generic.
  * **Warping-sink widgets** — `brake_time`/`spinup_time` fell to the
    GENERIC unbounded drag: the 0..5 s branch the audit's grep "saw" is
    inside the *resampler's* TYPE block. Notably Matthew's eyeballed sweet
    spot is 10 s — off the end of even that range. New branch in the
    stereo-sink family block: 0..30 s, `%.2f s`. `ratio_depth` (both
    buffered sinks) got 0..1 `%.2f x/unit` (±1 CV at 1.0 spans the
    engine's full 0.5..2 ratio clamp).
  * **Tripwires** — new `tests/test_docs_coverage.py` (4 tests): every
    registered type has a MODULES.md heading AND an index row, every
    heading maps to a registered type, every registered class is exported
    (and every `__all__` name resolves). Undocumented modules are now a
    test failure, not an audit finding.

**Audited and fine** (recorded so nobody re-audits): mixer `gainN_cv` and
oscillator `freq_cv`/`amp_cv` are depthless *by convention* (unity-normal
level jacks / calibrated 1 V/oct — a depth knob on osc `freq_cv` would
break keyboard tracking); motion_eq's shared `gain_cv_depth`/`q_cv_depth`
scaling its per-band ports is correct, not an orphan; sequencer/fader_seq
steps and the EQ band params ride pattern-based widget rules
(`endswith("_pitch")` / `_freq` / `_gain` / `_q`), fader_seq draws its own
panel; and the "thin" class docstrings on combiner/cv_combiner/disk_writer
sit atop rich module docstrings — house style, not neglect.

Suite **2310** pass / 1 skip (+4 on the 2306 baseline measured pre-change).

**Pending (meatthread0):** a real-GUI eyeball of the new widgets — slew's
`shape` combo, the shaper's `speed` combo, and the warping sink's
`brake_time`/`spinup_time`/`ratio_depth` drags (render + apply are
dpg-only paths).

**Remaining — slice 2, queued in TODO:** the bounded-widget sweep. Seven
modules still park numeric params on the generic unbounded drag_float:
audio_to_cv, bitcrusher, compressor, convolver (the long-queued predelay
slider), cv_to_frequency, freq_shifter, midi_input. Each format string
added also tightens scroll-to-adjust's step sizing for free (it keys off
displayed precision).

## 2026-07-20 — stream-health readout: measuring before rewriting the render path

Matthew raised the real motivation behind the buffer-size slider: the output
**skips when Chrome loads tabs**, but the buffer never exhausts and DSP% never
pins. His instinct was to move rendering off the callback thread so the output
can keep draining a queue while the renderer catches up. Read the code — he's
right about the diagnosis.

`_fill_output` calls `render_block_multi(frames)` *inside* the PortAudio
callback. Every block is strictly just-in-time: the deadline is one block
period and nothing anywhere can absorb a scheduling spike. That is a **jitter**
problem, not a throughput one, and a queue is the textbook fix.

**Why the async queue is worth more than a bigger buffer.** In-callback
rendering can never bank work — its slack is `(1 − load) × block_period`,
instantaneous and non-accumulating. An async renderer feeding a ring banks the
*full queue depth* regardless of load and refills itself after a stall. At ~50%
DSP load, N blocks of queue buys roughly twice the glitch immunity of N blocks
of extra device buffer. Per millisecond of added latency it's strictly the
better deal — and the depth *is* added latency on every note-on and knob turn,
which is the honest cost.

**The thing that could sabotage it: the GIL.** The device callback stays
Python, so it must acquire the GIL before it can memcpy out of the ring. The
ring absorbs *render* jitter, not *GIL-acquisition* jitter. Still a large win
(from "5 ms GIL wait + 7 ms render vs a 10.6 ms budget" to "5 ms GIL wait +
50 µs copy"), but it wants `sys.setswitchinterval(0.001)` alongside so the
per-sample ADSR/filter loops can't sit on the GIL for 5 ms at a stretch. The
fully GIL-proof variant — no callback, blocking `stream.write()`, PortAudio's
own C thread on drain duty — is awkward with the duplex mic stream, so not
first.

**Two cheaper things that may matter more.** (1) *Thread priority*: PortAudio
boosts its own callback thread; a plain `threading.Thread` gets normal
priority, so moving the render onto an unboosted thread could actually lose
ground. `AvSetMmThreadCharacteristicsW("Pro Audio")` + `SetThreadPriority` via
ctypes is ~20 lines and targets "Chrome stole my CPU" directly. (2) *Host API*:
the main stream opens with no `device`/`latency`/host-API hint, so Windows
almost certainly lands on **MME**, PortAudio's most jitter-prone backend.

Matthew picked **measure first**, so this slice is instrumentation only.

Shipped: PortAudio's own `status` flags are now counted rather than printed —
`output_underflow` into `_xruns`, `input_overflow` into `_input_xruns` — and
the resolved host-API name recorded on `start()`. Toolbar gained `xrun N` and
`api <name>` beside DSP%, and `ui/dsp_load.diagnose(load, xruns)` turns the
*pair* into a one-line tooltip reading, because neither number alone can tell
the two failure modes apart:

  * underflows climbing while `_dsp_overloads` stays at zero → the render fits
    the budget and the **dispatch** was late. The async queue fixes this.
  * both climbing together → genuine **throughput** overload. A queue only
    postpones the glitch; the patch has to get cheaper.

Bonus realtime-safety fix found on the way: both callbacks used to `print()`
the status. Writing to stdout from the audio thread takes a lock and does I/O
on the one thread that must never block — the old logging made a glitch storm
worse exactly when it mattered. Counting replaced it, with a test pinning
stdout/stderr clean.

35 tests (`tests/test_stream_health.py`); sandbox suite 2273 pass / 11 skip,
+35 over the 2238 baseline at 79e4f05 (the 23 headless-dearpygui UI failures
are identical before and after — they need a real dpg context).

**Pending (meatthread0):** run it, load some Chrome tabs, and read the two
numbers together. That reading picks the next slice.

## 2026-07-18 — real-GUI feedback: warping eyeball PASS + slew CPU fix

Matthew ran both of the day's builds in the real app.

**Warping sink — eyeball PASSED.** Routed to a second device (HD Audio),
`auto_govern` on, `buffer_size` 1024, ring holding ~50% (4063/8192), 0 under /
0 drop — "these settings seem to work well." Notably he tuned
`brake_time` = `spinup_time` = **10.0 s** (vs my 0.6/0.3 guess): a slow, gentle
coast rather than a dramatic dive — the musical setting is a long tape drift,
so the wide slew range earns its keep. Warping sink now real-GUI confirmed.

**Slew — CPU spike, fixed.** He wired an LFO into the slew's `in` and CPU
spiked. Confirmed by benchmark: a single mono slew ate **~20% of a core**
(2.3 ms of the 11.6 ms block budget), and V=16 cost the *same* as V=1 — the
tell that it was per-sample **numpy dispatch overhead** (~512 ufunc calls/block)
not arithmetic, exactly the 2026-06-07 ADSR finding. An LFO just exercises it
every sample so it shows.

Fix (the escape hatch flagged when it was built), two engines, both exact:
  * SYMMETRIC exponential (rise == fall) is LTI → a single vectorised
    `scipy.signal.lfilter` over the whole block and all voices, per-row `zi`
    carrying the running value (`zi = a*cur` reproduces the scalar first
    sample, so bit-parity holds). Near-free at any voice count.
  * everything else (linear; asymmetric exp) → the same recurrence in
    **pure-Python scalars** on a `.tolist()`'d row instead of tiny-numpy ops.

Result (same benchmark): mono linear **19.8% → 0.5%** core (~36×); mono
symmetric exp 18.3% → 0.2%; **16-voice** symmetric exp 20% → 0.5% (lfilter
vectorises the voices). The one remaining heavy case is 16-voice *linear* at
7.4% (the genuine per-sample scan) — workable, dropped to exp it's 0.5%, and
it's the candidate for an analytic/JIT pass if it ever bites. New parity test
locks the lfilter path against the scalar reference (incl. `zi` priming +
block carry). All 16 slew tests pass, full suite **2270**.

## 2026-07-18 — `slew`: a CV slew limiter / lag / glide

**Matthew's dream:** "a CV that has time-based settings." Mapped the space
(his sources — LFO/ADSR/seq/clock/S&H — are covered; the *processor* side is
the gap) and he picked the **slew limiter / lag / glide** — the time-shaped
counterpart to the pointwise `cv_offset`/`cv_scale`. Feed a CV in, chase it
over time; independent rise and fall times.

**Design (agreed in-thread).** New `slew` module in CV & Utilities: `in`→`out`
(cv), params `shape` (`linear`/`exponential`), `rise_time`, `fall_time`
(seconds), voice-aware, primed-to-input. Both shapes via a toggle (his call):
  * `linear` — constant rate, REACHES the target; times read as seconds per
    1.0 unit (a true slew limiter, step `1/(t·sr)`/sample).
  * `exponential` — asymmetric one-pole ease, asymptotic; times read as
    ~time-to-99% via `_LN100 = ln(100)`, chosen so the two shapes *arrive* in
    the same wall-clock at equal settings — flip `shape` and the glide keeps
    its length, only the curve changes. A non-positive time = instant.

**Engine.** A per-sample recurrence (output chases input; the exponential pole
also depends on direction of travel), so one Python loop over frames with
numpy vectorised across the voice axis — the shape the envelopes had before
vectorisation. Only *symmetric* exponential could collapse to an `lfilter`;
independent rise/fall (the whole point) can't, so it's the loop. Cheap for a
CV utility; the ADSR's analytic run-splitting is the escape hatch if it ever
shows in a profile (per the 2026-06-07 lesson). Shape-polymorphic via
`collapse=False`: mono `(F,)` → one running value, voice `(V,F)` → one per
slot (no crosstalk), carried across blocks and primed to the first input
sample so the output starts *at* the signal, no startup swoop. Unpatched →
silence, state dropped.

**Killer app:** polyphonic portamento — `cv_keyboard` pitch → `slew` →
oscillator `freq_cv`, each voice gliding independently. Example
`examples/slew_portamento.json`.

**Verification.** `tests/test_slew.py` (15): linear reach-time + independent
slower fall + constant-rate + negative targets; exponential 99%-in-rise_time,
monotonic, no overshoot, curve-differs-from-linear; instant at time 0;
prime-to-input; unpatched silent; voice-aware per-voice + mono==single-voice
parity; block-size independence. Drove the renderer directly first
(linear reaches 1.0 at sample 49/50 @1 kHz, fall 4× slower at 199, exp 99% at
49). Full suite **2270 passed, 1 skipped**; example loads/renders; all edited
files byte-compile.

**Pending (Matthew's):** real-GUI eyeball — play it, does the glide feel
right, tune rise/fall by ear, A/B the two shapes. Filed for later: v2
`rise_cv`/`fall_cv` (modulate the times) + clock-sync; v3 a `moving`/EOC gate
out to make it a mini slope generator.

## 2026-07-18 — `warping_buffered_speaker_output`: the tape-warp sink

**Matthew's idea.** Copy the buffered sink, but instead of *hiding* the ring
governor's correction with the pitch-preserving WSOLA stretch, *expose* it as
audible varispeed — a tape brake. When the ring starves the deck slows and
the pitch dives ("running out of juice"); when the load lifts it spins back
up. The failure mode becomes the instrument: a buffer underrun stops being a
click and becomes wow-and-flutter. His steer: a new module, not an option on
the original ("it's already getting complicated"), and reuse the resampler's
tape brake for the feel.

**It's the existing governor minus the pitch-cancellation.** The whole point
of the buffered actuator is a WSOLA shift UP by `ratio` that *cancels* the
length resample's pitch move. Drop the WSOLA stage and keep the resample and
you get varispeed for free — *less* DSP, and no ~50 ms grain latency or
warm-up. The control law (`_governed_ratio`: fill→ratio, 0.5 setpoint, clamp,
smooth) is reused verbatim, and its sign already gives the right physics:
`ratio > 1` → longer push (refills the ring) *and* slower playback (pitch
down). Starve→slow, recover→spin up falls straight out. Stateless: per-block
resampling of contiguous device blocks is seam-continuous, so — unlike the
WSOLA path — it allocates no `_GrainShifter`.

**Sibling, not fork.** `WarpingBufferedSpeakerOutput` subclasses
`BufferedSpecificSpeakerOutput` (inherits every jack + param). Introduced a
`_BUFFERED_SPEAKERS` family frozenset so every place that means "a sink with
its own `buffer_size` + `fill` out + ring governor" checks the set — the
`fill` seed, the delayed-edge test (`_is_delayed_edge`), `_sink_block_size`,
and the governor-population gate. Only two things branch on *which* member it
is: the actuator (varispeed vs WSOLA, via an `is_warp` flag carried in the
`governed` tuple) and the ratio slew. The buffered path is byte-identical —
proven by the suite (its pitch-preserving test still passes) and a new
contrast test.

**Tape-transport feel.** Two params, `brake_time` (coast the pitch DOWN when
starving; ratio rising) and `spinup_time` (wind back UP; ratio falling). A new
`_brake_slew` replaces the one-pole *for the warp sink only* with a
constant-rate, asymmetric slew (the resampler brake's constant-torque
physics): each full-scale swing takes that many seconds. Defaults 0.5 / 0.25,
so recovery is a touch eager. `auto_govern` defaults **on** (warping is the
identity — drop it in, pick a 2nd device, it self-warps); it only engages once
routed to a named device, and is an ordinary stereo speaker on the master bus.

**UI / plumbing.** Joined the buffered family in six app.py sites (ring
readout, pan/width/gain handling, device picker + its two list helpers, the
`buffer_size` dropdown); `brake_time`/`spinup_time` render through the generic
param-widget path. Listed in the pyo stub set (silent stub, like its sibling).
New example `examples/warping_buffer_tape.json` (small `buffer_size` = more
warp).

**Verification.** New `tests/test_warping_buffered_sink.py`: family
membership, the governor still moves the push length (low ring longer, full
ring shorter), the defining **pitch-bend** test (a governed 1 kHz sine lands
at 800 Hz = F0/ratio, the varispeed the buffered sink was built to avoid —
with the buffered sibling asserted to still hold 1000 Hz), and the brake slew
(asymmetric, linear-not-exponential, instant at time 0). Full suite **2253
passed, 1 skipped**; `test_examples` green with the new patch; all four edited
files byte-compile.

**Pending (Matthew's to run):** the real-GUI + second-device eyeball — does it
actually *sound* like a tape losing juice? — and tuning `brake_time` /
`spinup_time` by ear. Possible later refinement: a continuous read-head
varispeed (vs per-block) if block-rate artifacts show at extreme ratios; the
per-block version is clean at moderate swings and the whole point is lo-fi.

## 2026-07-17 — Bitcrusher CV: `bits_cv` + `rate_cv`

Matthew asked to "add a cv to the bit crusher", picked **both** crush axes
(bits + sample-rate) from the options, and sketched the scaler ("1v/bit or
(1/24)v … cv_bits and cv_rate"). Added two CV inputs to the `bitcrusher`.

**Design reconciliation** (noted per the working agreement):
- **Naming** — normalised his casual `cv_bits`/`cv_rate` to the codebase's
  universal `<param>_cv` suffix: **`bits_cv`**, **`rate_cv`** (sit next to
  `cutoff_cv`, `drive_cv`, `fold_cv`, …). Flagged to him; trivially reversible.
- **Scaler** — used the house `<param>_cv_depth` param (adjustable) rather than
  a fixed scaler, with the **default set to his "1v/bit"**: `bits_cv_depth = 1.0`
  (1 bit per unit; ~23 for his "whole range over 1 V" alternative). `bits` is
  additive in its already-logarithmic unit (1 bit = 1 octave of quantiser
  levels); `rate_div` is multiplicative — `rate_div · 2^(rate_cv_depth · cv)`,
  octave-even like the Filter's `cutoff_cv` (`rate_cv_depth = 1.0` = 1 oct/unit).

**Backend** (`_render_bitcrusher`): reads the **block-mean** of each CV input
(one value/block, matching `cutoff_cv`) and folds it into `bits`/`rate_div`
before the neutral-skip test, so CV can wake an otherwise-transparent crusher
(base `bits=24` + a `-16` CV → 8 bits). Guarded `if cv is not None and
cv.size > 0`, so **unpatched inputs are a byte-for-byte no-op** — every existing
bit-exact / block-size-independence guarantee holds, and stays exact under a
*constant* CV (a time-varying CV tracks block boundaries, same accepted
trade-off the filter documents). `mix<=0` still returns before even reading CV.

**UI**: CV ports auto-render; added a small `bitcrusher` branch in
`_add_param_widget` so the depths read in their unit (`%.2f bit/unit`,
`%.2f oct/unit`) instead of a bare float. pyo backend unaffected (bitcrusher is
already in its unsupported list).

**Verification**: `test_bitcrusher.py::TestCV` (8) + updated model asserts —
bits/rate behavioural (constant CV → exact quantiser grid / hold pattern),
depth scaling, positive CV *disabling* the quantiser, unpatched no-op, and
block-size independence under constant CV (identical-input methodology). Full
suite **2238 passed, 1 skipped**. Also drove it end-to-end through a compiled
graph via `render_block`: a `constant` +4 into `rate_cv` gave exactly
`rate_div=16` (191 ≈ 3072/16 transitions) and the bitcrusher stayed
block-size-exact given identical input (the ~1e-14 whole-graph difference is the
live oscillator's phase accumulator, pre-existing, not the crusher).

Docs: `MODULES.md` bitcrusher section + `bitcrusher.py` module/class docstrings.
No GUI eyeball needed for the audio path (it's test- and render-verified); a
real-window look at the two depth knobs' labels is a nice-to-have, not queued.

## 2026-07-17 — FilePlayer seek / scrub bar

Matthew asked to "give the file player some love" with a seek bar for the
current track's position. Added a draggable 0..1 scrub bar under the transport
row that shows the playhead as a fill and lets you jump anywhere in the track.

**Backend** (`numpy_backend.py`): one new hook, `seek_file_player(module_id,
fraction)`. It mirrors `rewind_file_player` exactly — stores a frame index in
`state["seek"]` that the renderer consumes at the next block boundary (single
reference store, atomic under the GIL), so a scrub is block-aligned and lands
whether the player is playing or paused (the renderer already consumes `seek`
before the paused check, so pause-then-scrub-then-play works). The fraction is
taken along the *known* length — `total_frames` once decoded, else the buffered
`frames_ready` — the same quantity `snapshot_file_positions` reports as `total`,
so the bar and the `m:ss / m:ss` readout always agree. No-op for an unknown id,
a non-file_player, a failed decode, or a player with nothing decoded yet
(nowhere to seek). Rewind stays independent (seek-to-0 must work even before any
length is known, which the length-gated seek path would refuse).

**UI** (`ui/app.py`): a `dpg.add_slider_float(min=0, max=1, format="")` (raw
fraction hidden — the existing time readout carries the human-readable time).
The tricky part is one slider that is *both* driven by the playhead and dragged
by the user. Solved by polling `dpg.is_item_active` in the per-frame
`_update_file_positions`, no per-widget handler registry to track/free:

- **idle** → drive the thumb to `elapsed/total`;
- **active (dragging)** → leave the thumb for the mouse, flag the scrub;
- **released** (was flagged, now inactive) → commit `seek_file_player(frac)`.

A quick click can land+release between two frame polls and never read as
`is_item_active`; the slider's thin callback (`_on_file_seek`) sets the same
scrub flag, so the release branch still commits it. `set_value` doesn't fire DPG
callbacks, so the per-frame thumb updates never self-trigger a phantom seek.
Bookkeeping (`_file_seek_sliders`, `_file_seek_active`) is torn down on node
delete and cleared on patch load, alongside the existing file-player maps.

**Tests.** `test_file_player.py::TestSeek` (7) drives the backend hook through
the real renderer — seek→render→exact samples, pause-scrub-resume, [0,1] clamp,
seek-to-end parks+`finished`, buffered-length-while-streaming, no-op guards. A
new `test_file_player_seek_ui.py` (5) exercises the GUI glue headlessly (mocked
`dpg`, real backend, à la `test_file_player_queue`): idle reflects the playhead,
drag doesn't stomp or seek, release commits to the backend, quick-click commits
via the callback flag, teardown drops the bookkeeping. Full suite **2230 passed,
1 skipped**.

Docs: `MODULES.md` file_player Transport section + the `playing`-param note in
`fileplayer.py`. **Real-GUI eyeball PASSED 2026-07-17** (Matthew: "works
flawlessly") — drag/click seeks, the thumb follows the mouse mid-drag then
resumes tracking the playhead, and scrubbing while paused all confirmed in the
real window. Feature complete.

## 2026-07-16 — ring governor Slice 3: built-in auto_govern controller

The governor finale — self-regulation with no patch. New `auto_govern`
bool param (default off, so nothing changes for anyone who doesn't touch
it). `_governed_ratio` gains a middle branch: `ratio_cv` cabled still
wins (patch drives it); else `auto_govern` on → the sink runs the loop
itself off its own ring fill, target `1 + 0.5*(0.5 - fill)`; else
ungoverned + bit-identical. The 0.5 gain is not arbitrary — it *is* the
recommended `fill → cv_offset(-0.5) → cv_scale(-2) → ratio_cv` patch
(that chain yields `ratio_cv = 2*(0.5 - fill)` and the 0.25 `ratio_depth`
makes the target `1 + 0.5*(0.5 - fill)`), so auto and the canonical patch
are the same controller. Both paths already shared the clamp, the
smoothing, and the pitch-preserving actuator.

Tidy-ups folded in: a `_sink_fill()` helper now reads the ring's fill
(telemetry, one block old) and backs *both* the `fill` cv-out seeding and
the auto controller, so they can't drift apart; `stop()` clears the
governor's smoothing + stretch state so each Start begins at unity.

Subslices: ad57340 (param + model test), ca8a554 (engine + 5 tests:
low→stretch, full→shrink, exact-gain match, auto-off ignores the ring,
cable overrides auto — injecting a fake pre-filled `_DeviceOutput` to
probe the ratio response headless). Suite **2217 pass**. Governor feature
COMPLETE (Slices 1/2/3); remaining buffered-sink work is the separate
8192-fails-open silent-idle fallback (TODO).

**Manual-verify (meatthread0):** on a real 2nd device, just tick
`auto_govern` on a buffered sink — no cables — and the ring readout
should settle at ~50% on its own.

## 2026-07-16 — ring governor Slice 2: pitch-preserving actuator (197fd53)

The governed push no longer bends pitch. Composition trick: `_GrainShifter`
(the pitch shifter's streaming WSOLA engine) is a same-length pitch shift by
`r`, and the Slice-1 linear resample to `frames·ratio` is a varispeed by the
same `r` in the opposite direction — run the block through both and the
pitch effects cancel exactly, leaving a pitch-preserving time stretch. So
Slice 2 is: per governed sink, a persistent `[L, R]` engine pair (50 ms
grain / overlap 2 / the pitch shifter's head formula), shift by the smoothed
ratio, then the existing resample stage untouched.

Design call: while `ratio_cv` is cabled the engines stay in-circuit even at
ratio 1.0 — bypassing at unity would spend the ~one-grain warm-up (zeros) at
the exact moment the governor first corrects, mid-performance; instead it's
paid once at patch/Start and the governed path carries one constant ~50 ms
latency (fine for a cue/monitor feed). Uncabled sinks never construct an
engine, so the unpatched push stays bit-identical (asserted).

`tests/test_sink_governor.py` +1: 1 kHz sine → converged 1.25× stretch →
FFT of the pushed tail keeps its fundamental at 1 kHz (Slice-1 varispeed
would read 800 Hz). All Slice-1 length/clamp/smoothing pins unchanged.
Suite **2126 pass** (was 2125).

**Manual-verify (meatthread0):** same governor patch as Slice 1 — but now
crank CVScale hard: the ring readout should still walk to ~50% while the
audio holds pitch (listen for grain texture instead of detune).

## 2026-07-16 — buffered sink: patchable ring governor, Slice 1 (plain resample)

Two findings this session. First, Matthew's real-GUI screenshots settled the
8192 mystery: `buffer_size 8192` on the HD Audio device reads **`buffer:
idle`** — the secondary stream *fails to open* at that blocksize (open()
throws, `_sync_device_outputs` logs + skips, sink silent) while 1024 runs a
healthy `25% (2048/8192) under 2`. So the earlier "never fills past 2048" was
a normal 2-block backlog at 1024, and the real defect is the silent-idle trap
plus conflating device blocksize with ring cushion (fix queued in TODO).

Second, designed and shipped Slice 1 of the **ring governor** — Matthew's
idea: the sink feeds its ring fill back as CV so an upstream stretch holds
the buffer at half. That's adaptive resampling (async-SRC, textbook 50%
setpoint) with the control law patchable from cables. Three subslices, each
committed green:

* `08d0c3e` ports — `fill` (cv out) + `ratio_cv` (cv in) + `ratio_depth`
  param on `buffered_specific_speaker_output`.
* `264eab6` topo sort — Kahn's ignores cables leaving the delayed `fill`
  port (both passes), so a fill → controller → ratio_cv loop keeps its
  controller chain deterministically ordered instead of falling into the
  leftover-append tail.
* `f61a80b` engine — render_block_multi seeds `fill` from the previous
  block's ring telemetry (neutral 0.5 streamless); a cabled ratio_cv maps
  block-mean cv → `1 + cv·ratio_depth`, clamps 0.5..2, one-pole smooths
  (0.2/block ≈ 0.15 s) and linear-interp resamples the pushed block to
  `frames·ratio`. Unpatched ratio_cv is bit-identical to the old push; the
  ring already accepts variable push sizes.

The governor patch: `fill → CVOffset(−0.5) → CVScale(gain) → ratio_cv`.
Plain resample bends pitch on big corrections *by design* (you can hear the
loop work); the pitch-preserving OLA actuator is Slice 2.
`tests/test_sink_governor.py` +11 (adversarial-order topo, seed neutrality,
stretch/shrink/clamp/first-step-smoothing push lengths); suite **2125 pass**
(was 2114).

**Manual-verify (meatthread0):** patch the loop against a real second device;
watch `fill`'s CV meter + the ring readout hold ~50%, then crank the CVScale
gain to find where it starts to warble.

## 2026-07-13 — buffered sink love: sizes past 1024 + a live ring readout

Matthew asked for two upgrades to `buffered_specific_speaker_output`: buffer
sizes larger than 1024, and "some kind of text on it that indicates buffer
usage/availability". Both shipped.

**Sizes.** `ui/buffer.py` grew `SINK_BUFFER_SIZES` — the global stops plus
**2048 / 4096 / 8192** — with `snap_sink_buffer` / `coerce_sink_buffer_size`
(same tie-to-smaller law). The sink's combo and its coercing callback now use
the sink list; the global toolbar slider deliberately stays 64..1024 (the main
block sets keyboard-to-ear latency; this cue/monitor stream's doesn't). No
backend change needed: `_MAX_SINK_BLOCK` was already 8192, so the top UI stop
now sits exactly on the rail (a test pins that equality). A patch saved at
4096 round-trips instead of being crushed to 1024.

**Readout.** One line on the node, ticked every GUI frame:
`buffer 47% (3852/8192)  under 0  drop 2`. Plumbing follows the FilePlayer
playhead pattern end to end: `_DeviceOutput` grew `_underruns` / `_drops` /
`_primed` (all mutated inside the existing ring lock) + a `telemetry()`
4-tuple; `NumpyBackend.snapshot_sink_buffers()` maps module id → its stream's
telemetry (shared (device, size) streams sampled once, so co-sinks always
agree; absent = no live stream); `_update_sink_buffers` in the frame loop
writes the text via the dpg-free `format_sink_buffer` (unit-tested, ASCII-only
for the node font) with colors: grey idle, green healthy, **amber flash 1.5 s**
whenever a counter ticks. Bookkeeping mirrors `_file_pos_labels` at node
delete + patch load, plus the self-healing try/except from the CV meters.

**Semantics that took thought.** `under` = device callbacks the ring couldn't
fully serve — but only *after the ring has once filled to one device block*.
Prime-on-first-push (my first cut) still showed `under 1` + amber on every
clean Start at 8192, because a large sink block needs ~186 ms of pushes before
it can serve anything; the review caught it, the gate now arms on first fill.
`drop` = any push that lost audio: drop-oldest overwrite *or* a push bigger
than the whole ring (reachable: `buffer_size` 64 → 512-sample ring under a
1024 global block; docs now explain that both-climbing-from-the-start
signature). Reading guide lives in MODULES.md and the module docstring.

**Review.** Ran the 4-dimension + adversarial-verify workflow over the diff.
It got cut short by the session token cap: ring-math / gui-lifecycle /
consistency reviewers completed (2 + 0 + 1 findings), the concurrency reviewer
and all verifier votes died to the cap — so I verified the three findings
myself instead of re-spending: (1) the prime-gate Start tick above — real,
fixed; (2) a phantom amber flash when a live device/buffer_size edit lands a
sink on an already-open stream with historical counters — real, fixed by
resetting the flash baseline in both param callbacks (+ component-wise count
comparison); (3) the `drop` prose narrower than the code in four places —
real, fixed. Concurrency I re-checked by hand: counters ride the existing
lock, `telemetry()` holds it for four int reads at frame rate, the snapshot
reads `_device_outputs` as one atomically-swapped reference, and patch/param
access stays on the GUI thread. Noting the deviation per the working
agreement: the confirmed-findings gate was me, not the verifier fleet.

### Tests

`test_ui_buffer.py` +15 (sink list pins incl. rail equality, snap/coerce past
the ceiling, formatter incl. zero-capacity and ASCII guards);
`test_buffered_specific_speaker.py` +18 (extended sizes key and open at
2048/4096/8192 with 8× ring capacity; ring telemetry: fill tracking, priming
vs fill-up-gap vs genuine starvation, overflow + oversize drops, close()
reset; snapshot: keyed by id, idle cases, shared-stream equality,
plain-specific included). Suite **2114 pass** (was 2081).
`pysynthrack.ui.app` imports clean headless.

**Manual-verify (meatthread0):** eyeball in a real window — the dropdown's
2048/4096/8192 apply (stream reopens at the new size), the readout goes green
on Start against a second device, `under`/`drop` tick + amber-flash when you
force trouble (e.g. 64 under a 1024 global), and idle grey on Stop. Headless
can't open PortAudio streams, same caveat as the module's original ship.

## 2026-07-11 — `fm_op` (DX-style FM operator, Sources)

Matthew picked `fm_op` off the module-ideas backlog ("new synthesis territory,
small testable surface"). One phase-modulation operator — a sine oscillator
whose phase is driven by an audio-rate input — which is the whole of DX FM: two
patched together make a bell, three make an electric piano. Built from the
spec in `docs/MODULE_IDEAS.md`.

**DSP.** Per sample `core = sin(2π·phase + index·pm + feedback·core_prev)`,
`out = amp_cv · core`. Phase integrates the carrier frequency
(`261.6256·2**pitch_cv·ratio·2**(fine/1200)`, or a fixed `freq`) as an
exclusive prefix-sum with per-voice phase persisted in `self._state` — lifted
straight from `_ring_internal_carrier`, so a fresh module starts at phase 0 and
a swept `pitch_cv` stays continuous across blocks. Reused `_ring_match_voices`
for all the `(V,F)` input coercion.

**The radians scaling (the thing to get right).** `pm` is added *directly* into
the sine argument, which is in radians, so `index` is the peak phase deviation
in radians for a full-scale `pm`. That makes `β = index · peak(pm)` the classic
FM modulation index, and the analytic test is exact: a unit sine into `pm` at a
1:1 ratio produces sideband `k` at amplitude `|J_k(β)|` — measured against
`scipy.special.jn` over a leakage-free 1-second rectangular FFT (integer-Hz
carrier + modulator complete whole cycles → no windowing needed), matching to
float32 for β = 1, 2, 3.

**Dual engine (delay precedent).** `feedback = 0` has no sample-to-sample
dependency, so the whole block vectorizes (`np.sin(theta + pm_arg)`).
`feedback > 0` needs the sequential recurrence, so it drops to a per-sample
loop (V-vectorized, F-looped) with `core_prev` carried in `self._state["fb"]`.
The two paths are **bit-identical at feedback 0** (`0·prev` adds nothing, and
the last-sample state is written on both paths so turning feedback up next
block continues seamlessly) — verified against a verbatim per-sample oracle
(max err 0.0). Block-size independent to < 1e-6 (the ring_mod phase-wrap
contract; the cross-block cumsum reassociation is the only source of drift,
well under 1e-6, both engines).

**Reconciliation — `index_cv`.** The spec's Ports line listed `pitch_cv` / `pm`
/ `amp_cv` but the Params line listed `index_cv_depth`, with no matching input.
A depth param implies its CV input per the project conventions, and an *index
envelope* is the single most important gesture in FM (the index is the
brightness), so I added the `index_cv` input. Effective index =
`max(index + index_cv_depth·index_cv, 0)` (floored so a bipolar CV can null the
FM but not invert it). Flagged here as a deliberate deviation from the literal
port list.

**Ratio snapping.** `ratio` snaps to the nearest entry of a 20-value
harmonic-leaning table (`RATIO_TABLE` in the module, shared by the renderer's
`snap_ratio` and the UI). Snapping lives in the renderer so a hand-edited JSON
value still lands on a musical partial; the UI presents `ratio` as a **combo**
of the table (stored numeric via a small coercing callback `_on_fm_ratio_changed`,
same pattern as the buffered-sink `buffer_size` combo), so the panel offers
exactly the allowed set. `fixed` mode bypasses ratio/fine/pitch_cv entirely and
runs at the constant `freq`.

**UI.** An `fm_op` block in `_add_param_widget`: the ratio combo, `fine`
(±50 ct slider), `index` (0..10 rad slider), `index_cv_depth` (drag),
`feedback` (0..1 slider), `freq` (Hz drag); `fixed` falls through to the
generic checkbox. Without this block the auto-UI would have rendered these as
unbounded bare drag-floats — functional but not eyeball-ready. pyo gets the
one-line silent stub (added `"fm_op"` to the stub tuple).

**Levels / examples.** `amp_cv` unpatched → unity (the operator is a Source, so
it sounds with nothing patched); patched, it's the operator's level envelope.
`examples/fm_op_bell.json` (2-op: a 3.5:1 modulator with a fast brightness
envelope into a 1:1 carrier + slow body envelope) and `fm_op_epiano.json` (3-op:
a 14:1 tine modulator + 1:1 body modulator summed through a `combiner` into a
1:1 carrier) both load and render at 0.6 peak (audible, within headroom).

### Tests

`tests/test_fm_op.py` (29): model (defaults/ports/kinds/category, JSON
round-trip, unknown-param reject, `snap_ratio`/`RATIO_TABLE`); frequency
(unpatched C4·ratio, ratio scaling + snapping, ±50 ct fine, 1 V/oct `pitch_cv`,
fixed-mode ignores `pitch_cv`); Bessel `J_k(β)` sidebands for β∈{1,2,3} + index
scaling; feedback (fb=0 ≡ per-sample oracle bit-exact, feedback adds partials);
`index_cv` (raises the index, depth 0 disables, floored at 0); `amp_cv` (linear
scale, unpatched unity); invariants (single voice row ≡ mono, voices
independent, (V,F) preserved, block-size independent, extremes finite and
bounded, zero frames); and both examples load + render within headroom. Full
suite **2081 pass** (was 2052), `py_compile` on the five edited/new source files.

**Manual-verify (meatthread0 — can't drive headlessly):** eyeball the node in a
real GUI window — the `ratio` combo + the fine/index/feedback sliders + `fixed`
checkbox render and apply — and build a live 2-op bell / 3-op e-piano to hear it
sing (no headless path builds the node or its audio out). Same "SHIPPED, pending
real-GUI eyeball" pattern as the recent modules. Committed per the working
agreement; push is Matthew's.

**Eyeball PASSED 2026-07-14.** Matthew loaded `fm_op_bell.json` in the real GUI
and it works well — the 2-op bell sings (he clocked its 3.5:1 character as a
warning/alarm bell, which is exactly right for that inharmonic modulator). So
the node builds + renders in-window and the real audio path is confirmed. Not
separately A/B'd yet: the 3-op e-piano example and a close look at the `ratio`
combo specifically — minor, both exercise the same paths the bell just proved.

## 2026-07-11 — `buffered_specific_speaker_output` (per-sink output buffer size)

New sink: a copy of `specific_stereo_speaker_output` plus a `buffer_size` param
that sets the block size of *its own* secondary output stream, independent of
the global buffer. Use case: a flaky USB/Bluetooth monitor that needs a roomy
buffer while the main mix stays tight, or a low-latency cue off an otherwise
sluggish main buffer.

**Finding first:** the `SpecificStereoSpeakerOutput` docstring still claimed
"Slice 1: the picker, not yet the routing" — **stale**. The routing (secondary
`sd.OutputStream` per device, drop-oldest ring, live device reconcile) had
already landed as Slice 2 (the test file header confirms it). Fixed that
docstring while here.

**The one real gotcha — the ring.** `_DeviceOutput` was a *block* ring: the
device callback did `if block.shape[0] != frames: fill silence`, which only
works because the secondary stream shared the main stream's block size. A
*different* per-sink buffer would have made every popped block the wrong length
→ **permanent silence** (there was even a `test_frame_mismatch_is_silence`
pinning that). Reworked it into a **sample-counted ring**: a fixed `(capacity,
2)` numpy buffer with read cursor + fill count, guarded by a small
`threading.Lock` (held only for a ≤1-block memcpy — far shorter than the render
lock the main callback already holds; the lock-free deque discipline couldn't
survive sample-level drop-oldest with differing block sizes). Capacity =
`max_blocks * device_block`, so a secondary buffer larger than the main push
still fills. push drops oldest on overflow; callback zero-pads on underrun.
Push size and pop size are now fully decoupled. Did this as an isolated first
pass (green) before building anything on top.

**Keying decision.** A secondary stream is genuinely identified by
`(device, block_size)`, so I unified `_device_outputs` / `device_blocks` on that
tuple key rather than the old bare device-name string. The plain specific
speaker now keys by `(device, global_block)` — same grouping as before, just
with the size appended — so its behaviour is unchanged (two on one device still
share/sum). A buffered sink keys by `(device, buffer_size)`; it shares a plain
sink's stream only when the sizes coincide. New helpers `_stream_key` /
`_sink_block_size` (the latter clamps `buffer_size` to [16, 8192] and coerces
float/garbage) centralise it. `_wanted_devices`→`_wanted_streams`,
`_sync_device_outputs`, `set_param` (now reconciles on `buffer_size` too, so a
live change rebuilds just that stream), `_fill_output`, and the drain loop all
switched to the key. Chose unified-key over a parallel `_buffered_outputs` dict:
one reconcile path can't drift, and the ~15 existing-test edits were mechanical
(`"Cans"` → `("Cans", F)`).

**UI.** Added the type to the stereo-sink param block (pan/width/gain/cv_depth
sliders), the device-combo gate + Refresh, and a `buffer_size` dropdown of the
standard sizes (reusing `BUFFER_SIZES`/`coerce_buffer_size`) with a small
int-coercing callback so patches stay numeric. pyo gets a one-line silent stub
like the other stereo speakers.

### Tests

`tests/test_buffered_specific_speaker.py` (28): model (defaults incl.
`buffer_size=512`, ports, JSON round-trip of device+buffer, unknown-param
reject, sink-ness); `_stream_key`/`_sink_block_size` (own-buffer key, empty
device→master, plain sink keys by global block, clamp low/high, float+garbage
coercion); buffer-inert-on-master-bus equivalence; routing ((device,size) key,
two sizes one device split, buffered+plain share only when sizes match, device
bus == stereo drain, clip); secondary stream opens at the sink's buffer size +
live buffer-change rebuilds only that stream (stubbed open/close). Reworked the
`_DeviceOutput` ring tests in `test_specific_stereo_speaker.py` (the fifo/
underrun/overflow ones still pass; replaced frame-mismatch-is-silence with
smaller/larger/partial cross-size reads + a capacity-scales check) and migrated
its device-key assertions to tuples. Full suite **2052 pass / 1 skip** (was
2021), `py_compile` + import checks on all four edited source files.

**Manual-verify (meatthread0 — can't drive headlessly):** eyeball the node in a
real GUI window (device dropdown + Refresh + the `buffer_size` combo render and
apply), and confirm real audio out of a *second* physical device at a custom
buffer size. These match the project's existing "SHIPPED, pending real-GUI
eyeball" pattern (buffer slider, window persistence). Committed per the working
agreement; push is Matthew's.

## 2026-07-11 — README refresh (caught it up to reality)

The README still described "v0.1: oscillator + speaker only" with filters /
ADSR / LFO / MIDI listed under "coming next" — all long shipped. Rewrote the
Status + What-works sections to the current reality: **60 modules** across seven
categories (enumerated by category), the node-editor features (zoom, meters,
scroll-to-adjust, overlap-aware placement, layout persistence), MIDI, recording,
multi-device output, crash logging, and the ~1,900-test headless suite. Also:
corrected the backend framing (numpy is the reference/full backend every module
targets; pyo is an optional partial alternative — verified pyo_backend is 381 LOC
/ ~11 render branches vs numpy's 9,675), added the `[media]` extra, refreshed the
architecture tree (`_crash.py` / `error_handler.py`), dropped stale version refs
("arrives in v0.3", "handles everything in v0.1 and v0.2"), and removed the
obsolete `git init` walkthrough (the repo has been a live git repo for ages).
Screenshots, binary links, and the install/run/troubleshooting steps left intact
(still accurate). Docs-only; committed per the working agreement.

## 2026-07-11 — two crashes from the desktop-rig logs (audio race + stale meter bar)

Matthew sent six crash reports from the desktop build. They cluster into two
distinct bugs, both real and neither related to the node-placement work.

### Family A — GUI crash: "Item not found" in _update_cv_meters (3 logs)

`crash_..._gui.txt` ×3: `dpg.set_value` raised `Item not found: <id>` from
`_update_cv_meters`, iterating `_cv_meter_bars` and pushing levels into bar
drawlist items. The backend was `stopped` in all three (incidental — the meter
snapshot returns the last levels regardless).

**Root cause.** `_on_delete_selected` prunes eight bookkeeping maps on a node
delete but **not** `_cv_meter_bars` / `_audio_meter_bars` / `_meter_bounds`.
Deleting a CV-source node frees its bar items (children of the node) yet leaves
the `(module_id, port) -> bar` entry behind; the next per-frame
`_update_cv_meters` calls `set_value` on the freed id and the whole GUI loop
dies. (`_update_audio_meters` never showed up in the logs because
`_draw_meter_channel` already wraps its dpg calls in try/except — the CV loop
was the one bare site.)

**Fix (two layers).** (1) Root cause: `_on_delete_selected` now prunes the two
bar maps + the auto-range `_meter_bounds` for the deleted module id, beside the
existing pops. (2) Defence-in-depth: `_update_cv_meters` wraps its
`set_value`/`configure_item` in try/except like `_draw_meter_channel`, and
prunes any entry that raises — self-healing for any path that ever misses the
delete-time prune.

### Family B — audio crash: "dictionary changed size during iteration" (3 logs)

`crash_..._audio_callback.txt` ×3: `RuntimeError` inside `render_block_multi`,
always with `SpecificStereoSpeakerOutput` last and the second render loop's
locals present (`out`, `device_blocks`, `dev`, `channels`, `target`).

**Root cause.** `compile()` stores the App's *same* `Patch` object as
`self._patch`, and the GUI thread mutates `patch.modules` in place
(`add_module`/`remove_module`) **outside** the backend lock. The audio thread
grabs `patch` under the lock but then iterates `patch.modules.values()`
(second loop) with the lock released — a concurrent add/remove mid-iteration
raises. The first loop was immune (it walks a list copy of the topo order);
only the raw `.values()` iteration was exposed. Cables aren't affected —
they're list-based, and only dict/set iteration raises this particular error.

**Fix.** Snapshot the module map atomically under the lock alongside `order` /
`cv_ports` (`modules = dict(patch.modules)`), and iterate the snapshot in both
loops. `dict(...)` is a single GIL-atomic step so it's safe even though the
writer doesn't take the lock; a module added/removed mid-block is simply seen
next block. Per-block cost is a shallow copy of a small dict — negligible.
Residual by design: individual param/cable reads on the audio thread stay
lock-free (scalar/list reads, no size-change RuntimeError class).

### Tests

`tests/test_render_modules_snapshot.py` (3): a deterministic mid-second-loop
delete (wraps `_SPEAKER_CHANNELS.get` to pop a module during iteration) —
**verified it raises the exact RuntimeError with the pre-fix live-dict loop and
passes after**; a snapshot-not-live-dict proof (drop a module the instant
render starts, block still completes); a threaded add/remove-vs-render stress
loop. `tests/test_meter_bar_cleanup.py` (3, dpg mocked): delete prunes all
three meter maps for the victim and leaves a sibling's; `_update_cv_meters`
survives a bar whose `set_value` raises and prunes it; empty-map no-op. Full
suite **2021 pass / 1 skip** (was 2015). Committed per the working agreement;
push is Matthew's. The live GUI-crash and audio-race paths remain
headless-untestable end-to-end, but both are now covered at the unit level and
the demonstrated failure modes are pinned.

## 2026-07-11 — node placement: stop new nodes landing on a lower slider

Matthew's one remaining bug: when a newly-added module lands on top of an
existing one, clicking the newcomer's **title bar** to drag it sometimes
adjusts a **slider on the node underneath** instead of moving the node.

**Root cause (two layers).** The *click-through* itself is an imnodes
limitation and isn't fixable from DearPyGui: imnodes only starts a title-bar
drag when ImGui reports no widget hovered at the click point (it yields the
mouse to widgets on purpose). A title bar is not an ImGui widget — imnodes
draws and hit-tests it itself — so when nodes overlap, the lower node's slider
occupies the same pixels as the upper node's title bar, ImGui calls that slider
hovered, and imnodes yields the drag to it. No knob makes a bare title bar win
over an overlapping widget; raising z-order doesn't help (the strip still has
no widget to claim the hover). The *trigger*, though, is fully ours:
auto-placement cascaded each new node only ~60px down (`_next_node_pos` stepped
`+60` on y), far less than a node's 150–300px height, so every newcomer landed
almost entirely on its predecessor by design. Kill the overlap and the imnodes
limit never fires in the normal add flow.

**Fix.** New dpg-free `ui/node_layout.py`: `find_free_position(existing,
preferred, …)` tries the caller's preferred (staggered) spot first — so a
genuinely clear cascade position is honoured unchanged — and on a collision
scans a grid for the first slot that clears every existing node by a margin,
falling back to `preferred` only if the canvas is so full nothing clears
(overlap then unavoidable). Rects with a non-positive size (a sibling that
hasn't rendered → 0×0) are ignored. `rects_overlap(a, b, margin)` is the shared
AABB test (margin = required empty gap; zero-gap touch is not an overlap).

**Glue (app.py).** `_create_node_for_module` now, on the interactive-add path
only (`pos is None`), builds the preferred spot as before then routes it through
`find_free_position(self._existing_node_rects(), preferred)`. New
`_existing_node_rects` reads each node's `get_item_pos`/`get_item_rect_size` and
divides both by the zoom factor (dpg reports scaled pixels; the helper works in
logical coords), defensively dropping any node dpg can't report. Load-from-patch
is untouched — it passes explicit positions, so saved layouts restore verbatim.

**Caveat (documented, unreachable from our side).** If the user *manually*
drags two nodes to overlap, the click-through can still happen — that's the
imnodes limit, not ours. But the "new node dropped on a slider" path — the one
actually hit — is now gone.

**Tests.** New `tests/test_node_layout.py` (14): overlap adjacency/margin
edges; preferred honoured when clear; zero-size rects ignored; collision moved
to a clear slot; result clears every node; first free slot tucks beside a
single node on the same row; a wider newcomer needs a bigger gap; a fully
tiled canvas falls back to preferred; float return. Full suite **2015 pass /
1 skip** (was 2001). The dpg glue (`_existing_node_rects`, real rect reads) is
headless-untestable and joins the real-window eyeball pile. Committed per the
working agreement; push is Matthew's.

## 2026-07-11 — KeyTrigger: bind one key → gate / trigger / latch

Matthew's idea, arrived at through a design chat: instead of one fat keyboard
node, a swarm of tiny single-purpose "this key does this one thing" nodes —
"drop in a single key at a time for a super complex setup." Built as a new
`key_trigger` source. He picked the name, chose to expose the output shape as
a **choice** ("offer selection of gate/trigger/latch… flexibility is king"),
and — for the shortcut-collision question — "shortcuts always win"; the key
set was left to my discretion.

**The one architectural finding (drove the whole slice split).** The existing
keyboards (`keyboard`/`cv_keyboard`/`cv_gates`) receive keys *as MIDI notes*
via `_KEY_TO_SEMITONE` (a home-row→note map); `_on_key_press` drops anything
not in that map. So non-note keys (number row, punctuation…) never reach a
module today. Binding *arbitrary* keys therefore needs a **parallel raw-key
dispatch path**, not a tweak to the note path. That's the only non-trivial
part; I sliced the work so the risky GUI bit was isolated.

**Slice 1 — module + DSP (fully headless).** `modules/key_trigger.py`:
Sources, `ACCEPTS_RAW_KEYS = True` (a new routing flag, sibling to
`ACCEPTS_COMPUTER_KEYS`), params `key=""` (unbound) + `mode`, one `out`
(gate). Thread-safe held/press-edge tracking like `cv_gates`
(`raw_key_down/up(name)` self-filter by the bound key; `snapshot()` returns
`(held, presses)` and consumes the edge). `_render_key_trigger`: **gate** =
held; **latch** = press-parity toggle held in backend state, surviving
key-up; **trigger** = a fixed ~5 ms pulse carried across blocks via a
`pulse` sample counter, so it's block-size independent (proved: 240 highs @
48k whether one 512 block or eight 64s). The renderer never reads `key` — the
module self-filters — so binding is a pure model-param write. 15 tests.

**Slice 2 — raw-key routing + Learn (the eyeball part).** A second global
key-handler pair (`_on_raw_key_press/_release`) with its **own** debounce set
(`_raw_key_down`, kept separate so it can't tangle with the note/zoom
`_held_keys`), plus `_KEY_CODE_TO_NAME`/`_KEY_NAME_TO_CODE` built at runtime
off `mvKey_*` constants (like `_init_key_map`) over A–Z, 0–9, common
punctuation, space. Reserved keys are simply absent from the map → unbindable
(Delete/Backspace keep deleting), and performance dispatch defers on
`_ctrl_down()/_alt_down()` (new) or `_text_field_focused()` (checks the two
`input_text` sites, now tagged + registered) — so "shortcuts always win" and
typing a path doesn't fire bound letters. Release is unguarded (no stuck
gates). Learn: a per-node button arms `_key_learn_target`; the next bindable
press binds it (`_bind_learned_key`), clicking again cancels, clicking
another node hands over. `mode` slots into the shared mode-combo;
`key_trigger` appears in the Add▸Sources menu for free (auto from CATEGORY).
7 GUI-glue tests (dpg mocked) cover dispatch fan-out/self-filter, independence
of two nodes, Learn bind/cancel/handover, and panic-release.

**Slice 3 — docs + example.** MODULES.md index row + a full catalogue entry;
the module docstring; `examples/key_trigger_latch_brake.json` (a latch key →
resampler `brake`: tap to tape-stop, tap to spin up — closing the loop on the
brake chat that prompted this) — verified to load and render 50 blocks.

Full suite **2001 pass / 1 skip** (+22). Committed per the working agreement;
push is Matthew's. **Pending real-window eyeball** (all dpg-only, unbuildable
headless): the Learn button + bound-key label, the code→name map resolving
real `mvKey_*` codes, and the modifier/focus guards actually gating. Known
follow-ups left open: single-key reorder/numpad support; an optional built-in
envelope like `cv_gates`; Esc-to-cancel Learn.

## 2026-07-11 — FilePlayer queue: auto-skip bad files, Remove, Next button

The three open FilePlayer-queue follow-ups, shipped together (Matthew picked
the queue follow-ups and, seeing the queue now exists, added a next-track
button to the ask).

**Auto-skip a bad/missing queued file (the interesting one).** The queue
advanced off `file_player_finished`, but a track that *fails* to decode
finishes as `done`+`failed`, never `finished` — so the list stalled on the
dud. The naïve fix (add a `failed` bool, advance on `finished OR failed`)
has a **race**: after we set `path` to the bad file, the audio thread
rebuilds its decoder on the next render and the decode of a missing file
fails almost instantly — often *between* two ~60 fps UI polls. A bool edge
(`ended and not was_ended`) needs to observe the decoder in a not-ended
state once to re-arm; if the failure lands inside the poll gap, the edge is
never seen and the queue stalls anyway — intermittently, the worst kind.

Fix: give the advancer a stable **decode identity** instead of a bool edge.
New `NumpyBackend.file_player_decode_gen(mid)` returns a counter bumped once
each time the renderer actually (re)starts a decode — at the existing render
rebuild site (`if decoder is None or path changed`), guarded on a non-None
decoder so an empty-path idle player never ticks, and *not* on the hot
steady-state path (the rebuild `if` is skipped while a track just plays, so
zero per-block cost). The GUI keeps `_fileplayer_advanced_gen[mid]` = the
generation it last advanced at and fires when `now_ended and gen !=
last_gen`. Because the bad file's decoder is a *new* generation regardless of
*when* it fails, the skip fires exactly once no matter where the failure
lands relative to the polls — race gone. This also subsumes the old
edge-trigger's no-double-eat guarantee (same generation across polls with no
render between ⇒ no re-advance), so `test_advance_is_edge_triggered` still
holds. Also added `file_player_failed(mid)` (`done and failed`) so the
advancer can tell a bad track's end from a still-decoding one and tag the
status line ("Skipped unreadable X → Y").

**Remove a single queued item.** A **Remove** button beside Clear drops the
selected **Up next** row. dpg listboxes hand back the selected row *string*,
not an index, and we display basenames — which can collide. So the rows are
now numbered (`_playlist_display_items` → `"1. name"`, renumbered on every
refresh as the queue drains), making each unique; Remove matches the selected
string against the freshly regenerated rows to get an unambiguous index. No
selection / a stale one is a gentle no-op.

**Next-track button.** A **>>|** transport button (beside `|<`/Play/Stop)
force-advances to the next queued track by hand, reusing `_advance_playlist`
via a new `"next"` action in `_on_file_transport`. Empty queue → a status
message, no disruption (matches the "stop when empty" auto-advance stance).
Works mid-track (doesn't wait for the current one to finish).

Renamed `_fileplayer_prev_ended` → `_fileplayer_advanced_gen` (init, delete-
prune, load-reset). Tests: +5 `TestFailedHook` in `test_file_player.py`; +4
in `test_file_player_queue.py` (skip-bad, next-advances, next-empty-no-op,
remove-selected+stale). The skip test is deterministic — it renders once to
kick (and generation-bump) the bad decode, `wait_for_file_decodes` to force
the failure, then advances — so it never races the worker thread. Full suite
**1979 pass / 1 skip** (was 1970). Docs: MODULES.md (file-list + transport
paragraphs), the `playlist` docstring, TODO (all three ticked). Committed per
the working agreement; push is Matthew's. **Pending:** the usual real-window
eyeball — the Remove/>>| buttons and numbered listbox are dpg-only, no
headless path builds the node.

## 2026-07-11 — resampler: tape-stop / spin-up brake (the last open idea ships)

Matthew asked whether the resampler could use more love or was at its peak.
The 07-10 arc (cubic → anti-alias → spread) had closed everything but one
TODO line: the **tape-stop / spin gesture**. Shipped it.

**Why a feature, not a glide preset:** glide ramps in semitone space, and a
dead stop is −∞ semitones — unreachable. The brake works in **ratio space**:
a per-sample brake position ramps 1→0 over `brake_time` (0→1 over
`spinup_time` on release), **linear in speed** — constant-torque physics,
how a real platter/capstan winds down — and multiplies the playback ratio,
all the way to an actual zero. Pitch dives through the floor, the read head
freezes (output holds a constant → silence through any AC path), release
whooshes back up to the set pitch.

**Shape.** New gate input `brake` (kind "gate": clock/sequencer/keyboard
gates patch straight in) ORed with a `brake` param switch; `brake_time`
0.5 s / `spinup_time` 0.25 s defaults, 0 = instant. Module-wide gesture —
a (V,F) gate collapses via max (any voice high engages), one transport
shared by all voices and spread channels. Sits after glide/pitch, before
the AA cutoff tracking (a braked read is slower → AA correctly relaxes).
While frozen the write head keeps lapping the ring; the ordinary low-edge
seam jump re-centres the head under its equal-power crossfade (constant-to-
constant, inaudible) — zero new declick machinery. Implementation is a
small `_brake_ramp` helper (segment-wise clipped linear ramp, vectorized
per gate-run) + a ratio multiply; with the brake released and recovered
the multiply is **skipped entirely**, so brake-free renders stay
bit-for-bit what they always were (asserted).

**Tests.** +11 (`TestBrake`): defaults/round-trip; gate-kind port walls;
released = bit-exact no-op; deceleration reaches a movement-free dead stop;
pitch dives mid-ramp; release recovers the set pitch; constant-high gate ≡
param switch bit-exactly; brake_time 0 stops within a sample; a 5 s held
stop across many ring laps stays finite/click-free; voice row ≡ mono
through a full gesture; spread channels brake together. Suite: 83
(resampler) / **1970 pass, 1 skip** full. Docs: module + core docstrings,
MODULES.md (ports/params tables, a brake paragraph, patching + example
lists). UI: `brake` checkbox + two time drag-floats.
`examples/resampler_tape_stop.json` (15 BPM clock gating the brake → a
stop/spin-up every 4 s) verified end-to-end headlessly: 600 blocks render
finite, ~1/5 of them near-silent (the stops), full level between.

That empties the resampler idea list — cubic → AA → spread → brake. The
real-window eyeball (checkbox/drags layout) is meatthread0's, as usual.
Committed per the working agreement; push is Matthew's.

## 2026-07-10 — scroll-to-adjust: Ctrl = fine (÷10), Shift = coarse (×10)

Matthew wanted a fine-adjust modifier for scrolling a param (fine-tuning a
`constant` CV and the resampler). Added **Ctrl = ÷10 (fine)** alongside the
existing **Shift = ×10 (coarse)**; a bare notch is unchanged (displayed
precision). Ctrl+wheel is also the zoom gesture, so the two are resolved by
**hover priority: over a param widget Ctrl+wheel fine-adjusts that knob; over
empty canvas it still zooms** (`_on_zoom_wheel` now yields when a param is
hovered).

The step math generalized from a `coarse` bool to a `mult` factor (1 / 10 /
0.1). Fine can go below the displayed precision (e.g. cents "%.0f ct" → 0.1
ct/notch), so the result is now rounded to whichever is finer of the display
precision and the step (`_step_decimals`) — otherwise the fine nudge would be
rounded straight back. Ints step by `round(mult)` floored at 1 (fine can't
subdivide an int). Coarse is unchanged (still rounds to the display, keeping
any fine offset). Handler side: `_on_param_wheel` maps the held modifiers to
the mult; `_nudge_param_widget` passes it through.

All numeric decisions stay in the dpg-free `ui/param_scroll.py` — 36 unit
tests including the sub-display fine case and the coarse-keeps-offset case.
Full suite **1926 passed, 1 skipped**. Real-window feel eyeball-pending — in
particular confirm Ctrl+scroll over a knob fine-tunes (not zooms) and Ctrl+
scroll over empty canvas still zooms.

---

## 2026-07-10 — scroll-to-adjust: step by displayed precision, not blunt 1%

Follow-up to the scroll-to-adjust feature: Matthew found some widgets jumped
0.1 per notch where 0.01 was wanted. The original rule (1% of range) gives
0.01 on a 0..1 "%.2f" mix but 0.1 on a "%.2f" 0.05..10 LFO rate — same shown
precision, 10× the step, because the range is 10× wider.

New rule (`scroll_step`): a notch is the largest power-of-ten multiple of the
widget's *displayed* precision (from its printf `format`) that stays within
~1% of range, and never finer than one shown digit. So a notch bumps the last
digit you can see — 0.01 on both the mix and the LFO rate, 0.1 st on a "%.2f"
±24 semitone (not 0.48 or 0.01), 100 Hz on a "%.0f" 20..20000 cutoff (not 1
Hz), 1 ct on "%.0f" cents. Wide low-precision params stay usably coarse;
narrow high-precision ones get the fine step they advertise. The result is
rounded to the displayed precision so the value matches the readout (an
off-grid drag snaps clean on the first notch). Shift is still ×10; ints and
combos unchanged.

Implementation: `decimals_from_format` parses the precision from the format
string (`get_item_configuration` exposes `format` — verified), `scroll_step`
picks the nice size, `nudge_number` gained a `decimals=` path (unbounded /
formatless widgets fall back to 1% of range). All in the dpg-free
`ui/param_scroll.py` — 37 unit tests pin the step for every real param family.
Full suite **1927 passed, 1 skipped**. Real-window feel still eyeball-pending
(the gesture itself isn't headless-testable).

---

## 2026-07-10 — UI fix: Ctrl+zoom keys debounced (one step per press)

The Ctrl+= / Ctrl+- / Ctrl+0 zoom shortcuts are bound with
`add_key_press_handler`, which re-fires at the OS key-repeat rate while a key
is held — so holding one cycled through zoom levels instead of stepping once
per press (Matthew caught it). The computer-keyboard note handler already
debounces auto-repeat via a `_held_keys` set; the zoom keys weren't gated.

Fix: a shared `_debounce_key(code)` — True on the first press (records the
code in `_held_keys`), False on the repeats (already recorded). The global
key-release handler clears the code on physical release, re-arming the next
press. The three zoom handlers now guard on
`self._ctrl_down() and self._debounce_key(app_data)`. Reuses the note
handler's set: zoom keys and note keys are disjoint codes so they never
interfere, and `_all_keyboards_notes_off` still clears everything. No change
to Ctrl+wheel zoom or the mouse-wheel param scroll.

Tests: `tests/test_zoom_key_debounce.py` pins the contract (first press fires,
repeats suppressed, release re-arms, distinct keys independent) via the
unbound method against a stub — no dpg context needed. Full suite **1909
passed, 1 skipped**. The key-repeat behaviour itself is eyeball-only in the
real window (dpg key events aren't headless-drivable): confirm Ctrl+= steps
once per tap and holding no longer runs away.

---

## 2026-07-10 — UI: scroll-to-adjust param widgets (mouse wheel over a knob)

Hovering a param widget and rolling the mouse wheel now nudges its value —
the standard "scroll a knob" gesture. A bare wheel over a slider/drag steps
it by 1% of its range (Shift = 10% coarse); an int slider (e.g. `overlap`)
by ±1 (Shift ±10); a combo (e.g. the compressor `detector` peak/rms) cycles
options; a checkbox (e.g. `formant_preserve`) flips on/off. The change
routes through the normal `_on_param_changed`, so the backend updates exactly
as a drag would.

Wiring reused the zoom infrastructure: a second
`add_mouse_wheel_handler(_on_param_wheel)` beside the Ctrl+wheel zoom one —
they never clash because zoom bails without Ctrl and the nudge bails *with*
Ctrl. Every param widget is registered (dpg id → `(module_id, param_name)`)
at its single build site; `_on_param_wheel` finds the hovered one via
`is_item_hovered` (pruning stale ids from deleted nodes) and dispatches on
the dpg item type. The value math is a dpg-free `ui/param_scroll.py`
(`nudge_number` / `cycle_index`) — the zoom.py / buffer.py split — with 19
unit tests. The item type-strings + config keys the dispatch relies on were
verified against real dearpygui (`mvAppItemType::mvSliderFloat`, `min_value`
/`max_value` on sliders & drags, `items` on combos). Full suite **1906
passed, 1 skipped**.

Pending — real-window eyeball (meatthread0): the hover/wheel gesture and the
step *feel* can only be judged live (the dpg glue isn't headless-testable,
like zoom / buffer / window-geometry). Worth checking that a bare wheel over
a slider doesn't also scroll an enclosing panel — in the node editor bare
wheel currently does nothing, so it should be clean.

---

## 2026-07-10 (follow-up) — pitch_shifter: honor the documented overlap range

Tiny consistency fix surfaced in the review: the engine clamped `overlap`
to 1..8 while the UI slider and docstring both say 2..4. The out-of-range
values were only reachable via hand-edited JSON (the GUI caps at 2..4), and
`overlap = 1` is degenerate (no grain overlap → amplitude dips). Tightened
the clamp in `_pitch_shifter_core` to `max(2, min(4, ...))` so a stray JSON
value snaps into range instead. New deterministic test
`test_overlap_clamped_to_2_4` (overlap 1 ≡ 2 and 8 ≡ 4, bit-identical).
Suite **1887 passed, 1 skipped**. No UI/docstring change needed — they were
already right; the engine now matches them.

---

## 2026-07-10 — pitch_shifter: phase-coherent dry/wet mix (exact latency comp)

The pitch_shifter's dry/wet `mix` used an **approximate** latency
compensation for the dry tap (`Dc = eng.Lg` — a code comment said as much).
Measured the WSOLA engine's true input→output latency directly (feed noise
at unison, cross-correlate wet vs input): at the 50 ms / overlap-2 default
it's **4608 samples**, not `Lg = 2205` — the dry landed ~2400 samples
(~54 ms) off the wet, so any partial mix near unison comb-filtered and a
few-cents detune "thickener" hollowed out instead of thickening.

Fix: the exact latency is `iw − rp/r` — total consumed input minus the
stretched read pointer mapped back through the r× time-stretch. Verified to
the sample against the cross-correlation (corr 1.000 across grain/overlap
settings, rock-stable block-to-block at every ratio). Added
`_GrainShifter.latency(r)` (returns `iw − rp/r`, or `Lg` before priming);
`_pitch_shifter_core` now sets `Dc = eng.latency(r)`. `dry_tap` gained a
ring-history clamp so a small grain / large block can't read wrapped
samples. No change to the wet path, `mix=1`, or the pitch — only the dry
tap's delay, so a partial `mix` now blends two time-aligned signals (at
unison, dry ≡ wet).

Trade-off worth noting: `mix=0` (pure dry) is now delayed by the full wet
latency too (was ~1 grain), so the timing no longer jumps as you sweep
`mix` — a uniform module latency across the whole knob, which is the point.

Tests: new `test_mix_is_phase_coherent_at_unison` (render the same noise at
mix 1/0/0.5, shift 0; wet↔dry corr > 0.98, 50% blend keeps ≥ 90% RMS).
Confirmed it fails on the old `Dc = Lg` (corr −0.007) and passes on the fix.
Full suite **1886 passed, 1 skipped**. Docs: module docstring + a dated
"Phase-coherent mix" note in MODULES.md; dropped the "approximate" comment.
The empirical latency probe lived in a throwaway sandbox script (not
committed). No UI/pyo/example change — pure DSP + docs + one test.

---

## 2026-07-10 — FilePlayer: a file list / queue that auto-advances

Matthew asked for a "file list" component that feeds the FilePlayer — when
the current track finishes, load the next from the list and remove it — with
an Add button, reusing the Browse dialog if easy.

**Design fork, asked up front.** Cables here only carry audio/CV between
ports, so a *path* can't be a signal a separate node cables in. The two
shapes that actually fit: (A) extend FilePlayer with a queue, or (C) a
standalone `file_list` source node that plays through a queue (like
`fader_seq` reuses the sequencer engine). Asked; Matthew picked **A — extend
FilePlayer**, and **stop/silence when the list empties** (over loop-the-list
or hold-last). So no new node type; the queue lives on the player.

**Model.** New `playlist` param on FilePlayer — an ordered `list[str]` of
paths, default `[]`. That default is *mutable*, and `Module.__init__` only
deep-copied dict-valued defaults (velocity_curve), so every player would have
shared one class-level list. Extended the per-instance copy to also `list(...)`
list defaults — a general correctness fix (no module had a list default
before, so nothing else changes). `playlist` round-trips for free via
`to_dict`/`from_dict`.

**Finish signal.** Auto-advance needs a clean "this one-shot just ended"
edge. `snapshot_file_positions` gives `(elapsed, total)` but not *done*, and
elapsed transiently equals total on a mid-file underrun — a false end. Added
`NumpyBackend.file_player_finished(mid)`: True iff the decoder is `done`, not
`failed`, `total_frames > 0`, and `pos >= total_frames`. That's exactly "a
non-looping, armed track ran off the end" thanks to renderer invariants — a
loop wraps `pos` modulo the length (never ≥ total) and disarming resets `pos`
to 0 — so no need to re-read `loop`/`armed` here. Lock-free (atomic int/bool
reads under the GIL; a block-late answer just delays the queue poke one
block). pyo backend has no file playback, so it simply lacks the hook.

**GUI glue.** The advance is UI-driven, matching how the app already polls
the backend each frame (meters, DSP load, playhead). New
`_advance_file_playlists` runs once per frame:
  * **edge-triggered** — it stores each player's last `finished` and only
    acts on the False→True transition. Critical: after we set `path` to the
    next track, the audio thread takes ~a block to rebuild state, so
    `finished` stays True for a frame or two; a level trigger would eat the
    whole queue in three frames. (`test_advance_is_edge_triggered_not_per_frame`
    pins this by ticking three times with no render between — exactly one hop.)
  * **advance** — pop the head of `playlist` into `path` via `backend.set_param`
    (same mutation Browse/typing use; the renderer re-decodes and restarts at
    0:00 because the path changed), repaint the field + listbox, status line.
  * **empty queue** — do nothing; the one-shot stays parked at its end
    (silence), which *is* the "stop when empty" Matthew chose.
  * **kick-start** — a *running* player sitting on an empty `path` with a
    non-empty queue loads its first track, so a fresh file list plays without
    a manual Browse first. Gated on `is_running` so a queue built while
    stopped isn't consumed before Start.

**Node UI.** Under the transport row: an **Up next** listbox (basenames),
**Add to list...**, and **Clear**. Add reuses the one shared `wav_dialog` —
`_show_wav_dialog` now takes a mode (`(mid, "playlist")` tuple vs bare `mid`),
and `_on_wav_selected` either sets `path` or appends to the queue. `playlist`
is skipped in the generic param-widget loop (it'd render a broken control) and
drawn as this dedicated panel instead. Per-module bookkeeping
(`_playlist_listboxes`, `_fileplayer_prev_finished`) is cleared on load and,
newly, pruned on single-node delete (the existing code left `_file_pos_labels`
to the recompile-prunes-the-snapshot trick; I pruned all three there).

**Tests.** +2 in `test_file_player` (playlist in default params; fresh-list-
per-instance; to/from_dict round-trip), +5 `TestFinishedHook` (not finished at
0:00 / mid-file; True once run off the end; loop never finishes; not-while-
decoding; missing-path and unknown-id both False). New
`tests/test_file_player_queue.py` drives the **GUI glue headless** — builds a
real `App` (dpg mocked out, numpy backend forced in) against real WAVs and
renders blocks to consume tracks: advance-and-remove, empty-queue-stops,
edge-not-per-frame, and kickstart-only-when-running. `pytest.importorskip`
guards the one GUI-touching module so a headless CI without dpg skips it
cleanly. Full suite **1959 pass / 1 skip**.

**Verified** end-to-end headlessly (the App advance loop actually drains a
2–3 track queue against a live decode). **Not** verified: the real window —
listbox/buttons layout and the Add picker are DPG-only, no headless path
builds the node. That eyeball is meatthread0's, same as the other recent
shipped-pending-eyeball features. Commit is Matthew's to run.

Known edges left as TODO follow-ups: a queued file that fails to decode
stalls the queue (loads silence, never "finishes", so no advance) — acceptable
as stop-ish for v1, could auto-skip; and only whole-list Clear exists, no
single-item Remove/reorder yet.

## 2026-07-10 — resampler: stereo detune spread (the "love" arc completes)

Third of the resampler follow-ups (after cubic + anti-alias). New `spread`
param (cents, default 0). Above 0 the module grows a detuned stereo pair
alongside the centre `out`: `out_l` reads `spread`/2 cents flat, `out_r`
the same sharp, **each off its own read head** — so they drift and
loop-seam independently and decorrelate (measured L/R correlation ≈ 0,
i.e. wide) into a chorus-like stereo image from a mono source. `out` stays
the centre pitch; at spread 0 it's a single centre read, unchanged.

**The refactor.** Producing three simultaneous varispeed reads meant the
one inline read head had to become N. Extracted the read-positions +
fast/slow + declick into `_resampler_read_channel(state, ch, ...)` and
`_ensure_resampler_channel`, with the seam state keyed by a channel suffix
(`""` centre, `"_l"`/`"_r"`); `_resampler_voice_declick` took a `ch` arg so
its `xf_*`/`seam_jumps` writes hit the right channel. The centre channel
keeps the exact old keys and code path, so it's **bit-identical** — all 63
prior tests passed untouched before a single stereo test was written (the
bar I held the refactor to). Chosen design points:
  * **`out` orthogonal to spread** — it's always the clean centre pitch;
    spread only *adds* the L/R pair. `test_out_unaffected_by_spread` pins
    `out` bit-equal across spread 0 vs 25 (AA off).
  * **Always emits the stereo pair** — `_render_resampler` returns the
    `{out,out_l,out_r}` dict unconditionally (chorus/reverb convention);
    at spread 0 the pair *mirrors* `out` (same array, no extra read). The
    first cut returned a bare `out` array at spread 0 to dodge test churn,
    but that left a connected `out_l`/`out_r` **silent** until spread was
    raised (the demo caught it) — a footgun and a mismatch with the docs'
    "equals out at spread 0". Fixed to always-emit; the price was pointing
    the `_run` test helper + ~13 direct call-sites at `["out"]`.
  * **L/R start aligned with the centre head** (`_ensure_resampler_channel`
    seeds their delay from `state["delay"]`), so engaging spread mid-stream
    doesn't jump; they're dropped when spread drops to 0 or the window
    rebuilds, re-seeding aligned next time.
  * **One AA ring shared** across channels (cutoff from the max channel
    ratio); each channel reads it on its own up-shift.

Cost stays at one read for the mono default; three only when spread > 0.

Tests: 9 new in `TestStereoSpread` — default + round-trip; ports; spread-0
returns a bare array; `out` unaffected by spread; detuned pair (out_l flat
/ out_r sharp); L/R decorrelation < 0.5; voice-row == mono on all three
outs; finite/bounded at extremes; engage-mid-stream no-dropout. Plus two
model tests updated for the new ports/param. `test_resampler` 63 → 72; full
suite 1948 pass / 1 skip. End-to-end (render_block, saw_wt → resampler
spread 20 → L/R speakers): master L/R correlation −0.03, both channels
alive. Docs: module docstring (top + params + ports), MODULES.md (ports +
param + a stereo paragraph + patching line + examples list), a `spread`
drag-float in the resampler UI block, and a new
`examples/resampler_stereo_spread.json`. Commit is Matthew's to run.

That closes the resampler "love" arc: **cubic Hermite read → anti-alias on
pitch-up → stereo detune spread.** One idea remains parked: a first-class
tape-stop / spin gesture.

## 2026-07-10 — resampler: anti-alias on pitch-up (the follow-up ships)

Matthew took the follow-up from the cubic session. Pitching *up* reads the
ring faster than it's written, shifting source highs above Nyquist where
they fold back as aliasing (real tape is inherently band-limited and never
does this). Measured baseline: a band-limited saw +12 st sits at −13 dB
alias/harmonics; a 15 kHz tone +12 st (→30 kHz, should vanish) folds back
to 14 kHz at essentially full level (peak 0.86).

**Approach — band-limit before the read, in a second ring.** Aliasing folds
*at* the decimating read, so it has to be removed *before* it (post-filtering
can't un-fold). New `antialias` toggle (bool, default **off** — house
`through_zero`/flanger convention, `float(...) >= 0.5` in the engine): when
on, the input is low-passed at `Fs/(2·ratio)` into a **second ring**
`buf_aa`, and the wet read samples that ring whenever the block pitches up.
Chosen over the alternatives (variable windowed-sinc; running the read at M×
oversample) precisely because it **leaves the delicate seam-declick core
untouched** — the fast path and the declick voice loop just receive
`read_buf` (= `buf_aa` on up-shift, else the raw `buf`) instead of a
hardcoded buffer. The dry tap always reads raw; unity and pitch-down keep
`read_buf = buf` (gated on `ratio.max() > 1`), so all their bit-exact paths
are untouched — the 55 prior tests pass unchanged with AA off *and* AA on at
unity is bit-identical to AA off.

**Filter.** 8th-order Butterworth in **sos** form (transfer-function form is
ill-conditioned at the low cutoffs of extreme up-shifts — its a-coeffs hit
~46; sos is stable, identical output), cross-block `zi` carried in state
(exact on static ratio, minor transient on a glide — acceptable). Two tuning
levers past raw order: the cutoff carries a **0.85 guard margin**
(`_RESAMP_AA_MARGIN`) so the filter's transition band sits below
Nyquist-after-scaling — content that survives it lands in-band instead of
folding, which mattered more than order beyond ~6; and a **Wn floor**
(`_RESAMP_AA_WN_MIN` 0.05) keeps the steepest up-shifts (past ~+52 st) in a
safe range with partial AA rather than a degenerate filter. Result: saw
+12 st −13 → −25 dB; the 15 kHz fold peak 0.86 → 0.11 (end-to-end through
`render_block` too, saw_wt source: −12.6 → −25.2 dB, finite/bounded).

**No-dropout on toggle / window change.** `buf_aa` is **seeded from the raw
ring** (`buf.copy()`) whenever it's missing or the wrong shape — so a live
toggle-on, a window resize, or a reinit never punches a wet dropout (the
recent tail is already there, unfiltered for one window then all AA'd). When
AA is off the second ring is dropped (`state.pop`) so a later toggle-on
re-seeds from the *up-to-date* raw ring rather than a stale gap.

Tests: 8 new in `TestAntialias` — default-off + JSON round-trip; unity
bit-exact with AA on; pitch-down on==off bit-for-bit (gating); folding-tone
+ band-limited-saw alias reduction; finite/bounded at extreme up-shifts (sos
stability); voice-row == mono; live-toggle no-dropout. `test_resampler`
55 → 63; full suite 1939 pass / 1 skip. Docs: module docstring (top +
params), `_render_resampler_core` docstring, MODULES.md (param row + an
anti-alias paragraph), and an `antialias` checkbox in the resampler UI
block (app.py, `through_zero` precedent). Constants `_RESAMP_AA_ORDER` /
`_MARGIN` / `_WN_MIN` next to the seam-declick ones. Commit is Matthew's to
run.

Follow-ups still open (from the cubic session): tape-stop/spin gesture;
stereo detune spread (`out_l`/`out_r`).

## 2026-07-10 — resampler: read upgraded to cubic Hermite interpolation

Matthew asked what the resampler could use; picked the fidelity direction
(over anti-alias / tape-stop / stereo-spread) from a question dialog. The
one real sonic weak spot was the ring read: **2-tap linear interpolation**
at all three read sites (fast path + the two declick taps), with no
anti-aliasing. Linear droops hard toward Nyquist and throws imaging
sidebands, so *every non-integer* transpose (detune-thicken, sample
pitching) read back dull-and-gritty in a way that isn't the nice tape
grit — just interpolation error.

Swapped it for **4-tap cubic Hermite (Catmull-Rom)**. New module-level
`_hermite4(pm1, p0, p1, p2, t)` helper (pure, next to `_dc_block`); all
three sites now gather four taps instead of two. The outer taps
(`i0-1`, `i0+2`) are **clamped to the window ends** — the correct
click-free boundary (hold the edge sample instead of wrapping to the
opposite end). In the fast path the guard band already keeps the head
≥ a block off either edge, so the clamp never binds there and the read
is a clean cubic; it only acts right at the declick seams.

**Bit-exactness held by construction.** At an integer read position
frac == 0, and Hermite's constant term is `p0` untouched by float ops, so
it returns the sample *exactly*. Unity ratio and octave/integer shifts
stay bit-exact — every existing bit-exact test (unity delayed
passthrough, cents≡semitones, cv-sum≡semitones, mix-half-at-unity
coherence, mix=0 dry, single-voice≡mono through seams) passes unchanged.
The cubic only differs where no test pins exact samples (fractional
reads), which is the point.

**Overshoot:** Catmull-Rom can mildly overshoot in principle (Lebesgue
const 1.25 at t=0.5); measured worst output/input on the extremes noise
test was 1.000 (unity dominates; every *shifted* case stayed *below* the
input peak on that signal), so the `<= 1.5` bound held with room. Left it
at 1.5.

**Honest scope.** The interpolator itself is 17–38× more accurate in the
low-mid band and ~3–7× near the top (measured, reconstruction vs a true
sine). But at the *engine* level the audible win is concentrated in the
**high end / bright material**: at 1.5 kHz cubic vs linear engine THD is
identical (seam + priming artifacts dominate there), while a 12 kHz tone
shifted down shows ~1.5× lower THD. So this is a clean-up for bright/
complex sources and big downshifts, not a night-and-day change on a
1 kHz sine. The bigger remaining lever — **anti-aliasing on pitch-up**
(a ratio-tracking low-pass so reading faster doesn't fold content past
Nyquist) — is untouched; it fights the lo-fi identity so it belongs
behind a toggle, filed as a follow-up.

Tests: 5 new in `TestInterpolation` — `_hermite4` endpoints (t=0 → p0
exact via `array_equal`; t=1 → p1, interpolating), collinear-ramp
reproduction (no overshoot on a line), reconstruction-beats-linear
(≥5× tighter RMS vs a 2-tap oracle on a 2.76 kHz sine), and
engine-read-is-cubic (monkeypatch `_hermite4` down to linear → a bright
downshift's THD measurably worsens, proving the module routes through the
cubic read). `tests/test_resampler.py` 50 → 55, all green. Docs: MODULES.md
+ the `_render_resampler_core` docstring updated linear → cubic. Commit is
Matthew's to run.

## 2026-07-07 (crash-test trigger) -- a way to force the GUI crash path

Matthew couldn't find a way to crash the (deliberately robust) app to eyeball
the new handler. Added a debug-only self-destruct in `App.run()`'s render loop:
`PYSYNTHRACK_CRASH_TEST=<frames>` makes the loop raise after N rendered frames,
which escapes `run()` into `main()`'s handler. Inert unless the env var is set
(non-integer value -> crash on the first frame). Verified the resulting
behaviour with a headless `main()`-wrapper smoke: friendly stderr pointer, crash
file in `~/.pysynthrack/crashes/`, exit 1, no traceback. Suite unchanged (1885).

Use (PowerShell): `$env:PYSYNTHRACK_CRASH_TEST=180; python -m pysynthrack`
(~3 s of live window at 60fps, then it crashes into the handler). Open a new
shell or `Remove-Item Env:PYSYNTHRACK_CRASH_TEST` to reset.

---

## 2026-07-07 (error-handler slice B) -- init suppresses + logs; global hooks wired

Completed the integration. Init now captures-suppresses-logs instead of
re-raising, and uncaught background crashes are caught globally.

- **`_crash.py`**: new `install_crash_logging()` registers a folder-writing
  observer on the (upgraded) handler and `install()`s the `threading` +
  `unraisable` hooks (excepthook is left to `ui.app.main`). The observer writes
  only BACKGROUND/uncaught reports (source `"uncaught"`); explicit catch points
  wrap their `describe_error` in the new thread-local `explicit_write()` guard
  so the observer skips them -- exactly one file per crash. `uninstall_crash_
  logging()` reverses it (tests / clean shutdown).
- **`ui/app.py:main()`**: calls `install_crash_logging()` early, then wraps
  `App().run()`; on `Exception` it writes the `"gui"` report and `sys.exit(1)`
  -- suppresses the traceback, keeps a non-zero exit (Matthew's call). Now
  catches `Exception` (not `BaseException`), so Ctrl-C / SystemExit pass through.
- **`numpy_backend._handle_audio_crash`**: unchanged behaviour, but its
  `describe_error` is now wrapped in `explicit_write()` so the global observer
  doesn't duplicate the `"audio_callback"` file.

**Testing.** New `tests/test_crash_logging.py` (observer writes uncaught to the
folder; `explicit_write` guard skips; flag resets after the block; uninstall
stops it; no observer without install). `test_crash` + `test_backend_crash`
unchanged and green. Full suite **1885 passed, 1 skipped**. Plus a headless
smoke: a real worker-thread crash fires the installed `threading.excepthook`
and lands one `*_uncaught.txt` in the folder.

**Pending -- real-window eyeball (meatthread0).** The suppressed GUI-crash path
(`App().run()` raises -> folder file + friendly stderr pointer + exit 1, no
traceback) can only be seen by launching the windowed app and forcing a crash.

---

## 2026-07-06 (error-handler slice A follow-up) -- vendored the upstream test suite

Copied upstream's `test_error_handler.py` (157 tests) into `tests/` to guard the
newly-vendored handler surface. One adaptation: upstream imports the module as
top-level `error_handler`; a 2-line shim at the top of the copy aliases
`pysynthrack.error_handler` under that bare name in `sys.modules`, so the file
stays a verbatim copy (trivial to re-sync) and the deeper `import error_handler`
sites resolve unchanged. On Python 3.14: **156 passed, 1 skipped** (the skip is
the module's own <3.11 graceful-fallback branch). Full suite now **1879 passed,
1 skipped**. The alias is inert for the rest of the suite -- app code always
imports via the `pysynthrack.error_handler` path.

---

## 2026-07-06 (error-handler slice A) -- vendored the upstream superset

Copied the standalone `error_handler.py` (2861 ln) over our older vendored copy
(1374 ln). Verified first that the vendored symbols are a subset of upstream --
the only vendored-only name was `_register`, which upstream keeps as an alias
for `register_extractor`, and which nothing outside `error_handler.py` uses.
Backward-compatible: the two call sites (`ui/app.py:main`,
`numpy_backend._handle_audio_crash`) use only `describe_error(include_locals=
True)` + `for_claude()`, both preserved. Full suite green (**1723**); `test_crash`
+ `test_backend_crash` pass unchanged. Gains the wrapping API
(`install`/`capture`/`capturing`/`register_observer`/`ReportFormatter`) that
Slice B will use to make init suppress-and-log. Follow-up worth considering:
vendor upstream's `test_error_handler.py` (157 tests) to guard the new surface.

---

## 2026-07-06 (error-handler scout) -- it's an upgrade, not a new add

Scouted Matthew's standalone "Python ErrorHandler" lib with a view to wrapping
init to suppress + log to folder. Findings:

- **We already vendor an older version of it.** `error_handler.py` (1374 ln) is
  a clean ancestor of the standalone (~2861 ln); no local mods. The standalone
  is a superset that adds `install()`, `@capture`/`capturing()` (suppress-and-
  continue), `ReportFormatter`, `register_observer`, asyncio wiring, PEP-657
  column anchors, more extractors, `to_json`/`to_markdown`. Ours has only the
  core `describe_error` / `ErrorReport.for_claude()` / redactors / ExceptionGroup.
- **Folder logging already exists.** `_crash.write_crash_report` writes
  `for_claude()` to `~/.pysynthrack/crashes/`; never raises.
- **Init is already wrapped but RE-RAISES.** `ui/app.py:main()` catches around
  `App().run()`, logs to the folder, prints a pointer, then `raise`s. Second
  catch point: `numpy_backend._handle_audio_crash` (audio_callback, once/stream).
- Guarded by `tests/test_crash.py` + `tests/test_backend_crash.py`.

Plan (not yet done): (A) upgrade `error_handler.py` to the superset
(backward-compatible -- call sites use only `describe_error(include_locals=True)`
+ `for_claude()`); (B) rewire `main()` to `capturing(reraise=False,
on_report=...)` so init SUPPRESSES instead of re-raising, still logging to the
folder; optionally `install()` for worker-thread / `__del__` coverage. Open
decision: suppress-and-exit-clean vs suppress-but-exit-nonzero vs keep-reraise.

---

## 2026-07-06 (settings persistence -- slice 2) -- per-patch window geometry

Second and final persistence slice. A patch now remembers the editor window's
**size and position**, restored (off-screen-safe) when the patch loads.

**New `ui/window_geometry.py`** (pure, dpg-free, like zoom/buffer): `resolve`
takes the saved `patch.ui["window"]` dict + the virtual-desktop bounds and
returns the size/pos to apply, clamping the whole window inside the desktop so
a **stale off-screen coordinate** (saved on a monitor that's since gone) is
pulled back into view rather than lost. Bounds unknown (non-Windows / query
failed) -> restore size only, never a blind position. `make_geometry` builds
the serialised dict.

**Glue in app.py.** `_capture_window_geometry` (in the save path, beside
`_capture_node_positions`) snapshots `get_viewport_width/height/pos` into
`patch.ui["window"]`; `_apply_window_geometry` (end of `_load_patch_from`,
after the zoom restore) runs `resolve` and applies via `set_viewport_*`.
`_virtual_screen_bounds` reads the Win32 virtual-screen metrics
(`GetSystemMetrics` 76-79) via ctypes, guarded -> None off-Windows. `patch.ui`
already round-trips, so no patch_io change.

**DPG 2.3.1 limitation -- maximized not captured.** Confirmed empirically: DPG
2.3.1 exposes no maximized state (no `is_viewport_maximized`, no `maximized`
key in `get_viewport_configuration`). So only size+pos are stored; a maximized
window restores to that same full size+position -- visually near-identical,
just not a true OS-maximized toggle. If we want real maximize later: a
heuristic (restored ~= monitor size -> `maximize_viewport()`), or wait for a
DPG that surfaces the state.

**Testing.** `tests/test_window_geometry.py` (16 cases: junk->None, size floor
+ desktop cap, screen-None keeps-size/drops-pos, within-bounds preserved,
off-screen right/left/partial clamped, secondary-monitor incl. negative
origin). Headless DPG smoke: capture -> resolve(stale x=5000 -> 920) -> apply,
and `set/get_viewport_*` round-trips cleanly (API contract verified; visual
result still wants an eyeball). Full suite green: **1723 passed**.

**Pending -- real-window eyeball (meatthread0).** Save a patch, move/resize the
window, reopen -> should restore; drag it onto a now-absent monitor coord,
reopen -> should clamp back on-screen. Only unverifiable-from-here part.

---

## 2026-07-06 (settings persistence -- slice 1) -- global settings.json, buffer size persists

Follow-on to the buffer slider. Buffer size now survives app restarts via a
new machine-scoped settings store -- the first of two persistence slices
agreed with Matthew (slice 2, per-patch window geometry, is still to come).

**New `pysynthrack/settings.py`** -- a generic JSON key/value store in the
platform config dir (`%APPDATA%\PySynthRack\settings.json`, with
`$XDG_CONFIG_HOME`/`~/.config` fallbacks and a `PYSYNTHRACK_SETTINGS`
override). Reads are total (missing/corrupt/non-dict -> `{}`, never raises, so
a bad file can't block launch); writes are atomic (temp + `os.replace`).
Deliberately generic and buffer-agnostic -- the natural home for audio device,
backend choice, and a default window size later.

**Wiring.** `App.__init__` loads the settings once (`self._settings`) and
resolves the buffer size through the new pure `ui.buffer.coerce_buffer_size`
(snap-or-default, so junk in the file can't crash the slider). The slider is
built from that persisted value, so it shows the remembered size on launch.
`_on_buffer_slider` writes the change back via a best-effort `_persist_setting`
(a read-only / permission-denied save is logged and swallowed, never fatal).
Buffer size stays *global*, not per-patch -- a hardware/latency setting must
not ride inside portable patch files.

**Testing.** `tests/test_settings.py` (path resolution across
override/APPDATA/XDG, total loads incl. corrupt/non-dict/directory, atomic
round-trip, no tmp sidecar) plus coerce cases in `tests/test_ui_buffer.py`.
Manual relaunch smoke: launch1 default 512 -> pick 128 -> launch2 loads 128.
Full suite green: **1707 passed**.

**Pending.** Slice 2: per-patch window geometry (size+pos+maximized in
`patch.ui`, off-screen-safe restore) -- GUI-heavy, wants a real-window eyeball.

---

## 2026-07-06 (buffer-size slider) -- global block_size control on the toolbar

Added a toolbar **Buffer** slider (mirrors the Zoom control) letting the user
pick the audio block size from a fixed set -- 64, 128, 256, 384, 512, 768,
1024 frames -- applied globally to the backend when audio is placed into
running mode. Default 512, matching `AudioBackend`'s existing default.

**Why a slider carrying an *index*.** The stops are non-uniform, so a raw
value slider would let you land between them. New dpg-free helper
`ui/buffer.py` (same pattern as `ui/zoom.py`) exposes `BUFFER_SIZES`,
`snap_buffer` (nearest stop, ties -> smaller/lower-latency) and
`index_to_size`/`size_to_index`. The slider spans indices 0..6; its printf
`format` is rewritten in the callback to show the real frame count on the
handle (the `format=""` fader precedent at app.py:1923 confirms DPG accepts
literal, specifier-less formats).

**Apply path.** `_on_toggle_audio` calls `backend.set_block_size(size)`
*before* `compile()`/`start()` -- numpy builds its device outputs and pyo
boots its Server during compile, so the size must be set first. New
`AudioBackend.set_block_size` is record-only in the base (numpy reads
`block_size` fresh each `start()`, so nothing else to do). **Pyo overrides it
to reboot:** a booted pyo Server bakes in `buffersize`, so a change tears the
server down (stop -> shutdown -> `_server=None`) and the next compile boots
fresh. The slider greys out while running, since the size is only read at
Start -- editing mid-run would be misleading. Running status line now shows
the active buffer.

**Testing.** `tests/test_ui_buffer.py` (mirrors the zoom suite -- snap /
index / size round-trips) and `tests/test_backend_block_size.py` (numpy
records; pyo no-ops on unchanged size, tears the server down on change, stops
first if running -- via a fake Server so pyo need not be installed). Full
suite green: **1690 passed**.

**Pending / not done here.**
- *Real-GUI eyeball* (meatthread0): no automated path builds the DPG toolbar
  (`--cli` plays a patch headlessly, no chrome), so the actual slider render,
  the `format` size readout, and the grey-out want a manual launch.
- *Pyo reboot on real pyo*: re-booting a Server in one process is the fussy
  bit -- verify shutdown+fresh-boot holds, else switch `_ensure_server` to
  reuse one Server via `shutdown()`+`reinit()`+`boot()`. The numpy path
  (what runs here) is unaffected.
- *Persistence*: buffer size is in-memory (global, resets to 512 each launch),
  matching how zoom isn't persisted. A small app-settings file could persist
  both later.

---

## 2026-07-06 — specific_stereo_speaker_output: live device switching

- Changing a sink's `device` while running now takes effect immediately —
  no Stop/Start. Only the affected secondary stream is rebuilt.
- `_open_device_outputs` → `_sync_device_outputs`, a reconciler: diff the
  sinks' selected devices against the open streams, open the new, close the
  now-unused, keep the rest (identity preserved). Builds a fresh dict and
  swaps `_device_outputs` in one assignment (audio thread only reads the
  reference → never a half-updated map); removed streams closed after the
  swap. Open failure logged + skipped + retried next change.
- Hooked at start() (opens all from empty), set_param (only when a
  specific_stereo_speaker_output `device` changes while running), and the end
  of compile() while running (outside the render lock — so add/remove a routed
  sink live is picked up too).
- 40 module tests (TestLiveDeviceSwitch stubs _DeviceOutput.open/close, no
  PortAudio). Full sandbox suite 1666/0.

---

## 2026-07-06 — specific_stereo_speaker_output (slice 2: real per-device routing)

- A named `device` now actually plays out that device. Left empty (`""`) the
  sink still drains to the master bus, bit-identically to `stereo_speaker_output`.
- Engine split: `render_block_multi(frames)` walks the graph once and returns
  `(master, {device: bus})`; routed sinks drain into per-device buffers
  **excluded** from master (each clipped ±1). `render_block` is now a thin
  master-only wrapper, so every existing offline caller is unchanged.
- `_DeviceOutput`: one secondary `sd.OutputStream` per distinct selected device,
  fed by the main callback through a `deque(maxlen)` ring (append/popleft are
  GIL-atomic → no lock; underrun & frame-mismatch → silence; overflow → drop
  oldest). `start()` opens one per device (snapshot → device change applies at
  next Start, MicInput convention; open failure logs + that sink stays silent).
  `stop()` closes them.
- Caveat: two PortAudio streams aren't sample-clock-synced; the ring absorbs
  drift and the 2nd device sits a few blocks behind. Monitor/cue bus, not
  phase-locked multi-device playback.
- Crash/DSP-load tests: the audio-callback render seam moved to
  `render_block_multi`; their monkeypatch points followed it.
- 32 module tests (routing + `_DeviceOutput` ring). Full sandbox suite 1658/0.
- MODULE COMPLETE (both slices on origin/main after this applies).

---

## 2026-07-06 — specific_stereo_speaker_output (slice 1: picker only)

- New sink `specific_stereo_speaker_output`: a clone of `stereo_speaker_output`
  carrying a `device` parameter so a patch can name which physical output the
  sink should play out of (cue/monitor bus → headphones, main mix → monitors).
- **Slice 1 ships the module + the live picker only.** Audio still drains into
  the shared master bus, **bit-identically** to the plain stereo speaker
  (verified by an A/B sweep over mono/stereo/pan/width/gain/CV). The `device`
  value is picker- and save-file-only and has no audible effect yet.
- Added `available_output_devices()` — the output mirror of MicInput's input
  enumerator (filters `max_output_channels > 0`, de-dupes, never raises).
- Backend: `_STEREO_SPEAKER` (str) → `_STEREO_SPEAKERS` (frozenset of both
  stereo-sink types); drain dispatch + sink check now test membership.
- UI: device dropdown + Refresh (MicInput pattern), listing output devices.
- pyo backend: listed in the not-yet-supported stub set (pyo stays parked).
- 23 tests; full sandbox suite 1649/0 (mido + scipy + dearpygui installed).

---

## 2026-07-06 — convolver (slice 3: predelay / tone / normalize + example IRs)

Final slice of the **convolver** — the module is now complete.

Wet shaping. Two new params, both wet-only (so `mix = 0` stays a bit-exact dry
bypass): `predelay` (0..500 ms) is a per-channel FIFO delay on the wet, an
intentional delay on top of the module's one-block latency so the reverb starts
behind the dry; `tone` (1k..20k Hz) is a per-channel one-pole low-pass that
darkens the tail and is **off** at its 20k maximum (exact bypass, so the neutral
is untouched). Both live in a new `_shape_conv_wet(wet, state, ch, tone, pd,
frames)` applied to each channel after the convolution and before the wet
`gain`/`mix`; state carries `tone_zi_<ch>` (filter zi) and `pd_buf_<ch>` (the
predelay FIFO). They run per channel even when the convolution engine is shared
(mono IR) — the FFT is the expensive part; the shaping is cheap.

IR conditioning on load (in `_IRLoader`). (1) **Length cap**: IRs longer than
`_IR_MAX_SECONDS` (5 s) are truncated with a ~10 ms fade-out (no click), so a
stray long file can't stall the audio — the DSP% readout is the meter. (2)
**Energy-normalise**: a shared scale `1 / max(||L||2, ||R||2)` (module-level
`_normalize_ir`, shared with the tests) — the louder channel gets unity RMS
gain through the convolution, the quieter keeps its relative level, so hot/long
IRs don't blow up and different IRs sit at a consistent level while the stereo
image survives. The unit impulse (norm 1) and a single-spike IR normalise to a
pass-through, so the transparent-insert neutral holds.

Example IRs are **license-clean by construction** (pure synthesis) and shipped
as a **generator script**, not committed binaries (Matthew's pick):
`examples/irs/generate_irs.py` writes seeded, decorrelated decaying-noise
room/hall/plate WAVs (with early reflections + a darkening tail);
`examples/irs/.gitignore` keeps the generated `*.wav` out of git.
`examples/convolver_reverb.json` is a clock→AD→VCA pluck through the hall IR
(predelay 18 ms, tone 7.5 k, mix 0.4; osc amp 0.5 / speaker gain 0.7 for
headroom) — it loads and passes audio even before you run the script (an
unreadable path is transparent), and the reverb appears once `hall.wav` exists.

Tests (`tests/test_convolver.py`, now 65): predelay delays the wet by exactly D
samples (dry untouched), tone off = passthrough / tone low attenuates 9 kHz,
shaping never touches `mix = 0`, a hot IR is energy-normalised (bounded + matches
`fftconvolve` of the normalised IR), a single-spike IR normalises to transparent,
and a long IR is length-capped (monkeypatching `_IR_MAX_SECONDS`). Slice-2
file-load refs updated to normalise. Sandbox suite **1567 passed / 18 mido-skips**
(the two dearpygui UI files ignored — no GUI lib in the sandbox).

pyo: still the silent stub (unchanged). No app.py change this slice — `predelay`
/`tone` render as generic sliders and the renderer clamps them.

Deviation note (working agreement): the `path`/Browse special-case already
covers it; no UI work needed. Bounded sliders for predelay/tone left as a
possible follow-up (generic drag-floats + renderer clamps for now).

---

## 2026-07-06 — convolver (slice 2: IR file load + true stereo)

Second slice of the **convolver**: it now loads real IR files and is a true
stereo effect. (Slice 3 — predelay/tone/normalize + license-clean example IRs
— is still to come.)

IR loading. A `path` param (empty by default) points at an audio file; the
node grows a **Browse…** button (the file_player's picker, extended in app.py
by one condition — `module.TYPE in ("file_player", "convolver")`). Decode
reuses the backend's `_decode_audio` (scipy WAV fast path → ffmpeg for
mp3/flac/ogg/m4a/video), so anything the FilePlayer can read works as an IR.
IRs load **whole** (they're short — no streaming), but the decode + the
per-channel partition-FFT build run on a **background thread** (`_IRLoader`,
daemon), kicked at `compile()` (mirroring the file_player lifecycle) and on any
live `path` edit in the renderer. The audio thread never decodes: it keeps the
previous IR (or the transparent unit impulse) sounding until the loader
publishes `ready`, then adopts the new engines at a block boundary. An empty /
missing / unreadable path stays a transparent insert (a saved patch always
loads). `wait_for_ir_loads()` is the tests/offline join hook (the
`wait_for_file_decodes` analogue).

True stereo. `_IRLoader` builds one `_PartitionedConvolver` per IR channel
(sharing a single engine when the two channels are identical — a mono file — so
a mono IR convolves once). The mono-summed input is convolved through the IR's
left channel into `out_l` and its right into `out_r`; the IR's own
decorrelation is the stereo image. Latency, the wet-only `gain`, the `mix = 0`
bit-exact bypass and the voices-sum-to-mono contract are all unchanged from
slice 1.

State machine (`_render_convolver`): `path == ""` → drop IR + cancel pending →
transparent; `path` changed → kick a loader, keep current engines; a finished
loader → adopt (ready) or fall back (failed); engines (re)built lazily on
first use, block-size change, or IR swap. Per-convolver state carries
`engine_l/engine_r/ir_l/ir_r/loaded_path/pending/block/dry_prev`; the drop-state
path in `compile()` closes a mid-flight loader.

IRs are **not normalised** and length is **not capped** yet — both are slice 3;
for now trim hot IRs with `gain` and watch the DSP% on very long IRs.

Tests (`tests/test_convolver.py`, now 58): everything from slice 1 plus a seeded
**stereo** IR (out_l/out_r each match their channel's `fftconvolve` and differ),
a mono IR sharing one engine with equal channels, a real **temp-WAV** stereo
load (compile → `wait_for_ir_loads` → convolves), a mono WAV giving equal
channels, a missing path staying transparent, and a **live path change**
adopting the new IR. Sandbox suite **1560 passed / 18 mido-skips** (the two
dearpygui UI files ignored — no GUI lib in the sandbox; unrelated).

Deviation note (working agreement): none of substance — built off-mount via
clone + patch. app.py touched (one condition) to give the convolver the same
Browse button as the file_player.

---

## 2026-07-06 — convolver (slice 1: mono partitioned-FFT core)

First slice of the **convolver** (Effects), an IR convolution reverb /
cabinet loader. This slice is the mono fixed-block DSP core; the IR file
loader + true stereo (slice 2) and predelay/tone/normalize + example IRs
(slice 3) follow in later sessions.

DSP: uniformly-partitioned **overlap-save** FFT convolution
(`_PartitionedConvolver` in numpy_backend.py). The IR is split into
render-block-sized partitions, each pre-transformed once to a length-2B
rfft; every block transforms the `[prev|cur]` 2B window once, pushes it onto
a frequency-domain delay line of the last P = ceil(L/B) input spectra, and
the output is the FD multiply-accumulate Σ_p H[p]·X[k−p] inverse-transformed
(the alias-free overlap-save half). One FFT pair per block instead of an
O(L) tap sum, so cost tracks IR length — the DSP% readout is the budget
meter.

The overlap-save core is intrinsically zero-latency; a one-block output
register presents a clean, fixed **one-block (B-sample) latency**, and the
dry path is delay-matched by that block inside `mix` so dry/wet stay
phase-coherent. Voices are summed to mono before convolving (convolution is
linear: per-voice-then-sum == sum-then-convolve, and mono is far cheaper),
so a single voice row is bit-identical to mono. `gain` trims the **wet
only**, so `mix = 0` is a bit-exact dry bypass (and short-circuits the FFT)
whatever `gain` is.

Until the file loader lands the IR defaults to a **unit impulse** — a
freshly added Convolver is a transparent insert (delayed passthrough) that
does nothing until you load a real IR. Both outputs carry the same mono
result; they split into a stereo pair with stereo IRs (slice 2).

Neutral & tolerances: unit-impulse IR at mix=1/gain=1 is a passthrough
delayed one block, within ~1e-6 (the FFT round-trip is float, not bit-exact
— pinned & documented); mix=0 is bit-exact delayed dry. Because N = 2B
depends on the block size, block-size independence holds within FFT
round-off (~1e-6), not bit-exact.

Tests (`tests/test_convolver.py`, 52): model/ports/defaults/category/JSON;
engine oracle vs `scipy.signal.fftconvolve` across block sizes
{64,128,256,512,333} × IR lengths {1,5,511,512,513,2000}; impulse = delayed
identity; tail length matches IR; one-block latency; neutral (impulse mix=1
passthrough <1e-6, mix=0 bit-exact dry, both channels equal, silence→silence);
latency constant across block sizes; module-level oracle with an injected IR
(gain scales wet only, mix blends); big-block == small-blocks within 1e-6;
single voice row ≡ mono, voices sum before conv; osc→convolver→stereo speaker
integration. Sandbox suite 1554 passed / 18 mido-skips (the two dearpygui UI
files — test_dsp_load, test_ui_zoom — are uncollectable without a GUI lib in
the sandbox, so ignored; unrelated to this change).

pyo: silent stub (added `convolver` to the TYPE list). Example
`examples/convolver_insert.json` (osc → convolver → stereo speaker,
transparent until you Browse an IR; source amp 0.4 + speaker gain 0.7 for
headroom). Docs: MODULES.md index row + catalogue entry.

Deviation note (working agreement): none of substance — built off-mount via
clone + patch per protocol. Design note: proper partitioned convolution is
intrinsically zero-latency; the fixed one-block latency is a deliberate,
reported, dry-compensated choice matching the spec (a one-block pipeline),
not a limitation.

---

## 2026-07-06 — tape ("put it on tape": wow/flutter/drift + saturation + hiss + head bump)

New "Effects" module `tape` — the character of an analog tape machine in
one pass. Six independent flavours, layered in the order a deck imposes
them: **wow** (slow ~1 Hz pitch sway), **flutter** (fast ~9 Hz waver +
noise), **drift** (very slow, non-periodic speed wander), **sat** (tanh
saturation), **hiss** (calibrated noise floor), **bump** (~60 Hz low-shelf
head bump), plus **mix**.

Signal flow: `in → wow/flutter/drift-modulated fractional-delay line →
saturation → + hiss → head-bump low shelf → mix against the
latency-matched dry`. The delay line reuses the **chorus core** (fixed
~10 ms nominal delay; write the whole block, then read fractional taps
behind the write head). A moving delay is a moving pitch — that *is* the
wow/flutter/drift. No feedback, so every read references an
already-written sample, the whole render vectorizes, and it is **exactly
block-size independent**.

Modulation detail: wow is a slow sine, flutter a fast sine + a little
band-limited noise, drift a heavily low-passed (~0.35 Hz) random walk,
unit-std-normalised and hard-bounded so the read can never cross the write
head (a final `clip(delay, 2, L−2)` is the safety net). The wow/flutter
LFO phases persist in `self._state`; the drift, flutter-noise and hiss are
each a **single seeded `default_rng`** (`_TAPE_DRIFT_SEED` /
`_TAPE_FLUT_SEED` / `_TAPE_HISS_SEED` + `module.id`) drawn **one sample
per output sample** and streamed through one-pole / biquad filters with
carried `zi` — so every stochastic path is block-size independent too, and
a patch renders identically every time. Saturation is
`tanh(drive·u)/tanh(drive)` via `_dist_curve("soft", …)` on the shared
`_Oversampler4` (4x); its 16-sample OS latency is folded into the dry
latency-comp. Hiss is a calibrated floor (RMS = `10^(hiss_db/20)`) living
in the wet path so it scales with `mix`; off at/below −80 dB. The head
bump is `_loud_shelf()` (0 dB → identity).

One tape path is modelled: the modulation and hiss are **shared** across a
polyphonic input's voices (each voice keeps its own delay line,
oversampler and shelf state, so they never cross-talk); the `(V,F)` core
runs with `V=1` for mono, so a **single voice row is bit-identical to
mono**. Neutral (`wow=flutter=drift=sat=bump=0` with `hiss` off)
short-circuits to a **bit-exact passthrough** — a freshly added Tape does
nothing until you turn a knob; `mix=0` is likewise bit-exact dry, no state
advance.

25 tests in `tests/test_tape.py` (model: registration/defaults/ports/kinds
/JSON/unknown-param/type walls; contract: disconnected silence, frames=0,
neutral bit-exact, mix=0 dry, float32, finite+bounded at extremes, voice
shape; character: **wow → measurable pitch deviation** via a windowed
parabolic-peak tracker — ~59 Hz swing on a 2 kHz tone vs 0 baseline;
**saturation THD monotone in `sat`** — 0.027 → 0.122 → 0.230; **hiss
calibrated** to within 0.1 % of the target dB and **reproducible**; **head
bump lifts lows not highs**; **exact block-size independence** at 512 /
4096 / 333; voice: single row ≡ mono, voices independent; osc→tape→speaker
integration). Sandbox suite **1561 passed / 0 failed** (dearpygui + mido
installed in-sandbox so the UI/MIDI files collect).

pyo: silent stub (added `"tape"` to the TYPE list). Example
`examples/tape_cassette.json` — clock → sequencer → saw + pluck ADSR → VCA
→ tape (wow 0.5 / flutter 0.35 / drift 0.3 / sat 0.45 / hiss −48 / bump 3.5
/ mix 0.7) → speaker; source amp 0.4, speaker gain 0.8, master peak ~0.64
(headroom rule). Docs: MODULES.md index row + catalogue entry (in the
patch). No CV inputs, so nothing to add to the `cv_depth` conventions
table.

Deviation note (working agreement): none — built off-mount via clone +
patch per protocol; WORKLOG/TODO held out of the patch as snippets
(compressor/ring_mod/freq_shifter/bitcrusher pattern) because your tree
carries uncommitted doc drift.

---

## 2026-07-05 — bitcrusher (bit-depth quantize + sample-rate decimation)

The lo-fi "digital destruction" box, new in "Effects". Two independent
degradations in one module. **Bit reduction**: a mid-tread quantizer,
`round(x·2^(bits−1))/2^(bits−1)` — 24 bits *skips* the op (bit-exact),
8 bits is grainy, 1–3 bits is buzzy digital fuzz. **Sample-rate
reduction**: a sample-and-hold that holds every `rate_div`-th sample
with **no anti-image filter**, so the discarded content folds back as
aliasing — that harsh alias *is* the sound (early-sampler / Aphex crunch).
`rate_div=1` skips it.

The decimator is vectorized, no per-sample loop: when `jitter=0` the
sampling instant for global index g is `(g//N)·N`; when jitter>0 the
hold length wobbles around N on a **seeded** stream and boundaries are
the cumulative sum, located per sample with `searchsorted`. The hold
phase — global sample offset, the per-voice held value that spans a
block edge, and (jitter) the boundary list + `default_rng` — persists in
`self._state`, so holds are continuous across block joins and **every
path is *exactly* block-size independent** (quantize and decimate
commute — the quantizer is pointwise — so their order is immaterial).
Jitter is seeded off `_BITCRUSHER_JITTER_SEED + module.id`, so a patch
renders identically every time, and it's inert unless `rate_div>1`.

`mix` blends dry/wet (`mix=0` bit-exact dry); the neutral `bits=24 ∧
rate_div=1` (dc off) short-circuits to the input untouched — bit-exact
at any mix. `dc_filter` (off by default) is a one-pole DC blocker
(`y=x−x1+R·y1`, ~20 Hz corner) with per-voice state, the only
per-sample loop and only when enabled. Shape-polymorphic (V,F) core with
V=1 for mono → one voice row bit-identical to mono, voices independent.

29 tests in `tests/test_bitcrusher.py` (model/kind wall, disconnected
silence, neutral + mix0 bit-exactness, quantize step-exact/on-grid/
bits=1/coarser-at-fewer-bits, hold pattern exact incl. across block
joins + flat plateaus + fewer transitions at larger rate_div, jitter
seeded-reproducible/wobbles-lengths/changes-result/inert-without-decim,
DC removal + off-by-default, mix blend, single-voice≡mono, voices
independent, (V,F) preserved, block-size independence exact across all
combos, extremes finite). Sandbox suite **1477 passed / 18 mido skips**
(2 dearpygui UI files uncollectable in-sandbox as usual); that's the
freq_shifter baseline 1448 + 29.

pyo: silent stub (added to the TYPE list). Example
`examples/bitcrusher_lofi.json` — clock → sequencer → saw + pluck ADSR →
VCA → bitcrusher (8-bit / quarter-rate, slight jitter, DC filter) →
speaker; source amp 0.5, speaker gain 0.8, peak master ~0.49 (headroom
rule). Docs: MODULES.md index row + catalogue entry (in the patch).

Deviation note (working agreement): none — built off-mount via clone +
patch per protocol; WORKLOG/TODO held out of the patch as snippets
(compressor/ring_mod/freq_shifter pattern) because your tree carries
uncommitted doc drift.

---

## 2026-07-05 — freq_shifter: Bode single-sideband frequency shifter (up/down sidebands)

The inharmonic twin of `ring_mod`, and a different animal from
`pitch_shifter`: instead of multiplying every partial by a ratio, it
*adds the same number of hertz to every partial*, so harmonic input turns
inharmonic — metallic clang, hollow phasing, and (with feedback) the
barberpole/Shepard endless-glide. Two outputs from one shift: `out_up`
raises every partial by `shift` Hz, `out_down` lowers it — the two
sidebands of a single-sideband (Bode/Moog) modulation.

The analytic signal is built with a 255-tap Type-III FIR **Hilbert pair**
(`_design_fs_hilbert`, module-level like `_OS_FIR`). Type-III = odd length,
antisymmetric, so the group delay is an **integer** 127 samples (~2.9 ms) —
no fractional-delay games on the dry path. Window choice mattered: I
benchmarked Hamming / Blackman / Kaiser / Blackman-Harris and Hamming was
the clear winner — a flat >55 dB rejection that *holds down to low
frequencies*, where the wide-transition windows collapse to ~25–33 dB
against the Type-III DC null (measured in-sandbox before committing to a
design). `out_up = x_d·cos φ − x_h·sin φ`, `out_down = x_d·cos φ + x_h·sin
φ`; φ integrates `2π·shift/sr` per sample, per voice, exclusive-prefix so a
fresh module's first sample sits at φ=0 (the ring_mod carrier convention).
`shift_cv` is **linear Hz** (a shift is additive, not 1 V/oct), depth 200
Hz/unit, clamped ±Nyquist.

The nice structural bit: at `shift = 0` the wet is exactly `x_d`, the input
delayed by the 127-sample Hilbert latency — the `x_h·sin(0)=x_h·0.0` term
is exactly zero — so it is bit-exactly the delay-matched dry, and the `mix`
blend at shift 0 is transparent (no combing). `mix = 0` short-circuits to
the untouched input (bit-exact bypass, no latency, no state advance), the
chorus/ring_mod contract.

Feedback (recirculates the wet `out_up`, the barberpole comb) is the one
part that looks like it should break block-size independence. It doesn't: I
process each block in fixed 127-sample (= the Hilbert latency) chunks, so
`out_up[n−L]` for a chunk is always already computed — the recurrence is
causal and boundary-independent, and with the FIR streamed through
`lfilter` (carried `zi`) plus a per-voice feedback delay line the result is
**block-size independent even with feedback** (measured bit-exact after the
float32 cast at 512 vs 4096 vs 333; I pinned the feedback test at <1e-6 to
be safe across scipy versions). `feedback` is clamped to 0.9 for a bounded
loop.

Shape-polymorphic `(V, F)` core, `V=1` == mono; per-voice Hilbert / delay /
phase state. 32 tests in `tests/test_freq_shifter.py` (model + kind walls;
mix=0 bit-exact dry; shift=0 == delayed dry bit-exact; single upper/lower
sideband with >40 dB image rejection by FFT; near-unity gain; negative
shift swaps sidebands; mix blend keeps both dry and shifted peaks; shift_cv
moves the shift, depth 0 disables; feedback bounded to the clamp + adds
copies; single-voice≡mono; voice independence; (V,F) shape; block
independence with and without feedback; finite at extremes; compiled-graph
stereo integration). Sandbox suite **1448 passed, 18 mido skips** (+2
dearpygui UI files uncollectable, the usual); `git am` pre-flighted clean
onto HEAD e4388e7. pyo: silent stub, added to the TYPE list. Example
`examples/freq_shifter_barberpole.json` — a saw drone (amp 0.2, headroom
rule) shifted with feedback into a rising stereo shimmer, peak ~0.41. Docs:
MODULES.md index row, CV conventions-table row (shift_cv = linear Hz), and
a full catalogue entry.

Deviation note (working agreement): none — built off-mount via clone +
patch per protocol; WORKLOG/TODO handed as snippets (this file) because of
the usual local doc drift.

---

## 2026-07-05 — ring_mod: metallic ring modulator (in × carrier)

New Effects module `ring_mod`: `out = in × carrier`. The carrier is either
an external audio cable on `carrier` or, when that's unpatched, an internal
per-voice phase-accumulated sine at `freq` (1..5000 Hz) swept 1 V/oct by
`freq_cv × freq_cv_depth`. `mix` blends dry against the modulated signal;
`mix = 0` is a bit-exact dry passthrough.

DSP (`_render_ring_mod` + `_ring_match_voices` + `_ring_internal_carrier`
in numpy_backend). Blended `(1−mix)·in + mix·(in·carrier)`. The internal
carrier integrates phase per sample from `freq · 2^(freq_cv_depth·freq_cv)`,
per-voice phase held in `self._state` (continuous across blocks); an
**exclusive prefix sum** puts a fresh module's first sample at phase 0
(sin → 0), so the carrier waveform is deterministic/testable. The `(V,F)`
core runs with V=1 for mono → a single voice row is bit-identical to mono;
voices independent via per-voice phase. `mix ≤ 0` short-circuits to the
input untouched (no phase advance) — the chorus/distortion dry contract.

Invariants. Dry (mix=0) and the external-carrier path are exactly
block-size independent (pure elementwise multiply, no accumulation); the
internal sine matches across block sizes to < 1e-6 (float phase-wrap
rounding — same class as the oscillator's phase accumulator, pinned by
tolerance and documented). A patched `carrier` bypasses `freq` / `freq_cv`.
Mismatched carrier/cv voice counts sum-to-mono-then-broadcast (the delay's
`time_cv` fallback).

pyo: silent stub (numpy is the real impl). UI: `ring_mod` knob branch
(freq 1..5000 Hz, freq_cv_depth 0..4 oct/unit, mix 0..1 slider). Example
`ring_mod_bells.json` — a clock-plucked sine (ADSR/VCA) rung by a 523 Hz
internal carrier into tuned bells; source amp 0.3, speaker 0.7 (gain
headroom, stereo peak ~0.21).

Tests: 22 in `tests/test_ring_mod.py`. Sandbox suite 1457 passed / 18 mido
skips, green (includes the 2 dearpygui UI files once dearpygui is present).
Shipped as `0001-ring_mod.patch` (8 files incl. docs/MODULES.md, 674
insertions, pure adds; git-am pre-flighted clean onto HEAD 87a9a03).

---

## 2026-07-05 — transient_shaper (threshold-free attack/sustain shaper)

NEW module `transient_shaper` ("Effects"), `in → out`. Reshapes the
dynamic envelope with no threshold and independent of level.

- **DSP.** Two envelope followers on `|in|` (fast + slow) via the shared
  fixed-point core (`_audio_to_cv_block`, symmetric one-pole). Their dB
  difference isolates the transient: positive part (fast leads → onset)
  drives the `attack` gain, negative part (fast trails → decay) drives the
  `sustain` gain; each soft-saturated (`1 − exp(−|Δ|/_TS_SENS_DB)`) to top
  out near ±`_TS_MAX_DB` (12 dB), summed in dB, a 2 ms one-pole smooths the
  linear gain, multiply. Being a dB *ratio* the control is level-invariant
  → threshold-free.
- **Params.** `attack`/`sustain` −1..+1 (0), `speed` fast|med|slow →
  `_TS_SPEEDS` (0.5/20, 2/50, 5/120 ms). `attack==sustain==0` short-circuits
  to a bit-exact passthrough.
- **Invariants.** Single voice row bit-identical to mono; per-voice
  fast/slow/gain state (no cross-talk); block-size independent to float64
  round-off (reassociated follower solve, like the compressor; observed
  bit-exact after the float32 cast in the sandbox). No `*_cv` inputs, so the
  depth-param convention doesn't apply.
- **Tests.** `tests/test_transient_shaper.py`, 20 tests — model/ports/
  signal-kind walls/CATEGORY, neutral bit-exact (every speed), attack-moves-
  click / sustain-moves-tail with each knob's off-region exactly unity, level
  invariance at −20 dB + steady-tone transparency, block-size independence,
  single-voice==mono + independent voices, robustness (silence/DC/unknown-
  speed/integration). Sandbox suite 1394 passed / 18 mido-skips (+20 over the
  noise_gate baseline of 1374; 2 dearpygui UI files uncollectable in sandbox).
- **Delivery.** Code-only patch `0001-transient_shaper.patch` (6 files, 642
  insertions, pure adds), git-am pre-flighted clean onto HEAD `ec587a7`
  (→ efd9c62 in the pre-flight). `ui/app.py` left untouched — `speed` uses the
  text-box fallback; the optional fast/med/slow combo is deferred (see TODO).
- **Example.** `examples/transient_shaper_snap.json` — clocked saw pluck
  (LFO→schmitt→AD→VCA) → shaper (`attack` +0.8, `sustain` −0.3, fast) with
  source levels backed off for the transient boost (speaker peak ~0.77).

---

## 2026-07-05 — noise_gate (hold-and-hysteresis downward gate + gate CV)

Shipped the noise_gate ("Effects"): the inverse of the compressor. While the
detector sits above `threshold` the signal passes; when it falls away the
gate closes and ducks the output to the `range` floor. Ports: `in`,
`sidechain` (normalled to `in`), `out`, and `open` — a 0/1 gate CV that's
high while the gate is open, a free gate-extractor for driving an ADSR / VCA
/ clock off an audio signal's dynamics. Params: threshold −80..0 dB (−45),
hysteresis 0..24 dB (4), attack 0.1..50 ms (1), hold 0..500 ms (40), release
5..2000 ms (150), range −80..0 dB (−80 = full mute, higher = expander-ish).

Signal path: an instant-attack / one-pole-release peak follower on the
rectified key, then a Schmitt (open above `threshold`, close only
`hysteresis` dB below — the two-threshold trick the schmitt module uses)
with a hold timer that keeps the gate open a minimum time after the level
dips below the close threshold, then a target gain (1 open / `range` floor
closed) smoothed by the asymmetric attack/release one-pole, then a multiply.

The whole control chain — detector, Schmitt, hold countdown and gain
smoothing — runs in one per-sample voice loop (vectorized across voices,
serial in time, like the limiter's release envelope). Because every stage is
a plain recurrence with its state carried exactly across blocks, the render
is **block-size independent AND bit-exact** — no reassociation, so a
big-block render equals a many-small-block render to the last bit (tested
15360 vs 512 vs 128 vs 300; out and open both `array_equal`). A step up from
the compressor/limiter, whose vectorized gain solves match only to float
round-off. `threshold` at its −80 floor short-circuits to a bit-exact
passthrough (always open, open=1) — the documented neutral.

Voice-aware per the v0.4 convention: `(F,)` or `(V,F)` in with per-voice
detector/Schmitt/hold/gain state; a mono sidechain broadcasts across voices,
a `(V,F)` sidechain keys each voice; single voice row bit-identical to mono
(`_gate_align_key` mirrors the compressor's sidechain alignment).

25 tests in `tests/test_noise_gate.py` (model/kind walls, neutral
bit-exactness, gating + range floor mute/partial-duck, hysteresis
anti-chatter — 149 gate transitions at hysteresis 0 vs 1 with a 12 dB band,
hold timing to ±5%, open matches the audible gating, sidechain
external/normalled/silent-key, block-size bit-exactness, single-voice ≡ mono
+ independent voices, osc→gate→speaker and open→vca integration). Sandbox
suite 1374 passed + 18 mido skips (your venv runs 0), plus the two dearpygui
UI test files that need a display.

UI: bounded param sliders (dBFS/dB/ms) next to the limiter block. pyo: silent
stub, added to the TYPE list. Example `examples/noise_gate_chop.json` — a
unipolar 2 Hz LFO pushes a saw's amp over the threshold twice a second so the
gate pulses it (auto-tremolo / trance chop); speaker peak ~0.4, headroom rule
respected. Docs: MODULES.md index row + catalogue entry.

Deviation note (working agreement): built off-mount via clone + patch per
protocol. FLAG — from the sandbox the mount's *working-tree* copies of
numpy_backend.py / app.py / __init__.py read as truncated (numpy_backend 6917
lines vs 7587 at HEAD, ending mid-docstring): the uncommitted
resampler-window / dsp-load / doc WIP looked corrupted, or the mount was
truncating large-file reads. Committed HEAD (c642059) is intact — the clone
built and the full suite passed off it — so this patch is against HEAD and
excludes the drifted doc files. Verify the working copy before applying.

---

## 2026-07-04 — limiter: brickwall lookahead peak limiter (the "demo can't clip" module)

Second dynamics module after the compressor, and a different animal: where
the compressor eases gain by a ratio around a threshold, the limiter is an
absolute wall. Ports `in` → `out`; params `ceiling` (−20…0 dBFS, −1),
`release` (20…1000 ms, 80), `lookahead` (1…10 ms, 5). Category Effects.

**DSP.** Instantaneous target gain `t = min(1, C/|x|)` (C = 10^(ceiling/20))
→ slope-limited lookahead anticipation → one-pole release → a final
per-sample clamp to `C/|x|` → multiply into the audio delayed by the
lookahead. The anticipation is the interesting bit: `A[i] = min_j(t[i+j] +
j/L)`, a linear ramp of slope 1/L that starts up to L samples before a peak
and lands exactly on it — no hard corner, and provably `A ≤ t` so the wall
holds. Computed as a reversed running min (`minimum.accumulate` of
`t' − q/L`, add back, reverse) — fully vectorized, no per-sample attack loop.
The release is instant-attack / one-pole-release on the gain *reduction*
(`1 − A`), reusing the compressor's per-sample voice loop
`_audio_to_cv_loop_voice` (instant attack breaks the vectorized solver's
`a = 0` algebra, so it takes the loop by design). A last `min(g, C/|x|)`
clamp keeps the ceiling hard to the last ULP regardless of envelope
round-off.

**Latency.** Fixed `L = round(lookahead_ms·sr/1000)` samples (≥ 1), carried
as a per-voice delay line + release state, so it's constant across block
sizes and a parallel path can be compensated by the same amount.

**Invariants.** Under the ceiling the gain is identically 1.0 and the output
is a bit-exact delayed passthrough (short-circuited, resampler-unity
precedent). Shape-polymorphic — `(V, F)` limits per voice with no cross-voice
ducking, a single row bit-identical to mono. Block independence is exact for
the latency and the neutral path; the limited signal matches a single
big-block render to float round-off (the anticipation's `+q/L` min-scan
reassociates, same class as the compressor's gain solve) — pinned at
atol 1e-6.

**Tests.** 25 in `tests/test_limiter.py` — model/kinds, brickwall on impulse
trains / 0 dBFS squares / hot sines / hot noise across ceilings (and under
small blocks), neutral bit-exact delayed passthrough, latency = lookahead
samples and constant across block sizes, release reaches 1−1/e in the nominal
time (and longer release recovers slower), big-block ≡ small-blocks,
single-voice ≡ mono + independent voices, osc→limiter→speaker integration.
Full suite 1408/0 in the sandbox (dpg/mido/rtmidi/ffmpeg installed;
sounddevice absent but guarded — no skips).

UI: param-panel block next to the distortion block (ceiling dBFS slider,
release ms drag, lookahead ms slider). pyo: silent stub, added to the TYPE
list. Example `examples/limiter_brickwall.json` — two `saw`s a fifth apart
(amp 0.5 each, headroom rule) → combiner → limiter (−1 dBFS); the summed
peaks land exactly on the wall. Docs: MODULES.md index row + catalogue entry.

---

## 2026-07-04 — compressor: feed-forward dynamics + external sidechain

The rack's first dynamics processor (`compressor`, CATEGORY "Effects").
Feed-forward: a detector watches a key signal, and above `threshold` the gain
is pulled down by `ratio` (1 = off, 20 ≈ limiter), with `attack`/`release` on
the gain, a soft `knee`, make-up `gain`, and `mix` for parallel compression.
Ports: `in` + `sidechain` (audio, key **normalled to `in`** when unpatched —
plug a kick in to duck a pad), `threshold_cv` (cv), → `out` (audio) + `gr`
(cv, applied gain reduction as `applied_gain − 1`, 0..−1).

Path (one `(V, F)` core; mono is the V==1 case, so a single voice row is
bit-identical to mono): detector → level → dB → soft-knee gain computer (log
domain) → attack/release smoothing of the *gain reduction* → linear multiply
of `in` + make-up + parallel mix. Zero latency, so `mix` needs no delay comp.

Reuse: the reduction envelope rises when compression deepens (→ attack) and
falls when it eases (→ release) — exactly the follower's "attack where the
target rises above the state" one-pole, so the smoother is one call to
`_audio_to_cv_block` (reduction ≥ 0 satisfies its no-cancellation
precondition), loop kept as the degenerate fallback. RMS detector (~10 ms) is
a one-pole on key² via `lfilter` + carried `zi`; `peak` is instantaneous.
Both carry per-voice state → block-size independent (bit-exact detector,
<1e-6 gain solve). Neutral (`ratio=1 ∧ gain=0 ∧ mix=1`) short-circuits to a
bit-exact passthrough, detector skipped. Gain computer
(`_compressor_reduction_db`) is the standard soft-knee law, C1-continuous at
the knee edges, hard hinge at `knee=0`.

22 tests (`tests/test_compressor.py`); sandbox suite 1365 + 18 mido skips. UI:
`detector` combo (peak/rms). pyo: silent stub. Example
`examples/sidechain_pump.json` (clock→AD→VCA kick ducks a saw pad, headroom-
safe, peak ~0.2). Docs: MODULES.md index + catalogue + `threshold_cv`
conventions row.

Flagged for review: `gr` = linear `applied_gain − 1` (one reading of "0..−1
scaled from dB"); `threshold_cv_depth` default 12 dB/unit (spec gave the unit,
no number). Stretch (lookahead, program-dependent release, `ratio_cv`)
deferred. Delivered via clone + patch per protocol; WORKLOG/TODO handed over
as paste-in snippets because the working tree was mid-compaction.

---

## 2026-07-04 — Module ideas backlog written

Brainstorm session (all areas, Matthew's pick): wrote **docs/MODULE_IDEAS.md** —
~26 detailed module specs (ports/params/DSP/tests/effort, each standalone as a
work item for a future session/agent, shared submission preamble at top) plus a
quick-hits list. Families: dynamics (compressor/limiter/noise_gate/transient
shaper — biggest gap, follower core = audio_to_cv fixed-point technique),
pitch/freq (ring_mod, freq_shifter, bitcrusher), character/space (tape,
convolver), CV tools (quantizer, slew, pitch_detector), generative
(shift_random, euclidean, clock_divider, bernoulli, burst, arpeggiator, chord),
voices (fm_op, pluck, modal, granular, drum trio), visual (scope, spectrum).
Suggested first five: compressor, scope, quantizer+shift_random, fm_op,
convolver. TODO.md gained a pointer line. Docs-only change — no code touched;
uncommitted on Matthew's tree (commit is his to run).

> **Worklog gap (noted 2026-07-10):** the 2026-07-06..07-09 sessions
> (buffer-size slider, window persistence, error-handler integration,
> pitch_shifter phase-coherent mix, scroll-to-adjust + Ctrl-zoom debounce)
> shipped and are committed, but were logged in TODO.md + auto-memory
> rather than here. Not back-filled yet — offered to Matthew.

## 2026-07-03 — audio_to_cv: per-sample follower loop vectorized

Matthew asked for the audio_to_cv per-sample loop vectorization (the TODO
line had it filed as "genuinely recursive — not run-splittable like the
ADSR was", cold path until a follower-heavy patch profiles hot).

The recurrence picks attack vs release by comparing the input against its
own evolving state, so it isn't one lfilter call. But each step is
max(comboA, comboR) of the previous level when attack >= release (min when
inverted), which buys two exact-arithmetic facts: any fixed attack/release
pattern solved as a linear time-varying one-pole brackets the true
trajectory from one side, and re-deriving the pattern from a solved
trajectory iterates monotonically INTO the true one — a self-consistent
pattern is the exact solution, not an approximation. The fixed-pattern
solve vectorizes as y = P*(l0 + cumsum(b/P)) with P = cumprod(1-c): all
terms nonnegative (rectified input), no cancellation; cumprod underflow
bounded by a chunked variant + a coefficient guard (> 1-1e-4, i.e. time
constants under ~0.1 samples, incl. the ms<=0 instant clamp) that takes
the old loop instead. Convergence typically 2-6 rounds (spike: mean ~4,
p95 12, max 18 at 0.01 ms attack on a 110 Hz sine; cap 24). One wrinkle:
plateaus (DC, saturated bursts) can flip razor-tie comparisons forever
while values sit still — solved by also accepting two consecutive
trajectories equal after the float32 cast.

Both shape branches now run the shared block solve on (V, F); the original
loops survive verbatim as _audio_to_cv_loop_mono/_voice, the fallback for
degenerate coefs / non-finite input (NaN semantics stay the loop's) /
hypothetical non-convergence. Spike + suite: bit-identical to the old
loops after the float32 cast on every tested signal x coefficient pair
(documented drift class is the ADSR-rewrite's — float64 reassociation
below float32 resolution; tests pin < 1e-6). In-repo timing (sandbox,
F=512): mono ~102 -> ~86 us/blk (1.2x, renderer overhead dominates);
16-voice ~1.27 -> ~0.33 ms/blk (3.9x, 10.9% -> 2.8% of the 11.6 ms
budget — the voice loop was the actual target).

Tests: 35 new in TestAudioToCVBlockEquivalence — verbatim old loops as
oracles (filter-slice convention): 5-param x 5-signal mono grid + 5-param
mixed-content 16-voice grid chained over 8 blocks, frames=1 chaining,
split-vs-whole continuity, block-path-engages regression guard,
instant-attack fallback correctness, state-key shape compatibility.
Suite: 1292 in-sandbox (was 1257), zero skips.

## 2026-07-03 — fader_seq: the sequencer with a fader-bank panel

Matthew asked for "the sequencer horizontalised": up to 16 notes on
vertical sliders, minimalist, only step numbers and a tickbox beneath —
and wanted a better name than his. Picks via question dialog: name
**fader_seq**, faders **±12 st**, **integer-semitone** quantization.

New module type (original untouched, house precedent cv_gates/cv_keyboard),
but engine sharing is by CONTRACT rather than copy: fader_seq publishes the
exact same param names (steps / step{i}_pitch / step{i}_on) and ports, its
DEFAULT_PARAMS are imported from sequencer._default_params (can't drift),
and the numpy backend routes both TYPEs through the one _render_sequencer
(state is per-module-id, so siblings step independently). pyo silent stub.

UI: node build short-circuits the per-param loop for fader_seq and draws
_build_fader_seq_panel instead — one labelled `steps` slider, then a
horizontal group of 16 columns: 18x96 px vertical slider_int (±12,
format="" so no in-slider text), step number, label-less checkbox. Hover
tooltip per fader shows "+7 st (G4)" (updated live by _on_fader_pitch,
which writes the param as float to keep the JSON shape identical to the
original). FADER_RANGE_ST lives in the module (dpg-free) so tests pin it.

Tests: 8 new in tests/test_fader_seq.py — param/port contract equality
against Sequencer, default scale fits the fader range, serialization
round-trip, BIT-IDENTICAL A/B render vs sequencer (same params + clock),
independent per-module state, rest/reset smoke through the fader_seq TYPE.
Suite: 1239 sandbox (+18 mido skips) expected, was 1231.

Docs: MODULES.md table row + section after `sequencer`. No example patch
yet — sequencer_melody.json applies verbatim (swap the type) — candidate
follow-up if wanted.

## 2026-07-03 — MIDI device-refresh button + per-key velocity calibration

Matthew asked for both remaining MIDIInput quality-of-life items in one go
(both were already on the TODO). Design choices confirmed via question
dialog: learn-dialog-plus-editable-table, normalize to the mean of captured
keys, refresh button on BOTH midi_input and mic_input.

**Refresh devices (UI-only).** The device combo on midi_input/mic_input now
carries an explicit tag (`device_combo_{id}`) and a Refresh button beside
it. `_device_combo_items` (shared helper) rebuilds the item list —
AUTO_DEVICE first, current selection always kept even if the device is
unplugged — and the button reconfigures the combo in place. Selection is
untouched, so nothing recompiles until the user picks from the fresh list.
Status bar reports the device count.

**Velocity calibration (model + UI).**
* Model: new `velocity_curve` param on midi_input — `{str(raw midi note):
  multiplier}`. String keys are canonical (JSON object keys are strings);
  `__init__` canonicalizes int keys from hand-built params. Applied in
  `note_on` after 0-127 normalization, clamped back to [0, 1], keyed by the
  RAW note (physical key, pre-octave_shift — calibration corrects the
  keybed, not the transposed pitch).
* Learn mode: `start/stop/snapshot_velocity_capture` on the module, a
  capture dict under the existing MIDI-state lock. note_on records the raw
  PRE-CURVE velocity while capturing (idempotent re-learning) and the note
  still plays. `stop_midi()` clears any in-flight capture.
* `compute_velocity_curve(samples)` (module-level, pure, dpg-free): per-key
  means → target = mean of means → multiplier = target/mean, rounded to 4
  dp. Quiet keys boosted, hot keys tamed; clamping happens at apply time.
* UI: "Calibrate keys..." button on the node (plus an "N keys calibrated"
  label). Non-modal dialog — learn mode needs the user playing while it is
  open. Learn/Stop toggle stashes the capture; Compute merges fresh
  multipliers into the existing curve (merge, not replace — Clear all is
  the from-scratch path); table row per key: note name, drag-float
  multiplier (0..4), remove button. Per-frame `_update_velocity_capture`
  ticks a "learning: N keys / M hits" readout.
* Base-class fix: `Module.__init__` shallow-copied DEFAULT_PARAMS; a
  dict-valued default would have been SHARED across instances. Now dict
  values get a fresh copy per instance (comment updated — first dict param
  in the codebase).

Tests: 23 new in test_midi_input.py (curve application incl. raw-note
keying under octave_shift + unity clamp + serialization round-trip +
no-shared-default; capture semantics incl. pre-curve recording and
snapshot copies; compute maths). UI paths (dialog, refresh) follow the
house convention of not unit-testing dpg wiring.

Docs: midi_input section in MODULES.md promoted from "_To document._" stub
to full ports/params tables + calibration walkthrough; mic_input device row
updated for the refresh button. Both TODO items moved to TODO-ARCHIVE.

## 2026-07-03 — Idea dropped: split-keyboard 2-player mode

Matthew dropped the logged split-keyboard 2-player idea (2026-07-01):
two people can already share the existing keyboard pipeline (cv_keyboard /
cv_gates) by simply agreeing on which keys are whose — no dedicated split
mode or split-point param needed. Removed from TODO.md (verified, 73-byte
delta) and marked DROPPED in memory. Not to be re-proposed.

## 2026-07-03 — CORRECTION: the FilePlayer 'working-tree drift' was a phantom

Retraction of the audit FOUND above. Matthew's Select-String shows
`class StreamingDecoder` at media.py:108 on the real filesystem — his
working tree was correct all along. The sandbox mount was serving STALE
per-file reads: media.py appeared as the 3.7 KB pre-streaming version
(and not even byte-faithful to any committed version — a corrupted read,
not a clean stale snapshot) while numpy_backend.py
read fresh in the same directory. The 'corrupt index' error was more of the
same. Both git restores I handed over (ca8ef8a's and f2fa75e's) were
therefore no-ops, not failures.

Lesson recorded in memory: never diagnose repo/working-tree state from
sandbox reads of this mount — phantom diffs are indistinguishable from real
drift on this side. Verify on the Windows side (Select-String / git status
there) before raising an alarm. Doc edits remain trustworthy because each
write is read back and verified in the same session.

State after this session: ca8ef8a (housekeeping) + f2fa75e (TODO compaction:
TODO.md 459→100-line live list, TODO-ARCHIVE.md 436 lines verbatim) both
committed and pushed by Matthew. No open hand-offs.

## 2026-07-03 — TODO compacted into live list + archive

Matthew approved the compaction offered in the housekeeping audit. TODO.md
(459 lines, ~350 of them [x]) split programmatically, conservation-checked
(every non-blank line lands in exactly one output, same multiplicity):

- TODO.md (100 lines): open items only — the Filter-vectorization block
  (slices 5/6 still open, shipped sub-slices kept in place for context),
  the audio_to_cv per-sample loop, and the six polish one-liners — plus a
  new "Follow-up threads extracted from archived entries" section hoisting
  the recurring open threads (resampler window/AA/seam-search, pitch_shifter
  vectorize/transients, S&H slew/T&H/noise-normal, meter round 4, FilePlayer
  ffmpeg hint, exe slimming, 2-player idea) so the live list stands alone.
- TODO-ARCHIVE.md (436 lines, new file): v0.1–v0.4 eras, all shipped
  Later/wishlist entries, and the CV-coverage section, verbatim with their
  per-entry follow-up notes.

Also: commit ca8ef8a landed but its accompanying git restore did NOT —
media.py in the working tree is still the 3.7 KB pre-streaming version
(mtime 2026-06-30) vs HEAD's 11 KB StreamingDecoder blob. Restore re-handed
to Matthew with a Select-String verification step. (Sandbox note: the
Windows-git index now uses an extension the sandbox git can't parse, so
index-dependent git — status/diff-against-index — is unusable from here;
object reads like log/show still work.)

## 2026-07-03 — Housekeeping audit (docs + repo state)

Doc sweep after the FilePlayer push: WORKLOG, TODO and MODULES.md are all
current through streaming + transport (transport params documented, index
row present, TODO items ticked). Two fixes and one find:

- TODO.md: removed the stale open item “Reverb / mixer CV (lowest
  priority)” — superseded by the DONE entries directly above it
  (decay/damping/mix_cv + per-channel gain{i}_cv, shipped 2026-07-02).
- .gitignore: added pytest-cache-files-*/ ; the sandbox pytest cache dir
  pytest-cache-files-ow898o_9/ is TRACKED and needs git rm -r --cached
  (Matthew's side).
- FOUND: the mount working tree carried pre-streaming versions of the five
  FilePlayer-commit files (media.py, numpy_backend.py, fileplayer.py,
  app.py, test_file_player.py) — git diff showed −625 lines vs HEAD 39e8e6c,
  i.e. StreamingDecoder + transport missing from the checkout while
  HEAD == origin/main is correct. Running the app from this folder would run
  the OLD FilePlayer. Handed a targeted git restore to Matthew (targeted so
  the doc edits above survive).

Compaction verdict: WORKLOG is 5.5k lines (append-only history — fine);
TODO still carries every completed v0.1–v0.4 item (~350 of 460 lines are
[x]) — offered to archive completed eras into a TODO-ARCHIVE.md if wanted.

## 2026-07-03 — FilePlayer: off-thread streaming decode + tape transport

The last audible wart: pointing a FilePlayer at a big video ran the whole
ffmpeg decode synchronously INSIDE the first audio render — seconds of
stalled audio thread. Now decode never touches the audio thread.

`media.StreamingDecoder`: daemon worker fills a growing (2, N) buffer —
scipy WAV fast path decodes whole-file in the worker; everything else
streams from ffmpeg's stdout in 256 KiB chunks. `frames_ready` is
published only after each chunk lands (int/reference stores are atomic
under the GIL), so the render side needs no lock: read the watermark,
slice below it. `close()` kills a decode in flight; `wait()` is the
test/offline hook.

Renderer is consume-only: decoders are kicked at compile() (UI thread) or
on a live path edit (a thread spawn, safe in the callback). Playback
gates on ~0.5 s prebuffered (moot once done); catching the writer holds
the playhead — partial block, silence, resume without skipping; loop
wraps only once the total is known. compile() closes decoders of dropped
modules (the disk_writer pattern); stop() keeps them — decoded audio
survives transport stops.

Tape transport (Matthew's pick over CD-style): new `playing` param
(default true, old patches unaffected) — Stop pauses holding position,
Play resumes, |< rewinds via backend.rewind_file_player() (a seek flag
consumed at block start, the reset_meter_clips pattern) working playing
or paused. Buttons live on the node next to the playhead readout; the
readout's total now grows with the decode watermark = free loading bar.

Tests: existing file-player/media tests adapted to the async contract
(wait_for_file_decodes after compile); 11 new (transport x5, streaming
x6 incl. real-ffmpeg byte-identity of chunked vs one-shot decode and
close-kills-decode). Suite 1206 sandbox. decode_with_ffmpeg stays for
one-shot uses; _decode_audio no longer has render-path callers.

## 2026-07-03 — Add-module menu grouped into submenus

The flat Add-module list had reached 47 entries; Matthew asked for groups.
Chosen scheme (over mirroring the docs' signal-flow taxonomy, whose
"Processor" bucket alone held 17): seven browsing-sized submenus —
Sources / Filters & EQ / Effects / Modulation / Routing & VCA /
CV & Utilities / Outputs — biggest is 9 items.

Mechanism: each module class declares `CATEGORY` (new ClassVar next to
TYPE, base default "Other"); `core/module.py` gains `CATEGORY_ORDER` and
`grouped_module_types()` (known categories in fixed order, unknown ones
appended alphabetically, names sorted within groups, empty groups
skipped). app.py's menu loop is now two nested `dpg.menu`/`add_menu_item`
loops — a forgotten CATEGORY lands in a visible "Other" submenu rather
than vanishing.

Docs: MODULES.md index Category column now names the menu submenu (note
added; detailed sections keep the signal-flow organisation), the
Adding-a-new-module recipe includes CATEGORY, and the index gained the
missing `phaser` row (drift — it was never added when phaser shipped).
5 new tests in tests/test_module_categories.py (partition, order,
inner sort, oddball-to-Other).

## 2026-07-02 - Meter round 3: LUFS-ish modes + stereo link + clip counter

Matthew's fresh meter set, scoped by three questions: stereo link =
"master readout" (bars stay per-channel, tick/lamp/number merge),
LUFS = BOTH momentary and short-term, clip counter = events with
recompile+click reset.

LUFS: `lufs_m` (400 ms) / `lufs_s` (3 s) join METER_MODES. K-weighting
is two plain RBJ biquads - the existing `_filter_coeffs` highpass at
38 Hz Q 0.5 and `_loud_shelf` +4 dB at 1681 Hz - cascaded in
`_meter_kweight` with exact zi carry (fixed coeffs, so no DF-I
gymnastics needed). Mean-square EMA per mode, displayed as
-0.691 + 10*log10(msq) mapped through the existing linear->dB bar
pipeline (bar value = 10^(LUFS/20), so the -90..0 scale just works and
the text unit flips to "LUFS"). Anchor: full-scale 997 Hz sine reads
-3.27 vs the spec's -3.01 - the RBJ approximation's tenths, hence the
honest -ish. 60 Hz reads 3.4 dB under 997 (bass discount), 6 kHz 3.5
over (presence). Voice buffers: loudest voice wins, mirroring rms.

Stereo link: `stereo_link` param (default False = today's behavior,
and a no-op without `in_r`). Per-channel detector state is untouched;
the merge happens at publish: hold = pair max, clip = pair OR, and the
readout is the louder channel in peak/rms but the CHANNEL-ENERGY SUM
in the LUFS modes (per-channel linear levels are 10^(LUFS/20), so the
combined value is just root-sum-square - two identical channels read
+3.01 dB, asserted to 0.1).

Clip counter: counts EVENTS on the raw signal in every mode - one
unbroken run of |x| >= 1.0 is one event; rising-edge count with the
run state carried across block boundaries (a spanning run counts
once); voices collapse to any-voice-over per time position. Published
as the 4th channel-tuple field; resets on recompile (compile() zeroes
the tallies) and via the new `reset_meter_clips(mid)` GUI hook (takes
the backend lock). UI: count rides next to the lamp as "xN" (hidden at
zero), and clicking the meter row zeroes it via an item-clicked
handler on the drawlist.

Published snapshot grew: outer (left, right, linked, mode, pair_level),
channels (level, hold, clip, clips). compile()'s pre-created zeros and
the GUI updater track the new shape; the UI hides the R row's text when
linked and shows the summed pair tally on the L row.

Sandbox gotcha worth remembering: the first meter-test run PASSED
against last session's stale `pip -e` install (pytest imported
/tmp/psverify_*), silently ignoring every edit in the fresh clone -
re-`pip install -e` the current clone before trusting green.

18 new tests (8 LUFS + 5 link + 5 counter) in tests/test_meter.py;
existing helper/tuple-shape tests updated for the wider snapshot.
Suite 1190 sandbox (+18 mido). MODULES.md meter section + param rows;
TODO logs a possible round 4 (gated integrated LUFS, LRA, true-peak).

## 2026-07-02 - PitchShifter trio: accuracy fix + deep-bass grains + formant preserve

Matthew asked for the three logged pitch_shifter follow-ups in one go.
Diagnosis first, and it re-scoped the job: the "~12 cents sharp at +12
on a pure sine" was the mild face of a real WSOLA defect. The engine
accumulated the similarity-search offset into the analysis pointer
(`a = sidx + Ha`), and on periodic input the offset settles to a
CONSTANT alignment residue (up to half a period), so input was consumed
at `Ha + d0` per grain instead of `Ha`. Two failure faces, measured:
over-consumption throttles production against the write head and the
resample clamp inserts micro-holds (the -12 ct pull at 440 Hz/50 ms/ov2
- and it IS flat, not sharp; the old argmax-bin measurement flipped the
sign); under-consumption walks the pointer out of the ring and
production DEADLOCKS - output dies to DC. Dead configs found: 100 ms
grain @ +12, 440 @ +5 st, 55 Hz @ +/-12. Nobody had noticed because the
shipped example (saw, +7, defaults) sits in a residue sweet spot.

Fix per the canonical WSOLA formulation: the analysis pointer lives on
the IDEAL FLOAT GRID (`a += Hs/r`, never absorbing d0), so search
excursions can't accumulate - exact consumption, no starvation, no
deadlock, and the integer-hop rounding error at fine cents settings
goes with it. On top: parabolic NCC-peak refinement + fractional grain
extraction (both grain and search target read at sub-sample positions),
making joins phase-continuous to sub-sample accuracy. Measured matrix
after the fix: WORST case -0.26 ct (55 Hz octave-down); every previously
dead config sustains at full level. Scoped with Matthew via
AskUserQuestion; both recommendations taken (LPC formants, auto grain).

Deep bass: `_detect_period` (module-level) - unbiased FFT autocorr,
INTERIOR local peaks only (the ACF of a low tone is still ~0.95 at
lag_min, so a threshold scan locks onto that shoulder - found the hard
way), smallest peak within 90% of best (subharmonic-proof), parabolic
refine, None for noise/silence. Every 2048 input samples the core
re-estimates; if the working grain holds < 2.5 periods it rebuilds the
engine at 2.5P (user grain_size = floor, 150 ms cap, 20% hysteresis),
primes it from the old ring via the new `history()` accessor and
equal-power crossfades over one block; per-voice `regrains` counter is
the observable. 35 Hz +12: one regrain to 3124 samples, +0.12 ct.
Normal material never regrains.

Formant preserve: `formant_preserve` bool (default False = bit-legacy
path). Order-24 Levinson-Durbin LPC (`_lpc_coeffs`, module-level) on
the raw history - Gaussian 60 Hz lag window + white-noise floor +
reflection-coefficient clamp for guaranteed stability; whiten input
through A(z) (FIR, zi carried), grain-shift the residual, re-color
through 1/A(z) with the coefficient set from ~one grain AGO (a small
FIFO aligns the envelope with the content it described). The engine
grew a parallel raw ring (`db`, fed via `process(..., x_dry=)`) so the
dry mix tap stays true raw - asserted bit-equal to the formant-off dry.
Two hard-won guards: (1) envelopes estimated before the wet path primed
describe the onset transient, not content - feeding them to the
synthesis IIR blasted the first wet block to full scale (found via the
example's 1.0 startup peak); pre-priming estimates are now dropped.
(2) a 4x-raw-RMS safety valve bounds any ill-conditioned estimate.
Synthetic-vowel proof (110 Hz pulses through 800+2400 Hz resonators,
+7 st): first-formant centroid 942 Hz preserved vs 1173 Hz migrated
(input 794). St=0 level ratio 1.002.

18 new tests (7 accuracy + 4 deep bass + 7 formant) -> suite 1172
sandbox (+18 mido). Perf 0.30 ms/block off / 0.70 on (budget 11.6).
Example `pitch_shifter_formant_vowel.json` (peak 0.097 after taming the
resonant-bell headroom trap AGAIN - square 0.09 + bells +14/+10 dB Q8 +
speaker 0.6). UI formant_preserve checkbox; MODULES.md updated.

Still open on this module: vectorize the per-voice NCC if profiled hot;
transient detection to sharpen attacks. The WSOLA-style seam search for
the RESAMPLER (logged yesterday) could now share `_detect_period`.


## 2026-07-02 - Resampler declick + dry/wet mix (follow-up pair ships)

The two resampler follow-ups flagged since 2026-06-30, scoped with
Matthew via two questions (both recommendations accepted): loop-seam
declick = **event-jump crossfade** (not the always-on dual-head
harmonizer, which would have combed unity), dry tap =
**latency-compensated** (pitch_shifter precedent, coherent blend).

**Declick.** The seam in a full-ring varispeed is the read head
colliding with the write head (pitch up) or falling off the oldest
sample (pitch down) - the hard `mod span` wrap butted audio ~0.2 s
apart together, a click per wrap. A same-buffer dual tap can't fix it
(offset `span` aliases to one sample away mod L), and crossfading *at*
the collision has zero runway, so the fix triggers **early**: a guard
band (`max(6% of window, fade+8, block+8)`, capped span/3) near both
edges; when the head drifts in it jumps **half a span** back toward
the centre, equal-power crossfading old->new tap over
`_RESAMP_XFADE_SEC` = 8 ms (auto-shortened to the old tap's runway
`(span-1-p)/rmax` at extreme up-ratios). Far from the edges the legacy
single-tap path runs **bit-identically** (the mods are no-ops
in-band), so unity stays a bit-exact delayed passthrough and no event
ever fires there - the fast/slow fork is per-block, slow only when a
fade is in flight or a voice is inside the band. Slow path is
per-voice but numpy-vector within the block; fades carry across block
boundaries via `xf_rem/xf_len/xf_off` state (weights are a function of
sample-index-within-fade, so a fade split over five 64-sample blocks
is seamless - tested). Seam events are >= half a span of travel apart
so fades never overlap; each bumps a per-voice `seam_jumps` counter
(the test observable). Old tap running out of content mid-fade is
force-completed at ~zero weight. Perf: 0.088 ms/block unity (fast
path, unchanged), 0.102 ms/block at +12 st through seams - noise.

**Mix.** `mix` (0..1, default 1.0 = wet-only, bit-identical to the
pre-mix render via a skip branch). The dry tap is the **same ring
buffer** read at the fixed init delay - no second buffer, and at unity
ratio it's the exact samples the wet tap reads, so `mix` sweeps
coherently: mix=0.5 at unity is *bit-equal* to full wet (0.5x+0.5x),
and mix=0 is the delayed dry passthrough, bit-equal to a unity render
even with the pitch cranked (both tested). The detune/thicken use case
from the module docstring is now one module: +12 ct at 50% mix.

14 tests (8 declick + 6 mix) in the two new classes in
`tests/test_resampler.py`; suite **1154** passed sandbox (+18 mido
skips), +14 from 1140. Voice row stays bit-identical to mono through
seams and mix. Example `examples/resampler_detune_blend.json`; UI mix
slider on the resampler block; MODULES.md param row + declick/mix
paragraphs + example index line.

Known limit kept deliberately: a blind crossfade can pass through a
brief anti-phase amplitude dip on a pure tone (equal-power handles
uncorrelated content; correlated-opposite is the worst case). The fix
is a WSOLA-style seam-position search - logged in TODO as the natural
next resampler step.


## 2026-07-02 - motion_eq per-band Q CV (the set completes)

Matthew took the flagged follow-up same-day: `band{i}_q_cv` x4, and the
motion_eq's per-band CV set is complete - freq, gain, Q, twelve CV
jacks on one EQ.

Design call (house rule: natural unit per domain): Q is ratio-like
(0.1-20 spans two decades), so additive CV would mean one CV unit is a
tickle at Q 10 and a catastrophe at Q 0.5. The natural unit is a
**doubling**, i.e. the freq-sweep convention: `q_i * 2^(q_cv_depth *
mean cv)`, block-meaned, one shared `q_cv_depth` (default 1.0 - a
bipolar LFO at full depth sweeps half-to-double). No new clamp code:
the result rides `_peq_coeffs`'s existing (0.1, 20) Q clip, the same
rail the static param rides - which makes both clip-rail tests exact
bit-identical equivalences rather than approximations.

Implementation is the third verse of the same song: `qs_override`
joins `freqs_override`/`gains_override` on
`_render_parametric_eq_mono/_voice` (None = bit-identical), and
`_render_motion_eq` builds all three override lists. 14 tests in
`tests/test_motion_eq_q_cv.py` (power-of-two exact equivalences, depth
scale/disable, both rails, alternating +/-1 block-mean proof, band
independence, skirt-tone attenuation >40% when a CV narrows Q 0.7 to
11.2, voice row == mono). Port/param exact assertions updated (15
params, 13 input ports).

Example `motion_eq_focus.json`: 110 Hz saw drone, two +8 dB bells at
500/2000 Hz whose Q two slow LFOs sweep 0.625-10 - the broad tone-shape
periodically snaps into vocal, formant-like stings. First cut peaked at
2.9 (two wide +9 dB bells overlap -> +18 dB, the classic headroom
trap); tuned to amp 0.2 / +8 dB / base Q 2.5 / depth 2.0 -> peak 0.73
at the EQ, 0.44 at the speaker.

MODULES.md: ports/params rows, conventions-table row
(motion_eq.band{i}_q_cv | 1.0 shared | Q doublings), index row now says
x4 per family. UI: q_cv_depth drag 0-4 "%.2f dbl/unit".


## 2026-07-02 — Animated-EQ extras: motion_eq per-band gain CV + reverb damping_cv

Matthew's ask, and both are the "flagged for later" items off their own
ship notes: the motion_eq entry said "gain-CV per band remains a possible
future add", the reverb CV entry listed "damping_cv (tone of the tail)".

**motion_eq `band{i}_gain_cv` (×4).** Additive in dB — the tilt_eq
convention, since gain lives in dB — with a second **shared** depth knob
`gain_cv_depth` (default 6.0 dB/unit, tilt_eq's default; the freq CVs
keep their oct/unit `cv_depth`, one knob per unit domain per the house
rule). Block-meaned like the freq sweep, clamped ±24 dB (the knob
range) so a hot CV can't push a bell absurd. Implementation is the
freq-CV move repeated: `gains_override` joins `freqs_override` on
`_render_parametric_eq_mono/_voice` (None = bit-identical, peq suite
untouched), `_render_motion_eq` builds both override lists. Unpatched
band = exact static gain; nothing patched = still bit-identical to
ParametricEQ.

**reverb `damping_cv`.** The third safe macro, literally the decay/mix
pattern with s/decay/damping/: additive level units on the same shared
`cv_depth`, block-mean, clamp 0..1. Click-safe: damping only sets the
recirculation one-pole's coefficient, recomputed per block anyway, and
the filter state carries. (`size` stays the one no-CV param — line
lengths still click.)

Tests: 13 in `tests/test_motion_eq_gain_cv.py` + 7 appended to
`tests/test_reverb_mixer_cv.py`. The equivalence tests are dyadic-exact
bit-identical (6.0·0.5 in dB space, 0.25+0.5 in level space); the
block-mean proof is an alternating ±1 CV whose mean is exactly zero ==
no CV at all; clamp tests hit both rails; voice row == mono; tail-HF
fraction halves when CV drives damping up. Existing exact-shape
assertions updated (motion_eq 13→14 params + port list, reverb port
list in two files).

Example `motion_eq_breathe.json`: white noise → motion_eq with two slow
LFOs breathing bands 2+3 (gain_cv_depth 10 → ±10 dB swells) → reverb
whose damping a 0.05 Hz LFO sweeps 0.05..0.95 (the hall darkens and
re-opens). Peak 0.29 at the speakers — headroom respected.

MODULES.md: motion_eq + reverb sections, module index rows, and two new
rows in the CV-depth conventions table. UI: gain_cv_depth drag
(0–18, "%.1f dB/unit") in the shared EQ block; reverb cv_depth comment
now names all three targets. No pyo change (types already stubbed).

## 2026-07-02 — width_cv on the stereo speaker (same-day follow-up)

Matthew took the first follow-up straight off the list: the width knob
gets a CV jack. Small and by-the-book: `width_cv` is per-sample, shares
the module's existing `cv_depth` with `pan_cv` — the Reverb's paired-CV
convention (one shared depth, CVScale in front for independent
sensitivity) — and clamps to the same 0..2 as the knob.

One care point: the width==1 mid/side SKIP is what makes the sink's
defaults bit-exact, so the skip now keys on "width_cv silent AND width
== 1" rather than the knob alone. A patched-but-zero-depth jack still
takes the exact path; the moment a live CV arrives, the mid/side maths
runs per sample with a vector width. Mono mode ignores width_cv
entirely (no side content), same as the knob.

+8 tests → 31 in `tests/test_stereo_speaker.py`: constant-CV == static
width, shared-depth scaling (0.25 × 2.0 ≡ +0.5), zero-depth
bit-identical, clamp both ends (CV −5 collapses to mono, CV +10 caps at
2), a within-block ramp measurably growing the side (0 → doubled),
(V,F) CV averaging, mono-mode ignore. `stereo_field_pluck.json` gains a
0.06 Hz triangle "Width breath" LFO on the new jack — the image now
breathes while the autopan orbits.

Suite: 1104 in sandbox (+18 mido; 1122 on Matthew's mido-equipped venv).

## 2026-07-02 — StereoSpeakerOutput: the sink learns where things are

Matthew's pick after the nonlinear pair — the long-listed "stereo-aware
speaker (pan / width)", which the mono SpeakerOutput docstring has
promised since v0.1 ("a stereo variant arrives once the mixer module
exists" — the mixer arrived 2026-05-13; the promise is now kept, and the
docstring updated to say so). Scoped via AskUserQuestion: NEW sink (the
existing three untouched), pan_cv autopan input, constant-power pan law,
width 0–2 with over-wide allowed.

**Two source modes, decided by whether `in_r` is cabled.** Mono
(`in_l` alone): constant-power placement — θ = (pan+1)·π/4, source ×
(cos θ, sin θ). The invariant L²+R² == source² holds at every pan
position (tested at five). Stereo pair: width first as mid/side
(M=(L+R)/2, S=(L−R)/2·width) — SKIPPED entirely at width==1 so the
defaults pass a pair to the bus bit-exactly — then balance with a
cosine taper on the far side only (gL=cos(max(p,0)·π/2), unity at
centre, no centre attenuation on pairs). `pan_cv` is per-sample
(LFO = autopan), clamped at the rails after cv_depth scaling; a (V,F)
CV averages across voices (one global position, the Loudness
convention). Audio jacks sum their voice axis — the implicit-sum rule.

**Drain architecture.** The three mono sinks stay on the channel-flag
table; the stereo sink gets its own `_drain_stereo_speaker(module,
frames, buffers, patch, out)` called from render_block's speaker pass —
factored as a method so tests can drive it directly with crafted (V,F)
buffers (which is how the voice-sum and voice-CV tests work). Stateless,
so block-size independence is structural. Master ±1 clip unchanged,
shared with all sinks.

**Tests: 23 in `tests/test_stereo_speaker.py`.** One test-side gotcha:
the first stereo rig used `noise` as the R source — random per render,
so any cross-render comparison failed; swapped for a square oscillator.
Model walls, the constant-power invariant, hard-pan kills, balance
near-side-unity, width 0/2 algebra (side doubles, mid preserved),
mono-width no-op, constant-CV == static pan, clamping, voice sums, two
sinks adding, gain, master clip, uncabled silence.

**Example.** `examples/stereo_field_pluck.json` — pentatonic tri pluck
→ chorus (the pair straight into the sink), width 1.6, 0.22 Hz LFO on
pan_cv at depth 0.7 sweeping the whole voice around the room. Peak 0.49;
measured L-dominant and R-dominant blocks both present over two sweep
cycles (it really pans).

Suite: 1096 in sandbox (+18 mido) — +23 over the nonlinear pair.
UI: TYPE-guarded pan/width/gain sliders + cv_depth drag. pyo silent-stub.
Follow-ups logged: width_cv, node meters, pan-law selector.

## 2026-07-02 — Waveshaper (`waveshaper`): the wavefolder, stacked on the 4× plumbing

Second of Matthew's two-module nonlinear pair. Where the Distortion
flattens against the rails, this reflects off them — the Buchla/Serge
route from a plain sine to brassy/metallic/comb-like spectra. Built
directly on the previous commit's `_Oversampler4` + `_dc_block`
infrastructure, which is exactly why that landed as shared module-level
kit.

**Folds.** `triangle` = the periodic triangle function of
`u = fold·x + symmetry`, one vectorised `np.mod` — hard geometric
reflection that is the IDENTITY for |u| ≤ 1, so a centred fold=1 passes
a full-scale signal through exactly (measured 5e-5, just FIR ripple).
`sine` = `sin(π/2·u)` — smooth creases, colours even below the rails
(that's its character, documented as such). `symmetry` slides the wave
off-centre pre-fold → even harmonics (H2/H1 ≈ 7% at 0.4) with the DC
blocked; the blocker only engages while symmetry ≠ 0 so the exactness
guarantee above survives. Per-sample `fold_cv` (fold units, ZOH to 4×,
clamped 0..32); fold=0 collapses to silence (u = symmetry constant).

**Measured at fold 6 on a sine: H3 and H5 rival the fundamental**
(1.9× / 2.0×) while the output stays bounded (1.15 incl. filter
ringing) — the folder doing folder things.

**Tests: 23 in `tests/test_waveshaper.py`** — identity/silence edges,
fold richness + bound, mode difference, sine-curve formula match at low
fold, symmetry evens + DC, constant-CV == static fold, a
held-then-ramped fold_cv blooming H3 by >5× between halves, and the
same invariant battery as the pedal (bit-identical 512/4096/333,
voice==mono, voices independent, extremes finite). Example
`examples/waveshaper_fold_drone.json` (110 Hz sine, 0.08 Hz LFO sweeps
fold 1→7, symmetry 0.15 — post-master peak 0.54).

**With this the nonlinear pair is complete:** distortion for east-coast
grit, waveshaper for west-coast bloom, both aliasing-safe on the same
streaming 4× pair. Suite: 1073 in sandbox (+18 mido) — +27 pedal, +23
folder over the meter baseline.

## 2026-07-02 — Distortion (`distortion`): the rack goes nonlinear

Matthew picked the missing food group — and asked for the pair as **two
separate modules** (this pedal, then the wavefolder). Scoped via
AskUserQuestion: drive-pedal + folder split, **4× oversampling on both**,
all three curves (soft/hard/tube), triangle+sine folds for its sibling.

**The oversampling infrastructure is the real fixture of this commit.**
Nonlinear curves make harmonics without a bandwidth limit; at the native
rate everything past Nyquist folds back as inharmonic hash. New
module-level `_Oversampler4` in `numpy_backend.py`: zero-stuff ×4 →
65-tap linear-phase FIR → curve → same FIR → decimate `[::4]`, both
filters run STREAMING via `lfilter` with per-voice `zi` carry, so the
result is block-size independent and voices stay fully independent. Tap
count chosen so total group delay is an integer **16 base-rate samples**,
letting the dry path of `mix` be delay-compensated exactly (same trick as
the pitch shifter's dry tap). Measured: the folded 5th harmonic of a
hard-clipped 6 kHz sine sits >34 dB below the legitimate 3rd (0.14%).
Also new: a shared streaming `_dc_block` one-pole (~3.5 Hz) for
asymmetric curves.

**The curves.** All normalised (full-scale → full-scale, identity as
drive→0): `soft` = tanh(d·u)/tanh(d); `hard` = clip(d·u); `tube` =
tanh(d·u + c) − tanh(c) with a CONSTANT bias c=0.25, normalised by the
larger rail. The first tube attempt scaled the bias WITH drive and
normalised to the positive rail only — the negative rail blew up to
≈−20× full scale. Caught in the smoke test (tube "DC" of −0.66 was
really the blocker chewing through a monster transient), redesigned to
the constant-bias bounded form: H2/H1 ≈ 5%, peak 0.83, DC 0.0007.

`tone` = streaming one-pole post low-pass (bypassed ≥ 20 kHz); `level`
trim; `mix` ≤ 0 returns the input bit-exactly (chorus contract);
`drive_cv` per-sample in drive units (zero-order-hold up to 4×), clamped
0.01..60. UI: TYPE-guarded sliders + `mode` arm in the shared combo
dispatch. pyo stub extended.

**Tests: 27 in `tests/test_distortion.py`** — curve character (odd-only
vs even+DC-blocked vs flat-top), tone/level/mix algebra (mix output ==
0.5·delayed-dry + 0.5·wet exactly), constant-CV == static-drive, alias
suppression, bit-identical 512/4096/333, voice==mono, voices
independent, extremes finite. Example `examples/distortion_drive.json`
(96 BPM sequenced saw riff → ADSR/VCA → tube drive 8, tone 3.5 kHz,
level 0.55 — post-master peak 0.51, headroom per the house rule).

Waveshaper (the folder) lands in the next commit, stacked on this
oversampling plumbing.

## 2026-07-02 — Meter follow-ups ×4: stereo, RMS, peak-hold tick, clip lamp

All four follow-ups flagged when the Meter shipped (2026-06-30) landed in
one pass, scoped with Matthew up front: **optional `in_r` on the existing
module** (over a separate stereo_meter), **`mode` combo peak/rms** (over an
always-both display, keeping the default bit-identical), **auto-clear ~2 s
clip lamp** (over click-to-reset latching), and a **~1.5 s hold-then-fall
tick** (over infinite hold).

**Backend.** `_render_meter` now runs a per-channel indicator bundle,
`_meter_channel`, with L/R-suffixed state keys so the channels are fully
independent. The peak bar is the exact historical envelope — the new
bit-identical guard test recomputes the old formula by hand and asserts
`==` over 50 random blocks. RMS is `sqrt(EMA(mean(x²)))` with a ~300 ms
time constant; on 2D voice buffers the mean-square is per-voice and the
loudest voice wins (a plain mean would read −12 dB low against 16
zero-padded slots — mirrors peak's max-over-voices). The tick has instant
attack, a sample-counted 1.5 s hold, then falls by the same `release`
coefficient as the bar (so it can never read below it); it stays
peak-driven in RMS mode — that's the point of it. The clip lamp lights at
|sample| ≥ 1.0 on any voice and clears after a sample-counted 2 s, so both
windows are block-size independent (tested at 512 vs 4096). `in_r` is
optional: unpatched, no R state advances, `out_r` renders silence, and the
snapshot's right slot is None — the mono Meter is untouched. New GUI hook
`snapshot_audio_meters()` publishes per-channel `(level, hold, clip)`
triples (immutable tuples swapped atomically, keys pre-created in
compile() — same no-lock discipline); `snapshot_audio_levels()` stays for
back-compat and still feeds anything that only wants the bar.

**UI.** Each channel is now a 172×16 drawlist — bar fill, 2 px hold tick,
clip lamp rect, dB text — replacing the progress bar (a bar widget can't
draw a tick over its own fill). Both drawlists are built up front; the R
one shows/hides by tracking whether the snapshot's right slot is None, so
patching/unpatching `in_r` just works. Fill stays on the fixed −90..0 dBFS
scale (two meters, and L/R of one pair, stay directly comparable). `mode`
got an arm in the shared mode-combo dispatch. Verified headlessly under a
real DPG context (fill/tick x-positions, lamp colours, R-bar show/hide,
overlay text).

**Tests.** +27 → 48 in `tests/test_meter.py`; the two spec asserts
(ports/params) updated, every behavioural test passed unchanged before the
new ones were added. Suite: full run below.

**Example.** `examples/meter_stereo_master.json` — LFO→Schmitt→AD-plucked
saw through a chorus, the stereo pair metered inline on the way to the L/R
speaker sinks. Post-master peak ≈ 0.50 (headroom per the house rule); the
tick rides above the falling bar on every pluck; the lamp stays dark —
push the osc amp up to see it fire.

**Follow-ups (new).** Stereo-link option (both bars share one peak scale);
K-weighted/LUFS-ish mode; clip counter; numeric hold readout on hover.

## 2026-07-02 — Reverb + Mixer CV: the CV-coverage plan closes

The last two static holdouts from the CV-coverage audit, scoped with Matthew
(both recommendations accepted): **reverb `decay_cv` + `mix_cv`**, and
**per-channel VCA-style gain CVs on the mixer**.

**Reverb.** Both new inputs target 0..1 macros, so they follow the loudness
pattern: additive in level units, one shared `cv_depth` (default 1.0),
block-meaned, then clamped 0..1 by the exact clamps the static params use.
Implementation is ~10 lines in `_render_reverb` before the clamps — the FDN
itself is untouched. `size` deliberately gets no CV: sweeping the delay-line
lengths clicks (noted as a follow-up only with crossfaded re-tap
interpolation). Musical payoffs: envelope-driven reverb throws, wet ducking
(mix CV to 0 reaches the documented bit-exact dry passthrough), rooms that
open on held notes.

**Mixer.** `gain1_cv`…`gain4_cv`, **per-sample multiplicative** — channel i
becomes `in_i · gain_i · cv_i`, unpatched = unity. Knobless by the
just-written house rule (the amplitude-multiplier exception: the CV *is* the
amplitude, like `vca.cv`); no new params at all. This turns the mixer into
four VCAs with a sum: ADSR-swelled channels, sequencer-stepped mixing, and
the classic auto-crossfade (LFO → `gain1_cv`, its inverse via CVScale −1 →
CVOffset +1 → `gain2_cv`; the crossfade test proves cv + (1−cv) reconstructs
the input exactly).

**Tested invariants (+19 → suite 1014).** Reverb: CV renders bit-identically
to the equivalent static param (dyadic-exact test values — 0.3+0.7 style
sums differ in the last ulp and taught the tests to use 0.5+0.5); depth
scales and 0 disables; over-range CV clamps; the tail audibly lengthens
(30-block render, late-window RMS > 4×). Mixer: unpatched CVs bit-identical
to the pre-change sum (the retrofit is inert); a ramp CV shapes the block
per-sample exactly; one channel's CV leaves the others untouched; crossfade
sums to constant. Model walls; pre-CV patch dicts load with the default.

**Example.** `examples/mixer_crossfade_verb.json` — saw A ↔ square B
auto-crossfaded at 0.2 Hz through the gain CVs, into a hall whose mix
breathes under a 0.09 Hz LFO (`cv_depth` 0.5 so it stays tasteful). Peak
≈ 0.35. UI: reverb `cv_depth` drag ("lvl/unit"); mixer needs no new widgets
(knobless). MODULES.md: CV-conventions table rows, reverb entry updated,
mixer "_To document._" stub replaced with a full entry.

**With this, every item on the 2026-07-02 CV-coverage plan is done:**
crossover freq_cv → animated-EQ trio (sweep/motion/tilt) → cv_depth
convention standardisation → reverb/mixer CV. Open follow-ups live with
their modules (damping_cv, master_cv, pivot_cv, per-band gain-CV, tilt
slope options).

## 2026-07-02 — CV-depth convention standardisation (audit + Filter/LFO retrofit)

The "subtler half" of the CV-coverage plan, done as audit-first: walked every
`*_cv` input in the codebase before touching anything. Finding: **the drift
was smaller than the TODO feared.** All eleven shipped `cv_depth` knobs
already agree — frequency targets are octaves/unit defaulting 1.0, pitch
targets semitones/unit defaulting 12.0 (≡ 1 V/oct), delay ms/unit, loudness
level/unit, tilt dB/unit — so every frequency/pitch input ships V/oct-
calibrated. The real gaps: Filter and LFO still had 1 V/oct **hard-coded**
with no knob, the rule existed nowhere in writing, and one UI label
(loudness) hid its unit.

**Matthew's calls** (offered with recommendations, both accepted): natural
unit per domain — no forced unification (ms→octaves would be a forced fit);
retrofit **Filter + LFO only**. `oscillator.freq_cv` stays a *calibrated*
pitch input: it's the pitch bus every keyboard/sequencer/MIDI patch tunes
through, and hardware makes the same split (calibrated V/OCT jack vs FM input
with attenuator). `vca.cv` / `oscillator.amp_cv` stay knobless multipliers —
the CV *is* the amplitude; CVScale attenuates.

**Shipped.** `filter.cutoff_cv` + `lfo.rate_cv` gained `cv_depth`
(octaves/unit, default 1.0 = the exact old behaviour). All five backend CV
sites updated: filter mono, filter voice per-voice `(V,F)`, filter voice
shared, lfo mono, lfo voice per-voice. Docs: new **"CV depth conventions"**
section in MODULES.md — the house rule, the two deliberate exceptions, and a
full module×input×default×unit×summing table; filter/lfo entries updated.
UI: a generic `cv_depth` fallback widget ("%.2f oct/unit") now catches any
octave-domain depth without a dedicated branch (filter + lfo land there);
loudness's bare "%.2f" became "%.2f lvl/unit" — every depth knob in the app
now shows its unit.

**Tested invariants (+14 → suite 995).** Depth 1.0 + CV c renders
**bit-identically** to a static cutoff/rate at `base·2^c` (the retrofit is
provably the old 1 V/oct); unset depth == explicit 1.0; depth 2 doubles the
octave shift; depth 0 disables bit-identically to unpatched; the voice paths
apply depth per voice ((V,F) CV, voice 0 base / voice 1 shifted); patch
dicts saved *before* the retrofit (no cv_depth key) load with the default —
old patches sound identical. No PARAM_ALIASES needed (params added, none
renamed). Exact-dict assertions in test_filter/test_lfo updated.

**CV-coverage plan status:** animated-EQ trio done, crossover freq_cv done,
convention standardised. Remaining (lowest priority): reverb size/mix CV,
mixer gain CV; motion_eq per-band gain-CV and tilt_eq pivot_cv stay logged
as follow-ups.

## 2026-07-02 — TiltEQ (`tilt_eq`): the spectral seesaw — animated-EQ trio COMPLETE

Third and final of the animated-EQ trio (after `sweep_eq` and `motion_eq`):
a **CV-controlled tilt EQ**. One control seesaws the whole spectral balance
about a `pivot` frequency — positive tilt boosts the lows and cuts the highs
by the same amount (warmer), negative is the exact mirror (brighter), and the
response passes ~0 dB at the pivot. `tilt_cv` drives it: an LFO makes a patch
breathe dark↔bright, an envelope opens the top end with dynamics — one-knob
voltage-controlled brightness, the simplest possible animated EQ.

**Built by reuse, again.** The TODO spec said "two opposed shelves about
`pivot` (like the `loudness` shelving pair)" and that's exactly what shipped:
`_tilt_eq_coeffs` calls the loudness module's `_loud_shelf` twice — low shelf
at `pivot` with `+tilt` dB, high shelf at the *same* pivot with `-tilt` — and
`_render_tilt_eq` then delegates straight to `_render_loudness_mono/_voice`,
which turn out to be fully generic biquad-cascade renderers keyed by module
id. No loudness code was touched. So the DF-I state discipline,
shape-polymorphism (mono `(F,)` + per-voice `(V, F)`, voice row bit-identical
to mono) and the bit-exact identity at 0 dB are literally the same code paths
the loudness module runs — the same reuse-not-duplication move as
motion_eq → parametric_eq.

**Knob convention** (Tonelux-style hardware tilt): `tilt` in dB is what the
lows gain *and* the highs lose, so the total low↔high spread is twice the
knob. Effective tilt = `tilt + cv_depth · mean(tilt_cv)` dB — summed in dB
space, block-meaned (one coefficient set per block shared across voices, the
Crossover's macro-sweep policy), clamped ±18 dB. `cv_depth` defaults to 6
dB/unit so a full-depth bipolar LFO seesaws ±6 dB. Measured on sine probes:
tilt +6 → **+6.0 dB @ 60 Hz, −0.0 @ 1 kHz pivot, −6.0 @ 12 kHz**; the null
tracks `pivot` (500 Hz probe: flat with pivot=500, +8 dB with pivot=4 kHz).

**Tested invariants (+20 → suite 981, all green in the sandbox with mido +
dpg installed).** Bit-exact passthrough at tilt 0; boost/cut symmetry both
directions; pivot stays flat and moves the null; a +1 CV at depth 6 renders
**bit-identically** to a static +6 dB tilt (dB-space summing); depth 0
disables CV; 12 + 12 clamps to 18 (bit-identical to static 18); block-size
independent (512 vs 2048 bit-identical); voice==mono bit-identical; type
walls; JSON round-trip; osc→tilt→speaker integration.

**Example.** `tilt_eq_seesaw.json` — a 110 Hz saw drone through tilt_eq
(`cv_depth` 9) swept by a 0.12 Hz bipolar sine LFO → speaker. Self-playing;
the saw's harmonic balance audibly rocks bass-heavy↔treble-heavy; peak ≈ 0.47
(headroom per the house gain rule). UI: pivot drag (Hz), tilt slider
(±12 dB), cv_depth drag (dB/unit); pyo silent-stub extended; MODULES.md
catalogue + index entries.

**Animated-EQ trio complete** — sweep_eq (one moving band) / motion_eq (four
moving bells) / tilt_eq (the whole spectrum on a seesaw). Follow-ups logged:
slope options (fixed dB/oct tilt steepness), `pivot_cv`, per-band gain-CV on
motion_eq. **Next on the CV-coverage plan:** the `cv_depth`-convention
standardisation (units drifted: octaves / ms / semitones / dB across
modules); reverb/mixer CV stays lowest priority.

## 2026-07-02 — MotionEQ (`motion_eq`): the four-CV-input animated EQ

Third CV-coverage item and the second of the animated-EQ trio. A 4-band
parametric EQ where each band's centre frequency has its own CV input —
`band1_freq_cv` … `band4_freq_cv` — so four peaks/notches glide
independently. Matthew picked **shared `cv_depth`** over per-band (per-band
sensitivity is still reachable by dropping a CVScale on any input).

**Built by reuse, not duplication.** MotionEQ *is* ParametricEQ's cascade with
CV-swept centres. I added a small backward-compatible `freqs_override=None`
argument to `_render_parametric_eq_mono/_voice`: when None (every existing
call) the behaviour is bit-identical — all 27 ParametricEQ tests stay green —
and when provided it uses those centres with the module's static gains/Qs. The
new `_render_motion_eq` just computes the swept centres
(`band{i}_freq * 2**(cv_depth * mean(band{i}_freq_cv))`, block-mean, one coeff
set per block shared across voices like the Crossover) and delegates. So the
peaking math, DF-I state discipline, shape-polymorphism and block-size
independence are literally the same code ParametricEQ uses.

**Tested invariants (+12 → suite 961: 943 passed + 18 mido-skips).** With
nothing patched, `motion_eq` is bit-identical to a `parametric_eq` of the same
params. A +1.0 CV at unit depth on band 2 moves *only* band 2's centre 500→1000
Hz (bit-identical to a static band2_freq=1000, and provably different from the
no-CV render). Two bands with different CVs move independently. The shared
`cv_depth` scales every band's sweep together; depth 0 disables. All-flat (0 dB)
is a bit-exact passthrough *even while the CV sweeps the centres* (a 0 dB peak
is identity). A boost band tracks a tone as its centre is swept onto it.
voice==mono bit-identical (per-band CV, two bands checked); block-size
independent.

**Example.** `motion_eq_animated.json` — white noise through two +10 dB bands
(500 Hz Q2.5 and 2 kHz Q2.5) swept by a 0.15 Hz sine and a 0.23 Hz triangle at
`cv_depth` 1.5; two resonant peaks glide through the noise. Noise amp backed to
0.15, speaker 0.5 — post-gain peak 0.18 ([[feedback_gain_headroom]]). UI reuses
the ParametricEQ band widgets (`band{i}_freq`/`_gain`/`_q`) with a `cv_depth`
drag added to the shared block; pyo silent-stub extended.

**Next on the CV-coverage plan.** Only `tilt_eq` (one `tilt_cv` seesaws
bass↔treble about a pivot via opposed shelves) remains to complete the trio;
then the `cv_depth`-convention standardisation. Reverb/mixer CV stays lowest
priority. Possible motion_eq follow-up: per-band gain-CV (the other animated
dimension).

## 2026-07-02 — SweepEQ (`sweep_eq`): CV-swept resonant band / auto-wah

Second item off the CV-coverage plan, and the first of the animated-EQ trio.
Matthew picked `sweep_eq` alone this session, with a **switchable voicing**
(over the plan's peak-only spec) — because the plain Filter + `cutoff_cv`
already covers a true bandpass wah (`examples/wah.json`), so a one-trick node
would have overlapped it.

**What shipped.** A single CV-swept resonant band. `in` + `freq_cv` → `out`;
params `mode` / `freq` / `gain` / `q` / `cv_depth` / `mix`. Three voicings:
`bandpass` (default — the classic auto-wah), `lowpass` (resonant corner sweep),
and `peak` (a swept EQ *bell* that boosts the moving band but passes the rest —
the one thing the Filter can't do, since it keeps the full-range signal).
`freq_cv` sweeps the centre 1 V/oct × `cv_depth`, block-meaned to one
coefficient set per block shared across voices (the crossover's macro-sweep
policy). `mix` blends dry/wet.

**Cheap by reuse.** One RBJ biquad. `peak` borrows ParametricEQ's
`_peq_coeffs`; `bandpass`/`lowpass` borrow the Filter's `_filter_coeffs` — so
clamping, stability and the sweep all match the modules it borrows from. State
is the coefficient-independent DF-I history (x1,x2,y1,y2), same discipline as
the Filter, so a swept `freq_cv` changing coefficients per block stays clean.
Shape-polymorphic; the voice path is one `lfilter` over all rows (shared
coeffs) and a single voice row is bit-identical to the mono path.

**Nice properties (tested).** `mix=0` is a bit-exact dry bypass; a `peak` band
at 0 dB with `mix=1` is a bit-exact passthrough; `mix=0.5` reconstructs from
the wet+dry renders. Behavioural: bandpass resonates at the centre (peak gain
≈ Q) and rejects far tones; lowpass passes below / cuts above; peak boosts the
band but leaves off-band at ~unity. `freq_cv` math: unit +CV at unit depth
doubles the centre to a static-match, `cv_depth` scales the exponent, depth 0
disables. Voice==mono bit-identical across all three modes. Block-size
independent. **19 tests → suite 949 (931 passed + 18 mido-skips).**

**Example.** `sweep_eq_autowah.json` — a 110 Hz saw through a `bandpass`
sweep_eq (q 3.5), a 1.2 Hz LFO into `freq_cv` at `cv_depth` 1.6 (≈ 165–1500 Hz
sweep); source amp backed to 0.3 and speaker to 0.5 for headroom (a resonant
bandpass boosts ~Q× at the peak — [[feedback_gain_headroom]]). Post-gain peak
0.31, RMS breathes ~2.85× over the sweep. UI: `mode` combo (bandpass/lowpass/
peak) + freq/gain/q/cv_depth/mix widgets; pyo silent-stub extended.

**Next on the CV-coverage plan.** `motion_eq` (4-band, four independent
`freq_cv` inputs) and `tilt_eq` (one `tilt_cv` seesaws bass↔treble) remain of
the trio; then the `cv_depth`-convention standardisation. Reverb/mixer CV stays
lowest priority.

---

## 2026-07-02 — Crossover `freq_cv` (CV-swept split point)

First item off the **CV-coverage plan** (the "uneven CV coverage" audit): the
`crossover` split point is now voltage-controllable. Matthew picked this — the
easy win — over the animated-EQ trio for this session.

**What shipped.** `crossover` gained a `freq_cv` input (cv) and a `cv_depth`
param (octaves per CV unit, default 1.0 = the standard 1 V/oct). The corner is
`freq * 2 ** (cv_depth * mean(freq_cv))`, block-meaned — the same cadence and
idiom as the Filter's `cutoff_cv` and the modulation FX' `rate_cv`. Leave
`freq_cv` unpatched and the corner is the static `freq` param, bit-identically.

**Design note / deviation.** The plan said "near copy-paste of the filter's CV
handling." The filter's *voice* path can give every voice its own coefficients
from a `(V, F)` `cutoff_cv`; the crossover deliberately keeps ONE scalar
coefficient set that its voice branch broadcasts across all slots. Rather than
rewrite that broadcast path, `freq_cv` is meaned over **all** axes → a single
macro sweep shared by every voice. Per-voice split points would be a much
larger change for an exotic use case, so I scoped it out (flagged in TODO). The
computation lives once in `_render_crossover`; the mono/voice branches now take
the effective `freq` as an argument (no more reading the param themselves).

**Tests (+9 → suite 930: 912 passed + 18 mido-skips).** Math equivalence
(unit +CV doubles the corner to a static-2 kHz match; −CV halves it; `cv_depth`
scales the exponent; depth 0 disables); zero-CV and unpatched are exact no-ops;
behavioral split-direction flips (a 1500 Hz tone crosses from the high band to
the low as the corner sweeps up, and the 700 Hz mirror); LR4 flat-sum survives
the sweep; and the voice==mono bit-identical invariant under a shared `freq_cv`.

**Example.** `crossover_sweep.json` — a 110 Hz saw split with only the **high**
band monitored while a 0.3 Hz LFO sweeps the corner ±2 octaves (`cv_depth` 2.0);
the high output's RMS breathes ~5× as harmonics cross the moving split. UI: the
crossover node now shows a `freq` (Hz) drag and a `cv_depth` (oct/unit) drag.

**Next on the CV-coverage plan.** The animated-EQ trio (`motion_eq`,
`sweep_eq`, `tilt_eq`) and the `cv_depth`-convention standardisation remain;
reverb/mixer CV stays lowest priority.

---

## 2026-07-01 — Phaser (`phaser`): swept allpass-notch (modulation trio complete)

The last of the modulation trio, after the chorus and flanger. Where the
chorus thickens with delay and the flanger rings with a short *fed-back*
delay, the phaser sweeps **notches** carved by a cascade of **allpass**
stages — the softer, rounder, less metallic cousin. Scoped with Matthew via
AskUserQuestion: **selectable 4 / 6 / 8 stages** (two / three / four
notches, as a combo), **bipolar** feedback (matching the flanger), and a
**stereo** pair out (quadrature LFO, matching the whole trio).

**DSP.** Mono-summed input runs through N first-order allpass sections
(transposed direct-form II: `y = a·v + s`, `s = v − a·y`, one state per
stage). Each allpass leaves magnitude flat and only rotates phase; summing
the chain output back with the dry signal cancels wherever a frequency has
been turned a half-cycle out of phase, carving a notch — one per stage
*pair*, so 4/6/8 stages give 2/3/4 notches. An internal sine LFO sweeps the
allpass break frequency exponentially (±`depth`·2 octaves around `center`),
giving the coefficient `a = (tan(π·fc/sr) − 1)/(tan(π·fc/sr) + 1)`. A
one-sample feedback of the last stage back into the chain input (bipolar,
±0.95) sharpens the notches into resonant, vocal peaks. Two chains run with
the L and R LFOs a quarter-cycle apart for stereo width. The feedback makes
each output sample depend on one just written, so the cascade runs
**per-sample** (both channels advanced together as a length-2 vector) — but
the LFO phase, the allpass state and the feedback memory all carry across
blocks, so the render is exactly **block-size independent** (bit-identical
at 512 / 4096 / 333). `mix = 0` is a bit-exact dry passthrough on both
channels even under strong feedback.

**Validated offline before wiring:** the notch count scales with the stage
count (4 / 6 / 8 stages → ≈ 2 / 5 / 8 smoothed spectral dips over noise); a
fixed 800 Hz tone is amplitude-modulated ≈ 8× as the notch sweeps through
it (a moving notch); an impulse rings ≈ 110× longer at feedback 0.9 vs 0.1
(resonance); mix = 0 bit-exact; block-size independence; single voice row
bit-identical to mono. 29 tests in `tests/test_phaser.py`; full suite **904**
(886 passed + 18 mido-skips), +29 from the flanger's 875.

**Hand-off to Matthew**: delivered as `phaser.patch`, `git am`-verified
clean on `22fcdd2` (your current `origin/main`, which has the flanger),
full suite green in the am'd tree. Hear it: open `examples/phaser_sweep.json`
— a three-saw power chord breathing through the notch sweep, the slow LFO
drifting the rate. That completes the **modulation trio** (chorus, flanger,
phaser).

**Next**: **through-zero** flanging is still the biggest open modulation
item (the flanger's dramatic tape jet); then `depth_cv` on any of the trio;
tempo-syncing the sweep rate to the Clock; a per-stage frequency spread for
the phaser; and optional feedback-path damping for a darker sweep.

---

## 2026-07-01 — Flanger (`flanger`): swept resonant comb (bipolar feedback)

The second of the modulation trio, after the chorus — and the fed-back
sibling the chorus docs kept pointing at. A flanger mixes the input with a
*very short* delayed copy of itself (a comb filter), sweeps that delay with
an internal LFO so the comb's notches slide across the spectrum, and feeds
part of the delayed signal back to sharpen the comb into ringing
resonances. That regeneration — the feedback the chorus deliberately
omitted — is the flanger's signature.

**Scoped with Matthew** (AskUserQuestion, as with chorus/resampler):
**stereo** out (`out_l`/`out_r`, matching the chorus as a sibling),
**standard** positive-delay flanging (the delay stays just above zero;
through-zero "tape" flanging is a follow-up), and **bipolar** feedback
(positive rings bright, negative goes hollow/metallic).

**DSP** (`_render_flanger` in `numpy_backend.py`): the mono-summed input
feeds two short delay lines (one per channel). One internal sine LFO drives
both, L and R phases a quarter-cycle apart so the two combs sweep out of
step (stereo width). The delay is `manual` ± `depth`·sweep, clamped to a
positive floor (≥ 2 samples) so it never crosses the write head — the
"standard, not through-zero" choice. Because a musical flange delay
(~0.1–6 ms) is always far shorter than a block, a read this sample can
depend on a sample written this sample, so the feedback recirculation runs
**per-sample** — the same short-time path the delay module already uses.
The LFO phase and ring contents carry across blocks, so despite the
per-sample loop the render is exactly **block-size independent**
(bit-identical at 512 / 4096 / 333). `mix=0` is a bit-exact dry passthrough
on both channels *even with strong feedback* (dry term `x·(1−mix)`, wet
gated to zero). A single-voice `(1,F)` input is bit-identical to the mono
path. Feedback clamps to ±0.95, so the comb stays bounded.

**Wiring**: registered in `modules/__init__.py`; `flanger` dispatch in the
backend; pyo silent-stub; UI param block (`rate`/`manual`/`cv_depth` drags,
`depth`/`mix` sliders, and a **bipolar** `feedback` slider from −0.95 to
0.95). `docs/MODULES.md` gets an index row and a `#### flanger` entry, and
the chorus entry's "planned sibling" note now links to it.

**Tests**: 26 in `tests/test_flanger.py` (model/ports/type-walls; mix=0
bit-exact dry with feedback; impulse tap at `manual`; depth=0 static comb ≠
dry; block independence; bipolar sign + longer ring with more feedback +
bounded at ±0.95; stereo decorrelation; rate_cv). Full suite green — 875
with the UI zoom tests (+26 from the chorus's 849); 852 in this headless
sandbox where dearpygui isn't installable so the 23 zoom tests don't
collect (834 passed + 18 mido-skips).

**Hand-off to Matthew**: delivered as `flanger.patch`, `git am`-verified
clean on `d34471d` (your current `origin/main`, which has the chorus), full
suite green in the am'd tree. Hear it: open `flanger_jet_sweep.json` — a saw
riff sweeps through the jet whoosh, wider and narrower as the slow LFO
drifts the rate. Note: the project **mount is healthy for reads** this
session (my earlier "corrupted" call was a wrong-path check on my part);
the one real snag is a stale zero-byte `.git/index.lock` the sandbox can't
remove — clear it in PowerShell (`Remove-Item .git\index.lock`) before
`git am` if git complains.

**Next**: the last of the trio — a **phaser** (a cascade of swept allpass
stages; the reverb's allpass diffusers are the building block). Then
**through-zero** flanging (a second delayed dry path so the sweep can cross
zero — the dramatic tape jet); `depth_cv`; tempo-sync the rate to the
Clock; a stereo-offset param; and optional feedback-path damping.

## 2026-07-01 — Chorus (`chorus`): detuned multi-voice stereo thickener

The synth's first **modulation effect**, and Matthew's pick after the
loudness contour. Offered chorus / flanger / phaser; he chose **chorus
first** (its modulated-delay core is what the flanger will build on) and,
in the design pass, a **stereo** pair out (like Reverb) with a `rate_cv`
input — and no feedback knob, since a fed-back chorus *is* a flanger and
those stay separate modules.

*What it is.* Mono in (a voice-aware input is summed to mono first, the
Reverb convention) → a bank of short delay lines, each read back a little
behind the write head with linear interpolation. One internal sine LFO is
sliced into `voices` evenly-spaced phase offsets, and each voice's read
delay is `base + depth·sweep·lfo` — base delays spread ~12–24 ms, the
sweep up to ±8 ms scaled by `depth`. A moving delay is a moving pitch, so
each copy drifts a few cents around the original; that shifting detune
between the copies is the chorus. The voices are panned across the stereo
field (equal-power, per-channel normalised) so `out_l` / `out_r` are
decorrelated — the width half of the sound.

*Why it's simple and exact.* There is **no feedback** (that's the
flanger's job), so no read this block depends on a sample written this
block: the render writes the whole block, then reads every tap in one
vectorized pass. That also makes it **exactly block-size independent** —
bit-identical output at 512 / 4096 / 333 (diff 0.0), the same correctness
bar the reverb holds to. `mix=0` is a bit-exact dry passthrough on both
channels.

*Params.* `rate` (LFO Hz, 0.05–10), `depth` (0–1 sweep), `voices` (1–6
detuned copies), `mix`, and `cv_depth` (octaves of LFO-rate shift per
`rate_cv` unit, 1 V/oct, block-mean — the LFO module's own cadence). One
voice sits dead-centre, so the two channels collapse together; two or
more spread and decorrelate.

*Tests / example / docs.* `tests/test_chorus.py` — 25 tests (model +
type walls; silence; `mix=0` bit-exact both channels; impulse taps;
`depth=0` static-comb vs modulated; voice-count changes texture;
finite/bounded at extremes; 2D → mono; block-size independence; stereo
decorrelation + single-voice collapse; `rate_cv` alters the sweep and an
all-zero `rate_cv` is a noop; osc → chorus → L/R integration). Suite
**849** sandbox (+18 mido), +25 from 824. Example
`examples/chorus_lush.json` (a self-playing saw pad widened into a
four-voice ensemble, a slow LFO drifting the rate through `rate_cv`).
`docs/MODULES.md` index row + `#### chorus`. pyo silent-stub; UI
(rate / cv_depth drags, depth / mix sliders, voices int).

**Hand-off to Matthew:** delivered as `chorus.patch`, `git am`-verified
clean on `d22dea8` (your current `origin/main`), full suite 849 green in
the am'd tree. Hear it: open `chorus_lush.json` — the saw pad thickens
into an ensemble across both speakers, and the shimmer slowly speeds up
and slows down as the LFO drifts the rate.

**Next:** the other two of the trio — a **flanger** (add feedback + a
shorter delay for the through-zero jet sweep; the modulated-delay core is
already here) and a **phaser** (swept allpass stages, the reverb-allpass
sibling). Plus a `depth_cv`; a stereo-width param; a slight per-voice
rate detune for an even creamier ensemble; and tempo-syncing the rate to
the Clock.

## 2026-07-01 — Loudness (equal-loudness contour)

Matthew asked "what's a sound contouring filter?" — I gave the two senses
(a synth envelope sweeping a filter over time vs. a hi-fi loudness/EQ
contour reshaping the frequency balance); he picked the **loudness**
sense, "both in one": an automatic equal-loudness curve on a `level` knob
plus manual bass/treble trims.

*What it is.* `loudness`: `in` + `level_cv` → `out`. The ear loses bass and
treble as things get quieter (equal-loudness / Fletcher–Munson), so as
`level` drops from 1 the module blooms a low shelf and a high shelf, bass
faster than treble, tracking that curve; `bass`/`treble` add fixed dB trims
on top; `level_cv` (averaged to a scalar) modulates the level. It reshapes
frequency balance — not an envelope sweeping a filter (that's ADSR →
Filter `cutoff_cv`).

*DSP.* Two RBJ shelving biquads (low ~120 Hz, high ~8 kHz), gains =
`BASS_MAX(12)`/`TREBLE_MAX(7)` × (1 − level) + the manual trims (clamped
±18 dB). Cascade + coefficient-independent DF-I state **mirrors
`parametric_eq`** (shape-polymorphic; a `(V, F)` input runs V parallel
cascades with the *shared* global curve; a single voice row is bit-
identical to mono). At `level` = 1 with no trims every shelf is 0 dB →
identity → **bit-exact passthrough**.

*Measured response* (bass 60 Hz / mid 1 kHz / treble 12 kHz): level 1.0 →
+0.0 / +0.0 / +0.0; level 0.5 → +5.6 / 0.0 / +3.2; level 0.0 → +11.1 / 0.0
/ +6.3. Manual +6 bass and +6 treble trims land on the right shelf with the
mid untouched; a −1 `level_cv` (cv_depth 1) drives the effective level to 0
(full bass boost). Mid stays flat throughout.

*Tests / example / docs.* `tests/test_loudness.py` — 18 tests (model,
flat bit-exact passthrough, bass blooms monotonically as level drops,
treble blooms but less than bass, mid untouched, manual trims, `level_cv`
lowers effective level, mono == voice, integration). Suite **824** sandbox
(+18 mido), +18 from 806. Example `examples/loudness_demo.json` (a quiet
saw bassline kept full by the contour). `docs/MODULES.md` index row +
`#### loudness`. pyo silent-stub; UI (level/bass/treble sliders, cv_depth
drag).

**Hand-off to Matthew:** delivered as `loudness.patch`, **stacked** on the
delay+reverb (base `5a55f80`). Full apply order from your current mount
HEAD `f854297`: `git am delay.patch`, `git am reverb.patch`, `git am
loudness.patch`. `git am`-verified clean on that stacked tree, suite 824
green. Hear it: open `loudness_demo.json`, then pull the Loudness `level`
up toward 1 to hear the low/high end thin out.

**Next:** exposed curve depth / corner frequencies; a mid-scoop "contour"
option (the bass-amp / distortion-pedal sense of the word); per-voice CV
(the curve is global today); an ISO 226-accurate curve fit; an envelope-
follower that reads the actual signal level to drive the compensation
automatically (true dynamic loudness).

## 2026-07-01 — Reverb (stereo Feedback Delay Network)

Matthew's "where to continue?" pick after the delay. Scoped via
AskUserQuestion to an **FDN** (over plate/Schroeder). He first chose mono
out, then mid-build switched to a **stereo pair** ("I like the signal path
mono, give two channels of output… 2-player mode") — which is the better
call anyway, since a reverb's spaciousness *is* L/R decorrelation, and the
`left_speaker_output` / `right_speaker_output` modules already exist.

*Architecture.* Mono in (voice sources summed) → `out_l` / `out_r`. Input
**diffusion** (4 series Schroeder allpasses) smears the input, then an
**8-line FDN**: eight near-prime delay lines cross-mixed every sample by an
orthonormal Sylvester–Hadamard matrix, re-injected with a per-line decay
gain (so all lines hit the same RT60) and a shared damping one-pole. Two
*orthogonal* Hadamard rows tap the lines for the L/R outputs, so the
channels are decorrelated (measured corr ≈ −0.01 = real width). Params:
`size` (line lengths, room→hall), `decay` (RT60 ~0.2–12 s), `damping`
(HF absorption), `mix`.

*Why diffusion got added.* The bare 8-line FDN smoke-tested with a **56 %
near-silent** tail — an audibly gappy, grainy "reverb". Adding the 4 input
allpasses took that to **0.6 %** (a dense, smooth wash) without disturbing
the other properties. That's the difference between sounding like a broken
comb filter and sounding like a room.

*Block-size independence (the correctness crux).* A feedback delay only
recirculates within a block when a line is shorter than the block, so the
whole network — diffusers and FDN — is processed in **hops no longer than
the shortest line**; within a hop every read predates the hop's writes, so
it vectorizes (Hadamard mix as a matmul, damping one-pole via `lfilter`
with carried `zi`). Output is **bit-identical across block sizes**
(verified 0.0 for L and R at 512 vs 4096 vs 333). `mix=0` is a bit-exact
dry passthrough; orthonormal feedback + per-line gain < 1 + damping keep it
stable (bounded/finite at max decay); wet/dry trimmed to ≈0.5.

*Tests / example / docs.* `tests/test_reverb.py` — 19 tests (model, mix=0
passthrough, impulse decays, more-decay-longer-tail, dense-not-gappy,
damping rolls off the tail, stability at max decay, voice→mono,
**block-size independence**, L/R decorrelation, osc→reverb→L/R-speakers
integration). Suite **806** sandbox (+18 mido), +19 from 787. Example
`examples/reverb_space.json` (self-playing triangle melody → big hall →
left/right speakers, peak ~0.31, true L≠R). `docs/MODULES.md` index row +
`#### reverb`. pyo silent-stub; UI sliders; headless DPG node-build check
passed.

**Hand-off to Matthew:** delivered as `reverb.patch`, **stacked on the
delay** (base `da98582` = your post-zoom tree + delay). Apply order from
your current mount HEAD `f854297` (zoom applied): `git am delay.patch`
then `git am reverb.patch`. `git am`-verified clean on that stacked tree,
full suite 806 green. To hear it: open `reverb_space.json` and play.

**Next:** tail **modulation** (slowly chorus the delay-line lengths to kill
the last metallic ring on pure sustained tones — the one quality gap left);
16 lines / longer diffusion for even more density; `pre_delay`; a
freeze/infinite-hold mode; `size`/`mix` CV; an early-reflections tap; true
stereo *input* once the signal path itself goes stereo.

---

## 2026-07-01 — Delay (analog-voiced feedback echo)

Matthew's pick when he asked "what else can we do?" after the zoom feature.
The synth had filter/EQ/crossover/pitch effects but **no time-based effect** —
no echo, no reverb — so a delay was the clearest gap. Scoped in-convo via
AskUserQuestion: **analog-voiced** (damped feedback) over clean/tape, and
**free time + CV** over clock-sync, for a robust v1.

*Module.* `delay`: `in` (audio) + `time_cv` (cv) → `out` (audio). Params
`time` (ms, 1–2000), `feedback` (0–0.98, clamped below runaway), `tone`
(0–1 damping), `mix` (dry/wet), `cv_depth` (ms of delay per `time_cv` unit).

*DSP.* An interpolated ring-buffer delay line. The feedback path runs a
one-pole low-pass whose cutoff the `tone` knob sweeps log-wise ~200 Hz→18 kHz
(sample-rate-independent), so each recirculation darkens — the analog/BBD
voicing. The output taps the **un-damped** read, so the first echo is bright
and the tail melts as it recirculates. Shape-polymorphic like Filter/Crossover
(mono → one line; `(V, F)` → one line per voice slot; a single voice row is
bit-identical to mono).

*Two paths, one result.* A feedback delay is sequential only when the delay is
shorter than a block. When the minimum delay over the block is ≥ one block
(every musical echo time — 300 ms is 14k samples), no read can depend on a
sample written this block, so the whole block **vectorizes**: gathered
interpolated reads, the damping one-pole via `lfilter` (state in `zi`), and a
single fancy-indexed write. Short or heavily-modulated delays (< one block, the
flanger/chorus edge) fall back to a per-sample loop. The two paths are
**bit-identical** (verified max abs diff 0.0 on a 0.6-feedback signal). Perf:
the fast path is **0.048 ms/block** (~0.4 % of the 11.6 ms budget) vs ~7 ms for
the per-sample loop — the reason the fast path exists.

*Tests / example / docs.* `tests/test_delay.py` — 22 tests: model
(defaults/ports/kinds/JSON round-trip/unknown-param/type walls), DSP
(disconnected→silence, `mix=0` bit-exact passthrough, single tap lands exactly
`time` samples late incl. across a block boundary, feedback gives decaying
repeats bounded by the feedback fraction, runaway feedback stays finite, `tone`
damps the high-frequency tail), the two paths agree bit-for-bit, voice (row ==
mono; voices echo independently via per-voice `time_cv`), `time_cv` lengthens
the delay, and an osc→delay→speaker integration render. Suite **787** sandbox
(+18 mido), +22 from 765. Example `examples/delay_dub_echo.json` (the
`sequencer_melody` riff routed VCA → delay → speaker, a dotted-eighth dub echo
at 120 BPM, self-playing, peak ~0.31). `docs/MODULES.md` index row + full
`#### delay` section. pyo silent-stub extended; UI param widgets (time/cv_depth
drags, feedback/tone/mix sliders).

**Hand-off to Matthew:** delivered as `delay.patch`, `git am`-verified clean on
a fresh tree at the post-zoom base (tree `c75e4c0`, == your mount HEAD
`f854297`), full suite 787 green in the am'd tree. To hear it: open
`delay_dub_echo.json` and play — the melody trails dotted-eighth echoes.

**Next:** tempo-sync (a `clock` gate input → delay = N note divisions, the
option not taken this round); ping-pong / stereo spread once the signal path
goes stereo; optional saturation in the loop for a full tape voicing; a
built-in mod LFO for one-knob chorus; a true sub-block flanger path; equal-power
dry/wet. The fast/per-sample split is already done, so the perf follow-up that
other effects still want is, for once, not on this list.

---

## 2026-07-01 — Window zoom (UI scale factor)

Matthew's long-standing ask: zoom out to take in a complex patch, zoom in on
a control for fidelity ("a window scaling factor like zoom in/out"). Up front,
the honest constraint: DearPyGui's node editor wraps the C library *imnodes*,
which has **no real canvas zoom** — it's imnodes' single most-requested
feature, open upstream since 2020, and the DPG maintainer's position (issue
#2530) is that the fix belongs upstream, no ETA. Chasing true zoom would mean
forking the toolkit's C deps or swapping the editor — exactly the "I don't
wanna break it" risk. So this ships a faithful **scale** zoom instead, pure
Python, no new deps, no audio-engine changes.

*How it works.* New `ui/zoom.py` holds the dpg-free maths (constants +
`clamp_zoom` / `step_zoom` / `scale_pos` / `factor_to_percent` /
`percent_to_factor`) so it unit-tests without a graphics context; `app.py`
holds the DPG glue. `_apply_zoom(z)` does two things: `set_global_font_scale(z)`
(nodes auto-size to their text, so they grow/shrink with the font) and
multiplies every node's position by the ratio about the editor origin, so
spacing — and therefore cable lengths — tracks the size instead of overlapping
on zoom-in or scattering on zoom-out. Range **25–300 %**, geometric step ×1.1
(each press is the same proportional change; in/out are exact inverses).

*Controls (the picked variant — slider + keys + wheel).* A toolbar **Zoom %
slider** (doubles as the readout) plus a Reset button; **Ctrl+= / Ctrl+- /
Ctrl+0**; and **Ctrl+mouse-wheel**. Every key/wheel callback re-checks Ctrl, so
a bare key still reaches the keyboard-as-MIDI handler untouched, and bare wheel
is left alone. `set_value` on the slider doesn't re-fire its callback, so keys
and slider stay in sync with no feedback loop.

*Save / load.* Node positions are captured in **logical (100 %) coords**
(divide out the live zoom) so a patch saved while zoomed reloads identically;
the zoom factor is persisted in `patch.ui["zoom"]`. New/Open reset to 100 %
before nodes are (re)built, then the saved zoom is re-applied once every node
exists.

*Known limits (cosmetic, not breakage).* The cables, jack circles and node
borders are drawn by imnodes in screen pixels and **don't** scale with the
font — slightly chunky cables fully zoomed out, slightly thin zoomed in. It's a
**global** scale (menus/toolbar grow too), not a cursor-anchored canvas zoom,
and the bitmap font is a touch soft at non-integer scales.

*Tests.* `tests/test_ui_zoom.py` — 23 pure-maths tests (clamp, geometric step
round-trips and bound saturation, position scaling/composition, percent
round-trip). Suite **765** in the sandbox (+18 mido), +23 from 742; no backend
tests touched. Separately, a headless **xvfb end-to-end check** drove the real
`App` + real node editor: font scale tracks zoom, node positions rescale by the
ratio (40→80, 260→520) and return to base on reset, the slider reads
200/100/150/300, zoom clamps at 3.0, the key/wheel handlers no-op safely when
Ctrl isn't held, and save stores logical coords + the zoom — all green.

**Hand-off to Matthew:** delivered as `window_zoom.patch`, `git am`-verified
clean on local HEAD `737e535` (clock+sequencer). Note GitHub `origin/main` is
still `04a8119` — `737e535` is committed locally but unpushed, so apply this on
your working tree, not a fresh clone of origin. UI-only, no new deps.

**Next:** cursor-anchored zoom (pan toward the mouse as you scale); a
"fit-to-all" button that frames the whole patch; scale link/border thickness
too if imnodes ever exposes it via theme; remember the last zoom in window
prefs; an optional crisp font atlas rasterised at the chosen scale.

---

## 2026-07-01 — Clock (`clock`) + Sequencer (`sequencer`): the self-playing pair

After cv_gates Matthew asked "where to now?" and picked **clock + step
sequencer** — the piece that makes the rack play itself and drives the gate-
bank/AD voices. Scoped via AskUserQuestion: **two modules** (a Clock BPM→gate
+ a clock-driven Sequencer) over a combined or sequencer-only build; **steps
adjustable up to 16**; **per-step value = pitch in semitones → 1V/oct CV +
gate**. Stated defaults (accepted): advance on the clock's rising edge,
transport drives the clock, a `reset` input, gate pulses on enabled steps,
per-step on/off for rests.

*Clock* (`modules/clock.py` + `numpy_backend._render_clock`). Params bpm /
division (pulses per beat) / pulse_width; one `out` gate. Pulse freq =
`bpm/60 * division` Hz. FULLY VECTORIZED: a float64 phase accumulator carried
in `self._state[id]["phase"]` across blocks (phase-continuous, no seam — a
test renders 2048 in one go vs two 1024s and asserts bit-equality), gate =
`mod(phase0 + inc*(1..frames), 1) < pulse_width`. A fresh clock (phase 0)
emits a rising edge on sample 0 so the downstream sequencer plays step 1
immediately.

*Sequencer* (`modules/sequencer.py` + `numpy_backend._render_sequencer`).
Inputs `clock` + `reset` (gate); outputs `cv` (1V/oct) + `gate`. Params:
`steps` (1..16) and an interleaved data-driven list `step{i}_pitch`
(semitones) + `step{i}_on` (rest toggle) for i=1..16 → 33 params, built by
`_default_params()` with a C-major scale on the first 8 steps so a freshly
dropped node plays something. `MAX_STEPS=16` defined once; backend mirrors it
as `_SEQ_MAX_STEPS` (local int, no modules-layer import, same pattern as
`_MAX_VOICES`). Per-sample edge-driven state machine (idx, held cv,
prev_clock, prev_reset in `self._state`): idx starts at **-1** so the first
clock rising edge lands on step 1 (index 0), advances `(idx+1) % steps`, reset
rising edge sets idx=-1; `cv` holds `pitch[idx]/12` for the whole step
(sample-and-hold, so a note stays in tune while its envelope rings out);
`gate` = clock-high AND step enabled. Mono. Reading params fresh each block
means bpm/pitch/steps tweaks apply live without resetting position (only a
structural recompile drops state, like every other stateful module).

*UI* (`ui/app.py`): TYPE-guarded branches — clock bpm (20–300 slider),
division (0.25–16 drag /beat), pulse_width (0.01–0.99); sequencer steps
(1–16 int slider), `*_pitch` (−24..24 st drag), and `*_on` falls through to
the generic bool checkbox. The `cv` out gets the auto CV meter; palette is
automatic. Verified headlessly with a stub dearpygui (each widget routes to
the right control + bounds). pyo punt tuple extended with clock/sequencer
(and cv_gates, which had been relying on the silent `return None` fallback) —
informative-print only; correctness is the numpy backend's.

*Tests / example / docs.* `tests/test_clock.py` (8: defaults/ports, binary
gate, rate from bpm×division, division changes rate, duty = pulse_width,
phase continuity across blocks, dispatch) + `tests/test_sequencer.py` (13:
model/ports, first-pulse-plays-step-1, 1V/oct values, wrap, gate aligned to
clock, disabled-step-is-a-rest, cv holds between pulses, reset rewinds, idle
silent, dispatch, clock-drives-seq integration, full self-playing voice makes
sound). Full suite **724 passed, 18 skipped** in the sandbox — +21 from 703.
Example `examples/sequencer_melody.json`: clock → sequencer → saw osc
freq_cv, gate → pluck ADSR → VCA → speaker; an 8-step riff (with one rest)
that plays itself (peak ~0.40). `docs/MODULES.md` index rows + full `####
clock` / `#### sequencer` entries.

**Hand-off to Matthew:** delivered as `clock_sequencer.patch`, `git
am`-verified clean on origin/main `04a8119` (CVGates) in a fresh clone, full
suite 724 green in the am'd tree. To hear it: open `sequencer_melody.json`
and hit play — it loops the riff on its own. Drive other modules off the same
`clock` (an AD drum, a sample-hold) to lock everything to the beat; patch
`sequencer.cv` into a filter `cutoff_cv` for stepped timbre instead of pitch.

**Next:** swing/shuffle on the clock; clock run/reset inputs (sync several
clocks); per-step gate-length / ratchets on the sequencer; a direction param
(up/down/ping-pong/random); pitch quantize-to-scale; multiple CV rows
(a second value lane per step); save the sequencer's run position so a
recompile doesn't restart it.

---

## 2026-07-01 — CVGates (`cv_gates`): computer keys as a bank of enveloped CV gates

Matthew wanted to "redo the cv keyboard" for **amplitude control**: a CV per
key, 0 when up and rising to 1 when pressed, with variable A/D/S/R — so one
keystroke can drive the `amp_cv` of, say, three oscillators at once and swell
them all together. We talked the design through first.

*Scoping (AskUserQuestion).* Two forks, both Matthew's pick: (1) ship it as a
**new gate-bank module** (leave `cv_keyboard` untouched — no pitch-controller
regression) rather than replacing/augmenting; (2) **17 outputs per physical
key** (C4..E5, absolute) rather than 12 octave-folded pitch classes. He also
confirmed in chat: **computer keyboard** (same input side as the other
keyboards). Design notes settled in conversation: one **shared** A/D/S/R for
the whole bank (per-key knobs would be 17×4 controls), each key an
**independent** envelope; and the fan-out he wants is free — the patch model
already allows many cables off one output port.

*Model* (`modules/cv_gates.py`). `CVGates(Module)`, `TYPE="cv_gates"`,
`ACCEPTS_COMPUTER_KEYS=True` (same flag the UI routes physical keys by, so it
plays alongside `keyboard`/`cv_keyboard`). Params attack/decay/sustain/
release only — **no `octave`**: pitch is meaningless for a gate bank, and
leaving it off means the UI's `params.get("octave",4)` falls back to 4 so
physical key *i* always lands on output *i*. Seventeen `cv` outputs named by
note (`c4`..`e5`), built from a data-driven `KEY_CV_NAMES` tuple. State is a
17-bool `_down` list under a lock; `note_on`/`note_off` map a routed MIDI
note to a key index via `midi - 60` and **ignore out-of-range** notes (keys
above/below the home row, like a short hardware keyboard). The envelope state
itself lives in the backend; the module only tracks which keys are held.

*Renderer* (`numpy_backend._render_cv_gates` + `_adsr_key_block`). Snapshots
the 17 held-flags once per block (block-constant gate, like every keyboard
renderer) and runs **17 independent ADSR state machines** sharing the four
params, one mono `(frames,)` cv buffer per jack. The inner loop
(`_adsr_key_block`) is the *exact* `_render_adsr_mono` state machine
(idle→attack→decay→sustain→release→idle) but with a single block-constant
gate bool instead of a per-sample buffer — so a key behaves identically to
patching a keyboard gate into a standalone ADSR (release-from-mid-attack
takes the full window; re-press mid-release attacks from the current level).
Per-key state kept in `self._state[id]["keys"]`, rebuilt on slot-count
mismatch; `compile()` already drops it on type-change/removal. **Idle-key
short-circuit:** a key that's up, idle, and at 0 returns a fresh zero buffer
without looping, so a bank with two keys held costs two envelopes, not 17.
The 17 cv outs get the auto CV meter for free; pyo path unaffected.

*UI* (`ui/app.py`). One TYPE-guarded `cv_gates` branch in `_add_param_widget`:
attack/decay/release as bounded 0..5 s sliders, sustain as a 0..1 slider
(the generic drag-float fallback would also work — that's what `adsr` uses —
but bounded sliders are nicer). Palette/outputs/meters are all automatic.
Verified headlessly with a stub `dearpygui` (no display in the sandbox):
confirmed each param routes to the right slider with the right bounds, and an
unrelated module's param still flows to its normal widget.

*Tests / example / docs.* 22 tests in `tests/test_cv_gates.py` — model
(ports, marker, key→index mapping + out-of-range ignore, snapshot-is-a-copy,
all-notes-off), envelope (idle zeros, attack reaches 1, settles to sustain,
sustain=1 has no decay dip, release to 0, retrigger-from-current-level,
per-key independence, distinct idle buffers, instant attack), dispatch, an
end-to-end `c4 → oscillator.amp_cv → speaker` amplitude test (silent→loud→
silent), the headline `one key → three oscillators` fan-out (summed through a
combiner, since the speaker takes one cable), and a save/load round-trip.
Full suite **703 passed, 18 skipped** in the sandbox — exactly +22 from 681.
Example `examples/cv_gates_amp.json`: press `A` (the `c4` key) and three saw
oscillators (C3/E3/G3) swell together as one chord via the shared ADSR.
`docs/MODULES.md` index row + full `#### cv_gates` entry.

**Hand-off to Matthew:** delivered as `cv_gates.patch`, `git am`-verified
clean on origin/main `3a5c576` (CV Keyboard), full suite green in the am'd
tree. To hear it: open `cv_gates_amp.json`, hold `A` — the three oscillators
attack/decay/sustain together; let go and they release. Re-patch any `c4`..`e5`
jack to any number of `amp_cv`/VCA inputs to gate a different sound per key.

**Next:** the obvious follow-ups — a `cv_reference`/octave-shift so the 17
keys can be moved off C4; a per-key *retrigger vs legato* choice; optional
exp/linear curve like the AD-envelope follow-up; and (if profiled hot with
many keys held) vectorising `_adsr_key_block` analytically since the gate is
block-constant.

## 2026-07-01 — CV Keyboard (`cv_keyboard`): keys as a CV/gate controller

Matthew's dream: "a keyboard like the existing module but all the keys are
CV outs for feeding to oscillators etc — for a different sound." So a
*controller*, not a sound source: the computer keys emit control voltage,
and the voice is built out in the patch (osc → filter → VCA → whatever).
Same keys, a different sound every patch — the canonical modular keyboard.

*Scoping (AskUserQuestion).* Three forks locked up front: (1) **output
shape** → **both** a unified 1V/oct `pitch_cv` + `gate` **and** per-key
gate jacks; (2) **voicing** → **voice-aware** (reuse the 16-slot
`VoiceSlots` model); (3) **new module** vs extending Keyboard → **new
dedicated module**. Matthew added "copy the existing if you want to" — so
the note-ingest is lifted from Keyboard.

*Model* (`modules/cv_keyboard.py`). `CVKeyboard(Module)` — `octave` is the
only param (no waveform/volume; it makes no sound). Note ingest
(`note_on`/`note_off`/`all_notes_off`/`snapshot_active_notes`/
`snapshot_voice_slots`) copied verbatim from Keyboard over its own
`VoiceSlots` + lock. Outputs: `pitch_cv` (cv), `gate` (gate), then twelve
per-pitch-class gate jacks `key_c`..`key_b` built from a data-driven
`KEY_GATE_NAMES` tuple so the port list and the renderer never drift.
`CV_REFERENCE_NOTE = 60` (C4 = 0 V).

*UI routing — the one shared touch.* Both Keyboard and CVKeyboard now carry
an `ACCEPTS_COMPUTER_KEYS = True` class marker, and the app's three key
handlers route by `getattr(module, "ACCEPTS_COMPUTER_KEYS", False)` instead
of `isinstance(module, Keyboard)` (the now-unused `Keyboard` import was
dropped). Decoupled — both keyboards can be in one patch and play together,
and any future computer-key source opts in with the same flag. `octave`
hits the existing generic int selector; `pitch_cv` (cv kind) gets the auto
CV meter for free; the gate jacks get none (correct).

*Renderer* (`numpy_backend._render_cv_keyboard`). No audio, no envelope, no
phase/last_note state — far simpler than `_render_keyboard`. Per voice slot:
`pitch_cv[i] = (note - 60) / 12` (held for any non-empty slot, **including a
released-but-tailing voice**, so an ADSR release stays on pitch; zeroes only
on slot reuse); `gate[i] = 1` while physically held. The twelve `key_*`
arrays are mono `(frames,)` booleans — high while **any** held voice is that
pitch class (octave-folded: C4 and C5 both raise `key_c`). pyo gets the
usual silent-stub (added to the punt tuple).

*Gotcha worth remembering.* The integration test first played `pitch_cv →
osc → speaker` with no VCA and read **C4**, not the note pressed: an
oscillator drones on **every** voice slot, and the 15 idle slots sit at
`pitch_cv = 0 = C4`, so 15 idle voices drowned out the 1 real one. That's
correct hardware behaviour — the gate/VCA is what articulates and silences
idle voices. Fixed the test (and the example) to gate a VCA, which is the
mandatory pattern; documented it in MODULES.md.

*Tests / example / docs.* 20 tests in `tests/test_cv_keyboard.py` (port
shape, the marker on both keyboards, 1V/oct values C3/C4/C5/G4/A4, per-voice
gate independence, pitch-class folding, release-holds-pitch-drops-gate,
all-notes-off, dispatch, and the FFT in-tune integration through a gated
VCA). Full suite **681 passing (+18 mido skipped)**, up from 661 — exactly
+20. Example `examples/cv_keyboard_external_voice.json`: `pitch_cv → saw
osc → ADSR/VCA → mixer`, and `key_c → a second ADSR/VCA on a noise burst →
mixer` — press any C to fire a snare alongside the pitched voice.
`docs/MODULES.md` index row + full `#### cv_keyboard` entry.

**Hand-off to Matthew:** delivered as `cv_keyboard.patch`, `git am`-verified
clean on origin/main `fdf597e`, full suite green in the am'd tree. To hear
it: open `cv_keyboard_external_voice.json`, play the home row (A = C4) — the
saw tracks pitch via `pitch_cv`, and pressing any C also triggers the noise
snare off `key_c`. Swap the oscillator for any voice chain for a different
sound.

**Next:** the per-key gates are 12 pitch classes — an absolute 17-key
(full home-row span) mode is an option; a `cv_reference` param to move 0 V
off C4; a mono/last-note priority mode for vintage single-oscillator leads;
velocity/aftertouch CV stays out of scope (computer keys can't express it —
that's MIDIInput's lane).
