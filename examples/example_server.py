"""
example_server.py — JSON echo server using the Veil protocol.

Run on the server machine:
    python example_server.py

The server listens on UDP port 4433 and exchanges structured JSON messages
over a real encrypted Veil session.
"""

import asyncio
from veil_core import Server


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

    async with server:
        print("[server] Listening on UDP 0.0.0.0:4433 …")

        while True:
            session = await server.accept(timeout=None)
            print(
                f"[server] New connection: {session.session_id:#x} "
                f"from {session.remote_host}:{session.remote_port}"
            )

            async def handle_session(active_session) -> None:
                while True:
                    try:
                        message = await active_session.recv_json(timeout=None)
                    except Exception as exc:
                        print(f"[server] Session {active_session.session_id:#x} stopped: {exc}")
                        break

                    print(
                        f"[server] [{active_session.session_id:#x}] stream={message.stream_id} "
                        f"message={message.body!r}"
                    )
                    sent = active_session.send_json(
                        {
                            "type": "echo",
                            "received": message.body,
                        },
                        stream_id=message.stream_id,
                    )
                    if not sent:
                        print("[server] Warning: send queue full (back-pressure)")
                        break

            asyncio.create_task(handle_session(session))


if __name__ == "__main__":
    asyncio.run(main())
