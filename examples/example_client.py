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

        # Wait for the session to come up (crypto handshake)
        conn = await client.connect()
        print(f"[client] Connected! session_id={conn.session_id:#x}")

        # Send a test message
        client.send(b"Hello from Python!", stream_id=1)

        # Read exactly one response and exit.
        reply = await client.recv(timeout=5.0, stream_id=1)
        print(f"[client] Server replied: {reply.data!r}")

        print("[client] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4433)
    args = parser.parse_args()
    asyncio.run(main(args.host, args.port))
