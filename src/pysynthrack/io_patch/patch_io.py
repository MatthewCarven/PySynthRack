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
    """Serialize ``patch`` to ``path`` as pretty-printed JSON.

    Also stamps ``patch.source_path`` with where it went, so a Save-As
    immediately followed by a recompile resolves relative media paths
    against the NEW folder rather than the old one.
    """
    data = patch.to_dict()
    target = Path(path)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    patch.source_path = str(target.resolve())


def load_patch(path: PathLike) -> Patch:
    """Read a patch JSON file and return a Patch.

    The absolute source path is recorded on the returned patch (see
    ``Patch.source_path``) so relative media paths inside it can be
    resolved against the patch's own folder instead of the process
    working directory.
    """
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    patch = Patch.from_dict(data)
    patch.source_path = str(source.resolve())
    return patch


def patch_to_json(patch: Patch) -> str:
    """Return the JSON string without writing to disk (useful in tests)."""
    return json.dumps(patch.to_dict(), indent=2)


def patch_from_json(text: str) -> Patch:
    """Parse a JSON string into a Patch."""
    return Patch.from_dict(json.loads(text))
