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
   `DEFAULT_PARAMS`, `INPUT_PORTS`, `OUTPUT_PORTS`. No DSP here — it's pure
   data.
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
(`→` separates inputs from outputs; “—” means none.)

| Module (`TYPE`) | Category | Inputs → Outputs |
|-----------------|----------|------------------|
| [`oscillator`](#oscillator) | Source | `freq_cv`,`amp_cv` (cv) → `out` (audio) |
| [`keyboard`](#keyboard) | Source | — → `out` (audio), `gate` (gate) |
| [`midi_input`](#midi_input) | Source | — → `out` (audio), `gate`, `pitch_cv`, `mod_cv`, `pressure_cv` |
| [`file_player`](#file_player) | Source | — → `left`,`right` (audio) |
| [`mic_input`](#mic_input) | Source | — → `left`,`right` (audio) |
| [`cv_to_frequency`](#cv_to_frequency) | Source | `cv` (cv) → `out` (audio) |
| [`filter`](#filter) | Processor | `in` (audio), `cutoff_cv` (cv) → `out` (audio) |
| [`crossover`](#crossover) | Processor | `in` (audio) → `low`,`high` (audio) |
| [`vca`](#vca) | Processor | `audio` (audio), `cv` (cv) → `out` (audio) |
| [`lfo`](#lfo) | Modulation | `rate_cv` (cv) → `cv` (cv) |
| [`adsr`](#adsr) | Modulation | `gate` (gate) → `cv` (cv) |
| [`audio_to_cv`](#audio_to_cv) | Bridge | `in` (audio) → `cv` (cv) |
| [`cv_to_audio`](#cv_to_audio) | Bridge | `cv` (cv) → `out` (audio) |
| [`schmitt`](#schmitt) | Bridge | `in` (cv) → `gate` (gate) |
| [`mixer`](#mixer) | Routing | `in1`–`in4` (audio) → `out` (audio) |
| [`combiner`](#combiner) | Routing | `in1`–`in4` (audio) → `out` (audio) |
| [`cv_combiner`](#cv_combiner) | Routing | `in1`–`in4` (cv) → `out` (cv) |
| [`speaker_output`](#speaker_output) | Sink | `in` (audio) → — |
| [`left_speaker_output`](#left_speaker_output) | Sink | `in` (audio) → — |
| [`right_speaker_output`](#right_speaker_output) | Sink | `in` (audio) → — |
| [`disk_writer`](#disk_writer) | Sink | `in` (audio) → — |

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
(audio) and `gate`. Params: `octave`, `waveform`, `volume`. See
`examples/keyboard_play.json`, `examples/keyboard_adsr.json`.

#### `midi_input`

_To document._ Hardware-MIDI note source (polyphonic), `[midi]` extra
required. Outputs `out`, `gate`, `pitch_cv`, `mod_cv`, `pressure_cv`. Params
include `device`, `channel`, `octave_shift`, `velocity_sensitive`,
`bend_range`, `mod_scale`, `pressure_scale`. See `examples/midi_lead.json`.

#### `file_player`

Streams a **WAV file** into the patch as a stereo audio source — so a recorded
track can be split and used as sound or modulation. Decoded once into memory
(resampled to the engine rate if needed), then streamed block by block.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `left` | out | audio | Left channel. A mono file is duplicated to both; >2 channels keep the first two. |
| `right` | out | audio | Right channel. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `path` | `""` | file path | Path to a `.wav`. Empty/missing/unreadable → silence (the patch still loads). **WAV only.** |
| `gain` | `1.0` | 0…2 | Linear gain on both channels. |
| `loop` | `false` | bool | `true` repeats seamlessly; `false` (default) plays once then silence until restart/re-arm. |
| `armed` | `true` | bool | `false` outputs silence and parks the playhead at the start, so re-arming replays from the top. |

**Notes.** The node shows a live `elapsed / total` time readout. One-shots
rewind when the transport stops. See `examples/file_crossover_split.json`
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
| `device` | `""` | input device name | `""` = system default input. The UI offers a dropdown of capture devices (snapshotted when the node is created — reopen the patch to refresh after hot-plugging). |
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

### Processors

Modules that take audio in and shape it.

#### `filter`

A resonant biquad filter (Robert Bristow-Johnson coefficients) — lowpass,
highpass, or bandpass, with CV-modulatable cutoff.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to filter. |
| `cutoff_cv` | in | cv | 1 volt/octave cutoff modulation (`cutoff · 2^cv`). Patch an envelope or LFO here for sweeps. |
| `out` | out | audio | Filtered signal. |

**Parameters**

| Param | Default | Options / range | Description |
|-------|---------|-----------------|-------------|
| `mode` | `lowpass` | `lowpass`, `highpass`, `bandpass` | Filter response. |
| `cutoff` | `1000.0` | ~20…20000 Hz | Corner/center frequency when `cutoff_cv` is unpatched. |
| `resonance` | `0.707` | ~0.1…15 | Q. `0.707` is flat (no peak); higher emphasises the cutoff and can self-oscillate-ish. |

**Patching.** Classic: `oscillator → filter → vca`, with an `adsr → cutoff_cv`
for a filter sweep. See `examples/filter_envelope.json`, `examples/wah.json`.

#### `crossover`

Splits one audio input into **low** and **high** bands at a chosen frequency
— a 4th-order Linkwitz-Riley split whose bands sum back flat.

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to split. |
| `low` | out | audio | Everything below `frequency`. |
| `high` | out | audio | Everything above `frequency`. |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `frequency` | `1000.0` | ~20 … 0.45·sample-rate Hz | Crossover corner. |

**Patching.** Feed `low`/`high` into separate chains, or back into a
[Combiner](#combiner) to reconstruct the input. Pairs beautifully with
[AudioToCV](#audio_to_cv) to turn each band into a modulation source — see
`examples/two_way_crossover.json`, `examples/file_crossover_split.json`,
`examples/mic_beatbox_crossover.json`.

#### `vca`

_To document._ Voltage-controlled amplifier: multiplies `audio` by `cv`
(makes an ADSR audible). **Note the port names: `audio` and `cv`, not `in`.**
Param: `gain`. See `examples/keyboard_adsr.json`.

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

#### `lfo`

_To document._ Low-frequency oscillator as a `cv` source (sine/tri/square/
saw/random), with optional `rate_cv` for FM-of-modulation. Params:
`waveform`, `rate`, `depth`, `bipolar`. See `examples/vibrato.json`,
`examples/keyboard_tremolo.json`.

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

_To document._ Four audio inputs with per-channel gains plus a master.
Params: `gain1`–`gain4`, `master`. Outputs `out`.

#### `combiner`

_To document._ Four audio inputs summed to one output (no per-channel gain).
Use to recombine crossover bands or sum voices.

#### `cv_combiner`

_To document._ Four `cv` inputs summed or averaged (param `mode`: `sum` /
`average`) — lets an LFO and an ADSR modulate the same destination. See
`examples/mod_matrix.json`.

---

### Sinks (outputs)

The end of a patch — where signal leaves the graph. Sinks have no outputs.

#### `speaker_output`

_To document._ Routes its `in` to both system output channels. Param: `gain`.
The default destination in most example patches.

#### `left_speaker_output`

_To document._ Routes `in` to the **left** channel only. Param: `gain`. Pair
with `right_speaker_output` for hard-panned stereo. See
`examples/stereo_hard_pan.json`.

#### `right_speaker_output`

_To document._ Routes `in` to the **right** channel only. Param: `gain`.

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
- `stereo_hard_pan.json` — left/right speaker sinks.
