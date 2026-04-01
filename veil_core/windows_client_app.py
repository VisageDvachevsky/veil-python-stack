from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from dataclasses import fields
from pathlib import Path
from typing import Any

from veil_core.windows_wintun import NetworkRouteSnapshot, WintunDll, WintunSession, resolve_ipv4


DEFAULT_CLIENT_PSK = bytes.fromhex("11" * 32)


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _default_program_data() -> Path:
    return Path(os.environ.get("PROGRAMDATA", Path.home() / "AppData" / "Local" / "ProgramData"))


def _default_app_data() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


def _default_local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


@dataclass(frozen=True)
class WindowsClientPaths:
    repo_root: Path
    config_dir: Path
    config_path: Path
    shared_dir: Path
    runtime_dir: Path
    log_dir: Path
    agent_command_path: Path
    agent_state_path: Path
    agent_pid_path: Path
    agent_log_path: Path
    gui_lock_path: Path
    ctl_script_path: Path
    gui_script_path: Path
    agent_script_path: Path
    wintun_dll_path: Path

    @classmethod
    def detect(cls, *, repo_root: Path | None = None) -> "WindowsClientPaths":
        source_root = repo_root or Path(__file__).resolve().parents[1]
        root = Path(sys.executable).resolve().parent if _is_frozen() else source_root
        config_dir = _default_app_data() / "VeilVPN"
        shared_dir = _default_program_data() / "VeilVPN"
        runtime_dir = shared_dir / "runtime"
        log_dir = _default_local_app_data() / "VeilVPN" / "logs"
        return cls(
            repo_root=root,
            config_dir=config_dir,
            config_path=config_dir / "client.json",
            shared_dir=shared_dir,
            runtime_dir=runtime_dir,
            log_dir=log_dir,
            agent_command_path=runtime_dir / "agent-command.json",
            agent_state_path=runtime_dir / "agent-state.json",
            agent_pid_path=runtime_dir / "agent.pid",
            agent_log_path=log_dir / "agent.log",
            gui_lock_path=runtime_dir / "gui.lock",
            ctl_script_path=(root / "veil-vpn-agent.exe") if _is_frozen() else (root / "desktop" / "veil_vpn_windows_ctl.py"),
            gui_script_path=(root / "veil-vpn-client.exe") if _is_frozen() else (root / "desktop" / "veil_vpn_client.py"),
            agent_script_path=(root / "veil-vpn-agent.exe") if _is_frozen() else (root / "desktop" / "veil_vpn_agent.py"),
            wintun_dll_path=(root / "wintun.dll") if _is_frozen() else (root / "desktop" / "wintun.dll"),
        )


@dataclass(frozen=True)
class WindowsClientEnvironment:
    python3: str | None
    powershell: str | None
    pwsh: str | None
    netsh: str | None
    route: str | None
    sc: str | None
    pywin32_available: bool
    is_admin: bool

    @classmethod
    def detect(cls) -> "WindowsClientEnvironment":
        try:
            import win32serviceutil  # type: ignore  # noqa: F401

            pywin32_available = True
        except Exception:
            pywin32_available = False

        return cls(
            python3=sys.executable,
            powershell=shutil.which("powershell"),
            pwsh=shutil.which("pwsh"),
            netsh=shutil.which("netsh"),
            route=shutil.which("route"),
            sc=shutil.which("sc"),
            pywin32_available=pywin32_available,
            is_admin=_is_process_admin(),
        )

    def doctor(self, paths: WindowsClientPaths) -> dict[str, Any]:
        return {
            "platform": "windows" if _is_windows() else sys.platform,
            "python3": self.python3,
            "powershell": self.powershell,
            "pwsh": self.pwsh,
            "netsh": self.netsh,
            "route": self.route,
            "sc": self.sc,
            "pywin32_available": self.pywin32_available,
            "is_admin": self.is_admin,
            "wintun_dll_exists": paths.wintun_dll_path.exists(),
            "config_path": str(paths.config_path),
            "agent_state_path": str(paths.agent_state_path),
        }


def _is_process_admin() -> bool:
    if not _is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass
class WindowsClientConfig:
    server_host: str = "vpn.example"
    server_port: int = 4433
    client_name: str = "veil-client"
    client_id: str = ""
    psk_hex: str = DEFAULT_CLIENT_PSK.hex()
    adapter_name: str = "VeilVPN"
    tun_address: str = "10.200.0.2/30"
    tun_peer: str = "10.200.0.1"
    packet_mtu: int = 1300
    keepalive_interval: float = 10.0
    keepalive_timeout: float = 30.0
    reconnect: bool = True
    auto_connect: bool = False
    full_tunnel: bool = True
    disable_system_proxy: bool = True
    dns_servers: tuple[str, ...] = ("1.1.1.1", "1.0.0.1")
    protocol_wrapper: str = "none"
    persona_preset: str = "custom"

    @property
    def psk(self) -> bytes:
        return bytes.fromhex(self.psk_hex)

    @property
    def tun_ipv4(self) -> tuple[str, int]:
        interface = ipaddress.IPv4Interface(self.tun_address)
        return str(interface.ip), interface.network.prefixlen

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_client_config(path: Path) -> WindowsClientConfig:
    if not path.exists():
        return WindowsClientConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "adapter_name" not in raw and "tun_name" in raw:
        raw["adapter_name"] = raw["tun_name"]
    allowed = {field.name for field in fields(WindowsClientConfig)}
    filtered = {key: value for key, value in raw.items() if key in allowed}
    config = WindowsClientConfig(**filtered)
    if _looks_like_legacy_placeholder_config(config):
        defaults = WindowsClientConfig()
        merged = {
            **config.__dict__,
            "server_host": defaults.server_host,
            "server_port": defaults.server_port,
            "psk_hex": defaults.psk_hex,
            "adapter_name": defaults.adapter_name,
            "tun_address": defaults.tun_address,
            "tun_peer": defaults.tun_peer,
            "packet_mtu": defaults.packet_mtu,
            "keepalive_interval": defaults.keepalive_interval,
            "keepalive_timeout": defaults.keepalive_timeout,
            "protocol_wrapper": defaults.protocol_wrapper,
            "persona_preset": defaults.persona_preset,
        }
        config = WindowsClientConfig(**merged)
        save_client_config(path, config)
    return config


def _looks_like_legacy_placeholder_config(config: WindowsClientConfig) -> bool:
    return (
        config.adapter_name == "LegacyTun"
        or config.tun_address == "10.0.0.2/30"
        or config.tun_peer == "10.0.0.1"
    )


def save_client_config(path: Path, config: WindowsClientConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.to_json(), encoding="utf-8")


def install_windows_client(paths: WindowsClientPaths, env: WindowsClientEnvironment) -> dict[str, Any]:
    for directory in (paths.config_dir, paths.shared_dir, paths.runtime_dir, paths.log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if not paths.config_path.exists():
        save_client_config(paths.config_path, WindowsClientConfig())

    if not paths.agent_state_path.exists():
        write_runtime_state(
            paths,
            {
                "installed": True,
                "running": False,
                "connected": False,
                "pid": None,
                "mode": "windows-agent",
                "tunnel_backend": "pending_wintun",
                "last_error": "",
                "last_event": "install",
                "updated_at": _now_ts(),
            },
        )

    return {
        "config_path": str(paths.config_path),
        "shared_dir": str(paths.shared_dir),
        "runtime_dir": str(paths.runtime_dir),
        "log_path": str(paths.agent_log_path),
        "wintun_dll_path": str(paths.wintun_dll_path),
        "pywin32_available": env.pywin32_available,
    }


def write_runtime_command(paths: WindowsClientPaths, command: str, config: WindowsClientConfig) -> Path:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "command": command,
        "config": asdict(config),
        "issued_at": _now_ts(),
    }
    paths.agent_command_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths.agent_command_path


def read_runtime_state(paths: WindowsClientPaths) -> dict[str, Any]:
    if not paths.agent_state_path.exists():
        return {
            "installed": False,
            "running": False,
            "connected": False,
            "pid": None,
            "mode": "windows-agent",
            "tunnel_backend": "pending_wintun",
            "last_error": "",
            "last_event": "unknown",
            "updated_at": "",
        }
    return json.loads(paths.agent_state_path.read_text(encoding="utf-8"))


def read_agent_pid(paths: WindowsClientPaths) -> int | None:
    if not paths.agent_pid_path.exists():
        return None
    try:
        return int(paths.agent_pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_agent_pid(paths: WindowsClientPaths, pid: int | None) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    if pid is None:
        paths.agent_pid_path.unlink(missing_ok=True)
        return
    paths.agent_pid_path.write_text(f"{pid}\n", encoding="utf-8")


def write_runtime_state(paths: WindowsClientPaths, payload: dict[str, Any]) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.agent_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_runtime_status(paths: WindowsClientPaths, config: WindowsClientConfig) -> dict[str, Any]:
    state = read_runtime_state(paths)
    pid = read_agent_pid(paths) or state.get("pid")
    running = bool(state.get("running")) and _pid_alive(pid)
    connected = bool(state.get("connected")) and running
    proxy_state = query_system_proxy_state(WindowsClientEnvironment.detect()) if _is_windows() else {}
    status = {
        **state,
        "installed": bool(paths.config_path.exists()),
        "running": running,
        "connected": connected,
        "pid": pid,
        "config_path": str(paths.config_path),
        "log_path": str(paths.agent_log_path),
        "command_path": str(paths.agent_command_path),
        "adapter_name": config.adapter_name,
        "tun_address": config.tun_address,
        "tun_peer": config.tun_peer,
        "wintun_dll_exists": paths.wintun_dll_path.exists(),
        "proxy_enabled": bool(proxy_state.get("enabled", False)),
        "proxy_server": proxy_state.get("server", ""),
        "proxy_bypass": proxy_state.get("override", ""),
    }
    if state.get("running") and not running:
        status["last_error"] = status.get("last_error") or "agent_process_not_running"
    return status


def launch_agent(paths: WindowsClientPaths, env: WindowsClientEnvironment, config_path: Path) -> dict[str, Any]:
    if env.python3 is None and paths.agent_script_path.suffix.lower() != ".exe":
        raise RuntimeError("Python executable not detected")
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)

    existing = read_runtime_state(paths)
    pid = read_agent_pid(paths) or existing.get("pid")
    if existing.get("running") and _pid_alive(pid):
        return {"launched": False, "pid": pid}
    if not paths.agent_script_path.exists():
        state = {
            **existing,
            "installed": True,
            "running": False,
            "connected": False,
            "pid": None,
            "mode": "windows-agent",
            "tunnel_backend": "pending_wintun",
            "last_error": "agent_script_missing",
            "last_event": "launch_skipped",
            "updated_at": _now_ts(),
        }
        write_runtime_state(paths, state)
        return {"launched": False, "pid": None, "reason": "agent_script_missing"}

    with paths.agent_log_path.open("a", encoding="utf-8") as log_file:
        command = (
            [str(paths.agent_script_path), "run-daemon", "--config", str(config_path)]
            if paths.agent_script_path.suffix.lower() == ".exe"
            else [env.python3, str(paths.agent_script_path), "run-daemon", "--config", str(config_path)]
        )
        process = subprocess.Popen(
            command,
            cwd=paths.repo_root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    state = {
        **existing,
        "installed": True,
        "running": True,
        "connected": False,
        "pid": process.pid,
        "mode": "windows-agent",
        "tunnel_backend": "pending_wintun",
        "last_error": "",
        "last_event": "agent_launched",
        "updated_at": _now_ts(),
    }
    write_agent_pid(paths, process.pid)
    write_runtime_state(paths, state)
    return {"launched": True, "pid": process.pid}


def start_runtime(paths: WindowsClientPaths, env: WindowsClientEnvironment, config: WindowsClientConfig, *, config_path: Path) -> dict[str, Any]:
    if not env.is_admin:
        raise RuntimeError("Administrator rights are required. Restart Veil VPN as administrator.")
    launch = launch_agent(paths, env, config_path)
    write_runtime_command(paths, "up", config)
    return {
        "ok": True,
        "command": "up",
        "agent": launch,
        "command_path": str(paths.agent_command_path),
    }


def stop_runtime(paths: WindowsClientPaths, config: WindowsClientConfig) -> dict[str, Any]:
    write_runtime_command(paths, "down", config)
    return {
        "ok": True,
        "command": "down",
        "command_path": str(paths.agent_command_path),
    }


def mark_agent_stopped(paths: WindowsClientPaths, *, reason: str) -> None:
    state = read_runtime_state(paths)
    state.update(
        {
            "running": False,
            "connected": False,
            "pid": None,
            "last_event": "agent_stopped",
            "last_error": reason,
            "updated_at": _now_ts(),
        }
    )
    write_agent_pid(paths, None)
    write_runtime_state(paths, state)


def _pid_alive(pid: Any) -> bool:
    if not pid:
        return False
    if _is_windows():
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_powershell(env: WindowsClientEnvironment, command: str) -> subprocess.CompletedProcess[str]:
    shell = env.pwsh or env.powershell
    if shell is None:
        raise RuntimeError("PowerShell is required on Windows")
    return subprocess.run(
        [shell, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def query_system_proxy_state(env: WindowsClientEnvironment) -> dict[str, Any]:
    completed = run_powershell(
        env,
        """
$settings = Get-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings'
[pscustomobject]@{
  enabled = [bool]$settings.ProxyEnable
  server = [string]$settings.ProxyServer
  override = [string]$settings.ProxyOverride
} | ConvertTo-Json -Compress
""",
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return {"enabled": False, "server": "", "override": ""}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"enabled": False, "server": "", "override": ""}


def disable_system_proxy(env: WindowsClientEnvironment) -> dict[str, Any]:
    state = query_system_proxy_state(env)
    if not state.get("enabled"):
        return state
    completed = run_powershell(
        env,
        """
Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' -Name ProxyEnable -Value 0
""",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Failed to disable Windows proxy: {completed.stderr or completed.stdout}")
    return state


def restore_system_proxy(env: WindowsClientEnvironment, state: dict[str, Any] | None) -> None:
    if not state:
        return
    enabled = 1 if state.get("enabled") else 0
    server = str(state.get("server", ""))
    override = str(state.get("override", ""))
    completed = run_powershell(
        env,
        """
$path = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings'
Set-ItemProperty -Path $path -Name ProxyServer -Value '{server}'
Set-ItemProperty -Path $path -Name ProxyOverride -Value '{override}'
Set-ItemProperty -Path $path -Name ProxyEnable -Value {enabled}
""".format(
            server=server.replace("'", "''"),
            override=override.replace("'", "''"),
            enabled=enabled,
        ),
    )
    _ = completed


def query_underlay_route(env: WindowsClientEnvironment, server_host: str) -> NetworkRouteSnapshot:
    server_ip = resolve_ipv4(server_host)
    command = (
        "$route = Find-NetRoute -RemoteIPAddress '{server_ip}' | "
        "Sort-Object -Property RouteMetric,InterfaceMetric | "
        "Select-Object -First 1 InterfaceAlias,InterfaceIndex,NextHop; "
        "if (-not $route) {{ exit 2 }}; $route | ConvertTo-Json -Compress"
    ).format(server_ip=server_ip)
    completed = run_powershell(env, command)
    if completed.returncode != 0:
        raise RuntimeError(f"Failed to discover underlay route: {completed.stderr or completed.stdout}")
    payload = json.loads(completed.stdout)
    return NetworkRouteSnapshot(
        interface_alias=str(payload["InterfaceAlias"]),
        interface_index=int(payload["InterfaceIndex"]),
        next_hop=str(payload["NextHop"]),
        server_ip=server_ip,
    )


def wait_for_adapter(env: WindowsClientEnvironment, adapter_name: str, timeout_seconds: float = 8.0) -> int:
    deadline = time.time() + timeout_seconds
    command = (
        "$adapter = Get-NetAdapter -Name '{adapter}' -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 ifIndex; "
        "if ($adapter) {{ $adapter.ifIndex }}"
    ).format(adapter=adapter_name.replace("'", "''"))
    while time.time() < deadline:
        completed = run_powershell(env, command)
        if completed.returncode == 0 and completed.stdout.strip():
            return int(completed.stdout.strip())
        time.sleep(0.3)
    raise RuntimeError(f"Wintun adapter '{adapter_name}' did not appear in time")


def configure_wintun_network(env: WindowsClientEnvironment, config: WindowsClientConfig) -> dict[str, Any]:
    if not env.is_admin:
        raise RuntimeError("Administrator rights are required to configure Wintun routes and addresses")
    underlay = query_underlay_route(env, config.server_host)
    interface_index = wait_for_adapter(env, config.adapter_name)
    local_ip, prefix_length = config.tun_ipv4
    proxy_state = disable_system_proxy(env) if config.disable_system_proxy else {"enabled": False, "server": "", "override": ""}

    configure_command = """
$alias = '{alias}'
$ip = '{ip}'
$prefix = {prefix}
$mtu = {mtu}
Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
Get-NetRoute -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceAlias $alias -IPAddress $ip -PrefixLength $prefix -AddressFamily IPv4 | Out-Null
""".format(
        alias=config.adapter_name.replace("'", "''"),
        ip=local_ip,
        prefix=prefix_length,
        mtu=config.packet_mtu,
    )
    completed = run_powershell(env, configure_command)
    if completed.returncode != 0:
        raise RuntimeError(f"Failed to configure Wintun address: {completed.stderr or completed.stdout}")

    dns_servers = [server for server in config.dns_servers if server]
    if dns_servers:
        dns_list = ",".join(f"'{server}'" for server in dns_servers)
        dns_command = """
$alias = '{alias}'
$servers = @({servers})
Set-DnsClientServerAddress -InterfaceAlias $alias -ServerAddresses $servers | Out-Null
""".format(
            alias=config.adapter_name.replace("'", "''"),
            servers=dns_list,
        )
        dns_completed = run_powershell(env, dns_command)
        if dns_completed.returncode != 0:
            raise RuntimeError(f"Failed to configure DNS servers: {dns_completed.stderr or dns_completed.stdout}")

    if env.netsh:
        subprocess.run(
            [env.netsh, "interface", "ipv4", "set", "subinterface", config.adapter_name, f"mtu={config.packet_mtu}", "store=active"],
            capture_output=True,
            text=True,
            check=False,
        )

    route_commands = [
        [env.route or "route", "add", underlay.server_ip, "mask", "255.255.255.255", underlay.next_hop, "if", str(underlay.interface_index), "metric", "1"],
    ]
    if config.full_tunnel:
        route_commands.extend(
            [
                [env.route or "route", "add", "0.0.0.0", "mask", "128.0.0.0", config.tun_peer, "if", str(interface_index), "metric", "5"],
                [env.route or "route", "add", "128.0.0.0", "mask", "128.0.0.0", config.tun_peer, "if", str(interface_index), "metric", "5"],
            ]
        )

    for command in route_commands:
        subprocess.run(command, capture_output=True, text=True, check=False)

    return {
        "underlay_interface_index": underlay.interface_index,
        "underlay_interface_alias": underlay.interface_alias,
        "underlay_next_hop": underlay.next_hop,
        "server_ip": underlay.server_ip,
        "adapter_interface_index": interface_index,
        "adapter_name": config.adapter_name,
        "local_ip": local_ip,
        "prefix_length": prefix_length,
        "full_tunnel": config.full_tunnel,
        "dns_servers": dns_servers,
        "proxy_state": proxy_state,
    }


def cleanup_wintun_network(env: WindowsClientEnvironment, config: WindowsClientConfig, network_state: dict[str, Any] | None) -> None:
    if not env.is_admin:
        return
    reset_dns = run_powershell(
        env,
        """
$alias = '{alias}'
Set-DnsClientServerAddress -InterfaceAlias $alias -ResetServerAddresses -ErrorAction SilentlyContinue | Out-Null
""".format(alias=config.adapter_name.replace("'", "''")),
    )
    _ = reset_dns
    if env.route and network_state is not None:
        subprocess.run([env.route, "delete", network_state.get("server_ip", config.server_host)], capture_output=True, text=True, check=False)
        if config.full_tunnel:
            subprocess.run([env.route, "delete", "0.0.0.0", "mask", "128.0.0.0"], capture_output=True, text=True, check=False)
            subprocess.run([env.route, "delete", "128.0.0.0", "mask", "128.0.0.0"], capture_output=True, text=True, check=False)
    restore_system_proxy(env, (network_state or {}).get("proxy_state"))


def create_wintun_session(paths: WindowsClientPaths, config: WindowsClientConfig) -> WintunSession:
    return WintunSession(WintunDll(paths.wintun_dll_path), config.adapter_name)
