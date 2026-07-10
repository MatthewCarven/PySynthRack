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
from ..core.module import grouped_module_types
from ..core.patch import Cable, Patch
from ..io_patch import load_patch, save_patch
from ..modules.filter import FILTER_MODES
from ..modules.keyboard import midi_to_name, semitone_to_midi
from ..modules.cvcombiner import CVCOMBINER_MODES
from ..modules.cvtofrequency import MODES as CVTOFREQ_MODES
from ..modules.lfo import LFO_WAVEFORMS
from ..modules.midiinput import (
    AUTO_DEVICE,
    available_devices as midi_available_devices,
    compute_velocity_curve,
)
from ..modules.micinput import available_input_devices as mic_available_devices
from ..modules.output import available_output_devices as spk_available_devices
from ..modules.fader_seq import FADER_RANGE_ST
from ..modules.sequencer import MAX_STEPS as SEQ_MAX_STEPS
from ..modules.compressor import DETECTOR_MODES
from ..modules.distortion import DISTORTION_MODES
from ..modules.meter import METER_MODES
from ..modules.waveshaper import WAVESHAPER_MODES
from ..modules.noise import NOISE_COLORS
from ..modules.oscillator import WAVEFORMS
from ..modules.sweep_eq import SWEEP_EQ_MODES
from .dsp_load import IDLE_COLOR, format_dsp_load, load_color
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
from .buffer import (
    BUFFER_SIZES,
    coerce_buffer_size,
    index_to_size,
    size_to_index,
)
from .param_scroll import cycle_index, decimals_from_format, nudge_number
from ..settings import load_settings, save_settings
from .window_geometry import make_geometry, resolve as resolve_window


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
BUFFER_SLIDER_TAG = "buffer_slider"
DSP_TEXT_TAG = "dsp_load_text"

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

        # dpg-id → (module_id, param_name) for every scrollable param widget,
        # so a mouse wheel over one can nudge its value. Filled as nodes are
        # built; stale ids (deleted nodes) are pruned lazily on scroll.
        self._param_widgets: dict[int, tuple[int, str]] = {}

        # The file_player node whose Browse / Add-to-list button was last
        # clicked, so the shared WAV file dialog's callback knows which
        # module to update, and whether to set ``path`` ("path") or append
        # to the queue ("playlist").
        self._wav_target_id: Optional[int] = None
        self._wav_target_mode: str = "path"

        # The midi_input whose Calibrate-keys dialog is open, plus the
        # last stopped-but-not-yet-computed learn capture (Learn -> Stop
        # stashes here so Compute can still use it).
        self._vel_target_id: Optional[int] = None
        self._vel_captured: dict[int, list[float]] = {}

        # Diagonal stagger for newly-added nodes so they don't stack.
        self._next_node_pos = [40, 40]

        # Current UI scale ("zoom") factor; 1.0 == 100 %. imnodes has no
        # real canvas zoom, so we fake it: scale the global font (the
        # auto-sized nodes grow/shrink with it) and rescale every node's
        # position by the same factor so spacing — and cable length —
        # tracks the size. See ui/zoom.py for the dpg-free maths.
        self._zoom: float = ZOOM_DEFAULT

        # Machine-scoped settings, persisted to a JSON file in the platform
        # config dir (see pysynthrack.settings). Loaded once here; individual
        # keys are written back as they change.
        self._settings: dict = load_settings()

        # Global audio buffer size (frames per block) applied to the backend
        # when audio is (re)started. The toolbar slider carries the *index*
        # into ui/buffer.BUFFER_SIZES, not the raw count; the size is read at
        # Start (see _on_toggle_audio) and the slider greys while running.
        # Persisted globally (NOT per patch — it's a hardware/latency setting):
        # loaded from settings here, saved when the slider changes.
        self._buffer_size: int = coerce_buffer_size(
            self._settings.get("buffer_size")
        )

        # Track which physical keys are currently down so OS auto-repeat
        # doesn't fire note_on repeatedly while a key is held.
        self._held_keys: set[int] = set()

        # CV meters. For each cv-kind output port we draw a progress bar
        # under its node attribute; ``_cv_meter_bars`` maps the port key
        # (module_id, port_name) to that bar's dpg tag. ``_meter_bounds``
        # holds the per-port auto-range state [lo, hi] used to normalise
        # the fill (instant-attack / slow-release; see _auto_range_fill).
        self._cv_meter_bars: dict[tuple[int, str], int] = {}
        # Meter-module level displays: module_id -> a bundle dict with
        # the L and R channel drawlists' item tags ("l"/"r", each a dict
        # of dl/fill/tick/lamp/text) plus "r_shown" (whether the R bar
        # is currently visible; it exists from creation but stays hidden
        # until the snapshot reports a patched ``in_r``).
        self._audio_meter_bars: dict[int, dict] = {}
        self._meter_bounds: dict[tuple[int, str], list[float]] = {}
        # FilePlayer playhead readouts. Maps module_id -> the dpg text
        # tag showing 'elapsed / total'; refreshed each frame in
        # _update_file_positions from the backend's snapshot hook.
        self._file_pos_labels: dict[int, int] = {}
        # FilePlayer queue ("file list"). ``_playlist_listboxes`` maps
        # module_id -> the dpg listbox tag showing the upcoming tracks;
        # ``_fileplayer_prev_finished`` remembers each player's last
        # 'finished' state so _advance_file_playlists can edge-trigger the
        # auto-advance (one pop per track end, not one per frame).
        self._playlist_listboxes: dict[int, int] = {}
        self._fileplayer_prev_finished: dict[int, bool] = {}

    # ----- entry point ----------------------------------------------------

    def run(self) -> None:
        dpg.create_context()
        # Debug-only self-destruct: set PYSYNTHRACK_CRASH_TEST to a frame count
        # to make the render loop raise after that many rendered frames, so the
        # last-resort crash handler in main() can be exercised from a live
        # window (folder report + friendly pointer + exit 1, no traceback).
        # Inert unless the env var is set; a non-integer value means "crash on
        # the first frame".
        _crash_test_after = os.environ.get("PYSYNTHRACK_CRASH_TEST")
        if _crash_test_after:
            try:
                _crash_test_after = int(_crash_test_after)
            except ValueError:
                _crash_test_after = 1
        else:
            _crash_test_after = None
        _frame_count = 0
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
                self._update_dsp_load()
                self._update_file_positions()
                self._advance_file_playlists()
                self._update_velocity_capture()
                dpg.render_dearpygui_frame()
                if _crash_test_after is not None:
                    _frame_count += 1
                    if _frame_count >= _crash_test_after:
                        raise RuntimeError(
                            "PYSYNTHRACK_CRASH_TEST: deliberate crash after "
                            f"{_frame_count} frame(s) to exercise the crash "
                            "handler"
                        )
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
                    # One submenu per category (see CATEGORY_ORDER in
                    # core.module); modules declare their group with a
                    # CATEGORY class attribute.
                    for category, type_names in grouped_module_types():
                        with dpg.menu(label=category):
                            for type_name in type_names:
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
                dpg.add_spacer(width=24)
                # Global audio buffer size. The slider spans the *indices*
                # into BUFFER_SIZES (non-uniform stops), and its printf
                # ``format`` is rewritten in the callback to show the real
                # frame count on the handle. Applied to the backend at Start.
                dpg.add_text("Buffer")
                dpg.add_slider_int(
                    tag=BUFFER_SLIDER_TAG,
                    width=120,
                    min_value=0,
                    max_value=len(BUFFER_SIZES) - 1,
                    default_value=size_to_index(self._buffer_size),
                    clamped=True,
                    format=str(self._buffer_size),
                    callback=self._on_buffer_slider,
                )
                dpg.add_spacer(width=24)
                # DSP-load readout: render time over the block budget,
                # smoothed by the backend (see ui/dsp_load.py). Grey
                # dashes while audio is stopped.
                dpg.add_text("DSP --", tag=DSP_TEXT_TAG, color=IDLE_COLOR)

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
            # Bare wheel over a param widget nudges its value. Zoom (above)
            # only fires with Ctrl held, so the two never both act on a notch.
            dpg.add_mouse_wheel_handler(callback=self._on_param_wheel)

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

            # Parameters (static, no circle). fader_seq draws its own
            # compact fader-bank panel instead of 33 labelled param rows.
            if module.TYPE == "fader_seq":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    self._build_fader_seq_panel(module)
            else:
                for param_name, default in module.DEFAULT_PARAMS.items():
                    # file_player's queue is not a scalar widget — it gets a
                    # dedicated listbox + Add/Clear panel in the block below.
                    if module.TYPE == "file_player" and param_name == "playlist":
                        continue
                    with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                        self._add_param_widget(module, param_name, default)
                        wid = dpg.last_item()
                        if wid:
                            self._param_widgets[wid] = (module.id, param_name)

            # FilePlayer: tape-style transport buttons plus a live
            # 'elapsed / total' playhead readout (_update_file_positions
            # ticks it each frame; while a long file is still decoding the
            # total shows the buffered length growing). Play resumes,
            # Stop pauses in place (the ``playing`` param), |< rewinds to
            # 0:00 whether playing or paused.
            if module.TYPE == "file_player":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    with dpg.group(horizontal=True):
                        dpg.add_button(
                            label="|<",
                            width=28,
                            callback=self._on_file_transport,
                            user_data=(module.id, "rewind"),
                        )
                        dpg.add_button(
                            label="Play",
                            width=44,
                            callback=self._on_file_transport,
                            user_data=(module.id, "play"),
                        )
                        dpg.add_button(
                            label="Stop",
                            width=44,
                            callback=self._on_file_transport,
                            user_data=(module.id, "stop"),
                        )
                        label = dpg.add_text("0:00 / 0:00")
                        self._file_pos_labels[module.id] = label
                # File list / queue: tracks here auto-play (then drop off the
                # list) as each one-shot finishes. 'Add to list...' reuses the
                # same WAV picker as Browse; Clear empties the queue.
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    dpg.add_text(
                        "Up next (plays then leaves the list):",
                        color=(170, 170, 170),
                    )
                    lb = dpg.add_listbox(
                        items=self._playlist_display_items(module),
                        num_items=4,
                        width=200,
                    )
                    self._playlist_listboxes[module.id] = lb
                    with dpg.group(horizontal=True):
                        dpg.add_button(
                            label="Add to list...",
                            callback=self._show_wav_dialog,
                            user_data=(module.id, "playlist"),
                        )
                        dpg.add_button(
                            label="Clear",
                            callback=self._on_clear_playlist,
                            user_data=module.id,
                        )

            # Meter: one dBFS level display (-90..0) per channel, driven
            # each frame from the backend's indicator triples. Each is a
            # drawlist (bar fill + peak-hold tick + clip lamp + dB text)
            # rather than a progress bar, so the tick and lamp can be
            # drawn on top of the fill. The R display exists up front
            # but stays hidden until ``in_r`` is patched.
            if module.TYPE == "meter":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    self._audio_meter_bars[module.id] = {
                        "l": self._build_meter_display(module.id, show=True),
                        "r": self._build_meter_display(module.id, show=False),
                        "r_shown": False,
                    }

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

        if module.TYPE in ("file_player", "convolver") and param_name == "path":
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

        if module.TYPE == "file_player" and param_name == "playing":
            # Same param the Play/Stop transport buttons drive; an explicit
            # tag lets their callback keep this checkbox in sync (the
            # mirror of the Browse button writing back into the path field).
            dpg.add_checkbox(
                label=param_name,
                default_value=bool(current),
                tag=f"fileplayer_playing_{module.id}",
                callback=self._on_param_changed,
                user_data=user_data,
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

        if module.TYPE in ("parametric_eq", "motion_eq"):
            # (Parametric/Motion) EQ bands: ``band{i}_freq`` (Hz),
            # ``band{i}_gain`` (dB, 0 = flat), ``band{i}_q`` (width);
            # MotionEQ adds a shared ``cv_depth`` (oct/unit) scaling its
            # per-band ``freq_cv`` sweeps, a shared ``gain_cv_depth``
            # (dB/unit) scaling its per-band ``gain_cv`` pushes, and a
            # shared ``q_cv_depth`` (Q doublings/unit) scaling its
            # per-band ``q_cv`` squeezes.
            # Distinct ranges from the generic numeric fallbacks below.
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
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gain_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.0, max_value=18.0, format="%.1f dB/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "q_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f dbl/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
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
            if param_name == "formant_preserve":
                dpg.add_checkbox(
                    label=param_name, default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
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
            if param_name == "window":
                # Looping-buffer window: latency (half of it) vs loop
                # texture. 200 ms is the old fixed behaviour.
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=2.0,
                    min_value=20.0,
                    max_value=2000.0,
                    format="%.0f ms",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "mix":
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
            if param_name == "antialias":
                # Off by default -> raw, aliased lo-fi up-shift. On ->
                # band-limits the input so pitching up doesn't fold
                # content past Nyquist (cleaner, less tape character).
                dpg.add_checkbox(
                    label=param_name,
                    default_value=bool(current),
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "spread":
                # Stereo detune width in cents (0 = mono). Patch
                # out_l/out_r to L/R speakers for one-module width.
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.2,
                    min_value=0.0,
                    max_value=50.0,
                    format="%.1f ct",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return

        if module.TYPE == "sweep_eq":
            # A single CV-swept resonant band (auto-wah / envelope filter).
            # ``mode`` (bandpass/lowpass/peak) is handled by the shared mode
            # combo below; ``gain`` only bites in peak mode. ``cv_depth``
            # scales freq_cv (octaves per unit, 1 V/oct).
            if param_name == "freq":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=20000.0, format="%.1f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gain":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-24.0, max_value=24.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "q":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.1, max_value=20.0, format="%.2f",
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
            if param_name == "mix":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "tilt_eq":
            # Spectral tilt (bass<->treble seesaw). ``pivot`` is the Hz the
            # balance seesaws about; ``tilt`` the static base tilt in dB
            # (positive = lows up / highs down); ``cv_depth`` the dB of
            # tilt per tilt_cv unit.
            if param_name == "pivot":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=20000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "tilt":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-12.0, max_value=12.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.0, max_value=18.0, format="%.1f dB/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "crossover":
            # LR4 two-way split. ``freq`` is the corner (Hz); ``cv_depth``
            # scales freq_cv (octaves per unit, 1 V/oct) to sweep the
            # split point.
            if param_name == "freq":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=20000.0, format="%.1f Hz",
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

        if module.TYPE == "ring_mod":
            # Ring modulator: ``freq`` sets the internal sine carrier's
            # pitch (Hz) when ``carrier`` is unpatched; ``freq_cv_depth``
            # scales freq_cv (octaves per unit, 1 V/oct); ``mix`` is
            # dry/wet (0 = bit-exact dry).
            if param_name == "freq":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=1.0, max_value=5000.0, format="%.1f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freq_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
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

        if module.TYPE == "meter":
            # Level meter: ``release`` sets the bar fall time in seconds
            # (small = snappier / more reactive) and how fast the
            # peak-hold tick falls once its ~1.5 s hold expires.
            # ``stereo_link`` merges the pair's tick/lamp/readout.
            if param_name == "release":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.02, max_value=2.0, format="%.2f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "stereo_link":
                dpg.add_checkbox(
                    label=param_name, default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
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
            if param_name == "through_zero":
                dpg.add_checkbox(
                    label=param_name, default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "polarity":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
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

        if module.TYPE == "vocoder":
            # Channel vocoder. ``bands`` is the analysis/synthesis band
            # count (8 lo-fi robot .. 24 clear speech); ``freq_lo``/
            # ``freq_hi`` bound the log-spaced band centres; ``width``
            # scales every band's bandwidth (narrow = robotic, wide =
            # smeared); ``attack``/``release`` set the follower speed;
            # ``hiss`` is the sibilance/noise path level (consonants);
            # ``gain`` is wet-path makeup; ``mix`` is dry carrier <->
            # vocoded (normally played fully wet).
            if param_name == "bands":
                dpg.add_combo(
                    label=param_name, items=["8", "12", "16", "24"],
                    default_value=str(int(round(float(current)))),
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freq_lo":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=50.0, max_value=500.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freq_hi":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=10.0,
                    min_value=2000.0, max_value=12000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "width":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.3, max_value=3.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "attack":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.1, max_value=100.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "release":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=1.0, max_value=500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "hiss":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gain":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=4.0, format="%.2f",
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

        if module.TYPE in (
            "stereo_speaker_output", "specific_stereo_speaker_output"
        ):
            # The stereo sink. ``pan`` places a mono source
            # (constant-power) or balances a stereo pair; ``width`` is
            # mid/side (0 mono .. 2 over-wide, pairs only); ``gain`` is
            # the output trim; ``cv_depth`` scales BOTH pan_cv and
            # width_cv, in knob units per CV unit (Reverb convention).
            if param_name == "pan":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "width":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=2.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gain":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=2.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=2.0, format="%.2f per unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "limiter":
            # Brickwall lookahead limiter. ``ceiling`` is the hard output
            # ceiling in dBFS (the output peak never exceeds it); ``release``
            # is the one-pole recovery time; ``lookahead`` is the attack
            # window, which also sets the module's fixed processing latency.
            if param_name == "ceiling":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-20.0, max_value=0.0, format="%.1f dBFS",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "release":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=1000.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "lookahead":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=1.0, max_value=10.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "noise_gate":
            # Hold-and-hysteresis downward gate. ``threshold`` is the open
            # level (dBFS; at -80 the gate is a bit-exact bypass);
            # ``hysteresis`` is the dB the level must fall below it to close
            # (anti-chatter Schmitt gap); ``attack`` / ``release`` are the
            # open / close ramps; ``hold`` keeps it open through brief dips;
            # ``range`` is how far a closed gate ducks (-80 = full mute,
            # higher = expander-style).
            if param_name == "threshold":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-80.0, max_value=0.0, format="%.1f dBFS",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "hysteresis":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=24.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "attack":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.1, max_value=50.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "hold":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=0.0, max_value=500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "release":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=2.0,
                    min_value=5.0, max_value=2000.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "range":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-80.0, max_value=0.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "distortion":
            # Drive pedal. ``drive`` pushes the signal into the curve;
            # ``tone`` is the post-distortion low-pass in Hz (20 kHz =
            # out of circuit); ``level`` trims the (loud) output;
            # ``mix`` blends dry back in; ``cv_depth`` scales drive_cv
            # in drive units per CV unit. ``mode`` hits the shared
            # mode-combo branch (soft / hard / tube).
            if param_name == "drive":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.1, max_value=30.0, format="%.1f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "tone":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=200.0, max_value=20000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "level":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=2.0, format="%.2f",
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
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.0, max_value=30.0, format="%.1f drive/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "waveshaper":
            # Wavefolder. ``fold`` is the push into the folder (1 = a
            # full-scale signal just reaches the rails); ``symmetry``
            # slides the signal off-centre pre-fold (even harmonics);
            # ``mix`` is dry/wet; ``cv_depth`` scales fold_cv in fold
            # units per CV unit. ``mode`` hits the shared mode-combo
            # branch (triangle / sine).
            if param_name == "fold":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=16.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "symmetry":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
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
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.0, max_value=16.0, format="%.1f fold/unit",
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
            if param_name == "cv_depth":
                # Shared by decay_cv + damping_cv + mix_cv; all three
                # targets are 0..1 macros, so the depth is level units
                # per CV unit.
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=2.0, format="%.2f lvl/unit",
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
                    min_value=0.0, max_value=2.0, format="%.2f lvl/unit",
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
            elif module.TYPE == "sweep_eq":
                items = list(SWEEP_EQ_MODES)
            elif module.TYPE == "meter":
                items = list(METER_MODES)
            elif module.TYPE == "distortion":
                items = list(DISTORTION_MODES)
            elif module.TYPE == "waveshaper":
                items = list(WAVESHAPER_MODES)
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

        if param_name == "detector":
            # Compressor level detector: peak (instantaneous) or rms
            # (~10 ms energy window).
            dpg.add_combo(
                label=param_name,
                items=list(DETECTOR_MODES),
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
        if param_name == "device" and module.TYPE in (
            "midi_input", "mic_input", "specific_stereo_speaker_output"
        ):
            # Device list snapshot at widget creation, plus a Refresh
            # button that re-enumerates in place after hot-plugging (no
            # more delete+re-add / reopen-the-patch dance). MIDIInput
            # lists MIDI ports; MicInput lists audio capture devices;
            # SpecificStereoSpeakerOutput lists audio playback devices.
            current_str = str(current)
            items, _ = self._device_combo_items(module.TYPE, current_str)
            with dpg.group(horizontal=True):
                dpg.add_combo(
                    label=param_name,
                    items=items,
                    default_value=current_str,
                    width=200,
                    tag=f"device_combo_{module.id}",
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                dpg.add_button(
                    label="Refresh",
                    callback=self._on_refresh_devices,
                    user_data=(module.id, module.TYPE),
                )
            return

        if module.TYPE == "midi_input" and param_name == "velocity_curve":
            # Dict-valued param — no generic widget fits. The whole
            # editing story lives in the Calibrate-keys dialog (learn
            # mode + per-key multiplier table); the label alongside just
            # says how many keys currently deviate from 1.0.
            n = len(module.params.get("velocity_curve") or {})
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Calibrate keys...",
                    callback=self._show_velocity_dialog,
                    user_data=module.id,
                )
                dpg.add_text(
                    f"{n} keys calibrated" if n else "",
                    tag=f"vel_curve_count_{module.id}",
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
            if param_name in {"freq", "cutoff"}:
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
                in {"amp", "gain", "sustain", "depth", "master", "high", "low"}
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
            elif param_name == "cv_depth":
                # House default for cv_depth knobs without a dedicated
                # module branch above (filter cutoff_cv, lfo rate_cv):
                # frequency-domain depth in octaves per CV unit,
                # default 1.0 = 1 V/oct. Modules whose depth is in
                # another natural unit (ms, st, dB, level) add their
                # own branch earlier with the unit in the label.
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.02,
                    min_value=0.0,
                    max_value=4.0,
                    format="%.2f oct/unit",
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

    def _on_file_transport(self, sender, app_data, user_data) -> None:
        """A FilePlayer transport button: (module_id, 'play'|'stop'|'rewind').

        Play/Stop drive the ``playing`` param (pause keeps the playhead;
        the renderer holds position while False) and mirror the value into
        the node's checkbox. Rewind asks the backend to seek to 0:00 at
        the next block boundary — a state poke, not a param, so it works
        identically while playing or paused (backends without the hook,
        i.e. the pyo stub, no-op).
        """
        module_id, action = user_data
        if action == "rewind":
            rewind = getattr(self.backend, "rewind_file_player", None)
            if rewind is not None:
                rewind(module_id)
            return
        playing = action == "play"
        try:
            self.backend.set_param(module_id, "playing", playing)
        except Exception as exc:
            self._set_status(f"Param error: {exc}")
            return
        tag = f"fileplayer_playing_{module_id}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, playing)

    def _show_wav_dialog(self, sender, app_data, user_data) -> None:
        """A FilePlayer Browse / Add-to-list button was clicked: open the
        shared WAV picker.

        ``user_data`` is the module id (Browse — set the ``path`` param) or
        a ``(module_id, "playlist")`` tuple (Add to list — append to the
        queue). The mode is stashed for _on_wav_selected to read back.
        """
        if isinstance(user_data, tuple):
            self._wav_target_id, self._wav_target_mode = user_data
        else:
            self._wav_target_id, self._wav_target_mode = user_data, "path"
        if dpg.does_item_exist("wav_dialog"):
            dpg.show_item("wav_dialog")

    def _on_wav_selected(self, sender, app_data) -> None:
        """Apply the picked WAV(s) to the FilePlayer that requested them —
        either as the current ``path`` or appended to its queue."""
        module_id = self._wav_target_id
        mode = self._wav_target_mode
        self._wav_target_id = None
        self._wav_target_mode = "path"
        if module_id is None:
            return
        selections = app_data.get("selections", {})
        if selections:
            paths = [p for p in selections.values() if p]
        else:
            one = app_data.get("file_path_name")
            paths = [one] if one else []
        if not paths:
            return

        if mode == "playlist":
            module = self.patch.modules.get(module_id)
            if module is None:
                return
            queue = module.params.get("playlist")
            if not isinstance(queue, list):
                queue = []
                module.params["playlist"] = queue
            queue.extend(paths)
            self._refresh_playlist_display(module_id)
            self._set_status(
                f"Queued {len(paths)} file(s) — {len(queue)} in the list"
            )
            return

        # "path" mode: same mutation path as typing into the field; the
        # renderer re-decodes on the next block because the path changed.
        path = paths[0]
        try:
            self.backend.set_param(module_id, "path", path)
        except Exception as exc:
            self._set_status(f"Param error: {exc}")
            return
        text_tag = f"fileplayer_path_{module_id}"
        if dpg.does_item_exist(text_tag):
            dpg.set_value(text_tag, path)
        self._set_status(f"Selected: {os.path.basename(path)}")

    # ----- file_player queue ("file list") ---------------------------------

    @staticmethod
    def _playlist_display_items(module) -> list[str]:
        """The queue as listbox rows: basenames of the pending paths."""
        queue = module.params.get("playlist") or []
        return [os.path.basename(p) for p in queue]

    def _refresh_playlist_display(self, module_id: int) -> None:
        """Repaint a FilePlayer's queue listbox from its ``playlist`` param."""
        tag = self._playlist_listboxes.get(module_id)
        if tag is None or not dpg.does_item_exist(tag):
            return
        module = self.patch.modules.get(module_id)
        items = self._playlist_display_items(module) if module else []
        dpg.configure_item(tag, items=items)

    def _on_clear_playlist(self, sender, app_data, user_data) -> None:
        """Clear button: empty a FilePlayer's queue (the current track keeps
        playing; only the pending list is dropped)."""
        module_id = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        module.params["playlist"] = []
        self._refresh_playlist_display(module_id)
        self._set_status("Queue cleared")

    def _advance_file_playlists(self) -> None:
        """Auto-advance each FilePlayer queue once per frame.

        A one-shot track that just reached its end (rising edge of the
        backend's ``file_player_finished``) pops the head of the queue into
        ``path`` and rolls on; an empty queue leaves the player parked at the
        end (silence). As a convenience, a running player sitting on an empty
        ``path`` with a non-empty queue is kick-started so a fresh 'file
        list' plays without a manual Browse first. Backends without the hook
        (the pyo stub) no-op.
        """
        if not self._playlist_listboxes:
            return
        finished = getattr(self.backend, "file_player_finished", None)
        if finished is None:
            return
        running = self.backend.is_running
        for module_id in list(self._playlist_listboxes):
            module = self.patch.modules.get(module_id)
            if module is None:
                continue
            try:
                now_finished = bool(finished(module_id))
            except Exception:
                now_finished = False
            was_finished = self._fileplayer_prev_finished.get(module_id, False)
            self._fileplayer_prev_finished[module_id] = now_finished
            if not (module.params.get("playlist") or []):
                continue
            current = str(module.params.get("path") or "")
            if (now_finished and not was_finished) or (running and not current):
                self._advance_playlist(module_id)

    def _advance_playlist(self, module_id: int) -> None:
        """Pop the next queued file into a FilePlayer and let it play."""
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        queue = module.params.get("playlist")
        if not queue:
            return  # nothing queued: stay parked at the end (silence)
        next_path = queue.pop(0)
        try:
            # Same mutation path as Browse/typing; the renderer re-decodes
            # and restarts from 0:00 because the path param changed.
            self.backend.set_param(module_id, "path", next_path)
        except Exception as exc:
            self._set_status(f"Queue error: {exc}")
            return
        text_tag = f"fileplayer_path_{module_id}"
        if dpg.does_item_exist(text_tag):
            dpg.set_value(text_tag, next_path)
        self._refresh_playlist_display(module_id)
        self._set_status(f"Queue → {os.path.basename(next_path)}")

    # ----- device refresh ---------------------------------------------------

    @staticmethod
    def _device_combo_items(module_type: str, current_str: str) -> tuple[list[str], int]:
        """Build the device-combo item list for a device-bearing module.

        Returns ``(items, n_devices)``. Items always start with
        ``AUTO_DEVICE`` (the "auto-pick first available" empty string)
        and always contain ``current_str`` — a saved patch may pin a
        device that isn't plugged in right now, and the combo must keep
        showing it rather than silently switching selection.
        """
        if module_type == "midi_input":
            devices = midi_available_devices()
        elif module_type == "specific_stereo_speaker_output":
            devices = spk_available_devices()
        else:
            devices = mic_available_devices()
        items = [AUTO_DEVICE] + devices
        if current_str not in items:
            items = items + [current_str]
        return items, len(devices)

    def _on_refresh_devices(self, sender, app_data, user_data) -> None:
        """Refresh button beside a device combo: re-enumerate in place.

        Pure UI mutation — the selected value is untouched, so nothing
        recompiles until the user actually picks a device from the fresh
        list (same ``_on_param_changed`` path as before).
        """
        module_id, module_type = user_data
        tag = f"device_combo_{module_id}"
        if not dpg.does_item_exist(tag):
            return
        current_str = str(dpg.get_value(tag))
        items, n = self._device_combo_items(module_type, current_str)
        dpg.configure_item(tag, items=items)
        if module_type == "midi_input":
            kind = "MIDI"
        elif module_type == "specific_stereo_speaker_output":
            kind = "audio output"
        else:
            kind = "audio input"
        self._set_status(f"Refreshed {kind} devices: {n} found")

    # ----- fader_seq panel ---------------------------------------------------

    @classmethod
    def _fader_tip(cls, st: int) -> str:
        """Tooltip text for a fader position, e.g. '+7 st (G4)'."""
        midi = 60 + int(st)
        name = f"{cls._NOTE_NAMES[midi % 12]}{midi // 12 - 1}"
        return f"{int(st):+d} st ({name})"

    def _build_fader_seq_panel(self, module) -> None:
        """The fader-bank front panel: one labelled ``steps`` slider, then
        sixteen vertical pitch faders with only a step number and an on/off
        tickbox beneath each — no other text (hover a fader for its note).
        """
        dpg.add_slider_int(
            label="steps",
            default_value=int(module.params["steps"]),
            min_value=1,
            max_value=SEQ_MAX_STEPS,
            width=140,
            callback=self._on_param_changed,
            user_data=(module.id, "steps"),
        )
        with dpg.group(horizontal=True, horizontal_spacing=5):
            for i in range(1, SEQ_MAX_STEPS + 1):
                with dpg.group():
                    st = int(round(float(module.params[f"step{i}_pitch"])))
                    st = max(-FADER_RANGE_ST, min(FADER_RANGE_ST, st))
                    fader = dpg.add_slider_int(
                        vertical=True,
                        default_value=st,
                        min_value=-FADER_RANGE_ST,
                        max_value=FADER_RANGE_ST,
                        width=18,
                        height=96,
                        format="",
                        callback=self._on_fader_pitch,
                        user_data=(module.id, i),
                    )
                    with dpg.tooltip(fader):
                        dpg.add_text(
                            self._fader_tip(st),
                            tag=f"fader_tip_{module.id}_{i}",
                        )
                    dpg.add_text(f"{i}")
                    dpg.add_checkbox(
                        label="",
                        default_value=bool(module.params[f"step{i}_on"]),
                        callback=self._on_param_changed,
                        user_data=(module.id, f"step{i}_on"),
                    )

    def _on_fader_pitch(self, sender, app_data, user_data) -> None:
        """A pitch fader moved: write the (float) semitone param, update tip.

        Stored as float to keep the param type identical to the original
        sequencer's — one engine, one JSON shape.
        """
        module_id, i = user_data
        try:
            self.backend.set_param(module_id, f"step{i}_pitch", float(app_data))
        except Exception as exc:
            self._set_status(f"Param error: {exc}")
            return
        tip_tag = f"fader_tip_{module_id}_{i}"
        if dpg.does_item_exist(tip_tag):
            dpg.set_value(tip_tag, self._fader_tip(int(app_data)))

    # ----- per-key velocity calibration dialog ------------------------------

    _NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

    @classmethod
    def _note_label(cls, note: int) -> str:
        """Human key name for a raw MIDI note, e.g. 60 -> 'C4 (60)'."""
        return f"{cls._NOTE_NAMES[note % 12]}{note // 12 - 1} ({note})"

    def _vel_module(self):
        """The MIDIInput the open calibration dialog is bound to, or None."""
        if self._vel_target_id is None:
            return None
        return self.patch.modules.get(self._vel_target_id)

    def _show_velocity_dialog(self, sender, app_data, user_data) -> None:
        """Calibrate-keys button on a midi_input node: open the dialog.

        Deliberately NOT modal: learn mode needs the user playing the
        keyboard while the dialog sits open, and the rest of the UI
        (meters, transport) should stay live while they do.
        """
        module_id = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        # If a previous dialog was learning for another module, stop it.
        prev = self._vel_module()
        if prev is not None and prev.is_capturing_velocity:
            prev.stop_velocity_capture()
        self._vel_target_id = module_id
        self._vel_captured = {}
        if dpg.does_item_exist("vel_dialog"):
            dpg.delete_item("vel_dialog")
        with dpg.window(
            label=f"Calibrate keys — {module.name}",
            tag="vel_dialog",
            width=450,
            height=470,
            pos=(320, 130),
            on_close=self._on_vel_dialog_close,
        ):
            dpg.add_text(
                "Learn: play every key a few times at the same intended\n"
                "force, then Compute. Each key's hits are averaged and\n"
                "normalized to the mean captured level; hand-trim any\n"
                "multiplier below. Uncaptured keys stay at 1.0."
            )
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Learn", tag="vel_learn_btn", callback=self._on_vel_learn
                )
                dpg.add_button(label="Compute", callback=self._on_vel_compute)
                dpg.add_button(label="Clear all", callback=self._on_vel_clear)
                dpg.add_text("", tag="vel_capture_status")
            dpg.add_separator()
            dpg.add_group(tag="vel_table")
        self._rebuild_vel_table()

    def _on_vel_dialog_close(self, sender=None, app_data=None, user_data=None) -> None:
        """Dialog X clicked: stop (and stash) any in-flight capture."""
        module = self._vel_module()
        if module is not None and module.is_capturing_velocity:
            self._vel_captured = module.stop_velocity_capture()

    def _set_vel_curve(self, module, curve: dict) -> None:
        """Write a new velocity_curve through the canonical param path."""
        try:
            self.backend.set_param(module.id, "velocity_curve", curve)
        except Exception as exc:
            self._set_status(f"Param error: {exc}")
            return
        count_tag = f"vel_curve_count_{module.id}"
        if dpg.does_item_exist(count_tag):
            n = len(curve)
            dpg.set_value(count_tag, f"{n} keys calibrated" if n else "")
        self._rebuild_vel_table()

    def _rebuild_vel_table(self) -> None:
        """Repopulate the dialog's per-key multiplier rows from the param."""
        if not dpg.does_item_exist("vel_table"):
            return
        dpg.delete_item("vel_table", children_only=True)
        module = self._vel_module()
        if module is None:
            return
        curve = module.params.get("velocity_curve") or {}
        if not curve:
            dpg.add_text(
                "No calibration yet — every key plays at 1.0.",
                parent="vel_table",
            )
            return
        for key in sorted(curve, key=int):
            note = int(key)
            with dpg.group(horizontal=True, parent="vel_table"):
                dpg.add_text(f"{self._note_label(note):<10}")
                dpg.add_drag_float(
                    default_value=float(curve[key]),
                    speed=0.01,
                    min_value=0.0,
                    max_value=4.0,
                    format="%.3f x",
                    width=110,
                    callback=self._on_vel_mult_changed,
                    user_data=(module.id, key),
                )
                dpg.add_button(
                    label="x",
                    small=True,
                    callback=self._on_vel_remove,
                    user_data=(module.id, key),
                )

    def _on_vel_learn(self, sender, app_data, user_data=None) -> None:
        """Learn/Stop toggle. Stop stashes the capture for Compute."""
        module = self._vel_module()
        if module is None:
            return
        if module.is_capturing_velocity:
            self._vel_captured = module.stop_velocity_capture()
            dpg.configure_item("vel_learn_btn", label="Learn")
            dpg.set_value(
                "vel_capture_status",
                f"stopped: {len(self._vel_captured)} keys captured",
            )
        else:
            self._vel_captured = {}
            module.start_velocity_capture()
            dpg.configure_item("vel_learn_btn", label="Stop")
            dpg.set_value("vel_capture_status", "learning: 0 keys / 0 hits")

    def _on_vel_compute(self, sender, app_data, user_data=None) -> None:
        """Compute multipliers from the capture and merge into the curve.

        Merge (not replace): keys captured this round get fresh
        multipliers; previously-calibrated keys that weren't replayed
        keep theirs. Clear-all is the escape hatch for a from-scratch
        run.
        """
        module = self._vel_module()
        if module is None:
            return
        if module.is_capturing_velocity:
            self._vel_captured = module.stop_velocity_capture()
            dpg.configure_item("vel_learn_btn", label="Learn")
        captured = self._vel_captured
        if not captured:
            dpg.set_value("vel_capture_status", "nothing captured — Learn first")
            return
        curve = dict(module.params.get("velocity_curve") or {})
        curve.update(compute_velocity_curve(captured))
        self._vel_captured = {}
        self._set_vel_curve(module, curve)
        dpg.set_value(
            "vel_capture_status", f"calibrated {len(captured)} keys"
        )

    def _on_vel_clear(self, sender, app_data, user_data=None) -> None:
        module = self._vel_module()
        if module is None:
            return
        self._set_vel_curve(module, {})
        if dpg.does_item_exist("vel_capture_status"):
            dpg.set_value("vel_capture_status", "cleared")

    def _on_vel_mult_changed(self, sender, app_data, user_data) -> None:
        """A multiplier drag in the table: write that one key back."""
        module_id, key = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        curve = dict(module.params.get("velocity_curve") or {})
        curve[key] = float(app_data)
        try:
            self.backend.set_param(module_id, "velocity_curve", curve)
        except Exception as exc:
            self._set_status(f"Param error: {exc}")

    def _on_vel_remove(self, sender, app_data, user_data) -> None:
        """A row's remove button: drop that key back to implicit 1.0."""
        module_id, key = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        curve = dict(module.params.get("velocity_curve") or {})
        curve.pop(key, None)
        self._set_vel_curve(module, curve)

    def _update_velocity_capture(self) -> None:
        """Per-frame: live 'learning: N keys / M hits' readout."""
        if not dpg.does_item_exist("vel_capture_status"):
            return
        module = self._vel_module()
        if module is None or not module.is_capturing_velocity:
            return
        cap = module.snapshot_velocity_capture()
        hits = sum(len(v) for v in cap.values())
        dpg.set_value(
            "vel_capture_status", f"learning: {len(cap)} keys / {hits} hits"
        )

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
            # Drop any per-module file_player bookkeeping so a deleted queue
            # stops being polled (and its listbox tag isn't reused stale).
            self._file_pos_labels.pop(module_id, None)
            self._playlist_listboxes.pop(module_id, None)
            self._fileplayer_prev_finished.pop(module_id, None)
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
            self._set_buffer_slider_enabled(True)
            dpg.set_item_label(AUDIO_BTN_TAG, "Start audio")
            self._set_status(f"Backend: {self.backend.name}  |  stopped")
        else:
            try:
                # Apply the chosen buffer size before compile: numpy builds
                # its device outputs and pyo boots its server during compile,
                # so block_size must be set first.
                self.backend.set_block_size(self._buffer_size)
                self.backend.compile(self.patch)
                self.backend.start()
            except Exception as exc:
                traceback.print_exc()
                self._set_status(f"Audio start failed: {exc}")
                return
            self._set_buffer_slider_enabled(False)
            dpg.set_item_label(AUDIO_BTN_TAG, "Stop audio")
            self._set_status(
                f"Backend: {self.backend.name}  |  running"
                f"  |  buffer {self._buffer_size}"
            )

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
            self._capture_window_geometry()
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

    def _shift_down(self) -> bool:
        """True if either Shift key is currently held down (coarse scroll)."""
        for _name in ("mvKey_LShift", "mvKey_RShift", "mvKey_Shift"):
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

    def _debounce_key(self, key_code) -> bool:
        """True on the first press of ``key_code``, False while it stays held.

        ``add_key_press_handler`` re-fires at the OS key-repeat rate while a
        key is held, which made the Ctrl+zoom keys cycle through zoom levels
        instead of stepping once per press. Gating on ``_held_keys`` — the
        same set the computer-keyboard note handler debounces with — collapses
        the repeats to a single action; the global key-release handler clears
        the code on physical release, so the next real press re-fires.
        """
        if key_code in self._held_keys:
            return False
        self._held_keys.add(key_code)
        return True

    def _on_zoom_in_key(self, sender, app_data) -> None:
        if self._ctrl_down() and self._debounce_key(app_data):
            self._apply_zoom(step_zoom(self._zoom, +1))

    def _on_zoom_out_key(self, sender, app_data) -> None:
        if self._ctrl_down() and self._debounce_key(app_data):
            self._apply_zoom(step_zoom(self._zoom, -1))

    def _on_zoom_reset_key(self, sender, app_data) -> None:
        if self._ctrl_down() and self._debounce_key(app_data):
            self._apply_zoom(ZOOM_DEFAULT)

    def _on_zoom_wheel(self, sender, app_data) -> None:
        # Ctrl+wheel zooms — but only over empty canvas. Over a param widget,
        # Ctrl+wheel is the fine-adjust gesture (see _on_param_wheel), so yield.
        if not self._ctrl_down():
            return
        if self._hovered_param_widget() is not None:
            return
        self._apply_zoom(step_zoom(self._zoom, 1 if app_data > 0 else -1))

    def _on_param_wheel(self, sender, app_data) -> None:
        """Mouse wheel over a param widget nudges its value.

        Finds the hovered registered param widget and steps it one notch —
        the displayed-precision step for float sliders/drags, ±1 for int
        sliders, next/previous option for a combo, on/off for a checkbox —
        then pushes the value to the backend through the normal param-changed
        path. Modifiers scale a float notch: **Shift = ×10 (coarse)**,
        **Ctrl = ÷10 (fine)**. When the pointer is over a widget this takes
        priority over the Ctrl+wheel zoom (which only fires over empty canvas).
        ``app_data`` is the signed wheel delta.
        """
        wid = self._hovered_param_widget()
        if wid is None:
            return
        if self._ctrl_down():
            mult = 0.1        # fine
        elif self._shift_down():
            mult = 10.0       # coarse
        else:
            mult = 1.0
        try:
            self._nudge_param_widget(wid, app_data, mult)
        except Exception as exc:
            self._set_status(f"Scroll error: {exc}")

    def _hovered_param_widget(self):
        """The registered param-widget id under the mouse, or None. Prunes
        ids whose nodes have since been deleted."""
        found = None
        stale = []
        for wid in self._param_widgets:
            if not dpg.does_item_exist(wid):
                stale.append(wid)
                continue
            try:
                if dpg.is_item_hovered(wid):
                    found = wid
                    break
            except Exception:
                stale.append(wid)
        for wid in stale:
            self._param_widgets.pop(wid, None)
        return found

    def _nudge_param_widget(self, wid, direction, mult) -> None:
        """Step one param widget by a wheel notch scaled by ``mult`` (1.0
        normal, 10.0 coarse/Shift, 0.1 fine/Ctrl), dispatching on dpg type."""
        user_data = self._param_widgets[wid]
        wtype = dpg.get_item_info(wid).get("type", "")
        cfg = dpg.get_item_configuration(wid)
        cur = dpg.get_value(wid)
        if wtype.endswith("mvCheckbox"):
            new = direction > 0
        elif wtype.endswith("mvCombo"):
            items = list(cfg.get("items", []) or [])
            if not items:
                return
            idx = items.index(cur) if cur in items else 0
            new = items[cycle_index(idx, direction, len(items))]
        elif wtype.endswith(("mvSliderInt", "mvDragInt", "mvInputInt")):
            new = nudge_number(
                cur, direction,
                min_value=cfg.get("min_value", cur),
                max_value=cfg.get("max_value", cur),
                is_int=True, mult=mult,
            )
        elif wtype.endswith(("mvSliderFloat", "mvDragFloat", "mvInputFloat")):
            mn, mx = cfg.get("min_value"), cfg.get("max_value")
            if mn is None or mx is None or mn == mx:
                return  # unbounded drag — no range to step within
            new = nudge_number(cur, direction, min_value=mn, max_value=mx,
                               mult=mult,
                               decimals=decimals_from_format(cfg.get("format")))
        else:
            return  # text / path / unknown — not scrollable
        if new == cur:
            return
        dpg.set_value(wid, new)
        self._on_param_changed(wid, new, user_data)

    # ----- buffer size ----------------------------------------------------

    def _on_buffer_slider(self, sender, app_data) -> None:
        """Map the slider index to a buffer size and stash it for Start.

        The slider value is an index into ``BUFFER_SIZES``; rewrite the
        printf ``format`` so the handle shows the real frame count (e.g.
        "512") rather than the raw index. The size is persisted globally and
        applied to the backend on the next Start (see ``_on_toggle_audio``).
        """
        size = index_to_size(app_data)
        self._buffer_size = size
        self._persist_setting("buffer_size", size)
        if dpg.does_item_exist(BUFFER_SLIDER_TAG):
            try:
                dpg.configure_item(BUFFER_SLIDER_TAG, format=str(size))
            except Exception:
                pass

    def _set_buffer_slider_enabled(self, enabled: bool) -> None:
        """Enable/grey the buffer slider (disabled while audio runs).

        Buffer size is read only at Start, so editing it mid-run would be
        misleading; greying it out makes that explicit.
        """
        if not dpg.does_item_exist(BUFFER_SLIDER_TAG):
            return
        try:
            dpg.configure_item(BUFFER_SLIDER_TAG, enabled=enabled)
        except Exception:
            pass

    def _persist_setting(self, key, value) -> None:
        """Update one global setting and write the file, best-effort.

        A write failure (read-only dir, permission denied) must never break
        the UI, so it is logged and swallowed rather than raised.
        """
        self._settings[key] = value
        try:
            save_settings(self._settings)
        except Exception:
            traceback.print_exc()

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

    # ----- window geometry (per-patch) ------------------------------------

    @staticmethod
    def _virtual_screen_bounds():
        """Virtual-desktop rect ``(x, y, w, h)`` across all monitors, or None.

        Windows-only, via Win32 ``GetSystemMetrics``. Returns None on other
        platforms or if the query fails — in which case a saved position is
        not restored (only the size is), since a blind position is the part
        that can strand the window off-screen.
        """
        try:
            import ctypes

            get = ctypes.windll.user32.GetSystemMetrics
            # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77,
            # SM_CXVIRTUALSCREEN=78, SM_CYVIRTUALSCREEN=79.
            bounds = (get(76), get(77), get(78), get(79))
            if bounds[2] <= 0 or bounds[3] <= 0:
                return None
            return bounds
        except Exception:
            return None

    def _capture_window_geometry(self) -> None:
        """Snapshot viewport size + position into ``patch.ui["window"]``.

        Called before save. DPG 2.3.1 exposes no maximized-state query, so
        only size and position are stored (see ui/window_geometry).
        """
        try:
            width = dpg.get_viewport_width()
            height = dpg.get_viewport_height()
            pos = dpg.get_viewport_pos()
        except Exception:
            return
        geo = make_geometry(width, height, pos[0], pos[1])
        if geo is not None:
            self.patch.ui["window"] = geo

    def _apply_window_geometry(self) -> None:
        """Restore ``patch.ui["window"]`` to the viewport, off-screen-safe.

        Size is always restored (clamped to the desktop); position only when
        it lands inside the current virtual desktop. See
        ui/window_geometry.resolve.
        """
        resolved = resolve_window(
            self.patch.ui.get("window"), self._virtual_screen_bounds()
        )
        if resolved is None:
            return
        try:
            dpg.set_viewport_width(resolved["width"])
            dpg.set_viewport_height(resolved["height"])
            if resolved["x"] is not None and resolved["y"] is not None:
                dpg.set_viewport_pos([resolved["x"], resolved["y"]])
        except Exception:
            traceback.print_exc()

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

        # Restore the saved window size/position (off-screen-safe).
        self._apply_window_geometry()

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
        self._playlist_listboxes.clear()
        self._fileplayer_prev_finished.clear()
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

    def _update_dsp_load(self) -> None:
        """Refresh the toolbar DSP-load readout from the backend.

        Reads the backend's lock-free snapshot when audio is running
        (and the backend publishes one -- getattr-guarded so a backend
        without the observable just leaves the readout greyed out).
        """
        if not dpg.does_item_exist(DSP_TEXT_TAG):
            return
        load = None
        if self.backend.is_running:
            snap = getattr(self.backend, "dsp_load_snapshot", None)
            if snap is not None:
                load = snap()[0]
        dpg.set_value(DSP_TEXT_TAG, format_dsp_load(load))
        dpg.configure_item(DSP_TEXT_TAG, color=load_color(load))

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

    # Meter display geometry (pixels inside each channel drawlist).
    _AMETER_BAR_W = 146.0
    _AMETER_H = 16.0
    _AMETER_LAMP = ((152.0, 2.0), (166.0, 14.0))
    _AMETER_FILL_RGBA = (90, 190, 120, 255)
    _AMETER_TICK_RGBA = (235, 235, 235, 255)
    _AMETER_LAMP_ON = (235, 64, 52, 255)
    _AMETER_LAMP_OFF = (60, 34, 34, 255)

    def _build_meter_display(self, module_id: int, show: bool = True) -> dict:
        """One meter channel: a drawlist with bar, tick, lamp and text.

        Layout (208x16): the level bar spans x 0..146 with its fill
        rect growing from the left; the peak-hold tick is a 2 px
        vertical line over the bar; the clip lamp is the small rect at
        x 152..166 (dim red normally, bright red while lit); the clip
        counter rides at x 170 (hidden while zero); the dB/LUFS readout
        is drawn over the bar's left edge. Clicking the row zeroes the
        module's clip counters. Returns the tag dict
        ``_update_audio_meters`` reconfigures each frame.
        """
        with dpg.drawlist(width=208, height=int(self._AMETER_H), show=show) as dl:
            dpg.draw_rectangle(
                (0, 0), (self._AMETER_BAR_W, self._AMETER_H - 1),
                fill=(28, 30, 34, 255), color=(70, 72, 78, 255),
            )
            fill = dpg.draw_rectangle(
                (1, 1), (1, self._AMETER_H - 2),
                fill=self._AMETER_FILL_RGBA, color=(0, 0, 0, 0),
            )
            tick = dpg.draw_line(
                (1, 1), (1, self._AMETER_H - 2),
                color=self._AMETER_TICK_RGBA, thickness=2,
            )
            lamp = dpg.draw_rectangle(
                self._AMETER_LAMP[0], self._AMETER_LAMP[1],
                fill=self._AMETER_LAMP_OFF, color=(110, 60, 60, 255),
            )
            text = dpg.draw_text(
                (4, 1), "-inf dB", size=12, color=(225, 225, 225, 255)
            )
            clips = dpg.draw_text(
                (170, 1), "", size=12, color=(235, 120, 110, 255)
            )
        # Click anywhere on the row -> zero this meter's clip counters.
        try:
            with dpg.item_handler_registry() as reg:
                dpg.add_item_clicked_handler(
                    callback=self._on_meter_clips_reset, user_data=module_id
                )
            dpg.bind_item_handler_registry(dl, reg)
        except Exception:
            pass
        return {"dl": dl, "fill": fill, "tick": tick, "lamp": lamp,
                "text": text, "clips": clips}

    def _on_meter_clips_reset(self, sender, app_data, user_data) -> None:
        """Clip-counter click target: zero the meter's counters."""
        reset = getattr(self.backend, "reset_meter_clips", None)
        if reset is not None:
            reset(int(user_data))

    def _meter_fill(self, value: float) -> float:
        """Linear amp -> 0..1 fill on the fixed -90..0 dBFS scale.

        Fixed, not auto-ranged, so any two meters are directly
        comparable (and L/R of one stereo pair line up).
        """
        if value <= 1e-9:
            return 0.0
        db = 20.0 * math.log10(value)
        db = max(self._METER_FLOOR_DB, min(0.0, db))
        return (db - self._METER_FLOOR_DB) / (0.0 - self._METER_FLOOR_DB)

    def _draw_meter_channel(self, ch: dict, chan, unit: str = "dB",
                            text_level=None, show_text: bool = True,
                            clips_value=None) -> None:
        """Reconfigure one channel drawlist from ``(level, hold, clip,
        clips)``.

        ``unit`` labels the readout ("dB" or "LUFS"); ``text_level``
        overrides the number the text shows (the stereo-linked pair
        readout) while the bar still draws ``level``; ``show_text``
        hides the R row's text when the pair shares one readout;
        ``clips_value`` overrides the displayed clip count (the linked
        pair shows the summed tally on the L row).
        """
        level, hold, clip = chan[0], chan[1], chan[2]
        clips = chan[3] if len(chan) > 3 else 0
        if clips_value is not None:
            clips = clips_value
        fl = self._meter_fill(level)
        fh = self._meter_fill(hold)
        x_fill = 1.0 + fl * (self._AMETER_BAR_W - 2.0)
        x_tick = 1.0 + fh * (self._AMETER_BAR_W - 2.0)
        tl = level if text_level is None else text_level
        if not show_text:
            overlay = ""
        elif tl > 1e-9 and self._meter_fill(tl) > 1e-6:
            db = max(self._METER_FLOOR_DB, 20.0 * math.log10(max(tl, 1e-9)))
            overlay = f"{min(0.0, db):.1f} {unit}"
        else:
            overlay = f"-inf {unit}"
        try:
            dpg.configure_item(ch["fill"], pmax=(x_fill, self._AMETER_H - 2))
            dpg.configure_item(
                ch["tick"], p1=(x_tick, 1.0), p2=(x_tick, self._AMETER_H - 2)
            )
            dpg.configure_item(
                ch["lamp"],
                fill=self._AMETER_LAMP_ON if clip else self._AMETER_LAMP_OFF,
            )
            dpg.configure_item(ch["text"], text=overlay)
            dpg.configure_item(
                ch["clips"], text=(f"x{clips}" if clips else "")
            )
        except Exception:
            pass

    def _update_audio_meters(self) -> None:
        """Push Meter-module indicator triples into their displays.

        Reads a snapshot of the backend's per-meter ``(left, right)``
        channel triples and reconfigures each channel's drawlist: bar
        fill and dB text from the level (peak or RMS, per the module's
        ``mode``), tick position from the peak-hold value, lamp colour
        from the clip flag. The R display is shown/hidden to track
        whether ``in_r`` is patched (None in the snapshot = hidden).
        Cheap: a handful of meters, a couple of log10s each.
        """
        if not self._audio_meter_bars:
            return
        snap = getattr(self.backend, "snapshot_audio_meters", None)
        if snap is None:
            return
        meters = snap()
        for module_id, bundle in self._audio_meter_bars.items():
            entry = meters.get(module_id)
            if entry is None:
                continue
            left, right, linked, mode, pair_level = entry
            unit = "LUFS" if str(mode).startswith("lufs") else "dB"
            if linked and right is not None:
                # Master readout: one number (and one summed clip
                # tally) on the L row; the R row keeps only its bar,
                # tick and lamp (already pair-merged by the backend).
                clips_sum = (left[3] if len(left) > 3 else 0) + (
                    right[3] if len(right) > 3 else 0
                )
                self._draw_meter_channel(
                    bundle["l"], left, unit,
                    text_level=pair_level, clips_value=clips_sum,
                )
                self._draw_meter_channel(
                    bundle["r"], right, unit, show_text=False, clips_value=0
                )
                want_r = True
            else:
                self._draw_meter_channel(bundle["l"], left, unit)
                want_r = right is not None
                if want_r:
                    self._draw_meter_channel(bundle["r"], right, unit)
            if want_r != bundle["r_shown"]:
                try:
                    dpg.configure_item(bundle["r"]["dl"], show=want_r)
                except Exception:
                    pass
                bundle["r_shown"] = want_r

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

    First wires the global uncaught-crash hooks (worker threads /
    ``__del__`` -> ~/.pysynthrack/crashes/ via ``_crash.install_crash_logging``),
    then wraps ``App().run()`` so any fatal error (DPG hard-exit territory,
    viewport setup failures, callback explosions, anything escaping the
    per-callback try/except'es in App) is captured by the error handler,
    written to the crash folder, and the process exits **non-zero with a
    friendly pointer instead of dumping a Python traceback**.

    Only ``Exception`` is caught, so ``KeyboardInterrupt`` / ``SystemExit``
    (a normal Ctrl-C or an explicit quit) propagate untouched. The reporter is
    itself wrapped so a failure inside ``describe_error`` / ``write_crash_report``
    falls back to a raw traceback rather than hiding the original error.
    """
    from .. import _crash

    _crash.install_crash_logging()
    try:
        App().run()
    except Exception as e:
        try:
            from ..error_handler import describe_error

            # Guard so the global observer doesn't also write this report --
            # we write it ourselves below with the precise "gui" source tag.
            with _crash.explicit_write():
                report = describe_error(e, include_locals=True)
            path = _crash.write_crash_report(report, source="gui")
            print(
                f"[pysynthrack] Fatal error: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            if path:
                print(
                    f"[pysynthrack] Crash report written to:\n  {path}\n"
                    "  Share this file when reporting the bug.",
                    file=sys.stderr,
                )
            else:
                print(
                    "[pysynthrack] (Crash report could not be written.)",
                    file=sys.stderr,
                )
        except Exception:
            # Reporter itself failed - fall back to a raw traceback so the
            # original error still surfaces somewhere.
            traceback.print_exc()
        # Suppress the traceback, but exit non-zero so scripts / CI still see
        # the failure.
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover - GUI entry
    main()
