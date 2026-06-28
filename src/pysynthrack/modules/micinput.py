"""MicInput — live microphone capture as a stereo audio source.

A *source* module: no inputs, two audio outputs (``left`` / ``right``).
Unlike every other source it doesn't synthesise — it hands the patch the
audio coming off an input device in real time, so you can run a voice (or
anything) through the modular graph. Beatbox into it, split the signal
with a ``Crossover``, rectify the low band with ``AudioToCV`` to get a
kick-driven envelope and the high band to steer a ``CVToFrequency`` — the
mic becomes a modulation source, not just a sound.

How the audio actually arrives. The numpy backend opens a *full-duplex*
stream (input + output in one callback) only when a patch contains a mic
module; patches without one keep the cheaper output-only stream, so users
with no microphone or no input permission are never forced into capture.
Each callback the backend stashes the just-captured input block, and this
module's renderer reads it: a 2-channel device maps to ``left`` / ``right``,
a mono device duplicates to both. If the duplex stream can't be opened
(no device, wrong samplerate, permission denied) the backend logs and
falls back to output-only, and this module renders silence.

Feedback warning: if the mic output reaches the speakers in the same room
as the mic, you'll get a howl. Beatboxers — wear headphones.

Parameters:
    device: Input device name, or ``""`` to use the system default input.
        The full name list is available via :func:`available_input_devices`
        and the UI offers it as a dropdown (snapshotted at widget creation,
        like the MIDIInput device picker).
    gain: Linear gain applied to both channels, in [0, 2]. The signal off
        the device is whatever level the OS/interface provides; trim here.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Optional at import time, exactly like the backend: a missing PortAudio
# install must not stop the module registering (the palette still shows it).
try:
    import sounddevice as _sd  # type: ignore
    _HAS_SD = True
except Exception:  # pragma: no cover - environment-dependent
    _sd = None  # type: ignore[assignment]
    _HAS_SD = False


# Sentinel for "use the system default input device" — shares the empty-
# string convention with MIDIInput's AUTO_DEVICE so the UI combo logic is
# uniform across both device-bearing modules.
AUTO_DEVICE = ""


def available_input_devices() -> list[str]:
    """List capture-capable device names; empty if sounddevice is absent.

    Filters to devices reporting at least one input channel and de-dupes by
    name (host APIs often expose the same device several times). Never
    raises — a flaky audio stack yields an empty list, and the UI still
    offers the ``AUTO_DEVICE`` default.
    """
    if not _HAS_SD:
        return []
    try:
        names: list[str] = []
        seen: set[str] = set()
        for dev in _sd.query_devices():
            if int(dev.get("max_input_channels", 0)) > 0:
                name = str(dev.get("name", "")).strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names
    except Exception:  # pragma: no cover - depends on host audio stack
        return []


@register_module_type
class MicInput(Module):
    """Live microphone capture, published as a stereo audio source."""

    TYPE = "mic_input"
    DEFAULT_PARAMS = {
        "device": AUTO_DEVICE,
        "gain": 1.0,
    }
    INPUT_PORTS: list[Port] = []  # source — no inputs
    OUTPUT_PORTS = [
        Port("left", "out", "audio"),
        Port("right", "out", "audio"),
    ]
