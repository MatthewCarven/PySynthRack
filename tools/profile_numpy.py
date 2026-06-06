"""CPU profile of the numpy backend — the pyo go/no-go measurement.

Scenario (as agreed 2026-06-06, TODO wishlist): a 16-voice chord through
the full canonical chain at 512-sample blocks:

    Keyboard(16 held notes) -> Filter -> VCA   (+ ADSR driving the VCA's cv,
                                                gate from the Keyboard)
                                       -> SpeakerOutput

Variants:
  * one run per oscillator flavour: naive ``saw``, ``saw_blep``, ``saw_wt``
  * a "heavy" run: saw_blep chain + bipolar LFO into ``cutoff_cv``
    (per-voice filter sweep — the most expensive realistic patch shape)

We call ``NumpyBackend.render_block(512)`` directly (no PortAudio device
needed) and time each call with ``perf_counter``. The real-time budget for
a block is ``frames / sample_rate`` (512/44100 = 11.61 ms). A backend keeps
up if its **worst** block stays under budget — the audio callback has no
mercy for p99 stragglers, so max matters more than mean.

GC stays enabled: the live audio callback runs with GC on, so spikes it
causes are part of the honest answer.

Run from the repo root (or anywhere — the script finds ``src`` relative to
itself):

    python tools/profile_numpy.py
    python tools/profile_numpy.py --blocks 5000   # longer soak
"""
from __future__ import annotations

import argparse
import platform
import sys
import time
from pathlib import Path

try:
    import pysynthrack  # noqa: F401
except ImportError:  # running from a checkout without install
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SAMPLE_RATE = 44100
BLOCK = 512
BUDGET_MS = BLOCK / SAMPLE_RATE * 1000.0

# A fat 16-note cluster: C major stacked across octaves, low C2 to D#6.
CHORD_16 = [36, 40, 43, 48, 52, 55, 58, 60, 62, 64, 67, 70, 72, 76, 79, 87]


def build_patch(waveform: str, with_lfo: bool) -> tuple[Patch, object]:
    """Canonical chain from examples/keyboard_adsr.json, built in code."""
    patch = Patch()
    kb = patch.add_module(
        "keyboard", params={"octave": 3, "waveform": waveform, "volume": 0.4}
    )
    flt = patch.add_module(
        "filter", params={"mode": "lowpass", "cutoff": 1500.0, "resonance": 2.0}
    )
    adsr = patch.add_module(
        "adsr", params={"attack": 0.02, "decay": 0.15, "sustain": 0.5, "release": 0.4}
    )
    vca = patch.add_module("vca", params={"gain": 1.0})
    out = patch.add_module("speaker_output", params={"gain": 0.8})

    patch.connect(kb.id, "out", flt.id, "in")
    patch.connect(flt.id, "out", vca.id, "audio")
    patch.connect(kb.id, "gate", adsr.id, "gate")
    patch.connect(adsr.id, "cv", vca.id, "cv")
    patch.connect(vca.id, "out", out.id, "in")

    if with_lfo:
        lfo = patch.add_module(
            "lfo",
            params={"waveform": "sine", "rate": 1.5, "depth": 1.5, "bipolar": True},
        )
        patch.connect(lfo.id, "cv", flt.id, "cutoff_cv")

    return patch, kb


def run_scenario(
    label: str, waveform: str, with_lfo: bool, blocks: int, warmup: int
) -> dict:
    patch, kb = build_patch(waveform, with_lfo)
    backend = NumpyBackend(sample_rate=SAMPLE_RATE, block_size=BLOCK)
    backend.compile(patch)
    for note in CHORD_16:
        kb.note_on(note)

    for _ in range(warmup):
        backend.render_block(BLOCK)

    times_ms = np.empty(blocks, dtype=np.float64)
    for i in range(blocks):
        t0 = time.perf_counter()
        buf = backend.render_block(BLOCK)
        times_ms[i] = (time.perf_counter() - t0) * 1000.0
    assert buf is not None and np.isfinite(buf).all(), "render produced non-finite audio"
    assert np.abs(buf).max() > 0.0, "render produced silence — patch is miswired"

    return {
        "label": label,
        "mean": float(np.mean(times_ms)),
        "median": float(np.median(times_ms)),
        "p99": float(np.percentile(times_ms, 99)),
        "max": float(np.max(times_ms)),
        "over_budget": int(np.sum(times_ms > BUDGET_MS)),
        "blocks": blocks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--blocks", type=int, default=2000,
                    help="timed blocks per scenario (default 2000 ≈ 23 s audio)")
    ap.add_argument("--warmup", type=int, default=50,
                    help="untimed warmup blocks (default 50)")
    args = ap.parse_args()

    print(f"PySynthRack numpy-backend CPU profile")
    print(f"  python {platform.python_version()} | numpy {np.__version__} "
          f"| {platform.system()} {platform.machine()}")
    print(f"  {BLOCK} frames @ {SAMPLE_RATE} Hz -> budget {BUDGET_MS:.2f} ms/block")
    print(f"  16-voice chord, {args.blocks} timed blocks per scenario "
          f"(~{args.blocks * BLOCK / SAMPLE_RATE:.0f} s of audio each)\n")

    scenarios = [
        ("saw (naive)        ", "saw", False),
        ("saw_blep           ", "saw_blep", False),
        ("saw_wt             ", "saw_wt", False),
        ("saw_blep + LFO mod ", "saw_blep", True),
    ]

    rows = []
    for label, waveform, with_lfo in scenarios:
        rows.append(run_scenario(label, waveform, with_lfo, args.blocks, args.warmup))

    hdr = f"{'scenario':22} {'mean':>8} {'median':>8} {'p99':>8} {'max':>8} {'cpu%':>6} {'worst%':>7} {'>budget':>8}"
    print(hdr)
    print("-" * len(hdr))
    worst_overall = 0.0
    for r in rows:
        cpu = r["mean"] / BUDGET_MS * 100.0
        worst = r["max"] / BUDGET_MS * 100.0
        worst_overall = max(worst_overall, worst)
        print(f"{r['label']:22} {r['mean']:7.3f}ms {r['median']:7.3f}ms "
              f"{r['p99']:7.3f}ms {r['max']:7.3f}ms {cpu:5.1f}% {worst:6.1f}% "
              f"{r['over_budget']:>5}/{r['blocks']}")

    print()
    if worst_overall < 50.0:
        print(f"VERDICT: numpy keeps up comfortably (worst block {worst_overall:.0f}% "
              f"of budget). No case for a pyo backend on performance grounds.")
    elif worst_overall < 100.0:
        print(f"VERDICT: numpy keeps up but headroom is thin (worst block "
              f"{worst_overall:.0f}% of budget). Pyo case is borderline — consider "
              f"profiling hot modules before committing to the epic.")
    else:
        print(f"VERDICT: numpy misses the deadline (worst block {worst_overall:.0f}% "
              f"of budget, {sum(r['over_budget'] for r in rows)} blocks over). "
              f"The pyo backend has a real performance case.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
