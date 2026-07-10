"""FilePlayer — stream a WAV file into the patch as a stereo audio source.

A *source* module: no inputs, two audio outputs (``left`` / ``right``). The
active backend decodes the file on a *background thread* (resampling to the
engine's sample rate when the file's native rate differs) and playback
starts as soon as ~0.5 s is buffered — so pointing the player at the audio
track of a two-hour video never stalls the audio thread. Steady-state
playback is just an array slice with no per-block disk I/O; if the playhead
ever catches a still-running decode it pauses and resumes seamlessly.

Channel handling. A mono file is duplicated to both outputs; a stereo file
maps its two channels to ``left`` / ``right``; a file with more than two
channels keeps the first two. Wire a single channel straight into a
``Crossover`` (mono ``in``) to split one side into highs/lows, or fan both
into separate chains.

Why a player belongs in a modular synth: it turns any recording into a
*modulation source*. Run a drum loop through a Crossover, rectify the low
band with ``AudioToCV`` and you have a kick-driven envelope; rectify the
high band into a ``CVToFrequency`` and the hats steer a pitch — the kind of
audio-rate cross-patching that's the whole point of a code synth.

Parameters:
    path: Path to a ``.wav`` file. Relative paths resolve against the
        process working directory; absolute paths are honored. **WAV only**
        — convert other formats first. An empty, missing, or unreadable
        path renders silence rather than raising, so a saved patch always
        loads even if the audio file has moved.
    gain: Linear gain applied to both channels, in [0, 2].
    loop: When True the file repeats seamlessly while the transport runs
        (a block that straddles the loop point wraps cleanly). When False
        (the default) it plays once, then outputs silence until the
        transport is restarted or the node is re-armed.
    armed: When False the player outputs silence and parks its playhead at
        the start, so toggling it back on replays from the top. Lets you
        keep the module patched without it sounding on every take.
    playing: Tape-transport pause. When False the playhead holds exactly
        where it is and the outputs go silent; back to True resumes from
        the same spot. Driven by the node's Play / Stop buttons (Rewind
        seeks to 0:00 whether playing or paused, without touching this
        param). Contrast with ``armed``, which parks at the start.
    playlist: An ordered queue of file paths that play *after* the current
        ``path``. When a one-shot track (``loop`` False) reaches its end,
        the GUI pops the head of this list into ``path`` and it plays from
        the top — so the module works as a simple gapless playlist. Each
        track is removed as it starts, so the queue drains to empty and the
        player then falls silent (parked at the last track's end). A queued
        file that can't be decoded (missing/unreadable) is skipped straight
        to the next good one rather than stalling the list. A player started
        with an empty ``path`` but a non-empty queue kicks off the first
        queued track automatically; the node's **>>|** button skips to the
        next track by hand. The auto-advance is a GUI behaviour (see
        ``ui/app.py``); the renderer itself only ever sees a single ``path``
        change, exactly as if you had Browsed a new file. Ignored while
        ``loop`` is True (a looping track never ends).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class FilePlayer(Module):
    """Play a WAV file as a stereo audio source (one-shot or looping)."""

    TYPE = "file_player"
    CATEGORY = "Sources"
    DEFAULT_PARAMS = {
        "path": "",
        "gain": 1.0,
        "loop": False,
        "armed": True,
        "playing": True,
        # Ordered queue of paths that auto-advance into ``path`` as each
        # one-shot track ends (see the class docstring). A fresh list is
        # given to every instance by Module.__init__ (mutable-default copy).
        "playlist": [],
    }
    INPUT_PORTS: list[Port] = []  # source — no inputs
    OUTPUT_PORTS = [
        Port("left", "out", "audio"),
        Port("right", "out", "audio"),
    ]
