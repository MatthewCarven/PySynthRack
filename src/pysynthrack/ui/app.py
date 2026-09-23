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
import time
import traceback
from typing import Optional

import dearpygui.dearpygui as dpg
import numpy as np

# Ensure all module types are registered before we build any UI.
import pysynthrack.modules  # noqa: F401

from ..audio import AudioBackend, pick_backend
from ..core.module import grouped_module_types
from ..core.patch import Cable, Patch
from ..io_patch import load_patch, save_patch
from ..modules.filter import FILTER_MODES
from ..modules.fm_op import RATIO_TABLE as FM_RATIO_TABLE, snap_ratio as fm_snap_ratio
from ..modules.keyboard import (
    midi_to_name,
    name_to_midi,
    semitone_to_midi,
)
from ..modules.key_trigger import KEY_TRIGGER_MODES
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
from ..modules.chaos import CHAOS_SYSTEMS
from ..modules.organ import (
    ORGAN_BARS,
    ORGAN_FOOTAGES,
    ORGAN_VIBRATO,
    PERC_DECAYS,
    PERC_MODES,
)
from ..modules.clock import CLOCK_MAX_SWING_UI
from ..modules.clockwork import DIVIDER_MAX_M, DIVIDER_MAX_N, DIVIDER_MAX_SWING
from ..modules.drums import HAT_TONE_MAX, HAT_TONE_MIN
from ..modules.sequencer import MAX_STEPS as SEQ_MAX_STEPS, SEQ_DIRECTIONS
from ..modules.sampler import (
    ROOT_MAX_NOTE as SAMPLER_ROOT_MAX,
    ROOT_MIN_NOTE as SAMPLER_ROOT_MIN,
    SAMPLER_MODES,
)
from ..modules.possibility_seq import (
    MAX_STEPS as PSEQ_MAX_STEPS,
    POSSIBILITY_MODES,
    format_possibilities,
    next_state as pseq_next_state,
)
from ..modules.possibility_selector import (
    MAX_STEPS as PSEL_MAX_STEPS,
    N_OUTS as PSEL_N_OUTS,
    format_possibilities as psel_format_possibilities,
    format_route as psel_format_route,
    next_state as psel_next_state,
    parse_route as psel_parse_route,
)
from ..modules.wavetable_morph import WT_STACKS
from ..modules.wind import WIND_MODELS
from ..modules.drift import DRIFT_MODES
from ..modules.cv_recorder import (
    CV_RECORDER_MODES, CV_RECORDER_PLAY_MODES, CV_RECORDER_SPEEDS)
from ..modules.vowel import (
    VOWEL_CUSTOM_FREQ_MAX,
    VOWEL_CUSTOM_FREQ_MIN,
    VOWEL_CV_RATES,
    VOWEL_VOICE_CHOICES,
)
from ..modules.freeze import FREEZE_SIZES
from ..modules.samplehold import SAMPLE_HOLD_MODES
from ..modules.compressor import DETECTOR_MODES
from ..modules.distortion import DISTORTION_MODES
from ..modules.meter import METER_MODES
from ..modules.waveshaper import WAVESHAPER_MODES
from ..modules.noise import (
    NOISE_COLORS,
    NOISE_CORNER_MAX,
    NOISE_CORNER_MIN,
)
from ..modules.oscillator import PULSE_WIDTH_MAX, PULSE_WIDTH_MIN, WAVEFORMS
from ..modules.quantizer import (
    CUSTOM_KEYS as QUANTIZER_CUSTOM_KEYS,
    QUANTIZER_ROOTS,
    QUANTIZER_SCALES,
)
from ..modules.arpeggiator import ARP_MODES
from ..modules.autopan import AUTOPAN_LAWS, AUTOPAN_SHAPES
from ..modules.chord import (
    CHORD_ENABLE_KEYS,
    CHORD_INTERVAL_KEYS,
    CHORD_PRESETS,
)
from ..modules.clockwork import BERNOULLI_MODES
from ..modules.function_generator import FUNCTION_GENERATOR_MODES
from ..modules.modal import MODAL_MATERIALS
from ..modules.scope import SCOPE_MODES, SCOPE_TRIGGER_MODES
from ..modules.slew import SLEW_SHAPES
from ..modules.rotary import ROTARY_SPEEDS
from ..modules.granular import GRANULAR_WINDOWS
from ..modules.sweep_eq import SWEEP_EQ_MODES
from . import scope_math
from ..modules.transient_shaper import TRANSIENT_SHAPER_SPEEDS
from .dsp_load import (
    IDLE_COLOR,
    WARN_COLOR,
    diagnose,
    format_dsp_load,
    format_host_api,
    format_xruns,
    load_color,
    xrun_color,
)
from .node_layout import find_free_position
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
    SINK_BUFFER_SIZES,
    coerce_buffer_size,
    coerce_sink_buffer_size,
    format_sink_buffer,
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


# ----- raw-key binding map (KeyTrigger) ------------------------------------
#
# KeyTrigger binds an *arbitrary* single key, not a note-mapped one, so it
# needs a plain key-code <-> name map covering every bindable key. Names are
# the portable, serialized identity (stored in the module's ``key`` param);
# codes are the runtime dpg ``mvKey_*`` values. Reserved keys are deliberately
# *absent* (Delete/Backspace remove nodes, Escape/Tab/Enter navigate), so the
# app's own shortcuts always win — an unmapped code is simply un-bindable.
_KEY_CODE_TO_NAME: dict[int, str] = {}
_KEY_NAME_TO_CODE: dict[str, int] = {}

# mvKey_* suffixes offered for binding. Letters + the number row + common
# punctuation + Space. Punctuation-constant spellings vary across dpg
# versions, so several are listed and the missing ones skipped (like the note
# map). The suffix doubles as the human-readable, serialized name.
_BINDABLE_KEY_SUFFIXES: tuple[str, ...] = (
    tuple(chr(c) for c in range(ord("A"), ord("Z") + 1))       # A..Z
    + tuple(str(d) for d in range(10))                          # 0..9
    + ("Spacebar", "Space",
       "Comma", "Period", "Slash", "Semicolon", "Apostrophe", "Quote",
       "Open_Brace", "Close_Brace", "Backslash", "Minus", "Plus", "Equal",
       "Tilde", "Backtick")
)


def _init_raw_key_map(dpg_module) -> None:
    """Build the KeyTrigger key-code <-> name maps from real ``mvKey_*``
    constants (runtime, like :func:`_init_key_map`). Idempotent."""
    if _KEY_CODE_TO_NAME:
        return
    for suffix in _BINDABLE_KEY_SUFFIXES:
        code = getattr(dpg_module, f"mvKey_{suffix}", None)
        if code is None or code in _KEY_CODE_TO_NAME:
            continue
        _KEY_CODE_TO_NAME[code] = suffix
        _KEY_NAME_TO_CODE[suffix] = code

EDITOR_TAG = "node_editor"
AUDIO_BTN_TAG = "audio_btn"
STATUS_TEXT_TAG = "status_text"
MAIN_WINDOW_TAG = "main_window"
ZOOM_SLIDER_TAG = "zoom_slider"
BUFFER_SLIDER_TAG = "buffer_slider"
DSP_TEXT_TAG = "dsp_load_text"
XRUN_TEXT_TAG = "xrun_text"
HOSTAPI_TEXT_TAG = "hostapi_text"
LOOPS_TEXT_TAG = "loops_text"
LOOPS_TOOLTIP_TAG = "loops_tooltip_text"
# A cable that closes a feedback loop is drawn in this (amber) so the one
# block of latency it carries is never a mystery. See _refresh_late_links.
LATE_LINK_COLOR = (235, 160, 40, 255)
LATE_LINK_HOVER_COLOR = (255, 200, 90, 255)
DSP_TOOLTIP_TAG = "dsp_load_tooltip_text"
XRUN_TOOLTIP_TAG = "xrun_tooltip_text"

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
        # Feedback door: the cable keys the backend will read one block
        # late for the CURRENT patch (recomputed on every cable/module
        # change, not per frame), the dpg theme that paints them, and
        # the last readout text so the toolbar is only touched on change.
        self._late_keys: set[tuple[int, str, int, str]] = set()
        self._late_link_theme: int | None = None
        self._loops_text: str = ""

        # dpg-id → (module_id, param_name) for every scrollable param widget,
        # so a mouse wheel over one can nudge its value. Filled as nodes are
        # built; stale ids (deleted nodes) are pruned lazily on scroll.
        self._param_widgets: dict[int, tuple[int, str]] = {}

        # possibility_seq panel bookkeeping. ``_pseq_cells`` maps module_id ->
        # its sixteen step-cell button ids (index 0 = step 1) and
        # ``_pseq_count_labels`` -> the possibility-count text, both so a
        # click can repaint the whole face from the model. ``_pseq_themes``
        # caches one button theme per cell colour ("0"/"1"/"?"/"off"),
        # shared by every node — themes are global dpg items.
        self._pseq_cells: dict[int, list[int]] = {}
        self._pseq_count_labels: dict[int, int] = {}
        self._pseq_themes: dict[str, int] = {}
        # possibility_selector panel: the same shape, one level up (cells
        # keyed by route, themes per output colour).
        self._psel_cells: dict[int, list[int]] = {}
        self._psel_count_labels: dict[int, int] = {}
        self._psel_themes: dict[str, int] = {}

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
        # KeyTrigger raw-key path (independent of the note _held_keys so it
        # can't tangle with the note/zoom debounce). ``_raw_key_down`` debounces
        # OS repeat for the raw dispatch; ``_key_learn_target`` is the module id
        # currently in Learn mode (None = not learning); ``_text_input_tags``
        # lists text fields whose focus suppresses raw triggers (so a bound
        # letter doesn't fire while you're typing a path/param).
        self._raw_key_down: set[int] = set()
        self._key_learn_target: int | None = None
        self._text_input_tags: set[str] = set()

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
        # Scope displays: module id -> {"dl", "p1", "p2"} drawlist tags,
        # repainted each frame from the backend's scope_window hook via
        # ui/scope_math (meter-display lifecycle: pruned on delete/load).
        self._scope_displays: dict[int, dict] = {}
        # Sampler waveform faces: module id -> drawlist item tags plus the
        # (path, region) key last painted, so _update_sampler_faces only
        # redraws when the file or a marker actually moved.
        self._sampler_faces: dict[int, dict] = {}
        self._meter_bounds: dict[tuple[int, str], list[float]] = {}
        # FilePlayer playhead readouts. Maps module_id -> the dpg text
        # tag showing 'elapsed / total'; refreshed each frame in
        # _update_file_positions from the backend's snapshot hook.
        self._file_pos_labels: dict[int, int] = {}
        # FilePlayer seek / scrub bar. ``_file_seek_sliders`` maps module_id
        # -> the dpg slider (0..1 fraction) under the transport row. Each
        # frame _update_file_positions drives the thumb to the playhead —
        # unless the module_id is in ``_file_seek_active`` (the user is
        # dragging it), in which case the thumb is left to follow the mouse
        # and the fractional position is handed to the backend on release.
        # Interaction is polled via dpg.is_item_active, so there is no
        # per-widget handler registry to track and free.
        self._file_seek_sliders: dict[int, int] = {}
        self._file_seek_active: set[int] = set()
        # Buffered-sink ring readouts (buffered_specific_speaker_output).
        # ``_sink_buffer_labels`` maps module_id -> the dpg text tag showing
        # the sink's secondary-stream ring fill / underruns / drops;
        # refreshed each frame in _update_sink_buffers from the backend's
        # snapshot hook. ``_sink_buffer_last`` remembers the previous
        # (underruns, drops) pair so a fresh count *increase* can arm
        # ``_sink_buffer_flash`` — a monotonic deadline until which the
        # readout is tinted amber, making a struggling ring catch the eye
        # without the cumulative counters tinting it forever.
        self._sink_buffer_labels: dict[int, int] = {}
        self._sink_buffer_last: dict[int, tuple[int, int]] = {}
        self._sink_buffer_flash: dict[int, float] = {}
        # FilePlayer queue ("file list"). ``_playlist_listboxes`` maps
        # module_id -> the dpg listbox tag showing the upcoming tracks;
        # ``_fileplayer_advanced_gen`` remembers the backend decode
        # "generation" the queue was last advanced at, so
        # _advance_file_playlists fires exactly once per track end — keyed
        # on decode identity, not a bool edge, so a bad track whose decode
        # fails between two polls is still skipped (not stalled on) once.
        self._playlist_listboxes: dict[int, int] = {}
        self._fileplayer_advanced_gen: dict[int, int] = {}
        # (module_id, path) pairs already named in the status bar, so a
        # failed media load is reported ONCE rather than every frame.
        self._media_failures_reported: set[tuple[int, str]] = set()

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
                self._update_scopes()
                self._update_sampler_faces()
                self._update_dsp_load()
                self._update_sink_buffers()
                self._update_file_positions()
                self._advance_file_playlists()
                self._report_media_failures()
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
                with dpg.tooltip(DSP_TEXT_TAG):
                    dpg.add_text(
                        "Share of the block budget recent renders used.\n"
                        "Above 100% the block missed real time.",
                        tag=DSP_TOOLTIP_TAG,
                    )
                dpg.add_spacer(width=12)
                # Device underflows reported by PortAudio itself. Read
                # next to DSP% these separate a patch that is too
                # expensive from a callback that merely arrived late --
                # see ui/dsp_load.diagnose, which writes the tooltip.
                dpg.add_text("xrun --", tag=XRUN_TEXT_TAG, color=IDLE_COLOR)
                with dpg.tooltip(XRUN_TEXT_TAG):
                    dpg.add_text("Audio stopped.", tag=XRUN_TOOLTIP_TAG)
                dpg.add_spacer(width=12)
                # Which host API the stream actually landed on. The
                # stream opens on the system default, so this is
                # informational rather than a setting -- but it decides
                # whether jitter is ours or the host API's.
                dpg.add_text(
                    "api --", tag=HOSTAPI_TEXT_TAG, color=IDLE_COLOR
                )
                with dpg.tooltip(HOSTAPI_TEXT_TAG):
                    dpg.add_text(
                        "PortAudio host API the output stream opened on.\n"
                        "MME is the Windows default and the most "
                        "jitter-prone; WASAPI generally schedules better."
                    )
                dpg.add_spacer(width=12)
                # Feedback loops in the patch: how many cables read one
                # block late (drawn amber in the editor), and -- while
                # running -- whether any loop blew past float range and
                # got scrubbed. Lives here rather than in the status bar
                # because a status line is gone the next time anything
                # else has something to say. See _refresh_late_links.
                dpg.add_text("loops --", tag=LOOPS_TEXT_TAG, color=IDLE_COLOR)
                with dpg.tooltip(LOOPS_TEXT_TAG):
                    dpg.add_text(
                        "No feedback loops in this patch.",
                        tag=LOOPS_TOOLTIP_TAG,
                    )

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
        _init_raw_key_map(dpg)
        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key_press)
            dpg.add_key_release_handler(callback=self._on_key_release)
            # KeyTrigger raw-key path: a second global pair, delivering every
            # bindable key by name to ACCEPTS_RAW_KEYS modules (and driving
            # Learn). Separate from the note handlers above; both fire per key.
            dpg.add_key_press_handler(callback=self._on_raw_key_press)
            dpg.add_key_release_handler(callback=self._on_raw_key_release)
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

    def _existing_node_rects(self):
        """Current node rects as logical ``(x, y, w, h)`` for placement.

        dpg reports position and size in scaled (on-canvas) pixels, so both are
        divided by the zoom factor to match the logical space find_free_position
        works in. A node that hasn't rendered yet reports 0x0 and is simply
        passed through — the placement helper ignores non-positive sizes. Any
        dpg hiccup (missing item) drops that one node rather than failing the
        add.
        """
        z = self._zoom or 1.0
        rects = []
        for node_id in self._node_to_module:
            try:
                p = dpg.get_item_pos(node_id)
                s = dpg.get_item_rect_size(node_id)
            except Exception:
                continue
            if not p or not s:
                continue
            rects.append((p[0] / z, p[1] / z, s[0] / z, s[1] / z))
        return rects

    def _create_node_for_module(self, module, pos=None) -> int:
        if pos is None:
            preferred = (self._next_node_pos[0], self._next_node_pos[1])
            # Stagger downward and right for the next node.
            self._next_node_pos[0] = (self._next_node_pos[0] + 220) % 800
            self._next_node_pos[1] = (self._next_node_pos[1] + 60) % 500
            # Nudge off any existing node so the new title bar never lands on a
            # lower node's slider (imnodes would then yield the drag to that
            # slider — the click-through bug). find_free_position works in
            # logical coords, so un-zoom each existing rect before the test.
            pos = find_free_position(self._existing_node_rects(), preferred)

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
                    dpg.add_text(f"< {port.name}")
                self._attr_to_port[attr_id] = (module.id, port.name, "in")
                self._port_to_attr[(module.id, port.name, "in")] = attr_id

            # Parameters (static, no circle). fader_seq draws its own
            # compact fader-bank panel instead of 33 labelled param rows.
            if module.TYPE == "fader_seq":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    self._build_fader_seq_panel(module)
            elif module.TYPE == "possibility_seq":
                # Sixteen click-to-cycle step cells instead of 36 labelled
                # rows — the whole param set lives on the panel.
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    self._build_possibility_panel(module)
            elif module.TYPE == "possibility_selector":
                # The same face one level up: cells hold routes, not bits.
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    self._build_selector_panel(module)
            else:
                # organ's nine drawbars render as a compact fader bank
                # (fader_seq lineage); its remaining params fall through
                # to the normal loop below.
                if module.TYPE == "organ":
                    with dpg.node_attribute(
                        attribute_type=dpg.mvNode_Attr_Static
                    ):
                        self._build_organ_drawbars(module)
                # matrix_mixer's sixteen gains render as a 4×4 drag grid
                # (soft_clip falls through to the normal loop).
                if module.TYPE == "matrix_mixer":
                    with dpg.node_attribute(
                        attribute_type=dpg.mvNode_Attr_Static
                    ):
                        self._build_matrix_grid(module)
                for param_name, default in module.DEFAULT_PARAMS.items():
                    # file_player's queue is not a scalar widget — it gets a
                    # dedicated listbox + Add/Clear panel in the block below.
                    if module.TYPE == "file_player" and param_name == "playlist":
                        continue
                    # key_trigger's bound key gets a Learn button + label
                    # panel below instead of a raw text box.
                    if module.TYPE == "key_trigger" and param_name == "key":
                        continue
                    # organ's bar1..bar9 are drawn by the drawbar bank.
                    if module.TYPE == "organ" and param_name.startswith("bar"):
                        continue
                    # matrix_mixer's g11..g44 are drawn by the drag grid.
                    if (
                        module.TYPE == "matrix_mixer"
                        and len(param_name) == 3
                        and param_name.startswith("g")
                    ):
                        continue
                    with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                        self._add_param_widget(module, param_name, default)
                        wid = dpg.last_item()
                        if wid:
                            self._param_widgets[wid] = (module.id, param_name)

            # Buffered family (plain + warping): a live readout of the sink's
            # secondary-stream hand-off ring — fill %, queued/capacity
            # samples, and cumulative underrun / drop counts
            # (_update_sink_buffers ticks it each frame from the backend's
            # snapshot hook). Idle grey until the sink actually has a
            # stream: transport running AND a named device picked.
            if module.TYPE in (
                "buffered_specific_speaker_output",
                "warping_buffered_speaker_output",
            ):
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    label = dpg.add_text(
                        format_sink_buffer(None),
                        color=self._SINK_BUF_IDLE_RGBA,
                    )
                    self._sink_buffer_labels[module.id] = label

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
                        # Skip to the next queued track (no-op if the queue
                        # is empty). The manual mate to the auto-advance.
                        dpg.add_button(
                            label=">>|",
                            width=32,
                            callback=self._on_file_transport,
                            user_data=(module.id, "next"),
                        )
                        label = dpg.add_text("0:00 / 0:00")
                        self._file_pos_labels[module.id] = label
                # A seek / scrub bar under the transport row. Its thumb is
                # driven to the playhead each frame by _update_file_positions;
                # while the user drags it (dpg.is_item_active) the thumb is
                # left to follow the mouse, and on release the fractional
                # position is handed to the backend to seek. format="" hides
                # the raw 0..1 value — the 'elapsed / total' text above is the
                # readable time. The callback only flags an in-progress scrub
                # so a quick click, which can fall between two frame polls,
                # still commits on the release check.
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    seek = dpg.add_slider_float(
                        default_value=0.0,
                        min_value=0.0,
                        max_value=1.0,
                        format="",
                        width=200,
                        callback=self._on_file_seek,
                        user_data=module.id,
                    )
                    self._file_seek_sliders[module.id] = seek
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
                            label="Remove",
                            callback=self._on_remove_playlist_item,
                            user_data=module.id,
                        )
                        dpg.add_button(
                            label="Clear",
                            callback=self._on_clear_playlist,
                            user_data=module.id,
                        )

            # KeyTrigger: a Learn button that binds the next key press, plus a
            # label showing the currently bound key. (The ``mode`` param renders
            # as a normal combo in the loop above; only ``key`` is custom.)
            if module.TYPE == "key_trigger":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    with dpg.group(horizontal=True):
                        dpg.add_button(
                            label="Learn",
                            tag=f"keytrigger_learnbtn_{module.id}",
                            callback=self._on_key_learn,
                            user_data=module.id,
                        )
                        dpg.add_text(
                            self._keytrigger_key_caption(
                                str(module.params.get("key") or "")
                            ),
                            tag=f"keytrigger_keylabel_{module.id}",
                            color=(170, 170, 170),
                        )

            # Scope: the waveform face — a drawlist with a background,
            # centre gridlines, and two polylines (main + second trace)
            # repainted each frame by _update_scopes from the backend's
            # capture rings. Both polylines exist up front; trace 2 shows
            # only in dual/xy with in_r patched.
            if module.TYPE == "scope":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    w, h = self._SCOPE_W, self._SCOPE_H
                    with dpg.drawlist(width=w, height=h) as dl:
                        dpg.draw_rectangle(
                            (0, 0), (w - 1, h - 1),
                            fill=(16, 20, 16, 255), color=(70, 78, 70, 255),
                        )
                        dpg.draw_line(
                            (0, h / 2), (w - 1, h / 2),
                            color=(50, 60, 50, 255),
                        )
                        dpg.draw_line(
                            (w / 2, 0), (w / 2, h - 1),
                            color=(50, 60, 50, 255),
                        )
                        p1 = dpg.draw_polyline(
                            [(0, h / 2), (w - 1, h / 2)],
                            color=(120, 230, 130, 255), thickness=1,
                        )
                        p2 = dpg.draw_polyline(
                            [(0, h / 2), (w - 1, h / 2)],
                            color=(230, 190, 90, 255), thickness=1,
                            show=False,
                        )
                    self._scope_displays[module.id] = {
                        "dl": dl, "p1": p1, "p2": p2, "p2_shown": False,
                    }

            # Sampler: the waveform face -- the loaded file's min/max
            # envelope (built once by the loader) with the playback region
            # and loop markers drawn over it, repainted by
            # _update_sampler_faces only when the file or a marker moves.
            if module.TYPE == "sampler":
                with dpg.node_attribute(attribute_type=dpg.mvNode_Attr_Static):
                    w, h = self._SFACE_W, self._SFACE_H
                    with dpg.drawlist(width=w, height=h) as dl:
                        dpg.draw_rectangle(
                            (0, 0), (w - 1, h - 1),
                            fill=(16, 20, 16, 255), color=(70, 78, 70, 255),
                        )
                        dpg.draw_line(
                            (0, h / 2), (w - 1, h / 2), color=(50, 60, 50, 255),
                        )
                        wave = dpg.draw_polyline(
                            [(0, h / 2), (w - 1, h / 2)],
                            color=self._SFACE_DIM, thickness=1,
                        )
                        markers = {}
                        for name, colour in (
                            ("start", self._SFACE_REGION),
                            ("end", self._SFACE_REGION),
                            ("loop_start", self._SFACE_LOOP),
                            ("loop_end", self._SFACE_LOOP),
                        ):
                            markers[name] = dpg.draw_line(
                                (0, 0), (0, h - 1), color=colour, thickness=1,
                                show=False,
                            )
                        caption = dpg.draw_text(
                            (4, 2), "no sample", color=(140, 140, 140, 255),
                            size=12,
                        )
                    self._sampler_faces[module.id] = {
                        "dl": dl, "wave": wave, "markers": markers,
                        "caption": caption, "key": None,
                    }

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
                    dpg.add_text(f"{port.name} >")
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

        if (
            module.TYPE in ("file_player", "convolver", "sampler")
            and param_name == "path"
        ) or (module.TYPE == "wavetable_morph" and param_name == "file"):
            # Path field + a Browse button that opens the shared WAV
            # dialog. The field keeps an explicit tag so the dialog's
            # callback can write the chosen path back into it; typing a
            # path by hand still works via the same _on_param_changed.
            with dpg.group(horizontal=True):
                path_tag = f"fileplayer_path_{module.id}"
                dpg.add_input_text(
                    label=param_name,
                    default_value=str(current),
                    width=140,
                    tag=path_tag,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                # Focusing this field suppresses KeyTrigger's raw triggers, so
                # typing a path doesn't fire bound keys.
                self._text_input_tags.add(path_tag)
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

        if module.TYPE == "oscillator":
            # ``pulse_width`` is the duty cycle of the square shapes,
            # clamped to the 0.05..0.95 band the renderer enforces;
            # ``pw_cv_depth`` scales pw_cv in width per CV unit (0.5 lets
            # a bipolar +/-1 LFO sweep the whole band). waveform / freq /
            # amp fall through to the shared widgets below.
            if param_name == "pulse_width":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=PULSE_WIDTH_MIN, max_value=PULSE_WIDTH_MAX,
                    format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "pw_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.0, max_value=1.0, format="%.2f width/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
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

        if module.TYPE == "lfo":
            # ``phase`` is the start phase in cycles: the free-running
            # start and where a ``reset`` edge jumps to (0.25 on a sine =
            # the peak). ``seed`` is the ``random`` waveform's stream (0 =
            # the global rng, N = a private one a reset replays), the
            # same drag_int as the sequencer's. ``waveform``, ``rate``,
            # ``depth``, ``bipolar`` and ``cv_depth`` ride the shared
            # branches below.
            if param_name == "phase":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f cyc",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "clock":
            # Tempo metronome: bpm, pulses-per-beat division, duty cycle,
            # and ``bpm_cv_depth`` -- tempo doublings per CV unit on
            # ``bpm_cv`` (1.0 = the 1 V/oct style: +1 doubles the tempo),
            # presented like the filter's ``res_cv_depth`` drag (0..4).
            # ``swing`` delays every second pulse by that fraction of the
            # period -- the divider's convention; the slider stops at 0.5
            # (the hard shuffle; 0.33 = triplet) though the backend takes
            # the divider's 0.75. ``swing_cv_depth`` is swing units per
            # CV unit on the ``swing_cv`` jack, the bpm_cv_depth drag's
            # sibling. ``reset`` / ``run`` / ``bpm_cv`` / ``swing_cv``
            # are jacks, no widget.
            # NOTE the order: ``swing_cv_depth`` must be tested BEFORE
            # ``swing`` would be -- the equality below is exact, so the
            # two never collide, but keep the specific one first if
            # either ever becomes a prefix match (the mode-combo
            # shadowing lesson).
            if param_name == "swing_cv_depth":
                dpg.add_drag_float(
                    label="swing_cv_depth (swing/unit)",
                    default_value=float(current), speed=0.01,
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "swing":
                dpg.add_slider_float(
                    label="swing (0.33 = triplet)", default_value=float(current),
                    min_value=0.0, max_value=CLOCK_MAX_SWING_UI, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "bpm_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f dbl/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
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
            # steps = loop length (int); direction = the run-mode switch
            # (combo over SEQ_DIRECTIONS -- deliberately not named ``mode``,
            # which would route through the shared combo branch above);
            # seed = the random direction's stream; step{i}_pitch =
            # semitones (drag); step{i}_on = rest toggle (falls through to
            # the generic checkbox).
            if param_name == "steps":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=16,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "direction":
                dpg.add_combo(
                    label=param_name, items=list(SEQ_DIRECTIONS),
                    default_value=str(current),
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
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

        if module.TYPE == "fm_op":
            # DX-style FM operator. ``ratio`` is a combo of the harmonic
            # snap table (stored numeric via _on_fm_ratio_changed); ``fine``
            # is cents; ``index`` is the FM depth in radians; ``feedback`` is
            # self-PM; ``fixed`` + ``freq`` give a note-independent carrier.
            # ``fixed`` (bool) falls through to the generic checkbox below.
            if param_name == "ratio":
                dpg.add_combo(
                    label=param_name,
                    items=[f"{r:g}" for r in FM_RATIO_TABLE],
                    default_value=f"{fm_snap_ratio(float(current)):g}",
                    width=140,
                    callback=self._on_fm_ratio_changed,
                    user_data=user_data,
                )
                return
            if param_name == "fine":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-50.0, max_value=50.0, format="%.0f ct",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "index":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=10.0, format="%.2f rad",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "index_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=10.0, format="%.2f /unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "feedback":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freq":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=20000.0, format="%.1f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "bitcrusher":
            # ``bits`` (1..24, 24 = quantizer skipped) and ``rate_div``
            # (1..64, 1 = no decimation) are the two crush axes;
            # ``jitter`` wobbles the hold length (needs rate_div > 1);
            # ``mix`` is dry/wet. The two CV depths carry their natural
            # unit in the label so the scaler reads clearly: bits_cv_depth
            # is bits per CV unit (default 1 = 1 bit/unit; ~23 sweeps the
            # whole 1..24 range from a single unit); rate_cv_depth is
            # octaves of decimation per unit (±6 spans the full 1..64
            # range). Negative inverts.
            if param_name == "bits":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=24,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "rate_div":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=64,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "jitter":
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
            if param_name == "bits_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=-24.0, max_value=24.0, format="%.2f bit/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "rate_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=-6.0, max_value=6.0, format="%.2f oct/unit",
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
            # 2026-09-14 love pass: shimmer feedback, a second harmony
            # voice with its own level, and a stereo spread between the
            # two shifted voices on out_l / out_r.
            if param_name == "feedback":
                dpg.add_slider_float(
                    label=f"{param_name} (shimmer)", default_value=float(current),
                    min_value=0.0, max_value=0.9, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "harmony":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-24.0, max_value=24.0, format="%.2f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "harmony_level":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "spread":
                dpg.add_slider_float(
                    label=f"{param_name} (L main / R harmony)", default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
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
            if param_name == "brake":
                # Tape-stop switch: on = decelerate to a dead stop
                # (linear in speed, over brake_time), off = spin back
                # up (over spinup_time). Also gateable via the brake
                # input port.
                dpg.add_checkbox(
                    label=param_name,
                    default_value=bool(current),
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name in ("brake_time", "spinup_time"):
                dpg.add_drag_float(
                    label=param_name,
                    default_value=float(current),
                    speed=0.01,
                    min_value=0.0,
                    max_value=5.0,
                    format="%.2f s",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return

        if module.TYPE == "filter":
            # The biquad. ``mode`` is the shared combo below; ``cutoff``,
            # ``resonance`` and ``cv_depth`` (oct/unit on cutoff_cv) ride
            # the generic numeric fallbacks at the bottom. Only the
            # resonance_cv scaler needs its own unit: Q doublings per CV
            # unit, the motion_eq ``q_cv_depth`` convention, presented
            # exactly like the cutoff's ``cv_depth`` drag (0..4, default 1).
            if param_name == "res_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f dbl/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
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

        if module.TYPE == "freq_shifter":
            # Bode single-sideband shifter. ``shift`` is linear Hz (not a
            # ratio — the inharmonic point of the module); ``shift_cv_depth``
            # is Hz of shift per CV unit; ``feedback`` recirculates out_up
            # (0..0.9, the engine's stability clamp); ``mix`` is dry/wet
            # with the dry path latency-matched.
            if param_name == "shift":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=-2000.0, max_value=2000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "shift_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=0.0, max_value=2000.0, format="%.0f Hz/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "feedback":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=0.9, format="%.2f",
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

        if module.TYPE == "rotary":
            # Leslie: ``speed`` is the switch (slow / fast / stop; a
            # patched ``fast`` gate overrides it); the rates are the
            # horn's Hz per setting; ``ramp`` scales spin-up/coast-down;
            # depth = tremolo + Doppler; spread = mic angle; balance =
            # drum <-> horn; crossover Hz; mix dry/wet.
            if param_name == "speed":
                dpg.add_combo(
                    label=f"{param_name} (or gate)", items=list(ROTARY_SPEEDS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "slow_rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.1, max_value=3.0, format="%.2f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "fast_rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=2.0, max_value=12.0, format="%.2f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "ramp":
                dpg.add_slider_float(
                    label=f"{param_name} (x time)", default_value=float(current),
                    min_value=0.25, max_value=4.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("depth", "spread", "mix"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "balance":
                dpg.add_slider_float(
                    label=f"{param_name} (drum < > horn)", default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "crossover":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=5.0,
                    min_value=100.0, max_value=4000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "granular":
            # Grain cloud over a live ring buffer. ``buffer`` is the
            # history length (s); ``density`` grains per second; ``size``
            # the grain length (ms); ``pitch`` transposition (st);
            # ``position`` how far back the grains read (0 = now, 1 =
            # ``buffer`` s ago); ``window`` the grain shape; ``mix``
            # dry/wet. Slice 2: ``spray_time`` (onset jitter, 0 = sync),
            # ``spray_pos`` (read-point scatter, position units),
            # ``spray_pitch`` (cents), ``width`` (per-grain pan scatter
            # on out_l/out_r), ``seed``. Slice 3: ``freeze`` (the switch,
            # ORed with the freeze gate) and ``position_cv_depth``
            # (buffer fractions per CV unit). Every knob reaches the
            # NEXT grain, not the ones in flight.
            if param_name == "window":
                dpg.add_combo(
                    label=param_name, items=list(GRANULAR_WINDOWS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "buffer":
                dpg.add_drag_float(
                    label=f"{param_name} (history)", default_value=float(current),
                    speed=0.05, min_value=0.5, max_value=10.0, format="%.2f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "density":
                dpg.add_drag_float(
                    label=f"{param_name} (grains/s)", default_value=float(current),
                    speed=0.25, min_value=0.5, max_value=100.0, format="%.1f /s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "size":
                dpg.add_drag_float(
                    label=f"{param_name} (grain)", default_value=float(current),
                    speed=1.0, min_value=10.0, max_value=500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "pitch":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-24.0, max_value=24.0, format="%.1f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "position":
                dpg.add_slider_float(
                    label=f"{param_name} (now < > past)", default_value=float(current),
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
            if param_name == "spray_time":
                dpg.add_slider_float(
                    label=f"{param_name} (sync < > async)", default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "spray_pos":
                dpg.add_slider_float(
                    label=f"{param_name} (+/-)", default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "spray_pitch":
                dpg.add_drag_float(
                    label=f"{param_name} (+/-)", default_value=float(current),
                    speed=2.0, min_value=0.0, max_value=1200.0, format="%.0f ct",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "width":
                dpg.add_slider_float(
                    label=f"{param_name} (L/R scatter)", default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freeze":
                dpg.add_checkbox(
                    label=f"{param_name} (or gate)", default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "position_cv_depth":
                dpg.add_slider_float(
                    label=f"{param_name} (buffer/unit)", default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
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
            # (octaves per unit). Love pass: ``division`` is clock ticks per
            # sweep (only while the ``clock`` jack is patched); ``spread``
            # is the L/R LFO phase offset (0 mono, 0.5 the shipped
            # quadrature, 1 a half cycle); ``manual_depth`` scales manual_cv
            # (octaves per unit).
            if param_name == "division":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=0.25, max_value=64.0, format="%.2f ticks",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "spread":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "manual_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
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
            # ``cv_depth`` scales rate_cv (octaves per unit). Love pass:
            # ``division`` is clock ticks per sweep (only while the
            # ``clock`` jack is patched); ``spread`` is the L/R LFO phase
            # offset (0 mono, 0.5 the shipped quadrature, 1 a half cycle);
            # ``manual_depth`` scales manual_cv (octaves per unit).
            if param_name == "division":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=0.25, max_value=64.0, format="%.2f ticks",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "spread":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "manual_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
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
            "stereo_speaker_output", "specific_stereo_speaker_output",
            "buffered_specific_speaker_output", "warping_buffered_speaker_output",
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
            if param_name == "ratio_depth":
                # Buffered-family governor swing: stretch per ratio_cv unit
                # (cv +-1 at 1.0 spans the engine's full 0.5..2 ratio clamp).
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.005,
                    min_value=0.0, max_value=1.0, format="%.2f x/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("brake_time", "spinup_time"):
                # Warping sink only: tape-transport slew of the audible
                # governor's ratio — coast-down / wind-up seconds. The
                # real-GUI sweet spot was ~10 s each (a slow gentle drift),
                # so the range runs long.
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.0, max_value=30.0, format="%.2f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "compressor":
            # Feed-forward dynamics. ``threshold`` is where reduction
            # starts (dBFS); ``ratio`` is dB-in per dB-out above it;
            # ``attack``/``release`` are the gain-move times in MS (the
            # generic attack/release branch below reads seconds — wrong
            # unit and range for this module); ``knee`` softens the bend;
            # ``gain`` is make-up in dB (not the generic 0..2 linear
            # trim); ``mix`` blends dry back in for parallel compression;
            # ``threshold_cv_depth`` scales threshold_cv in dB per unit.
            # ``detector`` hits the shared peak/rms combo branch.
            if param_name == "threshold":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-60.0, max_value=0.0, format="%.1f dBFS",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "ratio":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=1.0, max_value=20.0, format="%.1f:1",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "attack":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.1, max_value=250.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "release":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=2.0,
                    min_value=5.0, max_value=2500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "knee":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=24.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gain":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=24.0, format="%.1f dB",
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
            if param_name == "threshold_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.0, max_value=24.0, format="%.1f dB/unit",
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

        if module.TYPE == "transient_shaper":
            # Attack/sustain rebalance. Both params are BIPOLAR gains,
            # -1 (cut up to -12 dB) .. +1 (boost up to +12 dB), 0 =
            # untouched — NOT the generic envelope-time / 0..1 sustain
            # widgets, which can't reach the cut half at all. ``speed``
            # hits the fast/med/slow combo branch below.
            if param_name in ("attack", "sustain"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
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

        if module.TYPE == "tape":
            # Tape character macros. wow/flutter/drift are pitch-instability
            # depths, sat is saturation drive — all 0..1; ``hiss`` is the
            # noise-floor level in dB (-80 = off .. -30 = max, per the
            # module's calibrated bed); ``bump`` is the ~60 Hz head-bump
            # shelf in dB; ``mix`` is dry/wet (0 bit-exact dry);
            # ``stop_time`` / ``start_time`` are the tape-stop's coast-down
            # and spin-up in seconds (the ``stop`` gate jack drives them).
            if param_name in ("wow", "flutter", "drift", "sat"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "hiss":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-80.0, max_value=-30.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "bump":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=6.0, format="%.1f dB",
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
            if param_name in ("stop_time", "start_time"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.05, max_value=8.0, format="%.2f s",
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
            if param_name == "freeze":
                # The panel hold, ORed with the freeze gate (the granular
                # / freeze precedent): tick to hang the echo by hand.
                dpg.add_checkbox(
                    label=f"{param_name} (or gate)", default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
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
            if param_name == "freeze":
                # The panel hold, ORed with the freeze gate (the granular
                # / freeze precedent): tick to hang the tail as a pad.
                dpg.add_checkbox(
                    label=f"{param_name} (or gate)", default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "convolver":
            # IR reverb / cab. ``predelay`` delays the wet onset behind the
            # dry (articulation gap); ``tone`` low-passes the wet only
            # (20 kHz = out of circuit); ``mix`` is dry/wet with the dry
            # latency-comped. ``gain`` (wet trim) rides the generic 0..2
            # slider; ``path`` has the Browse branch up top.
            if param_name == "predelay":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=0.0, max_value=500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "tone":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=10.0,
                    min_value=1000.0, max_value=20000.0, format="%.0f Hz",
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

        if module.TYPE == "audio_to_cv":
            # Envelope follower: one-pole attack/release time constants in
            # MILLISECONDS (the generic attack/release branch reads
            # seconds). ``gain`` rides the generic 0..2 slider.
            if param_name == "attack_ms":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.5,
                    min_value=0.1, max_value=500.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "release_ms":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=2.0,
                    min_value=1.0, max_value=2000.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "cv_to_frequency":
            # The three-point CV→Hz mapping anchors (f0 at CV=0, fm at
            # CV=0.5, f1 at CV=1) and their negative-side twins. Same
            # 20..20k Hz drag as the EQ band freqs. ``freq``/``waveform``/
            # ``mode``/``mode_neg`` hit their shared branches elsewhere.
            if param_name in ("f0", "fm", "f1", "f0_neg", "fm_neg", "f1_neg"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=20000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "quantizer":
            # ``root``/``scale`` are combos; ``hysteresis`` is cents of
            # note-boundary stickiness; ``transpose`` shifts the OUTPUT in
            # semitones (post-quantize). The twelve custom-scale tickboxes
            # render as two compact horizontal rows when the FIRST of them
            # comes up; the rest then draw nothing (already on screen).
            if param_name == "root":
                dpg.add_combo(
                    label=param_name, items=list(QUANTIZER_ROOTS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "scale":
                dpg.add_combo(
                    label=param_name, items=list(QUANTIZER_SCALES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "hysteresis":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=50.0, format="%.0f ct",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "transpose":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=-24.0, max_value=24.0, format="%.0f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == QUANTIZER_CUSTOM_KEYS[0]:
                # Draw the whole 12-tickbox bank (used when scale=custom)
                # as two rows of six, labelled by pitch class.
                note_names = (
                    "C", "C#", "D", "D#", "E", "F",
                    "F#", "G", "G#", "A", "A#", "B",
                )
                for row in (range(0, 6), range(6, 12)):
                    with dpg.group(horizontal=True):
                        for i in row:
                            key = QUANTIZER_CUSTOM_KEYS[i]
                            dpg.add_checkbox(
                                label=note_names[i],
                                default_value=bool(module.params.get(key, True)),
                                callback=self._on_param_changed,
                                user_data=(module.id, key),
                            )
                return
            if param_name in QUANTIZER_CUSTOM_KEYS:
                return  # drawn with the bank above

        if module.TYPE == "shift_random":
            # Turing-machine-style looping random. ``probability`` is the
            # money knob (0 locked loop .. 1 coin flips); ``length`` is the
            # loop in clocks; ``range`` scales the CV (bipolar checkbox
            # centres it); ``seed`` re-rolls the register live.
            if param_name == "probability":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "length":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=2, max_value=16,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "range":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.0, max_value=5.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "organ":
            # bar1..bar9 are drawn by the drawbar bank panel; these are
            # the rest. ``perc``/``perc_decay`` are combos; the levels
            # are plain 0..1 sliders.
            if param_name == "perc":
                dpg.add_combo(
                    label=param_name, items=list(PERC_MODES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "perc_decay":
                dpg.add_combo(
                    label=param_name, items=list(PERC_DECAYS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            # ``vibrato`` is the scanner's six-way knob (off / v1..v3 /
            # c1..c3) -- its own combo, not named ``mode`` on purpose (the
            # shared mode branch would hand it the filter's list).
            if param_name == "vibrato":
                dpg.add_combo(
                    label=param_name, items=list(ORGAN_VIBRATO),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("click", "perc_level", "level"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "chaos":
            # ``system`` picks the attractor; ``rate`` ≈ orbits/second;
            # ``range``/``seed`` follow the shift_random idiom.
            if param_name == "system":
                dpg.add_combo(
                    label=param_name, items=list(CHAOS_SYSTEMS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=0.01, max_value=50.0, format="%.2f orbit/s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "range":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.0, max_value=5.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "supersaw":
            # detune/blend/spread are the three character knobs, all
            # 0..1; ``freq`` is the usual Hz drag; ``detune_cv_depth``
            # is detune per CV unit (bipolar so a rising CV can tighten
            # the stack as well as open it).
            if param_name in ("detune", "blend", "spread", "amp"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freq":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=4000.0, format="%.1f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "detune_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=-2.0, max_value=2.0, format="%.2f det/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "wavetable_morph":
            # ``table`` picks the built-in stack (``file`` overrides it
            # via Browse above); ``position`` is THE knob.
            if param_name == "table":
                dpg.add_combo(
                    label=param_name, items=list(WT_STACKS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("position", "amp"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "position_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.0, max_value=4.0, format="%.2f pos/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freq":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=20.0, max_value=4000.0, format="%.1f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "vinyl":
            # One knob per vice, all 0..1; ``seed`` picks the pressing.
            if param_name in ("crackle", "rumble", "wobble"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "euclidean":
            # Euclidean rhythm: fills hits over steps ticks; rotate walks
            # the pattern; accent_fills is the sparser accent layer;
            # gate_len is the hit length as a fraction of one step.
            if param_name == "steps":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=32,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("fills", "accent_fills"):
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=0, max_value=32,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "rotate":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=0, max_value=31,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gate_len":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.05, max_value=1.0, format="%.2f step",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "fills_cv_depth":
                dpg.add_slider_float(
                    label="fills_cv_depth (fills/unit)",
                    default_value=float(current),
                    min_value=0.0, max_value=32.0, format="%.1f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "clock_divider":
            # Divider: n is the custom division, m the multiplier, swing
            # delays every second divn gate by that fraction of its
            # period, pw is the gate width as a fraction of each output's
            # period.
            if param_name == "n":
                dpg.add_slider_int(
                    label="n (divn)", default_value=int(current),
                    min_value=1, max_value=DIVIDER_MAX_N,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "m":
                dpg.add_slider_int(
                    label="m (mult)", default_value=int(current),
                    min_value=2, max_value=DIVIDER_MAX_M,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "swing":
                dpg.add_slider_float(
                    label="swing (divn, 0.33 = triplet)",
                    default_value=float(current),
                    min_value=0.0, max_value=DIVIDER_MAX_SWING, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "pw":
                dpg.add_slider_float(
                    label="pw (of period)", default_value=float(current),
                    min_value=0.05, max_value=0.95, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "burst":
            # Ratchet generator: count gates per trigger; rate spans the
            # free-running burst (ignored when clocked); division picks
            # every Nth clock edge; decay tapers env; spread warps the
            # grid (accel ↔ ritard).
            if param_name == "count":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=16,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=0.5, max_value=50.0, format="%.1f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "division":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=8,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "decay":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "spread":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "count_cv_depth":
                dpg.add_slider_float(
                    label="count_cv_depth (gates/unit)",
                    default_value=float(current),
                    min_value=0.0, max_value=16.0, format="%.1f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "bernoulli_gate":
            # Probability router: probability = chance of out_a (plus
            # p_cv at the edge); mode hits the shared combo (independent
            # coin vs toggle-on-heads); seed re-rolls the coin stream.
            if param_name == "probability":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "arpeggiator":
            # Poly→mono note collapser: ``mode`` hits the shared combo
            # (up/down/updown/order/random); ``octaves`` stacks passes;
            # ``gate_len`` is the note length as a fraction of the clock
            # period; ``hold`` (generic checkbox) latches; ``seed``
            # drives random mode.
            if param_name == "octaves":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=1, max_value=4,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gate_len":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.05, max_value=0.95, format="%.2f step",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            # Internal clock (used only with ``clock`` unpatched): the
            # same bpm / pulses-per-beat pair as the clock module.
            if param_name == "bpm":
                dpg.add_slider_float(
                    label=f"{param_name} (no clock)", default_value=float(current),
                    min_value=20.0, max_value=300.0, format="%.1f BPM",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "division":
                dpg.add_drag_float(
                    label=f"{param_name} (no clock)", default_value=float(current), speed=0.25,
                    min_value=0.25, max_value=16.0, format="%.2f /beat",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "chord":
            # Mono→poly chord: ``preset`` picks the interval table
            # (``custom`` reads the four slot rows below); ``strum``
            # staggers row onsets; ``spread`` (generic checkbox) opens
            # the voicing ±1 octave.
            if param_name == "preset":
                dpg.add_combo(
                    label=param_name, items=list(CHORD_PRESETS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == CHORD_INTERVAL_KEYS[0]:
                # Draw the whole 4-slot bank (tickbox + semitone drag per
                # row, read when preset=custom) in one go.
                for i, (ikey, ekey) in enumerate(
                    zip(CHORD_INTERVAL_KEYS, CHORD_ENABLE_KEYS)
                ):
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(
                            default_value=bool(module.params.get(ekey, True)),
                            callback=self._on_param_changed,
                            user_data=(module.id, ekey),
                        )
                        dpg.add_drag_float(
                            label=ikey, default_value=float(
                                module.params.get(ikey, 0.0)
                            ),
                            speed=1.0, min_value=-24.0, max_value=24.0,
                            format="%.0f st", width=110,
                            callback=self._on_param_changed,
                            user_data=(module.id, ikey),
                        )
                return
            if param_name in CHORD_INTERVAL_KEYS or param_name in CHORD_ENABLE_KEYS:
                return  # drawn with the bank above
            if param_name == "strum":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=200.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "inversion":
                # 0 = root position; n = the n lowest notes up an octave.
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=0, max_value=3,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            # ``retrig`` (generic checkbox): re-strum on ``changed``.

        if module.TYPE == "mid_side":
            # Stereo width: 0 mono, 1 unity (decode ≡ input), 2 extra
            # wide. width_cv adds per sample.
            if param_name == "width":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=2.0, format="%.2f x",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "side_hp":
                # Bass mono: highpass corner on the side only. 0 = off
                # (the backend holds anything above 0 to 20..500 Hz).
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=0.0, max_value=500.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "octaver":
            # Sub-octave mixer: dry/sub1/sub2 levels + the subs' LP tone.
            if param_name in ("dry", "sub1", "sub2"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "tone":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=5.0,
                    min_value=200.0, max_value=2000.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE in ("kick_drum", "snare_drum", "hat_drum"):
            # Percussion voices. All ms params carry their unit; ``tune``
            # is a shared ±12 st shift; click/drive/snappy/level are 0..1.
            if param_name == "freq_start":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=100.0, max_value=400.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "freq_end":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.5,
                    min_value=30.0, max_value=80.0, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "bend":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.5,
                    min_value=5.0, max_value=200.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "decay":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=2.0,
                    min_value=50.0, max_value=1500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("tone_decay", "decay_closed"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=10.0, max_value=500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("noise_decay", "decay_open"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=2.0,
                    min_value=20.0, max_value=1500.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "tone":
                # Hat stack base frequency. Bounded to the module's own
                # range -- the other `tone` branches above belong to other
                # TYPE blocks and never reach a drum.
                dpg.add_drag_float(
                    label="tone (stack base)", default_value=float(current),
                    speed=2.0, min_value=HAT_TONE_MIN, max_value=HAT_TONE_MAX,
                    format="%.0f Hz", width=140,
                    callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "tune":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=-12.0, max_value=12.0, format="%.1f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("click", "drive", "snappy", "level"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "modal":
            # Resonator bank. ``material`` picks the physics table;
            # ``modes`` is the resonator count; ``decay`` is the lowest
            # mode's t60; ``decay_tilt`` kills highs faster; ``brightness``
            # tilts mode gains; ``inharm`` stretches the ratio table.
            if param_name == "material":
                dpg.add_combo(
                    label=param_name, items=list(MODAL_MATERIALS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "modes":
                dpg.add_slider_int(
                    label=param_name, default_value=int(current),
                    min_value=4, max_value=24,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "decay":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=0.1, max_value=30.0, format="%.2f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("decay_tilt", "brightness", "inharm", "level"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            # The love-pass trio, all 0..1 with 0 = off: ``position`` is
            # the strike-position comb, ``mallet`` the pitch-tracking
            # strike low-pass (0 hard .. 1 soft), ``spread`` the stereo
            # mode spread on out_l/out_r.
            if param_name in ("position", "mallet", "spread"):
                labels = {
                    "position": "position (strike, 0 = off)",
                    "mallet": "mallet (0 hard .. 1 soft)",
                    "spread": "spread (stereo, out_l/r)",
                }
                dpg.add_slider_float(
                    label=labels[param_name], default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "freeze":
            # Spectral freeze. ``size`` is the FFT window (the resolution
            # knob: bigger holds close harmony, smaller grabs faster);
            # ``freeze`` the tickbox ORed with the gate; ``smear`` 0 =
            # coherent hold, 1 = random-phase wash; ``pitch`` transposes
            # the frozen layer (st) with ``pitch_cv_depth`` oct/unit on
            # pitch_cv; ``level`` the frozen layer, ``dry`` the live
            # input (not a mix: engaging never ducks what you play over
            # it); ``fade`` the rise / fall / re-freeze crossfade (ms);
            # ``seed`` the smear's die. Love pass: ``width`` the
            # quadrature stereo scatter on out_l/out_r (0 = the pair is
            # the mono); ``decay`` the hold's own fade to -60 dB in
            # seconds (0 = forever) -- a drag, not a slider, because
            # the useful range is 0.5 s to a minute. Love pass 2:
            # ``latch`` makes the freeze gate TOGGLE the hold instead of
            # holding it while high (a footswitch becomes a switch) and
            # ``width_cv_depth`` is width units per unit on width_cv.
            if param_name == "size":
                dpg.add_combo(
                    label=f"{param_name} (fft)", items=[str(n) for n in FREEZE_SIZES],
                    default_value=str(int(current)),
                    width=140, callback=self._on_freeze_size_changed, user_data=user_data,
                )
                return
            if param_name == "freeze":
                dpg.add_checkbox(
                    label=f"{param_name} (or gate)", default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "latch":
                dpg.add_checkbox(
                    label=f"{param_name} (gate toggles)", default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("smear", "level", "dry"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "pitch":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=-24.0, max_value=24.0, format="%.1f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "pitch_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "fade":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=1.0,
                    min_value=1.0, max_value=2000.0, format="%.0f ms",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "width":
                dpg.add_slider_float(
                    label=f"{param_name} (stereo, out_l/r)", default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "width_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f width/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "decay":
                dpg.add_drag_float(
                    label=f"{param_name} (0 = forever)", default_value=float(current),
                    speed=0.1, min_value=0.0, max_value=60.0, format="%.1f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "vowel":
            # Formant filter. ``vowel`` slides A > E > I > O > U (0..4);
            # ``voice`` picks the formant table; ``resonance`` multiplies
            # every formant's Q; ``gain`` is makeup in dB; ``cv_depth``
            # is vowels per unit on vowel_cv; ``cv_rate`` is how often
            # that jack is read (block mean or per sample -- it is NOT
            # called ``mode``, which the shared combo branch would have
            # shadowed); ``formant`` is the throat size in semitones
            # (down = giant, up = child) and ``formant_cv_depth``
            # octaves per unit on formant_cv; ``f1``..``f5`` are the
            # custom voice's formant frequencies. ``mix`` rides the
            # generic slider.
            if param_name == "vowel":
                dpg.add_slider_float(
                    label=f"{param_name} (A E I O U)", default_value=float(current),
                    min_value=0.0, max_value=4.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "voice":
                dpg.add_combo(
                    label=param_name, items=list(VOWEL_VOICE_CHOICES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_rate":
                dpg.add_combo(
                    label=f"{param_name} (vowel_cv)", items=list(VOWEL_CV_RATES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("f1", "f2", "f3", "f4", "f5"):
                dpg.add_drag_float(
                    label=f"{param_name} (custom)", default_value=float(current),
                    speed=5.0, min_value=VOWEL_CUSTOM_FREQ_MIN,
                    max_value=VOWEL_CUSTOM_FREQ_MAX, format="%.0f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "resonance":
                dpg.add_drag_float(
                    label=f"{param_name} (x table Q)", default_value=float(current),
                    speed=0.01, min_value=0.25, max_value=4.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gain":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.1,
                    min_value=-12.0, max_value=24.0, format="%.1f dB",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f vow/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "formant":
                dpg.add_drag_float(
                    label=f"{param_name} (giant < 0 < child)", default_value=float(current),
                    speed=0.1, min_value=-24.0, max_value=24.0, format="%.1f st",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "formant_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f oct/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "cv_recorder":
            # The modulation looper. ``length`` is seconds, or clock ticks
            # while a clock is cabled; ``feedback`` scales the old layer on
            # overdub; ``value`` is the gesture knob (the input when ``in``
            # is unpatched). ``mode`` hits the shared combo branch below.
            # The transport: ``reverse`` (ORed with its gate) and ``speed``
            # (the playback head's rate; recording always runs at 1x) --
            # ``speed`` must sit HERE, above the transient shaper's shared
            # ``speed`` combo further down, or it inherits that list.
            # ``play_mode`` is deliberately NOT called ``mode`` for the
            # same reason (``mode`` is caught by the shared combo branch).
            if param_name == "play_mode":
                dpg.add_combo(
                    label=f"{param_name} (the play jack)",
                    items=list(CV_RECORDER_PLAY_MODES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "speed_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=-4.0, max_value=4.0, format="%.2f x2/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "reverse":
                dpg.add_checkbox(
                    label=f"{param_name} (or gate)", default_value=bool(current),
                    callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "speed":
                dpg.add_combo(
                    label=f"{param_name} (play; rec is 1x)", items=list(CV_RECORDER_SPEEDS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "length":
                dpg.add_drag_float(
                    label=f"{param_name} (s | x clock)", default_value=float(current),
                    speed=0.05, min_value=0.05, max_value=60.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "feedback":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "value":
                dpg.add_slider_float(
                    label=f"{param_name} (the gesture knob)", default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "sample_hold":
            # The S&H's love-pass knobs. ``mode`` hits the shared combo
            # branch below (sample / track); ``prob`` is the chance an
            # edge samples; ``seed`` its die; ``glide`` the output lag in
            # seconds-to-99% (0 = a straight wire); ``prob_cv_depth`` is
            # probability units per ``prob_cv`` unit (1 = a 0..1 CV sweeps
            # the whole chance; negative inverts), the supersaw's
            # ``detune_cv_depth`` drag.
            if param_name == "prob_cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=-2.0, max_value=2.0, format="%.2f p/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "prob":
                dpg.add_slider_float(
                    label=f"{param_name} (chance an edge samples)",
                    default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "glide":
                dpg.add_drag_float(
                    label=f"{param_name} (lag, 0 = off)", default_value=float(current),
                    speed=0.005, min_value=0.0, max_value=5.0, format="%.3f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "drift":
            # Smooth wandering random. ``rate`` is new values per second;
            # ``glide`` the fraction of each interval spent travelling (0 =
            # stepped); ``step`` the walk's gaussian step; ``mode`` hits the
            # shared combo branch below; ``bipolar`` rides the checkbox.
            if param_name == "rate":
                dpg.add_drag_float(
                    label=f"{param_name} (values/s)", default_value=float(current),
                    speed=0.01, min_value=0.02, max_value=50.0, format="%.2f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("glide", "step", "depth"):
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
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "wind":
            # Blown pipe. ``model`` picks the mouth (flute jet / clarinet
            # reed); ``breath`` is the player's pressure, ``noise`` the
            # breath noise; ``attack`` / ``release`` are the breath ramps
            # in seconds; ``damping`` darkens the bore; ``cv_depth`` scales
            # breath_cv in level per unit; ``seed`` keys the noise.
            if param_name == "model":
                dpg.add_combo(
                    label=param_name, items=list(WIND_MODELS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("breath", "noise", "damping", "level"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("attack", "release"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.005,
                    min_value=0.001, max_value=10.0, format="%.3f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f lvl/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "bowed":
            # Bowed string. ``pressure`` / ``velocity`` are the two hands
            # (force, speed); ``position`` is where the bow sits along the
            # string (near the bridge = bright); ``attack`` / ``release``
            # are the bow landing / lifting ramps in seconds; ``damping``
            # darkens; ``body`` mixes the resonance bank; ``cv_depth``
            # scales pressure_cv / velocity_cv in level per unit.
            if param_name in ("pressure", "velocity", "damping", "body", "level"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "position":
                dpg.add_slider_float(
                    label=f"{param_name} (of string)", default_value=float(current),
                    min_value=0.05, max_value=0.5, format="%.3f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("attack", "release"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.005,
                    min_value=0.001, max_value=10.0, format="%.3f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "cv_depth":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.0, max_value=4.0, format="%.2f lvl/unit",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "pluck":
            # Karplus–Strong string. ``decay`` is a real t60 in seconds
            # (pitch-independent); ``damping`` darkens the loop; ``color``
            # brightens the exciter (thumb → plectrum); ``vel_color`` is
            # how far a soft hit darkens it (inert without a ``vel``
            # source); ``position`` is the pick-position comb and
            # ``vel_position`` is how far a soft hit slides that pick
            # towards the middle of the string (both 0..1, both inert
            # without a ``vel`` source); ``carry`` is a tickbox
            # (default on -- untick for the old clear-on-hit) and
            # falls through to the shared bool branch; ``level`` trims
            # the output.
            if param_name == "decay":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=0.1, max_value=30.0, format="%.2f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in (
                "damping", "color", "vel_color", "position",
                "vel_position", "level",
            ):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "scope":
            # Scope face controls. ``time_div`` is ms per division (×10
            # divisions across the face); ``gain`` scales vertically
            # (±1 fills at 1.0); ``trigger``/``level`` align the sweep;
            # ``mode`` (mono/dual/xy) hits the shared mode-combo branch
            # below; ``freeze`` rides the generic checkbox.
            if param_name == "time_div":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.5,
                    min_value=1.0, max_value=500.0, format="%.0f ms/div",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "gain":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.02,
                    min_value=0.1, max_value=10.0, format="%.2f x",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "trigger":
                dpg.add_combo(
                    label=param_name, items=list(SCOPE_TRIGGER_MODES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "level":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "autopan":
            # Panner + LFO. ``pan`` is the manual centre; ``depth`` the
            # LFO swing in pan units; ``rate`` Hz (or ``division`` clock
            # ticks per L-R-L cycle while ``clock`` is patched); ``shape``
            # sine / triangle / square (a 10 ms glide, never a click);
            # ``tremolo`` the phase between the sides (0 autopan, 1 a mono
            # tremolo); ``law`` the mono placement law (-3 / -4.5 / -6 dB
            # centre); ``cv_depth`` octaves per unit on rate_cv.
            if param_name == "pan":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f L..R",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("depth", "tremolo"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "rate":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.01,
                    min_value=0.01, max_value=20.0, format="%.2f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "shape":
                dpg.add_combo(
                    label=param_name, items=list(AUTOPAN_SHAPES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "law":
                dpg.add_combo(
                    label=f"{param_name} (-3/-4.5/-6 dB)", items=list(AUTOPAN_LAWS),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "division":
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.05,
                    min_value=0.25, max_value=64.0, format="%.2f ticks",
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

        if module.TYPE == "slew":
            # CV slew limiter: ``shape`` picks the glide curve (linear
            # constant-rate reach vs exponential one-pole ease); the times
            # are seconds — per 1.0 unit of change in linear, ~time-to-99%
            # in exponential. 0 = instant on that side.
            if param_name == "shape":
                dpg.add_combo(
                    label=param_name, items=list(SLEW_SHAPES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("rise_time", "fall_time"):
                # Seconds -- or multiples of the clock period while a
                # clock is cabled (v2 sync); rise_cv / fall_cv scale them.
                dpg.add_drag_float(
                    label=f"{param_name} (s | x clock)", default_value=float(current),
                    speed=0.005, min_value=0.0, max_value=10.0, format="%.3f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "function_generator":
            # The Maths-style function: ``mode`` decides what the trigger
            # means (fire-and-forget / hold-while-high / free-running),
            # ``rise`` and ``fall`` are the two slopes in seconds, and
            # ``curve`` bends both at once -- negative logarithmic,
            # 0 straight, positive exponential.
            if param_name == "mode":
                dpg.add_combo(
                    label=param_name, items=list(FUNCTION_GENERATOR_MODES),
                    default_value=str(current),
                    width=120, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("rise", "fall"):
                dpg.add_drag_float(
                    label=param_name, default_value=float(current), speed=0.005,
                    min_value=0.0, max_value=10.0, format="%.3f s",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "curve":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f log/exp",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name in ("curve_rise", "curve_fall"):
                # One knob per slope: an offset added to ``curve`` for
                # that slope only, the sum clamped to +/-1.
                dpg.add_slider_float(
                    label=f"{param_name} (+ curve)", default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if param_name == "speed":
            # Transient shaper follower-pair responsiveness: fast (tight
            # percussion) / med (general) / slow (bass, sustained).
            dpg.add_combo(
                label=param_name, items=list(TRANSIENT_SHAPER_SPEEDS),
                default_value=str(current),
                width=120, callback=self._on_param_changed, user_data=user_data,
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
            # with a ``mode_neg``).
            #
            # ADD YOUR MODULE HERE. This branch is a catch-all that runs
            # BEFORE the per-TYPE blocks below, so a ``mode`` handled down
            # there is shadowed and never reached, and the ``else`` hands
            # out the *filter's* items to whoever asked. That has now
            # happened twice: cv_to_frequency listed LP/HP/BP until
            # 2026-06-07, and `sampler` did from the day it shipped
            # (2026-08-22) until 2026-08-30 -- which left `gated` and
            # `loop` unreachable from the panel, since the renderer
            # rejects a bogus mode and falls back to `one_shot`.
            # `test_mode_combos.py` walks the registry and fails if any
            # module's dropdown stops offering that module's own modes.
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
            elif module.TYPE == "key_trigger":
                items = list(KEY_TRIGGER_MODES)
            elif module.TYPE == "scope":
                items = list(SCOPE_MODES)
            elif module.TYPE == "bernoulli_gate":
                items = list(BERNOULLI_MODES)
            elif module.TYPE == "arpeggiator":
                items = list(ARP_MODES)
            elif module.TYPE == "sampler":
                items = list(SAMPLER_MODES)
            elif module.TYPE == "drift":
                items = list(DRIFT_MODES)
            elif module.TYPE == "cv_recorder":
                items = list(CV_RECORDER_MODES)
            elif module.TYPE == "sample_hold":
                items = list(SAMPLE_HOLD_MODES)
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

        if module.TYPE == "noise":
            # Noise. ``color`` is the spectral tilt (white / pink / brown
            # / violet -- NOISE_COLORS is the module's own list, so only
            # the noise gets these items; pluck's ``color`` is a 0..1
            # slider in its own branch above); ``corner`` is brown's leak
            # in Hz (2..40 -- low = deep wander, high = tight; the other
            # colours ignore it, hence the label); ``seed`` keys the die
            # (0 = free-running, N = the same stream every run, the house
            # drag_int); ``amp`` falls through to the shared 0..1 slider.
            if param_name == "color":
                dpg.add_combo(
                    label=param_name,
                    items=list(NOISE_COLORS),
                    default_value=str(current),
                    width=120,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "corner":
                dpg.add_drag_float(
                    label=f"{param_name} (brown only)",
                    default_value=float(current), speed=0.2,
                    min_value=NOISE_CORNER_MIN, max_value=NOISE_CORNER_MAX,
                    format="%.1f Hz",
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return
            if param_name == "seed":
                dpg.add_drag_int(
                    label=param_name, default_value=int(current), speed=1,
                    min_value=0, max_value=999999,
                    width=140, callback=self._on_param_changed, user_data=user_data,
                )
                return

        if module.TYPE == "sampler":
            # `root` is the note the recording IS. Shown as a note name and
            # stored as a MIDI number (the fm_op ratio-combo precedent), so
            # the panel reads musically while the patch stays numeric.
            if param_name == "root":
                items = [
                    midi_to_name(n)
                    for n in range(SAMPLER_ROOT_MIN, SAMPLER_ROOT_MAX + 1)
                ]
                try:
                    current_name = midi_to_name(int(round(float(current))))
                except (TypeError, ValueError):
                    current_name = midi_to_name(60)
                if current_name not in items:
                    current_name = items[0]
                dpg.add_combo(
                    label="root (the note the sample is)",
                    items=items,
                    default_value=current_name,
                    width=90,
                    callback=self._on_sampler_root_changed,
                    user_data=user_data,
                )
                return
            # (`mode` is handled by the shared combo branch above, which
            # runs first -- a second one here would be dead code.)
            if param_name == "tune":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-12.0, max_value=12.0, format="%.2f st",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "fine":
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=-50.0, max_value=50.0, format="%.1f ct",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name in ("start", "end"):
                dpg.add_slider_float(
                    label=param_name, default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.3f",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name in ("loop_start", "loop_end"):
                # Fractions of the REGION, not of the file, so that moving
                # start/end carries the loop along instead of stranding it.
                # The label says so -- that difference is the whole point.
                dpg.add_slider_float(
                    label=f"{param_name} (in region)",
                    default_value=float(current),
                    min_value=0.0, max_value=1.0, format="%.3f",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "loop_xfade":
                # Milliseconds of sample crossfaded across the loop seam;
                # 0 is a hard seam. Bounded like the declick ramps above,
                # not the generic seconds branch.
                dpg.add_slider_float(
                    label="loop_xfade (seam)", default_value=float(current),
                    min_value=0.0, max_value=100.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "attack":
                # Milliseconds, and a declick ramp rather than an envelope —
                # the generic attack branch below reads seconds.
                dpg.add_slider_float(
                    label="attack (declick)", default_value=float(current),
                    min_value=0.0, max_value=500.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "release":
                dpg.add_slider_float(
                    label="release (declick)", default_value=float(current),
                    min_value=1.0, max_value=2000.0, format="%.1f ms",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            if param_name == "start_cv_depth":
                # How far `start_cv` moves the slice point, as a fraction of
                # the file per CV unit: 1.0 means 0..1 V sweeps the whole
                # file. Bipolar so a CV can pull the start back as well.
                dpg.add_slider_float(
                    label="start_cv_depth (file/unit)",
                    default_value=float(current),
                    min_value=-1.0, max_value=1.0, format="%.2f",
                    width=140, callback=self._on_param_changed,
                    user_data=user_data,
                )
                return
            # (`reverse` / `antialias` are plain bools and take the generic
            # checkbox below.)

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
            "midi_input", "mic_input", "specific_stereo_speaker_output",
            "buffered_specific_speaker_output", "warping_buffered_speaker_output",
        ):
            # Device list snapshot at widget creation, plus a Refresh
            # button that re-enumerates in place after hot-plugging (no
            # more delete+re-add / reopen-the-patch dance). MIDIInput
            # lists MIDI ports; MicInput lists audio capture devices; the
            # specific / buffered speaker sinks list audio playback devices.
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

        if param_name == "bend_range":
            # MIDIInput pitch-wheel span: full deflection = ±this many
            # semitones on pitch_cv (1 V/oct). 2 is the hardware default,
            # 12/24 the synth-lead octave conventions.
            dpg.add_drag_float(
                label=param_name, default_value=float(current), speed=0.1,
                min_value=0.0, max_value=24.0, format="%.1f st",
                width=140, callback=self._on_param_changed, user_data=user_data,
            )
            return

        if param_name in ("mod_scale", "pressure_scale"):
            # MIDIInput CV trims: multiplier on the normalized mod-wheel /
            # channel-pressure value emitted on mod_cv / pressure_cv.
            dpg.add_drag_float(
                label=param_name, default_value=float(current), speed=0.01,
                min_value=0.0, max_value=4.0, format="%.2f x",
                width=140, callback=self._on_param_changed, user_data=user_data,
            )
            return

        if (
            param_name == "buffer_size"
            and module.TYPE in (
                "buffered_specific_speaker_output",
                "warping_buffered_speaker_output",
            )
        ):
            # The buffered sink's own output-stream block size: a dropdown of
            # the sink sizes — the global slider's stops plus the roomy
            # 2048/4096/8192 extensions a glitchy secondary device may need.
            # Stored as an int via _on_buffer_size_changed so saved patches
            # keep a clean numeric value; the current value is snapped onto the
            # list for display in case a hand-edited patch is off-list.
            dpg.add_combo(
                label=param_name,
                items=[str(s) for s in SINK_BUFFER_SIZES],
                default_value=str(coerce_sink_buffer_size(current)),
                width=140,
                callback=self._on_buffer_size_changed,
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
        text_tag = f"paramtext_{module.id}_{param_name}"
        dpg.add_input_text(
            label=param_name,
            default_value=str(current),
            width=140,
            tag=text_tag,
            callback=self._on_param_changed,
            user_data=user_data,
        )
        # Focusing a free-text param suppresses KeyTrigger's raw triggers.
        self._text_input_tags.add(text_tag)

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
            self._set_status("Cables must go output -> input.")
            return
        try:
            cable = self.patch.connect(src_mod, src_port, dst_mod, dst_port)
        except (ValueError, KeyError) as exc:
            self._set_status(f"Cannot connect: {exc}")
            return
        link_id = dpg.add_node_link(out_attr, in_attr, parent=EDITOR_TAG)
        self._link_to_cable[link_id] = cable
        self._recompile_if_running()
        self._refresh_late_links()
        if self._cable_key(cable) in self._late_keys:
            # The moment a loop closes is the moment the block of
            # latency matters; the amber cable shows WHERE, this says so.
            self._set_status(
                f"Loop closed: {self._cable_label(cable)} reads one block "
                "late (feedback)"
            )

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
        self._refresh_late_links()

    def _set_module_param(self, module_id: int, name: str, value) -> bool:
        """Write a param to the *model* first, then tell the backend.

        **Every** param write in the UI goes through here. The model is the
        source of truth (see docs/architecture.md) and the backend call is
        the live-update notification on top of it — not the other way
        round. Writing backend-first was a real bug: ``NumpyBackend.
        set_param`` returns early while it holds no patch, and it only gets
        one when **Start audio** compiles it, so on a freshly opened patch
        every edit made before pressing Start was silently discarded and a
        save right then wrote the old values. Once a patch *is* compiled
        the two orders are identical (the backend's set_param does this
        same assignment), so this costs nothing in the running case.

        Returns True if the model took the value. A backend that refuses it
        is reported but does not make this False — the patch still holds
        the edit, which is what a caller mirroring the value into a widget
        wants to know.
        """
        module = self.patch.modules.get(module_id)
        if module is None:
            return False
        try:
            module.set_param(name, value)
        except (KeyError, ValueError) as exc:
            self._set_status(f"Param error: {exc}")
            return False
        try:
            self.backend.set_param(module_id, name, value)
        except Exception as exc:
            self._set_status(f"Param error: {exc}")
        return True

    def _on_param_changed(self, sender, app_data, user_data) -> None:
        module_id, param_name = user_data
        self._set_module_param(module_id, param_name, app_data)
        if param_name == "device":
            # A routed sink's device change swaps its stream identity, so
            # its ring-readout baseline is from the OLD stream; drop it
            # rather than let the next frame compare against another
            # stream's historical counters (a phantom amber flash when
            # re-pointing onto an already-open shared stream). No-op for
            # the other device-bearing modules (nothing keyed for them).
            self._sink_buffer_last.pop(module_id, None)
            self._sink_buffer_flash.pop(module_id, None)

    def _on_buffer_size_changed(self, sender, app_data, user_data) -> None:
        """A buffered sink's ``buffer_size`` combo changed. The combo carries
        the size as a string; store it as an int (snapped onto the sink's
        allowed set, 64..8192) so patches stay numeric and the backend keys
        its secondary stream by a clean value. Changing it while running
        rebuilds just that sink's stream (see NumpyBackend.set_param);
        otherwise it applies at the next Start."""
        module_id, param_name = user_data
        self._set_module_param(
            module_id, param_name, coerce_sink_buffer_size(app_data)
        )
        # Stream identity changed — reset the ring-readout baseline, same
        # reasoning as the device branch in _on_param_changed.
        self._sink_buffer_last.pop(module_id, None)
        self._sink_buffer_flash.pop(module_id, None)

    def _on_freeze_size_changed(self, sender, app_data, user_data) -> None:
        """The freeze's ``size`` combo changed. The combo carries the FFT
        window as a string; store it as an int (snapped onto FREEZE_SIZES)
        so patches stay numeric and the renderer keys its history on a
        clean value."""
        module_id, param_name = user_data
        try:
            n = int(app_data)
        except (TypeError, ValueError):
            n = 4096
        if n not in FREEZE_SIZES:
            n = min(FREEZE_SIZES, key=lambda k: abs(k - n))
        self._set_module_param(module_id, param_name, n)

    def _on_fm_ratio_changed(self, sender, app_data, user_data) -> None:
        """An fm_op ``ratio`` combo changed. The combo carries the harmonic
        as a string; store it as a float (snapped onto the table) so patches
        stay numeric and the renderer's own snap is a no-op on the value."""
        module_id, param_name = user_data
        self._set_module_param(module_id, param_name, fm_snap_ratio(app_data))

    def _on_sampler_root_changed(self, sender, app_data, user_data) -> None:
        """The sampler's `root` combo changed. The combo carries a note name;
        store the MIDI number so patches stay numeric and the renderer's
        rate maths needs no parsing. An unparseable name leaves the param
        alone rather than silently retuning the instrument."""
        module_id, param_name = user_data
        note = name_to_midi(str(app_data))
        if note is None:
            self._set_status(f"Unknown note name: {app_data}")
            return
        self._set_module_param(module_id, param_name, float(note))

    def _on_file_transport(self, sender, app_data, user_data) -> None:
        """A FilePlayer transport button:
        (module_id, 'play'|'stop'|'rewind'|'next').

        Play/Stop drive the ``playing`` param (pause keeps the playhead;
        the renderer holds position while False) and mirror the value into
        the node's checkbox. Rewind asks the backend to seek to 0:00 at
        the next block boundary — a state poke, not a param, so it works
        identically while playing or paused (backends without the hook,
        i.e. the pyo stub, no-op). Next force-advances the queue to the
        following track (no-op when the queue is empty) — the manual mate
        to the auto-advance.
        """
        module_id, action = user_data
        if action == "rewind":
            rewind = getattr(self.backend, "rewind_file_player", None)
            if rewind is not None:
                rewind(module_id)
            return
        if action == "next":
            module = self.patch.modules.get(module_id)
            if module is None:
                return
            if module.params.get("playlist"):
                self._advance_playlist(module_id)
            else:
                self._set_status("Queue empty - nothing to skip to")
            return
        playing = action == "play"
        if not self._set_module_param(module_id, "playing", playing):
            return
        tag = f"fileplayer_playing_{module_id}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, playing)

    def _on_file_seek(self, sender, app_data, user_data) -> None:
        """A FilePlayer seek bar moved (user drag or click): flag the scrub
        so _update_file_positions stops driving the thumb from the playhead
        and commits the seek on release. Kept deliberately thin — recording
        the intent here rather than seeking straight away means a click that
        lands and releases between two frame polls is still caught by the
        release check, and a held drag doesn't spam the backend per pixel.
        """
        self._file_seek_active.add(user_data)

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
                f"Queued {len(paths)} file(s) - {len(queue)} in the list"
            )
            return

        # "path" mode: same mutation path as typing into the field; the
        # renderer re-decodes on the next block because the path changed.
        # wavetable_morph names its path param ``file`` (its Browse
        # shares this dialog); everyone else says ``path``.
        path = paths[0]
        target = self.patch.modules.get(module_id)
        param = (
            "file"
            if target is not None and target.TYPE == "wavetable_morph"
            else "path"
        )
        if not self._set_module_param(module_id, param, path):
            return
        text_tag = f"fileplayer_path_{module_id}"
        if dpg.does_item_exist(text_tag):
            dpg.set_value(text_tag, path)
        self._set_status(f"Selected: {os.path.basename(path)}")

    # ----- file_player queue ("file list") ---------------------------------

    @staticmethod
    def _playlist_display_items(module) -> list[str]:
        """The queue as listbox rows: ``'<n>. <basename>'`` per pending path.

        The leading position number keeps every row unique even when two
        queued files share a basename, so a selected row maps back to an
        unambiguous queue index for Remove (the rows renumber on every
        refresh as the queue drains)."""
        queue = module.params.get("playlist") or []
        return [f"{i + 1}. {os.path.basename(p)}" for i, p in enumerate(queue)]

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

    def _on_remove_playlist_item(self, sender, app_data, user_data) -> None:
        """Remove button: drop the selected row from a FilePlayer's queue.

        The listbox hands back the selected row *string* (``'<n>. name'``);
        matching it against the freshly regenerated numbered rows gives the
        exact queue index to pop — unambiguous even with duplicate
        basenames. No selection (or a stale one) is a gentle no-op."""
        module_id = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        queue = module.params.get("playlist") or []
        if not queue:
            return
        tag = self._playlist_listboxes.get(module_id)
        selected = str(dpg.get_value(tag)) if tag is not None else ""
        items = self._playlist_display_items(module)
        if selected not in items:
            self._set_status("Select a queued track to remove")
            return
        idx = items.index(selected)
        removed = queue.pop(idx)
        self._refresh_playlist_display(module_id)
        self._set_status(f"Removed from queue: {os.path.basename(removed)}")

    def _advance_file_playlists(self) -> None:
        """Auto-advance each FilePlayer queue once per frame.

        A one-shot track that just *ended* pops the head of the queue into
        ``path`` and rolls on. A healthy track ends by running off its end
        (``file_player_finished``); a bad/unreadable one ends by failing to
        decode (``file_player_failed``) — folding both in is what lets the
        queue skip past a dud instead of stalling on it. The 'once per end'
        guard keys on the backend's decode *generation*
        (``file_player_decode_gen``), not a bool edge: a fast-failing file
        can flip failed between two polls, but its generation still differs
        from the track it replaced, so the skip fires exactly once. An empty
        queue leaves the player parked (silence). As a convenience, a running
        player sitting on an empty ``path`` with a non-empty queue is
        kick-started so a fresh 'file list' plays without a manual Browse
        first. Backends without the hooks (the pyo stub) no-op.
        """
        if not self._playlist_listboxes:
            return
        finished = getattr(self.backend, "file_player_finished", None)
        if finished is None:
            return
        failed = getattr(self.backend, "file_player_failed", None)
        decode_gen = getattr(self.backend, "file_player_decode_gen", None)
        running = self.backend.is_running
        for module_id in list(self._playlist_listboxes):
            module = self.patch.modules.get(module_id)
            if module is None:
                continue
            try:
                now_failed = bool(failed(module_id)) if failed else False
                now_ended = bool(finished(module_id)) or now_failed
                gen = int(decode_gen(module_id)) if decode_gen else 0
            except Exception:
                now_failed = now_ended = False
                gen = 0
            if not (module.params.get("playlist") or []):
                continue
            current = str(module.params.get("path") or "")
            # Advance on a *fresh* ended decode (its generation differs from
            # the one we last advanced at — robust to a fast-failing file we
            # never caught mid-decode), or kick-start an idle running player.
            ended_fresh = now_ended and gen != self._fileplayer_advanced_gen.get(module_id)
            if ended_fresh or (running and not current):
                self._advance_playlist(module_id, skipped_bad=now_failed)
                self._fileplayer_advanced_gen[module_id] = gen

    def _report_media_failures(self) -> None:
        """Say so when a sampler/convolver's file did not load.

        These loaders fail soft by design -- they run on background
        threads and must never raise into the audio callback -- so a
        missing sample sounds exactly like a patch that is working
        correctly and happens to be quiet. That silence cost a listening
        pass on 2026-08-29: `sampler_breaks.json` was launched from a
        directory where its relative sample path did not resolve, played
        nothing, and gave no clue why.

        Each (module, path) is named once, not once per frame. The set is
        cleared on load/compile so fixing the file and recompiling gets a
        fresh verdict rather than being suppressed by the old one.
        """
        query = getattr(self.backend, "media_load_failures", None)
        if query is None:
            return
        try:
            failures = query()
        except Exception:
            return
        for module_id, module_type, path in failures:
            key = (module_id, path)
            if key in self._media_failures_reported:
                continue
            self._media_failures_reported.add(key)
            module = self.patch.modules.get(module_id)
            name = (module.name if module is not None else None) or module_type
            shown = path or "(no file set)"
            self._set_status(
                f"{name}: could not load '{shown}' - check the file exists "
                f"(relative paths are looked up next to the patch, then in "
                f"the folder you launched from)"
            )

    def _advance_playlist(self, module_id: int, skipped_bad: bool = False) -> None:
        """Pop the next queued file into a FilePlayer and let it play.

        ``skipped_bad`` tags the status line when the advance is skipping a
        track that failed to decode (vs a clean end-of-track advance)."""
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        queue = module.params.get("playlist")
        if not queue:
            return  # nothing queued: stay parked at the end (silence)
        prev_path = str(module.params.get("path") or "")
        next_path = queue.pop(0)
        # Same mutation path as Browse/typing; the renderer re-decodes
        # and restarts from 0:00 because the path param changed.
        if not self._set_module_param(module_id, "path", next_path):
            return
        text_tag = f"fileplayer_path_{module_id}"
        if dpg.does_item_exist(text_tag):
            dpg.set_value(text_tag, next_path)
        self._refresh_playlist_display(module_id)
        if skipped_bad and prev_path:
            self._set_status(
                f"Skipped unreadable {os.path.basename(prev_path)} "
                f"-> {os.path.basename(next_path)}"
            )
        else:
            self._set_status(f"Queue -> {os.path.basename(next_path)}")

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
        elif module_type in (
            "specific_stereo_speaker_output", "buffered_specific_speaker_output",
            "warping_buffered_speaker_output",
        ):
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
        elif module_type in (
            "specific_stereo_speaker_output", "buffered_specific_speaker_output",
            "warping_buffered_speaker_output",
        ):
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
        """The fader-bank front panel: a labelled ``steps`` slider, the
        ``direction`` run-mode combo and the random ``seed`` beside it
        (the three whole-pattern controls the shared engine reads — see
        modules/sequencer.py), then sixteen vertical pitch faders with
        only a step number and an on/off tickbox beneath each — no other
        text (hover a fader for its note).
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
        with dpg.group(horizontal=True, horizontal_spacing=8):
            dpg.add_combo(
                label="direction",
                items=list(SEQ_DIRECTIONS),
                default_value=str(module.params.get("direction", "forward")),
                width=110,
                callback=self._on_param_changed,
                user_data=(module.id, "direction"),
            )
            dpg.add_drag_int(
                label="seed",
                default_value=int(module.params.get("seed", 1)),
                speed=1,
                min_value=0,
                max_value=999999,
                width=80,
                callback=self._on_param_changed,
                user_data=(module.id, "seed"),
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
        if not self._set_module_param(module_id, f"step{i}_pitch", float(app_data)):
            return
        tip_tag = f"fader_tip_{module_id}_{i}"
        if dpg.does_item_exist(tip_tag):
            dpg.set_value(tip_tag, self._fader_tip(int(app_data)))

    # ----- organ drawbar bank ------------------------------------------------

    def _build_organ_drawbars(self, module) -> None:
        """The nine-drawbar front panel: vertical 0..8 faders labelled by
        footage (hover a fader for its footage + harmonic). Faders-up =
        louder — a deliberate deviation from pulled-out-is-louder
        hardware drawbars; screens read up as more.
        """
        with dpg.group(horizontal=True, horizontal_spacing=6):
            for i in range(1, ORGAN_BARS + 1):
                footage = ORGAN_FOOTAGES[i - 1]
                with dpg.group():
                    try:
                        lvl = int(module.params[f"bar{i}"])
                    except (TypeError, ValueError):
                        lvl = 0
                    lvl = max(0, min(8, lvl))
                    fader = dpg.add_slider_int(
                        vertical=True,
                        default_value=lvl,
                        min_value=0,
                        max_value=8,
                        width=18,
                        height=80,
                        format="",
                        callback=self._on_param_changed,
                        user_data=(module.id, f"bar{i}"),
                    )
                    with dpg.tooltip(fader):
                        dpg.add_text(f"{footage} (drawbar {i})")
                    dpg.add_text(footage.replace(" ", "\n"))

    # ----- matrix_mixer gain grid -------------------------------------------

    def _build_matrix_grid(self, module) -> None:
        """The 4×4 gain grid: rows are inputs, columns are outputs, each
        node a compact ±1 drag (0 = off, negative = phase flip). Row and
        column headers keep the routing readable.
        """
        from ..modules.matrix_mixer import MATRIX_SIZE

        with dpg.group(horizontal=True, horizontal_spacing=4):
            dpg.add_text("    ")
            for c in range(1, MATRIX_SIZE + 1):
                dpg.add_text(f"out{c}".center(7))
        for r in range(1, MATRIX_SIZE + 1):
            with dpg.group(horizontal=True, horizontal_spacing=4):
                dpg.add_text(f"in{r}")
                for c in range(1, MATRIX_SIZE + 1):
                    key = f"g{r}{c}"
                    try:
                        val = float(module.params.get(key, 0.0))
                    except (TypeError, ValueError):
                        val = 0.0
                    dpg.add_drag_float(
                        default_value=val,
                        speed=0.01,
                        min_value=-1.0,
                        max_value=1.0,
                        format="%.2f",
                        width=52,
                        callback=self._on_param_changed,
                        user_data=(module.id, key),
                    )

    # ----- possibility_seq panel --------------------------------------------

    # Cell colours, keyed by step state (plus "off" for a step parked past
    # the loop length): (base, hovered, active, text). Undecided is the
    # loudest on purpose — the ?s are what this module is *for*, so the eye
    # should land on them first.
    _PSEQ_COLORS = {
        "1": ((66, 148, 108), (84, 176, 130), (56, 128, 92), (244, 250, 245)),
        "0": ((52, 56, 64), (68, 73, 83), (44, 48, 55), (146, 152, 163)),
        "?": ((176, 130, 46), (200, 152, 62), (154, 112, 38), (24, 21, 14)),
        "off": ((38, 40, 45), (44, 47, 53), (34, 36, 41), (84, 88, 96)),
    }

    def _pseq_theme(self, key: str):
        """A cached button theme per cell colour key (built on first use).

        Themes are global dpg items, so they are built once and shared by
        every possibility_seq node rather than per cell — sixteen cells a
        node would otherwise leak a theme apiece on every patch load.
        Returns None if dpg cannot build one, and the cell simply keeps the
        default button colours (label and tooltip still carry the state).
        """
        cached = self._pseq_themes.get(key)
        if cached is not None:
            return cached
        base, hovered, active, text = self._PSEQ_COLORS[key]
        try:
            with dpg.theme() as theme:
                with dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, base)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hovered)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, active)
                    dpg.add_theme_color(dpg.mvThemeCol_Text, text)
        except Exception:
            return None
        self._pseq_themes[key] = theme
        return theme

    @staticmethod
    def _pseq_tip(i: int, state: str, p: float) -> str:
        """Tooltip for one cell: what it does now, and how to change it."""
        if state == "1":
            what = "hit - fires every take"
        elif state == "0":
            what = "rest - silent every take"
        else:
            what = f"undecided - fires {p * 100:.0f}% of takes"
        return (
            f"step {i}: {what}\n"
            "click to cycle 0 > 1 > ?   |   right-click for odds"
        )

    def _build_possibility_panel(self, module) -> None:
        """The possibility panel: the whole module on one face.

        Row one is the settings (loop length, when the ?s decide, the bag,
        the seed). Then sixteen click-to-cycle cells — one gesture,
        ``0 -> 1 -> ? -> 0``, the source project's rule — with the step
        number beneath and a right-click popup carrying that step's odds.
        The readout underneath counts the possibility space the pattern
        currently holds, which is the number this module is really about.
        """
        mid = module.id
        with dpg.group(horizontal=True):
            dpg.add_slider_int(
                label="steps",
                default_value=int(module.params["steps"]),
                min_value=1,
                max_value=PSEQ_MAX_STEPS,
                width=120,
                callback=self._on_possibility_steps,
                user_data=(mid, "steps"),
            )
            dpg.add_combo(
                label="mode",
                items=list(POSSIBILITY_MODES),
                default_value=str(module.params["mode"]),
                width=80,
                callback=self._on_possibility_param,
                user_data=(mid, "mode"),
            )
        with dpg.group(horizontal=True):
            balanced = dpg.add_checkbox(
                label="balanced",
                default_value=bool(module.params["balanced"]),
                callback=self._on_possibility_param,
                user_data=(mid, "balanced"),
            )
            with dpg.tooltip(balanced):
                dpg.add_text(
                    "Deal the fair ?s from a shuffle-bag instead of flipping\n"
                    "coins, so every bar lands on its share. Weighted steps\n"
                    "keep their own odds either way."
                )
            dpg.add_drag_int(
                label="seed",
                default_value=int(module.params["seed"]),
                speed=1,
                min_value=0,
                max_value=999999,
                width=110,
                callback=self._on_possibility_param,
                user_data=(mid, "seed"),
            )

        cells: list[int] = []
        with dpg.group(horizontal=True, horizontal_spacing=3):
            for i in range(1, PSEQ_MAX_STEPS + 1):
                state = str(module.params[f"step{i}_state"])
                with dpg.group():
                    cell = dpg.add_button(
                        label=state,
                        width=24,
                        height=28,
                        callback=self._on_possibility_step,
                        user_data=(mid, i),
                    )
                    cells.append(cell)
                    with dpg.tooltip(cell):
                        dpg.add_text(
                            self._pseq_tip(
                                i, state, float(module.params[f"step{i}_p"])
                            ),
                            tag=f"pseq_tip_{mid}_{i}",
                        )
                    # Right-click: that step's odds. Only a ? consults them,
                    # so the popup says so rather than hiding on a decided
                    # step — odds set now survive the cycle back round to ?.
                    with dpg.popup(cell, mousebutton=dpg.mvMouseButton_Right):
                        dpg.add_text(f"step {i} odds")
                        dpg.add_slider_float(
                            label="fires",
                            default_value=float(module.params[f"step{i}_p"]),
                            min_value=0.0,
                            max_value=1.0,
                            format="%.2f",
                            width=140,
                            callback=self._on_possibility_odds,
                            user_data=(mid, i),
                        )
                        dpg.add_text(
                            "0.5 is a fair coin (and the only\n"
                            "odds the bag deals); read by ? only."
                        )
                    dpg.add_text(f"{i}")
        self._pseq_cells[mid] = cells
        self._pseq_count_labels[mid] = dpg.add_text("", tag=f"pseq_count_{mid}")
        self._refresh_possibility_panel(mid)

    def _refresh_possibility_panel(self, module_id: int) -> None:
        """Redraw every cell (label, colour, tooltip) and the count readout
        straight from the model — one path, so the panel can never show a
        pattern the patch doesn't hold."""
        module = self.patch.modules.get(module_id)
        cells = self._pseq_cells.get(module_id)
        if module is None or not cells:
            return
        try:
            steps = int(module.params.get("steps", PSEQ_MAX_STEPS))
        except (TypeError, ValueError):
            steps = PSEQ_MAX_STEPS
        states = [
            str(module.params.get(f"step{i}_state", "0"))
            for i in range(1, PSEQ_MAX_STEPS + 1)
        ]
        for idx, cell in enumerate(cells):
            i = idx + 1
            state = states[idx]
            try:
                p = float(module.params.get(f"step{i}_p", 0.5))
            except (TypeError, ValueError):
                p = 0.5
            dpg.set_item_label(cell, state)
            theme = self._pseq_theme("off" if i > steps else state)
            if theme is not None:
                dpg.bind_item_theme(cell, theme)
            tip_tag = f"pseq_tip_{module_id}_{i}"
            if dpg.does_item_exist(tip_tag):
                dpg.set_value(tip_tag, self._pseq_tip(i, state, p))
        label = self._pseq_count_labels.get(module_id)
        if label is not None:
            dpg.set_value(label, format_possibilities(states, steps))

    def _on_possibility_step(self, sender, app_data, user_data) -> None:
        """A step cell was clicked: cycle its state 0 -> 1 -> ? -> 0."""
        module_id, i = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        nxt = pseq_next_state(str(module.params.get(f"step{i}_state", "0")))
        if self._set_module_param(module_id, f"step{i}_state", nxt):
            self._refresh_possibility_panel(module_id)

    def _on_possibility_steps(self, sender, app_data, user_data) -> None:
        """Loop length moved: parked cells grey out and the count shrinks."""
        module_id, name = user_data
        if self._set_module_param(module_id, name, int(app_data)):
            self._refresh_possibility_panel(module_id)

    def _on_possibility_param(self, sender, app_data, user_data) -> None:
        """mode / balanced / seed - no repaint needed, just the write."""
        module_id, name = user_data
        value = int(app_data) if name == "seed" else app_data
        self._set_module_param(module_id, name, value)

    def _on_possibility_odds(self, sender, app_data, user_data) -> None:
        """A step's right-click odds slider moved."""
        module_id, i = user_data
        if self._set_module_param(module_id, f"step{i}_p", float(app_data)):
            self._refresh_possibility_panel(module_id)

    # ----- possibility_selector panel ---------------------------------------

    # Cell colours keyed by route: one hue per output (so a bar reads as a
    # kit at a glance), the family's amber for anything still open (a full
    # ``?`` or a subset -- the label carries the digits), grey for a rest,
    # and the parked colour past the loop length.
    _PSEL_COLORS = {
        "1": ((172, 70, 70), (196, 88, 88), (150, 58, 58), (250, 240, 240)),
        "2": ((66, 108, 176), (84, 128, 200), (56, 92, 154), (240, 245, 250)),
        "3": ((66, 148, 108), (84, 176, 130), (56, 128, 92), (244, 250, 245)),
        "4": ((132, 88, 176), (154, 106, 200), (114, 76, 154), (246, 240, 250)),
        "0": _PSEQ_COLORS["0"],
        "?": _PSEQ_COLORS["?"],
        "off": _PSEQ_COLORS["off"],
    }

    def _psel_theme(self, key: str):
        """A cached button theme per route colour key (built on first use);
        the same global-theme economy as ``_pseq_theme``."""
        cached = self._psel_themes.get(key)
        if cached is not None:
            return cached
        base, hovered, active, text = self._PSEL_COLORS[key]
        try:
            with dpg.theme() as theme:
                with dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, base)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hovered)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, active)
                    dpg.add_theme_color(dpg.mvThemeCol_Text, text)
        except Exception:
            return None
        self._psel_themes[key] = theme
        return theme

    @staticmethod
    def _psel_tip(i: int, state: str) -> str:
        """Tooltip for one cell: where the step goes now, and how to change it."""
        cands = psel_parse_route(state)
        if not cands:
            what = "rest - nothing passes"
        elif len(cands) == 1:
            what = f"to out{cands[0]} every take"
        elif len(cands) == PSEL_N_OUTS:
            what = "undecided - any output (the weights decide)"
        else:
            what = "undecided - " + " or ".join(f"out{k}" for k in cands)
        return (
            f"step {i}: {what}\n"
            "click to cycle 0 > 1 > 2 > 3 > 4 > ?   |   right-click to pick outputs"
        )

    def _build_selector_panel(self, module) -> None:
        """The selector panel: the possibility panel one level up.

        Settings rows (loop length, mode, bag, seed), a row of four
        ``weight`` sliders (how the open steps lean), then sixteen
        click-to-cycle cells -- ``0 -> 1 -> 2 -> 3 -> 4 -> ? -> 0`` -- each
        with a right-click popup of four checkboxes for the step's
        candidate set (``13`` = out1 or out3), and the possibility readout
        underneath. Every repaint reads from the model, one path.
        """
        mid = module.id
        with dpg.group(horizontal=True):
            dpg.add_slider_int(
                label="steps",
                default_value=int(module.params["steps"]),
                min_value=1,
                max_value=PSEL_MAX_STEPS,
                width=120,
                callback=self._on_selector_steps,
                user_data=(mid, "steps"),
            )
            dpg.add_combo(
                label="mode",
                items=list(POSSIBILITY_MODES),
                default_value=str(module.params["mode"]),
                width=80,
                callback=self._on_selector_param,
                user_data=(mid, "mode"),
            )
        with dpg.group(horizontal=True):
            balanced = dpg.add_checkbox(
                label="balanced",
                default_value=bool(module.params["balanced"]),
                callback=self._on_selector_param,
                user_data=(mid, "balanced"),
            )
            with dpg.tooltip(balanced):
                dpg.add_text(
                    "Deal the fair open steps from a shuffle-bag - the\n"
                    "output used least so far - so a bar spreads across\n"
                    "the outputs. Weighted steps keep their lean."
                )
            dpg.add_drag_int(
                label="seed",
                default_value=int(module.params["seed"]),
                speed=1,
                min_value=0,
                max_value=999999,
                width=110,
                callback=self._on_selector_param,
                user_data=(mid, "seed"),
            )
        with dpg.group(horizontal=True):
            lean = dpg.add_text("? leans to")
            with dpg.tooltip(lean):
                dpg.add_text(
                    "How an open step draws among its outputs: all equal is\n"
                    "a fair draw, 0 takes that output out of every ?.\n"
                    "Decided steps ignore these."
                )
            for k in range(1, PSEL_N_OUTS + 1):
                dpg.add_slider_float(
                    label=f"{k}",
                    default_value=float(module.params[f"weight{k}"]),
                    min_value=0.0,
                    max_value=1.0,
                    format="%.2f",
                    width=62,
                    callback=self._on_selector_weight,
                    user_data=(mid, k),
                )

        cells: list[int] = []
        with dpg.group(horizontal=True, horizontal_spacing=2):
            for i in range(1, PSEL_MAX_STEPS + 1):
                state = str(module.params[f"step{i}_state"])
                with dpg.group():
                    cell = dpg.add_button(
                        label=state,
                        width=30,
                        height=28,
                        callback=self._on_selector_step,
                        user_data=(mid, i),
                    )
                    cells.append(cell)
                    with dpg.tooltip(cell):
                        dpg.add_text(
                            self._psel_tip(i, state),
                            tag=f"psel_tip_{mid}_{i}",
                        )
                    # Right-click: the step's candidate set. One checkbox
                    # per output; all four ticked is a plain ?, one is a
                    # decided step, none is a rest -- the popup and the
                    # click cycle write the same state string.
                    with dpg.popup(cell, mousebutton=dpg.mvMouseButton_Right):
                        dpg.add_text(f"step {i} may go to")
                        cands = psel_parse_route(state)
                        for k in range(1, PSEL_N_OUTS + 1):
                            dpg.add_checkbox(
                                label=f"out{k}",
                                default_value=k in cands,
                                tag=f"psel_cand_{mid}_{i}_{k}",
                                callback=self._on_selector_candidate,
                                user_data=(mid, i, k),
                            )
                    dpg.add_text(f"{i}")
        self._psel_cells[mid] = cells
        self._psel_count_labels[mid] = dpg.add_text("", tag=f"psel_count_{mid}")
        self._refresh_selector_panel(mid)

    def _refresh_selector_panel(self, module_id: int) -> None:
        """Redraw every cell (label, colour, tooltip, popup ticks) and the
        count readout straight from the model."""
        module = self.patch.modules.get(module_id)
        cells = self._psel_cells.get(module_id)
        if module is None or not cells:
            return
        try:
            steps = int(module.params.get("steps", PSEL_MAX_STEPS))
        except (TypeError, ValueError):
            steps = PSEL_MAX_STEPS
        states = [
            str(module.params.get(f"step{i}_state", "0"))
            for i in range(1, PSEL_MAX_STEPS + 1)
        ]
        for idx, cell in enumerate(cells):
            i = idx + 1
            state = states[idx]
            cands = psel_parse_route(state)
            dpg.set_item_label(cell, psel_format_route(cands))
            if i > steps:
                key = "off"
            elif len(cands) == 1:
                key = str(cands[0])
            elif cands:
                key = "?"
            else:
                key = "0"
            theme = self._psel_theme(key)
            if theme is not None:
                dpg.bind_item_theme(cell, theme)
            tip_tag = f"psel_tip_{module_id}_{i}"
            if dpg.does_item_exist(tip_tag):
                dpg.set_value(tip_tag, self._psel_tip(i, state))
            for k in range(1, PSEL_N_OUTS + 1):
                cand_tag = f"psel_cand_{module_id}_{i}_{k}"
                if dpg.does_item_exist(cand_tag):
                    dpg.set_value(cand_tag, k in cands)
        label = self._psel_count_labels.get(module_id)
        if label is not None:
            dpg.set_value(label, psel_format_possibilities(states, steps))

    def _on_selector_step(self, sender, app_data, user_data) -> None:
        """A cell was clicked: cycle its route 0 -> 1 -> 2 -> 3 -> 4 -> ? -> 0."""
        module_id, i = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        nxt = psel_next_state(str(module.params.get(f"step{i}_state", "0")))
        if self._set_module_param(module_id, f"step{i}_state", nxt):
            self._refresh_selector_panel(module_id)

    def _on_selector_candidate(self, sender, app_data, user_data) -> None:
        """A popup checkbox flipped: add or drop that output from the step's
        candidate set and write the canonical state string."""
        module_id, i, k = user_data
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        cands = set(psel_parse_route(module.params.get(f"step{i}_state", "0")))
        if app_data:
            cands.add(int(k))
        else:
            cands.discard(int(k))
        if self._set_module_param(module_id, f"step{i}_state", psel_format_route(cands)):
            self._refresh_selector_panel(module_id)

    def _on_selector_steps(self, sender, app_data, user_data) -> None:
        """Loop length moved: parked cells grey out and the count shrinks."""
        module_id, name = user_data
        if self._set_module_param(module_id, name, int(app_data)):
            self._refresh_selector_panel(module_id)

    def _on_selector_param(self, sender, app_data, user_data) -> None:
        """mode / balanced / seed - no repaint needed, just the write."""
        module_id, name = user_data
        value = int(app_data) if name == "seed" else app_data
        self._set_module_param(module_id, name, value)

    def _on_selector_weight(self, sender, app_data, user_data) -> None:
        """One of the four lean sliders moved."""
        module_id, k = user_data
        self._set_module_param(module_id, f"weight{k}", float(app_data))

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
            label=f"Calibrate keys - {module.name}",
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
        if not self._set_module_param(module.id, "velocity_curve", curve):
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
                "No calibration yet - every key plays at 1.0.",
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
            dpg.set_value("vel_capture_status", "nothing captured - Learn first")
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
        self._set_module_param(module_id, "velocity_curve", curve)

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
        """Release every note/key on every key-accepting module — avoids stuck
        notes and stuck KeyTrigger gates (e.g. on focus loss / audio stop)."""
        self._held_keys.clear()
        self._raw_key_down.clear()
        for module in self.patch.modules.values():
            if getattr(module, "ACCEPTS_COMPUTER_KEYS", False) or getattr(
                module, "ACCEPTS_RAW_KEYS", False
            ):
                module.all_notes_off()

    # ----- KeyTrigger raw-key path ----------------------------------------

    def _on_raw_key_press(self, sender, app_data, user_data=None) -> None:
        """Route a physical key press to KeyTrigger modules (by name), or bind
        it if a node is in Learn mode.

        Debounced against OS auto-repeat via ``_raw_key_down`` (its own set, so
        it never tangles with the note/zoom ``_held_keys``). Only *bindable*
        keys (in ``_KEY_CODE_TO_NAME``) do anything — reserved keys are absent
        from the map, so the app's shortcuts always win. Learn takes any
        bindable key; performance dispatch additionally defers when a modifier
        is held (shortcut context) or a text field is focused (you're typing).
        """
        key_code = app_data
        if key_code in self._raw_key_down:
            return
        self._raw_key_down.add(key_code)
        name = _KEY_CODE_TO_NAME.get(key_code)
        if name is None:
            return  # not a bindable key (reserved / unknown)
        if self._key_learn_target is not None:
            self._bind_learned_key(name)
            return
        if self._ctrl_down() or self._alt_down():
            return  # a shortcut chord — shortcuts win
        if self._text_field_focused():
            return  # typing into a field, not performing
        self._dispatch_raw_key(name, down=True)

    def _on_raw_key_release(self, sender, app_data, user_data=None) -> None:
        """Release a physical key on every KeyTrigger. Unguarded on purpose —
        a key released while typing/with a modifier must still drop its gate,
        so nothing sticks on."""
        key_code = app_data
        self._raw_key_down.discard(key_code)
        name = _KEY_CODE_TO_NAME.get(key_code)
        if name is None:
            return
        self._dispatch_raw_key(name, down=False)

    def _dispatch_raw_key(self, name: str, down: bool) -> None:
        """Fan a named raw key event out to every ACCEPTS_RAW_KEYS module;
        each self-filters by its own bound key."""
        for module in self.patch.modules.values():
            if getattr(module, "ACCEPTS_RAW_KEYS", False):
                (module.raw_key_down if down else module.raw_key_up)(name)

    def _text_field_focused(self) -> bool:
        """True while a registered text input has focus, so a bound
        performance key shouldn't fire (you're typing, not playing)."""
        for tag in self._text_input_tags:
            try:
                if dpg.does_item_exist(tag) and dpg.is_item_focused(tag):
                    return True
            except Exception:
                pass
        return False

    def _alt_down(self) -> bool:
        """True if either Alt key is currently held down."""
        for _name in ("mvKey_LAlt", "mvKey_RAlt", "mvKey_Alt", "mvKey_Menu"):
            code = getattr(dpg, _name, None)
            if code is None:
                continue
            try:
                if dpg.is_key_down(code):
                    return True
            except Exception:
                pass
        return False

    # ----- KeyTrigger Learn button ----------------------------------------

    @staticmethod
    def _keytrigger_key_caption(bound: str) -> str:
        return f"Key: {bound}" if bound else "Key: (click Learn)"

    def _set_keytrigger_learn_label(self, module_id: int, learning: bool) -> None:
        tag = f"keytrigger_learnbtn_{module_id}"
        if dpg.does_item_exist(tag):
            dpg.set_item_label(tag, "Press a key..." if learning else "Learn")

    def _refresh_keytrigger_label(self, module_id: int) -> None:
        module = self.patch.modules.get(module_id)
        tag = f"keytrigger_keylabel_{module_id}"
        if module is not None and dpg.does_item_exist(tag):
            bound = str(module.params.get("key") or "")
            dpg.set_value(tag, self._keytrigger_key_caption(bound))

    def _on_key_learn(self, sender, app_data, user_data) -> None:
        """Learn button: arm this node to capture the next key press. Clicking
        it again (same node) cancels; clicking a different node's button hands
        the arming over."""
        module_id = user_data
        if self._key_learn_target == module_id:
            self._key_learn_target = None
            self._set_keytrigger_learn_label(module_id, learning=False)
            self._set_status("Learn cancelled")
            return
        prev = self._key_learn_target
        self._key_learn_target = module_id
        if prev is not None:
            self._set_keytrigger_learn_label(prev, learning=False)
        self._set_keytrigger_learn_label(module_id, learning=True)
        self._set_status("Press a key to bind (Learn again to cancel)")

    def _bind_learned_key(self, name: str) -> None:
        """Bind the just-pressed key ``name`` to the learning node's ``key``
        param and leave Learn mode. ``key`` is a model-only param (the module
        self-filters on it; the renderer never reads it), so it's set directly
        on the shared module object."""
        module_id = self._key_learn_target
        self._key_learn_target = None
        if module_id is None:
            return
        module = self.patch.modules.get(module_id)
        if module is None:
            return
        module.params["key"] = name
        self._set_keytrigger_learn_label(module_id, learning=False)
        self._refresh_keytrigger_label(module_id)
        self._set_status(f"Bound key: {name}")

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
            self._file_seek_sliders.pop(module_id, None)
            self._file_seek_active.discard(module_id)
            self._playlist_listboxes.pop(module_id, None)
            self._fileplayer_advanced_gen.pop(module_id, None)
            # Same for a buffered sink's ring readout: the text item dies
            # with the node, so a lingering entry would have the next
            # _update_sink_buffers frame set_value a freed dpg item.
            self._sink_buffer_labels.pop(module_id, None)
            self._sink_buffer_last.pop(module_id, None)
            self._sink_buffer_flash.pop(module_id, None)
            # possibility_seq panel: the cell buttons and count text are
            # freed with the node, so a lingering entry would have the next
            # refresh set_value a dead item. The themes are global and stay.
            self._pseq_cells.pop(module_id, None)
            self._pseq_count_labels.pop(module_id, None)
            self._psel_cells.pop(module_id, None)
            self._psel_count_labels.pop(module_id, None)
            # Drop meter bookkeeping too: the bar drawlist items are freed
            # with the node below, so a lingering entry would make the next
            # _update_cv_meters frame call set_value on a dead item — the
            # "Item not found" GUI crash. (_audio_meter_bars is keyed by id;
            # the CV bar + auto-range maps are keyed by (id, port).)
            self._audio_meter_bars.pop(module_id, None)
            self._scope_displays.pop(module_id, None)
            self._sampler_faces.pop(module_id, None)
            for _mk in [k for k in self._cv_meter_bars if k[0] == module_id]:
                self._cv_meter_bars.pop(_mk, None)
            for _mk in [k for k in self._meter_bounds if k[0] == module_id]:
                self._meter_bounds.pop(_mk, None)
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
            self._refresh_late_links()

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
        self._refresh_late_links()
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
        # A new patch gets a fresh verdict on its media files.
        self._media_failures_reported.clear()

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

        self._refresh_late_links()
        tail = self._report_load_warnings(path)
        self._set_status(f"Loaded: {os.path.basename(path)}{tail}")

    def _report_load_warnings(self, path: str) -> str:
        """Print what the loader threw away; return the status-line tail.

        ``Patch.from_dict`` drops a cable whose module or port is gone
        rather than loading it silently inert (the old failure looked
        exactly like "nothing happens"). Fail-soft only helps if
        something SAYS so, and the status bar is one line: it carries
        the count, the console carries the list.

        Returns ``""`` for a clean patch so the usual "Loaded: foo.json"
        is untouched. ASCII only -- the UI font paints nothing above
        U+00FF.
        """
        warnings = list(getattr(self.patch, "load_warnings", ()) or ())
        if not warnings:
            return ""
        name = os.path.basename(path)
        print(
            f"[pysynthrack] {name}: dropped {len(warnings)} dead cable(s) on load:"
        )
        for line in warnings:
            print(f"    - {line}")
        plural = "" if len(warnings) == 1 else "s"
        return f" - {len(warnings)} dead cable{plural} dropped (see console)"

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
        self._scope_displays.clear()
        self._sampler_faces.clear()
        self._file_pos_labels.clear()
        self._file_seek_sliders.clear()
        self._file_seek_active.clear()
        self._playlist_listboxes.clear()
        self._fileplayer_advanced_gen.clear()
        self._sink_buffer_labels.clear()
        self._sink_buffer_last.clear()
        self._sink_buffer_flash.clear()
        self._pseq_cells.clear()
        self._pseq_count_labels.clear()
        # KeyTrigger UI state: stale text-input tags and any pending Learn
        # don't carry across a patch load.
        self._text_input_tags.clear()
        self._raw_key_down.clear()
        self._key_learn_target = None
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
        self._update_stream_health(load)
        self._update_loops_readout()

    def _update_stream_health(self, load: float | None) -> None:
        """Refresh the underflow count and host-API readouts.

        Same getattr-guarded, lock-free discipline as the DSP figure: a
        backend without ``stream_health_snapshot`` (the pyo stub) just
        leaves both slots greyed out. The tooltip is rewritten each tick
        because its reading depends on load *and* underflows together --
        the pair is what tells jitter from overload.
        """
        if not dpg.does_item_exist(XRUN_TEXT_TAG):
            return
        xruns: int | None = None
        host_api = ""
        if self.backend.is_running:
            snap = getattr(self.backend, "stream_health_snapshot", None)
            if snap is not None:
                xruns, _in_xruns, host_api = snap()
        dpg.set_value(XRUN_TEXT_TAG, format_xruns(xruns))
        dpg.configure_item(XRUN_TEXT_TAG, color=xrun_color(xruns))
        if dpg.does_item_exist(HOSTAPI_TEXT_TAG):
            dpg.set_value(HOSTAPI_TEXT_TAG, format_host_api(host_api))
        if dpg.does_item_exist(XRUN_TOOLTIP_TAG):
            dpg.set_value(XRUN_TOOLTIP_TAG, diagnose(load, xruns))

    # ----- feedback loops: amber cables + the toolbar readout -------------

    @staticmethod
    def _cable_key(cable: Cable) -> tuple[int, str, int, str]:
        return (
            cable.src_module_id,
            cable.src_port,
            cable.dst_module_id,
            cable.dst_port,
        )

    def _cable_label(self, cable: Cable) -> str:
        """``type#id.port -> type#id.port`` -- short and unambiguous, for
        the status bar and the loops tooltip (ASCII only: it is
        painted by the DPG font)."""
        def _m(mid: int) -> str:
            module = self.patch.modules.get(mid)
            return f"{module.TYPE}#{mid}" if module is not None else f"#{mid}"
        return (
            f"{_m(cable.src_module_id)}.{cable.src_port} -> "
            f"{_m(cable.dst_module_id)}.{cable.dst_port}"
        )

    def _ensure_late_link_theme(self) -> int | None:
        if self._late_link_theme is not None:
            return self._late_link_theme
        try:
            with dpg.theme() as theme:
                with dpg.theme_component(dpg.mvNodeLink):
                    dpg.add_theme_color(
                        dpg.mvNodeCol_Link, LATE_LINK_COLOR,
                        category=dpg.mvThemeCat_Nodes,
                    )
                    dpg.add_theme_color(
                        dpg.mvNodeCol_LinkHovered, LATE_LINK_HOVER_COLOR,
                        category=dpg.mvThemeCat_Nodes,
                    )
                    dpg.add_theme_color(
                        dpg.mvNodeCol_LinkSelected, LATE_LINK_HOVER_COLOR,
                        category=dpg.mvThemeCat_Nodes,
                    )
                    dpg.add_theme_style(
                        dpg.mvNodeStyleVar_LinkThickness, 4.0,
                        category=dpg.mvThemeCat_Nodes,
                    )
        except Exception:
            return None
        self._late_link_theme = theme
        return theme

    def _refresh_late_links(self) -> None:
        """Recolour the cables the feedback door will read one block late.

        Asks the backend which cables of the CURRENT patch close a loop
        (a pure function of the patch -- the same answer the next
        compile gives, so this is right before Start too), binds the
        amber theme to those links and the default to every other, and
        rewrites the toolbar ``loops`` readout. Called on every cable or
        module change and on load/new, never per frame. A backend
        without the observable (the pyo stub) leaves everything as it
        is.
        """
        finder = getattr(self.backend, "feedback_cables", None)
        late: set[tuple[int, str, int, str]] = set()
        if finder is not None:
            try:
                late = set(finder(self.patch))
            except Exception:
                late = set()
        self._late_keys = late
        theme = self._ensure_late_link_theme() if late else self._late_link_theme
        for link_id, cable in list(self._link_to_cable.items()):
            is_late = self._cable_key(cable) in late
            try:
                if is_late and theme is not None:
                    dpg.bind_item_theme(link_id, theme)
                else:
                    dpg.bind_item_theme(link_id, 0)
            except Exception:
                # A link deleted this frame is a freed item; nothing to paint.
                pass
        self._update_loops_readout(force=True)

    def _update_loops_readout(self, force: bool = False) -> None:
        """The toolbar ``loops`` slot: ``loops --`` (none), ``loops N``
        (N cables read one block late), and while running ``loops N !K``
        in the warning colour when K blocks of a loop have been scrubbed
        for going non-finite. Touches dpg only when the text changes."""
        if not dpg.does_item_exist(LOOPS_TEXT_TAG):
            return
        n = len(self._late_keys)
        scrubs = 0
        if self.backend.is_running:
            getter = getattr(self.backend, "feedback_scrubs", None)
            if getter is not None:
                try:
                    scrubs = int(getter())
                except Exception:
                    scrubs = 0
        if n == 0:
            text, color = "loops --", IDLE_COLOR
            tip = "No feedback loops in this patch."
        elif scrubs:
            text, color = f"loops {n} !{scrubs}", WARN_COLOR
            tip = (
                f"{scrubs} block(s) of a feedback loop blew past float range and "
                "were scrubbed to silence.\nAdd a limiter to the loop, or close "
                "it through a matrix_mixer (soft_clip)."
            )
        else:
            text, color = f"loops {n}", (200, 200, 200, 255)
            tip = ""
        if n:
            lines = [
                f"{self._cable_label(c)}  (reads one block late)"
                for c in self._link_to_cable.values()
                if self._cable_key(c) in self._late_keys
            ]
            head = f"{n} cable(s) close a feedback loop and read one block late (amber):"
            tip = head + "\n" + "\n".join(lines) + ("\n\n" + tip if tip else "")
        if force or text != self._loops_text:
            self._loops_text = text
            dpg.set_value(LOOPS_TEXT_TAG, text)
            dpg.configure_item(LOOPS_TEXT_TAG, color=color)
            if dpg.does_item_exist(LOOPS_TOOLTIP_TAG):
                dpg.set_value(LOOPS_TOOLTIP_TAG, tip)

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
        stale: list[tuple[int, str]] = []
        for key, bar in self._cv_meter_bars.items():
            value = levels.get(key)
            if value is None:
                continue
            value = float(value)
            if not math.isfinite(value):
                # A NaN / inf cable reads as what it is: the number says
                # "nan" (ASCII -- the font paints nothing above U+00FF)
                # and the bar holds its last reading. A readout is a
                # diagnostic, not a clamp; painting 0.00 would pass a
                # broken module off as an idle one.
                try:
                    dpg.configure_item(bar, overlay=f"{value:.2f}")
                except Exception:
                    stale.append(key)
                continue
            fill = self._auto_range_fill(key, value)
            # Defensive, like _draw_meter_channel: a bar whose node was
            # deleted this frame is a freed dpg item and set_value would raise
            # ("Item not found"), taking the whole GUI loop down. Prune it and
            # move on rather than crash — delete-time pruning is the primary
            # guard; this self-heals any path that misses it.
            try:
                dpg.set_value(bar, fill)
                dpg.configure_item(bar, overlay=f"{value:+.2f}")
            except Exception:
                stale.append(key)
        for key in stale:
            self._cv_meter_bars.pop(key, None)

    # Buffered-sink readout colors and the flash hold. Idle matches the other
    # grey hint text; OK is a calm green near the CV-meter fill; trouble is
    # an amber that holds this many seconds after the last underrun/drop so
    # a one-frame counter tick is actually visible.
    _SINK_BUF_IDLE_RGBA = (170, 170, 170)
    _SINK_BUF_OK_RGBA = (150, 205, 160)
    _SINK_BUF_TROUBLE_RGBA = (240, 190, 90)
    _SINK_BUF_FLASH_S = 1.5

    def _update_sink_buffers(self) -> None:
        """Push each buffered sink's secondary-stream ring telemetry into
        its node readout: fill %, queued/capacity, underrun / drop counts.

        Once per frame; backends without the hook (the pyo stub) show a
        permanent idle line. A sink absent from the snapshot (stopped, no
        device, failed open) reads idle and its flash baseline is dropped;
        a LIVE stream switch (device / buffer_size edit) keeps the sink in
        consecutive snapshots, so its callbacks reset the baseline instead
        (see _on_param_changed / _on_buffer_size_changed) — either way the
        next stream's counters never compare against the old stream's.
        Amber-tints the text for ``_SINK_BUF_FLASH_S`` after a counter
        increase; defensive against dead dpg items exactly like
        _update_cv_meters (prune, don't crash).
        """
        if not self._sink_buffer_labels:
            return
        snapshot = getattr(self.backend, "snapshot_sink_buffers", None)
        entries = snapshot() if snapshot is not None else {}
        now = time.monotonic()
        stale: list[int] = []
        for mid, label in self._sink_buffer_labels.items():
            entry = entries.get(mid)
            if entry is None:
                color = self._SINK_BUF_IDLE_RGBA
                self._sink_buffer_last.pop(mid, None)
                self._sink_buffer_flash.pop(mid, None)
            else:
                counts = (entry[2], entry[3])
                prev = self._sink_buffer_last.get(mid)
                if prev is not None and (
                    counts[0] > prev[0] or counts[1] > prev[1]
                ):
                    self._sink_buffer_flash[mid] = now + self._SINK_BUF_FLASH_S
                self._sink_buffer_last[mid] = counts
                if now < self._sink_buffer_flash.get(mid, 0.0):
                    color = self._SINK_BUF_TROUBLE_RGBA
                else:
                    color = self._SINK_BUF_OK_RGBA
            try:
                dpg.set_value(label, format_sink_buffer(entry))
                dpg.configure_item(label, color=color)
            except Exception:
                stale.append(mid)
        for mid in stale:
            self._sink_buffer_labels.pop(mid, None)
            self._sink_buffer_last.pop(mid, None)
            self._sink_buffer_flash.pop(mid, None)

    def _update_file_positions(self) -> None:
        """Push each FilePlayer's elapsed / total playhead time into its
        node readout and drive its seek bar's thumb. Once per frame; backends
        without the hook (the pyo stub) no-op, and a node with no current
        entry keeps its last text.

        The seek bar is a 0..1 slider whose thumb normally tracks the
        playhead (``elapsed / total``). While the user is dragging it
        (``dpg.is_item_active``) — or on the frame their ``_on_file_seek``
        callback just flagged a quick click — the thumb is left alone so it
        follows the mouse; on release the fractional position is handed to
        the backend's ``seek_file_player`` so the jump is block-aligned.
        """
        if not self._file_pos_labels:
            return
        snapshot = getattr(self.backend, "snapshot_file_positions", None)
        if snapshot is None:
            return
        seek = getattr(self.backend, "seek_file_player", None)
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
            slider = self._file_seek_sliders.get(mid)
            if slider is None:
                continue
            if dpg.is_item_active(slider):
                # Being dragged: let the thumb follow the mouse, don't stomp it.
                self._file_seek_active.add(mid)
            elif mid in self._file_seek_active:
                # Just released (or a quick click): seek to where it was left.
                self._file_seek_active.discard(mid)
                if seek is not None:
                    seek(mid, float(dpg.get_value(slider)))
            else:
                # Idle: reflect the playhead as a 0..1 fill.
                frac = (elapsed / total) if total > 0 else 0.0
                dpg.set_value(slider, min(1.0, max(0.0, frac)))

    @staticmethod
    def _format_time(seconds: float) -> str:
        """Seconds -> ``m:ss`` (minutes uncapped, e.g. ``12:05``)."""
        seconds = max(0.0, float(seconds))
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"

    # Meter floor in dB; the bar spans [_METER_FLOOR_DB, 0] dBFS.
    _METER_FLOOR_DB = -90.0

    # Scope face geometry (pixels) and column count (one min/max pair
    # per column; ~1 px columns at zoom 1).
    _SCOPE_W = 220
    _SCOPE_H = 110
    _SCOPE_COLS = 200

    # Sampler face geometry: the loaded file's min/max envelope with the
    # region (start/end) and loop (loop_start/loop_end) markers over it.
    _SFACE_W = 220
    _SFACE_H = 64
    _SFACE_REGION = (120, 200, 255, 255)
    _SFACE_LOOP = (255, 200, 90, 255)
    _SFACE_WAVE = (120, 230, 130, 255)
    _SFACE_DIM = (60, 70, 60, 255)

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

    def _update_scopes(self) -> None:
        """Repaint each Scope node's waveform from the capture rings.

        Per frame and per scope: ask the backend for the recent window
        (scope_window hook, numpy backend only), run the display maths in
        ui/scope_math (trigger alignment + min/max columns, or xy
        decimation), and reconfigure the node's polylines. A frozen scope
        skips the repaint entirely — the picture simply stops. Wrapped
        per-scope in try/except so a torn frame or a mid-delete widget
        self-heals next frame (CV-meter precedent).
        """
        if not self._scope_displays:
            return
        win_hook = getattr(self.backend, "scope_window", None)
        if win_hook is None:
            return
        w, h = float(self._SCOPE_W), float(self._SCOPE_H)
        for module_id, bundle in list(self._scope_displays.items()):
            module = self.patch.modules.get(module_id)
            if module is None:
                continue
            if bool(module.params.get("freeze", False)):
                continue
            try:
                need = scope_math.request_samples(
                    float(module.params.get("time_div", 10.0)),
                    self.backend.sample_rate,
                )
                snap = scope_math.build_snapshot(
                    win_hook(module_id, need),
                    module.params,
                    self.backend.sample_rate,
                    self._SCOPE_COLS,
                )
                if snap is None:
                    continue
                gain = float(module.params.get("gain", 1.0))
                if snap["mode"] == "xy":
                    xy2 = snap["xy2"]
                    if xy2 is None:
                        xy2 = np.zeros_like(snap["xy1"])
                    pts1 = scope_math.xy_polyline(snap["xy1"], xy2, w, h, gain)
                    pts2 = None
                else:
                    pts1 = (
                        scope_math.envelope_polyline(snap["cols1"], w, h, gain)
                        if snap["cols1"] is not None
                        else None
                    )
                    pts2 = (
                        scope_math.envelope_polyline(snap["cols2"], w, h, gain)
                        if snap["cols2"] is not None
                        else None
                    )
                if pts1 and len(pts1) >= 2:
                    dpg.configure_item(bundle["p1"], points=pts1)
                want2 = bool(pts2 and len(pts2) >= 2)
                if want2:
                    dpg.configure_item(bundle["p2"], points=pts2)
                if want2 != bundle.get("p2_shown", False):
                    dpg.configure_item(bundle["p2"], show=want2)
                    bundle["p2_shown"] = want2
            except Exception:
                continue  # self-heal next frame

    def _update_sampler_faces(self) -> None:
        """Repaint a Sampler node's waveform face when something moved.

        Per frame and per sampler: ask the backend for the loaded file's
        overview (sampler_overview hook, numpy backend only) and compare
        a (path, region, loop, mode) key against what was last painted.
        Nothing changed -> nothing drawn; this is a static picture, not a
        scope. Otherwise: the envelope polyline (ui/scope_math, the
        scope's own shape), the start/end markers, and the loop markers in
        `loop` mode -- placed inside the region, because that is what the
        fractions mean. Wrapped per-face in try/except so a mid-delete
        widget self-heals next frame (scope precedent).
        """
        if not self._sampler_faces:
            return
        hook = getattr(self.backend, "sampler_overview", None)
        if hook is None:
            return
        w, h = float(self._SFACE_W), float(self._SFACE_H)
        for module_id, bundle in list(self._sampler_faces.items()):
            module = self.patch.modules.get(module_id)
            if module is None:
                continue
            try:
                overview, loaded = hook(module_id)
                prm = module.params

                def frac(name, default):
                    try:
                        return min(1.0, max(0.0, float(prm.get(name, default))))
                    except (TypeError, ValueError):
                        return default

                start = frac("start", 0.0)
                end = frac("end", 1.0)
                l_start = frac("loop_start", 0.0)
                l_end = frac("loop_end", 1.0)
                looping = str(prm.get("mode", "one_shot")) == "loop"
                reverse = bool(prm.get("reverse", False))
                key = (loaded, overview is not None, start, end,
                       l_start, l_end, looping, reverse)
                if key == bundle.get("key"):
                    continue
                bundle["key"] = key

                if overview is None:
                    dpg.configure_item(
                        bundle["wave"], points=[(0, h / 2), (w - 1, h / 2)],
                        color=self._SFACE_DIM,
                    )
                    dpg.configure_item(
                        bundle["caption"], text="no sample", show=True,
                    )
                    for item in bundle["markers"].values():
                        dpg.configure_item(item, show=False)
                    continue

                pts = scope_math.envelope_polyline(overview, w, h, 1.0)
                if len(pts) >= 2:
                    dpg.configure_item(
                        bundle["wave"], points=pts, color=self._SFACE_WAVE,
                    )
                caption = "<< reverse" if reverse else ""
                dpg.configure_item(
                    bundle["caption"], text=caption, show=bool(caption),
                )

                def place(name, fraction, show=True):
                    x = min(w - 1.0, max(0.0, fraction * (w - 1.0)))
                    dpg.configure_item(
                        bundle["markers"][name], p1=(x, 0), p2=(x, h - 1),
                        show=show,
                    )

                place("start", start)
                place("end", end)
                span = end - start
                show_loop = looping and span > 0.0 and l_end > l_start
                place("loop_start", start + l_start * span, show_loop)
                place("loop_end", start + l_end * span, show_loop)
            except Exception:
                continue  # self-heal next frame

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

        A non-finite ``value`` never enters the window: one NaN used to
        turn ``lo``/``hi`` into NaN for good, and the bar sat at 0 long
        after the cable healed. It reads mid-scale and changes nothing.
        """
        if not math.isfinite(value):
            return 0.5
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
                # Recompile re-resolves every media path, so a file that
                # was missing gets another chance to be found -- and to be
                # reported again if it still isn't.
                self._media_failures_reported.clear()
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
