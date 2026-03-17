from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core import Client


DEFAULT_PSK = bytes.fromhex("ab" * 32)


def utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).with_name("veil_chat_client.json")
    return Path(__file__).with_name("veil_chat_client.json")


@dataclass
class ClientConfig:
    host: str = "127.0.0.1"
    port: int = 4433
    name: str = "veil-client"
    title: str = "Veil Chat Client"
    psk_hex: str = DEFAULT_PSK.hex()
    protocol_wrapper: str = "none"
    persona_preset: str = "custom"
    retry_delay: float = 1.5
    auto_connect: bool = True

    @property
    def psk(self) -> bytes:
        return bytes.fromhex(self.psk_hex)


def load_config(path: Path) -> ClientConfig:
    if not path.exists():
        return ClientConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ClientConfig(**raw)


class VeilChatWorker:
    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._events: queue.SimpleQueue[tuple[str, dict[str, Any]]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Client | None = None
        self._session = None
        self._running = False

    @property
    def events(self) -> queue.SimpleQueue[tuple[str, dict[str, Any]]]:
        return self._events

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def reconnect(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if self._session is not None:
            loop.call_soon_threadsafe(self._session.disconnect)

    def send_text(self, text: str) -> None:
        loop = self._loop
        if loop is None or self._session is None:
            self._emit("error", {"message": "Not connected"})
            return
        asyncio.run_coroutine_threadsafe(self._send_message(text), loop)

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._loop = None

    async def _main(self) -> None:
        self._client = Client(
            host=self._config.host,
            port=self._config.port,
            psk=self._config.psk,
            protocol_wrapper=self._config.protocol_wrapper,
            persona_preset=self._config.persona_preset,
            handshake_timeout_ms=5_000,
            session_idle_timeout_ms=30_000,
        )
        self._client.start()
        self._emit(
            "status",
            {
                "phase": "starting",
                "connected": False,
                "peer": f"{self._config.host}:{self._config.port}",
            },
        )

        try:
            while self._running:
                try:
                    self._emit(
                        "status",
                        {
                            "phase": "connecting",
                            "connected": False,
                            "peer": f"{self._config.host}:{self._config.port}",
                        },
                    )
                    self._session = await self._client.connect_session()
                    self._emit(
                        "status",
                        {
                            "phase": "connected",
                            "connected": True,
                            "peer": f"{self._session.remote_host}:{self._session.remote_port}",
                            "session_id": f"{self._session.session_id:#x}",
                        },
                    )
                    await self._reader_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._emit("error", {"message": f"connect failed: {exc}"})
                    self._emit(
                        "status",
                        {
                            "phase": "retrying",
                            "connected": False,
                            "peer": f"{self._config.host}:{self._config.port}",
                        },
                    )
                    await asyncio.sleep(self._config.retry_delay)
        finally:
            self._session = None
            if self._client is not None:
                self._client.stop()

    async def _reader_loop(self) -> None:
        assert self._session is not None
        session = self._session
        while self._running and self._session is session:
            try:
                message = await session.recv_json(timeout=0.5, stream_id=1)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                if self._session is session:
                    self._session = None
                self._emit("error", {"message": f"connection closed: {exc}"})
                self._emit(
                    "status",
                    {
                        "phase": "disconnected",
                        "connected": False,
                        "peer": f"{self._config.host}:{self._config.port}",
                    },
                )
                return

            body = message.body
            sender = "peer"
            if isinstance(body, dict):
                sender = str(body.get("sender", "peer"))
            self._emit(
                "message",
                {
                    "origin": "remote",
                    "sender": sender,
                    "timestamp": utc_iso(),
                    "text": json.dumps(body, ensure_ascii=False, indent=2)
                    if not isinstance(body, str)
                    else body,
                },
            )

    async def _send_message(self, text: str) -> None:
        session = self._session
        if session is None:
            self._emit("error", {"message": "Not connected"})
            return
        body = {
            "type": "chat",
            "sender": self._config.name,
            "text": text,
            "timestamp": utc_iso(),
        }
        sent = session.send_json(body, stream_id=1)
        if not sent:
            self._emit("error", {"message": "Send queue is full"})
            return
        self._emit(
            "message",
            {
                "origin": "local",
                "sender": self._config.name,
                "timestamp": body["timestamp"],
                "text": text,
            },
        )

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        self._events.put((kind, payload))


class ChatMainWindow(QMainWindow):
    def __init__(self, config: ClientConfig) -> None:
        super().__init__()
        self._config = config
        self._worker = VeilChatWorker(config)

        self.setWindowTitle(config.title)
        self.resize(920, 680)

        root = QWidget()
        self.setCentralWidget(root)

        layout = QGridLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(18)

        sidebar = QGroupBox("Connection")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setSpacing(12)

        self._status_label = QLabel("starting")
        self._status_label.setStyleSheet("font-weight: 700; font-size: 18px;")
        self._peer_label = QLabel("-")
        self._session_label = QLabel("-")
        self._name_label = QLabel(config.name)

        for title, widget in [
            ("Status", self._status_label),
            ("Peer", self._peer_label),
            ("Session", self._session_label),
            ("Name", self._name_label),
        ]:
            block = QWidget()
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 0, 0, 0)
            caption = QLabel(title.upper())
            caption.setStyleSheet("color: #6b7280; font-size: 11px; font-weight: 700;")
            block_layout.addWidget(caption)
            block_layout.addWidget(widget)
            sidebar_layout.addWidget(block)

        self._reconnect_button = QPushButton("Reconnect")
        self._reconnect_button.clicked.connect(self._worker.reconnect)
        sidebar_layout.addWidget(self._reconnect_button)
        sidebar_layout.addStretch(1)

        main_panel = QWidget()
        main_layout = QVBoxLayout(main_panel)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        self._history = QTextEdit()
        self._history.setReadOnly(True)
        self._history.setFont(QFont("JetBrains Mono", 10))

        self._input = QTextEdit()
        self._input.setPlaceholderText("Type a message. Enter sends, Shift+Enter inserts newline.")
        self._input.setMaximumHeight(120)
        self._input.setFont(QFont("JetBrains Mono", 10))

        send_row = QHBoxLayout()
        send_row.addStretch(1)
        self._send_button = QPushButton("Send")
        self._send_button.clicked.connect(self._send_current_message)
        send_row.addWidget(self._send_button)

        main_layout.addWidget(self._history, stretch=1)
        main_layout.addWidget(self._input)
        main_layout.addLayout(send_row)

        layout.addWidget(sidebar, 0, 0)
        layout.addWidget(main_panel, 0, 1)
        layout.setColumnStretch(1, 1)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._drain_events)
        self._poll_timer.start(100)

        self._worker.start()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._worker.stop()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self._send_current_message()
            return
        super().keyPressEvent(event)

    def _send_current_message(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._worker.send_text(text)
        self._input.clear()

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self._worker.events.get_nowait()
            except queue.Empty:
                return

            if kind == "status":
                self._status_label.setText(payload.get("phase", "-"))
                self._peer_label.setText(payload.get("peer", "-"))
                self._session_label.setText(payload.get("session_id", "-"))
                connected = payload.get("connected", False)
                self._send_button.setEnabled(bool(connected))
            elif kind == "message":
                self._append_message(
                    payload["origin"],
                    payload["sender"],
                    payload["timestamp"],
                    payload["text"],
                )
            elif kind == "error":
                self._append_message("system", "system", utc_iso(), payload["message"])

    def _append_message(self, origin: str, sender: str, timestamp: str, text: str) -> None:
        color = {
            "local": "#0f766e",
            "remote": "#0369a1",
            "system": "#92400e",
        }.get(origin, "#374151")
        self._history.append(
            f'<div style="margin: 0 0 10px 0;">'
            f'<span style="color:{color}; font-weight:700;">[{origin}] {sender}</span> '
            f'<span style="color:#6b7280;">{timestamp}</span><br>'
            f'<span>{text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(chr(10), "<br>")}</span>'
            f"</div>"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--name")
    parser.add_argument("--title")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.name:
        config.name = args.name
    if args.title:
        config.title = args.title

    app = QApplication(sys.argv)
    window = ChatMainWindow(config)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
