from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from e2e_metrics import stat_delta  # noqa: E402
from export_quality_metrics import build_summary, parse_embedded_json, write_distribution_csvs  # noqa: E402


class ExportQualityMetricsTests(unittest.TestCase):
    def test_stat_delta_ignores_non_monotonic_gauges(self) -> None:
        self.assertEqual(
            stat_delta({"active_sessions": 1, "tx_bytes": 10}, {"active_sessions": 0, "tx_bytes": 25}),
            {"tx_bytes": 15},
        )

    def test_parse_embedded_json_extracts_payload_from_mixed_stdout(self) -> None:
        payload = parse_embedded_json("banner line\n{\n  \"throughput\": {\"mbps\": 123.4}\n}\n")
        self.assertEqual(payload["throughput"]["mbps"], 123.4)

    def test_build_summary_exposes_release_and_asan_core_metrics(self) -> None:
        full_stack = {
            "tests": [
                {
                    "test_name": "full_stack_roundtrip",
                    "handshake_ms": 10.0,
                    "roundtrip": {"p95_ms": 2.0},
                },
                {
                    "test_name": "full_stack_sustained_throughput",
                    "payload_mb_s": 3.0,
                    "ack_loss_ratio": 0.0,
                    "ack_delivery_ratio": 1.0,
                    "message_delivery_ratio": 1.0,
                    "fragment_delivery_ratio": 1.0,
                    "message_reassembly_ratio": 1.0,
                },
                {
                    "test_name": "full_stack_windowed_throughput_sweep",
                    "results": [
                        {
                            "payload_size": 4096,
                            "payload_mb_s": 9.0,
                            "payload_mbit_s": 72.0,
                            "ack_latency_ms": {"p95_ms": 4.0},
                            "ack_delivery_ratio": 1.0,
                            "message_delivery_ratio": 1.0,
                            "fragment_delivery_ratio": 1.0,
                            "message_reassembly_ratio": 1.0,
                            "transport_efficiency": 0.95,
                            "client_stat_delta": {
                                "udp_tx_send_batch_calls": 2,
                                "udp_tx_packets_via_sendmmsg": 16,
                                "udp_tx_packets_via_sendto_fallback": 0,
                                "udp_tx_sendto_calls": 0,
                            },
                            "server_stat_delta": {
                                "transport_ack_delayed": 10,
                                "transport_ack_immediate": 5,
                                "transport_ack_coalesced": 20,
                                "transport_congestion_pacing_delays": 7,
                                "transport_congestion_pacing_tokens_granted": 100,
                                "transport_congestion_peak_cwnd": 65536,
                                "transport_reassembly_fast_path_messages": 9,
                                "transport_reassembly_fallback_messages": 1,
                                "transport_reassembly_fast_path_bytes": 36864,
                                "transport_reassembly_fallback_bytes": 4096,
                                "udp_tx_send_batch_calls": 4,
                                "udp_tx_packets_via_sendmmsg": 16,
                                "udp_tx_packets_via_sendto_fallback": 0,
                                "udp_tx_sendto_calls": 0,
                                "udp_rx_poll_wakeups": 8,
                                "udp_rx_recvmmsg_calls": 4,
                                "udp_rx_packets_via_recvmmsg": 16,
                                "udp_rx_recvfrom_calls": 0,
                                "udp_rx_recvfrom_fallback_calls": 0,
                                "udp_rx_packets_delivered": 16,
                            },
                        }
                    ],
                },
                {
                    "test_name": "full_stack_ingress_only_throughput_sweep",
                    "results": [
                        {
                            "payload_size": 16384,
                            "payload_mb_s": 999.0,
                            "payload_mbit_s": 7992.0,
                            "timed_out": True,
                            "server_event_delivery_ms": {"p95_ms": 999.0},
                            "callback_delivery_ratio": 0.1,
                            "message_delivery_ratio": 0.1,
                            "fragment_delivery_ratio": 0.1,
                            "message_reassembly_ratio": 0.1,
                        },
                        {
                            "payload_size": 4096,
                            "payload_mb_s": 12.0,
                            "payload_mbit_s": 96.0,
                            "timed_out": False,
                            "server_event_delivery_ms": {"p95_ms": 11.0},
                            "callback_delivery_ratio": 1.0,
                            "message_delivery_ratio": 1.0,
                            "fragment_delivery_ratio": 1.0,
                            "message_reassembly_ratio": 1.0,
                        }
                    ],
                },
                {
                    "test_name": "full_stack_event_queue_overhead",
                    "payload_mb_s": 8.0,
                    "callback_to_enqueue_ms": {"p95_ms": 0.1},
                    "enqueue_to_consumer_ms": {"p95_ms": 0.2},
                    "callback_to_consumer_ms": {"p95_ms": 0.3},
                    "send_to_consumer_ms": {"p95_ms": 12.0},
                },
                {
                    "test_name": "full_stack_ingress_pacing_sweep",
                    "results": [
                        {
                            "config_name": "burst_batch8",
                            "payload_mb_s": 4.0,
                            "timed_out": True,
                            "message_delivery_ratio": 0.5,
                            "server_event_delivery_ms": {"p95_ms": 40.0},
                        },
                        {
                            "config_name": "paced_100us_batch4",
                            "payload_mb_s": 18.0,
                            "timed_out": False,
                            "message_delivery_ratio": 1.0,
                            "server_event_delivery_ms": {"p95_ms": 9.0},
                        },
                    ],
                },
                {
                    "test_name": "handshake_distribution",
                    "handshake_samples_ms": [10.0, 12.0],
                    "client_stop_samples_ms": [0.5, 0.6],
                    "handshake_ms": {"p95_ms": 5.0},
                },
                {
                    "test_name": "full_stack_stability_series",
                    "ingress_pacing_repetitions": 4,
                    "sustained_payload_mb_s": {"cv": 0.1},
                    "sustained_ack_loss_ratio": {"avg": 0.0},
                    "sustained_message_delivery_ratio": {"avg": 1.0},
                    "sustained_runs": [
                        {
                            "payload_mb_s": 3.0,
                            "ack_loss_ratio": 0.0,
                            "ack_delivery_ratio": 1.0,
                            "message_delivery_ratio": 1.0,
                            "fragment_delivery_ratio": 1.0,
                            "message_reassembly_ratio": 1.0,
                        }
                    ],
                    "windowed_runs": [
                        {
                            "payload_size": 4096,
                            "payload_mb_s": {"cv": 0.2},
                            "runs": [
                                {
                                    "payload_mb_s": 9.0,
                                    "ack_loss_ratio": 0.0,
                                    "ack_delivery_ratio": 1.0,
                                    "message_delivery_ratio": 1.0,
                                    "fragment_delivery_ratio": 1.0,
                                    "message_reassembly_ratio": 1.0,
                                    "timed_out": False,
                                }
                            ],
                        }
                    ],
                    "ingress_pacing_runs": [
                        {
                            "config_name": "burst_batch8",
                            "payload_mb_s": {"cv": 0.35},
                            "timed_out_runs": 1,
                            "runs": [
                                {
                                    "payload_mb_s": 4.0,
                                    "message_delivery_ratio": 0.5,
                                    "fragment_delivery_ratio": 0.5,
                                    "callback_delivery_ratio": 0.5,
                                    "server_event_delivery_ms": {"p95_ms": 40.0},
                                    "timed_out": True,
                                }
                            ],
                        },
                        {
                            "config_name": "paced_100us_batch4",
                            "payload_mb_s": {"cv": 0.05},
                            "timed_out_runs": 0,
                            "runs": [
                                {
                                    "payload_mb_s": 18.0,
                                    "message_delivery_ratio": 1.0,
                                    "fragment_delivery_ratio": 1.0,
                                    "callback_delivery_ratio": 1.0,
                                    "server_event_delivery_ms": {"p95_ms": 9.0},
                                    "timed_out": False,
                                }
                            ],
                        },
                    ],
                },
                {
                    "test_name": "full_stack_fragment_failure_probe",
                    "payloads": [
                        {
                            "max_transport_packets_dropped_auth": 0,
                            "max_transport_packets_dropped_fragment_invalid": 0,
                            "max_transport_packets_dropped_fragment_reassembly": 0,
                            "max_transport_packets_dropped_fragment_size_mismatch": 0,
                            "max_transport_packets_dropped_small_packet": 0,
                            "max_transport_packets_dropped_frame_decode": 0,
                        }
                    ],
                },
            ]
        }
        protocol_quality = {
            "status": "ok",
            "returncode": 0,
            "tests": [
                {
                    "test_name": "LoopbackRoundTripLatencyMetricsStaySane",
                    "properties": {"avg_round_trip_ms": 0.4, "p95_round_trip_ms": 0.9},
                },
                {
                    "test_name": "CoreEncryptDecryptThroughputMetricsStaySane",
                    "properties": {"core_payload_throughput_mb_s": 6.8},
                },
            ]
        }
        release_core = {
            "throughput": {"mbps": 512.0, "target_mbps": 500.0, "passed": True},
        }

        summary = build_summary(full_stack, protocol_quality, release_core)

        self.assertEqual(
            summary["protocol_quality"]["asan_sanity_core_payload_throughput_mb_s"], 6.8
        )
        self.assertEqual(
            summary["protocol_quality"]["release_like_core_payload_throughput_mbps"], 512.0
        )
        self.assertTrue(summary["protocol_quality"]["release_like_core_passed"])
        self.assertEqual(summary["full_stack"]["best_ingress_payload_mb_s"], 12.0)
        self.assertEqual(summary["full_stack"]["ingress_timed_out_buckets"], 1)
        self.assertEqual(summary["full_stack"]["best_ingress_pacing_config"], "paced_100us_batch4")
        self.assertEqual(summary["full_stack"]["best_ingress_pacing_payload_mb_s"], 18.0)
        self.assertEqual(summary["full_stack"]["ingress_pacing_timed_out_configs"], 1)
        self.assertAlmostEqual(summary["full_stack"]["best_ingress_pacing_vs_unpaced_ratio"], 1.5)
        self.assertEqual(summary["full_stack"]["stability_ingress_pacing_repetitions"], 4)
        self.assertEqual(summary["full_stack"]["stability_noisiest_windowed_payload_size"], 4096)
        self.assertEqual(summary["full_stack"]["stability_noisiest_ingress_pacing_config"], "burst_batch8")
        self.assertEqual(summary["full_stack"]["stability_noisiest_ingress_pacing_timed_out_runs"], 1)
        self.assertEqual(summary["full_stack"]["stability_most_stable_ingress_pacing_config"], "paced_100us_batch4")
        self.assertEqual(summary["full_stack"]["event_queue_callback_to_consumer_p95_ms"], 0.3)
        self.assertEqual(summary["full_stack"]["best_windowed_server_ack_delayed"], 10)
        self.assertEqual(summary["full_stack"]["best_windowed_server_reassembly_fast_path_messages"], 9)
        self.assertAlmostEqual(summary["full_stack"]["best_windowed_server_reassembly_fast_path_ratio"], 0.9)
        self.assertEqual(summary["full_stack"]["best_windowed_udp_tx_packets_per_batch_call"], 4.0)
        self.assertEqual(summary["full_stack"]["best_windowed_client_udp_tx_packets_per_batch_call"], 8.0)
        self.assertIsNone(summary["full_stack"]["best_windowed_udp_rx_packets_per_recvfrom"])
        self.assertEqual(summary["full_stack"]["best_windowed_udp_rx_packets_per_receive_syscall"], 4.0)

    def test_build_summary_preserves_protocol_failure_status(self) -> None:
        summary = build_summary(
            None,
            {
                "status": "completed_with_fail_status",
                "returncode": 1,
                "tests": [
                    {
                        "test_name": "LoopbackRoundTripLatencyMetricsStaySane",
                        "properties": {"avg_round_trip_ms": 0.4, "p95_round_trip_ms": 0.9},
                    },
                    {
                        "test_name": "CoreEncryptDecryptThroughputMetricsStaySane",
                        "properties": {"core_payload_throughput_mb_s": 4.1},
                    },
                ],
            },
            None,
        )

        self.assertEqual(summary["protocol_quality"]["status"], "completed_with_fail_status")
        self.assertEqual(summary["protocol_quality"]["returncode"], 1)
        self.assertEqual(summary["protocol_quality"]["asan_sanity_core_payload_throughput_mb_s"], 4.1)

    def test_write_distribution_csvs_flattens_series(self) -> None:
        metrics = {
            "binding_and_protocol_e2e": {
                "tests": [
                    {
                        "test_name": "handshake_distribution",
                        "handshake_samples_ms": [10.0, 12.0],
                        "client_stop_samples_ms": [0.5, 0.6],
                    },
                    {
                        "test_name": "full_stack_stability_series",
                        "sustained_runs": [
                            {
                                "payload_mb_s": 3.0,
                                "ack_loss_ratio": 0.0,
                                "ack_delivery_ratio": 1.0,
                                "message_delivery_ratio": 1.0,
                                "fragment_delivery_ratio": 1.0,
                                "message_reassembly_ratio": 1.0,
                            }
                        ],
                        "windowed_runs": [
                            {
                                "payload_size": 4096,
                                "runs": [
                                    {
                                        "payload_mb_s": 9.0,
                                        "ack_loss_ratio": 0.0,
                                        "ack_delivery_ratio": 1.0,
                                        "message_delivery_ratio": 1.0,
                                        "fragment_delivery_ratio": 1.0,
                                        "message_reassembly_ratio": 1.0,
                                        "timed_out": False,
                                    }
                                ],
                            }
                        ],
                        "ingress_pacing_runs": [
                            {
                                "config_name": "yield_batch4",
                                "runs": [
                                    {
                                        "payload_mb_s": 80.0,
                                        "message_delivery_ratio": 1.0,
                                        "fragment_delivery_ratio": 1.0,
                                        "callback_delivery_ratio": 1.0,
                                        "server_event_delivery_ms": {"p95_ms": 8.0},
                                        "timed_out": False,
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        }

        normal_tests_dir = Path(str(TESTS_DIR).replace("\\\\?\\", ""))
        temp_prefix = next(tempfile._get_candidate_names())
        output_path = normal_tests_dir / f"{temp_prefix}_metrics.json"
        generated_paths = [
            output_path,
            output_path.with_name(f"{output_path.stem}_handshake_distribution.csv"),
            output_path.with_name(f"{output_path.stem}_sustained_runs.csv"),
            output_path.with_name(f"{output_path.stem}_windowed_runs.csv"),
            output_path.with_name(f"{output_path.stem}_ingress_pacing_runs.csv"),
        ]
        try:
            csv_paths = write_distribution_csvs(metrics, output_path)

            self.assertEqual(len(csv_paths), 4)
            handshake_csv = output_path.with_name(f"{output_path.stem}_handshake_distribution.csv")
            sustained_csv = output_path.with_name(f"{output_path.stem}_sustained_runs.csv")
            windowed_csv = output_path.with_name(f"{output_path.stem}_windowed_runs.csv")
            ingress_csv = output_path.with_name(f"{output_path.stem}_ingress_pacing_runs.csv")
            self.assertTrue(handshake_csv.exists())
            self.assertTrue(sustained_csv.exists())
            self.assertTrue(windowed_csv.exists())
            self.assertTrue(ingress_csv.exists())
            self.assertIn("sample_index,handshake_ms,client_stop_ms", handshake_csv.read_text(encoding="utf-8"))
            self.assertIn("4096,1,9.0", windowed_csv.read_text(encoding="utf-8"))
            self.assertIn("yield_batch4,1,80.0", ingress_csv.read_text(encoding="utf-8"))
        finally:
            for path in generated_paths:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()


if __name__ == "__main__":
    unittest.main()
