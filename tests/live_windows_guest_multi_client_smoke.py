from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core import Client, DataEvent


async def collect_reply(client: Client, expected: bytes) -> bytes:
    async for event in client.events():
        if not isinstance(event, DataEvent):
            continue
        if event.data != expected:
            raise RuntimeError(f"unexpected reply: expected={expected!r} actual={event.data!r}")
        return event.data
    raise RuntimeError("client event stream ended before reply")


async def run_smoke(host: str, port: int) -> int:
    alice = Client(
        host=host,
        port=port,
        client_id="alice",
        psk=bytes.fromhex("41" * 32),
        handshake_timeout_ms=2_000,
    )
    bob = Client(
        host=host,
        port=port,
        client_id="bob",
        psk=bytes.fromhex("42" * 32),
        handshake_timeout_ms=2_000,
    )

    async with alice, bob:
        alice_conn, bob_conn = await asyncio.gather(
            asyncio.wait_for(alice.connect(), timeout=10.0),
            asyncio.wait_for(bob.connect(), timeout=10.0),
        )

        if not alice.send(b"alice-ping", stream_id=31):
            raise RuntimeError("alice send queue rejected payload")
        if not bob.send(b"bob-ping", stream_id=37):
            raise RuntimeError("bob send queue rejected payload")

        alice_reply, bob_reply = await asyncio.gather(
            asyncio.wait_for(collect_reply(alice, b"alice-pong"), timeout=10.0),
            asyncio.wait_for(collect_reply(bob, b"bob-pong"), timeout=10.0),
        )

        print(
            json.dumps(
                {
                    "platform": sys.platform,
                    "host": host,
                    "port": port,
                    "alice_session_id": f"0x{alice_conn.session_id:x}",
                    "bob_session_id": f"0x{bob_conn.session_id:x}",
                    "alice_reply": alice_reply.decode("utf-8", errors="replace"),
                    "bob_reply": bob_reply.decode("utf-8", errors="replace"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guest-side Windows multi-client smoke")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_smoke(args.host, args.port))


if __name__ == "__main__":
    raise SystemExit(main())
