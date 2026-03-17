# Python Handoff Guide

Этот документ для Python-разработчика, который не хочет читать C++.

Коротко:
- транспорт, handshake, шифрование, фрагментация и session routing живут в C++
- Python пишет только прикладную логику вокруг `Server`, `Client` и событий
- если API ниже хватает, в плюсы лезть не нужно

## Что уже готово

- реальный handshake через `psk`
- end-to-end передача данных через `Server.send()` / `Client.send()`
- `disconnect()` с remote propagation
- `DisconnectedEvent` на stop / remote disconnect / idle timeout
- asyncio-friendly wrappers
- smoke example и тесты

## Что писать на Python

Обычный server-side код выглядит так:

1. создать `Server(...)`
2. вызвать `start()` или использовать `async with`
3. читать `async for event in server.events():`
4. на `DataEvent` вызывать свою бизнес-логику
5. при необходимости отвечать через `server.send(event.session_id, ...)`

Обычный client-side код:

1. создать `Client(...)`
2. вызвать `start()` или использовать `async with`
3. вызвать `await client.connect()`
4. отправлять данные через `client.send(...)`
5. читать `async for event in client.events():`

Если нужен более прикладной старт без ручного `session_id`, используй:

1. `await server.accept()`
2. `await client.connect_session()`
3. работать через `session.send(...)` / `await session.recv(...)`
4. для структурированных сообщений использовать `session.send_json(...)` / `await session.recv_json(...)`

## Минимальный mental model

- `session_id` это opaque идентификатор сессии из C++
- `stream_id` это логический stream внутри сессии
- `psk` должен совпадать у обеих сторон
- `NewConnectionEvent` означает, что handshake завершён и transport session уже готова
- после этого `send()` можно использовать сразу

## API Cheat Sheet

### Server

Конструктор:

```python
Server(
    port: int,
    host: str = "0.0.0.0",
    protocol_wrapper: str = "none",
    persona_preset: str = "custom",
    enable_http_handshake_emulation: bool = False,
    rotation_interval_seconds: int = 30,
    handshake_timeout_ms: int = 5000,
    session_idle_timeout_ms: int = 0,
    mtu: int = 1400,
    psk: bytes = bytes([0xAB]) * 32,
)
```

Основные методы:

- `start()`
- `stop()`
- `send(session_id, data, stream_id=0) -> bool`
- `disconnect(session_id) -> bool`
- `stats() -> dict`
- `events() -> AsyncIterator[Event]`

### Client

Конструктор:

```python
Client(
    host: str,
    port: int,
    local_port: int = 0,
    protocol_wrapper: str = "none",
    persona_preset: str = "custom",
    enable_http_handshake_emulation: bool = False,
    rotation_interval_seconds: int = 30,
    handshake_timeout_ms: int = 5000,
    session_idle_timeout_ms: int = 0,
    mtu: int = 1400,
    psk: bytes = bytes([0xAB]) * 32,
)
```

Основные методы:

- `start()`
- `stop()`
- `await connect() -> NewConnectionEvent`
- `await connect_session() -> Session`
- `send(data, stream_id=1, session_id=None) -> bool`
- `disconnect(session_id=None) -> bool`
- `stats() -> dict`
- `events() -> AsyncIterator[Event]`

### Session

- `session_id`
- `remote_host`
- `remote_port`
- `send(data, stream_id=None) -> bool`
- `await recv(timeout=None, stream_id=None) -> DataEvent`
- `send_json(body, stream_id=None) -> bool`
- `await recv_json(timeout=None, stream_id=None) -> Message`
- `disconnect() -> bool`

## События

Все события лежат в [events.py](veil_core/events.py).

Типы:

- `NewConnectionEvent`
- `DataEvent`
- `DisconnectedEvent`
- `ErrorEvent`

Практические правила:

- `ErrorEvent(session_id=0, ...)` обычно означает проблему до установления сессии
- `DisconnectedEvent` не означает обязательно ошибку: это может быть нормальный `disconnect()` или `stop()`
- `events()` после `stop()` ещё отдаст уже поставленные в очередь события и только потом завершится

## Рекомендуемый старт

### 1. Запусти smoke

```bash
cd veil-coreeee
PYTHONPATH=bindings/python python3 bindings/python/examples/smoke_roundtrip.py
```

Если видишь `roundtrip OK`, значит Python wrapper, compiled extension и transport path живы.

### 2. Посмотри простые примеры

- [example_server.py](examples/example_server.py)
- [example_client.py](examples/example_client.py)

### 3. Только потом пиши app logic

Самая нормальная точка расширения — код в обработке `DataEvent`.

## Готовый production skeleton

```python
import asyncio
from veil_core import Server, DataEvent, DisconnectedEvent, ErrorEvent, NewConnectionEvent

PSK = bytes.fromhex("ab" * 32)


async def main() -> None:
    server = Server(
        port=4433,
        host="0.0.0.0",
        psk=PSK,
        session_idle_timeout_ms=30_000,
    )

    sessions: dict[int, dict] = {}

    async with server:
        async for event in server.events():
            if isinstance(event, NewConnectionEvent):
                sessions[event.session_id] = {
                    "peer": (event.remote_host, event.remote_port),
                }
            elif isinstance(event, DataEvent):
                response = handle_payload(event.session_id, event.stream_id, event.data)
                if response is not None:
                    server.send(event.session_id, response, stream_id=event.stream_id)
            elif isinstance(event, DisconnectedEvent):
                sessions.pop(event.session_id, None)
            elif isinstance(event, ErrorEvent):
                print(f"[warn] sid={event.session_id:#x}: {event.message}")


def handle_payload(session_id: int, stream_id: int, payload: bytes) -> bytes | None:
    return b"ack:" + payload


asyncio.run(main())
```

## Когда всё-таки лезть в C++

Не надо идти в C++, если тебе нужно:

- принять соединение
- получить `DataEvent`
- отправить ответ
- вести reconnect loop
- вести session-local state в Python

Надо идти в C++, если тебе нужно:

- менять wire format
- менять handshake protocol
- добавлять новые internal control frames
- менять routing / session identity
- добавлять новый low-level callback прямо из transport

## Проверки, которые уже пройдены

На текущем состоянии уже проверены:

- C++ integration handshake round-trip
- end-to-end data flow
- mismatched PSK
- handshake timeout
- invalid handshake response
- reconnect после disconnect
- remote disconnect propagation
- idle timeout disconnect
- Python wrapper tests
- live Python smoke через compiled extension

Дополнительно для качества и производительности уже есть готовые артефакты:
- JSON summary со свежим clean-release слепком: [latest_quality_metrics_clean_release.json](metrics/latest_quality_metrics_clean_release.json)
- CSV с распределениями handshake/sustained/windowed/ingress pacing рядом с ним в [metrics](metrics)

## Как гонять тесты по слоям

### 1. Binding interface quality

Эти тесты отвечают на вопрос:
"Python/C++ интерфейс удобный, корректный и не ломает lifecycle?"

Локальные wrapper-тесты:

```bash
cd veil-coreeee/bindings
python -m unittest discover -s python/tests
```

Живые e2e smoke-тесты поверх compiled extension:

```bash
cd veil-coreeee
PYTHONPATH=bindings/python python3 -m unittest bindings.python.tests.test_live_smoke
```

Что они покрывают:

- handshake + round-trip через реальные `Server`/`Client`
- `stream_id` preservation
- multi-message flow
- disconnect/reconnect lifecycle
- context-manager semantics
- queue drain после `stop()`

### 2. Protocol quality

Эти тесты отвечают на вопрос:
"transport path реально рабочий и насколько он быстрый/устойчивый?"

Основные integration-тесты:

```bash
cd veil-coreeee
./build/tests/integration/veil_integration_transport
```

Только quality-метрики:

```bash
./build/tests/integration/veil_integration_transport \
  --gtest_filter=TransportIntegrationTest.LoopbackRoundTripLatencyMetricsStaySane:TransportIntegrationTest.CoreEncryptDecryptThroughputMetricsStaySane
```

Что они покрывают:

- real UDP loopback latency sanity
- encrypt/decrypt throughput sanity на protocol core под ASan
- fragmentation/reassembly
- replay protection
- session rotation
- ACK path
- WebSocket HTTP prelude path

Для более честной верхней границы core throughput exporter дополнительно строит `veil-performance-validation` в release-подобной конфигурации и пишет результат в `release_like_core_payload_throughput_mbps` внутри [latest_quality_metrics.json](metrics/latest_quality_metrics.json).

### 3. Самый короткий smoke

Если нужен просто "жив ли стек вообще":

```bash
cd veil-coreeee
PYTHONPATH=bindings/python python3 bindings/python/examples/smoke_roundtrip.py
```

## Частые проблемы

### `connect()` не проходит

Проверь:

- одинаковый ли `psk`
- правильный ли `host:port`
- запущен ли сервер
- не слишком ли маленький `handshake_timeout_ms`

### `send()` вернул `False`

Это back-pressure: очередь pipeline не приняла данные. Обычно это не crash, а сигнал подождать и попробовать позже.

### После `stop()` не все события видны

Нормальный паттерн:

```python
server.stop()
async for event in server.events():
    ...
```

`events()` дренирует хвост очереди, но новые события после stop уже не приходят.

## Где смотреть дальше

- [README.md](README.md)
- [QUALITY.md](QUALITY.md)
- [events.py](veil_core/events.py)
- [example_server.py](examples/example_server.py)
- [example_client.py](examples/example_client.py)
- [smoke_roundtrip.py](examples/smoke_roundtrip.py)
