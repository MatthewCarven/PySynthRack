"""Bitcrusher — bit-depth quantization + sample-rate decimation, the
lo-fi "digital destruction" effect.

Two independent forms of digital degradation, in one box:

  * **Bit reduction** — requantize the signal to a coarser word length.
    A mid-tread quantizer snaps each sample to the nearest of
    ``2^bits`` levels (``round(x·2^(bits−1))/2^(bits−1)``): at 24 bits the
    step is inaudibly small (and is *skipped* entirely — see Neutral); at
    8 bits you hear grainy quantization hiss; at 1–3 bits the waveform
    collapses into buzzy, harmonically rich digital fuzz.
  * **Sample-rate reduction (decimation)** — hold every ``rate_div``-th
    input sample and repeat it, throwing the rest away. This is a
    *sample-and-hold* downsample with **no anti-imaging filter**, so the
    discarded content folds back as aliasing — and that harsh, metallic
    aliasing *is* the sound (Aphex-style digital grit, early-sampler
    crunch, ring-mod-like sidebands on high material). ``rate_div = 4``
    quarters the effective rate; ``rate_div = 64`` is brutal.

``jitter`` wobbles the hold length randomly around ``rate_div`` on a
seeded stream, smearing the decimation clock — a dirtier, less periodic
"broken converter" character (no effect unless ``rate_div > 1``). Because
the stream is seeded, a given patch renders identically every time.

``mix`` blends the dry input against the crushed output; ``mix = 0`` is a
**bit-exact dry passthrough**. ``dc_filter`` (off by default) runs a
gentle one-pole high-pass on the output to strip any DC offset the
crushing introduces, handy before further gain stages.

Signal flow: ``in → decimate → quantize → [dc filter] → mix with dry``.
(Quantize and decimate commute — the quantizer is memoryless and
pointwise, so holding a quantized sample equals quantizing a held one —
so the order between those two is immaterial to the result.)

Controls:
  * ``bits`` — word length, 1 … 24. 24 = transparent (quantizer skipped).
  * ``rate_div`` — sample-hold decimation factor, 1 … 64. 1 = no
    decimation (skipped).
  * ``jitter`` — 0 … 1, random hold-length wobble around ``rate_div``
    (seeded). 0 = perfectly periodic decimation.
  * ``mix`` — dry/wet, 0 (bit-exact dry) … 1 (fully crushed).
  * ``dc_filter`` — on/off; one-pole DC blocker on the crushed signal.

Ports:
  * ``in`` (audio): signal to crush. Voice-aware; a single voice row is
    bit-identical to the mono render. Unpatched → silence.
  * ``out`` (audio): crushed (and dry-blended) signal.

Neutral: ``bits = 24 ∧ rate_div = 1`` skips **both** the quantizer and
the decimator, so the wet path equals the dry input — a bit-exact
passthrough at any ``mix`` (with ``dc_filter`` off). ``mix = 0`` is
likewise bit-exact dry regardless of the crush settings.

Use cases:
  * Lo-fi drums / breaks: a drum bus at ``rate_div ≈ 4–8`` and ``bits ≈
    8–10`` for that crunchy sampler grit.
  * Digital fuzz lead: ``bits ≈ 2–4``, ``mix = 1`` on a synth line.
  * Broken-converter texture: moderate ``rate_div`` with ``jitter``
    high for an unstable, warbling downsample.

Pairs naturally after a [`vca`](#vca)/envelope and before a
[`filter`](#filter) (a low-pass tames the aliasing into a smoother lo-fi
tone) or a [`delay`](#delay).
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Bitcrusher(Module):
    """Bit-depth quantizer + sample-rate decimator (lo-fi effect).

    Parameters:
        bits: Quantizer word length, 1 … 24. Mid-tread:
            ``round(x·2^(bits−1))/2^(bits−1)``. 24 skips the quantizer
            (bit-exact).
        rate_div: Sample-hold decimation factor, 1 … 64 (hold every
            ``rate_div``-th sample, deliberately aliased). 1 skips
            decimation (bit-exact).
        jitter: Random hold-length wobble around ``rate_div``, 0 … 1, on
            a seeded (reproducible) stream. No effect unless
            ``rate_div > 1``.
        mix: Dry/wet balance, dry (0) → crushed (1). 0 is a bit-exact
            dry passthrough.
        dc_filter: When True, a one-pole DC blocker runs on the crushed
            signal. Off by default.

    Ports:
        in (in, audio): signal to crush. Unpatched → silence.
        out (out, audio): crushed (and dry-blended) signal.
    """

    TYPE = "bitcrusher"
    CATEGORY = "Effects"
    DEFAULT_PARAMS = {
        "bits": 24,
        "rate_div": 1,
        "jitter": 0.0,
        "mix": 1.0,
        "dc_filter": False,
    }
    INPUT_PORTS = [Port("in", "in", "audio")]
    OUTPUT_PORTS = [Port("out", "out", "audio")]
