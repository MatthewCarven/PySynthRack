"""Render audit — what did a change do to every example?

Renders every example patch offline to ``.npz``, compares two such runs,
and (for the examples that moved) attributes each move to the commit that
made it. Built for the 2026-09-24 sixth batch, when nine parallel changes
were allowed to move shipped renders and the listening checklist needed to
say which examples actually changed, by how much, and why.

Three subcommands:

    python tools/render_audit.py render  OUT_DIR [--seconds 8] [--block 512]
                                         [--only a,b,c] [--repo PATH]
    python tools/render_audit.py compare BEFORE AFTER [--noise BEFORE_B]
    python tools/render_audit.py bisect  OUT_ROOT COMMIT [COMMIT ...]
                                         --only a,b,c [--seconds 8] [--block 512]

The usual session:

    1. Before touching anything, render twice:
           render audit/before   and   render audit/before_b
       ``compare audit/before audit/before_b`` should say "moved: 0". An
       example that differs between two runs of the SAME code is
       nondeterministic, and ``compare --noise`` marks it so its later
       movement isn't blamed on the change.
    2. Make the change (or merge the batch), then ``render audit/after``.
    3. ``compare audit/before audit/after --noise audit/before_b`` prints a
       markdown table of every moved or new example (max abs diff, the diff
       relative to the example's peak, % of samples touched) and a health
       list: render errors, loader warnings, non-finite output, silence,
       full-scale peaks, DC offset.
    4. For the big movers, ``bisect audit/bis <c1> <c2> ... --only ...``
       renders them at each commit (via a throwaway detached
       ``git worktree``, so your checkout is untouched) and prints, per
       example, which commit moved it and by how much. The FIRST commit is
       the baseline. Commits must be committed; the working tree isn't used.

Determinism: ``np.random.seed(12345)`` before every example (unseeded
noise/LFO modules draw from numpy's global rng), and a 2 s pause after
``compile`` for any patch that mentions a ``.wav`` (samples and IRs load
on a background thread). "Silent" examples are usually ones that wait for
a live key, MIDI or the mic — the audit can't play them.

Set ``OPENBLAS_NUM_THREADS=1`` if numpy won't import on a busy machine.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

SR = 44100
SEED = 12345
REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------- render

def render(repo: Path, out_dir: Path, seconds: float = 8.0, block: int = 512,
           only: set[str] | None = None, quiet: bool = False) -> int:
    """Render every example under ``repo/examples`` into ``out_dir``.

    Imports pysynthrack from ``repo/src`` — so pointing ``repo`` at another
    checkout renders THAT code. Returns the number of render errors.
    """
    src = str(repo / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    import pysynthrack.modules  # noqa: F401 — registers module types
    from pysynthrack.io_patch import load_patch
    from pysynthrack.audio.numpy_backend import NumpyBackend

    out_dir.mkdir(parents=True, exist_ok=True)
    n_blocks = max(1, int(seconds * SR / block))
    errors = 0
    t_all = time.time()
    for path in sorted((repo / "examples").glob("*.json")):
        if only and path.stem not in only:
            continue
        t0 = time.time()
        try:
            np.random.seed(SEED)
            patch = load_patch(path)
            warnings = [str(w) for w in (getattr(patch, "load_warnings", None) or [])]
            be = NumpyBackend(sample_rate=SR, block_size=block)
            be.compile(patch)
            if ".wav" in path.read_text(encoding="utf-8"):
                time.sleep(2.0)  # background media loaders
            master, devs = [], {}
            for _ in range(n_blocks):
                out, dev = be.render_block_multi(block)
                if out is not None:
                    master.append(np.array(out, dtype=np.float64, copy=True))
                for (device, bs), v in (dev or {}).items():
                    devs.setdefault(f"dev:{device}@{bs}", []).append(
                        np.array(v, dtype=np.float64, copy=True))
            arrays = {}
            if master:
                arrays["master"] = np.concatenate(master, axis=0)
            for k, v in devs.items():
                arrays[k] = np.concatenate(v, axis=0)
            np.savez_compressed(out_dir / f"{path.stem}.npz", **arrays)
            (out_dir / f"{path.stem}.meta.json").write_text(json.dumps(
                {"warnings": warnings, "secs": round(time.time() - t0, 2)}))
            if not quiet:
                print(f"ok   {path.stem} {time.time() - t0:.1f}s", flush=True)
        except Exception as e:  # noqa: BLE001 — an audit reports, never stops
            errors += 1
            (out_dir / f"{path.stem}.err.txt").write_text(repr(e))
            print(f"ERR  {path.stem}: {e!r}", flush=True)
    if not quiet:
        print(f"done {time.time() - t_all:.0f}s, {errors} error(s)", flush=True)
    return errors


# -------------------------------------------------------------------- compare

def _load(folder: Path, stem: str) -> dict | None:
    f = folder / f"{stem}.npz"
    if not f.exists():
        return None
    with np.load(f) as z:
        return {k: z[k] for k in z.files}


def _db(x: float) -> float:
    return -999.0 if x <= 0 else 20.0 * np.log10(x)


def health(folder: Path, stem: str, arrays: dict | None) -> list[str]:
    """Problems with one rendered example, as short strings."""
    err = folder / f"{stem}.err.txt"
    if err.exists():
        return ["RENDER ERROR: " + err.read_text()[:120]]
    if arrays is None:
        return ["no render"]
    out = []
    meta = folder / f"{stem}.meta.json"
    if meta.exists():
        w = json.loads(meta.read_text()).get("warnings")
        if w:
            out.append(f"load warnings: {w}")
    for k, v in arrays.items():
        if not np.all(np.isfinite(v)):
            out.append(f"{k}: NON-FINITE")
            continue
        peak = float(np.max(np.abs(v))) if v.size else 0.0
        if peak < 1e-4:
            out.append(f"{k}: SILENT (peak {_db(peak):.0f} dBFS)")
        elif peak >= 0.999:
            out.append(f"{k}: peak {peak:.3f} (at/over full scale)")
        if v.ndim == 2 and v.shape[0]:
            dc = float(np.abs(np.mean(v, axis=0)).max())
            if dc > 0.05:
                out.append(f"{k}: DC offset {dc:.3f}")
    return out


def diff(a: dict, b: dict) -> tuple[float, float]:
    """(max abs diff, % of samples differing) across the shared keys."""
    worst, nd, nk = 0.0, 0, 0
    for k in set(a) | set(b):
        if k not in a or k not in b or a[k].shape != b[k].shape:
            return float("inf"), 100.0
        d = np.abs(a[k] - b[k])
        if d.size:
            worst = max(worst, float(d.max()))
        nd += int(np.count_nonzero(d))
        nk += d.size
    return worst, 100.0 * nd / max(nk, 1)


def compare(before: Path, after: Path, noise: Path | None = None) -> str:
    """A markdown report: moved/new examples, then the health list."""
    stems = sorted({p.stem for p in after.glob("*.npz")}
                   | {p.stem for p in before.glob("*.npz")}
                   | {p.name[:-len(".err.txt")] for p in after.glob("*.err.txt")})
    rows, problems = [], []
    for s in stems:
        a, b = _load(before, s), _load(after, s)
        problems += [(s, m) for m in health(after, s, b)]
        if b is None:
            continue
        if a is None:
            rows.append((s, "NEW", "", "", ""))
            continue
        worst, pct = diff(a, b)
        note = ""
        if noise is not None:
            c = _load(noise, s)
            if c is not None and diff(a, c)[0] > 0:
                note = "nondeterministic"
        if worst == 0:
            rows.append((s, "same", "", "", note))
        else:
            pk = max((float(np.max(np.abs(v))) for v in a.values() if v.size),
                     default=0.0) or 1.0
            rows.append((s, "MOVED", f"{worst:.2e}", f"{_db(worst / pk):.0f} dB",
                         f"{pct:.1f}% samples {note}".strip()))
    n = lambda tag: sum(r[1] == tag for r in rows)  # noqa: E731
    lines = [f"examples: {len(rows)}  moved: {n('MOVED')}  same: {n('same')}  "
             f"new: {n('NEW')}", "",
             "| example | status | max diff | re peak | note |",
             "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r[1] != "MOVED", r[1] != "NEW", r[0])):
        if r[1] != "same":
            lines.append("| " + " | ".join(r) + " |")
    lines += ["", "## health", ""]
    lines += [f"- {s}: {m}" for s, m in problems] or ["- nothing to report"]
    return "\n".join(lines)


# --------------------------------------------------------------------- bisect

def bisect(out_root: Path, commits: list[str], only: set[str],
           seconds: float = 8.0, block: int = 512) -> str:
    """Render ``only`` at each commit; report which step moved each example."""
    out_root.mkdir(parents=True, exist_ok=True)
    for c in commits:
        dest = out_root / c
        if dest.exists() and any(dest.glob("*.npz")):
            continue  # already rendered — bisect is resumable
        with tempfile.TemporaryDirectory(prefix="render_audit_") as tmp:
            wt = Path(tmp) / "wt"
            subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                            "-q", str(wt), c], check=True)
            try:
                # A fresh interpreter per commit: modules imported from one
                # checkout must not leak into the next one's render.
                subprocess.run([sys.executable, __file__, "render", str(dest),
                                "--repo", str(wt), "--seconds", str(seconds),
                                "--block", str(block), "--only", ",".join(only),
                                "--quiet"], check=True)
            finally:
                subprocess.run(["git", "-C", str(REPO), "worktree", "remove",
                                "--force", str(wt)], check=False)
        print(f"rendered {c}", flush=True)
    lines = []
    for s in sorted(only):
        prev, steps = None, []
        for c in commits:
            cur = _load(out_root / c, s)
            if cur is None:
                steps.append(f"{c}: missing")
            elif prev is not None:
                worst, pct = diff(prev, cur)
                if worst > 0:
                    steps.append(f"{c} {worst:.1e} ({pct:.1f}%)")
            prev = cur if cur is not None else prev
        lines.append(f"{s:28s} " + ("; ".join(steps) or "unchanged"))
    return "\n".join(lines)


# ----------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="render every example to OUT_DIR")
    r.add_argument("out_dir", type=Path)
    r.add_argument("--repo", type=Path, default=REPO)
    r.add_argument("--seconds", type=float, default=8.0)
    r.add_argument("--block", type=int, default=512)
    r.add_argument("--only", default="", help="comma-separated example stems")
    r.add_argument("--quiet", action="store_true")

    c = sub.add_parser("compare", help="report what moved between two renders")
    c.add_argument("before", type=Path)
    c.add_argument("after", type=Path)
    c.add_argument("--noise", type=Path, default=None,
                   help="a second render of BEFORE, to flag nondeterminism")

    b = sub.add_parser("bisect", help="attribute moves to commits")
    b.add_argument("out_root", type=Path)
    b.add_argument("commits", nargs="+")
    b.add_argument("--only", required=True, help="comma-separated example stems")
    b.add_argument("--seconds", type=float, default=8.0)
    b.add_argument("--block", type=int, default=512)

    a = ap.parse_args(argv)
    if a.cmd == "render":
        only = {x for x in a.only.split(",") if x} or None
        return 1 if render(a.repo.resolve(), a.out_dir, a.seconds, a.block,
                           only, a.quiet) else 0
    if a.cmd == "compare":
        print(compare(a.before, a.after, a.noise))
        return 0
    print(bisect(a.out_root, a.commits, {x for x in a.only.split(",") if x},
                 a.seconds, a.block))
    return 0


if __name__ == "__main__":
    sys.exit(main())
