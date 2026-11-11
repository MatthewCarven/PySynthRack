# PySynthRack — Roadmap

Living list of what's next. Edit freely.

> Compacted 2026-07-03: the completed history — all of v0.1–v0.4 plus every
> shipped Later/wishlist and CV-coverage entry — moved **verbatim** to
> [TODO-ARCHIVE.md](TODO-ARCHIVE.md). Archived entries keep their follow-up
> notes; grep the archive before assuming an idea is new.
>
> Consolidated 2026-10-10, the second pass: everything done, closed,
> superseded or duplicated moved **verbatim** to TODO-ARCHIVE.md §
> "Consolidation of 2026-10-10", under the section heading it had here,
> and what is still open is regrouped into the sections below. Matthew's
> 2026-10-05 listening verdicts are recorded there as EARS PASSED. The
> `Later:` / `Follow-ons:` ideas on shipped entries went with those
> entries, so grep the archive (and docs/MODULE_IDEAS.md) before assuming
> an idea is new; the few open threads lifted out of archived entries say
> "Hoisted 2026-10-10".

## Listening session checklist (banked, 2026-09-19)

Everything that shipped since the last quiet room, in the order that
makes sense to sit through. Load the file, press Start, and the note in
brackets says what to listen or look for. Tick as you go; anything that
sounds wrong, one line here is enough and Claude takes it from there.
(Older banked items are under "Still owed from earlier sessions" at the end.)

**First run `python examples/samples/generate_samples.py` once** (the
sampler examples need it).

**2026-10-05:** Matthew heard twelve of the older banked files; his
verdicts are in TODO-ARCHIVE.md (§ Consolidation of 2026-10-10, under
"The sampler"), and what is left of that bank is under "Still owed from
earlier sessions" below.

*The delay's clock sync + stereo echo (landed 2026-11-11) — newest, start here.*
Every existing example that uses the delay renders bit-identical (checked at
block 512 and block 100), so the only thing to hear is new.
- [ ] `delay_pingpong_sync.json` — the 120 BPM clock drives both the sequencer and the delay's `clock` (`division` 1, `feel` dotted), with `out_l`/`out_r` into the stereo output. The echoes should bounce left and right and stay locked to the beat.
- [ ] **Call 3, crossfade vs bend.** In that patch, change the clock's `bpm` while it plays. The echo time follows with a 10 ms crossfade, so there should be no pitch swoop and no click. Is that enough, or do you want a tape-style `bend` option as well?
- [ ] **EYES on the panel:** the delay node shows `division`, a `feel` combo (straight / dotted / triplet) and `spread`, plus the new `clock` input and the `out_l` / `out_r` jacks. `spread` only does anything while `out_l`/`out_r` are cabled; turn it from 1 to 0 and the bounce should fold into the middle.

*After the backend split (2026-09-28) — EYES more than EARS.*
Every offline render is bit-identical before and after the split (165/165),
so there's nothing new to *hear*. What renders can't reach is the GUI side:
hooks the app calls on the backend that now live in mixins, plus the
dropped-cable report. Also run `python examples/irs/generate_irs.py` once.
Test the exe too (it's the windowed build).
- [ ] App starts, a patch loads and plays, Stop/Start works, DSP% and the stream-health readout tick over.
- [ ] `sampler_breaks.json` / `sampler_scrub.json`. The waveform overview appears on the sampler panel (`sampler_overview`). Scrub still scrubs.
- [ ] `file_crossover_split.json`: the file player plays, and seek / rewind move the playhead. A track end is noticed (`file_player_finished`). Point `path` at a missing file: the status line names it (`media_load_failures`).
- [ ] `scope_tap.json`: the scope draws (`scope_window`).
- [ ] `record_a_take.json`: the disk writer records, and the .wav plays back in another player.
- [ ] `mic_karaoke_recorder.json` (needs a mic): the input device opens (`_resolve_mic_input`).
- [ ] `convolver_reverb.json`: the IR loads and you hear the room.
- [ ] `keyboard_play.json` / `midi_lead.json`: notes play; in the MIDI one, the velocity-calibration dialog opens.
- [ ] **Dead-cable report.** Copy any example and hand-edit one cable's `dst_port` to `nope`, then open the copy. The status line reads "1 dead cable dropped (list in ...load_<time>_<name>.txt)". That file under `~/.pysynthrack/crashes/` names the cable and warns that saving drops it for good. In the exe this is the only place the list appears.

*The seventh batch (2026-10-12). Every example that changed.*
Audited like the sixth: every example rendered offline (8 s, block 512)
at the commit before the batch (df7b46c) and after it, and `render_audit
bisect` pinned each move to the commit that made it. **72 moved, 93 are
bit-identical (not listed), 1 is new.** Grouped by cause, loudest first;
the dB is the biggest difference relative to the example's own peak.
Anything that sounds wrong: one line here and Claude takes it from there.

**1. Genuinely different: the random LFO no longer skips a roll (2).**
The old render missed one sample-and-hold roll where a cycle ended
exactly on a block boundary, so every note after it played one value
late. Same sequence of values now, with nothing skipped (0b49cb9).
- [ ] `random_arp.json` (+6 dB)
- [ ] `sample_hold_arp.json` (+6 dB)

**2. Clock edges one sample EARLIER (38).** The clock's float phase
used to decide which side of an exact edge a sample landed on; it is an
integer count now (95f13ab). Every moved edge is exactly one sample
(about 23 µs) earlier, with no edge added or lost. The numbers are big
because an attack starts a sample sooner, but it should sound
**identical**. Flag any that don't.
- [ ] `sequencer_melody.json` (+6 dB)
- [ ] `chord_arp_factory.json` (+5 dB)
- [ ] `bitcrusher_lofi.json` (+4 dB)
- [ ] `delay_dub_echo.json` (+4 dB)
- [ ] `chorus_lush.json` (+2 dB)
- [ ] `resampler_tape_stop.json` (+2 dB)
- [ ] `autopan_pluck_bounce.json` (+1 dB)
- [ ] `clock_swing_breathe.json` (+1 dB)
- [ ] `cv_recorder_layers.json` (+1 dB)
- [ ] `sequencer_reverse_bars.json` (+1 dB)
- [ ] `clock_transport.json` (0 dB)
- [ ] `freeze_wide_wash.json` (0 dB)
- [ ] `granular_haze.json` (0 dB)
- [ ] `flanger_jet_sweep.json` (-3 dB)
- [ ] `granular_freeze.json` (-4 dB)
- [ ] `pluck_touch.json` (-4 dB)
- [ ] `distortion_drive.json` (-6 dB)
- [ ] `drum_machine.json` (-6 dB)
- [ ] `reverb_freeze_pad.json` (-6 dB)
- [ ] `granular_beat_repeat.json` (-7 dB)
- [ ] `freeze_chord_pad.json` (-8 dB)
- [ ] `matrix_feedback_echo.json` (-8 dB)
- [ ] `wind_duet.json` (-11 dB)
- [ ] `lfo_random_replay.json` (-19 dB)
- [ ] `fm_op_bell.json` (-21 dB)
- [ ] `wavetable_vowel_talk.json` (-21 dB)
- [ ] `fm_op_epiano.json` (-27 dB)
- [ ] `vowel_giant_child.json` (-31 dB)
- [ ] `vowel_robot_talk.json` (-31 dB)
- [ ] `vowel_talk.json` (-32 dB)
- [ ] `reverb_space.json` (-32 dB)
- [ ] `stereo_field_pluck.json` (-37 dB)
- [ ] `vinyl_dust.json` (-38 dB)
- [ ] `ring_mod_bells.json` (-39 dB)
- [ ] `adsr_velocity.json` (-41 dB)
- [ ] `flanger_jet.json` (-44 dB)
- [ ] `convolver_reverb.json` (-45 dB)
- [ ] `delay_freeze_stutter.json` (-58 dB)

**3. Random-LFO rolls one sample earlier (2).** Same values, the roll
lands on its exact period now (0b49cb9). Should sound identical.
- [ ] `krell_feedback.json` (-56 dB)
- [ ] `krell_machine.json` (-57 dB)

**4. Below hearing (30).** Expected identical; a quick play of one or
two per line is plenty.
- [ ] flanger/phaser `rate_cv` read per sample (35e64cb):
      `flanger_through_zero.json` (-74 dB), `phaser_sweep.json` (-97 dB).
- [ ] ADSR voices exact (f1e0fbd): `chord_legato_inversions.json`
      (-87 dB), `supersaw_chord_wall.json` (-92), `supersaw_detune_rise.json`
      (-97).
- [ ] pluck's float64 pitch mean (844aa92), -95 to -99 dB:
      `fg_eor_swell_strike`, `freeze_drone_breathe`, `granular_cloud`,
      `pluck_velocity`, `pluck_velocity_color`, `possibility_selector_kit`,
      `sequencer_pendulum`.
- [ ] Rounding only, -128 dB and quieter: the note sources' exact phase
      (572eee6): `cv_utility_demo`, `cvtofreq_bipolar_pendulum`,
      `file_crossover_split`, `mic_beatbox_crossover`,
      `mod_wheel_pitch_drone`, `police_siren`; the LFO's integer phase
      (0b49cb9): `ambient_bloom`, `bowed_cello`, `crossover_sweep`,
      `lfo_oscillator`, `mod_matrix`, `motion_eq_animated`,
      `motion_eq_breathe`, `ring_governor_auto`, `ring_governor_monitor`,
      `tilt_eq_seesaw`, `vibrato`, `warping_buffer_tape`.

**5. Played live, can't be heard offline.** Keyboard / MIDI voices now
use the oscillator's exact phase, and their envelopes count from each
slope (572eee6). A scripted performance over 28 of them measured
-138.5 dB at worst. ADSR voices start their decay 1-2 samples later
(f1e0fbd). Nothing to listen for, beyond the sixth batch's group 7.

**6. New** (see the hands-on checks): `scope_chaos_afterglow.json`.

*The sixth batch (2026-09-24). Every example that changed.*
Re-audited after the NaN fix: every example rendered offline (8 s, block
512) at the commit before the batch and at HEAD, plus a second pre-batch
render to confirm nothing is random run to run (nothing was). **80 moved,
84 are bit-identical (not listed — nothing to hear), 1 is new**, and 25
more can't be heard offline because they wait for your keys, MIDI or the
mic. The NaN fix moved none of them. Grouped loudest-first; the dB figure
is the biggest difference relative to the example's own peak. Anything that
sounds wrong: one line here and Claude takes it from there. (The audit is
`tools/render_audit.py`; its docstring has the whole session.)

**1. The new module**
- [ ] `autopan_pluck_bounce.json` — **module #101.** Alternate eighth-note plucks land left, then right, with no click at the flip; the reverb wash stays centred. The first note plays centred (before the clock lock). Try `shape` triangle / sine, `law` compromise, `tremolo` 1 (a mono tremolo). EYES: the autopan panel.

**2. Moved enough to hear — the oscillator's saw/square edges (17).** Naive
saw/square at integer-Hz pitches (110/220 Hz): the exact sample each
waveform edge lands on moved by one (about 22 µs), and the new one is the
correct one. The number is big because an edge is a jump of 1.0, but it
should sound **identical** — flag any that don't.
- [ ] `noise_gate_chop.json` (+6 dB)
- [ ] `meter_levels.json` (+5 dB)
- [ ] `mixer_crossfade_verb.json` (+5 dB)
- [ ] `parametric_eq_bass.json` (+1 dB)
- [ ] `tilt_eq_seesaw.json` (+1 dB)
- [ ] `transient_shaper_snap.json` (+1 dB)
- [ ] `limiter_brickwall.json` (0 dB)
- [ ] `phaser_sweep.json` (−1 dB) — the phaser's sweep is also exact across buffer sizes now.
- [ ] `meter_stereo_master.json` (−2 dB) — also has a chorus (`rate_cv` read per sample now).
- [ ] `motion_eq_focus.json` (−2 dB)
- [ ] `clock_swing_breathe.json` (−3 dB) — also a `clock_divider` (gates never merge now). Straight at 0 s, triplet at 10 s, straight at 20 s; the kick must not flutter.
- [ ] `sidechain_pump.json` (−3 dB)
- [ ] `distortion_drive.json` (−7 dB)
- [ ] `flanger_jet_sweep.json` (−7 dB) — also the flanger fix. **Play it at buffer 64:** the deepest sweep used to be clipped there and now reaches full depth.
- [ ] `flanger_through_zero.json` (−8 dB) — also the flanger fix.
- [ ] `freq_shifter_barberpole.json` (−10 dB)
- [ ] `fat_saw.json` (−17 dB)

**3. Moved enough to hear — pluck re-plucks (`carry` is on by default now) (11).**
A re-plucked string should now carry on under the new hit instead of
stepping. If one sounds worse, untick `carry` on that pluck node and say
which.
- [ ] `freeze_wide_wash.json` (−13 dB) — the loudest pluck mover, because the freeze holds the difference; freeze's quieter-partials change is in here too.
- [ ] `clock_transport.json` (−19 dB)
- [ ] `delay_freeze_stutter.json` (−23 dB) — the delay is also exact across buffer sizes now.
- [ ] `sequencer_pendulum.json` (−23 dB)
- [ ] `sequencer_reverse_bars.json` (−24 dB)
- [ ] `freeze_drone_breathe.json` (−33 dB) — the freeze's stereo image also reshuffled (which partials lean left vs right; same levels). Then put `size` on **65536** on `freeze_chord_pad.json`: the hold starts 93 ms after the gate (by design) — a longer, smoother moment?
- [ ] `pluck_velocity_color.json` (−35 dB)
- [ ] `possibility_selector_kit.json` (−36 dB)
- [ ] `pluck_velocity.json` (−37 dB)
- [ ] `reverb_freeze_pad.json` (−57 dB)

**4. The chorus**
- [ ] `chorus_lush.json` (−63 dB) — its `rate_cv` is read per sample now: the sweep's speed glides instead of stepping.

**5. Below hearing, but a quick play won't hurt (−88 to −95 dB) (3).** A
trace of the pluck change; should sound the same.
- [ ] `fg_eor_swell_strike.json` (−88 dB)
- [ ] `drift_wander.json` (−93 dB)

**6. Rounding only (−131 dB and quieter) (48).** Differences of about one
part in ten million — float rounding from the exact-phase and float64
voice-sum fixes. These should sound **identical**; a few seconds each
confirms it. (Several have their own listening notes further down — those
still stand.)
- [ ] `chord_arp_factory.json`
- [ ] `freeze_chord_pad.json`
- [ ] `shift_random_melody.json`
- [ ] `adsr_velocity.json`
- [ ] `organ_leslie.json` — also move the buffer slider while it plays: the rotor no longer resets.
- [ ] `lfo_retrigger.json`
- [ ] `sample_hold_prob_sweep.json`
- [ ] `stereo_field_pluck.json`
- [ ] `supersaw_detune_rise.json`
- [ ] `cv_math_delayed_vibrato.json`
- [ ] `delay_dub_echo.json`
- [ ] `krell_feedback.json`
- [ ] `reverb_space.json`
- [ ] `chaos_melody.json`
- [ ] `lfo_random_replay.json`
- [ ] `organ_scanner.json`
- [ ] `oscillator_pwm.json`
- [ ] `sample_hold_sometimes.json`
- [ ] `supersaw_chord_wall.json`
- [ ] `tape_cassette.json`
- [ ] `tape_stop_drop.json`
- [ ] `vibrato.json`
- [ ] `krell_machine.json`
- [ ] `vinyl_dust.json`
- [ ] `loudness_demo.json`
- [ ] `mod_matrix.json`
- [ ] `sequencer_melody.json`
- [ ] `phaser_envelope_sweep.json`
- [ ] `ambient_bloom.json`
- [ ] `ring_governor_auto.json`
- [ ] `ring_governor_monitor.json`
- [ ] `warping_buffer_tape.json`
- [ ] `mid_side_breathe.json`
- [ ] `flanger_jet.json`
- [ ] `pitch_shifter_harmony.json`
- [ ] `stereo_hard_pan.json`
- [ ] `filter_resonance_sweep.json`
- [ ] `key_trigger_latch_brake.json`
- [ ] `schmitt_lfo_clock.json`
- [ ] `resampler_tape_wobble.json`
- [ ] `crossover_sweep.json`
- [ ] `hello_sine.json`
- [ ] `ring_mod_bells.json`
- [ ] `saw_blep_vs_naive.json`
- [ ] `ad_kick.json`

**7. Can't be heard offline — they need your hands (25).** Each waits for a
key, MIDI or the mic, and each contains a module the batch touched. The
likeliest to sound different come first.
- [ ] `pluck_strings.json` — pluck `carry` is on now: re-pluck the same key fast and the string should carry on under the new hit.
- [ ] `organ_jazz.json` — has a chorus (`rate_cv` read per sample).
- [ ] Keyboard / MIDI voices — the oscillator's exact phase, and if the wave is set to a wavetable (`_wt`), its harmonic band is now chosen per sample. Should sound the same: `keyboard_play.json`, `keyboard_adsr.json`, `keyboard_filtered.json`, `keyboard_tremolo.json`, `filter_envelope.json`, `dual_mod_filter.json`, `envelope_follower_wah.json`, `fan_out.json`, `two_way_crossover.json`, `wah.json`, `record_a_take.json`, `cvtofreq_blip.json`, `midi_simple.json`, `midi_lead.json`, `mod_wheel_filter.json`, `pitch_bend.json`, `aftertouch_filter.json`, `poor_mans_theremin.json`.
- [ ] Oscillators behind a key or the mic — exact phase, should sound the same: `cv_gates_amp.json`, `cv_keyboard_external_voice.json`, `octaver_bass_lead.json`, `slew_portamento.json`, `vocoder_robot_choir.json`.

**8. Behaviour checks (no example file)**
- [ ] `clock_divider` — try `pw` 0.9 + `swing` 0.3 on `divn` into an ADSR: every gate should be its own note now.
- [ ] EYES: a CV meter on a broken (NaN) cable reads `nan` and holds its bar, and recovers when the cable does.
- [ ] EYES: the NaN guard at the speakers (new today). Oscillator into a mixer's `in1`, the mixer's `out` back into its own `in2` with `gain2` at 2, out to a speaker, and play: the speakers stay clean and the status bar says "Output: N block(s) carried NaN/inf and were silenced...".

*The new modules (seven):*
- [ ] `freeze_chord_pad.json` — **module #100.** Each sus2 chord plays a second, then hangs as a glassy pad for the rest of the bar and melts into the next. Is it a pad or a buzz? Try `smear` 1 (the wash), `pitch` -12 (a sub-pad under the organ), `size` 16384 (a longer, smoother moment), and the `freeze` tickbox under a held keyboard chord. EYES: the `size (fft)` combo, the `freeze (or gate)` tickbox.
- [ ] `bowed_cello.json` — does it read as a bowed string? Try `pressure` 0.8 (crunch), `position` 0.06 (sul ponticello), `body` 0 with the scope (the raw sawtooth). The 88% gates should re-bow without a gap.
- [ ] `wind_duet.json` — flute over reed. Flute `breath` 0.8 should go sharp and airy; reed `breath` 0.05 must NOT speak and 1.0 should choke — both on purpose. Is the flute a flute?
- [ ] `drift_wander.json` — the filter breathes with no corners (the scope shows the wander); the pluck lands exactly when the wander turns; the drone's 36-cent walk reads as an old oscillator, not vibrato. Try `glide` 0 (pure S&H) and `walk` with `step` 0.05.
- [ ] `cv_math_delayed_vibrato.json` — no wobble at each note's attack, full at the sustain (scope on the `mult` jack). Move the filter's cable from `max` to `min` / `avg` / `diff`.
- [ ] `cv_recorder_layers.json` — the loop that moves the filter changes every OTHER bar and never piles up. Then the real test: unpatch `in`, cable a `constant` 1.0 into `rec`, and turn the `value` slider — your gesture should come back every bar.
- [ ] `vowel_talk.json` — the talking pad: does the supersaw say A-E-I-O-U as the LFO sweeps? Try `voice` soprano vs bass, `resonance` 2.5 (more vowel), and a noise source instead of the supersaw (a whisper). EYES: the `vowel (A E I O U)` slider and the `voice` combo.
- [ ] `possibility_selector_kit.json` — whether x which drums + the harp. EYES: cells paint one hue per output (red / blue / green / purple), amber for `?` and subsets; right-click a cell opens FOUR checkboxes (never seen in a window yet).
- [ ] `possibility_reroll_divider.json` — kick and snare hold a take for four bars then re-decide on bar 5; the hat re-deals every bar.

*The love passes (thirty-one):*
- [ ] `noise_stereo_pair.json` — two brown tides, one per side (corr 0.09). Set **both seeds the same** and it should collapse to dead centre. Then sweep the new `corner` knob live on a brown: 2 Hz (distant thunder) to 40 Hz (tight wind) — it should not click.
- [ ] `vowel_robot_talk.json` — the 25 Hz buzz: does the audio-rate mouth read as a robot or as noise? EYES: the `cv_rate` combo and the five `f1..f5` Hz drags under `voice` custom.
- [ ] `clock_swing_breathe.json` — straight at 0 s, full triplet at 10 s, straight at 20 s, forever, over a kick that must NOT flutter (that's the divider fix). EYES: the `swing_cv_depth` drag.
- [ ] `freeze_drone_breathe.json` — a 743 ms capture (`size` 32768) with `latch` on: one trigger holds it, the next releases. Does the longer window read as "an average of a phrase"? EYES: `latch`, `width_cv_depth`, the extended `size` combo.
- [ ] `cv_recorder_oneshot.json` — a gesture fired as a one-shot at a different rate each time. Flip `speed` and `play_mode` live. EYES: the `play_mode` combo, `speed_cv_depth`.
- [ ] `pluck_touch.json` — soft notes dull AND round, accents bright and plucky. **Then the decision: untick `carry` and A/B a re-pluck** — with it on, a hit no longer interrupts the ringing string.
- [ ] `phaser_envelope_sweep.json` — the envelope phaser (every note drags the notches up 2.5 octaves). `flanger_jet.json` — through-zero at `spread` 1, one sweep every two bars. EYES: `division` / `spread` / `manual_depth` on both nodes.
- [ ] **A-B the two chaotic ones** — `bowed_cello.json` and `wind_duet.json` render differently after the CV sweep (exactly, where they were block-dependent before). Do they still sound right? If yes, nothing to do; if not, say so and I'll revert those two sites.
- [ ] `vowel_giant_child.json` — giant → child over 25 s while the mouth talks; does constant-Q read as "the same vowel, a different throat"? EYES: the `formant` drag + `formant_cv_depth` in the vowel block.
- [ ] `pluck_velocity_color.json` — the accents ring bright, the soft picks thud; A/B by flipping `vel_color` 0.8 → 0. Then `midi_input.velocity_cv -> pluck.vel` live.
- [ ] `lfo_random_replay.json` — the same 12-step random wah every 2 s bar: does the written-down accident read musically? EYES: `seed` on the lfo node — SEEN 2026-09-20 in a screenshot of Matthew's LFO-into-LFO siren patch (two lfo nodes with `reset` jack, `phase` `0.00 cyc`, `seed` 0; the oscillator's `pw_cv` jack, `pulse_width` and `pw_cv_depth` `0.50 width/unit`): every widget paints, ASCII, bounded. The live-drag checks (phase re-anchoring on a running LFO) and all ears items remain.
- [ ] `sample_hold_prob_sweep.json` — the melody frees up around 5 s and gets stuck around 15 s, every 20 s. EYES: the `prob_cv_depth` drag and the new jack.
- [ ] `clock_swing.json` — a 0.3 shuffle on the hats against a dead-straight `div4` kick; does it swing? EYES: the `swing (0.33 = triplet)` slider.
- [ ] `sequencer_reverse_bars.json` — the palindrome: bar 1 up, bar 2 its mirror. Try `direction` pendulum on it (the gate turns it around at the bar lines). EYES: the third jack on both sequencer panels.
- [ ] `freeze_wide_wash.json` — each phrase blooms into a wide wash that dies in six seconds; `width` 0 vs 0.8 on the chord pad — wide, or just louder? EYES: the two new jacks, `width`, `decay (0 = forever)`.
- [ ] `cv_recorder_backwards.json` — the wobble freezes on each downbeat, alternate bars run backwards; flip `speed` to `2x` and `0.5x` live. EYES: `reverse (or gate)` + `speed (play; rec is 1x)`; a live checkbox flip mid-loop turns the loop around with no jump.
- [ ] EYES only: the `freeze (or gate)` tickbox on the reverb and delay panels — tick with nothing patched, the pad / stutter hangs; untick, it releases.
- [ ] `noise_brown_surf.json` — brown noise as surf: does the 10 Hz leak read as waves, or does it want a `corner` knob? Switch `color` to violet (hiss) and set two seeds the same on a stereo pair. EYES: the four-item `color` combo + `seed` on the noise node; pluck's `color` slider unchanged.
- [ ] `sample_hold_sometimes.json` — a random line that repeats notes (prob 0.6); the 50 ms glide is a zip through the scale — should it sit AFTER the quantizer instead? Flip `mode` to track with a long-pulse clock. EYES: the `mode` combo shows sample/track (not the filter's modes).
- [ ] `clock_transport.json` — two bars on, one held, the A-B-C-A figure snapping to the bar line; is the ±2% breath gentle or a stumble (0.02 if so)? EYES: three new jacks on every clock node in 57 patches — panels still look right?
- [ ] `organ_scanner.json` — the scanner on c3: is it a Hammond? Flip v1 / v3 / c1 live under a held chord — the 40 ms crossfade should be invisible. EYES: the `vibrato` combo.
- [ ] `tape_stop_drop.json` — dive / halt / spin-up every 8 s. Is linear-in-speed the right coast, or does a slow-then-fast feel more like a deck? At `mix` 0.5 the doubling after a stop is the documented physics.
- [ ] `lfo_retrigger.json` — every note opens at the top of its tremolo (unpatch `sequencer.gate -> lfo.reset` to hear it go back to drifting). EYES: drag the `phase` slider on a running LFO — it re-anchors live.
- [ ] `filter_resonance_sweep.json` — the peak breathes on its own cycle whatever the cutoff does. EYES: `res_cv_depth` reads "dbl/unit" beside `cv_depth`'s "oct/unit".
- [ ] `oscillator_pwm.json` — the PWM pad. No pumping at the width extremes (DC-compensated), no zipper under the faster LFO. EYES: `pulse_width` slider + `pw_cv_depth` on the oscillator panel.
- [ ] `adsr_velocity.json` — accents loud-loud-mid-soft-soft-soft-mid-loud, hard notes brighter too. Then the live one: `midi_input.velocity_cv -> adsr.vel` on the real keyboard.
- [ ] `reverb_freeze_pad.json` — the Cmaj7 strum hangs as a pad between strikes. Is the 10 ms catch fast enough; is the bypassed-damping pad too bright?
- [ ] `sequencer_pendulum.json` — the pendulum line on eighths against a `random` hat bar that REPLAYS every reset (its step pitches are the accents). EYES: the `direction` combo + `seed` on the sequencer node and above the fader bank on `fader_seq`.
- [ ] `delay_freeze_stutter.json` — the 187.5 ms dotted-sixteenth hold reads as a tumbling beat-repeat, the phrase over it clearly dry. Freeze a sustained tone whose period doesn't divide the delay: the lap seam should be a smooth wobble, not a tick.
- [ ] `supersaw_detune_rise.json` — the riser: does the exponential rise open late enough; does the filter's 2.8-octave sweep want more or less? EYES: `detune_cv_depth` is bipolar (-2..2 "det/unit") so a negative depth is a falling riser.
- [ ] `mid_side_bass_mono.json` — toggle `side_hp` 120 vs 0 live: the sub should snap to the middle and the pad should NOT narrow. (`mid_side_breathe.json` is bit-exact unchanged.)
- [ ] `pluck_velocity.json` — do the accents breathe; is 0.3 too quiet a floor? Then `midi_input.velocity_cv -> pluck.vel` on the keyboard.

*Still owed from earlier sessions:*
- [ ] `fg_eor_swell_strike.json` — swell then strike, one gesture? Is the 12 s starter poke audible?
- [ ] `krell_feedback.json` + `envelope_follower_wah.json` — the real krell self-patch and the self-wah (alive since the door). EYES: the closing cable paints AMBER and thicker, the toolbar reads `loops 1`, drawing a loop live puts "Loop closed: ..." in the status bar.
- [ ] **2026-09-16, slice 3:** `granular_freeze.json` — play, **tap F**,
      the last 2 s hang while the LFO scans them; tap again. Does the
      freeze edge click (grains at the head hold their last sample)?
      And `granular_beat_repeat.json` — 2 s recording / 2 s held and
      re-cut at sixteenths; is `mix` 0.6 the right balance, or is the
      `mix` 1 "only the repeats" version the one? (The rest of the
      2026-09-11 bank was heard 2026-10-05.)
- [ ] **Matthew's question, 2026-10-05, on `granular_haze.json`:** "same
      as granular haze but different samples?" (written beside the file;
      probably meant `granular_cloud`, the entry before it). Not answered
      yet: say which, and Claude takes it from there.
- [ ] EYES: `modal_mallets.json` — the xy scope should draw a cloud when
      `spread` is up, not a line. (Heard 2026-10-05.)
- [ ] Sampler EYES: the `mode` dropdown (one_shot / gated / loop —
      `gated` was unreachable until 08-30), the waveform face, and the
      `reverse` / `antialias` tickboxes; then
      `midi_input.velocity_cv -> sampler.vel` on the keyboard.
      (`sampler_mellotron` and `sampler_scrub` heard 2026-10-05.)

*Hands-on checks outside the examples:*
- [ ] **HANDS + EYES: the buffered sink's fallback** (new 2026-10-12)
      — `ring_governor_monitor.json`, `buffer_size` 8192, Start: if the
      Realtek refuses 8192 the node's second line reads `device block
      N - 8192 would not open` and the status bar says so once; back
      to 1024 while running and the line goes. Save a copy with
      `"device": "NoSuchDevice"`: the node should read `buffer:
      could not open the device` in red while the rest plays. Later,
      the same at 8192 on the HDMI display.
- [ ] **EYES: scope afterglow** (new 2026-10-12) —
      `scope_chaos_afterglow.json` (silent on purpose), Start: the
      Lorenz butterfly, both wings, glowing. Untick `afterglow` (snaps
      to one sparse trace), tick again; drag `glow_frames` 2..16; try
      `mode` mono and dual; the DSP / xrun readouts shouldn't move.
- [ ] **Does the GUI-lockup audio freeze still happen?** (2026-10-12)
      The scope's repaint took ~100 ms per trace per frame on a slow
      sweep (mono/dual at 500 ms/div), holding the GIL the audio
      callback needs; it's ~0.1 ms now (20e123a). If a scope was open
      when the freezes happened, this was a suspect. Worth noticing
      before the off-thread planning session.
- [ ] **HANDS + EYES: `disk_writer`** (new 2026-10-10) — load
      `record_a_take.json`, play, Stop: the status bar should read
      `Recording -> ...Music\PySynthRack\take_01.wav` and the file
      should be there. Then click **Browse...** (opens in that folder,
      `.wav` only), type a new name, and tick **timestamp (new file per
      take)**: each Start should leave its own dated file. Your old
      `recordingleft/right.wav` and `take_01.wav` in the repo root are
      from the old cwd rule. Keep or delete them, your call.
- [ ] **Governor on a second clock** (meatthread0) — re-run
      `ring_governor_monitor.json` (ships at -4) with the buffered
      out's `device` set to a card OTHER than the main Realtek (the
      "HD Audio" device the warping-sink eyeball used on 2026-07-18,
      often a monitor's HDMI audio). Does fill still hold around half
      over a few minutes, and does -4 stay steady when the clocks
      really drift? *Split 2026-10-10 from the governor eyeball, which
      passed on the shared-clock case (archived).* Matthew has no second
      card right now; the plan is an **HDMI display** (its audio shows
      up as its own device: hit Refresh on the node). A display with no
      speakers still answers the question through the `fill` readout.
- [ ] **Stream-health reading under load** (meatthread0, pending since
      2026-07-20) — run it under load (load some Chrome tabs) and report
      `DSP%` / `xrun` / `api` together; that reading decides between
      "Off-thread audio rendering" and "Host-API / device selection"
      under Later / wishlist. *Hoisted 2026-10-10 from the archived
      "Stream-health readout" entry.*
- [ ] **EYES: the app icon** — the taskbar / title-bar icon in a real
      Windows build. *Hoisted 2026-10-10 from the archived "App icon"
      entry (2026-09-25).*

## Open defects

*Block-size exactness (the recurring defect class):*
- [ ] **`delay_freeze_stutter.json` is not exact across block sizes**
      (found 2026-11-11 while verifying the delay build). On main
      (e61daa4), 651,264 of 705,536 stereo samples differ between block
      100 and block 512, max 0.061, over 8 s. The listening note above says
      the delay itself is exact now, and the delay examples without a freeze
      gate match bit for bit, so the cause is in the chain feeding it: two
      `clock`s, `sequencer`, `pluck`, and `logic` `not_a` into the delay's
      `freeze`. Not yet located. Bisect by module (render sub-patches at
      100/512), then fix with the usual recipe. A fix will move this
      example's render. Compare the overlapping frames of `master`
      (`render_audit compare` reports inf when lengths differ).
      *Retested 2026-11-11 on the channel-timing scope's commit (9100b6b),
      as Matthew asked:* unchanged, 651,264 differ, max 0.0606. The scope
      is docs only, and the drift it found is the buffered sink's per-side
      stretch engines, not the clock, so that work won't fix this one.
      *Narrowed the same day* (variants rendered at 100 vs 512):
      - **Not the clock's fractional period.** Clock 1 on `division` 2
        (a whole 11025 samples) still differs, by 595,606 samples, max 0.010.
      - **Not the freeze gate.** With the freeze cable removed, 651,260
        still differ.
      - **Not the delay.** With delay `mix` 0 (dry pluck only), a
        whole-number clock and no freeze, 418,740 still differ, max 0.021.
      - **So it's the clock → sequencer → pluck (`pitch_cv` + `trigger`)
        chain.** Yet `pluck_*` and `sequencer_*` examples are exact on
        their own, so look at what this patch does that those don't.
      - **The class is wider.** Of the 19 examples that use a pluck, 12
        differ at 100 vs 512: `fg_eor_swell_strike` (max 0.56),
        `pitch_shifter_shimmer` (0.31), `freeze_wide_wash` (0.13), the three
        granulars, `reverb_freeze_pad`, `freeze_drone_breathe`,
        `drift_wander`, plus two at about 1e-6. Many go through renderers
        already listed in the block-size memory. **Start the fix with a
        100-vs-512 sweep of every example**, rather than this one alone.
- [ ] **No epoch wrap on the no-CV oscillator/organ phase** — precision
      erodes to ~−116 dB after 24 h of running. Low priority.
      *Plain English (2026-10-10, for Matthew):* with no CV patched, the
      oscillator's phase is "start + step × samples since the frequency
      last changed" (the organ's scanner counts from Start). That count
      just keeps growing. Nothing ends, overflows or reverses. But a
      float64 has a fixed number of digits, so the bigger the count, the
      fewer are left for the fraction the waveform actually uses. The
      result is a faint noise floor that rises slowly, about −116 dB
      after a day held unchanged, which reaches 16-bit's −96 dB after
      roughly ten days. Touching the frequency resets the count. The fix
      (the CV path already does it, `_OSC_EPOCH`) is to fold the count
      back every 65,536 samples, like a clock hand going past 12; the
      wave continues seamlessly. Cheap, but it touches the oscillator's
      fast path, so it needs the reference-render recipe. Matthew: "we
      might even keep this". Keep it low priority.
      *2026-10-12:* the no-CV phase now lives in `_osc_mono_phase`,
      shared by the oscillator, cv_to_frequency, keyboard and
      midi_input, so one fix covers all four. The clock, LFO and
      chorus counts (integer since the seventh batch) also grow
      without bound; harmless at LFO rates.

*Examples:*
- [ ] **Example health (pre-existing, from the audit):** `organ_leslie`,
      `noise_hat` and `clock_divider_swing` hit full scale in their
      first 8 s; `resampler_tape_stop` carries a 0.083 DC offset.
      *2026-10-12 audit:* `resampler_tape_stop`'s DC reads 0.101 now
      (the clock fix moved its edges a sample); the full-scale three
      are unchanged.

*UI:*
- [ ] **Zoom feeds back through the widgets it scales** — reported by Matthew
      2026-10-18, in his words: "when you adjust the zoom you adjust the widget
      size and that then adjusts the zoom again". `_apply_zoom` works by
      `set_global_font_scale`, which resizes *every* widget — the toolbar's
      zoom slider and the canvas included — so the control you zoom with
      changes size under the pointer. Hypotheses, in the order to check with
      eyes on the window: (a) the slider is rescaled mid-drag, so the same
      pointer position maps to a new percent and the drag re-fires;
      (b) Ctrl+wheel over the canvas — after one step a grown widget lands
      under the pointer and the next notch is read as a param wheel, or the
      other way round; (c) something on the resize path. Fix shape: keep the
      toolbar at a fixed font (bind it its own font, or scale only the node
      editor's font), and/or apply the slider on release; add a re-entrancy
      guard either way. The midiseq grid takes the lesson as a rule: zoom
      scales the drawing, never the widgets.

*Audio I/O:*
- [ ] **Left and right drift apart on a governed buffered speaker under
      load** (scoped 2026-11-11 on Fable 5.2: `docs/channel-timing-scope.md`
      on `claude/channel-timing-scope-afe939`, 9100b6b, not merged). The
      governor stretches each side with its own WSOLA engine
      (`numpy_backend.py`, the `_sink_stretch` pair; the supervisor checked
      the code), and under load the two sides splice at different points.
      They can stay up to 25 ms apart afterwards. Measured offline and on
      real streams on the laptop; not yet run on Matthew's desktop.
      **Workaround today:** untick `auto_govern` or use the warping sink;
      re-ticking `auto_govern` pulls drifted sides back. **Recommended fix:**
      one splice decision for both sides (S, one session). The doc has five
      questions for Matthew and five desktop checks; the first check takes
      three minutes and confirms the cause.
- [ ] **Found during that scope, not reproduced by the supervisor:**
      (a) 3 of 12 device names in the sink's dropdown won't open on the
      laptop ("Multiple output devices found"); (b) `disk_writer` drops
      blocks without saying so, so a left file and a right file can part
      under load (read in the code, not reproduced); (c) the governed
      path's latency measures 93-112 ms, not the documented 50 ms.

## Decisions waiting on Matthew

Calls, not bugs: each waits on a yes, a no or a which.

*From the seventh batch (2026-10-12):*
- [ ] **`adsr`: exact stage TIMES?** Both paths end a stage when the
      per-sample sum reaches its target, so a 20 ms attack can take
      883 samples, not 882 (exact and block-size-proof, just not an
      integer count). Counting stages by integer sample number (as
      `_gate_ramp_env` does) would make times exact but move EVERY
      ADSR example, mono included. A later pass, or leave it?
- [ ] **Buffered sink, Phase 2:** should `buffer_size` 2048-8192
      always mean a big ring with a modest device chunk, even when the
      device accepts the big chunk? (a) cap the device block at 1024
      (default untouched; less PortAudio-side buffering, which
      Bluetooth may want); (b) a separate `device_block` param (auto /
      64..8192, default = buffer_size); (c) leave Phase 1 as is. The
      agent leans (c) or (b).
- [ ] **The A440 edge flip:** keyboard / MIDI / cv_to_frequency now
      land every 4410th sample exactly on the waveform's edge at
      440 Hz, so a plain (non-blep) saw or square flips a full step
      there. Same as the oscillator since 2026-09-24. Acceptable?
- [ ] **Clock: the first period is one sample short.** The clock
      reads samples 1..n, so 120 BPM quarters land at 22049, 44099...
      Reading 0..n-1 puts every edge on the period exactly, but moves
      every edge in all 78 clock examples one sample LATER. Do it, or
      leave it?
- [ ] **Scope afterglow:** linear fade (now) or a phosphor-style
      exponential one? Is a silent example right, and does
      `glow_frames` counting the live trace read naturally (default 8,
      the example uses 16)?

- [ ] **EARS DECISION: `_wt` band follows the HIGHEST voice**, so a low
      voice in a chord loses harmonics. Per-voice bands are more correct
      but change how steady poly `_wt` patches sound.
- [ ] **Question, not a defect: loop-mode voice slots that were never
      triggered stay parked** (noticed 2026-09-14). The voice path's
      idle-skip runs before the kernel's "kick out of idle", so a poly
      keyboard into a `loop`-mode fgen only loops the slots that have
      been played once; the mono path free-runs unpatched. Defensible
      (sixteen free-running kernels cost the full ~35% whether or not
      anyone is playing). Decide whether that asymmetry should be
      documented as-is or changed; do not change it silently.
- [ ] **`rotary` `fast` switch lands on a block boundary** (block-majority
      read). A vote over fixed 64-sample windows would make it exact at
      ~1.3 ms latency. A call, not a bug.
- [ ] **`freeze`: stage 32768 too** (a 2048-sample delay, worst block
      ~25% from 46%) — shifts `freeze_drone_breathe`'s timing. Offered.
- [ ] **`lfo` `seed`: a `phase` knob move counts as a reset and so
      re-seeds** a seeded `random` LFO — decide whether that is wanted.
      *Hoisted 2026-10-10 from the archived "`lfo` — `seed`" entry
      (2026-09-20).*
- [ ] **Backend preference: pyo still wins when installed.**
      `audio/__init__.py` `_PREFERENCE = ("pyo", "numpy")`, so anyone who
      takes the README's optional "also try pyo" install step gets a GUI
      where most of the 101 modules are silent stubs. The README does say
      so ("The app selects `pyo` when it's installed"), but it also calls
      numpy the default and pyo is parked. Flip to numpy-first (pyo by
      `PYSYNTHRACK_BACKEND=pyo` only), or keep it as is? *Surfaced
      2026-10-10 by the MODULE_IDEAS realignment.*

## Plan together first

Matthew wants to scope and plan these with Claude in a planning
session before anything is built. Don't build ahead.

- [ ] **Off-thread audio rendering + host-API selection** (both below
      under Later / wishlist, with their 2026-07-20 scoping). Matthew,
      2026-10-10: "my desktop system has gui lockups that mean unless
      rendering off thread it freezes the output". That's a stronger
      reason than the old "only if the readout shows underflows". For
      the session: what kind of lockup is it? A thread inside the same
      process still needs Python's GIL, so if the lockup is Python code
      holding it, a render thread starves too. It rides a lockup out only
      as far as its ring is deep, and a separate audio process is the
      full fix. If the lockup is native (window messages, the DPG frame),
      a render thread plus a deep ring may be enough. The stream-health
      reading under "Hands-on checks" is the first measurement to bring.
      *2026-10-12:* one suspect found and fixed on the way -- the
      scope's repaint held the GIL ~100 ms per trace per frame on a
      slow sweep (20e123a). See the hands-on check.
- [ ] **Subpatch containers** (L) — group modules into a reusable
      macro-module with exposed ports. Off the module menu 2026-10-10;
      Matthew: "can take or leave this, again a planning scope and plan
      if we take it". Spec one-liner in docs/MODULE_IDEAS.md § "The
      2026-08-04 brainstorm".

## Follow-ons and small items

*From the seventh batch (2026-10-12):*
- [ ] **Sink open ladder runs on every reconcile** while a device is
      broken (up to 7 attempts per graph edit while running): could
      stutter the GUI. Retry only when that sink's device or
      buffer_size changes, or on Start.
- [ ] **More state that resets on a block-size change:** `tape`
      (the whole tape incl. the stop transport and its RNGs; also on
      unpatching `stop` / raising `stop_time`; the ring length is
      pinned in a tape test), `bowed` / `wind` (string/bore cleared),
      `granular` (grains in flight dropped), `convolver` (tail lost).
      `_ring_regrow` is the pattern. Tape is the strongest candidate.
- [ ] **`autopan` `rate_cv` is still a block average** — the shared
      `_mod_rate_cv_phase` applies.
- [ ] **`ring_mod`'s internal carrier re-wraps its phase per block**
      (`_render_ring_mod`, colour.py) — `_osc_carried_phase` fixes it.
- [ ] **LFO period grid:** `k * rate/sr` is block-size exact but not
      always ON the grid at whole-sample periods; the clock's integer
      form would put it there. Minor.
- [ ] **pluck's silence gate is per block**, so at block 64 a block
      shorter than a pitch period can cut the tail early (~-92 dBFS,
      inaudible). Judge silence over >= one period, or by energy.
- [ ] **MODULES.md: 10 `_To document._` stubs** — keyboard,
      cv_to_frequency, vca, cv_to_audio, schmitt, combiner,
      cv_combiner, speaker_output, left/right_speaker_output. And the
      `sequencer` `reverse` note still calls same-sample edges "a race
      between two float phases" (deterministic since the clock fix).
      A docs agent's job.
- [ ] **`render_audit` could verify each file it writes** (one corrupt
      .npz seen once under eight agents' memory pressure).

- [ ] **`autopan` follow-ons:** smooth-random shape, a clocked phase
      offset, a separate tremolo rate, `width` for a stereo pair, a
      `pan_cv` depth knob.
- [ ] **Loader follow-on:** the editor could OFFER TO REPAIR a dropped
      cable (nearest-name port match) rather than only naming it.
- [ ] **`cv_recorder`: save the loop in the patch** — today it lives in
      backend state and is lost on Stop, the honest gap for a
      *performance* capture. *Hoisted 2026-10-10 from the archived
      `cv_recorder` entries (2026-09-19 / 09-20), where it was the one
      follow-on still open.*

*Code health:*
- [ ] **Split `ui/app.py`** (7.4k lines, one `App` class) -- the custom
      panels (possibility, selector, sampler face, scope) first.
- [ ] **Optional: the module-level DSP kernels** (`_Oversampler4`,
      `_GrainShifter`, the drum hits, ...) could move to their own
      `audio/dsp/` module, but tests import them from `numpy_backend` by
      name, so that needs a re-export shim. *Hoisted 2026-10-10 from the
      finished backend-split plan (archived), where it was the one thing
      left.*

## Build queue — the keep-list and the ideas backlog

- [ ] **The 2026-08-04 brainstorm keep-list** — Matthew: "Can you add
      to the todo" (2026-08-04, with the full writeup pasted back).
      Twelve items, one-liners now living in docs/MODULE_IDEAS.md
      § "The 2026-08-04 brainstorm" — promote to a full spec when
      picked, the usual workflow. The menu:
      * Sources: ~~`bowed`/`wind`~~ (BOTH SHIPPED 2026-09-18, #94/#95 —
        see § "The physical-modeling family, sustained").
      * Modulation: `function_generator` — **SHIPPED 2026-08-23**, see
        § "The function generator" below,
        ~~`drift`~~ (SHIPPED 2026-09-19, #96 — see § "The keep-list,
        continued"), ~~`cv_math`~~ (SHIPPED 2026-09-19, #97, same §),
        `cv_recorder` (M — the modulation looper; nothing else
        captures performance).
      * Effects: ~~`rotary`~~ (SHIPPED 2026-09-14, module #91 — see below),
        `vowel` (S–M — formant filter bank, A–E–I–O–U morph),
        `freeze` (M — spectral freeze pad), ~~`autopan`~~ (SHIPPED
        2026-09-24, module #101 — see § "Nine follow-ons in parallel").
      * I/O: `midi_output` (M — gates/CV → notes/CCs; the rack as
        the brain of a hardware rig).
      * Endgame (architecture): **subpatch containers** (L — patches
        become modules; makes "running out" structurally impossible)
        and **snapshot morph** (M–L — knob scenes interpolated by
        one CV).
      *Status 2026-10-10: `cv_recorder` (#98), `vowel` (#99) and `freeze`
      (#100) have shipped as well, so what is left on this list is
      `midi_output`, subpatch containers and snapshot morph. The §
      references above now point into TODO-ARCHIVE.md.*
      *Matthew, 2026-10-10:* `midi_output` is wanted, "but only for midi
      based patches" (the hardware-rig case). Subpatch containers moved
      to § "Plan together first", snapshot morph to Later / wishlist.
      Neither is easy, and both are off the module menu.
- [ ] **Module ideas backlog** — see [docs/MODULE_IDEAS.md](docs/MODULE_IDEAS.md)
      (written 2026-07-04; realigned 2026-10-10, open items first). Open
      and spec'd: `pitch_detector` (M–L), `spectrum` (M). One-liners, spec
      when picked: the keep-list's three above, plus the quick hits
      `exciter`, `gate_delay`, `sequential_switch`, `macro`, `tuner`
      (needs `pitch_detector`), `looper` (L). The 47 shipped specs are in
      the doc's appendix. Pick items into this list as they're chosen.

## Later / wishlist

- [ ] **Off-thread audio rendering (render thread + ring buffer)** — SCOPED
      2026-07-20, gated on the readout above. *2026-10-10: wanted, and
      for a planning session with Matthew first; see § "Plan together
      first".* `_fill_output` currently renders
      the whole graph inside the PortAudio callback, so there is zero slack for
      a scheduling spike. Move `render_block_multi` onto a dedicated thread
      feeding a sample-counted ring; the callback becomes a memcpy.
      `_DeviceOutput` is already exactly this pattern (ring + thin callback +
      underrun/drop telemetry + live on-node readout) — same shape, producer
      and consumer swapped. Queue depth as a toolbar slider beside the buffer
      size; **depth is added latency**, so the two trade against each other and
      async lets you go *smaller* on the device block and spend the budget on
      depth instead, which is the better deal. Companions: raise thread
      priority (`AvSetMmThreadCharacteristicsW("Pro Audio")` +
      `SetThreadPriority` via ctypes — without it the render lands on an
      *unboosted* thread and could regress), and `sys.setswitchinterval(0.001)`
      so per-sample loops can't hold the GIL for 5 ms. Only worth building if
      the readout shows underflows **without** overloads. Moderate/medium-risk:
      touches the duplex mic path (`_input_block` is read synchronously inside
      the callback today) and every `_device_outputs` push.
- [ ] **Host-API / device selection for the main stream** — cheap follow-up.
      The main stream opens with no `device`, `latency` or host-API hint, so
      PortAudio takes the system default (typically MME on Windows). Offer a
      host-API picker (WASAPI, optionally `sd.WasapiSettings(exclusive=True)`)
      and a `latency` hint. Possibly a bigger win than the render rewrite for a
      fraction of the work — the new `api` readout says whether it's worth it.
- [ ] **Snapshot morph** (M–L) — save knob scenes, interpolate between
      them with one CV. Off the module menu 2026-10-10 (Matthew: "nah,
      unless easy to implement", and it isn't): a morph CV drives every
      numeric param of every module each block, through the single
      param-write door (`App._set_module_param`, tripwired). Combos and
      bools would snap at the midpoint. Kept here so the idea survives.

## Standing notes (not tasks)

Gotchas worth keeping in view while working here, not work to do.

- [ ] Gotcha for tests: `test_voice_collapse`'s tripwire reads the backend
      with `inspect.getsource`, so editing `numpy_backend.py` DURING a run
      fails it spuriously (line numbers shift). Not a bug; don't chase it.
- [ ] **Recipe note:** agent worktrees branch from the last PUSHED
      commit, not main's HEAD — push before a batch, or expect the
      agents' suite counts to run low and an import-line conflict.
