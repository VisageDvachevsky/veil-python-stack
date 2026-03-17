from __future__ import annotations

from dataclasses import dataclass

from veil_core.events import DataEvent
from veil_core.message import Message, encode_json_message, message_from_event


@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    remote_host: str = ""
    remote_port: int = 0


class Session:
    """
    Thin session-oriented wrapper over the transport-level Client/Server API.

    Keeps the existing event-driven API intact while allowing local code to
    work with a concrete peer session directly.
    """

    def __init__(
        self,
        owner: object,
        *,
        session_id: int,
        remote_host: str = "",
        remote_port: int = 0,
        default_stream_id: int = 1,
    ) -> None:
        self._owner = owner
        self._info = SessionInfo(
            session_id=session_id,
            remote_host=remote_host,
            remote_port=remote_port,
        )
        self._default_stream_id = default_stream_id

    @property
    def session_id(self) -> int:
        return self._info.session_id

    @property
    def remote_host(self) -> str:
        return self._info.remote_host

    @property
    def remote_port(self) -> int:
        return self._info.remote_port

    @property
    def info(self) -> SessionInfo:
        return self._info

    def send(self, data: bytes, *, stream_id: int | None = None) -> bool:
        sid = self._info.session_id
        resolved_stream_id = self._default_stream_id if stream_id is None else stream_id

        if hasattr(self._owner, "send"):
            try:
                return self._owner.send(sid, data, stream_id=resolved_stream_id)
            except TypeError:
                return self._owner.send(
                    data,
                    session_id=sid,
                    stream_id=resolved_stream_id,
                )
        raise RuntimeError("Session owner does not support send()")

    async def recv(
        self,
        *,
        timeout: float | None = None,
        stream_id: int | None = None,
    ) -> DataEvent:
        owner = self._owner
        if not hasattr(owner, "recv"):
            raise RuntimeError("Session owner does not support recv()")
        return await owner.recv(
            timeout=timeout,
            session_id=self._info.session_id,
            stream_id=stream_id,
        )

    def send_json(self, body: object, *, stream_id: int | None = None) -> bool:
        return self.send(encode_json_message(body), stream_id=stream_id)

    async def recv_json(
        self,
        *,
        timeout: float | None = None,
        stream_id: int | None = None,
    ) -> Message:
        event = await self.recv(timeout=timeout, stream_id=stream_id)
        return message_from_event(event)

    def disconnect(self) -> bool:
        owner = self._owner
        if not hasattr(owner, "disconnect"):
            raise RuntimeError("Session owner does not support disconnect()")
        return owner.disconnect(self._info.session_id)

    def __repr__(self) -> str:
        peer = ""
        if self.remote_host:
            peer = f" remote={self.remote_host}:{self.remote_port}"
        return f"<Session session_id={self.session_id:#x}{peer}>"
