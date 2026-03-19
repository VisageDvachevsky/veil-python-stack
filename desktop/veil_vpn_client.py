from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QLockFile, QProcess, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
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
    QSpinBox,
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
    from veil_core.linux_client_app import load_client_config


class VeilVpnWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self._paths = ClientPaths.detect(repo_root=ROOT)
        self._config_path = config_path or self._paths.config_path
        self._gui_lock = QLockFile(str(self._paths.gui_lock_path))
        self._gui_lock.setStaleLockTime(0)
        if not self._gui_lock.tryLock(100):
            raise RuntimeError("Veil VPN is already running. Close the existing window before starting another one.")
        self._process: QProcess | None = None
        self._status_process: QProcess | None = None
        self._refresh_in_flight = False
        self._last_log_stamp: tuple[float, int] | None = None
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(4000)

        self.setWindowTitle("Veil VPN Client")
        self.resize(980, 680)
        self.setStyleSheet(
            """
            QWidget {
                background: #f3f5f7;
                color: #12202a;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #d7e0e7;
                border-radius: 16px;
                margin-top: 12px;
                padding: 14px;
                background: #ffffff;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
                border: 1px solid #c9d5de;
                border-radius: 10px;
                padding: 8px 10px;
                background: #fbfcfd;
            }
            QPushButton {
                border: 0;
                border-radius: 10px;
                padding: 9px 14px;
                background: #12344d;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #184766;
            }
            QPushButton:pressed {
                background: #0d2738;
            }
            """
        )

        root = QWidget()
        self.setCentralWidget(root)
        layout = QGridLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(18)

        config_box = QGroupBox("Profile")
        config_layout = QFormLayout(config_box)
        self._server_host = QLineEdit()
        self._server_port = QSpinBox()
        self._server_port.setRange(1, 65535)
        self._client_name = QLineEdit()
        self._psk_hex = QLineEdit()
        self._adapter_label = QLabel("Adapter Name" if sys.platform.startswith("win") else "TUN Name")
        self._adapter_value = QLineEdit()
        self._tun_address = QLineEdit()
        self._tun_peer = QLineEdit()
        self._protocol_wrapper = QComboBox()
        self._protocol_wrapper.addItems(["none", "websocket", "tls"])
        self._persona_preset = QLineEdit()
        self._full_tunnel = QCheckBox("Route all IPv4 traffic through Veil")

        config_layout.addRow("Server Host", self._server_host)
        config_layout.addRow("Server Port", self._server_port)
        config_layout.addRow("Client Name", self._client_name)
        config_layout.addRow("PSK Hex", self._psk_hex)
        config_layout.addRow(self._adapter_label, self._adapter_value)
        config_layout.addRow("TUN Address", self._tun_address)
        config_layout.addRow("TUN Peer", self._tun_peer)
        config_layout.addRow("Protocol Wrapper", self._protocol_wrapper)
        config_layout.addRow("Persona Preset", self._persona_preset)
        if sys.platform.startswith("win"):
            config_layout.addRow("Tunnel Policy", self._full_tunnel)

        action_row = QHBoxLayout()
        self._install_button = QPushButton("Install")
        self._install_button.clicked.connect(self.install_client)
        self._save_button = QPushButton("Save Profile")
        self._save_button.clicked.connect(self.save_profile)
        self._import_button = QPushButton("Import Profile")
        self._import_button.clicked.connect(self.import_profile)
        self._import_token_button = QPushButton("Import Token")
        self._import_token_button.clicked.connect(self.import_profile_token)
        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self.connect_client)
        self._disconnect_button = QPushButton("Disconnect")
        self._disconnect_button.clicked.connect(self.disconnect_client)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.refresh_status)
        for button in (
            self._install_button,
            self._save_button,
            self._import_button,
            self._import_token_button,
            self._connect_button,
            self._disconnect_button,
            self._refresh_button,
        ):
            action_row.addWidget(button)
        config_layout.addRow(action_row)

        status_box = QGroupBox("Status")
        status_layout = QVBoxLayout(status_box)
        self._headline = QLabel("Windows-native Veil tunnel client" if sys.platform.startswith("win") else "Veil VPN client")
        self._headline.setStyleSheet("font-size: 15px; font-weight: 600; color: #345267;")
        self._status_label = QLabel("unknown")
        self._status_label.setStyleSheet("font-size: 20px; font-weight: 700;")
        self._details_label = QLabel("-")
        self._details_label.setWordWrap(True)
        status_layout.addWidget(self._headline)
        status_layout.addWidget(self._status_label)
        status_layout.addWidget(self._details_label)
        status_layout.addStretch(1)

        logs_box = QGroupBox("Logs")
        logs_layout = QVBoxLayout(logs_box)
        self._logs = QPlainTextEdit()
        self._logs.setReadOnly(True)
        self._logs.setFont(QFont("JetBrains Mono", 10))
        logs_layout.addWidget(self._logs)

        layout.addWidget(config_box, 0, 0)
        layout.addWidget(status_box, 1, 0)
        layout.addWidget(logs_box, 0, 1, 2, 1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(1, 1)

        self.load_profile()
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
        self._protocol_wrapper.setCurrentText(config.protocol_wrapper)
        self._persona_preset.setText(config.persona_preset)

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
            "--protocol-wrapper",
            self._protocol_wrapper.currentText().strip(),
            "--persona-preset",
            self._persona_preset.text().strip(),
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
                ]
            )
        result = self._run_ctl(*args)
        if result.returncode != 0:
            self._show_error("Save failed", result.stderr or result.stdout)
            return
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
        ctl_command = self._ctl_command("--config", str(self._config_path), command)
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
        self._details_label.setText("\n".join(detail_lines))
        self._load_log_tail(Path(str(payload.get("log_path", ""))))

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._install_button,
            self._save_button,
            self._import_button,
            self._import_token_button,
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
                "protocol_wrapper": self._protocol_wrapper.currentText().strip(),
                "persona_preset": self._persona_preset.text().strip(),
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
