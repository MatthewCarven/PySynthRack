"""Unit tests for the dpg-free buffer-size maths (``pysynthrack.ui.buffer``).

Like the zoom tests, these import no DearPyGui, so they run in the normal
suite without a graphics context. They pin the pure logic the toolbar's
buffer slider relies on: index<->size mapping and nearest-stop snapping.
"""

import pytest

from pysynthrack.ui import buffer as b

# ----- constants ----------------------------------------------------------

def test_default_is_a_real_stop():
    assert b.BUFFER_DEFAULT in b.BUFFER_SIZES


def test_default_matches_backend_default():
    # AudioBackend.__init__ defaults block_size=512; the slider must agree so
    # a fresh launch doesn't silently reboot the engine on first Start.
    assert b.BUFFER_DEFAULT == 512


def test_sizes_are_the_agreed_set():
    assert b.BUFFER_SIZES == (64, 128, 256, 384, 512, 768, 1024)


def test_sizes_sorted_unique_and_positive():
    assert list(b.BUFFER_SIZES) == sorted(b.BUFFER_SIZES)
    assert len(set(b.BUFFER_SIZES)) == len(b.BUFFER_SIZES)
    assert all(s > 0 for s in b.BUFFER_SIZES)


# ----- clamp_index --------------------------------------------------------

def test_clamp_index_inside_range_is_identity():
    for i in range(len(b.BUFFER_SIZES)):
        assert b.clamp_index(i) == i


def test_clamp_index_below_zero():
    assert b.clamp_index(-1) == 0
    assert b.clamp_index(-99) == 0


def test_clamp_index_above_top():
    top = len(b.BUFFER_SIZES) - 1
    assert b.clamp_index(top + 1) == top
    assert b.clamp_index(9999) == top


def test_clamp_index_coerces_to_int():
    assert b.clamp_index(2.0) == 2


# ----- index_to_size ------------------------------------------------------

def test_index_to_size_maps_each_stop():
    for i, size in enumerate(b.BUFFER_SIZES):
        assert b.index_to_size(i) == size


def test_index_to_size_clamps_out_of_range():
    assert b.index_to_size(-5) == b.BUFFER_SIZES[0]
    assert b.index_to_size(999) == b.BUFFER_SIZES[-1]


# ----- snap_buffer --------------------------------------------------------

def test_snap_exact_members_are_identity():
    for size in b.BUFFER_SIZES:
        assert b.snap_buffer(size) == size


def test_snap_below_min_and_above_max():
    assert b.snap_buffer(1) == 64
    assert b.snap_buffer(0) == 64
    assert b.snap_buffer(-100) == 64
    assert b.snap_buffer(100_000) == 1024


def test_snap_rounds_to_nearest():
    assert b.snap_buffer(100) == 128   # 100 is nearer 128 than 64
    assert b.snap_buffer(200) == 256   # nearer 256 than 128
    assert b.snap_buffer(500) == 512


def test_snap_tie_resolves_to_smaller():
    # 96 is equidistant from 64 and 128 -> lower latency wins.
    assert b.snap_buffer(96) == 64
    # 192 is equidistant from 128 and 256.
    assert b.snap_buffer(192) == 128


# ----- size_to_index ------------------------------------------------------

def test_size_to_index_maps_each_stop():
    for i, size in enumerate(b.BUFFER_SIZES):
        assert b.size_to_index(size) == i


def test_size_to_index_snaps_arbitrary_values():
    assert b.size_to_index(500) == b.BUFFER_SIZES.index(512)
    assert b.size_to_index(1) == 0
    assert b.size_to_index(100_000) == len(b.BUFFER_SIZES) - 1


# ----- round trips --------------------------------------------------------

def test_index_size_round_trip():
    for i in range(len(b.BUFFER_SIZES)):
        assert b.size_to_index(b.index_to_size(i)) == i


def test_size_index_round_trip_at_stops():
    for size in b.BUFFER_SIZES:
        assert b.index_to_size(b.size_to_index(size)) == size


# ----- coerce_buffer_size (settings-file resolver) ------------------------

def test_coerce_none_returns_default():
    assert b.coerce_buffer_size(None) == b.BUFFER_DEFAULT


def test_coerce_valid_stop_passthrough():
    assert b.coerce_buffer_size(512) == 512
    assert b.coerce_buffer_size(64) == 64
    assert b.coerce_buffer_size(1024) == 1024


def test_coerce_snaps_arbitrary_number():
    assert b.coerce_buffer_size(500) == 512
    assert b.coerce_buffer_size(1) == 64
    assert b.coerce_buffer_size(100_000) == 1024


def test_coerce_numeric_string():
    assert b.coerce_buffer_size("256") == 256
    assert b.coerce_buffer_size("500") == 512


def test_coerce_garbage_returns_default():
    assert b.coerce_buffer_size("abc") == b.BUFFER_DEFAULT
    assert b.coerce_buffer_size({}) == b.BUFFER_DEFAULT
    assert b.coerce_buffer_size([1]) == b.BUFFER_DEFAULT


def test_coerce_custom_default():
    assert b.coerce_buffer_size(None, default=128) == 128


# ----- SINK_BUFFER_SIZES (the buffered sink's longer list) ------------------

def test_sink_sizes_extend_the_global_set():
    assert b.SINK_BUFFER_SIZES == b.BUFFER_SIZES + (2048, 4096, 8192)


def test_sink_sizes_sorted_unique_and_positive():
    assert list(b.SINK_BUFFER_SIZES) == sorted(b.SINK_BUFFER_SIZES)
    assert len(set(b.SINK_BUFFER_SIZES)) == len(b.SINK_BUFFER_SIZES)
    assert all(s > 0 for s in b.SINK_BUFFER_SIZES)


def test_sink_top_stop_matches_backend_rail():
    # The dropdown's biggest offer must be exactly what the backend clamp
    # allows, or the UI would show a size the stream can't open at.
    from pysynthrack.audio.numpy_backend import _MAX_SINK_BLOCK
    assert b.SINK_BUFFER_SIZES[-1] == _MAX_SINK_BLOCK


def test_sink_default_is_a_sink_stop():
    assert b.BUFFER_DEFAULT in b.SINK_BUFFER_SIZES


# ----- snap_sink_buffer / coerce_sink_buffer_size ---------------------------

def test_snap_sink_exact_members_are_identity():
    for size in b.SINK_BUFFER_SIZES:
        assert b.snap_sink_buffer(size) == size


def test_snap_sink_reaches_past_the_global_ceiling():
    # The whole point: values the global snapper would crush to 1024.
    assert b.snap_sink_buffer(2048) == 2048
    assert b.snap_sink_buffer(3000) == 2048   # nearer 2048 than 4096
    assert b.snap_sink_buffer(100_000) == 8192
    assert b.snap_buffer(100_000) == 1024     # global stays capped


def test_snap_sink_tie_resolves_to_smaller():
    # 1536 is equidistant from 1024 and 2048 -> lower latency wins.
    assert b.snap_sink_buffer(1536) == 1024
    # 3072 is equidistant from 2048 and 4096.
    assert b.snap_sink_buffer(3072) == 2048


def test_coerce_sink_valid_stop_passthrough():
    for size in (64, 512, 1024, 2048, 4096, 8192):
        assert b.coerce_sink_buffer_size(size) == size


def test_coerce_sink_numeric_string_and_float():
    assert b.coerce_sink_buffer_size("4096") == 4096
    assert b.coerce_sink_buffer_size(4096.0) == 4096


def test_coerce_sink_garbage_returns_default():
    assert b.coerce_sink_buffer_size("abc") == b.BUFFER_DEFAULT
    assert b.coerce_sink_buffer_size(None) == b.BUFFER_DEFAULT
    assert b.coerce_sink_buffer_size(None, default=256) == 256


# ----- format_sink_buffer (node ring readout text) ---------------------------

def test_format_idle_when_no_stream():
    assert b.format_sink_buffer(None) == "buffer: idle"


def test_format_running_line():
    assert (
        b.format_sink_buffer((3852, 8192, 0, 2))
        == "buffer 47% (3852/8192)  under 0  drop 2"
    )


def test_format_empty_and_full_ring():
    assert b.format_sink_buffer((0, 4096, 0, 0)).startswith("buffer 0% ")
    assert b.format_sink_buffer((4096, 4096, 0, 0)).startswith("buffer 100% ")


def test_format_zero_capacity_does_not_divide():
    # Defensive: a degenerate ring reads 0%, not ZeroDivisionError.
    assert b.format_sink_buffer((0, 0, 0, 0)).startswith("buffer 0% ")


def test_format_is_ascii_only():
    # The node font has no wide glyph coverage; keep the readout plain.
    for entry in (None, (123, 8192, 4, 7)):
        b.format_sink_buffer(entry).encode("ascii")
