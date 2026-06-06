"""DearPyGui app — node editor, palette, file menu, transport.

The UI is intentionally a thin layer over the model. Every user action is
translated into a model mutation (``Patch.add_module``, ``Patch.connect``,
``module.set_param``) and then the backend is told to recompile or update.
That means the model is always the source of truth and the same operations
work whether they came from the GUI, a loaded patch, or a future scripting
interface.
"""
from __future__ import annotations

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
from ..modules.keyboard import Keyboard, midi_to_name, semitone_to_midi
from ..modules.cvcombiner import CVCOMBINER_MODES
from ..modules.cvtofrequency import MODES as CVTOFREQ_MODES
from ..modules.lfo import LFO_WAVEFORMS
from ..modules.midiinput import AUTO_DEVICE, available_devices as midi_available_devices
from ..modules.oscillator import WAVEFORMS


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

        # Diagonal stagger for newly-added nodes so they don't stack.
        self._next_node_pos = [40, 40]

        # Track which physical keys are currently down so OS auto-repeat
        # doesn't fire note_on repeatedly while a key is held.
        self._held_keys: set[int] = set()

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
            dpg.start_dearpygui()
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

    # ----- node creation --------------------------------------------------

    def _create_node_for_module(self, module, pos=None) -> int:
        if pos is None:
            pos = (self._next_node_pos[0], self._next_node_pos[1])
            # Stagger downward and right for the next node.
            self._next_node_pos[0] = (self._next_node_pos[0] + 220) % 800
            self._next_node_pos[1] = (self._next_node_pos[1] + 60) % 500

        with dpg.node(
            label=f"{module.name} (#{module.id})",
            parent=EDITOR_TAG,
            pos=pos,
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

            # Outputs (right)
            for port in module.output_ports:
                with dpg.node_attribute(
                    attribute_type=dpg.mvNode_Attr_Output,
                ) as attr_id:
                    dpg.add_text(f"{port.name} ▶")
                self._attr_to_port[attr_id] = (module.id, port.name, "out")
                self._port_to_attr[(module.id, port.name, "out")] = attr_id

        return node_id

    def _add_param_widget(self, module, param_name: str, default) -> None:
        current = module.params[param_name]
        user_data = (module.id, param_name)

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
        if param_name == "device" and module.TYPE == "midi_input":
            devices = midi_available_devices()
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
                    max_value=100.0,
                    format="%.2f Hz",
                    width=140,
                    callback=self._on_param_changed,
                    user_data=user_data,
                )
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

    def _on_key_press(self, sender, app_data, user_data=None) -> None:
        """Route a physical key press to every Keyboard module in the patch.

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
            if not isinstance(module, Keyboard):
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
            if not isinstance(module, Keyboard):
                continue
            octave = int(module.params.get("octave", 4))
            midi_note = semitone_to_midi(octave, semitone)
            module.note_off(midi_note)

    def _all_keyboards_notes_off(self) -> None:
        """Release every note on every Keyboard module — avoids stuck notes."""
        self._held_keys.clear()
        for module in self.patch.modules.values():
            if isinstance(module, Keyboard):
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

    def _capture_node_positions(self) -> None:
        """Snapshot the current DPG node positions into ``patch.ui``.

        Called just before save so the on-disk layout reflects whatever
        the user dragged around. Stored as ``{"node_positions": {str(mid): [x, y]}}``
        — module-id keys are JSON strings to keep the round-trip clean.
        """
        positions: dict[str, list[float]] = {}
        for module_id, node_id in self._module_to_node.items():
            try:
                pos = dpg.get_item_pos(node_id)
            except Exception:
                continue
            if pos is None:
                continue
            positions[str(module_id)] = [float(pos[0]), float(pos[1])]
        if positions:
            self.patch.ui["node_positions"] = positions

    def _load_patch_from(self, path: str) -> None:
        was_running = self.backend.is_running
        if was_running:
            self.backend.stop()
            dpg.set_item_label(AUDIO_BTN_TAG, "Start audio")

        self._clear_editor()
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
        # Reset the cascade so a fresh-loaded patch (with no saved positions)
        # starts laying out from the top-left again.
        self._next_node_pos = [40, 40]

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
