"""
example_server.py — Echo server using the Veil protocol.

Run on the server machine:
    python example_server.py

The server listens on UDP port 4433 and echoes every message back to the sender.
Any Python developer can extend this without touching any C++ code.
"""

import asyncio
from veil_core import DisconnectedEvent, ErrorEvent, NewConnectionEvent, Server, DataEvent


PSK = bytes.fromhex("ab" * 32)


async def main() -> None:
    # -----------------------------------------------------------------------
    # 1. Create the server (the C++ core binds the UDP socket and starts
    #    the encryption pipeline behind the scenes).
    # -----------------------------------------------------------------------
    server = Server(
        port=4433,
        host="0.0.0.0",
        psk=PSK,
        session_idle_timeout_ms=30_000,
        # Uncomment to enable WebSocket-style DPI obfuscation:
        # protocol_wrapper="websocket",
        # persona_preset="browser_ws",
        # enable_http_handshake_emulation=True,
    )

    async with server:  # calls start() and stop() automatically
        print("[server] Listening on UDP 0.0.0.0:4433 …")

        # -----------------------------------------------------------------------
        # 2. Consuming events — this is what your friend writes!
        #    The heavy lifting (crypto, fragmentation, retransmit) is C++.
        # -----------------------------------------------------------------------
        async for event in server.events():

            if isinstance(event, NewConnectionEvent):
                print(f"[server] New connection: {event.session_id:#x} "
                      f"from {event.remote_host}:{event.remote_port}")

            elif isinstance(event, DataEvent):
                print(f"[server] [{event.session_id:#x}] received "
                      f"{len(event.data)} bytes on stream {event.stream_id}: "
                      f"{event.data!r}")

                # Echo back on the same stream
                sent = server.send(
                    event.session_id,
                    b"ECHO: " + event.data,
                    stream_id=event.stream_id,
                )
                if not sent:
                    print("[server] Warning: send queue full (back-pressure)")

            elif isinstance(event, ErrorEvent):
                print(f"[server] Error on {event.session_id:#x}: {event.message}")

            elif isinstance(event, DisconnectedEvent):
                print(f"[server] Session {event.session_id:#x} closed: {event.reason}")


if __name__ == "__main__":
    asyncio.run(main())
