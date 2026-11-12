"""Entry point.

``python -m pysynthrack`` launches the GUI by default, the headless CLI
runner with ``--cli``, or an offline render to a WAV file with ``--render``. If DearPyGui is not installed (e.g. no wheel for your
Python yet) the GUI launcher auto-falls back to CLI mode with a hint.
"""
from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pysynthrack",
        description="Modular Python software synthesizer.",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run headlessly (no GUI). Loads a patch and plays it.",
    )
    parser.add_argument(
        "--patch",
        metavar="PATH",
        help="Patch JSON file to load. Defaults to examples/hello_sine.json.",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        metavar="N",
        help=(
            "With --cli: stop after N seconds (default: wait for Enter). "
            "With --render: the length of the file, and required."
        ),
    )
    parser.add_argument(
        "--render",
        metavar="OUT.wav",
        help=(
            "Render the patch offline to a 16-bit WAV file instead of playing "
            "it. Opens no audio device. Needs --seconds."
        ),
    )
    parser.add_argument(
        "--mono",
        action="store_true",
        help="(--render only) Write a mono file: the mean of left and right.",
    )
    parser.add_argument(
        "--backend",
        choices=("pyo", "numpy"),
        help="Force a specific audio backend. Default: auto-detect.",
    )
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.render:
        from .cli import render_cli
        return render_cli(
            patch_path=args.patch,
            out_path=args.render,
            seconds=args.seconds,
            mono=args.mono,
            backend_name=args.backend,
        )

    if args.cli:
        from .cli import run_cli
        return run_cli(
            patch_path=args.patch,
            seconds=args.seconds,
            backend_name=args.backend,
        )

    # GUI mode — try to import the UI; if DearPyGui is missing, fall back.
    try:
        from .ui.app import main as gui_main
    except ImportError as exc:
        print(
            "[pysynthrack] DearPyGui is not available "
            f"({type(exc).__name__}: {exc}).\n"
            "  Falling back to headless CLI mode. Use --cli to skip this message,\n"
            "  or install DearPyGui (e.g. `pip install dearpygui`) for the node-editor UI.",
            file=sys.stderr,
        )
        from .cli import run_cli
        return run_cli(
            patch_path=args.patch,
            seconds=args.seconds,
            backend_name=args.backend,
        )

    if args.backend:
        import os
        os.environ["PYSYNTHRACK_BACKEND"] = args.backend
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
