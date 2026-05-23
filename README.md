# BlueProximity Qt6

A desktop proximity daemon that locks and unlocks your screen automatically based on the distance of a paired Bluetooth device — typically your mobile phone.

When you walk away, your screen locks. When you come back, it unlocks. No interaction required.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Qt](https://img.shields.io/badge/Qt-6-green)
![License](https://img.shields.io/badge/license-GPL%20v2-orange)

---

## How it works

BlueProximity keeps your Bluetooth adapter in continuous discovery mode and reads the RSSI (signal strength) of your chosen device once per second. When the signal drops below a configurable threshold for a set number of seconds, it runs your lock command. When the signal recovers above a second threshold, it runs your unlock command.

All thresholds, durations, and commands are configurable through the preferences window.

---

## Requirements

- Kubuntu 26.04 / Ubuntu 26.04 or similar (BlueZ 5.x, D-Bus)
- Python 3.10+
- PyQt6
- dbus-python
- configobj

Install dependencies:

```sh
sudo apt install python3-dbus python3-pip
pip3 install PyQt6 configobj
```

For channel scanning (optional):

```sh
sudo apt install bluez-tools
```

---

## Running

```sh
cd blueproximity-qt6
python3 proximity.py
```

Or use the included launcher script (useful for adding to session startup):

```sh
./start_proximity.sh
```

The application runs as a system tray icon. Right-click the tray icon to access Preferences, Pause, or Quit.

---

## First-time setup

1. Launch the application — the Preferences window opens automatically on first run.
2. On the **Bluetooth Device** tab, click **Scan for devices**. Scanning runs for ~10 seconds and lists all nearby Bluetooth devices.
3. Select your device and click **Use selected device** — this fills in the MAC address field.
4. Switch to the **Proximity Details** tab and adjust the lock/unlock distance and duration sliders to suit your environment.
5. Click **Apply** to save.

The tray icon changes colour to reflect the current state:

| Icon | Meaning |
|------|---------|
| Blue (base) | Device detected, screen unlocked |
| Yellow (attention) | Device detected but signal weak |
| Red (no connection) | Device not visible, screen locked |
| Grey (error) | No device configured |
| Pause | Monitoring paused |

---

## Configuration

Configuration files are stored in `~/.blueproximity/` as `.conf` files. Multiple configurations (one per device) are supported — use the **Selected Configuration** drop-down to switch between them.

Key settings:

| Setting | Description |
|---------|-------------|
| Lock distance | RSSI threshold below which locking begins (higher = closer required) |
| Lock duration | Seconds the signal must stay below threshold before locking |
| Unlock distance | RSSI threshold above which unlocking begins |
| Unlock duration | Seconds the signal must stay above threshold before unlocking |
| Lock command | Shell command to run when locking (default: `loginctl lock-session`) |
| Unlock command | Shell command to run when unlocking (default: `loginctl unlock-session`) |
| Proximity command | Command run periodically while device is near (e.g. suppress screensaver) |
| Command interval | How often the proximity command fires (seconds) |

**Note on RSSI values:** RSSI is measured in negative dBm. A value of `-50` is closer than `-90`. The sliders show absolute values (50, 90) — higher numbers mean the device must be physically closer before the action triggers.

---

## Logging

Enable file logging on the **Proximity Details** tab. The default log path is `~/.blueproximity/blueprox.log`.

The log records:

- Startup (adapter path, configured device)
- Device appearing and disappearing (with RSSI)
- RSSI and state every 60 seconds
- State transitions (active → gone, gone → active)
- Lock and unlock command execution
- Clean shutdown

---

## Project history

This is a Qt6 port of the original BlueProximity, which was written by Lars Friedrichs in 2007 and later ported to Python 3 / GTK3 by Rodrigo Gambra-Middleton. This fork replaces the GTK3 interface with PyQt6 and updates the Bluetooth backend to use the modern BlueZ 5 D-Bus API, removing the dependency on PyBluez.

---

## Acknowledgements

1. Lars Friedrichs — original author
2. Tobias Jakobs — GUI optimisations
3. Zsolt Mazolt — GUI and KDE contributions
4. Rodrigo Gambra-Middleton — Python 3 / GTK3 port

---

## License

Distributed under the GNU General Public License v2. See `COPYING` for details.

    BlueProximity — lock/unlock your screen based on Bluetooth proximity.
    Copyright (C) 2007 Lars Friedrichs <larsfriedrichs@gmx.de>

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
