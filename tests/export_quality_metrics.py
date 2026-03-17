from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


THIS_DIR = Path(__file__).resolve().parent
PYTHON_ROOT = THIS_DIR.parent
REPO_ROOT = PYTHON_ROOT.parent.parent

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from e2e_metrics import EXT_AVAILABLE, collect_full_stack_metrics  # noqa: E402


TRANSPORT_FILTER = (
    "TransportIntegrationTest.LoopbackRoundTripLatencyMetricsStaySane:"
    "TransportIntegrationTest.CoreEncryptDecryptThroughputMetricsStaySane"
)


def parse_embedded_json(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in tool output")
    return json.loads(stdout[start : end + 1])


def parse_gtest_xml(xml_path: Path) -> list[dict[str, Any]]:
    root = ET.parse(xml_path).getroot()
    results: list[dict[str, Any]] = []
    for suite in root.findall("testsuite"):
        suite_name = suite.attrib.get("name", "")
        for testcase in suite.findall("testcase"):
            properties = {}
            properties_node = testcase.find("properties")
            if properties_node is not None:
                for prop in properties_node.findall("property"):
                    raw_value = prop.attrib.get("value", "")
                    try:
                        value: Any = float(raw_value)
                    except ValueError:
                        value = raw_value
                    properties[prop.attrib.get("name", "")] = value
            results.append(
                {
                    "suite": suite_name,
                    "test_name": testcase.attrib.get("name", ""),
                    "time_s": float(testcase.attrib.get("time", "0")),
                    "properties": properties,
                }
            )
    return results


def collect_protocol_quality_metrics() -> dict[str, Any]:
    binary = REPO_ROOT / "build" / "tests" / "integration" / "veil_integration_transport"
    if not binary.exists():
        raise FileNotFoundError(f"transport integration binary not found: {binary}")

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        xml_path = Path(tmp.name)

    command = [
        str(binary),
        f"--gtest_filter={TRANSPORT_FILTER}",
        f"--gtest_output=xml:{xml_path}",
    ]
    env = dict(os.environ)
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    tests: list[dict[str, Any]] = []
    if xml_path.exists():
        tests = parse_gtest_xml(xml_path)

    result = {
        "suite": "protocol_quality",
        "status": "ok" if completed.returncode == 0 else "completed_with_fail_status",
        "returncode": completed.returncode,
        "tests": tests,
        "source_binary": str(binary),
        "raw_stdout": completed.stdout,
        "raw_stderr": completed.stderr,
    }
    if completed.returncode != 0 and not tests:
        result["reason"] = "protocol_quality_run_failed_without_xml"
    return result


def collect_release_core_benchmark() -> dict[str, Any] | None:
    if os.name == "nt":
        return None

    release_binary_dir = REPO_ROOT / "build" / "release"
    configure_command = ["cmake", "--preset", "release", "-DVEIL_BUILD_TOOLS=ON"]
    build_command = ["cmake", "--build", "--preset", "release", "--target", "veil-performance-validation"]
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    env.pop("ASAN_OPTIONS", None)

    try:
        subprocess.run(configure_command, cwd=REPO_ROOT, env=env, check=True)
        subprocess.run(build_command, cwd=REPO_ROOT, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        return {
            "suite": "release_like_core_benchmark",
            "status": "unavailable",
            "reason": f"build_failed: {exc}",
        }

    binary = release_binary_dir / "src" / "tools" / "veil-performance-validation"
    if not binary.exists():
        return {
            "suite": "release_like_core_benchmark",
            "status": "unavailable",
            "reason": f"binary_not_found: {binary}",
        }

    command = [
        str(binary),
        "--test=throughput",
        "--duration=3",
        "--size=16384",
        "--json",
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        parsed = parse_embedded_json(completed.stdout)
    except ValueError:
        return {
            "suite": "release_like_core_benchmark",
            "binary": str(binary),
            "status": "unavailable",
            "reason": f"run_failed: exit={completed.returncode}",
            "raw_stdout": completed.stdout,
            "raw_stderr": completed.stderr,
        }
    return {
        "suite": "release_like_core_benchmark",
        "binary": str(binary),
        "status": "ok" if completed.returncode == 0 else "completed_with_fail_status",
        "returncode": completed.returncode,
        "throughput": parsed.get("throughput", {}),
        "raw_stdout": completed.stdout,
        "raw_stderr": completed.stderr,
    }


def build_summary(
    full_stack: dict[str, Any] | None,
    protocol_quality: dict[str, Any],
    release_core: dict[str, Any] | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}

    if full_stack is not None:
        tests = {test["test_name"]: test for test in full_stack["tests"]}
        roundtrip = tests.get("full_stack_roundtrip", {})
        sustained = tests.get("full_stack_sustained_throughput", {})
        windowed = tests.get("full_stack_windowed_throughput_sweep", {})
        ingress_only = tests.get("full_stack_ingress_only_throughput_sweep", {})
        ingress_pacing = tests.get("full_stack_ingress_pacing_sweep", {})
        event_queue = tests.get("full_stack_event_queue_overhead", {})
        handshake_distribution = tests.get("handshake_distribution", {})
        stability_series = tests.get("full_stack_stability_series", {})
        fragment_probe = tests.get("full_stack_fragment_failure_probe", {})
        windowed_results = windowed.get("results", [])
        best_windowed = max(windowed_results, key=lambda result: result.get("payload_mb_s", 0.0), default={})
        ingress_results = ingress_only.get("results", [])
        stable_ingress_results = [result for result in ingress_results if not result.get("timed_out", False)]
        best_ingress = max(stable_ingress_results, key=lambda result: result.get("payload_mb_s", 0.0), default={})
        ingress_pacing_results = ingress_pacing.get("results", [])
        stable_ingress_pacing_results = [
            result for result in ingress_pacing_results if not result.get("timed_out", False)
        ]
        best_ingress_pacing = max(
            stable_ingress_pacing_results,
            key=lambda result: result.get("payload_mb_s", 0.0),
            default={},
        )
        ingress_by_size = {result.get("payload_size"): result for result in ingress_results}
        matching_ingress = ingress_by_size.get(best_windowed.get("payload_size"))
        stability_windowed = stability_series.get("windowed_runs", [])
        stability_ingress_pacing = stability_series.get("ingress_pacing_runs", [])
        fragment_payloads = fragment_probe.get("payloads", [])
        worst_auth = max((payload.get("max_transport_packets_dropped_auth", 0) for payload in fragment_payloads), default=0)
        worst_fragment_invalid = max(
            (payload.get("max_transport_packets_dropped_fragment_invalid", 0) for payload in fragment_payloads),
            default=0,
        )
        worst_fragment_reassembly = max(
            (payload.get("max_transport_packets_dropped_fragment_reassembly", 0) for payload in fragment_payloads),
            default=0,
        )
        worst_fragment_size_mismatch = max(
            (payload.get("max_transport_packets_dropped_fragment_size_mismatch", 0) for payload in fragment_payloads),
            default=0,
        )
        worst_small_packet = max(
            (payload.get("max_transport_packets_dropped_small_packet", 0) for payload in fragment_payloads),
            default=0,
        )
        worst_frame_decode = max(
            (payload.get("max_transport_packets_dropped_frame_decode", 0) for payload in fragment_payloads),
            default=0,
        )
        worst_windowed_cv = max(
            (run.get("payload_mb_s", {}).get("cv", 0.0) for run in stability_windowed),
            default=0.0,
        )
        noisiest_windowed = max(
            stability_windowed,
            key=lambda run: run.get("payload_mb_s", {}).get("cv", 0.0),
            default={},
        )
        most_stable_windowed = min(
            stability_windowed,
            key=lambda run: run.get("payload_mb_s", {}).get("cv", float("inf")),
            default={},
        )
        worst_ingress_pacing_cv = max(
            (run.get("payload_mb_s", {}).get("cv", 0.0) for run in stability_ingress_pacing),
            default=0.0,
        )
        noisiest_ingress_pacing = max(
            stability_ingress_pacing,
            key=lambda run: run.get("payload_mb_s", {}).get("cv", 0.0),
            default={},
        )
        most_stable_ingress_pacing = min(
            stability_ingress_pacing,
            key=lambda run: run.get("payload_mb_s", {}).get("cv", float("inf")),
            default={},
        )
        summary["full_stack"] = {
            "handshake_ms": roundtrip.get("handshake_ms"),
            "roundtrip_p95_ms": roundtrip.get("roundtrip", {}).get("p95_ms"),
            "sustained_payload_mb_s": sustained.get("payload_mb_s"),
            "sustained_ack_loss_ratio": sustained.get("ack_loss_ratio"),
            "sustained_ack_delivery_ratio": sustained.get("ack_delivery_ratio"),
            "sustained_message_delivery_ratio": sustained.get("message_delivery_ratio"),
            "sustained_fragment_delivery_ratio": sustained.get("fragment_delivery_ratio"),
            "sustained_message_reassembly_ratio": sustained.get("message_reassembly_ratio"),
            "best_windowed_payload_size": best_windowed.get("payload_size"),
            "best_windowed_payload_mb_s": best_windowed.get("payload_mb_s"),
            "best_windowed_payload_mbit_s": best_windowed.get("payload_mbit_s"),
            "best_windowed_ack_p95_ms": best_windowed.get("ack_latency_ms", {}).get("p95_ms"),
            "best_windowed_ack_delivery_ratio": best_windowed.get("ack_delivery_ratio"),
            "best_windowed_message_delivery_ratio": best_windowed.get("message_delivery_ratio"),
            "best_windowed_fragment_delivery_ratio": best_windowed.get("fragment_delivery_ratio"),
            "best_windowed_message_reassembly_ratio": best_windowed.get("message_reassembly_ratio"),
            "best_windowed_efficiency": best_windowed.get("transport_efficiency"),
            "best_windowed_server_ack_delayed": best_windowed.get("server_stat_delta", {}).get("transport_ack_delayed"),
            "best_windowed_server_ack_immediate": best_windowed.get("server_stat_delta", {}).get("transport_ack_immediate"),
            "best_windowed_server_ack_coalesced": best_windowed.get("server_stat_delta", {}).get("transport_ack_coalesced"),
            "best_windowed_server_pacing_delays": best_windowed.get("server_stat_delta", {}).get("transport_congestion_pacing_delays"),
            "best_windowed_server_pacing_tokens": best_windowed.get("server_stat_delta", {}).get("transport_congestion_pacing_tokens_granted"),
            "best_windowed_server_peak_cwnd": best_windowed.get("server_stat_delta", {}).get("transport_congestion_peak_cwnd"),
            "best_windowed_server_reassembly_fast_path_messages": best_windowed.get("server_stat_delta", {}).get("transport_reassembly_fast_path_messages"),
            "best_windowed_server_reassembly_fallback_messages": best_windowed.get("server_stat_delta", {}).get("transport_reassembly_fallback_messages"),
            "best_windowed_server_reassembly_fast_path_bytes": best_windowed.get("server_stat_delta", {}).get("transport_reassembly_fast_path_bytes"),
            "best_windowed_server_reassembly_fallback_bytes": best_windowed.get("server_stat_delta", {}).get("transport_reassembly_fallback_bytes"),
            "best_windowed_server_reassembly_fast_path_ratio": (
                best_windowed.get("server_stat_delta", {}).get("transport_reassembly_fast_path_messages", 0.0)
                / max(
                    1.0,
                    best_windowed.get("server_stat_delta", {}).get("transport_reassembly_fast_path_messages", 0.0)
                    + best_windowed.get("server_stat_delta", {}).get("transport_reassembly_fallback_messages", 0.0),
                )
            ),
            "best_windowed_udp_tx_send_batch_calls": best_windowed.get("server_stat_delta", {}).get("udp_tx_send_batch_calls"),
            "best_windowed_udp_tx_packets_via_sendmmsg": best_windowed.get("server_stat_delta", {}).get("udp_tx_packets_via_sendmmsg"),
            "best_windowed_udp_tx_packets_via_sendto_fallback": best_windowed.get("server_stat_delta", {}).get("udp_tx_packets_via_sendto_fallback"),
            "best_windowed_udp_tx_sendto_calls": best_windowed.get("server_stat_delta", {}).get("udp_tx_sendto_calls"),
            "best_windowed_client_udp_tx_send_batch_calls": best_windowed.get("client_stat_delta", {}).get("udp_tx_send_batch_calls"),
            "best_windowed_client_udp_tx_packets_via_sendmmsg": best_windowed.get("client_stat_delta", {}).get("udp_tx_packets_via_sendmmsg"),
            "best_windowed_client_udp_tx_packets_via_sendto_fallback": best_windowed.get("client_stat_delta", {}).get("udp_tx_packets_via_sendto_fallback"),
            "best_windowed_client_udp_tx_sendto_calls": best_windowed.get("client_stat_delta", {}).get("udp_tx_sendto_calls"),
            "best_windowed_udp_rx_poll_wakeups": best_windowed.get("server_stat_delta", {}).get("udp_rx_poll_wakeups"),
            "best_windowed_udp_rx_recvmmsg_calls": best_windowed.get("server_stat_delta", {}).get("udp_rx_recvmmsg_calls"),
            "best_windowed_udp_rx_packets_via_recvmmsg": best_windowed.get("server_stat_delta", {}).get("udp_rx_packets_via_recvmmsg"),
            "best_windowed_udp_rx_recvfrom_calls": best_windowed.get("server_stat_delta", {}).get("udp_rx_recvfrom_calls"),
            "best_windowed_udp_rx_recvfrom_fallback_calls": best_windowed.get("server_stat_delta", {}).get("udp_rx_recvfrom_fallback_calls"),
            "best_windowed_udp_rx_packets_delivered": best_windowed.get("server_stat_delta", {}).get("udp_rx_packets_delivered"),
            "best_windowed_udp_tx_packets_per_batch_call": (
                (
                    best_windowed.get("server_stat_delta", {}).get("udp_tx_packets_via_sendmmsg", 0.0)
                    + best_windowed.get("server_stat_delta", {}).get("udp_tx_packets_via_sendto_fallback", 0.0)
                )
                / best_windowed.get("server_stat_delta", {}).get("udp_tx_send_batch_calls", 1.0)
                if best_windowed.get("server_stat_delta", {}).get("udp_tx_send_batch_calls", 0.0) > 0
                else None
            ),
            "best_windowed_client_udp_tx_packets_per_batch_call": (
                (
                    best_windowed.get("client_stat_delta", {}).get("udp_tx_packets_via_sendmmsg", 0.0)
                    + best_windowed.get("client_stat_delta", {}).get("udp_tx_packets_via_sendto_fallback", 0.0)
                )
                / best_windowed.get("client_stat_delta", {}).get("udp_tx_send_batch_calls", 1.0)
                if best_windowed.get("client_stat_delta", {}).get("udp_tx_send_batch_calls", 0.0) > 0
                else None
            ),
            "best_windowed_udp_rx_packets_per_recvfrom": (
                best_windowed.get("server_stat_delta", {}).get("udp_rx_packets_delivered", 0.0)
                / best_windowed.get("server_stat_delta", {}).get("udp_rx_recvfrom_calls", 1.0)
                if best_windowed.get("server_stat_delta", {}).get("udp_rx_recvfrom_calls", 0.0) > 0
                else None
            ),
            "best_windowed_udp_rx_packets_per_receive_syscall": (
                best_windowed.get("server_stat_delta", {}).get("udp_rx_packets_delivered", 0.0)
                / (
                    best_windowed.get("server_stat_delta", {}).get("udp_rx_recvmmsg_calls", 0.0)
                    + best_windowed.get("server_stat_delta", {}).get("udp_rx_recvfrom_calls", 0.0)
                )
                if (
                    best_windowed.get("server_stat_delta", {}).get("udp_rx_recvmmsg_calls", 0.0)
                    + best_windowed.get("server_stat_delta", {}).get("udp_rx_recvfrom_calls", 0.0)
                ) > 0
                else None
            ),
            "best_ingress_payload_size": best_ingress.get("payload_size"),
            "best_ingress_payload_mb_s": best_ingress.get("payload_mb_s"),
            "best_ingress_payload_mbit_s": best_ingress.get("payload_mbit_s"),
            "best_ingress_event_p95_ms": best_ingress.get("server_event_delivery_ms", {}).get("p95_ms"),
            "best_ingress_callback_delivery_ratio": best_ingress.get("callback_delivery_ratio"),
            "best_ingress_message_delivery_ratio": best_ingress.get("message_delivery_ratio"),
            "best_ingress_fragment_delivery_ratio": best_ingress.get("fragment_delivery_ratio"),
            "best_ingress_message_reassembly_ratio": best_ingress.get("message_reassembly_ratio"),
            "ingress_timed_out_buckets": sum(1 for result in ingress_results if result.get("timed_out", False)),
            "best_ingress_pacing_config": best_ingress_pacing.get("config_name"),
            "best_ingress_pacing_payload_mb_s": best_ingress_pacing.get("payload_mb_s"),
            "best_ingress_pacing_message_delivery_ratio": best_ingress_pacing.get("message_delivery_ratio"),
            "best_ingress_pacing_event_p95_ms": best_ingress_pacing.get("server_event_delivery_ms", {}).get("p95_ms"),
            "ingress_pacing_timed_out_configs": sum(
                1 for result in ingress_pacing_results if result.get("timed_out", False)
            ),
            "best_ingress_pacing_vs_unpaced_ratio": (
                best_ingress_pacing.get("payload_mb_s", 0.0) / best_ingress.get("payload_mb_s", 1.0)
                if best_ingress and best_ingress.get("payload_mb_s", 0.0) > 0
                else None
            ),
            "event_queue_payload_mb_s": event_queue.get("payload_mb_s"),
            "event_queue_callback_to_enqueue_p95_ms": event_queue.get("callback_to_enqueue_ms", {}).get("p95_ms"),
            "event_queue_enqueue_to_consumer_p95_ms": event_queue.get("enqueue_to_consumer_ms", {}).get("p95_ms"),
            "event_queue_callback_to_consumer_p95_ms": event_queue.get("callback_to_consumer_ms", {}).get("p95_ms"),
            "event_queue_send_to_consumer_p95_ms": event_queue.get("send_to_consumer_ms", {}).get("p95_ms"),
            "ingress_vs_windowed_same_payload_ratio": (
                (matching_ingress.get("payload_mb_s", 0.0) / best_windowed.get("payload_mb_s", 1.0))
                if matching_ingress and best_windowed.get("payload_mb_s", 0.0) > 0
                else None
            ),
            "ingress_vs_windowed_event_p95_delta_ms": (
                best_windowed.get("ack_latency_ms", {}).get("p95_ms", 0.0)
                - matching_ingress.get("server_event_delivery_ms", {}).get("p95_ms", 0.0)
                if matching_ingress
                else None
            ),
            "handshake_distribution_p95_ms": handshake_distribution.get("handshake_ms", {}).get("p95_ms"),
            "stability_sustained_payload_cv": stability_series.get("sustained_payload_mb_s", {}).get("cv"),
            "stability_sustained_ack_loss_avg": stability_series.get("sustained_ack_loss_ratio", {}).get("avg"),
            "stability_sustained_message_delivery_avg": stability_series.get(
                "sustained_message_delivery_ratio", {}
            ).get("avg"),
            "stability_worst_windowed_payload_cv": worst_windowed_cv,
            "stability_noisiest_windowed_payload_size": noisiest_windowed.get("payload_size"),
            "stability_noisiest_windowed_payload_cv": noisiest_windowed.get("payload_mb_s", {}).get("cv"),
            "stability_most_stable_windowed_payload_size": most_stable_windowed.get("payload_size"),
            "stability_most_stable_windowed_payload_cv": most_stable_windowed.get("payload_mb_s", {}).get("cv"),
            "stability_ingress_pacing_repetitions": stability_series.get("ingress_pacing_repetitions"),
            "stability_worst_ingress_pacing_payload_cv": worst_ingress_pacing_cv,
            "stability_noisiest_ingress_pacing_config": noisiest_ingress_pacing.get("config_name"),
            "stability_noisiest_ingress_pacing_payload_cv": noisiest_ingress_pacing.get("payload_mb_s", {}).get("cv"),
            "stability_noisiest_ingress_pacing_timed_out_runs": noisiest_ingress_pacing.get("timed_out_runs"),
            "stability_most_stable_ingress_pacing_config": most_stable_ingress_pacing.get("config_name"),
            "stability_most_stable_ingress_pacing_payload_cv": most_stable_ingress_pacing.get("payload_mb_s", {}).get("cv"),
            "fragment_probe_max_auth_drops": worst_auth,
            "fragment_probe_max_invalid_fragment_drops": worst_fragment_invalid,
            "fragment_probe_max_reassembly_drops": worst_fragment_reassembly,
            "fragment_probe_max_size_mismatch_drops": worst_fragment_size_mismatch,
            "fragment_probe_max_small_packet_drops": worst_small_packet,
            "fragment_probe_max_frame_decode_drops": worst_frame_decode,
        }

    protocol_tests = {test["test_name"]: test for test in protocol_quality.get("tests", [])}
    latency = protocol_tests.get("LoopbackRoundTripLatencyMetricsStaySane", {}).get("properties", {})
    throughput = protocol_tests.get("CoreEncryptDecryptThroughputMetricsStaySane", {}).get("properties", {})
    summary["protocol_quality"] = {
        "status": protocol_quality.get("status"),
        "returncode": protocol_quality.get("returncode"),
        "loopback_avg_round_trip_ms": latency.get("avg_round_trip_ms"),
        "loopback_p95_round_trip_ms": latency.get("p95_round_trip_ms"),
        "asan_sanity_core_payload_throughput_mb_s": throughput.get("core_payload_throughput_mb_s"),
        "release_like_core_payload_throughput_mbps": (
            release_core.get("throughput", {}).get("mbps") if release_core is not None else None
        ),
        "release_like_core_target_mbps": (
            release_core.get("throughput", {}).get("target_mbps") if release_core is not None else None
        ),
        "release_like_core_passed": (
            release_core.get("throughput", {}).get("passed") if release_core is not None else None
        ),
    }
    return summary


def _write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_distribution_csvs(metrics: dict[str, Any], output_path: Path) -> list[str]:
    written: list[str] = []
    full_stack = metrics.get("binding_and_protocol_e2e")
    if not isinstance(full_stack, dict):
        return written

    tests = {
        test.get("test_name"): test
        for test in full_stack.get("tests", [])
        if isinstance(test, dict) and isinstance(test.get("test_name"), str)
    }
    stem = output_path.stem

    handshake = tests.get("handshake_distribution", {})
    handshake_rows = [
        {
            "sample_index": index,
            "handshake_ms": handshake_ms,
            "client_stop_ms": stop_ms,
        }
        for index, (handshake_ms, stop_ms) in enumerate(
            zip(
                handshake.get("handshake_samples_ms", []),
                handshake.get("client_stop_samples_ms", []),
                strict=False,
            ),
            start=1,
        )
    ]
    if handshake_rows:
        path = output_path.with_name(f"{stem}_handshake_distribution.csv")
        _write_csv_rows(path, ["sample_index", "handshake_ms", "client_stop_ms"], handshake_rows)
        written.append(str(path))

    stability = tests.get("full_stack_stability_series", {})
    sustained_rows = [
        {
            "run_index": index,
            "payload_mb_s": run.get("payload_mb_s"),
            "ack_loss_ratio": run.get("ack_loss_ratio"),
            "ack_delivery_ratio": run.get("ack_delivery_ratio"),
            "message_delivery_ratio": run.get("message_delivery_ratio"),
            "fragment_delivery_ratio": run.get("fragment_delivery_ratio"),
            "message_reassembly_ratio": run.get("message_reassembly_ratio"),
        }
        for index, run in enumerate(stability.get("sustained_runs", []), start=1)
    ]
    if sustained_rows:
        path = output_path.with_name(f"{stem}_sustained_runs.csv")
        _write_csv_rows(
            path,
            [
                "run_index",
                "payload_mb_s",
                "ack_loss_ratio",
                "ack_delivery_ratio",
                "message_delivery_ratio",
                "fragment_delivery_ratio",
                "message_reassembly_ratio",
            ],
            sustained_rows,
        )
        written.append(str(path))

    windowed_rows: list[dict[str, Any]] = []
    for payload_bucket in stability.get("windowed_runs", []):
        payload_size = payload_bucket.get("payload_size")
        for index, run in enumerate(payload_bucket.get("runs", []), start=1):
            windowed_rows.append(
                {
                    "payload_size": payload_size,
                    "run_index": index,
                    "payload_mb_s": run.get("payload_mb_s"),
                    "ack_loss_ratio": run.get("ack_loss_ratio"),
                    "ack_delivery_ratio": run.get("ack_delivery_ratio"),
                    "message_delivery_ratio": run.get("message_delivery_ratio"),
                    "fragment_delivery_ratio": run.get("fragment_delivery_ratio"),
                    "message_reassembly_ratio": run.get("message_reassembly_ratio"),
                    "timed_out": run.get("timed_out", False),
                }
            )
    if windowed_rows:
        path = output_path.with_name(f"{stem}_windowed_runs.csv")
        _write_csv_rows(
            path,
            [
                "payload_size",
                "run_index",
                "payload_mb_s",
                "ack_loss_ratio",
                "ack_delivery_ratio",
                "message_delivery_ratio",
                "fragment_delivery_ratio",
                "message_reassembly_ratio",
                "timed_out",
            ],
            windowed_rows,
        )
        written.append(str(path))

    ingress_pacing_rows: list[dict[str, Any]] = []
    for config_bucket in stability.get("ingress_pacing_runs", []):
        config_name = config_bucket.get("config_name")
        for index, run in enumerate(config_bucket.get("runs", []), start=1):
            ingress_pacing_rows.append(
                {
                    "config_name": config_name,
                    "run_index": index,
                    "payload_mb_s": run.get("payload_mb_s"),
                    "message_delivery_ratio": run.get("message_delivery_ratio"),
                    "fragment_delivery_ratio": run.get("fragment_delivery_ratio"),
                    "callback_delivery_ratio": run.get("callback_delivery_ratio"),
                    "event_delivery_p95_ms": run.get("server_event_delivery_ms", {}).get("p95_ms"),
                    "timed_out": run.get("timed_out", False),
                }
            )
    if ingress_pacing_rows:
        path = output_path.with_name(f"{stem}_ingress_pacing_runs.csv")
        _write_csv_rows(
            path,
            [
                "config_name",
                "run_index",
                "payload_mb_s",
                "message_delivery_ratio",
                "fragment_delivery_ratio",
                "callback_delivery_ratio",
                "event_delivery_p95_ms",
                "timed_out",
            ],
            ingress_pacing_rows,
        )
        written.append(str(path))

    return written


async def collect_all_metrics() -> dict[str, Any]:
    full_stack = None
    if EXT_AVAILABLE:
        full_stack = await collect_full_stack_metrics()

    protocol_quality = collect_protocol_quality_metrics()
    release_core = collect_release_core_benchmark()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "binding_and_protocol_e2e": full_stack,
        "protocol_quality": protocol_quality,
        "release_like_core_benchmark": release_core,
        "summary": build_summary(full_stack, protocol_quality, release_core),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metrics = asyncio.run(collect_all_metrics())
    output_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    csv_paths = write_distribution_csvs(metrics, output_path)
    print(output_path)
    for csv_path in csv_paths:
        print(csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
