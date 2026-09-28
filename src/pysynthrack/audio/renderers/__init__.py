"""Per-family renderer mixins for ``NumpyBackend``.

``numpy_backend.py`` grew to ~22k lines with every module's renderer on one
class. Families move here one at a time as mixins the backend inherits --
verbatim, so behaviour is unchanged by construction and proven by the
block-exact pins and ``tools/render_audit.py``. See docs/architecture.md.
"""
from .clockwork import ClockworkRenderers
from .colour import ColourRenderers
from .cv_routing import CVRoutingRenderers
from .dynamics import DynamicsRenderers
from .eq_filter import EQFilterRenderers
from .io import IORenderers
from .mod_sources import ModSourceRenderers
from .modfx import ModFXRenderers
from .oscillators import OscillatorRenderers
from .physical import PhysicalRenderers
from .pitch_time import PitchTimeRenderers
from .reverb_delay import ReverbDelayRenderers
from .sequencing import SequencingRenderers
from .spectral import SpectralRenderers

__all__ = ["CVRoutingRenderers", "ClockworkRenderers", "ColourRenderers", "DynamicsRenderers", "EQFilterRenderers", "IORenderers", "ModFXRenderers", "ModSourceRenderers", "OscillatorRenderers", "PhysicalRenderers", "PitchTimeRenderers", "ReverbDelayRenderers", "SequencingRenderers", "SpectralRenderers"]
