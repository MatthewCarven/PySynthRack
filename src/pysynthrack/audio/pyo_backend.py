"""PyoBackend — primary audio engine using the pyo DSP library.

pyo is a Python wrapper around a C audio engine. It provides ready-made
oscillator, filter, envelope, and routing primitives that we map onto
PySynthRack's module model.

Why pyo is the primary choice: less DSP code for us to maintain (and to
get wrong), better real-time performance than a pure-numpy callback,
and built-in MIDI / sample-streaming primitives we'll need in v0.3+.
"""
from __future__ import annotations

from typing import Any

from ..core.patch import Patch
from .backend import AudioBackend

try:
    import pyo  # type: ignore
    _HAS_PYO = True
except Exception:  # pragma: no cover - environment-dependent
    pyo = None  # type: ignore[assignment]
    _HAS_PYO = False


# pyo.LFO waveform type codes. See pyo docs for the full list.
_LFO_TYPE = {
    "saw": 3,        # sawtooth (rising)
    "square": 2,
    "triangle": 1,
    "ramp": 0,       # alias for saw_down — not exposed in v0.1
}


class PyoBackend(AudioBackend):
    """pyo-backed engine. Preferred when pyo imports cleanly."""

    name = "pyo"

    def __init__(self, sample_rate: int = 44100, block_size: int = 512) -> None:
        super().__init__(sample_rate=sample_rate, block_size=block_size)
        self._server: Any = None
        # Per-module pyo objects, plus output objects we need to keep
        # alive so pyo doesn't garbage-collect them.
        self._objects: dict[int, Any] = {}
        self._sinks: list[Any] = []
        self._patch: Patch | None = None

    @classmethod
    def is_available(cls) -> bool:
        return _HAS_PYO

    # ----- server lifecycle -----------------------------------------------

    def _ensure_server(self) -> Any:
        if self._server is None:
            # ``buffersize`` here corresponds to our block_size.
            self._server = pyo.Server(
                sr=self.sample_rate,
                buffersize=self.block_size,
                nchnls=2,
                duplex=0,
            ).boot()
        return self._server

    def start(self) -> None:
        if self._running:
            return
        if not _HAS_PYO:
            raise RuntimeError("pyo is not installed — cannot start PyoBackend.")
        if self._patch is None:
            raise RuntimeError("Call compile(patch) before start().")
        self._ensure_server().start()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        try:
            if self._server is not None:
                self._server.stop()
        finally:
            self._running = False

    # ----- compile --------------------------------------------------------

    def compile(self, patch: Patch) -> None:
        was_running = self._running
        # We rebuild the entire graph on every compile. Cheap enough for v0.1
        # and avoids tricky diffing logic. Optimize when patches get big.
        if was_running:
            self.stop()
        self._ensure_server()
        # Drop old objects. pyo cleans them up when the references go.
        self._objects.clear()
        self._sinks.clear()
        self._patch = patch

        # First pass — create every module that has an output port.
        for module in patch:
            obj = self._build_module(module)
            if obj is not None:
                self._objects[module.id] = obj

        # Second pass — wire cables. For audio inputs we use pyo's
        # ``setInput()`` where available, and otherwise reconstruct the sink
        # with the upstream object passed in directly.
        for cable in patch.cables:
            self._wire_cable(cable, patch)

        # Third pass — speaker outputs need a final ``.out()`` call.
        for module in patch.modules.values():
            if module.TYPE in (
                "speaker_output", "left_speaker_output", "right_speaker_output"
            ):
                self._finalize_speaker_output(module, patch)

        if was_running:
            self.start()

    # ----- per-module builders --------------------------------------------

    def _build_module(self, module) -> Any | None:
        if module.TYPE == "oscillator":
            return self._build_oscillator(module)
        if module.TYPE in (
            "speaker_output", "left_speaker_output", "right_speaker_output"
        ):
            # Built in ``_finalize_speaker_output`` after its input is known.
            return None
        if module.TYPE == "keyboard":
            # Dynamic voice allocation in pyo needs a Mixer + per-note Sine
            # rebuilding on note events. Punt for now; v0.3 work.
            print(
                "[PyoBackend] keyboard module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy for keyboard play.",
            )
            return None
        if module.TYPE == "filter":
            # pyo.Biquad maps cleanly but we'd need to wire its input from
            # the upstream cable, which requires a two-pass build. Numpy
            # backend handles filter today; pyo support arrives alongside
            # keyboard in v0.3.
            print(
                "[PyoBackend] filter module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy for filtered sounds.",
            )
            return None
        if module.TYPE == "adsr":
            # pyo.Adsr exists but needs a trigger object derived from the
            # keyboard gate, which the pyo path doesn't generate yet. Land
            # this alongside keyboard support.
            print(
                "[PyoBackend] adsr module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy for envelopes.",
            )
            return None
        if module.TYPE == "vca":
            # VCA is conceptually just multiplication; once filter/keyboard
            # land in pyo, this becomes a pyo.Sig(audio, mul=cv*gain).
            print(
                "[PyoBackend] vca module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy.",
            )
            return None
        if module.TYPE == "lfo":
            # pyo.Sine / pyo.LFO at low frequency would work but needs to
            # output a Sig the VCA/Filter can read; lands with the rest of
            # the CV-aware pyo build.
            print(
                "[PyoBackend] lfo module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy.",
            )
            return None
        if module.TYPE == "mixer":
            # Sum-with-trims is straightforward with pyo.Mix, but the
            # upstream sources (keyboard, filter) aren't built in pyo
            # yet. Lands with the rest of the audio-graph pyo work.
            print(
                "[PyoBackend] mixer module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy.",
            )
            return None
        if module.TYPE in (
            "combiner",
            "cv_combiner",
            "cv_keyboard",
            "crossover",
            "disk_writer",
            "audio_to_cv",
            "cv_to_audio",
            "cv_to_frequency",
            "schmitt",
            "constant",
            "cv_scale",
            "cv_offset",
            "sample_hold",
            "noise",
            "parametric_eq",
            "meter",
            "ad_envelope",
            "chorus",
            "flanger",
            "phaser",
            "delay",
            "reverb",
            "loudness",
            "resampler",
            "pitch_shifter",
            "cv_gates",
            "clock",
            "sequencer",
        ):
            # v0.3+ routing / bridge / CV-oscillator modules. The numpy
            # backend is the real implementation; pyo support arrives
            # when the rest of the v0.2/v0.3/v0.4 graph does. Silent
            # stub until then.
            print(
                f"[PyoBackend] {module.TYPE} module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy.",
            )
            return None
        if module.TYPE == "midi_input":
            # v0.4 MIDI Input. The numpy backend is the real implementation
            # (it owns the mido callback and voice tracking). When the pyo
            # backend graduates from stubs it will use pyo.Notein for the
            # MIDI source — for now: silent stub.
            print(
                "[PyoBackend] midi_input module not yet supported in pyo "
                "backend; the node will be silent. Run with "
                "PYSYNTHRACK_BACKEND=numpy.",
            )
            return None
        return None

    def _build_oscillator(self, module) -> Any:
        waveform = str(module.params.get("waveform", "sine"))
        freq = float(module.params.get("freq", 440.0))
        amp = float(module.params.get("amp", 0.5))
        if waveform == "sine":
            return pyo.Sine(freq=freq, mul=amp)
        if waveform in _LFO_TYPE:
            # LFO at audio rate produces band-limited classic waveforms.
            return pyo.LFO(freq=freq, type=_LFO_TYPE[waveform], mul=amp)
        # Fallback: sine.
        return pyo.Sine(freq=freq, mul=amp)

    # ----- wiring ---------------------------------------------------------

    def _wire_cable(self, cable, patch: Patch) -> None:
        src_obj = self._objects.get(cable.src_module_id)
        if src_obj is None:
            return
        dst_module = patch.modules.get(cable.dst_module_id)
        if dst_module is None:
            return
        if dst_module.TYPE in (
            "speaker_output", "left_speaker_output", "right_speaker_output"
        ):
            # Wiring for speakers happens in ``_finalize_speaker_output``.
            return
        # Future module types (filter, mixer) will set their pyo input here.

    def _finalize_speaker_output(self, module, patch: Patch) -> None:
        incoming = patch.cables_into(module.id)
        if not incoming:
            return
        src_obj = self._objects.get(incoming[0].src_module_id)
        if src_obj is None:
            return
        gain = float(module.params.get("gain", 1.0))
        # Multiplying a pyo object by a number returns a new ``Sig``-like
        # object, but the canonical way to apply scalar gain is the ``mul``
        # attribute. We pipe through a ``Sig`` so we own the output object.
        out_obj = pyo.Sig(src_obj, mul=gain)
        # SpeakerOutput plays on every channel; the Left/Right variants
        # pin the signal to output channel 0 / 1 respectively (pyo's
        # ``chnl`` argument).
        if module.TYPE == "left_speaker_output":
            out_obj.out(chnl=0)
        elif module.TYPE == "right_speaker_output":
            out_obj.out(chnl=1)
        else:
            out_obj.out()
        self._objects[module.id] = out_obj
        self._sinks.append(out_obj)

    # ----- live params ----------------------------------------------------

    def set_param(self, module_id: int, name: str, value: Any) -> None:
        if self._patch is None or module_id not in self._patch.modules:
            return
        module = self._patch.get(module_id)
        # Update the model first so a future ``compile`` is consistent.
        module.set_param(name, value)

        obj = self._objects.get(module_id)
        if obj is None:
            return

        # Apply the change to the live pyo object where possible. Some
        # changes (e.g. switching waveform class) require a recompile.
        if module.TYPE == "oscillator":
            if name == "freq":
                if hasattr(obj, "setFreq"):
                    obj.setFreq(float(value))
                elif hasattr(obj, "freq"):
                    obj.freq = float(value)
            elif name == "amp":
                if hasattr(obj, "setMul"):
                    obj.setMul(float(value))
                else:
                    obj.mul = float(value)
            elif name == "waveform":
                # Swapping waveform changes the pyo class — rebuild.
                self.compile(self._patch)
        elif module.TYPE in (
            "speaker_output", "left_speaker_output", "right_speaker_output"
        ):
            if name == "gain":
                if hasattr(obj, "setMul"):
                    obj.setMul(float(value))
                else:
                    obj.mul = float(value)
