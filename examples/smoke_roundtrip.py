"""
smoke_roundtrip.py — one-process end-to-end smoke check for the Python bindings.

Starts a real Veil server and client, performs handshake, sends one payload,
waits for one reply, and exits. This is useful when validating that the Python
wrappers and compiled extension still work together after transport changes.
"""

from __future__ import annotations

import asyncio
import socket

from veil_core import Client, DataEvent, NewConnectionEvent, Server


PSK = bytes.fromhex("ab" * 32)


def reserve_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def main() -> None:
    port = reserve_ephemeral_port()
    payload = b"smoke-ping"
    reply = b"smoke-pong"

    server = Server(port=port, host="127.0.0.1", psk=PSK)
    client = Client(host="127.0.0.1", port=port, psk=PSK)

    async with server, client:
        connect_task = asyncio.create_task(client.connect())

        async def server_loop() -> None:
            async for event in server.events():
                if isinstance(event, NewConnectionEvent):
                    print(f"[smoke] server accepted session {event.session_id:#x}")
                elif isinstance(event, DataEvent):
                    print(f"[smoke] server received {event.data!r}")
                    server.send(event.session_id, reply, stream_id=event.stream_id)
                    break

        server_task = asyncio.create_task(server_loop())
        connection = await asyncio.wait_for(connect_task, timeout=5)
        print(f"[smoke] client connected as {connection.session_id:#x}")

        if not client.send(payload, stream_id=42):
            raise RuntimeError("client send queue rejected smoke payload")

        async for event in client.events():
            if isinstance(event, DataEvent):
                print(f"[smoke] client received {event.data!r} on stream {event.stream_id}")
                if event.data != reply or event.stream_id != 42:
                    raise RuntimeError("unexpected smoke reply payload")
                break

        await asyncio.wait_for(server_task, timeout=5)
        print("[smoke] roundtrip OK")


if __name__ == "__main__":
    asyncio.run(main())
