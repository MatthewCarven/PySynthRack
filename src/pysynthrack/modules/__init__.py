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
from .constant import Constant
from .crossover import Crossover
from .cvcombiner import CVCombiner
from .cvoffset import CVOffset
from .cvscale import CVScale
from .cvtoaudio import CVToAudio
from .cvtofrequency import CVToFrequency
from .diskwriter import DiskWriter
from .fileplayer import FilePlayer
from .filter import Filter
from .keyboard import Keyboard
from .lfo import LFO
from .micinput import MicInput
from .midiinput import MIDIInput
from .mixer import Mixer
from .oscillator import Oscillator
from .output import LeftSpeakerOutput, RightSpeakerOutput, SpeakerOutput
from .samplehold import SampleHold
from .schmitt import Schmitt
from .vca import VCA

__all__ = [
    "ADSR",
    "AudioToCV",
    "Combiner",
    "Constant",
    "Crossover",
    "CVCombiner",
    "CVOffset",
    "CVScale",
    "CVToAudio",
    "CVToFrequency",
    "DiskWriter",
    "FilePlayer",
    "Filter",
    "Keyboard",
    "LeftSpeakerOutput",
    "LFO",
    "MicInput",
    "MIDIInput",
    "Mixer",
    "Oscillator",
    "RightSpeakerOutput",
    "SampleHold",
    "Schmitt",
    "SpeakerOutput",
    "VCA",
]
