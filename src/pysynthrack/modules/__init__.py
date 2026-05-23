"""Module type catalog.

Importing this package registers every shipped Module type with the central
registry in ``core.module``. The UI palette and JSON loader both rely on
this registry, so anywhere in the app that needs to know what module types
exist should:

    import pysynthrack.modules  # noqa: F401
"""
from .adsr import ADSR
from .audiotocv import AudioToCV
from .combiner import Combiner
from .crossover import Crossover
from .cvcombiner import CVCombiner
from .cvtoaudio import CVToAudio
from .diskwriter import DiskWriter
from .filter import Filter
from .keyboard import Keyboard
from .lfo import LFO
from .midiinput import MIDIInput
from .mixer import Mixer
from .oscillator import Oscillator
from .output import SpeakerOutput
from .vca import VCA

__all__ = [
    "ADSR",
    "AudioToCV",
    "Combiner",
    "Crossover",
    "CVCombiner",
    "CVToAudio",
    "DiskWriter",
    "Filter",
    "Keyboard",
    "LFO",
    "MIDIInput",
    "Mixer",
    "Oscillator",
    "SpeakerOutput",
    "VCA",
]
