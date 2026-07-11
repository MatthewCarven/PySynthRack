# PySynthRack

A modular software synthesizer in Python with a drag-cable node-graph UI. Think VCV Rack / Reaktor in spirit — modules with input/output jacks, you wire them together by drawing cables.

## Status: active development (pre-1.0)

PySynthRack has grown well past its first prototype into a working instrument:
**61 modules** across seven categories, a full CV/modulation system, MIDI,
recording, and a node editor with zoom, live meters, and per-patch layout
persistence. It's still pre-1.0 — the patch format and APIs can shift — but you
can build real patches in it today.

### What works

- **61 modules** in seven Add-menu categories:
  - **Sources** — oscillator (sine / saw / square / triangle, each in naive,
    PolyBLEP/PolyBLAMP `*_blep`, and wavetable `*_wt` flavours), noise,
    computer-keyboard and CV keyboards, single-key triggers, MIDI input, a
    WAV/audio **file player with a queue**, and microphone input.
  - **Filters & EQ** — multimode resonant filter, Linkwitz-Riley crossover, and
    parametric / sweep / motion / tilt EQ plus a loudness contour.
  - **Effects** — delay, reverb, chorus, flanger, phaser, distortion,
    waveshaper, bitcrusher, tape, convolution reverb (IR), ring modulator,
    frequency shifter, pitch shifter, resampler (with tape-stop/spin), vocoder,
    and a full dynamics set (compressor, limiter, noise gate, transient shaper).
  - **Modulation** — LFO, ADSR, AD envelope, clock, step sequencer, fader
    sequencer.
  - **Routing & VCA** — VCA, 4-in mixer, audio/CV combiners.
  - **CV & Utilities** — audio↔CV bridges, Schmitt trigger, constant, CV
    scale/offset, sample & hold, level meter.
  - **Outputs** — mono / left / right / stereo speaker outs, per-device output
    routing (with an optional per-sink buffer size), and a disk recorder.
- **Drag-cable node-graph UI** (DearPyGui): wire jacks together, canvas zoom
  (Ctrl +/‑/wheel), scroll-to-adjust knobs, live CV and audio-level meters, and
  overlap-aware node placement.
- **Three signal kinds** — audio / CV / gate — with bridge modules to convert
  between them and a standardised 1 V/oct + `cv_depth` modulation convention.
- **MIDI input** from a hardware controller (optional `[midi]` extra).
- **Recording to disk** and **multi-device output** (route a sub-mix to a named
  sound card, e.g. a monitor/cue bus — optionally with its own buffer size for a
  flaky or higher-latency device).
- **Save / load patches** as JSON, with per-patch window size/position and zoom
  restored on reopen; a global buffer-size control and DSP-load readout live in
  the toolbar.
- **Crash logging** — uncaught GUI and audio-thread errors are written to
  `~/.pysynthrack/crashes/` instead of vanishing.
- **Two audio backends** behind one interface: **sounddevice + numpy** — the
  reference implementation every module targets, and the default — plus an
  optional, partial **pyo** backend.
- **CLI mode** for headless rendering, backed by an extensive headless test
  suite (~2,050 tests — no audio device or display required).

See **[docs/MODULES.md](docs/MODULES.md)** for the full per-module reference,
**[TODO.md](TODO.md)** for the roadmap, and **[docs/architecture.md](docs/architecture.md)**
for the design write-up.

<img width="1266" height="793" alt="image" src="https://github.com/user-attachments/assets/60918d8e-b21c-46fd-a0d4-768854ec9ee6" />

<img width="1561" height="793" alt="image" src="https://github.com/user-attachments/assets/8741ae75-ae27-4e05-9c8c-cb4364d673a2" />


## Architecture

```
src/pysynthrack/
├── core/            # Pure-Python model: Port, Module, Patch (no audio, no UI)
├── audio/           # AudioBackend interface + numpy (reference) / pyo backends
├── modules/         # The 61 module type definitions — ports & params, no DSP
├── io_patch/        # JSON save / load
├── ui/              # DearPyGui app — node editor, palette, transport, meters
├── _crash.py        # Crash-log wiring (GUI + audio-thread hooks)
├── error_handler.py # Rich error-report writer (~/.pysynthrack/crashes/)
└── __main__.py      # Entry point (GUI, with a --cli fallback)
```

The model layer is intentionally backend-agnostic. The UI edits the model. The backend compiles the model into a running audio graph. This keeps DSP and UI concerns cleanly separated, and means swapping the audio engine doesn't require touching the UI.

See `docs/architecture.md` for the longer write-up.

## Windows Binary
https://drive.google.com/file/d/1rM69rwT8YW5tqquJZ126CUD4lJsgooyq/view?usp=sharing

<img width="609" height="34" alt="image" src="https://github.com/user-attachments/assets/21e8f531-fb40-496a-9b4f-723e3fcc7d32" />

New Version
https://drive.google.com/file/d/1O-8EN4LsOeG2Sx664ZExyqiz6oRdIVnG/view?usp=sharing

<img width="597" height="30" alt="image" src="https://github.com/user-attachments/assets/20fb6933-d8cb-4555-ab50-f9e9df6bc05f" />

Extra New Version
https://drive.google.com/file/d/1BsgRBylvy4ngGPuNp09A66T-OATiMogK/view?usp=sharing

<img width="589" height="20" alt="image" src="https://github.com/user-attachments/assets/51c1cad6-5c5a-439c-b0fc-377bb706036f" />

Ultra New Version
https://drive.google.com/file/d/1XRuWLhmSIns1rs8PRUg1TmpmDwLoh5uX/view?usp=drive_link

<img width="591" height="22" alt="image" src="https://github.com/user-attachments/assets/e14b3a37-ca71-45c5-ae1e-bf146f146680" />

SuperHyperMegaUltra New Version
https://drive.google.com/file/d/1bqbVphSJbcw08xo-yxneqqdNiC5IMGXc/view?usp=sharing

<img width="591" height="22" alt="image" src="https://github.com/user-attachments/assets/ec99a758-d7d8-443c-bbfa-03b3b563e95a" />


## Installation (Windows)

PySynthRack splits its dependencies so the audio engine can be installed on any Python version, while the GUI (DearPyGui) is optional — DPG sometimes lags on bleeding-edge Python releases.

### Picking a Python version

DearPyGui and pyo both publish wheels for Python 3.10 – 3.12 on Windows. If you're on Python 3.13 or newer, DPG wheels may not exist yet and pyo definitely doesn't. **Python 3.12 is the sweet spot** — install it just for this project's venv if your default Python is newer.

### Using `uv` (recommended if installed)

```powershell
# install Python 3.12 in uv's managed location (no system PATH changes)
uv python install 3.12

# from the project root, create the venv on that Python
cd <project root>
uv venv --python 3.12 .venv
.venv\Scripts\activate

# IMPORTANT: use `uv pip`, not `pip`. uv venvs don't ship pip by default,
# so plain `pip install` will fall through to your system Python's pip
# and silently install into the wrong site-packages.
uv pip install -e ".[gui]"

# Optional: also try pyo. Skips silently if no compiler is set up.
uv pip install -e ".[pyo]"
```

### Using plain `py` / `pip`

```powershell
# create a 3.12 venv (assumes Python 3.12 is installed and registered)
py -3.12 -m venv .venv
.venv\Scripts\activate
python --version    # confirm 3.12.x

# minimum — installs numpy + sounddevice + the pysynthrack package
pip install -e .

# add the GUI
pip install -e ".[gui]"

# everything at once
pip install -e ".[all]"
```

### MIDI input

To play patches from a real MIDI controller, install the optional `[midi]` extra. It pulls in `mido` (the pure-Python message layer) and `python-rtmidi` (the C-extension that talks to the OS MIDI stack).

```powershell
pip install -e ".[midi]"
```

If `python-rtmidi` fails to build on Windows, the wheels usually solve it: `pip install --upgrade pip` first, then retry. On Linux you may need `apt install libasound2-dev libjack-dev` before the build succeeds. The MIDIInput module gracefully reports "no devices found" if the install is missing — the rest of the app still works.

The `[gui]`, `[pyo]`, `[midi]`, `[media]`, `[all]`, and `[dev]` extras are defined in `pyproject.toml`. `[media]` bundles a static ffmpeg binary so the file player can read mp3/flac/ogg and the audio track of video files without a system ffmpeg. The trailing dot in `pip install -e .` means "install the project in this directory in editable mode" — without it you'll get `No module named pysynthrack` when you try to run.

### Verifying you installed into the right venv

After install, this should print a path inside your project's `.venv\Lib\site-packages\`, not `C:\Program Files\Python3xx\`:

```powershell
python -c "import pysynthrack; print(pysynthrack.__file__)"
```

If the path is somewhere outside the venv, packages went to the wrong Python — usually a sign that `pip` and `python` resolve to different installs.

## Notes on pyo

pyo doesn't ship Windows wheels — `pip install pyo` tries to compile from C source. That works only if you have Microsoft Visual Studio Build Tools + PortAudio dev headers set up. If the build fails (`error: [WinError 2] The system cannot find the file specified`), just skip the `[pyo]` extra — the numpy backend is the reference implementation and covers every module, so you lose nothing.

## Running

### GUI mode (requires DearPyGui)

```powershell
.venv\Scripts\activate
python -m pysynthrack
```

The UI opens and auto-loads `examples/hello_sine.json` — a single sine oscillator wired to the speaker output. Click **Start audio** in the toolbar; you should hear a 440 Hz tone. If DearPyGui isn't installed, this command automatically falls back to CLI mode with a hint.

### CLI mode (no GUI required)

```powershell
python -m pysynthrack --cli                       # default patch, press Enter to stop
python -m pysynthrack --cli --seconds 3           # auto-stop after 3 seconds
python -m pysynthrack --cli --patch my_patch.json # custom patch
python -m pysynthrack --cli --backend numpy       # force numpy backend
```

CLI mode is the fastest way to verify your audio device works and a patch makes sound. It's also useful for batch rendering or running patches from a script.

### Forcing a backend

The app selects `pyo` when it's installed, else `sounddevice + numpy` (the full-featured default). To override:

```powershell
$env:PYSYNTHRACK_BACKEND = "numpy"   # or "pyo"
python -m pysynthrack --cli --seconds 2
```

## Troubleshooting

- **`Could not find a version that satisfies dearpygui...`** — your Python is too new for any released DearPyGui wheel. Either install a slightly older Python (3.12 is a safe bet) into a fresh venv, or just skip the `[gui]` extra and use `--cli`. Audio works fine without the GUI.
- **`No module named pysynthrack`** — you installed dependencies but not the package itself. Run `pip install -e .` (with the dot) from the project root.
- **`No audio backend is available`** — neither `pyo` nor `sounddevice` imported. Try `pip install --force-reinstall sounddevice`.
- **GUI launches but no sound** — open the file menu, confirm the patch loaded, click **Start audio**. Check the status bar — it shows which backend is active and whether it's running.

## Development

```powershell
pip install -r requirements-dev.txt
pytest
ruff check src tests
```

Headless tests (everything under `tests/`) don't require an audio device or display.

## Project conventions

- DSP code lives in `audio/` backends; UI code lives in `ui/`. Module classes in `modules/` declare ports and parameters but contain no audio rendering — they are descriptions, not implementations.
- Cable connections are stored in `Patch`, not on the ports themselves. This keeps the model serializable.
- The numpy backend is the reference implementation — every module targets it, and it's the default. The optional pyo backend covers a subset and is selected first only when installed. Both implement the same `AudioBackend` interface.
