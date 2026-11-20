# An output device module: the ring as its own thing (design note, 2026-11-20)

Design only, for *Plan together first*. Nothing under `src/`, `tests/` or
`examples/` changed for it. Read in the code at 202b6f6 (the eleven
decisions series). The decisions at the end are Matthew's.

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

## Build sketch (once decided)

Each slice ships on its own, with the house checks (existing patches bit
exact, the sink test files green, the render audit "moved 0").

1. The module, name binding (D1a), ownership rules (D2, D3), settings and
   readout. Speakers unchanged.
2. The governor moves onto it: `fill`, `ratio_cv`, `auto_govern`, the
   actuator (D5); the stretch engine keyed by the stream.
3. The buffered sinks' future (D4), the docs, and an example or two.

A guess, not a measurement: two to three sessions.
