"""Convolver — convolution reverb / cabinet / IR loader.

Runs the input through an **impulse response** (IR): every sample of the
input is stamped with a scaled, delayed copy of the whole IR, and the
overlapping copies sum. That single operation reproduces whatever space or
device the IR was captured from — a real room or hall, a plate or spring
tank, a guitar cab, or any exotic sampled "reverb" — because an IR *is* the
sound of that system responding to a single click.

The DSP is a **uniformly-partitioned FFT convolution** (overlap-save): the
IR is chopped into block-sized partitions, each pre-transformed once, and
every render block is convolved by a frequency-domain multiply-accumulate
across a delay line of past input spectra. That keeps a multi-second IR
affordable at audio rates (an FFT per block instead of an O(L) tap-for-tap
sum), and the cost scales with IR length — the **DSP % readout** in the
toolbar is the meter for how long an IR you can afford.

Loading an IR. Point ``path`` at an audio file — a WAV, or (with the
``[media]`` extra / a system ffmpeg) an mp3/flac/ogg/m4a or the audio track
of a video — and the Browse button on the node opens the same file picker
the FilePlayer uses. IRs load **whole** (no streaming; they're short), and
the decode + partition-FFT build runs on a **background thread**, so a fresh
or changed IR never blocks the audio thread — the convolver stays
transparent (or keeps the previous IR) until the new one is ready. With an
empty or unreadable path it is a **transparent insert** (a unit-impulse IR:
dry passthrough delayed by the reported latency), so a saved patch always
loads even if the IR file has moved. IRs are **not** normalised yet (that's
a later slice) — trim hot IRs with ``gain``.

Stereo. The convolver emits a **stereo pair**. A stereo IR file convolves
the (mono-summed) input through its **left** channel into ``out_l`` and its
**right** channel into ``out_r`` — the decorrelation captured in the IR *is*
the stereo image (a real room miked in stereo, a stereo plate, ping-pong
tanks). A mono IR drives both channels identically (and is convolved once).

Latency. The wet path has a fixed **one-block latency** (a render block is
buffered before it can be transformed). The dry path is delay-matched by the
same one block inside the ``mix`` blend, so dry and wet stay phase-coherent
and the whole module presents one clean, constant block of latency.

Parameters:
    path: Path to an IR audio file. Empty / missing / unreadable → a
        unit-impulse IR (transparent insert). Relative paths resolve against
        the process working directory.
    gain: Linear trim on the **wet** (convolved) signal only, in [0, 2].
        The dry path is never touched by ``gain``, so ``mix = 0`` is always
        a bit-exact dry bypass regardless of ``gain``. Use it to tame hot
        (un-normalised) IRs or as the wet make-up.
    mix: Dry/wet balance in [0, 1]. ``0`` = bit-exact dry (bypass, and the
        FFT is skipped); ``1`` = fully wet. With a unit-impulse IR every mix
        is transparent (wet == dry).

Ports:
    in (in, audio): the signal to convolve. A polyphonic (voice-aware)
        source is summed to mono first — convolution is linear, so
        convolving each voice and summing equals summing then convolving,
        and the mono sum is far cheaper. Unpatched -> silence.
    out_l (out, audio): left output (dry + wet through the IR's left
        channel). Equals ``out_r`` for a mono IR.
    out_r (out, audio): right output (dry + wet through the IR's right
        channel).

Neutral: a unit-impulse IR at ``mix = 1`` (and ``gain = 1``) is a
passthrough within ~1e-6 — not bit-exact, because the FFT round-trip is
float, not exact (pinned and documented). ``mix = 0`` is bit-exact dry.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Convolver(Module):
    """Partitioned-FFT convolution (IR reverb / cab); mono-in, stereo out.

    Parameters:
        path: IR audio file (WAV or ffmpeg-decodable). Empty/unreadable →
            a unit-impulse IR (transparent insert). Loaded whole, off the
            audio thread.
        gain: Linear wet trim in [0, 2]. Applied to the convolved signal
            only; the dry path is untouched so ``mix = 0`` bypasses
            bit-exactly whatever ``gain`` is.
        mix: Dry/wet balance in [0, 1]. 0 = bit-exact dry (FFT skipped),
            1 = fully wet.

    Ports:
        in (in, audio): signal to convolve (voice sources summed to mono).
            Unpatched -> silence.
        out_l (out, audio): left channel (dry + wet via the IR's left).
        out_r (out, audio): right channel (dry + wet via the IR's right).
    """

    TYPE = "convolver"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "path": "",
        "gain": 1.0,
        "mix": 1.0,
    }
    INPUT_PORTS = [
        Port("in", "in", "audio"),
    ]
    OUTPUT_PORTS = [
        Port("out_l", "out", "audio"),
        Port("out_r", "out", "audio"),
    ]
