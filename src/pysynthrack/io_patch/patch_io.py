"""Patch JSON save / load."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Union

# Module subpackage must be imported so its types register themselves before
# we try to load a patch (otherwise ``Patch.from_dict`` raises KeyError for
# unknown module types). The UI also imports it, but the I/O layer should be
# usable without the UI.
import pysynthrack.modules  # noqa: F401

from ..core.patch import Patch

PathLike = Union[str, Path]


def save_patch(patch: Patch, path: PathLike) -> None:
    """Serialize ``patch`` to ``path`` as pretty-printed JSON."""
    data = patch.to_dict()
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_patch(path: PathLike) -> Patch:
    """Read a patch JSON file and return a Patch."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Patch.from_dict(data)


def patch_to_json(patch: Patch) -> str:
    """Return the JSON string without writing to disk (useful in tests)."""
    return json.dumps(patch.to_dict(), indent=2)


def patch_from_json(text: str) -> Patch:
    """Parse a JSON string into a Patch."""
    return Patch.from_dict(json.loads(text))
