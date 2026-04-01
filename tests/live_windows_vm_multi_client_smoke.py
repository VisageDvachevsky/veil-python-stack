from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath


ROOT = Path(__file__).resolve().parents[1]


def reserve_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, *, timeout: float, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError("condition was not met in time")


def wait_for_tcp_port(host: str, port: int, *, timeout: float) -> None:
    def can_connect() -> bool:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            return False

    wait_for(can_connect, timeout=timeout)


def powershell_encoded(command: str) -> str:
    return base64.b64encode(command.encode("utf-16le")).decode("ascii")


def ssh_base_command(args: argparse.Namespace) -> list[str]:
    command: list[str] = []
    if args.ssh_password:
        command.extend(["sshpass", "-p", args.ssh_password])
    command.extend([
        "ssh",
        "-p",
        str(args.ssh_port),
        "-o",
        f"BatchMode={'no' if args.ssh_password else 'yes'}",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ])
    if args.ssh_identity:
        command.extend(["-i", args.ssh_identity])
    command.append(f"{args.guest_user}@{args.ssh_host}")
    return command


def launch_qemu(args: argparse.Namespace, *, qemu_log: Path) -> subprocess.Popen[str]:
    qemu_binary = shutil.which(args.qemu_binary) or args.qemu_binary
    if not qemu_binary or not Path(qemu_binary).exists():
        raise RuntimeError(f"qemu binary not found: {args.qemu_binary}")
    if not args.vm_image:
        raise RuntimeError("--vm-image is required when --launch-qemu is set")

    use_kvm = (not args.disable_kvm) and os.access("/dev/kvm", os.R_OK | os.W_OK)
    command = [
        qemu_binary,
        "-machine",
        "q35",
        "-cpu",
        "host" if use_kvm else "max",
        "-smp",
        str(args.vm_cpus),
        "-m",
        str(args.vm_memory_mb),
        "-drive",
        f"file={args.vm_image},format=qcow2,media=disk,if=ide",
        "-nic",
        f"user,model=e1000,hostfwd=tcp::{args.ssh_port}-:22",
        "-display",
        "none",
        "-serial",
        f"file:{qemu_log}",
        "-no-reboot",
    ]
    if use_kvm:
        command.extend(["-enable-kvm"])

    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
    )


def terminate_process(proc: subprocess.Popen[str] | None, *, timeout: float = 10.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


async def run_server(bind_host: str, bind_port: int, clients_json: str, expected_messages: int) -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from veil_core import DataEvent, Server

    client_entries = json.loads(clients_json)
    server = Server(
        port=bind_port,
        host=bind_host,
        clients=[
            {
                "client_id": entry["client_id"],
                "psk": bytes.fromhex(entry["psk_hex"]),
                "enabled": bool(entry.get("enabled", True)),
            }
            for entry in client_entries
        ],
        session_idle_timeout_ms=5_000,
    )

    server.start()
    try:
        seen = 0
        async for event in server.events():
            if not isinstance(event, DataEvent):
                continue
            if event.data == b"alice-ping":
                if not server.send(event.session_id, b"alice-pong", stream_id=event.stream_id):
                    raise RuntimeError("server send queue rejected alice reply")
                seen += 1
            elif event.data == b"bob-ping":
                if not server.send(event.session_id, b"bob-pong", stream_id=event.stream_id):
                    raise RuntimeError("server send queue rejected bob reply")
                seen += 1
            if seen >= expected_messages:
                await asyncio.sleep(0.5)
                return 0
        return 0
    finally:
        server.stop()


def run_remote_guest_smoke(args: argparse.Namespace, *, server_host: str, server_port: int, guest_log: Path) -> None:
    guest_repo_root = PureWindowsPath(args.guest_repo_root)
    guest_script = guest_repo_root / "tests" / "live_windows_guest_multi_client_smoke.py"
    guest_repo_root_ps = str(guest_repo_root).replace("'", "''")
    guest_script_ps = str(guest_script).replace("'", "''")
    guest_python_ps = args.guest_python.replace("'", "''")
    ps_command = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$env:PYTHONPATH = '{guest_repo_root_ps}'",
            f"& '{guest_python_ps}' '{guest_script_ps}' --host '{server_host}' --port {server_port}",
            "exit $LASTEXITCODE",
        ]
    )
    encoded = powershell_encoded(ps_command)
    command = [*ssh_base_command(args), "powershell", "-NoProfile", "-EncodedCommand", encoded]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    guest_log.write_text(
        "\n".join(
            [
                "[stdout]",
                result.stdout,
                "[stderr]",
                result.stderr,
            ]
        ),
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"guest smoke failed with exit code {result.returncode}; see {guest_log}"
        )


def orchestrate(args: argparse.Namespace) -> int:
    if args.launch_qemu and not args.vm_image:
        raise RuntimeError("--vm-image is required when --launch-qemu is set")
    if not args.guest_repo_root:
        raise RuntimeError("--guest-repo-root is required")

    topology_id = f"{int(time.time()) & 0xFFFF:04x}"
    bind_port = args.server_port or reserve_udp_port()
    server_host_for_guest = args.server_host
    if not server_host_for_guest:
        server_host_for_guest = "10.0.2.2" if args.launch_qemu else "127.0.0.1"

    server_log = Path(f"/tmp/veil-windows-vm-server-{topology_id}.log")
    guest_log = Path(f"/tmp/veil-windows-vm-guest-{topology_id}.log")
    qemu_log = Path(f"/tmp/veil-windows-vm-qemu-{topology_id}.log")
    clients_payload = [
        {"client_id": "alice", "psk_hex": "41" * 32, "enabled": True},
        {"client_id": "bob", "psk_hex": "42" * 32, "enabled": True},
    ]

    qemu_proc: subprocess.Popen[str] | None = None
    server_proc: subprocess.Popen[str] | None = None
    server_log_handle = server_log.open("w", encoding="utf-8")
    try:
        if args.launch_qemu:
            qemu_proc = launch_qemu(args, qemu_log=qemu_log)
            wait_for_tcp_port(args.ssh_host, args.ssh_port, timeout=args.vm_boot_timeout)

        server_proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--role",
                "server",
                "--bind-host",
                "0.0.0.0",
                "--bind-port",
                str(bind_port),
                "--clients-json",
                json.dumps(clients_payload),
                "--expected-messages",
                "2",
            ],
            stdout=server_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )

        run_remote_guest_smoke(
            args,
            server_host=server_host_for_guest,
            server_port=bind_port,
            guest_log=guest_log,
        )

        assert server_proc is not None
        server_exit = server_proc.wait(timeout=20.0)
        if server_exit != 0:
            raise RuntimeError(f"server process failed with exit code {server_exit}; see {server_log}")

        print(f"server log: {server_log}")
        print(f"guest log: {guest_log}")
        if args.launch_qemu:
            print(f"qemu log: {qemu_log}")
        return 0
    finally:
        terminate_process(server_proc)
        terminate_process(qemu_proc)
        server_log_handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Windows VM multi-client smoke against Linux host server")
    parser.add_argument("--role", choices=("orchestrate", "server"), default="orchestrate")
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--bind-port", type=int, default=4433)
    parser.add_argument("--clients-json", default="[]")
    parser.add_argument("--expected-messages", type=int, default=2)

    parser.add_argument("--launch-qemu", action="store_true")
    parser.add_argument("--qemu-binary", default="qemu-system-x86_64")
    parser.add_argument("--vm-image")
    parser.add_argument("--disable-kvm", action="store_true")
    parser.add_argument("--vm-memory-mb", type=int, default=8192)
    parser.add_argument("--vm-cpus", type=int, default=4)
    parser.add_argument("--vm-boot-timeout", type=float, default=180.0)

    parser.add_argument("--ssh-host", default="127.0.0.1")
    parser.add_argument("--ssh-port", type=int, default=2222)
    parser.add_argument("--ssh-identity")
    parser.add_argument("--ssh-password", default="")
    parser.add_argument("--guest-user", required=False, default="")
    parser.add_argument("--guest-python", default="py")
    parser.add_argument("--guest-repo-root", default="")
    parser.add_argument("--server-host", default="")
    parser.add_argument("--server-port", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.role == "server":
        return asyncio.run(
            run_server(
                bind_host=args.bind_host,
                bind_port=args.bind_port,
                clients_json=args.clients_json,
                expected_messages=args.expected_messages,
            )
        )
    if not args.guest_user:
        raise RuntimeError("--guest-user is required")
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
