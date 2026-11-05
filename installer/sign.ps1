<#
.SYNOPSIS
  Optionally code-sign one or more files with Azure Trusted Signing.

.DESCRIPTION
  Used for installer\dist\Nexus\Nexus.exe (before packaging) and
  installer\Output\Nexus-Setup.exe (via the Inno Setup SignTool hook).
  See docs\SIGNING.md for the one-time account setup.

  Configuration comes only from environment variables, never from the repo:
    NEXUS_SIGN_ENDPOINT   Trusted Signing endpoint URL for your region
    NEXUS_SIGN_ACCOUNT    Trusted Signing account name
    NEXUS_SIGN_PROFILE    certificate profile name
    NEXUS_SIGN_DLIB       (optional) full path to Azure.CodeSigning.Dlib.dll
    NEXUS_SIGN_SIGNTOOL   (optional) full path to signtool.exe
    NEXUS_SIGN_TIMESTAMP  (optional) RFC 3161 timestamp URL
  Azure credentials are read by the signing library itself from the usual Azure
  environment variables or from `az login`; this script never sees a secret.

  If none of the three required variables is set the script prints a message and
  exits 0, so a normal build still produces an unsigned installer. If only some
  are set, or the tools are missing, it fails (you asked for signing).

.EXAMPLE
  .\installer\sign.ps1 installer\dist\Nexus\Nexus.exe
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]] $Path
)

$ErrorActionPreference = 'Stop'

$required = 'NEXUS_SIGN_ENDPOINT', 'NEXUS_SIGN_ACCOUNT', 'NEXUS_SIGN_PROFILE'
$set = @($required | Where-Object { [Environment]::GetEnvironmentVariable($_) })

if ($set.Count -eq 0) {
    Write-Host "[sign] NEXUS_SIGN_* not set - skipping code signing (unsigned build): $($Path -join ', ')"
    exit 0
}
if ($set.Count -ne $required.Count) {
    $missing = $required | Where-Object { $_ -notin $set }
    Write-Error "[sign] Incomplete signing configuration. Missing: $($missing -join ', ')"
    exit 1
}

function Find-SignTool {
    if ($env:NEXUS_SIGN_SIGNTOOL) { return $env:NEXUS_SIGN_SIGNTOOL }
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path $kits) {
        $hit = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Find-Dlib {
    if ($env:NEXUS_SIGN_DLIB) { return $env:NEXUS_SIGN_DLIB }
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\MicrosoftTrustedSigningClientTools'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\MicrosoftArtifactSigningClientTools')
    )
    foreach ($r in $roots) {
        $f = Join-Path $r 'Azure.CodeSigning.Dlib.dll'
        if (Test-Path $f) { return $f }
    }
    return $null
}

$signtool = Find-SignTool
if (-not $signtool -or -not (Test-Path $signtool)) {
    Write-Error '[sign] signtool.exe not found. Install the Windows SDK or set NEXUS_SIGN_SIGNTOOL.'
    exit 1
}
$dlib = Find-Dlib
if (-not $dlib -or -not (Test-Path $dlib)) {
    Write-Error '[sign] Azure.CodeSigning.Dlib.dll not found. Install the Trusted Signing client tools or set NEXUS_SIGN_DLIB (see docs\SIGNING.md).'
    exit 1
}
$timestamp = if ($env:NEXUS_SIGN_TIMESTAMP) { $env:NEXUS_SIGN_TIMESTAMP } else { 'http://timestamp.acs.microsoft.com' }

# Metadata file: only non-secret identifiers. Written to a temp file, removed after.
$meta = Join-Path ([IO.Path]::GetTempPath()) ("nexus-sign-{0}.json" -f [guid]::NewGuid().ToString('N'))
@{
    Endpoint               = $env:NEXUS_SIGN_ENDPOINT
    CodeSigningAccountName = $env:NEXUS_SIGN_ACCOUNT
    CertificateProfileName = $env:NEXUS_SIGN_PROFILE
} | ConvertTo-Json | Set-Content -Path $meta -Encoding ascii

try {
    foreach ($file in $Path) {
        $file = $file.Trim('"')
        if (-not (Test-Path $file)) { throw "[sign] File not found: $file" }
        Write-Host "[sign] Signing $file"
        & $signtool sign /v /fd SHA256 /tr $timestamp /td SHA256 /dlib $dlib /dmdf $meta $file
        if ($LASTEXITCODE -ne 0) { throw "[sign] signtool sign failed (exit $LASTEXITCODE) for $file" }
        & $signtool verify /pa $file
        if ($LASTEXITCODE -ne 0) { throw "[sign] signature verification failed for $file" }
    }
}
finally {
    Remove-Item $meta -Force -ErrorAction SilentlyContinue
}
