from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from e2e_metrics import EXT_AVAILABLE, collect_full_stack_metrics  # noqa: E402


@unittest.skipUnless(EXT_AVAILABLE, "compiled veil_core._veil_core_ext is unavailable")
class FullStackE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_full_stack_metrics_stay_sane(self) -> None:
        metrics = await collect_full_stack_metrics()
        tests = {test["test_name"]: test for test in metrics["tests"]}

        roundtrip = tests["full_stack_roundtrip"]
        self.assertLess(roundtrip["handshake_ms"], 2_000.0)
        self.assertLess(roundtrip["roundtrip"]["p95_ms"], 100.0)
        self.assertGreater(roundtrip["client_stats"]["tx_bytes"], 0)
        self.assertGreater(roundtrip["server_stats"]["rx_bytes"], 0)

        reconnect = tests["full_stack_reconnect"]
        self.assertLess(reconnect["disconnect_propagation_ms"], 2_000.0)
        self.assertLess(reconnect["reconnect_handshake_ms"], 2_000.0)
        self.assertNotEqual(reconnect["first_session_id"], reconnect["second_session_id"])

        fanout = tests["full_stack_stream_fanout"]
        self.assertEqual(len(fanout["server_observed"]), fanout["messages"])
        self.assertLess(fanout["roundtrip"]["p95_ms"], 100.0)

        payload_sweep = tests["full_stack_payload_sweep"]
        self.assertGreaterEqual(len(payload_sweep["results"]), 4)
        for result in payload_sweep["results"]:
            self.assertGreater(result["effective_payload_mb_s"]["avg"], 0.0)
            self.assertLess(result["latency_ms"]["p95_ms"], 250.0)

        sustained = tests["full_stack_sustained_throughput"]
        self.assertGreater(sustained["acks_received"], 0)
        self.assertLess(sustained["ack_loss_ratio"], 1.0)
        self.assertGreater(sustained["payload_mb_s"], 0.1)
        self.assertGreaterEqual(sustained["ack_delivery_ratio"], 0.0)
        self.assertGreaterEqual(sustained["message_delivery_ratio"], 0.0)
        self.assertGreaterEqual(sustained["fragment_delivery_ratio"], 0.0)
        self.assertGreaterEqual(sustained["message_reassembly_ratio"], 0.0)
        self.assertIn("client_stat_delta", sustained)
        self.assertIn("server_stat_delta", sustained)
        self.assertIn("transport_packets_dropped_decrypt", sustained["client_stats"])
        self.assertIn("transport_packets_dropped_decrypt", sustained["server_stats"])
        self.assertIn("transport_packets_dropped_auth", sustained["client_stats"])
        self.assertIn("transport_packets_dropped_fragment_invalid", sustained["server_stats"])
        self.assertIn("transport_packets_dropped_small_packet", sustained["server_stats"])
        self.assertIn("transport_packets_dropped_frame_decode", sustained["server_stats"])

        windowed = tests["full_stack_windowed_throughput_sweep"]
        self.assertGreaterEqual(len(windowed["results"]), 4)
        best_windowed_payload_mb_s = 0.0
        for result in windowed["results"]:
            self.assertLess(result["ack_loss_ratio"], 0.25)
            self.assertLess(result["ack_latency_ms"]["p95_ms"], 250.0)
            self.assertGreater(result["payload_mb_s"], 0.0)
            self.assertGreater(result["transport_efficiency"], 0.1)
            self.assertGreaterEqual(result["ack_delivery_ratio"], 0.0)
            self.assertGreaterEqual(result["message_delivery_ratio"], 0.0)
            self.assertGreaterEqual(result["fragment_delivery_ratio"], 0.0)
            self.assertGreaterEqual(result["message_reassembly_ratio"], 0.0)
            self.assertIn("client_stat_delta", result)
            self.assertIn("server_stat_delta", result)
            self.assertIn("transport_packets_dropped_auth", result["server_stat_delta"])
            self.assertIn("transport_packets_dropped_fragment_reassembly", result["server_stat_delta"])
            self.assertIn("transport_packets_dropped_small_packet", result["server_stat_delta"])
            self.assertIn("transport_packets_dropped_frame_decode", result["server_stat_delta"])
            best_windowed_payload_mb_s = max(best_windowed_payload_mb_s, result["payload_mb_s"])
        self.assertGreater(best_windowed_payload_mb_s, 0.1)

        ingress_only = tests["full_stack_ingress_only_throughput_sweep"]
        self.assertGreaterEqual(len(ingress_only["results"]), 4)
        best_ingress_payload_mb_s = 0.0
        for result in ingress_only["results"]:
            self.assertGreater(result["payload_mb_s"], 0.0)
            self.assertGreaterEqual(result["message_delivery_ratio"], 0.0)
            self.assertGreaterEqual(result["fragment_delivery_ratio"], 0.0)
            self.assertGreaterEqual(result["message_reassembly_ratio"], 0.0)
            self.assertGreaterEqual(result["callback_delivery_ratio"], 0.0)
            self.assertLess(result["server_event_delivery_ms"]["p95_ms"], 250.0)
            self.assertIn("client_stat_delta", result)
            self.assertIn("server_stat_delta", result)
            self.assertIn("transport_fragments_sent", result["client_stat_delta"])
            self.assertIn("transport_fragments_received", result["server_stat_delta"])
            if not result.get("timed_out", False):
                self.assertTrue(all(value >= 0 for value in result["client_stat_delta"].values()))
                self.assertTrue(all(value >= 0 for value in result["server_stat_delta"].values()))
            best_ingress_payload_mb_s = max(best_ingress_payload_mb_s, result["payload_mb_s"])
        self.assertGreater(best_ingress_payload_mb_s, 0.1)

        ingress_pacing = tests["full_stack_ingress_pacing_sweep"]
        self.assertGreaterEqual(len(ingress_pacing["results"]), 4)
        best_ingress_pacing_payload_mb_s = 0.0
        for result in ingress_pacing["results"]:
            self.assertIn("config_name", result)
            self.assertGreater(result["payload_mb_s"], 0.0)
            self.assertGreaterEqual(result["message_delivery_ratio"], 0.0)
            self.assertGreaterEqual(result["fragment_delivery_ratio"], 0.0)
            self.assertGreaterEqual(result["message_reassembly_ratio"], 0.0)
            self.assertGreaterEqual(result["callback_delivery_ratio"], 0.0)
            self.assertLess(result["server_event_delivery_ms"]["p95_ms"], 250.0)
            best_ingress_pacing_payload_mb_s = max(best_ingress_pacing_payload_mb_s, result["payload_mb_s"])
        self.assertGreater(best_ingress_pacing_payload_mb_s, 0.1)

        event_queue = tests["full_stack_event_queue_overhead"]
        self.assertGreater(event_queue["payload_mb_s"], 0.1)
        self.assertGreater(event_queue["messages_consumed"], 0)
        self.assertGreaterEqual(event_queue["message_delivery_ratio"], 0.0)
        self.assertLess(event_queue["callback_to_enqueue_ms"]["p95_ms"], 250.0)
        self.assertLess(event_queue["enqueue_to_consumer_ms"]["p95_ms"], 250.0)
        self.assertLess(event_queue["callback_to_consumer_ms"]["p95_ms"], 250.0)
        self.assertLess(event_queue["send_to_consumer_ms"]["p95_ms"], 250.0)
        self.assertIn("transport_fragments_sent", event_queue["client_stat_delta"])
        self.assertIn("transport_fragments_received", event_queue["server_stat_delta"])

        handshake_distribution = tests["handshake_distribution"]
        self.assertGreaterEqual(handshake_distribution["handshake_ms"]["count"], 4.0)
        self.assertLess(handshake_distribution["handshake_ms"]["p95_ms"], 500.0)

        stability = tests["full_stack_stability_series"]
        self.assertGreaterEqual(stability["sustained_repetitions"], 3)
        self.assertGreaterEqual(stability["sustained_payload_mb_s"]["count"], 3.0)
        self.assertGreaterEqual(stability["sustained_payload_mb_s"]["avg"], 0.0)
        self.assertGreaterEqual(stability["sustained_ack_loss_ratio"]["avg"], 0.0)
        self.assertGreaterEqual(stability["sustained_message_delivery_ratio"]["avg"], 0.0)
        self.assertGreaterEqual(len(stability["windowed_runs"]), 3)
        for run in stability["windowed_runs"]:
            self.assertIn("payload_size", run)
            self.assertIn("payload_mb_s", run)
            self.assertIn("ack_loss_ratio", run)
            self.assertIn("message_delivery_ratio", run)
            self.assertIn("runs", run)
        self.assertGreaterEqual(stability["ingress_pacing_repetitions"], 3)
        self.assertGreaterEqual(len(stability["ingress_pacing_runs"]), 4)
        for run in stability["ingress_pacing_runs"]:
            self.assertIn("config_name", run)
            self.assertIn("payload_mb_s", run)
            self.assertIn("message_delivery_ratio", run)
            self.assertIn("fragment_delivery_ratio", run)
            self.assertIn("callback_delivery_ratio", run)
            self.assertIn("event_delivery_p95_ms", run)
            self.assertIn("timed_out_runs", run)
            self.assertIn("runs", run)

        fragment_diag = tests["full_stack_fragment_failure_probe"]
        self.assertGreaterEqual(fragment_diag["max_attempts_per_payload"], 1)
        self.assertGreaterEqual(len(fragment_diag["payloads"]), 2)
        for payload in fragment_diag["payloads"]:
            self.assertIn("payload_size", payload)
            self.assertIn("attempts", payload)
            self.assertIn("failure_detected", payload)
            self.assertIn("max_transport_packets_dropped_auth", payload)
            self.assertIn("max_transport_packets_dropped_fragment_invalid", payload)
            self.assertIn("max_transport_packets_dropped_small_packet", payload)
            self.assertIn("max_transport_packets_dropped_frame_decode", payload)
