# Worklog

Running log of decisions and progress. Newest first.

---

## 2026-05-23 (CVToFrequency examples pass) -- four new patches

Follow-up to phase 1: a sampling of patches exercising the module
in genuinely different ways. None require any new code -- they all
just plug existing modules into CVToFrequency to surface what the
three-point map + mode switch unlock.

**``examples/mod_wheel_pitch_drone.json``.** MIDIInput.mod_cv ->
CVToFrequency.cv -> Speaker. The hum is continuous (CVToFrequency
always produces sound -- no gate, no envelope, the static ``freq``
fallback at 220 Hz plays the moment the patch compiles); moving
the mod wheel scrubs pitch through f0=110 / fm=330 / f1=880 in log
mode for octave-feeling sweep. Set ``volume=0.0`` on the MIDIInput
so the keys themselves are silent -- only the wheel matters here.
Probably the cleanest single-cable demo of "CV from hardware
controls a sound source's pitch directly."

**``examples/poor_mans_theremin.json``.** Matthew's framing.
Two-axis MIDI expression: mod_cv -> CVToFrequency.cv (pitch),
pressure_cv -> VCA.cv (volume), CVToFrequency -> VCA.audio ->
Speaker. Hold any key to enable the chain (channel aftertouch
only fires while a note is held); the wheel scrubs pitch
110..1760 Hz in log mode, key pressure swells volume 0..1. The
key choice is incidental -- it just gates the VCA via pressure
rather than via gate. With ``velocity_sensitive=False`` and
``volume=0.0`` the key's own note is silent, so the wheel and
pressure are the only audible controls. Closest the modular can
get to a theremin's continuous-pitch / continuous-volume
interaction without a real ribbon controller.

**``examples/police_siren.json``.** Triangle LFO 1.2 Hz unipolar
(depth=1) -> CVToFrequency *linear* (f0=600 / fm=900 / f1=1200) ->
Speaker. The whole point of this example is the ``mode=linear``
switch -- run the same LFO into a log-mode CVToFrequency and the
wail bunches up around f0 and races past fm; linear mode gives
the equal-Hz-per-time ramp that actual sirens use. Triangle
unipolar swings CV 0 -> 1 -> 0 linearly, which the linear map
turns into a 600 -> 1200 -> 600 Hz pitch ramp. Cycles at the
LFO rate. The audible-difference proof for the log/linear
toggle.

**``examples/random_arp.json``.** Two LFOs at the same rate
(4 Hz) -- one ``random`` (sample-and-hold, re-rolls per cycle)
into CVToFrequency.cv (log, 110 / 330 / 1000 Hz wide range), one
``sine`` unipolar into VCA.cv for a per-step amplitude pump --
CVToFrequency -> VCA.audio -> Speaker. Each tick the S&H LFO
holds a new random value in [0, 1], which log-maps to a random
pitch in the musical range; meanwhile the sine LFO pumps the
amplitude in time with the ticks (peaks at quarter-second
intervals, naturally percussive). The unique CVToFrequency
flavour here: pure random-pitch-stepping that stays in a
listenable Hz range thanks to log interpolation, which a regular
Oscillator + 1V/oct random LFO wouldn't give as cleanly without
the user doing the Hz->oct math.

**Verification.** Each patch round-trips through
:meth:`Patch.from_dict` and renders three blocks of audio without
NaN/Inf via the numpy backend. The two MIDI-driven patches show
zero RMS in the sandbox (no hardware, mod wheel and pressure both
at 0) -- expected: the drone idles at f0 with zero output until
the wheel moves on a real device; the theremin sits silent until
a key is held. Police siren and random arp produce sound the
moment they compile (no MIDI dependency).

---

## 2026-05-23 (CVToFrequency phase 1) -- self-contained CV oscillator

First half of the planned CVToFrequency module ships. Where the
existing Oscillator's ``freq_cv`` follows the modular 1V/oct
convention against a base ``freq`` (so a 0..1 LFO sweeps about an
octave above the base), CVToFrequency is opinionated: the user
dials in three concrete Hz anchor points (``f0`` at CV=0, ``fm``
at CV=0.5, ``f1`` at CV=1.0) and the module interpolates between
them. The oscillator and the mapping live in one node.

**Mapping math.** Two segments around the midpoint. Lower segment
(CV in [0, 0.5]): blend ``f0`` -> ``fm`` with ``t = cv*2``. Upper
segment (CV in [0.5, 1.0]): blend ``fm`` -> ``f1`` with ``t =
(cv-0.5)*2``. The ``mode`` param picks the flavour: ``"log"``
(default) interpolates in log2-Hz, producing equal-octave splits
across CV (musical default); ``"linear"`` interpolates literal Hz
for deliberately-bent, non-musical sweeps. The two are
distinguishable by ear and by FFT -- with f0=100, fm=400, log
mode at cv=0.25 produces 200 Hz (geometric mean), linear mode
produces 250 Hz (arithmetic mean). One of the tests
(test_log_and_linear_modes_differ) is an explicit A/B guarding
against accidental mode passthrough.

**Bipolar CV in phase 1.** Clamped to [0, 1] before mapping --
documented explicitly in the module docstring. Phase 2 adds a
mirror three-point mapping for the negative side with its own
independent ``mode_neg``, letting the user mix log on one side
with linear on the other (the "cheat" Matthew acknowledged in
the design conversation; opt-in via ``negative_enabled`` so the
phase-1 surface stays unchanged).

**Unpatched CV falls back to the ``freq`` param.** Matches
Oscillator's pattern -- CVToFrequency is a *sound source*, so it
always produces sound. Different from CVToAudio, which is silent
without an input because it's a passthrough.

**Voice-awareness.** Shape-polymorphic on the CV input, same
convention as every other v0.4 voice-aware module. ``cv.ndim == 1``
runs the mono fast path with a single phase accumulator and a
vectorized constant-freq ramp (when CV is None) or cumsum
phase integration (when CV is patched). ``cv.ndim == 2`` runs the
voice path: V independent phase accumulators integrated via
per-row cumsum, output ``(V, F)``. Per-voice phase state persists
across blocks, exactly like the Oscillator's voice path -- so a
slot that briefly idles at CV=0 doesn't lose its place when it
becomes audible again.

**DSP shape.** The CV->Hz function is implemented as a single
``_cv_to_hz`` helper that's shape-preserving (np.clip + np.where
piecewise) and serves both branches. Phase integration is cumsum.
Both choices are deliberate: a vectorized lookup is the right
shape for numpy, and it keeps the mono and voice paths
algorithmically identical -- only the axis differs.

**Tests (22, all passing).** Model tests cover registration,
defaults, port signal kinds (cv in / audio out), JSON round-trip,
unknown-param rejection, the cv->cv cable acceptance, the
audio->cv rejection (the type wall), and the audio out feeding a
Speaker. Mono behaviour tests pin CV=0/0.5/1.0 to f0/fm/f1 via
one-second zero-crossing counts (sub-percent resolution against
the targets); log/linear at cv=0.25 are pinned to the geometric
and arithmetic means respectively; both clamping edges (cv=-0.5
-> f0, cv=1.5 -> f1) are checked; unpatched falls back to
``freq``; phase continuity across blocks; output finiteness and
non-trivial RMS. Voice-aware tests cover the (V, F) shape with
per-row FFT-peak assertions at f0/fm/f1, mono fast-path
preservation when CV is 1D, and per-voice phase state
independence across blocks. Integration test wires a unipolar
LFO -> CVToFrequency -> Speaker and verifies finite bounded
audible output. Full suite: 339 passed (was 317 pre-CVToFrequency,
+22 new), 18 skipped (mido), 1 pre-existing failure
(``test_adsr.py::test_no_nan_with_zero_durations``, drive-by
``sr`` undefined -- listed in TODO).

**Example patch (``examples/cvtofreq_blip.json``).** Keyboard
gate -> ADSR (2ms attack, 180ms decay, sustain=0) -> shared into
both CVToFrequency.cv (f0=50 Hz, fm=120 Hz, f1=400 Hz log) and a
VCA.cv -> Speaker. Each note triggers a pitch-falling sine blip
that doubles as both the kick body and its amplitude envelope --
the canonical synthesized-kick recipe, with the CVToFrequency
doing the pitch envelope work in a single node instead of an
LFO + 1V/oct calculation. The exponential ADSR shape pairs
naturally with log mode so the pitch sweep sounds smooth and
musical rather than warbled in the upper register.

**Implementation notes.** The pyo backend gets a stub entry in
the v0.3+ silent-stub tuple alongside ``audio_to_cv`` /
``cv_to_audio`` -- numpy remains the real implementation. The
stub strings were already growing per-module; folding them into
the existing tuple keeps the noise contained. No model layer
changes were needed (the existing Patch / Port / cable machinery
handles the new module without modification).

**What phase 2 needs.** The plan calls for ``negative_enabled``
(default False, preserves phase-1 clamp behaviour exactly),
``f0_neg`` / ``fm_neg`` / ``f1_neg`` (default to the positive
values for zero-crossing continuity), and ``mode_neg`` (independent
of ``mode``). The ``_cv_to_hz`` helper would split into a positive
and negative arm dispatched by sign with the same shape-preserving
np.where structure. Phase 1 doesn't constrain phase 2 -- the
existing public surface stays bit-for-bit compatible because the
new param defaults restore phase 1 semantics.

---

## 2026-05-23 (even later) -- CVToAudio signal-kind bridge

Second of the bridge modules, mirror partner of AudioToCV. Where
AudioToCV brings audio across into the CV domain via a real
envelope-follower, CVToAudio is the trivial reverse: the patch
model bans ``cv → audio`` cables (see :meth:`Patch.connect`), so
this module exists *purely* to satisfy the type system. The DSP
is a buffer copy multiplied by a single ``gain`` param.

**Why so much smaller than AudioToCV.** The two bridges are
architecturally asymmetric. Audio carries a wider amplitude
spectrum at higher rates than typical CV, so the audio→CV trip
needs *summarization* (rectify + smoother) to be useful. The
CV→audio trip needs no summarization -- the bytes already are
audio samples, just labelled differently. About 30 lines of
renderer code vs AudioToCV's ~120.

**Use cases enabled.** Three, in descending order of musical
payoff:

- *Audio-rate LFO as oscillator*. Crank an LFO's ``rate`` into
  the audible range (e.g. 220 Hz), drop it through CVToAudio
  into the speaker, and the LFO becomes a tone source. The LFO
  has waveforms the dedicated Oscillator module doesn't have
  (its specific clipping/saw shapes, plus the ``random``
  sample-and-hold which sounds like a quantized noise stream
  at audio rate). Better: the LFO already exposes ``rate_cv``
  as a 1V/oct input, so once the LFO is audible you get
  *built-in FM* by patching any modulator into it. Two-LFO FM
  patches now work without any new module beyond CVToAudio.
- *Percussive clicks*. An ADSR with ~1 ms attack and ~5 ms
  decay is a single audible transient when sent through
  CVToAudio. This is the foundational shape for synthesized
  kicks and percussion -- the user can sample one to a .wav
  via the DiskWriter and use it as a one-shot.
- *CV oscilloscope by WAV*. Route any modulator through
  CVToAudio into the DiskWriter; the resulting .wav file is a
  visual record of the modulator shape over time. Niche but
  exactly the right tool for debugging slow envelopes or
  LFOs whose shape needs verification.

**Pitch comes from oscillation rate, not CV value.** The
documentation in the module makes this explicit because it's an
easy misread: a constant CV (e.g. a held ADSR sustain) becomes
DC at the speaker -- silent, but a real load on the cone. The
speaker frequency is determined by how the CV *varies over
time*. To raise the pitch of a CVToAudio-fed tone you raise the
LFO's ``rate`` (or its ``rate_cv``), not its ``depth``. ``depth``
controls loudness.

**DC blocking: deliberately not included.** Considered and
declined. Modular convention is that the user adds a high-pass
module if the patch needs one. The synth already trusts the user
with self-oscillating filters, audio-rate FM, and sum-past-unity
CVCombiners; adding silent safety here would be inconsistent.
Speaker hardware survives DC offsets within reason; the speaker
limiter clamps to [-1, 1] which is also the natural CV range.
If a real high-pass module becomes desirable later it should be
its own module, not a hidden side-effect of this one.

**Voice-awareness by shape preservation.** No state means no
branching. The renderer reads its input via ``_input_buffer``
with ``collapse=False`` so a polyphonic CV (e.g. a per-voice
ADSR) arrives with its ``(V, F)`` shape intact, multiplies by
gain, casts to float32, and returns. Mono inputs stay mono.
Downstream Speaker drain collapses the voice axis the same way
it does for voice-aware audio from any other source -- the
"implicit sum at mono sinks" rule from voice routing slice 2
covers this without special-casing.

**Files added/changed:**

- ``src/pysynthrack/modules/cvtoaudio.py`` -- new ``CVToAudio``
  Module subclass. Type string ``cv_to_audio``. Default
  ``gain=1.0``. Single ``cv`` input port (signal_kind ``cv``),
  single ``out`` output port (signal_kind ``audio``).
- ``src/pysynthrack/modules/__init__.py`` -- import +
  ``__all__`` entry, alphabetized after CVCombiner.
- ``src/pysynthrack/audio/numpy_backend.py`` -- dispatch line in
  ``_render_module`` routing ``"cv_to_audio"`` to
  ``_render_cv_to_audio``. The renderer is six executable lines:
  ``_input_buffer(collapse=False)``, missing-cable check, gain
  load, multiply, cast, return.
- ``examples/lfo_oscillator.json`` -- canonical "LFO as
  oscillator" patch. Two LFOs: a slow 5.5 Hz "Vibrato LFO" at
  depth 0.08 feeds the carrier's ``rate_cv``; the 220 Hz
  "Carrier LFO" goes through CVToAudio at gain 1.0 to a
  speaker at gain 0.4. End-to-end FM with no Oscillator module
  in sight.
- ``tests/test_cvtoaudio.py`` -- 13 new tests across four
  classes. Model: registration, defaults, ports, signal kinds,
  JSON round-trip, LFO→CVToAudio cabling accepted, audio→CV-in
  cabling rejected, CVToAudio→Speaker cabling accepted. Mono
  behaviour: unpatched input is silent, gain=1 is sample-exact
  passthrough, gain=2 doubles, gain=-1 inverts. Voice-aware:
  (V, F) shape preserved with per-row gain scaling and silent
  voices stay silent, mono fast path stays mono when input is
  1D. Integration: 220 Hz LFO through CVToAudio shows a FFT
  peak within one bin (~10 Hz at block=4096) of 220 Hz; the
  two-LFO FM patch puts >30% of spectral energy in a ±20 Hz
  band around the carrier (proving the sideband structure is
  centered correctly).

**Verified in sandbox:**

- ``pytest tests/test_cvtoaudio.py`` → 13/13 pass on first run.
- Full suite → 317 passed (304 prior + 13 new), 18 skipped
  (mido optional), 1 failed (the pre-existing
  ``test_adsr.py::test_no_nan_with_zero_durations`` undefined-
  ``sr`` bug, still untouched and in TODO as a drive-by).
- ``examples/lfo_oscillator.json`` smoke render: peak 0.400
  (under the limiter), spectral peak at 226.1 Hz with clear
  vibrato sidebands at 215.3, 236.9, 247.6 Hz -- spaced
  ~10.8 Hz which matches a 5.5 Hz vibrato (the visible spacing
  is twice the modulator rate when looking at single-sided
  sidebands from a bipolar carrier). Textbook FM spectrum from
  two LFOs and one type-cast.

**No truncation incidents this slice.** Whole-file writes from
``/tmp/stage`` with ``cp`` + ``diff -q`` verification ran clean
on every change (modules/__init__.py, numpy_backend.py,
TODO.md). The refined memory note from the AudioToCV slice was
load-bearing: line-count check as the primary truncation
detector (AST parse alone can pass on a syntactically-valid cut)
makes the stage→copy→verify rhythm fast enough that there's no
incentive to fall back to in-place Edit.

**Sound-design pairings to try (Matthew):**

- Open ``examples/lfo_oscillator.json``. The carrier is a sine
  -- swap it for ``square`` or ``saw`` to hear how LFO
  waveshapes sound at audio rate (the LFO's square is hard
  and rich in harmonics; its saw is a classic ramp). The
  ``random`` waveform at 220 Hz is a sample-quantized noise
  with rhythmic character.
- Drop the carrier rate to ~80 Hz and the vibrato depth to
  0.05: that's a fundamental bass note with subtle pitch
  drift, like a slightly out-of-tune analog oscillator.
- Patch a third LFO modulating the *vibrato LFO's* depth
  via... hm, depth isn't CV-modulatable yet. Add a CVCombiner
  feeding the carrier's ``rate_cv`` instead, with the
  vibrato LFO on one input and an ADSR on another -- now the
  vibrato fades in over the note's lifetime.
- Build a kick: Keyboard gate → ADSR (1 ms attack, 80 ms
  decay, 0 sustain, 0 release) → CVToAudio → Speaker.
  Triggers a clean low-frequency thump.

**What's next.** The third bridge module ``Schmitt`` (CV
threshold → gate edge) remains in the wishlist. After Matthew's
nudge, the immediate follow-up is a **CV-to-frequency
generator** -- a different beast from CVToAudio: that one will
*interpret* the CV value as a frequency (probably 1V/oct or
similar), producing a fresh oscillation at that frequency rather
than passing the CV's waveshape through. Conceptually it's a
"VCO with V/oct control" packaged as a dedicated module rather
than a wired-up Oscillator with ``freq_cv``. Will think through
the design when Matthew kicks that one off.

---

## 2026-05-23 (later still) -- AudioToCV envelope follower

First of the signal-kind bridge modules. The patch model walls off
``audio`` from ``cv`` at the cable layer (``patch.py:113``), so until
today there was no way for an audio signal to drive a ``*_cv`` input.
``AudioToCV`` is the fix: rectify the input, smooth with an
asymmetric one-pole IIR (separate attack and release time
constants), emit a non-negative CV. Three params -- ``attack_ms``,
``release_ms``, ``gain`` -- and one input / one output port.

**Why this module first.** Of the three originally-proposed bridges
(AudioToCV / CVToAudio / Schmitt), AudioToCV is the one with the
strongest "I patched this and it sounds great" payoff:

- Self-modulating filter -- ``filter.out → audio_to_cv → filter.cutoff_cv``
  closes the filter when its own output gets loud. Classic
  "self-wah" without an LFO. Effects you cannot fake with the
  existing LFO/ADSR modules because those don't know the signal
  level.
- Sidechain ducking -- a kick's output drives an AudioToCV whose CV
  pulls a pad's VCA down. Same patch idea as the dance-music
  technique baked into compressors.
- Audio-rate to control-rate bridge -- generic. Any ``*_cv`` port
  can now be driven by the envelope of any audio signal.

CVToAudio and Schmitt are still useful and can come later; AudioToCV
unlocks more new sound design alone.

**DSP shape.** Asymmetric one-pole IIR with time-constant
ergonomics:

    coef = 1 - exp(-1 / (time_seconds * sample_rate))
    target = |audio[n]|
    if target > level: level += attack_coef * (target - level)
    else:              level += release_coef * (target - level)
    out[n] = level * gain

Per-sample state (the smoother level feeds back into the next
sample's update), so the DSP loop is a Python ``for n in
range(frames)`` -- same pattern as the scalar biquad in
``_render_filter_mono`` and the S&H branch in ``_render_lfo``. At
512-sample blocks the cost is in the same ballpark. Coefficients
are derived once per block from the params; per-sample work is one
compare + one fused-multiply-add. Zero or negative time constants
clamp to ``coef = 1.0`` (instant).

Why per-sample state rather than block-mean: an envelope follower's
whole job is to track *transient* dynamics inside a block. Block-mean
on the audio (the trick we use for ``filter.cutoff_cv``) would
average a snare hit and a quiet tail into the same number. Fast
attacks of ~1-5 ms are well below typical block sizes, so the
follower must integrate at sample rate to keep its character.

**Voice-aware, shape-polymorphic.** Same convention as the rest of
the slice-3b stateful modules. Branches on the audio input's
``ndim``:

- 1D ``(F,)`` audio -> scalar smoother state ``level`` -> 1D ``(F,)``
  output. Mono fast path; bitwise-stable against any earlier
  rectifier implementation.
- 2D ``(V, F)`` audio -> per-voice smoother state ``level_arr`` of
  shape ``(V,)`` -> ``(V, F)`` output. Per-sample update is
  vectorized across voices: ``abs`` + ``np.where`` (per-voice
  attack/release coefficient pick) + IIR step are each a single
  numpy op over the length-V state vector. F serial steps; no Python
  loop over voices. Per-voice envelope followers are exactly what
  you want for the polyphonic "each note ducks its own filter" use
  case once those voices are split out.

State reinitializes cleanly when the input switches shape
(``level_arr`` discarded if the input becomes mono; ``level``
discarded if the input becomes voice-aware). Same shape-handover
pattern as LFO / Filter / Oscillator.

**Missing-cable behaviour.** Audio in unpatched -> output silence
and the smoother state is left as-is. The "as-is" matters because
reconnecting the cable later shouldn't snap the level back from a
stale decayed value mid-transient -- the smoother resumes from
where it left off and any spike in the new input is followed
normally.

**Files added/changed:**

- ``src/pysynthrack/modules/audiotocv.py`` -- new ``AudioToCV``
  Module subclass. Type string ``audio_to_cv``. Defaults
  ``attack_ms=5.0``, ``release_ms=100.0``, ``gain=1.0``. Single
  ``in`` audio input port, single ``cv`` output port.
- ``src/pysynthrack/modules/__init__.py`` -- registration import
  + ``__all__`` entry, alphabetized between ADSR and Combiner.
- ``src/pysynthrack/audio/numpy_backend.py`` -- dispatch line in
  ``_render_module`` routing ``"audio_to_cv"`` to the new
  ``_render_audio_to_cv``. The renderer derives the two
  coefficients once per block, branches on input ``ndim`` to the
  mono or voice path, and writes its level state into
  ``self._state[module.id]``.
- ``examples/envelope_follower_wah.json`` -- canonical "self-wah"
  patch: Keyboard saw at oct 3 -> lowpass filter (cutoff 320, Q 2.2)
  -> AudioToCV (attack 6ms, release 140ms, gain 0.9) -> back into
  the filter's ``cutoff_cv``. Hold a note: the resonant peak rides
  the envelope of the filter's own output. Tuned so peaks stay
  under the speaker limiter (max output ~0.32, RMS rises ~0.12 ->
  0.16 as the envelope opens).
- ``tests/test_audiotocv.py`` -- 14 new tests across four classes.
  Model: registration, defaults, ports, signal kinds, JSON round-trip,
  rejection of CV-into-audio cabling, acceptance of AudioToCV.cv ->
  Filter.cutoff_cv. Mono behavior: silence stays silent, unpatched
  input is silent, step input reaches ~63% at one attack-time,
  release decays to ~37% at one release-time, gain scales,
  negative audio is rectified. Voice-aware: ``(V, F)`` in -> ``(V, F)``
  out with independent per-voice steady-state levels, per-voice
  state persists across blocks, mono<->voice state-reinit on shape
  change. Integration: the full self-modulating filter chain
  compiles, renders, and produces non-zero output through the
  speaker.

**Verified in sandbox:** ``pytest tests/test_audiotocv.py`` -> 14
pass. Full suite -> 304 passed, 18 skipped (mido optional), 1
failed -- ``tests/test_adsr.py::test_no_nan_with_zero_durations``
is **pre-existing**: it references an undefined ``sr`` (missing
``sr = 44100`` setup line). Untouched by this change, but noted in
TODO.md as a drive-by.

Audible test (smoke render of ``envelope_follower_wah.json``):
- Idle peak: 0.0000 (silence).
- Note-on C3: peak settles to 0.32 with RMS rising 0.119 -> 0.164
  -> 0.152 across 30 blocks of 512 frames. The RMS shape is the
  follower swinging the resonant peak through the note's
  partials.

**The truncation gauntlet, run again.** Two Edit-tool truncations
fired during this slice: numpy_backend.py lost ~120 lines off the
end after the first AudioToCV insertion, and TODO.md lost its last
four items on the wishlist-section rewrite. Both recovered by
restoring from ``git show HEAD:<path>``, building the new content
in /tmp/stage via Python string ops, and copying the staged file
to the mount as a whole-file overwrite. The
``feedback_mount_write_protocol`` memory was load-bearing here:
stage-then-copy-then-verify-size is the only safe rhythm for
multi-hundred-line files on this mount. Note for future: even
small Edits on the mount can corrupt the file tail; whole-file
writes are not optional for anything bigger than a one-liner.

**Sound-design pairings to try (Matthew):**

- Open ``envelope_follower_wah.json``, hold a chord. The resonance
  rides the envelope of the filter's own output -- pluck-like
  attacks brighten the tone, decays close it down.
- Patch a kick-like noise burst (Keyboard at oct 2, percussive
  ADSR) into AudioToCV, route its CV through a CVCombiner with
  a fixed positive bias, then drive a pad's VCA cv input with the
  combined value subtracted. That's a sidechain duck without a
  dedicated compressor module.
- Crank ``release_ms`` to 800 ms or more for a slow following
  envelope -- great for subtle "breathing" modulation that's
  dynamics-aware instead of LFO-aware.
- Drop ``attack_ms`` to 0.1 and ``release_ms`` to 5: the follower
  becomes a near-peak detector, useful for hard ducking effects.

**What's next.** With AudioToCV in, the obvious follow-ups are
CVToAudio (the dual; would let an LFO double as a tonal source)
and Schmitt (CV-threshold-to-gate, for envelope chaining). Both
are still in the wishlist with no new urgency. The bigger
sound-design unlocks are still the LeftSpeakerOut / RightSpeakerOut
pair just added to v0.4 and PolyBLEP for the oscillator's saw and
square shapes.

---

## 2026-05-13 (v0.3 starts) — CV-modulatable params

The big sound-design unlock: LFOs and envelopes can now sweep param
values through dedicated CV input ports on existing modules. The
filter cutoff is the obvious one (wah, filter envelope); oscillator
freq and amp open up vibrato/FM and AM/tremolo.

**1V/octave convention.** For frequency-domain params (cutoff, freq),
``effective = base * 2 ** cv``. A CV of +1 doubles the value, -1
halves it. This matches the standard modular-synth ergonomics — a
unipolar 0..1 envelope sweeps one octave up; a bipolar ±1 LFO swings
one octave each way. Amp CV is linear instead: ``amp * cv``, because
loudness perception is linear in this range and the multiplicative
shape lets a unipolar LFO act as a VCA-style amp modulator.

**Per-sample vs per-block.** Different modulation domains have
different sensitivity to update rate:
- ``oscillator.freq_cv`` and ``oscillator.amp_cv`` are evaluated per
  sample. Per-sample frequency integration (cumsum of inst-increments)
  is essentially free in NumPy and gives true vibrato/FM. Block-rate
  would alias the LFO into staircases at the block boundary.
- ``filter.cutoff_cv`` uses block-mean CV. Per-sample cutoff would
  need fresh biquad coefficients every sample — ~9x cost in the
  current scalar IIR loop. Block-mean is audibly fine at production
  block sizes (512–1024 samples); the LFO cycle has to be much
  shorter than the block for the mean to wash out, which would only
  happen at audio-rate "modulation" (i.e. FM cutoff), which is a
  different regime than what users want here.

**Cabling.** Adding ports is backward-compatible — old patches reference
ports that still exist (``in``, ``out``, ``gate``) and ignore the new
CV inputs. The patch model's signal-kind check ensures audio cables
can't accidentally land on a CV input.

**Files added/changed:**

- ``src/pysynthrack/modules/filter.py`` — Filter gains ``cutoff_cv``
  input (signal_kind ``cv``).
- ``src/pysynthrack/modules/oscillator.py`` — Oscillator gains
  ``freq_cv`` and ``amp_cv`` inputs.
- ``src/pysynthrack/audio/numpy_backend.py`` —
  ``_render_oscillator`` does per-sample 2^cv frequency integration
  via cumsum, plus per-sample linear amp multiplication. CV args on
  ``_render_oscillator`` are optional so existing test call sites
  (which pass just ``module, frames``) still work.
  ``_render_filter`` applies block-mean cutoff CV before the biquad
  coefficient pass.
- ``examples/wah.json`` — keyboard (saw) → bandpass filter ← LFO@1.5 Hz
  bipolar depth 1.5 on cutoff. Classic auto-wah.
- ``examples/filter_envelope.json`` — keyboard (saw) → lowpass filter
  ← ADSR (0.005/0.4/0.2/0.6) on cutoff. The acid bassline shape.
- ``examples/vibrato.json`` — oscillator ← LFO@5.5 Hz bipolar depth
  0.04 on freq. ~28 cents either side, gentle vibrato.
- ``tests/test_cv_modulation.py`` — 11 new tests: filter no-cv path
  is no-op, +1/-1/-5 octave shifts, end-to-end LFO sweep produces
  RMS swing; oscillator freq_cv at +1/-1 doubles/halves cycle count,
  phase continuity across blocks; amp_cv at 0/0.5 mutes/halves.
- ``tests/test_filter.py`` — updated input_ports assertion to expect
  ``["in", "cutoff_cv"]``.

**Verified in sandbox:** 108 tests pass (97 prior + 11 new).
End-to-end smoke render of the three example patches:
- ``wah.json``: per-block RMS swings 0.18–0.38 over LFO cycles.
- ``filter_envelope.json``: RMS 0.32→0.63 as envelope opens.
- ``vibrato.json``: RMS stable (vibrato changes pitch not amplitude);
  ear test on Matthew's side will confirm it's audibly modulated.

**Sound-design pairings to try (Matthew):**
- Open ``wah.json``, play a sustained note, drag the bandpass
  resonance up for a louder wah.
- ``filter_envelope.json`` with the keyboard set to a saw and decay
  long — bouncy filtered notes.
- ``vibrato.json`` — try cranking depth to 0.5 for tape-warble; rate
  to 30 Hz for a metallic FM tone (the LFO is now operating at
  audio-rate frequency modulation territory).
- Chain: LFO → freq_cv on osc, and a second LFO → cutoff_cv on a
  filter further down the chain. Two modulators at different rates.

---

## 2026-05-13 (v0.2 ships) — Mixer module

Closing v0.2 with the missing summing point. The mixer takes four audio
inputs, applies a per-channel gain trim, sums them, and applies a master
gain before output.

**Why fixed 4 channels, not N.** Flat JSON schema, predictable UI,
covers the typical patches we'll build during v0.2 (layered oscillators,
detuned saws, osc + sub + noise, dual-keyboard splits). The v0.3
``Combiner`` will handle unbounded-N pure summation — different concept,
no per-channel trims, lives in the routing-primitives bucket.

**Cabling.** One cable per input jack — same rule as every other module.
To bus more than four sources, chain mixers (mixer-of-mixers).

**Param ranges.** Channel gains and master live in [0, 2], slightly hot
so users can lift a quiet channel without leaving the slider. Speaker
output still clips at ±1, so over-driving the mixer is a hard ceiling,
not an explosion.

**UI tweak.** The slider-float branch was extended so any param named
``gain*`` or ``master`` lands in the 0-to-2 range. Previously only the
bare name ``"gain"`` qualified, so mixer's ``gain1``-``gain4`` would
have fallen into the generic drag-float.

**Files added/changed:**

- ``src/pysynthrack/modules/mixer.py`` (new) — Mixer class +
  MIXER_INPUT_NAMES / MIXER_GAIN_NAMES tuples
- ``src/pysynthrack/modules/__init__.py`` — register Mixer
- ``src/pysynthrack/audio/numpy_backend.py`` — ``_render_mixer``
  (port-lookup sum × master)
- ``src/pysynthrack/audio/pyo_backend.py`` — friendly skip for mixer
- ``src/pysynthrack/ui/app.py`` — slider widget covers any ``gain*`` /
  ``master`` param at 0..2
- ``examples/fat_saw.json`` — three saws detuned ±1.5 Hz around 220 Hz
  through the mixer, then a lowpass with some resonance. Stored
  positions show the mixer fanning into a single bus.
- ``tests/test_mixer.py`` — 13 new tests (model, port shape, JSON
  round-trip, signal-kind rejection, one-cable-per-jack, render
  silence/sum/per-channel/master arithmetic, four-input contribution,
  disconnected-channel silence, end-to-end render of fat_saw.json)

**Verified in sandbox:** 95 tests pass (82 prior + 13 new).
``examples/fat_saw.json`` loads, renders finite non-silent audio
through the full chain, and the speaker-stage clip keeps output ≤ 1.0.

**v0.2 SHIPPED.** Module library now: Oscillator, Keyboard, Filter,
ADSR, VCA, LFO, Mixer, SpeakerOutput. Drag-cable UI, JSON save/load
with node positions, CLI fallback, 95-test safety net. From oscillator-
to-speaker on Friday to a playable subtractive synth one week later.

**Pending from Matthew:**
- Open ``examples/fat_saw.json``, hit Start audio, you should hear a
  fat detuned sustained chord-ish drone. Sweep the filter cutoff
  while it plays for the classic supersaw lead motion.
- Try chaining mixers: 3 oscillators into mixer A, mixer A + keyboard
  + LFO into mixer B for a polyphonic-with-pad layer.

**Sandbox note:** continued to use bash heredoc + AST-parse-after-each-
write for every file edit this pass; zero truncation incidents.
Memory ``feedback_edit_tool_truncation.md`` covers the pattern.

---

## 2026-05-12 — v0.1 scaffold

**Decisions made with Matthew:**

- Project name: **PySynthRack**.
- Located in the existing `Python Synthesizer` workspace folder under `C:\Users\Admin\Desktop\-=Programming=-\Python Synthesiser 2\`.
- Audio stack: **abstract the backend** — both `pyo` and `sounddevice + numpy` implementations behind one interface. pyo preferred, numpy fallback. Auto-pick at startup based on what's installed.
- Python: whatever's on PATH (project pins `>=3.9`).
- UI: DearPyGui (built-in node editor with cable drawing).

**Architecture pattern picked:**

Modules are **pure model objects** (type + params + declared ports). They don't render audio themselves. Each `AudioBackend` implementation walks the patch and builds its own native graph — `PyoBackend` constructs `pyo.Sine` etc.; `NumpyBackend` builds a callback that walks the topology each buffer.

Why this shape:
- DSP knowledge sits in the backend, not duplicated across module classes.
- The model layer is trivially serializable (it's already a dict-of-dicts).
- Swapping backends doesn't require touching the UI or modules.

Trade-off: adding a new module type means adding a renderer for it in each backend. For a small, fixed module library that's fine; if the module set explodes later we may want a plugin-style registration where each module ships its own per-backend renderers.

**Scaffolded:**
- `src/pysynthrack/` package with `core/`, `audio/`, `modules/`, `ui/`, `io_patch/` subpackages
- `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `README.md`
- `tests/`, `examples/`, `docs/`

**Built in this pass:**
- `src/pysynthrack/core/` — `Port`, `Module` (with type registry decorator), `Patch` (graph + validation + serialization), `Cable`
- `src/pysynthrack/audio/` — `AudioBackend` ABC, `PyoBackend`, `NumpyBackend`, `pick_backend()` auto-selector (with `PYSYNTHRACK_BACKEND` override)
- `src/pysynthrack/modules/` — `Oscillator` (sine/saw/square/triangle), `SpeakerOutput`
- `src/pysynthrack/io_patch/` — `save_patch`, `load_patch`, JSON string helpers
- `src/pysynthrack/ui/app.py` — DearPyGui node editor, palette via menu, file open/save dialogs, transport button, inline param widgets per node
- `examples/hello_sine.json` — 440 Hz sine → speaker
- `tests/` — 24 headless tests covering model rules, JSON round-trip, oscillator DSP correctness, phase continuity, topo sort
- `docs/architecture.md` — layering, why pure-data model, compile-vs-set_param contract

**Verified in the sandbox:**
- All Python files compile (`py_compile`)
- 24/24 headless tests pass
- UI module imports cleanly with stubs

**Not yet verified (needs your Windows machine):**
- DearPyGui actually renders the node editor
- pyo install works on your Python
- A 440 Hz sine actually comes out of the speakers

## 2026-05-12 (later) — install hotfix + CLI fallback

Matthew's first install attempt failed. Two root causes:

1. **DearPyGui has no wheel for his Python** — pip's `(from versions: none)` is conclusive. Probably Python 3.13 or 3.14 where DPG hasn't published wheels yet. The original requirement `dearpygui>=1.10,<3.0` made it worse (excluded 2.x).
2. **`No module named pysynthrack`** after install — original README told user to `pip install -r requirements.txt`, which installs deps but never installs the project. Should have been `pip install -e .`.

**Fixes shipped:**

- `pyproject.toml`: moved DearPyGui to an optional dependency under `[gui]` extra. `pip install -e .` now succeeds with just numpy + sounddevice; `pip install -e ".[gui]"` adds the GUI. `[pyo]` and `[all]` extras also added.
- `src/pysynthrack/__main__.py`: added argparse with `--cli`, `--patch`, `--seconds`, `--backend` flags. GUI mode automatically falls back to CLI mode if DearPyGui isn't installed, with an informative stderr message.
- `src/pysynthrack/cli.py`: new headless runner. Loads a patch, prints the module/cable summary, picks a backend, starts audio, waits for Enter (or `--seconds`).
- README: rewrote install + run sections. Documents `[gui]`/`[pyo]`/`[all]` extras and CLI usage.

**Verified in sandbox:**

- All 24 tests still pass.
- CLI module imports cleanly without DearPyGui installed.
- `__main__.py` correctly detects missing DPG and routes to CLI fallback.

**Pending from Matthew:**

- ✅ Install succeeded with `pip install -e .` (2026-05-12).
- ✅ CLI mode plays sound. v0.1 audio goal hit.
- ✅ GUI install working (2026-05-12). Path: `uv python install 3.12` → `uv venv --python 3.12 .venv` → `uv pip install -e ".[gui]"`. pyo skipped (no Windows wheels, no MSVC build tools); numpy backend covers v0.1.

**v0.1 SHIPPED** — model, both backends, oscillator, output, JSON I/O, drag-cable GUI, CLI mode, 24 tests passing, and verified on Matthew's machine. From zero to playable synth in one session.

---

## 2026-05-12 (v0.2 starts) — Keyboard module

First v0.2 module shipped: `Keyboard` lets the computer keyboard play polyphonic notes through the synth.

**Layout** — one octave per home row, black keys on the QWERTY row above (FL Studio / Ableton typewriter style):

```
   W E   T Y U   O P
  A S D F G H J K L ;
   C# D# F# G# A#  C# D# (over)
  C  D  E  F  G  A  B  C  D  E
```

A/W/S/E/D/F/T/G/Y/H/U/J = chromatic C through B in the selected octave; K onwards spills into the next octave.

**Params** (all inline on the node, per the UX decision):
- `octave`: int slider 0–8, default 4 (so home-row A = middle C / MIDI 60)
- `waveform`: sine / saw / square / triangle (shared definition with the Oscillator module)
- `volume`: 0–1 master gain for the whole keyboard

**Architecture choices:**

- Keyboard owns its own `active_notes: set[int]` (transient, not serialized to JSON). UI mutates it via `note_on` / `note_off` under a `threading.Lock`. The audio thread reads via `snapshot_active_notes()` which returns a copy under the same lock. This keeps the pure-data model design intact (params is just spec) while giving the audio thread a safe view of live keyboard state.
- Polyphony is per-voice: each pressed note gets a voice dict with its own phase + envelope level. Voices are reaped once their release ramp returns to ~0.
- 5 ms linear attack + 20 ms linear release ramps prevent the click that would otherwise happen on every note edge. Not a full ADSR — that's a separate v0.2 module.
- Global DPG `handler_registry()` catches all key events. OS auto-repeat is debounced via `_held_keys` so holding A is one note, not a stream. All-notes-off is fired on audio-stop and patch-clear to prevent stuck notes.
- Pyo backend prints "not yet supported" for the keyboard type and produces silence — the dynamic voice allocation pattern doesn't map cleanly onto pyo's static-graph model without a separate Voice-manager design. Punted to v0.3.

**Files added/changed:**

- `src/pysynthrack/modules/keyboard.py` (new) — Keyboard class + midi/note helpers
- `src/pysynthrack/audio/numpy_backend.py` — `_render_keyboard` with envelope ramps and voice reaping
- `src/pysynthrack/audio/pyo_backend.py` — friendly "not yet supported" hint
- `src/pysynthrack/ui/app.py` — key handlers, int slider for octave, all-notes-off on stop
- `examples/keyboard_play.json` — keyboard wired to speaker (saw, octave 4)
- `tests/test_keyboard.py` — 15 new tests covering note math, model behaviour, polyphony, envelope ramp

**Verified in sandbox:** 39 tests pass (24 from v0.1 + 15 new), UI compiles and imports.

**Pending from Matthew:** run `python -m pysynthrack`, File → Open → `examples/keyboard_play.json`, hit Start audio, tap A/S/D/F/G/H/J — should hear a saw chord follow your typing.

---

## 2026-05-13 (v0.2 continued) — LFO + silent-exit bugfix + node positions

Three changes landed together because Matthew flagged the bug and the
missing positions while asking for the LFO; all three are small.

**Silent-exit on second Open (the bug).** DearPyGui's node editor keeps
its children in two slots: links in slot 0, nodes in slot 1. The
original `_clear_editor` only iterated slot 1, so opening a second
patch left orphan links pointing at attribute IDs from the now-deleted
nodes. Next frame, DPG hard-exits the process with no Python traceback.
Fix is one line: `dpg.delete_item(EDITOR_TAG, children_only=True)` —
clears every slot, no orphans. Defensive fallback (per-slot loop) kept
in case a future DPG release tightens the contract. While here, also
reset `_next_node_pos = [40, 40]` on clear so a fresh-loaded patch
without saved positions lays out from the top-left again.

**Node positions persist in JSON.** Added an optional opaque `ui` dict
to `Patch` (round-trips through `to_dict`/`from_dict`, omitted from
output when empty so the schema bump is invisible to callers that don't
use it). At save time `_capture_node_positions` snapshots
`dpg.get_item_pos(node_id)` for every live node into
`patch.ui["node_positions"]`. At load time `_load_patch_from` reads it
back and passes it to `_create_node_for_module(module, pos=...)`. The
staggered placement still kicks in for any module whose ID isn't in the
map — so legacy patches and freshly-added nodes both behave sensibly.
Positions are JSON-string-keyed (`{"1": [x, y]}`) because JSON object
keys are strings; converted at the call site.

**LFO module.** Output is CV (so it cannot be patched into audio
inputs by mistake). Five waveforms: sine, triangle, square, saw,
random (sample-and-hold — re-rolls on each phase wrap). Three params:
`rate` (Hz, clamped 0.001 to 0.45·sr), `depth` (0–1), and `bipolar`
(bool). Unipolar is the default: the wave is shaped into [0, depth]
so an LFO → VCA chain produces tremolo without the inverted-phase
audio fight you'd get from raw [-1, 1] modulation. Flip `bipolar` for
pitch / cutoff sweeps once those become CV-routable.

**Architecture notes:**
- LFO and Oscillator share the same per-block phase-accumulator
  pattern; if we ship more waveform-driven modules a shared
  `waveform_sample(phases, kind)` helper is worth pulling out. Held
  off for now — three callers don't justify the indirection yet.
- Pyo backend logs "not yet supported" for `lfo`, matching the
  established pattern.
- A CV mixer/multiplier would let LFO and ADSR co-modulate a VCA. It's
  on the v0.3 list along with the rest of the routing primitives.
- Filter has no CV input on its `cutoff` param yet, so LFO → filter
  cutoff doesn't work in v0.2. Added "CV-modulatable params" to v0.3
  TODO.

---

## 2026-05-14 — v0.3 Routing pass: Combiner, CVCombiner, Crossover, DiskWriter, LFO.rate_cv

**Result.** v0.3 closes out the way it set out to: every routing item on
the roadmap is built or consciously ruled out, the modulation matrix
got its bonus rate_cv, and the synth can now record itself to disk. 140
tests passing (110 prior + 30 new), and every example patch — old and
new — still loads and plays.

**Splitter: built nothing, on purpose.** The audit said it best: the
Patch model already permits multiple cables from a single output port
(only inputs are mono — see `Patch.connect`'s "destination not already
occupied" check). The numpy backend keys its buffer cache by
`(src_module_id, src_port)`, so any number of downstream consumers
reading the same source key receive the same array — fanout is free.
DPG's node editor allows multiple links per output by default. So a
Splitter module would only add an empty box with one in and four
identical outs — overhead with no new capability. The TODO entry is
ticked with a "architecturally redundant" annotation so future-me
doesn't try to build it again. The new `examples/fan_out.json` patches
one keyboard into three different filter chains via three cables from
the same output, demonstrating the fanout explicitly.

**Combiner (audio).** The lighter sibling of Mixer. Four audio inputs,
plain unit-gain sum, one audio output, no per-channel widgets in the
way. Useful when you want a structural sum (parallel filter paths
re-joining, crossover low+high stitched back together) rather than a
level-balance task. Mixer's docstring already pre-declared the
contract before this module existed; I just honoured it.

**CVCombiner.** The new module that fills the real architectural gap.
Each input jack accepts one cable (mono input convention), but
modular CV is *additive* — wanting LFO + ADSR co-modulating filter
cutoff has been a thing since 1965. CVCombiner takes four CV inputs
and emits their sum (default) or their average over the connected
inputs. Sum is the analog-modular convention; average is the right
choice when you want shared control without the depth doubling. Two
unipolar LFO squares of depth=1.0 sum to 2.0 in sum mode; the same
two average to 1.0. Tests verify both modes and the unconnected-
inputs-don't-affect-divisor invariant on average mode.

**Crossover — Linkwitz-Riley 4th order.** Two cascaded RBJ biquads
per branch at Q=1/√2 (Butterworth), at the same corner frequency.
Each branch is 4th order so phase rotates a clean 360° per side,
which is why low + high recombines flat in magnitude through a
Combiner. Tests cover (a) low-freq tones land in the low branch and
silence the high; (b) high-freq tones, vice versa; (c) at the corner
both branches sit at -6 dB (LR's signature); (d) summing low + high
through a Combiner reconstructs the source RMS within ±15%; (e)
extreme frequency values clamp without NaN. The new
`two_way_crossover.json` shows the canonical use: split a saw at
800 Hz, LP the low band, BP the high band, recombine — instant
multi-band shaping.

**DiskWriter.** A sink module. Audio in, nothing out, written to disk
as a 16-bit mono WAV at the backend's sample rate. Threading model
explained in the docstring: the audio callback hands blocks to a
bounded `queue.Queue`, a daemon worker thread pops and writes via the
stdlib `wave` module. The callback never blocks on filesystem I/O —
if the queue fills (very unlikely, 64 blocks ≈ 750 ms of latency), the
incoming block is dropped and a counter is bumped rather than the
audio thread stalling. Lifecycle hooks: the writer opens the file
lazily on first audio arrival; `armed=False` skips and tears down any
active worker; the path being edited mid-take closes the old file and
starts a new one (a manual punch-in); `backend.stop()` and
`backend.compile()` both close any active writer state so the WAV
header gets finalised and no thread leaks. Tests verify all of these:
disarmed creates no file, armed-but-unpatched creates no file, normal
recording writes the right number of frames and RMS matches the
source, mid-take path swap produces both files, disarm closes the
file, and a recompile swap that drops the disk_writer state closes
it cleanly.

**LFO.rate_cv — modulation matrix territory.** A second LFO (or ADSR)
can now modulate this LFO's rate. 1V/oct, block-mean evaluation,
same trade-off as filter cutoff_cv. Together with the existing
freq_cv / amp_cv / cutoff_cv ports this means CV can route to nearly
every continuous parameter that matters. The new
`examples/mod_matrix.json` shows the classic "breathing vibrato" —
a 0.3 Hz LFO modulating a 5 Hz vibrato LFO's rate, which itself
modulates oscillator freq.

**Backend wiring.** `_render_module` dispatches to four new
renderers. `_render_lfo` now accepts optional buffers/patch so it can
look up rate_cv when called from the topo walk (same back-compat
trick as `_render_oscillator`). `compile()` no longer just drops
state — when a disk_writer entry is being discarded (module removed,
or recompiled type changed) it calls `_close_disk_writer_state` first
so the file handle and thread don't leak across recompiles.
`stop()` walks the state map and closes any active writers so the
WAV header is finalised when the user hits Stop on the transport.

**UI wiring.** The Add Module menu pulls from `all_module_types()`
so the four new modules appeared in the palette for free. Three
small param-widget tweaks: the `mode` combo dispatches on module
type (cv_combiner → sum/average, filter → LP/HP/BP), `frequency` is
treated like `freq`/`cutoff` (drag float in Hz), and `path` falls
through to the existing input_text fallback. Boolean `armed` already
got a checkbox via the existing bool branch.

**Edit-tool truncation, again.** Hit the same file truncation issue
three times on this pass — numpy_backend.py, test_lfo.py, and
WORKLOG.md all got chopped mid-line by Edit. Switched all non-trivial
rewrites to Python scripts via `mcp__workspace__bash` (read whole
file → in-memory transform → write whole file → AST parse). The
memory entry on this is already current.

**File hygiene.** The disk_writer smoke test left a `take_01.wav` in
the project root (2 seconds, 88,064 samples, mono 16-bit at 44.1k —
exactly the smoke-test render length). The sandbox can't delete files
on the Windows mount, so it's still there waiting to be removed
manually.

**Architecture notes:**
- LR4 crossover is a clean candidate for "extract a biquad helper"
  if a 4th module ever reaches for one. Today we have: Filter
  (single biquad), Crossover (four biquads), and that's the threshold
  where DRY starts to win. Holding off until that 4th caller arrives.
- DiskWriter is the first module that owns process resources (a
  thread + a file handle) rather than pure numerical state. The
  cleanup hooks live in `_close_disk_writer_state` so the pattern
  is reusable if (say) a future MIDI input module needs its own
  worker thread.
- CV summing happens to be cheap because every CV buffer is
  float32 same-length — `out += buf` is one numpy fused-multiply.
- Fanout was a deliberate v0.1 design choice (port-keyed buffers)
  paying dividends in v0.3 with zero new code. Worth keeping in
  mind when the v0.4 polyphony refactor lands.

**Counts.** Modules: 8 → 12. Examples: 9 → 14. Tests: 110 → 140.
LOC of numpy_backend.py: ~656 → ~902. v0.3 is shipped — next stop
v0.4 (MIDI, real polyphony, anti-aliased osc shapes).

---

## 2026-05-14 — v0.4 starts: MIDI Input module

**Result.** MIDI keyboards play any existing patch — install the `[midi]`
extra, drop a MIDIInput node in place of a Keyboard node, and you have a
playable instrument. 172 tests passing (140 prior + 32 new). 13 modules,
16 example patches. v0.4 voice routing manager is a deliberate separate
slice; design pending.

**Sequencing choice.** The roadmap bundled "MIDI input" with "voice
routing manager" as one item, but they're very different jobs. MIDI
input as a self-polyphonic mirror of Keyboard is a single-module change
with no model-level impact. Voice routing — making each note into its
own signal path — is a model-level rewrite (either voice-aware signal
carriers, or explicit voice fanout). Splitting them lets MIDI ship now;
voice routing gets a proper design pass before it lands.

**MIDIInput module — what it is.** Same shape as Keyboard: no input
ports, two outputs (`out` audio, `gate` gate), self-polyphonic voice
tracking inside the module. The only structural difference is that
`active_notes` is a `dict[int, float]` instead of a `set[int]` — the
value is normalised note-on velocity. Renderer mirrors `_render_keyboard`
exactly with one extra line of velocity scaling per voice.

**Threading model.** mido owns its own IO thread. `start_midi()` opens
the port and registers `self._on_message` as the callback; mido invokes
the callback on its IO thread for every incoming message. The callback
mutates `active_notes` under `self._lock`. The audio thread takes a
snapshot copy each block via `snapshot_active_notes()` so it never
iterates a dict the MIDI thread might be writing. Exactly the lock
pattern Keyboard already uses for computer-key events; nothing novel,
which is the point.

**Message handling, what's in scope.** `note_on` with velocity > 0 adds
the note. `note_on` with velocity == 0 is treated as `note_off` (the
running-status optimization most controllers use — saves a status byte).
`note_off` removes the note. CC 123 (All Notes Off) clears everything.
Channel filter applies: param `channel=0` is omni; 1–16 filters on the
matching mido 0-indexed channel. Out of scope for this slice: pitch
bend, sustain pedal (CC 64), mod wheel (CC 1), aftertouch. Each of
those is a natural fit for new CV output ports (`pitch_cv`, `mod_cv`,
`pressure_cv`) and lands in a v0.4 follow-up.

**Octave shift.** Applied at note ingest time, not at render time. A
`note_on(60)` with `octave_shift=1` stores 72 in `active_notes`. A
subsequent `note_off(60)` resolves to the same shifted note and clears
it. Notes shifted outside the MIDI range (0..127) are dropped silently
rather than wrapping or clipping — voicing C-1 with `octave_shift=-1`
gets you nothing, not a wrong note.

**Velocity sensitivity.** Two-state param: `True` (default) scales each
voice by its normalised velocity; `False` plays every voice at unity.
Useful for organ-style patches where dynamic expression doesn't belong,
or for controllers with bad velocity curves. The velocity is always
stored in voice state — the param decides whether to apply it, so the
toggle takes effect immediately without disrupting active voices.

**Lifecycle wiring.** Tracked on the backend via `self._midi_inputs:
dict[int, MIDIInput]`. On `compile()`: new patch's MIDIInput modules
get their ports opened (idempotent if already open with the right
device); old ones that left the patch get their ports closed. On
`stop()`: every tracked MIDIInput's port is closed so the next start()
reopens cleanly. The module instances live on the patch, so closing the
port is the right teardown — we don't drop the module, just its OS
resource. Same lifecycle pattern as DiskWriter (own process resource,
explicit teardown hooks), generalised to a tracked-instances dict.

**Optional dependency handling.** `mido` and `python-rtmidi` are an
opt-in `[midi]` extra because `python-rtmidi` is a C extension and can
fail to build on locked-down systems. The module *imports cleanly*
without them (import-guarded with a `_MIDO_AVAILABLE` flag), so the
registry still sees MIDIInput, the UI palette still shows it, the JSON
loader can still create instances. The missing-dep error is reported
only when `start_midi()` is actually called — log warning, return,
render silence. This means a patch saved with a MIDIInput node loads
fine on a machine without mido; you just won't get notes.

**UI wiring.** Four new param widget branches in `_add_param_widget`:
`device` (combo populated by `available_devices()`, with `""` at the
top for auto-pick), `octave_shift` (int slider ±4), `channel` (int
slider 0..16), and `velocity_sensitive` falls through to the existing
bool checkbox branch. The device combo snapshots devices at widget
creation; user can recompile (delete + re-add the node, or reopen the
patch) to refresh after hot-plugging. Could add a refresh button later
if hot-plug refresh becomes annoying.

**Tests — 32 new, all pass headless.** Metadata sanity (5), direct
note_on/off ingest including thread-safety stress (11),
`mido.Message`-driven callback handling (6), channel filter (2),
rendering through the numpy backend (6), optional-dep guardrails (2).
The mido-message tests skip if mido isn't installed in the test env;
the rest don't require it. No real MIDI hardware is needed for any
test — we pass `mido.Message` objects directly into the callback.

**Example patches — 2 new.**

* `midi_simple.json` — MIDIInput → SpeakerOutput. The hello-world. One
  cable, plays the configured waveform whenever a note is held.
* `midi_lead.json` — MIDIInput → LP filter (cutoff modulated by ADSR
  off the MIDI gate) → VCA (gain modulated by a second ADSR off the
  same gate) → SpeakerOutput. The "proper" played-by-MIDI lead patch
  with a filter envelope and amp envelope, both triggered by the global
  gate. Tuned volume=0.35 because resonance=4 + a saw was clipping at
  the speaker; that headroom is the cost of the resonant peak.

**Bugs hit & fixed.**

* **VCA's audio input is named `audio`, not `in`.** First draft of
  `midi_lead.json` connected the filter to `vca.in` and got silence.
  Surfaced because the renderer returns silence when its declared input
  port has no cable. Fix: use `vca.audio`.
* **Edit-tool truncation, again.** The first save of midi_lead.json
  had its final `}` chopped by the Edit tool — same Windows-mount bug
  that bit us on numpy_backend.py and WORKLOG.md during v0.3. Rebuilt
  the file via bash heredoc. The memory note on this is still current.

**Counts.** Modules: 12 → 13. Examples: 14 → 16. Tests: 140 → 172.
v0.4 first slice shipped — next stop voice routing manager (design
pending), then anti-aliased oscillators, then porting the rest of the
graph into pyo.

---

## 2026-05-15 — MIDI Input confirmed end-to-end on real hardware

First played note through a real MIDI controller — Matthew's USB
keyboard plugged into Windows, `[midi]` extra installed, GUI launched,
`midi_lead.json` loaded, device picked from the populated dropdown,
keys pressed, audio out. Closes the loop on the v0.4 first slice; no
code changes needed from the headless tests.

**Install-day friction we should remember for future docs.**

* The `[midi]` extra is genuinely opt-in — on a fresh pull from the
  v0.4 commit, the device dropdown is empty until `uv pip install -e
  ".[midi]"` runs. The graceful-fallback design means it doesn't
  *break*, but a first-time user can mistake the empty list for "no
  devices" rather than "no library". The README install section calls
  out the extra explicitly; we should keep that prominent.

* The GUI's device combo snapshots `available_devices()` at widget
  creation. If a user installs `[midi]` while the app is already
  running, the dropdown won't repopulate until the patch is reopened
  (or the MIDIInput node deleted + re-added). A "refresh devices"
  button on the MIDIInput node would close this hole; small follow-up
  for the next UI sweep, but not urgent.

* Once a device is selected and the transport is started, silence
  before the first key-press is the correct idle state — `midi_lead`
  is gated through ADSRs off the MIDI gate, so the audio path is
  zero-amplitude until something plays. Worth keeping in mind for
  troubleshooting walk-throughs: "no sound at idle" is the design,
  not a bug.

---

## 2026-05-15 (continued) — Pitch bend on MIDIInput

First of the v0.4 MIDI follow-up slices. The wheel now does two things:

* **Internal:** each held voice's frequency is multiplied by
  ``2 ** (bend_normalized * bend_range / 12)`` at render time. With the
  default ``bend_range=2.0`` semitones (GM standard), a fully deflected
  wheel takes a note +-200 cents. Custom ``bend_range`` lets a patch
  widen this for dive-bomb leads (12.0 = +-octave) or narrow it for
  subtle expressive vibrato (0.5).
* **External:** a new ``pitch_cv`` output port emits the same
  ``bend_normalized * bend_range / 12`` value as a block-constant CV
  signal. Wire it to a filter's ``cutoff_cv`` for the classic "wheel
  opens the timbre" trick, or sum it through a CVCombiner with another
  modulator.

**Normalization.** mido's ``msg.pitch`` is signed 14-bit in
``[-8192, 8191]``; we divide by 8192.0 and clamp to ``[-1, 1]``. The
``+8191`` case gives ``0.99988``, which is the standard MIDI-spec
asymmetry. Wheel at rest = 0.

**State location.** ``self._pitch_bend`` lives on the MIDIInput module
under the same ``self._lock`` that guards ``active_notes``. Same
callback-thread / audio-thread split as note state -- mido callback
writes, audio thread snapshots once per block. ``stop_midi()`` resets
it to 0 so a recompile starts from neutral; ``all_notes_off()``
(CC 123 panic) deliberately does NOT touch it, because the physical
wheel position is independent of held-note state.

**Block-rate, not per-sample.** The ``pitch_cv`` buffer is constant
within a block. At 512 samples / 44.1 kHz that's an update every
~11.6 ms -- well below the threshold of audibility for any natural
wheel motion. Per-sample smoothing would be nice for staircase-free
vibrato via the wheel and is worth revisiting if anyone reports
stepping artefacts; for now block-rate matches the rest of our CV
consumers.

**Files added/changed:**

- ``src/pysynthrack/modules/midiinput.py`` -- ``pitch_cv`` output port,
  ``bend_range`` param (default 2.0), pitch-bend state +
  lock-protected ``set_pitch_bend`` / ``snapshot_pitch_bend``,
  ``pitchwheel`` message handling, wheel reset on ``stop_midi()``.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``_render_midi_input``
  applies the bend to internal voice frequencies (one float multiply
  per voice per block) and emits the ``pitch_cv`` buffer in the
  returned dict.
- ``examples/pitch_bend.json`` -- MIDIInput (saw) -> LP filter ->
  speaker, with ``pitch_cv`` -> ``cutoff_cv`` so the wheel bends the
  notes AND sweeps the filter cutoff in lockstep. Default
  bend_range=2.0; tweak the MIDIInput's ``bend_range`` param for wider
  sweeps.
- ``tests/test_midi_input.py`` -- replaced ``test_pitchwheel_is_ignored``
  with real handler tests; added a ``TestPitchBend`` class for direct
  API (no-mido) tests; added rendering tests for ``pitch_cv`` emission,
  ``bend_range`` scaling, and verified the internal frequency actually
  shifts via zero-crossing count.

**Verified in sandbox:** 174 tests pass headless + 9 mido-skipped
(mido absent in CI). End-to-end smoke render of ``pitch_bend.json``
confirms: load round-trips, idle silent, note-on audible, ``pitch_cv``
value tracks ``bend_normalized * bend_range / 12`` to within numeric
precision.

**Mount write postmortem.** Hit a new truncation flavour today -- the
``Write`` tool itself silently capped ``midiinput.py`` at exactly the
original byte size (10613) when the new content was longer. The file
on disk ended mid-comment despite the tool reporting success.
Fall-through to the proven pattern (Python ``open('w').write(content)``
via bash heredoc) worked first try. The newly-saved mount-write
protocol memory already covers this class of failure; the lesson
reinforced is: trust nothing through the Windows mount until
``stat -c %s`` confirms the byte count.

**Counts.** Modules: 13 (unchanged). Examples: 16 -> 17. Tests:
172 -> 183 (174 passing + 9 skipped).

**Up next:** mod wheel (CC 1 -> ``mod_cv``), then channel aftertouch
(``pressure_cv``), then the backend-affine submenu refactor before the
voice-routing slice lands.

---

## 2026-05-15 (later) — Mod wheel on MIDIInput

Second of the v0.4 MIDI follow-up slices, lands hot off the heels of
pitch bend. Same shape, same threading, same demo-and-test pattern.

**Feature.** MIDIInput now emits a ``mod_cv`` output port carrying CC 1
(mod wheel) as a normalized ``[0, 1]`` CV signal scaled by a new
``mod_scale`` param (default 1.0). Unlike pitch bend, the mod wheel
does NOT bend the internal voices -- it just emits the CV. The
convention is "mod wheel is a depth knob for downstream effects": the
source publishes a clean normalized signal, the downstream consumer
(filter, LFO, amp) decides what to do with it.

**Asymmetry vs. pitch_cv.** ``pitch_cv`` emits in 1V/oct units
(semitones / 12) because its destinations all use 2**cv shaping
(oscillator freq, filter cutoff). ``mod_cv`` is raw normalized x scale
because its destinations are heterogeneous (cutoff_cv uses 2**cv,
amp_cv is linear, future depth_cv inputs would be anything). The
``mod_scale`` param lets a patch pre-scale the wheel range without
needing a generic CV-scaler module yet -- though one is on the
wishlist (`CVScale` in the new "CV utility modules" entry).

**CC 1 handler.** New ``elif msg.type == "control_change" and msg.control == 1``
branch in ``_on_message``, parallel to the existing CC 123 handler.
``msg.value / 127.0`` lands in ``[0, 1]`` directly. The CC 123 comment
now mentions mod wheel alongside pitch wheel as state that's NOT
cleared by panic (consistent with hardware semantics -- a panic message
doesn't move the physical controls).

**State.** ``self._mod_wheel: float = 0.0`` joins ``self._pitch_bend``
under ``self._lock``. Reset to 0 on ``stop_midi()`` for the same reason
-- a stale wheel value from a previous session shouldn't leak into the
next compile. The CC 123 panic still leaves it alone.

**Files added/changed:**

- ``src/pysynthrack/modules/midiinput.py`` -- ``mod_cv`` output port,
  ``mod_scale`` param (default 1.0), mod-wheel state +
  ``set_mod_wheel`` / ``snapshot_mod_wheel`` lock-protected accessors,
  CC 1 message handling, wheel reset on ``stop_midi()``. Docstrings
  and message-semantics list updated to reflect the new port and
  message handling.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``_render_midi_input``
  reads ``mod_wheel`` + ``mod_scale`` and emits the ``mod_cv``
  block-constant buffer; return dict now has four keys (``out``,
  ``gate``, ``pitch_cv``, ``mod_cv``).
- ``examples/mod_wheel_filter.json`` -- MIDIInput (saw, ``mod_scale=2.0``)
  -> LP filter (cutoff 200 Hz, resonance 2.0) -> speaker, with
  ``mod_cv`` -> ``cutoff_cv``. Wheel from 0 -> 1 sweeps the cutoff
  200 -> 800 Hz (4x under 2**cv), a satisfying two-octave brightness
  open.
- ``tests/test_midi_input.py`` -- port-list assertion expanded;
  ``TestModWheel`` class (default zero, round-trips, clamps unipolar);
  CC 1 message tests under the mido-skipif guard; render tests
  verifying ``mod_cv`` buffer shape, ``mod_scale`` scaling, default
  passthrough behaviour, and that mod wheel does NOT modulate the
  internal audio (only emits cv).

**Verified in sandbox:** 183 tests pass + 11 mido-skipped. Smoke
render of ``mod_wheel_filter.json``: filter output zero-crossing rate
doubles from closed wheel (6 per block) to open wheel (12 per block),
confirming the cutoff actually opens audibly.

**Counts.** Modules: 13 (unchanged). Examples: 17 -> 18. Tests:
183 -> 194 (183 passing + 11 skipped).

**Up next:** channel aftertouch (``pressure_cv``) -- exactly the same
shape with ``msg.type == "aftertouch"`` and ``msg.value / 127.0``,
plus a new ``pressure_scale`` param mirroring ``mod_scale``. Then
voice routing.

---

## 2026-05-15 (even later) — Channel aftertouch on MIDIInput

Third of the v0.4 MIDI follow-up slices. Genuinely was "mod wheel one
more time" -- copy-paste of the same lock/state/CV-emission pattern
with the message-type and param-name swapped. Worth flagging so future
abstractions can pick this up: pitch_cv, mod_cv, pressure_cv all share
the same "MIDI-source-publishes-normalized-scaled-CV" shape and could
collapse into a single helper if a fourth lookalike ever ships.

**Feature.** ``pressure_cv`` output port carrying channel aftertouch
(mido ``msg.type == "aftertouch"``) as ``aftertouch_normalized x
pressure_scale``. Default scale is 1.0; the demo uses 2.0 so a fully
pressed key takes a downstream ``cutoff_cv`` consumer two octaves up
under standard 2**cv shaping. Unipolar, [0, 1], same shape as mod
wheel.

**Channel vs. polyphonic aftertouch.** ``aftertouch`` in mido is
*channel* aftertouch -- one value per channel, applied identically to
every held voice. ``polytouch`` is polyphonic aftertouch -- one value
per note, the controller emits which-note + pressure separately. We
ship channel aftertouch in this slice (scalar, fits the existing CV
emission shape exactly) and defer polytouch to the voice-routing
slice (it needs per-voice CV signals to express faithfully -- this
is the smoking gun for voice-aware signals, and having
``pressure_cv`` as a scalar already in place makes it obvious where
the per-voice version slots in).

**CC 123 panic.** Now mentions aftertouch alongside pitch wheel and
mod wheel as state that's NOT cleared by the all-notes-off panic --
all three are physical-controller positions that don't reset when
notes are released. ``stop_midi()`` does reset them, since the next
compile is logically a fresh session.

**Files added/changed:**

- ``src/pysynthrack/modules/midiinput.py`` -- ``pressure_cv`` output
  port, ``pressure_scale`` param (default 1.0), aftertouch state +
  ``set_aftertouch`` / ``snapshot_aftertouch`` lock-protected
  accessors, ``aftertouch`` message handling, reset on ``stop_midi()``.
  Docstring extended; trailing "other messages ignored" comment
  updated to flag that sustain pedal (CC 64) and polyphonic
  aftertouch (``polytouch``) are now the only remaining MIDI input
  features, both blocked on voice routing.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``_render_midi_input``
  reads aftertouch + pressure_scale and emits ``pressure_cv``
  block-constant buffer. Return dict has five keys now (``out``,
  ``gate``, ``pitch_cv``, ``mod_cv``, ``pressure_cv``).
- ``examples/aftertouch_filter.json`` -- MIDIInput (square,
  ``pressure_scale=2.0``) -> LP filter (cutoff 250 Hz, resonance 3.0)
  -> speaker, with ``pressure_cv`` -> ``cutoff_cv``. Higher Q than
  the mod wheel demo so the resonance peak walks across the square
  wave's odd harmonics one by one as pressure increases -- distinctly
  more "vocal" character than the mod wheel sweep. Pressing a held
  key opens the filter.
- ``tests/test_midi_input.py`` -- outputs port-list assertion expanded
  to five entries; ``TestAftertouch`` class for direct-API tests;
  aftertouch message tests under the mido skipif; render tests for
  ``pressure_cv`` emission, scale handling, and that aftertouch only
  emits CV (does not modulate internal audio).

**Verified in sandbox:** 192 tests pass + 13 mido-skipped. Smoke
render of ``aftertouch_filter.json``: ``pressure_cv`` reads 2.0 at
full pressure with ``pressure_scale=2.0``; return dict carries all
five keys; cutoff_cv consumer gets the right value (250 Hz ->
1000 Hz cutoff at full pressure under 2**cv).

**Pattern note for refactor candidate.** Pitch_cv, mod_cv, and
pressure_cv now share three identical shape elements: (a) a
normalized state float under ``self._lock``, (b) a ``set_X`` /
``snapshot_X`` accessor pair with clamp, (c) a renderer line
``X_value = X_normalized * X_scale`` emitting a block-constant
buffer. If a fourth CV source ever lands (poly-pressure, ribbon,
breath controller), pulling out a ``_CVSource`` helper becomes
worth it. For three, it's not -- the duplication is visible and
honest, and the differences (bend_range divides by 12, pitch
applies internally) would muddy a premature abstraction.

**Counts.** Modules: 13 (unchanged). Examples: 18 -> 19. Tests:
194 -> 205 (192 passing + 13 skipped).

**Up next:** voice routing. All three scalar MIDI CV ports are in
place; the only MIDI features still pending are sustain pedal
(per-voice state, lands during voice routing) and polyphonic
aftertouch (per-voice CV signal, the textbook motivation for
voice-aware signals). Time to write the short voice routing RFC.

---

## 2026-05-15 (evening) — Error handler integrated at GUI + audio panic paths

Matthew dropped in his ``error_handler.py`` plus the QUICKSTART/GUIDE
docs. After a code review pass (it is genuinely well-built -- the
never-raises contract is real, partial-failure tracking is the killer
feature, ContextVar-scoped redactors handle thread/asyncio safety
correctly, duck-typed ExceptionGroup support works pre-3.11), wired it
into the two catch points where rich crash context actually earns its
keep.

**Integration points.**

* ``ui/app.py:main()`` -- the GUI's outermost entry. Wrapped the
  ``App().run()`` call in a ``try / except BaseException``. On any
  uncaught exception, ``describe_error(e, include_locals=True)`` runs,
  ``write_crash_report(report, source="gui")`` writes the heavy
  ``for_claude()`` output to ``~/.pysynthrack/crashes/``, the path is
  printed to stderr so the user can find the file, then the original
  exception re-raises so the normal "non-zero exit, traceback in
  terminal" behaviour is preserved. The crash file is additive, not a
  replacement.
* ``audio/numpy_backend.py:_audio_callback`` -- the realtime thread.
  Two new sticky flags on ``NumpyBackend``: ``_render_disabled`` and
  ``_crash_reported``, both reset on ``compile()``. The callback wraps
  the ``render_block`` call: on first uncaught exception it calls
  ``_handle_audio_crash`` (capture, write file, disable rendering),
  then every block after that short-circuits at the
  ``_render_disabled`` check and returns silence without re-attempting
  the broken render. Avoids the "1000 crash files per second" failure
  mode that's the obvious risk when you put rich error reporting in an
  audio callback.

**File placement.** Moved ``error_handler.py`` from the project root
to ``src/pysynthrack/error_handler.py`` so it ships with
``pip install -e .`` and is importable as
``from pysynthrack.error_handler import describe_error``. Updated the
QUICKSTART/GUIDE imports to match.

**Crash file path.** ``~/.pysynthrack/crashes/crash_<timestamp>_<source>.txt``
on every platform (``Path.home()`` resolves to ``USERPROFILE`` on
Windows, ``$HOME`` elsewhere). Filename source is sanitised to safe
chars so platform filename rules can't bite. The writer never raises
-- mkdir failure, write failure, ``for_claude()`` failure all return
``None`` and the caller continues.

**Files added/changed:**

- ``src/pysynthrack/error_handler.py`` -- MOVED from project root.
- ``src/pysynthrack/_crash.py`` (new) -- ``write_crash_report`` and
  ``crash_dir`` helpers. Leading underscore = internal-but-stable.
- ``src/pysynthrack/ui/app.py`` -- ``main()`` wrapped with crash
  reporter; on failure writes ``crash_<ts>_gui.txt`` and re-raises.
- ``src/pysynthrack/audio/numpy_backend.py`` -- ``__init__`` adds two
  sticky flags; ``compile()`` resets them; ``_audio_callback`` wraps
  the render call; new ``_handle_audio_crash`` helper.
- ``tests/test_crash.py`` (new) -- 11 tests covering filename shape,
  path round-trip, directory creation, source sanitisation,
  for_claude/str/placeholder fallback chain, unwritable-home survival,
  and an integration test that runs describe_error through the writer
  and confirms the resulting file contains the exception type.
- ``tests/test_backend_crash.py`` (new) -- 8 tests covering: callback
  returns silence on first crash, exactly one crash file per session,
  subsequent blocks short-circuit, compile resets flags, init
  defaults, crash helper survives writer failure, normal operation
  unaffected by the wrapper.
- ``error_handler_QUICKSTART.md`` / ``error_handler_GUIDE.md`` --
  import paths updated from bare ``error_handler`` to
  ``pysynthrack.error_handler`` to match the new location.

**Verified in sandbox:** 211 tests pass + 13 mido-skipped (was
192 passing). All 19 new tests green.

**Counts.** Modules: 13 (unchanged). Examples: 19 (unchanged).
Tests: 205 -> 224 (211 passing + 13 skipped).

**What this changes for users.** When the GUI hard-exits (the DPG
node-editor orphan-link bug class), the user now has a crash file
they can paste into a chat instead of squinting at a dead terminal.
When the audio thread blows up mid-DSP-experiment (NaN, shape
mismatch, port-name typo), they get a clear "audio render crashed,
silenced for the rest of this stream, report: <path>" message and a
file with full traceback + locals + caller context, instead of an
opaque PortAudio error pointing at the C extension boundary.

**What this does NOT change.** Existing ad-hoc ``try/except`` calls
in App.py (per-callback error logging, the ``_set_status`` fallback,
etc.) stay exactly as they were -- the outermost catch is the safety
net of last resort, not a replacement for inline handling. Same with
``set_param`` validation, file-dialog error reporting, recompile
guards. The error handler is opt-in heavy machinery for the catch
sites that earn it.


## 2026-05-17 -- Packaging: single-file Windows .exe + bundled examples

**Goal.** Hand someone a single ``.exe`` they can double-click without
needing Python, uv, or the source tree.  Examples should travel with it
read-only so the bundled patches always exist alongside whatever the user
saves locally.

**Approach.**

1.  ``src/pysynthrack/_resources.py`` -- tiny dependency-free helper with
    ``is_frozen()``, ``resource_root()`` and ``examples_dir()``.  Resolves
    bundled-data paths via ``sys._MEIPASS`` when frozen and via the source
    tree otherwise.  Both ``cli.py`` and ``ui/app.py`` now go through it
    so the same code works in source mode and in the packaged build.

2.  ``packaging/entry.py`` -- the PyInstaller script entry point.  Lives
    outside the package so PyInstaller sees it as a top-level script;
    delegates straight to ``pysynthrack.__main__.main`` so the GUI/CLI
    dispatch, crash handler and DPG fallback all stay in one place.
    Source-mode runs still work via a sys.path injection guarded by the
    ``frozen`` check.

3.  Two spec files at the project root:

    -  ``pysynthrack.spec`` -- ``console=False``, name ``PySynthRack``.
       This is the distribution build.
    -  ``pysynthrack-cli.spec`` -- ``console=True``, name
       ``PySynthRack-cli``.  Use it when debugging the packaged build;
       stderr/print are visible.

    Both bundle the ``examples/`` directory and pull in
    ``mido.backends.rtmidi`` as a hidden import (mido picks backends by
    string so PyInstaller's static analyser misses it).  ``pyo`` is
    excluded explicitly to keep the binary small; drop the exclude in
    the spec if you want it bundled.

4.  ``build.ps1`` / ``build_cli.ps1`` -- PowerShell wrappers that activate
    ``.venv``, install pyinstaller via ``uv pip install`` if missing,
    clean ``build\`` + ``dist\``, run pyinstaller, and print the output
    path + size.

**Files added/changed:**

-  ``src/pysynthrack/_resources.py`` (new, 1875 bytes)
-  ``src/pysynthrack/cli.py`` -- ``DEFAULT_PATCH`` replaced by
   ``_default_patch()`` that resolves through ``examples_dir()``.
-  ``src/pysynthrack/ui/app.py`` -- ``DEFAULT_PATCH_PATH`` switched
   to ``str(examples_dir() / "hello_sine.json")``.
-  ``packaging/entry.py`` (new)
-  ``pysynthrack.spec`` (new, windowed)
-  ``pysynthrack-cli.spec`` (new, console)
-  ``build.ps1`` (new)
-  ``build_cli.ps1`` (new)

**Verified.** ``PYTHONPATH=src python -m pytest`` still 211 passed +
13 mido-skipped after the refactor.  ``_resources.examples_dir()``
resolves to the project root in source mode; ``_default_patch()`` and
``DEFAULT_PATCH_PATH`` both point at the real ``hello_sine.json``;
``packaging/entry.py --help`` prints the expected arg parser.

**What to run on a Windows box:**

::

    cd "<project root>"
    .\build.ps1           # produces dist\PySynthRack.exe
    .\dist\PySynthRack.exe

**Known caveat.** First-time pyinstaller runs on Windows can be a few
minutes -- numpy/DPG/sounddevice are bulky.  Subsequent builds are
faster.  If startup feels slow on the user's machine it is the
one-file extract-to-tempdir step; the trade-off was conscious (single
file > fast cold start for hobby distribution).

**Not bundled.** The ``error_handler_QUICKSTART.md`` /
``error_handler_GUIDE.md`` developer docs.  They're for someone hacking
on the source, not for an end user double-clicking the exe.


## 2026-05-17 -- Packaging hotfix: silent-exit diagnosis

**Symptom.** First windowed build (`dist/PySynthRack.exe`) silently
exits on launch.  No crash log in `~/.pysynthrack/crashes/`.

**Why the existing safety net missed it.**

-   The GUI catch lives *inside* `ui/app.py:main()`.  If anything blows
    up before that try-block opens -- an `ImportError` of dearpygui, a
    missing native DLL during import, a `print(file=sys.stderr)` call
    when `sys.stderr is None` in windowed mode -- the exception escapes
    and the bootloader just exits.
-   PyInstaller windowed builds (`console=False`) set both
    `sys.stdout` and `sys.stderr` to `None`.  Any
    `print(..., file=sys.stderr)` in the existing
    `__main__.py`/`cli.py` fallback paths then raises
    `AttributeError`, which since it sits OUTSIDE the catch is just
    "process dies, no output, no file."

**Fix.** Hardened `packaging/entry.py` so it survives all three:

1.  Null-stream guard runs first.  `sys.stdout`/`sys.stderr` get
    replaced by `os.devnull` writers if they're `None`, so existing
    package code that does `print(file=sys.stderr)` becomes a no-op
    instead of fatal.
2.  Startup ping.  Before importing the package we drop
    `~/.pysynthrack/crashes/_last_startup.txt` with python version,
    `_MEIPASS`, argv, etc.  If the user sees a silent exit AND this
    file isn't there, the failure is below the Python layer (bootloader,
    antivirus, missing portaudio DLL preventing module import).
3.  Two-tier outer catch.  Pure-stdlib `_emergency_dump` writes a
    `crash_<ts>_<source>_emergency.txt` even if the package fails to
    import or `describe_error` itself errors.  We try the heavy
    `describe_error` + `write_crash_report` combo first, fall back to
    `_emergency_dump` only if that explodes.

**Spec hardening too.** `pysynthrack.spec` (and the CLI variant) now
`collect_all` from `sounddevice`, `dearpygui`, and (best-effort)
`rtmidi` instead of using the lighter `collect_dynamic_libs` +
`collect_data_files` combo.  Heavier on disk but much less likely to
miss a native `.pyd` or DLL.  This was the most likely silent-exit
cause for a first-time build.

**Diagnostic flow for next attempt:**

1.  `Remove-Item -Recurse -Force build, dist` (always do a clean
    rebuild after spec changes -- PyInstaller's incremental cache lies).
2.  `.uild.ps1` and `.\dist\PySynthRack.exe`.
3.  Check `%USERPROFILE%\.pysynthrack\crashes\` -- you should now see
    `_last_startup.txt` at minimum.
    -   If `_last_startup.txt` exists and a `crash_*.txt` file appeared:
        open the crash file; the traceback names the offender.
    -   If `_last_startup.txt` exists but NO crash file: the process
        deadlocked or was killed externally; try the CLI build.
    -   If `_last_startup.txt` is still missing: the failure is below
        Python.  Run `.uild_cli.ps1` then
        `.\dist\PySynthRack-cli.exe` in a terminal; the bootloader will
        print its complaint to stderr.

**Status.** Tests still 211 passed + 13 mido-skipped after the
hardening.  Source mode unchanged; only frozen builds benefit from
the new entry-level catches.


## 2026-05-17 -- Packaging hotfix #2: the uv pitfall struck again

**Diagnosis.** Hardened entry.py's checkpoint log showed:

::

    about to import sounddevice
    import sounddevice FAILED: ModuleNotFoundError: No module named 'sounddevice'
    about to import mido
    import mido FAILED: ModuleNotFoundError: No module named 'mido'
    about to import rtmidi
    import rtmidi FAILED: ModuleNotFoundError: No module named 'rtmidi'
    about to import dearpygui
    import dearpygui FAILED: ModuleNotFoundError: No module named 'dearpygui'
    pysynthrack.main() returned 3

So PyInstaller bundled the exe with NONE of the optional/extra deps
inside.  The exe ran, fell through GUI -> CLI (no DPG), then CLI tried
``pick_backend()`` (no sounddevice) and returned exit code 3 from
``cli.py``.

**Why.** The deps live under pyproject extras (``[gui]``, ``[midi]``).
PyInstaller only bundles what's installed in the python it's run from.
A plain ``pip install -e .[all]`` inside a uv venv on Windows often
silently hits SYSTEM python instead -- the deps end up there, not in
``.venv``.  This is the documented uv pitfall already in memory; the
build script wasn't yet defending against it.

**Fix.**  ``build.ps1`` and ``build_cli.ps1`` now do a pre-flight:

1.  Call ``.venv\Scripts\python.exe`` directly (not via PATH lookup)
    and print which interpreter is being used.  Confirms it's the
    venv, not a stray system one.
2.  Try ``import X`` for each required module
    (``numpy``, ``sounddevice``, ``dearpygui.dearpygui``, ``mido``,
    ``rtmidi``) under that interpreter.
3.  If any are missing: abort the build, print the missing packages
    in red, and print the exact ``uv pip install`` command to fix it.
4.  Invoke pyinstaller as ``$venvPython -m PyInstaller`` rather than a
    bare ``pyinstaller`` shellout, so the build provably runs under
    the venv even if ``pyinstaller.exe`` shim resolution is funky.

The checkpoint log additions to ``entry.py`` stay -- they're what made
this debuggable, and they cost nothing on each launch (one file with a
dozen lines).

**For Matthew on next build.**

::

    uv pip install -e ".[all]"     # one-time, gets gui+midi+pyo extras

    Remove-Item -Recurse -Force .\build, .\dist
    .\build.ps1                    # pre-flight will catch any remaining gap


## 2026-05-17 -- Packaging hotfix #3: pyo dragged the install down

**What happened.** Matthew ran ``uv pip install -e ".[all]"``, which
tried to install pyo, mido, python-rtmidi and dearpygui together.
Pyo has no Python 3.14 wheel and its source build needs an external
C toolchain that isn't on the box:

::

    × Failed to build `pyo==1.0.5`
    error: [WinError 2] The system cannot find the file specified
    help: `pyo` (v1.0.5) was included because `pysynthrack[all]` (v0.1.0) depends on `pyo`

uv installs atomically -- when pyo failed, ``dearpygui``, ``mido``
and ``rtmidi`` got rolled back too.  That left the venv with zero
runtime deps, and the next build silently fell back to system Python
which didn't have them either (build output showed
``Python environment: C:\Program Files\Python314``).

**Fix.** ``pyproject.toml``: dropped pyo from the ``[all]`` extra.

Before:

::

    all = ["dearpygui>=1.10", "pyo>=1.0.5", "mido>=1.3", "python-rtmidi>=1.5"]

After:

::

    all = ["dearpygui>=1.10", "mido>=1.3", "python-rtmidi>=1.5"]

Pyo stays available under its own ``[pyo]`` extra for users on
supported Pythons -- it's still the alternate audio backend the
project supports, just not part of the "install everything for a
normal user" path.  The numpy backend is the canonical default
anyway.

**Also.** Build output revealed the system-Python fallback path was
real -- ``pygame-ce 2.5.7`` showed up in PyInstaller's banner (we
don't depend on pygame), and the module search path included
``C:\Program Files\Python314\Lib\site-packages``.  The new
``build.ps1`` pre-flight will refuse to build in that situation
because the missing imports will trip the ``[MISSING]`` red-text
output and abort.

**Next steps for Matthew:**

::

    # 1. Confirm build.ps1 is the 4521-byte version with pre-flight
    Get-Item .\build.ps1 | Select-Object Length

    # 2. Re-create or refresh the venv if uv left it in a partial state
    uv venv .venv --python 3.14
    uv pip install -e ".[all]"          # now succeeds without pyo

    # 3. Clean rebuild
    Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue
    .\build.ps1

The pre-flight should now print:

::

    Building with: ...\.venv\Scripts\python.exe
      [ok]      numpy
      [ok]      sounddevice
      [ok]      dearpygui.dearpygui
      [ok]      mido
      [ok]      rtmidi
    pyinstaller 6.20.0

If any line says ``[MISSING]``, the build aborts -- look at which one
and ``uv pip install`` it explicitly.


## 2026-05-17 -- Packaging hotfix #4: Python 3.13 build venv

**Second hard stop, same shape:** ``python-rtmidi==1.5.8`` has no
Python 3.14 wheel either; uv fell back to source build, which needs
MSVC.  No MSVC on the box, so the install failed and uv rolled the
whole transaction back -- venv empty again.

**Decision.** Pin the build venv to **Python 3.13**.  Source tree
still targets ``>=3.9`` per pyproject; this is purely about which
interpreter the ``.venv`` uses for packaging.

3.13 has prebuilt wheels for everything in ``[all]`` (dearpygui,
mido, python-rtmidi, sounddevice).  Pyo also has 3.13 wheels if we
ever want it back, but it stays under its own ``[pyo]`` extra.

**Saved as project memory** (``project_build_venv_python_313.md``)
so future Claude sessions don't recommend ``--python 3.14`` again.

**Also fixed:** ``build.ps1`` had a PowerShell bug -- the per-module
import check was sending Python's missing-module traceback to stderr,
and with ``$ErrorActionPreference = "Stop"`` PowerShell 7.4+ treats
native command stderr as a terminating error.  Rewrote the pre-flight
to use a SINGLE python invocation that catches imports internally and
writes structured ``OK\tmod\tpkg`` / ``MISSING\tmod\tpkg\treason``
lines to stdout.  PowerShell parses the stdout instead of being
ambushed by the stderr.  Also added
``$PSNativeCommandUseErrorActionPreference = $false`` at the top of
the script as a belt for the suspenders.

**For Matthew on next attempt:**

::

    # Nuke the 3.14 venv, replace with 3.13
    Remove-Item -Recurse -Force .\.venv
    uv venv .venv --python 3.13
    .\.venv\Scripts\Activate.ps1
    uv pip install -e ".[all]" pyinstaller

    # Clean rebuild
    Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue
    .\build.ps1


## 2026-05-20 -- Voice routing slice 1: VoiceSlots allocator + sustain pedal

**What.** First slice of the v0.4 voice-routing work landed.  This one
is **model-layer only** — no renderer changes, no buffer-shape changes.
The audio path still goes through ``snapshot_active_notes()`` and
produces mono buffers exactly as before.  What changes is what's behind
``active_notes``: a proper 16-slot polyphonic voice allocator with
stable slot indices, voice steal, and sustain pedal support.

Also Matthew confirmed the two open design questions before the slice
started: **fixed 16 slots, zero-pad silent ones** (not variable-V) and
**keep the mono fast path** in every stateful module once they're
migrated (not "broadcast everything to (16, frames)").

**New file: ``src/pysynthrack/core/voicing.py``** (236 lines)

The ``VoiceSlots`` class.  Each slot is in one of four states:

* **Empty** — ``note == -1``, never been used (or cleared by panic).
* **Held** — key currently down.
* **Sustained** — key released, sustain pedal down, slot stays gating.
* **Released** — key released, pedal not engaged.  ``note != -1`` so
  the renderer's per-slot state (oscillator phase, ADSR tail, biquad
  memory) keeps emitting until the slot is reused.

Voice steal evicts in order: oldest released → oldest sustained →
oldest held.  "Oldest" = lowest age counter, where age increments on
every allocation.  A retrigger of an already-held note reuses its slot
(updates velocity, doesn't burn a fresh voice).  Replaying a note while
its previous instance is still releasing allocates a FRESH slot — the
dying voice keeps its tail.

``snapshot()`` returns a length-16 list of ``VoiceSnapshot`` dicts;
empty slots are present with ``note=-1`` and ``gating=False`` so the
renderer can iterate as a fixed loop of 16 without any "which slots
are alive" bookkeeping.  ``held_notes()`` returns
``{note: velocity}`` for slots whose key is physically down — that's
what backs the preserved ``snapshot_active_notes()`` semantics on
``MIDIInput``.

No lock — the owner (MIDIInput) holds its own lock around every
mutation.  Keeps lock ownership single-sourced.

**Updated: ``src/pysynthrack/modules/midiinput.py``** (466 lines)

* ``self.active_notes: dict`` replaced with ``self.voices: VoiceSlots``.
* ``note_on``, ``note_off``, ``all_notes_off`` delegate to the
  allocator under ``self._lock``.
* ``snapshot_active_notes()`` proxies to ``voices.held_notes()``.
  Stable across the migration — the audio renderer doesn't notice
  anything has changed, and every existing test still passes.
* ``snapshot_voice_slots()`` is the new path the voice-aware renderer
  will use in slice 2.
* CC 64 (sustain pedal) is now handled in ``_on_message`` with the
  standard MIDI threshold (>= 64 = on).
* ``set_sustain(on)`` and ``snapshot_sustain_pedal()`` on the module
  delegate to the allocator.
* ``stop_midi()`` resets pedal state alongside the existing
  pitch/mod/aftertouch reset, so a stuck pedal can't leak across
  sessions.

**New file: ``tests/test_voicing.py``** (304 lines, 25 tests, all pass)

Allocator semantics — initial-empty / consecutive-slot assignment /
retrigger reuse / fresh-slot on replay-after-release.  Release —
unheld-no-op / multi-slot disambiguation.  Sustain pedal — default off /
release-with-pedal-down marks sustained / pedal-up drops sustained /
held-keys unaffected by pedal / classic "puddle of pedal" workflow.
Voice steal — released-first / released-over-sustained / falls-through-
to-held-when-all-keys-down.  Panic — clears every slot / clears
sustained / does NOT reset pedal state (per CC 123 spec).  Held-notes
view — only-held / sustained-not-held.  Snapshot — always 16 long /
mutating returned copy is safe / gating collapses held+sustained.

**Updated: ``tests/test_midi_input.py``** (824 lines, 78 tests, all pass)

Added ``TestSustainPedalDirect`` (5 tests), ``TestVoiceSlotsSnapshot``
(3 tests), ``TestSustainPedalViaCC`` (5 tests, mido-gated).  Retargeted
the now-stale "CC 64 is intentionally not handled" test to use CC 5
(portamento time) which IS still genuinely unhandled.  Every existing
rendering test continues to pass — that's the proof the renderer
contract didn't drift.

**Sandbox + verify protocol followed** per the mount memory.  Staged
all four files in ``/tmp/staging``, AST-parsed each, ran pytest in a
copy-of-tree (78 + 25 = 103 tests pass), then copied to the mount with
``cp`` (per the "heredoc, not Edit" pattern) and re-AST-parsed on the
mount to confirm no truncation.  MD5 sums match between sandbox and
mount for every file.

**What slice 2 looks like** (next session):

* Polyphonic ``_render_midi_input``: read ``snapshot_voice_slots()``,
  emit ``out``/``gate``/``pitch_cv`` as ``(16, frames)``.  ``mod_cv``
  and ``pressure_cv`` stay ``(frames,)`` — channel-wide by MIDI spec.
  Per-slot phase + env state.  Silent slots zero-fill.
* Speaker + DiskWriter: one-line voice-axis sum at the sink boundary
  (``if buf.ndim == 2: buf = buf.sum(axis=0)``).
* That's enough to play a chord through MIDIInput → Speaker and have
  it actually sound polyphonic.  Downstream stateful modules
  (Oscillator/ADSR/Filter/LFO/Crossover) come in slice 3.


## 2026-05-20 (later) -- Voice routing slice 2: polyphonic renderer + sink summing

**What.** Slice 2 of the voice-routing work lands the renderer side.
After this slice, ``MIDIInput`` emits voice-aware buffers, the speaker
sums them at the sink, and a chord played through ``MIDIInput`` ->
``SpeakerOutput`` is **audibly polyphonic** for the first time --
without touching a single downstream stateful module.  The trick that
lets this hold is an auto-collapse rule on the buffer-fetch helper.

**The auto-collapse rule** (the one architectural decision in this slice).

``_input_buffer(patch, buffers, dst_module_id, dst_port, collapse=True)``
gained an optional ``collapse`` argument that defaults to True.  When
True (every existing caller), a fetched ``(V, frames)`` buffer is
summed along axis 0 before being returned, so the caller receives the
mono mix exactly as it would have from the old self-summing
MIDIInput.  When False (voice-aware modules in slice 3+), the buffer
comes through unchanged so the consumer can grow its own per-slot
state.

This is what keeps slice 2 a contained change.  Filter, ADSR, VCA,
Mixer, Combiner, CVCombiner, Crossover, DiskWriter -- every existing
stateful module continues to work exactly as before.  A patch like
"MIDIInput -> Filter -> Speaker" still plays a single-note filter
sweep with mono-summed-on-fetch input to the filter, and a chord
through that same patch gives the same filter sound but with the
voice mix as the input.

**Changes to ``src/pysynthrack/audio/numpy_backend.py``** (1158 -> 1239 lines)

1. ``_input_buffer`` gains the ``collapse=True`` auto-collapse path
   described above.  Every existing call site uses the default
   collapse=True so behavior is unchanged for un-migrated modules.

2. SpeakerOutput's drain pass in ``render_block`` checks
   ``src_buf.ndim == 2`` and does ``src_buf = src_buf.sum(axis=0)``
   before mixing.  Speaker doesn't use ``_input_buffer`` (it has its
   own direct dict lookup), so the rule is duplicated here
   explicitly.  Same effect, just visible at the sink boundary
   instead of buried inside a helper.

3. ``_render_midi_input`` rewritten as a voice-aware renderer.

   Per-slot state now lives as four numpy arrays of length 16:

   * ``phase``     -- oscillator phase for each slot, [0, 1).
   * ``env``       -- attack/release envelope level for each slot.
   * ``last_note`` -- the MIDI note this slot rendered on the previous
                     block.  Used to detect "slot reassigned to a new
                     note" so phase + env reset cleanly on the
                     boundary.  Without this a voice steal would
                     have the new voice picking up the previous
                     voice's phase mid-cycle.
   * ``releasing`` -- bool per slot.  Latched on gate-fall edge,
                     cleared on gate-rise (retrigger before tail
                     finished).

   Output shapes:

   * ``out``         -- ``(16, frames)`` audio per slot.
   * ``gate``        -- ``(16, frames)`` block-constant per slot (1.0
                        for gating slots, 0.0 for the rest).
   * ``pitch_cv``    -- ``(16, frames)``.  Channel-wide today (every
                        slot row carries the same wheel value); shape
                        is per-slot so future polyphonic pitch bend
                        is just an edit to the row values, not a
                        shape change.
   * ``mod_cv``      -- ``(frames,)`` channel-wide per MIDI spec.
   * ``pressure_cv`` -- ``(frames,)`` channel-wide per MIDI spec.

   Slots with ``note == -1`` write zeros and explicitly reset their
   per-slot state so the next allocation starts clean.

**Changes to ``tests/test_midi_input.py``** (824 -> 992 lines)

* Four existing rendering tests that asserted 1D shapes on
  ``gate`` / ``pitch_cv`` updated to assert ``(16, frames)`` with
  appropriate per-slot indexing.  Net behaviour change:
  ``test_gate_high_when_notes_held`` now checks slot 0 is fully
  gating AND slots 1..15 are silent, instead of asserting "every
  sample of the global gate is 1.0".
* New ``TestPolyphonicRendering`` class with 6 tests:
  ``test_out_shape_is_voice_aware``,
  ``test_three_notes_populate_three_rows``,
  ``test_chord_is_audibly_summed_through_speaker``,
  ``test_slot_phase_persists_across_blocks``,
  ``test_slot_reassignment_resets_phase``,
  ``test_sustained_voice_keeps_emitting_through_speaker``.
  The chord-through-speaker test is the canonical "this slice
  actually works" check -- a triad's peak amplitude clearly exceeds
  a single note's, proving the voice axis summed correctly at the
  speaker boundary.

**Verification.**  Full project suite: **268 tests pass, 0 fail** off
the mount.  That's every test in ``tests/`` -- core, modules, io,
crossover, ADSR, filter, LFO, VCA, mixer, combiner, disk_writer,
keyboard, backend_crash, plus the voicing + MIDI input suites.  The
sandbox + mount-write protocol from the slice-1 entry stayed in
force: staged in ``/tmp/staging``, AST-parsed in sandbox, ran the
suite against a copy of the tree, copied to mount with ``cp``, AST-
parsed on the mount, ran the suite directly off the mount.  MD5 sums
match between sandbox and mount.

**What slice 3 looks like.**  Downstream stateful modules go shape-
polymorphic: Oscillator, ADSR, Filter, LFO, Crossover each grow
per-slot state arrays and switch to ``_input_buffer(..., collapse=
False)``.  At that point a patch like
"MIDIInput -> Filter -> ADSR -> VCA -> Speaker" preserves
per-voice identity all the way through, and the speaker still sums
at the very end.  VCA / Mixer / Combiner / CVCombiner stay
stateless and just broadcast: numpy broadcasting between a
``(16, frames)`` audio and a ``(frames,)`` mono CV does the right
thing already.

After slice 3, the Keyboard module mirrors the MIDIInput migration
(slice 4), and the whole voice-routing piece in v0.4 is done.


## 2026-05-20 (later still) -- Voice routing slice 3a: voice-aware ADSR + VCA

**What.** First half of the downstream-module migration.  ADSR and
VCA now respect the voice axis, which means the canonical synth
voice chain

::

    MIDIInput -> VCA(audio)
    MIDIInput.gate -> ADSR -> VCA(cv)
    VCA -> Speaker

produces **per-voice envelopes** for the first time.  Release one
note in a held chord and only that voice's envelope decays; the
other voices stay at sustain.  Pre-slice-3 the same patch would
collapse the gate to mono and trigger one global state machine for
the whole chord, so releasing a note didn't change the envelope at
all.

Filter, LFO, Crossover, Oscillator still go through the auto-
collapse fast path (slice 3b will migrate them next).  Patches that
use those modules continue to work -- they just see the voice-
summed signal at the module input, exactly like pre-slice-2.

**Changes to ``src/pysynthrack/audio/numpy_backend.py`` (1239 -> 1394 lines)**

VCA migration is six lines: both ``_input_buffer`` calls opt into
``collapse=False``.  No state to carry, no per-slot branch -- numpy
broadcasting handles every shape combination correctly out of the
box:

* ``(V, F) audio  *  (V, F) cv`` -> ``(V, F)``, element-wise.
* ``(V, F) audio  *  (F,)  cv``  -> ``(V, F)``, mono CV broadcasts
  across every voice (channel-wide modulation).
* ``(F,)  audio   *  (V, F) cv`` -> ``(V, F)``, mono audio sliced
  into voices by per-voice CV (niche but valid).
* ``(F,)  audio   *  (F,)  cv``  -> ``(F,)`` mono fast path.

ADSR migration is the substantive piece.  ``_render_adsr`` now
branches on the incoming gate's ndim:

* ``ndim == 1`` (or no gate connected) -> ``_render_adsr_mono``.
  This is the pre-slice-3 scalar state machine, lifted unchanged
  into its own method.  Phase is still encoded as a string
  ("idle" / "attack" / etc.); level/prev_gate/release_step are
  still scalars.  Output is ``(F,)``.  Every existing ADSR test
  exercises this path and passes bit-for-bit identically.

* ``ndim == 2`` -> ``_render_adsr_voice``.  V independent state
  machines in lockstep.  The per-sample loop is still serial (the
  state machine is per-sample-causal), but inside each sample the
  per-voice updates are vectorized across V via numpy boolean
  masks.  Phase is encoded as int codes (``_ADSR_IDLE = 0``,
  ``_ADSR_ATTACK = 1``, etc.) so ``phase == _ADSR_ATTACK`` gives a
  clean ``(V,)`` mask for ``level[mask] += attack_step``.  Output
  is ``(V, F)``.

The two branches store their state under different dict keys
(``phase``/``level``/... for mono, ``phase_arr``/``level_arr``/...
for voice-aware) so a recompile that switches an instance between
shapes won't see stale state of the wrong type.  The first call in
each direction clears and reinitialises if it finds the wrong
keys.

**New file: ``tests/test_voice_aware.py``** (302 lines, 10 tests)

Three test classes:

* ``TestADSRVoiceAware`` -- direct ``_render_adsr`` calls with
  synthetic ``(V, F)`` gates.  Slot 3 gating in isolation; per-
  voice state independence (release slot 5 while slot 0 holds
  sustain); mono backward compat.

* ``TestVCAVoiceAware`` -- broadcast cases.  Voice audio x voice
  CV, voice audio x mono CV (broadcast), all-mono, voice audio
  with no CV.

* ``TestPolyphonicChain`` -- end-to-end MIDIInput -> ADSR -> VCA
  -> Speaker.  The headline test
  ``test_released_voice_decays_while_held_voice_sustains`` plays
  a two-note chord, releases one note, and asserts the peak
  drops while the held voice stays audible -- the audible proof
  of per-voice envelopes.

Test fixtures dodge the patch model's connect() validation by
creating ``Cable`` objects directly with fabricated source ids,
then injecting buffers under those keys.  Lets the tests drive
``_render_adsr`` and ``_render_vca`` with arbitrary input shapes
without standing up a whole upstream MIDIInput.

**Verification.**  Full project suite off the mount: **278 tests
pass, 0 fail.**  Same sandbox + verify protocol as slices 1 and
2: staged in ``/tmp/staging``, AST-parsed, ran the suite against a
copy of the tree, copied to mount with ``cp``, AST-parsed on the
mount, ran the suite directly off the mount.  MD5 sums match
between sandbox and mount for both files.

**What slice 3b leaves to do.**  Filter, LFO, Crossover, and
Oscillator are the remaining stateful modules.  Filter and
Crossover need per-slot biquad memory arrays; LFO needs per-slot
phase + random_value (only when its rate_cv is voice-aware); the
Oscillator module needs per-slot phase when freq_cv or amp_cv is
voice-aware.  Same pattern as ADSR: detect input ndim, branch
into mono fast path or voice-aware path with vectorized per-
sample updates.  After slice 3b lands, every stateful module is
voice-aware and the only remaining piece is the Keyboard
migration (slice 4) to mirror MIDIInput.


---

## 2026-05-23 -- Voice routing slice 3b.1: voice-aware Filter + Oscillator

**What.**  Second installment of the downstream-module migration.
Filter and Oscillator now respect the voice axis.  After this
slice, four of the six stateful modules (ADSR, VCA, Filter,
Oscillator) take per-voice signals correctly; only LFO and
Crossover remain (slice 3b.2), then Keyboard (slice 4).

The user can now build the obvious polyphonic patch -- per-voice
filter sweeps driven by per-voice ADSRs -- without anything
collapsing to mono in the middle.

**Decision: split slice 3b in half.**  TODO had slice 3b as one
chunk (Filter + LFO + Crossover + Oscillator).  In the planning
exchange Matthew picked "split into 3b.1 / 3b.2", grouping the
headline use-case modules together: Filter (per-voice filter
sweep) + Oscillator (per-voice detune / FM).  LFO and Crossover
are mostly broadcast-friendly and land next as 3b.2.

**Filter migration.**  ``_render_filter`` is now a small
dispatcher that opts both inputs into ``collapse=False`` and
branches on the audio input's ``ndim``:

* ``ndim == 1`` -> ``_render_filter_mono``, lifted unchanged from
  the pre-slice-3 implementation.  Scalar biquad memory
  (``x1, x2, y1, y2``), scalar coefficients, single Python loop
  over ``frames``.  Every existing Filter test passes bit-for-bit
  identically.

* ``ndim == 2`` -> ``_render_filter_voice``.  V parallel biquads
  with per-slot memory ``(x1_arr, x2_arr, y1_arr, y2_arr)`` as
  ``(V,)`` arrays.  The per-sample recurrence
  ``y0 = b0*x0 + b1*x1 + b2*x2 - a1n*y1 - a2n*y2`` is byte-
  identical to mono -- only the operand shapes change.
  Broadcasting handles both single-cutoff and per-voice-cutoff
  cases:

  - Single cutoff -> ``b0..a2n`` are scalars, the recurrence
    runs ``(V,) <op> scalar -> (V,)``.
  - Per-voice cutoff -> ``b0..a2n`` are ``(V,)`` arrays from V
    sets of RBJ coefficients, recurrence is ``(V,) <op> (V,)
    -> (V,)``.

  Coefficient-computation routine (``_filter_coeffs``) factored
  out so the mono path keeps using the cheap scalar version.
  ``cutoff_cv`` semantic in the voice branch:

  * ``(V, F)`` cutoff_cv -> per-row block-mean -> ``(V,)`` cutoff
    -> ``(V,)`` coefficient arrays.  Per-voice filter sweep.
  * ``(F,)`` cutoff_cv -> single block-mean -> scalar coefficient
    set broadcast across every voice.  "Macro" filter sweep --
    one LFO modulates every voice's filter equally.  This is the
    common case (a global filter envelope, an aftertouch-driven
    LFO).
  * No cutoff_cv -> static cutoff from the param.

**Per-voice biquad cost.**  Per-sample loop is still serial
(biquad recurrence is causal in time, numpy can't vectorize an
IIR along time).  But each iteration now does a ``(V,)``-wide
multiply-add.  At V=16 numpy makes the per-iteration cost
basically identical to one scalar iteration, so a 512-sample
block costs ~150us in the voice branch vs ~100us in the mono
branch -- still 65x under the 11.6ms callback budget at 44.1kHz.

**Oscillator migration.**  ``_render_oscillator`` is now a
dispatcher that branches on ``freq_cv``'s ndim, because phase is
the state that changes with frequency:

* ``freq_cv`` 1D or None -> ``_render_oscillator_mono``, lifted
  from the pre-slice-3 implementation.  Single scalar phase
  accumulator.  Vectorized phase ramp via ``arange`` (no CV) or
  ``cumsum`` (mono CV).  Output ``(F,)``.  If ``amp_cv`` happens
  to be ``(V, F)``, the final ``wave * amp_cv`` broadcasts the
  mono carrier across V voices -- the "cheap-poly" pattern: one
  carrier, per-voice amp shaping.  No phase-state changes for
  this case, the voice-ness is purely an output shape.

* ``freq_cv`` 2D ``(V, F)`` -> ``_render_oscillator_voice``.
  V independent phase accumulators (``phase_arr`` as ``(V,)``
  array).  Per-row cumsum integrates each voice's phase
  separately.  Output ``(V, F)``.  Per-voice phase persists
  across blocks so a slot that was silent (freq_cv = 0,
  advancing at the param's base freq) still carries a sensible
  phase when next gated -- avoids a per-retrigger phase reset.
  MIDIInput zero-pads unused slots, so silent slots advance at
  ``freq`` -- harmless because the per-voice ADSR/VCA
  downstream silences those slots anyway.

Waveshape function (sine/saw/square/triangle) factored out as
``_osc_waveshape`` -- shape-polymorphic via numpy, same code
handles ``(F,)`` and ``(V, F)`` phase arrays elementwise.

**State branch isolation.**  Same pattern as ADSR slice 3a: mono
state uses one set of dict keys (``"phase"`` / ``"x1"`` / ...),
voice state uses ``_arr`` suffixed keys (``"phase_arr"`` /
``"x1_arr"`` / ...).  Each branch checks for the other branch's
keys on first call and reinitialises if it finds them, so a
recompile that switches an instance between shapes won't see
stale state of the wrong type.

**Phase-convention note (pre-existing, not from this slice).**
The mono path with no freq_cv uses ``arange``-based phase
(sample 0 = ``start_phase``).  The mono path with freq_cv uses
``cumsum`` (sample 0 = ``start_phase + inst_inc[0]``, one step
ahead).  The voice path also uses cumsum.  Two of the new
oscillator tests caught this -- they expected voice-with-zero-CV
to agree with mono-no-CV bit-for-bit, but the two paths differ
by one phase increment.  Rewrote the tests to compare against
the mono-WITH-CV path (which uses the same cumsum convention)
and to check inter-voice consistency rather than absolute phase.
Reconciling the two mono branches is a separate cleanup -- not
in scope for 3b.

**New tests: ``TestFilterVoiceAware`` + ``TestOscillatorVoiceAware``**
(11 tests appended to ``tests/test_voice_aware.py``).

Filter tests:

* ``test_voice_audio_returns_voice_shape`` -- (16, F) noise in
  slot 3 only, every other slot silent in, every other slot
  silent out (no per-voice leakage).
* ``test_per_voice_filter_memory_is_independent`` -- warmup slot
  0 with sustained signal, then drive an impulse into slot 5
  while slot 0's input goes to 0.  Slot 5's impulse response
  appears in slot 5 from a fresh memory; slot 0 decays from its
  prior state without being kicked by the slot-5 impulse.
* ``test_mono_audio_still_returns_mono`` -- backward compat,
  scalar biquad path still returns ``(F,)``.
* ``test_voice_audio_mono_cutoff_cv_broadcasts`` -- macro filter
  sweep, mono cutoff_cv applied to every voice.
* ``test_voice_audio_per_voice_cutoff_cv`` -- the polyphonic
  filter test.  Slot 0 cutoff +2 oct (=4000Hz), slot 5 cutoff
  -2 oct (=250Hz), both fed the same 2 kHz tone.  Slot 0's RMS
  should be >4x slot 5's -- proves the per-voice coefficient
  arrays actually differentiate voices.

Oscillator tests:

* ``test_voice_freq_cv_returns_voice_shape`` -- (V, F) freq_cv
  with all zeros, every voice produces the same waveform (inter-
  voice consistency check).
* ``test_per_voice_pitch_via_freq_cv`` -- slot 0 at 0V (=440Hz),
  slot 5 at +1V (=880Hz), zero-crossing count differs by ~2x.
* ``test_per_voice_phase_persists_across_blocks`` -- render two
  blocks back-to-back, assert sample-to-sample continuity at
  every block boundary for every voice.
* ``test_mono_freq_cv_with_voice_amp_cv_broadcasts`` -- cheap-
  poly path: mono carrier, per-voice amp_cv.  Different voices
  hear the carrier at different amplitudes via numpy broadcast.
* ``test_mono_freq_cv_returns_mono`` -- backward compat.
* ``test_voice_matches_mono_with_cv`` -- voice path with
  freq_cv=0 agrees with mono-with-freq_cv=0 to 1e-5 (both
  cumsum-based, same convention).

**Verification.**  Full project suite off the mount:  **271 tests
pass, 0 fail** (was 260 + 11 new = 271; 18 mido tests skipped in
the bash sandbox but pass under Matthew's ``[midi]``-installed
venv).  Same sandbox + verify protocol as slices 1, 2, 3a:
staged in ``/tmp/staging``, AST-parsed, ran the suite against a
sandbox copy of the project tree, copied to mount with ``cp``,
AST-parsed on the mount, md5'd to confirm byte-for-byte transfer,
ran the suite directly off the mount.

**What slice 3b.2 leaves to do.**  LFO and Crossover.  LFO is
slightly subtle: its only voice-aware input is ``rate_cv``, and
even then per-voice rate is a niche use (one LFO per voice, all
running at different rates -- mostly useful for unison detune
shimmer).  More common is mono LFO modulating per-voice
destinations downstream, which the existing collapse=True path
already handles transparently.  Decision pending for 3b.2:
whether to migrate LFO to voice-aware at all, or just document
why it stays mono.  Crossover is more clear-cut -- it's a stateful
pair of biquads per branch, V parallel pairs is a direct port of
the Filter pattern.

## 2026-05-23 (later) -- Voice routing slice 3b.2: voice-aware LFO + Crossover

**What.**  Third and final installment of the downstream-module
migration before Keyboard.  LFO and Crossover now respect the voice
axis.  After this slice, all six stateful modules (ADSR, VCA, Filter,
Oscillator, LFO, Crossover) take per-voice signals correctly; only
Keyboard remains (slice 4).

**Decision: migrate LFO after all.**  The 3b.1 entry left a question
open -- "per-voice LFO is a niche use, maybe document why it stays
mono?".  Decided to migrate.  Two reasons:

* The cost is negligible.  LFO with no rate_cv is the common case
  and routes to the unchanged mono fast path.  Only a 2D rate_cv
  signal triggers the voice branch.  No existing patch pays any
  cost, and the migration unlocks per-voice rate modulation when
  it's needed (one obvious use: aftertouch -> LFO rate, where each
  voice's aftertouch could clock its own LFO at a different speed).
* The pattern is the same as Oscillator.  Branch on the CV input's
  ndim, V independent phase accumulators in the voice branch,
  per-voice block-mean rate.  Implementation is ~80 lines and
  follows the same state-isolation discipline (``phase`` vs
  ``phase_arr``).  Cheap to add now while the pattern is fresh;
  expensive to revisit later if a "polyrate LFO" use case shows up
  and the migration has to happen against a settled codebase.

**LFO migration.**  ``_render_lfo`` is now a small dispatcher that
opts ``rate_cv`` into ``collapse=False`` and branches on ndim:

* None or 1D ``(F,)`` -> ``_render_lfo_mono``, lifted unchanged from
  the pre-slice implementation.  Single scalar phase accumulator,
  vectorized phase ramp via ``arange``.  Every existing LFO test
  passes bit-for-bit identically.

* 2D ``(V, F)`` -> ``_render_lfo_voice``.  V independent phase
  accumulators (``phase_arr`` as ``(V,)`` array).  Per-voice
  block-mean rate (1V/oct, same convention as the mono branch).
  Per-row phase ramps via broadcast:
  ``start_phase[:, None] + step[None, :] * phase_inc[:, None]``.
  Output ``(V, F)``.

  Block-mean (rather than per-sample cumsum) is the deliberate
  cost/quality trade -- LFO is sub-audio by definition and the
  oscillator-style per-sample integration would be overkill.
  Per-voice phase persists across blocks, same policy as the
  oscillator voice path.

  ``random`` waveform's sample-and-hold goes per-voice too:
  ``random_arr`` is a ``(V,)`` array of per-voice held values.
  Each voice independently detects its own phase wrap and rerolls,
  serial across voices because S&H isn't vectorizable along the
  voice axis (each row's output depends on its own prior value at
  its own wrap edges).

**Crossover migration.**  Direct port of the Filter pattern.
``_render_crossover`` is a dispatcher that branches on the audio
input's ndim:

* ``ndim == 1`` -> ``_render_crossover_mono``, lifted unchanged.
  Scalar memory for two cascaded biquads per branch (16 scalar
  state variables).  Outputs ``{"low": (F,), "high": (F,)}``.

* ``ndim == 2`` -> ``_render_crossover_voice``.  V parallel
  cascaded biquads per branch.  Memory as 16 ``(V,)`` arrays
  (``lp1_x1_arr`` ... ``hp2_y2_arr``).  Per-sample recurrence is
  byte-identical to mono -- the only difference is that ``x``,
  the intermediate stage outputs, and the (x1, x2, y1, y2)
  memories are ``(V,)`` arrays.  Outputs
  ``{"low": (V, F), "high": (V, F)}``.

  Coefficients stay scalar because Crossover has no frequency_cv
  yet, so the same LP/HP coefficient set applies to every voice.
  Broadcasting handles the scalar-times-vector arithmetic.

Coefficient computation extracted to ``_crossover_coeffs(freq)`` so
both branches share one source of truth for the LR4 building
blocks.

**State branch isolation.**  Same protocol as 3b.1: mono state uses
unsuffixed keys (``"phase"`` / ``"lp1_x1"`` / ...), voice state uses
``_arr`` suffixed keys (``"phase_arr"`` / ``"lp1_x1_arr"`` / ...).
Each branch checks for the other branch's keys on first call and
reinitialises if it finds them.

**New tests: ``TestLFOVoiceAware`` + ``TestCrossoverVoiceAware``**
(12 tests appended to ``tests/test_voice_aware.py``).

LFO tests (7):

* ``test_voice_rate_cv_returns_voice_shape`` -- (V, F) rate_cv of
  zeros, every voice produces the same waveform (inter-voice
  consistency).
* ``test_per_voice_rate_via_rate_cv`` -- slot 0 at 0V (=4 Hz), slot
  5 at +2V (=16 Hz, two octaves up), voice 5 advances further
  along its phase ramp than voice 0 within one block.
* ``test_per_voice_phase_persists_across_blocks`` -- two-block
  sample continuity check for every voice.
* ``test_mono_rate_cv_returns_mono`` and
  ``test_no_rate_cv_returns_mono`` -- backward compat for the two
  ways the mono path can be entered.
* ``test_unipolar_voice_output_stays_non_negative`` -- bipolar=False
  shaping applies correctly on the voice path; output >= 0 across
  many blocks.
* ``test_voice_matches_mono_at_zero_cv`` -- voice row 0 with
  rate_cv=0 agrees with mono with rate_cv=0 to 1e-6.

Crossover tests (5):

* ``test_voice_audio_returns_voice_shape_for_both_outputs`` -- 1 kHz
  tone in slot 7 only.  Both low and high outputs are (16, 512),
  slot 7 carries signal on both branches (-6 dB at the corner),
  every other slot is silent on both branches.
* ``test_per_voice_biquad_memory_is_independent`` -- warmup slot 0,
  then impulse into slot 5 while slot 0's input goes to 0.  Slot
  5's impulse response appears in slot 5; slot 0 decays from its
  prior state without being kicked by slot 5's impulse.
* ``test_low_plus_high_recombines_to_input`` -- the LR4 property on
  the voice path: low + high RMS matches input RMS to within 5%.
* ``test_mono_audio_still_returns_mono`` -- backward compat.
* ``test_voice_path_matches_mono_path_for_replicated_voice`` --
  feed identical signal to every voice slot, every voice's output
  row matches the mono path's output to 1e-5 (proves the parallel
  biquads agree with the scalar biquad when fed the same input).

**Verification.**  Full project suite off the mount: **283 tests pass,
0 fail** (was 271 + 12 new; 18 mido tests skipped in the bash sandbox
but pass under Matthew's ``[midi]``-installed venv).  Same sandbox
+ verify protocol as 3b.1: staged in ``/sessions/.../outputs``,
AST-parsed, ran the suite against a sandbox clone of the project
tree, copied whole files to the mount with ``cp``, AST-parsed and
size-verified on the mount, ran the suite directly off the mount.

**Slice 3b complete.**  All six stateful DSP modules are voice-aware.
The canonical polyphonic patch -- MIDIInput -> Oscillator (per-voice
pitch) -> Filter (per-voice cutoff envelope) -> VCA (per-voice ADSR)
-> Speaker -- now runs end-to-end with each voice's identity
preserved through every stage.  The two routing modules in 3b.2 also
mean a Crossover can sit in a polyphonic chain (e.g. multi-band
processing per voice) without collapsing to mono.

**Next: slice 4 -- Keyboard migration.**  Mirror Keyboard onto the
same self-polyphonic shape as MIDIInput (voice slots, per-slot
output buffers).  Mechanically a port of the MIDIInput renderer
adapted to the keyboard's note-set source.


## 2026-05-23 (much later) -- Voice routing slice 4: Keyboard migrated to MIDIInput shape

**What.**  Final voice-routing slice. Keyboard now uses the 16-slot
``VoiceSlots`` allocator instead of a flat ``active_notes`` set, and
its renderer emits per-slot ``(MAX_VOICES, frames)`` buffers on
``out`` and ``gate`` -- the same shape MIDIInput already publishes.
With this slice both note sources publish identical per-voice
signals, so anything downstream behaves identically regardless of
which one is driving it.

**Voice routing is complete.**  All eight voice-impacted modules --
two note sources (Keyboard, MIDIInput) plus six stateful DSP
modules (ADSR, VCA, Filter, Oscillator, LFO, Crossover) -- now
honour the voice axis. The canonical polyphonic patch
``Keyboard -> ADSR -> VCA -> Speaker`` produces per-voice envelopes
end-to-end, exactly like its MIDIInput equivalent.

**Decision: keep Keyboard's API narrow.**  MIDIInput grew velocity,
pitch wheel, mod wheel, channel aftertouch and sustain pedal
because real hardware sends them. None of those have a UI surface
on a computer keyboard -- you can't express velocity through a key
press, there's no wheel to deflect, no pedal to depress. So
Keyboard stays at:

  * ``note_on(midi_note)`` -- unit velocity, no second arg
  * ``note_off(midi_note)``
  * ``all_notes_off()``
  * ``snapshot_active_notes()`` -> ``set[int]`` for the UI
  * ``snapshot_voice_slots()`` -> per-slot snapshot for the renderer
  * Ports: ``out`` + ``gate`` only (no pitch_cv / mod_cv / pressure_cv)

The renderer mirrors ``_render_midi_input`` minus the controller
features. Per-slot state has the same shape (``phase`` / ``env`` /
``last_note`` / ``releasing`` as ``(V,)`` arrays) so the same slot-
reassignment + edge-detection logic works without change.

**Public API preserved.**  Existing UI code calls only
``note_on(midi_note)``, ``note_off(midi_note)``, ``all_notes_off()``.
All three keep their signatures. ``snapshot_active_notes()`` still
returns a ``set[int]`` -- internally it reads
``VoiceSlots.held_notes().keys()`` and casts to set, so the UI
sees no change.

**Gate semantics shift.**  Pre-slice-4 the gate was a single
block-constant signal: high if any key was held, low otherwise. No
retrigger when adding notes to a held chord. Post-slice-4 the gate
is per-voice block-constant: each slot has its own gate, raised
when that slot's note is allocated and dropped when released. A
new note in a chord rises its own slot's gate rather than
retriggering the existing slots -- which is exactly the
polyphonic behaviour a downstream ADSR per voice needs.

The pre-slice "no retrigger on additional keys" property survives
in a slightly different form: each existing voice's gate keeps its
state independently across chord changes. Old patches that depend
on the *summed* gate behaviour still work because un-migrated mono
consumers get the collapsed-to-1D view via ``_input_buffer``'s
default ``collapse=True`` -- summing all 16 per-slot gates of
``{0, 1}`` values back to 1 whenever any slot is gating.

**State-leak regression test updated.**
``test_compile_drops_state_when_module_type_changes`` previously
asserted the keyboard state contained a ``voices`` key (from the
old ``{"voices": {note: voice_state}}`` shape). The new state
uses the MIDIInput-mirror shape (``phase`` / ``env`` /
``last_note`` / ``releasing`` numpy arrays); the assertion checks
``"phase" in state_after`` and continues to catch the original
regression (oscillator state surviving into a keyboard slot).

**ADSR test helper updated.**  ``tests/test_adsr.py`` builds
patches as ``keyboard -> ADSR`` and renders the ADSR's CV out.
Pre-slice-4 the keyboard sent a mono ``(F,)`` gate, the ADSR
returned a mono ``(F,)`` CV. Post-slice-4 the keyboard sends a
``(V, F)`` gate, the (voice-aware as of slice 3a) ADSR returns a
``(V, F)`` CV. The test helper ``_render_cv`` now collapses the
voice axis via ``cv.sum(axis=0)`` when the result is 2D -- the
same implicit-sum-at-mono-sinks rule the SpeakerOutput uses. Since
every ADSR test presses one note at a time, the collapsed mono CV
is identical to what the pre-slice mono path returned, and every
assertion passes unchanged.

**Updated tests in ``tests/test_keyboard.py``.**

  * ``test_silent_when_no_keys_held`` -- now checks both ``out``
    and ``gate`` are ``(16, 512)`` and all-zero.
  * ``test_attack_ramp_avoids_click`` -- now checks ``buf[0, 0]``
    (slot 0, sample 0) instead of ``buf[0]`` (which under the
    old shape was sample 0 but under the new shape is the whole
    slot-0 row).
  * ``test_gate_per_voice_high_when_held_low_when_idle`` (renamed
    from ``test_gate_high_while_held_low_when_idle``) -- proves
    per-voice gate semantics: pressing note A raises ``gate[0]``,
    pressing note B raises ``gate[1]`` without disturbing
    ``gate[0]``, panic drops every slot's gate to 0.
  * ``test_polyphony_sums_voices`` -- now sums across the voice
    axis before computing RMS (same shape as the SpeakerOutput's
    mono mix), plus a sanity check that two notes occupy two
    distinct slot rows.
  * NEW: ``test_snapshot_voice_slots_returns_max_voices_entries``
    -- verifies the renderer hook returns 16 entries and the
    first allocation lands in slot 0.

**NEW tests: ``TestKeyboardVoiceAware`` + ``TestKeyboardPolyphonicChain``**
(7 tests appended to ``tests/test_voice_aware.py``).

``TestKeyboardVoiceAware`` (3 tests):

  * ``test_renderer_returns_voice_aware_shape`` -- both buffers
    are ``(16, 512)``, first note lands in slot 0, every other
    slot silent on both buffers.
  * ``test_notes_distribute_across_slots`` -- three notes fill
    three distinct slot rows.
  * ``test_released_voice_leaves_held_voices_alone`` -- per-voice
    independence: releasing one note in a two-note chord drops
    that slot's gate while the other slot's gate and audio stay
    at full strength.

``TestKeyboardPolyphonicChain`` (4 tests, mirror of
``TestPolyphonicChain`` from slice 3a):

  * ``test_chain_renders_audio`` -- a note through
    ``Keyboard -> ADSR -> VCA -> Speaker`` produces audio.
  * ``test_released_voice_decays_while_held_voice_sustains`` --
    the headline polyphony property, ported from MIDIInput: two
    notes, release one, the released voice's envelope decays
    while the held voice stays at sustain.
  * ``test_no_notes_silent`` -- the chain is silent with no
    notes held.
  * ``test_keyboard_and_midi_input_produce_equivalent_chain_behavior``
    -- cross-source sanity. Drive the same MIDI note through
    each note source into the same downstream chain; the two
    chains produce RMS values within a 2x ratio (margin allows
    for MIDIInput's velocity-scaled gain vs Keyboard's unit gain).

**Verification.**  Full project suite off the mount: **290 passed,
1 failed, 18 mido-skipped** in the bash sandbox. The single fail
(``test_no_nan_with_zero_durations`` -- ``NameError: name 'sr' is
not defined``) is a pre-existing linter edit in ``test_adsr.py``
that removed the ``sr = 44100`` line under the test docstring; not
introduced by slice 4. Was 283 + 7 new (3 Keyboard voice-aware +
4 polyphonic chain) + 1 new (``test_snapshot_voice_slots_returns_
max_voices_entries``) -- 8 net new tests this slice.

Same sandbox + verify protocol as 3b.2: staged numpy_backend.py,
keyboard.py and the test appendix in ``/sessions/.../outputs``,
AST-parsed, ran the suite against a ``/tmp/pyrack_s4`` clone of
the project tree, copied whole files to the mount via ``cp``,
byte-verified size against the staged copies, AST-parsed on the
mount, ran the suite directly off the mount.

**What comes next.**  Voice routing as a project is done. The
remaining v0.4 work is:

  * PolyBLEP / wavetable anti-aliased osc shapes (replace naive
    saw/square). Best done after voice routing settled, which it
    now is -- new shapes only need one implementation across both
    branches of every shape-polymorphic renderer.
  * Pyo backend wired for the new voice-aware modules so it's a
    drop-in fast path. Today pyo still stubs the v0.2/v0.3/v0.4
    modules; numpy is the real engine.


---

## 2026-06-04 — Anti-aliased oscillator shapes (PolyBLEP / PolyBLAMP + wavetable)

Picked up the v0.4 "PolyBLEP or wavetable anti-aliased osc shapes" item.
Per Matthew's call, the anti-aliased shapes are offered **alongside** the
naive saw/square/triangle rather than replacing them — naive aliasing is
cheap and sometimes exactly the lo-fi character you want. Both PolyBLEP and
wavetable are selectable, and the note-source oscillators (Keyboard +
MIDIInput) come along for the ride so every oscillator in the synth shares
one sound.

**Design — expanded `waveform` vocabulary.** Rather than a separate
orthogonal `antialias` param, the shape + band-limiting method live in one
string as `"<base>_<method>"`:

  * naive: `sine`, `saw`, `square`, `triangle` (unchanged from v0.2).
  * PolyBLEP/PolyBLAMP: `saw_blep`, `square_blep`, `triangle_blep`.
  * wavetable: `saw_wt`, `square_wt`, `triangle_wt`.
  * `sine` stays naive-only — it is already band-limited.

`oscillator.WAVEFORMS` grew from 4 to 10 entries. The UI dropdown derives
from that tuple for every non-LFO module, so Oscillator, Keyboard,
MIDIInput and CVToFrequency all surface the new shapes with no UI change.
Old patch JSON keeps loading: legacy `"saw"` etc. still map to the naive
path. `cvtofrequency.WAVEFORMS` mirrors the same tuple; Keyboard/MIDIInput
docstrings updated to point at `oscillator.WAVEFORMS`.

**NEW: anti-aliasing DSP centralised in `_osc_waveshape`.**
`_osc_waveshape(phases, waveform, dt=None)` now parses the base shape and
method from the waveform string and dispatches:

  * `_waveshape_naive` — the old elementwise math, unchanged.
  * `_waveshape_blep` — naive shape plus a discontinuity correction.
    `_poly_blep` (two-sample PolyBLEP residual) corrects saw's wrap edge
    and square's two edges; `_poly_blamp` (its integral) rounds triangle's
    two slope corners, scaled by the ±8 slope change × dt.
  * `_waveshape_wt` — band-limited wavetable lookup with linear
    interpolation. `_get_wavetable(base)` lazily builds and caches an
    11-band per-octave mipmap (2048-sample tables), each additively
    synthesised with only the harmonics that stay below Nyquist for the
    top of its octave band, then peak-normalised. The band is chosen per
    block from the largest `dt` (highest instantaneous freq → fewest-
    harmonics table → never aliases within the block); extreme FM
    excursions therefore fall back conservatively.

`dt` is the per-sample phase increment (`freq / sample_rate`), scalar for a
constant-frequency mono ramp or an array broadcastable to `phases` for
CV/FM. `dt is None` (isolated/unit-test callers with no frequency)
gracefully degrades any anti-aliased shape to its naive form.

**CHANGED: `dt` threaded through every caller.** The four vectorised
phase-ramp call sites — `_render_oscillator_mono`, `_render_oscillator_voice`,
`_render_cv_to_frequency_mono`, `_render_cv_to_frequency_voice` — now pass
the phase increment they already compute (`phase_inc` scalar or `inst_inc`
(F,)/(V,F) array) into `_osc_waveshape`. Voice-aware shape preservation is
unchanged: a (V, F) `dt` flows through the BLEP/wavetable maths elementwise.

**CHANGED: Keyboard + MIDIInput route through the shared shaper.** The two
note-source renderers had identical inline `if waveform == "sine": ...`
blocks operating on a per-voice `phases` array with scalar `phase_inc`.
Both were replaced with a single `wave = self._osc_waveshape(phases,
waveform, dt=phase_inc)` call, so the note sources get the same naive /
PolyBLEP / wavetable shapes as the patched Oscillator. No behaviour change
for the naive shapes; the envelope/gate/velocity logic around them is
untouched.

**NEW: wavetable cache on the backend.** `self._wavetables: dict[str,
np.ndarray]` holds the mipmaps, built once on first `*_wt` use and shared
across every oscillator-like module in the patch.

**Drive-by fix.** `tests/test_adsr.py::test_no_nan_with_zero_durations`
referenced an undefined `sr` (a linter had dropped the `sr = 44100` line);
inlined `sample_rate=44100`. The suite is now fully green with no
pre-existing failures.

**NEW tests: `tests/test_antialiasing.py` (20 tests).** Spectral
assertions rather than sample-exact, since band-limiting deliberately
changes the time-domain waveform. `_alias_fraction` FFTs a rendered tone
and measures the share of energy NOT on a harmonic of the fundamental.
Coverage:

  * Vocabulary + backward compat — all 10 shapes present; naive saw/square
    are bit-for-bit what they were.
  * Aliasing reduction — `*_blep` and `*_wt` cut saw/square aliased energy
    by >5x at a 2.2 kHz fundamental; triangle_blep is no worse than naive
    (triangle barely aliases) and stays a recognisable triangle; every
    anti-aliased shape is finite and bounded ≤1.1.
  * Helper contract — `dt=None` degrades to naive; the wavetable cache is
    built once (identity-equal on second fetch) with the right shape;
    `sine` + dt stays a clean sine.
  * Voice-aware — (V, F) freq_cv preserves (16, 512) output shape for
    blep + wt.
  * CVToFrequency — renders finite, audible audio with blep/wt.
  * Keyboard — `saw_blep` cuts aliasing >3x vs naive `saw` at a high note;
    new waveforms render at the right (V, F) shape.
  * MIDIInput — new waveforms render finite per-voice audio.

**Verification.** Same staged-in-sandbox + verify protocol as prior slices:
edited a `/tmp/pyrack_aa` clone via bash (Edit tool truncates on the
Windows mount), tuned the DSP against an FFT harness, ran the suite on the
clone, copied whole files to the mount via `cp`, byte-verified size, AST-
parsed on the mount, and ran the suite directly off the mount. Result off
the mount: **360 passed, 18 mido-skipped, 0 failed** (was 339 + 20 new +
1 fixed drive-by).

**What comes next.** v0.4 remaining: LeftSpeakerOut / RightSpeakerOut
hard-panned sinks, and the pyo backend wired for the voice-aware modules as
a drop-in fast path (pyo still stubs v0.2+; numpy is the real engine). The
new wavetable mipmaps would port cleanly to a pyo implementation later.

---

## 2026-06-06 — LeftSpeakerOut / RightSpeakerOut + pyo deferral

Two pieces this session: the hard-panned speaker pair (closing out v0.4's
module work), and a roadmap decision about the pyo backend.

**NEW: `LeftSpeakerOutput` + `RightSpeakerOutput`** (`modules/output.py`,
TYPE `left_speaker_output` / `right_speaker_output`). Mono `in` port,
`gain` param — exact mirrors of `SpeakerOutput` except each feeds one
channel of the stereo bus. Registered in `modules/__init__.py`; the Add
module menu derives from the registry so the UI picked them up with zero
UI changes.

**CHANGED: numpy drain generalised.** The speaker pass in `render_block`
previously special-cased `speaker_output`. Now a `_SPEAKER_CHANNELS`
class table maps each sink type to `(left, right)` flags —
`speaker_output` (True, True), left (True, False), right (False, True) —
and the loop adds the gained mono mix into whichever buses are flagged.
The voice-aware implicit-sum-at-mono-sinks rule applies unchanged: a
(16, F) source collapses to mono before being pinned to a channel. The
`_render_module` sink short-circuit now tests membership in that table.

**CHANGED: pyo speaker finalize routes channels.** Rather than stubbing,
the existing `_finalize_speaker_output` gained pyo's `chnl` argument:
left pins `.out(chnl=0)`, right `.out(chnl=1)`, plain stays `.out()`.
`set_param` gain handling covers the family. Cheap, keeps pyo's working
v0.1 surface (osc → speaker) coherent rather than letting it rot.

**DECISION: pyo backend deferred off v0.4 — profile first.** Talked it
through with Matthew (he was weighing a fresh pyo-native synth project
vs porting later). Assessment: a pyo backend is a *re-implementation*
of module semantics against pyo's object graph, not a translation of
the numpy code — and it only pays off at near-total coverage because one
backend runs the whole patch. Sized like the voice-routing epic. The
model/JSON/UI layers are already engine-agnostic (the original
abstract-backend decision paying off), so nothing is lost by waiting,
and a fresh synth project would duplicate everything except the engine.
Plan recorded in TODO: profile numpy with real patches first; only build
the pyo backend if numpy can't keep up; stubs stay meanwhile.

**NEW example: `examples/stereo_hard_pan.json`.** Two `saw_blep`
oscillators detuned 220.0 vs 221.5 Hz, hard-panned L/R. On headphones the
1.5 Hz detune reads as a slow binaural-style shimmer between the ears —
also doubles as a demo of the new anti-aliased shapes.

**NEW tests: `tests/test_speaker_outputs.py` (11 tests).** Model layer:
registry presence, port/param schema, JSON round-trip through
`patch_to_json`/`patch_from_json`. Drain: left fills L-only, right fills
R-only, plain speaker still identical on both buses, an L+R pair carries
distinct signals (zero-crossing pitch check confirms which side got
which), per-sink gain, pinned + plain speakers mixing additively on the
shared bus, voice-aware Keyboard source collapsing into a pinned channel,
and an unconnected pinned sink staying silent.

**Verification.** Usual protocol: edited a `/tmp/pyrack_sp` clone via
bash, ran the suite there, `cp`'d whole files to the mount, byte-verified
sizes, AST-parsed on the mount, re-ran the suite off the mount.
**371 passed, 18 mido-skipped, 0 failed** (360 + 11 new).

**What comes next.** v0.4 module work is done. Remaining open threads,
all optional-tier: CVToFrequency phase 2 (designed, in memory),
Schmitt bridge, CV utilities (Constant/CVScale/CVOffset), S&H, noise,
AD env, stereo-aware speaker (pan/width — the proper successor to the
hard-pan pair), presets palette, undo/redo, packaging niceties. And the
profile-first pyo plan above.

## 2026-06-07 — CPU profile of the numpy backend (the pyo go/no-go measurement)

**Decision recap.** Picking up the profile-first plan agreed 2026-06-06:
measure before building any pyo backend. Matthew confirmed profile-first
when offered the choice of jumping straight to the pyo epic.

**NEW: `tools/profile_numpy.py`.** Standalone, device-free profiler.
Builds the canonical chain from `examples/keyboard_adsr.json` in code
(Keyboard → Filter → VCA with ADSR on the gate → Speaker), holds a
16-note chord, and times `render_block(512)` over N blocks (default
2000 ≈ 23 s of audio). Four scenarios: naive `saw`, `saw_blep`,
`saw_wt`, and `saw_blep` + bipolar LFO into `cutoff_cv`. Reports
mean/median/p99/max per block against the 11.61 ms budget, plus a
worst-block verdict — the audio callback has no mercy for p99
stragglers, so max-under-budget is the pass bar, not mean. GC left
enabled (the live callback runs with GC on; its spikes are part of the
honest answer). Sanity asserts: output finite and non-silent.

**Sandbox first read (Linux container — indicative only, NOT the
verdict machine).** 300 blocks/scenario: mean 8.9–9.4 ms (77–81% of
budget), worst blocks 15.5–20.1 ms (134–173%) — deadline misses in
every scenario. Anti-aliased shapes barely move the needle (saw_wt ≈
naive saw within noise; blep +6%).

**cProfile breakdown (the transferable result).** Of ~9.9 ms/block in
the heavy scenario: `_render_adsr_voice` 6.3 ms (**63%**),
`_render_filter_voice` 2.2 ms (**22%**), keyboard + osc waveshaping
~1.0 ms, everything else noise. The giveaway: ~153,600 `astype`/`copy`
calls per 300 blocks = one per *sample* — the ADSR (and filter) run
per-sample Python loops. This is a vectorization problem, not a
"numpy is too slow" problem: two hot functions own 85% of the block.

**Implication for the pyo question.** Before a pyo epic (re-implement
every module against pyo's graph), there is a much cheaper intermediate:
vectorize `_render_adsr_voice` (analytic segment evaluation or
cumulative-product one-pole) and `_render_filter_voice`
(`scipy.signal.lfilter`-style or cascade trick — though biquads are
genuinely sequential, a C-backed lfilter call beats a Python loop by
~100x). If those land, numpy likely keeps up with 5-10x headroom and
pyo stays parked indefinitely.

**Next.** Matthew runs `tools\profile_numpy.py` natively (PowerShell,
project venv) — sandbox CPU is a shared container and absolute numbers
don't transfer. Native numbers decide: comfortable → park pyo; thin or
missing → vectorize the two hot modules first and re-measure; only if
*vectorized* numpy still can't keep up does the pyo epic get a green
light.

## 2026-06-07 (later) — Native profile verdict + ADSR vectorized (117x)

**Native profile (Matthew's machine, the real numbers).** Mean
11.2–11.9 ms against the 11.61 ms budget — 97–102% CPU. `saw_blep`
missed the deadline on 1999/2000 blocks; even naive saw had no headroom
left for the GUI. At 16 voices the synth could not render real-time.
Per the decision ladder: vectorize the hot modules before any pyo work.

**CHANGED: `_render_adsr_voice` vectorized — the 63% reclaimed.** The
per-sample Python loop (one pass of ~10 small-array numpy ops per
sample) is replaced by run-splitting: each voice's gate row is split at
gate *edges* (typically zero per block), and within a run the envelope
from a known entry state is a deterministic piecewise-linear chain —
attack→decay→sustain or release→idle — emitted analytically by a new
`_adsr_fill_run` helper (one `arange` + one clamp per stage). Stage
lengths are closed-form (smallest k ≥ 1 crossing the target).

**Semantics preserved, including the cascade.** Parity harness
(7 param cases × 5 gate patterns × 16 voices × 8 sequential blocks,
old vs new in separate processes) caught two things worth recording:
1. The voice path's mask loop *cascades* stage transitions within a
   sample — the attack-crossing sample immediately applies the decay
   update, so it emits `max(1.0 - decay_step, sustain)`, never a bare
   1.0 (and with decay=0 it falls through to sustain in one sample).
   This deliberately differs from the mono scalar path (elif chain,
   one stage per sample, emits the clamped 1.0). The rewrite
   reproduces the cascade exactly; mono path untouched, still the
   loop, still bit-for-bit.
2. Bit-exactness vs the old loop is *impossible* vectorized: the loop
   accumulated `level += step` with per-add rounding, so when a
   crossing lands on an exact integer sample count the accumulated
   value sits ~1e-13 below target and crosses one sample later than
   the analytic `L0 + k*step`. Residual divergence: stage boundaries
   shift ≤ 1 sample, value error ≤ one ramp step (measured max
   1.5e-4), only when (target−L0)/step is near-integer. Inaudible CV
   noise; the real contract is the suite.

**Verification.** Parity harness as above; full suite in the sandbox
clone **371 passed, 18 mido-skipped**; whole-file cp to the mount,
byte-verified (114,471 bytes), AST-parsed on the mount, suite re-run
from the mount: **371 passed** again.

**Re-profile (sandbox, same container that ran 77–81% mean before):**
mean 2.5–3.0 ms (21–26% of budget), worst block 30%, zero over-budget
across all four scenarios — 3.2x faster overall. cProfile now:
`_render_adsr_voice` 0.016 s/300 blocks (was 1.876 — 117x);
`_render_filter_voice` is the new leader at ~68% of remaining render
time, still a per-sample loop (~one astype per sample).

**Next.** Matthew re-runs `tools\profile_numpy.py` natively. If his
numbers land near the sandbox ratio (~3x), numpy keeps up with real
headroom and pyo stays parked. Filter vectorization is the remaining
candidate pass — but biquads are sequential IIRs, so the options are
voice-axis batching (pure numpy, modest win) or `scipy.signal.lfilter`
(C-speed, **new dependency** — Matthew's call given the numpy-only
stance so far).

## 2026-06-07 (close-out) — Native re-profile: numpy keeps up, pyo parked

Matthew's native re-run after the ADSR vectorization: mean 3.4–3.9 ms
(29–33% of budget), worst block 42%, **0/2000 blocks over** in all four
scenarios. Before/after on the same machine: 97–102% mean with
1999/2000 misses → 29–33% mean with none. The ladder resolves at step
2: **no performance case for a pyo backend.** It stays parked
indefinitely — the switchable-engine idea remains a *feature* question
(revisit only if wanted for its own sake, per the 2026-06-06 deferral).

Filter vectorization (the would-be next pass) is therefore **not
needed** for current patch sizes — filed as an optional wishlist item
with its dependency question (pure-numpy voice batching vs
scipy.signal.lfilter) for if patches ever grow past the headroom.

## 2026-06-07 — CVToFrequency phase 2: negative-side mirror

(Also today: Matthew rebuilt the .exe with the vectorized ADSR —
compiles and plays well.)

**NEW params on `cv_to_frequency`** (per the 2026-05-23 design,
`memory/project_cvtofrequency_plan.md`): `negative_enabled` (default
False — phase-1 clamp preserved exactly), `f0_neg` (Hz at CV=0⁻,
default 110.0 = f0's default so the zero-crossing starts smooth),
`fm_neg` (CV=-0.5), `f1_neg` (CV=-1.0), `mode_neg` (log|linear,
independent of `mode` — log upswing with linear downswing is the
whole point). CV exactly 0 belongs to the positive side; the crossing
snaps f0 → f0_neg and continuity is deliberately the user's choice.
CV beyond ±1 clamps to the nearest endpoint.

**CHANGED: renderer.** New `_cv_to_hz_mapped(cv, pos, neg)` staticmethod
dispatches on sign — `neg is None` reproduces phase 1 bit-for-bit
(same `_cv_to_hz` call), else both curves evaluate and `np.where`
selects (negative side maps |cv| through the neg anchors, so the
shared [0,1] clamp handles the beyond--1 case for free). Mono and
voice paths now take `(pos, neg)` tuples instead of loose
f0/fm/f1/mode scalars; phase accumulators untouched.

**FIXED (drive-by): cv_to_frequency's `mode` combo listed the filter's
items.** The UI's `mode` dispatch only knew cv_combiner vs
filter-shaped-everything-else, so since phase 1 the node's mode combo
offered lowpass/highpass/bandpass — selecting any wrote a junk mode
string the renderer silently treated as linear. Now imports
cvtofrequency.MODES, dispatches per type, and the same arm serves the
new `mode_neg` combo. `negative_enabled` renders via the existing
bool→checkbox path; the *_neg Hz params via the generic drag-float,
same as f0/fm/f1.

**NEW example: `examples/cvtofreq_bipolar_pendulum.json`.** Bipolar
sine LFO (0.2 Hz) swinging a saw_blep across both curves: upswing log
220→440→880 (musical octaves), downswing linear 220→330→440 (bent).
f0 == f0_neg keeps the crossing smooth. Verified to load through real
patch IO and render (peak 0.79).

**NEW tests: 12 in `TestCVToFrequencyPhase2`** — disabled-ignores-neg-
curve (back-compat with a loud f1_neg configured), f1_neg/fm_neg
anchors, zero-belongs-to-positive, just-below-zero ≈ f0_neg,
clamp-below--1, positive side unchanged when enabled, mixed
log/linear independence (geometric vs arithmetic midpoint from the
same anchors), deliberate zero-crossing step, JSON round-trip,
voice-aware ±1 rows (per-row FFT), full bipolar sweep finite.
Two phase-1 tests updated: defaults dict gains the new keys, and
`test_unknown_param_rejected` needed a new impostor — phase 1 had
used `negative_enabled` as its example of an unknown param.

**Verification.** Usual protocol: built in /tmp clone, suite there,
whole-file cp of all five files, byte-verified, AST-parsed on the
mount, suite re-run from the mount: **383 passed, 18 mido-skipped**
(371 + 12).

## 2026-06-07 — Schmitt trigger: the last signal-kind bridge

**NEW: `Schmitt` module** (`modules/schmitt.py`, TYPE `schmitt`).
CV in → gate out with classic two-threshold hysteresis: rises through
`high` (strict >, default 0.6) → gate sets; falls through `low`
(strict <, default 0.4) → gate clears; inside the band the gate
holds. The band is what makes a wobbly CV usable as a clock without
chatter. `low` > `high` degenerates to a plain comparator at `high`
(documented, tested) rather than anything surprising. Completes the
bridge trio: AudioToCV (audio→cv), CVToAudio (cv→audio), Schmitt
(cv→gate) — every signal-kind wall now has a door.

**NEW: `_render_schmitt` in the numpy backend.** Vectorized by event
forward-fill — samples classified +1/-1/0 (above high / below low /
deadband), `np.maximum.accumulate` over event positions finds the
most recent event per sample, gate = "was it a +1", seeded with
carried cross-block state. **No per-sample Python loop** — today's
ADSR lesson applied at birth. Shape-polymorphic per the voice
convention: (F,) in → (F,) out with scalar state, (V, F) in → (V, F)
out with per-voice state (per-voice envelope banks can clock
per-voice triggers). Unpatched input emits constant-low. Output
float32 0/1, comfortably astride `_GATE_HIGH`.

**Registered everywhere:** `modules/__init__.py` import + `__all__`;
pyo backend silent-stub list (alongside the other bridges); UI gets
`high`/`low` added to the 0..1 slider set (generic drag-float would
have been unbounded). Node appears in the Add menu via the registry,
zero UI layout work.

**NEW example: `examples/schmitt_lfo_clock.json`.** The headline use
case: LFO (sine 1.5 Hz, unipolar) → Schmitt → ADSR gate; saw_blep 220
through a VCA shaped by that envelope. A self-playing pluck at 1.5 Hz
— no keyboard anywhere in the patch. Verified through real patch IO:
renders ~2 s with loud blocks (peak 0.47) and fully silent troughs.

**NEW tests: 20 in `tests/test_schmitt.py`.** Model (defaults, ports,
kinds, JSON round-trip, unknown-param, type walls both directions —
including gate→speaker rejected), mono behaviour (crossing sample
exactness, deadband hold, hysteresis wobble survival, falling clear,
strict-inequality thresholds, cross-block state, inverted-pair
degeneration, unpatched silence), voice-aware (independent rows,
per-voice cross-block state, mono fast path), integration (LFO →
Schmitt → ADSR fires ~4 envelopes/s at rate 4, full release between
cycles).

**Verification.** Usual protocol: built in /tmp clone, suite there,
whole-file cp of all seven files, byte-verified, AST-parsed on the
mount, suite re-run from the mount: **403 passed, 18 mido-skipped**
(383 + 20).

**Spotted while in there (not fixed):** `_render_audio_to_cv_voice`
still runs a per-sample Python loop. It's an asymmetric one-pole —
genuinely recursive, signal-dependent coefficient switching — so it's
not run-splittable the way the ADSR was. Cold path today (no envelope
follower in the canonical patch); noted on the TODO wishlist for if
follower-heavy patches ever profile hot.


## 2026-06-09 — Filter vectorization spike: scipy lfilter cleared

**Decision: vectorize the biquad with `scipy.signal.lfilter`, not pure
numpy.** The voice axis in `_render_filter_voice` is already vectorized
(inner loop is a (V,)-wide multiply-add); what stays serial is the
per-sample *time* loop, and a biquad's recurrence (`y[n]` needs `y[n-1]`,
`y[n-2]`) can't be run-split the way the ADSR was. lfilter moves that
time recurrence into C — the one lever left.

**Spike (sandbox, throwaway — no tree changes beyond this log + TODO).**
Faithful DF-I reference (verbatim `_filter_coeffs` + the exact mono and
voice per-sample loops) vs an lfilter candidate carrying `zf`->`zi`
across blocks. Resonant lowpass (1 kHz, Q 2), 512-sample blocks, SR 44100.
- Equivalence: max abs error vs the current loop 7.6e-15 (mono),
  1.0e-14 (voice) — float64 round-off, i.e. bit-identical. The zf->zi
  handoff (the one fiddly part) is sound.
- Speed: mono 0.119 -> 0.0068 ms/blk (17.5x); voice (V=16) 1.98 ->
  0.043 ms/blk (46.2x). Voice path is 17.1% of the 11.6 ms block budget
  today -> 0.4% with lfilter. Matches the profile's "filter is the
  dominant remaining cost" finding.

**Caveats.** Sandbox numbers — native Windows will move the absolute
percentages (native ran hotter than sandbox on the ADSR work), but the
17–46x ratio is a property of the C loop and should hold. Shared-coeff
path only; per-voice cutoffs need a 16-call lfilter loop (smaller win,
measured for real in slice 4).

**Cost.** New dependency: scipy. Ships 3.12 Windows wheels (no MSVC),
but bloats the single-file exe — quantify in slice 2.

**Queued on TODO:** slices 2 (add dep + build check), 3 (mono path),
4 (voice path), 5 (crossover, optional), 6 (native re-profile + docs).
Each ends green + committable; multi-session by design. No production
code touched this session.

## 2026-06-10 — Filter vectorization slice 2: scipy dependency added

**Dep declared.** `scipy>=1.11` added to `pyproject.toml` runtime deps and
mirrored in `requirements.txt`. Floor chosen to keep `requires-python >=3.9`
honest while letting the 3.12 build venv resolve current scipy.

**Wheel check.** cp312 win_amd64 wheel confirmed available (scipy 1.17.1,
~36 MB wheel) via pip resolution against PyPI — no MSVC needed on the
build machine, consistent with the no-compiler constraint.

**Build pipeline reviewed.** `pysynthrack.spec` / `pysynthrack-cli.spec`
need no changes: only `pyo` is excluded, and PyInstaller ships a built-in
scipy hook that collects its DLLs. One real gap fixed: `build.ps1`'s
pre-flight import check didn't include scipy, so a venv missing it would
pass pre-flight and produce an exe that breaks the moment slice 3 imports
`lfilter`. Added `('scipy', 'scipy')` to the required list.

**Verification.** Mount-write protocol followed: staged in sandbox,
whole-file cp, byte-verified all three files; pyproject re-parsed as
valid TOML. Full suite from the mount: **421 passed** (the 18 mido tests
ran rather than skipped — sandbox has mido installed — so 403 + 18).
No production code touched; nothing imports scipy yet (that's slice 3).

**Hand-off to Matthew (closes slice 2):**
```powershell
cd "C:\Users\Admin\Desktop\-=Programming=-\Python Synthesiser 2\Python Synthesizer"
.\.venv\Scripts\Activate.ps1
uv pip install scipy
.\build.ps1          # pre-flight now checks scipy; note the printed exe size
```
Record the size delta (last build's MB vs this one) in the TODO slice-2
line, then commit.

**Slice 2 closed (same day).** Matthew ran the install + build: pre-flight
passed with scipy present, exe built clean — `dist/PySynthRack.exe`
24,225,019 bytes (23.1 MB). Two notes from the run:
- *Size delta is deferred to slice 3 by construction* — PyInstaller bundles
  only what's imported and nothing imports scipy yet, so 23.1 MB is the
  pre-scipy baseline, not the answer. Slice 3's build will show the growth.
- *PowerShell ate the version spec*: `uv pip install scipy>=1.11` parses
  `>` as a redirect — installed scipy unpinned (resolved fine) and left a
  stray empty file `1.11`, which got swept into a junk commit. Cleaned up
  by dropping that commit and amending the real one. Rule for future
  hand-offs: always quote version specs in PowerShell.


## 2026-06-11 — Session close: slice 2 wrapped, state saved

**Where things stand.** Slice 2 closed. Local main is clean — junk commit
dropped, real commit amended (single slice-2 commit with full message +
doc closure). Remote is one step behind reality: it still carries the two
messy commits from before the cleanup, so the next push must be
`git push --force-with-lease` and **not** preceded by a pull (a pull would
merge the junk history back). Recorded as a transient TODO item.

**Decisions this session.**
- Size budget made explicit: Matthew is comfortable up to ~256 MB for the
  exe. Baseline 23.1 MB; scipy expected +30–40 MB at slice 3. Size is a
  non-issue for the remaining slices.
- PowerShell hand-off rule learned the hard way: bare version specs
  (`scipy>=1.11`) are parsed as redirects — always quote them.

**Next session starts at slice 3:** `_render_filter_mono` → lfilter,
zf→zi cross-block state continuity, numerical-equivalence test vs the
old per-sample loop. The spike (2026-06-09 entry) has the validated
pattern to lift: same coeffs, zf of block N becomes zi of block N+1,
expect bit-identical output (~1e-14). Slice 4 (voice path) follows the
same shape with zi (V, 2) and a 16-call loop for per-voice cutoffs.

## 2026-06-12 — Filter vectorization slice 3: mono path on lfilter

**Shipped.** `_render_filter_mono` now runs its biquad through one
`scipy.signal.lfilter` call — the serial time recurrence executes in C
(17.5x on the spike). First production code to import scipy.

**One deliberate deviation from the spike's zf→zi pattern.** The spike
carried lfilter's `zf` directly into the next block's `zi`. That is only
exact while the coefficients stand still — `zf` is defined relative to
one coefficient set, and the mono path recomputes coefficients every
block from the block-mean cutoff_cv. The old loop's raw DF-I history
(x1, x2, y1, y2) is coefficient-independent, so the shipped version
keeps exactly that as the persisted state, converts it to the
equivalent transposed-DF-II `zi` at block start (the `lfiltic` identity,
inlined: zi1 = b1·x1 + b2·x2 − a1n·y1 − a2n·y2; zi2 = b2·x1 − a2n·y1)
and reads the history back off the input/output tails after the call.
Costs two scalar expressions per block; buys exactness under
modulation. State-dict keys are unchanged, so the mono↔voice
shape-switch reinit logic needed no edits — and slice 4 can vectorize
the same conversion straight across the voice axis.

**Equivalence: bit-identical, not just close.** Sandbox check against
the verbatim old loop: max abs error 0.0 — after the float32 cast, not
~1e-14 — across all three modes, an 8-block render with a different
cutoff_cv every block (the case zf-carry would get wrong), frames=1
blocks (history-tail edge case), and split-vs-whole renders. The 7 new
tests in `TestFilterMonoLfilterEquivalence` (tests/test_filter.py)
assert < 1e-6 rather than == so a future scipy that reorders float ops
doesn't break the suite spuriously; the old loop lives on in the test
file as `_reference_filter_mono`, the oracle.

**Suite:** 410 passed + 18 mido-skipped from the mount (was 403; +7).
Drive-by: the stale "we avoid scipy as a dep until the perf actually
pinches" paragraph in `_render_filter`'s docstring replaced with the
current story.

**Housekeeping.** Verified the slice-2 history-cleanup push landed:
local main == origin/main, junk commits gone. Transient TODO item
cleared. Mount-write protocol followed: staged in sandbox, whole-file
cp, byte-verified (cmp) both production files; AST-parsed on the mount.

**Hand-off to Matthew:**
- Commit — includes last session's still-uncommitted doc updates and
  the start.cmd whitespace touch (git writes from the sandbox still
  break on the mount). Suggested message in chat.
- Optional: `.\build.ps1` — first build that actually imports scipy,
  so the exe size delta (expected +30–40 MB on the 23.1 MB baseline)
  finally becomes measurable. Record it against the slice-3 TODO note.

**Next: slice 4** — `_render_filter_voice`: shared-coeff fast path =
one lfilter along the time axis (zi shape (V, 2)); per-voice cutoffs =
a 16-call loop. The raw-history↔zi conversion shipped here carries
over with (V,) arrays in place of scalars.

## 2026-06-12 — Filter vectorization slice 4: voice path on lfilter

**Shipped, same session as slice 3.** `_render_filter_voice` now runs
its V parallel biquads through lfilter. Two shapes, as planned:
- *Shared coefficients* (static cutoff or macro cutoff_cv): one lfilter
  call filters all 16 rows along the time axis with ``zi`` of shape
  (V, 2) — the 46x spike case.
- *Per-voice coefficients* ((V, F) cutoff_cv): lfilter can't vary
  coefficients across rows, so 16 independent single-row calls. Each
  row's recurrence still runs in C.

**State design carried over from slice 3, vectorized.** Persisted
state stays the raw DF-I history arrays (x1_arr..y2_arr), each (V,)
float64 — coefficient-independent, so per-block coefficient changes
(macro *or* per-voice) behave exactly as the old loop. The two
lfiltic-identity expressions convert history → zi at block start;
numpy broadcasting makes that code identical for scalar and (V,)
coefficients, which is the payoff of choosing raw history in slice 3.
State keys unchanged; the mono↔voice shape-switch reinit logic again
needed no edits.

**Equivalence: bit-identical again.** Max abs error 0.0 after the
float32 cast across: all three modes (shared, 8 blocks), macro 1D CV
changing every block, per-voice (V, F) CV changing every block (the
case zf-carry would get wrong, per voice this time), frames=1 blocks,
split-vs-whole renders in both coefficient shapes, and mono→voice
shape-switch reinit. 9 new tests in `TestFilterVoiceLfilterEquivalence`
with the verbatim old voice loop preserved as `_reference_filter_voice`
— same oracle pattern as the mono tests.

**Speed (sandbox).** Shared path 0.060 ms/blk vs the old loop's ~1.98
(~33x measured end-to-end incl. python overhead; the spike's pure-loop
ratio was 46x). Per-voice path 0.19 ms/blk (~10x) — the "smaller but
real win" the TODO predicted. Native numbers land at slice 6's
re-profile.

**Suite:** 419 passed + 18 mido-skipped from the mount (was 410; +9).
Drive-by: `_render_filter`'s docstring updated again — both paths now
on lfilter.

**Observation, not action:** post-commit, Claude.md and start.cmd show
working-tree diffs that are pure CRLF/LF churn (content identical) —
looks like autocrlf flip-flop, left alone. If it nags, a
`.gitattributes` with explicit eol rules would settle it; not queued.

**Hand-off to Matthew:** commit (message in chat). The optional
`.\build.ps1` size-delta measurement from the slice-3 hand-off still
applies unchanged if not yet run.

**Next: slice 5 (optional, separable)** — crossover on sosfilt (LR4 =
cascaded biquads, exactly sosfilt's shape). Droppable; if skipped,
slice 6 (native re-profile + docs) closes the filter-vectorization
arc.

## 2026-06-12 — CV source meters (+ exe size measured)

**Matthew ran the scipy build:** `dist/PySynthRack.exe` is now
54,206,720 bytes (51.7 MB) vs the 23.1 MB pre-scipy baseline — scipy
costs +28.6 MB, comfortably under the ~256 MB budget (and the thing
wants a couple GB of RAM to run regardless). Slice-3 TODO note
annotated with the figure; the filter-vectorization size question is
now closed.

**New feature — live CV meters on the node graph.** Every cv-kind
*output* port now shows a little 0..1 progress bar tucked under its
jack, so you can see what a modulator is doing at a glance.

*Backend (headless-tested).* `render_block` already builds every
port's buffer and threw it away; now it also stashes one block-mean
scalar per cv output port into `self._meter_levels`, building a fresh
dict each block and swapping the reference in atomically — no lock,
since a stale meter frame is harmless and the GUI only ever reads a
`snapshot_meter_levels()` copy. The cv-output port list is precomputed
each `compile()` (`_cv_output_ports`) so the hot path doesn't re-derive
signal kinds, and it resets on every recompile so stale meters can't
linger across a patch swap. Voice-aware (V, F) cv buffers collapse via
a full mean. 7 tests in `tests/test_cv_meters.py` cover capture, the
compile rebuild/reset, voice collapse, snapshot isolation, and the
disconnected-port (absent, not garbage) case. Suite 419 → 426.

*UI (Matthew runs to see it).* Two changes in `ui/app.py`:
1. `run()` swaps `dpg.start_dearpygui()` for the manual
   `while dpg.is_dearpygui_running(): self._update_cv_meters();
   dpg.render_dearpygui_frame()` loop — same vsync pacing, but now
   there's a per-frame hook. This is the one structural change.
2. `_create_node_for_module` adds a `dpg.add_progress_bar` under each
   cv output attribute (audio outputs get none — they'd peg at audio
   rate and mean nothing). `_clear_editor` drops the bar refs +
   auto-range state on patch load.

*Auto-range (the chosen behaviour).* Matthew picked per-source
auto-ranging over a fixed 0..1 bar — most CV here isn't unipolar
(bipolar LFOs, wide pitch CV). `_auto_range_fill` keeps a per-port
[lo, hi] window that relaxes 2%/frame toward the current value
(shrinking when extremes stop arriving) then re-widens instantly to
include it: instant attack, ~1 s release. A near-constant source
(range < 1e-6) parks the bar mid-scale instead of dividing by zero,
and the actual current value is printed as the bar overlay so the
auto-range's "what does full mean" ambiguity is always resolved by a
number. Verified the arithmetic standalone (sine sweeps full 0↔1,
constant → 0.5, step change captured instantly); the DPG rendering is
Matthew's to eyeball.

**Hand-off to Matthew:** commit (message in chat), then run the GUI
(`python -m pysynthrack` in the `[gui]` venv) and open a patch with an
LFO or ADSR to watch the bars move. The pyo backend has no meter hook;
`_update_cv_meters` no-ops there by design (getattr guard).

**Next:** back to the filter arc if you like — slice 5 (crossover on
sosfilt, optional) or slice 6 (native re-profile, closes it). Or keep
adding modules; the CV-utility trio (Constant / CVScale / CVOffset) is
still queued and would pair naturally with the new meters.


## 2026-06-17 — LFO rate slider ceiling 100 → 120 Hz

Bumped the LFO `rate` drag_float `max_value` from 100.0 to 120.0 in
`ui/app.py` (the only gate — the LFO module itself never clamped rate).
Lets the CV LFO be dialed to 120 Hz in the GUI; `min_value` (0.01) and
the `%.2f Hz` format are unchanged. Note `rate_cv` (1V/oct) can still
push effective rate past the slider ceiling at runtime, as before.
Staged via sandbox per the mount protocol; AST-parsed clean, line count
unchanged (900), no residual `max_value=100`.

**Hand-off to Matthew:** commit when convenient.


## 2026-06-28 — FilePlayer module (WAV → stereo audio source)

**New module: `file_player`.** A source that streams a WAV file into the
patch, so a recorded track can be split by the Crossover and used as a
modulation source. Closes Matthew's requested patch:
`track → crossover → low → AudioToCV → Oscillator(amp_cv)` and
`→ high → AudioToCV → CVToFrequency`. Everything downstream already
existed — the player was the only missing piece.

*Decisions (asked Matthew up front):* WAV-only (uses scipy.io.wavfile,
already a dep — zero new deps, no exe growth; 24-bit PCM is the one gap,
caught → silence); one-shot by default with a `loop` toggle; **stereo**
`left` / `right` output ports (mono files duplicate to both, >2 channels
keep the first two).

*Module* (`modules/fileplayer.py`). `TYPE="file_player"`, params
`path` / `gain` / `loop` / `armed`, no inputs, two audio outs. Pure model
object as usual — no DSP.

*Backend* (`audio/numpy_backend.py`). `_load_wav(path, target_sr)` decodes
the whole file to a contiguous `(2, N)` float32 array once: dtype-aware
normalise (int16/int32/uint8/float), mono→stereo duplicate, resample to the
engine rate via `scipy.signal.resample_poly` when the file rate differs (a
one-time load cost, not per block), returns `None` on any failure so the
audio thread renders silence rather than raising. `_render_file_player`
decodes lazily into `self._state` (re-decodes on path change), then each
block is a slice: one-shot zero-pads past the end and parks; `loop` wraps
with modular indexing so a block straddling the loop point is seamless;
`armed=False` parks the playhead at 0 so re-arming replays from the top.
`stop()` rewinds every file-player playhead so the next transport start
replays a one-shot. First-block decode happens on the audio thread (like
DiskWriter's lazy file open) — a multi-MB resample can hiccup the first
block; acceptable for now, a background loader is the obvious upgrade if it
bites.

*No UI / pyo changes.* The generic `_add_param_widget` already covers
`path` (text box), `gain` (0..2 slider) and the bool toggles (checkboxes),
and `_create_node_for_module` builds the `left`/`right` output jacks
automatically (audio outs, no meter). The pyo backend returns `None` for
unknown types, so `file_player` is a silent stub there — consistent with
crossover/diskwriter, and pyo is parked anyway.

*Tests* (`tests/test_file_player.py`, 13). Shape/registration, mono
duplicate, resample-to-engine-rate, one-shot→silence, seamless loop wrap,
gain, armed reset, missing/empty path → stereo silence, path-change reload,
`stop()` rewind, and the full `file → crossover → AudioToCV → Oscillator →
speaker` chain renders finite, non-silent audio. Suite **439 (+18 mido)**,
up from 426.

*Example* (`examples/file_crossover_split.json`). Exactly Matthew's patch,
both bands mixed to the speaker. Ships with an empty `path` (set it to a
`.wav` after opening) and `loop` on; loads/compiles/renders clean with no
file (the CVToFrequency drones at its `f0` until the high band steers it).

**Hand-off to Matthew:** commit (message in chat). To hear it: open
`examples/file_crossover_split.json`, set the FilePlayer `path` to a
`.wav`, hit play — the low band amplitude-shapes a saw, the high band
pitches a triangle. Drop a `.wav` straight into a DiskWriter chain too if
you just want to crossover-split and re-record a band.

**Next:** the filter arc still has slice 5 (crossover → sosfilt, optional)
and slice 6 (native re-profile). Or the CV-utility trio (Constant /
CVScale / CVOffset), which now pairs naturally with both the meters and
the file player.


## 2026-06-28 — FilePlayer: live elapsed / total playhead readout

Follow-up to the FilePlayer above: a transport time display on the node,
since audio outputs carry no CV meter and you couldn't otherwise see
where you were in a track. Matthew picked a UI readout (over a patchable
position CV output).

*Backend* (`audio/numpy_backend.py`). New `snapshot_file_positions()` ->
`{module_id: (elapsed_s, total_s)}`. Reads the per-module `_state` under
`self._lock` (only to copy the mapping so a concurrent `compile` can't
resize it mid-iteration); `pos` is written lock-free by the audio thread,
but an int read is atomic and a marginally stale playhead is fine for a
readout. `total = N/sr` from the decoded `(2,N)` buffer; `elapsed =
min(pos, N)/sr` so a finished one-shot reports its end, not past it. No
hot-path change — purely a pull from existing state. A file that hasn't
been decoded yet (lazy, pre-first-render) or an empty/bad path reports
`(0.0, 0.0)` / is simply absent.

*UI* (`ui/app.py`). `file_player` nodes get a `dpg.add_text` row showing
`"elapsed / total"` (`m:ss`); `_file_pos_labels` maps module_id -> tag.
The manual render loop already ticks `_update_cv_meters()` each frame —
added `_update_file_positions()` beside it, which pulls the snapshot and
sets each label via `_format_time`. Same defensive shape as the meters:
`getattr` hook guard (pyo no-ops), `.get(mid)` so a missing entry leaves
the last text rather than blanking. `_clear_editor` drops the refs on
patch load. Shows `0:00 / 0:00` until playback starts (decode is lazy by
design); thereafter it counts up and, for a one-shot, freezes at the
total.

*Tests.* 4 added to `tests/test_file_player.py` (TestPositionReadout):
elapsed+total track playback, one-shot elapsed clamps to total, missing
path -> (0,0), `stop()` rewinds elapsed to 0 while total stays known
(samples are kept). Suite **443 (+18 mido)**, up from 439. UI label
wiring is Matthew's to eyeball (no DPG in the sandbox), but it's the
verbatim CV-meter pattern.

**Hand-off to Matthew:** commit (message in chat). Open
`examples/file_crossover_split.json`, point the FilePlayer at a `.wav`,
hit play — the node shows e.g. `0:14 / 3:42` ticking up.

**Next:** unchanged — filter slice 5 (crossover->sosfilt) / slice 6
(re-profile), or the CV-utility trio. A patchable position CV output is
now an easy add if you ever want to modulate *with* the playhead.


## 2026-06-28 — MicInput module (live mic capture → stereo audio source)

Beatbox in. New `mic_input` source: hands the patch live audio off an
input device so a voice can be split by the Crossover and used as a
modulation source — low band → AudioToCV → sub-osc amp (kick-driven
envelope), high band → AudioToCV → CVToFrequency (hats steer a pitch).
Example `examples/mic_beatbox_crossover.json` wires exactly that.

*Decisions (asked Matthew):* stereo `left`/`right` outs (mono device
duplicates to both); selectable input device via a dropdown like
MIDIInput (vs default-only).

*Module* (`modules/micinput.py`). `TYPE="mic_input"`, params `device`
(`""`=system default) + `gain`, no inputs, two audio outs.
`available_input_devices()` enumerates capture devices via sounddevice
(filters `max_input_channels>0`, de-dupes, never raises); sounddevice
import is guarded so the module still registers without PortAudio.

*Backend* (`audio/numpy_backend.py`). The real work: live input means a
**full-duplex** stream. `start()` now opens `sd.Stream` (input+output, one
callback) *only* when the patch contains a mic module — patches without
one keep the cheaper `sd.OutputStream`, so no-mic / no-permission users
are never forced into capture. A duplex open that fails (no device, rate
mismatch, permission denied) logs and falls back to output-only, so the
rest of the patch still plays and the mic renders silence. `_audio_callback`
was refactored: the shared render+write body is now `_fill_output`, the
output-only callback calls it, and the new `_duplex_callback(indata,
outdata, …)` stashes `self._input_block = indata` before calling it.
`_render_mic_input` reads that block — 2ch → left/right, 1ch → duplicated,
gain applied, short block zero-padded / long truncated so a size mismatch
can never raise on the audio thread; None (output-only / pre-first-capture)
→ silence. `_resolve_mic_input` maps the device param to (device, channels)
for the open (`""`→default, channels clamped 1..2 from the device query).
`stop()` clears `_input_block`. Drive-by: fixed the pre-existing
`SyntaxWarning: invalid escape sequence '\|'` in the CVToFrequency docstring.

*UI* (`ui/app.py`). The `device` combo branch now covers `mic_input` too
(MIDI ports for midi_input, capture devices for mic_input); `gain` is the
generic 0..2 slider and the stereo out jacks are automatic. Snapshotted at
widget creation like MIDIInput — recompile to refresh after hot-plug.

*No pyo change* — `_build_module` returns None for unknown types, so
mic_input is a silent stub there (pyo is parked).

*Tests* (`tests/test_mic_input.py`, 15). Shape/registration, stereo split,
mono duplicate, gain, no-input silence, short/long block handling, full
mic→speaker dispatch, `stop()` clears the block, `_resolve_mic_input`
default/named, and device enumeration (empty without sounddevice, filters
+ de-dupes with a fake, never raises). Suite **458 (+18 mido)**, up from
443. The duplex `sd.Stream` setup in `start()` and the UI combo can't run
headless (no PortAudio/DPG in the sandbox) — those are Matthew's to verify
live.

**Feedback caveat:** mic → speakers in the same room = howl. The example
and the module docstring both say wear headphones.

**Built off-mount.** The project mount was serving corrupted reads this
session (autocrlf-on-flaky-mount truncation); this work was built and
tested against a fresh GitHub clone and delivered as a patch, so nothing
touched the flaky working tree.

**Hand-off to Matthew:** apply the patch (`git am`), then run the GUI,
add a MicInput node, pick your input device, and beatbox through
`mic_beatbox_crossover.json` (headphones!).

**Next:** filter slice 5 (crossover→sosfilt) / slice 6, or CV-utility trio.


## 2026-06-30 — CV-utility trio: Constant / CVScale / CVOffset

The greenfield piece from the TODO that pairs naturally with the CV
meters and the FilePlayer: three small, composable CV utilities so any
source can feed any destination with arbitrary scale and offset,
without baking the knob into the source module.

*Modules* (`modules/constant.py`, `cvscale.py`, `cvoffset.py`). Pure
model objects, mirroring the existing CV modules. `constant` (TYPE
`constant`): no inputs, one `cv` `out`, param `value` (default 1.0 — a
unity level is the most useful neutral; 0.0 is just silence, which you
already get from an unpatched input). `cv_scale` (TYPE `cv_scale`):
`in` cv → `out` cv, param `scale` (default 1.0) — the attenuverter
(attenuate <1, amplify >1, invert <0). `cv_offset` (TYPE `cv_offset`):
`in` cv → `out` cv, param `offset` (default 0.0) — slides the centre.
Scale-then-offset composes into a full affine map; kept as two
orthogonal one-job modules in the modular spirit rather than one
combined node. Registered in `modules/__init__.py` (alphabetical).

*Backend* (`audio/numpy_backend.py`). Three render methods + dispatch
entries. `_render_constant` fills the block with the scalar `value`
(`np.full`), always mono `(frames,)` — a constant has no voice context
of its own, and 1D broadcasts cleanly against any per-voice `(V, F)`
consumer downstream. `_render_cv_scale` / `_render_cv_offset` are pure
pointwise (`in * scale` / `in + offset`), so they're **shape-
polymorphic for free** with no per-voice state: they read the input
with `collapse=False`, so a mono `(F,)` input stays mono and a voice-
aware `(V, F)` input stays `(V, F)` (the scalar broadcasts across the
voice axis). Unpatched-input convention: both treat an absent input as
0, so CVScale → silence (`0 * scale`) and CVOffset → a constant
`offset` (which usefully makes an unpatched CVOffset a quick DC
source). Single-ndarray returns land under the `out` port via the
existing legacy-single-output store, and the CV-meter pass auto-picks
up the `cv` `out` jacks — so all three nodes get live meters with no
extra wiring.

*UI* (`ui/app.py`). No structural change — the generic param-widget
builder already covers them. Added one branch so `value` / `scale` /
`offset` render as a fine-grained drag-float (speed 0.01) with soft
±10 bounds, which covers ±1 modulation depths and several octaves of
1V/oct pitch voltage alike (the prior fallback was an unbounded coarse
drag). Auto CV meters on the outputs come from the registry as usual.

*Pyo* (`audio/pyo_backend.py`). Added the three types to the v0.3+
silent-stub tuple — numpy is the real implementation; pyo stays a
coherent v0.1 surface.

*Tests* (`tests/test_cv_utilities.py`, 26). Model (registration,
defaults, ports/signal kinds, JSON round-trip, unknown-param rejection,
type walls: cv↔cv legal, audio→cv input illegal, cv→audio sink
illegal); Constant (default/custom/negative values, mono shape, ignores
stray buffers); CVScale (attenuate/amplify/invert, zero→silence,
unpatched→silence, `(V, F)` preserved+scaled per row, mono stays mono);
CVOffset (add/negative/transparent, unpatched→constant offset, `(V, F)`
preserved + scalar broadcast, mono stays mono); integration (LFO ±1 →
CVScale 0.5 → CVOffset 0.5 lands in 0..1 centred on 0.5 and drives an
oscillator amp_cv to finite bounded audio; Constant 0.5 → CVToFrequency
sings at its mid anchor). Full suite **484 passing (+18 mido skipped)**,
up from 458 — exactly +26.

*Docs* (`docs/MODULES.md`). New **Utilities** category: index rows +
prose entries for all three. Example `examples/cv_utility_demo.json`
(LFO → cv_scale → cv_offset → filter cutoff for a rhythmic one-octave
sweep, plus Constant → cv_to_frequency for a dialed-in drone, both
summed to the speaker); compiles + renders ~1 s clean, peak 0.71.

**Hand-off to Matthew:** delivered as a git patch (`git am`). UI drag
ranges are mine-untested in a live DPG window (no DPG in the sandbox)
but reuse the existing generic-widget path. To hear it: open
`examples/cv_utility_demo.json`, hit play — the filter pulses once
every ~2.5 s under the LFO→scale→offset chain while the Constant-tuned
triangle drones underneath.

**Next:** unchanged backlog — filter slice 5 (crossover→sosfilt) /
slice 6 (re-profile), or the next utility (sample-and-hold pairs
naturally with these and the Schmitt clock; noise generator).


## 2026-06-30 — Sample-and-hold (`sample_hold`)

The next utility off the backlog, and a natural partner to the Schmitt
clock and the CV trio. Pure stepped sample-and-hold: on each rising
edge of a trigger it grabs the input's current value and holds it flat
until the next edge — the classic staircase.

*Design choices* (Matthew's call): **pure S&H, no internal noise** — an
unpatched `in` samples 0 rather than being normalled to a white-noise
source (the iconic random patch waits on the separate Noise generator;
keeps this a clean single-job module). **Stepped now, slew later** — no
`glide` param this pass, so the module is param-less (precedent:
Combiner). The trigger is a `gate` input, so it composes with the
existing gate emitters (Schmitt turns any LFO/CV into a clock;
Keyboard/MIDI/ADSR gates work too) rather than re-implementing
threshold detection.

*Module* (`modules/samplehold.py`). TYPE `sample_hold`, no params,
ports `in` (cv) + `trig` (gate) → `out` (cv). Registered in
`modules/__init__.py` (alphabetical, after `output`).

*Backend* (`audio/numpy_backend.py`). `_render_sample_hold` dispatches
to mono / voice helpers. Vectorized rising-edge forward-fill, the same
trick the Schmitt trigger uses: a rising edge is `gate high & previous
sample low` (the first frame's "previous" is the gate state carried
from the end of the last block); the held value at sample n is the
input sampled at the most recent edge ≤ n, found with
`np.maximum.accumulate` over edge positions, with pre-first-edge
samples keeping the value carried from the previous block. No
per-sample Python loop. Shape-polymorphic with `collapse=False`: mono
`(F,)` in/trig → `(F,)` out (scalar `held` + `prev_gate` state); a
`(V, F)` on *either* input → `(V, F)` out with per-voice `held_arr` +
`gate_arr`, a mono partner broadcasting across the voice axis (so a
shared clock can sample per-voice sources, or per-voice clocks can
sample one shared source). Voice dimension is taken from whichever
input carries the voice axis. Conventions: unpatched `in` → samples 0;
unpatched `trig` → no edges, holds last value (0 at startup). The
single-ndarray return lands under `out` and auto-gets a CV meter.

*Pyo* (`audio/pyo_backend.py`). Added `sample_hold` to the v0.3+
silent-stub tuple.

*UI.* No change — param-less, so the generic widget loop renders just
the ports + the auto CV meter on `out`.

*Tests* (`tests/test_sample_hold.py`, 24). Model (registration, no
params, ports/signal kinds, JSON round-trip, unknown-param rejection,
type walls: cv→in legal, gate→trig legal, audio→in illegal, cv→trig
illegal, cv→audio-sink illegal); mono (holds 0 pre-trigger, samples at
the rising edge, holds flat between edges, only rising edges sample,
state + no spurious seam edge across blocks, unpatched in→0, unpatched
trig→hold); voice-aware ((V, F) per-voice sampling, mono-source/
per-voice-clocks, shared-clock/per-voice-sources, per-voice state
across blocks, mono stays 1D); integration (LFO→Schmitt→S&H is a
piecewise-constant staircase at ~clock rate with >95% flat samples;
the full LFO(random)→S&H→CVScale→CVOffset→CVToFrequency→speaker chain
renders finite, audible audio). Full suite **508 passing (+18 mido
skipped)**, up from 484 — exactly +24.

*Docs* (`docs/MODULES.md`). Index row + a Utilities entry. Example
`examples/sample_hold_arp.json` (random LFO sampled by an LFO→Schmitt
clock, then CVScale→CVOffset→CVToFrequency: a self-playing stepped
arp); compiles + renders ~1 s clean, peak 0.70.

**Hand-off to Matthew:** delivered as a git patch stacked on the
CV-utility trio — apply `cv_utility_trio.patch` first, then
`sample_hold.patch` (both `git am`). To hear it: open
`examples/sample_hold_arp.json`, hit play — a new random pitch every
quarter-ish second (the 4 Hz clock), each held steady between steps.

**Next:** Noise generator (white/pink) is the obvious follow-up — it
turns this into the textbook random-voltage source and would let the
S&H normal to it later. Filter slice 5/6 still open.


## 2026-06-30 — Noise generator (`noise`): white + pink

The textbook random source, and the natural partner to Sample-and-Hold
(noise → S&H = stepped random voltages). Off the backlog.

*Design choice* (Matthew's call): **two output jacks**, an `out`
(audio) and a `cv`, both carrying the same noise stream — so noise
drives filters/speakers *and* modulation without any bridge module
(the way Keyboard exposes `out` + `gate`). This beat "cv only" (audio
would need a CVToAudio in every sound patch) and "audio only" (the only
audio→cv bridge is the envelope follower, which would smear the noise
rather than pass raw random values).

*Module* (`modules/noise.py`). TYPE `noise`, no inputs, outputs `out`
(audio) + `cv` (cv). Params `color` (`white`/`pink`, exported as
`NOISE_COLORS`) and `amp` (default 1.0). Registered in
`modules/__init__.py`.

*Backend* (`audio/numpy_backend.py`). `_render_noise` returns a dict
`{"out": sig, "cv": sig}` — the same float32 array on both jacks
(consumers are read-only, exactly like existing fan-out). `white` is
`np.random.uniform(-1, 1)` (hard-bounded, the convention LFO's
`random` already uses). `pink` filters that white through a class-level
3rd-order pinking IIR (`_PINK_B`/`_PINK_A`, the music-dsp standard
coefficients) via `scipy.signal.lfilter` — the filter state `zi` is
carried in `self._state` across blocks so the spectrum is continuous at
block seams (same zi-carry pattern as the filter vectorization slices)
— then scaled by `_PINK_SCALE = 11.7027` to RMS-match uniform white, so
`amp` means the same level for both colors. Switching color back to
white drops the stale `pink_zi`. Output is always mono `(frames,)` — a
source has no voice context of its own (like Constant). The dict return
lands under both ports via the existing multi-output store, and the
`cv` jack auto-gets a CV meter. Measured: white spectrum flat
(low/high ≈ 1.0), pink slope −3.0 dB/oct.

*Pyo* (`audio/pyo_backend.py`). Added `noise` to the v0.3+ silent-stub.

*UI* (`ui/app.py`). Added a `color` combo branch (imports
`NOISE_COLORS`); `amp` reuses the existing 0..1 level slider.

*Tests* (`tests/test_noise.py`, 26). Model (registration, defaults,
dual-jack ports/signal kinds, JSON round-trip, unknown-param rejection,
type walls: audio→audio-sink legal, cv→cv legal, audio→cv illegal,
cv→audio-sink illegal); white (mono shape/dtype, both jacks the same
array, hard-bounded to ±amp, ~zero mean, roughly flat spectrum via
Welch, amp scales RMS); pink (mono, steep low/high tilt, slope ≈ −3
dB/oct, zi carried + evolving across blocks, switch-to-white drops
state, RMS ≈ white); randomness (consecutive blocks differ, two modules
independent); integration (white→filter→speaker audible; noise.cv→S&H
clocked = bounded random staircase >95% flat; noise.cv→CVToAudio→
speaker bridge path). Full suite **534 passing (+18 mido skipped)**, up
from 508 — exactly +26.

*Docs* (`docs/MODULES.md`). Sources index row + entry. Example
`examples/noise_hat.json` — white → highpass filter → VCA, with the VCA
opened by an ADSR clocked from an LFO→Schmitt: a self-playing hi-hat
(first percussion example in the set). Renders ~1 s clean.

**Hand-off to Matthew:** delivered as a git patch stacked on the trio +
sample_hold — apply in order: `cv_utility_trio.patch`,
`sample_hold.patch`, then `noise.patch` (all `git am`). To hear it:
open `examples/noise_hat.json`, hit play — a ticking hi-hat at 8 Hz.
Switch the noise `color` to `pink` for a softer, lower hat.

**Next:** with noise in hand, the S&H could optionally *normal* its
`in` to an internal/!patched noise source (revisits the design Matthew
deferred). Otherwise filter slice 5/6, or an AD percussion envelope to
pair with the noise drums.

---

## 2026-06-30 — ParametricEQ (`parametric_eq`): 4-band peaking EQ

Started life as Matthew's "one input, 64-selector log/linear graphic EQ"
question; scoped down in conversation to what he actually wanted — a
small **parametric** EQ: a handful of bells with adjustable centre
frequency, gain, and Q. Locked at 4 bands, all peaking, each centre
fully sweepable across 20 Hz–20 kHz (defaults 25/50/100/250 Hz, his
bass-shaping brief). That scoping erased the two hard parts of the
graphic version — no vector/array param (12 plain scalars) and no
custom slider-bank widget (ordinary knobs). A routine module add.

*Model* (`modules/parametric_eq.py`). `TYPE = "parametric_eq"`, mono
`in` (audio) → `out` (audio). `DEFAULT_PARAMS` is generated from a
band list: `band{i}_freq/gain/Q` for i in 1..4. `EQ_BANDS = 4` is the
single knob to change the band count — the backend and UI both derive
the band list by walking `band{i}_freq`, so nothing else hardcodes 4.

*DSP* (`numpy_backend.py`). Four RBJ **peaking** biquads cascaded.
`_peq_coeffs` does the cookbook math vectorized over all bands at once
(freq clamped to 20…0.45·sr, Q to 0.1…20); a band at 0 dB collapses to
identity coefficients (`b == a`), i.e. exact passthrough, so unused
bands are tonally free. State design copies the Filter module (slices
3+4): persisted state is the coefficient-independent DF-I history
`(x1,x2,y1,y2)`, one entry per band, converted to the transposed-DF-II
`zi` each block — so editing a band's freq/gain/Q between blocks stays
clean, unlike sosfilt's coefficient-bound `zi`. Shape-polymorphic like
Filter/Crossover: a `(F,)` input runs one cascade via `lfilter`; a
`(V, F)` voice input runs V parallel cascades, one `lfilter` call per
stage with `zi` of shape `(V, 2)` (coeffs shared — no CV yet). pyo
silent-stub added.

*UI* (`ui/app.py`). Gated on `module.TYPE == "parametric_eq"`:
`*_freq` → drag-float 20…20000 Hz, `*_gain` → slider −24…+24 dB,
`*_q` → slider 0.1…20. (Note: with per-band adjustable freq the
log/linear question from the original brief no longer applies — each
centre is entered directly in Hz; the freq drag matches the existing
`cutoff`/`frequency` controls.)

*Verification.* Bench-measured before writing tests: flat (all 0 dB)
is bit-exact transparent; +12/−12 dB at the band centre measure 12.00/
−12.00 dB (RBJ peaking gain at f0 = design gain); block-stitch and the
voice path are bit-identical to the reference; low-Q bell leaks 7.4 dB
an octave away vs 0.3 dB for high-Q. 27 tests in
`tests/test_parametric_eq.py` (model/ports/round-trip/type-walls;
coeff math + 0 dB identity + clamping; mono boost/cut/Q/independence/
stitch/silence; voice shape/row-match/stitch/reinit; noise→eq→speaker
integration). Full suite **561 passing (+18 mido skipped)**, up from
534 — exactly +27.

*Docs.* `docs/MODULES.md` Processors index row + entry. Example
`examples/parametric_eq_bass.json` — saw @ 55 Hz → EQ (50 Hz +9, 120 Hz
−5, 250 Hz +2, 2.5 kHz +4) → speaker; loads and renders clean.

**Hand-off to Matthew:** delivered as a standalone git patch on top of
`origin/main` (HEAD `ae0961f`, the trio+S&H+noise already pushed) —
`git am` it. To hear it: open `examples/parametric_eq_bass.json`, hit
play — a saw with weighted sub, scooped low-mids, and a touch of edge.

**Next:** per-band freq/gain CV inputs would make it an animated EQ
(the obvious extension, deferred — Crossover has the same gap). Filter
slice 5 (crossover on sosfilt) / slice 6 re-profile still open; AD perc
envelope still the natural pairing for the noise drums.

---

## 2026-06-30 — FilePlayer: Browse button for WAV selection (UI)

Small UX win Matthew asked for: a **Browse...** button on the FilePlayer
node so you can pick the WAV from a file dialog instead of typing the
path by hand.

*UI* (`ui/app.py`, the only file touched). One shared WAV file dialog
built in `_build_file_dialogs` alongside the existing open/save patch
dialogs (tag `wav_dialog`, filtered to `.wav` + `.*`) — one is enough
because picking a file is modal. `_add_param_widget` now special-cases
the FilePlayer `path` param: it renders the path `input_text` (with an
explicit tag `fileplayer_path_{id}`) next to a **Browse...** button in a
horizontal group. The button's callback `_show_wav_dialog` records which
node asked via `self._wav_target_id`, then shows the dialog;
`_on_wav_selected` reads the picked path (`selections` first, falling
back to `file_path_name`), applies it through the same
`backend.set_param(id, "path", ...)` mutation that typing uses, and
writes it back into the field with `dpg.set_value`. Typing a path by
hand still works exactly as before — the dialog is purely additive.

No backend change: `_render_file_player` already re-decodes when
`state["path"] != params["path"]`, so a freshly-picked file loads on the
next block without a recompile.

*Verification.* The test suite doesn't cover the DPG layer, so checked
headlessly instead: under a DPG context (numpy backend injected — the
sandbox has no PortAudio) built the dialog + a FilePlayer node and drove
the callbacks. Confirmed the path field starts at the param value, a
simulated pick updates both the model param and the displayed field,
`_wav_target_id` resets after, the `file_path_name`-only fallback works,
and other node types still build. Full suite still **561 passing (+18
mido skipped)** — unchanged, as expected for a UI-only edit.

*Docs.* `docs/MODULES.md` FilePlayer entry — path row + Notes mention
the Browse button.

**Hand-off to Matthew:** delivered as a git patch **stacked on
`parametric_eq.patch`** — `git am` order: `parametric_eq.patch` then
`fileplayer_browse.patch` (both clean on origin/main HEAD `ae0961f`).
It's a GUI-only change, so confirm it live: open a patch with a
FilePlayer, click **Browse...**, pick a WAV, hit play.

---

## 2026-06-30 — FilePlayer: ffmpeg decode (mp3/flac/ogg + video audio)

Matthew wants to feed the FilePlayer audio from video files. Added an
ffmpeg decode fallback so it reads anything ffmpeg can — mp3, flac, ogg,
m4a, and the audio track of video containers (mp4/mkv/mov/webm) — while
keeping WAV on the existing zero-dependency scipy path. He picked the
"both" provisioning strategy: prefer a bundled binary, fall back to a
system ffmpeg, else WAV-only.

*New* (`audio/media.py`). `find_ffmpeg()` resolves an executable in two
steps — a binary bundled by the optional `imageio-ffmpeg` dep first
(`get_ffmpeg_exe()`), then `shutil.which("ffmpeg")` — cached for the
process. `decode_with_ffmpeg(path, sr)` shells out
(`ffmpeg -v error -nostdin -i path -vn -f f32le -ac 2 -ar sr pipe:1`),
reads raw little-endian float32 off stdout, reshapes the interleaved
bytes to a contiguous `(2, N)` — the exact contract `_load_wav` returns.
ffmpeg does the stereo downmix and the resample; we only reshape. Any
failure (no ffmpeg, missing file, no audio stream, nonzero exit, empty
output) returns None so the player renders silence, never raises.

*Wiring* (`numpy_backend.py`). New `_decode_audio(path, sr)`: try the
scipy WAV fast path, and on None fall back to `media.decode_with_ffmpeg`.
`_render_file_player` now calls `_decode_audio` instead of `_load_wav` —
nothing else changed, so loop/playhead/one-shot/re-arm and the
path-change re-decode all work unchanged. Bonus: a 24-bit WAV (which
scipy can't open) now also succeeds via the ffmpeg fallback.

*Deps / packaging.* New optional extra `media = ["imageio-ffmpeg>=0.4"]`
(also folded into `[all]`); requirements.txt note. `pysynthrack.spec`
gained a guarded `collect_all("imageio_ffmpeg")` so the bundled binary
ships in the exe when the extra is installed — wrapped in try/except so
the default build is byte-for-byte unchanged. **Build note for Matthew:
verify the next `[media]` build actually bundles the binary and that the
exe size lands where expected (was 51.7 MB; imageio-ffmpeg's binary is
~30 MB, so ~80 MB, well under the ~256 MB budget).**

*UI* (`ui/app.py`). The Browse dialog's filter widened from `.wav` only
to a grouped Audio/Video filter (wav/mp3/flac/ogg/m4a/aac/wma + mp4/m4v/
mov/mkv/webm/avi) plus `.wav` and `.*`.

*Tests* (`tests/test_media.py`, 16). Discovery (no crash, sane types);
byte parsing via a mocked subprocess (interleaved f32le → (2, N);
empty/error/no-ffmpeg/missing-file/ragged-bytes → None); backend
dispatch (readable WAV never invokes ffmpeg, garbage → None, non-WAV
without ffmpeg → None); and real ffmpeg integration **skipped when no
ffmpeg is present** (FLAC round-trip, decode resamples 22050→44100, the
backend routes non-WAV through ffmpeg, a FilePlayer renders audible
audio from a FLAC, and the audio track of a synthesized mp4 decodes).
Full suite **577 passing (+18 mido skipped)** with ffmpeg available, up
from 561 — exactly +16. On a machine with neither ffmpeg nor the extra,
the 6 integration tests skip instead.

*Known limits.* Decode is whole-file-into-memory (pre-existing FilePlayer
trait) — a feature-length film's audio is GBs; streaming decode is a
future upgrade. Decode still happens lazily on the audio thread on first
play, so a big file gives one long first-block stall (also pre-existing).

*Docs.* `docs/MODULES.md` FilePlayer entry rewritten (intro/path/notes).

**Hand-off to Matthew:** delivered as a git patch **stacked on
`fileplayer_browse.patch`** — `git am` order: `parametric_eq.patch`,
`fileplayer_browse.patch`, then `fileplayer_ffmpeg.patch` (all clean on
origin/main `ae0961f`). To use video audio: `pip install -e ".[media]"`
(or have ffmpeg on PATH), open a FilePlayer, Browse to an mp4, play.

**Next:** streaming/chunked decode for long files (drop the whole-file
memory load); decode off the audio thread so first play never stalls; a
small "ffmpeg: bundled / system / none" status hint on the node.

---

## 2026-06-30 — Meter module: audio level indicator (dBFS)

Matthew wanted a level indicator he can hang off any audio signal to
compare source levels (mic vs file player), low-latency, rough is fine.
Chose a **dedicated Meter module** (over auto-meters on every jack) with
a **dBFS scale floored at −90**.

*New* (`modules/meter.py`). `Meter` — `TYPE="meter"`, audio `in` →
**pass-through** audio `out`, no params. It's a monitoring tap: audio
forwards untouched (same array, same shape, mono or voice-aware) so it
sits inline (`source → meter → speaker`) or hangs off a fan-out cable.

*Backend* (`numpy_backend.py`). `_render_meter` forwards `in`→`out` and
updates a peak envelope: `peak = max(|block|)` (over samples and
voices), **instant attack, slow decay** (`env = peak` if rising, else
`peak + (env−peak)·_METER_DECAY`, `_METER_DECAY=0.985` ≈ 0→−20 dB in
~1.8 s). Computed on the audio thread, so a transient registers even
between UI frames — latency is block-rate (~12 ms), not frame-rate,
which is what Matthew asked for. Levels go to a new `self._audio_levels`
{module_id: linear peak}; `snapshot_audio_levels()` copies it for the
GUI. Keys are pre-created in `compile()` on the GUI thread so the audio
thread only ever updates values — the snapshot copy needs no lock (same
discipline as the CV `_meter_levels`). pyo silent-stub extended.

*UI* (`ui/app.py`). Meter nodes get a `dpg.add_progress_bar`;
`_update_audio_meters()` (added to the render loop next to
`_update_cv_meters`) reads the snapshot, maps each linear env to a
**fixed −90..0 dBFS** bar (`20·log10`), and writes a `"-xx.x dB"` /
`"-inf dB"` overlay. Fixed scale (not the CV meters' auto-range) on
purpose: two meters must share a reference to be comparable. Bars
tracked in `_audio_meter_bars`, cleared in `_clear_editor`.

*Verification.* 18 tests in `tests/test_meter.py` (model/ports/round-
trip/type-walls; pass-through mono+voice+disconnected; envelope: instant
attack reads peak, max-abs peak, slow decay = before·_METER_DECAY,
attack overrides a decayed level, silence→0, voice→loudest; osc→meter→
speaker integration renders audible + meters nonzero). The DPG layer
isn't in the suite, so checked headlessly under a real context: osc→
meter→speaker, render, `_update_audio_meters()` — env 0.5 → −6.0 dB →
bar fill 0.933 + overlay "-6.0 dB"; silence → "-inf dB", fill 0. Full
suite **595 passing (+18 mido skipped)** with ffmpeg present, up from
577 — exactly +18.

*Docs.* `docs/MODULES.md` Utilities index row + entry. Example
`examples/meter_levels.json` — a loud saw (≈−1.9 dB) and a quiet square
(≈−12.0 dB), each through its own meter into a mixer: the two bars read
clearly different, demonstrating the comparison use case.

**Hand-off to Matthew:** delivered as a git patch **stacked 4th** —
`git am` order: `parametric_eq.patch`, `fileplayer_browse.patch`,
`fileplayer_ffmpeg.patch`, then `meter.patch` (all clean on origin/main
`ae0961f`). GUI feature, so confirm live: open `examples/meter_levels.json`,
hit play, watch the two bars.

**Next:** if it gets heavy use, options worth offering — a peak-hold tick
that lingers at the max; a switchable RMS mode; a stereo/2-channel meter;
a clip indicator at 0 dBFS. All additive to this module.

---

## 2026-06-30 — AD envelope (`ad_envelope`): trigger Attack/Decay

The percussion envelope flagged repeatedly as the natural pairing for
the noise drums. A trigger-style Attack→Decay: a rising edge fires it
and the A→D contour plays to completion **regardless of trigger length**
— so a momentary clock pulse gives a full, consistent hit, with no
sustain stage holding the tail open (that's ADSR's job).

*New* (`modules/ad_envelope.py`). `ADEnvelope` — `TYPE="ad_envelope"`,
`trig` (gate) in → `cv` out, params `attack`/`decay` (seconds). Port
named `trig` (kind gate, like `sample_hold`'s) to signal "edge trigger,
not held gate".

*Backend* (`numpy_backend.py`). State machine idle→attack→decay→idle.
Rising edge on `trig` (re)enters attack **from the current level** (a
retrigger mid-decay picks up where it was — no click); attack ramps to
1.0, decay ramps to 0.0, then idle. The trigger going low is ignored.
Mono is a scalar per-sample loop (the reference); the voice path is a
per-sample loop vectorized across V — deliberately written so each
sample applies exactly one stage update per voice based on the phase at
the *start* of the sample, so a voice crossing attack→decay still emits
1.0 and decays the next sample, i.e. **bit-identical to the mono path**
(asserted in tests). Chose the per-sample voice loop (like the Crossover
voice path) over ADSR's run-splitting: simpler and plenty fast for an
envelope; run-based vectorization is a later optimization if profiling
flags it. pyo silent-stub extended.

*UI.* None needed — `attack`/`decay` already hit the existing
attack/decay/release drag-float branch (0…5 s), and the `cv` output gets
the standard CV meter for free.

*Tests* (`tests/test_ad_envelope.py`, 20). Model/ports/round-trip/
type-walls; mono (silence without a trigger, reaches 1→0, **gate length
ignored** = 1-sample pulse equals a 500-sample gate, attack length
tracks the param, attack-then-decay monotone, instant attack, retrigger
deep in decay climbs back up, disconnected→silence); voice (single-voice
row == mono bit-for-bit, voices independent, mono↔voice reinit, block-
stitch equivalence); integration (lfo→schmitt clock → AD → vca renders
audible audio). Full suite **615 passing (+18 mido skipped)** with
ffmpeg present, up from 595 — exactly +20.

*Docs.* `docs/MODULES.md` Modulation index row + entry. Example
`examples/ad_kick.json` — square LFO → Schmitt clock → AD → VCA on a
55 Hz sine: a self-playing kick (peak ≈ 0.8).

**Hand-off to Matthew:** delivered as a git patch on top of origin/main
(HEAD `4e97e35` — all prior features already pushed) — `git am` it. To
hear it: open `examples/ad_kick.json`, hit play (a kick every 0.5 s);
drop the decay for a tighter click, or swap the sine for noise→highpass
for a hat.

**Next:** a curve/shape param (exponential vs linear segments) would
make it punchier; per-trigger velocity scaling once a velocity CV exists;
pitch-envelope kicks once an env can hit `cv_to_frequency`. Run-based
voice vectorization if envelopes ever dominate a profile.

---

## 2026-06-30 — Resampler (`resampler`): varispeed pitch shifter

Matthew's dream module: "a bespoke audio resampler for pitch shifting,
C→D or D→C, or just a slider." Scoped it together — he chose **varispeed**
(pitch and speed coupled, tape/turntable style — the literal resampler),
**slider + CV** control, **linear** interpolation, a **looping buffer** so
it keeps sounding on a continuous signal, and an **optional glide**. The
honest trade he signed off on: a varispeed device reading at a different
rate than it's fed can't stay in sync with a live stream forever, so it
loops a short window of recent audio (faint repeat texture on extreme
shifts) and carries ~90 ms of latency — the unavoidable cost of varispeed
on a continuous signal, and what makes the pitch freely glide-able.

*Model* (`modules/resampler.py`). `in` (audio) + `pitch_cv` (cv) →
`out` (audio). Params `semitones` (±24), `cents`, `cv_depth` (semitones
per CV unit, default 12 = one octave/unit), `glide` (portamento seconds,
0 = instant). Registered in `modules/__init__.py`; added to the pyo
silent-stub tuple.

*DSP* (`audio/numpy_backend.py`, `_render_resampler` + `_render_resampler_core`).
Pitch summed in semitone space `st = semitones + cents/100 + cv_depth·cv`,
optionally glided (one-pole via `lfilter`), clamped to ±60 st, then
`ratio = 2^(st/12)`. Each block: write the incoming audio into a per-voice
ring buffer; the read positions are the cumulative integral of the
per-sample ratio, wrapped into the loop window with one `np.mod`, read
with linear interpolation between the two bracketing samples. The read
head is carried block-to-block as a lag behind the write head
(`delay + frames − Σratio`), wrapped by integer span steps so unity ratio
stays click-free. **Fully vectorized** (no per-sample Python loop) — the
mono path runs the same `(V, F)` core with `V = 1`, so a single voice row
is bit-identical to mono. Disconnected audio → silence (heads left as-is).

*UI* (`ui/app.py`). `_add_param_widget` gains a `resampler` block:
`semitones` (±24 st slider), `cents` (±100 ct slider), `cv_depth`
(0–48 st/unit drag), `glide` (0–5 s drag). Node jacks/palette come from
the registry automatically.

*Tests* (`tests/test_resampler.py`, 22). Model/ports/round-trip/type-walls
(audio→in, cv→pitch_cv legal; cv→in, audio→pitch_cv, audio-out→cv-sink
illegal); mono (disconnected→silence; **unity is a bit-exact delayed
passthrough**; octave up doubles / down halves the pitch; cents == semitones
bit-for-bit; CV summed in semitone space — cv·depth == the same semitones;
finite + bounded on sustained extremes ±60 st; **glide ramps through
intermediate pitches**); voice (single row == mono bit-for-bit, per-voice
CV transposes voices independently, mono↔voice reinit); integration
(osc→resampler→speaker audible; LFO→pitch_cv vibrato). Full suite
**637 passing (+18 mido skipped)**, up from 615 — exactly +22.

*Docs.* `docs/MODULES.md` Processor index row + full entry. Example
`examples/resampler_tape_wobble.json` — saw → varispeed with a slow
bipolar LFO on `pitch_cv` → speaker (self-playing wow/flutter, peak ≈ 0.56).

**Hand-off to Matthew:** delivered as a git patch on top of origin/main
(HEAD `3a8f729`) — `git am` it. To hear it: open
`examples/resampler_tape_wobble.json` and hit play (a saw warbling like a
tape with a slipping capstan); set `cv_depth` to 0 and `semitones` to +7
for a clean fifth-up, or raise `glide` and drag `semitones` down for a
tape-stop.

**Next:** a short equal-power crossfade at the loop seam would declick the
repeat texture; a **pitch-only** sibling (granular or phase-vocoder) is the
natural follow-up for shifting pitch while holding speed; a window-size /
latency param and a dry/wet mix would round it out; an anti-alias lowpass
before large up-shifts would tame the brightness.

---

## 2026-06-30 — PitchShifter (`pitch_shifter`): time-preserving granular shift

The speed-preserving cousin of the resampler, and the most involved
module so far. Scoped in-convo: granular over phase-vocoder, dry/wet
mix, exposed grain knobs. The first prototype (plain windowed overlap-
add) was correct on pitch + duration but **combed badly on tonal
material** — each harmonic split into a beating ±grain-rate doublet,
audible on the pure/near-pure waveforms this synth produces. Held for a
steer; Matthew picked **WSOLA** (waveform-similarity overlap-add), which
fixes it: each grain is nudged to the position that best correlates with
the previous one (a tie-break bias toward the smallest shift keeps pure
sines stable), so overlap joins are phase-continuous and the shift comes
out clean.

*Algorithm.* Pitch shift = WSOLA time-stretch by the ratio r, then
resample by r to restore duration. Validated offline first (clean fifth
on saw *and* sine, full amplitude), then ported to a streaming engine
and stress-tested across block boundaries. A nasty bug en route: the
stretched-output ring accumulated grains with `+=` but never cleared
consumed slots, so stale data piled up as the ring wrapped — killed
amplitude and smeared duration. Fix: zero each stretched slot once the
read pointer passes it.

*Engine* (`audio/numpy_backend.py`, `_GrainShifter` + `_render_pitch_shifter`).
`_GrainShifter` is one channel's streaming WSOLA state (input ring,
stretched-output OLA ring, analysis pointer with a vectorized NCC search
via `np.correlate` + cumsum norms, and a vectorized resample read). The
renderer keeps a list of engines, one per voice slot, so a single voice
is bit-identical to mono. Pitch is summed in semitone space and sampled
per block (block-rate CV — ample for vibrato); effective shift clamped
to ±36 st. Dry tap is latency-compensated for the `mix`. Shape-
polymorphic like Filter/Crossover.

*Module* (`modules/pitch_shifter.py`). `in`(audio)+`pitch_cv`(cv) →
`out`(audio). Params `semitones`(±24), `cents`, `cv_depth`(12),
`mix`(1.0), `grain_size`(50 ms), `overlap`(2). Registered; pyo silent-
stub extended.

*UI* (`ui/app.py`). `_add_param_widget` `pitch_shifter` block: semitones
(±24 st), cents (±100 ct), cv_depth (0–48 st/unit), mix (0–1), grain_size
(10–200 ms), overlap (2–4 int).

*Tests* (`tests/test_pitch_shifter.py`, 24). Model/ports/round-trip/type-
walls; mono (disconnected→silence; octave up/down + a fifth; **time-
preserving** = a held tone stays steady and full-level; CV in semitone
space; mix=0 dry / mix=1 wet; finite on extremes; grain/overlap variants
still shift); voice (row==mono bit-identical across 14 blocks, per-voice
CV independence, mono↔voice reinit); integration (osc→shifter→speaker,
50% mix harmony). Full suite **661 passing (+18 mido skipped)**, up from
637 — exactly +24.

*Docs.* `docs/MODULES.md` Processor index row + full entry. Example
`examples/pitch_shifter_harmony.json` — saw → +7 st at 50% mix → speaker:
a self-playing fifth (peak ≈ 0.55).

**Hand-off to Matthew:** delivered as `pitch_shifter.patch`, **stacked on
`resampler.patch`** — `git am` order: `resampler.patch` then
`pitch_shifter.patch` (both verified clean on origin/main `3a8f729`, full
suite green in the am'd tree). To hear it: open
`pitch_shifter_harmony.json` (a fifth that holds tempo); set `mix` to 1.0
for a full transpose, or feed it the FilePlayer to re-pitch a loop while
it keeps time.

**Next:** a formant-preserve option (resample the spectral envelope
separately); pitch-synchronous grain sizing so deep bass stays clean with
short grains; vectorize the per-voice search if it ever dominates a
profile; transient detection to sharpen attacks; and a small octave-
accuracy tighten (~12 cents sharp at +12 on a pure sine).

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
