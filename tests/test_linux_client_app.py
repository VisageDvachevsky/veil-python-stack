from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.linux_client_app import (  # noqa: E402
    LinuxClientConfig,
    LinuxClientEnvironment,
    LinuxClientPaths,
    build_action_command,
    install_user_client,
    load_client_config,
    read_runtime_status,
    runtime_files,
    save_client_config,
    start_runtime,
    stop_runtime,
)


class LinuxClientAppTests(unittest.TestCase):
    def make_paths(self, tempdir: str) -> LinuxClientPaths:
        root = Path(tempdir) / "repo"
        (root / "desktop").mkdir(parents=True)
        (root / "examples").mkdir(parents=True)
        return LinuxClientPaths(
            repo_root=root,
            config_dir=Path(tempdir) / "config",
            config_path=Path(tempdir) / "config" / "client.json",
            data_dir=Path(tempdir) / "data",
            bin_dir=Path(tempdir) / "bin",
            desktop_entry_dir=Path(tempdir) / "applications",
            desktop_entry_path=Path(tempdir) / "applications" / "veil-vpn.desktop",
            ctl_wrapper_path=Path(tempdir) / "bin" / "veil-vpn",
            gui_wrapper_path=Path(tempdir) / "bin" / "veil-vpn-gui",
            ctl_script_path=root / "desktop" / "veil_vpn_ctl.py",
            gui_script_path=root / "desktop" / "veil_vpn_client.py",
            runtime_dir=Path(tempdir) / "run",
        )

    def test_save_and_load_client_config(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "client.json"
            config = LinuxClientConfig(server_host="1.2.3.4", client_name="alice", tun_name="veil42")

            save_client_config(path, config)
            loaded = load_client_config(path)

            self.assertEqual(loaded.server_host, "1.2.3.4")
            self.assertEqual(loaded.client_name, "alice")
            self.assertEqual(loaded.tun_name, "veil42")

    def test_install_user_client_writes_wrappers_and_desktop_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = self.make_paths(tempdir)
            env = LinuxClientEnvironment(
                python3="/usr/bin/python3",
                ip="/usr/sbin/ip",
                pkexec="/usr/bin/pkexec",
                sudo="/usr/bin/sudo",
                systemctl="/usr/bin/systemctl",
                nmcli="/usr/bin/nmcli",
                resolvectl=None,
            )

            result = install_user_client(paths, env)

            self.assertTrue(paths.config_path.exists())
            self.assertTrue(paths.ctl_wrapper_path.exists())
            self.assertTrue(paths.gui_wrapper_path.exists())
            self.assertTrue(paths.desktop_entry_path.exists())
            self.assertIn("veil_vpn_ctl.py", paths.ctl_wrapper_path.read_text(encoding="utf-8"))
            self.assertIn("Veil VPN Client", paths.desktop_entry_path.read_text(encoding="utf-8"))
            self.assertEqual(result["config_path"], str(paths.config_path))

    def test_build_action_command_prefers_pkexec(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = self.make_paths(tempdir)
            config = LinuxClientConfig(
                server_host="8.8.8.8",
                tun_name="veiltest0",
                psk_hex="11" * 32,
                protocol_wrapper="websocket",
                persona_preset="browser_ws",
            )
            env = LinuxClientEnvironment(
                python3="/usr/bin/python3",
                ip="/usr/sbin/ip",
                pkexec="/usr/bin/pkexec",
                sudo="/usr/bin/sudo",
                systemctl="/usr/bin/systemctl",
                nmcli=None,
                resolvectl=None,
            )

            with mock.patch("os.geteuid", return_value=1000):
                command = build_action_command(
                    "up",
                    config_path=Path(tempdir) / "alt-client.json",
                    config=config,
                    paths=paths,
                    env=env,
                )

            self.assertEqual(command[:3], ["/usr/bin/pkexec", "/usr/bin/python3", str(paths.ctl_script_path)])
            self.assertEqual(command[-3:], ["--config", str(Path(tempdir) / "alt-client.json"), "internal-up"])

    def test_read_runtime_status_reports_process_and_tun(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = self.make_paths(tempdir)
            config = LinuxClientConfig(tun_name="veiltest0")
            paths.config_dir.mkdir(parents=True, exist_ok=True)
            paths.bin_dir.mkdir(parents=True, exist_ok=True)
            paths.config_path.write_text(json.dumps({}), encoding="utf-8")
            paths.ctl_wrapper_path.write_text("#!/bin/sh\n", encoding="utf-8")

            files = runtime_files(paths, config)
            files.pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file = files.pid_file
            pid_file.write_text("12345\n", encoding="utf-8")
            self.addCleanup(lambda: pid_file.unlink(missing_ok=True))

            with (
                mock.patch("os.kill", return_value=None),
                mock.patch("subprocess.run") as run_mock,
            ):
                run_mock.return_value.returncode = 0
                payload = read_runtime_status(paths, config)

            self.assertTrue(payload["installed"])
            self.assertTrue(payload["running"])
            self.assertTrue(payload["tun_exists"])
            self.assertEqual(payload["pid"], 12345)
            self.assertEqual(payload["tun_name"], "veiltest0")

    def test_environment_doctor_reports_tun_and_pyqt(self) -> None:
        env = LinuxClientEnvironment(
            python3="/usr/bin/python3",
            ip="/usr/sbin/ip",
            pkexec="/usr/bin/pkexec",
            sudo="/usr/bin/sudo",
            systemctl="/usr/bin/systemctl",
            nmcli="/usr/bin/nmcli",
            resolvectl=None,
        )

        payload = env.doctor()

        self.assertIn("tun_device", payload)
        self.assertIn("pyqt6_available", payload)

    def test_start_and_stop_runtime_manage_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = self.make_paths(tempdir)
            config = LinuxClientConfig(server_host="8.8.8.8", tun_name="veilrt0")
            files = runtime_files(paths, config)

            process = mock.Mock()
            process.pid = 4242

            def fake_run(args, check=True, capture_output=True, text=True):
                joined = " ".join(args)
                result = mock.Mock()
                result.returncode = 0
                if "route get 8.8.8.8" in joined:
                    result.stdout = "8.8.8.8 via 192.168.0.1 dev eth0 src 192.168.0.2 cache\n"
                else:
                    result.stdout = ""
                return result

            with (
                mock.patch("subprocess.run", side_effect=fake_run),
                mock.patch("subprocess.Popen", return_value=process),
                mock.patch("os.kill", side_effect=[None, ProcessLookupError()]),
            ):
                payload = start_runtime(paths, config)
                self.assertTrue(files.pid_file.exists())
                self.assertTrue(files.state_file.exists())
                self.assertEqual(payload["pid"], 4242)

                stopped = stop_runtime(paths, config)
                self.assertTrue(stopped["stopped"])
                self.assertFalse(files.pid_file.exists())

    def test_start_and_stop_runtime_suspend_and_restore_conflicting_service(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = self.make_paths(tempdir)
            config = LinuxClientConfig(server_host="8.8.8.8", tun_name="veilrt1", suspend_conflicting_services=True)
            files = runtime_files(paths, config)

            process = mock.Mock()
            process.pid = 5151
            calls: list[list[str]] = []

            def fake_run(args, check=True, capture_output=True, text=True):
                calls.append(args)
                joined = " ".join(args)
                result = mock.Mock()
                result.returncode = 0
                result.stdout = ""
                if joined == "ip -4 route get 8.8.8.8":
                    result.stdout = "8.8.8.8 via 192.168.0.1 dev eth0 src 192.168.0.2 cache\n"
                elif joined == "systemctl is-active clash-verge-service.service":
                    result.stdout = "active\n"
                elif joined == "systemctl is-enabled clash-verge-service.service":
                    result.stdout = "enabled\n"
                return result

            with (
                mock.patch("subprocess.run", side_effect=fake_run),
                mock.patch("subprocess.Popen", return_value=process),
                mock.patch("os.kill", side_effect=[None, ProcessLookupError()]),
                mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            ):
                payload = start_runtime(paths, config)
                state = json.loads(files.state_file.read_text(encoding="utf-8"))
                self.assertEqual(payload["suspended_conflicts"][0]["service"], "clash-verge-service.service")
                self.assertEqual(state["suspended_conflicts"][0]["interface"], "Mihomo")

                stopped = stop_runtime(paths, config)
                self.assertEqual(stopped["restored_conflicts"][0]["service"], "clash-verge-service.service")

            joined_calls = [" ".join(call) for call in calls]
            self.assertIn("systemctl stop clash-verge-service.service", joined_calls)
            self.assertIn("systemctl start clash-verge-service.service", joined_calls)
            self.assertIn("/usr/bin/nmcli device set veilrt1 managed no", joined_calls)


if __name__ == "__main__":
    unittest.main()
