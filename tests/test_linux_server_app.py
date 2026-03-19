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
    LinuxServerConfig,
    LinuxServerEnvironment,
    LinuxServerPaths,
    install_server_assets,
    load_server_config,
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
            config = LinuxServerConfig(public_host="vpn.example", psk_hex="13" * 32)
            save_server_config(path, config)
            loaded = load_server_config(path)
            self.assertEqual(loaded.public_host, "vpn.example")
            self.assertEqual(loaded.psk_hex, "13" * 32)

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
            )
            written = write_client_profile(output, config)
            self.assertEqual(written, output)
            self.assertIn("15" * 32, output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
