# Creates a "Nexus OSINT" shortcut on the Desktop that launches the app
# silently (via Nexus.vbs) with the custom icon. Called by setup.ps1 (or run
# directly); safe to run again — it just overwrites the existing shortcut.

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$icon = Join-Path (Split-Path -Parent $here) 'nexus.ico'  # shared asset in launcher/

$ws      = New-Object -ComObject WScript.Shell
$desktop = $ws.SpecialFolders('Desktop')
$lnk     = $ws.CreateShortcut((Join-Path $desktop 'Nexus OSINT.lnk'))

$lnk.TargetPath       = (Join-Path $env:WINDIR 'System32\wscript.exe')
$lnk.Arguments        = '"' + (Join-Path $here 'Nexus.vbs') + '"'
$lnk.WorkingDirectory = $here
$lnk.IconLocation     = $icon + ',0'
$lnk.Description       = 'Open Nexus-OSINT'
$lnk.Save()

Write-Host ""
Write-Host "  Done! Look for the 'Nexus OSINT' icon on your Desktop."
Write-Host "  Double-click it any time to open the app."
