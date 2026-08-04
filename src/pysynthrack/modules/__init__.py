"""Module type catalog.

Importing this package registers every shipped Module type with the central
registry in ``core.module``. The UI palette and JSON loader both rely on
this registry, so anywhere in the app that needs to know what module types
exist should:

    import pysynthrack.modules  # noqa: F401
"""
from .ad_envelope import ADEnvelope
from .adsr import ADSR
from .arpeggiator import Arpeggiator
from .audiotocv import AudioToCV
from .bitcrusher import Bitcrusher
from .chaos import Chaos
from .chord import Chord
from .chorus import Chorus
from .clock import Clock
from .clockwork import BernoulliGate, Burst, Euclidean
from .combiner import Combiner
from .compressor import Compressor
from .constant import Constant
from .convolver import Convolver
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
from .drums import HatDrum, KickDrum, SnareDrum
from .fileplayer import FilePlayer
from .filter import Filter
from .flanger import Flanger
from .fm_op import FMOperator
from .freq_shifter import FreqShifter
from .keyboard import Keyboard
from .key_trigger import KeyTrigger
from .lfo import LFO
from .limiter import Limiter
from .logic import Logic
from .loudness import Loudness
from .micinput import MicInput
from .meter import Meter
from .mid_side import MidSide
from .midiinput import MIDIInput
from .mixer import Mixer
from .modal import Modal
from .motion_eq import MotionEQ
from .noise import Noise
from .noise_gate import NoiseGate
from .octaver import Octaver
from .organ import Organ
from .oscillator import Oscillator
from .parametric_eq import ParametricEQ
from .phaser import Phaser
from .pluck import Pluck
from .quantizer import Quantizer
from .resampler import Resampler
from .reverb import Reverb
from .ring_mod import RingMod
from .pitch_shifter import PitchShifter
from .output import (
    BufferedSpecificSpeakerOutput,
    LeftSpeakerOutput,
    RightSpeakerOutput,
    SpeakerOutput,
    SpecificStereoSpeakerOutput,
    StereoSpeakerOutput,
    WarpingBufferedSpeakerOutput,
)
from .samplehold import SampleHold
from .schmitt import Schmitt
from .scope import Scope
from .shift_random import ShiftRandom
from .slew import Slew
from .sequencer import Sequencer
from .fader_seq import FaderSeq
from .sweep_eq import SweepEQ
from .tape import Tape
from .tilt_eq import TiltEQ
from .transient_shaper import TransientShaper
from .vca import VCA
from .vocoder import Vocoder

__all__ = [
    "ADEnvelope",
    "ADSR",
    "Arpeggiator",
    "AudioToCV",
    "BernoulliGate",
    "Bitcrusher",
    "BufferedSpecificSpeakerOutput",
    "Burst",
    "Chaos",
    "Chord",
    "Chorus",
    "Clock",
    "Combiner",
    "Compressor",
    "Constant",
    "Convolver",
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
    "Euclidean",
    "Distortion",
    "FaderSeq",
    "FilePlayer",
    "Filter",
    "Flanger",
    "FMOperator",
    "FreqShifter",
    "HatDrum",
    "Keyboard",
    "KeyTrigger",
    "KickDrum",
    "LeftSpeakerOutput",
    "LFO",
    "Limiter",
    "Logic",
    "Loudness",
    "MicInput",
    "Meter",
    "MidSide",
    "MIDIInput",
    "Mixer",
    "Modal",
    "MotionEQ",
    "Noise",
    "NoiseGate",
    "Octaver",
    "Organ",
    "Oscillator",
    "ParametricEQ",
    "Phaser",
    "Pluck",
    "Quantizer",
    "Resampler",
    "Reverb",
    "RingMod",
    "PitchShifter",
    "RightSpeakerOutput",
    "SampleHold",
    "Schmitt",
    "Scope",
    "ShiftRandom",
    "Slew",
    "SnareDrum",
    "Sequencer",
    "SweepEQ",
    "Tape",
    "TiltEQ",
    "TransientShaper",
    "SpeakerOutput",
    "SpecificStereoSpeakerOutput",
    "StereoSpeakerOutput",
    "VCA",
    "Vocoder",
    "WarpingBufferedSpeakerOutput",
    "Waveshaper",
]
