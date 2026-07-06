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
toolbar is the meter for how long an IR you can afford. Loaded IRs are
**length-capped** (~5 s to start) so a stray long file can't stall the audio.

Loading an IR. Point ``path`` at an audio file — a WAV, or (with the
``[media]`` extra / a system ffmpeg) an mp3/flac/ogg/m4a or the audio track
of a video — and the Browse button on the node opens the same file picker
the FilePlayer uses. IRs load **whole** (no streaming; they're short), and
the decode + partition-FFT build runs on a **background thread**, so a fresh
or changed IR never blocks the audio thread — the convolver stays
transparent (or keeps the previous IR) until the new one is ready. On load
the IR is **energy-normalised** (so different IRs sit at a consistent level
rather than blowing up or vanishing) — a single scale across both channels,
so the stereo image is preserved. With an empty or unreadable path it is a
**transparent insert** (a unit-impulse IR: dry passthrough delayed by the
reported latency), so a saved patch always loads even if the IR file moved.

Stereo. The convolver emits a **stereo pair**. A stereo IR file convolves
the (mono-summed) input through its **left** channel into ``out_l`` and its
**right** channel into ``out_r`` — the decorrelation captured in the IR *is*
the stereo image. A mono IR drives both channels identically.

Shaping the wet. ``predelay`` delays the reverb onset behind the dry (the
gap that keeps a source articulate in a big space); ``tone`` is a low-pass
that darkens the wet (air, soft furnishings) and is *off* at its maximum.
Both act on the wet only, so ``mix = 0`` stays a bit-exact dry bypass.

Latency. The wet path has a fixed **one-block latency** (a render block is
buffered before it can be transformed). The dry path is delay-matched by the
same one block inside the ``mix`` blend, so dry and wet stay phase-coherent
and the whole module presents one clean, constant block of latency
(``predelay`` is an extra, intentional wet-only delay on top).

Parameters:
    path: Path to an IR audio file. Empty / missing / unreadable → a
        unit-impulse IR (transparent insert). Relative paths resolve against
        the process working directory.
    predelay: Milliseconds of wet-only pre-delay, 0 … 500. 0 = reverb starts
        with the dry.
    tone: Wet low-pass cutoff in Hz, 1000 … 20000. At 20000 (the maximum)
        the filter is **off** (transparent wet); lower darkens the tail.
    gain: Linear trim on the **wet** (convolved) signal only, in [0, 2].
        The dry path is never touched by ``gain``, so ``mix = 0`` is always
        a bit-exact dry bypass regardless of ``gain``.
    mix: Dry/wet balance in [0, 1]. ``0`` = bit-exact dry (bypass, and the
        FFT is skipped); ``1`` = fully wet. With a unit-impulse IR at
        ``predelay = 0`` / ``tone`` off, every mix is transparent.

Ports:
    in (in, audio): the signal to convolve. A polyphonic (voice-aware)
        source is summed to mono first — convolution is linear, so
        convolving each voice and summing equals summing then convolving,
        and the mono sum is far cheaper. Unpatched -> silence.
    out_l (out, audio): left output (dry + wet through the IR's left
        channel). Equals ``out_r`` for a mono IR.
    out_r (out, audio): right output (dry + wet through the IR's right
        channel).

Neutral: a unit-impulse IR at ``mix = 1`` (``gain = 1``, ``predelay = 0``,
``tone`` off) is a passthrough within ~1e-6 — not bit-exact, because the FFT
round-trip is float, not exact (pinned and documented). ``mix = 0`` is
bit-exact dry.
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
            audio thread; energy-normalised and length-capped on load.
        predelay: Wet-only pre-delay in ms, 0 … 500.
        tone: Wet low-pass cutoff in Hz, 1000 … 20000; off at 20000.
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
        "predelay": 0.0,
        "tone": 20000.0,
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
