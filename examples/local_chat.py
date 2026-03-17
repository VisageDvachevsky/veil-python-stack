"""
local_chat.py — browser chat demo over a real Veil session.

Run two instances locally:

    python examples/local_chat.py --mode server --veil-port 4433 --ui-port 8080 --name server
    python examples/local_chat.py --mode client --host 127.0.0.1 --veil-port 4433 --ui-port 8081 --name client

Then open:

    http://127.0.0.1:8080
    http://127.0.0.1:8081

The browser UI talks to this process over local HTTP/SSE. The actual chat
messages cross the wire through Veil, not through the browser transport.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core import Client, Server, Session


PSK = bytes.fromhex("ab" * 32)


def utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def pretty_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Veil Local Chat</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: rgba(15, 23, 42, 0.88);
      --panel-2: rgba(30, 41, 59, 0.92);
      --line: rgba(148, 163, 184, 0.24);
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #14b8a6;
      --accent-2: #f59e0b;
      --remote: #38bdf8;
      --local: #34d399;
      --error: #f87171;
      --shadow: 0 24px 80px rgba(15, 23, 42, 0.45);
      --mono: "JetBrains Mono", "Fira Code", monospace;
      --sans: "IBM Plex Sans", "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: var(--sans);
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(20, 184, 166, 0.22), transparent 28%),
        radial-gradient(circle at bottom right, rgba(245, 158, 11, 0.18), transparent 26%),
        linear-gradient(160deg, #020617 0%, #0f172a 60%, #111827 100%);
      display: flex;
      align-items: stretch;
      justify-content: center;
      padding: 24px;
    }
    .shell {
      width: min(1100px, 100%);
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 18px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    .sidebar {
      padding: 22px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--accent);
    }
    .title {
      margin: 0;
      font-size: 34px;
      line-height: 1.02;
      font-weight: 700;
    }
    .subtitle {
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
    }
    .status-grid {
      display: grid;
      gap: 12px;
    }
    .status-block {
      padding: 14px 16px;
      border-radius: 18px;
      background: var(--panel-2);
      border: 1px solid rgba(148, 163, 184, 0.12);
    }
    .status-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 6px;
    }
    .status-value {
      font-family: var(--mono);
      font-size: 14px;
      word-break: break-word;
    }
    .main {
      display: grid;
      grid-template-rows: auto 1fr auto;
      min-height: 76vh;
      overflow: hidden;
    }
    .main-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 20px 24px;
      border-bottom: 1px solid var(--line);
    }
    .headline {
      margin: 0;
      font-size: 18px;
      font-weight: 600;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--line);
      font-size: 13px;
      color: var(--muted);
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--error);
      box-shadow: 0 0 0 6px rgba(248, 113, 113, 0.16);
    }
    .dot.connected {
      background: var(--local);
      box-shadow: 0 0 0 6px rgba(52, 211, 153, 0.16);
    }
    .timeline {
      overflow: auto;
      padding: 22px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .entry {
      border: 1px solid rgba(148, 163, 184, 0.16);
      background: rgba(15, 23, 42, 0.72);
      border-radius: 18px;
      padding: 14px 16px;
      animation: slideIn 160ms ease-out;
    }
    .entry.local { border-color: rgba(52, 211, 153, 0.28); }
    .entry.remote { border-color: rgba(56, 189, 248, 0.28); }
    .entry.system { border-color: rgba(245, 158, 11, 0.28); }
    .entry-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 8px;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
    }
    .entry-body {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--mono);
      font-size: 13px;
      line-height: 1.6;
    }
    .composer {
      border-top: 1px solid var(--line);
      padding: 18px;
      display: grid;
      gap: 12px;
      background: rgba(2, 6, 23, 0.5);
    }
    textarea {
      width: 100%;
      min-height: 120px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(15, 23, 42, 0.78);
      color: var(--text);
      padding: 16px;
      font: inherit;
      line-height: 1.5;
    }
    textarea:focus {
      outline: none;
      border-color: rgba(20, 184, 166, 0.55);
      box-shadow: 0 0 0 4px rgba(20, 184, 166, 0.12);
    }
    .composer-actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }
    .hint {
      color: var(--muted);
      font-size: 13px;
    }
    button {
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      cursor: pointer;
      font: inherit;
      font-weight: 600;
      color: #082f49;
      background: linear-gradient(135deg, #5eead4 0%, #38bdf8 100%);
      box-shadow: 0 16px 32px rgba(56, 189, 248, 0.24);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
      box-shadow: none;
    }
    @keyframes slideIn {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @media (max-width: 860px) {
      body { padding: 14px; }
      .shell { grid-template-columns: 1fr; }
      .main { min-height: 68vh; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="card sidebar">
      <div>
        <div class="eyebrow">Veil Protocol Chat</div>
        <h1 class="title">Local-first message path</h1>
      </div>
      <p class="subtitle">
        Browser traffic stays local. Peer messages move through the real Veil
        transport session underneath.
      </p>
      <div class="status-grid">
        <div class="status-block">
          <div class="status-label">Mode</div>
          <div class="status-value" id="mode">-</div>
        </div>
        <div class="status-block">
          <div class="status-label">Peer</div>
          <div class="status-value" id="peer">-</div>
        </div>
        <div class="status-block">
          <div class="status-label">Session</div>
          <div class="status-value" id="session">-</div>
        </div>
        <div class="status-block">
          <div class="status-label">User</div>
          <div class="status-value" id="name">-</div>
        </div>
      </div>
    </aside>
    <section class="card main">
      <header class="main-header">
        <div>
          <p class="headline">Veil Browser Chat</p>
        </div>
        <div class="pill">
          <span class="dot" id="dot"></span>
          <span id="status-text">starting</span>
        </div>
      </header>
      <div class="timeline" id="timeline"></div>
      <form class="composer" id="composer">
        <textarea id="message" placeholder='{"type":"chat","text":"hello"} or plain text'></textarea>
        <div class="composer-actions">
          <div class="hint">Plain text becomes a JSON chat envelope automatically.</div>
          <button id="send-button" type="submit">Send Through Veil</button>
        </div>
      </form>
    </section>
  </div>
  <script>
    const timeline = document.getElementById("timeline");
    const form = document.getElementById("composer");
    const messageInput = document.getElementById("message");
    const sendButton = document.getElementById("send-button");
    const dot = document.getElementById("dot");
    const statusText = document.getElementById("status-text");
    const modeEl = document.getElementById("mode");
    const peerEl = document.getElementById("peer");
    const sessionEl = document.getElementById("session");
    const nameEl = document.getElementById("name");

    function addEntry(entry) {
      const card = document.createElement("article");
      card.className = `entry ${entry.origin || "system"}`;
      const head = document.createElement("div");
      head.className = "entry-head";
      const left = document.createElement("span");
      left.textContent = entry.label || entry.origin || "event";
      const right = document.createElement("span");
      right.textContent = entry.timestamp || "";
      const body = document.createElement("pre");
      body.className = "entry-body";
      body.textContent = entry.text || "";
      head.append(left, right);
      card.append(head, body);
      timeline.append(card);
      timeline.scrollTop = timeline.scrollHeight;
    }

    function setStatus(status) {
      modeEl.textContent = status.mode || "-";
      peerEl.textContent = status.peer || "-";
      sessionEl.textContent = status.session_id || "-";
      nameEl.textContent = status.name || "-";
      statusText.textContent = status.connected ? "connected" : (status.phase || "waiting");
      dot.classList.toggle("connected", !!status.connected);
      sendButton.disabled = !status.connected;
    }

    async function sendMessage(rawText) {
      const response = await fetch("/api/send", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text: rawText}),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({error: response.statusText}));
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const rawText = messageInput.value.trim();
      if (!rawText) {
        return;
      }
      try {
        await sendMessage(rawText);
        messageInput.value = "";
      } catch (error) {
        addEntry({
          origin: "system",
          label: "send error",
          timestamp: new Date().toISOString(),
          text: String(error),
        });
      }
    });

    const source = new EventSource("/api/events");
    source.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "snapshot") {
        timeline.innerHTML = "";
        setStatus(payload.status);
        for (const item of payload.messages) {
          addEntry(item);
        }
        return;
      }
      if (payload.type === "status") {
        setStatus(payload.status);
        return;
      }
      if (payload.type === "entry") {
        addEntry(payload.entry);
      }
    };
    source.onerror = () => {
      setStatus({
        connected: false,
        phase: "event stream lost",
        mode: modeEl.textContent,
        peer: peerEl.textContent,
        session_id: sessionEl.textContent,
        name: nameEl.textContent,
      });
    };
  </script>
</body>
</html>
"""


@dataclass
class ChatEntry:
    origin: str
    label: str
    text: str
    timestamp: str

    def to_payload(self) -> dict[str, str]:
        return {
            "origin": self.origin,
            "label": self.label,
            "text": self.text,
            "timestamp": self.timestamp,
        }


class LocalChatApp:
    def __init__(
        self,
        *,
        mode: str,
        host: str,
        veil_port: int,
        ui_host: str,
        ui_port: int,
        name: str,
        psk: bytes,
        retry_delay: float,
    ) -> None:
        self._mode = mode
        self._veil_host = host
        self._veil_port = veil_port
        self._ui_host = ui_host
        self._ui_port = ui_port
        self._name = name
        self._psk = psk
        self._retry_delay = retry_delay

        self._server: Server | None = None
        self._client: Client | None = None
        self._active_session: Session | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._control_task: asyncio.Task[None] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._running = False
        self._history: list[ChatEntry] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._session_generation = 0

        self._app = web.Application()
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/api/events", self._handle_events)
        self._app.router.add_post("/api/send", self._handle_send)

    async def start(self) -> None:
        self._running = True
        if self._mode == "server":
            self._server = Server(
                port=self._veil_port,
                host=self._veil_host,
                psk=self._psk,
                session_idle_timeout_ms=30_000,
            )
            self._server.start()
            self._control_task = asyncio.create_task(self._accept_loop())
            self._record_system(
                f"Veil server listening on {self._veil_host}:{self._veil_port}"
            )
        else:
            self._client = Client(
                host=self._veil_host,
                port=self._veil_port,
                psk=self._psk,
                handshake_timeout_ms=5_000,
                session_idle_timeout_ms=30_000,
            )
            self._client.start()
            self._control_task = asyncio.create_task(self._connect_loop())
            self._record_system(
                f"Veil client dialing {self._veil_host}:{self._veil_port}"
            )

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._ui_host, self._ui_port)
        await self._site.start()
        self._publish_status()

    async def stop(self) -> None:
        self._running = False

        for task in [self._control_task, self._reader_task]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self._server is not None:
            self._server.stop()
        if self._client is not None:
            self._client.stop()

        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({"type": "shutdown"})
        self._subscribers.clear()

        if self._runner is not None:
            await self._runner.cleanup()

    async def _accept_loop(self) -> None:
        assert self._server is not None
        while self._running:
            try:
                session = await self._server.accept(timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_system(f"accept loop error: {exc}")
                await asyncio.sleep(self._retry_delay)
                continue

            await self._activate_session(session, reason="accepted")

    async def _connect_loop(self) -> None:
        assert self._client is not None
        while self._running:
            try:
                session = await self._client.connect_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_system(f"connect failed: {exc}")
                self._publish_status(phase="retrying")
                await asyncio.sleep(self._retry_delay)
                continue

            await self._activate_session(session, reason="connected")
            while self._running and self._active_session is session:
                await asyncio.sleep(0.2)
            await asyncio.sleep(self._retry_delay)

    async def _activate_session(self, session: Session, *, reason: str) -> None:
        self._session_generation += 1
        generation = self._session_generation
        self._active_session = session
        self._record_system(
            f"{reason}: session {session.session_id:#x} peer={session.remote_host}:{session.remote_port}"
        )
        self._publish_status()

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader_task = asyncio.create_task(self._session_reader(session, generation))

    async def _session_reader(self, session: Session, generation: int) -> None:
        while self._running:
            try:
                message = await session.recv_json(timeout=0.5, stream_id=1)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._active_session is session and generation == self._session_generation:
                    self._active_session = None
                    self._record_system(
                        f"session {session.session_id:#x} closed: {exc}"
                    )
                    self._publish_status(phase="disconnected")
                return

            body = message.body
            sender = "peer"
            if isinstance(body, dict):
                sender = str(body.get("sender", "peer"))
            self._record_entry(
                ChatEntry(
                    origin="remote",
                    label=f"remote · {sender}",
                    text=pretty_json(body),
                    timestamp=utc_iso(),
                )
            )

    async def _handle_index(self, _: web.Request) -> web.Response:
        return web.Response(text=HTML_PAGE, content_type="text/html")

    async def _handle_events(self, _: web.Request) -> web.StreamResponse:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._subscribers.add(queue)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(_)
        await self._write_sse(
            response,
            {
                "type": "snapshot",
                "status": self._status_payload(),
                "messages": [entry.to_payload() for entry in self._history],
            },
        )

        try:
            while self._running:
                item = await queue.get()
                if item.get("type") == "shutdown":
                    break
                await self._write_sse(response, item)
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            self._subscribers.discard(queue)
            with contextlib.suppress(Exception):
                await response.write_eof()
        return response

    async def _handle_send(self, request: web.Request) -> web.Response:
        payload = await request.json()
        raw_text = str(payload.get("text", "")).strip()
        if not raw_text:
            return web.json_response({"error": "message text is empty"}, status=400)

        session = self._active_session
        if session is None:
            return web.json_response({"error": "no active veil session"}, status=409)

        body = self._normalize_message(raw_text)
        if isinstance(body, dict):
            body.setdefault("sender", self._name)
            body.setdefault("timestamp", utc_iso())

        sent = session.send_json(body, stream_id=1)
        if not sent:
            return web.json_response({"error": "veil send queue is full"}, status=503)

        self._record_entry(
            ChatEntry(
                origin="local",
                label=f"local · {self._name}",
                text=pretty_json(body),
                timestamp=utc_iso(),
            )
        )
        return web.json_response({"ok": True})

    def _normalize_message(self, raw_text: str) -> Any:
        try:
            decoded = json.loads(raw_text)
        except json.JSONDecodeError:
            return {
                "type": "chat",
                "text": raw_text,
            }
        return decoded

    def _record_system(self, text: str) -> None:
        self._record_entry(
            ChatEntry(
                origin="system",
                label="system",
                text=text,
                timestamp=utc_iso(),
            )
        )

    def _record_entry(self, entry: ChatEntry) -> None:
        self._history.append(entry)
        self._history = self._history[-200:]
        self._publish({"type": "entry", "entry": entry.to_payload()})

    def _publish_status(self, *, phase: str | None = None) -> None:
        status = self._status_payload(phase=phase)
        self._publish({"type": "status", "status": status})

    def _status_payload(self, *, phase: str | None = None) -> dict[str, Any]:
        session = self._active_session
        peer = f"{self._veil_host}:{self._veil_port}"
        if session is not None and session.remote_host:
            peer = f"{session.remote_host}:{session.remote_port}"
        return {
            "mode": self._mode,
            "connected": session is not None,
            "phase": phase or ("ready" if session is not None else "waiting for peer"),
            "peer": peer,
            "session_id": f"{session.session_id:#x}" if session is not None else "-",
            "name": self._name,
        }

    def _publish(self, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def _write_sse(self, response: web.StreamResponse, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        await response.write(f"data: {data}\n\n".encode("utf-8"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["server", "client"], required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--veil-port", type=int, default=4433)
    parser.add_argument("--ui-host", default="127.0.0.1")
    parser.add_argument("--ui-port", type=int, required=True)
    parser.add_argument("--name", default="veil-user")
    parser.add_argument("--retry-delay", type=float, default=1.5)
    parser.add_argument("--psk-hex", default=PSK.hex())
    args = parser.parse_args()

    app = LocalChatApp(
        mode=args.mode,
        host=args.host,
        veil_port=args.veil_port,
        ui_host=args.ui_host,
        ui_port=args.ui_port,
        name=args.name,
        psk=bytes.fromhex(args.psk_hex),
        retry_delay=args.retry_delay,
    )
    await app.start()
    print(
        f"[chat] mode={args.mode} veil={args.host}:{args.veil_port} "
        f"ui=http://{args.ui_host}:{args.ui_port} name={args.name}"
    )

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
