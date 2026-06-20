# Nexus-OSINT desktop launcher.
#
# Behaves like a native app:
#   1. Starts the local server (hidden, no console) if it isn't already up.
#      The server's Boot Sync automatically re-runs your latest topic queries
#      on every fresh start, so reopening always shows current results.
#   2. Opens a chromeless "app window" (Edge or Chrome in --app mode) — no
#      address bar, no tabs, so it doesn't look like a web page.
#   3. When you close that window, the server we started is shut down.
#
# Nothing here ever touches the network beyond 127.0.0.1, and it starts no
# server if one is already running (so a second click just opens a window).

$ErrorActionPreference = 'SilentlyContinue'

$Root    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)  # repo root (launcher/windows/ is two levels under it)
$Url     = 'http://127.0.0.1:8000'
$Health  = "$Url/health"
$DataDir = Join-Path $Root 'data'
$Profile = Join-Path $DataDir 'app-profile'   # isolated browser profile for app mode

function Test-ServerUp {
    try { return (Invoke-WebRequest $Health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}

function Count-AppWindows {
    # How many browser processes are still running OUR chromeless app window?
    # Identified by the dedicated --user-data-dir, which is unique to this app
    # and absent from the user's normal browsing. Chromium spawns helper
    # processes that all carry this flag, so any count > 0 means the app is
    # still open; 0 means every Nexus window has been closed. This is what lets
    # us avoid the old bug where closing one window killed another window's
    # server (Chromium delegates a second --app launch to the first process and
    # exits immediately, which used to trip the shutdown prematurely).
    $needle = "--user-data-dir=$Profile"
    $procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe' OR Name='chrome.exe'" -ErrorAction SilentlyContinue
    return (@($procs | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) })).Count
}

function Find-Browser {
    # Prefer Edge (always present on Win10/11), then Chrome. Returns a path or $null.
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe')
    )
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    foreach ($b in 'msedge.exe', 'chrome.exe') {
        $k = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$b"
        if (Test-Path $k) {
            $p = (Get-ItemProperty $k).'(default)'
            if ($p -and (Test-Path $p)) { return $p }
        }
    }
    return $null
}

# --- 0. Restart a STALE server ---------------------------------------------
# When you run from source, an already-running server keeps serving the OLD code
# until it's restarted — so edits/updates don't show up. We detect this safely:
# /health reports when the server started; if any source file is newer than that,
# the running server is stale, so we stop it and let step 1 start a fresh one.
# Only fires when code genuinely changed, so a current/in-use server is untouched.
function Get-ServerStartedAt {
    try {
        $j = (Invoke-WebRequest $Health -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
        if ($j.started_at) { return [datetimeoffset]::Parse($j.started_at).UtcDateTime }
    } catch {}
    return $null
}
function Get-NewestSourceTime {
    try {
        $m = Get-ChildItem (Join-Path $Root 'nexus') -Recurse -Include *.py, *.html -ErrorAction SilentlyContinue |
             Measure-Object -Property LastWriteTimeUtc -Maximum
        if ($m.Maximum) { return [datetime]$m.Maximum }
    } catch {}
    return $null
}
if (Test-ServerUp) {
    $startedAt = Get-ServerStartedAt
    $newest = Get-NewestSourceTime
    # 5s margin so clock/precision jitter never triggers a needless restart.
    if ($startedAt -and $newest -and ($newest -gt $startedAt.AddSeconds(5))) {
        try {
            (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess |
                Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
        } catch {}
        for ($i = 0; $i -lt 10; $i++) { if (-not (Test-ServerUp)) { break }; Start-Sleep -Milliseconds 300 }
    }
}

# --- 1. Start the server if needed -----------------------------------------
# We only ever start a server when /health is not already answering, so repeated
# launches never stack multiple servers. When we do start one, we remember its
# exact PID so shutdown can target that process and nothing else.
$ServerPid = $null
if (-not (Test-ServerUp)) {
    $pyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $pyw)) { $pyw = Join-Path $Root '.venv\Scripts\python.exe' }
    if (-not (Test-Path $pyw)) {
        # No virtual-env found — fall back to opening whatever may be running.
        Start-Process $Url; return
    }
    if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }

    $srvArgs = @('-m','uvicorn','nexus.web.app:app','--host','127.0.0.1','--port','8000')
    # Capture the server's output to a launcher-specific log. A dedicated name
    # (not server.log) avoids colliding with a manual run's open file handle.
    # If the redirect can't be set up for any reason, fall back to starting
    # without it so the app still launches. -PassThru gives us the PID to track.
    try {
        $srv = Start-Process -FilePath $pyw -ArgumentList $srvArgs `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $DataDir 'nexus-launcher.log') `
            -RedirectStandardError  (Join-Path $DataDir 'nexus-launcher.err')
    } catch {
        $srv = Start-Process -FilePath $pyw -ArgumentList $srvArgs `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    }
    if ($srv) { $ServerPid = $srv.Id }

    # Wait for the server to answer (up to ~30s on a cold first start).
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-ServerUp) { break }
        Start-Sleep -Milliseconds 750
    }
}

# --- 2. Open the chromeless app window --------------------------------------
$browser = Find-Browser
$proc = $null
if ($browser) {
    $bargs = @(
        "--app=$Url",
        "--user-data-dir=$Profile",
        '--window-size=1440,900',
        '--no-first-run',
        '--no-default-browser-check'
    )
    $proc = Start-Process -FilePath $browser -ArgumentList $bargs -PassThru
} else {
    # No Chromium browser found: open in the default browser (a normal tab).
    Start-Process $Url
}

# --- 3. When the LAST app window closes, stop the server we started ---------
# Only the launch that actually started the server ($ServerPid set) is allowed
# to stop it, and only once no Nexus app windows remain. This prevents the
# previous failure mode where closing one window tore down a server that another
# still-open window depended on (ERR_CONNECTION_REFUSED).
if ($proc -and $ServerPid) {
    Wait-Process -Id $proc.Id -ErrorAction SilentlyContinue
    # The spawned browser process may exit immediately if Chromium delegated to
    # an existing instance; in that case other windows are still open and the
    # check below correctly keeps the server alive. Give the OS a moment to
    # reflect a genuine close before counting.
    Start-Sleep -Milliseconds 600
    if ((Count-AppWindows) -eq 0) {
        Stop-Process -Id $ServerPid -Force -ErrorAction SilentlyContinue
    }
}
