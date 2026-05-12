"""Patch — the graph of modules and cables.

The Patch is the single source of truth that the UI edits and the audio
backend compiles. It is plain Python (no audio, no UI) and round-trips to
JSON via ``to_dict`` / ``from_dict``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from .module import Module, get_module_type


@dataclass(frozen=True)
class Cable:
    """A directed connection from one module's output port to another's input."""

    src_module_id: int
    src_port: str
    dst_module_id: int
    dst_port: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "src_module_id": self.src_module_id,
            "src_port": self.src_port,
            "dst_module_id": self.dst_module_id,
            "dst_port": self.dst_port,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cable":
        return cls(
            src_module_id=int(data["src_module_id"]),
            src_port=str(data["src_port"]),
            dst_module_id=int(data["dst_module_id"]),
            dst_port=str(data["dst_port"]),
        )


@dataclass
class Patch:
    """A collection of modules and the cables between them.

    Module IDs are integers assigned in ``add_module``. The patch retains the
    next-id counter so reloading and adding works without collisions.
    """

    modules: dict[int, Module] = field(default_factory=dict)
    cables: list[Cable] = field(default_factory=list)
    _next_id: int = 1

    # ----- modules ---------------------------------------------------------

    def add_module(self, module_type: str, **kwargs: Any) -> Module:
        """Create and register a module by its TYPE string."""
        cls = get_module_type(module_type)
        module_id = self._next_id
        self._next_id += 1
        module = cls(module_id=module_id, **kwargs)
        self.modules[module_id] = module
        return module

    def remove_module(self, module_id: int) -> None:
        """Remove a module and any cables touching it."""
        if module_id not in self.modules:
            raise KeyError(f"No module with id {module_id}")
        del self.modules[module_id]
        self.cables = [
            c
            for c in self.cables
            if c.src_module_id != module_id and c.dst_module_id != module_id
        ]

    def get(self, module_id: int) -> Module:
        return self.modules[module_id]

    def __iter__(self) -> Iterator[Module]:
        return iter(self.modules.values())

    def __len__(self) -> int:
        return len(self.modules)

    # ----- cables ----------------------------------------------------------

    def connect(
        self,
        src_module_id: int,
        src_port: str,
        dst_module_id: int,
        dst_port: str,
    ) -> Cable:
        """Cable an output port to an input port.

        Validates:
          - both modules exist
          - both ports exist with the right direction
          - signal kinds match
          - destination is not already occupied (input jacks are mono — one
            cable in. To sum signals, use a Combiner module.)
        """
        src = self.modules[src_module_id]
        dst = self.modules[dst_module_id]
        src_p = src.get_port(src_port, "out")
        dst_p = dst.get_port(dst_port, "in")
        if not src_p.is_compatible_with(dst_p):
            raise ValueError(
                f"Cannot connect {src.TYPE}.{src_port} ({src_p.signal_kind}) → "
                f"{dst.TYPE}.{dst_port} ({dst_p.signal_kind}): incompatible."
            )
        # Reject duplicate destination — one cable per input jack.
        for existing in self.cables:
            if (
                existing.dst_module_id == dst_module_id
                and existing.dst_port == dst_port
            ):
                raise ValueError(
                    f"{dst.TYPE}.{dst_port} already has an incoming cable from "
                    f"module id {existing.src_module_id}. Disconnect it first."
                )
        cable = Cable(src_module_id, src_port, dst_module_id, dst_port)
        self.cables.append(cable)
        return cable

    def disconnect(
        self,
        src_module_id: int,
        src_port: str,
        dst_module_id: int,
        dst_port: str,
    ) -> bool:
        """Remove a specific cable. Returns True if a cable was removed."""
        for i, cable in enumerate(self.cables):
            if (
                cable.src_module_id == src_module_id
                and cable.src_port == src_port
                and cable.dst_module_id == dst_module_id
                and cable.dst_port == dst_port
            ):
                del self.cables[i]
                return True
        return False

    def cables_into(self, module_id: int) -> list[Cable]:
        return [c for c in self.cables if c.dst_module_id == module_id]

    def cables_out_of(self, module_id: int) -> list[Cable]:
        return [c for c in self.cables if c.src_module_id == module_id]

    # ----- serialization ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "next_id": self._next_id,
            "modules": [m.to_dict() for m in self.modules.values()],
            "cables": [c.to_dict() for c in self.cables],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Patch":
        patch = cls()
        for mod_data in data.get("modules", []):
            module = Module.from_dict(mod_data)
            patch.modules[module.id] = module
        patch.cables = [Cable.from_dict(c) for c in data.get("cables", [])]
        # Preserve next_id so subsequent additions don't collide with reloaded ids.
        max_existing = max(patch.modules, default=0)
        patch._next_id = max(int(data.get("next_id", 0)), max_existing + 1)
        return patch
