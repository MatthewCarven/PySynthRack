"""Port — an input or output jack on a Module.

Ports are pure data: a name, a direction, and a signal kind. They do not own
their connections — cables live on the Patch. That keeps the model trivially
serializable and avoids back-reference tangles.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["in", "out"]
SignalKind = Literal["audio", "cv", "gate"]


@dataclass(frozen=True)
class Port:
    """A jack on a module.

    Attributes:
        name: Identifier unique within its module (e.g. "freq", "out").
        direction: "in" for an input jack, "out" for an output jack.
        signal_kind: What kind of signal flows here. v0.1 only uses "audio";
            "cv" (control-rate values) and "gate" (note-on/off triggers) are
            placeholders for the envelope and MIDI work in later versions.
    """

    name: str
    direction: Direction
    signal_kind: SignalKind = "audio"

    def is_compatible_with(self, other: "Port") -> bool:
        """Two ports can be cabled together if directions oppose and kinds match."""
        if self.direction == other.direction:
            return False
        return self.signal_kind == other.signal_kind
