Nexus-OSINT — desktop launchers
===============================

Pick the folder for your operating system. Each one installs everything and
gives you a clickable "Nexus OSINT" icon — like a normal desktop program.

  Windows  ->  open the  "windows"  folder, then double-click
               "Install Nexus (Windows).bat"

  Linux    ->  run in a terminal from the project folder:
               bash launcher/linux/install.sh

Each installer:
  * sets up Python and the app's dependencies (one time),
  * generates the app icon,
  * adds a "Nexus OSINT" icon to your Desktop / applications menu.

After that, just click the icon. It opens a clean app window (no address bar,
no tabs), refreshes your latest results automatically, and shuts the app down
when you close the window. Everything runs locally on 127.0.0.1.

Shared files in this folder:
  - make_icon.py  : generates the icons below
  - nexus.ico     : Windows icon
  - nexus.png     : Linux icon

See windows/README.txt or linux/README.txt for full details.
