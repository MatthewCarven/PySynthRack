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
| `vca.cv` | — (multiplier) | linear | `audio · cv · gain` |
| `filter.cutoff_cv` | `1.0` | octaves | `cutoff · 2^(d·mean cv)` |
| `lfo.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `crossover.freq_cv` | `1.0` | octaves | `freq · 2^(d·mean cv)` |
| `sweep_eq.freq_cv` | `1.0` | octaves | `freq · 2^(d·mean cv)` |
| `motion_eq.band{i}_freq_cv` | `1.0` (shared) | octaves | `freq_i · 2^(d·mean cv)` |
| `motion_eq.band{i}_gain_cv` | `6.0` (shared, `gain_cv_depth`) | dB | `gain_i + d·mean cv` (clamped ±24) |
| `motion_eq.band{i}_q_cv` | `1.0` (shared, `q_cv_depth`) | Q doublings | `q_i · 2^(d·mean cv)` (clipped 0.1…20) |
| `chorus.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `flanger.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `phaser.rate_cv` | `1.0` | octaves | `rate · 2^(d·mean cv)` |
| `resampler.pitch_cv` | `12.0` | semitones | `st + d·cv` (semitone space) |
| `pitch_shifter.pitch_cv` | `12.0` | semitones | `st + d·mean cv` |
| `delay.time_cv` | `50.0` | ms | `time + d·cv` |
| `loudness.level_cv` | `1.0` | level (0…1) | `level + d·mean cv` |
| `tilt_eq.tilt_cv` | `6.0` | dB | `tilt + d·mean cv` |
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
| [`oscillator`](#oscillator) | Sources | `freq_cv`,`amp_cv` (cv) → `out` (audio) |
| [`keyboard`](#keyboard) | Sources | — → `out` (audio), `gate` (gate) |
| [`cv_keyboard`](#cv_keyboard) | Sources | — → `pitch_cv` (cv), `gate`, `key_c`…`key_b` (gate) |
| [`cv_gates`](#cv_gates) | Sources | — → `c4`…`e5` (cv, one enveloped gate per key) |
| [`midi_input`](#midi_input) | Sources | — → `out` (audio), `gate`, `pitch_cv`, `mod_cv`, `pressure_cv` |
| [`file_player`](#file_player) | Sources | — → `left`,`right` (audio) |
| [`mic_input`](#mic_input) | Sources | — → `left`,`right` (audio) |
| [`cv_to_frequency`](#cv_to_frequency) | Sources | `cv` (cv) → `out` (audio) |
| [`noise`](#noise) | Sources | — → `out` (audio), `cv` (cv) |
| [`filter`](#filter) | Filters & EQ | `in` (audio), `cutoff_cv` (cv) → `out` (audio) |
| [`crossover`](#crossover) | Filters & EQ | `in` (audio), `freq_cv` (cv) → `low`,`high` (audio) |
| [`parametric_eq`](#parametric_eq) | Filters & EQ | `in` (audio) → `out` (audio) |
| [`sweep_eq`](#sweep_eq) | Filters & EQ | `in` (audio), `freq_cv` (cv) → `out` (audio) |
| [`motion_eq`](#motion_eq) | Filters & EQ | `in` (audio), `band{i}_freq_cv`, `band{i}_gain_cv`, `band{i}_q_cv` ×4 (cv) → `out` (audio) |
| [`tilt_eq`](#tilt_eq) | Filters & EQ | `in` (audio), `tilt_cv` (cv) → `out` (audio) |
| [`vca`](#vca) | Routing & VCA | `audio` (audio), `cv` (cv) → `out` (audio) |
| [`resampler`](#resampler) | Effects | `in` (audio), `pitch_cv` (cv) → `out` (audio) |
| [`pitch_shifter`](#pitch_shifter) | Effects | `in` (audio), `pitch_cv` (cv) → `out` (audio) |
| [`delay`](#delay) | Effects | `in` (audio), `time_cv` (cv) → `out` (audio) |
| [`reverb`](#reverb) | Effects | `in` (audio), `decay_cv`,`damping_cv`,`mix_cv` (cv) → `out_l`,`out_r` (audio) |
| [`loudness`](#loudness) | Filters & EQ | `in` (audio), `level_cv` (cv) → `out` (audio) |
| [`distortion`](#distortion) | Effects | `in` (audio), `drive_cv` (cv) → `out` (audio) |
| [`waveshaper`](#waveshaper) | Effects | `in` (audio), `fold_cv` (cv) → `out` (audio) |
| [`chorus`](#chorus) | Effects | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`flanger`](#flanger) | Effects | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`phaser`](#phaser) | Effects | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`lfo`](#lfo) | Modulation | `rate_cv` (cv) → `cv` (cv) |
| [`adsr`](#adsr) | Modulation | `gate` (gate) → `cv` (cv) |
| [`ad_envelope`](#ad_envelope) | Modulation | `trig` (gate) → `cv` (cv) |
| [`clock`](#clock) | Modulation | — → `out` (gate) |
| [`sequencer`](#sequencer) | Modulation | `clock`,`reset` (gate) → `cv` (cv), `gate` (gate) |
| [`fader_seq`](#fader_seq) | Modulation | `clock`,`reset` (gate) → `cv` (cv), `gate` (gate) |
| [`audio_to_cv`](#audio_to_cv) | CV & Utilities | `in` (audio) → `cv` (cv) |
| [`cv_to_audio`](#cv_to_audio) | CV & Utilities | `cv` (cv) → `out` (audio) |
| [`schmitt`](#schmitt) | CV & Utilities | `in` (cv) → `gate` (gate) |
| [`mixer`](#mixer) | Routing & VCA | `in1`–`in4` (audio), `gain1_cv`–`gain4_cv` (cv) → `out` (audio) |
| [`combiner`](#combiner) | Routing & VCA | `in1`–`in4` (audio) → `out` (audio) |
| [`cv_combiner`](#cv_combiner) | Routing & VCA | `in1`–`in4` (cv) → `out` (cv) |
| [`constant`](#constant) | CV & Utilities | — → `out` (cv) |
| [`cv_scale`](#cv_scale) | CV & Utilities | `in` (cv) → `out` (cv) |
| [`cv_offset`](#cv_offset) | CV & Utilities | `in` (cv) → `out` (cv) |
| [`sample_hold`](#sample_hold) | CV & Utilities | `in` (cv), `trig` (gate) → `out` (cv) |
| [`meter`](#meter) | CV & Utilities | `in`, `in_r` (audio) → `out`, `out_r` (audio) |
| [`speaker_output`](#speaker_output) | Outputs | `in` (audio) → — |
| [`left_speaker_output`](#left_speaker_output) | Outputs | `in` (audio) → — |
| [`right_speaker_output`](#right_speaker_output) | Outputs | `in` (audio) → — |
| [`stereo_speaker_output`](#stereo_speaker_output) | Outputs | `in_l`,`in_r` (audio), `pan_cv`,`width_cv` (cv) → — |
| [`disk_writer`](#disk_writer) | Outputs | `in` (audio) → — |

---

### Sources

Modules that generate or bring in signal — the start of a patch.

#### `oscillator`

The workhorse tone generator: a periodic waveform at a chosen pitch, with
optional CV modulation of pitch and amplitude.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `freq_cv` | in | cv | 1 volt/octave pitch modulation. `freq` becomes `freq · 2^cv` per sample, so a bipolar LFO here is vibrato and an audio-rate signal is FM. Unpatched = no modulation. |
| `amp_cv` | in | cv | Linear amplitude modulation (`amp · cv`). A unipolar LFO here is tremolo/AM. Unpatched = no modulation. |
| `out` | out | audio | The waveform. |

**Parameters**

| Param | Default | Options / range | Description |
|-------|---------|-----------------|-------------|
| `waveform` | `sine` | `sine`, `saw`, `square`, `triangle`, plus `*_blep` and `*_wt` variants of saw/square/triangle | Shape + band-limiting. Naive shapes are cheap but alias; `_blep` (PolyBLEP/PolyBLAMP) and `_wt` (band-limited wavetable) are anti-aliased. `sine` is already band-limited. |
| `freq` | `440.0` | Hz | Base pitch when `freq_cv` is unpatched. |
| `amp` | `0.5` | 0…1 | Linear output level. |

**Patching.** The canonical voice is `oscillator → filter → vca → speaker`,
with an `adsr` driving the VCA's `cv`. See `examples/hello_sine.json` and
`examples/fat_saw.json`.

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
| `path` | `""` | file path | Path to an audio/video file — type it or use the node's **Browse...** button. WAV always works; other formats (mp3/flac/ogg, video-audio) need ffmpeg. Empty/missing/unreadable → silence (the patch still loads). |
| `gain` | `1.0` | 0…2 | Linear gain on both channels. |
| `loop` | `false` | bool | `true` repeats seamlessly; `false` (default) plays once then silence until restart/re-arm. |
| `armed` | `true` | bool | `false` outputs silence and parks the playhead at the start, so re-arming replays from the top. |
| `playing` | `true` | bool | Tape-transport pause: `false` holds the playhead in place (silent); `true` resumes from the same spot. Driven by the node's **Play**/**Stop** buttons. |

**Transport.** The node carries tape-style buttons: **Play** resumes,
**Stop** pauses in place (both drive the `playing` param and its checkbox),
and **|<** rewinds to 0:00 — honoured at the next block boundary whether
playing or paused. `armed` remains the coarser control (off = silent *and*
parked at the start). One-shots also rewind when the audio transport stops.

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

### Processors

Modules that take audio in and shape it.

#### `filter`

A resonant biquad filter (Robert Bristow-Johnson coefficients) — lowpass,
highpass, or bandpass, with CV-modulatable cutoff.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to filter. |
| `cutoff_cv` | in | cv | Sweeps the cutoff, `cv_depth` octaves per CV unit (default 1.0 = 1 V/oct: `cutoff · 2^(cv_depth·cv)`). Patch an envelope or LFO here for sweeps. |
| `out` | out | audio | Filtered signal. |

**Parameters**

| Param | Default | Options / range | Description |
|-------|---------|-----------------|-------------|
| `mode` | `lowpass` | `lowpass`, `highpass`, `bandpass` | Filter response. |
| `cutoff` | `1000.0` | ~20…20000 Hz | Corner/center frequency when `cutoff_cv` is unpatched. |
| `resonance` | `0.707` | ~0.1…15 | Q. `0.707` is flat (no peak); higher emphasises the cutoff and can self-oscillate-ish. |
| `cv_depth` | `1.0` | 0…4 oct/unit | Octaves the cutoff moves per `cutoff_cv` unit. Default 1 V/oct (pre-2026-07-02 fixed behaviour); 0 disables. |

**Patching.** Classic: `oscillator → filter → vca`, with an `adsr → cutoff_cv`
for a filter sweep. See `examples/filter_envelope.json`, `examples/wah.json`.

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
| `out` | out | audio | The resampled signal. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `semitones` | `0.0` | −24 … +24 st | Coarse transpose (C→D = +2). 0 = unity. |
| `cents` | `0.0` | −100 … +100 ct | Fine-tune, added to `semitones`. |
| `cv_depth` | `12.0` | 0 … 48 st/unit | Semitones per unit of `pitch_cv` (12 = one octave per unit, 1V/oct-style). |
| `glide` | `0.0` | 0 … 5 s | Portamento time for pitch changes (0 = instant). |
| `mix` | `1.0` | 0 … 1 | Dry/wet blend. The dry tap is latency-compensated to line up with unity-pitch wet, so the blend is coherent (no slapback). |
| `window` | `200.0` | 20 … 2000 ms | Looping-buffer window. Latency is half of it, so shorter = tighter live latency but a stronger loop texture; longer = subtler texture on big shifts. Floored at 4 audio blocks; live changes keep the recent audio (no dropout). |

**How it works.** Pitch is summed in semitone space
(`st = semitones + cents/100 + cv_depth · pitch_cv`), optionally glided
with a one-pole, then exponentiated to a playback ratio
`2^(st/12)`. The read head advances by that ratio per output sample
with linear interpolation. Because a resampler reading at a different
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

For pitch shifting that keeps the *speed* fixed you'd want a granular
or phase-vocoder engine — a heavier build for later. This one is
deliberately the tape kind.

**Patching.** `oscillator → resampler → speaker` to transpose a tone,
or feed the [FilePlayer](#file_player) in to pitch a sample. Wire an
[LFO](#lfo) into `pitch_cv` for vibrato/tape-wobble, or an
[ADSR](#adsr)/[AD](#ad_envelope) for pitch dives; raise `glide` for
portamento and tape-stop sweeps. Set `mix` to ~0.5 with a few `cents`
of detune for one-module chorus-style thickening. See
`examples/resampler_tape_wobble.json` (saw → varispeed with a slow LFO
wobbling the pitch → speaker) and `examples/resampler_detune_blend.json`
(+12 ct at 50% mix → detune thickening).

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
| `pitch_cv` | in | cv | Added to the transpose, scaled by `cv_depth` (summed in semitone space, sampled per block). |
| `out` | out | audio | The pitch-shifted signal. |

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
(saw → +7 st at 50% mix → speaker: a self-playing fifth).

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
| `cv` | out | cv | The envelope, 0…1 (sustain level held while the gate is high). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `attack` | `0.01` | 0…5 s | Time to rise from 0 to 1 after gate-on. |
| `decay` | `0.1` | 0…5 s | Time to fall from 1 to the sustain level. |
| `sustain` | `0.7` | 0…1 | Level held while the gate stays high. |
| `release` | `0.3` | 0…5 s | Time to fall from sustain to 0 after gate-off. |

**Patching.** `keyboard.gate → adsr.gate`, then `adsr.cv → vca.cv`. See
`examples/keyboard_adsr.json`, `examples/filter_envelope.json`.

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

#### `lfo`

_To document._ Low-frequency oscillator as a `cv` source (sine/tri/square/
saw/random), with optional `rate_cv` for FM-of-modulation (`cv_depth`
octaves per CV unit, default 1.0 = 1 V/oct, 0 disables). Params:
`waveform`, `rate`, `depth`, `bipolar`, `cv_depth`. See
`examples/vibrato.json`, `examples/keyboard_tremolo.json`.

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
oscillator.amp_cv`. See `examples/envelope_follower_wah.json`.

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

#### `disk_writer`

_To document._ Records its `in` to a 16-bit mono WAV while the transport runs
(threaded, so it never glitches the audio). Params: `path`, `armed`. See
`examples/record_a_take.json`.

---

## Appendix: example patches

The `examples/` folder is the fastest way to learn a module — each `.json`
loads in the app. Notable ones referenced above:

- `hello_sine.json`, `fat_saw.json` — basic oscillators.
- `keyboard_adsr.json`, `filter_envelope.json` — envelopes into VCA / filter.
- `two_way_crossover.json` — the crossover splitting a keyboard.
- `file_crossover_split.json` — a WAV track split and used as modulation.
- `mic_beatbox_crossover.json` — live mic, beatbox-driven.
- `resampler_tape_wobble.json` — varispeed pitch shift, LFO wobbling the pitch.
- `resampler_detune_blend.json` — resampler `mix`: +12 cents at 50% dry/wet, one-module detune thickening.
- `pitch_shifter_harmony.json` — time-preserving shift; +7 st at 50% mix = a fifth harmony.
- `pitch_shifter_formant_vowel.json` — formant-preserving shift: synthetic vowel up a fourth, timbre intact.
- `chorus_lush.json` — a saw pad widened into a four-voice stereo ensemble; a slow LFO drifts the chorus rate.
- `cv_keyboard_external_voice.json` — the CV keyboard: `pitch_cv` drives an external oscillator, `key_c` triggers a separate noise voice.
- `stereo_hard_pan.json` — left/right speaker sinks.
