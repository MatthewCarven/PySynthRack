# PySynthRack — Module Reference

PySynthRack is a modular synthesizer: you build sound by dropping **modules**
onto a canvas and dragging **cables** between their ports, the same way you
would patch a Eurorack or VCV Rack. This document explains how the module
system works, then catalogues the modules themselves.

It has two halves:

- **[How it works](#how-it-works)** — the model, signal kinds, cabling
  rules, backends, and how to add a new module.
- **[Module catalogue](#module-catalogue)** — an at-a-glance index of every
  module, followed by detailed entries.

> Status: this is a living document. Every module appears in the
> [index table](#module-index), but full write-ups currently exist for a
> representative set (Oscillator, Filter, ADSR, Crossover, FilePlayer,
> MicInput). The remaining entries are stubbed under their category headings
> and marked _“to document”_ — fill them in using the template set as a guide.

---

## How it works

### The model: Patch, Module, Port

A **Patch** is the whole instrument: a bag of modules plus the cables between
them. It is pure data — it holds no audio code at all. Saving a patch writes
this structure to JSON; loading reads it back.

A **Module** is one node. Every module declares four things:

- a **`TYPE`** string (e.g. `"oscillator"`) used for save/load and to look up
  its renderer,
- a set of **parameters** with default values (the knobs),
- a list of **input ports**, and
- a list of **output ports**.

A **Port** is one jack on a module. It has a name, a direction (`in` / `out`),
and a **signal kind** (below). Cables connect an output port to an input port.

The model never produces sound. The active **audio backend** reads the patch
and builds its own renderer for each module — this keeps DSP code out of the
data layer, so the same patch behaves identically whether it came from the
GUI, a loaded `.json`, or a future script.

### Signal kinds

Every cable carries one of three kinds of signal, and a cable can only join
ports of the **same kind**:

| Kind | Meaning | Typical range |
|------|---------|---------------|
| `audio` | Audio-rate sound you can hear | roughly −1.0 … +1.0 |
| `cv` | Control voltage — a modulation signal (envelopes, LFOs, pitch) | unipolar 0…1 or bipolar −1…1, depending on source |
| `gate` | On/off trigger (note held / released) | 0 (off) or 1 (on) |

The **bridge modules** ([AudioToCV](#audio_to_cv), [CVToAudio](#cv_to_audio),
[Schmitt](#schmitt)) convert between kinds, so any signal can eventually reach
any destination — e.g. rectify an `audio` signal into `cv` to use a drum loop
as an envelope.

### CV depth conventions

House rule (standardised 2026-07-02): **every modulatable `*_cv` input has a
`cv_depth` parameter, measured in the target's natural unit per CV unit**, and
the UI label always shows that unit. Frequency-domain depths are **octaves per
unit** and default to `1.0` — the classic **1 V/oct** — and musical-pitch
depths are **semitones per unit** defaulting to `12.0` (≡ 1 V/oct), so every
frequency/pitch input is V/oct-calibrated out of the box. `cv_depth = 0`
disables an input without unpatching it; per-input attenuation beyond the knob
is a [CVScale](#cv_scale) away.

Two deliberate exceptions:

- **`oscillator.freq_cv` is a calibrated pitch input** (fixed 1 V/oct,
  per-sample, no knob). It's the pitch bus — keyboards, sequencers and MIDI
  emit 1 V/oct into it, and a depth knob here would silently detune patches.
  Hardware makes the same split: a calibrated V/OCT jack, and separate FM
  inputs with attenuators.
- **Amplitude multipliers (`vca.cv`, `oscillator.amp_cv`) are knobless.** The
  CV *is* the amplitude (`out = in × cv`), the modular convention; attenuate
  with the source's own level or a CVScale.

The full map:

| Module . input | `cv_depth` default | Unit per CV unit | Summing |
|----------------|--------------------|------------------|---------|
| `oscillator.freq_cv` | — (calibrated) | 1 V/oct fixed, per-sample | `freq · 2^cv[n]` |
| `oscillator.amp_cv` | — (multiplier) | linear | `amp · cv[n]` |
| `oscillator.pw_cv` | `0.5` (`pw_cv_depth`) | width (duty cycle) per unit | `clip(pulse_width + d·cv[n], 0.05, 0.95)`, per-sample, voice-aware; only `square` / `square_blep` listen (`square_wt` is a fixed 50% table) |
| `vca.cv` | — (multiplier) | linear | `audio · cv · gain` |
| `filter.cutoff_cv` | `1.0` | octaves | `cutoff · 2^(d·mean cv)` |
| `filter.resonance_cv` | `1.0` (`res_cv_depth`) | Q doublings | `resonance · 2^(d·mean cv)` (clipped 0.1…20); a `(V, F)` source gives every voice its own Q |
| `lfo.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `drift.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)`, re-read per block; mono (a `(V, F)` source is averaged) |
| `crossover.freq_cv` | `1.0` | octaves | `freq · 2^(d·mean cv)` |
| `sweep_eq.freq_cv` | `1.0` | octaves | `freq · 2^(d·mean cv)` |
| `motion_eq.band{i}_freq_cv` | `1.0` (shared) | octaves | `freq_i · 2^(d·mean cv)` |
| `motion_eq.band{i}_gain_cv` | `6.0` (shared, `gain_cv_depth`) | dB | `gain_i + d·mean cv` (clamped ±24) |
| `motion_eq.band{i}_q_cv` | `1.0` (shared, `q_cv_depth`) | Q doublings | `q_i · 2^(d·mean cv)` (clipped 0.1…20) |
| `chorus.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `flanger.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `phaser.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `ring_mod.freq_cv` | `1.0` (`freq_cv_depth`) | octaves | `freq · 2^(freq_cv_depth·cv[n])`, per-sample (internal carrier; bypassed when `carrier` patched) |
| `freq_shifter.shift_cv` | `200.0` (`shift_cv_depth`) | Hz (linear, additive) | `shift + shift_cv_depth·cv[n]`, per-sample; a shift adds Hz, not V/oct; clamped ±Nyquist |
| `resampler.pitch_cv` | `12.0` | semitones | `st + d·cv` (semitone space) |
| `sampler.start_cv` | `1.0` (`start_cv_depth`) | fraction of the file | `start + d·cv[edge]`, read at the gate edge and latched per hit; clamped 0…1 |
| `kick_drum.pitch_cv` | — (calibrated) | 1 V/oct fixed | `tune + 12·cv[edge]`, read at the trigger edge and latched per hit — the pitch bus, like `oscillator.freq_cv` |
| `euclidean.fills_cv` | `8.0` (`fills_cv_depth`) | fills (hits per loop) | `fills + round(d·cv[edge])`, read at each clock edge, clamped 0…steps |
| `burst.count_cv` | `8.0` (`count_cv_depth`) | gates per burst | `count + round(d·cv[edge])`, read at the trigger edge and latched per burst, clamped 1…16 |
| `sampler.vel` / `kick_drum.vel` / `snare_drum.vel` / `hat_drum.vel` / `adsr.vel` | — (multiplier) | linear | `hit · max(0, cv[edge])`, read at the edge and latched; a `(V, F)` source collapses to the loudest voice at that sample (the `adsr` latches per voice when its gate is `(V, F)`, and scales the whole envelope of the note — release included) |
| `pitch_shifter.pitch_cv` | `12.0` | semitones | `st + d·mean cv` |
| `bowed.pressure_cv` / `bowed.velocity_cv` | `1.0` (shared) | level (0…1) | `pressure/velocity + d·cv[n]`, per sample, clamped 0…1; mono, shared by every voice |
| `wind.breath_cv` | `1.0` | level (0…1) | `breath + d·cv[n]`, per sample, clamped 0…1; mono, shared by every voice |
| `granular.position_cv` | `1.0` (`position_cv_depth`) | fraction of the buffer | `position + d·cv[onset]`, read at each grain's onset and latched for that grain; clamped 0…1; a `(V, F)` source is averaged |
| `delay.time_cv` | `50.0` | ms | `time + d·cv` |
| `loudness.level_cv` | `1.0` | level (0…1) | `level + d·mean cv` |
| `tilt_eq.tilt_cv` | `6.0` | dB | `tilt + d·mean cv` |
| `compressor.threshold_cv` | `12.0` | dB | `threshold + d·mean cv` |
| `reverb.decay_cv` / `reverb.damping_cv` / `reverb.mix_cv` | `1.0` (shared) | level (0…1) | `decay/damping/mix + d·mean cv` |
| `mixer.gain{i}_cv` | — (multiplier) | linear, per-sample | `in_i · gain_i · cv_i` |

(Converters whose entire job is a CV mapping — `cv_to_frequency`, the bridges,
`cv_scale`/`cv_offset`, sample_hold, schmitt, sequencer — are out of scope:
their params *are* the mapping.)

### Cabling rules

- **Kinds must match.** You can't plug `cv` into an `audio` jack; the patch
  rejects it. Use a bridge module to convert.
- **One cable per input jack.** Inputs are mono — a jack takes a single cable.
  To sum several signals into one input, use a [Mixer](#mixer),
  [Combiner](#combiner), or [CVCombiner](#cv_combiner).
- **Outputs fan out freely.** One output can feed any number of inputs — just
  drag multiple cables from it. (This is why there's no “splitter” module: it
  would be redundant.)

> **Port-name gotcha:** most modules name their main audio input `in`, but the
> **VCA**'s audio input is named **`audio`** (and its control input `cv`).
> Always check a module's ports when wiring.

- **Feedback loops close, one block late.** The engine renders the graph in
  dependency order, which a loop has none of — so at compile time the cable
  that *closes* each loop (the one you drew last) becomes a **late-read**:
  its destination hears the source's *previous block*. Every loop therefore
  costs exactly one block of latency (~10.7 ms at 48 kHz / 512), sitting on
  that one cable; feed-forward paths stay instant, and the rest of the graph
  orders exactly as before. Cables into a [`matrix_mixer`](#matrix_mixer)
  are chosen first (it has been the feedback door since August, and it is
  the *guarded* one — its `soft_clip` keeps a hot loop on the ceiling);
  since 2026-09-16 any other loop closes the same way. That means a
  self-patched `eoc → trig`, a filter driven by its own envelope follower,
  a delay regenerating through a filter — all real now. Three things to
  know: the loop's timing depends on the block size (a `eoc → trig` krell's
  period is its cycle plus one block); a loop with gain above one and no
  ceiling grows until it clips (put a [`limiter`](#limiter) in it, or close
  it through the matrix) — if it ever blows past float range the engine
  scrubs the loop to silence for a block and counts it rather than
  poisoning the graph; and to move the block of latency to a different
  cable, delete and re-draw so that cable is the last one. **You can see
  all of this in the editor:** the late cable is drawn **amber** (and
  thicker) the moment the loop closes, the status bar says which cable it
  is, and the toolbar's `loops` slot counts them — hover it for the list.
  While audio runs, `loops N !K` in the warning colour means K blocks of a
  loop blew past float range and were scrubbed.

### Backends

The DSP lives behind an `AudioBackend` interface with two implementations:

- **NumpyBackend** — the real engine (pure Python + NumPy + `sounddevice`).
  This is what you hear. Every module's renderer lives here.
- **PyoBackend** — currently parked/stubbed; modules it doesn't implement run
  silent. Don't rely on it.

The backend is auto-selected at startup; override with the environment
variable `PYSYNTHRACK_BACKEND=numpy` (or `=pyo`).

**Polyphony.** Note sources ([Keyboard](#keyboard), [MIDIInput](#midi_input))
publish up to 16 per-voice signals shaped `(voices, frames)`. Voice-aware
modules process each voice independently; everything collapses (sums) to mono
at the speaker. Mono signals take a faster path, so a monophonic patch pays no
polyphony tax.

### Adding a new module

The pattern, end to end:

1. **Write the class** in `src/pysynthrack/modules/<name>.py`: subclass
   `Module`, decorate with `@register_module_type`, and declare `TYPE`,
   `CATEGORY` (the Add-menu submenu — one of `CATEGORY_ORDER` in
   `core/module.py`; forgetting it lands the module in a visible "Other"
   submenu), `DEFAULT_PARAMS`, `INPUT_PORTS`, `OUTPUT_PORTS`. No DSP here —
   it's pure data.
2. **Register it** by importing the class in `src/pysynthrack/modules/__init__.py`
   (and adding it to `__all__`).
3. **Write the renderer** in `src/pysynthrack/audio/numpy_backend.py`: add a
   `_render_<type>` method and wire it into the `_render_module` dispatch. A
   module with multiple outputs returns a dict like `{"low": ..., "high": ...}`;
   a single-output module returns one array. Read inputs with
   `_input_buffer(patch, buffers, module_id, port_name)`.
4. **(Optional) pyo** — add a builder in `pyo_backend.py`. Unknown types are a
   silent stub there, so this can wait.
5. **Test it** headlessly in `tests/test_<name>.py` (render blocks, assert the
   output) and ship an example patch in `examples/`.
6. **Document it** — add a row to the [index](#module-index) and an entry to the
   catalogue.

The UI needs no changes for most modules: it builds knobs from
`DEFAULT_PARAMS` automatically (sliders for numbers, checkboxes for booleans,
combos for known enum params, a text box otherwise) and draws a jack per port.

---

## Module catalogue

### Module index

Every module type, its category, and its ports at a glance.
(`→` separates inputs from outputs; “—” means none.) *Category* is the
submenu the module appears under in the UI's **Add module** menu — each class
declares it with a `CATEGORY` attribute (`CATEGORY_ORDER` in `core/module.py`
fixes the menu order). The detailed sections below are still organised by
signal-flow role (sources → processors → … → sinks).

| Module (`TYPE`) | Category | Inputs → Outputs |
|-----------------|----------|------------------|
| [`oscillator`](#oscillator) | Sources | `freq_cv`,`amp_cv`,`pw_cv` (cv) → `out` (audio) |
| [`keyboard`](#keyboard) | Sources | — → `out` (audio), `gate` (gate) |
| [`cv_keyboard`](#cv_keyboard) | Sources | — → `pitch_cv` (cv), `gate`, `key_c`…`key_b` (gate) |
| [`cv_gates`](#cv_gates) | Sources | — → `c4`…`e5` (cv, one enveloped gate per key) |
| [`key_trigger`](#key_trigger) | Sources | — → `out` (gate) |
| [`midi_input`](#midi_input) | Sources | — → `out` (audio), `gate`, `pitch_cv`, `mod_cv`, `pressure_cv`, `velocity_cv` |
| [`file_player`](#file_player) | Sources | — → `left`,`right` (audio) |
| [`sampler`](#sampler) | Sources | `pitch_cv` (cv), `gate` (gate), `start_cv` (cv), `vel` (cv) → `out`, `out_l`, `out_r` (audio) |
| [`mic_input`](#mic_input) | Sources | — → `left`,`right` (audio) |
| [`cv_to_frequency`](#cv_to_frequency) | Sources | `cv` (cv) → `out` (audio) |
| [`noise`](#noise) | Sources | — → `out` (audio), `cv` (cv) |
| [`fm_op`](#fm_op) | Sources | `pitch_cv`,`amp_cv`,`index_cv` (cv), `pm` (audio) → `out` (audio) |
| [`pluck`](#pluck) | Sources | `pitch_cv` (cv), `trigger` (gate) → `out` (audio) |
| [`bowed`](#bowed) | Sources | `pitch_cv`,`pressure_cv`,`velocity_cv` (cv), `gate` (gate) → `out` (audio) |
| [`wind`](#wind) | Sources | `pitch_cv`,`breath_cv` (cv), `gate` (gate) → `out` (audio) |
| [`modal`](#modal) | Sources | `excite` (audio), `pitch_cv` (cv) → `out`, `out_l`, `out_r` (audio) |
| [`kick_drum`](#kick_drum) | Sources | `trigger` (gate), `vel`, `pitch_cv` (cv) → `out` (audio) |
| [`snare_drum`](#snare_drum) | Sources | `trigger` (gate), `vel` (cv) → `out` (audio) |
| [`hat_drum`](#hat_drum) | Sources | `closed_trigger`,`open_trigger` (gate), `vel` (cv) → `out` (audio) |
| [`organ`](#organ) | Sources | `pitch_cv` (cv), `gate` (gate) → `out` (audio) |
| [`supersaw`](#supersaw) | Sources | `freq_cv`,`amp_cv` (cv) → `out_l`,`out_r` (audio) |
| [`wavetable_morph`](#wavetable_morph) | Sources | `freq_cv`,`position_cv`,`amp_cv` (cv) → `out` (audio) |
| [`filter`](#filter) | Filters & EQ | `in` (audio), `cutoff_cv` (cv), `resonance_cv` (cv) → `out` (audio) |
| [`crossover`](#crossover) | Filters & EQ | `in` (audio), `freq_cv` (cv) → `low`,`high` (audio) |
| [`parametric_eq`](#parametric_eq) | Filters & EQ | `in` (audio) → `out` (audio) |
| [`sweep_eq`](#sweep_eq) | Filters & EQ | `in` (audio), `freq_cv` (cv) → `out` (audio) |
| [`motion_eq`](#motion_eq) | Filters & EQ | `in` (audio), `band{i}_freq_cv`, `band{i}_gain_cv`, `band{i}_q_cv` ×4 (cv) → `out` (audio) |
| [`tilt_eq`](#tilt_eq) | Filters & EQ | `in` (audio), `tilt_cv` (cv) → `out` (audio) |
| [`vca`](#vca) | Routing & VCA | `audio` (audio), `cv` (cv) → `out` (audio) |
| [`matrix_mixer`](#matrix_mixer) | Routing & VCA | `in_1`…`in_4` (audio), `cv_1`…`cv_4` (cv) → `out_1`…`out_4` (audio) |
| [`resampler`](#resampler) | Effects | `in` (audio), `pitch_cv` (cv), `brake` (gate) → `out`, `out_l`, `out_r` (audio) |
| [`pitch_shifter`](#pitch_shifter) | Effects | `in` (audio), `pitch_cv` (cv) → `out`, `out_l`, `out_r` (audio) |
| [`granular`](#granular) | Effects | `in` (audio), `position_cv` (cv), `freeze` (gate) → `out`, `out_l`, `out_r` (audio) |
| [`delay`](#delay) | Effects | `in` (audio), `time_cv` (cv) → `out` (audio) |
| [`reverb`](#reverb) | Effects | `in` (audio), `decay_cv`,`damping_cv`,`mix_cv` (cv), `freeze` (gate) → `out_l`,`out_r` (audio) |
| [`compressor`](#compressor) | Effects | `in`,`sidechain` (audio), `threshold_cv` (cv) → `out` (audio), `gr` (cv) |
| [`limiter`](#limiter) | Effects | `in` (audio) → `out` (audio) |
| [`noise_gate`](#noise_gate) | Effects | `in`,`sidechain` (audio) → `out` (audio), `open` (cv) |
| [`transient_shaper`](#transient_shaper) | Effects | `in` (audio) → `out` (audio) |
| [`loudness`](#loudness) | Filters & EQ | `in` (audio), `level_cv` (cv) → `out` (audio) |
| [`distortion`](#distortion) | Effects | `in` (audio), `drive_cv` (cv) → `out` (audio) |
| [`waveshaper`](#waveshaper) | Effects | `in` (audio), `fold_cv` (cv) → `out` (audio) |
| [`octaver`](#octaver) | Effects | `in` (audio) → `out` (audio) |
| [`ring_mod`](#ring_mod) | Effects | `in`,`carrier` (audio), `freq_cv` (cv) → `out` (audio) |
| [`freq_shifter`](#freq_shifter) | Effects | `in` (audio), `shift_cv` (cv) → `out_up`,`out_down` (audio) |
| [`bitcrusher`](#bitcrusher) | Effects | `in` (audio), `bits_cv`,`rate_cv` (cv) → `out` (audio) |
| [`tape`](#tape) | Effects | `in` (audio) → `out` (audio) |
| [`vinyl`](#vinyl) | Effects | `in` (audio) → `out` (audio) |
| [`convolver`](#convolver) | Effects | `in` (audio) → `out_l`,`out_r` (audio) |
| [`chorus`](#chorus) | Effects | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`rotary`](#rotary) | Effects | `in` (audio), `fast` (gate) → `out_l`,`out_r`,`out` (audio) |
| [`flanger`](#flanger) | Effects | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`phaser`](#phaser) | Effects | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`vocoder`](#vocoder) | Effects | `mod`,`carrier` (audio) → `out` (audio) |
| [`lfo`](#lfo) | Modulation | `rate_cv` (cv), `reset` (gate) → `cv` (cv) |
| [`adsr`](#adsr) | Modulation | `gate` (gate), `vel` (cv) → `cv` (cv) |
| [`ad_envelope`](#ad_envelope) | Modulation | `trig` (gate) → `cv` (cv) |
| [`function_generator`](#function_generator) | Modulation | `trig` (gate), `rate_cv`,`rise_cv`,`fall_cv` (cv) → `out`,`out_inv` (cv), `eor`,`eoc` (gate) |
| [`clock`](#clock) | Modulation | — → `out` (gate) |
| [`sequencer`](#sequencer) | Modulation | `clock`,`reset` (gate) → `cv` (cv), `gate` (gate) |
| [`fader_seq`](#fader_seq) | Modulation | `clock`,`reset` (gate) → `cv` (cv), `gate` (gate) |
| [`shift_random`](#shift_random) | Modulation | `clock`,`write` (gate) → `cv` (cv), `gate` (gate) |
| [`possibility_seq`](#possibility_seq) | Modulation | `clock`,`reset`,`reroll` (gate) → `gate` (gate) |
| [`possibility_selector`](#possibility_selector) | Modulation | `in`,`clock`,`reset`,`reroll` (gate) → `out1`–`out4` (gate) |
| [`chaos`](#chaos) | Modulation | `reset` (gate) → `x`,`y`,`z` (cv), `gate` (gate) |
| [`drift`](#drift) | Modulation | `rate_cv` (cv), `clock` (gate) → `cv`,`stepped` (cv), `trig` (gate) |
| [`euclidean`](#euclidean) | Modulation | `clock`,`reset` (gate), `fills_cv` (cv) → `gate`,`accent` (gate) |
| [`burst`](#burst) | Modulation | `trigger`,`clock` (gate), `count_cv` (cv) → `gate` (gate), `env` (cv) |
| [`bernoulli_gate`](#bernoulli_gate) | Modulation | `in` (gate), `p_cv` (cv) → `out_a`,`out_b` (gate) |
| [`clock_divider`](#clock_divider) | Modulation | `clock`,`reset` (gate) → `div2`,`div4`,`div8`,`divn`,`mult` (gate) |
| [`arpeggiator`](#arpeggiator) | Modulation | `pitch_cv` (cv), `gate`,`clock` (optional — internal bpm otherwise),`reset` (gate) → `pitch_cv` (cv), `gate` (gate) |
| [`logic`](#logic) | Modulation | `a`,`b` (gate) → `and`,`or`,`xor`,`nand`,`not_a` (gate) |
| [`audio_to_cv`](#audio_to_cv) | CV & Utilities | `in` (audio) → `cv` (cv) |
| [`cv_to_audio`](#cv_to_audio) | CV & Utilities | `cv` (cv) → `out` (audio) |
| [`schmitt`](#schmitt) | CV & Utilities | `in` (cv) → `gate` (gate) |
| [`mixer`](#mixer) | Routing & VCA | `in1`–`in4` (audio), `gain1_cv`–`gain4_cv` (cv) → `out` (audio) |
| [`combiner`](#combiner) | Routing & VCA | `in1`–`in4` (audio) → `out` (audio) |
| [`cv_combiner`](#cv_combiner) | Routing & VCA | `in1`–`in4` (cv) → `out` (cv) |
| [`mid_side`](#mid_side) | Routing & VCA | `in_l`,`in_r` (audio), `width_cv` (cv) → `mid`,`side`,`out_l`,`out_r` (audio) |
| [`constant`](#constant) | CV & Utilities | — → `out` (cv) |
| [`cv_scale`](#cv_scale) | CV & Utilities | `in` (cv) → `out` (cv) |
| [`cv_offset`](#cv_offset) | CV & Utilities | `in` (cv) → `out` (cv) |
| [`cv_math`](#cv_math) | CV & Utilities | `a`,`b` (cv) → `min`,`max`,`avg`,`diff`,`mult`,`rect`,`inv` (cv) |
| [`cv_recorder`](#cv_recorder) | CV & Utilities | `in` (cv), `clock`,`rec`,`clear` (gate) → `out`,`pos` (cv) |
| [`sample_hold`](#sample_hold) | CV & Utilities | `in` (cv), `trig` (gate) → `out` (cv) |
| [`slew`](#slew) | CV & Utilities | `in`, `rise_cv`, `fall_cv` (cv), `clock` (gate) → `out` (cv) |
| [`quantizer`](#quantizer) | CV & Utilities | `in` (cv), `gate` (gate) → `out` (cv), `changed` (gate) |
| [`chord`](#chord) | CV & Utilities | `pitch_cv` (cv), `gate` (gate) → `pitch_cv` (cv), `gate` (gate, both (4, F)), `changed` (gate, mono) |
| [`scope`](#scope) | CV & Utilities | `in`,`in_r` (audio), `cv` (cv), `trig` (gate) → `out`,`out_r` (audio) |
| [`meter`](#meter) | CV & Utilities | `in`, `in_r` (audio) → `out`, `out_r` (audio) |
| [`speaker_output`](#speaker_output) | Outputs | `in` (audio) → — |
| [`left_speaker_output`](#left_speaker_output) | Outputs | `in` (audio) → — |
| [`right_speaker_output`](#right_speaker_output) | Outputs | `in` (audio) → — |
| [`stereo_speaker_output`](#stereo_speaker_output) | Outputs | `in_l`,`in_r` (audio), `pan_cv`,`width_cv` (cv) → — |
| [`specific_stereo_speaker_output`](#specific_stereo_speaker_output) | Outputs | `in_l`,`in_r` (audio), `pan_cv`,`width_cv` (cv) → — |
| [`buffered_specific_speaker_output`](#buffered_specific_speaker_output) | Outputs | `in_l`,`in_r` (audio), `pan_cv`,`width_cv`,`ratio_cv` (cv) → `fill` (cv) |
| [`warping_buffered_speaker_output`](#warping_buffered_speaker_output) | Outputs | `in_l`,`in_r` (audio), `pan_cv`,`width_cv`,`ratio_cv` (cv) → `fill` (cv) |
| [`disk_writer`](#disk_writer) | Outputs | `in` (audio) → — |

---

### Sources

Modules that generate or bring in signal — the start of a patch.

#### `oscillator`

The workhorse tone generator: a periodic waveform at a chosen pitch, with
optional CV modulation of pitch, amplitude and — for the square shapes —
pulse width.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `freq_cv` | in | cv | 1 volt/octave pitch modulation. `freq` becomes `freq · 2^cv` per sample, so a bipolar LFO here is vibrato and an audio-rate signal is FM. Unpatched = no modulation. |
| `amp_cv` | in | cv | Linear amplitude modulation (`amp · cv`). A unipolar LFO here is tremolo/AM. Unpatched = no modulation. |
| `pw_cv` | in | cv | Pulse-width modulation of the `square` / `square_blep` shapes: `pw[n] = clip(pulse_width + pw_cv_depth · cv[n], 0.05, 0.95)` per sample. Voice-aware like `freq_cv` — a `(V, F)` source gives every voice its own width, a mono source is shared. A slow LFO here is the classic PWM pad. Unpatched = the static `pulse_width`. |
| `out` | out | audio | The waveform. |

**Parameters**

| Param | Default | Options / range | Description |
|-------|---------|-----------------|-------------|
| `waveform` | `sine` | `sine`, `saw`, `square`, `triangle`, plus `*_blep` and `*_wt` variants of saw/square/triangle | Shape + band-limiting. Naive shapes are cheap but alias; `_blep` (PolyBLEP/PolyBLAMP) and `_wt` (band-limited wavetable) are anti-aliased. `sine` is already band-limited. |
| `freq` | `440.0` | Hz | Base pitch when `freq_cv` is unpatched. |
| `amp` | `0.5` | 0…1 | Linear output level. |
| `pulse_width` | `0.5` | 0.05…0.95 | Duty cycle of `square` / `square_blep` (the pulse is high while the phase is below the width; 0.5 = the classic symmetric square). Ignored by every other shape, including `square_wt`. |
| `pw_cv_depth` | `0.5` | width per CV unit | Scales `pw_cv`. At 0.5 a bipolar ±1 LFO sweeps the whole 0.05…0.95 band. |

**Pulse width.** `square` and `square_blep` are pulse waves: `pulse_width`
sets the duty cycle and `pw_cv` moves it per sample, clamped to 0.05…0.95
(narrower is a click train; at the rails the two edges' band-limiting
windows would start to overlap at ordinary pitches). A pulse that isn't 50%
carries a DC offset of `2·pw − 1` (a 20% pulse averages −0.6), and a slow
PWM sweep would push the speaker cone in and out with it — so the offset is
removed per sample (`out = pulse − (2·pw − 1)`). At 0.5 the correction is
exactly zero, which is why a patch saved before PWM existed renders bit for
bit as it did. `square_blep` keeps both edges band-limited while the width
moves: the rising edge is at phase 0 as ever, and the falling edge's
PolyBLEP runs on the falling edge's own phase `(phase − pw) mod 1` with a
window sized by that phase's own per-sample increment (`dt − dpw`), so a
width sweeping against the phase still gets exactly one before/after
correction pair per edge. At width 0.1 the blep's alias floor stays within
a factor of 1.5 of the 50% square's, sixty times below the naive pulse.
**`square_wt` does not do PWM** — it is a fixed 50% mipmap table (the
wavetable flavour is for band-limited vintage tone, not width modulation);
it ignores `pulse_width` and `pw_cv`, and that is a documented limit, not
a bug.

**Patching.** The canonical voice is `oscillator → filter → vca → speaker`,
with an `adsr` driving the VCA's `cv`. See `examples/hello_sine.json` and
`examples/fat_saw.json`. For PWM, `lfo.cv → oscillator.pw_cv` on a
`square_blep` (see `examples/oscillator_pwm.json`, the classic pad: two
pulses, two out-of-step LFOs, a breathing lowpass).

#### `keyboard`

_To document._ Computer-keyboard note source (polyphonic). Outputs `out`
(audio) and `gate`. Params: `octave`, `waveform`, `amp`. See
`examples/keyboard_play.json`, `examples/keyboard_adsr.json`.

#### `cv_keyboard`

The **controller** sibling of [`keyboard`](#keyboard): the computer keys
emit **CV and gate only** — no internal oscillator — so you build the voice
yourself out in the patch (oscillator → filter → VCA → whatever). Same keys,
a different sound every patch, exactly like a hardware modular keyboard. It
shares Keyboard's 16-slot polyphony and accepts the same physical keys (both
modules can be in a patch at once and play together).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `pitch_cv` | out | cv | Held note as a **1V/octave** control voltage, **C4 (MIDI 60) = 0 V** (each semitone = 1/12). Per-voice. Wire into an [`oscillator`](#oscillator)'s `freq_cv` (set the osc's base `freq` to C4 = 261.6256 Hz to track in tune) or into [`cv_to_frequency`](#cv_to_frequency). Pitch is held through a voice's release tail so an ADSR release stays in tune. |
| `gate` | out | gate | High while a key is held, per voice — drives one [`adsr`](#adsr)/[`ad_envelope`](#ad_envelope) envelope per note. |
| `key_c` … `key_b` | out | gate | Twelve per-pitch-class gates ("all the keys are CV outs"). Each is high while **any** held voice is that pitch class (octave-folded: C4 and C5 both raise `key_c`). Patch one into a kick, another into a snare, etc. — a different module triggered per key. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `octave` | `4` | int | Base octave for the home row (same mapping as `keyboard`: home-row `A` = C of this octave). |

**Patching.** The voice **must** pass through a `gate`-driven VCA — an
oscillator drones on every voice slot (idle slots sit at `pitch_cv` = 0 = the
C4 reference), and the gate/VCA is what articulates notes and silences the
idle voices. Typical chain: `cv_keyboard.pitch_cv → oscillator.freq_cv`,
`oscillator.out → vca.audio`, `cv_keyboard.gate → adsr.gate`,
`adsr.cv → vca.cv`. For a per-key drum, `cv_keyboard.key_c → adsr.gate` on a
separate noise voice. See `examples/cv_keyboard_external_voice.json`.

#### `cv_gates`

A bank of **per-key enveloped CV gates** for amplitude/trigger control — the
amplitude counterpart to [`cv_keyboard`](#cv_keyboard) (which puts out pitch).
Every one of the 17 home-row keys (`A`…`;` → C4 up to E5) has its **own** CV
output that idles at 0 and, while the key is held, runs a shared ADSR toward
1. Patch one key's jack into the `amp_cv` of three [`oscillator`](#oscillator)s
(or three [`vca`](#vca)s) and a single keystroke envelopes all three together
— fan-out is free, since one output port can feed any number of cables.
Accepts the same physical keys as the other keyboards (they can all be in a
patch at once).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `c4` … `e5` | out | cv | Seventeen mono CV jacks, one per physical key, labelled by the note each plays. Each idles at 0 and, while its key is down, attacks toward 1, decays to `sustain`, holds, then releases to 0 on key-up. Independent per key (holding C doesn't disturb E). Drive an [`oscillator`](#oscillator) `amp_cv`, a [`vca`](#vca) `cv`, or any CV input. |

**Parameters** (one shared ADSR for the whole bank)

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `attack` | `0.01` | 0…5 s | Time for the 0 → 1 ramp on key-down. 0 = instant. |
| `decay` | `0.10` | 0…5 s | Time from 1.0 down to `sustain`. |
| `sustain` | `0.80` | 0…1 | Held level while the key stays down (`1.0` = no decay dip). |
| `release` | `0.30` | 0…5 s | Time from the key-up level down to 0. A release from mid-attack still takes the full window (no snap); re-pressing mid-release attacks from the current level (no click). |

**Patching.** No internal voice and no pitch — `cv_gates` is purely a source
of enveloped control voltages keyed to the computer keyboard. Headline use:
`cv_gates.c4 → oscillator.amp_cv` on each of several oscillators summed into a
[`mixer`](#mixer), so one key swells a whole chord. See
`examples/cv_gates_amp.json`.

#### `key_trigger`

**One bound key → one control signal.** A single-purpose controller: each
node listens for **one** physical key and puts out a gate/trigger/latch when
you press it. Drop as many as you like — one per key — and wire each
independently, so a busy patch reads as a swarm of small labelled "this key
does this one thing" nodes rather than one fat keyboard with a cable tangle.
Fan-out is free, so one key can drive a whole rack of destinations at once.

Where [`keyboard`](#keyboard) / [`cv_keyboard`](#cv_keyboard) /
[`cv_gates`](#cv_gates) route the home-row keys as *notes* (a fixed keymap),
`key_trigger` binds **any** single key — letters, the number row,
punctuation, space — because the UI feeds it *raw* key events by name, not
note-mapped ones. Bind a key the note keyboards don't use and it's a
dedicated control; bind one they do and both respond (fan-out is free). App
shortcuts always win: a bound key doesn't fire while a modifier is held
(so `Ctrl+`-chords are safe) or while you're typing in a field, and the
reserved keys (Delete/Backspace, etc.) can't be bound.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `out` | out | gate | The control signal in {0, 1}, shaped by `mode`. |

**Parameters**

| Param | Default | Options / range | Description |
|-------|---------|-----------------|-------------|
| `key` | `""` | a key name | The bound physical key (e.g. `Q`, `5`, `Semicolon`, `Spacebar`). `""` = unbound (idles at 0). Set with the node's **Learn** button (click Learn, press a key; click again to cancel), stored as a portable name so a saved patch rebinds on any machine. |
| `mode` | `gate` | `gate` / `trigger` / `latch` | How a press shapes `out`: **gate** = high while the key is held; **trigger** = a short (~5 ms) pulse on each press, for clocking a [`sequencer`](#sequencer), resetting a [`clock`](#clock), or firing an [`ad_envelope`](#ad_envelope); **latch** = each press *toggles* the output and it holds through key-up until the next press (tap-on / tap-off). |

**Patching.** No pitch, no velocity, no envelope — shape it downstream with an
[`adsr`](#adsr)/[`ad_envelope`](#ad_envelope) if you want a contour. Headline
use: a `latch`-mode key into a [`resampler`](#resampler)'s `brake` — tap once
to tape-stop, tap again to spin back up — see
`examples/key_trigger_latch_brake.json`. Or `trigger` mode into a
[`sequencer`](#sequencer) `clock`/`reset`, or `gate` mode into an
[`adsr`](#adsr) for a hands-on one-key voice.

#### `midi_input`

Hardware-MIDI note source (polyphonic, 16 voice slots) — the external-
keyboard sibling of [Keyboard](#keyboard). Needs the `[midi]` extra
(`pip install -e ".[midi]"`); without it the node still appears but logs
what to install. Handles note on/off (incl. running-status note-offs),
pitch wheel, mod wheel (CC 1), sustain pedal (CC 64), channel aftertouch,
and All Notes Off (CC 123).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `out` | out | audio | Summed voices (per-voice rows to voice-aware consumers). |
| `gate` | out | gate | High while any key is held or sustained. |
| `pitch_cv` | out | cv | Pitch-wheel deflection as 1V/oct CV (`bend * bend_range / 12`). |
| `mod_cv` | out | cv | Mod wheel (CC 1), `[0, 1] * mod_scale`. |
| `pressure_cv` | out | cv | Channel aftertouch, `[0, 1] * pressure_scale`. |
| `velocity_cv` | out | cv | Per-voice note-on velocity, `[0, 1]` after the calibration curve, held for the life of the slot (0 where nothing plays). Always the real velocity — `velocity_sensitive` only governs the built-in tone. Patch into a [`sampler`](#sampler)'s `vel`. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `device` | `""` | MIDI input name | `""` = auto-pick the first available. Dropdown lists ports at node creation; the **Refresh** button beside it re-enumerates in place after hot-plugging. |
| `channel` | `0` | 0…16 | MIDI channel filter; `0` = omni (all channels). |
| `octave_shift` | `0` | -4…4 | Integer transpose in octaves, applied at note arrival. |
| `velocity_sensitive` | `true` | bool | `false` plays every note at unit velocity. |
| `velocity_curve` | `{}` | `{note: mult}` | Per-key velocity calibration — see below. Edited via the **Calibrate keys...** dialog, not a plain widget. |
| `waveform` | `"sine"` | osc shapes | Same vocabulary as [`oscillator`](#oscillator), incl. `*_blep` / `*_wt`. |
| `amp` | `0.5` | 0…1 | Master level after voice summing. |
| `bend_range` | `2.0` | semitones | Full wheel deflection = ±`bend_range` semitones (GM default 2). |
| `mod_scale` | `1.0` | ≥0 | Multiplier on the normalized mod wheel before `mod_cv`. |
| `pressure_scale` | `1.0` | ≥0 | Multiplier on normalized aftertouch before `pressure_cv`. |

**Per-key velocity calibration.** Budget keybeds drift key-by-key — the
same finger force yields different velocities on different keys.
`velocity_curve` maps *raw* MIDI note (the physical key, pre-
`octave_shift`; string keys, JSON-canonical) to a multiplier applied to
the normalized note-on velocity, clamped back into [0, 1]. Keys absent
from the map play at 1.0. The node's **Calibrate keys...** dialog drives
it: press **Learn**, play every key a few times at the *same intended
force* (notes keep sounding — learn is a tap, not a detour; a live
`keys / hits` readout ticks as you play), then **Compute**. Each key's
hits are averaged and normalized to the mean captured level (quiet keys
boosted, hot keys tamed); captured multipliers *merge* into the existing
curve, and every key gets an editable row in the dialog's table
(hand-trim, per-key remove, **Clear all**). Capture records raw
velocities, so re-learning over an existing curve never compounds it.

See `examples/midi_lead.json`.

#### `file_player`

Streams an **audio file** into the patch as a stereo audio source — so a
recorded track can be split and used as sound or modulation. WAV always
works (no extra deps); with ffmpeg present it also reads mp3/flac/ogg/m4a
and the **audio track of video files** (mp4/mkv/mov/webm). Decoding runs on
a **background thread** (kicked at compile, resampled to the engine rate if
needed): playback starts once ~0.5 s is buffered, so even a feature-length
video never stalls the audio thread. If the playhead ever catches a
still-running decode it holds in place and resumes seamlessly; a `loop`
plays linearly until the full length is known, then wraps. Once decoded,
steady-state playback is an in-memory array slice — no per-block disk I/O.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `left` | out | audio | Left channel. A mono file is duplicated to both; >2 channels keep the first two. |
| `right` | out | audio | Right channel. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `path` | `""` | file path | Path to an audio/video file — type it or use the node's **Browse...** button. WAV always works; other formats (mp3/flac/ogg, video-audio) need ffmpeg. Empty/missing/unreadable → silence (the patch still loads). A **relative** path is looked up next to the patch file first, then in its parent folder, then in the directory you launched from — so a patch plus its media travels as a unit and plays the same however the app was started. A file that fails to load says so in the status bar instead of quietly playing silence. |
| `gain` | `1.0` | 0…2 | Linear gain on both channels. |
| `loop` | `false` | bool | `true` repeats seamlessly; `false` (default) plays once then silence until restart/re-arm. |
| `armed` | `true` | bool | `false` outputs silence and parks the playhead at the start, so re-arming replays from the top. |
| `playing` | `true` | bool | Tape-transport pause: `false` holds the playhead in place (silent); `true` resumes from the same spot. Driven by the node's **Play**/**Stop** buttons. |
| `playlist` | `[]` | list of paths | Queue of files that auto-play (then drop off the list) after `path` — see **File list** below. Round-trips with the patch; edited via the node's list, not typed. |

**File list (queue).** Under the transport row the node carries a
**file list**: an **Up next** listbox (rows numbered `1. …`, `2. …`) with
**Add to list...** (same picker as **Browse...**), **Remove** (drops the
selected row), and **Clear** (empties the list). When a one-shot track
(`loop` off) reaches its end, the head of the list loads into `path` and plays
from 0:00 — and is removed from the list — so the player works as a simple
gapless playlist that drains to empty and then falls silent. A queued file that
**can't be decoded** (missing, unreadable, not audio) is **auto-skipped** to the
next good track instead of stalling the list on it. A player left on an empty
`path` with a queued list kicks off its first track automatically once audio is
running (no initial **Browse...** needed). Auto-advance is a GUI behaviour; the
engine only ever sees an ordinary `path` change. (`loop` on = the current track
repeats and the queue never advances.)

**Transport.** The node carries tape-style buttons: **Play** resumes,
**Stop** pauses in place (both drive the `playing` param and its checkbox),
**|<** rewinds to 0:00 — honoured at the next block boundary whether playing
or paused — and **>>|** skips to the next queued track immediately (the manual
mate to the auto-advance; a no-op when the queue is empty). `armed` remains the
coarser control (off = silent *and* parked at the start). One-shots also rewind
when the audio transport stops.

**Seek bar.** Below the transport row a **seek / scrub bar** shows the current
position as a fill and lets you jump anywhere in the track: drag its thumb (or
click the bar) and the playhead moves there on release — block-aligned and
honoured whether playing or paused, exactly like **|<**. While you drag, the
thumb follows the mouse instead of the playhead; let go and it resumes tracking.
The `m:ss / m:ss` readout beside the buttons is the human-readable time (during a
long decode the right-hand number is the *buffered* length so far, and the bar
seeks within that). Backends without the seek hook (the pyo stub) leave the bar
inert.

**Notes.** A **Browse...** button beside the path field opens a file picker
(audio + video formats) and writes the chosen path back into the field; the
player starts a fresh background decode on the next block. Non-WAV formats
are decoded by ffmpeg, found either from the `[media]` extra
(`pip install -e ".[media]"`, a bundled binary that also travels inside the
packaged exe) or a system `ffmpeg` on PATH; without ffmpeg, non-WAV files
play silence. The node shows a live `elapsed / total` readout; while a long
file is still decoding, the total is the buffered length so far and grows
until the decode completes (a free loading indicator). See
`examples/file_crossover_split.json`
(track → crossover → AudioToCV → oscillator/CVToFrequency).

#### `mic_input`

Live **microphone** (or any input device) as a stereo audio source — run a
voice through the modular graph.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `left` | out | audio | Left capture channel (mono device → duplicated to both). |
| `right` | out | audio | Right capture channel. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `device` | `""` | input device name | `""` = system default input. The UI offers a dropdown of capture devices; the **Refresh** button beside it re-enumerates in place after hot-plugging. |
| `gain` | `1.0` | 0…2 | Linear gain on both channels. |

**How capture works.** When a patch contains a mic, the backend opens a
**full-duplex** audio stream (input + output together); patches without one
keep the cheaper output-only stream. If the input device can't be opened
(busy, no permission, rate mismatch) it falls back to output-only and the mic
renders silence — check the console for a warning.

> ⚠️ **Feedback:** if the mic output reaches speakers in the same room as the
> mic, you'll get a howl. **Wear headphones.**

See `examples/mic_beatbox_crossover.json` (beatbox → crossover → low band
drives a sub-osc amp, high band steers a pitched zap).

#### `cv_to_frequency`

_To document._ Self-contained CV-controlled oscillator: maps a `cv` input to
Hz via a three-point curve (`f0`/`fm`/`f1`, `mode` log/linear), with an
optional negative-side mirror. Outputs `out` (audio). See
`examples/cvtofreq_blip.json`.

---

#### `noise`

White or pink noise with no inputs and two output jacks carrying the
*same* stream: `out` (audio) to drive filters/speakers directly (hats,
snares, wind, breath) and `cv` to drive modulation directly — the
textbook random-voltage source for `sample_hold`. Two jacks so neither
use needs a bridge, the way Keyboard exposes `out` + `gate`.

`color` selects `white` (flat spectrum; uniform ±1) or `pink`
(−3 dB/oct, equal power per octave — the tilt of rain and rushing
water). Pink is white filtered through a 3rd-order pinking IIR
(`scipy.signal.lfilter`, state carried across blocks), RMS-normalised
so `amp` means the same level for both colors. `amp` scales both jacks
(white is hard-bounded to ±amp; pink's occasional peaks run slightly
past it). Output is mono — a source has no voice context of its own and
broadcasts cleanly to any per-voice consumer. See
`examples/noise_hat.json`.

---

#### `fm_op`

One DX-style phase-modulation **FM operator**: a sine oscillator whose phase is
modulated by an audio-rate input. That single primitive is the whole of FM
synthesis — patch one operator's `out` into another's `pm` and stack them
(two make a bell, three make an electric piano). Each operator carries its own
level envelope via `amp_cv`, exactly like a DX7 voice.

Per sample: `core = sin(2π·phase + index·pm + feedback·core_prev)` and
`out = amp_cv · core`. `phase` integrates the carrier frequency, so `pitch_cv`
is a true per-sample 1 V/oct input (C4 = 0 V). `pm` is added directly into the
sine argument, which is in radians, so **`index` is the peak phase deviation in
radians for a full-scale `pm`** — a unit sine into `pm` at `index = I` gives
the classic modulation index `β = I`, and at a 1:1 ratio the output shows the
textbook Bessel sideband amplitudes `J_k(β)`.

Frequency: normally `261.6256 Hz (C4) · 2**pitch_cv · ratio · 2**(fine/1200)`.
`ratio` snaps to the nearest entry of a harmonic table (0.25 .. 16, a UI combo)
so hand-dialled values land on musical partials; `fine` detunes ±50 cents. In
`fixed` mode the carrier ignores `pitch_cv`/`ratio`/`fine` and runs at a
constant `freq` Hz. `feedback` (0..1) feeds the previous output back into the
phase — a lone operator brightens toward a saw. `index_cv` (× `index_cv_depth`)
modulates the index from an envelope/LFO — the central FM gesture, since the
index *is* the brightness (effective index floored at 0).

**Ports:** `pitch_cv` (cv, 1 V/oct; unpatched → base pitch), `pm` (audio phase
modulator), `amp_cv` (cv output level; unpatched → unity), `index_cv` (cv) →
`out` (audio). Voice-aware: a single voice row is bit-identical to mono, voices
keep independent phase/feedback state, and the output is block-size independent
(< 1e-6). `feedback = 0` runs a vectorized block path; any `feedback > 0`
drops to a per-sample loop (bit-identical to the block path at 0). Pairs with
`ring_mod` / `freq_shifter` as the inharmonic corner, but is a full voice. See
`examples/fm_op_bell.json` (2-op bell) and `examples/fm_op_epiano.json`
(3-op electric piano).

#### `sampler`

**Pitched sample playback with per-voice playheads** — the gap
[`file_player`](#file_player) doesn't fill. FilePlayer is a *transport*:
one playhead, one speed, transport buttons, you point it at a track and it
plays. This is a *voice*: each voice gets its own playhead running at a rate
set by `pitch_cv`, started by a `gate` edge. Load one recording, tell it
what note the recording **is**, and the rack's 16-slot voice architecture
turns it into a mellotron, a rompler or a breaks machine with no extra
patching — a [`cv_keyboard`](#cv_keyboard) or [`midi_input`](#midi_input) in
front gives you sixteen independent playheads.

`root` is the whole trick: the note the sample was recorded at. Play that
note and the rate is exactly 1.0 and the file comes back **untouched**
(bit-for-bit — see below). Play an octave up and it reads every other
sample. `tune` (±12 semitones) and `fine` (±50 cents) stack on top.

`mode` decides what a gate means:

* **`one_shot`** (default) — a rising edge fires the whole region and the
  gate's *length is ignored*, the drum-voice idiom. Right for hits and
  breaks, and what lets a [`euclidean`](#euclidean) or a [`clock`](#clock)
  drive it directly.
* **`gated`** — sounds while the gate is high, fading on the fall over
  `release`. Right for played notes.
* **`loop`** — `gated`, plus the playhead wraps `loop_end` back to
  `loop_start` instead of running out, so a three-second recording sustains
  for as long as you hold the key. The mellotron mode.

`start` / `end` cut a region out of the file (0..1 of its length),
sample-exact. Three samplers pointed at three regions of one drum loop, each
triggered by its own euclidean, is a breaks machine built out of nothing but
those two knobs — see `examples/sampler_breaks.json`.

**The loop is where a recording becomes an instrument.** `loop_start` and
`loop_end` are fractions **of the region**, not of the file, so moving
`start`/`end` carries the loop along instead of stranding it. The playhead
still begins at `start`, so everything before `loop_start` plays *once* as
the note's attack and only the loop repeats — put `loop_start` past the bow
scrape or the mallet strike and you get a natural onset over an endless
sustain. A collapsed or inverted loop region (`loop_end` at or below
`loop_start`) is not an error and not silence: it simply doesn't loop, and
the voice plays as `gated`. This is a slider you can drag past its partner,
and going quiet would be a trap rather than an answer.

`loop_xfade` hides the seam. Over the last stretch of the loop the read
crossfades into **the previous lap** — the same signal one loop length
earlier, which is the material running into `loop_start` — so where a bare
wrap would step from one waveform to an unrelated one, the two are faded
across and arrive at `loop_start` already matching. On the shipped
`pad_c4.wav` that takes the worst sample-to-sample step at the seam from
0.285 down to 0.035, which is *below* the recording's own natural worst
step of 0.158: the seam ends up quieter than the material around it. The
fade is measured on the **sample**, not the clock, so it covers the same
slice of waveform whatever the pitch, and it is clamped to the loop length
— there is only one lap to fade into. A `loop_start` sitting at the very
beginning of the file has no previous lap, so the fade runs into the file's
first sample instead: still continuous, because that is exactly where the
wrap lands. Set it to **0** for a hard seam — right when the loop is
already cut on a zero crossing, and the only way to keep a loop bit-exact
(a unity-rate loop with no fade is a bit-exact *tiling* of the file).

**`attack` / `release` are declick ramps, not an envelope.** They exist so a
region boundary landing mid-waveform doesn't tick. For real shaping patch an
[`adsr`](#adsr) into a [`vca`](#vca), the way every other source here
expects; raise `attack` only if your `start` point clicks.

**The neutral is bit-exact.** Root pitch, `start` 0, `end` 1, `attack` 0,
`level` 1 → rate exactly 1.0 → every read position an integer → the output
*is* the decoded file, sample for sample. The 4-tap cubic read is the
[`resampler`](#resampler)'s, whose spline returns `p0` untouched at an
integer position; an octave up is likewise a bit-exact `[::2]`. Nothing is
faded at the region end for the same reason — a sample that stops abruptly
is the file's business, and `end` plus a gated `release` are the tools for
trimming it.

**Pitch-up aliases, deliberately — unless you tick `antialias`.** Reading
faster than 1.0 shifts the spectrum up and anything near Nyquist folds.
That crunch is the sound of every classic sampler and it is the default
(the [`bitcrusher`](#bitcrusher) precedent — some grit is the point). With
`antialias` on, the read comes from an on-load **mip chain** instead: the
loader keeps half-band-filtered, 2:1-decimated copies of the sample, one
per octave (six of them, or fewer for a short file), and a read above
unity takes the copy whose bandwidth fits the rate — an exact octave reads
one level verbatim, and in between the two nearest levels crossfade by the
fractional octave so a glide never steps between bandwidths (the
[`wavetable_morph`](#wavetable_morph) rule). At the root and below it still
reads the untouched original, so the neutral is bit-exact whichever way the
box is set. Being a mip blend it is honest rather than perfect: at an exact
octave the fold is gone (>40 dB down on the shipped test tone); halfway
between octaves it is attenuated by the lower level's weight (~8 dB at a
fifth), not removed. Pitch *down* is clean either way.

**`start_cv` is what makes a breaks machine.** It moves `start` by
`start_cv_depth` of the file per volt, read **at the gate edge** and
latched into that hit — a sequencer step and the gate that fires it land
together, and the slice point is what the CV said at that sample, not the
block's mean. A [`shift_random`](#shift_random) into it re-slices a loop on
every trigger; a [`sequencer`](#sequencer) programs the slices. The loop
region rides along (it is measured from wherever *this hit* started), and a
start pushed past `end` fires nothing rather than running backwards — and
leaves whatever was sounding alone. See `examples/sampler_scrub.json`.

**`reverse`** plays the same region backwards: the playhead starts one
sample inside `end` and runs down to `start`, a loop wraps the other way
(`loop_start` back up to `loop_end`) and the seam crossfade mirrors —
fading just above `loop_start` into the lap *after*, which is what runs
into `loop_end` from above. At unity rate it is a bit-exact mirror of the
forward read; a reversed loop is a bit-exact tiling of the region backwards.

**`vel`** is a level multiplier sampled at the gate edge (the velocity
idiom: a property of the hit, not a tremolo — what the CV does afterwards
changes nothing). Patch [`midi_input`](#midi_input)'s `velocity_cv` for
real velocity, or any stepped CV for accents. Unpatched it is 1; a negative
value is silence. The 2 ms retrigger tail keeps the *old* hit's gain, so a
quiet note under a loud one doesn't step the tail.

**Stereo.** `out` is the mono voice; `out_l` / `out_r` carry a stereo
file's channels, each read through the same playhead and each bit-exact at
the neutral. A mono file (or a dual-mono one) feeds all three identically
from a single read, so patching only `out` costs what it always did. The
node also wears a **waveform face**: the loaded file's envelope with the
`start`/`end` markers over it and, in `loop` mode, the loop markers placed
inside the region (they are fractions of it), repainted only when
something moves.

Loading runs on a background thread, so a fresh or changed `path` never
blocks audio — the module is simply silent until the sample is ready, and a
missing or unreadable file *stays* silent rather than raising, so a saved
patch always loads whatever became of the audio on disk. Files longer than
120 s are truncated (the limit is RAM, not DSP — the mip chain adds under
half the original again, a stereo file its second channel). Retriggering a
voice that is still sounding crossfades the old playhead out over ~2 ms
rather than cutting to the new one.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `pitch_cv` | in | cv | 1 V/oct, C4 = 0 V. Unpatched → C4 → the root note. Read per block (mean), so glides track at block rate. |
| `gate` | in | gate | Rising edge starts playback from `start`. No cable → silence. |
| `start_cv` | in | cv | Moves `start` by `start_cv_depth` per unit, read at the gate edge and latched per hit. Voice-aware. |
| `vel` | in | cv | Level multiplier, read at the gate edge and latched per hit. Unpatched → 1. Voice-aware. |
| `out` | out | audio | The voice, mono. Voice-aware: `(V, F)` in gives per-voice playheads. |
| `out_l` / `out_r` | out | audio | The voice's stereo channels; identical to `out` for a mono file. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `path` | `""` | file | The sample. WAV needs nothing; mp3 / flac / ogg / m4a and video audio need the `[media]` extra. Empty or unreadable → silence. A **relative** path is looked up next to the patch file first, then in its parent folder, then in the directory you launched from — so a patch plus its media travels as a unit and plays the same however the app was started. A file that fails to load says so in the status bar instead of quietly playing silence. |
| `root` | `60` (C4) | C0…C8 | The note the recording **is**. Playing it reads at rate 1.0. Shown as a note name, stored as a MIDI number. |
| `tune` | `0` | ±12 st | Semitone shift on top. |
| `fine` | `0` | ±50 ct | Cent shift on top. |
| `mode` | `one_shot` | one_shot / gated / loop | What a gate means (above). |
| `start` | `0` | 0…1 | Region start, as a fraction of the file. |
| `end` | `1` | 0…1 | Region end. `end` ≤ `start` plays nothing (rather than running backwards). |
| `loop_start` | `0` | 0…1 | Loop start, as a fraction **of the region** — not of the file. `loop` mode only. |
| `loop_end` | `1` | 0…1 | Loop end, likewise. At or below `loop_start` means "don't loop": the voice plays as `gated`. |
| `loop_xfade` | `10` | 0…100 ms | Seam crossfade, measured on the sample and clamped to the loop length. 0 is a hard seam (and keeps a unity-rate loop bit-exact). |
| `attack` | `0` | 0…500 ms | Declick ramp in. |
| `release` | `10` | 1…2000 ms | Declick ramp out on a `gated` release. |
| `level` | `0.8` | 0…1 | Output level. |
| `start_cv_depth` | `1.0` | −1…1 | Fraction of the file `start` moves per unit of `start_cv` (1.0: 0..1 V sweeps the whole file). |
| `reverse` | `false` | bool | Play the region backwards, from `end` down to `start`. |
| `antialias` | `false` | bool | Read pitch-up from the on-load mip chain instead of letting it fold. Never touches a read at or below unity. |

**Patching.** `euclidean.gate → sampler.gate` for a drum voice;
`cv_keyboard.pitch_cv → sampler.pitch_cv` plus its `gate` for a played
instrument (set `mode` to `gated`). [`chord`](#chord) in front of the pitch
gives a four-deep sample stack for free. Put a [`quantizer`](#quantizer)
before `pitch_cv` and a [`shift_random`](#shift_random) before that and the
sample plays itself in key.

For the mellotron: a [`cv_keyboard`](#cv_keyboard) into a `loop`-mode
sampler into a [`reverb`](#reverb), with `loop_start` past the sample's
attack — see `examples/sampler_mellotron.json`, which holds a chord on
three seconds of pad indefinitely. Percussive samples have nothing to loop
(they are over before you let go of the key); `loop` wants material that
sustains.

For the slicer: a [`clock`](#clock) into an every-step [`euclidean`](#euclidean)
firing the gate, and the same clock into a [`shift_random`](#shift_random)
whose `cv` goes to `start_cv` — every sixteenth lands on a fresh slice of
the bar, and the next hit cuts it off. `examples/sampler_scrub.json` does
that with `antialias` on, plus a sparse reversed stab a fifth up off the
same CV.

#### `pluck`

An **extended Karplus–Strong plucked string**: a delay line one pitch
period long, fed back through a gentle lowpass, excited by a seeded noise
burst on every `trigger` rising edge. `pitch_cv` is 1 V/oct (C4 = 0 V,
the `fm_op` convention), and a polyphonic source gives **one independent
string per voice** — wire a `cv_keyboard` or `midi_input` and it's a
16-string instrument with no extra patching (`pitch_cv` → `pitch_cv`,
`gate` → `trigger`).

Beyond the textbook: the loop uses an **allpass fractional delay** with
the damping filter's phase delay compensated, so pitch lands within a few
cents across the range instead of quantizing to whole-sample loop lengths
(naive KS goes audibly out of tune above ~500 Hz); `decay` is a **real
t60 in seconds**, pitch-independent (textbook KS rings longer the lower
the note); `damping` darkens the string (0 = bright and wiry, 1 = the
classic two-point average, nylon-ish); `color` shapes the exciter from
lowpassed thumb (0) to white-noise plectrum (1); `position` is the
pick-position comb — the burst minus itself delayed `position`·period,
notching the harmonics a pluck at that spot cancels (0 = off).

Re-plucking a ringing string **adds** the new burst into the loop —
physical (the string was still moving) and click-free by construction
(the loop is linear, so plucks superpose). Every burst is seeded per
(voice, hit), so renders are deterministic. Decayed voices early-out to
exact silence for free. Pitch is re-read per block (glides track at
block rate); the pluck pitch itself locks from the trigger sample. Numpy
backend only; silent stub under pyo. See `examples/pluck_strings.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `pitch_cv` | in | cv | 1 V/oct, C4 = 0 V. Unpatched → C4. |
| `trigger` | in | gate | Pluck on each rising edge, per voice. |
| `out` | out | audio | The string(s). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `decay` | `2.0` | 0.1 … 30 s | Ring time (t60), pitch-independent. |
| `damping` | `0.5` | 0 … 1 | Loop lowpass blend; higher = darker, faster HF fade. |
| `color` | `0.7` | 0 … 1 | Exciter spectrum: soft thumb → hard plectrum. |
| `position` | `0.2` | 0 … 1 | Pick-position comb on the burst; 0 disables. |
| `level` | `0.5` | 0 … 1 | Output level. |

#### `bowed`

A **bowed-string physical model** — the sustained-excitation waveguide
that completes the family beside [`pluck`](#pluck) (a string struck once)
and [`modal`](#modal) (a resonator struck once); [`wind`](#wind) is its
blown sibling. The bow stays on the
string for as long as `gate` is high, so the note *sustains*, swells and
speaks: a cello line, a scraped double bass, a viola drone, and past the
sweet spot the squeal and crunch a real bow makes when it is pressed too
hard.

The model is the classic digital waveguide bowed string (Smith 1986, as
in STK's *Bowed*): two delay lines meet at the bow — the bridge side
(`position` of the string) and the nut side (the rest) — and at the
meeting point a **friction table** turns the difference between the bow's
velocity and the string's velocity into the velocity the bow injects back
into both halves. Below a threshold the string sticks to the bow and is
dragged; above it the string slips; the stick-slip cycle at the string's
own period *is* the note (the Helmholtz motion — a sawtooth at the
bridge, which is what a [`scope`](#scope) on `out` with `body` 0 shows).
The bridge end reflects through a one-pole lowpass (`damping`, the
string's losses) and the nut end inverts; the output is the wave arriving
at the bridge, optionally coloured by a small bank of body resonances
(`body`: an air mode, two wood modes and the bridge hill, in parallel).

**The two hands.** `velocity` is how fast the bow moves — mostly loudness
and how quickly the note speaks; its envelope is `attack` / `release`
(the bow lands over `attack` seconds after the gate rises and lifts over
`release` seconds after it falls, after which the string rings down on
its own losses, quickly). `pressure` is how hard the bow is pressed — the
friction table's slope: light is airy and whistly, medium the full tone,
heavy raw and scratchy, and hard pressure at high velocity the crunch.
`pressure_cv` and `velocity_cv` add `cv_depth` × CV to the two hands
**per sample** (mono, shared by every voice): an LFO on `pressure_cv` is
bow-pressure tremolo, a slow one on `velocity_cv` a swell, an envelope a
sforzando.

`position` is where the bow sits along the string as a fraction of its
length: near the bridge (0.05–0.1) is bright and nasal, sul tasto (0.3+)
soft and hollow (0.5, the exact middle, cancels the even harmonics).
`damping` darkens the string. Pitch is 1 V/oct on `pitch_cv` (C4 = 0 V)
read per block — at the block's last gate edge if there is one, else the
block mean — so a [`slew`](#slew) on the CV is portamento and a small LFO
summed in is vibrato; a polyphonic source gives one independent string
per voice, and silent voices cost nothing. A re-bow during the release
picks the envelope up from where it is, so nothing clicks.

Tuning: the loop delay is `sr/f0` minus the bridge filter's exact phase
delay at f0 (the fraction rides the bridge side as a linear-interpolation
read), which lands within ±10 cents from C2 to C6 at the defaults; very
hard pressure or extreme damping bends a real bowed note and this one
too. The loop is advanced in vectorized chunks no longer than the bridge
delay, so cost rises with pitch and with closeness to the bridge —
roughly 5% of a 512-sample block at C4 for one voice, ~15% at C6: a solo
instrument by design. Renders are block-size independent at constant
pitch (integer-count bow ramps). Numpy backend only; silent stub under
pyo. See `examples/bowed_cello.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `pitch_cv` | in | cv | 1 V/oct, C4 = 0 V. Unpatched → C4. |
| `gate` | in | gate | The bow is on the string while high, per voice. |
| `pressure_cv` | in | cv | Adds `cv_depth` × CV to `pressure`, per sample (mono, shared by all voices). |
| `velocity_cv` | in | cv | Adds `cv_depth` × CV to `velocity`, per sample (mono, shared). |
| `out` | out | audio | The string at the bridge, through the body. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `pressure` | `0.5` | 0 … 1 | Bow force: light / full / crunch. |
| `velocity` | `0.6` | 0 … 1 | Bow speed: loudness, and how fast the note speaks. |
| `position` | `0.127` | 0.05 … 0.5 | Bow position as a fraction of the string; near the bridge is bright. |
| `attack` | `0.05` | 0.001 … 10 s | Bow-landing ramp after the gate rises. |
| `release` | `0.15` | 0.001 … 10 s | Bow-lifting ramp after the gate falls. |
| `damping` | `0.5` | 0 … 1 | String losses (darkness). |
| `body` | `0.5` | 0 … 1 | Body-resonance mix (0 = the raw bridge sawtooth). |
| `cv_depth` | `1.0` | 0 … 4 | Level per CV unit on `pressure_cv` / `velocity_cv`. |
| `level` | `0.5` | 0 … 1 | Output level. |

#### `wind`

A **blown-pipe physical model** — the other half of the rack's sustained
physical-modeling pair (with [`bowed`](#bowed)). A column of air in a
pipe is a delay line; what keeps it singing is the player's breath
through a nonlinearity at the mouth, and the note is the pipe's own
resonance, held for as long as `gate` is high. `model` picks the mouth:

- **`flute`** — Cook's slide flute (STK *Flute*): breath pressure minus
  the pipe's reflection drives a **jet** delay line whose output passes
  through the cubic jet table `x(x² − 1)` and back into the bore, with a
  lowpass and a DC blocker in the bore's return. The bore is tuned to one
  and a half periods so the pipe speaks in its *overblown* register —
  STK's own trick ("we're overblowing here"), and why it sounds like a
  flute rather than a whistle. Breathy and hollow; blow hard at the
  bottom of the range and it goes sharp, as a flute does. Home range
  ~C3 upward (below ~80 Hz the jet loses its register and the pitch is
  clamped).
- **`reed`** — the STK *Clarinet* mouthpiece: one round-trip delay line
  with a lowpass loss and the **reed table** `clip(0.7 − 0.3·Δp)`, `Δp`
  the pressure difference across the reed. It needs enough pressure
  before it speaks and too much closes it — both physical, and `breath`
  is mapped so 0..1 runs from just-speaking to nearly closed. Hollow low
  down, reedy and bright blown harder; goes down to the contrabass.

`breath` is the player's pressure; `noise` is breath noise, seeded per
voice and per note so renders are deterministic (`seed`); `attack` /
`release` are the breath ramps on the gate (integer-count, block-size
exact, a re-blow picking up from the current level); `damping` is the
pipe's loss lowpass. `breath_cv` adds `cv_depth` × CV to `breath` **per
sample** (mono, shared by every voice): an envelope for a swell, a slow
LFO for a breathing player, a fast one for flutter. Pitch is 1 V/oct on
`pitch_cv` (C4 = 0 V) read per block — at the block's last gate edge or
the block mean; a polyphonic source gives one independent pipe per voice
and silent voices cost nothing.

Tuning: the reed's bore is `sr/f0` minus the loss filter's exact phase
delay at f0 (±5 cents C2..C6 measured, ±8 pinned); the flute's is 1.5
periods times a measured 1.5% regime correction (±4 cents C3..C6 at
moderate breath, ±10 pinned; hard breath low down bends it sharp). Both
loops run in vectorized chunks no longer than the shortest delay (the
jet for the flute, the whole bore for the reed), so the models are cheap
— under 0.5 ms of an 11.6 ms block per voice across the range. The
output is DC-blocked (the reed's line carries the breath pressure).
Numpy backend only; silent stub under pyo. See `examples/wind_duet.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `pitch_cv` | in | cv | 1 V/oct, C4 = 0 V. Unpatched → C4. |
| `gate` | in | gate | Breath on while high, per voice. |
| `breath_cv` | in | cv | Adds `cv_depth` × CV to `breath`, per sample (mono, shared by all voices). |
| `out` | out | audio | The pipe. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `model` | `flute` | flute / reed | The mouth: air jet or clarinet reed. |
| `breath` | `0.5` | 0 … 1 | Blowing pressure, just-speaking → hard-blown. |
| `noise` | `0.15` | 0 … 1 | Breath noise. |
| `attack` | `0.04` | 0.001 … 10 s | Breath ramp in after the gate rises. |
| `release` | `0.1` | 0.001 … 10 s | Breath ramp out after the gate falls. |
| `damping` | `0.5` | 0 … 1 | Pipe losses (darkness). |
| `cv_depth` | `1.0` | 0 … 4 | Level per CV unit on `breath_cv`. |
| `level` | `0.5` | 0 … 1 | Output level. |
| `seed` | `1` | ≥ 0 | Breath-noise seed. |

#### `modal`

A **struck/blown resonator bank** — physical modelling's other half:
where [`pluck`](#pluck) is a string you pluck, `modal` is a *body* you
strike. Feed anything into `excite` (a short noise burst through a gated
VCA is the classic mallet; a drum click, a full mix, anything) and a
bank of two-pole resonators rings at `pitch × ratio[i]` like the modes
of a real object. The ratio tables are physics-textbook, not product
clones: `bar` (free-free bar, the 1 : 2.76 : 5.40 xylophone series),
`bell` (stylized minor-third bell stack), `membrane` (circular drumhead
— Bessel-function zeros, computed not tabulated), `string` (plain
harmonics).

`modes` sets the resonator count (4..24); `decay` is the lowest mode's
t60 with `decay_tilt` making higher modes die faster (0 glassy, 1
woody); `brightness` tilts the mode gains; `inharm` stretches the ratio
table upward (a little = piano-ish, a lot = clangorous plate). Pitch is
1 V/oct (C4 = 0 V), read per block; modes that would land above
~0.45·sr are dropped, not aliased. Voice-aware: per-voice banks with
per-voice pitches; voices sharing a pitch share coefficients and are
**batched into single vectorized filter calls** (16 unison voices cost
the same as one); rung-out voices early-out. Numpy backend only; silent
stub under pyo. See `examples/modal_bells.json`.

**Where, with what, and in stereo (2026-09-11).** Three knobs, all 0 by
default so the shipped sound is untouched:

* **`position`** — *where* you strike. A body struck at a node of some
  mode doesn't excite that mode; struck near the edge it is bright and
  thin. Each mode's gain is combed by `|sin(π · ratio · position)|` — the
  [`pluck`](#pluck)'s pick-position comb, moved from the exciter to the
  mode gains — then the gains are renormalized so the level holds
  (striking near the edge is *thin*, not quiet). 0 is off. Just off zero
  the comb is ∝ ratio, a bright tilt; 0.5 on a `string` cancels every
  even harmonic (the hollow strike), and the same 0.5 on a `bell` nulls
  its prime and nominal. A position that would null *every* mode (1.0 on
  a harmonic string) falls back to off rather than dividing into silence.
* **`mallet`** — *what* you strike with. 0 is hard: the excite goes in
  raw. Up from there a one-pole low-pass on the strike **tracks the
  pitch** — cutoff `f0 · 2^(6·(1 − mallet))`, six octaves above the
  fundamental down to the fundamental itself at 1 — so a felt mallet
  reads as the same softness across the keyboard, where a fixed-Hz
  filter would leave the top notes duller than the bottom. Per voice,
  state carried (block-size independent at constant pitch).
* **`spread`** — stereo. `out` stays the mono sum; `out_l` / `out_r`
  place each mode in the field by a fixed, evenly-scattered pattern
  (golden-ratio by mode index, so neighbours land on different sides
  without an odd-left / even-right lattice), scaled by `spread`.
  Equal-power pans normalized so at 0 the two outs *are* `out`
  (bit-identical) and a fully-panned mode is √2 louder in its channel.
  Real bells radiate their modes in different directions; this is that,
  stylized. See `examples/modal_mallets.json`.

Cost with all three on: 16 voices × 24 modes at 16 distinct pitches went
from 31.4% to 39.4% of a block's budget.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `excite` | in | audio | The strike/breath. Unpatched → silence. |
| `pitch_cv` | in | cv | 1 V/oct, C4 = 0 V. Unpatched → C4. |
| `out` | out | audio | The ringing body, mono. |
| `out_l` / `out_r` | out | audio | The body with its modes spread; equal to `out` at `spread` 0. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `material` | `bar` | combo | bar · bell · membrane · string. |
| `modes` | `12` | 4 … 24 | Resonator count. |
| `decay` | `2.0` | 0.1 … 30 s | Lowest-mode t60. |
| `decay_tilt` | `0.5` | 0 … 1 | Higher modes die faster. |
| `brightness` | `0.5` | 0 … 1 | Mode-gain tilt, dark → bright. |
| `inharm` | `0.0` | 0 … 1 | Ratio stretch. |
| `level` | `0.5` | 0 … 1 | Output level. |
| `position` | `0.0` | 0 … 1 | Strike-position comb on the mode gains, renormalized. 0 = off. |
| `mallet` | `0.0` | 0 … 1 | Pitch-tracking strike low-pass: 0 hard (raw) … 1 soft (cut at the fundamental). |
| `spread` | `0.0` | 0 … 1 | Stereo mode spread on `out_l`/`out_r`. 0 = the outs equal `out`. |

#### `kick_drum`

The pitch-envelope kick: a sine that **dives** from `freq_start` to
`freq_end` over `bend` ms (exponential — the constant analog circuits
give), amplitude decaying over `decay` ms (a t60). `click` mixes a 2 ms
noise transient into the attack; `drive` pushes the body into a
normalized tanh for saturation grit (plain tanh, no oversampling — the
kick is LF-dominant so foldover is negligible). `tune` shifts everything
±12 st. Every hit is synthesized whole at the trigger edge, seeded per
hit — deterministic, block-size independent, DC-free by construction —
and a retrigger fades the old tail over ~2 ms (no machine-gun clicks).
Mono; fan the trigger out for layers. Numpy only; silent stub under pyo.

**Velocity and pitch (2026-09-11).** `vel` is a level multiplier read
**at the trigger edge** and latched into that hit — the accent idiom, and
the [`sampler`](#sampler)'s. Unpatched it is 1, so nothing changes until
you patch it: a [`midi_input`](#midi_input)'s `velocity_cv` makes the pad
touch-sensitive; a [`shift_random`](#shift_random) programs accents. On
the kick, velocity is applied **before** `drive`, so a soft hit stays
clean and a hard one saturates, the way the circuit would. `pitch_cv` is
the calibrated 1 V/oct bus (no depth knob, like every pitched voice here),
also read at the edge and stacked on `tune` — a [`sequencer`](#sequencer)
into it plays a tuned 808 line. A voice-aware `(V, F)` source on either
jack collapses to the **loudest voice at that sample** (idle slots read
0, so that is the key just struck), not the house sum.

**Ports**: `trigger` (gate), `vel`, `pitch_cv` (cv) → `out` (audio).
**Params**: `freq_start` 100..400 Hz (180) · `freq_end` 30..80 Hz (50) ·
`bend` 5..200 ms (40) · `decay` 50..1500 ms (350) · `click` 0..1 (0.3) ·
`drive` 0..1 (0) · `tune` ±12 st · `level` (0.7). See
`examples/drum_machine.json` and `examples/drum_dynamics.json`.

#### `snare_drum`

Two detuned **head modes** (~185/330 Hz sines, `tone_decay` ms) under a
band-passed **wire-noise** layer (800 Hz–8 kHz, `noise_decay` ms,
seeded per hit). `snappy` balances shell against wires (0 = all tone,
1 = all noise). Same engine as the kick: whole-hit synthesis at the
edge, deterministic, retrigger declick. `tune` ±12 st shifts the modes.

`vel` is a level multiplier read at the trigger edge and latched (unpatched
→ 1; see [`kick_drum`](#kick_drum) for the collapse rule).

**Ports**: `trigger` (gate), `vel` (cv) → `out` (audio). **Params**:
`tone_decay` 20..500 ms (120) · `noise_decay` 20..1000 ms (200) ·
`snappy` 0..1 (0.5) · `tune` ±12 st · `level` (0.7). See
`examples/drum_machine.json`.

#### `hat_drum`

Six detuned **square waves** (the classic metallic ratio stack, base
`tone` Hz) high-passed near 7 kHz — deterministic, no noise source needed;
the stack *is* the noise (the squares alias mildly; hats are noise-like
and it reads as character). One module, two jacks: `closed_trigger`
(tight, `decay_closed`) and `open_trigger` (ringing, `decay_open`)
share the voice, and a closed hit **chokes** a ringing open hit with
the 2 ms fade — the pedal coming down, exactly like hardware.

`tone` (2026-09-11) sets the stack's base frequency, 200..1600 Hz (400 is
the classic). The high-pass is fixed, so what moves is how *dense* the
stack is above it: a low base packs more partials into the band — noisier,
trashier — and a high base leaves it sparse and pitched, the thin hat.
`vel` is a level multiplier read at whichever trigger edge fired and
latched (unpatched → 1; see [`kick_drum`](#kick_drum) for the collapse
rule) — a stepped CV into it is the ghost-note machine.

**Ports**: `closed_trigger`, `open_trigger` (gate), `vel` (cv) → `out`
(audio). **Params**: `decay_closed` 10..300 ms (60) · `decay_open`
50..1500 ms (400) · `tone` 200..1600 Hz (400) · `tune` ±12 st · `level`
(0.6). See `examples/drum_machine.json` and `examples/drum_dynamics.json`.

#### `organ`

A **nine-drawbar additive organ** (tonewheel lineage). Nine sine
partials per voice at the classic footages — 16′, 5⅓′, 8′, 4′, 2⅔′,
2′, 1⅗′, 1⅓′, 1′ (harmonic ratios ½, 1½, 1, 2, 3, 4, 5, 6, 8 of the
played pitch) — each on its own 0..8 fader following the hardware's
**~3 dB per step** law (8 = unity, 0 = silent). The sum is normalised
by `1/√max(1, Σgain²)` (constant RMS: pulling bars changes timbre more
than level, and full registrations keep headroom). The default
registration is **888000000**, the jazz classic. Play it from any
voice-routed pitch/gate pair ([`cv_keyboard`](#cv_keyboard),
[`midi_input`](#midi_input), [`chord`](#chord)) — one organ per voice
slot, polyphonic for free.

An organ has **no envelope** — the gate *is* the articulation. Each
edge rides a ~1 ms linear ramp (reaching exactly 1/0, so a held note
is bit-transparent and a lone full 8′ drawbar is **bit-exact against
the sine [`oscillator`](#oscillator)** — the pinned neutral), and
`click` adds the seeded key-click contact tick on press (softer on
release). The **percussion register** (`perc` 2nd/3rd) is the other
tonewheel signature: a fast-decaying extra harmonic that fires **only
on a key struck from silence** — one shared generator, so legato lines
and chord additions do *not* re-fire it (the strike lands on voice
row 0; monophonic hardware). Partials at/above Nyquist are masked,
never aliased. Pitch reads per block (vibrato tracks at block rate);
phases, ramps, click tails and the percussion strike all carry across
blocks — bit-exact at any block size.

**Ports**: `pitch_cv` (cv, 1 V/oct, C4 = 0 V; unpatched → C4), `gate`
(gate; unpatched → silence) → `out` (audio). **Params**: `bar1`..`bar9`
0..8 (888000000) · `click` 0..1 (0.3) · `perc` off/2nd/3rd (off) ·
`perc_decay` fast ≈0.3 s / slow ≈1 s · `perc_level` 0..1 (0.7) ·
`level` 0..1 (0.5). The node draws the bars as a vertical fader bank
(faders-up = louder — a deliberate deviation from pulled-out-is-louder
hardware). Pair it with [`chorus`](#chorus) for the scanner shimmer —
see `examples/organ_jazz.json`.

#### `supersaw`

**Seven detuned band-limited saws per voice** — the trance chord
machine (JP-8000 lineage). One center saw, six spread on the classic
**asymmetric** detune table (the sides don't mirror — half the
shimmer), outermost pair ±50 cents at `detune` = 1. Free-running
initial phases seeded per (voice slot, saw) are the other half of the
signature — and being seeded they're deterministic: patches recall.
`blend` is the JP control (0 = center saw alone — and therefore
detune-inert, pinned); the seven gains are RMS-normalised so
blend/detune don't pump level. `spread` pans the stack across the
field equal-power (flat-side saws left, sharp right); at 0 the outs
are **bit-identical** (patch either for mono). Voice-aware via
`freq_cv` (1 V/oct around `freq`, per-sample): [`chord`](#chord) →
supersaw is the obvious wall of sound. Heavy by design — 16 voices =
112 PolyBLEP saws (~46 % of the 48 k/512 budget; recorded) — spend it
on the pad, not the whole rack. **Ports**: `freq_cv`, `amp_cv` (cv) →
`out_l`, `out_r` (audio). **Params**: `freq` (261.6256) · `detune`
0..1 (0.35) · `blend` 0..1 (0.75) · `spread` 0..1 (0.5) · `amp`
(0.5). See `examples/supersaw_chord_wall.json`.

#### `wavetable_morph`

A **scanning wavetable oscillator** — the `*_wt` mipmap infrastructure
grown into an instrument. A stack of single-cycle frames;
`position` scans it, crossfading adjacent frames, and every frame is
band-limited per octave exactly like the oscillator's `*_wt` shapes,
so the morph stays alias-free up the keyboard (> 40 dB suppression,
pinned). Built-in stacks: `analog` (sine → triangle → saw → square —
position 0/1 land the pure endpoint shapes, the thirds land triangle
and saw exactly, all pinned), `vowel` (five generic formant frames,
A→E→I→O→U — sweep `position_cv` from an LFO and it talks), `metallic`
(sparse seeded-phase harmonic sets, glassy → gnarly). `file` imports a
**single-cycle WAV** via Browse (spectrally resampled, mip-banded like
the built-ins; loads as a single frame, so `position` is inert until a
multi-frame import exists; a bad path falls back to `table` silently).
Voice-aware via `freq_cv`; `position_cv` adds to the knob ×
`position_cv_depth` at block rate per voice. **Ports**: `freq_cv`,
`position_cv`, `amp_cv` (cv) → `out` (audio). **Params**: `freq`
(261.6256) · `position` 0..1 (0) · `position_cv_depth` (1) · `table`
analog/vowel/metallic · `file` ('' = built-in) · `amp` (0.5). See
`examples/wavetable_vowel_talk.json`.

---

### Processors

Modules that take audio in and shape it.

#### `filter`

A resonant biquad filter (Robert Bristow-Johnson coefficients) — lowpass,
highpass, or bandpass, with CV-modulatable cutoff *and* resonance.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to filter. |
| `cutoff_cv` | in | cv | Sweeps the cutoff, `cv_depth` octaves per CV unit (default 1.0 = 1 V/oct: `cutoff · 2^(cv_depth·cv)`). Patch an envelope or LFO here for sweeps. |
| `resonance_cv` | in | cv | Sweeps the Q, `res_cv_depth` doublings per CV unit (`resonance · 2^(res_cv_depth·cv)`, block mean, clipped to the 0.1…20 legal range). +1 doubles the peak's Q, −1 halves it; an envelope here makes the peak swell and relax with the note. Voice-aware like `cutoff_cv`: a `(V, F)` source gives every voice its own Q. |
| `out` | out | audio | Filtered signal. |

**Parameters**

| Param | Default | Options / range | Description |
|-------|---------|-----------------|-------------|
| `mode` | `lowpass` | `lowpass`, `highpass`, `bandpass` | Filter response. |
| `cutoff` | `1000.0` | ~20…20000 Hz | Corner/center frequency when `cutoff_cv` is unpatched. |
| `resonance` | `0.707` | ~0.1…15 | Q. `0.707` is flat (no peak); higher emphasises the cutoff and can self-oscillate-ish. |
| `cv_depth` | `1.0` | 0…4 oct/unit | Octaves the cutoff moves per `cutoff_cv` unit. Default 1 V/oct (pre-2026-07-02 fixed behaviour); 0 disables. |
| `res_cv_depth` | `1.0` | 0…4 dbl/unit | Q doublings per `resonance_cv` unit (the [`motion_eq`](#motion_eq) `q_cv_depth` convention). Default 1 = cv +1 doubles the Q; 0 disables without unpatching. Unpatched, the Q is exactly `resonance`. |

**Patching.** Classic: `oscillator → filter → vca`, with an `adsr → cutoff_cv`
for a filter sweep. See `examples/filter_envelope.json`, `examples/wah.json`.
Put a second, slower modulator on `resonance_cv` and the peak *breathes* —
sharp and whistling at the top of the cycle, soft and round at the bottom —
independently of where the cutoff is; `examples/filter_resonance_sweep.json`
does exactly that with two LFOs. A runaway CV can't blow the filter up: the
Q pins at 20 (or 0.1) and stays there.

#### `crossover`

Splits one audio input into **low** and **high** bands at a chosen frequency
— a 4th-order Linkwitz-Riley split whose bands sum back flat.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to split. |
| `freq_cv` | in | cv | Sweeps the corner 1 V/oct × `cv_depth`; optional. |
| `low` | out | audio | Everything below the (possibly CV-swept) corner. |
| `high` | out | audio | Everything above it. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `freq` | `1000.0` | ~20 … 0.45·sample-rate Hz | Crossover corner. |
| `cv_depth` | `1.0` | octaves / CV unit | How far `freq_cv` sweeps the corner (1 V/oct). Ignored when `freq_cv` is unpatched. |

**Patching.** Feed `low`/`high` into separate chains, or back into a
[Combiner](#combiner) to reconstruct the input. Pairs beautifully with
[AudioToCV](#audio_to_cv) to turn each band into a modulation source — see
`examples/two_way_crossover.json`, `examples/file_crossover_split.json`,
`examples/mic_beatbox_crossover.json`. Patch an LFO/envelope into
`freq_cv` to sweep the split point (1 V/oct × `cv_depth`, block-mean like
the [Filter](#filter)'s `cutoff_cv`) for dynamic band-splitting — see
`examples/crossover_sweep.json`.

#### `parametric_eq`

A 4-band **parametric EQ** — four independent peaking ("bell") bands on
one mono signal. Each band has its own centre frequency, gain, and Q, so
the same module is a bass-shaping low EQ (the 25/50/100/250 Hz defaults)
or a full-range four-point tone control.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to equalise. |
| `out` | out | audio | Equalised signal. |

**Parameters** (per band `i` in 1–4)

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `band{i}_freq` | 25 / 50 / 100 / 250 Hz | ~20 … 0.45·sample-rate Hz | Band centre frequency. |
| `band{i}_gain` | `0.0` | −24 … +24 dB | Boost (+) / cut (−). `0` dB is exactly transparent. |
| `band{i}_q` | `1.0` | ~0.1 … 20 | Bell width — low Q broad, high Q narrow. |

**How it works.** Each band is an RBJ peaking biquad; the four run in
series. A band left at 0 dB has identity coefficients, so unused bands
are tonally free. Coefficients are param-only (no CV yet) and the path
is shape-polymorphic like [Filter](#filter) / [Crossover](#crossover):
a mono input runs one cascade, a voice-aware `(V, F)` input runs V
independent cascades.

**Patching.** Drop it anywhere in an audio chain: `oscillator →
parametric_eq → vca → speaker`, or sculpt a drum/sub bus. See
`examples/parametric_eq_bass.json` (saw → low-end boost + low-mid cut +
a presence band → speaker).

#### `sweep_eq`

A single **CV-swept resonant band** — the focused auto-wah / envelope-filter
node. Where [parametric_eq](#parametric_eq) gives four *static* bells,
`sweep_eq` is one band tuned to *move*: patch an LFO, an envelope (via
[AudioToCV](#audio_to_cv)), a [Sequencer](#sequencer) or a keyboard into
`freq_cv` and the centre frequency sweeps 1 V/oct — the classic wah "wow".

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to filter. |
| `freq_cv` | in | cv | Sweeps the centre frequency 1 V/oct × `cv_depth`; optional. |
| `out` | out | audio | Processed (mixed) signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `mode` | `bandpass` | bandpass / lowpass / peak | Voicing. `bandpass` = classic wah; `lowpass` = resonant corner sweep; `peak` = a swept EQ *bell* that lifts the moving band but passes the rest (the one voicing the plain [Filter](#filter) can't do). |
| `freq` | `800.0` | ~20 … 0.45·sample-rate Hz | Centre/corner frequency. |
| `gain` | `12.0` | −24 … +24 dB | Peak boost/cut — **`peak` mode only**, ignored by the filters. |
| `q` | `4.0` | 0.1 … 20 | Resonance / band width. High = a biting wah. |
| `cv_depth` | `1.0` | octaves / CV unit | How far `freq_cv` sweeps the centre (1 V/oct). |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 1.0 = fully wet (the effect); 0.0 = bit-exact bypass. |

**Patching.** The drop-in auto-wah: `oscillator → sweep_eq → speaker` with an
LFO or an [AudioToCV](#audio_to_cv) envelope into `freq_cv`. A resonant
`bandpass`/`lowpass` boosts at the peak (peak gain ≈ `q`), so back off the
source level. DSP reuses the same RBJ biquads as
[parametric_eq](#parametric_eq) (peak) and [filter](#filter) (bandpass/lowpass);
shape-polymorphic and block-size independent like both. See
`examples/sweep_eq_autowah.json` (a 110 Hz saw wah-swept by a 1.2 Hz LFO).

#### `motion_eq`

A **4-band parametric EQ whose band centres you sweep with CV** — the full
"animated EQ". Four peaking bells like [parametric_eq](#parametric_eq), but
each band has its own CV input (`band1_freq_cv` … `band4_freq_cv`) that slides
*that band's* centre frequency, and a second (`band1_gain_cv` …
`band4_gain_cv`) that pushes *that band's gain* in dB, and a third
(`band1_q_cv` … `band4_q_cv`) that squeezes *that band's width*. Patch four
LFOs/envelopes in and four peaks/notches glide independently around the
spectrum — breathe in and out, snap into resonant focus and relax.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to EQ. |
| `band1_freq_cv` … `band4_freq_cv` | in | cv | Each sweeps its band's centre 1 V/oct × `cv_depth`; optional per band. |
| `band1_gain_cv` … `band4_gain_cv` | in | cv | Each adds `gain_cv_depth` dB per CV unit to its band's gain (clamped ±24 dB); optional per band. |
| `band1_q_cv` … `band4_q_cv` | in | cv | Each scales its band's Q by `2^(q_cv_depth·cv)` (doublings; the cascade clips 0.1…20); optional per band. |
| `out` | out | audio | Equalised signal. |

**Parameters** (per band `i` in 1..4, plus shared `cv_depth` / `gain_cv_depth` / `q_cv_depth`)

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `band{i}_freq` | 120 / 500 / 1800 / 6000 Hz | ~20 … 0.45·sr Hz | Band centre — the static value and the base the CV sweeps around. |
| `band{i}_gain` | `0.0` | −24 … +24 dB | Boost/cut (0 = transparent; negative = a notch). |
| `band{i}_q` | `1.0` | 0.1 … 20 | Band width. |
| `cv_depth` | `1.0` | octaves / CV unit | **Shared** — octaves each `band{i}_freq_cv` sweeps its band (1 V/oct). Per-band sensitivity is reachable with a [CVScale](#cv_scale) on any input. |
| `gain_cv_depth` | `6.0` | 0 … 18 dB / CV unit | **Shared** — dB each `band{i}_gain_cv` adds to its band's gain (additive, block-meaned, clamped ±24 dB), the [tilt_eq](#tilt_eq) convention. 0 disables the gain CVs. |
| `q_cv_depth` | `1.0` | 0 … 4 doublings / CV unit | **Shared** — Q doublings each `band{i}_q_cv` applies to its band (multiplicative like the freq sweep — Q is ratio-like, so the natural unit is a doubling). 0 disables the Q CVs. |

**Patching.** All three band dimensions are now animated. With
nothing patched, `motion_eq` is bit-identical to a [parametric_eq](#parametric_eq)
of the same params (an unpatched band stays at its static centre). Reuses
ParametricEQ's exact peaking cascade, so a 0 dB band is exactly transparent and
shape-polymorphic/block-size behaviour matches. See
`examples/motion_eq_animated.json` (two boosted bands swept through white noise
by a pair of slow LFOs) and `examples/motion_eq_breathe.json` (two bands
*breathing* via `gain_cv` while the reverb behind them darkens on
`damping_cv`).

#### `tilt_eq`

A **CV-controlled spectral tilt** — a bass↔treble seesaw about a pivot
frequency, the third (and simplest) of the animated-EQ trio. Positive tilt
boosts the lows and cuts the highs by the same amount (warmer/darker);
negative tilt is the mirror (brighter/thinner). Patch an LFO into `tilt_cv`
and the sound breathes dark↔bright; an envelope (via
[audio_to_cv](#audio_to_cv)) opens the top end with dynamics — one-knob
voltage-controlled brightness.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to tilt. |
| `tilt_cv` | in | cv | Added to `tilt`, scaled by `cv_depth` (dB per unit); optional. |
| `out` | out | audio | Tilted signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `pivot` | `1000.0` | ~20 … 0.45·sr Hz | Frequency the balance seesaws about — the response stays ~0 dB there. |
| `tilt` | `0.0` | −12 … +12 dB (UI) | Static base tilt. What the lows gain and the highs lose (total low↔high spread is twice this). 0 = bit-exact passthrough. |
| `cv_depth` | `6.0` | 0 … 18 dB/unit | dB of tilt per `tilt_cv` unit — a bipolar LFO at full depth seesaws ±6 dB by default. |

**Patching.** Two opposed RBJ shelves cornered at the *same* pivot — the
[loudness](#loudness) module's shelf pair with mirrored gains, run by the same
cascade renderer, so shape-polymorphism and the bit-exact identity at 0 dB are
literally the same code. Effective tilt = `tilt + cv_depth × mean(tilt_cv)`
dB, block-meaned (macro control, all voices share the curve), clamped ±18 dB.
Where the trio sits: [sweep_eq](#sweep_eq) moves one resonant band,
[motion_eq](#motion_eq) sweeps four bells, `tilt_eq` seesaws the whole
spectrum. See `examples/tilt_eq_seesaw.json` (a saw drone breathing
dark↔bright under a slow LFO).

#### `vca`

_To document._ Voltage-controlled amplifier: multiplies `audio` by `cv`
(makes an ADSR audible). **Note the port names: `audio` and `cv`, not `in`.**
Param: `gain`. See `examples/keyboard_adsr.json`.

#### `resampler`

A **varispeed pitch shifter** — it transposes audio by *resampling*,
reading the signal back at a different rate. Like a turntable or tape
machine, pitch and speed move together: pitch up and it plays faster,
pitch down and it slows. It's the cheapest, cleanest way to shift
pitch (no FFT, no phase vocoder) and is ideal for sample transposition
and lo-fi tape effects.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to transpose. Unpatched → silence. |
| `pitch_cv` | in | cv | Added to the transpose, scaled by `cv_depth` (summed in semitone space). |
| `brake` | in | gate | High engages the tape-stop brake (ORed with the `brake` param switch). Unpatched → released. |
| `out` | out | audio | The resampled signal (centre pitch). |
| `out_l` | out | audio | Left of the stereo detune pair (centre − `spread`/2 cents). Equals `out` when `spread` = 0. |
| `out_r` | out | audio | Right of the pair (centre + `spread`/2 cents). Equals `out` when `spread` = 0. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `semitones` | `0.0` | −24 … +24 st | Coarse transpose (C→D = +2). 0 = unity. |
| `cents` | `0.0` | −100 … +100 ct | Fine-tune, added to `semitones`. |
| `cv_depth` | `12.0` | 0 … 48 st/unit | Semitones per unit of `pitch_cv` (12 = one octave per unit, 1V/oct-style). |
| `glide` | `0.0` | 0 … 5 s | Portamento time for pitch changes (0 = instant). |
| `mix` | `1.0` | 0 … 1 | Dry/wet blend. The dry tap is latency-compensated to line up with unity-pitch wet, so the blend is coherent (no slapback). |
| `window` | `200.0` | 20 … 2000 ms | Looping-buffer window. Latency is half of it, so shorter = tighter live latency but a stronger loop texture; longer = subtler texture on big shifts. Floored at 4 audio blocks; live changes keep the recent audio (no dropout). |
| `antialias` | `False` | off / on | Off = raw, aliased **lo-fi** up-shift (the default character). On = band-limit the input before the read so **pitching up** doesn't fold content past Nyquist into aliasing (a cleaner up-shift). Pitch-down and unity never fold, so they're unaffected; the dry side of `mix` stays full-band. |
| `spread` | `0.0` | 0 … 50 ct | Stereo detune width. 0 = mono (all three outs identical). Above 0, `out_l`/`out_r` read `spread`/2 cents flat/sharp of the centre off their own drifting heads → patch them to L/R speakers for one-module stereo width. `out` stays the centre pitch. ~10–25 ct is a natural spread. |
| `brake` | `False` | off / on | The tape-stop switch. On (or with the `brake` gate high — they OR) playback decelerates to a **dead stop**; off it spins back up. |
| `brake_time` | `0.5` | 0 … 5 s | Seconds from full speed to stopped when the brake engages. 0 = instant. |
| `spinup_time` | `0.25` | 0 … 5 s | Seconds from stopped back to full speed on release. 0 = instant. |

**How it works.** Pitch is summed in semitone space
(`st = semitones + cents/100 + cv_depth · pitch_cv`), optionally glided
with a one-pole, then exponentiated to a playback ratio
`2^(st/12)`. The read head advances by that ratio per output sample,
reading the buffer with **4-tap cubic Hermite (Catmull-Rom)**
interpolation — a flat-passband read that keeps non-integer
transposition and detune clean (its win is biggest on bright,
high-frequency material, where 2-tap linear droops and images badly),
while an integer read position (unity ratio, octave shifts) still
returns the sample exactly, so unity stays a bit-exact passthrough.
Because a resampler reading at a different
rate than it's fed can't stay in sync with a continuous stream
forever, it runs a short **looping buffer** of recent audio: the read
head wraps inside the window, so the module keeps sounding indefinitely
on any live source (oscillator, mic, file player), at the cost of a
faint granular-repeat texture on extreme shifts. Each wrap is
**declicked**: when the head drifts into a guard band near either
buffer edge it jumps half a span back toward the centre under a short
equal-power crossfade, instead of splicing audio a window apart with a
click. At unity ratio the head never drifts, no jump ever fires, and
the output stays a bit-exact delayed passthrough. That buffer also means
latency — half the `window` param (~100 ms at the 200 ms default) — the
unavoidable price of varispeed on a live signal, and what lets you glide
and modulate the pitch freely. `window` is the trade-off knob: shorten
it toward 20 ms for tight live-input latency at the cost of a stronger,
more granular loop texture; stretch it toward 2 s to make the texture
subtler on big shifts. Changing it live rebuilds the ring around the
most recent audio, so a slider drag doesn't punch a hole in the sound. The path
is shape-polymorphic like [Filter](#filter) / [Crossover](#crossover):
a mono input runs one buffer, a voice-aware `(V, F)` input runs V
independent buffers with per-voice read heads (a single voice row is
bit-identical to the mono render).

**Anti-alias (`antialias`, off by default).** Pitching *up* reads the
buffer faster than it's written, which shifts the source's high content
above Nyquist, where it folds back down as aliasing — real tape never
does this because it's inherently band-limited. With the toggle on, the
input is low-passed at `Fs/(2·ratio)` (a Butterworth, into a second ring
the up-shift wet read samples), so nothing folds and up-shifts stay
clean — e.g. a band-limited saw up an octave drops from ≈ −13 dB alias
to ≈ −25 dB. It's a deliberate switch, not a default: off keeps the raw,
aliased lo-fi grit that suits the sci-fi/tape character. Pitching down
and unity can't fold, so they read the raw ring untouched (and stay
bit-exact); the dry side of `mix` always stays full-band.

**Stereo detune spread (`spread`, off by default).** Above 0 the module
grows a detuned pair alongside the centre `out`: `out_l` reads `spread`/2
cents flat, `out_r` the same sharp, each off its **own** read head that
drifts and loop-seams independently — so the two channels decorrelate
(L/R correlation near 0) into a wide, chorus-like image from a single
mono input. Patch `out_l`/`out_r` into the
[left](#left_speaker_output)/[right](#right_speaker_output) speakers.
`out` stays the centre pitch regardless, and at `spread` 0 all three
outputs are identical — the module is a drop-in mono processor (a single
centre read, no extra cost) until you dial width in.

**Tape-stop brake (`brake`, `brake_time`, `spinup_time`).** The
first-class stop/start gesture: engage the brake (the param switch or a
high `brake` gate — either, they OR together) and playback decelerates
to a dead stop over `brake_time` seconds; release it and the transport
spins back up over `spinup_time`. The ramp is linear in *speed* —
constant-torque physics, the way a real platter or capstan winds down —
applied to the playback ratio itself, which is why it exists as a
feature rather than a `glide` trick: glide ramps in semitone space,
where a full stop is −∞ semitones, unreachable. The brake scales the
ratio to an actual zero — the pitch dives through the floor, the audio
freezes (silence through any AC path), and on release it whooshes back
up to the set pitch. The gesture is module-wide (all voices and spread
channels brake together — one transport), and while stopped the `mix`
dry tap keeps playing, so at 50% mix the wet layer brakes over a dry
bed; leave `mix` at 1 for the full stop-to-silence. Wire a
[clock](#clock)/[sequencer](#sequencer)/[keyboard](#keyboard) gate into
`brake` for rhythmic stutter-stops, or flip the switch by hand for the
DJ power-down. With the brake released and fully recovered the feature
costs nothing and every render is bit-for-bit what it always was.

For pitch shifting that keeps the *speed* fixed you'd want a granular
or phase-vocoder engine — a heavier build for later. This one is
deliberately the tape kind.

**Patching.** `oscillator → resampler → speaker` to transpose a tone,
or feed the [FilePlayer](#file_player) in to pitch a sample. Wire an
[LFO](#lfo) into `pitch_cv` for vibrato/tape-wobble, or an
[ADSR](#adsr)/[AD](#ad_envelope) for pitch dives; raise `glide` for
portamento sweeps, or use the `brake` for a true tape-stop. Set `mix`
to ~0.5 with a few `cents` of detune for one-module chorus-style
thickening. For instant stereo width, raise `spread` to ~15 ct and
patch `out_l`/`out_r` into the
[left](#left_speaker_output)/[right](#right_speaker_output) speakers. See
`examples/resampler_tape_wobble.json` (saw → varispeed with a slow LFO
wobbling the pitch → speaker), `examples/resampler_detune_blend.json`
(+12 ct at 50% mix → detune thickening) and
`examples/resampler_tape_stop.json` (a slow clock gating the brake →
a tape stop and spin-up every four seconds).

#### `pitch_shifter`

A **time-preserving pitch shifter** — the speed-preserving cousin of
the [resampler](#resampler). Where the resampler is varispeed (pitch
and speed move together, like tape), this shifts pitch while the speed
and duration stay put: transpose a held note, a loop, or live playing
without it getting faster or slower.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to transpose. Unpatched → silence. |
| `pitch_cv` | in | cv | Added to the transpose, scaled by `cv_depth` (summed in semitone space, sampled per block). Moves the `harmony` voice too, so a chord transposes as one. |
| `out` | out | audio | The pitch-shifted signal — main + harmony, mono sum. |
| `out_l` / `out_r` | out | audio | (2026-09-14) The stereo pair: with a harmony present, `spread` leans the main shift left and the harmony right; the dry stays centred. Identical to `out` when there is no harmony or `spread` is 0, so a patch wired stereo plays mono rather than falling silent. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `semitones` | `0.0` | −24 … +24 st | Coarse transpose (C→D = +2). 0 = unity. |
| `cents` | `0.0` | −100 … +100 ct | Fine-tune, added to `semitones`. |
| `cv_depth` | `12.0` | 0 … 48 st/unit | Semitones per unit of `pitch_cv` (12 = one octave per unit). |
| `mix` | `1.0` | 0 … 1 | Dry/wet: 0 = original, 1 = fully shifted. |
| `grain_size` | `50.0` | 10 … 200 ms | Grain length — longer = smoother on sustained/low material, shorter = sharper transients. |
| `overlap` | `2` | 2 … 4 | Number of overlapping grains — higher = smoother/denser at more CPU. |
| `formant_preserve` | `False` | bool | Keep the spectral envelope (timbre) in place while pitch moves — LPC whiten → shift residual → re-color. Off = classic chipmunk/giant coloration. |
| `feedback` | `0.0` | 0 … 0.9 | (2026-09-14) **Shimmer.** Recirculates the shifted output into the input, so every lap is shifted *again* — at +12, an octave cascade that blooms away from the note. Short loop (one block plus the engine's latency, a grain or two): the fast, spectral kind; for the slow cathedral kind put the shifter in a [matrix_mixer](#matrix_mixer) loop with a [delay](#delay) (`organ_shimmer.json`). Damped above ~6 kHz so the climb dies at the top of hearing instead of aliasing, soft-ceilinged at the matrix knee, and the dry side of `mix` always hears the *original*. |
| `harmony` | `0.0` | −24 … +24 st | (2026-09-14) **Harmonizer.** Semitones for a second shifted voice, on top of `semitones`. `semitones` +4 and `harmony` +7 over the dry is a major triad from one module; +7 / +12 a power chord; −12 / +12 an octave stack. Hears the raw input, not the feedback loop. |
| `harmony_level` | `0.0` | 0 … 1 | Level of the harmony voice. 0 = off, and no second engine is built until it rises (it costs one more grain engine per voice — about +1.8 points of a block per mono voice). |
| `spread` | `0.0` | 0 … 1 | With a harmony present, pans the main shift toward `out_l` and the harmony toward `out_r` — the far channel fades by `spread`, the dry stays centred. Nothing to pan without a harmony. |

**How it works.** It uses **WSOLA** (waveform-similarity overlap-add):
the audio is sliced into short overlapping grains and overlapped back
together at the original rate (preserving duration), with each grain
resampled to move the pitch. The “waveform-similarity” part nudges each
grain to the position where it best lines up with the previous one, so
the overlap joins stay phase-continuous — that's what keeps it clean on
tonal material instead of the beating/doubling a naïve granular shifter
produces on a held tone. Pitch is summed in semitone space and the
engine adds about one grain of latency. Shape-polymorphic like
[Filter](#filter): a mono input runs one grain engine, a voice-aware
`(V, F)` input runs one independent engine per voice (a single voice row
is bit-identical to the mono render). Pushed to extremes, or on very low
material with short grains, it takes on a characteristic granular smear.

**Patching.** `oscillator → pitch_shifter → speaker` to transpose a
tone without changing tempo; feed the [FilePlayer](#file_player) in to
re-pitch a loop while it keeps time. Wire an [LFO](#lfo) into `pitch_cv`
for vibrato. Set `mix` to ~0.5 with `semitones` = 7 for a fifth stacked
over the dry (instant harmony), or a couple of cents for detune-
thickening. For pitch shifting where speed *should* follow, use the
[resampler](#resampler). See `examples/pitch_shifter_harmony.json`
(saw → +7 st at 50% mix → speaker: a self-playing fifth). Put one at
**+12 inside a feedback loop** and you get shimmer — every lap returns
an octave higher, stacking into a cloud that climbs away from the note
that started it; see `examples/organ_shimmer.json`, where a dark delay
in the same loop is what the climb finally dies against. Since
2026-09-14 the loop is also built in: `feedback` 0.75 at +12 on a pluck
is `examples/pitch_shifter_shimmer.json` — the fast bloom, each pluck
into a cloud. For a **stereo triad from one module** see
`examples/pitch_shifter_harmonizer.json`: a saw at E3, `semitones` +4,
`harmony` +7 at `harmony_level` 0.9, `spread` 1 — root centred, third
hard left, fifth hard right, `out_l`/`out_r` into the
[left](#left_speaker_output)/[right](#right_speaker_output) speakers.

**Accuracy & deep bass (2026-07-02).** The analysis clock runs on the
ideal WSOLA grid (search excursions never accumulate into the input
timeline — the earlier revision's accumulation could starve or
deadlock the engine at some grain/ratio combos and pulled the pitch a
few cents), and NCC peaks are refined parabolically with fractional
grain extraction, so joins are phase-continuous to sub-sample
accuracy: shifts land **sub-cent** on pure tones across the range. A
built-in period detector grows the working grain whenever the
configured one holds fewer than ~2.5 cycles (low E and below at the
default 50 ms) — `grain_size` is the floor, growth is capped at 150 ms
and hysteresis prevents thrash; the swap is primed from history and
equal-power crossfaded in, observable via the per-voice `regrains`
counter. With `formant_preserve` on, an order-24 LPC envelope
(Levinson-Durbin, time-domain) whitens the input, the residual is
shifted, and the envelope from ~one grain earlier re-colors the
output — a shifted voice keeps its vowel instead of chipmunking. The
dry `mix` tap always reads the raw input, and a level safety valve
bounds any ill-conditioned envelope estimate at 4x the input RMS. See
`examples/pitch_shifter_formant_vowel.json` (square → two resonant
bells as a synthetic vowel → +5 st with formants held).

**Phase-coherent mix (2026-07-10).** The dry tap is delay-matched to the
wet path's *exact* latency — computed per block from the engine's own
pointers (`iw − rp/r`, verified to the sample against a wet-vs-input
cross-correlation) rather than an approximate one-grain guess that under-
compensated by ~50 ms at the defaults. A partial `mix` — a stacked
harmony, or a few-cents detune-thicken — now blends two time-aligned
signals instead of comb-filtering; at unison the dry and wet are the
same delayed signal.

#### `delay`

An **analog-voiced feedback delay** (echo). It feeds the signal into a
delay line and mixes the delayed copy back in; part of that copy
recirculates, so the echo repeats and fades. A damping low-pass in the
feedback path rolls a little more high end off on every pass, so the
repeats darken the way a tape or bucket-brigade (BBD) echo does, instead
of staying digitally bright.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to echo. Unpatched → silence. |
| `time_cv` | in | cv | Added to `time`, scaled by `cv_depth`. Modulate for wow / dub throws. |
| `out` | out | audio | Dry + echo mix. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `time` | `300.0` | 1 … 2000 ms | Echo spacing. Slapback ~80 ms, roomy dub ~500 ms. |
| `feedback` | `0.4` | 0 … 0.98 | How many repeats; clamped just below runaway. |
| `tone` | `0.5` | 0 … 1 | Feedback damping: low = dark repeats, high = bright/faithful. |
| `mix` | `0.35` | 0 … 1 | Dry / wet balance. |
| `cv_depth` | `50.0` | 0 … 2000 ms/unit | Milliseconds of delay time per unit of `time_cv`. |

Shape-polymorphic like [Filter](#filter) / [Crossover](#crossover): a
mono input runs one delay line; a voice-aware `(V, F)` input runs one
line per voice slot (a single voice row is bit-identical to mono). Any
musical delay time is many blocks long, so the common case runs on a
fully vectorized block path; only short or heavily modulated delays
(under one block) fall back to a per-sample loop.

**Patching.** `… → vca → delay → speaker` puts an echo on a voice; raise
`feedback` and lower `tone` for a dub tail that melts away; wobble
`time_cv` from an [LFO](#lfo) for tape flutter, or set a short `time`
with a little modulation for chorus / vibrato shading. See
`examples/delay_dub_echo.json` (a self-playing sequencer melody through a
dotted-eighth dub echo).

#### `reverb`

A **stereo Feedback Delay Network** reverb — it turns a mono sound into a
sense of space. The input is diffused and fed through eight cross-coupled
delay lines that bloom into a dense, decaying wash; two decorrelated taps
give a **stereo** tail (`out_l` / `out_r`) from the mono input, which is
what the ear reads as width.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to reverberate (voice sources summed to mono). Unpatched → silence. |
| `decay_cv` | in | cv | Added to `decay` (× `cv_depth`) — animate the tail length. Optional. |
| `damping_cv` | in | cv | Added to `damping` (× `cv_depth`) — darken/brighten the tail over a phrase. Optional. |
| `mix_cv` | in | cv | Added to `mix` (× `cv_depth`) — envelope-driven reverb throws / wet ducking. Optional. |
| `freeze` | in | gate | (2026-09-19) High (> 0.5) **holds the tail as a pad**: unity loop, damping bypassed, input muted from the tank (the dry path still passes). A `(V, F)` gate collapses to any-voice-high. Unpatched → never frozen, bit-exact with the pre-freeze render. |
| `out_l` | out | audio | Left channel (dry + decorrelated wet). |
| `out_r` | out | audio | Right channel (dry + decorrelated wet). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `size` | `0.5` | 0 … 1 | Delay-line lengths: small room → large hall. |
| `decay` | `0.5` | 0 … 1 | Tail length (reverberation time), short → long. |
| `damping` | `0.5` | 0 … 1 | High-frequency absorption in the tail, bright → dark. |
| `mix` | `0.3` | 0 … 1 | Dry/wet balance. Dry is centred; wet is the stereo tail. |
| `cv_depth` | `1.0` | 0 … 2 lvl/unit | Level units per CV unit, shared by `decay_cv`, `damping_cv` and `mix_cv`; 0 disables all three. `size` deliberately has no CV — sweeping delay-line lengths clicks. |

Patch the outputs into [`left_speaker_output`](#left_speaker_output) and
[`right_speaker_output`](#right_speaker_output) for a wide tail. The
network is **block-size independent** (processed in hops no longer than
its shortest delay line, so the sound never depends on the audio block
size). See `examples/reverb_space.json` (a self-playing triangle melody
in a big hall, spread across both speakers).

**Patching.** `… → vca → reverb → L/R speakers`. Long + dark (`decay` up,
`damping` up) for an ambient wash behind a sparse line; short + bright for
a subtle glue. Pure sustained tones can still ring a touch — v1 has no
tail modulation yet.

**Freeze** (2026-09-19 love pass). Play a chord, hold `freeze`, and the
chord hangs as a pad you can play over — the shimmer-pad trick. While the
gate is high the tank recirculates at **exactly unity gain** with the
damping low-pass bypassed and the input muted from the tank, so whatever
was ringing at the moment of the freeze keeps ringing, unchanged, for as
long as the gate is held. The feedback matrix is orthonormal, so the unity
loop is lossless by construction rather than by tuning: the energy in
circulation is conserved to within 0.01 dB over 10 s of freeze (pinned),
and the output RMS wanders no more than ±0.35 dB around the level that
was caught (a fixed ±1 tap's share of the energy shifts as the lines mix)
— it neither decays nor grows. The dry path through `mix` never sees the
gate: notes played over a frozen tail are heard dry, and because they
never reach the tank the pad does not pile up. When the gate falls,
`decay` and `damping` resume and the held tail decays away like any
other. The switch is per sample — an integer-count **10 ms ramp** from
each gate edge crossfades the loop gain, the damping bypass and the input
mute together — so a freeze landing anywhere in a block is click-free
(steps across the edge no larger than the tail's own; an abrupt switch
would be *captured* by the lossless loop and click forever, which is the
other reason the ramp exists) and lands on the same sample at any block
size. The damping one-pole keeps tracking the raw read while bypassed, so
its state is warm the moment the gate falls. Feed it a
[`key_trigger`](#key_trigger) latch to freeze by hand, a slow
[`clock`](#clock) (or its [`logic`](#logic) `not_a`) to freeze between
strikes, or a [`function_generator`](#function_generator) `eoc` chain for
timed holds. See `examples/reverb_freeze_pad.json`.

#### `compressor`

A **feed-forward compressor** — the rack's dynamics processor. It watches a
detector signal and, whenever it rises above the `threshold`, turns the gain
down by a fraction set by `ratio` (2:1 halves every dB over the line; 20:1 is
effectively a limiter). `attack` / `release` set how fast the gain chases the
level, `knee` softens the bend around the threshold, `gain` is the make-up
boost, and `mix` blends the compressed signal back against the dry one for
**parallel** (New York) compression.

The detector normally listens to `in` (ordinary feed-forward), but patch a
signal into `sidechain` and it gain-controls `in` while listening to *that* —
the classic **ducking** trick (a kick in a pad's sidechain pumps a hole for
the low end). Unpatched, `sidechain` is normalled to `in`. `detector` picks
`peak` (instantaneous, transient-accurate) or `rms` (~10 ms energy window,
loudness-like).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to compress. Unpatched → silence. |
| `sidechain` | in | audio | External detector key. Normalled to `in` when unpatched. |
| `threshold_cv` | in | cv | Added to `threshold` (block-meaned), scaled by `threshold_cv_depth`. Optional. |
| `out` | out | audio | Compressed (and optionally parallel-mixed) signal. |
| `gr` | out | cv | Applied gain reduction, `applied_gain − 1` (0 … −1). Patch for ducking/metering. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `threshold` | `-18.0` | −60 … 0 dB | Level above which compression starts. |
| `ratio` | `2.0` | 1 … 20 | Compression ratio. 1 = off; 20 ≈ limiting. |
| `attack` | `10.0` | 0.1 … 250 ms | Gain fall time toward deeper reduction. |
| `release` | `120.0` | 5 … 2500 ms | Gain recovery time. |
| `knee` | `6.0` | 0 … 24 dB | Soft-knee width (0 = hard knee). |
| `gain` | `0.0` | 0 … 24 dB | Make-up gain applied after compression. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 1 = fully compressed; <1 = parallel. |
| `detector` | `rms` | peak / rms | Level detection: instantaneous peak or ~10 ms RMS. |
| `threshold_cv_depth` | `12.0` | dB/unit | dB of threshold shift per `threshold_cv` unit. |

Detector on the sidechain → level in dB → soft-knee gain computer (log domain)
→ attack/release smoothing of the gain reduction (the same vectorized monotone
one-pole the [`audio_to_cv`](#audio_to_cv) follower uses) → linear multiply,
make-up and parallel mix. Zero latency, so `mix` needs no delay compensation.
With `ratio` = 1, `gain` = 0 and `mix` = 1 it **short-circuits to a bit-exact
passthrough** (the detector is skipped). Shape-polymorphic like the other
effects — a `(V, F)` input compresses per voice, a single row bit-identical to
mono; a mono sidechain broadcasts across voices. The `gr` output mirrors the
applied gain (`applied_gain = gr + 1`). See `examples/sidechain_pump.json`
(a kick ducking a pad).

**Patching.** Even out a bass or vocal (`rms`, ratio ~3, soft knee, make-up to
taste); pump a pad from a kick via the `sidechain`; smash drums in parallel
(`mix` ~0.3); or drive `gr` into a VCA / any `*_cv` to duck a whole group in
lock-step.

#### `limiter`

A **brickwall lookahead limiter** — the "demo can't clip" module. Where the
[`compressor`](#compressor) eases gain down by a *ratio* around a threshold, the
limiter is an absolute wall: whatever it takes to keep the output at or under
`ceiling`, it does. It **delays the audio by `lookahead`** and watches the
un-delayed signal, so by the time a loud sample reaches the output the gain has
already ramped down to meet it — the descent is spread linearly across the
lookahead window and lands exactly on the peak, so there's no hard corner.
After the peak, gain recovers with a one-pole `release`.

Because the audio is delayed, the limiter has a **fixed latency equal to the
lookahead** — `round(lookahead_ms · sr / 1000)` samples, constant for a given
`lookahead` and independent of block size, so a parallel dry path elsewhere can
be compensated by the same amount.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to limit. Unpatched → silence. |
| `out` | out | audio | Limited signal, delayed by `lookahead`. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `ceiling` | `-1.0` | −20 … 0 dBFS | Hard output ceiling; the peak never exceeds it. |
| `release` | `80.0` | 20 … 1000 ms | One-pole gain recovery time after a peak. |
| `lookahead` | `5.0` | 1 … 10 ms | Attack-ramp window; also the fixed processing latency. |

Instantaneous target gain `min(1, ceiling/|x|)` → slope-limited lookahead
anticipation (the gain ramps at ≤ 1/`look` per sample so it lands on the
trough) → one-pole release (instant on the way down, `release`-paced on the way
up) → a final per-sample clamp to `ceiling/|x|` that keeps the wall hard to the
last ULP → multiply into the delayed audio. Under the ceiling the gain is
exactly 1.0 and the output is a **bit-exact (delayed) passthrough**.
Shape-polymorphic like the other effects — a `(V, F)` input limits per voice
(no cross-voice ducking), a single row bit-identical to mono. See
`examples/limiter_brickwall.json` (two saws summed hot, held at −1 dBFS).

**Patching.** Drop one in front of the [`speaker_output`](#speaker_output) as a
master safety net; pull `ceiling` down a dB and drive the input harder for
transparent loudness; or tame a spiky source before a stage that assumes
headroom. It is the last link in the chain — place it after any make-up gain.

#### `noise_gate`

A **hold-and-hysteresis downward gate** — the inverse of the
[`compressor`](#compressor). While the detector sits above `threshold` the
gate is **open** and the signal passes untouched; when the level falls away
the gate **closes** and pulls the output down to the `range` floor. Kills the
hiss/hum in the gaps between notes, tightens a boomy drum by chopping its
tail, or (sidechained) chops one sound to another's rhythm.

`hysteresis` is a Schmitt gap — the gate opens above `threshold` but only
closes once the level falls `hysteresis` dB below it, so a signal parked at
the boundary can't chatter. `hold` keeps the gate open for a minimum time
after the level drops under the close threshold, bridging brief dips (the
quiet moment inside a word, the gap between two hits of a roll). `attack` /
`release` ramp the open / close so transients keep their edge and tails fade
instead of clicking. `range` sets how far a closed gate ducks: −80 dB is a
full mute, a shallower value (say −12 dB) only ducks the noise floor — an
**expander**-style gentle gate.

The detector normally listens to `in`, but patch a signal into `sidechain`
and the gate opens/closes on *that* while still gating `in` (key a pad off a
hi-hat for rhythmic chops). Unpatched, `sidechain` is normalled to `in`. The
**`open`** output is a 0/1 gate CV, high exactly while the gate is open — a
free gate-*extractor*: drive an ADSR, a VCA, or a clock's reset from any
audio signal's dynamics (a crude beat detector / audio→trigger bridge).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to gate. Unpatched → silence. |
| `sidechain` | in | audio | External detector key. Normalled to `in` when unpatched. |
| `out` | out | audio | The gated signal. |
| `open` | out | cv | 0/1 gate, high while the gate is open. A free gate-extractor. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `threshold` | `-45.0` | −80 … 0 dBFS | Open level. At the −80 floor the gate is a bit-exact bypass (always open). |
| `hysteresis` | `4.0` | 0 … 24 dB | Schmitt gap; the gate closes only this far below `threshold`. Anti-chatter. |
| `attack` | `1.0` | 0.1 … 50 ms | Open ramp time. |
| `hold` | `40.0` | 0 … 500 ms | Minimum open time after the level drops below the close threshold. |
| `release` | `150.0` | 5 … 2000 ms | Close ramp time. |
| `range` | `-80.0` | −80 … 0 dB | Closed-gate floor. −80 = full mute; higher = expander-style duck. |

Instant-attack / one-pole-release peak follower on the key → Schmitt
open/close with hysteresis + a hold timer → target gain (1 open / `range`
floor closed) → attack/release smoothing of the gain → multiply. A single
per-sample voice loop carries the detector, Schmitt/hold and gain state, so
the render is **block-size independent and bit-exact** (pure recurrences, no
reassociation — stronger than the compressor's to-round-off gain solve).
`threshold` at its −80 floor **short-circuits to a bit-exact passthrough**
(always open, `open` = 1). Shape-polymorphic like the other effects — a
`(V, F)` input gates per voice (a single row bit-identical to mono); a mono
sidechain broadcasts across voices, a `(V, F)` sidechain keys each voice. See
`examples/noise_gate_chop.json` (an LFO-driven auto-gate).

**Patching.** Silence the hiss between phrases (`range` −80); tighten drums
(short `hold`, fast `release`); expand rather than gate (`range` −6…−12);
sidechain-chop a pad off a rhythmic key; or take `open` into an ADSR / VCA /
clock so the patch plays in step with "is the signal present".

#### `transient_shaper`

A **transient shaper** — reshapes a sound's dynamic envelope, and the one
dynamics tool with *no threshold to set*. Push `attack` to snap onsets (a
pluck, a kick's click, a picked string) or cut it to soften them; push
`sustain` to bloom the body and tail (room, ring, decay) or cut it to dry a
boomy kit or shorten a ringing tail without a gate. `speed` picks how quick
the detector pair is — `fast` for tight percussion, `slow` for bass and
sustained material, `med` between.

**Threshold-free (the classic trick).** The effect is **level-independent** —
a quiet ghost note is shaped exactly like a loud accent, and turning the input
up or down changes nothing. Two envelope followers run on `|in|`, one **fast**
and one **slow**; their *difference in dB* isolates the transient. When a note
attacks the fast follower leaps ahead of the slow one (difference **positive** →
an onset), and as it decays the fast follower drops below (difference
**negative** → sustain); in steady state the two agree and the difference is
zero, so a held tone is untouched. Because a dB difference is a *ratio*, it is
the same at any level — that is what makes the shaper threshold-free.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to shape. Unpatched → silence. |
| `out` | out | audio | Reshaped signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `attack` | `0.0` | −1 … +1 | Onset gain. +1 boosts attacks by up to +12 dB, −1 cuts by −12 dB. Acts only where the signal is transient. |
| `sustain` | `0.0` | −1 … +1 | Body/tail gain. +1 lifts the sustain by up to +12 dB, −1 dries it by −12 dB. Acts only where the signal is decaying. |
| `speed` | `med` | fast / med / slow | Follower-pair responsiveness (fast/med/slow = 0.5·20 / 2·50 / 5·120 ms). |

The positive part of the follower difference scales the `attack` gain and the
negative part the `sustain` gain (each soft-saturated to top out near ±12 dB);
the two sum in dB, a short one-pole smooths the linear gain, and it multiplies
the signal. The followers reuse the same vectorized fixed-point one-pole the
[`audio_to_cv`](#audio_to_cv) follower and the compressor's gain smoother use.
With `attack` = `sustain` = 0 it **short-circuits to a bit-exact passthrough**
(the followers are skipped). Shape-polymorphic like the other effects — a
`(V, F)` input is shaped per voice with independent follower/gain state, a
single row bit-identical to mono. Block-size independent to float64 round-off
(the reassociated follower solve, < 1e-6 after the float32 cast). See
`examples/transient_shaper_snap.json`.

**Patching.** Add snap to a drum loop (`attack` up, `speed` fast) or tame a
clicky kick (`attack` down); dry an over-roomy kit or shorten a snare's ring
(`sustain` down) — a gate-free "less room" move; bring out a bass or pad's body
(`sustain` up) without a compressor's pumping.

#### `loudness`

An **equal-loudness contour** (loudness compensation). The ear hears less
bass and treble as things get quieter (the Fletcher–Munson / equal-loudness
curves), so this boosts the low and high ends as you turn `level` down — a
hi-fi "loudness" button — with manual `bass` / `treble` trims on top. It
reshapes the *frequency balance* (a static, level-dependent EQ), unlike an
envelope sweeping a filter over time.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to shape. Unpatched → silence. |
| `level_cv` | in | cv | Added to `level`, scaled by `cv_depth`. Optional. |
| `out` | out | audio | The contoured signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `level` | `0.5` | 0 … 1 | Listening level. 1 = flat; lower = more bass/treble boost. |
| `bass` | `0.0` | −12 … +12 dB | Manual low-shelf trim, added on top of the auto curve. |
| `treble` | `0.0` | −12 … +12 dB | Manual high-shelf trim, added on top. |
| `cv_depth` | `1.0` | 0 … 2 | Level change per unit of `level_cv`. |

Two RBJ shelving biquads (low ~120 Hz, high ~8 kHz); the auto curve boosts
bass more than treble as the level drops. At `level` = 1 with no trims it is
a **bit-exact passthrough**. Shape-polymorphic like [`parametric_eq`](#parametric_eq);
the contour is one global control (a voice-aware `level_cv` is averaged). See
`examples/loudness_demo.json` (a quiet bassline kept full by the contour).

**Patching.** Drop it on the output bus as a master "loudness", or fatten a
thin oscillator / the mic. Automate `level_cv` from an envelope for a sound
that warms as it fades.

#### `distortion`

A **drive pedal** — the rack's first nonlinear stage. Everything else
in the Processors family reshapes the signal *linearly*; distortion
bends the waveform itself, creating harmonics that were never in the
input. Push a dull sine and it grows teeth; push a saw and it turns
into a wall.

`drive` scales the signal into the curve; `mode` picks the bend:
**soft** (normalised tanh — smooth, warm, odd harmonics), **hard**
(straight clipping — aggressive, buzzy) or **tube** (asymmetric tanh —
adds *even* harmonics, the octave-flavoured valve warmth; its DC
byproduct is blocked internally). `tone` is the classic
post-distortion low-pass, `level` trims the loud result, `mix` blends
dry back in for parallel grit, and `drive_cv` modulates the drive per
sample (envelope → bite on the attack; LFO → chew).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to distort. Unpatched → silence. |
| `drive_cv` | in | cv | Per-sample drive modulation, scaled by `cv_depth`. Optional. |
| `out` | out | audio | The distorted signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `drive` | `4.0` | 0.1 … 30 | How hard the signal is pushed into the curve. |
| `mode` | `"soft"` | soft / hard / tube | Curve family (see above). |
| `tone` | `20000` | 200 … 20000 Hz | Post-distortion low-pass; 20 kHz = out of the circuit. |
| `level` | `1.0` | 0 … 2 | Output trim — saturation is loud. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 0 = bit-exact passthrough. |
| `cv_depth` | `5.0` | 0 … 30 | Drive units per unit of `drive_cv`. |

**How it works.** The curve runs at **4× the sample rate** between a
streaming polyphase up/down pair: nonlinear stages generate harmonics
past Nyquist that would otherwise fold back as inharmonic aliasing
hash, so they're filtered off at 4× *before* decimation (the folded
5th harmonic of a hard-clipped 6 kHz sine measures > 34 dB below the
legitimate 3rd). The FIR pair costs a fixed 16 samples (~0.4 ms); the
dry path of `mix` is delay-compensated to match. All three curves are
normalised (full scale in → full scale out) and tend to the identity
as drive → 0. Shape-polymorphic with per-voice filter state; a single
voice row is bit-identical to mono; block-size independent. See
`examples/distortion_drive.json` (a sequenced saw riff through the
tube curve).

---

#### `waveshaper`

A **wavefolder** — the west-coast sibling of the
[`distortion`](#distortion) pedal. Where distortion *flattens* a
waveform against the rails, a folder *reflects* it: signal that would
exceed the rails folds back toward zero, and keeps folding as you push
harder. A plain sine through a rising `fold` sweeps from pure tone
through nasal, brassy, metallic, to shimmering comb-like spectra — the
classic Buchla/Serge way to build complex timbres from simple sources
(sine in, fold, filter after).

`fold` is the push into the folder (1 = a full-scale signal just
touches the rails); `mode` picks the reflection — **triangle** (hard
geometric reflection; *exact passthrough* below the rails) or **sine**
(a sine transfer curve: smooth creases, gently colours even below the
rails). `symmetry` slides the signal off-centre before folding for
even harmonics and growl (its DC byproduct is blocked internally), and
`fold_cv` modulates the fold per sample — a slow LFO here is *the*
wavefolder patch. `mix` = 0 is a bit-exact passthrough.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to fold. Unpatched → silence. |
| `fold_cv` | in | cv | Per-sample fold modulation, scaled by `cv_depth`. Optional. |
| `out` | out | audio | The folded signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `fold` | `1.0` | 0 … 16 | Fold amount; 1 = rails just reached, higher folds repeatedly. |
| `symmetry` | `0.0` | −1 … 1 | Pre-fold offset — even harmonics; DC blocked internally. |
| `mode` | `"triangle"` | triangle / sine | Hard reflection vs smooth sine fold. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 0 = bit-exact passthrough. |
| `cv_depth` | `4.0` | 0 … 16 | Fold units per unit of `fold_cv`. |

**How it works.** Shares the Distortion's **4× streaming oversampling**
pair (folding is savagely bright — without it the upper folds alias
into inharmonic hash) and its 16-sample delay-compensated dry path.
The triangle fold is one vectorised `mod` (the periodic triangle
function of `fold·x + symmetry`, identity for |u| ≤ 1); the sine fold
is `sin(π/2·u)`. The DC blocker only engages while `symmetry` ≠ 0, so
a centred triangle fold at `fold` = 1 passes a full-scale signal
through *exactly* (modulo the oversampler's tiny FIR ripple).
Shape-polymorphic, per-voice state, block-size independent. See
`examples/waveshaper_fold_drone.json` (a pure 110 Hz sine, fold swept
1→7 by a 0.08 Hz LFO with a touch of symmetry — timbre that breathes).

---

#### `octaver`

The **zero-crossing flip-flop sub-octave** (Boss OC-2 lineage): every
rising zero-crossing of the input toggles a flip-flop — a square at
**half** the input frequency; a second flip-flop toggling on the first
gives **quarter**. Each square rides the input's own envelope (the
`audio_to_cv` asymmetric follower, 5 ms attack / 50 ms release — so
silence stays silent and playing dynamics survive), the summed subs
pass one one-pole low-pass (`tone`) to round the corners, and the
result mixes under the dry. A lead line in, an instant bass line
underneath. Proudly **monophonic-minded**: chords and bright timbres
make the flip-flops stutter — that's the hardware's charm too; feed it
single-note lines for clean tracking. `dry` 1 + subs 0 is a bit-exact
passthrough. All state carries across blocks. **Ports**: `in` (audio)
→ `out` (audio). **Params**: `dry` 0..1 (1) · `sub1` 0..1 (0.5) ·
`sub2` 0..1 (0) · `tone` 200..2000 Hz (800). See
`examples/octaver_bass_lead.json`.

#### `ring_mod`

A **ring modulator** — the metallic, inharmonic corner of the rack. A
[`vca`](#vca) multiplies audio by a (mostly positive) control voltage; a
ring mod multiplies audio by another *bipolar audio* signal, the
**carrier**, and the product keeps only the **sum and difference**
frequencies of the two inputs — none of the originals. A 200 Hz tone
against a 440 Hz carrier rings at 240 and 640 Hz; because those sidebands
rarely line up with the input's own harmonics, the ear hears *inharmonic*
metal — bells, gongs, the Dalek growl, clangorous sci-fi textures.

The carrier is either **external** — patch any audio source into
`carrier` for classic two-oscillator ring mod — or, when `carrier` is
unpatched, an **internal sine** at `freq`, so the module is
self-contained: drop it after an [`oscillator`](#oscillator) and dial the
metal in. The internal sine is a per-voice phase-accumulated oscillator,
swept 1 V/oct by `freq_cv × freq_cv_depth`. `mix` blends dry against the
modulated signal; `mix = 0` is a **bit-exact dry passthrough**.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to modulate. Voice-aware; a single voice row is bit-identical to mono. Unpatched → silence. |
| `carrier` | in | audio | External carrier. Unpatched → internal sine at `freq`; patched → the two signals multiply and `freq` / `freq_cv` are bypassed. |
| `freq_cv` | in | cv | 1 V/oct pitch of the internal carrier, scaled by `freq_cv_depth` (per-sample). Optional; ignored with an external carrier. |
| `out` | out | audio | `in` × carrier, blended with dry by `mix`. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `freq` | `440.0` | 1 … 5000 Hz | Internal-carrier pitch. Ignored while `carrier` is patched. |
| `freq_cv_depth` | `1.0` | 0 … 4 oct/unit | Octaves the internal carrier shifts per `freq_cv` unit (1 V/oct). 0 disables the sweep. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 0 = bit-exact passthrough. |

**How it works.** `out = in × carrier`, blended as
`(1−mix)·in + mix·(in·carrier)`. The internal carrier integrates phase
per sample from `freq · 2^(freq_cv_depth · freq_cv)`, with per-voice
phase held in state so a swept carrier stays continuous across blocks; an
**exclusive prefix sum** puts a fresh module's first sample at phase 0 (a
deterministic, testable sine). Shape-polymorphic — the `(V, F)` core runs
with `V = 1` for mono, so one voice row is bit-identical to mono and
voices stay independent. `mix = 0` short-circuits to the input untouched
(no phase advance). The dry and external-carrier paths are exactly
block-size independent; the internal sine matches across block sizes to
within float phase-wrap rounding (< 1e-6). See `examples/ring_mod_bells.json`
(a mallet-plucked sine rung by a 523 Hz internal carrier into tuned
bells). Pairs with the planned `fm_op` and `modal` modules as the
inharmonic corner of the rack.

---

#### `freq_shifter`

A **frequency shifter** — the *other* inharmonic corner of the rack,
beside [`ring_mod`](#ring_mod). Where a [`pitch_shifter`](#pitch_shifter)
*multiplies* every partial's frequency by a ratio (harmonics stay
harmonic — the same note, higher or lower), a frequency shifter **adds a
fixed number of hertz to every partial**. A 100/200/300 Hz series shifted
up 50 Hz becomes 150/250/350 — no longer integer multiples of any
fundamental, so the ear stops hearing "a note" and hears metallic,
inharmonic clang. Small shifts give slow phasing and a hollow "detune
that never resolves"; larger shifts give bells and robots; and with
`feedback` the endlessly re-shifted signal becomes the classic
**barberpole / Shepard** glide that seems to rise (or fall) forever.

The shift is done the analog **Bode/Moog** way — single-sideband
modulation. The input is split into a quadrature (90°) pair by a 255-tap
FIR **Hilbert transformer** to form its analytic signal, which is then
rotated by a complex sine at the shift frequency. The two real
projections of that rotation are the **two sidebands at once**: `out_up`
moves every partial *up* by `shift` Hz, `out_down` *down* by the same
amount (the mirror-image conjugate sideband). Patch whichever you want,
or both into L/R for a shimmering, decorrelated spread from a mono
source.

The Hilbert FIR has a fixed group delay of 127 samples (~2.9 ms), so the
wet runs that far behind the input; the dry in the `mix` blend is
delay-matched, so at `shift = 0` the wet *is* the delayed dry and the
blend is transparent rather than combing.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to shift. Voice-aware; a single voice row is bit-identical to mono. Unpatched → silence. |
| `shift_cv` | in | cv | **Linear-Hz** shift modulation (not 1 V/oct — a shift is an addition), scaled by `shift_cv_depth`, per-sample. Optional. |
| `out_up` | out | audio | Every partial shifted **up** by `shift` Hz. |
| `out_down` | out | audio | Every partial shifted **down** by `shift` Hz (the opposite sideband). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `shift` | `0.0` | −2000 … +2000 Hz | Hz added to every partial. Positive raises `out_up` / lowers `out_down`; negative swaps them. 0 → the wet is the delay-matched dry. |
| `shift_cv_depth` | `200.0` | Hz / CV unit | Hz of shift per `shift_cv` unit (linear, additive). 0 disables the CV. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 0 = bit-exact dry passthrough on both outputs. |
| `feedback` | `0.0` | 0 … 0.9 | Recirculates `out_up` (the shifted, wet signal) into the input — the barberpole/endless-shift comb. 0 = a clean single shift. |

**How it works.** The analytic signal `a = x_d + j·H{x}` is built from a
255-tap Type-III (antisymmetric, integer group delay) Hilbert FIR —
Hamming-windowed for a flat passband and > 55 dB opposite-sideband
rejection across the band, crucially holding down to low frequencies
where wider windows (Blackman, Kaiser) collapse against the Type-III DC
null. `out_up = x_d·cos φ − x_h·sin φ` and `out_down = x_d·cos φ +
x_h·sin φ`, where φ integrates `2π·shift/sr` per sample (held per voice
across blocks, exclusive-prefix so a fresh module starts at φ = 0).
Shape-polymorphic — the `(V, F)` core runs with `V = 1` for mono, so one
voice row is bit-identical to mono and voices stay independent. `mix = 0`
short-circuits to the input untouched (no latency, no state advance). The
block is processed in fixed 127-sample chunks so the `feedback`
recirculation only ever reads already-computed output: the recurrence is
causal and boundary-independent, making the result **block-size
independent** (bit-exact with no feedback, to < 1e-6 with). See
`examples/freq_shifter_barberpole.json` (a saw drone shifted with
feedback into a rising stereo shimmer). Pairs with [`ring_mod`](#ring_mod):
ring mod keeps the *sum and difference* of two signals, the frequency
shifter keeps *one shifted sideband* of one.

---

#### `bitcrusher`

A **bitcrusher** — the lo-fi "digital destruction" box, two independent
kinds of degradation in one module. **Bit reduction** requantizes each
sample to a coarser word length (a mid-tread quantizer snapping to
`2^bits` levels); at 24 bits it is transparent, at 8 bits you hear grainy
quantization hiss, at 1–3 bits the waveform collapses into buzzy digital
fuzz. **Sample-rate reduction** holds every `rate_div`-th sample and
throws the rest away — a sample-and-hold downsample with *no* anti-imaging
filter, so the discarded content folds back as aliasing, and that harsh,
metallic alias *is* the sound (the classic early-sampler / Aphex crunch).

`jitter` wobbles the hold length randomly around `rate_div` on a seeded
stream for a "broken converter" smear (inert unless `rate_div > 1`).
`mix` blends dry against crushed (`mix = 0` is a bit-exact dry
passthrough), and `dc_filter` runs a gentle one-pole high-pass on the
output to strip any offset the crushing introduces.

**CV.** Both crush axes are voltage-controllable. Patch a modulator (LFO,
envelope, a Crossover-rectified band…) into `bits_cv` to sweep the
quantizer word length, or `rate_cv` to sweep the sample-rate crush. Each
reads the block's **mean** CV (one value per block, like the Filter's
`cutoff_cv`) and is scaled by its own depth: `bits` shifts *additively*
(`bits + bits_cv_depth · cv`, default 1 bit per unit — each bit is an
octave of quantizer levels), `rate_div` *multiplicatively* (`rate_div ·
2^(rate_cv_depth · cv)`, octave-even decimation sweeps). CV can push `bits`
below 24 or `rate_div` above 1 from an otherwise-neutral base, waking the
crusher purely from voltage; unpatched, both inputs leave the static params
untouched (bit-for-bit the old behaviour).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to crush. Voice-aware; a single voice row is bit-identical to mono. Unpatched → silence. |
| `bits_cv` | in | cv | Bit-depth modulation, block-mean × `bits_cv_depth` added to `bits`. Optional. |
| `rate_cv` | in | cv | Sample-rate modulation, `rate_div` × `2^(rate_cv_depth · block-mean)`. Optional. |
| `out` | out | audio | Crushed (and dry-blended) signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `bits` | `24` | 1 … 24 | Quantizer word length. `round(x·2^(bits−1))/2^(bits−1)`. 24 skips the quantizer (bit-exact). |
| `bits_cv_depth` | `1.0` | −24 … 24 | Bits of shift per unit of `bits_cv` (1 = 1 bit/unit; ~23 sweeps the whole range from one unit; negative inverts). |
| `rate_div` | `1` | 1 … 64 | Sample-hold decimation factor (hold every Nth sample, deliberately aliased). 1 skips decimation. |
| `rate_cv_depth` | `1.0` | −6 … 6 | Octaves of decimation shift per unit of `rate_cv` (1 = one octave/unit; ±6 spans the full range). |
| `jitter` | `0.0` | 0 … 1 | Random hold-length wobble around `rate_div` (seeded, reproducible). No effect unless `rate_div > 1`. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 0 = bit-exact passthrough. |
| `dc_filter` | `False` | on/off | One-pole DC blocker on the crushed signal. Off by default. |

**How it works.** Signal flow is `in → decimate → quantize → [dc filter]
→ mix`. The quantizer is pointwise; decimation is a sample-and-hold
located by integer division of the global sample index (`//N`) when
`jitter = 0`, or by `searchsorted` over a seeded stream of wobbled hold
lengths when it is not (quantize and decimate commute, so their order is
immaterial to the result). The hold phase — the global sample offset, the
per-voice held value, and the jitter boundary stream — lives in state, so
holds stay continuous across block joins; every path (quantize, decimate,
jitter, DC filter) is **exactly block-size independent** — with the CV
inputs absent or constant (a time-varying `bits_cv`/`rate_cv` uses each
block's mean, so it tracks the block boundaries, exactly as `cutoff_cv`
does). Neutral is
`bits = 24 ∧ rate_div = 1`: both crush ops are skipped, so the wet path
equals the dry input — a bit-exact passthrough at any `mix` (with
`dc_filter` off). Shape-polymorphic — the `(V, F)` core runs with `V = 1`
for mono, so one voice row is bit-identical to mono and voices stay
independent. See `examples/bitcrusher_lofi.json` (a plucked saw line
crushed to a crunchy 8-bit / quarter-rate lo-fi lead).

---

#### `tape`

**"Put it on tape"** — wow, flutter, drift, saturation, hiss and a head
bump in a single pass: the sound of running a signal through an analog
tape machine. Six independent flavours of that character, layered in the
order a real deck imposes them:

- **wow** — slow (~1 Hz) pitch sway, from a worn capstan or off-centre
  reel; a moving tape speed is a moving pitch, so the sound drifts gently
  sharp and flat.
- **flutter** — fast (~9 Hz) pitch waver *plus a little noise* (scrape
  flutter): a shimmer rather than a sway.
- **drift** — very slow, non-periodic speed wander; the pitch centre
  ambles around over seconds.
- **sat** — tape saturation, a soft `tanh` curve on the shared 4x
  oversampling path (so the added harmonics don't alias): warmth low,
  crunch high.
- **hiss** — a calibrated noise floor, from off up to −30 dB.
- **bump** — the *head bump*, a broad low-shelf lift around 60 Hz that
  makes tape "sound bigger" down low.

The wow/flutter/drift together modulate one short **fractional-delay
line** (a moving delay is a moving pitch); its fixed ~10 ms nominal delay
is latency-compensated in the dry path, so `mix` blends dry against wet
without combing.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to tape. Voice-aware; a single voice row is bit-identical to mono. Unpatched → silence. |
| `out` | out | audio | Taped (and dry-blended) signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `wow` | `0.0` | 0 … 1 | Depth of the slow (~1 Hz) pitch sway. |
| `flutter` | `0.0` | 0 … 1 | Depth of the fast (~9 Hz) waver (with a little noise). |
| `drift` | `0.0` | 0 … 1 | Amount of slow random speed wander. |
| `sat` | `0.0` | 0 … 1 | Tape-saturation drive (`tanh`, 4x oversampled). |
| `hiss` | `-80.0` | −80 (off) … −30 dB | Noise-floor level. Lives in the wet path, so it scales with `mix`. |
| `bump` | `0.0` | 0 … 6 dB | Low-shelf head bump around 60 Hz. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. 0 = bit-exact dry passthrough. |

**How it works.** Signal flow is `in → wow/flutter/drift-modulated
fractional delay → saturation → + hiss → head-bump low shelf → mix with
the latency-matched dry`. The delay line reuses the chorus core (write
the whole block, then read fractional taps behind the write head); with
no feedback every read references an already-written sample, so the
render vectorises and is **exactly block-size independent**. The
wow/flutter LFOs carry their phase in state; the drift, flutter noise and
hiss are each a *single* seeded generator drawn one sample per output
sample and streamed through one-pole / biquad filters with carried state
— so every stochastic path is block-size independent too, and a patch
renders identically every time. One tape path is modelled: the modulation
and hiss are **shared** across a polyphonic input's voices (each voice
keeps its own delay line, oversampler and shelf state, so they never
cross-talk), and a single voice row is bit-identical to the mono render.
Neutral is `wow = flutter = drift = sat = bump = 0` with `hiss` off — a
**bit-exact passthrough** (a freshly added Tape does nothing until you
turn a knob); `mix = 0` is likewise bit-exact dry. See
`examples/tape_cassette.json` (a plucked riff run through a wobbly,
saturated old cassette).

---

#### `vinyl`

**Surface noise and warp** — [`tape`](#tape)'s scrappy sibling: the
record, not the machine. One knob per vice, all seeded: `crackle` is
dust — seeded Poisson ticks whose rate *and* size scale with the knob;
`rumble` is the turntable bearing — seeded noise through a ~40 Hz
resonant low-pass (felt more than heard; mind the subs); `wobble` is
the once-per-revolution pitch wobble at **0.55 Hz** (33⅓ rpm) via a
modulated fractional delay, up to ~±24 cents at full. All three at
zero is a **bit-exact passthrough**. Engaging `wobble` puts the signal
on the turntable (a ~5 ms nominal delay under the modulation), so the
knob's 0↔on edge is a patch-edit moment. Everything is deterministic
per `seed` (which pressing of the record) and **exactly block-size
independent** — the noise streams are drawn per absolute-sample
window, so any block split sees the identical dust. One turntable:
polyphonic input sums to mono at the door; with nothing patched the
module still emits its crackle + rumble — a free surface-noise bed.
**Ports**: `in` (audio) → `out` (audio, mono). **Params**: `crackle`
0..1 (0.3) · `rumble` 0..1 (0.2) · `wobble` 0..1 (0.2) · `seed` (1).
See `examples/vinyl_dust.json` — a lo-fi melody box.

#### `convolver`

A **convolution reverb / cabinet loader**. Convolution stamps a scaled,
delayed copy of an **impulse response** (IR) onto every input sample and sums
the overlaps — and because an IR *is* the recorded sound of a space or device
answering a single click, that one operation reproduces whatever was captured:
a real room, hall, plate or spring tank, a guitar/speaker cabinet, or any
exotic sampled "reverb". Load an IR and the input now sounds like it was
played there.

**Loading an IR.** Point `path` at an audio file — a WAV, or (with the
`[media]` extra / a system ffmpeg) an mp3/flac/ogg/m4a or the audio track of a
video — or click **Browse…** on the node for the same picker the
[`file_player`](#file_player) uses. IRs load **whole** (no streaming; they're
short), and the decode + partition-FFT build run on a **background thread**, so
a fresh or changed IR never blocks the audio thread — the convolver keeps the
previous IR (or a transparent unit impulse) sounding until the new one is
ready. On load the IR is **energy-normalised** (a single scale across both
channels, so different IRs sit at a consistent level without blowing up or
vanishing, and the stereo image is preserved) and **length-capped** (~5 s to
start, truncated with a short fade) so a stray long file can't stall the audio
— the **DSP %** readout is the meter for how long an IR you can afford. An
empty / missing / unreadable path is a **transparent insert** (a unit-impulse
IR: dry passthrough delayed by the reported latency), so a saved patch always
loads even if the IR moved.

**Stereo.** The convolver emits a **stereo pair**. A stereo IR convolves the
(mono-summed) input through its **left** channel into `out_l` and its **right**
into `out_r` — the decorrelation captured in the IR *is* the stereo image (a
room miked in stereo, a stereo plate, ping-pong tanks). A mono IR drives both
channels identically, and is convolved once.

**Shaping the wet.** `predelay` (0…500 ms) delays the reverb onset behind the
dry — the gap that keeps a source articulate in a big space; `tone` is a wet
low-pass (1 k…20 k Hz) that darkens the tail and is **off** at its maximum.
Both act on the wet only, so `mix = 0` stays a bit-exact dry bypass.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to convolve. A polyphonic (voice-aware) source is summed to mono first — convolution is linear, so convolving each voice then summing equals summing then convolving, and the mono sum is far cheaper. A single voice row is bit-identical to mono. Unpatched → silence. |
| `out_l` | out | audio | Left output (dry + wet through the IR's left channel). Equals `out_r` for a mono IR. |
| `out_r` | out | audio | Right output (dry + wet through the IR's right channel). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `path` | `""` | file path | IR audio file (WAV or ffmpeg-decodable). Empty / missing / unreadable → a unit-impulse IR (transparent insert). Loaded whole off the audio thread, energy-normalised and length-capped; **Browse…** opens the file picker. A **relative** path is looked up next to the patch file first, then in its parent folder, then in the directory you launched from — so a patch plus its media travels as a unit and plays the same however the app was started. A file that fails to load says so in the status bar instead of quietly playing silence. |
| `predelay` | `0.0` | 0 … 500 ms | Wet-only pre-delay — how far the reverb onset sits behind the dry. 0 = starts with the dry. |
| `tone` | `20000.0` | 1000 … 20000 Hz | Wet low-pass cutoff. At 20000 (max) the filter is **off** (transparent wet); lower darkens the tail. |
| `gain` | `1.0` | 0 … 2 | Linear trim on the **wet** only. The dry path is never scaled, so `mix = 0` is a bit-exact dry bypass whatever `gain` is. |
| `mix` | `1.0` | 0 … 1 | Dry/wet balance. 0 = bit-exact dry (bypass; the FFT is skipped), 1 = fully wet. |

**How it works.** The DSP is a **uniformly-partitioned overlap-save FFT
convolution**. The IR is chopped into render-block-sized partitions, each
pre-transformed once to a length-`2B` rfft (`B` = block size). Every render
block transforms the `[previous block | current block]` window once, pushes
that spectrum onto a **frequency-domain delay line** of the last
`P = ceil(L / B)` input spectra, and the output is the frequency-domain
multiply-accumulate `Σ_p H[p]·X[k−p]` inverse-transformed (the alias-free
overlap-save half). That turns an `O(L)` tap-for-tap sum into one FFT pair per
block, so cost scales with IR length. A stereo IR runs one such engine per
channel (a mono IR shares one); the wet of each channel is then shaped by a
one-pole `tone` low-pass and a `predelay` FIFO before the wet `gain` and the
`mix` with the latency-matched dry.

The overlap-save core is intrinsically zero-latency; a one-block output
register presents a clean, fixed **one-block (`B`-sample) latency** that the
dry path is delay-matched against inside `mix`, so dry and wet stay
phase-coherent (`predelay` is an extra, intentional wet-only delay on top). All
FFT math is float64 (numpy upcasts regardless), cast to float32 on the way out.
Because the transform size `N = 2B` depends on the block size, the result is
**block-size independent only up to FFT round-off** (~1e-6), not bit-exact —
pinned and documented; the oracle tests hold each block size to `fftconvolve`
within that tolerance. Neutral: a unit-impulse IR at `mix = 1` (`gain = 1`,
`predelay = 0`, `tone` off) is a passthrough delayed one block, within ~1e-6
(the FFT round-trip is float, not exact); `mix = 0` is bit-exact delayed dry.

See `examples/convolver_reverb.json` (a plucked line through a synthetic hall)
— run `python examples/irs/generate_irs.py` first to create the license-clean
example IRs it points at (`room` / `hall` / `plate`; the patch is a transparent
passthrough until they exist). `examples/convolver_insert.json` is the bare
transparent insert.

---

#### `chorus`

A **detuned multi-voice stereo chorus** — it thickens a sound into an
ensemble. The input feeds a small bank of short delay lines that an
internal LFO sweeps; a moving delay is a moving pitch, so each copy
drifts a few cents around the original, and that shifting detune is what
the ear reads as one sound *thickened*. The voices are panned across a
**stereo pair** (`out_l` / `out_r`) for width. There is deliberately no
feedback — a fed-back chorus is a flanger, which is its own module.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to thicken (voice sources summed to mono). Unpatched → silence. |
| `rate_cv` | in | cv | Modulates the LFO rate (1 V/oct × `cv_depth`). Optional. |
| `out_l` | out | audio | Left channel (dry + panned wet). |
| `out_r` | out | audio | Right channel (dry + panned wet). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `rate` | `0.6` | 0.05 … 10 Hz | LFO sweep speed. Slow = gentle drift; fast → vibrato/Leslie. |
| `depth` | `0.5` | 0 … 1 | Sweep amount. Low = subtle thickening; high = wide, warbly detune. |
| `voices` | `3` | 1 … 6 | Number of detuned copies. More = a denser, wider ensemble. |
| `mix` | `0.5` | 0 … 1 | Dry/wet balance. `0` is a bit-exact dry passthrough on both channels. |
| `cv_depth` | `1.0` | 0 … 4 oct/unit | Octaves of LFO-rate shift per unit of `rate_cv`. |

Patch the outputs into [`left_speaker_output`](#left_speaker_output) and
[`right_speaker_output`](#right_speaker_output) for a wide ensemble. The
chorus is **block-size independent** (no feedback, so the whole render
vectorizes and never depends on the audio block size). See
`examples/chorus_lush.json` (a self-playing saw pad widened into a four-
voice ensemble, with a slow LFO drifting the chorus rate through
`rate_cv`).

**Patching.** `… → vca → chorus → L/R speakers`. Widen a mono pad or
ensemble a saw lead; feed a slow envelope or LFO into `rate_cv` for a
shimmer that breathes. For the resonant jet-sweep sound, reach for its
sibling the [`flanger`](#flanger) (feedback + a shorter delay).

---

#### `granular`

A **grain cloud** over a live-captured buffer — the rack's other grain
module, and the opposite temperament to the
[`pitch_shifter`](#pitch_shifter). That one *hides* its grains (WSOLA
picks their positions so the joins vanish and the result is the input,
transposed). This one **exposes** them. The input is captured
continuously into a ring `buffer` seconds long, and a scheduler fires
**grains** — short windowed reads of that history, each played at its
own rate — `density` times a second, `size` ms long, from `position`
seconds ago, transposed by `pitch`. The output is every grain in flight,
overlap-added.

Where you park the knobs is the sound. The **neutral** is the defaults:
`hann` grains at 50% overlap (`density` × `size` = 2) tile to exactly
one, so at `pitch` 0 the output *is* the input, delayed by `position` —
a grain delay you then bend. `pitch` up or down is a granular pitch
shifter of the classic, un-hidden kind: exact inside every grain, but a
synchronous grain train chops the phase at every hop, so on a held tone
the energy sits on a sideband within ±`density` Hz of the target — a
little metallic at high density, smeared at low, and *that* is the sound
people patch these for. `density` down until the grains stop touching is
a stutter / a tremolo at the grain rate; `size` short is a buzz at that
rate; `position` up reads further into the past; `window` shapes each
grain — `hann` (smooth), `triangle` (brighter joins), `expo` (a
percussive attack-decay "expodec" grain that turns a pad into a rain of
taps).

**The sprays** (slice 2) are what turn a grain *stream* into a grain
*cloud*. Each is a random amount added per grain, drawn from a stream
seeded by `seed` and keyed by the grain's index, so the cloud is exactly
reproducible — same seed, same input, same cloud, whatever the block
size. `spray_time` jitters the onsets (each interval is the hop scaled
by a random factor in 1 ± `spray_time`; 0 is the synchronous stream
with its sideband, 1 is fully asynchronous — even 0.3 smears the
sideband into a haze). `spray_pos` scatters the read point (in
`position` units, symmetric, clamped to the buffer — the last few
hundred ms of a pluck become a wash). `spray_pitch` scatters the
transposition in cents (10–30 ct is a chorus; 1200 an octave cloud).
`width` scatters each grain across `out_l` / `out_r`.

**Freeze and the scrub.** `freeze` — the param switch, or a high
`freeze` gate, either one — stops the buffer *recording* while the grains
keep *reading*: whatever was in the last `buffer` seconds is held, and
`position` becomes a scrub across it (0 = the last sample captured, 1 =
the oldest). The buffer's timeline is *captured* time, like a tape:
release the freeze and recording resumes from where it stopped, with no
hole. `position_cv` (× `position_cv_depth`, in fractions of the buffer
per CV unit) moves the read point, read once per grain at its onset — a
sequencer into it re-cuts a frozen phrase at every step, an LFO scans
the held buffer, a `shift_random` at sixteenths is a beat repeat. A
grain that was reading right at the head when a freeze landed holds its
last sample for the rest of its window; grains further back are
untouched.

**Slice 1 (2026-09-15):** capture + a synchronous, deterministic grain
stream, mono. **Slice 2 (2026-09-15):** the sprays, `seed`, stereo.
**Slice 3 (2026-09-16):** `freeze` and `position_cv` — the module is
complete against its spec.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | The signal captured into the buffer. A `(V, F)` voice source is summed — one buffer. Unpatched → silence. |
| `position_cv` | in | cv | Added to `position` × `position_cv_depth`, read at each grain's onset sample and latched for that grain. A `(V, F)` source is averaged. |
| `freeze` | in | gate | High holds the buffer (ORed with the `freeze` param). Per-sample: the freeze lands on the exact sample the gate rises, so the held content does not depend on the block size. |
| `out` | out | audio | The cloud, every grain at unity, blended with the dry input by `mix`. |
| `out_l` / `out_r` | out | audio | The cloud with each grain panned by its own draw within ±`width`; the dry stays centred. At `width` 0 both are `out`, bit-exact. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `buffer` | `2.0` | 0.5 … 10 s | Seconds of history the grains can read. Resizing keeps the history it can and drops the grains in flight (one grain's worth of dropout). |
| `density` | `25.0` | 0.5 … 100 /s | Grains per second (the mean, when `spray_time` is up). |
| `spray_time` | `0.0` | 0 … 1 | Onset jitter: each interval is the hop × a random factor in 1 ± `spray_time`. 0 synchronous, 1 asynchronous. |
| `size` | `80.0` | 10 … 500 ms | Grain length (rounded to an even sample count so 50% overlap is exact). |
| `pitch` | `0.0` | −24 … +24 st | Transposition, per grain. |
| `spray_pitch` | `0.0` | 0 … 1200 ct | Transposition scatter, ± cents about `pitch` (the sum never past ±36 st). |
| `position` | `0.0` | 0 … 1 | How far back the grains read: 0 = now, 1 = `buffer` seconds ago. |
| `spray_pos` | `0.0` | 0 … 1 | Read-point scatter, ± in `position` units, clamped to the buffer. |
| `position_cv_depth` | `1.0` | −1 … 1 | Fractions of the buffer per CV unit on `position_cv`; 1 means a 0..1 CV sweeps the whole buffer. |
| `window` | `hann` | `hann` / `triangle` / `expo` | Grain shape. |
| `width` | `0.0` | 0 … 1 | Stereo scatter: each grain panned to a random spot within ±`width` on `out_l` / `out_r`. Constant-peak law — a centred grain is at unity in both, a hard-panned one at unity in one and zero in the other. |
| `freeze` | off | on / off | Hold the buffer: stop recording, keep reading. ORed with the `freeze` gate. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. The dry is the live input two samples late — the head start every read needs — so at the neutral dry and wet line up sample for sample; it keeps playing while frozen. |
| `seed` | `1` | int | The random stream. Same seed, same input → the same cloud. |

**How it works.** Every block the input is written into the ring at
*absolute* sample indices; reads are absolute too, modulo the ring, so
nothing knows where block boundaries fall. A grain is a record of
`(onset, read start, rate, length, window, level, pan)` **frozen from
the params — plus its own four random draws — at the moment it fires**
— turning a knob reaches the *next* grain, never one in flight, which
is why there is no zipper, no click, and the render is block-size
independent to the bit. Grain *i*'s draws come from
`default_rng([seed, i])` (interval factor, position, pitch, pan), so
the cloud is a pure function of the seed and the grain index; while
every spray and `width` is zero no draws are made at all and the
arithmetic reduces to the slice-1 stream exactly. Every grain in
flight is rendered as one `(G, F)` matrix op: `k = n − onset`, `pos =
start + rate·k`, a 4-tap Hermite read of the ring at `pos` (the
[`resampler`](#resampler)'s, exact at integer positions), times the
window at `k`, masked to the grain's span, summed in spawn order —
once unpanned for `out`, and once per channel when any grain in flight
is panned.

The ring is indexed in *captured* time — a counter that advances only
while recording — while grain onsets run on wall time. Per sample,
`frozen = gate > 0.5 or the freeze param`; the input is written only at
the live samples, and a grain fired at sample *j* reads back from the
index of the last sample captured as of *j*. Live, that is the sample's
own index and everything reduces to the unfrozen stream exactly; frozen,
the head stands still, so the grain's head start becomes `rate × size`
(not `(rate − 1) × size`), and every read is clamped to the head so a
grain that was running alongside it when a freeze landed holds its last
sample instead of running into stale data.

A grain reading faster than real time (`pitch` > 0) would overtake the
write head, so `position` is floored at the head start it needs —
`(rate − 1) × size` — and if `buffer` is shorter than that the grain is
shortened to fit. Grains are scaled by `1 / max(1, density × size ×
window mean)` so a dense cloud sits at about the input's level (a DC
input through a dense hann or triangle cloud comes out at exactly 1.0);
sparse grains play at their natural level. Cost: ~1.5% of realtime at
the defaults, ~2% sprayed, ~10% at the extreme (100 grains/s × 500 ms
= 50 in flight, more when `spray_time` clumps them).

**Patching.** `pluck → granular → reverb`, `pitch` +12, `density` 30,
`size` 120, `position` 0.15, `mix` 0.6 — every pluck gets an
octave-up cloud trailing 300 ms behind it: `examples/granular_cloud.json`.
The same patch at `pitch` 0, `density` 8, `size` 40 is a stutter; at
`pitch` −12, `size` 400, `window` `expo` it is a rain of low taps; with
a [`file_player`](#file_player) in front and `position` swept by hand it
is a scrub through the last two seconds. `examples/granular_haze.json`
is the cloud proper: the same pluck into `spray_time` 1, `spray_pos`
0.22, `spray_pitch` 25 ct, `width` 1 — every note dissolves into a
stereo wash a quarter-second behind itself; turn `seed` to hear a
different cloud from the same notes. `examples/granular_freeze.json`
adds a [`key_trigger`](#key_trigger) latch on `freeze` (**tap F**) and a
0.07 Hz triangle [`lfo`](#lfo) on `position_cv`: play, tap F, and the
last two seconds hang in the air while the LFO scans them; tap again to
let go. `examples/granular_beat_repeat.json` is the drum machine into
`density` 32 × `size` 62.5 ms (exact 125 ms slices), a
[`shift_random`](#shift_random) at sixteenths on `position_cv` and a 15
BPM clock on `freeze` — two seconds recording, two seconds held and
re-cut, forever; `mix` 1 hears only the repeats. For a transposition
that *hides* the grains, use the [`pitch_shifter`](#pitch_shifter).

---

#### `rotary`

The **Leslie** — a spinning horn and a spinning drum, in stereo. The
[`organ`](#organ)'s destined partner, and the thing [`chorus`](#chorus)
and [`phaser`](#phaser) are imitations of. The signal is split at a
crossover; the treble goes to a **horn** spinning on a vertical axis,
the bass to a **drum** (a rotating baffle around a fixed woofer), and
two virtual mics hear three things from each rotor at once: a
**tremolo** (loud when the mouth points at the mic, quiet when it points
away), a **Doppler vibrato** (coming toward you, then going away — a
real moving delay, not a pitch LFO), and a **stereo swirl** (the two mics
hear those at different moments).

The two rotors are not the same machine, and that is the sound. The horn
is light and quick: ~0.7 Hz on `slow` (the *chorale*), ~6.7 Hz on `fast`
(the *tremolo*), and about a second to get between them. The drum is
heavy: it turns at 0.85× the horn, takes ~4.5 s to spin up and ~6 s to
coast down, and spins the other way. Flip `speed` at the top of a phrase
— or gate `fast` from a slow clock — and the horn is already whirling
while the bass is still gathering itself. `stop` is the brake: both
rotors coast to a halt wherever they are and the image freezes there.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | The signal. A `(V, F)` voice source is summed — a cabinet is one physical thing. Unpatched → silence. |
| `fast` | in | gate | While patched it *is* the speed switch: high = fast, low = slow (the block's majority level), and `speed` is ignored. |
| `out_l` / `out_r` | out | audio | The two mics, `spread` × 90° either side of the front. |
| `out` | out | audio | `(L + R) / 2`, bit-exact. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `speed` | `slow` | `slow` / `fast` / `stop` | Chorale, tremolo, or the brake. Ignored while `fast` is patched. |
| `slow_rate` / `fast_rate` | `0.7` / `6.7` | 0.1…3 / 2…12 Hz | The horn's rate on each setting; the drum runs at 0.85× either. |
| `ramp` | `1.0` | 0.25 … 4 | Scales every spin-up / coast-down time (horn 1 s up, 1.5 s down; drum 4.5 s up, 6 s down). |
| `depth` | `0.7` | 0 … 1 | Tremolo + Doppler intensity. 0 = the cabinet stands still. |
| `spread` | `0.7` | 0 … 1 | Mic separation: 0 is mono (L == R), 1 is 180° apart, the widest swirl. |
| `balance` | `0.0` | −1 … +1 | Drum ↔ horn tilt; −1 is drum only, +1 horn only. |
| `crossover` | `800` | 100 … 4000 Hz | Horn/drum split. 800 Hz is the classic 122. |
| `mix` | `1.0` | 0 … 1 | Dry/wet. The dry is delay-matched to the rotors' centre delay, so a blend thickens rather than combs. |

**How it works.** A Linkwitz–Riley 4th-order crossover (the
[`crossover`](#crossover) module's coefficients) splits the bands. Each
rotor has an angle that integrates a rate; the rate approaches its
target through a one-pole with separate up and down time constants, run
as an exact per-sample recurrence, so the ramp is block-size independent
and exponential (belt-driven motors accelerate that way). Per rotor and
per mic, the band is read from a delay ring at `base − (r/c)·depth·cos θ`
samples (the mouth's distance to the mic: the horn mouth is ~19 cm off
axis, so its delay swings ~0.55 ms and at 6.7 Hz that is ±2.3% of pitch,
~40 cents; the drum's 14 cm baffle less) and scaled by
`1 − am·depth·(1 − cos θ)/2` (the horn beams at 0.8, the drum at 0.45).
No feedback anywhere, so the render vectorizes and is block-size
independent. Cost ~2.3% of a block.

**Patching.** `organ → rotary`, `out_l`/`out_r` into the
[`left`](#left_speaker_output)/[`right`](#right_speaker_output)
speakers, and a slow [`clock`](#clock) (5 BPM, 50% duty) into `fast`
so the rotors chase each other every six seconds — see
`examples/organ_leslie.json`. Works on anything with harmonics:
a [`supersaw`](#supersaw) pad through a slow rotary is a different
kind of wide from the chorus. `depth` 0.3 with `spread` 0.4 is a subtle
motion; `depth` 1, `spread` 1, `balance` +0.5 is the full gospel.

---

#### `flanger`

A **swept, resonant comb** — the jet-plane whoosh. The input is mixed with
a *very short* delayed copy of itself (a comb filter: a stack of evenly-
spaced notches), and an internal LFO sweeps that delay so the notches slide
up and down the spectrum. A fraction of the delayed signal is fed back into
the line (**feedback**, or regeneration), sharpening the comb into ringing
resonances. The feedback is **bipolar**: positive rings brightly, negative
goes hollow and metallic. Where the [`chorus`](#chorus) uses a longer delay
and *no* feedback to thicken a sound, the flanger uses a shorter delay
*with* feedback for that resonant sweep — the two are close cousins. The
sweep is spread across a **stereo pair** (`out_l` / `out_r`) with the L and
R LFOs a quarter-cycle apart, for a wide, rotating image.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to flange (voice sources summed to mono). Unpatched → silence. |
| `rate_cv` | in | cv | Modulates the LFO rate (1 V/oct × `cv_depth`). Optional. |
| `out_l` | out | audio | Left channel (dry + swept comb). |
| `out_r` | out | audio | Right channel (dry + swept comb). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `rate` | `0.3` | 0.05 … 10 Hz | LFO sweep speed. Slow = a long ocean-liner sweep; faster = warble. |
| `depth` | `0.7` | 0 … 1 | Sweep width — how far the comb slides across the spectrum. |
| `manual` | `1.5` | 0.1 … 10 ms | Centre delay. Short = high/tight whoosh; long = low, hollow sweep. |
| `feedback` | `0.5` | −0.95 … 0.95 | Regeneration, **bipolar**. `0` = plain comb; `+` = ringing; `−` = hollow/metallic. |
| `mix` | `0.5` | 0 … 1 | Dry/wet. The comb is deepest near `0.5`; `0` is a bit-exact dry passthrough on both channels. |
| `cv_depth` | `1.0` | 0 … 4 oct/unit | Octaves of LFO-rate shift per unit of `rate_cv`. |
| `through_zero` | `false` | off / on | Off = the standard positive-delay flanger. On = **through-zero**: a fixed reference tap plus a moving tap swept through it for the dramatic tape "jet". |
| `polarity` | `1.0` | −1 … 1 | Through-zero only. `+1` = additive **bloom** (bright at the crossing); `−1` = subtractive **null** (cancellation hole); blended between. |

Patch the outputs into [`left_speaker_output`](#left_speaker_output) and
[`right_speaker_output`](#right_speaker_output). Unlike the chorus, the
flanger's feedback makes each output sample depend on one just written, so
the comb runs **per-sample** (the delay's short-time path) — but the LFO
phase and ring state carry across blocks, so the render is still exactly
**block-size independent** (bit-identical at 512 / 4096 / 333). This is a
**standard** (positive-delay) flanger. Switch on **`through_zero`** and it
becomes a tape-style *through-zero* flanger: a fixed reference tap plus a
moving tap swept around it, so their relative delay passes through zero
(and goes negative) each LFO crossing — the notches sweep out to infinity
and the comb flips there. The `polarity` knob picks the crossing character
(`+1` additive bloom, `−1` subtractive null). Through-zero keeps the same
block-size independence, and `mix = 0` is still a bit-exact dry passthrough.
See `examples/flanger_jet_sweep.json` (standard sweep) and
`examples/flanger_through_zero.json` (the tape jet through zero).

**Patching.** `… → vca → flanger → L/R speakers`. Try positive feedback for
a bright, ringing sweep, negative for a hollow one; feed a slow envelope or
LFO into `rate_cv` for an auto-flanger that breathes.

---

#### `phaser`

A **swept notch filter** — the whooshing, vocal sweep. The input runs
through a chain of **allpass** stages, which leave every frequency's level
untouched but rotate its phase (more toward the top of the spectrum);
summing that phase-shifted signal back with the dry input carves **notches**
wherever a frequency has been turned a half-cycle out of phase. An internal
LFO sweeps the allpass break frequency, so the notches glide up and down —
that gliding, hollow sweep is the phaser. Each *pair* of allpass stages
makes one notch, so `stages` of 4 / 6 / 8 give two / three / four notches. A
fraction of the last stage is fed back (**feedback**, bipolar) to sharpen
the notches into ringing, vocal peaks. Where the [`flanger`](#flanger)'s
notches come from a short *delay* (evenly, harmonically spaced and metallic),
the phaser's come from *allpass phase* (spread unevenly, softer and rounder)
— it is the third of the modulation trio. The sweep is spread across a
**stereo pair** (`out_l` / `out_r`) with the L and R LFOs a quarter-cycle
apart, for a wide, rotating image.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to phase (voice sources summed to mono). Unpatched → silence. |
| `rate_cv` | in | cv | Modulates the LFO rate (1 V/oct × `cv_depth`). Optional. |
| `out_l` | out | audio | Left channel (dry + swept notch chain). |
| `out_r` | out | audio | Right channel (dry + swept notch chain). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `rate` | `0.5` | 0.05 … 10 Hz | LFO sweep speed. Slow = a long breathing sweep; faster = warble. |
| `depth` | `0.6` | 0 … 1 | Sweep width, in octaves around `center` (±2 octaves at `1`). |
| `center` | `800` | 100 … 6000 Hz | Centre frequency of the notch sweep. Low = throaty; high = airy. |
| `feedback` | `0.4` | −0.95 … 0.95 | Resonance, **bipolar**. `0` = plain notches; `+` = ringing/vocal; `−` = hollow. |
| `stages` | `6` | 4 / 6 / 8 | Allpass stages = two / three / four notches. More = deeper, busier. |
| `mix` | `0.5` | 0 … 1 | Dry/wet. The notches are deepest near `0.5`; `0` is a bit-exact dry passthrough on both channels. |
| `cv_depth` | `1.0` | 0 … 4 oct/unit | Octaves of LFO-rate shift per unit of `rate_cv`. |

Patch the outputs into [`left_speaker_output`](#left_speaker_output) and
[`right_speaker_output`](#right_speaker_output). Like the flanger, the
phaser's feedback makes each output sample depend on one just written, so
the allpass cascade runs **per-sample** — but the LFO phase, the allpass
state and the feedback memory carry across blocks, so the render is still
exactly **block-size independent** (bit-identical at 512 / 4096 / 333). See
`examples/phaser_sweep.json` (a self-playing chord swept by the phaser, with
a slow LFO drifting the sweep rate through `rate_cv`).

**Patching.** `… → vca → phaser → L/R speakers`. Raise `feedback` for a
resonant, vocal sweep and `stages` for a deeper one; feed a slow envelope or
LFO into `rate_cv` for an auto-phaser that breathes. For the harder,
metallic jet-sweep reach for its sibling the [`flanger`](#flanger).

#### `vocoder`

A **channel vocoder** — one signal wears the spectral shape of another (the
classic robot voice). The **modulator** (`mod`, usually a voice from
[`mic_input`](#mic_input) or [`file_player`](#file_player)) and the
**carrier** (`carrier`, usually a synth — a fat saw chord, [`noise`](#noise),
strings) are each split into the same `bands` log-spaced bands by two
matched banks of bandpass filters. Per band, an **envelope follower**
measures the modulator's level and that level becomes the gain of the
carrier's matching band; the bands are summed and the carrier "talks".
Speech intelligibility lives in those slow band envelopes, not in the
voice's pitch — the output's pitch is the **carrier's** (play a chord and
the voice speaks in harmony). Consonants (*s*, *t*, *k*) are noise bursts
the bands can't see, so a dedicated **hiss** path — a high-band follower
above the band range gating filtered noise — rides them into the output.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `mod` | in | audio | The modulator — the voice whose spectral envelope is measured (voice sources summed to mono). Unpatched → the bands all close. |
| `carrier` | in | audio | The carrier — the instrument being shaped (voice sources summed to mono). Unpatched → silence. |
| `out` | out | audio | The vocoded result, mono. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `bands` | `16` | 8 / 12 / 16 / 24 | Band count. Fewer = lo-fi robot; more = clearer speech. |
| `freq_lo` | `120` | 50 … 500 Hz | Centre of the lowest band. |
| `freq_hi` | `7500` | 2000 … 12000 Hz | Centre of the highest band (band centres are log-spaced between the two). |
| `width` | `1.0` | 0.3 … 3 | Every band's bandwidth, as a multiple of the adjacent-band spacing. Narrow = precise/robotic; wide = smeared/soft. |
| `attack` | `4` | 0.1 … 100 ms | Follower attack — fast catches consonant onsets. |
| `release` | `60` | 1 … 500 ms | Follower release — long blurs words into a pad-like wash. |
| `hiss` | `0.4` | 0 … 1 | Sibilance/noise path level. Raise until *s* and *t* cut through. |
| `gain` | `1.0` | 0 … 4 | Wet-path makeup gain (never touches the dry carrier). |
| `mix` | `1.0` | 0 … 1 | Dry carrier ↔ vocoded. `0` is a bit-exact carrier passthrough; normally played fully wet. |

All filter memory (two DF-I band banks + the two sibilance highpasses), the
follower levels and the noise generator's stream position carry across
blocks, so the render is exactly **block-size independent** (bit-identical
at 512 / 4096 / 160). The follower bank — all bands plus the sibilance row —
runs as **one** call to the [`audio_to_cv`](#audio_to_cv) monotone-pattern
block solve. See `examples/vocoder_robot_choir.json` (mic → `mod`, two
detuned saws → [`combiner`](#combiner) → `carrier` — speak and the chord
talks; use headphones with an open mic).

**Patching.** `mic → vocoder.mod`, `saws/chord → vocoder.carrier`,
`vocoder → speaker`. A drum loop into `mod` gates a pad rhythmically;
[`noise`](#noise) as the carrier whispers; long `release` + wide `width`
turns speech into a droning vowel pad.

---

### Modulation

Sources of control voltage that shape other modules over time.

#### `adsr`

A classic Attack–Decay–Sustain–Release envelope. A gate going high starts the
attack; going low starts the release. Outputs a `cv` contour, usually wired to
a [VCA](#vca)'s `cv` (for volume) or a [Filter](#filter)'s `cutoff_cv`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `gate` | in | gate | Note on/off. Rising edge → attack; falling edge → release. |
| `vel` | in | cv | Velocity: a multiplier on the whole envelope of a note, **read at the gate's rising-edge sample and latched for that note**. `max(0, cv)`; unpatched = 1.0. |
| `cv` | out | cv | The envelope, 0…1 (sustain level held while the gate is high) — times `vel`. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `attack` | `0.01` | 0…5 s | Time to rise from 0 to 1 after gate-on. |
| `decay` | `0.1` | 0…5 s | Time to fall from 1 to the sustain level. |
| `sustain` | `0.7` | 0…1 | Level held while the gate stays high. |
| `release` | `0.3` | 0…5 s | Time to fall from sustain to 0 after gate-off. |

**Velocity.** `vel` is the envelope's velocity input: `cv = shape × vel`, the
way a velocity-sensitive VCA sits after an analog envelope, so the attack
peak, the sustain plateau and the release tail of a note all scale
together. It is a knobless multiplier like `vca.cv` (the CV *is* the
amount), and it follows the house edge-latch rule the drums and the
[`sampler`](#sampler) use: the bus is sampled **at the gate's rising-edge
sample** and held for that note — a velocity that moves mid-note changes
nothing until the next key-down, so a `sample_hold`ed noise or a
[`shift_random`](#shift_random) clocked alongside the sequencer gives each
step its own accent. Negative values clamp to 0 (a silent note); values
above 1 are honoured. Timing does not change with velocity: a soft note's
attack still takes `attack` seconds to reach its lower peak. The one place
velocity meets the retrigger rule (attack from the current level, no click)
is a note re-struck *softer* than it is currently ringing — rather than
jump down to the new peak, the level falls to it at the full-velocity
attack slope and then decays as usual, so a velocity of 0 on a ringing
note fades it out over one attack time. Voice-aware like the gate: a
`(V, F)` `vel` (`midi_input.velocity_cv` is exactly that) latches per
voice from its own row, a mono `vel` is shared by every voice, and a
`(V, F)` `vel` on a mono gate collapses to the loudest voice at the edge,
as the drums do. Unpatched it is exactly 1.0 and the envelope is
bit-for-bit what it was before the input existed.

**Patching.** `keyboard.gate → adsr.gate`, then `adsr.cv → vca.cv`. See
`examples/keyboard_adsr.json`, `examples/filter_envelope.json`. For
velocity: `midi_input.gate → adsr.gate` + `midi_input.velocity_cv →
adsr.vel`, or the sequenced accents of `examples/adsr_velocity.json`.

#### `ad_envelope`

A trigger-style **Attack–Decay** envelope for percussion and plucks. A trigger fires it and it plays a full A→D contour on its own, **ignoring how long the trigger is held** — so a momentary clock pulse gives every hit the same snappy shape, with no sustain stage holding the tail open. For held notes with a sustain, use [adsr](#adsr) instead.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `trig` | in | gate | Trigger. A **rising edge** (re)starts the envelope; the trigger's length is ignored. |
| `cv` | out | cv | The envelope, 0…1. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `attack` | `0.005` | 0…5 s | Time to rise from the current level to 1. |
| `decay` | `0.20` | 0…5 s | Time to fall from 1 back to 0. |

**How it works.** Rising edge → attack from the current level (a retrigger mid-decay picks up where it was, no click) → decay to 0 → idle. The trigger going low does nothing; the decay always completes. Shape-polymorphic like [adsr](#adsr): a `(V, F)` trigger drives V independent envelopes, bit-identical to the mono path per voice.

**Patching.** `lfo → schmitt → ad_envelope.trig`, then `ad_envelope.cv → vca.cv` for a self-playing drum, or a keyboard/MIDI `gate → trig`. See `examples/ad_kick.json` (a clocked sine kick).

#### `function_generator`

The **rise/fall function** — the west-coast module that is an envelope, an LFO, a clock or a gate delay depending only on how you cable it. A ramp up over `rise` seconds and down over `fall` seconds, with a `curve` knob that bends both slopes, and — the part that matters — two gate outs that announce **end of rise** (`eor`) and **end of cycle** (`eoc`). Patch `eoc` back into `trig` and it re-fires itself: the "krell" patch, a machine that plays itself forever.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `trig` | in | gate | Start / sustain / sync — what it means depends on `mode`. Unpatched, only `loop` mode does anything. |
| `rate_cv` | in | cv | **1 V/oct on the rate**: +1 = twice as fast, −1 = half speed. Scales `rise` and `fall` together, clamped to ±5 octaves. Block-mean, like the [lfo](#lfo)'s. |
| `rise_cv` | in | cv | (2026-09-14) The same 1 V/oct law on the **rise only**. Sums in octaves with `rate_cv`; the per-slope total is what gets clamped to ±5. Maths' per-channel jack next to its "both". Block-mean. **Per voice** when it arrives as `(V, F)` alongside a `(V, F)` trigger: each slot gets its own stage lengths (and pulse width). A mono CV, or a voice CV into a mono trigger, is one law for every slot (summed across voices, as any mono module sees a voice source). |
| `fall_cv` | in | cv | Likewise for the **fall only**. `midi_input.velocity_cv → [cv_scale](#cv_scale) → fall_cv` is the classic — hard hits ring longer (or shorter; flip the sign) — and because it is per voice, every note in a chord keeps its own ring. |
| `out` | out | cv | The function, 0…1. |
| `out_inv` | out | cv | (2026-09-14) **`1 − out`**, bit-exact: sits at 1, dips to 0 at `eor`, climbs back to 1 by `eoc`; 1.0 at rest and on a parked voice. Into a [vca](#vca)'s `cv` it is a **ducker** keyed off whatever is on `trig` — sidechain compression without a compressor. |
| `eor` | out | gate | ~2 ms pulse the moment the rise completes. |
| `eoc` | out | gate | ~2 ms pulse the moment the fall completes. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `mode` | `trigger` | `trigger` / `gate` / `loop` | What the trigger means. **trigger** = fire and forget: a rising edge starts the rise and the whole shape plays out, gate length ignored. **gate** = rise, **hold at 1.0** while the gate stays high, fall on release (an AR/ASR). **loop** = free-running; it cycles with nothing patched, and `trig` becomes a click-free **sync reset**. |
| `rise` | `0.05` | 0…10 s | Time from 0 to 1. 0 = instant. |
| `fall` | `0.5` | 0…10 s | Time from 1 back to 0. 0 = instant. |
| `curve` | `0.0` | −1…+1 | Slope shape, applied to both slopes. **0** = straight lines. **Positive = exponential**: a rise that starts slow and accelerates, and (the same curve mirrored) a fall that drops fast then trails away — the natural, percussive shape. **Negative = logarithmic**: a rise that leaps then eases into the top, and a fall that lingers before dropping — the swelling, orchestral shape. |
| `curve_rise` | `0.0` | −1…+1 | One knob per slope (2026-09-12): added to `curve` for the **rise** only, the sum clamped to ±1. 0 leaves `curve` in charge. |
| `curve_fall` | `0.0` | −1…+1 | Likewise for the **fall**. `curve_rise` + and `curve_fall` − is the pluck-then-linger; the reverse is a swell that drops out. With the two effective exponents different the fall is no longer the rise mirrored — by design. Every stage entry solves backwards with the exponent of the stage being *entered*, so retriggers and releases stay click-free when the slopes disagree. |

**How it works.** State machine `idle → rise → [hold] → fall → idle`, with `loop` wrapping the fall straight back into the rise instead of idling. Stage progress is an **integer sample counter** against an integer stage length, not an accumulated float step, so a stage is exactly `round(t × sr)` samples long and a loop-mode clock never drifts. Stage *entry* solves the curve backwards for the position matching the current output level, so every retrigger and every mid-rise gate release starts from where the output already is — click-free anywhere in the cycle. Shape-polymorphic like [adsr](#adsr)/[ad_envelope](#ad_envelope): a `(V, F)` trigger runs V independent functions. `rate_cv` is always collapsed to mono (one tempo, as the hardware "both" jack is); `rise_cv` / `fall_cv` are collapsed *unless* they arrive `(V, F)` against a `(V, F)` trigger with the same V, in which case each slot gets its own `(rise_len, fall_len, pulse_len)` from its own row's block-mean — the same arithmetic as the mono law, applied per slot, so a voice row is bit-identical to the mono path fed that row's value. `out_inv` is `1 − out` taken on the finished block, outside the kernel, so it costs one subtraction and leaves the scalar loop untouched. Both shapes drive the *same* scalar kernel, so a voice row is bit-identical to the mono result by construction; a parked slot skips the loop entirely. Cost against one block's budget: ~2.3% mono, ~8.9% at four sounding voices, ~34.6% at all sixteen, ~0.2% idle.

**Compared to its neighbours.** [ad_envelope](#ad_envelope) is the same fire-and-forget idea with a linear curve and no gate outs — reach for it when that is all you need. [adsr](#adsr) has a real sustain *level*; `gate` mode here holds at full, so it is a two-knob envelope with a hold rather than a four-knob one. [lfo](#lfo) gives you waveshapes and a rate in Hz; `loop` mode here gives you a triangle whose up and down slopes are set independently in seconds, which an LFO cannot do. [slew](#slew) shapes a signal you feed it; this generates its own.

**Patching.**

* **Envelope** — `keyboard.gate → trig` (`mode` `gate`), `out → vca.cv`. Positive `curve` for plucks, negative for swells.
* **Lopsided LFO** — `mode` `loop`, `rise` 2 s, `fall` 0.05 s, `out → filter.cutoff_cv`: a slow swell with an instant reset, which is a saw no LFO knob gives you.
* **Clock** — `mode` `loop`, `eoc → sequencer.clock`. The period is `rise + fall`, and `rate_cv` becomes a tempo CV.
* **Gate delay** — `mode` `trigger`, `trig` from a clock, `eoc` out: the same trigger, `rise + fall` seconds later. Chain two for a rhythm.
* **Ducker** — `mode` `trigger`, `trig` from the kick's clock, `out_inv → vca.cv` on the pad: the pad drops out on every kick and swells back over `fall`. Sidechain compression with no compressor and no detector lag — the duck lands *on* the sample the trigger does.
* **Velocity-dependent decay** — `midi_input.velocity_cv → cv_scale → fall_cv` with `rate_cv` left for tempo: hard hits ring longer, the rise stays put, and each note in a chord keeps its own ring (`velocity_cv` is per voice and so is `fall_cv`).
* **Wandering clock, fixed swing** — `mode` `loop`, an [lfo](#lfo) into `rise_cv` only: the period wanders while `eoc` stays a fixed `fall` after every `eor`.
* **Krell** — `mode` `loop` with something wandering into `rate_cv` (an [lfo](#lfo) on its `random` waveform, a [shift_random](#shift_random)), and `eoc → sample_hold.trig` so every cycle grabs a fresh pitch: a self-playing generative machine whose pace never repeats. See `examples/krell_machine.json`.

> **On self-patching `eoc → trig`.** On hardware that is *the* krell patch, and since 2026-09-16 it works here too: any cable that closes a loop is read one block late (see *Feedback loops* under Cabling rules). `trig` takes one cable and the loop needs one poke to begin, so OR the returning `eoc` with a short starter pulse through a [`logic`](#logic) module — `examples/krell_feedback.json` is exactly that, and its period is the cycle plus one block. `loop` mode is the same machine with no latency and no starter, which is why it exists; `examples/krell_machine.json` uses it.

> **What `eor` is for.** Firing things **at the peak**: `eor → `[`pluck`](#pluck)`.trigger` strikes a note at the top of a swell, `eor → `[`sample_hold`](#sample_hold)`.trig` grabs a voltage at the loudest moment, `eor` into a *second* function generator's `trig` chains an attack into another shape (a four-stage envelope from two modules), and `eor` into a [`sequencer`](#sequencer)'s clock is `eoc`'s clock shifted by the rise time. `examples/fg_eor_swell_strike.json` is the first two: a swelling drone (the fg on `cutoff_cv` and a VCA, cycling through the krell loop) whose every peak fires a pentatonic pluck. **One patch that does not work**, and is worth knowing why: `eor` back into the *same* module's `trig`. The pulse arrives a block after the top, a few milliseconds into the fall; a retrigger there restarts a rise from ~0.99 that finishes in a couple of milliseconds and fires `eor` again, so the output pins at the top with a block-rate ripple — and at small block sizes the 2 ms pulse straddles two blocks, the edge never re-arms, and the loop dies. A latch, not a cycle (pinned by a test). The krell is `eoc`, always.

#### `clock`

The rack's **metronome**: a tempo turned into a steady gate pulse train. No
input, no audio — it free-runs while the transport plays and emits a pulse on
`out` that other modules step off (most obviously a [sequencer](#sequencer)'s
`clock`, but equally an [adsr](#adsr)/[ad_envelope](#ad_envelope) trigger or a
[sample_hold](#sample_hold) `trig`).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `out` | out | gate | Pulse train at `bpm / 60 × division` Hz. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `bpm` | `120.0` | 20…300 | Tempo in beats per minute. |
| `division` | `4.0` | 0.25…16 | Pulses per beat — 1 = quarter, 2 = eighth, 4 = sixteenth notes. |
| `pulse_width` | `0.5` | 0.01…0.99 | Duty cycle (fraction of each period the gate is high). |

**How it works.** A float64 phase accumulator carries across blocks so pulses stay phase-continuous (no drift, no seam). A fresh clock emits a rising edge on its first sample, so a downstream sequencer plays step 1 immediately.

**Patching.** `clock.out → sequencer.clock`. See `examples/sequencer_melody.json`.

#### `sequencer`

A clock-driven **step sequencer** — the self-playing centrepiece. On each `clock` pulse it advances one step (up to 16) and emits that step's pitch as a **1V/octave** `cv` plus a `gate` that fires on enabled steps. Wire `cv → oscillator.freq_cv` (osc base `freq` = C4 = 261.6256 Hz to play in tune) and `gate → adsr → vca` and the patch plays a melody by itself.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `clock` | in | gate | Advance one step on each **rising edge**. First pulse plays step 1. |
| `reset` | in | gate | A rising edge rewinds so the next clock plays step 1. Unpatched = free-running loop. |
| `cv` | out | cv | Current step's pitch as 1V/oct (`semitones / 12`, C4 = 0 V), **held** for the whole step (sample-and-hold) so a note stays in tune while its envelope rings out. |
| `gate` | out | gate | High while the clock is high **and** the current step is enabled. A disabled step is a rest. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `steps` | `8` | 1…16 | Active loop length; the sequence wraps back to step 1 after this many steps. |
| `step{i}_pitch` | C-major scale | −24…24 st | Pitch of step *i* in semitones (i = 1…16). Default is an ascending C-major scale on the first 8 steps. |
| `step{i}_on` | `true` | bool | Whether step *i* fires its gate. `false` = a rest (the step still consumes a clock tick). |

**Patching.** `clock.out → sequencer.clock`; `sequencer.cv → oscillator.freq_cv`; `sequencer.gate → adsr.gate → vca.cv`; `oscillator.out → vca.audio → speaker`. See `examples/sequencer_melody.json`. The `cv` is generic 1V/oct — patch it into a filter `cutoff_cv` or any CV input for stepped modulation instead of pitch.

#### `fader_seq`

The [`sequencer`](#sequencer) with a hardware-style **fader-bank panel** —
same engine, same ports, same params, different front. Instead of 33
labelled parameter rows, the node draws sixteen **vertical pitch faders**
side by side (Korg SQ-10 lineage) with nothing beneath each but its step
number and an on/off tickbox; hover a fader for its note (`+7 st (G4)`).
One labelled `steps` slider sets the loop length. The melody is readable
at a glance — the fader heights *are* the tune.

Faders are quantized to **integer semitones over ±12**; the shared engine
accepts any float, so a hand-edited patch JSON can still go microtonal or
beyond the panel range (the slider only clamps what the mouse does).
Everything else — stepping, rests, reset, sample-and-hold `cv`, wrap at
`steps` — behaves exactly as documented on [`sequencer`](#sequencer), and
is pinned by a bit-identical A/B test. Pick whichever panel suits the
patch; saved patches remember which one they used.

#### `shift_random`

A **looping shift-register random CV** — the generative classic. A 16-bit
register rotates one place per rising `clock` edge; the bit falling off
the loop end (position `length`) recirculates to the front, and with
`probability` it flips on the way. The first eight bits — newest as MSB —
read as a byte give `cv` (`byte/255 × range`, or centred ±`range` with
`bipolar`); `gate` mirrors bit 0, held between clocks — a free rhythm
line that mutates in lockstep with the melody.

`probability` is the money knob: **0 is a locked loop** (the same
`length`-step pattern forever — a found melody), **1 is a coin flip per
step** (pure random), and ≈0.1 keeps a motif recognisable while it slowly
walks somewhere new. A `write` gate held high forces the incoming bit to
1 — a performance "seed it now" handle. All randomness draws from `seed`:
the register's initial fill and every flip are deterministic per seed
(patches recall their character; renders are testable), and changing
`seed` re-rolls the register live. Shortening `length` and lengthening it
again recovers the old tail — the register always keeps 16 bits of
history, the hardware behaviour. Mono, stepped outputs (follow with
[`slew`](#slew) for glide, [`quantizer`](#quantizer) for melody). See
`examples/shift_random_melody.json` — the endless melody box.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `clock` | in | gate | Rotate one step per rising edge. Unpatched → holds. |
| `write` | in | gate | While high, the incoming bit is forced to 1. Optional. |
| `cv` | out | cv | Register byte scaled to `range`, held between clocks. |
| `gate` | out | gate | Register bit 0, held between clocks. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `probability` | `0.1` | 0 … 1 | Chance the recirculating bit flips per clock. 0 = locked loop, 1 = coin flips. |
| `length` | `8` | 2 … 16 | Loop length in clocks. |
| `range` | `2.0` | 0 … 5 | CV span: 0..range unipolar, ±range bipolar. |
| `bipolar` | `False` | bool | Centre the CV on 0. |
| `seed` | `1` | ≥ 0 | Deterministic character; change to re-roll live. |

#### `possibility_seq`

A clock-driven step sequencer **whose steps can be undecided** — the bridge
module from [PythonBinaryPossibility](https://github.com/MatthewCarven/PythonBinaryPossibility),
its possibility-rack step semantics ported to this rack. Each of up to 16
steps is `"0"` (a rest), `"1"` (a hit), or `"?"` — *both, until the music
needs an answer*. A pattern with k undecided steps holds 2^k distinct bars;
this module plays one per pass and draws fresh ones on demand. Where
[`sequencer`](#sequencer) covers decided patterns and
[`bernoulli_gate`](#bernoulli_gate) covers the all-random single stream,
this is the pattern with holes in it. Its sibling
[`possibility_selector`](#possibility_selector) is the same register one
level up — steps that say *which* output fires rather than *whether*.

`mode` says **when the `?`s decide**: `loop` (default) redraws every `?`
each time the pattern wraps — every bar a fresh take from the same
possibility space; `latch` resolves once and holds the take until a
`reroll` edge or a `seed` change — a bar you *found* and get to keep;
`dice` rolls every `?` fresh each time it comes around.

`balanced` is the measured switch: independent coins clump (in the source
project, one fair 16-step bar in fifteen lands audibly lopsided), so with
`balanced` on, the *fair* `?`s (odds 0.5) are dealt from a width-1
shuffle-bag instead — every pair of fair steps gets exactly one hit and
every bar lands on its share, the same reason Tetris deals pieces from a
bag. Weighted steps keep their own odds either way (a 0.2 ghost note's
whole point is its rarity and its clumping). In `dice` mode the bag deals
through *time* instead — consecutive fair rolls pair up.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `clock` | in | gate | Advance one step on each **rising edge**. First pulse plays step 1. |
| `reset` | in | gate | A rising edge rewinds so the next clock plays step 1. The current take is kept. |
| `reroll` | in | gate | A rising edge draws a fresh take — every `?` still to come re-resolves. Patch a slow clock here and the pattern re-decides itself every few bars. |
| `gate` | out | gate | High while the clock is high **and** the current step fires — the [`sequencer`](#sequencer) contract, so pulse width follows the clock's. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `steps` | `16` | 1…16 | Active loop length; the pattern wraps after this many steps. |
| `mode` | `loop` | loop / latch / dice | When the `?`s decide (see above). |
| `balanced` | `false` | bool | Deal fair `?`s from the shuffle-bag — every bar exactly its share. |
| `seed` | `1` | ≥ 0 | All randomness; the sequence of takes is a pure function of the seed and the clock, so patches reload with the character they were saved with. |
| `step{i}_state` | `1?101?101?101?10` | "0" / "1" / "?" | The pattern (i = 1…16). The default ships four `?`s — 16 possible bars out of the box. |
| `step{i}_p` | `0.5` | 0…1 | Odds step *i*'s `?` fires. 0.5 = fair coin (bag-eligible under `balanced`), 0.2 = a ghost note in a fifth of takes. |

**The panel.** The node draws its own face rather than 36 labelled
parameter rows: sixteen **step cells** side by side, each showing its state
as `0`, `1` or `?` and colour-coded (hit bright, rest dark, undecided
amber — the `?`s are what the module is *for*, so they catch the eye
first). The gesture is one click: **click a cell to cycle `0 → 1 → ? →
0`**, the same rule as the source project's rack. **Right-click a cell**
for that step's odds — a `fires` slider from 0 to 1; the value is kept
whatever state the cell is in, so odds you dial now survive the cycle back
round to `?`. Hover a cell for what it does in words (`step 3: undecided —
fires 20% of takes`). Above the cells sit `steps`, `mode`, `balanced` and
`seed`; beneath them the **possibility readout** — `4 ? -> 16 possible
bars` — which is the number the module is really about. Steps past `steps`
grey out: parked, not played, and they don't widen the space.

**Patching.** `clock.out → possibility_seq.clock`; `possibility_seq.gate →
kick_drum.trigger` (or any gate consumer). Three of them off one clock into
[`kick_drum`](#kick_drum) / [`snare_drum`](#snare_drum) /
[`hat_drum`](#hat_drum) → [`mixer`](#mixer) is a drum machine that is
genuinely undecided — see `examples/possibility_groove.json`. A slow
[`clock`](#clock) into `reroll` re-deals a `latch`ed pattern every N bars —
or, tighter, a [`clock_divider`](#clock_divider) off the *same* sixteenth
clock, so the re-deal lands exactly on a downbeat:
`examples/possibility_reroll_divider.json` holds a latched kick and snare
for four bars (`divn` 16 = a bar, then `div4`) and re-deals the hat every
bar.

#### `possibility_selector`

A clocked **1-to-4 gate router whose routes can be undecided** — the second
brick of the possibility bridge, and the *meta* one.
[`possibility_seq`](#possibility_seq) is a register of bits with holes in
it: each step says *whether* something fires. This is a register of
**routes** with holes in it: each step sends the incoming gate to `out1`,
`out2`, `out3` or `out4` — *which* module fires — or is `?`, undecided
among them until the music needs an answer. Patch a clock into `in` and
the four outputs into a [`kick_drum`](#kick_drum), a
[`snare_drum`](#snare_drum), a [`hat_drum`](#hat_drum) and a fourth voice,
and every tick fires *some* drum; which one is the undecided bit. The
router itself collapses.

Each step's state is **the set of outputs it may go to**: `"1"`…`"4"` is
decided (that output, every take); `"?"` is open among all four; a subset
is written as its digits — `"13"` is *kick or hat, never snare*, `"234"`
is *anything but the kick*; `"0"` is a rest. A pattern with k open steps
holds the product of their choices — up to 4^k — distinct bars, and the
panel counts them.

`mode` is exactly [`possibility_seq`](#possibility_seq)'s: `loop` redraws
the open steps every wrap, `latch` holds one take until a `reroll` edge or
a `seed` change, `dice` rolls fresh every time round.
`weight1`…`weight4` say **how the open steps lean** — an open step draws
among its candidates in proportion to their weights, so `weight4` = 0.25
makes the fourth voice a rare guest and `weight2` = 0 takes the snare out
of every `?` without touching a decided `"2"`; all equal is a fair draw.
`balanced` is the clumping fix generalised from a coin to a hand of cards:
a *fair* open step is dealt from a shuffle-bag — the output dealt least so
far among its candidates, ties broken by the die — so a bar of `?`s spreads
across the outputs instead of landing three kicks in a row. (In
PythonBinaryPossibility that least-used draw is called *the selector*,
hence the name.)

**Two stepping contracts**, so one cable is enough either way. With
`clock` patched it is the sequencer reading: step *k*'s route applies to
clock tick *k* and `in` only says whether anything passes — a
[`possibility_seq`](#possibility_seq) `gate` into `in` makes a *whether* ×
*which* kit. With `clock` unpatched it is the distributor reading: `in`'s
own rising edges step the register, so route *k* applies to the *k*-th
hit whenever it arrives — four [`pluck`](#pluck)s tuned to a chord on the
outputs and a sparse gate into `in` is a harp whose string order is a
register you can leave holes in. With `in` unpatched the clock itself is
routed.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | gate | The gate to route. |
| `clock` | in | gate | Advance one step per **rising edge**. Unpatched, `in`'s own edges step the register. |
| `reset` | in | gate | A rising edge rewinds so the next step is step 1. The current take is kept. |
| `reroll` | in | gate | A rising edge draws a fresh take — every open step still to come re-resolves. |
| `out1`…`out4` | out | gate | High while `in` is high **and** the current step routes there — the [`sequencer`](#sequencer) contract, so pulse width follows the input's. Nothing passes before the first step. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `steps` | `16` | 1…16 | Active loop length; the register wraps after this many steps. |
| `mode` | `loop` | loop / latch / dice | When the open steps decide (see above). |
| `balanced` | `false` | bool | Deal fair open steps from the least-used-output bag. |
| `seed` | `1` | ≥ 0 | All randomness; the sequence of takes is a pure function of the seed and the edge history. |
| `weight{k}` | `1.0` | 0…1 | How the open steps lean towards `out{k}`. Decided steps ignore it. |
| `step{i}_state` | `1?3?2?3?1?3?2?3?` | "0" / "1"–"4" / "?" / digits | The register (i = 1…16). The default ships eight open steps — 65,536 possible bars. |

**The panel.** The [`possibility_seq`](#possibility_seq) face one level up:
sixteen **step cells**, each coloured by its route (one hue per output,
grey for a rest, the family's amber for anything still open) and labelled
`0`, `1`–`4`, `?` or the subset's digits. **Click a cell to cycle
`0 → 1 → 2 → 3 → 4 → ? → 0`**; **right-click** for four checkboxes — tick
the outputs the step may go to (all four is a plain `?`, one is a decided
step, none is a rest). Hover for what the step does in words. Above the
cells sit `steps`, `mode`, `balanced`, `seed` and the four `? leans to`
sliders; beneath them the **possibility readout** — `8 ? -> 65,536
possible bars`. Steps past `steps` grey out.

**Patching.** `possibility_seq.gate → possibility_selector.in` with the
same `clock` on both (whether × which), `out1..out4 → kick / snare /
closed hat / open hat`; or a sparse gate into `in` alone and four plucks
on the outputs. `examples/possibility_selector_kit.json` is both at once.

#### `drift`

A **smooth wandering random CV**. The rack's other random sources all
have corners or a plan: the [`lfo`](#lfo)'s `random` steps,
[`sample_hold`](#sample_hold) steps, [`shift_random`](#shift_random)
loops, [`chaos`](#chaos) orbits an attractor with perfect determinism.
This one *stumbles*: a random voltage that wanders smoothly from value to
value — Buchla's fluctuating random voltage, the Wogglebug's smooth out —
for filter cutoffs that breathe, pitches that drift like an old
oscillator, pans that never sit still.

Every `1/rate` seconds a new target is drawn and the output travels to it
along a half-cosine over `glide` of the interval, then holds. `mode`
picks how the target is drawn: **`smooth`** — a fresh uniform draw across
the whole range each time (the classic fluctuating random: it goes
anywhere, at a walking pace); **`walk`** — the previous target plus a
gaussian step of `step`, reflected off the edges (a random walk: it goes
*somewhere near*, and keeps going — the drift of a thing with no home
value). `glide` is the travel fraction: 0 is a plain sample-and-hold, 1 is
one continuous curve with no flat spots, 0.3 arrives early and waits. A
tick that lands mid-glide starts the next curve from wherever the value
is, so nothing ever jumps — including under a `rate_cv` sweep.

**Three outputs off the same die.** `cv` is the smooth wander; `stepped`
is the held targets — the S&H twin, so a filter can drift while a
[`quantizer`](#quantizer)'d pitch steps to the very same values; `trig`
is a 1 ms pulse on every new value — a random clock at the same pace,
which fires a [`pluck`](#pluck) exactly when the wander turns a corner.

`rate_cv` bends the rate in octaves (`cv_depth` per unit, block mean;
mono — a polyphonic source is averaged). Patch a [`clock`](#clock) and
the draws happen on its rising edges instead of the internal rate, the
glide sized from the measured beat (a jump until one exists).

All randomness comes from `seed`, and the tick schedule is an integer
sample count rather than a float phase, so a render is **bit-identical
at any block size** (pinned at 64 vs 2048) and patches reload with the
wander they were saved with. Numpy backend only; silent stub under pyo.
See `examples/drift_wander.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `rate_cv` | in | cv | Rate in octaves per unit × `cv_depth` (block mean). |
| `clock` | in | gate | Optional: a new value on every rising edge instead of the internal rate. |
| `cv` | out | cv | The smooth wander. |
| `stepped` | out | cv | The targets, held (the S&H twin). |
| `trig` | out | gate | A 1 ms pulse on every new value. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `mode` | `smooth` | smooth / walk | Fresh uniform targets, or a reflected random walk. |
| `rate` | `0.5` | 0.02 … 50 Hz | New values per second. |
| `glide` | `1.0` | 0 … 1 | Fraction of each interval spent travelling; 0 = stepped. |
| `step` | `0.25` | 0 … 1 | `walk` only: the gaussian step's size on the ±1 scale. |
| `depth` | `1.0` | 0 … 1 | Output amplitude. |
| `bipolar` | `true` | bool | ±`depth`, or 0 … `depth`. |
| `cv_depth` | `1.0` | 0 … 4 | Octaves per unit on `rate_cv`. |
| `seed` | `1` | ≥ 0 | All randomness. |

#### `chaos`

A **strange-attractor CV source** — deterministic wandering with
*structure*. [`shift_random`](#shift_random) loops, the
[`lfo`](#lfo)'s `random` steps; chaos **orbits**: the same shape never
repeats, yet the module is fully deterministic (same `seed`, same
render, forever — its only randomness is the seeded initial-condition
jitter). Two systems: `lorenz` (σ=10, ρ=28, β=8/3 — the butterfly) and
`rossler` (a=b=0.2, c=5.7 — spiral-and-kick).

The three CV outs `x`/`y`/`z` are three views of **one orbit**, so one
module drives several destinations *coherently* — the cutoff swell
arrives with the pitch wander that caused it. The `gate` out is the
attractor's own rhythm, different per system: **lorenz** emits the
sign of x (a quasi-random square that hangs on each wing for an
unpredictable while), **rossler** emits z-above-threshold (sparse
irregular spike bursts). Integration is RK4 at a rail-limited substep
on a 16-sample control grid keyed to the absolute sample count, then
linearly interpolated — CV-smooth, cheap, and **bit-exact at any
block size**. Outputs are normalised by the attractor's known bounds,
clipped, then mapped by `range`/`bipolar`. A `reset` edge returns the
orbit to its seeded start, sample-accurate.

The demo: patch `x`/`y` into a [`scope`](#scope) in **xy** mode — the
butterfly, live on the node. Three settings stand between "looks
broken" and the textbook picture, so set them all: chaos's outs are
**cv** and the scope's traces are **audio**, so each needs a
[`cv_to_audio`](#cv_to_audio) bridge on the way; the scope's window is
`time_div` × 10 divisions, so wind `time_div` to **500 ms/div** (the
default 10 ms/div is a *tenth of one orbit* at `rate` 1.0 — an arc, not
a loop); and drop `range` to **1.00** (or the scope's `gain` to 0.50),
because the face clamps at ±1 and a ±2 orbit flattens against the
edges. One wing is the normal result — lobe switches are irregular — so
for both at once raise `rate` to ~2–3 and fit more orbits in the window.
Or `x` → [`quantizer`](#quantizer) →
[`oscillator`](#oscillator) for a melody that never repeats and
recalls exactly by seed — see `examples/chaos_melody.json`.

**Ports**: `reset` (gate) → `x`,`y`,`z` (cv), `gate` (gate).

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `system` | `lorenz` | lorenz / rossler | Which attractor. |
| `rate` | `1.0` | 0.01 … 50 | Speed, calibrated to ≈ orbits per second (approximate by nature). |
| `range` | `2.0` | 0 … 5 | CV span: 0..range unipolar, ±range bipolar. |
| `bipolar` | `False` | bool | Centre the CVs on 0. |
| `seed` | `1` | ≥ 0 | The orbit's identity; change to re-roll live. |

#### `euclidean`

The **Euclidean rhythm generator**: `fills` hits distributed as evenly
as possible across `steps` clock ticks — the arithmetic Bjorklund
pattern `hit[i] = ((i+rotate)·fills) mod steps < fills` (no recursion;
E(3,8) is the tresillo `10010010` verbatim). `rotate` walks the pattern;
`reset` realigns so the next clock plays step 1. The gate holds each hit
for `gate_len` of a step (step length measured from the incoming clock;
until two edges have been seen it mirrors the clock's high time).
`accent` is a second, sparser Euclidean layer (`accent_fills`)
**intersected with the main pattern** — accents always land on hits;
patch it to a VCA boost or a second envelope. Feed the drum voices:
E(3,8) into a kick over a 16-step grid is instant Afro-Cuban.
`fills_cv` (2026-09-11) moves `fills` by `fills_cv_depth` per unit —
read **at each clock edge**, rounded, clamped to 0..`steps`, and the
pattern rebuilt there — so a slow [`lfo`](#lfo) breathes a rhythm from
sparse to dense and back, and a [`sequencer`](#sequencer) writes the
density per bar. **Ports**: `clock`, `reset` (gate), `fills_cv` (cv) →
`gate`, `accent` (gate). **Params**: `steps` 1..32 (16) · `fills`
0..steps (4) · `rotate` (0) · `accent_fills` (0) · `gate_len` 0.05..1
step (0.5) · `fills_cv_depth` fills/unit (8). See
`examples/clockwork_groove.json` and `examples/clock_divider_swing.json`.

#### `burst`

The **ratchet generator**: one `trigger` edge fires `count` gates.
Free-running they span `count/rate` seconds, `spread` warping the grid
(−1 accelerando … +1 ritardando, 0 even); with `clock` patched the
gates land on every `division`-th clock edge instead (mirroring the
clock's high time). `env` rides `(1−decay)^k` while gate k is high —
patch it straight into a VCA for decaying drum ratchets, no envelope
needed. A retrigger mid-burst restarts the burst. Bursts are scheduled
whole at the trigger edge: deterministic, block-size independent.
`count_cv` (2026-09-11) moves `count` by `count_cv_depth` per unit, read
**at the trigger edge** and latched — the count is a property of that
burst, so a sequencer step into it writes the ratchet count per hit.
**Ports**: `trigger`, `clock` (gate), `count_cv` (cv) → `gate` (gate),
`env` (cv). **Params**: `count` 1..16 (3) · `rate` 0.5..50 Hz (8) ·
`division` 1..8 (1) · `decay` 0..1 (0.3) · `spread` ±1 (0) ·
`count_cv_depth` gates/unit (8). See `examples/clockwork_groove.json`.

#### `bernoulli_gate`

The **probability switch**: each incoming gate is routed *whole*
(decision latched on the rising edge, length preserved) to `out_a` with
probability `p`, else `out_b` — `p` being `probability` plus the `p_cv`
value at the edge, clamped 0..1. `mode` `independent` flips a fresh coin
per gate; `toggle` makes the coin decide whether to *switch* outputs
(p = 1 alternates A/B/A/B strictly). One seeded rng draw per gate —
deterministic per `seed`, block-size independent, and `count(A) +
count(B) = count(in)` exactly. The classic patch: eighth-note hats in,
closed hat on A, open hat on B, probability low — a hi-hat line that
breathes. **Ports**: `in` (gate), `p_cv` (cv) → `out_a`, `out_b`
(gate). **Params**: `probability` 0..1 (0.5) · `mode` (independent) ·
`seed` (1). See `examples/clockwork_groove.json`.

#### `clock_divider`

The clockwork family's fourth member (2026-09-11): one clock in, five
gates out. `div2` / `div4` / `div8` are the fixed divisions; `divn` is
yours (`n` 1..32) and carries **swing** — every second `divn` gate is
delayed by `swing` of its period (0.33 is the triplet feel, 0.5 a
dotted shuffle; up to 0.75); `mult` emits `m` gates per incoming period
(2..4), the extra ones scheduled from the last measured interval, so
multiplication is **approximate for exactly one period after a tempo
change** (pending gates are dropped when the next real edge arrives).
Edges since the last `reset` are counted and edge *i* fires each
division when `i mod k = 0`, so the first edge after a reset is the
downbeat on **every** output at once. Gates are pulses of `pw` × *that
output's* period (so `div8`'s gates are four times longer than `div2`'s
at the same `pw`); before an interval has been measured — the very
first edge — they mirror the clock's own high time, the
[`euclidean`](#euclidean)'s rule. Absolute-sample scheduling: exact
division counts over a thousand edges, block-size independent. The
patch: sixteenths in, `div4` to a kick, `divn` at `n` 1 with `swing`
0.33 to closed hats — swung hats from a straight clock — `div8` to open
hats, and `mult` clocking a [`burst`](#burst) for triplet ratchets. See
`examples/clock_divider_swing.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `clock` | in | gate | The clock to divide. Unpatched → silence. |
| `reset` | in | gate | Rising edge: the next clock edge is the downbeat on every output. |
| `div2` / `div4` / `div8` | out | gate | Fixed divisions. |
| `divn` | out | gate | Division by `n`, with `swing`. |
| `mult` | out | gate | `m` gates per incoming period. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `n` | `3` | 1 … 32 | `divn`'s division. |
| `m` | `2` | 2 … 4 | `mult`'s gates per period. |
| `swing` | `0.0` | 0 … 0.75 | Delay on every second `divn` gate, as a fraction of its period. |
| `pw` | `0.5` | 0.05 … 0.95 | Gate width as a fraction of each output's period. |

#### `arpeggiator`

The **poly→mono collapser** — the first module to run the voice
architecture in reverse. Feed it a polyphonic `pitch_cv` + `gate` pair
([`cv_keyboard`](#cv_keyboard) or [`midi_input`](#midi_input)), hold a
chord, and each `clock` rising edge plays the next note as a mono
1 V/oct line: the classic hardware arp, patched not preset. `mode`
walks the held set `up`, `down`, `updown` (endpoints unrepeated),
`order` (true as-played order — arrival-stamped, not slot order) or
`random` (one seeded draw per step); `octaves` stacks 1..4 passes.
Notes joining or leaving mid-arp take effect on the next step; when the
last note is released the arp falls silent, rewinds, and the **pitch
holds** so downstream release tails stay in tune ([`cv_keyboard`](#cv_keyboard)
convention). `reset` rewinds so the next clock plays note 1. `hold`
latches — releases keep notes, and a press from silence clears the
latch and starts a new chord (the performance latch). The gate runs
`gate_len` of the measured clock period (mirroring the clock's high
time until two edges have been seen — the [`euclidean`](#euclidean)
convention). Mono inputs work too: one finger + `octaves` 2 is an
instant octave arp. **Internal clock** (2026-09-14): leave `clock`
unpatched and the arp steps on its own at `bpm` × `division` per minute
(120 × 4 = sixteenths) on an exact integer period — one module fewer
for the common case; `reset` re-phases it so the very next sample is
note 1 (by decree, since the internal line may already be high there).
Patch a `clock` and both params are ignored — the cable wins, as it
always did. **Ports**: `pitch_cv` (cv), `gate`, `clock`, `reset` (gate)
→ `pitch_cv` (cv), `gate` (gate). **Params**: `mode` (up) · `octaves`
1..4 (1) · `gate_len` 0.05..0.95 step (0.5) · `hold` (off) · `seed`
(1) · `bpm` 20..300 (120) + `division` 0.25..16 /beat (4), the internal
clock. See `examples/chord_arp_factory.json` (external clock) and
`examples/chord_legato_inversions.json` (no clock cabled).

#### `logic`

**Two-input gate algebra**, every jack live at once — no mode combo,
you swap cables, not settings. `a`/`b` in; `and`, `or`, `xor`, `nand`,
`not_a` out, all computed every block, all exact. Cross a clock with a
[`euclidean`](#euclidean) pattern: `and` is the pattern gated to the
clock's width, `xor` is the **complementary rhythm** (hits where the
pattern rests — instant interlocking drums), `not_a` inverts for
"everything except" patching. An unpatched operand reads **low**, so
`nand` idles *high* with nothing connected — the classic normalled-
NAND trick, a free gate-off signal. **Zero parameters, deliberately**:
the idea-list's "comparator mode" was dropped because port kinds are
strict (a cv out cannot cable into a gate in) — cv→gate conversion is
[`schmitt`](#schmitt)'s whole job. Stateless; a voice-aware gate
source collapses to any-voice-high on fetch. **Ports**: `a`, `b`
(gate) → `and`, `or`, `xor`, `nand`, `not_a` (gate). **Params**: none.
See `examples/logic_offbeat_drums.json`.

#### `lfo`

Low-frequency oscillator as a `cv` source: `sine`, `triangle`, `square`,
`saw` or `random` (sample-and-hold — a fresh value once per cycle) at
`rate` Hz, scaled by `depth`. **Unipolar by default** (`0 … depth`), which
is the shape a [`vca`](#vca) wants — a sine here into `vca.cv` is a
tremolo that "just works"; `bipolar` swings `−depth … +depth` for pitch
and cutoff modulation. `rate_cv` is FM-of-modulation: `cv_depth` octaves
per CV unit (1 V/oct by default, 0 disables), read as a block mean. The
LFO is **voice-aware through its rate**: a `(V, F)` `rate_cv` (a poly
envelope, say) gives every voice its own phase accumulator and a `(V, F)`
output; a mono `rate_cv`, or none, keeps one phase and a mono output.

**Retriggering** (2026-09-19). Free-running, a tremolo lands on each note
wherever its cycle happens to be — the first note swells, the next one
dips, and the ear hears the difference as unevenness. Patch the voice's
gate into `reset` (`cv_keyboard.gate → lfo.reset`, or a sequencer's
`gate`) and every rising edge restarts the phase at `phase` **on that
very sample**, not at the next block boundary: the block is rendered in
segments between edges, so a note that starts mid-block still gets its
LFO from the top. `phase` is in cycles: 0.25 on a sine is the peak (the
tremolo opens *with* the note and dips from there), 0.5 on a saw is the
zero crossing, 0 on a square is the top of the high half. It is also the
free-running start, and moving the knob re-anchors a free-running LFO at
the next block, so the slider is audible without a cable. On the
per-voice path a `(V, F)` gate resets each voice from its own row and a
mono gate resets every voice together; on the mono path a `(V, F)` gate
collapses to any-voice-high, the house rule. The `random` waveform rolls
a fresh value on a reset edge as well as on a wrap, so a retriggered S&H
starts every note on a new step. Unpatched `reset` at `phase` 0 is
exactly the old free-run (bit-exact).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `rate_cv` | in | cv | `cv_depth` octaves per unit on the rate, block-mean. A `(V, F)` source switches the LFO to one phase per voice. |
| `reset` | in | gate | Rising edge: restart the phase at `phase` on that sample. `(V, F)` per voice on the voice path; any-voice-high on the mono path. |
| `cv` | out | cv | The wave, `0 … depth` (unipolar) or `±depth` (bipolar). `(V, F)` when `rate_cv` is. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `waveform` | `sine` | sine / triangle / square / saw / random | Shape; `random` is sample-and-hold. |
| `rate` | `4.0` | 0.01 … 120 Hz | Cycles per second (clamped internally to 0.001 Hz … 0.45 × sample rate). |
| `depth` | `1.0` | 0 … 1 | Output amplitude. |
| `bipolar` | `false` | — | Off: `0 … depth`. On: `−depth … +depth`. |
| `cv_depth` | `1.0` | 0 … 4 oct/unit | Octaves the rate moves per unit of `rate_cv`. |
| `phase` | `0.0` | 0 … 1 cyc | Start phase: the free-running start and where `reset` jumps to. Wraps. |

**Patching.** `lfo.cv → vca.cv` for tremolo, `lfo.cv → oscillator.freq_cv`
(bipolar, `depth` ≈ 0.03) for vibrato, `lfo.cv → filter.cutoff_cv` for
wah; `lfo.cv → schmitt.in` turns it into a clock. See
`examples/vibrato.json`, `examples/keyboard_tremolo.json`,
`examples/lfo_retrigger.json` (key-synced tremolo) and
`examples/schmitt_lfo_clock.json`.

---

### Bridges (signal-kind converters)

Every wall between signal kinds has a door.

#### `audio_to_cv`

Envelope follower: rectifies an `audio` input and smooths it (asymmetric
attack/release) into a `cv` signal. Lets audio drive modulation — a kick can
sidechain a pad, a band of a track can shape a synth.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to follow. |
| `cv` | out | cv | Smoothed envelope of the input level. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `attack_ms` | `5.0` | ms | How fast the envelope rises to a louder input. |
| `release_ms` | `100.0` | ms | How fast it falls when the input quietens. |
| `gain` | `1.0` | ≥0 | Scales the output `cv`. |

**Patching.** `crossover.low → audio_to_cv.in`, then `audio_to_cv.cv →
oscillator.amp_cv`. Or the **self-wah**: `filter.out → audio_to_cv.in`
and `audio_to_cv.cv → filter.cutoff_cv` — the filter's own output
envelope opens it, so attacks brighten and decays close down. That is a
loop, and it closes one block late (see *Feedback loops* under Cabling
rules; before 2026-09-16 this cable was silently inert). See
`examples/envelope_follower_wah.json`.

#### `cv_to_audio`

_To document._ Re-labels a `cv` signal as `audio` (with `gain`) — unlocks
audio-rate LFOs as tone sources and percussive clicks from fast envelopes. See
`examples/lfo_oscillator.json`.

#### `schmitt`

_To document._ Schmitt trigger: turns a `cv` signal into a `gate` using two
thresholds (`high`/`low`) with hysteresis — e.g. an LFO becomes a clock. See
`examples/schmitt_lfo_clock.json`.

---

### Routing & mixing

#### `mixer`

Four audio inputs with per-channel gain trims and a master:
`out = master · Σ (gain_i · cv_i · in_i)`. Output is clipped at the speaker,
not here, so a hot mix keeps its headroom into downstream filters.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in1` … `in4` | in | audio | The four channels. Unconnected = silence. |
| `gain1_cv` … `gain4_cv` | in | cv | Per-channel VCA-style gain CV, **per-sample multiplicative** (`in_i · gain_i · cv_i`); unpatched = unity. Optional. |
| `out` | out | audio | The mix. |

**Parameters:** `gain1`–`gain4` (default `1.0`), `master` (default `0.7`).

**Patching.** The gain CVs are knobless by the house rule ([CV depth
conventions](#cv-depth-conventions)) — the CV *is* the channel's amplitude,
like [vca](#vca)'s `cv`; attenuate with a [CVScale](#cv_scale) if needed. An
ADSR into `gain2_cv` swells channel 2; an LFO into `gain1_cv` plus its
inverse (CVScale −1 → CVOffset +1) into `gain2_cv` is an auto-crossfade; a
[sequencer](#sequencer) lane steps channels in and out — voltage-controlled
mixing. See `examples/mixer_crossfade_verb.json`.

#### `combiner`

_To document._ Four audio inputs summed to one output (no per-channel gain).
Use to recombine crossover bands or sum voices.

#### `cv_combiner`

_To document._ Four `cv` inputs summed or averaged (param `mode`: `sum` /
`average`) — lets an LFO and an ADSR modulate the same destination. See
`examples/mod_matrix.json`.

#### `matrix_mixer`

A **4×4 bipolar gain matrix** — and the rack's sanctioned **feedback
door**. ``out_c = Σ_r g_rc · in_r``, sixteen gains −1..+1 on a drag
grid (negative = phase flip; identity diagonal default = a bit-exact
4-channel pass). Each `cv_c` input scales its whole output *column*
(one jack per column — per-node CV would be sixteen jacks of soup).

**Feedback.** The backend renders a topo-sorted DAG, so a loop
(matrix → delay → back into the matrix) has no valid order — at
compile time, any cable INTO a matrix_mixer that would close a cycle
is marked a **late-read** — the matrix reads that source's *previous
block*, giving the loop exactly **one block of feedback latency**
(~10.7 ms at 48 kHz/512; the standard software-modular answer).
Feed-forward paths through the matrix stay zero-latency, the rest of
the graph sorts exactly as before, and a fresh loop's first block reads
silence. Suddenly whole patch classes exist: regenerating echo
networks, shimmer (reverb + pitch_shifter in a loop), drone feedback,
cross-coupled delay lines. Since 2026-09-16 *every* loop closes this
way, through any module (see *Feedback loops* under Cabling rules) —
the matrix is no longer the only door, but it is the **guarded** one:
`soft_clip` is what keeps a hot loop on the ceiling.

``soft_clip`` (default **on**) is the stability guardrail, shaped to
never color a sane mix: **transparent below 0.95** (bit-exact — a
plain tanh would shave 8 % off a 0.5 signal), then a C1-continuous
tanh section saturating at exactly **1.0**. A runaway loop (summed
loop gain > 1) lands on the ceiling; switch it off and the same loop
grows without bound. Note the ±1 gain clamp means loop gain > 1 needs
*parallel* return paths (e.g. one return into two rows) — a single
gain can't exceed unity.

**Ports**: `in_1`…`in_4` (audio), `cv_1`…`cv_4` (cv, per-column) →
`out_1`…`out_4` (audio). **Params**: `g11`…`g44` −1..+1 (identity) ·
`soft_clip` (on). See `examples/matrix_feedback_echo.json` — a kick
through a regenerating echo network; `g21` is the regen knob — and
`examples/organ_feedback_drone.json`, the same door held open under a
sustained [`organ`](#organ) instead of a transient, and
`examples/organ_shimmer.json`, which adds a
[`pitch_shifter`](#pitch_shifter) inside the loop so every lap climbs an
octave.

#### `mid_side`

**M/S encode/decode + stereo width** — the missing stereo utility
beside [`stereo_speaker_output`](#stereo_speaker_output). Feed a
stereo pair in and every view is live at once: `mid`/`side` are the
encode (`M = (L+R)/2`, `S = (L−R)/2` — process them separately,
recombine however you like), `out_l`/`out_r` are the decode
`M ± width·S`. `width` 0..2: **0** collapses to dual mono, **1** is
unity (decode ≡ input, bit-close), **2** doubles the side — wider than
the room. `width_cv` adds per sample (clamped 0..2): an LFO breathes
the field, an envelope ducks width on transients. Mono-friendly: one
patched input **is** the mid (level preserved, not halved; width
inert) — a free passthrough. Stateless and exact. **Ports**: `in_l`,
`in_r` (audio), `width_cv` (cv) → `mid`, `side`, `out_l`, `out_r`
(audio). **Params**: `width` 0..2 (1). See
`examples/mid_side_breathe.json`.

---

### Utilities

Small CV helpers that scale, offset, or generate control signals — the
patch-cord glue that lets any source drive any destination.

#### `constant`

A fixed CV level: no inputs, one `cv` output holding the scalar `value`
(default 1.0) every sample. A patchable DC source — the manual knob of a
modular. Use it to bias a modulator (into a `cv_offset` or `cv_combiner`),
to tune a fixed pitch (`constant → cv_to_frequency.cv`), or as a steady
VCA gain. Output is always mono `(frames,)`, which broadcasts cleanly
against any per-voice consumer. Param: `value` (not clamped — ±1 for
modulation, larger for 1V/oct pitch).

#### `cv_scale`

Multiplies a CV by a fixed factor: `out = in * scale`. The classic
*attenuverter* — attenuate when |scale| < 1, amplify when > 1, invert when
negative. Tames a full-depth LFO, flips an envelope for ducking, or boosts
a shy modulator. Shape-polymorphic (pure pointwise gain): a mono CV stays
mono, a voice-aware `(V, F)` CV stays `(V, F)`. Unpatched input → silence.
Param: `scale` (default 1.0).

#### `cv_offset`

Adds a fixed DC level to a CV: `out = in + offset`. Where `cv_scale`
changes a modulator's depth, this changes its centre — slide a bipolar ±1
LFO up by 1.0 to get a 0..2 unipolar signal, or bias a cutoff CV. With
nothing patched the input is treated as 0, so an unpatched `cv_offset` is a
constant `offset` (a quick stand-in for `constant`). Scale-then-offset
composes into a full affine map. Shape-polymorphic; the scalar `offset`
broadcasts across the voice axis. Param: `offset` (default 0.0). See
`examples/cv_utility_demo.json`.

#### `cv_math`

**[`logic`](#logic) for voltages**: two CV operands in, seven functions
out, all computed every block — no mode combo, you swap cables, not
settings. The rack could already add CVs ([`cv_combiner`](#cv_combiner)),
scale and offset them ([`cv_scale`](#cv_scale), [`cv_offset`](#cv_offset))
and slew them ([`slew`](#slew)); what it could not do is *compare* two of
them or *multiply* them, and those are the moves that make one modulator
shape another.

| Jack | Function | What it is for |
|------|----------|----------------|
| `min` | min(a, b) | The analog AND: an LFO ducked by an envelope. |
| `max` | max(a, b) | The analog OR: two slow LFOs make a shape neither has alone. |
| `avg` | (a + b) / 2 | The blend that never overshoots either. |
| `diff` | a − b | An LFO minus its slewed self is its rate of change; a pitch minus another is an interval. |
| `mult` | a × b | The CV×CV multiplier the rack had no other jack for: an envelope on a vibrato's depth (delayed vibrato), a slow LFO fading a fast one in and out. |
| `rect` | \|a\| | Full-wave: a bipolar LFO folded unipolar at twice the rate. |
| `inv` | −a | The flip (swap cables for −b). |

Zero parameters, deliberately. An unpatched operand reads **0**, which is
the normalled trick again: with only `a` patched, `max` is its positive
half-wave and `min` its negative one (half-wave rectifiers for free),
`avg` is `a/2`, `diff` is `a` itself bit-exact, `mult` is silence. Both
unpatched: everything is 0. Stateless, elementwise, exact.
Shape-polymorphic: mono CVs give mono outs; a voice-aware `(V, F)` operand
gives `(V, F)` outs with a mono partner broadcast across the voice axis
(a per-voice pitch times a mono envelope); a single voice row is
bit-identical to mono. See `examples/cv_math_delayed_vibrato.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `a`, `b` | in | cv | The two operands. Unpatched → 0. |
| `min`, `max`, `avg`, `diff`, `mult` | out | cv | The pair functions. |
| `rect`, `inv` | out | cv | The one-operand functions, of `a`. |

**Parameters:** none.

#### `cv_recorder`

**The modulation looper.** Every other modulator in the rack is
*generated* — an LFO's shape, an envelope's curve, a sequencer's steps, a
drift's wander — and none of them is *you turning a knob*. This records
a control voltage for a fixed loop length and plays it back forever: a
filter sweep you performed once becomes the filter's motion for the rest
of the piece; a hand-drawn pan becomes a pattern; an LFO caught mid-bar
becomes a rhythmic modulation that repeats in time with the song. Nothing
else here captures *performance*.

It is the fixed-length kind of looper (the "N bars" kind): `length` is a
setting, `rec` writes into it, the loop plays from the moment it exists,
`clear` wipes it. **Leave `in` unpatched and the module's own `value`
knob is the input** — a hand on the slider while `rec` is high is the
recorded gesture (the knob is read once per block and ramped linearly
across it from the previous block's value, so a recorded turn has no
steps). `rec` records while high; its first rising edge is when the loop
starts to exist — the position ramp starts at 0 then and runs forever
until `clear`. `mode` says what recording does to the buffer:
**`replace`** overwrites (a punch-in: only the stretch where `rec` was
high changes); **`overdub`** (default) adds the input to what is there,
the old layer first scaled by `feedback` — 1 piles layers up, 0.5 halves
what was there on every pass, so the loop keeps evolving instead of
accumulating. `out` is the loop, and while recording the value being
written (in overdub, old + new) — what you hear is what you keep. `pos`
is the loop position as a 0..1 ramp, a free sync signal. `clear` wipes,
rewinds and stops recording; the position holds at 0 until the next
`rec`, so the next take starts at the top.

**Clocked.** Patch a [`clock`](#clock) and `length` is read as **clock
ticks** instead of seconds (16 sixteenths = a bar); the loop's sample
length is fixed from the clock's period (measured from its last two
rising edges) when the loop is created — a `rec` edge that would create
the loop before the period is known waits for the clock's second tick.
Two things then keep it musical: `rec` edges, on *and* off, are honoured
on the **next tick** (quantised punch-in — press slightly late and the
loop still starts on the bar), and the position **hard-syncs to 0 on
every `length`-th tick** counted from the loop's start, so it never
drifts from the transport. A tempo change after the loop exists crops
or wraps the fixed buffer until the next sync — documented, not fought;
`clear` and re-record to re-measure.

Mono (a polyphonic `in` collapses to the house sum). Renders are
block-size independent whenever `in` is patched — every event is an
integer sample position; the knob path is block-rate by nature. Cost is
nil: each block is a few vectorized slices. Numpy backend only; silent
stub under pyo. See `examples/cv_recorder_layers.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | cv | The signal to record. Unpatched → the `value` knob. |
| `clock` | in | gate | Optional: `length` in ticks, quantised rec, hard sync at the loop boundary. |
| `rec` | in | gate | Record while high. The first rising edge creates the loop. |
| `clear` | in | gate | A rising edge wipes the loop and rewinds; the position holds until the next `rec`. |
| `out` | out | cv | The loop (while recording, what is being written). |
| `pos` | out | cv | Loop position, 0..1. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `length` | `4.0` | 0.05 … 60 | Loop length — seconds, or clock ticks while `clock` is patched. Fixed when the loop is created. |
| `mode` | `overdub` | replace / overdub | Punch-in overwrite, or add to the existing layer. |
| `feedback` | `1.0` | 0 … 1 | Overdub: what the old layer is scaled by before the new input is added. |
| `value` | `0.0` | −1 … 1 | The gesture knob — the input while `in` is unpatched. |

#### `sample_hold`

Samples `in` on each **rising edge** of the `trig` gate and holds that
value steady on `out` until the next trigger — the classic modular
staircase. It discretises a signal in *time*: feed a wandering source
(an LFO, or a fast `random` LFO as a noise stand-in) and a steady clock
for stepped/random melodies, or sample a slow modulator to stair-step
it. The trigger is a `gate`, so `schmitt` (turn any LFO/CV into a
clock), a keyboard/MIDI gate, or an ADSR gate all drive it. Unpatched
`in` samples 0 (pure S&H — no internal noise; that's the Noise
generator's job); unpatched `trig` holds the last value. No params.
Shape-polymorphic: mono `(F,)` or per-voice `(V, F)` with per-voice held
values, a mono partner broadcasting across the voice axis. See
`examples/sample_hold_arp.json`.

#### `slew`

A **slew limiter / lag / glide** for control signals — the time-shaped
counterpart to the pointwise `cv_scale` / `cv_offset`. Feed a CV into `in`
and `out` *chases* it: when the input jumps, the output ramps toward it
instead of snapping, with independent `rise_time` and `fall_time` (seconds;
0 = instant on that side). `shape` picks the character: `linear` moves at a
constant rate and **reaches** the target (times read as seconds per 1.0 unit
of change — a rate, like an analog slew), while `exponential` eases toward
it one-pole style, fast then asymptotic (times read as ~time-to-99%, chosen
so the two shapes arrive in about the same wall-clock — flip the combo and
the glide keeps its length, only its curve changes).

The killer app is **polyphonic portamento**: `cv_keyboard` pitch CV →
`slew` → `cv_to_frequency` → oscillator, and every voice glides
independently between notes (voice-aware per-voice state, no crosstalk).
Also good for lagging a stepped `sequencer` into smooth sweeps, turning a
raw gate into a sloped poor-man's AR (instant rise + slow fall = peak
follower), or adding physical inertia to a filter-cutoff sweep. The running
value is carried across blocks (block-size independent) and **primed to the
first input sample**, so the output starts at the signal rather than
swooping up from zero; unpatched `in` emits 0.

**v2 (2026-09-14) — the times can move.** `rise_cv` / `fall_cv` are
1 V/oct on that side's *rate*, the [function_generator](#function_generator)'s
law: +1 halves the time, −1 doubles it, ±5 octaves, block-mean. They go
**per voice** when the CV arrives `(V, F)` alongside a `(V, F)` input —
`midi_input.velocity_cv → cv_scale → fall_cv` and hard-played notes glide
differently from soft ones — and are one law for every voice otherwise
(a voice row fed a constant is bit-equal to the mono render fed that
constant). Patch a `clock` and the glide goes **tempo-relative**: while
it is cabled the two times are read as *multiples of the clock period*
(1.0 = one pulse of whatever is cabled; 0.6 = most of a sequencer step)
instead of seconds, so a portamento or a sequencer lag stays in time when
the tempo moves — the period is the gap between the last two rising
edges, carried across blocks (the [euclidean](#euclidean) convention);
until two have been seen the times are seconds, and unpatching the clock
makes them seconds again. The two compose: clock sets the beat, CV bends
it. Params: `shape` (default `linear`), `rise_time` / `fall_time`
(default 0.1 — seconds, or × clock period while `clock` is patched). See
`examples/slew_portamento.json` (free time) and
`examples/slew_clocked_glide.json` (an 8-step line whose exponential glide
is 0.6 of a step at any tempo — change the clock's BPM and the glide
follows — with a slow LFO breathing the rise speed ±1 octave).

#### `quantizer`

Snaps a wandering pitch CV to the **nearest note of a scale** — the
missing link between random/LFO/sequencer voltages and *melody*. 1 V/oct,
C4 = 0 V (the house pitch convention), so `out` lands in tune at any
`freq_cv` / `pitch_cv` destination. The scale is `root` + `scale` (ten
built-ins from chromatic to blues and whole-tone); pick `custom` and the
node's twelve pitch-class tickboxes take over (an empty custom set falls
back to chromatic — the module never wedges silent). `transpose` shifts
the *output* in semitones after quantization.

Two modes, picked by patching. **Continuous** (`gate` unpatched): every
sample quantizes, and `hysteresis` (cents) makes the held note sticky —
a new note wins only once the input is more than the margin closer to it
than to the held one, so an input hovering exactly on a note boundary
doesn't flutter. **Gated** (`gate` patched): sample-and-quantize on
rising edges only, holding in between — clocked melodies from a
free-running source, drift-proof. Either way `changed` fires a ~5 ms
trigger per new held note — patch it to an envelope and every fresh note
articulates itself, no separate clock needed.

Voice-aware: a `(V, F)` input quantizes per voice (held note and
`changed` per slot, no crosstalk); the held note is primed to the first
input so loading a patch fires nothing. Neutral note: `chromatic` +
`hysteresis 0` + `transpose 0` is **semitone rounding**, deliberately not
a passthrough. See `examples/shift_random_melody.json`.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | cv | Pitch CV to quantize (1 V/oct). Unpatched → 0. |
| `gate` | in | gate | Optional: quantize on rising edges only. Mono, applies to all voices. |
| `out` | out | cv | Quantized (then transposed) pitch CV. |
| `changed` | out | gate | ~5 ms trigger per new held note, per voice. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `root` | `C` | C … B | Scale root. |
| `scale` | `major` | combo | chromatic · major · minor · harmonic minor · pentatonic maj/min · dorian · mixolydian · blues · whole tone · custom. |
| `hysteresis` | `10` | 0 … 50 ct | Boundary stickiness in continuous mode. |
| `transpose` | `0` | −24 … +24 st | Added to the output post-quantize (may leave the scale — it's a transposition, not a rotation). |
| `custom_c` … `custom_b` | all on | bools | Pitch classes for `custom`, drawn as two rows of tickboxes. |

#### `chord`

The **mono→poly explorer**, the [`arpeggiator`](#arpeggiator)'s mirror
twin: one pitch CV in, four voice rows out. Feed any mono melody source
([`sequencer`](#sequencer), [`quantizer`](#quantizer),
[`shift_random`](#shift_random)…) into `pitch_cv` + `gate` and the
outputs are `(4, F)` voice buffers — each row the input plus one
interval — ready for any voice-aware chain (oscillator `freq_cv`,
per-voice [`adsr`](#adsr), [`vca`](#vca)), exactly as if four keys were
held on a [`cv_keyboard`](#cv_keyboard). A named `preset` fills the
four interval slots (`major` = 0/4/7/12 and friends; `5` is the power
chord with slot 4 off); `custom` reads the `interval_n`/`enable_n` slot
rows instead. A disabled slot's row stays gate-low but the output is
always 4 rows, so downstream per-voice state never re-shapes on a live
toggle. `strum` staggers the gate *onsets* — row k rises `k × strum` ms
after the input edge (enabled rows only, sample-accurate across
blocks), every row falls together, and a fall cancels unfired onsets.
Pitch is continuous (`in + interval` every sample): glides chord along
and release tails stay in tune. `spread` opens the voicing one octave,
alternating (slot 2 +12, slot 3 −12) — `major` becomes the open
(−5, 0, 12, 16). **`inversion`** (2026-09-14) turns the voicing over:
1 sends the lowest sounding note up an octave (`major` → 12/4/7/12, E
in the bass), 2 the two lowest (G in the bass), 3 the three lowest.
Rows keep their slot identity — only a slot's pitch moves — so
downstream per-voice state and the strum order are untouched.
**`changed`** (gate out, same day) is the re-strum trigger: a ~2 ms
pulse whenever the *sounding* chord changes under a held gate — the
root jumping half a semitone or more between consecutive samples (a
sequencer or quantizer step; a glide never counts) or the interval set
changing (`preset`, `inversion`, `spread`, a slot edit). A fresh press
is its own trigger and does not also pulse. Patch it into an envelope
retrigger or a [`burst`](#burst) to re-articulate a legato root walk —
or tick **`retrig`** and the chord does it itself: on a change the four
gate rows drop for one sample and re-strum, a fresh rising edge for
whatever they drive, no extra cable. **Mind the sum**: four rows into a
mono sink collapse to a 4× signal — trim the downstream VCA (0.25 each
is unity). **Ports**: `pitch_cv` (cv), `gate` (gate) → `pitch_cv`,
`gate` (both `(4, F)`), `changed` (gate, mono). **Params**: `preset`
(major) · `interval_1..4` −24..24 st (0/4/7/12) + `enable_1..4` (on),
read when custom · `strum` 0..200 ms (0) · `spread` (off) · `inversion`
0..3 (0) · `retrig` (off). See `examples/chord_arp_factory.json` and
`examples/chord_legato_inversions.json` (a held gate, roots walking
under it, `retrig` re-strumming a maj7 in first inversion; the arp on
its internal clock).

#### `scope`

An **oscilloscope tap** you patch inline anywhere: `in` passes to `out`
**bit-exact** (the [`meter`](#meter) precedent), and the node draws the
waveform live — a classic 10-division face where the window is
`time_div` ms/div × 10 and every pixel column shows its slice's min/max,
so a one-sample click can never hide between pixels. `gain` scales
vertically; `freeze` holds the picture while the audio runs on.

The main trace is `in`; patch only the `cv` jack instead and the scope
draws *that* — LFOs, envelopes, quantizer steps, no `cv_to_audio` bridge
needed (`in` wins when both are patched; `cv` has no pass-through).
`in_r` is the second trace: `mode` `dual` stacks both in one face, `xy`
plots `in` against `in_r` — the goniometer (mono = diagonal line, stereo
width opens a cloud, quadrature LFOs draw circles). In `xy` the trace is
decimated to ~200 points per frame and there is **no persistence** — it
is the live window, not an accumulating image — so a shape built from
many slow passes (a [`chaos`](#chaos) attractor, say) wants `time_div`
wide enough to hold several passes at once rather than a fast repaint;
past roughly twenty passes per window the point budget starts to show as
straight edges. `trigger`
`rising`/`falling` align the sweep to the last `level` crossing that
fits a full window so periodic signals hold still; `free` shows the
newest window (envelopes, one-shots); a patched `trig` gate overrides
the level trigger and aligns to its last rising edge (clock-locked
sweeps). Voice-aware sources pass through shape-intact; the display sums
voices (what a mono consumer hears). Display maths lives in
`ui/scope_math.py` (dpg-free, tested headless); the audio thread only
copies into a capture ring. Numpy backend only; silent stub under pyo.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Main trace; forwarded to `out` untouched. |
| `in_r` | in | audio | Second trace (dual/xy); forwarded to `out_r`. |
| `cv` | in | cv | Fallback main trace when `in` is unpatched. |
| `trig` | in | gate | External trigger; overrides the level trigger. |
| `out` / `out_r` | out | audio | The untouched inputs. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `time_div` | `10` | 1 … 500 ms/div | Horizontal scale (10 divisions across). |
| `gain` | `1.0` | 0.1 … 10 | Vertical scale; ±1 fills the face at 1.0. |
| `trigger` | `rising` | free · rising · falling | Sweep alignment. |
| `level` | `0.0` | −1 … 1 | Trigger level. |
| `freeze` | `False` | bool | Hold the current picture. |
| `mode` | `mono` | mono · dual · xy | One trace, two stacked, or goniometer. |

#### `meter`

A **level indicator** you patch any audio signal into — `in` passes
straight through to `out` untouched, so a Meter can sit inline
(`source → meter → speaker`) or hang off a fan-out cable purely to
watch a level. The node shows the signal's **recent level in dBFS**
(a fixed −90 → 0 scale, so two meters read on the same reference and
are directly comparable — handy for eyeballing, say, a MicInput
against a FilePlayer before they hit a mixer). Patch the optional
`in_r` and the node grows a second bar for a **stereo pair**
(`in_r` → `out_r`, e.g. straight off a chorus/reverb's `out_l`/`out_r`
on the way to the two speaker sinks); leave it unpatched and the
Meter is exactly the single-channel meter it always was.

Each channel's display is three indicators drawn together:

- the **bar** — `mode="peak"` (default) is the classic fast-attack /
  adjustable-release recent-maximum reading; `mode="rms"` is a ~300 ms
  RMS average that reads closer to perceived loudness (a sine reads
  its amplitude −3 dB, and it sits lower than peak on transient
  material);
- the **peak-hold tick** — a marker that sits at the most recent peak
  for ~1.5 s before falling at the `release` rate (DAW-style), so a
  transient's true level can be read after the bar has fallen. It is
  peak-driven in *both* modes;
- the **clip lamp** — the small lamp past the bar lights the moment
  any sample reaches 0 dBFS (|sample| ≥ 1.0) and stays lit for ~2 s,
  so a momentary overload can't slip past between glances.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to measure (left / only channel). |
| `in_r` | in | audio | Optional right channel; patching it adds the second bar. |
| `out` | out | audio | `in`, passed through unchanged. |
| `out_r` | out | audio | `in_r` passed through unchanged (silence while `in_r` is unpatched). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `release` | `0.4` | 0.02 … 2 s | Fall time — roughly how long the bar takes to drop ~20 dB after a peak, and how fast the peak-hold tick falls once its ~1.5 s hold expires. Small = snappy/reactive; large = holds peaks longer for an easier read. Attack is always instant. |
| `mode` | `"peak"` | peak / rms | What the bar shows: recent maximum (peak) or ~300 ms average level (rms). The tick and lamp are peak-driven either way.  Plus `lufs_m`/`lufs_s`: K-weighted momentary (400 ms) / short-term (3 s) loudness, read in LUFS-ish units. |
| `stereo_link` | `False` | bool | With `in_r` patched: bars stay per-channel but hold tick, clip lamp and the numeric readout merge pair-wide (louder channel; channel-energy sum in the LUFS modes). |

**How it works.** Everything is computed on the audio thread, so a
short transient registers even between UI repaints and the meter
latency is block-rate, not frame-rate. The peak path is the same
fast-attack / time-based-release envelope as ever (bit-identical in
the default mode); RMS is `sqrt` of an exponential moving average of
the block mean-square (~300 ms time constant); hold and clip windows
are counted in samples, so their wall-clock timing is block-size
independent. Shape-polymorphic: a voice-aware `(V, F)` input shows the
loudest voice (per-voice RMS too — a plain average would be diluted
~16× by the zero-padded slots), and a clip on *any* voice lights the
lamp. See `examples/meter_levels.json` (a loud saw and a quiet square,
each through its own meter) and `examples/meter_stereo_master.json`
(a plucked saw through a chorus, the stereo pair metered inline on the
way to the L/R speakers — the tick rides above the falling bar on
every pluck).

---

### Sinks (outputs)

The end of a patch — where signal leaves the graph. Sinks have no outputs.

**Loudness + clip accounting (2026-07-02).** Two K-weighted modes read
loudness the broadcast way: `lufs_m` (momentary, 400 ms) and `lufs_s`
(short-term, 3 s) run the signal through a BS.1770-style pre-filter
pair (2nd-order highpass + high shelf, plain RBJ biquads — hence
LUFS-*ish*) into a mean-square window, displayed as
`-0.691 + 10·log10(msq)`; a full-scale 997 Hz sine anchors within a
few tenths of the spec's −3.01. A **clip counter** rides next to the
lamp in every mode: one unbroken run of samples at ≥0 dBFS is one
event (a flat-top counts once, block boundaries don't double-count);
it resets on recompile or a click on the meter row. `stereo_link`
turns a patched pair into a DAW-style master meter: per-channel bars,
shared tick/lamp/readout (energy-summed in the LUFS modes, so two
identical channels read +3 dB over one).


#### `speaker_output`

_To document._ Routes its `in` to both system output channels. Param: `gain`.
The default destination in most example patches.

#### `left_speaker_output`

_To document._ Routes `in` to the **left** channel only. Param: `gain`. Pair
with `right_speaker_output` for hard-panned stereo. See
`examples/stereo_hard_pan.json`.

#### `right_speaker_output`

_To document._ Routes `in` to the **right** channel only. Param: `gain`.

#### `stereo_speaker_output`

The **stereo speaker** — a sink with a place in the field, and the
"stereo variant" the mono speaker's docstring promised since v0.1.
Patch a mono source into `in_l` alone and `pan` places it with a
**constant-power** (−3 dB centre) cos/sin law — sweeps keep even
loudness. Patch a stereo pair (`in_l` + `in_r`, e.g. straight off a
chorus/reverb/flanger) and `pan` becomes a **balance** control (unity
at centre, cosine fade of the far side) while `width` does mid/side
scaling: 0 collapses to mono, 1 is untouched (bit-exact — the default
settings pass a pair through exactly), up to 2 exaggerates the sides.

`pan_cv` moves the pan per sample — an LFO is the classic autopan, an
envelope walks each note across the field — and `width_cv` breathes
the width the same way (both share `cv_depth`, the Reverb's paired-CV
convention; put a CVScale in front of either for independent
sensitivity). Voice-aware sources sum at
the jacks (the implicit-sum rule); everything lands on the same master
bus as the other speaker sinks, clipped at ±1.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in_l` | in | audio | Left / only input. Alone = mono source, constant-power panned. |
| `in_r` | in | audio | Right input; cabling it switches to stereo balance + width. |
| `pan_cv` | in | cv | Per-sample pan modulation, scaled by `cv_depth`. Optional. |
| `width_cv` | in | cv | Per-sample width modulation, same shared `cv_depth`. Optional. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `gain` | `1.0` | 0 … 2 | Output trim, applied after pan/width. |
| `pan` | `0.0` | −1 … 1 | Position (mono) or balance (pair). |
| `width` | `1.0` | 0 … 2 | Mid/side width; pairs only (mono has no side content). |
| `cv_depth` | `1.0` | 0 … 2 | Knob units per CV unit, shared by `pan_cv` and `width_cv`. |

**How it works.** Stateless per block, so block-size independence is
structural. Mono: θ = (pan + 1)·π/4, source × (cos θ, sin θ) — L² + R²
equals the source power at every position. Pair: width first
(M = (L+R)/2, S = (L−R)/2·width, skipped exactly at width = 1 while
`width_cv` is silent — the bit-exact default survives until a cable
actually modulates it; a live `width_cv` runs the maths per sample,
clamped 0..2), then
balance gL = cos(max(p, 0)·π/2) / gR mirrored, then gain. `pan_cv` is
per-sample; a `(V, F)` CV is averaged across voices (one global
position, like Loudness's `level_cv`). See
`examples/stereo_field_pluck.json` (a pentatonic pluck through a
chorus, width 1.6, a 0.22 Hz autopan sweeping the voice around the
room while a 0.06 Hz triangle on `width_cv` breathes the image
between narrow and wide).

---

#### `specific_stereo_speaker_output`

The **device-targetable stereo speaker**: everything
[`stereo_speaker_output`](#stereo_speaker_output) does — the same
`in_l` / `in_r` inputs, the `pan` / `width` / `gain` knobs, and the
shared-`cv_depth` `pan_cv` / `width_cv` jacks (see that entry for the
full pan-law / mid-side / CV behaviour) — plus a `device` parameter
naming the physical output it should play out of. The intent is a
cue / monitor bus you can pin to headphones while the main mix stays
on the studio monitors.

The `device` field is a dropdown snapshotted at widget creation with a
**Refresh** button, exactly like the [`mic_input`](#mic_input) picker
but listing audio *playback* devices via `available_output_devices()`,
and it round-trips through saved patches. Left empty (`""`, the
default) the sink drains into the shared master bus **bit-identically**
to `stereo_speaker_output`. Set to a named device, the sink is pulled
**off** the master onto that device's own stereo bus: at `start()` the
engine opens one secondary `sounddevice.OutputStream` per distinct
selected device, the single per-block render is split so each routed
sink's audio goes to its device bus (summed if several sinks share a
device), and a small drop-oldest ring buffer hands blocks from the main
audio callback to each device stream. **Changing a sink's device takes
effect live** — the engine reconciles the open streams on the fly,
rebuilding only the one that changed (opening the new device, closing
the old if nothing else uses it) with no Stop/Start; adding or removing
a routed sink reconciles the same way on the next graph edit. A device
that fails to open is logged and that sink stays silent while the rest
of the patch plays on.

**Caveat — drift.** The two (or more) `OutputStream`s share samplerate
and blocksize but run on independent PortAudio clocks, so a second
device is not sample-synchronised with the main output: the ring
absorbs jitter, an empty ring emits a block of silence and a full ring
drops its oldest block, and the second device sits a few blocks behind.
Fine for a cue / monitor / headphone bus, which is what this sink is
for; don't rely on it for phase-locked multi-device playback.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in_l` | in | audio | Left / only input. Alone = mono source, constant-power panned. |
| `in_r` | in | audio | Right input; cabling it switches to stereo balance + width. |
| `pan_cv` | in | cv | Per-sample pan modulation, scaled by `cv_depth`. Optional. |
| `width_cv` | in | cv | Per-sample width modulation, same shared `cv_depth`. Optional. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `gain` | `1.0` | 0 … 2 | Output trim, applied after pan/width. |
| `pan` | `0.0` | −1 … 1 | Position (mono) or balance (pair). |
| `width` | `1.0` | 0 … 2 | Mid/side width; pairs only. |
| `cv_depth` | `1.0` | 0 … 2 | Knob units per CV unit, shared by `pan_cv` and `width_cv`. |
| `device` | `""` | device name | Target output device; `""` = system default (drains to master). A named device routes the sink to its own OutputStream; changing it re-routes **live**. |

---

#### `buffered_specific_speaker_output`

The **device-targetable stereo speaker with its own buffer size**:
everything [`specific_stereo_speaker_output`](#specific_stereo_speaker_output)
does — the same `in_l` / `in_r` inputs, the `pan` / `width` / `gain`
knobs, the shared-`cv_depth` `pan_cv` / `width_cv` jacks, and the same
`device` picker with **Refresh** that routes the sink to its own output
stream — plus a `buffer_size` parameter setting the block size (frames
per PortAudio callback) of *that* secondary stream, independent of the
global buffer size driving the main output.

It also carries the **governor pair**: `fill` (cv out) publishes the
hand-off ring's fill fraction (0..1; neutral 0.5 with no live stream),
one block delayed — the topological sort deliberately ignores cables
leaving this jack, so feedback patches are legal. `ratio_cv` (cv in)
varispeed-resamples the block pushed to the secondary stream: length
becomes `frames * (1 + cv * ratio_depth)`, clamped 0.5×..2× and smoothed
(~0.15 s) against control wobble. Wire
`fill → cv_offset(−0.5) → cv_scale(scale −1, NEGATIVE) → ratio_cv` and
the sink stretches time to hold its own ring at half — an
adaptive-resampling clock governor built from patch cables. (The sign
matters: a low ring needs positive cv to push more samples, so the loop
inverts; positive gain runs away.) The stretch is **pitch-preserving**
(streaming WSOLA cancelled by the length resample), so even large
corrections hold pitch — at the cost of ~50 ms constant latency on the
governed path and a one-grain warm-up (brief silence) when the cable
first lands. Prefer no patching? Flip **`auto_govern`** and the sink runs
that same controller internally (canonical half-full setpoint + 0.5
gain), holding its own ring with no cables; a patched `ratio_cv` still
overrides it. With `ratio_cv` unpatched and `auto_govern` off (both
defaults) the push is bit-identical to before. Numpy
backend only.

Why a per-sink buffer: the main mix might run at a tight 128-frame
buffer for a responsive keyboard while a flaky USB / Bluetooth monitor
on this sink needs a roomy 1024 to stop crackling — or the reverse, a
low-latency cue feed off an otherwise safe, sluggish main buffer. The
secondary stream already runs on its own PortAudio clock, so it can
carry its own block size. To make that work the drop-oldest hand-off
ring is counted in **samples, not blocks**, so the main render's block
size and this stream's block size need not match; the ring's capacity
scales with this sink's `buffer_size` so even a large secondary buffer
always fills.

`buffer_size` is a dropdown of the sink sizes — the global slider's
stops (`64` … `1024`) **plus `2048` / `4096` / `8192` extensions** the
global slider deliberately doesn't offer (the main stream's block size
also sets keyboard-to-ear latency; this cue/monitor stream's doesn't, so
a drifting Bluetooth device can ride ~186 ms of cushion) — and
round-trips through saved patches. It is read when the stream opens, so
the natural workflow is to set it **before you Start**; changing it live
rebuilds just this sink's stream (a brief gap on that one device),
exactly as changing `device` does. Secondary streams are keyed by
`(device, buffer_size)`, so one physical device can carry several
streams at different buffer sizes, and a buffered sink shares a plain
`specific_stereo_speaker_output`'s stream only when their sizes match.
Everything else — the `""`-drains-to-master equivalence, the per-device
summing, the independent-clock **drift** caveat, a failed open logging
and silencing just that sink — is identical to
[`specific_stereo_speaker_output`](#specific_stereo_speaker_output).
Only the numpy backend routes it; under pyo it is a silent stub, like
the other stereo speakers.

**Ring readout.** The node carries a live one-line telemetry readout of
the sink's hand-off ring, refreshed every GUI frame:

```
buffer 47% (3852/8192)  under 0  drop 2
```

The percentage (and `queued/capacity` samples) is how full the ring
between the render clock and the device clock is; `under` counts device
callbacks the ring couldn't fully serve (a gap was zero-padded in) and
`drop` counts render pushes that lost audio — the ring overflowed and
shed its oldest samples, or the push was bigger than the whole ring —
both cumulative since the stream opened. Underruns only start counting
once the ring has first filled to one device block, so the inevitable
fill-up gap at Start (a 8192 sink block takes ~186 ms of pushes before
it can serve anything) doesn't read as trouble. Reading it: a climbing
`under` means the cushion is too thin — pick a bigger `buffer_size`; a
ring pinned near 100% with climbing `drop` means the device clock runs
slower than the main stream and the ring is shedding oldest audio
(latency rides the ring's ceiling); *both* climbing from the first
moment means the ring itself (8× `buffer_size`) is smaller than one
main-stream block (e.g. `buffer_size` 64 under a 1024 global buffer) —
raise `buffer_size` or lower the global buffer. The text sits grey at
`buffer: idle` while the sink has
no stream of its own (transport stopped, `device` empty so it drains to
master, or the device failed to open), green while the ring is healthy,
and flashes amber for a moment whenever either counter ticks. Sinks
sharing one `(device, buffer_size)` stream show identical numbers, by
design. (numpy backend only, like the routing itself.)

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in_l` | in | audio | Left / only input. Alone = mono source, constant-power panned. |
| `in_r` | in | audio | Right input; cabling it switches to stereo balance + width. |
| `pan_cv` | in | cv | Per-sample pan modulation, scaled by `cv_depth`. Optional. |
| `width_cv` | in | cv | Per-sample width modulation, same shared `cv_depth`. Optional. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `gain` | `1.0` | 0 … 2 | Output trim, applied after pan/width. |
| `pan` | `0.0` | −1 … 1 | Position (mono) or balance (pair). |
| `width` | `1.0` | 0 … 2 | Mid/side width; pairs only. |
| `cv_depth` | `1.0` | 0 … 2 | Knob units per CV unit, shared by `pan_cv` and `width_cv`. |
| `device` | `""` | device name | Target output device; `""` = system default (drains to master). A named device routes the sink to its own OutputStream; changing it re-routes **live**. |
| `buffer_size` | `512` | 64 … 8192 | Block size of this sink's own output stream, independent of the global buffer (whose slider tops out at 1024 — the 2048/4096/8192 stops are sink-only). Read at stream open; a live change rebuilds only this stream. Out-of-range values are clamped. |

---

#### `warping_buffered_speaker_output`

The **tape-warp sibling** of
[`buffered_specific_speaker_output`](#buffered_specific_speaker_output):
same sink, same jacks, same per-device routing, `buffer_size`, hand-off
ring, live ring readout and governor loop — but the ring correction is
*audible on purpose*. Where the buffered sink hides its stretch behind a
pitch-preserving WSOLA engine, this one drives a plain varispeed resample,
like a tape deck whose capstan speeds up and slows down. When the ring runs
low the governor commands `ratio > 1`, which both pushes more samples to
refill the ring **and** plays the block slower — the pitch dives, the deck
sounds like it's running out of juice. When the ring recovers the ratio
falls back through 1 and the pitch spins back up. A buffer underrun stops
being a click and becomes musical wow-and-flutter.

`auto_govern` defaults **on** here (warping *is* the point): drop the sink
in, pick a device, and it self-warps with no cables — it only engages once
routed to a named device (a ring to regulate has to exist; on the master
bus it's an ordinary stereo speaker). A patched `ratio_cv` overrides the
internal controller, exactly as on the buffered sink. Instead of the
buffered sink's one-pole ratio smoothing, the move is shaped like a real
transport: a constant-rate slew in ratio (the resampler brake's
constant-torque feel), asymmetric between the two directions. The real-GUI
sweet spot found in testing: `brake_time` = `spinup_time` = **10 s** gives
a slow, gentle tape drift rather than a dramatic dive. Numpy backend only,
like the routing itself.

**Ports** — identical to
[`buffered_specific_speaker_output`](#buffered_specific_speaker_output)
(`in_l` / `in_r`, `pan_cv` / `width_cv`, `ratio_cv` → `fill`).

**Parameters** — the buffered sink's set (`gain`, `pan`, `width`,
`cv_depth`, `device`, `buffer_size`, `ratio_depth`) plus:

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `auto_govern` | `True` | bool | Inherited, but defaults **on**: the sink self-regulates its ring audibly out of the box. |
| `brake_time` | `0.5` | 0 … 30 s | Seconds the pitch takes to coast DOWN when the ring starves (ratio ramps up). Longer = a slower, more dramatic tape-slowdown; 0 = snap. |
| `spinup_time` | `0.25` | 0 … 30 s | Seconds the pitch takes to wind back UP as the ring recovers. Slightly quicker than the brake by default, so it recovers eagerly. |

See `examples/warping_buffer_tape.json`.

---

#### `disk_writer`

Records its `in` to a **16-bit mono WAV** at the backend's sample rate — a
sink: audio in, nothing out, a file on disk. The renderer opens the file
lazily the first time a compiled patch contains an armed writer and closes
it when the transport stops; re-arming creates a new take under the same
name (existing files are overwritten without prompting — no modal dialogs
mid-take). Disk I/O never touches the audio callback: each writer owns a
daemon worker thread behind a bounded queue, and in the rare case the queue
is momentarily full the block is dropped (counted) rather than letting the
audible output glitch.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | The bus to record. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `path` | `"recording.wav"` | filename | Where the WAV lands. Relative paths resolve against the process working directory; add `.wav` yourself. |
| `armed` | `True` | bool | Off = the writer sits in the patch inert, no file opened; lets you keep it wired without cutting a take every play. |

See `examples/record_a_take.json`.

---

## Appendix: example patches

The `examples/` folder is the fastest way to learn a module — each `.json`
loads in the app. Notable ones referenced above:

- `hello_sine.json`, `fat_saw.json` — basic oscillators.
- `oscillator_pwm.json` — the classic PWM pad: two `square_blep` [`oscillator`](#oscillator)s seven cents apart, each with its own slow LFO on `pw_cv` (widths sweeping 0.1…0.9, never in step), summed through a breathing 900 Hz lowpass into a room.
- `sampler_breaks.json` — a breaks machine: three [`sampler`](#sampler)s
  pointed at three regions of one synthetic drum loop, each fired by its
  own [`euclidean`](#euclidean) (4, 2 and 6 pulses in 16). Run
  `python examples/samples/generate_samples.py` first to create the
  loop — until you do, the patch loads and plays silently rather than
  failing, because an unreadable path is silence by contract.
- `wind_duet.json` — two [`wind`](#wind)s: a `flute` tune an octave up
  (vibrato LFO summed into `pitch_cv`, a 0.15 Hz LFO on `breath_cv` so
  the player breathes) over a `reed` line in the chalumeau register at
  half speed off a [`clock_divider`](#clock_divider), through a chamber.
- `cv_recorder_layers.json` — the modulation looper, clocked: sixteenths
  into [`cv_recorder`](#cv_recorder)'s `clock` with `length` 16 (a bar),
  a slow clock holding `rec` high every other bar, a 0.37 Hz triangle
  LFO as the thing recorded — each recording bar overdubs another
  out-of-step layer at `feedback` 0.6, so the loop that moves the
  filter keeps evolving; a [`scope`](#scope) shows it layering.
- `cv_math_delayed_vibrato.json` — the classic: a vibrato LFO × the
  note's ADSR through [`cv_math`](#cv_math)'s `mult`, so the wobble is
  absent at the attack and full at the sustain (summed into the pitch
  with a [`cv_combiner`](#cv_combiner)); beside it a second `cv_math`
  takes the `max` of two slow LFOs onto a filter cutoff — a shape
  neither LFO has alone. Try `min`, `avg` and `diff` on that jack.
- `drift_wander.json` — one [`drift`](#drift), three jobs: its `cv`
  breathes a resonant lowpass over a low saw (2.5 octaves of `cutoff_cv`),
  its `stepped` twin goes through a pentatonic [`quantizer`](#quantizer)
  into a [`pluck`](#pluck) that its `trig` fires — a note at every new
  place the wander turns towards; a second `drift` in `walk` mode, 36
  cents deep, is the old-oscillator pitch drift on the drone. A
  [`scope`](#scope) shows the wander itself.
- `bowed_cello.json` — a cello line on the [`bowed`](#bowed) string: an
  eight-step [`sequencer`](#sequencer) two octaves down through a
  [`slew`](#slew) (portamento) summed with a 5.5 Hz vibrato LFO into
  `pitch_cv`, the clock's 88% gate re-bowing every note without quite
  lifting, a slow triangle on `velocity_cv` swelling the bow, a
  [`scope`](#scope) on the bridge signal (the Helmholtz sawtooth), a hall.
  Try `pressure` 0.8 for the crunch.
- `possibility_selector_kit.json` — the router collapses: a
  [`possibility_seq`](#possibility_seq) decides *whether* each sixteenth
  hits and a clocked [`possibility_selector`](#possibility_selector)
  decides *which* drum gets it (kick 1, snare 2, closed hat 3, open hat
  4, the open steps leaning to the hats); beside it a four-string harp —
  a sparse gate into a second selector with no clock, so every *hit*
  steps an eight-note register of strings (C3 E3 G3 B3) with two notes
  left open and dealt `balanced`.
- `possibility_reroll_divider.json` — the reroll divider: one sixteenth
  clock, two chained [`clock_divider`](#clock_divider)s (`divn` 16 = a bar,
  then `div4` = four bars) and three `latch`ed
  [`possibility_seq`](#possibility_seq)s. Kick and snare hold a take for
  four bars and re-decide on the downbeat of bar 5; the hat re-deals every
  bar off the first divider. A groove you keep long enough to learn, then
  lose. Pairs with `clockwork_groove.json`.
- `clock_divider_swing.json` — swung hats from a straight clock: sixteenths
  into a [`clock_divider`](#clock_divider) (`div4` kick, `divn` 1 +
  `swing` 0.33 closed hats, `div8` open hats, `mult` 3 clocking a
  [`burst`](#burst) into a rimshot with `env → vel`), plus a slow
  [`lfo`](#lfo) into the snare [`euclidean`](#euclidean)'s `fills_cv`
  so the backbeat breathes from 2 fills to 6 and back.
- `chord_legato_inversions.json` — a held gate with roots walking under it: the
  [`chord`](#chord)'s `retrig` re-strums a maj7 in first inversion 20 ms apart on
  every root step, and the [`arpeggiator`](#arpeggiator) below it runs on its
  internal 140 × 4 clock — no clock module cabled.
- `slew_clocked_glide.json` — [`slew`](#slew) v2: a sequencer line glides 0.6 of a
  clock period per step (the clock is cabled, so the times are tempo-relative —
  change the BPM and the glide keeps its fraction) while a slow bipolar LFO into
  `rise_cv` breathes the rise speed over ±1 octave.
- `modal_mallets.json` — a stereo marimba: keys → a [`modal`](#modal) `bar`
  with `position` 0.28, `mallet` 0.55 and `spread` 0.8, `out_l`/`out_r`
  to the speakers with a [`reverb`](#reverb) off the mono `out` mixed in
  behind, and an xy [`scope`](#scope) across L/R so the spread is visible.
- `drum_dynamics.json` — the drums with feel: a [`shift_random`](#shift_random)
  (+0.3 via [`cv_offset`](#cv_offset)) into the hat's `vel` for accents and
  ghost notes, a 4-step [`sequencer`](#sequencer) into the kick's `pitch_cv`
  for a tuned 808 line with `drive` up (soft hits clean, hard ones
  saturated), a fixed 0.85 on the snare's `vel`.
- `sampler_scrub.json` — the slicer: a [`shift_random`](#shift_random)
  into a [`sampler`](#sampler)'s `start_cv` re-cuts the same drum loop on
  every sixteenth (`antialias` on), with a sparse `reverse` stab a fifth
  up off the same CV. Needs `generate_samples.py` like `sampler_breaks`.
- `organ_feedback_drone.json` — the drone machine: a slow Cm7 on the
  [`organ`](#organ) into the [`matrix_mixer`](#matrix_mixer)'s
  regenerating loop (via a 700 ms [`delay`](#delay)), with the
  [`reverb`](#reverb) outside the loop as polish. `g21` on the matrix is
  the whole patch: 0 is a dry organ, 0.65 sings, 0.9 blooms into a pad
  that keeps arriving after the note stops. Safe to crank — the
  matrix's `soft_clip` ceiling holds it at unity even with the loop
  wound past 1.0, which is exactly what makes an invitation like that
  shippable.
- `organ_shimmer.json` — the same door with an octave in it: a
  [`pitch_shifter`](#pitch_shifter) at **+12** sits *inside* the loop, so
  every lap comes back an octave higher and the chord climbs away from
  itself in stacked octaves. The clock's `pulse_width` drops to 0.45 so
  there are gaps for the cascade to bloom into, and the delay's `tone` is
  wound dark (0.3) — that roll-off is what the climb finally dies
  against, otherwise it just accumulates hiss. Measured against the same
  loop with the shift set to 0: 12× the energy one-to-two octaves up and
  ~41,000× three octaves and beyond.
- `keyboard_adsr.json`, `filter_envelope.json` — envelopes into VCA / filter.
- `two_way_crossover.json` — the crossover splitting a keyboard.
- `file_crossover_split.json` — a WAV track split and used as modulation.
- `mic_beatbox_crossover.json` — live mic, beatbox-driven.
- `resampler_tape_wobble.json` — varispeed pitch shift, LFO wobbling the pitch.
- `resampler_detune_blend.json` — resampler `mix`: +12 cents at 50% dry/wet, one-module detune thickening.
- `resampler_stereo_spread.json` — resampler `spread`: a mono saw widened into a decorrelated stereo pair (`out_l`/`out_r` → L/R speakers).
- `resampler_tape_stop.json` — resampler `brake`: a slow clock gating a true tape stop and spin-up every four seconds.
- `pitch_shifter_harmony.json` — time-preserving shift; +7 st at 50% mix = a fifth harmony.
- `pitch_shifter_formant_vowel.json` — formant-preserving shift: synthetic vowel up a fourth, timbre intact.
- `pitch_shifter_shimmer.json` — the built-in shimmer loop: a slow pluck at C3 into +12 with `feedback` 0.75, through a hall — each pluck blooms into an octave cloud.
- `pitch_shifter_harmonizer.json` — a stereo major triad from one module: `semitones` +4, `harmony` +7, `spread` 1 → third left, fifth right, root centred.
- `chorus_lush.json` — a saw pad widened into a four-voice stereo ensemble; a slow LFO drifts the chorus rate.
- `lfo_retrigger.json` — the key-synced tremolo: a 90 BPM sequencer melody through an ADSR VCA and then a 7 Hz [`lfo`](#lfo) VCA, with the sequencer's `gate` also into the LFO's `reset` and `phase` 0.25 — every note opens at the top of the tremolo and pulses down from there, instead of landing wherever the cycle happened to be.
- `filter_resonance_sweep.json` — the [`filter`](#filter)'s `resonance_cv`: a saw through a lowpass, a 0.5 Hz LFO on `cutoff_cv` and a 0.11 Hz triangle on `resonance_cv` (`res_cv_depth` 1.5, so the Q breathes between 0.7 and 5.7) — the peak sharpens and softens on its own nine-second cycle, whatever the cutoff is doing.
- `organ_leslie.json` — the pairing: a self-playing maj7 organ through the [`rotary`](#rotary), a 5 BPM clock on `fast` flipping the Leslie between chorale and tremolo every six seconds so the horn and drum chase each other.
- `krell_feedback.json` — the real krell self-patch: the [`function_generator`](#function_generator) in `trigger` mode with its own `eoc` OR'd (through [`logic`](#logic)) with a 12-second starter pulse back into `trig`. The loop closes one block late — the feedback door, generalized 2026-09-16. Same dice, quantizer and voice as `krell_machine.json`, which runs the no-latency `loop` mode version.
- `fg_eor_swell_strike.json` — what `eor` is for: a slow [`function_generator`](#function_generator) (1.2 s up, 1.8 s down, cycling through the krell loop with a wandering `rate_cv`) swells a low saw through a filter and VCA, and at the **top** of every swell its `eor` fires a [`pluck`](#pluck) whose pitch a [`sample_hold`](#sample_hold) grabbed at that same instant — swell, then strike.
- `granular_cloud.json` — a shift-register pluck melody into the [`granular`](#granular) at `pitch` +12, `density` 30, `size` 120 ms, `position` 0.15: every pluck gets an octave-up grain cloud trailing 300 ms behind it, through a hall.
- `granular_haze.json` — the cloud proper: a pluck melody into the [`granular`](#granular) with `spray_time` 1 (asynchronous), `spray_pos` 0.22, `spray_pitch` 25 ct and `width` 1 — every note dissolves into a stereo haze a quarter-second behind itself; `seed` picks the cloud.
- `granular_freeze.json` — **tap F** to freeze: a [`key_trigger`](#key_trigger) latch on the [`granular`](#granular)'s `freeze` holds the last two seconds of a pluck melody while a slow triangle [`lfo`](#lfo) on `position_cv` scans them; tap again to release and recording resumes with no hole.
- `granular_beat_repeat.json` — the drum machine into a [`granular`](#granular) cutting exact 125 ms slices (`density` 32 × `size` 62.5 ms), a [`shift_random`](#shift_random) at sixteenths on `position_cv` picking the slice point and a 15 BPM clock on `freeze`: two seconds recording, two seconds held and re-cut, forever.
- `adsr_velocity.json` — accents: a [`shift_random`](#shift_random) clocked alongside the sequencer, scaled into 0.3…1.0, into [`adsr`](#adsr)`.vel` — every step's velocity is read at its gate edge and scales the whole note; the same envelope opens a filter, so hard notes are brighter as well as louder.
- `reverb_freeze_pad.json` — the shimmer-pad trick: a strummed Cmaj7 of four [`pluck`](#pluck) strings every six seconds into a big [`reverb`](#reverb) (`size` 0.9, `decay` 0.9, `mix` 0.75), the strike [`clock`](#clock)'s [`logic`](#logic) `not_a` holding the reverb's `freeze` between strikes so the chord hangs as a pad, and a quieter sequenced pluck line playing over it — heard dry through `mix`, never piling into the tank.
- `cv_keyboard_external_voice.json` — the CV keyboard: `pitch_cv` drives an external oscillator, `key_c` triggers a separate noise voice.
- `stereo_hard_pan.json` — left/right speaker sinks.
