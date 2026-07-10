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
Generative: `shift_random` `euclidean` `clock_divider` `bernoulli_gate` `burst` `arpeggiator` `chord` ·
Voices: `fm_op` `pluck` `modal` `granular` `kick_drum`/`snare_drum`/`hat_drum` ·
Visual: `scope` `spectrum` · plus quick hits at the end.

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

### `quantizer` (M) — "CV & Utilities"

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

### `slew` (S) — "CV & Utilities"

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

### `shift_random` (S–M)

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

### `euclidean` (S)

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

### `bernoulli_gate` (S)

- Ports: `in` (gate); `p_cv`; `out_a`; `out_b`.
- Params: `probability` 0..1 (0.5, chance of A) · `mode` independent|toggle ·
  `seed`.
- Each incoming gate routes whole (decision latched on the rising edge, gate
  length preserved).
- Tests: p=0 / p=1 degenerate exactness; seeded sequence reproducible;
  count(A) + count(B) = count(in) — nothing lost or doubled.

### `burst` (S)

Ratchet generator: one trigger → N gates.

- Ports: `trigger` in; `clock` in (optional — when patched, rate = clock
  division); `gate` out; `env` cv out (per-burst amplitude taper — patch to a
  VCA for decaying ratchets).
- Params: `count` 1..16 (3) · `rate` 0.5..50 Hz (8, ignored when clocked) ·
  `division` (clocked mode) · `decay` 0..1 (taper) · `spread` −1..+1
  (accel/ritard curve).
- Tests: exact gate count; timing grid; retrigger mid-burst restarts
  (document); clocked division correct.

### `arpeggiator` (M)

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

### `chord` (M)

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

## New voices — "Sources"

### `fm_op` (M)

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

### `pluck` (M–L)

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

### `modal` (M–L)

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

### Drum voices: `kick_drum`, `snare_drum`, `hat_drum` (S–M each; submit separately)

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

## Seeing the signal — "CV & Utilities"

### `scope` (M)

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

## Quick hits (S unless noted)

- `octaver` — zero-crossing flip-flop sub-octave (−1/−2 oct squares, filtered,
  mixed under the dry); dirty analog charm for bass.
- `exciter` — HP → soft nonlinearity (oversampling infra) → blend; adds air.
- `vinyl` — Poisson crackle + rumble + 33 rpm wobble; `tape`'s scrappy sibling.
- `logic` — 2-in gate AND/OR/XOR/NAND + NOT out; comparator mode with
  threshold for CVs.
- `gate_delay` — delay/stretch a gate by ms or clock division.
- `sequential_switch` — clocked 1→4 router / 4→1 selector, reset in.
- `matrix_mixer` (M) — 4×4 gain matrix, every node CV-able; enables feedback
  patching (document stability guardrails + soft-clip option).
- `macro` — one big knob → 4 scaled/offset cv outs; performance macro
  (cv_scale ×4 in one panel).
- `mid_side` — M/S encode/decode + width; completes the stereo utility story
  beside stereo_speaker_output.
- `supersaw` (S–M) — 7 detuned blep saws per voice, `detune` + `blend` +
  stereo spread; the trance chord machine.
- `wavetable_morph` (M) — scanning wavetable osc: built-in table stack +
  single-cycle WAV import (Browse/media.py), `position` + `position_cv`,
  mip-mapped like the `*_wt` shapes.
- `tuner` — pitch_detector core + a cents needle panel.
- `looper` (L) — clock-synced record/overdub/undo layer on the transport
  pattern (FilePlayer Play/Stop + resampler seam crossfades); slice it.
- Vocoder follow-ups (from TODO): stereo decorrelated bands, `formant` shift
  knob, carrier normal to noise, per-band trims.

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
