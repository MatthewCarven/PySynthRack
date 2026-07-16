"""Output modules — sinks that drive a speaker or disk file."""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port

# Optional at import time, exactly like the backend and MicInput: a missing
# PortAudio install must not stop the module registering (the palette still
# shows it, and the device dropdown just falls back to the default output).
try:
    import sounddevice as _sd  # type: ignore
    _HAS_SD = True
except Exception:  # pragma: no cover - environment-dependent
    _sd = None  # type: ignore[assignment]
    _HAS_SD = False


# Sentinel for "use the system default output device" — shares the empty-
# string convention with MIDIInput / MicInput's AUTO_DEVICE so the UI combo
# logic is uniform across every device-bearing module.
AUTO_DEVICE = ""


def available_output_devices() -> list[str]:
    """List playback-capable device names; empty if sounddevice is absent.

    The output mirror of :func:`pysynthrack.modules.micinput.available_input_devices`.
    Filters to devices reporting at least one *output* channel and de-dupes by
    name (host APIs often expose the same device several times). Never raises —
    a flaky audio stack yields an empty list, and the UI still offers the
    ``AUTO_DEVICE`` default.
    """
    if not _HAS_SD:
        return []
    try:
        names: list[str] = []
        seen: set[str] = set()
        for dev in _sd.query_devices():
            if int(dev.get("max_output_channels", 0)) > 0:
                name = str(dev.get("name", "")).strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names
    except Exception:  # pragma: no cover - depends on host audio stack
        return []


@register_module_type
class SpeakerOutput(Module):
    """Routes its input to the system audio output device.

    Mono, routed to both channels. For placement in the field, see
    :class:`StereoSpeakerOutput` (pan / width — the stereo variant this
    docstring promised since v0.1).

    Parameters:
        gain: Linear gain applied just before output, in [0, 1].
    """

    TYPE = "speaker_output"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list[Port] = []


@register_module_type
class LeftSpeakerOutput(Module):
    """Routes its mono input exclusively to the LEFT output channel.

    The numpy backend's drain mixes this sink into the left bus only;
    the right bus stays silent for this node. Place a Left + Right pair
    to get hard-panned stereo without a stereo Speaker module.

    Parameters:
        gain: Linear gain applied just before output, in [0, 1].
    """

    TYPE = "left_speaker_output"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list[Port] = []


@register_module_type
class RightSpeakerOutput(Module):
    """Mirror of :class:`LeftSpeakerOutput` — mono input to the RIGHT
    channel only. Compose with LeftSpeakerOutput for stereo patches.

    Parameters:
        gain: Linear gain applied just before output, in [0, 1].
    """

    TYPE = "right_speaker_output"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS: list[Port] = []


@register_module_type
class StereoSpeakerOutput(Module):
    """The stereo speaker — a sink with a place in the field.

    The full-width sibling the mono :class:`SpeakerOutput` docstring has
    promised since v0.1. Two audio inputs and three knobs put a signal
    *somewhere* between the speakers instead of dead centre:

    * ``pan`` — where. With only ``in_l`` patched the module treats the
      source as MONO and places it with a constant-power (−3 dB centre)
      cos/sin law, so a sweep keeps even loudness. With both inputs
      patched it acts as a BALANCE control: unity at centre, smoothly
      attenuating the far side as you turn.
    * ``width`` — how wide (stereo pairs only; a mono source has no
      side content to widen). Mid/side scaling: 0 collapses to mono,
      1 leaves the pair untouched, up to 2 exaggerates the sides.
    * ``gain`` — how loud, applied last.

    ``pan_cv`` modulates the pan per sample and ``width_cv`` the width
    (both scaled by the shared ``cv_depth``, like the Reverb's paired
    CV inputs — drop a CVScale in front for independent sensitivity):
    an LFO on ``pan_cv`` is the classic autopan, an envelope walks each
    note across the field, and a slow LFO on ``width_cv`` makes the
    image breathe between mono and wide. Voice-aware sources are summed at the jack (the
    implicit-sum-at-mono-sinks rule), and everything lands on the same
    master bus as the other speaker sinks, clipped at ±1.

    At the defaults (pan 0, width 1, gain 1, CV jacks unpatched) a
    stereo pair passes to the bus bit-exactly — patching a
    chorus/reverb straight into this sink is transparent until you
    reach for a knob (or a CV cable).

    Parameters:
        gain: Linear output gain, applied after pan/width.
        pan: Position/balance, -1 (hard left) .. 1 (hard right).
        width: Stereo width, 0 (mono) .. 2 (over-wide). Pairs only.
        cv_depth: Knob units added per CV unit, shared by ``pan_cv``
            and ``width_cv``. 0 disables both.
    """

    TYPE = "stereo_speaker_output"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
        "pan": 0.0,
        "width": 1.0,
        "cv_depth": 1.0,
    }
    INPUT_PORTS = [
        Port("in_l", "in", "audio"),
        Port("in_r", "in", "audio"),
        Port("pan_cv", "in", "cv"),
        Port("width_cv", "in", "cv"),
    ]
    OUTPUT_PORTS: list[Port] = []


@register_module_type
class SpecificStereoSpeakerOutput(Module):
    """A stereo speaker aimed at a *chosen* output device.

    Everything :class:`StereoSpeakerOutput` does — the same ``in_l`` /
    ``in_r`` inputs, the ``pan`` / ``width`` / ``gain`` knobs, and the
    shared-``cv_depth`` ``pan_cv`` / ``width_cv`` jacks (see that class
    for the full pan-law / mid-side / CV semantics) — plus one extra
    parameter, ``device``, naming the physical output it should play out
    of. Drop one on a cue/monitor bus and pin it to your headphones while
    the main mix stays on the studio monitors.

    The ``device`` field is a snapshot dropdown + Refresh (the MicInput
    pattern) and round-trips through saved patches. Left empty (``""``, the
    default) the sink drains into the shared master bus **bit-identically**
    to :class:`StereoSpeakerOutput`. Set to a named device, the numpy
    backend pulls it **off** the master onto that device's own secondary
    :class:`sounddevice.OutputStream` (one per distinct device, sinks on a
    shared device summed), fed from the main audio callback through a
    drop-oldest ring that absorbs the drift between the two independent
    PortAudio clocks. Changing the device takes effect **live** — only the
    affected stream is rebuilt, no Stop/Start. For a per-sink output block
    size on that secondary stream, see
    :class:`BufferedSpecificSpeakerOutput`. (pyo leaves this a silent stub;
    run the numpy backend for the routing.)

    Parameters:
        gain: Linear output gain, applied after pan/width.
        pan: Position/balance, -1 (hard left) .. 1 (hard right).
        width: Stereo width, 0 (mono) .. 2 (over-wide). Pairs only.
        cv_depth: Knob units added per CV unit, shared by ``pan_cv``
            and ``width_cv``. 0 disables both.
        device: Output device name, or ``""`` (``AUTO_DEVICE``) for the
            system default. The list comes from
            :func:`available_output_devices` and the UI offers it as a
            dropdown snapshotted at widget creation, like the MicInput
            device picker.
    """

    TYPE = "specific_stereo_speaker_output"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
        "pan": 0.0,
        "width": 1.0,
        "cv_depth": 1.0,
        "device": AUTO_DEVICE,
    }
    INPUT_PORTS = [
        Port("in_l", "in", "audio"),
        Port("in_r", "in", "audio"),
        Port("pan_cv", "in", "cv"),
        Port("width_cv", "in", "cv"),
    ]
    OUTPUT_PORTS: list[Port] = []


@register_module_type
class BufferedSpecificSpeakerOutput(Module):
    """A :class:`SpecificStereoSpeakerOutput` with its own output buffer size.

    Identical in every audible respect to
    :class:`SpecificStereoSpeakerOutput` — the same ``in_l`` / ``in_r``
    inputs, the ``pan`` / ``width`` / ``gain`` knobs, the shared-``cv_depth``
    ``pan_cv`` / ``width_cv`` jacks, and the same ``device`` picker that pulls
    this sink onto its own secondary output stream (see that class for the
    pan-law / mid-side / CV semantics and the per-device routing). The one
    addition is ``buffer_size``: the block size (frames per PortAudio
    callback) of *this sink's* secondary stream, independent of the global
    buffer size that drives the main output.

    Why a per-sink buffer: the main mix might run at a tight 128-frame buffer
    for a responsive keyboard while a flaky USB / Bluetooth monitor on this
    sink needs a roomy 1024 — or even 8192, nearly 200 ms of cushion — to
    stop crackling; or the reverse, a low-latency cue feed taken off an
    otherwise safe, sluggish main buffer. The secondary stream already runs
    on its own PortAudio clock (fed through a drop-oldest ring that absorbs
    the drift between the two), so it can carry its own block size, at the
    cost of added latency on that one device. The sink's dropdown therefore
    extends past the global slider's 1024 ceiling with 2048/4096/8192 stops —
    sizes that would be absurd for the keyboard-to-ear path but are exactly
    right for a drifting Bluetooth monitor.

    ``buffer_size`` is read when the stream opens, so the natural workflow is
    to set it before you Start. Changing it live rebuilds just this sink's
    stream (a brief gap on that one device), exactly as changing ``device``
    does. Only the numpy backend routes this sink; under pyo it is a silent
    stub, like the other stereo speakers.

    To make the size choice observable instead of guesswork, the node carries
    a live ring readout — ``buffer 47% (3852/8192)  under 0  drop 2`` — fed
    each GUI frame from the backend: how full the hand-off ring is, plus
    cumulative underrun (device ran dry, gap played; counted only once the
    ring has first filled, so a clean Start doesn't tick) and drop (a push
    lost audio: the ring overflowed and shed its oldest samples, or the push
    outsized the whole ring) counts since the stream opened. A climbing
    ``under`` says buy more cushion (bigger ``buffer_size``); a pinned-full
    ring with climbing ``drop`` says the device clock runs slow relative to
    the main stream and latency will ride the ring's ceiling; both climbing
    from the very first moment says the ring itself (8x ``buffer_size``) is
    smaller than one main-stream block — raise ``buffer_size`` or lower the
    global buffer. The line reads ``buffer: idle`` when the sink has no
    stream of its own (transport stopped, ``device`` empty, or the device
    failed to open), and sinks sharing one (device, buffer_size) stream show
    identical numbers.

    The governor jacks — ``fill`` (cv out) and ``ratio_cv`` (cv in) — make
    the ring's regulation *patchable*. ``fill`` publishes the ring's fill
    fraction (0 empty .. 1 full; a neutral 0.5 while the sink has no
    stream), delayed one block so a feedback patch is legal — the
    topological sort deliberately ignores cables leaving this jack.
    ``ratio_cv`` varispeed-resamples the block this sink pushes onto its
    secondary stream: the pushed length becomes ``frames * (1 + cv *
    ratio_depth)``, clamped to 0.5x..2x and smoothed over ~a dozen blocks
    so a twitchy patch can't warble the pitch violently. Wire
    ``fill -> (offset -0.5) -> (NEGATIVE gain) -> ratio_cv`` and the sink
    stretches time to hold its own ring at half — an adaptive-resampling
    clock governor built from patch cables. The gain's sign matters: a
    low ring needs a POSITIVE cv (push more), so the loop inverts —
    positive gain runs away to an empty or pinned-full ring. The stretch
    is PITCH-PRESERVING: a streaming WSOLA shift (the pitch shifter's
    engine, 50 ms grain per channel) cancelled by the length resample,
    so even large corrections hold pitch — at the cost of one constant
    ~50 ms latency on the governed path and a one-grain warm-up (brief
    silence) when the cable first lands or the transport starts. The
    engines stay in-circuit while cabled, even at ratio 1, so that
    warm-up is never paid mid-performance; with ``ratio_cv`` unpatched
    the push is bit-identical to before. Numpy backend only, like the
    routing itself.

    Parameters:
        gain: Linear output gain, applied after pan/width.
        pan: Position/balance, -1 (hard left) .. 1 (hard right).
        width: Stereo width, 0 (mono) .. 2 (over-wide). Pairs only.
        cv_depth: Knob units added per CV unit, shared by ``pan_cv``
            and ``width_cv``. 0 disables both.
        device: Output device name, or ``""`` (``AUTO_DEVICE``) for the
            system default — the same picker as SpecificStereoSpeakerOutput.
        buffer_size: Frames per block for this sink's own output stream. One
            of the sink sizes (64 .. 8192 — the global stops plus the
            2048/4096/8192 extensions); defaults to 512. The backend clamps
            any out-of-range value to a safe stop.
        ratio_depth: Stretch swing per ``ratio_cv`` unit (default 0.25:
            cv +-1 pushes 25% more/fewer samples). The engine clamps the
            resulting ratio to 0.5..2 whatever the depth.
    """

    TYPE = "buffered_specific_speaker_output"
    CATEGORY = "Outputs"
    DEFAULT_PARAMS = {
        "gain": 1.0,
        "pan": 0.0,
        "width": 1.0,
        "cv_depth": 1.0,
        "device": AUTO_DEVICE,
        "buffer_size": 512,
        "ratio_depth": 0.25,
    }
    INPUT_PORTS = [
        Port("in_l", "in", "audio"),
        Port("in_r", "in", "audio"),
        Port("pan_cv", "in", "cv"),
        Port("width_cv", "in", "cv"),
        Port("ratio_cv", "in", "cv"),
    ]
    OUTPUT_PORTS = [Port("fill", "out", "cv")]
