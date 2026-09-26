"""Per-family renderer mixins for ``NumpyBackend``.

``numpy_backend.py`` grew to ~22k lines with every module's renderer on one
class. Families move here one at a time as mixins the backend inherits --
verbatim, so behaviour is unchanged by construction and proven by the
block-exact pins and ``tools/render_audit.py``. See docs/architecture.md.
"""
from .clockwork import ClockworkRenderers
from .dynamics import DynamicsRenderers
from .modfx import ModFXRenderers
from .reverb_delay import ReverbDelayRenderers

__all__ = ["ClockworkRenderers", "DynamicsRenderers", "ModFXRenderers", "ReverbDelayRenderers"]
