"""Per-family renderer mixins for ``NumpyBackend``.

``numpy_backend.py`` grew to ~22k lines with every module's renderer on one
class. Families move here one at a time as mixins the backend inherits --
verbatim, so behaviour is unchanged by construction and proven by the
block-exact pins and ``tools/render_audit.py``. See docs/architecture.md.
"""
from .clockwork import ClockworkRenderers

__all__ = ["ClockworkRenderers"]
