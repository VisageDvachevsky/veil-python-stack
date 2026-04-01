# veil_core — Python bindings

Python-обёртка над криптографическим транспортным ядром Veil (C++).

(спешл фор марк)
Вся тяжёлая работа (шифрование ChaCha20-Poly1305, фрагментация, ретрансмиссия, обфускация трафика)
выполняется в C++ и недоступна из Python напрямую — это сделано намеренно, чтобы твой код оставался
простым и быстрым одновременно.

Если тебе нужно быстро начать без чтения C++:
- смотри [HANDOFF.md](HANDOFF.md)
- для packet-oriented overlay смотри [VPN.md](VPN.md)
- для минимального живого прогона смотри [smoke_roundtrip.py](examples/smoke_roundtrip.py)
- для quality/perf baseline смотри [QUALITY.md](QUALITY.md) и артефакты в [metrics](metrics)

---

## Структура

```
bindings/python/
├── CMakeLists.txt            # Сборка C++ расширения (pybind11)
├── README.md                 # Этот файл
├── _veil_core_ext/           # C++ исходники расширения
│   ├── node.h / node.cpp     # VeilNode: UDP EventLoop + роутер сессий
│   └── bindings.cpp          # pybind11 модуль
├── veil_core/                # Python пакет
│   ├── __init__.py           # Публичный API
│   ├── server.py             # Класс Server
│   ├── client.py             # Класс Client
│   └── events.py             # Типы событий (dataclasses)
└── examples/
    ├── example_server.py
    └── example_client.py
    └── smoke_roundtrip.py
```

---

## Сборка (один раз, в WSL / Linux)

```bash
# 1. Перейди в корень репозитория
cd veil-coreeee

# 2. Сконфигурируй CMake с флагом для Python-биндингов
cmake -B build -DVEIL_BUILD_PYTHON_BINDINGS=ON

# 3. Собери только расширение (быстро, ~30 секунд)
cmake --build build --target _veil_core_ext -j$(nproc)
```

После сборки файл `_veil_core_ext.so` (Linux) или `_veil_core_ext.pyd` (Windows)
появится рядом с папкой `veil_core/`.

---

## Использование

Обе стороны должны использовать один и тот же `psk`. Это обязательный pre-shared key для handshake.
Если `psk` не совпадает, сессия не установится, а клиент получит `ErrorEvent`.

### Сервер

```python
import asyncio
from veil_core import Server, DataEvent, DisconnectedEvent, ErrorEvent, NewConnectionEvent

PSK = bytes.fromhex("ab" * 32)

async def main():
    server = Server(port=4433, psk=PSK)
    server.start()

    async for event in server.events():
        if isinstance(event, NewConnectionEvent):
            print(f"Новый клиент: {event.session_id:#x}")

        elif isinstance(event, DataEvent):
            print(f"Получили: {event.data!r}")
            server.send(event.session_id, b"pong")  # ответить

        elif isinstance(event, DisconnectedEvent):
            print(f"Клиент {event.session_id:#x} отключился: {event.reason}")

        elif isinstance(event, ErrorEvent):
            print(f"[warn] {event.session_id:#x}: {event.message}")

asyncio.run(main())
```

---

## Быстрая проверка, что всё живо

После сборки биндингов можно проверить end-to-end round-trip так:

```bash
cd veil-coreeee
PYTHONPATH=bindings/python python3 bindings/python/examples/smoke_roundtrip.py
```

Ожидаемый результат:

```text
[smoke] server accepted session ...
[smoke] client connected as ...
[smoke] server received b'smoke-ping'
[smoke] client received b'smoke-pong' on stream 42
[smoke] roundtrip OK
```

### Клиент

```python
import asyncio
from veil_core import Client, DataEvent, DisconnectedEvent, ErrorEvent

PSK = bytes.fromhex("ab" * 32)

async def main():
    async with Client(host="127.0.0.1", port=4433, psk=PSK) as client:
        await client.connect()
        client.send(b"ping")

        reply = await client.recv(timeout=5.0, stream_id=1)
        print(f"Сервер ответил: {reply.data!r}")

asyncio.run(main())
```

### Session-oriented start point

Если не хочется таскать `session_id` вручную, используй session API:

```python
import asyncio
from veil_core import Client, Server

PSK = bytes.fromhex("ab" * 32)

async def main() -> None:
    server = Server(port=4433, host="127.0.0.1", psk=PSK)
    client = Client(host="127.0.0.1", port=4433, psk=PSK)

    async with server, client:
        accept_task = asyncio.create_task(server.accept(timeout=5.0))
        client_session = await client.connect_session()
        server_session = await accept_task

        client_session.send(b"ping", stream_id=7)
        event = await server_session.recv(timeout=5.0, stream_id=7)
        server_session.send(b"pong", stream_id=event.stream_id)

        reply = await client_session.recv(timeout=5.0, stream_id=7)
        print(reply.data)

asyncio.run(main())
```

Ключевые методы:

- `await client.connect_session() -> Session`
- `await server.accept() -> Session`
- `session.send(data, stream_id=...)`
- `await session.recv(timeout=..., stream_id=...)`
- `session.send_json(body, stream_id=...)`
- `await session.recv_json(timeout=..., stream_id=...)`
- `session.disconnect()`

### Desktop Client

Для реального клиентского сценария без браузера теперь есть desktop client:

- entrypoint: `desktop/veil_chat_client.py`
- config template: `desktop/veil_chat_client.example.json`
- Windows build script: `desktop/build_windows_client.ps1`
- Inno Setup installer script: `desktop/veil_chat_client.iss`

Локальный запуск:

```bash
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libasan.so.8 \
ASAN_OPTIONS=detect_leaks=0 \
python3 desktop/veil_chat_client.py --host 192.168.0.102 --port 4433 --name guest
```

Сборка на Windows:

```powershell
pwsh -File desktop/build_windows_client.ps1
```

После сборки рядом с `veil-chat-client.exe` кладётся `veil_chat_client.json`,
чтобы клиент просто открыл `.exe` и сразу пошёл коннект на нужный сервер.

Если нужен installer, после сборки `.exe` запускается Inno Setup со
скриптом `desktop/veil_chat_client.iss`.

### Linux VPN Client

Для Linux теперь есть отдельный VPN client stack:

- GUI client: `desktop/veil_vpn_client.py`
- config template: `desktop/veil_vpn_client.example.json`
- CLI controller: `desktop/veil_vpn_ctl.py`
- installer: `desktop/install_linux_client.py`

Быстрый старт:

```bash
cd submodules/veil-python-stack
python3 desktop/install_linux_client.py
~/.local/bin/veil-vpn doctor
~/.local/bin/veil-vpn-gui
```

Подробности по TUN/full-tunnel flow смотри в [VPN.md](VPN.md).

### Windows VPN Client

Для Windows теперь есть отдельный VPN packaging/runtime path:

- GUI client: `desktop/veil_vpn_client.py`
- Windows controller: `desktop/veil_vpn_windows_ctl.py`
- Windows agent: `desktop/veil_vpn_agent.py`
- Wintun bridge: `veil_core/windows_wintun.py`
- build script: `desktop/build_windows_vpn_client.ps1`
- installer script: `desktop/veil_vpn_client_windows.iss`
- operator guide: [WINDOWS_VPN.md](WINDOWS_VPN.md)
- VM smoke for protocol/runtime validation: [WINDOWS_VM_SMOKE.md](WINDOWS_VM_SMOKE.md)

Целевой deliverable:

- `VeilVPN-Setup-x64.exe`

Этот installer тащит GUI, agent, runtime, config template и `wintun.dll` в одном пакете.

---

## Параметры Server / Client

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `port` | int | — | UDP порт |
| `host` | str | `"0.0.0.0"` | IP / хост |
| `protocol_wrapper` | str | `"none"` | `"none"` / `"websocket"` / `"tls"` |
| `persona_preset` | str | `"custom"` | `"browser_ws"` / `"quic_media"` / `"interactive_game"` / `"low_noise_enterprise"` |
| `enable_http_handshake_emulation` | bool | `False` | Эмуляция HTTP Upgrade (нужна при `websocket`) |
| `rotation_interval_seconds` | int | `30` | Частота ротации сессионных ключей |
| `handshake_timeout_ms` | int | `5000` | Таймаут установления handshake |
| `session_idle_timeout_ms` | int | `0` | Idle timeout сессии в миллисекундах, `0` = отключено |
| `mtu` | int | `1400` | Максимальный размер UDP пакета |
| `psk` | bytes | `b\"\\xab\" * 32` | Общий pre-shared key для handshake |

---

## Контракт событий

- `NewConnectionEvent` приходит только после успешного handshake. Если событие пришло, `send()` уже безопасно использовать.
- `DataEvent` приходит только для data frames. Внутренние control frames в Python не пробрасываются.
- `DisconnectedEvent` приходит при локальном `disconnect()`, при `stop()`, при remote disconnect и при idle timeout.
- `ErrorEvent(session_id=0, ...)` используется для client-side handshake ошибок до появления сессии.
- После `stop()` `events()` дренирует уже поставленные в очередь события и затем завершается.

Поля событий:

- `NewConnectionEvent(session_id, remote_host, remote_port)`
- `DataEvent(session_id, stream_id, data)`
- `DisconnectedEvent(session_id, reason)`
- `ErrorEvent(session_id, message)`

---

## Что делать НЕ нужно

- Не нужно думать о фрагментации: можно передавать любой размер данных в `send()`.
- Не нужно управлять сессиями явно: C++ находит нужную сессию по `session_id`.
- Не нужно вызывать `encrypt` / `decrypt` вручную: всё происходит автоматически.

Но есть что понимать:

- `Client.connect()` ждёт завершения handshake и бросает `RuntimeError`, если handshake завершился ошибкой.
- `disconnect()` закрывает сессию локально и отправляет peer-у control frame disconnect.
- Если нужен reconnect после ошибки или disconnect, просто вызови `connect()` заново.

### Удобный приём данных

Если тебе не нужен общий `async for event in ...`, можно ждать только data frame:

```python
reply = await client.recv(timeout=5.0, stream_id=42)
print(reply.data)
```

На сервере это выглядит так же:

```python
event = await server.recv(timeout=5.0, session_id=session_id, stream_id=42)
server.send(event.session_id, b"ack", stream_id=event.stream_id)
```

Семантика простая:

- `recv()` возвращает только `DataEvent`
- `session_id` и `stream_id` фильтры опциональны
- неподходящие события не теряются и остаются доступными через `events()` или `next_event()`

### Минимальный reconnect loop

```python
import asyncio
from veil_core import Client, DataEvent, DisconnectedEvent, ErrorEvent

PSK = bytes.fromhex("ab" * 32)

async def run_client() -> None:
    while True:
        try:
            async with Client("127.0.0.1", 4433, psk=PSK) as client:
                await client.connect()
                client.send(b"hello", stream_id=1)

                async for event in client.events():
                    if isinstance(event, DataEvent):
                        print("reply:", event.data)
                    elif isinstance(event, DisconnectedEvent):
                        print("disconnected:", event.reason)
                        break
                    elif isinstance(event, ErrorEvent):
                        print("error:", event.message)
                        break
        except RuntimeError as exc:
            print("connect failed:", exc)

        await asyncio.sleep(1.0)

asyncio.run(run_client())
```

---

## Добавление обработки событий на production

Просто расширяй условие `async for event in server.events()`:

```python
from veil_core import Server, DataEvent, DisconnectedEvent, ErrorEvent
import asyncio

async def main():
    server = Server(port=4433, protocol_wrapper="websocket",
                    persona_preset="browser_ws",
                    enable_http_handshake_emulation=True,
                    psk=bytes.fromhex("ab" * 32),
                    session_idle_timeout_ms=30_000)
    server.start()

    sessions = {}  # session_id -> контекст твоего приложения

    async for event in server.events():
        if isinstance(event, DataEvent):
            # Здесь — любая логика на Python
            response = my_app_handler(sessions, event.session_id, event.data)
            if response:
                server.send(event.session_id, response)

        elif isinstance(event, DisconnectedEvent):
            sessions.pop(event.session_id, None)

        elif isinstance(event, ErrorEvent):
            print(f"[warn] {event.message}")

asyncio.run(main())
```

---

## Troubleshooting

### `RuntimeError: ... _veil_core_ext is not compiled`

Собери pybind extension:

```bash
cd veil-coreeee
cmake -B build -DVEIL_BUILD_PYTHON_BINDINGS=ON
cmake --build build --target _veil_core_ext
```

### `Client.connect()` падает с ошибкой handshake

Проверь:

- одинаковый ли `psk` у `Server` и `Client`
- что клиент подключается к правильному `host:port`
- что сервер реально вызвал `start()`
- не слишком ли маленький `handshake_timeout_ms`

### Сессия закрывается сама

Самая частая причина — `session_idle_timeout_ms`. Если не нужен idle timeout, оставь `0`.

### Хочется понять, жив ли transport path

Запусти [smoke_roundtrip.py](examples/smoke_roundtrip.py). Это самый короткий end-to-end smoke поверх реального `Server`, `Client` и compiled extension.
