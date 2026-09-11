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
Character/space: `tape` `convolver` ·
CV tools: `quantizer` `slew` `pitch_detector` ·
Generative: `shift_random` `euclidean` `clock_divider` `bernoulli_gate` `burst` `arpeggiator` `chord` `chaos` ·
Voices: `fm_op` `pluck` `modal` `granular` `kick_drum`/`snare_drum`/`hat_drum` `sampler` `organ` ·
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

## CV tools & bridges

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

### `clock_divider` (S)

- Ports: `clock` in; `reset` in; outs `div2` `div4` `div8` + `divn` (param n)
  + `mult` (×m, period-estimate based — document as approximate during tempo
  changes).
- Params: `n` 1..32 (3) · `m` 2..4 (2) · `swing` 0..75% on divn (delays every
  2nd emitted gate) · `pw` gate width fraction.
- Tests: division counts exact over 1000 edges; swing timing; mult tracks a
  tempo ramp within one period; reset realigns all counters.

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

### `granular` (L — slice it)

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
- Slices: (1) capture + single-stream grains, mono, seeded + tested;
  (2) density/spray scheduler + stereo; (3) freeze + position_cv + examples.
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
- Killer patches: keys → organ → `rotary` when that ships (the pairing
  both specs deserve); until then keys → organ → chorus → reverb;
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
- `bowed` or `wind` (M–L) — sustained-excitation waveguide (bowed
  string / blown pipe): completes the physical-modeling family beside
  `pluck` (struck string) and `modal` (struck resonator), and sounds
  like nothing else in the rack.

**Modulation — the west-coast hole**
- `function_generator` (M) — the Maths move: rise/fall envelope with
  loop mode and **EOR/EOC gate outs** (the `slew` spec's unshipped
  `eoc` grown into a module). EOC-into-trigger self-patching = krell
  patches; one module is an LFO, envelope, slew and clock depending
  on cabling. Probably the highest patch-value-per-line-of-code item
  on this list.
- `drift` (S) — smooth random CV (interpolated sample-and-hold /
  random walk). The LFO's `random` is stepped; there's no wandering,
  Wogglebug-style source yet. (`chaos` orbits deterministically;
  drift *stumbles* stochastically — siblings, not rivals.)
- `cv_math` (S) — `logic` for CVs: min, max, average, difference,
  rectify, invert of two CV ins, every jack live at once. Same
  zero-param shape as `logic`.
- `cv_recorder` (M) — record a knob gesture or incoming CV for N
  clocked bars, loop, overdub. A modulation looper — nothing else in
  the rack captures *performance*.

**Effects**
- `rotary` (M) — Leslie: crossover + Doppler fractional delay (the
  chorus core) + slow/fast ramp between chorale and tremolo. Distinct
  from chorus/phaser/flanger in a way people can hear instantly. The
  `organ` module's destined partner.
- `vowel` (S–M) — formant filter bank with an A–E–I–O–U morph knob.
  The vocoder's expressive little cousin; pairs beautifully with
  `supersaw`. (Cousin of `wavetable_morph`'s vowel *stack* — that one
  IS the source, this one filters any source.)
- `freeze` (M) — spectral freeze: FFT a moment, hold it forever as a
  pad. A different animal from `granular`'s time-domain freeze.
- `autopan` (S) — there's no dedicated panner anywhere in the rack.
  Equal-power pan with CV in; fold tremolo into it and it's the
  missing stereo motion utility.

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
