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
from .bitcrusher import Bitcrusher
from .chorus import Chorus
from .clock import Clock
from .combiner import Combiner
from .compressor import Compressor
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
from .distortion import Distortion
from .waveshaper import Waveshaper
from .diskwriter import DiskWriter
from .fileplayer import FilePlayer
from .filter import Filter
from .flanger import Flanger
from .freq_shifter import FreqShifter
from .keyboard import Keyboard
from .lfo import LFO
from .limiter import Limiter
from .loudness import Loudness
from .micinput import MicInput
from .meter import Meter
from .midiinput import MIDIInput
from .mixer import Mixer
from .motion_eq import MotionEQ
from .noise import Noise
from .noise_gate import NoiseGate
from .oscillator import Oscillator
from .parametric_eq import ParametricEQ
from .phaser import Phaser
from .resampler import Resampler
from .reverb import Reverb
from .ring_mod import RingMod
from .pitch_shifter import PitchShifter
from .output import (
    LeftSpeakerOutput,
    RightSpeakerOutput,
    SpeakerOutput,
    StereoSpeakerOutput,
)
from .samplehold import SampleHold
from .schmitt import Schmitt
from .sequencer import Sequencer
from .fader_seq import FaderSeq
from .sweep_eq import SweepEQ
from .tilt_eq import TiltEQ
from .transient_shaper import TransientShaper
from .vca import VCA
from .vocoder import Vocoder

__all__ = [
    "ADEnvelope",
    "ADSR",
    "AudioToCV",
    "Bitcrusher",
    "Chorus",
    "Clock",
    "Combiner",
    "Compressor",
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
    "Distortion",
    "FilePlayer",
    "Filter",
    "Flanger",
    "FreqShifter",
    "Keyboard",
    "LeftSpeakerOutput",
    "LFO",
    "Limiter",
    "Loudness",
    "MicInput",
    "Meter",
    "MIDIInput",
    "Mixer",
    "MotionEQ",
    "Noise",
    "NoiseGate",
    "Oscillator",
    "ParametricEQ",
    "Phaser",
    "Resampler",
    "Reverb",
    "RingMod",
    "PitchShifter",
    "RightSpeakerOutput",
    "SampleHold",
    "Schmitt",
    "Sequencer",
    "SweepEQ",
    "TiltEQ",
    "TransientShaper",
    "SpeakerOutput",
    "StereoSpeakerOutput",
    "VCA",
    "Vocoder",
    "Waveshaper",
]
