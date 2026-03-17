from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from veil_core.events import DataEvent


@dataclass(frozen=True)
class Message:
    session_id: int
    stream_id: int
    body: Any
    raw: bytes


def encode_json_message(body: Any) -> bytes:
    return json.dumps(
        body,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_json_message(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"))


def message_from_event(event: DataEvent) -> Message:
    return Message(
        session_id=event.session_id,
        stream_id=event.stream_id,
        body=decode_json_message(event.data),
        raw=event.data,
    )
