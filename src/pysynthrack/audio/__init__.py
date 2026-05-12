"""Audio backends.

``pick_backend`` returns the first available backend in preference order.
Set ``PYSYNTHRACK_BACKEND=pyo`` or ``=numpy`` to force a specific one;
useful for debugging when both are installed.
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

# Preference order — first match wins.
_PREFERENCE = ("pyo", "numpy")


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
