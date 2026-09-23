# PySynthRack — Module Ideas Backlog

Written 2026-07-04. A menu of candidate modules, each spec'd to stand alone as a
work item. Not commitments — when one is picked, move a line into `TODO.md` and
build from the spec here. Grep `TODO-ARCHIVE.md` before adding new ideas (some
were shipped already).

Effort scale: **S** ≈ one patch · **M** ≈ 1–2 sessions · **L** = slice it
(multi-session, per working agreement).

## How to submit an item

Paste the preamble below plus one module spec as the task.

> Add a new module to PySynthRack (numpy backend first, headless tests).
> Follow docs/MODULES.md → "Adding a new module": module class in
> `src/pysynthrack/modules/` with `@register_module_type` and a `CATEGORY`
> ClassVar; renderer in `audio/numpy_backend.py` (multi-output renderers return
> a dict); silent TYPE stub in `audio/pyo_backend.py`. House invariants:
> single voice row bit-identical to mono; bit-exact passthrough at the neutral
> setting (mix=0 bit-exact dry for effects); block-size independence where
> feasible — if not exactly achievable, pin a tolerance and document why;
> per-voice state on stateful modules; voice-aware reads via
> `_input_buffer(..., collapse=False)`; pitch CV is 1V/oct with C4 = 0 V;
> gates are 0/1. Every new `*_cv` input gets an explicit depth param with
> units documented in the MODULES.md conventions table. Modules owning OS
> resources go in backend `compile()`/`stop()` hooks. Update docs/MODULES.md,
> WORKLOG.md and TODO.md in the same commit. Examples respect gain headroom.

## Index

Dynamics: `compressor` `limiter` `noise_gate` `transient_shaper` ·
Pitch/frequency: `ring_mod` `freq_shifter` `bitcrusher` ·
Character/space: `tape` `convolver` `freeze` `autopan` ·
CV tools: `cv_math` `cv_recorder` `quantizer` `slew` `pitch_detector` · Filters: `vowel` ·
Generative: `shift_random` `euclidean` `clock_divider` `bernoulli_gate` `burst` `arpeggiator` `chord` `chaos` `possibility_selector` `drift` ·
Voices: `fm_op` `pluck` `bowed` `wind` `modal` `granular` `kick_drum`/`snare_drum`/`hat_drum` `sampler` `organ` ·
Visual: `scope` `spectrum` ·
Planned run (2026-08-03): `logic` `mid_side` `octaver` · `matrix_mixer`
`vinyl` · `supersaw` `wavetable_morph` · plus quick hits at the end.

---

## Dynamics — the biggest hole in the rack

Nothing in the rack tracks level yet (`loudness` is spectral compensation, not
dynamics). These four share one core: a one-pole attack/release envelope
follower — the same recurrence `audio_to_cv` vectorized with the monotone
fixed-point solve, so the per-sample-loop problem is already solved in-house.

### `compressor` (M) — CATEGORY "Effects"

Feed-forward compressor with external sidechain.

- Ports: `in` (audio); `sidechain` (audio, normal to `in` when unpatched);
  `threshold_cv`; `out`; `gr` (cv out — applied gain reduction, 0..−1 scaled
  from dB, patchable for ducking/vis).
- Params: `threshold` −60..0 dB (−18) · `ratio` 1..20 (2; 1 = off, 20 ≈ limit)
  · `attack` 0.1..250 ms (10) · `release` 5..2500 ms (120) · `knee` 0..24 dB
  (6, soft quadratic) · `gain` 0..24 dB makeup (0) · `mix` 0..1 (1; <1 =
  parallel compression) · `detector` peak|rms (rms, ~10 ms window) ·
  `threshold_cv_depth` dB/unit.
- DSP: detector on sidechain → dB → gain computer (log domain, soft knee) →
  attack/release smoothing of the *gain* (rising/falling branch → monotone
  fixed-point vectorization) → linear multiply. Zero latency, so `mix` needs
  no compensation.
- Neutral: ratio=1 ∧ gain=0 ∧ mix=1 → short-circuit, bit-exact passthrough
  (skip the detector entirely).
- Tests: steady-sine static gain matches the gain law analytically; time
  constants hit 1−1/e; sidechain keying (kick ducks pad example); `gr` mirrors
  applied gain; block-size independence via the exact recurrence; voice ≡ mono.
- Stretch: lookahead (adds latency comp), program-dependent release, `ratio_cv`.

### `limiter` (M) — "Effects"

Brickwall lookahead limiter — the "demo can't clip" module.

- Ports: `in`, `out`. Params: `ceiling` −20..0 dBFS (−1) · `release`
  20..1000 ms (80) · `lookahead` 1..10 ms (5).
- DSP: sliding max over the lookahead window (`scipy.ndimage.maximum_filter1d`
  or monotonic deque — scipy already a dep) → gain needed to stay ≤ ceiling →
  attack spread across the lookahead so gain lands *before* the peak; one-pole
  release. Audio path delayed by lookahead; fixed latency, documented.
- Neutral: signal under ceiling → gain exactly 1.0 → output = delayed input;
  test the delayed passthrough bit-exact (resampler-unity precedent).
- Tests: never exceeds ceiling on impulse trains / 0 dBFS squares; release
  behavior; latency constant across block sizes.
- Stretch: true-peak via the shared 4x oversampling infra.

### `noise_gate` (S–M) — "Effects"

- Ports: `in`; `sidechain` (normal to `in`); `out`; `open` (cv out 0/1 — free
  gate-extractor for generative patching).
- Params: `threshold` −80..0 dB (−45) · `hysteresis` 0..24 dB (4; close
  threshold sits below open — schmitt semantics) · `attack` 0.1..50 ms (1) ·
  `hold` 0..500 ms (40) · `release` 5..2000 ms (150) · `range` −80..0 dB
  (−80 = full mute; higher = expander-ish).
- DSP: follower on key → Schmitt open/close + hold timer → target gain (1 or
  range) → attack/release smoothing.
- Neutral: threshold at min → always open → bit-exact.
- Tests: no chatter at boundary amplitude; hold honored; `open` matches the
  audible gating; voice ≡ mono.

### `transient_shaper` (M) — "Effects"

Attack/sustain rebalance, threshold-free (level-independent — the classic trick).

- Params: `attack` −1..+1 (0; maps to ±12 dB on the attack portion) ·
  `sustain` −1..+1 (0) · `speed` fast|med|slow.
- DSP: two followers (fast, slow) on |in|; their dB difference isolates
  transients; positive part drives attack gain, negative part sustain gain;
  smooth, multiply. Follower math = the shared fixed-point core.
- Neutral: attack=0 ∧ sustain=0 → exact 1.0 gain short-circuit, bit-exact.
- Tests: synthetic click+tail — attack knob moves click energy only, sustain
  the reverse; same shaping at −20 dB input (level invariance).

## Pitch & frequency mangling

### `ring_mod` (S) — "Effects"

- Ports: `in`; `carrier` (audio, normal to an internal sine when unpatched);
  `freq_cv`; `out`.
- Params: `freq` 1..5000 Hz (440, internal carrier) · `freq_cv_depth` oct/unit
  · `mix` 0..1 (1).
- DSP: out = in × carrier. Internal carrier = per-voice phase-accumulated sine
  (deterministic phase → testable waveforms).
- mix=0 bit-exact dry. One-afternoon module; pairs with `fm_op` and `modal`.

### `freq_shifter` (M) — "Effects"

Bode-style single-sideband shift: every partial moves by the same **Hz**
(inharmonic clang, barberpole) — a different animal from `pitch_shifter`'s
ratio shift.

- Ports: `in`; `shift_cv`; `out_up`; `out_down`.
- Params: `shift` −2000..+2000 Hz (0) · `shift_cv_depth` Hz/unit (200) ·
  `mix` 0..1 (1) · `feedback` 0..0.9 (0, from up output — barberpole).
- DSP: analytic signal via FIR Hilbert pair (scipy.signal design, ~255 taps;
  group delay ≈ 2.9 ms — latency-compensate the dry for `mix`, house pattern)
  × complex exponential; up = one sideband, down = the conjugate. Quadrature
  phase carried per voice across blocks.
- Neutral: mix=0 bit-exact dry; at shift=0 the wet is the Hilbert-delayed
  input — pin phase-coherent blending with the latency-comped dry.
- Tests: sine f0 shifted by s → single FFT peak at f0+s, opposite sideband
  rejected > 40 dB; `out_down` at f0−s; feedback stability bound.

### `bitcrusher` (S) — "Effects"

- Params: `bits` 1..24 (24) · `rate_div` 1..64 (1, sample-hold decimation) ·
  `jitter` 0..1 (0, random hold-length wobble, seeded) · `mix` · `dc_filter`
  on|off.
- DSP: mid-tread quantize round(x·2^(bits−1))/2^(bits−1); decimate = hold every
  Nth sample, deliberately aliased (that is the sound). Vectorized hold via
  index arithmetic (arange//N); jitter via cumulative hold lengths +
  searchsorted. Hold phase carried across blocks.
- Neutral: bits=24 ∧ rate_div=1 → both ops skipped → bit-exact.
- Tests: quantization step exact; hold pattern exact incl. across block joins;
  bits=1 sanity; jitter seeded reproducible.

## Character & space

### `tape` (M–L) — "Effects"

Wow/flutter/saturation/hiss in one "put it on tape" pass.

- Params: `wow` 0..1 (0, ~0.5–2 Hz depth) · `flutter` 0..1 (0, ~6–15 Hz +
  noise) · `drift` 0..1 (0, slow random walk) · `sat` 0..1 (0, tanh drive on
  the shared 4x oversampling infra) · `hiss` off..−30 dB (off) · `bump` 0..6 dB
  (0, RBJ low shelf ~60 Hz head bump) · `mix`.
- DSP: modulated fractional-delay line (reuse the chorus core) driven by
  wow+flutter+drift sum → saturation → hiss add → shelf. Fixed nominal delay
  (~10 ms) → latency-comped dry for `mix`.
- Neutral: everything at zero → delayed passthrough bit-exact (or full bypass
  short-circuit — pick one, test it).
- Tests: wow depth → measurable pitch deviation (resampler pitch-test
  machinery); saturation THD monotone in `sat`; hiss level calibrated;
  block-size independent (chorus precedent).
- Stretch: Poisson dropouts; stereo azimuth error; `vinyl` sibling (see quick
  hits) as a follow-up S module.

### `convolver` (L — slice it) — "Effects"

IR loader + partitioned FFT convolution: real rooms, springs, plates, cabs.

- Ports: `in`; `out_l`/`out_r` (stereo when the IR is). Params: `gain` ·
  `predelay` 0..500 ms · `tone` LP 1k..20k (off at max) · `mix` · IR Browse
  button (FilePlayer Browse + media.py ffmpeg decode; IRs load whole — no
  streaming needed).
- DSP: uniform partitioned overlap-add (rfft blocks at render block size),
  frequency-domain accumulate; one-block latency → latency-comped dry.
  Normalize IR on load; cap length by DSP budget (~2–5 s to start; the DSP %
  readout is the meter for this).
- Slices: (1) mono fixed-block core, oracle-tested vs scipy fftconvolve;
  (2) IR file load + stereo; (3) predelay/tone/normalize + license-clean
  example IRs in examples/.
- Neutral: unit-impulse IR ∧ mix=1 → passthrough within 1e-6 (FFT roundtrip
  isn't bit-exact — pin and document); mix=0 bit-exact dry.
- Tests: oracle equivalence per block size; latency reported; tail length
  matches IR.

### `freeze` (M) — "Effects" — **SHIPPED 2026-09-20** (see TODO.md / WORKLOG.md) — the spectral freeze — **module #100**

The keep-list's "spectral freeze: FFT a moment, hold it forever as a
pad. A different animal from `granular`'s time-domain freeze." That one
loops a slice of TIME (grains re-read a ring — a rhythm, a stutter, a
cloud); this one holds a SPECTRUM: the moment's frequencies and their
levels, re-synthesised indefinitely with the phases advancing at each
partial's true rate, so a chord becomes a stationary pad with no loop
seam and no rhythm at all — the sound of the Freeze pedal, of Clouds'
spectral mode, of Norris' Spectral Freeze. Play over it: the dry keeps
passing.

- Ports: `in` (audio, mono — a `(V, F)` source is the house sum);
  `freeze` (gate — rising edge captures, high holds; a `(V, F)` gate
  collapses any-voice-high); `pitch_cv` (cv, 1 V/oct × `pitch_cv_depth`,
  block mean) → `out` (audio).
- Params: `size` 1024 | 2048 | 4096 | 8192 | 16384 (4096 — the FFT
  window in samples; the resolution knob, see below) · `freeze` tickbox
  (off — ORed with the gate, the `granular` precedent) · `smear` 0..1
  (0.0 — 0 is the coherent phase-vocoder hold: a sine freezes to the
  same sine; 1 randomises every frame's phases: the classic spectral
  wash, the tone's identity kept, its coherence gone) · `pitch`
  semitones −24..24 (0.0 — the frozen layer's transposition, exact) ·
  `pitch_cv_depth` oct/unit (1.0) · `level` 0..1 (0.7 — the frozen
  layer's level) · `dry` 0..1 (1.0 — the live input's level; NOT a
  `mix`, because a freeze is something you play OVER: engaging it must
  never duck the dry) · `fade` ms 1..2000 (60 — the layer's rise at the
  edge and its fall at release; also the crossfade between two
  freezes) · `seed` (1 — the smear's die).
- DSP — capture: the last `size + hop` input samples before the edge
  (a rolling history, always kept), Hann-windowed into two frames one
  hop (`size/4`) apart; magnitudes from the later frame; each bin's
  TRUE frequency from the pair's phase difference (`w_bin +
  princarg(dphi − w_bin·hop)/hop`, the phase-vocoder estimate) so a
  partial between bins holds at its real pitch and does not beat.
  Synthesis: frame j = `mag · exp(i(phi0 + j·w_true·hop [+ smear ·
  jitter_j]))`, inverse FFT, synthesis Hann, overlap-add at the hop —
  Hann² at 75% overlap sums to exactly 1.5, divided out, so a
  stationary input freezes at unity. `jitter_j` is `default_rng([seed,
  j])`, keyed by the frame index (the granular's grain-index idiom), so
  the wash is reproducible and block-size exact. Frames are generated
  in order on demand into a rolling synthesis buffer; the additions
  happen in frame order whatever the block partition, so the stream
  is bit-exact across block sizes.
- DSP — pitch: NOT by resampling the spectrum (that widens every lobe
  and loses ~4 dB an octave up — measured); the frozen stream is
  stationary, so a pitch shift is simply reading it at rate `r =
  2^(pitch/12 + depth·mean cv)` (exponent clipped to ±4 octaves before
  the power) with linear interpolation — exact frequency, unity level
  (measured 659.26 / 220.00 / 880.00 Hz at 0.500 for +7 / −12 / +12).
  Bins above Nyquist/r are zeroed at frame synthesis when r > 1 (no
  aliasing). The read position is an integer count of output samples
  times r, rebased when r changes, so a constant ratio is bit-exact
  across block sizes and a ratio change never jumps.
- DSP — the layer: the gate (OR the tickbox) drives one integer-count
  `_gate_ramp_env` (attack = release = `fade`); the frozen stream
  times `env · level` is ADDED to `src · dry`. A rising edge while a
  layer is still sounding (a chord change under a held pedal, or a
  re-trigger mid-release) captures a NEW layer and forces the old one
  into its release from its current level — the two crossfade over
  `fade` — so a re-freeze melts, never cuts; at most four layers live,
  the oldest dropped beyond that. When a layer's envelope reaches 0
  its synthesis state is dropped. Unfrozen with no layer alive the
  render returns `src` itself at `dry` 1.0 (bit-exact passthrough,
  the effects neutral) while the history keeps rolling.
- The resolution rule (measured, drives the default): partials closer
  than about four bins (`4·sr/size` Hz) share lobes, their bins'
  true-frequency estimates disagree, and the hold loses them — a
  C-E-G triad (68 Hz apart) at `size` 1024 vanishes, 2048 loses a
  partial (−2.6 dB on the lowest), 4096 holds within 1%, 8192 exactly.
  Bigger `size` = a longer moment captured (93 ms at 4096, 372 ms at
  16384 — an average of a third of a second) and cleaner harmony;
  smaller = a snappier grab. `smear` 1 drops the level ~5 dB (the lobe
  bins no longer add in phase) — `level` is the makeup.
- Neutral / contracts: unpatched `in` → silence, no state; unpatched
  `freeze` with the tickbox off → `src` (at `dry` 1.0 the same buffer);
  a gate that never rises == no gate, bit-exact; `pitch_cv` +1 at depth
  1 == `pitch` +12 bit-exact; block-size exact with rising and falling
  edges mid-stream at a constant ratio (a MOVING `pitch_cv` is
  block-mean like every other block-mean CV — documented).
- Tests: registration/ports/params; the sine hold (frequency, unity
  amplitude, steady RMS over 3 s, still there at 5 s with the input
  gone silent — "forever"); a non-bin sine does not beat; the triad
  at 4096 within 10% per partial; every `size` holds; `smear` 1 keeps
  the peak bin, decorrelates the waveform (corr < 0.3) and reproduces
  per seed; `pitch` +12 → 880 at unity; the cv equivalence; the
  clip; `fade` measured (half level at half the fade; release to 0
  and the state dropped); the re-freeze crossfade (old → new, no
  click by the house step tripwire, ≤ 2 layers); tickbox == gate;
  never-rising == no cable; `dry` 0.5 halves the passthrough; a
  `(V, F)` input sums; block-size independence 64 vs 512 with edges
  mid-stream at pitch +7, smear 0.5; finite under absurd params;
  widget sweep; the example.
- Example `freeze_chord_pad.json`: a bar clock at `pulse_width` 0.25
  plays four chords on the organ (sequencer → chord → organ) for a
  second each; the clock's `not_a` through `logic` is the freeze gate
  — it rises exactly when the chord's gate FALLS, so the capture is
  the chord's sustain, held as a glassy pad for the rest of the bar
  and melting into the next chord's freeze on the following bar; a
  little reverb. `smear` 0.25, `size` 4096.
- Follow-ons: stereo `width` (two smear seeds, L/R decorrelated); a
  `spread` (formant-preserving shift); a `decay` (a hold that fades
  on its own); `hold` as a latch/toggle mode.
- As built (2026-09-20), verbatim plus one addition the third
  prototype forced: the true-frequency estimate is **phase-locked to
  the spectral peaks** (identity locking) — without it the hold of
  close partials decays over seconds; with it a triad at 4096 holds
  within 2% forever. The read starts at frozen time `size` (the edge
  itself), so a stationary source's hold is in phase with the live
  input. 35 tests. Example `freeze_chord_pad.json`.

### `autopan` (S) — "Routing & VCA" — the stereo motion utility — **module #101**

The keep-list's "there's no dedicated panner anywhere in the rack.
Equal-power pan with CV in; fold tremolo into it". The stereo speaker
sink can already be panned by a cable, but only at the very end of the
chain and only by building the LFO yourself; this is the panner as a
module — before the delay, before the reverb, with its own LFO, synced
to the clock if you like. Two VCAs and a law, in other words.

- Ports: `in_l`, `in_r` (audio; the `mid_side` / stereo-sink pair; a
  `(V, F)` source is summed — panning is linear, so panning the mix IS
  panning every voice), `pan_cv` (cv, **per sample**, added 1:1 to the
  position; a `(V, F)` source is averaged, the sink's rule), `rate_cv`
  (cv, block mean, 1 V/oct × `cv_depth` on `rate`), `clock` (gate) →
  `out_l`, `out_r` (audio).
- Two source modes, decided by what is cabled (the sink's rule, made
  symmetric): **one** input cabled (either jack) = a MONO source,
  PLACED by `law`; **both** cabled = a STEREO pair, BALANCED — unity at
  centre, the far side fades with the sink's cosine taper
  (`gL = cos(max(p, 0)·π/2)`, mirrored), `law` does not apply.
- Params: `pan` −1..1 (0.0 — the manual centre the LFO swings around)
  · `depth` 0..1 (0.7 — LFO swing in pan units; 0 = a static panner)
  · `rate` 0.01..20 Hz (0.5) · `shape` sine | triangle | square (sine)
  · `tremolo` 0..1 (0.0 — see below) · `law` power | compromise |
  linear (power = −3 dB centre, sin/cos, constant power; compromise =
  −4.5 dB, the geometric mean of the other two; linear = −6 dB,
  constant amplitude) · `division` 0.25..64 (4.0 — clock ticks per full
  L→R→L cycle, only while `clock` is patched) · `cv_depth` (1.0 octaves
  per unit on `rate_cv`).
- Position: `p = clip(pan + pan_cv + depth·lfo, −1, 1)`; mono power
  law `θ = (p+1)·π/4`, `(cos θ, sin θ)` — exactly the stereo sink's
  placement, so an autopan into the sink at pan 0 depth 0 = the sink.
- **Tremolo folded in** as the phase between the two sides: the right
  channel's position reads the LFO `tremolo/2` cycles later than the
  left's. 0 = both sides read the same position (autopan); 1 = half a
  cycle apart, and for an odd-symmetric shape the right's position is
  the left's mirrored, so the two gains are EQUAL and move together (a
  mono tremolo, from 0 to unity around the −3 dB centre); between = the
  swirl, part pan part throb. Constant power holds at `tremolo` 0 only
  (documented — a tremolo is supposed to change the level).
- LFO: phase keyed to an ABSOLUTE integer sample count — `phase[n] =
  (φa + (n − na)·rate/sr) mod 1`, re-anchored only when the effective
  rate changes (a knob move or a moving `rate_cv`), never accumulated,
  so a constant-rate render is bit-identical at every block size.
  `clock` patched and locked hands the phase to `_mod_clock_sync` (the
  phaser/flanger helper: one cycle per `division` ticks, absolute-sample
  anchor). Shapes start at 0 rising: `sin 2πφ`; the triangle
  `1 − 4|((φ+¼) mod 1) − ½|`; the square = `sin(π/2 · clip(K·tri, −1,
  1))` with `K = max(1, 1/(2·rate·T))`, `T` = 10 ms — the switch
  between sides is a raised-cosine glide lasting T at every rate (the
  pan moves across it exactly as a 50 Hz sine would, never faster), and
  above 50 Hz-equivalent it degrades to a rounded triangle.
- Neutral / contracts: stereo pair at pan 0 depth 0 = the inputs to the
  bit; nothing cabled = silence (the LFO still keeps time); `rate_cv`
  NaN reads as unpatched (`_finite_mean`), a NaN sample on `pan_cv`
  reads 0 at that sample; `(V, F)` in = the summed render.
- Tests: registration/category; constant power (L²+R² = x² for a sine
  at every pan, power law); the three centre levels (−3.01 / −4.52 /
  −6.02 dB); hard pan = silence opposite; balance mode unity at centre
  bit-exact and the far side fades; depth 0 = static; the sine LFO's
  recovered position = `depth·sin`; the triangle's corners;
  `rate_cv` +1 == the knob doubled bit-exact; block-size exactness at
  64/128/512/1000 over ≥ 4 s (square, tremolo, a per-sample `pan_cv`
  and a constant `rate_cv`); clock-synced cycle length = `division` ×
  the clock's interval, block-size exact; the square's steepest step
  vs a sine's at the same rate (≈ K×, and far below a hard switch);
  tremolo 1 = equal gains; NaN on both CVs finite; poly = summed;
  widget sweep; the example.
- Example `autopan_pluck_bounce.json` — a clocked pluck line through
  the autopan synced to the same clock (a square that throws alternate
  bars' notes across the field… or a triangle over two bars), then a
  stereo delay/reverb so the echoes land where the notes were thrown.
- Follow-ons (not in this build): a `random`/smooth-random shape (needs
  a cycle counter carried through the clock lock); a `phase` offset for
  where the clocked cycle starts on the downbeat (needs a reset edge
  the helper does not have); a separate tremolo rate; `width` on the
  stereo pair (the sink and `mid_side` have it).

## CV tools & bridges

### `cv_recorder` (M) — "CV & Utilities" — **SHIPPED 2026-09-19** (see TODO.md / WORKLOG.md) — the modulation looper

The keep-list's "record a knob gesture or incoming CV for N clocked bars,
loop, overdub" — nothing else in the rack captures *performance*. A
fixed-length looper (the N-bars kind, not the free-length kind): the
loop's length is a setting, `rec` writes into it, the loop plays from the
moment it exists, `clear` wipes it.

- Ports: `in` (cv — the signal to record; UNPATCHED, the module's own
  `value` knob is the input, so a hand on the slider while `rec` is high
  is the recorded gesture), `clock` (gate, optional — see below), `rec`
  (gate — record while high), `clear` (gate — rising edge wipes the loop
  and rewinds; the position then HOLDS at 0 until the next `rec`) →
  `out` (cv — the loop; while recording, what is being written), `pos`
  (cv — the loop position as a 0..1 ramp; 0 while no loop exists).
- Params: `length` 0.05..60 (4.0 — seconds free-running, CLOCK TICKS
  while `clock` is patched: the slew v2 "s | x clock" convention) ·
  `mode` replace | overdub (overdub) · `feedback` 0..1 (1.0 — in overdub,
  the existing layer is scaled by this before the new input is added:
  1 = layers pile up, 0.5 = each pass halves what was there) · `value`
  −1..1 (0.0 — the gesture knob, read once per block and RAMPED
  linearly across it from the previous block's value so a recorded turn
  has no steps).
- DSP: a float64 buffer of `L` samples (`L = round(length·sr)`
  free-running; `round(length·interval)` clocked, with the clock's
  period measured from its last two rising edges the way slew/euclidean
  do — fixed when the loop is created). The loop "exists" from the first
  `rec` edge: the position ramp starts at 0 then and runs forever
  (`pos = p / L`) until `clear`. Per sample while `rec` is high:
  `replace` → `buf[p] = x`; `overdub` → `buf[p] = feedback·buf[p] + x`;
  `out = buf[p]` (the written value) — while `rec` is low `out = buf[p]`
  (silence before anything was recorded). Vectorized per segment between
  events (rec edges, clock ticks, wraps), so cost is nil. CLOCKED: `rec`
  on AND off edges are honoured on the NEXT clock tick (quantised
  punch-in, the looper norm — press slightly late and the loop still
  starts on the bar) and the position hard-syncs to 0 on every
  `length`-th tick counted from the loop's start, so the loop stays in
  phase with the transport (a tempo change crops or wraps the fixed
  buffer until the next sync — documented, not fought). Mono; a `(V, F)`
  `in` collapses (house sum).
- Neutral / contracts: nothing patched → `out` 0, `pos` 0, no state
  growth; `in` patched but never `rec`'d → 0 (the looper is not a
  pass-through — a `cv_combiner` is); block-size independent when `in`
  is patched (events at integer sample positions); the knob path is
  block-rate by nature (documented).
- Tests: registration; nothing-patched contract; the first pass records
  a ramp and the second pass plays it back bit-exact; `out` during rec
  is the written content; replace vs overdub (feedback 1 sums, 0.5
  halves the old layer; replace punches in only where rec was high);
  rec low → playback continues; `clear` wipes, rewinds and holds until
  the next rec; `pos` is a 0..1 ramp of period L; clocked: L =
  length·interval, rec edges land on the next tick, the loop re-syncs
  at the boundary tick; knob path ramps across the block; block-size
  independence (64 vs 512) with a patched in and rec edges mid-stream;
  the `mode` combo offers replace/overdub (the shared-branch trap);
  widget sweep; the example.
- As built, verbatim plus one rule found in the building: a clocked rec
  edge that would CREATE the loop waits for the clock's second tick
  (the period is unknown before it, so the buffer cannot be sized) —
  rec held high from t=0 starts the loop one tick in. 18 tests.
  Example `cv_recorder_layers.json`.

### `cv_math` (S) — "CV & Utilities" — **SHIPPED 2026-09-19** (see TODO.md / WORKLOG.md) — `logic` for CVs

The keep-list's "logic-for-CVs, zero params": two CV operands in, every
two-operand function the rack lacks out at once, no mode combo — you
swap cables, not settings, exactly like [`logic`](#logic). Filed under
CV & Utilities beside `cv_scale` / `cv_offset` (the pointwise CV tools),
not Modulation where the one-liner sat.

- Ports: `a`, `b` (cv in) → `min` (the analog AND), `max` (the analog
  OR), `avg` ((a+b)/2 — the equal-power blend), `diff` (a − b), `mult`
  (a × b — the CV×CV multiplier the rack has no other jack for: an
  envelope on a vibrato's depth), `rect` (|a|, full-wave), `inv` (−a).
  All cv out, all live every block.
- Params: none.
- DSP: elementwise, stateless, exact. An unpatched operand reads **0**,
  which makes `max`/`min` with only `a` patched the positive / negative
  half-wave rectifiers, `avg` = a/2, `diff` = a, `mult` = 0 — the
  normalled trick, documented like `logic`'s NAND. Shape-polymorphic:
  mono in → mono out; a `(V, F)` operand → `(V, F)` outs with a mono
  partner broadcast across the voice axis (numpy broadcasting; single
  voice row ≡ mono).
- Neutral: both unpatched → all zeros; `diff`/`rect`/`inv`/`max`/`min`
  of a lone `a` are exact functions of it (bit-exact `diff` == `a`).
- Tests: registration / zero params / round trip; every output against
  numpy on random operands (exact); unpatched-`b` identities; both
  unpatched zeros; poly × mono broadcast and single-voice ≡ mono;
  float32 out; stateless (two renders equal, no `_state` entry); the
  example (delayed vibrato via `mult`, a two-LFO `max` on a filter).
- As built, verbatim. 13 tests. Example `cv_math_delayed_vibrato.json`.

### `vowel` (S–M) — "Filters & EQ" — **SHIPPED 2026-09-19** (see TODO.md / WORKLOG.md) — the formant filter

The keep-list's "formant filter bank with an A–E–I–O–U morph knob — the
vocoder's expressive little cousin; pairs beautifully with `supersaw`".
Five parallel resonant bandpasses at the formant frequencies of a sung
vowel, the classic five-formant table (the one every formant synth and
Csound's manual carry: F1–F5 frequency, level in dB and bandwidth for
A E I O U, per voice type), and a knob that slides between the vowels.

- Ports: `in` (audio, voice-aware like `filter`), `vowel_cv` (cv, block
  mean, vowels per unit × `cv_depth`) → `out` (audio).
- Params: `vowel` 0..4 (0.0 — 0 = A, 1 = E, 2 = I, 3 = O, 4 = U, and
  everything in between) · `voice` soprano | alto | countertenor |
  tenor | bass (tenor) · `resonance` 0.25..4 (1.0 — a multiplier on
  every formant's Q, 1 = the table's bandwidths, higher = narrower,
  more vowel, more ring) · `gain` dB −12..24 (6.0 — makeup; a formant
  bank passes only what sits near its peaks, so the wet is ~15 dB down
  on a saw) · `mix` 0..1 (1.0; 0 = bit-exact dry — the effects
  neutral) · `cv_depth` (2.0 vowels per unit).
- DSP: `vowel_formants(voice, v)` — a pure helper in the module — takes
  the two neighbouring table rows and interpolates: frequencies
  geometrically, bandwidths linearly, levels linearly in dB; the
  renderer builds five RBJ constant-peak bandpasses (Q = F/BW ·
  `resonance`) from it once per block (coefficients cached on the
  (voice, vowel_eff, resonance) key), runs them with `lfilter` and
  carried `zi` (per voice row for a `(V, F)` input — one call per
  formant along the last axis), sums them with the table's linear
  gains, applies `gain`, then `out = dry·(1−mix) + wet·mix`. At `mix`
  0 nothing runs. The table lives in the module file as data
  (`FORMANTS[voice][vowel] = (freqs, dBs, bandwidths)`).
- Neutral / contracts: `mix` 0 bit-exact dry; unpatched `in` → silence,
  no state; `vowel` clamps 0..4, so a CV past U holds U; single voice
  row ≡ mono; block-size independent at a constant vowel (lfilter zi).
- Tests: registration; the table's shape (5 voices × 5 vowels × 3 × 5,
  F1 < F2 < … everywhere, level 0 dB on F1); the helper's geometric
  midpoint (tenor A→E at 0.5: F1 = √(650·400)); white noise through
  each vowel puts the spectral peak within ±15% of that vowel's F1 and
  a local maximum near F2; `vowel_cv` +1 at depth 2 == `vowel` + 2
  bit-exact; `resonance` sharpens (peak/skirt monotone); `gain` +6 dB
  doubles; `mix` 0 bit-exact dry, 0.5 = the half blend; voice-aware
  `(V, F)` in/out and single row ≡ mono; block-size independence; the
  clamp; the voice combo offers the five voices; widget sweep; the
  example (`vowel_talk.json` — a supersaw through the vowel with a
  slow LFO on `vowel_cv`: the talking pad).
- As built, verbatim. 22 tests. Example `vowel_talk.json` (makeup 14 dB
  on the supersaw — the bank really is ~15 dB down).

### `quantizer` (M) — "CV & Utilities" — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

CV in → nearest allowed pitch out. The missing link between
random/LFO/sequencer and *melody*.

- Ports: `in` (cv); `gate` (optional — when patched, sample-and-quantize on
  rising edges only); `out` (cv); `changed` (gate out, fires per new note).
- Params: `root` C..B (C) · `scale` combo (chromatic, major, natural/harmonic
  minor, pent maj/min, dorian, mixolydian, blues, whole-tone, custom) ·
  custom = 12 tickboxes (fader_seq precedent) · `hysteresis` 0..50 cents (10)
  · `transpose` −24..+24 st (0).
- DSP: allowed-note table across ±5 oct; nearest neighbor with a hysteresis
  band around the previous pick (kills boundary flutter). Continuous mode
  vectorized via searchsorted; gated mode per-edge. Voice-aware (V,F) in/out.
- Neutral: chromatic + hysteresis 0 + transpose 0 = semitone rounding — NOT
  passthrough; document that as the intended neutral.
- Tests: pitch-class membership exhaustive per scale; synthetic wobble at a
  boundary stays put; `changed` fires once per note; gated mode holds between
  edges.

### `slew` (S) — "CV & Utilities" — **SHIPPED 2026-07-18** (as built: shape linear|exponential, rise/fall in seconds; the `eoc` out and `link` didn't ship — see TODO for the v3 idea)

Slew limiter / portamento.

- Ports: `in` (cv); `out`; `eoc` (gate out when target reached — makes it a
  function-generator seed).
- Params: `rise` 0..5000 ms/V (50) · `fall` 0..5000 ms/V (50) · `curve`
  linear|expo · `link` tickbox (fall follows rise).
- DSP: slope-limited ramp toward the input — another monotone recurrence;
  within a block the target is piecewise-stepped, so segment hit-times solve
  analytically (audio_to_cv playbook).
- Neutral: rise=0 ∧ fall=0 → bit-exact passthrough.
- Tests: step input → exact ramp duration; expo tau; example patch
  cv_keyboard → slew → cv_to_frequency → osc (glide).

### `pitch_detector` (M–L) — "CV & Utilities"

Audio → pitch bridge: sing/whistle into `mic_input`, play the rack.

- Ports: `in` (audio); `pitch_cv` (out, 1V/oct); `gate` (voiced); `level`
  (cv out, follower).
- Params: `range_low`/`range_high` 60..2000 Hz bounds · `confidence` 0..1
  (0.85) · `glide` ms on pitch out (10).
- DSP: hop-based (512 hop / 2048 window) autocorrelation/NSDF —
  `pitch_shifter._detect_period` is the in-house seed; parabolic peak interp
  for sub-Hz accuracy; hold last pitch while unvoiced (gate low). Latency ≈
  one window, documented.
- Tests: sines + saws across range within ±3 cents; octave-error guard (strong
  2nd harmonic case — the classic NSDF trap); noise → gate low, pitch held.
- Killer example: mic → pitch_detector → quantizer → cv_to_frequency → osc
  (+ vocoder on the voice itself) = autotune-adjacent instrument.

## Generative & clockwork — "Modulation"

The clock → sequencer chain plays itself; these make it *surprise* you.
All clocked modules share `clock`-edge semantics with the existing clock/
sequencer pair, and all randomness takes a `seed` param (deterministic when
seeded — testable, and patches recall their character).

### `shift_random` (S–M) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

Looping shift-register random CV — the generative classic.

- Ports: `clock` in; `write` gate in (optional force-write); `cv` out;
  `gate` out (register bit 0).
- Params: `probability` 0..1 (0 = locked loop, 1 = coin-flip each step — the
  money knob) · `length` 2..16 (8) · `range` 0..5 V (2) · `bipolar` tickbox ·
  `seed`.
- DSP: 16-bit register; on each clock rising edge rotate by one; with prob p,
  flip the incoming bit. CV = register byte / 255 × range (plain binary —
  document the mapping).
- Tests: p=0 loops exactly every `length` steps; p=1 distribution sanity;
  seeded reproducibility; edge detection consistent with `sequencer`'s.
- Ships with an example: clock → shift_random → quantizer → osc = endless
  melody box.

### `euclidean` (S) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

- Ports: `clock` in; `reset` in; `gate` out; `accent` out (second layer).
- Params: `steps` 1..32 (16) · `fills` 0..steps (4) · `rotate` 0..steps−1 (0)
  · `accent_fills` (0) · `gate_len` fraction of step (0.5).
- DSP: arithmetic Bjorklund — pattern[i] = (((i+rotate)·fills) mod steps) <
  fills; no recursion. Step counter on clock edges; reset realigns.
- Tests: canonical patterns verbatim (E(3,8) tresillo = 10010010, E(5,8),
  E(4,16)); rotation; reset phase; gate length across block joins.

### `clock_divider` (S) — **SHIPPED 2026-09-11** (see TODO.md / WORKLOG.md)

- Ports: `clock` in; `reset` in; outs `div2` `div4` `div8` + `divn` (param n)
  + `mult` (×m, period-estimate based — document as approximate during tempo
  changes).
- Params: `n` 1..32 (3) · `m` 2..4 (2) · `swing` 0..75% on divn (delays every
  2nd emitted gate) · `pw` gate width fraction.
- Tests: division counts exact over 1000 edges; swing timing; mult tracks a
  tempo ramp within one period; reset realigns all counters.
- Built to the spec verbatim; `swing` is a fraction of the divn period
  (0.33 = triplet), `pw` a fraction of each output's own period.

### `bernoulli_gate` (S) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

- Ports: `in` (gate); `p_cv`; `out_a`; `out_b`.
- Params: `probability` 0..1 (0.5, chance of A) · `mode` independent|toggle ·
  `seed`.
- Each incoming gate routes whole (decision latched on the rising edge, gate
  length preserved).
- Tests: p=0 / p=1 degenerate exactness; seeded sequence reproducible;
  count(A) + count(B) = count(in) — nothing lost or doubled.

### `burst` (S) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

Ratchet generator: one trigger → N gates.

- Ports: `trigger` in; `clock` in (optional — when patched, rate = clock
  division); `gate` out; `env` cv out (per-burst amplitude taper — patch to a
  VCA for decaying ratchets).
- Params: `count` 1..16 (3) · `rate` 0.5..50 Hz (8, ignored when clocked) ·
  `division` (clocked mode) · `decay` 0..1 (taper) · `spread` −1..+1
  (accel/ritard curve).
- Tests: exact gate count; timing grid; retrigger mid-burst restarts
  (document); clocked division correct.

### `arpeggiator` (M) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

Sits between a poly note source and a mono voice: collapses held notes into a
clocked line. First "poly→mono collapser" — a nice exercise of the voice
architecture in reverse.

- Ports: `pitch_cv` in (voice-routed poly); `gate` in (voice-routed);
  `clock` in; `reset` in; outs mono `pitch_cv` + `gate`.
- Params: `mode` up|down|updown|order|random (seeded) · `octaves` 1..4 (1) ·
  `gate_len` 5..95% (50) · `hold` tickbox (latch after release).
- DSP: per block, scan active voice rows (gate high) for the held set; rebuild
  the sorted note list on change; advance on clock edges.
- Tests: chord {C,E,G}, mode up → exact CV sequence; add/remove notes mid-arp;
  hold latch; octave spans; works from both cv_keyboard and midi_input.

### `chord` (M) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

Mono pitch in → poly voices out; the 16-slot voice architecture as an
instrument.

- Ports: `pitch_cv` in (mono); `gate` in; outs `pitch_cv` (V,F — 4 active
  rows) + `gate` (V,F).
- Params: 4 interval slots −24..+24 st (defaults 0/4/7/12) with enable
  tickboxes · `preset` combo (maj, min, 7, m7, maj7, sus2, sus4, dim, aug, 5,
  custom) · `strum` 0..200 ms (0, staggered gate onsets) · `spread` tickbox
  (alternate voices ±1 oct).
- Tests: emitted rows at exact semitone offsets; strum stagger
  sample-accurate; the 4 rows ≡ 4 independent mono renders; example patch
  minds headroom at the mono sink (4-voice sum!).

### `possibility_selector` (M) — "Modulation" — **SHIPPED 2026-09-18** (see TODO.md / WORKLOG.md)

The possibility bridge's second brick, the *meta* one: a register over
WHICH module fires. Where `possibility_seq` holds bits with holes in them
(whether), this holds **routes** with holes in them (which). Sketched on
TODO 2026-08-15 ("collapse the router itself"), earned its spec once the
first module had been played, built to it the same day.

- Ports: `in` gate (the gate to route); `clock` gate (step advance —
  unpatched, `in`'s own edges step it: the distributor reading); `reset`;
  `reroll`; outs `out1`..`out4` gate (high while `in` is high and the
  step routes there; nothing before step 1). `in` unpatched → the clock
  itself is routed.
- Params: `steps` 1..16 · `mode` loop/latch/dice (possibility_seq's) ·
  `balanced` · `seed` · `weight1..4` 0..1 (how the open steps lean; all
  equal = fair; 0 removes that output from every `?`) ·
  `step{i}_state` = the step's candidate set: `"0"` rest, `"1"`–`"4"`
  decided, `"?"` all four, a digit subset (`"13"`) some.
- DSP: possibility_seq's take model verbatim (decided steps read live;
  open steps memoized per take in loop/latch, cleared on wrap / reroll /
  seed; dice never memoizes) with `resolve_route` in place of
  `resolve_step`: a weighted draw over the candidates (one uniform,
  cumulative walk), or under `balanced` for a *fair* step the least-used
  candidate (PBP's `RandomGeneratorPerfect` selector, width 1, ties by
  the die). Pure reference `collapse_routes`; the renderer is pinned
  against it. Possibility count = product of each open step's choices.
- Panel: the possibility panel one level up — cells coloured per route,
  click cycles `0→1→2→3→4→?→0`, right-click = four checkboxes for the
  candidate set (writes the canonical string), four `? leans to` sliders,
  the readout.
- Tests (56): route-string round trips; weights lean / zero removes /
  all-zero falls back fair; balanced deals every output its share and
  respects subsets; rng consumed only on real choices; renderer ==
  reference in loop / balanced / dice; latch holds, reroll refreshes,
  reset rewinds; block-size independence; the two stepping contracts and
  the held-input mid-gate switch; a real whether × which graph at 512 and
  64; the panel's gesture, popup, readout and repaint; the example.
- Example `possibility_selector_kit.json`: whether × which drums + a
  four-string harp stepping on its own hits.

### `drift` (S) — "Modulation" — **SHIPPED 2026-09-19** (see TODO.md / WORKLOG.md) — the keep-list's smooth wandering random

The LFO's `random` is stepped; `sample_hold` + `slew` can round the
corners but the corners are still there; `chaos` orbits deterministically.
This *stumbles*: a smooth random voltage that wanders — Buchla's
fluctuating random / the Wogglebug's smooth out — with a stepped twin and
a trigger on every new value so the same die can drive three things.

- Ports: `rate_cv` (cv, octaves per unit × `cv_depth`, block mean; a
  `(V, F)` source is averaged — mono module), `clock` (gate, optional:
  patched, a new value on every rising edge instead of the internal
  rate) → `cv` (cv, the smooth wander), `stepped` (cv, the targets held
  — the plain S&H), `trig` (gate, a 1 ms pulse on every new value).
- Params: `mode` smooth | walk (smooth) · `rate` Hz 0.02..50 (0.5, new
  values per second) · `glide` 0..1 (1.0, the fraction of each interval
  spent travelling to the new value — 0 is stepped, 1 is one continuous
  curve) · `step` 0..1 (0.25, walk only: gaussian step size as a
  fraction of the range) · `depth` 0..1 (1.0) · `bipolar` (True) ·
  `cv_depth` (1.0 oct/unit) · `seed` (1).
- DSP: an integer tick schedule — `next_tick = last_tick +
  round(sr / rate_eff)` with `rate_eff = rate · 2^(cv_depth · mean
  rate_cv)` re-read per block, so ticks land on the same absolute samples
  at any block size (the clock's float phase does not, see the selector
  test). At each tick draw the next target from `default_rng(seed)`:
  `smooth` = uniform(−1, 1); `walk` = previous + `step` · N(0, 1),
  reflected at ±1. Between ticks the value travels from the old target
  to the new one along a half-cosine over `max(1, round(glide ·
  interval))` samples, then holds — vectorized per segment, one segment
  per tick per block. `clock` patched: ticks on rising edges, the glide
  length from the measured edge interval (a jump until one exists).
  `cv` = value · depth (bipolar) or (value + 1)/2 · depth; `stepped` the
  same scaling of the held target; `trig` high for min(1 ms, half the
  interval) from each tick, carried across blocks.
- Neutral: unpatched inputs are the default (free-running); `depth` 0
  is exact zeros; the first block starts from 0 and glides to the first
  draw (no click on start).
- Tests: registration; the tick schedule (ticks at round(k·sr/rate) for
  constant rate; `rate_cv` an octave doubles them); block-size
  independence bit-exact (64 vs 512, 1024); seed determinism and
  divergence; `smooth` targets are uniform in ±1 (a long run's stepped
  output covers the range, mean ≈ 0), `walk` is a walk (consecutive
  targets differ by ~`step`, never leave ±1, the reflection); `glide` 0
  == the stepped output, `glide` 1 is C¹-smooth (max sample-to-sample
  step bounded by π·range/interval), intermediate glides hold; `cv` ==
  half-cosine between the stepped values at glide 1; `trig` fires once
  per tick, 1 ms wide; `clock` patched ticks on edges only and glides
  over the measured interval; bipolar/unipolar scaling; depth 0; the
  mode combo offers drift's own modes (the shared-branch trap); the
  example.
- As built, verbatim to the spec. `step` is the gaussian's standard
  deviation on the ±1 scale. 24 tests. Example `drift_wander.json`.

### `chaos` (S–M) — "Modulation" (added 2026-08-04)

Strange-attractor CV source — deterministic wandering with *structure*,
the corner none of the existing random sources cover: `shift_random`
loops, `lfo` random steps, the future `drift` smooths noise — chaos
*orbits*. Same shape never repeats, yet it's fully seeded-deterministic
(the house randomness rule for free: it isn't random at all).

- Ports: `reset` in (gate — re-seed to the initial conditions,
  performance rewind); outs `x`, `y`, `z` (cv — three views of one
  orbit, so one module modulates three destinations *coherently*) +
  `gate` (gate — see per-system semantics below).
- Params: `system` combo `lorenz` (σ=10, ρ=28, β=8/3 — the butterfly)
  | `rossler` (a=b=0.2, c=5.7 — spiral-and-kick) · `rate` 0.01..50
  (1, log drag) — scales integration dt; calibrate so `rate` ≈ the
  dominant orbital frequency in Hz (document the calibration per
  system, pin it loosely) · `range` 0..5 V (2) · `bipolar` tickbox
  (the shift_random idiom) · `seed` (1).
- Gate semantics (the free lunch): `lorenz` → sign of x (lobe
  switching — quasi-random square that hangs unpredictably on each
  wing); `rossler` → z above threshold (sparse unpredictable spike
  bursts — z sits near zero then kicks). Two very different rhythm
  characters from one combo. Documented per system.
- DSP: RK4 at a fixed internal substep (stability — the classic dt
  rails per system, `rate` scales substeps-per-sample not dt beyond
  the rail), evaluated on a decimated control grid (~every 16 samples)
  and linearly interpolated to audio rate — the per-sample-Python trap
  (slew lesson) never opens. Substep phase + last control point carried
  across blocks → block-size independence *exact*. Seeded ICs drawn
  near the attractor + a warmup run at seed/reset (transient skip,
  deterministic). Output normalized by per-system attractor bounds
  (Lorenz x ≈ ±20, z ≈ 5..45 — constants documented), clipped at the
  rails, then range/bipolar mapping. Non-finite guard: re-seed +
  counter (should never fire; tripwire test).
- Neutral: none (a source with no passthrough) — the pinned contract is
  seeded determinism: same seed → bit-identical render, forever.
- Tests: seeded determinism bit-exact; `reset` ≡ fresh module;
  block-size independence exact (substep + interp carry); long-run
  bounds within `range`, zero NaNs; **the chaos test**: two ICs ε
  apart decorrelate within a documented horizon while identical seeds
  stay identical (positive-Lyapunov behavior, not a numerics
  accident); rate calibration (zero-crossing rate of x scales with
  `rate`, pinned loosely); lorenz gate ≡ sign(x); rossler gate sparse
  (duty cycle bounds pinned).
- Killer patches: `x` → quantizer → osc (wandering melody that never
  loops, recalled exactly by seed); `z` → filter cutoff + `gate` →
  envelope = self-playing patch with one module; `x`/`y` → scope xy
  mode = **the butterfly on screen** (the demo that sells it).
- Stretch: a `chaos` knob morphing ρ/c through the period-doubling
  route (risk: parameter zones that collapse to a fixed point = frozen
  CV — needs a guard); audio-rate mode (growly Lorenz drones);
  `rate_cv` (+ depth per conventions).

## New voices — "Sources"

### `fm_op` (M) — **SHIPPED 2026-07-11** (see TODO.md / WORKLOG.md)

One DX-style phase-modulation operator; two make a bell, three make nearly
everything.

- Ports: `pitch_cv` in; `pm` in (audio-rate phase mod); `amp_cv` in (drive it
  from adsr/cv_gates); `out`.
- Params: `ratio` 0.25..16 (1, snapped to a harmonic table) · `fine` ±50 ct
  (0) · `index` 0..10 (1, scales the `pm` input — document the radians
  scaling) · `index_cv_depth` · `feedback` 0..1 (0, self-PM) · `fixed`
  tickbox + `freq` Hz (fixed-frequency mode, ignores pitch_cv).
- DSP: per-voice phase accumulator; out = sin(2πφ + index·pm + fb·y[n−1]).
  Feedback needs a per-sample loop — only when fb > 0: dual engine (vectorized
  fb=0 path, per-sample fallback, delay-module precedent; the two paths
  bit-identical at fb=0).
- Tests: ratio/fine → exact frequency (FFT); sine PM at ratio 1 → Bessel
  sideband amplitudes J0/J1/J2 within tolerance (the classic analytic check);
  per-voice phase independence; fb=0 path equivalence.
- Ships with examples/: 2-op bell, 3-op e-piano.

### `pluck` (M–L) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

Extended Karplus–Strong string. Polyphonic plucks from cv_keyboard — 16
strings for free.

- Ports: `pitch_cv` in; `trigger` in; `out`.
- Params: `decay` 0.1..30 s (2, t60-ish) · `damping` 0..1 (loop LP tone) ·
  `color` 0..1 (exciter noise→pick spectrum) · `position` 0..1 (comb on the
  exciter = pick position) · `level`.
- DSP: per-voice fractional-delay loop (allpass/Lagrange interp for tuning) +
  one-pole damping; exciter = short enveloped noise burst shaped by
  color/position, injected on trigger (seeded per hit → exact-waveform tests).
  High notes make the loop shorter than a block → per-sample region: dual
  engine (block path while loop ≥ block, fallback otherwise) with early-out on
  silent voices (track decay).
- Tests: pitch accuracy ±5 ct across range (interp verified); t60 within 10%;
  damping monotone; retrigger while ringing declicks; 8-voice example.

### `modal` (M–L) — **SHIPPED 2026-08-03**; love pass 2026-09-11 (`position` / `mallet` / `spread` + `out_l`/`out_r`) (see TODO.md / WORKLOG.md)

Struck/blown resonator bank (bars, bells, membranes) — feed it `burst`,
noise, or anything.

- Ports: `excite` in (audio); `pitch_cv` in; `out`.
- Params: `material` combo (bar/bell/membrane/string — generic
  physics-textbook mode ratio+Q tables, not cloned from any product) ·
  `modes` 4..24 (12) · `brightness` (gain tilt across modes) · `decay` +
  `decay_tilt` (highs die faster) · `inharm` 0..1 (ratio stretch) · `level`.
- DSP: bank of 2-pole resonators at pitch×ratio[i] — exactly the slice-4
  house pattern: vectorized lfilter across mode rows with per-row coeffs;
  batch shared-coeff groups where voices share a pitch. Coefficients update
  per block on pitch change.
- Tests: FFT peaks land on the ratio table; per-mode decay times; bounded
  energy (no runaway Q); voice ≡ mono; measure DSP % at 16 voices × 24 modes
  and record it (the new readout is the tool).
- Gorgeous with cv_gates (17 enveloped strikes) and `burst`.

### `bowed` (M) — "Sources" — **SHIPPED 2026-09-18** (see TODO.md / WORKLOG.md)

The keep-list's "bowed or wind" — the bowed half. Sustained-excitation
waveguide: the bow stays on the string while `gate` is high.

- Ports: `pitch_cv` (cv, voice-aware, 1 V/oct C4 = 0 V, unpatched → C4);
  `gate` (gate, voice-aware — bow on while high); `pressure_cv`,
  `velocity_cv` (cv, mono, per-sample, × `cv_depth`) → `out` (audio).
- Params: `pressure` 0..1 (0.5, friction-table slope 5 − 4p) · `velocity`
  0..1 (0.6, bow speed 0.03 + 0.2v) · `position` 0.05..0.5 (0.127, the
  bridge-side fraction β) · `attack` / `release` s (0.05 / 0.15, integer-
  count bow ramps) · `damping` 0..1 (0.5, bridge one-pole pole 0.15..0.7)
  · `body` 0..1 (0.5, five parallel constant-peak bandpasses: 275/460/550
  /1100/2200 Hz) · `cv_depth` (1.0, level per unit) · `level` (0.5).
- DSP: Smith's bowed string as in STK `Bowed`. Bridge delay β·L and nut
  delay (1−β)·L meet at the bow; string velocity there is
  `−lowpass(bridge_out) − nut_out`; the bow injects `dv · table(dv)` with
  `table(x) = min(1, (|x·slope| + 0.75)^−4)`, `dv` = bow velocity − string
  velocity, into both halves; bridge reflection −0.95·one-pole, nut −1.
  `L = sr/f0 − τ(f0)` with τ the one-pole's exact phase delay; the
  fraction rides the bridge side as a linear-interp read, the nut side is
  integer. Advanced in vectorized chunks ≤ the bridge delay (the pluck
  precedent with a nonlinearity inside — elementwise, so it chunks the
  same); slice buffers compacted once per block, no per-chunk modulo.
  Output = the bridge wave → body bank mixed by `body` → × level. Voice
  early-out when the bow is lifted, the envelope is 0 and the block is
  silent.
- Neutral: gate low from the start = free, exact zeros; unpatched =
  silent and stateless.
- Tests (27): registration / round trip; sustains and stays bounded;
  ±10 ct C2..C6; fundamental carries energy; release then exact zeros;
  attack time; velocity monotone level; pressure changes tone and level;
  per-sample pressure_cv; velocity_cv × cv_depth; bridge vs sul tasto
  centroid; damping monotone; body colours without retuning; voice
  independence; mono ≡ single voice; block-size independence with the
  bow landing and lifting mid-stream; re-bow during release doesn't jump;
  early-out; unpatched; the widget sweep; the example.
- Cost measured: ~0.5 ms of an 11.6 ms block at C4, ~1.7 ms at C6, one
  voice (44.1 kHz).
- Example `bowed_cello.json`.

### `wind` (M) — "Sources" — **SHIPPED 2026-09-18** (see TODO.md / WORKLOG.md) — the blown half of "bowed or wind"

- Ports: `pitch_cv` (cv, voice-aware); `gate` (gate, voice-aware — breath
  on while high); `breath_cv` (cv, mono, per-sample, × `cv_depth`) →
  `out` (audio).
- Params: `model` flute | reed · `breath` 0..1 · `noise` 0..1 (breath
  noise) · `attack` / `release` s · `damping` 0..1 (bore reflection
  lowpass) · `cv_depth` · `level` · `seed`.
- DSP: STK `Flute` (jet delay 0.32 × a bore tuned to 1.5 periods — the
  overblown register — jet table `x(x²−1)` clamped, reflections 0.5/0.5,
  one-pole + DC block in the bore return) and STK `Clarinet` (one
  round-trip delay, reed table `clip(0.7 − 0.3·pd)`, reflection
  −0.95·lowpass). Chunked ≤ the jet delay (flute) / the bore (reed).
  Breath = `maxp(breath) · env · (1 + noise · white)` with the white
  noise a per-voice seeded stream (block-size exact). Prototyped
  2026-09-18: the reed is in tune to ±5 ct C2..C6 and speaks above a
  pressure threshold (over-pressure closes the reed — physical); the
  flute holds ±10 ct C3..C6 at moderate breath and goes sharp when blown
  hard at low pitch (physical too).
- As built: the flute bore is `1.5 × 1.015 × sr/f0 − τ(f0)` (no extra
  sample — a stray −1 was 25 ct sharp at C6), the breath maps
  flute 0.85..1.4 / reed 0.58..1.0, both models clamp f0 at 2.5 kHz and
  the flute at 80 Hz below; per-note seeded breath noise drawn per
  segment between rising edges; output DC-blocked; the gate ramp is
  the shared `_gate_ramp_env` helper (extracted from `bowed`).
  Tests (32) mirror the bowed set plus: reed ±8 ct C2..C6, flute ±10 ct
  C3..C6, the two models differ on one note (reed harmonic-rich,
  flute nearly pure — the odd-harmonic claim did NOT survive
  measurement: the model's even harmonics come and go with breath and
  noise), reed level monotone in breath, the flute speaks at every
  breath 0..1, seeded noise, breath_cv, model switch mid-stream.
  Example `wind_duet.json`.

### `granular` (L — slice it) — **ALL THREE SLICES SHIPPED** 2026-09-15/16 (Effects; capture + stream — `granular_cloud.json`; sprays + seed + stereo — `granular_haze.json`; freeze + position_cv — `granular_freeze.json`, `granular_beat_repeat.json`). Complete against this spec.

Grain-cloud texture engine over a live-captured buffer.

- Ports: `in`; `position_cv`; `freeze` gate in; `out_l`/`out_r`.
- Params: `buffer` 0.5..10 s (2) · `density` 0.5..100 grains/s (12) · `size`
  10..500 ms (80) · `pitch` ±24 st (0) + `spray_pitch` cents · `position`
  0..1 + `spray_pos` · `window` hann|triangle|expo · `freeze` toggle ·
  `width` (per-grain pan spread) · `mix` · `seed`.
- Reuse: ring-buffer capture (resampler `window` infra), grain windowing +
  fractional resampling (pitch_shifter), seam-declick lessons, stereo outs
  (chorus/reverb precedent). Normalize by expected overlap (density×size) for
  headroom.
- Slices: ~~(1) capture + single-stream grains, mono, seeded + tested~~ —
  SHIPPED 2026-09-15 as a *synchronous, deterministic* stream: `buffer` /
  `density` / `size` / `pitch` / `position` / `window` / `mix`, grains
  frozen at their onset, bit-exact neutral (hann at 50% = the input),
  bit-exact block independence; `seed` deferred to slice 2 since nothing
  in slice 1 is random. ~~(2) density/spray scheduler (`spray_pos`,
  `spray_pitch`, `seed`, jittered onsets) + stereo (`width`,
  `out_l`/`out_r`)~~ — SHIPPED 2026-09-15: `spray_time` (interval × a
  factor in 1 ± s), `spray_pos` (± position units), `spray_pitch` (±
  cents), `width` (constant-peak per-grain pan), `seed`; grain *i*
  draws from `default_rng([seed, i])` so the cloud is reproducible and
  block-independent to the bit; slice-1 renders unchanged at the
  defaults. ~~(3) freeze + position_cv + examples~~ — SHIPPED 2026-09-16:
  `freeze` gate + param (ORed, per-sample edge), the ring in *captured*
  time so a release resumes with no hole, head start `rate × size` while
  frozen, reads clamped to the head (a mid-grain freeze holds, never
  stale), `position_cv` + `position_cv_depth` read per grain at its
  onset; slice-2 renders unchanged at the defaults.
- Tests: seeded cloud reproducible; freeze truly static (repeated reads
  bit-identical); scheduler block-size independent (grain onsets carried
  across joins); mix=0 bit-exact dry.

### Drum voices: `kick_drum`, `snare_drum`, `hat_drum` — **ALL SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

Trigger-driven percussion sources; with clock/euclidean/burst the rack becomes
a groovebox.

- Common: `trigger` in; `out`; `level`; `tune` ±12 st; retrigger restarts
  envelopes with a ~2 ms declick ramp (resampler-declick lesson); noise seeded
  per hit → exact-waveform tests.
- `kick_drum`: pitch envelope `freq_start` 100..400 Hz (180) → `freq_end`
  30..80 Hz (50) over `bend` 5..200 ms (40, exponential); `click` 0..1;
  `drive` (shared oversampling infra); `decay` 50..1500 ms (350). Test:
  instantaneous-frequency trajectory matches spec; no DC offset.
- `snare_drum`: two detuned sine modes (~180/330 Hz) + bandpassed noise;
  `tone_decay` / `noise_decay` · `snappy` balance.
- `hat_drum`: 6 detuned squares (metallic ratio stack) → HP ~7 kHz;
  `closed_trigger` + `open_trigger` ports in one module, open choked by
  closed (document choke semantics); `decay_closed` / `decay_open`.

### `sampler` (M–L — slice it) — "Sources" (added 2026-08-04)

Keyboard-tracked pitched sample playback — the gap `file_player` doesn't
fill: FilePlayer is a *transport* (one playhead, no pitch), this is a
*voice* (per-voice playheads, pitch_cv-tracked rate). Load one recording,
tell it what note the recording is, and the 16-slot architecture makes it
a mellotron/romplers/breaks-machine for the price of a cubic read. Every
hard part is already shipped somewhere in the rack.

- Ports: `pitch_cv` in (cv, voice-aware; 1 V/oct, C4 = 0 V, the
  `pluck`/`fm_op` convention; unpatched → C4); `gate` in (gate,
  voice-aware; rising edge starts playback from `start`, fall behavior
  per `mode`); `out` (audio).
- Params:
  - `path` — Browse (FilePlayer picker reuse). Decode via `media.py`
    (scipy WAV fast path, ffmpeg for mp3/flac/ogg/m4a/video audio),
    **whole-load off the audio thread** (convolver `_IRLoader`
    precedent: silent until ready, keeps the previous sample on a
    reload); missing/unreadable → silence, never raises (FilePlayer
    contract, saved patches always load). Multi-channel mono-sums on
    load (documented; stereo is a stretch). Length cap ~120 s
    (RAM-bound, not DSP-bound: 60 s mono @ 48 k float32 ≈ 11.5 MB —
    document the cap, truncate with a status).
  - `root` — the note the recording *is*: note-name combo C0..C8
    stored numeric (fm_op ratio-combo precedent), default C4. Playing
    the root note = playback rate exactly 1.0.
  - `tune` ±12 st (0) · `fine` ±50 ct (0) — the drums' `tune` idiom.
  - `mode` combo: `one_shot` (edge fires the whole region, gate length
    ignored — the drums idiom) | `gated` (sustains while high, release
    fade on fall) | `loop` (gated + loops the loop region while held —
    the mellotron mode).
  - `start` / `end` 0..1 of the file (playback region, sample-exact).
  - `loop_start` / `loop_end` 0..1 within the region (loop mode) ·
    `loop_xfade` 1..100 ms (10) — linear crossfade across the seam
    (resampler seam-declick lesson).
  - `attack` 0..500 ms (**0**) / `release` 1..2000 ms (10) — declick
    ramps, NOT an envelope (document: patch `adsr` → `vca` for real
    shaping; raise `attack` only if your `start` point clicks).
  - `level` 0..1 (0.8).
- DSP: per-voice float64 playhead; rate = 2^(pitch − root_offset +
  tune/12 + fine/1200), read per block (mean — vibrato tracks at block
  rate, the pluck precedent, documented). Block render: positions =
  pos0 + rate·arange(F) (affine → wrap/end segment logic vectorizes),
  gathered through the **4-tap cubic Hermite** read — `_hermite4` from
  the resampler verbatim, whose pinned bit-exact-at-integer-positions
  property is what makes the neutral testable. Loop wrap = modulo into
  the loop region + seam crossfade; one_shot/gated ends → zeros, voice
  early-outs (pluck precedent: finished voices cost nothing).
  Retrigger of a sounding voice: ~2 ms crossfade old-position →
  new-start (drums declick idiom). Gate-fall release = multiplied ramp
  then inactive. Pitch-up aliases by design — the classic sampler
  crunch (bitcrusher "deliberately aliased" precedent, documented);
  clean pitch-up is the mip-chain stretch below.
- Neutral: root pitch ∧ start=0 ∧ end=1 ∧ attack=0 ∧ level=1 →
  rate exactly 1.0 → integer read positions → **the decoded buffer
  verbatim, bit-exact** (a source's passthrough contract).
- Tests: unity bit-exact (above); +12 st ≡ buffer[::2] exactly (integer
  stride → still bit-exact — the strong version); sine sample +7 st →
  FFT ratio 2^(7/12) within cents (resampler pitch-test machinery);
  one_shot exact length then zeros + early-out; gated release ramp
  exact length, max sample-delta bounded (no click); loop steady-state
  period exact + seam spike-free vs xfade=off + loop across block
  joins; retrigger declick bound; root/tune/fine math (root C3, play
  C4 → rate 2); per-voice independence + single row ≡ mono; block-size
  independence bit-exact at constant pitch; missing path → silence, no
  raise; start/end region sample-exact; multi-channel mono-sum.
- Slices: (1) load path + one_shot/gated + root/tune/fine + cubic read
  + declick ramps + unity neutral pinned, voice-aware; example:
  euclidean → sampler one-shot (breaks machine). (2) loop mode + seam
  crossfade + start/end/loop UI (bounded drags per the polish
  standard); example: keys → loop-mode sampler → reverb (mellotron
  pad — and `chord` in front = 4-deep sample stack for free).
  (3) stretch menu: stereo `out_l`/`out_r`; on-load halfband mip chain
  (`*_wt` infra) for alias-free pitch-up; `start_cv` (+ depth per
  conventions); velocity in scaling level; `reverse` tickbox; a
  waveform face with region markers (scope-face precedent).
- **Status:** all three slices SHIPPED (1: 2026-08-22, 2: 2026-08-30,
  3: 2026-09-11). Slice 3 deviations: `antialias` is a tickbox
  defaulting OFF (the crunch stayed the default; the chain is
  octave-decimated with a crossfade between levels, so it is
  attenuation between octaves rather than removal); velocity arrived
  as a `vel` cv input plus a new `velocity_cv` OUT on `midi_input` —
  nothing in the rack emitted velocity before; `start_cv` is read at
  the gate edge and latched per hit rather than block-mean. See
  WORKLOG 2026-09-11.

### `organ` (S–M) — "Sources" (added 2026-08-04)

Additive drawbar organ — nine harmonic faders, per-voice sines, key
click. The cheapest joy on the ideas list: the DSP is nine sines, the
panel is the `fader_seq` fader-bank idiom, and the sound is immediate.

- Ports: `pitch_cv` in (cv, voice-aware; 1 V/oct, C4 = 0 V); `gate` in
  (gate, voice-aware; organs are gate-on/gate-off — no envelope, that's
  the instrument); `out` (audio).
- Params:
  - `bar1`..`bar9` — the nine drawbars, integer 0..8 (drawbar
    convention), at the classic footages 16′ 5⅓′ 8′ 4′ 2⅔′ 2′ 1⅗′ 1⅓′
    1′ = harmonic ratios ½, 1½, 1, 2, 3, 4, 5, 6, 8 of the played
    pitch (UI labels show footages). Level law: ~3 dB per step, 0 =
    silent (document the exact law, pin it). Default **888000000**
    (the jazz registration — sounds right out of the box). Panel:
    vertical fader bank (`fader_seq` precedent); faders-up = louder —
    a documented deviation from pulled-out-is-louder hardware
    drawbars, screens read up-is-more.
  - `click` 0..1 (0.3) — key-click transient: short seeded HP-shaped
    noise tick at each gate rise (softer at fall), the contact-bounce
    sound (drums/pluck seeded-hit precedent). At 0, a ~1 ms declick
    ramp keeps onsets clean instead.
  - `perc` combo off|2nd|3rd (off) · `perc_decay` fast|slow ·
    `perc_level` 0..1 (0.7) — the percussion register: a fast-decaying
    extra partial (4′ or 2⅔′) that fires **only on a note played from
    silence** (single-trigger: legato/held-note additions don't
    re-fire — the classic behavior, and the musically load-bearing
    part). The 1′-drawbar-stealing hardware quirk is deliberately
    skipped (authenticity trivia, not music).
  - `level` 0..1 (0.5).
- DSP: per voice, 9 phase accumulators at pitch × ratio — folded into
  the voice axis so one vectorized sine call covers (V·9, F) (the
  supersaw-spec idiom; `_osc_waveshape` centralization). Weight by the
  drawbar law, sum, sum-normalize so full-registration chords keep
  headroom (document the norm). Partials above Nyquist masked per
  block (a C8 fundamental puts 1′ at ~33 kHz — mute, don't alias;
  tonewheel top-octave *foldback* is the authenticity stretch).
  Percussion = one extra decaying sine per voice with the
  from-silence trigger latch; click = seeded per-hit burst (exact-
  waveform testable). Phases carried across blocks.
- Neutral (a source's pin): `bar3` (8′) at 8, everything else 0,
  click 0 → **a pure per-voice sine** — pinned against the
  oscillator's sine render at the same pitch (bit-exact if the shared
  phase+sin path allows, else < 1e-6 with the tolerance documented).
- Tests: the single-drawbar sine pin (above); registration FFT —
  888000000 → partials at ratios ½/1½/1 with 3 dB-law amplitudes
  exact; per-step level law −3 dB pinned; Nyquist mask (high-note
  render alias-free — antialiasing-test machinery); click energy
  scales with `click`, seeded reproducible, click=0 onset passes the
  max-delta declick bound; **percussion single-trigger** — a legato
  second note while one is held does NOT re-fire, release-all then
  press does; perc decay times; voice ≡ mono; per-voice independence;
  block-size independence (phase carry); perf at 16 voices × 9
  partials = 144 sines — measure and RECORD (modal/supersaw
  precedent).
- Killer patches: keys → organ → `rotary` (shipped 2026-09-14 — see
  `organ_leslie.json`); keys → organ → chorus → reverb;
  `chord` in front for one-finger full-organ stabs.
- Stretch: tonewheel foldback (top-octave repeat instead of mute);
  `leakage` (quiet all-wheels hum bed); vibrato/chorus scanner —
  deliberately NOT built in, the rack's chorus module is the patch.

## Seeing the signal — "CV & Utilities"

### `scope` (M) — **SHIPPED 2026-08-03** (see TODO.md / WORKLOG.md)

Oscilloscope pass-through tap — the learn-by-building module par excellence,
and it makes every later module easier to debug and demo.

- Ports: `in` (audio or cv); `in_r` (optional second trace); `trig` (optional
  external trigger); `out` (pass-through — bit-exact always, meter precedent).
- Params: `time_div` 1..500 ms/div (10) · `gain` vertical · `trigger`
  free|rising|falling + `level` · `freeze` tickbox · `mode` mono|dual|xy
  (xy = goniometer on in vs in_r).
- Engine: render thread writes decimated min/max pairs per pixel column into a
  snapshot ring (meter snapshot pattern; shape (columns, 2) + trigger index);
  UI draws a polyline via dpg drawlist. Trigger = first rising crossing after
  holdoff (sign-change searchsorted). Maths in a dpg-free `ui/scope_math.py`
  (zoom.py precedent) so it tests headless.
- Tests: pass-through bit-exact; known sine → period spans the expected
  columns for time_div; trigger phase-locks consecutive snapshots; min/max
  decimation never misses a one-sample spike.

### `spectrum` (M)

FFT analyzer tap.

- Params: `size` 1024|2048|4096|8192 (4096) · `avg` 1..8 (4, exponential) ·
  `peak_hold` on|off + decay · `range_db` 60..120 (90); log-frequency axis.
- Engine: Hann window → rfft → power dB → precomputed log-freq rebin to ~256
  columns; snapshot ring as in meter/scope; pass-through bit-exact.
- Tests: single sine → peak in the right column within 1 dB (window gain
  compensated); two-tone resolution at 4096; averaging time constant; rebin
  map monotone and gap-free.

## The quick-hit run (planned 2026-08-03 — Matthew's pick, seven modules)

Promoted from the quick-hit bullets to full specs at Matthew's request.
Proposed as **three sessions** (working-agreement slicing; each module
still gets the full polish standard — tests, example, MODULES.md entry,
tripwires green):

> **Session A — utility sweep**: `logic` + `mid_side` + `octaver` (S×3,
> zero new infra — the clockwork-trio session shape).
> **Session B — patch bay & dust**: `matrix_mixer` (M — carries the one
> real architecture question: feedback through a topo-sorted DAG) +
> `vinyl` (S, dessert).
> **Session C — oscillator double**: `supersaw` (S–M) +
> `wavetable_morph` (M) — shared anti-aliasing/mipmap infra, and the
> obvious demo patch is `chord` → both.

### `logic` (S) — "Modulation" (Session A) — **SHIPPED 2026-08-03**

2-in gate algebra; every jack live at once, no mode combo — swap cables,
not settings.

- Ports: `a`, `b` (gate in) → `and`, `or`, `xor`, `nand`, `not_a`
  (gate out).
- Params: none. **Deviation from the old bullet**: the "comparator mode
  with threshold for CVs" is dropped — port kinds are strict (a cv out
  cannot cable into a gate in), and cv→gate conversion is exactly
  `schmitt`'s job. Cleaner module, zero params.
- DSP: elementwise on thresholded bools (house `> 0.5`); unpatched `b`
  reads as low (so `or`/`xor` pass `a`, `and` is 0, `nand` is 1 —
  document; `nand` high-when-idle is the classic normalled trick).
- Tests: full truth table per jack; unpatched-`b` contract; xor of a
  clock against its own division = ratchet pattern (integration).

### `mid_side` (S) — "Routing & VCA" (Session A) — **SHIPPED 2026-08-03**

M/S encode/decode + width — completes the stereo utility story beside
`stereo_speaker_output`. All four outs always computed, no mode combo.

- Ports: `in_l`, `in_r` (audio) → `mid`, `side`, `out_l`, `out_r`
  (audio); `width_cv` (cv) in.
- Params: `width` 0..2 (1).
- DSP: M = (L+R)/2, S = (L−R)/2; `out_l/r` = M ± width·S. Unpatched
  `in_r` → mono (M = in_l, S = 0, width inert). Stateless.
- Tests: round trip at width 1 bit-close to input; width 0 → out_l ≡
  out_r ≡ M; width response linear in S; mono-in edge; encode outs of a
  hard-panned input land ±.

### `octaver` (S) — "Effects" (Session A) — **SHIPPED 2026-08-03**

Zero-crossing flip-flop sub-octave — −1/−2 oct squares under the dry;
dirty analog charm for bass.

- Ports: `in` (audio) → `out` (audio).
- Params: `dry` 0..1 (1) · `sub1` 0..1 (0.5) · `sub2` 0..1 (0) ·
  `tone` LP cutoff on the subs (~200..2000 Hz, 800).
- DSP: rising zero-crossings toggle a flip-flop (÷2), a second one
  toggles on the first (÷4); each square rides the input's envelope
  (audio_to_cv follower core) so silence stays silent and dynamics
  track; one-pole LP (`tone`) rounds the squares; sum under the dry.
  State: flip-flop phases + follower + LP zi, carried across blocks.
- Tests: sine at f → FFT peaks at f/2 (sub1) and f/4 (sub2); envelope
  gating (silence in → silence out with subs up); dry-only bit-exact
  passthrough; block-size independence (flip-flop carried).

### `matrix_mixer` (M) — "Routing & VCA" (Session B)

4×4 bipolar gain matrix — and the door to **feedback patching**, which
is the actual work: the backend topo-sorts a DAG, so a cycle through
the matrix cannot compile today.

- Ports: `in_1..in_4` (audio) → `out_1..out_4` (audio); `cv_1..cv_4`
  (cv) in, one per *output column* (scales that column's mix).
  **Deviation from the old bullet**: "every node CV-able" would be 16
  jacks of soup on a DPG node — per-column CV covers the musical uses
  (duck a whole bus) at 4 jacks. Revisit only if a real patch demands
  per-node.
- Params: 16 gains `g_rc` −1..+1 (identity diagonal default) drawn as a
  4×4 drag grid (chord slot-bank precedent) · `soft_clip` tickbox
  (default ON) — tanh on each out, the stability guardrail.
- **Architecture (the M of the M)**: compile-time cycle handling. Keep
  the graph a DAG for topo purposes by detecting cables that would
  close a cycle *into a matrix_mixer* and marking them **late-reads**:
  the matrix reads that input's previous-block buffer (one-block
  feedback latency — the standard software-modular answer; document
  it). Feed-forward cables through the matrix stay zero-latency. SCC
  detection at compile; late-read buffers owned by backend state.
- Tests: identity → bit-exact pass; gain exactness incl. negative
  (phase flip); column CV scales its column only; a matrix→delay→matrix
  loop compiles, stays finite with soft_clip on, and grows without it
  at loop gain > 1 (pinned); one-block latency of the late path pinned;
  block-size independence.

### `vinyl` (S) — "Effects" (Session B)

`tape`'s scrappy sibling: surface noise + warp, all seeded.

- Ports: `in` (audio) → `out` (audio).
- Params: `crackle` 0..1 (0.3) · `rumble` 0..1 (0.2) · `wobble` 0..1
  (0.2) · `seed` (1).
- DSP: crackle = seeded Poisson impulses (rate and amplitude scale with
  the knob), LP-shaped ticks; rumble = pink-ish noise through a ~40 Hz
  resonant LP (turntable bearing); wobble = 0.55 Hz (33⅓ rpm)
  fractional-delay pitch wobble (chorus/tape vibrato core), depth from
  the knob. All-zero knobs → bit-exact passthrough (tape precedent).
- Tests: passthrough at zero; impulse count scales with `crackle`
  (seeded, exact); rumble spectrum LF-dominant; wobble → measurable
  0.55 Hz pitch deviation (resampler pitch-test machinery); seeded
  determinism; block-size independence.

### `supersaw` (S–M) — "Sources" (Session C)

The trance chord machine: 7 detuned blep saws per voice.

- Ports: `freq_cv` (cv, voice-aware), `amp_cv` (cv) in → `out_l`,
  `out_r` (audio; identical when `spread` 0 — patch either for mono).
- Params: `freq` (261.6256) · `detune` 0..1 (0.35, → max ±~50 ct via
  the classic asymmetric offset table) · `blend` 0..1 (0.75, center saw
  vs the six side saws — the JP-8000 control) · `spread` 0..1 (0.5,
  side saws alternate-panned) · `amp` (0.5).
- DSP: per voice, 7 phase accumulators at freq × 2^(offset·detune);
  fold the 7 saws into the voice axis so `_osc_waveshape("saw_blep")`
  vectorizes over (V·7, F); initial phases seeded-random per voice
  allocation (the supersaw signature — phase-locked saws buzz;
  deterministic per seed param? no: per-slot fixed seeds, patches
  recall). Amp-normalize so detune/blend moves don't pump level.
- Tests: detune 0 + blend 1 ≈ one saw (spectrum match); sideband
  cluster width grows monotone with `detune` (FFT); blend 0 → center
  only; spread 0 → L ≡ R, spread 1 → decorrelated (side-saw pan
  pinned); per-voice independence; 16-voice perf measured vs budget
  (7× oscillator — RECORD the number, modal precedent).

### `wavetable_morph` (M) — "Sources" (Session C)

Scanning wavetable oscillator: the `*_wt` mipmap infra grown into an
instrument.

- Ports: `freq_cv` (cv, voice-aware), `amp_cv`, `position_cv` (cv) in →
  `out` (audio).
- Params: `freq` · `amp` · `position` 0..1 (scan point; `position_cv`
  adds) · `table` combo (built-in stacks: analog basics → formant/vocal
  → bells/metallic) · `file` (single-cycle WAV import via Browse —
  media.py precedent; loads as the top stack).
- DSP: stack of N single-cycle tables, `position` crossfades adjacent
  pairs; each table mip-mapped exactly like the oscillator's `*_wt`
  shapes (WT_LEN/NUM_WT_TABLES infra reused, band from block-max dt);
  per-voice phase accumulators. Import path: resample the cycle to
  WT_LEN, build mip bands offline at load (compile hook, FilePlayer
  decode precedent for the file handling).
- Tests: position 0/1 land the endpoint tables (spectrum match to the
  equivalent `*_wt` render); mid-position is the crossfade (linearity
  pinned at a probe harmonic); scan sweep click-free (no discontinuity
  spikes); alias suppression at high freq (antialiasing-test
  machinery); WAV import round trip (write cycle → load → spectrum);
  per-voice independence; block-size independence.

## The 2026-08-04 brainstorm (Matthew's keep-list — unspecced menu)

From the "what's left" brainstorm (WORKLOG 2026-08-04); Matthew asked
for all of these on the roadmap. One-liners only — promote to a full
spec (ports/params/DSP/neutral/tests) when picked, same as ever.
`sampler`, `organ` and `chaos` were picked first and already have
full specs above.

**Sources**
- ~~`bowed` or `wind` (M–L)~~ — sustained-excitation waveguide (bowed
  string / blown pipe): completes the physical-modeling family beside
  `pluck` (struck string) and `modal` (struck resonator), and sounds
  like nothing else in the rack. **Picked 2026-09-18 — full specs above
  (both SHIPPED 2026-09-18).**

**Modulation — the west-coast hole**
- `function_generator` (M) — the Maths move: rise/fall envelope with
  loop mode and **EOR/EOC gate outs** (the `slew` spec's unshipped
  `eoc` grown into a module). EOC-into-trigger self-patching = krell
  patches; one module is an LFO, envelope, slew and clock depending
  on cabling. Probably the highest patch-value-per-line-of-code item
  on this list.
- ~~`drift` (S)~~ — smooth random CV (interpolated sample-and-hold /
  random walk). The LFO's `random` is stepped; there's no wandering,
  Wogglebug-style source yet. (`chaos` orbits deterministically;
  drift *stumbles* stochastically — siblings, not rivals.) **Picked
  and SHIPPED 2026-09-19 — full spec above.**
- ~~`cv_math` (S)~~ — `logic` for CVs: min, max, average, difference,
  rectify, invert of two CV ins, every jack live at once. Same
  zero-param shape as `logic`. **Picked and SHIPPED 2026-09-19 — full
  spec under CV tools (plus `mult`).**
- ~~`cv_recorder` (M)~~ — record a knob gesture or incoming CV for N
  clocked bars, loop, overdub. A modulation looper — nothing else in
  the rack captures *performance*. **Picked and SHIPPED 2026-09-19 —
  full spec under CV tools.**

**Effects**
- ~~`rotary` (M)~~ — **SHIPPED 2026-09-14** as `rotary` (Effects):
  LR4 crossover, per-rotor Doppler fractional delay + cos-of-angle AM,
  counter-rotating horn and drum with their own exponential spin-up /
  coast-down, two virtual mics, `fast` gate. `organ_leslie.json`.
- ~~`vowel` (S–M)~~ — formant filter bank with an A–E–I–O–U morph knob.
  The vocoder's expressive little cousin; pairs beautifully with
  `supersaw`. (Cousin of `wavetable_morph`'s vowel *stack* — that one
  IS the source, this one filters any source.) **Picked and SHIPPED
  2026-09-19 — full spec under CV tools.**
- ~~`freeze` (M)~~ — spectral freeze: FFT a moment, hold it forever as a
  pad. A different animal from `granular`'s time-domain freeze.
  **Picked and SHIPPED 2026-09-19 as module #100 — full spec under
  Character & space.**
- ~~`autopan` (S)~~ — there's no dedicated panner anywhere in the rack.
  Equal-power pan with CV in; fold tremolo into it and it's the
  missing stereo motion utility. **Picked 2026-09-24 as module #101 —
  full spec under Character & space.**

**I/O**
- `midi_output` (M) — the rack has `midi_input` but can't drive
  external hardware. Gates + pitch CV → MIDI notes, CVs → CCs. A
  whole new dimension: PySynthRack as the *brain* of a hardware
  setup.

**The endgame (architecture, not modules)**
- **Subpatch containers** (L) — group a set of modules into a
  reusable macro-module with exposed ports. At that point the rack
  stops *adding* modules and starts *multiplying* them — every patch
  ever built becomes a module. The feature that makes "running out of
  options" structurally impossible.
- **Snapshot morph** (M–L) — save knob scenes, interpolate between
  them with one CV. A performance feature more than a module.

## Quick hits (S unless noted)

- `exciter` — HP → soft nonlinearity (oversampling infra) → blend; adds air.
- `gate_delay` — delay/stretch a gate by ms or clock division.
- `sequential_switch` — clocked 1→4 router / 4→1 selector, reset in.
- `macro` — one big knob → 4 scaled/offset cv outs; performance macro
  (cv_scale ×4 in one panel).
- `tuner` — pitch_detector core + a cents needle panel.
- `looper` (L) — clock-synced record/overdub/undo layer on the transport
  pattern (FilePlayer Play/Stop + resampler seam crossfades); slice it.
- Vocoder follow-ups (from TODO): stereo decorrelated bands, `formant` shift
  knob, carrier normal to noise, per-band trims.
- (octaver / vinyl / logic / matrix_mixer / mid_side / supersaw /
  wavetable_morph promoted to the planned run above, 2026-08-03.)

## If I had to pick five first

1. **compressor** — biggest functional hole; `gr` out unlocks sidechain
   patching everywhere.
2. **scope** — force multiplier: every later module gets easier to build,
   debug, and show off.
3. **quantizer + shift_random** — an evening each; together the rack starts
   writing its own melodies.
4. **fm_op** — new synthesis territory with a small, well-testable surface.
5. **convolver** — flagship-sized, but real spaces + cab sims lift everything
   already shipped.
