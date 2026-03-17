from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import socket
import struct
import statistics
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

try:
    from veil_core import Client, DataEvent, DisconnectedEvent, NewConnectionEvent, Server
    from veil_core import _veil_core_ext  # noqa: F401
    EXT_AVAILABLE = True
except ImportError:
    Client = None  # type: ignore[assignment]
    DataEvent = None  # type: ignore[assignment]
    DisconnectedEvent = None  # type: ignore[assignment]
    NewConnectionEvent = None  # type: ignore[assignment]
    Server = None  # type: ignore[assignment]
    EXT_AVAILABLE = False


def reserve_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def summarize_samples_ms(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    if not ordered:
        return {
            "count": 0.0,
            "avg_ms": 0.0,
            "min_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "max_ms": 0.0,
        }

    def percentile(p: float) -> float:
        index = int((p / 100.0) * (len(ordered) - 1))
        return ordered[index]

    return {
        "count": float(len(ordered)),
        "avg_ms": statistics.fmean(ordered),
        "min_ms": ordered[0],
        "p50_ms": percentile(50.0),
        "p95_ms": percentile(95.0),
        "max_ms": ordered[-1],
    }


def summarize_rate_samples(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {
            "count": 0.0,
            "avg": 0.0,
            "min": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
    ordered = sorted(samples)

    def percentile(p: float) -> float:
        index = int((p / 100.0) * (len(ordered) - 1))
        return ordered[index]

    return {
        "count": float(len(ordered)),
        "avg": statistics.fmean(ordered),
        "min": ordered[0],
        "p50": percentile(50.0),
        "p95": percentile(95.0),
        "max": ordered[-1],
    }


def summarize_variance(samples: list[float]) -> dict[str, float]:
    summary = summarize_rate_samples(samples)
    if not samples:
        summary["stdev"] = 0.0
        summary["cv"] = 0.0
        return summary
    if len(samples) == 1:
        summary["stdev"] = 0.0
        summary["cv"] = 0.0
        return summary
    stdev = statistics.pstdev(samples)
    avg = statistics.fmean(samples)
    summary["stdev"] = stdev
    summary["cv"] = (stdev / avg) if avg > 0 else 0.0
    return summary


def stat_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    non_monotonic_gauges = {"active_sessions"}
    delta: dict[str, int] = {}
    keys = set(before) | set(after)
    for key in keys:
        if key in non_monotonic_gauges:
            continue
        before_value = before.get(key)
        after_value = after.get(key)
        if isinstance(before_value, int) and isinstance(after_value, int):
            delta[key] = after_value - before_value
    return delta


def ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def make_timed_payload(payload_size: int, sequence: int) -> bytes:
    sent_at_ns = time.perf_counter_ns()
    header = struct.pack("!QQ", sent_at_ns, sequence)
    if payload_size <= len(header):
        return header[:payload_size]
    return header + bytes([sequence & 0xFF]) * (payload_size - len(header))


def extract_payload_timestamp_ns(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 16:
        return None
    sent_at_ns, sequence = struct.unpack("!QQ", payload[:16])
    return sent_at_ns, sequence


class EventDeliveryProbe:
    def __init__(self, endpoint: Any) -> None:
        self._endpoint = endpoint
        self._lock = threading.Lock()
        self._callback_ns: dict[int, int] = {}
        self._enqueued_ns: dict[int, int] = {}
        self._original_push_event = endpoint._push_event
        self._original_put_nowait = endpoint._queue.put_nowait

    def install(self) -> None:
        def instrumented_push(event: Event) -> None:
            decoded = None
            if isinstance(event, DataEvent):
                decoded = extract_payload_timestamp_ns(event.data)
            if decoded is None:
                self._original_push_event(event)
                return

            sequence = decoded[1]
            callback_ns = time.perf_counter_ns()
            with self._lock:
                self._callback_ns[sequence] = callback_ns

            if self._endpoint._loop is not None and self._endpoint._loop.is_running():
                def enqueue() -> None:
                    enqueue_ns = time.perf_counter_ns()
                    with self._lock:
                        self._enqueued_ns[sequence] = enqueue_ns
                    self._original_put_nowait(event)

                self._endpoint._loop.call_soon_threadsafe(enqueue)
            else:
                self._original_push_event(event)

        self._endpoint._push_event = instrumented_push

    def uninstall(self) -> None:
        self._endpoint._push_event = self._original_push_event

    def snapshot(self, sequence: int) -> tuple[int | None, int | None]:
        with self._lock:
            return self._callback_ns.get(sequence), self._enqueued_ns.get(sequence)


def collect_system_probes() -> dict[str, Any]:
    udp_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count() or 0,
            "asyncio_event_loop": asyncio.get_running_loop().__class__.__name__,
            "monotonic_resolution_s": time.get_clock_info("monotonic").resolution,
            "perf_counter_resolution_s": time.get_clock_info("perf_counter").resolution,
            "udp_default_rcvbuf": udp_probe.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
            "udp_default_sndbuf": udp_probe.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
        }
    finally:
        udp_probe.close()


async def collect_roundtrip_metrics(*, psk: bytes, port: int, iterations: int = 8) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None and NewConnectionEvent is not None

    server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
    client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)

    handshake_started = time.perf_counter()
    roundtrip_samples_ms: list[float] = []
    server_events = 0

    async with server, client:
        server_ready = asyncio.Event()
        server_session_id: int | None = None

        async def server_loop() -> None:
            nonlocal server_events, server_session_id
            async for event in server.events():
                if isinstance(event, NewConnectionEvent):
                    server_events += 1
                    server_session_id = event.session_id
                    server_ready.set()
                    continue
                if isinstance(event, DataEvent):
                    server_events += 1
                    assert server.send(event.session_id, b"ack:" + event.data, stream_id=event.stream_id)
                    if server_events >= iterations + 1:
                        break

        server_task = asyncio.create_task(server_loop())
        connection = await asyncio.wait_for(client.connect(), timeout=5)
        handshake_ms = (time.perf_counter() - handshake_started) * 1000.0
        await asyncio.wait_for(server_ready.wait(), timeout=5)

        for index in range(iterations):
            payload = f"msg-{index}".encode()
            start = time.perf_counter()
            assert client.send(payload, stream_id=100 + index)

            while True:
                event = await asyncio.wait_for(client._queue.get(), timeout=5)
                if isinstance(event, DataEvent):
                    roundtrip_samples_ms.append((time.perf_counter() - start) * 1000.0)
                    if event.data == b"ack:" + payload and event.stream_id == 100 + index:
                        break

        await asyncio.wait_for(server_task, timeout=5)
        client_stats = client.stats()
        server_stats = server.stats()

    return {
        "test_name": "full_stack_roundtrip",
        "handshake_ms": handshake_ms,
        "client_session_id": connection.session_id,
        "server_session_id": server_session_id,
        "roundtrip": summarize_samples_ms(roundtrip_samples_ms),
        "client_stats": client_stats,
        "server_stats": server_stats,
    }


async def collect_reconnect_metrics(*, psk: bytes, port: int) -> dict[str, Any]:
    assert Server is not None and Client is not None and DisconnectedEvent is not None

    server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
    client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)

    async with server, client:
        first_start = time.perf_counter()
        first_conn = await asyncio.wait_for(client.connect(), timeout=5)
        first_handshake_ms = (time.perf_counter() - first_start) * 1000.0

        disconnect_start = time.perf_counter()
        assert client.disconnect()
        disconnected_event = None
        while disconnected_event is None:
            event = await asyncio.wait_for(client._queue.get(), timeout=5)
            if isinstance(event, DisconnectedEvent):
                disconnected_event = event
        disconnect_propagation_ms = (time.perf_counter() - disconnect_start) * 1000.0

        reconnect_start = time.perf_counter()
        second_conn = await asyncio.wait_for(client.connect(), timeout=5)
        reconnect_handshake_ms = (time.perf_counter() - reconnect_start) * 1000.0

        return {
            "test_name": "full_stack_reconnect",
            "first_session_id": first_conn.session_id,
            "second_session_id": second_conn.session_id,
            "first_handshake_ms": first_handshake_ms,
            "disconnect_propagation_ms": disconnect_propagation_ms,
            "reconnect_handshake_ms": reconnect_handshake_ms,
            "final_client_stats": client.stats(),
            "final_server_stats": server.stats(),
        }


async def collect_stream_fanout_metrics(*, psk: bytes, port: int, messages: int = 6) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None

    server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
    client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)

    async with server, client:
        await asyncio.wait_for(client.connect(), timeout=5)
        seen_on_server: list[dict[str, Any]] = []

        async def server_loop() -> None:
            async for event in server.events():
                if isinstance(event, DataEvent):
                    seen_on_server.append(
                        {
                            "stream_id": event.stream_id,
                            "payload_size": len(event.data),
                        }
                    )
                    assert server.send(event.session_id, b"ok:" + event.data, stream_id=event.stream_id)
                    if len(seen_on_server) >= messages:
                        break

        server_task = asyncio.create_task(server_loop())

        roundtrip_samples_ms: list[float] = []
        for idx in range(messages):
            payload = bytes([0x41 + idx]) * (32 + idx * 16)
            stream_id = 1000 + idx
            start = time.perf_counter()
            assert client.send(payload, stream_id=stream_id)
            while True:
                event = await asyncio.wait_for(client._queue.get(), timeout=5)
                if isinstance(event, DataEvent) and event.stream_id == stream_id:
                    roundtrip_samples_ms.append((time.perf_counter() - start) * 1000.0)
                    break

        await asyncio.wait_for(server_task, timeout=5)
        return {
            "test_name": "full_stack_stream_fanout",
            "messages": messages,
            "server_observed": seen_on_server,
            "roundtrip": summarize_samples_ms(roundtrip_samples_ms),
            "client_stats": client.stats(),
            "server_stats": server.stats(),
        }


async def collect_payload_sweep_metrics(
    *,
    psk: bytes,
    port: int,
    payload_sizes: list[int] | None = None,
    iterations_per_size: int = 6,
) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None
    if payload_sizes is None:
        payload_sizes = [32, 128, 512, 1024, 4096]

    server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
    client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)

    async with server, client:
        await asyncio.wait_for(client.connect(), timeout=5)

        async def server_loop(expected_messages: int) -> None:
            seen = 0
            async for event in server.events():
                if isinstance(event, DataEvent):
                    assert server.send(event.session_id, b"ack:" + event.data, stream_id=event.stream_id)
                    seen += 1
                    if seen >= expected_messages:
                        break

        total_messages = len(payload_sizes) * iterations_per_size
        server_task = asyncio.create_task(server_loop(total_messages))

        per_size: list[dict[str, Any]] = []
        for payload_size in payload_sizes:
            latencies_ms: list[float] = []
            effective_mb_s_samples: list[float] = []
            for iteration in range(iterations_per_size):
                payload = bytes([(payload_size + iteration) & 0xFF]) * payload_size
                stream_id = payload_size * 100 + iteration
                started = time.perf_counter()
                assert client.send(payload, stream_id=stream_id)

                while True:
                    event = await asyncio.wait_for(client._queue.get(), timeout=5)
                    if isinstance(event, DataEvent) and event.stream_id == stream_id:
                        elapsed_ms = (time.perf_counter() - started) * 1000.0
                        latencies_ms.append(elapsed_ms)
                        effective_mb_s_samples.append(
                            (payload_size / (1024.0 * 1024.0)) / (elapsed_ms / 1000.0)
                        )
                        break

            per_size.append(
                {
                    "payload_size": payload_size,
                    "latency_ms": summarize_samples_ms(latencies_ms),
                    "effective_payload_mb_s": summarize_rate_samples(effective_mb_s_samples),
                }
            )

        await asyncio.wait_for(server_task, timeout=5)
        return {
            "test_name": "full_stack_payload_sweep",
            "iterations_per_size": iterations_per_size,
            "results": per_size,
            "client_stats": client.stats(),
            "server_stats": server.stats(),
        }


async def collect_sustained_throughput_metrics(
    *,
    psk: bytes,
    port: int,
    messages: int = 64,
    payload_size: int = 2048,
) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None

    server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
    client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)

    async with server, client:
        await asyncio.wait_for(client.connect(), timeout=5)
        before_client = client.stats()
        before_server = server.stats()

        server_received = 0
        async def server_loop() -> None:
            nonlocal server_received
            async for event in server.events():
                if isinstance(event, DataEvent):
                    server_received += 1
                    assert server.send(event.session_id, b"ok", stream_id=event.stream_id)
                    if server_received >= messages:
                        break

        server_task = asyncio.create_task(server_loop())

        started = time.perf_counter()
        for idx in range(messages):
            payload = bytes([idx & 0xFF]) * payload_size
            assert client.send(payload, stream_id=10_000 + idx)

        client_received = 0
        ack_deadline = time.monotonic() + 10.0
        while client_received < messages and time.monotonic() < ack_deadline:
            timeout_left = max(0.0, ack_deadline - time.monotonic())
            if timeout_left <= 0.0:
                break
            try:
                event = await asyncio.wait_for(client._queue.get(), timeout=min(1.0, timeout_left))
            except asyncio.TimeoutError:
                break
            if isinstance(event, DataEvent):
                client_received += 1

        elapsed_s = time.perf_counter() - started
        try:
            await asyncio.wait_for(server_task, timeout=1)
        except asyncio.TimeoutError:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task

        total_payload_bytes = messages * payload_size
        payload_mb_s = (total_payload_bytes / (1024.0 * 1024.0)) / elapsed_s if elapsed_s > 0 else 0.0
        packets_per_second = messages / elapsed_s if elapsed_s > 0 else 0.0
        ack_loss_count = messages - client_received
        ack_loss_ratio = (ack_loss_count / messages) if messages > 0 else 0.0
        client_after = client.stats()
        server_after = server.stats()
        client_delta = stat_delta(before_client, client_after)
        server_delta = stat_delta(before_server, server_after)
        ack_delivery_ratio = ratio(float(client_received), float(messages))
        message_delivery_ratio = ratio(float(server_received), float(messages))
        fragment_delivery_ratio = ratio(
            float(server_delta.get("transport_fragments_received", 0)),
            float(client_delta.get("transport_fragments_sent", 0)),
        )
        message_reassembly_ratio = ratio(
            float(server_delta.get("transport_messages_reassembled", 0)),
            float(messages),
        )

        return {
            "test_name": "full_stack_sustained_throughput",
            "messages": messages,
            "payload_size": payload_size,
            "elapsed_s": elapsed_s,
            "payload_mb_s": payload_mb_s,
            "messages_per_second": packets_per_second,
            "acks_received": client_received,
            "ack_loss_count": ack_loss_count,
            "ack_loss_ratio": ack_loss_ratio,
            "ack_delivery_ratio": ack_delivery_ratio,
            "message_delivery_ratio": message_delivery_ratio,
            "fragment_delivery_ratio": fragment_delivery_ratio,
            "message_reassembly_ratio": message_reassembly_ratio,
            "client_stat_delta": client_delta,
            "server_stat_delta": server_delta,
            "client_stats": client_after,
            "server_stats": server_after,
        }


async def collect_windowed_throughput_sweep_metrics(
    *,
    psk: bytes,
    port: int,
    payload_sizes: list[int] | None = None,
    window_size: int = 8,
    max_messages_per_size: int = 128,
) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None
    if payload_sizes is None:
        payload_sizes = [256, 1024, 4096, 16384]

    server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
    client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)

    async with server, client:
        await asyncio.wait_for(client.connect(), timeout=5)
        results: list[dict[str, Any]] = []

        for payload_size in payload_sizes:
            messages = max(32, min(max_messages_per_size, (2 * 1024 * 1024) // payload_size))
            server_received = 0

            async def server_loop(expected_messages: int) -> None:
                nonlocal server_received
                async for event in server.events():
                    if isinstance(event, DataEvent):
                        server_received += 1
                        assert server.send(event.session_id, b"ok", stream_id=event.stream_id)
                        if server_received >= expected_messages:
                            break

            before_client = client.stats()
            before_server = server.stats()
            server_task = asyncio.create_task(server_loop(messages))

            started = time.perf_counter()
            latency_samples_ms: list[float] = []
            sent = 0
            acked = 0
            in_flight_started: dict[int, float] = {}
            next_stream_id = payload_size * 1000

            while sent < min(window_size, messages):
                payload = bytes([sent & 0xFF]) * payload_size
                stream_id = next_stream_id + sent
                in_flight_started[stream_id] = time.perf_counter()
                assert client.send(payload, stream_id=stream_id)
                sent += 1

            ack_deadline = time.monotonic() + 10.0
            while acked < messages and time.monotonic() < ack_deadline:
                timeout_left = max(0.0, ack_deadline - time.monotonic())
                if timeout_left <= 0.0:
                    break
                try:
                    event = await asyncio.wait_for(client._queue.get(), timeout=min(1.0, timeout_left))
                except asyncio.TimeoutError:
                    break
                if not isinstance(event, DataEvent):
                    continue
                started_at = in_flight_started.pop(event.stream_id, None)
                if started_at is None:
                    continue
                latency_samples_ms.append((time.perf_counter() - started_at) * 1000.0)
                acked += 1

                if sent < messages:
                    payload = bytes([sent & 0xFF]) * payload_size
                    stream_id = next_stream_id + sent
                    in_flight_started[stream_id] = time.perf_counter()
                    assert client.send(payload, stream_id=stream_id)
                    sent += 1

            elapsed_s = time.perf_counter() - started
            try:
                await asyncio.wait_for(server_task, timeout=1)
            except asyncio.TimeoutError:
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task
            after_client = client.stats()
            after_server = server.stats()

            payload_bytes = messages * payload_size
            client_wire_tx_bytes = after_client["tx_bytes"] - before_client["tx_bytes"]
            client_wire_rx_bytes = after_client["rx_bytes"] - before_client["rx_bytes"]
            server_wire_tx_bytes = after_server["tx_bytes"] - before_server["tx_bytes"]
            server_wire_rx_bytes = after_server["rx_bytes"] - before_server["rx_bytes"]

            payload_mb_s = (payload_bytes / (1024.0 * 1024.0)) / elapsed_s if elapsed_s > 0 else 0.0
            client_wire_tx_mb_s = (client_wire_tx_bytes / (1024.0 * 1024.0)) / elapsed_s if elapsed_s > 0 else 0.0
            server_wire_rx_mb_s = (server_wire_rx_bytes / (1024.0 * 1024.0)) / elapsed_s if elapsed_s > 0 else 0.0
            transport_efficiency = (payload_bytes / client_wire_tx_bytes) if client_wire_tx_bytes > 0 else 0.0
            ack_delivery_ratio = ratio(float(acked), float(messages))
            message_delivery_ratio = ratio(float(server_received), float(messages))
            fragment_delivery_ratio = ratio(
                float(after_server["transport_fragments_received"] - before_server["transport_fragments_received"]),
                float(after_client["transport_fragments_sent"] - before_client["transport_fragments_sent"]),
            )
            message_reassembly_ratio = ratio(
                float(after_server["transport_messages_reassembled"] - before_server["transport_messages_reassembled"]),
                float(messages),
            )

            results.append(
                {
                    "payload_size": payload_size,
                    "messages": messages,
                    "window_size": window_size,
                    "elapsed_s": elapsed_s,
                    "payload_mb_s": payload_mb_s,
                    "payload_mbit_s": payload_mb_s * 8.0,
                    "client_wire_tx_mb_s": client_wire_tx_mb_s,
                    "server_wire_rx_mb_s": server_wire_rx_mb_s,
                    "transport_efficiency": transport_efficiency,
                    "ack_loss_count": messages - acked,
                    "ack_loss_ratio": ((messages - acked) / messages) if messages > 0 else 0.0,
                    "ack_delivery_ratio": ack_delivery_ratio,
                    "message_delivery_ratio": message_delivery_ratio,
                    "fragment_delivery_ratio": fragment_delivery_ratio,
                    "message_reassembly_ratio": message_reassembly_ratio,
                    "ack_latency_ms": summarize_samples_ms(latency_samples_ms),
                    "client_stat_delta": stat_delta(before_client, after_client),
                    "server_stat_delta": stat_delta(before_server, after_server),
                    "client_wire_tx_bytes": client_wire_tx_bytes,
                    "client_wire_rx_bytes": client_wire_rx_bytes,
                    "server_wire_tx_bytes": server_wire_tx_bytes,
                    "server_wire_rx_bytes": server_wire_rx_bytes,
                }
            )

        return {
            "test_name": "full_stack_windowed_throughput_sweep",
            "payload_sizes": payload_sizes,
            "window_size": window_size,
            "results": results,
            "client_stats": client.stats(),
            "server_stats": server.stats(),
        }


async def collect_ingress_only_throughput_sweep_metrics(
    *,
    psk: bytes,
    port: int,
    payload_sizes: list[int] | None = None,
    sender_batch_size: int = 8,
    max_messages_per_size: int = 256,
) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None
    if payload_sizes is None:
        payload_sizes = [256, 1024, 4096, 16384]

    results: list[dict[str, Any]] = []
    aggregate_client_stats: dict[str, int] = {}
    aggregate_server_stats: dict[str, int] = {}

    for index, payload_size in enumerate(payload_sizes):
        bucket_port = port if index == 0 else reserve_ephemeral_port()
        messages = max(64, min(max_messages_per_size, (4 * 1024 * 1024) // payload_size))
        server = Server(port=bucket_port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=30_000)
        client = Client(host="127.0.0.1", port=bucket_port, psk=psk, handshake_timeout_ms=2_000)

        async with server, client:
            await asyncio.wait_for(client.connect(), timeout=5)
            server_received = 0
            event_latency_samples_ms: list[float] = []
            seen_sequences: set[int] = set()
            server_done = asyncio.Event()

            async def server_loop(expected_messages: int) -> None:
                nonlocal server_received
                async for event in server.events():
                    if not isinstance(event, DataEvent):
                        continue
                    server_received += 1
                    decoded = extract_payload_timestamp_ns(event.data)
                    if decoded is not None:
                        sent_at_ns, sequence = decoded
                        seen_sequences.add(sequence)
                        event_latency_samples_ms.append(
                            (time.perf_counter_ns() - sent_at_ns) / 1_000_000.0
                        )
                    if server_received >= expected_messages:
                        server_done.set()
                        break

            before_client = client.stats()
            before_server = server.stats()
            server_task = asyncio.create_task(server_loop(messages))

            started = time.perf_counter()
            for idx in range(messages):
                payload = make_timed_payload(payload_size, idx)
                assert client.send(payload, stream_id=50_000 + idx)
                if (idx + 1) % sender_batch_size == 0:
                    await asyncio.sleep(0)

            timed_out = False
            try:
                await asyncio.wait_for(server_done.wait(), timeout=10)
            except asyncio.TimeoutError:
                timed_out = True

            elapsed_s = time.perf_counter() - started
            if not server_done.is_set():
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task
            else:
                await asyncio.wait_for(server_task, timeout=1)

            after_client = client.stats()
            after_server = server.stats()
            client_delta = stat_delta(before_client, after_client)
            server_delta = stat_delta(before_server, after_server)

            sent_payload_bytes = messages * payload_size
            received_payload_bytes = server_received * payload_size
            payload_mb_s = (
                (received_payload_bytes / (1024.0 * 1024.0)) / elapsed_s if elapsed_s > 0 else 0.0
            )
            client_wire_tx_bytes = client_delta.get("tx_bytes", 0)
            server_wire_rx_bytes = server_delta.get("rx_bytes", 0)
            transport_efficiency = (
                (received_payload_bytes / client_wire_tx_bytes) if client_wire_tx_bytes > 0 else 0.0
            )
            message_delivery_ratio = ratio(float(server_received), float(messages))
            fragment_delivery_ratio = ratio(
                float(server_delta.get("transport_fragments_received", 0)),
                float(client_delta.get("transport_fragments_sent", 0)),
            )
            message_reassembly_ratio = ratio(
                float(server_delta.get("transport_messages_reassembled", 0)),
                float(messages),
            )
            callback_delivery_ratio = ratio(float(len(seen_sequences)), float(messages))

            results.append(
                {
                    "payload_size": payload_size,
                    "messages": messages,
                    "sender_batch_size": sender_batch_size,
                    "elapsed_s": elapsed_s,
                    "timed_out": timed_out,
                    "payload_mb_s": payload_mb_s,
                    "payload_mbit_s": payload_mb_s * 8.0,
                    "transport_efficiency": transport_efficiency,
                    "message_delivery_ratio": message_delivery_ratio,
                    "fragment_delivery_ratio": fragment_delivery_ratio,
                    "message_reassembly_ratio": message_reassembly_ratio,
                    "callback_delivery_ratio": callback_delivery_ratio,
                    "server_event_delivery_ms": summarize_samples_ms(event_latency_samples_ms),
                    "client_stat_delta": client_delta,
                    "server_stat_delta": server_delta,
                    "client_wire_tx_bytes": client_wire_tx_bytes,
                    "server_wire_rx_bytes": server_wire_rx_bytes,
                    "sent_payload_bytes": sent_payload_bytes,
                    "received_payload_bytes": received_payload_bytes,
                }
            )
            aggregate_client_stats = after_client
            aggregate_server_stats = after_server

    return {
        "test_name": "full_stack_ingress_only_throughput_sweep",
        "payload_sizes": payload_sizes,
        "sender_batch_size": sender_batch_size,
        "results": results,
        "client_stats": aggregate_client_stats,
        "server_stats": aggregate_server_stats,
    }


async def collect_ingress_pacing_sweep_metrics(
    *,
    psk: bytes,
    payload_size: int = 16_384,
    messages: int = 256,
) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None

    configurations = [
        {"name": "burst_batch8", "sender_batch_size": 8, "inter_batch_sleep_us": 0},
        {"name": "yield_batch4", "sender_batch_size": 4, "inter_batch_sleep_us": 0},
        {"name": "paced_100us_batch4", "sender_batch_size": 4, "inter_batch_sleep_us": 100},
        {"name": "paced_500us_batch4", "sender_batch_size": 4, "inter_batch_sleep_us": 500},
    ]

    results: list[dict[str, Any]] = []
    aggregate_client_stats: dict[str, int] = {}
    aggregate_server_stats: dict[str, int] = {}

    for config in configurations:
        bucket_port = reserve_ephemeral_port()
        server = Server(port=bucket_port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=30_000)
        client = Client(host="127.0.0.1", port=bucket_port, psk=psk, handshake_timeout_ms=2_000)

        async with server, client:
            await asyncio.wait_for(client.connect(), timeout=5)
            server_received = 0
            event_latency_samples_ms: list[float] = []
            seen_sequences: set[int] = set()
            server_done = asyncio.Event()

            async def server_loop(expected_messages: int) -> None:
                nonlocal server_received
                async for event in server.events():
                    if not isinstance(event, DataEvent):
                        continue
                    server_received += 1
                    decoded = extract_payload_timestamp_ns(event.data)
                    if decoded is not None:
                        sent_at_ns, sequence = decoded
                        seen_sequences.add(sequence)
                        event_latency_samples_ms.append(
                            (time.perf_counter_ns() - sent_at_ns) / 1_000_000.0
                        )
                    if server_received >= expected_messages:
                        server_done.set()
                        break

            before_client = client.stats()
            before_server = server.stats()
            server_task = asyncio.create_task(server_loop(messages))
            sleep_s = config["inter_batch_sleep_us"] / 1_000_000.0

            started = time.perf_counter()
            for idx in range(messages):
                payload = make_timed_payload(payload_size, idx)
                assert client.send(payload, stream_id=60_000 + idx)
                if (idx + 1) % config["sender_batch_size"] == 0:
                    await asyncio.sleep(sleep_s)

            timed_out = False
            try:
                await asyncio.wait_for(server_done.wait(), timeout=10)
            except asyncio.TimeoutError:
                timed_out = True

            elapsed_s = time.perf_counter() - started
            if not server_done.is_set():
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task
            else:
                await asyncio.wait_for(server_task, timeout=1)

            after_client = client.stats()
            after_server = server.stats()
            client_delta = stat_delta(before_client, after_client)
            server_delta = stat_delta(before_server, after_server)
            sent_payload_bytes = messages * payload_size
            received_payload_bytes = server_received * payload_size
            payload_mb_s = (
                (received_payload_bytes / (1024.0 * 1024.0)) / elapsed_s if elapsed_s > 0 else 0.0
            )

            results.append(
                {
                    "config_name": config["name"],
                    "payload_size": payload_size,
                    "messages": messages,
                    "sender_batch_size": config["sender_batch_size"],
                    "inter_batch_sleep_us": config["inter_batch_sleep_us"],
                    "elapsed_s": elapsed_s,
                    "timed_out": timed_out,
                    "payload_mb_s": payload_mb_s,
                    "payload_mbit_s": payload_mb_s * 8.0,
                    "message_delivery_ratio": ratio(float(server_received), float(messages)),
                    "fragment_delivery_ratio": ratio(
                        float(server_delta.get("transport_fragments_received", 0)),
                        float(client_delta.get("transport_fragments_sent", 0)),
                    ),
                    "message_reassembly_ratio": ratio(
                        float(server_delta.get("transport_messages_reassembled", 0)),
                        float(messages),
                    ),
                    "callback_delivery_ratio": ratio(float(len(seen_sequences)), float(messages)),
                    "server_event_delivery_ms": summarize_samples_ms(event_latency_samples_ms),
                    "client_stat_delta": client_delta,
                    "server_stat_delta": server_delta,
                    "client_wire_tx_bytes": client_delta.get("tx_bytes", 0),
                    "server_wire_rx_bytes": server_delta.get("rx_bytes", 0),
                }
            )
            aggregate_client_stats = after_client
            aggregate_server_stats = after_server

    return {
        "test_name": "full_stack_ingress_pacing_sweep",
        "payload_size": payload_size,
        "messages": messages,
        "results": results,
        "client_stats": aggregate_client_stats,
        "server_stats": aggregate_server_stats,
    }


async def collect_event_queue_overhead_metrics(
    *,
    psk: bytes,
    port: int,
    payload_size: int = 4096,
    messages: int = 128,
    sender_batch_size: int = 8,
) -> dict[str, Any]:
    assert Server is not None and Client is not None and DataEvent is not None

    server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
    client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)

    async with server, client:
        await asyncio.wait_for(client.connect(), timeout=5)
        probe = EventDeliveryProbe(server)
        probe.install()

        callback_to_enqueue_ms: list[float] = []
        enqueue_to_consumer_ms: list[float] = []
        callback_to_consumer_ms: list[float] = []
        send_to_consumer_ms: list[float] = []
        seen_sequences: set[int] = set()
        server_done = asyncio.Event()

        async def server_loop(expected_messages: int) -> None:
            async for event in server.events():
                if not isinstance(event, DataEvent):
                    continue
                decoded = extract_payload_timestamp_ns(event.data)
                if decoded is None:
                    continue
                sent_at_ns, sequence = decoded
                callback_ns, enqueued_ns = probe.snapshot(sequence)
                consumed_ns = time.perf_counter_ns()
                if callback_ns is not None:
                    callback_to_consumer_ms.append((consumed_ns - callback_ns) / 1_000_000.0)
                if callback_ns is not None and enqueued_ns is not None:
                    callback_to_enqueue_ms.append((enqueued_ns - callback_ns) / 1_000_000.0)
                    enqueue_to_consumer_ms.append((consumed_ns - enqueued_ns) / 1_000_000.0)
                send_to_consumer_ms.append((consumed_ns - sent_at_ns) / 1_000_000.0)
                seen_sequences.add(sequence)
                if len(seen_sequences) >= expected_messages:
                    server_done.set()
                    break

        before_client = client.stats()
        before_server = server.stats()
        server_task = asyncio.create_task(server_loop(messages))

        started = time.perf_counter()
        for idx in range(messages):
            payload = make_timed_payload(payload_size, idx)
            assert client.send(payload, stream_id=70_000 + idx)
            if (idx + 1) % sender_batch_size == 0:
                await asyncio.sleep(0)

        timed_out = False
        try:
            await asyncio.wait_for(server_done.wait(), timeout=10)
        except asyncio.TimeoutError:
            timed_out = True

        elapsed_s = time.perf_counter() - started
        if not server_done.is_set():
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
        else:
            await asyncio.wait_for(server_task, timeout=1)
        probe.uninstall()

        after_client = client.stats()
        after_server = server.stats()
        payload_mb_s = ((len(seen_sequences) * payload_size) / (1024.0 * 1024.0)) / elapsed_s if elapsed_s > 0 else 0.0

        return {
            "test_name": "full_stack_event_queue_overhead",
            "payload_size": payload_size,
            "messages": messages,
            "sender_batch_size": sender_batch_size,
            "elapsed_s": elapsed_s,
            "timed_out": timed_out,
            "payload_mb_s": payload_mb_s,
            "messages_consumed": len(seen_sequences),
            "message_delivery_ratio": ratio(float(len(seen_sequences)), float(messages)),
            "callback_to_enqueue_ms": summarize_samples_ms(callback_to_enqueue_ms),
            "enqueue_to_consumer_ms": summarize_samples_ms(enqueue_to_consumer_ms),
            "callback_to_consumer_ms": summarize_samples_ms(callback_to_consumer_ms),
            "send_to_consumer_ms": summarize_samples_ms(send_to_consumer_ms),
            "client_stat_delta": stat_delta(before_client, after_client),
            "server_stat_delta": stat_delta(before_server, after_server),
            "client_stats": after_client,
            "server_stats": after_server,
        }


async def collect_handshake_distribution_metrics(
    *,
    psk: bytes,
    repetitions: int = 8,
) -> dict[str, Any]:
    assert Server is not None and Client is not None

    samples_ms: list[float] = []
    disconnect_samples_ms: list[float] = []

    for _ in range(repetitions):
        port = reserve_ephemeral_port()
        server = Server(port=port, host="127.0.0.1", psk=psk, session_idle_timeout_ms=5_000)
        client = Client(host="127.0.0.1", port=port, psk=psk, handshake_timeout_ms=2_000)
        async with server, client:
            started = time.perf_counter()
            await asyncio.wait_for(client.connect(), timeout=5)
            samples_ms.append((time.perf_counter() - started) * 1000.0)

            stopped = time.perf_counter()
            client.stop()
            disconnect_samples_ms.append((time.perf_counter() - stopped) * 1000.0)

    return {
        "test_name": "handshake_distribution",
        "repetitions": repetitions,
        "handshake_samples_ms": samples_ms,
        "client_stop_samples_ms": disconnect_samples_ms,
        "handshake_ms": summarize_samples_ms(samples_ms),
        "client_stop_ms": summarize_samples_ms(disconnect_samples_ms),
    }


async def collect_stability_series_metrics(
    *,
    psk: bytes,
    sustained_repetitions: int = 5,
    windowed_repetitions: int = 4,
    ingress_pacing_repetitions: int = 4,
    sustained_messages: int = 64,
    sustained_payload_size: int = 2048,
    windowed_payload_sizes: list[int] | None = None,
    window_size: int = 8,
) -> dict[str, Any]:
    if windowed_payload_sizes is None:
        windowed_payload_sizes = [1024, 4096, 16384]

    sustained_runs: list[dict[str, Any]] = []
    for _ in range(sustained_repetitions):
        sustained_runs.append(
            await collect_sustained_throughput_metrics(
                psk=psk,
                port=reserve_ephemeral_port(),
                messages=sustained_messages,
                payload_size=sustained_payload_size,
            )
        )

    windowed_runs: dict[int, list[dict[str, Any]]] = {payload_size: [] for payload_size in windowed_payload_sizes}
    for payload_size in windowed_payload_sizes:
        for _ in range(windowed_repetitions):
            metrics = await collect_windowed_throughput_sweep_metrics(
                psk=psk,
                port=reserve_ephemeral_port(),
                payload_sizes=[payload_size],
                window_size=window_size,
            )
            windowed_runs[payload_size].append(metrics["results"][0])

    sustained_payload_mb_s = [run["payload_mb_s"] for run in sustained_runs]
    sustained_ack_loss = [run["ack_loss_ratio"] for run in sustained_runs]
    sustained_message_delivery = [run["message_delivery_ratio"] for run in sustained_runs]

    windowed_summary: list[dict[str, Any]] = []
    for payload_size, runs in windowed_runs.items():
        payload_mb_s = [run["payload_mb_s"] for run in runs]
        ack_loss = [run["ack_loss_ratio"] for run in runs]
        ack_delivery = [run["ack_delivery_ratio"] for run in runs]
        message_delivery = [run["message_delivery_ratio"] for run in runs]
        fragment_delivery = [run["fragment_delivery_ratio"] for run in runs]
        windowed_summary.append(
            {
                "payload_size": payload_size,
                "payload_mb_s": summarize_variance(payload_mb_s),
                "ack_loss_ratio": summarize_variance(ack_loss),
                "ack_delivery_ratio": summarize_variance(ack_delivery),
                "message_delivery_ratio": summarize_variance(message_delivery),
                "fragment_delivery_ratio": summarize_variance(fragment_delivery),
                "runs": runs,
            }
        )

    ingress_pacing_runs: dict[str, list[dict[str, Any]]] = {}
    for _ in range(ingress_pacing_repetitions):
        metrics = await collect_ingress_pacing_sweep_metrics(psk=psk)
        for run in metrics["results"]:
            ingress_pacing_runs.setdefault(run["config_name"], []).append(run)

    ingress_pacing_summary: list[dict[str, Any]] = []
    for config_name, runs in ingress_pacing_runs.items():
        payload_mb_s = [run["payload_mb_s"] for run in runs]
        message_delivery = [run["message_delivery_ratio"] for run in runs]
        fragment_delivery = [run["fragment_delivery_ratio"] for run in runs]
        callback_delivery = [run["callback_delivery_ratio"] for run in runs]
        event_p95 = [run["server_event_delivery_ms"]["p95_ms"] for run in runs]
        ingress_pacing_summary.append(
            {
                "config_name": config_name,
                "payload_mb_s": summarize_variance(payload_mb_s),
                "message_delivery_ratio": summarize_variance(message_delivery),
                "fragment_delivery_ratio": summarize_variance(fragment_delivery),
                "callback_delivery_ratio": summarize_variance(callback_delivery),
                "event_delivery_p95_ms": summarize_variance(event_p95),
                "timed_out_runs": sum(1 for run in runs if run.get("timed_out", False)),
                "runs": runs,
            }
        )

    return {
        "test_name": "full_stack_stability_series",
        "sustained_repetitions": sustained_repetitions,
        "windowed_repetitions": windowed_repetitions,
        "ingress_pacing_repetitions": ingress_pacing_repetitions,
        "sustained_messages": sustained_messages,
        "sustained_payload_size": sustained_payload_size,
        "window_size": window_size,
        "sustained_payload_mb_s": summarize_variance(sustained_payload_mb_s),
        "sustained_ack_loss_ratio": summarize_variance(sustained_ack_loss),
        "sustained_message_delivery_ratio": summarize_variance(sustained_message_delivery),
        "sustained_runs": sustained_runs,
        "windowed_runs": windowed_summary,
        "ingress_pacing_runs": ingress_pacing_summary,
    }


async def collect_fragment_failure_probe_metrics(
    *,
    psk: bytes,
    payload_sizes: list[int] | None = None,
    window_size: int = 8,
    max_attempts_per_payload: int = 8,
) -> dict[str, Any]:
    if payload_sizes is None:
        payload_sizes = [4096, 16384]

    payload_results: list[dict[str, Any]] = []
    for payload_size in payload_sizes:
        attempts: list[dict[str, Any]] = []
        failure_detected = False
        max_dropped_auth = 0
        max_dropped_fragment_invalid = 0
        max_dropped_fragment_reassembly = 0
        max_dropped_fragment_size_mismatch = 0
        max_dropped_small_packet = 0
        max_dropped_frame_decode = 0

        for _ in range(max_attempts_per_payload):
            metrics = await collect_windowed_throughput_sweep_metrics(
                psk=psk,
                port=reserve_ephemeral_port(),
                payload_sizes=[payload_size],
                window_size=window_size,
            )
            run = metrics["results"][0]
            server_delta = run["server_stat_delta"]
            client_delta = run["client_stat_delta"]
            attempt = {
                "payload_mb_s": run["payload_mb_s"],
                "ack_loss_ratio": run["ack_loss_ratio"],
                "ack_delivery_ratio": run["ack_delivery_ratio"],
                "message_delivery_ratio": run["message_delivery_ratio"],
                "fragment_delivery_ratio": run["fragment_delivery_ratio"],
                "message_reassembly_ratio": run["message_reassembly_ratio"],
                "server_transport_packets_dropped_auth": server_delta.get("transport_packets_dropped_auth", 0),
                "server_transport_packets_dropped_fragment_invalid": server_delta.get(
                    "transport_packets_dropped_fragment_invalid", 0
                ),
                "server_transport_packets_dropped_fragment_reassembly": server_delta.get(
                    "transport_packets_dropped_fragment_reassembly", 0
                ),
                "server_transport_packets_dropped_fragment_size_mismatch": server_delta.get(
                    "transport_packets_dropped_fragment_size_mismatch", 0
                ),
                "server_transport_packets_dropped_small_packet": server_delta.get(
                    "transport_packets_dropped_small_packet", 0
                ),
                "server_transport_packets_dropped_frame_decode": server_delta.get(
                    "transport_packets_dropped_frame_decode", 0
                ),
                "server_transport_packets_dropped_replay": server_delta.get("transport_packets_dropped_replay", 0),
                "client_transport_packets_dropped_auth": client_delta.get("transport_packets_dropped_auth", 0),
            }
            attempts.append(attempt)

            max_dropped_auth = max(max_dropped_auth, int(attempt["server_transport_packets_dropped_auth"]))
            max_dropped_fragment_invalid = max(
                max_dropped_fragment_invalid,
                int(attempt["server_transport_packets_dropped_fragment_invalid"]),
            )
            max_dropped_fragment_reassembly = max(
                max_dropped_fragment_reassembly,
                int(attempt["server_transport_packets_dropped_fragment_reassembly"]),
            )
            max_dropped_fragment_size_mismatch = max(
                max_dropped_fragment_size_mismatch,
                int(attempt["server_transport_packets_dropped_fragment_size_mismatch"]),
            )
            max_dropped_small_packet = max(
                max_dropped_small_packet,
                int(attempt["server_transport_packets_dropped_small_packet"]),
            )
            max_dropped_frame_decode = max(
                max_dropped_frame_decode,
                int(attempt["server_transport_packets_dropped_frame_decode"]),
            )

            if (
                attempt["server_transport_packets_dropped_auth"] > 0
                or attempt["server_transport_packets_dropped_fragment_invalid"] > 0
                or attempt["server_transport_packets_dropped_fragment_reassembly"] > 0
                or attempt["server_transport_packets_dropped_fragment_size_mismatch"] > 0
                or attempt["server_transport_packets_dropped_small_packet"] > 0
                or attempt["server_transport_packets_dropped_frame_decode"] > 0
            ):
                failure_detected = True

        payload_results.append(
            {
                "payload_size": payload_size,
                "window_size": window_size,
                "failure_detected": failure_detected,
                "max_transport_packets_dropped_auth": max_dropped_auth,
                "max_transport_packets_dropped_fragment_invalid": max_dropped_fragment_invalid,
                "max_transport_packets_dropped_fragment_reassembly": max_dropped_fragment_reassembly,
                "max_transport_packets_dropped_fragment_size_mismatch": max_dropped_fragment_size_mismatch,
                "max_transport_packets_dropped_small_packet": max_dropped_small_packet,
                "max_transport_packets_dropped_frame_decode": max_dropped_frame_decode,
                "attempts": attempts,
            }
        )

    return {
        "test_name": "full_stack_fragment_failure_probe",
        "max_attempts_per_payload": max_attempts_per_payload,
        "payloads": payload_results,
    }


async def collect_full_stack_metrics() -> dict[str, Any]:
    if not EXT_AVAILABLE:
        raise RuntimeError("compiled veil_core._veil_core_ext is unavailable")

    psk = bytes([0x5A]) * 32
    tests = [
        await collect_roundtrip_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_reconnect_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_stream_fanout_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_payload_sweep_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_sustained_throughput_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_windowed_throughput_sweep_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_ingress_only_throughput_sweep_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_ingress_pacing_sweep_metrics(psk=psk),
        await collect_event_queue_overhead_metrics(psk=psk, port=reserve_ephemeral_port()),
        await collect_handshake_distribution_metrics(psk=psk),
        await collect_stability_series_metrics(psk=psk),
        await collect_fragment_failure_probe_metrics(psk=psk),
    ]
    return {
        "suite": "full_stack_e2e",
        "environment": {
            "python_version": os.sys.version,
            "asan_preload": os.environ.get("LD_PRELOAD", ""),
            "asan_options": os.environ.get("ASAN_OPTIONS", ""),
            "system_probes": collect_system_probes(),
        },
        "tests": tests,
    }
