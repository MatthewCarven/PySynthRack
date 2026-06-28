# PySynthRack

A modular software synthesizer in Python with a drag-cable node-graph UI. Think VCV Rack / Reaktor in spirit — modules with input/output jacks, you wire them together by drawing cables.

## Status: v0.1 (work in progress)

What works in v0.1:

- Oscillator module — sine / saw / square / triangle, each (except sine) in three
  band-limiting flavours: naive, PolyBLEP/PolyBLAMP (`*_blep`), and wavetable (`*_wt`)
- Speaker output module — plus LeftSpeakerOut / RightSpeakerOut hard-panned variants (pair them for stereo)
- Drag-cable node-graph UI (DearPyGui)
- Start/Stop audio
- Save / load patch as JSON
- Two audio backends behind one interface — `pyo` (primary) and `sounddevice + numpy` (fallback). The app picks whichever is available at startup.

Coming next: filters, ADSR envelope, LFO, mixer, splitter/combiner, Linkwitz-Riley crossover, disk-writer output, MIDI input.

<img width="1266" height="793" alt="image" src="https://github.com/user-attachments/assets/60918d8e-b21c-46fd-a0d4-768854ec9ee6" />


## Architecture

```
src/pysynthrack/
├── core/        # Pure-Python model: Port, Module, Patch (no audio, no UI)
├── audio/       # AudioBackend interface + pyo / numpy implementations
├── modules/     # Module type definitions (Oscillator, SpeakerOutput, ...)
├── io_patch/    # JSON save / load
├── ui/          # DearPyGui app — node editor, palette, transport
└── __main__.py  # Entry point
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

The `[gui]`, `[pyo]`, `[midi]`, `[all]`, and `[dev]` extras are defined in `pyproject.toml`. The trailing dot in `pip install -e .` means "install the project in this directory in editable mode" — without it you'll get `No module named pysynthrack` when you try to run.

### Verifying you installed into the right venv

After install, this should print a path inside your project's `.venv\Lib\site-packages\`, not `C:\Program Files\Python3xx\`:

```powershell
python -c "import pysynthrack; print(pysynthrack.__file__)"
```

If the path is somewhere outside the venv, packages went to the wrong Python — usually a sign that `pip` and `python` resolve to different installs.

### Version control

This folder is set up for git but the repo has to be initialized from your own PowerShell (the editor's sandbox can't reliably write `.git` through the Windows mount).

```powershell
cd "C:\Users\Admin\Desktop\-=Programming=-\Python Synthesiser 2\Python Synthesizer"

# If a broken .git folder is present from a previous attempt, delete it first.
if (Test-Path .git) { Remove-Item -Recurse -Force .git }

# Initialize on the modern default branch name.
git init -b main

# Stage everything; .gitignore already excludes .venv, __pycache__, *.wav, etc.
git add .

# Sanity-check what's about to be committed.
git status

# First commit.
git commit -m "v0.2: oscillator, keyboard, filter, drag-cable UI"

# Optional: tag the milestone so you can `git checkout v0.2.0` later.
git tag v0.2.0
```

To push to GitHub afterwards:

```powershell
# Create the empty repo on github.com first (no README, no .gitignore — we have those).
git remote add origin https://github.com/<your-username>/pysynthrack.git
git push -u origin main
git push --tags
```

## Notes on pyo

pyo doesn't ship Windows wheels — `pip install pyo` tries to compile from C source. That works only if you have Microsoft Visual Studio Build Tools + PortAudio dev headers set up. If the build fails (`error: [WinError 2] The system cannot find the file specified`), skip the `[pyo]` extra — the numpy backend handles everything in v0.1 and v0.2.

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

CLI mode is the fastest way to verify your audio device works and a patch makes sound. It's also useful for batch rendering or running patches from a script — both arrive properly in v0.3.

### Forcing a backend

The app prefers `pyo` when installed, else falls back to `sounddevice + numpy`. To override:

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
- The pyo backend is preferred when available; the numpy backend is the fallback. Both implement the same `AudioBackend` interface.
