from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SERVER_TUN_IP = "10.200.0.1"
CLIENT_TUN_IP = "10.200.0.2"
SERVER_UNDERLAY_IP = "10.100.0.1"
CLIENT_UNDERLAY_IP = "10.100.0.2"


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def wait_for(predicate, *, timeout: float, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:
            last_error = exc
        time.sleep(interval)
    if last_error is not None:
        raise last_error
    raise TimeoutError("condition was not met in time")


def netns_exec(ns: str, argv: list[str], *, stdout=None, stderr=None) -> subprocess.Popen[str]:
    return subprocess.Popen(["ip", "netns", "exec", ns, *argv], text=True, stdout=stdout, stderr=stderr)


def main() -> int:
    if os.geteuid() != 0:
        print("live smoke requires root", file=sys.stderr)
        return 1

    suffix = secrets.token_hex(2)
    server_ns = f"veilsv{suffix}"
    client_ns = f"veilcl{suffix}"
    veth_server = f"vsv{suffix}"
    veth_client = f"vcl{suffix}"
    server_log = Path(f"/tmp/veil-linux-vpn-server-{suffix}.log")
    client_log = Path(f"/tmp/veil-linux-vpn-client-{suffix}.log")

    server_proc: subprocess.Popen[str] | None = None
    client_proc: subprocess.Popen[str] | None = None

    try:
        run(["ip", "netns", "add", server_ns])
        run(["ip", "netns", "add", client_ns])
        run(["ip", "link", "add", veth_server, "type", "veth", "peer", "name", veth_client])
        run(["ip", "link", "set", veth_server, "netns", server_ns])
        run(["ip", "link", "set", veth_client, "netns", client_ns])
        run(["ip", "netns", "exec", server_ns, "ip", "addr", "add", f"{SERVER_UNDERLAY_IP}/30", "dev", veth_server])
        run(["ip", "netns", "exec", client_ns, "ip", "addr", "add", f"{CLIENT_UNDERLAY_IP}/30", "dev", veth_client])
        run(["ip", "netns", "exec", server_ns, "ip", "link", "set", "dev", "lo", "up"])
        run(["ip", "netns", "exec", client_ns, "ip", "link", "set", "dev", "lo", "up"])
        run(["ip", "netns", "exec", server_ns, "ip", "link", "set", "dev", veth_server, "up"])
        run(["ip", "netns", "exec", client_ns, "ip", "link", "set", "dev", veth_client, "up"])

        server_log_handle = server_log.open("w")
        client_log_handle = client_log.open("w")
        server_proc = netns_exec(
            server_ns,
            [
                "bash",
                "-lc",
                (
                    f"cd {ROOT} && "
                    "PYTHONPATH=. python3 examples/linux_vpn_proxy.py "
                    f"--mode server --host {SERVER_UNDERLAY_IP} --port 4433 "
                    "--tun-name veil0 "
                    f"--tun-address {SERVER_TUN_IP}/30 "
                    f"--tun-peer {CLIENT_TUN_IP} "
                    "--packet-mtu 1300 --name server"
                ),
            ],
            stdout=server_log_handle,
            stderr=subprocess.STDOUT,
        )
        client_proc = netns_exec(
            client_ns,
            [
                "bash",
                "-lc",
                (
                    f"cd {ROOT} && "
                    "PYTHONPATH=. python3 examples/linux_vpn_proxy.py "
                    f"--mode client --host {SERVER_UNDERLAY_IP} --port 4433 "
                    "--tun-name veil0 "
                    f"--tun-address {CLIENT_TUN_IP}/30 "
                    f"--tun-peer {SERVER_TUN_IP} "
                    "--packet-mtu 1300 --name client"
                ),
            ],
            stdout=client_log_handle,
            stderr=subprocess.STDOUT,
        )

        wait_for(
            lambda: run(
                ["ip", "netns", "exec", client_ns, "ip", "addr", "show", "dev", "veil0"],
                check=False,
            ).returncode
            == 0,
            timeout=20.0,
        )
        wait_for(
            lambda: run(
                ["ip", "netns", "exec", server_ns, "ip", "addr", "show", "dev", "veil0"],
                check=False,
            ).returncode
            == 0,
            timeout=20.0,
        )

        ping = run(
            [
                "ip",
                "netns",
                "exec",
                client_ns,
                "ping",
                "-I",
                CLIENT_TUN_IP,
                "-c",
                "3",
                "-W",
                "1",
                SERVER_TUN_IP,
            ]
        )
        print(ping.stdout.strip())
        print(f"server log: {server_log}")
        print(f"client log: {client_log}")
        return 0
    finally:
        for proc in (client_proc, server_proc):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        run(["ip", "netns", "del", client_ns], check=False)
        run(["ip", "netns", "del", server_ns], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
