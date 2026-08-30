"""Generate license-clean synthetic samples for the Sampler examples.

Pure algorithmic synthesis - seeded noise, decaying sines and a bit of
filtering - so there is no third-party recording here and nothing carries a
license. Binaries stay out of git (the `examples/irs/` precedent); run this
once to create them next to itself:

    python examples/samples/generate_samples.py

It writes **breaks.wav**: a one-bar 120 BPM drum loop at 44.1 kHz, mono,
deliberately built in four equal quarters so the Sampler's `start`/`end`
region controls line up with musically useful slices:

    0.000 - 0.250   kick
    0.250 - 0.500   snare
    0.500 - 0.750   closed hat (two hits)
    0.750 - 1.000   open hat / crash-ish tail

`examples/sampler_breaks.json` points three samplers at three of those
quarters and triggers each from its own euclidean - a breaks machine built
entirely out of `start`/`end`. Until you run this script the patch still
loads and plays silently (an unreadable path is silence by contract), so
nothing breaks; it just has nothing to play.

Also writes **marimba_c4.wav**: a single struck-bar note at C4 (261.63 Hz),
the pitched counterpart - point a keyboard at a Sampler with `root` = C4
and you have a playable instrument.

And **pad_c4.wav**: three seconds of a sustaining bowed/choral tone, also
at C4, for the Sampler's `loop` mode. The two above are percussive - they
are over long before you let go of the key, which is precisely what a loop
is for. This one swells in and then just keeps going, so
`examples/sampler_mellotron.json` can hold a chord on it indefinitely.
"""
from __future__ import annotations

import os

import numpy as np
from scipy.io import wavfile

SR = 44100
BPM = 120.0
BAR = 4.0 * 60.0 / BPM          # one 4/4 bar in seconds = 2.0 s at 120


def _env(n, attack_s, decay_s):
    """Percussive envelope: short linear rise, exponential fall."""
    t = np.arange(n, dtype=np.float64) / SR
    atk = np.clip(t / max(attack_s, 1e-6), 0.0, 1.0)
    return atk * np.exp(-t / max(decay_s, 1e-6))


def _kick(n, rng):
    t = np.arange(n, dtype=np.float64) / SR
    # Pitch sweep 110 -> 45 Hz: the classic drop.
    f = 45.0 + 65.0 * np.exp(-t / 0.030)
    phase = 2.0 * np.pi * np.cumsum(f) / SR
    body = np.sin(phase) * _env(n, 0.001, 0.170)
    click = rng.standard_normal(n) * _env(n, 0.0002, 0.004) * 0.25
    return body * 0.9 + click


def _snare(n, rng):
    t = np.arange(n, dtype=np.float64) / SR
    tone = (np.sin(2 * np.pi * 185.0 * t) + np.sin(2 * np.pi * 330.0 * t))
    tone *= _env(n, 0.001, 0.045) * 0.35
    noise = rng.standard_normal(n) * _env(n, 0.001, 0.110)
    # Cheap high-pass on the noise: subtract a one-pole lowpass of itself.
    a = 0.55
    lp = np.zeros(n)
    for i in range(1, n):
        lp[i] = a * lp[i - 1] + (1 - a) * noise[i]
    return tone + (noise - lp) * 0.55


def _hat(n, rng, decay=0.045):
    noise = rng.standard_normal(n)
    # Two-pole-ish high-pass by differencing twice: bright and metallic.
    hp = np.diff(np.diff(noise, prepend=0.0), prepend=0.0)
    return hp * _env(n, 0.0003, decay) * 0.30


def build_break(rng):
    """One bar, four quarters, each a different drum."""
    quarter = int(round(BAR / 4 * SR))
    bar = np.zeros(quarter * 4, dtype=np.float64)

    bar[0:quarter] += _kick(quarter, rng)
    bar[quarter:2 * quarter] += _snare(quarter, rng)

    # Quarter three: two closed hats, so a slice of it has internal rhythm.
    third = bar[2 * quarter:3 * quarter]
    half = quarter // 2
    third[0:half] += _hat(half, rng)
    third[half:half + half] += _hat(quarter - half, rng)[:quarter - half]

    # Quarter four: one long open hat.
    bar[3 * quarter:] += _hat(quarter, rng, decay=0.220)

    peak = np.abs(bar).max()
    if peak > 0:
        bar *= 0.89 / peak
    return bar


def build_marimba(rng, freq=261.6255653005986, seconds=1.6):
    """A struck bar: inharmonic partials over a fast-decaying strike."""
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float64) / SR
    # Free-free bar ratios (the xylophone series), the modal module's set.
    out = np.zeros(n)
    for ratio, gain, decay in ((1.0, 1.0, 0.90), (2.76, 0.42, 0.42),
                               (5.40, 0.18, 0.20), (8.93, 0.07, 0.10)):
        out += gain * np.sin(2 * np.pi * freq * ratio * t) * np.exp(-t / decay)
    out += rng.standard_normal(n) * _env(n, 0.0002, 0.003) * 0.06   # mallet
    peak = np.abs(out).max()
    if peak > 0:
        out *= 0.85 / peak
    return out


def build_pad(rng, freq=261.6255653005986, seconds=3.0):
    """A sustaining bowed/choral tone - the loop-mode counterpart.

    Built the opposite way round from the percussive samples above: a slow
    swell into a long body with no decay at all, so `loop` mode has
    something to sustain. Each partial is detuned a few cents and given its
    own slow, shallow vibrato, which keeps a looped middle section from
    sounding frozen the way a static waveform would.

    The partials are deliberately NOT phase-aligned between the loop points
    - the seam does not match, and hiding a seam that does not match is
    exactly what `loop_xfade` is for. A tiny fade sits on the very tail so
    that even the lazy whole-file loop has somewhere soft to wrap.
    """
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float64) / SR
    out = np.zeros(n)
    for k in range(1, 9):
        cents = rng.uniform(-6.0, 6.0)
        rate = rng.uniform(0.17, 0.43)        # Hz - slower than the loop
        depth = rng.uniform(0.002, 0.006)     # fraction of the partial
        phase = rng.uniform(0.0, 2 * np.pi)
        wobble = 1.0 + depth * np.sin(2 * np.pi * rate * t + phase)
        f = freq * k * (2.0 ** (cents / 1200.0)) * wobble
        out += (1.0 / k) * np.sin(2 * np.pi * np.cumsum(f) / SR + phase)

    # Breath: high-passed noise, quiet, so the top is not glassy.
    breath = rng.standard_normal(n)
    a = 0.92
    lp = np.zeros(n)
    for i in range(1, n):
        lp[i] = a * lp[i - 1] + (1 - a) * breath[i]
    out += (breath - lp) * 0.035

    swell = int(0.35 * SR)
    out[:swell] *= np.linspace(0.0, 1.0, swell) ** 1.7
    tail = int(0.05 * SR)
    out[-tail:] *= np.linspace(1.0, 0.0, tail)

    peak = np.abs(out).max()
    if peak > 0:
        out *= 0.80 / peak
    return out


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    rng = np.random.default_rng(20260822)

    for name, data in (
        ("breaks.wav", build_break(rng)),
        ("marimba_c4.wav", build_marimba(rng)),
        ("pad_c4.wav", build_pad(rng)),
    ):
        path = os.path.join(here, name)
        wavfile.write(path, SR, data.astype(np.float32))
        print(f"wrote {path}  ({len(data) / SR:.2f} s, {SR} Hz mono)")


if __name__ == "__main__":
    main()
