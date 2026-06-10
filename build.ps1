<#
.SYNOPSIS
    Build the windowed (GUI) one-file PySynthRack.exe.

.DESCRIPTION
    Activates the uv-managed venv at .venv\, verifies that pyinstaller and
    every native dependency PySynthRack needs at runtime are installed in
    that venv, then runs pyinstaller pysynthrack.spec.

    Why the pre-flight check?  PyInstaller bundles only what's installed in
    the python it runs from.  On Windows with uv, a plain `pip install`
    inside a uv venv often silently hits SYSTEM python instead -- and
    PyInstaller then produces an exe with no deps inside, which crashes
    silently at launch with ModuleNotFoundError on its native imports.
    The pre-flight catches that BEFORE wasting build time.

.EXAMPLE
    .\build.ps1                 # full build
    .\build.ps1 -NoClean        # keep PyInstaller's incremental cache
#>
[CmdletBinding()]
param(
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"
# PowerShell 7+ otherwise treats any native command stderr output as an
# error and aborts; we deliberately want to inspect Python's stderr
# when an import fails, so opt out.
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Push-Location $root
try {
    if (-not (Test-Path ".\.venv\Scripts\Activate.ps1")) {
        throw "No .venv found at $root\.venv. Run 'uv venv' and 'uv pip install -e .[all]' first."
    }
    . .\.venv\Scripts\Activate.ps1

    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        throw "venv python not found at $venvPython"
    }
    $reportedPython = & $venvPython -c "import sys; print(sys.executable)"
    Write-Host "Building with: $reportedPython"

    # ---- Pre-flight: ONE python call that imports each module and prints
    # ---- a structured result line.  Stays on stdout so PowerShell's
    # ---- error-on-stderr behaviour can't bite us regardless of how
    # ---- $ErrorActionPreference is set.
    $preflightCode = @"
import importlib, sys
required = [
    ('numpy', 'numpy'),
    ('scipy', 'scipy'),
    ('sounddevice', 'sounddevice'),
    ('dearpygui.dearpygui', 'dearpygui'),
    ('mido', 'mido'),
    ('rtmidi', 'python-rtmidi'),
]
for mod, pkg in required:
    try:
        importlib.import_module(mod)
        sys.stdout.write(f'OK\t{mod}\t{pkg}\n')
    except BaseException as e:
        sys.stdout.write(f'MISSING\t{mod}\t{pkg}\t{type(e).__name__}: {e}\n')
sys.stdout.flush()
"@
    $results = & $venvPython -c $preflightCode 2>&1
    $missing = @()
    foreach ($line in $results) {
        $parts = "$line" -split "`t"
        if ($parts.Count -ge 3 -and $parts[0] -eq "OK") {
            Write-Host "  [ok]      $($parts[1])" -ForegroundColor DarkGray
        }
        elseif ($parts.Count -ge 3 -and $parts[0] -eq "MISSING") {
            $missing += $parts[2]
            $detail = if ($parts.Count -ge 4) { "  $($parts[3])" } else { "" }
            Write-Host "  [MISSING] $($parts[1])  (package: $($parts[2]))$detail" -ForegroundColor Yellow
        }
        # Any other line is unexpected python noise; let it through to help
        # debug environments where python itself misbehaves.
        else {
            Write-Host "  $line" -ForegroundColor DarkGray
        }
    }
    if ($missing.Count -gt 0) {
        $uniq = ($missing | Select-Object -Unique) -join " "
        Write-Host ""
        Write-Host "Pre-flight failed: the venv is missing $($missing.Count) runtime dep(s)." -ForegroundColor Red
        Write-Host "Install them with:" -ForegroundColor Red
        Write-Host "    uv pip install -e `".[all]`"" -ForegroundColor Cyan
        Write-Host "  (or, for just the missing ones:)" -ForegroundColor Red
        Write-Host "    uv pip install $uniq" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "If a package itself has no wheel for your Python version" -ForegroundColor Yellow
        Write-Host "(common for python-rtmidi and pyo on bleeding-edge Pythons)," -ForegroundColor Yellow
        Write-Host "consider a Python 3.13 venv for builds:" -ForegroundColor Yellow
        Write-Host "    uv venv .venv --python 3.13" -ForegroundColor Cyan
        throw "Build aborted: missing runtime dependencies."
    }

    # ---- pyinstaller itself ----------------------------------------------
    $piVersion = $null
    try { $piVersion = & $venvPython -m PyInstaller --version 2>&1 } catch { }
    if (-not $piVersion -or "$piVersion" -match "No module named") {
        Write-Host "pyinstaller not found in venv; installing with 'uv pip install pyinstaller'..."
        & uv pip install pyinstaller
        $piVersion = & $venvPython -m PyInstaller --version 2>&1
    }
    Write-Host "pyinstaller $piVersion"

    if (-not $NoClean) {
        Write-Host "Cleaning build\ and dist\ ..."
        Remove-Item -Recurse -Force ".\build", ".\dist" -ErrorAction SilentlyContinue
    }

    Write-Host "Building windowed exe ..."
    & $venvPython -m PyInstaller pysynthrack.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller exited with code $LASTEXITCODE" }

    $exe = Join-Path $root "dist\PySynthRack.exe"
    if (-not (Test-Path $exe)) {
        throw "Build finished but $exe not found."
    }
    $sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "OK  Built $exe  ($sizeMb MB)" -ForegroundColor Green
    Write-Host "    Examples are baked into the exe; double-clicking should launch the GUI."
    Write-Host "    Crash reports (if any) land in `$env:USERPROFILE\.pysynthrack\crashes\."
}
finally {
    Pop-Location
}
