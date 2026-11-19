"""DiskWriter — record an audio bus to a WAV file.

A sink module: audio in, nothing out, written to disk as a 16-bit WAV
at the backend's sample rate -- mono, or stereo with ``in_r`` patched.

**Stereo** (``in_r``, added 2026-11-19): patch a stereo source's left into
``in`` and its right into ``in_r`` and the take is ONE two-channel WAV,
fed through one queue and one worker. Two mono writers recording left and
right each drop blocks on their own when the disk stalls, so their files
can part by a whole block and never rejoin; one stereo writer drops a
block from both sides at once or not at all, so the two channels cannot
go out of step. The channel count is fixed for a take: patching or
pulling ``in_r`` mid-take starts a new take (with ``timestamp`` off that
overwrites the file, exactly as re-arming does). A missing side records
silence. The node shows the take (mono / stereo) and how many blocks it
has dropped -- 0 is a clean take.

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
during a take. Tick ``timestamp`` to give every take its own file
instead. The status bar names each take's full path as it opens.

Parameters:
    path: Filename for the recording. A relative path is a name inside
        the recordings folder, ``<Music>/PySynthRack/`` (created on
        first use; sub-folders allowed); an absolute path is used as
        given. The node's Browse... button picks one. Stick ``.wav`` on
        the end yourself when typing — we don't second-guess. (Before
        2026-10-10 relative paths landed in the process working
        directory.) See ``audio/recordings.py``.
    armed: When False the writer ignores incoming audio and never
        opens a file. Lets you keep the module in the patch without
        a take being made every time you hit play.
    timestamp: When True each take (every Start, re-arm, path change,
        or ``in_r`` patched / pulled) writes a NEW file, ``<stem>_YYYY-MM-DD_HH-MM-SS.wav``,
        stamped when the take opens; a second take inside the same
        second gets ``_2``. Off (the default) keeps the one name and
        overwrites it, as before.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class DiskWriter(Module):
    """Record an audio bus to a WAV file."""

    TYPE = "disk_writer"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {"path": "recording.wav", "armed": True, "timestamp": False}
    INPUT_PORTS = [Port("in", "in", "audio"), Port("in_r", "in", "audio")]
    OUTPUT_PORTS: list = []  # sink — no audio output
