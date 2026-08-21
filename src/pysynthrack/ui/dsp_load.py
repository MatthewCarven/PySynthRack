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


# Stream-health readout thresholds, in cumulative underflow events since
# Start. Zero is the only genuinely clean reading -- every underflow is an
# audible click -- so the ramp starts at the first one; RED is where a
# handful has become a pattern rather than a one-off scheduling wobble.
XRUN_WARN = 1
XRUN_HOT = 10


def format_xruns(xruns: int | None) -> str:
    """Toolbar text for the device underflow count (``None`` = stopped)."""
    if xruns is None:
        return "xrun --"
    return f"xrun {max(int(xruns), 0)}"


def xrun_color(xruns: int | None) -> tuple[int, int, int, int]:
    """Readout colour for the underflow count (``None`` = stopped)."""
    if xruns is None:
        return IDLE_COLOR
    if xruns < XRUN_WARN:
        return OK_COLOR
    if xruns < XRUN_HOT:
        return WARN_COLOR
    return HOT_COLOR


def format_host_api(name: str | None) -> str:
    """Toolbar text naming the host API the stream opened on.

    Blank or missing (audio stopped, or the lookup failed) reads as
    dashes rather than vanishing, so the slot keeps its width and the
    toolbar doesn't reflow on Start.
    """
    if not name:
        return "api --"
    return f"api {name}"


def diagnose(load: float | None, xruns: int | None) -> str:
    """One-line reading of load against underflows, for the tooltip.

    The pair is more informative than either alone: underflows with the
    render comfortably inside budget mean the *callback* was late, not
    that the patch is too expensive -- a scheduling/jitter problem that
    rendering ahead into a queue would absorb. Underflows alongside a
    load near or over 100% mean the patch genuinely doesn't fit the
    budget, and only a cheaper patch or a bigger block will help.
    """
    if load is None or xruns is None:
        return "Audio stopped."
    if xruns < XRUN_WARN:
        return "No device underflows - output is keeping up."
    if load >= HOT_LOAD:
        return (
            "Underflows with the render near or over budget: the patch is "
            "too expensive for this block size. Simplify it or raise the "
            "buffer."
        )
    return (
        "Underflows while the render fits the budget: the callback is "
        "arriving late (OS scheduling, GIL contention, or host-API "
        "jitter) rather than the patch being too heavy."
    )
