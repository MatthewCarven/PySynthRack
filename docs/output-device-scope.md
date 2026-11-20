# An output device module: the ring as its own thing (design note, 2026-11-20)

Design only, for *Plan together first*. Nothing under `src/`, `tests/` or
`examples/` changed for it. Read in the code at 202b6f6 (the eleven
decisions series). The decisions at the end are Matthew's.

**Status:** decided 2026-11-20, 21:40 -- see *Decided* at the end, which
supersedes the leans in *Decisions for Matthew*. Not built.

Matthew, after `device_block` landed: "am I right in believing that the
buffer could in fact be a separate module from the speaker out ... after
it". Short answer: yes for everything you see and set, no for the ring
itself as an ordinary graph module. Why, and what that module would be,
below.

## How it works today

**Two clocks.** The graph renders once per block on the main stream's
callback, everything in one topological walk. Each named output device has
its own PortAudio stream (`_DeviceOutput`, numpy_backend.py) that pulls
audio on its own clock, at its own block size. The ring sits between the
two: the render pushes into it, the device's callback pops from it.

**The ring belongs to the device stream, not the speaker.** A routed sink
(`specific_stereo_speaker_output`, `buffered_specific_speaker_output`,
`warping_buffered_speaker_output`) owns no stream. `_stream_key` maps it to
`(device, block)` -- `(device, block, device_block)` when that is set apart
-- and every sink with the same key sums into one bus
(`device_blocks[key]` in `render_block_multi`) and one ring. The speaker
itself only applies gain, pan and width.

**The plain specific speaker already has a ring.** The same `_DeviceOutput`,
eight global blocks deep. It just has no controls, no readout and no
`fill` out. (Correcting my own chat answer: what a device module would give
it is the *controls and readout*, not a buffer.)

**The governor is per stream but configured per sink.** The stretch runs on
the summed device bus, but `_governed_ratio` reads each buffered sink's own
`ratio_cv` / `auto_govern`, and when two buffered sinks share a stream "the
last drained sink's ratio wins" (its own docstring says so). The WSOLA
engine's state is keyed by the winning sink's id (`_sink_stretch[mid]`)
while it processes the shared bus, so if the winner changes, the engine
starts over.

**Settings are copied per sink.** `buffer_size` and `device_block` live on
each buffered sink. Two buffered sinks on one device with different values
silently become two streams on that device; with the same values they share
a ring but each still carries its own copies.

**`fill` is one block late, on purpose.** Every buffered sink publishes the
previous block's ring fill before anything renders (`_sink_fill`), and
`_is_delayed_edge` keeps the `fill -> ... -> ratio_cv` loop out of the
sort. That is what lets a governor patch close without a feedback door.

## Why the ring cannot be a module in the middle of the graph

Everything in the graph runs on the render clock. A buffer between two
graph modules has the same clock on both sides, so there is nothing for it
to absorb: it would just be a delay. The ring exists because the device
pulls on a *different* clock, so it has to sit where the graph ends and the
device stream begins. "After the speaker" in the graph is exactly that
boundary. So the module can be the ring's *face* -- its settings, its
governor, its readout -- while the ring itself stays at the stream.

## The proposal: an `output_device` module

One module per physical output device (the name is a placeholder). It
would own:

* `device` -- which device, the same picker as today
* `buffer_size` and `device_block` -- the ring and the device chunk
* the governor -- `ratio_cv` in, `ratio_depth`, `auto_govern`, and an
  actuator choice: hold the pitch (today's WSOLA) or bend it (today's
  warping varispeed, with `brake_time` / `spinup_time`)
* `fill` out (still one block late) and the ring readout on its node,
  including tonight's `queued ... ms  dev block ...` line

The speakers shrink to gain, pan and width, and send their audio to the
device's bus.

**What it fixes.** One owner per ring: no last-wins governor, no silent
split into two streams, no duplicated settings. The plain specific speaker
gets the controls and readout. The stretch engine can be keyed by the
stream instead of by whichever sink rendered last.

**What does not change.** The ring, the fallback ladder, `device_block`,
the delayed `fill`, the master output and its block size, and every patch
without a buffered sink. Numpy only, like the routing.

## Decisions for Matthew

**D1. How does a speaker reach the device module?**
(a) *By name*: speakers keep their `device` combo; a device module that
names the same device takes over that stream's ring. No new cables, several
speakers per device as today. The routing is not visible as cables.
(b) *By cable*: the speaker gets stereo outs and the device module becomes
the terminal sink with `in_l` / `in_r`. The routing is visible, but an input
jack takes one cable, so several speakers into one device need a mixer
first (or the module grows several input pairs).
*Claude's lean:* (a) for the first build -- it changes nothing about how
speakers are patched today -- with (b) open later if cables feel more
natural to you.

**D2. Two device modules naming the same device?**
*Lean:* the first one owns it; the second reads "already owned by ..." in
red on its node and the status bar says so once. Never a silent split.

**D3. A speaker naming a device with no device module?**
*Lean:* exactly today's behaviour -- a default ring (eight global blocks,
or the buffered sink's own settings) with no readout. Nothing breaks when
the module is absent.

**D4. The buffered sinks in old patches** (three examples use them:
`ring_governor_auto`, `ring_governor_monitor`, `warping_buffer_tape`).
(a) Keep them as they are, alongside the new module -- two ways to do one
thing. (b) Keep loading them but label them "legacy" in the add menu.
(c) Have the loader convert each into a specific speaker plus a device
module, re-pointing `fill` / `ratio_cv` cables.
*Lean:* (b) first; (c) only if (b) feels cluttered.

**D5. One module with an actuator param, or two module types** like
today's buffered / warping pair?
*Lean:* one module, `actuator` = hold / bend, with `brake_time` /
`spinup_time` only shown for bend.

**D6. A device module for the master output too?** The master stream is
the clock everything else runs on, so it has no ring and nothing to govern.
It could still show the main block size and latency in ms.
*Lean:* out of scope for this build; a later readout if you want it.

## Decided (Matthew, 2026-11-20, 21:40)

His words, then what each means for the build.

**D1 -- by name, with the device's channels.** "d1 by name but with all the
channels the specific device offers if you can be honest otherwise its just
2 and drop the 2nd channel if no detection available and no second output".

* Speakers bind to a device module by its device name, as today.
* The module opens as many channels as the device offers, and each speaker
  bound to it picks where it lands: a pair (`1+2`, `3+4`, ...) or a single
  channel (its L and R folded to one).
* **Being honest about the count:** PortAudio reports
  `max_output_channels` for each *host-API entry* of a device, and the
  entries disagree. On Windows, MME usually says 2 even for a 7.1 card;
  WASAPI says whatever the card is set to in the Windows Sound settings.
  The module asks about the exact entry its stream will open, so the
  number it shows is the number that will open, and its node says which
  host API it came from. Getting more than two channels from a card that
  MME reports as stereo is a host-API choice, which ties this to the
  host-API selection item already under *Plan together first*.
* No answer from PortAudio (the query fails): 2 channels, as he said.
* The device reports exactly 1 output: the module opens 1 channel and
  every speaker on it folds L and R to mono.

**D2 -- refuse it at design time.** "yeah i mean if you want to reject with
message at design time i'm cool with that".

* Pointing a second ring owner at a device that already has one is
  refused when you make the change: the combo snaps back and the status
  bar names the owner ("'Cans' already belongs to output_device 3").
* "Ring owner" covers the legacy buffered sinks too (D4), so a legacy sink
  and a device module cannot fight over one ring either.
* A hand-edited patch that arrives with two owners cannot be refused at
  design time: the first keeps the device, the second loads with a load
  warning and reads "not bound: 'Cans' belongs to ..." on its node, so it
  says so rather than passing for idle.

**D3 -- no device module means exactly today.** "sweet change my other
answers if i violate this one". This is the rule the others answer to.

* A speaker whose device has no device module behaves bit for bit as now:
  a stereo stream, channels 1 and 2, the ring it has today.
* Checked against D1: channel choice exists only on a speaker bound to a
  device module; without one the speaker is today's 1+2 pair. The mono
  fold is the module's too, so a mono device with no module still does
  whatever it does today (most likely refuses a 2-channel stream and
  says "could not open") -- the module is the way to drive a mono device.
* Checked against D2: refusal only arises between two owners; a patch
  with no device module has none to refuse.
* Checked against D4: legacy buffered sinks keep their own ring and
  settings exactly as now when no device module names their device.
* No conflicts found, so no answer needed changing.

**D4 -- legacy label.** "yes please". The buffered and warping buffered
sinks keep loading and playing exactly as now, labelled "legacy" in the add
menu, with a line in their docs pointing at the device module.

**D5 -- one module.** "one". `actuator` = hold (WSOLA) or bend (varispeed),
with `brake_time` / `spinup_time` shown for bend only.

**D6 -- Claude's call** ("up to youa"): **not in this build.** The master
stream is the clock everything else runs on, so there is no ring to show or
govern, its block size is already the toolbar's control, and the DSP-load
readout already sits beside it. If its latency in ms would help, that is a
small toolbar readout of its own later, not a module.

## Build sketch (decided)

Each slice ships on its own, with the house checks (patches without a
device module bit exact -- D3 -- the sink test files green, the render
audit "moved 0").

1. **The module, stereo first.** Name binding, ownership with design-time
   refusal and the load-time fallback (D2), settings, readout. Speakers
   unchanged; the ring still two channels.
2. **Channels (D1).** The honest count for the exact entry and its host
   API on the node; an N-channel stream, ring and bus; the speaker's
   channel choice (pairs and singles); the mono device and its fold.
3. **The governor moves onto it (D5).** `fill`, `ratio_cv`,
   `auto_govern`, the actuator; the stretch keyed by the stream instead of
   the last sink; the hold actuator generalised from the stereo pair's
   shared splice decision to N channels, so every channel stays in step.
4. **Legacy and docs (D4).** The menu label, the docs, an example or two.

A guess, not a measurement: three to four sessions (the channels added one).
