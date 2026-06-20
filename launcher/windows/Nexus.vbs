' Silent launcher for Nexus-OSINT.
' Runs launch_nexus.ps1 fully hidden (no console window flashes on screen),
' so double-clicking the desktop icon behaves like opening a native app.
Dim sh, fso, here
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & here & "\launch_nexus.ps1""", 0, False
