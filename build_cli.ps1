<#
.SYNOPSIS
    Build the console (CLI) one-file PySynthRack-cli.exe.
.DESCRIPTION
    Same pre-flight as build.ps1 but builds the console variant.
.EXAMPLE
    .\build_cli.ps1
    .\dist\PySynthRack-cli.exe --cli --seconds 2
#>
[CmdletBinding()]
param([switch]$NoClean)

$ErrorActionPreference = "Stop"
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

    $preflightCode = @"
import importlib, sys
required = [
    ('numpy', 'numpy'),
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
        throw "Build aborted: missing runtime dependencies."
    }

    $piVersion = $null
    try { $piVersion = & $venvPython -m PyInstaller --version 2>&1 } catch { }
    if (-not $piVersion -or "$piVersion" -match "No module named") {
        Write-Host "pyinstaller not found; installing with 'uv pip install pyinstaller'..."
        & uv pip install pyinstaller
        $piVersion = & $venvPython -m PyInstaller --version 2>&1
    }
    Write-Host "pyinstaller $piVersion"

    if (-not $NoClean) {
        Write-Host "Cleaning build\ and dist\ ..."
        Remove-Item -Recurse -Force ".\build", ".\dist" -ErrorAction SilentlyContinue
    }

    Write-Host "Building CLI exe ..."
    & $venvPython -m PyInstaller pysynthrack-cli.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller exited with code $LASTEXITCODE" }

    $exe = Join-Path $root "dist\PySynthRack-cli.exe"
    if (-not (Test-Path $exe)) {
        throw "Build finished but $exe not found."
    }
    $sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "OK  Built $exe  ($sizeMb MB)" -ForegroundColor Green
    Write-Host "    Try: .\dist\PySynthRack-cli.exe --cli --seconds 2"
}
finally {
    Pop-Location
}
