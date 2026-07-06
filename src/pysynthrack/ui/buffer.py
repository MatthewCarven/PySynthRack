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
"""
from __future__ import annotations

# Allowed buffer sizes in frames (samples per block), ascending. Powers of
# two plus the common 384/768 half-steps PortAudio and pyo both accept.
# 64 @ 44.1 kHz ≈ 1.5 ms (low latency, CPU-hungry); 1024 ≈ 23 ms (safe).
BUFFER_SIZES: tuple[int, ...] = (64, 128, 256, 384, 512, 768, 1024)

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
