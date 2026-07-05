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

Slice 1 (this build) is the **mono fixed-block core**: a single IR channel,
oracle-tested block-for-block against ``scipy.signal.fftconvolve``. Until an
IR file is loaded (that's a later slice), the IR defaults to a **unit
impulse**, so a freshly-added Convolver is a *transparent insert* — it
passes audio straight through (delayed by its reported latency) and does
nothing until you give it a real IR. The two outputs carry the same mono
result for now; they split into a true stereo pair once stereo IRs load.

Latency. The wet path has a fixed **one-block latency** (a render block is
buffered before it can be transformed). The dry path is delay-matched by the
same one block inside the ``mix`` blend, so dry and wet stay phase-coherent
and the whole module presents one clean, constant block of latency.

Parameters:
    gain: Linear trim on the **wet** (convolved) signal only, in [0, 2].
        The dry path is never touched by ``gain``, so ``mix = 0`` is always
        a bit-exact dry bypass regardless of ``gain``. Once IRs are
        normalised on load (a later slice) this is the wet make-up.
    mix: Dry/wet balance in [0, 1]. ``0`` = bit-exact dry (bypass, and the
        FFT is skipped); ``1`` = fully wet. With the default unit-impulse
        IR every mix is transparent (wet == dry).

Ports:
    in (in, audio): the signal to convolve. A polyphonic (voice-aware)
        source is summed to mono first — convolution is linear, so
        convolving each voice and summing equals summing then convolving,
        and the mono sum is far cheaper. Unpatched -> silence.
    out_l (out, audio): left output (dry + wet). Mono result duplicated to
        both channels until a stereo IR loads.
    out_r (out, audio): right output.

Neutral: a unit-impulse IR at ``mix = 1`` (and ``gain = 1``) is a
passthrough within ~1e-6 — not bit-exact, because the FFT round-trip is
float, not exact (pinned and documented). ``mix = 0`` is bit-exact dry.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Convolver(Module):
    """Partitioned-FFT convolution (IR reverb / cab); mono core, stereo outs.

    Parameters:
        gain: Linear wet trim in [0, 2]. Applied to the convolved signal
            only; the dry path is untouched so ``mix = 0`` bypasses
            bit-exactly whatever ``gain`` is.
        mix: Dry/wet balance in [0, 1]. 0 = bit-exact dry (FFT skipped),
            1 = fully wet.

    Ports:
        in (in, audio): signal to convolve (voice sources summed to mono).
            Unpatched -> silence.
        out_l (out, audio): left channel (dry + wet).
        out_r (out, audio): right channel.
    """

    TYPE = "convolver"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
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
