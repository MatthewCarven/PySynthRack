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
| [`cv_keyboard`](#cv_keyboard) | Source | — → `pitch_cv` (cv), `gate`, `key_c`…`key_b` (gate) |
| [`cv_gates`](#cv_gates) | Source | — → `c4`…`e5` (cv, one enveloped gate per key) |
| [`midi_input`](#midi_input) | Source | — → `out` (audio), `gate`, `pitch_cv`, `mod_cv`, `pressure_cv` |
| [`file_player`](#file_player) | Source | — → `left`,`right` (audio) |
| [`mic_input`](#mic_input) | Source | — → `left`,`right` (audio) |
| [`cv_to_frequency`](#cv_to_frequency) | Source | `cv` (cv) → `out` (audio) |
| [`noise`](#noise) | Source | — → `out` (audio), `cv` (cv) |
| [`filter`](#filter) | Processor | `in` (audio), `cutoff_cv` (cv) → `out` (audio) |
| [`crossover`](#crossover) | Processor | `in` (audio) → `low`,`high` (audio) |
| [`parametric_eq`](#parametric_eq) | Processor | `in` (audio) → `out` (audio) |
| [`vca`](#vca) | Processor | `audio` (audio), `cv` (cv) → `out` (audio) |
| [`resampler`](#resampler) | Processor | `in` (audio), `pitch_cv` (cv) → `out` (audio) |
| [`pitch_shifter`](#pitch_shifter) | Processor | `in` (audio), `pitch_cv` (cv) → `out` (audio) |
| [`delay`](#delay) | Processor | `in` (audio), `time_cv` (cv) → `out` (audio) |
| [`reverb`](#reverb) | Processor | `in` (audio) → `out_l`,`out_r` (audio) |
| [`loudness`](#loudness) | Processor | `in` (audio), `level_cv` (cv) → `out` (audio) |
| [`chorus`](#chorus) | Processor | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`flanger`](#flanger) | Processor | `in` (audio), `rate_cv` (cv) → `out_l`,`out_r` (audio) |
| [`lfo`](#lfo) | Modulation | `rate_cv` (cv) → `cv` (cv) |
| [`adsr`](#adsr) | Modulation | `gate` (gate) → `cv` (cv) |
| [`ad_envelope`](#ad_envelope) | Modulation | `trig` (gate) → `cv` (cv) |
| [`clock`](#clock) | Modulation | — → `out` (gate) |
| [`sequencer`](#sequencer) | Modulation | `clock`,`reset` (gate) → `cv` (cv), `gate` (gate) |
| [`audio_to_cv`](#audio_to_cv) | Bridge | `in` (audio) → `cv` (cv) |
| [`cv_to_audio`](#cv_to_audio) | Bridge | `cv` (cv) → `out` (audio) |
| [`schmitt`](#schmitt) | Bridge | `in` (cv) → `gate` (gate) |
| [`mixer`](#mixer) | Routing | `in1`–`in4` (audio) → `out` (audio) |
| [`combiner`](#combiner) | Routing | `in1`–`in4` (audio) → `out` (audio) |
| [`cv_combiner`](#cv_combiner) | Routing | `in1`–`in4` (cv) → `out` (cv) |
| [`constant`](#constant) | Utility | — → `out` (cv) |
| [`cv_scale`](#cv_scale) | Utility | `in` (cv) → `out` (cv) |
| [`cv_offset`](#cv_offset) | Utility | `in` (cv) → `out` (cv) |
| [`sample_hold`](#sample_hold) | Utility | `in` (cv), `trig` (gate) → `out` (cv) |
| [`meter`](#meter) | Utility | `in` (audio) → `out` (audio) |
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

_To document._ Hardware-MIDI note source (polyphonic), `[midi]` extra
required. Outputs `out`, `gate`, `pitch_cv`, `mod_cv`, `pressure_cv`. Params
include `device`, `channel`, `octave_shift`, `velocity_sensitive`,
`bend_range`, `mod_scale`, `pressure_scale`. See `examples/midi_lead.json`.

#### `file_player`

Streams an **audio file** into the patch as a stereo audio source — so a
recorded track can be split and used as sound or modulation. WAV always
works (no extra deps); with ffmpeg present it also reads mp3/flac/ogg/m4a
and the **audio track of video files** (mp4/mkv/mov/webm). Decoded once
into memory (resampled to the engine rate if needed), then streamed block
by block.

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

**Notes.** A **Browse...** button beside the path field opens a file picker
(audio + video formats) and writes the chosen path back into the field; the
player re-decodes on the next block. Non-WAV formats are decoded by ffmpeg,
found either from the `[media]` extra (`pip install -e ".[media]"`, a
bundled binary that also travels inside the packaged exe) or a system
`ffmpeg` on PATH; without ffmpeg, non-WAV files play silence. The node also
shows a live `elapsed / total` time readout. One-shots rewind when the
transport stops. See `examples/file_crossover_split.json`
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

**How it works.** Pitch is summed in semitone space
(`st = semitones + cents/100 + cv_depth · pitch_cv`), optionally glided
with a one-pole, then exponentiated to a playback ratio
`2^(st/12)`. The read head advances by that ratio per output sample
with linear interpolation. Because a resampler reading at a different
rate than it's fed can't stay in sync with a continuous stream
forever, it runs a short **looping buffer** of recent audio: the read
head wraps inside the window, so the module keeps sounding indefinitely
on any live source (oscillator, mic, file player), at the cost of a
faint granular-repeat texture on extreme shifts. That buffer also means
a fixed latency (~90 ms) — the unavoidable price of varispeed on a live
signal, and what lets you glide and modulate the pitch freely. The path
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
portamento and tape-stop sweeps. See `examples/resampler_tape_wobble.json`
(saw → varispeed with a slow LFO wobbling the pitch → speaker).

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
| `out_l` | out | audio | Left channel (dry + decorrelated wet). |
| `out_r` | out | audio | Right channel (dry + decorrelated wet). |

**Parameters**

| Param | Default | Range | Description |
|-------|---------|-------|-------------|
| `size` | `0.5` | 0 … 1 | Delay-line lengths: small room → large hall. |
| `decay` | `0.5` | 0 … 1 | Tail length (reverberation time), short → long. |
| `damping` | `0.5` | 0 … 1 | High-frequency absorption in the tail, bright → dark. |
| `mix` | `0.3` | 0 … 1 | Dry/wet balance. Dry is centred; wet is the stereo tail. |

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

Patch the outputs into [`left_speaker_output`](#left_speaker_output) and
[`right_speaker_output`](#right_speaker_output). Unlike the chorus, the
flanger's feedback makes each output sample depend on one just written, so
the comb runs **per-sample** (the delay's short-time path) — but the LFO
phase and ring state carry across blocks, so the render is still exactly
**block-size independent** (bit-identical at 512 / 4096 / 333). This is a
**standard** (positive-delay) flanger; the delay never crosses zero, so
through-zero "tape" flanging is a planned extension. See
`examples/flanger_jet_sweep.json` (a self-playing saw riff swept by the
flanger, with a slow LFO drifting the sweep rate through `rate_cv`).

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
watch a level. The node shows the signal's **recent peak in dBFS**
(a fixed −90 → 0 scale, so two meters read on the same reference and
are directly comparable — handy for eyeballing, say, a MicInput
against a FilePlayer before they hit a mixer).

**Ports**

| Port | Dir | Kind | Description |
|------|-----|------|-------------|
| `in` | in | audio | Signal to measure. |
| `out` | out | audio | The input, passed through unchanged. |

**Parameters.** None — it only observes.

**How it works.** The reading is a fast-attack / slow-decay peak
envelope (max |sample| over the block, instant rise, gentle fall)
computed on the audio thread, so a short transient registers even
between UI repaints and the meter latency is block-rate, not
frame-rate. Shape-polymorphic: a voice-aware `(V, F)` input shows the
loudest voice. See `examples/meter_levels.json` (a loud saw and a
quiet square, each through its own meter — the bars read clearly
different levels).

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
- `resampler_tape_wobble.json` — varispeed pitch shift, LFO wobbling the pitch.
- `pitch_shifter_harmony.json` — time-preserving shift; +7 st at 50% mix = a fifth harmony.
- `chorus_lush.json` — a saw pad widened into a four-voice stereo ensemble; a slow LFO drifts the chorus rate.
- `cv_keyboard_external_voice.json` — the CV keyboard: `pitch_cv` drives an external oscillator, `key_c` triggers a separate noise voice.
- `stereo_hard_pan.json` — left/right speaker sinks.
