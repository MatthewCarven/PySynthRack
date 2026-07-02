"""DiskWriter — record an audio bus to a WAV file.

A sink module: audio in, nothing out, written to disk as a 16-bit
mono WAV at the backend's sample rate.

Threading model. Disk I/O can pause for tens of ms (filesystem cache
flush, antivirus scan, the OS deciding it'd like a coffee break) so we
can't write from the audio callback. Each DiskWriter owns a daemon
worker thread plus a bounded queue:

  audio callback ──► queue.put_nowait(block.copy()) ──► worker thread
                                                        opens file
                                                        writes blocks
                                                        closes on stop

The callback is non-blocking even when the queue is momentarily full —
in that (rare) case we drop the block and increment a counter. Losing
a few ms of recording is preferable to glitching the audible output.

Lifecycle. The renderer opens the file lazily the first time it sees
the writer in a compiled patch, and closes it when the transport stops
(``AudioBackend.stop()``). Re-arming creates a new file with the same
name — existing files are overwritten without prompting, matching the
unsentimental nature of a hobby synth and avoiding modal dialogs
during a take.

Parameters:
    path: Filename for the recording. Relative paths land in the
        process working directory; absolute paths are honored. Stick
        ``.wav`` on the end yourself — we don't second-guess.
    armed: When False the writer ignores incoming audio and never
        opens a file. Lets you keep the module in the patch without
        a take being made every time you hit play.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class DiskWriter(Module):
    """Record an audio bus to a WAV file."""

    TYPE = "disk_writer"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {"path": "recording.wav", "armed": True}
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list = []  # sink — no audio output
