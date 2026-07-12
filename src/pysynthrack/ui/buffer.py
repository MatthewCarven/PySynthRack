"""Pure helpers for the global audio buffer-size ("block size") control.

The audio backends render in fixed-size blocks; ``block_size`` frames per
block is the classic latency-vs-safety knob (small = low latency, high CPU
and underrun risk; large = safe, sluggish). This module holds the dpg-free
maths for the toolbar's buffer slider so it can be unit-tested without a
graphics context — the DearPyGui glue lives in ``app.py`` and the actual
apply-to-backend happens in ``AudioBackend.set_block_size``.

The allowed sizes are a fixed, *non-uniform* set, so the toolbar slider
carries the **index** into ``BUFFER_SIZES`` (a clean 0..N-1 integer range)
rather than the raw frame count; ``index_to_size`` / ``size_to_index`` map
between the two, and ``snap_buffer`` rounds an arbitrary count to the
nearest allowed stop.

The buffered speaker sink (``buffered_specific_speaker_output``) shares the
same stops but extends past them: its secondary stream is a cue/monitor
feed where enormous safety buffers make sense (a flaky Bluetooth device
can ride 8192 frames while the main mix stays tight), so
``SINK_BUFFER_SIZES`` adds the 2048/4096/8192 stops and
``coerce_sink_buffer_size`` snaps onto that longer list. ``format_sink_buffer``
is the dpg-free text for that sink's on-node ring readout.
"""
from __future__ import annotations

# Allowed buffer sizes in frames (samples per block), ascending. Powers of
# two plus the common 384/768 half-steps PortAudio and pyo both accept.
# 64 @ 44.1 kHz ≈ 1.5 ms (low latency, CPU-hungry); 1024 ≈ 23 ms (safe).
BUFFER_SIZES: tuple[int, ...] = (64, 128, 256, 384, 512, 768, 1024)

# The buffered sink's dropdown: the global stops plus roomy power-of-two
# extensions for glitchy secondary devices. 8192 matches the backend's
# _MAX_SINK_BLOCK rail (≈186 ms @ 44.1 kHz — Bluetooth territory); the
# global slider deliberately stops at 1024 because the *main* stream's
# block size also sets keyboard-to-ear latency.
SINK_BUFFER_SIZES: tuple[int, ...] = BUFFER_SIZES + (2048, 4096, 8192)

# Matches the backend default (AudioBackend.__init__ block_size=512): a safe
# middle-of-the-road choice most machines run glitch-free.
BUFFER_DEFAULT: int = 512


def clamp_index(i: int) -> int:
    """Clamp a slider index into the valid ``[0, len(BUFFER_SIZES)-1]`` range."""
    i = int(i)
    if i < 0:
        return 0
    top = len(BUFFER_SIZES) - 1
    if i > top:
        return top
    return i


def index_to_size(i: int) -> int:
    """Slider index -> buffer size in frames (index clamped into range)."""
    return BUFFER_SIZES[clamp_index(i)]


def snap_buffer(n: int) -> int:
    """Round an arbitrary frame count to the nearest allowed buffer size.

    Ties resolve to the smaller size (lower latency). Defensive — used for
    any value that did not come straight off the slider.
    """
    n = int(n)
    return min(BUFFER_SIZES, key=lambda s: (abs(s - n), s))


def size_to_index(n: int) -> int:
    """Buffer size in frames -> slider index, snapping first if needed."""
    return BUFFER_SIZES.index(snap_buffer(n))


def coerce_buffer_size(raw: object, default: int = BUFFER_DEFAULT) -> int:
    """Resolve an arbitrary/persisted value to a valid buffer size.

    Snaps anything numeric to the nearest allowed stop; falls back to
    ``default`` (itself a valid stop) for ``None`` or non-numeric junk — e.g.
    a missing or garbage ``buffer_size`` key read out of the settings file.
    """
    try:
        return snap_buffer(int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def snap_sink_buffer(n: int) -> int:
    """``snap_buffer`` against the buffered sink's longer stop list.

    Same tie-break (nearer stop, then smaller = lower latency); the only
    difference is the 2048/4096/8192 extensions being valid targets.
    """
    n = int(n)
    return min(SINK_BUFFER_SIZES, key=lambda s: (abs(s - n), s))


def coerce_sink_buffer_size(raw: object, default: int = BUFFER_DEFAULT) -> int:
    """``coerce_buffer_size`` for the buffered sink's ``buffer_size`` param.

    Snaps onto ``SINK_BUFFER_SIZES`` so a patch saved with a roomy 4096
    displays (and round-trips) as 4096 instead of being crushed to 1024.
    """
    try:
        return snap_sink_buffer(int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def format_sink_buffer(entry: tuple[int, int, int, int] | None) -> str:
    """One-line ring readout for the buffered sink's node.

    ``entry`` is the backend's ``(queued, capacity, underruns, drops)``
    telemetry for the sink's secondary-stream hand-off ring, or ``None``
    when the sink has no live stream (transport stopped, ``device`` empty
    so it drains to master, or the device failed to open). The percentage
    is how full the ring is — the cushion between the render clock and the
    device clock; ``under`` counts device callbacks that ran dry (gap /
    zero-pad) after the ring had first filled to one device block, and
    ``drop`` counts pushes that lost audio (overwrote unread samples, or
    exceeded the whole ring), both cumulative since the stream opened.
    ASCII only: the node font has no wider glyph coverage.
    """
    if entry is None:
        return "buffer: idle"
    queued, capacity, underruns, drops = entry
    pct = 0 if capacity <= 0 else round(100.0 * queued / capacity)
    return (
        f"buffer {pct:d}% ({queued}/{capacity})  "
        f"under {underruns}  drop {drops}"
    )
