"""DearPyGui app — node editor, palette, file menu, transport.

The UI is intentionally a thin layer over the model. Every user action is
translated into a model mutation (``Patch.add_module``, ``Patch.connect``,
``module.set_param``) and then the backend is told to recompile or update.
That means the model is always the source of truth and the same operations
work whether they came from the GUI, a loaded patch, or a future scripting
interface.
"""
from __future__ import annotations

import math
import os
import sys
import traceback
from typing import Optional

import dearpygui.dearpygui as dpg

# Ensure all module types are registered before we build any UI.
import pysynthrack.modules  # noqa: F401

from ..audio import AudioBackend, pick_backend
from ..core.module import all_module_types
from ..core.patch import Cable, Patch
from ..io_patch import load_patch, save_patch
from ..modules.filter import FILTER_MODES
from ..modules.keyboard import midi_to_name, semitone_to_midi
from ..modules.cvcombiner import CVCOMBINER_MODES
from ..modules.cvtofrequency import MODES as CVTOFREQ_MODES
from ..modules.lfo import LFO_WAVEFORMS
from ..modules.midiinput import AUTO_DEVICE, available_devices as midi_available_devices
from ..modules.micinput import available_input_devices as mic_available_devices
from ..modules.noise import NOISE_COLORS
from ..modules.oscillator import WAVEFORMS
from .zoom import (
    ZOOM_DEFAULT,
    ZOOM_MAX,
    ZOOM_MIN,
    clamp_zoom,
    factor_to_percent,
    percent_to_factor,
    scale_pos,
    step_zoom,
)


# Computer-keyboard → semitone-offset mapping. Home row A..K = white keys
# of one octave; W E T Y U = black keys. The upper row K L ; spans the
# next octave so chord shapes feel natural.
_KEY_TO_SEMITONE: dict[int, int] = {}


def _init_key_map(dpg_module) -> None:
    """Build _KEY_TO_SEMITONE lazily using the real dpg.mvKey_* constants.

    Done at runtime because dearpygui.dearpygui resolves these as
    attributes; doing it at import time would fail when running headless.
    """
    if _KEY_TO_SEMITONE:
        return
    pairs = [
        ("A", 0),  ("W", 1),  ("S", 2),  ("E", 3),  ("D", 4),
        ("F", 5),  ("T", 6),  ("G", 7),  ("Y", 8),  ("H", 9),
        ("U", 10), ("J", 11), ("K", 12), ("O", 13), ("L", 14),
        ("P", 15), ("Semicolon", 16),
    ]
    for name, semitone in pairs:
        attr = f"mvKey_{name}"
        key_code = getattr(dpg_module, attr, None)
        if key_code is None:
            continue
        _KEY_TO_SEMITONE[key_code] = semitone

EDITOR_TAG = "node_editor"
AUDIO_BTN_TAG = "audio_btn"
STATUS_TEXT_TAG = "status_text"
MAIN_WINDOW_TAG = "main_window"
ZOOM_SLIDER_TAG = "zoom_slider"

from .._resources import examples_dir

DEFAULT_PATCH_PATH = str(examples_dir() / "hello_sine.json")


class App:
    """Top-level GUI controller."""

    def __init__(self) -> None:
        self.patch: Patch = Patch()
        self.backend: AudioBackend = pick_backend()

        # DPG-id ↔ model bookkeeping. Used to translate UI events back into
        # patch operations and to recreate the visuals when loading a patch.
        self._node_to_module: dict[int, int] = {}            # dpg_node_id → module_id
        self._module_to_node: dict[int, int] = {}            # module_id → dpg_node_id
        self._attr_to_port: dict[int, tuple[int, str, str]] = {}  # attr_id → (mod, port, dir)
        self._port_to_attr: dict[tuple[int, str, str], int] = {}  # (mod, port, dir) → attr_id
        self._link_to_cable: dict[int, Cable] = {}

        # The file_player node whose Browse button was last clicked, so the
        # shared WAV file dialog's callback knows which module to update.
        self._wav_target_id: Optional[int] = None

        # Diagonal stagger for newly-added nodes so they don't stack.
        self._next_node_pos = [40, 40]

        # Current UI scale ("zoom") factor; 1.0 == 100 %. imnodes has no
        # real canvas zoom, so we fake it: scale the global font (the
        # auto-sized nodes grow/shrink with it) and rescale every node's
        # position by the same factor so spacing — and cable length —
        # tracks the size. See ui/zoom.py for the dpg-free maths.
        self._zoom: float = ZOOM_DEFAULT

        # Track which physical keys are currently down so OS auto-repeat
        # doesn't fire note_on repeatedly while a key is held.
        self._held_keys: set[int] = set()

        # CV meters. For each cv-kind output port we draw a progress bar
        # under its node attribute; ``_cv_meter_bars`` maps the port key
        # (module_id, port_name) to that bar's dpg tag. ``_meter_bounds``
        # holds the per-port auto-range state [lo, hi] used to normalise
        # the fill (instant-attack / slow-release; see _auto_range_fill).
        self._cv_meter_bars: dict[tuple[int, str], int] = {}
        # Meter-module level bars: module_id -> progress-bar tag.
        self._audio_meter_bars: dict[int, int] = {}
        self._meter_bounds: dict[tuple[int, str], list[float]] = {}
        # FilePlayer playhead readouts. Maps module_id -> the dpg text
        # tag showing 'elapsed / total'; refreshed each frame in
        # _update_file_positions from the backend's snapshot hook.
        self._file_pos_labels: dict[int, int] = {}

    # ----- entry point ----------------------------------------------------

    def run(self) -> None:
        dpg.create_context()
        try:
            self._build_ui()
            dpg.create_viewport(title="PySynthRack v0.1", width=1280, height=800)
            dpg.setup_dearpygui()
            dpg.show_viewport()
            dpg.set_primary_window(MAIN_WINDOW_TAG, True)
            # Auto-load the hello-sine patch so first-run users see something.
            if os.path.isfile(DEFAULT_PATCH_PATH):
                try:
                    self._load_patch_from(DEFAULT_PATCH_PATH)
                except Exception:
                    traceback.print_exc()
            self._set_status(f"Backend: {self.backend.name}  |  sr={self.backend.sample_rate}")
            # Manual render loop (vs dpg.start_dearpygui) so each frame can
            # push fresh CV meter levels from the audio thread into the
            # progress bars. render_dearpygui_frame paces itself to the
            # viewport's vsync, so this is no busier than the built-in loop.
            while dpg.is_dearpygui_running():
                self._update_cv_meters()
                self._update_audio_meters()
                self._update_file_positions()
                dpg.render_dearpygui_frame()
        finally:
            try:
                self.backend.stop()
            except Exception:
                pass
            dpg.destroy_context()

    # ----- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        with dpg.window(label="PySynthRack", tag=MAIN_WINDOW_TAG):
            with dpg.menu_bar():
                with dpg.menu(label="File"):
                    dpg.add_menu_item(label="New patch", callback=self._on_new)
                    dpg.add_menu_item(label="Open...", callback=self._on_open)
                    dpg.add_menu_item(label="Save as...", callback=self._on_save)
                    dpg.add_separator()
                    dpg.add_menu_item(label="Quit", callback=lambda: dpg.stop_dearpygui())
                with dpg.menu(label="Add module"):
                    for type_name in sorted(all_module_types()):
                        dpg.add_menu_item(
                            label=type_name,
                            callback=self._on_add_module,
                            user_data=type_name,
                        )

            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Start audio",
                    tag=AUDIO_BTN_TAG,
                    callback=self._on_toggle_audio,
                )
                dpg.add_text("", tag=STATUS_TEXT_TAG)
                dpg.add_spacer(width=24)
                dpg.add_text("Zoom")
                dpg.add_slider_int(
                    tag=ZOOM_SLIDER_TAG,
                    width=150,
                    min_value=int(ZOOM_MIN * 100),
                    max_value=int(ZOOM_MAX * 100),
                    default_value=int(ZOOM_DEFAULT * 100),
                    clamped=True,
                    format="%d%%",
                    callback=self._on_zoom_slider,
                )
                dpg.add_button(label="Reset", callback=self._on_zoom_reset)

            dpg.add_separator()
            dpg.add_text(
                "Drag from an output jack (right) to an input jack (left) to "
                "patch a cable. Click a cable or node and press Delete to "
                "remove it.",
                color=(180, 180, 180),
            )

            dpg.add_node_editor(
                tag=EDITOR_TAG,
                callback=self._on_link_created,
                delink_callback=self._on_link_deleted,
                minimap=True,
            )

        self._build_file_dialogs()

        # Global key handlers — only fire when the viewport has focus.
        # Used by Keyboard modules to translate physical key presses into
        # MIDI note-on / note-off events, and by the node editor for
        # Delete-to-remove-selected.
        _init_key_map(dpg)
        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key_press)
            dpg.add_key_release_handler(callback=self._on_key_release)
            # Delete key removes whatever's currently selected in the node
            # editor (cables + nodes). Backspace as a forgiving alternative.
            del_key = getattr(dpg, "mvKey_Delete", None)
            if del_key is not None:
                dpg.add_key_press_handler(
                    key=del_key, callback=self._on_delete_selected
                )
            back_key = getattr(dpg, "mvKey_Back", None)
            if back_key is not None:
                dpg.add_key_press_handler(
                    key=back_key, callback=self._on_delete_selected
                )
            # Ctrl+= / Ctrl+- / Ctrl+0 and Ctrl+mouse-wheel drive the UI
            # zoom. Each callback re-checks Ctrl so a bare key still
            # reaches the keyboard-as-MIDI handler untouched. Register
            # every +/- spelling DPG exposes (main row and numpad).
            for _name in ("mvKey_Plus", "mvKey_Add"):
                _k = getattr(dpg, _name, None)
                if _k is not None:
                    dpg.add_key_press_handler(key=_k, callback=self._on_zoom_in_key)
            for _name in ("mvKey_Minus", "mvKey_Subtract"):
                _k = getattr(dpg, _name, None)
                if _k is not None:
                    dpg.add_key_press_handler(key=_k, callback=self._on_zoom_out_key)
            for _name in ("mvKey_0", "mvKey_NumPad0"):
                _k = getattr(dpg, _name, None)
                if _k is not None:
                    dpg.add_key_press_handler(key=_k, callback=self._on_zoom_reset_key)
            dpg.add_mouse_wheel_handler(callback=self._on_zoom_wheel)

    def _build_file_dialogs(self) -> None:
        with dpg.file_dialog(
            label="Open patch",
            show=False,
            callback=self._on_open_selected,
            tag="open_dialog",
            width=700,
            height=500,
            default_path=os.path.dirname(DEFAULT_PATCH_PATH),
        ):
            dpg.add_file_extension(".json", color=(150, 220, 255))
            dpg.add_file_extension(".*")

        with dpg.file_dialog(
            label="Save patch as",
            show=False,
            callback=self._on_save_selected,
            tag="save_dialog",
            width=700,
            height=500,
            default_path=os.path.dirname(DEFAULT_PATCH_PATH),
            default_filename="patch.json",
        ):
            dpg.add_file_extension(".json", color=(150, 220, 255))
            dpg.add_file_extension(".*")

        # Shared by every FilePlayer node's Browse button. One dialog is
        # enough because picking a file is modal; ``_wav_target_id`` records
        # which node asked, set just before the dialog is shown.
        with dpg.file_dialog(
            label="Select WAV file",
            show=False,
            callback=self._on_wav_selected,
            tag="wav_dialog",
            width=700,
            height=500,
        ):
            # Audio + video containers ffmpeg can pull an audio track from
            # (when the [media] extra or a system ffmpeg is present). Plain
            # .wav always works; the rest fall back to silence without ffmpeg.
            dpg.add_file_extension(
                "Audio/Video{.wav,.mp3,.flac,.ogg,.m4a,.aac,.wma,.mp4,.m4v,.mov,.mkv,.webm,.avi}",
                color=(150, 220, 255),
            )
            dpg.add_file_extension(".wav", color=(150, 220, 255))
            dpg.add_file_extension(".*")

    # ----- node creation --------------------------------------------------

    def _create_node_for_module(self, module, pos=None) -> int:
        if pos is None:
            pos = (self._next_node_pos[0], self._next_node_pos[1])
            # Stagger downward and right for the next node.
            self._next_node_pos[0] = (self._next_node_pos[0] + 220) % 800
            self._next_node_pos[1] = (self._next_node_pos[1] + 60) % 500

        # Place at the logical position scaled by the current zoom so a
        # node added (or loaded) while zoomed lands in the right spot.
        with dpg.node(
            label=f"{module.name} (#{module.id})",
            parent=EDITOR_TAG,
            pos=scale_pos(pos, self._zoom),
        ) as node_id:
            self._node_to_module[node_id] = module.id
            self._module_to_node[module.id] = node_id

            # Inputs (left)
            for port in module.input_ports:
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Input,
                ) as attr_id:
                    dpg.add_text(f"◀ {port.name}")
                self._attr_to_port[attr_id] = (module.id, port.name, "in")
                self._port_to_attr[(module.id, port.name, "in")] = attr_id

            # Parameters (static, no circle)
            for param_name, default in module.DEFAULT_PARAMS.items():
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    self._add_param_widget(module, param_name, default)

            # FilePlayer: a live 'elapsed / total' playhead readout. Audio
            # outputs carry no CV meter, so this text is the one bit of
            # transport feedback on the node; _update_file_positions ticks
            # it each frame.
            if module.TYPE == "file_player":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    label = dpg.add_text("0:00 / 0:00")
                    self._file_pos_labels[module.id] = label

            # Meter: a dBFS level bar (-90..0), driven each frame from
            # the backend's peak envelope.
            if module.TYPE == "meter":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    bar = dpg.add_progress_bar(
                        default_value=0.0, overlay="-inf dB", width=160
                    )
                    self._audio_meter_bars[module.id] = bar

            # Outputs (right)
            for port in module.output_ports:
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output,
                ) as attr_id:
                    dpg.add_text(f"{port.name} ▶")
                    # A live meter for CV outputs: a 0..1 bar whose fill
                    # is auto-ranged to the source's recent swing, with
                    # the actual current value printed as the overlay.
                    # Audio outputs get no meter (they'd peg at audio
                    # rate and mean nothing at a glance).
                    if port.signal_kind == "cv":
                        bar = dpg.add_progress_bar(
                            default_value=0.0,
                            overlay="--",
                            width=120,
                        )
                        self._cv_meter_bars[(module.id, port.name)] = bar
                self._attr_to_port[attr_id] = (module.id, port.name, "out")
                self._port_to_attr[(module.id, port.name, "out")] = attr_id

        return node_id

    def _add_param_widget(self, module, param_name: str, default) -> None:
        current = module.params[param_name]
        user_data = (module.id, param_name)

        if module.TYPE == "file_player" and param_name == "path":
            # Path field + a Browse button that opens the shared WAV
            # dialog. The field keeps an explicit tag so the dialog's
            # callback can write the chosen path back into it; typing a
            # path by hand still works via the same _on_param_changed.
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    label=param_name,
                    default_value=str(current),
                    width=140,
                    tag=f"fileplayer_path_{module.id}",
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                dpg.add_button(
                    label="Browse...",
                    callback=self._show_wav_dialog,
                    user_data=module.id,
                )
            return

        if module.TYPE == "cv_gates":
            # Per-key ADSR, shared across the whole gate bank. attack /
            # decay / release are times in seconds; sustain is a 0..1 held
            # level. Bounded sliders rather than the generic drag floats.
            if param_name in ("attack", "decay", "release"):
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=0.0,
                    max_value=5.0,
                    format="%.3f s",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "sustain":
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=0.0,
                    max_value=1.0,
                    format="%.2f",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return

        if module.TYPE == "clock":
            # Tempo metronome: bpm, pulses-per-beat division, duty cycle.
            if param_name == "bpm":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=20.0, max_value=300.0, format="%.1f BPM",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "division":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.25,
                    min_value=0.25, max_value=16.0, format="%.2f /beat",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "pulse_width":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.01, max_value=0.99, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "sequencer":
            # steps = loop length (int); step{i}_pitch = semitones (drag);
            # step{i}_on = rest toggle (falls through to the generic checkbox).
            if param_name == "steps":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=16,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name.endswith("_pitch"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=-24.0, max_value=24.0, format="%.0f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "parametric_eq":
            # Parametric EQ bands: ``band{i}_freq`` (Hz), ``band{i}_gain``
            # (dB, 0 = flat), ``band{i}_q`` (width). Distinct ranges from
            # the generic numeric fallbacks below.
            if param_name.endswith("_freq"):
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=1.0,
                    min_value=20.0,
                    max_value=20000.0,
                    format="%.0f Hz",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name.endswith("_gain"):
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=-24.0,
                    max_value=24.0,
                    format="%.1f dB",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name.endswith("_q"):
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=0.1,
                    max_value=20.0,
                    format="%.2f",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return

        if module.TYPE == "pitch_shifter":
            # Granular WSOLA transpose controls. semitones+cents set the
            # shift; cv_depth scales pitch_cv; mix is dry/wet; grain_size
            # and overlap shape the grain engine.
            if param_name == "semitones":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-24.0, max_value=24.0, format="%.2f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cents":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-100.0, max_value=100.0, format="%.0f ct",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.0, max_value=48.0, format="%.1f st/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "mix":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "grain_size":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=10.0, max_value=200.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "overlap":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=2, max_value=4,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "resampler":
            # Varispeed transpose controls. Pitch is set in semitones
            # (C->D = +2) with a cents fine-tune on top; cv_depth scales
            # the pitch_cv input (semitones per unit, 12 = one octave);
            # glide ramps pitch changes into portamento.
            if param_name == "semitones":
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=-24.0,
                    max_value=24.0,
                    format="%.2f st",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "cents":
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=-100.0,
                    max_value=100.0,
                    format="%.0f ct",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.1,
                    min_value=0.0,
                    max_value=48.0,
                    format="%.1f st/unit",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "glide":
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.005,
                    min_value=0.0,
                    max_value=5.0,
                    format="%.3f s",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return

        if module.TYPE == "chorus":
            # Stereo chorus: rate is the LFO speed (Hz); depth is the
            # sweep amount (0..1); voices sets how many detuned copies;
            # mix is dry/wet; cv_depth scales rate_cv (octaves per unit).
            if param_name == "rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.05, max_value=10.0, format="%.2f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "depth":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "voices":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=6,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "mix":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "flanger":
            # Swept comb flanger. ``rate`` is the LFO speed (Hz); ``depth``
            # is the sweep width (0..1); ``manual`` is the centre delay in
            # ms; ``feedback`` is bipolar regeneration (-0.95..0.95, hollow
            # <-> ringing); ``mix`` is dry/wet; ``cv_depth`` scales rate_cv
            # (octaves per unit).
            if param_name == "rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.05, max_value=10.0, format="%.2f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "depth":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "manual":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.1, max_value=10.0, format="%.2f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "feedback":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-0.95, max_value=0.95, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "mix":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "phaser":
            # Swept allpass notch phaser. ``rate`` is the LFO speed (Hz);
            # ``depth`` is the sweep width in octaves (0..1); ``center`` is
            # the sweep centre frequency in Hz; ``feedback`` is bipolar
            # resonance (-0.95..0.95, hollow <-> vocal); ``stages`` is the
            # allpass count 4/6/8 (2/3/4 notches); ``mix`` is dry/wet;
            # ``cv_depth`` scales rate_cv (octaves per unit).
            if param_name == "rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.05, max_value=10.0, format="%.2f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "depth":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "center":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=5.0,
                    min_value=100.0, max_value=6000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "feedback":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-0.95, max_value=0.95, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "stages":
                dpg.add_combo(
                    label=param_name, items=["4", "6", "8"],
                    default_value=str(int(round(float(current)))),
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "mix":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "delay":
            # Echo controls. ``time`` is the delay in ms; ``feedback`` sets
            # how many repeats; ``tone`` damps the feedback path (dark <->
            # bright); ``mix`` is dry/wet; ``cv_depth`` scales the time_cv
            # input in ms per unit.
            if param_name == "time":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=1.0, max_value=2000.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "feedback":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=0.98, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "tone":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "mix":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=0.0, max_value=2000.0, format="%.0f ms/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "reverb":
            # Stereo FDN reverb: four 0..1 macro controls (size, decay,
            # damping, mix).
            if param_name in ("size", "decay", "damping", "mix"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "loudness":
            # Equal-loudness contour: level drives the auto curve; bass/
            # treble are manual dB trims; cv_depth scales level_cv.
            if param_name == "level":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("bass", "treble"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-12.0, max_value=12.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=2.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if param_name == "waveform":
            # LFO has its own waveform list (includes "random"); other
            # modules share the oscillator's list.
            items = (
                list(LFO_WAVEFORMS) if module.TYPE == "lfo" else list(WAVEFORMS)
            )
            dpg.add_combo(
                label=param_name,
                items=items,
                default_value=str(current),
                width=120,
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        if param_name in {"mode", "mode_neg"}:
            # ``mode`` means different things on different modules:
            # filter picks LP/HP/BP, cv_combiner picks sum/average,
            # cv_to_frequency picks log/linear (and is the only module
            # with a ``mode_neg``). The cv_to_frequency arm also fixes
            # a phase-1 drive-by: its mode combo wrongly listed the
            # filter's items before 2026-06-07.
            if module.TYPE == "cv_combiner":
                items = list(CVCOMBINER_MODES)
            elif module.TYPE == "cv_to_frequency":
                items = list(CVTOFREQ_MODES)
            else:
                items = list(FILTER_MODES)
            dpg.add_combo(
                label=param_name,
                items=items,
                default_value=str(current),
                width=120,
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        if param_name == "color":
            # Noise color picker (white / pink).
            dpg.add_combo(
                label=param_name,
                items=list(NOISE_COLORS),
                default_value=str(current),
                width=120,
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        # Integer octave selector — keep before the generic int/float case
        # so it isn't treated as a free-range float.
        if param_name == "octave":
            dpg.add_slider_int(
                label=param_name,
                default_value=int(current),
                min_value=0,
                max_value=8,
                width=140,
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        # MIDIInput device selector. Snapshot the available devices once
        # at widget creation; the user can recompile (delete + re-add the
        # module, or reopen the patch) to refresh after hot-plugging. An
        # empty string at the top is the "auto-pick first available" path,
        # so saved patches that don't pin a device still load and run.
        if param_name == "device" and module.TYPE in ("midi_input", "mic_input"):
            # Snapshot the relevant device list at widget creation; the
            # user recompiles (delete+re-add, or reopen the patch) to
            # refresh after hot-plugging. MIDIInput lists MIDI ports;
            # MicInput lists audio capture devices.
            if module.TYPE == "midi_input":
                devices = midi_available_devices()
            else:
                devices = mic_available_devices()
            items = [AUTO_DEVICE] + devices
            current_str = str(current)
            if current_str not in items:
                items = items + [current_str]
            dpg.add_combo(
                label=param_name,
                items=items,
                default_value=current_str,
                width=200,
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        if param_name == "octave_shift":
            # MIDIInput transpose, integer ± octaves around 0.
            dpg.add_slider_int(
                label=param_name,
                default_value=int(current),
                min_value=-4,
                max_value=4,
                width=140,
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        if param_name == "channel":
            # MIDI channel: 0 means "all channels" (omni); 1-16 is the
            # standard hardware numbering.
            dpg.add_slider_int(
                label=param_name,
                default_value=int(current),
                min_value=0,
                max_value=16,
                width=140,
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        if isinstance(default, bool):
            dpg.add_checkbox(
                label=param_name,
                default_value=bool(current),
                callback=self._on_param_changed,
                user_data=user_data,
            )
            return

        if isinstance(default, (int, float)):
            # Pick a sane range per param. Tweak as more module types arrive.
            if param_name in {"freq", "cutoff", "frequency"}:
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=1.0,
                    min_value=20.0,
                    max_value=20000.0,
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
            elif param_name == "resonance":
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=0.1,
                    max_value=15.0,
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
            elif param_name in {"attack", "decay", "release"}:
                # Envelope time in seconds. Drag-float with a fine speed so
                # users can dial in milliseconds; range 0..5 s covers
                # everything from clicky to pad-style.
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.005,
                    min_value=0.0,
                    max_value=5.0,
                    format="%.3f s",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
            elif (
                param_name
                in {"amp", "gain", "volume", "sustain", "depth", "master", "high", "low"}
                or param_name.startswith("gain")
            ):
                # Mixer channel trims (gain1..gain4) and master live in
                # the same 0..2 range as the lone "gain" param.
                hot_range = param_name in {"gain", "master"} or param_name.startswith("gain")
                dpg.add_slider_float(
                    label=param_name,
                    default_value=float(current),
                    min_value=0.0,
                    max_value=2.0 if hot_range else 1.0,
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
            elif param_name == "rate":
                # LFO rate. Drag-float covers tremolo (~3–8 Hz),
                # slow filter sweeps (sub-Hz), and audio-rate FM (>20 Hz)
                # without needing log scaling.
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.1,
                    min_value=0.01,
                    max_value=120.0,
                    format="%.2f Hz",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
            elif param_name in {"value", "scale", "offset"}:
                # CV-utility trio amounts (Constant.value, CVScale.scale,
                # CVOffset.offset). Fine-grained drag with soft +/-10
                # bounds: covers +/-1 modulation depths and several
                # octaves of 1V/oct pitch voltage alike.
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.01,
                    min_value=-10.0,
                    max_value=10.0,
                    format="%.3f",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            else:
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
            return

        # Fallback: input_text for anything we don't recognize.
        dpg.add_input_text(
            label=param_name,
            default_value=str(current),
            width=140,
            callback=self._on_param_changed,
            user_data=user_data,
        )

    # ----- callbacks ------------------------------------------------------

    def _on_add_module(self, sender, app_data, user_data) -> None:
        module_type: str = user_data
        try:
            module = self.patch.add_module(module_type)
        except Exception as exc:
            self._set_status(f"Could not add {module_type}: {exc}")
            return
        self._create_node_for_module(module)
        self._recompile_if_running()

    def _on_link_created(self, sender, app_data) -> None:
        out_attr, in_attr = app_data
        out_info = self._attr_to_port.get(out_attr)
        in_info = self._attr_to_port.get(in_attr)
        if out_info is None or in_info is None:
            return
        src_mod, src_port, src_dir = out_info
        dst_mod, dst_port, dst_dir = in_info
        # DPG should already enforce out → in, but verify defensively.
        if src_dir != "out" or dst_dir != "in":
            self._set_status("Cables must go output → input.")
            return
        try:
            cable = self.patch.connect(src_mod, src_port, dst_mod, dst_port)
        except (ValueError, KeyError) as exc:
            self._set_status(f"Cannot connect: {exc}")
            return
        link_id = dpg.add_node_link(out_attr, in_attr, parent=EDITOR_TAG)
        self._link_to_cable[link_id] = cable
        self._recompile_if_running()

    def _on_link_deleted(self, sender, app_data) -> None:
        link_id = app_data
        cable = self._link_to_cable.pop(link_id, None)
        if cable is not None:
            self.patch.disconnect(
                cable.src_module_id,
                cable.src_port,
                cable.dst_module_id,
                cable.dst_port,
            )
        # DPG removes the visual link itself; we only update our state.
        self._recompile_if_running()

    def _on_param_changed(self, sender, app_data, user_data) -> None:
        module_id, param_name = user_data
        try:
            self.backend.set_param(module_id, param_name, app_data)
        except Exception as exc:
            self._set_status(f"Param error: {exc}")

    def _show_wav_dialog(self, sender, app_data, user_data) -> None:
        """A FilePlayer Browse button was clicked: open the WAV picker."""
        self._wav_target_id = user_data  # module id
        if dpg.does_item_exist("wav_dialog"):
            dpg.show_item("wav_dialog")

    def _on_wav_selected(self, sender, app_data) -> None:
        """Apply the picked WAV path to the FilePlayer that requested it."""
        module_id = self._wav_target_id
        self._wav_target_id = None
        if module_id is None:
            return
        selections = app_data.get("selections", {})
        if selections:
            path = next(iter(selections.values()))
        else:
            path = app_data.get("file_path_name")
        if not path:
            return
        # Same mutation path as typing into the field; the renderer
        # re-decodes on the next block because the path param changed.
        try:
            self.backend.set_param(module_id, "path", path)
        except Exception as exc:
            self._set_status(f"Param error: {exc}")
            return
        text_tag = f"fileplayer_path_{module_id}"
        if dpg.does_item_exist(text_tag):
            dpg.set_value(text_tag, path)
        self._set_status(f"Selected: {os.path.basename(path)}")

    def _on_key_press(self, sender, app_data, user_data=None) -> None:
        """Route a physical key press to every key-accepting module in the patch.

        ``app_data`` is the dpg key code. If the key isn't mapped to a
        semitone we ignore it. We also debounce auto-repeat: holding A
        should be one note, not a stream of note-ons.
        """
        key_code = app_data
        if key_code in self._held_keys:
            return
        semitone = _KEY_TO_SEMITONE.get(key_code)
        if semitone is None:
            return
        self._held_keys.add(key_code)
        for module in self.patch.modules.values():
            if not getattr(module, "ACCEPTS_COMPUTER_KEYS", False):
                continue
            octave = int(module.params.get("octave", 4))
            midi_note = semitone_to_midi(octave, semitone)
            module.note_on(midi_note)

    def _on_key_release(self, sender, app_data, user_data=None) -> None:
        key_code = app_data
        self._held_keys.discard(key_code)
        semitone = _KEY_TO_SEMITONE.get(key_code)
        if semitone is None:
            return
        for module in self.patch.modules.values():
            if not getattr(module, "ACCEPTS_COMPUTER_KEYS", False):
                continue
            octave = int(module.params.get("octave", 4))
            midi_note = semitone_to_midi(octave, semitone)
            module.note_off(midi_note)

    def _all_keyboards_notes_off(self) -> None:
        """Release every note on every key-accepting module — avoids stuck notes."""
        self._held_keys.clear()
        for module in self.patch.modules.values():
            if getattr(module, "ACCEPTS_COMPUTER_KEYS", False):
                module.all_notes_off()

    def _on_delete_selected(self, sender, app_data, user_data=None) -> None:
        """Delete selected links and nodes from both the UI and the model.

        Order matters: links first, then nodes. Removing a node also drops
        any cables touching it; we walk the bookkeeping maps to keep the
        UI and the Patch in lockstep.
        """
        try:
            selected_links = list(dpg.get_selected_links(EDITOR_TAG) or [])
            selected_nodes = list(dpg.get_selected_nodes(EDITOR_TAG) or [])
        except Exception:
            return

        removed_any = False

        # 1) Selected links → update model, delete from UI.
        for link_id in selected_links:
            cable = self._link_to_cable.pop(link_id, None)
            if cable is not None:
                self.patch.disconnect(
                    cable.src_module_id,
                    cable.src_port,
                    cable.dst_module_id,
                    cable.dst_port,
                )
                removed_any = True
            try:
                dpg.delete_item(link_id)
            except Exception:
                pass

        # 2) Selected nodes → remove from patch, prune our maps, delete from UI.
        for node_id in selected_nodes:
            module_id = self._node_to_module.pop(node_id, None)
            if module_id is None:
                continue
            self._module_to_node.pop(module_id, None)
            # Drop any cables touching this module — both in our maps and in
            # the editor visual. ``patch.remove_module`` handles the model.
            for lid, cab in list(self._link_to_cable.items()):
                if cab.src_module_id == module_id or cab.dst_module_id == module_id:
                    del self._link_to_cable[lid]
                    try:
                        dpg.delete_item(lid)
                    except Exception:
                        pass
            for attr_id, info in list(self._attr_to_port.items()):
                if info[0] == module_id:
                    del self._attr_to_port[attr_id]
            for key in list(self._port_to_attr):
                if key[0] == module_id:
                    del self._port_to_attr[key]
            try:
                self.patch.remove_module(module_id)
            except KeyError:
                pass
            try:
                dpg.delete_item(node_id)
            except Exception:
                pass
            removed_any = True

        if removed_any:
            self._set_status(
                f"Deleted: {len(selected_links)} cable(s), {len(selected_nodes)} node(s)"
            )
            self._recompile_if_running()

    def _on_toggle_audio(self) -> None:
        if self.backend.is_running:
            self.backend.stop()
            self._all_keyboards_notes_off()
            dpg.set_item_label(AUDIO_BTN_TAG, "Start audio")
            self._set_status(f"Backend: {self.backend.name}  |  stopped")
        else:
            try:
                self.backend.compile(self.patch)
                self.backend.start()
            except Exception as exc:
                traceback.print_exc()
                self._set_status(f"Audio start failed: {exc}")
                return
            dpg.set_item_label(AUDIO_BTN_TAG, "Stop audio")
            self._set_status(f"Backend: {self.backend.name}  |  running")

    # ----- file menu ------------------------------------------------------

    def _on_new(self) -> None:
        self._clear_editor()
        self._reset_zoom_state()
        self.patch = Patch()
        self._recompile_if_running()
        self._set_status("New patch")

    def _on_open(self) -> None:
        dpg.show_item("open_dialog")

    def _on_open_selected(self, sender, app_data) -> None:
        selections = app_data.get("selections", {})
        if not selections:
            return
        path = next(iter(selections.values()))
        try:
            self._load_patch_from(path)
        except Exception as exc:
            traceback.print_exc()
            self._set_status(f"Open failed: {exc}")

    def _on_save(self) -> None:
        dpg.show_item("save_dialog")

    def _on_save_selected(self, sender, app_data) -> None:
        path = app_data.get("file_path_name") or app_data.get("current_path")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            self._capture_node_positions()
            save_patch(self.patch, path)
            self._set_status(f"Saved: {path}")
        except Exception as exc:
            traceback.print_exc()
            self._set_status(f"Save failed: {exc}")

    # ----- zoom -----------------------------------------------------------

    def _ctrl_down(self) -> bool:
        """True if either Control key is currently held down."""
        for _name in ("mvKey_LControl", "mvKey_RControl", "mvKey_Control"):
            code = getattr(dpg, _name, None)
            if code is None:
                continue
            try:
                if dpg.is_key_down(code):
                    return True
            except Exception:
                pass
        return False

    def _apply_zoom(self, new_zoom: float) -> None:
        """Set the UI scale factor and rescale the canvas to match.

        Scales the global font (nodes auto-size to their text, so they grow
        or shrink with it) and multiplies every node's position by the same
        ratio about the editor origin, so the relative layout — and the
        cable lengths — track the size rather than overlapping or scattering.
        Keeps the toolbar slider in sync; ``set_value`` does not re-fire the
        slider callback, so there is no feedback loop.
        """
        new_zoom = clamp_zoom(float(new_zoom))
        old = self._zoom or ZOOM_DEFAULT
        ratio = new_zoom / old
        if abs(ratio - 1.0) > 1e-9:
            for node_id in list(self._node_to_module):
                try:
                    pos = dpg.get_item_pos(node_id)
                except Exception:
                    continue
                if pos is None:
                    continue
                dpg.set_item_pos(node_id, list(scale_pos(pos, ratio)))
        self._zoom = new_zoom
        try:
            dpg.set_global_font_scale(new_zoom)
        except Exception:
            pass
        if dpg.does_item_exist(ZOOM_SLIDER_TAG):
            dpg.set_value(ZOOM_SLIDER_TAG, factor_to_percent(new_zoom))

    def _reset_zoom_state(self) -> None:
        """Snap back to 100 % without moving any nodes.

        Used on New / Open before nodes are (re)built: saved positions are
        in logical (100 %) coords, so the canvas must be at 1.0 while they
        are created. ``_apply_zoom`` then re-applies any saved zoom.
        """
        self._zoom = ZOOM_DEFAULT
        try:
            dpg.set_global_font_scale(ZOOM_DEFAULT)
        except Exception:
            pass
        if dpg.does_item_exist(ZOOM_SLIDER_TAG):
            dpg.set_value(ZOOM_SLIDER_TAG, factor_to_percent(ZOOM_DEFAULT))

    def _on_zoom_slider(self, sender, app_data) -> None:
        self._apply_zoom(percent_to_factor(app_data))

    def _on_zoom_reset(self, *args) -> None:
        self._apply_zoom(ZOOM_DEFAULT)

    def _on_zoom_in_key(self, sender, app_data) -> None:
        if self._ctrl_down():
            self._apply_zoom(step_zoom(self._zoom, +1))

    def _on_zoom_out_key(self, sender, app_data) -> None:
        if self._ctrl_down():
            self._apply_zoom(step_zoom(self._zoom, -1))

    def _on_zoom_reset_key(self, sender, app_data) -> None:
        if self._ctrl_down():
            self._apply_zoom(ZOOM_DEFAULT)

    def _on_zoom_wheel(self, sender, app_data) -> None:
        if not self._ctrl_down():
            return
        self._apply_zoom(step_zoom(self._zoom, 1 if app_data > 0 else -1))

    def _capture_node_positions(self) -> None:
        """Snapshot the current DPG node positions into ``patch.ui``.

        Called just before save so the on-disk layout reflects whatever
        the user dragged around. Stored as ``{"node_positions": {str(mid): [x, y]}}``
        — module-id keys are JSON strings to keep the round-trip clean.
        """
        # Positions are stored at 100 % zoom (logical coords) so a patch
        # saved while zoomed reloads with the same layout regardless of
        # the zoom in effect at save time. Divide the factor out here.
        z = self._zoom or ZOOM_DEFAULT
        positions: dict[str, list[float]] = {}
        for module_id, node_id in self._module_to_node.items():
            try:
                pos = dpg.get_item_pos(node_id)
            except Exception:
                continue
            if pos is None:
                continue
            positions[str(module_id)] = [float(pos[0]) / z, float(pos[1]) / z]
        if positions:
            self.patch.ui["node_positions"] = positions
        # Remember the zoom so reopening restores the same scale.
        self.patch.ui["zoom"] = float(self._zoom)

    def _load_patch_from(self, path: str) -> None:
        was_running = self.backend.is_running
        if was_running:
            self.backend.stop()
            dpg.set_item_label(AUDIO_BTN_TAG, "Start audio")

        self._clear_editor()
        # Build the new patch's nodes at 100 % so saved (logical)
        # positions land correctly; the saved zoom is re-applied below.
        self._reset_zoom_state()
        self.patch = load_patch(path)

        saved_positions = self.patch.ui.get("node_positions", {})
        for module in self.patch:
            pos = saved_positions.get(str(module.id))
            if pos is not None and isinstance(pos, (list, tuple)) and len(pos) == 2:
                self._create_node_for_module(module, pos=(float(pos[0]), float(pos[1])))
            else:
                self._create_node_for_module(module)

        for cable in self.patch.cables:
            src_attr = self._port_to_attr.get((cable.src_module_id, cable.src_port, "out"))
            dst_attr = self._port_to_attr.get((cable.dst_module_id, cable.dst_port, "in"))
            if src_attr is None or dst_attr is None:
                continue
            link_id = dpg.add_node_link(src_attr, dst_attr, parent=EDITOR_TAG)
            self._link_to_cable[link_id] = cable

        # Restore the saved zoom (if any) now that every node exists.
        saved_zoom = self.patch.ui.get("zoom")
        if saved_zoom is not None:
            try:
                self._apply_zoom(float(saved_zoom))
            except (TypeError, ValueError):
                pass

        self._set_status(f"Loaded: {os.path.basename(path)}")

    # ----- helpers --------------------------------------------------------

    def _clear_editor(self) -> None:
        # Release held notes — the modules they belong to are about to vanish.
        self._all_keyboards_notes_off()
        # DearPyGui's node editor keeps links in slot 0 and nodes in slot 1.
        # Deleting only nodes leaves orphan links pointing at attribute IDs
        # that no longer exist — and DPG hard-exits the next time it tries
        # to render them, which is what caused the silent crash when opening
        # a second patch on top of an existing one. ``children_only=True``
        # blasts every slot at once, which is what we want here.
        try:
            dpg.delete_item(EDITOR_TAG, children_only=True)
        except Exception:
            # Defensive: if DPG raises (rare), fall back to the per-slot loop
            # so a single bad child doesn't take the whole load down.
            for slot in (0, 1):
                children = dpg.get_item_children(EDITOR_TAG, slot=slot) or []
                for child in children:
                    try:
                        dpg.delete_item(child)
                    except Exception:
                        pass
        self._node_to_module.clear()
        self._module_to_node.clear()
        self._attr_to_port.clear()
        self._port_to_attr.clear()
        self._link_to_cable.clear()
        # The bars themselves were children of the editor and are already
        # gone; drop our references and the stale auto-range bounds so a
        # freshly-loaded patch starts metering from scratch.
        self._cv_meter_bars.clear()
        self._meter_bounds.clear()
        self._audio_meter_bars.clear()
        self._file_pos_labels.clear()
        # Reset the cascade so a fresh-loaded patch (with no saved positions)
        # starts laying out from the top-left again.
        self._next_node_pos = [40, 40]

    # ----- CV meters ------------------------------------------------------

    # Per-frame release coefficient for the auto-range bounds. Each frame
    # the [lo, hi] window relaxes this fraction of the way toward the
    # current value (shrinking the range when the signal stops hitting
    # its old extremes), then re-widens instantly to include the current
    # value. ~0.02 at vsync rates settles over roughly a second -- fast
    # enough to track a patch change, slow enough not to twitch.
    _METER_RELEASE = 0.02

    def _update_cv_meters(self) -> None:
        """Push the backend's latest per-cv-port levels into the bars.

        Called once per rendered frame. Cheap and defensive: backends
        without the meter hook (e.g. the pyo stub) simply no-op, and a
        port that isn't currently producing a level leaves its bar
        untouched (frozen at its last reading) rather than snapping to
        zero.
        """
        if not self._cv_meter_bars:
            return
        snapshot = getattr(self.backend, "snapshot_meter_levels", None)
        if snapshot is None:
            return
        levels = snapshot()
        for key, bar in self._cv_meter_bars.items():
            value = levels.get(key)
            if value is None:
                continue
            value = float(value)
            fill = self._auto_range_fill(key, value)
            dpg.set_value(bar, fill)
            dpg.configure_item(bar, overlay=f"{value:+.2f}")

    def _update_file_positions(self) -> None:
        """Push each FilePlayer's elapsed / total playhead time into its
        node readout. Once per frame; backends without the hook (the pyo
        stub) no-op, and a node with no current entry keeps its last text.
        """
        if not self._file_pos_labels:
            return
        snapshot = getattr(self.backend, "snapshot_file_positions", None)
        if snapshot is None:
            return
        positions = snapshot()
        for mid, label in self._file_pos_labels.items():
            pair = positions.get(mid)
            if pair is None:
                continue
            elapsed, total = pair
            dpg.set_value(
                label,
                f"{self._format_time(elapsed)} / {self._format_time(total)}",
            )

    @staticmethod
    def _format_time(seconds: float) -> str:
        """Seconds -> ``m:ss`` (minutes uncapped, e.g. ``12:05``)."""
        seconds = max(0.0, float(seconds))
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"

    # Meter floor in dB; the bar spans [_METER_FLOOR_DB, 0] dBFS.
    _METER_FLOOR_DB = -90.0

    def _update_audio_meters(self) -> None:
        """Push Meter-module peak levels into their dBFS bars.

        Reads a snapshot of the backend's per-meter linear peak
        envelopes and maps each to a fixed -90..0 dBFS bar (fixed, not
        auto-ranged, so two meters are directly comparable). Cheap: a
        handful of meters, one log10 each.
        """
        if not self._audio_meter_bars:
            return
        snap = getattr(self.backend, "snapshot_audio_levels", None)
        if snap is None:
            return
        levels = snap()
        floor = self._METER_FLOOR_DB
        for module_id, bar in self._audio_meter_bars.items():
            env = levels.get(module_id, 0.0)
            if env > 1e-9:
                db = 20.0 * math.log10(env)
            else:
                db = floor
            db_clamped = max(floor, min(0.0, db))
            fill = (db_clamped - floor) / (0.0 - floor)
            overlay = "-inf dB" if db_clamped <= floor else f"{db_clamped:.1f} dB"
            try:
                dpg.set_value(bar, fill)
                dpg.configure_item(bar, overlay=overlay)
            except Exception:
                pass

    def _auto_range_fill(self, key: tuple[int, str], value: float) -> float:
        """Normalise ``value`` into 0..1 against the port's auto-range
        window, updating that window in place (instant attack, slow
        release). A near-constant source (range ~ 0) parks the bar at
        mid-scale rather than dividing by zero.
        """
        bounds = self._meter_bounds.get(key)
        if bounds is None:
            # First sight: seed the window on this value, show mid-scale.
            self._meter_bounds[key] = [value, value]
            return 0.5
        lo, hi = bounds
        k = self._METER_RELEASE
        lo += (value - lo) * k
        hi += (value - hi) * k
        if value < lo:
            lo = value
        if value > hi:
            hi = value
        bounds[0], bounds[1] = lo, hi
        span = hi - lo
        if span < 1e-6:
            return 0.5
        return min(1.0, max(0.0, (value - lo) / span))

    def _recompile_if_running(self) -> None:
        if self.backend.is_running:
            try:
                self.backend.compile(self.patch)
            except Exception as exc:
                traceback.print_exc()
                self._set_status(f"Recompile failed: {exc}")

    def _set_status(self, text: str) -> None:
        try:
            dpg.set_value(STATUS_TEXT_TAG, f"   {text}")
        except Exception:
            print(text, file=sys.stderr)


def main() -> None:
    """GUI entry point with crash protection.

    Wraps ``App().run()`` in a try/except so any uncaught exception (DPG
    hard-exit territory, viewport setup failures, callback explosions,
    anything that escapes the per-callback try/except'es scattered
    through App) gets written out as a heavy crash report to
    ~/.pysynthrack/crashes/ before the process dies. The user gets a
    pointer to the file on stderr so they can paste it into a chat for
    diagnosis.

    The crash reporter itself is wrapped in another try/except so a
    failure inside describe_error or write_crash_report falls back to a
    plain traceback rather than swallowing the original exception. The
    final ``raise`` preserves the normal "non-zero exit, traceback in
    terminal" behaviour - the crash file is additive, not a
    replacement.
    """
    try:
        App().run()
    except BaseException as e:
        try:
            from ..error_handler import describe_error
            from .._crash import write_crash_report
            report = describe_error(e, include_locals=True)
            path = write_crash_report(report, source="gui")
            print(
                f"[pysynthrack] Fatal GUI error: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            if path:
                print(
                    f"[pysynthrack] Crash report written to:\n  {path}\n"
                    "  Share this file when reporting the bug.",
                    file=sys.stderr,
                )
        except BaseException:
            # Crash reporter itself failed - fall back to a normal
            # traceback so the user at least sees the original error.
            traceback.print_exc()
        raise


if __name__ == "__main__":  # pragma: no cover - GUI entry
    main()
