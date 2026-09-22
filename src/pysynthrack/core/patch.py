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
    # Opaque UI metadata round-tripped through JSON. The audio model
    # never reads this; the UI uses it for node positions, future view
    # state, etc. Schema by convention:
    #   {"node_positions": {"<module_id>": [x, y]}}
    ui: dict[str, Any] = field(default_factory=dict)
    # Where this patch was last read from or written to, as an absolute
    # path -- set by ``load_patch`` / ``save_patch``, ``None`` for a patch
    # built in memory. Deliberately NOT serialized: it describes where the
    # file *is*, not what it contains, so it must not travel inside it.
    #
    # The audio backend uses it to resolve RELATIVE media paths (a
    # sampler's `path`, a convolver's IR) against the patch's own folder
    # rather than the process working directory, which is what makes a
    # patch play the same whether it was launched from the project root,
    # from `dist/`, or by double-clicking it. See
    # ``NumpyBackend._resolve_media_path``.
    source_path: str | None = None
    # What ``from_dict`` had to throw away to build this patch, one plain
    # ASCII line per dropped cable (see ``cable_problem``). Empty for a
    # clean patch and for anything built in memory. Like ``source_path``
    # it is deliberately NOT serialized -- it describes this *load*, not
    # the instrument -- and it is excluded from ``__eq__`` / ``__repr__``
    # so a patch that dropped a cable still compares equal to the same
    # patch loaded from a tidied file. The UI reports the count on the
    # status line and the detail to the console; see ``App._load_patch_from``.
    load_warnings: list[str] = field(
        default_factory=list, compare=False, repr=False
    )

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
                f"Cannot connect {src.TYPE}.{src_port} ({src_p.signal_kind}) -> "
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

    # ----- validation ------------------------------------------------------

    def cable_problem(self, cable: Cable) -> str | None:
        """Return ``None`` if ``cable`` is legal here, else WHY it is not.

        The checks ``connect`` makes -- both modules present, both ports
        present *in the right direction*, signal kinds compatible -- but
        phrased as a report instead of an exception, so ``from_dict`` can
        drop a bad cable and still say what it dropped.

        One rule of ``connect``'s is deliberately absent: the duplicate-
        destination check. That is a property of a *pair* of cables rather
        than of one, and silently deciding which of two claimants on an
        input jack survives a load would change how an existing patch
        plays. A hand-edited double is left alone (the backend already
        picks one) and stays a job for the editor.

        The returned line is plain ASCII, names both endpoints as
        ``type#id.port``, and is safe to print.
        """
        src = self.modules.get(cable.src_module_id)
        dst = self.modules.get(cable.dst_module_id)
        src_id = (
            f"{src.TYPE}#{cable.src_module_id}" if src is not None
            else f"#{cable.src_module_id}"
        )
        dst_id = (
            f"{dst.TYPE}#{cable.dst_module_id}" if dst is not None
            else f"#{cable.dst_module_id}"
        )
        where = f"{src_id}.{cable.src_port} -> {dst_id}.{cable.dst_port}"
        if src is None:
            return f"{where}: no module with id {cable.src_module_id}"
        if dst is None:
            return f"{where}: no module with id {cable.dst_module_id}"

        outs = {p.name: p for p in src.output_ports}
        ins = {p.name: p for p in dst.input_ports}
        if cable.src_port not in outs:
            backwards = any(p.name == cable.src_port for p in src.input_ports)
            hint = (
                " (that is an IN-port -- the cable is backwards)" if backwards
                else f" (out-ports: {', '.join(sorted(outs)) or 'none'})"
            )
            return f"{where}: {src_id} has no out-port {cable.src_port!r}{hint}"
        if cable.dst_port not in ins:
            backwards = any(p.name == cable.dst_port for p in dst.output_ports)
            hint = (
                " (that is an OUT-port -- the cable is backwards)" if backwards
                else f" (in-ports: {', '.join(sorted(ins)) or 'none'})"
            )
            return f"{where}: {dst_id} has no in-port {cable.dst_port!r}{hint}"

        src_p, dst_p = outs[cable.src_port], ins[cable.dst_port]
        if not src_p.is_compatible_with(dst_p):
            return (
                f"{where}: incompatible signal kinds "
                f"({src_p.signal_kind} -> {dst_p.signal_kind})"
            )
        return None

    # ----- serialization ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": 1,
            "next_id": self._next_id,
            "modules": [m.to_dict() for m in self.modules.values()],
            "cables": [c.to_dict() for c in self.cables],
        }
        # Only emit the UI block when it has content. Keeps minimal patches
        # tidy and makes the schema bump-free for callers that don't care.
        if self.ui:
            out["ui"] = dict(self.ui)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Patch":
        """Rebuild a patch from its JSON dict.

        **Dead cables are dropped, not obeyed and not raised on.** A
        hand-edited or version-drifted file can carry a cable to a port
        that no longer exists (or never did); before 2026-09-22 such a
        cable loaded clean and was silently inert -- the backend simply
        never found a buffer for it -- so the patch greeted its owner
        with "nothing happens" and no clue why. Every cable is now put
        through ``cable_problem`` (the checks ``connect`` makes) and a
        failing one is left out of ``patch.cables`` and recorded as a
        line in ``patch.load_warnings``.

        Loading never raises over a cable: a patch with one dead cable
        must still open and play the rest. It is the CALLER's job to say
        something -- fail-soft only works when something else speaks up
        (the same lesson the relative-media-path fix left behind). The
        GUI prints the lines and puts the count on the status line; the
        CLI prints them; tests read the list directly.

        A patch with no dead cables loads exactly as it always did.
        """
        patch = cls()
        for mod_data in data.get("modules", []):
            module = Module.from_dict(mod_data)
            patch.modules[module.id] = module
        for index, cable_data in enumerate(data.get("cables", [])):
            try:
                cable = Cable.from_dict(cable_data)
            except (KeyError, TypeError, ValueError) as exc:
                patch.load_warnings.append(
                    f"cable {index}: unreadable entry ({type(exc).__name__}: {exc})"
                )
                continue
            problem = patch.cable_problem(cable)
            if problem is None:
                patch.cables.append(cable)
            else:
                patch.load_warnings.append(problem)
        # Preserve next_id so subsequent additions don't collide with reloaded ids.
        max_existing = max(patch.modules, default=0)
        patch._next_id = max(int(data.get("next_id", 0)), max_existing + 1)
        # UI metadata is optional — older patches without it just work.
        ui_data = data.get("ui")
        if isinstance(ui_data, dict):
            patch.ui = dict(ui_data)
        return patch
