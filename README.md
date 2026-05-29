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

## Wayland support (KDE Plasma 6 / Kubuntu 26.04)

Kubuntu 26.04 uses Wayland by default. BlueProximity detects this automatically
and adjusts its default commands accordingly.

### Commands that change on Wayland

| Purpose | X11 command | Wayland command |
|---------|-------------|-----------------|
| Keep screen awake | `xset dpms force on` | `qdbus6 org.kde.screensaver /ScreenSaver org.freedesktop.ScreenSaver.SimulateUserActivity` |
| Lock screen | `loginctl lock-session` | `loginctl lock-session` *(unchanged)* |
| Unlock screen | `loginctl unlock-session` | see below |

The proximity command dropdown in the Preferences window lists Wayland-compatible options.  
If you have an existing config that still uses `xset`, a warning is written to the log at startup.

### Unlocking on Wayland — important note

Wayland's security model restricts programmatic unlocking.  
`loginctl unlock-session` **works** in most cases — KDE's kscreenlocker listens
for the logind `Unlock` D-Bus signal and dismisses the lock screen when it
receives it, as long as the session was locked *via logind* (i.e. by
BlueProximity's lock command).

If you find it does not work (common when the screen was already locked by
KDE's own idle timer before BlueProximity fired), use the PAM-based approach
below which is the architecturally correct Wayland solution.

### PAM-based auto-unlock (fully automatic on Wayland)

This replaces the password prompt with a Bluetooth proximity check.
When your phone is in range, the lock screen authenticates and dismisses itself
without any key press.

**1. Create the helper script**

```bash
sudo tee /usr/local/bin/blueproximity-pam-auth > /dev/null << 'EOF'
#!/usr/bin/env python3
"""
PAM exec helper for BlueProximity.
Returns 0 (auth success) when the configured Bluetooth device is in RSSI range.
Reads the first *.conf found in ~/.blueproximity/.
"""
import os, sys, glob, dbus

conf_dir = os.path.expanduser('~/.blueproximity')
confs = glob.glob(os.path.join(conf_dir, '*.conf'))
if not confs:
    sys.exit(1)

mac = ''
unlock_dist = 40   # default |RSSI| threshold
for line in open(confs[0]):
    k, _, v = line.partition('=')
    k, v = k.strip(), v.strip().strip('"')
    if k == 'device_mac':
        mac = v
    elif k == 'unlock_distance':
        try:
            unlock_dist = int(v)
        except ValueError:
            pass

if not mac:
    sys.exit(1)

try:
    bus = dbus.SystemBus()
    dev_path = '/org/bluez/hci0/dev_' + mac.replace(':', '_').upper()
    props = dbus.Interface(bus.get_object('org.bluez', dev_path),
                           'org.freedesktop.DBus.Properties')
    rssi = int(props.Get('org.bluez.Device1', 'RSSI'))
    # rssi is negative dBm; device is "close enough" when |rssi| < unlock_dist
    sys.exit(0 if -rssi <= unlock_dist else 1)
except Exception:
    sys.exit(1)
EOF
sudo chmod +x /usr/local/bin/blueproximity-pam-auth
```

**2. Edit the kscreenlocker PAM config**

```bash
sudo cp /etc/pam.d/kscreenlocker /etc/pam.d/kscreenlocker.bak
```

Open `/etc/pam.d/kscreenlocker` in an editor and add this line **before** the
existing `auth` lines:

```
auth    sufficient    pam_exec.so    /usr/local/bin/blueproximity-pam-auth
```

The `sufficient` keyword means: if the script returns 0 the entire auth stack
succeeds immediately; if it returns 1 PAM falls through to the password prompt
as normal.

**3. Set the unlock command to a no-op**

In the BlueProximity Preferences, set the **Unlocking command** to an empty
string (or `true`). With PAM handling unlock, you do not need BlueProximity to
call `loginctl unlock-session` — the lock screen will auto-dismiss as soon as
the user's next interaction (or on a short polling interval configured in KDE's
idle settings).

> **Security note:** `pam_exec.so sufficient` means anyone who can write to the
> helper script path can bypass the lock screen. Ensure
> `/usr/local/bin/blueproximity-pam-auth` is owned by root and not
> world-writable.

---

## Logging

Enable file logging on the **Proximity Details** tab. The default log path is `~/.blueproximity/blueprox.log`.

The log records:

- Startup (display server, adapter path, configured device)
- Device appearing and disappearing (with RSSI)
- RSSI and state every 60 seconds
- State transitions (active → gone, gone → active)
- Lock and unlock command execution and any errors
- Wayland-specific warnings if X11-only commands are configured
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
