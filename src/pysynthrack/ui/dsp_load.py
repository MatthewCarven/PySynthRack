"""DSP-load readout maths — dpg-free so tests can pin it (ui/zoom.py style).

The toolbar shows the audio engine's *DSP load*: the share of the block
budget (``block_size / sample_rate`` seconds) that recent renders
consumed, exponentially smoothed by the backend. This is the figure a
DAW's "CPU" meter shows — whole-machine CPU would need a new dependency
and says much less about whether the patch is about to glitch. Above
100% a block missed real time and an underrun is likely audible.
"""
from __future__ import annotations

# Colour-ramp thresholds, as fractions of the block budget. Below WARN
# the readout sits in the meter green (comfortable headroom); from WARN
# it turns amber (getting close); from HOT it turns red (one scheduling
# wobble from an underrun, or already over budget).
WARN_LOAD = 0.5
HOT_LOAD = 0.8

# RGBA text colours. Green/red match the audio meter's fill/clip-lamp
# palette so the toolbar speaks the same language as the meters.
IDLE_COLOR = (128, 128, 128, 255)
OK_COLOR = (90, 190, 120, 255)
WARN_COLOR = (230, 180, 40, 255)
HOT_COLOR = (235, 64, 52, 255)


def format_dsp_load(load: float | None) -> str:
    """Toolbar text for a smoothed load figure (``None`` = audio stopped)."""
    if load is None:
        return "DSP --"
    pct = int(round(max(load, 0.0) * 100.0))
    return f"DSP {pct}%"


def load_color(load: float | None) -> tuple[int, int, int, int]:
    """Readout text colour for a smoothed load (``None`` = audio stopped)."""
    if load is None:
        return IDLE_COLOR
    if load < WARN_LOAD:
        return OK_COLOR
    if load < HOT_LOAD:
        return WARN_COLOR
    return HOT_COLOR
