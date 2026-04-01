from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.linux_server_app import (  # noqa: E402
    LinuxServerClientConfig,
    LinuxServerConfig,
    LinuxServerEnvironment,
    LinuxServerPaths,
    install_server_assets,
    load_server_config,
    read_server_status,
    save_server_config,
    write_client_profile,
)


class LinuxServerAppTests(unittest.TestCase):
    def make_paths(self, tempdir: str) -> LinuxServerPaths:
        root = Path(tempdir) / "repo"
        (root / "desktop").mkdir(parents=True)
        return LinuxServerPaths(
            repo_root=root,
            config_dir=Path(tempdir) / "etc",
            config_path=Path(tempdir) / "etc" / "server.json",
            state_dir=Path(tempdir) / "state",
            client_profile_path=Path(tempdir) / "state" / "client-profile.json",
            service_path=Path(tempdir) / "service" / "veil-vpn-server.service",
            launcher_path=Path(tempdir) / "bin" / "veil-vpn-server",
            ctl_script_path=root / "desktop" / "veil_vpn_server_ctl.py",
            server_script_path=root / "desktop" / "veil_vpn_server.py",
        )

    def test_save_and_load_server_config(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "server.json"
            config = LinuxServerConfig(
                public_host="vpn.example",
                psk_hex="13" * 32,
                clients=[LinuxServerClientConfig(client_id="alice", psk_hex="12" * 32)],
            )
            save_server_config(path, config)
            loaded = load_server_config(path)
            self.assertEqual(loaded.public_host, "vpn.example")
            self.assertEqual(loaded.psk_hex, "13" * 32)
            self.assertEqual(len(loaded.clients or []), 1)
            self.assertEqual(loaded.clients[0].client_id, "alice")

    def test_install_server_assets_writes_config_profile_and_service(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = self.make_paths(tempdir)
            env = LinuxServerEnvironment(
                python3="/usr/bin/python3",
                ip="/usr/sbin/ip",
                iptables="/usr/sbin/iptables",
                systemctl="/usr/bin/systemctl",
            )
            config = LinuxServerConfig(
                public_host="vpn.example",
                public_interface="eth0",
                psk_hex="14" * 32,
            )

            with mock.patch("os.geteuid", return_value=0):
                result = install_server_assets(paths, env, config)

            self.assertTrue(paths.config_path.exists())
            self.assertTrue(paths.client_profile_path.exists())
            self.assertTrue(paths.service_path.exists())
            self.assertTrue(paths.launcher_path.exists())
            self.assertEqual(result["config_path"], str(paths.config_path))
            self.assertIn("vpn.example", paths.client_profile_path.read_text(encoding="utf-8"))

    def test_write_client_profile_writes_requested_path(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "profiles" / "client.json"
            config = LinuxServerConfig(
                public_host="vpn.example",
                public_interface="eth0",
                psk_hex="15" * 32,
                enable_http_handshake_emulation=True,
            )
            written = write_client_profile(output, config)
            self.assertEqual(written, output)
            payload = output.read_text(encoding="utf-8")
            self.assertIn("15" * 32, payload)
            self.assertIn('"tunnel_mode": "dynamic"', payload)
            self.assertIn('"tun_peer": ""', payload)
            self.assertIn('"enable_http_handshake_emulation": true', payload)

    def test_write_client_profile_prefers_first_enabled_multi_client_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "profiles" / "client.json"
            config = LinuxServerConfig(
                public_host="vpn.example",
                public_interface="eth0",
                clients=[
                    LinuxServerClientConfig(client_id="alice", client_name="Alice", psk_hex="21" * 32, enabled=True),
                    LinuxServerClientConfig(client_id="bob", client_name="Bob", psk_hex="22" * 32, enabled=False),
                ],
            )
            written = write_client_profile(output, config)
            self.assertEqual(written, output)
            payload = output.read_text(encoding="utf-8")
            self.assertIn('"client_id": "alice"', payload)
            self.assertIn('"client_name": "Alice"', payload)
            self.assertIn("21" * 32, payload)

    def test_read_server_status_reports_service_and_profile_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = self.make_paths(tempdir)
            config = LinuxServerConfig(
                public_host="vpn.example",
                public_interface="eth0",
                psk_hex="16" * 32,
                protocol_wrapper="websocket",
                persona_preset="browser_ws",
                enable_http_handshake_emulation=True,
            )
            paths.config_dir.mkdir(parents=True, exist_ok=True)
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            paths.launcher_path.parent.mkdir(parents=True, exist_ok=True)
            paths.service_path.parent.mkdir(parents=True, exist_ok=True)
            paths.config_path.write_text(config.to_json(), encoding="utf-8")
            paths.launcher_path.write_text("#!/bin/sh\n", encoding="utf-8")
            paths.service_path.write_text("[Service]\n", encoding="utf-8")

            def fake_run(args, check=False, capture_output=True, text=True):
                result = mock.Mock()
                result.returncode = 0
                result.stdout = "active\n" if args[-2:] == ["is-active", paths.service_path.name] else "enabled\n"
                return result

            with (
                mock.patch("shutil.which", return_value="/usr/bin/systemctl"),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                payload = read_server_status(paths, config)

            self.assertTrue(payload["installed"])
            self.assertTrue(payload["service_active"])
            self.assertTrue(payload["service_enabled"])
            self.assertEqual(payload["client_profile_summary"]["protocol_details"]["wrapper"]["value"], "websocket")


if __name__ == "__main__":
    unittest.main()
