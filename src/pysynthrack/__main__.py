"""Entry point.

``python -m pysynthrack`` launches the GUI by default, or the headless CLI
runner with ``--cli``. If DearPyGui is not installed (e.g. no wheel for your
Python yet) the GUI launcher auto-falls back to CLI mode with a hint.
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence


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
        help="(CLI only) Stop after N seconds. Default: wait for Enter.",
    )
    parser.add_argument(
        "--backend",
        choices=("pyo", "numpy"),
        help="Force a specific audio backend. Default: auto-detect.",
    )
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

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
