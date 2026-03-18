from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
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

from veil_core.linux_client_app import LinuxClientPaths, load_client_config


class VeilVpnWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self._paths = LinuxClientPaths.detect(repo_root=ROOT)
        self._config_path = config_path or self._paths.config_path
        self._process: QProcess | None = None
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(1500)

        self.setWindowTitle("Veil VPN Client")
        self.resize(980, 680)

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
        self._tun_name = QLineEdit()
        self._tun_address = QLineEdit()
        self._tun_peer = QLineEdit()
        self._protocol_wrapper = QComboBox()
        self._protocol_wrapper.addItems(["none", "websocket", "tls"])
        self._persona_preset = QLineEdit()

        config_layout.addRow("Server Host", self._server_host)
        config_layout.addRow("Server Port", self._server_port)
        config_layout.addRow("Client Name", self._client_name)
        config_layout.addRow("PSK Hex", self._psk_hex)
        config_layout.addRow("TUN Name", self._tun_name)
        config_layout.addRow("TUN Address", self._tun_address)
        config_layout.addRow("TUN Peer", self._tun_peer)
        config_layout.addRow("Protocol Wrapper", self._protocol_wrapper)
        config_layout.addRow("Persona Preset", self._persona_preset)

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
        self._status_label = QLabel("unknown")
        self._status_label.setStyleSheet("font-size: 20px; font-weight: 700;")
        self._details_label = QLabel("-")
        self._details_label.setWordWrap(True)
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
        return subprocess.run(
            [sys.executable, str(self._paths.ctl_script_path), *args],
            cwd=self._paths.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

    def install_client(self) -> None:
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
        self._tun_name.setText(config.tun_name)
        self._tun_address.setText(config.tun_address)
        self._tun_peer.setText(config.tun_peer)
        self._protocol_wrapper.setCurrentText(config.protocol_wrapper)
        self._persona_preset.setText(config.persona_preset)

    def save_profile(self) -> None:
        result = self._run_ctl(
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
            "--tun-name",
            self._tun_name.text().strip(),
            "--tun-address",
            self._tun_address.text().strip(),
            "--tun-peer",
            self._tun_peer.text().strip(),
            "--protocol-wrapper",
            self._protocol_wrapper.currentText().strip(),
            "--persona-preset",
            self._persona_preset.text().strip(),
        )
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
        self._run_process("up")

    def disconnect_client(self) -> None:
        self._run_process("down")

    def _run_process(self, command: str) -> None:
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self._show_error("Action already running", "Wait for the previous action to finish")
            return
        process = QProcess(self)
        process.setProgram(sys.executable)
        process.setArguments([str(self._paths.ctl_script_path), "--config", str(self._config_path), command])
        process.setWorkingDirectory(str(self._paths.repo_root))
        process.finished.connect(self._process_finished)
        process.start()
        self._process = process

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
        self.refresh_status()

    def refresh_status(self) -> None:
        result = self._run_ctl("--config", str(self._config_path), "status")
        if result.returncode != 0:
            self._status_label.setText("error")
            self._details_label.setText(result.stderr.strip() or result.stdout.strip())
            return
        payload = json.loads(result.stdout)
        running = bool(payload.get("running"))
        tun_exists = bool(payload.get("tun_exists"))
        installed = bool(payload.get("installed"))
        self._status_label.setText(
            "connected" if running and tun_exists else "installed" if installed else "not installed"
        )
        self._details_label.setText(
            f"installed={installed}\n"
            f"running={running}\n"
            f"tun_exists={tun_exists}\n"
            f"log={payload.get('log_path', '-')}\n"
            f"config={payload.get('config_path', '-')}"
        )
        self._load_log_tail(Path(payload.get("log_path", "")))

    def _load_log_tail(self, path: Path) -> None:
        if not path or not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = VeilVpnWindow(config_path=args.config)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
