from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.windows_client_app import (  # noqa: E402
    WindowsClientConfig,
    WindowsClientEnvironment,
    WindowsClientPaths,
    install_windows_client,
    load_client_config,
    read_runtime_status,
    save_client_config,
    start_runtime,
    stop_runtime,
)


def make_test_paths(repo_root: Path) -> WindowsClientPaths:
    return WindowsClientPaths(
        repo_root=repo_root,
        config_dir=repo_root / "config",
        config_path=repo_root / "config" / "client.json",
        shared_dir=repo_root / "shared",
        runtime_dir=repo_root / "shared" / "runtime",
        log_dir=repo_root / "logs",
        agent_command_path=repo_root / "shared" / "runtime" / "agent-command.json",
        agent_state_path=repo_root / "shared" / "runtime" / "agent-state.json",
        agent_pid_path=repo_root / "shared" / "runtime" / "agent.pid",
        agent_log_path=repo_root / "logs" / "agent.log",
        gui_lock_path=repo_root / "shared" / "runtime" / "gui.lock",
        ctl_script_path=repo_root / "desktop" / "veil_vpn_windows_ctl.py",
        gui_script_path=repo_root / "desktop" / "veil_vpn_client.py",
        agent_script_path=repo_root / "desktop" / "veil_vpn_agent.py",
        wintun_dll_path=repo_root / "desktop" / "wintun.dll",
    )


class WindowsClientAppTests(unittest.TestCase):
    def test_install_and_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir)
            paths = make_test_paths(repo_root)
            env = WindowsClientEnvironment.detect()
            payload = install_windows_client(paths, env)
            self.assertTrue(Path(payload["config_path"]).exists())

            config = WindowsClientConfig(server_host="vpn.test.local", adapter_name="TestVeil")
            save_client_config(paths.config_path, config)
            loaded = load_client_config(paths.config_path)
            self.assertEqual(loaded.server_host, "vpn.test.local")
            self.assertEqual(loaded.adapter_name, "TestVeil")

    def test_loader_accepts_legacy_tun_name_field(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir)
            paths = make_test_paths(repo_root)
            paths.config_dir.mkdir(parents=True, exist_ok=True)
            paths.config_path.write_text(
                json.dumps(
                    {
                        "server_host": "vpn.example",
                        "server_port": 4433,
                        "client_name": "veil-client",
                        "psk_hex": "12" * 32,
                        "tun_name": "LegacyTun",
                        "tun_address": "10.0.0.2/30",
                        "tun_peer": "10.0.0.1",
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_client_config(paths.config_path)
            self.assertEqual(loaded.adapter_name, "VeilVPN")
            self.assertEqual(loaded.server_host, "185.23.35.241")

    def test_runtime_commands_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir)
            paths = make_test_paths(repo_root)
            env = replace(WindowsClientEnvironment.detect(), is_admin=True)
            install_windows_client(paths, env)
            config = load_client_config(paths.config_path)

            up_payload = start_runtime(paths, env, config, config_path=paths.config_path)
            self.assertEqual(up_payload["command"], "up")
            command = json.loads(paths.agent_command_path.read_text(encoding="utf-8"))
            self.assertEqual(command["command"], "up")

            down_payload = stop_runtime(paths, config)
            self.assertEqual(down_payload["command"], "down")
            status = read_runtime_status(paths, config)
            self.assertTrue(status["installed"])


if __name__ == "__main__":
    unittest.main()
