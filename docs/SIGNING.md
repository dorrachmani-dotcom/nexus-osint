# Code signing (Windows)

Unsigned installers trigger Windows SmartScreen warnings. The build supports
optional signing with **Azure Trusted Signing**. Nothing here is required: with
no configuration, builds work exactly as before and produce unsigned files.

The repository contains no account, certificate or secret. Everything below is a
one-time setup you do yourself in the Azure portal.

## What is wired up

- `installer/sign.ps1` signs the files you pass it with `signtool` and the Azure
  Trusted Signing dlib, then verifies the signature. It reads three environment
  variables; if none is set it prints a message and exits successfully (unsigned
  build). If only some are set, or a tool is missing, it fails on purpose.
- `installer/nexus.iss` has an opt-in `SignTool` hook, active only when ISCC is
  run with `/DSignWithScript`. It also signs the uninstaller.

## One-time setup

1. **Azure subscription.** Create or use a paid Azure subscription. The service
   is billed per month; check current Azure pricing before you start.
2. **Create a Trusted Signing account** in the Azure portal (search for
   "Trusted Signing accounts"). Pick a region and note its **endpoint URL**
   (for example `https://eus.codesigning.azure.net`) and the **account name**.
3. **Identity validation.** In the account, create an identity validation
   (individual or organization) and submit the requested documents. Approval is
   done by Microsoft and can take days. You cannot sign until it is approved.
4. **Certificate profile.** After approval, create a *Public Trust* certificate
   profile linked to that validation. Note the **profile name**.
5. **Permissions.** Assign yourself (or the service principal you sign with) the
   role **Trusted Signing Certificate Profile Signer** on the account.
6. **Sign-in for the build machine.** Either run `az login` (Azure CLI) as that
   user, or set the standard Azure variables for a service principal
   (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`). Keep these out
   of the repository and out of shell history files.
7. **Install the tools** on the build machine:
   - Windows SDK (provides `signtool.exe`, version 10.0.2261 or newer).
   - The Trusted Signing client tools (provides `Azure.CodeSigning.Dlib.dll`);
     see the "Trusted Signing" quickstart in Microsoft's documentation for the
     current installer or NuGet package.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `NEXUS_SIGN_ENDPOINT` | yes | Endpoint URL of your Trusted Signing region |
| `NEXUS_SIGN_ACCOUNT` | yes | Trusted Signing account name |
| `NEXUS_SIGN_PROFILE` | yes | Certificate profile name |
| `NEXUS_SIGN_DLIB` | no | Full path to `Azure.CodeSigning.Dlib.dll` if not auto-found |
| `NEXUS_SIGN_SIGNTOOL` | no | Full path to `signtool.exe` if not on PATH |
| `NEXUS_SIGN_TIMESTAMP` | no | Timestamp server (default `http://timestamp.acs.microsoft.com`) |

## Building a signed release

From the repository root, in PowerShell, with the variables above set:

```powershell
# 1) Build the app folder as usual.
pyinstaller installer/nexus.spec --noconfirm --distpath installer/dist --workpath installer/build

# 2) Sign the main executable before it is packaged.
.\installer\sign.ps1 installer\dist\Nexus\Nexus.exe

# 3) Build the installer; ISCC signs Nexus-Setup.exe and the uninstaller.
$cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File $q' + "$PWD\installer\sign.ps1" + '$q $f'
ISCC /DSignWithScript "/Snexussign=$cmd" installer
exus.iss
```

`$q` and `$f` are Inno Setup placeholders (a quote character and the file to sign);
keep them in single quotes so PowerShell does not expand them.

A normal build (no `/DSignWithScript`, no variables) is unchanged and unsigned.

## Check the result

```powershell
signtool verify /pa /v installer\Output\Nexus-Setup.exe
```

or right-click the file, Properties, Digital Signatures.

## Notes

- Only `Nexus.exe` and the installer are signed. DLLs and the bundled Chromium
  inside `_internal\` are not; that does not affect the SmartScreen reputation of
  the installer.
- SmartScreen reputation with a Trusted Signing certificate builds with
  downloads; a brand-new publisher can still see a warning for a short time.
- Certificates from this service are short-lived by design; always keep the
  timestamp option on so signatures stay valid after the certificate expires.
