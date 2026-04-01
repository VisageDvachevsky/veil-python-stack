from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def netns_exec(
    namespace: str,
    argv: list[str],
    *,
    stdout=None,
    stderr=None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        ["ip", "netns", "exec", namespace, *argv],
        text=True,
        stdout=stdout,
        stderr=stderr,
        env=env,
    )


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
            if not server.send(event.session_id, b"ack:" + event.data, stream_id=event.stream_id):
                raise RuntimeError("server send queue rejected reply")
            seen += 1
            if seen >= expected_messages:
                # Allow the outbound queue to flush before shutting the node down.
                await asyncio.sleep(0.5)
                return 0
        return 0
    finally:
        server.stop()


async def run_client(host: str, port: int, client_id: str, psk_hex: str, payload_text: str) -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from veil_core import Client, DataEvent

    payload = payload_text.encode("utf-8")
    client = Client(
        host=host,
        port=port,
        client_id=client_id,
        psk=bytes.fromhex(psk_hex),
        handshake_timeout_ms=2_000,
    )

    client.start()
    try:
        await asyncio.wait_for(client.connect(), timeout=5)
        if not client.send(payload, stream_id=17):
            raise RuntimeError("client send queue rejected payload")

        async for event in client.events():
            if isinstance(event, DataEvent):
                if event.data != b"ack:" + payload:
                    raise RuntimeError(f"unexpected reply for {client_id}: {event.data!r}")
                return 0
        raise RuntimeError(f"event stream ended before reply for {client_id}")
    finally:
        client.stop()


def wait_for(predicate, *, timeout: float, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError("condition was not met in time")


def setup_bridge(topology_id: str) -> tuple[str, str, str, str, str, str, str]:
    bridge = f"veilbr{topology_id}"
    server_ns = f"veilsv{topology_id}"
    alice_ns = f"veilal{topology_id}"
    bob_ns = f"veilbo{topology_id}"
    server_veth_host = f"vsvh{topology_id}"
    alice_veth_host = f"valh{topology_id}"
    bob_veth_host = f"vboh{topology_id}"
    server_veth_ns = f"vsvn{topology_id}"
    alice_veth_ns = f"valn{topology_id}"
    bob_veth_ns = f"vbon{topology_id}"

    run(["ip", "netns", "add", server_ns])
    run(["ip", "netns", "add", alice_ns])
    run(["ip", "netns", "add", bob_ns])
    run(["ip", "link", "add", bridge, "type", "bridge"])
    run(["ip", "link", "set", bridge, "up"])

    for host_if, ns_if, ns_name in (
        (server_veth_host, server_veth_ns, server_ns),
        (alice_veth_host, alice_veth_ns, alice_ns),
        (bob_veth_host, bob_veth_ns, bob_ns),
    ):
        run(["ip", "link", "add", host_if, "type", "veth", "peer", "name", ns_if])
        run(["ip", "link", "set", host_if, "master", bridge])
        run(["ip", "link", "set", host_if, "up"])
        run(["ip", "link", "set", ns_if, "netns", ns_name])
        run(["ip", "netns", "exec", ns_name, "ip", "link", "set", "dev", "lo", "up"])
        run(["ip", "netns", "exec", ns_name, "ip", "link", "set", "dev", ns_if, "up"])

    run(["ip", "netns", "exec", server_ns, "ip", "addr", "add", "10.90.0.10/24", "dev", server_veth_ns])
    run(["ip", "netns", "exec", alice_ns, "ip", "addr", "add", "10.90.0.11/24", "dev", alice_veth_ns])
    run(["ip", "netns", "exec", bob_ns, "ip", "addr", "add", "10.90.0.12/24", "dev", bob_veth_ns])
    return bridge, server_ns, alice_ns, bob_ns, server_veth_host, alice_veth_host, bob_veth_host


def cleanup_bridge(
    bridge: str,
    server_ns: str,
    alice_ns: str,
    bob_ns: str,
) -> None:
    run(["ip", "netns", "del", server_ns], check=False)
    run(["ip", "netns", "del", alice_ns], check=False)
    run(["ip", "netns", "del", bob_ns], check=False)
    run(["ip", "link", "del", bridge], check=False)


def orchestrate() -> int:
    if os.geteuid() != 0:
        print("live netns multi-client smoke requires root", file=sys.stderr)
        return 1

    topology_id = secrets.token_hex(2)
    bridge, server_ns, alice_ns, bob_ns, *_ = setup_bridge(topology_id)
    server_log = Path(f"/tmp/veil-netns-server-{topology_id}.log")
    alice_log = Path(f"/tmp/veil-netns-alice-{topology_id}.log")
    bob_log = Path(f"/tmp/veil-netns-bob-{topology_id}.log")
    clients_payload = [
        {"client_id": "alice", "psk_hex": "41" * 32, "enabled": True},
        {"client_id": "bob", "psk_hex": "42" * 32, "enabled": True},
    ]

    server_proc: subprocess.Popen[str] | None = None
    alice_proc: subprocess.Popen[str] | None = None
    bob_proc: subprocess.Popen[str] | None = None
    try:
        server_log_handle = server_log.open("w")
        alice_log_handle = alice_log.open("w")
        bob_log_handle = bob_log.open("w")

        server_proc = netns_exec(
            server_ns,
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--role",
                "server",
                "--bind-host",
                "0.0.0.0",
                "--bind-port",
                "4433",
                "--clients-json",
                json.dumps(clients_payload),
                "--expected-messages",
                "2",
            ],
            stdout=server_log_handle,
            stderr=subprocess.STDOUT,
        )

        wait_for(
            lambda: run(
                ["ip", "netns", "exec", alice_ns, "ping", "-c", "1", "-W", "1", "10.90.0.10"],
                check=False,
            ).returncode == 0,
            timeout=10.0,
        )

        alice_proc = netns_exec(
            alice_ns,
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--role",
                "client",
                "--host",
                "10.90.0.10",
                "--port",
                "4433",
                "--client-id",
                "alice",
                "--psk-hex",
                "41" * 32,
                "--payload",
                "alice-ping",
            ],
            stdout=alice_log_handle,
            stderr=subprocess.STDOUT,
        )
        bob_proc = netns_exec(
            bob_ns,
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--role",
                "client",
                "--host",
                "10.90.0.10",
                "--port",
                "4433",
                "--client-id",
                "bob",
                "--psk-hex",
                "42" * 32,
                "--payload",
                "bob-ping",
            ],
            stdout=bob_log_handle,
            stderr=subprocess.STDOUT,
        )

        for proc, name in ((alice_proc, "alice"), (bob_proc, "bob"), (server_proc, "server")):
            assert proc is not None
            exit_code = proc.wait(timeout=20)
            if exit_code != 0:
                raise RuntimeError(f"{name} process failed with exit code {exit_code}")

        print(f"server log: {server_log}")
        print(f"alice log: {alice_log}")
        print(f"bob log: {bob_log}")
        return 0
    finally:
        for proc in (alice_proc, bob_proc, server_proc):
            if proc is None or proc.poll() is not None:
                continue
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        cleanup_bridge(bridge, server_ns, alice_ns, bob_ns)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("orchestrate", "server", "client"), default="orchestrate")
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--bind-port", type=int, default=4433)
    parser.add_argument("--clients-json", default="[]")
    parser.add_argument("--expected-messages", type=int, default=2)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--client-id", default="")
    parser.add_argument("--psk-hex", default="ab" * 32)
    parser.add_argument("--payload", default="ping")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.role == "orchestrate":
        return orchestrate()
    if args.role == "server":
        return asyncio.run(
            run_server(
                bind_host=args.bind_host,
                bind_port=args.bind_port,
                clients_json=args.clients_json,
                expected_messages=args.expected_messages,
            )
        )
    return asyncio.run(
        run_client(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            psk_hex=args.psk_hex,
            payload_text=args.payload,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
