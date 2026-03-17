"""
example_client.py — Client that connects to the Veil echo server.

Run on the client machine (or in a second terminal):
    python example_client.py --host 127.0.0.1 --port 4433
"""

import asyncio
import argparse
from veil_core import Client


PSK = bytes.fromhex("ab" * 32)


async def main(host: str, port: int) -> None:
    client = Client(
        host=host,
        port=port,
        psk=PSK,
        handshake_timeout_ms=5_000,
    )

    async with client:
        print(f"[client] Connecting to {host}:{port} …")

        session = await client.connect_session()
        print(f"[client] Connected! session_id={session.session_id:#x}")

        payload = {
            "type": "hello",
            "message": "Hello from Python!",
        }
        session.send_json(payload, stream_id=1)

        reply = await session.recv_json(timeout=5.0, stream_id=1)
        print(f"[client] Server replied: {reply.body!r}")

        print("[client] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4433)
    args = parser.parse_args()
    asyncio.run(main(args.host, args.port))
