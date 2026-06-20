Nexus-OSINT — desktop launcher (Linux)
======================================

This folder turns Nexus into a one-click desktop app on Linux.

SETUP (do this once)
--------------------
Open a terminal in the project folder and run:

    bash launcher/linux/install.sh

It sets up everything (Python environment + dependencies + icon) and adds a
"Nexus OSINT" entry to your applications menu and your Desktop. That's all.

EVERY DAY
---------
Click the "Nexus OSINT" icon (applications menu or Desktop).
  - It opens in its own clean window (no address bar, no browser tabs) —
    it looks and feels like a normal program, not a web page.
  - Each time it opens it automatically refreshes your topics with the
    latest results, so you always see current information.
  - When you close the window, the app shuts down in the background.

That's it. No commands after the one-time setup.

NOTES
-----
- First open after a restart can take a few extra seconds while the engine
  warms up — the window appears as soon as it's ready.
- Everything runs only on your own computer (127.0.0.1). Nothing about what
  you look at is sent anywhere except the public searches you choose to run.
- Uses Google Chrome, Chromium, or Microsoft Edge for the app window. If none
  is found, it opens in your default browser (a normal tab).
- Needs Python 3.10+ with the venv module. On Debian/Ubuntu you may first need:
      sudo apt install python3 python3-venv python3-pip

FILES IN THIS FOLDER (you don't need to touch these)
----------------------------------------------------
- install.sh        : one-time setup (creates the icon)
- launch_nexus.sh   : the launcher logic
- README.txt        : this file

The icon image (nexus.png) and its generator (make_icon.py) live one level up,
in launcher/, and are shared with the Windows launcher.
