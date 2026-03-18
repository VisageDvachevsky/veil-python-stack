from __future__ import annotations

import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CLIENT_PSK = bytes.fromhex("ab" * 32)


@dataclass(frozen=True)
class LinuxClientPaths:
    repo_root: Path
    config_dir: Path
    config_path: Path
    data_dir: Path
    bin_dir: Path
    desktop_entry_dir: Path
    desktop_entry_path: Path
    ctl_wrapper_path: Path
    gui_wrapper_path: Path
    ctl_script_path: Path
    gui_script_path: Path
    runtime_dir: Path

    @classmethod
    def detect(cls, *, repo_root: Path | None = None) -> "LinuxClientPaths":
        root = repo_root or Path(__file__).resolve().parents[1]
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        bin_home = Path.home() / ".local" / "bin"
        config_dir = config_home / "veil-vpn"
        data_dir = data_home / "veil-vpn"
        desktop_entry_dir = data_home / "applications"
        return cls(
            repo_root=root,
            config_dir=config_dir,
            config_path=config_dir / "client.json",
            data_dir=data_dir,
            bin_dir=bin_home,
            desktop_entry_dir=desktop_entry_dir,
            desktop_entry_path=desktop_entry_dir / "veil-vpn.desktop",
            ctl_wrapper_path=bin_home / "veil-vpn",
            gui_wrapper_path=bin_home / "veil-vpn-gui",
            ctl_script_path=root / "desktop" / "veil_vpn_ctl.py",
            gui_script_path=root / "desktop" / "veil_vpn_client.py",
            runtime_dir=Path("/run/veil-vpn"),
        )


@dataclass(frozen=True)
class LinuxClientEnvironment:
    python3: str | None
    ip: str | None
    pkexec: str | None
    sudo: str | None
    systemctl: str | None
    nmcli: str | None
    resolvectl: str | None
    tun_device_path: str = "/dev/net/tun"

    @classmethod
    def detect(cls) -> "LinuxClientEnvironment":
        return cls(
            python3=sys.executable,
            ip=shutil.which("ip"),
            pkexec=shutil.which("pkexec"),
            sudo=shutil.which("sudo"),
            systemctl=shutil.which("systemctl"),
            nmcli=shutil.which("nmcli"),
            resolvectl=shutil.which("resolvectl"),
        )

    @property
    def privilege_helper(self) -> str | None:
        if os.geteuid() == 0:
            return None
        if self.pkexec:
            return self.pkexec
        if self.sudo:
            return self.sudo
        return None

    def doctor(self) -> dict[str, Any]:
        tun_access = {
            "path": self.tun_device_path,
            "exists": Path(self.tun_device_path).exists(),
            "readable": os.access(self.tun_device_path, os.R_OK),
            "writable": os.access(self.tun_device_path, os.W_OK),
        }
        try:
            import PyQt6  # noqa: F401

            pyqt6_available = True
        except Exception:
            pyqt6_available = False

        return {
            "python3": self.python3,
            "ip": self.ip,
            "pkexec": self.pkexec,
            "sudo": self.sudo,
            "systemctl": self.systemctl,
            "nmcli": self.nmcli,
            "resolvectl": self.resolvectl,
            "pyqt6_available": pyqt6_available,
            "tun_device": tun_access,
            "is_root": os.geteuid() == 0,
            "privilege_helper": self.privilege_helper,
        }


@dataclass
class LinuxClientConfig:
    server_host: str = "185.23.35.241"
    server_port: int = 4433
    client_name: str = "veil-client"
    psk_hex: str = DEFAULT_CLIENT_PSK.hex()
    tun_name: str = "veilfull0"
    tun_address: str = "10.200.0.2/30"
    tun_peer: str = "10.200.0.1"
    packet_mtu: int = 1300
    keepalive_interval: float = 10.0
    keepalive_timeout: float = 30.0
    reconnect: bool = True
    auto_connect: bool = False
    protocol_wrapper: str = "none"
    persona_preset: str = "custom"
    suspend_conflicting_services: bool = True

    @property
    def psk(self) -> bytes:
        return bytes.fromhex(self.psk_hex)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def shell_env(self) -> dict[str, str]:
        return {
            "SERVER_HOST": self.server_host,
            "SERVER_PORT": str(self.server_port),
            "CLIENT_NAME": self.client_name,
            "PSK_HEX": self.psk_hex,
            "TUN_NAME": self.tun_name,
            "TUN_ADDR": self.tun_address,
            "TUN_PEER": self.tun_peer,
            "PACKET_MTU": str(self.packet_mtu),
            "KEEPALIVE_INTERVAL": str(self.keepalive_interval),
            "KEEPALIVE_TIMEOUT": str(self.keepalive_timeout),
            "PROTOCOL_WRAPPER": self.protocol_wrapper,
            "PERSONA_PRESET": self.persona_preset,
        }


@dataclass(frozen=True)
class LinuxClientRuntimeFiles:
    pid_file: Path
    state_file: Path
    log_file: Path


def runtime_files(paths: LinuxClientPaths, config: LinuxClientConfig) -> LinuxClientRuntimeFiles:
    return LinuxClientRuntimeFiles(
        pid_file=paths.runtime_dir / f"{config.tun_name}.pid",
        state_file=paths.runtime_dir / f"{config.tun_name}.env.json",
        log_file=paths.runtime_dir / f"{config.tun_name}.log",
    )


def load_client_config(path: Path) -> LinuxClientConfig:
    if not path.exists():
        return LinuxClientConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return LinuxClientConfig(**raw)


def save_client_config(path: Path, config: LinuxClientConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.to_json(), encoding="utf-8")


def render_ctl_wrapper(paths: LinuxClientPaths, env: LinuxClientEnvironment) -> str:
    python_bin = env.python3 or sys.executable
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"export PYTHONPATH={shell_quote(str(paths.repo_root))}\n"
        f"exec {shell_quote(python_bin)} {shell_quote(str(paths.ctl_script_path))} \"$@\"\n"
    )


def render_gui_wrapper(paths: LinuxClientPaths, env: LinuxClientEnvironment) -> str:
    python_bin = env.python3 or sys.executable
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"export PYTHONPATH={shell_quote(str(paths.repo_root))}\n"
        f"exec {shell_quote(python_bin)} {shell_quote(str(paths.gui_script_path))} \"$@\"\n"
    )


def render_desktop_entry(paths: LinuxClientPaths) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Veil VPN Client\n"
        "Comment=Linux GUI client for Veil VPN\n"
        f"Exec={paths.gui_wrapper_path}\n"
        "Terminal=false\n"
        "Categories=Network;Security;\n"
    )


def install_user_client(paths: LinuxClientPaths, env: LinuxClientEnvironment) -> dict[str, str]:
    for directory in (paths.config_dir, paths.data_dir, paths.bin_dir, paths.desktop_entry_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if not paths.config_path.exists():
        save_client_config(paths.config_path, LinuxClientConfig())

    paths.ctl_wrapper_path.write_text(render_ctl_wrapper(paths, env), encoding="utf-8")
    paths.gui_wrapper_path.write_text(render_gui_wrapper(paths, env), encoding="utf-8")
    paths.desktop_entry_path.write_text(render_desktop_entry(paths), encoding="utf-8")
    paths.ctl_wrapper_path.chmod(0o755)
    paths.gui_wrapper_path.chmod(0o755)

    return {
        "config_path": str(paths.config_path),
        "ctl_wrapper_path": str(paths.ctl_wrapper_path),
        "gui_wrapper_path": str(paths.gui_wrapper_path),
        "desktop_entry_path": str(paths.desktop_entry_path),
    }


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def build_action_command(
    action: str,
    *,
    config_path: Path,
    config: LinuxClientConfig,
    paths: LinuxClientPaths,
    env: LinuxClientEnvironment,
) -> list[str]:
    internal_command = "internal-up" if action == "up" else "internal-down"

    if os.geteuid() == 0:
        prefix: list[str] = [env.python3 or sys.executable, str(paths.ctl_script_path)]
    else:
        helper = env.privilege_helper
        if helper is None:
            raise RuntimeError("Neither pkexec nor sudo is available for privileged actions")
        prefix = [helper, env.python3 or sys.executable, str(paths.ctl_script_path)]

    command = [
        *prefix,
        "--config",
        str(config_path),
        internal_command,
    ]
    return command


def _run_ip(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["ip", *args], check=check, capture_output=True, text=True)


def _run_command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=check, capture_output=True, text=True)


def _route_info_for_host(host: str) -> dict[str, str]:
    route = _run_ip("-4", "route", "get", host).stdout.strip().split()
    result: dict[str, str] = {"host": host}
    if "dev" in route:
        result["dev"] = route[route.index("dev") + 1]
    if "via" in route:
        result["via"] = route[route.index("via") + 1]
    if "src" in route:
        result["src"] = route[route.index("src") + 1]
    if "dev" not in result:
        raise RuntimeError(f"Could not determine underlay route for {host}")
    return result


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _service_is_active(service_name: str) -> bool:
    if not _systemctl_available():
        return False
    completed = _run_command("systemctl", "is-active", service_name, check=False)
    return completed.returncode == 0 and completed.stdout.strip() == "active"


def _service_is_enabled(service_name: str) -> bool:
    if not _systemctl_available():
        return False
    completed = _run_command("systemctl", "is-enabled", service_name, check=False)
    return completed.returncode == 0 and completed.stdout.strip() in {"enabled", "static", "generated", "alias"}


def _stop_service(service_name: str) -> None:
    if _systemctl_available():
        _run_command("systemctl", "stop", service_name)


def _start_service(service_name: str) -> None:
    if _systemctl_available():
        _run_command("systemctl", "start", service_name)


def _interface_exists(name: str) -> bool:
    return _run_ip("link", "show", "dev", name, check=False).returncode == 0


def _set_nm_managed(name: str, managed: bool) -> None:
    nmcli = shutil.which("nmcli")
    if not nmcli or not _interface_exists(name):
        return
    _run_command(nmcli, "device", "set", name, "managed", "yes" if managed else "no", check=False)


def _detect_conflicting_services() -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    if _service_is_active("clash-verge-service.service") or _interface_exists("Mihomo"):
        conflicts.append(
            {
                "service": "clash-verge-service.service",
                "interface": "Mihomo",
                "was_active": _service_is_active("clash-verge-service.service"),
                "was_enabled": _service_is_enabled("clash-verge-service.service"),
                "interface_present": _interface_exists("Mihomo"),
            }
        )
    return conflicts


def _suspend_conflicting_services() -> list[dict[str, Any]]:
    suspended: list[dict[str, Any]] = []
    for conflict in _detect_conflicting_services():
        if conflict["was_active"]:
            _stop_service(conflict["service"])
            suspended.append(conflict)
    return suspended


def _restore_conflicting_services(conflicts: list[dict[str, Any]]) -> None:
    for conflict in conflicts:
        if conflict.get("was_active"):
            _start_service(conflict["service"])


def _write_runtime_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_runtime_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_tun(name: str, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _run_ip("link", "show", "dev", name, check=False).returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"TUN {name} did not appear in time")


def start_runtime(paths: LinuxClientPaths, config: LinuxClientConfig) -> dict[str, Any]:
    files = runtime_files(paths, config)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    suspended_conflicts: list[dict[str, Any]] = []

    if files.pid_file.exists():
        try:
            pid = int(files.pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            raise RuntimeError(f"Client already running for {config.tun_name} (pid={pid})")
        except ProcessLookupError:
            files.pid_file.unlink(missing_ok=True)

    underlay = _route_info_for_host(config.server_host)
    if "via" in underlay:
        _run_ip("route", "replace", f"{config.server_host}/32", "via", underlay["via"], "dev", underlay["dev"])
    else:
        _run_ip("route", "replace", f"{config.server_host}/32", "dev", underlay["dev"])

    if config.suspend_conflicting_services:
        suspended_conflicts = _suspend_conflicting_services()

    command = [
        env_python := (shutil.which("python3") or sys.executable),
        str(paths.repo_root / "examples" / "linux_vpn_proxy.py"),
        "--mode",
        "client",
        "--host",
        config.server_host,
        "--port",
        str(config.server_port),
        "--tun-name",
        config.tun_name,
        "--tun-address",
        config.tun_address,
        "--tun-peer",
        config.tun_peer,
        "--packet-mtu",
        str(config.packet_mtu),
        "--name",
        config.client_name,
        "--keepalive-interval",
        str(config.keepalive_interval),
        "--keepalive-timeout",
        str(config.keepalive_timeout),
        "--psk-hex",
        config.psk_hex,
        "--protocol-wrapper",
        config.protocol_wrapper,
        "--persona-preset",
        config.persona_preset,
    ]
    if config.reconnect:
        command.append("--reconnect")

    with files.log_file.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=paths.repo_root,
            env={**os.environ, "PYTHONPATH": str(paths.repo_root)},
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    files.pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
    _write_runtime_state(
        files.state_file,
        {
            "server_host": config.server_host,
            "server_port": config.server_port,
            "tun_name": config.tun_name,
            "underlay_route": underlay,
            "suspended_conflicts": suspended_conflicts,
            "python": env_python,
            "command": command,
        },
    )

    try:
        _wait_for_tun(config.tun_name)
        _set_nm_managed(config.tun_name, managed=False)
        _run_ip("route", "replace", "0.0.0.0/1", "dev", config.tun_name)
        _run_ip("route", "replace", "128.0.0.0/1", "dev", config.tun_name)
    except Exception:
        stop_runtime(paths, config)
        raise

    return {
        "started": True,
        "pid": process.pid,
        "tun_name": config.tun_name,
        "log_path": str(files.log_file),
        "suspended_conflicts": suspended_conflicts,
    }


def stop_runtime(paths: LinuxClientPaths, config: LinuxClientConfig) -> dict[str, Any]:
    files = runtime_files(paths, config)
    state = _read_runtime_state(files.state_file)
    suspended_conflicts = list(state.get("suspended_conflicts", []))

    if files.pid_file.exists():
        try:
            pid = int(files.pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.2)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            files.pid_file.unlink(missing_ok=True)

    _run_ip("route", "del", "0.0.0.0/1", "dev", config.tun_name, check=False)
    _run_ip("route", "del", "128.0.0.0/1", "dev", config.tun_name, check=False)
    if config.server_host:
        _run_ip("route", "del", f"{config.server_host}/32", check=False)
    _run_ip("link", "delete", "dev", config.tun_name, check=False)
    _restore_conflicting_services(suspended_conflicts)
    files.state_file.unlink(missing_ok=True)

    return {
        "stopped": True,
        "tun_name": config.tun_name,
        "had_state": bool(state),
        "restored_conflicts": suspended_conflicts,
    }


def read_runtime_status(paths: LinuxClientPaths, config: LinuxClientConfig) -> dict[str, Any]:
    files = runtime_files(paths, config)
    pid: int | None = None
    running = False
    if files.pid_file.exists():
        try:
            pid = int(files.pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            running = True
        except Exception:
            running = False

    tun_exists = subprocess.run(
        ["ip", "link", "show", "dev", config.tun_name],
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0

    return {
        "installed": paths.config_path.exists() and paths.ctl_wrapper_path.exists(),
        "running": running,
        "tun_exists": tun_exists,
        "pid": pid,
        "server_host": config.server_host,
        "server_port": config.server_port,
        "tun_name": config.tun_name,
        "client_name": config.client_name,
        "protocol_wrapper": config.protocol_wrapper,
        "persona_preset": config.persona_preset,
        "suspend_conflicting_services": config.suspend_conflicting_services,
        "config_path": str(paths.config_path),
        "log_path": str(files.log_file),
        "ctl_wrapper_path": str(paths.ctl_wrapper_path),
        "gui_wrapper_path": str(paths.gui_wrapper_path),
        "state_path": str(files.state_file),
    }
