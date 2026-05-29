#!/usr/bin/env python3
# coding: utf-8

import os
import sys
import time
import struct
import signal
import threading
import gettext
import locale
import syslog
import subprocess

from configobj import ConfigObj
from validate import Validator

try:
    import dbus
    import dbus.bus
except ImportError:
    print("Please install dbus-python: sudo apt-get install python3-dbus")
    sys.exit(1)

from PyQt6.QtWidgets import (QApplication, QMainWindow, QMessageBox,
                             QSystemTrayIcon, QMenu, QFileDialog, QHeaderView)
from PyQt6.QtGui import QIcon, QAction, QStandardItemModel, QStandardItem
from PyQt6.QtCore import QTimer, pyqtSignal, QObject, Qt
from PyQt6 import uic

APP_NAME = "blueproximity"
SW_VERSION = '1.4.0-qt6'
dist_path = os.path.dirname(os.path.realpath(__file__)) + '/'

icon_base = 'blueproximity_base.svg'
icon_att = 'blueproximity_attention.svg'
icon_away = 'blueproximity_nocon.svg'
icon_error = 'blueproximity_error.svg'
icon_pause = 'blueproximity_pause.svg'

# Detect the display server so we can choose appropriate shell commands.
# WAYLAND_DISPLAY is set by the compositor; DISPLAY is set by Xorg.
IS_WAYLAND = bool(os.environ.get('WAYLAND_DISPLAY'))

# Pre-populated suggestions shown in the command combo boxes.
# The lock command is the same on both display servers.
_CMD_LOCK = [
    'loginctl lock-session',
    'qdbus6 org.kde.screensaver /ScreenSaver org.freedesktop.ScreenSaver.Lock',
    'xdg-screensaver lock',
]

# Wayland: loginctl unlock-session is the first thing to try.
# qdbus6 SetActive false works on some KDE builds.
# NOTE: KDE Plasma 6 Wayland may require PAM-based auth for a fully automatic
# unlock — see the README for the pam_exec setup instructions.
_CMD_UNLOCK_WAYLAND = [
    'loginctl unlock-session',
    'qdbus6 org.kde.screensaver /ScreenSaver org.freedesktop.ScreenSaver.SetActive false',
    'dbus-send --session --dest=org.freedesktop.ScreenSaver /ScreenSaver '
        'org.freedesktop.ScreenSaver.SetActive boolean:false',
]
_CMD_UNLOCK_X11 = [
    'loginctl unlock-session',
    'xdg-screensaver reset',
    'gnome-screensaver-command --deactivate',
]

# On Wayland, keep the screen alive via SimulateUserActivity.
# On X11, xset dpms force on works the same way.
_CMD_PROXI_WAYLAND = [
    'qdbus6 org.kde.screensaver /ScreenSaver '
        'org.freedesktop.ScreenSaver.SimulateUserActivity',
    'dbus-send --session --dest=org.freedesktop.ScreenSaver /ScreenSaver '
        'org.freedesktop.ScreenSaver.SimulateUserActivity',
    '',
]
_CMD_PROXI_X11 = [
    'xset dpms force on',
    'xset s reset',
    '',
]

# Choose the right defaults for brand-new config files.
_default_unlock_cmd  = _CMD_UNLOCK_WAYLAND[0]  if IS_WAYLAND else _CMD_UNLOCK_X11[0]
_default_proxi_cmd   = _CMD_PROXI_WAYLAND[0]   if IS_WAYLAND else _CMD_PROXI_X11[0]

conf_specs = [
    'device_mac=string(max=17,default="")',
    'device_channel=integer(1,30,default=7)',
    'lock_distance=integer(0,127,default=60)',
    'lock_duration=integer(0,120,default=6)',
    'unlock_distance=integer(0,127,default=40)',
    'unlock_duration=integer(0,120,default=1)',
    'lock_command=string(default="loginctl lock-session")',
    f'unlock_command=string(default="{_default_unlock_cmd}")',
    f'proximity_command=string(default="{_default_proxi_cmd}")',
    'proximity_interval=integer(5,600,default=60)',
    'buffer_size=integer(1,255,default=1)',
    'log_to_syslog=boolean(default=True)',
    'log_syslog_facility=string(default="local7")',
    'log_to_file=boolean(default=False)',
    'log_filelog_filename=string(default="' + os.getenv('HOME') + '/.blueproximity/blueprox.log")'
]

class Logger:
    def __init__(self):
        self.syslogging = False
        self.filelogging = False
        self.syslog_facility = None
        self.filename = ''
        self.flog = None

    def getFacilityFromString(self, facility):
        log_dict = {
            "local0": syslog.LOG_LOCAL0, "local1": syslog.LOG_LOCAL1,
            "local2": syslog.LOG_LOCAL2, "local3": syslog.LOG_LOCAL3,
            "local4": syslog.LOG_LOCAL4, "local5": syslog.LOG_LOCAL5,
            "local6": syslog.LOG_LOCAL6, "local7": syslog.LOG_LOCAL7,
            "user": syslog.LOG_USER
        }
        return log_dict.get(facility, syslog.LOG_USER)

    def enable_syslogging(self, facility):
        self.syslog_facility = self.getFacilityFromString(facility)
        syslog.openlog('blueproximity', syslog.LOG_PID)
        self.syslogging = True

    def disable_syslogging(self):
        self.syslogging = False

    def enable_filelogging(self, filename):
        self.filename = filename
        try:
            self.flog = open(filename, 'a')
            self.filelogging = True
        except:
            self.filelogging = False

    def disable_filelogging(self):
        if self.flog:
            try:
                self.flog.close()
            except: pass
        self.filelogging = False

    def log_line(self, line):
        if self.syslogging:
            syslog.syslog(self.syslog_facility | syslog.LOG_NOTICE, line)
        if self.filelogging:
            try:
                self.flog.write(time.ctime() + " blueproximity: " + line + "\n")
                self.flog.flush()
            except:
                self.disable_filelogging()

    @staticmethod
    def _as_bool(value):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == 'true'

    def configureFromConfig(self, config):
        if self._as_bool(config['log_to_syslog']):
            self.enable_syslogging(config['log_syslog_facility'])
        else:
            self.disable_syslogging()
        if self._as_bool(config['log_to_file']):
            if self.filelogging and config['log_filelog_filename'] != self.filename:
                self.disable_filelogging()
            if not self.filelogging:
                self.enable_filelogging(config['log_filelog_filename'])
        else:
            self.disable_filelogging()

class Proximity(QObject, threading.Thread):
    # Signals are the only thread-safe way to invoke slots across a
    # threading.Thread → Qt main-thread boundary.
    _sig_active    = pyqtSignal()
    _sig_gone      = pyqtSignal()
    _sig_proximity = pyqtSignal()

    def __init__(self, config):
        QObject.__init__(self)
        threading.Thread.__init__(self, name="WorkerThread")
        self._sig_active.connect(self.go_active)
        self._sig_gone.connect(self.go_gone)
        self._sig_proximity.connect(self.go_proximity)
        self.config = config
        self.Dist = -255
        self.State = "gone"
        self.Simulate = False
        self.Stop = False
        self.dev_mac = self.config['device_mac']
        self.dev_channel = self.config['device_channel']
        self.ringbuffer_size = self.config['buffer_size']
        self.ringbuffer = [-254] * self.ringbuffer_size
        self.ringbuffer_pos = 0
        self.gone_duration = self.config['lock_duration']
        self.gone_limit = -self.config['lock_distance']
        self.active_duration = self.config['unlock_duration']
        self.active_limit = -self.config['unlock_distance']
        self.ErrorMsg = "Initialized..."
        self.ignoreFirstTransition = True
        self.logger = Logger()
        self.logger.configureFromConfig(self.config)
        # Lock protecting attributes written by the GUI thread and read by run()
        self._state_lock = threading.Lock()
        self.bus = None
        self._adapter_path = "/org/bluez/hci0"
        self._discovery_active = False
        try:
            self.bus = dbus.SystemBus()
        except Exception as e:
            print("Could not connect to dbus:", e)

    def _find_adapter_path(self):
        try:
            manager = dbus.Interface(
                self.bus.get_object("org.bluez", "/"),
                "org.freedesktop.DBus.ObjectManager"
            )
            for path, ifaces in manager.GetManagedObjects().items():
                if "org.bluez.Adapter1" in ifaces:
                    return str(path)
        except Exception:
            pass
        return "/org/bluez/hci0"

    def _start_discovery(self):
        try:
            adapter = dbus.Interface(
                self.bus.get_object("org.bluez", self._adapter_path),
                "org.bluez.Adapter1"
            )
            adapter.StartDiscovery()
            self._discovery_active = True
        except dbus.exceptions.DBusException:
            pass

    def _stop_discovery(self):
        if not self._discovery_active:
            return
        try:
            adapter = dbus.Interface(
                self.bus.get_object("org.bluez", self._adapter_path),
                "org.bluez.Adapter1"
            )
            adapter.StopDiscovery()
        except dbus.exceptions.DBusException:
            pass
        self._discovery_active = False

    def _is_discovering(self):
        """Ask BlueZ whether the adapter is actually scanning right now."""
        try:
            props = dbus.Interface(
                self.bus.get_object("org.bluez", self._adapter_path),
                "org.freedesktop.DBus.Properties"
            )
            return bool(props.Get("org.bluez.Adapter1", "Discovering"))
        except Exception:
            return False

    def _ensure_discovery(self):
        """Restart discovery if BlueZ reports it has stopped (e.g. GUI scan ended)."""
        if not self._is_discovering():
            self.logger.log_line('discovery stopped unexpectedly — restarting')
            self._discovery_active = False
            self._start_discovery()

    def get_proximity_once(self, dev_mac):
        if not self.bus or not dev_mac:
            return -255
        try:
            dev_path = self._adapter_path + "/dev_" + dev_mac.replace(':', '_').upper()
            device = self.bus.get_object("org.bluez", dev_path)
            props = dbus.Interface(device, "org.freedesktop.DBus.Properties")
            rssi = props.Get("org.bluez.Device1", "RSSI")
            return int(rssi)
        except dbus.exceptions.DBusException:
            return -255
        except Exception:
            return -255

    def run_cycle(self, dev_mac):
        self.ringbuffer_pos = (self.ringbuffer_pos + 1) % self.ringbuffer_size
        self.ringbuffer[self.ringbuffer_pos] = self.get_proximity_once(dev_mac)
        ret_val = sum(self.ringbuffer)
        if self.ringbuffer[self.ringbuffer_pos] == -255:
            self.ErrorMsg = "No connection found..."
        else:
            self.ErrorMsg = "Connected"
        return int(ret_val / self.ringbuffer_size)

    def _run_cmd(self, label, cmd):
        """Run a shell command and log any failure. Runs in the calling thread."""
        if not cmd:
            return
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                msg = f'{label} command returned {result.returncode}'
                if result.stderr.strip():
                    msg += f': {result.stderr.strip()}'
                self.logger.log_line(msg)
        except subprocess.TimeoutExpired:
            self.logger.log_line(f'{label} command timed out after 5 s')
        except Exception as e:
            self.logger.log_line(f'{label} command error: {e}')

    def go_active(self):
        if self.ignoreFirstTransition:
            self.ignoreFirstTransition = False
        else:
            self.logger.log_line('screen is unlocked')
            self._run_cmd('unlock', self.config['unlock_command'])

    def go_gone(self):
        if self.ignoreFirstTransition:
            self.ignoreFirstTransition = False
        else:
            self.logger.log_line('screen is locked')
            self._run_cmd('lock', self.config['lock_command'])

    def go_proximity(self):
        # Fire-and-forget — proximity command may be long-running (e.g. a script).
        cmd = self.config['proximity_command']
        if cmd:
            subprocess.Popen(cmd, shell=True)

    def run(self):
        display_server = 'Wayland' if IS_WAYLAND else 'X11'
        if self.bus:
            self._adapter_path = self._find_adapter_path()
            self._start_discovery()
            self.logger.log_line(
                f'started. display={display_server} adapter={self._adapter_path}'
                f' device={self.dev_mac or "not configured"}'
            )
        else:
            self.logger.log_line(
                f'started. display={display_server} WARNING: no D-Bus connection, Bluetooth unavailable'
            )

        if IS_WAYLAND:
            proxi_cmd = self.config.get('proximity_command', '')
            if 'xset' in proxi_cmd:
                self.logger.log_line(
                    'WARNING: proximity_command uses xset which is X11-only. '
                    'On Wayland use: qdbus6 org.kde.screensaver /ScreenSaver '
                    'org.freedesktop.ScreenSaver.SimulateUserActivity'
                )
            self.logger.log_line(
                'Wayland note: if loginctl unlock-session does not dismiss the '
                'lock screen, see README for PAM-based auto-unlock setup.'
            )

        duration_count = 0
        state = "gone"
        proxiCmdCounter = 0
        discovery_check_counter = 0
        discovery_restart_counter = 0
        rssi_log_counter = 0
        last_rssi = None

        while not self.Stop:
            try:
                # --- Discovery maintenance ---
                # Every 10 s: check BlueZ is still discovering and restart if not.
                # Recovers quickly when the GUI scan's StopDiscovery kills our session.
                discovery_check_counter += 1
                if discovery_check_counter >= 10 and self.bus:
                    discovery_check_counter = 0
                    self._ensure_discovery()

                # Every 30 s: force a full stop+start cycle even if discovery appears
                # to be running. This forces BlueZ to begin a fresh scan epoch so it
                # removes the cached RSSI property for any device it no longer sees —
                # which is the only reliable way to detect a device whose Bluetooth
                # has been switched off (stale RSSI fix).
                discovery_restart_counter += 1
                if discovery_restart_counter >= 30 and self.bus:
                    discovery_restart_counter = 0
                    self._stop_discovery()
                    self._start_discovery()

                # --- Snapshot volatile attributes written by the GUI thread ---
                with self._state_lock:
                    dev_mac       = self.dev_mac
                    gone_limit    = self.gone_limit
                    active_limit  = self.active_limit
                    gone_duration = self.gone_duration
                    active_dur    = self.active_duration
                    simulate      = self.Simulate

                if dev_mac != "":
                    dist = self.run_cycle(dev_mac)
                else:
                    dist = -255
                    self.ErrorMsg = "No bluetooth device configured..."

                # --- Logging ---
                rssi_log_counter += 1
                device_visible = dist != -255
                prev_visible = last_rssi is not None and last_rssi != -255
                if device_visible != prev_visible:
                    if device_visible:
                        self.logger.log_line(f'device {dev_mac} found, RSSI={dist}')
                    else:
                        self.logger.log_line(f'device {dev_mac} lost')
                elif rssi_log_counter >= 60:
                    rssi_log_counter = 0
                    if device_visible:
                        self.logger.log_line(f'RSSI={dist} state={state}')
                    else:
                        self.logger.log_line(f'device not visible, state={state}')
                last_rssi = dist

                # --- State machine ---
                # Device completely invisible (dist==-255) is always treated as gone,
                # regardless of gone_limit, as a belt-and-suspenders guard.
                definitely_gone = (dist == -255)

                if state == "gone":
                    if not definitely_gone and dist >= active_limit:
                        duration_count += 1
                        if duration_count >= active_dur:
                            state = "active"
                            duration_count = 0
                            self.logger.log_line(f'state -> active (RSSI={dist})')
                            if not simulate:
                                self._sig_active.emit()
                    else:
                        duration_count = 0
                else:  # active
                    if definitely_gone or dist <= gone_limit:
                        duration_count += 1
                        if duration_count >= gone_duration:
                            state = "gone"
                            proxiCmdCounter = 0
                            duration_count = 0
                            self.logger.log_line(f'state -> gone (RSSI={dist})')
                            if not simulate:
                                self._sig_gone.emit()
                    else:
                        duration_count = 0
                        proxiCmdCounter += 1

                self.State = state
                self.Dist = dist

                if proxiCmdCounter >= int(self.config['proximity_interval']) and not simulate and self.config['proximity_command']:
                    proxiCmdCounter = 0
                    self._sig_proximity.emit()

                time.sleep(1)
            except KeyboardInterrupt:
                break

        # Always attempt to stop discovery at shutdown regardless of _discovery_active
        # flag, since a failed _start_discovery can leave the flag inconsistent.
        if self.bus:
            try:
                adapter = dbus.Interface(
                    self.bus.get_object("org.bluez", self._adapter_path),
                    "org.bluez.Adapter1"
                )
                adapter.StopDiscovery()
            except dbus.exceptions.DBusException:
                pass
            self._discovery_active = False

class ProximityGUI(QMainWindow):
    def __init__(self, configs, new_config):
        super().__init__()
        self.configs = configs
        self.configname = configs[0][0]
        self.config = configs[0][1]
        self.proxi = configs[0][2]

        uic.loadUi(os.path.join(dist_path, "proximity.ui"), self)

        self.minDist = -255
        self.maxDist = 0
        self.pauseMode = False
        self.gone_live = False

        self.setup_tray_icon()
        self.setup_scan_view()
        self._populate_command_combos()   # must come before readSettings
        self.fillConfigCombo()
        self.readSettings()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.updateState)
        self.timer.start(1000)

        self.gone_live = True

        # Connect scan buttons
        self.btnScan.clicked.connect(self.scanDevices)
        self.btnSelect.clicked.connect(self.selectDevice)
        self.btnScanChannel.clicked.connect(self.scanChannels)

        # Apply button — enabled only when a device MAC is set and settings are dirty
        self.btnApply.clicked.connect(self.applySettings)

        # Reset the live min/max RSSI tracking
        self.btnResetMinMax.clicked.connect(self._resetMinMax)

        # Connect UI signals — changes mark settings dirty but do not auto-save
        self.comboConfig.currentTextChanged.connect(self.comboConfig_changed)
        self.entryMAC.textChanged.connect(self._on_settings_changed)
        self.hscaleLockDist.valueChanged.connect(self._on_settings_changed)
        self.hscaleLockDur.valueChanged.connect(self._on_settings_changed)
        self.hscaleUnlockDist.valueChanged.connect(self._on_settings_changed)
        self.hscaleUnlockDur.valueChanged.connect(self._on_settings_changed)
        self.hscaleProxi.valueChanged.connect(self._on_settings_changed)
        self.checkSyslog.toggled.connect(self._on_settings_changed)
        self.checkFile.toggled.connect(self._on_settings_changed)
        self.entryFile.textChanged.connect(self._on_settings_changed)

        if new_config:
            self.show()

    def setup_scan_view(self):
        self._scan_model = QStandardItemModel(0, 2)
        self._scan_model.setHorizontalHeaderLabels(["Device Name", "MAC Address"])
        self.treeScanResult.setModel(self._scan_model)
        self.treeScanResult.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.treeScanResult.setSelectionMode(self.treeScanResult.SelectionMode.SingleSelection)

        self._chan_model = QStandardItemModel(0, 2)
        self._chan_model.setHorizontalHeaderLabels(["Channel", "Service"])
        self.treeScanChannelResult.setModel(self._chan_model)

        self._scan_timer = None
        self._scan_bus = None
        self._scan_adapter = None
        self._scan_polls = 0

    def _populate_command_combos(self):
        """Fill lock/unlock/proximity combos with platform-appropriate suggestions.
        Called once at startup; readSettings() restores the saved text on top."""
        unlock_cmds = _CMD_UNLOCK_WAYLAND if IS_WAYLAND else _CMD_UNLOCK_X11
        proxi_cmds  = _CMD_PROXI_WAYLAND  if IS_WAYLAND else _CMD_PROXI_X11
        for combo, items in [
            (self.comboLock,   _CMD_LOCK),
            (self.comboUnlock, unlock_cmds),
            (self.comboProxi,  proxi_cmds),
        ]:
            combo.clear()
            for item in items:
                combo.addItem(item)

    def scanDevices(self):
        self.btnScan.setEnabled(False)
        self.btnScan.setText("Scanning...")
        self._scan_model.removeRows(0, self._scan_model.rowCount())

        try:
            # Use a dedicated connection so our StopDiscovery at the end of the
            # scan does not cancel the Proximity thread's StartDiscovery call.
            # dbus.SystemBus() is a singleton shared with the Proximity thread;
            # dbus.bus.BusConnection gives us a separate service name.
            self._scan_bus = dbus.bus.BusConnection(dbus.bus.BusConnection.TYPE_SYSTEM)
            manager = dbus.Interface(
                self._scan_bus.get_object("org.bluez", "/"),
                "org.freedesktop.DBus.ObjectManager"
            )

            # Find adapter
            adapter_path = None
            for path, ifaces in manager.GetManagedObjects().items():
                if "org.bluez.Adapter1" in ifaces:
                    adapter_path = str(path)
                    break

            if not adapter_path:
                QMessageBox.warning(self, "Bluetooth Error", "No Bluetooth adapter found.")
                self._reset_scan_button()
                return

            # Show already-known devices immediately
            for path, ifaces in manager.GetManagedObjects().items():
                if "org.bluez.Device1" in ifaces:
                    props = ifaces["org.bluez.Device1"]
                    name = str(props.get("Name", props.get("Alias", "Unknown")))
                    addr = str(props.get("Address", ""))
                    if addr:
                        self._add_scan_result(name, addr)

            # Start active discovery
            self._scan_adapter = dbus.Interface(
                self._scan_bus.get_object("org.bluez", adapter_path),
                "org.bluez.Adapter1"
            )
            self._scan_adapter.StartDiscovery()
            self._scan_polls = 0

            self._scan_timer = QTimer(self)
            self._scan_timer.timeout.connect(self._poll_scan)
            self._scan_timer.start(2000)

        except dbus.exceptions.DBusException as e:
            QMessageBox.warning(self, "Bluetooth Error", f"Could not start scan:\n{e}")
            self._reset_scan_button()

    def _poll_scan(self):
        self._scan_polls += 1
        try:
            manager = dbus.Interface(
                self._scan_bus.get_object("org.bluez", "/"),
                "org.freedesktop.DBus.ObjectManager"
            )
            for path, ifaces in manager.GetManagedObjects().items():
                if "org.bluez.Device1" in ifaces:
                    props = ifaces["org.bluez.Device1"]
                    name = str(props.get("Name", props.get("Alias", "Unknown")))
                    addr = str(props.get("Address", ""))
                    if addr:
                        self._add_scan_result(name, addr)
        except Exception:
            pass

        if self._scan_polls >= 5:  # 10 seconds total
            self._scan_timer.stop()
            try:
                self._scan_adapter.StopDiscovery()
            except Exception:
                pass
            self._reset_scan_button()

    def _add_scan_result(self, name, addr):
        for row in range(self._scan_model.rowCount()):
            if self._scan_model.item(row, 1).text() == addr:
                if self._scan_model.item(row, 0).text() in ("Unknown", "") and name not in ("Unknown", ""):
                    self._scan_model.item(row, 0).setText(name)
                return
        self._scan_model.appendRow([QStandardItem(name), QStandardItem(addr)])

    def _reset_scan_button(self):
        self.btnScan.setEnabled(True)
        self.btnScan.setText("Scan for devices")

    def selectDevice(self):
        idx = self.treeScanResult.currentIndex()
        if not idx.isValid():
            return
        addr = self._scan_model.item(idx.row(), 1).text()
        self.entryMAC.setText(addr)

    def scanChannels(self):
        mac = self.entryMAC.text().strip()
        if not mac:
            QMessageBox.information(self, "Channel Scan",
                "Enter or select a device MAC address first.")
            return
        try:
            result = subprocess.run(
                ["sdptool", "browse", "--l2cap", mac],
                capture_output=True, text=True, timeout=15
            )
            self._chan_model.removeRows(0, self._chan_model.rowCount())
            channel = None
            service = None
            for line in result.stdout.splitlines():
                if line.startswith("Service Name:"):
                    service = line.split(":", 1)[1].strip()
                elif "Channel:" in line:
                    channel = line.split(":", 1)[1].strip()
                if channel and service:
                    self._chan_model.appendRow([QStandardItem(channel), QStandardItem(service)])
                    channel = None
                    service = None
            if self._chan_model.rowCount() == 0:
                QMessageBox.information(self, "Channel Scan",
                    "No RFCOMM channels found via sdptool.\n"
                    "Try channel 1 (BLE) or 7 (classic SPP).")
        except FileNotFoundError:
            QMessageBox.information(self, "Channel Scan",
                "sdptool not found. Install bluez-tools:\n"
                "  sudo apt install bluez-tools")
        except subprocess.TimeoutExpired:
            QMessageBox.warning(self, "Channel Scan", "Scan timed out.")

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon(os.path.join(dist_path, icon_error)))

        menu = QMenu()
        pref_action = QAction("Preferences", self)
        pref_action.triggered.connect(self.toggleWindow)
        menu.addAction(pref_action)

        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self.pausePressed)
        menu.addAction(self.pause_action)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.show()

    def toggleWindow(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def pausePressed(self):
        self.pauseMode = not self.pauseMode
        if self.pauseMode:
            self.pause_action.setText("Resume")
            for conf in self.configs:
                # Fix 6: acquire the lock so the worker thread never sees
                # Simulate=True with the old dev_mac still set (or vice-versa).
                with conf[2]._state_lock:
                    conf[2].lastMAC = conf[2].dev_mac
                    conf[2].dev_mac = ''
                    conf[2].Simulate = True
        else:
            self.pause_action.setText("Pause")
            for conf in self.configs:
                with conf[2]._state_lock:
                    conf[2].dev_mac = getattr(conf[2], 'lastMAC', '')
                    conf[2].Simulate = False

    def fillConfigCombo(self):
        self.comboConfig.clear()
        for conf in self.configs:
            self.comboConfig.addItem(conf[0])
        self.comboConfig.setCurrentText(self.configname)

    def comboConfig_changed(self, text):
        if text != self.configname and text:
            for conf in self.configs:
                if text == conf[0]:
                    self.config = conf[1]
                    self.configname = conf[0]
                    self.proxi = conf[2]
                    self.readSettings()
                    break

    def _on_settings_changed(self):
        if not self.gone_live:
            return
        has_device = bool(self.entryMAC.text().strip())
        self.btnApply.setEnabled(has_device)

    def _resetMinMax(self):
        self.minDist = -255
        self.maxDist = 0

    def readSettings(self):
        was_live = self.gone_live
        self.gone_live = False
        self.entryMAC.setText(self.config['device_mac'])
        self.hscaleLockDist.setValue(int(self.config['lock_distance']))
        self.hscaleLockDur.setValue(int(self.config['lock_duration']))
        self.hscaleUnlockDist.setValue(int(self.config['unlock_distance']))
        self.hscaleUnlockDur.setValue(int(self.config['unlock_duration']))
        self.hscaleProxi.setValue(int(self.config['proximity_interval']))
        self.checkSyslog.setChecked(self.config['log_to_syslog'] == 'True' or self.config['log_to_syslog'] is True)
        self.checkFile.setChecked(self.config['log_to_file'] == 'True' or self.config['log_to_file'] is True)
        self.entryFile.setText(self.config['log_filelog_filename'])
        self.comboLock.setCurrentText(self.config['lock_command'])
        self.comboUnlock.setCurrentText(self.config['unlock_command'])
        self.comboProxi.setCurrentText(self.config['proximity_command'])
        self.gone_live = was_live
        # Reflect whether a device is already configured
        self.btnApply.setEnabled(bool(self.config['device_mac']))

    def applySettings(self):
        self.writeSettings()
        self.btnApply.setEnabled(False)

    def writeSettings(self):
        # Fix 5: hold the lock while writing multiple attributes so the worker
        # thread never sees a half-updated snapshot (e.g. new dev_mac but old
        # gone_limit from the previous config).
        # Fix 9: also sync ringbuffer if buffer_size changed in the config file.
        new_buf_size = int(self.config.get('buffer_size', 1))
        with self.proxi._state_lock:
            self.proxi.dev_mac = self.entryMAC.text()
            self.proxi.gone_limit = -self.hscaleLockDist.value()
            self.proxi.gone_duration = self.hscaleLockDur.value()
            self.proxi.active_limit = -self.hscaleUnlockDist.value()
            self.proxi.active_duration = self.hscaleUnlockDur.value()
            if new_buf_size != self.proxi.ringbuffer_size:
                self.proxi.ringbuffer_size = new_buf_size
                self.proxi.ringbuffer = [-254] * new_buf_size
                self.proxi.ringbuffer_pos = 0

        self.config['device_mac'] = self.proxi.dev_mac
        self.config['lock_distance'] = int(-self.proxi.gone_limit)
        self.config['lock_duration'] = int(self.proxi.gone_duration)
        self.config['unlock_distance'] = int(-self.proxi.active_limit)
        self.config['unlock_duration'] = int(self.proxi.active_duration)
        self.config['proximity_interval'] = self.hscaleProxi.value()
        self.config['log_to_syslog'] = self.checkSyslog.isChecked()
        self.config['log_to_file'] = self.checkFile.isChecked()
        self.config['log_filelog_filename'] = self.entryFile.text()
        self.config['lock_command'] = self.comboLock.currentText()
        self.config['unlock_command'] = self.comboUnlock.currentText()
        self.config['proximity_command'] = self.comboProxi.currentText()

        self.proxi.logger.configureFromConfig(self.config)
        self.config.write()

    def updateState(self):
        newVal = int(self.proxi.Dist)

        # Fix 8: only update min/max when the device is actually visible.
        # The sentinel value -255 ("no device") would corrupt maxDist on the
        # very first tick (−255 < 0), permanently pinning "max: 255" in the UI.
        if newVal != -255:
            if newVal > self.minDist:
                self.minDist = newVal
            if newVal < self.maxDist:
                self.maxDist = newVal

        # Show '-' until we have at least one real reading (matches the UI default).
        min_str = str(-self.minDist) if self.minDist != -255 else '-'
        max_str = str(-self.maxDist) if self.maxDist != 0 else '-'
        self.labState.setText(f"min: {min_str} max: {max_str} state: {self.proxi.State}")

        # Fix 7: clamp the slider to its [0, 127] range.
        # When dist==-255 (no device) −newVal would be 255, overflowing the max.
        if newVal == -255:
            slider_val = 0
        else:
            slider_val = max(0, min(127, -newVal))
        self.hscaleAct.setValue(slider_val)

        if self.pauseMode:
            self.tray_icon.setIcon(QIcon(os.path.join(dist_path, icon_pause)))
            self.tray_icon.setToolTip('Pause Mode - not connected')
        else:
            con_state = 0
            if self.proxi.State != 'active':
                con_state = 2
            elif newVal < self.proxi.active_limit:
                con_state = 1
            icons = [icon_base, icon_att, icon_away, icon_error]
            self.tray_icon.setIcon(QIcon(os.path.join(dist_path, icons[con_state])))
            dist_display = 'N/A' if newVal == -255 else str(-newVal)
            self.tray_icon.setToolTip(f"{self.configname}: State: {self.proxi.State}\nDist: {dist_display}\n{self.proxi.ErrorMsg}")

    def quit(self):
        for conf in self.configs:
            conf[2].logger.log_line('stopped.')
            conf[2].Stop = True
        QApplication.quit()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    configs = []
    new_config = True
    conf_dir = os.path.join(os.getenv('HOME'), '.blueproximity')
    if not os.path.exists(conf_dir):
        os.mkdir(conf_dir)

    vdt = Validator()
    for filename in os.listdir(conf_dir):
        if filename.endswith('.conf'):
            try:
                config = ConfigObj(os.path.join(conf_dir, filename),
                                   create_empty=False, file_error=True, configspec=conf_specs)
                config.validate(vdt, copy=True)
                config.write()
                configs.append([filename[:-5], config])
                new_config = False
            except: pass

    if new_config:
        config = ConfigObj(os.path.join(conf_dir, 'standard.conf'),
                           create_empty=True, file_error=False, configspec=conf_specs)
        config['device_mac'] = ''
        config.validate(vdt, copy=True)
        config.write()
        configs.append(['standard', config])

    for config in configs:
        p = Proximity(config[1])
        p.start()
        config.append(p)

    gui = ProximityGUI(configs, new_config)
    sys.exit(app.exec())
