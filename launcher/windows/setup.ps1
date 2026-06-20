# Nexus-OSINT — one-time Windows setup (full install).
#
# Run once on a fresh machine. This script:
#   1. Finds a Python 3 interpreter (the "py" launcher or "python" on PATH).
#   2. Creates the project's virtual environment (.venv) if it doesn't exist.
#   3. Installs all Python dependencies into it.
#   4. Generates the app icon.
#   5. Puts a "Nexus OSINT" icon on your Desktop.
#
# After this, just double-click the Desktop icon to use the app — no terminal.
# Safe to run again: it skips steps that are already done.

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$Root = Split-Path -Parent (Split-Path -Parent $here)   # repo root
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$Req  = Join-Path $Root 'requirements.txt'

function Write-Step($n, $msg) { Write-Host ""; Write-Host "[$n] $msg" -ForegroundColor Cyan }

# --- 1. Find a Python 3 interpreter ----------------------------------------
function Find-Python {
    # Prefer the Windows "py" launcher (handles versions cleanly), then python.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try { & py -3 --version *> $null; if ($LASTEXITCODE -eq 0) { return @('py','-3') } } catch {}
    }
    foreach ($name in 'python','python3') {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) {
            # Skip the Microsoft Store alias stub, which isn't a real interpreter.
            if ($c.Source -notlike '*WindowsApps*') { return @($c.Source) }
        }
    }
    return $null
}

Write-Step 1 "Looking for Python 3..."
$py = Find-Python
if (-not $py) {
    Write-Host ""
    Write-Host "  Python 3 was not found on this computer." -ForegroundColor Yellow
    Write-Host "  Please install it once from:  https://www.python.org/downloads/"
    Write-Host "  During install, tick 'Add Python to PATH', then run this again."
    Write-Host ""
    exit 1
}
Write-Host "  Found: $($py -join ' ')"

# --- 2. Create the virtual environment -------------------------------------
if (Test-Path $VenvPy) {
    Write-Step 2 "Virtual environment already exists — skipping."
} else {
    Write-Step 2 "Creating virtual environment (.venv)..."
    & $py[0] @($py[1..($py.Length-1)]) -m venv $Venv
    if (-not (Test-Path $VenvPy)) { throw "Failed to create .venv" }
}

# --- 3. Install dependencies -----------------------------------------------
Write-Step 3 "Installing dependencies (this can take a few minutes)..."
& $VenvPy -m pip install --upgrade pip *> $null
& $VenvPy -m pip install -r $Req
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# --- 4. Generate the icon --------------------------------------------------
Write-Step 4 "Generating app icon..."
try { & $VenvPy (Join-Path $Root 'launcher\make_icon.py') } catch {
    Write-Host "  (icon generation skipped — a committed icon will be used)"
}

# --- 5. Create the Desktop shortcut ----------------------------------------
Write-Step 5 "Creating the Desktop icon..."
& (Join-Path $here 'install_shortcut.ps1')

Write-Host ""
Write-Host "  All set!  Look for the 'Nexus OSINT' icon on your Desktop." -ForegroundColor Green
Write-Host "  Double-click it any time to open the app."
Write-Host ""
