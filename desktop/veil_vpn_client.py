from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QEasingCurve, QLockFile, QProcess, QPropertyAnimation, Qt, QTimer
from PyQt6.QtCore import QLockFile as QtLockFile
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform.startswith("win"):
    from veil_core.windows_client_app import WindowsClientPaths as ClientPaths
    from veil_core.windows_client_app import (
        WindowsClientConfig,
        WindowsClientEnvironment,
        install_windows_client,
        load_client_config,
        read_runtime_status,
        save_client_config as save_windows_client_config,
        start_runtime,
        stop_runtime,
    )
    from veil_core.provisioning import ClientConnectionProfile
else:
    from veil_core.linux_client_app import LinuxClientPaths as ClientPaths
    from veil_core.linux_client_app import LinuxClientEnvironment, build_action_command, load_client_config

from veil_core.protocol_catalog import (
    PERSONA_OPTIONS,
    WRAPPER_OPTIONS,
    describe_protocol_selection,
)


class VeilVpnWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self._paths = ClientPaths.detect(repo_root=ROOT)
        self._config_path = config_path or self._paths.config_path
        self._paths.gui_lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._gui_lock = QLockFile(str(self._paths.gui_lock_path))
        self._gui_lock.setStaleLockTime(0)
        if not self._gui_lock.tryLock(100):
            if self._gui_lock.error() == QtLockFile.LockError.LockFailedError:
                raise RuntimeError("Veil VPN is already running. Close the existing window before starting another one.")
            raise RuntimeError(f"Could not create GUI lock at {self._paths.gui_lock_path}: {self._gui_lock.error().name}")
        self._process: QProcess | None = None
        self._status_process: QProcess | None = None
        self._refresh_in_flight = False
        self._last_log_stamp: tuple[float, int] | None = None
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(4000)

        self.setWindowTitle("Veil VPN Client")
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        target_width = 1040 if available is None else max(860, min(1040, available.width() - 64))
        target_height = 760 if available is None else max(620, min(760, available.height() - 64))
        self.resize(target_width, target_height)
        self.setMinimumSize(820, 600)
        self.setStyleSheet(
            """
            QMainWindow, QWidget#appRoot {
                background: #eef3f6;
                color: #13202b;
                font-size: 13px;
            }
            QLabel {
                background: transparent;
            }
            QLabel#formLabel {
                color: #27465b;
                font-weight: 600;
                padding-right: 6px;
            }
            QFrame#heroCard {
                border-radius: 22px;
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #102d46,
                    stop: 0.5 #18607a,
                    stop: 1 #2d8d8b
                );
            }
            QFrame#actionBar {
                border: 1px solid #d5dfe8;
                border-radius: 18px;
                background: #fbfdff;
            }
            QWidget#leftPanel {
                background: #eef3f6;
            }
            QTabWidget::pane {
                border: 1px solid #d5dfe8;
                border-radius: 16px;
                top: -1px;
                background: #fbfdff;
            }
            QTabBar::tab {
                background: #dce7ef;
                color: #234357;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                padding: 8px 14px;
                margin-right: 4px;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: #123b59;
                color: #ffffff;
            }
            QGroupBox {
                border: 1px solid #d5dfe8;
                border-radius: 18px;
                margin-top: 12px;
                padding: 14px 14px 12px 14px;
                background: #fbfdff;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 8px;
                color: #26475c;
            }
            QGroupBox QLabel {
                color: #3a566a;
            }
            QScrollArea {
                border: 0;
                background: transparent;
            }
            QSplitter::handle {
                background: transparent;
                width: 8px;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {
                border: 1px solid #c7d6e2;
                border-radius: 12px;
                padding: 8px 10px;
                background: #ffffff;
                color: #13202b;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 6px 0 6px 0;
            }
            QScrollBar::handle:vertical {
                background: #bccbd7;
                border-radius: 5px;
                min-height: 24px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                background: transparent;
                height: 10px;
                margin: 0 6px 0 6px;
            }
            QScrollBar::handle:horizontal {
                background: #bccbd7;
                border-radius: 5px;
                min-width: 24px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QPushButton {
                border: 0;
                border-radius: 12px;
                padding: 9px 14px;
                background: #123b59;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #185070;
            }
            QPushButton:pressed {
                background: #102f45;
            }
            QPushButton[role="secondary"] {
                background: #dce8f1;
                color: #12344d;
            }
            QPushButton[role="secondary"]:hover {
                background: #cfdeea;
            }
            QPushButton[role="ghost"] {
                background: #eef4f8;
                color: #234559;
            }
            QPushButton[role="ghost"]:hover {
                background: #e2edf4;
            }
            QLabel[kind="badge"] {
                padding: 4px 10px;
                border-radius: 999px;
                background: rgba(255, 255, 255, 0.18);
                color: white;
                font-weight: 600;
            }
            """
        )

        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QGridLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(14)

        hero = QFrame()
        hero.setObjectName("heroCard")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(20, 16, 20, 16)
        hero_layout.setSpacing(6)
        self._hero_title = QLabel("Veil VPN")
        self._hero_title.setStyleSheet("color: white; font-size: 28px; font-weight: 700;")
        self._hero_subtitle = QLabel(
            "Linux client workbench for tunnel setup, protocol shaping, diagnostics and live status."
        )
        self._hero_subtitle.setWordWrap(True)
        self._hero_subtitle.setStyleSheet("color: rgba(255, 255, 255, 0.88); font-size: 13px;")
        self._hero_meta = QLabel("")
        self._hero_meta.setStyleSheet("color: rgba(255, 255, 255, 0.78); font-size: 12px;")
        self._hero_badge = QLabel("Linux Desktop")
        self._hero_badge.setProperty("kind", "badge")
        hero_layout.addWidget(self._hero_title)
        hero_layout.addWidget(self._hero_subtitle)
        hero_layout.addWidget(self._hero_badge)
        hero_layout.addWidget(self._hero_meta)

        config_box = QGroupBox("Profile")
        config_layout = QVBoxLayout(config_box)

        def add_form_row(form: QFormLayout, label_text: str, widget: QWidget) -> None:
            label = QLabel(label_text)
            label.setObjectName("formLabel")
            form.addRow(label, widget)

        self._server_host = QLineEdit()
        self._server_host.setPlaceholderText("vpn.example or public IP")
        self._server_port = QSpinBox()
        self._server_port.setRange(1, 65535)
        self._client_name = QLineEdit()
        self._client_name.setPlaceholderText("veil-client")
        self._psk_hex = QLineEdit()
        self._psk_hex.setPlaceholderText("64 hex chars")
        self._adapter_label = QLabel("Adapter Name" if sys.platform.startswith("win") else "TUN Name")
        self._adapter_label.setObjectName("formLabel")
        self._adapter_value = QLineEdit()
        self._adapter_value.setPlaceholderText("veilfull0" if not sys.platform.startswith("win") else "VeilVPN")
        self._tunnel_mode = QComboBox()
        self._tunnel_mode.addItems(["static", "dynamic"])
        self._tun_address = QLineEdit()
        self._tun_address.setPlaceholderText("10.200.0.2/30")
        self._tun_peer = QLineEdit()
        self._tun_peer.setPlaceholderText("10.200.0.1")
        self._protocol_wrapper = QComboBox()
        for option in WRAPPER_OPTIONS:
            self._protocol_wrapper.addItem(f"{option.label} ({option.value})", option.value)
        self._persona_preset = QComboBox()
        self._persona_preset.setEditable(True)
        for option in PERSONA_OPTIONS:
            self._persona_preset.addItem(f"{option.label} ({option.value})", option.value)
        self._http_handshake = QCheckBox("Enable HTTP Upgrade prelude")
        self._packet_mtu = QSpinBox()
        self._packet_mtu.setRange(576, 65535)
        self._packet_mtu.setValue(1300)
        self._keepalive_interval = QDoubleSpinBox()
        self._keepalive_interval.setRange(0.5, 3600.0)
        self._keepalive_interval.setDecimals(1)
        self._keepalive_interval.setSingleStep(0.5)
        self._keepalive_interval.setValue(10.0)
        self._keepalive_timeout = QDoubleSpinBox()
        self._keepalive_timeout.setRange(0.5, 3600.0)
        self._keepalive_timeout.setDecimals(1)
        self._keepalive_timeout.setSingleStep(0.5)
        self._keepalive_timeout.setValue(30.0)
        self._rotation_interval = QSpinBox()
        self._rotation_interval.setRange(1, 3600)
        self._rotation_interval.setValue(30)
        self._handshake_timeout = QSpinBox()
        self._handshake_timeout.setRange(100, 300000)
        self._handshake_timeout.setSingleStep(100)
        self._handshake_timeout.setValue(5000)
        self._session_idle_timeout = QSpinBox()
        self._session_idle_timeout.setRange(0, 3_600_000)
        self._session_idle_timeout.setSingleStep(1000)
        self._transport_mtu = QSpinBox()
        self._transport_mtu.setRange(576, 65535)
        self._transport_mtu.setValue(1400)
        self._reconnect = QCheckBox("Reconnect after disconnect")
        self._auto_connect = QCheckBox("Auto-connect when UI starts")
        self._suspend_conflicting_services = QCheckBox("Suspend conflicting VPN services before connect")
        self._full_tunnel = QCheckBox("Route all IPv4 traffic through Veil")
        self._protocol_summary = QLabel("-")
        self._protocol_summary.setWordWrap(True)
        self._protocol_summary.setStyleSheet(
            "padding: 10px 12px; border-radius: 12px; background: #eef5fb; color: #18384d;"
        )

        tabs = QTabWidget()
        tunnel_tab = QWidget()
        tunnel_layout = QFormLayout(tunnel_tab)
        tunnel_layout.setHorizontalSpacing(14)
        tunnel_layout.setVerticalSpacing(10)
        add_form_row(tunnel_layout, "Server Host", self._server_host)
        add_form_row(tunnel_layout, "Server Port", self._server_port)
        add_form_row(tunnel_layout, "Client Name", self._client_name)
        add_form_row(tunnel_layout, "PSK Hex", self._psk_hex)
        tunnel_layout.addRow(self._adapter_label, self._adapter_value)
        add_form_row(tunnel_layout, "Tunnel Mode", self._tunnel_mode)
        add_form_row(tunnel_layout, "TUN Address", self._tun_address)
        add_form_row(tunnel_layout, "TUN Peer", self._tun_peer)
        add_form_row(tunnel_layout, "Packet MTU", self._packet_mtu)
        if sys.platform.startswith("win"):
            add_form_row(tunnel_layout, "Tunnel Policy", self._full_tunnel)

        protocol_tab = QWidget()
        protocol_layout = QFormLayout(protocol_tab)
        protocol_layout.setHorizontalSpacing(14)
        protocol_layout.setVerticalSpacing(10)
        add_form_row(protocol_layout, "Protocol Wrapper", self._protocol_wrapper)
        add_form_row(protocol_layout, "Persona Preset", self._persona_preset)
        add_form_row(protocol_layout, "HTTP Upgrade", self._http_handshake)
        add_form_row(protocol_layout, "Transport MTU", self._transport_mtu)
        add_form_row(protocol_layout, "Protocol Summary", self._protocol_summary)

        session_tab = QWidget()
        session_layout = QFormLayout(session_tab)
        session_layout.setHorizontalSpacing(14)
        session_layout.setVerticalSpacing(10)
        add_form_row(session_layout, "Keepalive Interval (s)", self._keepalive_interval)
        add_form_row(session_layout, "Keepalive Timeout (s)", self._keepalive_timeout)
        add_form_row(session_layout, "Key Rotation (s)", self._rotation_interval)
        add_form_row(session_layout, "Handshake Timeout (ms)", self._handshake_timeout)
        add_form_row(session_layout, "Session Idle Timeout (ms)", self._session_idle_timeout)
        add_form_row(session_layout, "Session Policy", self._reconnect)
        add_form_row(session_layout, "Startup Policy", self._auto_connect)
        if not sys.platform.startswith("win"):
            add_form_row(session_layout, "Conflict Handling", self._suspend_conflicting_services)

        tabs.addTab(tunnel_tab, "Tunnel")
        tabs.addTab(protocol_tab, "Protocol")
        tabs.addTab(session_tab, "Session")
        config_layout.addWidget(tabs)
        self._install_button = QPushButton("Install")
        self._install_button.clicked.connect(self.install_client)
        self._save_button = QPushButton("Save Profile")
        self._save_button.clicked.connect(self.save_profile)
        self._import_button = QPushButton("Import Profile")
        self._import_button.clicked.connect(self.import_profile)
        self._import_url_button = QPushButton("Import URL")
        self._import_url_button.setProperty("role", "secondary")
        self._import_url_button.clicked.connect(self.import_profile_url)
        self._import_token_button = QPushButton("Import Token")
        self._import_token_button.clicked.connect(self.import_profile_token)
        self._doctor_button = QPushButton("Doctor")
        self._doctor_button.setProperty("role", "ghost")
        self._doctor_button.clicked.connect(self.show_doctor_report)
        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self.connect_client)
        self._disconnect_button = QPushButton("Disconnect")
        self._disconnect_button.clicked.connect(self.disconnect_client)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.setProperty("role", "secondary")
        self._refresh_button.clicked.connect(self.refresh_status)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_layout = QGridLayout(action_bar)
        action_layout.setContentsMargins(12, 12, 12, 12)
        action_layout.setHorizontalSpacing(10)
        action_layout.setVerticalSpacing(10)
        action_buttons = (
            self._install_button,
            self._save_button,
            self._import_button,
            self._import_url_button,
            self._import_token_button,
            self._doctor_button,
            self._connect_button,
            self._disconnect_button,
            self._refresh_button,
        )
        for index, button in enumerate(action_buttons):
            action_layout.addWidget(button, index // 3, index % 3)

        status_box = QGroupBox("Status")
        status_layout = QVBoxLayout(status_box)
        self._headline = QLabel(
            "Windows-native Veil tunnel client" if sys.platform.startswith("win") else "Linux tunnel and protocol status"
        )
        self._headline.setStyleSheet("font-size: 15px; font-weight: 600; color: #345267;")
        self._status_label = QLabel("unknown")
        self._status_label.setStyleSheet("font-size: 20px; font-weight: 700;")
        self._details_label = QLabel("-")
        self._details_label.setWordWrap(True)
        self._status_notes = QLabel("-")
        self._status_notes.setWordWrap(True)
        self._status_notes.setStyleSheet(
            "padding: 10px 12px; border-radius: 12px; background: #f0f6fa; color: #294659;"
        )
        status_layout.addWidget(self._headline)
        status_layout.addWidget(self._status_label)
        status_layout.addWidget(self._details_label)
        status_layout.addWidget(self._status_notes)
        status_layout.addStretch(1)

        logs_box = QGroupBox("Logs")
        logs_layout = QVBoxLayout(logs_box)
        self._logs = QPlainTextEdit()
        self._logs.setReadOnly(True)
        self._logs.setFont(QFont("JetBrains Mono", 10))
        self._logs.setPlaceholderText("Runtime logs will appear here after install, connect or doctor actions.")
        logs_layout.addWidget(self._logs)

        left_panel = QWidget()
        left_panel.setObjectName("leftPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)
        left_layout.addWidget(config_box)
        left_layout.addWidget(status_box)
        left_layout.addStretch(1)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_panel)

        splitter = QSplitter()
        splitter.addWidget(left_scroll)
        splitter.addWidget(logs_box)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, max(380, target_width - 460)])

        layout.addWidget(hero, 0, 0)
        layout.addWidget(action_bar, 1, 0)
        layout.addWidget(splitter, 2, 0)
        layout.setRowStretch(2, 1)

        style = self.style()
        self._install_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self._save_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self._import_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        self._import_url_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self._import_token_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self._doctor_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self._connect_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self._disconnect_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self._refresh_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload))

        self.setWindowOpacity(0.0)
        self._fade_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_animation.setDuration(260)
        self._fade_animation.setStartValue(0.0)
        self._fade_animation.setEndValue(1.0)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.start()

        self.load_profile()
        self._tunnel_mode.currentTextChanged.connect(self._sync_tunnel_mode_state)
        self._protocol_wrapper.currentTextChanged.connect(self._update_protocol_summary)
        self._persona_preset.currentTextChanged.connect(self._update_protocol_summary)
        self._http_handshake.toggled.connect(self._update_protocol_summary)
        self._server_host.textChanged.connect(self._update_protocol_summary)
        self._server_port.valueChanged.connect(self._update_protocol_summary)
        self._sync_tunnel_mode_state(self._tunnel_mode.currentText())
        self._update_protocol_summary()
        self.refresh_status()

    def _run_ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = self._ctl_command(*args)
        return subprocess.run(
            command,
            cwd=self._paths.repo_root,
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0,
        )

    def _ctl_command(self, *args: str) -> list[str]:
        if sys.platform.startswith("win") and getattr(sys, "frozen", False):
            return [str(self._paths.ctl_script_path), "ctl", *args]
        return [sys.executable, str(self._paths.ctl_script_path), *args]

    def install_client(self) -> None:
        if sys.platform.startswith("win"):
            try:
                payload = install_windows_client(self._paths, WindowsClientEnvironment.detect())
            except Exception as exc:
                self._show_error("Install failed", str(exc))
                return
            self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))
            self.refresh_status()
            return
        result = self._run_ctl("install")
        if result.returncode != 0:
            self._show_error("Install failed", result.stderr or result.stdout)
            return
        self._append_log(result.stdout.strip())
        self.refresh_status()

    def load_profile(self) -> None:
        config = load_client_config(self._config_path)
        self._server_host.setText(config.server_host)
        self._server_port.setValue(config.server_port)
        self._client_name.setText(config.client_name)
        self._psk_hex.setText(config.psk_hex)
        if sys.platform.startswith("win"):
            self._adapter_value.setText(getattr(config, "adapter_name", "VeilVPN"))
            self._tun_address.setText(getattr(config, "tun_address", "10.200.0.2/30"))
            self._tun_peer.setText(getattr(config, "tun_peer", "10.200.0.1"))
            self._full_tunnel.setChecked(bool(getattr(config, "full_tunnel", True)))
        else:
            self._adapter_value.setText(config.tun_name)
            self._tun_address.setText(config.tun_address)
            self._tun_peer.setText(config.tun_peer)
            self._tunnel_mode.setCurrentText(getattr(config, "tunnel_mode", "static"))
        wrapper_index = self._protocol_wrapper.findData(config.protocol_wrapper)
        if wrapper_index >= 0:
            self._protocol_wrapper.setCurrentIndex(wrapper_index)
        else:
            self._protocol_wrapper.setCurrentText(config.protocol_wrapper)
        persona_index = self._persona_preset.findData(config.persona_preset)
        if persona_index >= 0:
            self._persona_preset.setCurrentIndex(persona_index)
        else:
            self._persona_preset.setCurrentText(config.persona_preset)
        self._http_handshake.setChecked(bool(getattr(config, "enable_http_handshake_emulation", False)))
        self._packet_mtu.setValue(int(getattr(config, "packet_mtu", 1300)))
        self._keepalive_interval.setValue(float(getattr(config, "keepalive_interval", 10.0)))
        self._keepalive_timeout.setValue(float(getattr(config, "keepalive_timeout", 30.0)))
        self._rotation_interval.setValue(int(getattr(config, "rotation_interval_seconds", 30)))
        self._handshake_timeout.setValue(int(getattr(config, "handshake_timeout_ms", 5000)))
        self._session_idle_timeout.setValue(int(getattr(config, "session_idle_timeout_ms", 0)))
        self._transport_mtu.setValue(int(getattr(config, "transport_mtu", 1400)))
        self._reconnect.setChecked(bool(getattr(config, "reconnect", True)))
        self._auto_connect.setChecked(bool(getattr(config, "auto_connect", False)))
        self._suspend_conflicting_services.setChecked(bool(getattr(config, "suspend_conflicting_services", False)))
        self._sync_tunnel_mode_state(self._tunnel_mode.currentText())
        self._update_protocol_summary()

    def save_profile(self) -> None:
        if sys.platform.startswith("win"):
            try:
                config = self._build_windows_config()
                save_windows_client_config(self._config_path, config)
            except Exception as exc:
                self._show_error("Save failed", str(exc))
                return
            self._append_log("Profile saved")
            self.refresh_status()
            return
        args = [
            "--config",
            str(self._config_path),
            "save-config",
            "--server-host",
            self._server_host.text().strip(),
            "--server-port",
            str(self._server_port.value()),
            "--client-name",
            self._client_name.text().strip(),
            "--psk-hex",
            self._psk_hex.text().strip(),
            "--tunnel-mode",
            self._tunnel_mode.currentText().strip(),
            "--protocol-wrapper",
            self._current_wrapper_value(),
            "--persona-preset",
            self._current_persona_value(),
            "--enable-http-handshake-emulation",
            "true" if self._http_handshake.isChecked() else "false",
            "--packet-mtu",
            str(self._packet_mtu.value()),
            "--keepalive-interval",
            f"{self._keepalive_interval.value():.1f}",
            "--keepalive-timeout",
            f"{self._keepalive_timeout.value():.1f}",
            "--reconnect",
            "true" if self._reconnect.isChecked() else "false",
            "--auto-connect",
            "true" if self._auto_connect.isChecked() else "false",
            "--rotation-interval-seconds",
            str(self._rotation_interval.value()),
            "--handshake-timeout-ms",
            str(self._handshake_timeout.value()),
            "--session-idle-timeout-ms",
            str(self._session_idle_timeout.value()),
            "--transport-mtu",
            str(self._transport_mtu.value()),
        ]
        if sys.platform.startswith("win"):
            args.extend(
                [
                    "--adapter-name",
                    self._adapter_value.text().strip(),
                    "--tun-address",
                    self._tun_address.text().strip(),
                    "--tun-peer",
                    self._tun_peer.text().strip(),
                    "--full-tunnel",
                    "true" if self._full_tunnel.isChecked() else "false",
                ]
            )
        else:
            args.extend(
                [
                    "--tun-name",
                    self._adapter_value.text().strip(),
                    "--tun-address",
                    self._tun_address.text().strip(),
                    "--tun-peer",
                    self._tun_peer.text().strip(),
                    "--suspend-conflicting-services",
                    "true" if self._suspend_conflicting_services.isChecked() else "false",
                ]
            )
        result = self._run_ctl(*args)
        if result.returncode != 0:
            self._show_error("Save failed", result.stderr or result.stdout)
            return
        self.load_profile()
        self._append_log("Profile saved")
        self.refresh_status()

    def import_profile(self) -> None:
        profile_path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import Veil VPN Profile",
            str(Path.home()),
            "JSON Files (*.json);;All Files (*)",
        )
        if not profile_path_str:
            return
        if sys.platform.startswith("win"):
            try:
                profile = ClientConnectionProfile.from_path(Path(profile_path_str))
                current = load_client_config(self._config_path)
                updated = WindowsClientConfig(
                    **{
                        **current.__dict__,
                        "server_host": profile.server_host,
                        "server_port": profile.server_port,
                        "client_name": profile.client_name,
                        "psk_hex": profile.psk_hex,
                        "tun_address": profile.tun_address,
                        "tun_peer": profile.tun_peer,
                        "packet_mtu": profile.packet_mtu,
                        "keepalive_interval": profile.keepalive_interval,
                        "keepalive_timeout": profile.keepalive_timeout,
                        "reconnect": profile.reconnect,
                        "auto_connect": profile.auto_connect,
                        "protocol_wrapper": profile.protocol_wrapper,
                        "persona_preset": profile.persona_preset,
                    }
                )
                save_windows_client_config(self._config_path, updated)
            except Exception as exc:
                self._show_error("Import failed", str(exc))
                return
            self.load_profile()
            self._append_log(f"Imported profile: {profile_path_str}")
            self.refresh_status()
            return
        result = self._run_ctl(
            "--config",
            str(self._config_path),
            "import-profile",
            "--profile",
            profile_path_str,
        )
        if result.returncode != 0:
            self._show_error("Import failed", result.stderr or result.stdout)
            return
        self.load_profile()
        self._append_log(f"Imported profile: {profile_path_str}")
        self.refresh_status()

    def import_profile_url(self) -> None:
        profile_url, accepted = QInputDialog.getText(
            self,
            "Import Veil VPN Profile From URL",
            "Profile URL",
        )
        if not accepted or not profile_url.strip():
            return
        if sys.platform.startswith("win"):
            try:
                with urllib.request.urlopen(profile_url.strip()) as response:
                    profile = ClientConnectionProfile.from_json_text(response.read().decode("utf-8"))
                current = load_client_config(self._config_path)
                updated = WindowsClientConfig(
                    **{
                        **current.__dict__,
                        "server_host": profile.server_host,
                        "server_port": profile.server_port,
                        "client_name": profile.client_name,
                        "psk_hex": profile.psk_hex,
                        "tun_address": profile.tun_address,
                        "tun_peer": profile.tun_peer,
                        "packet_mtu": profile.packet_mtu,
                        "keepalive_interval": profile.keepalive_interval,
                        "keepalive_timeout": profile.keepalive_timeout,
                        "reconnect": profile.reconnect,
                        "auto_connect": profile.auto_connect,
                        "protocol_wrapper": profile.protocol_wrapper,
                        "persona_preset": profile.persona_preset,
                    }
                )
                save_windows_client_config(self._config_path, updated)
            except Exception as exc:
                self._show_error("Import failed", str(exc))
                return
            self.load_profile()
            self._append_log(f"Imported profile URL: {profile_url.strip()}")
            self.refresh_status()
            return
        result = self._run_ctl(
            "--config",
            str(self._config_path),
            "import-profile",
            "--profile-url",
            profile_url.strip(),
        )
        if result.returncode != 0:
            self._show_error("Import failed", result.stderr or result.stdout)
            return
        self.load_profile()
        self._append_log(f"Imported profile URL: {profile_url.strip()}")
        self.refresh_status()

    def import_profile_token(self) -> None:
        token, accepted = QInputDialog.getMultiLineText(
            self,
            "Import Veil VPN Token",
            "Paste provisioning token",
        )
        if not accepted or not token.strip():
            return
        if sys.platform.startswith("win"):
            try:
                profile = ClientConnectionProfile.from_import_token(token.strip())
                current = load_client_config(self._config_path)
                updated = WindowsClientConfig(
                    **{
                        **current.__dict__,
                        "server_host": profile.server_host,
                        "server_port": profile.server_port,
                        "client_name": profile.client_name,
                        "psk_hex": profile.psk_hex,
                        "tun_address": profile.tun_address,
                        "tun_peer": profile.tun_peer,
                        "packet_mtu": profile.packet_mtu,
                        "keepalive_interval": profile.keepalive_interval,
                        "keepalive_timeout": profile.keepalive_timeout,
                        "reconnect": profile.reconnect,
                        "auto_connect": profile.auto_connect,
                        "protocol_wrapper": profile.protocol_wrapper,
                        "persona_preset": profile.persona_preset,
                    }
                )
                save_windows_client_config(self._config_path, updated)
            except Exception as exc:
                self._show_error("Import failed", str(exc))
                return
            self.load_profile()
            self._append_log("Imported profile token")
            self.refresh_status()
            return
        result = self._run_ctl(
            "--config",
            str(self._config_path),
            "import-profile",
            "--profile-token",
            token.strip(),
        )
        if result.returncode != 0:
            self._show_error("Import failed", result.stderr or result.stdout)
            return
        self.load_profile()
        self._append_log("Imported profile token")
        self.refresh_status()

    def show_doctor_report(self) -> None:
        result = self._run_ctl("--format", "text", "doctor")
        if result.returncode != 0:
            self._show_error("Doctor failed", result.stderr or result.stdout)
            return
        report = result.stdout.strip()
        if report:
            self._append_log(report)
            QMessageBox.information(self, "Veil VPN Doctor", report)

    def connect_client(self) -> None:
        self.save_profile()
        if sys.platform.startswith("win"):
            try:
                payload = start_runtime(
                    self._paths,
                    WindowsClientEnvironment.detect(),
                    load_client_config(self._config_path),
                    config_path=self._config_path,
                )
            except Exception as exc:
                self._show_error("Connect failed", str(exc))
                self.refresh_status()
                return
            self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))
            self.refresh_status()
            return
        self._run_process("up")

    def disconnect_client(self) -> None:
        if sys.platform.startswith("win"):
            try:
                payload = stop_runtime(self._paths, load_client_config(self._config_path))
            except Exception as exc:
                self._show_error("Disconnect failed", str(exc))
                return
            self._append_log(json.dumps(payload, ensure_ascii=False, indent=2))
            self.refresh_status()
            return
        self._run_process("down")

    def _run_process(self, command: str) -> None:
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self._show_error("Action already running", "Wait for the previous action to finish")
            return
        process = QProcess(self)
        if sys.platform.startswith("win"):
            ctl_command = self._ctl_command("--config", str(self._config_path), command)
        else:
            ctl_command = build_action_command(
                command,
                config_path=self._config_path,
                config=load_client_config(self._config_path),
                paths=self._paths,
                env=LinuxClientEnvironment.detect(),
            )
        process.setProgram(ctl_command[0])
        process.setArguments(ctl_command[1:])
        process.setWorkingDirectory(str(self._paths.repo_root))
        process.finished.connect(self._process_finished)
        process.start()
        self._process = process
        self._set_action_buttons_enabled(False)

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        assert self._process is not None
        stdout = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        stderr = bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        if stdout:
            self._append_log(stdout)
        if stderr:
            self._append_log(stderr)
        if exit_code != 0:
            self._show_error("Command failed", stderr or stdout or f"Exit code {exit_code}")
        self._process = None
        self._set_action_buttons_enabled(True)
        self.refresh_status()

    def refresh_status(self) -> None:
        if sys.platform.startswith("win"):
            try:
                payload = read_runtime_status(self._paths, load_client_config(self._config_path))
            except Exception as exc:
                self._status_label.setText("error")
                self._details_label.setText(str(exc))
                return
            self._apply_status_payload(payload)
            return
        if self._refresh_in_flight:
            return
        if self._status_process is not None and self._status_process.state() != QProcess.ProcessState.NotRunning:
            return
        self._refresh_in_flight = True
        process = QProcess(self)
        ctl_command = self._ctl_command("--config", str(self._config_path), "status")
        process.setProgram(ctl_command[0])
        process.setArguments(ctl_command[1:])
        process.setWorkingDirectory(str(self._paths.repo_root))
        process.finished.connect(self._status_finished)
        process.start()
        self._status_process = process

    def _status_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        assert self._status_process is not None
        stdout = bytes(self._status_process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        stderr = bytes(self._status_process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        self._status_process = None
        self._refresh_in_flight = False
        if exit_code != 0:
            self._status_label.setText("error")
            self._details_label.setText(stderr or stdout or f"Exit code {exit_code}")
            return
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            self._status_label.setText("error")
            self._details_label.setText(stdout or "Invalid status payload")
            return
        self._apply_status_payload(payload)

    def _apply_status_payload(self, payload: dict[str, object]) -> None:
        running = bool(payload.get("running"))
        connected = bool(payload.get("connected", running))
        installed = bool(payload.get("installed"))
        self._status_label.setText(
            "connected" if connected else "running" if running else "installed" if installed else "not installed"
        )
        if connected:
            self._status_label.setStyleSheet("font-size: 20px; font-weight: 700; color: #14804a;")
        elif running:
            self._status_label.setStyleSheet("font-size: 20px; font-weight: 700; color: #c77700;")
        else:
            self._status_label.setStyleSheet("font-size: 20px; font-weight: 700; color: #7a8691;")
        detail_lines = [
            f"installed={installed}",
            f"running={running}",
            f"connected={connected}",
            f"endpoint={payload.get('server_host', '-')}:{payload.get('server_port', '-')}",
            f"log={payload.get('log_path', '-')}",
            f"config={payload.get('config_path', '-')}",
        ]
        if sys.platform.startswith("win"):
            detail_lines.extend(
                [
                    f"adapter={payload.get('adapter_name', '-')}",
                    f"backend={payload.get('tunnel_backend', '-')}",
                    f"tun_address={payload.get('tun_address', '-')}",
                    f"tun_peer={payload.get('tun_peer', '-')}",
                    f"wintun_dll_exists={payload.get('wintun_dll_exists', False)}",
                    f"proxy_enabled={payload.get('proxy_enabled', False)}",
                    f"proxy_server={payload.get('proxy_server', '') or '-'}",
                    f"last_error={payload.get('last_error', '') or '-'}",
                ]
            )
        else:
            detail_lines.append(f"tun_exists={payload.get('tun_exists', False)}")
            detail_lines.extend(
                [
                    f"packet_mtu={payload.get('packet_mtu', '-')}",
                    f"keepalive_interval={payload.get('keepalive_interval', '-')}",
                    f"keepalive_timeout={payload.get('keepalive_timeout', '-')}",
                    f"tunnel_mode={payload.get('tunnel_mode', '-')}",
                    f"reconnect={payload.get('reconnect', '-')}",
                    f"auto_connect={payload.get('auto_connect', '-')}",
                    f"suspend_conflicting_services={payload.get('suspend_conflicting_services', '-')}",
                    f"http_upgrade={payload.get('enable_http_handshake_emulation', False)}",
                    f"transport_mtu={payload.get('transport_mtu', '-')}",
                    f"rotation_interval_seconds={payload.get('rotation_interval_seconds', '-')}",
                    f"handshake_timeout_ms={payload.get('handshake_timeout_ms', '-')}",
                    f"session_idle_timeout_ms={payload.get('session_idle_timeout_ms', '-')}",
                ]
            )
        self._details_label.setText("\n".join(detail_lines))
        protocol = describe_protocol_selection(
            str(payload.get("protocol_wrapper", self._current_wrapper_value())),
            str(payload.get("persona_preset", self._current_persona_value())),
            bool(payload.get("enable_http_handshake_emulation", False)),
        )
        note_lines = [
            f"{protocol['wrapper']['label']}: {protocol['wrapper']['summary']}",
            f"{protocol['persona']['label']}: {protocol['persona']['summary']}",
        ]
        for note in protocol.get("notes", []):
            note_lines.append(f"Note: {note}")
        self._status_notes.setText("\n".join(note_lines))
        self._hero_meta.setText(
            f"Config: {payload.get('config_path', '-')}\n"
            f"Log: {payload.get('log_path', '-')}"
        )
        self._load_log_tail(Path(str(payload.get("log_path", ""))))

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._install_button,
            self._save_button,
            self._import_button,
            self._import_url_button,
            self._import_token_button,
            self._doctor_button,
            self._connect_button,
            self._disconnect_button,
        ):
            button.setEnabled(enabled)

    def _load_log_tail(self, path: Path) -> None:
        if not path or not path.exists():
            return
        try:
            stat = path.stat()
            stamp = (stat.st_mtime, stat.st_size)
            if stamp == self._last_log_stamp:
                return
            self._last_log_stamp = stamp
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(size - 32768, 0))
                chunk = handle.read().decode("utf-8", errors="replace")
            lines = chunk.splitlines()[-80:]
        except Exception:
            return
        self._logs.setPlainText("\n".join(lines))
        self._logs.verticalScrollBar().setValue(self._logs.verticalScrollBar().maximum())

    def _append_log(self, message: str) -> None:
        if not message:
            return
        current = self._logs.toPlainText().splitlines()
        current.extend(message.splitlines())
        self._logs.setPlainText("\n".join(current[-120:]))
        self._logs.verticalScrollBar().setValue(self._logs.verticalScrollBar().maximum())

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def _current_wrapper_value(self) -> str:
        current = self._protocol_wrapper.currentData()
        return str(current if current is not None else self._protocol_wrapper.currentText().strip())

    def _current_persona_value(self) -> str:
        current = self._persona_preset.currentData()
        text = self._persona_preset.currentText().strip()
        if text:
            matched_index = self._persona_preset.findData(text)
            if matched_index >= 0:
                return text
        if current is not None and text == self._persona_preset.itemText(self._persona_preset.currentIndex()).strip():
            return str(current)
        return text or "custom"

    def _update_protocol_summary(self) -> None:
        summary = describe_protocol_selection(
            self._current_wrapper_value(),
            self._current_persona_value(),
            self._http_handshake.isChecked(),
        )
        lines = [
            f"{summary['wrapper']['label']}: {summary['wrapper']['summary']}",
            f"{summary['persona']['label']}: {summary['persona']['summary']}",
        ]
        for note in summary.get("notes", []):
            lines.append(f"Note: {note}")
        lines.append(
            f"Target: {self._server_host.text().strip() or '-'}:{self._server_port.value()} | "
            f"tunnel_mode={self._tunnel_mode.currentText()}"
        )
        self._protocol_summary.setText("\n".join(lines))

    def _sync_tunnel_mode_state(self, tunnel_mode: str) -> None:
        static_mode = tunnel_mode == "static" or sys.platform.startswith("win")
        self._tun_address.setEnabled(static_mode)
        self._tun_peer.setEnabled(static_mode)

    def _build_windows_config(self) -> WindowsClientConfig:
        current = load_client_config(self._config_path)
        return WindowsClientConfig(
            **{
                **current.__dict__,
                "server_host": self._server_host.text().strip(),
                "server_port": self._server_port.value(),
                "client_name": self._client_name.text().strip(),
                "psk_hex": self._psk_hex.text().strip(),
                "adapter_name": self._adapter_value.text().strip(),
                "tun_address": self._tun_address.text().strip(),
                "tun_peer": self._tun_peer.text().strip(),
                "packet_mtu": self._packet_mtu.value(),
                "keepalive_interval": self._keepalive_interval.value(),
                "keepalive_timeout": self._keepalive_timeout.value(),
                "reconnect": self._reconnect.isChecked(),
                "auto_connect": self._auto_connect.isChecked(),
                "protocol_wrapper": self._current_wrapper_value(),
                "persona_preset": self._current_persona_value(),
                "full_tunnel": self._full_tunnel.isChecked(),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()

    app = QApplication(sys.argv)
    try:
        window = VeilVpnWindow(config_path=args.config)
    except Exception as exc:
        QMessageBox.critical(None, "Veil VPN", str(exc))
        raise SystemExit(1)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
