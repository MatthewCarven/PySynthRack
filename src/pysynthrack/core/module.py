"""Module — base class for every module type (oscillator, filter, output, ...).

A Module is a pure model object. It declares:
  * a type string (used for JSON round-trip and the backend's dispatch table)
  * a set of named parameters with default values
  * input and output ports

It does NOT render audio. The active AudioBackend reads modules out of the
Patch and constructs its own native representation (pyo objects, numpy
callbacks, etc.). This keeps DSP code out of the model layer.

Subclasses declare:

    class Oscillator(Module):
        TYPE = "oscillator"
        DEFAULT_PARAMS = {"waveform": "sine", "freq": 440.0, "amp": 0.5}
        INPUT_PORTS = []
        OUTPUT_PORTS = [Port("out", "out")]

The class-level declarations are copied onto instances at construction.
"""
from __future__ import annotations

from typing import Any, ClassVar

from .port import Port

# Module-type registry. Populated by ``register_module_type`` (called by
# ``modules/__init__.py``). The patch loader uses it to look up a class by type.
_REGISTRY: dict[str, type["Module"]] = {}


def register_module_type(cls: type["Module"]) -> type["Module"]:
    """Class decorator that adds a Module subclass to the type registry."""
    if not cls.TYPE:
        raise ValueError(f"Module subclass {cls.__name__} must set TYPE")
    if cls.TYPE in _REGISTRY:
        # Re-registering the same class (e.g. during a reload) is fine; a
        # collision between two different classes is a bug.
        if _REGISTRY[cls.TYPE] is not cls:
            raise ValueError(
                f"Module type {cls.TYPE!r} already registered to "
                f"{_REGISTRY[cls.TYPE].__name__}"
            )
    _REGISTRY[cls.TYPE] = cls
    return cls


def get_module_type(type_name: str) -> type["Module"]:
    """Look up a Module subclass by its TYPE string."""
    try:
        return _REGISTRY[type_name]
    except KeyError as exc:
        raise KeyError(
            f"Unknown module type {type_name!r}. Registered types: "
            f"{sorted(_REGISTRY)}"
        ) from exc


def all_module_types() -> dict[str, type["Module"]]:
    """Return a copy of the registry — used by the UI palette."""
    return dict(_REGISTRY)


class Module:
    """Base class for every module type.

    Instances are created by the Patch when modules are added; they carry an
    integer ``id`` (assigned by the patch) and a mutable ``params`` dict.
    """

    TYPE: ClassVar[str] = ""
    DEFAULT_PARAMS: ClassVar[dict[str, Any]] = {}
    INPUT_PORTS: ClassVar[list[Port]] = []
    OUTPUT_PORTS: ClassVar[list[Port]] = []

    def __init__(
        self,
        module_id: int,
        name: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        if not self.TYPE:
            raise TypeError(
                f"{type(self).__name__} cannot be instantiated directly — "
                "subclasses must set TYPE."
            )
        self.id: int = module_id
        self.name: str = name if name is not None else self.TYPE
        # Deep-ish copy: params values are scalars/strings for v0.1.
        self.params: dict[str, Any] = dict(self.DEFAULT_PARAMS)
        if params:
            for key, value in params.items():
                if key not in self.DEFAULT_PARAMS:
                    raise KeyError(
                        f"{self.TYPE!r} has no parameter {key!r}. "
                        f"Known params: {sorted(self.DEFAULT_PARAMS)}"
                    )
                self.params[key] = value

    # ----- ports -----------------------------------------------------------

    @property
    def input_ports(self) -> list[Port]:
        return list(self.INPUT_PORTS)

    @property
    def output_ports(self) -> list[Port]:
        return list(self.OUTPUT_PORTS)

    def get_port(self, name: str, direction: str) -> Port:
        """Return the port with the given name and direction or raise KeyError."""
        ports = self.input_ports if direction == "in" else self.output_ports
        for port in ports:
            if port.name == name:
                return port
        raise KeyError(
            f"Module {self.TYPE!r} has no {direction}-port named {name!r}. "
            f"Available {direction}-ports: {[p.name for p in ports]}"
        )

    # ----- params ----------------------------------------------------------

    def set_param(self, name: str, value: Any) -> None:
        if name not in self.DEFAULT_PARAMS:
            raise KeyError(
                f"{self.TYPE!r} has no parameter {name!r}. "
                f"Known params: {sorted(self.DEFAULT_PARAMS)}"
            )
        self.params[name] = value

    def get_param(self, name: str) -> Any:
        return self.params[name]

    # ----- serialization ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.TYPE,
            "name": self.name,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Module":
        module_cls = get_module_type(data["type"])
        return module_cls(
            module_id=data["id"],
            name=data.get("name"),
            params=data.get("params"),
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id} name={self.name!r}>"
