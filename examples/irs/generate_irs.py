"""Generate license-clean synthetic impulse responses for the Convolver.

These IRs are pure algorithmic synthesis — seeded, decorrelated decaying
noise with a few early reflections and a darkening tail — so they contain no
third-party recording and carry **no license**. They're deliberately kept out
of git (binaries); run this script once to (re)create them next to itself:

    python examples/irs/generate_irs.py

It writes ``room.wav`` / ``hall.wav`` / ``plate.wav`` (44.1 kHz stereo).
``examples/convolver_reverb.json`` points at ``hall.wav``; until you generate
it the Convolver simply passes audio through (an unreadable path is a
transparent insert), so the patch still loads and sounds — just without the
reverb.

The Convolver energy-normalises whatever it loads, so the absolute level here
doesn't matter; these are peak-trimmed only to keep the WAV tidy.
"""
from __future__ import annotations

import os

import numpy as np
from scipy.signal import lfilter

SR = 44100


def _one_pole_lp(x, fc):
    a = 1.0 - np.exp(-2.0 * np.pi * fc / SR)
    return lfilter([a], [1.0, -(1.0 - a)], x)


def _channel(seconds, rt60, seed, tail_lp, predelay_ms, early):
    """One IR channel: pre-delay gap, early reflections, decaying noise tail."""
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    # Exponentially-decaying decorrelated noise (−60 dB at rt60), darkened.
    tail = rng.standard_normal(n) * np.exp(-6.9078 * t / rt60)
    tail = _one_pole_lp(tail, tail_lp)
    ir = np.zeros(n)
    pd = int(predelay_ms * 1e-3 * SR)
    # Sparse early reflections (a little definition before the wash).
    for delay_ms, amp in early:
        i = pd + int(delay_ms * 1e-3 * SR)
        if i < n:
            ir[i] += amp * (1.0 if rng.random() > 0.5 else -1.0)
    if pd < n:
        ir[pd:] += tail[: n - pd]
    return ir


def _stereo(seconds, rt60, seed, tail_lp, predelay_ms, early):
    left = _channel(seconds, rt60, seed, tail_lp, predelay_ms, early)
    right = _channel(seconds, rt60, seed + 977, tail_lp, predelay_ms, early)
    stereo = np.stack([left, right], axis=1)
    peak = float(np.max(np.abs(stereo))) or 1.0
    stereo = (stereo / (peak * 1.02)).astype(np.float32)
    return stereo


# name -> (seconds, rt60, seed, tail_lp Hz, predelay ms, early reflections)
_SPECS = {
    "room": (0.55, 0.32, 1, 7000.0, 4.0,
             [(7, 0.5), (11, 0.4), (17, 0.35), (23, 0.3)]),
    "hall": (2.40, 2.00, 10, 4800.0, 12.0,
             [(13, 0.45), (21, 0.4), (29, 0.35), (41, 0.3), (53, 0.25)]),
    "plate": (1.40, 1.10, 20, 9000.0, 1.0,
              [(3, 0.5), (5, 0.45), (8, 0.4), (12, 0.35)]),
}


def main():
    from scipy.io import wavfile

    here = os.path.dirname(os.path.abspath(__file__))
    for name, spec in _SPECS.items():
        ir = _stereo(*spec)
        path = os.path.join(here, name + ".wav")
        wavfile.write(path, SR, ir)
        print(f"wrote {path}  ({ir.shape[0]} samples, {ir.shape[0] / SR:.2f}s stereo)")


if __name__ == "__main__":
    main()
