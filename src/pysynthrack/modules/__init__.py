"""Module type catalog.

Importing this package registers every shipped Module type with the central
registry in ``core.module``. The UI palette and JSON loader both rely on
this registry, so anywhere in the app that needs to know what module types
exist should:

    import pysynthrack.modules  # noqa: F401
"""
from .ad_envelope import ADEnvelope
from .adsr import ADSR
from .audiotocv import AudioToCV
from .chorus import Chorus
from .clock import Clock
from .combiner import Combiner
from .constant import Constant
from .crossover import Crossover
from .cvcombiner import CVCombiner
from .cvoffset import CVOffset
from .cvscale import CVScale
from .cv_gates import CVGates
from .cv_keyboard import CVKeyboard
from .cvtoaudio import CVToAudio
from .cvtofrequency import CVToFrequency
from .delay import Delay
from .diskwriter import DiskWriter
from .fileplayer import FilePlayer
from .filter import Filter
from .flanger import Flanger
from .keyboard import Keyboard
from .lfo import LFO
from .loudness import Loudness
from .micinput import MicInput
from .meter import Meter
from .midiinput import MIDIInput
from .mixer import Mixer
from .motion_eq import MotionEQ
from .noise import Noise
from .oscillator import Oscillator
from .parametric_eq import ParametricEQ
from .phaser import Phaser
from .resampler import Resampler
from .reverb import Reverb
from .pitch_shifter import PitchShifter
from .output import LeftSpeakerOutput, RightSpeakerOutput, SpeakerOutput
from .samplehold import SampleHold
from .schmitt import Schmitt
from .sequencer import Sequencer
from .sweep_eq import SweepEQ
from .vca import VCA

__all__ = [
    "ADEnvelope",
    "ADSR",
    "AudioToCV",
    "Chorus",
    "Clock",
    "Combiner",
    "Constant",
    "Crossover",
    "CVCombiner",
    "CVGates",
    "CVKeyboard",
    "CVOffset",
    "CVScale",
    "CVToAudio",
    "CVToFrequency",
    "Delay",
    "DiskWriter",
    "FilePlayer",
    "Filter",
    "Flanger",
    "Keyboard",
    "LeftSpeakerOutput",
    "LFO",
    "Loudness",
    "MicInput",
    "Meter",
    "MIDIInput",
    "Mixer",
    "MotionEQ",
    "Noise",
    "Oscillator",
    "ParametricEQ",
    "Phaser",
    "Resampler",
    "Reverb",
    "PitchShifter",
    "RightSpeakerOutput",
    "SampleHold",
    "Schmitt",
    "Sequencer",
    "SweepEQ",
    "SpeakerOutput",
    "VCA",
]
