"""
smoke_roundtrip.py — one-process end-to-end smoke check for the Python bindings.

Starts a real Veil server and client, performs handshake, sends one payload,
waits for one reply, and exits.

Unlike the lower-level examples, this smoke test uses the session-oriented API
(`server.accept()` / `client.connect_session()`), which is the intended
starting point for real protocol usage.
"""

from __future__ import annotations

import asyncio
import socket

from veil_core import Client, Server


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
        connect_task = asyncio.create_task(client.connect_session())

        async def server_loop() -> None:
            session = await server.accept(timeout=5.0)
            print(f"[smoke] server accepted session {session.session_id:#x}")
            event = await session.recv(timeout=5.0, stream_id=42)
            print(f"[smoke] server received {event.data!r}")
            if not session.send(reply, stream_id=event.stream_id):
                raise RuntimeError("server send queue rejected smoke reply")

        server_task = asyncio.create_task(server_loop())
        session = await asyncio.wait_for(connect_task, timeout=5)
        print(f"[smoke] client connected as {session.session_id:#x}")

        if not session.send(payload, stream_id=42):
            raise RuntimeError("client send queue rejected smoke payload")

        event = await session.recv(timeout=5.0, stream_id=42)
        print(f"[smoke] client received {event.data!r} on stream {event.stream_id}")
        if event.data != reply or event.stream_id != 42:
            raise RuntimeError("unexpected smoke reply payload")

        await asyncio.wait_for(server_task, timeout=5)
        print("[smoke] roundtrip OK")


if __name__ == "__main__":
    asyncio.run(main())
