Nexus-OSINT — desktop launcher (Windows)
========================================

This folder turns Nexus into a one-click desktop app on Windows.

SETUP (do this once)
--------------------
Double-click  "Install Nexus (Windows).bat"
It sets up everything (Python environment + dependencies + icon) and puts a
"Nexus OSINT" icon on your Desktop. That's all.

(It needs Python 3 installed once. If it tells you Python is missing, get it
from https://www.python.org/downloads/ — tick "Add Python to PATH" during
install — then double-click the installer again.)

EVERY DAY
---------
Double-click the "Nexus OSINT" icon on your Desktop.
  - It opens in its own clean window (no address bar, no browser tabs) —
    it looks and feels like a normal program, not a web page.
  - Each time it opens it automatically refreshes your topics with the
    latest results, so you always see current information.
  - When you close the window, the app shuts down in the background.

That's it. No commands, no terminal.

NOTES
-----
- First open after a restart can take a few extra seconds while the engine
  warms up — the window appears as soon as it's ready.
- Everything runs only on your own computer (127.0.0.1). Nothing about what
  you look at is sent anywhere except the public searches you choose to run.
- Uses Microsoft Edge or Google Chrome for the app window (one of them ships
  with Windows). If neither is found, it opens in your default browser.

FILES IN THIS FOLDER (you don't need to touch these)
----------------------------------------------------
- Install Nexus (Windows).bat : one-time setup (run once)
- setup.ps1                   : does the full install (env + deps + icon)
- install_shortcut.ps1        : creates the Desktop shortcut
- Nexus.vbs                   : starts the app silently
- launch_nexus.ps1            : the launcher logic

The icon image (nexus.ico) and its generator (make_icon.py) live one level up,
in launcher/, and are shared with the Linux launcher.
