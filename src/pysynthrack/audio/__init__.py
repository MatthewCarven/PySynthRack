"""Audio backends.

``pick_backend`` returns the first available backend in preference order:
numpy first (the reference backend, every module), then pyo (parked; most
modules are silent stubs there). Ask for one with ``--backend pyo`` /
``--backend numpy`` on the command line, or ``PYSYNTHRACK_BACKEND=pyo`` /
``=numpy`` in the environment.
"""
from __future__ import annotations

import os

from .backend import AudioBackend
from .numpy_backend import NumpyBackend
from .pyo_backend import PyoBackend

_REGISTRY: dict[str, type[AudioBackend]] = {
    "pyo": PyoBackend,
    "numpy": NumpyBackend,
}

# Preference order — first match wins. numpy first since 2026-11-20
# (Matthew's call): with pyo first, anyone who took the README's optional
# pyo step got a GUI where most modules were silent stubs. pyo is still one
# --backend / PYSYNTHRACK_BACKEND away.
_PREFERENCE = ("numpy", "pyo")


def pick_backend(
    sample_rate: int = 44100,
    block_size: int = 512,
) -> AudioBackend:
    """Pick the highest-priority backend whose dependencies are installed."""
    override = os.environ.get("PYSYNTHRACK_BACKEND")
    if override:
        if override not in _REGISTRY:
            raise ValueError(
                f"Unknown PYSYNTHRACK_BACKEND={override!r}. "
                f"Valid choices: {sorted(_REGISTRY)}"
            )
        cls = _REGISTRY[override]
        if not cls.is_available():
            raise RuntimeError(
                f"PYSYNTHRACK_BACKEND={override!r} was requested but its "
                f"dependencies are not installed."
            )
        return cls(sample_rate=sample_rate, block_size=block_size)

    for name in _PREFERENCE:
        cls = _REGISTRY[name]
        if cls.is_available():
            return cls(sample_rate=sample_rate, block_size=block_size)

    raise RuntimeError(
        "No audio backend is available. Install pyo or sounddevice:\n"
        "  pip install pyo\n"
        "  pip install sounddevice"
    )


__all__ = ["AudioBackend", "NumpyBackend", "PyoBackend", "pick_backend"]
