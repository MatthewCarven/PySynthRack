"""AudioBackend — abstract interface for the audio engine.

The model layer (Patch, Module, Port) is shared. Each concrete backend reads
the patch and renders sound through its own DSP library.

Lifecycle:

    backend = pick_backend()       # picked once at app startup
    backend.compile(patch)         # called when the patch changes structurally
    backend.start()                # begin streaming to the speakers
    ...
    backend.set_param(id, k, v)    # live tweak without re-compiling
    ...
    backend.stop()                 # halt streaming

``compile`` is allowed to be expensive; ``set_param`` should be cheap and
glitch-free where possible.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.patch import Patch


class AudioBackend(ABC):
    """Common interface every audio backend must implement."""

    name: str = "abstract"

    def __init__(self, sample_rate: int = 44100, block_size: int = 512) -> None:
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._running = False

    # ----- capability check ------------------------------------------------

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """True if the dependencies for this backend are importable."""

    # ----- lifecycle -------------------------------------------------------

    @abstractmethod
    def compile(self, patch: Patch) -> None:
        """Translate ``patch`` into the backend's native graph.

        Safe to call while stopped or running; if running, the backend may
        briefly pause to swap in the new graph.
        """

    @abstractmethod
    def start(self) -> None:
        """Begin audio output."""

    @abstractmethod
    def stop(self) -> None:
        """Halt audio output. Idempotent."""

    @abstractmethod
    def set_param(self, module_id: int, name: str, value: Any) -> None:
        """Update a parameter on an already-compiled module."""

    @property
    def is_running(self) -> bool:
        return self._running

    # ----- convenience -----------------------------------------------------

    def __repr__(self) -> str:
        state = "running" if self._running else "stopped"
        return f"<{type(self).__name__} {state} sr={self.sample_rate}>"
